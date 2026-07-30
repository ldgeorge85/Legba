/**
 * CitedProse — the ONE prose renderer for the whole workstation (S7-T3).
 *
 * A single component every prose surface routes through — the Live Feed preview,
 * the World Assessment report, the desk Intelligence Card, the Inspector, and the
 * Journal — so NO surface ever paints raw markdown (`**BLUF:**`, `## …`), a raw
 * `{"title","body"}` JSON envelope, or a literal citation run (`[3][4]`) again.
 *
 *  - Markdown is ALWAYS rendered (react-markdown + the shared dark theme), never
 *    shown as source. A JSON-envelope body is unwrapped to its prose first.
 *  - `[N]` (unit) and `[[ref:N]]` (composition) markers are TOKENIZED into
 *    interactive chips backed by `citationsModel`. Each resolved chip carries a
 *    hover/focus CARD: source · passage · credibility · per-claim verdict, plus a
 *    "trace" action into the evidence / record.
 *  - A marker with NO backing citation renders as an explicit muted "unresolved"
 *    chip — visible + honest, never a fabricated anchor and never literal noise.
 *
 * Two variants:
 *  - `block`  (default) — full markdown block, for the reading columns.
 *  - `inline` — a compact, markdown-stripped single flow for a clamped feed scan
 *    line; markers become tiny tooltip-only chips (a scrolling clamp can't host a
 *    popover), still never literal text.
 */
