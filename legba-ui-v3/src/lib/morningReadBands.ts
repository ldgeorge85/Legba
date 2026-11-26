/**
 * morningReadBands — the Morning Read as a twenty-minute JUDGMENT surface (7b-iv).
 *
 * The landing answers "what happened, what moved, what does it mean?". Until
 * this module it answered the third part only: the assembly reader renders ONE
 * composed record beautifully and says nothing about the other fifty desks. A
 * reader with twenty minutes has to leave knowing four things per desk, and
 * each of them is already written somewhere in the substrate:
 *
 *   CHANGED  what landed since I last looked        `/v3/since`
 *   CHECKED  which claims held and which did not    `/findings?since=` + its
 *                                                   `verification` block
 *   GAPS     where the evidence ran out             `/v3/absence?scope=` per
 *                                                   desk, over the desk set
 *                                                   `/v3/eval/country_scorecard`
 *                                                   carries
 *   DUE      what the platform owes an answer on    `/v3/since.forecasts_due`
 *
 * The GAPS band is the one that changed after k5b: it asked the scorecard
 * what a desk's card could not band, which answers a third of the question.
 * TYPED ABSENCE answers the rest — a covering source that went quiet, a
 * search that could not run, a layer an operator declared absent — each with
 * a proof and a clock. The card-derived rows are still shown, as the rows no
 * kind already names.
 *
 * Everything in this file is pure and DOM-free so each band's derivation can be
 * argued with in a unit test rather than through a rendered panel.
 *
 * ── THE FOUR RULES THIS MODULE OBEYS ──────────────────────────────────────
 *
 * 1. NOTHING IS COMPUTED THAT THE ROW DOES NOT SAY. Counts are counts of rows;
 *    a claim's verdict is the verify pass's own; a gap is a persisted
 *    `insufficient-evidence` band, never a threshold this surface invented.
 *    The one derived number is a desk's `weight`, and it is an ORDERING, not a
 *    disclosure — it is never printed as a score.
 *
 * 2. ABSENCE IS A STATE, NOT A ZERO. A finding with no verify block is
 *    `unchecked`, counted in its own column and never folded into "verified"
 *    or "unsupported". A desk with no scorecard card is not a desk with a
 *    clean one. The empty band renders as a stated fact, never as a blank.
 *
 * 3. THE VISIT IS THE TELEMETRY'S, NOT A FRESH CLOCK. `resolveVisit` keys on
 *    the read-telemetry SESSION NONCE (`lib/readTelemetry.sessionNonce`), so a
 *    reload inside the same tab continues one morning's reading rather than
 *    resetting the diff to nothing and hiding everything that landed overnight.
 *    A first-ever visit falls back to YESTERDAY 06:00Z — the operator's own
 *    reading hour, not a rolling 24h window, so the surface answers "since the
 *    last morning" on the day it is actually read.
 *
 * 4. THE TIMER DOES NOT EMIT. It displays the visit's elapsed minutes against
 *    the twenty-minute budget. It deliberately does NOT fire a second
 *    `brief_read`: `MorningRead.tsx` already emits one per rendered read, the
 *    telemetry's dedupe window is 1.2s, and a timer that re-emitted on a tick
 *    would inflate the exact number the 90-day wager is graded on — in the
 *    flattering direction, which is the one bias the instrument must not have.
 */

import { humanizeAnalystId } from '@/lib/analystNames'
import {
  ABSENCE_KIND_LABEL,
  ABSENCE_KIND_ORDER,
  notMeasuredReason,
  type AbsenceItem,
  type AbsenceKind,
  type AbsenceResponse,
} from '@/lib/absenceModel'
import { severityRank } from '@/lib/findingsViews'
import { humanizeId } from '@/lib/deskNames'
import { bandsFromProjection, PROFILE_UI, useProjection, type ProjectionRead } from '@/lib/profileUi'
import { sessionNonce } from '@/lib/readTelemetry'
import { pageLoadGeneration } from '@/lib/pageLoad'
import { whyNot, type UnsupportedSpanView } from '@/lib/claimsModel'
import {
  isInsufficient,
  insufficientLabel,
  type CountryScorecard,
  type DimensionBand,
} from '@/lib/evalOps'
import { isVerifyExemptState, unverifiedStateOf, type UnverifiedInfo } from '@/lib/unverifiedState'
import type { BandChange, SinceAlert, SinceFinding, SinceResponse, SituationChange, SinceSection } from '@/lib/wallModel'

// ---------------------------------------------------------------------------
// `/v3/since.forecasts_due` mirror (registry/forecasts_due.py)
// ---------------------------------------------------------------------------

/** One pre-registered call whose window closed with no outcome. */
export interface ForecastDue {
  id: string
  /** The desk descriptor id the call was minted for. */
  region: string
  event_class: string
  window_start: string
  window_end: string
  p: number
  p_base: number
  method: string
  method_version: string | null
  /** K3 — the SCALE `p` and `p_base` are read on: the clamped open
   *  probability interval. A different question from `method_version` (which
   *  code ran) — this one is whether two p's are comparable at all. Null on a
   *  row minted before the stamp existed; never back-labelled. */
  scale_version: string | null
  /** H13 — the falsifiable test frozen on the row at mint, verbatim. A
   *  `retro: ` prefix means migration 0212 backfilled it; never trimmed. */
  resolution_test: string
  resolved_by: string | null
  issued_at: string
  days_overdue: number
  /** `in_grace` | `awaiting` | `expired` — see `forecasts_due.py`. */
  mark: string
}

