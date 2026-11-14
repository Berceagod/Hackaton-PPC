"""Slice J load generator: uploads outage records through the gateway as fast as
CONCURRENCY clients can, for DURATION seconds, and prints throughput and latency as JSON.

Runs inside the Docker network (in the simulator image: httpx + the gateway's CA), so the
measurement is not limited by the host. Used by scripts/demo/scale-out.sh.
Env: API_BASE, LOGIN_PASSWORD, CONCURRENCY, DURATION, SIZE_KB, VERIFY_TLS.
"""

import asyncio
import json
import os
import ssl
import time

import httpx

BASE = os.environ.get("API_BASE", "https://nginx/api/v1")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "32"))
DURATION = float(os.environ.get("DURATION", "20"))
SIZE = int(os.environ.get("SIZE_KB", "512")) * 1024
CA = os.environ.get("VERIFY_TLS", "/certs/tls.crt")


async def main() -> None:
    verify = ssl.create_default_context(cafile=CA) if os.path.exists(CA) else False
    async with httpx.AsyncClient(verify=verify, timeout=60,
                                 limits=httpx.Limits(max_connections=CONCURRENCY)) as client:
        r = await client.post(f"{BASE}/auth/login", json={
            "email": "dispecer@ppc.ro", "password": os.environ["LOGIN_PASSWORD"]})
        r.raise_for_status()
        headers = {"Authorization": f"Bearer {r.json()['token']}"}
        body = os.urandom(SIZE)
        latencies: list[float] = []
        errors = 0
        deadline = time.monotonic() + DURATION

        async def worker(n: int) -> None:
            nonlocal errors
            while time.monotonic() < deadline:
                t0 = time.perf_counter()
                try:
                    resp = await client.post(
                        f"{BASE}/ingest", headers=headers,
                        files={"file": (f"scale_out_{n}.bin", body)},
                        data={"category": "outage_records",
                              "metadata": '{"zone":"Muntenia","station":"scale-out"}'})
                    ok = resp.status_code == 202
                except httpx.HTTPError:
                    ok = False
                if ok:
                    latencies.append(time.perf_counter() - t0)
                else:
                    errors += 1

        started = time.monotonic()
        await asyncio.gather(*(worker(i) for i in range(CONCURRENCY)))
        elapsed = time.monotonic() - started

    latencies.sort()

    def pct(q: float) -> float:
        return round(latencies[min(len(latencies) - 1, int(q * len(latencies)))] * 1000, 1) if latencies else 0.0

    print(json.dumps({
        "ok": len(latencies), "errors": errors, "seconds": round(elapsed, 1),
        "req_per_s": round(len(latencies) / elapsed, 1),
        "mb_per_s": round(len(latencies) * SIZE / elapsed / 1048576, 1),
        "p50_ms": pct(0.50), "p95_ms": pct(0.95),
    }))


if __name__ == "__main__":
    asyncio.run(main())
