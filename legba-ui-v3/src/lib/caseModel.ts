/**
 * caseModel — "consult presents a case", the PURE half.
 *
 * A case is an ordered list of governed workstation actions one consult turn
 * presents: open a panel, arrange the wall, set the scope, focus a record, move
 * the map, move the clock, run a mission, narrate, done. This module owns
 * everything about a case that needs no store and no DOM:
 *
 *   * the WIRE SHAPES — the canonical action, the persisted case record
 *     (`case/1`, stored on the turn's response bag), and the four new step
 *     frame kinds that ride the existing consult stream;
 *   * the FRAME → ACTION projection, defensive on every field (the relay is
 *     best-effort, and a frame we cannot read is shown as unreadable, never
 *     guessed at);
 *   * the CLOSED VOCABULARY the shell may be driven with, and the client-side
 *     refusal of anything outside it (defence in depth — the server's case gate
 *     refuses first);
 *   * the PLAYER STATE MACHINE — live, replay, stop latch, gap pause, the
 *     terminal-record reconcile — as plain reducers over plain data;
 *   * the SNAPSHOT shape Restore returns the wall to.
 *
 * Absence is absence: a step resting on no read says "no read", a record that
 * was never measured is null, and nothing here ever writes a zero for a count
 * it did not take.
 */

import type { ScopeKind } from '@/state/scope'
import type { Selection } from '@/state/selection'
import type { Scope } from '@/state/scope'
import type { WorldLayer } from '@/v4/world/worldState'
import vocabularyJson from '@/lib/workstationVocabulary.json'

// ---------------------------------------------------------------------------
// The closed vocabulary — one file, two readers.
// ---------------------------------------------------------------------------

/** The nine actions. Closed: anything else is refused. */
export const CASE_ACTIONS = [
  'open_panel',
  'arrange',
  'set_scope',
  'focus',
  'map',
  'timeline',
  'run_mission',
  'narrate',
  'done',
] as const
export type CaseActionName = (typeof CASE_ACTIONS)[number]

export function isCaseActionName(v: unknown): v is CaseActionName {
  return typeof v === 'string' && (CASE_ACTIONS as readonly string[]).includes(v)
}

/**
 * Q-1 — the TENTH drive tool, `panel_action`: one console view driven through
 * its own declared contract (`panel-registry/contracts.ts`, the source the
 * server's generated catalogue `panel_action_catalog.json` is read from).
 * Deliberately OUTSIDE the frozen nine, exactly as the server keeps it outside
 * `case/1`'s contract document (`consult_case_schema.PANEL_ACTION_SPEC`), so
 * every recorded case replays as before. Its args are `{view, action, args}`;
 * the executor routes it to `applyPanelAction`, whose own checks refuse an
 * unknown view, an action the view does not accept, or a bad argument — by
 * name, on the rail, never silently.
 */
export const PANEL_ACTION = 'panel_action' as const
export type DriveActionName = CaseActionName | typeof PANEL_ACTION

export function isDriveActionName(v: unknown): v is DriveActionName {
  return v === PANEL_ACTION || isCaseActionName(v)
}

/**
 * What the shell may be driven with — kinds, stances, presets, layers,
 * missions and the scope kinds. Exported by the backend's leaf
 * (`src/legba/data/workstation_vocabulary.py`) as
 * `lib/workstationVocabulary.json`; the server's drift test keeps it equal to
 * the TS sources, and its hash is stamped on every record.
 */
export interface CaseVocabulary {
  version: string
  /** sha256 of the canonical vocabulary — the one every record is stamped with. */
  hash: string | null
  drivableKinds: readonly string[]
  /** Bound kinds → the ref kind their `subject` must be. */
  subjectKind: Readonly<Record<string, string>>
  /** Kind → the tabs it may be opened on. */
  tabs: Readonly<Record<string, readonly string[]>>
  stances: readonly string[]
  presets: readonly string[]
  /** The map surface this profile drives, or null — `map` then refuses `no_map_surface`. */
  mapSurfaceKind: string | null
  layers: readonly string[]
  missions: readonly string[]
  scopeKinds: readonly string[]
  /** Rounds a case turn defaults to (`limits.case_rounds_default`); null = not declared. */
  caseRoundsDefault: number | null
}

