/**
 * mobileConsult (M-3g) — what a phone turn carries beside the shared engine:
 *
 *   1. the on-screen report is the SHELL's (`describeOnScreen()` through the
 *      phone frame port: the screen, or while Consult is the open tab the
 *      screen the person came from, `chat: {placement: 'tab'}`), with a small
 *      truthful fallback when no port is registered;
 *   2. the window is the shell's chip (`useWorldState`), not a fixed week;
 *   5. the pin travels exactly as the desktop's does: the desktop's builders
 *      (`questionWithPins`, `pinnedContext`), never a hand-built copy;
 *   the free route, never `allow_writes`; a case only when drive mode is on.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { buildConsultRequest } from '@/lib/consultCore'
import { MAX_BYTES } from '@/lib/onScreen'
import { pinnedContext, questionWithPins } from '@/lib/consultContext'
import type { Selection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import { registerPhoneFramePort } from '@/mobile/shell/phoneFramePort'
import { usePhoneShell } from '@/mobile/shell/phoneState'
import { phoneScreenOf } from '@/mobile/shell/phoneAddress'
import { phoneOnScreen, phoneTurnRequest, phoneWindowChoice, phoneWindowWords, type PhoneAsking } from './mobileConsult'

const NOW = Date.parse('2026-11-14T08:00:00.000Z')
const DAY = 24 * 3_600_000

const PINS: Selection[] = [
  { kind: 'finding', id: 'ex-read-1', label: 'Example read about a harbour', preview: { title: 'Example read about a harbour', body: 'Country read about Example, written 2026-11-13.' } },
  { kind: 'target', id: 'example_x', label: 'Example' },
]
const ASKING: PhoneAsking = { pins: PINS, label: 'Example › harbour' }

function body(req: ReturnType<typeof phoneTurnRequest>) {
  return buildConsultRequest({ ...req, messages: [], sessionId: 's', requestId: 'r', mode: 'chat', model: 'core' })
}

beforeEach(() => {
  window.history.replaceState(null, '', '/m/')
  usePhoneShell.setState({ watchSegment: 'alerts', lastScreen: null, asking: null })
  useWorldState.getState().setSpan(7 * DAY, NOW)
})

describe('2 · the window is the shell chip', () => {
  it('maps each chip to the matching fixed span, in plain words', () => {
    expect(phoneWindowChoice(DAY)).toBe('24h')
    expect(phoneWindowChoice(7 * DAY)).toBe('168h')
    expect(phoneWindowChoice(30 * DAY)).toBe('720h')
    expect(phoneWindowWords(DAY)).toBe('the last 24 hours')
    expect(phoneWindowWords(30 * DAY)).toBe('the last 30 days')
  })

  it('the request window follows the chip, not a fixed week', () => {
    for (const [span, hours] of [
      [DAY, 24],
      [7 * DAY, 168],
      [30 * DAY, 720],
    ] as const) {
      const b = body(phoneTurnRequest({ question: 'q', asking: null, maxRounds: 10, spanMs: span, groundingMode: 'default', presentCase: false, nowMs: NOW }))
      expect(b.window).toEqual({ hours, end: '2026-11-14T08:00:00.000Z', label: `console ${hours}h` })
    }
  })
})

describe('5 · the pin travels as the desktop sends it', () => {
  const b = body(phoneTurnRequest({ question: 'What is driving this?', asking: ASKING, maxRounds: 10, spanMs: 7 * DAY, groundingMode: 'record_only', presentCase: false, nowMs: NOW }))

  it('the question carries the [Pinned …] prefix from the desktop builder', () => {
    expect(b.question).toBe(questionWithPins('What is driving this?', PINS))
    expect(b.question).toContain('[Pinned finding: "Example read about a harbour" (id=ex-read-1)]')
    expect(b.question.endsWith('What is driving this?')).toBe(true)
  })

  it('pinned_context is the desktop builder output', () => {
    expect(b.pinned_context).toEqual(pinnedContext(PINS))
    expect(b.pinned_context.map((p) => p.kind)).toEqual(['finding', 'target'])
  })

  it('no pin: the question is the question', () => {
    const plain = body(phoneTurnRequest({ question: 'q', asking: null, maxRounds: 10, spanMs: DAY, groundingMode: 'default', presentCase: false, nowMs: NOW }))
    expect(plain.question).toBe('q')
    expect(plain.pinned_context).toEqual([])
  })

  it('record-only is sent as the grounding mode; never writes; scope left null', () => {
    expect(b.grounding_mode).toBe('record_only')
    expect(b).not.toHaveProperty('allow_writes')
    expect(b).not.toHaveProperty('present_case')
    expect(b.scope_predicate).toBeNull()
  })

  it('drive mode on sends the turn as a case (and only then)', () => {
    const c = body(phoneTurnRequest({ question: 'q', asking: null, maxRounds: 10, spanMs: DAY, groundingMode: 'default', presentCase: true, nowMs: NOW }))
    expect(c.present_case).toBe(true)
    expect(c).not.toHaveProperty('allow_writes')
  })
})

describe("1 · the on-screen report is the shell's screen", () => {
  let off: (() => void) | null = null
  afterEach(() => {
    off?.()
    off = null
  })

  it('without the frame port: the small fallback — the pin as the point, the chip, a tab', () => {
    const os = phoneTurnRequest({ question: 'q', asking: ASKING, maxRounds: 10, spanMs: DAY, groundingMode: 'default', presentCase: false, nowMs: NOW }).onScreen
    expect(os).toEqual({
      v: 1,
      page: 'phone',
      side: 'reader',
      point: { kind: 'finding', id: 'ex-read-1', label: 'Example read about a harbour' },
      window: { span: '24h', from: '2026-11-13T08:00Z', to: '2026-11-14T08:00Z' },
      views: [],
      chat: { placement: 'tab', collapsed: false },
    })
    expect(JSON.stringify(os).length).toBeLessThanOrEqual(MAX_BYTES)
    expect(phoneOnScreen(null, 7 * DAY, NOW).point).toBeNull()
  })

  it('with the port: the screen itself (a subject page open)', () => {
    off = registerPhoneFramePort()
    window.history.replaceState(null, '', '/m/#ws=subject&screen=subject%3Aexample_x')
    const os = phoneTurnRequest({ question: 'q', asking: null, maxRounds: 10, spanMs: 7 * DAY, groundingMode: 'default', presentCase: false, nowMs: NOW }).onScreen!
    expect(os.page).toBe('subject')
    expect(os.views.map((v) => v.id)).toEqual(['subject.page:example_x'])
    expect(os.chat).toEqual({ placement: 'tab', collapsed: true })
  })

  it('with the port, Consult open: the screen the person came from, and Consult as the open tab', () => {
    off = registerPhoneFramePort()
    window.history.replaceState(null, '', '/m/#ws=watch')
    usePhoneShell.setState({ lastScreen: phoneScreenOf(window.location.hash) })
    window.history.replaceState(null, '', '/m/#screen=consult')
    const os = phoneTurnRequest({ question: 'q', asking: null, maxRounds: 10, spanMs: 7 * DAY, groundingMode: 'default', presentCase: false, nowMs: NOW }).onScreen!
    expect(os.views.map((v) => v.id)).toEqual(['view:watch_alerts'])
    expect(os.chat).toEqual({ placement: 'tab', collapsed: false })
    expect(os.window.span).toBe('7d')
    expect(JSON.stringify(os).length).toBeLessThanOrEqual(MAX_BYTES)
  })
})
