/**
 * mobileModel — the pure read-model layer behind the phone surface.
 *
 * THE CROSSROADS RULE (UI_V2 §"Selection Store"): one click filters every
 * panel. On a phone there are no panels, so the same rule lands as: one
 * selected report is the context for every section below it, and one selected
 * entity narrows all three sections at once. Everything on this page is
 * therefore derived from ONE selected row — never independently fetched, never
 * independently filtered.
 *
 * ── WHY NO EXTRA ENDPOINTS ────────────────────────────────────────────────
 * The `assembly.v1` payload already carries its own lineage. Each block IS a
 * cited head (`finding_id`, `desk`, `target_id`, `severity`, `verify`) and
 * carries the signals it cited (`marker`, `signal_id`, `title`, `url`,
 * `age_h`). So the Findings and Signals sections are PROJECTIONS of the report
 * the operator already downloaded, not second round trips. That is why opening
 * a report on a phone costs exactly one request, which is the whole point on a
 * cellular connection.
 *
 * The one thing the payload does NOT carry is a substrate entity list — see
 * `reportEntities`, which is honest about deriving from the blocks' targets
 * rather than inventing an entity resolution the API cannot do (there is no
 * server-side filter from a read to its entities; documented in the build
 * report, deliberately not stubbed).
 *
 * Everything here is pure and DOM-free so the selection semantics can be
 * tested without mounting React.
 */

import type {
  AssemblyBlock,
  AssemblyPayload,
  AssemblySignal,
  ReadFindingRow,
} from '@/lib/assemblyModel'
import {
  PLATFORM_NOUNS,
  readTiers,
  tierForProducer,
  untargetedSubject,
  type ProfileFrame,
} from '@/lib/profileUi'
import { humanizeId } from '@/lib/deskNames'
import { feedPreview } from '@/lib/proseText'

/** Names a subject id in reader words, or null when it cannot. */
export type SubjectNamer = (id: string) => string | null

/**
 * U-1 — a subject's reader name: the namer's (the subject roster) when it
 * knows the id, else {@link humanizeId} (the declared name, else the id
 * de-underscored and title-cased). A machine id is never returned as-is.
 */
export function readerSubjectName(id: string, nameOf?: SubjectNamer): string {
  return nameOf?.(id) ?? humanizeId(id)
}

const MACHINE_ID_RE = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/

/**
 * U-1 — a block's or finding's subject in reader words. Some stored blocks
 * carry the machine id in their `target_name` field, so a name that is itself
 * id-shaped is named like an id; a real name passes through.
 */
export function readerBlockName(name: string | null | undefined, id: string | null | undefined, nameOf?: SubjectNamer): string {
  if (name && !MACHINE_ID_RE.test(name)) return name
  const key = id ?? name
  return key ? readerSubjectName(key, nameOf) : ''
}

/** A snake_case machine token (`military_posture`, `country_g20_sa`). */
const MACHINE_TOKEN_RE = /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g

/**
 * U-1 — a report's title in reader words. Some read tiers title their rows
 * with the subject's machine id and the units' ids ("<subject_id>, <date> —
 * 3 desks: unit_a, unit_b"); the subject id becomes its name and any other
 * snake_case token reads as words. A title with no machine token is unchanged.
 */
export function readerReportTitle(
  title: string,
  subjectId: string | null,
  nameOf?: SubjectNamer,
): string {
  let t = title
  if (subjectId) t = t.split(subjectId).join(readerSubjectName(subjectId, nameOf))
  return t.replace(MACHINE_TOKEN_RE, (tok) => nameOf?.(tok) ?? tok.replace(/_/g, ' '))
}

// ── the read producers ───────────────────────────────────────────────────────

