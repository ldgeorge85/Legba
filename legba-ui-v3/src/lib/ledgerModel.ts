/**
 * The forecast ledger's read model (ST0-C) — `GET /api/v1/v3/ledger/forecasts`.
 *
 * The Ledger tab shows every forecast question sorted by what resolves next:
 * open questions first by due date, then closed ones. Each row carries one
 * cell per ARM (the operator, the scheduled tower, the consult, the frontier
 * comparison arm, the Clef arm), the sealed/open state, the due date and the
 * resolution. An arm that stated no number is an ABSENCE with its reason
 * (`absent`, the reason on hover), never a blank; an arm with no row at all
 * is `—` ("not stated"). The Brier column is ST1's: until a resolution exists
 * and ST1's scoring lands it reads a placeholder, never a number.
 *
 * Pure: the panel renders `ledgerTableRows(...)`; this file is the unit-tested
 * mapping.
 */

export interface LedgerNumber {
  issuer_kind: string
  issuer_ref: string
  p: number | null
  absent_reason?: string | null
  as_of?: string | null
  issued_at?: string | null
  rationale?: string | null
  rationale_ref?: string | null
  sealed: boolean
}

export interface LedgerResolution {
  id: string
  outcome: 'yes' | 'no' | 'void' | string
  resolved_at?: string | null
  resolved_by?: string | null
  evidence_refs?: string[]
  early?: boolean
  note?: string | null
}

export interface LedgerForecast {
  id: string
  slug: string
  question: string
  question_class?: string | null
  resolver_kind: string
  resolver_spec?: unknown
  machine_resolvable?: boolean
  window_start?: string | null
  window_end?: string | null
  due_at: string
  grace_hours?: number
  condition?: string | null
  void_rules?: string | null
  exogenous?: boolean
  subject_keys?: string[]
  status: 'open' | 'resolved' | 'void' | string
  sealed_at?: string | null
  /** The seal's hash over the question's content (`forecast_ledger.ForecastQuestion.content`). */
  content_sha256?: string | null
  overdue?: boolean
  numbers: LedgerNumber[]
  resolution?: LedgerResolution | null
}

export interface LedgerForecastsResponse {
  version: string
  ledger_version?: string
  available: boolean
  issuer_kinds?: string[]
  resolver_kinds?: string[]
  forecasts: LedgerForecast[]
}

export const LEDGER_ROUTE = '/v3/ledger/forecasts'

/** The arm columns, in reading order. `think_tank` arrives with ST2. */
export const ARM_COLUMNS: readonly { kind: string; label: string }[] = [
  { kind: 'operator', label: 'Operator' },
  { kind: 'tower', label: 'Tower' },
  { kind: 'consult', label: 'Consult' },
  { kind: 'frontier_arm', label: 'Frontier' },
  { kind: 'clef_arm', label: 'Clef' },
]

/** The Brier column's text until ST1 scores resolutions. */
export const BRIER_PLACEHOLDER = 'Brier (from resolution)'

export interface ArmCell {
  kind: string
  /** `0.25` · `absent` · `—` */
  text: string
  state: 'number' | 'absent' | 'not_stated'
  /** Hover text: the reason, the rationale, the issuer ref. */
  title: string
  /** More than one row of this kind (seats, several refs). */
  count: number
}

export interface LedgerTableRow {
  id: string
  slug: string
  question: string
  resolverKind: string
  machineResolvable: boolean
  dueAt: string
  /** `in 12 d` · `due today` · `3 d past due` · `—` for closed rows. */
  dueIn: string
  state: 'open' | 'sealed' | 'resolved' | 'void' | 'overdue'
  stateLabel: string
  arms: ArmCell[]
  resolution: string
  brier: string
  condition: string | null
}

const DAY_MS = 86_400_000

function fmtP(p: number): string {
  return p.toFixed(2)
}

