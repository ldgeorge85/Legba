/**
 * goldsetModel — the weekly correctness gold-set worksheet (P2-5).
 *
 * The operator agreed to label a handful of findings per week if a lightweight
 * surface exists; `system.goldset` is that surface and this module is its
 * DOM-free brain. Reads `GET /api/v1/v3/eval/goldset/worksheet` (this ISO
 * week's deterministic, server-pinned sample — same week → same items) and
 * writes `POST /api/v1/v3/eval/goldset/label` (upsert ONE verdict per finding;
 * the server snapshots title/claims/citations at label time so supersession
 * can't orphan the judgment).
 *
 * Verdicts flow into the eval scoreboard (`/eval/scores`): each unit's
 * operator-correctness segment (`operator 0.75 (n=6)`) grows its n live as
 * labels land — the whole point of the loop.
 *
 * All progress / verdict / empty-state logic lives here so it is unit-tested
 * without a DOM (the house lib/ pattern).
 */
import { apiGet, apiPost } from '@/lib/api'

/** The CLOSED verdict vocabulary (mirrors the server's CHECK constraint). */
export type GoldsetVerdict = 'correct' | 'partially_correct' | 'incorrect' | 'unresolvable'

/** One stored operator verdict (mirrors the server `LabelState`). */
export interface GoldsetLabelState {
  id: string
  finding_id: string
  unit_analyst_id: string
  target_id: string | null
  label: GoldsetVerdict
  rationale: string | null
  labeled_by: string | null
  labeled_at: string
  created_at: string
}

/** One sampled finding, ready to read + judge (mirrors `WorksheetItem`).
 *  `data` is the finding's full JSONB envelope — feed it to
 *  `extractCitations` for the CitedProse reading kit, exactly as the Findings
 *  panels do. */
export interface GoldsetWorksheetItem {
  finding_id: string
  unit: string
  target_id: string | null
  title: string
  body: string
  data: Record<string, unknown>
  citations: Array<Record<string, unknown>>
  faithfulness: number | null
  produced_at: string
  superseded: boolean
  label: GoldsetLabelState | null
}

/** `GET /v3/eval/goldset/worksheet` body (mirrors `WorksheetOut`). */
export interface GoldsetWorksheet {
  week: string
  week_started_at: string
  next_sample_at: string
  sample_size: number
  labeled_count: number
  all_labeled: boolean
  items: GoldsetWorksheetItem[]
}

/** One verdict button: value + display label + hint + tone bucket. */
export interface VerdictOption {
  value: GoldsetVerdict
  label: string
  hint: string
  tone: 'good' | 'warn' | 'bad' | 'muted'
}

/** The four verdict buttons, in display order. `unresolvable` is a first-class
 *  honest state (looked, could not judge) — excluded from the operator score,
 *  never dropped. */
export const VERDICT_OPTIONS: readonly VerdictOption[] = [
  { value: 'correct', label: 'Correct', hint: 'the read is right', tone: 'good' },
  {
    value: 'partially_correct',
    label: 'Partially correct',
    hint: 'right direction, wrong detail(s)',
    tone: 'warn',
  },
  { value: 'incorrect', label: 'Incorrect', hint: 'the read is wrong', tone: 'bad' },
  {
    value: 'unresolvable',
    label: 'Unresolvable',
    hint: 'could not judge from the evidence',
    tone: 'muted',
  },
] as const

/** Worksheet progress — derived from the ITEMS (the rendered truth), not the
 *  server counters, so an optimistic local update stays consistent. */
export function worksheetProgress(ws: Pick<GoldsetWorksheet, 'items'> | null | undefined): {
  total: number
  labeled: number
  allLabeled: boolean
} {
  const items = ws?.items ?? []
  const labeled = items.filter((i) => i.label !== null).length
  return { total: items.length, labeled, allLabeled: items.length > 0 && labeled === items.length }
}

/** Pure local upsert of one saved verdict into the worksheet (the optimistic /
 *  post-save update) — returns a NEW worksheet, never mutates. An unknown
 *  finding_id is a no-op (the operator may label outside the sample). */
export function applyLabel(ws: GoldsetWorksheet, saved: GoldsetLabelState): GoldsetWorksheet {
  const items = ws.items.map((i) => (i.finding_id === saved.finding_id ? { ...i, label: saved } : i))
  const labeled = items.filter((i) => i.label !== null).length
  return {
    ...ws,
    items,
    labeled_count: labeled,
    all_labeled: items.length > 0 && labeled === items.length,
  }
}

/** Weekday-name label for the next sample boundary (always a Monday — the ISO
 *  week pin). Kept as a function so a locale/format change lives in one place. */
export function nextSampleLabel(): string {
  return 'next sample Monday'
}

/**
 * The honest empty-state line, or null when the worksheet has unlabeled work.
 *  * no sample at all (no verified candidates) → says so + when the next draw is
 *  * every item labeled → "all labeled — next sample Monday"
 */
export function emptyStateMessage(ws: GoldsetWorksheet | null | undefined): string | null {
  if (!ws) return null
  if (ws.items.length === 0) {
    return `no verified findings eligible this week — ${nextSampleLabel()}`
  }
  const { allLabeled } = worksheetProgress(ws)
  if (allLabeled) return `all labeled — ${nextSampleLabel()}`
  return null
}

// ---------------------------------------------------------------------------
// API wrappers (thin; the panel goes through these so paths live in one place)
// ---------------------------------------------------------------------------

export function fetchGoldsetWorksheet(): Promise<GoldsetWorksheet> {
  return apiGet<GoldsetWorksheet>('/v3/eval/goldset/worksheet')
}

export function postGoldsetLabel(body: {
  finding_id: string
  label: GoldsetVerdict
  rationale?: string | null
}): Promise<GoldsetLabelState> {
  return apiPost<GoldsetLabelState>('/v3/eval/goldset/label', body)
}
