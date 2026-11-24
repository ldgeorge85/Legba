/**
 * X1/D1 — the Consult composer gives the question box its own row.
 *
 * Review D (2026-10-08, C1): in every stance that seeds Consult the tile gets
 * about a quarter of the dock, and the composer put the textarea (`flex-1`) on
 * ONE row with three buttons that never shrink — the question box came out
 * 34 px wide and wrapped its placeholder as "As / k / th".
 *
 * jsdom has no layout engine, so a pixel width cannot be asserted here; the
 * live widths are in the lane report's screenshots. What CAN be asserted, and
 * is what makes the bug impossible, is the structure at every dock width the
 * tile is measured at (`useElementWidth` drives the tile's own compact rules,
 * so the three widths below are delivered through the same ResizeObserver
 * path the shell uses):
 *
 *   * the textarea is the ONLY child of its row — nothing in that row can take
 *     width from it;
 *   * it is a full-width block (`w-full`), not a flex item that shrinks;
 *   * Present-as-a-case, Allow-changes and Send live in the row UNDER it, and
 *     that row wraps rather than squeezing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'

import ConsultPanel from './Consult'
import { useConsultSessions } from '@/state/consultSession'
import { resetScope } from '@/state/scope'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'singleton:system.consult',
    panel_id: 'system_consult',
    descriptor_id: '(singleton)',
    descriptor_version: '00000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Consult',
    mode: 'personal',
    layout_slot: 'system.consult',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-10-08T00:00:00Z',
    retired_at: null,
  }
}

/** A ResizeObserver that reports one fixed width — the dock width under test. */
let DOCK_WIDTH = 0
class FixedWidthResizeObserver {
  constructor(private cb: ResizeObserverCallback) {}
  observe() {
    this.cb(
      [{ contentRect: { width: DOCK_WIDTH } } as unknown as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    )
  }
  unobserve() {}
  disconnect() {}
}

const realRO = globalThis.ResizeObserver

beforeEach(() => {
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  resetScope()
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(globalThis as any).ResizeObserver = FixedWidthResizeObserver
})

afterEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(globalThis as any).ResizeObserver = realRO
  vi.restoreAllMocks()
})

/** 26 % of a 1366 px dock (the seeded stances), a half dock, a full dock. */
const DOCK_WIDTHS = [300, 640, 1280]

describe('the Consult composer (X1/D1)', () => {
  for (const width of DOCK_WIDTHS) {
    it(`gives the question box its own full-width row at a ${width} px dock`, () => {
      DOCK_WIDTH = width
      act(() => {
        render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
      })
      const textarea = screen.getByTestId('consult-question')
      const row = screen.getByTestId('consult-question-row')

      // The textarea is the row's ONLY child: nothing can take width from it.
      expect(textarea.parentElement).toBe(row)
      expect(row.children).toHaveLength(1)
      expect(textarea.className).toMatch(/\bw-full\b/)
      expect(textarea.className).toMatch(/\bblock\b/)
      expect(textarea.className).not.toMatch(/\bflex-1\b/)
      expect(row.className).not.toMatch(/\bflex\b/)

      // The toggles and Send sit in the row UNDER it, and that row wraps.
      const controls = screen.getByTestId('consult-composer-controls')
      expect(controls.className).toMatch(/\bflex-wrap\b/)
      for (const id of ['consult-drive-toggle', 'consult-allow-writes', 'consult-submit']) {
        const btn = screen.getByTestId(id)
        expect(controls.contains(btn)).toBe(true)
        expect(row.contains(btn)).toBe(false)
      }
      // Reading order: the question first, the controls after it.
      expect(
        row.compareDocumentPosition(controls) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy()
    })
  }

  it('asks in plain words — no engine vocabulary in the placeholder or empty state (U-1)', () => {
    DOCK_WIDTH = 640
    act(() => {
      render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    })
    const placeholder = screen.getByTestId('consult-question').getAttribute('placeholder') ?? ''
    expect(placeholder).toMatch(/^Ask a question…/)
    const empty = screen.getByTestId('consult-scroll').textContent ?? ''
    expect(empty).toMatch(/Ask anything — answers cite the records they used\./)
    for (const text of [placeholder, empty]) expect(text).not.toMatch(/substrate/i)
  })
})
