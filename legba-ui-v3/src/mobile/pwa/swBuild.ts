/**
 * P-1 — the build half of the phone app's service worker (used by
 * `vite.config.mobile.ts`, never by the browser).
 *
 * The worker stores the phone app's SHELL and nothing else, so the list is
 * computed from the bundle, not written by hand:
 *
 *   - the page itself (`/m/index.html`);
 *   - the ENTRY chunk and every chunk it imports statically (what the browser
 *     must load before the first screen draws), with their CSS and the assets
 *     that CSS names (fonts);
 *   - the phone's public files: the icons, the manifest, the favicon.
 *
 * Lazily imported chunks are NOT listed: the `/m/` build also emits copies of
 * desktop chunks (the map, the registry, the graph) the phone never requests,
 * and storing them would put megabytes of unused code on every phone. A lazy
 * chunk is fetched from the network when it is opened, like any other file.
 *
 * Nothing under /api/ can enter the list (`precacheList` throws), and the
 * version is a hash over everything the worker stores plus the worker's own
 * source — any change to the shell is a byte-different `sw.js`, which is
 * what makes the browser install the new version.
 */
import { createHash } from 'node:crypto'

export interface BundleItem {
  type: 'chunk' | 'asset'
  fileName: string
  isEntry?: boolean
  imports?: string[]
  source?: string | Uint8Array
  viteMetadata?: { importedCss?: Set<string>; importedAssets?: Set<string> }
}
export type BundleLike = Record<string, BundleItem>

/** The bundle files the first screen needs: the entry's static import graph, its CSS and assets. */
export function shellFiles(bundle: BundleLike): string[] {
  const out = new Set<string>()
  const seen = new Set<string>()
  const visit = (name: string) => {
    if (seen.has(name)) return
    seen.add(name)
    const item = bundle[name]
    if (!item || item.type !== 'chunk') return
    out.add(item.fileName)
    for (const css of item.viteMetadata?.importedCss ?? []) out.add(css)
    for (const asset of item.viteMetadata?.importedAssets ?? []) out.add(asset)
    for (const dep of item.imports ?? []) visit(dep)
  }
  const entries = Object.values(bundle).filter((b) => b.type === 'chunk' && b.isEntry)
  if (entries.length !== 1) throw new Error(`phone sw: expected one entry chunk, found ${entries.length}`)
  visit(entries[0].fileName)
  return [...out].sort()
}

const NEVER = /^\/(api|ws|tiles)(\/|$)/

/** Absolute paths under `base`, the page first; throws on anything outside the shell. */
export function precacheList(base: string, bundleFiles: string[], publicFiles: string[]): string[] {
  if (!base.startsWith('/') || !base.endsWith('/')) throw new Error(`phone sw: base must be /…/, got ${base}`)
  const all = new Set<string>([base + 'index.html'])
  for (const f of [...bundleFiles, ...publicFiles]) {
    const rel = f.replace(/^\/+/, '')
    if (rel === 'sw.js' || rel.endsWith('.map')) continue
    all.add(base + rel)
  }
  for (const p of all) {
    if (NEVER.test(p) || !p.startsWith(base) || p.includes('?') || p.includes('..')) {
      throw new Error(`phone sw: ${p} cannot be stored by the worker`)
    }
  }
  const [page, ...rest] = [...all]
  return [page, ...rest.sort()]
}

/** A short content hash: the worker's source, the list, and the bytes of what it stores. */
export function swVersion(template: string, precache: string[], contents: (string | Uint8Array)[]): string {
  const h = createHash('sha256')
  h.update(template)
  h.update(JSON.stringify(precache))
  for (const c of contents) h.update(c)
  return h.digest('hex').slice(0, 16)
}

/** The worker's source with its two placeholders filled. */
export function renderServiceWorker(template: string, version: string, precache: string[]): string {
  if (!template.includes("'__LEGBA_SW_VERSION__'") || !template.includes('__LEGBA_SW_PRECACHE__')) {
    throw new Error('phone sw: the template lost a placeholder')
  }
  if (!/^[0-9a-f]{8,64}$/.test(version)) throw new Error(`phone sw: bad version ${version}`)
  return template
    .replace("'__LEGBA_SW_VERSION__'", JSON.stringify(version))
    .replace('__LEGBA_SW_PRECACHE__', JSON.stringify(precache))
}
