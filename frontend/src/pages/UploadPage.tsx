import { useRef, useState, type ChangeEvent } from 'react'
import {
  Upload,
  AlertTriangle,
  FileVideo,
  FileText,
  Lock,
  Info,
  CheckCircle2,
  Copy,
} from 'lucide-react'
import { ProgressBar } from '../components/ProgressBar'
import { StorageBadge } from '../components/StorageBadge'
import { uploadFileResilient, formatBytes } from '../services/api'
import type {
  FileCategory,
  FileMetadata,
  IngestResponse,
  UploadProgress,
  Zone,
} from '../types/filegrid'
import { ZONE_OPTIONS } from '../types/filegrid'
import type { ToastMessage } from '../components/Toast'

type ManualCategory = Exclude<FileCategory, 'meter_readings'>

const CATEGORIES: {
  id: ManualCategory
  title: string
  description: string
  icon: typeof AlertTriangle
  badge?: string
}[] = [
  {
    id: 'outage_records',
    title: 'Înregistrări de avarie',
    description: 'Stații de transformare — prioritate maximă pentru dispecerat',
    icon: AlertTriangle,
  },
  {
    id: 'connection_docs',
    title: 'Documente de racordare',
    description: 'Cereri, CI, avize — date personale sensibile',
    icon: FileText,
    badge: 'Criptare AES-256-GCM automată',
  },
  {
    id: 'drone_videos',
    title: 'Video-uri inspecții dronă',
    description: 'Fișiere mari 100MB – 1GB+ — upload pe bucăți de 5 MB',
    icon: FileVideo,
  },
]

interface UploadPageProps {
  pushToast: (t: Omit<ToastMessage, 'id'>) => void
}

