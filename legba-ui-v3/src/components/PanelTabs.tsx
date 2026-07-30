/**
 * PanelTabStrip — the shared tab/mode switcher for U-3's merged panels.
 *
 * Five panel-kind pairs/trios were folded into one sidebar row each (Timeline,
 * Provenance, Alerts & Watches, Consult's depth toggle, Entities' Graph +
 * Structure tabs) per COHERENCE_WAVES_PLAN_2026-07-28 §U-3. Every merge wraps
 * its FULL, UNMODIFIED original panel components (no internals rewritten —
 * see `src/panels/merged/*`) behind one small strip like this one, so there is
 * exactly one tab-bar implementation instead of five bespoke button rows.
 */
import { cn } from '@/lib/cn'

export interface PanelTabDef {
  id: string
  label: string
  /** Optional small pill next to the label (e.g. the preview-tier tag on
   *  Alerts & Watches' "Triggers" tab). */
  badge?: string
}

export function PanelTabStrip({
  tabs,
  active,
  onChange,
  ariaLabel,
  testIdPrefix,
}: {
  tabs: readonly PanelTabDef[]
  active: string
  onChange: (id: string) => void
  ariaLabel: string
  testIdPrefix: string
}) {
  return (
    <div role="tablist" aria-label={ariaLabel} className="flex items-center gap-1">
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={active === t.id}
          data-testid={`${testIdPrefix}-${t.id}`}
          onClick={() => onChange(t.id)}
          className={cn(
            'flex items-center gap-1 rounded border px-2 py-0.5 text-label font-medium',
            active === t.id
              ? 'border-line-strong bg-surf-3 text-ink-1'
              : 'border-line text-ink-3 hover:bg-surf-2 hover:text-ink-1',
          )}
        >
          <span>{t.label}</span>
          {t.badge && (
            <span className="rounded-sm bg-amber-500/20 px-1 text-[9px] uppercase tracking-wide text-amber-400">
              {t.badge}
            </span>
          )}
        </button>
      ))}
    </div>
  )
}
