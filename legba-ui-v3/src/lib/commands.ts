/**
 * commands — THE ONE COMMAND REGISTRY (UI-B1/shell; UI-D1 §3, D5).
 *
 * Three families, one table:
 *
 *   | family  | members                                 | drivers                 | replayable | executes                    |
 *   |---------|-----------------------------------------|-------------------------|------------|-----------------------------|
 *   | `drive` | the nine `case/1` actions               | operator + consult      | yes        | `applyAction` (the ONE)     |
 *   | `shell` | reset view, copy link, print, PNG,      | operator only           | never      | a local handler the shell   |
 *   |         | density, close view, palette search     |                         |            | registers                   |
 *   | `write` | C-2a's consult write tools + the        | consult emits, operator | never      | the REGISTRY executor —     |
 *   |         | operator's confirm / cancel / revert    | confirms                |            | the shell never writes      |
 *
 * And the PRODUCERS: every human way of moving the view compiles here to a
 * list of drive actions (the palette, the object rail, the `＋` catalog, the
 * panels' cross-links that used to be `legba:open-*` DOM events). The URL
 * compiles in `lib/address.ts`. Nothing below touches a store or Dockview — it
 * only builds actions, so "every producer compiles to drive actions" is a pure,
 * testable statement.
 *
 * U-2 SHELL: the layout save / restore and "save as mission" shell commands
 * are gone with the arrangement machinery; a view is kept as a link
 * (`view.link`) and a surface goes back to its default with `view.reset`.
 */
import type { PanelKind } from '@/types'
import { act, type WorkstationAction } from '@/lib/applyAction'
import { CASE_ACTIONS, type CaseActionName } from '@/lib/caseModel'
import { SUBJECT_READ_KIND, SUBJECT_READ_TAB } from '@/lib/subjectNoun'

// ---------------------------------------------------------------------------
// The registry.
// ---------------------------------------------------------------------------

export type CommandFamily = 'drive' | 'shell' | 'write'

export type ShellCommandId =
  | 'view.reset'
  | 'view.link'
  | 'print'
  | 'png'
  | 'density.cycle'
  | 'tile.close'
  | 'palette.search'

export interface CommandDef {
  id: string
  family: CommandFamily
  label: string
  /** Recorded in a case and re-run on Replay. Only `drive` is. */
  replayable: boolean
  /** Who executes it. */
  executor: 'applyAction' | 'shell' | 'registry'
  /** For a command this shell does not carry yet: the surface or lane that does. */
  homeElsewhere?: string
}

const DRIVE: CommandDef[] = CASE_ACTIONS.map((id) => ({
  id,
  family: 'drive' as const,
  label: id.replace('_', ' '),
  replayable: true,
  executor: 'applyAction' as const,
}))

const SHELL: CommandDef[] = [
  { id: 'view.reset', family: 'shell', label: 'Reset this surface to its default', replayable: false, executor: 'shell' },
  { id: 'view.link', family: 'shell', label: 'Copy a link to this view', replayable: false, executor: 'shell' },
  {
    id: 'print',
    family: 'shell',
    label: 'Print',
    replayable: false,
    executor: 'shell',
    homeElsewhere: 'the surface being read (Desk Brief, Report Export)',
  },
  {
    id: 'png',
    family: 'shell',
    label: 'Export PNG',
    replayable: false,
    executor: 'shell',
    homeElsewhere: 'the surface being read (Desk Brief)',
  },
  { id: 'density.cycle', family: 'shell', label: 'Cycle density', replayable: false, executor: 'shell' },
  { id: 'tile.close', family: 'shell', label: 'Close the opened view', replayable: false, executor: 'shell' },
  { id: 'palette.search', family: 'shell', label: 'Search everything', replayable: false, executor: 'shell' },
]

/**
 * C-2a's write tools as the server offers them
 * (`src/legba/data/consult_write_vocabulary.py::WRITE_TOOL_NAMES`), plus the
 * operator's three verbs on a pending or executed write. Listed so the shell
 * can REFUSE them by name — it never executes one.
 */
export const WRITE_TOOL_NAMES = [
  'inspect_object',
  'create_object',
  'update_object',
  'transition_object',
  'revert_write',
  'rerun_object',
] as const

