/**
 * ClaimTag — the cited source, and the fold reasons, BESIDE the claim (7b-i).
 *
 * A composed read's sentence ends in an ordinal. The ordinal is a promise that
 * something is behind the claim; until this component the reader had to leave
 * the sentence — hover, drill, scroll to the evidence panel — to find out what,
 * and the checking the platform already did (the wire fold, the single-source
 * mark, the judge's per-claim verdict) was invisible at the sentence.
 *
 * So the tag rides with the ordinal: masthead · date, then one chip per fold
 * reason the record states, each with a one-sentence tooltip taken from the
 * data (`lib/claimFold` is the whole derivation; this file only paints it).
 *
 * INLINE BY CONSTRUCTION — every element is a `<span>`, because this renders
 * inside markdown prose (a `<p>`/`<li>`), where a block element would be
 * invalid markup and would break the line the claim lives on.
 *
 * DEGRADES TO NOTHING. No masthead, no date, no fold keys ⇒ the component
 * renders `null` and the claim reads exactly as it did before 7b-i. That is
 * the honesty contract, not an optimisation: the absence of a fold key means
 * the record did not say, and a chip that appeared anyway would be a claim the
 * platform has not earned.
 */
import { citationDate, citationMasthead, type Citation } from '@/lib/citationsModel'
import type { ClaimFoldChip, ClaimFoldTone } from '@/lib/claimFold'
import { selectRow } from '@/state/selection'

/** Tone → the panel's accent tokens. Three channels, one meaning each: a
 *  contradiction is critical, a weakened claim is warning, a stated fold that
 *  cost nothing (the count still names every outlet) is muted. */
const TONE_CLASS: Record<ClaimFoldTone, string> = {
  critical: 'border-accent-critical/50 bg-accent-critical/10 text-accent-critical',
  warning: 'border-accent-warning/50 bg-accent-warning/10 text-accent-warning',
  muted: 'border-line bg-surf-2 text-ink-3',
}

/**
 * One fold reason. The tooltip is the sentence; the face is the vocabulary.
 *
 * A chip that carries a `drill` renders as a BUTTON and selects that record —
 * the same `selectRow` the absence rows use (k5b), so the Inspector resolves it
 * like every other kind and no new panel or modal is introduced. Only the 7a
 * contrary-evidence chip carries one today: its evidence is a page OUTSIDE this
 * tower, and a chip that stated it without offering it would be asking to be
 * taken on trust. Every other chip stays a plain `<span>` — read, never
 * clicked — because a drill that re-showed the row you are already reading is a
 * dead end.
 */
export function ClaimFoldChipTag({ chip }: { chip: ClaimFoldChip }) {
  const className = `ml-1 inline-flex items-center rounded border px-1 align-baseline text-[10px] font-medium leading-tight ${
    TONE_CLASS[chip.tone]
  }`
  if (chip.drill) {
    const drill = chip.drill
    return (
      <button
        type="button"
        className={`${className} hover:underline`}
        data-testid="claim-fold-chip"
        data-chip-kind={chip.kind}
        data-drills="true"
        title={`${chip.reason} Click to open the record.`}
        onClick={(e) => {
          // Inside a citation chip's own prose run: the ordinal beside this tag
          // has its own click, and letting this one bubble would scroll the
          // evidence panel as a side effect of opening the record.
          e.stopPropagation()
          selectRow(drill.kind, drill.id, drill.label, { origin: 'claim-fold-chip' })
        }}
      >
        {chip.label}
      </button>
    )
  }
  return (
    <span
      className={className}
      data-testid="claim-fold-chip"
      data-chip-kind={chip.kind}
      title={chip.reason}
    >
      {chip.label}
    </span>
  )
}

export interface ClaimTagProps {
  citation: Citation
  /** The fold reasons for this citation, already derived (`claimFoldChips`). */
  chips: ClaimFoldChip[]
}

/**
 * The compact source tag + the fold chips, rendered beside a claim's ordinal.
 * Returns `null` when the citation carries none of the three.
 */
export default function ClaimTag({ citation, chips }: ClaimTagProps) {
  const masthead = citationMasthead(citation)
  const date = citationDate(citation)
  if (!masthead && !date && chips.length === 0) return null
  // No `whitespace-nowrap` on the wrapper: in a narrow Inspector rail a
  // masthead plus three chips must be allowed to wrap onto the next line
  // rather than push the reading column sideways. The source tag itself stays
  // unbreakable, so "masthead · date" never splits across a line.
  return (
    <span className="align-baseline" data-testid="claim-tag">
      {(masthead || date) && (
        <span
          className="ml-1 inline-flex items-baseline gap-1 whitespace-nowrap align-baseline text-[10px] leading-tight text-ink-3"
          data-testid="claim-source-tag"
          data-masthead={masthead ?? undefined}
          data-date={date ?? undefined}
          title={
            masthead && date
              ? `cited source: ${masthead}, ${date}`
              : `cited source: ${masthead ?? date}`
          }
        >
          {masthead && <span className="max-w-[14rem] truncate">{masthead}</span>}
          {masthead && date && <span aria-hidden>·</span>}
          {date && <span className="font-mono">{date}</span>}
        </span>
      )}
      {chips.map((chip) => (
        <ClaimFoldChipTag key={chip.kind} chip={chip} />
      ))}
    </span>
  )
}
