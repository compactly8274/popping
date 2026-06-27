// Tiny header chip showing login state.
//
// When OIDC is disabled on the backend, /auth/me 404s; the parent detects
// that and passes ``user=null`` with ``oidcDisabled=true`` so we render
// nothing. When OIDC is enabled and the user isn't logged in, we render a
// "Sign in" link. When logged in, name + sign-out button.

import type { CurrentUser } from '../api'
import { api } from '../api'

interface Props {
  user: CurrentUser | null
  oidcDisabled: boolean
  onChange: (user: CurrentUser | null) => void
}

export function AuthChip({ user, oidcDisabled, onChange }: Props) {
  if (oidcDisabled) return null

  if (!user) {
    return (
      <a
        href={api.loginUrl('/')}
        className="rounded px-3 py-1 text-sm bg-slate-800 hover:bg-slate-700 text-slate-200"
      >
        Sign in
      </a>
    )
  }

  const onSignOut = async () => {
    try {
      await api.logout()
    } finally {
      onChange(null)
    }
  }

  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-slate-300">{user.name || user.email}</span>
      <button
        onClick={onSignOut}
        className="rounded px-2 py-0.5 bg-slate-800 hover:bg-slate-700 text-slate-300"
      >
        Sign out
      </button>
    </div>
  )
}