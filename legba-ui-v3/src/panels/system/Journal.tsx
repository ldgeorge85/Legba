/**
 * Journal (`system.journal`) — Legba's first-person reflective voice + the
 * navigable INDEX over the product (JOURNAL_ASSESSOR_PLAN §9, Wave 3).
 *
 * Reads `GET /api/v1/journal` (the open consolidation + a paged stream of recent
 * entries; every cited ref resolved server-side to its (kind, title)). The panel
 * surfaces:
 *
 *  - The LATEST CONSOLIDATION prominently — "Legba's current inner landscape"
 *    (the single open `entry_kind='consolidation'` row).
 *  - A scrollable stream of recent ENTRIES below it.
 *  - PER-CLAIM PROVENANCE CHIPS — each `claims[].refs` ref renders as a
 *    `ProvenanceChip` bound to its specific cited span (NOT a footnote pile) and
 *    deep-links to the cited situation / assessment / nexus / fact via the
 *    shared `selectRow` (opens the Inspector + brushes the other rooms). The
 *    chip walk is UP-only, from the entry's in-payload refs (§3.5) — reusing the
 *    exact mechanism The Why's lineage trail uses.
 *  - The "UNVERIFIED PERSPECTIVE" visual style — a `[needs_citation]`-prefixed
 *    span (an uncited factual assertion that slipped the REFLECT flag) and a
 *    `kind="perspective"` span render in a distinct dashed/amber style, visible
 *    and NEVER hidden. This IS the grounding-honesty surface (§4.5) — the real
 *    enforcement is the visible distinction, not an LLM stripper.
 *  - The HONESTY BANNER — driven by the entry's deterministic `honesty_flags`
 *    (forced from the live calibration metric at write time, §10) AND
 *    cross-checked against the substrate-derived `calibration` verdict the route
 *    returns, so the banner is keyed off substrate metrics, not a self-reported
 *    field.
 *
 * Reuse: `ProvenanceChip` (@/v4/components), the shared selection store
 * (@/state/selection), the dark markdown map (@/v4/why/WorldAssessment), the
 * standard `apiGet`-backed fetch (@/lib/api → fetchJournal), and `PanelChrome`.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { AlertTriangle, BookOpen } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import ProvenanceChip from '@/v4/components/ProvenanceChip'
import { MD_COMPONENTS } from '@/v4/why/WorldAssessment'
import { selectRow } from '@/state/selection'
import { fetchJournal } from '@/lib/api'
import type { JournalEntry, JournalClaim, JournalRef, JournalCalibration } from '@/lib/api'
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

/** Strip inline `[[ref:<uuid>]]` citation markers from the body for the
 *  human-readable markdown render — the chip BINDING lives in `claims`, so the
 *  markers are redundant noise in the prose (§3.6). */
function stripRefMarkers(body: string): string {
  return body.replace(/\[\[ref:[0-9a-fA-F-]+\]\]/g, '').replace(/[ \t]{2,}/g, ' ')
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

/** One journal entry / consolidation card — title, period, honesty pills, the
 *  narrative body, and the per-claim cited spans with chips. */
function EntryCard({
  entry,
  prominent = false,
}: {
  entry: JournalEntry
  prominent?: boolean
}) {
  const bodyText = useMemo(() => stripRefMarkers(entry.body), [entry.body])
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

      {/* The narrative — markdown, ref markers stripped (the chip binding lives
          in the claims sidecar, rendered below). */}
      {bodyText.trim() && (
        <div className="mb-3 text-sm" data-testid="journal-entry-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
            {bodyText}
          </ReactMarkdown>
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
  consolidation: JournalEntry | null
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

export default function JournalPanel({ registration }: PanelProps) {
  const [appended, setAppended] = useState<JournalEntry[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)

  const query = useQuery({
    queryKey: ['journal'],
    queryFn: async () => {
      const r = await fetchJournal({ limit: 25 })
      setAppended([])
      setNextCursor(r.next_cursor)
      return r
    },
    refetchInterval: 60_000,
  })

  const entries = useMemo(() => {
    const page = query.data?.entries ?? []
    const seen = new Set<string>()
    const out: JournalEntry[] = []
    for (const e of [...page, ...appended]) {
      if (seen.has(e.id)) continue
      seen.add(e.id)
      out.push(e)
    }
    return out
  }, [query.data?.entries, appended])

  async function loadMore() {
    if (!nextCursor || loadingMore) return
    setLoadingMore(true)
    try {
      const next = await fetchJournal({ limit: 25, cursor: nextCursor })
      setAppended((prev) => [...prev, ...next.entries])
      setNextCursor(next.next_cursor)
    } finally {
      setLoadingMore(false)
    }
  }

  const consolidation = query.data?.consolidation ?? null
  const calibration = query.data?.calibration

  const error = query.error instanceof Error ? query.error : null

  return (
    <PanelChrome
      registration={registration}
      subtitle={
        consolidation
          ? `current landscape · ${entries.length} recent ${
              entries.length === 1 ? 'entry' : 'entries'
            }`
          : `${entries.length} ${entries.length === 1 ? 'entry' : 'entries'}`
      }
      onRefresh={() => query.refetch()}
    >
      <div className="flex-1 overflow-auto" data-testid="journal-panel">
        {query.isLoading && <div className="text-sm text-slate-500">loading journal…</div>}
        {error && <div className="text-sm text-rose-400">error: {error.message}</div>}

        {calibration && (
          <HonestyBanner calibration={calibration} consolidation={consolidation} />
        )}

        {consolidation ? (
          <div className="mb-4">
            <EntryCard entry={consolidation} prominent />
          </div>
        ) : (
          !query.isLoading && (
            <div className="mb-4 rounded border border-line bg-surf-1 px-3 py-2 text-xs text-slate-500">
              <BookOpen className="mr-1.5 inline h-3.5 w-3.5" aria-hidden />
              No consolidation yet — the daily consolidation tier opens the first one once
              enough entries accumulate.
            </div>
          )
        )}

        {entries.length > 0 && (
          <>
            <div className="mb-2 text-[10px] uppercase tracking-wide text-slate-500">
              recent entries
            </div>
            <div className="space-y-3" data-testid="journal-entry-stream">
              {entries.map((entry) => (
                <EntryCard key={entry.id} entry={entry} />
              ))}
            </div>
          </>
        )}

        {entries.length === 0 && !consolidation && !query.isLoading && !error && (
          <div className="py-6 text-center text-sm text-slate-500">
            The journal has not written anything yet.
          </div>
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
    </PanelChrome>
  )
}
