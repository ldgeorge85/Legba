/**
 * SubjectReadState — what the Inspector shows FIRST for a subject (X3/D3;
 * review D §C5).
 *
 * A subject focused from the rail or ⌘K used to open the Inspector on its
 * registry DESCRIPTOR. The descriptor is configuration; the analyst came for
 * the READ. So a `target` selection now opens on the read's state:
 *
 *   * LAST READ — the newest row any of the profile's composing frames
 *     published on the subject (`/findings?target_id=&analyst_id_in=<frame
 *     producers>&limit=1`, the subject page's own query), its producer, its
 *     as-of, and its verify line in the route's own terms (faithfulness when
 *     judged, the judge-unavailable line when not, "not judged" when the row
 *     carries no verify block — never a number that was not read);
 *   * ABSENCES — `/v3/absence?scope=` (the Morning Read's GAPS band and the
 *     status bar share the cache key): "carried, unsupported" named on its
 *     own line, then every kind present with its count, every kind the route
 *     could not read as "not measured", and how many kinds were read and came
 *     back empty — with the read's own as-of;
 *   * the subject's page, one click away.
 *
 * The descriptor is rendered by the Inspector under "Configuration", one
 * click further, never first. Absence renders as absence: `—` while a read is
 * in flight, "unavailable" when it failed, never 0.
 */
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { drive } from '@/lib/applyAction'
import { openPanel } from '@/lib/commands'
import {
  ABSENCE_KIND_LABEL,
  ABSENCE_KIND_ORDER,
  notMeasuredReason,
  type AbsenceKind,
  type AbsenceResponse,
} from '@/lib/absenceModel'
import type { ReadFindingRow } from '@/lib/assemblyModel'
import { asOfLabel, compositionIndex } from '@/lib/deskBriefPage'
import { compositionProducers } from '@/lib/subjectPage'
import { humanizeAnalystId } from '@/lib/analystNames'
import { relativeTime } from '@/lib/findingsViews'
import { unverifiedFact, unverifiedLine, type VerificationBlock } from '@/lib/verdictModel'
import { nounTitle, SUBJECT_NOUN, SUBJECT_READ_KIND, SUBJECT_READ_TAB } from '@/lib/subjectNoun'
import { PROFILE_UI } from '@/lib/profileUi'

const PRODUCERS = compositionProducers()
const NOUN = SUBJECT_NOUN.toLowerCase()
const unitsOf = (n: number) =>
  `${n} ${(n === 1 ? PROFILE_UI.unit_noun.singular : PROFILE_UI.unit_noun.plural).toLowerCase()}`

/** The newest composition row on a subject, or null (the subject page's query). */
async function fetchLastRead(targetId: string): Promise<ReadFindingRow | null> {
  if (PRODUCERS.length === 0) return null
  const resp = await apiGet<{ data: ReadFindingRow[] }>(
    `/findings?target_id=${encodeURIComponent(targetId)}` +
      `&analyst_id_in=${encodeURIComponent(PRODUCERS.join(','))}&limit=1&fields=full`,
  )
  return Array.isArray(resp?.data) ? (resp.data[0] ?? null) : null
}

/** The row's verify line in the route's own terms — never an invented score. */
export function verifyLine(v: Record<string, unknown> | null | undefined): string {
  if (!v || typeof v !== 'object') return 'not judged — the row carries no verify block'
  const block = v as VerificationBlock
  const fact = unverifiedFact(block)
  if (fact) return unverifiedLine(fact)
  if (typeof block.faithfulness_score === 'number') {
    const status = typeof block.judge_status === 'string' && block.judge_status ? ` (judge: ${block.judge_status})` : ''
    return `faithfulness ${block.faithfulness_score.toFixed(2)}${status}`
  }
  return 'not judged — the verify block carries no score'
}

export interface AbsenceSummary {
  /** Kinds present, in the route's order, with their counts. */
  present: { kind: AbsenceKind; count: number }[]
  /** Kinds the route could not read for this subject. */
  notMeasured: AbsenceKind[]
  /** Kinds read and found nothing under. */
  readEmpty: number
  /** `carried_unsupported`: a count, or null when not measured. */
  carriedUnsupported: number | null
}

/** Fold one `/v3/absence` answer into what the Inspector prints. */
export function summarizeAbsences(resp: AbsenceResponse): AbsenceSummary {
  const items = Array.isArray(resp.absences) ? resp.absences : []
  const present: AbsenceSummary['present'] = []
  const notMeasured: AbsenceKind[] = []
  let readEmpty = 0
  let carriedUnsupported: number | null = null
  for (const kind of ABSENCE_KIND_ORDER) {
    const count = items.filter((a) => a.kind === kind).length
    const unread = count === 0 && notMeasuredReason(resp, kind) !== null
    if (kind === 'carried_unsupported') carriedUnsupported = unread ? null : count
    if (count > 0) present.push({ kind, count })
    else if (unread) notMeasured.push(kind)
    else readEmpty += 1
  }
  return { present, notMeasured, readEmpty, carriedUnsupported }
}

