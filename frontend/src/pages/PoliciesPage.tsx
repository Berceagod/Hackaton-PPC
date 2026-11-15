import { useEffect, useState } from 'react'
import { Save, RefreshCw, Zap, CheckCircle2, Lock } from 'lucide-react'
import { getAlerts, getPolicies, getStoredSession, updatePolicies } from '../services/api'
import type {
  FileCategory,
  PoliciesMap,
  QueuePriority,
  StorageBackend,
} from '../types/filegrid'
import { CATEGORY_LABELS } from '../types/filegrid'
import type { ToastMessage } from '../components/Toast'

const CATEGORIES: FileCategory[] = [
  'meter_readings',
  'outage_records',
  'connection_docs',
  'drone_videos',
]

interface PoliciesPageProps {
  pushToast: (t: Omit<ToastMessage, 'id'>) => void
}

export function PoliciesPage({ pushToast }: PoliciesPageProps) {
  const [policies, setPolicies] = useState<PoliciesMap | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [applied, setApplied] = useState(false)
  // categories whose new files are going to the fallback storage right now (from the alert feed)
  const [failovers, setFailovers] = useState<Record<string, string>>({})
  // Only an admin may change routing (the backend answers 403 otherwise)
  const canEdit = getStoredSession()?.user.role === 'admin'

  useEffect(() => {
    void (async () => {
      setLoading(true)
      try {
        setPolicies(await getPolicies())
      } catch (err) {
        pushToast({
          kind: 'error',
          title: 'Nu s-au putut încărca politicile',
          detail: err instanceof Error ? err.message : String(err),
        })
      } finally {
        setLoading(false)
      }
    })()
  }, [pushToast])

  useEffect(() => {
    let alive = true
    async function poll() {
      try {
        const feed = await getAlerts()
        if (!alive) return
        const active: Record<string, string> = {}
        for (const a of feed.alerts) {
          if (a.source === 'app' && a.state === 'firing' && a.category && a.id.startsWith('app:failover:')) {
            active[a.category] = a.title
          }
        }
        setFailovers(active)
      } catch {
        // the page works without the live badge
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 5000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  function updateCategory<K extends keyof PoliciesMap[FileCategory]>(
    category: FileCategory,
    key: K,
    value: PoliciesMap[FileCategory][K],
  ) {
    setPolicies((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        [category]: { ...prev[category], [key]: value },
      }
    })
    setApplied(false)
  }

  async function handleSave() {
    if (!policies) return
    setSaving(true)
    try {
      const saved = await updatePolicies(policies)
      setPolicies(saved)
      setApplied(true)
      pushToast({
        kind: 'success',
        title: 'Politici aplicate — Hot-Reload activ',
        detail: 'Noua rutare este activă imediat, fără restart.',
      })
    } catch (err) {
      pushToast({
        kind: 'error',
        title: 'Salvare eșuată',
        detail: err instanceof Error ? err.message : String(err),
      })
    } finally {
      setSaving(false)
    }
  }

  if (loading || !policies) {
    return (
      <div className="flex items-center gap-2 py-16 text-grid-muted">
        <RefreshCw className="h-4 w-4 animate-spin" />
        Se încarcă politicile…
      </div>
    )
  }

  return (
    <div className="animate-fade-in space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-grid-text">Politici de Rutare</h2>
          <p className="mt-1 text-sm text-grid-muted">
            Modificați storage, criptare, deduplicare și prioritate — fără modificare de cod.
          </p>
        </div>
        {canEdit ? (
        <button
          type="button"
          disabled={saving}
          onClick={() => void handleSave()}
          className="inline-flex items-center gap-2 rounded-lg bg-grid-amber px-4 py-2.5 text-sm font-semibold text-grid-bg hover:bg-amber-400 disabled:opacity-50"
        >
          {saving ? (
            <RefreshCw className="h-4 w-4 animate-spin" />
          ) : (
            <Save className="h-4 w-4" />
          )}
          Salvează și Aplică Hot-Reload
        </button>
        ) : (
          <span className="inline-flex items-center gap-2 rounded-lg border border-grid-border px-4 py-2.5 text-sm text-grid-muted">
            <Lock className="h-4 w-4" />
            Doar citire — modificările sunt permise doar administratorului
          </span>
        )}
      </div>

      {applied && (
        <div className="animate-fade-in flex items-center gap-2 rounded-xl border border-grid-green/30 bg-grid-green/10 px-4 py-3 text-sm text-grid-green">
          <CheckCircle2 className="h-4 w-4" />
          <Zap className="h-4 w-4" />
          Rutarea nouă este activă imediat — fără restart al serviciilor.
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {CATEGORIES.map((cat) => {
          const p = policies[cat]
          return (
            <div
              key={cat}
              className="rounded-xl border border-grid-border bg-grid-surface p-5"
            >
              <h3 className="text-sm font-semibold text-grid-text">
                {CATEGORY_LABELS[cat]}
              </h3>
              <p className="mb-4 font-mono text-[11px] text-grid-muted">{cat}</p>
              {failovers[cat] && (
                <p className="mb-3 rounded-lg border border-grid-amber/40 bg-grid-amber/10 px-3 py-2 text-[12px] font-medium text-grid-amber">
                  {failovers[cat]}
                </p>
              )}

              <div className="space-y-3">
                <div>
                  <label className="mb-1 block text-[11px] uppercase tracking-wider text-grid-muted">
                    Storage destinație
                  </label>
                  <select
                    disabled={!canEdit}
                    value={p.storage}
                    onChange={(e) =>
                      updateCategory(cat, 'storage', e.target.value as StorageBackend)
                    }
                    className="w-full rounded-lg border border-grid-border bg-grid-bg px-3 py-2 text-sm text-grid-text outline-none focus:ring-2 focus:ring-grid-blue disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <option value="postgres">postgres</option>
                    <option value="s3">s3</option>
                    <option value="ftp">ftp</option>
                  </select>
                </div>

                <ToggleRow
                  label="Criptare la stocare"
                  disabled={!canEdit}
                  checked={p.encryption}
                  onChange={(v) => updateCategory(cat, 'encryption', v)}
                />
                <ToggleRow
                  label="Deduplicare"
                  disabled={!canEdit}
                  checked={p.deduplicate}
                  onChange={(v) => updateCategory(cat, 'deduplicate', v)}
                />

                <div>
                  <label className="mb-1 block text-[11px] uppercase tracking-wider text-grid-muted">
                    Prioritate coadă
                  </label>
                  <select
                    disabled={!canEdit}
                    value={p.priority}
                    onChange={(e) =>
                      updateCategory(cat, 'priority', e.target.value as QueuePriority)
                    }
                    className="w-full rounded-lg border border-grid-border bg-grid-bg px-3 py-2 text-sm text-grid-text outline-none focus:ring-2 focus:ring-grid-blue disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <option value="high">high</option>
                    <option value="normal">normal</option>
                    <option value="low">low</option>
                  </select>
                </div>

                <div>
                  <label className="mb-1 block text-[11px] uppercase tracking-wider text-grid-muted">
                    Storage de rezervă (redirecționare automată)
                  </label>
                  <select
                    disabled={!canEdit}
                    value={p.fallback_storage ?? ''}
                    onChange={(e) =>
                      updateCategory(
                        cat,
                        'fallback_storage',
                        e.target.value === '' ? null : (e.target.value as StorageBackend),
                      )
                    }
                    className="w-full rounded-lg border border-grid-border bg-grid-bg px-3 py-2 text-sm text-grid-text outline-none focus:ring-2 focus:ring-grid-blue disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <option value="">— fără (nu comută automat) —</option>
                    {(['postgres', 's3', 'ftp'] as StorageBackend[])
                      .filter((st) => st !== p.storage)
                      .map((st) => (
                        <option key={st} value={st}>
                          {st}
                        </option>
                      ))}
                  </select>
                  <p className="mt-1 text-[11px] text-grid-muted">
                    Dacă storage-ul principal pică sau fluxul de date este prea mare, fișierele noi merg
                    automat aici și revin singure la principal când își revine.
                  </p>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ToggleRow({
  label,
  checked,
  onChange,
  disabled = false,
}: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
}) {
  return (
    <div className="flex items-center justify-between rounded-lg border border-grid-border bg-grid-panel/40 px-3 py-2.5">
      <span className="text-sm text-grid-text">{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`relative h-6 w-11 rounded-full transition disabled:cursor-not-allowed disabled:opacity-50 ${
          checked ? 'bg-grid-cyan' : 'bg-grid-border'
        }`}
      >
        <span
          className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white transition ${
            checked ? 'translate-x-5' : ''
          }`}
        />
      </button>
    </div>
  )
}
