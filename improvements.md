# improvements.md: how FileGrid can stand out, and how to make the UI look designed by a person

Written after slices A to H were merged. It is a menu, not a promise: pick by **demo impact per hour of work**.
House rule that still applies: **a claim only counts if a script proves it** (the project is graded on a live demo). Every idea says what the proof would be.

Legend: **Impact** ★ (nice) to ★★★ (judges will remember it) · **Effort** S (hours), M (a day), L (several days) · 🆕 not built yet · ✅ already built, only needs polish.

---

## 0. What we already have that most teams will not
Worth saying out loud in the pitch, because it is rare:
- ✅ Per-category policy with hot reload, three real separate storages, SHA-256 proof on every read, corrupt file refused (409).
- ✅ Exactly-once meter readings (UNIQUE constraint + claim/commit, race-tested), an outbox so a dead storage loses nothing, circuit breakers and bulkheads per category.
- ✅ AES-256-GCM envelope encryption for personal data, storages unreachable from outside, secrets generated at first start, gitleaks.
- ✅ Resumable chunked upload, flat memory on 1 GB, toxiproxy for bad networks.
- ✅ Metrics per category and storage, one trace id from gateway to storage, 17 alerts tested with promtool and fired for real, dashboards as code.
- ✅ A script for every claim, ADRs for every decision, and a "known gaps" list. Honesty is a feature.

The gap to close is **presentation and the "wow" moments**. Today the proof lives in terminal scripts; judges remember what they can *see*.

---

## 1. The five things to do first

| # | Feature | Impact | Effort | Why it wins |
|---|---|---|---|---|
| 1 | 🆕 **Chaos console + live system view** (2.1) | ★★★ | M | Turns every non-functional claim into something visible in 60 seconds, on one screen |
| 2 | 🆕 **Self-healing storage: replicas + background scrubber** (2.2) | ★★★ | M | "Corrupt a byte, and the system notices and repairs it" beats "it refuses to serve it" |
| 3 | 🆕 **Per-file journey view and signed integrity receipt** (2.3) | ★★★ | M | Makes SHA-256, trace ids and audit tangible for a non-technical judge |
| 4 | 🆕 **Per-meter identity instead of one shared token** (2.5) | ★★ | M | Answers the obvious security question: "what if the simulator token leaks?" |
| 5 | 🆕 **The UI remake** (section 4), as a "dispatcher control room", not a SaaS dashboard | ★★★ | L | First impression, and it removes the "AI-generated" look |

If time is short, do 1, 2 and 3 first, then the UI, then 4.

---

## 2. Features that make the demo memorable

