// One card: headline link, source name, relative time, score badge.
// When the entry has a thumbnail (parsed from the feed at ingest), a
// 96 px square renders to the right of the title — feeds without
// thumbnails keep the original compact layout.
//
// UX features layered on top of the basic card:
//   - `↗` glyph + tooltip signals "opens in new tab" so the user
//     isn't surprised when their back button doesn't go back.
//   - Long-press on mobile / right-click on desktop copies the URL
//     to the clipboard via the global Toast singleton.
//   - `unread` toggles a soft blue ring + opacity boost so the user
//     can scan for what they haven't read yet. Defaults to "read"
//     so cards that haven't been computed (e.g. before F2 lands on
//     a given column) don't all flash unread.
//   - Swipe right = mark read, swipe left = hide (Apollo/iOS Mail
//     style). See the SWIPE_* constants below for the tuning —
//     there's a dead zone + a direction lock so an intentional
//     vertical scroll never gets mistaken for a swipe, and a firm
//     commit threshold so a short/accidental drag always snaps back
//     with no action taken. The buttons below stay as an explicit
//     backup for anyone who doesn't want to use the gesture.

import { memo, useEffect, useRef, useState, type MouseEvent, type TouchEvent } from 'react'
import { api, type Entry } from '../api'
import { recordBatched, recordImmediate } from '../lib/interactions'
import { SourceIcon, stableHue } from './SourceIcon'
import { toast } from './Toast'

type Props = {
  entry: Entry
  sourceName?: string
  // Source favicon path (relative to /assets), or null if not
  // fetched yet. Used only as a thumbnail FALLBACK when the entry
  // itself has no image — see ThumbnailFallback below. Undefined
  // (prop omitted) and null (fetched-but-empty) both degrade the
  // same way: a colored letter tile instead of the site's icon.
  sourceFaviconPath?: string | null
  unread?: boolean
  // True when this card is the keyboard-selected card. Drives the
  // focus ring. Set by App's keyboard handler.
  selected?: boolean
  // Ref-callback so App can focus/scroll into view on keyboard nav.
  // Optional — most cards never need it.
  cardRef?: (el: HTMLElement | null) => void
  onActivate?: () => void
  // Source category — drives the colored left stripe. Optional; when
  // absent the card just renders without a stripe. The Column passes
  // it through from ``sourcesByMeta``.
  category?: string
  // Per-card mark-read. When present, an inline ✓ button renders
  // on the card so the user can dim one entry without nuking the
  // column. Tapping ✓ on an already-dim card is a no-op up at
  // App.markEntryRead.
  //
  // The button is ALWAYS visible (not hover-only) so the user
  // can see at a glance that "I can mark this as read" is an
  // option — the hover-only treatment hid the affordance from
  // anyone who didn't already know it existed, which defeated
  // the point of having a per-card action.
  onMarkRead?: () => void
  // Per-card "hide" action. When present, the context menu
  // (right-click / long-press) gets a "Hide this entry" item
  // that adds the entry to the user's hidden set. The card
  // immediately disappears from every column + the For You
  // row. The action is reversible only by clearing the hidden
  // set (a future "show hidden" affordance in Settings).
  onHide?: () => void
  // Per-card "star" / "unstar" action. When present, the
  // context menu gets "Save for later" / "Unsave" items, and
  // a star button renders on the card next to the mark-read
  // checkmark. The card's starred state is reflected in the
  // button's filled/outline styling.
  onStar?: () => void
  // True if this entry is currently starred. Drives the
  // star-button fill (filled = starred) and the context menu
  // label toggle. Optional — when absent the star button
  // renders in its empty (unstarred) state.
  starred?: boolean
  // Per-card "hide" / "unhide" action. When present, the
  // card gets a visible eye button next to the mark-read
  // checkmark. Open eye = visible, closed eye = hidden. The
  // button's state is driven by the ``hidden`` prop and the
  // click toggles via ``onHide``. The same handler is used
  // for the context menu's "Hide this entry" / "Unhide"
  // item, so both entry points stay in sync.
  onHideToggle?: () => void
  // True if this entry is currently hidden. Drives the
  // eye-button icon (open vs closed) and the context menu
  // label toggle. Optional — when absent the eye button
  // renders in its "visible" state (open eye) and the
  // context menu shows the hide action unconditionally.
  hidden?: boolean
  // Per-card inline summary. When ``expanded`` is true the card
  // fetches the cached summary once and renders it under the
  // title. ``onToggleSummary`` flips the expanded bit. Independent
  // of mark-read — expanding a card doesn't mark it.
  expanded?: boolean
  onToggleSummary?: () => void
  // Per-card vote. ``vote`` is the entry's current persisted
  // direction (null = no vote) — drives the filled/unfilled arrow
  // icon and the button's tinted background so the user can see at
  // a glance which way (if any) they've already voted. ``onVote``
  // receives the FINAL desired direction, already toggled — Card
  // computes "clicking the same direction again clears it" itself
  // (it has the current ``vote`` value in scope) so the caller
  // (App.tsx) doesn't need matching toggle logic.
  vote?: 'up' | 'down' | null
  onVote?: (direction: 'up' | 'down' | null) => void
}

// Map a category name to a Tailwind background class for the left
// stripe. Kept inline (no new module) because the call site is the
// only consumer and we want it obvious from Card.tsx which colors
// map to which categories. ``other`` falls through to a neutral
// slate so unrecognized categories don't render without a stripe.
// Full opacity (not the old /70) — paired with the wider 3px stripe
// below, this reads as a deliberate accent rather than a faint tick
// mark, closer to how Apollo's per-community color bar pops against
// a dark background.
function categoryStripeClass(category: string | undefined): string {
  switch (category) {
    case 'news':     return 'bg-blue-500'
    case 'tech':     return 'bg-violet-500'
    case 'vulns':    return 'bg-red-500'
    case 'science':  return 'bg-emerald-500'
    case 'finance':  return 'bg-amber-500'
    case 'policy':   return 'bg-cyan-500'
    case 'longform': return 'bg-rose-500'
    case 'deals':    return 'bg-lime-500'
    case 'podcast':  return 'bg-orange-500'
    case 'video':    return 'bg-pink-500'
    default:         return 'bg-neutral-600'
  }
}

// Same category -> hue mapping as the stripe, but as a text color
// for the source name in the meta row. Previously the source name
// used ``text-label-primary`` — visually identical to the title
// right above it, so the two blurred into one gray mass ("looks
// like a spreadsheet"). Tinting the source name gives every card an
// immediate, scannable category identity without adding a second
// element, and echoes the stripe color so the two reinforce each
// other rather than introducing a third, unrelated color. The 400
// shade (one step lighter than the stripe's 500) reads better as
// body text against the dark background than the more saturated 500
// would at this size.
function categorySourceTextClass(category: string | undefined): string {
  switch (category) {
    case 'news':     return 'text-blue-400'
    case 'tech':     return 'text-violet-400'
    case 'vulns':    return 'text-red-400'
    case 'science':  return 'text-emerald-400'
    case 'finance':  return 'text-amber-400'
    case 'policy':   return 'text-cyan-400'
    case 'longform': return 'text-rose-400'
    case 'deals':    return 'text-lime-400'
    case 'podcast':  return 'text-orange-400'
    case 'video':    return 'text-pink-400'
    default:         return 'text-label-primary'
  }
}

