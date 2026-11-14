# ADR-0005: Frontend wiring with minimal edits
Status: accepted
Decision: Compose builds the frontend with `VITE_USE_MOCK=false`. Its nginx role is merged into the gateway (frontend image only provides static files). Frontend source untouched unless the real API exposes a bug; each edit is listed in the PR description.
