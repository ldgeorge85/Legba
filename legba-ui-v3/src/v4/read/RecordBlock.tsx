/**
 * RecordBlock — one quoted desk head in the Morning Read's record band.
 *
 * ── THE READING DECISION THIS FILE ENCODES ────────────────────────────────────
 * Under the demotion every world-claim on the page is a VERBATIM span of a desk
 * head (`DEMOTION_D1_SPEC_2026-09-04.md` §1.2/§1.8). The obvious rendering — one
 * boxed quote per span, with its role tag, offsets and verify chips above it —
 * is what the rejected mock did, and it turns a page of real sentences into a
 * table of records. The operator's ruling was that it could not be read.
 *
 * So the block is arranged the way a paragraph is arranged, not the way a row is:
 *
 *   1. The BOUNDED QUESTION is the header (§B.4 Correction 3). It is a real
 *      sentence in the reading face, and after the composed prose is gone it is
 *      the surface that carries the product's register (VOICE §4.2 rank 2).
 *   2. The LEAD SPAN is the lede — one sentence, at reading size, in primary
 *      ink. Every remaining span flows beneath it as ONE paragraph. Both are
 *      verbatim; joining them adds no word, and every span in a block shares one
 *      origin head, so nothing is attributed to the wrong desk by the join.
 *   3. ATTRIBUTION COMES AFTER. Desk, target, stamp and the origin head id sit
 *      in a caption BELOW the prose, at `--ink-3`. You read the claim, then who
 *      made it — which is the order a reader actually wants and the inverse of
 *      the mock's header-first block.
 *   4. SEVERITY AND VERIFY LIVE IN THE MARGIN. A narrow rail carries the
 *      ordinal, the severity dot and the verify score, at label size, folding to
 *      one inline line below `52rem`. They qualify the sentence; they must be
 *      quieter than it.
 *   5. THE EVIDENCE IS ONE CLICK DOWN. The signals + correlation grain is real
 *      and large (7 signals on a typical block, 892 citation rows across 227
 *      desk heads on the specimen day). Rendered open it is a wall. It is a
 *      per-block disclosure, and a citation chip opens it and scrolls to its own
 *      row — the drill lands on the thing that was clicked.
 *
 * Drilling must reward the reader with BETTER prose, not worse (VOICE §4.6.3):
 * the expand also mounts `CountryUnitsAssessment`, so the eight bounded desk
 * reads behind the block are one click from the sentence they produced.
 */
import { useCallback, useId, useMemo, useState } from 'react'
import { ChevronDown, ChevronRight, ShieldAlert } from 'lucide-react'
import CitedProse from '@/components/CitedProse'
import { SeverityDot } from '@/components/SeverityBadge'
import { CountryUnitsAssessment } from '@/v4/why/CountryUnitsAssessment'
import { citationAnchorId, type Citation } from '@/lib/citationsModel'
import { emitRead } from '@/lib/readTelemetry'
import { selectRow } from '@/state/selection'
import type { AssemblyBlock, AssemblySpan } from '@/lib/assemblyModel'
import { age, asSeverity, plural, score, scrollToAnchor, shortId, utcStamp } from './readFormat'

/**
 * The block's signals, as the citation dialect `CitedProse` already speaks.
 *
 * `blocks[].signals[]` is a pass-through of the ORIGIN head's own
 * `data.data.citations[]` (spec §1.2b), so the `[N]` markers inside a quoted
 * span are the desk's own markers and they resolve here — the same chips, the
 * same hover cards and the same `citation_drill` telemetry the rest of the
 * workstation already emits. Nothing new is invented for this surface.
 */
export function blockCitations(block: AssemblyBlock): Citation[] {
  return block.signals
    .filter((s) => s.marker !== '')
    .map((s) => ({
      marker: s.marker,
      refId: s.signal_id ?? '',
      refKind: 'signal' as const,
      title: s.title,
      source: s.url ?? undefined,
      signalId: s.signal_id ?? '',
    }))
}

/** The lead span (§1.4d) — the head's BLUF, or the stamped fallback. */
export function leadSpan(block: AssemblyBlock): AssemblySpan | null {
  return (
    block.spans.find((s) => s.role === 'bluf') ??
    block.spans.find((s) => s.role === 'what_changed') ??
    block.spans[0] ??
    null
  )
}

/**
 * §3.6 READ time: a row whose arms failed refuses the affected blocks.
 *
 * The refusal is deliberately not a degraded render of the block. A span that
 * cannot be resolved against its origin body is a CONSTRUCTION failure, not a
 * lower score, and showing it with a warning would be publishing a sentence the
 * system cannot prove anyone wrote.
 */
