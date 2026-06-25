/**
 * usePaletteRecords — the record half of the command-palette index (Move 3a).
 *
 * The palette is no longer just a panel-jump list; it is a RECORD-jump gateway.
 * This hook fetches the three operator-addressable record families from the
 * registry — targets, analysts, sources — so a fuzzy query like "brazil findings"
 * can resolve to the bound panel `target.findings:brazil`, and "country_assessor"
 * can drop that analyst into the Inspector.
 *
 * It reads only FROZEN generic registry routes (the same ones the registry panels
 * and the Investigate pickers already use), so it adds no new backend surface:
 *   - targets  → GET /registry/descriptors?family=target&head_only=true
 *   - analysts → GET /registry/descriptors?family=analyst&head_only=true
 *   - sources  → GET /registry/descriptors?family=source&head_only=true
 *
 * Failures degrade to an empty list per family (the palette still works on panels
 * + presets); records are a strict superset of the prior behaviour.
 */
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import type { SelectionKind } from '@/state/selection'

/** One registry record the palette can jump to. */
export interface PaletteRecord {
  /** 'target' | 'analyst' | 'source' — drives the open behaviour. */
  recordKind: Extract<SelectionKind, 'target' | 'analyst' | 'source'>
  id: string
  /** Human label (descriptor name), falls back to the id. */
  label: string
  /** Lifecycle state for the secondary hint (draft / active / retired …). */
  state?: string
}

interface DescriptorRow {
  descriptor_id: string
  name?: string | null
  state?: string | null
  family?: string | null
}

/** Soft fetch — a single family's records; never throws (empty on failure). */
async function softList(
  family: 'target' | 'analyst' | 'source',
  recordKind: PaletteRecord['recordKind'],
): Promise<PaletteRecord[]> {
  try {
    const rows = await apiGet<DescriptorRow[]>(
      `/registry/descriptors?family=${family}&head_only=true&limit=500`,
    )
    return rows
      .filter((r) => r.state !== 'retired')
      .map((r) => ({
        recordKind,
        id: r.descriptor_id,
        label: r.name && r.name.length > 0 ? r.name : r.descriptor_id,
        state: r.state ?? undefined,
      }))
  } catch {
    return []
  }
}

/**
 * The full record index: targets ∪ analysts ∪ sources. Cached for 5 minutes
 * (records change rarely relative to a palette open); refetched in the
 * background so a long-lived session stays current.
 */
export function usePaletteRecords(enabled: boolean): {
  records: PaletteRecord[]
  isLoading: boolean
} {
  const q = useQuery<PaletteRecord[]>({
    enabled,
    queryKey: ['palette-records'],
    queryFn: async () => {
      const [targets, analysts, sources] = await Promise.all([
        softList('target', 'target'),
        softList('analyst', 'analyst'),
        softList('source', 'source'),
      ])
      return [...targets, ...analysts, ...sources]
    },
    staleTime: 300_000,
    refetchInterval: 300_000,
  })
  return { records: q.data ?? [], isLoading: q.isLoading }
}
