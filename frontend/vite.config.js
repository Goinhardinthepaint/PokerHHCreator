import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    // In dev the API runs on :8000; proxy /api so the frontend can use
    // same-origin relative URLs (which also work in the single-server prod build).
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
})
