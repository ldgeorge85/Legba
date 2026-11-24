// @vitest-environment node
/**
 * M-2 — the phone's import lint (`tools/lint-phone-imports.mjs`), run on every
 * `vitest run`: nothing under `src/mobile/` reaches the desktop frame, the
 * docking library, the graph/flow libraries, the YAML editor, the charting
 * library, or (outside the Map screen's own dynamic import) the map library.
 * The parser is exercised on seeded source first, so a lint that silently
 * stopped matching fails here rather than passing forever.
 */
import { describe, expect, it } from 'vitest'
import { spawnSync } from 'node:child_process'
import { join } from 'node:path'
// @ts-expect-error — a plain .mjs tool, no types
import { importsOf } from '../../tools/lint-phone-imports.mjs'

const UI_ROOT = join(__dirname, '..', '..')

describe('lint-phone-imports: the parser', () => {
  it('follows value imports, side effects and re-exports; skips type-only ones; lists dynamic ones apart', () => {
    const src = [
      "import { a } from 'dockview-react'",
      "import type { B } from 'cytoscape'",
      "import { type C, type D } from 'recharts'",
      "import E, { type F } from '@/App'",
      "export { G } from './g'",
      "export type { H } from './h'",
      "import './side.css'",
      "const M = lazy(() => import('maplibre-gl'))",
      '// import { x } from "js-yaml"',
      "import {\n  multi,\n  line,\n} from 'elkjs'",
    ].join('\n')
    const { statics, dynamics } = importsOf(src) as { statics: string[]; dynamics: string[] }
    expect(statics).toEqual(expect.arrayContaining(['dockview-react', '@/App', './g', './side.css', 'elkjs']))
    expect(statics).not.toContain('cytoscape')
    expect(statics).not.toContain('recharts')
    expect(statics).not.toContain('./h')
    expect(statics).not.toContain('js-yaml')
    expect(dynamics).toEqual(['maplibre-gl'])
  })
})

describe('lint-phone-imports: the tree', () => {
  it('nothing forbidden is reachable from the phone', () => {
    const r = spawnSync(process.execPath, [join(UI_ROOT, 'tools', 'lint-phone-imports.mjs'), '--json'], { cwd: UI_ROOT, encoding: 'utf8' })
    const out = JSON.parse(r.stdout) as { files: number; reached: number; hits: Array<{ what: string; chain: string[] }> }
    expect(out.files).toBeGreaterThan(10)
    expect(out.hits.map((h) => `${h.what} via ${h.chain.join(' → ')}`)).toEqual([])
    expect(r.status).toBe(0)
  })
})
