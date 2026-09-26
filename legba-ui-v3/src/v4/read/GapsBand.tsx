/**
 * GapsBand — the Morning Read's third band, read off TYPED ABSENCE (k5b).
 *
 * "Where did the evidence run out?" used to be answered by the scorecard
 * alone: a dimension the card banded `insufficient-evidence`, or a desk with
 * no current composition. Both are real, and both are one organ's opinion of
 * one window. A desk can band cleanly on every dimension while three of its
 * covering sources have gone quiet, its external audit has searched twice and
 * decided nothing, and an operator has declared one of its information layers
 * absent outright. None of that reached the reader.
 *
 * `GET /v3/absence?scope=<desk>` answers all of it in one closed vocabulary of
 * seven kinds, and this band renders that answer:
 *
 *   * ONE CALL PER DESK SHOWN, cached for the page load (`staleTime:
 *     Infinity`). The read is several bounded scans per desk; the surface's
 *     five-minute poll would multiply that by the desk cap to re-learn an
 *     answer whose own `expires_at` is usually hours away.
 *   * THE THREE ANSWERS ARE KEPT APART. Items, read-and-clear, and NOT
 *     MEASURED are three different facts about a desk, and a blank row would
 *     say the first when the third is true. A kind the route could not read
 *     says so, in the route's own sentence.
 *   * EVERY ITEM CARRIES ITS CLOCK. `absenceHeadline` states when the absence
 *     was measured and, when its `expires_at` has passed, that it is the LAST
 *     KNOWN state and nobody has re-checked — an old absence must never read
 *     as a current one on a morning surface.
 *   * NOTHING IS DOUBLE-COUNTED. A scorecard dimension and a bounded unit
 *     share an id, so the card-derived rows are shown only where no typed
 *     absence already names the same subject (`deskGapView`).
 *
 * The derivation is entirely in `lib/morningReadBands`; this file fetches,
 * lays out, and adds no number of its own.
 */
import { useMemo } from 'react'
import { useQueries } from '@tanstack/react-query'

import { apiGet } from '@/lib/api'
import { selectRow } from '@/state/selection'
import {
  ABSENCE_KIND_LABEL,
  TYPED_ABSENCE_GLOSSARY_NOTE,
  absenceHeadline,
  absenceId,
  type AbsenceItem,
  type AbsenceKind,
  type AbsenceResponse,
} from '@/lib/absenceModel'
import type { CountryScorecard } from '@/lib/evalOps'
import {
  BANDS,
  deskGapView,
  gapDeskOrder,
  gapsBand,
  type AbsenceKindGroup,
  type DeskGapView,
} from '@/lib/morningReadBands'
import {
  BandFrame,
  DESKS_PER_BAND,
  DeskHead,
  HeldBack,
  ITEMS_PER_DESK,
  StateLine,
  type QueryState,
} from './bandChrome'
import { utcStamp } from './readFormat'

/** Items shown per kind per desk before the held-back line. Smaller than
 *  {@link ITEMS_PER_DESK} because a desk answers under up to seven kinds and
 *  the band's budget is the desk, not the kind. */
export const ABSENCE_ITEMS_PER_KIND = 2

/** The `proof.ref_kind`s that resolve to a real Inspector selection — the
 *  same two `useInspectorDetail.resolveAbsence` offers. The ROUTE says what a
 *  ref is; a `scorecard` card or a `map_version` has no resolver and is shown
 *  as text rather than as a click that would 404. */
const REF_KIND_TO_SELECTION: Partial<Record<string, string>> = {
  finding: 'finding',
  source: 'source',
}

export interface GapViews {
  views: DeskGapView[]
  /** Desks read cleanly — named, never silently dropped: "we looked here and
   *  found nothing" is an answer and a band that hides it implies it never
   *  looked. */
  clean: DeskGapView[]
  /** Rows this band is describing: typed absences plus the card-derived gaps
   *  no kind already names. */
  total: number
  /** Desks the band is NOT reading, because the cap bounds the fan-out. */
  heldBackDesks: number
}

