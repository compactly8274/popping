// One card, laid out as a Reddit post (PR5a, D1): the feed's icon,
// name and time on top, then the headline with its preview text (D34)
// and thumbnail, then one action bar: ▲ ▼, Save, Share, the Summary
// chevron and ⋯. When the entry has a thumbnail (parsed from the feed
// at ingest), it renders to the right of the title — feeds without
// thumbnails keep the compact layout.
//
// UX features layered on top of the basic card:
//   - `↗` glyph + tooltip signals "opens in new tab" so the user
//     isn't surprised when their back button doesn't go back.
//   - ⋯ opens the story's sheet (ActionSheet.tsx): Hide, Edit feed,
//     Copy link and Open in browser. A right-click on a desktop
//     shows the same rows in a small menu at the pointer.
//   - Long-press on touch laptops copies the URL to the clipboard via
//     the global Toast singleton.
//   - `dimmed` fades a card under a tab's "Earlier (N)": a story
//     already seen (PR3b). Opening a story is what uses it up
//     (``onActivate``); there is no mark-read button or gesture.
//   - Swipe left = hide (Apollo/iOS Mail style). See the SWIPE_*
//     constants below for the tuning — there's a dead zone + a
//     direction lock so an intentional vertical scroll never gets
//     mistaken for a swipe, and a firm commit threshold so a
//     short/accidental drag always snaps back with no action taken.
//     Swipe right does nothing.

import { memo, useEffect, useRef, useState, type MouseEvent, type TouchEvent } from 'react'
import { api, type Entry, type FramingCluster, safeExternalUrl } from '../api'
import { noteStoryOpened, recordImmediate } from '../lib/interactions'
import { isHistorical } from '../lib/feedRules'
import { sourceLabel } from '../lib/labels'
import { useSourceLabels } from '../lib/useSourceLabels'
import { openActionSheet, type SheetAction } from './ActionSheet'
import { SourceIcon } from './SourceIcon'
import { toast } from './Toast'

type Props = {
  entry: Entry
  sourceName?: string
  // Source favicon path (relative to /assets), or null if not
  // fetched yet: the 16px icon on the header line (a coloured letter
  // without one).
  sourceFaviconPath?: string | null
  // A story already seen, drawn under its tab's "Earlier (N)": faded and
  // desaturated. Stories above the marker never fade, even once seen,
  // so nothing changes under the reader (PR3b).
  dimmed?: boolean
  // True when this card is the keyboard-selected card. Drives the
  // focus ring. Set by App's keyboard handler.
  selected?: boolean
  // Ref-callback so App can focus/scroll into view on keyboard nav.
  // Optional — most cards never need it.
  cardRef?: (el: HTMLElement | null, prev?: HTMLElement | null) => void
  // The story was opened, from any of its ways out: the headline, the
  // thumbnail, the Reddit thread, Listen, the context menu's "Open in new
  // tab". App marks it seen (PR3b). Must be stable in effect: the memo
  // comparator ignores it.
  onActivate?: () => void
  // Source category — drives the colored left stripe. Optional; when
  // absent the card just renders without a stripe. The Column passes
  // it through from ``sourcesByMeta``.
  category?: string
  // Hide (App's ``hideStory``): the ⋯ sheet's and the right-click
  // menu's Hide row, and a swipe left. The story leaves every tab at
  // once; App shows Undo and holds the ranking's ``never`` until the
  // Undo window closes (D25). Without it there is no Hide row and no
  // swipe.
  onHide?: () => void
  // Save / unsave (the bookmark button). The card records ``bookmark``
  // on a save; App shows "Removed from Saved · Undo" on an unsave.
  onStar?: () => void
  // True if this entry is currently saved. Drives the bookmark's fill
  // and the button's label. Optional — when absent the bookmark
  // renders in its empty state.
  starred?: boolean
  // Per-card inline summary. When ``expanded`` is true the card
  // fetches the cached summary once and renders it under the
  // title. ``onToggleSummary`` flips the expanded bit. Expanding a
  // card doesn't mark it seen.
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
  // Tapping the source name opens that feed's editor in Settings.
  onSourceTap?: (sourceId: number) => void
}

