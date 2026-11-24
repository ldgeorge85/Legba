// @vitest-environment node
/**
 * P-1 — the installed app's manifest and icons.
 *
 * The icons are redrawn from `public-m/favicon.svg` by the committed script and
 * compared PIXEL for pixel with the committed PNGs (so the outputs are what
 * the script makes, and nobody hand-edited one). The manifest is checked the
 * way Chrome's install criteria and iOS read it.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { decodeOwnPng, ICONS, OUT_DIR, parseMark, renderAll } from './make-phone-icons.mjs'

const UI = join(__dirname, '..')
const read = (p: string) => readFileSync(join(UI, p), 'utf8')
type Png = { px: number; rgba: Uint8Array }

describe('the icons', () => {
  const drawn = renderAll() as Record<string, Png>

  it('each committed PNG is exactly what the script draws from the one SVG', () => {
    for (const [name, { px, rgba }] of Object.entries(drawn)) {
      const got = decodeOwnPng(readFileSync(join(UI, OUT_DIR, name))) as Png
      expect(got.px, name).toBe(px)
      expect(Buffer.compare(Buffer.from(got.rgba), Buffer.from(rgba)), name).toBe(0)
    }
  })

  it('draws only the console mark: the ground, the crossroads, the magenta point', () => {
    const mark = parseMark(read('public-m/favicon.svg'))
    expect(mark.shapes.map((s: { kind: string }) => s.kind)).toEqual(['rect', 'stroke', 'circle'])
    expect(() => parseMark('<svg viewBox="0 0 32 32"><image href="x.png"/></svg>')).toThrow()
  })

  it('maskable and Home Screen icons are opaque squares with the mark inside the 40% safe circle', () => {
    for (const name of ['maskable-192.png', 'maskable-512.png', 'apple-touch-icon.png']) {
      const { px, rgba } = drawn[name]
      const c = px / 2
      let transparent = 0, outside = 0, markPixels = 0
      for (let y = 0; y < px; y++) {
        for (let x = 0; x < px; x++) {
          const o = (y * px + x) * 4
          if (rgba[o + 3] !== 255) transparent++
          const ground = rgba[o] === 0x11 && rgba[o + 1] === 0x16 && rgba[o + 2] === 0x1a
          if (!ground) {
            markPixels++
            if (Math.hypot(x + 0.5 - c, y + 0.5 - c) > 0.4 * px) outside++
          }
        }
      }
      expect({ name, transparent, outside }).toEqual({ name, transparent: 0, outside: 0 })
      expect(markPixels).toBeGreaterThan(0)
    }
  })
})

describe('the manifest', () => {
  const m = JSON.parse(read('public-m/manifest.webmanifest'))

  it('installs the phone app, scoped to /m/ and nothing else', () => {
    expect(m.id).toBe('/m/')
    expect(m.scope).toBe('/m/')
    expect(m.start_url).toBe('/m/')
    expect(m.display).toBe('standalone')
    expect(m.name).toBeTruthy()
    expect(m.short_name.length).toBeLessThanOrEqual(12)
    expect(m.prefer_related_applications).toBe(false)
  })

  it('is dark: the colours are the app ground (--surf-base), as m.html theme-color', () => {
    expect(read('src/globals.css')).toContain(`--surf-base: ${m.background_color};`)
    expect(m.theme_color).toBe(m.background_color)
    expect(read('m.html')).toContain(`<meta name="theme-color" content="${m.theme_color}" />`)
  })

  it('names 192 and 512 icons, a maskable pair, and each file exists at its declared size', () => {
    const icons = m.icons as { src: string; sizes: string; type: string; purpose: string }[]
    const has = (size: string, purpose: string) => icons.some((i) => i.sizes === size && i.purpose === purpose)
    expect(has('192x192', 'any') && has('512x512', 'any')).toBe(true)
    expect(has('192x192', 'maskable') && has('512x512', 'maskable')).toBe(true)
    for (const i of icons) {
      expect(i.src.startsWith('/m/icons/')).toBe(true)
      const name = i.src.slice('/m/icons/'.length)
      expect(ICONS[name], i.src).toBeTruthy()
      const { px } = decodeOwnPng(readFileSync(join(UI, OUT_DIR, name))) as Png
      expect(`${px}x${px}`).toBe(i.sizes)
      expect(i.type).toBe('image/png')
    }
  })

  it('carries no per-user data and no push (alerts stay on ntfy)', () => {
    const raw = read('public-m/manifest.webmanifest')
    expect(raw).not.toMatch(/gcm_sender_id|push|token|@/i)
  })
})

describe('m.html (the phone page)', () => {
  const html = read('m.html')
  it('links the manifest with credentials, the Home Screen icon, and the standalone status bar', () => {
    expect(html).toContain('<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials" />')
    expect(html).toContain('<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png" />')
    expect(html).toContain('<meta name="apple-mobile-web-app-capable" content="yes" />')
    expect(html).toContain('<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />')
    expect(html).toContain('viewport-fit=cover')
  })
})
