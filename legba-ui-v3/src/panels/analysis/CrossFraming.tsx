/**
 * Cross-framing (`analysis.cross_framing`) — ONE CLAIM AGAINST THE UNITS THAT
 * TOUCH IT, where the layers diverge, and what no unit says (wave P lane B).
 *
 * A country desk is a roster of bounded units, each answering one narrow
 * question over the same slice, and every surface we ship reads them SEPARATELY
 * — as composition inputs, as feed rows, as cells in the gap strip. Nothing put
 * one claim against the units that touch it at once, so a reader could not see
 * that three units rest on the same signal and frame it three different ways,
 * or that a fourth is not disagreeing but ABSENT. Meridian's reality-check
 * screen is the polish borrowed here (`ui_research/meridian_shot-reality.jpg`:
 * what happened · conflicting narratives by camp · spin · ground truth); the
 * objects are ours — the units, the source layers, typed absence, the leans.
 *
 * WHAT IT RENDERS, in the order the question is asked:
 *   1. THE HEADER — the claim verbatim, its per-claim verify verdict, the
 *      count sentence ("carried by N of M units · K with no read"), and the
 *      record's own scale/method stamp beside its as-of.
 *   2. THE UNIT TABLE — one row per unit on the RECORD's declared coverage
 *      roster: how that unit's read frames the claim (its own sentence, with
 *      its own markers), what it rests on (cited count, the record's fold
 *      chips, its block faithfulness), and for a unit with no read the typed
 *      absence in the ROUTE's own words with its proof and its clock. Never a
 *      blank row.
 *   3. THE LENS ROW — the six stance-typed leans' framings of the same
 *      evidence, each with its declared prior named and its own gate score.
 *   4. WHERE THE LAYERS DIVERGE — the desk's divergence receipt, pair by pair,
 *      with the finding when one fired and the SEAMS #60 audit stamp.
 *   5. WHAT NO UNIT SAYS — the units named by none, the desk's off-roster typed
 *      absences with their proofs, the kinds the route could not read, the
 *      record's own drop ledger by why-class, and the tension pass's checked
 *      negative.
 *   6. ASK ABOUT THIS CLAIM — sets the AMBIENT consult scope pin to the claim.
 *
 * HONESTY RULES this panel holds to:
 *   * It computes no verdict and no statistic. Every number is a length of a
 *     list the payload holds or a figure the record stamped; `JudgmentBands`
 *     computes none and neither does this.
 *   * A unit with no read renders its typed absence, and a STALE absence says
 *     "last known absence, not re-checked" rather than passing for current.
 *   * A unit the record has no absence for says "no typed absence is recorded"
 *     — a real answer, and the one the old surfaces collapsed into a blank.
 *   * Absence renders as absence: `—` and "not measured", never 0.
 *   * Every figure carries its unit and its as-of.
 *
 * NO NEW ROUTE. Five reads the UI already makes elsewhere carry all of it:
 * `/findings` (the desk's newest composition), `/v3/contentions?scope=`,
 * `/v3/absence?scope=`, `/v3/layers/divergence` and `/journal?kind=lens`.
 * All derivation is pure in `lib/framingModel.ts`.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Columns3, MessageSquare } from 'lucide-react'

import { PanelChrome } from '@/components/PanelChrome'
import { ScaleStamp } from '@/components/ScaleStamp'
import { RecordLink } from '@/components/inspector/RecordLink'
import { InfoTip } from '@/components/InfoTip'
import { cn } from '@/lib/cn'
import { apiGet, ApiError, fetchJournal, fetchLayerDivergence } from '@/lib/api'
import type { JournalEntry, LayerDivergenceResponse } from '@/lib/api'
import type { AbsenceResponse } from '@/lib/absenceModel'
import { projectAssembly, type ReadFindingRow } from '@/lib/assemblyModel'
import { extractCitations } from '@/lib/citationsModel'
import { corroborationByOrdinal } from '@/lib/claimFold'
import { CLAIM_VERDICT_ABSENCE_EXPLAIN } from '@/lib/claimVerdicts'
import type { ContentionRow, ContentionsResponse } from '@/lib/contentionsModel'
import { countryNameForTargetId, humanizeId } from '@/lib/deskNames'
import {
  absenceSentence,
  countsSentence,
  crossFramingScope,
  deriveCrossFraming,
  type CrossFramingView,
  type FramingLensRow,
  type FramingUnitRow,
  type UnitRowState,
} from '@/lib/framingModel'
import {
  OPEN_CROSS_FRAMING_EVENT,
  drainPendingCrossFraming,
  type CrossFramingSubject,
} from '@/lib/crossFramingLink'
import { formatZ, type PairState } from '@/lib/layerDivergence'
import { useScope } from '@/state/scope'
import type { PanelProps } from '@/types'

/** The composition producer whose record carries a country desk's unit reads. */
const COUNTRY_COMPOSITION = 'country_composition'

