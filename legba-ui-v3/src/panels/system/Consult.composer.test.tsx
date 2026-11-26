/**
 * The Consult composer is one compact block: the line naming what the question
 * is about, Deep, On record only and a "⋯" menu above the box; the box with Ask
 * beside it. Ask never wraps under the box or after a row of controls, and
 * every other setting is behind the menu with a one-line summary of what is
 * not at its default.
 *
 * jsdom has no layout engine, so pixel positions are checked by the walk
 * (`tools/walk`); what is asserted here is the structure that makes a cut-off
 * Ask impossible, at the dock widths the tile is measured at (the width is
 * delivered through the same ResizeObserver path the shell uses).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

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

describe('the Consult composer', () => {
  for (const width of DOCK_WIDTHS) {
    it(`keeps the box and Ask on one row, and the settings out of the way, at a ${width} px dock`, () => {
      DOCK_WIDTH = width
      act(() => {
        render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
      })
      const textarea = screen.getByTestId('consult-question')
      const row = screen.getByTestId('consult-question-row')
      const ask = screen.getByTestId('consult-submit')

      // The box takes the row's spare width; Ask sits beside it and never
      // shrinks, so it cannot fall off the end of the dock.
      expect(row.contains(textarea)).toBe(true)
      expect(row.contains(ask)).toBe(true)
      expect(textarea.className).toMatch(/\bmin-w-0\b/)
      expect(textarea.className).toMatch(/\bflex-1\b/)
      expect(ask.className).toMatch(/\bshrink-0\b/)
      expect(ask).toHaveTextContent('Ask')

      // The composer shows only: Deep, On record only and the menu. The rest
      // is behind the menu until it is opened.
      const controls = screen.getByTestId('consult-composer-controls')
      expect(controls.className).toMatch(/\bflex-wrap\b/)
      for (const id of ['consult-deep-toggle', 'consult-grounding', 'consult-more']) {
        expect(controls.contains(screen.getByTestId(id))).toBe(true)
      }
      for (const id of ['consult-drive-toggle', 'consult-allow-writes', 'consult-model', 'consult-history-toggle', 'consult-new-chat', 'consult-pin', 'consult-scope', 'consult-max-rounds', 'consult-window']) {
        expect(screen.queryByTestId(id)).toBeNull()
      }
      // Reading order: the answers, then the controls, then the box.
      const transcript = screen.getByTestId('consult-scroll')
      expect(transcript.compareDocumentPosition(controls) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(controls.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })
  }

  it('the menu holds every other setting, opens from the keyboard, and has no second window selector', async () => {
    DOCK_WIDTH = 640
    act(() => {
      render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    })
    const more = screen.getByTestId('consult-more')
    more.focus()
    await userEvent.keyboard('{Enter}')
    for (const id of ['consult-drive-toggle', 'consult-allow-writes', 'consult-model', 'consult-history-toggle', 'consult-new-chat', 'consult-pin', 'consult-scope', 'consult-max-rounds']) {
      expect(await screen.findByTestId(id)).toBeInTheDocument()
    }
    // The window is shown as read-only words, never a select.
    expect(screen.queryByTestId('consult-window')).toBeNull()
    // ...and they are the time band's window (24 hours by default), not Consult's own.
    expect(screen.getByTestId('consult-window-words').textContent).toMatch(/24h/)
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByTestId('consult-scope')).toBeNull()
  })

  it('says in one line which menu settings are not at their defaults', async () => {
    DOCK_WIDTH = 640
    act(() => {
      render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    })
    expect(screen.queryByTestId('consult-nondefault')).toBeNull()
    await userEvent.click(screen.getByTestId('consult-more'))
    await userEvent.click(await screen.findByTestId('consult-drive-toggle'))
    await userEvent.type(screen.getByTestId('consult-scope'), 'x == 1')
    const line = await screen.findByTestId('consult-nondefault')
    expect(line.textContent).toMatch(/drive the view on/)
    expect(line.textContent).toMatch(/scope set/)
  })

  it('Deep is a switch in the same composer: no second tab or form, and no off-palette colours', () => {
    DOCK_WIDTH = 640
    act(() => {
      render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    })
    expect(screen.queryByTestId('consult-depth-deep')).toBeNull()
    const deep = screen.getByTestId('consult-deep-toggle')
    expect(deep).toHaveAttribute('role', 'switch')
    expect(deep).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(deep)
    expect(deep).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('consult-question').getAttribute('placeholder')).toMatch(/deep question/i)
    expect(screen.getAllByTestId('consult-question')).toHaveLength(1)
    expect(document.body.innerHTML).not.toMatch(/indigo|purple|violet/)
  })

  it('asks in plain words — no engine vocabulary in the placeholder or empty state', () => {
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
