/**
 * Voices (`system.journal`) — Legba's reflective voices, filtered + grouped
 * (VOICES_PANEL_SPEC.md, step 1 — the incremental path §4).
 *
 * Reads `GET /api/v1/journal` (summary-weight for the list, `fields=summary`
 * §3.3) + `GET /api/v1/journal/{id}` (full weight, on row-select — the reader
 * pane's fetch) + the open consolidation + the substrate `calibration` verdict.
 * Polled every 60s. The panel surfaces:
 *
 *  - A FILTER RAIL (§2a) — kind chips (Journal / Consolidation / Chronicle,
 *    plus any `lens`/`lens_diff` kinds actually present once LV-1 lands) with
 *    a live row count + a verify-score pill, multi-select / union semantics,
 *    all-selected by default.
 *  - The LATEST CONSOLIDATION prominently, above the list — "Legba's current
 *    inner landscape" (the single open `entry_kind='consolidation'` row) —
 *    ALWAYS fetched regardless of chip state (a "jump to" shortcut, not a
 *    true filter, §2a); the Consolidation chip toggles only its local
 *    visibility.
 *  - A GROUPED-BY-CYCLE collapsed list (§2b) replacing the old unbounded
 *    scroll — rows bucketed by `period_end` date, newest cycle expanded, an
 *    entry_kind priority order within a group, a diary-row reveal cap.
 *  - A READER PANE (§2c) — selecting a row fetches the full row and reuses
 *    `EntryCard` + `ClaimRow` + `HonestyBanner` wholesale (they already render
 *    any `entry_kind` generically). When the row's `verify_body` names
 *    contested spans (`[judge_contradicted]` / `[judge_unsupported]`), a
 *    compact per-claim VERDICT BLOCK renders them as flagged chips — the
 *    operator's window into what the verify pass actually disputed.
 *  - A DIFF TAB shell (§2d) — permanent empty state pre-LV-1.
 *  - PER-CLAIM PROVENANCE CHIPS — unchanged from the prior cut: each
 *    `claims[].refs` ref renders as a `ProvenanceChip` deep-linking via the
 *    shared `selectRow`.
 *  - The "UNVERIFIED PERSPECTIVE" visual style — a `[needs_citation]`-prefixed
 *    span or a `kind="perspective"` claim renders dashed/amber, visible and
 *    NEVER hidden (§4.5) — the grounding-honesty surface.
 *  - The HONESTY BANNER — driven by `calibration`, cross-checked against the
 *    open consolidation's stored `honesty_flags`.
 *
 * Reuse: `ProvenanceChip` (@/v4/components), the shared selection store
 * (@/state/selection), the dark markdown map (@/v4/why/WorldAssessment), the
 * standard `apiGet`-backed fetch (@/lib/api → fetchJournalSummary /
 * fetchJournalEntry), and `PanelChrome`.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, BookOpen, ChevronDown, ChevronRight } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import ProvenanceChip from '@/v4/components/ProvenanceChip'
import CitedProse from '@/components/CitedProse'
import { cn } from '@/lib/cn'
import { selectRow } from '@/state/selection'
import { fetchJournalSummary, fetchJournalEntry } from '@/lib/api'
import type {
  JournalEntry,
  JournalEntryKind,
  JournalEntrySummary,
  JournalClaim,
  JournalRef,
  JournalCalibration,
} from '@/lib/api'
import type { ProvenanceRef } from '@/v4/why/types'
import type { PanelProps } from '@/types'

const NEEDS_CITATION_PREFIX = '[needs_citation]'

/** Human-readable, non-alarmist labels for the deterministic honesty flags. */
const HONESTY_FLAG_LABELS: Record<string, string> = {
  forecast_unproven:
    'Forecast skill is unproven — the acute-forecast pilot has not earned positive skill (BSS not yet > 0).',
  calibration_thin:
    'Exogenous calibration is thin — too few resolved exogenous outcomes to calibrate against reality.',
}

function flagLabel(flag: string): string {
  return HONESTY_FLAG_LABELS[flag] ?? flag
}

/** Project a resolved journal ref onto the shared `ProvenanceRef` chip contract.
 *  `kind` flows straight through — the chip palette colours known substrate
 *  kinds and falls back to slate for `nexus`/`fact`/`unknown`. */
function refToChip(ref: JournalRef): ProvenanceRef {
  return {
    kind: ref.kind as ProvenanceRef['kind'],
    id: ref.id,
    label: ref.title ?? ref.id,
  }
}

/** Drive the shared selection from a chip — opens the Inspector + brushes the
 *  other rooms. `selectRow` coerces an unknown/non-first-class kind to a
 *  walkable Inspector path, so a click is never a dead-end (§9). */
function openRef(ref: JournalRef): void {
  selectRow(ref.kind, ref.id, ref.title ?? undefined, { origin: 'journal' })
}

