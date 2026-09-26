/**
 * MorningRead — the reading surface for `assembly.v1` (D-4).
 *
 * THE PAGE THE OPERATOR READS EVERY MORNING. Under the composition demotion the
 * record is no longer composed prose: it is quoted desk prose with byte-level
 * origin attached, plus a published selection, plus a fenced interpretive
 * channel. That is three grains on one page, and ruling 5 requires each to be
 * present and correctly contexted. The hard part is that all three are honest
 * and only one of them is a READ.
 *
 * ── THE LAYOUT ────────────────────────────────────────────────────────────────
 * Ruling 3 makes Layout B the default — evidence map → verified record →
 * Assessment LAST — and requires the flip to stay cheap. It is one attribute:
 * `data-read-layout` on this element, four `order` rules in `globals.css`, and a
 * persisted external store (`lib/readLayout`) so the choice survives a Dockview
 * remount and a reload. No second component tree, no re-fetch, no re-render of
 * the bands.
 *
 * ── WHAT THIS SURFACE DELIBERATELY DOES NOT DO ────────────────────────────────
 *  * It does not compute. Every number on the page is on the row: the counts
 *    come from `drops.counts`, the concentration verdict from `lead.test`, the
 *    coverage from the persisted ledger (§1.4c), the tension counters from
 *    `tension_checked`. A reader that arithmetically derives a disclosure can
 *    disagree with the record it is disclosing.
 *  * It does not parse prose. Coverage came out of `## Coverage` and into
 *    structured data (VOICE §3.4 A3); the unsupported markers carry character
 *    offsets so the Assessment is marked without a second detector.
 *  * It does not add an endpoint. Spec §2.1b(4): `/api/v1/findings` filters
 *    `kind='finding'` and takes `analyst_id`, so both the spine and the
 *    Assessment are served on day one by the read path that already exists.
 *
 * ── WHICH INTERPRETIVE VOICE (Program 3, lane C) ──────────────────────────────
 * The Assessment shipped as a constant — `world_assessment`, one voice, hard
 * against the world spine. A constant cannot say "this tier has no voice", so a
 * country or thematic read fell through to the newest world Assessment and
 * rendered it under a spine-mismatch note: a voice about another record, shown
 * under this one. The channel is now a FUNCTION of the record (`lib/
 * assessmentChannel`): world → `world_assessment`, country → `country_assessment`
 * (fenced per desk), region/thematic → none, and "none" means no band at all.
 * Same component, same look, same badge machinery at every tier — the only
 * difference the reader sees is the word in the label line.
 *
 * ── TELEMETRY (D2e) ───────────────────────────────────────────────────────────
 * `brief_read` fires from THIS component's mount rather than only from the
 * stance switch, because a stance switch is already recorded by `workspace_open`
 * and does not prove a brief rendered. It is emitted with no subject and under
 * the ambient workspace so it shares `App.tsx`'s dedupe key on the landing path
 * (`readTelemetry.DEDUPE_MS`) — the count cannot inflate, and a Morning Read
 * opened from the palette in another stance, which today records nothing, is
 * finally counted. `citation_drill` rides `CitedProse`'s chips unchanged;
 * `lineage_walk` marks a per-block evidence drill; `finding_open` marks a walk
 * to an origin head.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { emitRead } from '@/lib/readTelemetry'
import CitedAssessment from '@/components/inspector/CitedAssessment'
import { SeverityDot } from '@/components/SeverityBadge'
import { extractCitations } from '@/lib/citationsModel'
import {
  READ_LAYOUT_DESCRIPTION,
  READ_LAYOUT_LABEL,
  setReadLayout,
  useReadLayout,
  type ReadLayout,
} from '@/lib/readLayout'
import {
  blockRefused,
  historyLabel,
  leadReason,
  orphanTensions,
  projectArms,
  projectAssembly,
  projectAssessment,
  readPayload,
  recordBadge,
  tensionsByAnchor,
  type AssemblyPayload,
  type AssemblyTension,
  type ReadFindingRow,
} from '@/lib/assemblyModel'
import {
  assessmentChannelFor,
  COUNTRY_SPINE_ANALYST,
  pickAssessmentRow,
  spineAnalystFor,
  WORLD_SPINE_ANALYST,
} from '@/lib/assessmentChannel'
import RecordBlock from './RecordBlock'
import TensionNote, { TensionNegative } from './TensionNote'
import EvidenceMap from './EvidenceMap'
import DropLedger, { CoverageLine } from './DropLedger'
import AssessmentBand, { AssessmentAbsent } from './AssessmentBand'
import { asSeverity, longStamp, plural, score, utcStamp } from './readFormat'
import { useScope } from '@/state/scope'

/** The spine's producer. Unchanged by the demotion (spec §1.1). */
const SPINE_ANALYST = WORLD_SPINE_ANALYST
const RUN_LIMIT = 12

