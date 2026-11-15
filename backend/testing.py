"""Testing overlay (admin only, off unless TESTING_ENABLED=true).

Wraps the demo simulations (the shell scripts need Docker, so they cannot run from the app) as
one-press backend jobs, plus an optional generator of synthetic files for the categories the
meter simulator does not cover. Everything here is for demonstrations:

- the generator is OFF by default and every run expires by itself, so the feed is not drowned
  in random files when you want to test with a real one; its files are named `gen_*` and carry
  the description "sintetic (generator de test)" so they are easy to tell from real ones;
- a job runs one at a time, is bounded in size and duration, and reports its own result;
- the meter simulator is only called through its public control API (nothing in it changes).
"""

import asyncio
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
import uuid

import analytics
from auth import issue_token

log = logging.getLogger("filegrid.testing")

GENERATOR_CATEGORIES = ("outage_records", "connection_docs", "drone_videos")
MAX_RATE = 5.0           # synthetic files per second, per category
MAX_DURATION = 1800.0    # a generator run ends by itself after at most 30 minutes
WORKER_ID = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
REGIONS = analytics.ZONES
_tasks: set = set()      # strong references to running jobs

JOBS = {
    "storm-meters": "Furtună de citiri (10x)",
    "outage-flood": "Inundație de avarii",
    "compare-storages": "Comparație storage-uri",
    "duplicates": "Citiri duplicate",
    "day-vs-storm": "Zi normală vs. furtună (Secțiunea B)",
    "overload-flood": "Flux prea mare (failover automat)",
}


# ------------------------------------------------------------------ quiet mode
async def is_quiet(deps) -> bool:
    """True while the 'Liniște' switch is on: the canary and the scrubber pause, so a demo shows
    only the traffic the presenter creates. It expires by itself (see routes/testing.py)."""
    try:
        return await deps.repo.get_testing("quiet") is not None
    except Exception:
        return False  # never let this flag stop the safety checks from running


# ------------------------------------------------------------------ simulator relay
def _sim_call(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


async def simulator(deps, method: str, path: str, body: dict | None = None):
    return await asyncio.to_thread(_sim_call, deps.settings.simulator_url, method, path, body)


async def simulator_status(deps) -> dict | None:
    try:
        s = await simulator(deps, "GET", "/status")
    except Exception:
        return None
    return {k: s.get(k) for k in ("mode", "target_rate", "paused", "meters", "backlog", "counts")}


# ------------------------------------------------------------------ synthetic content
def synthetic_file(category: str, n: int) -> tuple[str, bytes, dict]:
    region = random.choice(REGIONS)
    meta = {"zone": region, "description": "sintetic (generator de test)"}
    if category == "outage_records":
        doc = {"tip": "avarie", "sintetic": True, "zona": region, "nr": n,
               "descriere": "Eveniment generat pentru test " + "x" * random.randint(200, 1500)}
        return f"gen_avarie_{n}.json", json.dumps(doc).encode(), meta
    if category == "connection_docs":
        text = f"DOCUMENT SINTETIC {n}\nSolicitant: Test Test\nCI: 0000000000000 (fictiv)\n" + "linie\n" * 300
        return f"gen_cerere_{n}.txt", text.encode(), meta
    return f"gen_drona_{n}.bin", os.urandom(200_000), meta  # drone_videos: a small fake clip


def _post_file(base: str, token: str, category: str, name: str, data: bytes, meta: dict,
               extra_headers: dict | None = None, ctype: str = "application/octet-stream",
               want_body: bool = False):
    """Multipart POST to our own /ingest (stdlib only). Returns the HTTP status."""
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in (("category", category), ("metadata", json.dumps(meta))):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                  f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        base.rstrip("/") + "/api/v1/ingest", data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {token}", **(extra_headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if want_body:
                try:
                    return resp.status, json.loads(resp.read().decode() or "{}")
                except ValueError:
                    return resp.status, {}
            return resp.status
    except urllib.error.HTTPError as exc:
        return (exc.code, {}) if want_body else exc.code
    except Exception:
        return (0, {}) if want_body else 0


def _token(deps) -> str:
    return issue_token(deps.settings, {"email": "generator@ppc.ro", "role": "admin", "name": "Generator"})


async def send_one(deps, category: str, n: int) -> int:
    name, data, meta = synthetic_file(category, n)
    return await asyncio.to_thread(_post_file, deps.settings.testing_self_url, _token(deps),
                                   category, name, data, meta)


# ------------------------------------------------------------------ the generator
async def generator_tick(deps, carry: dict) -> int:
    """One second of generation if this worker holds the lease. Returns files sent."""
    state = await deps.repo.get_testing("generator")
    if not state or not state.get("on"):
        carry.clear()
        return 0
    if not await deps.repo.try_lease("generator", WORKER_ID, 2.0):
        return 0
    jobs = []
    for category in state.get("categories", []):
        if category not in GENERATOR_CATEGORIES:
            continue
        carry[category] = carry.get(category, 0.0) + min(float(state.get("rate", 1.0)), MAX_RATE)
        while carry[category] >= 1.0:
            carry[category] -= 1.0
            jobs.append(send_one(deps, category, int(time.time() * 1000) % 10_000_000))
    await asyncio.gather(*jobs)
    return len(jobs)


async def run_generator(deps, stop: asyncio.Event) -> None:
    if not deps.settings.testing_enabled:
        return
    carry: dict = {}
    while not stop.is_set():
        try:
            await generator_tick(deps, carry)
        except Exception:
            log.exception("generator tick failed")
        try:
            await asyncio.wait_for(stop.wait(), 1.0)
        except (asyncio.TimeoutError, TimeoutError):
            pass


# ------------------------------------------------------------------ one-press jobs
def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(p * len(ordered)))] * 1000, 1)


