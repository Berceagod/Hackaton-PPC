# FileGrid

FileGrid este punctul unic de intrare pentru fișierele din teren ale unui distribuitor de energie (Muntenia, Banat, Dobrogea). Un fișier ajunge într-o stocare numai prin aplicație. Politica fiecărei categorii, schimbabilă fără restart, alege serviciul: FTP, S3 (MinIO) sau PostgreSQL. La descărcare, FileGrid recalculează SHA-256 și nu livrează octeți dacă fișierul nu este identic cu cel depus.

Locația unui fișier este un județ. Lista are cele 41 de județe. În Analitice se alege județul de afișat.

## Pornire

Cerințe: Docker pornit (Docker Desktop pe Windows). Porturile 80 și 443 libere. Un PostgreSQL instalat pe gazdă, pe 5432, nu este FileGrid.

Prima pornire compilează MinIO din sursă (imaginile oficiale nu mai sunt publicate) și interfața. Durează câteva minute. Secretele lipsă sunt generate în `.env`, care nu este versionat. Valorile existente nu sunt rescrise.

Windows, PowerShell, din rădăcina repo-ului:

```
.\start.ps1
```

Linux sau WSL:

```
./start.sh
```

Scriptul ridică stiva (`docker compose up -d --build`) și așteaptă `https://localhost/api/v1/livez`. La final afișează adresele și parolele generate pe mașina asta:

```
https://localhost
dispecer@ppc.ro / <parolă>
admin@ppc.ro    / <parolă>
auditor@ppc.ro  / <parolă>     doar audit, fără descărcare
https://localhost/grafana/     admin / <GRAFANA_ADMIN_PASSWORD>
```

Acceptați certificatul self-signed. Dispecerul vede politicile doar în citire. Doar adminul le salvează, pornește testele și consola Chaos. Auditorul vede traseul de audit.

`TESTING_ENABLED` și `CHAOS_ENABLED` sunt `true` de la prima pornire. Butonul Testing și tabul Chaos apar pentru admin.

Oprire, cu datele păstrate: `docker compose down`. `docker compose down -v` șterge volumele. Păstrați o copie a `.env`: fără `FILEGRID_MASTER_KEY`, documentele de racordare deja cifrate nu se mai citesc.

Dacă așteptarea expiră: `docker compose ps` și `docker compose logs nginx backend`.

## Categorii

| Categorie | Intrare | Stocare implicită | Garanție |
|---|---|---|---|
| `meter_readings` | simulator | PostgreSQL, deduplicare | nicio citire confirmată pierdută sau dublată |
| `outage_records` | interfață | S3, prioritate mare | disponibile imediat |
| `connection_docs` | interfață | FTP + AES-256-GCM | date personale protejate și auditabile |
| `drone_videos` | interfață | S3, upload pe bucăți | transferul se reia pe o legătură lentă |

Valorile implicite sunt în `policies.yaml`. La prima pornire se copiază în tabelul `policies_config`; de atunci tabelul este sursa. `PUT /api/v1/admin/policies` (admin, JSON sau YAML) validează și aplică imediat. Cele 4 procese ale backend-ului primesc schimbarea prin `NOTIFY`/`LISTEN`. Un fișier deja scris rămâne în stocarea în care a fost pus. Adminul poate seta un `fallback_storage`: când stocarea principală cade sau categoria e supraîncărcată, fișierele noi merg acolo.

Fișiere de probă, cu date personale fictive: `demo-files/`. La încărcare, câmpul este Județ.

## Arhitectură

Singurele porturi publicate pe gazdă sunt 80 și 443, ambele ale nginx. Restul serviciilor stau pe o rețea Docker `internal: true`.

```
browser, simulator
        |  TLS
      nginx            SPA, /api/, /grafana/
        |
    toxiproxy          trece traficul; demo-ul de rețea proastă adaugă latență și tăieri
        |
     backend           FastAPI, 4 procese Uvicorn, fără stare locală
        |
   postgres-meta       metadate, citiri, audit, politici, outbox, măsurători
   postgres-blob       stocare: rânduri de 1 MB, o tranzacție per fișier
   minio               stocare S3
   ftp                 pure-ftpd; scriere în .part, redenumire la final
   prometheus          metrici, reguli de alertă, scrape prin DNS pe backend:8000
   alertmanager        trimite alertele la webhook-ul intern
   loki + alloy        loguri JSON, 48 h, căutare după X-Trace-Id
   grafana             dashboard-uri din cod
```

