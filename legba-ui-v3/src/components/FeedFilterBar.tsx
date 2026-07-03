/**
 * FeedFilterBar — the ONE filter surface for the unified Live Feed (S7-T4).
 *
 * A single row that carries: removable typed-facet CHIP pills, a free-text /
 * `key:value` input (type `severity:high ` and it becomes a chip), quick facet
 * dropdowns (severity + verification), and the saved-views strip. Verification
 * is a first-class facet here — the "verify" dropdown writes `verified:`/
 * `confidence:` chips read against the ICD-203 verdict vocabulary.
 *
 * Pure-ish presentational: all state lives in the parent (`ParsedFilter`); this
 * component only edits chips/text and raises callbacks. The filter model lives
 * in `@/lib/feedFilters`.
 */
import { Search, X, BookmarkPlus } from 'lucide-react'
import { VerdictLegend } from '@/components/VerdictBadge'
import {
  chipValue,
  mergeChips,
  parseFilterInput,
  removeChip,
  setChip,
  type FacetKey,
  type FeedChip,
  type FeedSavedView,
  type ParsedFilter,
} from '@/lib/feedFilters'

const SEVERITY_OPTS = ['', 'low', 'medium', 'high', 'critical'] as const

/** Verification quick-pick → the chip it writes (verified / confidence facet). */
const VERIFY_OPTS: Array<{ value: string; label: string; chip: FeedChip | null }> = [
  { value: '', label: 'any verification', chip: null },
  { value: 'verified', label: 'verified only', chip: { key: 'verified', value: 'true' } },
  { value: 'unverified', label: 'unverified only', chip: { key: 'verified', value: 'false' } },
  { value: 'conf-high', label: 'confidence: high', chip: { key: 'confidence', value: 'high' } },
  { value: 'conf-moderate', label: 'confidence: moderate', chip: { key: 'confidence', value: 'moderate' } },
  { value: 'conf-low', label: 'confidence: low', chip: { key: 'confidence', value: 'low' } },
]

function currentVerifyValue(chips: FeedChip[]): string {
  const verified = chipValue(chips, 'verified')
  if (verified === 'true') return 'verified'
  if (verified === 'false') return 'unverified'
  const conf = chipValue(chips, 'confidence')
  if (conf) return `conf-${conf}`
  return ''
}

export interface FeedFilterBarProps {
  parsed: ParsedFilter
  onChange: (next: ParsedFilter) => void
  views: FeedSavedView[]
  onApplyView: (view: FeedSavedView) => void
  onSaveView: () => void
  onDeleteView: (name: string) => void
  /** True when a severity facet is meaningless (the raw-signals stream). */
  severityDisabled?: boolean
}

