import type { StorageBackend } from '../types/filegrid'
import { STORAGE_LABELS } from '../types/filegrid'

const STYLES: Record<StorageBackend, string> = {
  postgres: 'bg-grid-postgres/15 text-grid-postgres border-grid-postgres/30',
  s3: 'bg-grid-s3/15 text-grid-s3 border-grid-s3/30',
  ftp: 'bg-grid-ftp/15 text-grid-ftp border-grid-ftp/30',
}

export function StorageBadge({ storage }: { storage: StorageBackend }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-semibold tracking-wide ${STYLES[storage]}`}
    >
      {STORAGE_LABELS[storage]}
    </span>
  )
}
