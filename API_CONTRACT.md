# Contract API FileGrid

- **Base URL:** `https://localhost/api/v1`
- **Auth Header:** `Authorization: Bearer <TOKEN>` (raspuns cu `X-Trace-Id` în header)

1. `POST /ingest` (multipart/form-data: `file`, `category` = `meter_readings` | `outage_records` | `connection_docs` | `drone_videos`, `metadata` JSON)
   -> Returneaza `202 Accepted`: `{ "file_id": "uuid", "trace_id": "uuid", "sha256": "...", "storage": "s3" }`
2. `POST /upload/chunk` (pentru video-uri > 1 GB: `upload_id`, `chunk_index`, `total_chunks`, `file_chunk`)
   -> Returneaza `200 OK`: `{ "upload_id": "...", "received_chunks": 5 }`
3. `GET /files?category=...&q=...`
   -> Returneaza lista fisierelor: `[{ "id", "filename", "category", "storage", "size_bytes", "sha256", "created_at", "encrypted" }]`
4. `GET /files/{id}/download`
   -> Descarca fisierul + header `X-Integrity-Verified: true` si `X-File-Sha256: <hash>`
5. `GET /admin/policies` & `PUT /admin/policies` (primeste JSON/YAML si face hot-reload fara restart)
6. `GET /analytics/summary`
