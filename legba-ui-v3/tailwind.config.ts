import type { Config } from 'tailwindcss'

const config: Config = {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Surface palette per Personal-mode dark default (M-036 Q5 ratified).
        // CSS-var-driven (Wave 3 follow-up): these now read the same `--surf-*`
        // vars as the `surf.*` aliases below so the ~76 not-yet-migrated panels
        // that still use `bg-surface-300`/`bg-surface-100` ALSO flip with the
        // light toggle instead of staying hardcoded-dark (light text on a dark
        // box = unreadable). Mapping mirrors the historical literals:
        //   surface-300 #0a0c10 → --surf-base   surface-200 #0f1115 → --surf-1
        //   surface-100 #15171c → --surf-2       surface-50  #1a1d23 → --surf-3
        surface: {
          50: 'var(--surf-3)',
          100: 'var(--surf-2)',
          200: 'var(--surf-1)',
          300: 'var(--surf-base)',
        },
        // Token-aliased semantic colors (Wave 3, redesign Move 5) — these read
        // the CSS vars in globals.css so they flip with the light toggle. Use
        // `text-ink-2`, `bg-surf-2`, `border-line` on token-adopting surfaces.
        surf: {
          base: 'var(--surf-base)',
          1: 'var(--surf-1)',
          2: 'var(--surf-2)',
          3: 'var(--surf-3)',
        },
        ink: {
          1: 'var(--ink-1)',
          2: 'var(--ink-2)',
          3: 'var(--ink-3)',
        },
        line: {
          DEFAULT: 'var(--line-1)',
          strong: 'var(--line-2)',
        },
        accent: {
          critical: '#ef4444',
          warning: '#f59e0b',
          info: '#3b82f6',
          ok: '#10b981',
        },
        // v4 severity ramp tuned for the dark basemap (UI_V4_PLAN D8).
        severity: {
          critical: '#ff5555',
          high: '#ff9955',
          medium: '#ffdd55',
          low: '#55ff55',
        },
      },
      // Typographic scale (Wave 3 Move 5) — replaces the ad-hoc
      // text-[10px]/text-[11px] sprawl with three named tiers. Base font is
      // bumped: `body` (13px) is the new field/cell default (was 10–11px),
      // `label` (12px) for keys/meta, `heading` (15px) for panel titles.
      // Line-heights are set for comfortable scanning (~1.45).
      fontSize: {
        // [size, lineHeight]
        label: ['0.75rem', { lineHeight: '1.1rem' }], // 12px — keys, chips, meta
        body: ['0.8125rem', { lineHeight: '1.2rem' }], // 13px — field values, cells, rows
        'body-lg': ['0.875rem', { lineHeight: '1.35rem' }], // 14px — emphasised body
        heading: ['0.9375rem', { lineHeight: '1.35rem' }], // 15px — panel titles
        'heading-lg': ['1.0625rem', { lineHeight: '1.5rem' }], // 17px — section titles
      },
      // 8pt spacing scale (Wave 3 Move 5). Named steps on an 8px grid so panel
      // padding/gaps use a scale, not arbitrary values. `density.*` aliases the
      // CSS-var-driven steps so a panel can also follow the active density mode.
      spacing: {
        density: 'var(--density-pad)',
        'density-gap': 'var(--density-gap)',
        'density-row': 'var(--density-row)',
        'density-section': 'var(--density-section)',
      },
    },
  },
  plugins: [require('@tailwindcss/typography')],
}

export default config
