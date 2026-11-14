import {
  Zap,
  LogOut,
  Activity,
  FlaskConical,
  User,
} from 'lucide-react'
import { USE_MOCK } from '../services/api'
import type { AuthUser } from '../types/filegrid'

export type AppTab = 'upload' | 'files' | 'policies' | 'analytics'

const TABS: { id: AppTab; label: string }[] = [
  { id: 'upload', label: 'Încărcare' },
  { id: 'files', label: 'Fișiere & Audit' },
  { id: 'policies', label: 'Politici Rutare' },
  { id: 'analytics', label: 'Analytics' },
]

interface HeaderProps {
  user: AuthUser
  activeTab: AppTab
  onTabChange: (tab: AppTab) => void
  onLogout: () => void
}

export function Header({ user, activeTab, onTabChange, onLogout }: HeaderProps) {
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
          <span className="inline-flex items-center gap-1.5 rounded-md border border-grid-green/25 bg-grid-green/10 px-2.5 py-1 text-[11px] font-medium text-grid-green">
            <Activity className="h-3 w-3 animate-pulse-soft" />
            API Online
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
        {TABS.map((tab) => (
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
