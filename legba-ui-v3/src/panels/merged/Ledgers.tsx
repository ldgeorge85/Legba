/**
 * Ledgers (`system.ledgers`) — UI-B1/merges, UI-D1 Table A "Governor · Audit ·
 * Budget".
 *
 * The append-only ledgers, read one instead of the other:
 *   - Governor  → the per-pack governor's allow/block stream (`system.governor`)
 *   - Audit     → the signed descriptor audit chain (`system.audit`)
 *   - Budget    → per-analyst tokens / runs / cost and the envelope (`system.budget`)
 *
 * The forecast ledger and its scorecard USED to be two more tabs here. Since
 * 2026-11-13 they have one home, `panels/ledger/ForecastLedger` (brief §6
 * rule 1: one home per object), mounted on the reader side; this wrapper
 * carries a one-line pointer to it instead of a second copy. A saved layout or
 * link that still asks for the old `forecasts` / `scorecard` tab falls back to
 * Governor (`useMergedTab` ignores an unknown tab).
 *
 * Absence contract (Table A): actors print as `bearer:<12 hex>` /
 * `consult_write:<id>`, never a token — which the Audit and Governor children
 * already do; this wrapper adds no field of its own. The three retired ids
 * resolve here through `panel-registry/aliases.ts`, each onto its own tab, and
 * every child renders UNMODIFIED inside `PanelEmbedProvider`.
 */
import type { PanelProps } from '@/types'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import { drive } from '@/lib/applyAction'
import { goToSurface } from '@/lib/commands'
import GovernorEventsPanel from '@/panels/system/GovernorEvents'
import AuditChainPanel from '@/panels/system/AuditChain'
import BudgetPanel from '@/panels/system/Budget'
import { useMergedTab } from './useMergedTab'

type Tab = 'governor' | 'audit' | 'budget'

export const LEDGERS_TABS: readonly PanelTabDef[] = [
  { id: 'governor', label: 'Governor' },
  { id: 'audit', label: 'Audit' },
  { id: 'budget', label: 'Budget' },
]

/** Where forecasts live now — the reader surface whose first region mounts `ForecastLedger`. */
export const FORECASTS_HOME_SURFACE = 'ledger'
export const FORECASTS_HOME_LABEL = 'Ledger → Forecasts'

export default function LedgersMerged(props: PanelProps) {
  const [tab, setTab] = useMergedTab<Tab>('system.ledgers', props.initialTab, LEDGERS_TABS, 'governor')
  return (
    <div className="flex h-full w-full flex-col bg-surf-2">
      <div className="flex items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">Ledgers</span>
        <PanelTabStrip
          tabs={LEDGERS_TABS}
          active={tab}
          onChange={(id) => setTab(id as Tab)}
          ariaLabel="Engine ledgers"
          testIdPrefix="ledgers-tab"
        />
        <button
          type="button"
          className="ml-auto text-xs text-ink-3 underline hover:text-ink-1"
          onClick={() => drive([goToSurface(FORECASTS_HOME_SURFACE)])}
          data-testid="ledgers-forecasts-pointer"
        >
          Forecasts and their scorecard: {FORECASTS_HOME_LABEL}
        </button>
      </div>
      <div className="min-h-0 flex-1">
        <PanelEmbedProvider>
          {tab === 'governor' && <GovernorEventsPanel {...props} />}
          {tab === 'audit' && <AuditChainPanel {...props} />}
          {tab === 'budget' && <BudgetPanel {...props} />}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}