/** `/v3/since` with the DUE section this lane added to the envelope. */
export interface JudgmentSince extends SinceResponse {
  forecasts_due?: SinceSection<ForecastDue>
}

/** How each `mark` reads to an operator. `expired` is the only failure. */
export const FORECAST_MARK_LABEL: Record<string, string> = {
  in_grace: 'window just closed — the resolver has not run yet',
  awaiting: 'past the resolver grace and still unanswered',
  expired: 'the resolver ran and could not grade it',
}

export function forecastMarkLabel(mark: string): string {
  return FORECAST_MARK_LABEL[mark] ?? mark.replace(/_/g, ' ')
}

// ---------------------------------------------------------------------------
// The visit clock (rule 3)
// ---------------------------------------------------------------------------

/** Where the visit record lives. One key, one JSON object, best-effort. */
export const VISIT_KEY = 'legba_morning_visit'

/** The hour the fallback cursor opens on — the operator's reading hour. */
export const FALLBACK_READ_HOUR_UTC = 6

export interface MorningVisit {
  /** Epoch ms this visit began (the first paint under this session nonce). */
  startedAt: number
  /** Epoch ms the PREVIOUS visit began, or null on a first-ever visit. */
  previousVisitAt: number | null
  /** True when nothing was stored — the fallback cursor is in play. */
  firstVisit: boolean
}

interface StoredVisit {
  nonce: string
  startedAt: number
  previousVisitAt: number | null
}

function storage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

