import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'

// Two UIs, one build: index.html is the redesigned app (src/), classic.html is
// the original UI (src-classic/), which shares the betting engine, bounty/squid
// logic and API layer from src/. server.py picks the page per the `ui` cookie;
// both pages' hashed assets land in the same dist/assets folder.
// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        main: resolve(import.meta.dirname, 'index.html'),
        classic: resolve(import.meta.dirname, 'classic.html'),
      },
    },
  },
  server: {
    port: 3000,
    // In dev the API runs on :8000; proxy /api so the frontend can use
    // same-origin relative URLs (which also work in the single-server prod build).
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
})