/** How many lens entries to pull. Ten leans + faculties fit inside one page. */
const LENS_PAGE = 24

const STATE_TONE: Record<UnitRowState, string> = {
  carries: 'border-accent-ok/50 bg-accent-ok/10 text-accent-ok',
  frames: 'border-accent-ok/35 bg-accent-ok/5 text-accent-ok',
  silent: 'border-line bg-surf-1 text-ink-3',
  absent: 'border-accent-warning/45 bg-accent-warning/10 text-accent-warning',
  unrecorded: 'border-dashed border-line-strong bg-transparent text-ink-3',
}

const STATE_LABEL: Record<UnitRowState, string> = {
  carries: 'carries the claim',
  frames: 'frames it',
  silent: 'read, silent on it',
  absent: 'absent — typed',
  unrecorded: 'absent — untyped',
}

/** Mirrors `LayerDivergence.tsx`'s own labels so one state reads one way. */
const PAIR_STATE_LABEL: Record<PairState, string> = {
  fired: 'fired',
  thin: 'thin',
  aperture_excluded: 'not evaluable',
  below_threshold: 'below threshold',
  evaluable: 'evaluated',
}

const FOLD_TONE: Record<string, string> = {
  critical: 'border-accent-critical/50 bg-accent-critical/10 text-accent-critical',
  warning: 'border-accent-warning/50 bg-accent-warning/10 text-accent-warning',
  muted: 'border-line bg-surf-2 text-ink-3',
}

/** A percentage with its unit, or the honest-absence mark. Never 0 for null. */
function pct(v: number | null): string {
  return v === null ? '— not measured' : `${(v * 100).toFixed(0)}%`
}

function Stamp({ label, value }: { label: string; value: string }) {
  return (
    <span className="whitespace-nowrap text-label text-ink-3">
      <span className="text-ink-3/70">{label}</span>{' '}
      <span className="font-mono text-ink-2">{value}</span>
    </span>
  )
}

function SectionHead({ children, tip }: { children: React.ReactNode; tip?: string }) {
  return (
    <h3 className="mb-1 flex items-center gap-1 text-label uppercase tracking-wide text-ink-3">
      {children}
      {tip && <InfoTip text={tip}>?</InfoTip>}
    </h3>
  )
}

// ---------------------------------------------------------------------------
// The unit table
// ---------------------------------------------------------------------------

