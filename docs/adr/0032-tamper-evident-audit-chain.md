# ADR-0032: Tamper-evident audit log (hash chain)
Status: accepted (improvements.md 2.3)

## Context
PDF §2.1: connection documents must stay "protected and verifiable at audit". The audit trail was a plain table: anyone with database access could rewrite who downloaded what, or delete an inconvenient row, and nothing would show it.

## Decision
- Each `audit_logs` row stores `prev_hash` and `row_hash = SHA-256(prev_hash, id, file_id, action, username, trace_id, details, created_at)`; the first row's `prev_hash` is 64 zeros. Fields are joined with a unit-separator character (no ambiguity); the timestamp is hashed as UTC with microseconds, so the same instant in another zone gives the same hash.
- **One chain, one order.** Appends take a Postgres advisory lock held until commit and read the latest `row_hash` inside the transaction, so concurrent workers and replicas cannot fork the chain. Measured: 60 parallel requests across 4 workers produced 47 rows, all chained (gap-free `seq`, no duplicate `prev_hash`), chain valid.
- `GET /api/v1/audit/verify` (admin) streams every chained row with a cursor and recomputes. It reports `valid`, `checked`, `head_hash` and the first break: `row_hash_mismatch` (a row was edited) or `chain_broken` (a row was removed, inserted or moved).
- Rows written before this change have no hashes and are simply not in the chain (columns added with `ADD COLUMN IF NOT EXISTS`); nothing is rewritten.
- The scrubber logs the head hash about once an hour (`audit anchor`), and logs an error if the chain is broken.

## Limits (stated plainly)
- Deleting the LAST rows leaves a shorter chain that is still consistent. Detecting that needs a copy of the head hash outside the database: the hourly log line (shipped to Loki) is that copy, but only if the log store is not under the same attacker's control.
- Someone who can rewrite the whole table AND recompute every hash can forge a consistent chain; the anchor is what defeats that.
- It proves the log was changed, not who changed it.

## Consequences
- Scripts and operators must never delete or edit `audit_logs` rows (a demo script that did was fixed to leave audit rows in place).
- Proof: `scripts/demo/audit-tamper.sh` (edit and delete are detected, naming the row; each is clean again once undone) and the audit-chain unit tests.
