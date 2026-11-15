import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle,
  Database,
  ExternalLink,
  Gauge,
  Power,
  RefreshCw,
  Wifi,
  WifiOff,
} from 'lucide-react'
import {
  breakStorage,
  clearChaos,
  getChaosStatus,
  healStorage,
  setNetwork,
  type ChaosFault,
  type ChaosStatus,
} from '../services/api'
import type { ToastMessage } from '../components/Toast'

const STORAGES = [
  { id: 'postgres', label: 'PostgreSQL', note: 'bază de date (blob)', color: 'text-grid-postgres' },
  { id: 's3', label: 'S3 (MinIO)', note: 'obiecte', color: 'text-grid-s3' },
  { id: 'ftp', label: 'FTP', note: 'fișiere', color: 'text-grid-ftp' },
]

const DURATIONS = [30, 60, 180, 300]
const POLL_MS = 2000

const CIRCUIT: Record<string, { label: string; className: string }> = {
  closed: { label: 'Funcționează', className: 'border-grid-green/25 bg-grid-green/10 text-grid-green' },
  half_open: { label: 'Se reverifică', className: 'border-grid-amber/30 bg-grid-amber/10 text-grid-amber' },
  open: { label: 'Circuit deschis', className: 'border-grid-red/30 bg-grid-red/10 text-grid-red' },
}

interface ChaosPageProps {
  pushToast: (t: Omit<ToastMessage, 'id'>) => void
}

