# ADR-0002: Contract additions
Status: accepted
Context: The frontend uses `POST /auth/login`, `GET /files/{id}/audit` and a `storage` filter on `GET /files`, none of which are in API_CONTRACT.md.
Decision: Add to the contract (existing six endpoints unchanged): `POST /auth/login`, `GET /files/{id}/audit`, `GET /files?storage=`, `GET /files/{id}/verify`, `GET /health`, `GET /metrics`; `GET /admin/policies` returns the frontend `PoliciesMap` shape. `/health` and `/metrics` are unauthenticated and reachable only on the internal network (nginx does not proxy `/metrics`).
Consequences: Contract is the single reference for frontend, simulator and tests.

Update (slice D): implemented and written into `API_CONTRACT.md`. Correction: `/api/v1/health` is reachable through nginx (it proxies all of `/api/`), so it now requires a dispecer/admin token; the unauthenticated `/health` and `/livez` stay on the root path, which nginx does not proxy.
