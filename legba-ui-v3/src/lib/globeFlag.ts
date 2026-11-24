/**
 * The globe flag (U-4) — the map showpiece on maplibre-gl 5, behind a switch
 * that can turn it off without a code change.
 *
 *   ON  — the reader surfaces' map is the globe map (`v4/globe/`): a globe at
 *         world zoom flattening on zoom-in on the Board, a flat inset on
 *         Subject, layers built from the platform's objects, and a drawn SVG
 *         fallback where WebGL2 is missing.
 *   OFF — the map is the earlier flat World map (`v4/world/MapLibreWorldMap`),
 *         now running on maplibre-gl 5 in Mercator, with the same drawn
 *         fallback where WebGL2 is missing.
 *
 * Precedence: `?globe=0|1` in the URL, then `localStorage.legba_globe`
 * (`'0'`/`'1'`), then the build default `VITE_LEGBA_GLOBE` (`'0'` turns it
 * off at build time), then ON.
 */
export function globeEnabled(): boolean {
  try {
    const url = new URLSearchParams(window.location.search).get('globe')
    if (url === '0' || url === '1') return url === '1'
    const ls = localStorage.getItem('legba_globe')
    if (ls === '0' || ls === '1') return ls === '1'
  } catch {
    // no URL or storage (a test, a private window): the build default
  }
  return import.meta.env.VITE_LEGBA_GLOBE !== '0'
}
