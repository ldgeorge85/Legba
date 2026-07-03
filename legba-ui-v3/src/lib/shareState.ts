/**
 * Shareable state — addressability WITHOUT a router (S7-T2 task 5).
 *
 * The one shared selection is the workstation's brushing anchor: click a desk
 * and the feed, map, timeline, report and Inspector all follow it. This module
 * serializes that selection to the URL hash (`#sel=<kind>:<id>`) so a link
 * carries "the desk I'm looking at" — the vision's "selection serializes to a
 * shareable hash/state" — with zero react-router (the app is a single Dockview
 * root; there are no routes).
 *
 * Layout is addressable separately via the sidebar Layouts menu (named
 * save/restore, localStorage). Only the selection rides the hash so a shared
 * link never clobbers the recipient's composed wall.
 */
import { useEffect } from 'react'
import { useSelection, type SelectionKind } from '@/state/selection'

const VALID_KINDS: ReadonlySet<string> = new Set<SelectionKind>([
  'target',
  'entity',
  'source',
  'analyst',
  'finding',
  'situation',
  'signal',
])

interface ParsedSel {
  kind: SelectionKind
  id: string
}

/** Parse `#sel=<kind>:<id>` from the current hash; null if absent/invalid. */
function parseHash(): ParsedSel | null {
  try {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    const sel = params.get('sel')
    if (!sel) return null
    const idx = sel.indexOf(':')
    if (idx < 0) return null
    const kind = sel.slice(0, idx)
    const id = decodeURIComponent(sel.slice(idx + 1))
    if (!VALID_KINDS.has(kind) || !id) return null
    return { kind: kind as SelectionKind, id }
  } catch {
    return null
  }
}

/** Write (or clear) the `sel` hash param without adding a history entry. */
function writeHash(kind: SelectionKind | null, id: string | null): void {
  try {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    if (kind && id) params.set('sel', `${kind}:${encodeURIComponent(id)}`)
    else params.delete('sel')
    const q = params.toString()
    const next = q ? `#${q}` : ''
    if (next !== window.location.hash) {
      const { pathname, search } = window.location
      window.history.replaceState(null, '', `${pathname}${search}${next}`)
    }
  } catch {
    // history/URL unavailable — sharing is best-effort, never fatal.
  }
}

/**
 * Mount once (from the App root). Restores the selection from the hash on load,
 * mirrors every selection change back into the hash, and follows manual hash
 * edits / browser back-forward.
 */
export function useShareState(): void {
  useEffect(() => {
    const applyHash = () => {
      const parsed = parseHash()
      if (parsed) {
        const cur = useSelection.getState().selection
        if (!cur || cur.kind !== parsed.kind || cur.id !== parsed.id) {
          useSelection.getState().select({ kind: parsed.kind, id: parsed.id, origin: 'share-link' })
        }
      }
    }
    // 1. Restore on load.
    applyHash()
    // 2. Selection → hash.
    const unsub = useSelection.subscribe((s) =>
      writeHash(s.selection?.kind ?? null, s.selection?.id ?? null),
    )
    // 3. Hash → selection (manual edit / back-forward).
    window.addEventListener('hashchange', applyHash)
    return () => {
      unsub()
      window.removeEventListener('hashchange', applyHash)
    }
  }, [])
}
