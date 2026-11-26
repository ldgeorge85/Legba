/**
 * The Board's reads: the since-diff and the window's checked findings, under
 * the SAME query keys and paths the full daily read (`JudgmentBands.tsx`)
 * uses, so the list, the rail's change counts and the daily read share one
 * request each. The "since" point is the existing visit cursor
 * (`lib/morningReadBands::pageVisit`), resolved once per page load and shared
 * by every region that states a since-time.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { pageVisit, type JudgmentFindingRow, type JudgmentSince } from '@/lib/morningReadBands'
import { utcStamp } from './readFormat'
import { changeCounts } from './boardList'

/** The daily read's own poll. */
export const BOARD_REFETCH_MS = 5 * 60_000
/** The page of findings the Checked cut reads (the daily read's own). */
export const BOARD_CHECKED_LIMIT = 200

/**
 * The visit cursor: THE ONE "since" of this page load
 * (`lib/morningReadBands::pageVisit`), the same for every region.
 */
export function useVisitCursor(): { cursor: string; firstVisit: boolean } {
  return useMemo(() => {
    const v = pageVisit()
    return { cursor: v.cursor, firstVisit: v.firstVisit }
  }, [])
}

/**
 * The words every region uses for the since-time, so no two regions can
 * state it differently: the fallback reading hour on a first visit, else the
 * time the reader last looked (UTC).
 */
export function sinceWords(v: { cursor: string; firstVisit: boolean }): string {
  return v.firstVisit ? 'since yesterday 06:00Z' : `since you last looked, ${utcStamp(v.cursor) ?? v.cursor}`
}

export function useSince(cursor: string, enabled = true) {
  return useQuery<JudgmentSince>({
    queryKey: ['morning-judgment', 'since', cursor],
    refetchInterval: BOARD_REFETCH_MS,
    enabled,
    queryFn: () => apiGet<JudgmentSince>(`/v3/since?cursor=${encodeURIComponent(cursor)}`),
  })
}

export function useWindowFindings(cursor: string) {
  return useQuery<{ data: JudgmentFindingRow[] }>({
    queryKey: ['morning-judgment', 'checked', cursor],
    refetchInterval: BOARD_REFETCH_MS,
    queryFn: () =>
      apiGet<{ data: JudgmentFindingRow[] }>(
        `/findings?since=${encodeURIComponent(cursor)}&limit=${BOARD_CHECKED_LIMIT}&fields=judgment`,
      ),
  })
}

/**
 * Changes per subject since the reader last looked (the rail's order and
 * figure); null until the diff has answered, so nothing prints a zero it did
 * not read.
 */
export function useChangeCounts(enabled = true): Map<string, number> | null {
  const { cursor } = useVisitCursor()
  const since = useSince(cursor, enabled)
  return useMemo(() => (since.data ? changeCounts(since.data) : null), [since.data])
}
