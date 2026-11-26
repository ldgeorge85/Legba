/**
 * The "as recorded" strip a region carries while the console is set to an
 * earlier instant (`lib/asOf.ts`).
 *
 * A region whose reads honour the instant says so: "Showing the record as of
 * 20 Nov 08:10 UTC". A region that cannot says it is not time-shifted, so the
 * reader never takes today's data for the past. Both offer the way back.
 * With no instant set the region renders exactly as it would without this
 * wrapper.
 */
import type { ReactNode } from 'react'
import { asOfWords, honoursAsOf, useAsOf } from '@/lib/asOf'
import { useWorldState } from '@/v4/world/worldState'
import { cn } from '@/lib/cn'

export function AsOfStrip({ honoured, atMs }: { honoured: boolean; atMs: number }) {
  return (
    <div
      className={cn(
        'flex shrink-0 items-baseline gap-2 border-b px-3 py-1 text-label',
        honoured ? 'border-point text-point' : 'border-line text-ink-2',
      )}
      role="status"
      data-testid="as-of-strip"
      data-honoured={honoured ? '1' : '0'}
    >
      <span className="min-w-0 truncate">
        {honoured ? (
          <>Showing the record as of {asOfWords(atMs)}</>
        ) : (
          <>Not time-shifted: this region shows the record now, not as of {asOfWords(atMs)}</>
        )}
      </span>
      <button
        type="button"
        className="ml-auto shrink-0 underline underline-offset-2 hover:text-ink-1 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent-emphasis"
        onClick={() => useWorldState.getState().setAsOf(null)}
        data-testid="as-of-return"
      >
        return to now
      </button>
    </div>
  )
}

/** Wrap one region's content: the strip on top while an instant is set. */
export function RegionAsOf({ viewId, children }: { viewId: string; children: ReactNode }) {
  const atMs = useAsOf()
  if (atMs === null) return <>{children}</>
  return (
    <div className="flex h-full w-full flex-col" data-as-of-region={viewId}>
      <AsOfStrip honoured={honoursAsOf(viewId)} atMs={atMs} />
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  )
}
