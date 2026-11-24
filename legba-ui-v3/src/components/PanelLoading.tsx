/**
 * PanelLoading — the one "Loading…" a view shows while its chunk arrives.
 * Its own module (M-2) so a view registered outside the dock (the Watch
 * views, the phone shell) can use it without importing the dock's
 * `SingletonView` (and, through it, the Dockview panel-api context).
 */
export function PanelLoading() {
  return <div className="flex h-full w-full items-center justify-center text-label text-ink-3">Loading…</div>
}