/**
 * The read tiers — the PROFILE's frames that carry a producer (UI-B2; the
 * profile's `ui.frames`, or the projection's `tiers` section when the plane
 * answers — `lib/profileUi.ts::useReadTiers`). Top floor first, then the
 * manifest's order: the way the operator reads down the page, widest first.
 *
 * Each tier's producer is the substrate's own analyst id, and it is the ONLY
 * tier discriminator the findings row carries — there is no `tier` column on
 * `/findings`, so the analyst id is what tells one tier's read from another's.
 * No tier, label or producer is spelled here.
 */
export const PROFILE_TIERS: readonly ProfileFrame[] = readTiers()

/** A read tier's id (a profile frame id). */
export type ReportTier = string

/** The CSV the Reports list sends as `analyst_id_in` — one request, all tiers. */
export function readAnalystCsv(tiers: readonly ProfileFrame[] = PROFILE_TIERS): string {
  return tiers.map((t) => t.producer).join(',')
}

export const READ_ANALYST_CSV = readAnalystCsv()

/** A tier's label, in the profile's words (an unknown tier reads as its id). */
export function tierLabel(tier: ReportTier, tiers: readonly ProfileFrame[] = PROFILE_TIERS): string {
  return tiers.find((t) => t.id === tier)?.label ?? tier
}

export function tierOf(row: ReadFindingRow, tiers: readonly ProfileFrame[] = PROFILE_TIERS): ReportTier | null {
  return tierForProducer(row.analyst_id, tiers)?.id ?? null
}

/** What a read with no target is about (`untargetedSubject`), or the platform noun off the read set. */
export function untargetedReadSubject(
  tier: ReportTier | null,
  tiers: readonly ProfileFrame[] = PROFILE_TIERS,
): string {
  const frame = tier ? tiers.find((t) => t.id === tier) : undefined
  return frame ? untargetedSubject(frame, tiers) : PLATFORM_NOUNS.read
}

// ── the list row ─────────────────────────────────────────────────────────────

/**
 * One row of the Reports navigator.
 *
 * `leadThread` is the sentence the reader scans. M-0a — the navigator is built
 * from the `fields=summary` projection (no blocks, no body), so for a summary
 * row the lead is the read's own title past its "<subject>, <date> —" prefix.
 * The verify number left the card with the full-weight read: an unexplained
 * 0.60 pill is an engine figure, not a reader's word.
 */
export interface ReportRow {
  id: string
  tier: ReportTier
  /** The producing analyst — with the subject and instant, what re-finds the full row on open. */
  analystId: string | null
  /** The read's subject: a country/region name where the row names one. */
  target: string
  targetId: string | null
  /** ISO produced_at, verbatim off the row. */
  producedAt: string
  /** UTC calendar day, `YYYY-MM-DD` — the date-navigation key. */
  day: string
  title: string
  /** The one-line thread; empty string when the row carries none. */
  leadThread: string
  /** Which region a country read belongs under, when derivable. */
  regionHint: string | null
}

/** `YYYY-MM-DD` in UTC. Tolerates the substrate's space-separated stamps. */
export function dayKey(iso: string | null | undefined): string {
  if (!iso) return ''
  const normalized = iso.includes('T') ? iso : iso.replace(' ', 'T')
  const d = new Date(normalized)
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10)
  return d.toISOString().slice(0, 10)
}

/**
 * The lead thread — the sentence the list row shows.
 *
 * Preference order, each a REAL field on the record rather than a summary we
 * compose: the lead block's BLUF span (the record's own published lead), then
 * any first BLUF span, then the assessment body's first sentence, then the
 * title. Never fabricated: an empty string when the row genuinely carries no
 * prose, and the list renders the honest dash.
 */