function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

/**
 * Read the exported vocabulary document
 * (`{vocabulary: {...}, vocabulary_hash}` — `workstation_vocabulary.export_document`).
 * An absent axis is EMPTY, which refuses everything on it: the safe reading of
 * a vocabulary that does not name something.
 */
export function vocabularyFrom(raw: unknown): CaseVocabulary {
  const doc = obj(raw)
  const v = obj(doc.vocabulary)
  const kinds = obj(v.drivable_kinds)
  const subjectKind: Record<string, string> = {}
  const tabs: Record<string, string[]> = {}
  for (const [k, spec] of Object.entries(kinds)) {
    const s = obj(spec)
    if (typeof s.subject === 'string') subjectKind[k] = s.subject
    const t = strings(s.tabs)
    if (t.length) tabs[k] = t
  }
  const layouts = obj(v.layouts)
  const ofType = (type: string) =>
    Object.entries(layouts)
      .filter(([, spec]) => obj(spec).type === type)
      .map(([id]) => id)
  return {
    version: typeof v.version === 'string' ? v.version : 'unknown',
    hash: typeof doc.vocabulary_hash === 'string' ? doc.vocabulary_hash : null,
    drivableKinds: Object.keys(kinds),
    subjectKind,
    tabs,
    stances: ofType('stance'),
    presets: ofType('preset'),
    mapSurfaceKind: typeof v.map_surface_kind === 'string' ? v.map_surface_kind : null,
    layers: strings(v.map_layers),
    missions: Object.keys(obj(v.missions)),
    scopeKinds: strings(v.scope_kinds),
    caseRoundsDefault:
      typeof obj(v.limits).case_rounds_default === 'number'
        ? (obj(v.limits).case_rounds_default as number)
        : null,
  }
}

/**
 * The tool-round cap a turn is sent with. A case turn defaults to the
 * vocabulary's case default (20) — but only when the operator left the cap at
 * the panel's default; a cap they CHOSE is never raised behind their back.
 */
export function roundsForTurn(
  chosen: number,
  panelDefault: number,
  asCase: boolean,
  vocab: CaseVocabulary = CASE_VOCABULARY,
): number {
  if (!asCase || chosen !== panelDefault || vocab.caseRoundsDefault === null) return chosen
  return vocab.caseRoundsDefault
}

/** The vocabulary this bundle was built with. */
export const CASE_VOCABULARY: CaseVocabulary = vocabularyFrom(vocabularyJson)

/** Client-side allowlist check (D3) — the second of two refusals. */
export function isDrivableKind(kind: string, vocab: CaseVocabulary = CASE_VOCABULARY): boolean {
  return vocab.drivableKinds.includes(kind)
}

// ---------------------------------------------------------------------------
// Wire shapes.
// ---------------------------------------------------------------------------

/** A ref the action rests on — `{kind, id}`, the read set's own type. */
export interface CaseRef {
  kind: string
  id: string
}

/** Where the gate found a cite in the run's read set. Server-stamped only. */
export interface CaseRestsOn {
  round: number
  tool: string
}

/** One canonical, gate-admitted action. */
export interface CaseAction {
  seq: number
  action: DriveActionName
  args: Record<string, unknown>
  /** ISO instant the gate admitted it. */
  at: string | null
  cites: CaseRef[]
  /** One per cite, stamped by the gate — the model never supplies provenance. */
  rests_on: CaseRestsOn[]
}

export type CaseStatus = 'sealed' | 'unsealed' | 'stopped'

