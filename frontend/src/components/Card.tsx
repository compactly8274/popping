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

import { memo, useEffect, useRef, useState, type MouseEvent, type TouchEvent } from 'react'
import { api, type Entry } from '../api'
import { recordBatched, recordImmediate } from '../lib/interactions'
import { toast } from './Toast'

type Props = {
  entry: Entry
  sourceName?: string
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
  // Per-card inline summary. When ``expanded`` is true the card
  // fetches the cached summary once and renders it under the
  // title. ``onToggleSummary`` flips the expanded bit. Independent
  // of mark-read — expanding a card doesn't mark it.
  expanded?: boolean
  onToggleSummary?: () => void
}

// Map a category name to a Tailwind background class for the left
// stripe. Kept inline (no new module) because the call site is the
// only consumer and we want it obvious from Card.tsx which colors
// map to which categories. ``other`` falls through to a neutral
// slate so unrecognized categories don't render without a stripe.
function categoryStripeClass(category: string | undefined): string {
  switch (category) {
    case 'news':     return 'bg-blue-500/70'
    case 'tech':     return 'bg-violet-500/70'
    case 'vulns':    return 'bg-red-500/70'
    case 'science':  return 'bg-emerald-500/70'
    case 'finance':  return 'bg-amber-500/70'
    case 'policy':   return 'bg-cyan-500/70'
    case 'longform': return 'bg-rose-500/70'
    case 'deals':    return 'bg-lime-500/70'
    default:         return 'bg-neutral-600/70'
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

// Long-press threshold. ~500ms is the conventional "long enough to
// mean something, short enough not to feel laggy". The 10px move
// tolerance lets a finger jitter slightly without cancelling.
const LONG_PRESS_MS = 500
const LONG_PRESS_MOVE_TOLERANCE_PX = 10

export function _CardInner({ entry, sourceName, unread, selected, cardRef, onActivate, category, onMarkRead, expanded, onToggleSummary, onHide }: Props) {
  const band = scoreBand(entry.composite_score)
  const stripeClass = categoryStripeClass(category)
  // Touch tracking for long-press → copy URL.
  // Kept in refs so the values don't trigger re-renders mid-press.
  const touchStart = useRef<{ x: number; y: number; t: number; id: number; onInteractiveChild: boolean } | null>(null)
  const longPressTimer = useRef<number | null>(null)

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

  const onTouchStart = (e: TouchEvent<HTMLElement>) => {
    // Single finger only. Two-finger gestures (pinch, etc.) skip the
    // long-press path entirely — we don't want to fire copy on pinch.
    if (e.touches.length !== 1) {
      clearLongPress()
      touchStart.current = null
      return
    }
    const t = e.touches[0]
    // Mark whether the touch originated on the thumbnail (or any
    // other interactive child like the future score chip). The
    // long-press path skips when this is set so we don't fire
    // copy-URL on top of the thumbnail's own click handler.
    const target = e.target as HTMLElement
    const onInteractiveChild = !!target.closest('[data-card-interactive]')
    touchStart.current = {
      x: t.clientX,
      y: t.clientY,
      t: Date.now(),
      id: t.identifier,
      onInteractiveChild,
    }
    if (onInteractiveChild) {
      // Don't arm the timer at all — saves the cleanup path too.
      return
    }
    clearLongPress()
    longPressTimer.current = window.setTimeout(() => {
      // Re-check the touch is still the same finger and roughly in
      // place. If the user has already started swiping the column we
      // bail so we don't fire copy mid-swipe.
      const start = touchStart.current
      if (!start || start.onInteractiveChild) return
      longPressTimer.current = null
      void copyUrl(entry.url)
    }, LONG_PRESS_MS)
  }

  const onTouchMove = (e: TouchEvent<HTMLElement>) => {
    const start = touchStart.current
    if (!start) return
    const t = e.touches[0]
    if (Math.abs(t.clientX - start.x) > LONG_PRESS_MOVE_TOLERANCE_PX ||
        Math.abs(t.clientY - start.y) > LONG_PRESS_MOVE_TOLERANCE_PX) {
      clearLongPress()
    }
  }

  const onTouchEnd = (e: TouchEvent<HTMLElement>) => {
    const start = touchStart.current
    clearLongPress()
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
    if (timerFired && dur >= LONG_PRESS_MS) {
      e.preventDefault()
      e.stopPropagation()
    }
  }

  const onTouchCancel = () => {
    clearLongPress()
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
    if (onHide) {
      actions.push({
        label: 'Hide this entry',
        onClick: () => onHide(),
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

  // Visual state for unread vs selected. Read cards stay at 100%
  // opacity but lose the ring — they're not "hidden", just clearly
  // seen. Selected cards get a stronger accent ring regardless of
  // read state so the keyboard focus is unambiguous.
  const opacityClass = unread ? 'opacity-100' : 'opacity-60'
  const ringClass = selected
    ? 'ring-2 ring-accent/70'
    : unread
      ? 'ring-1 ring-accent/40'
      : ''

  return (
    <article
      ref={stableCardRef}
      data-card-id={entry.id}
      // ``tabIndex`` only on the selected card so the rest of the
      // grid isn't a giant tab-stop forest. Arrow keys set
      // tabIndex={0} and call focus() when the user moves with the
      // keyboard.
      tabIndex={selected ? 0 : -1}
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
      onTouchEnd={onTouchEnd}
      onTouchCancel={onTouchCancel}
      onContextMenu={onContextMenu}
      className={`group relative rounded-ios-lg bg-bg-surface border border-hairline p-4 pl-5
                  hover:-translate-y-px hover:shadow-glow-md
                  transition-[transform,box-shadow,border-color] duration-200
                  ${opacityClass} ${ringClass}`}
    >
      {/* Category stripe. 2px wide, full height of the card. Lives
          outside the padding flow so it doesn't shift content when
          a category is/isn't known. ``aria-hidden`` because the
          color is decorative — the category name (if shown) carries
          the semantic. */}
      <div
        aria-hidden="true"
        className={`absolute left-0 top-0 bottom-0 w-0.5 rounded-l-ios-lg ${stripeClass}`}
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
        {entry.image_path && (
          <Thumbnail
            path={entry.image_path}
            title={entry.title}
            url={entry.url}
            entryId={entry.id}
          />
        )}
        {/* Score badge. The gradient gives the badge a tiny bit of
            depth; ``ring-1 ring-white/10`` is a faint inner highlight
            that reads as "this is a label" rather than "this is a
            button". Title shows the raw number for power users. */}
        <span
          className={`shrink-0 inline-flex items-center rounded-ios px-2 py-0.5 text-xs font-semibold text-white ring-1 ring-white/10 ${band.color}`}
          title={`composite score ${entry.composite_score.toFixed(0)}`}
        >
          {entry.composite_score.toFixed(0)}
        </span>
      </div>
      <div className="flex items-center gap-2 text-ios-caption text-label-secondary">
        {sourceName && <span className="font-medium text-label-primary">{sourceName}</span>}
        {sourceName && <span>·</span>}
        <time dateTime={entry.published_at ?? ''}>{timeAgo(entry.published_at)}</time>
        {/* Per-card summary chevron. Sits inline on the meta row so
            it reads as part of the same toolbar as the ✓ button. Same
            ``data-card-interactive`` guard so a long-press / right-
            click on the card itself doesn't fire while the user is
            targeting the chevron. Title alternates by state so the
            hover hint matches the keyboard shortcut (``s``).
            Visually mirrors the ✓'s hover-reveal treatment — hidden
            until hover on desktop, always visible on touch. */}
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
            className={`ml-auto w-7 h-7 flex items-center justify-center rounded-full text-label-secondary active:bg-bg-elevated
                        ${expanded ? 'opacity-100 text-accent' : 'opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100'}`}
          >
            {/* Rotate 180° when expanded so the chevron flips up —
                standard iOS disclosure-indicator idiom. */}
            <ChevronDownIcon className={`w-4 h-4 transition-transform ${expanded ? 'rotate-180' : ''}`} />
          </button>
        )}
        {/* Per-card mark-read ✓. Sits on the trailing edge of the
            meta row so it's visually associated with the read-state
            line above it. ``opacity-0 group-hover:opacity-100``
            keeps it out of the way on desktop until the user hovers;
            the ``@media (hover: none)`` escape hatch makes it always
            visible on touch where there's no hover state to discover
            it. ``onMouseDown`` swallows the press so it doesn't
            bubble into the article's long-press / context-menu
            paths. Fires ``view`` so the ranker sees the same signal
            it sees for headline and thumbnail clicks. */}
        {onMarkRead && (
          // The ✓ is ALWAYS visible so the user can see at a
          // glance that the action is available. The previous
          // hover-only treatment (“opacity-0 group-hover:opacity-100”)
          // hid the affordance from anyone who didn't already
          // know it existed. The visual weight is still muted
          // (text-label-secondary for unread, text-accent for
          // read) so it doesn't compete with the title. On read
          // cards the checkmark is filled so the user can tell
          // at a glance which cards are dimmed and which still
          // need attention.
          <button
            type="button"
            data-card-interactive
            data-mark-read
            onMouseDown={(e) => e.stopPropagation()}
            onTouchStart={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              // "view" + manual read flip. The ranker sees the
              // same signal it sees for headline and thumbnail
              // clicks; the column sees the manual readEntries
              // flip that dims the card.
              recordImmediate({ entry_id: entry.id, type: 'view' })
              onMarkRead()
            }}
            aria-label="mark this card as read"
            aria-pressed={!unread}
            title="mark as read (m)"
            className={`shrink-0 w-7 h-7 flex items-center justify-center rounded-full active:bg-bg-elevated
                        ${onToggleSummary ? '' : 'ml-auto'}
                        ${unread ? 'text-label-secondary' : 'text-accent'}`}
          >
            <CheckIcon className="w-4 h-4" filled={!unread} />
          </button>
        )}
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
    </article>
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
    prev.unread === next.unread &&
    prev.selected === next.selected &&
    prev.category === next.category &&
    prev.expanded === next.expanded
  )
}

export const Card = memo(_CardInner, _cardPropsEqual) as typeof _CardInner

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
      className="shrink-0 w-24 sm:w-32 aspect-video rounded-ios overflow-hidden bg-bg-elevated cursor-zoom-in focus:outline-none focus:ring-2 focus:ring-accent/60"
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

