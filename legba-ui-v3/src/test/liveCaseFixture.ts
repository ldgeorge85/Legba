/**
 * Two RECORDED cases from real consult sessions (UI-B6), read with GET only
 * from the live registry on 2026-10-04 (`GET /api/v1/consult/sessions/{id}`):
 *
 *  - LIVE_CASE_SESSION — session `afa27c66-4557-4031-9fe6-68432ebae41e`, turn
 *    `9037291e-0bcd-4f86-9ac6-633bb81f785c`: a SEALED five-step case
 *    (arrange a stance → open the subject's overview → scope → open its
 *    situations → focus one situation). The `case` key is VERBATIM.
 *  - LIVE_STOPPED_SESSION — session `54f54de7-e584-4db1-8e67-2803de7348f8`,
 *    turn `a9297e19-231b-4824-82b7-55f9c459aeea`: a case the operator STOPPED
 *    (`status: stopped`, `stopped_after_seq: 3`). The `case` key is VERBATIM —
 *    including its empty action list (see the lane report).
 *
 * Trimmed to what replay reads: the answer bodies, step traces, tool calls and
 * census blobs are NOT carried (a fixture is not a copy of an instance's
 * reporting); every turn id, question and case record is as the server
 * returned it.
 */

export const LIVE_CASE_SESSION_ID = 'afa27c66-4557-4031-9fe6-68432ebae41e'
export const LIVE_CASE_TURN_ID = '9037291e-0bcd-4f86-9ac6-633bb81f785c'
export const LIVE_CASE_SUBJECT = 'country_watch_ir'
export const LIVE_CASE_SITUATION = '328eb202-681a-4a39-a38e-ca387fb55e37'

const TARGET_CITE = [{ id: 'country_watch_ir', kind: 'target' }]
const SITUATION_CITE = [{ id: '328eb202-681a-4a39-a38e-ca387fb55e37', kind: 'situation' }]

export const LIVE_CASE = {
  as_of: '2026-10-04T04:31:12Z',
  plane: 'core',
  status: 'sealed',
  actions: [
    {
      at: '2026-10-04T04:31:00Z',
      seq: 1,
      args: { layout: 'desk', layout_type: 'stance' },
      cites: TARGET_CITE,
      action: 'arrange',
      rests_on: [{ tool: 'list_targets', round: 1 }],
    },
    {
      at: '2026-10-04T04:31:00Z',
      seq: 2,
      args: { kind: 'target.overview', params: { subject: { id: 'country_watch_ir', kind: 'target' } } },
      cites: TARGET_CITE,
      action: 'open_panel',
      rests_on: [{ tool: 'list_targets', round: 1 }],
    },
    {
      at: '2026-10-04T04:31:00Z',
      seq: 3,
      args: { id: 'country_watch_ir', kind: 'target' },
      cites: TARGET_CITE,
      action: 'set_scope',
      rests_on: [{ tool: 'list_targets', round: 1 }],
    },
    {
      at: '2026-10-04T04:31:00Z',
      seq: 4,
      args: { kind: 'target.situations', params: { subject: { id: 'country_watch_ir', kind: 'target' } } },
      cites: SITUATION_CITE,
      action: 'open_panel',
      rests_on: [{ tool: 'list_situations', round: 3 }],
    },
    {
      at: '2026-10-04T04:31:00Z',
      seq: 5,
      args: { ref: { id: '328eb202-681a-4a39-a38e-ca387fb55e37', kind: 'situation' } },
      cites: SITUATION_CITE,
      action: 'focus',
      rests_on: [{ tool: 'list_situations', round: 3 }],
    },
  ],
  refused: 0,
  summary: 'Iran desk case view arranged with overview, situations, and focus on US naval buildup',
  version: 'case/1',
  complete: true,
  gap_at_seq: null,
  request_id: '01b207c7-ab5a-4c60-bbfb-1bd91b579767',
  vocabulary: '0b698ec07b8e6c7a6e814d92f8dad29e6c9b6fc4d812c78687136c80ed179b15',
  stopped_after_seq: null,
}

export const LIVE_CASE_QUESTION =
  'Present the Iran desk as a case: what changed in the last 7 days, where, and what to watch next — arrange the workstation to show it.'

export const LIVE_CASE_SESSION = {
  id: LIVE_CASE_SESSION_ID,
  mode: 'chat' as const,
  title: 'Present the Iran desk as a case: what changed in the last 7 days, where, and what to watch next — arrange the workstati…',
  turns: [
    {
      id: 'd8efee6a-cb94-4244-809e-0e02b4947af8',
      role: 'user' as const,
      content: LIVE_CASE_QUESTION,
      steps: [],
      tool_calls: [],
      cited_refs: [],
    },
    {
      id: LIVE_CASE_TURN_ID,
      role: 'assistant' as const,
      content: '(answer body not carried in the fixture)',
      steps: [],
      tool_calls: [],
      cited_refs: [],
      response_recorded: true,
      request_id: '01b207c7-ab5a-4c60-bbfb-1bd91b579767',
      case: LIVE_CASE,
    },
  ],
}

export const LIVE_STOPPED_SESSION_ID = '54f54de7-e584-4db1-8e67-2803de7348f8'
export const LIVE_STOPPED_TURN_ID = 'a9297e19-231b-4824-82b7-55f9c459aeea'

export const LIVE_STOPPED_CASE = {
  as_of: '2026-10-04T04:35:54Z',
  plane: 'core',
  status: 'stopped',
  actions: [],
  refused: 0,
  summary: null,
  version: 'case/1',
  complete: true,
  gap_at_seq: null,
  request_id: '0a2aa8e4-7925-47f3-a07e-c5655382e26f',
  vocabulary: '0b698ec07b8e6c7a6e814d92f8dad29e6c9b6fc4d812c78687136c80ed179b15',
  stopped_after_seq: 3,
}

export const LIVE_STOPPED_SESSION = {
  id: LIVE_STOPPED_SESSION_ID,
  mode: 'chat' as const,
  title: 'Present the Ukraine desk as a case: the last 7 days, where, what to watch.',
  turns: [
    {
      id: 'a93c36d2-302e-47ba-9bbf-e89330236619',
      role: 'user' as const,
      content: 'Present the Ukraine desk as a case: the last 7 days, where, what to watch.',
      steps: [],
      tool_calls: [],
      cited_refs: [],
    },
    {
      id: LIVE_STOPPED_TURN_ID,
      role: 'assistant' as const,
      content: '(answer body not carried in the fixture)',
      steps: [],
      tool_calls: [],
      cited_refs: [],
      response_recorded: true,
      request_id: '0a2aa8e4-7925-47f3-a07e-c5655382e26f',
      case: LIVE_STOPPED_CASE,
    },
  ],
}
