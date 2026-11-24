/**
 * JudgmentBands — the Morning Read's twenty-minute judgment surface (7b-iv).
 *
 * The landing already renders ONE composed record beautifully. It said nothing
 * about the other fifty desks, so the question the stance is named for —
 * "what happened, what moved, what does it mean?" — was answered a third of
 * the way. This is the other two thirds, above the read, in four bands:
 *
 *   CHANGED  what landed since the reader last looked, cited
 *   CHECKED  which claims held and which did not, the failures quoted
 *   GAPS     where the evidence ran out
 *   DUE      what the platform owes an answer on today
 *
 * ── WHAT THIS COMPONENT DOES NOT DO ───────────────────────────────────────
 *  * It does not compute a verdict. Every derivation is in `lib/
 *    morningReadBands` and is pure, so each band's argument is arguable in a
 *    unit test rather than through a rendered panel.
 *  * It does not add an analyst, an LLM call or a dependency. Three existing
 *    routes and one additive section on a fourth carry the whole surface.
 *  * It does not replace the read. The assembly reader below is untouched and
 *    reachable exactly as it was — this is the same panel made judgmental.
 *  * It does not emit telemetry. `MorningRead.tsx` already fires one
 *    `brief_read` per rendered read; a timer that emitted on a tick would
 *    inflate the number the 90-day wager is graded on. The timer READS the
 *    telemetry's session clock and writes nothing.
 *
 * ── WHY EVERY LIST IS CAPPED, LOUDLY ──────────────────────────────────────
 * Twenty minutes is the design constraint, so the surface shows the worst
 * `DESKS_PER_BAND` desks and the worst `ITEMS_PER_DESK` rows on each (both in
 * `bandChrome.tsx`, with the rest of the bands' shared furniture), and then
 * SAYS how many it is holding back. A capped list presented as the whole story
 * is the one failure mode a judgment surface cannot have — it is the same
 * honesty contract `/since`'s own `truncated` flag carries, moved up into the
 * layout.
 *
 * The GAPS band lives in `GapsBand.tsx`: it reads `/v3/absence` once per desk
 * it shows, which is a fetch shape none of the other three has.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { apiGet } from '@/lib/api'
import { SeverityDot } from '@/components/SeverityBadge'
import type { CountryScorecard } from '@/lib/evalOps'
import { PROFILE_UI } from '@/lib/profileUi'
import {
  bandsSourceText,
  bandTestId,
  useBand,
  useBands,
  alertMarkText,
  changedDeskCount,
  changedList,
  checkedBand,
  dueBand,
  forecastMarkLabel,
  formatElapsed,
  READ_BUDGET_MS,
  resolveVisit,
  visitCursor,
  visitElapsedMs,
  type BandId,
  type ChangedRow,
  type CheckedDesk,
  type DeskGroup,
  type ForecastDue,
  type JudgmentFindingRow,
  type JudgmentSince,
  type MorningVisit,
} from '@/lib/morningReadBands'
import GapsBand, { useGapViews } from './GapsBand'
import UnverifiedMark from './UnverifiedMark'
import {
  BandFrame,
  bandCount,
  CitedRow,
  DESKS_PER_BAND,
  DeskHead,
  HeldBack,
  ITEMS_PER_DESK,
  pendingCountText,
  StateLine,
  stateOf,
  type QueryState,
} from './bandChrome'
import { asSeverity, scrollToAnchor, utcStamp } from './readFormat'
import { ScaleStamp } from '@/components/ScaleStamp'
import { useScope } from '@/state/scope'

/** The profile's subject noun in a band's note (UI-B2; the exemplar's "desk"). */
const SUBJECT = PROFILE_UI.subject.singular.toLowerCase()
const SUBJECTS = PROFILE_UI.subject.plural.toLowerCase()

/** The CHECKED band's population: one page of the day's findings. */
export const CHECKED_LIMIT = 200
/** Every query on this surface polls on the read's own five-minute cadence. */
const REFETCH_MS = 5 * 60_000

