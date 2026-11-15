# Runbook: outage records (immediately available to dispatchers)

A record is acknowledged only after it is stored, so the save time is how long a dispatcher waits for it to exist. Dashboards: *FileGrid - Business guarantees* -> Outage records.

## OutageRecordIngestSlow
**Means:** saving an outage record takes more than 2 s at p95.
1. Which storage holds the category? `GET /api/v1/admin/policies`. The *Operations* dashboard shows latency per storage: one slow storage is the usual cause.
2. Is it load? A spike in another category cannot slow this one (separate bulkheads), but CPU or the network can: check *CategoryAtCapacity* and backend CPU (`docker stats`).
3. Move the category to a faster storage with a policy change (no restart): `PUT /api/v1/admin/policies` or the *Politici Rutare* page.

## OutageRecordDownloadSlow
**Means:** opening a record takes more than 3 s at p95. Downloads verify the SHA-256 first (they read the file twice), so large files are slower. Same checks as above.

## OutageIngestSLOBurn
**Means:** the SLO is "99.5 % of outage records saved within 2.5 s". More than 5 % were slower in the last 10 minutes: the error budget is burning ten times faster than allowed. Treat it like *OutageRecordIngestSlow*, but note the time since it started: after about a day at this rate the monthly budget is gone.