/**
 * The band's data: one `/v3/absence` read per desk shown, folded against the
 * scorecard-derived gaps.
 *
 * Lives beside the component rather than inside it so the surface's reading
 * rail can carry the band's real count — a rail that counted only the
 * card-derived rows would under-report the band it links to.
 */
export function useGapViews(cards: CountryScorecard[] | undefined): GapViews {
  const derivedGroups = useMemo(() => gapsBand(cards), [cards])
  const order = useMemo(() => gapDeskOrder(derivedGroups, cards), [derivedGroups, cards])
  const desks = useMemo(() => order.slice(0, DESKS_PER_BAND), [order])

  const results = useQueries({
    queries: desks.map((d) => ({
      queryKey: ['morning-judgment', 'absence', d.targetId],
      // Cached for the page load: see the banner — this is not a five-minute
      // number and re-reading it on the band's poll buys nothing.
      staleTime: Infinity,
      retry: false,
      queryFn: () =>
        apiGet<AbsenceResponse>(`/v3/absence?scope=${encodeURIComponent(d.targetId)}`),
    })),
  })

  const derivedByDesk = useMemo(
    () => new Map(derivedGroups.map((g) => [g.targetId, g.items])),
    [derivedGroups],
  )

  const all = desks.map((d, i) => {
    const r = results[i]
    const state: QueryState = !r
      ? 'loading'
      : r.isLoading
        ? 'loading'
        : r.isError
          ? 'error'
          : 'ready'
    return deskGapView(
      d,
      derivedByDesk.get(d.targetId) ?? [],
      r?.data,
      state,
      ABSENCE_ITEMS_PER_KIND,
    )
  })

  const hasContent = (v: DeskGapView) =>
    v.state !== 'ready' ||
    v.absenceCount > 0 ||
    v.derived.length > 0 ||
    v.kinds.some((k) => k.state === 'not_measured')

  return {
    views: all.filter(hasContent),
    clean: all.filter((v) => !hasContent(v)),
    total: all.reduce((n, v) => n + v.absenceCount + v.derived.length, 0),
    heldBackDesks: Math.max(0, order.length - desks.length),
  }
}

/** One typed absence: the row selects the absence itself (the Inspector
 *  resolves its whole proof), and the proof's ref rides beside it as its own
 *  control wherever the route named a kind that resolves. */
function AbsenceRow({
  scope,
  kind,
  item,
  meaning,
}: {
  scope: string
  kind: AbsenceKind
  item: AbsenceItem
  meaning: string
}) {
  const refKind = item.proof.ref_kind
  const refSelection = refKind ? REF_KIND_TO_SELECTION[refKind] : undefined
  return (
    <li className="flex items-start gap-1.5">
      <button
        type="button"
        title={meaning}
        onClick={() =>
          selectRow(
            'absence',
            absenceId(scope, kind, item.subject),
            `${item.subject} — ${ABSENCE_KIND_LABEL[kind]}`,
            { origin: 'morning-read-judgment' },
          )
        }
        className="min-w-0 flex-1 rounded px-1 py-0.5 text-left hover:bg-surf-1"
        data-testid="morning-read-gaps-absence"
        data-stale={item.stale ? 'true' : 'false'}
      >
        <span className="block text-xs text-ink-1">{item.subject}</span>
        <span className="block text-[0.6875rem] leading-relaxed text-ink-3">
          {item.reason}
        </span>
        <span
          className={`block text-[0.6875rem] ${item.stale ? 'text-ink-2' : 'text-ink-3'}`}
          data-testid="morning-read-gaps-stamp"
        >
          {absenceHeadline(item)}
        </span>
      </button>
      {item.proof.ref && refSelection ? (
        <button
          type="button"
          title={item.proof.what_was_checked}
          onClick={() =>
            selectRow(refSelection, item.proof.ref as string, `what was checked — ${item.subject}`, {
              origin: 'morning-read-judgment',
            })
          }
          className="mt-0.5 shrink-0 rounded border border-line px-1 py-0.5 text-[0.625rem] text-ink-3 hover:bg-surf-1"
          data-testid="morning-read-gaps-proof-ref"
        >
          {refKind}
        </button>
      ) : (
        <span
          className="mt-0.5 shrink-0 px-1 py-0.5 text-[0.625rem] text-ink-3"
          title={item.proof.what_was_checked}
          data-testid="morning-read-gaps-proof-text"
        >
          {refKind ?? 'no ref'}
        </span>
      )}
    </li>
  )
}

