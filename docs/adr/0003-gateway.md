# ADR-0003: nginx is the only public entry, TLS at the edge
Status: accepted
Decision: nginx publishes 443 (and 80 redirecting to 443). Self-signed cert generated at first start into a volume. Serves SPA, proxies `/api/` to `backend:8000`. Large uploads use the chunk path; `client_max_body_size` stays 2g and `proxy_request_buffering off` on upload locations. Network alias `nginx` so the simulator's `https://nginx/api/v1/ingest` works. No other service publishes host ports.
Consequences: Satisfies "storages unreachable" and "external traffic encrypted". Demos run with `curl -k`.
Update (slice D): the key pair now really lives in the `gateway-certs` volume (it was in the container layer, so every re-creation issued a new certificate and broke the simulator's pinned CA).
