/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Score bands: hot -> warm -> cold
        'score-hot':   '#dc2626',  // red-600
        'score-warm':  '#f59e0b',  // amber-500
        'score-cool':  '#3b82f6',  // blue-500
        'score-cold':  '#475569',  // slate-600
        // Semantic surfaces. The three tiers let a card visually sit
        // above the page background without manually picking a slate
        // shade every time. New layers (modal, dropdown, toast) get a
        // -elevated step above whatever sits on -surface.
        'bg-app':      '#020617',  // slate-950 — page shell
        'bg-surface':  '#0f172a',  // slate-900 — panels (Drawer, BriefCard)
        'bg-elevated': '#1e293b',  // slate-800 — cards, popovers
        // Accent. Used for primary CTAs, focus rings, filter chips,
        // and the gradient endpoint in the logo SVG.
        'accent':      '#3b82f6',  // blue-500
        // Tint behind accent elements (chips, badges). Lower contrast
        // than the accent itself so the underlying surface stays
        // legible.
        'accent-soft': 'rgba(59, 130, 246, 0.12)',
      },
      boxShadow: {
        // Card hover elevation. Subtle so cards don't jump when
        // hovered next to non-hovered neighbours.
        'glow-sm': '0 1px 2px rgba(0, 0, 0, 0.3)',
        'glow-md': '0 4px 12px rgba(0, 0, 0, 0.45)',
        // Focus / accent glow. The blue tint sells the press
        // affordance on primary CTAs without an outline jump.
        'glow-accent': '0 0 0 1px rgba(59, 130, 246, 0.25), 0 4px 12px rgba(59, 130, 246, 0.12)',
      },
      keyframes: {
        // Subtle fade-up for new filter chips + drawer-open items.
        // 2px translate keeps it from feeling like a slide-in.
        fadeIn: {
          '0%':   { opacity: '0', transform: 'translateY(2px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
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
      },
    },
  },
  plugins: [],
}