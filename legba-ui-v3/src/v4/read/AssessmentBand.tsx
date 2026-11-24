/**
 * AssessmentBand — the interpretive channel, clearly a different voice.
 *
 * Operator ruling 1: the Assessment survives from day one as a clearly-labelled
 * channel with a live accuracy badge, generated FROM the assembled spine and
 * graded on fidelity to that spine as its own population (spec §2).
 *
 * ── WHY THE REGISTER SHIFT IS TYPOGRAPHIC ─────────────────────────────────────
 * "Clearly labelled" cannot mean a badge, because a badge is the first thing a
 * daily reader stops seeing. The band is set in a SERIF at a narrower measure on
 * a raised surface with an accent rule down its edge. That difference survives
 * skimming, survives a screenshot, and cannot be mistaken for the record even at
 * a glance — which is the actual requirement. The label and badge are then free
 * to be quiet, because they are confirming what the page already looks like.
 *
 * ── WHY THE BADGE IS ONE LINE AND NOT A GRID ──────────────────────────────────
 * The mock rendered five grades in a coloured row. A grid of numbers reads as a
 * scoreboard, invites comparison between numbers measured on different
 * populations, and buries the one sentence that matters. Spec §2.3 is explicit
 * that the badge's provenance is where a wrong number is worse than no number,
 * so the band renders ONE line, and every number in it carries its population
 * ("graded on the pre-assembly prose tier", "R3, n=77"). A bare number is never
 * shown.
 *
 * ── THE MARKERS PUBLISH WITH THE PROSE ────────────────────────────────────────
 * §2.4: an unsupported-by-spine span is advisory on the row and PROMINENT in the
 * render. The Assessment publishes with its markers, exactly as the journal
 * publishes with its `contradicted_claims` honesty flag — the off-chain
 * privilege granted to the one surface that cannot write a fact into the record.
 * The record itself may never do this (VOICE §3.4 C1/C4), which is why nothing
 * in the record band has an equivalent.
 */
import { useMemo } from 'react'
import type { AssessmentPayload, UnsupportedMark } from '@/lib/assemblyModel'
import { standingSentence } from '@/lib/assemblyModel'
import type { Citation } from '@/lib/citationsModel'
import CitedProse from '@/components/CitedProse'
import type { UnverifiedInfo } from '@/lib/unverifiedState'
import { score } from './readFormat'
import UnverifiedMark from './UnverifiedMark'

/** Superscript letters, so a mark inside prose costs one glyph. */
const MARK_GLYPHS = 'abcdefghijklmnopqrstuvwxyz'

/** The same letters as Unicode modifier glyphs, for the markdown path, where a
 *  `<sup>` cannot be injected into the source (`q` has no BMP form). */
const MARK_GLYPHS_INLINE = 'ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖ*ʳˢᵗᵘᵛʷˣʸᶻ'

/** A `[[ref:N]]` / `[N]` marker, so a glyph is never inserted INSIDE one. */
const MARKER_RE = /\[\[ref:[^\]]*\]\]|\[\d+\]/g

/**
 * X1/D1 — the markdown path's inline marks: the body with each flagged span's
 * letter glyph inserted right after the span, so the prose can go through the
 * workstation's one markdown renderer (`CitedProse`: `**BLUF:**` as bold,
 * `## The reading` as a heading, `[[ref:N]]` as chips) and still carry every
 * mark where it was flagged. Offsets come off the row exactly as `markBody`
 * takes them; an overlapping mark is dropped the same way; an insertion point
 * that would split a citation marker moves to the marker's end.
 */
