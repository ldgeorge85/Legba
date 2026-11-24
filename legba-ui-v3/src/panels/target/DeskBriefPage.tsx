/**
 * The subject brief — `subject.page`'s Brief tab (UI-B3; was the bound kind
 * `target.desk_brief_page`, which now aliases onto this tab). The desk brief
 * READ, not downloaded, GENERALIZED to a subject of any frame kind: the roster
 * is the subject's OWN (`lib/subjectPage.ts::subjectRoster` — a supply-chain
 * lane carries its lane unit, a regime its own questions, never a country
 * desk's nine), the composition is the newest row any of the profile's
 * composing frames published on it, and every exemplar word on the page is the
 * profile's subject noun.
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
 * The page pins its subject (the bound tile's pin — `App.tsx`, UI-B6 — and
 * `SubjectPage`'s own `usePinScope`), and opening a unit block RE-PINS Consult
 * to that unit's read through the same hook (`usePinScope`, surface
 * `subject brief`) and focuses it. The Consult tile, wherever it is docked,
 * follows — the same session, with its provenance census intact. There is no
 * consult panel bolted onto this one.
 *
 * ## No composition
 *
 * A subject no composing frame has published on (a lane, a regime on a fresh
 * instance) renders the absence line and its OWN roster's latest reads — one
 * `/findings?analyst_id_in=<roster>` read — so the evidence table says what
 * each unit has read, never a country-shaped empty page.
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
import { usePinScope, type PinObject } from '@/lib/consultContext'
import { type GapLatestFinding } from '@/lib/gapStripModel'
import { defaultDeskBriefFetchers } from '@/state/exportBasket'
import { profileUnits, useProjection } from '@/lib/profileUi'
import { useSubjectIndex } from '@/lib/deskNames'
import {
  cadenceFromProjection,
  compositionProducers,
  subjectNoun,
  subjectRoster,
} from '@/lib/subjectPage'
import type { CountryScorecard } from '@/lib/evalOps'
import { fetchUnitCorrectness, type UnitCorrectness } from '@/lib/unitCorrectnessModel'
import { extractCitations } from '@/lib/citationsModel'
import {
  compositionIndex,
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

/** Unit id → its display label (every unit the profile declares — a block the
 *  composition carried for a unit off this subject's roster still reads by name). */
const UNIT_LABEL = new Map(profileUnits().map((u) => [u.id, u.label]))
/** The profile's composing frames' producers — the composition candidates. */
const PRODUCERS = compositionProducers()
/** The profile's subject noun (the exemplar's "desk"). */
const NOUN = subjectNoun()
/** "Desk brief" for the exemplar; the profile's noun anywhere else. */
const BRIEF_TITLE = `${NOUN.charAt(0).toUpperCase()}${NOUN.slice(1)} brief`

/** The newest row any composing frame published on the subject, or null. */
async function fetchSubjectComposition(targetId: string): Promise<ReadFindingRow | null> {
  if (PRODUCERS.length === 0) return null
  const resp = await apiGet<{ data: ReadFindingRow[] }>(
    `/findings?target_id=${encodeURIComponent(targetId)}` +
      `&analyst_id_in=${encodeURIComponent(PRODUCERS.join(','))}&limit=1&fields=full`,
  )
  return resp.data[0] ?? null
}