// Same tone -> style map as FramingWatch.tsx's standalone section —
// duplicated rather than imported (small, UI-local, and importing a
// component file's internal const to reuse four lines isn't worth
// the coupling). Reuses the score-hot/warm vocabulary already
// established for the composite-score gradient — alarmist reads as
// "hot", urgent as "warm", neutral stays unstyled.
const RELATED_TONE_STYLES: Record<string, string> = {
  urgent: 'text-score-warm border-score-warm/40',
  alarmist: 'text-score-hot border-score-hot/40',
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
  switch (category?.toLowerCase().trim()) {
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
    case 'sports':       return 'bg-teal-500'
    case 'entertainment': return 'bg-fuchsia-500'
    case 'gaming':        return 'bg-indigo-500'
    case 'food':          return 'bg-yellow-500'
    case 'music':         return 'bg-sky-500'
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
// Same category -> hue mapping again, this time as a low-opacity
// border color so the card's edge itself carries a whisper of the
// category identity instead of relying solely on the 3px stripe —
// asked for alongside the stripe/source-text tinting so a card reads
// as "this category" even at a glance from its silhouette. Kept
// faint (/25) so it doesn't compete with the ring treatment used for
// the selected card, which still needs to read as the stronger
// signal.
function categoryBorderClass(category: string | undefined): string {
  switch (category?.toLowerCase().trim()) {
    case 'news':     return 'border-blue-500/25'
    case 'tech':     return 'border-violet-500/25'
    case 'vulns':    return 'border-red-500/25'
    case 'science':  return 'border-emerald-500/25'
    case 'finance':  return 'border-amber-500/25'
    case 'policy':   return 'border-cyan-500/25'
    case 'longform': return 'border-rose-500/25'
    case 'deals':    return 'border-lime-500/25'
    case 'podcast':  return 'border-orange-500/25'
    case 'video':    return 'border-pink-500/25'
    case 'sports':       return 'border-teal-500/25'
    case 'entertainment': return 'border-fuchsia-500/25'
    case 'gaming':        return 'border-indigo-500/25'
    case 'food':          return 'border-yellow-500/25'
    case 'music':         return 'border-sky-500/25'
    default:         return 'border-hairline'
  }
}

function categorySourceTextClass(category: string | undefined): string {
  switch (category?.toLowerCase().trim()) {
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
    case 'sports':       return 'text-teal-400'
    case 'entertainment': return 'text-fuchsia-400'
    case 'gaming':        return 'text-indigo-400'
    case 'food':          return 'text-yellow-400'
    case 'music':         return 'text-sky-400'
    default:         return 'text-label-primary'
  }
}

// "5h", as Reddit writes it (PR5a): the header line has no room for
// "ago". '' without a time, and the header hides its "·" with it.
function timeAgo(iso: string | null): string {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  if (ms < 0) return 'now'
  const mins = Math.floor(ms / 60000)
  if (mins < 60) return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  return `${days}d`
}

// Share and Copy link hand the address to the system or the
// clipboard: only an http(s) one. (``safeExternalUrl`` is not used for
// them: it would hand out old.reddit.com.)
function isHttpUrl(url: string): boolean {
  return /^https?:\/\//i.test(url)
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
// Exported for App's ``a`` / ``z`` keys, which vote the same way.
export function voteInteractionEvents(
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

export function CardInner({ entry, sourceName, sourceFaviconPath, dimmed, selected, cardRef, onActivate, category, expanded, onToggleSummary, onHide, onStar, starred, vote, onVote, onSourceTap }: Props) {
  // A renamed feed shows its new name here even though memo skips
  // re-renders when only the feed list changed.
  useSourceLabels()
  const stripeClass = categoryStripeClass(category)
  const sourceTextClass = categorySourceTextClass(category)
  const borderClass = categoryBorderClass(category)
  // Touch tracking for long-press → copy URL.
  // Kept in refs so the values don't trigger re-renders mid-press.
  const touchStart = useRef<{ x: number; y: number; t: number; id: number; onInteractiveChild: boolean } | null>(null)
  const longPressTimer = useRef<number | null>(null)

  // Swipe-to-act state. Refs, not useState — a touchmove-driven
  // re-render for every pixel of drag would be needlessly expensive
  // across a list of 20-50 cards. Visuals are applied imperatively
  // (see applySwipeVisual) and only the final commit/cancel touches
  // React state, via the same ``onHide`` the ⋯ sheet's Hide row calls.
  const articleRef = useRef<HTMLElement | null>(null)
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
  // On detach it also passes the element going away, so the registry
  // only drops the entry if it still points at this card (the same
  // story can be on screen twice, in the phone and desktop layouts).
  const stableCardRef = useRef((el: HTMLElement | null) => {
    const prev = articleRef.current
    articleRef.current = el
    cardRefLatest.current?.(el, prev)
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

  // Reddit comment-discussion summary. Same shape as the podcast
  // summary state above (independent toggle + cache, since an entry
  // could have both a feed summary and a comment-discussion summary
  // as distinct content). ``redditSummaryRateLimited`` is its own
  // flag rather than folded into "unavailable" — Reddit's direct-
  // mode fetch allows only ~1 request/75s (app.reddit_client), so a
  // tap can lose that race against the scheduled ingest jobs; the
  // backend deliberately does NOT cache that case, so re-expanding
  // (collapse then re-expand) retries rather than being stuck.
  const [redditSummaryExpanded, setRedditSummaryExpanded] = useState(false)
  const [redditSummary, setRedditSummary] = useState<string | null>(null)
  const [redditSummaryError, setRedditSummaryError] = useState(false)
  const [redditSummaryUnavailable, setRedditSummaryUnavailable] = useState(false)
  const [redditSummaryRateLimited, setRedditSummaryRateLimited] = useState(false)

  // Related coverage (Framing Watch, re-surfaced per-card). ``related``
  // is ``undefined`` before the first fetch, ``null`` once fetched and
  // confirmed empty (shouldn't normally happen — the button is only
  // shown when ``entry.story_cluster_id`` is already known non-null —
  // but the cluster could have lost its other members between the
  // list poll and this tap), or the cluster payload once loaded.
  const [relatedExpanded, setRelatedExpanded] = useState(false)
  const [related, setRelated] = useState<FramingCluster | null | undefined>(undefined)
  const [relatedError, setRelatedError] = useState(false)

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

  useEffect(() => {
    if (!redditSummaryExpanded) return
    if (redditSummary !== null) return
    if (redditSummaryError) return
    if (redditSummaryUnavailable) return
    // Rate-limited is deliberately NOT a terminal state — the
    // backend doesn't cache it, so re-collapsing and re-expanding
    // (the button's onClick clears the flag) retries. Clear it here
    // too, right as a fresh attempt starts, so a stale "rate
    // limited" message doesn't linger under the new "summarizing…"
    // state while this attempt is in flight.
    setRedditSummaryRateLimited(false)
    let cancelled = false
    api
      .redditCommentSummary(entry.id)
      .then((r) => {
        if (cancelled) return
        if (r.rate_limited) {
          setRedditSummaryRateLimited(true)
          return
        }
        if (!r.available) {
          setRedditSummaryUnavailable(true)
          return
        }
        setRedditSummary(r.summary ?? '')
      })
      .catch(() => {
        if (cancelled) return
        setRedditSummaryError(true)
      })
    return () => {
      cancelled = true
    }
  }, [redditSummaryExpanded, entry.id, redditSummary, redditSummaryError, redditSummaryUnavailable])

  useEffect(() => {
    if (!relatedExpanded) return
    if (related !== undefined) return
    if (relatedError) return
    let cancelled = false
    api
      .entryRelated(entry.id)
      .then((r) => {
        if (cancelled) return
        setRelated(r)
      })
      .catch(() => {
        if (cancelled) return
        setRelatedError(true)
      })
    return () => {
      cancelled = true
    }
  }, [relatedExpanded, entry.id, related, relatedError])

  const clearLongPress = () => {
    if (longPressTimer.current != null) {
      window.clearTimeout(longPressTimer.current)
      longPressTimer.current = null
    }
  }

  // Push the current drag offset onto the DOM directly (no React
  // state) so a fast finger doesn't queue a render per pixel.
  // ``animate`` adds a short transition for the release snap;
  // during an active drag we want 1:1 finger tracking, so every
  // touchmove sets ``none``. An empty string would let the
  // article's own 200ms transition class apply, and the card would
  // trail the finger.
  const applySwipeVisual = (dx: number, animate: boolean) => {
    const el = articleRef.current
    if (el) {
      el.style.transition = animate ? 'transform 180ms ease-out' : 'none'
      el.style.transform = dx === 0 ? '' : `translateX(${dx}px)`
    }
    const progress = Math.min(1, Math.abs(dx) / SWIPE_MAX_REVEAL_PX)
    if (hideRevealRef.current) {
      hideRevealRef.current.style.opacity = dx < 0 ? String(progress) : '0'
    }
  }

  const resetSwipe = (animate: boolean) => {
    swipeDx.current = 0
    swipeLock.current = 'none'
    applySwipeVisual(0, animate)
  }

  // A swipe past the commit point: the same Hide as the sheet's row.
  // App records the ranking's ``never`` (held for Undo, D25).
  const commitHide = () => {
    onHide?.()
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
    // other interactive child like the action bar's buttons). Both the
    // long-press-copy path and the swipe path skip when this is set,
    // so dragging a button doesn't also drag the card underneath it.
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
    // Long-press to copy is for touchscreens on hover-capable devices
    // (touch laptops). On a phone the press belongs to the system's
    // own link menu (open, share, copy), which the copy would fight.
    if (!window.matchMedia('(hover: hover)').matches) return
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

      // Only a left swipe does something (hide), and only with
      // ``onHide`` wired: anything else is clamped to 0 so the card
      // doesn't visually drag in a direction that does nothing on
      // release. (A right swipe used to mark a story read; PR3b took
      // it out, so opening a story is the one way to use it up.)
      let clamped = dx
      if (dx > 0 || !onHide) clamped = 0
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
    // onHide is read fresh from the closure each effect run (the deps
    // array below re-attaches the listener whenever its identity
    // changes, which — per Card.memo's custom comparator — is only
    // when the Column re-derives its per-card callbacks, not on every
    // poll).
  }, [onHide])

  const onTouchEnd = (e: TouchEvent<HTMLElement>) => {
    const start = touchStart.current
    clearLongPress()
    const wasSwiping = swipeLock.current === 'horizontal'
    const finalDx = swipeDx.current
    if (wasSwiping) {
      resetSwipe(true)
      if (finalDx <= -SWIPE_COMMIT_PX) {
        e.preventDefault()
        e.stopPropagation()
        commitHide()
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

  // Visual state. The ring marks the keyboard-selected card only.
  // A card under "Earlier (N)" is dimmed: the opacity dim applies to
  // the article container so the entire card (title, body, meta row,
  // sources, summary) fades together. ``opacity-60`` is heavy enough
  // to read as "seen at a glance" but light enough that the user can
  // still read the card body if they expand it. ``saturate-50`` rides
  // along so a seen card loses color too, not just brightness.
  const ringClass = selected ? 'ring-2 ring-accent/70' : ''
  const dimClass = dimmed && !selected ? 'opacity-60 saturate-50' : ''

  // The story was opened from one of the card's links: record the
  // ``click`` and tell the card's owner (App marks it seen). The links'
  // own ``onClick`` and their middle-click both run it.
  const noteOpen = () => {
    // Fire-and-forget: the click POST doesn't gate the navigation.
    recordImmediate({ entry_id: entry.id, type: 'click' })
    onActivate?.()
  }
  // A middle-click opens the link in a background tab by itself (an
  // ``auxclick``, not a click), so it has to note the open too. The
  // browser's own open goes ahead (the hrefs are already
  // ``safeExternalUrl``'s).
  const onMiddleClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.button === 1) noteOpen()
  }

  // Share and Copy link need an address a browser can open; a story
  // whose address isn't http(s) gets neither.
  const canShare = isHttpUrl(entry.url)

  // The ⋯ sheet's rows, and the right-click menu's (one list for both).
  // PR5b adds Hide stories from <feed>. Every open records a ``click``
  // and uses the story up (``noteOpen``), as the headline does.
  const buildActions = (): SheetAction[] => {
    const actions: SheetAction[] = []
    if (onHide) actions.push({ label: 'Hide', destructive: true, onClick: () => onHide() })
    if (onSourceTap && sourceName) actions.push({ label: 'Edit feed', onClick: () => onSourceTap(entry.source_id) })
    actions.push({ label: 'Copy link', disabled: !canShare, onClick: () => copyUrl(entry.url) })
    actions.push({
      label: 'Open in browser',
      disabled: !canShare,
      onClick: () => {
        // ``safeExternalUrl`` rewrites reddit.com -> old.reddit.com
        // so the popped-out tab doesn't show the "Get the App"
        // overlay (see api.ts for the full rationale).
        noteOpen()
        window.open(safeExternalUrl(entry.url), '_blank', 'noopener,noreferrer')
      },
    })
    return actions
  }

  // Right-click → the same rows as ⋯, in a small menu at the pointer.
  // Native context menu is suppressed so we can offer them without a
  // third-party library.
  const onContextMenu = (e: MouseEvent<HTMLElement>) => {
    e.preventDefault()
    showContextMenu(e.clientX, e.clientY, buildActions())
  }

  // Share: the system sheet where there is one, with the story's own
  // address (not ``safeExternalUrl``'s, which would hand out
  // old.reddit.com). Called in the tap handler itself: iOS needs the
  // user gesture. ``share`` is recorded once the sheet reports the
  // share done; a sheet dismissed (AbortError) or refused records
  // nothing. Without ``navigator.share`` the address is copied instead.
  const share = () => {
    if (!canShare) return
    if (typeof navigator.share === 'function') {
      navigator.share({ title: entry.title, url: entry.url }).then(
        () => recordImmediate({ entry_id: entry.id, type: 'share' }),
        () => {},
      )
      return
    }
    copyUrl(entry.url)
  }

  // A feed of history (On this day) has no "5h": its stories are about
  // today's date, not from today (PR4d).
  const historical = isHistorical(sourceName)
  const time = historical ? '' : timeAgo(entry.published_at)
  const preview = entry.preview && !expanded ? entry.preview : null

  return (
    <div className="relative">
      {/* Swipe-reveal background. Sits behind the card (z-order is
          implicit — the article below has its own opaque
          background), full-bleed so the color reads as "the card is
          sliding off this surface" rather than a separate strip.
          Opacity is driven imperatively by applySwipeVisual as the
          user drags; at rest it sits fully transparent and
          pointer-events-none so it never intercepts a tap. */}
      {onHide && (
        <div
          ref={hideRevealRef}
          aria-hidden="true"
          className="absolute inset-0 rounded-ios-lg bg-red-600 flex items-center justify-end pr-5 pointer-events-none opacity-0"
        >
          <span className="mr-2 text-ios-body font-medium text-white">Hide</span>
          <EyeOffIcon className="w-5 h-5 text-white" />
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
        // ``touch-pan-y`` tells the browser this element only ever
        // scrolls vertically under native control — horizontal pans
        // belong to the JS swipe lock below. Without it, mobile
        // browsers can claim a predominantly-horizontal touch for
        // native scrolling of an ancestor (the column list, or the
        // page itself if anything overflows) BEFORE the touchmove
        // lock engages, so the swipe pans the content instead of
        // dragging the card. Vertical scrolling starting on the card
        // stays fully native; horizontal gestures are delivered to
        // JS untouched.
        className={`group relative touch-pan-y-zoom rounded-ios-lg bg-bg-surface border p-4 pl-5 max-md:[container-type:inline-size]
                    hover:-translate-y-px hover:shadow-glow-md
                    active:scale-[0.985] active:shadow-glow-sm
                    transition-[transform,box-shadow,border-color,opacity,filter] duration-200
                    ${borderClass} ${ringClass} ${dimClass}`}
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
      {/* The header line (D1): the feed's icon, its name and the time,
          above the title as on a Reddit post. 44px tall so the feed
          name is a full-size tap target that never reaches the
          headline. Tapping it opens the feed's editor (until PR8a's
          one-feed view; Edit feed stays in ⋯, D26). */}
      <div className="flex min-h-[44px] items-center gap-2 text-ios-caption text-label-secondary">
        {sourceName && (
          <span className="shrink-0 flex" aria-hidden="true">
            <SourceIcon src={sourceFaviconPath ?? null} name={sourceLabel(sourceName)} size={16} />
          </span>
        )}
        {sourceName &&
          (onSourceTap ? (
            <button
              type="button"
              data-card-interactive
              onTouchStart={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.stopPropagation()
                onSourceTap(entry.source_id)
              }}
              aria-label={`edit feed ${sourceLabel(sourceName)}`}
              className={`min-w-0 min-h-[44px] truncate text-left font-semibold ${sourceTextClass}`}
            >
              {sourceLabel(sourceName)}
            </button>
          ) : (
            <span className={`min-w-0 truncate font-semibold ${sourceTextClass}`}>{sourceLabel(sourceName)}</span>
          ))}
        {sourceName && (historical || time) && <span className="shrink-0 whitespace-nowrap">·</span>}
        {historical ? (
          <span className="shrink-0 whitespace-nowrap">On this day</span>
        ) : (
          time && (
            <time className="shrink-0 whitespace-nowrap" dateTime={entry.published_at ?? ''}>
              {time}
            </time>
          )
        )}
      </div>
      {/* The title, the preview under it (D34) and the thumbnail are one
          way into the story: the headline link holds the first two, and
          the thumbnail opens the same address through ``onActivate``.
          On narrow phone cards the thumbnail shrinks to a square, then
          moves under the headline (container queries on the article),
          so the headline keeps a readable width. */}
      <div className="flex items-start justify-between gap-3 mb-2 [@container(max-width:299px)]:flex-col">
        <a
          href={safeExternalUrl(entry.url)}
          target="_blank"
          rel="noopener noreferrer"
          aria-label="open in new tab"
          title="opens in a new tab"
          // The browser opens the new tab itself, at once; noteOpen
          // doesn't hold it up.
          onClick={noteOpen}
          onAuxClick={onMiddleClick}
          // On a phone the headline is how a story opens, so the link
          // fills its row and is at least 44px tall even for a
          // one-line title.
          className="flex-1 min-w-0 block max-md:self-stretch max-md:min-h-[44px] text-ios-body font-medium text-label-primary hover:text-white"
        >
          <span className="flex items-start gap-1.5">
            {/* Full title, no line-clamp — a long headline just wraps
                and grows the card rather than getting cut off with an
                ellipsis. The row's height follows the title; the
                thumbnail column (a separate flex item) simply renders
                at its own natural height alongside it. */}
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
          </span>
          {/* The preview (D34): the cached AI summary, or the feed's own
              blurb, clamped to 2 lines on a phone and 3 from md up. Gone
              while the Summary panel is open, so the text is never
              shown twice. */}
          {preview && (
            <span
              data-preview
              className="mt-1 text-ios-body font-normal text-label-secondary line-clamp-2 md:line-clamp-3 break-words"
            >
              {preview}
            </span>
          )}
        </a>
        {/* Real thumbnails only. Cards without a photo give the
            headline the full width rather than showing a placeholder
            tile, and the ranking score stays out of the reader's way
            (it still orders the list). */}
        {entry.image_path && (
          <div className="shrink-0 [@container(max-width:299px)]:w-full [@container(max-width:299px)]:self-stretch">
            <Thumbnail
              path={entry.image_path}
              title={entry.title}
              url={entry.url}
              entryId={entry.id}
              onOpen={onActivate}
            />
          </div>
        )}
      </div>
      {/* Reddit cross-reference footer. Rendered between the title row
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
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2">
          <a
            href={safeExternalUrl(entry.reddit_thread_url)}
            target="_blank"
            rel="noopener noreferrer"
            data-card-interactive
            data-one-line
            onAuxClick={onMiddleClick}
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              noteOpen()
              // ``safeExternalUrl`` rewrites reddit.com -> old.reddit.com
              // so the popped-out tab doesn't show the "Get the App"
              // overlay (see api.ts). The href above is also rewritten
              // for middle-click / cmd-click / context-menu "Open in
              // new tab" so every path out of this card uses the
              // app-prompt-free variant.
              window.open(safeExternalUrl(entry.reddit_thread_url!), '_blank', 'noopener,noreferrer')
            }}
            className="inline-flex items-center gap-1 whitespace-nowrap min-h-[44px] -my-2 text-ios-caption text-accent active:opacity-60"
          >
            <span aria-hidden="true">💬</span>
            <span>Discussed on Reddit</span>
            {typeof entry.reddit_comment_count === 'number' && entry.reddit_comment_count > 0 && (
              <span className="text-label-secondary">· {entry.reddit_comment_count} comments</span>
            )}
          </a>
          {/* On-demand LLM summary of the comment discussion — same
              "computed on first tap, not preloaded" contract as the
              podcast episode summary below. */}
          <button
            type="button"
            data-card-interactive
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              setRedditSummaryExpanded((v) => !v)
            }}
            className="inline-flex items-center gap-1 whitespace-nowrap min-h-[44px] -my-2 text-ios-caption text-accent active:opacity-60"
          >
            <span aria-hidden="true">📝</span>
            <span>{redditSummaryExpanded ? 'Hide summary' : 'Summarize comments'}</span>
          </button>
        </div>
      )}
      {redditSummaryExpanded && (
        <div
          onClick={(e) => e.stopPropagation()}
          className="mt-2 text-ios-caption text-label-secondary leading-relaxed whitespace-pre-wrap break-words"
        >
          {redditSummaryError ? (
            <span className="italic">couldn't generate a summary — try again later</span>
          ) : redditSummaryRateLimited ? (
            <span className="italic text-label-tertiary">rate limited by Reddit — try again in a moment</span>
          ) : redditSummaryUnavailable ? (
            <span className="italic text-label-tertiary">no Reddit discussion found for this entry</span>
          ) : redditSummary === null ? (
            <span className="italic text-label-tertiary">summarizing comments…</span>
          ) : redditSummary === '' ? (
            <span className="italic text-label-tertiary">couldn't generate a summary (no comments found or no LLM configured)</span>
          ) : (
            redditSummary
          )}
        </div>
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
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2">
          <a
            href={safeExternalUrl(entry.audio_url)}
            target="_blank"
            rel="noopener noreferrer"
            data-card-interactive
            data-one-line
            onAuxClick={onMiddleClick}
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              noteOpen()
              window.open(safeExternalUrl(entry.audio_url!), '_blank', 'noopener,noreferrer')
            }}
            className="inline-flex items-center gap-1 whitespace-nowrap min-h-[44px] -my-2 text-ios-caption text-accent active:opacity-60"
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
              className="inline-flex items-center gap-1 whitespace-nowrap min-h-[44px] -my-2 text-ios-caption text-accent active:opacity-60"
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
          className="mt-2 text-ios-caption text-label-secondary leading-relaxed whitespace-pre-wrap break-words"
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
      {/* Related coverage — re-surfaces the same Framing Watch
          clustering the standalone section (App.tsx, desktop-only)
          already shows, scoped to just this entry's siblings so the
          comparison is available without scrolling up and cross-
          referencing manually. Only rendered when the list payload
          already told us this entry is clustered
          (``story_cluster_id`` non-null) — same on/off gating as the
          Reddit/podcast affordances above, so there's no button that
          predictably does nothing when tapped. */}
      {entry.story_cluster_id != null && (
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2">
          <button
            type="button"
            data-card-interactive
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              setRelatedExpanded((v) => !v)
            }}
            className="inline-flex items-center gap-1 whitespace-nowrap min-h-[44px] -my-2 text-ios-caption text-accent active:opacity-60"
          >
            <span aria-hidden="true">🔗</span>
            <span>{relatedExpanded ? 'Hide related coverage' : 'Related coverage'}</span>
          </button>
        </div>
      )}
      {relatedExpanded && (
        <div onClick={(e) => e.stopPropagation()} className="mt-2 space-y-1.5">
          {relatedError ? (
            <p className="text-ios-caption text-label-secondary italic">couldn't load related coverage — try again later</p>
          ) : related === undefined ? (
            <p className="text-ios-caption text-label-tertiary italic">looking for related coverage…</p>
          ) : related === null ? (
            <p className="text-ios-caption text-label-tertiary italic">no other coverage found</p>
          ) : (
            related.articles.map((a) => (
              <a
                key={a.entry_id}
                href={safeExternalUrl(a.url)}
                target="_blank"
                rel="noopener noreferrer"
                data-card-interactive
                onClick={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  // ``safeExternalUrl`` rewrites reddit.com -> old.reddit.com so
                  // the popped-out tab doesn't show the "Get the App" overlay.
                  noteStoryOpened()
                  window.open(safeExternalUrl(a.url), '_blank', 'noopener,noreferrer')
                }}
                className="flex items-start gap-2 group"
              >
                <span className="mt-0.5 shrink-0">
                  <SourceIcon src={a.favicon_path} name={a.source_name} size={16} />
                </span>
                <span className="min-w-0">
                  <span className="block text-ios-caption text-label-primary group-hover:underline">
                    {a.title}
                  </span>
                  <span className="flex items-center gap-1.5 mt-0.5">
                    <span className="text-[11px] text-label-tertiary truncate">{a.source_name}</span>
                    {a.framing_tone && a.framing_tone !== 'neutral' && (
                      <span
                        className={`shrink-0 text-[11px] uppercase tracking-wide border rounded-full px-1 ${RELATED_TONE_STYLES[a.framing_tone] ?? ''}`}
                      >
                        {a.framing_tone}
                      </span>
                    )}
                  </span>
                </span>
              </a>
            ))
          )}
        </div>
      )}
      {/* Inline summary: the full AI summary (3-4 sentences), under
          the title row and above the action bar. While it is open the
          preview under the title is gone (one text, once).
          ``onClick`` stops propagation because the card-level
          onContextMenu / long-press paths would otherwise fire when
          the user is just trying to read the summary text. The
          click target is the card itself; selecting text inside
          works because the inner ``e.stopPropagation`` only fires
          on a true click, not on text-selection dragstart.

          No ``line-clamp`` here — the user explicitly expanded the
          summary to read it, and long LLM summaries getting
          truncated to 3 lines made them visually concatenated (the
          browser would overflow the text horizontally inside the
          narrow clamp, producing the "stuck together" appearance).
          Removing the clamp lets the card grow to fit whatever the
          summary is. The user collapses via the chevron to hide it
          again. The podcast + reddit summary blocks below already
          work this way (no clamp). */}
      {expanded && (
        <div
          onClick={(e) => e.stopPropagation()}
          className="mt-2 text-ios-caption text-label-secondary leading-relaxed whitespace-pre-wrap break-words"
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
      {/* The action bar (D1): ▲ ▼, Save and Share, then the Summary
          chevron and ⋯ pushed right. Six 44×44 targets with no gaps,
          264px in all, which fits from a 310px viewport. The bar
          bleeds 12px left and 8px right into the card's padding, as
          the old footer did, so the glyphs line up with the text.
          Votes do what they do on Reddit: the card stays, the arrow
          colours, and a second tap takes the vote back. Saving is
          silent; a hide and an unsave say so with Undo (App). */}
      <div className="mt-2 -mb-1 -ml-3 -mr-2 flex items-center">
        {onVote && (
          <>
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
                onVote(next)
              }}
              aria-label={vote === 'up' ? 'remove upvote' : 'upvote'}
              aria-pressed={vote === 'up'}
              title="more like this"
              className={`shrink-0 w-11 h-11 flex items-center justify-center rounded-full transition-colors active:bg-bg-elevated
                          ${vote === 'up' ? 'text-orange-500' : 'text-label-secondary hover:text-orange-500'}`}
            >
              <ArrowUpIcon className="w-5 h-5" filled={vote === 'up'} />
            </button>
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
                onVote(next)
              }}
              aria-label={vote === 'down' ? 'remove downvote' : 'downvote'}
              aria-pressed={vote === 'down'}
              title="less like this"
              className={`shrink-0 w-11 h-11 flex items-center justify-center rounded-full transition-colors active:bg-bg-elevated
                          ${vote === 'down' ? 'text-indigo-400' : 'text-label-secondary hover:text-indigo-400'}`}
            >
              <ArrowDownIcon className="w-5 h-5" filled={vote === 'down'} />
            </button>
          </>
        )}
        {/* Save: a bookmark, filled when saved. ``data-star`` stays as
            the keyboard shortcut's handle. ``bookmark`` is recorded on
            a save only, never an unsave. */}
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
              if (!starred) recordImmediate({ entry_id: entry.id, type: 'bookmark' })
              onStar()
            }}
            aria-label={starred ? 'unsave' : 'save'}
            aria-pressed={!!starred}
            title={starred ? 'unsave (b)' : 'save (b)'}
            className={`shrink-0 w-11 h-11 flex items-center justify-center rounded-full active:bg-bg-elevated
                        ${starred ? 'text-accent' : 'text-label-secondary'}`}
          >
            <BookmarkIcon className="w-5 h-5" filled={!!starred} />
          </button>
        )}
        <button
          type="button"
          data-card-interactive
          data-card-share
          disabled={!canShare}
          onMouseDown={(e) => e.stopPropagation()}
          onTouchStart={(e) => e.stopPropagation()}
          onClick={(e) => {
            e.preventDefault()
            e.stopPropagation()
            share()
          }}
          aria-label="share"
          title="share"
          className="shrink-0 w-11 h-11 flex items-center justify-center rounded-full text-label-secondary active:bg-bg-elevated disabled:opacity-40"
        >
          <ShareIcon className="w-5 h-5" />
        </button>
        <div className="ml-auto flex items-center">
          {/* Per-card summary chevron. Same ``data-card-interactive``
              guard so a long-press / right-click on the card itself
              doesn't fire while the user is targeting the chevron.
              Title alternates by state so the hover hint matches the
              keyboard shortcut (``s``). */}
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
              className={`shrink-0 w-11 h-11 flex items-center justify-center rounded-full active:bg-bg-elevated
                          ${expanded ? 'text-accent' : 'text-label-secondary'}`}
            >
              {/* Rotate 180° when expanded so the chevron flips up —
                  standard iOS disclosure-indicator idiom. */}
              <ChevronDownIcon className={`w-5 h-5 transition-transform ${expanded ? 'rotate-180' : ''}`} />
            </button>
          )}
          {/* ⋯: the story's sheet (ActionSheet.tsx). The button is
              handed over so focus comes back to it, and so the sheet
              knows which card opened it when Hide takes the card away. */}
          <button
            type="button"
            data-card-interactive
            data-card-more
            onMouseDown={(e) => e.stopPropagation()}
            onTouchStart={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              openActionSheet({ title: entry.title, actions: buildActions(), from: e.currentTarget })
            }}
            aria-label="more actions"
            aria-haspopup="dialog"
            title="more"
            className="shrink-0 w-11 h-11 flex items-center justify-center rounded-full text-label-secondary active:bg-bg-elevated"
          >
            <MoreIcon className="w-5 h-5" />
          </button>
        </div>
      </div>
      </article>
    </div>
  )
}

