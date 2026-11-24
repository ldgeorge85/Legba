/**
 * X2 / C1 — the two stamps a consult answer's turn header carries: what the
 * turn USED (the server's `consult_usage/1` receipt) and whether its answer
 * was JUDGED (the server's `consult_verification/1` stamp).
 *
 * The panel prints what the server says and derives nothing. The rules:
 *
 *   * the verification label is NEVER blank — `judged: <score>`,
 *     `unassessable · <why>` (never a number: an unassessable turn is not
 *     0.5, W11-F29), or `not judged · <why>`. A turn recorded before judging
 *     existed (the server sends `verification: null`) says exactly that;
 *   * a not-judged label whose judge could not run names WHY (W11-F93) —
 *     `judge_unavailable_reason`: budget exhausted / route down / timed out /
 *     not settled — never a bare "judge unavailable";
 *   * a usage field the run did not report reads "not measured", never 0;
 *     a route that does not price its calls (the self-hosted core plane)
 *     reads "not priced", never "$0.00".
 */

export interface ConsultUsageReceipt {
  schema?: string
  plane?: string | null
  route?: string | null
  model?: string | null
  subprovider?: string | null
  calls?: number | null
  input_tokens?: number | null
  output_tokens?: number | null
  elapsed_s?: number | null
  cost_usd?: number | null
  cost_reason?: string | null
  reason?: string | null
}

export type ConsultVerificationState = 'judged' | 'unassessable' | 'not_judged'

export interface ConsultVerification {
  schema?: string
  state: ConsultVerificationState
  score?: number | null
  reason?: string | null
  judge_status?: string | null
  judge_model?: string | null
  judge_llm_ref?: string | null
  weak_judge?: boolean
  checkable_claims?: number | null
  supported_claims?: number | null
  cited_sentences?: number | null
  claims_truncated?: boolean
  judged_at?: string | null
  /** W11-F93 — why the judge could not run (closed vocabulary, server-side). */
  judge_unavailable_reason?: string | null
}

const STATES: ReadonlySet<string> = new Set(['judged', 'unassessable', 'not_judged'])

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** The receipt, or `null` when the server recorded none (`{}` / absent). */
export function parseUsageReceipt(raw: unknown): ConsultUsageReceipt | null {
  if (!isObject(raw) || Object.keys(raw).length === 0) return null
  return raw as ConsultUsageReceipt
}

/**
 * `undefined` = the server sent no key (an older registry, or a live turn
 * that never settled); `null` = the row predates judging (migration 0234);
 * otherwise the stamp. A malformed stamp is treated as absent, not as a verdict.
 */
export function parseVerification(raw: unknown): ConsultVerification | null | undefined {
  if (raw === null) return null
  if (!isObject(raw) || typeof raw.state !== 'string' || !STATES.has(raw.state)) return undefined
  return raw as unknown as ConsultVerification
}

const REASONS: Record<string, string> = {
  no_cited_claims: 'no cited claims',
  no_checkable_claims: 'no checkable claims',
  thin_claims_on_substantive_body: 'too few claims to score',
  judge_disabled: 'judge off',
  no_judge_route: 'no judge route',
  judge_route_unresolved: 'judge route unavailable',
  synthesis_incomplete: 'answer incomplete',
  judge_timeout: 'judge timed out',
  judge_unavailable: 'judge unavailable',
  judge_failed: 'judge failed',
  no_evidence_reader: 'evidence unreadable',
  no_stamp_from_run: 'no stamp from the run',
  judged_without_score: 'judge returned no score',
  deep_consult_not_judged: 'deep answers are not judged yet',
  judged_text_changed: 'answer changed after judging',
  judge_input_over_budget: 'too much to judge',
}

/** W11-F93 — `judge_unavailable_reason`, the judge's why-not. */
const UNAVAILABLE: Record<string, string> = {
  budget_exhausted: 'budget exhausted',
  route_down: 'route down',
  timeout: 'timed out',
  not_settled: 'answer not settled',
}

/** Reasons whose own text does not already say why the judge could not run. */
const BARE_REASONS: ReadonlySet<string> = new Set(['judge_unavailable', 'judged_text_changed'])

