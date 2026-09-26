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
import { emptyMembers, useScope, type Scope, type ScopeKind } from '@/state/scope'

const VALID_KINDS: ReadonlySet<string> = new Set<SelectionKind>([
  'target',
  'entity',
  'source',
  'analyst',
  'finding',
  'situation',
  'signal',
  'report',
  'journal_entry',
])

/** The scope kinds a link may carry (`#scope=<kind>:<id>`). */
const VALID_SCOPE_KINDS: ReadonlySet<string> = new Set<ScopeKind>([
  'report',
  'target',
  'entity',
  'situation',
  'journal_entry',
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
 * Parse `#scope=<kind>:<id>` — what the shared wall is ABOUT, beside `#sel=`
 * (what the sender was reading). Null when absent or unparseable.
 */
function parseScopeHash(): { kind: ScopeKind; id: string } | null {
  try {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    const raw = params.get('scope')
    if (!raw) return null
    const idx = raw.indexOf(':')
    if (idx < 0) return null
    const kind = raw.slice(0, idx)
    const id = decodeURIComponent(raw.slice(idx + 1))
    if (!VALID_SCOPE_KINDS.has(kind) || !id) return null
    return { kind: kind as ScopeKind, id }
  } catch {
    return null
  }
}

/** Write (or clear) the `scope` hash param without adding a history entry. */
function writeScopeHash(scope: Scope | null): void {
  try {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    if (scope && scope.kind !== 'none' && scope.id) {
      params.set('scope', `${scope.kind}:${encodeURIComponent(scope.id)}`)
    } else params.delete('scope')
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
 * Read `#ws=<workspace>` — the STANCE a shared link carries alongside the
 * record (design §3.5 #4: "look at this finding *in Investigate*"). Returns
 * null when absent; the caller validates the id against the workspace list.
 */
export function readWorkspaceHash(): string | null {
  try {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    return params.get('ws')
  } catch {
    return null
  }
}

/**
 * Mirror the active workspace into the hash, preserving `sel` (the two params
 * share one hash: `#sel=finding:8f21&ws=investigate`). Replace, never push —
 * switching stances is not a history entry.
 */
export function writeWorkspaceHash(ws: string | null): void {
  try {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    if (ws) params.set('ws', ws)
    else params.delete('ws')
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
 * Read `#mission=<id>` — the JOB a shared link carries, beside the stance.
 *
 * A mission is a stance PLUS a scope, a window and a layer selection
 * (`lib/missions.ts`), so a link that carries one hands the recipient the same
 * aperture the sender was reading at, not merely the same tiles. Returns null
 * when absent; the caller validates the id against the mission list.
 */
export function readMissionHash(): string | null {
  try {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    return params.get('mission')
  } catch {
    return null
  }
}

/**
 * Mirror the active mission into the hash, preserving every other param (the
 * four share one hash: `#sel=finding:8f21&scope=target:x&ws=desk&mission=desk_watch`).
 * Replace, never push — choosing a mission is not a history entry.
 */
export function writeMissionHash(mission: string | null): void {
  try {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    if (mission) params.set('mission', mission)
    else params.delete('mission')
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
      // SCOPE restores DEGRADED, on purpose. The hash carries an identity, not
      // a world: `members` are projected from a payload (lib/scopeFromReport),
      // and refetching that payload here would make link-restore an async,
      // failable operation on the boot path. An empty member set filters
      // nothing (`scopeParams` pushes no param, `inScope` matches everything),
      // so the recipient lands on the right thing with nothing hidden, and the
      // Navigator re-projects the full world the moment its rows arrive.
      const s = parseScopeHash()
      if (s) {
        const cur = useScope.getState().scope
        if (!cur || cur.kind !== s.kind || cur.id !== s.id) {
          useScope.getState().setScope({
            kind: s.kind,
            id: s.id,
            label: s.id,
            members: emptyMembers(),
            origin: 'share-link',
          })
        }
      }
    }
    // 1. Restore on load.
    applyHash()
    // 2. Selection → hash.
    const unsub = useSelection.subscribe((s) =>
      writeHash(s.selection?.kind ?? null, s.selection?.id ?? null),
    )
    // 3. Scope → hash (the second addressable axis; `#sel` + `#scope` + `#ws`).
    const unsubScope = useScope.subscribe((s) => writeScopeHash(s.scope))
    // 4. Hash → selection/scope (manual edit / back-forward).
    window.addEventListener('hashchange', applyHash)
    return () => {
      unsub()
      unsubScope()
      window.removeEventListener('hashchange', applyHash)
    }
  }, [])
}
