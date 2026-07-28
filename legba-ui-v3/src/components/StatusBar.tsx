/**
 * Status bar — footer with mode badge, panel count, last refresh, auth state,
 * and (A10) the export-basket chip: a count of collected items that opens the
 * Report Export panel. The chip only renders when the basket is non-empty.
 */

import { FileDown } from 'lucide-react'
import type { Mode } from '@/types'
import { cn } from '@/lib/cn'
import { PreferencesControls } from '@/components/density/PreferencesControls'
import { useDebugMode } from '@/lib/debugMode'
import { useExportBasket } from '@/state/exportBasket'

export interface StatusBarProps {
  mode: Mode
  panelCount: number
  authenticated: boolean
  lastRefresh: Date | null
  errorText?: string | null
  /** Open the Report Export panel (the basket chip's click target). */
  onOpenExport?: () => void
}

/** A10 — the persistent basket count; hidden at zero so the chrome stays lean. */
function ExportBasketChip({ onOpen }: { onOpen?: () => void }) {
  const count = useExportBasket((s) => s.items.length)
  if (count === 0) return null
  return (
    <button
      type="button"
      onClick={onOpen}
      className="inline-flex items-center gap-1 rounded border border-line px-1.5 py-px text-ink-2 hover:text-ink-1"
      title="open Report Export (collection basket)"
      data-testid="statusbar-export-chip"
    >
      <FileDown className="h-3 w-3" aria-hidden />
      export: {count}
    </button>
  )
}

export function StatusBar({ mode, panelCount, authenticated, lastRefresh, errorText, onOpenExport }: StatusBarProps) {
  // The mode badge + registered-panel counter are developer plumbing; keep only
  // the refresh time by default and reveal the rest under debug chrome (item 5).
  const debug = useDebugMode()
  return (
    <footer className="h-7 flex items-center justify-between px-3 text-label bg-surf-base border-t border-line text-ink-2">
      <div className="flex items-center gap-3">
        {debug && (
          <span
            className={cn(
              'px-1.5 py-px rounded text-label uppercase tracking-wider',
              mode === 'personal' && 'bg-accent-info/20 text-accent-info',
              mode === 'above_ai' && 'bg-accent-warning/20 text-accent-warning',
              mode === 'cis' && 'bg-accent-ok/20 text-accent-ok',
            )}
          >
            mode: {mode}
          </span>
        )}
        {debug && (
          <span>
            {panelCount} panel{panelCount === 1 ? '' : 's'} registered
          </span>
        )}
        {lastRefresh && <span>refreshed {lastRefresh.toLocaleTimeString()}</span>}
        <ExportBasketChip onOpen={onOpenExport} />
      </div>
      <div className="flex items-center gap-3">
        {errorText && <span className="text-accent-critical truncate max-w-md">{errorText}</span>}
        <span>{authenticated ? 'auth: ok' : 'auth: dev'}</span>
        <PreferencesControls />
      </div>
    </footer>
  )
}