function readStoredVisit(): StoredVisit | null {
  try {
    const raw = storage()?.getItem(VISIT_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as StoredVisit
    if (!parsed || typeof parsed !== 'object') return null
    if (typeof parsed.nonce !== 'string' || !Number.isFinite(parsed.startedAt)) return null
    return parsed
  } catch {
    return null
  }
}

/**
 * The visit this render belongs to, rolled forward when the session changed.
 *
 * The identity is the telemetry's `session_nonce`, which `sessionStorage`
 * holds: same tab, same morning, even across a reload; a new tab or a new day
 * is a new visit and the OLD visit's start becomes the diff cursor. Storage
 * failures degrade to an in-memory first visit rather than throwing inside a
 * render — a preference is never worth an exception.
 */
export function resolveVisit(nowMs: number = Date.now()): MorningVisit {
  const nonce = sessionNonce()
  const stored = readStoredVisit()
  if (stored && stored.nonce === nonce) {
    return {
      startedAt: stored.startedAt,
      previousVisitAt: stored.previousVisitAt ?? null,
      firstVisit: false,
    }
  }
  const next: StoredVisit = {
    nonce,
    startedAt: nowMs,
    previousVisitAt: stored ? stored.startedAt : null,
  }
  try {
    storage()?.setItem(VISIT_KEY, JSON.stringify(next))
  } catch {
    // In-memory only for this render — the bands still work off the fallback.
  }
  return {
    startedAt: next.startedAt,
    previousVisitAt: next.previousVisitAt,
    firstVisit: stored === null,
  }
}

/** Yesterday at {@link FALLBACK_READ_HOUR_UTC}:00Z relative to `nowMs`. */
export function yesterdayReadingHour(nowMs: number): number {
  const d = new Date(nowMs)
  const at = Date.UTC(
    d.getUTCFullYear(),
    d.getUTCMonth(),
    d.getUTCDate() - 1,
    FALLBACK_READ_HOUR_UTC,
    0,
    0,
    0,
  )
  return at
}

/**
 * The `?cursor=` for this visit: the previous visit's start, else yesterday
 * 06:00Z. Clamped into the route's 90-day bound (it 400s past it) with the
 * same margin `wallModel` uses, so a browser left open for a quarter cannot
 * hand the server a cursor it must reject.
 */
export function visitCursor(visit: MorningVisit, nowMs: number = Date.now()): string {
  const fallback = yesterdayReadingHour(nowMs)
  const wanted = visit.previousVisitAt ?? fallback
  const oldest = nowMs - 90 * 86_400_000 + 5 * 60_000
  const clamped = Math.min(Math.max(wanted, oldest), nowMs)
  return new Date(clamped).toISOString()
}

/**
 * THE ONE "SINCE" of this page load: the visit resolved once and the cursor
 * built from it, shared by every region that says "since you last looked" —
 * the Board list, the rail's change counts, the map's change layer, the full
 * daily read and the Wall's movers. Each used to resolve its own: the first
 * to mount rolled the stored visit forward and said "first visit" while the
 * next read the rolled record and printed a since-time.
 *
 * `firstVisit` here means there is no earlier visit to diff from (the
 * cursor is the fallback reading hour) — true for every region alike.
 */
export interface PageVisit {
  visit: MorningVisit
  /** The `?cursor=` every since read sends. */
  cursor: string
  /** No earlier visit: the cursor is yesterday's reading hour. */
  firstVisit: boolean
}

let pageVisitMemo: { nonce: string; load: number; value: PageVisit } | null = null

/** This page load's visit and cursor — resolved on first use, then fixed. */
export function pageVisit(nowMs: number = Date.now()): PageVisit {
  const nonce = sessionNonce()
  const load = pageLoadGeneration()
  if (pageVisitMemo && pageVisitMemo.nonce === nonce && pageVisitMemo.load === load) return pageVisitMemo.value
  const visit = resolveVisit(nowMs)
  const value: PageVisit = { visit, cursor: visitCursor(visit, nowMs), firstVisit: visit.previousVisitAt === null }
  pageVisitMemo = { nonce, load, value }
  return value
}

/** Tests only: forget this page load's visit. */
export function __resetPageVisit(): void {
  pageVisitMemo = null
}

/** Elapsed ms of this visit, floored at zero (a skewed clock reads 0, not −). */
export function visitElapsedMs(visit: MorningVisit, nowMs: number = Date.now()): number {
  return Math.max(0, nowMs - visit.startedAt)
}

/** The twenty-minute budget the surface is designed against. */
export const READ_BUDGET_MS = 20 * 60_000

/** `m:ss` — a timer, not a duration prose string. */
export function formatElapsed(ms: number): string {
  const total = Math.floor(Math.max(0, ms) / 1000)
  const mins = Math.floor(total / 60)
  const secs = total % 60
  return `${mins}:${String(secs).padStart(2, '0')}`
}

// ---------------------------------------------------------------------------
// Desk grouping — the shared shape every band returns
// ---------------------------------------------------------------------------

/** The bucket rows with no desk fall into (a world-tier read, an orphan). */
export const UNSCOPED_DESK = '__unscoped__'
/** The profile's words for that bucket (`ui.labels.unscoped_subject`; the platform noun when undeclared). */
export const UNSCOPED_DESK_LABEL = PROFILE_UI.labels.unscoped_subject

export interface DeskGroup<T> {
  /** The target id, or {@link UNSCOPED_DESK}. */
  targetId: string
  /** The humanized desk name — the row never renders raw snake_case. */
  label: string
  items: T[]
  /**
   * This band's movement weight for this desk. An ORDERING ONLY (rule 1) —
   * never printed. Bands that rank by severity fold it in here so "what moved
   * most" means what it says rather than "what produced the most rows".
   */
  weight: number
}

export function deskLabel(targetId: string): string {
  return targetId === UNSCOPED_DESK ? UNSCOPED_DESK_LABEL : humanizeId(targetId)
}

/**
 * Group rows onto desks and order the desks by weight, worst first.
 *
 * Ties break on the LABEL, not on insertion order: two desks with the same
 * weight must sit in the same place on every paint, or a reader loses their
 * position between refetches.
 */
export function groupByDesk<T>(
  rows: readonly T[],
  targetOf: (row: T) => string | null | undefined,
  weightOf: (row: T) => number = () => 1,
): DeskGroup<T>[] {
  const byDesk = new Map<string, DeskGroup<T>>()
  for (const row of rows) {
    const raw = targetOf(row)
    const target = raw && raw.trim() !== '' ? raw : UNSCOPED_DESK
    let group = byDesk.get(target)
    if (!group) {
      group = { targetId: target, label: deskLabel(target), items: [], weight: 0 }
      byDesk.set(target, group)
    }
    group.items.push(row)
    group.weight += weightOf(row)
  }
  return [...byDesk.values()].sort(
    (a, b) => b.weight - a.weight || a.label.localeCompare(b.label),
  )
}

// ---------------------------------------------------------------------------
// Band 1 — CHANGED
// ---------------------------------------------------------------------------

export type ChangedKind = 'finding' | 'alert' | 'situation' | 'band'

/** An alert folded INTO the item it alerts on: shown as a mark on that row. */
export interface ChangedAlertMark {
  /** The alert row id — still a citation. */
  id: string
  /** The alert's trigger class (`verified_finding`, `situation_escalation`, …). */
  channel: string
  severity: string | null
  at: string
}

export interface ChangedRow {
  kind: ChangedKind
  /** The substrate row id — the citation. Every row is openable. */
  id: string
  /** The substrate `row_kind` for `selectRow` (`finding` / `alert` / …). */
  rowKind: string
  headline: string
  /** The one-line note under the headline (the transition, the producer). */
  note: string
  severity: string | null
  at: string
  targetId: string | null
  /** The desk this row sits on, {@link UNSCOPED_DESK} when it has none. */
  deskId: string
  /** The humanized desk name, printed on the row (the list is ranked, not grouped). */
  deskLabel: string
  /** Alerts that point at this item, folded in rather than listed beside it. */
  alerts: ChangedAlertMark[]
}

/**
 * A since-finding's effective confidence as printed, or `—` when the
 * row does not carry a finite number. The live `/v3/since` route cannot omit
 * it on `new_findings` (`SinceFinding.effective_confidence` is a required
 * float over `LEAST(confidence NOT NULL, faithfulness_score)` on a JOINed
 * critique — checked 2026-10-13: 50 of 50 items numeric), but this band also
 * reads fixtures, cached envelopes and older registries, and one row without
 * the field used to throw inside the render and blank every band.
 */
export function confidenceText(v: unknown): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(2) : '—'
}

