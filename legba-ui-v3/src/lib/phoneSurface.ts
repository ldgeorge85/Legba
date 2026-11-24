/**
 * phoneSurface — a phone gets the phone shell (X4/D4; M-2, plan §2.6, §3.5).
 *
 * THE RULE (M-2). Go to `/m/` when the SHORTER SIDE of the screen is at most
 * {@link PHONE_MAX_WIDTH} px (a phone, portrait or landscape), or when the
 * pointer is coarse and the window is narrower than {@link TOUCH_MAX_WIDTH}
 * px (a small tablet). A narrow desktop window with a mouse stays on the
 * desktop. Until M-2 the rule was the window's width alone, so a phone held
 * sideways got the squeezed desk.
 *
 * WHERE IT RUNS. The decision is made by a few lines inlined at the top of
 * the desktop's `index.html`, BEFORE the desktop's code is requested: the
 * desktop build moves its entry script, stylesheet and preloads into a loader
 * that runs only when the page stays (`vite.config.ts::deferEntryForPhones`).
 * So a phone never downloads the desktop's 400 kB entry to be sent away.
 * That inline script is this module's rule written out by hand; a test runs
 * it against {@link phoneRedirectTarget} over a table of cases
 * (`phoneSurface.test.ts`), so the two cannot drift. `redirectPhoneToMobile`
 * (called from `src/main.tsx`) remains as the backstop for a page served
 * without the inline script (the dev server).
 *
 * THE ADDRESS IS KEPT WHOLE: the query and the hash ride to `/m/`, so a link
 * to a record (`#rec=`), a point (`#sel=`), a window or an invite opens the
 * same thing on the phone. `?desktop=1` (and the "Desktop view" choice,
 * remembered for the tab in `sessionStorage`) keeps the desk.
 */

export const PHONE_MAX_WIDTH = 480
/** Below this width a COARSE pointer (a touch screen) gets the phone shell. */
export const TOUCH_MAX_WIDTH = 900
export const DESKTOP_OPT_KEY = 'legba_desktop'
const MOBILE_BASE = '/m/'

export interface PhoneAddress {
  /** `window.innerWidth` — the layout viewport, in CSS px. */
  width: number
  /** The shorter side of `window.screen`, CSS px (0 when unknown). */
  screenShort?: number
  /** `(pointer: coarse)` matches. */
  coarse?: boolean
  pathname: string
  search: string
  hash: string
  /** The tab already chose the desk (sessionStorage). */
  desktopChosen: boolean
}

/** `?desktop=1` on the address: the reader asks for the desk. */
export function asksForDesktop(search: string): boolean {
  return new URLSearchParams(search).get('desktop') === '1'
}

/** Is this a phone-shaped screen (the rule above)? */
export function isPhoneLike(a: Pick<PhoneAddress, 'width' | 'screenShort' | 'coarse'>): boolean {
  if (!(a.width > 0)) return false
  if (a.screenShort !== undefined && a.screenShort > 0) {
    if (a.screenShort <= PHONE_MAX_WIDTH) return true
    return !!a.coarse && a.width < TOUCH_MAX_WIDTH
  }
  // no screen measure (a test, an odd embed): the window's width, as before M-2
  return a.width <= PHONE_MAX_WIDTH || (!!a.coarse && a.width < TOUCH_MAX_WIDTH)
}

/**
 * Where a phone on the workstation goes: the `/m/` URL with the WHOLE query
 * and hash, or null to stay.
 */
export function phoneRedirectTarget(a: PhoneAddress): string | null {
  if (!isPhoneLike(a)) return null
  if (a.pathname === '/m' || a.pathname.startsWith('/m/')) return null
  if (a.desktopChosen || asksForDesktop(a.search)) return null
  return MOBILE_BASE + a.search + a.hash
}

function readChosen(): boolean {
  try {
    return window.sessionStorage.getItem(DESKTOP_OPT_KEY) === '1'
  } catch {
    return false
  }
}

function screenShort(): number {
  try {
    const s = window.screen
    return s && s.width > 0 && s.height > 0 ? Math.min(s.width, s.height) : 0
  } catch {
    return 0
  }
}

function coarse(): boolean {
  try {
    return typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches
  } catch {
    return false
  }
}

/** Run once at boot. Returns true when it navigated (the caller renders nothing). */
export function redirectPhoneToMobile(): boolean {
  try {
    if (asksForDesktop(window.location.search)) {
      try {
        window.sessionStorage.setItem(DESKTOP_OPT_KEY, '1')
      } catch {
        // the choice just does not outlive this load
      }
    }
    const target = phoneRedirectTarget({
      width: window.innerWidth,
      screenShort: screenShort(),
      coarse: coarse(),
      pathname: window.location.pathname,
      search: window.location.search,
      hash: window.location.hash,
      desktopChosen: readChosen(),
    })
    if (!target) return false
    window.location.replace(target)
    return true
  } catch {
    return false
  }
}
