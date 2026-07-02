/**
 * Status bar — footer with mode badge, panel count, last refresh, auth state.
 */

import type { Mode } from '@/types'
import { cn } from '@/lib/cn'
import { PreferencesControls } from '@/components/density/PreferencesControls'
import { useDebugMode } from '@/lib/debugMode'

export interface StatusBarProps {
  mode: Mode
  panelCount: number
  authenticated: boolean
  lastRefresh: Date | null
  errorText?: string | null
}

export function StatusBar({ mode, panelCount, authenticated, lastRefresh, errorText }: StatusBarProps) {
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
      </div>
      <div className="flex items-center gap-3">
        {errorText && <span className="text-accent-critical truncate max-w-md">{errorText}</span>}
        <span>{authenticated ? 'auth: ok' : 'auth: dev'}</span>
        <PreferencesControls />
      </div>
    </footer>
  )
}
