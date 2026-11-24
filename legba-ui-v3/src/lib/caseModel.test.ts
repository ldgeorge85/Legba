/**
 * C-1b/ui — the pure half of "consult presents a case": the vocabulary read,
 * the frame → action projection (against the FROZEN contract's frame shapes),
 * the persisted-record parse, the replay/live state machine and the refusal of
 * a non-allowlisted kind.
 */
import { describe, expect, it } from 'vitest'

import vocabularyDoc from '@/lib/workstationVocabulary.json'
import {
  CASE_VOCABULARY,
  advanceReplay,
  caseFrameOf,
  caseTimelineDomain,
  daysCovering,
  firstGap,
  isDrivableKind,
  latchStop,
  markStep,
  newLivePlayer,
  newReplayPlayer,
  nextReplayAction,
  parseCaseAction,
  parseCaseRecord,
  receiveLiveAction,
  receiveRefusal,
  reconcileWithRecord,
  replayCeiling,
  restsOnLine,
  roundsForTurn,
  stepCounter,
  vocabularyFrom,
  windowArg,
  type CaseAction,
} from './caseModel'

/** A frame exactly as `CaseActionFrame.to_step()` emits it, on the stream. */
function actionFrame(seq: number, action: string, args: Record<string, unknown> = {}) {
  return {
    type: 'step',
    phase: 'case',
    kind: 'case_action',
    seq,
    action,
    args,
    cites: [{ kind: 'situation', id: `s-${seq}` }],
    rests_on: [{ round: 2, tool: 'list_situations' }],
    at: '2026-10-03T12:00:00Z',
    round: 3,
  }
}

function act(seq: number, action: CaseAction['action'] = 'narrate', args: Record<string, unknown> = { text: `step ${seq}` }): CaseAction {
  return parseCaseAction(actionFrame(seq, action, args))!
}

function record(n: number, extra: Record<string, unknown> = {}) {
  return {
    version: 'case/1',
    request_id: 'req-1',
    status: 'sealed',
    as_of: '2026-10-03T12:05:00Z',
    plane: 'core',
    vocabulary: 'a'.repeat(64),
    refused: 1,
    stopped_after_seq: null,
    complete: true,
    gap_at_seq: null,
    summary: 'the case in one line',
    actions: Array.from({ length: n }, (_, i) => {
      const f = actionFrame(i + 1, 'narrate', { text: `step ${i + 1}` })
      return { seq: f.seq, action: f.action, args: f.args, at: f.at, cites: f.cites, rests_on: f.rests_on }
    }),
    ...extra,
  }
}

describe('the vocabulary — one file, two readers', () => {
  it('reads the exported document the backend generates', () => {
    expect(CASE_VOCABULARY.hash).toBe((vocabularyDoc as { vocabulary_hash: string }).vocabulary_hash)
    expect(CASE_VOCABULARY.drivableKinds).toContain('system.findings')
    // UI-B3: the per-subject reads are ONE drivable kind, its reads are tabs.
    expect(CASE_VOCABULARY.drivableKinds).toContain('subject.page')
    expect(CASE_VOCABULARY.drivableKinds).not.toContain('target.overview')
    expect(CASE_VOCABULARY.subjectKind['subject.page']).toBe('target')
    expect(CASE_VOCABULARY.tabs['subject.page']).toEqual([
      'brief', 'evidence', 'claims', 'signals', 'situations', 'sources', 'graph', 'runtime',
    ])
    expect(CASE_VOCABULARY.subjectKind['analyst.runs']).toBe('analyst')
    expect(CASE_VOCABULARY.stances).toEqual(expect.arrayContaining(['morning_read', 'desk', 'investigate']))
    expect(CASE_VOCABULARY.presets).toEqual(expect.arrayContaining(['wall', 'workspace', 'focus']))
    expect(CASE_VOCABULARY.missions).not.toContain('release')
    expect(CASE_VOCABULARY.mapSurfaceKind).toBe('v4.map')
    expect(CASE_VOCABULARY.tabs['system.timeline']).toEqual(['events', 'validity'])
  })

  it('refuses a non-allowlisted kind: the Engine Room, authoring, and the presenting tile', () => {
    expect(isDrivableKind('registry.stack')).toBe(false)
    expect(isDrivableKind('source.subscription_builder')).toBe(false)
    expect(isDrivableKind('system.settings')).toBe(false)
    expect(isDrivableKind('system.consult')).toBe(false)
    expect(isDrivableKind('not.a.kind')).toBe(false)
    expect(isDrivableKind('system.wall')).toBe(true)
  })

  it('a case turn defaults to the case round cap — never raising one the operator chose', () => {
    expect(CASE_VOCABULARY.caseRoundsDefault).toBe(20)
    expect(roundsForTurn(10, 10, true)).toBe(20)
    expect(roundsForTurn(10, 10, false)).toBe(10)
    expect(roundsForTurn(6, 10, true)).toBe(6)
    expect(roundsForTurn(25, 10, true)).toBe(25)
  })

  it('an absent axis refuses everything on it', () => {
    const v = vocabularyFrom({})
    expect(v.drivableKinds).toEqual([])
    expect(v.mapSurfaceKind).toBeNull()
    expect(v.hash).toBeNull()
  })
})

