// Small user badge in the header — replaces the old AuthChip now that
// the unauthenticated state is handled by LoginPage. Shows the user's
// name and a sign-out button; renders nothing when OIDC is off.
//
// ``auth_method`` values:
//   "oidc"     — real OIDC login. Sign-out works (deletes the session row).
//   "local"    — POST /auth/local login. Same sign-out semantics.
//   "bypass"   — synthetic user from LOCAL_AUTH_BYPASS. The cookie was
//                never set, so sign-out is a no-op against the server
//                but we still flip local state so the badge disappears.

import type { CurrentUser } from '../api'
import { api } from '../api'

interface Props {
  user: CurrentUser
  onSignedOut: () => void
}

export function UserBadge({ user, onSignedOut }: Props) {
  const onSignOut = async () => {
    try {
      // The server returns 204 for both real sessions and the bypass
      // synthetic user (which has no row to delete). Either way, the
      // caller should clear local state.
      await api.logout()
    } finally {
      onSignedOut()
    }
  }

  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-slate-300">
        {user.name || user.email}
        {user.auth_method === 'bypass' && (
          <span className="ml-1 text-slate-500">(local bypass)</span>
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