function Refusal({ block }: { block: AssemblyBlock }) {
  return (
    <div
      className="rounded border border-line-strong bg-surf-2 px-3 py-2.5 text-xs leading-relaxed text-ink-2"
      data-testid="read-block-refused"
    >
      <div className="mb-1 flex items-center gap-1.5 font-medium text-ink-1">
        <ShieldAlert className="h-3.5 w-3.5" style={{ color: 'var(--sev-critical)' }} aria-hidden />
        Block {block.ordinal} is withheld
      </div>
      A verify arm could not confirm this block&rsquo;s quotation against its origin head. Under the
      assembly contract that is a construction failure, not a lower score — so the sentence is not
      shown. The origin head is still reachable below.
    </div>
  )
}

/** The per-block evidence drill: the signals, their correlation, and the desk. */
function BlockEvidence({ block }: { block: AssemblyBlock }) {
  return (
    <div className="mt-3 border-l border-line pl-3" data-testid="read-block-evidence">
      <ul className="space-y-2.5">
        {block.signals.map((s, i) => (
          <li
            key={`${s.signal_id ?? s.marker}-${i}`}
            id={s.signal_id ? citationAnchorId({ marker: s.marker, refId: s.signal_id, refKind: 'signal', signalId: s.signal_id }) : undefined}
            className="text-xs leading-relaxed"
            data-testid="read-signal"
          >
            <div className="flex flex-wrap items-baseline gap-1.5 text-ink-3">
              <span className="font-mono text-ink-2">{s.marker}</span>
              {s.source_id && <span>{s.source_id.replace(/^source\./, '')}</span>}
              {age(s.age_h) && <span>· {age(s.age_h)} old</span>}
              {s.salience_magnitude !== null && (
                <span title="signal salience magnitude">· salience {score(s.salience_magnitude)}</span>
              )}
            </div>
            <div className="text-ink-2">{s.title}</div>
            {s.url && (
              <a
                href={s.url}
                target="_blank"
                rel="noreferrer"
                className="break-all text-ink-3 underline decoration-dotted underline-offset-2 hover:text-ink-2"
              >
                {s.url}
              </a>
            )}
            {s.also_cited_by.length > 0 && (
              <div className="text-ink-3" data-testid="read-signal-shared">
                also cited by{' '}
                {s.also_cited_by
                  .map((d) => `${d.desk}${d.target_id ? ` / ${d.target_id}` : ''}`)
                  .join(', ')}
              </div>
            )}
          </li>
        ))}
        {block.signals.length === 0 && (
          <li className="text-xs text-ink-3">
            This head published no wire citations — its span is quoted, its evidence map is empty.
          </li>
        )}
      </ul>

      {/* The desk sentence is the organic one (VOICE §4.6.2/3): drilling lands on
          the eight bounded reads behind this block, not on a thinner summary. */}
      {block.target_id && (
        <div className="mt-3.5 border-t border-line pt-3" data-testid="read-block-units">
          <CountryUnitsAssessment targetId={block.target_id} />
        </div>
      )}
    </div>
  )
}

export interface RecordBlockProps {
  block: AssemblyBlock
  /** Ordinals in the lead position, if the concentration test earned one. */
  lead: boolean
  refused: boolean
}