describe('frame → action projection (the contract frames)', () => {
  it('reads a flat case_action frame', () => {
    const f = caseFrameOf(actionFrame(4, 'focus', { ref: { kind: 'finding', id: 'f1' } }))
    expect(f?.kind).toBe('case_action')
    if (f?.kind !== 'case_action') throw new Error('unreachable')
    expect(f.action).toMatchObject({
      seq: 4,
      action: 'focus',
      args: { ref: { kind: 'finding', id: 'f1' } },
      at: '2026-10-03T12:00:00Z',
      rests_on: [{ round: 2, tool: 'list_situations' }],
    })
  })

  it('an unreadable case_action is carried as null, never guessed at', () => {
    const f = caseFrameOf({ type: 'step', kind: 'case_action', seq: 'x', action: 'focus' })
    expect(f).toEqual(expect.objectContaining({ kind: 'case_action', action: null }))
    expect(parseCaseAction({ seq: 1, action: 'delete_everything' })).toBeNull()
  })

  it('reads refusals, seals and stops; ignores ordinary step frames', () => {
    expect(
      caseFrameOf({ phase: 'case', kind: 'case_refused', action: 'focus', cause: 'ref_not_read', detail: 'f9', args: {}, round: 2 }),
    ).toEqual({ kind: 'case_refused', tool: 'focus', cause: 'ref_not_read', detail: 'f9' })
    expect(caseFrameOf({ phase: 'case', kind: 'case_sealed', seq_through: 5, summary: 'S', at: 'x', round: 4 })).toEqual({
      kind: 'case_sealed',
      summary: 'S',
    })
    expect(caseFrameOf({ phase: 'case', kind: 'case_stopped', stopped_after_seq: 3, at: 'x' })).toEqual({
      kind: 'case_stopped',
      stoppedAfterSeq: 3,
    })
    expect(caseFrameOf({ type: 'step', kind: 'tool_call', tool: 'list_targets' })).toBeNull()
  })
})

describe('the persisted record', () => {
  it('null means no case', () => {
    expect(parseCaseRecord(null)).toBeNull()
    expect(parseCaseRecord({ status: 'sealed' })).toBeNull()
  })

  it('parses a full record, sorted by seq', () => {
    const raw = record(3)
    raw.actions.reverse()
    const rec = parseCaseRecord(raw)!
    expect(rec.actions.map((a) => a.seq)).toEqual([1, 2, 3])
    expect(rec).toMatchObject({ status: 'sealed', complete: true, gap_at_seq: null, refused: 1, summary: 'the case in one line' })
  })

  it('flags a hole the server did not flag, and replay stops before it', () => {
    const raw = record(4)
    raw.actions.splice(2, 1) // drop seq 3
    const rec = parseCaseRecord(raw)!
    expect(rec.complete).toBe(false)
    expect(rec.gap_at_seq).toBe(3)
    expect(replayCeiling(rec)).toBe(2)
    expect(firstGap([1, 2, 4])).toBe(3)
    expect(firstGap([1, 2, 3])).toBeNull()
  })

  it('a stopped record never replays past stopped_after_seq', () => {
    const rec = parseCaseRecord(record(5, { status: 'stopped', stopped_after_seq: 3 }))!
    expect(replayCeiling(rec)).toBe(3)
    const p = newReplayPlayer(rec)
    expect(p.steps.filter((s) => s.state === 'stopped').map((s) => s.seq)).toEqual([4, 5])
  })
})

describe('the census line — absence is absence', () => {
  it('a step with no read says "no read", never a blank', () => {
    expect(restsOnLine({ action: 'done', rests_on: [] })).toBe('no read')
    expect(restsOnLine({ action: 'focus', rests_on: [{ round: 2, tool: 't' }] })).toBe('rests on 1 read')
    expect(
      restsOnLine({ action: 'focus', rests_on: [{ round: 2, tool: 't' }, { round: 3, tool: 't' }, { round: 0, tool: 'pinned' }] }),
    ).toBe('rests on 2 reads + 1 pin')
  })
})

