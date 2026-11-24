/**
 * The banded verdicts per subject (P4-T3/T5) — one honest card per scored
 * subject: a band per dimension, click a band → its verified sub-claims (basis
 * findings, each an evidence + signed-lineage drill), a per-dimension
 * faithfulness+correctness badge, and an explicit not-enough-verified state
 * for an insufficient-evidence band — never a fabricated band. Moved out of the
 * Accuracy page's panel file (UI-1); part of its calibration section, because
 * these are the bands the band-calibration harness grades.
 *
 * "download CSV" (7b-iii) serialises the bands the page is showing — one row
 * per (subject, dimension), insufficient dimensions included — straight from the
 * `CountryScorecard[]` in hand (`@/lib/bandCsv`). No second fetch and no new
 * route, so the file cannot disagree with the section it came from.
 */
import { useMemo, useState } from 'react'
import { Download } from 'lucide-react'
import { InfoTip } from '@/components/InfoTip'
import { RecordLink } from '@/components/inspector/RecordLink'
import { ScaleStamp } from '@/components/ScaleStamp'
import { BAND_CSV_MIME, buildBandCsv } from '@/lib/bandCsv'
import { humanizeId } from '@/lib/deskNames'
import {
  bandTone,
  evalBadge,
  insufficientLabel,
  isInsufficient,
  orderedDimensions,
  relTime,
  type BandTone,
  type CountryScorecard,
} from '@/lib/evalOps'
import { downloadText } from '@/lib/reportDownload'
import { PROFILE_UI } from '@/lib/profileUi'
import { FAITHFULNESS_EXPLAIN } from '@/lib/verdictModel'

/** Past this many subjects the cards fold behind one "show all" — the
 *  calibration section must not push the graders and the closing list off the
 *  page. Below it they render open, as before. */
export const BANDS_OPEN_MAX = 6

/** U-5 — reused wherever this panel shows a raw "faithfulness N | correctness
 *  N (n=k)" / "unmeasured" badge, so the honest-absence idiom (never a
 *  fabricated score) reads as "nothing measured yet", not as an error. */
const EVAL_BADGE_EXPLAIN =
  `${FAITHFULNESS_EXPLAIN} Correctness (when shown) is graded against operator ` +
  'gold labels. "Unmeasured" means neither has been computed yet for this ' +
  'basis claim — an honest absence, not a broken score.'

// Coarse band-tone → pill color. `insufficient` is a MUTED honest tone, never a
// severity color — an insufficient band renders no colored pill at all.
const TONE_PILL: Record<BandTone, string> = {
  good: 'bg-emerald-900 text-emerald-200',
  watch: 'bg-amber-900 text-amber-200',
  elevated: 'bg-orange-900 text-orange-200',
  high: 'bg-rose-900 text-rose-200',
  critical: 'bg-red-900 text-red-200',
  insufficient: 'bg-surf-3 text-ink-2',
}

// A subject id reads as the subject's DECLARED name (its descriptor, read
// through the subject roster — UI-B2: never a regex over the id); anything
// else (unit/analyst ids) is humanized generically (split, title-case). The
// shared `lib/deskNames.ts` util is the ONE place this mapping lives (the rail
// and the Wall's movers list use the same resolver — U-2). The raw id is
// still used for keys / RecordLink drills.
const displayName = humanizeId

// Tone severity ordering — for rolling per-dimension bands up to one headline.
const TONE_SEVERITY: BandTone[] = ['insufficient', 'good', 'watch', 'elevated', 'high', 'critical']

/** Roll the per-dimension bands up to a single country headline tone: the most
 *  severe banded dimension, or `insufficient` when nothing is banded yet. */
function countryHeadline(sc: CountryScorecard): BandTone {
  let worst: BandTone = 'insufficient'
  for (const dim of Object.values(sc.dimensions)) {
    if (isInsufficient(dim)) continue
    const t = bandTone(dim.band)
    if (TONE_SEVERITY.indexOf(t) > TONE_SEVERITY.indexOf(worst)) worst = t
  }
  return worst
}

