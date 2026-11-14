# ADR-0010: Chunked, resumable uploads staged on S3
Status: accepted (slice E)

Context: the UI sends every file > 5 MB and every drone video as 5 MB chunks to `POST /upload/chunk`, sequentially, with retries. PDF §4: files > 1 GB without resources growing with the size; transfers finish or resume on bad links; any backend replica must be able to serve any request (scale-out).

Decision:
- Each chunk is streamed (64 KB reads) to S3 under `_staging/<upload_id>/<index:06d>` and recorded in `upload_chunks (upload_id, idx) PRIMARY KEY`. A resent chunk overwrites the same key and row: retries are always safe. No local disk, no sticky sessions.
- The first chunk to arrive opens `upload_sessions` with the file's identity (filename, category, metadata, total_chunks, optional declared `size_bytes` and `sha256`). Later chunks must match it (else 409). Only the owner sees or extends an upload (others get 404).
- Optional `chunk_sha256` per chunk: mismatch -> 422 and the chunk is discarded; the client resends it. This replaces a whole-file hash in the browser (`crypto.subtle` cannot hash a 1 GB file without loading it whole). The server always hashes the assembled file.
- `GET /upload/{id}` returns `received` / `missing` chunk indexes: a client resumes by sending only the missing ones.
- When the last missing chunk is recorded, one request wins `open -> assembling` (conditional UPDATE) and streams the staged chunks in order into the storage named by the category's policy, hashing on the way. Declared size/SHA mismatch -> `failed` (422), nothing kept. Target storage down -> back to `open` (503) with every chunk still staged; resending the last chunk retries the assembly. An `assembling` upload untouched for 10 min is taken over.
- Success: one transaction writes `files_metadata` and marks the session `complete`; audit row; staged chunks deleted. A chunk resent after completion returns the same result (idempotent).
- Limits: chunk <= 16 MB (413), total_chunks <= 100000; `meter_readings` are refused here (they use the idempotent `/ingest` path); encrypted categories return 501 until slice F.

Consequences:
- Memory per request is bounded by the chunk and S3 part size (5 MB), proven with a 1.2 GB file through a backend capped at 512 MB (`scripts/demo/large-file-1gb.sh`: +29 MiB).
- The file is written twice (staging, then final). Accepted for simplicity and storage independence (the final target may be FTP or Postgres); an S3-to-S3 multipart copy is a later optimisation.
- Chunk uploads need S3 even when the policy targets FTP/Postgres. If MinIO is down, chunked uploads get 503 (the client retries).
- Abandoned uploads leave staged chunks until a cleanup job exists (not yet implemented; candidate for slice G).
