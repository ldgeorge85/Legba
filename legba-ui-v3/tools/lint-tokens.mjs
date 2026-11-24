#!/usr/bin/env node
/**
 * lint-tokens — raw Tailwind palette scanner (S7-T6).
 *
 * The design system exposes SEMANTIC tokens (`--surf-*` / `--ink-*` /
 * `--line-*`, aliased to `bg-surf-*` / `text-ink-*` / `border-line[-strong]`)
 * so a surface flips with the light/dark toggle and retunes from one place.
 * Raw palette classes (`slate-500`, `gray-700`, …) are hardcoded-dark and do
 * NOT flip — this script counts them so the migration has a number to drive.
 *
 *   node tools/lint-tokens.mjs           # report the count + top offenders
 *   node tools/lint-tokens.mjs --strict  # exit 1 if ANY raw palette class remains
 *   node tools/lint-tokens.mjs --max 900 # exit 1 if the count exceeds a ceiling
 *   node tools/lint-tokens.mjs --src DIR # scan another tree (the test fixture)
 *   node tools/lint-tokens.mjs --json    # machine-readable result
 *
 * THE RATCHET (UI-B1/tokens, UI-D1 §7): the ceiling lives in package.json's
 * `lint:tokens` script and `src/lintRatchets.test.ts` runs this tool on every
 * `vitest run`, so the count can only go DOWN — lower `--max` when you clear
 * some. It started at 1,440 in 82 files (the W7 pin). Independently of the
 * ceiling, the paths in `ZERO_PATHS` must hold NO raw palette class at all:
 * the shell chrome, the primitives, and the rail (UI-D1 §7: "zero in the
 * shell, `subject.page` and the rail"). A zero path that does not exist yet is
 * vacuously clean — the entry is a promise to the lane that creates it.
 *
 * Intentionally dependency-free (runs in the bare node build image). Matches
 * the Tailwind color scales that a token should replace; the accent/severity
 * ramps (emerald/amber/sky/rose/red…) are semantic and NOT flagged.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { existsSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(fileURLToPath(new URL('.', import.meta.url)), '..')

/**
 * Paths held at ZERO raw palette classes (relative to legba-ui-v3/; a trailing
 * `/` is a directory prefix, a `*` a filename prefix). The list only grows.
 */
const ZERO_PATHS = [
  // The shell chrome.
  'src/App.tsx',
  'src/components/WorkspaceBar.tsx',
  'src/components/StatusBar.tsx',
  // The U-2 frame (held at zero from its first commit).
  'src/components/SurfaceBar.tsx',
  // The U-6 frame (held at zero from its first commit).
  'src/components/ConsultDock.tsx',
  'src/components/ConsultFrameContext.tsx',
  'src/components/FrameRail.tsx',
  'src/components/Stage.tsx',
  'src/components/Splitter.tsx',
  'src/components/ViewHost.tsx',
  'src/components/CitedRecords.tsx',
  'src/lib/frameGrid.ts',
  'src/lib/panelActions.ts',
  'src/lib/onScreen.ts',
  'src/panel-registry/contracts.ts',
  'src/state/frame.ts',
  'src/components/ConsultPointLine.tsx',
  'src/components/RecordSheet.tsx',
  // The U-10 Record sheet's chain (held at zero from its first commit).
  'src/components/record/',
  'src/lib/recordSheet.ts',
  'src/lib/recordChain.ts',
  'src/components/RegionSlot.tsx',
  'src/components/SingletonView.tsx',
  'src/components/CatalogPopover.tsx',
  'src/components/CaseRail.tsx',
  'src/v4/world/TimeScrubber.tsx',
  // U-4: the globe (held at zero from its first commit).
  'src/v4/globe/',
  'src/panels/v4/MapPanel.tsx',
  'src/lib/surfaces.ts',
  'src/lib/surfaceDock.ts',
  'src/components/CommandPalette.tsx',
  'src/components/PanelChrome.tsx',
  'src/components/Sidebar.tsx',
  // The rail (UI-B1/shell builds it; held at zero from its first commit).
  'src/components/ObjectRail*',
  'src/components/rail/',
  // The subject page (UI-B3, W11; held at zero from its first commit).
  'src/panels/subject/',
  // The Flow (N-J; held at zero from its first commit).
  'src/panels/flow/',
  // The primitives and the files UI-B1/tokens migrated onto them.
  'src/ui/',
  'src/components/InfoTip.tsx',
  'src/components/PanelTabs.tsx',
  'src/components/ScopePicker.tsx',
  'src/v4/flow/WiringModal.tsx',
  'src/lib/Figure.tsx',
]

function onZeroPath(file) {
  return ZERO_PATHS.some((z) =>
    z.endsWith('/') ? file.startsWith(z) : z.endsWith('*') ? file.startsWith(z.slice(0, -1)) : file === z,
  )
}

// The neutral greys a semantic surface/ink/line token should replace.
const RAW_PALETTE = /\b(?:slate|gray|zinc|neutral|stone)-(?:50|[1-9]00|950)\b/g

const args = new Set(process.argv.slice(2))
const strict = args.has('--strict')
const json = args.has('--json')
const maxIdx = process.argv.indexOf('--max')
const max = maxIdx >= 0 ? Number(process.argv[maxIdx + 1]) : null
const srcIdx = process.argv.indexOf('--src')
const SRC = srcIdx >= 0 ? process.argv[srcIdx + 1] : join(ROOT, 'src')
const BASE = join(SRC, '..')

/** Recursively collect .ts/.tsx source files under src. */
function walk(dir) {
  const out = []
  if (!existsSync(dir)) return out
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) out.push(...walk(p))
    else if (/\.(ts|tsx)$/.test(name)) out.push(p)
  }
  return out
}

const perFile = []
let total = 0
for (const file of walk(SRC)) {
  const text = readFileSync(file, 'utf8')
  const matches = text.match(RAW_PALETTE)
  if (matches && matches.length > 0) {
    perFile.push({ file: relative(BASE, file).split(sep).join('/'), count: matches.length })
    total += matches.length
  }
}
perFile.sort((a, b) => b.count - a.count || a.file.localeCompare(b.file))
const zeroHits = perFile.filter((f) => onZeroPath(f.file))

if (json) {
  console.log(JSON.stringify({ total, files: perFile.length, perFile, zeroHits }, null, 2))
  if (zeroHits.length > 0 || (max != null && Number.isFinite(max) && total > max)) process.exit(1)
  process.exit(0)
}

console.log(`\ntoken lint — raw Tailwind palette classes (should be --surf/--ink/--line tokens)\n`)
console.log(`  files with raw palette: ${perFile.length}`)
console.log(`  total raw occurrences : ${total}\n`)
if (perFile.length > 0) {
  console.log('  top offenders:')
  for (const { file, count } of perFile.slice(0, 15)) {
    console.log(`    ${String(count).padStart(4)}  ${file}`)
  }
  console.log('')
}

if (zeroHits.length > 0) {
  for (const { file, count } of zeroHits) {
    console.error(`FAIL (zero path): ${file} holds ${count} raw palette class(es).`)
  }
  process.exit(1)
}
if (strict && total > 0) {
  console.error(`FAIL (--strict): ${total} raw palette occurrences remain.`)
  process.exit(1)
}
if (max != null && Number.isFinite(max) && total > max) {
  console.error(`FAIL (--max ${max}): ${total} raw palette occurrences exceed the ceiling.`)
  process.exit(1)
}
console.log(max != null ? `ok (under the --max ${max} ceiling).` : 'ok (reporting mode).')
