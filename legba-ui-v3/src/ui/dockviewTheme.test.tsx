/**
 * The Dockview theme object — it mounts on a real Dockview, it replaces the
 * Abyss class, and its stylesheet maps EVERY `--dv-*` variable the Abyss theme
 * sets onto a workstation token (so the light theme follows the tokens and no
 * dock colour is left on Dockview's defaults).
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, waitFor, cleanup } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createRequire } from 'node:module'
import { DockviewReact, type DockviewReadyEvent, type IDockviewPanelProps } from 'dockview-react'
import { legbaDark, DOCK_TAB_STRIP_PX } from './dockviewTheme'

afterEach(cleanup)

function Body(props: IDockviewPanelProps) {
  return <div data-testid={`body-${props.api.id}`}>panel</div>
}

/** The declarations inside the FIRST `{…}` after `selector` in `css`. */
function block(css: string, selector: string): string {
  const at = css.indexOf(`${selector} {`)
  if (at < 0) throw new Error(`no ${selector} block`)
  const open = css.indexOf('{', at)
  return css.slice(open + 1, css.indexOf('}', open))
}

/** Every `--dv-*` name declared in a block. */
function dvVars(decls: string): Set<string> {
  return new Set([...decls.matchAll(/(--dv-[a-z0-9-]+)\s*:/g)].map((m) => m[1]))
}

const GLOBALS = readFileSync(join(__dirname, '..', 'globals.css'), 'utf8')
const require_ = createRequire(import.meta.url)
const DOCKVIEW_CSS = readFileSync(require_.resolve('dockview-react/dist/styles/dockview.css'), 'utf8')

describe('legbaDark — the Dockview theme object', () => {
  it('is a real theme object with the §5.5 behaviour', () => {
    expect(legbaDark.className).toBe('dockview-theme-legba')
    expect(legbaDark.gap).toBe(1)
    expect(legbaDark.dndTabIndicator).toBe('line')
    expect(legbaDark.edgeGroupCollapsedSize).toBe(DOCK_TAB_STRIP_PX)
  })

  it('mounts on a real Dockview: the legba class is applied and Abyss is gone', async () => {
    let ready = false
    const { container } = render(
      <div style={{ width: 800, height: 600 }}>
        <DockviewReact
          components={{ default: Body }}
          theme={legbaDark}
          onReady={(ev: DockviewReadyEvent) => {
            ev.api.addPanel({ id: 'p1', component: 'default', title: 'one' })
            ready = true
          }}
        />
      </div>,
    )
    await waitFor(() => expect(ready).toBe(true))
    expect(container.querySelector('.dockview-theme-legba')).not.toBeNull()
    expect(container.querySelector('.dockview-theme-abyss')).toBeNull()
  })

  it('the stylesheet maps every --dv-* variable the Abyss theme sets', () => {
    // Abyss is split over two blocks in dockview.css: the structural one and
    // the colour one. The union is what the old className relied on.
    const abyssA = block(DOCKVIEW_CSS, '.dockview-theme-abyss')
    const rest = DOCKVIEW_CSS.slice(DOCKVIEW_CSS.indexOf('.dockview-theme-abyss {') + 10)
    const abyssB = block(rest, '.dockview-theme-abyss')
    const abyss = new Set([...dvVars(abyssA), ...dvVars(abyssB)])
    // Abyss's private palette names are its own implementation detail.
    for (const v of [...abyss]) if (v.startsWith('--dv-color-abyss')) abyss.delete(v)
    const legba = dvVars(block(GLOBALS, '.dockview-theme-legba'))
    const missing = [...abyss].filter((v) => !legba.has(v))
    expect(missing).toEqual([])
    expect(abyss.size).toBeGreaterThanOrEqual(23)
  })

  it('every SURFACE / INK / LINE / ACCENT variable reads a token, never a literal', () => {
    const decls = block(GLOBALS, '.dockview-theme-legba')
    const colourVars = [...decls.matchAll(/(--dv-[a-z0-9-]+)\s*:\s*([^;]+);/g)].filter(([, name]) =>
      /(background-color|-color$|separator-border|tab-divider)/.test(name) && !name.includes('tab-group-color'),
    )
    expect(colourVars.length).toBeGreaterThanOrEqual(15)
    const literal = colourVars.filter(([, , value]) => !/var\(--|transparent|color-mix\(/.test(value))
    expect(literal.map(([, n, v]) => `${n}: ${v}`)).toEqual([])
  })

  it('the Abyss className hack and its light-theme overrides are gone from globals.css', () => {
    expect(GLOBALS).not.toMatch(/^\.dockview-theme-abyss\s*\{/m)
    expect(GLOBALS).not.toMatch(/\.theme-light\s+\.dockview-theme-abyss/)
  })
})
