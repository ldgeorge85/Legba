/**
 * VerdictBadge — the ONE verification dialect (S7-T3), ICD-203 aligned.
 *
 * Two MUTED chips, kept separate exactly as ICD-203 keeps the axes separate:
 *
 *   LIKELIHOOD  ·  how probable the judgment is ("likely", "very likely", …)
 *   CONFIDENCE  ·  how much the evidence backs it (Low / Moderate / High)
 *
 * The chips are deliberately low-saturation (slate) so they never compete with
 * the severity ramp; the numeric detail (probability %, faithfulness %, judge
 * status, citation count) floats to the hover/focus TOOLTIP rather than
 * cluttering the chip. A small `?` legend affordance opens the ICD-203 tables so
 * the vocabulary is discoverable in place. Nothing is fabricated: an `unstated`
 * likelihood or `unassessed` confidence renders as an explicit honest chip.
 *
 * This replaces the old scattered `faithfulness NN%` / `judge_status` /
 * `unverified` / `unrated` chips; use it wherever a finding or composition needs
 * a verdict.
 */
import { useState } from 'react'
import { HelpCircle } from 'lucide-react'
import {
  CONFIDENCE_LEGEND,
  LIKELIHOOD_LEGEND,
  STRUCTURAL_EXEMPT_NOTE,
  buildVerdict,
  judgeStatusLabel,
  type ConfidenceLevel,
  type Verdict,
  type VerdictInput,
} from '@/lib/verdictModel'

const CHIP =
  'inline-flex items-center gap-1 rounded border border-line bg-surf-2 px-1.5 py-0.5 text-[10px] leading-none text-ink-2'

/** Muted tonal accent per confidence level (a thin dot only — no filled chip,
 *  so it never reads as a severity color). `unassessed` gets a hollow ring. */
const CONFIDENCE_DOT: Record<ConfidenceLevel, string> = {
  high: 'bg-emerald-400/80',
  moderate: 'bg-sky-400/80',
  low: 'bg-amber-400/80',
  unassessed: 'border border-line-strong bg-transparent',
}

function pct(x: number | null): string {
  return x == null ? '—' : `${Math.round(x * 100)}%`
}

function confidenceLabel(level: ConfidenceLevel): string {
  return level === 'unassessed' ? 'unverified' : level
}

/** The two muted chips. `verdict` may be passed directly, or the raw finding
 *  fields via `input` (buildVerdict is applied). */
export function VerdictBadge({
  verdict,
  input,
  showLegend = false,
  className,
}: {
  verdict?: Verdict
  input?: VerdictInput
  /** Render the discoverable `?` legend affordance next to the chips. */
  showLegend?: boolean
  className?: string
}) {
  const v = verdict ?? buildVerdict(input ?? {})

  // P0-4 — a verify-EXEMPT structural analyst's finding never enters the
  // faithfulness verify pass; make the exception visible instead of letting
  // the row read like an ordinary (someday-verifiable) unverified one.
  const structural = v.confidence === 'unassessed' && v.structural === true

  const likelihoodTitle =
    v.likelihood === 'unstated'
      ? 'Likelihood: not stated by this read (no probability recorded)'
      : `ICD-203 likelihood · assessed probability ${pct(v.probability)}`
  const confidenceTitle =
    v.confidence === 'unassessed'
      ? structural
        ? `Analytic confidence: ${STRUCTURAL_EXEMPT_NOTE} (unverified — structural)`
        : 'Analytic confidence: no faithfulness-verify pass on this read (unverified)'
      : `Analytic confidence (evidence quality) · faithfulness ${pct(v.faithfulness)} · ` +
        `${judgeStatusLabel(v.judgeStatus)} · ${v.citationCount} citation${
          v.citationCount === 1 ? '' : 's'
        }`

  return (
    <span
      className={`inline-flex flex-wrap items-center gap-1.5 ${className ?? ''}`}
      data-testid="verdict-badge"
    >
      <span className={CHIP} title={likelihoodTitle} data-testid="verdict-likelihood">
        <span className="uppercase tracking-wide text-ink-3">L</span>
        <span className={v.likelihood === 'unstated' ? 'italic text-ink-3' : ''}>
          {v.likelihood}
        </span>
      </span>
      <span className={CHIP} title={confidenceTitle} data-testid="verdict-confidence">
        <span
          className={`h-1.5 w-1.5 shrink-0 rounded-full ${CONFIDENCE_DOT[v.confidence]}`}
          aria-hidden
        />
        <span className="uppercase tracking-wide text-ink-3">C</span>
        <span className={v.confidence === 'unassessed' ? 'italic text-ink-3' : ''}>
          {structural ? 'unverified — structural' : confidenceLabel(v.confidence)}
        </span>
      </span>
      {showLegend && <VerdictLegend />}
    </span>
  )
}

/** The discoverable `?` legend — a click-toggled popover with the ICD-203
 *  likelihood scale + the confidence definitions, so the vocabulary is
 *  learnable in place (never a bare undocumented chip). */
export function VerdictLegend({ className }: { className?: string }) {
  const [open, setOpen] = useState(false)
  return (
    <span className={`relative inline-flex ${className ?? ''}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center rounded p-0.5 text-ink-3 hover:text-ink-2"
        title="What do L (likelihood) and C (confidence) mean? — ICD-203"
        aria-expanded={open}
        data-testid="verdict-legend-toggle"
      >
        <HelpCircle className="h-3.5 w-3.5" aria-hidden />
      </button>
      {open && (
        <>
          {/* click-away backdrop */}
          <span
            className="fixed inset-0 z-40"
            aria-hidden
            onClick={() => setOpen(false)}
          />
          <span
            className="absolute left-0 top-5 z-50 w-72 rounded-lg border border-line-strong bg-surf-3 p-3 text-left shadow-xl"
            data-testid="verdict-legend-popover"
          >
            <div className="mb-1 text-[11px] font-semibold text-ink-1">
              Verdict vocabulary (ICD-203)
            </div>
            <div className="mb-1 text-[10px] leading-relaxed text-ink-2">
              Two independent axes — a claim's{' '}
              <span className="text-ink-1">likelihood</span> (how probable) is
              never conflated with the{' '}
              <span className="text-ink-1">confidence</span> we place in it
              (how well the evidence backs it).
            </div>
            <div className="mt-2 text-[10px] uppercase tracking-wide text-ink-3">
              L · likelihood
            </div>
            <ul className="mt-0.5 space-y-0.5">
              {LIKELIHOOD_LEGEND.map((row) => (
                <li key={row.band} className="flex justify-between gap-2 text-[10px] text-ink-2">
                  <span>{row.band}</span>
                  <span className="font-mono text-ink-3">{row.range}</span>
                </li>
              ))}
            </ul>
            <div className="mt-2 text-[10px] uppercase tracking-wide text-ink-3">
              C · confidence
            </div>
            <ul className="mt-0.5 space-y-0.5">
              {CONFIDENCE_LEGEND.map((row) => (
                <li key={row.level} className="text-[10px] text-ink-2">
                  <span className="font-medium text-ink-1">{confidenceLabel(row.level)}</span>
                  <span className="text-ink-3"> — {row.meaning}</span>
                </li>
              ))}
            </ul>
          </span>
        </>
      )}
    </span>
  )
}
