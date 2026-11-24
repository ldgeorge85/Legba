#!/usr/bin/env node
/**
 * lint-absence — the absence-as-zero scanner (UI-D1 §6.4).
 *
 * Absence renders as absence, never as 0. The commonest way a render path
 * breaks that rule is a one-token default that turns "we have no reading"
 * into a number the reader cannot tell from a measurement. This tool counts
 * those defaults in the RENDER PATHS — `src/panels/`, `src/components/` and
 * the primitives in `src/ui/` — so the migration has a number to drive and
 * the count can only go down (the `--max` ratchet; `src/lintRatchets.test.ts`
 * runs it on every `vitest run`).
 *
 * What it flags (syntax, via the TypeScript parser — comments and strings
 * never match):
 *
 *   ?? 0              `x ?? 0` / `x ?? 0.0` — a nullish reading defaulted to zero
 *   || 0              `x || 0` — the same, and it also eats a measured 0/NaN
 *   ?.toFixed         `x?.toFixed(n)` — toFixed on an optional receiver
 *   !.toFixed         `x!.toFixed(n)` — toFixed on a nullable asserted away
 *   Number().toFixed  `Number(x).toFixed(n)` — Number(null) is 0
 *   (… ?? n).toFixed  any `??` / `||` default feeding toFixed
 *
 * A *measured* zero is fine and is never flagged: a literal `0`, a count of a
 * list the server sent, `Math.max(0, …)`. What is flagged is a DEFAULT.
 *
 *   node tools/lint-absence.mjs               # report the count + offenders
 *   node tools/lint-absence.mjs --max 120     # exit 1 above the ceiling
 *   node tools/lint-absence.mjs --zero <path> # exit 1 if <path> has any (repeatable)
 *   node tools/lint-absence.mjs --src <dir>   # scan another tree (the test fixture)
 *   node tools/lint-absence.mjs --json        # machine-readable result
 *
 * The files UI-B1/tokens touched are pinned at zero in `ZERO_PATHS` below;
 * the list only grows.
 */
import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const ROOT = join(fileURLToPath(new URL('.', import.meta.url)), '..')

/** Render-path roots, relative to the scanned src dir. */
const RENDER_ROOTS = ['panels', 'components', 'ui']

/**
 * Files held at ZERO findings (relative to the repo's legba-ui-v3/). A file
 * that does not exist yet is vacuously clean — the entry is a promise to the
 * lane that creates it.
 */
export const ZERO_PATHS = [
  'src/ui/',
  'src/components/InfoTip.tsx',
  'src/components/PanelTabs.tsx',
  'src/components/ScopePicker.tsx',
  'src/components/CommandPalette.tsx',
  'src/v4/flow/WiringModal.tsx',
  'src/lib/Figure.tsx',
  'src/panels/flow/',
  // The U-2 frame (held at zero from its first commit).
  'src/App.tsx',
  'src/components/SurfaceBar.tsx',
  // The U-6 frame (held at zero from its first commit).
  'src/components/ConsultDock.tsx',
  'src/components/ConsultFrameContext.tsx',
  'src/components/FrameRail.tsx',
  'src/components/Stage.tsx',
  'src/components/Splitter.tsx',
  'src/components/ViewHost.tsx',
  'src/components/CitedRecords.tsx',
  'src/components/ConsultPointLine.tsx',
  'src/components/RecordSheet.tsx',
  // The U-10 Record sheet's chain (held at zero from its first commit).
  'src/components/record/',
  'src/lib/recordSheet.ts',
  'src/lib/recordChain.ts',
  'src/components/RegionSlot.tsx',
  'src/components/SingletonView.tsx',
  'src/components/CatalogPopover.tsx',
  'src/components/ObjectRail.tsx',
  'src/components/StatusBar.tsx',
]

function parseArgs(argv) {
  const out = { max: null, zero: [], src: join(ROOT, 'src'), json: false }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--max') out.max = Number(argv[++i])
    else if (a === '--zero') out.zero.push(argv[++i])
    else if (a === '--src') out.src = argv[++i]
    else if (a === '--json') out.json = true
  }
  return out
}

/** Recursively collect .ts/.tsx files, skipping tests and fixtures. */
function walk(dir) {
  const out = []
  if (!existsSync(dir)) return out
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) {
      if (name === '__fixtures__' || name === 'node_modules') continue
      out.push(...walk(p))
    } else if (/\.(ts|tsx)$/.test(name) && !/\.(test|spec)\.(ts|tsx)$/.test(name) && !name.endsWith('.d.ts')) {
      out.push(p)
    }
  }
  return out
}

