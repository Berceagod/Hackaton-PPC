import { useCallback, useEffect, useState } from 'react'
import { FlaskConical, Pause, Play, RefreshCw, X, Zap } from 'lucide-react'
import {
  getTestingStatus,
  setGenerator,
  setQuiet,
  simulatorAction,
  startTestJob,
  type TestingStatus,
} from '../services/api'
import type { ToastMessage } from './Toast'

const CATEGORIES = [
  { id: 'outage_records', label: 'Înregistrări de avarie' },
  { id: 'connection_docs', label: 'Documente de racordare' },
  { id: 'drone_videos', label: 'Video-uri dronă (200 KB)' },
]
const RATES = [0.5, 1, 2, 5]
const DURATIONS = [
  { s: 300, label: '5 min' },
  { s: 600, label: '10 min' },
  { s: 1800, label: '30 min' },
]
const JOB_HINT: Record<string, string> = {
  'storm-meters': 'Contoarele trimit de 10x mai mult timp de 30 s, apoi revin la normal.',
  'outage-flood': '60 de avarii în paralel; verifică și dacă alte categorii sunt afectate.',
  'compare-storages': '25 s de scrieri și citiri pe PostgreSQL, S3 și FTP deodată.',
  duplicates: 'Retrimite 100 de citiri deja stocate; toate trebuie respinse.',
  'day-vs-storm': 'Zi liniștită (3 avarii), apoi 30 de avarii deodată. Citirile, racordările și video-ul rămân la fel. Completează Secțiunea B.',
  'overload-flood':
    '240 de citiri exact în același moment: dacă storage-ul principal este depășit, surplusul merge pe cel de rezervă (setat în Politici).',
}
// where the effect of each button can be watched
const JOB_WHERE: Record<string, string> = {
  'storm-meters': 'Vezi: Grafana → Operations (cereri/s) și rezultatul de mai jos.',
  'outage-flood': 'Vezi: rezultatul de mai jos și Grafana → Operations (latență, 503).',
  'compare-storages': 'Vezi: Analytics → „Latență live pe storage” (sus) și rezultatul de mai jos.',
  duplicates: 'Vezi: rezultatul de mai jos și contorul „duplicate” din Grafana.',
  'day-vs-storm': 'Vezi: Analytics → Secțiunea B (reîncărcați pagina).',
  'overload-flood': 'Vezi: clopoțelul „Alerte” (failover) și rezultatul de mai jos, cu câte citiri au mers pe fiecare storage.',
}

interface Props {
  pushToast: (t: Omit<ToastMessage, 'id'>) => void
}

