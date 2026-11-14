import { useState, type FormEvent } from 'react'
import { Shield, Zap, Loader2 } from 'lucide-react'
import { login, USE_MOCK } from '../services/api'
import type { AuthSession } from '../types/filegrid'

interface LoginScreenProps {
  onSuccess: (session: AuthSession) => void
}

export function LoginScreen({ onSuccess }: LoginScreenProps) {
  const [email, setEmail] = useState('dispecer@ppc.ro')
  // Mock mode has fixed demo accounts; the real backend's passwords are random per
  // installation (printed by start.sh / start.ps1, stored only in .env).
  const [password, setPassword] = useState(USE_MOCK ? 'dispecer' : '')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const session = await login(email.trim(), password)
      onSuccess(session)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Eroare la autentificare')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="animate-fade-in w-full max-w-md">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-xl border border-grid-cyan/30 bg-grid-panel shadow-lg shadow-cyan-500/10">
            <Zap className="h-7 w-7 text-grid-cyan" />
          </div>
          <h1 className="text-2xl font-bold tracking-tight text-grid-text">FileGrid PPC</h1>
          <p className="mt-1 text-sm text-grid-muted">
            Rețele Electrice România — punct unic de ingestie
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="rounded-xl border border-grid-border bg-grid-surface/90 p-6 shadow-2xl shadow-black/40 backdrop-blur"
        >
          <div className="mb-5 flex items-center gap-2 text-sm text-grid-muted">
            <Shield className="h-4 w-4 text-grid-blue" />
            Autentificare securizată
          </div>

          <label className="mb-1 block text-xs font-medium uppercase tracking-wider text-grid-muted">
            Email
          </label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mb-4 w-full rounded-lg border border-grid-border bg-grid-bg px-3 py-2.5 text-sm text-grid-text outline-none ring-grid-blue focus:ring-2"
            required
            autoComplete="username"
          />

          <label className="mb-1 block text-xs font-medium uppercase tracking-wider text-grid-muted">
            Parolă
          </label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mb-4 w-full rounded-lg border border-grid-border bg-grid-bg px-3 py-2.5 text-sm text-grid-text outline-none ring-grid-blue focus:ring-2"
            required
            autoComplete="current-password"
          />

          {error && (
            <p className="mb-4 rounded-lg border border-grid-red/30 bg-grid-red/10 px-3 py-2 text-xs text-grid-red">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-grid-blue px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-500 disabled:opacity-60"
          >
            {loading && <Loader2 className="h-4 w-4 animate-spin" />}
            Autentificare
          </button>

          <p className="mt-4 text-center text-[11px] text-grid-muted">
            {USE_MOCK ? (
              <>
                Demo: <span className="font-mono text-grid-cyan">dispecer@ppc.ro</span> / dispecer ·{' '}
                <span className="font-mono text-grid-cyan">admin@ppc.ro</span> / admin
              </>
            ) : (
              <>
                Conturi: <span className="font-mono text-grid-cyan">dispecer@ppc.ro</span> ·{' '}
                <span className="font-mono text-grid-cyan">admin@ppc.ro</span> — parolele sunt
                afișate de start.sh / start.ps1 (fișierul .env)
              </>
            )}
          </p>
        </form>
      </div>
    </div>
  )
}