/** An explicit speculation / perspective / self-instrument marker family the
 *  server deliberately KEEPS (never strips) — appendix §5.2. Mirrors
 *  `journal_assessor.py`'s `_SPECULATION_RE`. */
const SPECULATION_MARKER_RE =
  /\[\[(spec|speculation|perspective|wonder|inference|unverified|instrument)\]\]/gi

/** Normalize inline body markers for the human-readable markdown render
 *  (appendix §5.2, renamed from `stripRefMarkers` now that it does more than
 *  strip refs):
 *   - `[[ref:<uuid>]]` markers are STRIPPED — the chip binding lives in
 *     `claims`, so an inline marker is redundant noise in the prose.
 *   - the `_SPECULATION_RE` family (`[[instrument]]` and its siblings) is
 *     REPLACED with a small inline tag (rendered as a pill below) rather than
 *     left as literal brackets or silently deleted — deleting would hide the
 *     honesty signal the server deliberately preserved. */
function normalizeMarkers(body: string): string {
  return body
    .replace(/\[\[ref:[0-9a-fA-F-]+\]\]/g, '')
    .replace(SPECULATION_MARKER_RE, (_m, tag: string) => `{{spec:${tag.toLowerCase()}}}`)
    .replace(/[ \t]{2,}/g, ' ')
}

/** Split normalized body text on the `{{spec:<tag>}}` sentinel `normalizeMarkers`
 *  emits, so the render can interleave prose (through `CitedProse`) with a
 *  small pill for each speculation/instrument marker in its original position. */
function splitOnSpeculationTags(text: string): Array<{ text: string } | { tag: string }> {
  const parts: Array<{ text: string } | { tag: string }> = []
  const re = /\{\{spec:([a-z]+)\}\}/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push({ text: text.slice(last, m.index) })
    parts.push({ tag: m[1] })
    last = re.lastIndex
  }
  if (last < text.length) parts.push({ text: text.slice(last) })
  return parts
}

/** One `[[instrument]]`-family inline pill — reuses `ClaimRow`'s amber/slate
 *  unverified palette for visual family consistency (appendix §5.2). */
function SpeculationTag({ tag }: { tag: string }) {
  return (
    <span
      className="mx-0.5 inline-flex items-center gap-0.5 rounded border border-dashed border-amber-700/60 bg-amber-950/20 px-1 align-middle text-[10px] leading-none text-amber-300/90"
      title={`self-${tag} read — no citable substrate row (kept, never hidden)`}
      data-testid="journal-speculation-tag"
      data-tag={tag}
    >
      ⚙ {tag}
    </span>
  )
}

