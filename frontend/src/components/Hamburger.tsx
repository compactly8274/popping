type Props = {
  // Whether the Menu it opens is open, for assistive tech.
  open: boolean
  onClick: () => void
}

// The one Menu button in the top bar (≡). iOS-style "list" icon (three
// short horizontal lines), a 44×44 tap target to match the HIG
// minimum, with a circular active state that mirrors the iOS nav-bar
// icon-button treatment. The lines sit at
// the SF Symbols "line.3.horizontal" weight — 1.75px stroke, rounded
// caps. ``text-label-primary`` keeps the icon bright on the dark app
// background.
export function Hamburger({ open, onClick }: Props) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label="open menu"
      aria-expanded={open}
      aria-haspopup="dialog"
      className="w-11 h-11 flex items-center justify-center rounded-full text-label-primary active:bg-bg-elevated"
    >
      <svg
        width="22"
        height="22"
        viewBox="0 0 22 22"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.75}
        strokeLinecap="round"
        aria-hidden="true"
      >
        <line x1="4" y1="7"  x2="18" y2="7" />
        <line x1="4" y1="11" x2="18" y2="11" />
        <line x1="4" y1="15" x2="18" y2="15" />
      </svg>
    </button>
  )
}
