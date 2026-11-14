"""Slice I: what the stored files and the measurements say.

T1  best storage per category, from a benchmark that wrote the same workload to
    postgres, s3 and ftp (POST /analytics/measurements). The table shows the
    storage the current policy actually uses.
T2  normal day vs storm peak, from the scenario scripts. Two bars, fixed labels.
T3  zone picture: outage files, drone files, and meter anomalies detected from
    the reading itself (voltage, tamper events, power vs the meter's own median)
    compared with the simulator's sim_label.

Prometheus (slice H) is optional. When PROMETHEUS_URL answers, its numbers fill
the table for the current policy. When it does not, the table falls back to the
latest benchmark and the endpoint still returns 200.
"""

import asyncio
import json
import logging
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from statistics import median

from policies import CATEGORIES, STORAGES

log = logging.getLogger("filegrid.analytics")

ZONES = ("Muntenia", "Banat", "Dobrogea")
ANOMALY_LABELS = ("voltage_sag", "overvoltage", "load_spike", "tamper", "outage")
LOAD_LABELS = (("normal", "Zi normală"), ("peak", "Vârf furtună (10x)"))

# Same edges the simulator uses (sim.py): sag 170-200 V, overvoltage 254-270 V,
# outage 0 V, around a 230 V nominal. 207 and 253 are the EN 50160 band (±10%).
# A load spike is power above 3.5x the meter's own median: daily variation stays
# under ~1.6x, and the simulator multiplies a spike by 4-8. One reading has no
# median, so it is not called a spike.
OUTAGE_BELOW_V = 50.0
SAG_BELOW_V = 207.0
OVER_ABOVE_V = 253.0
SPIKE_FACTOR = 3.5
TAMPER_EVENTS = frozenset({"TAMPER_COVER_OPEN", "MAGNETIC_FIELD_DETECTED"})

WHY = {
    "meter_readings": "citiri mici, latență și unicitate",
    "outage_records": "trebuie disponibile imediat",
    "connection_docs": "documente cu date personale",
    "drone_videos": "fișiere mari, contează throughput-ul",
}
# Used only when two storages measure identically. The numbers still decide.
TIE_BREAK = {
    "meter_readings": {"postgres": 0, "s3": 1, "ftp": 2},
    "outage_records": {"s3": 0, "postgres": 1, "ftp": 2},
    "connection_docs": {"ftp": 0, "postgres": 1, "s3": 2},
    "drone_videos": {"s3": 0, "ftp": 1, "postgres": 2},
}
_CATEGORY_RO = {
    "meter_readings": "Citiri contoare",
    "outage_records": "Avarii",
    "connection_docs": "Documente de racordare",
    "drone_videos": "Video-uri dronă",
}
_WINDOW = re.compile(r"^[1-9][0-9]{0,3}[smhd]$")
# Same predicate as the SQL in PostgresRepo.analytics_snapshot (repo.py, slice I).
POWER_KW_PATTERN = r"^[0-9]+(\.[0-9]+)?$"


def classify_reading(voltage, power_kw, events, median_power) -> str | None:
    """Label one reading, or None when it looks normal. Rules are in this order."""
    if voltage is not None:
        if voltage < OUTAGE_BELOW_V:
            return "outage"
        if voltage < SAG_BELOW_V:
            return "voltage_sag"
        if voltage > OVER_ABOVE_V:
            return "overvoltage"
    if TAMPER_EVENTS.intersection(events or ()):
        return "tamper"
    if (
        power_kw is not None
        and median_power is not None
        and median_power > 0
        and power_kw > SPIKE_FACTOR * median_power
    ):
        return "load_spike"
    return None


def new_snapshot() -> dict:
    zones = {z: 0 for z in ZONES}
    return {
        "outages": dict(zones),
        "drones": dict(zones),
        "detected": dict(zones),
        "labelled": dict(zones),
        "confirmed": dict(zones),
        "totals": {"detected": 0, "labelled": 0, "confirmed": 0},
        "audit_connection_docs": 0,
    }


