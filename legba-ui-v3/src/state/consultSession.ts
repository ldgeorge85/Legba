/**
 * Per-panel consult session store (GLASS-4) — the consult conversation, lifted
 * out of the component so it OUTLIVES the component.
 *
 * The defect this closes
 * ======================
 *
 * `panels/system/Consult.tsx` held `transcript`, `sessionId`, `liveSteps` and
 * `pins` in component-local `useState`. Dockview destroys a panel's React tree
 * on `api.clear()` — which `applyPreset` and both Investigate grids call — so
 * picking a layout preset silently threw away an in-flight turn, the answer it
 * was about to produce, the whole conversation above it, and the pinned
 * context the operator had assembled. Nothing warned; the panel just came back
 * empty.
 *
 * State keyed by panel id lives here instead. A panel that unmounts loses its
 * DOM, not its conversation: the in-flight `fetch` inside `send()` is never
 * aborted, and its continuation writes THROUGH this store, so a turn started
 * before a preset pick still lands its answer and is waiting when the panel is
 * reopened.
 *
 * The key is `PanelRegistration.id` — `singleton:system.consult` for the
 * ordinary sidebar/preset panel (deterministic, so the SAME store slice is
 * rejoined after a clear), and the registration id for a bound instance.
 *
 * Durability, in layers
 * =====================
 *
 *   1. **The store** survives unmount / remount / `api.clear()` / tab switch —
 *      it is module state, not component state.
 *   2. **localStorage** (`legba_consult_panels_v1`, the same best-effort,
 *      never-throw shape `lib/layoutPresets.ts` uses for custom layouts)
 *      carries `{sessionId, transcript, pins, pendingTurn, draft, scope,
 *      maxRounds}` across a reload.
 *   3. **The server** is the authority. On remount the panel reconciles its
 *      slice against `GET /api/v1/consult/sessions/{id}` via
 *      {@link reconcileWithServer}: server truth wins for completed turns, and
 *      a local `pendingTurn` survives only while the server has NOT recorded
 *      an answer for it.
 *
 * Layer 3 rests on a backend property that is PROVEN, not assumed: a client
 * that disconnects mid-turn does not lose the assistant turn — the registry's
 * request handler runs to completion and `_persist_assistant_turn` still
 * writes. See `tests/data_pkg/test_consult_disconnect_persistence.py`, which
 * establishes it through a real uvicorn with a real TCP abort and carries the
 * counterfactual (a cancelling middleware, under which the turn IS lost) so
 * the guarantee is falsifiable rather than decorative.
 *
 * What deliberately does NOT live here: the history sidebar's open/closed
 * state, its fetched session list, and its error. Those are view chrome that
 * SHOULD reset with the panel; only the conversation is durable.
 */
import { create } from 'zustand'
import type { ConsultSessionDetail, ConsultUsage } from '@/lib/api'
import type { Selection } from '@/state/selection'

// ---------------------------------------------------------------------------
// Wire / turn shapes — owned here now that the store, not the panel, is the
// source of truth for a conversation.
// ---------------------------------------------------------------------------

export interface ConsultToolCall {
  tool: string
  args: Record<string, unknown>
  result: unknown
}

export interface ConsultCitedRef {
  kind: string
  id: string
  description?: string | null
}

/** One streamed ReAct step frame off the SSE relay. */
export interface StepFrame {
  type: string
  phase?: string
  kind?: string
  tool?: string
  round?: number
  [k: string]: unknown
}

/** One turn of the transcript. */
export interface ChatTurn {
  role: 'user' | 'assistant'
  content: string
  steps?: StepFrame[]
  toolCalls?: ConsultToolCall[]
  citedRefs?: ConsultCitedRef[]
  uncertainty?: number | null
  unansweredAspects?: string[]
  findingId?: string | null
  deep?: boolean
  model?: string | null
  /**
   * The run this answer came out of. Carried on the SETTLED turn, not just the
   * pending one, because it is the address "Synthesize from evidence" posts to
   * — an answer that was cut short is only recoverable while the panel still
   * knows which run's evidence to rebuild it from.
   */
  requestId?: string | null
  /**
   * The persisted turn's own id. The OTHER address a re-synthesis can use, and
   * the only one a turn written before the run id was recorded on the row has
   * — which is every turn the incident itself is in.
   */
  turnId?: string | null
  /** Did the final synthesis finish? See `ConsultResponseBody.synthesis_status`. */
  synthesisStatus?: 'complete' | 'partial' | 'none' | null
  /** Does the server still hold the evidence to re-synthesise from? */
  resynthesizable?: boolean
  /** How faithfully a re-synthesis reconstructed the transcript it read. */
  replayFidelity?: 'exact' | 'rebuilt' | 'unavailable' | null
  /** The human sentence that goes with a non-`exact` fidelity. Always shown. */
  replayNote?: string | null
  /**
   * 7g-2 — the PROVENANCE CENSUS the server composed for this answer: cited
   * refs by origin class, plus the number of sentences carrying no citation
   * at all. Absent on every pre-7g-2 turn, and the counts inside it are
   * `null` rather than 0 when the classes could not be measured — a zero
   * would read as "cites no live reporting", which is a claim.
   */
  provenanceCensus?: ProvenanceCensus | null
}

