import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Base path is configurable for subpath deployment behind nginx
// (e.g. /drhiro/). See infra/Caddyfile for the reverse proxy.
export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE_PATH || '/',
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