/** One kind's block on one desk — items, or the state that explains why not. */
function KindGroup({ scope, group }: { scope: string; group: AbsenceKindGroup }) {
  if (group.state === 'not_measured') {
    return (
      <p
        className="mt-1 text-[0.6875rem] leading-relaxed text-ink-3"
        data-testid="morning-read-gaps-not-measured"
        data-kind={group.kind}
        title={group.meaning}
      >
        <span className="text-ink-2">{ABSENCE_KIND_LABEL[group.kind]}</span> — not
        measured for this desk: {group.notMeasured}
      </p>
    )
  }
  if (group.state === 'clear') return null
  return (
    <div className="mt-1" data-testid="morning-read-gaps-kind" data-kind={group.kind}>
      <p className="text-[0.6875rem] text-ink-2" title={group.meaning}>
        {ABSENCE_KIND_LABEL[group.kind]} · {group.count}
      </p>
      <ul className="space-y-0.5">
        {group.items.map((item) => (
          <AbsenceRow
            key={`${group.kind}-${item.subject}`}
            scope={scope}
            kind={group.kind}
            item={item}
            meaning={group.meaning}
          />
        ))}
      </ul>
      <HeldBack n={group.heldBack} noun={`${ABSENCE_KIND_LABEL[group.kind]} row`} />
    </div>
  )
}

/** One desk: its typed absence by kind, then whatever the card said that no
 *  kind already says. */
