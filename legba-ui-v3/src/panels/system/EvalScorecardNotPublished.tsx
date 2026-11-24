/**
 * Accuracy page · section 5 — *what this page does not publish* (UI-1).
 *
 * The list is GENERATED, never hand-written: `lib/not_published.json` is
 * written by `scripts/gen_not_published.py` from docs/SEAMS.md's measurement
 * seams and the eval routes' own declared unmeasured classes, and
 * `tests/test_gen_not_published.py` fails when it is stale. This section only
 * renders it, attaching to each route class the live figure its route reports
 * today (`LIVE_RESOLVERS`).
 */
import { Figure } from '@/lib/Figure'
import {
  NOT_PUBLISHED,
  liveReading,
  type AccuracyInputs,
  type NotPublishedDoc,
} from '@/lib/accuracyPage'
import { PROFILE_UI, type ProfileUi } from '@/lib/profileUi'
import { Prose, Section } from './EvalScorecardParts'

export function NotPublishedSection({
  inputs,
  doc = NOT_PUBLISHED,
  ui = PROFILE_UI,
}: {
  inputs: AccuracyInputs
  doc?: NotPublishedDoc
  ui?: ProfileUi
}) {
  return (
    <Section
      n={5}
      title="What this page does not publish"
      testId="accuracy-not-published"
      lead="Generated from docs/SEAMS.md and the routes' own declared unmeasured classes (scripts/gen_not_published.py) — the page states its absences rather than drawing them as zeros."
    >
      <ul className="list-disc space-y-1.5 pl-4 text-label text-ink-2">
        {doc.route_classes.map((c) => {
          const live = liveReading(c, inputs, ui)
          return (
            <li key={c.id} data-testid={`not-published-${c.id}`}>
              <span className="font-mono text-label text-ink-1">{c.route}</span> — <Prose text={c.text} />
              {live?.figure && (
                <div className="mt-0.5">
                  <Figure {...live.figure} testId={`not-published-${c.id}-live`} className="text-label" />
                </div>
              )}
              {live?.text && (
                <div className="mt-0.5 text-label text-ink-3" data-testid={`not-published-${c.id}-live`}>
                  {live.text}
                </div>
              )}
              <div className="font-mono text-label text-ink-3">{c.source}</div>
            </li>
          )
        })}
        {doc.seams.map((s) => (
          <li key={s.id} data-testid={`not-published-${s.id}`}>
            <Prose text={s.title} className="text-ink-1" /> — <Prose text={s.summary} /> <Prose text={s.guard} />
            <div className="font-mono text-label text-ink-3">{s.source}</div>
          </li>
        ))}
      </ul>
    </Section>
  )
}
