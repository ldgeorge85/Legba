/**
 * O-Consult / system.consult — daily-driver consult panel (Piece 1 rework).
 *
 * Wired to the `consult_on_demand` analyst kind (L-178) via the registry
 * proxy at `POST /api/v1/consult` (see
 * `src/legba/data/registry/consult_api.py`).
 *
 * This panel is a multi-turn CHAT surface:
 *   - `mode:'chat'` (default) → the actor answers without writing a finding;
 *     the answer comes back in the envelope (no DB row). History threads
 *     under a server-side session and is re-sent as `messages[]` on each turn.
 *   - Live thinking → before POSTing, the panel mints a `request_id`, opens an
 *     `EventSource` on `/api/v1/consult/stream/<id>?token=...`, and renders each
 *     ReAct step as it streams; the stream closes on the terminal `final` frame.
 *
 * Deep, durable analysis (a long-running task that writes a finding) lives in
 * its OWN `Deep Consult` panel (`system.deep_consult`) — this panel is the
 * lightweight chat surface only.
 *
 * The conversation is NOT component state (GLASS-4)
 * =================================================
 *
 * Transcript, session id, pins, the in-flight turn and its live step ticker all
 * live in `state/consultSession.ts`, keyed by `registration.id`. This panel is
 * a view over that slice.
 *
 * That is a correctness requirement, not tidiness. Dockview destroys a panel's
 * React tree on `api.clear()` — which every layout preset and both Investigate
 * grids call — so with the state held locally, picking a preset silently
 * discarded an in-flight turn along with the whole conversation above it. With
 * the state in the store the unmount costs only the DOM: the `fetch` in `send`
 * is never aborted and its continuation writes through the store, so the answer
 * still lands and is waiting when the panel reopens.
 *
 * Three things follow from that split, and each is load-bearing:
 *
 *   1. **The EventSource is closed on unmount.** It is the one resource that
 *      genuinely cannot outlive the component, so `send` registers it on a ref
 *      and an unmount cleanup closes it. The turn keeps running; only the live
 *      ticker stops, and the steps collected so far stay on the pending turn.
 *   2. **On mount the panel reconciles against the server** via the existing
 *      `loadConsultSession`. Server truth wins for completed turns; the local
 *      pending turn survives only while the server has not recorded its answer
 *      (see `reconcileWithServer`).
 *   3. **A pending turn orphaned by a RELOAD is polled back.** Its `fetch` died
 *      with the old page, so nothing will ever resolve it locally — the panel
 *      re-reads the session until the answer appears, then stops waiting and
 *      says so rather than showing "Consulting…" forever.
 *
 * Step 2 and 3 rest on the registry finishing a turn whose client has gone
 * away. That is proven, not assumed — see
 * `tests/data_pkg/test_consult_disconnect_persistence.py`.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { PanelChrome } from '@/components/PanelChrome'
import {
  listConsultSessions,
  loadConsultSession,
  loadConsultModel,
  saveConsultModel,
  CONSULT_MODEL_OPTIONS,
  type ConsultModel,
  type ConsultSessionSummary,
  type ConsultUncertaintyFloor,
  type ConsultUsage,
  type ConsultWatchMint,
} from '@/lib/api'
import { RecordLink } from '@/components/inspector/RecordLink'
import { Pin, X } from 'lucide-react'
import { useSelection } from '@/state/selection'
import {
  isScopePin,
  pinnedContext,
  questionWithPins,
  turnPinLine,
  turnPinOf,
  useAmbientPin,
} from '@/lib/consultContext'
import {
  consultActions,
  useConsultPanel,
  stripFinalFrame,
  type ChatTurn,
  type StepFrame,
} from '@/state/consultSession'
import type { ProvenanceCensus } from '@/state/consultSession'
import { formatApiError, useConsultTurn } from '@/state/useConsultTurn'
import { casePlayer, isCasePlaying, useCasePlayer } from '@/state/casePlayer'
import { CaseRail } from '@/components/CaseRail'
import { ConsultPointLine } from '@/components/ConsultPointLine'
import { useDriveMode } from '@/state/driveMode'
import { ConsultWritesBlock } from '@/components/ConsultWrites'
import { ConsultTurnStamp } from '@/components/ConsultTurnStamp'
import { useElementWidth } from '@/lib/useElementWidth'
import { selectionKindLabel } from '@/lib/subjectNoun'
import { useConsultFrame } from '@/components/ConsultFrameContext'
import { CitedRecords } from '@/components/CitedRecords'
import { describeOnScreen } from '@/lib/onScreen'
import {
  GROUNDING_CHOICES,
  WINDOW_CHOICES,
  loadGroundingMode,
  loadWindowChoice,
  saveGroundingMode,
  saveWindowChoice,
  windowChoiceWords,
  type GroundingMode,
  type WindowChoiceId,
} from '@/lib/consultContract'
import { ConsultContractBlock } from '@/components/ConsultContract'
import { useWorldState } from '@/v4/world/worldState'
import { spanPresetFor } from '@/lib/mapTime'
import type { PanelProps } from '@/types'

/** Below this tile width the case rail collapses to the step counter + STOP. */
const CASE_RAIL_COMPACT_PX = 380

/**
 * The EVIDENCE FLOOR stamp beside the uncertainty: the server raised the
 * model's own number because too few cited refs resolved to a real record.
 * Printed only when the server stamped it — the stamp is a claim that the
 * floor MOVED the number, and its absence claims nothing.
 */
function UncertaintyFloorStamp({ floor }: { floor: ConsultUncertaintyFloor }) {
  const resolved =
    typeof floor.resolved === 'number'
      ? `${floor.resolved} cited ref${floor.resolved === 1 ? '' : 's'} resolved`
      : 'resolved refs: not measured'
  return (
    <span
      className="font-mono text-label text-amber-400/80"
      data-testid="consult-uncertainty-floor"
      title={`the model said ${floor.from.toFixed(2)}; the evidence floor raised it to ${floor.to.toFixed(2)} (${resolved})`}
    >
      floored from {floor.from.toFixed(2)} · {resolved}
    </span>
  )
}

/**
 * W3 / F-4 — how many of this answer's watch candidates now STAND as open
 * questions `claim_watch` matches new reporting against. Rendered only when
 * the server attempted the mint; its absence claims nothing. A refused
 * candidate is counted beside the standing ones and named, with its reason,
 * on the tooltip — never silently dropped. W4-F1: a candidate that FOLDED onto
 * a watch already standing (same desk, horizon within ±3 days, the same claim)
 * is counted as "K folded"; the tooltip names the watch it folded onto and the
 * claim-core cosine when the server measured one.
 */
