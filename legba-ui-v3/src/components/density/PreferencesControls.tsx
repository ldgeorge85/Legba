/**
 * PreferencesControls — the density + theme switcher (redesign Move 5).
 *
 * A compact control surfaced in the StatusBar: a three-way density segmented
 * control (tight / cozy / comfortable) and a dark↔light theme toggle. Both are
 * token-driven via PreferencesProvider; this is just the affordance.
 */
import { Moon, Sun } from 'lucide-react'
import { usePreferences, type Density } from './PreferencesProvider'
import { cn } from '@/lib/cn'
import { useDebugMode } from '@/lib/debugMode'

const DENSITY_OPTIONS: ReadonlyArray<{ value: Density; label: string; abbr: string }> = [
  { value: 'tight', label: 'Tight density', abbr: 'T' },
  { value: 'cozy', label: 'Cozy density', abbr: 'C' },
  { value: 'comfortable', label: 'Comfortable density', abbr: 'Cf' },
]

export function PreferencesControls() {
  const { density, setDensity, theme, toggleTheme } = usePreferences()
  // The cryptic T/C/Cf density segmented control is developer chrome — keep only
  // the theme toggle by default; reveal density under debug chrome (item 5).
  const debug = useDebugMode()
  return (
    <div className="flex items-center gap-2">
      {debug && (
      <div
        className="inline-flex overflow-hidden rounded border border-line"
        role="group"
        aria-label="Density"
        data-testid="density-control"
      >
        {DENSITY_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            type="button"
            aria-pressed={density === opt.value}
            title={opt.label}
            onClick={() => setDensity(opt.value)}
            data-testid={`density-${opt.value}`}
            className={cn(
              'px-1.5 py-px text-label leading-none',
              density === opt.value ? 'bg-surf-3 text-ink-1' : 'text-ink-3 hover:text-ink-1',
            )}
          >
            {opt.abbr}
          </button>
        ))}
      </div>
      )}
      <button
        type="button"
        onClick={toggleTheme}
        title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
        aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
        data-testid="theme-toggle"
        className="inline-flex items-center gap-1 rounded border border-line px-1.5 py-px text-ink-2 hover:text-ink-1"
      >
        {theme === 'dark' ? (
          <Sun className="h-3 w-3" aria-hidden />
        ) : (
          <Moon className="h-3 w-3" aria-hidden />
        )}
        <span className="text-label leading-none">{theme === 'dark' ? 'light' : 'dark'}</span>
      </button>
    </div>
  )
}
