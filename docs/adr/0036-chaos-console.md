# ADR-0036: Chaos console (failures on demand, safely)
Status: accepted (improvements.md 2.1; the UI for it is part of the UI remake)

## Context
The non-functional requirements (a storage down loses nothing and stops no other category, bad networks, recovery) were proven by scripts that stop real containers. Convincing, but invisible to an audience and not repeatable from the UI.

## Decision
- **Off by default.** `CHAOS_ENABLED=false` in a default install: no wrapper is installed (zero overhead, nothing to attack), `GET /admin/chaos` answers `{"enabled": false}` and every other call is 403. Enable with `CHAOS_ENABLED=true docker compose up -d backend`.
- **Storage faults** (`down`, or `slow` by 0.1-30 s per operation) are injected by `storage/chaos.py`, which sits BETWEEN the real adapter and the circuit breaker. The system therefore reacts exactly as to a real outage: the breaker opens, meter readings go to the outbox, replicas are served, metrics move, alerts fire. Faults live in the table `chaos_faults`, so every backend replica sees one within a second.
- **Network degradation** uses toxiproxy's REST API (same toxics as `scripts/chaos/network-bad.sh`: 400±200 ms each way, 2 MB/s upload, 20% of connections cut). It also slows the console's own requests; the panic button is retried.
- **Every fault expires by itself** (default 60 s, at most `CHAOS_MAX_SECONDS` = 600). Storage faults expire because the wrapper ignores expired rows; a network toxic must be undone, so a reaper (any replica) removes it and audits that it did.
- **Admin only; every use is audited** (audit trail of the fixed id `0000...c4a0`, part of the tamper-evident chain) and logged at warning level with the user.
- `DELETE /admin/chaos` is the panic button: it clears every fault and restores the network.

## Honest limits
- A storage fault is an application-level simulation: the container keeps running. `kill-storage.sh` remains the proof of a true outage; the console is for showing it live and repeatably.
- The console cannot stop containers (no Docker socket is given to the backend, deliberately).
- Load spikes are not part of the console (the simulator is a teammate's service); `spike-10x.sh` and `scripts/scenarios/*` do that.

## Proof
`scripts/demo/chaos.sh` (S3 down: outage uploads 503, connection documents still 202, recovery with no action; the meter storage down: 5 of 5 readings accepted, 5 queued, 5 delivered after it returns, none duplicated; network degraded then restored; a 5 s fault that expires by itself; audit chain valid) passed on the live stack. 37 unit tests (`test_chaos.py`).
