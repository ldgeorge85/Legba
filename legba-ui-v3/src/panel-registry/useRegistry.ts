/**
 * React hook that fetches `PanelRegistration` rows from the backend and
 * keeps the in-memory map live via NATS events.
 *
 * The hook is the L-108 §8 step [4] "frontend's reactive registry subscriber"
 * for L-204 — it owns the instance map keyed on
 * `(panel_kind, scope_value)` so other parts of the shell read from it
 * without re-fetching.
 *
 * Bound-panel reachability (P0-2f): the live `ui_panel_registrations` surface
 * is empty (no descriptor declares `outputs.ui_panel`), so alongside the real
 * rows the hook fetches the target/analyst descriptor heads and SYNTHESIZES
 * the per-record bound-panel registrations (see synthesize.ts). Real rows stay
 * authoritative — a synthetic row is dropped when a live registration already
 * covers the same panel instance. Descriptor fetch failures degrade softly to
 * the real rows alone.
 *
 * Subjects subscribed to:
 *   - `registry.bindings.activated.*` → add/refresh
 *   - `registry.bindings.retired.*`   → mark retired
 *   - `registry.targets.activated.*`  → defensive refetch trigger
 *   - `registry.targets.retired.*`    → defensive refetch trigger
 *
 * On any registry-shaping event the hook does a focused refetch rather
 * than splicing — the SQL surface is the source of truth, the WS feed
 * is just a "something changed" nudge. Target/analyst lifecycle events
 * re-run the descriptor fetch too, so the synthesized groups track the
 * registry.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Mode, PanelRegistration } from '@/types'
import { apiGet, fetchUiPanels } from '@/lib/api'
import { subscribeRegistryEvents } from '@/lib/ws'
import {
  mergeRegistrations,
  synthesizeBoundRegistrations,
  type RecordDescriptor,
} from './synthesize'

/** Soft descriptor-head fetch — empty on any failure so the sidebar always
 *  degrades to the real registration rows rather than erroring out. */
async function softDescriptorHeads(family: 'target' | 'analyst'): Promise<RecordDescriptor[]> {
  try {
    return await apiGet<RecordDescriptor[]>(
      `/registry/descriptors?family=${family}&head_only=true&limit=500`,
    )
  } catch {
    return []
  }
}

export interface RegistryState {
  registrations: PanelRegistration[]
  isLoading: boolean
  isError: boolean
  error: Error | null
  refresh: () => void
}

export function useRegistry(mode: Mode): RegistryState {
  const [registrations, setRegistrations] = useState<PanelRegistration[]>([])
  const [isLoading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [tick, setTick] = useState(0)

  const refresh = useCallback(() => setTick((t) => t + 1), [])

  // Fetch on mount + on every refresh tick. The real ui_panels rows and the
  // descriptor heads load in parallel; a ui_panels failure still surfaces as
  // the hook error while the synthesized rows keep bound panels reachable.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all([
      fetchUiPanels(mode).then(
        (rows) => ({ rows, err: null as Error | null }),
        (err) => ({ rows: [] as PanelRegistration[], err: err as Error }),
      ),
      softDescriptorHeads('target'),
      softDescriptorHeads('analyst'),
    ])
      .then(([real, targets, analysts]) => {
        if (cancelled) return
        const synthetic = synthesizeBoundRegistrations(targets, analysts, mode)
        setRegistrations(mergeRegistrations(real.rows, synthetic))
        setError(real.err)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [mode, tick])

  // Subscribe to NATS events for the whole registry namespace.
  useEffect(() => {
    const sub = subscribeRegistryEvents('registry.>', (ev) => {
      if (ev.type !== 'event' || !ev.subject) return
      // Defensive — any descriptor/binding shape change schedules a refetch.
      if (
        ev.subject.startsWith('registry.bindings.') ||
        ev.subject.startsWith('registry.targets.') ||
        ev.subject.startsWith('registry.analysts.')
      ) {
        refresh()
      }
    })
    return () => sub.close()
  }, [refresh])

  return useMemo(
    () => ({
      registrations,
      isLoading,
      isError: !!error,
      error,
      refresh,
    }),
    [registrations, isLoading, error, refresh],
  )
}
