#!/usr/bin/env node
/**
 * lint-engine-words — engine terms in the text reader components render.
 *
 * Readers see plain words; the engine's own terms (the check's internals,
 * route paths, producer ids, the operator's instruments) belong on the
 * operator side or inside the collapsed "How this was produced" readout. The
 * list of terms, with the reader words that replace each, is ONE file:
 * `src/lib/engineWords.json` (also read by the console and the browser walk).
 *
 * This tool counts those terms in the text the READER component trees render
 * — JSX text, a string or template written directly inside JSX, and the
 * attributes and object fields that carry visible words (title, label,
 * placeholder, aria-label, alt, text, note, hint, subtitle, value, what). A
 * string passed to a call (a route given to `apiGet`, a query key) is not
 * visible text and is never counted; nor are comments.
 *
 * An admin-only readout inside a reader file still counts: the number is a
 * ceiling that may only go down (`--max`, run by `src/lintRatchets.test.ts`),
 * and the files listed in ZERO_PATHS are held at none.
 *
 *   node tools/lint-engine-words.mjs               # report
 *   node tools/lint-engine-words.mjs --max 40      # exit 1 above the ceiling
 *   node tools/lint-engine-words.mjs --src <dir>   # scan another tree (the test fixture)
 *   node tools/lint-engine-words.mjs --json        # machine-readable
 */
import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const ROOT = join(fileURLToPath(new URL('.', import.meta.url)), '..')

/** The reader component trees, relative to the scanned src dir. */
export const READER_ROOTS = [
  'v4/read',
  'v4/watch',
  'v4/world',
  'v4/why',
  'panels/subject',
  'panels/target',
  'panels/ledger',
  'components',
  'mobile',
]

/** Files held at ZERO engine words (relative to legba-ui-v3/). The list only grows. */
export const ZERO_PATHS = [
  'src/components/HowProduced.tsx',
  'src/components/StatusBar.tsx',
  'src/components/RecordSheet.tsx',
  'src/components/inspector/SubjectReadState.tsx',
  'src/v4/read/BoardList.tsx',
  'src/v4/world/LayerPanel.tsx',
  'src/panels/subject/SubjectPage.tsx',
  'src/panels/target/Claims.tsx',
  'src/components/inspector/CitedAssessment.tsx',
  'src/components/inspector/InspectorPanel.tsx',
  'src/v4/read/GapsBand.tsx',
  'src/v4/read/JudgmentBands.tsx',
  'src/v4/read/MorningRead.tsx',
  'src/v4/why/WorldAssessment.tsx',
  'src/v4/why/CountryUnitsAssessment.tsx',
  'src/mobile/',
]

/** Attribute and field names whose string value is shown to the reader. */
const VISIBLE_NAMES = new Set([
  'title',
  'label',
  'placeholder',
  'aria-label',
  'alt',
  'text',
  'note',
  'hint',
  'subtitle',
  'value',
  'what',
  'headline',
  'sentence',
])

function loadTerms(srcDir) {
  const doc = JSON.parse(readFileSync(join(srcDir, 'lib', 'engineWords.json'), 'utf8'))
  return {
    terms: doc.terms.map((t) => ({ term: t.term, re: new RegExp(t.pattern, t.flags) })),
    allow: new Set(doc.allow.texts),
  }
}

function parseArgs(argv) {
  const out = { max: null, src: join(ROOT, 'src'), json: false }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--max') out.max = Number(argv[++i])
    else if (a === '--src') out.src = argv[++i]
    else if (a === '--json') out.json = true
  }
  return out
}

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

/** The visible text a string-ish node carries (a template's literal parts joined by a space). */
function textOf(node) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text
  if (ts.isTemplateExpression(node)) return [node.head.text, ...node.templateSpans.map((s) => s.literal.text)].join(' ')
  return null
}

function nameOf(node) {
  if (!node) return null
  if (ts.isIdentifier(node) || ts.isStringLiteral(node)) return node.text
  if (ts.isJsxNamespacedName?.(node)) return `${node.namespace.text}:${node.name.text}`
  return node.getText ? node.getText() : null
}

/** Is this string the visible text of JSX, or of a field that carries visible words? */
function isVisible(node) {
  let p = node.parent
  // a conditional's branches and a parenthesis are transparent
  while (p && (ts.isParenthesizedExpression(p) || ts.isConditionalExpression(p) || (ts.isBinaryExpression(p) && p.operatorToken.kind !== ts.SyntaxKind.EqualsToken))) p = p.parent
  if (!p) return false
  if (ts.isJsxExpression(p)) {
    const host = p.parent
    if (ts.isJsxAttribute(host)) return VISIBLE_NAMES.has(nameOf(host.name))
    return true // a child of an element
  }
  if (ts.isJsxAttribute(p)) return VISIBLE_NAMES.has(nameOf(p.name))
  if (ts.isPropertyAssignment(p)) return VISIBLE_NAMES.has(nameOf(p.name))
  return false
}

export function scanSource(text, fileName, vocab) {
  const kind = fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  const sf = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, kind)
  const findings = []
  const check = (node, s) => {
    const t = s.replace(/\s+/g, ' ').trim()
    if (!t || vocab.allow.has(t)) return
    for (const { term, re } of vocab.terms) {
      if (re.test(t)) {
        const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf))
        findings.push({ line: line + 1, rule: term, text: t.slice(0, 80) })
      }
    }
  }
  const visit = (node) => {
    if (ts.isJsxText(node)) check(node, node.text)
    else {
      const s = textOf(node)
      if (s != null && isVisible(node)) check(node, s)
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return findings
}

export function scanTree(srcDir) {
  const vocab = loadTerms(srcDir)
  const base = join(srcDir, '..')
  const files = new Set()
  for (const root of READER_ROOTS) for (const f of walk(join(srcDir, root))) files.add(f)
  const perFile = []
  let total = 0
  for (const file of files) {
    const findings = scanSource(readFileSync(file, 'utf8'), file, vocab)
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
  const { total, perFile } = scanTree(args.src)
  const zeroHits = perFile.filter((f) => ZERO_PATHS.some((z) => (z.endsWith('/') ? f.file.startsWith(z) : f.file === z)))
  if (args.json) {
    console.log(JSON.stringify({ total, files: perFile.length, perFile, zeroHits }, null, 2))
  } else {
    console.log(`\nengine-words lint — engine terms in reader component text (${READER_ROOTS.join(', ')})\n`)
    console.log(`  files with findings: ${perFile.length}`)
    console.log(`  total findings     : ${total}\n`)
    for (const { file, count, findings } of perFile) {
      console.log(`    ${String(count).padStart(4)}  ${file}  (${[...new Set(findings.map((f) => f.rule))].join(', ')})`)
    }
    console.log('')
  }
  let failed = false
  for (const z of zeroHits) {
    for (const f of z.findings) console.error(`FAIL (zero path): ${z.file}:${f.line}  ${f.rule}  ${f.text}`)
    failed = true
  }
  if (args.max != null && Number.isFinite(args.max) && total > args.max) {
    console.error(`FAIL (--max ${args.max}): ${total} engine words in reader text exceed the ceiling.`)
    failed = true
  }
  if (failed) process.exit(1)
  if (!args.json) console.log('ok')
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main()
