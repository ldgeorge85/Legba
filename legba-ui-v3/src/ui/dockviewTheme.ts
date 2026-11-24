/**
 * The Dockview theme OBJECT (UI_HOLISTIC_DESIGN_2026-08-24 §5.5).
 *
 * Replaces the className hack (`className="dockview-theme-abyss"` plus three
 * `--dv-*` overrides, plus a second hand-maintained block of eight under
 * `.theme-light`). Dockview 8 takes a first-class theme: the object carries
 * the dock's BEHAVIOUR (gap, tab-drop indicator, edge-group collapsed size),
 * and its `className` points at ONE stylesheet block in `globals.css`
 * (`.dockview-theme-legba`) that maps every supported `--dv-*` variable onto
 * the workstation tokens (`--surf-*` / `--ink-*` / `--line-*` / `--accent*`),
 * so there is no second dock palette to drift. There is one theme, dark
 * (U-2 shell); `colorScheme` is informational in Dockview 8.
 */
import type { DockviewTheme } from 'dockview-react'

/** The tab-strip height in px — §5.4 change 3 ("reclaim the tab strip"). */
export const DOCK_TAB_STRIP_PX = 28

export const legbaDark: DockviewTheme = {
  name: 'legba-dark',
  className: 'dockview-theme-legba',
  colorScheme: 'dark',
  // §5.4 change 5: flush tiles, one hairline (the separator reads --line-1).
  gap: 1,
  tabAnimation: 'default',
  // A hairline insertion strip, not a fill.
  dndTabIndicator: 'line',
  // Matches the tab strip, so a collapsed edge group lines up with the tabs.
  edgeGroupCollapsedSize: DOCK_TAB_STRIP_PX,
  // Tab groups render as a flat bar (Abyss's choice, kept).
  tabGroupIndicator: 'none',
}