function DeskGaps({ view }: { view: DeskGapView }) {
  const clear = view.kinds.filter((k) => k.state === 'clear')
  const note =
    view.state === 'loading'
      ? 'reading the desk…'
      : view.state === 'error'
        ? 'the absence read did not answer'
        : `${view.absenceCount} typed · ${view.derived.length} from the card`
  return (
    <div className="mb-3" data-testid="morning-read-gaps-desk" data-desk={view.targetId}>
      <DeskHead label={view.label} note={note} />
      {view.state === 'loading' && (
        <StateLine testid="morning-read-gaps-desk-loading">
          Reading this desk&rsquo;s typed absence…
        </StateLine>
      )}
      {view.state === 'error' && (
        <StateLine testid="morning-read-gaps-desk-error">
          The typed-absence read did not answer for this desk, so nothing here is
          described as covered.
        </StateLine>
      )}
      {view.state === 'ready' &&
        view.kinds.map((g) => <KindGroup key={g.kind} scope={view.targetId} group={g} />)}
      {view.state === 'ready' && view.absenceCount === 0 && (
        <StateLine testid="morning-read-gaps-desk-empty">
          No typed absence is recorded for this desk as of{' '}
          {utcStamp(view.readAt) ?? view.readAt ?? 'the read above'}.
        </StateLine>
      )}
      {view.derived.length > 0 && (
        <div className="mt-1" data-testid="morning-read-gaps-derived">
          <p className="text-[0.6875rem] text-ink-2">from this desk&rsquo;s card</p>
          <ul className="space-y-0.5">
            {view.derived.slice(0, ITEMS_PER_DESK).map((item, i) => (
              <li key={`${item.kind}-${item.dimension ?? 'desk'}-${i}`}>
                <button
                  type="button"
                  onClick={() =>
                    selectRow('finding', item.cardId, `${view.label} scorecard`, {
                      origin: 'morning-read-judgment',
                    })
                  }
                  className="w-full rounded px-1 py-0.5 text-left hover:bg-surf-1"
                  data-testid="morning-read-gaps-row"
                >
                  <span className="block text-xs text-ink-1">
                    {item.dimension ?? 'whole desk'}
                  </span>
                  <span className="block text-[0.6875rem] text-ink-3">
                    {item.reason} · card {utcStamp(item.producedAt) ?? item.producedAt}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <HeldBack n={view.derived.length - ITEMS_PER_DESK} noun="card gap" />
        </div>
      )}
      {view.coveredByKind > 0 && (
        <p
          className="mt-1 text-[0.6875rem] text-ink-3"
          data-testid="morning-read-gaps-deduped"
        >
          {view.coveredByKind} card row{view.coveredByKind === 1 ? '' : 's'} not repeated
          here — a typed absence above already names the same subject, with its proof.
        </p>
      )}
      {view.state === 'ready' && clear.length > 0 && (
        <p
          className="mt-1 text-[0.6875rem] text-ink-3"
          data-testid="morning-read-gaps-clear-kinds"
        >
          Read for this desk, nothing absent:{' '}
          {clear.map((k) => ABSENCE_KIND_LABEL[k.kind]).join(', ')}.
        </p>
      )}
    </div>
  )
}

export default function GapsBand({
  views,
  clean,
  total,
  heldBackDesks,
  state,
}: GapViews & { state: QueryState }) {
  const deskCount = views.length + clean.length
  return (
    <BandFrame
      id="gaps"
      label={BANDS[2].label}
      question={BANDS[2].question}
      count={total}
      note={`${total} on ${deskCount} desk${deskCount === 1 ? '' : 's'} read`}
    >
      {state === 'loading' && (
        <StateLine testid="morning-read-gaps-loading">Reading the desks…</StateLine>
      )}
      {state === 'error' && (
        <StateLine testid="morning-read-gaps-error">
          The scorecard read did not answer, so this band has no desk set to read
          absence for and no desk is described as covered.
        </StateLine>
      )}
      {state === 'ready' && deskCount === 0 && (
        <StateLine testid="morning-read-gaps-empty">
          No desk has a card on file, so there is no desk set to read typed absence
          for. This band is empty because nothing was read, not because nothing is
          missing.
        </StateLine>
      )}
      {views.map((v) => (
        <DeskGaps key={v.targetId} view={v} />
      ))}
      {state === 'ready' && views.length === 0 && clean.length > 0 && (
        <StateLine testid="morning-read-gaps-all-clear">
          No typed absence is recorded on any desk read here.
        </StateLine>
      )}
      {clean.length > 0 && (
        <p className="mt-1 text-[0.6875rem] text-ink-3" data-testid="morning-read-gaps-clean">
          Read clean, nothing typed absent:{' '}
          {clean.map((v) => v.label).join(', ')}.
        </p>
      )}
      <HeldBack n={heldBackDesks} noun="desk" />
      {state === 'ready' && deskCount > 0 && (
        <p className="mt-1 text-[0.6875rem] leading-relaxed text-ink-3">
          Each desk read once from{' '}
          <span className="font-mono">/v3/absence</span> —{' '}
          <span
            className="cursor-help underline decoration-dotted underline-offset-2"
            title={TYPED_ABSENCE_GLOSSARY_NOTE}
            data-testid="morning-read-gaps-glossary"
          >
            typed absence
          </span>
          , seven kinds, each item with its proof and its clock. A kind the route
          could not read here says so; a kind it read and found nothing under is
          named as read. The card rows below each desk are the ones no kind already
          names.
        </p>
      )}
    </BandFrame>
  )
}