/**
 * What a consult answer rests on, counted by the SERVER
 * (`analysts.consult_provenance_census`). The panel prints this; it derives
 * nothing, because a share the client computed could disagree with the answer
 * it is printed beside.
 *
 * The four class counts are REFS; `modelKnowledge` is SENTENCES. They are
 * never added together, and the rendered line carries the unit on each.
 */
export interface ProvenanceCensus {
  version?: string
  live?: number | null
  web_retrieval?: number | null
  history?: number | null
  seed?: number | null
  unclassified?: number | null
  unresolved?: number | null
  cited_total?: number | null
  model_knowledge?: number | null
  sentences_examined?: number | null
  basis?: string | null
}

/**
 * A turn that has been sent but not yet answered.
 *
 * `steps` is the live ReAct ticker for this turn — it lives on the pending
 * turn rather than beside it so that "what is in flight" and "what has it done
 * so far" can never disagree, and so both are dropped together the instant the
 * answer lands.
 */
export interface PendingTurn {
  /** Client-minted id that correlates the POST with its SSE step stream. */
  requestId: string
  /** The question AS TYPED — what the transcript renders. */
  question: string
  mode: 'chat' | 'deep'
  /** Epoch ms, for the reattach poll's deadline. */
  startedAt: number
  /**
   * The page load that issued this turn — see {@link CONSULT_PAGE_LOAD_ID}.
   * Stamped at send time and persisted, so it is what tells a turn interrupted
   * by an UNMOUNT (its `fetch` is still alive) apart from one interrupted by a
   * RELOAD (its `fetch` died with the page).
   */
  pageLoadId: string
  steps: StepFrame[]
  /**
   * Set when a reattached turn has been polled to its deadline without the
   * server producing an answer. The panel stops waiting and offers to dismiss
   * it, rather than showing "Consulting…" forever.
   */
  stalled?: boolean
  /**
   * Running LLM spend for this turn, with its ceilings — folded off the
   * `render_prompt` (ceilings) and `llm_call` (totals) step frames by
   * {@link applyFrame}. Live, because a bill you only learn afterwards is a
   * bill you could not have stopped.
   */
  usage?: ConsultUsage | null
  /**
   * The effective tool-round cap this run is actually operating under, and
   * where it came from ("request" / "default" / "default_malformed_request").
   *
   * The whole of the 2026-09-16 over-spend traces to a cap nobody could see:
   * the panel asked for one number and the loop ran under another. It is on the
   * pending turn so it is visible DURING the run, not in a post-mortem.
   */
  roundCap?: number | null
  roundsSource?: string | null
  /**
   * The final answer as it streams, accumulated from `answer_delta` frames in
   * arrival order. This is the text that used to be DISCARDED when a synthesis
   * ran out of budget — holding it client-side means the operator has read most
   * of it before the server ever decides whether it finished.
   */
  answerPreview?: string
}

export interface ConsultPanelState {
  sessionId: string | null
  transcript: ChatTurn[]
  pins: Selection[]
  pendingTurn: PendingTurn | null
  /** The composer's text — a half-typed question is a partial turn too. */
  draft: string
  scope: string
  maxRounds: number
  /**
   * Last turn error. In the store (not just the component) so a turn that
   * fails while the panel is unmounted still explains itself when it reopens
   * — otherwise the pending turn would simply vanish. Not persisted: a stale
   * error from a previous session is noise, not information.
   */
  error: string | null
  /**
   * Bumped on every mutation. The remount reconcile snapshots it before its
   * `loadConsultSession` round-trip and refuses to apply a result that raced a
   * newer local change — without it, a reconcile in flight when a turn lands
   * would overwrite the fresh answer with the stale server transcript.
   */
  rev: number
  /** Epoch ms of the last mutation — orders the persisted-panel eviction. */
  updatedAt: number
}

