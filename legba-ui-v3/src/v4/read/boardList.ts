/**
 * boardList — the Board's ranked list as data: what changed since the reader
 * last looked, and the same list cut three other ways (what was checked, the
 * gaps, what is due). Every row has the same five parts:
 *
 *   subject    the profile's name for it, never a machine id
 *   sentence   one sentence of what changed (rendered by `claimText.tsx`)
 *   when       the instant the row's own record carries
 *   trust      at most two plain words, each with its number where it has one
 *   open       the record the row opens in the Record sheet (null: none)
 *
 * The rows are cut from the reads the daily read already makes
 * (`lib/morningReadBands.ts`): nothing is ranked or counted here that those
 * reads do not say. Pure; the component is `BoardList.tsx`.
 */
import { whyNot } from '@/lib/claimsModel'
import { findingTrust, type TrustWord } from '@/lib/trustWords'
import { ABSENCE_KIND_LABEL, absenceId } from '@/lib/absenceModel'
import { humanizeId } from '@/lib/deskNames'
import { isVerifyExemptState, unverifiedStateOf } from '@/lib/unverifiedState'
import {
  changedList,
  deskLabel,
  UNSCOPED_DESK,
  type ChangedRow,
  type DeskGapView,
  type ForecastDue,
  type JudgmentFindingRow,
  type JudgmentSince,
} from '@/lib/morningReadBands'
import { plainIds } from './claimText'

// The trust words live in `lib/trustWords.ts`, shared with every place a
// finding's trust is shown; re-exported here for the list's callers.
export { findingTrust, type TrustWord }

export type BoardFilter = 'changed' | 'checked' | 'gaps' | 'due'

export const BOARD_FILTERS: readonly { id: BoardFilter; label: string; question: string }[] = [
  { id: 'changed', label: 'Changed', question: 'What landed since you last looked, worst first.' },
  { id: 'checked', label: 'Checked', question: 'Which claims held when they were checked, and which did not.' },
  { id: 'gaps', label: 'Gaps', question: 'Where the evidence ran out: looked and could not see, or not collected.' },
  { id: 'due', label: 'Due', question: 'Calls whose window closed with no outcome recorded.' },
]

/** Rows the list shows before "more". */
export const BOARD_ROWS_SHOWN = 8

