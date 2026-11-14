import { useCallback, useState } from 'react'
import { Header, type AppTab } from './components/Header'
import { LoginScreen } from './components/LoginScreen'
import { ToastStack, type ToastMessage } from './components/Toast'
import { UploadPage } from './pages/UploadPage'
import { FilesPage } from './pages/FilesPage'
import { PoliciesPage } from './pages/PoliciesPage'
import { AnalyticsPage } from './pages/AnalyticsPage'
import { getStoredSession, logout } from './services/api'
import type { AuthSession } from './types/filegrid'

export default function App() {
  const [session, setSession] = useState<AuthSession | null>(() => getStoredSession())
  const [tab, setTab] = useState<AppTab>('upload')
  const [toasts, setToasts] = useState<ToastMessage[]>([])

  const pushToast = useCallback((t: Omit<ToastMessage, 'id'>) => {
    setToasts((prev) => [...prev, { ...t, id: crypto.randomUUID() }])
  }, [])

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  function handleLogout() {
    logout()
    setSession(null)
    setTab('upload')
  }

  if (!session) {
    return (
      <>
        <LoginScreen onSuccess={setSession} />
        <ToastStack toasts={toasts} onDismiss={dismissToast} />
      </>
    )
  }

  return (
    <div className="min-h-screen">
      <Header
        user={session.user}
        activeTab={tab}
        onTabChange={setTab}
        onLogout={handleLogout}
      />
      <main className="mx-auto max-w-7xl px-4 py-6">
        {tab === 'upload' && <UploadPage pushToast={pushToast} />}
        {tab === 'files' && <FilesPage pushToast={pushToast} />}
        {tab === 'policies' && <PoliciesPage pushToast={pushToast} />}
        {tab === 'analytics' && <AnalyticsPage pushToast={pushToast} />}
      </main>
      <ToastStack toasts={toasts} onDismiss={dismissToast} />
    </div>
  )
}