export const DEFAULT_MAX_ROUNDS = 10

/** Shared frozen default so an unseen panel id renders without churning refs. */
export const EMPTY_PANEL: ConsultPanelState = Object.freeze({
  sessionId: null,
  transcript: [],
  pins: [],
  pendingTurn: null,
  draft: '',
  scope: '',
  maxRounds: DEFAULT_MAX_ROUNDS,
  error: null,
  rev: 0,
  updatedAt: 0,
}) as ConsultPanelState

// ---------------------------------------------------------------------------
// localStorage — best-effort, bounded, never throws (layoutPresets.ts idiom).
// ---------------------------------------------------------------------------

const STORAGE_KEY = 'legba_consult_panels_v1'

/** Keep the newest N panel slices; a workspace churns through panel ids. */
export const MAX_PERSISTED_PANELS = 8
/** Keep the newest N turns of a conversation. */
export const MAX_PERSISTED_TURNS = 40
/** ReAct traces are the bulk of a turn — cap them hard. */
export const MAX_PERSISTED_STEPS = 40
export const MAX_PERSISTED_TOOL_CALLS = 10
/**
 * The live answer preview is unbounded prose — cap what crosses into storage.
 * The HEAD is kept, not the tail: a preview is read from the top, and the
 * settled answer replaces it entirely the moment the turn lands.
 */
export const MAX_PERSISTED_PREVIEW = 4000

/** The subset of a panel slice that is written to localStorage. */
type PersistedPanel = Pick<
  ConsultPanelState,
  'sessionId' | 'transcript' | 'pins' | 'pendingTurn' | 'draft' | 'scope' | 'maxRounds' | 'updatedAt'
>

function trimTurn(turn: ChatTurn): ChatTurn {
  const out: ChatTurn = { ...turn }
  if (out.steps && out.steps.length > MAX_PERSISTED_STEPS) {
    out.steps = out.steps.slice(-MAX_PERSISTED_STEPS)
  }
  if (out.toolCalls && out.toolCalls.length > MAX_PERSISTED_TOOL_CALLS) {
    out.toolCalls = out.toolCalls.slice(0, MAX_PERSISTED_TOOL_CALLS)
  }
  return out
}

/**
 * Project the live store to its persisted form, bounded on every axis that can
 * grow without limit (panels, turns, steps per turn, tool calls per turn).
 * Exported for the unit tests — the caps are the contract, not an accident.
 */
export function toPersisted(
  panels: Record<string, ConsultPanelState>,
): Record<string, PersistedPanel> {
  const entries = Object.entries(panels)
    .sort((a, b) => b[1].updatedAt - a[1].updatedAt)
    .slice(0, MAX_PERSISTED_PANELS)
  const out: Record<string, PersistedPanel> = {}
  for (const [id, panel] of entries) {
    const pending = panel.pendingTurn
    out[id] = {
      sessionId: panel.sessionId,
      transcript: panel.transcript.slice(-MAX_PERSISTED_TURNS).map(trimTurn),
      pins: panel.pins,
      pendingTurn: pending
        ? {
            ...pending,
            steps: pending.steps.slice(-MAX_PERSISTED_STEPS),
            answerPreview: pending.answerPreview?.slice(0, MAX_PERSISTED_PREVIEW),
          }
        : null,
      draft: panel.draft,
      scope: panel.scope,
      maxRounds: panel.maxRounds,
      updatedAt: panel.updatedAt,
    }
  }
  return out
}

function isTurn(value: unknown): value is ChatTurn {
  if (!value || typeof value !== 'object') return false
  const t = value as Record<string, unknown>
  return (t.role === 'user' || t.role === 'assistant') && typeof t.content === 'string'
}

function isPin(value: unknown): value is Selection {
  if (!value || typeof value !== 'object') return false
  const p = value as Record<string, unknown>
  return typeof p.kind === 'string' && typeof p.id === 'string' && !!p.id
}