/** How an alert's trigger class reads as a mark on the item it alerts on. */
export function alertMarkText(channel: string): string {
  const c = (channel || '').trim()
  if (c === 'verified_finding') return 'alerted: verified'
  if (c === 'situation_escalation') return 'alerted: escalation'
  return c ? `alerted: ${c.replace(/_/g, ' ')}` : 'alerted'
}

/** The verified-finding alert's title shape (`alert_trigger_scan`). */
const VERIFIED_ALERT_RE = /^Verified [a-z]+-severity finding: ([\s\S]+)$/
/** The situation-escalation alert's title shape (`_situation_escalation_scan`). */
const ESCALATION_ALERT_RE = /^Situation escalated: ([\s\S]+) \((\S+) -> (\S+)\)$/

function deskOf(targetId: string | null | undefined): string {
  return targetId && targetId.trim() !== '' ? targetId : UNSCOPED_DESK
}

/** The worse of a row's own severity and the severities of the alerts folded into it. */
export function changedRankSeverity(row: Pick<ChangedRow, 'severity' | 'alerts'>): number {
  return Math.max(severityRank(row.severity), ...row.alerts.map((a) => severityRank(a.severity)))
}

/**
 * Everything the diff says landed, as ONE ranked, de-duplicated, cited list.
 *
 * FOUR SOURCES, ONE GRAMMAR. A verified finding, an alert, a situation
 * lifecycle edge and a band crossing are four different rows in the substrate;
 * a reader with twenty minutes needs them in one list by how bad they are.
 * `superseded` is deliberately NOT here: a reversal is a change to a record
 * the reader may never have seen, and it has its own surface on the Wall.
 *
 * THREE RULES (UI 2026-11-13, "changed de-duplicated and ranked"):
 *  1. AN ALERT COLLAPSES INTO WHAT IT ALERTS ON. An alert that points at an
 *     item already in the list — by the pointer the route carries
 *     (`finding_id` / `situation_id`), else by its own title shape on the same
 *     desk — is not a row: it becomes a mark on that item. An alert whose item
 *     is not in the list stays a row. Two identical alerts on one desk are one.
 *  2. A STATE TO THE SAME STATE IS NOT A CHANGE. A situation edge or a band
 *     crossing whose from and to are equal, and an escalation alert `x -> x`,
 *     are dropped.
 *  3. RANKED, NOT GROUPED. Order: the worse of the row's severity and its
 *     folded alerts' severities (an alert raises its item, it does not add a
 *     row), then most recent first, then id for a stable paint. A situation
 *     carries intensity, not severity, so it ranks with the unscored rows.
 */
export function changedList(since: JudgmentSince | null | undefined): ChangedRow[] {
  if (!since) return []
  const rows: ChangedRow[] = []
  const base = (targetId: string | null) => {
    const deskId = deskOf(targetId)
    return { targetId, deskId, deskLabel: deskLabel(deskId), alerts: [] as ChangedAlertMark[] }
  }

  const findingById = new Map<string, ChangedRow>()
  for (const f of since.new_findings?.items ?? ([] as SinceFinding[])) {
    if (findingById.has(f.id)) continue
    const row: ChangedRow = {
      kind: 'finding',
      id: f.id,
      rowKind: 'finding',
      headline: f.title,
      // The producing unit in words, never its machine id (`energy_security` → `Energy security`).
      note: `${humanizeAnalystId(f.analyst_id, 'unknown producer')} · effective confidence ${confidenceText(f.effective_confidence)}`,
      severity: f.severity,
      at: f.produced_at,
      ...base(f.target_id),
    }
    findingById.set(f.id, row)
    rows.push(row)
  }

  const situationById = new Map<string, ChangedRow>()
  for (const s of since.situations?.items ?? ([] as SituationChange[])) {
    // Rule 2 — `x → x` is not a change.
    if (s.from_status !== null && s.from_status === s.to_status) continue
    if (situationById.has(s.id)) continue
    const row: ChangedRow = {
      kind: 'situation',
      id: s.id,
      rowKind: 'situation',
      headline: s.name,
      note:
        s.from_status === null
          ? `situation ${s.change} · now ${s.to_status}`
          : `situation ${s.change} · ${s.from_status} → ${s.to_status}`,
      // A situation carries an intensity, not a severity; leaving it null keeps
      // the two scales from being read as one (rule 1 of this module).
      severity: null,
      at: s.updated_at,
      ...base(s.target_id),
    }
    situationById.set(s.id, row)
    rows.push(row)
  }

  for (const b of since.band_changes?.items ?? ([] as BandChange[])) {
    if (b.from_band === b.to_band) continue
    rows.push({
      kind: 'band',
      id: b.to_scorecard_row_id,
      rowKind: 'scorecard',
      headline: `${b.dimension}: ${b.from_band} → ${b.to_band}`,
      note: `band ${b.direction}`,
      severity: b.severity,
      at: b.changed_at,
      ...base(b.target_id),
    })
  }

  const alertRowByKey = new Map<string, ChangedRow>()
  for (const a of since.alerts?.items ?? ([] as SinceAlert[])) {
    const deskId = deskOf(a.target_id)
    const mark: ChangedAlertMark = { id: a.id, channel: a.channel || '', severity: a.severity, at: a.produced_at }
    const summary = a.summary ?? ''
    const esc = ESCALATION_ALERT_RE.exec(summary)
    const from = a.state_from ?? esc?.[2] ?? null
    const to = a.state_to ?? esc?.[3] ?? null
    // Rule 2 — an escalation page from a state to the same state.
    if (from !== null && to !== null && from === to) continue

    // Rule 1 — fold into the item it points at.
    let host: ChangedRow | undefined
    if (a.finding_id) host = findingById.get(a.finding_id)
    if (!host && a.situation_id) host = situationById.get(a.situation_id)
    if (!host) {
      const verified = VERIFIED_ALERT_RE.exec(summary)
      if (verified) {
        const title = verified[1].trim()
        host = rows.find(
          (r) =>
            r.kind === 'finding' &&
            r.deskId === deskId &&
            (r.headline === title || r.headline.startsWith(title)),
        )
      } else if (esc) {
        const name = esc[1].trim()
        host = rows.find((r) => r.kind === 'situation' && r.deskId === deskId && r.headline === name)
      }
    }
    if (host) {
      host.alerts.push(mark)
      continue
    }

    // Two identical alerts on one desk are one row.
    const key = `${deskId}\u0000${summary}`
    const twin = alertRowByKey.get(key)
    if (twin) {
      twin.alerts.push(mark)
      continue
    }
    const row: ChangedRow = {
      kind: 'alert',
      id: a.id,
      rowKind: 'alert',
      headline: summary,
      // The trigger class in words (`situation_escalation` → `situation escalation`).
      note: `alert · ${(a.channel || 'unrouted').replace(/_/g, ' ')}`,
      severity: a.severity,
      at: a.produced_at,
      ...base(a.target_id),
    }
    alertRowByKey.set(key, row)
    rows.push(row)
  }

  return rows.sort(
    (x, y) =>
      changedRankSeverity(y) - changedRankSeverity(x) ||
      (Date.parse(y.at) || 0) - (Date.parse(x.at) || 0) ||
      x.id.localeCompare(y.id),
  )
}

