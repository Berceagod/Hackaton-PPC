import type { UploadProgress } from '../types/filegrid'
import { Loader2, RefreshCw } from 'lucide-react'

export function ProgressBar({ progress }: { progress: UploadProgress }) {
  const isRetry = progress.status === 'retrying'

  return (
    <div className="rounded-xl border border-grid-border bg-grid-panel/50 p-4">
      <div className="mb-2 flex items-center justify-between text-xs">
        <span className="flex items-center gap-2 font-medium text-grid-text">
          {(progress.status === 'uploading' || progress.status === 'hashing') && (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-grid-cyan" />
          )}
          {isRetry && <RefreshCw className="h-3.5 w-3.5 animate-spin text-grid-amber" />}
          {progress.message || progress.status}
        </span>
        <span className="font-mono text-grid-muted">{progress.percent}%</span>
      </div>

      <div className="h-2.5 overflow-hidden rounded-full bg-grid-bg">
        <div
          className={`h-full rounded-full transition-all duration-300 ${
            isRetry ? 'bg-grid-amber' : 'bg-gradient-to-r from-grid-blue to-grid-cyan'
          }`}
          style={{ width: `${progress.percent}%` }}
        />
      </div>

      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-grid-muted">
        {progress.totalChunks > 1 && (
          <span>
            Chunk {progress.currentChunk}/{progress.totalChunks}
          </span>
        )}
        {progress.speedMBps > 0 && (
          <span className="font-mono">{progress.speedMBps.toFixed(2)} MB/s</span>
        )}
        {isRetry && progress.retryAttempt != null && (
          <span className="text-grid-amber">
            Retry {progress.retryAttempt}/3 (exponential backoff)
          </span>
        )}
      </div>
    </div>
  )
}
