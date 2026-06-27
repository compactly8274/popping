// Small user badge in the header — replaces the old AuthChip now that
// the unauthenticated state is handled by LoginPage. Shows the user's
// name and a sign-out button; renders nothing when OIDC is off.

import type { CurrentUser } from '../api'
import { api } from '../api'

interface Props {
  user: CurrentUser
  onSignedOut: () => void
}

export function UserBadge({ user, onSignedOut }: Props) {
  const onSignOut = async () => {
    try {
      await api.logout()
    } finally {
      onSignedOut()
    }
  }

  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-slate-300">
        {user.name || user.email}
        {user.auth_method === 'loopback' && (
          <span className="ml-1 text-slate-500">(loopback)</span>
        )}
      </span>
      <button
        onClick={onSignOut}
        className="rounded px-2 py-0.5 bg-slate-800 hover:bg-slate-700 text-slate-300"
      >
        Sign out
      </button>
    </div>
  )
}