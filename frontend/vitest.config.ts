/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    // jsdom gives us window.localStorage + matchMedia for the
    // preferences/seed tests that touch them. The provider itself
    // isn't tested here yet (no React Testing Library dep) — these
    // tests target the pure helpers.
    environment: 'jsdom',
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    globals: false,
  },
})
