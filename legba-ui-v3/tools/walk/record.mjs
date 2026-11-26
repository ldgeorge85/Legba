/**
 * The Record sheet's address, as the walk opens it when no Board row can be
 * clicked (live only).
 *
 * When no change row takes the click, setting
 * `location.hash = 'rec=finding:<id>'` cannot open the sheet: the shell reads a
 * `#rec=` address ONCE, when it loads (`RecordSheet.tsx::useRecordLink`),
 * never on a hash change — and it also wipes the frame's own params (`ws=`,
 * `scope=`). So the address is written beside the frame's params and the page
 * RELOADED, as a shared link is opened.
 */

/** `hash` (with or without its `#`) with `rec=<kind>:<id>` set, every other param kept. */
export function withRecordAddress(hash, kind, id) {
  const p = new URLSearchParams(String(hash ?? '').replace(/^#/, ''))
  p.set('rec', `${kind}:${id}`)
  p.delete('cite')
  return '#' + p.toString()
}