/** The persisted record — `response.case` on the turn. */
export interface CaseRecord {
  version: string
  request_id: string | null
  status: CaseStatus
  as_of: string | null
  plane: string | null
  vocabulary: string | null
  /** How many actions the gate refused. Null = not measured, never 0. */
  refused: number | null
  stopped_after_seq: number | null
  complete: boolean
  gap_at_seq: number | null
  /** The `done` summary; null when the case was not sealed with one. */
  summary: string | null
  actions: CaseAction[]
}

function refsOf(v: unknown): CaseRef[] {
  if (!Array.isArray(v)) return []
  const out: CaseRef[] = []
  for (const c of v) {
    if (!c || typeof c !== 'object') continue
    const r = c as Record<string, unknown>
    if (typeof r.kind === 'string' && typeof r.id === 'string' && r.id) {
      out.push({ kind: r.kind, id: r.id })
    }
  }
  return out
}

function restsOnOf(v: unknown): CaseRestsOn[] {
  if (!Array.isArray(v)) return []
  const out: CaseRestsOn[] = []
  for (const c of v) {
    if (!c || typeof c !== 'object') continue
    const r = c as Record<string, unknown>
    if (typeof r.round === 'number' && typeof r.tool === 'string') {
      out.push({ round: r.round, tool: r.tool })
    }
  }
  return out
}

function numOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null
}

/**
 * Read one canonical action. Returns null for anything that is not one — a
 * missing or non-integer `seq`, an action name outside the nine and Q-1's
 * `panel_action`. The caller renders such a frame as unreadable; it is never
 * executed.
 */
export function parseCaseAction(raw: unknown): CaseAction | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const seq = r.seq
  if (typeof seq !== 'number' || !Number.isInteger(seq) || seq < 1) return null
  const name = typeof r.action === 'string' ? r.action : typeof r.tool === 'string' ? r.tool : null
  if (!isDriveActionName(name)) return null
  const args =
    r.args && typeof r.args === 'object' && !Array.isArray(r.args)
      ? (r.args as Record<string, unknown>)
      : {}
  return {
    seq,
    action: name,
    args,
    at: strOrNull(r.at),
    cites: refsOf(r.cites),
    rests_on: restsOnOf(r.rests_on),
  }
}

/**
 * Read the persisted record off a response bag. `null` means "this turn
 * presented no case" — the bag's own contract — and so does anything that is
 * not a record. Actions are sorted by `seq`; an unreadable one is dropped and
 * its seq becomes a gap the player stops at, never a step it skips silently.
 */
export function parseCaseRecord(raw: unknown): CaseRecord | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  if (!Array.isArray(r.actions)) return null
  const actions = r.actions
    .map(parseCaseAction)
    .filter((a): a is CaseAction => a !== null)
    .sort((a, b) => a.seq - b.seq)
  const status: CaseStatus =
    r.status === 'sealed' || r.status === 'stopped' ? r.status : 'unsealed'
  let complete = r.complete !== false
  let gapAt = numOrNull(r.gap_at_seq)
  // The record's own seq must run 1..n. A hole the server did not flag (an
  // action this client could not read) is flagged here, the same way.
  const hole = firstGap(actions.map((a) => a.seq))
  if (hole !== null && (gapAt === null || hole < gapAt)) {
    gapAt = hole
    complete = false
  }
  return {
    version: typeof r.version === 'string' ? r.version : 'case/1',
    request_id: strOrNull(r.request_id),
    status,
    as_of: strOrNull(r.as_of),
    plane: strOrNull(r.plane),
    vocabulary: strOrNull(r.vocabulary),
    refused: numOrNull(r.refused),
    stopped_after_seq: numOrNull(r.stopped_after_seq),
    complete,
    gap_at_seq: complete ? null : gapAt,
    summary: strOrNull(r.summary),
    actions,
  }
}

