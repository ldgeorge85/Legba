/**
 * Journal-gate model — the pure layer under the `system.journal_gate` panel.
 *
 * A `journal_proposals` row is NOT a journal entry. Its `diff` is an OPERATION
 * on the substrate or the registry, and accepting it is the human causing that
 * operation through the existing write path (see
 * `src/legba/data/registry/journal_proposals_apply.py`). So "render the
 * proposal as it would publish" means: render WHAT ACCEPTING WOULD DO, in the
 * vocabulary of the apply worker's own dispatch — not a prose preview of the
 * rationale, which is the thing the operator is supposed to be sceptical of.
 *
 * Everything here is pure so the gate's semantics are testable without a DOM:
 *   * `proposalEffect`  — the diff → a structured "this is what accept applies"
 *   * `protectedSectionViolations` — a CLIENT MIRROR of the §7.5(b) auto-reject
 *     check, so a self_revision that would be refused is visible BEFORE the
 *     click rather than as a 409 after it
 *   * `decisionErrorText` — an `ApiError` → the honest operator-facing string
 *
 * The mirror is a preview, never an authority: the server re-runs the same
 * check at accept time and its answer is the one that counts. A drift between
 * the two lists shows up as a proposal the panel calls safe and the server
 * 409s — which the panel still surfaces honestly, because the accept path
 * never optimistically reports success.
 */

import { ApiError } from '@/lib/api'
import type { JournalProposal, ProposalKind, ProposalStatus } from '@/lib/api'

// ---------------------------------------------------------------------------
// §7.5(b) — the PROTECTED SECTION mirror.
//
// Kept VERBATIM in lockstep with `journal_proposals_apply.PROTECTED_PROMPT_PHRASES`.
// A self-revision that drops one of these grounding / honesty / anti-self-
// confirmation clauses is auto-rejected server-side and never applied.
// ---------------------------------------------------------------------------

export const PROTECTED_PROMPT_PHRASES: readonly string[] = [
  // Grounding — the thesis line.
  'poetry without evidence is noise',
  // Citation discipline (provenance).
  '[[ref:',
  // Anti-overclaim about the unproven forecast leg.
  'the forecast pilot has no skill',
  // Temporal honesty — never re-assert retired state.
  'never re-assert',
  // The anti-self-confirmation backstop.
  'never write a fact',
]

/**
 * Which protected phrases a proposed prompt DROPPED. Empty ⇒ the revision
 * preserved every clause (the server would let it through §7.5(b)).
 * Case-insensitive verbatim containment, exactly like the server check.
 */
export function protectedSectionViolations(newPromptText: string): string[] {
  const haystack = (newPromptText ?? '').toLowerCase()
  return PROTECTED_PROMPT_PHRASES.filter((p) => !haystack.includes(p.toLowerCase()))
}

// ---------------------------------------------------------------------------
// diff → what accepting APPLIES
// ---------------------------------------------------------------------------

/** The rendered consequence of accepting one proposal. */
export interface ProposalEffect {
  /** The apply worker's `op` (or `'(none)'` when the diff declares none). */
  op: string
  /** One line: what the operator is about to cause. */
  summary: string
  /** Ordered key/value detail rows pulled out of the diff. */
  fields: Array<{ key: string; value: string }>
  /**
   * A full text body the diff carries (today: a self_revision's proposed
   * system prompt) — rendered verbatim in a scrollable block, because the
   * whole point of the gate is that the operator reads the actual text.
   */
  body?: { label: string; text: string }
  /**
   * True when the diff does not match any op the apply worker dispatches, so
   * accepting would 422 and archive the row. Flagged BEFORE the click.
   */
  unrecognized: boolean
}

function str(diff: Record<string, unknown>, key: string): string {
  const v = diff[key]
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}

