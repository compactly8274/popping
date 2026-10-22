// iOS grouped-list primitives: a labelled section of rounded rows, a
// 44px row, and the chevron. The Menu (Drawer.tsx) and the tab editor
// (TabsEditor.tsx) build on them.

// Section. The iOS convention is to render an uppercase label in
// ``text-label-tertiary`` above a rounded card of rows (the first
// section has none). The label has
// generous left/right padding (``px-4``) and a small bottom margin.
// The card is a single rounded ``bg-bg-surface`` block; rows inside
// are separated by ``border-hairline`` so dividers extend to the
// edges of the card (not full bleed). ``footnote`` sits below the
// card in ``text-ios-caption text-label-secondary`` — useful for
// explanatory copy (e.g. "filtering: reuters, hackernews").
//
// ``action`` (e.g. a "Clear" button) renders on the right side of
// the label row, like the "Edit" button next to the "Reminders"
// header in iOS Settings.
export function GroupedSection({
  label,
  footnote,
  action,
  children,
}: {
  label?: string
  footnote?: string
  action?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="mt-6 first:mt-2">
      {(label || action) && (
        <div className="flex items-end justify-between px-4 mb-2">
          {label && (
            <h3 className="text-ios-caption uppercase tracking-wide text-label-tertiary">
              {label}
            </h3>
          )}
          {action}
        </div>
      )}
      {/* The rows go inside a single rounded card so the iOS grouped-
          list visual works: rounded card on the page, hairline
          dividers between rows, no outside border. ``overflow-hidden``
          keeps the first/last row from poking past the card corners. */}
      <div className="mx-4 rounded-ios bg-bg-surface overflow-hidden">
        {children}
      </div>
      {footnote && (
        <p className="px-4 mt-2 text-ios-caption text-label-secondary">
          {footnote}
        </p>
      )}
    </div>
  )
}

// Single iOS-style row. ``title`` is the primary text, ``subtitle``
// is the secondary line beneath it (font-300, ``label-secondary``).
// Renders as a 44px-tall button when ``onClick`` is set — the entire
// row is the tap target. When used as a static display (no
// ``onClick``) it falls back to a non-button ``<div>``.
//
// ``tone`` recolors the chevron-area / title when there's a state to
// surface (success / warning / destructive) — iOS uses these for
// health-check rows in Settings → Battery, for example.
//
// ``trailing`` puts controls at the row's end (the tab editor's arrows
// and Show switch). A row with it is always a ``<div>``, so those
// controls are never nested inside a button; ``onClick`` is ignored.
export function GroupedRow({
  title,
  subtitle,
  onClick,
  showChevron,
  tone,
  trailing,
}: {
  title: React.ReactNode
  subtitle?: React.ReactNode
  onClick?: () => void
  showChevron?: boolean
  tone?: 'success' | 'warning' | 'destructive'
  trailing?: React.ReactNode
}) {
  const toneClass =
    tone === 'success'
      ? 'text-emerald-400'
      : tone === 'warning'
      ? 'text-amber-400'
      : tone === 'destructive'
      ? 'text-red-400'
      : 'text-label-primary'
  const content = (
    <>
      <div className="flex-1 min-w-0">
        {/* Beside controls the title wraps rather than truncates: the
            controls leave it little room on a narrow phone. */}
        <div className={`text-ios-body ${toneClass} ${trailing != null ? 'break-words' : 'truncate'}`}>{title}</div>
        {subtitle && (
          <div className="text-ios-caption text-label-secondary truncate">
            {subtitle}
          </div>
        )}
      </div>
      {showChevron && (
        <ChevronRight className="shrink-0 w-4 h-4 text-label-tertiary" />
      )}
    </>
  )
  const baseClass =
    'flex items-center gap-3 px-4 min-h-[44px] border-b border-hairline last:border-b-0'
  if (trailing != null) {
    // Tighter padding and gap than a plain row: the controls take most
    // of a 320px phone's row, and the title keeps room for a whole word.
    return (
      <div className="flex items-center gap-2 pl-4 pr-1 min-h-[44px] border-b border-hairline last:border-b-0">
        {content}
        <div className="shrink-0 flex items-center">{trailing}</div>
      </div>
    )
  }
  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`${baseClass} w-full text-left active:bg-bg-elevated`}
      >
        {content}
      </button>
    )
  }
  return <div className={baseClass}>{content}</div>
}

// iOS chevron — used for navigation rows. Right-pointing, single 1.5
// stroke. Inline rather than from an icon library; the Menu and the
// tab editor use it.
export function ChevronRight({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <polyline points="9 6 15 12 9 18" />
    </svg>
  )
}



