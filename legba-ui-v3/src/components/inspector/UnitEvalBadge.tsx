/**
 * UnitEvalBadge — the per-bounded-unit eval badge (P2-T6).
 *
 * Given a finding's `analystId`, renders the unit's honest eval badge off
 * `GET /api/v1/eval/scores` (e.g. `verified | faithfulness 0.45 | unmeasured
 * (0 labels)`). The badge string is composed SERVER-side (the "no invented
 * number" contract lives in one place), so this component renders it verbatim.
 *
 * HONESTY / DEGRADE: renders NOTHING when the analyst id is not a bounded unit,
 * the scorer has never run, or the fetch fails — a non-unit finding gets no
 * badge rather than a fabricated one.
 */
import { useEffect, useState } from 'react'
import { Gauge } from 'lucide-react'
import { fetchEvalScores, findUnitScore, type UnitEvalScore } from '@/lib/unitEvalModel'

export function UnitEvalBadge({ analystId }: { analystId: string | null | undefined }) {
  const [score, setScore] = useState<UnitEvalScore | null>(null)

  useEffect(() => {
    let cancelled = false
    if (!analystId) {
      setScore(null)
      return
    }
    fetchEvalScores()
      .then((s) => {
        if (!cancelled) setScore(findUnitScore(s, analystId))
      })
      .catch(() => {
        if (!cancelled) setScore(null)
      })
    return () => {
      cancelled = true
    }
  }, [analystId])

  // Not a bounded unit / no scorer run / fetch failed → no badge (honest).
  if (!score) return null

  return (
    <span
      className="inline-flex items-center gap-1 rounded bg-surf-1 px-1.5 py-0.5 text-label text-accent-info"
      data-testid="unit-eval-badge"
      title="Per-unit eval (P2-T6): faithfulness from the mandatory verify pass + correctness vs operator gold labels"
    >
      <Gauge className="h-3 w-3" aria-hidden />
      {score.badge}
    </span>
  )
}
