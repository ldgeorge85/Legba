/**
 * CitedAssessment — the per-country read surface (P1-T3).
 *
 * Renders a finding's report as a CITED card: the prose (with its inline `[N]`
 * markers) up top, an evidence panel below. Each `[N]` that resolves to a known
 * citation becomes a clickable chip; clicking it scrolls to + highlights that
 * citation's evidence row (title + source + verify label). The evidence row's
 * title is itself a `RecordLink` to the cited signal, so the operator can drill
 * into the evidence.
 *
 * DEGRADE / HONESTY (built first): a legacy / uncited finding has no citations.
 * Then the prose renders plainly (the existing dark-theme markdown map) under an
 * explicit "uncited" marker — NO fabricated anchors, NO empty evidence panel.
 *
 * Verification: the faithfulness-verify block is a sibling field of `/findings`
 * (joined from the critique row) and is NOT carried on the lineage `root.body`
 * the Inspector reads, so it is usually absent here. We read it DEFENSIVELY and
 * show a per-finding verify label only when present; otherwise an honest
 * "unverified" label — never a fabricated score.
 */
import { Children, isValidElement, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ShieldCheck, ShieldAlert, FileText } from 'lucide-react'
import { MD_COMPONENTS } from '@/v4/why/WorldAssessment'
import { RecordLink } from '@/components/inspector/RecordLink'
import { UnitEvalBadge } from '@/components/inspector/UnitEvalBadge'
import {
  type Citation,
  citationsByMarker,
  evidenceAnchorId,
  normalizeCitationMarkers,
  splitProse,
} from '@/lib/citationsModel'

/** Scroll a citation's evidence row into view + flash it. Pure DOM, guarded for
 *  the no-element case (SSR / test) so it never throws. */
function scrollToEvidence(refId: string): void {
  const el = typeof document !== 'undefined' ? document.getElementById(evidenceAnchorId(refId)) : null
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  el.setAttribute('data-flash', 'true')
  window.setTimeout(() => el.removeAttribute('data-flash'), 1200)
}

/** One inline citation chip — the `[N]` superscript pill. */
function CitationChip({ citation }: { citation: Citation }) {
  return (
    <button
      type="button"
      onClick={() => scrollToEvidence(citation.refId)}
      title={citation.title ?? citation.source ?? `evidence ${citation.marker}`}
      data-testid="citation-chip"
      data-marker={citation.marker}
      className="mx-0.5 inline-flex items-center rounded bg-surf-3 px-1 align-super text-[10px] font-medium leading-none text-accent-info hover:bg-surf-1 hover:underline"
    >
      {citation.marker}
    </button>
  )
}

/**
 * Recursively transform a markdown leaf's children: every string run is split
 * on its `[N]` markers, and each resolved marker becomes a {@link CitationChip}.
 * Non-string nodes (nested <strong>/<em>/<a>…) recurse so markers inside them
 * are linked too. Unknown markers stay literal text (no fabricated chip).
 */
function linkChildren(children: ReactNode, byMarker: Map<string, Citation>): ReactNode {
  return Children.map(children, (child, i) => {
    if (typeof child === 'string') {
      const tokens = splitProse(child, byMarker)
      if (tokens.length === 1 && tokens[0].kind === 'text') return child
      return tokens.map((tok, j) =>
        tok.kind === 'marker' ? (
          <CitationChip key={`c-${i}-${j}`} citation={tok.citation} />
        ) : (
          <span key={`t-${i}-${j}`}>{tok.text}</span>
        ),
      )
    }
    if (isValidElement(child)) {
      const props = child.props as { children?: ReactNode }
      if (props && props.children != null) {
        return { ...child, props: { ...props, children: linkChildren(props.children, byMarker) } }
      }
    }
    return child
  })
}

/** Wrap the shared dark-theme markdown map so every text-bearing element links
 *  its `[N]` markers. Only the leaf renderers that hold prose are wrapped. */
function citedComponents(byMarker: Map<string, Citation>): Components {
  const base = MD_COMPONENTS
  const wrap = (key: keyof Components) => {
    const Original = base[key] as ((p: { children?: ReactNode }) => ReactNode) | undefined
    return (props: { children?: ReactNode }) => {
      const linked = linkChildren(props.children, byMarker)
      return Original ? Original({ ...props, children: linked }) : <>{linked}</>
    }
  }
  return {
    ...base,
    p: wrap('p'),
    li: wrap('li'),
    strong: wrap('strong'),
    em: wrap('em'),
    h1: wrap('h1'),
    h2: wrap('h2'),
    h3: wrap('h3'),
    blockquote: wrap('blockquote'),
  } as Components
}

