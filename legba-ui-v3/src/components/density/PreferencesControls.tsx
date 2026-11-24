/**
 * PreferencesControls — the density switcher (redesign Move 5).
 *
 * A compact control surfaced in the health line: a three-way density
 * segmented control (tight / cozy / comfortable), token-driven via
 * PreferencesProvider. It is developer chrome, so it shows only under debug
 * chrome (Ctrl/Cmd+Shift+D). The theme toggle that sat beside it is gone with
 * the light theme (U-2: one dark theme).
 */
import { usePreferences, type Density } from './PreferencesProvider'
import { cn } from '@/lib/cn'
import { useDebugMode } from '@/lib/debugMode'

const DENSITY_OPTIONS: ReadonlyArray<{ value: Density; label: string; abbr: string }> = [
  { value: 'tight', label: 'Tight density', abbr: 'T' },
  { value: 'cozy', label: 'Cozy density', abbr: 'C' },
  { value: 'comfortable', label: 'Comfortable density', abbr: 'Cf' },
]

export function PreferencesControls() {
  const { density, setDensity } = usePreferences()
  const debug = useDebugMode()
  if (!debug) return null
  return (
    <div
      className="inline-flex overflow-hidden rounded-control border border-line"
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
  )
}
