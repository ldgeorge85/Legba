/**
 * claimVerdicts — per-citation-chip verify verdicts (P1-8 / P2-4), pure + DOM-free.
 *
 * WHAT THE PAYLOAD ACTUALLY CARRIES (src/legba/data/provenance/verify.py,
 * `FaithfulnessReport.as_dict`): the finding-level `verification` block records
 * the POOLED score (`faithfulness_score`, `checkable_claims`,
 * `supported_claims`, `judge_status`) plus `unsupported_spans` — one entry per
 * FLAGGED claim: `{text, reason, markers}`, where `markers` lists the citation
 * ordinals the flagged claim carried (unit `[N]` signal indices, or
 * composition `[[ref:N]]` sub-claim ordinals).
 *
 * P2-4 additionally persists `claim_verdicts` — the FULL per-claim ledger
 * (`{text, markers, verdict: 'supported'|'hard_fail'|'soft_fail', reason}`),
 * one row per GRADED claim including supported ones (previously recorded
 * nowhere — the citation-hover finding this module now closes). Advisory-only
 * flags (`hedge_laundering` / `double_counted`) are deliberately NOT ledger
 * rows — they annotate a claim that is itself recorded (typically supported)
 * — so they still live ONLY in `unsupported_spans`.
 *
 * So the honest per-chip verdict, worst-to-best:
 *   * a span with this chip's ordinal in its `markers` → that claim's flag
 *     (contradicted / unsupported / hedge-laundering / …), with the flagged
 *     claim text to show — checked FIRST so an advisory flag is never
 *     shadowed by a ledger row that (correctly) also marks the same claim
 *     `supported`;
 *   * no flagged span names this chip, but the persisted `claim_verdicts`
 *     LEDGER names it `supported` → "supported" — a real, backed verdict,
 *     never fabricated (this is the case the ledger newly makes possible);
 *   * no flagged span, no ledger row (ledger absent — a pre-P2-4 critique —
 *     or present but silent on this ordinal) while the LLM judge RAN → "not
 *     flagged" (the legacy, vaguer fallback: every flagged checkable claim is
 *     named, so absence is meaningful, but we cannot say "supported" without
 *     the ledger);
 *   * the judge did not run (deterministic floor only) or there is no verify
 *     block → "claim-level verdict not recorded" — stated, never fabricated.
 */

// ---------------------------------------------------------------------------
// Reason vocabulary — superset of claimsModel's why-not labels, covering every
// reason `UnsupportedSpan.reason` can carry (verify.py docstring).
// ---------------------------------------------------------------------------

