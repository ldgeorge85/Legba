import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { InspectorBody } from './FlowInspector'
import { DiffView } from './FlowCompare'
import { RunDetailView } from './FlowRunView'
import { nodeDetailFixture } from './flowFixtures'
import type { FlowRunDetail } from './flowTypes'

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

function openTab(id: string) {
  const tab = screen.getByTestId(`flow-inspector-tab-${id}`)
  fireEvent.mouseDown(tab)
  fireEvent.click(tab)
  fireEvent.focus(tab)
}

describe('Flow inspector — read-only', () => {
  it('shows every field, the identity and the declared edges', () => {
    wrap(<InspectorBody d={nodeDetailFixture} runId={null} onRun={() => {}} />)
    expect(screen.getByTestId('flow-inspector')).toBeInTheDocument()
    expect(screen.getByText('a'.repeat(64))).toBeInTheDocument()
    expect(screen.getByTestId('flow-fields').textContent).toContain('ESCALATION RISK')
    expect(screen.getByText(/via subscription.targets.predicate/)).toBeInTheDocument()
  })

  it('has no edit affordance anywhere', () => {
    wrap(<InspectorBody d={nodeDetailFixture} runId={null} onRun={() => {}} />)
    for (const id of ['definition', 'prompt', 'contract', 'params', 'runs', 'versions', 'undeclared']) {
      openTab(id)
      for (const b of screen.queryAllByRole('button')) {
        expect(b.textContent ?? '').not.toMatch(/save|edit|publish|delete|apply|pause|activate/i)
      }
      expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    }
  })

  it('the prompt tab shows the stored rendering with its basis, and the declared template', () => {
    wrap(<InspectorBody d={nodeDetailFixture} runId={null} onRun={() => {}} />)
    openTab('prompt')
    expect(screen.getByTestId('flow-prompt-rendered').textContent).toContain('senior all-source intelligence analyst')
    expect(screen.getByText(/most recent run that recorded one/)).toBeInTheDocument()
    expect(screen.getByTestId('flow-prompt-declared').textContent).toContain('ESCALATION RISK')
  })

  it('the sampling tab separates configured from sent', () => {
    wrap(<InspectorBody d={nodeDetailFixture} runId={null} onRun={() => {}} />)
    openTab('params')
    const sent = screen.getByTestId('flow-params-sent')
    expect(within(sent).getByText('not sent')).toBeInTheDocument()
    expect(within(sent).getByText(/NOT SENT: vllm.py/)).toBeInTheDocument()
    expect(screen.getByText('4096')).toBeInTheDocument()
  })

  it('the runs tab lists runs, shows the observed sequence, and selects a run', () => {
    const onRun = vi.fn()
    wrap(<InspectorBody d={nodeDetailFixture} runId={null} onRun={onRun} />)
    openTab('runs')
    expect(screen.getByText(/observed, not declared/)).toBeInTheDocument()
    expect(screen.getByTestId('flow-observed-steps').textContent).toContain('reason · llm_call')
    fireEvent.click(screen.getByTestId('flow-run-row-r1'))
    expect(onRun).toHaveBeenCalledWith('r1')
  })

  it('the undeclared tab lists what the registry cannot say', () => {
    wrap(<InspectorBody d={nodeDetailFixture} runId={null} onRun={() => {}} />)
    openTab('undeclared')
    expect(screen.getByTestId('flow-undeclared').textContent).toContain('this analyst writes are not declared')
  })
})

describe('run overlay detail', () => {
  const run: FlowRunDetail = {
    run_id: 'r1', analyst_id: 'escalation', status: 'success', started_at: '2026-11-13T06:57:35Z',
    ended_at: '2026-11-13T06:57:58Z', duration_ms: 22900, tokens: 32678, sequence_label: 'observed, not declared',
    steps: [{ index: 0, bytes: 30, phase: 'wake', kind: 'envelope' }, { index: 1, bytes: 900, phase: 'ground', kind: 'inject_preamble', preamble_chars: 1387 }],
    llm_calls: [{ index: 0, leg: 'verify_judge', model: 'gemma', status: 'success', duration_ms: 1777, prompt_tokens: 4565, completion_tokens: 8192, finish_reason: 'length' }],
    tool_calls: [],
    input_refs: { count: 53, ids: [], listed: 50 }, output_refs: { count: 1, ids: ['o1'] },
    outputs: [{ id: 'o1', table: 'analyst_outputs', kind: 'finding', gates: { 'data.data.verification': { ok: true } }, judge_critiques: [] }],
  }

  it('lists steps with sizes, calls with finish reasons, and the gate verdicts on the output row', () => {
    wrap(<RunDetailView run={run} />)
    expect(screen.getByTestId('flow-run-steps').textContent).toContain('inject_preamble')
    expect(screen.getByText('preamble_chars=1387')).toBeInTheDocument()
    expect(screen.getByText('length')).toBeInTheDocument()
    expect(screen.getByTestId('flow-run-outputs').textContent).toContain('data.data.verification')
    expect(screen.getAllByText('fetch body').length).toBeGreaterThan(0)
  })
})

describe('compare view', () => {
  it('renders a semantic field diff and a prompt text diff', () => {
    const before = { node_id: 'analyst:x', version: 'b'.repeat(64), state: 'retired', is_head: false, created_at: null, fields: { method: { system_prompt: 'line one\nline two', temperature: 0.2 } } }
    const after = { ...before, version: 'a'.repeat(64), is_head: true, fields: { method: { system_prompt: 'line one\nline 2', temperature: 1 } } }
    wrap(<DiffView before={before} after={after} />)
    expect(screen.getByTestId('flow-diff').textContent).toContain('method.temperature')
    expect(screen.getByText('Text diff — method.system_prompt')).toBeInTheDocument()
    expect(screen.getByText('+ line 2')).toBeInTheDocument()
    wrap(<DiffView before={before} after={before} />)
    expect(screen.getByTestId('flow-diff-empty')).toBeInTheDocument()
  })
})
