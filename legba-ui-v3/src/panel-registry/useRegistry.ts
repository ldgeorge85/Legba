/**
 * React hook that fetches `PanelRegistration` rows from the backend and
 * keeps the in-memory map live via NATS events.
 *
 * The hook is the L-108 §8 step [4] "frontend's reactive registry subscriber"
 * for L-204 — it owns the instance map keyed on
 * `(panel_kind, scope_value)` so other parts of the shell read from it
 * without re-fetching.
 *
 * Subjects subscribed to:
 *   - `registry.bindings.activated.*` → add/refresh
 *   - `registry.bindings.retired.*`   → mark retired
 *   - `registry.targets.activated.*`  → defensive refetch trigger
 *   - `registry.targets.retired.*`    → defensive refetch trigger
 *
 * On any registry-shaping event the hook does a focused refetch rather
 * than splicing — the SQL surface is the source of truth, the WS feed
 * is just a "something changed" nudge.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Mode, PanelRegistration } from '@/types'
import { fetchUiPanels } from '@/lib/api'
import { subscribeRegistryEvents } from '@/lib/ws'

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

  // Fetch on mount + on every refresh tick.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchUiPanels(mode)
      .then((rows) => {
        if (cancelled) return
        setRegistrations(rows)
        setError(null)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err as Error)
        setRegistrations([])
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
