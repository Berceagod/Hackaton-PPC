# Runbook: integrity

A corrupt file is never served. These alerts say damage was found, or repaired. Dashboards: Grafana -> *FileGrid - Business guarantees* -> Integrity.

## FileIntegrityFailure
**Means:** a stored file no longer matches the SHA-256 recorded at ingest (`checksum_mismatch`), vanished from its storage (`object_missing`), or its ciphertext no longer authenticates (`decryption_failed`). The download was refused (409); nothing damaged was served. If the category had a second copy and that was damaged too, this is what you see.

1. Which file? Grafana alert labels give category, storage and reason. In the app: *Fisiere & Audit*, the file's audit trail (`download_blocked` or `scrub_failed`) has the trace id.
2. Is a second copy available? `GET /api/v1/files/<id>/audit`: a `replicated` entry means there was one. `docker compose exec postgres-meta psql -U filegrid -d filegrid -c "select * from file_replicas where file_id='<id>'"` shows its status.
3. Is the storage itself the problem? `docker compose ps`, then the *Operations* dashboard (storage error rate and latency). One bad file is a disk or a human; many files on one storage is the storage.
4. Restore from the second copy or from backup. If `file_replicas.status = 'ok'`, the next download or the next scrubber pass repairs the primary by itself.
5. Look at who touched the storage directly (it is not reachable from outside; check `docker compose logs` of the storage and the audit chain: `GET /api/v1/audit/verify`).

## FileRepaired (info)
**Means:** damage was found and repaired from the other copy. Nothing was lost and nothing damaged was served. No action is urgent, but the storage that held the damaged copy deserves a look (disk errors, someone writing to it directly). A repeated `FileRepaired` on the same storage is a failing disk.

## RepairFailed
**Means:** a damaged copy could not be rewritten from its good copy (the target storage is down, full or read-only). The file is now readable from one copy only.

1. `docker compose ps`; is the target storage up? Check free disk on its volume.
2. Once it is back the scrubber retries on its own at the next pass (`GET /api/v1/admin/scrubber`).
3. If it keeps failing, restore the damaged copy by hand: download the file through the app (it is served from the good copy) and re-upload.
