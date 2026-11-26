/**
 * The scorecard's read model — `GET /api/v1/v3/ledger/scorecard`.
 *
 * Reads only the sealed forecast ledger. Pure projections: the panel renders
 * these. Rules: an absent number is a dash with its reason, never blank; an
 * arm below its minimum sample reads "n too small", never 0; calibration that
 * is unavailable shows its reason, never a table of zeros.
 */

export const SCORECARD_ROUTE = '/v3/ledger/scorecard'

export interface ScorecardNumber {
  issuer_kind: string
  issuer_ref?: string | null
  p: number | null
  absent_reason?: string | null
  brier?: number | null
}
export interface ScorecardResolution {
  id?: string
  outcome: string
  resolved_at?: string | null
  resolved_by?: string | null
  evidence_refs?: string[]
  note?: string | null
}
export interface ScorecardProposal {
  record_id?: string
  outcome: string
  recorded_at?: string | null
  actor?: string | null
  record_kind?: string | null
  /** The role that made an act through the console (owner | admin | contributor); null for the rule and the host script. */
  actor_role?: string | null
  /** How many evidence items the proposal cites (the items: `GET /v3/ledger/resolution-records`). */
  evidence_count?: number
  judgements?: { key: string; question: string }[]
}
export interface ScorecardQuestion {
  id: string
  slug: string
  question: string
  status: 'open' | 'resolved' | 'void' | string
  resolver_kind: string
  due_at: string
  grace_hours?: number
  grace_until?: string | null
  overdue?: boolean
  sealed_at?: string | null
  single_arm?: boolean
  numbers: ScorecardNumber[]
  spread?: { n: number; min: number; max: number; range: number } | null
  baselines?: { name: string; p: number | null; brier: number | null }[]
  outcome?: string | null
  resolution?: ScorecardResolution | null
  proposal?: ScorecardProposal | null
  /** The newest act on the question (confirmation, override, correction). */
  latest_act?: ScorecardProposal | null
}
export interface ScorecardArm {
  issuer_kind: string
  n: number
  mean_brier: number | null
  baseline_mean_brier: number | null
  skill_vs_climatology: number | null
  below_min_sample: boolean
  note?: string | null
}
export interface ScorecardCalibration {
  available: boolean
  min_sample: number
  n: number
  reason?: string | null
  bins?: { lo: number; hi: number; n: number; mean_p: number | null; observed: number | null }[]
  /** Whose numbers `bins` holds (one issuer, never a pool). */
  bins_arm?: string | null
}
export interface ScorecardHeader {
  n_questions: number
  n_sealed: number
  n_open: number
  n_resolved: number
  n_void: number
  n_overdue: number
  min_sample: number
  calibration_min_sample?: number
  skill_claim: boolean
  statement: string
  scores_withheld?: boolean
  scores_min_resolved?: number
}
export interface ScorecardResponse {
  version?: string
  ledger_version?: string
  scoring_version?: string
  available: boolean
  as_of?: string
  /** True until `scores_min_resolved` questions have resolved: no Brier figure anywhere. */
  scores_withheld?: boolean
  scores_min_resolved?: number
  header?: ScorecardHeader
  separation_note?: string
  questions?: ScorecardQuestion[]
  arms?: ScorecardArm[]
  single_arm?: { slug: string; issuer_kind: string; p: number | null; brier: number | null; outcome: string | null }[]
  calibration?: ScorecardCalibration
  due_next?: { slug: string; due_at: string; grace_until?: string | null; resolver_kind: string; status: string }[]
  void_questions?: { slug: string; reason: string }[]
}

export const SEPARATION_LABEL = 'Sealed ledger only — not the legacy calibration figures'
export const NOT_DEPLOYED = 'scorecard route not deployed'

const ARM_ORDER = ['operator', 'tower', 'consult', 'synthesis', 'seat', 'actor_model', 'frontier_arm', 'clef_arm']
const DASH = '—'

export function fmtP(v: number | null | undefined): string {
  return v == null ? DASH : v.toFixed(2)
}
export function fmtBrier(v: number | null | undefined): string {
  return v == null ? DASH : v.toFixed(3)
}

/** No Brier figure is shown unless the route says scores are NOT withheld. */
export function scoresWithheld(p: ScorecardResponse | null | undefined): boolean {
  return p?.scores_withheld !== false
}
export function scoresMin(p: ScorecardResponse | null | undefined): number {
  return p?.scores_min_resolved ?? p?.header?.scores_min_resolved ?? 5
}
export function withheldLine(p: ScorecardResponse | null | undefined): string {
  return `${p?.header?.n_resolved ?? 0} resolved; scores appear at ${scoresMin(p)}`
}

/** Issuer kinds present in the questions' numbers, in the canonical order. */
export function scorecardArmColumns(p: ScorecardResponse | null | undefined): string[] {
  if (!p) return []
  const seen = new Set<string>()
  for (const q of p.questions ?? []) for (const n of q.numbers ?? []) seen.add(n.issuer_kind)
  const known = ARM_ORDER.filter((k) => seen.has(k))
  const others = [...seen].filter((k) => !ARM_ORDER.includes(k)).sort()
  return [...known, ...others]
}

export interface ScorecardCell {
  kind: string
  text: string
  title: string
  brier: string | null
  state: 'number' | 'absent' | 'none'
}
export interface ScorecardRow {
  id: string
  slug: string
  question: string
  status: string
  statusLabel: string
  dueAt: string
  overdue: boolean
  singleArm: boolean
  cells: ScorecardCell[]
  spread: string
  outcome: string
  proposed: boolean
  needsJudgement: boolean
  evidence: { count: number; urls: string[]; others: string[] }
}