function parsePending(value: unknown): PendingTurn | null {
  if (!value || typeof value !== 'object') return null
  const p = value as Record<string, unknown>
  if (typeof p.requestId !== 'string' || !p.requestId) return null
  if (typeof p.question !== 'string' || !p.question) return null
  return {
    requestId: p.requestId,
    question: p.question,
    mode: p.mode === 'deep' ? 'deep' : 'chat',
    startedAt: typeof p.startedAt === 'number' ? p.startedAt : Date.now(),
    // A restored turn with no stamp is treated as belonging to some older page
    // — i.e. detached, which is the safe reading: it gets polled rather than
    // silently waited on forever.
    pageLoadId: typeof p.pageLoadId === 'string' ? p.pageLoadId : '',
    steps: Array.isArray(p.steps) ? (p.steps as StepFrame[]) : [],
    stalled: p.stalled === true,
    // Everything a reload would otherwise silently drop: the spend meter's
    // totals and ceilings, the cap the run is under, and the answer the
    // operator was already reading.
    usage: parsePersistedUsage(p.usage),
    roundCap: typeof p.roundCap === 'number' ? p.roundCap : null,
    roundsSource: typeof p.roundsSource === 'string' ? p.roundsSource : null,
    answerPreview: typeof p.answerPreview === 'string' ? p.answerPreview : undefined,
  }
}

/**
 * A persisted spend snapshot. Missing/garbage numbers read as 0, but a value
 * carrying NONE of them reads as null — a meter showing "$0.00 so far" off a
 * stale storage blob would be a lie, and no meter is better than a wrong one.
 */
function parsePersistedUsage(value: unknown): ConsultUsage | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  const out: ConsultUsage = { ...ZERO_USAGE }
  let found = false
  for (const key of USAGE_FIELDS) {
    const v = raw[key]
    if (typeof v === 'number' && Number.isFinite(v)) {
      out[key] = v
      found = true
    }
  }
  return found ? out : null
}

/**
 * Defensive parse of the persisted map — a malformed or stale shape yields an
 * empty store rather than a crashed shell, and individual bad entries are
 * dropped without taking their neighbours with them.
 */
export function parseConsultPanels(raw: string | null): Record<string, ConsultPanelState> {
  if (!raw) return {}
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return {}
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
  const out: Record<string, ConsultPanelState> = {}
  for (const [id, value] of Object.entries(parsed as Record<string, unknown>)) {
    if (!value || typeof value !== 'object') continue
    const p = value as Record<string, unknown>
    out[id] = {
      sessionId: typeof p.sessionId === 'string' ? p.sessionId : null,
      transcript: Array.isArray(p.transcript) ? p.transcript.filter(isTurn) : [],
      pins: Array.isArray(p.pins) ? p.pins.filter(isPin) : [],
      pendingTurn: parsePending(p.pendingTurn),
      draft: typeof p.draft === 'string' ? p.draft : '',
      scope: typeof p.scope === 'string' ? p.scope : '',
      maxRounds: typeof p.maxRounds === 'number' ? p.maxRounds : DEFAULT_MAX_ROUNDS,
      error: null,
      rev: 0,
      updatedAt: typeof p.updatedAt === 'number' ? p.updatedAt : 0,
    }
  }
  return out
}

function loadInitial(): Record<string, ConsultPanelState> {
  try {
    return parseConsultPanels(localStorage.getItem(STORAGE_KEY))
  } catch {
    // localStorage unavailable (private mode / SSR) — the store still works
    // in memory, it just won't survive a reload.
    return {}
  }
}

function persist(panels: Record<string, ConsultPanelState>): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(toPersisted(panels)))
  } catch {
    // Almost always a quota overrun on a long conversation with fat ReAct
    // traces. Retry once with the austere projection — the conversation text
    // matters far more than its tool trace — and give up quietly after that
    // rather than surfacing a storage error into a working panel.
    try {
      const austere: Record<string, PersistedPanel> = {}
      for (const [id, panel] of Object.entries(toPersisted(panels))) {
        austere[id] = {
          ...panel,
          transcript: panel.transcript
            .slice(-10)
            .map(({ role, content }) => ({ role, content })),
          pendingTurn: panel.pendingTurn ? { ...panel.pendingTurn, steps: [] } : null,
        }
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(austere))
    } catch {
      // Best-effort persistence — the in-memory store is unaffected.
    }
  }
}

// ---------------------------------------------------------------------------
// Page-load identity — what tells an unmount apart from a reload.
// ---------------------------------------------------------------------------