export function markBodyInline(body: string, marks: UnsupportedMark[]): string {
  const ordered = marks
    .map((m, i) => ({ m, i }))
    .filter(({ m }) => m.char_start >= 0 && m.char_end <= body.length && m.char_end > m.char_start)
    .sort((a, b) => a.m.char_start - b.m.char_start)
  const tokens = [...body.matchAll(MARKER_RE)].map((t) => [t.index ?? 0, (t.index ?? 0) + t[0].length])
  const inserts: { at: number; glyph: string }[] = []
  let cursor = 0
  for (const { m, i } of ordered) {
    if (m.char_start < cursor) continue
    let at = m.char_end
    for (const [a, b] of tokens) if (at > a && at < b) at = b
    inserts.push({ at, glyph: MARK_GLYPHS_INLINE[i] ?? '*' })
    cursor = m.char_end
  }
  let out = body
  for (const ins of inserts.sort((a, b) => b.at - a.at)) {
    out = out.slice(0, ins.at) + ins.glyph + out.slice(ins.at)
  }
  return out
}

const CLASS_COPY: Record<string, string> = {
  rank: 'RANK — a cross-block ordering claim; no record block makes one',
  superlative: 'SUPERLATIVE — unbounded scope over a target-scoped block',
  causal_link: 'CAUSAL LINK — an inferential connective between two ordinals',
  scope_widening: 'SCOPE WIDENING — a collection-scoped negative read as a world negative',
  instrument_prose: 'INSTRUMENT PROSE — the subject is the record’s machinery, not the world',
  aperture_unrostered:
    'APERTURE — names a blind spot the record never declared; the drop ledger names the real ones',
  aperture_guess:
    'APERTURE GUESS — denies a class of subject matter without naming one declared uncarried unit',
  uncited: 'UNCITED — no ordinal at all; unsupported by construction, every sentence names the block it rests on',
  fact: 'FACT — an assertion no record passage states',
}

/**
 * Split the body into text runs and marked runs, by character offset.
 *
 * The offsets come from `data.data.assessment.unsupported[]` so the reader marks
 * the span inline WITHOUT re-parsing the prose — no second detector, no chance
 * of the render and the row disagreeing about which words were flagged.
 */
export function markBody(
  body: string,
  marks: UnsupportedMark[],
): { text: string; mark: UnsupportedMark | null; index: number }[] {
  const ordered = [...marks]
    .filter((m) => m.char_start >= 0 && m.char_end <= body.length && m.char_end > m.char_start)
    .sort((a, b) => a.char_start - b.char_start)
  const out: { text: string; mark: UnsupportedMark | null; index: number }[] = []
  let cursor = 0
  ordered.forEach((m, i) => {
    if (m.char_start < cursor) return // overlapping marks: keep the first, drop the rest
    if (m.char_start > cursor) out.push({ text: body.slice(cursor, m.char_start), mark: null, index: -1 })
    out.push({ text: body.slice(m.char_start, m.char_end), mark: m, index: i })
    cursor = m.char_end
  })
  if (cursor < body.length) out.push({ text: body.slice(cursor), mark: null, index: -1 })
  return out
}

/**
 * Reader words for the notes the backend stamps on the badge (U-1, target brief
 * §5). The stored notes were written for the engine's own ledger ("the
 * PRE-assembly prose tier", "the standing loop … on this population"); a reader
 * is never asked to learn that vocabulary. A note this table does not know
 * passes through unchanged, so a new backend sentence is shown, not lost.
 */
const NOTE_REWRITES: [RegExp, string][] = [
  [
    /graded against the world by human raters on the PRE-assembly prose tier/i,
    'graded against the world by human raters, on an earlier form of this prose',
  ],
  [
    /the standing loop has not decided enough claims(?: on this population)? to report a rate/i,
    'not enough of its claims have been checked yet to report a rate',
  ],
  [/\bDESK SENTENCES\b/g, 'desk sentences'],
]

export function readerNote(note: string): string {
  return NOTE_REWRITES.reduce((acc, [re, to]) => acc.replace(re, to), note)
}

/** A 0–1 rate as a whole percent, the reader's unit for an accuracy. */
function percent(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? `${Math.round(v * 100)} %` : '—'
}

/** One standing sentence, always naming its subject (a stored state note
 *  arrives without one), in reader words. */
function readerStanding(
  standing: Parameters<typeof standingSentence>[0],
  subject: string,
): string {
  const s = standingSentence(standing, `${subject}, graded against the world,`)
  return readerNote(s.startsWith(subject) ? s : `${subject}: ${s}`)
}