function timeAgo(iso: string | null): string {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  if (ms < 0) return 'just now'
  const mins = Math.floor(ms / 60000)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

// Podcast episode duration, MM:SS under an hour and H:MM:SS past it
// — matches how every podcast app formats episode length.
function formatDuration(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600)
  const m = Math.floor((totalSeconds % 3600) / 60)
  const s = Math.floor(totalSeconds % 60)
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  const ss = String(s).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`
}

// Score bands. Each tier is a gradient now — the flat bg-score-*
// colors in the previous config were serviceable but felt like
// badges stuck on a sticker. Gradients give a tiny bit of depth
// without crossing into skeuomorphism.
function scoreBand(score: number): { color: string; label: string } {
  if (score >= 75) return { color: 'bg-gradient-to-br from-red-500 to-red-700',   label: 'hot' }
  if (score >= 50) return { color: 'bg-gradient-to-br from-amber-400 to-amber-600', label: 'warm' }
  if (score >= 25) return { color: 'bg-gradient-to-br from-accent to-blue-700',   label: 'cool' }
  return               { color: 'bg-gradient-to-br from-neutral-500 to-neutral-700',   label: 'cold' }
}

// There's no dedicated "un-vote" interaction type on the backend —
// the interactions table is an append-only log, and thumb_up/
// thumb_down are the only vote-shaped types it knows. So clearing or
// switching a vote is expressed as a CANCEL event (the inverse of
// whatever was previously recorded) followed by an APPLY event (the
// new direction, if any) — both land in the same log the backend
// already aggregates for the source net-score and the preference-
// vector recompute, and a cancel-then-apply pair nets to exactly the
// right delta without any backend changes:
//   up -> null (toggle off):   [thumb_down]                 net -1
//   down -> null (toggle off): [thumb_up]                   net +1
//   null -> up:                [thumb_up]                   net +1
//   null -> down:               [thumb_down]                net -1
//   up -> down (switch):       [thumb_down, thumb_down]      net -2
//   down -> up (switch):       [thumb_up, thumb_up]          net +2
function voteInteractionEvents(
  prev: 'up' | 'down' | null,
  next: 'up' | 'down' | null,
): Array<'thumb_up' | 'thumb_down'> {
  if (prev === next) return []
  const events: Array<'thumb_up' | 'thumb_down'> = []
  if (prev === 'up') events.push('thumb_down')
  if (prev === 'down') events.push('thumb_up')
  if (next === 'up') events.push('thumb_up')
  if (next === 'down') events.push('thumb_down')
  return events
}

// Long-press threshold. ~500ms is the conventional "long enough to
// mean something, short enough not to feel laggy". The 10px move
// tolerance lets a finger jitter slightly without cancelling.
const LONG_PRESS_MS = 500
const LONG_PRESS_MOVE_TOLERANCE_PX = 10

// Swipe-to-act tuning. The previous mobile gesture (swipe to change
// column, in App.tsx) had none of this — any 60px horizontal delta
// fired regardless of direction, which is what made it feel like a
// hair trigger. This one is deliberately firmer:
//   - DEAD_ZONE: below this, nothing happens yet (absorbs finger
//     jitter and the first few px of every scroll attempt).
//   - DIRECTION_RATIO: once past the dead zone, the gesture only
//     locks into a horizontal swipe if the horizontal delta clearly
//     dominates the vertical one. Otherwise it's treated as a
//     vertical scroll and the card ignores the rest of the touch —
//     an intentional scroll never gets hijacked into a swipe.
//   - MAX_REVEAL: the card can't be dragged past this; further
//     finger movement just holds it there (no rubber-band physics,
//     keeps the interaction predictable).
//   - COMMIT: how far (out of MAX_REVEAL) the drag has to go before
//     release fires the action. ~66% of the max reveal — anything
//     short of that snaps back with no action taken.
const SWIPE_DEAD_ZONE_PX = 12
const SWIPE_DIRECTION_RATIO = 1.3
const SWIPE_MAX_REVEAL_PX = 88
const SWIPE_COMMIT_PX = 58

export function CardInner({ entry, sourceName, sourceFaviconPath, unread, selected, cardRef, onActivate, category, onMarkRead, expanded, onToggleSummary, onHide, onStar, starred, onHideToggle, hidden, vote, onVote }: Props) {
  const band = scoreBand(entry.composite_score)
  const stripeClass = categoryStripeClass(category)
  const sourceTextClass = categorySourceTextClass(category)
  // Touch tracking for long-press → copy URL.
  // Kept in refs so the values don't trigger re-renders mid-press.
  const touchStart = useRef<{ x: number; y: number; t: number; id: number; onInteractiveChild: boolean } | null>(null)
  const longPressTimer = useRef<number | null>(null)

  // Swipe-to-act state. Refs, not useState — a touchmove-driven
  // re-render for every pixel of drag would be needlessly expensive
  // across a list of 20-50 cards. Visuals are applied imperatively
  // (see applySwipeVisual) and only the final commit/cancel touches
  // React state, via the same onMarkRead / onHideToggle callbacks
  // the buttons already use.
  const articleRef = useRef<HTMLElement | null>(null)
  const readRevealRef = useRef<HTMLDivElement | null>(null)
  const hideRevealRef = useRef<HTMLDivElement | null>(null)
  const swipeDx = useRef(0)
  const swipeLock = useRef<'none' | 'horizontal' | 'vertical'>('none')

  // Stable DOM-ref callback. The Column passes a fresh lambda per
  // card per render; we stash the latest in a ref and forward
  // through a single closure that never changes identity. React's
  // ref semantics compare by identity, so a stable wrapper means
  // no re-attach on every parent re-render — and combined with
  // ``Card.memo`` this keeps the card's DOM truly stable across
  // polls that don't change any of its data props.
  const cardRefLatest = useRef<typeof cardRef>(cardRef)
  cardRefLatest.current = cardRef
  const stableCardRef = useRef((el: HTMLElement | null) => {
    articleRef.current = el
    cardRefLatest.current?.(el)
  }).current

  // Summary panel state. ``null`` means we haven't loaded yet (or
  // haven't tried); the string is the cleaned text; ``false`` marks
  // a failed request so we don't loop on retries every time the
  // card re-renders. The fetch only fires on the first expand —
  // subsequent re-renders with ``expanded=true`` are a no-op until
  // the user collapses + re-expands the same card.
  const [summary, setSummary] = useState<string | null>(null)
  const [summaryError, setSummaryError] = useState(false)

  // Podcast transcript summary. Separate local toggle (not lifted to
  // App like ``expanded``/``onToggleSummary`` — there's no keyboard
  // shortcut for it, so plain component state is simpler) and
  // separate cache state, since a podcast entry can have both a
  // regular feed summary (meta.summary) and a transcript summary,
  // and they're different content the user might want independently.
  const [podcastSummaryExpanded, setPodcastSummaryExpanded] = useState(false)
  const [podcastSummary, setPodcastSummary] = useState<string | null>(null)
  const [podcastSummaryError, setPodcastSummaryError] = useState(false)
  const [podcastSummaryUnavailable, setPodcastSummaryUnavailable] = useState(false)

  // Mount timestamp. Reset on every mount via useEffect so the
  // dwell counter starts at 0 for each card instance (not for
  // each unique entry id \u2014 a re-mount on the same entry after a
  // filter toggle is a fresh "dwell" interval from the user's
  // perspective).
  const mountTime = useRef<number>(Date.now())
  useEffect(() => {
    mountTime.current = Date.now()
  }, [entry.id])

  useEffect(() => {
    if (!expanded) return
    // Skip when the card is already showing a summary or when an
    // earlier attempt failed. The latter stops a flapping card from
    // firing one request per render — collapsing + re-expanding
    // resets neither state, by design; a fresh attempt would
    // require the user to reload the page or wait for the entry
    // to disappear and re-appear.
    if (summary !== null) return
    if (summaryError) return
    let cancelled = false
    api
      .entrySummary(entry.id)
      .then((r) => {
        if (cancelled) return
        setSummary(r.summary ?? '')
      })
      .catch(() => {
        if (cancelled) return
        setSummaryError(true)
      })
    return () => {
      cancelled = true
    }
  }, [expanded, entry.id, summary, summaryError])

  useEffect(() => {
    if (!podcastSummaryExpanded) return
    if (podcastSummary !== null) return
    if (podcastSummaryError) return
    if (podcastSummaryUnavailable) return
    let cancelled = false
    api
      .podcastSummary(entry.id)
      .then((r) => {
        if (cancelled) return
        if (!r.available) {
          setPodcastSummaryUnavailable(true)
          return
        }
        setPodcastSummary(r.summary ?? '')
      })
      .catch(() => {
        if (cancelled) return
        setPodcastSummaryError(true)
      })
    return () => {
      cancelled = true
    }
  }, [podcastSummaryExpanded, entry.id, podcastSummary, podcastSummaryError, podcastSummaryUnavailable])

  // Record one ``view`` event per (entry, session). ``recordBatched``
  // dedups internally so even if React strict-mode mounts the card
  // twice in dev, only one event lands. Effect fires on mount and
  // when the entry id changes; no dependency on entry itself — we
  // only care that the card is on screen.
  useEffect(() => {
    recordBatched({ entry_id: entry.id, type: 'view' })
  }, [entry.id])

  const clearLongPress = () => {
    if (longPressTimer.current != null) {
      window.clearTimeout(longPressTimer.current)
      longPressTimer.current = null
    }
  }

  // Push the current drag offset onto the DOM directly (no React
  // state) so a fast finger doesn't queue a render per pixel.
  // ``animate`` adds a short transition for the release snap;
  // during an active drag we want 1:1 finger tracking, so it's
  // omitted on every touchmove call.
  const applySwipeVisual = (dx: number, animate: boolean) => {
    const el = articleRef.current
    if (el) {
      el.style.transition = animate ? 'transform 180ms ease-out' : ''
      el.style.transform = dx === 0 ? '' : `translateX(${dx}px)`
    }
    const progress = Math.min(1, Math.abs(dx) / SWIPE_MAX_REVEAL_PX)
    if (readRevealRef.current) {
      readRevealRef.current.style.opacity = dx > 0 ? String(progress) : '0'
    }
    if (hideRevealRef.current) {
      hideRevealRef.current.style.opacity = dx < 0 ? String(progress) : '0'
    }
  }

  const resetSwipe = (animate: boolean) => {
    swipeDx.current = 0
    swipeLock.current = 'none'
    applySwipeVisual(0, animate)
  }

  // Mirrors the checkmark button's onClick (below) exactly — same
  // event, same callback — so a swipe-right and a tap produce an
  // identical outcome.
  const commitMarkRead = () => {
    if (!onMarkRead) return
    const dwellMs = Math.min(Date.now() - mountTime.current, 5 * 60 * 1000)
    recordImmediate({ entry_id: entry.id, type: 'view' })
    recordBatched({ entry_id: entry.id, type: 'dwell', value: dwellMs })
    onMarkRead()
  }

  // Mirrors the eye button's onClick (below) exactly.
  const commitHideToggle = () => {
    if (!onHideToggle) return
    if (!hidden) {
      recordImmediate({ entry_id: entry.id, type: 'never' })
    }
    onHideToggle()
  }

  const onTouchStart = (e: TouchEvent<HTMLElement>) => {
    // Single finger only. Two-finger gestures (pinch, etc.) skip the
    // long-press AND swipe paths entirely — we don't want to fire
    // copy or a swipe action on pinch.
    if (e.touches.length !== 1) {
      clearLongPress()
      resetSwipe(false)
      touchStart.current = null
      return
    }
    const t = e.touches[0]
    // Mark whether the touch originated on the thumbnail (or any
    // other interactive child like the mark-read / star / eye
    // buttons). Both the long-press-copy path and the swipe path
    // skip when this is set, so dragging a button doesn't also
    // drag the card underneath it.
    const target = e.target as HTMLElement
    const onInteractiveChild = !!target.closest('[data-card-interactive]')
    touchStart.current = {
      x: t.clientX,
      y: t.clientY,
      t: Date.now(),
      id: t.identifier,
      onInteractiveChild,
    }
    resetSwipe(false)
    if (onInteractiveChild) {
      // Don't arm the timer at all — saves the cleanup path too.
      return
    }
    clearLongPress()
    longPressTimer.current = window.setTimeout(() => {
      // Re-check the touch is still the same finger and roughly in
      // place. If the user has already started swiping we bail so
      // we don't fire copy mid-swipe.
      const start = touchStart.current
      if (!start || start.onInteractiveChild || swipeLock.current === 'horizontal') return
      longPressTimer.current = null
      void copyUrl(entry.url)
    }, LONG_PRESS_MS)
  }

  // Native (non-passive) touchmove listener. JSX's onTouchMove is
  // registered passive by React for scroll performance, which means
  // preventDefault() inside it is silently ignored — the page would
  // keep scrolling underneath an in-progress card swipe. Drawer.tsx
  // hit the same issue for its swipe-to-dismiss and solved it the
  // same way: a manual, non-passive listener via addEventListener.
  useEffect(() => {
    const el = articleRef.current
    if (!el) return
    const onMove = (e: globalThis.TouchEvent) => {
      const start = touchStart.current
      if (!start || start.onInteractiveChild || e.touches.length !== 1) return
      const t = e.touches[0]
      const dx = t.clientX - start.x
      const dy = t.clientY - start.y

      if (swipeLock.current === 'none') {
        if (Math.abs(dx) < SWIPE_DEAD_ZONE_PX && Math.abs(dy) < SWIPE_DEAD_ZONE_PX) {
          // Still inside the dead zone — could be a tap, could be
          // the start of a scroll or a swipe. Wait for more signal.
          if (Math.abs(dx) > LONG_PRESS_MOVE_TOLERANCE_PX || Math.abs(dy) > LONG_PRESS_MOVE_TOLERANCE_PX) {
            clearLongPress()
          }
          return
        }
        clearLongPress()
        if (Math.abs(dx) > Math.abs(dy) * SWIPE_DIRECTION_RATIO) {
          swipeLock.current = 'horizontal'
        } else {
          // Vertical intent — this is a scroll, not a swipe. Don't
          // touch it again for the rest of this touch sequence.
          swipeLock.current = 'vertical'
          return
        }
      }
      if (swipeLock.current !== 'horizontal') return

      // A right-swipe with no onMarkRead wired (or a left-swipe with
      // no onHideToggle) has nothing to commit to — clamp to 0 so
      // the card doesn't visually drag in a direction that does
      // nothing on release.
      let clamped = dx
      if (dx > 0 && !onMarkRead) clamped = 0
      if (dx < 0 && !onHideToggle) clamped = 0
      clamped = Math.max(-SWIPE_MAX_REVEAL_PX, Math.min(SWIPE_MAX_REVEAL_PX, clamped))
      if (clamped !== 0) {
        // Only now, once we're actually dragging the card, block the
        // page from also scrolling underneath the gesture.
        e.preventDefault()
      }
      swipeDx.current = clamped
      applySwipeVisual(clamped, false)
    }
    el.addEventListener('touchmove', onMove, { passive: false })
    return () => el.removeEventListener('touchmove', onMove)
    // onMarkRead / onHideToggle are read fresh from the closure each
    // effect run (the deps array below re-attaches the listener
    // whenever either identity changes, which — per Card.memo's
    // custom comparator — is only when the Column re-derives its
    // per-card callbacks, not on every poll).
  }, [onMarkRead, onHideToggle])

  const onTouchEnd = (e: TouchEvent<HTMLElement>) => {
    const start = touchStart.current
    clearLongPress()
    const wasSwiping = swipeLock.current === 'horizontal'
    const finalDx = swipeDx.current
    if (wasSwiping) {
      resetSwipe(true)
      if (Math.abs(finalDx) >= SWIPE_COMMIT_PX) {
        e.preventDefault()
        e.stopPropagation()
        if (finalDx > 0) commitMarkRead()
        else commitHideToggle()
      }
    }
    swipeLock.current = 'none'
    if (!start) return
    touchStart.current = null
    // If the press held LONG_PRESS_MS the timer already fired (and we
    // showed the toast). Cancel the click that would otherwise happen
    // when the finger lifts — opening the link after a copy would be
    // confusing. ``longPressTimer.current === null`` is the
    // authoritative "the timer fired" check — the timer handler
    // nulls the ref before calling ``copyUrl``. Skipping the
    // long-press path (because the touch started on an interactive
    // child like the thumbnail) leaves the ref set, so the click
    // suppression correctly doesn't fire — a long tap on the
    // thumbnail opens the article as the user expects.
    const timerFired = longPressTimer.current === null
    const dur = Date.now() - start.t
    if (!wasSwiping && timerFired && dur >= LONG_PRESS_MS) {
      e.preventDefault()
      e.stopPropagation()
    }
  }

  const onTouchCancel = () => {
    clearLongPress()
    resetSwipe(true)
    touchStart.current = null
  }

  // Right-click → small context menu. Native context menu is
  // suppressed so we can offer "mark as read" + "hide" + "copy
  // link" + "open in new tab" without a third-party library.
  //
  // Actions are conditionally included: ``mark as read`` only
  // when ``onMarkRead`` is wired AND the card is still unread
  // (no point offering the action on a card that's already
  // dim), ``hide`` only when ``onHide`` is wired. Copy/open are
  // always available.
  const onContextMenu = (e: MouseEvent<HTMLElement>) => {
    e.preventDefault()
    const actions: Array<{ label: string; onClick: () => void }> = []
    if (onMarkRead && unread) {
      actions.push({
        label: 'Mark as read',
        onClick: () => {
          recordImmediate({ entry_id: entry.id, type: 'view' })
          onMarkRead()
        },
      })
    }
    if (onHideToggle) {
      // Context-menu hide uses the SAME handler as the
      // eye button: hides + marks read. The entry
      // moves from Fresh to History in the same
      // column, so the user can still see it (faded)
      // and the column stays populated. The label
      // flips based on the current state so the menu
      // tells the user what the action will do.
      //
      // (The previous implementation used a separate
      // onHide callback that hid-only, which made the
      // column go empty after a hide. That
      // inconsistency between the eye button and
      // the context menu caused "Nothing showing in
      // feeds" after a few hides. Now both paths
      // produce the same outcome.)
      actions.push({
        label: hidden ? 'Unhide this entry' : 'Hide this entry',
        onClick: () => {
          if (!hidden) {
            recordImmediate({ entry_id: entry.id, type: 'never' })
          }
          onHideToggle()
        },
      })
    } else if (onHide) {
      // Fallback: if onHideToggle is not wired (e.g.
      // a custom Card instance that has not been
      // migrated yet) fall back to the legacy onHide
      // path. This preserves backward compatibility
      // for any external consumers.
      actions.push({
        label: 'Hide this entry',
        onClick: () => onHide(),
      })
    }
    if (onStar) {
      // Toggle label based on current starred state. The verb
      // flips so the menu tells the user what the action will
      // DO (not just the current state) — "Save for later" /
      // "Unsave" reads more clearly than "Starred" / "Not
      // starred" as a button label.
      actions.push({
        label: starred ? 'Unsave' : 'Save for later',
        onClick: () => {
          // Same engagement type as the star button — keeps
          // the ranker signal consistent regardless of input
          // path (button vs. context menu vs. keyboard ``s``).
          recordImmediate({ entry_id: entry.id, type: 'bookmark' })
          onStar()
        },
      })
    }
    if (onVote) {
      actions.push({
        label: vote === 'up' ? '▲ Remove upvote' : '▲ Upvote',
        onClick: () => {
          const next = vote === 'up' ? null : 'up'
          for (const type of voteInteractionEvents(vote ?? null, next)) {
            recordImmediate({ entry_id: entry.id, type })
          }
          if (next === 'up') toast('▲ Thanks — tuning toward more like this.', 'info')
          onVote(next)
        },
      })
      actions.push({
        label: vote === 'down' ? '▼ Remove downvote' : '▼ Downvote',
        onClick: () => {
          const next = vote === 'down' ? null : 'down'
          for (const type of voteInteractionEvents(vote ?? null, next)) {
            recordImmediate({ entry_id: entry.id, type })
          }
          if (next === 'down') toast('▼ Got it — tuning down similar stories.', 'info')
          onVote(next)
        },
      })
    }
    actions.push({ label: 'Copy link', onClick: () => copyUrl(entry.url) })
    actions.push({
      label: 'Open in new tab',
      onClick: () => {
        window.open(entry.url, '_blank', 'noopener,noreferrer')
      },
    })
    showContextMenu(e.clientX, e.clientY, actions)
  }

  // Visual state for unread vs selected vs read.
  //
  // The previous design (Claude code's refactor) used the ring
  // as the only signal for read state. The user reported that
  // the mark-read click had no visual effect — the ring
  // absence is too subtle on its own. The original design
  // had an opacity dim (opacity-60) for read cards. The
  // dim is back: read cards are 60% opaque, so they're
  // visibly "seen" without being "hidden".
  //
  // Ring is still used for selected (strong) and unread
  // (thin). Read cards have no ring.
  const ringClass = selected
    ? 'ring-2 ring-accent/70'
    : unread
      ? 'ring-1 ring-accent/40'
      : ''
  // The opacity dim is applied to the article container
  // so the entire card (title, body, meta row, sources,
  // summary) fades together. ``opacity-60`` is heavy
  // enough to read as "seen at a glance" but light
  // enough that the user can still read the card body
  // if they expand it.
  const dimClass = unread || selected ? '' : 'opacity-60'

  return (
    <div className="relative">
      {/* Swipe-reveal backgrounds. Sit behind the card (z-order is
          implicit — the article below has its own opaque
          background), full-bleed so the color reads as "the card is
          sliding off this surface" rather than a separate strip.
          Opacity is driven imperatively by applySwipeVisual as the
          user drags; at rest both sit fully transparent and
          pointer-events-none so they never intercept a tap. */}
      {onMarkRead && (
        <div
          ref={readRevealRef}
          aria-hidden="true"
          className="absolute inset-0 rounded-ios-lg bg-emerald-600 flex items-center pl-5 pointer-events-none opacity-0"
        >
          <CheckIcon className="w-5 h-5 text-white" filled />
          <span className="ml-2 text-ios-body font-medium text-white">Read</span>
        </div>
      )}
      {onHideToggle && (
        <div
          ref={hideRevealRef}
          aria-hidden="true"
          className="absolute inset-0 rounded-ios-lg bg-red-600 flex items-center justify-end pr-5 pointer-events-none opacity-0"
        >
          <span className="mr-2 text-ios-body font-medium text-white">Hide</span>
          <EyeIcon className="w-5 h-5 text-white" closed />
        </div>
      )}
      <article
        ref={stableCardRef}
        data-card-id={entry.id}
        // ``tabIndex`` only on the selected card so the rest of the
        // grid isn't a giant tab-stop forest. Arrow keys set
        // tabIndex={0} and call focus() when the user moves with the
        // keyboard.
        tabIndex={selected ? 0 : -1}
        onTouchStart={onTouchStart}
        onTouchEnd={onTouchEnd}
        onTouchCancel={onTouchCancel}
        onContextMenu={onContextMenu}
        className={`group relative rounded-ios-lg bg-bg-surface border border-hairline p-4 pl-5
                    hover:-translate-y-px hover:shadow-glow-md
                    transition-[box-shadow,border-color,opacity] duration-200
                    ${ringClass} ${dimClass}`}
      >
      {/* Category stripe. 3px wide, full height of the card. Lives
          outside the padding flow so it doesn't shift content when
          a category is/isn't known. ``aria-hidden`` because the
          color is decorative — the category name (if shown) carries
          the semantic. */}
      <div
        aria-hidden="true"
        className={`absolute left-0 top-0 bottom-0 w-[3px] rounded-l-ios-lg ${stripeClass}`}
      />
      <div className="flex items-start justify-between gap-3 mb-2">
        <a
          href={entry.url}
          target="_blank"
          rel="noopener noreferrer"
          aria-label="open in new tab"
          title="opens in a new tab"
          onClick={() => {
            // Fire-and-forget: the click POST doesn't gate the
            // navigation. We don't await because the browser opens
            // the new tab synchronously and we don't want a slow
            // network to delay it.
            recordImmediate({ entry_id: entry.id, type: 'click' })
            onActivate?.()
          }}
          className="flex-1 min-w-0 flex items-start gap-1.5 text-ios-body font-medium text-label-primary hover:text-white line-clamp-2"
        >
          <span className="min-w-0">{entry.title}</span>
          {/* "↗" affordance. Sits inline at the end of the title so it
              reads as part of the link, not a separate control. Group-
              hover brightens it on devices that have a hover state;
              touch devices get the static tertiary baseline. */}
          <span
            aria-hidden="true"
            className="shrink-0 text-label-tertiary group-hover:text-label-secondary transition text-ios-body leading-tight"
          >
            ↗
          </span>
        </a>
        {/* Score badge + thumbnail, stacked in one column rather than
            sitting side by side. Side by side wasted space two ways
            at once: the badge ate horizontal width from the title
            (more wrapped lines than necessary), and the combined
            badge+thumbnail block was shorter than the wrapped title,
            leaving a dead gap under the thumbnail where nothing
            rendered. Stacking narrows the right-hand column to just
            the thumbnail's width and lets it fill closer to the
            title's actual wrapped height. */}
        <div className="shrink-0 flex flex-col items-end gap-1.5">
          {/* The gradient gives the badge a tiny bit of depth;
              ``ring-1 ring-white/10`` is a faint inner highlight that
              reads as "this is a label" rather than "this is a
              button". Title shows the raw number for power users. */}
          <span
            className={`inline-flex items-center rounded-ios px-2 py-0.5 text-xs font-semibold text-white ring-1 ring-white/10 ${band.color}`}
            title={`composite score ${entry.composite_score.toFixed(0)}`}
          >
            {entry.composite_score.toFixed(0)}
          </span>
          {entry.image_path ? (
            <Thumbnail
              path={entry.image_path}
              title={entry.title}
              url={entry.url}
              entryId={entry.id}
            />
          ) : (
            // No feed-supplied or scraped photo (backend already
            // tries media:thumbnail / og:image / etc. before giving
            // up) — fall back to the source's favicon on a colored
            // tile rather than leaving a blank gap. Only needs
            // sourceName; ThumbnailFallback degrades to a plain
            // colored letter if there's no favicon either, so every
            // card with a known source gets SOME visual.
            sourceName && (
              <ThumbnailFallback sourceName={sourceName} faviconPath={sourceFaviconPath} />
            )
          )}
        </div>
      </div>
      <div className="flex items-center gap-2 text-ios-caption text-label-secondary">
        {sourceName && <span className={`font-medium ${sourceTextClass}`}>{sourceName}</span>}
        {sourceName && <span>·</span>}
        <time dateTime={entry.published_at ?? ''}>{timeAgo(entry.published_at)}</time>
      </div>
      {/* Reddit cross-reference footer. Rendered between the meta row
          and the summary block so it reads as "extra metadata about
          the article", not "extra metadata about the source". Only
          appears when the background cross-ref sweep stamped the
          entry (``reddit_thread_url`` non-null). The comment count
          suffix is omitted when the sweep hasn't recorded a count yet
          — same data path, just a defensive check for rows the sweep
          is mid-update on. ``stopPropagation`` keeps the click from
          opening the article (the link is to the Reddit thread, not
          the article URL); ``data-card-interactive`` makes the long-
          press / context-menu paths ignore the footer so a tap-and-
          hold for "copy Reddit link" still works as expected. */}
      {entry.reddit_thread_url && (
        <a
          href={entry.reddit_thread_url}
          target="_blank"
          rel="noopener noreferrer"
          data-card-interactive
          onClick={(e) => {
            e.preventDefault()
            e.stopPropagation()
            recordImmediate({ entry_id: entry.id, type: 'click' })
            window.open(entry.reddit_thread_url!, '_blank', 'noopener,noreferrer')
          }}
          className="mt-1.5 inline-flex items-center gap-1 text-ios-caption text-accent active:opacity-60"
        >
          <span aria-hidden="true">💬</span>
          <span>Discussed on Reddit</span>
          {typeof entry.reddit_comment_count === 'number' && entry.reddit_comment_count > 0 && (
            <span className="text-label-secondary">· {entry.reddit_comment_count} comments</span>
          )}
        </a>
      )}
      {/* Podcast episode audio. Same footer treatment as the Reddit
          cross-reference above — only appears when the entry came
          from a podcast feed (``audio_url`` non-null, populated by
          app.sources.rss's enclosure extraction). Opens the raw
          audio file directly rather than embedding a player: an
          inline <audio> element would need per-card playback state
          and competing-playback handling across a list of 20-50
          cards, which is a lot of surface area for what the browser
          already does for free on an audio-file link. */}
      {entry.audio_url && (
        <div className="mt-1.5 flex items-center gap-3">
          <a
            href={entry.audio_url}
            target="_blank"
            rel="noopener noreferrer"
            data-card-interactive
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              recordImmediate({ entry_id: entry.id, type: 'click' })
              window.open(entry.audio_url!, '_blank', 'noopener,noreferrer')
            }}
            className="inline-flex items-center gap-1 text-ios-caption text-accent active:opacity-60"
          >
            <span aria-hidden="true">🎧</span>
            <span>Listen</span>
            {typeof entry.duration_seconds === 'number' && entry.duration_seconds > 0 && (
              <span className="text-label-secondary">· {formatDuration(entry.duration_seconds)}</span>
            )}
          </a>
          {/* Only shown when the feed publishes a Podcasting 2.0
              transcript (entry.transcript_url) — reuses that
              transcript for an LLM summary rather than transcribing
              the audio ourselves, so the affordance only makes
              sense (and only appears) when one exists. */}
          {entry.transcript_url && (
            <button
              type="button"
              data-card-interactive
              onClick={(e) => {
                e.preventDefault()
                e.stopPropagation()
                setPodcastSummaryExpanded((v) => !v)
              }}
              className="inline-flex items-center gap-1 text-ios-caption text-accent active:opacity-60"
            >
              <span aria-hidden="true">📝</span>
              <span>{podcastSummaryExpanded ? 'Hide summary' : 'Summarize episode'}</span>
            </button>
          )}
        </div>
      )}
      {podcastSummaryExpanded && (
        <div
          onClick={(e) => e.stopPropagation()}
          className="mt-2 text-ios-caption text-label-secondary leading-relaxed whitespace-pre-wrap"
        >
          {podcastSummaryError ? (
            <span className="italic">couldn't generate a summary — try again later</span>
          ) : podcastSummaryUnavailable ? (
            <span className="italic text-label-tertiary">this episode has no transcript to summarize</span>
          ) : podcastSummary === null ? (
            <span className="italic text-label-tertiary">summarizing episode…</span>
          ) : podcastSummary === '' ? (
            <span className="italic text-label-tertiary">couldn't generate a summary (no transcript text or no LLM configured)</span>
          ) : (
            podcastSummary
          )}
        </div>
      )}
      {/* Inline summary. Sits between the meta row and the bottom
          edge of the card so it reads as "extra content below the
          metadata", not "extra metadata between two meta rows".
          ``onClick`` stops propagation because the card-level
          onContextMenu / long-press paths would otherwise fire when
          the user is just trying to read the summary text. The
          click target is the card itself; selecting text inside
          works because the inner ``e.stopPropagation`` only fires
          on a true click, not on text-selection dragstart. */}
      {expanded && (
        <div
          onClick={(e) => e.stopPropagation()}
          className="mt-2 text-ios-caption text-label-secondary leading-relaxed whitespace-pre-wrap line-clamp-3"
        >
          {summaryError
            ? <span className="italic">couldn't load summary</span>
            : summary === null
              ? <span className="italic text-label-tertiary">loading…</span>
              : summary === ''
                ? <span className="italic text-label-tertiary">no summary available</span>
                : summary}
        </div>
      )}
      {/* Vote footer. Deliberately its own full-width bar at the very
          bottom of the card, separated from the top meta row's check/
          star/eye cluster by real vertical distance and a hairline
          divider — the previous layout crammed the vote pair in next
          to those tightly-packed 28px icons, which read as one
          indistinct row and invited mis-taps on mobile. Bigger
          targets (h-9, generous horizontal padding) and its own
          breathing room fix that.
          ``vote`` is a persisted per-entry direction (App.tsx, synced
          via the same server-backed preferences store hidden/starred
          use) — clicking the currently-active direction again clears
          it (toggle), clicking the other direction switches. The
          active direction renders as a solid filled arrow on a
          tinted pill background so the user can tell at a glance
          which way (if any) they've already voted on this entry,
          matching the persisted-state treatment star/hidden already
          get. */}
      {(onVote || onToggleSummary || onMarkRead || onStar || onHideToggle) && (
        <div className="mt-3 pt-2.5 border-t border-hairline/70 flex items-center">
          {onVote && (
          <div className="flex items-center rounded-full bg-bg-elevated/70 ring-1 ring-white/5 overflow-hidden">
            <button
              type="button"
              data-card-interactive
              onMouseDown={(e) => e.stopPropagation()}
              onTouchStart={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.preventDefault()
                e.stopPropagation()
                const next = vote === 'up' ? null : 'up'
                for (const type of voteInteractionEvents(vote ?? null, next)) {
                  recordImmediate({ entry_id: entry.id, type })
                }
                if (next === 'up') {
                  toast('▲ Thanks — tuning toward more like this.', 'info')
                }
                onVote(next)
              }}
              aria-label={vote === 'up' ? 'remove upvote' : 'upvote — more like this'}
              aria-pressed={vote === 'up'}
              title="more like this"
              className={`h-9 px-3 flex items-center justify-center transition-colors
                          ${vote === 'up'
                            ? 'bg-amber-400/15 text-amber-400'
                            : 'text-label-secondary hover:text-amber-400 active:text-amber-400'}`}
            >
              <ArrowUpIcon className="w-4 h-4" filled={vote === 'up'} />
            </button>
            <div aria-hidden="true" className="w-px h-5 bg-white/10" />
            <button
              type="button"
              data-card-interactive
              onMouseDown={(e) => e.stopPropagation()}
              onTouchStart={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.preventDefault()
                e.stopPropagation()
                const next = vote === 'down' ? null : 'down'
                for (const type of voteInteractionEvents(vote ?? null, next)) {
                  recordImmediate({ entry_id: entry.id, type })
                }
                if (next === 'down') {
                  toast('▼ Got it — tuning down similar stories.', 'info')
                }
                onVote(next)
              }}
              aria-label={vote === 'down' ? 'remove downvote' : 'downvote — less like this'}
              aria-pressed={vote === 'down'}
              title="less like this"
              className={`h-9 px-3 flex items-center justify-center transition-colors
                          ${vote === 'down'
                            ? 'bg-sky-400/15 text-sky-400'
                            : 'text-label-secondary hover:text-sky-400 active:text-sky-400'}`}
            >
              <ArrowDownIcon className="w-4 h-4" filled={vote === 'down'} />
            </button>
          </div>
          )}
          {/* Utility icon cluster — summary chevron, mark-read, star,
              hide. Lives on the trailing edge of the SAME footer bar
              the vote pair is in, per the "top of card is info,
              bottom is interaction" split: the meta row above now
              carries only source + timestamp, nothing clickable.
              ``ml-auto`` pushes this cluster to the right regardless
              of whether the vote pill rendered (e.g. a caller that
              wires hide/star but not votes still gets the icons
              flush right, not stranded at the left edge). */}
          <div className="ml-auto flex items-center gap-1">
            {/* Per-card summary chevron. Same ``data-card-interactive``
                guard so a long-press / right-click on the card itself
                doesn't fire while the user is targeting the chevron.
                Title alternates by state so the hover hint matches the
                keyboard shortcut (``s``). Visually mirrors the ✓'s
                hover-reveal treatment — hidden until hover on desktop,
                always visible on touch. */}
            {onToggleSummary && (
              <button
                type="button"
                data-card-interactive
                onMouseDown={(e) => e.stopPropagation()}
                onTouchStart={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  onToggleSummary()
                }}
                aria-label={expanded ? 'hide summary' : 'show summary'}
                aria-expanded={!!expanded}
                title={expanded ? 'hide summary (s)' : 'show summary (s)'}
                className={`shrink-0 w-7 h-7 flex items-center justify-center rounded-full text-label-secondary active:bg-bg-elevated
                            ${expanded ? 'opacity-100 text-accent' : 'opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100'}`}
              >
                {/* Rotate 180° when expanded so the chevron flips up —
                    standard iOS disclosure-indicator idiom. */}
                <ChevronDownIcon className={`w-4 h-4 transition-transform ${expanded ? 'rotate-180' : ''}`} />
              </button>
            )}
            {/* Per-card mark-read ✓. ``onMouseDown`` swallows the press
                so it doesn't bubble into the article's long-press /
                context-menu paths. Fires ``view`` so the ranker sees
                the same signal it sees for headline and thumbnail
                clicks. */}
            {onMarkRead && (
              // Always visible (not hover-only) so the user can see at
              // a glance that the action is available. Muted weight
              // (text-label-secondary for unread, text-accent for
              // read) so it doesn't compete with the title. On read
              // cards the checkmark is filled so the user can tell at
              // a glance which cards are dimmed and which still need
              // attention.
              <button
                type="button"
                data-card-interactive
                data-mark-read
                onMouseDown={(e) => e.stopPropagation()}
                onTouchStart={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  // "view" + manual read flip + dwell. Three signals in
                  // one click: "view" (same signal headline/thumbnail
                  // clicks send), "dwell" (read-time, capped at 5 min so
                  // a backgrounded tab doesn't dominate the ranker's
                  // read-time average), and the manual readEntries flip
                  // that dims the card.
                  const dwellMs = Math.min(Date.now() - mountTime.current, 5 * 60 * 1000)
                  recordImmediate({ entry_id: entry.id, type: 'view' })
                  recordBatched({ entry_id: entry.id, type: 'dwell', value: dwellMs })
                  onMarkRead()
                }}
                aria-label="mark this card as read"
                aria-pressed={!unread}
                title="mark as read (m)"
                className={`shrink-0 w-7 h-7 flex items-center justify-center rounded-full active:bg-bg-elevated
                            ${unread ? 'text-label-secondary' : 'text-accent'}`}
              >
                <CheckIcon className="w-4 h-4" filled={!unread} />
              </button>
            )}
            {/* Per-card star. ``data-star`` so the keyboard ``s``
                shortcut in App can target the button via
                document.querySelector for the currently-selected card.
                Outline when unstarred, filled when starred. */}
            {onStar && (
              <button
                type="button"
                data-card-interactive
                data-star
                onMouseDown={(e) => e.stopPropagation()}
                onTouchStart={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  // ``bookmark`` is the engagement_events type that
                  // matches the ranker's existing "save" signal.
                  recordImmediate({ entry_id: entry.id, type: 'bookmark' })
                  onStar()
                }}
                aria-label={starred ? 'remove from saved' : 'save for later'}
                aria-pressed={!!starred}
                title="save for later (s)"
                className={`shrink-0 w-7 h-7 flex items-center justify-center rounded-full active:bg-bg-elevated
                            ${starred ? 'text-accent' : 'text-label-secondary'}`}
              >
                <StarIcon className="w-4 h-4" filled={!!starred} />
              </button>
            )}
            {/* Per-card hide (eye) button. State reflects the entry's
                current ``hidden`` prop: open eye = visible, closed eye
                = hidden. Click toggles via ``onHideToggle`` (App wires
                this to a callback that ALSO marks the entry read when
                hiding, so the entry moves to the column's History
                section instead of just disappearing). The keyboard
                ``h`` shortcut targets this button via
                document.querySelector + ``data-eye``. */}
            {onHideToggle && (
              <button
                type="button"
                data-card-interactive
                data-eye
                onMouseDown={(e) => e.stopPropagation()}
                onTouchStart={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  // ``never`` is a stronger signal than the ``view``
                  // mark-read fires — the ranker treats it as a
                  // category penalty.
                  if (!hidden) {
                    recordImmediate({ entry_id: entry.id, type: 'never' })
                  }
                  onHideToggle()
                }}
                aria-label={hidden ? 'unhide' : 'hide'}
                aria-pressed={!!hidden}
                title="hide (h)"
                className={`shrink-0 w-7 h-7 flex items-center justify-center rounded-full active:bg-bg-elevated
                            ${hidden ? 'text-accent' : 'text-label-secondary'}`}
              >
                <EyeIcon className="w-4 h-4" closed={!!hidden} />
              </button>
            )}
          </div>
        </div>
      )}
      </article>
    </div>
  )
}

// ``memo`` wrap. Default shallow-equal check would still let the
// inline ref / callback lambdas at the Column level defeat the
// memo, because they're allocated fresh on every Column render.
// So we use a custom areEqual that ignores the per-card callback
// props (``cardRef``, ``onActivate``, ``onMarkRead``,
// ``onToggleSummary``). They're inherently per-render at the
// Column layer; the parent passes them through to whatever the
// latest closure captures. The data-driven props — ``entry``,
// ``sourceName``, ``unread``, ``selected``, ``category``,
// ``expanded`` — are what we actually re-render against.
//
// Trade-off: if a callback's logic changes without the data
// changing, this Card won't re-render. That can't happen in
// practice because callbacks here only depend on data props
// (``onMarkEntryRead(e.id)`` reads from the same entry id) and
// App-level state. If a future caller starts passing callbacks
// that close over mutable refs, lift this to a stable ref and
// re-evaluate.
function _cardPropsEqual(prev: Props, next: Props): boolean {
  return (
    prev.entry === next.entry &&
    prev.sourceName === next.sourceName &&
    prev.sourceFaviconPath === next.sourceFaviconPath &&
    prev.unread === next.unread &&
    prev.selected === next.selected &&
    prev.category === next.category &&
    prev.expanded === next.expanded &&
    prev.starred === next.starred &&
    prev.hidden === next.hidden &&
    prev.vote === next.vote
  )
}

export const Card = memo(CardInner, _cardPropsEqual) as typeof CardInner

// iOS-style checkmark. Used for the per-card mark-read ✓. 2px stroke
// is heavier than the 1.75 the header icons use — the ✓ is the
// primary affordance on its button so it should be assertive.
// ``filled`` toggles a solid filled circle (like a completed
// checkbox) so the user can tell at a glance which cards are
// already dimmed and which still need attention. The fill
// colour is ``currentColor`` so the button's text-label-
// secondary (unread) vs text-accent (read) recolour logic
// drives both the icon and its background.
function CheckIcon({ className, filled = false }: { className?: string; filled?: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill={filled ? 'currentColor' : 'none'}
      stroke="currentColor"
      strokeWidth={filled ? 0 : 2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {filled ? (
        <path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1.2 14.4l-4.2-4.2 1.4-1.4 2.8 2.8 5.6-5.6 1.4 1.4-7 7z" />
      ) : (
        <polyline points="4 12 10 18 20 6" />
      )}
    </svg>
  )
}

// iOS-style eye. The "hide" / "unhide" affordance. Open
// eye (outline) = the entry is currently visible to the
// user; clicking the button will hide it. Closed eye
// (filled, with a stroke through it) = the entry is
// hidden; clicking will unhide it. The fill / stroke
// distinction matches the star button's pattern so the
// three meta-row buttons (check / star / eye) all read
// as the same scale.
//
// We use two separate SVG paths and switch between them
// rather than a single path with a stroke. Single-path
// + stroke-through-line works in some viewers but the
// closed-eye stroke needs to be visually heavier than
// the open-eye outline so the two states are
// unambiguous; the dedicated filled-with-line path
// achieves that without tuning the stroke width.
function EyeIcon({
  className,
  closed = false,
}: {
  className?: string
  closed?: boolean
}) {
  return closed ? (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {/* Almond outline. Same as the open-eye path so the
          silhouette stays recognizable. */}
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" />
      {/* Pupil. Filled so the closed-eye state reads as
          "sealed" rather than "empty". */}
      <circle cx="12" cy="12" r="3" fill="currentColor" />
      {/* Diagonal strike-through — the standard "hidden"
          affordance, mirrors the eye-off icon used in the
          Settings tab strip. */}
      <line x1="3" y1="3" x2="21" y2="21" />
    </svg>
  ) : (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  )
}

// iOS-style star. The same outline-vs-filled pattern as
// CheckIcon — outline when unstarred (the user hasn't saved
// this yet), filled when starred (the user has saved this
// and the visual should make that immediately obvious). The
// filled state uses ``currentColor`` so the button's
// ``text-label-secondary`` (unstarred) vs ``text-accent``
// (starred) recolour logic drives both the icon and any
// surrounding halo.
function StarIcon({ className, filled = false }: { className?: string; filled?: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill={filled ? 'currentColor' : 'none'}
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <polygon points="12 2 15 9 22 9.5 17 14.5 18.5 22 12 18 5.5 22 7 14.5 2 9.5 9 9 12 2" />
    </svg>
  )
}

// Down-chevron used for the per-card summary disclosure. 2px stroke
// matches CheckIcon so the two buttons read as visual siblings on the
// meta row. Rotated 180° via Tailwind's ``rotate-180`` when the
// panel is open — standard iOS disclosure-indicator idiom.
function ChevronDownIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <polyline points="6 9 12 15 18 9" />
    </svg>
  )
}

// Vote-pair arrows. A straight stem + chevron head (not a solid
// Reddit-style triangle) so they read as siblings of the other
// stroke-based icons here (Check/Eye/Star/Chevron) rather than a
// visually foreign import. Slightly heavier stroke (2.5 vs the 2 the
// other icons use) since these are the primary voting affordance and
// benefit from a touch more presence at 16px.
// ``filled`` swaps the open stem+chevron path (idle) for a solid
// closed triangle (voted) — mirrors the outline-vs-filled pattern
// StarIcon/CheckIcon already use, but needs a genuinely different
// path rather than just toggling ``fill`` because the idle path is
// an open stroke (a stem + a separate chevron), which has no
// enclosed area for ``fill`` to do anything visible to.
function ArrowUpIcon({ className, filled = false }: { className?: string; filled?: boolean }) {
  if (filled) {
    return (
      <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true">
        <path d="M12 4l8 14H4z" />
      </svg>
    )
  }
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M12 19V5M5 12l7-7 7 7" />
    </svg>
  )
}

function ArrowDownIcon({ className, filled = false }: { className?: string; filled?: boolean }) {
  if (filled) {
    return (
      <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true">
        <path d="M12 20l-8-14h16z" />
      </svg>
    )
  }
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M12 5v14M19 12l-7 7-7-7" />
    </svg>
  )
}

// Thumbnail placeholder for entries with no photo — same footprint
// as the real Thumbnail (so the title/badge row doesn't jump between
// cards that have a photo and cards that don't) but shows the
// source's favicon centered on a colored tile instead of stretching
// a tiny icon full-bleed (which would look pixelated at this size).
// The tile color is the SAME per-source hue SourceIcon's letter
// fallback uses (``stableHue``, imported not reimplemented — see its
// comment) so a source reads as "the same color" everywhere it
// appears, not just in FeedManager/Drawer.
//
// Degrades in two steps: favicon present -> icon on a colored tile;
// no favicon yet either -> SourceIcon's own letter-on-color fallback,
// just scaled up. Either way the card never has a blank gap where a
// thumbnail-having sibling has a photo — "media rich, even if it has
// to cheat" per the brief this was built for.
function ThumbnailFallback({ sourceName, faviconPath }: { sourceName: string; faviconPath?: string | null }) {
  const hue = stableHue(sourceName)
  return (
    <div
      aria-hidden="true"
      title={sourceName}
      className="shrink-0 w-28 sm:w-40 aspect-video rounded-ios overflow-hidden ring-1 ring-white/10 shadow-md flex items-center justify-center"
      style={{ background: `hsl(${hue} 45% 18%)` }}
    >
      <SourceIcon src={faviconPath ?? null} name={sourceName} size={32} />
    </div>
  )
}

// Aspect-video thumbnail (16:9). Wider than the old 96×96 square so
// the image fills more of the right side of the card — a real
// thumbnail instead of a postage-stamp. ``bg-bg-elevated`` reserves
// the box on first paint so the score badge doesn't shift when the
// image arrives (or fails). ``min-h-[72px]`` keeps a row from
// collapsing entirely if the image is a long 1×N transparent.
//
// Click handler opens the article in a new tab — gives the
// thumbnail a second obvious affordance in addition to the title
// link. ``e.preventDefault()`` + ``e.stopPropagation()`` keep the
// click from bubbling into the card's long-press / context-menu
// paths. The cursor-zoom-in style signals the clickability.
//
// Keyboard: ``role="button"`` + ``tabIndex={0}`` so the thumbnail
// is reachable via Tab and activatable with Enter/Space, matching
// the title ``<a>``. Without this, sighted keyboard users would
// have no way to act on a card without scrolling past the title.
//
// ``onError`` now ``console.warn``s in addition to hiding the wrapper
// so a developer chasing a 403 / 404 sees which thumbnail URL is
// the offender. Hidden state is the same as before (display:none on
// the wrapper) so the badge still lands flush right.
function Thumbnail({ path, title, url, entryId }: { path: string; title: string; url: string; entryId: number }) {
  const wrapperRef = useRef<HTMLDivElement | null>(null)
  const open = () => {
    // The thumbnail click is its own affordance and uses
    // ``e.stopPropagation`` to bypass the article-level handlers —
    // so the headline's onClick won't fire. Record the click event
    // here so the recommendation ranker sees both kinds of opens.
    recordImmediate({ entry_id: entryId, type: 'click' })
    window.open(url, '_blank', 'noopener,noreferrer')
  }
  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    // Don't bubble up — the article-level handlers (long-press,
    // context menu) shouldn't fire when the user just wants the
    // thumbnail to act as a link.
    e.preventDefault()
    e.stopPropagation()
    open()
  }
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      e.stopPropagation()
      open()
    }
  }
  return (
    <div
      ref={wrapperRef}
      role="button"
      tabIndex={0}
      data-card-interactive
      onClick={onClick}
      onKeyDown={onKey}
      aria-label={`open ${title} in new tab`}
      title={title}
      className="shrink-0 w-28 sm:w-40 aspect-video rounded-ios overflow-hidden bg-bg-elevated cursor-zoom-in
                 ring-1 ring-white/10 shadow-md
                 focus:outline-none focus:ring-2 focus:ring-accent/60"
    >
      <img
        src={`/assets/${path}`}
        alt=""
        loading="lazy"
        decoding="async"
        className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
        onError={() => {
          console.warn(`Card thumbnail failed: /assets/${path}`)
          if (wrapperRef.current) wrapperRef.current.style.display = 'none'
        }}
      />
    </div>
  )
}

// ---- Context menu (Card-level "copy link" / "open in new tab") ----
//
// Tiny custom menu — pulled out of Card.tsx body so the React tree
// stays clean. We don't use a library because (a) it's <60 lines,
// (b) a portal library adds a dependency we don't otherwise need,
// (c) the menu is only ever shown from a right-click on a card, so
// the call site is one place.
//
// The menu is a positioned div with a transparent backdrop. Click
// on the backdrop or press Esc → dismiss.

function copyUrl(url: string) {
  // navigator.clipboard requires a secure context (https or
  // localhost). When the dashboard is served from a bare IP over
  // http (e.g. on the LAN behind the TrueNAS host) the clipboard
  // API is unavailable. Fall back to a textarea + execCommand for
  // that case — works on http, ugly, but functional.
  const fallback = () => {
    try {
      const ta = document.createElement('textarea')
      ta.value = url
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      toast('link copied')
    } catch {
      toast('copy failed — long-press the title instead', 'error')
    }
  }
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(url).then(
      () => toast('link copied'),
      () => fallback(),
    )
  } else {
    fallback()
  }
}

function showContextMenu(
  x: number,
  y: number,
  actions: Array<{ label: string; onClick: () => void }>,
) {
  // Dismiss any existing menu. There's only ever one, but a fast
  // double right-click on the same card would otherwise stack them.
  document.getElementById('card-context-menu')?.remove()

  const menu = document.createElement('div')
  menu.id = 'card-context-menu'
  menu.className =
    'fixed z-50 min-w-[180px] rounded-ios bg-bg-elevated border border-hairline shadow-xl text-ios-body py-1'
  // Clamp to viewport so the menu never renders off-screen.
  const left = Math.min(x, window.innerWidth - 200)
  const top = Math.min(y, window.innerHeight - 100)
  menu.style.left = `${left}px`
  menu.style.top = `${top}px`

  // Single teardown. The original implementation leaked a keydown
  // listener on ``document`` whenever the user dismissed the menu via
  // the backdrop or by clicking a button — only the Escape branch
  // detached its handler, so each right-click → backdrop-click round
  // trip left another active listener closing over the orphaned
  // menu/backdrop DOM nodes. Use one close() callback that removes
  // every node and every listener, and call it from every exit path.
  // The backdrop element is created below, after the close()
  // closure is defined, because it needs to be in scope for the
  // close() body. We declare it via the hoisted var pattern so
  // the closure can see it before the const initializer runs.
  const close = () => {
    menu.remove()
    backdrop.remove()
    document.removeEventListener('keydown', onKey, true)
  }
  for (const item of actions) {
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className =
      'block w-full text-left px-3 py-1.5 text-label-primary active:bg-bg-surface'
    btn.textContent = item.label
    btn.onclick = () => {
      item.onClick()
      close()
    }
    menu.appendChild(btn)
  }

  const backdrop = document.createElement('div')
  backdrop.className = 'fixed inset-0 z-40'
  backdrop.onclick = () => close()
  document.body.appendChild(backdrop)
  document.body.appendChild(menu)

  // Esc dismisses too. Listeners are per-menu so multiple opens
  // don't fight over a single handler.
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape') close()
  }
  document.addEventListener('keydown', onKey, true)
}