/** How many distinct desks a changed list spans (the band's note). */
export function changedDeskCount(rows: readonly ChangedRow[]): number {
  return new Set(rows.map((r) => r.deskId)).size
}

// ---------------------------------------------------------------------------
// Band 2 — CHECKED
// ---------------------------------------------------------------------------

/**
 * One citation reduced to the judgment weight's key set (7b-v).
 *
 * Mirrors `findings_projection.JUDGMENT_FIELDS` / `substrate_reads_api
 * .CitationJudgmentEntry` field-for-field — never the full stored citation
 * (`evidence_text`, `title`, `derived_from`, …). This band does not render a
 * citation directly today (the flagged span's own `markers` carry the
 * ordinals it shows); the type exists so `JudgmentFindingRow` is honest about
 * everything `GET /findings?fields=judgment` actually returns.
 */
export interface JudgmentCitation {
  ordinal: number | null
  source: string | null
  source_id: string | null
  produced_at: string | null
  single_source: boolean
  wire_folded: boolean
  marker_class: string | null
  /** C-1: the claimant when the cited row is a CLAIM, else null. */
  claimant?: string | null
}

/**
 * One `/findings?fields=judgment` row (7b-v) — the CHECKED band's population.
 *
 * Mirrors `substrate_reads_api.FindingJudgmentRow` field-for-field. Never
 * `body`, `data`, or `derived_from` — the three leaves that made the
 * pre-projection page ~5.5 MB for 200 rows and this band never read. A
 * SEPARATE type from `claimsModel.FindingRow` (the full-weight shape other
 * surfaces still fetch) rather than a subset of it, so a field this band
 * does not actually receive can never silently type-check.
 */
export interface JudgmentFindingRow {
  id: string
  kind: string
  title: string
  analyst_id: string | null
  analyst_version: string | null
  target_id: string | null
  target_version: string | null
  produced_at: string
  severity: string | null
  confidence: number
  schema_uri: string
  /** The verify block, or — X4 READPATH — the registry's pending|absent
   *  state block (`lib/unverifiedState`) when no verdict exists; `null` only
   *  from a registry that predates it. */
  verification: Record<string, unknown> | null
  citations: JudgmentCitation[]
}

export interface UnsupportedClaim extends UnsupportedSpanView {
  /** The finding the sentence was written in — the claim's SOURCE. */
  findingId: string
  findingTitle: string
  analystId: string | null
  producedAt: string
}

/** One finding on a desk with NO verify verdict, in the registry's own state. */
export interface UnverifiedFinding {
  findingId: string
  title: string
  analystId: string | null
  producedAt: string
  info: UnverifiedInfo
}