/**
 * The badge, in one sentence, with every number carrying its population.
 *
 * U-1: in reader words. The grading round's programme code (lineage + round)
 * and the four-decimal figure sit on hover, never in the line.
 */
function Badge({ payload }: { payload: AssessmentPayload }) {
  const badge = payload.badge
  const fidelity = badge?.fidelity_to_spine ?? payload.fidelity?.score ?? null
  const ext = badge?.external_accuracy ?? null
  return (
    <p className="read-assessment-badge mt-1.5" data-testid="read-assessment-badge">
      {fidelity !== null ? (
        <>
          How faithful this read is to its sources: <b>{score(fidelity)}</b>
          {payload.fidelity ? ` (${payload.fidelity.supported}/${payload.fidelity.checkable} checkable claims)` : ''}
          , measured on this read alone.
        </>
      ) : (
        <>How faithful this read is to its sources is not measured yet.</>
      )}
      {ext && (
        <>
          {' '}
          Claim accuracy, externally graded:{' '}
          <b title={`${ext.lineage} ${ext.round}: ${score(ext.value, 4)}`}>{percent(ext.value)}</b> (n={ext.n},{' '}
          {ext.as_of})
          {badge?.external_accuracy_note ? ` — ${readerNote(badge.external_accuracy_note)}` : ''}.
        </>
      )}
      {/* W-7 — the STANDING pair, record and voice, never summed (F-12). Each
          side renders a SENTENCE, never a bare number: an unmeasured number says
          so in words, and a week whose two grader families disagreed publishes
          the disagreement instead of a rate. The frozen external number above is
          never replaced by these, only joined. */}
      <span data-testid="read-assessment-standing-voice">
        {' '}
        {readerStanding(badge?.standing_accuracy ?? null, 'This read')}.
      </span>
      {payload.spine_badge && (
        <span data-testid="read-assessment-standing-record">
          {' '}
          {readerStanding(payload.spine_badge.standing_accuracy, 'The records it quotes')}
          {payload.spine_badge.standing_note ? ` — ${readerNote(payload.spine_badge.standing_note)}` : ''}.
        </span>
      )}
    </p>
  )
}

export interface AssessmentBandProps {
  /** The Assessment row's prose. */
  body: string
  title: string | null
  payload: AssessmentPayload
  /**
   * What this channel is called in the label line — "Assessment" for the world
   * voice, "Country assessment" for a country's. The HEADING stays "The
   * Assessment" at every tier: the band is one thing, rendered one way, and a
   * reader who learns its look on the world read must recognise it on a desk.
   */
  channelLabel?: string
  /** Ordinals the spine actually carries — an out-of-range marker is a bug. */
  spineOrdinals: number[]
  /** True when this Assessment names a spine other than the read on screen. */
  spineMismatch: boolean
  /**
   * X1/D1 — the Assessment row's own citation list. When given, the body goes
   * through the workstation's one markdown renderer (`CitedProse`): markdown
   * renders, `[[ref:N]]` ordinals become chips, and each flagged span keeps
   * its letter as an inline glyph. Absent → the plain-text path below.
   */
  citations?: Citation[]
  /** X4 READPATH — the Assessment row's own unverified state (no verify
   *  verdict on the voice), or null/absent when it carries one. */
  unverified?: UnverifiedInfo | null
}

