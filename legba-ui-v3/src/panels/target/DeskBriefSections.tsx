/**
 * DeskBriefSections — the desk brief page's sections, as pure presentation.
 *
 * Split off `DeskBriefPage.tsx` so the panel is fetch + arrangement and each
 * section is a small component that can be rendered from a fixture. Nothing
 * here fetches, and nothing here computes a figure: every number arrives
 * already derived by `lib/deskBriefPage.ts` or already composed by the route
 * that published it.
 *
 * The section ORDER is the one the research note draws out of a reader page
 * that works — index card, cited reads, the per-row evidence table with its
 * state column, endnotes, reading limits — but every section here closes
 * around an object of ours: the composition and its fenced voice, the desk's
 * bounded units, the eight-kind typed absence, the scale stamp, the grader's
 * independent reference.
 */
import { AlertTriangle, FileWarning, ScrollText } from 'lucide-react'
import CitedProse from '@/components/CitedProse'
import { InfoTip } from '@/components/InfoTip'
import { ScaleStamp } from '@/components/ScaleStamp'
import { UnitCorrectnessBadge } from '@/components/inspector/UnitCorrectnessBadge'
import { cn } from '@/lib/cn'
import { citationMasthead } from '@/lib/citationsModel'
import type { Citation } from '@/lib/citationsModel'
import { TYPED_ABSENCE_GLOSSARY_NOTE } from '@/lib/absenceModel'
import type { ScaleStamp as Stamp } from '@/lib/scaleStamp'
import type { DimensionBand } from '@/lib/evalOps'
import {
  EVIDENCE_STATE_LABEL,
  citationDateLine,
  evidenceStateDetail,
  judgeText,
  shareText,
  type EvidenceUnitRow,
  type ExportAbsences,
  type ExportCitation,
  type ExportItem,
  type NotPublishedLine,
  type ReadingLimit,
} from '@/lib/deskBriefPage'

/** The one em dash this page uses for a figure that does not exist. */
export const ABSENT = '—'

/** A small-caps section label — the page's one structural typographic tier. */
export function SectionLabel({
  children,
  tip,
  testId,
}: {
  children: React.ReactNode
  tip?: string
  testId?: string
}) {
  return (
    <h3
      className="mb-2 flex items-center gap-1 font-mono text-label uppercase tracking-wider text-ink-3"
      data-testid={testId}
    >
      {children}
      {tip && <InfoTip text={tip}>?</InfoTip>}
    </h3>
  )
}

