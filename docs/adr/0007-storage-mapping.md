# ADR-0007: Storage mapping and policy persistence
Status: accepted (updated in slice B)
Decision: Defaults come from `policies.yaml` (meter→postgres, outage→s3, docs→ftp+encryption, video→s3). On first start they are seeded into the `policies_config` table of the metadata DB; from then on the table is the source of truth. `PUT /admin/policies` (admin only, JSON or YAML, flat map or `{categories: ...}`) validates, persists and applies the change immediately. Every backend replica re-reads the table at most 2 s later (TTL cache), so hot reload works with `--scale backend=N` and survives restarts. `policies.yaml` is mounted read-only and is not rewritten: containers must not edit a tracked repo file.
Each file records the storage it was written to, so a policy change only affects new uploads; older files stay readable where they are.
The benchmark (slice I) may justify changing a mapping; if so only policy data changes, never code.
Consequences: To reset to the YAML defaults, PUT the YAML file content or clear `policies_config`.
