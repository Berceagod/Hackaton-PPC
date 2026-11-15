import { useCallback, useEffect, useState } from 'react'
import { Header, type AppTab } from './components/Header'
import { LoginScreen } from './components/LoginScreen'
import { ToastStack, type ToastMessage } from './components/Toast'
import { UploadPage } from './pages/UploadPage'
import { FilesPage } from './pages/FilesPage'
import { PoliciesPage } from './pages/PoliciesPage'
import { AnalyticsPage } from './pages/AnalyticsPage'
import { ChaosPage } from './pages/ChaosPage'
import { getStoredSession, logout, SESSION_EXPIRED_EVENT } from './services/api'
import type { AuthSession } from './types/filegrid'

const TAB_IDS: AppTab[] = ['upload', 'files', 'policies', 'analytics', 'chaos']

/** The page lives in the URL (#/files), so a refresh or a shared link stays on the same page. */
function tabFromHash(): AppTab {
  const id = window.location.hash.replace(/^#\/?/, '') as AppTab
  return TAB_IDS.includes(id) ? id : 'upload'
}

export default function App() {
  const [session, setSession] = useState<AuthSession | null>(() => getStoredSession())
  const [tab, setTabState] = useState<AppTab>(() => tabFromHash())
  const [toasts, setToasts] = useState<ToastMessage[]>([])

  const setTab = useCallback((next: AppTab) => {
    setTabState(next)
    if (window.location.hash !== `#/${next}`) window.location.hash = `/${next}`
  }, [])

  useEffect(() => {
    const onHash = () => setTabState(tabFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const pushToast = useCallback((t: Omit<ToastMessage, 'id'>) => {
    setToasts((prev) => [...prev, { ...t, id: crypto.randomUUID() }])
  }, [])

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  useEffect(() => {
    function onExpired() {
      setSession(null) // the page stays in the URL: after logging in again you are back where you were
      pushToast({
        kind: 'error',
        title: 'Sesiunea a expirat',
        detail: 'Autentificați-vă din nou.',
      })
    }
    window.addEventListener(SESSION_EXPIRED_EVENT, onExpired)
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, onExpired)
  }, [pushToast])

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
        {tab === 'chaos' && session.user.role === 'admin' && <ChaosPage pushToast={pushToast} />}
      </main>
      <ToastStack toasts={toasts} onDismiss={dismissToast} />
    </div>
  )
}
