/**
 * FeedFilterBar — the ONE filter surface for the unified Live Feed (S7-T4).
 *
 * Carries: removable typed-facet CHIP pills, a free-text / `key:value` input
 * (type `severity:high ` and it becomes a chip), a row of quick facet
 * dropdowns, and the saved-views strip. Verification is a first-class facet —
 * the "verify" dropdown writes `verified:`/`judge:`/`confidence:` chips read
 * against the ICD-203 verdict vocabulary (`judge:` = how the verify pass ran:
 * llm / deterministic / unsampled, the honest J2 sampling-gate state included).
 *
 * The dropdown row is the feed's DRILL-DOWN surface, and every control on it
 * writes an ordinary chip, so anything the sidebar/map can put on the feed the
 * operator can also set — and clear — by hand:
 *
 *   desk/target · producer (units · compositions · other) · output kind ·
 *   severity · verification · effective-confidence floor
 *
 * The desk and producer option lists are supplied by the panel (`deskOptions` /
 * `producerOptions`) because they are DATA — the live desk roster and the
 * producers that actually exist — not a hard-coded menu. `kindOptions` likewise
 * lists only the substrate output kinds actually present in the loaded rows: an
 * empty axis renders as a disabled "any kind", never a fabricated menu.
 *
 * Pure-ish presentational: all state lives in the parent (`ParsedFilter`); this
 * component only edits chips/text and raises callbacks. The filter model lives
 * in `@/lib/feedFilters`.
 */
import { Search, X, BookmarkPlus } from 'lucide-react'
import { VerdictLegend } from '@/components/VerdictBadge'
import { PRODUCER_GROUP_LABEL, type ProducerClass, type ProducerOption } from '@/lib/feedProducers'
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

/**
 * The effective-confidence FLOOR quick-pick (`minconf:` chip). 0.50 is the
 * system-wide verification floor (the platform's 0.50 decision, mirrored server
 * side as `substrate_reads_api._FAITH_FLOOR`), so "clears the floor" is a real,
 * named threshold rather than an arbitrary slider stop.
 */
const BAND_OPTS: Array<{ value: string; label: string }> = [
  { value: '', label: 'any effective conf' },
  { value: '0.5', label: 'effective ≥ 0.50 (clears floor)' },
  { value: '0.7', label: 'effective ≥ 0.70' },
]

/** Group the producer options into `<optgroup>`s, preserving the model's order. */
const PRODUCER_GROUP_ORDER: ProducerClass[] = ['unit', 'composition', 'other']

/** Verification quick-pick → the chip it writes (verified / judge / confidence
 *  facet). The `judge:` rows are the GLASS-1 server-side facet — llm /
 *  deterministic / unsampled, with the honest J2 unsampled stratum a
 *  first-class pick, not a client-side sieve. */
const VERIFY_OPTS: Array<{ value: string; label: string; chip: FeedChip | null }> = [
  { value: '', label: 'any verification', chip: null },
  { value: 'verified', label: 'verified only', chip: { key: 'verified', value: 'true' } },
  { value: 'unverified', label: 'unverified only', chip: { key: 'verified', value: 'false' } },
  { value: 'judge-llm', label: 'judge: llm', chip: { key: 'judge', value: 'llm' } },
  { value: 'judge-deterministic', label: 'judge: deterministic', chip: { key: 'judge', value: 'deterministic' } },
  { value: 'judge-unsampled', label: 'judge: unsampled', chip: { key: 'judge', value: 'unsampled' } },
  { value: 'conf-high', label: 'confidence: high', chip: { key: 'confidence', value: 'high' } },
  { value: 'conf-moderate', label: 'confidence: moderate', chip: { key: 'confidence', value: 'moderate' } },
  { value: 'conf-low', label: 'confidence: low', chip: { key: 'confidence', value: 'low' } },
]

/** The chip keys the verify dropdown owns — picking any option clears them all
 *  first, so the single-pick select never leaves a stale sibling chip behind. */
const VERIFY_CHIP_KEYS: ReadonlySet<string> = new Set(['verified', 'judge', 'confidence'])

function currentVerifyValue(chips: FeedChip[]): string {
  const verified = chipValue(chips, 'verified')
  if (verified === 'true') return 'verified'
  if (verified === 'false') return 'unverified'
  const judge = chipValue(chips, 'judge')
  if (judge) return `judge-${judge}`
  const conf = chipValue(chips, 'confidence')
  if (conf) return `conf-${conf}`
  return ''
}

/** One desk/target option — the exact `target_id` plus its human name. */
export interface DeskOption {
  id: string
  label: string
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
  /** The desk roster (+ any target in view) for the desk dropdown. */
  deskOptions?: DeskOption[]
  /** Grouped producers for the analyst/unit dropdown (see `feedProducers`). */
  producerOptions?: ProducerOption[]
  /** Substrate output kinds present in the loaded rows. */
  kindOptions?: string[]
}

