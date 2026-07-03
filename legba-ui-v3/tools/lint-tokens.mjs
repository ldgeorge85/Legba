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
 *
 * Intentionally dependency-free (runs in the bare node build image). Matches
 * the Tailwind color scales that a token should replace; the accent/severity
 * ramps (emerald/amber/sky/rose/red…) are semantic and NOT flagged.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(fileURLToPath(new URL('.', import.meta.url)), '..')
const SRC = join(ROOT, 'src')

// The neutral greys a semantic surface/ink/line token should replace.
const RAW_PALETTE = /\b(?:slate|gray|zinc|neutral|stone)-(?:50|[1-9]00|950)\b/g

const args = new Set(process.argv.slice(2))
const strict = args.has('--strict')
const maxIdx = process.argv.indexOf('--max')
const max = maxIdx >= 0 ? Number(process.argv[maxIdx + 1]) : null

/** Recursively collect .ts/.tsx source files under src. */
function walk(dir) {
  const out = []
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
    perFile.push({ file: relative(ROOT, file), count: matches.length })
    total += matches.length
  }
}
perFile.sort((a, b) => b.count - a.count)

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

if (strict && total > 0) {
  console.error(`FAIL (--strict): ${total} raw palette occurrences remain.`)
  process.exit(1)
}
if (max != null && Number.isFinite(max) && total > max) {
  console.error(`FAIL (--max ${max}): ${total} raw palette occurrences exceed the ceiling.`)
  process.exit(1)
}
console.log('ok (reporting mode — not a build gate).')
