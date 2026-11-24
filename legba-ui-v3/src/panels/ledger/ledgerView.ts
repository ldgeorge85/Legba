/**
 * The Ledger page's on-screen state (U-11): which forecast is the reader's
 * point, which forecasts the list shows, which evaluation run is open, and
 * which proposal the admin queue has open. One small store, so a click, a
 * Consult `panel_action` (`show_forecast`, `filter`, `show_run`) and the
 * on-screen report (`lib/onScreen.ts`, the `ledger` field) all read and write
 * the same thing.
 */
import { create } from 'zustand'

export type ForecastFilter = 'open' | 'resolved' | 'all'
export const FORECAST_FILTERS: readonly ForecastFilter[] = ['open', 'resolved', 'all']

interface LedgerViewState {
  /** The selected forecast's slug (the reader's point on this page). */
  forecast: string | null
  filter: ForecastFilter
  /** The open evaluation run's id. */
  run: string | null
  /** The admin queue's open question id. */
  proposal: string | null
  set: (patch: Partial<Omit<LedgerViewState, 'set'>>) => void
}

export const useLedgerView = create<LedgerViewState>((set) => ({
  forecast: null,
  filter: 'all',
  run: null,
  proposal: null,
  set: (patch) => set(patch),
}))

export function resetLedgerView(): void {
  useLedgerView.setState({ forecast: null, filter: 'all', run: null, proposal: null })
}

/** The `ledger` field of the on-screen report: ids and words only, a few bytes. */
export function ledgerOnScreen(): { forecast: string | null; filter: ForecastFilter; run: string | null } {
  const s = useLedgerView.getState()
  return { forecast: s.forecast, filter: s.filter, run: s.run }
}