/** The first seq missing from 1..max, or null when the run is contiguous. */
export function firstGap(seqs: readonly number[]): number | null {
  const sorted = [...new Set(seqs)].sort((a, b) => a - b)
  let expect = 1
  for (const s of sorted) {
    if (s !== expect) return expect
    expect += 1
  }
  return null
}

/**
 * The last seq a replay may run: never past a stop, never into a hole.
 * `Infinity` when the record bounds nothing.
 */
export function replayCeiling(rec: CaseRecord): number {
  let ceil = Infinity
  if (rec.stopped_after_seq !== null) ceil = Math.min(ceil, rec.stopped_after_seq)
  if (!rec.complete && rec.gap_at_seq !== null) ceil = Math.min(ceil, rec.gap_at_seq - 1)
  return ceil
}

// ---------------------------------------------------------------------------
// Frames — the four new step kinds on the existing stream.
// ---------------------------------------------------------------------------

export const CASE_FRAME_KINDS = ['case_action', 'case_refused', 'case_sealed', 'case_stopped'] as const
export type CaseFrameKind = (typeof CASE_FRAME_KINDS)[number]

export type CaseFrame =
  | { kind: 'case_action'; action: CaseAction | null; raw: Record<string, unknown> }
  | { kind: 'case_refused'; tool: string | null; cause: string; detail: string | null }
  | { kind: 'case_sealed'; summary: string | null }
  | { kind: 'case_stopped'; stoppedAfterSeq: number | null }

export function isCaseFrameKind(v: unknown): v is CaseFrameKind {
  return typeof v === 'string' && (CASE_FRAME_KINDS as readonly string[]).includes(v)
}

/**
 * Project a step frame onto a case frame, or null when it is not one.
 *
 * The canonical action may ride FLAT on the frame (`{type:'step',
 * kind:'case_action', seq, action, args, …}`) or nested under `action` /
 * `case_action`; both are read, so the frame shape can settle without a client
 * change. A `case_action` whose body cannot be read carries `action: null` and
 * is rendered as unreadable — never executed.
 */
export function caseFrameOf(frame: Record<string, unknown>): CaseFrame | null {
  const kind = frame.kind
  if (!isCaseFrameKind(kind)) return null
  switch (kind) {
    case 'case_action': {
      const nested =
        frame.case_action && typeof frame.case_action === 'object'
          ? frame.case_action
          : frame.action && typeof frame.action === 'object'
            ? frame.action
            : null
      return { kind, action: parseCaseAction(nested ?? frame), raw: frame }
    }
    case 'case_refused':
      return {
        kind,
        tool: strOrNull(frame.tool) ?? strOrNull(frame.action),
        cause: strOrNull(frame.cause) ?? 'refused',
        detail: strOrNull(frame.detail),
      }
    case 'case_sealed':
      return { kind, summary: strOrNull(frame.summary) }
    case 'case_stopped':
      return { kind, stoppedAfterSeq: numOrNull(frame.stopped_after_seq) }
  }
}

// ---------------------------------------------------------------------------
// What the rail prints for one step.
// ---------------------------------------------------------------------------

/** The step's census line: what it rests on, in its own unit. */
export function restsOnLine(a: Pick<CaseAction, 'rests_on' | 'action'>): string {
  // Round 0 is the operator's own context (`pinned` / `scope`), not a read the
  // run made — counted beside the reads, never folded into them.
  const reads = a.rests_on.filter((r) => r.round > 0).length
  const pins = a.rests_on.length - reads
  if (reads === 0 && pins === 0) return 'no read'
  const parts: string[] = []
  if (reads > 0) parts.push(`${reads} read${reads === 1 ? '' : 's'}`)
  if (pins > 0) parts.push(`${pins} pin${pins === 1 ? '' : 's'}`)
  return `rests on ${parts.join(' + ')}`
}