export function leadThread(row: ReadFindingRow, assembly: AssemblyPayload | null): string {
  if (assembly) {
    const leadOrdinals = assembly.lead?.block_ordinals ?? []
    const ordered = [
      ...assembly.blocks.filter((b) => leadOrdinals.includes(b.ordinal)),
      ...assembly.blocks.filter((b) => !leadOrdinals.includes(b.ordinal)),
    ]
    for (const b of ordered) {
      const bluf = b.spans.find((s) => s.role === 'bluf') ?? b.spans[0]
      // U-1 — a list line is plain prose: no `**`, `##` or `[N]` noise.
      if (bluf?.text) return feedPreview(bluf.text)
    }
  }
  const body = typeof row.body === 'string' ? row.body.trim() : ''
  if (body) return bodyLead(body)
  return titleLead(row.title)
}

/**
 * "<subject>, <date> — " / "<subject> read · 3 Sep — ": the prefix a read's
 * title opens with before its own headline. Anchored on the DATE, so a
 * subject whose name carries a dash ("Region — Africa, 2026-11-13 — …") is
 * cut at the right one.
 */
const TITLE_PREFIX_RE = /^.*?(?:\d{4}-\d{2}-\d{2}|\b\d{1,2} [A-Z][a-z]{2})\s+—\s+/

/**
 * M-0a — the lead of a SUMMARY row (`fields=summary` carries no blocks and no
 * body): the title's headline past its subject-and-date prefix, the quotes it
 * is wrapped in dropped, in plain words. A title with no prefix is its own
 * headline (an Assessment's).
 */
export function titleLead(title: string | null | undefined): string {
  if (!title) return ''
  const rest = title.replace(TITLE_PREFIX_RE, '').trim()
  const unquoted = rest.replace(/^[“"]/, '').replace(/[”"]$/, '')
  return feedPreview(readerReportTitle(unquoted, null))
}

/**
 * The lead sentence of a markdown body (the assessment channel's shape).
 *
 * The body opens with an italic as-of slice, then a `**BLUF:**` line, then
 * `##` sections. The thread the operator wants is the BLUF — which a naive
 * "first line that isn't italic" rule SKIPS, because `**BLUF:**` also starts
 * with an asterisk, landing on the `## The picture` heading instead. So the
 * BLUF is sought explicitly, headings are never eligible, and the emphasis
 * markers are stripped so a navigator row shows prose rather than syntax.
 */
function bodyLead(body: string): string {
  const lines = body.split('\n').map((l) => l.trim())
  const isAsOf = (l: string) => /^\*[^*].*\*$/.test(l)
  const isHeading = (l: string) => l.startsWith('#')
  const strip = (l: string) => feedPreview(l.replace(/\*\*/g, '').replace(/^BLUF:\s*/i, ''))

  const bluf = lines.find((l) => /^\*\*BLUF:?\*\*/i.test(l))
  if (bluf) return strip(bluf)

  const first = lines.find((l) => l && !isAsOf(l) && !isHeading(l))
  return first ? strip(first) : strip(lines[0] ?? '')
}

/**
 * Project a `/findings` row into a navigator row.
 *
 * Returns `null` for a row produced by an analyst outside the read set, so a
 * widened query can never smuggle a non-read into the Reports list.
 */
export function toReportRow(
  row: ReadFindingRow,
  assembly: AssemblyPayload | null = null,
  tiers: readonly ProfileFrame[] = PROFILE_TIERS,
  nameOf?: SubjectNamer,
): ReportRow | null {
  const tier = tierOf(row, tiers)
  if (!tier) return null
  return {
    id: row.id,
    tier,
    analystId: row.analyst_id ?? null,
    target: readSubjectTitle(row, assembly, tier, tiers, nameOf),
    targetId: row.target_id ?? null,
    producedAt: row.produced_at,
    day: dayKey(row.produced_at),
    title: row.title ?? '',
    leadThread: leadThread(row, assembly),
    regionHint: null,
  }
}

/**
 * U-1 / M-0a — what a read's card is titled: the subject the READ is about.
 *
 * A targeted read is its target's name (the block that speaks about that
 * target names it best; the roster names it otherwise). An untargeted read —
 * the World read, a thematic read — is named for what it covers, never for the
 * first block it quotes: titling the World read with its first block's country
 * was the 2026-11-14 phone audit's finding.
 */
