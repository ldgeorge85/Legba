#!/usr/bin/env node
/**
 * lint-phone-imports — what the PHONE may not load (M-2; plan §3.4).
 *
 * Walks every STATIC import reachable from the phone's code (`src/mobile/**`,
 * entry `src/mobile/main.tsx` and every other file under `src/mobile/`),
 * following `@/` and relative paths into the shared tree, and fails when one
 * reaches the desktop's frame or a library the phone must not carry:
 *
 *   dockview*, cytoscape*, react-cytoscape*, @xyflow/*, elkjs, js-yaml,
 *   html-to-image, recharts (the time band's charts)
 *   src/App.tsx, src/lib/surfaceDock.ts, src/ui/dockviewTheme*,
 *   src/components/DockviewPanelApiContext.tsx, TileWebGLOverlay, TimeScrubber
 *   maplibre-gl, pmtiles — EXCEPT behind a dynamic `import()` written in the
 *   Map screen (`src/mobile/screens/map/`): the globe on demand.
 *
 * `import type` / `export type` are erased by the compiler and are not
 * followed. A dynamic `import()` is not a static edge: the walk does not
 * follow it — but a dynamic import WRITTEN under `src/mobile/` outside the
 * Map screen is walked as if static (with the map libraries forbidden), so
 * the phone cannot reach the map library through a lazy screen of its own.
 * Lazy chunks of shared modules (the registry's `lazy()` kinds) are never
 * requested by the phone's screens and are reported by the bundle check, not
 * here.
 *
 *   node tools/lint-phone-imports.mjs          # exit 1 on a forbidden import, listing each chain
 *   node tools/lint-phone-imports.mjs --json   # machine-readable
 *
 * Dependency-free (runs in the bare node image), like lint-tokens.
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(fileURLToPath(new URL('.', import.meta.url)), '..')
const SRC = join(ROOT, 'src')
const PHONE = join(SRC, 'mobile')
const MAP_SCREEN = join(PHONE, 'screens', 'map')

/** Bare packages the phone never loads. */
const FORBIDDEN_PACKAGES = [/^dockview/, /^cytoscape/, /^react-cytoscape/, /^@xyflow\//, /^elkjs/, /^js-yaml/, /^html-to-image/, /^recharts/]
/** Allowed only behind a dynamic import in the Map screen. */
const MAP_PACKAGES = [/^maplibre-gl/, /^pmtiles/]
/** Shared files the phone never loads (relative to src/). */
const FORBIDDEN_FILES = [
  /^App\.tsx$/,
  /^lib\/surfaceDock\.ts$/,
  /^ui\/dockviewTheme/,
  /^components\/DockviewPanelApiContext\.tsx$/,
  /TileWebGLOverlay/,
  /TimeScrubber/,
]

const EXTS = ['.ts', '.tsx', '.js', '.jsx', '.mjs', '.json']

function resolveFile(base) {
  if (existsSync(base) && statSync(base).isFile()) return base
  for (const e of EXTS) if (existsSync(base + e)) return base + e
  for (const e of EXTS) {
    const idx = join(base, 'index' + e)
    if (existsSync(idx)) return idx
  }
  return null
}

/** Strip comments (block and line) so a commented-out import is not an edge. */
function stripComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`\\])\/\/[^\n]*/g, '$1')
}

/** The module specifiers a file imports: static (value) and dynamic. */
export function importsOf(src) {
  const code = stripComments(src)
  const statics = []
  const dynamics = []
  // import ... from 'x' / export ... from 'x' (multi-line clauses included)
  const re = /\b(import|export)\s+(type\s+)?([^'";]*?)\s*from\s*['"]([^'"]+)['"]/g
  for (let m; (m = re.exec(code)); ) {
    if (m[2]) continue // import type / export type
    const clause = m[3].trim()
    // `import { type A, type B } from` — every binding type-only
    const braces = /^\{([\s\S]*)\}$/.exec(clause)
    if (braces) {
      const names = braces[1].split(',').map((s) => s.trim()).filter(Boolean)
      if (names.length > 0 && names.every((n) => n.startsWith('type '))) continue
    }
    statics.push(m[4])
  }
  // side-effect imports: import 'x'
  const se = /\bimport\s*['"]([^'"]+)['"]/g
  for (let m; (m = se.exec(code)); ) statics.push(m[1])
  const dy = /\bimport\(\s*['"]([^'"]+)['"]\s*\)/g
  for (let m; (m = dy.exec(code)); ) dynamics.push(m[1])
  return { statics, dynamics }
}

function resolveSpec(spec, fromFile) {
  if (spec.startsWith('@/')) return { file: resolveFile(join(SRC, spec.slice(2))) }
  if (spec.startsWith('.')) return { file: resolveFile(resolve(dirname(fromFile), spec)) }
  if (spec.endsWith('.css') || spec.includes('?')) return { asset: spec }
  return { pkg: spec }
}

function walkFiles(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) {
      if (name === '__fixtures__') continue
      walkFiles(p, out)
    } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name)) out.push(p)
  }
  return out
}

/**
 * Walk from `starts`; return each forbidden hit with the chain that reached it.
 * `mapAllowed`: the map packages are allowed in this walk (the Map screen's own lazy chunk).
 */
function check(starts, { mapAllowed }) {
  const seen = new Map() // file -> parent file
  const queue = []
  for (const s of starts) {
    if (!seen.has(s)) {
      seen.set(s, null)
      queue.push(s)
    }
  }
  const hits = []
  const chainOf = (file) => {
    const chain = []
    for (let f = file; f; f = seen.get(f)) chain.unshift(relative(ROOT, f))
    return chain
  }
  while (queue.length) {
    const file = queue.shift()
    if (file.endsWith('.json')) continue
    const src = readFileSync(file, 'utf8')
    const { statics } = importsOf(src)
    for (const spec of statics) {
      const r = resolveSpec(spec, file)
      if (r.pkg) {
        const bad = FORBIDDEN_PACKAGES.some((p) => p.test(r.pkg)) || (!mapAllowed && MAP_PACKAGES.some((p) => p.test(r.pkg)))
        if (bad) hits.push({ what: r.pkg, chain: chainOf(file) })
        continue
      }
      if (!r.file) continue
      const rel = relative(SRC, r.file).split('\\').join('/')
      if (FORBIDDEN_FILES.some((p) => p.test(rel))) {
        hits.push({ what: `src/${rel}`, chain: chainOf(file) })
        continue
      }
      if (!seen.has(r.file)) {
        seen.set(r.file, file)
        queue.push(r.file)
      }
    }
  }
  return { hits, reached: seen.size }
}

export function lintPhoneImports() {
  const files = walkFiles(PHONE)
  // 1. Every phone file's static graph — the Map screen's files excepted,
  //    which are reached through the dynamic imports below: no map library,
  //    nothing forbidden.
  const main = check(files.filter((f) => !f.startsWith(MAP_SCREEN)), { mapAllowed: false })
  // 2. Dynamic imports written under src/mobile/: outside the Map screen they
  //    are walked as static (map forbidden); inside it, the map is allowed.
  const lazyHits = []
  for (const f of files) {
    const { dynamics } = importsOf(readFileSync(f, 'utf8'))
    for (const spec of dynamics) {
      const r = resolveSpec(spec, f)
      const inMap = f.startsWith(MAP_SCREEN)
      if (r.pkg) {
        const bad = FORBIDDEN_PACKAGES.some((p) => p.test(r.pkg)) || (!inMap && MAP_PACKAGES.some((p) => p.test(r.pkg)))
        if (bad) lazyHits.push({ what: r.pkg, chain: [relative(ROOT, f), `import('${spec}')`] })
        continue
      }
      if (!r.file) continue
      const sub = check([r.file], { mapAllowed: inMap })
      for (const h of sub.hits) lazyHits.push({ what: h.what, chain: [relative(ROOT, f), `import('${spec}')`, ...h.chain] })
    }
  }
  return { files: files.length, reached: main.reached, hits: [...main.hits, ...lazyHits] }
}

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])
if (isMain) {
  const r = lintPhoneImports()
  if (process.argv.includes('--json')) {
    console.log(JSON.stringify(r, null, 2))
  } else {
    console.log(`lint-phone-imports: ${r.files} phone files, ${r.reached} modules reached by static imports`)
    for (const h of r.hits) console.log(`  FORBIDDEN ${h.what}\n    via ${h.chain.join('\n      → ')}`)
    console.log(r.hits.length ? `${r.hits.length} forbidden import(s)` : 'ok — nothing forbidden is reachable')
  }
  process.exit(r.hits.length ? 1 : 0)
}
