/**
 * The Watch surface's model (U-9) — pure: wire rows in, reader rows out. No
 * React, no fetch. Every word a reader sees on Watch is chosen here, so the
 * vocabulary is tested in one place (`model.test.ts`) and engine words stay
 * off the page (kinds, ids and `*_stall` never reach a title).
 *
 * Three regions read it:
 *
 *   ALERT LOG   the one log's open and recently closed episodes, newest first,
 *               each with its kind in plain words, its severity (a word and a
 *               2 px rule), whether it PAGED or was only LOGGED and by which
 *               entry of the page rule, and its subject as a point a click
 *               makes current (source, unit, subject).
 *   SILENCES    sources (and units) that are quiet, each with the reason in
 *               plain words and on which side of the one line that matters:
 *               LOOKED and saw nothing, or COULD NOT look.
 *   SOURCE EDITS  open flags on pages that changed after they were read
 *               (what changed, how the source said so, what relied on it, both
 *               passages), the auto-close shadow as a shadow, and the
 *               per-source edit record as a tracked measure — never a rank.
 */
import { PROFILE_UI } from '@/lib/profileUi'
import { humanizeId } from '@/lib/deskNames'
import { SUBJECT_NOUN } from '@/lib/subjectNoun'
import type { SelectionKind } from '@/state/selection'
import type {
  AlertEpisode,
  AlertKindInfo,
  AlertLogResponse,
  DriftEditorRow,
  DriftFlag,
  EditWindow,
  FiringRow,
  PagedAlert,
} from './types'