const WRITE: CommandDef[] = [
  ...WRITE_TOOL_NAMES.map((id) => ({
    id,
    family: 'write' as const,
    label: id.replace('_', ' '),
    replayable: false,
    executor: 'registry' as const,
  })),
  ...(['write.confirm', 'write.cancel', 'write.revert'] as const).map((id) => ({
    id,
    family: 'write' as const,
    label: id.slice('write.'.length),
    replayable: false,
    executor: 'registry' as const,
  })),
]

export const COMMANDS: readonly CommandDef[] = [...DRIVE, ...SHELL, ...WRITE]

export function findCommand(id: string): CommandDef | undefined {
  return COMMANDS.find((c) => c.id === id)
}

export function commandsOf(family: CommandFamily): CommandDef[] {
  return COMMANDS.filter((c) => c.family === family)
}

// ---------------------------------------------------------------------------
// Shell commands — local handlers, operator only, never recorded.
// ---------------------------------------------------------------------------

type ShellHandlers = Partial<Record<ShellCommandId, () => void>>
let shellHandlers: ShellHandlers = {}

/** The shell registers the handlers it carries; returns the unregister fn. */
export function registerShellCommands(h: ShellHandlers): () => void {
  shellHandlers = h
  return () => {
    if (shellHandlers === h) shellHandlers = {}
  }
}

/** The shell commands this shell can run right now (the palette lists these). */
export function availableShellCommands(): CommandDef[] {
  return SHELL.filter((c) => typeof shellHandlers[c.id as ShellCommandId] === 'function')
}

export type CommandOutcome = { ok: true } | { ok: false; reason: string }

/**
 * Run a non-drive command. A `write` is refused by name — the shell never
 * writes, which keeps the drive pack read-only by construction (C-1a §5). A
 * `drive` name is refused here too: drive goes through `applyAction`.
 */
export function runCommand(id: string): CommandOutcome {
  const def = findCommand(id)
  if (!def) return { ok: false, reason: `unknown command: ${id}` }
  if (def.family === 'write') return { ok: false, reason: `write_not_executed_by_shell: ${id}` }
  if (def.family === 'drive') return { ok: false, reason: `drive_goes_through_applyAction: ${id}` }
  const fn = shellHandlers[id as ShellCommandId]
  if (!fn) return { ok: false, reason: `not carried by this shell: ${id}${def.homeElsewhere ? ` — ${def.homeElsewhere}` : ''}` }
  fn()
  return { ok: true }
}

// ---------------------------------------------------------------------------
// Producers → drive actions.
// ---------------------------------------------------------------------------

type RefKind = 'target' | 'analyst' | 'source' | 'report' | 'journal_entry' | 'situation' | 'entity' | 'finding'

const ref = (kind: string, id: string) => ({ kind, id })

/** Open a view; `subject` binds a per-subject / per-unit kind; `params` reach a drill. */
export function openPanel(
  kind: PanelKind | string,
  opts: { subject?: { kind: string; id: string }; tab?: string; params?: Record<string, unknown> } = {},
): WorkstationAction {
  const params: Record<string, unknown> = { ...(opts.params ?? {}) }
  if (opts.subject) params.subject = ref(opts.subject.kind, opts.subject.id)
  if (opts.tab) params.tab = opts.tab
  return act('open_panel', { kind, params })
}

export const focus = (kind: RefKind | string, id: string, label?: string): WorkstationAction =>
  act('focus', label ? { ref: ref(kind, id), label } : { ref: ref(kind, id) })

export const setScope = (kind: string, id: string | null, label?: string): WorkstationAction =>
  act('set_scope', kind === 'none' ? { kind: 'none', id: null } : { kind, id, ...(label ? { label } : {}) })

/**
 * Go to a surface. The wire name stays `arrange {layout_type: 'stance'}` — the
 * `case/1` grammar and the server's vocabulary carry it — and the shell
 * resolves the id (a surface, or a retired workspace id) to the surface.
 */
export const goToSurface = (id: string): WorkstationAction => act('arrange', { layout: id, layout_type: 'stance' })
/** @deprecated the retired name of {@link goToSurface}; kept for recorded-case producers. */
export const arrangeStance = goToSurface
export const runMission = (id: string): WorkstationAction => act('run_mission', { name: id })

/**
 * The reads "Investigate · X" opens on one record: they open bound to the
 * record, each in its home region. A subject's reads are ONE kind since UI-B3
 * — `subject.page` — beside the map and the clock still pending their
 * survivors; `unit.page` will do the same for a unit.
 */
