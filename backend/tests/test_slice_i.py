"""Slice I: analytics summary, benchmark verdicts, scenarios, anomalies vs sim_label.

Prometheus is not part of this branch (slice H). These tests either stub it or
point the client at a fake HTTP response. A dead Prometheus must never turn
GET /analytics/summary into a 500.
"""

import asyncio
import json
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import pytest

import analytics
from analytics import classify_reading, fetch_live_metrics, format_throughput, storm_narrative

SIM = {"Authorization": "Bearer sim-token"}


@pytest.fixture(autouse=True)
def prometheus_down(monkeypatch):
    """Slice H is not merged here, so the summary must not dial a real Prometheus."""

    async def _down(*_args, **_kwargs):
        return None

    monkeypatch.setattr("routes.analytics.fetch_live_metrics", _down)


def _row(category, storage, p95, mbps=1.0, samples=10, errors=0, p50=None, p99=None):
    return {
        "category": category,
        "storage": storage,
        "samples": samples,
        "ok": samples - errors,
        "errors": errors,
        "latency_avg_ms": p95,
        "latency_p50_ms": p50 if p50 is not None else p95 / 2,
        "latency_p95_ms": p95,
        "latency_p99_ms": p99 if p99 is not None else p95,
        "throughput_mbps": mbps,
        "bytes_total": 1000,
    }


def _post(client, headers, kind, rows, scenario=None):
    body = {"kind": kind, "rows": rows}
    if scenario is not None:
        body["scenario"] = scenario
    return client.post("/api/v1/analytics/measurements", headers=headers, json=body)


