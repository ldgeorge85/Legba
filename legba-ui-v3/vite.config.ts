import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

/**
 * M-2 — a phone never downloads the desktop's code to be sent away.
 *
 * `index.html` opens with an inline script that sends a phone to `/m/`
 * (`lib/phoneSurface.ts`'s rule). But a `<script type="module">` and its
 * `<link rel="modulepreload">` / stylesheet in the page are fetched by the
 * browser's preload scanner whatever an earlier script decides. So in the
 * BUILT page this moves the entry, its preloads and its stylesheet into a
 * loader at the end of the body that adds them only when the page was not
 * sent away (`window.__legbaToPhone`). The JS chunks are untouched — only
 * `dist/index.html` changes. The dev server keeps the plain tags.
 */
export function deferEntryForPhones(): Plugin {
  return {
    name: 'legba-defer-entry-for-phones',
    apply: 'build',
    enforce: 'post',
    transformIndexHtml: {
      order: 'post',
      handler(html) {
        const scripts: string[] = []
        const preloads: string[] = []
        const styles: string[] = []
        let out = html.replace(/\s*<script type="module" crossorigin src="([^"]+)"><\/script>/g, (_m, src: string) => {
          scripts.push(src)
          return ''
        })
        out = out.replace(/\s*<link rel="modulepreload" crossorigin href="([^"]+)">/g, (_m, href: string) => {
          preloads.push(href)
          return ''
        })
        out = out.replace(/\s*<link rel="stylesheet" crossorigin href="([^"]+)">/g, (_m, href: string) => {
          styles.push(href)
          return ''
        })
        if (scripts.length === 0) return html
        const loader =
          `<script id="legba-desktop-loader">(function(){if(window.__legbaToPhone)return;var d=document,h=d.head;` +
          `function a(t,p){var e=d.createElement(t);e.crossOrigin='';for(var k in p)e[k]=p[k];h.appendChild(e);}` +
          `${JSON.stringify(styles)}.forEach(function(u){a('link',{rel:'stylesheet',href:u});});` +
          `${JSON.stringify(preloads)}.forEach(function(u){a('link',{rel:'modulepreload',href:u});});` +
          `${JSON.stringify(scripts)}.forEach(function(u){a('script',{type:'module',src:u});});})();</script>`
        return out.replace('</body>', `  ${loader}\n  </body>`)
      },
    },
  }
}

/**
 * Vite config for legba-ui-v3 (L-204).
 *
 * The dev server proxies `/api/v1` + `/ws` to the legba-registry process so
 * that `npm run dev` works against a locally-running registry without CORS.
 * Default base is `http://localhost:8501` (the legba-registry default per
 * pyproject.toml console script). Override with `VITE_API_BASE=...`.
 */
export default defineConfig({
  plugins: [react(), deferEntryForPhones()],
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
