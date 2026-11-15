import {
  Zap,
  LogOut,
  Activity,
  FlaskConical,
  User,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { getHealth, USE_MOCK, type ApiHealth } from '../services/api'
import type { AuthUser } from '../types/filegrid'

export type AppTab = 'upload' | 'files' | 'policies' | 'analytics' | 'chaos'

const TABS: { id: AppTab; label: string; adminOnly?: boolean }[] = [
  { id: 'upload', label: 'Încărcare' },
  { id: 'files', label: 'Fișiere & Audit' },
  { id: 'policies', label: 'Politici Rutare' },
  { id: 'analytics', label: 'Analytics' },
  { id: 'chaos', label: 'Chaos', adminOnly: true },
]

interface HeaderProps {
  user: AuthUser
  activeTab: AppTab
  onTabChange: (tab: AppTab) => void
  onLogout: () => void
}

const HEALTH_BADGE: Record<ApiHealth, { label: string; className: string }> = {
  ok: { label: 'API Online', className: 'border-grid-green/25 bg-grid-green/10 text-grid-green' },
  degraded: {
    label: 'API Degradat',
    className: 'border-grid-amber/30 bg-grid-amber/10 text-grid-amber',
  },
  offline: { label: 'API Offline', className: 'border-grid-red/30 bg-grid-red/10 text-grid-red' },
}

const HEALTH_POLL_MS = 15_000

export function Header({ user, activeTab, onTabChange, onLogout }: HeaderProps) {
  const [health, setHealth] = useState<ApiHealth>('ok')

  useEffect(() => {
    let alive = true
    async function poll() {
      const status = await getHealth()
      if (alive) setHealth(status)
    }
    void poll()
    const timer = window.setInterval(() => void poll(), HEALTH_POLL_MS)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  return (
    <header className="sticky top-0 z-40 border-b border-grid-border/80 bg-grid-bg/85 backdrop-blur-md">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-grid-cyan/30 bg-grid-panel">
            <Zap className="h-5 w-5 text-grid-cyan" />
          </div>
          <div>
            <div className="text-sm font-bold tracking-tight text-grid-text">FileGrid PPC</div>
            <div className="text-[11px] text-grid-muted">Rețele Electrice România</div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px] font-medium ${HEALTH_BADGE[health].className}`}
          >
            <Activity className="h-3 w-3 animate-pulse-soft" />
            {HEALTH_BADGE[health].label}
          </span>
          {USE_MOCK && (
            <span className="inline-flex items-center gap-1.5 rounded-md border border-grid-amber/30 bg-grid-amber/10 px-2.5 py-1 text-[11px] font-medium text-grid-amber">
              <FlaskConical className="h-3 w-3" />
              Mock Mode Active
            </span>
          )}
          <span className="inline-flex items-center gap-1.5 rounded-md border border-grid-border bg-grid-panel px-2.5 py-1 text-[11px] text-grid-muted">
            <User className="h-3 w-3 text-grid-blue" />
            <span className="font-medium text-grid-text">{user.name}</span>
            <span className="text-grid-muted">· {user.role}</span>
          </span>
          <button
            type="button"
            onClick={onLogout}
            className="inline-flex items-center gap-1.5 rounded-md border border-grid-border px-2.5 py-1 text-[11px] text-grid-muted transition hover:border-grid-red/40 hover:text-grid-red"
          >
            <LogOut className="h-3 w-3" />
            Logout
          </button>
        </div>
      </div>

      <nav className="mx-auto flex max-w-7xl gap-1 overflow-x-auto px-4 pb-2 scrollbar-thin">
        {TABS.filter((tab) => !tab.adminOnly || user.role === 'admin').map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => onTabChange(tab.id)}
            className={`whitespace-nowrap rounded-lg px-3.5 py-2 text-sm font-medium transition ${
              activeTab === tab.id
                ? 'bg-grid-blue/15 text-grid-cyan'
                : 'text-grid-muted hover:bg-grid-panel hover:text-grid-text'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </nav>
    </header>
  )
}