describe('the live state machine', () => {
  it('executes in seq order', () => {
    const r = receiveLiveAction(newLivePlayer('r'), act(1))
    expect(r.decision).toBe('execute')
    const s = markStep(r.next, 1, 'executed')
    expect(s.executedThrough).toBe(1)
    expect(receiveLiveAction(s, act(2)).decision).toBe('execute')
    expect(receiveLiveAction(s, act(1)).decision).toBe('duplicate')
  })

  it('a missing seq pauses live execution and holds what arrives', () => {
    let s = markStep(receiveLiveAction(newLivePlayer('r'), act(1)).next, 1, 'executed')
    const gap = receiveLiveAction(s, act(3))
    expect(gap.decision).toBe('hold')
    expect(gap.next.status).toBe('paused_gap')
    expect(gap.next.gapAtSeq).toBe(2)
    s = gap.next
    expect(receiveLiveAction(s, act(4)).decision).toBe('hold')
  })

  it('the terminal record resumes from where execution stopped', () => {
    let s = markStep(receiveLiveAction(newLivePlayer('r'), act(1)).next, 1, 'executed')
    s = receiveLiveAction(s, act(3)).next
    const rec = parseCaseRecord(record(4))!
    const { next, toRun } = reconcileWithRecord(s, rec)
    expect(toRun.map((a) => a.seq)).toEqual([2, 3, 4])
    expect(next.steps.map((st) => st.seq)).toEqual([1, 2, 3, 4])
    expect(next.steps[0].state).toBe('executed')
  })

  it('the STOP latch: nothing after k runs, later frames render not executed — stopped', () => {
    let s = markStep(receiveLiveAction(newLivePlayer('r'), act(1)).next, 1, 'executed')
    s = markStep(receiveLiveAction(s, act(2)).next, 2, 'executed')
    s = latchStop(s)
    expect(s.status).toBe('stopped')
    expect(s.executedThrough).toBe(2)
    const late = receiveLiveAction(s, act(3))
    expect(late.decision).toBe('latched')
    expect(late.next.steps.at(-1)).toMatchObject({ seq: 3, state: 'stopped', reason: 'not executed — stopped' })
    // …and the record landing afterwards runs nothing more.
    expect(reconcileWithRecord(late.next, parseCaseRecord(record(5))!).toRun).toEqual([])
    expect(stepCounter(late.next)).toBe('step 2 of 3')
  })

  it('a refusal is listed in the trace and never executed', () => {
    const s = receiveRefusal(newLivePlayer('r'), { tool: 'open_panel', cause: 'kind_not_drivable', detail: 'registry.stack' })
    expect(s.steps[0]).toMatchObject({ seq: null, state: 'refused', reason: 'kind_not_drivable: registry.stack' })
    expect(s.executedThrough).toBe(0)
  })
})

describe('the replay state machine', () => {
  it('walks the record to its end', () => {
    let s = newReplayPlayer(parseCaseRecord(record(2))!)
    expect(nextReplayAction(s)?.seq).toBe(1)
    s = advanceReplay(markStep(s, 1, 'executed'))
    expect(nextReplayAction(s)?.seq).toBe(2)
    s = advanceReplay(markStep(s, 2, 'executed'))
    expect(s.status).toBe('done')
    expect(nextReplayAction(s)).toBeNull()
  })

  it('stops at a hole and says so', () => {
    const raw = record(3)
    raw.actions.splice(1, 1)
    let s = newReplayPlayer(parseCaseRecord(raw)!)
    s = advanceReplay(markStep(s, 1, 'executed'))
    expect(s.status).toBe('done')
    expect(s.note).toMatch(/hole at step 2/)
  })
})

describe('windows and the driven clock', () => {
  it('reads the canonical FLAT window, absolute', () => {
    expect(windowArg({ start: '2026-10-01T00:00:00Z', end: '2026-10-02T00:00:00Z', span: '24h', at: null }, null)).toEqual({
      startMs: Date.parse('2026-10-01T00:00:00Z'),
      endMs: Date.parse('2026-10-02T00:00:00Z'),
    })
  })

  it('resolves a bare span against the ACTION instant, never now', () => {
    const at = '2026-09-01T00:00:00Z'
    expect(windowArg({ span: '7d' }, at)).toEqual({
      startMs: Date.parse(at) - 7 * 86_400_000,
      endMs: Date.parse(at),
    })
    expect(windowArg({ start: 'b', end: 'a' }, at)).toBeNull()
  })

  it('centres the validity Timeline on `at` and fetches far enough back', () => {
    expect(caseTimelineDomain(0, 100, null)).toEqual([0, 100])
    expect(caseTimelineDomain(0, 100, 500)).toEqual([450, 550])
    const now = 100 * 86_400_000
    expect(daysCovering(now - 3 * 86_400_000, now)).toBe(7)
    expect(daysCovering(now - 20 * 86_400_000, now)).toBe(30)
    expect(daysCovering(now - 400 * 86_400_000, now)).toBe(90)
  })
})