export interface BoardRow {
  key: string
  subjectId: string | null
  subject: string
  /** Raw text: markup is handled where it is rendered (`claimText.tsx`). */
  sentence: string
  at: string | null
  /** At most two. */
  trust: TrustWord[]
  severity: string | null
  open: { rowKind: string; id: string; label: string } | null
  /** A longer text for the row's title (a frozen test, a reason). */
  detail?: string
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

function capital(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s
}

// ---------------------------------------------------------------------------
// The four cuts
// ---------------------------------------------------------------------------

/** A machine-shaped headline said in words (alerts and band crossings carry ids). */
function changedSentence(r: ChangedRow): string {
  if (r.kind === 'band') {
    const m = /^([^:]+):\s*(\S+)\s*→\s*(\S+)$/.exec(r.headline)
    if (m) return `${capital(plainIds(m[1]))} moved from ${plainIds(m[2])} to ${plainIds(m[3])}`
  }
  if (r.kind === 'alert') {
    const verified = /^Verified [a-z]+-severity finding:\s*([\s\S]+)$/.exec(r.headline)
    if (verified) return verified[1]
    const esc = /^Situation escalated:\s*([\s\S]+?)\s*\((\S+)\s*->\s*(\S+)\)$/.exec(r.headline)
    if (esc) return `${esc[1]} escalated from ${plainIds(esc[2])} to ${plainIds(esc[3])}`
  }
  if (r.kind === 'situation') return `${r.headline}: ${r.note.replace(/^situation /, '')}`
  return r.headline
}

function subjectOf(deskId: string): string {
  return deskId === UNSCOPED_DESK ? deskLabel(UNSCOPED_DESK) : humanizeId(deskId)
}

/** CHANGED — the daily read's ranked list, as it ranks it. */
export function changedRows(
  since: JudgmentSince | null | undefined,
  checks: ReadonlyMap<string, JudgmentFindingRow>,
): BoardRow[] {
  return changedList(since).map((r) => ({
    key: `${r.kind}:${r.id}`,
    subjectId: r.targetId,
    subject: subjectOf(r.deskId),
    sentence: changedSentence(r),
    at: r.at,
    trust:
      r.kind === 'finding'
        ? findingTrust(checks.get(r.id), r.alerts.some((a) => a.channel === 'verified_finding'))
        : [],
    severity: r.severity,
    open: { rowKind: r.rowKind, id: r.id, label: r.headline },
  }))
}

/**
 * CHECKED — every finding in the window that the check has an answer on, or
 * owes one: flagged first (most flagged claims first), then the ones that
 * held, then the ones not checked; newest first inside each. A flagged
 * finding's sentence is its first flagged claim.
 */
export function checkedRows(rows: readonly JudgmentFindingRow[] | null | undefined): BoardRow[] {
  if (!Array.isArray(rows)) return []
  const ranked: { row: BoardRow; tier: number; weight: number; t: number }[] = []
  for (const f of rows) {
    const unverified = unverifiedStateOf(f)
    if (unverified && isVerifyExemptState(unverified)) continue
    const flagged = unverified ? null : whyNot(f.verification)
    const tier = flagged ? 0 : unverified ? 2 : 1
    const target = f.target_id && f.target_id.trim() !== '' ? f.target_id : UNSCOPED_DESK
    ranked.push({
      tier,
      weight: flagged ? flagged.unsupportedSpans.length : 0,
      t: Date.parse(f.produced_at) || 0,
      row: {
        key: `finding:${f.id}`,
        subjectId: f.target_id ?? null,
        subject: subjectOf(target),
        sentence: flagged ? flagged.unsupportedSpans[0].text : f.title,
        at: f.produced_at,
        trust: findingTrust(f),
        severity: f.severity,
        open: { rowKind: 'finding', id: f.id, label: f.title },
        ...(flagged ? { detail: f.title } : {}),
      },
    })
  }
  ranked.sort((a, b) => a.tier - b.tier || b.weight - a.weight || b.t - a.t || a.row.key.localeCompare(b.row.key))
  return ranked.map((r) => r.row)
}

/**
 * GAPS — per subject, in the daily read's order: each typed absence the
 * route recorded (a source gone quiet, a search that could not run, …),
 * then what the subject's card says no kind already names. A kind the route
 * could not read is not a gap and is left out.
 */
export function gapRows(views: readonly DeskGapView[]): BoardRow[] {
  const out: BoardRow[] = []
  for (const v of views) {
    if (v.state !== 'ready') continue
    for (const k of v.kinds) {
      if (k.state !== 'absent') continue
      for (const item of k.items) {
        out.push({
          key: `absence:${v.targetId}:${k.kind}:${item.subject}`,
          subjectId: v.targetId,
          subject: v.label,
          sentence: `${capital(ABSENCE_KIND_LABEL[k.kind])}: ${plainIds(item.subject)}. ${item.reason}`,
          at: item.since ?? item.as_of,
          trust: item.stale ? [{ text: 'Last known', title: 'Not re-checked since it was measured' }] : [],
          severity: null,
          open: {
            rowKind: 'absence',
            id: absenceId(v.targetId, k.kind, item.subject),
            label: `${item.subject} — ${ABSENCE_KIND_LABEL[k.kind]}`,
          },
          detail: k.meaning,
        })
      }
    }
    for (const g of v.derived) {
      out.push({
        key: `card:${v.targetId}:${g.kind}:${g.dimension ?? 'all'}`,
        subjectId: v.targetId,
        subject: v.label,
        sentence: g.dimension ? `${capital(plainIds(g.dimension))}: ${g.reason}` : capital(g.reason),
        at: g.producedAt,
        trust: [],
        severity: null,
        open: { rowKind: 'finding', id: g.cardId, label: `${v.label} scorecard` },
      })
    }
  }
  return out
}

const DUE_WORD: Record<string, string> = {
  in_grace: 'In grace',
  awaiting: 'Awaiting an answer',
  expired: 'Expired unanswered',
}

/** DUE — calls whose window closed with no outcome, longest overdue first. */
export function dueRows(items: readonly ForecastDue[] | null | undefined): BoardRow[] {
  if (!Array.isArray(items)) return []
  return [...items]
    .sort((a, b) => b.days_overdue - a.days_overdue || a.id.localeCompare(b.id))
    .map((f) => {
      const days = Math.max(0, Math.floor(f.days_overdue))
      return {
        key: `due:${f.id}`,
        subjectId: f.region,
        subject: humanizeId(f.region),
        sentence: `${capital(plainIds(f.event_class))}, called at ${Math.round(f.p * 100)} %: its window closed with no outcome recorded`,
        at: f.window_end,
        trust: [
          { text: DUE_WORD[f.mark] ?? capital(f.mark.replace(/_/g, ' ')) },
          { text: `${plural(days, 'day')} past` },
        ],
        severity: null,
        open: null,
        detail: f.resolution_test,
      }
    })
}

/** Rows on one subject, or all of them. */
export function onSubjectRows(rows: readonly BoardRow[], subjectId: string | null): BoardRow[] {
  return subjectId ? rows.filter((r) => r.subjectId === subjectId) : [...rows]
}

/**
 * How many changes each subject has in the window (the rail's figure and
 * order). Rows with no subject are not counted.
 */
export function changeCounts(since: JudgmentSince | null | undefined): Map<string, number> {
  const out = new Map<string, number>()
  for (const r of changedList(since)) {
    if (!r.targetId) continue
    out.set(r.targetId, (out.get(r.targetId) ?? 0) + 1)
  }
  return out
}

/** The subject with the most changes (ties: the first id in order), or null. */
export function mostChanged(counts: ReadonlyMap<string, number> | null | undefined): string | null {
  let best: string | null = null
  let n = 0
  for (const [id, c] of counts ?? []) {
    if (c > n || (c === n && best !== null && id < best)) {
      best = id
      n = c
    }
  }
  return n > 0 ? best : null
}

/** "18:01Z" today, "24 Nov" before, in UTC like every other stamp. */
export function whenText(iso: string | null | undefined, nowMs = Date.now()): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return ''
  const d = new Date(t)
  const n = new Date(nowMs)
  const sameDay = d.getUTCFullYear() === n.getUTCFullYear() && d.getUTCMonth() === n.getUTCMonth() && d.getUTCDate() === n.getUTCDate()
  if (sameDay) return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}Z`
  return `${d.getUTCDate()} ${['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getUTCMonth()]}`
}