function isZeroLiteral(node) {
  return ts.isNumericLiteral(node) && Number(node.text) === 0
}

function unwrapParens(node) {
  let n = node
  while (ts.isParenthesizedExpression(n)) n = n.expression
  return n
}

/** Scan one file's text; return [{line, rule, text}]. */
export function scanSource(text, fileName = 'x.tsx') {
  const kind = fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  const sf = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, kind)
  const findings = []
  const push = (node, rule) => {
    const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf))
    findings.push({ line: line + 1, rule, text: node.getText(sf).slice(0, 80) })
  }
  const visit = (node) => {
    if (ts.isBinaryExpression(node) && isZeroLiteral(unwrapParens(node.right))) {
      if (node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken) push(node, '?? 0')
      else if (node.operatorToken.kind === ts.SyntaxKind.BarBarToken) push(node, '|| 0')
    }
    if (ts.isPropertyAccessExpression(node) && node.name.text === 'toFixed') {
      const recv = node.expression
      const inner = unwrapParens(recv)
      if (node.questionDotToken) push(node, '?.toFixed')
      else if (ts.isNonNullExpression(recv)) push(node, '!.toFixed')
      else if (
        ts.isCallExpression(inner) &&
        ts.isIdentifier(inner.expression) &&
        inner.expression.text === 'Number'
      )
        push(node, 'Number().toFixed')
      else if (
        ts.isBinaryExpression(inner) &&
        !isZeroLiteral(unwrapParens(inner.right)) &&
        (inner.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken ||
          inner.operatorToken.kind === ts.SyntaxKind.BarBarToken)
      )
        push(node, '(default).toFixed')
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return findings
}

/**
 * Scan the render roots under `srcDir`, plus any zero-path FILE outside them
 * (a touched file is held at zero wherever it lives). Paths are reported
 * relative to `srcDir`'s parent (`src/panels/…`).
 */
export function scanTree(srcDir, extraFiles = []) {
  const base = join(srcDir, '..')
  const files = new Set()
  for (const root of RENDER_ROOTS) for (const f of walk(join(srcDir, root))) files.add(f)
  for (const rel of extraFiles) {
    const abs = join(base, rel)
    if (!rel.endsWith('/') && existsSync(abs)) files.add(abs)
  }
  const perFile = []
  let total = 0
  for (const file of files) {
    const findings = scanSource(readFileSync(file, 'utf8'), file)
    if (findings.length > 0) {
      perFile.push({ file: relative(base, file).split(sep).join('/'), count: findings.length, findings })
      total += findings.length
    }
  }
  perFile.sort((a, b) => b.count - a.count || a.file.localeCompare(b.file))
  return { total, perFile }
}

function main() {
  const args = parseArgs(process.argv.slice(2))
  const zeroList = [...ZERO_PATHS, ...args.zero]
  const { total, perFile } = scanTree(args.src, zeroList)
  const zeroHits = perFile.filter((f) => zeroList.some((z) => (z.endsWith('/') ? f.file.startsWith(z) : f.file === z)))

  if (args.json) {
    console.log(JSON.stringify({ total, files: perFile.length, perFile, zeroHits }, null, 2))
  } else {
    console.log(`\nabsence lint — "?? 0" / "|| 0" / toFixed on a nullable, in render paths (${RENDER_ROOTS.join(', ')})\n`)
    console.log(`  files with findings: ${perFile.length}`)
    console.log(`  total findings     : ${total}\n`)
    for (const { file, count, findings } of perFile.slice(0, 15)) {
      console.log(`    ${String(count).padStart(4)}  ${file}  (${[...new Set(findings.map((f) => f.rule))].join(', ')})`)
    }
    console.log('')
  }

  let failed = false
  if (zeroHits.length > 0) {
    for (const z of zeroHits) {
      for (const f of z.findings) console.error(`FAIL (zero path): ${z.file}:${f.line}  ${f.rule}  ${f.text}`)
    }
    failed = true
  }
  if (args.max != null && Number.isFinite(args.max) && total > args.max) {
    console.error(`FAIL (--max ${args.max}): ${total} absence-as-zero defaults exceed the ceiling.`)
    failed = true
  }
  if (failed) process.exit(1)
  if (!args.json) console.log('ok')
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main()
