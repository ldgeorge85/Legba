/**
 * U-5 — the shared query layer: one cache across regions, identical reads
 * shared, a stale time honoured across panels, and a region nobody can see
 * fetching nothing (the Dockview tab kept mounted while hidden; the palette
 * mounted while closed).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { QueryRegion, TileQueryRegion, attributeFetch, installFetchLog } from './queryLayer'
import { DockviewPanelApiProvider, type TilePanelApi } from '@/components/DockviewPanelApiContext'

function Reader({ qkey, fn, staleTime, testid }: { qkey: string; fn: () => Promise<string>; staleTime?: number; testid: string }) {
  const q = useQuery({ queryKey: ['u5', qkey], queryFn: fn, staleTime })
  return <div data-testid={testid}>{q.data ?? (q.isFetching ? 'fetching' : 'idle')}</div>
}

function Root({ client, children }: { client: QueryClient; children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

const flush = async (ms = 0) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('queryLayer — regions over one shared cache', () => {
  let client: QueryClient
  beforeEach(() => {
    vi.useFakeTimers()
    client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } })
    window.__legbaFetchLog = []
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    client.clear()
  })

  it('an inactive region fetches nothing; shown, it renders its loading state in that same frame and reads', async () => {
    let release: (v: string) => void = () => undefined
    const fn = vi.fn(() => new Promise<string>((r) => (release = r)))
    const tree = (active: boolean) => (
      <Root client={client}>
        <QueryRegion id="t-hidden" active={active}>
          <Reader qkey="a" fn={fn} testid="r" />
        </QueryRegion>
      </Root>
    )
    const { rerender } = render(tree(false))
    await flush(500)
    expect(fn).not.toHaveBeenCalled()
    expect(screen.getByTestId('r').textContent).toBe('idle')

    rerender(tree(true))
    // the very render that shows the tab is already a loading render — never a
    // disabled "nothing yet" painted for a frame
    expect(screen.getByTestId('r').textContent).toBe('fetching')
    await flush(0)
    expect(fn).toHaveBeenCalledTimes(1)
    await act(async () => {
      release('read')
      await vi.advanceTimersByTimeAsync(10)
    })
    expect(screen.getByTestId('r').textContent).toBe('read')
  })

  it('a region visible when it mounts reads from its first render', async () => {
    const fn = vi.fn(async () => 'first')
    render(
      <Root client={client}>
        <QueryRegion id="t-front" active>
          <Reader qkey="front" fn={fn} testid="r" />
        </QueryRegion>
      </Root>,
    )
    expect(screen.getByTestId('r').textContent).toBe('fetching')
    await flush(10)
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('a hidden region reads what a visible panel fetched — one cache, one request', async () => {
    const fn = vi.fn(async () => 'shared')
    render(
      <Root client={client}>
        <QueryRegion id="t-visible" active>
          <Reader qkey="s" fn={fn} testid="visible" />
        </QueryRegion>
        <QueryRegion id="t-hidden2" active={false}>
          <Reader qkey="s" fn={fn} testid="hidden" />
        </QueryRegion>
      </Root>,
    )
    await flush(10)
    expect(fn).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('visible').textContent).toBe('shared')
    expect(screen.getByTestId('hidden').textContent).toBe('shared')
  })

  it('two panels naming one key in the frame share one request (dedupe)', async () => {
    const fn = vi.fn(async () => 'one')
    render(
      <Root client={client}>
        <Reader qkey="d" fn={fn} testid="a" />
        <Reader qkey="d" fn={fn} testid="b" />
      </Root>,
    )
    await flush(10)
    expect(fn).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('b').textContent).toBe('one')
  })

  it('a stale time is honoured across regions: a tab shown within it asks nothing', async () => {
    const fn = vi.fn(async () => 'fresh')
    const tree = (shown: boolean) => (
      <Root client={client}>
        <Reader qkey="slow" fn={fn} staleTime={60_000} testid="frame" />
        <QueryRegion id="t-later" active={shown}>
          <Reader qkey="slow" fn={fn} staleTime={60_000} testid="tab" />
        </QueryRegion>
      </Root>
    )
    const { rerender } = render(tree(false))
    await flush(10)
    expect(fn).toHaveBeenCalledTimes(1)
    rerender(tree(true))
    await flush(10)
    expect(fn).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('tab').textContent).toBe('fresh')
  })

  it('a region going hidden stops its polling', async () => {
    const fn = vi.fn(async () => 'tick')
    function Poller() {
      const q = useQuery({ queryKey: ['u5', 'poll'], queryFn: fn, refetchInterval: 1_000 })
      return <div>{q.data}</div>
    }
    const tree = (active: boolean) => (
      <Root client={client}>
        <QueryRegion id="t-poll" active={active}>
          <Poller />
        </QueryRegion>
      </Root>
    )
    const { rerender } = render(tree(true))
    await flush(10)
    await flush(2_100)
    const seen = fn.mock.calls.length
    expect(seen).toBeGreaterThanOrEqual(2)
    rerender(tree(false))
    await flush(5_000)
    expect(fn.mock.calls.length).toBe(seen)
  })

  it('the fetch log marks a fetch made only for an inactive region as hidden', async () => {
    const off = installFetchLog(client)
    const fn = vi.fn(async () => 'x')
    function Forcer() {
      // `refetch()` ignores `enabled` — exactly the bypass the log exists to catch
      const q = useQuery({ queryKey: ['u5', 'forced'], queryFn: fn })
      const c = useQueryClient()
      return (
        <button type="button" data-testid="force" onClick={() => void q.refetch()}>
          {c ? 'go' : ''}
        </button>
      )
    }
    render(
      <Root client={client}>
        <QueryRegion id="t-forced" active={false}>
          <Forcer />
        </QueryRegion>
        <Reader qkey="frame" fn={async () => 'f'} testid="frame" />
      </Root>,
    )
    await flush(10)
    await act(async () => {
      screen.getByTestId('force').click()
      await vi.advanceTimersByTimeAsync(10)
    })
    const log = window.__legbaFetchLog ?? []
    const forced = log.find((e) => e.key.includes('forced'))
    const frame = log.find((e) => e.key.includes('frame'))
    expect(forced?.hidden).toBe(true)
    expect(forced?.tile).toBe('t-forced')
    expect(frame?.hidden).toBe(false)
    off()
  })

  it('a Dockview tile: a hidden tab fetches nothing until Dockview shows it', async () => {
    let visible = false
    const listeners: Array<(e: { isVisible: boolean }) => void> = []
    const api = {
      id: 'wall-tab',
      get isVisible() {
        return visible
      },
      onDidVisibilityChange: (cb: (e: { isVisible: boolean }) => void) => {
        listeners.push(cb)
        return { dispose: () => undefined }
      },
    } as unknown as TilePanelApi
    const fn = vi.fn(async () => 'wall')
    render(
      <Root client={client}>
        <DockviewPanelApiProvider value={api}>
          <TileQueryRegion>
            <Reader qkey="wall" fn={fn} testid="w" />
          </TileQueryRegion>
        </DockviewPanelApiProvider>
      </Root>,
    )
    await flush(500)
    expect(fn).not.toHaveBeenCalled()
    await act(async () => {
      visible = true
      for (const l of listeners) l({ isVisible: true })
    })
    await flush(10)
    expect(fn).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('w').textContent).toBe('wall')
  })

  it('attribution: no observers, or any observer outside an inactive region, is not hidden', () => {
    expect(attributeFetch([]).hidden).toBe(false)
    expect(attributeFetch([{}]).hidden).toBe(false)
  })
})
