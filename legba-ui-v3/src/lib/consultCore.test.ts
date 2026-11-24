/**
 * M-1 — `buildConsultRequest` against the code it replaced.
 *
 * `legacyInlineBody` below is a VERBATIM transcription of the body the desktop
 * built inline in `Consult.tsx` `send()` before 2026-11-14 (the object literal
 * passed to `apiPostWithStatus`, with the `asCase` / `armWrites` gating above
 * it). The builder must serialise to the same bytes over the full cartesian
 * matrix — including `mode: 'deep'`, which the desktop UI no longer sends but
 * the code path still carries. The UI-reachable states are held separately
 * against bodies recorded from the panel itself (`Consult.requestBody.test.tsx`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { buildConsultRequest, turnArmsWrites, turnPresentsCase } from './consultCore'
import { pinnedContext, questionWithPins } from './consultContext'
import { roundsForTurn } from './caseModel'
import { windowForRequest, type GroundingMode, type WindowChoiceId } from './consultContract'
import { DEFAULT_MAX_ROUNDS } from '@/state/consultSession'
import type { ConsultModel } from './api'
import type { OnScreen } from './onScreen'
import type { Selection } from '@/state/selection'

const NOW = Date.parse('2026-11-14T08:00:00.000Z')
const BAND = { startMs: NOW - 72 * 3_600_000, endMs: NOW - 2 * 3_600_000 }

interface State {
  draft: string
  pins: Selection[]
  scope: string
  maxRounds: number
  mode: 'chat' | 'deep'
  model: ConsultModel
  requestId: string
  transcript: { role: 'user' | 'assistant'; content: string; extra?: unknown }[]
  sessionId: string | null
  onScreen: OnScreen | null
  presentCase: boolean
  allowWrites: boolean
  windowChoice: WindowChoiceId
  grounding: GroundingMode
}

// ---- the inline code, as it stood (Consult.tsx, 8ef878b0, send()) ----------
function legacyInlineBody(st: State) {
  const trimmed = st.draft.trim()
  const priorMessages = st.transcript.map((t) => ({ role: t.role, content: t.content }))
  const asCase = st.presentCase && st.mode === 'chat'
  const armWrites = st.allowWrites && st.mode === 'chat'
  const question = questionWithPins(trimmed, st.pins)
  const onScreen = st.onScreen
  const { pins, scope, maxRounds, mode, model, requestId, sessionId, windowChoice, grounding } = st
  return {
    question,
    scope_predicate: scope.trim() || null,
    max_tool_rounds: roundsForTurn(maxRounds, DEFAULT_MAX_ROUNDS, asCase),
    mode,
    model,
    request_id: requestId,
    messages: priorMessages,
    session_id: sessionId,
    pinned_context: pinnedContext(pins),
    ...(onScreen ? { on_screen: onScreen } : {}),
    ...(asCase ? { present_case: true } : {}),
    ...(armWrites ? { allow_writes: true } : {}),
    window: windowForRequest(windowChoice, {
      startMs: BAND.startMs,
      endMs: BAND.endMs,
    }),
    grounding_mode: grounding,
  }
}
// ---------------------------------------------------------------------------

const PIN_SETS: Selection[][] = [
  [],
  [{ kind: 'finding', id: 'f-1', label: 'A finding', preview: { title: 'A finding', body: 'Body text.' } } as Selection],
  [
    { kind: 'target', id: 'country_g20_br', label: 'Brazil', origin: 'scope' } as Selection,
    { kind: 'signal', id: 's-9' } as Selection,
  ],
]
const ON_SCREEN: OnScreen = {
  v: 1,
  page: 'board',
  side: 'reader',
  point: { kind: 'target', id: 'country_g20_br', label: 'Brazil' },
  window: { span: '24h', from: '2026-11-13T08:00Z', to: '2026-11-14T08:00Z' },
  views: [{ id: 'v4.morning_read', at: 'stage', front: true }],
  chat: { placement: 'bottom', collapsed: false },
}

function* matrix(): Generator<State> {
  for (const model of ['core', 'opus', 'fable'] as ConsultModel[])
    for (const windowChoice of ['24h', '168h', '720h', 'band'] as WindowChoiceId[])
      for (const grounding of ['default', 'record_only'] as GroundingMode[])
        for (const pins of PIN_SETS)
          for (const mode of ['chat', 'deep'] as const)
            for (const presentCase of [false, true])
              for (const allowWrites of [false, true])
                for (const onScreen of [null, ON_SCREEN])
                  for (const history of [false, true])
                    for (const [scope, maxRounds] of [['', 10], ['  x == 1 ', 7]] as const)
                      yield {
                        draft: '  what moved?\n',
                        pins,
                        scope,
                        maxRounds,
                        mode,
                        model,
                        requestId: 'req-1',
                        transcript: history
                          ? [
                              { role: 'user', content: 'q1' },
                              { role: 'assistant', content: 'a1', extra: { steps: [1] } },
                            ]
                          : [],
                        sessionId: history ? 'sess-1' : null,
                        onScreen,
                        presentCase,
                        allowWrites,
                        windowChoice,
                        grounding,
                      }
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
})
afterEach(() => vi.useRealTimers())

describe('buildConsultRequest — byte-identical to the inline body it replaced', () => {
  it('matches over the full matrix (3 models × 4 windows × 2 groundings × 3 pin sets × chat/deep × case × writes × on-screen × history × scope/rounds)', () => {
    let n = 0
    const mismatches: string[] = []
    for (const st of matrix()) {
      n += 1
      const legacy = JSON.stringify(legacyInlineBody(st))
      const built = JSON.stringify(
        buildConsultRequest({
          question: questionWithPins(st.draft.trim(), st.pins),
          messages: st.transcript,
          sessionId: st.sessionId,
          requestId: st.requestId,
          mode: st.mode,
          model: st.model,
          window: st.windowChoice,
          band: BAND,
          groundingMode: st.grounding,
          pinnedContext: pinnedContext(st.pins),
          onScreen: st.onScreen,
          presentCase: st.presentCase,
          allowWrites: st.allowWrites,
          scopePredicate: st.scope,
          maxToolRounds: st.maxRounds,
        }),
      )
      if (built !== legacy && mismatches.length < 3) mismatches.push(`${legacy}\n!=\n${built}`)
    }
    expect(n).toBe(3 * 4 * 2 * 3 * 2 * 2 * 2 * 2 * 2 * 2)
    expect(mismatches).toEqual([])
  })

  it('a deep turn never carries present_case or allow_writes', () => {
    expect(turnPresentsCase({ presentCase: true, mode: 'deep' })).toBe(false)
    expect(turnArmsWrites({ allowWrites: true, mode: 'deep' })).toBe(false)
    const body = buildConsultRequest({
      question: 'q',
      messages: [],
      sessionId: null,
      requestId: 'r',
      mode: 'deep',
      model: 'core',
      window: '168h',
      groundingMode: 'default',
      pinnedContext: [],
      onScreen: null,
      presentCase: true,
      allowWrites: true,
    })
    expect('present_case' in body).toBe(false)
    expect('allow_writes' in body).toBe(false)
  })

  it('defaults: no scope predicate, the default round cap, the default window ending now', () => {
    const body = buildConsultRequest({
      question: 'q',
      messages: [],
      sessionId: null,
      requestId: 'r',
      mode: 'chat',
      model: 'core',
      window: '168h',
      groundingMode: 'default',
      pinnedContext: [],
      onScreen: null,
      presentCase: false,
      allowWrites: false,
    })
    expect(body.scope_predicate).toBeNull()
    expect(body.max_tool_rounds).toBe(DEFAULT_MAX_ROUNDS)
    expect(body.window).toEqual({ hours: 168, end: '2026-11-14T08:00:00.000Z', label: 'console 168h' })
  })

  it('a band choice with no band falls back to the default span rather than an invalid window', () => {
    const body = buildConsultRequest({
      question: 'q',
      messages: [],
      sessionId: null,
      requestId: 'r',
      mode: 'chat',
      model: 'core',
      window: 'band',
      groundingMode: 'default',
      pinnedContext: [],
      onScreen: null,
      presentCase: false,
      allowWrites: false,
    })
    expect(body.window).toMatchObject({ hours: 168 })
  })
})