/** A short human sentence for an action — the rail's step label. */
export function actionLabel(a: CaseAction): string {
  const g = a.args
  const s = (k: string) => (typeof g[k] === 'string' ? (g[k] as string) : null)
  switch (a.action) {
    case 'open_panel': {
      const subj = refArg(g.params && typeof g.params === 'object' ? (g.params as Record<string, unknown>).subject : null)
      return `open ${s('kind') ?? '?'}${subj ? ` · ${subj.kind} ${shortId(subj.id)}` : ''}`
    }
    case 'arrange':
      return `arrange ${s('layout') ?? '?'}`
    case 'set_scope':
      return s('kind') === 'none' ? 'clear scope' : `scope to ${s('kind') ?? '?'} ${shortId(s('id') ?? '?')}`
    case 'focus': {
      const r = refArg(g.ref)
      return r ? `focus ${r.kind} ${shortId(r.id)}` : 'focus ?'
    }
    case 'map': {
      const layers = strings(g.layers)
      const view = s('view') ?? '?'
      const r = refArg(g.ref)
      return `map ${view === 'fit_ref' && r ? `fit ${r.kind} ${shortId(r.id)}` : view}${layers.length ? ` · ${layers.join(', ')}` : ''}`
    }
    case 'timeline': {
      const w = windowArg(g, a.at)
      return w ? `timeline ${fmtInstant(w.startMs)} → ${fmtInstant(w.endMs)}` : 'timeline ?'
    }
    case 'run_mission':
      return `mission ${s('name') ?? '?'}`
    case 'narrate':
      return 'narrate'
    case 'done':
      return 'done'
    case 'panel_action': {
      const inner = g.args && typeof g.args === 'object' ? (g.args as Record<string, unknown>) : {}
      const target = refArg(inner.ref) ?? refArg(inner.subject)
      const layer = typeof inner.layer === 'string' ? ` ${inner.layer} ${inner.on === false ? 'off' : 'on'}` : ''
      return `${s('action') ?? '?'} ${s('view') ?? '?'}${layer}${target ? ` · ${target.kind} ${shortId(target.id)}` : ''}`
    }
  }
}

function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id
}

function fmtInstant(ms: number): string {
  return new Date(ms).toISOString().slice(0, 16).replace('T', ' ') + 'Z'
}

/** Read a `{kind, id}` argument, or null. */
export function refArg(v: unknown): CaseRef | null {
  if (!v || typeof v !== 'object') return null
  const r = v as Record<string, unknown>
  return typeof r.kind === 'string' && typeof r.id === 'string' && r.id
    ? { kind: r.kind, id: r.id }
    : null
}

const SPAN_RE = /^(\d+)\s*([hd])$/

/**
 * Read a `timeline` action's window to absolute epoch ms.
 *
 * The canonical args are FLAT — `{start, end, span, at}` with `start`/`end`
 * already absolute (`consult_case_schema.CANONICAL_ARGS.timeline`) — so a
 * replay a week later keeps the aperture. The model-side nested `window`
 * shape is read too. A `span` with no absolute ends is resolved against the
 * action's own admission instant — never against "now", which would move the
 * aperture under a replay.
 */
export function windowArg(
  v: unknown,
  at: string | null,
): { startMs: number; endMs: number } | null {
  if (!v || typeof v !== 'object') return null
  const outer = v as Record<string, unknown>
  const w =
    typeof outer.start === 'string' || typeof outer.span === 'string'
      ? outer
      : outer.window && typeof outer.window === 'object'
        ? (outer.window as Record<string, unknown>)
        : outer
  if (typeof w.start === 'string' && typeof w.end === 'string') {
    const startMs = Date.parse(w.start)
    const endMs = Date.parse(w.end)
    if (Number.isFinite(startMs) && Number.isFinite(endMs) && startMs < endMs) {
      return { startMs, endMs }
    }
    return null
  }
  if (typeof w.span === 'string' && at) {
    const m = SPAN_RE.exec(w.span.trim())
    const endMs = Date.parse(at)
    if (!m || !Number.isFinite(endMs)) return null
    const unit = m[2] === 'h' ? 3_600_000 : 86_400_000
    const span = Number(m[1]) * unit
    return span > 0 ? { startMs: endMs - span, endMs } : null
  }
  return null
}

