/**
 * The World — rails · Drill drawer.
 *
 * A right-side slide-in panel bound to `useWorldState.drawer`. The map writes a
 * cluster/point click into the drawer (`openDrawer`) and this renders its signals
 * + findings; clicking a row drives the shared selection store (Flow / Why
 * follow), and each row has a Pin to drop it onto the Casework board.
 */
import { X } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import { useWorldState } from './worldState'
import { SEVERITY_COLOR, type Severity } from './types'
import { useSelection, type Selection } from '@/state/selection'
import PinButton from '@/v4/components/PinButton'

export default function Drawer() {
  const drawer = useWorldState((s) => s.drawer)
  const closeDrawer = useWorldState((s) => s.closeDrawer)
  const select = useSelection((s) => s.select)

  if (!drawer.open) return null

  return (
    <aside
      className="absolute right-0 top-0 z-20 flex h-full w-[360px] flex-col border-l border-slate-800 bg-surface-200 shadow-2xl"
      data-testid="world-drawer"
    >
      <header className="flex shrink-0 items-center justify-between gap-2 border-b border-slate-800 px-3 py-2">
        <span className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-200">
          {drawer.title || 'Details'}
        </span>
        <button
          type="button"
          onClick={closeDrawer}
          className="shrink-0 rounded p-1 text-slate-400 hover:bg-surface-50/60 hover:text-slate-200"
          aria-label="Close drawer"
          data-testid="world-drawer-close"
        >
          <X size={16} />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <Section title="Signals" count={drawer.signals.length} empty="No signals in this selection.">
          {drawer.signals.map((sig) => (
            <Row
              key={sig.id}
              kind="signal"
              refId={sig.id}
              title={sig.title}
              severity={sig.severity}
              ts={sig.ts}
              onSelect={select}
            />
          ))}
        </Section>

        <Section title="Findings" count={drawer.findings.length} empty="No findings in this selection.">
          {drawer.findings.map((f) => (
            <Row
              key={f.id}
              kind="finding"
              refId={f.id}
              title={f.title}
              severity={f.severity}
              ts={f.ts}
              onSelect={select}
            />
          ))}
        </Section>
      </div>
    </aside>
  )
}

function Section({
  title,
  count,
  empty,
  children,
}: {
  title: string
  count: number
  empty: string
  children: React.ReactNode
}) {
  return (
    <section className="border-b border-slate-800/60">
      <div className="flex items-center justify-between px-3 pt-3 pb-1">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">
          {title}
        </span>
        <span className="text-[10px] tabular-nums text-slate-500">{count}</span>
      </div>
      {count === 0 ? (
        <div className="px-3 pb-3 text-xs text-slate-500">{empty}</div>
      ) : (
        <ul className="pb-2">{children}</ul>
      )}
    </section>
  )
}

function Row({
  kind,
  refId,
  title,
  severity,
  ts,
  onSelect,
}: {
  kind: 'signal' | 'finding'
  refId: string
  title: string
  severity: Severity
  ts: number
  onSelect: (sel: Selection | null) => void
}) {
  return (
    <li className="group flex items-center gap-1 px-3 py-1.5 hover:bg-surface-50/50">
      <button
        type="button"
        onClick={() => onSelect({ kind, id: refId, label: title })}
        className="flex min-w-0 flex-1 items-start gap-2 text-left"
        data-testid="world-drawer-row"
      >
        <span
          className="mt-1 h-2 w-2 shrink-0 rounded-full"
          style={{ backgroundColor: SEVERITY_COLOR[severity] }}
          title={severity}
        />
        <span className="min-w-0 flex-1 truncate text-xs text-slate-200">{title}</span>
        <span className="shrink-0 whitespace-nowrap text-[10px] text-slate-500">
          {formatDistanceToNow(ts, { addSuffix: true })}
        </span>
      </button>
      <PinButton kind={kind} refId={refId} label={title} compact />
    </li>
  )
}