function reasonText(reason: string | null | undefined): string {
  if (!reason) return 'no reason given'
  return REASONS[reason] ?? reason.replace(/_/g, ' ')
}

export interface StampLabel {
  text: string
  title: string
  tone: 'judged' | 'unassessable' | 'not_judged'
}

/** The turn header's judged / unassessable / not-judged label. Never blank. */
export function verificationLabel(v: ConsultVerification | null | undefined): StampLabel {
  if (v === null) {
    return {
      text: 'not judged · recorded before judging',
      title: 'this turn was recorded before consult answers were judged (migration 0234)',
      tone: 'not_judged',
    }
  }
  if (v === undefined) {
    return {
      text: 'not judged',
      title: 'the server sent no verification stamp for this turn',
      tone: 'not_judged',
    }
  }
  if (v.state === 'judged' && typeof v.score === 'number') {
    const weak = v.weak_judge ? ' (weak judge)' : ''
    const claims =
      typeof v.supported_claims === 'number' && typeof v.checkable_claims === 'number'
        ? `${v.supported_claims} of ${v.checkable_claims} cited claims supported`
        : 'claim counts not recorded'
    const judge = v.judge_model ?? v.judge_llm_ref ?? 'judge not named'
    const at = v.judged_at ? ` · judged ${v.judged_at}` : ''
    const cut = v.claims_truncated ? ' · only the first cited claims were judged' : ''
    return {
      text: `judged: ${v.score.toFixed(2)}${weak}`,
      title: `faithfulness of the answer's cited claims to what they cite (0–1): ${claims} · ${judge}${v.judge_llm_ref && v.judge_model ? ` (${v.judge_llm_ref})` : ''}${at}${cut}`,
      tone: 'judged',
    }
  }
  if (v.state === 'unassessable') {
    return {
      text: `unassessable · ${reasonText(v.reason)}`,
      title: 'the judge had nothing it could grade, so no score is published (never 0.5)',
      tone: 'unassessable',
    }
  }
  const why = v.judge_unavailable_reason
    ? (UNAVAILABLE[v.judge_unavailable_reason] ?? v.judge_unavailable_reason.replace(/_/g, ' '))
    : null
  const suffix = why && BARE_REASONS.has(v.reason ?? '') ? ` (${why})` : ''
  return {
    text: `not judged · ${reasonText(v.reason)}${suffix}`,
    title: `no judge adjudicated this answer; nothing here is a verdict${why ? ` · cause: ${why}` : ''}`,
    tone: 'not_judged',
  }
}

function tokens(n: number | null | undefined): string | null {
  return typeof n === 'number' ? n.toLocaleString('en-US') : null
}

/** The turn header's usage line: model · tokens · elapsed · cost. */
export function usageLabel(u: ConsultUsageReceipt | null): { text: string; title: string } {
  if (u === null) {
    return { text: 'usage: not recorded', title: 'the server recorded no usage for this turn' }
  }
  if (u.reason) {
    return {
      text: 'usage: not measured',
      title: `the run reported no usage (${reasonText(u.reason)})`,
    }
  }
  const tin = tokens(u.input_tokens)
  const tout = tokens(u.output_tokens)
  const tok = tin !== null && tout !== null ? `${tin} in / ${tout} out tok` : 'tokens: not measured'
  const secs = typeof u.elapsed_s === 'number' ? `${Math.round(u.elapsed_s)} s` : 'time: not measured'
  const cost =
    typeof u.cost_usd === 'number'
      ? `${u.cost_reason === 'table_estimate' ? '~' : ''}$${u.cost_usd.toFixed(2)}`
      : u.cost_reason === 'route_unpriced'
        ? 'cost: not priced'
        : 'cost: not measured'
  const who = u.model ?? u.plane ?? 'model not recorded'
  const calls = typeof u.calls === 'number' ? `${u.calls} model call(s)` : 'calls not recorded'
  return {
    text: `${who} · ${tok} · ${secs} · ${cost}`,
    title: `${calls} · route ${u.route ?? 'not recorded'}${u.cost_reason === 'route_unpriced' ? ' · this route carries no price table (self-hosted)' : ''}${u.cost_reason === 'table_estimate' ? ' · cost estimated from the price table (the run was cut before its receipt)' : ''}`,
  }
}