export default function SubjectReadState({ targetId, label }: { targetId: string; label: string }) {
  const lastRead = useQuery<ReadFindingRow | null>({
    queryKey: ['inspector-subject-last-read', targetId],
    queryFn: () => fetchLastRead(targetId),
    staleTime: 60_000,
  })
  const absence = useQuery<AbsenceResponse>({
    // The GAPS band's and the status bar's key — one read per subject per page load.
    queryKey: ['morning-judgment', 'absence', targetId],
    queryFn: () => apiGet<AbsenceResponse>(`/v3/absence?scope=${encodeURIComponent(targetId)}`),
    staleTime: Infinity,
    retry: false,
  })

  const row = lastRead.data ?? null
  const index = compositionIndex(row, null, NOUN)
  const summary = absence.data ? summarizeAbsences(absence.data) : null

  return (
    <div className="space-y-density" data-testid="inspector-subject-read">
      <section>
        <div className="mb-1 flex items-center gap-2">
          <span className="text-label font-semibold uppercase tracking-wider text-ink-2">Last read</span>
          <button
            type="button"
            data-testid="inspector-subject-open-page"
            onClick={() =>
              drive([openPanel(SUBJECT_READ_KIND, { subject: { kind: 'target', id: targetId }, tab: SUBJECT_READ_TAB })])
            }
            className="ml-auto rounded border border-line px-1.5 py-0.5 text-label text-ink-2 hover:text-ink-1"
            title={`Open ${label}'s ${NOUN} page on its brief`}
          >
            Open the {NOUN} page
          </button>
        </div>
        {lastRead.isLoading ? (
          <div className="text-body text-ink-3" data-testid="inspector-subject-last-read-pending">
            —
          </div>
        ) : lastRead.isError ? (
          <div className="text-body text-ink-3" data-testid="inspector-subject-last-read-error">
            unavailable — the composition read did not answer
          </div>
        ) : !row ? (
          <div className="text-body text-ink-2" data-testid="inspector-subject-last-read-none">
            {index.headline}
          </div>
        ) : (
          <div className="space-y-0.5" data-testid="inspector-subject-last-read">
            <div className="text-body text-ink-1">{index.headline}</div>
            <div className="text-label text-ink-3">
              {/* the producer's name in the profile's noun (one subject word on screen) */}
              {row.analyst_id ? nounTitle(humanizeAnalystId(row.analyst_id)) : 'producer not recorded'} · as of{' '}
              {asOfLabel(row.produced_at) ?? 'not recorded'} ({relativeTime(row.produced_at)})
            </div>
            <div className="text-label text-ink-3" data-testid="inspector-subject-verify">
              {verifyLine(row.verification)}
            </div>
          </div>
        )}
      </section>

      <section data-testid="inspector-subject-absences">
        <div className="mb-1 text-label font-semibold uppercase tracking-wider text-ink-2">
          Typed absences
          {absence.data?.read_at && (
            <span className="ml-2 font-normal normal-case tracking-normal text-ink-3">
              as of {asOfLabel(absence.data.read_at) ?? absence.data.read_at}
            </span>
          )}
        </div>
        {absence.isLoading ? (
          <div className="text-body text-ink-3" data-testid="inspector-subject-absences-pending">
            —
          </div>
        ) : absence.isError || !summary ? (
          <div className="text-body text-ink-3" data-testid="inspector-subject-absences-error">
            unavailable — /v3/absence did not answer for this {NOUN}
          </div>
        ) : (
          <ul className="space-y-0.5 text-body">
            <li className="flex gap-2" data-testid="inspector-subject-carried-unsupported">
              <span className="text-ink-2">{ABSENCE_KIND_LABEL.carried_unsupported}</span>
              <span className="ml-auto tabular-nums text-ink-1">
                {summary.carriedUnsupported == null ? 'not measured' : unitsOf(summary.carriedUnsupported)}
              </span>
            </li>
            {summary.present
              .filter((p) => p.kind !== 'carried_unsupported')
              .map((p) => (
                <li key={p.kind} className="flex gap-2" data-testid={`inspector-subject-absence-${p.kind}`}>
                  <span className="text-ink-2">{ABSENCE_KIND_LABEL[p.kind]}</span>
                  <span className="ml-auto tabular-nums text-ink-1">
                    {p.count} item{p.count === 1 ? '' : 's'}
                  </span>
                </li>
              ))}
            {summary.notMeasured
              .filter((k) => k !== 'carried_unsupported')
              .map((k) => (
                <li key={k} className="flex gap-2 text-ink-3" data-testid={`inspector-subject-absence-${k}`}>
                  <span>{ABSENCE_KIND_LABEL[k]}</span>
                  <span className="ml-auto">not measured</span>
                </li>
              ))}
            <li className="text-label text-ink-3" data-testid="inspector-subject-absences-empty-kinds">
              {summary.readEmpty} kind{summary.readEmpty === 1 ? '' : 's'} read and found nothing
            </li>
          </ul>
        )}
      </section>
    </div>
  )
}