/** A small label: the finding-level verify status, read defensively. */
function VerifyLabel({ verification }: { verification: Record<string, unknown> | null }) {
  if (!verification || typeof verification !== 'object') {
    return (
      <span
        className="inline-flex items-center gap-1 text-label text-ink-3"
        title="No faithfulness-verify block on this read"
      >
        <ShieldAlert className="h-3 w-3" aria-hidden />
        unverified
      </span>
    )
  }
  const score = verification['faithfulness_score']
  const status = verification['judge_status']
  const scoreTxt = typeof score === 'number' ? ` ${Math.round(score * 100)}%` : ''
  const statusTxt = typeof status === 'string' ? status : 'verified'
  return (
    <span
      className="inline-flex items-center gap-1 text-label text-accent-info"
      title="Faithfulness verify pass result for this finding"
    >
      <ShieldCheck className="h-3 w-3" aria-hidden />
      {statusTxt}
      {scoreTxt}
    </span>
  )
}

export interface CitedAssessmentProps {
  /** The report prose (carries inline `[N]` markers when cited). */
  text: string
  /** The citation list extracted from the merged finding body. */
  citations: Citation[]
  /** The finding-level faithfulness-verify block, when present (else null). */
  verification?: Record<string, unknown> | null
  /** The finding's analyst id — keys the per-unit eval badge (P2-T6). A
   *  non-bounded-unit id simply renders no badge. */
  analystId?: string | null
}

/**
 * The cited assessment card. Renders the cited path when `citations` is
 * non-empty, otherwise the honest uncited path (prose only + an "uncited"
 * marker). Built so the uncited path renders on real data with zero citation
 * machinery in the way.
 */
export default function CitedAssessment({ text, citations, verification = null, analystId = null }: CitedAssessmentProps) {
  // Normalize any full-width 【N】/［N］ ordinal brackets to ASCII [N] so the
  // ASCII-only marker parser resolves them to chips instead of dropping them to
  // literal text (core-plane models emit the variant brackets non-deterministically).
  const prose = normalizeCitationMarkers(text)
  const cited = citations.length > 0
  const byMarker = cited ? citationsByMarker(citations) : new Map<string, Citation>()
  const components = cited ? citedComponents(byMarker) : MD_COMPONENTS

  return (
    <div data-testid="cited-assessment">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {cited ? (
          <span className="inline-flex items-center gap-1 text-label text-ink-2" data-testid="cited-marker">
            <FileText className="h-3 w-3" aria-hidden />
            {citations.length} citation{citations.length === 1 ? '' : 's'}
          </span>
        ) : (
          <span
            className="inline-flex items-center gap-1 rounded bg-surf-1 px-1.5 py-0.5 text-label text-ink-3"
            data-testid="uncited-marker"
            title="This finding predates citations — its prose has no linked evidence"
          >
            <ShieldAlert className="h-3 w-3" aria-hidden />
            uncited (legacy finding)
          </span>
        )}
        <VerifyLabel verification={verification} />
        <UnitEvalBadge analystId={analystId} />
      </div>

      {/* The report prose. Cited path links `[N]`; uncited path renders plainly. */}
      <div className="text-body text-ink-1" data-testid="cited-prose">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
          {prose}
        </ReactMarkdown>
      </div>

      {/* Evidence panel — one row per citation, an anchor a chip scrolls to. */}
      {cited && (
        <div className="mt-3" data-testid="evidence-panel">
          <div className="mb-1 text-label font-semibold uppercase tracking-wider text-ink-2">
            Evidence
          </div>
          <ul className="space-y-1">
            {citations.map((c) => (
              <li
                key={`${c.marker}:${c.refId}`}
                id={evidenceAnchorId(c.refId)}
                data-testid="evidence-row"
                className="flex items-baseline gap-2 rounded px-1 py-0.5 text-body transition-colors data-[flash=true]:bg-surf-1"
              >
                <span className="mt-px shrink-0 font-mono text-label text-accent-info">{c.marker}</span>
                <div className="min-w-0 flex-1">
                  <RecordLink
                    kind={c.refKind}
                    id={c.refId}
                    label={c.title ?? c.refId}
                    origin="cited-assessment"
                    showKind
                  />
                  {c.source && (
                    <a
                      href={c.source}
                      target="_blank"
                      rel="noreferrer"
                      className="mt-0.5 block truncate text-label text-ink-3 hover:text-accent-info hover:underline"
                      title={c.source}
                    >
                      {c.source}
                    </a>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
