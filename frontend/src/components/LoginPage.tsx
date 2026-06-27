// Login page: shown in place of the dashboard when the user isn't
// authenticated. Always offers the OIDC button; shows the local form
// only when /auth/local/availability reports it as enabled.
//
// No state lives outside this component — when the user lands on /
// authenticated, App.tsx stops rendering this and shows the dashboard.

import { useEffect, useState } from 'react'
import { api, type CurrentUser } from '../api'

interface Props {
  returnTo: string
  onSignedIn: (user: CurrentUser) => void
}

export function LoginPage({ returnTo, onSignedIn }: Props) {
  const [localAvailable, setLocalAvailable] = useState<boolean | null>(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api.localAuthAvailable()
      .then((r) => setLocalAvailable(r.enabled))
      .catch(() => setLocalAvailable(false))
  }, [])

  const onSubmitLocal = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      const user = await api.loginLocal(username, password)
      onSignedIn(user)
    } catch (e2) {
      setErr((e2 as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="h-full flex items-center justify-center p-6">
      <div className="w-full max-w-sm bg-slate-900 rounded-lg border border-slate-800 p-6 shadow-xl">
        <h1 className="text-xl font-bold mb-1">Popping</h1>
        <p className="text-sm text-slate-400 mb-6">Sign in to continue</p>

        {/* OIDC: navigate, not submit — the IdP drives the roundtrip. */}
        <a
          href={api.loginUrl(returnTo)}
          className="block w-full text-center rounded px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white font-medium"
        >
          Sign in with OIDC
        </a>

        {localAvailable && (
          <>
            <div className="flex items-center gap-3 my-5 text-xs text-slate-500">
              <div className="flex-1 h-px bg-slate-800" />
              <span>or</span>
              <div className="flex-1 h-px bg-slate-800" />
            </div>

            <form onSubmit={onSubmitLocal} className="space-y-3">
              <label className="block">
                <span className="text-xs text-slate-400">Username</span>
                <input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="username"
                  className="mt-1 w-full rounded bg-slate-950 border border-slate-800 px-3 py-2 text-sm focus:outline-none focus:border-slate-600"
                />
              </label>
              <label className="block">
                <span className="text-xs text-slate-400">Password</span>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  className="mt-1 w-full rounded bg-slate-950 border border-slate-800 px-3 py-2 text-sm focus:outline-none focus:border-slate-600"
                />
              </label>
              <button
                type="submit"
                disabled={busy || !username || !password}
                className="w-full rounded px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-100 text-sm font-medium disabled:opacity-50"
              >
                {busy ? 'Signing in…' : 'Sign in'}
              </button>
            </form>
          </>
        )}

        {err && (
          <div className="mt-4 px-3 py-2 rounded bg-red-900/40 border border-red-800 text-xs text-red-200">
            {err}
          </div>
        )}
      </div>
    </div>
  )
}