/**
 * PanelTabStrip — the shared tab/mode switcher for U-3's merged panels.
 *
 * Five panel-kind pairs/trios were folded into one sidebar row each (Timeline,
 * Provenance, Alerts & Watches, Consult's depth toggle, Entities' Graph +
 * Structure tabs) per COHERENCE_WAVES_PLAN_2026-07-28 §U-3. Every merge wraps
 * its FULL, UNMODIFIED original panel components (no internals rewritten —
 * see `src/panels/merged/*`) behind one small strip like this one, so there is
 * exactly one tab-bar implementation instead of five bespoke button rows.
 *
 * Since UI-B1/tokens the strip renders the `Tabs` primitive (`src/ui/tabs.tsx`,
 * Radix underneath): the API and the test ids are unchanged, and the strip
 * becomes a real roving-focus `tablist` — ONE Tab stop, ←/→ (and Home/End)
 * move between tabs and activate them. The panel body stays the caller's
 * (each merge renders its own tab's original component), so the triggers
 * carry no `aria-controls` pointing at a tabpanel that is not there.
 */
import { Tabs, TabsList, TabsTrigger } from '@/ui/tabs'

/**
 * Pick a tabbed panel's opening tab.
 *
 * `requested` comes from `PanelProps.initialTab` — set when a RETIRED kind
 * resolved onto this panel through `panel-registry/aliases.ts` (opening
 * `system.lineage` must land on Provenance's Lineage tab), or when a workspace
 * seed asks for a specific tab. An unrecognized value falls back to the
 * panel's own default: a stale tab name in an old saved layout must never
 * render an empty surface.
 */
export function initialTab(
  requested: string | undefined,
  tabs: readonly PanelTabDef[],
  fallback: string,
): string {
  if (requested && tabs.some((t) => t.id === requested)) return requested
  return fallback
}

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
    <Tabs value={active} onValueChange={onChange} activationMode="automatic">
      <TabsList aria-label={ariaLabel}>
        {tabs.map((t) => (
          <TabsTrigger
            key={t.id}
            value={t.id}
            aria-controls={undefined}
            data-testid={`${testIdPrefix}-${t.id}`}
          >
            <span>{t.label}</span>
            {t.badge && (
              <span className="rounded-sm bg-amber-500/20 px-1 text-label uppercase tracking-wide text-amber-400">
                {t.badge}
              </span>
            )}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  )
}
