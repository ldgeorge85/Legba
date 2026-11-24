/**
 * queryLayer — the console's ONE shared query layer, and the rule that a
 * region nobody can see fetches nothing (U-5, the landing fetch diet).
 *
 * Two layers share every read:
 *
 *   1. REQUESTS (`lib/queryCache.ts`, under `apiGet`): identical GETs in
 *      flight are one request whoever asks; slow-changing routes keep their
 *      answer for a while.
 *   2. QUERIES (react-query, one `QueryCache` for the whole page): one query
 *      key per logical resource, shared by every panel that names it.
 *
 * VISIBILITY. Dockview keeps an inactive tab's React tree MOUNTED, and the
 * shell keeps the command palette mounted while it is closed, so a panel that
 * fetches on mount used to fetch for every hidden tab (the Board's Wall) and
 * for a closed palette. Rather than ask every panel to remember a gate, each
 * such region renders its children under a {@link QueryRegion}: a
 * `QueryClient` that SHARES the page's cache (so data, dedupe and invalidation
 * are one), and whose only difference is that while the region is inactive
 * every query observed inside it is disabled. A disabled observer still reads
 * whatever the cache holds (another, visible, panel may have fetched it); it
 * just never starts a fetch or a poll. When the region becomes active the
 * same observers are re-enabled and fetch what is stale — the reader sees the
 * same panel, read when it is first shown.
 *
 * A region's state follows its tile at RENDER time: a tile visible when it
 * mounts renders its queries enabled from the first frame (its loading state,
 * never a disabled "nothing yet"), and a tab brought forward renders enabled
 * in the same frame it is shown. The surface seed adds a region's other views
 * INACTIVE (`lib/surfaceDock.ts::seedSurface`), so a hidden tab is never
 * visible, even for a moment, while the landing is laid out.
 *
 * THE FETCH LOG. {@link installFetchLog} records, for every query fetch, the
 * regions observing it; a fetch every observer of which sits in an inactive
 * region is a hidden-region fetch. The browser walk reads
 * `window.__legbaFetchLog` and fails on one (tools/walk/budgets.mjs).
 */
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import {
  QueryClient,
  QueryClientProvider,
  useQueryClient,
  type DefaultError,
  type QueryKey,
  type QueryObserverOptions,
} from '@tanstack/react-query'
import { useTileVisible } from '@/components/useTileVisible'
import { useDockviewPanelApi } from '@/components/DockviewPanelApiContext'

/** The option keys a region adds to the observers it defaults. */
const REGION_KEY = '__legbaRegion'
const ENABLED_KEY = '__legbaEnabled'

type AnyOptions = QueryObserverOptions<unknown, DefaultError, unknown, unknown, QueryKey> & {
  [REGION_KEY]?: string
  [ENABLED_KEY]?: unknown
}

/** Every live region by id (the fetch log's attribution). */
const regions = new Map<string, RegionQueryClient>()

/**
 * A QueryClient over the ROOT client's cache whose observers are disabled
 * while the region is inactive.
 */
export class RegionQueryClient extends QueryClient {
  readonly region: string
  readonly root: QueryClient
  #active: boolean

  constructor(root: QueryClient, region: string, active = false) {
    super({
      queryCache: root.getQueryCache(),
      mutationCache: root.getMutationCache(),
      defaultOptions: root.getDefaultOptions(),
    })
    this.root = root
    this.region = region
    this.#active = active
  }

  get active(): boolean {
    return this.#active
  }

  // The root client owns the focus / online subscriptions; a region adding
  // its own would refetch the shared cache once per region on every focus.
  override mount(): void {}
  override unmount(): void {}

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  override defaultQueryOptions(options: any): any {
    const d = super.defaultQueryOptions(options) as unknown as AnyOptions
    // The caller's own `enabled`, remembered across re-defaulting (an observer
    // re-defaults its already-defaulted options on every setOptions).
    const own = ENABLED_KEY in d ? d[ENABLED_KEY] : d.enabled
    return {
      ...d,
      [REGION_KEY]: this.region,
      [ENABLED_KEY]: own,
      enabled: this.#active ? own : false,
    }
  }