/**
 * A fresh id per page load, stamped onto every pending turn.
 *
 * Two pending turns look identical in the persisted state but need opposite
 * handling:
 *
 *   * interrupted by an UNMOUNT (preset pick, tab switch) — the `fetch` in
 *     `send` was never aborted, so it is still running and WILL write its
 *     answer into the store by itself. Nothing else should touch it.
 *   * interrupted by a RELOAD — the `fetch` died with the old page, so no
 *     promise will ever resolve it and the answer can only be recovered by
 *     re-reading the session from the server.
 *
 * Comparing the stamp against this constant separates them exactly: a turn
 * carrying a different (or absent) id belongs to a page that no longer exists.
 * A persisted stamp is what makes this survive the reload it is detecting,
 * which a mutable in-memory registry could not do.
 *
 * Generated without `crypto.randomUUID` on purpose — tests routinely stub that
 * to a constant, and an id that collides across page loads would defeat the
 * one distinction this value exists to make.
 */
export const CONSULT_PAGE_LOAD_ID = `pl-${Date.now().toString(36)}-${Math.random()
  .toString(36)
  .slice(2, 10)}`

/** A pending turn this page load can no longer receive an answer for. */
export function isDetached(pending: PendingTurn | null): boolean {
  return !!pending && pending.pageLoadId !== CONSULT_PAGE_LOAD_ID
}

// ---------------------------------------------------------------------------
// Live frame folding — what a step frame does to the turn BESIDES being listed
// ---------------------------------------------------------------------------

/** The six numbers of a spend snapshot: four running totals, two ceilings. */
const USAGE_FIELDS = [
  'calls',
  'input_tokens',
  'output_tokens',
  'est_cost_usd',
  'max_input_tokens',
  'max_cost_usd',
] as const

/** Kinds that may carry the spend snapshot as FLAT fields rather than nested. */
const SPEND_FRAME_KINDS = new Set(['render_prompt', 'llm_call', 'spend_ceiling_reached'])

const ZERO_USAGE: ConsultUsage = {
  calls: 0,
  input_tokens: 0,
  output_tokens: 0,
  est_cost_usd: 0,
  max_input_tokens: 0,
  max_cost_usd: 0,
}

