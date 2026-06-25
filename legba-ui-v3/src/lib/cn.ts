import clsx from 'clsx'
import type { ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Tailwind-aware className composer. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
