/**
 * A C-1b-shaped fixture case (`case/1`, the shape `consult_case_record.py`
 * persists on a turn's response bag) that exercises the nine actions on the
 * shell: a preset arrangement, a bound open, a drill open with a claim, the
 * scope, the focus, the clock, the map layers, a narration and the seal. Used
 * by `App.shell.test.tsx` to prove a recorded case replays on the UI-B1 shell.
 */
const AT = '2026-10-03T12:00:00Z'
const CITE = [{ kind: 'target', id: 'country_g20_br' }]
const RESTS = [{ round: 1, tool: 'list_targets' }]

function step(seq: number, action: string, args: Record<string, unknown>) {
  return { seq, action, args, at: AT, cites: action === 'done' ? [] : CITE, rests_on: action === 'done' ? [] : RESTS }
}

export const C1B_FIXTURE_CASE = {
  version: 'case/1',
  request_id: 'req-fixture',
  status: 'sealed',
  as_of: '2026-10-03T12:05:00Z',
  plane: 'core',
  vocabulary: 'a'.repeat(64),
  refused: 0,
  stopped_after_seq: null,
  complete: true,
  gap_at_seq: null,
  summary: 'Brazil: what moved, and the claim it rests on',
  actions: [
    step(1, 'arrange', { layout: 'workspace', layout_type: 'preset' }),
    step(2, 'set_scope', { kind: 'target', id: 'country_g20_br' }),
    step(3, 'open_panel', { kind: 'target.claims', params: { subject: { kind: 'target', id: 'country_g20_br' } } }),
    step(4, 'focus', { ref: { kind: 'finding', id: 'f-77' } }),
    step(5, 'timeline', { start: '2026-09-26T12:00:00Z', end: '2026-10-03T12:00:00Z', span: '7d', at: null }),
    step(6, 'map', { view: 'reset', ref: null, layers: ['events', 'findings'] }),
    step(7, 'open_panel', {
      kind: 'analysis.cross_framing',
      params: { subject: { kind: 'target', id: 'country_g20_br' } },
    }),
    step(8, 'narrate', { text: 'The energy read moved on one port report.' }),
    step(9, 'done', { summary: 'Brazil: what moved, and the claim it rests on' }),
  ],
}

/** A persisted session holding the case on its assistant turn (`a1`). */
export const C1B_FIXTURE_SESSION = {
  id: 'sess-fixture',
  mode: 'chat' as const,
  title: 'Brazil',
  turns: [
    { id: 'u1', role: 'user' as const, content: 'What moved in Brazil?', steps: [], tool_calls: [], cited_refs: [] },
    {
      id: 'a1',
      role: 'assistant' as const,
      content: 'Here is the case.',
      steps: [],
      tool_calls: [],
      cited_refs: [],
      response_recorded: true,
      case: C1B_FIXTURE_CASE,
    },
  ],
}
