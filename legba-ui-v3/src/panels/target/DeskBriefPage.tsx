/**
 * Desk Brief (`target.desk_brief_page`) — the desk brief READ, not downloaded.
 *
 * `POST /api/v1/v3/export` has composed a desk brief since 7b-iii and
 * `lib/printDocument.ts` has laid it out for paper since k1. Both hand the
 * reader a FILE. This panel renders the same composed document as a page:
 * one scrollable surface, organised by the desk's bounded units, with the
 * markdown, print, JSON and PNG actions on the thing being read rather than
 * on a basket errand two panels away.
 *
 * ## What it renders, in the order the reader asks
 *
 *  1. **The index card** — the composition record that quotes this desk, with
 *     its fenced country voice beside it in its own band, the scale/method
 *     stamp, the as-of, its faithfulness and — never pooled with it — its
 *     correctness badge with the coverage that badge is over.
 *  2. **The units**, in the composition's own declared order: each unit's
 *     latest admitted read, cited through the workstation's one prose
 *     renderer (so every marker keeps its source tag and its fold chips),
 *     its two badges side by side, and the temporal layer where a historical
 *     series is cited — valid time, record time and the producer's tense
 *     marker verbatim.
 *  3. **The evidence table** over the desk's unit ROSTER — one row per
 *     bounded unit whether the composition carried it or not, with an
 *     evidence state drawn from our own vocabularies and the typed absences
 *     that touch it, beside the desk's declared source-layer aperture.
 *  4. **The endnotes**, with each citation's masthead and the route's own
 *     date word (`published` / `fetched`) kept verbatim.
 *  5. **Reading limits** and a GENERATED *what this page does not publish*.
 *
 * ## Server surface: none added
 *
 * Every read is a route the workstation already calls. The page composes the
 * document through `POST /v3/export` with `format:'json'` — the SAME call the
 * Report Export panel and the Target Overview action make — so the page, the
 * markdown file and the printed PDF cannot tell three stories about one desk.
 * `state/exportBasket.readDeskBrief` supplies the basket contents WITHOUT
 * writing the operator's basket: a page that collected on their behalf every
 * time it mounted would be collecting without being asked.
 *
 * ## Consult
 *
 * Opening the page sets the ambient scope pin (`lib/consultContext`,
 * `SCOPE_PIN_ORIGIN`) to this desk, and opening a unit block re-pins it to
 * that unit's read. The Consult tile, wherever it is docked, follows — the
 * same session, with its provenance census intact. There is no consult panel
 * bolted onto this one.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Braces, Image as ImageIcon, Newspaper, Printer } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import type { PanelProps } from '@/types'
import {
  apiGet,
  ApiError,
  downloadExportArtifact,
  exportCollection,
  fetchLayerDivergence,
  type ExportArtifact,
  type LayerDivergenceResponse,
} from '@/lib/api'
import { printExportDocument } from '@/lib/printDocument'
import { deskBriefAppendix, readDeskBrief, type DeskBriefRead } from '@/state/exportBasket'
import { projectAssembly, projectAssessment, type ReadFindingRow } from '@/lib/assemblyModel'
import { assessmentChannelFor, pickAssessmentRow } from '@/lib/assessmentChannel'
import AssessmentBand, { AssessmentAbsent } from '@/v4/read/AssessmentBand'
import { readScaleStamp, scaleStampLabel } from '@/lib/scaleStamp'
import { selectRow } from '@/state/selection'
import { useScope } from '@/state/scope'
import { scopeFromRow, scopeFromTarget } from '@/lib/scopeFromReport'
import { GAP_STRIP_UNITS, type GapLatestFinding } from '@/lib/gapStripModel'
import type { CountryScorecard } from '@/lib/evalOps'
import { fetchUnitCorrectness, type UnitCorrectness } from '@/lib/unitCorrectnessModel'
import {
  deriveEvidenceTable,
  exportCitations,
  findItem,
  notPublished,
  parseExportDocument,
  readingLimits,
  type ExportDocument,
} from '@/lib/deskBriefPage'
import {
  Endnotes,
  EvidenceTable,
  IndexCard,
  LayerAperture,
  NoComposition,
  ReadingLimits,
  SectionLabel,
  UnitBlock,
} from './DeskBriefSections'

/** The desk's bounded-unit roster size — the evidence table's denominator. */
const ROSTER_SIZE = GAP_STRIP_UNITS.length
/** Unit id → its display label, for a roster row the composition never named. */
const UNIT_LABEL = new Map(GAP_STRIP_UNITS.map((u) => [u.id, u.label]))