export interface CheckedDesk {
  targetId: string
  label: string
  /** Findings whose verify pass ran and flagged nothing. */
  verified: number
  /** Findings whose verify pass flagged at least one claim. */
  unsupported: number
  /** Findings with NO verify block at all — an absence, not a pass (rule 2).
   *  Always `pending + notJudged`. */
  unchecked: number
  /** X4 READPATH — no verdict YET: inside the judge's window. */
  pending: number
  /** X4 READPATH — no verdict and none coming (window passed, or verify-exempt). */
  notJudged: number
  /** The block-less findings a reader should see by name — every one but the
   *  verify-EXEMPT producers' (they never enter the pass; counted, not listed). */
  unverified: UnverifiedFinding[]
  /** Every flagged sentence on the desk, with the finding it came from. */
  claims: UnsupportedClaim[]
  weight: number
}

/**
 * The day's claim verdicts per desk.
 *
 * WHAT COUNTS AS "VERIFIED" HERE is the verify pass's own answer and nothing
 * else: a `verification` block exists and named no unsupported span. It is NOT
 * the effective-confidence floor (`/since`'s `new_findings` gate) — a finding
 * can clear 0.50 and still carry a flagged sentence, and this band exists
 * precisely to show that sentence. A row with no block is `unchecked`, because
 * "we did not look" and "we looked and it held" must not render the same —
 * and since X4 READPATH it is split by the REGISTRY's own state (never this
 * band's clock): `pending` (the verify pass may still land) or `notJudged`.
 *
 * THE SOURCE ON EACH SENTENCE is the finding it was written in — id, title and
 * producer — which is what a reader needs to go argue with it. The `markers`
 * carried alongside are the citation ORDINALS inside that finding; resolving
 * them to signals is the Inspector's job one drill-click down, and this band
 * deliberately does not re-implement it.
 */
export function checkedBand(rows: readonly JudgmentFindingRow[] | null | undefined): CheckedDesk[] {
  if (!Array.isArray(rows)) return []
  const byDesk = new Map<string, CheckedDesk>()
  for (const row of rows) {
    const target = row.target_id && row.target_id.trim() !== '' ? row.target_id : UNSCOPED_DESK
    let desk = byDesk.get(target)
    if (!desk) {
      desk = {
        targetId: target,
        label: deskLabel(target),
        verified: 0,
        unsupported: 0,
        unchecked: 0,
        pending: 0,
        notJudged: 0,
        unverified: [],
        claims: [],
        weight: 0,
      }
      byDesk.set(target, desk)
    }
    // X4 READPATH — the registry's pending|absent state block is NOT a verify
    // block: it is read first, so it can never be counted as a pass.
    const unverified = unverifiedStateOf(row)
    const verification = row.verification ?? null
    const flagged = unverified ? null : whyNot(verification)
    if (unverified) {
      desk.unchecked += 1
      if (unverified.state === 'pending') desk.pending += 1
      else desk.notJudged += 1
      if (!isVerifyExemptState(unverified)) {
        desk.unverified.push({
          findingId: row.id,
          title: row.title,
          analystId: row.analyst_id ?? null,
          producedAt: row.produced_at,
          info: unverified,
        })
      }
    } else if (flagged) {
      desk.unsupported += 1
      for (const span of flagged.unsupportedSpans) {
        desk.claims.push({
          ...span,
          findingId: row.id,
          findingTitle: row.title,
          analystId: row.analyst_id ?? null,
          producedAt: row.produced_at,
        })
      }
    } else {
      desk.verified += 1
    }
  }
  // Newest first inside a desk — the pending ones are the ones still moving.
  for (const desk of byDesk.values()) {
    desk.unverified.sort((a, b) => Date.parse(b.producedAt) - Date.parse(a.producedAt))
  }
  // A desk is ranked by how much it FAILED, not by how much it produced: the
  // flagged sentences are the thing a reader has twenty minutes to act on.
  for (const desk of byDesk.values()) desk.weight = desk.claims.length * 10 + desk.unsupported
  return [...byDesk.values()].sort(
    (a, b) => b.weight - a.weight || a.label.localeCompare(b.label),
  )
}

// ---------------------------------------------------------------------------
// Band 3 — GAPS
// ---------------------------------------------------------------------------

export type GapKind = 'insufficient-evidence' | 'no-current-read'

export interface GapRow {
  kind: GapKind
  /** The scorecard dimension, or null for a whole-desk gap. */
  dimension: string | null
  /** The persisted machine reason, rendered for an operator. */
  reason: string
  /** The card the gap was read off — the citation. */
  cardId: string
  producedAt: string
}

/**
 * Where the evidence ran out, per desk.
 *
 * DERIVED LOCALLY, AND SAYING SO. The gap-strip lane owns a shared model for
 * this; it had not landed at this lane's pin, so the derivation here is the
 * persisted card's own two statements and nothing more:
 *
 *   * a dimension banded `insufficient-evidence` — the banding engine's own
 *     verdict that no qualifying verified claim exists, with its `reason`
 *     (`no-finding` / `verify-failed` / `below-floor` / …) carried through
 *     `evalOps.insufficientLabel`, the same resolver the Eval Scorecard uses,
 *     so the two surfaces cannot disagree;
 *   * `composition.present === false` — the desk has no CURRENT composition to
 *     read. That is the honest reading of "has no current read" available off
 *     this route: a desk that never had a card at all is absent from the
 *     response entirely and is therefore outside this derivation. The band
 *     says so in surface rather than implying the roster is complete.
 *
 * When the shared model lands, this function is the seam to replace — the
 * component consumes `GapRow[]` and knows nothing about scorecards.
 */
