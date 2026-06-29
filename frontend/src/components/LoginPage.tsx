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
    <div className="h-full flex items-center justify-center p-6 bg-bg-app">
      <div className="w-full max-w-sm rounded-ios-lg bg-bg-surface border border-hairline p-6 shadow-2xl">
        <h1 className="text-ios-large-title font-bold mb-1 text-label-primary tracking-tight">
          Popping
        </h1>
        <p className="text-ios-body text-label-secondary mb-6">Sign in to continue</p>

        {/* OIDC: navigate, not submit — the IdP drives the roundtrip. */}
        <a
          href={api.loginUrl(returnTo)}
          className="block w-full text-center min-h-[44px] leading-[44px] rounded-ios bg-accent active:opacity-80 text-white text-ios-body font-medium"
        >
          Sign in with OIDC
        </a>

        {localAvailable && (
          <>
            <div className="flex items-center gap-3 my-5 text-ios-caption text-label-secondary">
              <div className="flex-1 h-px bg-hairline" />
              <span>or</span>
              <div className="flex-1 h-px bg-hairline" />
            </div>

            <form onSubmit={onSubmitLocal} className="space-y-3">
              <label className="block">
                <span className="text-ios-caption uppercase tracking-wide text-label-tertiary">
                  Username
                </span>
                <input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="username"
                  className="mt-1 w-full min-h-[44px] rounded-ios bg-bg-elevated border border-hairline px-3 text-ios-body text-label-primary placeholder:text-label-tertiary focus:outline-none focus:border-accent"
                />
              </label>
              <label className="block">
                <span className="text-ios-caption uppercase tracking-wide text-label-tertiary">
                  Password
                </span>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  className="mt-1 w-full min-h-[44px] rounded-ios bg-bg-elevated border border-hairline px-3 text-ios-body text-label-primary placeholder:text-label-tertiary focus:outline-none focus:border-accent"
                />
              </label>
              <button
                type="submit"
                disabled={busy || !username || !password}
                className="w-full min-h-[44px] rounded-ios bg-bg-elevated active:bg-bg-surface text-label-primary text-ios-body font-medium disabled:opacity-40"
              >
                {busy ? 'Signing in…' : 'Sign in'}
              </button>
            </form>
          </>
        )}

        {err && (
          <div className="mt-4 px-3 py-2 rounded-ios bg-red-500/15 border border-red-500/40 text-ios-caption text-red-200">
            {err}
          </div>
        )}
      </div>
    </div>
  )
}