function finiteNumber(src: Record<string, unknown> | null, key: string): number | null {
  if (!src) return null
  const v = src[key]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/**
 * Merge whatever spend a frame carries onto the totals so far.
 *
 * Returns `null` when the frame says nothing about spend, so a caller can tell
 * "no news" from "zero" — two very different readings of a live run.
 *
 * Two carriers are accepted because the run publishes both: `usage: {...}` on
 * the LLM-call frames (running totals plus ceilings), and the same field names
 * FLAT on the frames whose subject IS the budget (`render_prompt` seeding the
 * ceilings, `spend_ceiling_reached` announcing the stop). Merging rather than
 * replacing is what lets a ceiling arrive on the first frame of a run and still
 * be standing twenty calls later.
 */
export function usageFromFrame(
  frame: StepFrame,
  prev?: ConsultUsage | null,
  allowFlat = false,
): ConsultUsage | null {
  const raw = frame as Record<string, unknown>
  const nested =
    raw.usage && typeof raw.usage === 'object' && !Array.isArray(raw.usage)
      ? (raw.usage as Record<string, unknown>)
      : null
  const flat = allowFlat ? raw : null
  const out: ConsultUsage = { ...ZERO_USAGE, ...(prev ?? {}) }
  let found = false
  for (const key of USAGE_FIELDS) {
    const value = finiteNumber(nested, key) ?? finiteNumber(flat, key)
    if (value != null) {
      out[key] = value
      found = true
    }
  }
  return found ? out : null
}

/**
 * Fold one streamed frame into the pending turn.
 *
 * Most frames are just another line on the ticker. Three do more, and each one
 * is here because the 2026-09-16 run was expensive in a way nobody could watch:
 *
 *   * `render_prompt` — the run's FIRST frame — declares the round cap it is
 *     actually operating under (and whether that came from the request or a
 *     default), plus the spend ceilings.
 *   * `llm_call` carries the running spend, so the meter moves per call.
 *   * `answer_delta` carries a chunk of the final answer. It accumulates into
 *     `answerPreview` and is deliberately NOT listed as a step: a hundred
 *     400-char chunks would bury the trace, blow the persisted-step cap, and
 *     turn "Thinking… (N steps)" into a meaningless number.
 */
export function applyFrame(pending: PendingTurn, frame: StepFrame): PendingTurn {
  const kind = String(frame.kind ?? '')
  if (kind === 'answer_delta') {
    const text = typeof frame.text === 'string' ? frame.text : ''
    if (!text) return pending
    return { ...pending, answerPreview: (pending.answerPreview ?? '') + text }
  }
  const next: PendingTurn = { ...pending, steps: [...pending.steps, frame] }
  const usage = usageFromFrame(frame, pending.usage, SPEND_FRAME_KINDS.has(kind))
  if (usage) next.usage = usage
  if (kind === 'render_prompt') {
    const cap = frame.max_rounds
    if (typeof cap === 'number' && Number.isFinite(cap)) next.roundCap = cap
    if (typeof frame.rounds_source === 'string') next.roundsSource = frame.rounds_source
  }
  return next
}

// ---------------------------------------------------------------------------
// Server reconcile
// ---------------------------------------------------------------------------

/**
 * Map the persisted server turns onto the client's transcript shape.
 *
 * `turnId` is what makes a re-seeded turn recoverable AT ALL: the session API
 * does not carry the run id, and a turn written before it was recorded on the
 * row has none to carry, so the turn's own id is the address the synthesis
 * endpoint resolves on.
 *
 * `requestId` / `synthesisStatus` are read through OPTIMISTICALLY — absent
 * today, so the panel decides "was this answer cut?" by scanning the turn's
 * steps for a `degraded_final` / `partial_final` instead. Reading them here
 * means the cheaper, exact signal starts working the day the server sends it,
 * with no client change.
 */
export function turnsFromServer(detail: ConsultSessionDetail): ChatTurn[] {
  return detail.turns.map((t) => ({
    role: t.role,
    content: t.content,
    steps: (t.steps as StepFrame[]) ?? [],
    toolCalls: (t.tool_calls as ConsultToolCall[]) ?? [],
    citedRefs: (t.cited_refs as ConsultCitedRef[]) ?? [],
    findingId: t.finding_id ?? null,
    deep: Boolean(t.finding_id),
    turnId: t.id,
    requestId: t.request_id ?? null,
    synthesisStatus: t.synthesis_status ?? null,
  }))
}

export interface ReconcileResult {
  transcript: ChatTurn[]
  pendingTurn: PendingTurn | null
}

/**
 * Merge a local panel slice with server truth.
 *
 * The rules, and why each one is the way it is:
 *
 *   * **Server truth wins for completed turns.** The registry persists every
 *     turn (`consult_turns`), so its transcript is authoritative and richer
 *     than the local copy — it carries turns this browser never saw, including
 *     the answer to a turn that was in flight when the tab reloaded.
 *
 *   * **A local `pendingTurn` survives only while the server has not recorded
 *     its answer.** The discriminator is the server transcript's LAST turn:
 *     `consult_api.invoke_consult` appends the USER turn *before* the actor
 *     runs and the ASSISTANT turn on completion, so a trailing `user` turn
 *     means "still running", and a trailing `assistant` turn means "answered —
 *     and the answer is already in the transcript above". A pending turn kept
 *     past its answer would double-render the question; one dropped early
 *     would leave the operator staring at nothing while a turn ran.
 *
 *   * The match is `endsWith`, not equality, because the panel prefixes pinned
 *     context onto the question it sends while the transcript shows the
 *     question as typed.
 *
 *   * **A server transcript SHORTER than ours is not authoritative** — nothing
 *     is changed at all. Turns are append-only, so a short read cannot mean
 *     the conversation shrank; it means either a stale read or a lost audit
 *     write. That second case is real and not merely theoretical:
 *     `_persist_assistant_turn` is best-effort by design (`append_turn`
 *     swallows DB errors so an audit outage can never fail a consult), so a
 *     turn the operator has already READ can legitimately be missing from the
 *     server's copy. Letting "server truth wins" run unguarded there would
 *     erase a good answer off the operator's screen — the exact class of
 *     silent loss this whole store exists to end.
 *
 *   * **A null `server` changes nothing** — the load failed, or there is no
 *     session yet. Absence of evidence is not evidence of an empty
 *     conversation.
 */
export function reconcileWithServer(
  local: ConsultPanelState,
  server: ConsultSessionDetail | null,
): ReconcileResult {
  if (!server) {
    return { transcript: local.transcript, pendingTurn: local.pendingTurn }
  }
  const serverTurns = turnsFromServer(server)
  if (serverTurns.length < local.transcript.length) {
    return { transcript: local.transcript, pendingTurn: local.pendingTurn }
  }
  const pending = local.pendingTurn
  if (!pending) return { transcript: serverTurns, pendingTurn: null }

  const last = serverTurns[serverTurns.length - 1]
  const stillRunning = last.role === 'user' && last.content.endsWith(pending.question)
  return { transcript: serverTurns, pendingTurn: stillRunning ? pending : null }
}

// ---------------------------------------------------------------------------
// The store
// ---------------------------------------------------------------------------

interface ConsultSessionsState {
  panels: Record<string, ConsultPanelState>

  /** Read a slice, defaulted — never undefined, never a fresh object. */
  panel: (panelId: string) => ConsultPanelState

  setDraft: (panelId: string, draft: string) => void
  setScope: (panelId: string, scope: string) => void
  setMaxRounds: (panelId: string, maxRounds: number) => void
  setError: (panelId: string, error: string | null) => void
  setSessionId: (panelId: string, sessionId: string | null) => void

  /** Open a turn: append the user turn and mark it pending. */
  startTurn: (panelId: string, pending: PendingTurn) => void
  /** Append one live ReAct frame to the pending turn (no-op once settled). */
  pushStep: (panelId: string, requestId: string, frame: StepFrame) => void
  /** Land the answer: append the assistant turn and clear the pending one. */
  completeTurn: (panelId: string, requestId: string, turn: ChatTurn) => void
  /**
   * Swap one settled assistant turn for a better version of ITSELF — the
   * re-synthesis of a cut-short answer. Replaced in place rather than appended
   * because the two are one answer, not a conversation: appending would send
   * the truncated text back to the model as history on the next turn.
   */
  replaceTurn: (panelId: string, index: number, turn: ChatTurn) => void
  /** Give up on a turn (request failed) — clears pending, records the error. */
  failTurn: (panelId: string, requestId: string, error: string) => void
  /** Stop waiting on a reattached turn that outlived the poll deadline. */
  markStalled: (panelId: string, requestId: string) => void
  /** Drop a stalled/abandoned pending turn at the operator's request. */
  dismissPending: (panelId: string) => void

  addPin: (panelId: string, pin: Selection) => void
  removePin: (panelId: string, kind: string, id: string) => void
  clearPins: (panelId: string) => void

  /** New chat — everything about the conversation goes, options stay. */
  reset: (panelId: string) => void
  /** Adopt a prior session wholesale (history sidebar). */
  adoptSession: (panelId: string, detail: ConsultSessionDetail) => void
  /**
   * Apply a server reconcile, unless `expectedRev` is stale — see
   * {@link ConsultPanelState.rev}.
   */
  reconcile: (panelId: string, server: ConsultSessionDetail | null, expectedRev: number) => void
}

/** Apply `mutate` to one panel slice, bump its revision, and persist. */
function updatePanel(
  set: (fn: (s: ConsultSessionsState) => Partial<ConsultSessionsState>) => void,
  panelId: string,
  mutate: (panel: ConsultPanelState) => Partial<ConsultPanelState> | null,
): void {
  set((state) => {
    const current = state.panels[panelId] ?? EMPTY_PANEL
    const patch = mutate(current)
    if (patch === null) return {}
    const next: ConsultPanelState = {
      ...current,
      ...patch,
      rev: current.rev + 1,
      updatedAt: Date.now(),
    }
    const panels = { ...state.panels, [panelId]: next }
    persist(panels)
    return { panels }
  })
}

export const useConsultSessions = create<ConsultSessionsState>((set, get) => ({
  panels: loadInitial(),

  panel: (panelId) => get().panels[panelId] ?? EMPTY_PANEL,

  setDraft: (panelId, draft) => updatePanel(set, panelId, () => ({ draft })),
  setScope: (panelId, scope) => updatePanel(set, panelId, () => ({ scope })),
  setMaxRounds: (panelId, maxRounds) => updatePanel(set, panelId, () => ({ maxRounds })),
  setError: (panelId, error) => updatePanel(set, panelId, () => ({ error })),
  setSessionId: (panelId, sessionId) => updatePanel(set, panelId, () => ({ sessionId })),

  startTurn: (panelId, pending) =>
    updatePanel(set, panelId, (panel) => ({
      transcript: [...panel.transcript, { role: 'user', content: pending.question }],
      pendingTurn: pending,
      draft: '',
      error: null,
    })),

  pushStep: (panelId, requestId, frame) =>
    updatePanel(set, panelId, (panel) => {
      const pending = panel.pendingTurn
      // A frame for a turn that already settled (or for a different turn) is
      // stale relay traffic — drop it rather than resurrecting a pending turn.
      if (!pending || pending.requestId !== requestId) return null
      const next = applyFrame(pending, frame)
      // `applyFrame` returns the SAME object for a frame that changed nothing
      // (an empty `answer_delta`) — don't churn the store for it.
      if (next === pending) return null
      return { pendingTurn: next }
    }),

  completeTurn: (panelId, requestId, turn) =>
    updatePanel(set, panelId, (panel) => {
      const pending = panel.pendingTurn
      // The answer to a turn the operator already reset away, or to an older
      // turn superseded by a newer one — appending it would corrupt the
      // conversation, so it is dropped.
      if (!pending || pending.requestId !== requestId) return null
      return {
        transcript: [...panel.transcript, turn],
        pendingTurn: null,
        error: null,
      }
    }),

  replaceTurn: (panelId, index, turn) =>
    updatePanel(set, panelId, (panel) => {
      const existing = panel.transcript[index]
      // The transcript moved under us (a reconcile, a reset, a new turn) —
      // writing an answer over whatever is at that index now would corrupt it.
      if (!existing || existing.role !== 'assistant') return null
      const transcript = panel.transcript.slice()
      transcript[index] = turn
      return { transcript, error: null }
    }),

  failTurn: (panelId, requestId, error) =>
    updatePanel(set, panelId, (panel) => {
      const pending = panel.pendingTurn
      if (!pending || pending.requestId !== requestId) return null
      return { pendingTurn: null, error }
    }),

  markStalled: (panelId, requestId) =>
    updatePanel(set, panelId, (panel) => {
      const pending = panel.pendingTurn
      if (!pending || pending.requestId !== requestId || pending.stalled) return null
      return { pendingTurn: { ...pending, stalled: true } }
    }),

  dismissPending: (panelId) =>
    updatePanel(set, panelId, (panel) =>
      panel.pendingTurn ? { pendingTurn: null } : null,
    ),

  addPin: (panelId, pin) =>
    updatePanel(set, panelId, (panel) =>
      panel.pins.some((p) => p.kind === pin.kind && p.id === pin.id)
        ? null
        : { pins: [...panel.pins, pin] },
    ),

  removePin: (panelId, kind, id) =>
    updatePanel(set, panelId, (panel) => ({
      pins: panel.pins.filter((p) => !(p.kind === kind && p.id === id)),
    })),

  clearPins: (panelId) => updatePanel(set, panelId, () => ({ pins: [] })),

  // An abandoned turn's POST may still be running. It cannot corrupt the new
  // conversation: `completeTurn` only lands an answer whose request id matches
  // the CURRENT pending turn, and both of these clear it.
  reset: (panelId) =>
    updatePanel(set, panelId, () => ({
      sessionId: null,
      transcript: [],
      pendingTurn: null,
      pins: [],
      error: null,
    })),

  adoptSession: (panelId, detail) =>
    updatePanel(set, panelId, () => ({
      sessionId: detail.id,
      transcript: turnsFromServer(detail),
      pendingTurn: null,
      error: null,
    })),

  reconcile: (panelId, server, expectedRev) =>
    updatePanel(set, panelId, (panel) => {
      // Something changed locally while the round-trip was in the air — most
      // likely the very answer we were reconciling for. The store already has
      // the fresher truth; applying a stale server read would undo it.
      if (panel.rev !== expectedRev) return null
      const merged = reconcileWithServer(panel, server)
      if (
        merged.transcript === panel.transcript &&
        merged.pendingTurn === panel.pendingTurn
      ) {
        return null
      }
      return merged
    }),
}))

/** Subscribe to one panel's slice. Defaulted, so a new panel id just works. */
export function useConsultPanel(panelId: string): ConsultPanelState {
  return useConsultSessions((s) => s.panels[panelId] ?? EMPTY_PANEL)
}

/** Direct (non-React) handle for the async send path. */
export function consultActions(): ConsultSessionsState {
  return useConsultSessions.getState()
}
