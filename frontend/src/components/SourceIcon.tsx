// Tiny favicon + colored-letter fallback.
//
// Used in three places (Drawer source list, FeedManager row, Card
// metadata strip) where the previous code rendered a bare
// ``<img src={/assets/${path}} onError={display:none}>``. The bare
// pattern had two failure modes worth fixing:
//
//   1. If ``favicon_path`` was null, the row rendered an empty box
//      and the user couldn't tell whether the source had no favicon
//      or just hadn't fetched yet. The letter fallback makes every
//      source visually present from the first paint.
//
//   2. If the image failed (404, 403, CORS, network blip), the
//      ``display:none`` swap silently hid the failure. ``console.warn``
//      surfaces the URL in DevTools so a developer chasing a bug can
//      see which CDN is the offender without standing up a debugger.
//
// The colored letter is a stable hue from a djb2 hash of the name,
// so a source keeps the same color across the dashboard — toggling
// the same source in different surfaces doesn't make it look like
// two different things.

import { useState } from 'react'

type Props = {
  // Path under /assets, e.g. "favicons/3.png". When null OR the
  // image errors, the colored-letter fallback renders.
  src: string | null
  // Source name — used for the letter and the hue. ``aria-hidden``
  // covers the letter case so screen readers don't read "B, B, B, B"
  // for every BBC row.
  name: string
  // Box size in px. 14/16/20 are the call sites today.
  size?: number
}

// djb2 — five lines, deterministic, no deps. Mod 360 keeps the hue in
// the usable range; 45% saturation + 35% lightness give a slate-
// friendly color that doesn't fight the dark theme.
function stableHue(name: string): number {
  let h = 5381
  for (let i = 0; i < name.length; i++) {
    h = ((h << 5) + h + name.charCodeAt(i)) & 0xffffffff
  }
  return Math.abs(h) % 360
}

export function SourceIcon({ src, name, size = 16 }: Props) {
  const [errored, setErrored] = useState(false)

  // Show the letter when we never had a path, or when the image
  // failed to load. Setting ``errored`` from onError is a state flip
  // rather than ``display: none`` because the wrapper is the same
  // element either way — no layout shift, no flash of empty box.
  const useFallback = !src || errored

  if (useFallback) {
    const letter = (name.trim().charAt(0) || '?').toUpperCase()
    const hue = stableHue(name || '?')
    return (
      <span
        aria-hidden="true"
        title={name}
        className="shrink-0 inline-flex items-center justify-center rounded-sm text-[9px] font-semibold text-white/90 leading-none"
        style={{
          width: size,
          height: size,
          background: `hsl(${hue} 45% 35%)`,
        }}
      >
        {letter}
      </span>
    )
  }

  return (
    <img
      src={`/assets/${src}`}
      alt=""
      width={size}
      height={size}
      loading="lazy"
      decoding="async"
      title={name}
      className="shrink-0 rounded-sm bg-bg-elevated"
      style={{ width: size, height: size }}
      onError={() => {
        // Surface the URL in DevTools so a developer chasing a 403
        // sees which host is the offender without standing up a
        // debugger. Kept at warn level — frequent enough to notice
        // without being noisy enough to clutter the console.
        console.warn(`SourceIcon failed to load: /assets/${src}`)
        setErrored(true)
      }}
    />
  )
}