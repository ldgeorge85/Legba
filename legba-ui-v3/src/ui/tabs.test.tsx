/**
 * Tabs primitive — a roving-focus tablist: one Tab stop, arrows move and
 * activate, Home/End jump, a click (even a synthetic one) activates.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { Tabs, TabsContent, TabsList, TabsTrigger } from './tabs'

afterEach(cleanup)

function Harness() {
  const [v, setV] = useState('a')
  return (
    <>
      <button type="button" data-testid="before">before</button>
      <Tabs value={v} onValueChange={setV}>
        <TabsList aria-label="sections">
          <TabsTrigger value="a" data-testid="t-a">A</TabsTrigger>
          <TabsTrigger value="b" data-testid="t-b">B</TabsTrigger>
          <TabsTrigger value="c" data-testid="t-c">C</TabsTrigger>
        </TabsList>
        <TabsContent value="a">panel A</TabsContent>
        <TabsContent value="b">panel B</TabsContent>
        <TabsContent value="c">panel C</TabsContent>
      </Tabs>
      <span data-testid="value">{v}</span>
    </>
  )
}

describe('Tabs primitive', () => {
  it('renders a labelled tablist with exactly one selected tab', () => {
    render(<Harness />)
    expect(screen.getByRole('tablist')).toHaveAccessibleName('sections')
    const selected = screen.getAllByRole('tab').filter((t) => t.getAttribute('aria-selected') === 'true')
    expect(selected).toHaveLength(1)
    expect(selected[0]).toHaveTextContent('A')
    expect(screen.getByRole('tabpanel')).toHaveTextContent('panel A')
  })

  it('is ONE Tab stop: tabbing in lands on the active tab only', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    screen.getByTestId('before').focus()
    await user.tab()
    expect(screen.getByTestId('t-a')).toHaveFocus()
    expect(screen.getByTestId('t-b')).toHaveAttribute('tabindex', '-1')
  })

  it('arrow keys move focus and activate; Home/End jump; arrows wrap', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    screen.getByTestId('t-a').focus()
    await user.keyboard('{ArrowRight}')
    expect(screen.getByTestId('t-b')).toHaveFocus()
    expect(screen.getByTestId('value')).toHaveTextContent('b')
    await user.keyboard('{End}')
    expect(screen.getByTestId('value')).toHaveTextContent('c')
    await user.keyboard('{ArrowRight}')
    expect(screen.getByTestId('value')).toHaveTextContent('a')
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByTestId('value')).toHaveTextContent('c')
    await user.keyboard('{Home}')
    expect(screen.getByTestId('value')).toHaveTextContent('a')
  })

  it('a click activates — a real one and a synthetic fireEvent.click alike', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByTestId('t-c'))
    expect(screen.getByTestId('value')).toHaveTextContent('c')
    fireEvent.click(screen.getByTestId('t-b'))
    expect(screen.getByTestId('value')).toHaveTextContent('b')
    expect(screen.getByTestId('t-b')).toHaveAttribute('aria-selected', 'true')
  })
})