// ---------------------------------------------------------------------------
// Time.
// ---------------------------------------------------------------------------

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** `14 Nov 19:03Z`, or '' for an unreadable time (never "Invalid Date"). */
export function whenText(iso: string | null | undefined): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return ''
  const d = new Date(t)
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${hh}:${mm}Z`
}

/** `3 min ago`, `5 h ago`, `2 d ago` — '' when either time is unreadable. */
export function agoText(iso: string | null | undefined, now: number): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return ''
  const s = Math.max(0, Math.round((now - t) / 1000))
  if (s < 90) return 'just now'
  const m = Math.round(s / 60)
  if (m < 90) return `${m} min ago`
  const h = Math.round(m / 60)
  if (h < 48) return `${h} h ago`
  return `${Math.round(h / 24)} d ago`
}

/** The alert log's window in hours for the one clock's span (1 … 720). */
export function windowHours(spanMs: number): number {
  const h = Math.round(spanMs / 3_600_000)
  return Math.min(720, Math.max(1, Number.isFinite(h) ? h : 24))
}

// ---------------------------------------------------------------------------
// Subjects — what a row points at, and whether the frame can make it the point.
// ---------------------------------------------------------------------------

const unitNoun = PROFILE_UI.unit_noun.singular

/** `source.who.disease_outbreak_news` → `who · disease outbreak news`. */
export function sourceLabel(id: string): string {
  const rest = id.replace(/^source\./, '').replace(/^host:/, '')
  const parts = rest.split('.').map((p) => p.replace(/[_-]+/g, ' ').trim()).filter(Boolean)
  return parts.length ? parts.join(' · ') : id
}

export interface SubjectRef {
  /** The selection kind the frame's point takes, or null when it is not a record (the host, a route). */
  kind: SelectionKind | null
  id: string
  label: string
  /** The plain noun: "source", "unit", "subject", "the system". */
  noun: string
}

const SYSTEM_SUBJECTS: Record<string, string> = {
  route: 'a model route',
  platform: 'the platform',
  host: 'the host',
  deploy: 'a release',
  question: 'a forecast',
}

export function subjectOf(subjectKind: string, subjectId: string): SubjectRef {
  const id = subjectId ?? ''
  switch (subjectKind) {
    case 'source':
      return { kind: 'source', id, label: sourceLabel(id), noun: 'source' }
    case 'analyst':
      return { kind: 'analyst', id, label: humanizeId(id), noun: unitNoun.toLowerCase() }
    case 'target':
      return { kind: 'target', id, label: humanizeId(id), noun: PROFILE_UI.subject.singular.toLowerCase() }
    default:
      return { kind: null, id, label: SYSTEM_SUBJECTS[subjectKind] ?? 'the system', noun: 'system' }
  }
}

// ---------------------------------------------------------------------------
// The alert log.
// ---------------------------------------------------------------------------

/** The ladder, lowest first (docs/ALERTING.md). */
export const SEVERITIES = ['info', 'low', 'medium', 'high', 'critical'] as const
export type Severity = (typeof SEVERITIES)[number]

export function severityRank(s: string | null | undefined): number {
  const i = SEVERITIES.indexOf((s ?? '') as Severity)
  return i < 0 ? 2 : i // an unknown word reads as medium, as the ladder maps it
}

export function severityWord(s: string | null | undefined): string {
  const k = (s ?? '').toLowerCase()
  return SEVERITIES.includes(k as Severity) ? k.charAt(0).toUpperCase() + k.slice(1) : 'Medium'
}

/**
 * The reader's kind groups (the chips). `system` holds the operational kinds
 * (disk, the nightly suite, model routes, the pipeline): the reader filter
 * leaves them out; "all" shows them.
 */
export type AlertGroup = 'fired' | 'quiet' | 'changed' | 'desk' | 'forecast' | 'system'

export const ALERT_GROUP_LABEL: Record<AlertGroup, string> = {
  fired: 'Watch fired',
  quiet: 'Source quiet',
  changed: 'Changed after read',
  // The reader's noun (the profile's subject noun, as the other
  // reader pages say it), never the engine's "unit".
  desk: `${SUBJECT_NOUN} stalled`,
  forecast: 'Forecast resolved',
  system: 'System',
}

const GROUP_OF_KIND: Record<string, AlertGroup> = {
  source_cadence_stall: 'quiet',
  source_degraded: 'quiet',
  source_staleness_stale: 'quiet',
  source_staleness_dead: 'quiet',
  evidence_drift: 'changed',
  analyst_cadence_stall: 'desk',
  analyst_cursor_lag: 'desk',
  analyst_hard_fail: 'desk',
  production_deficit: 'desk',
  coverage_floor: 'desk',
  external_audit: 'desk',
  forecast_resolution: 'forecast',
}

/** The group of a kind: its own entry, else by the log's group (findings, baseline → fired). */
export function groupOf(kind: string, logGroup?: string | null): AlertGroup {
  const own = GROUP_OF_KIND[kind]
  if (own) return own
  if (logGroup === 'findings' || logGroup === 'baseline') return 'fired'
  return 'system'
}

const KIND_WORDS: Record<string, string> = {
  escalations: 'a statement escalated',
  verified_finding: 'a checked statement was published',
  band_crossing: 'a level changed',
  situation_escalation: 'a situation escalated',
  watchlist_hit: 'a watch matched',
  contention_flip: 'a contested statement flipped',
  geo_convergence: 'reports converged on one place',
  rollup: 'several alerts rolled into one',
  analyst_alert_output: `a ${unitNoun.toLowerCase()} raised an alert`,
  baseline_deviation: 'off its usual level',
  volume_surge: 'a surge of reports',
  source_cadence_stall: 'stopped arriving on schedule',
  source_degraded: 'its polls fail or come back empty',
  source_staleness_stale: 'is running late',
  source_staleness_dead: 'has gone quiet for good',
  evidence_drift: 'pages changed after they were read',
  analyst_cadence_stall: 'has not run on schedule',
  analyst_cursor_lag: 'is behind its input',
  analyst_hard_fail: 'its runs fail',
  production_deficit: 'produced less than it promises',
  coverage_floor: 'coverage fell below its floor',
  external_audit: 'an outside check flagged it',
  forecast_resolution: 'a forecast resolved',
  consult_unhealthy: 'questions are not being answered',
  liveness_stall: 'the pipeline stalled',
  alert_volume_high: 'unusually many alerts',
  reminder_orphan_collected: 'housekeeping',
  host_disk: 'the disk is filling',
  host_llm_heartbeat: 'the model host missed a heartbeat',
  host_search_canary: 'search failed its check',
  host_stall_recovered: 'a stall was recovered',
  nightly_suite: 'the nightly checks',
  llm_route_dead: 'a model route is down',
  llm_route_auth_failed: 'a model route refused its key',
  search_degraded: 'search is degraded',
}

/** A kind in plain words — never the kind id. */
export function kindWords(kind: string): string {
  return KIND_WORDS[kind] ?? kind.replace(/[_.]+/g, ' ').trim()
}

/** One rule entry as the log stores it: `kind >= min` or `kind >= min (subject pat)`. */
export function parseRuleEntry(entry: string | null | undefined): { min: string; subject: string | null } | null {
  if (!entry) return null
  const m = /^\s*\S+\s*>=\s*([a-z]+)(?:\s*\(subject\s+(.+)\))?\s*$/.exec(entry)
  return m ? { min: m[1], subject: m[2] ?? null } : null
}

export interface PageDecision {
  paged: boolean
  /** "Paged at high" / "Logged only". */
  words: string
  /** Which rule, in words: "this kind sends a notification at high or above". */
  rule: string
  /** For a paged alert: whether a push was delivered (null: not known). */
  delivered: boolean | null
}

function ruleLine(entry: string | null | undefined): string | null {
  const p = parseRuleEntry(entry)
  if (!p) return null
  return `this kind sends a notification at ${p.min} or above${p.subject ? ` for ${p.subject}` : ''}`
}

export function pageDecision(ep: AlertEpisode, kinds: ReadonlyMap<string, AlertKindInfo>, paged: readonly PagedAlert[]): PageDecision {
  const entries = kinds.get(ep.kind)?.pages_when ?? null
  if ((ep.paged_count ?? 0) > 0) {
    const page = paged.find((p) => p.alert_id === ep.id)
    const rule = ruleLine(page?.rule_entry) ?? (entries && entries.length ? ruleLine(entries[0]) : null)
    return {
      paged: true,
      words: `Paged${ep.paged_severity ? ` at ${ep.paged_severity}` : ''}`,
      rule: rule ?? 'it matched the notification settings',
      delivered: page ? page.delivered ?? (page.outcomes ?? []).includes('delivered') : null,
    }
  }
  if (entries == null) return { paged: false, words: 'Logged only', rule: 'the notification settings could not be read', delivered: null }
  if (entries.length === 0) {
    return { paged: false, words: 'Logged only', rule: 'no notification is set for this kind', delivered: null }
  }
  const floor = ruleLine(entries[0])
  return {
    paged: false,
    words: 'Logged only',
    rule: floor ? `${floor}; this reached ${ep.max_severity ?? ep.severity}` : 'below the level that sends a notification',
    delivered: null,
  }
}

export interface AlertRow {
  id: string
  kind: string
  group: AlertGroup
  groupLabel: string
  /** The subject, then what happened: "who · disease outbreak news — stopped arriving on schedule". */
  title: string
  /** What happened, in plain words (the title after the subject). */
  what: string
  subject: SubjectRef
  /** The producer's own line, shown second. */
  detail: string
  severity: Severity
  severityWord: string
  open: boolean
  /** "open since 14 Nov 17:30Z" / "closed 14 Nov 18:00Z — quiet for 6 h". */
  stateWords: string
  /** The time the row sorts by (its newest raise, or its close). */
  at: string
  occurrences: number
  decision: PageDecision
}

function closeWords(reason: string | null | undefined): string {
  if (!reason) return ''
  const q = /^quiet: no raise for (\d+(?:\.\d+)?) h$/.exec(reason)
  if (q) return `quiet for ${q[1]} h`
  if (/recover/i.test(reason)) return 'recovered'
  return reason.replace(/[_]+/g, ' ')
}

/** The log's rows: every open episode and every one closed in the window, newest first. */
export function alertRows(log: AlertLogResponse | null | undefined): AlertRow[] {
  if (!log || !log.measured) return []
  const kinds = new Map((log.kinds ?? []).map((k) => [k.kind, k]))
  const paged = log.paged ?? []
  const one = (ep: AlertEpisode, open: boolean): AlertRow => {
    const subject = subjectOf(ep.subject_kind, ep.subject_id)
    const group = groupOf(ep.kind, ep.group ?? kinds.get(ep.kind)?.group)
    const sev = SEVERITIES[severityRank(ep.max_severity ?? ep.severity)]
    const at = open ? ep.last_raised_at : ep.closed_at ?? ep.last_raised_at
    const closed = closeWords(ep.close_reason)
    return {
      id: ep.id,
      kind: ep.kind,
      group,
      groupLabel: ALERT_GROUP_LABEL[group],
      title: `${subject.label} — ${kindWords(ep.kind)}`,
      what: kindWords(ep.kind),
      subject,
      detail: ep.summary ?? '',
      severity: sev,
      severityWord: severityWord(sev),
      open,
      stateWords: open
        ? `open since ${whenText(ep.opened_at)}`
        : `closed ${whenText(ep.closed_at)}${closed ? ` — ${closed}` : ''}`,
      at,
      occurrences: ep.occurrences ?? 1,
      decision: pageDecision(ep, kinds, paged),
    }
  }
  const rows = [...(log.open ?? []).map((e) => one(e, true)), ...(log.closed ?? []).map((e) => one(e, false))]
  return rows.sort((a, b) => (Date.parse(b.at) || 0) - (Date.parse(a.at) || 0))
}

export interface AlertFilter {
  /** `reader` = every group but system; `all`; or one group. */
  group: 'reader' | 'all' | AlertGroup
  /** At least this severity, or any. */
  severity: 'any' | Severity
  paged: 'any' | 'paged' | 'logged'
  state: 'any' | 'open' | 'closed'
}

export const DEFAULT_ALERT_FILTER: AlertFilter = { group: 'reader', severity: 'any', paged: 'any', state: 'any' }

export function filterAlerts(rows: readonly AlertRow[], f: AlertFilter): AlertRow[] {
  const min = f.severity === 'any' ? -1 : severityRank(f.severity)
  return rows.filter(
    (r) =>
      (f.group === 'all' || (f.group === 'reader' ? r.group !== 'system' : r.group === f.group)) &&
      severityRank(r.severity) >= min &&
      (f.paged === 'any' || (f.paged === 'paged') === r.decision.paged) &&
      (f.state === 'any' || (f.state === 'open') === r.open),
  )
}

/** Counts over the rows the reader filter would show, per group (the chips' figures). */
export function groupCounts(rows: readonly AlertRow[]): Record<AlertGroup, number> {
  const out: Record<AlertGroup, number> = { fired: 0, quiet: 0, changed: 0, desk: 0, forecast: 0, system: 0 }
  for (const r of rows) out[r.group]++
  return out
}

// ---------------------------------------------------------------------------
// Silences.
// ---------------------------------------------------------------------------

/** The one line that matters on this region: looked and saw nothing, or could not look. */
export type QuietSide = 'looked' | 'could_not_look' | 'unknown'

export const QUIET_SIDE_LABEL: Record<QuietSide, string> = {
  looked: 'Looked, saw nothing',
  could_not_look: 'Could not look',
  unknown: 'Cannot tell yet',
}

export interface QuietClassDef {
  side: QuietSide
  title: string
  detail: string
}

/** The server's closed list (`source_quiet.QUIET_CLASSES`) in plain words. */
export const QUIET_CLASSES: Record<string, QuietClassDef> = {
  published_nothing: {
    side: 'looked',
    title: 'Nothing new published',
    detail: 'The feed answered; its newest entry is no newer than what was already read.',
  },
  filtered_to_nothing: {
    side: 'looked',
    title: 'Filtered to nothing',
    detail: 'The feed answered; nothing in it passed the topic filter.',
  },
  polling_fails: {
    side: 'could_not_look',
    title: 'Polling fails',
    detail: 'The latest polls failed, so nothing could be read.',
  },
  published_not_kept: {
    side: 'could_not_look',
    title: 'Published, nothing taken in',
    detail: 'The feed shows newer entries than anything read here; they were not taken in.',
  },
  never_produced: {
    side: 'unknown',
    title: 'Never produced anything',
    detail: 'Nothing from this source has ever been read, so quiet cannot be told from a fault yet.',
  },
  unrecorded: {
    side: 'unknown',
    title: 'Quiet, reason not recorded',
    detail: 'The latest poll left nothing to tell whether the feed published.',
  },
}

export interface SilenceRow {
  id: string
  /** `source` or the unit noun. */
  noun: string
  ref: SubjectRef
  quietClass: string
  def: QuietClassDef
  /** "last item 3 d ago" / "nothing ever read". */
  since: string
}

/**
 * The class of a firing row: the server's `quiet_class` when it sent one;
 * otherwise (a registry not yet rebuilt) the coarser reading the older fields
 * allow — it can name a failing poll, a filtered poll and a source that never
 * produced, but it cannot split "published nothing" from "published, nothing
 * taken in", so those read as unrecorded rather than as a guess.
 */
export function quietClassOf(r: FiringRow): string | null {
  if (r.state && r.state !== 'active') return null
  if (r.status !== 'silent' && r.status !== 'error') return null
  if (r.quiet_class !== undefined) return r.quiet_class && QUIET_CLASSES[r.quiet_class] ? r.quiet_class : r.quiet_class ? 'unrecorded' : null
  if (r.status === 'error' || r.last_poll_outcome === 'error') return 'polling_fails'
  if (r.last_poll_outcome === 'gated') return 'filtered_to_nothing'
  if (!r.last_seen_at) return 'never_produced'
  return 'unrecorded'
}

const SIDE_ORDER: Record<QuietSide, number> = { could_not_look: 0, unknown: 1, looked: 2 }

export function sourceSilences(rows: readonly FiringRow[] | null | undefined, now: number): SilenceRow[] {
  const out: SilenceRow[] = []
  for (const r of rows ?? []) {
    const cls = quietClassOf(r)
    if (!cls) continue
    const ref = subjectOf('source', r.source_id)
    out.push({
      id: `source:${r.source_id}`,
      noun: 'source',
      ref,
      quietClass: cls,
      def: QUIET_CLASSES[cls],
      since: r.last_seen_at ? `last item ${agoText(r.last_seen_at, now)}` : 'nothing ever read',
    })
  }
  return out.sort((a, b) => SIDE_ORDER[a.def.side] - SIDE_ORDER[b.def.side] || a.ref.label.localeCompare(b.ref.label))
}

/** The unit kinds of the alert log that make a unit quiet, and on which side. */
const UNIT_QUIET: Record<string, QuietClassDef> = {
  analyst_cadence_stall: { side: 'could_not_look', title: 'Has not run on schedule', detail: 'Its scheduled runs did not happen.' },
  analyst_hard_fail: { side: 'could_not_look', title: 'Its runs fail', detail: 'It ran and failed, so it read nothing.' },
  analyst_cursor_lag: { side: 'could_not_look', title: 'Behind its input', detail: 'It runs, but has not reached the newest items.' },
  production_deficit: { side: 'looked', title: 'Ran, produced less than it promises', detail: 'It ran on schedule and wrote less than its own record says it should.' },
}

/** Units that are quiet: the OPEN unit episodes of the log (no extra read). */
export function unitSilences(log: AlertLogResponse | null | undefined, now: number): SilenceRow[] {
  if (!log || !log.measured) return []
  const out: SilenceRow[] = []
  const seen = new Set<string>()
  for (const ep of log.open ?? []) {
    if (ep.subject_kind !== 'analyst') continue
    const def = UNIT_QUIET[ep.kind]
    if (!def || seen.has(ep.subject_id)) continue
    seen.add(ep.subject_id)
    const ref = subjectOf('analyst', ep.subject_id)
    out.push({ id: `unit:${ep.subject_id}`, noun: ref.noun, ref, quietClass: ep.kind, def, since: `since ${agoText(ep.opened_at, now)}` })
  }
  return out.sort((a, b) => SIDE_ORDER[a.def.side] - SIDE_ORDER[b.def.side] || a.ref.label.localeCompare(b.ref.label))
}

export interface FiringCensus {
  active: number
  quiet: number
  paused: number
}

/** The region's one line of figures, from the same rows. */
export function firingCensus(rows: readonly FiringRow[] | null | undefined): FiringCensus {
  let active = 0
  let quiet = 0
  let paused = 0
  for (const r of rows ?? []) {
    if (r.state && r.state !== 'active') {
      paused++
      continue
    }
    active++
    if (quietClassOf(r)) quiet++
  }
  return { active, quiet, paused }
}

// ---------------------------------------------------------------------------
// Source edits.
// ---------------------------------------------------------------------------

const CHANGE_TYPE_WORDS: Record<string, string> = {
  spelling_or_name_correction: 'a spelling or a name corrected',
  update_new_facts: 'updated with new facts',
  clarification: 'clarified',
  attribution_change: 'who said it changed',
  reframing: 'reframed',
  claim_removed: 'a claim removed',
  claim_reversed: 'a claim reversed',
  fact_corrected: 'a fact corrected',
  retraction: 'retracted',
  page_gone: 'the page is gone',
  content_replaced: 'the page was replaced',
  mixed: 'several kinds of change',
  unassessable: 'the change could not be assessed',
}

const DRIFT_CLASS_WORDS: Record<string, string> = {
  identical_text: 'no change to the text',
  cosmetic: 'page furniture only',
  appended: 'text added, nothing removed',
  minor_edit: 'a small edit',
  title_changed: 'the headline changed',
  substantive_edit: 'the text changed in substance',
  retracted_or_replaced: 'retracted or replaced',
  gone: 'the page is gone',
}

/** What changed, in plain words: the assessed type when there is one, else the measured class. */
export function changeWords(f: Pick<DriftFlag, 'change_type' | 'drift_class'>): string {
  if (f.change_type && CHANGE_TYPE_WORDS[f.change_type]) return CHANGE_TYPE_WORDS[f.change_type]
  if (f.drift_class && DRIFT_CLASS_WORDS[f.drift_class]) return DRIFT_CLASS_WORDS[f.drift_class]
  return 'changed'
}

export const ACK_WORDS: Record<string, string> = {
  correction_notice: 'with a correction notice',
  visible_update_stamp: 'with a visible "updated" stamp',
  metadata_date_only: 'only a hidden date changed',
  silent: 'without saying so',
}

/** How the source acknowledged the change; '' when not yet assessed. */
export function ackWords(ack: string | null | undefined): string {
  if (!ack) return 'not assessed yet'
  return ACK_WORDS[ack] ?? ack.replace(/_/g, ' ')
}

const EFFECT_WORDS: Record<string, string> = {
  cited_text_survives: 'the passage relied on is still there',
  cited_text_gone: 'the passage relied on is gone',
  cited_text_changed: 'the passage relied on changed',
  not_checkable: 'the passage relied on could not be checked',
}

export function effectWords(effect: string | null | undefined): string {
  return effect ? EFFECT_WORDS[effect] ?? effect.replace(/_/g, ' ') : ''
}

/** What relied on the page, as a point the frame can take. */
export function dependantOf(f: Pick<DriftFlag, 'dependant_kind' | 'dependant_id'>): SubjectRef | null {
  if (!f.dependant_id) return null
  switch (f.dependant_kind) {
    case 'finding':
    case 'claim':
      return { kind: 'finding', id: f.dependant_id, label: 'a statement', noun: 'statement' }
    case 'signal':
      return { kind: 'signal', id: f.dependant_id, label: 'an item of evidence', noun: 'evidence' }
    case 'event':
      return { kind: 'event', id: f.dependant_id, label: 'an event', noun: 'event' }
    default:
      return { kind: null, id: f.dependant_id, label: 'a record', noun: 'record' }
  }
}

/** The page's host and path, short: `example.org/news/item-1`. */
export function pageLabel(address: string): string {
  try {
    const u = new URL(address)
    const path = u.pathname.length > 48 ? `${u.pathname.slice(0, 47)}…` : u.pathname
    return `${u.host}${path === '/' ? '' : path}`
  } catch {
    return address.length > 60 ? `${address.slice(0, 59)}…` : address
  }
}

export interface FlagRow {
  id: string
  page: string
  address: string
  source: SubjectRef | null
  change: string
  ack: string
  effect: string
  reliedOn: SubjectRef | null
  /** "read 12 Nov 09:10Z · changed by 13 Nov 07:02Z". */
  when: string
  historyRewrite: boolean
  /** The auto-close rule WOULD close this (a shadow count; the switch is off). */
  wouldAutoClose: boolean
  passages: { earlier: string; later: string }[]
  removed: string[]
}

export function flagRows(flags: readonly DriftFlag[] | null | undefined): FlagRow[] {
  return (flags ?? []).map((f) => ({
    id: String(f.id),
    page: pageLabel(f.address),
    address: f.address,
    source: f.source_id ? subjectOf('source', f.source_id) : null,
    change: changeWords(f),
    ack: ackWords(f.acknowledged),
    effect: effectWords(f.claim_effect),
    reliedOn: dependantOf(f),
    when: [f.earlier_fetched_at ? `read ${whenText(f.earlier_fetched_at)}` : '', f.later_fetched_at ? `changed by ${whenText(f.later_fetched_at)}` : '']
      .filter(Boolean)
      .join(' · '),
    historyRewrite: !!f.history_rewrite,
    wouldAutoClose: !!f.would_auto_close,
    passages: (f.changed ?? []).map((p) => ({ earlier: p.earlier ?? '', later: p.later ?? '' })),
    removed: f.removed ?? [],
  }))
}

export interface EditRecordRow {
  id: string
  label: string
  /** The 30-day window (the 7-day one beside it when present). */
  rechecked: number | null
  assessed: number | null
  notice: number | null
  stamp: number | null
  dateOnly: number | null
  silent: number | null
  rewrites: number | null
  /** The 7-day figures, same order. */
  week: { rechecked: number | null; assessed: number | null }
}

const n = (v: number | undefined | null): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/**
 * The per-source edit record as a TRACKED MEASURE: sorted by the source's
 * name — never by how much it edits (the route's own order is a rank, and a
 * rank would read as a verdict on the source).
 */
export function editRecordRows(rows: readonly DriftEditorRow[] | null | undefined): EditRecordRow[] {
  return (rows ?? [])
    .map((r) => {
      const m: EditWindow = r.edit_record?.['30d'] ?? {}
      const w: EditWindow = r.edit_record?.['7d'] ?? {}
      const ack = m.acknowledged ?? {}
      return {
        id: r.subject,
        label: sourceLabel(r.subject),
        rechecked: n(m.pages_rechecked),
        assessed: n(m.assessed),
        notice: n(ack.correction_notice),
        stamp: n(ack.visible_update_stamp),
        dateOnly: n(ack.metadata_date_only),
        silent: n(ack.silent),
        rewrites: n(m.history_rewrite),
        week: { rechecked: n(w.pages_rechecked), assessed: n(w.assessed) },
      }
    })
    .sort((a, b) => a.label.localeCompare(b.label))
}