function foldNote(m: ConsultWatchMint): string {
  if (!m.folded) {
    return m.fold_check === 'unavailable' ? ' (fold check unavailable)' : ''
  }
  const onto = m.folded_onto ? ` — folded onto ${m.folded_onto.slice(0, 8)}` : ''
  const sim =
    typeof m.similarity === 'number' ? `, claim-core cosine ${m.similarity.toFixed(2)}` : ''
  return ` (already standing${onto}${sim})`
}

function WatchStandingLine({ minted }: { minted: ConsultWatchMint[] }) {
  const standing = minted.filter((m) => !m.refused && m.hypothesis_id)
  const refused = minted.filter((m) => m.refused)
  const folded = standing.filter((m) => m.folded)
  const title = [
    ...standing.map(
      (m) =>
        `watching: ${m.candidate}` +
        (m.resolves_by ? ` — until ${m.resolves_by.slice(0, 10)}` : '') +
        foldNote(m),
    ),
    ...refused.map((m) => `not watched: ${m.candidate} — ${m.reason ?? 'refused'}`),
  ].join('\n')
  return (
    <span
      className="font-mono text-label text-slate-500"
      data-testid="consult-watch-standing"
      title={title}
    >
      {standing.length} {standing.length === 1 ? 'watch' : 'watches'} standing
      {refused.length > 0 && ` · ${refused.length} not watched`}
      {folded.length > 0 && ` · ${folded.length} folded`}
    </span>
  )
}

/**
 * The census line beside the answer header — the count behind "mostly model
 * knowledge". Renders ONLY what the server measured: a class whose count the
 * server could not measure is `null` and is omitted, never printed as 0, and
 * a census with nothing measurable at all renders nothing.
 *
 * REFS and SENTENCES are different units and the line says so on each, so the
 * two halves can never be read as one total.
 */
function ProvenanceCensusLine({ census }: { census: ProvenanceCensus }) {
  const classes: Array<[string, number | null | undefined]> = [
    ['live', census.live],
    ['history', census.history],
    ['web', census.web_retrieval],
    ['seed', census.seed],
  ]
  const shown = classes.filter(([, n]) => typeof n === 'number')
  // 2026-09-28 §B.3 — the three counts that used to be one silence. Each is
  // APPENDED and only when the server measured it: `unresolved` (matched a
  // record whose origin class this census does not read), `fabricated` (a
  // well-formed uuid matching no record at all) and `malformed` (a citation
  // token that was never an id). An unmeasured one is `null` and prints
  // nothing — a `0 fabricated` nobody measured reads as "nothing was
  // invented", which is a claim.
  const unresolved = census.unresolved
  const fabricated = census.fabricated
  const malformed = census.malformed
  const tail: string[] = []
  if (typeof unresolved === 'number' && unresolved > 0) {
    tail.push(`${unresolved} unresolved`)
  }
  if (typeof fabricated === 'number' && fabricated > 0) {
    tail.push(`${fabricated} fabricated`)
  }
  if (Array.isArray(malformed) && malformed.length > 0) {
    tail.push(`${malformed.length} malformed`)
  }
  const mk = census.model_knowledge
  if (!shown.length && typeof mk !== 'number' && !tail.length) return null
  // The by-kind breakdown rides the TOOLTIP rather than the line: the 29 refs
  // the old census filed as unresolved were real findings, situations,
  // hypotheses and events, and naming them is the difference between a number
  // and an explanation. The line stays one line.
  const kinds = census.by_kind
  const kindNote =
    kinds && Object.keys(kinds).length > 0
      ? `resolved by kind: ${Object.entries(kinds)
          .map(([k, n]) => `${n} ${k}`)
          .join(', ')}`
      : null
  const malformedNote =
    Array.isArray(malformed) && malformed.length > 0
      ? `not ids: ${malformed.join(', ')}`
      : null
  const title = [census.basis, kindNote, malformedNote]
    .filter(Boolean)
    .join(' · ')
  return (
    <span
      className="font-mono text-label text-slate-500"
      data-testid="consult-provenance-census"
      title={title || undefined}
    >
      {shown.length > 0 ? (
        <>cited: {shown.map(([label, n]) => `${n} ${label}`).join(' · ')}</>
      ) : (
        <>cited: not measured</>
      )}
      {tail.length > 0 && (
        <span className="text-amber-400/80"> · {tail.join(' · ')}</span>
      )}
      {typeof mk === 'number' && (
        <>
          {' '}
          · model knowledge: {mk} sentence{mk === 1 ? '' : 's'}
        </>
      )}
    </span>
  )
}

const MAX_ROUNDS = 30

/**
 * Why a step that isn't a plain tool call happened, in the operator's words.
 *
 * A round the planner wasted, or a budget the loop hit, is the most useful
 * thing in a trace and used to render as a bare kind name — `unparseable` with
 * no hint of what the model actually said. These are the steps someone reads
 * when a consult disappoints them, so they carry their reason.
 */
function stepReason(s: StepFrame): string | null {
  const kind = String(s.kind ?? '')
  if (kind === 'unparseable') {
    const raw = typeof s.raw === 'string' ? s.raw.replace(/\s+/g, ' ').trim() : ''
    return raw
      ? `the planner's reply matched neither reply shape — "${raw.slice(0, 120)}${raw.length > 120 ? '…' : ''}"`
      : "the planner's reply matched neither reply shape"
  }
  if (kind === 'missing_tool_or_final') {
    return 'the planner emitted JSON with neither a tool call nor an answer'
  }
  if (kind === 'native_text_tool_fallback') {
    return 'the planner answered in the old JSON protocol; ran it as a tool round'
  }
  if (kind === 'wall_budget_reached') {
    return `stopped drilling after ${s.elapsed_s ?? '?'}s — synthesising from what it has`
  }
  if (kind === 'total_budget_reserve_reached') {
    return `time budget nearly spent (${s.remaining_s ?? '?'}s left) — synthesising now`
  }
  if (kind === 'round_deadline_exceeded') {
    return `this round passed its ${s.deadline_s ?? '?'}s deadline`
  }
  if (kind === 'degraded_final') {
    return `answered from partial evidence: ${String(s.reason ?? 'out of budget')}`
  }
  if (kind === 'partial_final') {
    // The answer was CUT, and what there is of it is being delivered. Say how
    // much survived — the difference between "487 chars" and "4,800 chars" is
    // the difference between re-running and reading.
    return `synthesis cut at ${s.chars ?? '?'} chars (${String(
      s.reason ?? 'deadline',
    )}) — delivering the partial`
  }
  if (kind === 'spend_ceiling_reached') {
    return `stopped drilling to stay under the spend ceiling: ${String(
      s.reason ?? 'ceiling reached',
    )}`
  }
  if (kind === 'compaction') {
    return `compacted ${s.bodies_compacted ?? '?'} tool bodies (${
      typeof s.chars_saved === 'number' ? s.chars_saved.toLocaleString() : '?'
    } chars saved)`
  }
  if (kind === 'llm_error' || kind === 'forced_final_error') {
    return 'the model call failed'
  }
  return null
}

