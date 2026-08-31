/**
 * Component tests for the workspace bar (UI_HOLISTIC_DESIGN_2026-08-24 §2.3).
 *
 * The bar is the front door: six tabs, always visible, one click or one
 * keystroke each. What must hold:
 *  - all six stances render, in bar order, with the ACTIVE one marked
 *    (`aria-selected`) so the operator always knows which stance they are in;
 *  - clicking a tab asks the shell to switch — the bar owns no layout state
 *    itself (switching-saves-the-outgoing lives in App.tsx / lib/workspaces);
 *  - clicking the active tab is a no-op switch request, never a reseed;
 *  - the reset affordance is present and separate from switching (it is the
 *    only control that discards an arrangement).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { WorkspaceBar } from './WorkspaceBar'
import { WORKSPACES } from '@/lib/workspaces'

describe('WorkspaceBar', () => {
  it('renders all six stances in bar order', () => {
    render(<WorkspaceBar active="morning_read" onSwitch={() => {}} onReset={() => {}} />)
    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual(WORKSPACES.map((w) => w.label))
  })

  it('marks exactly the active stance', () => {
    render(<WorkspaceBar active="trust" onSwitch={() => {}} onReset={() => {}} />)
    expect(screen.getByTestId('workspace-tab-trust')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('workspace-tab-morning_read')).toHaveAttribute(
      'aria-selected',
      'false',
    )
    expect(screen.getAllByRole('tab').filter((t) => t.getAttribute('aria-selected') === 'true')).toHaveLength(1)
  })

  it('clicking a tab requests that stance', () => {
    const onSwitch = vi.fn()
    render(<WorkspaceBar active="morning_read" onSwitch={onSwitch} onReset={() => {}} />)
    fireEvent.click(screen.getByTestId('workspace-tab-gate'))
    expect(onSwitch).toHaveBeenCalledWith('gate')
  })

  it('advertises each stance\'s question and its Alt+N key in the tooltip', () => {
    render(<WorkspaceBar active="morning_read" onSwitch={() => {}} onReset={() => {}} />)
    const investigate = WORKSPACES.find((w) => w.id === 'investigate')!
    const tab = screen.getByTestId('workspace-tab-investigate')
    expect(tab.getAttribute('title')).toContain(investigate.question)
    expect(tab.getAttribute('title')).toContain('Alt+3')
  })

  it('shows the active stance\'s question as the bar\'s own caption', () => {
    render(<WorkspaceBar active="gate" onSwitch={() => {}} onReset={() => {}} />)
    expect(screen.getByTestId('workspace-question')).toHaveTextContent('Work the human queues')
  })

  it('reset is a separate control from switching', () => {
    const onSwitch = vi.fn()
    const onReset = vi.fn()
    render(<WorkspaceBar active="desk" onSwitch={onSwitch} onReset={onReset} />)
    fireEvent.click(screen.getByTestId('workspace-reset'))
    expect(onReset).toHaveBeenCalledTimes(1)
    expect(onSwitch).not.toHaveBeenCalled()
  })
})
