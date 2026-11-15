import { useCallback, useEffect, useState } from 'react'
import {
  Search,
  Download,
  Copy,
  ShieldCheck,
  RefreshCw,
  ScrollText,
  Lock,
  Fingerprint,
} from 'lucide-react'
import { StorageBadge } from '../components/StorageBadge'
import { AuditDrawer } from '../components/AuditDrawer'
import {
  listFiles,
  downloadFile,
  getAuditLog,
  formatBytes,
} from '../services/api'
import type {
  AuditEntry,
  FileCategory,
  FileRecord,
  StorageBackend,
} from '../types/filegrid'
import { CATEGORY_LABELS } from '../types/filegrid'
import type { ToastMessage } from '../components/Toast'

interface FilesPageProps {
  pushToast: (t: Omit<ToastMessage, 'id'>) => void
}

export function FilesPage({ pushToast }: FilesPageProps) {
  const [files, setFiles] = useState<FileRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [category, setCategory] = useState<FileCategory | 'all'>('all')
  const [storage, setStorage] = useState<StorageBackend | 'all'>('all')
  const [query, setQuery] = useState('')
  const [auditFile, setAuditFile] = useState<FileRecord | null>(null)
  const [auditEntries, setAuditEntries] = useState<AuditEntry[]>([])
  const [downloadingId, setDownloadingId] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const data = await listFiles({
        category,
        storage,
        q: query || undefined,
      })
      setFiles(data)
    } catch (err) {
      pushToast({
        kind: 'error',
        title: 'Eroare listare',
        detail: err instanceof Error ? err.message : String(err),
      })
    } finally {
      setLoading(false)
    }
  }, [category, storage, query, pushToast])

  useEffect(() => {
    void refresh()
  }, [refresh])

  async function handleDownload(file: FileRecord) {
    setDownloadingId(file.id)
    try {
      const result = await downloadFile(file.id)
      if (result.corrupted) {
        pushToast({
          kind: 'error',
          title: 'Fișier corupt — 409 Conflict',
          detail: 'Integritatea SHA-256 nu a putut fi confirmată.',
        })
        return
      }
      if (result.integrityVerified) {
        pushToast({
          kind: 'success',
          title: 'Integritate verificată byte-for-byte: SHA-256 identic ✓',
          detail: result.sha256.slice(0, 32) + '…',
        })
      }
      const url = URL.createObjectURL(result.blob)
      const a = document.createElement('a')
      a.href = url
      a.download = result.filename
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      const corrupted =
        err instanceof Error &&
        ('corrupted' in err || err.message.includes('409') || err.message.includes('Corrupted'))
      pushToast({
        kind: 'error',
        title: corrupted
          ? 'ALERTĂ CRITICĂ: 409 Conflict / File Corrupted'
          : 'Download eșuat',
        detail: err instanceof Error ? err.message : String(err),
      })
    } finally {
      setDownloadingId(null)
    }
  }

  async function openAudit(file: FileRecord) {
    try {
      const entries = await getAuditLog(file.id)
      setAuditEntries(entries)
      setAuditFile(file)
    } catch (err) {
      pushToast({
        kind: 'error',
        title: 'Audit indisponibil',
        detail: err instanceof Error ? err.message : String(err),
      })
    }
  }

  function copyTrace(id: string) {
    void navigator.clipboard.writeText(id)
    pushToast({ kind: 'info', title: 'Trace ID copiat' })
  }

  return (
    <div className="animate-fade-in space-y-5">
      <div>
        <h2 className="text-xl font-bold text-grid-text">Explorare, Căutare & Descărcare</h2>
        <p className="mt-1 text-sm text-grid-muted">
          Filtrați după categorie și storage, căutați după nume, trace_id sau zonă.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-3 rounded-xl border border-grid-border bg-grid-surface p-4">
        <div>
          <label className="mb-1 block text-[11px] uppercase tracking-wider text-grid-muted">
            Categorie
          </label>
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value as FileCategory | 'all')}
            className="rounded-lg border border-grid-border bg-grid-bg px-3 py-2 text-sm text-grid-text outline-none focus:ring-2 focus:ring-grid-blue"
          >
            <option value="all">Toate</option>
            {(Object.keys(CATEGORY_LABELS) as FileCategory[]).map((c) => (
              <option key={c} value={c}>
                {CATEGORY_LABELS[c]}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-[11px] uppercase tracking-wider text-grid-muted">
            Storage
          </label>
          <select
            value={storage}
            onChange={(e) => setStorage(e.target.value as StorageBackend | 'all')}
            className="rounded-lg border border-grid-border bg-grid-bg px-3 py-2 text-sm text-grid-text outline-none focus:ring-2 focus:ring-grid-blue"
          >
            <option value="all">Toate</option>
            <option value="postgres">Postgres</option>
            <option value="s3">S3</option>
            <option value="ftp">FTP</option>
          </select>
        </div>
        <div className="min-w-[200px] flex-1">
          <label className="mb-1 block text-[11px] uppercase tracking-wider text-grid-muted">
            Căutare
          </label>
          <div className="relative">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-grid-muted" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Nume, trace_id, zonă…"
              className="w-full rounded-lg border border-grid-border bg-grid-bg py-2 pl-9 pr-3 text-sm text-grid-text outline-none focus:ring-2 focus:ring-grid-blue"
            />
          </div>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          className="inline-flex items-center gap-1.5 rounded-lg border border-grid-border px-3 py-2 text-sm text-grid-muted hover:text-grid-text"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      <div className="overflow-x-auto rounded-xl border border-grid-border bg-grid-surface scrollbar-thin">
        <table className="w-full min-w-[960px] text-left text-sm">
          <thead className="border-b border-grid-border bg-grid-panel/80 text-[11px] uppercase tracking-wider text-grid-muted">
            <tr>
              <th className="px-4 py-3 font-medium">Nume fișier</th>
              <th className="px-4 py-3 font-medium">Categorie</th>
              <th className="px-4 py-3 font-medium">Storage</th>
              <th className="px-4 py-3 font-medium">Dimensiune</th>
              <th className="px-4 py-3 font-medium">Dată</th>
              <th className="px-4 py-3 font-medium">Securitate</th>
              <th className="px-4 py-3 font-medium">Trace ID</th>
              <th className="px-4 py-3 font-medium">Acțiuni</th>
            </tr>
          </thead>
          <tbody>
            {loading && files.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-4 py-10 text-center text-grid-muted">
                  Se încarcă…
                </td>
              </tr>
            ) : files.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-4 py-10 text-center text-grid-muted">
                  Niciun fișier găsit.
                </td>
              </tr>
            ) : (
              files.map((f) => (
                <tr
                  key={f.id}
                  className="border-b border-grid-border/60 last:border-0 hover:bg-grid-panel/40"
                >
                  <td className="max-w-[200px] truncate px-4 py-3 font-medium text-grid-text">
                    {f.filename}
                    {f.zone && (
                      <span className="mt-0.5 block text-[11px] font-normal text-grid-muted">
                        {f.zone}
                        {f.station ? ` · ${f.station}` : ''}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs text-grid-muted">
                    {CATEGORY_LABELS[f.category]}
                  </td>
                  <td className="px-4 py-3">
                    <StorageBadge storage={f.storage} />
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-grid-muted">
                    {formatBytes(f.size_bytes)}
                  </td>
                  <td className="px-4 py-3 text-xs text-grid-muted">
                    {new Date(f.created_at).toLocaleString('ro-RO')}
                  </td>
                  <td className="px-4 py-3">
                    <SecurityBadge file={f} />
                  </td>
                  <td className="px-4 py-3">
                    <button
                      type="button"
                      onClick={() => copyTrace(f.trace_id)}
                      className="inline-flex max-w-[120px] items-center gap-1 font-mono text-[11px] text-grid-cyan hover:underline"
                      title={f.trace_id}
                    >
                      <span className="truncate">{f.trace_id.slice(0, 8)}…</span>
                      <Copy className="h-3 w-3 shrink-0" />
                    </button>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1">
                      <button
                        type="button"
                        disabled={downloadingId === f.id}
                        onClick={() => void handleDownload(f)}
                        className="inline-flex items-center gap-1 rounded-md border border-grid-border px-2 py-1 text-[11px] font-medium text-grid-text hover:border-grid-cyan/40 disabled:opacity-50"
                        title="Download & Verifică Integritate"
                      >
                        <Download className="h-3.5 w-3.5 text-grid-cyan" />
                        {downloadingId === f.id ? '…' : 'Download'}
                      </button>
                      {(
                        <button
                          type="button"
                          onClick={() => void openAudit(f)}
                          className="inline-flex items-center gap-1 rounded-md border border-grid-border px-2 py-1 text-[11px] text-grid-muted hover:text-grid-text"
                          title="Audit Log"
                        >
                          <ScrollText className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {auditFile && (
        <AuditDrawer
          file={auditFile}
          entries={auditEntries}
          onClose={() => setAuditFile(null)}
        />
      )}
    </div>
  )
}

function SecurityBadge({ file }: { file: FileRecord }) {
  if (file.category === 'connection_docs' || file.encrypted) {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-grid-ftp/30 bg-grid-ftp/10 px-2 py-0.5 text-[10px] font-medium text-grid-ftp">
        <Lock className="h-3 w-3" />
        Criptat AES-256
      </span>
    )
  }
  if (file.category === 'meter_readings') {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-grid-green/30 bg-grid-green/10 px-2 py-0.5 text-[10px] font-medium text-grid-green">
        <Fingerprint className="h-3 w-3" />
        Idempotent
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1 rounded border border-grid-border px-2 py-0.5 text-[10px] text-grid-muted">
      <ShieldCheck className="h-3 w-3" />
      Standard
    </span>
  )
}
