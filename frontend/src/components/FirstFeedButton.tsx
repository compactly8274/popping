// "Add your first feed": shown while every feed is a built-in one
// (``isBuiltIn``, lib/feedRules.ts). An empty Saved or For You tab shows
// it under "No stories yet"; it opens Settings → Feeds. One component so
// every place that offers it looks and behaves the same.

export function FirstFeedButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      data-first-feed
      onClick={onClick}
      className="min-h-[44px] px-4 rounded-ios bg-accent text-white text-ios-body font-medium active:opacity-80"
    >
      Add your first feed
    </button>
  )
}
