/** @type {import('tailwindcss').Config} */
// Apollo for iOS aesthetic: true-black surfaces, cool-gray neutrals,
// hairline borders, generous 44px tap targets. The palette deliberately
// avoids slate-blue tints (the old config's bg-app was #020617 — a
// dark blue-black that read as "Tailwind demo"); iOS dark mode uses
// pure black with neutral grays stacked on top.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Score bands: hot -> warm -> cold. Slightly more saturated
        // than before so the gradient badges pop against the new
        // true-black background.
        'score-hot':   '#ef4444',  // red-500
        'score-warm':  '#f59e0b',  // amber-500
        'score-cool':  '#3b82f6',  // blue-500
        'score-cold':  '#525252',  // neutral-600
        // Semantic surfaces. Stacked true-black → neutral grays
        // mirrors iOS dark mode (UIColor.systemBackground,
        // .secondarySystemBackground, .tertiarySystemBackground).
        'bg-app':      '#000000',  // pure black — root shell
        'bg-surface':  '#0a0a0a',  // near-black — grouped list rows
        'bg-elevated': '#1c1c1e',  // iOS tertiary — cards, popovers
        'bg-grouped':  '#1c1c1e',  // alias used inside Drawer sections
        // Accent — iOS systemBlue. Used for CTAs, focus rings,
        // toggles, and the gradient endpoint in the logo SVG.
        'accent':      '#0a84ff',  // iOS systemBlue (dark)
        // Tint behind accent elements (chips, badges). Matches the
        // new accent hue so chips don't pick up blue against a
        // brighter background.
        'accent-soft': 'rgba(10, 132, 255, 0.15)',
        // Hairline separator color. The whole UI leans on this —
        // dividers between cards, columns, grouped-list sections,
        // and the bottom of the nav bar.
        'hairline':    'rgba(255, 255, 255, 0.08)',
        // Cool-gray label colors. Replace the slate-300/400/500 mix
        // with neutral grays so text reads as "Apple" rather than
        // "Tailwind slate".
        // Cool-gray label colors. Replace the slate-300/400/500 mix
        // with neutral grays so text reads as "Apple" rather than
        // "Tailwind slate".
        //
        // ``label-tertiary`` is the iOS .tertiaryLabel equivalent
        // (used for grouped-list section headers, timestamps, and
        // other secondary metadata). Apple's 0.3 fails WCAG AA on a
        // black background for 13pt text — bump to 0.45 for
        // readability while keeping the "muted" hierarchy below
        // secondary.
        'label-primary':   '#ffffff',
        'label-secondary': 'rgba(235, 235, 245, 0.6)',  // iOS .secondaryLabel
        'label-tertiary':  'rgba(235, 235, 245, 0.45)',  // iOS .tertiaryLabel + WCAG
      },
      fontFamily: {
        // SF Pro on Apple platforms, fall back to system-ui so
        // Android/Linux still render with the local UI font instead
        // of a generic sans-serif.
        sans: [
          '-apple-system', 'BlinkMacSystemFont', '"SF Pro Display"',
          '"SF Pro Text"', '"Helvetica Neue"', 'system-ui', 'sans-serif',
        ],
      },
      fontSize: {
        // Apple large title. Slightly tighter than Tailwind's
        // default leading — iOS titles hug their descenders.
        'ios-large-title': ['34px', { lineHeight: '41px', letterSpacing: '0.37px', fontWeight: '700' }],
        // 17pt body — matches iOS body text.
        'ios-body':        ['17px', { lineHeight: '22px', letterSpacing: '-0.41px' }],
        // 13pt caption used for grouped-list section headers.
        'ios-caption':     ['13px', { lineHeight: '18px', letterSpacing: '-0.08px' }],
      },
      borderRadius: {
        // iOS rounded rect for grouped list rows. 10px on the corners
        // matches .continuous corner radius on standard list rows.
        'ios': '10px',
        // 14px — used for cards / sheets / popovers.
        'ios-lg': '14px',
      },
      boxShadow: {
        // Card hover elevation. Subtle so cards don't jump when
        // hovered next to non-hovered neighbours.
        'glow-sm': '0 1px 2px rgba(0, 0, 0, 0.3)',
        'glow-md': '0 4px 12px rgba(0, 0, 0, 0.45)',
        // Focus / accent glow. Matches the iOS systemBlue tint.
        'glow-accent': '0 0 0 1px rgba(10, 132, 255, 0.4), 0 4px 12px rgba(10, 132, 255, 0.18)',
      },
      keyframes: {
        // Subtle fade-up for new filter chips + drawer-open items.
        // 2px translate keeps it from feeling like a slide-in.
        fadeIn: {
          '0%':   { opacity: '0', transform: 'translateY(2px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        // Sheet slide — used by the Drawer on mobile. Slides up
        // from the bottom edge with the iOS spring-ish feel (we use
        // a plain ease-out instead of a spring library).
        sheetUp: {
          '0%':   { transform: 'translateY(100%)' },
          '100%': { transform: 'translateY(0)' },
        },
        // Reserved for future loading skeletons — defined here so the
        // animation utility is purged-in instead of having to wire
        // it up when we add the first skeleton surface.
        shimmer: {
          '0%':   { backgroundPosition: '-100% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
      },
      animation: {
        // 180ms feels "snappy" — short enough that the chip appears
        // to settle instantly but long enough to draw the eye when
        // several chips arrive in quick succession.
        'fade-in': 'fadeIn 180ms ease-out',
        // Sheet open. ~320ms matches iOS modal presentation.
        'sheet-up': 'sheetUp 320ms cubic-bezier(0.32, 0.72, 0, 1)',
      },
    },
  },
  plugins: [],
}