// ``memo`` wrap. Default shallow-equal check would still let the
// inline ref / callback lambdas at the Column level defeat the
// memo, because they're allocated fresh on every Column render.
// So we use a custom areEqual that ignores the per-card callback
// props (``cardRef``, ``onActivate``, ``onToggleSummary``).
// They're inherently per-render at the Column layer; the parent
// passes them through to whatever the latest closure captures. The
// data-driven props — ``entry``, ``sourceName``, ``dimmed``,
// ``selected``, ``category``, ``expanded`` — are what we actually
// re-render against.
//
// Trade-off: if a callback's logic changes without the data
// changing, this Card won't re-render. That can't happen in
// practice because callbacks here only depend on data props
// (``onOpenEntry(e)`` reads from the same entry) and App-level
// state. If a future caller starts passing callbacks
// that close over mutable refs, lift this to a stable ref and
// re-evaluate.
function _cardPropsEqual(prev: Props, next: Props): boolean {
  return (
    prev.entry === next.entry &&
    prev.sourceName === next.sourceName &&
    prev.sourceFaviconPath === next.sourceFaviconPath &&
    prev.dimmed === next.dimmed &&
    prev.selected === next.selected &&
    prev.category === next.category &&
    prev.expanded === next.expanded &&
    prev.starred === next.starred &&
    prev.vote === next.vote
  )
}

