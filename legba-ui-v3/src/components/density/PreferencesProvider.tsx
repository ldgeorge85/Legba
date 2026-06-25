/**
 * PreferencesProvider — the UI-preferences root (redesign Move 5).
 *
 * Owns the two token-driving UI axes and persists them to localStorage:
 *
 *   1. DENSITY — 'tight' | 'cozy' | 'comfortable'. `comfortable` is the
 *      daily-driver default (≥30% whitespace target); `cozy` is acceptable for
 *      ops/runtime panels. The mode sets a `.density-*` class on the app root,
 *      which flips the `--density-*` CSS vars (see globals.css), so every
 *      token-adopting surface retunes at once — no hand-editing 58 panels.
 *
 *   2. THEME — 'dark' | 'light'. DARK STAYS THE DEFAULT and the design target
 *      (fixed constraint); light is an accessibility affordance. Theme drives
 *      Tailwind `darkMode:'class'`: `dark` adds `.dark` to <html>, `light`
 *      adds `.theme-light` (and removes `.dark`), flipping the `--surf/--ink`
 *      token vars.
 *
 * Both default to the daily-driver values on first run and survive reloads.
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
export type Theme = 'dark' | 'light'

const DENSITY_KEY = 'legba_density'
const THEME_KEY = 'legba_theme'

const DENSITIES: readonly Density[] = ['tight', 'cozy', 'comfortable']

interface Preferences {
  density: Density
  setDensity: (d: Density) => void
  theme: Theme
  setTheme: (t: Theme) => void
  toggleTheme: () => void
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

function readTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY)
    if (v === 'light' || v === 'dark') return v
  } catch {
    // ignore
  }
  return 'dark'
}

/**
 * Reflect theme onto <html> so Tailwind `darkMode:'class'` + the CSS-var token
 * sets in globals.css resolve. Dark = `.dark`; light = `.theme-light`.
 */
function applyTheme(theme: Theme) {
  const root = document.documentElement
  if (theme === 'light') {
    root.classList.remove('dark')
    root.classList.add('theme-light')
  } else {
    root.classList.add('dark')
    root.classList.remove('theme-light')
  }
}

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const [density, setDensityState] = useState<Density>(() => readDensity())
  const [theme, setThemeState] = useState<Theme>(() => readTheme())

  // Theme is a document-level concern (token vars + Dockview chrome live on
  // <html>), so reflect it there rather than wrapping the tree.
  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  const setDensity = useCallback((d: Density) => {
    setDensityState(d)
    try {
      localStorage.setItem(DENSITY_KEY, d)
    } catch {
      // best-effort persistence
    }
  }, [])

  const setTheme = useCallback((t: Theme) => {
    setThemeState(t)
    try {
      localStorage.setItem(THEME_KEY, t)
    } catch {
      // best-effort persistence
    }
  }, [])

  const toggleTheme = useCallback(() => {
    setTheme(theme === 'dark' ? 'light' : 'dark')
  }, [theme, setTheme])

  const value = useMemo<Preferences>(
    () => ({ density, setDensity, theme, setTheme, toggleTheme }),
    [density, setDensity, theme, setTheme, toggleTheme],
  )

  // The density class wraps the app subtree (not <html>) so density is a
  // workspace concern; theme is on <html> (above). A `.dark`/`.theme-light`
  // marker also lives here so descendants that read `.dark`-scoped utilities
  // resolve even inside portals mounted under this node.
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