const SUBJECT_BRIEF_FETCHERS = { ...defaultDeskBriefFetchers, fetchComposition: fetchSubjectComposition }

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

  // ── the subject's OWN roster (UI-B3) ─────────────────────────────────────
  const subjectIndex = useSubjectIndex()
  const unitsProjection = useProjection('units')
  const cadence = useMemo(
    () => (unitsProjection.state === 'ok' ? cadenceFromProjection(unitsProjection.rows) : undefined),
    [unitsProjection],
  )
  const roster = useMemo(
    () => subjectRoster(targetId, { index: subjectIndex, cadence }),
    [targetId, subjectIndex, cadence],
  )
  const rosterIds = roster.units.map((u) => u.id).join(',')

  // ── the basket, read-only ────────────────────────────────────────────────
  const briefQ = useQuery<DeskBriefRead>({
    enabled: !!targetId,
    queryKey: ['desk-brief-page-read', targetId],
    queryFn: () => readDeskBrief(targetId, SUBJECT_BRIEF_FETCHERS),
  })
  const brief = briefQ.data ?? null

  // ── no composition: the roster's own latest reads (one request) ─────────
  const rosterReadsQ = useQuery<FindingsResponse>({
    enabled: !!targetId && !!brief && !brief.compositionId && rosterIds.length > 0,
    queryKey: ['desk-brief-page-roster-reads', targetId, rosterIds],
    queryFn: () =>
      apiGet<FindingsResponse>(
        `/findings?target_id=${encodeURIComponent(targetId)}` +
          `&analyst_id_in=${encodeURIComponent(rosterIds)}&limit=50&fields=summary`,
      ),
  })

  // ── the composed document (the ONE call the file and the paper share) ────
  const itemKey = (brief?.items ?? []).map((i) => `${i.kind}:${i.id}`).join('|')
  const docQ = useQuery<ComposedBrief>({
    enabled: !!brief && (brief.items.length > 0 || brief.appendix !== null),
    queryKey: ['desk-brief-page-doc', targetId, itemKey, brief?.appendix ?? ''],
    queryFn: async () => {
      const artifact = await exportCollection({
        items: (brief?.items ?? []).map((i) => ({ kind: i.kind, id: i.id })),
        format: 'json',
        title: `${BRIEF_TITLE} — ${targetId}`,
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
          `&target_id=${encodeURIComponent(targetId)}&limit=5&fields=full`,
      ),
  })
  const voiceRow = useMemo(
    () => pickAssessmentRow(voiceQ.data?.data, channelSpec, brief?.composition ?? null),
    [voiceQ.data, channelSpec, brief?.composition],
  )
  const voice = useMemo(() => (voiceRow ? projectAssessment(voiceRow) : null), [voiceRow])

  // ── CONSULT: a unit block drill re-pins to THAT read (UI-B6's hook) ──────
  // The subject itself is pinned by the page around this tab; the drill is the
  // newer claim, so Consult follows the read the reader opened. A new subject
  // releases the drill.
  const [drill, setDrill] = useState<{ pin: PinObject | null; rev: number }>({ pin: null, rev: 0 })
  useEffect(() => setDrill((d) => ({ pin: null, rev: d.rev })), [targetId])
  usePinScope(drill.pin, 'subject brief', drill.rev)

  /** Opening a unit block re-pins Consult to THAT read and focuses it. */
  const openUnit = useCallback((row: ReadFindingRow, label: string) => {
    setDrill((d) => ({ pin: { kind: 'finding', id: row.id, label }, rev: d.rev + 1 }))
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
          title: `${BRIEF_TITLE} — ${targetId}`,
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
          title: `${BRIEF_TITLE} — ${targetId}`,
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
        a.download = `legba-${NOUN.replace(/[^a-z0-9]+/g, '-')}-brief-${targetId}-${new Date().toISOString().slice(0, 10)}.png`
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

  /** C2/fix 2 — "units carried" is the composition's OWN carried set (its
   *  assembly's blocks), the one source its headline counts "N verified desk
   *  reads" from. It used to count the export document's resolved unit items,
   *  which is 0 while the document composes — so the card printed "0 of 8"
   *  under a headline saying 3. No composition carries nothing (a read answer,
   *  0); a composition whose assembly cannot be read is unknown ("–"). */
  const unitsCarried: number | null = !brief?.compositionId
    ? 0
    : assembly
      ? assembly.blocks.length
      : null

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
    // No composition on record: each roster unit's own newest read stands in
    // (the route returns newest first; the first row per unit is its latest).
    for (const row of rosterReadsQ.data?.data ?? []) {
      const unit = row.analyst_id
      if (!unit || !row.produced_at || m.has(unit)) continue
      m.set(unit, { id: row.id, producedAt: row.produced_at, severity: row.severity ?? null })
    }
    return m
  }, [unitItems, rosterReadsQ.data])

  /** The question set: the composition's own block questions, else the
   *  profile's standing question for the unit. */
  const questionSet = useMemo(() => {
    const m = new Map<string, string>()
    for (const u of roster.units) if (u.question) m.set(u.id, u.question)
    for (const [k, v] of questions) m.set(k, v)
    return m
  }, [roster, questions])

  const evidenceRows = useMemo(
    () =>
      deriveEvidenceTable({
        units: roster.units,
        coverage: assembly?.coverage ?? [],
        questions: questionSet,
        latestByUnit,
        dimensions,
        correctness: correctnessByUnit,
        absences: doc?.absences,
      }),
    [roster, assembly, questionSet, latestByUnit, dimensions, correctnessByUnit, doc],
  )

  const ungraded = useMemo(
    () =>
      correctnessPage?.reference?.state === 'current'
        ? roster.units.filter((u) => !correctnessByUnit.has(u.id)).map((u) => u.id)
        : [],
    [roster, correctnessPage, correctnessByUnit],
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

  /** X1/D1 — the card's headline and its as-of, off the ONE composition row. */
  const index = useMemo(
    () => compositionIndex(brief?.composition ?? null, assembly?.as_of ?? null, NOUN),
    [brief?.composition, assembly],
  )

  /** X1/D1 — the voice's own citations, so its `[[ref:N]]` render as chips. */
  const voiceCitations = useMemo(
    () =>
      voiceRow?.data && typeof voiceRow.data === 'object'
        ? extractCitations(voiceRow.data as Record<string, unknown>)
        : [],
    [voiceRow],
  )

  const limits = useMemo(
    () =>
      readingLimits({
        doc,
        compositionAsOf: index.asOf,
        scaleLine: scaleStampLabel(stamp),
        scorecardAt: scorecardQ.data?.[0]?.produced_at ?? null,
        reference: correctnessPage?.reference ?? null,
      }),
    [doc, index, stamp, scorecardQ.data, correctnessPage],
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
      subtitle={`${NOUN} ${targetId}`}
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
            reading this {NOUN}&rsquo;s composition…
          </p>
        )}
        {briefQ.isError && (
          <p className="text-xs text-accent-critical" data-testid="desk-brief-page-read-error">
            Could not read the {NOUN}: {String(briefQ.error)}
          </p>
        )}

        {brief && !brief.compositionId && <NoComposition targetId={targetId} producers={PRODUCERS} />}

        {brief && (
          <>
            <IndexCard
              targetId={targetId}
              index={index}
              composition={compositionItem}
              stamp={stamp}
              unitsCarried={unitsCarried}
              unitsRoster={roster.units.length}
              absences={doc?.absences ?? null}
            />

            {/* The voice read can take seconds; "none written" is a claim about
                the record and prints only once that read answered empty. */}
            {channelSpec && voiceQ.isError && (
              <p className="mb-8 text-xs text-accent-critical" data-testid="desk-brief-voice-error">
                Could not read the {channelSpec.label.toLowerCase()}: {String(voiceQ.error)}
              </p>
            )}
            {channelSpec && !voiceQ.isSuccess && !voiceQ.isError && (
              <p className="mb-8 text-xs text-ink-3" data-testid="desk-brief-voice-loading">
                reading the {channelSpec.label.toLowerCase()}…
              </p>
            )}
            {channelSpec &&
              voiceQ.isSuccess &&
              (voice && voiceRow ? (
                <AssessmentBand
                  body={voiceRow.body ?? ''}
                  title={voiceRow.title ?? null}
                  payload={voice}
                  channelLabel={channelSpec.label}
                  spineOrdinals={(assembly?.blocks ?? []).map((b) => b.ordinal)}
                  spineMismatch={voice.spine_id !== brief.composition?.id}
                  citations={voiceCitations}
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
              rosterReason={roster.basis === 'unresolved' || roster.units.length === 0 ? roster.reason : null}
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