  /**
   * Set the flag only (no observer is touched): called while the region
   * renders, so the queries rendered in the same pass default with the new
   * state. {@link reevaluate} brings the observers that did not re-render.
   */
  markActive(active: boolean): void {
    this.#active = active
  }

  /** Re-apply this region's state to every observer it defaulted. */
  reevaluate(): void {
    for (const query of this.getQueryCache().getAll()) {
      for (const observer of [...query.observers]) {
        const o = observer.options as AnyOptions
        if (o[REGION_KEY] === this.region) observer.setOptions(o as never)
      }
    }
  }

  /** Activate or deactivate now (both steps). */
  setActive(active: boolean): void {
    if (active === this.#active) return
    this.#active = active
    this.reevaluate()
  }
}

/** The root client above any region (a region nested in a region shares the root). */
function rootOf(client: QueryClient): QueryClient {
  return client instanceof RegionQueryClient ? client.root : client
}

let regionSeq = 0

/**
 * Render `children` with every query they observe enabled only while `active`.
 */
export function QueryRegion({ id, active, children }: { id?: string; active: boolean; children: ReactNode }) {
  const parent = useQueryClient()
  const idRef = useRef<string>(id ?? `region-${++regionSeq}`)
  const [client] = useState(() => new RegionQueryClient(rootOf(parent), idRef.current, active))
  // The children rendering in this pass default with the new state…
  client.markActive(active)
  // …and every observer of the region (a memoised child that did not
  // re-render included) is brought to it before the frame is painted.
  useLayoutEffect(() => {
    client.reevaluate()
  }, [active, client])
  useEffect(() => {
    regions.set(client.region, client)
    return () => {
      if (regions.get(client.region) === client) regions.delete(client.region)
    }
  }, [client])
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

/**
 * A Dockview tile's region: active while the tile is the visible one in its
 * group. Outside a tile (a unit test, a standalone render) it is always active.
 */
export function TileQueryRegion({ children }: { children: ReactNode }) {
  const api = useDockviewPanelApi()
  const visible = useTileVisible()
  return (
    <QueryRegion id={api ? `tile:${api.id}` : undefined} active={visible}>
      {children}
    </QueryRegion>
  )
}

// ---------------------------------------------------------------------------
// The fetch log
// ---------------------------------------------------------------------------

export interface FetchLogEntry {
  /** The query key, as JSON. */
  key: string
  /** The regions observing the query when it fetched (`null` = outside any region). */
  regions: Array<string | null>
  /** True when every observer sat in an inactive region. */
  hidden: boolean
  /** The first such region (the walk's report names it). */
  tile: string | null
  at: number
}

const LOG_CAP = 400

declare global {
  interface Window {
    __legbaFetchLog?: FetchLogEntry[]
  }
}

/** Attribute one fetching query to the regions that observe it. */
export function attributeFetch(observerOptions: ReadonlyArray<Record<string, unknown>>): {
  regions: Array<string | null>
  hidden: boolean
  tile: string | null
} {
  const ids = observerOptions.map((o) => (typeof o[REGION_KEY] === 'string' ? (o[REGION_KEY] as string) : null))
  const inactive = ids.filter((r) => r !== null && regions.get(r)?.active === false)
  const hidden = ids.length > 0 && inactive.length === ids.length
  return { regions: ids, hidden, tile: hidden ? inactive[0] : null }
}

/**
 * Record every query fetch on `client`'s cache into `window.__legbaFetchLog`
 * (bounded). Returns the unsubscribe.
 */
export function installFetchLog(client: QueryClient): () => void {
  if (typeof window === 'undefined') return () => {}
  const log: FetchLogEntry[] = (window.__legbaFetchLog = window.__legbaFetchLog ?? [])
  return client.getQueryCache().subscribe((event) => {
    if (event.type !== 'updated' || event.action.type !== 'fetch') return
    const q = event.query
    const a = attributeFetch(q.observers.map((o) => o.options as unknown as Record<string, unknown>))
    let key: string
    try {
      key = JSON.stringify(q.queryKey)
    } catch {
      key = String(q.queryHash)
    }
    log.push({ key, ...a, at: Math.round(performance.now()) })
    if (log.length > LOG_CAP) log.splice(0, log.length - LOG_CAP)
  })
}
