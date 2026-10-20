import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Same-origin contract in dev: the console calls relative /v1/... and
    // /healthz paths, proxied to the compose API. Production serves the built
    // assets behind the same origin, so no CORS and no API base URL config.
    proxy: {
      '/v1': 'http://localhost:8000',
      '/healthz': 'http://localhost:8000',
    },
  },
})
