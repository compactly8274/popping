// What part of the screen is actually visible. On iOS the on-screen
// keyboard shrinks the visual viewport but not the layout viewport, so
// anything pinned to the bottom of the layout (a toast, a sheet's
// buttons) ends up under the keyboard. These hooks let such elements
// follow the visible area instead.

import { useEffect, useState } from 'react'

export type VisualViewportState = {
  height: number
  offsetTop: number
  // How much of the layout viewport's bottom is covered (the keyboard).
  kbInset: number
  keyboardOpen: boolean
}

const ZERO: VisualViewportState = { height: 0, offsetTop: 0, kbInset: 0, keyboardOpen: false }

function read(): VisualViewportState {
  const vv = typeof window !== 'undefined' ? window.visualViewport : null
  if (!vv) return ZERO
  const layoutHeight = document.documentElement.clientHeight
  return {
    height: vv.height,
    offsetTop: vv.offsetTop,
    kbInset: Math.max(0, Math.round(layoutHeight - vv.height - vv.offsetTop)),
    // A real keyboard is far taller than the toolbar resizing Safari
    // does while scrolling.
    keyboardOpen: layoutHeight - vv.height > 120,
  }
}

export function useVisualViewport(): VisualViewportState {
  const [state, setState] = useState<VisualViewportState>(read)
  useEffect(() => {
    const vv = window.visualViewport
    if (!vv) return
    let frame = 0
    const onChange = () => {
      if (frame) return
      frame = requestAnimationFrame(() => {
        frame = 0
        setState(read())
      })
    }
    vv.addEventListener('resize', onChange)
    vv.addEventListener('scroll', onChange)
    return () => {
      vv.removeEventListener('resize', onChange)
      vv.removeEventListener('scroll', onChange)
      if (frame) cancelAnimationFrame(frame)
    }
  }, [])
  return state
}

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => typeof window !== 'undefined' && window.matchMedia(query).matches,
  )
  useEffect(() => {
    const mql = window.matchMedia(query)
    const onChange = () => setMatches(mql.matches)
    onChange()
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])
  return matches
}
