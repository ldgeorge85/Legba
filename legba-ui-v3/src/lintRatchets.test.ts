// @vitest-environment node
/**
 * The two lint RATCHETS (UI-B1/tokens; UI-D1 §6.4, §7), run on every
 * `vitest run`:
 *
 *   - `tools/lint-tokens.mjs` — raw Tailwind palette classes (the neutral
 *     greys: slate / gray / zinc / neutral / stone at a numbered step) that
 *     should be `--surf/--ink/--line` tokens. 1,440 at the W7 pin.
 *   - `tools/lint-absence.mjs` — `?? 0` / `|| 0` / `toFixed` on a nullable in
 *     the render paths: absence defaulted to a number the reader cannot tell
 *     from a measurement.
 *
 * Each ceiling is the `--max` in its package.json script; the count may only
 * go DOWN (lower the number in the same commit that clears some). Each tool
 * also holds a list of ZERO paths that must stay at none: the shell chrome,
 * the primitives, the rail, and every file UI-B1/tokens migrated. Both tools
 * are exercised against a seeded bad tree first, so a ratchet that silently
 * stopped matching would fail here rather than pass forever.
 */
import { describe, it, expect, afterAll } from 'vitest'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const UI_ROOT = join(__dirname, '..')
const PKG = JSON.parse(readFileSync(join(UI_ROOT, 'package.json'), 'utf8')) as {
  scripts: Record<string, string>
}

function ceiling(script: string): number {
  const m = /--max\s+(\d+)/.exec(PKG.scripts[script] ?? '')
  if (!m) throw new Error(`package.json "${script}" carries no --max ceiling`)
  return Number(m[1])
}

interface LintResult {
  status: number | null
  total: number
  perFile: Array<{ file: string; count: number; findings?: Array<{ rule: string; line: number }> }>
  zeroHits: Array<{ file: string }>
}

/**
 * Seeded raw classes are written with `~` and swapped to `-` at runtime, so
 * this file does not itself count against the token ratchet it tests.
 */
const raw = (s: string) => s.replace(/~/g, '-')

function runLint(tool: string, args: string[]): LintResult {
  const r = spawnSync(process.execPath, [join(UI_ROOT, 'tools', tool), '--json', ...args], {
    cwd: UI_ROOT,
    encoding: 'utf8',
  })
  const out = JSON.parse(r.stdout) as Omit<LintResult, 'status'>
  return { status: r.status, ...out }
}

const tmpRoots: string[] = []
afterAll(() => {
  for (const d of tmpRoots) rmSync(d, { recursive: true, force: true })
})

/** A throwaway `<tmp>/src` tree holding the given files. */
function seedTree(files: Record<string, string>): string {
  const root = mkdtempSync(join(tmpdir(), 'legba-lint-'))
  tmpRoots.push(root)
  for (const [rel, text] of Object.entries(files)) {
    const p = join(root, 'src', rel)
    mkdirSync(join(p, '..'), { recursive: true })
    writeFileSync(p, text)
  }
  return join(root, 'src')
}

describe('lint-absence — fires on a seeded bad file', () => {
  const BAD = `
    export function Bad({ n, m, k }: { n: number | null; m?: number; k: number | null }) {
      const a = n ?? 0
      const b = m || 0
      const c = m?.toFixed(1)
      const d = k!.toFixed(2)
      const e = Number(k).toFixed(0)
      const f = (m ?? 1).toFixed(0)
      // a comment saying "x ?? 0" is not code
      const s = "y ?? 0 in a string"
      const measured = 0
      const max = Math.max(0, a)
      return <span>{a}{b}{c}{d}{e}{f}{s}{measured}{max}</span>
    }
  `
  it('flags every default-to-zero shape and nothing else', () => {
    const src = seedTree({ 'panels/Bad.tsx': BAD })
    const r = runLint('lint-absence.mjs', ['--src', src])
    expect(r.status).toBe(0)
    const rules = r.perFile[0].findings!.map((f) => f.rule).sort()
    expect(rules).toEqual(
      ['!.toFixed', '(default).toFixed', '?.toFixed', '?? 0', 'Number().toFixed', '|| 0'].sort(),
    )
    expect(r.total).toBe(6)
  })

  it('--max below the count fails the run', () => {
    const src = seedTree({ 'panels/Bad.tsx': BAD })
    expect(runLint('lint-absence.mjs', ['--src', src, '--max', '5']).status).toBe(1)
    expect(runLint('lint-absence.mjs', ['--src', src, '--max', '6']).status).toBe(0)
  })

  it('a zero path with ANY finding fails the run, ceiling or not', () => {
    const src = seedTree({ 'ui/thing.tsx': 'export const x = (v: number | null) => v ?? 0\n' })
    const r = runLint('lint-absence.mjs', ['--src', src, '--max', '999'])
    expect(r.status).toBe(1)
    expect(r.zeroHits.map((z) => z.file)).toEqual(['src/ui/thing.tsx'])
  })

  it('tests are not render paths', () => {
    const src = seedTree({ 'panels/Bad.test.tsx': 'const a = (n: number | null) => n ?? 0\n' })
    expect(runLint('lint-absence.mjs', ['--src', src]).total).toBe(0)
  })
})

