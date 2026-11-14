import { CheckCircle2, AlertTriangle, Info, X } from 'lucide-react'
import { useEffect } from 'react'

export type ToastKind = 'success' | 'error' | 'info'

export interface ToastMessage {
  id: string
  kind: ToastKind
  title: string
  detail?: string
}

interface ToastProps {
  toasts: ToastMessage[]
  onDismiss: (id: string) => void
}

export function ToastStack({ toasts, onDismiss }: ToastProps) {
  return (
    <div className="fixed bottom-4 right-4 z-[100] flex w-full max-w-md flex-col gap-2 px-4 sm:px-0">
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} onDismiss={onDismiss} />
      ))}
    </div>
  )
}

function ToastItem({
  toast,
  onDismiss,
}: {
  toast: ToastMessage
  onDismiss: (id: string) => void
}) {
  useEffect(() => {
    const timer = setTimeout(() => onDismiss(toast.id), 5500)
    return () => clearTimeout(timer)
  }, [toast.id, onDismiss])

  const styles =
    toast.kind === 'success'
      ? 'border-grid-green/40 bg-grid-surface'
      : toast.kind === 'error'
        ? 'border-grid-red/50 bg-grid-surface'
        : 'border-grid-blue/40 bg-grid-surface'

  const Icon =
    toast.kind === 'success' ? CheckCircle2 : toast.kind === 'error' ? AlertTriangle : Info
  const iconColor =
    toast.kind === 'success'
      ? 'text-grid-green'
      : toast.kind === 'error'
        ? 'text-grid-red'
        : 'text-grid-blue'

  return (
    <div
      className={`animate-fade-in flex items-start gap-3 rounded-lg border px-4 py-3 shadow-xl shadow-black/40 ${styles}`}
      role="status"
    >
      <Icon className={`mt-0.5 h-5 w-5 shrink-0 ${iconColor}`} />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-semibold text-grid-text">{toast.title}</p>
        {toast.detail && (
          <p className="mt-0.5 text-xs text-grid-muted break-all">{toast.detail}</p>
        )}
      </div>
      <button
        type="button"
        onClick={() => onDismiss(toast.id)}
        className="rounded p-1 text-grid-muted hover:bg-grid-panel hover:text-grid-text"
        aria-label="Închide"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  )
}
