import { useEffect, useState } from 'react'
import { Activity } from 'lucide-react'
import {
  Bar,
  BarChart,
  Cell,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { getLiveLatency, type LiveLatency } from '../services/api'

const ORDER = ['postgres', 's3', 'ftp']
const LABEL: Record<string, string> = { postgres: 'PostgreSQL', s3: 'S3 (MinIO)', ftp: 'FTP' }
const POLL_MS = 2000
/** Under this many operations per second a storage only has background traffic (the automatic
 *  health checks): its percentiles come from a handful of calls and are not meaningful. */
const BACKGROUND_OPS = 0.5

/** Real-time latency per storage (last 30 s, from Prometheus), refreshed every 2 s. */
export function LiveStorageLatency() {
  const [live, setLive] = useState<LiveLatency | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    async function poll() {
      try {
        const next = await getLiveLatency()
        if (alive) {
          setLive(next)
          setFailed(false)
        }
      } catch {
        if (alive) setFailed(true)
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), POLL_MS)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  const rows = ORDER.filter((s) => live?.storages[s]).map((s) => {
    const v = live!.storages[s]
    return {
      name: LABEL[s],
      'Scriere p50': v.put_p50_ms ?? 0,
      'Scriere p95': v.put_p95_ms ?? 0,
      'Citire p50': v.get_p50_ms ?? 0,
      'Citire p95': v.get_p95_ms ?? 0,
      ops: v.ops_per_s ?? 0,
      background: (v.ops_per_s ?? 0) < BACKGROUND_OPS,
    }
  })

  return (
    <section className="space-y-3 rounded-xl border border-grid-border bg-grid-surface p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-grid-cyan">
          <Activity className="h-4 w-4 animate-pulse-soft" />
          Latență live pe storage (ultimele 30 s)
        </h3>
        <span className="text-[11px] text-grid-muted">
          se actualizează la 2 s · milisecunde, mai mic = mai bine
        </span>
      </div>

      {failed && !live ? (
        <p className="text-sm text-grid-amber">Datele live nu pot fi citite acum.</p>
      ) : live && !live.available ? (
        <p className="text-sm text-grid-amber">
          Prometheus nu răspunde; graficul live apare când este disponibil.
        </p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-grid-muted">
          Niciun trafic către storage în ultimele 30 s. Porniți <strong>Comparație storage-uri</strong> din
          panoul Testing (butonul din dreapta jos) ca să vedeți cele trei storage-uri una lângă alta.
        </p>
      ) : (
        <>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={rows} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#243049" />
                <XAxis dataKey="name" stroke="#8b9bb8" fontSize={12} />
                <YAxis stroke="#8b9bb8" fontSize={12} unit=" ms" />
                <Tooltip
                  contentStyle={{ background: '#182338', border: '1px solid #243049', borderRadius: 8 }}
                  formatter={(v) => `${Number(v).toFixed(1)} ms`}
                />
                <Legend />
                <Bar dataKey="Scriere p50" fill="#22d3ee" isAnimationActive={false}>
                  {rows.map((r) => (
                    <Cell key={r.name} fill="#22d3ee" fillOpacity={r.background ? 0.25 : 1} />
                  ))}
                </Bar>
                <Bar dataKey="Scriere p95" fill="#1d9bf0" isAnimationActive={false}>
                  {rows.map((r) => (
                    <Cell key={r.name} fill="#1d9bf0" fillOpacity={r.background ? 0.25 : 1} />
                  ))}
                </Bar>
                <Bar dataKey="Citire p50" fill="#34d399" isAnimationActive={false}>
                  {rows.map((r) => (
                    <Cell key={r.name} fill="#34d399" fillOpacity={r.background ? 0.25 : 1} />
                  ))}
                </Bar>
                <Bar dataKey="Citire p95" fill="#f59e0b" isAnimationActive={false}>
                  {rows.map((r) => (
                    <Cell key={r.name} fill="#f59e0b" fillOpacity={r.background ? 0.25 : 1} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="flex flex-wrap gap-4 text-[12px] text-grid-muted">
            {rows.map((r) => (
              <span key={r.name} className={r.background ? 'opacity-70' : ''}>
                {r.name}: <span className="text-grid-text">{r.ops.toFixed(1)}</span> operații/s
                {r.background && (
                  <span className="ml-1 rounded border border-grid-border px-1.5 py-0.5 text-[10px] text-grid-amber">
                    trafic de fundal — cifre neconcludente
                  </span>
                )}
              </span>
            ))}
          </div>
        </>
      )}
    </section>
  )
}
