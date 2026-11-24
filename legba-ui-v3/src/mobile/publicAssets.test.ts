// @vitest-environment node
/**
 * M-0a — the phone's page shell: the icon both builds reference exists and is
 * emitted by both, and the phone is dark only.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const UI = join(__dirname, '..', '..')
const read = (p: string) => readFileSync(join(UI, p), 'utf8')

describe('the icon (/favicon.svg)', () => {
  it('both HTML entries reference /favicon.svg, and the file exists for each build', () => {
    expect(read('index.html')).toContain('href="/favicon.svg"')
    expect(read('m.html')).toContain('href="/favicon.svg"')
    // the desktop build's public dir and the phone build's own
    expect(read('public/favicon.svg')).toMatch(/^<svg /)
    expect(read('public-m/favicon.svg')).toMatch(/^<svg /)
  })

  it('is one icon: the two copies are byte-identical', () => {
    expect(read('public-m/favicon.svg')).toBe(read('public/favicon.svg'))
  })

  it('is the console\'s own mark: the dark ground and the magenta point, nothing external', () => {
    const svg = read('public/favicon.svg')
    expect(svg).toContain('#11161a') // --surf-base, the ground
    expect(svg).toContain('#ec6fa9') // --point
    expect(svg).not.toMatch(/<image|href=|<script|font-family/)
  })

  it('the phone build emits its public dir (it was `publicDir: false`, so the icon was never emitted)', () => {
    const cfg = read('vite.config.mobile.ts')
    expect(cfg).not.toMatch(/publicDir:\s*false/)
    expect(cfg).toMatch(/publicDir:\s*path\.resolve\(__dirname, 'public-m'\)/)
  })
})

describe('the phone page is dark only', () => {
  it('declares color-scheme dark and nothing else', () => {
    const m = read('m.html')
    expect(m).toMatch(/<meta name="color-scheme" content="dark" \/>/)
    expect(m).not.toMatch(/content="dark light"|content="light/)
    expect(m).toContain('viewport-fit=cover')
  })

  it('the phone chrome pays out the top, left and right safe-area insets', () => {
    const css = read('src/mobile/mobile.css')
    for (const side of ['top', 'left', 'right', 'bottom']) expect(css).toContain(`env(safe-area-inset-${side}`)
  })
})
