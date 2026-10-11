import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Reachable from outside the dev container (Docker publishes 5173).
    host: true,
    // Windows/macOS bind mounts don't reliably deliver inotify events into
    // a Linux container, so Vite's native file watcher can miss edits and
    // HMR goes stale. Polling is the standard, reliable fix for
    // containerised dev on those hosts — a small CPU cost for dependable
    // hot reload. (No effect on production builds.)
    watch: { usePolling: true, interval: 120 },
  },
})