interface FindingsResponse {
  data: ReadFindingRow[]
}

/** The composed document plus the artifact the actions hand to the browser. */
interface ComposedBrief {
  artifact: ExportArtifact
  doc: ExportDocument | null
}

export default function DeskBriefPagePanel({ registration, scope }: PanelProps) {
  const targetId = scope.target_id ?? registration.descriptor_id
  const pageRef = useRef<HTMLDivElement | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  // ── the basket, read-only ────────────────────────────────────────────────
  const briefQ = useQuery<DeskBriefRead>({
    enabled: !!targetId,
    queryKey: ['desk-brief-page-read', targetId],
    queryFn: () => readDeskBrief(targetId),
  })
  const brief = briefQ.data ?? null

  // ── the composed document (the ONE call the file and the paper share) ────
  const itemKey = (brief?.items ?? []).map((i) => `${i.kind}:${i.id}`).join('|')
  const docQ = useQuery<ComposedBrief>({
    enabled: !!brief && (brief.items.length > 0 || brief.appendix !== null),
    queryKey: ['desk-brief-page-doc', targetId, itemKey, brief?.appendix ?? ''],
    queryFn: async () => {
      const artifact = await exportCollection({
        items: (brief?.items ?? []).map((i) => ({ kind: i.kind, id: i.id })),
        format: 'json',
        title: `Desk brief — ${targetId}`,
        appendix: deskBriefAppendix(targetId, brief?.appendix ?? null),
      })
      return { artifact, doc: parseExportDocument(artifact.content) }
    },
  })
  const doc = docQ.data?.doc ?? null

  // ── the judged / graded / declared reads the table needs ─────────────────
  const scorecardQ = useQuery<CountryScorecard[]>({
    enabled: !!targetId,
    queryKey: ['desk-brief-page-scorecard', targetId],
    queryFn: async () => {
      try {
        return await apiGet<CountryScorecard[]>(
          `/v3/eval/country_scorecard?target_id=${encodeURIComponent(targetId)}`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return []
        throw e
      }
    },
  })

  const correctnessQ = useQuery({
    enabled: !!targetId,
    queryKey: ['desk-brief-page-correctness', targetId],
    // Shares the model-level memo with every `UnitCorrectnessBadge` on the
    // page, so nine badges and this table are ONE request.
    queryFn: () => fetchUnitCorrectness(targetId),
  })

  const layersQ = useQuery<LayerDivergenceResponse>({
    enabled: !!targetId,
    queryKey: ['desk-brief-page-layers'],
    queryFn: () => fetchLayerDivergence(),
    staleTime: 5 * 60_000,
  })

  // ── the fenced country voice ─────────────────────────────────────────────
  const assembly = useMemo(
    () => (brief?.composition ? projectAssembly(brief.composition) : null),
    [brief?.composition],
  )
  const channelSpec = useMemo(
    () => assessmentChannelFor(brief?.composition ?? null, assembly),
    [brief?.composition, assembly],
  )
  const voiceQ = useQuery<FindingsResponse>({
    enabled: !!channelSpec && !!targetId,
    queryKey: ['desk-brief-page-voice', targetId, channelSpec?.analystId ?? ''],
    queryFn: () =>
      apiGet<FindingsResponse>(
        `/findings?analyst_id=${encodeURIComponent(channelSpec!.analystId)}` +
          `&target_id=${encodeURIComponent(targetId)}&limit=5`,
      ),
  })
  const voiceRow = useMemo(
    () => pickAssessmentRow(voiceQ.data?.data, channelSpec, brief?.composition ?? null),
    [voiceQ.data, channelSpec, brief?.composition],
  )
  const voice = useMemo(() => (voiceRow ? projectAssessment(voiceRow) : null), [voiceRow])

  // ── CONSULT: the ambient scope pin follows the page ──────────────────────
  useEffect(() => {
    if (!targetId) return
    useScope.getState().setScope(scopeFromTarget(targetId, targetId, 'desk-brief-page'))
  }, [targetId])

  /** Opening a unit block re-pins the scope to THAT read and focuses it. */
  const openUnit = useCallback((row: ReadFindingRow, label: string) => {
    useScope.getState().setScope(scopeFromRow(row, 'desk-brief-page'))
    selectRow('finding', row.id, label, { origin: 'desk-brief-page' })
  }, [])

  // ── actions ──────────────────────────────────────────────────────────────
  const runAction = useCallback(
    async (name: string, fn: () => Promise<void> | void) => {
      setActionError(null)
      setBusy(name)
      try {
        await fn()
      } catch (e) {
        setActionError(`${name} failed: ${e instanceof Error ? e.message : String(e)}`)
      } finally {
        setBusy(null)
      }
    },
    [],
  )

  const downloadMarkdown = useCallback(
    () =>
      runAction('markdown', async () => {
        const artifact = await exportCollection({
          items: (brief?.items ?? []).map((i) => ({ kind: i.kind, id: i.id })),
          format: 'markdown',
          title: `Desk brief — ${targetId}`,
          appendix: deskBriefAppendix(targetId, brief?.appendix ?? null),
        })
        downloadExportArtifact(artifact)
      }),
    [runAction, brief, targetId],
  )

  const printBrief = useCallback(
    () =>
      runAction('print', async () => {
        // The print path stays the document of record: it prints the MARKDOWN
        // the route composes, through the same dedicated print document the
        // Report Export panel uses — never this page's DOM.
        const artifact = await exportCollection({
          items: (brief?.items ?? []).map((i) => ({ kind: i.kind, id: i.id })),
          format: 'markdown',
          title: `Desk brief — ${targetId}`,
          appendix: deskBriefAppendix(targetId, brief?.appendix ?? null),
        })
        const opened = printExportDocument({
          markdown: artifact.content,
          filename: artifact.filename,
          printedAt: new Date().toISOString(),
        })
        if (!opened) {
          throw new Error(
            'the browser refused a print frame — use the markdown action, which ' +
              'carries the same document',
          )
        }
      }),
    [runAction, brief, targetId],
  )

  const downloadJson = useCallback(
    () =>
      runAction('JSON', () => {
        const artifact = docQ.data?.artifact
        if (!artifact) throw new Error('the document has not been composed yet')
        downloadExportArtifact(artifact)
      }),
    [runAction, docQ.data],
  )

  const savePng = useCallback(
    () =>
      runAction('PNG', async () => {
        const node = pageRef.current
        if (!node) throw new Error('the page has nothing rendered to capture')
        const { toPng } = await import('html-to-image')
        const dataUrl = await toPng(node, {
          // The page's own background, so a capture is never a transparent
          // sheet of text the viewer paints their own ground behind.
          backgroundColor: getComputedStyle(node).backgroundColor || undefined,
          pixelRatio: 2,
        })
        const a = document.createElement('a')
        a.href = dataUrl
        a.download = `legba-desk-brief-${targetId}-${new Date().toISOString().slice(0, 10)}.png`
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
      }),
    [runAction, targetId],
  )

  // ── derivations ──────────────────────────────────────────────────────────
  const dimensions = scorecardQ.data?.[0]?.dimensions
  const correctnessPage = correctnessQ.data ?? null

  const correctnessByUnit = useMemo(() => {
    const m = new Map<string, UnitCorrectness>()
    for (const row of correctnessPage?.data ?? []) m.set(row.analyst_id, row)
    return m
  }, [correctnessPage])

  /** The unit items of the document, in the composition's declared order. */
  const unitItems = useMemo(() => {
    if (!doc || !brief) return []
    return brief.items
      .filter((i) => i.id !== brief.compositionId)
      .map((i) => findItem(doc, i.id))
      .filter((i): i is NonNullable<typeof i> => i !== null && !i.error)
  }, [doc, brief])

  const compositionItem = findItem(doc, brief?.compositionId ?? null)

  const questions = useMemo(() => {
    const m = new Map<string, string>()
    for (const b of assembly?.blocks ?? []) if (b.question) m.set(b.desk, b.question)
    return m
  }, [assembly])

  const latestByUnit = useMemo(() => {
    const m = new Map<string, GapLatestFinding>()
    for (const item of unitItems) {
      const unit = item.analyst_id
      if (!unit || !item.produced_at) continue
      m.set(unit, { id: item.id, producedAt: item.produced_at, severity: item.severity })
    }
    return m
  }, [unitItems])

  const evidenceRows = useMemo(
    () =>
      deriveEvidenceTable({
        coverage: assembly?.coverage ?? [],
        questions,
        latestByUnit,
        dimensions,
        correctness: correctnessByUnit,
        absences: doc?.absences,
      }),
    [assembly, questions, latestByUnit, dimensions, correctnessByUnit, doc],
  )

  const ungraded = useMemo(
    () =>
      correctnessPage?.reference?.state === 'current'
        ? GAP_STRIP_UNITS.filter((u) => !correctnessByUnit.has(u.id)).map((u) => u.id)
        : [],
    [correctnessPage, correctnessByUnit],
  )

  const notPublishedLines = useMemo(
    () =>
      notPublished({
        doc,
        missingUnits: brief?.missingUnits ?? [],
        referenceState: correctnessPage?.reference?.state ?? null,
        ungradedUnits: ungraded,
      }),
    [doc, brief, correctnessPage, ungraded],
  )

  const stamp = useMemo(() => readScaleStamp(brief?.composition), [brief?.composition])

  const limits = useMemo(
    () =>
      readingLimits({
        doc,
        compositionAsOf: assembly?.as_of ?? brief?.composition?.produced_at ?? null,
        scaleLine: scaleStampLabel(stamp),
        scorecardAt: scorecardQ.data?.[0]?.produced_at ?? null,
        reference: correctnessPage?.reference ?? null,
      }),
    [doc, assembly, brief, stamp, scorecardQ.data, correctnessPage],
  )

  const endnoteGroups = useMemo(() => {
    const groups: { heading: string; citations: NonNullable<ExportDocument['items'][number]['citations']> }[] = []
    for (const item of doc?.items ?? []) {
      if (item.error) continue
      groups.push({
        heading: item.title?.trim() || item.analyst_id || item.id,
        citations: item.citations ?? [],
      })
    }
    return groups
  }, [doc])

  const layerDesk = useMemo(
    () => layersQ.data?.desks?.find((d) => d.target_id === targetId) ?? null,
    [layersQ.data, targetId],
  )
  const layerState: 'ready' | 'loading' | 'unavailable' | 'undeclared' = layersQ.isLoading
    ? 'loading'
    : layersQ.isError || layersQ.data?.measured === false
      ? 'unavailable'
      : layerDesk
        ? 'ready'
        : 'undeclared'

  const actions = (
    <div className="flex items-center gap-1.5">
      {actionError && (
        <span className="text-accent-critical" data-testid="desk-brief-page-error">
          {actionError}
        </span>
      )}
      <ActionButton
        icon={<Newspaper className="h-3 w-3" aria-hidden />}
        label={busy === 'markdown' ? 'composing…' : 'markdown'}
        onClick={downloadMarkdown}
        disabled={!brief || busy !== null}
        title="Download the composed brief as one markdown file"
        testId="desk-brief-page-markdown"
      />
      <ActionButton
        icon={<Printer className="h-3 w-3" aria-hidden />}
        label={busy === 'print' ? 'composing…' : 'print / PDF'}
        onClick={printBrief}
        disabled={!brief || busy !== null}
        title="Print the composed markdown through the dedicated print document — the document of record"
        testId="desk-brief-page-print"
      />
      <ActionButton
        icon={<Braces className="h-3 w-3" aria-hidden />}
        label="JSON"
        onClick={downloadJson}
        disabled={!docQ.data || busy !== null}
        title="Download the structured export document exactly as the route served it"
        testId="desk-brief-page-json"
      />
      <ActionButton
        icon={<ImageIcon className="h-3 w-3" aria-hidden />}
        label={busy === 'PNG' ? 'capturing…' : 'PNG'}
        onClick={savePng}
        disabled={busy !== null}
        title="Capture this page as a PNG. The print path stays the document of record."
        testId="desk-brief-page-png"
      />
    </div>
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={`desk ${targetId}`}
      actions={actions}
      onRefresh={() => {
        briefQ.refetch()
        docQ.refetch()
        scorecardQ.refetch()
      }}
    >
      {/* The scroll container is the tile; the PNG ref sits on the CONTENT, so a
          capture carries the whole page rather than the visible box. */}
      <div className="flex-1 overflow-auto bg-surf-2">
        <div className="px-4 py-4" ref={pageRef}>
        {briefQ.isLoading && (
          <p className="text-xs text-ink-3" data-testid="desk-brief-page-loading">
            reading this desk&rsquo;s composition…
          </p>
        )}
        {briefQ.isError && (
          <p className="text-xs text-accent-critical" data-testid="desk-brief-page-read-error">
            Could not read the desk: {String(briefQ.error)}
          </p>
        )}

        {brief && !brief.compositionId && <NoComposition targetId={targetId} />}

        {brief && (
          <>
            <IndexCard
              targetId={targetId}
              composition={compositionItem}
              stamp={stamp}
              compositionAsOf={assembly?.as_of ?? brief.composition?.produced_at ?? null}
              unitsCarried={unitItems.length}
              unitsRoster={ROSTER_SIZE}
              absences={doc?.absences ?? null}
            />

            {channelSpec &&
              (voice && voiceRow ? (
                <AssessmentBand
                  body={voiceRow.body ?? ''}
                  title={voiceRow.title ?? null}
                  payload={voice}
                  channelLabel={channelSpec.label}
                  spineOrdinals={(assembly?.blocks ?? []).map((b) => b.ordinal)}
                  spineMismatch={voice.spine_id !== brief.composition?.id}
                />
              ) : (
                <AssessmentAbsent reason={channelSpec.absentReason} />
              ))}

            <section className="mb-8">
              <SectionLabel
                testId="desk-brief-units-label"
                tip="The composition's own declared order — the coverage register it was built over, not a ranking this page invented."
              >
                the units, in the composition&rsquo;s declared order
              </SectionLabel>
              {docQ.isLoading && (
                <p className="text-xs text-ink-3" data-testid="desk-brief-page-doc-loading">
                  composing the document…
                </p>
              )}
              {docQ.isError && (
                <p className="text-xs text-accent-critical" data-testid="desk-brief-page-doc-error">
                  Could not compose the document: {String(docQ.error)}
                </p>
              )}
              {doc && unitItems.length === 0 && (
                <p className="text-xs text-ink-3" data-testid="desk-brief-units-empty">
                  The composition carried no admitted unit read. Every unit on the roster is
                  accounted for in the evidence table below.
                </p>
              )}
              {unitItems.map((item) => (
                <UnitBlock
                  key={item.id}
                  targetId={targetId}
                  item={item}
                  citations={exportCitations(item)}
                  label={
                    UNIT_LABEL.get(item.analyst_id ?? '') ??
                    item.analyst_id ??
                    item.title ??
                    item.id
                  }
                  question={questions.get(item.analyst_id ?? '') ?? null}
                  situations={
                    // The desk's open situations ride the brief, not the unit;
                    // they are shown once, under the first block, rather than
                    // repeated as if each unit had its own register.
                    item.id === unitItems[0]?.id ? brief.situations : []
                  }
                  onOpen={() =>
                    openUnit(
                      {
                        id: item.id,
                        title: item.title ?? null,
                        analyst_id: item.analyst_id ?? null,
                        target_id: item.target_id ?? null,
                        produced_at: item.produced_at ?? '',
                      },
                      UNIT_LABEL.get(item.analyst_id ?? '') ?? item.id,
                    )
                  }
                />
              ))}
            </section>

            <EvidenceTable
              rows={evidenceRows}
              dimensions={dimensions}
              layers={
                <LayerAperture
                  state={layerState}
                  aperture={layerDesk?.aperture ?? null}
                  mapVersion={layerDesk?.map_version ?? null}
                />
              }
            />

            <Endnotes groups={endnoteGroups} />

            <ReadingLimits
              limits={limits}
              notPublishedLines={notPublishedLines}
              provenanceNote={doc?.provenance_note ?? null}
            />
          </>
        )}
        </div>
      </div>
    </PanelChrome>
  )
}

function ActionButton({
  icon,
  label,
  onClick,
  disabled,
  title,
  testId,
}: {
  icon: React.ReactNode
  label: string
  onClick: () => void
  disabled: boolean
  title: string
  testId: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="flex items-center gap-1 rounded border border-line bg-surf-1 px-2 py-0.5 text-xs text-ink-2 hover:bg-surf-3 disabled:opacity-40"
      title={title}
      data-testid={testId}
    >
      {icon}
      {label}
    </button>
  )
}
