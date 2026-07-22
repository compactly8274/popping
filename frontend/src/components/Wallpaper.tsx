// Ambient animated backdrop for the whole dashboard. Cards and columns
// sit on fully opaque surfaces (bg-bg-surface / bg-bg-elevated), so this
// never touches readability — it only shows through the gaps between
// columns/cards and the translucent (backdrop-blur) header. Three large
// blurred color blobs drift slowly on independent paths/durations so
// they never look lock-stepped, giving a slow macOS-style shifting-
// wallpaper feel without any JS animation loop competing with scroll
// or interaction work — it's pure CSS transform, GPU-composited.
//
// z-index is very negative (see .wallpaper-blob-layer in styles.css)
// so it paints behind literally everything, including the app root's
// own inline black background (see App.tsx's flash-prevention comment)
// and any non-positioned content, per CSS stacking-order rules.
export function Wallpaper() {
  return (
    <div aria-hidden="true" className="wallpaper-blob-layer">
      <div className="wallpaper-blob wallpaper-blob-a" />
      <div className="wallpaper-blob wallpaper-blob-b" />
      <div className="wallpaper-blob wallpaper-blob-c" />
    </div>
  )
}
