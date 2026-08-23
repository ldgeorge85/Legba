/**
 * Journal Gate (`system.journal_gate`) — THE HUMAN GATE, made clickable.
 *
 * Journal writes are human-gated by standing rule, and until this panel the
 * gate was API-only: `GET /api/v1/journal_proposals` +
 * `POST .../{id}/accept|reject` existed, no surface consumed them, and the only
 * way to exercise the rule was curl. This is that surface.
 *
 * WHAT IT RENDERS, and why in this order:
 *   1. The queue, `pending` first (the only actionable status), with the other
 *      statuses one chip away so a decision can be re-read afterwards.
 *   2. Per row, THE EFFECT — what accepting would actually apply, in the apply
 *      worker's own vocabulary (`lib/journalGate.ts::proposalEffect`). A
 *      proposal's `diff` is an operation on the substrate/registry, not prose,
 *      so previewing the rationale would preview the wrong thing: the rationale
 *      is the part the operator is meant to be sceptical OF.
 *   3. For `self_revision` — the §7.5(a) objective evidence (the journal's own
 *      calibration + critic record) next to the rationale, and a CLIENT MIRROR
 *      of the §7.5(b) protected-section check so a revision that the server
 *      would auto-reject is visible BEFORE the click, not as a 409 after it.
 *   4. The decision affordances, and then the outcome.
 *
 * HONESTY RULES this panel holds to:
 *   * Accept is two-click ("Accept" → "click again to apply") — it is an
 *     irreversible write through the registry's real update path.
 *   * Reject requires a reason in the box before the button enables; the server
 *     requires `decision_reason` and would 422 an empty one.
 *   * Accept OFFERS a reason (GLASS-3) and does not require one. Until that
 *     train the accept route had no body at all, so the decision trail was
 *     asymmetric by construction: every refusal explained itself and every
 *     APPLIED change — the half that actually mutates the substrate — could not.
 *     It stays optional because the asymmetry in the other direction is real: a
 *     refusal is only legible through its reason, whereas an accept is already
 *     described by the diff it applied, and a mandatory field would fill the
 *     column with "ok". If the apply then fails, the operator's note is carried
 *     into the archived row's reason rather than being overwritten by the
 *     machine's.
 *   * NO optimistic success. The row's status comes from the refetched list,
 *     and every failure renders the server's own detail — a 409 (protected
 *     section auto-reject) and a 422 (apply failed) both say plainly that
 *     NOTHING was applied and the row is archived.
 *   * A `replayed: true` decision says so — the row was already decided and
 *     nothing was re-applied.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown, ChevronRight, ShieldAlert } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { cn } from '@/lib/cn'
import { relativeTime } from '@/lib/findingsViews'
import { selectRow } from '@/state/selection'
import {
  acceptJournalProposal,
  fetchJournalProposals,
  rejectJournalProposal,
} from '@/lib/api'
import type { JournalProposal, ProposalDecision, ProposalStatus } from '@/lib/api'
import {
  decisionErrorText,
  decisionOutcomeText,
  isActionable,
  proposalEffect,
  proposalKindLabel,
  protectedSectionViolations,
  selfRevisionEvidenceSummary,
} from '@/lib/journalGate'
import type { PanelProps } from '@/types'

/** `pending` leads — it is the only status that can still be acted on. */
const STATUS_FILTERS: Array<{ id: ProposalStatus | 'all'; label: string }> = [
  { id: 'pending', label: 'pending' },
  { id: 'accepted', label: 'accepted' },
  { id: 'rejected', label: 'rejected' },
  { id: 'archived', label: 'archived' },
  { id: 'all', label: 'all' },
]

const KIND_TONE: Record<string, string> = {
  correction: 'bg-sky-500/15 text-sky-300 border-sky-500/40',
  change: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
  self_revision: 'bg-rose-500/15 text-rose-300 border-rose-500/40',
}

