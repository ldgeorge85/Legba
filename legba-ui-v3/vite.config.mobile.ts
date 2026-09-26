import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

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

export default defineConfig({
  plugins: [react(), htmlAsIndex()],
  // Assets are requested from `/m/assets/...` — the surface is served under a
  // sub-path, not the origin root.
  base: '/m/',
  // `public/` holds `world.geojson` — 820 kB of basemap the workstation's map
  // panels use and this surface does not (there is no map here; see the
  // build note). Copying it would have made the phone bundle mostly a file
  // the phone never opens. The workstation still ships it from its own build.
  publicDir: false,
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