### 2.1 Chaos console and live topology (the "watch it survive" screen): ★★★ / M
An admin-only page: a diagram of nginx, backend replicas, postgres-blob, S3, FTP and the outbox, drawn live from Prometheus, with buttons:
**Kill FTP · Corrupt a byte in S3 · Bad network (latency and loss) · 10x storm on outage records · Add a backend replica.**
Each button calls a guarded endpoint or a small sidecar. The diagram edges turn amber or red, breakers open, the outbox counter climbs, the other categories' latency stays flat, then everything recovers.
- Proof: `scripts/demo/chaos-run.sh` performs the same sequence headlessly and asserts the invariants (no confirmed file lost, other categories' p95 within a set limit).
- Guard it: only when `CHAOS_ENABLED=true`, only for `admin`, every use audited. Never on in a default install.

### 2.2 Self-healing: replicas and a scrubber: ★★★ / M
- Policy gets `replicas: 1` or `2`, with the second copy on a *different* storage (for example S3 primary, Postgres secondary).
- A background **scrubber** re-verifies files at a set rate (metrics `filegrid_scrub_files_total`, `filegrid_repairs_total`). On a mismatch it copies the good replica over the bad one and writes an audit entry `repaired`.
- Downloading a corrupt primary transparently serves the good replica, still with `X-Integrity-Verified: true` plus `X-Repaired: true`.
- Proof: `scripts/demo/self-heal.sh`: flip a byte, wait for the scrubber, show the audit entry and a clean download. Alert `FileRepaired` (info) instead of paging.
- Watch the cost: replicas double the write cost. Record it in an ADR and make it per category (videos: 1 copy; outage records: 2).

### 2.3 The file's journey and a signed receipt: ★★★ / M
- **Journey view** on the Files page: click a file and see a vertical timeline built from its trace id: received at gateway (+0 ms), authenticated, hashed, written to S3 (130 ms), metadata committed, acknowledged, then later downloads and verifications. The data already exists (trace spans in Loki, audit table); this is a UI over it.
- **Integrity receipt**: a downloadable JSON or PDF with SHA-256, size, storage, timestamps and trace id, **signed** (Ed25519 key in `.env`), plus a public `GET /verify-receipt` page. "FileGrid can *prove* it" becomes a document a client keeps.
- **Tamper-evident audit log**: each `audit_logs` row stores a `prev_hash`; `GET /audit/verify` recomputes the chain. Edit one row by hand in the database and verification fails. Today the audit is a plain table, so anyone with database access can rewrite history, which weakens "auditable".
- Proof: `scripts/demo/audit-tamper.sh`.

### 2.4 A smarter policy engine: ★★ / M
- **Rules, not just per category**: route by size and metadata, for example `drone_videos > 1 GB → S3 multipart`, `zone: Banat → FTP`, `age > 30 days → cold tier`. Still data, no code.
- **Dry run**: "what would this policy do to the last 1000 uploads?" before saving (cost, latency estimate, which storages change).
- **History, rollback and diff** of policies (`policies_config` already has `updated_at`). **Four-eyes approval** when a change would turn encryption off for personal data.
- **Recommender**: slice I's benchmark gives a verdict per category; add a button "apply recommended policy". This closes the loop from statistics to decision, which is what brief section 5 asks for.

### 2.5 Security beyond the checklist: ★★ / M
- **Per-meter credentials** (mTLS with device certificates, or per-meter HMAC keys), so one leaked secret cannot impersonate every meter and one meter can be revoked. Today the simulator uses one shared token: least-privilege `service` role, but still shared.
- **Crypto-shredding for erasure**: today each file's key is derived from the file id and the master key. Use a **random per-file key wrapped by the master key** instead. Erasing a person's document then means deleting its wrapped key, which makes the ciphertext unreadable everywhere, including backups. It also allows **master-key rotation** without re-encrypting files. Proof: `scripts/demo/erase-document.sh`.
- **Upload hygiene**: ClamAV scanning for `connection_docs` and drone videos, strict content-type sniffing, size limits per category.
- **Signed, short-lived download links**, and an `auditor` role (reads audit trails, cannot download documents).
- **Login hardening** beyond the nginx rate limit: lockout with backoff, and optional TOTP for `admin`.
- A short **threat model** page (assets, attackers, mitigations, what is *not* covered). It is cheap, and judges read it.

### 2.6 Meter fleet intelligence (the statistics part of the brief): ★★★ / M
Slice I builds the summary endpoint. These make it useful to a decision-maker:
- **Fleet view**: per-meter last seen, silent meters (expected N readings a minute, got 0), data completeness per zone. "Meter RO-DOB-000645 silent for 12 minutes" is an action; a chart is not.
- **Anomaly detection scored against `sim_label`**: show precision and recall per anomaly type (voltage sag, overvoltage, load spike, tamper, outage). A confusion matrix is something almost no team will have.
- **Zone view**: a simple schematic of Muntenia, Banat and Dobrogea with outages, anomalies and drone inspection status.
- **A "so what" sentence under every chart** (already required by the brief), generated from the numbers, not from a template.
- **Forecast**: storage growth per category, and the day a volume fills up.
- **Cost and capacity view**: GB per day per category, cost per GB per storage from the benchmark, projected monthly cost for the chosen policy against the alternatives.

### 2.7 Resilience you can watch: ★★ / M
- **Sender-side durability**: the simulator drops readings once its small backlog is full. A small meter-gateway agent with a local store-and-forward queue (SQLite) would show the whole chain is loss-free through a long outage. Needs the simulator owner's agreement.
- **Zero-downtime rolling deploy**: rebuild a backend replica while 100 readings a second flow; assert zero failed and zero duplicated.
- **Backup and restore drill**: `scripts/demo/restore-drill.sh` deletes a volume, restores from backup, and prints the measured RPO and RTO. Teams rarely test restore.
- **Autoscaling on a real signal** (slice J): scale on queue depth or p95 rather than CPU only, and show the replicas moving during a storm.

### 2.8 Observability polish: ★★ / S to M
- **SLOs and error budgets** per guarantee (for example "outage records saved in under 2 s, 99.5 %"), with burn-rate alerts and one stat per SLO on the dashboard.
- **Synthetic canary**: upload, download and verify a tiny file every minute; alert if it fails. It catches what metrics cannot.
- **Runbook link in every alert** (`annotations.runbook`) pointing at a short `docs/runbooks/*.md`: what to check, in order.
- **A real notification channel** (Telegram, Slack or Teams) behind the webhook, so the demo ends with a phone buzzing.
- A **public read-only status page** generated from the same Prometheus data.

### 2.9 Operations and developer experience: ★★ / S
- **`scripts/demo/run-all.sh`**: the full scripted presentation with narration and pauses, in the order of the PDF checklist. A rehearsed demo is a stronger demo.
- **Demo seed**: realistic synthetic data in the UI from the first second.
- **Pre-built images**: MinIO is currently built from source on first start (about 2 minutes, and it depends on an upstream repository staying available). Publish our own image, or use an S3 server that still ships images (the notes name SeaweedFS as the fallback). A clean-machine start in about 2 minutes is a selling point.
- **Trusted local TLS** (mkcert or a local CA), so the browser opens `https://localhost` without a warning. The "Privacy error" page is the first thing a judge sees.
- **A committed load-test report** (k6 or Locust): throughput and latency per category, with the flat-memory graph for the 1 GB upload beside it.

### 2.10 Product touches: ★ / S to M
- **Romanian and English switch**: the data is Romanian, the judges may not be.
- **Keyboard first**: command palette (`Ctrl+K`: "go to files", "verify file", "switch policy"), `/` focuses search.
- **Bulk actions** on files (verify selected, export audit as CSV or PDF), **file preview** (first page of a PDF, video poster), **saved searches**.
- **Accessibility pass** (contrast, focus rings, screen-reader labels, reduced motion). Cheap, and a thoughtful reviewer will notice.

---

## 3. What could embarrass us in the demo
Fix these before adding features. Ordered by how likely a judge is to see them:
1. **Browser certificate warning** on first open (self-signed). Fix: trusted local CA (2.9).
2. **Slow first start** (MinIO source build, image pulls). Fix: pre-built images, and a prewarm note in the README.
3. **The start message does not show the Grafana URL.** Fix: one line in `start.sh` and `start.ps1` (noted in `docs/slices/H.md`).
4. **Anything that depends on the simulator's own metrics** (`sim_*`) looks broken when the simulator is stopped. Say so on the dashboard, or fall back to FileGrid's own counters.
5. **An alert that stays red after a demo** (`FileIntegrityFailure` looks back 5 minutes). Know it, or show the "resolved" notification on purpose.
6. **Resource use** (about 15 containers, around 2 GB of RAM). Test on an 8 GB laptop at projector resolution (1366×768 or 1920×1080).
7. **The Analytics tab on real data** (slice I). Rehearse with a populated database, not an empty one.

---

## 4. Remaking the UI so it does not look AI-generated

### 4.1 Why it reads as "Claude-coded" today
This is not a criticism of the work. It is the recognisable default look of generated front ends:
- **Palette**: deep navy `#0b1220` with cyan and blue accents plus amber, and **soft radial colour glows** in the page background. This "dark SaaS" look is everywhere.
- **Type**: IBM Plex Sans and Mono, with tiny all-caps, letter-spaced labels above every field.
- **Shapes**: every surface is a rounded card with a 1 px border; uniform, symmetrical grids; every status is a pill badge; an icon in a coloured square beside a bold title and a grey subtitle.
- **Ornament**: fade-in on everything, a pulsing green dot ("API Online"), frosted-glass blur, gradient buttons, toasts bottom-right.
- **Copy**: tidy but generic ("Hot-Reload", "Confirmare ingestie"); a headline and a one-line subtitle on every page.
- **Layout logic**: pages are lists of equally weighted cards, not one clear work surface per task.

None of this is wrong. The problem is that nothing in it is about electricity, dispatching, or files that matter. A human designer starts from the domain.

### 4.2 Direction: a dispatcher's control room, not a SaaS dashboard
Pick one idea and carry it everywhere. Suggestion:

**"Single-line diagram" language.** Power-grid people read single-line diagrams: thin lines, nodes, breakers, labels in a technical face. Make the app feel like the tools a distribution operator already trusts.
- **Navigation as a schematic**: a left rail with the four categories as nodes on a line, leading to storage "busbars" (Postgres, S3, FTP). The current policy is the *wiring*: changing a policy visibly re-routes a line.
- **ISA-101 style colour discipline** (the high-performance HMI standard): neutral grey surfaces, **colour only for abnormal states** (a breaker open, an integrity failure, a stalled upload). When everything is normal the screen is calm; when something is wrong it is obvious. This is the opposite of a rainbow of badges.
- **Density over decoration**: real tables with tabular figures, aligned units, sticky headers, row hover, inline actions. Operators prefer information-rich screens to large airy cards.
- **Light theme as the default** (an office desk, a projector), with a proper dark mode for a control-room setting. The current dark-only look is the loudest "generated" signal.

Two other directions, if the schematic idea does not appeal:
- *Ledger or records office*: off-white paper, serif headings, seals for verified files, monospaced hashes. Fits the proof-and-audit story.
- *Railway timetable*: a strict grid, one grotesque typeface, heavy rules, almost no colour. Very recognisably human-designed.

### 4.3 Concrete design rules (write these into a short style guide first)
1. **Tokens, not defaults.** A `tokens.css` with a 4 or 8 px spacing scale, a type scale of six sizes, **at most two corner radii**, one border colour, one signal colour and three semantic colours. Stop using Tailwind's default radii and shadows.
2. **Typography with a voice.** Replace Plex everywhere with a deliberate pair: a characterful sans for UI and headings (for example Bricolage Grotesque, Instrument Sans, Familjen Grotesk or Geist) and a mono for hashes and ids. **Check that ș and ț (comma below) and ă, â, î render correctly.** Many fonts silently fall back to ş and ţ (cedilla), which looks sloppy to a Romanian reader. Use tabular figures for every number.
3. **Remove the tells**: background glows, frosted glass, gradient buttons, pulsing dots, fade-in on every element, icon-in-coloured-square, pill badges for everything. Keep motion only where it carries information.
4. **Motion with meaning**: a chunk grid that fills cell by cell during a drone-video upload; a seal that stamps **only after** the integrity check has passed; a line that re-routes when a policy changes; counters that tick. Nothing animates just because it can.
5. **Break the symmetry**: one dominant region per page (the file table, the upload surface), narrower secondary panels, deliberate whitespace instead of equal cards.
6. **Hashes and ids are first-class**: grouped monospace (`97ea 3d6c 89b0 f056…`), a copy button, a "verify" action, and the trace id linking to the journey view.
7. **Real microcopy**: write the Romanian operational text by hand ("Verificat byte cu byte la 14:02, S3, 38 ms"), with units and timestamps. Empty states say what to do next. Errors say what happened and whether it is safe.
8. **Show the truth of the system**: live storage topology, per-file journey, outbox counter, "last verified" on every file. Expose what makes FileGrid special instead of hiding it behind generic cards.
9. **Keyboard and projector first**: `Ctrl+K`, visible focus rings, body text at least 16 px, contrast that survives a projector.

### 4.4 Process (this is what actually removes the generated look)
Generated UIs look generated because the first draft gets shipped. Do the opposite:
1. **Collect 6 to 8 references** from real operational software (SCADA and outage-management screens, network-operations tools, trading terminals, a railway timetable, a good e-ledger). **Write down what you like about each, in words,** before touching code.
2. **Design three screens** on paper or in Figma or Penpot (Upload, Files with the journey, Policies), using **real data** (real file names, real hashes, real Romanian text) at 1366×768.
3. **Decide the tokens (4.3.1) and the type pair once**, then implement. No generator picks the palette.
4. **Build the component set by hand** on unstyled primitives (Radix or Headless UI). Avoid default shadcn/ui styling: it is itself recognisable.
5. **Review with a person** who did not build it (a designer friend, a teammate) with one question: "what does this remind you of?" If the answer is "every dashboard", iterate.
6. **Keep the API contract and the logic** as they are. The remake is a re-skin and re-layout of the four tabs, plus the new views (journey, topology, chaos console).

### 4.5 The "AI smell" checklist (use it as a review gate)
- [ ] Is the background a plain colour, with no gradients or glows?
- [ ] At most two corner radii in the whole app?
- [ ] Is colour reserved for state, plus one accent?
- [ ] Are status chips used only where a state really must be scanned?
- [ ] Fewer than about three animated elements on screen at once?
- [ ] Does every number use tabular figures and show its unit?
- [ ] Does Romanian text render ș and ț correctly everywhere?
- [ ] Does each page have one obvious primary surface?
- [ ] Is the copy written for this domain, not generic?
- [ ] Can a stranger tell this is for *power distribution*, not "any SaaS"?

### 4.6 Practical notes
- **Ownership**: under the parallel-slice rules, everything in `frontend/` belongs to slice I until it is merged. Start the remake after slice I merges, or agree the file split with its owner first, to avoid conflicts in `api.ts` and `filegrid.ts`.
- **Do not break what works**: the integrity toast, the 409 corruption alert, chunked upload with retries and the audit drawer are already correct. Keep the behaviour, change the skin.
- **Suggested phases**: (0) references and three paper screens; (1) tokens, type, app shell and navigation; (2) Files and Upload; (3) Policies and Analytics; (4) journey view and chaos console; (5) accessibility, Romanian copy and a projector test.
- **Before and after**: keep screenshots of each screen in `docs/ui/` to show the progression, and for the final presentation.

---

## 5. Suggested plan for the time left

| Block | Work | Result you can show |
|---|---|---|
| 1 | Fix the section 3 items (trusted TLS, pre-built images, start message) | A clean first minute of the demo |
| 2 | Chaos console and topology (2.1) | One screen that proves scale, resilience and observability |
| 3 | Replicas and scrubber (2.2), audit hash chain (2.3) | "It heals itself" and "the audit cannot be rewritten" |
| 4 | Journey view and signed receipt (2.3) | Integrity and tracing made visible |
| 5 | Fleet intelligence with precision and recall (2.6, with slice I) | A decision-ready statistics page |
| 6 | UI remake (section 4) | A product that does not look generated |
| 7 | Rehearsal: `run-all.sh`, projector test, 8 GB laptop test | A demo that does not surprise you |

If only two things get done: **2.1 (chaos console) and the UI remake**. One makes the system's strengths visible; the other makes the first impression.


+

la partea de analytics sa fie totul updatat continuu 
la sectiunea b de exemplu sa fie in grafic continuu in care sa arate latenta intr o zi normala care e sub un 
    anumit threshhold vs furtuna care e peste threshhold astelf incat sa nu trebuiasca sa rulam cele 2 scripturi de stomr/zi normala.sh si sa se trimita fisiere constant ca sa se creeze un flux de date care sa simuleze o aplicatie reala.
