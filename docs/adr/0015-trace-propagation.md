# ADR-0015: One operation, one trace id, from the gateway to the storage
Status: accepted (slice H)

## Context
PDF §4: "a single operation can be followed from the initial request to the storage". The backend already assigned or accepted an `X-Trace-Id`, returned it in the response header and the ingest body, and put it in every JSON log line (contextvar). Missing: the gateway hop, the storage hop, and the outbox (a reading accepted during an outage is written out minutes later by another task).

## Decision
- **Storage hop.** `BreakerStorage` logs one `storage op` line per call: `storage`, `op` (put/get/delete), `outcome`, `duration_ms`, plus `category` and `operation` of the request it belongs to, under that request's trace id. Calls made while a download streams are attributed to the download as well. Meter-reading calls are logged only when slow (> 1 s) or failed: 10 to 100 readings per second would drown Loki; every other category logs every call.
- **Gateway hop.** nginx writes a JSON access line per request whose `trace_id` is `$upstream_http_x_trace_id`, the id the backend assigned, so the gateway line carries the same id even when the client sent none. Only `$uri` is logged (no query string), so search terms never reach the log.
- **Outbox.** When the drainer writes a queued reading to its storage it runs under the reading's ORIGINAL trace id; the storage span of a reading accepted during an outage is found under the id the meter was given.
- **Finding it.** Loki gets the trace id as structured metadata (not a label: too many distinct values). Queries are limited to `{service=~"backend|nginx"}`, the two services that log trace ids: Loki and Grafana log the queries they receive, and those contain the id, so matching every service would fill the result with the lookup's own traffic (seen while testing the dashboard). A Grafana dashboard ("Trace one operation") takes a trace id and shows every line in time order, the storage calls alone, and the gateway line; the Loki datasource turns any trace id in a log line into a link to all of its logs. `scripts/demo/trace-one-request.sh` proves it end to end.

## Why not exemplars / OpenTelemetry
`prometheus_client` does not support exemplars in multiprocess mode, which the contract requires (ADR-0014), so a trace id cannot be attached to a histogram sample. Full OpenTelemetry tracing (collector + a trace store such as Tempo) would add services and instrumentation of every adapter for a span tree that the structured spans above already approximate for a 3-hop system.

## Consequences
- Metrics and traces are tied by labels (category, storage) and by time, not by a click from a bucket to a trace.
- There is no waterfall view; the "spans" are ordered log lines with durations.
- Slice J's replicas need nothing extra: the id travels in the request.