interface FindingsResponse {
  data: ReadFindingRow[]
}

/**
 * One producer's runs. `targetId` is pushed only when the producer's rows are
 * per-desk and the surface knows WHICH desk — `/findings` takes a single
 * `target_id`, so this narrows a country query to the country on screen and
 * leaves a world query (whose rows carry no target) alone.
 */
function useRuns(
  analystId: string,
  opts: { enabled?: boolean; targetId?: string | null } = {},
) {
  const { enabled = true, targetId = null } = opts
  const query = targetId ? `&target_id=${encodeURIComponent(targetId)}` : ''
  return useQuery<FindingsResponse>({
    queryKey: ['morning-read', analystId, targetId],
    enabled: enabled && analystId !== '',
    refetchInterval: 5 * 60_000,
    queryFn: () =>
      apiGet<FindingsResponse>(
        `/findings?analyst_id=${encodeURIComponent(analystId)}${query}&limit=${RUN_LIMIT}`,
      ),
  })
}

/** The ordering flip. Two buttons, each saying what it does. */
function LayoutToggle({ layout }: { layout: ReadLayout }) {
  return (
    <div
      className="flex items-center gap-1"
      role="group"
      aria-label="Reading order"
      data-testid="read-layout-toggle"
    >
      {(['B', 'A'] as ReadLayout[]).map((id) => (
        <button
          key={id}
          type="button"
          onClick={() => setReadLayout(id)}
          aria-pressed={layout === id}
          title={READ_LAYOUT_DESCRIPTION[id]}
          data-testid={`read-layout-${id}`}
          className={`rounded border px-2 py-0.5 text-xs ${
            layout === id
              ? 'border-line-strong bg-surf-3 text-ink-1'
              : 'border-line text-ink-3 hover:text-ink-2'
          }`}
        >
          {READ_LAYOUT_LABEL[id]}
        </button>
      ))}
    </div>
  )
}

/**
 * The standfirst — one line naming the shape of the day, built from the payload
 * counters. This is where the page opens on the WORLD rather than on an as-of
 * line (VOICE §3.4 A4); the stamp stays above it as metadata.
 */
function standfirst(a: AssemblyPayload): string {
  const c = a.drops?.counts
  const parts: string[] = [
    `${plural(a.blocks.length, 'desk head')}, quoted verbatim with ${
      a.blocks.length === 1 ? 'its origin' : 'their origins'
    }`,
  ]
  if (c && c.not_selected > 0) parts.push(`${c.not_selected} ranked below the line`)
  if (c && c.shown_not_carried > 0) parts.push(`${c.shown_not_carried} shown and not quoted`)
  if (c && c.invisible_heads > 0) parts.push(`${c.invisible_heads} outside this surface's aperture`)
  const t = a.tension_checked
  if (t && t.pairs_found > 0) parts.push(`${plural(t.pairs_found, 'tension')} declared`)
  return `${parts.join('; ')}.`
}

/** The verify arms' verdict, when the row carries one. Quiet unless it failed. */
function GateBanner({ assembly, row }: { assembly: AssemblyPayload; row: ReadFindingRow }) {
  const arms = projectArms(row)
  const badge = recordBadge(assembly, arms)
  if (badge.gate === 'failed') {
    return (
      <p
        className="mb-4 rounded border border-line-strong bg-surf-2 px-3 py-2 text-xs leading-relaxed text-ink-1"
        data-testid="read-gate-failed"
      >
        A verify arm did not read 1.0 on this record. Under the assembly contract that is a
        construction regression, not a score — the affected blocks are withheld below rather than
        shown with a caveat.
      </p>
    )
  }
  return (
    <p className="mb-4 text-xs leading-relaxed text-ink-3" data-testid="read-record-badge">
      {badge.quoteFidelity !== null
        ? `Quote fidelity ${score(badge.quoteFidelity)} by construction`
        : 'Quote fidelity is not yet independently audited on this row'}
      {badge.coverageCompleteness !== null
        ? ` · coverage completeness ${score(badge.coverageCompleteness)}`
        : ''}
      {` · ${badge.dropCount} dropped · ${plural(badge.blocks, 'block')}.`} This record carries no
      faithfulness score: a composed number that can read 1.00 on a page containing a fact its own
      desk denies is not a badge.
    </p>
  )
}