import { Children, isValidElement, useMemo, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { AlertTriangle } from 'lucide-react'
import { MD_COMPONENTS } from '@/lib/markdownComponents'
import { selectRow } from '@/state/selection'
import { VerdictBadge } from '@/components/VerdictBadge'
import {
  citationLabel,
  citationsByMarker,
  evidenceAnchorId,
  normalizeCitationMarkers,
  tokenizeProse,
  type Citation,
} from '@/lib/citationsModel'
import {
  CLAIM_VERDICT_ABSENCE_EXPLAIN,
  claimVerdictForMarker,
  type ClaimVerdict,
} from '@/lib/claimVerdicts'
import { stripMarkdown, unwrapEnvelope } from '@/lib/proseText'

export interface CitedProseProps {
  /** The prose (markdown; may be a raw `{"title","body"}` JSON envelope). */
  text: string
  /** The citation list extracted from the finding/composition body. */
  citations: Citation[]
  /**
   * The finding-level faithfulness `verification` block (P1-8/P2-4). When
   * present, each chip's hover card derives its per-claim verdict — preferring
   * the persisted `claim_verdicts` LEDGER (every graded claim, supported
   * included) when it names the chip's ordinal, falling back to
   * `unsupported_spans` (flag-only, no positive "supported" record) for
   * critiques written before the ledger existed. See @/lib/claimVerdicts for
   * the full derivation + fallback contract. Absent → the card shows an
   * honest `claim-level verdict not recorded` line, never a fabricated one.
   */
  verification?: Record<string, unknown> | null
  variant?: 'block' | 'inline'
  /**
   * What a chip click does. Defaults to scrolling to the citation's evidence
   * row when an evidence panel exists on the page, else selecting the record.
   * The report/inspector pass their own (scroll-to-evidence).
   */
  onCiteClick?: (citation: Citation) => void
  className?: string
}

/** Default chip action — scroll to the on-page evidence row if present, else
 *  drive the shared selection (open the record in the Inspector). */
function defaultCiteClick(c: Citation): void {
  const anchor =
    typeof document !== 'undefined' ? document.getElementById(evidenceAnchorId(c.refId)) : null
  if (anchor) {
    anchor.scrollIntoView({ behavior: 'smooth', block: 'center' })
    anchor.setAttribute('data-flash', 'true')
    window.setTimeout(() => anchor.removeAttribute('data-flash'), 1200)
    return
  }
  selectRow(c.refKind, c.refId, c.title ?? undefined, { origin: 'cited-prose' })
}

/**
 * The per-claim verify line inside the hover card (P1-8). Renders EXACTLY what
 * the verification block records for this chip's ordinal:
 *  * a flag naming this ordinal → the flag's honesty-vocabulary label + the
 *    flagged claim text (the judge's per-claim subject);
 *  * LLM judge ran, nothing names it → "not flagged" + the pooled context
 *    (per-claim SUPPORTED verdicts are not persisted, so we never claim one);
 *  * floor-only / no verify block → the explicit not-recorded line.
 */
function CardVerdictLine({ verdict }: { verdict: ClaimVerdict }) {
  if (verdict.kind === 'not-recorded' || verdict.kind === 'not-checked') {
    // U-5 — this reads like an error to a first-time analyst ("not recorded")
    // without the plain-language gloss; the card is already a hover-revealed
    // surface, so the gloss renders as always-visible caption text rather
    // than a second nested tooltip.
    return (
      <span data-testid="citation-card-verdict" data-verdict={verdict.kind}>
        <span className="mt-1.5 block text-[10px] italic text-ink-3">{verdict.label}</span>
        <span className="mt-0.5 block text-[10px] leading-snug text-ink-3">
          {CLAIM_VERDICT_ABSENCE_EXPLAIN}
        </span>
      </span>
    )
  }
  if (verdict.kind === 'supported' || verdict.kind === 'not-flagged') {
    // P2-4: 'supported' is the ledger-backed positive verdict for THIS chip;
    // 'not-flagged' is the legacy (pre-ledger) silence — visually distinct
    // (accent-ok vs muted) so the reader can tell a real verdict from an
    // absence of one.
    return (
      <span
        className={`mt-1.5 block text-[10px] ${
          verdict.kind === 'supported' ? 'text-accent-ok' : 'text-ink-2'
        }`}
        data-testid="citation-card-verdict"
        data-verdict={verdict.kind}
      >
        verify judge: {verdict.label}
        {verdict.checkable != null && verdict.supported != null && (
          <span className="text-ink-3">
            {' '}
            · {verdict.supported}/{verdict.checkable} checkable claims supported overall
          </span>
        )}
      </span>
    )
  }
  // contradicted / unsupported / flagged — a recorded per-claim flag.
  const tone = verdict.kind === 'contradicted' ? 'text-accent-critical' : 'text-accent-warning'
  const firstSpan = verdict.spans[0]
  return (
    <span
      className="mt-1.5 block text-[10px]"
      data-testid="citation-card-verdict"
      data-verdict={verdict.kind}
    >
      <span className={`inline-flex items-center gap-1 font-medium ${tone}`}>
        <AlertTriangle className="h-2.5 w-2.5 shrink-0" aria-hidden />
        verify judge: {verdict.label}
      </span>
      {firstSpan && (
        <span className="mt-0.5 block leading-snug text-ink-3">
          claim: “
          {firstSpan.text.length > 160 ? `${firstSpan.text.slice(0, 160)}…` : firstSpan.text}”
          {verdict.spans.length > 1 && ` (+${verdict.spans.length - 1} more flagged)`}
        </span>
      )}
    </span>
  )
}

/** The hover/focus card body for a resolved citation. */
function CitationCard({ c, verdict }: { c: Citation; verdict: ClaimVerdict }) {
  const hasCredibility = typeof c.effectiveConfidence === 'number'
  return (
    <span
      className="pointer-events-none absolute left-0 top-full z-50 mt-1 hidden w-72 rounded-lg border border-line bg-surf-3 p-2.5 text-left align-top shadow-xl group-hover/cite:block group-focus-within/cite:block"
      data-testid="citation-card"
      role="tooltip"
    >
      <span className="mb-1 flex items-center gap-1.5">
        <span className="rounded bg-surf-2 px-1 font-mono text-[10px] text-accent-info">
          {citationLabel(c.marker)}
        </span>
        <span className="text-[10px] uppercase tracking-wide text-ink-3">
          {c.refKind === 'finding' ? 'sub-claim' : 'signal'}
        </span>
      </span>
      {c.title && (
        <span className="block text-[12px] font-medium leading-snug text-ink-1">{c.title}</span>
      )}
      {c.source && (
        <span className="mt-0.5 block truncate text-[10px] text-ink-3" title={c.source}>
          {c.source}
        </span>
      )}
      {c.evidenceText ? (
        <span className="mt-1.5 block max-h-24 overflow-hidden text-[11px] italic leading-snug text-ink-2">
          “{c.evidenceText.length > 240 ? `${c.evidenceText.slice(0, 240)}…` : c.evidenceText}”
        </span>
      ) : (
        <span className="mt-1.5 block text-[10px] italic text-ink-3">
          cited passage not recorded
        </span>
      )}
      {/* P1-8 — the per-claim verify verdict for THIS chip's ordinal. */}
      <CardVerdictLine verdict={verdict} />
      <span className="mt-2 flex items-center justify-between gap-2">
        {hasCredibility ? (
          <VerdictBadge
            input={{ effectiveConfidence: c.effectiveConfidence, citationCount: 1 }}
          />
        ) : (
          <span className="text-[10px] italic text-ink-3">credibility not recorded</span>
        )}
      </span>
    </span>
  )
}

/** One resolved citation chip + its hover/focus card. Hover, keyboard focus,
 *  AND touch (tapping focuses the button → `group-focus-within` shows the
 *  card) all reveal it; the card is absolutely positioned so it never shifts
 *  layout. */
function CitationChip({
  c,
  verdict,
  onClick,
}: {
  c: Citation
  verdict: ClaimVerdict
  onClick: () => void
}) {
  return (
    <span className="group/cite relative inline-block align-baseline">
      <button
        type="button"
        onClick={onClick}
        title={c.title ?? c.source ?? `evidence ${c.marker}`}
        data-testid="citation-chip"
        data-marker={c.marker}
        className="mx-0.5 inline-flex items-center rounded bg-surf-3 px-1 align-super text-[10px] font-medium leading-none text-accent-info hover:bg-surf-1 hover:underline"
      >
        {citationLabel(c.marker)}
      </button>
      <CitationCard c={c} verdict={verdict} />
    </span>
  )
}

/** A dangling marker with no backing citation — shown, not hidden. */
function UnresolvedChip({ marker }: { marker: string }) {
  return (
    <span
      className="mx-0.5 inline-flex items-center gap-0.5 rounded border border-dashed border-amber-700/60 bg-amber-950/20 px-1 align-super text-[10px] leading-none text-amber-300/90"
      title="Unresolved citation — this marker has no backing evidence in the record (shown, never fabricated)"
      data-testid="citation-unresolved"
      data-marker={marker}
    >
      <AlertTriangle className="h-2.5 w-2.5" aria-hidden />
      {marker}
    </span>
  )
}

/**
 * Recursively transform a markdown leaf's children: each string run is tokenized
 * on its citation markers, resolved markers → chips, dangling markers →
 * unresolved chips. Non-string nodes (nested <strong>/<em>/<a>…) recurse.
 */
function linkChildren(
  children: ReactNode,
  byMarker: Map<string, Citation>,
  onCite: (c: Citation) => void,
  verdictFor: (marker: string) => ClaimVerdict,
): ReactNode {
  return Children.map(children, (child, i) => {
    if (typeof child === 'string') {
      const tokens = tokenizeProse(child, byMarker)
      if (tokens.length === 1 && tokens[0].kind === 'text') return child
      return tokens.map((tok, j) => {
        if (tok.kind === 'marker') {
          return (
            <CitationChip
              key={`c-${i}-${j}`}
              c={tok.citation}
              verdict={verdictFor(tok.citation.marker)}
              onClick={() => onCite(tok.citation)}
            />
          )
        }
        if (tok.kind === 'unresolved') {
          return <UnresolvedChip key={`u-${i}-${j}`} marker={tok.marker} />
        }
        return <span key={`t-${i}-${j}`}>{tok.text}</span>
      })
    }
    if (isValidElement(child)) {
      const props = child.props as { children?: ReactNode }
      if (props && props.children != null) {
        return {
          ...child,
          props: { ...props, children: linkChildren(props.children, byMarker, onCite, verdictFor) },
        }
      }
    }
    return child
  })
}

/** Wrap the shared dark markdown map so every text-bearing leaf links its
 *  citation markers. Only the prose-holding leaves are wrapped. */
function citedComponents(
  byMarker: Map<string, Citation>,
  onCite: (c: Citation) => void,
  verdictFor: (marker: string) => ClaimVerdict,
): Components {
  const base = MD_COMPONENTS
  const wrap = (key: keyof Components) => {
    const Original = base[key] as ((p: { children?: ReactNode }) => ReactNode) | undefined
    return (props: { children?: ReactNode }) => {
      const linked = linkChildren(props.children, byMarker, onCite, verdictFor)
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

export default function CitedProse({
  text,
  citations,
  verification = null,
  variant = 'block',
  onCiteClick,
  className,
}: CitedProseProps) {
  // Unwrap a JSON envelope, then normalize full-width 【N】/［N］ ordinal brackets
  // to ASCII so the marker parser resolves them to chips (never literal text).
  const prose = useMemo(() => normalizeCitationMarkers(unwrapEnvelope(text ?? '')), [text])
  const byMarker = useMemo(() => citationsByMarker(citations), [citations])
  const onCite = onCiteClick ?? defaultCiteClick
  // P1-8 — memoized per-marker verify verdicts (small map; derived once per
  // verification block, so re-renders never re-scan the spans list per chip).
  const verdictFor = useMemo(() => {
    const cache = new Map<string, ClaimVerdict>()
    return (marker: string): ClaimVerdict => {
      let v = cache.get(marker)
      if (!v) {
        v = claimVerdictForMarker(verification, marker)
        cache.set(marker, v)
      }
      return v
    }
  }, [verification])

  if (variant === 'inline') {
    // A clamped scan line: strip markdown to a flat flow (no headings/blocks) and
    // keep RESOLVED citation markers as tiny chips — never raw markdown, JSON, or a
    // `[3][4]` run. A marker with no citation in hand (the feed rarely carries the
    // citation list) is DROPPED here rather than shown as noise; the full cited
    // card is where a dangling marker is surfaced honestly.
    const flat = normalizeCitationMarkers(stripMarkdown(prose)).replace(/\s+/g, ' ').trim()
    const tokens = tokenizeProse(flat, byMarker)
    return (
      <span className={className} data-testid="cited-prose-inline">
        {tokens.map((tok, i) => {
          if (tok.kind === 'marker') {
            return (
              <span
                key={i}
                className="mx-0.5 rounded bg-surf-3 px-1 align-super text-[9px] text-accent-info"
                title={tok.citation.title ?? tok.citation.source ?? `evidence ${tok.citation.marker}`}
                data-testid="cited-prose-inline-chip"
                data-marker={tok.citation.marker}
              >
                {citationLabel(tok.citation.marker)}
              </span>
            )
          }
          if (tok.kind === 'unresolved') return null // drop dangling markers in the scan line
          return <span key={i}>{tok.text}</span>
        })}
      </span>
    )
  }

  const components = citedComponents(byMarker, onCite, verdictFor)
  return (
    // `report-prose` gives the reading columns a 45–75ch measure + 16px/1.7
    // off-white type scale (S7-T6); the caller's className still layers on top.
    <div className={`report-prose ${className ?? ''}`} data-testid="cited-prose">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {prose}
      </ReactMarkdown>
    </div>
  )
}