function pretty(value: unknown): string {
  if (value == null) return '—'
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/**
 * Describe what accepting this proposal would apply. Mirrors the dispatch in
 * `journal_proposals_apply.apply_accepted_proposal` — including which ops each
 * kind actually recognises, so an unroutable diff is visible as unroutable
 * instead of looking like a normal pending row.
 */
export function proposalEffect(
  kind: ProposalKind,
  diff: Record<string, unknown>,
): ProposalEffect {
  const op = str(diff, 'op').trim()

  if (kind === 'correction') {
    if (op === 'supersede_fact') {
      const subject = str(diff, 'subject')
      const predicate = str(diff, 'predicate')
      const value = str(diff, 'value')
      const complete = Boolean(subject && predicate && value)
      return {
        op,
        summary: complete
          ? `Closes the open fact for “${subject} · ${predicate}” whose value differs from “${value}”. The journal never writes a replacement fact — this retires the stale row only.`
          : 'supersede_fact is INCOMPLETE — it needs subject, predicate and value; accepting would fail and archive the row.',
        fields: [
          { key: 'subject', value: subject || '(missing)' },
          { key: 'predicate', value: predicate || '(missing)' },
          { key: 'corrected value', value: value || '(missing)' },
        ],
        unrecognized: !complete,
      }
    }
    if (op === 'merge_entities' || op === 'correct_situation') {
      return {
        op,
        summary: `Routes to the existing ${
          op === 'merge_entities' ? 'entity-resolution' : 'situation-lifecycle'
        } path. The accept is recorded in the audit trail; the lifecycle subsystem owns the write.`,
        fields: Object.entries(diff)
          .filter(([k]) => k !== 'op')
          .map(([k, v]) => ({ key: k, value: pretty(v) })),
        unrecognized: false,
      }
    }
  }

  if (kind === 'change') {
    if (op === 'update_descriptor') {
      const family = str(diff, 'family')
      const descriptorId = str(diff, 'descriptor_id')
      const patch = diff.patch
      const complete = Boolean(family && descriptorId && patch && typeof patch === 'object')
      return {
        op,
        summary: complete
          ? `Deep-merges this patch into the CURRENT head of ${family}/${descriptorId} and persists a new content-hash version — the same path PUT /descriptors/{family}/{id} uses.`
          : 'update_descriptor is INCOMPLETE — it needs family, descriptor_id and an object patch; accepting would fail and archive the row.',
        fields: [
          { key: 'family', value: family || '(missing)' },
          { key: 'descriptor', value: descriptorId || '(missing)' },
        ],
        body: { label: 'patch (deep-merged into the head)', text: pretty(patch) },
        unrecognized: !complete,
      }
    }
    if (op === 'update_stack') {
      const stackId = str(diff, 'stack_id')
      const patch = diff.patch
      const complete = Boolean(stackId && patch && typeof patch === 'object')
      return {
        op,
        summary: complete
          ? `Deep-merges this patch into stack component ${stackId} and persists a new version — the same path PUT /stack/{id} uses.`
          : 'update_stack is INCOMPLETE — it needs stack_id and an object patch; accepting would fail and archive the row.',
        fields: [{ key: 'stack component', value: stackId || '(missing)' }],
        body: { label: 'patch (deep-merged into the head)', text: pretty(patch) },
        unrecognized: !complete,
      }
    }
  }

  if (kind === 'self_revision') {
    const analystId = str(diff, 'target_analyst_id')
    const promptText = str(diff, 'new_prompt_text')
    const complete = Boolean(analystId && promptText)
    return {
      op: op || 'revise_prompt',
      summary: complete
        ? `Promotes this text as ${analystId}'s LIVE system prompt (a promoted prompt_module_candidate). Its next run reasons under the revised instruction.`
        : 'self_revision is INCOMPLETE — it needs target_analyst_id and new_prompt_text; accepting would fail and archive the row.',
      fields: [
        { key: 'target analyst', value: analystId || '(missing)' },
        ...(str(diff, 'summary')
          ? [{ key: 'proposed summary', value: str(diff, 'summary') }]
          : []),
      ],
      body: complete
        ? { label: 'proposed system prompt (the text that would go live)', text: promptText }
        : undefined,
      unrecognized: !complete,
    }
  }

  // Either an unknown proposal_kind or an op this kind does not dispatch.
  return {
    op: op || '(none)',
    summary: `No apply path matches proposal_kind “${kind}”${
      op ? ` with op “${op}”` : ' (the diff declares no op)'
    }. Accepting would fail and archive the row with the error.`,
    fields: Object.entries(diff).map(([k, v]) => ({ key: k, value: pretty(v) })),
    unrecognized: true,
  }
}

// ---------------------------------------------------------------------------
// Presentation helpers
// ---------------------------------------------------------------------------

const KIND_LABELS: Record<string, string> = {
  correction: 'correction',
  change: 'change',
  self_revision: 'self-revision',
}

export function proposalKindLabel(kind: ProposalKind): string {
  return KIND_LABELS[kind] ?? kind
}

/** Only `pending` rows are actionable — everything else is already decided. */
export function isActionable(p: JournalProposal): boolean {
  return p.status === 'pending'
}

/**
 * A one-line reading of the §7.5(a) evidence. Deliberately unflattering where
 * the record is thin: this is the counterweight to a persuasive rationale.
 */
export function selfRevisionEvidenceSummary(
  ev: JournalProposal['self_revision_evidence'],
): string {
  if (!ev || !ev.available) {
    return 'No calibration record — the journal has not earned a track record to weigh this against.'
  }
  const parts: string[] = []
  parts.push(
    ev.forecast_unproven
      ? 'forecast skill UNPROVEN'
      : `forecast skill positive (BSS ${ev.brier_skill_score?.toFixed(3) ?? '—'})`,
  )
  if (ev.calibration_thin) parts.push('exogenous calibration THIN')
  parts.push(
    ev.journal_critic_n > 0
      ? `journal critic mean ${ev.journal_critic_mean?.toFixed(2) ?? '—'} over n=${ev.journal_critic_n} (30d)`
      : 'no critic scores on the journal in 30d',
  )
  return parts.join(' · ')
}

/**
 * The honest operator-facing text for a failed decision. The two interesting
 * statuses are named explicitly because they mean specific things:
 *   409 → §7.5(b) protected-section AUTO-REJECT (nothing was applied; archived)
 *   422 → the apply itself failed (nothing was applied; archived)
 * Everything else surfaces the server's own detail rather than a euphemism.
 */
export function decisionErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body
    let detail = ''
    if (typeof body === 'string') detail = body
    else if (body && typeof body === 'object' && 'detail' in body) {
      const d = (body as { detail: unknown }).detail
      detail = typeof d === 'string' ? d : JSON.stringify(d)
    }
    if (err.status === 409) {
      return `Auto-rejected (§7.5(b) protected section) — NOTHING was applied and the proposal is archived. ${detail}`.trim()
    }
    if (err.status === 422) {
      return `Apply failed — NOTHING was applied and the proposal is archived. ${detail}`.trim()
    }
    if (err.status === 404) {
      return 'Proposal not found — it may have been removed since this list was loaded.'
    }
    return `HTTP ${err.status}${detail ? ` — ${detail}` : ''}`
  }
  return String((err as Error)?.message ?? err)
}

/**
 * The outcome line for a SUCCESSFUL decision. A `replayed` decision is called
 * out as such: the row was already decided, so this click changed nothing and
 * (critically) did not re-apply.
 */
export function decisionOutcomeText(d: {
  status: ProposalStatus
  replayed: boolean
  applied: Record<string, unknown> | null
  decision_reason: string | null
}): string {
  if (d.replayed) {
    return `Already decided (${d.status}) — replayed, so nothing was re-applied.`
  }
  if (d.status === 'accepted') {
    const op = d.applied && typeof d.applied.op === 'string' ? d.applied.op : null
    return op ? `Accepted and applied (${op}).` : 'Accepted and applied.'
  }
  if (d.status === 'rejected') {
    return `Rejected${d.decision_reason ? ` — “${d.decision_reason}”` : ''}.`
  }
  return `Recorded as ${d.status}.`
}
