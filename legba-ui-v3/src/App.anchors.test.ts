/**
 * Anchor-panel policy (operator call 2026-08-04).
 *
 * The Live Feed (`system.findings`) and the Inspector (`system.inspector`) used
 * to be PINNED — rendered with the close-button-less `AnchorTab` so they could
 * not be closed out of the workspace. They are now ordinary closable panels:
 * nothing is stranded by closing one, since both still open by default at boot
 * and reopen from the sidebar or the command palette.
 *
 * `ANCHOR_KINDS` is the whole switch (`App.tsx` passes `tabComponent: 'anchor'`
 * only for kinds in this set), so asserting it is empty is asserting every
 * panel is closable. The `AnchorTab` machinery is deliberately retained for a
 * future pin, which is why this pins the SET rather than the mechanism.
 */
import { describe, it, expect } from 'vitest'
import { ANCHOR_KINDS } from './App'
import { DEFAULT_BOOT_LAYOUT } from '@/lib/layoutPresets'

describe('anchor panels', () => {
  it('pins nothing — every panel carries a close button', () => {
    expect(ANCHOR_KINDS.size).toBe(0)
  })

  it('leaves the Live Feed and the Inspector closable', () => {
    expect(ANCHOR_KINDS.has('system.findings')).toBe(false)
    expect(ANCHOR_KINDS.has('system.inspector')).toBe(false)
  })

  it('still seeds both panels at boot (unpinned is not unopened)', () => {
    const seeded = DEFAULT_BOOT_LAYOUT.map((p) => p.kind)
    expect(seeded).toContain('system.findings')
    expect(seeded).toContain('system.inspector')
  })
})