def _summary(client, headers):
    response = client.get("/api/v1/analytics/summary", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _matrix(body, category):
    return next(row for row in body["storage_matrix"] if row["category"] == category)


def test_summary_requires_a_user_and_refuses_the_simulator_token(client, auth):
    assert client.get("/api/v1/analytics/summary").status_code == 401
    assert client.get("/api/v1/analytics/summary", headers=SIM).status_code == 403
    assert client.post("/api/v1/analytics/measurements", headers=SIM, json={}).status_code == 403
    assert client.get("/api/v1/analytics/summary", headers=auth).status_code == 200


def test_empty_summary_is_200_and_names_the_benchmark(client, auth):
    body = _summary(client, auth)
    assert [row["category"] for row in body["storage_matrix"]] == [
        "meter_readings", "outage_records", "connection_docs", "drone_videos",
    ]
    assert [row["storage"] for row in body["storage_matrix"]] == ["postgres", "s3", "ftp", "s3"]
    assert all(row["latency_p95_ms"] == 0 for row in body["storage_matrix"])
    assert [point["label"] for point in body["load_comparison"]] == ["Zi normală", "Vârf furtună (10x)"]
    assert [zone["zone"] for zone in body["zone_insights"]] == ["Muntenia", "Banat", "Dobrogea"]
    assert "Nu există încă măsurători de latență." in body["narrative"]["storage_verdict"]
    assert "scripts/benchmark/run.sh" in body["narrative"]["storage_verdict"]
    assert "scripts/scenarios/normal-day.sh" in body["narrative"]["storm_isolation"]
    assert "sim_label" in body["narrative"]["network"]
    assert "Audit: 0 evenimente" in _matrix(body, "connection_docs")["verdict"]


def test_matrix_follows_the_current_policy(client, admin, auth):
    saved = client.put(
        "/api/v1/admin/policies",
        headers=admin,
        json={"meter_readings": {
            "storage": "ftp", "encryption": False, "deduplicate": True, "priority": "normal",
        }},
    )
    assert saved.status_code == 200
    assert _matrix(_summary(client, auth), "meter_readings")["storage"] == "ftp"


def test_benchmark_names_the_best_storage_from_the_numbers(client, auth):
    rows = [
        _row("meter_readings", "postgres", p95=10, p50=6, p99=14),
        _row("meter_readings", "s3", p95=40),
        _row("meter_readings", "ftp", p95=80),
        _row("outage_records", "postgres", p95=15),
        _row("outage_records", "s3", p95=90),
        _row("outage_records", "ftp", p95=40),
        _row("connection_docs", "ftp", p95=30),
        _row("drone_videos", "postgres", p95=100, mbps=10),
        _row("drone_videos", "s3", p95=200, mbps=80),
        _row("drone_videos", "ftp", p95=150, mbps=20),
    ]
    assert _post(client, auth, "benchmark", rows).status_code == 204
    body = _summary(client, auth)
    meters = _matrix(body, "meter_readings")
    outages = _matrix(body, "outage_records")
    docs = _matrix(body, "connection_docs")
    drones = _matrix(body, "drone_videos")
    assert meters["latency_p95_ms"] == 10
    assert meters["latency_avg_ms"] == 10
    assert "postgres este cea mai bună alegere" in meters["verdict"]
    assert "p50 6 ms" in meters["verdict"]
    assert "p99 14 ms" in meters["verdict"]
    # Policy keeps outages on s3, but the benchmark measured postgres faster.
    assert "benchmark-ul indică postgres" in outages["verdict"]
    assert "Măsurat doar pe ftp" in docs["verdict"]
    assert "s3 este cea mai bună alegere" in drones["verdict"]
    assert "throughput" in drones["verdict"]
    assert "ultimul benchmark" in body["narrative"]["storage_verdict"]


def test_a_later_benchmark_replaces_the_earlier_one(client, auth):
    assert _post(client, auth, "benchmark", [_row("meter_readings", "postgres", p95=10),
                                             _row("meter_readings", "s3", p95=50)]).status_code == 204
    assert _post(client, auth, "benchmark", [_row("meter_readings", "postgres", p95=70),
                                             _row("meter_readings", "s3", p95=12)]).status_code == 204
    verdict = _matrix(_summary(client, auth), "meter_readings")["verdict"]
    assert "benchmark-ul indică s3" in verdict
    assert _matrix(_summary(client, auth), "meter_readings")["latency_p95_ms"] == 70


def test_prometheus_fills_the_table_and_a_dead_one_does_not_fail_the_request(client, auth, monkeypatch):
    assert _post(client, auth, "benchmark", [
        _row("meter_readings", "postgres", p95=9),
        _row("meter_readings", "s3", p95=40),
    ]).status_code == 204

    async def live(*_args, **_kwargs):
        return {("meter_readings", "postgres"): {
            "avg_ms": 8, "p95_ms": 11, "mbps": 1.5, "error_rate": 0.01,
        }}

    monkeypatch.setattr("routes.analytics.fetch_live_metrics", live)
    body = _summary(client, auth)
    assert _matrix(body, "meter_readings")["latency_p95_ms"] == 11
    assert _matrix(body, "meter_readings")["error_rate"] == 0.01
    assert "Prometheus" in body["narrative"]["storage_verdict"]
    assert "postgres este cea mai bună alegere" in _matrix(body, "meter_readings")["verdict"]

    async def exploded(*_args, **_kwargs):
        raise RuntimeError("dns")

    monkeypatch.setattr("routes.analytics.fetch_live_metrics", exploded)
    fallen_back = _summary(client, auth)
    assert _matrix(fallen_back, "meter_readings")["latency_p95_ms"] == 9


def test_normal_day_and_storm_become_the_two_bars(client, auth):
    def scenario(name, meter, docs, drone):
        rows = [
            _row("meter_readings", "postgres", p95=meter),
            _row("outage_records", "s3", p95=40),
            _row("connection_docs", "ftp", p95=docs),
            _row("drone_videos", "s3", p95=drone),
        ]
        assert _post(client, auth, "scenario", rows, scenario=name).status_code == 204

    scenario("normal", 10, 100, 200)
    scenario("peak", 40, 110, 210)
    body = _summary(client, auth)
    normal, peak = body["load_comparison"]
    assert normal["label"] == "Zi normală" and normal["meter_readings_ms"] == 10
    assert normal["connection_docs_ms"] == 100 and normal["drone_videos_ms"] == 200
    assert peak["label"] == "Vârf furtună (10x)" and peak["meter_readings_ms"] == 40
    text = body["narrative"]["storm_isolation"]
    assert "Citiri contoare: p95 de la 10 ms la 40 ms" in text
    assert "nu trage celelalte categorii după el" in text


def test_a_meter_reading_throughput_is_shown_in_kilobytes(client, auth):
    assert format_throughput(0.008) == "8.0 KB/s"
    assert format_throughput(0.78) == "0.8 MB/s"
    rows = [
        _row("meter_readings", "postgres", p95=10, mbps=0.008),
        _row("meter_readings", "s3", p95=40, mbps=0.02),
    ]
    assert _post(client, auth, "benchmark", rows).status_code == 204
    body = _summary(client, auth)
    assert body["storage_matrix"][0]["throughput_mbps"] == pytest.approx(0.008)
    assert "8.0 KB/s" in _matrix(body, "meter_readings")["verdict"]
    assert "0.0 MB/s" not in _matrix(body, "meter_readings")["verdict"]


def test_storm_text_says_when_isolation_is_not_visible():
    flat = {"meter_readings": 10, "outage_records": 10, "connection_docs": 10, "drone_videos": 10}
    text = storm_narrative(flat, {key: value * 2 for key, value in flat.items()})
    assert "izolarea nu se vede" in text


def test_invalid_measurements_are_422_and_store_nothing(client, auth):
    bad = [
        {},
        {"kind": "benchmark", "rows": []},
        {"kind": "benchmark", "scenario": "normal", "rows": [_row("meter_readings", "postgres", 1)]},
        {"kind": "scenario", "rows": [_row("meter_readings", "postgres", 1)]},
        {"kind": "benchmark", "rows": [_row("tape", "postgres", 1)]},
        {"kind": "benchmark", "rows": [
            _row("meter_readings", "postgres", 1),
            _row("meter_readings", "postgres", 2),
        ]},
        {"kind": "benchmark", "rows": [{**_row("meter_readings", "postgres", 1), "samples": 4, "ok": 1, "errors": 1}]},
        {"kind": "nope", "rows": [_row("meter_readings", "postgres", 1)]},
    ]
    for body in bad:
        response = client.post("/api/v1/analytics/measurements", headers=auth, json=body)
        assert response.status_code == 422, body
    assert client.post(
        "/api/v1/analytics/measurements", headers=auth, content=b"not-json"
    ).status_code == 422
    assert "Nu există încă măsurători" in _summary(client, auth)["narrative"]["storage_verdict"]


def _reading(meter, ts, zone, voltage=230.0, power=1.0, label=None, events=None):
    return {
        "meter_id": meter,
        "timestamp": ts,
        "zone": zone,
        "instant": {"voltage_v": voltage, "active_power_kw": power},
        "events": events or [],
        "sim_label": label,
    }


def _send(client, headers, payload):
    raw = json.dumps(payload).encode()
    response = client.post(
        "/api/v1/ingest",
        headers=headers,
        files={"file": ("r.json", raw, "application/json")},
        data={"category": "meter_readings", "metadata": "{}"},
    )
    assert response.status_code == 202, response.text
    return response


def _upload(client, headers, category, zone, content=b"synthetic"):
    response = client.post(
        "/api/v1/ingest",
        headers=headers,
        files={"file": (f"{category}.bin", content, "application/octet-stream")},
        data={"category": category, "metadata": json.dumps({"zone": zone, "station": "ST-demo"})},
    )
    assert response.status_code == 202, response.text


def test_zone_insights_detect_anomalies_and_compare_them_to_sim_label(client, auth, repo):
    # Muntenia: five normal readings and one sag the simulator also labelled.
    for n in range(5):
        _send(client, auth, _reading("RO-AN-A", f"2026-11-14T10:00:0{n}.000Z", "Muntenia"))
    _send(client, auth, _reading(
        "RO-AN-A", "2026-11-14T10:00:05.000Z", "Muntenia", voltage=188.4, label="voltage_sag",
    ))
    # Banat: the simulator says sag, the voltage does not. That is a miss, not an anomaly we count.
    _send(client, auth, _reading(
        "RO-AN-B", "2026-11-14T11:00:00.000Z", "Banat", voltage=230.0, label="voltage_sag",
    ))
    # Dobrogea: a real outage, and a load spike against that meter's own median.
    _send(client, auth, _reading(
        "RO-AN-C", "2026-11-14T12:00:00.000Z", "Dobrogea", voltage=0.0, power=0.0, label="outage",
    ))
    for n in range(5):
        _send(client, auth, _reading("RO-AN-D", f"2026-11-14T13:00:0{n}.000Z", "Dobrogea", power=1.0))
    _send(client, auth, _reading(
        "RO-AN-D", "2026-11-14T13:00:05.000Z", "Dobrogea", power=8.0, label="load_spike",
    ))
    # A claim that was never stored must not show up as an anomaly.
    repo.readings[("RO-PEND", datetime(2026, 11, 14, tzinfo=timezone.utc))] = {
        "meter_id": "RO-PEND",
        "status": "pending",
        "zone": "Muntenia",
        "voltage_v": 0.0,
        "sim_label": "outage",
        "payload": "{}",
    }

    _upload(client, auth, "outage_records", "Muntenia")
    _upload(client, auth, "outage_records", "Muntenia", b"synthetic-2")
    _upload(client, auth, "outage_records", "Dobrogea")
    _upload(client, auth, "drone_videos", "Muntenia", b"drone-frame")
    _upload(client, auth, "drone_videos", "Banat", b"drone-frame-2")
    _upload(client, auth, "connection_docs", "Banat", b'{"titular":"Persoana Sintetica","cnp":"0000000000000"}')

    body = _summary(client, auth)
    zones = {zone["zone"]: zone for zone in body["zone_insights"]}
    assert zones["Muntenia"]["outages_reported"] == 2
    assert zones["Muntenia"]["meter_anomalies"] == 1
    assert zones["Muntenia"]["drone_inspections"] == 1
    assert zones["Muntenia"]["drone_status"] == "in_progress"
    assert zones["Banat"]["outages_reported"] == 0
    assert zones["Banat"]["meter_anomalies"] == 0
    assert zones["Banat"]["drone_status"] == "completed"
    assert zones["Dobrogea"]["outages_reported"] == 1
    assert zones["Dobrogea"]["meter_anomalies"] == 2
    assert zones["Dobrogea"]["drone_inspections"] == 0
    assert zones["Dobrogea"]["drone_status"] == "pending"
    assert "Audit: 1 eveniment pe documentele de racordare." in _matrix(body, "connection_docs")["verdict"]
    network = body["narrative"]["network"]
    assert "Detectorul a marcat 3 citiri anormale." in network
    assert "Din 4 citiri etichetate de simulator, 3 coincid cu sim_label (75%)." in network


@pytest.mark.parametrize(
    ("voltage", "power", "events", "baseline", "expected"),
    [
        (188.0, 1.0, [], 1.0, "voltage_sag"),
        (260.0, 1.0, [], 1.0, "overvoltage"),
        (0.0, 0.0, [], None, "outage"),
        (49.9, 1.0, [], 1.0, "outage"),
        (230.0, 1.0, ["TAMPER_COVER_OPEN"], 1.0, "tamper"),
        (230.0, 3.5, [], 1.0, None),
        (230.0, 3.51, [], 1.0, "load_spike"),
        (230.0, 1.0, [], 1.0, None),
        (None, None, [], None, None),
        (180.0, 9.0, ["MAGNETIC_FIELD_DETECTED"], 1.0, "voltage_sag"),
    ],
)
def test_detector_rules(voltage, power, events, baseline, expected):
    assert classify_reading(voltage, power, events, baseline) == expected


def test_promql_uses_only_a_safe_window_and_parses_vectors(monkeypatch):
    queries = analytics._promql("5m")
    assert 'outcome=~"server_error|integrity_error"' in queries["errors"]
    assert "histogram_quantile(0.95" in queries["p95"]
    assert queries["p95"].count("(") == queries["p95"].count(")")

    seen = []

    def fake_urlopen(url, timeout=0):
        url = urllib.parse.unquote_plus(url)
        seen.append(url)
        if "histogram_quantile(0.95" in url:
            value = "0.011"
        elif "histogram_quantile(0.50" in url:
            value = "0.008"
        elif "histogram_quantile(0.99" in url:
            value = "0.020"
        elif "_sum" in url:
            value = "0.008"
        elif "filegrid_bytes_total" in url:
            value = "1500000"
        else:
            value = "0.25"
        body = json.dumps({
            "status": "success",
            "data": {"resultType": "vector", "result": [{
                "metric": {"category": "meter_readings", "storage": "postgres"},
                "value": [1700000000, value],
            }, {
                "metric": {"category": "meter_readings", "storage": "none"},
                "value": [1700000000, "NaN"],
            }]},
        }).encode()

        class _Resp:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        return _Resp()

    monkeypatch.setattr(analytics.urllib.request, "urlopen", fake_urlopen)
    live = asyncio.run(fetch_live_metrics("http://prometheus:9090", 1.0, "5m"))
    assert live[("meter_readings", "postgres")]["p95_ms"] == pytest.approx(11.0)
    assert live[("meter_readings", "postgres")]["p50_ms"] == pytest.approx(8.0)
    assert live[("meter_readings", "postgres")]["mbps"] == pytest.approx(1.5)
    assert live[("meter_readings", "postgres")]["error_rate"] == pytest.approx(0.25)
    assert ("meter_readings", "none") not in live
    assert all("[5m]" in url for url in seen)

    seen.clear()
    asyncio.run(fetch_live_metrics("http://prometheus:9090", 1.0, '5m} or vector(1)'))
    assert seen and all("[5m]" in url for url in seen)
    assert not any("vector(1)" in url for url in seen)

    def down(*_args, **_kwargs):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(analytics.urllib.request, "urlopen", down)
    assert asyncio.run(fetch_live_metrics("http://prometheus:9090", 0.2, "5m")) is None
    assert asyncio.run(fetch_live_metrics("ftp://prometheus", 0.2, "5m")) is None
