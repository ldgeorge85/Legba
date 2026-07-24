/**
 * The Inspector (redesign Move 1 — the keystone).
 *
 * A single, persistent, docked-right Dockview tile driven entirely by the
 * unified selection store. Clicking any row / map dot / graph node / id anywhere
 * loads that record's full detail here, with every referenced id rendered as a
 * `RecordLink` (Move 4) — so the next selection is one click away, with a
 * breadcrumb behind you (drill-through).
 *
 * It is built atop the existing `PanelChrome` + `DescriptorView`; it is not a
 * new rendering stack. It reuses the Why provenance trail (which itself reuses
 * GET /lineage/{kind}/{id}). When nothing is selected it shows the world
 * assessment one-pager (parity with Why's empty state) — never dead space.
 */
import { ChevronRight, ChevronLeft } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { DescriptorView } from '@/components/DescriptorView'
import { RecordLink, refKindForField } from '@/components/inspector/RecordLink'
import { useInspectorDetail, type InspectorDetail, type Ref } from './useInspectorDetail'
import { useSelection, type Selection, type SelectionPreview } from '@/state/selection'
import ProvenanceTrail from '@/v4/why/ProvenanceTrail'
import CitedAssessment from '@/components/inspector/CitedAssessment'
import { extractCitations } from '@/lib/citationsModel'
import { unwrapEnvelope } from '@/lib/proseText'
import type { PanelProps } from '@/types'

/** Keys floated to the top of the BODY DescriptorView. */
const BODY_PRIMARY = ['title', 'summary', 'severity', 'confidence', 'status', 'produced_at'] as const

/** Payload keys that hold the actual written report, in preference order. The
 *  rendered keys are dropped from the metadata DescriptorView below so the
 *  report isn't shown twice (once rendered, once as a raw collapsed string).
 *  `distilled_body` is FIRST: for a signal it's OUR analysis-tuned summary
 *  (markdown BLUF + quoted claims) written by signal_summarizer — feature it as
 *  the report instead of the publisher's `summary` teaser. Non-signal kinds have
 *  no distilled_body, so they fall through to `summary`/`body` exactly as before. */
const REPORT_KEYS = ['distilled_body', 'summary', 'body', 'assessment', 'narrative', 'text'] as const

/** Extract the report markdown + which key it came from, from the merged body. */
function pickReport(body: Record<string, unknown>): { key: string; text: string } | null {
  for (const k of REPORT_KEYS) {
    const v = body[k]
    if (typeof v === 'string' && v.trim() !== '') return { key: k, text: v }
  }
  return null
}

export default function InspectorPanel({ registration }: PanelProps) {
  const selection = useSelection((s) => s.selection)
  const history = useSelection((s) => s.history)
  const back = useSelection((s) => s.back)
  const { detail, refetch } = useInspectorDetail(selection)

  // Empty state — a call-to-action (#90: the world assessment is a FINDING; it
  // shows here when selected, like any finding — no special-cased panel/teaser).
  if (!selection) {
    return (
      <PanelChrome registration={registration} title="Inspector" subtitle="select anything to inspect">
        <div className="flex h-full flex-col items-center justify-center gap-2 px-6 py-10 text-center" data-testid="inspector-empty">
          <div className="text-body font-medium text-ink-2">Nothing selected</div>
          <div className="max-w-xs text-label text-ink-3">
            Click a finding, signal, entity, target, or any id — its full detail,
            report, and provenance trail load here.
          </div>
        </div>
      </PanelChrome>
    )
  }

  const title = detail?.label ?? selection.label ?? selection.id
  const subtitle = `${selection.kind}${selection.origin ? ` · from ${selection.origin}` : ''}`

  return (
    <PanelChrome
      registration={registration}
      title={title}
      subtitle={subtitle}
      onRefresh={() => refetch()}
    >
      <div className="space-y-density" data-testid="inspector-body">
        <Breadcrumb history={history} selection={selection} onBack={back} />

        <Header kind={selection.kind} id={selection.id} />

        {detail ? (
          <DetailView detail={detail} />
        ) : selection.preview?.body ? (
          // #4 — paint the prose we already have (the clicked feed row's body)
          // immediately; the full detail (citations, refs, provenance) hydrates
          // per-section behind this. No 9s blank skeleton for the report.
          <OptimisticReport preview={selection.preview} />
        ) : (
          <DetailSkeleton />
        )}

        {/* Provenance trail — reused Why fetch (lineage walk). */}
        <Section label="Provenance trail">
          <ProvenanceTrail selection={selection} />
        </Section>
      </div>
    </PanelChrome>
  )
}