export function readSubjectTitle(
  row: ReadFindingRow,
  assembly: AssemblyPayload | null,
  tier: ReportTier | null,
  tiers: readonly ProfileFrame[] = PROFILE_TIERS,
  nameOf?: SubjectNamer,
): string {
  const id = row.target_id ?? null
  if (!id) return untargetedReadSubject(tier, tiers)
  const named = (assembly?.blocks ?? []).find((b) => b.target_id === id && b.target_name)?.target_name
  return readerBlockName(named ?? null, id, nameOf)
}

/**
 * M-0a, roll 84 — the path that reads ONE read at full weight when it is
 * opened: `GET /findings/{id}` (P-8), exact and one row. `null` when the row is
 * not in the summary page (an old link): the caller then says the read is not
 * in the loaded window, as before.
 */
export function fullReadQuery(id: string, rows: readonly ReportRow[]): string | null {
  if (!rows.some((r) => r.id === id)) return null
  return `/findings/${encodeURIComponent(id)}`
}

// ── grouping + date navigation ───────────────────────────────────────────────

export interface TierGroup {
  tier: ReportTier
  label: string
  rows: ReportRow[]
}

/**
 * Group the navigator by tier, newest first inside each.
 *
 * Empty tiers are DROPPED rather than rendered as empty headings: a phone
 * screen has no room to spend on a heading that says nothing, and an absent
 * tier is already visible as an absence in the date strip.
 */
export function groupByTier(rows: ReportRow[], tiers: readonly ProfileFrame[] = PROFILE_TIERS): TierGroup[] {
  const out: TierGroup[] = []
  for (const { id: tier, label } of tiers) {
    const inTier = rows
      .filter((r) => r.tier === tier)
      .sort((a, b) => b.producedAt.localeCompare(a.producedAt))
    if (inTier.length > 0) out.push({ tier, label, rows: inTier })
  }
  return out
}

/** Distinct UTC days present in the rows, newest first — the date strip. */
export function daysPresent(rows: ReportRow[]): string[] {
  return [...new Set(rows.map((r) => r.day).filter(Boolean))].sort((a, b) => b.localeCompare(a))
}

export function rowsForDay(rows: ReportRow[], day: string | null): ReportRow[] {
  if (!day) return rows
  return rows.filter((r) => r.day === day)
}

// ── the sections under a selected report ─────────────────────────────────────

/** A cited head, projected from the block that quotes it. */
export interface ReportFinding {
  ordinal: number
  findingId: string
  desk: string
  targetId: string | null
  targetName: string | null
  question: string
  severity: string | null
  verifyScore: number | null
  /** The block's own BLUF, the sentence that head contributed. */
  bluf: string
  signalCount: number
}

export function reportFindings(a: AssemblyPayload | null): ReportFinding[] {
  if (!a) return []
  return a.blocks.map((b: AssemblyBlock) => ({
    ordinal: b.ordinal,
    findingId: b.finding_id,
    desk: b.desk,
    targetId: b.target_id,
    targetName: b.target_name,
    question: b.question,
    severity: b.severity,
    verifyScore: b.verify?.overall_score ?? null,
    bluf: feedPreview((b.spans.find((s) => s.role === 'bluf') ?? b.spans[0])?.text ?? ''),
    signalCount: b.signals.length,
  }))
}

/** A cited signal, deduped across the blocks that share it. */
export interface ReportSignal {
  signalId: string | null
  marker: string
  title: string
  url: string | null
  sourceId: string | null
  ageH: number | null
  /** Which on-page blocks cite it — the "one selection, everything follows" link. */
  ordinals: number[]
  targetIds: string[]
}

/**
 * Every distinct signal the report cites.
 *
 * Deduped on `signal_id` where present; a signal with no id (the substrate
 * allows one) is kept under its marker rather than dropped, because losing a
 * cited source silently is worse than showing one twice.
 */
