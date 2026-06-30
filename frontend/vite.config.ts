import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In docker-compose the backend service is reachable as "backend".
// In local dev (running `vite` outside docker) it's on localhost:8000.
// vite.config picks the right one based on whether we're inside the
// container by checking VITE_BACKEND_URL (set in docker-compose env_file).
const backend = process.env.VITE_BACKEND_URL || 'http://localhost:8000'

// /api and /assets are routed to the backend in BOTH dev and preview.
// The dev server uses ``server.proxy``; ``vite preview`` uses
// ``preview.proxy`` (same shape, separate code path). They share the
// same target/changeOrigin because the backend hostname is identical
// in both contexts — dev runs the frontend container with
// VITE_BACKEND_URL=http://backend:8000, and the prod image's
// `vite preview` is built into the same image so it inherits the
// same env. Without the preview proxy, a /api request after the
// build would 404 because ``vite preview`` only serves the static
// ``dist/`` output and knows nothing about the backend.
const proxy = {
  '/api': {
    target: backend,
    changeOrigin: true,
  },
  // Cached favicons/thumbnails served by the backend at /assets.
  // Required because the backend has no published port in compose;
  // the browser must reach it through Vite like /api does.
  '/assets': {
    target: backend,
    changeOrigin: true,
  },
}

export default defineConfig({
  plugins: [react()],
  build: {
    // Build output goes to ``/static/`` instead of Vite's default
    // ``/assets/`` so the path doesn't collide with the backend's
    // dynamic ``/assets/`` route (favicons + thumbnails served
    // from ``app/assets/``). With the default ``/assets/`` the
    // production build would either 404 the JS/CSS (vite preview
    // would proxy every /assets/* to the backend) or, if we
    // removed the /assets proxy, the favicon / thumbnail <img>
    // tags would 404. Using ``/static/`` for the bundle gives
    // ``vite preview`` a clean namespace for the static files
    // and leaves ``/assets/`` exclusively for the backend proxy.
    assetsDir: 'static',
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy,
  },
  preview: {
    host: '0.0.0.0',
    port: 5173,
    proxy,
  },
})