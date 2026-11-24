/**
 * `src/ui/` — the workstation's copied-in primitives (UI-D1 §7, D9).
 *
 * Exactly five, each a shadcn/ui component copied in as source on its Radix
 * package and hand-ported onto the tokens: Dialog, Popover, Tooltip, Tabs,
 * Select. The set is held at five on purpose — the shell bundle pays for each,
 * and the mobile bundle would pay twice — so a sixth is a design decision, not
 * a convenience. Every overlay portals into the shell's explicit host
 * (`portal.tsx`). The Dockview theme object (`dockviewTheme.ts`) lives here
 * too: it is the dock's half of the same token set.
 */
export * from './dialog'
export * from './popover'
export * from './tooltip'
export * from './tabs'
export * from './select'
export * from './portal'
export { legbaDark, DOCK_TAB_STRIP_PX } from './dockviewTheme'
