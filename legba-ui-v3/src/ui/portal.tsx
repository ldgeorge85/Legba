/**
 * The portal container — where every overlay primitive in `src/ui/` mounts.
 *
 * Radix portals default to `document.body`. The workstation does not want
 * that, for two measured reasons:
 *
 *   1. The density tokens (`--density-*`, `--row-h`) live on a wrapper INSIDE
 *      the React root (`PreferencesProvider` sets `.density-*` on a
 *      `display: contents` div), not on <html>. An overlay mounted on <body>
 *      would read the stylesheet defaults instead of the operator's density.
 *   2. Dockview tiles sit under `transform: translate3d(...)` ancestors (the
 *      dock's GPU layers), which turn `position: fixed` into "fixed to that
 *      ancestor". An overlay mounted INSIDE a tile would be clipped and offset;
 *      one mounted beside the dock is positioned against the viewport.
 *
 * So the shell renders ONE explicit host element beside the Dockview workspace
 * (`<PortalHost>`), and every primitive's Portal reads it through
 * {@link usePortalContainer}. Outside a host (a unit test that renders a lone
 * panel) the container is `undefined` and Radix falls back to `document.body`,
 * which keeps every existing panel test working unchanged.
 */
import { createContext, useContext, useState, type ReactNode } from 'react'

const PortalContainerContext = createContext<HTMLElement | null>(null)

/** Provide an explicit portal container to every primitive below. */
export function PortalContainerProvider({
  container,
  children,
}: {
  container: HTMLElement | null
  children: ReactNode
}) {
  return (
    <PortalContainerContext.Provider value={container}>{children}</PortalContainerContext.Provider>
  )
}

/** The container the nearest host provides, or `undefined` (Radix → body). */
export function usePortalContainer(): HTMLElement | undefined {
  return useContext(PortalContainerContext) ?? undefined
}

/**
 * Render `children` with a dedicated overlay host element as their portal
 * container. The host is a sibling AFTER the children, so overlays paint above
 * the dock in document order without a z-index war, and it carries no layout
 * (`display: contents` is not used — a fixed-position child of a zero-size
 * static box is positioned against the viewport, which is what overlays want).
 */
export function PortalHost({ children, testId = 'ui-portal-host' }: { children: ReactNode; testId?: string }) {
  const [host, setHost] = useState<HTMLElement | null>(null)
  return (
    <PortalContainerProvider container={host}>
      {children}
      <div ref={setHost} data-ui-portal-host="" data-testid={testId} className="legba-portal-host" />
    </PortalContainerProvider>
  )
}
