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

## Completări (slice D, ADR-0002) — cele 6 puncte de mai sus rămân valabile

### Autentificare și roluri
7. `POST /auth/login` body `{ "email", "password" }`
   -> `200`: `{ "token": "<JWT>", "user": { "email", "role": "dispecer" | "admin", "name" } }`; credențiale greșite -> `401`
   - Token-ul expiră după 24 h. Orice token lipsă / invalid / expirat -> `401` (UI-ul revine la ecranul de login).
- Peste 20 de încercări de login pe minut de la același IP -> `429` (nginx).
- Parolele conturilor sunt generate aleator la prima pornire (`start.sh` / `start.ps1` le afișează; sunt doar în `.env`).
   - Simulatorul folosește token-ul fix `SIMULATOR_TOKEN` din `.env` (rol `service`).

| Endpoint | dispecer | admin | service (simulator) |
|---|---|---|---|
| `POST /ingest` | da | da | doar `meter_readings` (altfel `403`) |
| `GET /files`, `/files/{id}/download`, `/verify`, `/audit` | da | da | `403` |
| `GET /admin/policies` | da | da | `403` |
| `PUT /admin/policies` | `403` | da | `403` |
| `GET /health` | da | da | `403` |

### Endpoint-uri noi
8. `GET /files/{id}/audit` -> `[{ "id", "file_id", "action": "upload" | "download", "event", "user", "timestamp", "details" }]`, cel mai nou primul.
   `event` = evenimentul exact: `upload` | `download` | `verify` | `download_blocked`.
9. `GET /files/{id}/verify` -> `{ "file_id", "verified": bool, "expected_sha256", "actual_sha256", "reason": null | "checksum_mismatch" | "object_missing" }`
10. `GET /health` -> `{ "status": "ok" | "degraded", "checks": { "metadata_db", "postgres", "s3", "ftp": bool } }` (mereu `200` cât timp procesul trăiește).
    `GET /livez` -> `{ "status": "alive" }`, fără autentificare.

### `GET /files` — parametri
`category`, `storage` (`postgres` | `s3` | `ftp`), `q` (nume fișier, trace_id, zonă; fără majuscule), `limit` (1–1000, implicit 500), `offset` (implicit 0). Cele mai noi primele.
Fără `category` și fără `q`, citirile de contor (`meter_readings`) nu sunt incluse; se văd cu `category=meter_readings` sau căutând cu `q`.
Răspunsul conține și câmpurile opționale `trace_id`, `zone`, `station`, `description`, `uploaded_by`.

### Download
`Content-Disposition: attachment; filename="<nume ASCII>"; filename*=UTF-8''<nume exact, percent-encoded>` (numele cu diacritice se păstrează).
Fișier corupt sau lipsă în storage -> `409` + `X-Integrity-Verified: false`, corp `{ "error": "integrity_check_failed", "reason", "expected_sha256", "actual_sha256" }`; niciun byte nu e livrat.

### Coduri de răspuns la `POST /ingest`
| Cod | Când |
|---|---|
| `202` | stocat durabil; `{ file_id, trace_id, sha256, storage }` |
| `409` | citire de contor deja stocată (`duplicate_reading`), sau fișier identic când politica are `deduplicate: true` (`duplicate_file`); conține `file_id` existent |
| `503` | storage-ul indisponibil sau aceeași citire e încă în scriere (`Retry-After: 1`); clientul reîncearcă |
| `422` | categorie / metadata / citire invalidă; `Idempotency-Key` (`{meter_id}:{timestamp}`) diferit de citire |
| `413` | citire de contor > 1 MB |
| `501` | storage-ul numit în politică nu există |
| `401` / `403` | vezi tabelul de roluri |

### `PUT /admin/policies`
Acceptă forma întoarsă de `GET` (`{ "<categorie>": { storage, encryption, deduplicate, priority } }`) sau `{ "categories": { ... } }` (forma din `policies.yaml`), JSON sau YAML. Invalid -> `422` și nimic nu se aplică.

## Upload pe bucăți (slice E, ADR-0010) — detaliază punctul 2
`POST /upload/chunk` (multipart), câmpuri:
`upload_id` (UUID ales de client), `chunk_index` (0..total-1), `total_chunks`, `file_chunk` (≤ 16 MB; UI-ul trimite 5 MB),
`filename`, `category` (nu `meter_readings`), `metadata` (JSON), opțional `sha256` și `size_bytes` (ale întregului fișier), opțional `chunk_sha256` (al bucății).
- Bucată primită: `200 { "upload_id", "received_chunks", "total_chunks", "status": "open" }`
- Bucata care completează fișierul: `200 { ..., "status": "complete", "file_id", "trace_id", "sha256", "storage" }`. Retrimisă după aceea -> același răspuns.
- `422` `chunk_checksum_mismatch` (bucată coruptă pe drum: retrimiteți-o) · `413` bucată prea mare · `409` `upload_id_conflict` / `upload_failed` (`file_checksum_mismatch`, `file_size_mismatch`, `duplicate_file`: începeți un upload nou) · `503` + `Retry-After` (storage indisponibil sau fișierul e în asamblare: retrimiteți) · `404` upload-ul altui utilizator.

`GET /upload/{upload_id}` -> `{ "upload_id", "status": "open" | "assembling" | "complete" | "failed", "filename", "category", "total_chunks", "received_chunks", "received": [..], "missing": [..] }` (+ `file_id`, `sha256`, `storage`, `trace_id` când e complet; `reason` când a eșuat). Reluare: trimiteți doar indicii din `missing`.

## Criptare (slice F, ADR-0011)
Categoriile cu `encryption: true` în politică (implicit `connection_docs`) sunt criptate AES-256-GCM înainte de orice storage, inclusiv bucățile unui upload în curs.
API-ul nu se schimbă: `sha256` și `size_bytes` sunt ale fișierului original, download-ul întoarce fișierul original, `encrypted: true` apare în listă.
Conținut criptat alterat în storage -> download `409` cu `reason: "decryption_failed"`.

## Reziliență (slice G, ADR-0013)
- Citire de contor când storage-ul ei e căzut: `202 { ..., "status": "queued" }`. Citirea e păstrată durabil (outbox) și scrisă în storage automat când acesta revine; retrimisă între timp -> `409`. Apare în `/files` după ce e scrisă.
- `503` + `Retry-After: 1` pe `/ingest` și `/upload/chunk` când categoria e la capacitate maximă (bulkhead); clientul reîncearcă.
- O politică salvată se aplică imediat în toate procesele backend (Postgres NOTIFY).
