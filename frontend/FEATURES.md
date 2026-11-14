# FileGrid PPC — Frontend Features

Stack: React 19 + TypeScript + Vite 8 + Tailwind CSS 4 + Lucide + Recharts  
Path: `frontend/` · Mock default: `VITE_USE_MOCK=true`, API: `VITE_API_URL=/api/v1`

## Layout

```
src/
  types/filegrid.ts      # types + CATEGORY_LABELS, CHUNK_SIZE (5MB)
  services/api.ts        # real API + full mock (localStorage, SHA-256, chunks)
  components/            # Header, LoginScreen, Toast, ProgressBar, StorageBadge, AuditDrawer
  pages/                 # UploadPage, FilesPage, PoliciesPage, AnalyticsPage
  App.tsx                # auth gate + 4 tabs
Dockerfile + nginx.conf  # multi-stage Vite build → Nginx :80, SPA + /api proxy
```

## Auth

- Login: `dispecer@ppc.ro`/`dispecer`, `admin@ppc.ro`/`admin`
- JWT mock in `localStorage` (`filegrid_token`, `filegrid_user`) → `Authorization: Bearer`
- Header: API Online banner, Mock Mode badge, user, Logout

## Tab Upload

- Categories (manual): `outage_records`, `connection_docs` (AES badge), `drone_videos`
- Note only for `meter_readings` (simulator — nu se încarcă manual)
- Meta optional: zone (Muntenia/Banat/Dobrogea), station, description
- Files ≤5MB → `POST /ingest`; >5MB or drone → chunked `POST /upload/chunk` (5MB slices, 3 retries exponential backoff)
- Progress: %, chunk N/M, MB/s, retry status
- Result card: `file_id`, `trace_id`, storage, SHA-256 (Web Crypto)

## Tab Files & Audit

- Filters: category, storage (postgres/s3/ftp), search (name/trace_id/zone)
- Table: name, category, storage badge, size, date, security (AES / Idempotent), trace copy, Download
- Download → integrity toast (`X-Integrity-Verified`) or 409 corrupted alert
- Audit drawer for `connection_docs` only

## Tab Policies

- `GET/PUT /admin/policies` — per category: storage, encryption, dedupe, priority
- Hot-reload confirm (no restart); mock persists in `localStorage`

## Tab Analytics

- `GET /analytics/summary`: storage×category matrix + verdicts RO, storm 10× bar chart (Recharts), zone insights (Muntenia/Banat/Dobrogea)

## Mock notes

- Seed demo files on first load; UTF-8 via `utf8ToBase64` (not raw `btoa` — diacritics crash)
- Chunks in memory Map; file list/policies/audit in `localStorage`
- Chunk upload ~12% simulated failure for retry demo
- Toggle mock off: `.env` → `VITE_USE_MOCK=false`

## Simulator (`../simulator/`)

Trimite continuu citiri de contor către **API FileGrid**, nu către frontend.

| | |
|---|---|
| Entry | `sim.py` — flota ~1000 contoare, rate control, storm 10× |
| Fake API | `mock_api.py` — `202` nou, `409` duplicate, `503` aleatoriu (retry) |
| Control | `:8080` — `/health`, `/status`, `POST /mode`, `PATCH /config`, `POST /duplicates/burst`, `/metrics` |
| Target | `API_URL` default `https://nginx/api/v1/ingest` |

**Ingest (multipart, `INGEST_MODE=multipart`):**
- `file` = JSON reading (`meter_id`, `timestamp`, `zone`, energy/instant/events…)
- `category` = `meter_readings`
- `metadata` = `{meter_id, timestamp, zone, source: "meter-simulator"}`
- Headers: `Idempotency-Key: {meter_id}:{timestamp}`, optional `Authorization: Bearer`, `X-Source`

**Compat cu frontend/contract:** aceleași categorii + zone (Muntenia/Banat/Dobrogea); UI așteaptă `meter_readings` din simulator.  
**Nu e legat de Mock Mode:** cu `VITE_USE_MOCK=true` citirile **nu apar** în tab Fișiere (localStorage browser ≠ HTTP ingest). E2E UI = backend real + `VITE_USE_MOCK=false`.

### Test rapid simulator (fără backend)

```bash
cd simulator && pip install -r requirements.txt
python mock_api.py                          # :9000
# alt terminal:
set API_URL=http://127.0.0.1:9000/api/v1/ingest
set VERIFY_TLS=false
set BASE_RATE=5
python sim.py                               # control :8080
```

Check: `GET :8080/status`, `GET :9000/stats` · Storm: `POST :8080/mode {"mode":"storm"}` · Idempotency: `POST :8080/duplicates/burst {"count":50}` → crește `duplicate_rejected`, nu `unique_rows`.

## Run frontend

```bash
cd frontend && npm install && npm run dev   # :5173
npm run build                              # tsc + vite
docker build -t filegrid-frontend .
```
