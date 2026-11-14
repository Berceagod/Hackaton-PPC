# ADR-0017: Logs to Loki through Alloy and a read-only Docker API proxy
Status: accepted (slice H)

## Context
Loki needs a collector. Our services log JSON to stdout. The collector must know which container is which service, and it must not become a way to take over the host.

## Decision
- **Loki** (single binary, local filesystem, 48 h retention, internal only). **Grafana Alloy** reads the containers' logs and pushes them to Loki. (Promtail reached end of life in 2026.)
- **Labels** are `service`, `container` and `level`. The **trace id is structured metadata**, not a label (every request has a new one). Non-JSON lines (databases, MinIO, FTP) are shipped as plain text.
- **Only this project's containers.** Alloy asks the Docker API for containers with the label `com.docker.compose.project=<LOKI_COMPOSE_PROJECT>` (the project's name, written by `start.sh`/`start.ps1`, default `hackaton-ppc`), so logs of anything else on the machine are never read. Verified: on a machine running another project, only this project's 15 services appear in Loki.
- **The Docker socket is the risk.** Reading container logs needs the Docker API. Handing Alloy the socket would let it start containers. Instead a `tecnativa/docker-socket-proxy` allows only `GET` on containers/networks/events/version/info (`POST=0`), and it shares **Alloy's network namespace and listens on 127.0.0.1 only** (`network_mode: service:alloy`, entrypoint override), so no other container can reach the Docker API through it. The socket is mounted read-only into that proxy alone.

## Trade-offs
- The proxy cannot filter by project; the project filter is in Alloy's configuration. A compromised Alloy could read metadata and logs of every container on the Docker host. It cannot change anything.
- The Docker-API approach is specific to Docker Compose. On Kubernetes (slice J) the collector reads the node's pod log files as a DaemonSet instead; nothing else changes.
- Logs of the proxy and of Alloy itself are collected too (useful when the pipeline misbehaves).
