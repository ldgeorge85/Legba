/**
 * analystNames — humanize a raw analyst/unit id for READER-FACING prose (U-5).
 *
 * A hostile UX review found finding/desk-card prose showing raw pipeline ids
 * as if they were English: "the economic_coercion unit reports…",
 * "Energy_security analysts note…". This is the ONE shared formatter for that
 * — snake_case (or dotted / mixed) → a sentence-case phrase a reader can
 * parse without knowing the codebase: `economic_coercion` → `Economic
 * coercion`. Pure, DOM-free, reused wherever an analyst id surfaces in
 * reader-facing text (never in Engine Room / plumbing tables, which keep the
 * raw id on purpose for operators).
 *
 * NEVER touches backend text — this only reformats an id token the CLIENT
 * already has (e.g. `finding.analyst_id`), at render time.
 */

/** Humanize a raw analyst/unit id into reader-facing prose. Strips a leading
 *  `analyst_` plumbing prefix, splits on `_`/`.`/`-`, lowercases, and
 *  capitalizes only the first character (sentence case — safe to embed
 *  mid-sentence without shouting every word). `null`/`undefined`/empty →
 *  the honest `fallback` (default `'unknown analyst'`), never a fabricated
 *  name. */
export function humanizeAnalystId(
  id: string | null | undefined,
  fallback = 'unknown analyst',
): string {
  if (!id || !id.trim()) return fallback
  const stripped = id.replace(/^analyst_/i, '')
  const words = stripped
    .replace(/[._-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase()
  if (!words) return fallback
  return words.charAt(0).toUpperCase() + words.slice(1)
}
