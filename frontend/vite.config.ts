import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In docker-compose the backend service is reachable as "backend".
// In local dev (running `vite` outside docker) it's on localhost:8000.
// vite.config picks the right one based on whether we're inside the
// container by checking VITE_BACKEND_URL (set in docker-compose env_file).
const backend = process.env.VITE_BACKEND_URL || 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: backend,
        changeOrigin: true,
      },
    },
  },
})