const STATUS_TONE: Record<string, string> = {
  pending: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
  accepted: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
  rejected: 'bg-rose-500/15 text-rose-300 border-rose-500/40',
  archived: 'bg-surf-1 text-ink-3 border-line',
}

/** Per-row decision UI state. Kept in the panel (not the row) so a refetch that
 *  re-renders the list cannot silently drop a half-typed rejection reason. */
interface RowState {
  expanded: boolean
  confirmingAccept: boolean
  rejecting: boolean
  reason: string
  /** The OPTIONAL accept reason (GLASS-3). Separate from `reason` so switching
   *  between accept and reject cannot leak one decision's text into the other. */
  acceptReason: string
  error: string | null
  outcome: string | null
}

const EMPTY_ROW: RowState = {
  expanded: false,
  confirmingAccept: false,
  rejecting: false,
  reason: '',
  acceptReason: '',
  error: null,
  outcome: null,
}

export default function JournalGatePanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [status, setStatus] = useState<ProposalStatus | 'all'>('pending')
  const [rows, setRows] = useState<Record<string, RowState>>({})

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['journal_proposals', status],
    queryFn: () =>
      fetchJournalProposals(status === 'all' ? { limit: 100 } : { status, limit: 100 }),
    refetchInterval: 60_000,
  })

  function patchRow(id: string, patch: Partial<RowState>) {
    setRows((prev) => ({ ...prev, [id]: { ...(prev[id] ?? EMPTY_ROW), ...patch } }))
  }

  const rowState = (id: string): RowState => rows[id] ?? EMPTY_ROW

  const decide = useMutation<
    ProposalDecision,
    unknown,
    | { id: string; action: 'accept'; reason?: string }
    | { id: string; action: 'reject'; reason: string }
  >({
    mutationFn: (v) =>
      v.action === 'accept'
        ? acceptJournalProposal(v.id, v.reason)
        : rejectJournalProposal(v.id, v.reason),
    onSuccess: (decision, v) => {
      // NOT optimistic: the outcome text is built from what the server
      // actually recorded, and the row's status comes from the refetch.
      patchRow(v.id, {
        confirmingAccept: false,
        rejecting: false,
        reason: '',
        acceptReason: '',
        error: null,
        outcome: decisionOutcomeText(decision),
      })
      void qc.invalidateQueries({ queryKey: ['journal_proposals'] })
    },
    onError: (err, v) => {
      patchRow(v.id, {
        confirmingAccept: false,
        error: decisionErrorText(err),
        outcome: null,
      })
      // A 409/422 leaves the row ARCHIVED server-side — refetch so the list
      // stops showing it as pending.
      void qc.invalidateQueries({ queryKey: ['journal_proposals'] })
    },
  })

  const proposals = data?.proposals ?? []
  const pendingCount = proposals.filter(isActionable).length

  return (
    <PanelChrome
      registration={registration}
      subtitle={
        status === 'pending'
          ? `${pendingCount} awaiting a human decision · the journal suggests, you dispose`
          : `${proposals.length} ${status} · the journal suggests, you dispose`
      }
      onRefresh={() => refetch()}
      actions={
        <div className="flex items-center gap-1" data-testid="journal-gate-filters">
          {STATUS_FILTERS.map((f) => (
            <button
              key={String(f.id)}
              type="button"
              onClick={() => setStatus(f.id)}
              data-testid={`journal-gate-filter-${f.id}`}
              className={cn(
                'rounded border px-2 py-0.5 text-label',
                status === f.id
                  ? 'border-line-strong bg-surf-3 text-ink-1'
                  : 'border-line text-ink-3 hover:text-ink-1',
              )}
            >
              {f.label}
            </button>
          ))}
        </div>
      }
    >
      {isLoading && (
        <div className="text-body text-ink-3" data-testid="journal-gate-loading">
          loading the gate queue…
        </div>
      )}

      {error != null && (
        <div
          className="rounded border border-rose-500/40 bg-rose-500/10 p-2 text-body text-rose-300"
          data-testid="journal-gate-load-error"
        >
          Could not read the proposal queue — {decisionErrorText(error)}
        </div>
      )}

      {!isLoading && error == null && proposals.length === 0 && (
        <div className="text-body text-ink-3" data-testid="journal-gate-empty">
          {status === 'pending'
            ? 'Nothing is waiting on you — the journal has proposed no changes that are still undecided.'
            : `No ${status} proposals.`}
        </div>
      )}

      <div className="space-y-2" data-testid="journal-gate-rows">
        {proposals.map((p) => (
          <ProposalRow
            key={p.id}
            proposal={p}
            state={rowState(p.id)}
            busy={decide.isPending}
            onPatch={(patch) => patchRow(p.id, patch)}
            onAccept={(reason) => decide.mutate({ id: p.id, action: 'accept', reason })}
            onReject={(reason) => decide.mutate({ id: p.id, action: 'reject', reason })}
          />
        ))}
      </div>
    </PanelChrome>
  )
}

