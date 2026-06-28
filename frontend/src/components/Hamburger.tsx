type Props = {
  onClick: () => void
}

export function Hamburger({ onClick }: Props) {
  return (
    <button
      onClick={onClick}
      className="rounded p-1 sm:p-2 text-slate-300 active:bg-slate-800 [@media(hover:hover)]:hover:text-white [@media(hover:hover)]:hover:bg-slate-800"
      aria-label="open menu"
    >
      <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        <line x1="3" y1="6"  x2="19" y2="6" />
        <line x1="3" y1="11" x2="19" y2="11" />
        <line x1="3" y1="16" x2="19" y2="16" />
      </svg>
    </button>
  )
}