// @vitest-environment node
/**
 * P-1 — the shell list the build writes into the worker, and its version.
 */
import { describe, expect, it } from 'vitest'
import { precacheList, renderServiceWorker, shellFiles, swVersion, type BundleLike } from './swBuild'

const meta = (css: string[] = [], assets: string[] = []) => ({ importedCss: new Set(css), importedAssets: new Set(assets) })

const BUNDLE: BundleLike = {
  'assets/index-a.js': {
    type: 'chunk', fileName: 'assets/index-a.js', isEntry: true,
    imports: ['assets/vendor-b.js'], viteMetadata: meta(['assets/index-a.css'], ['assets/font-x.woff2']),
  },
  'assets/vendor-b.js': { type: 'chunk', fileName: 'assets/vendor-b.js', imports: ['assets/index-a.js'], viteMetadata: meta() },
  // Lazy: reached only through a dynamic import — never in the shell.
  'assets/GlobeMap-c.js': { type: 'chunk', fileName: 'assets/GlobeMap-c.js', imports: ['assets/vendor-b.js'], viteMetadata: meta(['assets/GlobeMap-c.css']) },
  'assets/index-a.css': { type: 'asset', fileName: 'assets/index-a.css' },
  'assets/GlobeMap-c.css': { type: 'asset', fileName: 'assets/GlobeMap-c.css' },
  'index.html': { type: 'asset', fileName: 'index.html' },
}

describe('shellFiles', () => {
  it('is the entry, its static imports (cycles allowed), their CSS and assets — no lazy chunk', () => {
    expect(shellFiles(BUNDLE)).toEqual(['assets/font-x.woff2', 'assets/index-a.css', 'assets/index-a.js', 'assets/vendor-b.js'])
  })

  it('refuses a bundle without exactly one entry', () => {
    expect(() => shellFiles({ 'a.js': { type: 'chunk', fileName: 'a.js' } })).toThrow(/one entry/)
  })
})

describe('precacheList', () => {
  it('puts the page first, under the base, and skips the worker itself and source maps', () => {
    expect(precacheList('/m/', ['assets/index-a.js', 'assets/index-a.js.map'], ['icons/icon-192.png', 'manifest.webmanifest', 'sw.js'])).toEqual([
      '/m/index.html', '/m/assets/index-a.js', '/m/icons/icon-192.png', '/m/manifest.webmanifest',
    ])
  })

  it('throws before anything outside the shell could reach the worker', () => {
    expect(() => precacheList('/', ['api/v1/findings'], [])).toThrow(/cannot be stored/)
    expect(() => precacheList('/m/', ['../api/v1/x'], [])).toThrow(/cannot be stored/)
    expect(() => precacheList('/m/', ['index.html?x=1'], [])).toThrow(/cannot be stored/)
    expect(() => precacheList('m', [], [])).toThrow(/base/)
  })
})

describe('the version', () => {
  const list = ['/m/index.html']
  it('changes when any stored byte, the list, or the worker source changes', () => {
    const v = swVersion('tpl', list, ['<html>'])
    expect(v).toMatch(/^[0-9a-f]{16}$/)
    expect(swVersion('tpl', list, ['<html>'])).toBe(v)
    expect(swVersion('tpl', list, ['<html >'])).not.toBe(v)
    expect(swVersion('tpl', [...list, '/m/a.js'], ['<html>'])).not.toBe(v)
    expect(swVersion('tpl2', list, ['<html>'])).not.toBe(v)
  })

  it('renderServiceWorker fills both placeholders and refuses a template without them', () => {
    const tpl = "var VERSION = '__LEGBA_SW_VERSION__'\nvar PRECACHE = __LEGBA_SW_PRECACHE__\n"
    const out = renderServiceWorker(tpl, '0123456789abcdef', list)
    expect(out).toBe('var VERSION = "0123456789abcdef"\nvar PRECACHE = ["/m/index.html"]\n')
    expect(() => renderServiceWorker('nothing here', '0123456789abcdef', list)).toThrow(/placeholder/)
    expect(() => renderServiceWorker(tpl, 'not-hex!', list)).toThrow(/bad version/)
  })
})
