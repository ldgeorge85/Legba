/**
 * v4 panel — the Morning Read (`assembly.v1` + the judgment bands).
 *
 * The Dockview shell only. Everything the surface decides lives in
 * `v4/read/MorningRead.tsx`; this file exists so the reading column is a
 * registry kind with a tab, a sidebar entry and a place in the Morning Read
 * stance's seed — and so `AssessmentPanel` keeps rendering the legacy prose
 * one-pager unchanged while both regimes are live (spec §5.2: the old path
 * stays runnable for the whole program).
 *
 * 7b-iv — the stance's own question is "what happened, what moved, what does
 * it mean?", and the assembly reader answers only the third part: it renders
 * ONE composed record and is silent about every other desk. So the panel now
 * opens on `v4/read/JudgmentBands.tsx` — CHANGED / CHECKED / GAPS / DUE, per
 * desk, from routes the platform already writes — and the read follows it,
 * BYTE-IDENTICAL. The composition is deliberate: the bands are what a reader
 * with twenty minutes needs first, and the record is what they drop into once
 * a band has told them which desk to drop into. Nothing was taken away.
 */
import { PanelBoundary } from './PanelBoundary'
import MorningRead from '@/v4/read/MorningRead'
import JudgmentBands from '@/v4/read/JudgmentBands'

export default function MorningReadPanel() {
  return (
    <PanelBoundary>
      <div className="h-full w-full overflow-auto bg-surface-300">
        <JudgmentBands />
        <MorningRead />
      </div>
    </PanelBoundary>
  )
}