function Header({ kind, id }: { kind: Selection['kind']; id: string }) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-line pb-2">
      <span className="rounded bg-surf-1 px-1.5 py-0.5 text-label uppercase tracking-wide text-ink-2">
        {kind}
      </span>
      <code className="truncate font-mono text-label text-ink-3" title={id}>
        {id}
      </code>
    </div>
  )
}

function Breadcrumb({
  history,
  selection,
  onBack,
}: {
  history: Selection[]
  selection: Selection
  onBack: () => void
}) {
  if (history.length === 0) return null
  // Show the last few hops + the current selection.
  const trail = history.slice(-3)
  return (
    <div className="flex flex-wrap items-center gap-1 text-label text-ink-3" data-testid="inspector-breadcrumb">
      <button
        type="button"
        onClick={onBack}
        className="mr-1 inline-flex items-center gap-0.5 rounded border border-line px-1 py-0.5 text-ink-2 hover:text-ink-1"
        title="Back to previous selection"
        data-testid="inspector-back"
      >
        <ChevronLeft className="h-3 w-3" aria-hidden />
        back
      </button>
      {trail.map((h, i) => (
        <span key={`${h.kind}:${h.id}:${i}`} className="inline-flex items-center gap-1">
          <RecordLink kind={h.kind} id={h.id} label={h.label ?? h.id} origin="breadcrumb" className="text-label" />
          <ChevronRight className="h-3 w-3 text-ink-3" aria-hidden />
        </span>
      ))}
      <span className="truncate text-ink-1">{selection.label ?? selection.id}</span>
    </div>
  )
}

function DetailView({ detail }: { detail: InspectorDetail }) {
  const report = pickReport(detail.body)
  // P1-T3: pull the finding's citation list out of the merged body
  // (`body.data.citations`). Empty for a legacy / uncited finding — the card
  // then renders the prose plainly with an honest "uncited" marker.
  const citations = detail.kind === 'finding' ? extractCitations(detail.body) : []
  // The faithfulness-verify block, read defensively — usually absent on the
  // lineage read path, present only if the merged body carries it.
  const verification =
    detail.body.verification && typeof detail.body.verification === 'object'
      ? (detail.body.verification as Record<string, unknown>)
      : null
  // The report renders as markdown in its own section — drop it from the raw
  // metadata view so the long text isn't also shown collapsed into one line.
  const metaBody = report
    ? Object.fromEntries(Object.entries(detail.body).filter(([k]) => k !== report.key))
    : detail.body
  return (
    <>
      {/* The actual written report FIRST (the operator reached this finding to
          READ it) — a CITED card: each `[N]` chip scrolls to its evidence row.
          The card handles BOTH the cited and the honest uncited path. */}
      {report && (
        <Section label="Report">
          <div data-testid="inspector-report">
            <CitedAssessment
              text={report.text}
              citations={citations}
              verification={verification}
              confidence={typeof detail.body.confidence === 'number' ? detail.body.confidence : null}
              analystId={typeof detail.body.analyst_id === 'string' ? detail.body.analyst_id : null}
            />
          </div>
        </Section>
      )}

      {Object.keys(detail.core).length > 0 && (
        <Section label="Core">
          <CoreFields core={detail.core} />
        </Section>
      )}

      <Section label="Body">
        <DescriptorView body={metaBody} primaryKeys={BODY_PRIMARY} />
      </Section>

      {detail.refs.length > 0 && (
        <Section label="Derived from / linked">
          <ul className="space-y-1" data-testid="inspector-refs">
            {detail.refs.map((r, i) => (
              <RefRow key={`${r.kind}:${r.id}:${i}`} refItem={r} />
            ))}
          </ul>
        </Section>
      )}

      {detail.related.length > 0 && (
        <Section label="Related">
          <div className="flex flex-wrap gap-1.5" data-testid="inspector-related">
            {detail.related.map((b, i) => (
              <span
                key={i}
                className="inline-flex items-center gap-1 rounded-full bg-surf-1 px-2 py-0.5 text-label text-ink-1"
              >
                {b.label}
                <span className="rounded bg-surf-3 px-1 tabular-nums text-ink-2">{b.count}</span>
              </span>
            ))}
          </div>
        </Section>
      )}
    </>
  )
}