export const CLAIM_REASON_LABELS: Record<string, string> = {
  no_citation: 'no citation',
  unresolved_citation: 'citation resolves to nothing',
  judge_unsupported: 'unsupported by the verify judge',
  judge_contradicted: 'contradicted by the verify judge',
  double_counted: 'double-counted evidence (cited sub-claims share lineage)',
  hedge_laundering: 'asserts more confidence than the cited sub-claim',
  indicator_uncited_triggered: 'triggered indicator carries no citation',
  stale_leader: 'stale-cutoff officeholder reference',
  cross_target_leak: 'names only other countries than its desk target',
  off_subject: 'is about a different place that shares its desk subject code (a homonym)',
  unhedged_periphery_citation: 'rests on periphery evidence without a hedge',
  unscoped_absence_claim: 'states an absence without scoping it to collection',
  // Q4 (2026-09-28) — the CARRIED DATED ANCHOR. A date or figure asserted on
  // this desk's own memory (prior read / window ledger) and on no cited
  // signal. The reader has to be told WHICH kind of unsupported this is:
  // nothing was fabricated at write time, the sentence simply stopped being
  // checked, which is why it gets more confident every cycle it survives.
  carried_unsupported:
    'asserts a date or figure carried from the prior read / window ledger, not re-sourced from a signal this window',
  // C-1 — a clause resting only on CLAIM rows (a party's own claim) that
  // states the claimed event instead of the claim.
  claim_as_fact:
    'states as fact what its only source is a party claiming (a claim supports only "X claimed Y")',
  metadata_mismatch: 'misquotes its own recorded confidence/tier value',
  judge_contradicted_unquoted: 'judge contradiction lacked a verbatim quote (demoted to soft)',
  absence_slice_contradicted: 'scoped absence contradicted by an item in the full input slice',
  absence_slice_unresolved: 'scoped-absence check could not resolve a verdict',
  // F-5 (W1) — the rest of verify._FAIL_CLASS_BY_REASON, so the table above
  // is the superset it claims to be (a Python test pins the two together).
  // Facts / register / world-knowledge flags.
  stale_leader_vs_facts: 'officeholder named differs from the current facts-table officeholder',
  register_self_corroboration: 'rests only on the open-situation register, stated as corroboration',
  uncited_world_knowledge: 'states a historical or structural baseline no cited row contains',
  // Judge demotions: the judge flagged the claim but its refutation did not hold.
  judge_contradicted_unrefuted: 'judge contradiction restates the claim or quotes the prior read (demoted to soft)',
  judge_prior_read_conflict: 'judge quote comes from a finding the claim never cited, usually the prior read (soft)',
  judge_contradicted_off_scope: 'judge quote evidences something the claim did not deny (demoted to soft)',
  judge_quote_confirms_claim: 'judge quote states the claim\'s own numbers back to it (demoted to soft)',
  judge_contradicted_machine_row: 'judge quote is a machine-coded event record, not testimony (demoted to soft)',
  judge_contradicted_route_excluded: 'judge contradiction on a claim already routed out of slice checking (soft)',
  judge_contradicted_hedged_conflict: 'judge contradiction is the weak side the claim itself disclosed (soft)',
  judge_nonpropositional_unearned: 'judge declined to grade a span that does assert a checkable particular',
  // Composition checks.
  buried_lead_salience: 'leads on an input much less consequential than the top one',
  unsurfaced_input_contradiction: 'inputs disagree and the composition merged them without saying so',
  absence_scope_laundered: 'a desk-scoped absence republished as a world-wide one',
  attribution_direction_conflict: 'attributed to a desk whose own verdict says the opposite',
  attribution_asserts_desk_negative: 'asserts presence of something the cited desk recorded as absent',
  attribution_ungrounded_quote: 'quotes a phrase the named desk never wrote',
  // Assembly arms (record-vs-source checks on a generated payload).
  quote_not_contained: 'quoted text does not occur in its origin body',
  quote_offset_mismatch: 'quoted text is real but not at the recorded offsets',
  quote_origin_drift: 'quote is anchored to a body other than the one it names',
  quote_origin_truncated: 'quote reaches past the captured evidence text',
  scope_truncated: 'drops the collection bound its source sentence carried',
  scope_widened: 'declares a collection bound its source sentence does not have',
  attribution_desk_mismatch: 'attributed desk is not the producer of the cited head',
  attribution_date_mismatch: 'attribution date differs from the cited head\'s timestamp',
  attribution_target_mismatch: 'attributed target differs from the cited head\'s target',
  attribution_head_unresolved: 'cited head resolves to nothing, so the block cannot be traced',
  coverage_unit_missing: 'a declared unit has no coverage row',
  coverage_unit_duplicated: 'a unit has two coverage rows',
  coverage_status_invalid: 'coverage status is outside the allowed set',
  drop_count_mismatch: 'the drop ledger\'s counts do not add up',
  drop_why_unknown: 'a drop carries a reason outside the allowed set',
  drop_order_violation: 'a dropped candidate ranks above a carried block',
  drop_floor_violation: 'the selection floor\'s ledger does not add up',
}

export function claimReasonLabel(reason: string): string {
  return CLAIM_REASON_LABELS[reason] ?? reason.replace(/_/g, ' ')
}

/**
 * U-5 — the plain-language gloss for the two honest-ABSENCE verdict kinds
 * ('not-recorded' / 'not-checked'). A hostile UX review found these strings
 * read like errors to a first-time reader ("verdict not recorded" sounds
 * broken); this says plainly that nothing was measured yet, never that
 * something failed.
 */
export const CLAIM_VERDICT_ABSENCE_EXPLAIN =
  'Nothing was checked at this per-claim granularity yet — an honest absence, ' +
  'not a hidden failure. (The pooled faithfulness score above still covers the ' +
  'whole read; this line is about THIS ONE citation specifically.)'

