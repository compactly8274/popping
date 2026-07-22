// First-load placeholder cards. Shown only while the very first
// ``refresh()`` is in flight and no entries have landed yet — once
// any entries exist, later refreshes update cards in place and never
// show this (a skeleton flashing over live content would read as a
// regression, not "loading"). Mirrors Card's own shape (stripe,
// title lines, meta row, footer bar) closely enough that the layout
// doesn't jump when real cards replace these.

// One shimmering placeholder block. ``bg-gradient-to-r`` + the
// ``animate-shimmer`` utility (see tailwind.config.js) sweeps a
// lighter band across via ``background-position`` — pure CSS, no
// per-frame JS.
function ShimmerBlock({ className }: { className: string }) {
  return (
    <div
      aria-hidden="true"
      className={`rounded-ios bg-gradient-to-r from-bg-elevated via-white/10 to-bg-elevated bg-[length:200%_100%] animate-shimmer ${className}`}
    />
  )
}

function SkeletonCard() {
  return (
    <div className="relative rounded-ios-lg bg-bg-surface border border-hairline p-4 pl-5 overflow-hidden">
      <div aria-hidden="true" className="absolute left-0 top-0 bottom-0 w-[3px] rounded-l-ios-lg bg-neutral-700" />
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex-1 min-w-0 space-y-2 py-0.5">
          <ShimmerBlock className="h-4 w-full" />
          <ShimmerBlock className="h-4 w-4/5" />
        </div>
        <div className="shrink-0 flex flex-col items-end gap-1.5">
          <ShimmerBlock className="h-5 w-9" />
          <ShimmerBlock className="w-28 sm:w-40 aspect-video" />
        </div>
      </div>
      <ShimmerBlock className="h-3 w-24" />
      <div className="mt-3 pt-2.5 border-t border-hairline/70 flex items-center">
        <ShimmerBlock className="h-9 w-20 rounded-full" />
        <div className="ml-auto flex items-center gap-1">
          <ShimmerBlock className="h-7 w-7 rounded-full" />
          <ShimmerBlock className="h-7 w-7 rounded-full" />
        </div>
      </div>
    </div>
  )
}

// ``count`` defaults to 6 — enough to fill a desktop grid row or two
// without over-committing to a number that has to match whatever the
// real response eventually contains.
export function SkeletonGrid({ count = 6 }: { count?: number }) {
  return (
    <div
      role="status"
      aria-label="loading entries"
      className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4 p-4"
    >
      {Array.from({ length: count }, (_, i) => (
        <SkeletonCard key={i} />
      ))}
    </div>
  )
}