/**
 * The validity Timeline's domain while a case drives the clock: the shared
 * window, re-centred on `atMs` when the action named one (same span).
 */
export function caseTimelineDomain(
  startMs: number,
  endMs: number,
  atMs: number | null,
): [number, number] {
  if (atMs === null || !Number.isFinite(atMs)) return [startMs, endMs]
  const half = (endMs - startMs) / 2
  return [atMs - half, atMs + half]
}

/**
 * The smallest of the Timeline's look-back options (days) whose fetch reaches
 * back to `startMs` — so the window the case set is inside what was fetched.
 * The widest option when none does (the panel cannot fetch further back).
 */
export function daysCovering(
  startMs: number,
  nowMs: number,
  options: readonly number[] = [7, 30, 90],
): number {
  const sorted = [...options].sort((a, b) => a - b)
  for (const d of sorted) if (nowMs - d * 86_400_000 <= startMs) return d
  return sorted[sorted.length - 1]
}

// ---------------------------------------------------------------------------
// The snapshot Restore returns to (D2).
// ---------------------------------------------------------------------------

export interface WorkspaceSnapshot {
  takenAt: number
  scope: Scope | null
  selection: Selection | null
  window: { startMs: number; endMs: number; spanMs: number }
  layers: Record<WorldLayer, boolean>
  /** The stance the wall was on, and its serialized arrangement. */
  stance: string | null
  layout: unknown | null
}

// ---------------------------------------------------------------------------
// The player state machine.
// ---------------------------------------------------------------------------

/** One row of the rail. */
export type StepState =
  | 'pending' // received, not yet run (paused at a gap, or follow-live off)
  | 'executed'
  | 'refused' // the gate (or the client allowlist) refused it
  | 'skipped' // the shell cannot run it (mode-gated, retired, no surface)
  | 'stopped' // arrived after the stop latch — "not executed — stopped"
  | 'unreadable' // a frame whose action could not be read

export interface CaseStep {
  /** Admitted actions carry their seq; a refusal rides between them with none. */
  seq: number | null
  action: CaseAction | null
  state: StepState
  /** Why: the refusal cause, the skip reason. */
  reason: string | null
  label: string
  restsOn: string
  /** The narration text, for a `narrate` step. */
  text: string | null
}

export type PlayerStatus =
  | 'live' // following a running turn
  | 'paused_gap' // a seq was missed — waiting for the terminal record
  | 'replay' // replaying a record step by step
  | 'stopped' // the operator stopped it
  | 'done' // sealed / terminal record landed / replay ran out

export interface PlayerState {
  requestId: string
  mode: 'live' | 'replay'
  status: PlayerStatus
  steps: CaseStep[]
  /** Highest seq EXECUTED (or deliberately passed) — the latch's `n`. */
  executedThrough: number
  /** The seq the live path expects next. */
  expectSeq: number
  gapAtSeq: number | null
  sealed: boolean
  summary: string | null
  /** For a replay: the record, and the seq it may not run past. */
  record: CaseRecord | null
  ceiling: number
  /** Index into `record.actions` of the next replay step. */
  cursor: number
  note: string | null
}

export function stepFromAction(a: CaseAction, state: StepState, reason: string | null = null): CaseStep {
  return {
    seq: a.seq,
    action: a,
    state,
    reason,
    label: actionLabel(a),
    restsOn: restsOnLine(a),
    text: a.action === 'narrate' && typeof a.args.text === 'string' ? a.args.text : null,
  }
}