Rutele nu aleg stocarea. O caută după numele din politică, prin interfața `StorageBackend` (`postgres_storage`, `s3_storage`, `ftp_storage`).

Aceeași aplicație pornește și pe Kubernetes local: `k8s/up.sh`, verificarea cu `k8s/verify.sh`, oprirea cu `k8s/down.sh`. Intrarea este `https://localhost:8443`. HPA ține backend-ul între 2 și 6 pod-uri. NetworkPolicy lasă stocările accesibile doar din backend. Detalii în `k8s/README.md`.

## Tehnologii

- Backend: Python 3.11, FastAPI 0.115, Uvicorn 0.32, asyncpg, aioboto3, aioftp, PyJWT, cryptography 44, prometheus-client 0.21, PyYAML. Teste: pytest.
- Interfață: React 19, TypeScript, Vite, Tailwind CSS 4, Recharts. Imaginea nginx o compilează cu API-ul real (`VITE_USE_MOCK=false`).
- Simulator: Python, flotă de contoare pe cele 41 de județe, rată configurabilă, mod normal și furtună 10×. Verifică certificatul gateway-ului.
- Infrastructură: Docker Compose, nginx, PostgreSQL 16 (două instanțe), MinIO compilat din sursă, pure-ftpd, toxiproxy, Prometheus, Alertmanager, Loki, Grafana Alloy, Grafana. Kubernetes: kind, HPA, NetworkPolicy.

Contractul HTTP: `API_CONTRACT.md`. Deciziile: `docs/adr/`.

## Tehnici

Fișierul se citește în bucăți de 64 KB. SHA-256 se calculează pe flux. Memoria nu crește cu dimensiunea fișierului.

Sub 5 MB, `POST /api/v1/ingest` scrie direct în stocarea politicii. Peste 5 MB, și orice video de dronă, urcă prin `POST /api/v1/upload/chunk`, bucăți de 5 MB. Fiecare bucată se scrie în MinIO la `_staging/<upload_id>/<index>` și se înregistrează. Aceeași bucată retrimisă suprascrie aceeași cheie. `GET /api/v1/upload/{id}` listează indicii lipsă; clientul retrimite doar aceia. La ultima bucată, un singur request câștigă trecerea `open → assembling` și streamează bucățile în stocarea finală (S3, FTP sau PostgreSQL), cu hash pe drum. Un fișier de 1,2 GB (240 × 5 MB) a trecut printr-un backend limitat la 512 MB: memoria a urcat de la 78 la 107 MiB.

O citire de contor se identifică prin `(meter_id, timestamp)`. Constrângerea `UNIQUE` din `meter_readings` decide, inclusiv cu mai multe replici. Pașii: claim (`pending`) → scriere la o cheie deterministă → commit (`stored`) în aceeași tranzacție cu rândul de metadate. Doar atunci răspunsul este 202. O citire deja stocată primește 409. Dacă scrierea în stocare eșuează, octeții rămân în outbox cu status `queued` și contorul primește tot 202; un worker îi scrie când stocarea răspunde. Retrimiterea unei citiri din coadă este 409. 503 apare doar dacă și scrierea în outbox eșuează.

Izolarea între categorii: limită de concurență pe worker după prioritate (high 64, normal 32, low 8), pool de conexiuni separat pentru citiri, întrerupător pe stocare (3 eșecuri consecutive, apoi 5 secunde de refuz imediat). Un vârf de 10× pe o categorie nu ocupă sloturile celorlalte. Capacitatea crește cu `docker compose up -d --scale backend=N`, fără schimbare de cod. Prometheus descoperă replicile prin DNS.

Simulatorul ține într-un backlog citirile pe care nu le-a putut preda și le retrimite până la 202 sau 409.

## Observabilitate

Grafana, la `https://localhost/grafana/`, are trei dashboard-uri: Operations (p50/p95/p99, throughput și rată de erori pe categorie și pe stocare), Business guarantees și Trace one operation.

Fiecare răspuns poartă `X-Trace-Id`. Același id este în logul nginx, în logul backend și pe apelul către stocare. Loki îi ține 48 de ore. `scripts/demo/trace-one-request.sh` găsește un upload după id, de la gateway până la scrierea în stocare.

