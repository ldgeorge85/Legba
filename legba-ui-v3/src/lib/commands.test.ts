/**
 * THE COMMAND REGISTRY + EVERY PRODUCER COMPILES TO DRIVE ACTIONS (UI-B1/shell;
 * UI-D1 §3).
 *
 *  - one registry, three families; only `drive` is replayable; the shell
 *    refuses every `write` by name (the drive pack stays read-only by
 *    construction);
 *  - every producer — the palette's families, the rail, the retired DOM
 *    bridges — emits ONLY the nine `case/1` actions (U-2: the missions'
 *    expansion is gone with the missions; a retired `run_mission` lands on a
 *    surface, `App.shell.test.tsx`);
 *  - `#case=` opens a replay of the addressed turn (and refuses honestly).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  COMMANDS,
  INVESTIGATE_KINDS,
  WRITE_TOOL_NAMES,
  availableShellCommands,
  bridgeActions,
  commandsOf,
  paletteActions,
  railActions,
  registerShellCommands,
  runCommand,
} from './commands'
import { isWorkstationAction, type WorkstationAction } from './applyAction'
import { CASE_ACTIONS } from './caseModel'
import { SURFACES } from './surfaces'
import { CONSULT_PANEL_ID, findCaseTurn, openCaseAddress, recordedCases } from './caseReplay'
import { casePlayer, resetCasePlayer } from '@/state/casePlayer'
import { useConsultSessions } from '@/state/consultSession'
import { catalogKinds } from '@/panel-registry/kindPlan'
import type { ConsultSessionDetail } from './api'

function allDrive(list: WorkstationAction[]) {
  for (const a of list) {
    expect(isWorkstationAction(a), JSON.stringify(a)).toBe(true)
    expect(CASE_ACTIONS).toContain(a.action)
  }
}

describe('the registry — three families, one table', () => {
  it('drive is exactly the nine case/1 actions, replayable, executed by applyAction', () => {
    const drive = commandsOf('drive')
    expect(drive.map((c) => c.id)).toEqual([...CASE_ACTIONS])
    for (const c of drive) {
      expect(c.replayable).toBe(true)
      expect(c.executor).toBe('applyAction')
    }
  })

  it('shell commands are operator-only, never replayable, run by a local handler', () => {
    const shell = commandsOf('shell').map((c) => c.id)
    expect(shell).toEqual(
      expect.arrayContaining(['view.reset', 'view.link', 'print', 'png', 'tile.close', 'palette.search']),
    )
    // U-2: the arrangement machinery's commands are gone.
    for (const gone of ['layout.save', 'layout.restore', 'mission.save', 'theme.toggle']) expect(shell).not.toContain(gone)
    for (const c of commandsOf('shell')) expect(c.replayable).toBe(false)
  })

  it('write commands are the registry\'s — the shell refuses every one by name', () => {
    expect(commandsOf('write').map((c) => c.id)).toEqual(
      expect.arrayContaining([...WRITE_TOOL_NAMES, 'write.confirm', 'write.cancel', 'write.revert']),
    )
    for (const c of commandsOf('write')) {
      expect(c.replayable).toBe(false)
      expect(c.executor).toBe('registry')
      expect(runCommand(c.id)).toEqual({ ok: false, reason: `write_not_executed_by_shell: ${c.id}` })
    }
  })

  it('ids are unique across the families', () => {
    const ids = COMMANDS.map((c) => c.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('a shell command runs its registered handler; one this shell does not carry says where it lives', () => {
    const reset = vi.fn()
    const off = registerShellCommands({ 'view.reset': reset })
    try {
      expect(runCommand('view.reset')).toEqual({ ok: true })
      expect(reset).toHaveBeenCalledTimes(1)
      expect(availableShellCommands().map((c) => c.id)).toEqual(['view.reset'])
      const png = runCommand('png')
      expect(png.ok).toBe(false)
      expect(!png.ok && png.reason).toMatch(/not carried by this shell: png — the surface being read/)
      expect(runCommand('focus')).toEqual({ ok: false, reason: 'drive_goes_through_applyAction: focus' })
    } finally {
      off()
    }
  })
})

describe('every producer compiles to drive actions', () => {
  it('⌘K — panels, surfaces, records, investigate', () => {
    for (const k of catalogKinds()) allDrive(paletteActions.panel(k))
    for (const sf of SURFACES) allDrive(paletteActions.surface(sf.id))
    expect(paletteActions.surface('ledger')).toEqual([{ action: 'arrange', args: { layout: 'ledger', layout_type: 'stance' } }])
    allDrive(paletteActions.selectRecord('target', 't1', 'T'))
    allDrive(paletteActions.openRecord('subject.page', 'target', 't1'))
    allDrive(paletteActions.openRecord('system.registry', 'source', 's1'))
    allDrive(paletteActions.investigate('target', 't1', INVESTIGATE_KINDS.target))
    allDrive(paletteActions.investigate('analyst', 'a1', INVESTIGATE_KINDS.analyst))
  })

  it('⌘K shapes: Enter focuses, ⌘Enter opens bound, a source has no bound primary', () => {
    expect(paletteActions.selectRecord('target', 't1', 'T')).toEqual([
      { action: 'focus', args: { ref: { kind: 'target', id: 't1' }, label: 'T' } },
    ])
    expect(paletteActions.openRecord('subject.page', 'target', 't1')).toEqual([
      { action: 'open_panel', args: { kind: 'subject.page', params: { subject: { kind: 'target', id: 't1' } } } },
    ])
    expect(paletteActions.openRecord('system.registry', 'source', 's1')[0].action).toBe('focus')
    const inv = paletteActions.investigate('target', 't1', INVESTIGATE_KINDS.target)
    expect(inv[0].action).toBe('focus')
    expect(inv.slice(1).map((a) => a.args.kind)).toEqual([...INVESTIGATE_KINDS.target])
  })

  it('the rail — objects only: focus, set_scope, open_panel', () => {
    allDrive(railActions.subject('t1', 'T'))
    allDrive(railActions.unit('a1'))
    allDrive(railActions.source('s1'))
    allDrive(railActions.read('r1', 'R'))
    allDrive(railActions.drill('subject.page', { kind: 'target', id: 't1' }))
    allDrive(railActions.queue('system.journal_gate'))
    expect(railActions.read('r1', 'R').map((a) => a.action)).toEqual(['set_scope', 'focus'])
  })

  it('the retired legba:open-* bridges — each is one open_panel with its params', () => {
    const cf = bridgeActions.crossFraming({ targetId: 't1', claimId: 'c1', claimText: 'x', origin: 'claims' })
    expect(cf).toEqual({
      action: 'open_panel',
      args: {
        kind: 'analysis.cross_framing',
        params: { claim_id: 'c1', claim_text: 'x', origin: 'claims', subject: { kind: 'target', id: 't1' } },
      },
    })
    expect(bridgeActions.optimizerDiff('c9')).toEqual({
      action: 'open_panel',
      args: { kind: 'system.optimizer.diff', params: { candidate_id: 'c9' } },
    })
    expect(bridgeActions.deskBrief('t1')).toEqual({
      action: 'open_panel',
      args: { kind: 'subject.page', params: { subject: { kind: 'target', id: 't1' }, tab: 'brief' } },
    })
  })

  it('no source file dispatches a legba:open-* event or parks on window any more', () => {
    const files = import.meta.glob('/src/**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }) as Record<
      string,
      string
    >
    const offenders = Object.entries(files)
      .filter(([path]) => !/\.test\.tsx?$/.test(path))
      // Code only — the retirement is still NAMED in comments, and should be.
      .map(([path, text]) => [path, text.split('\n').filter((l) => !/^\s*(\*|\/\/|\/\*)/.test(l)).join('\n')])
      .filter(([, text]) => /new CustomEvent\(\s*['"`]legba:open-|__legbaPending|addEventListener\(\s*['"`]legba:open-/.test(text))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })
})

describe('#case= opens a replay', () => {
  const RECORD = {
    version: 'case/1',
    request_id: 'req-1',
    status: 'sealed',
    as_of: '2026-10-03T12:00:00Z',
    plane: 'core',
    vocabulary: null,
    refused: 0,
    stopped_after_seq: null,
    complete: true,
    gap_at_seq: null,
    summary: 'look here',
    actions: [
      {
        seq: 1,
        action: 'focus',
        args: { ref: { kind: 'target', id: 'country_g20_br' } },
        at: '2026-10-03T12:00:00Z',
        cites: [{ kind: 'target', id: 'country_g20_br' }],
        rests_on: [{ round: 1, tool: 'list_targets' }],
      },
    ],
  }
  const SESSION: ConsultSessionDetail = {
    id: 'sess-1',
    mode: 'chat',
    title: 't',
    turns: [
      { id: 'u1', role: 'user', content: 'what moved in Brazil?', steps: [], tool_calls: [], cited_refs: [] },
      {
        id: 'a1',
        role: 'assistant',
        content: 'this',
        steps: [],
        tool_calls: [],
        cited_refs: [],
        response_recorded: true,
        case: RECORD,
      },
    ],
  }

  beforeEach(() => resetCasePlayer())
  afterEach(() => {
    resetCasePlayer()
    useConsultSessions.getState().reset(CONSULT_PANEL_ID)
  })

  it('finds the turn by id, else by 0-based position', () => {
    expect(findCaseTurn(SESSION, 'a1')).toBe(1)
    expect(findCaseTurn(SESSION, '1')).toBe(1)
    expect(findCaseTurn(SESSION, '9')).toBe(-1)
    expect(findCaseTurn(SESSION, 'zz')).toBe(-1)
  })

  it('adopts the session into the Consult tile and hands the record to the player as a REPLAY', async () => {
    const load = vi.fn(async () => SESSION)
    const r = await openCaseAddress({ session: 'sess-1', turn: 'a1' }, load)
    expect(r).toEqual({ ok: true, turnIndex: 1 })
    expect(load).toHaveBeenCalledWith('sess-1')
    const p = casePlayer().player!
    expect(p.mode).toBe('replay')
    expect(casePlayer().panelId).toBe(CONSULT_PANEL_ID)
    // The adopted session is now a recorded case the rail and ⌘K list.
    const listed = recordedCases(useConsultSessions.getState().panels)
    expect(listed.map((c) => c.label)).toContain('what moved in Brazil?')
  })

  it('a turn that presented no case, or a session that cannot be read, replays nothing and says why', async () => {
    const noCase = await openCaseAddress({ session: 'sess-1', turn: 'u1' }, async () => SESSION)
    expect(noCase).toEqual({ ok: false, reason: 'turn u1 presented no case' })
    const broken = await openCaseAddress({ session: 'gone', turn: '0' }, async () => {
      throw new Error('404')
    })
    expect(broken).toEqual({ ok: false, reason: 'session gone could not be read: 404' })
    expect(casePlayer().player).toBeNull()
  })
})

describe('X4/D4 — the shell command strings and the rail\'s subject click', () => {
  const label = (id: string) => COMMANDS.find((c) => c.id === id)?.label

  it('the shell commands name the view and the surface — never layout, mission or workspace (U-2)', () => {
    expect(label('view.reset')).toBe('Reset this surface to its default')
    expect(label('view.link')).toBe('Copy a link to this view')
    for (const c of commandsOf('shell')) {
      expect(c.label, c.id).not.toMatch(/\bworkspace\b/i)
      expect(c.label, c.id).not.toMatch(/\b(layout|mission)\b/i)
    }
  })

  it('railActions.subject is the subject READ: set_scope, focus, open_panel subject.page on the brief', () => {
    expect(railActions.subject('country_g20_br', 'Brazil')).toEqual([
      { action: 'set_scope', args: { kind: 'target', id: 'country_g20_br', label: 'Brazil' } },
      { action: 'focus', args: { ref: { kind: 'target', id: 'country_g20_br' }, label: 'Brazil' } },
      {
        action: 'open_panel',
        args: { kind: 'subject.page', params: { subject: { kind: 'target', id: 'country_g20_br' }, tab: 'brief' } },
      },
    ])
  })

  it('a unit click is still a bare focus (only a subject has a page)', () => {
    expect(railActions.unit('a1').map((a) => a.action)).toEqual(['focus'])
  })
})
