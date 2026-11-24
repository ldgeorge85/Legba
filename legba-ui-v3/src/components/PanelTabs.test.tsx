/**
 * PanelTabStrip on the Tabs primitive (UI-B1/tokens): same API, same test ids,
 * and now a roving-focus tablist (arrows move and activate).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { initialTab, PanelTabStrip, type PanelTabDef } from './PanelTabs'

afterEach(cleanup)

const TABS: readonly PanelTabDef[] = [
  { id: 'why', label: 'Why' },
  { id: 'lineage', label: 'Lineage' },
  { id: 'triggers', label: 'Triggers', badge: 'preview' },
]

function Harness({ onChange }: { onChange?: (id: string) => void }) {
  const [tab, setTab] = useState('why')
  return (
    <PanelTabStrip
      tabs={TABS}
      active={tab}
      onChange={(id) => {
        setTab(id)
        onChange?.(id)
      }}
      ariaLabel="Provenance views"
      testIdPrefix="prov-tab"
    />
  )
}

describe('PanelTabStrip', () => {
  it('renders a labelled tablist with the test ids and the badge', () => {
    render(<Harness />)
    expect(screen.getByRole('tablist')).toHaveAccessibleName('Provenance views')
    expect(screen.getByTestId('prov-tab-why')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('prov-tab-lineage')).toHaveAttribute('aria-selected', 'false')
    expect(screen.getByTestId('prov-tab-triggers')).toHaveTextContent('preview')
  })

  it('a click switches tabs (fireEvent.click, as the merged-panel tests use)', () => {
    const onChange = vi.fn()
    render(<Harness onChange={onChange} />)
    fireEvent.click(screen.getByTestId('prov-tab-lineage'))
    expect(onChange).toHaveBeenLastCalledWith('lineage')
    expect(screen.getByTestId('prov-tab-lineage')).toHaveAttribute('aria-selected', 'true')
  })

  it('←/→ move between tabs and activate them', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Harness onChange={onChange} />)
    screen.getByTestId('prov-tab-why').focus()
    await user.keyboard('{ArrowRight}')
    expect(screen.getByTestId('prov-tab-lineage')).toHaveFocus()
    expect(onChange).toHaveBeenLastCalledWith('lineage')
    await user.keyboard('{ArrowLeft}')
    expect(onChange).toHaveBeenLastCalledWith('why')
  })

  it('carries no aria-controls to a tabpanel the caller does not render', () => {
    render(<Harness />)
    for (const t of screen.getAllByRole('tab')) expect(t).not.toHaveAttribute('aria-controls')
  })

  it('initialTab keeps its fallback contract', () => {
    expect(initialTab('lineage', TABS, 'why')).toBe('lineage')
    expect(initialTab('gone', TABS, 'why')).toBe('why')
    expect(initialTab(undefined, TABS, 'why')).toBe('why')
  })
})