export function SubjectBands({ scorecards }: { scorecards: CountryScorecard[] | undefined }) {
  // Which (target:unit) band is expanded to its basis sub-claims.
  const [openBand, setOpenBand] = useState<string | null>(null)
  // Which subject's collapsed "insufficient" group is expanded to its why-drill.
  const [openInsufficient, setOpenInsufficient] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const subject = PROFILE_UI.subject.singular.toLowerCase()
  const subjects = PROFILE_UI.subject.plural.toLowerCase()
  const countryCards = useMemo(
    () =>
      [...(scorecards ?? [])].sort((a, b) =>
        (a.target_id ?? '').localeCompare(b.target_id ?? ''),
      ),
    [scorecards],
  )

  /** 7b-iii — the banded section as CSV, built from the rows already on this
   *  page (`@/lib/bandCsv`): no second read, so the file and the section can
   *  never disagree. Every dimension travels, insufficient ones included. */
  function downloadBandCsv() {
    if (countryCards.length === 0) return
    const csv = buildBandCsv(countryCards)
    downloadText(csv.filename, BAND_CSV_MIME, csv.content)
  }
  const collapsible = countryCards.length > BANDS_OPEN_MAX

  return (
      <>
      {/* Banded per-subject scorecard (P4-T3/T5). One honest card per scored
          subject: a band per dimension, click a band → its verified sub-claims
          (basis findings, each a P1 evidence + signed-lineage drill), a per-dim
          faithfulness+correctness badge, and an explicit not-enough-verified
          state for an insufficient-evidence band — never a fabricated band. */}
      <div className="mb-2 space-y-2" data-testid="scorecard-country-list">
        <div className="flex items-baseline gap-2">
          <span className="text-ink-3 text-label uppercase tracking-wide">
            banded verdicts (per {subject})
          </span>
          {collapsible && (
            <button
              type="button"
              onClick={() => setShowAll(!showAll)}
              className="text-label text-ink-3 hover:text-ink-1"
              aria-expanded={showAll}
              data-testid="scorecard-show-all"
            >
              {showAll ? 'hide' : `show all ${countryCards.length} ${subjects}`}
            </button>
          )}
          <button
            type="button"
            onClick={downloadBandCsv}
            disabled={countryCards.length === 0}
            className="ml-auto flex items-center gap-1 rounded border border-line-strong bg-surf-1 px-2 py-0.5 text-label text-ink-1 hover:bg-surf-3 disabled:opacity-40"
            title={
              countryCards.length === 0
                ? 'No scorecard computed yet — there is nothing to export'
                : `Download these bands as CSV — one row per (${subject}, dimension), ` +
                  'built from the rows on this page'
            }
            data-testid="scorecard-csv"
          >
            <Download className="h-3 w-3" aria-hidden />
            download CSV
          </button>
        </div>
        {countryCards.length === 0 && (
          <div
            className="text-ink-3 text-center py-3 text-xs"
            data-testid="scorecard-empty"
          >
            no scorecard computed yet
          </div>
        )}
        {(collapsible && !showAll ? [] : countryCards).map((sc) => {
          const dims = orderedDimensions(sc.dimensions)
          const bandedN = dims.filter(([, d]) => !isInsufficient(d)).length
          return (
            <div
              key={sc.target_id}
              className="bg-surf-2 border border-line rounded p-2 space-y-1.5"
              data-testid={`scorecard-card-${sc.target_id}`}
            >
              <div className="flex items-baseline gap-2">
                <RecordLink
                  kind="target"
                  id={sc.target_id}
                  label={displayName(sc.target_id)}
                  origin="scorecard"
                  className="truncate text-ink-1"
                />
                {/* One rolled-up headline verdict per subject — the most severe
                    banded dimension (insufficient when nothing is banded yet). */}
                {(() => {
                  const tone = countryHeadline(sc)
                  return (
                    <span
                      className={`shrink-0 rounded px-1 text-label font-mono ${TONE_PILL[tone]}`}
                      data-testid={`scorecard-headline-${sc.target_id}`}
                      title={`rolled-up ${subject} verdict (most severe banded dimension)`}
                    >
                      {tone === 'insufficient' ? 'insufficient' : tone}
                    </span>
                  )
                })()}
                <span className="text-ink-3 text-label shrink-0">
                  {bandedN}/{dims.length} banded
                </span>
                {/* H12/K3 — the instrument stamps this card's bands were
                    computed under. The scorecard's bands are a LADDER, not a
                    measure, so the card carries a method revision and no scale
                    of its own: the chip's method-only state is the honest
                    render, and a pre-H12 card reads "unstamped". The
                    generated-at beside it is this card's as-of. */}
                <ScaleStamp
                  stamp={{
                    scale: null,
                    method: sc.method_version ?? null,
                    state: sc.method_version ? 'method-only' : 'unstamped',
                  }}
                  testId={`scorecard-scale-stamp-${sc.target_id}`}
                />
                {sc.generated_at && (
                  <span className="text-ink-3 text-label shrink-0 ml-auto">
                    {relTime(sc.generated_at)}
                  </span>
                )}
              </div>

              <div className="space-y-1">
                {/* Banded dimensions — one row each, drill to verified sub-claims. */}
                {dims
                  .filter(([, dim]) => !isInsufficient(dim))
                  .map(([unit, dim]) => {
                    const bandKey = `${sc.target_id}:${unit}`
                    const open = openBand === bandKey
                    const flagged = dim.eval?.faithfulness_flagged === true
                    return (
                      <div
                        key={unit}
                        className="text-label"
                        data-testid={`scorecard-dim-${sc.target_id}-${unit}`}
                      >
                        <div className="flex items-baseline gap-2">
                          <span className="w-40 shrink-0 truncate text-ink-2" title={unit}>
                            {displayName(unit)}
                          </span>
                          <button
                            type="button"
                            className={`shrink-0 rounded px-1 text-label font-mono ${TONE_PILL[bandTone(dim.band)]}`}
                            onClick={() => setOpenBand(open ? null : bandKey)}
                            data-testid={`scorecard-band-${sc.target_id}-${unit}`}
                            title={`${dim.basis.length} verified sub-claim${dim.basis.length === 1 ? '' : 's'}`}
                          >
                            {dim.band}
                          </button>
                          {dim.effective_confidence !== null && (
                            <span className="text-ink-3 font-mono text-label">
                              eff {dim.effective_confidence.toFixed(2)}
                            </span>
                          )}
                          {flagged && (
                            <span
                              className="text-rose-400 text-label shrink-0"
                              title="aggregate faithfulness below floor"
                            >
                              ⚑ low faithfulness
                            </span>
                          )}
                        </div>

                        {/* Expanded basis — the verified sub-claims this band rests
                            on. Each basis id is a P1 evidence + signed-lineage drill.
                            basis.length===0 never renders a drill target. */}
                        {open && (
                          <div className="mt-1 ml-40 pl-2 border-l border-line space-y-1">
                            {dim.basis.length === 0 ? (
                              <div className="text-ink-3 text-label">no basis sub-claim</div>
                            ) : (
                              dim.basis.map((basisId) => (
                                <div key={basisId} data-testid={`scorecard-basis-${basisId}`}>
                                  <RecordLink
                                    kind="finding"
                                    id={basisId}
                                    label="sub-claim"
                                    origin="scorecard"
                                    className="text-label"
                                  />
                                </div>
                              ))
                            )}
                            <InfoTip
                              text={EVAL_BADGE_EXPLAIN}
                              className={flagged ? 'text-rose-300 text-label' : 'text-ink-3 text-label'}
                              testId={`scorecard-eval-badge-${sc.target_id}-${unit}`}
                            >
                              {evalBadge(dim.eval)}
                            </InfoTip>
                          </div>
                        )}
                      </div>
                    )
                  })}

                {/* The identical "insufficient" rows collapse behind ONE why-drill
                    (they all say the same thing) instead of a wall of red-ish
                    rows — the honest state stays one click away. */}
                {(() => {
                  const insuff = dims.filter(([, dim]) => isInsufficient(dim))
                  if (insuff.length === 0) return null
                  const open = openInsufficient === sc.target_id
                  return (
                    <div
                      className="text-label"
                      data-testid={`scorecard-insufficient-group-${sc.target_id}`}
                    >
                      <button
                        type="button"
                        className="flex w-full items-baseline gap-2 text-left"
                        onClick={() => setOpenInsufficient(open ? null : sc.target_id)}
                        data-testid={`scorecard-insufficient-toggle-${sc.target_id}`}
                        aria-expanded={open}
                      >
                        <span className="rounded bg-surf-3 px-1 text-label text-ink-2">
                          {insuff.length} dimension{insuff.length === 1 ? '' : 's'} insufficient
                        </span>
                        <span className="text-ink-3 text-label">{open ? 'hide' : 'why?'}</span>
                      </button>
                      {open && (
                        <div className="mt-1 ml-2 pl-2 border-l border-line space-y-0.5">
                          {insuff.map(([unit, dim]) => (
                            <div
                              key={unit}
                              className="flex items-baseline gap-2"
                              data-testid={`scorecard-insufficient-${sc.target_id}-${unit}`}
                            >
                              <span
                                className="w-40 shrink-0 truncate text-ink-2"
                                title={unit}
                              >
                                {displayName(unit)}
                              </span>
                              <InfoTip
                                text={
                                  'No band has been computed for this dimension yet — an honest ' +
                                  'absence (nothing measured), not an error. ' +
                                  `Reason: ${insufficientLabel(dim.reason)}.`
                                }
                                className="text-ink-3 text-label"
                                testId={`scorecard-insufficient-reason-${sc.target_id}-${unit}`}
                              >
                                {insufficientLabel(dim.reason)}
                              </InfoTip>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })()}
              </div>

              {/* The P3 composition aggregate node. */}
              <div className="flex items-baseline gap-2 text-label border-t border-line pt-1">
                <span className="w-40 shrink-0 text-ink-3">composition</span>
                {sc.composition.present && sc.composition.basis[0] ? (
                  <RecordLink
                    kind="finding"
                    id={sc.composition.basis[0]}
                    label="composition"
                    origin="scorecard"
                    className="text-label"
                  />
                ) : (
                  <span className="text-ink-3">no verified composition</span>
                )}
              </div>
            </div>
          )
        })}
      </div>
      </>
  )
}