/** Extract the ordinal from a citation marker (`[8]` → 8, `[[ref:3]]` → 3).
 *  Null when the marker carries no digits (legacy uuid markers). */
export function markerOrdinal(marker: string): number | null {
  const m = /(\d+)/.exec(marker)
  return m ? Number(m[1]) : null
}

// ---------------------------------------------------------------------------
// Verdict derivation
// ---------------------------------------------------------------------------

export type ClaimVerdictKind =
  /** A judge_contradicted span names this chip's ordinal. */
  | 'contradicted'
  /** A judge_unsupported span names this chip's ordinal. */
  | 'unsupported'
  /** A non-judge flag (hedge_laundering, double_counted, …) names it. */
  | 'flagged'
  /** P2-4 — the persisted claim_verdicts LEDGER names this chip `supported`
   *  (no flag names it, and the ledger recorded a real per-claim verdict). */
  | 'supported'
  /** LLM judge ran; no flagged claim names this chip, and either no ledger
   *  was persisted for this critique or it says nothing about this ordinal. */
  | 'not-flagged'
  /** Only the deterministic floor ran — no per-claim judge verdict exists. */
  | 'not-checked'
  /** No verification block at all. */
  | 'not-recorded'

export interface ClaimVerdictSpan {
  /** The flagged claim's text (the judge's per-claim subject). */
  text: string
  reason: string
  reasonLabel: string
}

