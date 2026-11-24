import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import fs from 'node:fs'
import {
  precacheList,
  renderServiceWorker,
  shellFiles,
  swVersion,
  type BundleLike,
} from './src/mobile/pwa/swBuild'

/**
 * Vite config for the MOBILE surface (`/m/`), built into `dist/m/`.
 *
 * ── WHY A SECOND CONFIG AND NOT A SECOND ROLLUP INPUT ─────────────────────
 * The obvious move is one build with two entries in
 * `build.rollupOptions.input`. It does not survive contact with the
 * requirement that the workstation bundle stay unchanged. Rollup hoists the
 * modules two entries share — React, `lib/api`, `assemblyModel`, `CitedProse`
 * — into common chunks, which re-partitions the workstation's chunk graph and
 * re-hashes its files.
 *
 * MEASURED, not assumed. The two-entry build was run against this tree:
 * `dist/index.html` stopped pointing at `assets/index-<hash>.js` and came back
 * pointing at a split `assets/globals-<hash>.js` + `assets/main-<hash>.js`,
 * and only 16 of the workstation's 112 asset filenames survived unchanged. A
 * 96-file rename of a production bundle, to host a surface the workstation
 * does not even link to, is not a trade worth making.
 *
 * Two independent builds keep the guarantee absolute: `vite build` emits the
 * workstation exactly as it did before this branch existed — byte-identical,
 * provable with a checksum diff — and this config emits the phone surface
 * beside it. `npm run build` runs both, so one command still produces the
 * whole tree and the Docker build job needs no change (it copies all of
 * `dist/`, `dist/m` included).
 *
 * The cost is honest and small: the two bundles do not share chunks, so a
 * visitor who opened both would download React twice. Nobody does — these are
 * two different devices — and the alternative was re-hashing a production
 * bundle to save a download that never happens.
 */

/**
 * Rename the emitted `m.html` to `index.html`.
 *
 * Vite names an HTML output after its input path, so `m.html` as the entry
 * would land at `dist/m/m.html` and Caddy's `try_files` would have to know the
 * odd name. Renaming in `generateBundle` keeps the SPA fallback boring:
 * `/m/` serves `dist/m/index.html`, exactly like `/` serves `dist/index.html`.
 */
function htmlAsIndex(): Plugin {
  return {
    name: 'legba-mobile-html-as-index',
    enforce: 'post',
    generateBundle(_options, bundle) {
      const entry = bundle['m.html']
      if (entry) {
        delete bundle['m.html']
        entry.fileName = 'index.html'
        bundle['index.html'] = entry
      }
    },
  }
}

/**
 * P-1 — emit the phone app's service worker as `dist/m/sw.js` (served at
 * `/m/sw.js`, so its scope is `/m/` and never the desktop at `/`).
 *
 * The worker's source is `src/mobile/pwa/sw.template.js`; this fills in the
 * shell list (the page, the entry's static import graph, the public files —
 * `src/mobile/pwa/swBuild.ts` says why lazy chunks are left out) and a version
 * hashed over all of it, so any change to the shell is a new worker. Runs
 * after `htmlAsIndex`, so the page is already `index.html`.
 */
function phoneServiceWorker(publicDir: string): Plugin {
  const listPublic = (dir: string, rel = ''): string[] =>
    fs.readdirSync(path.join(dir, rel), { withFileTypes: true }).flatMap((e) =>
      e.isDirectory() ? listPublic(dir, path.join(rel, e.name)) : [path.join(rel, e.name).split(path.sep).join('/')],
    )
  return {
    name: 'legba-mobile-service-worker',
    enforce: 'post',
    apply: 'build',
    generateBundle(_options, bundle) {
      const template = fs.readFileSync(path.resolve(__dirname, 'src/mobile/pwa/sw.template.js'), 'utf8')
      const publicFiles = listPublic(publicDir)
      const precache = precacheList('/m/', shellFiles(bundle as unknown as BundleLike), publicFiles)
      const contents = precache.map((p) => {
        const rel = p.slice('/m/'.length)
        const item = (bundle as unknown as BundleLike)[rel]
        if (item) return item.type === 'chunk' ? String((item as unknown as { code: string }).code) : (item.source ?? '')
        return fs.readFileSync(path.join(publicDir, rel))
      })
      const source = renderServiceWorker(template, swVersion(template, precache, contents), precache)
      this.emitFile({ type: 'asset', fileName: 'sw.js', source })
    },
  }
}

const PUBLIC_M = path.resolve(__dirname, 'public-m')

export default defineConfig({
  plugins: [react(), htmlAsIndex(), phoneServiceWorker(PUBLIC_M)],
  // Assets are requested from `/m/assets/...` — the surface is served under a
  // sub-path, not the origin root.
  base: '/m/',
  // `public/` holds `world.geojson` — 820 kB of basemap the workstation's map
  // panels use and this surface does not (there is no map here; see the
  // build note). Copying it would have made the phone bundle mostly a file
  // the phone never opens. The workstation still ships it from its own build.
  // M-0a — so the phone has its OWN public dir, `public-m/`, holding only what
  // the phone serves at `/m/…`: today the icon (`/m/favicon.svg`; m.html's
  // `/favicon.svg` is rewritten under the base). Until 2026-11-14 this was
  // `false`, nothing was emitted, and the icon request fell through to the
  // SPA's index.html. The icon is the same file as `public/favicon.svg`
  // (`src/mobile/publicAssets.test.ts` holds them byte-identical).
  // P-1: it also holds the app manifest and the icons (`public-m/icons/`,
  // drawn by `tools/make-phone-icons.mjs` from `favicon.svg`).
  publicDir: path.resolve(__dirname, 'public-m'),
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: 'dist/m',
    // Scoped to `dist/m`, so this can never clear the workstation's output.
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: path.resolve(__dirname, 'm.html'),
    },
  },
})
