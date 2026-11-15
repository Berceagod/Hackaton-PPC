import { useEffect, useRef, useState } from 'react'
import { Bell, CheckCircle2 } from 'lucide-react'
import { getAlerts, type AlertsResponse, type AppAlert } from '../services/api'

const POLL_MS = 5000

const SEVERITY: Record<string, { label: string; chip: string }> = {
  critical: { label: 'CRITIC', chip: 'border-grid-red/40 bg-grid-red/10 text-grid-red' },
  warning: { label: 'AVERTISMENT', chip: 'border-grid-amber/40 bg-grid-amber/10 text-grid-amber' },
  info: { label: 'INFO', chip: 'border-grid-blue/40 bg-grid-blue/10 text-grid-blue' },
}

function ago(iso?: string | null): string {
  if (!iso) return ''
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (s < 60) return `acum ${s} s`
  if (s < 3600) return `acum ${Math.round(s / 60)} min`
  return `acum ${Math.round(s / 3600)} h`
}

/** Header bell: what is wrong right now (Prometheus alerts + the app's own, e.g. a failover). */
export function AlertBell() {
  const [data, setData] = useState<AlertsResponse | null>(null)
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let alive = true
    async function poll() {
      try {
        const next = await getAlerts()
        if (alive) setData(next)
      } catch {
        // a transient error must not break the header; the next poll retries
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), POLL_MS)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [])

  const counts = data?.counts ?? { critical: 0, warning: 0, info: 0 }
  const urgent = counts.critical + counts.warning
  const tone = counts.critical > 0 ? 'red' : counts.warning > 0 ? 'amber' : 'muted'

  // the browser tab itself shows when something is wrong
  useEffect(() => {
    const base = document.title.replace(/^\(\d+\)\s*/, '')
    document.title = urgent > 0 ? `(${urgent}) ${base}` : base
  }, [urgent])

  const shown: AppAlert[] = data?.alerts ?? []

  return (
    <div className="relative" ref={box}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label="Alerte"
        className={`relative inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px] font-medium transition ${
          tone === 'red'
            ? 'border-grid-red/40 bg-grid-red/10 text-grid-red'
            : tone === 'amber'
              ? 'border-grid-amber/40 bg-grid-amber/10 text-grid-amber'
              : 'border-grid-border bg-grid-panel text-grid-muted hover:text-grid-text'
        }`}
      >
        <Bell className={`h-3.5 w-3.5 ${counts.critical > 0 ? 'animate-pulse' : ''}`} />
        {urgent > 0 ? `${urgent} ${urgent === 1 ? 'alertă' : 'alerte'}` : 'Alerte'}
      </button>

      {open && (
        <div className="absolute right-0 top-9 z-50 max-h-[70vh] w-[26rem] max-w-[calc(100vw-2rem)] overflow-y-auto rounded-xl border border-grid-border bg-grid-bg p-3 shadow-2xl scrollbar-thin">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-bold text-grid-text">Alerte</h3>
            <span className="text-[11px] text-grid-muted">
              {counts.critical} critice · {counts.warning} avertismente · {counts.info} info
            </span>
          </div>

          {data && !data.prometheus_ok && (
            <p className="mb-2 rounded-lg border border-grid-amber/30 bg-grid-amber/10 px-2.5 py-1.5 text-[11px] text-grid-amber">
              Prometheus nu răspunde: se arată doar alertele aplicației.
            </p>
          )}

          {shown.length === 0 ? (
            <p className="flex items-center gap-2 py-6 text-sm text-grid-green">
              <CheckCircle2 className="h-4 w-4" />
              Nicio alertă activă. Totul funcționează.
            </p>
          ) : (
            <ul className="space-y-2">
              {shown.map((a) => {
                const sev = SEVERITY[a.severity] ?? SEVERITY.warning
                const dim = a.state !== 'firing'
                return (
                  <li
                    key={a.id}
                    className={`rounded-lg border border-grid-border bg-grid-surface p-2.5 ${dim ? 'opacity-60' : ''}`}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold ${sev.chip}`}>
                        {sev.label}
                      </span>
                      {a.state === 'pending' && (
                        <span className="rounded border border-grid-border px-1.5 py-0.5 text-[10px] text-grid-muted">
                          în așteptare
                        </span>
                      )}
                      {a.state === 'resolved' && (
                        <span className="rounded border border-grid-green/30 px-1.5 py-0.5 text-[10px] text-grid-green">
                          rezolvată
                        </span>
                      )}
                      <span className="ml-auto text-[10px] text-grid-muted">{ago(a.since)}</span>
                    </div>
                    <p className="mt-1.5 text-[13px] font-medium text-grid-text">{a.title}</p>
                    {a.detail && <p className="mt-0.5 text-[12px] text-grid-muted">{a.detail}</p>}
                    {a.runbook && (
                      <p className="mt-1 font-mono text-[10px] text-grid-cyan/80">Ce verific: {a.runbook}</p>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
