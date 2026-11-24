/**
 * mobileCache — what the phone keeps on the device: NOTHING of the record.
 *
 * Until M-0a (2026-11-14) this module mirrored the navigator list and up to
 * eight full report bodies into `localStorage`, so a phone could show
 * yesterday's read in a tunnel. The phone plan (§5c) ends that on purpose: a
 * sensitive record does not belong in a phone's storage, where it outlives the
 * session, sign-out and the person who signed in. What the phone shows offline
 * is what it already loaded IN MEMORY this session, labelled with when it was
 * loaded — never a data archive on the device.
 *
 * So this module now does two things:
 *
 *   1. `purgeStoredReads()` deletes what earlier builds wrote (the list key and
 *      every `legba_mobile_report_v1:*` body), once per page load, so a phone
 *      that ran the old build stops carrying the record.
 *   2. `loadedAtLabel()` words the honest offline line: "No connection.
 *      Showing what was loaded at 14:52 UTC."
 *
 * Every accessor is total: storage can be disabled (private mode) or full.
 */

const LEGACY_LIST_KEY = 'legba_mobile_reports_v1'
const LEGACY_REPORT_PREFIX = 'legba_mobile_report_v1:'

/** Delete every read an earlier build stored on this device. Returns how many keys went. */
export function purgeStoredReads(): number {
  let removed = 0
  try {
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i)
      if (k && (k === LEGACY_LIST_KEY || k.startsWith(LEGACY_REPORT_PREFIX))) doomed.push(k)
    }
    for (const k of doomed) {
      localStorage.removeItem(k)
      removed += 1
    }
  } catch {
    // Storage disabled: there is nothing stored to remove.
  }
  return removed
}

/** True when this device still holds a read an earlier build stored (test seam). */
export function hasStoredReads(): boolean {
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i)
      if (k && (k === LEGACY_LIST_KEY || k.startsWith(LEGACY_REPORT_PREFIX))) return true
    }
  } catch {
    return false
  }
  return false
}

/** The legacy key names, for the test that proves they are purged. */
export const __LEGACY_KEYS = { list: LEGACY_LIST_KEY, reportPrefix: LEGACY_REPORT_PREFIX }

/**
 * "No connection. Showing what was loaded at 07:41 UTC." — the line that must
 * accompany a screen drawn from what was loaded earlier in this session.
 *
 * Deliberately UTC and explicit: a bare "07:41" read across time zones is the
 * ambiguity that makes an old read look current.
 */
export function loadedAtLabel(loadedAtMs: number | null | undefined): string {
  if (!loadedAtMs || !Number.isFinite(loadedAtMs)) return 'No connection.'
  const d = new Date(loadedAtMs)
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  return `No connection. Showing what was loaded at ${hh}:${mm} UTC.`
}
