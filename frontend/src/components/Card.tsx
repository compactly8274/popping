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

import { useRef, type MouseEvent, type TouchEvent } from 'react'
import type { Entry } from '../api'
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
    default:         return 'bg-slate-600/70'
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
  if (score >= 25) return { color: 'bg-gradient-to-br from-blue-500 to-blue-700',   label: 'cool' }
  return               { color: 'bg-gradient-to-br from-slate-500 to-slate-700',   label: 'cold' }
}

// Long-press threshold. ~500ms is the conventional "long enough to
// mean something, short enough not to feel laggy". The 10px move
// tolerance lets a finger jitter slightly without cancelling.
const LONG_PRESS_MS = 500
const LONG_PRESS_MOVE_TOLERANCE_PX = 10

export function Card({ entry, sourceName, unread, selected, cardRef, onActivate, category }: Props) {
  const band = scoreBand(entry.composite_score)
  const stripeClass = categoryStripeClass(category)
  // Touch tracking for long-press → copy URL.
  // Kept in refs so the values don't trigger re-renders mid-press.
  const touchStart = useRef<{ x: number; y: number; t: number; id: number; onInteractiveChild: boolean } | null>(null)
  const longPressTimer = useRef<number | null>(null)

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
  // suppressed so we can offer "copy link" + "open in new tab" without
  // a third-party library.
  const onContextMenu = (e: MouseEvent<HTMLElement>) => {
    e.preventDefault()
    // Build a tiny inline menu at the cursor. After the user picks
    // (or dismisses) the menu, dispose. Kept inline rather than a
    // portal because the click-outside dismissal is the only thing
    // we need and a fixed-positioned div with a backdrop handles it.
    showContextMenu(e.clientX, e.clientY, entry.url)
  }

  // Visual state for unread vs selected. Read cards stay at 100%
  // opacity but lose the ring — they're not "hidden", just clearly
  // seen. Selected cards get a stronger ring regardless of read
  // state so the keyboard focus is unambiguous.
  const opacityClass = unread ? 'opacity-100' : 'opacity-60'
  const ringClass = selected
    ? 'ring-2 ring-blue-500/70'
    : unread
      ? 'ring-1 ring-blue-500/40'
      : ''

  return (
    <article
      ref={cardRef}
      data-card-id={entry.id}
      // ``tabIndex`` only on the selected card so the rest of the
      // grid isn't a giant tab-stop forest. F6 sets tabIndex={0}
      // and calls focus() when the user moves with the keyboard.
      tabIndex={selected ? 0 : -1}
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
      onTouchEnd={onTouchEnd}
      onTouchCancel={onTouchCancel}
      onContextMenu={onContextMenu}
      className={`group relative rounded-lg border border-slate-800 bg-slate-900/60 p-4 pl-5
                  hover:border-slate-700 hover:-translate-y-px hover:shadow-glow-md
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
        className={`absolute left-0 top-0 bottom-0 w-0.5 rounded-l-lg ${stripeClass}`}
      />
      <div className="flex items-start justify-between gap-3 mb-2">
        <a
          href={entry.url}
          target="_blank"
          rel="noopener noreferrer"
          aria-label="open in new tab"
          title="opens in a new tab"
          onClick={() => onActivate?.()}
          className="flex-1 min-w-0 flex items-start gap-1.5 text-base font-medium text-slate-100 hover:text-white line-clamp-2"
        >
          <span className="min-w-0">{entry.title}</span>
          {/* "↗" affordance. Sits inline at the end of the title so it
              reads as part of the link, not a separate control. Group-
              hover brightens it on devices that have a hover state;
              touch devices get the static ``text-slate-500`` baseline. */}
          <span
            aria-hidden="true"
            className="shrink-0 text-slate-500 group-hover:text-slate-300 transition text-sm leading-tight"
          >
            ↗
          </span>
        </a>
        {entry.image_path && (
          <Thumbnail
            path={entry.image_path}
            title={entry.title}
            url={entry.url}
          />
        )}
        {/* Score badge. The gradient gives the badge a tiny bit of
            depth; ``ring-1 ring-white/10`` is a faint inner highlight
            that reads as "this is a label" rather than "this is a
            button". Title shows the raw number for power users. */}
        <span
          className={`shrink-0 inline-flex items-center rounded px-2 py-0.5 text-xs font-semibold text-white ring-1 ring-white/10 ${band.color}`}
          title={`composite score ${entry.composite_score.toFixed(0)}`}
        >
          {entry.composite_score.toFixed(0)}
        </span>
      </div>
      <div className="flex items-center gap-2 text-xs text-slate-400">
        {sourceName && <span className="font-medium text-slate-300">{sourceName}</span>}
        {sourceName && <span>·</span>}
        <time>{timeAgo(entry.published_at)}</time>
      </div>
    </article>
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
function Thumbnail({ path, title, url }: { path: string; title: string; url: string }) {
  const wrapperRef = useRef<HTMLDivElement | null>(null)
  const open = () => {
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
      className="shrink-0 w-24 sm:w-32 aspect-video rounded-md overflow-hidden bg-bg-elevated cursor-zoom-in focus:outline-none focus:ring-2 focus:ring-blue-500/60"
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

function showContextMenu(x: number, y: number, url: string) {
  // Dismiss any existing menu. There's only ever one, but a fast
  // double right-click on the same card would otherwise stack them.
  document.getElementById('card-context-menu')?.remove()

  const menu = document.createElement('div')
  menu.id = 'card-context-menu'
  menu.className =
    'fixed z-50 min-w-[180px] rounded border border-slate-700 bg-slate-900 shadow-xl text-sm py-1'
  // Clamp to viewport so the menu never renders off-screen.
  const left = Math.min(x, window.innerWidth - 200)
  const top = Math.min(y, window.innerHeight - 100)
  menu.style.left = `${left}px`
  menu.style.top = `${top}px`

  const items: Array<{ label: string; onClick: () => void }> = [
    { label: 'Copy link', onClick: () => copyUrl(url) },
    {
      label: 'Open in new tab',
      onClick: () => {
        window.open(url, '_blank', 'noopener,noreferrer')
      },
    },
  ]
  for (const item of items) {
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className =
      'block w-full text-left px-3 py-1.5 text-slate-200 hover:bg-slate-800'
    btn.textContent = item.label
    btn.onclick = () => {
      item.onClick()
      menu.remove()
      backdrop.remove()
    }
    menu.appendChild(btn)
  }

  const backdrop = document.createElement('div')
  backdrop.className = 'fixed inset-0 z-40'
  backdrop.onclick = () => {
    menu.remove()
    backdrop.remove()
  }
  document.body.appendChild(backdrop)
  document.body.appendChild(menu)

  // Esc dismisses too. Listeners are per-menu so multiple opens
  // don't fight over a single handler.
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      menu.remove()
      backdrop.remove()
      document.removeEventListener('keydown', onKey, true)
    }
  }
  document.addEventListener('keydown', onKey, true)
}