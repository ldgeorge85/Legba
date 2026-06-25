/**
 * `system.eval` — re-exports the real UI-5 Eval Scorecard (was a stub).
 *
 * The richer implementation lives in `EvalScorecard.tsx` and is also
 * registered under the visible `system.eval_scorecard` kind; this keeps the
 * pre-existing `system.eval` registration resolving to the real panel without
 * editing the shared registry's hidden-set (append-only constraint).
 */

export { default } from './EvalScorecard'