export function gapsBand(
  cards: readonly CountryScorecard[] | null | undefined,
): DeskGroup<GapRow>[] {
  if (!Array.isArray(cards)) return []
  const groups: DeskGroup<GapRow>[] = []
  for (const card of cards) {
    const items: GapRow[] = []
    if (card.composition && card.composition.present === false) {
      items.push({
        kind: 'no-current-read',
        dimension: null,
        reason: 'no current composition on this desk',
        cardId: card.id,
        producedAt: card.produced_at,
      })
    }
    const dimensions: Record<string, DimensionBand> = card.dimensions ?? {}
    for (const [dimension, band] of Object.entries(dimensions)) {
      if (!band || !isInsufficient(band)) continue
      items.push({
        kind: 'insufficient-evidence',
        dimension,
        reason: insufficientLabel(band.reason),
        cardId: card.id,
        producedAt: band.produced_at ?? card.produced_at,
      })
    }
    if (items.length === 0) continue
    items.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === 'no-current-read' ? -1 : 1
      return (a.dimension ?? '').localeCompare(b.dimension ?? '')
    })
    groups.push({
      targetId: card.target_id,
      label: deskLabel(card.target_id),
      items,
      // A desk with no current read at all outranks one missing a dimension.
      weight: items.reduce((n, g) => n + (g.kind === 'no-current-read' ? 100 : 1), 0),
    })
  }
  return groups.sort((a, b) => b.weight - a.weight || a.label.localeCompare(b.label))
}

// ---------------------------------------------------------------------------
// Band 3 — GAPS, read off `/v3/absence` (k5b)
// ---------------------------------------------------------------------------

/**
 * THE BAND'S DESK POPULATION.
 *
 * Until k5b the band showed only desks whose CARD already carried a gap, so a
 * desk whose scorecard bands cleanly printed nothing — and a desk can band
 * cleanly on every dimension while three of its sources have gone silent and
 * its external audit has found nothing twice. Typed absence is a different
 * read, so it needs a different population: every desk that has a card, in
 * the band's existing order (card-derived gaps first, weightiest first), then
 * the rest by label so the order is stable across paints.
 *
 * Capped, because each desk costs one `/v3/absence` read — the cap is the
 * caller's and the band states how many desks it is holding back.
 */
export function gapDeskOrder(
  groups: readonly DeskGroup<GapRow>[],
  cards: readonly CountryScorecard[] | null | undefined,
): { targetId: string; label: string }[] {
  const seen = new Set<string>()
  const out: { targetId: string; label: string }[] = []
  for (const g of groups) {
    if (seen.has(g.targetId)) continue
    seen.add(g.targetId)
    out.push({ targetId: g.targetId, label: g.label })
  }
  const rest = (Array.isArray(cards) ? cards : [])
    .map((c) => c.target_id)
    .filter((t): t is string => typeof t === 'string' && t.trim() !== '' && !seen.has(t))
  for (const t of [...new Set(rest)].sort((a, b) =>
    deskLabel(a).localeCompare(deskLabel(b)),
  )) {
    out.push({ targetId: t, label: deskLabel(t) })
  }
  return out
}

/** One kind's rows on one desk, in exactly one of the route's three states. */
export interface AbsenceKindGroup {
  kind: AbsenceKind
  /** The route's OWN one-line meaning — never a label this file invented. */
  meaning: string
  /**
   * `absent` — items; `clear` — read for this desk and nothing absent under
   * this kind; `not_measured` — the route could not read it here. Three
   * answers a blank band would collapse into one, which is the whole defect
   * typed absence exists to close.
   */
  state: 'absent' | 'clear' | 'not_measured'
  /** The route's own sentence, when the state is `not_measured`. */
  notMeasured: string | null
  items: AbsenceItem[]
  /** Items this kind recorded and the band is not showing. */
  heldBack: number
  count: number
}

/** One desk in the Gaps band, typed absence first, card-derived gaps after. */
export interface DeskGapView {
  targetId: string
  label: string
  /** All seven kinds, always, in the route's declaration order. */
  kinds: AbsenceKindGroup[]
  /** Card-derived gaps NOT already named by a typed absence on this desk. */
  derived: GapRow[]
  /** Card-derived gaps a typed absence already names — counted, not repeated. */
  coveredByKind: number
  /** Typed absences recorded for this desk, before the per-kind cap. */
  absenceCount: number
  /** The absence read's OWN instant, never this page's clock. */
  readAt: string | null
  state: 'loading' | 'error' | 'ready'
}

/**
 * One desk's Gaps view: the typed absences from `/v3/absence`, plus whatever
 * the card said that no kind already says.
 *
 * NO DOUBLE COUNTING. A scorecard dimension and a bounded unit share an id
 * (`energy_security` is both), so a unit the route has classed `not_collected`
 * / `source_stale` / `below_floor` is ALREADY on the desk under that kind. The
 * card-derived row for the same subject is dropped from `derived` and counted
 * in `coveredByKind` — dropped rather than merged, because the typed row is
 * the one carrying a proof and a clock and the card row is not.
 *
 * NOTHING IS DERIVED HERE. The route classifies, stamps and orders; this
 * function groups what it returned and subtracts what it duplicates.
 */