/**
 * The live spend readout: what this run has burned, against what it may burn.
 *
 * Both halves always carry their ceiling when there is one. A token count on
 * its own is trivia; `78,400 / 150,000` is a decision. The INPUT tokens are the
 * numerator because `max_input_tokens` is the cap that actually bites — output
 * and call count ride the tooltip rather than muddying the line.
 */
function formatUsage(u: ConsultUsage): string {
  const tokens =
    u.max_input_tokens > 0
      ? `${u.input_tokens.toLocaleString()} / ${u.max_input_tokens.toLocaleString()} tok`
      : `${u.input_tokens.toLocaleString()} tok`
  const cost =
    u.max_cost_usd > 0
      ? `$${u.est_cost_usd.toFixed(2)} / $${u.max_cost_usd.toFixed(2)}`
      : `$${u.est_cost_usd.toFixed(2)}`
  return `${tokens} · ${cost}`
}

/** How far into the tighter of the two ceilings this run already is (0–1+). */
function usageFraction(u: ConsultUsage): number {
  const byTokens = u.max_input_tokens > 0 ? u.input_tokens / u.max_input_tokens : 0
  const byCost = u.max_cost_usd > 0 ? u.est_cost_usd / u.max_cost_usd : 0
  return Math.max(byTokens, byCost)
}

/**
 * Escalate the meter as it closes on a ceiling — the same rose/amber ramp the
 * Budget panel uses, so "this is about to cost you" reads the same everywhere.
 */
function usageTone(u: ConsultUsage): string {
  const pct = usageFraction(u)
  if (pct >= 0.9) return 'text-rose-400'
  if (pct >= 0.75) return 'text-amber-400'
  return 'text-slate-400'
}

/**
 * A cap the operator did not choose is the one worth shouting about.
 *
 * The incident's root cause was exactly this: the run executed under a default
 * of 10 rounds while nobody was looking at the number. `'request'` is the quiet
 * case; a silently substituted default is not.
 */
function roundCapTone(source?: string | null): string {
  if (source === 'default_malformed_request') return 'text-rose-400'
  if (source === 'default') return 'text-amber-400'
  return 'text-slate-400'
}

function roundCapTitle(cap: number, source?: string | null): string {
  const base = `this run drills at most ${cap} tool round(s)`
  if (source === 'default_malformed_request') {
    return `${base} — the requested cap was MALFORMED and the server's default was used instead`
  }
  if (source === 'default') return `${base} — the server's default; the request did not set one`
  if (source === 'request') return `${base} — as requested by this panel`
  return base
}

/** Steps that mean "the answer above is a stump, not an answer". */
const CUT_SYNTHESIS_KINDS = new Set(['degraded_final', 'partial_final'])

/**
 * Can this settled turn be re-synthesised from the evidence behind it?
 *
 * The server says so directly (`synthesis_status`), but turns persisted before
 * that field existed — including every turn in the conversation the incident
 * happened in — can only be recognised by the step that cut them off. Both are
 * honoured, and an explicit `resynthesizable: false` (the evidence has aged
 * out) overrides both: offering a button that can only 404 is worse than
 * offering nothing.
 */
function needsResynthesis(turn: ChatTurn): boolean {
  if (turn.role !== 'assistant') return false
  // Neither address to POST to — nothing to recover from. A turn re-seeded
  // from the session API has no RUN id but does have its own — see
  // `turnsFromServer`.
  if (!turn.requestId && !turn.turnId) return false
  if (turn.resynthesizable === false) return false
  if (turn.synthesisStatus === 'partial' || turn.synthesisStatus === 'none') return true
  if (turn.synthesisStatus === 'complete') return false
  return (turn.steps ?? []).some((s) => CUT_SYNTHESIS_KINDS.has(String(s.kind ?? '')))
}

/** Compact one-line label for a streamed step. */
function stepLabel(s: StepFrame): string {
  const parts: string[] = []
  if (s.phase) parts.push(String(s.phase))
  if (s.kind) parts.push(String(s.kind))
  if (s.tool) parts.push(`tool=${String(s.tool)}`)
  if (typeof s.round === 'number') parts.push(`round=${s.round}`)
  const base = parts.join(' · ') || 'step'
  const reason = stepReason(s)
  return reason ? `${base} — ${reason}` : base
}

