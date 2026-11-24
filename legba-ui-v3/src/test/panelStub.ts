/**
 * Test helper: a stand-in panel MODULE for `vi.mock` (UI-B1/merges).
 *
 * A merged wrapper's contract is "this tab mounts THAT component". Mocking each
 * child module with a stub that renders `data-testid="mounted-<name>"` proves
 * exactly that, with no network and no child internals in the way:
 *
 *   vi.mock('@/panels/system/DeadLetter', async () =>
 *     (await import('@/test/panelStub')).panelStub('DeadLetter'))
 *
 * The stub also echoes the `initialTab` it was handed, so a test can see what
 * the wrapper passed through.
 */
import { createElement } from 'react'
import type { PanelProps } from '@/types'

export function panelStub(name: string): { default: (props: Partial<PanelProps>) => ReturnType<typeof createElement> } {
  return {
    default: (props: Partial<PanelProps>) =>
      createElement('div', { 'data-testid': `mounted-${name}`, 'data-initial-tab': props.initialTab ?? '' }, name),
  }
}