/** Core fields — render id-shaped values as RecordLinks (Move 4). */
function CoreFields({ core }: { core: Record<string, unknown> }) {
  const entries = Object.entries(core).filter(([, v]) => v != null)
  if (entries.length === 0) return <span className="text-body text-ink-3">—</span>
  return (
    <dl className="space-y-rows">
      {entries.map(([key, value]) => {
        const refKind = refKindForField(key, value)
        return (
          <div key={key} className="flex gap-2 text-body">
            <dt className="min-w-[7rem] shrink-0 font-mono text-ink-2">{key}</dt>
            <dd className="min-w-0 flex-1 break-words text-ink-1">
              {refKind ? (
                <RecordLink kind={refKind} id={String(value)} origin="inspector-core" />
              ) : (
                String(value)
              )}
            </dd>
          </div>
        )
      })}
    </dl>
  )
}

function RefRow({ refItem }: { refItem: Ref }) {
  return (
    <li className="flex items-baseline gap-2 text-body">
      {refItem.relation && (
        <span className="shrink-0 text-label uppercase tracking-wide text-ink-3">
          {refItem.relation}
        </span>
      )}
      <RecordLink
        kind={refItem.kind}
        id={refItem.id}
        label={refItem.label ?? refItem.id}
        origin="inspector-ref"
        showKind
      />
    </li>
  )
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section>
      <div className="mb-1 text-label font-semibold uppercase tracking-wider text-ink-2">
        {label}
      </div>
      {children}
    </section>
  )
}

/**
 * The optimistic first paint (#4): render the prose the caller already had in
 * hand (the clicked feed row's body) as the Report, so the operator reads it in
 * <300ms instead of staring at a skeleton for the full ~9s lineage chain. The
 * body may be a raw `{"title","body"}` JSON envelope, so unwrap it first;
 * citations are empty here (they arrive with the full detail) so the prose
 * renders plainly — an honest, un-fabricated stand-in that swaps to the fully
 * cited card the moment `detail` resolves. Only the report is optimistic; the
 * remaining sections show a slim "hydrating" skeleton.
 */
function OptimisticReport({ preview }: { preview: SelectionPreview }) {
  const text = unwrapEnvelope(preview.body ?? '')
  return (
    <>
      <Section label="Report">
        <div data-testid="inspector-report-optimistic">
          <CitedAssessment text={text} citations={[]} analystId={preview.analystId ?? null} />
        </div>
      </Section>
      <div
        className="flex items-center gap-2 text-label text-ink-3"
        data-testid="inspector-hydrating"
        aria-busy="true"
      >
        <span className="h-2 w-2 animate-pulse rounded-full bg-accent-info" aria-hidden />
        hydrating citations, references &amp; provenance…
      </div>
    </>
  )
}

function DetailSkeleton() {
  return (
    <div className="animate-pulse space-y-2" data-testid="inspector-skeleton" aria-busy="true">
      <div className="h-3 w-1/2 rounded bg-surf-1" />
      <div className="h-3 w-2/3 rounded bg-surf-1" />
      <div className="h-20 w-full rounded bg-surf-1" />
      <div className="h-3 w-1/3 rounded bg-surf-1" />
    </div>
  )
}