export default function RecordBlock({ block, lead, refused }: RecordBlockProps) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const citations = useMemo(() => blockCitations(block), [block])
  const head = leadSpan(block)
  const rest = useMemo(
    () => block.spans.filter((s) => s !== head).map((s) => s.text).join(' '),
    [block.spans, head],
  )
  const sev = asSeverity(block.severity)
  const stamp = utcStamp(block.produced_at)

  const openEvidence = useCallback(() => {
    setOpen((was) => {
      if (!was) {
        // A drill from the read into the origin head's own evidence map.
        emitRead('lineage_walk', { subjectKind: 'finding', subjectId: block.finding_id })
      }
      return !was
    })
  }, [block.finding_id])

  // A citation chip opens the drill and lands on its own row. `CitedProse` has
  // already counted the `citation_drill` by the time this runs.
  const onCite = useCallback(
    (c: Citation) => {
      setOpen(true)
      if (typeof window === 'undefined') return
      // The rows only exist once the drill has rendered, so the scroll waits a
      // tick. Never throws: jsdom (and any environment without a layout engine)
      // does not implement `scrollIntoView`.
      window.setTimeout(() => scrollToAnchor(citationAnchorId(c)), 0)
    },
    [],
  )

  const shared = block.corroboration?.n_desks_sharing ?? 0

  return (
    <article
      className="read-block"
      id={`read-block-${block.ordinal}`}
      data-testid="read-block"
      data-ordinal={block.ordinal}
      data-lead={lead ? 'true' : undefined}
    >
      <div className="read-rail" aria-hidden={false}>
        <span className="read-ordinal">{block.ordinal}</span>
        {sev && (
          <span title={`severity ${block.severity}`} className="flex items-center">
            <SeverityDot severity={sev} className="h-3 w-3" />
          </span>
        )}
        {block.verify?.overall_score !== null && block.verify !== null && (
          <span
            className="font-mono tabular-nums"
            title={
              `desk verify ${score(block.verify.overall_score)}` +
              (block.verify.checkable_claims !== null
                ? ` · ${block.verify.supported_claims ?? 0}/${block.verify.checkable_claims} claims`
                : '') +
              ` · judged ${block.verify.judge_status}`
            }
            data-testid="read-block-verify"
          >
            {score(block.verify.overall_score)}
          </span>
        )}
        {block.tier === 'periphery' && (
          <span
            className="text-[0.625rem] uppercase tracking-wide"
            style={{ color: 'var(--sev-medium)' }}
            title="below the verify floor — carried as periphery, not basis"
            data-testid="read-block-periphery"
          >
            below floor
          </span>
        )}
      </div>

      <div className="min-w-0">
        {/* The desk's bounded question — the block header and the voice surface. */}
        <h3 className="read-question" data-testid="read-question">
          {block.question}
          {block.question_source === 'fallback_desk_name' && (
            <span className="ml-1.5 text-[0.6875rem] text-ink-3" title="no bounded question is declared on this desk's descriptor yet">
              (desk name — no question declared)
            </span>
          )}
        </h3>

        {refused ? (
          <Refusal block={block} />
        ) : (
          <>
            {head && (
              <div className="read-lede report-prose" data-testid="read-lede">
                <CitedProse text={head.text} citations={citations} onCiteClick={onCite} />
              </div>
            )}
            {rest !== '' && (
              <div className="read-rest report-prose" data-testid="read-rest">
                <CitedProse text={rest} citations={citations} onCiteClick={onCite} />
              </div>
            )}
            {head && head.scope_tokens.length > 0 && (
              <p className="mt-1.5 text-[0.6875rem] text-ink-3" data-testid="read-scope">
                scope preserved from the source sentence: &ldquo;{head.scope_tokens.join('&rdquo;, &ldquo;')}&rdquo;
              </p>
            )}
          </>
        )}

        <p className="read-attrib" data-testid="read-attrib">
          <button
            type="button"
            className="text-left underline decoration-dotted underline-offset-2 hover:text-ink-2"
            onClick={() => {
              emitRead('finding_open', { subjectKind: 'finding', subjectId: block.finding_id })
              selectRow('finding', block.finding_id, block.question, { origin: 'morning-read' })
            }}
            title="Open this desk head in the Inspector"
            data-testid="read-open-head"
          >
            {block.desk} · {block.target_name ?? block.target_id ?? 'no target'}
            {stamp ? ` · ${stamp}` : ''}
          </button>
          <span aria-hidden>·</span>
          <span title={`origin head ${block.finding_id}`}>
            verbatim from head <span className="font-mono">{shortId(block.finding_id)}</span>
          </span>
          {block.evidence_age_h !== null && <span aria-hidden>·</span>}
          {block.evidence_age_h !== null && <span>{age(block.evidence_age_h)} old</span>}
          <span aria-hidden>·</span>
          <button
            type="button"
            onClick={openEvidence}
            aria-expanded={open}
            aria-controls={panelId}
            className="inline-flex items-center gap-1 underline decoration-dotted underline-offset-2 hover:text-ink-2"
            data-testid="read-evidence-toggle"
          >
            {open ? (
              <ChevronDown className="h-3 w-3" aria-hidden />
            ) : (
              <ChevronRight className="h-3 w-3" aria-hidden />
            )}
            {plural(block.signals.length, 'signal')}
            {block.corroboration ? ` · ${plural(block.corroboration.n_sources, 'source')}` : ''}
            {shared > 0 ? ` · shared with ${plural(shared, 'other desk read')}` : ''}
          </button>
        </p>

        {open && (
          <div id={panelId}>
            <BlockEvidence block={block} />
          </div>
        )}
      </div>
    </article>
  )
}