// ---------------------------------------------------------------------------
// The read timer (the telemetry's session clock, displayed — never emitted)
// ---------------------------------------------------------------------------

function ReadTimer({ visit }: { visit: MorningVisit }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  const elapsed = visitElapsedMs(visit, now)
  const over = elapsed > READ_BUDGET_MS
  return (
    <span
      className={`font-mono text-xs ${over ? 'text-ink-2' : 'text-ink-3'}`}
      data-testid="morning-read-timer"
      title={
        over
          ? 'Past the twenty minutes this surface is designed for.'
          : 'This visit, against the twenty-minute budget. Nothing is emitted by the timer.'
      }
    >
      {formatElapsed(elapsed)} / {formatElapsed(READ_BUDGET_MS)}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Shared furniture
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Band 1 — CHANGED
// ---------------------------------------------------------------------------

/** Rows the ranked CHANGED list shows before its held-back line. */
export const CHANGED_ROWS_SHOWN = DESKS_PER_BAND * ITEMS_PER_DESK

function ChangedBand({
  rows,
  cursor,
  state,
}: {
  rows: ChangedRow[]
  cursor: string
  state: 'loading' | 'error' | 'ready'
}) {
  const spec = useBand('changed')
  const total = rows.length
  const desks = changedDeskCount(rows)
  return (
    <BandFrame
      id="changed"
      label={spec.label}
      question={spec.question}
      count={bandCount(state, total)}
      state={state}
      note={`${total} across ${desks} ${desks === 1 ? SUBJECT : SUBJECTS} · worst first · since ${utcStamp(cursor) ?? cursor}`}
    >
      {state === 'loading' && <StateLine testid="morning-read-changed-loading">Reading the diff…</StateLine>}
      {state === 'error' && (
        <StateLine testid="morning-read-changed-error">
          The since-diff did not answer. Nothing is being hidden — this band simply has no data
          this paint.
        </StateLine>
      )}
      {state === 'ready' && rows.length === 0 && (
        <StateLine testid="morning-read-changed-empty">
          Nothing landed since {utcStamp(cursor) ?? cursor}. That is a real answer, not a stalled
          panel — the engine writes on its own cadence.
        </StateLine>
      )}
      {rows.length > 0 && (
        <ul className="space-y-0.5" data-testid="morning-read-changed-list">
          {rows.slice(0, CHANGED_ROWS_SHOWN).map((item) => (
            <CitedRow
              key={`${item.kind}-${item.id}`}
              rowKind={item.rowKind}
              rowId={item.id}
              label={item.headline}
              testid="morning-read-changed-row"
            >
              <span className="flex items-baseline gap-2 text-xs">
                {asSeverity(item.severity) ? (
                  <SeverityDot
                    severity={asSeverity(item.severity)!}
                    className="h-2 w-2 shrink-0 translate-y-[1px]"
                  />
                ) : (
                  <span className="inline-block h-2 w-2 shrink-0" aria-hidden />
                )}
                <span className="min-w-0 flex-1 text-ink-1">{item.headline}</span>
                {item.alerts.length > 0 && (
                  <span
                    className="shrink-0 rounded border border-line px-1 text-label text-ink-2"
                    title={`alert ${item.alerts.map((a) => a.id).join(', ')}`}
                    data-testid="morning-read-changed-alert-mark"
                  >
                    {alertMarkText(item.alerts[0].channel)}
                    {item.alerts.length > 1 ? ` ×${item.alerts.length}` : ''}
                  </span>
                )}
              </span>
              <span className="block pl-4 text-label text-ink-3">
                <span className="text-ink-2" data-testid="morning-read-changed-desk">{item.deskLabel}</span>
                {' · '}
                {item.note} · {utcStamp(item.at) ?? item.at}
              </span>
            </CitedRow>
          ))}
        </ul>
      )}
      <HeldBack n={rows.length - CHANGED_ROWS_SHOWN} noun="change" />
    </BandFrame>
  )
}

// ---------------------------------------------------------------------------
// Band 2 — CHECKED
// ---------------------------------------------------------------------------

/** X4 READPATH — the block-less findings, split by the registry's state. */
function uncheckedText(pending: number, notJudged: number): string {
  return `${pending} judge pending, ${notJudged} not judged`
}

function CheckedBand({
  desks,
  cursor,
  state,
  truncated,
}: {
  desks: CheckedDesk[]
  cursor: string
  state: 'loading' | 'error' | 'ready'
  /** The findings page came back FULL — the window holds more than we read. */
  truncated: boolean
}) {
  const spec = useBand('checked')
  const flagged = desks.reduce((n, d) => n + d.claims.length, 0)
  const verified = desks.reduce((n, d) => n + d.verified, 0)
  const unchecked = desks.reduce((n, d) => n + d.unchecked, 0)
  const pending = desks.reduce((n, d) => n + d.pending, 0)
  const notJudged = desks.reduce((n, d) => n + d.notJudged, 0)
  return (
    <BandFrame
      id="checked"
      label={spec.label}
      question={spec.question}
      count={bandCount(state, flagged)}
      state={state}
      note={`${verified} verified · ${flagged} claim${flagged === 1 ? '' : 's'} flagged · unverified: ${uncheckedText(pending, notJudged)}`}
    >
      {state === 'loading' && <StateLine testid="morning-read-checked-loading">Reading the day&rsquo;s verdicts…</StateLine>}
      {state === 'error' && (
        <StateLine testid="morning-read-checked-error">
          The findings read did not answer, so no verdict is claimed either way.
        </StateLine>
      )}
      {state === 'ready' && desks.length === 0 && (
        <StateLine testid="morning-read-checked-empty">
          No finding was written since {utcStamp(cursor) ?? cursor}, so there is nothing to have
          checked.
        </StateLine>
      )}
      {state === 'ready' && desks.length > 0 && flagged === 0 && (
        <StateLine testid="morning-read-checked-clean">
          The verify pass flagged nothing on any desk this window. {unchecked > 0
            ? `${unchecked} finding${unchecked === 1 ? '' : 's'} carried no verify verdict (${uncheckedText(pending, notJudged)}) — an absence, not a pass.`
            : 'Every finding carried a verify block.'}
        </StateLine>
      )}
      {desks.slice(0, DESKS_PER_BAND).map((d) => (
        <div key={d.targetId} className="mb-3" data-testid="morning-read-checked-desk">
          <DeskHead
            label={d.label}
            note={`${d.verified} verified · ${d.unsupported} with a flagged claim · unverified: ${uncheckedText(d.pending, d.notJudged)}`}
          />
          {d.claims.length === 0 ? (
            <StateLine>
              {d.verified > 0
                ? 'Nothing flagged on this desk.'
                : 'Nothing on this desk carries a verify verdict, so nothing is claimed as held.'}
            </StateLine>
          ) : (
            <ul className="space-y-1">
              {d.claims.slice(0, ITEMS_PER_DESK).map((c, i) => (
                <CitedRow
                  key={`${c.findingId}-${i}`}
                  rowKind="finding"
                  rowId={c.findingId}
                  label={c.findingTitle}
                  testid="morning-read-checked-claim"
                >
                  <span className="block text-xs leading-relaxed text-ink-1">
                    &ldquo;{c.text}&rdquo;
                  </span>
                  <span className="block text-label text-ink-3">
                    {c.reasonLabel} · {c.analystId ?? 'unknown producer'} · {c.findingTitle}
                    {c.markers.length > 0 ? ` · cited [${c.markers.join('] [')}]` : ' · uncited'}
                  </span>
                </CitedRow>
              ))}
            </ul>
          )}
          <HeldBack n={d.claims.length - ITEMS_PER_DESK} noun="flagged claim" />
          {d.unverified.length > 0 && (
            <ul className="mt-1 space-y-0.5" data-testid="morning-read-checked-unverified">
              {d.unverified.slice(0, ITEMS_PER_DESK).map((u) => (
                <CitedRow
                  key={u.findingId}
                  rowKind="finding"
                  rowId={u.findingId}
                  label={u.title}
                  testid="morning-read-checked-unverified-row"
                >
                  <span className="flex flex-wrap items-baseline gap-2 text-xs">
                    <UnverifiedMark info={u.info} />
                    <span className="min-w-0 flex-1 text-ink-1">{u.title}</span>
                  </span>
                  <span className="block text-label text-ink-3">
                    {u.analystId ?? 'unknown producer'} · {utcStamp(u.producedAt) ?? u.producedAt}
                  </span>
                </CitedRow>
              ))}
            </ul>
          )}
          <HeldBack n={d.unverified.length - ITEMS_PER_DESK} noun="unverified finding" />
        </div>
      ))}
      <HeldBack n={desks.length - DESKS_PER_BAND} noun={SUBJECT} />
      {state === 'ready' && truncated && (
        <p
          className="mt-1 text-label leading-relaxed text-ink-3"
          data-testid="morning-read-checked-truncated"
        >
          Read over the newest {CHECKED_LIMIT} findings in the window, not all of them — an older
          flagged claim in a long window is not counted here. Widen it from the Live Feed.
        </p>
      )}
    </BandFrame>
  )
}

// ---------------------------------------------------------------------------
// Band 4 — DUE
// ---------------------------------------------------------------------------

function DueBand({
  groups,
  state,
}: {
  groups: DeskGroup<ForecastDue>[]
  state: 'loading' | 'error' | 'ready'
}) {
  const spec = useBand('due')
  const total = groups.reduce((n, g) => n + g.items.length, 0)
  return (
    <BandFrame
      id="due"
      label={spec.label}
      question={spec.question}
      count={bandCount(state, total)}
      state={state}
      note={`${total} call${total === 1 ? '' : 's'} past their window`}
    >
      {state === 'ready' && groups.length > 0 && (
        <StateLine testid="morning-read-due-legacy-label">
          Acute-forecast pilot calls — untrusted, legacy, under diagnosis. Not the sealed forecast ledger.
        </StateLine>
      )}
      {state === 'loading' && <StateLine testid="morning-read-due-loading">Reading the forecast ledger…</StateLine>}
      {state === 'error' && (
        <StateLine testid="morning-read-due-error">
          The since-diff did not answer, so no obligation is claimed as settled.
        </StateLine>
      )}
      {state === 'ready' && groups.length === 0 && (
        <StateLine testid="morning-read-due-empty">
          Every pre-registered call whose window has closed has been graded. Withdrawn
          (<span className="font-mono">voided:</span>) calls are not obligations and are excluded.
        </StateLine>
      )}
      {groups.slice(0, DESKS_PER_BAND).map((g) => (
        <div key={g.targetId} className="mb-3" data-testid="morning-read-due-desk">
          <DeskHead label={g.label} note={`${g.items.length} due`} />
          <ul className="space-y-1.5">
            {g.items.slice(0, ITEMS_PER_DESK).map((f) => (
              <li key={f.id} data-testid="morning-read-due-row" className="px-1">
                <span className="block text-xs text-ink-1">
                  {f.event_class} · p={f.p.toFixed(2)} (base {f.p_base.toFixed(2)}) ·{' '}
                  {f.days_overdue.toFixed(1)}d past its window
                </span>
                <span className="block text-label text-ink-3">
                  {forecastMarkLabel(f.mark)} · window closed {utcStamp(f.window_end) ?? f.window_end}
                </span>
                {/* K3 — WHICH probability scale the p above is a reading on,
                    with the moment it was issued. The clamp is what makes p a
                    scale rather than an assertion, so a call minted before it
                    reads "unstamped" rather than being counted as comparable
                    with today's. */}
                <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-label text-ink-3">
                  <ScaleStamp row={f} testId={`morning-read-due-scale-${f.id}`} />
                  <span data-testid="morning-read-due-issued">
                    issued {utcStamp(f.issued_at) ?? f.issued_at}
                  </span>
                </span>
                <details className="mt-0.5">
                  <summary className="cursor-pointer text-label text-ink-3">
                    The frozen resolution test
                  </summary>
                  <p
                    className="mt-0.5 whitespace-pre-wrap break-words font-mono text-label leading-relaxed text-ink-2"
                    data-testid="morning-read-due-test"
                  >
                    {f.resolution_test || 'no resolution test recorded on this row'}
                  </p>
                </details>
              </li>
            ))}
          </ul>
          <HeldBack n={g.items.length - ITEMS_PER_DESK} noun="call" />
        </div>
      ))}
      <HeldBack n={groups.length - DESKS_PER_BAND} noun={SUBJECT} />
    </BandFrame>
  )
}

// ---------------------------------------------------------------------------
// The reading-order rail
// ---------------------------------------------------------------------------

function ReadingRail({
  counts,
  states,
  visit,
}: {
  /** Null until the band has been read — the rail prints `—`, never `0`. */
  counts: Record<BandId, number | null>
  states: Record<BandId, QueryState>
  visit: MorningVisit
}) {
  // The bands' words: the projection's when the plane answers; the platform's
  // reading order otherwise — and the rail says which, naming the refusal.
  const { bands, read } = useBands()
  return (
    <nav
      className="read-col mb-5 flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line pb-2"
      aria-label="Reading order"
      data-testid="morning-read-rail"
      data-bands-source={read.state === 'ok' ? 'projection' : read.state === 'absent' ? read.name : 'loading'}
    >
      <span className="text-label uppercase tracking-wider text-ink-3" title={bandsSourceText(read)}>
        Read in order
      </span>
      {bands.map((band, i) => (
        <button
          key={band.id}
          type="button"
          onClick={() => scrollToAnchor(bandTestId(band.id))}
          title={band.question}
          className="text-xs text-ink-2 underline decoration-dotted underline-offset-2 hover:text-ink-1"
          data-testid={`morning-read-rail-${band.id}`}
        >
          {i + 1}. {band.label}
          <span
            className="ml-1 font-mono text-ink-3"
            data-testid={`morning-read-rail-count-${band.id}`}
          >
            {counts[band.id] ?? pendingCountText(states[band.id])}
          </span>
        </button>
      ))}
      <span className="ml-auto flex items-center gap-2">
        {visit.firstVisit && (
          <span className="text-xs text-ink-3" data-testid="morning-read-first-visit">
            first visit — showing since yesterday 06:00Z
          </span>
        )}
        <ReadTimer visit={visit} />
      </span>
    </nav>
  )
}

// ---------------------------------------------------------------------------
// The wall's scope (X3/D3)
// ---------------------------------------------------------------------------

/**
 * A SUBJECT scope narrows every band to that subject (a panel FILTERS by
 * scope — `state/scope.ts`). Before X3/D3 a rail click on Brazil scoped the
 * feed while the bands kept listing Ukraine and Iran. Rows with no subject
 * (the unscoped group) leave with the rest; "show every …" clears the scope.
 */
export function onSubject<T extends { targetId: string }>(rows: readonly T[], subjectId: string | null): T[] {
  return subjectId ? rows.filter((r) => r.targetId === subjectId) : [...rows]
}

function ScopedLine({ label, onClear }: { label: string; onClear: () => void }) {
  return (
    <div className="read-col mb-3 flex items-center gap-2 text-xs text-ink-2" data-testid="morning-read-scoped">
      <span>
        Scoped to <span className="font-medium text-ink-1">{label}</span> — every band below reads this {SUBJECT}{' '}
        only.
      </span>
      <button
        type="button"
        onClick={onClear}
        className="text-ink-3 underline decoration-dotted underline-offset-2 hover:text-ink-1"
        data-testid="morning-read-scope-clear"
      >
        show every {SUBJECT}
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The surface
// ---------------------------------------------------------------------------

export default function JudgmentBands() {
  // ONE visit per mount: `resolveVisit` rolls the stored record forward on a
  // new telemetry session, so re-resolving on every render would keep handing
  // the diff a cursor of "now" and hide everything that landed overnight.
  const visit = useMemo(() => resolveVisit(), [])
  const cursor = useMemo(() => visitCursor(visit), [visit])

  const since = useQuery<JudgmentSince>({
    queryKey: ['morning-judgment', 'since', cursor],
    refetchInterval: REFETCH_MS,
    queryFn: () => apiGet<JudgmentSince>(`/v3/since?cursor=${encodeURIComponent(cursor)}`),
  })

  // P-mobile/7b-v — `fields=judgment`: the verify verdict whole
  // (`unsupported_spans` included) plus reduced citations, never `data` /
  // `derived_from` / `body` — the CHECKED band reads none of those three,
  // and they were 4.95 of a measured 5.5 MB 200-row page (2026-09-24).
  const day = useQuery<{ data: JudgmentFindingRow[] }>({
    queryKey: ['morning-judgment', 'checked', cursor],
    refetchInterval: REFETCH_MS,
    queryFn: () =>
      apiGet<{ data: JudgmentFindingRow[] }>(
        `/findings?since=${encodeURIComponent(cursor)}&limit=${CHECKED_LIMIT}&fields=judgment`,
      ),
  })

  const cards = useQuery<CountryScorecard[]>({
    queryKey: ['morning-judgment', 'scorecards'],
    refetchInterval: REFETCH_MS,
    queryFn: () => apiGet<CountryScorecard[]>('/v3/eval/country_scorecard'),
  })

  // X3/D3 — a subject scope (a rail click, ⌘K, a map click) narrows every band.
  const wallScope = useScope((s) => s.scope)
  const clearScope = useScope((s) => s.clear)
  const subjectId = wallScope?.kind === 'target' ? wallScope.id : null

  const changed = useMemo(() => {
    const rows = changedList(since.data)
    return subjectId ? rows.filter((r) => r.deskId === subjectId) : rows
  }, [since.data, subjectId])
  const checked = useMemo(() => onSubject(checkedBand(day.data?.data), subjectId), [day.data, subjectId])
  const due = useMemo(() => onSubject(dueBand(since.data?.forecasts_due), subjectId), [since.data, subjectId])
  const scopedCards = useMemo(
    () => (subjectId && cards.data ? cards.data.filter((c) => c.target_id === subjectId) : cards.data),
    [cards.data, subjectId],
  )
  // k5b — the GAPS band reads `/v3/absence` once per desk it shows. The hook
  // lives beside that band; the count comes back here so the reading rail
  // links to a number the band actually renders.
  // U-5 — the per-desk absence reads wait for the band to come near view.
  const [gapsNear, setGapsNear] = useState(false)
  const markGapsNear = useCallback(() => setGapsNear(true), [])
  const gaps = useGapViews(scopedCards, { enabled: gapsNear })

  // X1/D1 — a band's count is a number only once its routes have answered;
  // the GAPS band also waits on every per-desk absence read it is counting.
  const states: Record<BandId, QueryState> = {
    changed: stateOf(since),
    checked: stateOf(day),
    gaps: stateOf(cards),
    due: stateOf(since),
  }
  const counts: Record<BandId, number | null> = {
    changed: bandCount(states.changed, changed.length),
    checked: bandCount(states.checked, checked.reduce((n, d) => n + d.claims.length, 0)),
    gaps: gaps.pending ? null : bandCount(states.gaps, gaps.total),
    due: bandCount(states.due, due.reduce((n, g) => n + g.items.length, 0)),
  }

  return (
    <section className="px-6 pt-8" data-testid="morning-read-judgment">
      <ReadingRail counts={counts} states={states} visit={visit} />
      {subjectId && <ScopedLine label={wallScope?.label ?? subjectId} onClear={clearScope} />}
      <ChangedBand rows={changed} cursor={cursor} state={states.changed} />
      <CheckedBand
        desks={checked}
        cursor={cursor}
        state={states.checked}
        truncated={(day.data?.data?.length ?? 0) >= CHECKED_LIMIT}
      />
      <GapsBand
        {...gaps}
        state={states.gaps}
        onNear={markGapsNear}
        armed={states.changed !== 'loading' && states.checked !== 'loading'}
      />
      <DueBand groups={due} state={states.due} />
    </section>
  )
}