function UnitRow({ row }: { row: FramingUnitRow }) {
  const hasRead = row.state === 'carries' || row.state === 'frames' || row.state === 'silent'
  return (
    <tr className="border-t border-line align-top" data-testid={`cross-framing-unit-${row.unit}`}>
      <td className="py-1.5 pr-2 align-top">
        <div className="font-medium text-ink-1">{row.label}</div>
        <span
          className={cn(
            'mt-0.5 inline-block rounded border px-1 text-label',
            STATE_TONE[row.state],
          )}
          data-testid={`cross-framing-state-${row.unit}`}
        >
          {STATE_LABEL[row.state]}
        </span>
      </td>

      <td className="py-1.5 pr-2 align-top text-body text-ink-2">
        {hasRead ? (
          <>
            <span data-testid={`cross-framing-framing-${row.unit}`}>{row.framing}</span>
            {row.state === 'silent' && (
              <div className="mt-0.5 text-label text-ink-3">
                cites none of this claim&rsquo;s evidence — absent on this matter, not
                contradicting it
              </div>
            )}
            {row.sharedSignalIds.length > 0 && (
              <div className="mt-0.5 text-label text-ink-3">
                shares {row.sharedSignalIds.length} cited signal
                {row.sharedSignalIds.length === 1 ? '' : 's'} with the claim
              </div>
            )}
            {row.tensions.map((t, i) => (
              <div
                key={i}
                className="mt-0.5 text-label text-accent-warning"
                data-testid={`cross-framing-tension-${row.unit}`}
              >
                declared tension ({t.detector}): {t.statement}
              </div>
            ))}
          </>
        ) : (
          <div data-testid={`cross-framing-absence-${row.unit}`}>
            <span className="text-ink-3">{absenceSentence(row)}</span>
            {row.absenceStale && (
              <span className="ml-1 rounded border border-accent-warning/45 bg-accent-warning/10 px-1 text-label text-accent-warning">
                stale
              </span>
            )}
            {row.absence?.proof.what_was_checked && (
              <div className="mt-0.5 text-label text-ink-3">
                proof: {row.absence.proof.what_was_checked}
                {row.absence.proof.ref && row.absence.proof.ref_kind === 'finding' && (
                  <>
                    {' · '}
                    <RecordLink
                      kind="finding"
                      id={row.absence.proof.ref}
                      label="open the record of that look"
                      origin="cross-framing"
                    />
                  </>
                )}
                {row.absence.proof.ref && row.absence.proof.ref_kind !== 'finding' && (
                  <span className="font-mono"> · {row.absence.proof.ref}</span>
                )}
              </div>
            )}
          </div>
        )}
      </td>

      <td className="py-1.5 align-top text-label text-ink-3">
        {hasRead ? (
          <div className="space-y-0.5">
            <div>
              {row.citedCount ?? '—'} cited signal
              {row.citedCount === 1 ? '' : 's'}
              {row.nSources !== null && <> · {row.nSources} outlets</>}
            </div>
            <div>
              faithfulness{' '}
              <span className="font-mono text-ink-2">{pct(row.faithfulness)}</span>
              {row.producedAt && (
                <> · as of {new Date(row.producedAt).toLocaleString()}</>
              )}
            </div>
            <div className="flex flex-wrap gap-1">
              {row.foldChips.map((chip) => (
                <span
                  key={chip.kind}
                  className={cn('rounded border px-1 text-label', FOLD_TONE[chip.tone])}
                  title={chip.reason}
                  data-testid={`cross-framing-fold-${row.unit}-${chip.kind}`}
                >
                  {chip.label}
                </span>
              ))}
              {row.contested && row.foldChips.length === 0 && (
                <span
                  className="rounded border border-accent-warning/50 bg-accent-warning/10 px-1 text-label text-accent-warning"
                  title={row.contested.reason || undefined}
                  data-testid={`cross-framing-contested-${row.unit}`}
                >
                  contested by retrieval
                </span>
              )}
            </div>
            {row.findingId && (
              <RecordLink
                kind="finding"
                id={row.findingId}
                label="open this read"
                origin="cross-framing"
              />
            )}
          </div>
        ) : (
          <span>— no admitted read</span>
        )}
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// The lens row
// ---------------------------------------------------------------------------

function LensRow({ row }: { row: FramingLensRow }) {
  return (
    <li
      className="border-t border-line py-1.5 first:border-t-0"
      data-testid={`cross-framing-lens-${row.analystId}`}
    >
      <div className="flex flex-wrap items-baseline gap-1.5 text-label">
        <span className="font-medium text-ink-1">{row.name ?? humanizeId(row.analystId)}</span>
        <span className="text-ink-3">
          prior: {row.prior ?? 'not named in this bundle'}
        </span>
        <span className="text-ink-3">
          · judge{' '}
          <span className="font-mono text-ink-2">{pct(row.judgeScore)}</span>
        </span>
        {row.producedAt && (
          <span className="text-ink-3">· as of {new Date(row.producedAt).toLocaleString()}</span>
        )}
        {row.honestyFlags.map((f) => (
          <span
            key={f}
            className="rounded border border-accent-warning/45 bg-accent-warning/10 px-1 text-accent-warning"
          >
            {humanizeId(f)}
          </span>
        ))}
      </div>
      <div className="mt-0.5 text-body text-ink-2">
        {row.framing ? (
          <span data-testid={`cross-framing-lens-framing-${row.analystId}`}>{row.framing}</span>
        ) : row.entryId ? (
          <span className="text-ink-3">
            read this window and cited none of this claim&rsquo;s evidence — silent, not
            dissenting
          </span>
        ) : (
          <span className="text-ink-3">no lens read in the window</span>
        )}
      </div>
      {row.entryId && (
        <RecordLink
          kind="journal_entry"
          id={row.entryId}
          label="open the lens read"
          origin="cross-framing"
          className="text-label"
        />
      )}
    </li>
  )
}

// ---------------------------------------------------------------------------
// The panel
// ---------------------------------------------------------------------------

export default function CrossFramingPanel({ registration }: PanelProps) {
  // The subject: the parked deep link first (a claim chip or a situation row
  // clicked before this tile existed), then the live event, then the wall
  // scope's desk when it names exactly one. A scope over fourteen desks does
  // NOT pick one of them — that would show a fourteenth of the scope and call
  // it the scope (the `scopeParams` precedent).
  const [subject, setSubject] = useState<CrossFramingSubject | null>(() =>
    drainPendingCrossFraming(),
  )
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<CrossFramingSubject>).detail
      if (detail?.targetId) setSubject(detail)
    }
    window.addEventListener(OPEN_CROSS_FRAMING_EVENT, handler)
    return () => window.removeEventListener(OPEN_CROSS_FRAMING_EVENT, handler)
  }, [])

  const wallScope = useScope((s) => s.scope)
  const setScope = useScope((s) => s.setScope)
  const scopeDesk =
    wallScope && wallScope.members.targetIds.length === 1 ? wallScope.members.targetIds[0] : null
  const targetId = subject?.targetId ?? scopeDesk

  const recordQ = useQuery<{ data: ReadFindingRow[] }>({
    enabled: !!targetId,
    queryKey: ['cross-framing-record', targetId],
    queryFn: async () => {
      try {
        return await apiGet<{ data: ReadFindingRow[] }>(
          `/findings?analyst_id=${COUNTRY_COMPOSITION}&target_id=${encodeURIComponent(
            targetId!,
          )}&limit=1`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { data: [] }
        throw e
      }
    },
  })

  const contentionsQ = useQuery<ContentionRow[]>({
    enabled: !!targetId,
    queryKey: ['cross-framing-contentions', targetId],
    queryFn: async () => {
      try {
        const resp = await apiGet<ContentionsResponse>(
          `/v3/contentions?scope=${encodeURIComponent(targetId!)}`,
        )
        return resp?.contentions ?? []
      } catch {
        // An optional sidecar. Its pass ships draft; an empty answer is the
        // expected one until it is activated, and a reader surface does not
        // fall over because a sidecar is missing.
        return []
      }
    },
  })

  const absenceQ = useQuery<AbsenceResponse | null>({
    enabled: !!targetId,
    queryKey: ['cross-framing-absence', targetId],
    queryFn: async () => {
      try {
        return await apiGet<AbsenceResponse>(
          `/v3/absence?scope=${encodeURIComponent(targetId!)}`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null
        throw e
      }
    },
  })

  const divergenceQ = useQuery<LayerDivergenceResponse | null>({
    enabled: !!targetId,
    queryKey: ['cross-framing-divergence'],
    queryFn: async () => {
      try {
        return await fetchLayerDivergence()
      } catch {
        return null
      }
    },
  })

  const lensQ = useQuery<JournalEntry[]>({
    enabled: !!targetId,
    queryKey: ['cross-framing-lenses'],
    queryFn: async () => {
      try {
        const resp = await fetchJournal({ kind: ['lens'], limit: LENS_PAGE })
        return resp?.entries ?? []
      } catch {
        return []
      }
    },
  })

  const row = recordQ.data?.data?.[0] ?? null
  const view: CrossFramingView | null = useMemo(() => {
    if (!row || !targetId) return null
    const payload = (row.data ?? row.payload ?? null) as Record<string, unknown> | null
    return deriveCrossFraming({
      targetId,
      recordId: row.id,
      assembly: projectAssembly(row),
      contentions: contentionsQ.data ?? [],
      absence: absenceQ.data ?? null,
      divergence: divergenceQ.data ?? null,
      lensEntries: lensQ.data ?? [],
      evidence: {
        citations: extractCitations(payload),
        corroboration: corroborationByOrdinal(payload),
      },
      verification: row.verification ?? null,
      claimId: subject?.claimId ?? null,
      claimText: subject?.claimText ?? null,
    })
  }, [
    row,
    targetId,
    contentionsQ.data,
    absenceQ.data,
    divergenceQ.data,
    lensQ.data,
    subject?.claimId,
    subject?.claimText,
  ])

  const deskName = targetId
    ? (countryNameForTargetId(targetId) ?? humanizeId(targetId))
    : null

  const [pinned, setPinned] = useState(false)
  useEffect(() => setPinned(false), [view?.claim.text])

  const askAboutClaim = () => {
    if (!view) return
    // The AMBIENT pin, not a side panel: Consult replaces its scope pin from
    // the wall scope on every change (`lib/consultContext.SCOPE_PIN_ORIGIN`),
    // so this is the whole mechanism and the same session follows the reader
    // here with its provenance census line intact. The scope is durable, so a
    // Consult tile opened LATER picks it up on mount.
    setScope(crossFramingScope(view))
    setPinned(true)
  }

  return (
    <PanelChrome
      registration={registration}
      title="Cross-framing"
      subtitle={deskName ? `${deskName} · one claim across the desk's units` : undefined}
      onRefresh={() => Promise.all([recordQ.refetch(), contentionsQ.refetch()])}
    >
      <div className="space-y-3 p-2" data-testid="cross-framing">
        {!targetId && (
          <div
            className="rounded border border-line bg-surf-1 p-2 text-body text-ink-2"
            data-testid="cross-framing-no-subject"
          >
            No claim in hand. Open this panel from a claim on a desk — the Claims
            panel&rsquo;s &ldquo;cross-framing&rdquo; action or a situation row — or scope the
            wall to a single desk and it will read that desk&rsquo;s most contested claim.
          </div>
        )}

        {targetId && recordQ.isLoading && (
          <div className="text-label text-ink-3">reading the desk&rsquo;s composition…</div>
        )}

        {targetId && !recordQ.isLoading && !row && (
          <div
            className="rounded border border-line bg-surf-1 p-2 text-body text-ink-2"
            data-testid="cross-framing-no-record"
          >
            {deskName} has no published <span className="font-mono">{COUNTRY_COMPOSITION}</span>{' '}
            record. There is nothing to frame — not a claim that no unit carries.
          </div>
        )}

        {targetId && row && !view && (
          <div
            className="rounded border border-line bg-surf-1 p-2 text-body text-ink-2"
            data-testid="cross-framing-no-claim"
          >
            The desk&rsquo;s newest record carries no quoted claim (no
            <span className="font-mono"> assembly.v1 </span>
            payload, or no quoted span in it), so there is no sentence to put against the
            units.
          </div>
        )}

        {view && (
          <>
            {/* 1 — THE HEADER */}
            <header
              className="rounded border border-line bg-surf-1 p-2"
              data-testid="cross-framing-header"
            >
              <div className="mb-1 flex flex-wrap items-center gap-2">
                <Columns3 className="h-3.5 w-3.5 text-ink-3" aria-hidden />
                <ScaleStamp row={row} testId="cross-framing-scale" />
                <Stamp label="record" value={`${view.schema} · ${view.regime} · ${view.tier}`} />
                <Stamp label="as of" value={new Date(view.claim.asOf).toLocaleString()} />
              </div>
              <blockquote
                className="border-l-2 border-line-strong pl-2 text-body text-ink-1"
                data-testid="cross-framing-claim"
              >
                {view.claim.text}
              </blockquote>
              <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-label text-ink-3">
                <span
                  className="rounded border border-line bg-surf-2 px-1 text-ink-2"
                  title={
                    view.verdict.kind === 'not-recorded' || view.verdict.kind === 'not-checked'
                      ? CLAIM_VERDICT_ABSENCE_EXPLAIN
                      : view.verdict.label
                  }
                  data-testid="cross-framing-verdict"
                >
                  {view.verdict.label}
                </span>
                <span data-testid="cross-framing-counts">{countsSentence(view.counts)}</span>
                <span>
                  · quoted from <span className="text-ink-2">{view.claim.unitLabel}</span>
                  {' · '}
                  {view.claim.markers.length > 0 ? (
                    <span className="font-mono">{view.claim.markers.join(' ')}</span>
                  ) : (
                    <span>no marker on this span</span>
                  )}
                </span>
                <span>
                  ·{' '}
                  {view.claim.chosenBy === 'requested'
                    ? 'the claim you opened'
                    : view.claim.chosenBy === 'most-contested'
                      ? 'the desk’s most contested claim (nothing was requested)'
                      : 'the record’s first quoted claim (nothing contested)'}
                </span>
              </div>
            </header>

            {/* 2 — THE UNIT TABLE */}
            <section data-testid="cross-framing-units">
              <SectionHead tip="One row per unit on the RECORD's own coverage roster. A unit frames the claim when its read cites at least one of the same signals — a link the producer stamped, never a similarity guess. A unit with no read shows its typed absence in the route's own words, with the proof and the clock; a stale absence says so.">
                the units
              </SectionHead>
              <table className="w-full table-fixed text-body">
                <thead>
                  <tr className="text-left text-label uppercase tracking-wide text-ink-3">
                    <th className="w-[22%] pb-1 font-normal">unit</th>
                    <th className="w-[48%] pb-1 font-normal">how its read frames the claim</th>
                    <th className="w-[30%] pb-1 font-normal">what it rests on</th>
                  </tr>
                </thead>
                <tbody>
                  {view.units.map((r) => (
                    <UnitRow key={r.unit} row={r} />
                  ))}
                </tbody>
              </table>
            </section>

            {/* 3 — THE LENS ROW */}
            <section data-testid="cross-framing-lenses">
              <SectionHead tip="The six stance-typed lenses (docs/GLOSSARY.md § leans). A lens appears as framing this claim only when its own cited refs name one of the claim's signals or the read it was quoted from. Its prior is the one its persona module declares; the score is that entry's own faithfulness gate.">
                the leans on the same evidence
              </SectionHead>
              <ul className="rounded border border-line bg-surf-1 px-2">
                {view.lenses.map((l) => (
                  <LensRow key={l.analystId} row={l} />
                ))}
              </ul>
            </section>

            {/* 4 — WHERE THE LAYERS DIVERGE */}
            <section data-testid="cross-framing-divergence">
              <SectionHead tip="The desk's own layer-divergence receipt — the CHANGE in this country's layer-to-layer gap against its own rolling baseline, never the raw gap. Pair rows travel verbatim from /v3/layers/divergence.">
                where the layers diverge
              </SectionHead>
              <div className="rounded border border-line bg-surf-1 p-2 text-label">
                {view.divergence.absent ? (
                  <div className="text-ink-3" data-testid="cross-framing-divergence-absent">
                    {view.divergence.absent === 'no-response' &&
                      'the divergence route did not answer — nothing here is a measurement of zero divergence'}
                    {view.divergence.absent === 'not-measured' &&
                      'the divergence read itself FAILED (measured: false) — not a quiet instrument'}
                    {view.divergence.absent === 'no-run' &&
                      'no run yet — the unit has never written a receipt for any desk'}
                    {view.divergence.absent === 'desk-not-resolved' &&
                      `the newest receipt (${view.divergence.asOf ?? 'undated'}) does not resolve this desk — it was not measured, not measured as agreeing`}
                  </div>
                ) : (
                  <div className="space-y-1">
                    <div className="text-ink-3">
                      receipt{' '}
                      <span className="font-mono text-ink-2">
                        {view.divergence.asOf ?? 'undated'}
                      </span>
                      {view.divergence.methodVersion && (
                        <> · method {view.divergence.methodVersion}</>
                      )}
                      {view.divergence.desk && (
                        <> · {view.divergence.desk.sources_mapped} sources mapped</>
                      )}
                    </div>
                    {view.divergence.pairs.map(({ pair, state, meaning }) => (
                      <div
                        key={pair.pair_id}
                        className="flex flex-wrap items-baseline gap-1.5"
                        data-testid={`cross-framing-pair-${pair.pair_id}`}
                      >
                        <span className="font-mono text-ink-2">
                          {pair.layer_a} ↔ {pair.layer_b}
                        </span>
                        {/* The state badge and the reason are the divergence
                            map's own words, verbatim — `aperture_excluded` and
                            `below_threshold` are different findings and neither
                            is prettified into a sentence this panel wrote. */}
                        <span className="rounded border border-line bg-surf-2 px-1 text-ink-3">
                          {PAIR_STATE_LABEL[state]}
                        </span>
                        <span className="font-mono text-ink-3">
                          {pair.no_fire_reason || 'fired'}
                        </span>
                        {meaning && <span className="text-ink-3">— {meaning}</span>}
                        {(pair.excluded_layers ?? []).map((x) => (
                          <span key={x.layer} className="text-ink-3">
                            · {x.layer}: {x.state} — {x.reason}
                          </span>
                        ))}
                      </div>
                    ))}
                    {view.divergence.fired && (
                      <div
                        className="text-accent-warning"
                        data-testid="cross-framing-divergence-fired"
                      >
                        fired: {view.divergence.fired.pair_id}
                        {view.divergence.fired.direction && (
                          <> · {view.divergence.fired.direction}</>
                        )}{' '}
                        · {formatZ(view.divergence.fired.z)} on{' '}
                        {view.divergence.fired.day ?? 'an unrecorded day'} ·{' '}
                        <RecordLink
                          kind="finding"
                          id={view.divergence.fired.finding_id}
                          label="the receipt"
                          origin="cross-framing"
                        />
                      </div>
                    )}
                  </div>
                )}
                {view.divergence.classificationAudit && (
                  <div
                    className="mt-1 flex items-start gap-1.5 rounded border border-accent-warning/45 bg-accent-warning/10 p-1.5 text-accent-warning"
                    data-testid="cross-framing-divergence-audit"
                  >
                    <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                    <span>{view.divergence.classificationAudit}</span>
                  </div>
                )}
              </div>
            </section>

            {/* 5 — WHAT NO UNIT SAYS */}
            <section data-testid="cross-framing-silence">
              <SectionHead tip="Absence, not contradiction. A unit that never ran has not disagreed with the claim, and a subject the desk declared absent is not one the desk denied. Every row quotes a record the platform wrote.">
                what no unit says
              </SectionHead>
              <div className="space-y-1 rounded border border-line bg-surf-1 p-2 text-label text-ink-3">
                {view.silence.unnamedUnits.length > 0 && (
                  <div data-testid="cross-framing-unnamed">
                    named by 0 of {view.counts.roster} units:{' '}
                    <span className="text-ink-2">
                      {view.silence.unnamedUnits.map((u) => humanizeId(u)).join(' · ')}
                    </span>{' '}
                    — absent, not contradicted
                  </div>
                )}
                {view.silence.offRosterAbsences.map((a) => (
                  <div key={`${a.kind}:${a.subject}`} data-testid="cross-framing-off-roster">
                    <span className="text-ink-2">{a.kindLabel}</span> — {a.reason}
                    {a.stale && <span className="text-accent-warning"> · last known, not re-checked</span>}
                    {a.asOf && <> · as of {new Date(a.asOf).toLocaleString()}</>}
                  </div>
                ))}
                {view.silence.notMeasured.map((n) => (
                  <div key={n} data-testid="cross-framing-not-measured">
                    not measured for this desk: {n}
                  </div>
                ))}
                {view.silence.drops.map((d) => (
                  <div key={d.why} data-testid={`cross-framing-drop-${d.why}`}>
                    the record considered and did not carry {d.rows.length} read
                    {d.rows.length === 1 ? '' : 's'} — {humanizeId(d.why)}
                  </div>
                ))}
                {view.silence.tensionsExamined !== null && (
                  <div data-testid="cross-framing-tension-check">
                    the record examined {view.silence.tensionsExamined} unit pair
                    {view.silence.tensionsExamined === 1 ? '' : 's'} for tension and found{' '}
                    {view.silence.tensionsFound}
                    {view.silence.tensionScopeNote && <> ({view.silence.tensionScopeNote})</>}
                  </div>
                )}
                {view.silence.empty && (
                  <div data-testid="cross-framing-silence-empty">
                    every unit on the roster carries a read, the desk declared no typed absence,
                    and the record logged no drop — there is nothing unsaid to report.
                  </div>
                )}
              </div>
            </section>

            {/* 6 — ASK ABOUT THIS CLAIM */}
            <footer className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={askAboutClaim}
                className="inline-flex items-center gap-1 rounded border border-line bg-surf-2 px-2 py-1 text-label text-ink-2 hover:border-line-strong"
                data-testid="cross-framing-ask"
              >
                <MessageSquare className="h-3 w-3" aria-hidden />
                ask about this claim ▸
              </button>
              <span className="text-label text-ink-3">
                {pinned
                  ? 'scope pin set — Consult now answers about this claim, and its provenance census line reports what the answer rested on'
                  : 'sets the ambient consult scope pin to this claim; the same session follows you here'}
              </span>
            </footer>
          </>
        )}
      </div>
    </PanelChrome>
  )
}
