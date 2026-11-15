"""FileGrid - simulator de contoare inteligente.

Trimite continuu citiri de contor catre API-ul FileGrid (punctul unic de intrare)
si expune un mic API de control (port 8080, doar in reteaua Docker interna):

  GET   /health              -> healthcheck pentru Docker
  GET   /status              -> modul curent, rata, contoare de trimiteri
  POST  /mode                -> {"mode": "normal" | "storm"}
  PATCH /config              -> {"base_rate", "storm_multiplier", "duplicate_ratio",
                                 "anomaly_ratio", "paused"} (toate optionale)
  POST  /duplicates/burst    -> {"count": 50} retrimite imediat ultimele N citiri
                                 (demo pentru idempotenta: nu trebuie sa apara dubluri in DB)
  GET   /metrics             -> metrici Prometheus

Toata configurarea vine din variabile de mediu (vezi CONFIG mai jos).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import ssl
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Configurare (din variabile de mediu)
# --------------------------------------------------------------------------- #


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


CONFIG = {
    # Prin nginx (HTTPS), exact ca un contor real. In docker-compose: https://nginx/api/v1/ingest
    "api_url": _env("API_URL", "https://nginx/api/v1/ingest"),
    "api_token": _env("API_TOKEN", ""),
    # "multipart" = conform API_CONTRACT.md (file + category + metadata)
    # "json"      = body JSON direct (daca Mario face un endpoint dedicat pentru citiri)
    "ingest_mode": _env("INGEST_MODE", "multipart"),
    # "false" = accepta certificatul self-signed; "true" = verificare standard;
    # orice alta valoare = cale catre un fisier CA (ex: /certs/ca.crt)
    "verify_tls": _env("VERIFY_TLS", "false"),
    "base_rate": float(_env("BASE_RATE", "10")),  # citiri / secunda in mod normal
    "storm_multiplier": float(_env("STORM_MULTIPLIER", "10")),
    "meter_count": int(_env("METER_COUNT", "1000")),
    "duplicate_ratio": float(_env("DUPLICATE_RATIO", "0.05")),
    "anomaly_ratio": float(_env("ANOMALY_RATIO", "0.02")),
    "max_in_flight": int(_env("MAX_IN_FLIGHT", "200")),
    "max_retries": int(_env("MAX_RETRIES", "5")),
    "start_mode": _env("START_MODE", "normal"),
    # backlog: citiri amanate cand FileGrid e indisponibil (nu se pierd)
    "backlog_max": int(_env("BACKLOG_MAX", "500000")),
    "backlog_rate": float(_env("BACKLOG_RATE", "50")),  # retrimiteri / secunda
    "control_port": int(_env("CONTROL_PORT", "8080")),
    "seed": int(_env("SEED", "42")),
}

# --------------------------------------------------------------------------- #
# Loguri JSON pe stdout (le culege Loki)
# --------------------------------------------------------------------------- #


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": "simulator",
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            data.update(extra)
        return json.dumps(data, ensure_ascii=False)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
log = logging.getLogger("simulator")
log.addHandler(_handler)
log.setLevel(logging.INFO)
log.propagate = False


def log_event(level: int, msg: str, **fields) -> None:
    log.log(level, msg, extra={"extra_fields": fields})


# --------------------------------------------------------------------------- #
# Metrici Prometheus
# --------------------------------------------------------------------------- #

M_REQUESTS = Counter(
    "sim_requests_total",
    "Citiri trimise, dupa rezultat",
    ["result", "mode"],  # result: ok | duplicate_rejected | client_error | failed
)
M_RETRIES = Counter("sim_retries_total", "Reincercari dupa erori de retea / 5xx")
M_DROPPED = Counter("sim_dropped_total", "Citiri pierdute: backlog plin (memoria contorului epuizata)")
M_BACKLOG = Gauge("sim_backlog", "Citiri amanate (FileGrid indisponibil), in asteptare de retrimitere")
M_REDELIVERED = Counter("sim_redelivered_total", "Citiri din backlog livrate ulterior (202 sau 409)")
M_LATENCY = Histogram(
    "sim_request_duration_seconds",
    "Durata unei cereri de ingestie (inclusiv retry-uri)",
    ["mode"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
M_IN_FLIGHT = Gauge("sim_in_flight", "Cereri in curs")
M_TARGET_RATE = Gauge("sim_target_rate", "Rata tinta (citiri/s)")
M_STORM = Gauge("sim_storm_mode", "1 daca simulatorul e in mod furtuna")
M_ANOMALIES = Counter("sim_anomalies_total", "Anomalii injectate", ["kind"])
M_DUPLICATES = Counter("sim_duplicates_sent_total", "Citiri retrimise intentionat (test idempotenta)")

# --------------------------------------------------------------------------- #
# Flota de contoare
# --------------------------------------------------------------------------- #

# Județ -> prefix used in meter ids. Equal weight across all 41 counties.
ZONES = {
    "Alba": "AB", "Arad": "AR", "Argeș": "AG", "Bacău": "BC", "Bihor": "BH",
    "Bistrița-Năsăud": "BN", "Botoșani": "BT", "Brașov": "BV", "Brăila": "BR",
    "Buzău": "BZ", "Caraș-Severin": "CS", "Călărași": "CL", "Cluj": "CJ",
    "Constanța": "CT", "Covasna": "CV", "Dâmbovița": "DB", "Dolj": "DJ",
    "Galați": "GL", "Giurgiu": "GR", "Gorj": "GJ", "Harghita": "HR",
    "Hunedoara": "HD", "Ialomița": "IL", "Iași": "IS", "Ilfov": "IF",
    "Maramureș": "MM", "Mehedinți": "MH", "Mureș": "MS", "Neamț": "NT",
    "Olt": "OT", "Prahova": "PH", "Satu Mare": "SM", "Sălaj": "SJ", "Sibiu": "SB",
    "Suceava": "SV", "Teleorman": "TR", "Timiș": "TM", "Tulcea": "TL",
    "Vaslui": "VS", "Vâlcea": "VL", "Vrancea": "VN",
}
SUBSTATIONS_PER_ZONE = 12

# putere medie (kW) per tip de consumator
METER_TYPES = {
    "residential": (0.4, 1.2),
    "commercial": (3.0, 15.0),
    "prosumer": (0.4, 1.2),  # are si productie fotovoltaica
}
METER_TYPE_WEIGHTS = [0.8, 0.12, 0.08]

NOMINAL_VOLTAGE = 230.0
NOMINAL_FREQ = 50.0

ANOMALY_KINDS = ["voltage_sag", "overvoltage", "load_spike", "tamper", "outage"]
ANOMALY_WEIGHTS = [0.35, 0.15, 0.25, 0.1, 0.15]


@dataclass
class Meter:
    meter_id: str
    zone: str
    substation: str
    meter_type: str
    base_kw: float
    kwh_import: float
    kwh_export: float
    kvarh: float
    last_ts_ms: int


def build_fleet(rng: random.Random, count: int) -> list[Meter]:
    fleet = []
    now_ms = int(time.time() * 1000)
    zone_names = list(ZONES)
    type_names = list(METER_TYPES)
    for i in range(count):
        zone = rng.choice(zone_names)
        prefix = ZONES[zone]
        mtype = rng.choices(type_names, METER_TYPE_WEIGHTS)[0]
        lo, hi = METER_TYPES[mtype]
        fleet.append(
            Meter(
                meter_id=f"RO-{prefix}-{i:06d}",
                zone=zone,
                substation=f"{prefix}-PT-{rng.randint(1, SUBSTATIONS_PER_ZONE):03d}",
                meter_type=mtype,
                base_kw=rng.uniform(lo, hi),
                kwh_import=rng.uniform(1_000, 40_000),
                kwh_export=rng.uniform(0, 5_000) if mtype == "prosumer" else 0.0,
                kvarh=rng.uniform(50, 2_000),
                last_ts_ms=now_ms,
            )
        )
    return fleet


def daily_load_factor(hour: float) -> float:
    """Profil de consum aproximativ: minim noaptea, varf seara."""
    if hour < 6:
        return 0.45
    if hour < 9:
        return 0.9
    if hour < 17:
        return 0.75
    if hour < 22:
        return 1.35
    return 0.7


def solar_factor(hour: float) -> float:
    if 7 <= hour <= 19:
        return max(0.0, 1 - abs(hour - 13) / 6)
    return 0.0


# --------------------------------------------------------------------------- #
# Starea simulatorului
# --------------------------------------------------------------------------- #


class SimState:
    def __init__(self) -> None:
        self.rng = random.Random(CONFIG["seed"])
        self.fleet = build_fleet(self.rng, CONFIG["meter_count"])
        self.mode: str = "storm" if CONFIG["start_mode"] == "storm" else "normal"
        self.base_rate: float = CONFIG["base_rate"]
        self.storm_multiplier: float = CONFIG["storm_multiplier"]
        self.duplicate_ratio: float = CONFIG["duplicate_ratio"]
        self.anomaly_ratio: float = CONFIG["anomaly_ratio"]
        self.paused: bool = False
        self.in_flight: int = 0
        self.recent: deque[dict] = deque(maxlen=1000)  # pentru retrimiteri (duplicate)
        self.counts = {"ok": 0, "duplicate_rejected": 0, "client_error": 0, "failed": 0, "dropped": 0,
                       "deferred": 0, "redelivered": 0}
        # citiri care nu au putut fi trimise: retrimise de backlog_worker (memoria contorului)
        self.backlog: deque[dict] = deque()
        self.retries = 0  # reincercari (retele instabile, 5xx), vizibile in /status
        self.started_at = time.time()
        self._update_gauges()

    def target_rate(self) -> float:
        return self.base_rate * (self.storm_multiplier if self.mode == "storm" else 1.0)

    def _update_gauges(self) -> None:
        M_TARGET_RATE.set(self.target_rate())
        M_STORM.set(1 if self.mode == "storm" else 0)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._update_gauges()

    def new_reading(self) -> dict:
        rng = self.rng
        m = rng.choice(self.fleet)

        # timestamp strict crescator per contor (ms), ca (meter_id, timestamp) sa fie unic
        # pentru citiri diferite; altfel la 100 req/s ar aparea coliziuni false
        now_ms = int(time.time() * 1000)
        ts_ms = max(now_ms, m.last_ts_ms + 1)
        elapsed_h = (ts_ms - m.last_ts_ms) / 3_600_000
        m.last_ts_ms = ts_ms
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        hour = ts.hour + 2 + ts.minute / 60  # aproximativ ora Romaniei
        hour %= 24

        power_kw = m.base_kw * daily_load_factor(hour) * rng.uniform(0.85, 1.15)
        voltage = rng.gauss(NOMINAL_VOLTAGE, 3.0)
        freq = rng.gauss(NOMINAL_FREQ, 0.02)
        pf = rng.uniform(0.9, 0.99) if m.meter_type != "commercial" else rng.uniform(0.8, 0.95)
        export_kw = m.base_kw * 2.5 * solar_factor(hour) * rng.uniform(0.6, 1.0) if m.meter_type == "prosumer" else 0.0
        events: list[str] = []
        relay = "connected"
        anomaly = None

        if rng.random() < self.anomaly_ratio:
            anomaly = rng.choices(ANOMALY_KINDS, ANOMALY_WEIGHTS)[0]
            M_ANOMALIES.labels(kind=anomaly).inc()
            if anomaly == "voltage_sag":
                voltage = rng.uniform(170, 200)
                events.append("VOLTAGE_SAG")
            elif anomaly == "overvoltage":
                voltage = rng.uniform(254, 270)
                events.append("OVERVOLTAGE")
            elif anomaly == "load_spike":
                power_kw *= rng.uniform(4, 8)  # contorul nu "stie" ca e anomalie; o detecteaza analytics
            elif anomaly == "tamper":
                events.append(rng.choice(["TAMPER_COVER_OPEN", "MAGNETIC_FIELD_DETECTED"]))
            elif anomaly == "outage":
                voltage, power_kw, export_kw, freq = 0.0, 0.0, 0.0, 0.0
                events.append("POWER_OUTAGE")

        net_kw = max(power_kw - export_kw, 0.0)
        m.kwh_import += net_kw * elapsed_h
        m.kwh_export += max(export_kw - power_kw, 0.0) * elapsed_h
        m.kvarh += net_kw * elapsed_h * 0.3
        current = (power_kw * 1000 / (voltage * pf)) if voltage > 0 else 0.0

        return {
            "reading_id": str(uuid.uuid4()),
            "meter_id": m.meter_id,
            "timestamp": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "zone": m.zone,
            "substation": m.substation,
            "meter_type": m.meter_type,
            "energy": {
                "active_import_kwh": round(m.kwh_import, 3),
                "active_export_kwh": round(m.kwh_export, 3),
                "reactive_kvarh": round(m.kvarh, 3),
            },
            "instant": {
                "voltage_v": round(voltage, 1),
                "current_a": round(current, 2),
                "active_power_kw": round(power_kw, 3),
                "frequency_hz": round(freq, 3),
                "power_factor": round(pf, 3),
            },
            "events": events,
            "relay_state": relay,
            "firmware": "2.4.1",
            # eticheta de "adevar" pentru verificarea detectiei de anomalii; backend-ul o poate ignora
            "sim_label": anomaly,
        }

    def next_payload(self) -> tuple[dict, bool]:
        if self.recent and self.rng.random() < self.duplicate_ratio:
            M_DUPLICATES.inc()
            return self.rng.choice(self.recent), True
        reading = self.new_reading()
        self.recent.append(reading)
        return reading, False


STATE = SimState()

# --------------------------------------------------------------------------- #
# Trimitere catre FileGrid API
# --------------------------------------------------------------------------- #


def build_verify() -> bool | ssl.SSLContext:
    v = CONFIG["verify_tls"]
    if v.lower() in ("false", "0", "no"):
        return False
    if v.lower() in ("true", "1", "yes"):
        return True
    return ssl.create_default_context(cafile=v)


def build_request(payload: dict) -> dict:
    headers = {
        "Idempotency-Key": f"{payload['meter_id']}:{payload['timestamp']}",
        "X-Source": "meter-simulator",
    }
    if CONFIG["api_token"]:
        headers["Authorization"] = f"Bearer {CONFIG['api_token']}"

    if CONFIG["ingest_mode"] == "json":
        return {"headers": headers, "json": {"category": "meter_readings", **payload}}

    body = json.dumps(payload, separators=(",", ":")).encode()
    filename = f"{payload['meter_id']}_{payload['timestamp'].replace(':', '-')}.json"
    metadata = {
        "meter_id": payload["meter_id"],
        "timestamp": payload["timestamp"],
        "zone": payload["zone"],
        "source": "meter-simulator",
    }
    return {
        "headers": headers,
        "files": {"file": (filename, body, "application/json")},
        "data": {"category": "meter_readings", "metadata": json.dumps(metadata)},
    }


async def send(client: httpx.AsyncClient, payload: dict, is_duplicate: bool) -> None:
    mode = STATE.mode
    STATE.in_flight += 1
    M_IN_FLIGHT.set(STATE.in_flight)
    started = time.perf_counter()
    result = "failed"
    last_error = ""
    try:
        for attempt in range(CONFIG["max_retries"] + 1):
            try:
                resp = await client.post(CONFIG["api_url"], **build_request(payload))
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 300:
                    result = "ok"
                    break
                if resp.status_code == 409:  # API-ul a recunoscut duplicatul -> corect
                    result = "duplicate_rejected"
                    break
                if resp.status_code < 500 and resp.status_code != 429:
                    result = "client_error"
                    log_event(
                        logging.WARNING, "ingest rejected",
                        status=resp.status_code, meter_id=payload["meter_id"],
                        body=resp.text[:300], trace_id=resp.headers.get("X-Trace-Id"),
                    )
                    break
                last_error = f"HTTP {resp.status_code}"

            if attempt < CONFIG["max_retries"]:
                M_RETRIES.inc()
                STATE.retries += 1
                await asyncio.sleep(min(0.2 * 2**attempt, 5.0) * random.uniform(0.5, 1.5))
        else:
            # Ca un contor real: citirea ramane in memoria contorului si e retrimisa cand
            # FileGrid revine (backlog), in loc sa se piarda. Idempotency-Key-ul identic
            # face retrimiterea sigura: 202 sau 409, niciodata o dublura.
            if len(STATE.backlog) < CONFIG["backlog_max"]:
                STATE.backlog.append(payload)
                M_BACKLOG.set(len(STATE.backlog))
                result = "deferred"
            log_event(
                logging.WARNING if result == "deferred" else logging.ERROR,
                "ingest failed after retries" + ("; kept in backlog" if result == "deferred"
                                                 else "; backlog full, reading LOST"),
                meter_id=payload["meter_id"], error=last_error, duplicate=is_duplicate,
            )
    finally:
        STATE.in_flight -= 1
        M_IN_FLIGHT.set(STATE.in_flight)
        M_LATENCY.labels(mode=mode).observe(time.perf_counter() - started)
        M_REQUESTS.labels(result=result, mode=mode).inc()
        STATE.counts[result] += 1


async def post_once(client: httpx.AsyncClient, payload: dict) -> str:
    """One attempt: 'ok' | 'duplicate_rejected' | 'client_error' | 'retry'."""
    try:
        resp = await client.post(CONFIG["api_url"], **build_request(payload))
    except httpx.TransportError:
        return "retry"
    if resp.status_code < 300:
        return "ok"
    if resp.status_code == 409:
        return "duplicate_rejected"
    if resp.status_code < 500 and resp.status_code != 429:
        log_event(logging.WARNING, "backlog reading rejected", status=resp.status_code,
                  meter_id=payload["meter_id"], body=resp.text[:300])
        return "client_error"
    return "retry"


async def backlog_worker(client: httpx.AsyncClient) -> None:
    """Retrimite citirile amanate, in ordine, cu cel mult BACKLOG_RATE pe secunda, pana
    sunt acceptate (202) sau recunoscute ca deja primite (409)."""
    pause = 1.0
    while True:
        if not STATE.backlog:
            await asyncio.sleep(0.5)
            continue
        payload = STATE.backlog[0]
        outcome = await post_once(client, payload)
        if outcome == "retry":  # FileGrid inca indisponibil: asteptam, nu pierdem nimic
            await asyncio.sleep(pause)
            pause = min(pause * 2, 10.0)
            continue
        pause = 1.0
        STATE.backlog.popleft()
        M_BACKLOG.set(len(STATE.backlog))
        if outcome == "client_error":
            STATE.counts["failed"] += 1
        else:
            STATE.counts["redelivered"] += 1
            M_REDELIVERED.inc()
        await asyncio.sleep(1.0 / max(CONFIG["backlog_rate"], 1.0))


TASKS: set[asyncio.Task] = set()


def launch(client: httpx.AsyncClient, payload: dict, is_duplicate: bool) -> bool:
    if STATE.in_flight >= CONFIG["max_in_flight"]:
        # Prea multe cereri in zbor (FileGrid lent/indisponibil): citirea nu se arunca, ci
        # asteapta in memoria contorului (backlog). Se pierde doar daca backlog-ul e plin.
        if len(STATE.backlog) < CONFIG["backlog_max"]:
            STATE.backlog.append(payload)
            M_BACKLOG.set(len(STATE.backlog))
            STATE.counts["deferred"] += 1
        else:
            M_DROPPED.inc()
            STATE.counts["dropped"] += 1
        return False
    task = asyncio.create_task(send(client, payload, is_duplicate))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return True


async def producer(client: httpx.AsyncClient) -> None:
    """Porneste cereri la rata tinta, in tick-uri de 50 ms."""
    loop = asyncio.get_running_loop()
    tick = 0.05
    budget = 0.0
    while True:
        t0 = loop.time()
        if not STATE.paused:
            budget += STATE.target_rate() * tick
            n = int(budget)
            budget -= n
            for _ in range(n):
                payload, dup = STATE.next_payload()
                launch(client, payload, dup)
        else:
            budget = 0.0
        await asyncio.sleep(max(0.0, tick - (loop.time() - t0)))


async def reporter() -> None:
    """Un rezumat la 10 s in loguri, ca sa nu inundam Loki cu o linie per cerere."""
    prev = dict(STATE.counts)
    while True:
        await asyncio.sleep(10)
        now = dict(STATE.counts)
        delta = {k: now[k] - prev[k] for k in now}
        prev = now
        log_event(
            logging.INFO, "simulator stats (last 10s)",
            mode=STATE.mode, target_rate=STATE.target_rate(), paused=STATE.paused,
            in_flight=STATE.in_flight, actual_rate=round(sum(delta.values()) / 10, 1), **delta,
        )


# --------------------------------------------------------------------------- #
# API de control
# --------------------------------------------------------------------------- #

app = FastAPI(title="FileGrid Meter Simulator", docs_url="/docs")
CLIENT: httpx.AsyncClient | None = None


class ModeIn(BaseModel):
    mode: Literal["normal", "storm"]


class ConfigPatch(BaseModel):
    base_rate: float | None = Field(None, ge=0, le=5000)
    storm_multiplier: float | None = Field(None, ge=1, le=100)
    duplicate_ratio: float | None = Field(None, ge=0, le=1)
    anomaly_ratio: float | None = Field(None, ge=0, le=1)
    paused: bool | None = None


class BurstIn(BaseModel):
    count: int = Field(50, ge=1, le=1000)


def status_dict() -> dict:
    return {
        "mode": STATE.mode,
        "paused": STATE.paused,
        "target_rate": STATE.target_rate(),
        "base_rate": STATE.base_rate,
        "storm_multiplier": STATE.storm_multiplier,
        "duplicate_ratio": STATE.duplicate_ratio,
        "anomaly_ratio": STATE.anomaly_ratio,
        "meters": len(STATE.fleet),
        "in_flight": STATE.in_flight,
        "backlog": len(STATE.backlog),
        "retries": STATE.retries,
        "counts": STATE.counts,
        "uptime_s": round(time.time() - STATE.started_at),
        "api_url": CONFIG["api_url"],
        "ingest_mode": CONFIG["ingest_mode"],
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/status")
async def status() -> dict:
    return status_dict()


@app.post("/mode")
async def set_mode(body: ModeIn) -> dict:
    old = STATE.mode
    STATE.set_mode(body.mode)
    log_event(logging.INFO, "mode changed", old=old, new=body.mode, target_rate=STATE.target_rate())
    return status_dict()


@app.patch("/config")
async def patch_config(body: ConfigPatch) -> dict:
    changes = body.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(STATE, key, value)
    STATE._update_gauges()
    log_event(logging.INFO, "config changed", **changes)
    return status_dict()


@app.post("/duplicates/burst")
async def duplicate_burst(body: BurstIn) -> dict:
    """Retrimite ultimele N citiri. Dupa asta, numarul de randuri din DB nu trebuie sa creasca."""
    assert CLIENT is not None
    recent = list(STATE.recent)[-body.count:]
    sent = sum(launch(CLIENT, p, True) for p in recent)
    M_DUPLICATES.inc(sent)
    log_event(logging.INFO, "duplicate burst", requested=body.count, sent=sent)
    return {"requested": body.count, "sent": sent}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --------------------------------------------------------------------------- #
# Pornire
# --------------------------------------------------------------------------- #


async def main() -> None:
    global CLIENT
    CLIENT = httpx.AsyncClient(
        verify=build_verify(),
        timeout=httpx.Timeout(15.0, connect=5.0),
        limits=httpx.Limits(
            max_connections=CONFIG["max_in_flight"],
            max_keepalive_connections=min(50, CONFIG["max_in_flight"]),
        ),
    )
    log_event(
        logging.INFO, "simulator starting",
        api_url=CONFIG["api_url"], mode=STATE.mode, target_rate=STATE.target_rate(),
        meters=len(STATE.fleet), ingest_mode=CONFIG["ingest_mode"],
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=CONFIG["control_port"], log_level="warning", access_log=False)
    )
    background = [asyncio.create_task(producer(CLIENT)), asyncio.create_task(reporter()),
                  asyncio.create_task(backlog_worker(CLIENT))]
    try:
        await server.serve()  # se termina la SIGTERM (docker stop)
    finally:
        for t in background:
            t.cancel()
        if TASKS:
            await asyncio.wait(TASKS, timeout=5)
        await CLIENT.aclose()
        log_event(logging.INFO, "simulator stopped", **STATE.counts)


if __name__ == "__main__":
    asyncio.run(main())