export function armCell(kind: string, numbers: LedgerNumber[]): ArmCell {
  const mine = numbers.filter((n) => n.issuer_kind === kind)
  if (mine.length === 0) return { kind, text: '—', state: 'not_stated', title: 'no number stated', count: 0 }
  const stated = mine.filter((n) => typeof n.p === 'number')
  if (stated.length === 0) {
    const reason = mine.map((n) => n.absent_reason).filter(Boolean).join(' · ')
    return { kind, text: 'absent', state: 'absent', title: reason || 'absent', count: mine.length }
  }
  const first = stated[0]
  const text = stated.length > 1 ? `${fmtP(first.p as number)} (+${stated.length - 1})` : fmtP(first.p as number)
  const title = [first.issuer_ref, first.rationale, first.sealed ? 'sealed' : 'not sealed']
    .filter(Boolean)
    .join(' — ')
  return { kind, text, state: 'number', title, count: mine.length }
}

export function dueIn(dueAt: string, now: Date): string {
  const due = Date.parse(dueAt)
  if (Number.isNaN(due)) return '—'
  const days = Math.floor((due - now.getTime()) / DAY_MS)
  if (days > 0) return `in ${days} d`
  if (days === 0) return 'due today'
  return `${-days} d past due`
}

function stateOf(f: LedgerForecast): LedgerTableRow['state'] {
  if (f.status === 'resolved') return 'resolved'
  if (f.status === 'void') return 'void'
  if (f.overdue) return 'overdue'
  return f.sealed_at ? 'sealed' : 'open'
}

const STATE_LABEL: Record<LedgerTableRow['state'], string> = {
  open: 'open · not sealed',
  sealed: 'sealed',
  resolved: 'resolved',
  void: 'void',
  overdue: 'overdue · awaiting resolution',
}

function resolutionText(r: LedgerResolution | null | undefined): string {
  if (!r) return '—'
  const when = r.resolved_at ? r.resolved_at.slice(0, 10) : ''
  return `${r.outcome.toUpperCase()}${when ? ` · ${when}` : ''}${r.early ? ' · early' : ''}`
}

/** Open first by due date (what resolves next), then the closed rows by due date. */
export function sortForecasts(rows: LedgerForecast[]): LedgerForecast[] {
  const rank = (f: LedgerForecast) => (f.status === 'open' ? 0 : 1)
  return [...rows].sort(
    (a, b) => rank(a) - rank(b) || a.due_at.localeCompare(b.due_at) || a.slug.localeCompare(b.slug),
  )
}

export function ledgerTableRows(res: LedgerForecastsResponse | null | undefined, now: Date): LedgerTableRow[] {
  if (!res || !Array.isArray(res.forecasts)) return []
  return sortForecasts(res.forecasts).map((f) => {
    const state = stateOf(f)
    return {
      id: f.id,
      slug: f.slug,
      question: f.question,
      resolverKind: f.resolver_kind,
      machineResolvable: !!f.machine_resolvable,
      dueAt: f.due_at.slice(0, 10),
      dueIn: f.status === 'open' ? dueIn(f.due_at, now) : '—',
      state,
      stateLabel: STATE_LABEL[state],
      arms: ARM_COLUMNS.map((c) => armCell(c.kind, f.numbers ?? [])),
      resolution: resolutionText(f.resolution),
      // ST1 computes the score from the resolution; nothing is printed before it.
      brier: f.resolution && f.resolution.outcome !== 'void' ? 'not scored yet' : '—',
      condition: f.condition ?? null,
    }
  })
}

/** The one-line header: counts by state, and what resolves next. */
export function ledgerSummary(rows: LedgerTableRow[]): string {
  if (rows.length === 0) return 'no forecasts on the ledger'
  const open = rows.filter((r) => r.state === 'open' || r.state === 'sealed' || r.state === 'overdue')
  const sealed = rows.filter((r) => r.state === 'sealed').length
  const next = open[0]
  return [
    `${rows.length} forecasts`,
    `${open.length} open (${sealed} sealed)`,
    next ? `next: ${next.slug} ${next.dueIn}` : null,
  ]
    .filter(Boolean)
    .join(' · ')
}