export const Card = memo(CardInner, _cardPropsEqual) as typeof CardInner

// The crossed-out eye in the swipe reveal ("Hide" slides in under the
// card): an almond outline, a filled pupil and a diagonal strike, the
// standard "hidden" glyph (it mirrors the eye-off icon in the Settings
// tab strip).
function EyeOffIcon({ className }: { className?: string }) {
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
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" />
      <circle cx="12" cy="12" r="3" fill="currentColor" />
      <line x1="3" y1="3" x2="21" y2="21" />
    </svg>
  )
}

// Bookmark (Save). Outline when the story isn't saved, filled when it
// is: the fill uses ``currentColor`` so the button's
// ``text-label-secondary`` / ``text-accent`` drives both.
function BookmarkIcon({ className, filled = false }: { className?: string; filled?: boolean }) {
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
      <path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z" />
    </svg>
  )
}

// Share: iOS's box with an arrow out of its top.
function ShareIcon({ className }: { className?: string }) {
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
      <path d="M12 3v12" />
      <polyline points="8 7 12 3 16 7" />
      <path d="M5 11v8a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-8" />
    </svg>
  )
}

// ⋯: three dots, the same glyph as the tab's ⋯ (Column.tsx).
function MoreIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true">
      <circle cx="6" cy="12" r="1.75" />
      <circle cx="12" cy="12" r="1.75" />
      <circle cx="18" cy="12" r="1.75" />
    </svg>
  )
}

