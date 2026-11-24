/**
 * X4/D4 — a phone gets the phone surface. The decision at 390 / 1366 / 1680.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  asksForDesktop,
  DESKTOP_OPT_KEY,
  PHONE_MAX_WIDTH,
  phoneRedirectTarget,
  redirectPhoneToMobile,
  type PhoneAddress,
} from './phoneSurface'

const at = (width: number, over: Partial<PhoneAddress> = {}): PhoneAddress => ({
  width,
  pathname: '/',
  search: '',
  hash: '',
  desktopChosen: false,
  ...over,
})

describe('phoneRedirectTarget', () => {
  it('390 px (a phone) on the workstation goes to /m/', () => {
    expect(phoneRedirectTarget(at(390))).toBe('/m/')
  })

  it('the threshold is 480 px: 480 redirects, 481 does not', () => {
    expect(PHONE_MAX_WIDTH).toBe(480)
    expect(phoneRedirectTarget(at(480))).toBe('/m/')
    expect(phoneRedirectTarget(at(481))).toBeNull()
  })

  it('1366 and 1680 px stay on the workstation', () => {
    expect(phoneRedirectTarget(at(1366))).toBeNull()
    expect(phoneRedirectTarget(at(1680))).toBeNull()
  })

  it('an unlaid-out window (width 0) never redirects', () => {
    expect(phoneRedirectTarget(at(0))).toBeNull()
  })

  it('already under /m never loops', () => {
    expect(phoneRedirectTarget(at(390, { pathname: '/m/' }))).toBeNull()
    expect(phoneRedirectTarget(at(390, { pathname: '/m' }))).toBeNull()
    expect(phoneRedirectTarget(at(390, { pathname: '/m/index.html' }))).toBeNull()
  })

  it('?desktop=1 and a chosen desk stay put', () => {
    expect(asksForDesktop('?desktop=1')).toBe(true)
    expect(asksForDesktop('?desktop=0')).toBe(false)
    expect(phoneRedirectTarget(at(390, { search: '?desktop=1' }))).toBeNull()
    expect(phoneRedirectTarget(at(390, { desktopChosen: true }))).toBeNull()
  })

  it('the WHOLE address rides to the phone (M-2): an invite, #signin, a record, a point, the query', () => {
    expect(phoneRedirectTarget(at(390, { hash: '#invite=abc123' }))).toBe('/m/#invite=abc123')
    expect(phoneRedirectTarget(at(390, { hash: '#signin' }))).toBe('/m/#signin')
    expect(phoneRedirectTarget(at(390, { hash: '#ws=desk&sel=target:x' }))).toBe('/m/#ws=desk&sel=target:x')
    expect(phoneRedirectTarget(at(390, { hash: '#rec=finding:ex_1&cite=2' }))).toBe('/m/#rec=finding:ex_1&cite=2')
    expect(phoneRedirectTarget(at(390, { search: '?case=s:1', hash: '#win=7d' }))).toBe('/m/?case=s:1#win=7d')
  })

  it('the shorter side of the screen decides (M-2): a landscape phone goes, a narrow desktop window with a mouse stays', () => {
    // a phone held sideways: 844 wide, its screen's short side 390
    expect(phoneRedirectTarget(at(844, { screenShort: 390 }))).toBe('/m/')
    // a desktop window dragged narrow: 420 wide on a 1080 screen, mouse
    expect(phoneRedirectTarget(at(420, { screenShort: 1080, coarse: false }))).toBeNull()
    // a small tablet (touch) under 900 px goes; a wide touch screen stays
    expect(phoneRedirectTarget(at(820, { screenShort: 820, coarse: true }))).toBe('/m/')
    expect(phoneRedirectTarget(at(1180, { screenShort: 820, coarse: true }))).toBeNull()
    expect(phoneRedirectTarget(at(1366, { screenShort: 768, coarse: false }))).toBeNull()
  })
})

describe("index.html's inline redirect is this module's rule (M-2)", () => {
  const html = readFileSync(join(__dirname, '..', '..', 'index.html'), 'utf8')
  const body = /<script id="legba-phone-redirect">([\s\S]*?)<\/script>/.exec(html)?.[1] ?? ''

  function runInline(a: PhoneAddress): string | null {
    let replaced: string | null = null
    const store = new Map<string, string>(a.desktopChosen ? [[DESKTOP_OPT_KEY, '1']] : [])
    const fakeSession = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) }
    const fakeWindow = {
      location: { pathname: a.pathname, search: a.search, hash: a.hash, replace: (u: string) => (replaced = u) },
      innerWidth: a.width,
      screen: { width: a.screenShort ?? 0, height: a.screenShort ? 2000 : 0 },
      matchMedia: () => ({ matches: !!a.coarse }),
    }
    new Function('window', 'sessionStorage', 'URLSearchParams', body)(fakeWindow, fakeSession, URLSearchParams)
    return replaced
  }

  it('is present, before any module script', () => {
    expect(body.length).toBeGreaterThan(100)
    expect(html.indexOf('legba-phone-redirect')).toBeLessThan(html.indexOf('type="module"'))
  })

  const cases: Array<[string, PhoneAddress]> = [
    ['portrait phone', at(390, { screenShort: 390 })],
    ['landscape phone', at(844, { screenShort: 390 })],
    ['narrow desk window', at(420, { screenShort: 1080 })],
    ['touch tablet', at(820, { screenShort: 820, coarse: true })],
    ['wide touch', at(1180, { screenShort: 820, coarse: true })],
    ['desk', at(1680, { screenShort: 1050 })],
    ['no screen measure, narrow', at(390)],
    ['already on /m', at(390, { screenShort: 390, pathname: '/m/' })],
    ['?desktop=1', at(390, { screenShort: 390, search: '?desktop=1' })],
    ['desk chosen', at(390, { screenShort: 390, desktopChosen: true })],
    ['a record link', at(390, { screenShort: 390, hash: '#rec=finding:ex_1&cite=3' })],
    ['query and hash', at(390, { screenShort: 390, search: '?case=s:1', hash: '#win=7d' })],
  ]
  for (const [name, a] of cases) {
    it(`agrees on: ${name}`, () => {
      expect(runInline(a)).toBe(phoneRedirectTarget(a))
    })
  }
})

describe('redirectPhoneToMobile (the effectful call)', () => {
  const realLocation = window.location
  let replaced: string | null = null

  function stubWindow(width: number, search = '') {
    Object.defineProperty(window, 'innerWidth', { value: width, configurable: true })
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: {
        pathname: '/',
        search,
        hash: '',
        replace: (u: string) => {
          replaced = u
        },
      },
    })
  }

  beforeEach(() => {
    replaced = null
    sessionStorage.clear()
  })
  afterEach(() => {
    Object.defineProperty(window, 'location', { configurable: true, value: realLocation })
  })

  it('navigates a phone and reports it', () => {
    stubWindow(390)
    expect(redirectPhoneToMobile()).toBe(true)
    expect(replaced).toBe('/m/')
  })

  it('?desktop=1 is remembered for the tab, so a reload does not bounce back', () => {
    stubWindow(390, '?desktop=1')
    expect(redirectPhoneToMobile()).toBe(false)
    expect(sessionStorage.getItem(DESKTOP_OPT_KEY)).toBe('1')
    stubWindow(390, '')
    expect(redirectPhoneToMobile()).toBe(false)
    expect(replaced).toBeNull()
  })

  it('a desktop-width window is never navigated', () => {
    stubWindow(1366)
    expect(redirectPhoneToMobile()).toBe(false)
    expect(replaced).toBeNull()
  })
})