/** A single cited claim row — its span (styled per kind) + its chips. */
function ClaimRow({ claim }: { claim: JournalClaim }) {
  const span = claim.text_span
  const needsCitation = span.startsWith(NEEDS_CITATION_PREFIX)
  // An uncited assertion (`[needs_citation]`) OR a perspective span is the
  // "unverified perspective" style: visible, distinct, never hidden (§4.5).
  const unverified = needsCitation || claim.kind === 'perspective'
  const display = needsCitation ? span.slice(NEEDS_CITATION_PREFIX.length).trim() : span

  return (
    <div
      className={
        unverified
          ? 'rounded border border-dashed border-amber-700/60 bg-amber-950/20 p-2'
          : 'rounded border border-line bg-surf-1 p-2'
      }
      data-testid="journal-claim"
      data-claim-kind={claim.kind}
      data-needs-citation={needsCitation ? 'true' : undefined}
    >
      <div className="flex items-start gap-2">
        {unverified && (
          <span
            className="shrink-0 rounded px-1 text-[10px] uppercase tracking-wide bg-amber-900/60 text-amber-200"
            title={
              needsCitation
                ? 'Uncited factual span — flagged, shown verbatim, never hidden (§4.5)'
                : 'Perspective / inference — the voice, not a cited fact'
            }
          >
            {needsCitation ? 'uncited' : 'perspective'}
          </span>
        )}
        <p
          className={
            unverified
              ? 'text-sm leading-relaxed text-amber-100/90 italic'
              : 'text-sm leading-relaxed text-slate-200'
          }
        >
          {display}
        </p>
      </div>
      {claim.refs.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5" data-testid="journal-claim-chips">
          {claim.refs.map((ref) => (
            <ProvenanceChip
              key={ref.id}
              refItem={refToChip(ref)}
              onClick={() => openRef(ref)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// §3 of the task: the per-claim verdict block, parsed from `verify_body`.
// ---------------------------------------------------------------------------

interface VerdictLine {
  reason: string
  text: string
}

/** Parse the critique body's `  - [reason] text` lines
 *  (`verify.build_faithfulness_critique_payload`'s exact format) into
 *  structured verdict rows. Only the judge/no-citation reasons that name a
 *  DISPUTED span are surfaced here — `double_counted` / `hedge_laundering`
 *  are structural/advisory notes, not a contradicted or unsupported claim. */
const VERDICT_LINE_RE = /^\s*-\s*\[([a-z_]+)\]\s*(.+)$/
const DISPUTED_REASONS = new Set([
  'judge_contradicted',
  'judge_unsupported',
  'no_citation',
  'unresolved_citation',
])

function parseVerdictLines(verifyBody: string | null): VerdictLine[] {
  if (!verifyBody) return []
  const out: VerdictLine[] = []
  for (const raw of verifyBody.split('\n')) {
    const m = VERDICT_LINE_RE.exec(raw)
    if (!m) continue
    const reason = m[1]
    if (!DISPUTED_REASONS.has(reason)) continue
    out.push({ reason, text: m[2].trim() })
  }
  return out
}

/** red for a judge-graded contradiction (the strongest signal — the model
 *  actively disagreed), amber for everything else disputed (unsupported /
 *  uncited / unresolved). */
function verdictChipClass(reason: string): string {
  return reason === 'judge_contradicted'
    ? 'border-rose-700/60 bg-rose-950/30 text-rose-200'
    : 'border-amber-700/60 bg-amber-950/20 text-amber-200'
}

/** The compact per-claim verdict block (task §2 / spec §3.4) — the operator's
 *  window into what the faithfulness verify pass actually disputed. Renders
 *  ONLY when `verify_body` names at least one disputed span; silent
 *  otherwise (a clean verify carries no adverse lines to show). */
function VerdictBlock({ verifyBody }: { verifyBody: string | null }) {
  const lines = useMemo(() => parseVerdictLines(verifyBody), [verifyBody])
  if (lines.length === 0) return null
  return (
    <div className="mb-3 rounded border border-line bg-surf-1 p-2" data-testid="journal-verdict-block">
      <div className="mb-1.5 flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-slate-500">
        <AlertTriangle className="h-3 w-3 shrink-0 text-amber-400" aria-hidden />
        verify pass flagged {lines.length} {lines.length === 1 ? 'span' : 'spans'}
      </div>
      <div className="space-y-1">
        {lines.map((l, i) => (
          <div key={i} className="flex items-start gap-2" data-testid="journal-verdict-line" data-reason={l.reason}>
            <span
              className={cn(
                'mt-0.5 shrink-0 rounded border px-1 py-0.5 text-[9px] uppercase tracking-wide',
                verdictChipClass(l.reason),
              )}
              title={l.reason}
            >
              {l.reason.replace(/_/g, ' ')}
            </span>
            <p className="text-[12px] leading-snug text-slate-300">{l.text}</p>
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The card — reused wholesale for the prominent consolidation slot AND the
// reader pane (§2c).
// ---------------------------------------------------------------------------

/** One journal entry / consolidation card — title, period, honesty pills, the
 *  verdict block (when disputed spans exist), the narrative body, and the
 *  per-claim cited spans with chips. */
function EntryCard({
  entry,
  prominent = false,
}: {
  entry: JournalEntry
  prominent?: boolean
}) {
  const bodyText = useMemo(() => normalizeMarkers(entry.body), [entry.body])
  const bodyParts = useMemo(() => splitOnSpeculationTags(bodyText), [bodyText])
  const period = `${new Date(entry.period_start).toLocaleString()} → ${new Date(
    entry.period_end,
  ).toLocaleString()}`

  return (
    <article
      className={
        prominent
          ? 'rounded-lg border border-emerald-800/50 bg-surf-1 p-4'
          : 'rounded-lg border border-line bg-surf-2 p-3'
      }
      data-testid={`journal-entry-${entry.id}`}
      data-entry-kind={entry.entry_kind}
    >
      <header className="mb-2 flex flex-wrap items-center gap-2">
        {prominent && (
          <span className="rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide bg-emerald-900/60 text-emerald-200">
            current inner landscape
          </span>
        )}
        <h3
          className={
            prominent
              ? 'text-base font-semibold text-slate-100'
              : 'text-sm font-semibold text-slate-200'
          }
        >
          {entry.title}
        </h3>
        <VerifyScorePill score={entry.verify_score} />
        <span className="ml-auto shrink-0 text-[11px] text-slate-500" title={period}>
          {new Date(entry.produced_at).toLocaleString()}
        </span>
      </header>

      <div className="mb-2 text-[11px] text-slate-500">reflecting on {period}</div>

      {/* Per-entry honesty pills — the deterministic flags forced from the live
          calibration metric at write time (§10). */}
      {entry.honesty_flags.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5" data-testid="journal-entry-honesty">
          {entry.honesty_flags.map((flag) => (
            <span
              key={flag}
              className="rounded px-1.5 py-0.5 text-[10px] bg-amber-950/40 text-amber-300 border border-amber-800/50"
              title={flagLabel(flag)}
            >
              {flag}
            </span>
          ))}
        </div>
      )}

      {/* The per-claim verdict block (§3.4 / task §2) — flagged contested
          spans from the verify critique, when any exist. */}
      <VerdictBlock verifyBody={entry.verify_body} />

      {/* The narrative — rendered through the shared CitedProse (markdown
          always rendered, never raw), with `[[ref:...]]` markers stripped
          (the chip binding lives in the claims sidecar below) and the
          `_SPECULATION_RE` family (`[[instrument]]` etc.) rendered as small
          inline tags rather than literal brackets (appendix §5.2). */}
      {bodyText.trim() && (
        <div className="mb-3 text-sm" data-testid="journal-entry-body">
          {bodyParts.map((part, i) =>
            'tag' in part ? (
              <SpeculationTag key={i} tag={part.tag} />
            ) : part.text.trim() ? (
              <CitedProse key={i} text={part.text} citations={[]} />
            ) : null,
          )}
        </div>
      )}

      {/* Per-claim cited spans + provenance chips — the "every claim a chip"
          promise + the visible unverified-perspective distinction (§9). */}
      {entry.claims.length > 0 && (
        <div className="space-y-1.5" data-testid="journal-entry-claims">
          <div className="text-[10px] uppercase tracking-wide text-slate-500">
            claims &amp; provenance
          </div>
          {entry.claims.map((claim, i) => (
            <ClaimRow key={`${entry.id}-claim-${i}`} claim={claim} />
          ))}
        </div>
      )}
    </article>
  )
}

/**
 * The honesty banner (§9 / §10). Keyed off the substrate-derived `calibration`
 * verdict the route returns (the live metric), and cross-checked against the
 * current consolidation's stored `honesty_flags` (which were themselves forced
 * deterministically from that metric). The banner is NEVER green-washed: the
 * unproven legs are stated plainly so the journal can't read as more mature than
 * the substrate warrants.
 */
function HonestyBanner({
  calibration,
  consolidation,
}: {
  calibration: JournalCalibration
  consolidation: JournalEntrySummary | null
}) {
  // The live verdict drives the banner; the stored flags are the cross-check.
  const liveFlags: string[] = []
  if (calibration.forecast_unproven) liveFlags.push('forecast_unproven')
  if (calibration.calibration_thin) liveFlags.push('calibration_thin')

  const storedFlags = consolidation?.honesty_flags ?? []
  // A divergence between what the substrate says now and what the open
  // consolidation recorded is itself worth surfacing (stale consolidation).
  const drift = liveFlags.filter((f) => !storedFlags.includes(f))

  if (liveFlags.length === 0) {
    return (
      <div
        className="mb-3 rounded border border-emerald-800/40 bg-emerald-950/20 px-3 py-2 text-xs text-emerald-200"
        data-testid="journal-honesty-banner"
        data-banner-state="clear"
      >
        Calibration posture: both legs currently pass (forecast skill earned + exogenous
        calibration sufficient).
        {!calibration.available && ' (no calibration finding computed yet — read defaulted clear)'}
      </div>
    )
  }

  return (
    <div
      className="mb-3 rounded border border-amber-800/50 bg-amber-950/30 px-3 py-2 text-xs text-amber-200"
      data-testid="journal-honesty-banner"
      data-banner-state="flagged"
    >
      <div className="flex items-center gap-1.5 font-medium">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden />
        Honest posture — built but unproven
        {!calibration.available && (
          <span className="font-normal text-amber-300/80">
            (no calibration finding yet — conservatively flagged)
          </span>
        )}
      </div>
      <ul className="mt-1 list-disc space-y-0.5 pl-5">
        {liveFlags.map((flag) => (
          <li key={flag}>{flagLabel(flag)}</li>
        ))}
      </ul>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-amber-300/80">
        {calibration.brier_skill_score != null && (
          <span>BSS {calibration.brier_skill_score.toFixed(3)}</span>
        )}
        {calibration.forecast_acute_sample_size != null && (
          <span>acute n={calibration.forecast_acute_sample_size}</span>
        )}
        {calibration.exogenous_sample_size != null && (
          <span>exogenous n={calibration.exogenous_sample_size}</span>
        )}
        {calibration.forecast_acute_status && (
          <span>status: {calibration.forecast_acute_status}</span>
        )}
      </div>
      {drift.length > 0 && (
        <div className="mt-1.5 text-[11px] text-amber-400">
          ⚠ the open consolidation omits {drift.join(', ')} that the live metric now flags — its
          posture may be stale.
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// §2a — the filter rail (kind chips w/ count + verify-score pill).
// ---------------------------------------------------------------------------

/** Fixed display order + label for the always-present kinds (§2a); any other
 *  kind actually present (a future `lens`/`lens_diff` id, or an unforeseen
 *  kind) is appended after these in first-seen order — "generated from the
 *  distinct kinds present," never hardcoded to a roster. */
const FIXED_KIND_ORDER: JournalEntryKind[] = ['entry', 'consolidation', 'chronicle']
const KIND_LABELS: Record<string, string> = {
  entry: 'Journal',
  consolidation: 'Consolidation',
  chronicle: 'Chronicle',
  lens: 'Lens',
  lens_diff: 'Lens diff',
}

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind
}

/** emerald ≥0.7 / amber <0.7 / slate if absent — reuses HonestyBanner's
 *  palette family (§2a). */
function scoreTone(score: number | null): 'good' | 'warn' | 'none' {
  if (score == null) return 'none'
  return score >= 0.7 ? 'good' : 'warn'
}

function VerifyScorePill({ score }: { score: number | null }) {
  const tone = scoreTone(score)
  return (
    <span
      className={cn(
        'shrink-0 rounded px-1.5 py-0.5 text-[10px] font-mono leading-none',
        tone === 'good' && 'bg-emerald-950/40 text-emerald-300 border border-emerald-800/50',
        tone === 'warn' && 'bg-amber-950/40 text-amber-300 border border-amber-800/50',
        tone === 'none' && 'bg-surf-2 text-slate-500 border border-line',
      )}
      title={
        score == null
          ? 'No faithfulness-verify critique yet for this row'
          : `Faithfulness verify score ${score.toFixed(2)}`
      }
      data-testid="journal-verify-score-pill"
    >
      {score == null ? '—' : score.toFixed(2)}
    </span>
  )
}

interface KindChipInfo {
  kind: JournalEntryKind
  count: number
  /** The most recent row's verify score for this kind (§2a). */
  latestScore: number | null
}

function computeKindChips(rows: JournalEntrySummary[]): KindChipInfo[] {
  const byKind = new Map<JournalEntryKind, JournalEntrySummary[]>()
  for (const r of rows) {
    const list = byKind.get(r.entry_kind) ?? []
    list.push(r)
    byKind.set(r.entry_kind, list)
  }
  // Fixed kinds are ALWAYS present (even with 0 rows loaded) — Journal /
  // Consolidation / Chronicle chips never disappear (§2a). Any other kind
  // present in the loaded window (a future lens/lens_diff id) is appended,
  // first-seen order — never hardcoded to a roster.
  const order: JournalEntryKind[] = [...FIXED_KIND_ORDER]
  for (const k of byKind.keys()) {
    if (!order.includes(k)) order.push(k)
  }
  return order.map((kind) => {
    const list = byKind.get(kind) ?? []
    // Rows arrive newest-first (produced_at DESC) from the API — the first
    // row with a non-null score is "the most recent row's verify score."
    const latest = list.find((r) => r.verify_score != null)
    return { kind, count: list.length, latestScore: latest?.verify_score ?? null }
  })
}

function FilterRail({
  chips,
  selected,
  onToggle,
}: {
  chips: KindChipInfo[]
  selected: Set<JournalEntryKind>
  onToggle: (kind: JournalEntryKind) => void
}) {
  return (
    <div
      className="mb-3 flex flex-wrap items-center gap-1.5 border-b border-line pb-2"
      data-testid="journal-filter-rail"
    >
      {chips.map(({ kind, count, latestScore }) => {
        const active = selected.has(kind)
        return (
          <button
            key={kind}
            type="button"
            onClick={() => onToggle(kind)}
            data-testid="journal-kind-chip"
            data-kind={kind}
            data-active={active ? 'true' : 'false'}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-full border px-2 py-1 text-[11px] leading-none transition-colors',
              active
                ? 'border-slate-600 bg-surf-2 text-slate-100'
                : 'border-line bg-surf-1 text-slate-500 opacity-60 hover:opacity-100',
            )}
          >
            <span aria-hidden>{active ? '✓' : ''}</span>
            {kindLabel(kind)}
            <span className="rounded bg-surf-3 px-1 font-mono text-[10px] text-slate-400">
              {count}
            </span>
            <VerifyScorePill score={latestScore} />
          </button>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// §2b — grouped-by-cycle collapsed list.
// ---------------------------------------------------------------------------

/** `entry_kind` priority within a cycle group — synthesized rows lead,
 *  high-volume diary rows trail (§2b). */
const KIND_PRIORITY: Record<string, number> = {
  consolidation: 0,
  chronicle: 1,
  lens_diff: 2,
  lens: 3,
  entry: 4,
}

function kindPriority(kind: string): number {
  return KIND_PRIORITY[kind] ?? 5
}

/** `period_end` date-bucketed to `YYYY-MM-DD` — the grouping key (§2b): when
 *  the reflected-on window closes, not write time (which can lag under
 *  retry/backoff). */
function cycleBucket(periodEnd: string): string {
  const d = new Date(periodEnd)
  if (Number.isNaN(d.getTime())) return periodEnd.slice(0, 10)
  return d.toISOString().slice(0, 10)
}

interface CycleGroup {
  bucket: string
  rows: JournalEntrySummary[]
}

function groupByCycle(rows: JournalEntrySummary[]): CycleGroup[] {
  const byBucket = new Map<string, JournalEntrySummary[]>()
  for (const r of rows) {
    const b = cycleBucket(r.period_end)
    const list = byBucket.get(b) ?? []
    list.push(r)
    byBucket.set(b, list)
  }
  const groups: CycleGroup[] = Array.from(byBucket.entries()).map(([bucket, list]) => {
    const sorted = [...list].sort((a, b) => {
      const pa = kindPriority(a.entry_kind)
      const pb = kindPriority(b.entry_kind)
      if (pa !== pb) return pa - pb
      return new Date(b.produced_at).getTime() - new Date(a.produced_at).getTime()
    })
    return { bucket, rows: sorted }
  })
  // Groups descending by period_end (newest first).
  groups.sort((a, b) => (a.bucket < b.bucket ? 1 : a.bucket > b.bucket ? -1 : 0))
  return groups
}

/** A week can carry ~14 diary entries against 1 chronicle + 1 consolidation;
 *  cap the initial reveal so expansion doesn't bury the synthesized rows. */
const DIARY_REVEAL_CAP = 5

function CycleRow({
  row,
  active,
  onSelect,
}: {
  row: JournalEntrySummary
  active: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      data-testid="journal-cycle-row"
      data-entry-kind={row.entry_kind}
      aria-current={active ? 'true' : undefined}
      className={cn(
        'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] transition-colors',
        active ? 'bg-surf-3 ring-1 ring-accent-info' : 'hover:bg-surf-2',
      )}
    >
      <span className="shrink-0 rounded bg-surf-3 px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-slate-400">
        {kindLabel(row.entry_kind)}
      </span>
      <span className="min-w-0 flex-1 truncate text-slate-200">{row.title}</span>
      <VerifyScorePill score={row.verify_score} />
      <span className="shrink-0 text-[10px] text-slate-500">
        {new Date(row.produced_at).toLocaleDateString(undefined, { month: '2-digit', day: '2-digit' })}
      </span>
    </button>
  )
}

function CycleGroupSection({
  group,
  expanded,
  onToggleExpanded,
  selectedId,
  onSelectRow,
}: {
  group: CycleGroup
  expanded: boolean
  onToggleExpanded: () => void
  selectedId: string | null
  onSelectRow: (row: JournalEntrySummary) => void
}) {
  const [revealAll, setRevealAll] = useState(false)

  // Synthesized rows (consolidation/chronicle/lens*) are never capped — only
  // the high-volume `entry` kind is, per §2b.
  const synth = group.rows.filter((r) => r.entry_kind !== 'entry')
  const diary = group.rows.filter((r) => r.entry_kind === 'entry')
  const visibleDiary = revealAll ? diary : diary.slice(0, DIARY_REVEAL_CAP)
  const hiddenCount = diary.length - visibleDiary.length

  return (
    <div className="mb-1.5" data-testid="journal-cycle-group" data-bucket={group.bucket}>
      <button
        type="button"
        onClick={onToggleExpanded}
        className="flex w-full items-center gap-1.5 rounded px-1 py-1 text-left text-[11px] font-medium uppercase tracking-wide text-slate-400 hover:text-slate-200"
        data-testid="journal-cycle-header"
      >
        {expanded ? (
          <ChevronDown className="h-3 w-3 shrink-0" aria-hidden />
        ) : (
          <ChevronRight className="h-3 w-3 shrink-0" aria-hidden />
        )}
        Cycle — week of {group.bucket}
        <span className="font-mono text-[10px] text-slate-600">({group.rows.length})</span>
      </button>
      {expanded && (
        <div className="space-y-0.5 pl-1 pt-0.5">
          {synth.map((row) => (
            <CycleRow
              key={row.id}
              row={row}
              active={row.id === selectedId}
              onSelect={() => onSelectRow(row)}
            />
          ))}
          {visibleDiary.map((row) => (
            <CycleRow
              key={row.id}
              row={row}
              active={row.id === selectedId}
              onSelect={() => onSelectRow(row)}
            />
          ))}
          {hiddenCount > 0 && (
            <button
              type="button"
              onClick={() => setRevealAll(true)}
              className="w-full rounded px-2 py-1 text-left text-[11px] text-slate-500 hover:text-slate-300"
              data-testid="journal-cycle-reveal-more"
            >
              … {hiddenCount} more
            </button>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// §2d — the Diff tab shell (permanent empty state, pre-LV-1).
// ---------------------------------------------------------------------------

function DiffTabEmptyState() {
  return (
    <div
      className="rounded border border-dashed border-line bg-surf-1 px-3 py-4 text-center text-[11px] text-slate-500"
      data-testid="journal-diff-tab-empty"
    >
      No diff for this cycle yet — the Diff tab activates once a faculty-lens
      cycle (LV-1) lands a comparison row.
    </div>
  )
}

// ---------------------------------------------------------------------------
// §2c — the reader pane.
// ---------------------------------------------------------------------------

type ReaderTab = 'entry' | 'diff'

function ReaderPane({ entryId }: { entryId: string }) {
  const [tab, setTab] = useState<ReaderTab>('entry')
  const query = useQuery({
    queryKey: ['journal-entry', entryId],
    queryFn: () => fetchJournalEntry(entryId),
  })

  return (
    <div className="flex h-full flex-col overflow-hidden" data-testid="journal-reader-pane">
      <div className="mb-2 flex shrink-0 gap-1 border-b border-line pb-1.5">
        <button
          type="button"
          onClick={() => setTab('entry')}
          data-testid="journal-reader-tab-entry"
          className={cn(
            'rounded px-2 py-1 text-[11px] uppercase tracking-wide',
            tab === 'entry' ? 'bg-surf-3 text-slate-100' : 'text-slate-500 hover:text-slate-300',
          )}
        >
          Entry
        </button>
        <button
          type="button"
          onClick={() => setTab('diff')}
          data-testid="journal-reader-tab-diff"
          className={cn(
            'rounded px-2 py-1 text-[11px] uppercase tracking-wide',
            tab === 'diff' ? 'bg-surf-3 text-slate-100' : 'text-slate-500 hover:text-slate-300',
          )}
        >
          Diff
        </button>
      </div>
      <div className="flex-1 overflow-auto">
        {tab === 'diff' ? (
          <DiffTabEmptyState />
        ) : query.isLoading ? (
          <div className="text-sm text-slate-500">loading entry…</div>
        ) : query.error ? (
          <div className="text-sm text-rose-400">
            error: {query.error instanceof Error ? query.error.message : String(query.error)}
          </div>
        ) : query.data ? (
          <EntryCard entry={query.data} />
        ) : null}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Top-level panel.
// ---------------------------------------------------------------------------

const LIST_PAGE_LIMIT = 200

export default function JournalPanel({ registration }: PanelProps) {
  const [appended, setAppended] = useState<JournalEntrySummary[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [selected, setSelected] = useState<Set<JournalEntryKind> | null>(null)
  const [expandedBuckets, setExpandedBuckets] = useState<Set<string> | null>(null)
  const [selectedRowId, setSelectedRowId] = useState<string | null>(null)
  const [showConsolidation, setShowConsolidation] = useState(true)

  const query = useQuery({
    queryKey: ['journal-summary'],
    queryFn: async () => {
      // Always request the consolidation slot too (§2a: the chip is a
      // "jump to" shortcut, not a true filter — hiding it is a pure
      // client-side render toggle, never a re-fetch) alongside the default
      // stream kinds. Once real chip-driven kind filtering is wired to a
      // re-fetch (a possible follow-up), this still fetches the full
      // superset so the initial chip counts are accurate.
      const r = await fetchJournalSummary({
        limit: LIST_PAGE_LIMIT,
        kind: ['entry', 'consolidation', 'chronicle', 'lens', 'lens_diff'],
      })
      setAppended([])
      setNextCursor(r.next_cursor)
      return r
    },
    refetchInterval: 60_000,
  })

  const allRows = useMemo(() => {
    const page = query.data?.entries ?? []
    const seen = new Set<string>()
    const out: JournalEntrySummary[] = []
    for (const e of [...page, ...appended]) {
      if (seen.has(e.id)) continue
      seen.add(e.id)
      out.push(e)
    }
    return out
  }, [query.data?.entries, appended])

  // The grouped list only ever shows stream kinds — the consolidation stays
  // a separate prominent slot, never folded into a cycle group (§2c).
  const streamRows = useMemo(() => allRows.filter((r) => r.entry_kind !== 'consolidation'), [allRows])

  const kindChips = useMemo(() => computeKindChips(allRows), [allRows])
  const activeSelection = selected ?? new Set(kindChips.map((c) => c.kind))

  const filteredStreamRows = useMemo(
    () => streamRows.filter((r) => activeSelection.has(r.entry_kind)),
    [streamRows, activeSelection],
  )

  const groups = useMemo(() => groupByCycle(filteredStreamRows), [filteredStreamRows])
  const activeExpanded =
    expandedBuckets ?? new Set(groups.length > 0 ? [groups[0].bucket] : [])

  function toggleKind(kind: JournalEntryKind) {
    if (kind === 'consolidation') {
      setShowConsolidation((v) => !v)
      return
    }
    const next = new Set(activeSelection)
    if (next.has(kind)) next.delete(kind)
    else next.add(kind)
    setSelected(next)
  }

  function toggleBucket(bucket: string) {
    const next = new Set(activeExpanded)
    if (next.has(bucket)) next.delete(bucket)
    else next.add(bucket)
    setExpandedBuckets(next)
  }

  async function loadMore() {
    if (!nextCursor || loadingMore) return
    setLoadingMore(true)
    try {
      const next = await fetchJournalSummary({
        limit: LIST_PAGE_LIMIT,
        cursor: nextCursor,
        kind: ['entry', 'consolidation', 'chronicle', 'lens', 'lens_diff'],
      })
      setAppended((prev) => [...prev, ...next.entries])
      setNextCursor(next.next_cursor)
    } finally {
      setLoadingMore(false)
    }
  }

  const consolidationSummary = useMemo(
    () => allRows.find((r) => r.entry_kind === 'consolidation') ?? null,
    [allRows],
  )
  const consolidationQuery = useQuery({
    queryKey: ['journal-entry', consolidationSummary?.id],
    queryFn: () => fetchJournalEntry(consolidationSummary!.id),
    enabled: consolidationSummary != null,
  })
  const calibration = query.data?.calibration

  const error = query.error instanceof Error ? query.error : null

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${allRows.length} ${allRows.length === 1 ? 'row' : 'rows'} loaded`}
      onRefresh={() => query.refetch()}
    >
      <div className="flex h-full flex-col overflow-hidden" data-testid="journal-panel">
        {query.isLoading && <div className="p-2 text-sm text-slate-500">loading journal…</div>}
        {error && <div className="p-2 text-sm text-rose-400">error: {error.message}</div>}

        {!query.isLoading && (
          <div className="flex-1 overflow-hidden px-2 pt-2">
            {calibration && (
              <HonestyBanner calibration={calibration} consolidation={consolidationSummary} />
            )}

            <FilterRail
              chips={kindChips}
              selected={
                new Set([...activeSelection, ...(showConsolidation ? ['consolidation' as const] : [])])
              }
              onToggle={toggleKind}
            />

            {showConsolidation &&
              (consolidationSummary ? (
                <div className="mb-4">
                  {consolidationQuery.data ? (
                    <EntryCard entry={consolidationQuery.data} prominent />
                  ) : (
                    <div className="rounded-lg border border-emerald-800/50 bg-surf-1 p-4 text-xs text-slate-500">
                      loading current inner landscape…
                    </div>
                  )}
                </div>
              ) : (
                <div className="mb-4 rounded border border-line bg-surf-1 px-3 py-2 text-xs text-slate-500">
                  <BookOpen className="mr-1.5 inline h-3.5 w-3.5" aria-hidden />
                  No consolidation yet — the daily consolidation tier opens the first one once
                  enough entries accumulate.
                </div>
              ))}

            <div className="grid h-[calc(100%-1rem)] grid-cols-1 gap-3 overflow-hidden md:grid-cols-2">
              {/* Grouped-by-cycle collapsed list (§2b) — replaces the old
                  unbounded scroll. */}
              <div className="overflow-auto" data-testid="journal-cycle-list">
                {groups.length === 0 ? (
                  <div className="py-6 text-center text-sm text-slate-500">
                    {allRows.length === 0
                      ? 'The journal has not written anything yet.'
                      : 'No rows match the current filter.'}
                  </div>
                ) : (
                  groups.map((group) => (
                    <CycleGroupSection
                      key={group.bucket}
                      group={group}
                      expanded={activeExpanded.has(group.bucket)}
                      onToggleExpanded={() => toggleBucket(group.bucket)}
                      selectedId={selectedRowId}
                      onSelectRow={(row) => setSelectedRowId(row.id)}
                    />
                  ))
                )}

                {nextCursor && (
                  <div className="mt-3 border-t border-line pt-2">
                    <button
                      onClick={loadMore}
                      disabled={loadingMore}
                      className="w-full rounded border border-line bg-surf-1 p-1 text-xs hover:bg-surf-2 disabled:opacity-50"
                      data-testid="journal-load-more"
                    >
                      {loadingMore ? 'loading…' : 'load more entries'}
                    </button>
                  </div>
                )}
              </div>

              {/* Reader pane (§2c) — reuses EntryCard/ClaimRow/HonestyBanner
                  wholesale, fetches the full row on selection. */}
              <div className="overflow-hidden rounded border border-line bg-surf-1 p-2">
                {selectedRowId ? (
                  <ReaderPane entryId={selectedRowId} />
                ) : (
                  <div className="flex h-full items-center justify-center text-center text-[12px] text-slate-500">
                    Select a row to read it here.
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </PanelChrome>
  )
}