export function reportSignals(a: AssemblyPayload | null): ReportSignal[] {
  if (!a) return []
  const by = new Map<string, ReportSignal>()
  for (const b of a.blocks) {
    for (const s of b.signals as AssemblySignal[]) {
      const key = s.signal_id ?? `marker:${b.ordinal}:${s.marker}`
      const found = by.get(key)
      if (found) {
        if (!found.ordinals.includes(b.ordinal)) found.ordinals.push(b.ordinal)
        if (b.target_id && !found.targetIds.includes(b.target_id)) found.targetIds.push(b.target_id)
        continue
      }
      by.set(key, {
        signalId: s.signal_id,
        marker: s.marker,
        title: s.title,
        url: s.url,
        sourceId: s.source_id,
        ageH: s.age_h,
        ordinals: [b.ordinal],
        targetIds: b.target_id ? [b.target_id] : [],
      })
    }
  }
  return [...by.values()].sort(
    (x, y) => y.ordinals.length - x.ordinals.length || x.marker.localeCompare(y.marker),
  )
}

/**
 * The entities the report names.
 *
 * HONEST SCOPE — read this before extending it. `/api/v1/entities` takes only
 * `q`, `entity_class` and `limit`: there is NO filter from a read (or from a
 * signal id, or a geo box) to the entities it names, so a true "entities in
 * this report" list cannot be served today. What the record DOES carry, as
 * first-class data, is the target each block speaks about. Those targets are
 * what this returns — labelled as targets in the UI, never dressed up as a
 * resolved entity list. The missing filter is named precisely in the build
 * report rather than stubbed here.
 */
export interface ReportEntity {
  targetId: string
  name: string
  /** Blocks that speak about it — tapping filters findings + signals to these. */
  ordinals: number[]
  severity: string | null
}

export function reportEntities(a: AssemblyPayload | null): ReportEntity[] {
  if (!a) return []
  const by = new Map<string, ReportEntity>()
  for (const b of a.blocks) {
    if (!b.target_id) continue
    const found = by.get(b.target_id)
    if (found) {
      if (!found.ordinals.includes(b.ordinal)) found.ordinals.push(b.ordinal)
      continue
    }
    by.set(b.target_id, {
      targetId: b.target_id,
      name: b.target_name ?? b.target_id,
      ordinals: [b.ordinal],
      severity: b.severity,
    })
  }
  return [...by.values()].sort(
    (x, y) => y.ordinals.length - x.ordinals.length || x.name.localeCompare(y.name),
  )
}

// ── the selection filter (the Crossroads rule) ───────────────────────────────

/**
 * Narrow a report's three sections to one selected entity.
 *
 * This is the whole Selection Store idea in one function: the entity names a
 * set of block ordinals, and every section is filtered to that set. Passing
 * `null` restores the full report — selection is always reversible.
 */
export function filterToEntity(
  entityId: string | null,
  findings: ReportFinding[],
  signals: ReportSignal[],
  entities: ReportEntity[],
): { findings: ReportFinding[]; signals: ReportSignal[] } {
  if (!entityId) return { findings, signals }
  const entity = entities.find((e) => e.targetId === entityId)
  if (!entity) return { findings, signals }
  const ords = new Set(entity.ordinals)
  return {
    findings: findings.filter((f) => ords.has(f.ordinal)),
    signals: signals.filter((s) => s.ordinals.some((o) => ords.has(o))),
  }
}

// ── where (the map's cheap honest form) ──────────────────────────────────────

export interface WherePoint {
  targetId: string
  name: string
  severity: string | null
}

/** The report's geography, as the targets it actually speaks about. */
export function reportWhere(a: AssemblyPayload | null): WherePoint[] {
  return reportEntities(a).map((e) => ({
    targetId: e.targetId,
    name: e.name,
    severity: e.severity,
  }))
}
