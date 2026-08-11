/**
 * unitEvalModel — the per-bounded-unit eval scoreboard (P2-T6).
 *
 * Reads `GET /api/v1/eval/scores`, which surfaces each bounded reasoning unit's
 * eval off the latest `unit_correctness_scorer` run (P2-T5): faithfulness
 * (measured by the mandatory verify pass) + correctness-vs-reference (None until
 * scorable gold labels exist) + a server-composed honest `badge` string. The
 * badge already encodes the "no invented number" contract (an unlabeled unit
 * reads `verified | faithfulness 0.45 | unmeasured (0 labels)`), so the UI just
 * renders it verbatim — it never fabricates or reformats a score.
 *
 * The fetch is memoised at module scope so many unit badges share ONE request
 * per page load (the scorer runs daily; the board is not hot).
 */
import { apiGet } from '@/lib/api'

/** One bounded unit's eval row (mirrors the server `UnitEvalScore`). */
export interface UnitEvalScore {
  unit: string
  faithfulness: number | null
  correctness_vs_reference: number | null
  n_labeled: number
  n_findings: number
  status: string | null
  /**
   * M-1 — the PRIMARY correctness axis: OPERATOR gold-set verdicts. Segregated
   * from `correctness_vs_reference` (a different table, a different question)
   * and never pooled with faithfulness. `operator_sufficient` is false below
   * the server's floor, which is where the whole gold set sits today — the
   * badge marks such a reading `indicative` rather than hiding it.
   */
  correctness_operator?: number | null
  n_operator_labels?: number
  n_operator_scored?: number
  operator_sufficient?: boolean
  operator_mix?: Record<string, number>
  operator_status?: string | null
  /** The honest, server-composed badge string — rendered verbatim. */
  badge: string
}

/** `GET /eval/scores` body. `scored_at` is null (and `units` empty) if the
 *  scorer has never run — an honest empty board, no invented rows. */
export interface EvalScores {
  scored_at: string | null
  units: UnitEvalScore[]
}

let _cache: Promise<EvalScores> | null = null

/** Fetch the eval scoreboard, memoised. `force` re-fetches (e.g. after a manual
 *  scorer run). A failed request clears the cache so a later mount can retry. */
export function fetchEvalScores(force = false): Promise<EvalScores> {
  if (force || _cache === null) {
    _cache = apiGet<EvalScores>('/eval/scores').catch((err) => {
      _cache = null
      throw err
    })
  }
  return _cache
}

/** Find one unit's eval row by its analyst id, or null when the id is not a
 *  bounded unit / the board is empty (honest — no badge for a non-unit). */
export function findUnitScore(
  scores: EvalScores | null | undefined,
  analystId: string | null | undefined,
): UnitEvalScore | null {
  if (!scores || !analystId) return null
  return scores.units.find((u) => u.unit === analystId) ?? null
}