export interface ClaimVerdict {
  kind: ClaimVerdictKind
  /** The one-line verdict for the hover card. */
  label: string
  /** Flagged claim spans naming this chip's ordinal (empty unless flagged). */
  spans: ClaimVerdictSpan[]
  judgeStatus: string | null
  /** Pooled context when recorded (`checkable_claims` / `supported_claims`). */
  checkable: number | null
  supported: number | null
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function intOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** Severity order within a chip's matched flags — worst first. */
const REASON_SEVERITY: Record<string, number> = {
  judge_contradicted: 0,
  judge_unsupported: 1,
}

function spanNamesOrdinal(rawMarkers: unknown, ordinal: number): boolean {
  if (!Array.isArray(rawMarkers)) return false
  return rawMarkers.some((m) => {
    if (typeof m === 'number') return m === ordinal
    if (typeof m === 'string') return Number(m) === ordinal
    return false
  })
}

// ---------------------------------------------------------------------------
// P2-4 — the persisted per-claim `claim_verdicts` LEDGER (supported +
// hard_fail + soft_fail rows; verify.py `ClaimVerdict.as_dict`). Consulted
// ONLY when no span already flags this chip's ordinal — the ledger never
// overrides a real flag (including the advisory ones it deliberately excludes
// from its own rows), it only upgrades the previously-vague "not flagged"
// silence into a backed "supported" when the ledger actually says so.
// ---------------------------------------------------------------------------

type LedgerVerdict = 'supported' | 'hard_fail' | 'soft_fail'

function isLedgerVerdict(v: unknown): v is LedgerVerdict {
  return v === 'supported' || v === 'hard_fail' || v === 'soft_fail'
}

interface LedgerRowMatch {
  text: string
  verdict: LedgerVerdict
  reason: string | null
}

/** Worst-first ordering for ledger rows that (defensively) both name the same
 *  ordinal — mirrors REASON_SEVERITY's "hard beats soft" precedent. */
const LEDGER_SEVERITY: Record<LedgerVerdict, number> = {
  hard_fail: 0,
  soft_fail: 1,
  supported: 2,
}

function ledgerRowsNamingOrdinal(
  v: Record<string, unknown>,
  ordinal: number,
): LedgerRowMatch[] {
  const raw = v['claim_verdicts']
  if (!Array.isArray(raw)) return []
  const out: LedgerRowMatch[] = []
  for (const item of raw) {
    const o = asRecord(item)
    if (!o) continue
    const verdict = o['verdict']
    if (!isLedgerVerdict(verdict)) continue
    if (!spanNamesOrdinal(o['markers'], ordinal)) continue
    const text = typeof o['text'] === 'string' ? o['text'] : ''
    const reason = typeof o['reason'] === 'string' ? o['reason'] : null
    out.push({ text, verdict, reason })
  }
  return out
}

/**
 * Derive the per-chip verdict for one citation `marker` from the finding's
 * `verification` block. Never fabricates: absence of data yields the explicit
 * not-checked / not-recorded verdicts with honest labels.
 */
export function claimVerdictForMarker(
  verification: Record<string, unknown> | null | undefined,
  marker: string,
): ClaimVerdict {
  const v = asRecord(verification)
  if (!v) {
    return {
      kind: 'not-recorded',
      label: 'claim-level verdict not recorded',
      spans: [],
      judgeStatus: null,
      checkable: null,
      supported: null,
    }
  }

  const judgeStatus = typeof v['judge_status'] === 'string' ? (v['judge_status'] as string) : null
  const checkable = intOrNull(v['checkable_claims'])
  const supported = intOrNull(v['supported_claims'])
  const ordinal = markerOrdinal(marker)

  const spans: ClaimVerdictSpan[] = []
  if (ordinal !== null && Array.isArray(v['unsupported_spans'])) {
    for (const item of v['unsupported_spans'] as unknown[]) {
      const o = asRecord(item)
      if (!o) continue
      const text = typeof o['text'] === 'string' ? o['text'] : ''
      const reason = typeof o['reason'] === 'string' ? o['reason'] : 'unsupported'
      if (!text) continue
      if (!spanNamesOrdinal(o['markers'], ordinal)) continue
      spans.push({ text, reason, reasonLabel: claimReasonLabel(reason) })
    }
    spans.sort((a, b) => (REASON_SEVERITY[a.reason] ?? 2) - (REASON_SEVERITY[b.reason] ?? 2))
  }

  if (spans.length > 0) {
    const worst = spans[0]
    const kind: ClaimVerdictKind =
      worst.reason === 'judge_contradicted'
        ? 'contradicted'
        : worst.reason === 'judge_unsupported'
          ? 'unsupported'
          : 'flagged'
    return { kind, label: worst.reasonLabel, spans, judgeStatus, checkable, supported }
  }

  // P2-4 — no flag names this ordinal. Prefer the persisted claim_verdicts
  // LEDGER (records EVERY graded claim, supported included) over the vaguer
  // legacy "not flagged" read, when the ledger actually names this ordinal.
  if (ordinal !== null) {
    const ledgerRows = ledgerRowsNamingOrdinal(v, ordinal)
    if (ledgerRows.length > 0) {
      // A defensive worst-first pick: spans and the ledger are written in
      // parallel server-side, so a failing ledger row should already have
      // been caught by the spans branch above — this only guards against a
      // ledger-only failure ever being silently swallowed.
      const worst = [...ledgerRows].sort(
        (a, b) => LEDGER_SEVERITY[a.verdict] - LEDGER_SEVERITY[b.verdict],
      )[0]
      if (worst.verdict === 'supported') {
        return {
          kind: 'supported',
          label: 'supported by the verify judge',
          spans: [],
          judgeStatus,
          checkable,
          supported,
        }
      }
      const reasonLabel = worst.reason
        ? claimReasonLabel(worst.reason)
        : worst.verdict === 'hard_fail'
          ? 'flagged (hard fail)'
          : 'flagged (soft fail)'
      const kind: ClaimVerdictKind =
        worst.reason === 'judge_contradicted'
          ? 'contradicted'
          : worst.reason === 'judge_unsupported'
            ? 'unsupported'
            : 'flagged'
      return {
        kind,
        label: reasonLabel,
        spans: worst.text
          ? [{ text: worst.text, reason: worst.reason ?? worst.verdict, reasonLabel }]
          : [],
        judgeStatus,
        checkable,
        supported,
      }
    }
  }

  if (judgeStatus === 'llm') {
    return {
      kind: 'not-flagged',
      label: 'not flagged by the verify judge',
      spans: [],
      judgeStatus,
      checkable,
      supported,
    }
  }

  return {
    kind: 'not-checked',
    label: 'claim-level verdict not recorded — LLM judge did not run',
    spans: [],
    judgeStatus,
    checkable,
    supported,
  }
}