export default function AssessmentBand({
  body,
  title,
  payload,
  channelLabel = 'Assessment',
  spineOrdinals,
  spineMismatch,
  citations,
  unverified = null,
}: AssessmentBandProps) {
  const runs = useMemo(() => markBody(body, payload.unsupported), [body, payload.unsupported])
  const marked = useMemo(
    () => (citations ? markBodyInline(body, payload.unsupported) : null),
    [citations, body, payload.unsupported],
  )
  const marks = payload.unsupported
  const strayMarkers = payload.markers.filter((m) => !spineOrdinals.includes(m.ordinal))

  return (
    <section className="read-col mb-8" data-band="assessment" data-testid="read-assessment">
      <div className="read-band-head">
        <h2>The Assessment</h2>
        <div className="read-rule" />
        <span className="read-band-note">interpretive voice &mdash; labelled, graded, not the record</span>
      </div>

      <div className="read-assessment">
        <div className="read-assessment-label" data-testid="read-assessment-label">
          {channelLabel} &middot; written from the record above &middot; its claims are the
          voice&rsquo;s, not the record&rsquo;s
        </div>
        <Badge payload={payload} />
        {unverified && (
          <div className="mt-1.5" data-testid="read-assessment-unverified">
            <UnverifiedMark info={unverified} />
          </div>
        )}

        {spineMismatch && (
          <p className="read-assessment-badge mt-1.5" data-testid="read-assessment-spine-mismatch">
            This Assessment was written from a different run of the record. It is shown so the
            channel is never silently missing, but its ordinals refer to that run.
          </p>
        )}

        {title && (
          <h3 className="mt-3 text-[1.0625rem] font-semibold leading-snug" data-testid="read-assessment-title">
            {title}
          </h3>
        )}

        {marked !== null && citations ? (
          <div className="mt-2" data-testid="read-assessment-body" data-render="markdown">
            <CitedProse text={marked} citations={citations} />
          </div>
        ) : (
        <div className="mt-2 whitespace-pre-wrap" data-testid="read-assessment-body">
          <p>
            {runs.map((r, i) =>
              r.mark ? (
                <a
                  key={i}
                  href={`#read-unsup-${r.index}`}
                  className="read-unsup"
                  data-testid="read-unsupported"
                  data-class={r.mark.class}
                  title={CLASS_COPY[r.mark.class] ?? r.mark.class}
                >
                  {r.text}
                  <sup>{MARK_GLYPHS[r.index] ?? '*'}</sup>
                </a>
              ) : (
                <span key={i}>{r.text}</span>
              ),
            )}
          </p>
        </div>
        )}

        {marks.length > 0 && (
          <ol className="mt-4 space-y-2.5 border-t border-line pt-3" data-testid="read-unsupported-notes">
            {marks.map((m, i) => (
              <li key={i} id={`read-unsup-${i}`} className="read-assessment-badge">
                <span className="font-semibold">{MARK_GLYPHS[i] ?? '*'}.</span>{' '}
                <span style={{ color: 'var(--sev-medium)' }}>
                  {CLASS_COPY[m.class] ?? m.class.toUpperCase()}
                </span>{' '}
                <span className="text-ink-3">({m.detector})</span> &mdash; {m.note}
              </li>
            ))}
          </ol>
        )}

        {strayMarkers.length > 0 && (
          <p className="read-assessment-badge mt-3" data-testid="read-assessment-stray">
            {strayMarkers.length} reference{strayMarkers.length === 1 ? '' : 's'} in this Assessment
            name{strayMarkers.length === 1 ? 's' : ''} a block the record above does not carry. Under
            the contract an out-of-range ordinal is a construction failure, not a soft reason code.
          </p>
        )}

        {payload.lead_test && (
          <p className="read-assessment-badge mt-3 text-ink-3" data-testid="read-assessment-leadtest">
            The concentration verdict was handed to this voice as a fact:{' '}
            {payload.lead_test.earned ? 'earned' : 'not earned'} this cycle &mdash; top-share{' '}
            {score(payload.lead_test.top_share, 3)}, ratio {score(payload.lead_test.ratio_12)}{' '}
            against bars {score(payload.lead_test.bar_share)} / {score(payload.lead_test.bar_ratio)}.
          </p>
        )}
      </div>
    </section>
  )
}

/** What the band renders when the channel is off or has not run for this spine. */
export function AssessmentAbsent({ reason }: { reason: string }) {
  return (
    <section className="read-col mb-8" data-band="assessment" data-testid="read-assessment-absent">
      <div className="read-band-head">
        <h2>The Assessment</h2>
        <div className="read-rule" />
        <span className="read-band-note">interpretive voice</span>
      </div>
      <p className="text-sm leading-relaxed text-ink-3">{reason}</p>
    </section>
  )
}