/** Admin-only floating panel, on top of whatever tab is open. */
export function TestingOverlay({ pushToast }: Props) {
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<TestingStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [cats, setCats] = useState<string[]>(['outage_records', 'connection_docs'])
  const [rate, setRate] = useState(1)
  const [duration, setDuration] = useState(600)

  const refresh = useCallback(async () => {
    try {
      setStatus(await getTestingStatus())
    } catch {
      // a transient error must not break the page; the next poll retries
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), open ? 2000 : 5000)
    return () => window.clearInterval(timer)
  }, [refresh, open])

  async function act(doneTitle: string, fn: () => Promise<void>) {
    setBusy(true)
    try {
      await fn()
      pushToast({ kind: 'success', title: doneTitle })
      await refresh()
    } catch (err) {
      pushToast({
        kind: 'error',
        title: 'Acțiune eșuată',
        detail: err instanceof Error ? err.message : String(err),
      })
    } finally {
      setBusy(false)
    }
  }

  const gen = status?.generator
  const job = status?.job
  const running = job?.running === true
  const active = gen?.on === true || running

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="fixed bottom-4 right-4 z-50 inline-flex items-center gap-2 rounded-full border border-grid-cyan/40 bg-grid-panel px-4 py-2.5 text-sm font-semibold text-grid-cyan shadow-lg transition hover:bg-grid-surface"
      >
        <FlaskConical className="h-4 w-4" />
        Testing
        {active && <span className="h-2 w-2 animate-pulse rounded-full bg-grid-red" />}
      </button>

      {open && (
        <div className="fixed bottom-16 right-4 z-50 max-h-[80vh] w-[26rem] max-w-[calc(100vw-2rem)] overflow-y-auto rounded-xl border border-grid-border bg-grid-bg p-4 shadow-2xl scrollbar-thin">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="flex items-center gap-2 text-sm font-bold text-grid-text">
              <FlaskConical className="h-4 w-4 text-grid-cyan" />
              Testing și simulări
            </h3>
            <button type="button" onClick={() => setOpen(false)} className="text-grid-muted hover:text-grid-text">
              <X className="h-4 w-4" />
            </button>
          </div>

          {!status ? (
            <div className="flex items-center gap-2 py-6 text-grid-muted">
              <RefreshCw className="h-4 w-4 animate-spin" /> Se încarcă…
            </div>
          ) : !status.enabled ? (
            <div className="space-y-2 text-sm text-grid-muted">
              <p>
                Panoul este <strong className="text-grid-text">oprit</strong> implicit. Pentru a-l porni pe
                mașina de demonstrație:
              </p>
              <pre className="overflow-x-auto rounded-lg border border-grid-border bg-grid-surface px-3 py-2 font-mono text-[11px] text-grid-cyan">
                TESTING_ENABLED=true docker compose up -d backend
              </pre>
              <p>Apoi reîncărcați pagina.</p>
            </div>
          ) : (
            <div className="space-y-4">
              {/* generator */}
              <section className="rounded-lg border border-grid-border bg-grid-surface p-3">
                <div className="flex items-center justify-between">
                  <h4 className="text-[11px] font-semibold uppercase tracking-wider text-grid-muted">
                    Fișiere sintetice (alte categorii)
                  </h4>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={gen?.on === true}
                    disabled={busy || (!gen?.on && cats.length === 0)}
                    onClick={() =>
                      void act(gen?.on ? 'Generator oprit' : 'Generator pornit', () =>
                        setGenerator(!gen?.on, cats, rate, duration),
                      )
                    }
                    className={`relative h-6 w-11 rounded-full transition disabled:opacity-40 ${
                      gen?.on ? 'bg-grid-red' : 'bg-grid-border'
                    }`}
                  >
                    <span
                      className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition ${
                        gen?.on ? 'translate-x-5' : ''
                      }`}
                    />
                  </button>
                </div>
                <p className={`mt-1 text-[12px] ${gen?.on ? 'text-grid-red' : 'text-grid-muted'}`}>
                  {gen?.on
                    ? `PORNIT · ${gen.categories.join(', ')} · ${gen.rate}/s fiecare · se oprește singur în ${Math.round(gen.remaining_seconds)} s`
                    : 'Oprit. Pentru un test cu un fișier real, lăsați-l oprit: fișierul dvs. nu se pierde printre cele generate.'}
                </p>
                <div className="mt-2 space-y-1">
                  {CATEGORIES.map((c) => (
                    <label key={c.id} className="flex items-center gap-2 text-[13px] text-grid-text">
                      <input
                        type="checkbox"
                        disabled={gen?.on === true}
                        checked={cats.includes(c.id)}
                        onChange={(e) =>
                          setCats((prev) => (e.target.checked ? [...prev, c.id] : prev.filter((x) => x !== c.id)))
                        }
                      />
                      {c.label}
                    </label>
                  ))}
                </div>
                <div className="mt-2 flex gap-2 text-[12px] text-grid-muted">
                  <select
                    disabled={gen?.on === true}
                    value={rate}
                    onChange={(e) => setRate(Number(e.target.value))}
                    className="rounded border border-grid-border bg-grid-bg px-2 py-1 text-grid-text"
                  >
                    {RATES.map((r) => (
                      <option key={r} value={r}>
                        {r} fișier/s per categorie
                      </option>
                    ))}
                  </select>
                  <select
                    disabled={gen?.on === true}
                    value={duration}
                    onChange={(e) => setDuration(Number(e.target.value))}
                    className="rounded border border-grid-border bg-grid-bg px-2 py-1 text-grid-text"
                  >
                    {DURATIONS.map((d) => (
                      <option key={d.s} value={d.s}>
                        oprire automată: {d.label}
                      </option>
                    ))}
                  </select>
                </div>
                <p className="mt-2 text-[11px] text-grid-muted">
                  Fișierele generate se numesc <span className="font-mono">gen_*</span> și au descrierea
                  &quot;sintetic&quot;.
                </p>
              </section>

              {/* quiet mode */}
              <section className="rounded-lg border border-grid-border bg-grid-surface p-3">
                <div className="flex items-center justify-between">
                  <h4 className="text-[11px] font-semibold uppercase tracking-wider text-grid-muted">
                    Liniște (pauză verificări de fundal)
                  </h4>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={status.quiet?.on === true}
                    disabled={busy}
                    onClick={() =>
                      void act(status.quiet?.on ? 'Verificările rulează din nou' : 'Mod liniște pornit', () =>
                        setQuiet(!status.quiet?.on),
                      )
                    }
                    className={`relative h-6 w-11 rounded-full transition disabled:opacity-40 ${
                      status.quiet?.on ? 'bg-grid-amber' : 'bg-grid-border'
                    }`}
                  >
                    <span
                      className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition ${
                        status.quiet?.on ? 'translate-x-5' : ''
                      }`}
                    />
                  </button>
                </div>
                <p className={`mt-1 text-[12px] ${status.quiet?.on ? 'text-grid-amber' : 'text-grid-muted'}`}>
                  {status.quiet?.on
                    ? `ACTIV · canarul și scrubber-ul sunt oprite, graficele arată doar traficul dvs. · revine singur în ${Math.round(status.quiet.remaining_seconds)} s`
                    : 'Oprit. Canarul (test pe fiecare storage, 1/min) și scrubber-ul (re-verifică fișiere) trimit un trafic mic de fundal. Porniți liniștea pentru o demonstrație curată (30 min, apoi revine singur).'}
                </p>
              </section>

              {/* meters */}
              <section className="rounded-lg border border-grid-border bg-grid-surface p-3">
                <h4 className="text-[11px] font-semibold uppercase tracking-wider text-grid-muted">
                  Simulator contoare
                </h4>
                <p className="mt-1 text-[12px] text-grid-muted">
                  {status.simulator
                    ? `${status.simulator.mode}${status.simulator.paused ? ' (în pauză)' : ''} · țintă ${status.simulator.target_rate}/s · ${status.simulator.meters} contoare · coadă ${status.simulator.backlog}`
                    : 'Simulatorul nu răspunde.'}
                </p>
                <div className="mt-2 flex flex-wrap gap-2">
                  <SmallButton disabled={busy} onClick={() => void act('Furtună pornită', () => simulatorAction('storm'))}>
                    <Zap className="h-3.5 w-3.5" /> Furtună 10x
                  </SmallButton>
                  <SmallButton disabled={busy} onClick={() => void act('Mod normal', () => simulatorAction('normal'))}>
                    Normal
                  </SmallButton>
                  <SmallButton disabled={busy} onClick={() => void act('Simulator în pauză', () => simulatorAction('pause'))}>
                    <Pause className="h-3.5 w-3.5" /> Pauză
                  </SmallButton>
                  <SmallButton disabled={busy} onClick={() => void act('Simulator reluat', () => simulatorAction('resume'))}>
                    <Play className="h-3.5 w-3.5" /> Reia
                  </SmallButton>
                </div>
              </section>

              {/* one-press simulations */}
              <section className="rounded-lg border border-grid-border bg-grid-surface p-3">
                <h4 className="text-[11px] font-semibold uppercase tracking-wider text-grid-muted">
                  Simulări dintr-un click
                </h4>
                <div className="mt-2 space-y-2">
                  {Object.entries(status.jobs ?? {}).map(([id, label]) => (
                    <div key={id} className="flex items-start justify-between gap-2">
                      <div>
                        <div className="text-[13px] text-grid-text">{label}</div>
                        <div className="text-[11px] text-grid-muted">{JOB_HINT[id]}</div>
                        <div className="text-[11px] text-grid-cyan/80">{JOB_WHERE[id]}</div>
                      </div>
                      <SmallButton
                        disabled={busy || running}
                        onClick={() => void act(`${label} pornită`, () => startTestJob(id))}
                      >
                        Pornește
                      </SmallButton>
                    </div>
                  ))}
                </div>
                {job && (
                  <div
                    className={`mt-3 rounded-lg border px-3 py-2 text-[12px] ${
                      job.error
                        ? 'border-grid-red/30 bg-grid-red/10 text-grid-red'
                        : running
                          ? 'border-grid-amber/30 bg-grid-amber/10 text-grid-amber'
                          : 'border-grid-green/25 bg-grid-green/10 text-grid-green'
                    }`}
                  >
                    {running ? (
                      <span className="inline-flex items-center gap-2">
                        <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                        Rulează: {status.jobs?.[job.name] ?? job.name}…
                      </span>
                    ) : (
                      (job.error ?? job.summary)
                    )}
                  </div>
                )}
              </section>
            </div>
          )}
        </div>
      )}
    </>
  )
}

function SmallButton({
  children,
  onClick,
  disabled,
}: {
  children: React.ReactNode
  onClick: () => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-lg border border-grid-border px-3 py-1.5 text-xs font-medium text-grid-text transition hover:border-grid-cyan/40 hover:text-grid-cyan disabled:opacity-40"
    >
      {children}
    </button>
  )
}