function day(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : DASH
}

export function scorecardRows(p: ScorecardResponse | null | undefined, now: Date): ScorecardRow[] {
  if (!p || p.available === false) return []
  const cols = scorecardArmColumns(p)
  const withheld = scoresWithheld(p)
  const rows = (p.questions ?? []).map((q): ScorecardRow => {
    const cells = cols.map((kind): ScorecardCell => {
      const n = q.numbers.find((x) => x.issuer_kind === kind)
      if (!n) return { kind, text: DASH, title: 'no number from this arm', brier: null, state: 'none' }
      if (n.p == null) return { kind, text: DASH, title: n.absent_reason || 'absent', brier: null, state: 'absent' }
      return {
        kind,
        text: fmtP(n.p),
        title: n.issuer_ref ?? kind,
        brier: !withheld && q.status === 'resolved' && n.brier != null ? fmtBrier(n.brier) : null,
        state: 'number',
      }
    })
    const refs = q.resolution?.evidence_refs ?? []
    const urls = refs.filter((r) => r.startsWith('url:')).map((r) => r.slice(4))
    const others = refs.filter((r) => !r.startsWith('url:'))
    const proposed = !q.outcome && !q.resolution && !!q.proposal
    const needsJudgement = proposed && (q.proposal?.judgements?.length ?? 0) > 0
    const outcome = proposed ? q.proposal!.outcome : (q.outcome ?? q.resolution?.outcome ?? DASH)
    const due = new Date(q.due_at)
    const overdue = q.overdue === true || (q.status === 'open' && !isNaN(due.getTime()) && due < now)
    return {
      id: q.id,
      slug: q.slug,
      question: q.question,
      status: q.status,
      statusLabel: overdue && q.status === 'open' ? 'overdue' : q.status,
      dueAt: day(q.due_at),
      overdue,
      singleArm: q.single_arm === true,
      cells,
      spread: q.spread ? q.spread.range.toFixed(3) : DASH,
      outcome,
      proposed,
      needsJudgement,
      evidence: { count: refs.length, urls, others },
    }
  })
  const rank = (r: ScorecardRow) => (r.status === 'open' ? 0 : 1)
  return rows.sort((a, b) => rank(a) - rank(b) || a.dueAt.localeCompare(b.dueAt) || a.slug.localeCompare(b.slug))
}

/** "n too small (0 < 5)" vs "0.123 (n=7)". Never 0, never blank. */
export function armCell(arm: ScorecardArm, minSample = 5): string {
  if (arm.below_min_sample || arm.mean_brier == null) {
    return arm.note && arm.note.startsWith('n too small') ? arm.note : `n too small (${arm.n} < ${minSample})`
  }
  return `${arm.mean_brier.toFixed(3)} (n=${arm.n})`
}

export interface ArmRowView {
  kind: string
  n: number
  mean: string
  baseline: string
  skill: string | null
}
export function armRows(p: ScorecardResponse | null | undefined): ArmRowView[] {
  if (!p) return []
  const min = p.header?.min_sample ?? 5
  const withheld = scoresWithheld(p)
  return (p.arms ?? []).map((a) => {
    if (withheld) return { kind: a.issuer_kind, n: a.n, mean: `scores appear at ${scoresMin(p)}`, baseline: DASH, skill: null }
    const small = a.below_min_sample || a.mean_brier == null
    return {
      kind: a.issuer_kind,
      n: a.n,
      mean: armCell(a, min),
      baseline: small || a.baseline_mean_brier == null ? DASH : a.baseline_mean_brier.toFixed(3),
      skill: small || a.skill_vs_climatology == null ? null : a.skill_vs_climatology.toFixed(3),
    }
  })
}

export interface CalibrationView {
  available: boolean
  reason: string
  bins: { range: string; n: number; meanP: string; observed: string }[]
}
export function calibrationView(p: ScorecardResponse | null | undefined): CalibrationView {
  const c = p?.calibration
  if (!c) return { available: false, reason: 'calibration not reported', bins: [] }
  if (!c.available) return { available: false, reason: c.reason || `n too small (${c.n} < ${c.min_sample})`, bins: [] }
  return {
    available: true,
    reason: '',
    bins: (c.bins ?? []).map((b) => ({
      range: `${b.lo.toFixed(1)}–${b.hi.toFixed(1)}`,
      n: b.n,
      meanP: fmtP(b.mean_p),
      observed: fmtP(b.observed),
    })),
  }
}

export interface HeaderView {
  statement: string
  resolvedText: string
  nResolved: number
  skillClaim: boolean
  noClaimMarker: string | null
}
export function headerView(p: ScorecardResponse | null | undefined): HeaderView {
  const h = p?.header
  if (!h) return { statement: '', resolvedText: '0 resolved', nResolved: 0, skillClaim: false, noClaimMarker: null }
  return {
    statement: h.statement,
    resolvedText: `${h.n_resolved} resolved`,
    nResolved: h.n_resolved,
    skillClaim: h.skill_claim,
    noClaimMarker: scoresWithheld(p) ? `scores appear at ${scoresMin(p)}` : null,
  }
}

export type ScorecardState = 'not_deployed' | 'unavailable' | 'ready'
export function scorecardState(p: ScorecardResponse | null | undefined): ScorecardState {
  if (p == null) return 'not_deployed'
  if (p.available === false) return 'unavailable'
  return 'ready'
}
