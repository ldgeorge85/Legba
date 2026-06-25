import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

/**
 * Vite config for legba-ui-v3 (L-204).
 *
 * The dev server proxies `/api/v1` + `/ws` to the legba-registry process so
 * that `npm run dev` works against a locally-running registry without CORS.
 * Default base is `http://localhost:8501` (the legba-registry default per
 * pyproject.toml console script). Override with `VITE_API_BASE=...`.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5174,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: process.env.VITE_API_BASE || 'http://localhost:8501',
        changeOrigin: true,
      },
      '/ws': {
        target: process.env.VITE_API_BASE || 'http://localhost:8501',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