export function FeedFilterBar({
  parsed,
  onChange,
  views,
  onApplyView,
  onSaveView,
  onDeleteView,
  severityDisabled = false,
}: FeedFilterBarProps) {
  // The input mirrors the free text; a completed `key:value` token (Enter or a
  // trailing space) is lifted OUT of the input into a chip pill.
  const setChips = (chips: FeedChip[]) => onChange({ chips, text: parsed.text })
  const setText = (text: string) => onChange({ chips: parsed.chips, text })

  function handleInput(val: string) {
    const trailingSpace = /\s$/.test(val)
    const p = parseFilterInput(val)
    if (p.chips.length > 0 && trailingSpace) {
      // Commit the completed facet token(s); the residual becomes the free text.
      onChange({ chips: mergeChips(parsed.chips, p.chips), text: p.text })
    } else {
      setText(val)
    }
  }

  function commitInput() {
    const p = parseFilterInput(parsed.text)
    onChange({ chips: mergeChips(parsed.chips, p.chips), text: p.text })
  }

  return (
    <div className="mb-2 space-y-1.5" data-testid="feed-filter-bar">
      {/* Row 1 — chips + input + quick facet dropdowns */}
      <div className="flex flex-wrap items-center gap-1.5 text-label">
        <div className="relative flex min-w-[180px] flex-1 items-center">
          <Search className="pointer-events-none absolute left-2 h-3.5 w-3.5 text-ink-3" aria-hidden />
          <input
            className="w-full rounded border border-line bg-surf-2 py-1 pl-7 pr-2 text-ink-1 placeholder:text-ink-3"
            placeholder="filter — e.g. severity:high verified:true country:iran last:7d"
            value={parsed.text}
            onChange={(e) => handleInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                commitInput()
              }
            }}
            data-testid="feed-filter-input"
            aria-label="feed filter"
          />
        </div>

        <select
          className="rounded border border-line bg-surf-2 px-1.5 py-1 text-ink-2 disabled:opacity-40"
          value={severityDisabled ? '' : chipValue(parsed.chips, 'severity')}
          disabled={severityDisabled}
          title={severityDisabled ? 'signals carry no severity' : 'severity facet'}
          onChange={(e) => setChips(setChip(parsed.chips, 'severity', e.target.value))}
          data-testid="feed-facet-severity"
        >
          {SEVERITY_OPTS.map((s) => (
            <option key={s || 'all'} value={s}>
              {s ? `severity: ${s}` : 'any severity'}
            </option>
          ))}
        </select>

        <select
          className="rounded border border-line bg-surf-2 px-1.5 py-1 text-ink-2 disabled:opacity-40"
          value={severityDisabled ? '' : currentVerifyValue(parsed.chips)}
          disabled={severityDisabled}
          title={severityDisabled ? 'signals are raw intake — not verify-assessed' : 'verification facet (ICD-203)'}
          onChange={(e) => {
            const opt = VERIFY_OPTS.find((o) => o.value === e.target.value)
            // Clear both verification facets, then apply the picked one (if any).
            let chips = parsed.chips.filter((c) => c.key !== 'verified' && c.key !== 'confidence')
            if (opt?.chip) chips = mergeChips(chips, [opt.chip])
            setChips(chips)
          }}
          data-testid="feed-facet-verify"
        >
          {VERIFY_OPTS.map((o) => (
            <option key={o.value || 'any'} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <VerdictLegend />
      </div>

      {/* Row 2 — active chip pills (removable) */}
      {parsed.chips.length > 0 && (
        <div className="flex flex-wrap items-center gap-1" data-testid="feed-active-chips">
          {parsed.chips.map((c) => (
            <span
              key={`${c.key}:${c.value}`}
              className="inline-flex items-center gap-1 rounded border border-line bg-surf-3 px-1.5 py-0.5 text-label text-ink-2"
              data-testid={`feed-chip-${c.key}-${c.value}`}
            >
              <span className="uppercase tracking-wide text-ink-3">{c.key}</span>
              <span className="text-ink-1">{c.value}</span>
              <button
                type="button"
                className="text-ink-3 hover:text-accent-critical"
                title={`remove ${c.key}:${c.value}`}
                onClick={() => setChips(removeChip(parsed.chips, c.key as FacetKey, c.value))}
                data-testid={`feed-chip-remove-${c.key}-${c.value}`}
              >
                <X className="h-3 w-3" aria-hidden />
              </button>
            </span>
          ))}
          <button
            type="button"
            className="ml-1 text-label text-ink-3 underline hover:text-ink-1"
            onClick={() => onChange({ chips: [], text: parsed.text })}
            data-testid="feed-chips-clear"
          >
            clear
          </button>
        </div>
      )}

      {/* Row 3 — saved views */}
      <div className="flex flex-wrap items-center gap-1.5 text-label">
        <span className="text-ink-3">views:</span>
        {views.length === 0 && <span className="text-ink-3">none saved</span>}
        {views.map((v) => (
          <span
            key={v.name}
            className="inline-flex items-center gap-1 rounded border border-line bg-surf-2 px-1.5 py-0.5"
            data-testid={`feed-view-${v.name}`}
          >
            <button
              className="text-ink-2 hover:text-ink-1"
              onClick={() => onApplyView(v)}
              data-testid={`feed-view-apply-${v.name}`}
              title={`${v.stream} · ${v.sort} · ${v.query || '(no filter)'}`}
            >
              {v.name}
            </button>
            <button
              className="text-ink-3 hover:text-accent-critical"
              onClick={() => onDeleteView(v.name)}
              title="delete view"
              data-testid={`feed-view-delete-${v.name}`}
            >
              <X className="h-3 w-3" aria-hidden />
            </button>
          </span>
        ))}
        <button
          className="inline-flex items-center gap-1 text-ink-2 underline hover:text-ink-1"
          onClick={onSaveView}
          data-testid="feed-save-view"
        >
          <BookmarkPlus className="h-3 w-3" aria-hidden />
          save view
        </button>
      </div>
    </div>
  )
}
