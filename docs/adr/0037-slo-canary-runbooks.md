# ADR-0037: Storage canary, an SLO alert and runbooks
Status: accepted (improvements.md 2.8)

## Context
Alerts were built on traffic. With no uploads, a broken storage shows nothing; and an alert with no instructions only tells someone to start thinking at 3 a.m.

## Decision
- **Storage canary** (`canary.py`): once a minute per worker, a random 2 KB object is written to EVERY storage (through the same adapters, circuit breakers and chaos wrapper as real traffic), read back, compared byte for byte, and deleted. Keys live under `_canary/`, never in the database; user data is untouched. Metrics: `filegrid_canary_runs_total{storage,outcome}` (the failed series start at 0 so the first failure is seen) and `filegrid_canary_duration_seconds{storage}`. Alert `StorageCanaryFailing` (two failures in 3 minutes, for 30 s, warning). It also reports a storage that returns silently different bytes.
- **SLO**: "99.5 % of outage records are saved within 2.5 s" (2.5 s is a histogram bucket boundary). `OutageIngestSLOBurn` fires when more than 5 % (ten times the 0.5 % budget) were slower over 10 minutes with enough traffic to be meaningful. Both alerts have promtool tests, one for firing and one for staying quiet.
- **Runbooks**: every alert carries `annotations.runbook` pointing at `docs/runbooks/<guarantee>.md#<alertname>` (21 alerts, 6 files): what it means, what to check in order, how to fix. The link is added and tested together with the alert, so an alert cannot be added without one being expected by the tests.
- **Dashboard**: *Business guarantees* gains a row "Self-healing and early detection": repairs, repairs that failed, second copies written/pending, canary failures, scrubber outcomes over time, canary round-trip p95.

## Not done (honest)
- No error-budget *remaining* panel (it needs 30 days of history, which a demo stack does not have); the burn alert is the actionable half.
- A real notification channel (Slack/Telegram) is not wired: the webhook receiver logs and keeps alerts. Adding a receiver is one block in `alertmanager.yml` and needs a token that must not live in the repository.
- No public status page.

## Proof
Live: with FTP taken down through the chaos console, `StorageCanaryFailing` reached `firing` in Prometheus with its runbook link; 9 canary tests, promtool tests for both alerts and for the existing ones with the new annotation.