export function newLivePlayer(requestId: string): PlayerState {
  return {
    requestId,
    mode: 'live',
    status: 'live',
    steps: [],
    executedThrough: 0,
    expectSeq: 1,
    gapAtSeq: null,
    sealed: false,
    summary: null,
    record: null,
    ceiling: Infinity,
    cursor: 0,
    note: null,
  }
}

export function newReplayPlayer(rec: CaseRecord): PlayerState {
  const ceiling = replayCeiling(rec)
  return {
    requestId: rec.request_id ?? 'replay',
    mode: 'replay',
    status: 'replay',
    steps: rec.actions.map((a) =>
      a.seq > ceiling
        ? stepFromAction(a, 'stopped', 'not executed — stopped')
        : stepFromAction(a, 'pending'),
    ),
    executedThrough: 0,
    expectSeq: 1,
    gapAtSeq: rec.complete ? null : rec.gap_at_seq,
    sealed: rec.status === 'sealed',
    summary: rec.summary,
    record: rec,
    ceiling,
    cursor: 0,
    note: null,
  }
}

/** What the live reducer decided to do with an arriving action. */
export type LiveDecision = 'execute' | 'hold' | 'latched' | 'duplicate'

/**
 * Take one live `case_action` in. Pure: returns the next state and whether the
 * caller should execute it now.
 *
 *   * after a STOP every later frame renders "not executed — stopped" (latch);
 *   * a seq ahead of the expected one is a GAP — live execution pauses and the
 *     action is held as `pending` until the terminal record resumes it;
 *   * while paused, everything is held;
 *   * a seq already seen is a duplicate and changes nothing.
 */
export function receiveLiveAction(
  s: PlayerState,
  a: CaseAction,
): { next: PlayerState; decision: LiveDecision } {
  if (s.steps.some((st) => st.seq === a.seq)) return { next: s, decision: 'duplicate' }
  if (s.status === 'stopped') {
    return {
      next: { ...s, steps: [...s.steps, stepFromAction(a, 'stopped', 'not executed — stopped')] },
      decision: 'latched',
    }
  }
  if (s.status === 'paused_gap' || s.status === 'done') {
    return { next: { ...s, steps: [...s.steps, stepFromAction(a, 'pending')] }, decision: 'hold' }
  }
  if (a.seq !== s.expectSeq) {
    return {
      next: {
        ...s,
        status: 'paused_gap',
        gapAtSeq: s.expectSeq,
        steps: [...s.steps, stepFromAction(a, 'pending')],
        note: 'a step was missed; the record completes when the turn lands',
      },
      decision: 'hold',
    }
  }
  return {
    next: { ...s, expectSeq: a.seq + 1, steps: [...s.steps, stepFromAction(a, 'pending')] },
    decision: 'execute',
  }
}

/** Record the outcome of running step `seq`. */
export function markStep(
  s: PlayerState,
  seq: number,
  state: StepState,
  reason: string | null = null,
): PlayerState {
  const steps = s.steps.map((st) => (st.seq === seq ? { ...st, state, reason } : st))
  const ran = state === 'executed' || state === 'skipped' || state === 'refused'
  return {
    ...s,
    steps,
    executedThrough: ran ? Math.max(s.executedThrough, seq) : s.executedThrough,
  }
}

/** A refusal frame: listed in the trace, never executed. */
export function receiveRefusal(
  s: PlayerState,
  f: { tool: string | null; cause: string; detail: string | null },
): PlayerState {
  const step: CaseStep = {
    seq: null,
    action: null,
    state: 'refused',
    reason: f.detail ? `${f.cause}: ${f.detail}` : f.cause,
    label: `refused ${f.tool ?? 'action'}`,
    restsOn: 'no read',
    text: null,
  }
  return { ...s, steps: [...s.steps, step] }
}

/**
 * The STOP latch. Immediate and total: the status flips, every step not yet
 * run becomes "not executed — stopped", and `executedThrough` is the `n` the
 * stop route is told.
 */
