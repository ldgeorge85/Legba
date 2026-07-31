/**
 * Supply-chain desk hook — the thematic `lane_*` / `flow_*` targets (Sidebar
 * "Supply chain" subsection under Desks, U-2 follow-up).
 *
 * These desks are a NEW target family (`scope.domain: thematic` — see
 * `descriptors/target_lane_hormuz.yaml` et al.) with deliberately NO
 * `country_composition` tier yet (gated on a 7-day readout), so
 * `useCountryVerdicts`' `/findings?analyst_id=country_composition` read never
 * surfaces them — they need their own source.
 *
 * SOURCE: the same registry descriptor-heads surface the Engine Room's
 * Targets section (`panels/registry/Targets.tsx`) and the bound-panel
 * synthesizer (`panel-registry/synthesize.ts` / `useRegistry.ts`) already
 * read — `GET /registry/descriptors?family=target&head_only=true` (head_only
 * still returns the full `body`, confirmed against `DescriptorRowOut` in
 * `registry/api.py`). Filtered to `state === 'active'` AND
 * `body.scope.tags` contains `supply_chain`. NOTHING here hardcodes the three
 * live lane ids — the set grows as more lanes activate (draft/configured
 * desks are filtered out by the state check, so they stay invisible until a
 * deliberate operator activation).
 *
 * RECENCY: a desk has no confidence band (no composition), so instead of a
 * fabricated chip we surface the cheapest honest affordance available — the
 * newest `disruption_status` finding's timestamp for that target, read from
 * the same `/findings?analyst_id=…` shape `useCountryVerdicts` uses. A desk
 * with findings outside the fetch window (or none yet) simply shows no age —
 * never a fabricated "just now".
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'

const SUPPLY_CHAIN_TAG = 'supply_chain'
const DISRUPTION_STATUS_ANALYST_ID = 'disruption_status'

interface TargetDescriptorRow {
  descriptor_id: string
  state: string
  body?: { scope?: { tags?: unknown } } | null
}

interface FindingRow {
  target_id: string | null
  produced_at: string
}

interface FindingsResponse {
  data: FindingRow[]
  next_cursor: string | null
}

export interface SupplyChainDesk {
  targetId: string
  /** The newest `disruption_status` finding's `produced_at` for this desk, or
   *  `null` when there isn't one (yet) in the fetch window — never fabricated. */
  latestFindingAt: string | null
}

function hasSupplyChainTag(row: TargetDescriptorRow): boolean {
  const tags = row.body?.scope?.tags
  return Array.isArray(tags) && tags.some((t) => t === SUPPLY_CHAIN_TAG)
}

async function fetchActiveSupplyChainTargetIds(): Promise<string[]> {
  const rows = await apiGet<TargetDescriptorRow[]>(
    '/registry/descriptors?family=target&head_only=true&limit=500',
  )
  return rows.filter((r) => r.state === 'active' && hasSupplyChainTag(r)).map((r) => r.descriptor_id)
}

async function fetchLatestDisruptionFindingAt(): Promise<Map<string, string>> {
  const page = await apiGet<FindingsResponse>(
    `/findings?analyst_id=${DISRUPTION_STATUS_ANALYST_ID}&limit=200`,
  )
  const latest = new Map<string, string>()
  for (const row of page.data ?? []) {
    if (!row.target_id) continue
    const prev = latest.get(row.target_id)
    if (!prev || Date.parse(row.produced_at) > Date.parse(prev)) {
      latest.set(row.target_id, row.produced_at)
    }
  }
  return latest
}

async function fetchSupplyChainDesks(): Promise<SupplyChainDesk[]> {
  const [targetIds, latestByTarget] = await Promise.all([
    fetchActiveSupplyChainTargetIds(),
    // A findings-read failure shouldn't hide the desks themselves — degrade
    // to name-only rows (no age) rather than losing reachability.
    fetchLatestDisruptionFindingAt().catch(() => new Map<string, string>()),
  ])
  return targetIds.map((targetId) => ({
    targetId,
    latestFindingAt: latestByTarget.get(targetId) ?? null,
  }))
}

export interface UseSupplyChainDesksResult {
  desks: SupplyChainDesk[]
  isLoading: boolean
}

export function useSupplyChainDesks(): UseSupplyChainDesksResult {
  const q = useQuery<SupplyChainDesk[]>({
    queryKey: ['world-supply-chain-desks'],
    queryFn: fetchSupplyChainDesks,
    refetchInterval: 120_000,
    staleTime: 60_000,
  })
  const desks = useMemo(() => q.data ?? [], [q.data])
  return { desks, isLoading: q.isLoading }
}
