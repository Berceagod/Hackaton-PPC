# Runbook: drone videos (transfer survives slow or unstable links)

## VideoUploadStalled
**Means:** a chunked upload received no chunk within the stall threshold (5 minutes by default). Nothing is lost: the upload is resumable, and abandoned ones are purged after 24 h.

1. How many and for whom? Grafana *Business guarantees* -> Drone videos. The client holds the file; it resumes by sending the missing chunks (`GET /api/v1/upload/<id>` lists which ones arrived).
2. Is it the link? A field team on a poor connection is the normal cause. To see what the system does under that condition: `scripts/chaos/network-bad.sh` and `scripts/demo/slow-network.sh` (undo with `scripts/chaos/network-ok.sh`).
3. If it is not the link: `docker compose logs backend --since 10m | grep -i upload` and check the staging storage (S3) is up.
4. Nothing to do if the client comes back: it resumes where it stopped.
