"""Chaos console helpers (improvements.md 2.1): network degradation and the reaper.

Storage faults are injected by storage/chaos.py. Network degradation uses toxiproxy, the proxy
that already sits between nginx and the backend (the same toxics as scripts/chaos/network-bad.sh).
A toxic does not expire by itself, so every network fault is recorded in `chaos_faults` with an
expiry and the reaper undoes it, whichever replica notices first.

Everything here exists only when CHAOS_ENABLED=true (default off, never in a default install).
"""

import asyncio
import json
import logging
import urllib.request

log = logging.getLogger("filegrid.chaos")

CHAOS_AUDIT_ID = "00000000-0000-0000-0000-00000000c4a0"  # audit trail of the console itself
PROXY = "backend"
TOXICS = {  # name -> (type, stream, toxicity, attributes): same as scripts/chaos/network-bad.sh
    "chaos_latency_down": ("latency", "downstream", 1.0, {"latency": 400, "jitter": 200}),
    "chaos_latency_up": ("latency", "upstream", 1.0, {"latency": 400, "jitter": 200}),
    "chaos_bandwidth_up": ("bandwidth", "upstream", 1.0, {"rate": 2048}),
    "chaos_cut": ("reset_peer", "downstream", 0.2, {"timeout": 500}),
}


class Toxiproxy:
    """Minimal client for toxiproxy's REST API (stdlib only, run in a thread)."""

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self._base}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    async def _async(self, *args):
        return await asyncio.to_thread(self._call, *args)

    async def degraded(self) -> bool:
        toxics = await self._async("GET", f"/proxies/{PROXY}/toxics") or []
        return any(t["name"] in TOXICS for t in toxics)

    async def degrade(self) -> None:
        await self.restore()
        for name, (kind, stream, toxicity, attrs) in TOXICS.items():
            await self._async("POST", f"/proxies/{PROXY}/toxics", {
                "name": name, "type": kind, "stream": stream, "toxicity": toxicity,
                "attributes": attrs})

    async def restore(self) -> None:
        existing = {t["name"] for t in (await self._async("GET", f"/proxies/{PROXY}/toxics") or [])}
        for name in TOXICS:
            if name in existing:
                await self._async("DELETE", f"/proxies/{PROXY}/toxics/{name}")


def network_client(deps) -> Toxiproxy:
    return getattr(deps, "toxiproxy", None) or Toxiproxy(deps.settings.toxiproxy_url)


async def reap_once(deps) -> list[str]:
    """Undo what ran out. Storage faults expire on their own (the wrapper ignores expired
    rows); a network fault must be removed from toxiproxy."""
    gone = await deps.repo.expired_faults()
    if "network" in gone:
        try:
            await network_client(deps).restore()
            await deps.repo.add_audit(CHAOS_AUDIT_ID, "chaos", "chaos-reaper", "-",
                                      "Rețea: degradarea a expirat, revenire la normal")
            log.warning("network degradation expired; restored")
        except Exception:
            log.exception("could not restore the network after its fault expired")
            await deps.repo.set_fault("network", "bad", 0, 10, "chaos-reaper")  # try again soon
    return gone


async def run_reaper(deps, stop: asyncio.Event) -> None:
    if not deps.settings.chaos_enabled:
        return
    while not stop.is_set():
        try:
            await reap_once(deps)
        except Exception:
            log.exception("chaos reaper pass failed")
        try:
            await asyncio.wait_for(stop.wait(), 5.0)
        except (asyncio.TimeoutError, TimeoutError):
            pass
