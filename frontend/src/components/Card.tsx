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

function scoreBand(score: number): { color: string; label: string } {
  if (score >= 75) return { color: 'bg-score-hot',   label: 'hot' }
  if (score >= 50) return { color: 'bg-score-warm',  label: 'warm' }
  if (score >= 25) return { color: 'bg-score-cool',  label: 'cool' }
  return                { color: 'bg-score-cold',  label: 'cold' }
}

// Long-press threshold. ~500ms is the conventional "long enough to
// mean something, short enough not to feel laggy". The 10px move
// tolerance lets a finger jitter slightly without cancelling.
const LONG_PRESS_MS = 500
const LONG_PRESS_MOVE_TOLERANCE_PX = 10

export function Card({ entry, sourceName, unread, selected, cardRef, onActivate }: Props) {
  const band = scoreBand(entry.composite_score)
  // Touch tracking for long-press → copy URL.
  // Kept in refs so the values don't trigger re-renders mid-press.
  const touchStart = useRef<{ x: number; y: number; t: number; id: number } | null>(null)
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
    touchStart.current = { x: t.clientX, y: t.clientY, t: Date.now(), id: t.identifier }
    clearLongPress()
    longPressTimer.current = window.setTimeout(() => {
      // Re-check the touch is still the same finger and roughly in
      // place. If the user has already started swiping the column we
      // bail so we don't fire copy mid-swipe.
      const start = touchStart.current
      if (!start) return
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
    // confusing. ``changedTouches`` survives after the touchend so we
    // can compare ids; if the lifted finger matches the one we
    // tracked and the gesture was long, suppress the default.
    const dur = Date.now() - start.t
    if (dur >= LONG_PRESS_MS) {
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
      className={`group rounded-lg border border-slate-800 bg-slate-900/60 p-4 hover:border-slate-700 transition ${opacityClass} ${ringClass}`}
    >
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
        {entry.image_path && <Thumbnail path={entry.image_path} title={entry.title} />}
        <span
          className={`shrink-0 inline-flex items-center rounded px-2 py-0.5 text-xs font-semibold text-white ${band.color}`}
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

// 96 px square. The wrapper div reserves the box on first paint so
// the score badge doesn't shift when the image arrives (or fails).
// ``onError`` hides the wrapper, not the bare <img> — hiding the img
// alone leaves an empty 96px hole in the layout, which would have
// shifted the score badge left in the original code.
function Thumbnail({ path, title }: { path: string; title: string }) {
  const wrapperRef = useRef<HTMLDivElement | null>(null)
  return (
    <div
      ref={wrapperRef}
      className="shrink-0 w-24 h-24 rounded overflow-hidden bg-slate-800"
      title={title}
    >
      <img
        src={`/assets/${path}`}
        alt=""
        loading="lazy"
        decoding="async"
        width={96}
        height={96}
        className="w-24 h-24 object-cover"
        onError={() => {
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