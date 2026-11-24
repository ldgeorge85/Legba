import clsx from 'clsx'
import type { ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

/**
 * The type scale's named sizes (`tailwind.config.ts` → `fontSize`). Without
 * this, tailwind-merge reads `text-label` as a text COLOUR and drops it when a
 * later `text-ink-1` arrives, so a control composed with `cn()` fell back to
 * the inherited size (U-3: the Watch surface's sort buttons drew at 16 px).
 */
export const TYPE_SCALE = ['label', 'body', 'body-lg', 'heading', 'heading-lg', 'display'] as const

const twMerge = extendTailwindMerge({
  extend: { classGroups: { 'font-size': [{ text: [...TYPE_SCALE] }] } },
})

/** Tailwind-aware className composer. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
