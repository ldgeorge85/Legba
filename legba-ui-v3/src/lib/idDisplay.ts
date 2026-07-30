/**
 * idDisplay — truncate a raw UUID/SHA for product-surface display (U-5).
 *
 * A hostile UX review flagged raw ids rendered in full in reader-facing chrome
 * (the Inspector header finding id, export-preview items) — technically
 * honest but noisy, and un-copyable without a text-select. Pure, DOM-free;
 * paired with `@/components/CopyableId` for the actual affordance.
 */

/** Truncate `id` to `head…tail` when it's longer than that would save; short
 *  ids (already ≤ head+tail+1) pass through UNCHANGED — never truncate what's
 *  already short enough to read at a glance. */
export function truncateId(id: string, head = 8, tail = 4): string {
  if (id.length <= head + tail + 1) return id
  return `${id.slice(0, head)}…${id.slice(-tail)}`
}