describe('lint-tokens — fires on a seeded bad file', () => {
  it('counts raw palette classes and fails above --max', () => {
    const src = seedTree({ 'panels/Bad.tsx': raw('export const c = "text~slate~500 bg~gray~800 border~zinc~700"\n') })
    expect(runLint('lint-tokens.mjs', ['--src', src]).total).toBe(3)
    expect(runLint('lint-tokens.mjs', ['--src', src, '--max', '2']).status).toBe(1)
    expect(runLint('lint-tokens.mjs', ['--src', src, '--max', '3']).status).toBe(0)
  })

  it('a zero path (the shell, the rail, the primitives) fails on ONE raw class', () => {
    const src = seedTree({
      'App.tsx': raw('export const c = "text~slate~500"\n'),
      'components/ObjectRail.tsx': raw('export const r = "bg~gray~900"\n'),
      'ui/x.tsx': raw('export const u = "border~stone~700"\n'),
    })
    const r = runLint('lint-tokens.mjs', ['--src', src, '--max', '999'])
    expect(r.status).toBe(1)
    expect(r.zeroHits.map((z) => z.file).sort()).toEqual(
      ['src/App.tsx', 'src/components/ObjectRail.tsx', 'src/ui/x.tsx'].sort(),
    )
  })

  it('semantic ramps (amber / sky / rose …) are not raw palette', () => {
    const src = seedTree({ 'panels/Ok.tsx': 'export const c = "text-amber-400 bg-sky-900 text-rose-300"\n' })
    expect(runLint('lint-tokens.mjs', ['--src', src]).total).toBe(0)
  })
})

describe('the ratchets on the real tree', () => {
  it('lint-tokens: under its ceiling, the ceiling ratcheted down from 1,440, zero paths clean', () => {
    const max = ceiling('lint:tokens')
    expect(max).toBeLessThan(1440)
    const r = runLint('lint-tokens.mjs', ['--max', String(max)])
    expect(r.zeroHits).toEqual([])
    expect(r.total).toBeLessThanOrEqual(max)
    expect(r.status).toBe(0)
  })

  it('lint-absence: under its ceiling, zero paths clean', () => {
    const max = ceiling('lint:absence')
    const r = runLint('lint-absence.mjs', ['--max', String(max)])
    expect(r.zeroHits).toEqual([])
    expect(r.total).toBeLessThanOrEqual(max)
    expect(r.status).toBe(0)
  })

  // The ratchet argument (UI-B8): a ceiling above the measured count is slack
  // the next change can spend without a failure. Measure with a ceiling too
  // high to trip (--max 9999), parse the totals, and require the package.json
  // --max to EQUAL the count: lower the number in the commit that clears some.
  it('lint-tokens: the package.json ceiling equals the measured count (no slack)', () => {
    const measured = runLint('lint-tokens.mjs', ['--max', '9999'])
    expect(measured.status).toBe(0)
    expect(ceiling('lint:tokens')).toBe(measured.total)
  })

  it('lint-absence: the package.json ceiling equals the measured count (no slack)', () => {
    const measured = runLint('lint-absence.mjs', ['--max', '9999'])
    expect(measured.status).toBe(0)
    expect(ceiling('lint:absence')).toBe(measured.total)
  })
})
