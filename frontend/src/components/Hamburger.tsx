type Props = {
  onClick: () => void
}

// iOS-style "list" icon (three short horizontal lines). 44×44 tap
// target to match the HIG minimum, with a circular active state that
// mirrors the iOS nav-bar icon-button treatment. The lines sit at
// the SF Symbols "line.3.horizontal" weight — 1.75px stroke, rounded
// caps. ``text-label-primary`` keeps the icon bright on the dark app
// background.
export function Hamburger({ onClick }: Props) {
  return (
    <button
      onClick={onClick}
      aria-label="open menu"
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