/** One figure card — a value that always carries its own label and unit. */
export function FigureCard({
  label,
  value,
  note,
  testId,
}: {
  label: string
  value: string
  note?: string | null
  testId?: string
}) {
  return (
    <div
      className="rounded border border-line bg-surf-1 px-2.5 py-2"
      data-testid={testId}
    >
      <div className="text-label uppercase tracking-wide text-ink-3">{label}</div>
      <div className="mt-0.5 text-sm text-ink-1">{value}</div>
      {note && <div className="mt-0.5 text-label text-ink-3">{note}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 1 · the index card
// ---------------------------------------------------------------------------

/** The two badges are NEVER pooled — the tooltip says why, in one place. */
export const NEVER_POOLED =
  'Faithfulness and correctness are two measurements of two different ' +
  'things: faithfulness asks whether the read stayed true to what it cited, ' +
  'correctness asks whether it was right against a reference built without ' +
  'seeing this platform. They are shown side by side and never combined — a ' +
  'single number over the pair would mean nothing.'

export function IndexCard({
  targetId,
  composition,
  stamp,
  compositionAsOf,
  unitsCarried,
  unitsRoster,
  absences,
}: {
  targetId: string
  composition: ExportItem | null
  stamp: Stamp
  compositionAsOf: string | null
  unitsCarried: number
  unitsRoster: number
  absences: ExportAbsences | null
}) {
  const verify = composition?.verify_state?.trim() || null
  const flags = Object.entries(composition?.verify_flags ?? {})
  return (
    <section className="mb-8" data-testid="desk-brief-index-card">
      <div className="text-label uppercase tracking-wider text-ink-3">
        desk brief &middot; <span className="font-mono normal-case">{targetId}</span>
      </div>
      <h2 className="mt-1 text-lg leading-snug text-ink-1" data-testid="desk-brief-title">
        {composition?.title?.trim() || 'No composition published for this desk yet'}
      </h2>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <ScaleStamp stamp={stamp} testId="desk-brief-scale-stamp" />
        {composition?.severity && (
          <span className="rounded border border-line bg-surf-2 px-1.5 py-0.5 text-label text-ink-2">
            severity {composition.severity}
          </span>
        )}
        {composition?.superseded && (
          <span
            className="rounded border border-amber-700 bg-amber-900/40 px-1.5 py-0.5 text-label text-amber-200"
            data-testid="desk-brief-superseded"
          >
            superseded run
          </span>
        )}
        {composition?.analyst_version && (
          <span className="rounded border border-line bg-surf-2 px-1.5 py-0.5 font-mono text-label text-ink-3">
            {composition.analyst_id}@{composition.analyst_version}
          </span>
        )}
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <FigureCard
          label="composition as of"
          value={compositionAsOf ?? 'not recorded'}
          testId="desk-brief-asof"
        />
        <FigureCard
          label="faithfulness"
          value={verify ?? 'unmeasured'}
          note={flags.length > 0 ? flags.map(([k, n]) => `${k}: ${n}`).join(' · ') : null}
          testId="desk-brief-faithfulness"
        />
        <FigureCard
          label="units carried"
          value={`${unitsCarried} of ${unitsRoster} on the roster`}
          note="the roster is the denominator, not the carried set"
          testId="desk-brief-units-carried"
        />
        <FigureCard
          label="typed absences"
          value={
            absences
              ? `${absences.item_count} recorded · read ${absences.read_at.slice(0, 19)}Z`
              : 'not read for this document'
          }
          testId="desk-brief-absence-count"
        />
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span className="text-label uppercase tracking-wide text-ink-3">correctness</span>
        <UnitCorrectnessBadge
          analystId={composition?.analyst_id ?? null}
          targetId={targetId}
        />
        <InfoTip text={NEVER_POOLED} testId="desk-brief-never-pooled">
          <span className="text-label italic text-ink-3">never pooled with faithfulness</span>
        </InfoTip>
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// 2 · one block per bounded unit, in the composition's declared order
// ---------------------------------------------------------------------------

/**
 * The temporal note a cited HISTORICAL observation carries: the validity
 * window, the record time, and the producer's own tense marker VERBATIM.
 *
 * The marker is never re-derived. One string, one producer, three surfaces —
 * and a past figure that read as present on one of them would be the whole
 * point of the marker, lost.
 */
export function TemporalNotes({ citations }: { citations: Citation[] }) {
  const observed = citations.filter((c) => c.observation)
  if (observed.length === 0) return null
  return (
    <div className="mt-2 rounded border border-line bg-surf-1 px-2.5 py-2" data-testid="desk-brief-temporal">
      <SectionLabel tip="Valid time is the period the figure describes; record time is when the platform took it in. They are different clocks and this page never folds them.">
        historical series cited
      </SectionLabel>
      <ul className="space-y-1">
        {observed.map((c) => {
          const o = c.observation ?? {}
          const valid =
            o.validFrom || o.validTo
              ? `valid ${o.validFrom ?? ABSENT} → ${o.validTo ?? ABSENT}`
              : 'valid window not recorded'
          const record = o.recordTime ? `recorded ${o.recordTime}` : 'record time not recorded'
          return (
            <li key={c.marker} className="text-xs text-ink-2">
              <span className="font-mono text-ink-3">{c.marker}</span>{' '}
              {o.indicatorName ?? c.title ?? 'observation'}
              {o.value && (
                <>
                  {' — '}
                  <span className="text-ink-1">
                    {o.value}
                    {o.unit ? ` ${o.unit}` : ''}
                  </span>
                </>
              )}
              <span className="ml-1 text-ink-3">
                · {valid} · {record}
              </span>
              {o.staleTense && (
                <span className="ml-1 italic text-ink-3" data-testid="desk-brief-tense-marker">
                  {o.staleTense}
                </span>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

export function UnitBlock({
  targetId,
  item,
  citations,
  label,
  question,
  situations,
  onOpen,
}: {
  targetId: string
  item: ExportItem
  citations: Citation[]
  label: string
  question: string | null
  situations: { id: string; name: string; status: string; events: { id: string; title: string; lifecycleState: string }[] }[]
  onOpen: () => void
}) {
  return (
    <section
      className="mb-6 border-t border-line pt-4"
      data-testid="desk-brief-unit-block"
      data-unit={item.analyst_id ?? item.id}
    >
      <button
        type="button"
        onClick={onOpen}
        className="text-left"
        data-testid="desk-brief-unit-open"
        title="Re-pin the consult scope to this unit's read and open it in the Inspector"
      >
        <h3 className="text-sm text-ink-1 hover:underline">{label}</h3>
      </button>
      {question && <p className="mt-0.5 text-xs italic text-ink-3">{question}</p>}

      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <span
          className="rounded bg-surf-1 px-1.5 py-0.5 text-label text-ink-2"
          data-testid="desk-brief-unit-faithfulness"
        >
          {item.verify_state?.trim() || 'faithfulness unmeasured'}
        </span>
        <UnitCorrectnessBadge analystId={item.analyst_id ?? null} targetId={targetId} />
        {item.produced_at && (
          <span className="text-label text-ink-3">read {item.produced_at.slice(0, 19)}Z</span>
        )}
      </div>

      {item.body?.trim() ? (
        <CitedProse
          className="mt-2"
          text={item.body}
          citations={citations}
          claimTags
        />
      ) : (
        <p className="mt-2 text-xs text-ink-3">This read carries no written body.</p>
      )}

      <TemporalNotes citations={citations} />

      {situations.length > 0 && (
        <div className="mt-2" data-testid="desk-brief-unit-situations">
          <SectionLabel>open situations on this desk</SectionLabel>
          <ul className="space-y-1">
            {situations.map((s) => (
              <li key={s.id} className="text-xs text-ink-2">
                {s.name} <span className="text-ink-3">· {s.status}</span>
                {s.events.length === 0 ? (
                  <span className="text-ink-3"> · no tracked event linked yet</span>
                ) : (
                  <ul className="ml-4 mt-0.5 space-y-0.5">
                    {s.events.map((e) => (
                      <li key={e.id} className="text-ink-3">
                        {e.title} — {e.lifecycleState}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// 3 · the evidence table over the desk's unit ROSTER
// ---------------------------------------------------------------------------

const STATE_TONE: Record<string, string> = {
  in_basis: 'text-emerald-300',
  stale: 'text-amber-300',
  insufficient: 'text-ink-3',
  below_floor: 'text-ink-3',
  unverified: 'text-ink-3',
  no_head_in_horizon: 'text-ink-3',
  no_read: 'text-ink-3',
}

export function EvidenceTable({
  rows,
  dimensions,
  layers,
}: {
  rows: EvidenceUnitRow[]
  dimensions: Record<string, DimensionBand> | null | undefined
  layers: React.ReactNode
}) {
  return (
    <section className="mb-8" data-testid="desk-brief-evidence">
      <SectionLabel
        tip={
          'One row per bounded unit of this desk, whether or not the ' +
          'composition carried it — the roster is the denominator. The state ' +
          'column names which record decided it: the composition’s own ' +
          'coverage register, the banded scorecard, or the cadence check. A ' +
          'figure that was never measured prints as unmeasured, never as a zero.'
        }
        testId="desk-brief-evidence-label"
      >
        evidence by unit
      </SectionLabel>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[46rem] border-collapse text-xs">
          <thead>
            <tr className="border-b border-line text-left text-label uppercase tracking-wide text-ink-3">
              <th className="py-1 pr-3 font-normal">Unit</th>
              <th className="py-1 pr-3 font-normal">Last read</th>
              <th className="py-1 pr-3 font-normal">Faithfulness</th>
              <th className="py-1 pr-3 font-normal">Correctness coverage</th>
              <th className="py-1 pr-3 font-normal">Evidence state</th>
              <th className="py-1 font-normal">Typed absence</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const detail = evidenceStateDetail(r, dimensions)
              return (
                <tr
                  key={r.unitId}
                  className="border-b border-line/60 align-top"
                  data-testid={`desk-brief-evidence-row-${r.unitId}`}
                  data-state={r.state}
                >
                  <td className="py-1.5 pr-3 text-ink-1">
                    {r.label}
                    {r.question && (
                      <div className="text-label italic text-ink-3">{r.question}</div>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 text-ink-2">
                    {r.readAt ? r.readAt.slice(0, 10) : ABSENT}
                  </td>
                  <td className="py-1.5 pr-3 text-ink-2">{judgeText(r.judgeScore)}</td>
                  <td className="py-1.5 pr-3 text-ink-2">
                    {shareText(r.correctnessCoverage)}
                  </td>
                  <td className={cn('py-1.5 pr-3', STATE_TONE[r.state] ?? 'text-ink-2')}>
                    {EVIDENCE_STATE_LABEL[r.state]}
                    <div className="text-label text-ink-3">
                      {r.stateSource}
                      {detail ? ` · ${detail}` : ''}
                    </div>
                  </td>
                  <td className="py-1.5 text-ink-2">
                    {r.absences.length === 0 ? (
                      <span className="text-ink-3">none recorded</span>
                    ) : (
                      <ul className="space-y-0.5">
                        {r.absences.map((a, i) => (
                          <li key={`${a.kind}-${i}`}>
                            {a.label}
                            {a.stale && (
                              <span className="ml-1 italic text-amber-300">
                                last known, not re-checked
                              </span>
                            )}
                            <div className="text-label text-ink-3">{a.reason}</div>
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-1.5 text-label text-ink-3">{TYPED_ABSENCE_GLOSSARY_NOTE}</p>
      {layers}
    </section>
  )
}

// ---------------------------------------------------------------------------
// 4 · the cited endnotes
// ---------------------------------------------------------------------------

export function Endnotes({
  groups,
}: {
  groups: { heading: string; citations: ExportCitation[] }[]
}) {
  const total = groups.reduce((n, g) => n + g.citations.length, 0)
  return (
    <section className="mb-8" data-testid="desk-brief-endnotes">
      <SectionLabel testId="desk-brief-endnotes-label">
        <ScrollText className="h-3 w-3" aria-hidden /> endnotes
      </SectionLabel>
      {total === 0 ? (
        <p className="text-xs text-ink-3" data-testid="desk-brief-endnotes-empty">
          No record in this brief carries a citation.
        </p>
      ) : (
        groups.map((g) =>
          g.citations.length === 0 ? null : (
            <div key={g.heading} className="mb-3">
              <div className="text-label uppercase tracking-wide text-ink-3">{g.heading}</div>
              <ol className="mt-1 space-y-1">
                {g.citations.map((c, i) => {
                  const masthead = citationMasthead({
                    marker: c.marker,
                    refId: '',
                    refKind: 'signal',
                    signalId: '',
                    source: c.canonical_url ?? undefined,
                  })
                  return (
                    <li key={`${c.marker}-${i}`} className="text-xs text-ink-2">
                      <span className="font-mono text-ink-3">{c.marker}</span>{' '}
                      {c.title?.trim() || c.citation_kind}
                      <span className="ml-1 text-ink-3">
                        · {masthead ?? 'no masthead on record'} · {citationDateLine(c)} ·{' '}
                        {c.citation_kind}
                        {c.resolved ? '' : ' · unresolved'}
                      </span>
                    </li>
                  )
                })}
              </ol>
            </div>
          ),
        )
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// 5 · reading limits, and what this page does not publish
// ---------------------------------------------------------------------------

export function ReadingLimits({
  limits,
  notPublishedLines,
  provenanceNote,
}: {
  limits: ReadingLimit[]
  notPublishedLines: NotPublishedLine[]
  provenanceNote: string | null
}) {
  return (
    <section className="mb-4" data-testid="desk-brief-reading-limits">
      <SectionLabel testId="desk-brief-limits-label">reading limits</SectionLabel>
      <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
        {limits.map((l) => (
          <div key={l.label} className="flex gap-2 text-xs">
            <dt className="shrink-0 text-ink-3">{l.label}</dt>
            <dd className="text-ink-2">{l.value}</dd>
          </div>
        ))}
      </dl>
      {provenanceNote && (
        <p className="mt-2 text-label italic text-ink-3" data-testid="desk-brief-provenance-note">
          {provenanceNote}
        </p>
      )}

      <div className="mt-4">
        <SectionLabel
          testId="desk-brief-not-published-label"
          tip={
            'Assembled from the records themselves — the absence route’s own ' +
            'not-measured classes, the composition’s coverage register, the ' +
            'export’s missing-id placeholders and the grader’s reference ' +
            'state. Nothing on this list is prose somebody wrote about a ' +
            'condition that may since have changed.'
          }
        >
          <FileWarning className="h-3 w-3" aria-hidden /> what this page does not publish
        </SectionLabel>
        {notPublishedLines.length === 0 ? (
          <p className="text-xs text-ink-3" data-testid="desk-brief-not-published-empty">
            Every class this page reads answered for this desk: nothing was held back, no
            unit was named and left unfilled, and no id failed to resolve.
          </p>
        ) : (
          <ul className="space-y-1" data-testid="desk-brief-not-published">
            {notPublishedLines.map((l, i) => (
              <li key={`${l.subject}-${i}`} className="text-xs text-ink-2">
                <span className="text-ink-1">{l.subject}</span>
                <span className="text-ink-3"> — {l.why}</span>
                <span className="ml-1 text-label text-ink-3">[{l.source}]</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}

/** The desk's declared source-layer aperture — present / absent / unmeasured /
 *  undeclared, each in the operator's own sentence. A desk with no declared
 *  map says so; it never renders as a desk with no layers. */
export function LayerAperture({
  state,
  aperture,
  mapVersion,
}: {
  state: 'ready' | 'loading' | 'unavailable' | 'undeclared'
  aperture: {
    present?: string[]
    absent?: { layer: string; reason: string }[]
    unmeasured?: { layer: string; reason: string }[]
    undeclared?: { layer: string; reason: string }[]
  } | null
  mapVersion: string | null
}) {
  return (
    <div className="mt-3" data-testid="desk-brief-layers" data-state={state}>
      <SectionLabel tip="The six declared source layers and what this desk's own layer map says about each. A layer the map calls absent is a declared absence with the operator's reason, never a layer that happens to be quiet.">
        source layers for this desk
      </SectionLabel>
      {state === 'loading' && <p className="text-xs text-ink-3">reading the layer map…</p>}
      {state === 'unavailable' && (
        <p className="text-xs text-ink-3">
          The divergence read did not answer, so this page cannot say which layers feed this
          desk.
        </p>
      )}
      {state === 'undeclared' && (
        <p className="text-xs text-ink-3">
          No layer map is declared for this desk, so no layer is stated as present, absent or
          unmeasured here.
        </p>
      )}
      {state === 'ready' && aperture && (
        <>
          {mapVersion && (
            <div className="mb-1 font-mono text-label text-ink-3">map {mapVersion}</div>
          )}
          <div className="flex flex-wrap gap-1">
            {(aperture.present ?? []).map((l) => (
              <span
                key={`p-${l}`}
                className="rounded border border-emerald-700 bg-emerald-900/40 px-1.5 py-0.5 text-label text-emerald-200"
              >
                {l} · present
              </span>
            ))}
            {(['absent', 'unmeasured', 'undeclared'] as const).flatMap((kind) =>
              (aperture[kind] ?? []).map((entry) => (
                <InfoTip key={`${kind}-${entry.layer}`} text={entry.reason} interactiveParent>
                  <span className="rounded border border-line bg-surf-2 px-1.5 py-0.5 text-label text-ink-3">
                    {entry.layer} · {kind}
                  </span>
                </InfoTip>
              )),
            )}
          </div>
        </>
      )}
    </div>
  )
}

/** The honest banner for a desk with nothing composed yet. */
export function NoComposition({ targetId }: { targetId: string }) {
  return (
    <div
      className="flex items-start gap-2 rounded border border-line bg-surf-1 px-3 py-2 text-xs text-ink-2"
      data-testid="desk-brief-no-composition"
    >
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-300" aria-hidden />
      <span>
        No <span className="font-mono">country_composition</span> row has been published for{' '}
        <span className="font-mono">{targetId}</span>, so there is no record to read this desk
        from. The unit roster below still states what would be carried.
      </span>
    </div>
  )
}
