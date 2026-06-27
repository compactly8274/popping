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
      },
    },
  },
  plugins: [],
}