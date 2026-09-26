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
import { MISSIONS, missionShows } from '@/lib/missions'

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

/**
 * The MISSION chooser (wave-P design pass).
 *
 * The bar is still presentational — it applies nothing. What it owes the reader
 * is that every mission is named, described, and honest about what choosing it
 * will do, and that the stances which carry no mission are named rather than
 * quietly missing from the list.
 */
describe('WorkspaceBar — the mission chooser', () => {
  it('has no mission control at all when the shell wires no handler', () => {
    render(<WorkspaceBar active="morning_read" onSwitch={() => {}} onReset={() => {}} />)
    expect(screen.queryByTestId('mission-trigger')).toBeNull()
  })

  it('opens a chooser listing all four missions, each with its description', () => {
    render(
      <WorkspaceBar
        active="morning_read"
        onSwitch={() => {}}
        onReset={() => {}}
        onChooseMission={() => {}}
      />,
    )
    expect(screen.queryByTestId('mission-chooser')).toBeNull()
    fireEvent.click(screen.getByTestId('mission-trigger'))
    const chooser = screen.getByTestId('mission-chooser')
    for (const m of MISSIONS) {
      const option = screen.getByTestId(`mission-option-${m.id}`)
      expect(option).toHaveTextContent(m.label)
      expect(option).toHaveTextContent(m.description)
    }
    expect(chooser.querySelectorAll('[role="menuitem"]')).toHaveLength(MISSIONS.length)
  })

  it('shows the derived "what this mission shows" line beside each description', () => {
    render(
      <WorkspaceBar
        active="desk"
        onSwitch={() => {}}
        onReset={() => {}}
        onChooseMission={() => {}}
      />,
    )
    fireEvent.click(screen.getByTestId('mission-trigger'))
    for (const m of MISSIONS) {
      expect(screen.getByTestId(`mission-shows-${m.id}`)).toHaveTextContent(missionShows(m))
    }
    // The line names the axes a tab strip never carried.
    expect(screen.getByTestId('mission-shows-crisis')).toHaveTextContent('6h window')
    expect(screen.getByTestId('mission-shows-crisis')).toHaveTextContent('Consult pinned')
  })

  it('names the stances that carry no mission rather than omitting them silently', () => {
    render(
      <WorkspaceBar
        active="morning_read"
        onSwitch={() => {}}
        onReset={() => {}}
        onChooseMission={() => {}}
      />,
    )
    fireEvent.click(screen.getByTestId('mission-trigger'))
    const note = screen.getByTestId('mission-chooser-footnote')
    expect(note).toHaveTextContent('The Gate and Engine')
    expect(note).toHaveTextContent('no desk, no window and no layers')
  })

  it('choosing a mission asks the shell for it and closes the chooser', () => {
    const onChooseMission = vi.fn()
    render(
      <WorkspaceBar
        active="morning_read"
        onSwitch={() => {}}
        onReset={() => {}}
        onChooseMission={onChooseMission}
      />,
    )
    fireEvent.click(screen.getByTestId('mission-trigger'))
    fireEvent.click(screen.getByTestId('mission-option-crisis'))
    expect(onChooseMission).toHaveBeenCalledWith('crisis')
    expect(screen.queryByTestId('mission-chooser')).toBeNull()
  })

  it('Escape and a click outside both dismiss it — no trap', () => {
    render(
      <WorkspaceBar
        active="morning_read"
        onSwitch={() => {}}
        onReset={() => {}}
        onChooseMission={() => {}}
      />,
    )
    fireEvent.click(screen.getByTestId('mission-trigger'))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('mission-chooser')).toBeNull()

    fireEvent.click(screen.getByTestId('mission-trigger'))
    fireEvent.click(screen.getByTestId('mission-scrim'))
    expect(screen.queryByTestId('mission-chooser')).toBeNull()
  })

  it('marks the active mission on the trigger and in the list', () => {
    render(
      <WorkspaceBar
        active="trust"
        onSwitch={() => {}}
        onReset={() => {}}
        activeMission="release"
        onChooseMission={() => {}}
      />,
    )
    expect(screen.getByTestId('mission-active-chip')).toHaveTextContent('Release')
    fireEvent.click(screen.getByTestId('mission-trigger'))
    expect(screen.getByTestId('mission-option-release')).toHaveTextContent('current')
    expect(screen.getByTestId('mission-option-crisis')).not.toHaveTextContent('current')
  })

  it("shows what the mission DID to the scope in place of the stance's question", () => {
    render(
      <WorkspaceBar
        active="desk"
        onSwitch={() => {}}
        onReset={() => {}}
        activeMission="desk_watch"
        missionNote="Desk Watch — no desk selected — Desk Watch left the wall's scope as it was"
        onChooseMission={() => {}}
      />,
    )
    // A mission that could not do what its name promises says so where the
    // reader is already looking.
    expect(screen.getByTestId('workspace-question')).toHaveTextContent('no desk selected')
  })

  it('falls back to the stance question when no mission is active', () => {
    render(
      <WorkspaceBar
        active="gate"
        onSwitch={() => {}}
        onReset={() => {}}
        onChooseMission={() => {}}
      />,
    )
    expect(screen.getByTestId('workspace-question')).toHaveTextContent('Work the human queues')
  })
})