/** Prior runs — now navigable, because the row can say which read it is. */
function RunHistory({
  runs,
  activeId,
  onSelect,
}: {
  runs: ReadFindingRow[]
  activeId: string
  onSelect: (id: string) => void
}) {
  if (runs.length === 0) return null
  return (
    <section className="read-col mt-10 border-t border-line pt-5" data-testid="read-history">
      <details>
        <summary className="cursor-pointer text-label uppercase tracking-wider text-ink-3">
          Prior reads &middot; {plural(runs.length, 'superseded run')}
        </summary>
        <ul className="mt-2 space-y-1">
          {runs.map((run) => {
            const label = historyLabel(projectAssembly(run))
            const sev = asSeverity(run.severity)
            return (
              <li key={run.id}>
                <button
                  type="button"
                  onClick={() => onSelect(run.id)}
                  aria-current={run.id === activeId ? 'true' : undefined}
                  className={`flex w-full items-baseline gap-2 rounded px-1 py-0.5 text-left text-xs ${
                    run.id === activeId ? 'bg-surf-3 text-ink-1' : 'text-ink-2 hover:bg-surf-1'
                  }`}
                  data-testid="read-history-row"
                >
                  {sev ? (
                    <SeverityDot severity={sev} className="h-2.5 w-2.5 shrink-0" />
                  ) : (
                    <span className="inline-block h-2.5 w-2.5 shrink-0" aria-hidden />
                  )}
                  <span className="font-mono shrink-0 text-ink-3">{utcStamp(run.produced_at)}</span>
                  {/* `date · lead desk/target · n blocks · n dropped` — VOICE §4.6.1.
                      A pre-assembly run has no counts, so it keeps its title. */}
                  <span className="truncate">
                    {label
                      ? `${label.lead} · ${label.blocks} blocks · ${label.dropped} dropped`
                      : (run.title ?? 'untitled run')}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      </details>
    </section>
  )
}

/** A run that predates the assembly: shown as what it is, never as an error. */
function LegacyRun({ row }: { row: ReadFindingRow }) {
  const payload = readPayload(row)
  const body =
    typeof payload.body === 'string' ? payload.body : typeof row.body === 'string' ? row.body : ''
  return (
    <section className="read-col" data-testid="read-legacy">
      <p className="mb-3 text-xs leading-relaxed text-ink-3">
        This run predates the assembly: it is composed prose, not a quoted record, so it carries no
        blocks, no drop ledger and no per-span origins. It is shown as written.
      </p>
      {body.trim() !== '' ? (
        <CitedAssessment
          text={body}
          citations={extractCitations(payload)}
          verification={row.verification ?? null}
          confidence={row.confidence ?? null}
          analystId={row.analyst_id ?? SPINE_ANALYST}
          targetId={row.target_id ?? null}
        />
      ) : (
        <p className="text-sm text-ink-3">This run published without a body.</p>
      )}
    </section>
  )
}

export interface MorningReadProps {
  /** Which producer's reads to render. Defaults to the world spine. */
  analystId?: string
}

export default function MorningRead({ analystId = SPINE_ANALYST }: MorningReadProps) {
  const [pinnedRunId, setPinnedRunId] = useState<string | null>(null)
  const layout = useReadLayout()

  // SCOPE decides WHICH read is on the page (WORKSTATION_V2_FLOW_DESIGN §4).
  //
  // Clicking a row in the Navigator sets the scope; this surface renders that
  // row. Both halves matter: the PRODUCER (so a region or country composition
  // renders here, not only the world spine) and the RUN (so a prior read opens
  // in place rather than through the history disclosure). Nothing else about
  // the reader changes — same query, same projectors, same layout toggle.
  //
  // A manual history pick still wins, because it is the more recent instruction;
  // it is reset whenever the scope moves to a different report.
  const wallScope = useScope((s) => s.scope)
  const scopedReportId = wallScope?.kind === 'report' ? wallScope.id : null
  const scopedAnalyst =
    wallScope?.kind === 'report' ? (wallScope.members.analystIds[0] ?? null) : null
  // A scope that names an interpretive CHANNEL means "read the record that voice
  // was written from", never "render the voice as the record" — so a channel
  // producer resolves to ITS spine (`country_assessment` → `country_composition`),
  // which with the desk below lands on the record the voice actually grades.
  const effectiveAnalyst =
    (scopedAnalyst ? (spineAnalystFor(scopedAnalyst) ?? scopedAnalyst) : null) ?? analystId
  // The desk a report scope names, when it names exactly one AND the producer on
  // screen is the per-desk one. `country_composition` publishes ~60 rows a cycle
  // and `RUN_LIMIT` is 12, so without this a country scope could ask for twelve
  // newest country reads and not get the country it was scoped to.
  const scopedTarget =
    effectiveAnalyst === COUNTRY_SPINE_ANALYST &&
    wallScope?.kind === 'report' &&
    wallScope.members.targetIds.length === 1
      ? wallScope.members.targetIds[0]
      : null
  useEffect(() => {
    setPinnedRunId(scopedReportId)
  }, [scopedReportId])

  const spine = useRuns(effectiveAnalyst, { targetId: scopedTarget })

  const runs = useMemo(() => {
    const rows = (spine.data?.data ?? []).filter((r) => r.analyst_id === effectiveAnalyst)
    return [...rows].sort((a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at))
  }, [spine.data, effectiveAnalyst])

  const latest = runs[0] ?? null
  const active = (pinnedRunId ? runs.find((r) => r.id === pinnedRunId) : null) ?? latest
  const assembly = useMemo(() => (active ? projectAssembly(active) : null), [active])

  // brief_read — see the module header on why it is emitted here and subject-less.
  const activeId = active?.id ?? null
  useEffect(() => {
    if (activeId === null) return
    emitRead('brief_read')
  }, [activeId])

  // WHICH voice belongs to this record — a function of the producer and the tier
  // its payload declares, never a constant. `null` is a real answer: a region or
  // thematic read has no interpretive channel, and rendering the world's here
  // would put a voice written about another record under this one.
  const channelSpec = useMemo(() => assessmentChannelFor(active, assembly), [active, assembly])
  const channel = useRuns(channelSpec?.analystId ?? '', {
    enabled: channelSpec !== null,
    targetId: channelSpec?.targetScoped ? (active?.target_id ?? null) : null,
  })

  const assessmentRow = useMemo(
    () => pickAssessmentRow(channel.data?.data, channelSpec, active),
    [channel.data, channelSpec, active],
  )

  const assessment = useMemo(
    () => (assessmentRow ? projectAssessment(assessmentRow) : null),
    [assessmentRow],
  )

  if (spine.isLoading) {
    return (
      <div className="read-col animate-pulse px-6 py-8" data-testid="read-loading">
        <div className="mb-3 h-6 w-2/3 rounded bg-surf-3" />
        <div className="mb-6 h-3 w-40 rounded bg-surf-3" />
        <div className="space-y-2.5">
          <div className="h-3 w-full rounded bg-surf-3" />
          <div className="h-3 w-11/12 rounded bg-surf-3" />
          <div className="h-3 w-4/6 rounded bg-surf-3" />
        </div>
      </div>
    )
  }

  if (spine.error instanceof Error) {
    return (
      <div className="read-col px-6 py-8" data-testid="read-error">
        <p className="rounded border border-line-strong bg-surf-2 px-3 py-2 text-sm text-ink-1">
          Couldn&rsquo;t load the read: {spine.error.message}
        </p>
      </div>
    )
  }

  if (!active) {
    return (
      <div className="read-col px-6 py-8 text-sm text-ink-3" data-testid="read-empty">
        No read published yet. <span className="font-mono text-ink-2">{effectiveAnalyst}</span> composes one
        on its daily cadence.
      </div>
    )
  }

  const isLatest = active.id === latest?.id
  const arms = projectArms(active)
  const anchored: Map<number, AssemblyTension[]> = assembly
    ? tensionsByAnchor(assembly)
    : new Map()
  const orphans = assembly ? orphanTensions(assembly) : []
  const leadOrdinals = new Set(assembly?.lead?.block_ordinals ?? [])
  const reason = leadReason(assembly?.lead ?? null)

  return (
    <article className="px-6 py-8" data-testid="morning-read">
      <header className="read-col mb-8">
        <div className="text-label uppercase tracking-wider text-ink-3" data-testid="read-kicker">
          {assembly ? `${assembly.tier} read` : 'read'} &middot;{' '}
          {longStamp(assembly?.as_of ?? active.produced_at) ?? 'time unknown'}
          {assembly && (
            <span className="ml-2 normal-case tracking-normal">
              {assembly.schema} &middot; {assembly.regime}
            </span>
          )}
          {!isLatest && (
            <span className="ml-2 normal-case tracking-normal" data-testid="read-superseded">
              &middot; superseded run
            </span>
          )}
        </div>

        <div className="mt-1.5 flex items-start justify-between gap-4">
          <h1 className="text-xl font-semibold leading-snug text-ink-1" data-testid="read-title">
            {active.title ?? 'Untitled read'}
          </h1>
          <div className="shrink-0 pt-0.5">
            <LayoutToggle layout={layout} />
          </div>
        </div>

        {assembly && (
          <p className="mt-2 text-sm leading-relaxed text-ink-2" data-testid="read-standfirst">
            {standfirst(assembly)}
          </p>
        )}
        {!isLatest && (
          <button
            type="button"
            onClick={() => setPinnedRunId(null)}
            className="mt-2 text-xs text-ink-3 underline decoration-dotted underline-offset-2 hover:text-ink-2"
            data-testid="read-back-to-latest"
          >
            &larr; Back to the current read
          </button>
        )}
      </header>

      {!assembly ? (
        <LegacyRun row={active} />
      ) : (
        <div className="read-page" data-read-layout={layout} data-testid="read-page">
          <EvidenceMap assembly={assembly} />

          <section className="read-col mb-8" data-band="record" data-testid="read-record">
            <div className="read-band-head">
              <h2>The record</h2>
              <div className="read-rule" />
              <span className="read-band-note">
                {plural(assembly.blocks.length, 'block')} &middot; canonical &middot; quotation spine
              </span>
            </div>

            <GateBanner assembly={assembly} row={active} />

            {reason && (
              <p className="mb-5 text-xs leading-relaxed text-ink-3" data-testid="read-lead-reason">
                {assembly.lead?.kind === 'earned_single'
                  ? `Lead — block ${assembly.lead.block_ordinals.join(', ')}. `
                  : assembly.lead?.kind === 'co_leads'
                    ? `Co-leads — blocks ${assembly.lead.block_ordinals.join(', ')}. `
                    : 'No lead position this cycle. '}
                {reason}
              </p>
            )}

            {assembly.blocks.map((block) => (
              <div key={block.finding_id + block.ordinal}>
                <RecordBlock
                  block={block}
                  lead={leadOrdinals.has(block.ordinal)}
                  refused={blockRefused(arms, block.ordinal)}
                />
                {(anchored.get(block.ordinal) ?? []).map((t, i) => (
                  <TensionNote key={i} tension={t} />
                ))}
              </div>
            ))}

            {orphans.map((t, i) => (
              <TensionNote key={`orphan-${i}`} tension={t} />
            ))}

            {assembly.tension_checked && assembly.tension_checked.pairs_found === 0 && (
              <TensionNegative
                pairsExamined={assembly.tension_checked.pairs_examined}
                scope={assembly.tension_checked.scope}
                scopeNote={assembly.tension_checked.scope_note}
                blocks={assembly.blocks.length}
              />
            )}

            <CoverageLine assembly={assembly} />
          </section>

          <DropLedger assembly={assembly} />

          {/* No channel at this tier ⇒ no band. A region or thematic read is not
              a record whose voice is missing; it is a record that has none, and
              an "absent" band would invite the operator to wait for one. */}
          {channelSpec &&
            (assessment && assessmentRow ? (
              <AssessmentBand
                body={assessmentRow.body ?? ''}
                title={assessmentRow.title ?? null}
                payload={assessment}
                channelLabel={channelSpec.label}
                spineOrdinals={assembly.blocks.map((b) => b.ordinal)}
                spineMismatch={assessment.spine_id !== active.id}
              />
            ) : (
              <AssessmentAbsent
                reason={
                  channel.isLoading
                    ? 'Loading the interpretive channel…'
                    : channelSpec.absentReason
                }
              />
            ))}
        </div>
      )}

      <RunHistory runs={runs.slice(1)} activeId={active.id} onSelect={setPinnedRunId} />
    </article>
  )
}