export function UploadPage({ pushToast }: UploadPageProps) {
  const [category, setCategory] = useState<ManualCategory>('outage_records')
  const [zone, setZone] = useState<Zone | ''>('')
  const [station, setStation] = useState('')
  const [description, setDescription] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [progress, setProgress] = useState<UploadProgress | null>(null)
  const [result, setResult] = useState<IngestResponse | null>(null)
  const [uploading, setUploading] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  function onFileChange(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] ?? null
    setFile(f)
    setResult(null)
    setProgress(null)
  }

  async function handleUpload() {
    if (!file) {
      pushToast({ kind: 'info', title: 'Selectați un fișier' })
      return
    }
    setUploading(true)
    setResult(null)
    const metadata: FileMetadata = {}
    if (zone) metadata.zone = zone
    if (station.trim()) metadata.station = station.trim()
    if (description.trim()) metadata.description = description.trim()

    try {
      const res = await uploadFileResilient(file, category, metadata, setProgress)
      setResult(res)
      pushToast({
        kind: 'success',
        title: 'Fișier ingerat cu succes',
        detail: `${res.file_id} → ${res.storage}`,
      })
    } catch (err) {
      setProgress((p) =>
        p
          ? { ...p, status: 'error', message: err instanceof Error ? err.message : 'Eroare' }
          : null,
      )
      pushToast({
        kind: 'error',
        title: 'Upload eșuat',
        detail: err instanceof Error ? err.message : String(err),
      })
    } finally {
      setUploading(false)
    }
  }

  function copyText(text: string, label: string) {
    void navigator.clipboard.writeText(text)
    pushToast({ kind: 'info', title: `${label} copiat` })
  }

  return (
    <div className="animate-fade-in space-y-6">
      <div>
        <h2 className="text-xl font-bold text-grid-text">Încărcare Fișiere</h2>
        <p className="mt-1 text-sm text-grid-muted">
          Selectați categoria, completați metadatele opționale și încărcați fișierul din teren.
        </p>
      </div>

      <div className="flex items-start gap-3 rounded-xl border border-grid-blue/25 bg-grid-blue/5 px-4 py-3">
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-grid-blue" />
        <p className="text-sm text-grid-muted">
          <span className="font-medium text-grid-text">Citirile de contoare</span> (
          <code className="font-mono text-xs text-grid-cyan">meter_readings</code>) sosesc
          automat de la simulatorul de teren — nu necesită încărcare manuală.
        </p>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        {CATEGORIES.map((c) => {
          const Icon = c.icon
          const active = category === c.id
          return (
            <button
              key={c.id}
              type="button"
              onClick={() => setCategory(c.id)}
              className={`rounded-xl border p-4 text-left transition ${
                active
                  ? 'border-grid-cyan/50 bg-grid-cyan/5 ring-1 ring-grid-cyan/30'
                  : 'border-grid-border bg-grid-surface hover:border-grid-blue/40'
              }`}
            >
              <Icon className={`mb-2 h-5 w-5 ${active ? 'text-grid-cyan' : 'text-grid-muted'}`} />
              <div className="text-sm font-semibold text-grid-text">{c.title}</div>
              <p className="mt-1 text-xs text-grid-muted">{c.description}</p>
              {c.badge && (
                <span className="mt-2 inline-flex items-center gap-1 rounded border border-grid-ftp/30 bg-grid-ftp/10 px-2 py-0.5 text-[10px] font-medium text-grid-ftp">
                  <Lock className="h-3 w-3" />
                  {c.badge}
                </span>
              )}
            </button>
          )
        })}
      </div>

      <div className="grid gap-4 rounded-xl border border-grid-border bg-grid-surface p-5 md:grid-cols-3">
        <div>
          <label className="mb-1 block text-xs font-medium uppercase tracking-wider text-grid-muted">
            Județ
          </label>
          <select
            value={zone}
            onChange={(e) => setZone(e.target.value as Zone | '')}
            className="w-full rounded-lg border border-grid-border bg-grid-bg px-3 py-2 text-sm text-grid-text outline-none focus:ring-2 focus:ring-grid-blue"
          >
            <option value="">— Opțional —</option>
            {ZONE_OPTIONS.map((z) => (
              <option key={z} value={z}>
                {z}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium uppercase tracking-wider text-grid-muted">
            Stație / Linie electrică
          </label>
          <input
            value={station}
            onChange={(e) => setStation(e.target.value)}
            placeholder="ex. ST-Bucuresti-Nord"
            className="w-full rounded-lg border border-grid-border bg-grid-bg px-3 py-2 text-sm text-grid-text outline-none focus:ring-2 focus:ring-grid-blue"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium uppercase tracking-wider text-grid-muted">
            Descriere
          </label>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Context operațional"
            className="w-full rounded-lg border border-grid-border bg-grid-bg px-3 py-2 text-sm text-grid-text outline-none focus:ring-2 focus:ring-grid-blue"
          />
        </div>
      </div>

      <div className="rounded-xl border border-dashed border-grid-border bg-grid-surface/60 p-6">
        <input
          ref={inputRef}
          type="file"
          className="hidden"
          onChange={onFileChange}
        />
        <div className="flex flex-col items-center gap-3 text-center sm:flex-row sm:text-left">
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="inline-flex items-center gap-2 rounded-lg border border-grid-border bg-grid-panel px-4 py-2.5 text-sm font-medium text-grid-text hover:border-grid-cyan/40"
          >
            <Upload className="h-4 w-4 text-grid-cyan" />
            Alege fișier
          </button>
          <div className="min-w-0 flex-1">
            {file ? (
              <p className="truncate text-sm text-grid-text">
                {file.name}{' '}
                <span className="text-grid-muted">({formatBytes(file.size)})</span>
              </p>
            ) : (
              <p className="text-sm text-grid-muted">
                Fișiere &gt; 5 MB folosesc upload pe bucăți (5 MB) cu auto-retry.
              </p>
            )}
          </div>
          <button
            type="button"
            disabled={!file || uploading}
            onClick={() => void handleUpload()}
            className="inline-flex items-center gap-2 rounded-lg bg-grid-blue px-5 py-2.5 text-sm font-semibold text-white hover:bg-sky-500 disabled:opacity-50"
          >
            <Upload className="h-4 w-4" />
            {uploading ? 'Se încarcă…' : 'Încarcă'}
          </button>
        </div>
      </div>

      {progress && <ProgressBar progress={progress} />}

      {result && (
        <div className="animate-fade-in rounded-xl border border-grid-green/30 bg-grid-green/5 p-5">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-grid-green">
            <CheckCircle2 className="h-5 w-5" />
            Confirmare ingestie
          </div>
          <dl className="grid gap-3 sm:grid-cols-2">
            <ResultRow
              label="file_id"
              value={result.file_id}
              onCopy={() => copyText(result.file_id, 'file_id')}
            />
            <ResultRow
              label="X-Trace-Id"
              value={result.trace_id}
              onCopy={() => copyText(result.trace_id, 'Trace ID')}
            />
            <div>
              <dt className="text-[11px] uppercase tracking-wider text-grid-muted">Storage</dt>
              <dd className="mt-1">
                <StorageBadge storage={result.storage} />
              </dd>
            </div>
            <ResultRow
              label="SHA-256"
              value={result.sha256}
              onCopy={() => copyText(result.sha256, 'SHA-256')}
            />
          </dl>
        </div>
      )}
    </div>
  )
}

function ResultRow({
  label,
  value,
  onCopy,
}: {
  label: string
  value: string
  onCopy: () => void
}) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wider text-grid-muted">{label}</dt>
      <dd className="mt-1 flex items-center gap-2">
        <code className="truncate font-mono text-xs text-grid-cyan">{value}</code>
        <button
          type="button"
          onClick={onCopy}
          className="shrink-0 rounded p-1 text-grid-muted hover:bg-grid-panel hover:text-grid-text"
        >
          <Copy className="h-3.5 w-3.5" />
        </button>
      </dd>
    </div>
  )
}
