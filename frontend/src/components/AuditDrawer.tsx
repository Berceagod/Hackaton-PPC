import { X, Upload, Download, Shield } from 'lucide-react'
import type { AuditEntry, FileRecord } from '../types/filegrid'

interface AuditDrawerProps {
  file: FileRecord
  entries: AuditEntry[]
  onClose: () => void
}

export function AuditDrawer({ file, entries, onClose }: AuditDrawerProps) {
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/50 backdrop-blur-sm">
      <button
        type="button"
        className="absolute inset-0 cursor-default"
        aria-label="Închide overlay"
        onClick={onClose}
      />
      <aside className="animate-slide-in relative z-10 flex h-full w-full max-w-md flex-col border-l border-grid-border bg-grid-surface shadow-2xl">
        <div className="flex items-start justify-between border-b border-grid-border px-5 py-4">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-grid-text">
              <Shield className="h-4 w-4 text-grid-ftp" />
              Audit Log
            </div>
            <p className="mt-1 text-xs text-grid-muted break-all">{file.filename}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 text-grid-muted hover:bg-grid-panel hover:text-grid-text"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5 scrollbar-thin">
          {entries.length === 0 ? (
            <p className="text-sm text-grid-muted">Nicio înregistrare de audit.</p>
          ) : (
            <ul className="space-y-3">
              {entries.map((e) => (
                <li
                  key={e.id}
                  className="rounded-lg border border-grid-border bg-grid-panel/60 px-3 py-3"
                >
                  <div className="flex items-center gap-2 text-sm font-medium text-grid-text">
                    {e.action === 'upload' ? (
                      <Upload className="h-3.5 w-3.5 text-grid-cyan" />
                    ) : (
                      <Download className="h-3.5 w-3.5 text-grid-amber" />
                    )}
                    {e.action === 'upload' ? 'Încărcare' : 'Descărcare'}
                  </div>
                  <p className="mt-1 text-xs text-grid-muted">
                    {e.user} · {new Date(e.timestamp).toLocaleString('ro-RO')}
                  </p>
                  {e.details && (
                    <p className="mt-1 text-[11px] text-grid-muted/80">{e.details}</p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>
    </div>
  )
}