export function FeedFilterBar({
  parsed,
  onChange,
  views,
  onApplyView,
  onSaveView,
  onDeleteView,
  severityDisabled = false,
  deskOptions = [],
  producerOptions = [],
  kindOptions = [],
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

  // A chip value the option list doesn't carry (hand-typed, or a desk the
  // roster hasn't loaded yet) still has to SHOW in its dropdown — otherwise the
  // select silently falls back to "any" and lies about the active filter.
  const deskValue = chipValue(parsed.chips, 'target')
  const deskChoices: DeskOption[] =
    deskValue && !deskOptions.some((d) => d.id === deskValue)
      ? [{ id: deskValue, label: deskValue }, ...deskOptions]
      : deskOptions
  const producerValue = chipValue(parsed.chips, 'analyst')
  const producerChoices: ProducerOption[] =
    producerValue && !producerOptions.some((p) => p.id === producerValue)
      ? [{ id: producerValue, label: producerValue, group: 'other', present: false }, ...producerOptions]
      : producerOptions
  const kindValue = chipValue(parsed.chips, 'kind')
  const kindChoices = kindValue && !kindOptions.includes(kindValue) ? [kindValue, ...kindOptions] : kindOptions

  return (
    <div className="mb-2 space-y-1.5" data-testid="feed-filter-bar">
      {/* Row 1 — free text / `key:value` input */}
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
        <VerdictLegend />
      </div>

      {/* Row 2 — the drill-down dropdowns. Every one writes an ordinary chip,
          so each is equally clearable from the chip row below. */}
      <div className="flex flex-wrap items-center gap-1.5 text-label">
        {/* Desk / target — the facet the sidebar SEEDS and the operator owns. */}
        <select
          className="max-w-[150px] rounded border border-line bg-surf-2 px-1.5 py-1 text-ink-2"
          value={deskValue}
          title="desk / target facet — the same chip a sidebar desk click seeds"
          onChange={(e) => setChips(setChip(parsed.chips, 'target', e.target.value))}
          data-testid="feed-facet-desk"
          aria-label="desk filter"
        >
          <option value="">any desk</option>
          {deskChoices.map((d) => (
            <option key={d.id} value={d.id}>
              {d.label}
            </option>
          ))}
        </select>

        {/* Producer — units / compositions / other, grouped. */}
        <select
          className="max-w-[170px] rounded border border-line bg-surf-2 px-1.5 py-1 text-ink-2 disabled:opacity-40"
          value={producerValue}
          disabled={severityDisabled}
          title={
            severityDisabled
              ? 'signals are raw intake — they carry no producing analyst'
              : 'analyst / unit facet'
          }
          onChange={(e) => setChips(setChip(parsed.chips, 'analyst', e.target.value))}
          data-testid="feed-facet-producer"
          aria-label="producer filter"
        >
          <option value="">any producer</option>
          {PRODUCER_GROUP_ORDER.map((group) => {
            const inGroup = producerChoices.filter((p) => p.group === group)
            if (inGroup.length === 0) return null
            return (
              <optgroup key={group} label={PRODUCER_GROUP_LABEL[group]}>
                {inGroup.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                  </option>
                ))}
              </optgroup>
            )
          })}
        </select>

        {/* Output kind — only the substrate kinds actually in view. */}
        <select
          className="max-w-[130px] rounded border border-line bg-surf-2 px-1.5 py-1 text-ink-2 disabled:opacity-40"
          value={kindValue}
          disabled={kindChoices.length === 0}
          title={
            kindChoices.length === 0
              ? 'no output kinds loaded yet'
              : 'substrate output kind facet'
          }
          onChange={(e) => setChips(setChip(parsed.chips, 'kind', e.target.value))}
          data-testid="feed-facet-kind"
          aria-label="output kind filter"
        >
          <option value="">any kind</option>
          {kindChoices.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>

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
            // Clear every verification facet, then apply the picked one (if any).
            let chips = parsed.chips.filter((c) => !VERIFY_CHIP_KEYS.has(c.key))
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

        {/* Effective-confidence floor — the numeric band gate, distinct from the
            ICD-203 verification LEVEL next to it. */}
        <select
          className="rounded border border-line bg-surf-2 px-1.5 py-1 text-ink-2 disabled:opacity-40"
          value={severityDisabled ? '' : chipValue(parsed.chips, 'minconf')}
          disabled={severityDisabled}
          title={
            severityDisabled
              ? 'signals carry no graded confidence'
              : 'surfaced (critic-folded) effective-confidence floor'
          }
          onChange={(e) => setChips(setChip(parsed.chips, 'minconf', e.target.value))}
          data-testid="feed-facet-band"
          aria-label="effective confidence floor"
        >
          {BAND_OPTS.map((o) => (
            <option key={o.value || 'any'} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>

      {/* Row 3 — active chip pills (removable) */}
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

      {/* Row 4 — saved views */}
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
