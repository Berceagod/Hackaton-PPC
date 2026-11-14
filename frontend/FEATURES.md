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
- Note only for `meter_readings` (simulator)
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

## Run

```bash
cd frontend && npm install && npm run dev   # :5173
npm run build                              # tsc + vite
docker build -t filegrid-frontend .
```