function ProposalRow({
  proposal: p,
  state,
  busy,
  onPatch,
  onAccept,
  onReject,
}: {
  proposal: JournalProposal
  state: RowState
  busy: boolean
  onPatch: (patch: Partial<RowState>) => void
  onAccept: (reason?: string) => void
  onReject: (reason: string) => void
}) {
  const effect = proposalEffect(p.proposal_kind, p.diff)
  const actionable = isActionable(p)

  // §7.5(b) client mirror — only meaningful for a self_revision carrying a
  // proposed prompt. `null` means "not applicable", NOT "clean".
  const promptText =
    p.proposal_kind === 'self_revision' && typeof p.diff.new_prompt_text === 'string'
      ? p.diff.new_prompt_text
      : null
  const dropped = promptText != null ? protectedSectionViolations(promptText) : null

  return (
    <div
      className="rounded border border-line bg-surf-1"
      data-testid={`journal-gate-row-${p.id}`}
    >
      <button
        type="button"
        onClick={() => onPatch({ expanded: !state.expanded })}
        className="flex w-full items-start gap-2 px-2 py-1.5 text-left hover:bg-surf-2"
        data-testid={`journal-gate-toggle-${p.id}`}
      >
        {state.expanded ? (
          <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
        ) : (
          <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
        )}
        <span
          className={cn(
            'shrink-0 rounded border px-1.5 py-0.5 text-label',
            KIND_TONE[p.proposal_kind] ?? 'border-line bg-surf-2 text-ink-2',
          )}
          data-testid={`journal-gate-kind-${p.id}`}
        >
          {proposalKindLabel(p.proposal_kind)}
        </span>
        <span className="min-w-0 flex-1 text-body text-ink-1">{effect.summary}</span>
        {dropped != null && dropped.length > 0 && (
          <span
            className="shrink-0 rounded border border-rose-500/40 bg-rose-500/15 px-1.5 py-0.5 text-label text-rose-300"
            title="This revision drops a protected grounding/honesty clause — the server auto-rejects it at accept time."
            data-testid={`journal-gate-protected-flag-${p.id}`}
          >
            would auto-reject
          </span>
        )}
        <span
          className={cn(
            'shrink-0 rounded border px-1.5 py-0.5 text-label',
            STATUS_TONE[p.status] ?? 'border-line bg-surf-2 text-ink-2',
          )}
          data-testid={`journal-gate-status-${p.id}`}
        >
          {p.status}
        </span>
        <span className="shrink-0 text-label text-ink-3">{relativeTime(p.produced_at)}</span>
      </button>

      {state.expanded && (
        <div className="space-y-2 border-t border-line px-2 py-2">
          <Section label="Rationale (what the journal argues)">
            <p className="whitespace-pre-wrap text-body text-ink-2">{p.rationale}</p>
          </Section>

          {p.proposal_kind === 'self_revision' && (
            <Section label="Track record (§7.5a — the objective counterweight)">
              <p
                className="text-body text-amber-300"
                data-testid={`journal-gate-evidence-${p.id}`}
              >
                {selfRevisionEvidenceSummary(p.self_revision_evidence)}
              </p>
            </Section>
          )}

          <Section label={`Effect of accepting · op ${effect.op}`}>
            {effect.unrecognized && (
              <div
                className="mb-1 flex items-start gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 p-1.5 text-body text-amber-300"
                data-testid={`journal-gate-unroutable-${p.id}`}
              >
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                <span>
                  No apply path matches this diff — accepting would fail and archive the
                  row.
                </span>
              </div>
            )}
            <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-body">
              {effect.fields.map((f) => (
                <div key={f.key} className="contents">
                  <dt className="text-ink-3">{f.key}</dt>
                  <dd className="min-w-0 break-words font-mono text-ink-2">{f.value}</dd>
                </div>
              ))}
            </dl>
            {effect.body && (
              <div className="mt-1.5">
                <div className="text-label text-ink-3">{effect.body.label}</div>
                <pre
                  className="mt-0.5 max-h-64 overflow-auto rounded border border-line bg-surf-base p-2 font-mono text-label text-ink-2 whitespace-pre-wrap"
                  data-testid={`journal-gate-body-${p.id}`}
                >
                  {effect.body.text}
                </pre>
              </div>
            )}
          </Section>

          {dropped != null && (
            <Section label="Protected section (§7.5b — checked before you click)">
              {dropped.length === 0 ? (
                <p className="text-body text-emerald-300">
                  Preserves every protected grounding / honesty / anti-self-confirmation
                  clause. The server re-checks at accept time and its answer is the one
                  that counts.
                </p>
              ) : (
                <div
                  className="flex items-start gap-1.5 text-body text-rose-300"
                  data-testid={`journal-gate-protected-${p.id}`}
                >
                  <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                  <span>
                    Drops {dropped.length} protected clause
                    {dropped.length === 1 ? '' : 's'} —{' '}
                    <span className="font-mono">{dropped.join(' · ')}</span>. The server
                    will AUTO-REJECT this at accept time and apply nothing.
                  </span>
                </div>
              )}
            </Section>
          )}

          {p.cited_substrate_refs.length > 0 && (
            <Section label={`Cited substrate (${p.cited_substrate_refs.length})`}>
              <div className="flex flex-wrap gap-1">
                {p.cited_substrate_refs.map((ref) => (
                  <button
                    key={ref}
                    type="button"
                    onClick={() => selectRow('finding', ref, ref, { origin: 'journal_gate' })}
                    className="rounded border border-line bg-surf-2 px-1.5 py-0.5 font-mono text-label text-ink-2 hover:text-ink-1"
                    title="Open this cited row in the Inspector"
                  >
                    {ref.slice(0, 8)}…
                  </button>
                ))}
              </div>
            </Section>
          )}

          <Section label="Provenance">
            <div className="text-label text-ink-3">
              proposed by <span className="font-mono text-ink-2">{p.proposed_by_analyst_id}</span>
              {p.run_id && (
                <>
                  {' '}
                  · run <span className="font-mono text-ink-2">{p.run_id.slice(0, 8)}…</span>
                </>
              )}
              {p.decided_by && (
                <>
                  {' '}
                  · decided by <span className="font-mono text-ink-2">{p.decided_by}</span>
                  {p.decided_at ? ` ${relativeTime(p.decided_at)}` : ''}
                </>
              )}
            </div>
            {/* The decision trail, both halves. Until GLASS-3 only a REJECT could
                carry a reason, so an accepted row was silent about why by
                construction. Now that both can, a decided row with no reason is a
                real (and permitted) state — say so, rather than rendering nothing
                and letting it read as "not applicable". */}
            {p.decided_by && (
              <div
                className="mt-0.5 text-body"
                data-testid={`journal-gate-decision-reason-${p.id}`}
              >
                {p.decision_reason ? (
                  <span className="text-ink-2">reason: “{p.decision_reason}”</span>
                ) : (
                  <span className="text-ink-3">no reason recorded</span>
                )}
              </div>
            )}
          </Section>

          {state.outcome && (
            <div
              className="rounded border border-emerald-500/40 bg-emerald-500/10 p-1.5 text-body text-emerald-300"
              data-testid={`journal-gate-outcome-${p.id}`}
            >
              {state.outcome}
            </div>
          )}

          {state.error && (
            <div
              className="rounded border border-rose-500/40 bg-rose-500/10 p-1.5 text-body text-rose-300"
              data-testid={`journal-gate-error-${p.id}`}
            >
              {state.error}
            </div>
          )}

          {actionable ? (
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    if (!state.confirmingAccept) {
                      onPatch({ confirmingAccept: true, rejecting: false, error: null })
                      return
                    }
                    onAccept(state.acceptReason)
                  }}
                  data-testid={`journal-gate-accept-${p.id}`}
                  className="rounded border border-emerald-500/40 bg-emerald-500/15 px-3 py-1 text-body text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-50"
                >
                  {state.confirmingAccept ? 'click again to apply' : 'Accept'}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    onPatch({
                      rejecting: !state.rejecting,
                      confirmingAccept: false,
                      error: null,
                    })
                  }
                  data-testid={`journal-gate-reject-open-${p.id}`}
                  className="rounded border border-rose-500/40 bg-rose-500/15 px-3 py-1 text-body text-rose-300 hover:bg-rose-500/25 disabled:opacity-50"
                >
                  Reject…
                </button>
                {state.confirmingAccept && (
                  <span className="text-label text-amber-300">
                    this applies the change through the real write path — it is not a
                    dry run
                  </span>
                )}
              </div>

              {/* GLASS-3 — the accept side of the decision trail. OPTIONAL, unlike
                  reject's: a refusal is only legible through its reason, whereas an
                  accept is already described by the diff it applied. It sits between
                  the two clicks so it is offered at the moment the operator is
                  deciding, without becoming another thing to dismiss. */}
              {state.confirmingAccept && (
                <div className="space-y-1">
                  <textarea
                    rows={2}
                    value={state.acceptReason}
                    onChange={(e) => onPatch({ acceptReason: e.target.value })}
                    placeholder="why accept? (optional — recorded on the row, and the only note that survives if the apply then fails)"
                    data-testid={`journal-gate-accept-reason-${p.id}`}
                    className="w-full rounded border border-line bg-surf-base p-1.5 text-body text-ink-1"
                  />
                </div>
              )}

              {state.rejecting && (
                <div className="space-y-1">
                  <textarea
                    rows={2}
                    value={state.reason}
                    onChange={(e) => onPatch({ reason: e.target.value })}
                    placeholder="why is this rejected? (required — the API records it on the row)"
                    data-testid={`journal-gate-reason-${p.id}`}
                    className="w-full rounded border border-line bg-surf-base p-1.5 text-body text-ink-1"
                  />
                  <button
                    type="button"
                    disabled={busy || state.reason.trim().length === 0}
                    onClick={() => onReject(state.reason.trim())}
                    data-testid={`journal-gate-reject-submit-${p.id}`}
                    className="rounded border border-rose-500/40 bg-rose-500/15 px-3 py-1 text-body text-rose-300 hover:bg-rose-500/25 disabled:opacity-40"
                  >
                    Confirm reject
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="text-label text-ink-3" data-testid={`journal-gate-decided-${p.id}`}>
              Already decided ({p.status}) — accept/reject are no longer offered. A
              replayed decision would return the recorded one without re-applying.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-label uppercase tracking-wider text-ink-3">{label}</div>
      <div className="mt-0.5">{children}</div>
    </div>
  )
}
