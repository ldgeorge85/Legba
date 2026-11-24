/**
 * panelParams — what an `open_panel` action hands the tile it opens (UI-B1/shell).
 *
 * The `drive` executor (`lib/applyAction.ts`) is the ONE way a surface opens
 * another surface. A singleton drill that needs more than its kind to render —
 * Cross-framing needs the claim, the prompt-module diff needs the candidate —
 * receives that through `open_panel.params`, and the executor writes those
 * params HERE before it opens (or focuses) the tile.
 *
 * This store replaces the `window` parks and `legba:open-*` DOM events the
 * bridges used (UI-D1 §3: "a surface reachable only by an event is
 * unaddressable"). It keeps the two properties the parks existed for:
 *
 *   * **first open works** — the params are written before the tile mounts, and
 *     the tile reads them on mount;
 *   * **a remount never replays a stale request** — every write carries a
 *     nonce, and a tile marks the nonce it consumed; a remounted tile sees an
 *     already-consumed nonce and does nothing.
 *
 * An already-mounted tile follows a new request because it subscribes.
 */
import { useEffect, useRef } from 'react'
import { create } from 'zustand'

export interface PanelOpenRequest {
  /** The params the `open_panel` action carried (minus `tab` / `position`). */
  params: Readonly<Record<string, unknown>>
  /** Monotonic per write — a new request is a new nonce. */
  nonce: number
}

interface PanelParamsState {
  byKind: Record<string, PanelOpenRequest>
  /** The newest nonce a mounted tile has applied, per kind. */
  consumed: Record<string, number>
}

let nextNonce = 1

export const usePanelParamsStore = create<PanelParamsState>(() => ({ byKind: {}, consumed: {} }))

/** Record what an `open_panel` asked the tile of `kind` to show. */
export function setPanelParams(kind: string, params: Record<string, unknown>): void {
  const nonce = nextNonce++
  usePanelParamsStore.setState((s) => ({
    byKind: { ...s.byKind, [kind]: { params: { ...params }, nonce } },
  }))
}

/** The newest request for `kind` that no tile has consumed yet, or null. */
export function pendingPanelParams(kind: string): PanelOpenRequest | null {
  const s = usePanelParamsStore.getState()
  const req = s.byKind[kind]
  if (!req) return null
  return (s.consumed[kind] ?? 0) >= req.nonce ? null : req
}

/** Mark a request applied, so a remount does not replay it. */
export function consumePanelParams(kind: string, nonce: number): void {
  usePanelParamsStore.setState((s) =>
    (s.consumed[kind] ?? 0) >= nonce ? s : { consumed: { ...s.consumed, [kind]: nonce } },
  )
}

/**
 * Subscribe a tile to the open requests for its kind. `onRequest` runs once for
 * an unconsumed request present at mount, and once per new request after.
 */
export function usePanelOpenParams(
  kind: string,
  onRequest: (params: Readonly<Record<string, unknown>>) => void,
): void {
  const cb = useRef(onRequest)
  cb.current = onRequest
  useEffect(() => {
    const apply = () => {
      const req = pendingPanelParams(kind)
      if (!req) return
      consumePanelParams(kind, req.nonce)
      cb.current(req.params)
    }
    apply()
    return usePanelParamsStore.subscribe((s, prev) => {
      if (s.byKind[kind] !== prev.byKind[kind]) apply()
    })
  }, [kind])
}

/** Test/teardown helper. */
export function resetPanelParams(): void {
  usePanelParamsStore.setState({ byKind: {}, consumed: {} })
}

// ---------------------------------------------------------------------------
// Tab requests — an already-open tabbed survivor follows an alias's tab.
// ---------------------------------------------------------------------------

/**
 * The key a tabbed survivor's tab requests ride (UI-B1/merges). A retired id
 * opened while its survivor is ALREADY on the wall must still land on its tab
 * (`system.dead_letter` → System Status' Failures tab): a fresh tile reads the
 * tab from its Dockview params at mount, but an open tile is only re-focused,
 * so the shell's singleton opener posts the tab here and the merged wrapper
 * (`panels/merged/useMergedTab.ts`) follows it. Same nonce semantics as the
 * params above: a remount never replays a consumed request.
 */
export function tabRequestKey(kind: string): string {
  return `${kind}#tab`
}

/** Ask the mounted tile of `kind` to switch to `tab`. */
export function requestPanelTab(kind: string, tab: string): void {
  setPanelParams(tabRequestKey(kind), { tab })
}