def _payload_doc(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            doc = json.loads(raw)
        except ValueError:
            return {}
        return doc if isinstance(doc, dict) else {}
    return {}


def _power_and_events(doc: dict) -> tuple[float | None, list]:
    instant = doc.get("instant") if isinstance(doc.get("instant"), dict) else {}
    power = instant.get("active_power_kw")
    if isinstance(power, bool) or not isinstance(power, (int, float)):
        power = None
    events = doc.get("events") if isinstance(doc.get("events"), list) else []
    return power, events


def snapshot_from_memory(files, readings, audit) -> dict:
    """Same numbers PostgresRepo.analytics_snapshot computes, for the test fake."""
    snap = new_snapshot()
    for rec in files.values():
        zone = getattr(rec, "zone", None)
        if zone not in ZONES:
            continue
        if rec.category == "outage_records":
            snap["outages"][zone] += 1
        elif rec.category == "drone_videos":
            snap["drones"][zone] += 1
    if audit:
        file_by_id = {rec.id: rec for rec in files.values()}
        for entry in audit:
            rec = file_by_id.get(entry.get("file_id"))
            if rec is not None and rec.category == "connection_docs":
                snap["audit_connection_docs"] += 1

    usable = []
    for row in readings.values():
        if row.get("status") not in ("stored", "queued"):
            continue
        doc = _payload_doc(row.get("payload"))
        power, events = _power_and_events(doc)
        usable.append((row, power, events))
    powers: dict[str, list[float]] = {}
    for row, power, _events in usable:
        if power is not None and power > 0:
            powers.setdefault(row.get("meter_id") or "", []).append(power)
    medians = {meter: median(vals) for meter, vals in powers.items() if vals}

    for row, power, events in usable:
        label = classify_reading(
            row.get("voltage_v"), power, events, medians.get(row.get("meter_id") or "")
        )
        sim = row.get("sim_label") or ""
        labelled = sim in ANOMALY_LABELS
        confirmed = label is not None and label == sim
        if label is not None:
            snap["totals"]["detected"] += 1
        if labelled:
            snap["totals"]["labelled"] += 1
        if confirmed:
            snap["totals"]["confirmed"] += 1
        zone = row.get("zone")
        if zone not in ZONES:
            continue
        if label is not None:
            snap["detected"][zone] += 1
        if labelled:
            snap["labelled"][zone] += 1
        if confirmed:
            snap["confirmed"][zone] += 1
    return snap


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _as_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def _as_float(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and >= 0")
    return number


def parse_measurement_document(doc) -> tuple[str, str | None, list[dict]]:
    """Validate a POST /analytics/measurements body. Raises ValueError -> HTTP 422."""
    if not isinstance(doc, dict):
        raise ValueError("body must be a JSON object")
    kind = doc.get("kind")
    if kind not in ("benchmark", "scenario"):
        raise ValueError("kind must be 'benchmark' or 'scenario'")
    scenario = doc.get("scenario")
    if kind == "benchmark":
        if scenario not in (None, ""):
            raise ValueError("a benchmark run has no scenario")
        scenario = None
    elif scenario not in ("normal", "peak"):
        raise ValueError("scenario must be 'normal' or 'peak'")
    rows = doc.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > 64:
        raise ValueError("rows must contain between 1 and 64 measurements")
    cleaned, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each row must be an object")
        category, storage = row.get("category"), row.get("storage")
        if category not in CATEGORIES:
            raise ValueError(f"unknown category '{category}'")
        if storage not in STORAGES:
            raise ValueError(f"unknown storage '{storage}'")
        if (category, storage) in seen:
            raise ValueError(f"duplicate measurement for {category} on {storage}")
        seen.add((category, storage))
        samples = _as_int(row.get("samples"), "samples")
        ok = _as_int(row.get("ok"), "ok")
        errors = _as_int(row.get("errors"), "errors")
        if samples < 1:
            raise ValueError("samples must be >= 1")
        if ok + errors != samples:
            raise ValueError("ok + errors must equal samples")
        cleaned.append({
            "category": category,
            "storage": storage,
            "samples": samples,
            "ok": ok,
            "errors": errors,
            "latency_avg_ms": _as_float(row.get("latency_avg_ms"), "latency_avg_ms"),
            "latency_p50_ms": _as_float(row.get("latency_p50_ms"), "latency_p50_ms"),
            "latency_p95_ms": _as_float(row.get("latency_p95_ms"), "latency_p95_ms"),
            "latency_p99_ms": _as_float(row.get("latency_p99_ms"), "latency_p99_ms"),
            "throughput_mbps": _as_float(row.get("throughput_mbps"), "throughput_mbps"),
            "bytes_total": _as_int(row.get("bytes_total"), "bytes_total"),
        })
    return kind, scenario, cleaned


def _promql(window: str) -> dict[str, str]:
    # Ingest and chunked upload are the writes. `window` matches _WINDOW, so it cannot
    # close the selector or inject another PromQL expression.
    write = 'operation=~"ingest|chunk"'
    duration = "filegrid_request_duration_seconds"
    selector = f"{{{write}}}"
    errors = f'{{{write},outcome=~"server_error|integrity_error"}}'
    return {
        "avg": (
            f"sum by (category, storage) (rate({duration}_sum{selector}[{window}]))"
            f" / sum by (category, storage) (rate({duration}_count{selector}[{window}]))"
        ),
        "p50": (
            "histogram_quantile(0.50, sum by (le, category, storage) "
            f"(rate({duration}_bucket{selector}[{window}])))"
        ),
        "p95": (
            "histogram_quantile(0.95, sum by (le, category, storage) "
            f"(rate({duration}_bucket{selector}[{window}])))"
        ),
        "p99": (
            "histogram_quantile(0.99, sum by (le, category, storage) "
            f"(rate({duration}_bucket{selector}[{window}])))"
        ),
        "bytes": (
            f"sum by (category, storage) (rate(filegrid_bytes_total{selector}[{window}]))"
        ),
        "errors": (
            f"sum by (category, storage) (rate(filegrid_requests_total{errors}[{window}]))"
            f" / sum by (category, storage) (rate(filegrid_requests_total{selector}[{window}]))"
        ),
    }


def _query(base_url: str, timeout: float, promql: str) -> list | None:
    """One instant query. None means this query did not succeed."""
    url = base_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None
    result = (payload.get("data") or {}).get("result")
    return result if isinstance(result, list) else None


def _merge_live(by_name: dict[str, list | None]) -> dict:
    acc: dict[tuple, dict] = {}

    def slot(metric: dict) -> dict | None:
        category, storage = metric.get("category"), metric.get("storage")
        if category not in CATEGORIES or storage not in STORAGES:
            return None
        return acc.setdefault((category, storage), {})

    scale = {
        "avg": ("avg_ms", 1000.0),
        "p50": ("p50_ms", 1000.0),
        "p95": ("p95_ms", 1000.0),
        "p99": ("p99_ms", 1000.0),
        "bytes": ("mbps", 1 / 1_000_000),
        "errors": ("error_rate", 1.0),
    }
    for name, rows in by_name.items():
        if not rows:
            continue
        field, factor = scale[name]
        for item in rows:
            if not isinstance(item, dict):
                continue
            number = _finite((item.get("value") or [None, None])[1])
            if number is None:
                continue
            dest = slot(item.get("metric") or {})
            if dest is not None:
                dest[field] = number * factor
    return acc


async def fetch_live_metrics(base_url: str, timeout: float, window: str) -> dict | None:
    """Prometheus instant vectors, or None when Prometheus cannot be reached.

    An empty dict means Prometheus answered and has no FileGrid series yet.
    """
    if not base_url or not base_url.startswith(("http://", "https://")):
        return None
    window = window if _WINDOW.match(window or "") else "5m"
    queries = _promql(window)
    try:
        rows = await asyncio.gather(*[
            asyncio.to_thread(_query, base_url, timeout, promql) for promql in queries.values()
        ])
    except Exception:
        log.info("prometheus query failed")
        return None
    paired = dict(zip(queries.keys(), rows))
    if all(value is None for value in paired.values()):
        return None
    return _merge_live(paired)


def _policy_storage(policies: dict, category: str) -> str:
    storage = (policies.get(category) or {}).get("storage")
    return storage if storage in STORAGES else "postgres"


def _bench_for(measurements, category: str) -> list[dict]:
    return [
        row for row in measurements
        if row["kind"] == "benchmark" and row["category"] == category
    ]


def _best(category: str, rows: list[dict]) -> dict | None:
    if len(rows) < 2:
        return None

    def score(row):
        samples = row["samples"] or 1
        error_rate = row["errors"] / samples
        prefer = TIE_BREAK[category].get(row["storage"], 9)
        if category == "drone_videos":
            return (-row["throughput_mbps"], error_rate, row["latency_p95_ms"], prefer)
        return (row["latency_p95_ms"], error_rate, -row["throughput_mbps"], prefer)

    return min(rows, key=score)


def _fmt_ms(value: float) -> str:
    return f"{value:.0f}"


def format_throughput(mbps: float) -> str:
    """MB/s from about 0.1 up; smaller rates in KB/s so a meter reading is not shown as 0.0."""
    if mbps >= 0.1:
        return f"{mbps:.1f} MB/s"
    return f"{mbps * 1000:.1f} KB/s"


def _others(best: dict, rows: list[dict]) -> str:
    parts = []
    for row in sorted(rows, key=lambda r: r["storage"]):
        if row["storage"] == best["storage"]:
            continue
        parts.append(f"{row['storage']} p95 {_fmt_ms(row['latency_p95_ms'])} ms")
    return ", ".join(parts)


def _row_verdict(category: str, current: str, rows: list[dict], audit_n: int) -> str:
    why = WHY[category]
    best = _best(category, rows)
    if best is None:
        if len(rows) == 1:
            only = rows[0]
            text = (
                f"Măsurat doar pe {only['storage']} "
                f"(p95 {_fmt_ms(only['latency_p95_ms'])} ms). "
                "Rulați scripts/benchmark/run.sh pe postgres, s3 și ftp pentru comparație."
            )
        else:
            text = (
                f"Politica ține categoria pe {current}. "
                "Rulați scripts/benchmark/run.sh pentru comparația între postgres, s3 și ftp."
            )
    else:
        detail = (
            f"p50 {_fmt_ms(best['latency_p50_ms'])} ms, "
            f"p95 {_fmt_ms(best['latency_p95_ms'])} ms, "
            f"p99 {_fmt_ms(best['latency_p99_ms'])} ms, "
            f"{format_throughput(best['throughput_mbps'])}"
        )
        others = _others(best, rows)
        if best["storage"] == current:
            text = (
                f"{current} este cea mai bună alegere ({why}): {detail}"
                + (f", față de {others}." if others else ".")
            )
        else:
            current_row = next((r for r in rows if r["storage"] == current), None)
            current_p95 = _fmt_ms(current_row["latency_p95_ms"]) if current_row else "n/a"
            text = (
                f"Politica folosește {current} (p95 {current_p95} ms); "
                f"benchmark-ul indică {best['storage']} ({why}): {detail}"
                + (f", față de {others}." if others else ".")
            )
    if category == "connection_docs":
        word = "eveniment" if audit_n == 1 else "evenimente"
        text += f" Audit: {audit_n} {word} pe documentele de racordare."
    return text


def _metrics_for(category: str, storage: str, live: dict | None, bench_rows: list[dict]):
    live_row = (live or {}).get((category, storage))
    if live_row:
        p95 = live_row.get("p95_ms")
        avg = live_row.get("avg_ms", p95 if p95 is not None else 0.0)
        if p95 is None:
            p95 = avg or 0.0
        return {
            "latency_avg_ms": round(float(avg or 0), 1),
            "latency_p95_ms": round(float(p95 or 0), 1),
            "throughput_mbps": round(float(live_row.get("mbps") or 0), 3),
            "error_rate": round(float(live_row.get("error_rate") or 0), 4),
        }, "live"
    match = next((r for r in bench_rows if r["storage"] == storage), None)
    if match is not None:
        samples = match["samples"] or 1
        return {
            "latency_avg_ms": round(match["latency_avg_ms"], 1),
            "latency_p95_ms": round(match["latency_p95_ms"], 1),
            "throughput_mbps": round(match["throughput_mbps"], 3),
            "error_rate": round(match["errors"] / samples, 4),
        }, "benchmark"
    return {
        "latency_avg_ms": 0.0,
        "latency_p95_ms": 0.0,
        "throughput_mbps": 0.0,
        "error_rate": 0.0,
    }, "none"


def _lead(sources: set[str], window: str) -> str:
    if "live" in sources and "benchmark" in sources:
        return "Numerele din tabel vin din Prometheus; unde nu are date, din ultimul benchmark."
    if "live" in sources:
        return f"Numerele din tabel vin din Prometheus (fereastra {window})."
    if "benchmark" in sources:
        return "Numerele din tabel vin din ultimul benchmark."
    return "Nu există încă măsurători de latență."


def _scenario_p95(measurements, scenario: str, category: str, storage: str) -> float:
    rows = [
        row for row in measurements
        if row["kind"] == "scenario" and row["scenario"] == scenario and row["category"] == category
    ]
    if not rows:
        return 0.0
    for row in rows:
        if row["storage"] == storage:
            return float(row["latency_p95_ms"])
    return sum(float(row["latency_p95_ms"]) for row in rows) / len(rows)


def storm_narrative(normal: dict[str, float], peak: dict[str, float]) -> str:
    if not normal or not peak:
        return (
            "Lipsește ziua normală sau vârful. "
            "Rulați scripts/scenarios/normal-day.sh, apoi scripts/scenarios/storm.sh."
        )
    parts = [
        f"{_CATEGORY_RO[cat]}: p95 de la {_fmt_ms(normal[cat])} ms la {_fmt_ms(peak[cat])} ms"
        for cat in CATEGORIES
    ]
    text = ". ".join(parts) + "."

    def growth(cat: str) -> float | None:
        base = normal[cat]
        if base <= 0:
            return None
        return (peak[cat] - base) / base

    growths = {cat: growth(cat) for cat in CATEGORIES}
    if any(value is None for value in growths.values()):
        return text
    spike = max(CATEGORIES, key=lambda cat: growths[cat])
    others = [cat for cat in CATEGORIES if cat != spike]
    if growths[spike] <= 0:
        text += " Latențele nu au crescut la vârf."
    elif all(growths[cat] < growths[spike] for cat in others):
        text += (
            " Vârful se vede la "
            + {
                "meter_readings": "citirile de contor",
                "outage_records": "înregistrările de avarie",
                "connection_docs": "documentele de racordare",
                "drone_videos": "video-urile de dronă",
            }[spike]
            + "; celelalte categorii au crescut mai puțin, deci vârful nu trage celelalte categorii după el."
        )
    else:
        text += (
            " O altă categorie a crescut la fel de tare; "
            "izolarea nu se vede în această măsurătoare."
        )
    return text


def _has_scenario(measurements, scenario: str) -> bool:
    return any(row["kind"] == "scenario" and row["scenario"] == scenario for row in measurements)


def network_narrative(snapshot: dict) -> str:
    detected = snapshot["totals"]["detected"]
    labelled = snapshot["totals"]["labelled"]
    confirmed = snapshot["totals"]["confirmed"]
    if detected == 0 and labelled == 0:
        return (
            "Nicio citire anormală în metadata. Detectorul compară tensiunea (în afara 207-253 V), "
            "evenimentele de tamper și puterea (peste "
            f"{SPIKE_FACTOR:g}× mediana contorului) cu sim_label."
        )
    pct = f" ({100.0 * confirmed / labelled:.0f}%)" if labelled else ""
    return (
        f"Detectorul a marcat {detected} citiri anormale. "
        f"Din {labelled} citiri etichetate de simulator, {confirmed} coincid cu sim_label{pct}."
    )


def _drone_status(inspections: int, outages: int) -> str:
    if inspections <= 0:
        return "pending"
    if outages > inspections:
        return "in_progress"
    return "completed"


def build_summary(policies: dict, measurements: list[dict], snapshot: dict,
                  live: dict | None, window: str = "5m") -> dict:
    """AnalyticsSummary (frontend/src/types/filegrid.ts). `live` is None when Prometheus is down."""
    window = window if _WINDOW.match(window or "") else "5m"
    matrix = []
    sources = set()
    audit_n = int(snapshot.get("audit_connection_docs") or 0)
    for category in CATEGORIES:
        storage = _policy_storage(policies, category)
        bench_rows = _bench_for(measurements, category)
        metrics, source = _metrics_for(category, storage, live, bench_rows)
        sources.add(source)
        matrix.append({
            "category": category,
            "storage": storage,
            "verdict": _row_verdict(category, storage, bench_rows, audit_n),
            **metrics,
        })

    by_scenario = {}
    for scenario, _label in LOAD_LABELS:
        by_scenario[scenario] = {
            category: _scenario_p95(measurements, scenario, category, _policy_storage(policies, category))
            for category in CATEGORIES
        }
    normal = by_scenario["normal"] if _has_scenario(measurements, "normal") else None
    peak = by_scenario["peak"] if _has_scenario(measurements, "peak") else None
    load = [
        {
            "label": label,
            "meter_readings_ms": round(by_scenario[scenario]["meter_readings"], 1),
            "outage_records_ms": round(by_scenario[scenario]["outage_records"], 1),
            "connection_docs_ms": round(by_scenario[scenario]["connection_docs"], 1),
            "drone_videos_ms": round(by_scenario[scenario]["drone_videos"], 1),
        }
        for scenario, label in LOAD_LABELS
    ]
    zones = []
    for zone in ZONES:
        outages = int(snapshot["outages"].get(zone, 0))
        drones = int(snapshot["drones"].get(zone, 0))
        zones.append({
            "zone": zone,
            "outages_reported": outages,
            "meter_anomalies": int(snapshot["detected"].get(zone, 0)),
            "drone_inspections": drones,
            "drone_status": _drone_status(drones, outages),
        })
    return {
        "storage_matrix": matrix,
        "load_comparison": load,
        "zone_insights": zones,
        "narrative": {
            "storage_verdict": _lead(sources, window) + " " + " ".join(row["verdict"] for row in matrix),
            "storm_isolation": storm_narrative(normal, peak),
            "network": network_narrative(snapshot),
        },
    }