// Down-chevron used for the per-card summary disclosure. 2px stroke
// matches StarIcon so the two buttons read as visual siblings on the
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
// stroke-based icons here (Eye/Star/Chevron) rather than a
// visually foreign import. Slightly heavier stroke (2.5 vs the 2 the
// other icons use) since these are the primary voting affordance and
// benefit from a touch more presence at 16px.
// ``filled`` swaps the open stem+chevron path (idle) for a solid
// closed triangle (voted) — mirrors the outline-vs-filled pattern
// StarIcon already uses, but needs a genuinely different
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
function Thumbnail({ path, title, url, entryId, onOpen }: { path: string; title: string; url: string; entryId: number; onOpen?: () => void }) {
  const wrapperRef = useRef<HTMLDivElement | null>(null)
  const open = () => {
    // The thumbnail click is its own affordance and uses
    // ``e.stopPropagation`` to bypass the article-level handlers —
    // so the headline's onClick won't fire. Record the click event
    // here so the recommendation ranker sees both kinds of opens,
    // and tell the card the story was opened (it is seen now).
    recordImmediate({ entry_id: entryId, type: 'click' })
    onOpen?.()
    // ``safeExternalUrl`` rewrites reddit.com -> old.reddit.com so
    // the popped-out tab doesn't show the "Get the App" overlay.
    window.open(safeExternalUrl(url), '_blank', 'noopener,noreferrer')
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
                 [@container(max-width:379px)]:w-[72px] [@container(max-width:379px)]:aspect-square
                 [@container(max-width:299px)]:w-full [@container(max-width:299px)]:aspect-video
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

// ---- Context menu (the ⋯ sheet's rows, at a right-click) ----
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
      toast('Link copied')
    } catch {
      toast("Couldn't copy the link", 'error')
    }
  }
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(url).then(
      () => toast('Link copied'),
      () => fallback(),
    )
  } else {
    fallback()
  }
}

function showContextMenu(
  x: number,
  y: number,
  actions: SheetAction[],
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
    btn.className = `block w-full text-left px-3 py-1.5 active:bg-bg-surface disabled:opacity-40 ${
      item.destructive ? 'text-red-400' : 'text-label-primary'
    }`
    btn.disabled = !!item.disabled
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