Alertmanager trimite alertele la webhook-ul intern când o garanție e în risc: citiri care nu mai sosesc, se pierd sau se dublează, avarii lente, document personal stocat necriptat, upload de video blocat, fișier corupt. Clopoțelul din interfață arată aceleași alerte. `scripts/demo/alert-fire.sh` le declanșează pe sistemul pornit.

## Securitate

Traficul din afara Docker trece prin TLS. Perechea de chei stă în volumul `gateway-certs`, așa că recrearea nginx păstrează certificatul pe care simulatorul îl verifică.

`POST /api/v1/auth/login` emite un JWT HS256, algoritm fix, 24 h. Roluri: `admin`, `dispecer`, `auditor`, `service`. Simulatorul folosește `SIMULATOR_TOKEN` și poate doar trimite citiri de contor. nginx limitează login-ul la 20 de încercări pe minut per IP, apoi 429. Headere: HSTS, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`.

Categoriile cu `encryption: true` (implicit `connection_docs`) sunt cifrate în backend, înainte de FTP, S3 sau PostgreSQL. Format `FGE1`: AES-256-GCM pe segmente de 64 KB. Numărul segmentului și un marcaj de ultim segment intră în datele autentificate, deci tăierea, reordonarea sau mutarea cifrului pe alt fișier se detectează înainte de a elibera vreun octet. Cheia fiecărui obiect iese din HKDF-SHA256 peste cheia master și id-ul obiectului. Cheia master, 32 de octeți aleatori, este doar în mediul containerului backend. O copie a stocării sau a metadatelor nu se decriptează. SHA-256 și dimensiunea din metadate sunt ale fișierului în clar. Cifru alterat: 409 `decryption_failed`, zero octeți, rând în `audit_logs`.

Parolele de interfață și cheile sunt generate la pornire. `scripts/security/secrets-scan.sh` verifică că `.env` nu este urmărit și rulează gitleaks pe istoric și pe arborele de lucru. Datele de test sunt sintetice.

Fiecare încărcare, descărcare, verificare și descărcare blocată lasă un rând de audit (utilizator, timp, eveniment, `X-Trace-Id`).

## Testare

Testele de backend sunt în `backend/tests`. Pe Windows, roata asyncpg lipsește pentru Python 3.15 de pe gazdă; suita se rulează așa, din rădăcina repo-ului:

```
docker run --rm -v "${PWD}/backend:/src:ro" python:3.12-slim sh -c "cp -r /src /app && cd /app && pip install -q -r requirements-dev.txt && python -m pytest -q tests"
```

Probele live sunt scripturi bash. Pe Windows se rulează din Git Bash sau WSL, după pornire, din rădăcina repo-ului. Citesc parolele din `.env`. Nu le rulați cu `bash -x`.

| Ce se arată | Comandă |
|---|---|
| Upload, listă, download identic la octet, verificare, apoi obiect alterat în S3 → 409 | `scripts/demo/smoke.sh` |
| PostgreSQL, FTP și S3, servicii separate; aceeași categorie mutată între ele fără restart | `scripts/demo/policy-change.sh` |
| Fișier alterat direct în fiecare stocare, refuzat la citire, cu audit | `scripts/demo/corrupt-file.sh` |
| Interfață: login, încărcare, căutare, descărcare | `https://localhost` |
| Simulator, volum configurabil | `scripts/sim.sh status` · `scripts/sim.sh mode storm` · `scripts/sim.sh rate 50` |
| Vârf 10× pe o categorie, latența celorlalte rămâne | `scripts/demo/spike-10x.sh` |
| Mai multe instanțe de backend, aceeași imagine | `scripts/demo/scale-out.sh` |
| 1,2 GB, memorie plată, reluare de la bucata lipsă | `scripts/demo/large-file-1gb.sh` |
| Stocările nu răspund din rețeaua publică; obiectul brut nu conține CNP sau nume; descărcarea prin FileGrid este fișierul original | `scripts/demo/unauthorized-direct-storage-access.sh` |
| Nicio parolă sau cheie în repo | `scripts/security/secrets-scan.sh` |
| Stocare oprită: citirile confirmate rămân în outbox, celelalte categorii continuă | `scripts/demo/kill-storage.sh` |
| Latență, bandă limitată, conexiuni tăiate: video reluat, citiri fără pierderi și fără duplicate | `scripts/demo/slow-network.sh` |
| 200 de retrimiteri → 0 rânduri noi; o citire nouă trimisă de 40 de ori în paralel → un singur rând | `scripts/demo/duplicate-reading.sh` |
| Un request, de la gateway până în stocare, după `X-Trace-Id` | `scripts/demo/trace-one-request.sh` |
| Alertă livrată când o garanție e în risc | `scripts/demo/alert-fire.sh` |
| Cel mai bun storage pe categorie, din latență p95 sau throughput | `scripts/benchmark/run.sh` |
| Zi normală, apoi vârf de citiri sau furtună de avarii | `scripts/scenarios/normal-day.sh`, apoi `peak-day.sh` sau `storm.sh` |
| Județ, anomalii de contor față de `sim_label` | tab-ul Analitice, județul ales |

