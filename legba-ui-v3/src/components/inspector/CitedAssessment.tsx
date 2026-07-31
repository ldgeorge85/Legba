/**
 * CitedAssessment — the per-country / composition read surface (P1-T3, reworked
 * for S7-T3's reading kit).
 *
 * Renders a finding's report as a CITED card: the prose up top (via the shared
 * `CitedProse` renderer, so `[N]` / `[[ref:N]]` markers become interactive chips
 * with hover-cards + an explicit unresolved state), an evidence panel below.
 * Clicking a chip scrolls to + highlights that citation's evidence row, whose
 * title is a `RecordLink` into the cited signal / sub-claim.
 *
 * Verification is expressed in the ONE dialect — a `VerdictBadge` (ICD-203
 * likelihood + confidence), with the legend affordance — replacing the old
 * scattered `faithfulness NN%` / `judge_status` / `unverified` chips.
 *
 * DEGRADE / HONESTY: a legacy / uncited finding has no citations → the prose
 * renders plainly under an explicit "uncited" marker, no fabricated anchors.
 */
import { ShieldAlert, FileText } from 'lucide-react'
import CitedProse from '@/components/CitedProse'
import { RecordLink } from '@/components/inspector/RecordLink'
import { UnitEvalBadge } from '@/components/inspector/UnitEvalBadge'
import { VerdictBadge } from '@/components/VerdictBadge'
import { type Citation, evidenceAnchorId } from '@/lib/citationsModel'
import {
  STRUCTURAL_EXEMPT_NOTE,
  buildVerdict,
  type VerificationBlock,
} from '@/lib/verdictModel'

export interface CitedAssessmentProps {
  /** The report prose (carries inline `[N]` / `[[ref:N]]` markers when cited). */
  text: string
  /** The citation list extracted from the merged finding body. */
  citations: Citation[]
  /** The finding-level faithfulness-verify block, when present (else null). */
  verification?: Record<string, unknown> | null
  /** The finding's own probability/confidence [0,1] — the likelihood axis. */
  confidence?: number | null
  /** The finding's analyst id — keys the per-unit eval badge (P2-T6). A
   *  non-bounded-unit id simply renders no badge. */
  analystId?: string | null
}

/**
 * The cited assessment card. Renders the cited path when `citations` is
 * non-empty, otherwise the honest uncited path (prose only + an "uncited"
 * marker).
 */
export default function CitedAssessment({
  text,
  citations,
  verification = null,
  confidence = null,
  analystId = null,
}: CitedAssessmentProps) {
  const cited = citations.length > 0
  const verdict = buildVerdict({
    confidence,
    verification: (verification as VerificationBlock | null) ?? null,
    citationCount: citations.length,
    // P0-4 — a verify-exempt structural analyst's read renders the explicit
    // `unverified — structural` badge, never the ambiguous bare `unverified`.
    analystId,
  })

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
        <VerdictBadge verdict={verdict} showLegend />
        <UnitEvalBadge analystId={analystId} />
      </div>

      {/* P0-4 — one-line subtext for a verify-EXEMPT structural read, so the
          exception is stated in place (the badge tooltip repeats it). */}
      {verdict.structural && verdict.confidence === 'unassessed' && (
        <div
          className="mb-2 text-label italic text-ink-3"
          data-testid="structural-exempt-note"
        >
          {STRUCTURAL_EXEMPT_NOTE}
        </div>
      )}

      {/* The report prose — one shared renderer: markdown always rendered, markers
          tokenized to chips (hover-card + unresolved state). Chip click scrolls to
          the evidence row below (CitedProse's default finds the on-page anchor).
          The verify block rides along (P1-8) so each chip's hover card carries
          its per-claim judge verdict — or the honest not-recorded line. */}
      <div className="text-body text-ink-1">
        <CitedProse text={text} citations={citations} verification={verification} />
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
                  {c.refKind === 'situation_register' ? (
                    // Continuity situation register: a real citation with NO
                    // single drill target by design — labeled, non-drilling.
                    <span className="text-body text-fg-muted">
                      {c.title ?? 'Open-situation register'}
                    </span>
                  ) : (
                    <RecordLink
                      kind={c.refKind}
                      id={c.refId}
                      label={c.title ?? c.refId}
                      origin="cited-assessment"
                      showKind
                    />
                  )}
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
