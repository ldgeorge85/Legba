import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  // vitest@2 ships its own copy of vite type defs which drift from the
  // top-level vite@6 install — the React plugin satisfies vite@6's
  // PluginOption shape but not vitest's bundled vite@5 one. Cast to
  // `any` until vitest@3 lands alongside vite@6.
  plugins: [react() as any],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    css: false,
  },
})
