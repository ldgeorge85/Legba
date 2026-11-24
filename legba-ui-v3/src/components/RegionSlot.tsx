/**
 * RegionSlot — an honest empty region (U-2 shell).
 *
 * A surface seeds a region for a view that does not exist yet (the Ledger's
 * forecasts, the Board's coverage strip, …). The region says WHAT will be
 * there and WHAT it waits on, in words — never a number, never a fake list,
 * never a spinner pretending something is loading.
 */
import { SLOTS, type SlotId } from '@/lib/surfaces'
import { lazy, Suspense } from 'react'

// Lazy: the field (and the map reads it shares) loads only under a profile with no geography.
const SubjectField = lazy(() => import('@/v4/globe/SubjectField'))

export function RegionSlot({ slot }: { slot: SlotId }) {
  // U-4: the subject field is built — it stands in for the map under a
  // profile that declares no geography.
  if (slot === 'slot:subject_field') {
    return (
      <Suspense fallback={null}>
        <SubjectField />
      </Suspense>
    )
  }
  const def = SLOTS[slot]
  if (!def) return null
  return (
    <div className="flex h-full w-full flex-col bg-surf-1 px-6 py-5" data-testid={`region-slot-${slot}`}>
      <div className="text-label uppercase tracking-[0.06em] text-ink-3">{def.title}</div>
      <p className="mt-3 max-w-[56ch] font-serif text-heading text-ink-1">{def.what}</p>
      <p className="mt-2 max-w-[56ch] text-body text-ink-3">
        Not built yet — waits on {def.waits}. Nothing in this region is a measurement.
      </p>
    </div>
  )
}
