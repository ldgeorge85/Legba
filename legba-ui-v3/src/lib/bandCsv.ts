/**
 * bandCsv (7b-iii, lane k1) — the Eval Scorecard's banded per-desk section as
 * a CSV the operator can take away.
 *
 * Built ENTIRELY from the `CountryScorecard[]` the panel already holds (the
 * `/v3/eval/country_scorecard` read it renders from): no new route, no second
 * fetch, and therefore no way for the file to disagree with the page. One row
 * per (desk, dimension) — every dimension the card carries, banded or not,
 * because a scorecard whose export silently dropped its insufficient
 * dimensions would read as a fuller picture than the platform has.
 *
 * Honest cells, the panel's own rule carried onto paper:
 *   * `band` is the card's own band label, verbatim — `insufficient-evidence`
 *     included, never blanked and never recoloured into a severity;
 *   * `verified_basis_count` is `basis.length`, a REAL count of the verified
 *     sub-claims the band rests on. It is `0` only where the card genuinely
 *     records an empty basis (which is the definition of
 *     insufficient-evidence), never as a stand-in for an absent number;
 *   * every other absent value is an EMPTY cell. A card written before the
 *     H12 `method_version` stamp exports an empty `method_version`, not a
 *     guess and not a zero;
 *   * the two as-of columns are kept apart — `band_produced_at` is the
 *     dimension's own stamp and `card_generated_at` is the card's. Neither
 *     ever stands in for the other, because a silent fallback between two
 *     different as-ofs is exactly how a stale figure reads as fresh.
 *
 * RFC 4180: CRLF row terminators, `"` doubled inside a quoted field, and a
 * field quoted whenever it holds a quote, comma, CR/LF or edge whitespace.
 * UTF-8, no BOM (a BOM lands inside the first header cell for every parser
 * that does not special-case it).
 *
 * Pure and DOM-free — the download itself goes through `reportDownload
 * :downloadText`, the one Blob-anchor mechanism this app has.
 */
import { orderedDimensions, type CountryScorecard } from '@/lib/evalOps'
import { humanizeId } from '@/lib/deskNames'

/** The header row, in file order. */
export const BAND_CSV_COLUMNS = [
  'desk_id',
  'desk',
  'dimension',
  'band',
  'verified_basis_count',
  'band_produced_at',
  'card_generated_at',
  'method_version',
] as const

/** MIME the download is served under. */
export const BAND_CSV_MIME = 'text/csv;charset=utf-8'

/** One exported (desk, dimension) row. `null` renders as an EMPTY cell. */
export interface BandCsvRow {
  desk_id: string
  desk: string
  dimension: string
  band: string
  /** basis.length — a real count, `0` only where the basis is truly empty. */
  verified_basis_count: number
  band_produced_at: string | null
  card_generated_at: string | null
  method_version: string | null
}

/** Quote one field per RFC 4180. Absent (`null`/`undefined`) is an empty
 *  cell — never `0`, never the string "null". */
export function csvField(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return ''
  const s = String(value)
  if (s === '') return ''
  if (/[",\r\n]/.test(s) || s !== s.trim()) return `"${s.replace(/"/g, '""')}"`
  return s
}

/** Serialise a header + rows to an RFC 4180 document (CRLF terminated). */
export function toCsv(
  columns: readonly string[],
  rows: ReadonlyArray<ReadonlyArray<string | number | null | undefined>>,
): string {
  const lines = [columns.map(csvField).join(',')]
  for (const row of rows) lines.push(row.map(csvField).join(','))
  return lines.join('\r\n') + '\r\n'
}

/**
 * One row per (desk, dimension). Desks in the order the panel lists them
 * (target_id ascending) and dimensions in the panel's own display order, so
 * the file reads top-to-bottom exactly like the section it came from.
 */
export function bandCsvRows(cards: readonly CountryScorecard[]): BandCsvRow[] {
  const out: BandCsvRow[] = []
  const ordered = [...cards].sort((a, b) =>
    (a.target_id ?? '').localeCompare(b.target_id ?? ''),
  )
  for (const card of ordered) {
    for (const [unit, dim] of orderedDimensions(card.dimensions ?? {})) {
      out.push({
        desk_id: card.target_id,
        desk: humanizeId(card.target_id),
        dimension: unit,
        band: dim.band,
        verified_basis_count: (dim.basis ?? []).length,
        band_produced_at: dim.produced_at ?? null,
        card_generated_at: card.generated_at ?? null,
        method_version: card.method_version ?? null,
      })
    }
  }
  return out
}

/** `YYYYMMDD` from an ISO stamp, or null when it does not parse. */
function dayStamp(iso: string | null): string | null {
  if (!iso) return null
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso)
  return m ? `${m[1]}${m[2]}${m[3]}` : null
}

/** Filename-safe slug of a desk id. */
function slugDesk(id: string): string {
  return id.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
}

/**
 * `legba_bands_<desk-or-all>_<asof>.csv`. `<desk>` when the export covers
 * exactly one desk, `all` otherwise; `<asof>` is the NEWEST as-of any exported
 * row carries, and `undated` when not one row carries one — the absence is
 * named in the filename rather than stamped with today's date.
 */
export function bandCsvFilename(rows: readonly BandCsvRow[]): string {
  const desks = new Set(rows.map((r) => r.desk_id))
  const scope = desks.size === 1 ? slugDesk([...desks][0]) : 'all'
  const stamps = rows
    .flatMap((r) => [dayStamp(r.card_generated_at), dayStamp(r.band_produced_at)])
    .filter((s): s is string => s !== null)
    .sort()
  const asOf = stamps.length ? stamps[stamps.length - 1] : 'undated'
  return `legba_bands_${scope || 'all'}_${asOf}.csv`
}

/** The whole artifact: filename + RFC 4180 content, ready for `downloadText`. */
export function buildBandCsv(cards: readonly CountryScorecard[]): {
  filename: string
  content: string
} {
  const rows = bandCsvRows(cards)
  return {
    filename: bandCsvFilename(rows),
    content: toCsv(
      BAND_CSV_COLUMNS,
      rows.map((r) => BAND_CSV_COLUMNS.map((c) => r[c])),
    ),
  }
}