async def _storm(deps, seconds: float) -> dict:
    before = await simulator_status(deps)
    await simulator(deps, "POST", "/mode", {"mode": "storm"})
    try:
        await asyncio.sleep(seconds)
    finally:
        await simulator(deps, "POST", "/mode", {"mode": "normal"})
    after = await simulator_status(deps)
    gained = ((after or {}).get("counts", {}).get("ok", 0) - (before or {}).get("counts", {}).get("ok", 0))
    return {"summary": f"Furtună de {seconds:g} s: {gained} citiri acceptate (ținta crește de 10x), apoi revenire la normal."}


async def _outage_flood(deps, count: int = 60, concurrency: int = 20) -> dict:
    sem = asyncio.Semaphore(concurrency)
    outage, probe = [], []

    async def one(category, bucket, n):
        async with sem:
            t0 = time.perf_counter()
            status = await send_one(deps, category, n)
            bucket.append((status, time.perf_counter() - t0))

    base = int(time.time())
    await asyncio.gather(
        *[one("outage_records", outage, base + i) for i in range(count)],
        *[one("connection_docs", probe, base + i) for i in range(6)])  # another category, same moment
    ok = [t for s, t in outage if s == 202]
    pok = [t for s, t in probe if s == 202]
    return {
        "summary": f"{count} avarii în paralel ({concurrency} odată): {len(ok)} acceptate, "
                   f"{sum(1 for s, _ in outage if s == 503)} refuzate (503), "
                   f"p50 {_pct(ok, .5)} ms, p95 {_pct(ok, .95)} ms. "
                   f"În același timp documentele de racordare: {len(pok)}/6 acceptate, p95 {_pct(pok, .95)} ms "
                   "(categoriile nu se influențează).",
        "outage_p95_ms": _pct(ok, .95), "other_p95_ms": _pct(pok, .95)}


async def _compare_storages(deps, seconds: float = 25.0) -> dict:
    """Hit all three storages at once with 64 KB write+read cycles, so the live latency chart
    in Analytics has real traffic for each. Client-side timings are returned as well."""
    payload = os.urandom(64 * 1024)
    deadline = time.monotonic() + seconds
    stats: dict[str, dict] = {}

    async def chunks():
        yield payload

    async def hammer(name: str):
        puts, gets = [], []
        storage = deps.storages[name]
        while time.monotonic() < deadline:
            key = f"_bench/{uuid.uuid4()}"
            try:
                t0 = time.perf_counter()
                await storage.put_stream(key, chunks())
                puts.append(time.perf_counter() - t0)
                t0 = time.perf_counter()
                async for _ in storage.get_stream(key):
                    pass
                gets.append(time.perf_counter() - t0)
            except Exception:
                break
            finally:
                try:
                    await storage.delete(key)
                except Exception:
                    pass
        stats[name] = {"cycles": len(puts), "put_p50_ms": _pct(puts, .5), "put_p95_ms": _pct(puts, .95),
                       "get_p50_ms": _pct(gets, .5), "get_p95_ms": _pct(gets, .95)}

    await asyncio.gather(*[hammer(n) for n in deps.storages])
    parts = [f"{n}: scriere p50 {s['put_p50_ms']} ms / p95 {s['put_p95_ms']} ms, citire p50 {s['get_p50_ms']} ms"
             for n, s in stats.items()]
    return {"summary": "; ".join(parts), "storages": stats}


