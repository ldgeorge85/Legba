/**
 * P-1 — what the phone app drops when its sign-in session ends by sign-out.
 *
 * The phone's service worker stores only the app's shell (code, icons — no
 * record, no answer; `src/mobile/pwa/sw.template.js`), but a signed-out
 * device should keep nothing of the session, so sign-out on the phone:
 *
 *   1. asks the server for `Clear-Site-Data: "storage"` (the
 *      `X-Legba-Clear-Device: phone` header on the sign-out request — the
 *      registry sends it only when asked, so a desktop sign-out never wipes the
 *      desktop's saved layouts);
 *   2. tells the worker to drop its caches (`LEGBA_SW_CLEAR`), and deletes
 *      them from the page as well, in case the worker is not running;
 *   3. unregisters the worker. The next open of `/m/` registers it again.
 *
 * Every step is best-effort and never throws: sign-out must complete even
 * where a step is unsupported (the browser, a private window).
 */

export const CLEAR_DEVICE_HEADER = 'X-Legba-Clear-Device'
export const PHONE_CACHE_PREFIX = 'legba-m-shell-'

/** True on the phone surface (`/m`, `/m/…`), where the worker lives. */
export function onPhoneSurface(pathname: string = window.location.pathname): boolean {
  return pathname === '/m' || pathname.startsWith('/m/')
}

export async function clearPhoneDevice(): Promise<void> {
  try {
    const sw = typeof navigator !== 'undefined' ? navigator.serviceWorker : undefined
    const reg = sw ? await sw.getRegistration('/m/') : undefined
    for (const w of [reg?.active, reg?.waiting, reg?.installing]) {
      w?.postMessage({ type: 'LEGBA_SW_CLEAR' })
    }
    await reg?.unregister()
  } catch {
    /* no worker here */
  }
  try {
    if (typeof caches !== 'undefined') {
      for (const key of await caches.keys()) {
        if (key.startsWith(PHONE_CACHE_PREFIX)) await caches.delete(key)
      }
    }
  } catch {
    /* Cache Storage unavailable */
  }
}