export function ChaosPage({ pushToast }: ChaosPageProps) {
  const [status, setStatus] = useState<ChaosStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [duration, setDuration] = useState(60)

  const refresh = useCallback(async () => {
    try {
      setStatus(await getChaosStatus())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [refresh])

  async function act(key: string, doneTitle: string, doneDetail: string, fn: () => Promise<void>) {
    setBusy(key)
    try {
      await fn()
      pushToast({ kind: 'success', title: doneTitle, detail: doneDetail })
      await refresh()
    } catch (err) {
      pushToast({
        kind: 'error',
        title: 'Acțiune eșuată',
        detail: err instanceof Error ? err.message : String(err),
      })
    } finally {
      setBusy(null)
    }
  }

  if (error && !status) {
    return (
      <div className="rounded-xl border border-grid-red/30 bg-grid-red/10 px-4 py-3 text-sm text-grid-red">
        Consola Chaos nu poate fi încărcată: {error}
      </div>
    )
  }
  if (!status) {
    return (
      <div className="flex items-center gap-2 py-16 text-grid-muted">
        <RefreshCw className="h-4 w-4 animate-spin" />
        Se încarcă…
      </div>
    )
  }

  if (!status.enabled) return <DisabledCard />

  const max = status.max_seconds
  const durations = DURATIONS.filter((d) => d <= max)
  const faultOf = (target: string): ChaosFault | undefined =>
    status.faults?.find((f) => f.target === target)
  const anyActive = (status.faults?.length ?? 0) > 0 || status.network_degraded === true
  const networkFault = faultOf('network')

  return (
    <div className="animate-fade-in space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-grid-text">Consola Chaos</h2>
          <p className="mt-1 max-w-2xl text-sm text-grid-muted">
            Stricați lucruri intenționat și urmăriți cum se comportă sistemul. Defecțiunile sunt
            simulate în aplicație, expiră singure și fiecare folosire este înregistrată în audit.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <a
            href="/grafana/"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-2 rounded-lg border border-grid-border px-3 py-2.5 text-sm text-grid-muted transition hover:border-grid-cyan/40 hover:text-grid-cyan"
          >
            <ExternalLink className="h-4 w-4" />
            Grafana
          </a>
          <button
            type="button"
            disabled={busy !== null || !anyActive}
            onClick={() =>
              void act('all', 'Totul a fost oprit', 'Toate defecțiunile simulate au fost anulate.', clearChaos)
            }
            className="inline-flex items-center gap-2 rounded-lg bg-grid-red px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-red-500 disabled:opacity-40"
          >
            <Power className="h-4 w-4" />
            Oprește tot
          </button>
        </div>
      </div>

      <div className="flex items-start gap-2 rounded-xl border border-grid-amber/30 bg-grid-amber/10 px-4 py-3 text-sm text-grid-amber">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        <span>
          Cât timp un storage este &quot;oprit&quot;, încărcările către el primesc 503, iar citirile
          de contor se pun în așteptare și sunt livrate la revenire. Celelalte categorii continuă
          să funcționeze.
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-sm text-grid-muted">
        <label htmlFor="chaos-duration">Durata unei defecțiuni:</label>
        <select
          id="chaos-duration"
          value={duration}
          onChange={(e) => setDuration(Number(e.target.value))}
          className="rounded-lg border border-grid-border bg-grid-bg px-3 py-1.5 text-sm text-grid-text outline-none focus:ring-2 focus:ring-grid-blue"
        >
          {durations.map((d) => (
            <option key={d} value={d}>
              {d >= 60 ? `${d / 60} min` : `${d} s`}
            </option>
          ))}
        </select>
        <span className="text-[11px]">(expiră singură; maxim {max >= 60 ? `${max / 60} min` : `${max} s`})</span>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        {STORAGES.map((s) => {
          const fault = faultOf(s.id)
          const circuit = CIRCUIT[status.storages?.[s.id]?.circuit ?? 'closed'] ?? CIRCUIT.closed
          return (
            <div key={s.id} className="rounded-xl border border-grid-border bg-grid-surface p-5">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <h3 className={`flex items-center gap-2 text-sm font-semibold ${s.color}`}>
                    <Database className="h-4 w-4" />
                    {s.label}
                  </h3>
                  <p className="font-mono text-[11px] text-grid-muted">
                    {s.id} · {s.note}
                  </p>
                </div>
                <span className={`rounded-md border px-2 py-0.5 text-[11px] font-medium ${circuit.className}`}>
                  {circuit.label}
                </span>
              </div>

              <div className="mt-4 min-h-[2.5rem] text-sm">
                {fault ? (
                  <p className="text-grid-red">
                    {fault.kind === 'down'
                      ? 'Oprit (simulat)'
                      : `Încetinit (+${fault.seconds} s pe operație)`}{' '}
                    <span className="text-grid-muted">
                      · mai sunt {Math.max(0, Math.round(fault.remaining_seconds))} s · de {fault.created_by}
                    </span>
                  </p>
                ) : (
                  <p className="text-grid-muted">Fără defecțiuni.</p>
                )}
              </div>

              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() =>
                    void act(
                      `${s.id}-down`,
                      `${s.label} oprit`,
                      `Simulat, pentru ${duration} s. Încercați o încărcare acum.`,
                      () => breakStorage(s.id, 'down', duration),
                    )
                  }
                  className="inline-flex items-center gap-1.5 rounded-lg bg-grid-red/90 px-3 py-2 text-xs font-semibold text-white transition hover:bg-grid-red disabled:opacity-40"
                >
                  <Power className="h-3.5 w-3.5" />
                  Oprește
                </button>
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() =>
                    void act(
                      `${s.id}-slow`,
                      `${s.label} încetinit`,
                      `+2 s pe fiecare operație, pentru ${duration} s.`,
                      () => breakStorage(s.id, 'slow', duration, 2),
                    )
                  }
                  className="inline-flex items-center gap-1.5 rounded-lg border border-grid-amber/40 px-3 py-2 text-xs font-semibold text-grid-amber transition hover:bg-grid-amber/10 disabled:opacity-40"
                >
                  <Gauge className="h-3.5 w-3.5" />
                  Încetinește
                </button>
                <button
                  type="button"
                  disabled={busy !== null || !fault}
                  onClick={() =>
                    void act(`${s.id}-heal`, `${s.label} reparat`, 'Defecțiunea simulată a fost oprită.', () =>
                      healStorage(s.id),
                    )
                  }
                  className="inline-flex items-center gap-1.5 rounded-lg border border-grid-border px-3 py-2 text-xs text-grid-muted transition hover:border-grid-green/40 hover:text-grid-green disabled:opacity-40"
                >
                  Repară
                </button>
              </div>
            </div>
          )
        })}
      </div>

      <div className="rounded-xl border border-grid-border bg-grid-surface p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="flex items-center gap-2 text-sm font-semibold text-grid-text">
              {status.network_degraded ? (
                <WifiOff className="h-4 w-4 text-grid-red" />
              ) : (
                <Wifi className="h-4 w-4 text-grid-green" />
              )}
              Rețea între gateway și backend
            </h3>
            <p className="mt-1 max-w-2xl text-[13px] text-grid-muted">
              Degradată: latență 400±200 ms pe sens, încărcare limitată la 2 MB/s și 20% din
              conexiuni întrerupte. Afectează și această consolă (cererile ei trec pe același drum).
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy !== null}
              onClick={() =>
                void act('net-bad', 'Rețea degradată', `Pentru ${duration} s.`, () => setNetwork('bad', duration))
              }
              className="inline-flex items-center gap-1.5 rounded-lg bg-grid-red/90 px-3 py-2 text-xs font-semibold text-white transition hover:bg-grid-red disabled:opacity-40"
            >
              <WifiOff className="h-3.5 w-3.5" />
              Degradează
            </button>
            <button
              type="button"
              disabled={busy !== null || status.network_degraded !== true}
              onClick={() => void act('net-ok', 'Rețea restabilită', 'Revenire la normal.', () => setNetwork('ok'))}
              className="rounded-lg border border-grid-border px-3 py-2 text-xs text-grid-muted transition hover:border-grid-green/40 hover:text-grid-green disabled:opacity-40"
            >
              Restabilește
            </button>
          </div>
        </div>
        <p className="mt-3 text-sm">
          {status.network_degraded === null ? (
            <span className="text-grid-amber">Starea rețelei nu poate fi citită (toxiproxy indisponibil).</span>
          ) : status.network_degraded ? (
            <span className="text-grid-red">
              Degradată
              {networkFault ? ` · mai sunt ${Math.max(0, Math.round(networkFault.remaining_seconds))} s` : ''}
            </span>
          ) : (
            <span className="text-grid-muted">Normală.</span>
          )}
        </p>
      </div>

      <div className="rounded-xl border border-grid-border bg-grid-surface p-5">
        <h3 className="text-sm font-semibold text-grid-text">Ultimele acțiuni (din audit)</h3>
        {status.recent && status.recent.length > 0 ? (
          <ul className="mt-3 space-y-1.5">
            {status.recent.map((e) => (
              <li key={e.id} className="flex flex-wrap gap-x-3 text-[13px]">
                <span className="font-mono text-[11px] text-grid-muted">
                  {e.timestamp ? new Date(e.timestamp).toLocaleTimeString('ro-RO') : ''}
                </span>
                <span className="text-grid-muted">{e.user}</span>
                <span className="text-grid-text">{e.details}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-grid-muted">Nicio acțiune încă.</p>
        )}
      </div>
    </div>
  )
}

function DisabledCard() {
  return (
    <div className="animate-fade-in space-y-4">
      <h2 className="text-xl font-bold text-grid-text">Consola Chaos</h2>
      <div className="max-w-3xl rounded-xl border border-grid-border bg-grid-surface p-6">
        <p className="text-sm text-grid-text">
          Consola este <strong>oprită</strong>. Este oprită implicit, ca într-o instalare normală să
          nu existe nicio cale de a strica sistemul din interfață.
        </p>
        <p className="mt-3 text-sm text-grid-muted">Pentru a o porni (pe mașina de demonstrație):</p>
        <pre className="mt-2 overflow-x-auto rounded-lg border border-grid-border bg-grid-bg px-4 py-3 font-mono text-[12px] text-grid-cyan">
          CHAOS_ENABLED=true docker compose up -d backend{'\n'}docker compose restart toxiproxy
        </pre>
        <p className="mt-3 text-sm text-grid-muted">
          Apoi reîncărcați pagina. Doar administratorul vede această consolă; fiecare acțiune este
          auditată, iar orice defecțiune expiră singură.
        </p>
      </div>
    </div>
  )
}
