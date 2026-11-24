/**
 * PreferencesProvider — the UI-preferences root (redesign Move 5).
 *
 * Owns ONE token-driving UI axis and persists it to localStorage:
 *
 *   DENSITY — 'tight' | 'cozy' | 'comfortable'. `comfortable` is the
 *   daily-driver default (≥30% whitespace target); `cozy` is acceptable for
 *   ops/runtime panels. The mode sets a `.density-*` class on the app root,
 *   which flips the `--density-*` CSS vars (see globals.css), so every
 *   token-adopting surface retunes at once.
 *
 * THERE IS NO THEME AXIS (U-2 shell; the owner: "just dark. just dark."). The
 * console has one dark theme; the light theme, its toggle and the stored
 * `legba_theme` preference are gone. `<html>` always carries `.dark` so the
 * Tailwind `darkMode:'class'` variants keep resolving; a browser that stored
 * "light" under the old key simply lands on the one theme.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

export type Density = 'tight' | 'cozy' | 'comfortable'

const DENSITY_KEY = 'legba_density'

const DENSITIES: readonly Density[] = ['tight', 'cozy', 'comfortable']

interface Preferences {
  density: Density
  setDensity: (d: Density) => void
}

const PreferencesContext = createContext<Preferences | null>(null)

function readDensity(): Density {
  try {
    const v = localStorage.getItem(DENSITY_KEY)
    if (v && (DENSITIES as readonly string[]).includes(v)) return v as Density
  } catch {
    // localStorage unavailable — fall through to the default.
  }
  return 'comfortable'
}

/** The one theme: `.dark` on <html>, and no `.theme-light` left from an old session. */
function applyDarkTheme() {
  const root = document.documentElement
  root.classList.add('dark')
  root.classList.remove('theme-light')
}

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const [density, setDensityState] = useState<Density>(() => readDensity())

  useEffect(() => {
    applyDarkTheme()
  }, [])

  const setDensity = useCallback((d: Density) => {
    setDensityState(d)
    try {
      localStorage.setItem(DENSITY_KEY, d)
    } catch {
      // best-effort persistence
    }
  }, [])

  const value = useMemo<Preferences>(() => ({ density, setDensity }), [density, setDensity])

  // The density class wraps the app subtree (not <html>) so density is a
  // workspace concern.
  return (
    <PreferencesContext.Provider value={value}>
      <div className={`density-${density} contents`}>{children}</div>
    </PreferencesContext.Provider>
  )
}

export function usePreferences(): Preferences {
  const ctx = useContext(PreferencesContext)
  if (!ctx) {
    throw new Error('usePreferences must be used within a PreferencesProvider')
  }
  return ctx
}

/** Convenience: just the density mode. */
export function useDensity(): Density {
  return usePreferences().density
}
