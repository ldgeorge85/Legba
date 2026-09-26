/**
 * mobileCache — the last-known-good read, kept on the phone.
 *
 * A phone loses the network in ways a desk browser does not: a lift, a tunnel,
 * a train. The workstation can afford to render an error state because the
 * operator is sitting in front of a working connection; a phone cannot, and a
 * Morning Read that is unreadable on the platform at 07:40 is a Morning Read
 * that does not get read.
 *
 * So both the navigator list and the last opened report are mirrored into
 * `localStorage` on every successful fetch, and served from there when the API
 * fails. The cache ALWAYS carries the time it was written, and the UI always
 * shows it — a stale read presented as live would corrupt the very measurement
 * the read telemetry exists to take.
 *
 * Every accessor is total: storage can be full, disabled (private mode), or
 * carrying a payload written by an older build. All three are treated the same
 * way — the cache reports a miss and the caller falls back to the network.
 * Nothing here ever throws.
 */

import type { ReadFindingRow } from '@/lib/assemblyModel'
import type { ReportRow } from './mobileModel'

const LIST_KEY = 'legba_mobile_reports_v1'
const REPORT_PREFIX = 'legba_mobile_report_v1:'

/** Bump when a cached shape changes; an older `v` is discarded, never coerced. */
const CACHE_VERSION = 1

/** How many report bodies to keep. Each is tens of KB; a phone quota is ~5 MB. */
export const MAX_CACHED_REPORTS = 8

export interface CachedList {
  v: number
  savedAt: string
  rows: ReportRow[]
}

export interface CachedReport {
  v: number
  savedAt: string
  row: ReadFindingRow
}

function read<T extends { v: number }>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as T
    if (!parsed || typeof parsed !== 'object' || parsed.v !== CACHE_VERSION) return null
    return parsed
  } catch {
    return null
  }
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // Quota exceeded / storage disabled. Evicting the oldest report body is
    // worth one retry: the list is the cheaper and more valuable of the two.
    try {
      evictOldestReport()
      localStorage.setItem(key, JSON.stringify(value))
    } catch {
      // Genuinely no room. The surface still works online; nothing is lost
      // except the offline fallback, so this stays silent.
    }
  }
}

function reportKeys(): string[] {
  const keys: string[] = []
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i)
      if (k && k.startsWith(REPORT_PREFIX)) keys.push(k)
    }
  } catch {
    return []
  }
  return keys
}

function evictOldestReport(): void {
  const entries = reportKeys()
    .map((k) => ({ k, at: read<CachedReport>(k)?.savedAt ?? '' }))
    .sort((a, b) => a.at.localeCompare(b.at))
  const oldest = entries[0]
  if (oldest) {
    try {
      localStorage.removeItem(oldest.k)
    } catch {
      /* nothing further to try */
    }
  }
}

// ── the navigator list ───────────────────────────────────────────────────────

export function saveReportsList(rows: ReportRow[]): void {
  write(LIST_KEY, { v: CACHE_VERSION, savedAt: new Date().toISOString(), rows })
}

export function loadReportsList(): CachedList | null {
  const hit = read<CachedList>(LIST_KEY)
  return hit && Array.isArray(hit.rows) ? hit : null
}

// ── one report body ──────────────────────────────────────────────────────────

export function saveReport(row: ReadFindingRow): void {
  if (!row?.id) return
  const existing = reportKeys()
  if (existing.length >= MAX_CACHED_REPORTS && !existing.includes(REPORT_PREFIX + row.id)) {
    evictOldestReport()
  }
  write(REPORT_PREFIX + row.id, { v: CACHE_VERSION, savedAt: new Date().toISOString(), row })
}

export function loadReport(id: string): CachedReport | null {
  if (!id) return null
  const hit = read<CachedReport>(REPORT_PREFIX + id)
  return hit && hit.row && typeof hit.row === 'object' ? hit : null
}

/** Test seam — drop every mobile cache entry. */
export function __clearMobileCache(): void {
  try {
    localStorage.removeItem(LIST_KEY)
    for (const k of reportKeys()) localStorage.removeItem(k)
  } catch {
    /* nothing to clear */
  }
}

/**
 * "Cached at 07:41 UTC" — the line that must accompany every cached render.
 *
 * Deliberately UTC and deliberately explicit: the operator reads across time
 * zones and a bare "07:41" would be the kind of ambiguity that makes a stale
 * read look current.
 */
export function cachedAtLabel(savedAt: string | null | undefined): string {
  if (!savedAt) return 'cached'
  const d = new Date(savedAt)
  if (Number.isNaN(d.getTime())) return 'cached'
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  return `cached ${d.toISOString().slice(0, 10)} ${hh}:${mm} UTC`
}