export const INVESTIGATE_KINDS: Readonly<Record<'target' | 'analyst', readonly PanelKind[]>> = {
  target: ['subject.page', 'target.map', 'target.timeline'],
  analyst: ['analyst.outputs', 'analyst.runs', 'analyst.critiques', 'analyst.cross_target'],
}

/** ⌘K — what each palette family compiles to. */
export const paletteActions = {
  panel: (kind: PanelKind): WorkstationAction[] => [openPanel(kind)],
  surface: (id: string): WorkstationAction[] => [goToSurface(id)],
  /** Enter on a record: focus it (the Record sheet and every subscriber follow). */
  selectRecord: (kind: string, id: string, label?: string): WorkstationAction[] => [focus(kind, id, label)],
  /** ⌘Enter on a record: its bound primary surface. A source has none, so it is focused. */
  openRecord: (primary: PanelKind, recordKind: 'target' | 'analyst' | 'source', id: string): WorkstationAction[] =>
    recordKind === 'source' ? [focus('source', id)] : [openPanel(primary, { subject: { kind: recordKind, id } })],
  /** "Investigate · X": focus the record, then open its reads. */
  investigate: (recordKind: 'target' | 'analyst', id: string, kinds: readonly PanelKind[]): WorkstationAction[] => [
    focus(recordKind, id),
    ...kinds.map((k) => openPanel(k, { subject: { kind: recordKind, id } })),
  ],
}

/** The object rail — a click is an object action, never a panel row. */
export const railActions = {
  /**
   * A click on a subject = its READ (X4/D4; review D C5): scope the view,
   * focus the subject (the Record sheet and Consult's pin follow), open its
   * page on the brief. The order is the contract — the scope lands first so
   * the page opens already scoped. `lib/subjectRead.ts::subjectReadActions`
   * is this.
   */
  subject: (id: string, label?: string): WorkstationAction[] => [
    setScope('target', id, label),
    focus('target', id, label),
    openPanel(SUBJECT_READ_KIND, { subject: { kind: 'target', id }, tab: SUBJECT_READ_TAB }),
  ],
  unit: (id: string, label?: string): WorkstationAction[] => [focus('analyst', id, label)],
  source: (id: string, label?: string): WorkstationAction[] => [focus('source', id, label)],
  /** A read: what the view is about AND what is being read. */
  read: (id: string, label?: string): WorkstationAction[] => [setScope('report', id, label), focus('report', id, label)],
  /** One of an object's drills (its bound reads). */
  drill: (kind: PanelKind, subject: { kind: 'target' | 'analyst'; id: string }): WorkstationAction[] => [
    openPanel(kind, { subject }),
  ],
  queue: (kind: PanelKind): WorkstationAction[] => [openPanel(kind)],
  catalog: (kind: PanelKind): WorkstationAction[] => [openPanel(kind)],
}

/**
 * The retired DOM bridges, as actions. Each former `legba:open-*` call site
 * calls one of these and `drive`s the result — a cross-link is addressable
 * (and recordable) exactly like any other open.
 */
export const bridgeActions = {
  /** `legba:open-cross-framing` → the claim drill on its desk. */
  crossFraming: (s: {
    targetId: string
    claimId?: string | null
    claimText?: string | null
    origin?: string
  }): WorkstationAction =>
    openPanel('analysis.cross_framing', {
      subject: { kind: 'target', id: s.targetId },
      params: {
        ...(s.claimId ? { claim_id: s.claimId } : {}),
        ...(s.claimText ? { claim_text: s.claimText } : {}),
        ...(s.origin ? { origin: s.origin } : {}),
      },
    }),
  /** `legba:open-optimizer-diff` → the candidate drill. */
  optimizerDiff: (candidateId: string): WorkstationAction =>
    openPanel('system.optimizer.diff', { params: { candidate_id: candidateId } }),
  /** `legba:open-desk-brief` → the subject page's Brief tab, bound (UI-B3). */
  deskBrief: (targetId: string): WorkstationAction =>
    openPanel('subject.page', { subject: { kind: 'target', id: targetId }, tab: 'brief' }),
}

/** Every action name a producer may emit — the closed set. */
export const DRIVE_ACTION_NAMES: readonly CaseActionName[] = CASE_ACTIONS
