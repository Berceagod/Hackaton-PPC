# ADR-0019: What telemetry may contain (personal data stays out)
Status: accepted (slice H)

## Context
Connection documents contain personal data and must stay protected and auditable (table 2.1). Metrics, logs and alerts are stored in more places, with weaker protection, than the files themselves.

## Decision
- **Metrics** carry only counts, durations and sizes labelled by values the code chooses from fixed sets (category, storage, operation, outcome, reason). No filename, user, file id or trace id is ever a label, so nothing a client types can reach Prometheus.
- **Log lines added by slice H** (`storage op`, the gateway access line) hold storage, operation, outcome, duration, category and the trace id; never a filename, metadata field, user or key. The gateway logs `$uri` only: no query string, so a search for a person's name is not logged. Alerts carry the alert name, the category/storage and the guarantee, never file names.
- **Existing log lines** (written before slice H, not changed here) include: file and upload ids, object keys (`category/<uuid>`; for meter readings `meter_readings/<meter id>/<timestamp>`), the meter id and reading timestamp of ingest events (pseudonymous), and the administrator's e-mail when policies are changed. No filename, document content or customer data is logged.
- **Retention and access.** Logs are kept 48 h and metrics 2 days, in Docker volumes; reading them needs the Grafana login. `/metrics` is reachable only on the internal network.

## Consequences
- The PII alert reports a count, not which document; the person investigating queries the database (`files_metadata`, `audit_logs`).
- Anyone adding a log line or a metric label must follow the same rule; the privacy test in `test_slice_h.py` checks `/metrics` against a document named after a person.
