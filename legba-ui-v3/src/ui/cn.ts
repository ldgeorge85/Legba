/**
 * The primitives' className composer — `lib/cn.ts`, taught the workstation's
 * own token class names.
 *
 * `tailwind-merge` only knows Tailwind's default scale, so it files an unknown
 * `text-<name>` under text COLOUR: `cn('text-label', 'text-ink-2')` drops
 * `text-label` (the 12px type step) as a "conflicting colour" and keeps the
 * colour. Likewise `rounded-panel` is not recognised as a radius, so a
 * caller's `rounded-lg` cannot override it. This helper registers the custom
 * font-size steps and radii (tailwind.config.ts) so both merge correctly.
 * Scoped to `src/ui/` deliberately: switching the shared `lib/cn.ts` would
 * change the type size of every existing surface that has been (silently)
 * losing its `text-label`/`text-body` step — see the UI-B1 report.
 */
import clsx from 'clsx'
import type { ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

const twMergeTokens = extendTailwindMerge({
  extend: {
    classGroups: {
      'font-size': [{ text: ['label', 'body', 'body-lg', 'heading', 'heading-lg'] }],
      rounded: [{ rounded: ['control', 'panel'] }],
    },
  },
})

/** Tailwind-aware className composer that knows the token class names. */
export function cn(...inputs: ClassValue[]): string {
  return twMergeTokens(clsx(inputs))
}