export function latchStop(s: PlayerState): PlayerState {
  if (s.status === 'stopped') return s
  return {
    ...s,
    status: 'stopped',
    steps: s.steps.map((st) =>
      st.state === 'pending' ? { ...st, state: 'stopped', reason: 'not executed — stopped' } : st,
    ),
    note: `stopped after step ${s.executedThrough}`,
  }
}

/**
 * The terminal record landed (live). Returns the actions still to RUN — the
 * ones after `executedThrough`, up to the record's own ceiling — and the next
 * state. A stopped player runs nothing more, whatever the record says.
 */
export function reconcileWithRecord(
  s: PlayerState,
  rec: CaseRecord,
): { next: PlayerState; toRun: CaseAction[] } {
  const ceiling = replayCeiling(rec)
  if (s.status === 'stopped') {
    return { next: { ...s, record: rec, ceiling }, toRun: [] }
  }
  const toRun = rec.actions.filter((a) => a.seq > s.executedThrough && a.seq <= ceiling)
  // Rebuild the rail from the record (exact) while keeping the refusals the
  // stream showed and the outcomes already reached.
  const known = new Map(s.steps.filter((st) => st.seq !== null).map((st) => [st.seq, st]))
  const steps: CaseStep[] = []
  for (const a of rec.actions) {
    const prev = known.get(a.seq)
    if (prev && prev.state !== 'pending') steps.push(prev)
    else if (a.seq > ceiling) steps.push(stepFromAction(a, 'stopped', 'not executed — stopped'))
    else steps.push(stepFromAction(a, 'pending'))
  }
  for (const st of s.steps) if (st.seq === null) steps.push(st)
  const gap = rec.complete ? null : rec.gap_at_seq
  return {
    next: {
      ...s,
      steps,
      record: rec,
      ceiling,
      gapAtSeq: gap,
      sealed: rec.status === 'sealed' || s.sealed,
      summary: rec.summary ?? s.summary,
      status: 'done',
      note:
        gap !== null
          ? `the record has a hole at step ${gap}; nothing after it runs`
          : rec.status === 'stopped'
            ? `stopped after step ${rec.stopped_after_seq ?? s.executedThrough}`
            : null,
    },
    toRun,
  }
}

/** The next replay action, or null when the replay is at its end. */
export function nextReplayAction(s: PlayerState): CaseAction | null {
  if (s.mode !== 'replay' || s.status !== 'replay' || !s.record) return null
  const a = s.record.actions[s.cursor]
  if (!a || a.seq > s.ceiling) return null
  return a
}

/** Advance the replay cursor past `seq`, finishing when nothing is left. */
export function advanceReplay(s: PlayerState): PlayerState {
  const cursor = s.cursor + 1
  const more = !!s.record && !!s.record.actions[cursor] && s.record.actions[cursor].seq <= s.ceiling
  return {
    ...s,
    cursor,
    status: more ? s.status : 'done',
    note: more
      ? s.note
      : s.record && !s.record.complete && s.record.gap_at_seq !== null
        ? `the record has a hole at step ${s.record.gap_at_seq}; the replay stops there`
        : s.note,
  }
}

/** The step counter — the mobile/narrow rail's one line. */
export function stepCounter(s: PlayerState): string {
  const total = s.steps.filter((st) => st.seq !== null).length
  return `step ${s.executedThrough} of ${total}`
}

/** The replay caption — what replaying a case does and does not show. */
export function replayCaption(rec: CaseRecord): string {
  return `Replaying a case recorded ${rec.as_of ?? '(as-of not recorded)'}; panels show the record as it stands now. A record no longer on record renders as absent, never skipped silently.`
}

/** True for the scope kinds a case may set. */
export function isCaseScopeKind(v: unknown): v is ScopeKind {
  return (
    v === 'report' ||
    v === 'target' ||
    v === 'entity' ||
    v === 'situation' ||
    v === 'journal_entry' ||
    v === 'none'
  )
}