export default function ConsultPanel({ registration }: PanelProps) {
  // The store key. `singleton:system.consult` for the ordinary panel — stable
  // across an `api.clear()`, so a re-opened panel rejoins its own conversation.
  const panelId = registration.id
  const panel = useConsultPanel(panelId)
  const { sessionId, transcript, pins, pendingTurn, draft, scope, maxRounds, error } = panel

  // F1 model picker — the LLM plane this chat runs on; persisted across opens
  // under its own key (it is an operator preference, not conversation state).
  const [model, setModel] = useState<ConsultModel>(() => loadConsultModel())
  // History sidebar: prior chat sessions. Deliberately component-local — view
  // chrome SHOULD reset with the panel; only the conversation is durable.
  const [sessions, setSessions] = useState<ConsultSessionSummary[]>([])
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  // U-2 — "Drive the view" is a MODE of Consult (the owner: "made modes in the
  // Consult"), replacing the per-turn "Present as a case": while it is on,
  // every chat turn is sent as a case and its view actions run at once with
  // one Undo; while it is off, a turn is a plain turn and moves nothing
  // (`state/driveMode.ts`). A per-viewer preference, off until chosen.
  const presentCase = useDriveMode((s) => s.on)
  const setPresentCase = useDriveMode((s) => s.setOn)
  // C-2b-1 / D2 — "Allow changes this turn", the same per-turn rule: off by
  // default, reset after every send. Only an armed turn is offered the write
  // tools; destructive changes still wait for the operator's Confirm.
  const [allowWrites, setAllowWrites] = useState(false)
  // U-8 / Q-1 — the window and the grounding mode the turn is asked under,
  // sent on every turn (the server's `window` / `grounding_mode`). A window the
  // question itself states ("last 48 hours") still wins on the server.
  const [windowChoice, setWindowChoice] = useState<WindowChoiceId>(() => loadWindowChoice())
  const [grounding, setGrounding] = useState<GroundingMode>(() => loadGroundingMode())
  const bandSpanMs = useWorldState((s) => s.spanMs)
  const bandSpanText = spanPresetFor(bandSpanMs)?.label ?? `${Math.round(bandSpanMs / 3_600_000)}h`
  // The case player is module state (it must outlive this tile); the tile only
  // reads whether a case of ITS slice is playing.
  const casePlaying = useCasePlayer(
    (s) => s.panelId === panelId && isCasePlaying(s.player),
  )
  const caseRequestId = useCasePlayer((s) =>
    s.panelId === panelId ? (s.player?.requestId ?? null) : null,
  )
  const [tileRef, tileWidth] = useElementWidth<HTMLDivElement>()
  // U-6 — the frame holding this panel: `compact` is the ask bar alone (the
  // conversation and the options fold away; the point, the box, the switch
  // and Send stay). Null when Consult is a plain panel.
  const frame = useConsultFrame()
  const compact = !!frame?.compact

  // Auto-scroll the conversation to the newest turn / live step.
  const scrollRef = useRef<HTMLDivElement | null>(null)

  // Pin-to-context (#90): the operator pins records from the shared selection
  // into a sticky set; every pin is injected into each turn's context (see
  // `send`). Pins live in the store, so they now survive a preset pick too.
  //
  // MANUAL pinning still pins FOCUS — deliberately. The operator holds three
  // findings pinned under one report scope; that is the v2 working posture.
  const selection = useSelection((s) => s.selection)
  const pinSelection = () => {
    if (!selection) return
    consultActions().addPin(panelId, selection)
  }
  const selectionPinned =
    !!selection && pins.some((p) => p.kind === selection.kind && p.id === selection.id)

  // AUTO-PIN THE SCOPE (design §5.2). One ambient pin, tagged
  // `origin:'scope'`, REPLACED on every scope change — never appended, or a
  // morning's navigation would leave a stack of stale scopes poisoning every
  // later turn. Clearing the scope removes it and pins nothing in its place.
  //
  // UI-B6 — the ambient pin follows the READER, not only the wall scope: every
  // object surface claims it through `usePinScope` (`lib/consultContext.ts`),
  // the wall scope is one more claimant, and the newest live claim is the pin.
  const ambient = useAmbientPin()
  const ambientPin = ambient?.pin ?? null
  useEffect(() => {
    const store = consultActions()
    const current = store.panel(panelId).pins
    const existing = current.find(isScopePin)
    const same = (p: { kind: string; id: string }) =>
      !!ambientPin && p.kind === ambientPin.kind && p.id === ambientPin.id
    if (existing && !same(existing)) {
      store.removePin(panelId, existing.kind, existing.id)
    }
    if (ambientPin && (!existing || !same(existing))) {
      store.addPin(panelId, ambientPin)
    }
    // Keyed on the pin's identity: a re-projection of the same object (the
    // Navigator re-affirming its scope) must not churn the pin list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [panelId, ambientPin?.kind, ambientPin?.id])

  // M-1 — the turn itself (send, stream, 202 and reload recovery, STOP and
  // synthesise, the contract) is `state/useConsultTurn`, shared with the
  // phone. This panel supplies what differs: its model, the pins, the frame's
  // on-screen report, the window, grounding, drive mode and write-arming.
  // Called AFTER the ambient-pin effect so its mount reconcile reads the slice
  // the pin effect left, as it always has.
  const consultTurn = useConsultTurn(panelId, { model })
  const { busy, detached, stopPhase, resynthIndex, quiet, closeStream } = consultTurn

  // Keep the conversation pinned to the bottom as turns / steps arrive.
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [transcript, pendingTurn])

  const send = async (mode: 'chat' | 'deep') => {
    const trimmed = draft.trim()
    if (!trimmed || busy) return
    // Inject pinned records two ways: a `[Pinned …]` context prefix on the
    // question (works against today's text-only backend) AND a structured
    // `pinned_context` field a backend can hydrate full record bodies from.
    // Both are built in `lib/consultContext` so Chat and Deep Consult cannot
    // drift apart on what the model is told. The on-screen report is sent only
    // inside the frame.
    const sending = consultTurn.send({
      question: trimmed,
      mode,
      // UI-B6 — what the answer is asked under; its census line prints it.
      pinnedTo: turnPinOf(ambient),
      request: {
        question: questionWithPins(trimmed, pins),
        scopePredicate: scope,
        maxToolRounds: maxRounds,
        pinnedContext: pinnedContext(pins),
        onScreen: frame ? describeOnScreen() : null,
        presentCase,
        allowWrites,
        window: windowChoice,
        band: {
          startMs: useWorldState.getState().windowStartMs,
          endMs: useWorldState.getState().windowEndMs,
        },
        groundingMode: grounding,
      },
    })
    // D1 — "Allow changes" applies to THIS turn only, and resets on send.
    setAllowWrites(false)
    await sending
  }

  const resetChat = () => {
    closeStream()
    consultActions().reset(panelId)
  }

  // Load the prior-session list for the history sidebar.
  const loadHistory = useCallback(async () => {
    setHistoryError(null)
    try {
      const rows = await listConsultSessions({ mode: 'chat', limit: 50 })
      setSessions(rows)
    } catch (err) {
      setHistoryError(formatApiError(err))
    }
  }, [])

  // Open a prior session: re-seed the transcript from its persisted turns and
  // adopt its id so the next turn CONTINUES the conversation server-side.
  const openSession = async (id: string) => {
    if (busy) return
    closeStream()
    try {
      const detail = await loadConsultSession(id)
      consultActions().adoptSession(panelId, detail)
      setHistoryOpen(false)
    } catch (err) {
      consultActions().setError(panelId, formatApiError(err))
    }
  }

  // Refresh the history list when the sidebar opens.
  useEffect(() => {
    if (historyOpen) void loadHistory()
  }, [historyOpen, loadHistory])

  /** What the in-flight block says about itself — the three states differ. */
  const pendingLabel = useMemo(() => {
    if (!pendingTurn) return ''
    if (pendingTurn.stalled) {
      return sessionId
        ? 'Stopped waiting — the server never recorded an answer for this turn.'
        : 'Stopped waiting — this turn was interrupted before it was threaded to a session. Check History.'
    }
    if (detached) {
      // A reattached turn is no longer merely "waiting": the panel now seeds
      // the steps the reload missed and re-opens the live stream, so say what
      // is actually happening. Claiming to wait while rounds visibly arrive
      // below it is the kind of small lie that makes a panel feel broken.
      return pendingTurn.steps.length > 0
        ? `Reattached after a reload — thinking… (${pendingTurn.steps.length} steps)`
        : 'Reattached after a reload — waiting for the server…'
    }
    return `Thinking… (${pendingTurn.steps.length} steps)`
  }, [pendingTurn, detached, sessionId])

  return (
    <PanelChrome
      registration={registration}
      subtitle="consult_on_demand (chat · on-demand via dapr actor)"
      onRefresh={frame ? undefined : resetChat}
    >
      <div className="flex h-full min-h-0 min-w-0" ref={tileRef}>
        {/* History sidebar — prior chat sessions (0038 audit trail). */}
        {historyOpen && !compact && (
          <div
            className="w-56 shrink-0 border-r border-slate-700 pr-2 mr-2 overflow-y-auto min-h-0"
            data-testid="consult-history"
          >
            <div className="flex items-center justify-between mb-2">
              <span className="text-label uppercase tracking-wide text-slate-400">
                Prior chats
              </span>
              <button
                onClick={() => void loadHistory()}
                className="text-label text-slate-400 hover:text-slate-200"
                title="refresh history"
                data-testid="consult-history-refresh"
              >
                ↻
              </button>
            </div>
            {historyError && (
              <div className="text-label text-accent-critical mb-2">{historyError}</div>
            )}
            {sessions.length === 0 && !historyError && (
              <div className="text-label text-slate-500 py-2">No prior chats yet.</div>
            )}
            <ul className="space-y-1" data-testid="consult-history-list">
              {sessions.map((s) => (
                <li key={s.id}>
                  <button
                    onClick={() => void openSession(s.id)}
                    disabled={busy}
                    className={
                      'w-full text-left rounded px-2 py-1 text-xs hover:bg-surface-200 disabled:opacity-50 ' +
                      (s.id === sessionId ? 'bg-surface-200 text-slate-100' : 'text-slate-300')
                    }
                    title={s.title}
                    data-testid="consult-history-item"
                  >
                    <div className="truncate">{s.title || '(untitled)'}</div>
                    <div className="text-label text-slate-500">
                      {s.turn_count} turn{s.turn_count === 1 ? '' : 's'}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="flex flex-col h-full min-h-0 min-w-0 flex-1">
        {/* History toggle + new-chat bar */}
        {!compact && (
        <div className="flex items-center gap-2 mb-2 shrink-0">
          <button
            onClick={() => setHistoryOpen((v) => !v)}
            className="text-label text-ink-3 hover:text-ink-1 rounded px-2 py-0.5 border border-line"
            data-testid="consult-history-toggle"
          >
            {historyOpen ? '◀ Hide history' : '☰ History'}
          </button>
          <button
            onClick={resetChat}
            disabled={busy}
            className="text-label text-ink-3 hover:text-ink-1 rounded px-2 py-0.5 border border-line disabled:opacity-50"
            data-testid="consult-new-chat"
          >
            + New chat
          </button>
          <button
            onClick={pinSelection}
            disabled={!selection || selectionPinned}
            className="ml-auto flex items-center gap-1 text-label text-ink-3 hover:text-ink-1 rounded px-2 py-0.5 border border-line disabled:opacity-50"
            title={
              selection
                ? selectionPinned
                  ? 'already pinned to context'
                  : `pin ${selection.kind} to consult context`
                : 'select a record to pin it'
            }
            data-testid="consult-pin"
          >
            <Pin className="h-3 w-3" />
            {selectionPinned ? 'Pinned' : 'Pin selection'}
          </button>
        </div>
        )}
        {/* Conversation — scrolls; the composer below stays pinned. */}
        <div
          ref={scrollRef}
          className={compact ? 'hidden' : 'flex-1 overflow-y-auto min-h-0 space-y-3 pr-1'}
          data-testid="consult-scroll"
        >
          {transcript.length === 0 && !pendingTurn && (
            <div className="text-label text-ink-3 py-6 text-center">
              Ask anything — answers cite the records they used.
            </div>
          )}

          {transcript.length > 0 && (
            <div className="space-y-3" data-testid="consult-transcript">
              {transcript.map((turn: ChatTurn, i: number) =>
                turn.role === 'user' ? (
                  <div key={i} className="flex justify-end" data-testid="consult-turn-user">
                    <div className="bg-accent-info/20 rounded px-3 py-2 text-sm max-w-[85%] whitespace-pre-wrap">
                      {turn.content}
                    </div>
                  </div>
                ) : (
                  <div key={i} className="space-y-2" data-testid="consult-turn-assistant">
                    <div className="text-label text-slate-400 flex items-center gap-2">
                      <span>Answer</span>
                      {turn.model && (
                        <span
                          className="font-mono text-slate-500"
                          data-testid="consult-answer-model"
                          title="the LLM plane that produced this answer"
                        >
                          via {turn.model}
                        </span>
                      )}
                      {typeof turn.uncertainty === 'number' && (
                        <span className="font-mono text-slate-500">
                          uncertainty={turn.uncertainty.toFixed(2)}
                        </span>
                      )}
                      <ConsultTurnStamp usage={turn.usageReceipt} verification={turn.verification} />
                      {turn.uncertaintyFloor && (
                        <UncertaintyFloorStamp floor={turn.uncertaintyFloor} />
                      )}
                      {turn.provenanceCensus ? (
                        <ProvenanceCensusLine census={turn.provenanceCensus} />
                      ) : (
                        turn.responseRecorded === false && (
                          <span
                            className="font-mono text-label text-slate-500"
                            data-testid="consult-provenance-census"
                            title="this turn was recorded before its census, hold and floor were kept with it (migration 0223)"
                          >
                            cited: not measured
                          </span>
                        )
                      )}
                      {/* UI-B6 — the census reports the PIN the question was
                          asked under (the object the reader was on), beside
                          what the answer rested on. Only for a turn asked in
                          this page: the server does not record the pin. */}
                      {i > 0 && transcript[i - 1].role === 'user' && transcript[i - 1].pinnedTo !== undefined && (
                        <span
                          className="font-mono text-label text-ink-3"
                          data-testid="consult-turn-pin"
                          title="the ambient pin this question was asked under — the object the reader was on"
                        >
                          {turnPinLine(transcript[i - 1].pinnedTo ?? null)}
                        </span>
                      )}
                      {turn.watchMinted && <WatchStandingLine minted={turn.watchMinted} />}
                      {turn.deep && turn.findingId && (
                        <>
                          <span className="font-mono text-emerald-400">durable finding written</span>
                          <span data-testid="consult-finding-link">
                            <RecordLink
                              kind="finding"
                              id={turn.findingId}
                              label={`finding=${turn.findingId.slice(0, 8)}`}
                              origin="consult"
                              mono
                              title="inspect the produced finding"
                            />
                          </span>
                        </>
                      )}
                    </div>
                    {/* 2026-09-28 §B.3 — THE PROVENANCE HOLD. Not one cited
                        reference resolved to live or held reporting, and the
                        prose still asserts. The server already prepended its
                        own banner to the answer (it names the counts, and it
                        travels wherever the answer travels); this frame is so
                        a reader scanning the transcript sees it before they
                        read a word of the answer, not after. */}
                    {turn.holdReason && (
                      <div
                        className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-label text-amber-300"
                        data-testid="consult-provenance-hold"
                      >
                        held — nothing this answer cites resolved to live or held
                        reporting; read it as model knowledge until it is grounded
                      </div>
                    )}
                    <div
                      className={`rounded p-2 text-sm prose prose-invert prose-sm max-w-none ${
                        turn.holdReason
                          ? 'bg-surface-200 border border-amber-500/40'
                          : 'bg-surface-200'
                      }`}
                      data-testid="consult-answer"
                    >
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.content}</ReactMarkdown>
                    </div>
                    {/* U-8 / Q-1 — the turn contract as data: window, coverage,
                        explanations, grounding, cost. Absent parts draw nothing. */}
                    {turn.contract && <ConsultContractBlock contract={turn.contract} usage={turn.usageReceipt} />}
                    {/* A rebuilt transcript is NOT the run the operator paid
                        for. Saying so is the whole point — a recovery that
                        passes itself off as a clean re-run is a lie. */}
                    {turn.replayFidelity && turn.replayFidelity !== 'exact' && (
                      <div
                        className="rounded border border-amber-500/40 bg-amber-500/10 p-2 text-label text-amber-300"
                        data-testid="consult-replay-note"
                      >
                        {turn.replayNote ||
                          (turn.replayFidelity === 'unavailable'
                            ? 'the original transcript could not be recovered — this synthesis read the persisted evidence alone'
                            : 'the transcript this synthesis read was rebuilt, not the original')}
                      </div>
                    )}
                    {/* C-2b-1 — what an armed turn changed, from the ledger. */}
                    {Array.isArray(turn.writes) && (
                      <ConsultWritesBlock
                        sessionId={sessionId}
                        requestId={turn.requestId ?? null}
                        entries={turn.writes}
                      />
                    )}
                    {/* C-1b — a turn that presented a case can replay it: the
                        same executor over the persisted record, with no consult
                        POST and no tool call. */}
                    {turn.caseRecord && (
                      <div
                        className="flex flex-wrap items-center gap-2 text-label text-ink-3"
                        data-testid="consult-case-line"
                      >
                        <span className="font-mono">
                          case · {turn.caseRecord.status} · {turn.caseRecord.actions.length} step
                          {turn.caseRecord.actions.length === 1 ? '' : 's'}
                          {turn.caseRecord.as_of
                            ? ` · recorded ${turn.caseRecord.as_of}`
                            : ' · as-of not recorded'}
                          {!turn.caseRecord.complete && turn.caseRecord.gap_at_seq !== null
                            ? ` · hole at step ${turn.caseRecord.gap_at_seq}`
                            : ''}
                        </span>
                        <button
                          onClick={() => casePlayer().replay(turn.caseRecord!, panelId)}
                          disabled={casePlaying || turn.caseRecord.actions.length === 0}
                          className="rounded border border-line px-2 py-0.5 text-ink-2 hover:text-ink-1 disabled:opacity-50"
                          data-testid="consult-case-replay"
                          title="replay this case without the model — panels show the records as they stand now"
                        >
                          Replay
                        </button>
                      </div>
                    )}
                    {needsResynthesis(turn) && (
                      <div className="flex items-center gap-2 flex-wrap">
                        <button
                          onClick={() => void consultTurn.resynthesize(i, turn)}
                          disabled={resynthIndex !== null}
                          className="text-label rounded px-2 py-0.5 border border-amber-500/40 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20 disabled:opacity-50"
                          data-testid="consult-resynthesize"
                          title="re-run ONLY the final synthesis over the evidence this run already gathered — no new drilling, no new tool calls"
                        >
                          {resynthIndex === i ? 'Synthesizing…' : 'Synthesize from evidence'}
                        </button>
                        <span className="text-label text-slate-500">
                          this answer was cut short — the evidence behind it is still on the
                          server
                        </span>
                      </div>
                    )}
                    {turn.unansweredAspects && turn.unansweredAspects.length > 0 && (
                      <div>
                        <div className="text-label text-slate-400 mb-1">Unanswered aspects</div>
                        <ul className="text-xs space-y-1 list-disc list-inside text-amber-300">
                          {turn.unansweredAspects.map((u, j) => (
                            <li key={j}>{u}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {turn.steps && turn.steps.length > 0 && (
                      <details className="bg-surface-200 rounded p-2">
                        <summary className="text-label text-slate-400 cursor-pointer">
                          Thinking ({turn.steps.length} steps)
                        </summary>
                        <ul className="text-label font-mono mt-2 space-y-0.5 text-slate-400">
                          {turn.steps.map((s, j) => (
                            <li key={j}>{stepLabel(s)}</li>
                          ))}
                        </ul>
                      </details>
                    )}
                    {turn.toolCalls && turn.toolCalls.length > 0 && (
                      <details className="bg-surface-200 rounded p-2">
                        <summary className="text-label text-slate-400 cursor-pointer">
                          Tool calls ({turn.toolCalls.length})
                        </summary>
                        <pre className="text-label font-mono mt-2 overflow-x-auto">
                          {JSON.stringify(turn.toolCalls, null, 2)}
                        </pre>
                      </details>
                    )}
                    {turn.citedRefs && turn.citedRefs.length > 0 && (
                      <CitedRecords refs={turn.citedRefs} toolCalls={turn.toolCalls} />
                    )}
                  </div>
                ),
              )}
            </div>
          )}

          {/* The in-flight turn — survives an unmount, so this block is drawn
              from the store and can outlive the component that started it. */}
          {pendingTurn && (
            <div
              className="bg-surface-200 rounded p-2 border border-slate-700"
              data-testid="consult-live-steps"
            >
              <div className="flex items-center gap-2 mb-1 flex-wrap">
                <div
                  className={
                    'text-label ' +
                    (pendingTurn.stalled ? 'text-amber-300' : 'text-slate-400')
                  }
                  data-testid="consult-pending-label"
                >
                  {pendingLabel}
                </div>
                {/* The cap this run is ACTUALLY under. An unnoticed default of
                    10 is what turned one question into a ~$10 bill. */}
                {typeof pendingTurn.roundCap === 'number' && (
                  <span
                    className={
                      'font-mono text-label ' + roundCapTone(pendingTurn.roundsSource)
                    }
                    data-testid="consult-round-cap"
                    title={roundCapTitle(pendingTurn.roundCap, pendingTurn.roundsSource)}
                  >
                    {pendingTurn.roundCap} rounds
                  </span>
                )}
                {/* Live spend, against its ceilings, escalating as it closes. */}
                {pendingTurn.usage && (
                  <span
                    className={'font-mono text-label ' + usageTone(pendingTurn.usage)}
                    data-testid="consult-usage"
                    title={`${pendingTurn.usage.calls} model call(s) · ${pendingTurn.usage.input_tokens.toLocaleString()} in / ${pendingTurn.usage.output_tokens.toLocaleString()} out tokens · estimated $${pendingTurn.usage.est_cost_usd.toFixed(
                      2,
                    )} so far`}
                  >
                    {formatUsage(pendingTurn.usage)}
                  </span>
                )}
                {pendingTurn.stalled && (
                  <button
                    onClick={() => consultActions().dismissPending(panelId)}
                    className="ml-auto text-label text-slate-400 hover:text-slate-200 underline underline-offset-2"
                    data-testid="consult-pending-dismiss"
                  >
                    Dismiss
                  </button>
                )}
              </div>
              {(pendingTurn.steps.length > 0 || quiet) && (
                <ul className="text-label font-mono space-y-0.5 text-slate-400 max-h-40 overflow-y-auto">
                  {pendingTurn.steps.map((s, i) => (
                    <li key={i}>{stepLabel(s)}</li>
                  ))}
                  {quiet && (
                    <li
                      className="text-sky-300"
                      data-testid="consult-quiet-round"
                      data-synthesising={quiet.synthesising ? 'true' : 'false'}
                      title="No step for this long, but the stream's keepalive says the run is still going — the counter is the silence since the last frame."
                    >
                      {quiet.text}
                    </li>
                  )}
                </ul>
              )}
              {/* The answer as it is written. This text used to be thrown away
                  when a synthesis ran out of budget; now the operator has read
                  most of it before the server decides whether it finished. It
                  is replaced by the settled answer the moment the turn lands. */}
              {pendingTurn.answerPreview && (
                <div className="mt-2">
                  <div className="text-label uppercase tracking-wide text-slate-500 mb-1">
                    Answer forming
                  </div>
                  <div
                    className="bg-surface-200 rounded p-2 text-sm prose prose-invert prose-sm max-w-none"
                    data-testid="consult-answer-live"
                  >
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {stripFinalFrame(pendingTurn.answerPreview)}
                    </ReactMarkdown>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Composer — pinned chat bar at the bottom */}
        <div className={compact ? 'shrink-0 space-y-1.5' : 'shrink-0 border-t border-line pt-2 mt-2 space-y-2'}>
          {/* C-1b — the case rail. In the pinned composer region, never in the
              scrolling transcript, so its STOP cannot scroll out of view. */}
          {caseRequestId && (
            <CaseRail
              panelId={panelId}
              compact={tileWidth > 0 && tileWidth < CASE_RAIL_COMPACT_PX}
              held={transcript.some(
                (t) => t.caseRecord?.request_id === caseRequestId && !!t.holdReason,
              )}
            />
          )}
          {/* Pinned context — injected into every turn until cleared (#90). */}
          {pins.length > 0 && !compact && (
            <div className="flex flex-wrap items-center gap-1" data-testid="consult-pins">
              <span className="text-label uppercase tracking-wide text-ink-3 mr-1">
                Context
              </span>
              {pins.map((p) => (
                <span
                  key={`${p.kind}:${p.id}`}
                  className="flex items-center gap-1 rounded bg-accent-info/15 border border-accent-info/30 px-1.5 py-0.5 text-label text-ink-1"
                  title={`${selectionKindLabel(p.kind)} ${p.id}`}
                  data-testid="consult-pin-chip"
                >
                  <span className="text-ink-3">{selectionKindLabel(p.kind)}:</span>
                  <span className="truncate max-w-[140px]">{p.label ?? p.id}</span>
                  <button
                    onClick={() => consultActions().removePin(panelId, p.kind, p.id)}
                    className="text-ink-3 hover:text-ink-1"
                    title="unpin"
                    aria-label="unpin"
                  >
                    <X className="h-2.5 w-2.5" />
                  </button>
                </span>
              ))}
              <button
                onClick={() => consultActions().clearPins(panelId)}
                className="text-label text-ink-3 hover:text-ink-1 underline underline-offset-2 ml-1"
                data-testid="consult-pins-clear"
              >
                Clear
              </button>
            </div>
          )}
          {error && (
            <div
              className="bg-accent-critical/10 border border-accent-critical/40 rounded p-2 text-xs text-accent-critical whitespace-pre-wrap"
              data-testid="consult-error"
            >
              {error}
            </div>
          )}
          {/* F1 model picker — which LLM plane answers this chat. */}
          <div className={compact ? 'hidden' : 'flex items-center gap-2 text-label text-ink-3'}>
            <label htmlFor="consult-model" className="shrink-0">
              Model
            </label>
            <select
              id="consult-model"
              value={model}
              onChange={(e) => {
                const m = e.target.value as ConsultModel
                setModel(m)
                saveConsultModel(m)
              }}
              className="bg-surface-200 border border-line rounded px-1.5 py-0.5 text-label"
              data-testid="consult-model"
            >
              {CONSULT_MODEL_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          {/* X1/D1 — the question box owns its row. It used to share one
              flex row with three buttons that never shrink, so in a 26 %
              dock the textarea came out ~34 px wide. Now it is the ONLY
              child of its row at every dock width, and the per-turn toggles
              and Send sit under it in a row that wraps instead of squeezing. */}
          {/* U-2 — what the next question is asked ABOUT, above the box. */}
          <ConsultPointLine pinned={pins.length} windowWords={windowChoiceWords(windowChoice, bandSpanText)} />
          <div className="w-full" data-testid="consult-question-row">
            <textarea
              className="block w-full min-w-0 bg-surface-200 border border-line rounded p-2 text-sm resize-none"
              rows={compact ? 1 : 2}
              value={draft}
              onChange={(e) => consultActions().setDraft(panelId, e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  void send('chat')
                }
              }}
              placeholder="Ask a question…  (Enter to send · Shift+Enter for a new line)"
              data-testid="consult-question"
            />
          </div>
          <div
            className="flex flex-wrap items-center gap-2"
            data-testid="consult-composer-controls"
          >
            {/* U-2 — the DRIVE MODE, a switch that stays where it is set. On:
                what an answer pulls up (open, focus, window, map) runs at once,
                listed in the case rail with STOP and one Undo. Off: a plain
                turn that moves nothing. A change to the system is never a view
                action and always waits for your Confirm. */}
            <button
              type="button"
              role="switch"
              onClick={() => setPresentCase(!presentCase)}
              disabled={busy || casePlaying}
              aria-checked={presentCase}
              className={
                'flex items-center gap-1.5 px-2 py-1.5 text-label rounded-control border shrink-0 disabled:opacity-50 ' +
                (presentCase ? 'border-point/60 text-ink-1' : 'border-line text-ink-3 hover:text-ink-1')
              }
              data-testid="consult-drive-toggle"
              aria-describedby="consult-drive-hint"
              title={
                presentCase
                  ? 'Drive the view is ON — what an answer pulls up runs at once; Undo in the case strip puts the view back'
                  : 'Drive the view is OFF — answers move nothing; turn it on to let an answer open, focus and set the window, with one Undo'
              }
            >
              <span
                aria-hidden
                className={
                  'inline-block h-2 w-2 rounded-full ' + (presentCase ? 'bg-point' : 'border border-ink-3')
                }
              />
              Drive the view: {presentCase ? 'on' : 'off'}
            </button>
            {/* C-2b-1 / D2 — opt-in per turn, beside Send; off by default,
                reset on every send. While on, this turn may change the
                platform through consult — destructive changes still wait for
                your Confirm, and every change gets a receipt with Revert. */}
            <button
              type="button"
              onClick={() => setAllowWrites((v) => !v)}
              disabled={busy}
              aria-pressed={allowWrites}
              className={
                'px-2 py-1.5 text-label rounded-control border shrink-0 disabled:opacity-50 ' +
                (allowWrites
                  ? 'border-amber-500/60 bg-amber-500/20 text-ink-1'
                  : 'border-line text-ink-3 hover:text-ink-1')
              }
              data-testid="consult-allow-writes"
              title={
                allowWrites
                  ? 'this turn may change the platform — pause, retire and activate wait for your Confirm; every change gets a receipt with Revert'
                  : 'allow consult to carry out a change you ask for, on this turn only'
              }
            >
              Allow changes this turn
            </button>
            {/* U-8 / Q-1 — the window this turn is asked over (default: the
                server's declared 7 days) and how strictly it must rest on rows. */}
            <label className="flex items-center gap-1 text-label text-ink-3 shrink-0">
              <span>Window</span>
              <select
                value={windowChoice}
                onChange={(e) => {
                  const v = e.target.value as WindowChoiceId
                  setWindowChoice(v)
                  saveWindowChoice(v)
                }}
                disabled={busy}
                className="bg-surface-200 border border-line rounded px-1 py-0.5 text-label"
                data-testid="consult-window"
                title="the window the answer is held to; a window your question states (last 48 hours, say) wins"
              >
                {WINDOW_CHOICES.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.id === 'band' ? `${c.label} (${bandSpanText})` : c.label}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              role="switch"
              aria-checked={grounding === 'record_only'}
              onClick={() => {
                const v: GroundingMode = grounding === 'record_only' ? 'default' : 'record_only'
                setGrounding(v)
                saveGroundingMode(v)
              }}
              disabled={busy}
              className={
                'px-2 py-1.5 text-label rounded-control border shrink-0 disabled:opacity-50 ' +
                (grounding === 'record_only' ? 'border-point/60 text-ink-1' : 'border-line text-ink-3 hover:text-ink-1')
              }
              data-testid="consult-grounding"
              title={GROUNDING_CHOICES.find((g) => g.id === grounding)?.hint}
            >
              Record only: {grounding === 'record_only' ? 'on' : 'off'}
            </button>
            <button
              onClick={() => send('chat')}
              disabled={busy || !draft.trim()}
              className="ml-auto px-4 py-1.5 text-body border border-point/60 text-ink-1 hover:bg-point/15 disabled:opacity-50 rounded-control shrink-0"
              data-testid="consult-submit"
            >
              {busy ? 'Consulting…' : 'Send'}
            </button>
            {/* Only while something is in flight, and never gated on `busy` —
                this is the control that ENDS the spending, so it must work
                exactly when everything else is locked. */}
            {pendingTurn && (
              <button
                onClick={() => void consultTurn.stop()}
                disabled={stopPhase !== null}
                className="px-3 py-2 text-sm bg-accent-critical/20 hover:bg-accent-critical/40 border border-accent-critical/40 text-accent-critical disabled:opacity-50 rounded shrink-0"
                data-testid="consult-stop"
                title="stop drilling now and answer from the evidence gathered so far"
              >
                {stopPhase === 'stopping'
                  ? 'Stopping…'
                  : stopPhase === 'synthesizing'
                    ? 'Synthesizing…'
                    : 'Stop'}
              </button>
            )}
          </div>
          {/* U-3 — what the switch does, in one plain line (the owner: "if
              toggled on, do it with an undo; if not, not"). Off is the default. */}
          <p id="consult-drive-hint" className={compact ? 'sr-only' : 'text-label text-ink-3'} data-testid="consult-drive-hint">
            {presentCase
              ? 'On: an answer may open panels, focus a record and set the window for you — Undo puts the view back.'
              : 'Off: answers only reply; nothing on screen moves. Turn on to let an answer move the view, with Undo.'}
          </p>
          <details className={compact ? 'hidden' : 'text-xs text-ink-3'}>
            <summary className="cursor-pointer select-none">Options</summary>
            <div className="mt-2 space-y-2">
              <input
                className="w-full bg-surface-200 border border-line rounded p-2 text-xs font-mono"
                value={scope}
                onChange={(e) => consultActions().setScope(panelId, e.target.value)}
                placeholder='scope predicate (optional) — target.id == "brazil"'
                data-testid="consult-scope"
              />
              <label className="flex items-center gap-2">
                max tool rounds:
                <input
                  type="number"
                  min={1}
                  max={MAX_ROUNDS}
                  value={maxRounds}
                  onChange={(e) =>
                    consultActions().setMaxRounds(
                      panelId,
                      Math.max(1, Math.min(MAX_ROUNDS, Number(e.target.value) || 1)),
                    )
                  }
                  className="w-16 bg-surface-200 border border-line rounded px-1 py-0.5 text-xs font-mono"
                  data-testid="consult-max-rounds"
                />
              </label>
              <p className="text-label text-ink-3">
                Deep, durable analysis (a long-running task that writes a finding) lives in its own{' '}
                <span className="text-ink-3 font-medium">Deep Consult</span> panel.
              </p>
            </div>
          </details>
        </div>
        </div>
      </div>
    </PanelChrome>
  )
}