export function deskGapView(
  desk: { targetId: string; label: string },
  derived: readonly GapRow[],
  resp: AbsenceResponse | null | undefined,
  state: 'loading' | 'error' | 'ready',
  perKind: number,
): DeskGapView {
  const items = resp?.absences ?? []
  const subjects = new Set(items.map((a) => a.subject))
  const kinds: AbsenceKindGroup[] = ABSENCE_KIND_ORDER.map((kind) => {
    const mine = items.filter((a) => a.kind === kind)
    const why = notMeasuredReason(resp, kind)
    const shown = perKind > 0 ? mine.slice(0, perKind) : mine
    return {
      kind,
      // The route publishes the meaning of every kind it answers with; fall
      // back to the reader's short label only if a response omitted the block.
      meaning: resp?.kinds?.[kind] ?? ABSENCE_KIND_LABEL[kind],
      state: why !== null ? 'not_measured' : mine.length > 0 ? 'absent' : 'clear',
      notMeasured: why,
      items: shown,
      heldBack: mine.length - shown.length,
      count: mine.length,
    }
  })
  const uncovered = derived.filter((g) => !(g.dimension && subjects.has(g.dimension)))
  return {
    targetId: desk.targetId,
    label: desk.label,
    kinds,
    derived: uncovered,
    coveredByKind: derived.length - uncovered.length,
    absenceCount: items.length,
    readAt: resp?.read_at ?? null,
    state,
  }
}

// ---------------------------------------------------------------------------
// Band 4 — DUE
// ---------------------------------------------------------------------------

/**
 * What the platform owes an answer on, per desk.
 *
 * The rows arrive longest-overdue-first from the route and the grouping keeps
 * that inside each desk; desks rank on total days overdue, so one call nine
 * days late outranks three that closed this morning. `expired` rows weigh
 * double — the resolver already tried and failed on those, which is the only
 * one of the three marks that is a failure rather than a wait.
 */
export function dueBand(
  section: SinceSection<ForecastDue> | null | undefined,
): DeskGroup<ForecastDue>[] {
  const items = section?.items
  if (!Array.isArray(items)) return []
  return groupByDesk(
    items,
    (f) => f.region,
    (f) => (f.mark === 'expired' ? 2 : 1) * Math.max(1, f.days_overdue),
  )
}

// ---------------------------------------------------------------------------
// The reading-order rail
// ---------------------------------------------------------------------------

export type BandId = 'changed' | 'checked' | 'gaps' | 'due'

export interface BandSpec {
  id: BandId
  /** The band's kicker. */
  label: string
  /** The question it answers — the rail's own copy and the anchor's title. */
  question: string
}

/**
 * The reading ORDER, and it is an argument rather than a list. What landed
 * comes first because it is the only band that can change what the reader does
 * next; what held comes second because a change you cannot trust is not a
 * change; where the evidence ran out third, because that is the shape of what
 * the first two could not say; and what is owed last, because it is the only
 * band about the platform rather than the world.
 */
export const BANDS: readonly BandSpec[] = [
  { id: 'changed', label: 'Changed', question: 'What landed since you last looked?' },
  { id: 'checked', label: 'Checked', question: 'Which claims held, and which did not?' },
  { id: 'gaps', label: 'Gaps', question: 'Where did the evidence run out?' },
  { id: 'due', label: 'Due', question: 'What does the platform owe an answer on?' },
]

/**
 * The bands as THIS profile words them (UI-B2): the projection's `bands`
 * section overrides a band's label and question by id when the plane answers;
 * otherwise the platform's own reading order above stands. The ORDER and the
 * ids are the platform's (the argument above) — a projected band id the
 * platform does not have is not rendered, never invented. `read` carries the
 * refusal for the surface to name.
 */
export function bandsWithProjection(read: ProjectionRead): readonly BandSpec[] {
  if (read.state !== 'ok') return BANDS
  const byId = new Map(bandsFromProjection(read.rows).map((b) => [b.id, b]))
  return BANDS.map((b) => {
    const p = byId.get(b.id)
    return p ? { id: b.id, label: p.label, question: p.question } : b
  })
}

/** {@link bandsWithProjection} over the live plane, plus the read itself. */
export function useBands(): { bands: readonly BandSpec[]; read: ProjectionRead } {
  const read = useProjection('bands')
  return { bands: bandsWithProjection(read), read }
}

/** One band's spec, by id, with the projection's precedence. */
export function useBand(id: BandId): BandSpec {
  const { bands } = useBands()
  return bands.find((b) => b.id === id) ?? BANDS.find((b) => b.id === id)!
}

/** Where the bands' words came from, in one line (the reading rail's tooltip). */
export function bandsSourceText(read: ProjectionRead): string {
  if (read.state === 'ok') return `bands from the ${read.plane} projection`
  if (read.state === 'loading') return 'bands: the platform reading order (projection not read yet)'
  return `bands: the platform reading order — ${read.name}: ${read.reason}`
}

/** The DOM id / test id a band container carries (`morning-read-changed`). */
export function bandTestId(id: BandId): string {
  return `morning-read-${id}`
}
