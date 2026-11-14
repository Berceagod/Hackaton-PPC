import { useCallback, useEffect, useState } from 'react'
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'
import {
  RefreshCw,
  Lightbulb,
  MapPin,
  AlertTriangle,
  Activity,
  Plane,
} from 'lucide-react'
import { getAnalyticsSummary } from '../services/api'
import type { AnalyticsSummary } from '../types/filegrid'
import { CATEGORY_LABELS } from '../types/filegrid'
import { StorageBadge } from '../components/StorageBadge'
import type { ToastMessage } from '../components/Toast'

interface AnalyticsPageProps {
  pushToast: (t: Omit<ToastMessage, 'id'>) => void
}

export function AnalyticsPage({ pushToast }: AnalyticsPageProps) {
  const [data, setData] = useState<AnalyticsSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    void (async () => {
      try {
        setData(await getAnalyticsSummary())
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err)
        setData(null)
        setError(detail)
        pushToast({
          kind: 'error',
          title: 'Analytics indisponibil',
          detail,
        })
      } finally {
        setLoading(false)
      }
    })()
  }, [pushToast])

  useEffect(() => {
    load()
  }, [load])

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-16 text-grid-muted">
        <RefreshCw className="h-4 w-4 animate-spin" />
        Se încarcă statisticile…
      </div>
    )
  }

  if (!data) {
    return (
      <div className="space-y-3 py-16">
        <p className="text-sm text-grid-muted">Analytics indisponibil. {error}</p>
        <button
          type="button"
          onClick={load}
          className="inline-flex items-center gap-2 rounded-lg border border-grid-border px-3 py-1.5 text-sm text-grid-text"
        >
          <RefreshCw className="h-4 w-4" />
          Reîncearcă
        </button>
      </div>
    )
  }

  const chartData = data.load_comparison.map((p) => ({
    name: p.label,
    Contoare: p.meter_readings_ms,
    Avarii: p.outage_records_ms,
    Racordări: p.connection_docs_ms,
    Dronă: p.drone_videos_ms,
  }))

  return (
    <div className="animate-fade-in space-y-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-bold text-grid-text">Statistici & Insights Decizionale</h2>
          <p className="mt-1 text-sm text-grid-muted">
            Dashboard pentru managementul Rețele Electrice România
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          className="inline-flex shrink-0 items-center gap-2 rounded-lg border border-grid-border px-3 py-1.5 text-sm text-grid-text"
        >
          <RefreshCw className="h-4 w-4" />
          Reîncarcă
        </button>
      </div>

      {/* Section A */}
      <section className="space-y-4">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-grid-cyan">
          Secțiunea A — Matricea de Potrivire Storage vs. Categorie
        </h3>

        <div className="overflow-x-auto rounded-xl border border-grid-border bg-grid-surface scrollbar-thin">
          <table className="w-full min-w-[800px] text-left text-sm">
            <thead className="border-b border-grid-border bg-grid-panel/80 text-[11px] uppercase tracking-wider text-grid-muted">
              <tr>
                <th className="px-4 py-3 font-medium">Categorie</th>
                <th className="px-4 py-3 font-medium">Storage</th>
                <th className="px-4 py-3 font-medium">Latență avg</th>
                <th className="px-4 py-3 font-medium">p95</th>
                <th className="px-4 py-3 font-medium">Throughput</th>
                <th className="px-4 py-3 font-medium">Erori</th>
                <th className="px-4 py-3 font-medium">Verdict</th>
              </tr>
            </thead>
            <tbody>
              {data.storage_matrix.map((row) => (
                <tr
                  key={row.category}
                  className="border-b border-grid-border/60 last:border-0"
                >
                  <td className="px-4 py-3 font-medium text-grid-text">
                    {CATEGORY_LABELS[row.category]}
                  </td>
                  <td className="px-4 py-3">
                    <StorageBadge storage={row.storage} />
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">{row.latency_avg_ms} ms</td>
                  <td className="px-4 py-3 font-mono text-xs text-grid-amber">
                    {row.latency_p95_ms} ms
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-grid-cyan">
                    {formatThroughput(row.throughput_mbps)}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {(row.error_rate * 100).toFixed(1)}%
                  </td>
                  <td className="max-w-xs px-4 py-3 text-xs text-grid-muted">{row.verdict}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="flex gap-3 rounded-xl border border-grid-amber/25 bg-grid-amber/5 px-4 py-4">
          <Lightbulb className="mt-0.5 h-5 w-5 shrink-0 text-grid-amber" />
          <div>
            <p className="text-sm font-semibold text-grid-text">Verdict Automat</p>
            <p className="mt-1 text-sm leading-relaxed text-grid-muted">
              {data.narrative.storage_verdict}
            </p>
          </div>
        </div>
      </section>

      {/* Section B */}
      <section className="space-y-4">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-grid-cyan">
          Secțiunea B — Comportament Zi Normală vs. Vârf Furtună (10×)
        </h3>
        <div className="rounded-xl border border-grid-border bg-grid-surface p-4">
          <div className="h-72 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#243049" />
                <XAxis dataKey="name" tick={{ fill: '#8b9bb8', fontSize: 12 }} />
                <YAxis
                  tick={{ fill: '#8b9bb8', fontSize: 12 }}
                  label={{
                    value: 'Latență p95 (ms)',
                    angle: -90,
                    position: 'insideLeft',
                    fill: '#8b9bb8',
                    fontSize: 11,
                  }}
                />
                <Tooltip
                  contentStyle={{
                    background: '#121a2b',
                    border: '1px solid #243049',
                    borderRadius: 8,
                    color: '#e8eef8',
                  }}
                />
                <Legend />
                <Bar dataKey="Contoare" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                <Bar dataKey="Avarii" fill="#f59e0b" radius={[4, 4, 0, 0]} />
                <Bar dataKey="Racordări" fill="#a78bfa" radius={[4, 4, 0, 0]} />
                <Bar dataKey="Dronă" fill="#22d3ee" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="flex gap-3 rounded-xl border border-grid-blue/25 bg-grid-blue/5 px-4 py-4">
          <Activity className="mt-0.5 h-5 w-5 shrink-0 text-grid-blue" />
          <p className="text-sm leading-relaxed text-grid-muted">
            {data.narrative.storm_isolation}
          </p>
        </div>
      </section>

      {/* Section C */}
      <section className="space-y-4">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-grid-cyan">
          Secțiunea C — Insights din Rețeaua Electrică
        </h3>
        <div className="grid gap-4 md:grid-cols-3">
          {data.zone_insights.map((z) => (
            <div
              key={z.zone}
              className="rounded-xl border border-grid-border bg-grid-surface p-5"
            >
              <div className="mb-4 flex items-center gap-2">
                <MapPin className="h-4 w-4 text-grid-cyan" />
                <h4 className="font-semibold text-grid-text">{z.zone}</h4>
              </div>
              <ul className="space-y-3 text-sm">
                <li className="flex items-center justify-between">
                  <span className="flex items-center gap-1.5 text-grid-muted">
                    <AlertTriangle className="h-3.5 w-3.5 text-grid-amber" />
                    Avarii raportate
                  </span>
                  <span className="font-mono font-semibold text-grid-text">
                    {z.outages_reported}
                  </span>
                </li>
                <li className="flex items-center justify-between">
                  <span className="flex items-center gap-1.5 text-grid-muted">
                    <Activity className="h-3.5 w-3.5 text-grid-red" />
                    Anomalii contoare
                  </span>
                  <span className="font-mono font-semibold text-grid-text">
                    {z.meter_anomalies}
                  </span>
                </li>
                <li className="flex items-center justify-between">
                  <span className="flex items-center gap-1.5 text-grid-muted">
                    <Plane className="h-3.5 w-3.5 text-grid-cyan" />
                    Inspecții dronă
                  </span>
                  <span className="font-mono font-semibold text-grid-text">
                    {z.drone_inspections}
                  </span>
                </li>
              </ul>
              <div className="mt-4 border-t border-grid-border pt-3">
                <span
                  className={`inline-flex rounded border px-2 py-0.5 text-[11px] font-medium ${
                    z.drone_status === 'completed'
                      ? 'border-grid-green/30 bg-grid-green/10 text-grid-green'
                      : z.drone_status === 'in_progress'
                        ? 'border-grid-amber/30 bg-grid-amber/10 text-grid-amber'
                        : 'border-grid-border text-grid-muted'
                  }`}
                >
                  Status dronă: {statusLabel(z.drone_status)}
                </span>
              </div>
            </div>
          ))}
        </div>
        <p className="text-xs text-grid-muted">
          Top zone după avarii:{' '}
          {[...data.zone_insights]
            .sort((a, b) => b.outages_reported - a.outages_reported)
            .map((z) => `${z.zone} (${z.outages_reported})`)
            .join(' → ')}
          .{data.narrative.network ? ` ${data.narrative.network}` : ''}
        </p>
      </section>
    </div>
  )
}

function formatThroughput(mbps: number): string {
  if (mbps >= 0.1) return `${mbps.toFixed(1)} MB/s`
  return `${(mbps * 1000).toFixed(1)} KB/s`
}

function statusLabel(s: 'completed' | 'in_progress' | 'pending'): string {
  if (s === 'completed') return 'finalizat'
  if (s === 'in_progress') return 'în curs'
  return 'planificat'
}