`scripts/benchmark/run.sh` scrie aceeași sarcină mică pe postgres, s3 și ftp, postează timpii și pune politica la loc. Verdictul: p95 cel mai mic pentru citiri, avarii și documente; throughput cel mai mare pentru video. Tabelul arată stocarea politicii curente.

`normal-day.sh` umple bara de zi liniștită. `storm.sh` trimite de 10 ori mai multe avarii și scrie bara de furtună. `peak-day.sh` face același lucru pentru citiri; rulează după ziua normală, altfel suprascrie bara de furtună. În interfață, butonul „Zi normală vs. furtună” face varianta cu avarii: 3 avarii, apoi 30, restul categoriilor la volumul liniștit.

Detectorul de anomalii este pe praguri, aceleași în Python și în SQL: sub 50 V pană, sub 207 V gol de tensiune, peste 253 V supratensiune, eveniment `TAMPER_COVER_OPEN` sau `MAGNETIC_FIELD_DETECTED`, putere peste 3,5× mediana contorului. `narrative.network` spune câte etichete `sim_label` coincid cu detecția.

`GET /api/v1/analytics/summary` răspunde 200 și când Prometheus nu este pornit. Când `PROMETHEUS_URL` răspunde, completează tabelul din metricile `filegrid_request_duration_seconds`, `filegrid_bytes_total` și `filegrid_requests_total`.

## Utilizare

1. `https://localhost`, parola afișată la pornire.
2. Încărcare din `demo-files/`: avarie, document de racordare, MP4. Alegeți județul. Sub 5 MB merge la `/ingest`. Restul, și orice video, merge pe bucăți, cu reluare.
3. Fișiere: filtru pe categorie, stocare, nume, județ sau trace id. Descărcarea aduce `X-Integrity-Verified` și `X-File-Sha256`. Lista implicită omite citirile de contor; se văd cu categoria `meter_readings`. Auditul este pe fiecare fișier.
4. Politici: admin salvează, fără restart. Aici se setează și stocarea de rezervă.
5. Analitice: alegeți județul. Barele de zi normală și furtună apar după scenariu sau după butonul din Testing.
6. Testing (admin): furtună de avarii, inundație de avarii, comparație de stocări, duplicate, generator sintetic. Generatorul rămâne oprit până îl porniți din panou.
7. Chaos (admin): stocare oprită sau lentă, rețea proastă. Defectele expiră singure.
8. Grafana: `https://localhost/grafana/`.

Simulatorul pornește odată cu stiva. Control:

```
scripts/sim.sh status
scripts/sim.sh mode storm
scripts/sim.sh mode normal
scripts/sim.sh pause
scripts/sim.sh resume
```

## Limite

Categoriile din interfață nu intră în outbox. Fără `fallback_storage`, o stocare căzută răspunde 503 pentru fișierele noi din categoria ei; celelalte categorii continuă, iar fișierele deja confirmate rămân. Citirile de contor intră în outbox și fără rezervă.

Backlog-ul simulatorului este în memorie: supraviețuiește unei pene FileGrid, nu repornirii containerului simulator.

Upload-ul pe bucăți are nevoie de MinIO chiar dacă politica finală este FTP sau PostgreSQL. Sesiunile abandonate se șterg după 24 h fără activitate.

Benchmark-ul folosește fișiere de demo (implicit 64 KB pentru avarii, 1 MB pentru video), nu un video de 1 GB. `docker compose up -d --scale backend=3` crește debitul de aproximativ două ori, nu de trei ori: nginx, MinIO și discul rămân o singură instanță. Kubernetes nu include Grafana. Nu există rotație automată a cheii master.
