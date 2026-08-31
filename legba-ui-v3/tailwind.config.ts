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
        // ── THE THREE CHANNELS (UI_HOLISTIC_DESIGN_2026-08-24 §5.2) ─────────
        // "One meaning, one channel, one ramp." These MIRROR the `--sev-*` /
        // `--conf-*` / `--state-*` tokens in globals.css (which is where the
        // meanings are documented, and which the light theme re-tunes).
        //
        // They are LITERALS, not `var(--…)`, deliberately: Tailwind v3 can only
        // apply an opacity modifier (`bg-accent-ok/20`, `bg-confidence-high/80`)
        // to a literal colour or to an `rgb(… / <alpha-value>)` form — pointing
        // these at a plain CSS var silently DROPS every `/nn` utility from the
        // build. Keep the two in sync by hand; `lint:tokens` and the design doc
        // are the cross-check.
        //
        // CHANNEL C · SYSTEM STATE — ops surfaces only. The ONLY place ok-green
        // is allowed to mean "nothing is wrong".
        accent: {
          critical: '#f85149',
          warning: '#d29922',
          info: '#3b82f6',
          ok: '#3fb950',
        },
        // CHANNEL A · SEVERITY — the only WARM ramp in the product (§5.2/§5.3).
        // Was the neon v4 ramp (#ff5555/#ff9955/#ffdd55/#55ff55, UI_V4_PLAN
        // D8): `low` at pure neon green made the calmest datum the brightest
        // pixel on the screen, on the map and in every feed row. Now Primer's
        // graduated set, with low RECEDING to a quiet grey. Colour was already
        // redundant here (SeverityBadge/ProvenanceBadge carry meaning in icon
        // shape + text label), which is what makes a ramp re-key safe.
        severity: {
          critical: '#f85149',
          high: '#db6d28',
          medium: '#d29922',
          low: '#6e7681',
          info: '#4493f8',
        },
        // CHANNEL B · CONFIDENCE — one hue, sequential, + a "no data" neutral.
        // Never red/amber/green: low confidence is not danger, high confidence
        // is not "ok" (§5.1's exact confusion).
        confidence: {
          high: '#79c0ff',
          moderate: '#4493f8',
          low: '#1f6feb',
          unassessed: '#30363d',
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
