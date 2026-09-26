/**
 * Small formatting helpers shared by the Morning Read surface.
 *
 * They live in one file because each of them encodes a decision the reader is
 * not allowed to make twice — the severity re-key, what a "quiet" verify score
 * looks like, and the spelling of a count in a sentence. A second copy is a
 * second answer.
 */
import type { Severity } from '@/v4/world/types'

/**
 * Desk severities → CHANNEL A's five rungs. `elevated` and `moderate` are unit-
 * tier vocabulary the world ramp does not carry; this is the same re-key
 * `WorldAssessment` and `CountryUnitsAssessment` already do (UI_HOLISTIC_DESIGN
 * §5.4 #1), kept here so the read surface does not invent a sixth mapping.
 */
const SEVERITY_REKEY: Record<string, Severity> = {
  critical: 'critical',
  high: 'high',
  elevated: 'high',
  moderate: 'medium',
  medium: 'medium',
  low: 'low',
  info: 'info',
}

export function asSeverity(raw: string | null | undefined): Severity | null {
  if (!raw) return null
  return SEVERITY_REKEY[raw.toLowerCase()] ?? null
}

/** `2026-09-03 12:00Z` — UTC, so the stamp names a run the operator can look up. */
export function utcStamp(iso: string | null | undefined): string | null {
  if (!iso) return null
  const ms = Date.parse(iso)
  if (!Number.isFinite(ms)) return null
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
    `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}Z`
  )
}

/** `3 September 2026, 12:00 UTC` — the masthead's own longer form. */
export function longStamp(iso: string | null | undefined): string | null {
  if (!iso) return null
  const ms = Date.parse(iso)
  if (!Number.isFinite(ms)) return null
  const d = new Date(ms)
  const month = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
  ][d.getUTCMonth()]
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getUTCDate()} ${month} ${d.getUTCFullYear()}, ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`
}

/** The first eight characters of a uuid — how every other surface shows one. */
export function shortId(id: string): string {
  return id.slice(0, 8)
}

/** `7 sources` / `1 source` — a count that reads as English inside a sentence. */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

/** `0.36` — a score, or an em dash. Never `0.00` for "we did not measure". */
export function score(v: number | null | undefined, digits = 2): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—'
}

/**
 * Scroll an on-page anchor into view, if there is one and if the environment
 * implements it. jsdom does not (`Element.scrollIntoView` is unimplemented), and
 * an unhandled rejection from a scroll is a comically bad reason for a reading
 * surface to fall over.
 */
export function scrollToAnchor(id: string): void {
  if (typeof document === 'undefined') return
  const el = document.getElementById(id)
  if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center' })
}

/** `2.0 h` / `1.9 d` — an age the eye can size without arithmetic. */
export function age(hours: number | null | undefined): string | null {
  if (typeof hours !== 'number' || !Number.isFinite(hours)) return null
  if (hours < 48) return `${hours.toFixed(1)} h`
  return `${(hours / 24).toFixed(1)} d`
}