async def _send_reading(deps, i: int, want_body: bool = False):
    """A synthetic meter reading through the real ingest path, with the service token."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{i % 1000:03d}Z"
    meter = f"MTR-TST-{uuid.uuid4().hex[:8]}"
    doc = {"meter_id": meter, "timestamp": ts, "zone": random.choice(REGIONS),
           "instant": {"voltage_v": 229.7}, "events": [], "sim_label": None}
    return await asyncio.to_thread(
        _post_file, deps.settings.testing_self_url, deps.settings.simulator_token, "meter_readings",
        "reading.json", json.dumps(doc).encode(), {}, {"Idempotency-Key": f"{meter}:{ts}"},
        "application/json", want_body)


async def _overload_flood(deps, count: int = 240) -> dict:
    """The data flux is too big: `count` readings at the very same moment. Each backend worker has
    its own concurrency limit, so this must exceed FAILOVER_LOAD_THRESHOLD on every worker. With a
    fallback storage set for meter_readings (Politici), the surplus is redirected to it."""
    policy = (await deps.policies.all())["meter_readings"]
    # the default thread pool has ~32 threads, which would spread the burst out: use one thread
    # per request so they really arrive at the same moment
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    requests = []
    for i in range(count):
        meter = f"MTR-TST-{uuid.uuid4().hex[:8]}"
        stamp = f"{ts}.{i % 1000:03d}Z"
        doc = {"meter_id": meter, "timestamp": stamp, "zone": random.choice(REGIONS),
               "instant": {"voltage_v": 229.7}, "events": [], "sim_label": None}
        requests.append((json.dumps(doc).encode(), {"Idempotency-Key": f"{meter}:{stamp}"}))
    with ThreadPoolExecutor(max_workers=count) as pool:
        results = await asyncio.gather(*[
            loop.run_in_executor(pool, _post_file, deps.settings.testing_self_url,
                                 deps.settings.simulator_token, "meter_readings", "reading.json",
                                 raw, {}, headers, "application/json", True)
            for raw, headers in requests])
    where: dict[str, int] = {}
    refused = 0
    for status, body in results:
        if status == 202:
            where[body.get("storage", "?")] = where.get(body.get("storage", "?"), 0) + 1
        else:
            refused += 1
    parts = ", ".join(f"{n} pe {s}" for s, n in sorted(where.items()))
    note = ("" if policy.get("fallback_storage") else
            " Nu este setat un storage de rezervă pentru citiri: setați-l în Politici ca să vedeți redirecționarea.")
    return {"summary": f"{count} citiri deodată: {parts or 'niciuna acceptată'}, {refused} refuzate. "
                       f"Principal: {policy['storage']}, rezervă: {policy.get('fallback_storage') or 'fără'}.{note}"}


async def _scenario(deps, meters: int, concurrency: int) -> dict:
    """One phase: `meters` readings plus the quiet volume of the other categories, all at once,
    timed client-side like the scenario scripts do."""
    plan = [("meter_readings", meters), ("outage_records", 3), ("connection_docs", 2), ("drone_videos", 1)]
    sem = asyncio.Semaphore(concurrency)
    out: dict = {cat: {"t": [], "ok": 0, "err": 0, "bytes": 0} for cat, _ in plan}

    async def one(cat: str, n: int):
        async with sem:
            t0 = time.perf_counter()
            if cat == "meter_readings":
                status, size = await _send_reading(deps, n), 400
            else:
                name, data, meta = synthetic_file(cat, int(time.time() * 1000) % 10_000_000 + n)
                status = await asyncio.to_thread(_post_file, deps.settings.testing_self_url, _token(deps),
                                                 cat, name, data, meta)
                size = len(data)
            dt = time.perf_counter() - t0
        stat = out[cat]
        stat["t"].append(dt)
        stat["ok" if status == 202 else "err"] += 1
        stat["bytes"] += size if status == 202 else 0

    started = time.perf_counter()
    await asyncio.gather(*[one(cat, n) for cat, count in plan for n in range(count)])
    out["_wall"] = time.perf_counter() - started
    return out


async def _day_vs_storm(deps) -> dict:
    """Records the two bars of Analytics Section B: a quiet day, then 10x meter volume while the
    other categories stay quiet (the method of scripts/scenarios/normal-day.sh and peak-day.sh)."""
    policies = await deps.policies.all()
    summary = []
    for scenario, meters, concurrency in (("normal", 12, 4), ("peak", 120, 40)):
        result = await _scenario(deps, meters, concurrency)
        wall = max(result.pop("_wall"), 0.001)
        rows = []
        for cat, st in result.items():
            ts = st["t"]
            if not ts:
                continue
            rows.append({
                "category": cat, "storage": policies[cat]["storage"], "samples": len(ts),
                "ok": st["ok"], "errors": st["err"],
                "latency_avg_ms": round(sum(ts) / len(ts) * 1000, 1),
                "latency_p50_ms": _pct(ts, .5), "latency_p95_ms": _pct(ts, .95), "latency_p99_ms": _pct(ts, .99),
                "throughput_mbps": round(st["bytes"] / wall / 1_000_000, 3), "bytes_total": st["bytes"]})
        await deps.repo.save_measurements("scenario", scenario, rows)
        label = "Zi normală" if scenario == "normal" else "Vârf (10x contoare)"
        summary.append(f"{label}: " + ", ".join(f"{r['category'].split('_')[0]} p95 {r['latency_p95_ms']} ms" for r in rows))
        await asyncio.sleep(2)
    return {"summary": " | ".join(summary) + ". Vezi Analytics → Secțiunea B (reîncărcați pagina)."}


async def _duplicates(deps, count: int = 100) -> dict:
    r = await simulator(deps, "POST", "/duplicates/burst", {"count": count})
    return {"summary": f"Retrimise {r.get('sent')} citiri deja stocate: toate trebuie respinse (409), niciuna stocată de două ori."}


RUNNERS = {
    "storm-meters": lambda d: _storm(d, 30.0),
    "outage-flood": _outage_flood,
    "compare-storages": _compare_storages,
    "duplicates": _duplicates,
    "day-vs-storm": _day_vs_storm,
    "overload-flood": _overload_flood,
}


async def start_job(deps, name: str, by: str) -> bool:
    """Start a job in the background. False when another job is still running."""
    current = await deps.repo.get_testing("job")
    if current and current.get("running"):
        return False
    started = time.time()
    await deps.repo.set_testing("job", {"name": name, "running": True, "started": started, "by": by}, 120)

    async def runner():
        result: dict
        try:
            result = await RUNNERS[name](deps)
        except Exception as exc:
            log.exception("testing job failed", extra={"job": name})
            result = {"error": str(exc) or exc.__class__.__name__}
        await deps.repo.set_testing(
            "job", {"name": name, "running": False, "started": started, "finished": time.time(),
                    "by": by, **result}, 900)

    task = asyncio.create_task(runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


# ------------------------------------------------------------------ live latency per storage
async def live_latency(deps) -> dict:
    """p50/p95 of writes and reads per storage over the last 30 s, from Prometheus."""
    base, timeout = deps.settings.prometheus_url, deps.settings.analytics_timeout_seconds
    window = "30s"
    metric = "filegrid_storage_operation_duration_seconds"

    def q(p: float, op: str) -> str:
        return (f'histogram_quantile({p}, sum by (le, storage) '
                f'(rate({metric}_bucket{{op="{op}",outcome="ok"}}[{window}])))')

    queries = {"put_p50": q(0.5, "put"), "put_p95": q(0.95, "put"),
               "get_p50": q(0.5, "get"), "get_p95": q(0.95, "get"),
               "ops": f'sum by (storage) (rate({metric}_count[{window}]))'}
    rows = await asyncio.gather(*[asyncio.to_thread(analytics._query, base, timeout, v) for v in queries.values()])
    if all(r is None for r in rows):
        return {"available": False, "storages": {}, "at": time.time()}
    storages: dict[str, dict] = {}
    for key, result in zip(queries, rows):
        for item in result or []:
            name = (item.get("metric") or {}).get("storage")
            value = analytics._finite((item.get("value") or [None, None])[1])
            if name in ("postgres", "s3", "ftp") and value is not None:
                field = key + "_ms" if key != "ops" else "ops_per_s"
                storages.setdefault(name, {})[field] = round(value * (1000 if key != "ops" else 1), 2)
    return {"available": True, "storages": storages, "at": time.time()}
