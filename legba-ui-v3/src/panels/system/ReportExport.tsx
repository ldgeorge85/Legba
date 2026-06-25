/**
 * P-UI6-C. Report Export (`system.report_export`) — UI-6 (Tier G).
 *
 * Pull a slice of findings (+ best-effort situations), let the operator tick the
 * rows to include, then export the selection four ways:
 *   - **STIX 2.1 bundle** — mirrors the `stix_bundle` output kind client-side.
 *     Each item → a `report` SDO + an `indicator` SDO (so downstream TIPs that
 *     consume indicators get one per finding), `derived_from` → `relationship`
 *     SDOs, with TLP markings. The report+relationship SDOs come from the
 *     unit-tested `@/lib/reportModel`; the indicator SDOs are layered here.
 *   - **Raw JSON** — the selected rows verbatim (no STIX mapping), for ad-hoc
 *     downstream tooling.
 *   - **Markdown report** — severity-grouped intelligence brief (model-built).
 *   - **Print → PDF** — `window.print()` over a `print:`-only formatted view
 *     (the rest of the app is `print:hidden`), so the operator gets a clean
 *     paginated PDF via the browser print dialog.
 *
 * Selection list (operator feedback, 2026-06): the row list now FILLS the panel
 * height and scrolls (flex + min-h-0 + overflow-y-auto), and findings paginate
 * through the `/findings` `next_cursor` ("load more") instead of capping at one
 * page. Server-side filters (severity / target / analyst / since) are pushed to
 * the endpoint; a client-side meta-kind filter narrows the merged findings +
 * situations set. Each row is labeled with its finding kind (`data.kind` meta-
 * kind when present, else the substrate kind).
 *
 * Bundle / markdown building lives in `@/lib/reportModel` (DOM-free,
 * unit-tested); this panel does fetch + select + augment + download + print.
 */

import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'
import {
  buildMarkdownReport,
  buildStixBundle,
  stixId,
  type ReportItem,
  type Tlp,
} from '@/lib/reportModel'

/** Panel-local format superset (the model ships stix + markdown). */
type ExportFormat = 'stix' | 'json' | 'markdown'

/** Findings page envelope, mirroring `FindingsPage` in substrate_reads_api.py. */
interface Page {
  data: Record<string, unknown>[]
  next_cursor: string | null
}

/**
 * A `ReportItem` plus the panel-local **meta-kind** label. `ReportItem.kind`
 * is the STIX-typing discriminator (`finding` | `situation`) and must stay
 * that, so the finer-grained analyst meta-kind (e.g. `competing_hypotheses`,
 * `situation_assessment`) rides alongside for display + the kind filter.
 */
interface RowItem extends ReportItem {
  metaKind: string
}

/** Server-side findings filters pushed onto the `/findings` query string. */
interface Filters {
  severity: string
  target_id: string
  analyst_id: string
  since: string
}

const EMPTY_FILTERS: Filters = { severity: '', target_id: '', analyst_id: '', since: '' }

/** Per-page fetch size — findings paginate, so this is the "load more" step. */
const PAGE_SIZE = 100

async function softGet<T>(path: string, fallback: T): Promise<T> {
  try {
    return await apiGet<T>(path)
  } catch {
    return fallback
  }
}

/** Best-effort array body (some endpoints return `[]`, some `{ data: [] }`). */
function asRows(r: unknown): Record<string, unknown>[] {
  if (Array.isArray(r)) return r as Record<string, unknown>[]
  if (r && typeof r === 'object' && Array.isArray((r as { data?: unknown }).data)) {
    return (r as { data: Record<string, unknown>[] }).data
  }
  return []
}

/** The display meta-kind: the analyst-output sub-kind (`data.kind`) when the
 *  row carries one, else the substrate kind (`finding` / `situation`). */
function metaKindOf(kind: ReportItem['kind'], row: Record<string, unknown>): string {
  const data = row.data
  if (data && typeof data === 'object') {
    const dk = (data as { kind?: unknown }).kind
    if (typeof dk === 'string' && dk) return dk
  }
  return kind
}

function toItem(kind: ReportItem['kind'], row: Record<string, unknown>): RowItem {
  const str = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : String(v))
  const sn = (v: unknown) => (typeof v === 'string' && v ? v : null)
  return {
    kind,
    metaKind: metaKindOf(kind, row),
    id: str(row.id),
    title: str(row.title) || str(row.summary) || str(row.name) || `(untitled ${kind})`,
    body: str(row.body) || str(row.summary),
    severity: sn(row.severity),
    target_id: sn(row.target_id),
    produced_at: sn(row.produced_at) ?? sn(row.opened_at),
    derived_from: Array.isArray(row.derived_from)
      ? (row.derived_from as unknown[]).map((d) => String(d))
      : Array.isArray(row.contributing_finding_ids)
        ? (row.contributing_finding_ids as unknown[]).map((d) => String(d))
        : [],
  }
}

/** Build the `/findings` query string from the active filters + page cursor. */
function findingsQuery(filters: Filters, cursor: string | null): string {
  const qs = new URLSearchParams({ limit: String(PAGE_SIZE) })
  if (filters.severity) qs.set('severity', filters.severity)
  if (filters.target_id) qs.set('target_id', filters.target_id)
  if (filters.analyst_id) qs.set('analyst_id', filters.analyst_id)
  if (filters.since) qs.set('since', new Date(filters.since).toISOString())
  if (cursor) qs.set('cursor', cursor)
  return `/findings?${qs.toString()}`
}

/**
 * STIX bundle with an `indicator` SDO appended per item, on top of the model's
 * report + relationship SDOs. Deterministic ids reuse the model's `stixId`.
 */
function buildStixWithIndicators(
  items: ReportItem[],
  opts: { tlp: Tlp; created: string },
): Record<string, unknown> {
  const bundle = buildStixBundle(items, opts)
  const objects = [...((bundle.objects as Array<Record<string, unknown>>) ?? [])]
  for (const item of items) {
    objects.push({
      type: 'indicator',
      spec_version: '2.1',
      id: stixId('indicator', `${item.kind}:${item.id}`),
      created: opts.created,
      modified: opts.created,
      name: item.title,
      description: item.body || item.title,
      indicator_types: ['anomalous-activity'],
      // A descriptive STIX pattern keyed on the Legba provenance id (selection
      // is intel, not a network IOC — this keeps the SDO valid + traceable).
      pattern: `[x-legba:source_id = '${item.id}']`,
      pattern_type: 'stix',
      valid_from: item.produced_at ?? opts.created,
      labels: item.severity ? [item.kind, `severity:${item.severity}`] : [item.kind],
      x_legba_source_id: item.id,
      x_legba_target_id: item.target_id,
    })
  }
  return { ...bundle, objects }
}

interface Artifact {
  filename: string
  mime: string
  content: string
}

function buildArtifactEx(
  items: ReportItem[],
  format: ExportFormat,
  opts: { title: string; tlp: Tlp; created: string },
): Artifact {
  const stamp = opts.created.slice(0, 10)
  if (format === 'stix') {
    return {
      filename: `legba-report-${stamp}.stix.json`,
      mime: 'application/json',
      content: JSON.stringify(buildStixWithIndicators(items, opts), null, 2),
    }
  }
  if (format === 'json') {
    return {
      filename: `legba-report-${stamp}.json`,
      mime: 'application/json',
      content: JSON.stringify(
        { title: opts.title, tlp: opts.tlp, created: opts.created, count: items.length, items },
        null,
        2,
      ),
    }
  }
  return {
    filename: `legba-report-${stamp}.md`,
    mime: 'text/markdown',
    content: buildMarkdownReport(items, opts),
  }
}

/** Trigger a browser download for the given content (Blob object-URL). */
function downloadArtifact(filename: string, mime: string, content: string) {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

const SEVERITY_RANK: Record<string, number> = { critical: 4, high: 3, medium: 2, low: 1 }
const SEVERITIES = ['critical', 'high', 'medium', 'low'] as const

export default function ReportExportPanel({ registration }: PanelProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [format, setFormat] = useState<ExportFormat>('stix')
  const [tlp, setTlp] = useState<Tlp>('amber')
  const [title, setTitle] = useState('Legba Intelligence Report')
  const [preview, setPreview] = useState<{ filename: string; content: string } | null>(null)

  // Filter inputs — server-side findings filters + a client-side meta-kind narrow.
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)
  const [kindFilter, setKindFilter] = useState('')

  // Findings paginate through the `next_cursor`; severity/target/analyst/since
  // are pushed server-side so the cursor stays consistent under the filter.
  const findings = useInfiniteQuery<Page>({
    queryKey: ['report-findings', filters],
    queryFn: async ({ pageParam }) =>
      softGet<Page>(findingsQuery(filters, (pageParam as string | null) ?? null), {
        data: [],
        next_cursor: null,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })

  // Situations are supplemental + small; one best-effort page, re-fetched when
  // the target/since filters change so the merged set stays coherent.
  const situations = useQuery<Record<string, unknown>[]>({
    queryKey: ['report-situations', filters.target_id, filters.since],
    queryFn: async () => {
      const qs = new URLSearchParams({ limit: '100' })
      if (filters.target_id) qs.set('target_id', filters.target_id)
      if (filters.since) qs.set('since', new Date(filters.since).toISOString())
      return asRows(await softGet<unknown>(`/situations?${qs.toString()}`, []))
    },
  })

  const findingRows = useMemo(
    () => (findings.data?.pages ?? []).flatMap((p) => asRows(p.data)),
    [findings.data],
  )

  // Merge findings (paginated, server-filtered) + situations (best-effort) into
  // the candidate list once, then derive the visible set + the kind options.
  const allItems = useMemo<RowItem[]>(
    () =>
      [
        ...findingRows.map((r) => toItem('finding', r)),
        ...(situations.data ?? []).map((r) => toItem('situation', r)),
      ].filter((i) => i.id),
    [findingRows, situations.data],
  )

  // Client-side meta-kind narrow (the only filter the endpoint can't express,
  // since `/findings` hard-filters substrate kind='finding').
  const items = useMemo<RowItem[]>(() => {
    const kf = kindFilter.trim().toLowerCase()
    return kf ? allItems.filter((i) => i.metaKind.toLowerCase().includes(kf)) : allItems
  }, [allItems, kindFilter])

  // Distinct meta-kinds present in the loaded set — drives the kind <select>.
  const kindOptions = useMemo(
    () => [...new Set(allItems.map((i) => i.metaKind))].filter(Boolean).sort(),
    [allItems],
  )

  const key = (i: ReportItem) => `${i.kind}:${i.id}`
  const selectedItems = useMemo(
    () => items.filter((i) => selected.has(key(i))),
    [items, selected],
  )

  function toggle(i: ReportItem) {
    setSelected((prev) => {
      const next = new Set(prev)
      const k = key(i)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })
  }
  function selectAll() {
    setSelected(new Set(items.map(key)))
  }
  function clearAll() {
    setSelected(new Set())
  }
  function setFilter<K extends keyof Filters>(k: K, v: Filters[K]) {
    setFilters((prev) => ({ ...prev, [k]: v }))
  }
  function resetFilters() {
    setFilters(EMPTY_FILTERS)
    setKindFilter('')
  }

  function opts() {
    return { title, tlp, created: new Date().toISOString() }
  }
  function generate() {
    const a = buildArtifactEx(selectedItems, format, opts())
    setPreview({ filename: a.filename, content: a.content })
  }
  function download() {
    const a = buildArtifactEx(selectedItems, format, opts())
    downloadArtifact(a.filename, a.mime, a.content)
  }
  function printPdf() {
    // The print-only view (below) renders the selection; the rest of the app is
    // print:hidden. Browser print → "Save as PDF" yields the report PDF.
    window.print()
  }

  // Severity-sorted selection for the printable view.
  const printItems = useMemo(
    () =>
      [...selectedItems].sort(
        (a, b) => (SEVERITY_RANK[b.severity ?? ''] ?? 0) - (SEVERITY_RANK[a.severity ?? ''] ?? 0),
      ),
    [selectedItems],
  )

  const isLoading = findings.isLoading
  const error = findings.error
  const filtersActive = !!(
    filters.severity ||
    filters.target_id ||
    filters.analyst_id ||
    filters.since ||
    kindFilter
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${selectedItems.length} selected of ${items.length}${findings.hasNextPage ? '+' : ''}`}
      onRefresh={() => {
        findings.refetch()
        situations.refetch()
      }}
    >
      {/* screen-only controls + body (hidden when printing) */}
      <div className="print:hidden flex flex-col h-full min-h-0">
        {/* export controls */}
        <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
          <select
            className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
            value={format}
            onChange={(e) => setFormat(e.target.value as ExportFormat)}
            data-testid="report-format"
          >
            <option value="stix">STIX 2.1 bundle</option>
            <option value="json">Raw JSON</option>
            <option value="markdown">Markdown report</option>
          </select>
          <select
            className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
            value={tlp}
            onChange={(e) => setTlp(e.target.value as Tlp)}
            data-testid="report-tlp"
          >
            {(['white', 'green', 'amber', 'red'] as Tlp[]).map((t) => (
              <option key={t} value={t}>
                TLP:{t.toUpperCase()}
              </option>
            ))}
          </select>
          <input
            className="flex-1 min-w-[140px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="report title…"
            data-testid="report-title"
          />
        </div>

        {/* filter controls — severity / kind / target / analyst / since */}
        <div className="flex items-center gap-2 mb-2 text-xs flex-wrap" data-testid="report-filters">
          <select
            className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
            value={filters.severity}
            onChange={(e) => setFilter('severity', e.target.value)}
            data-testid="report-filter-severity"
          >
            <option value="">all severities</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <select
            className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value)}
            data-testid="report-filter-kind"
          >
            <option value="">all kinds</option>
            {kindOptions.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
          <input
            className="w-[120px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
            value={filters.target_id}
            onChange={(e) => setFilter('target_id', e.target.value)}
            placeholder="target id…"
            data-testid="report-filter-target"
          />
          <input
            className="w-[120px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
            value={filters.analyst_id}
            onChange={(e) => setFilter('analyst_id', e.target.value)}
            placeholder="analyst id…"
            data-testid="report-filter-analyst"
          />
          <input
            type="date"
            className="bg-surface-200 border border-slate-700 rounded p-1 px-2 text-slate-300"
            value={filters.since}
            onChange={(e) => setFilter('since', e.target.value)}
            title="only rows produced on/after this date"
            data-testid="report-filter-since"
          />
          {filtersActive && (
            <button
              onClick={resetFilters}
              className="text-slate-400 hover:text-slate-200 underline"
              data-testid="report-filter-reset"
            >
              reset filters
            </button>
          )}
        </div>

        {/* action controls */}
        <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
          <button
            onClick={generate}
            disabled={selectedItems.length === 0}
            className="bg-accent-info/20 hover:bg-accent-info/30 border border-accent-info/50 text-accent-info rounded px-3 py-1 disabled:opacity-40"
            data-testid="report-generate"
          >
            generate
          </button>
          <button
            onClick={download}
            disabled={selectedItems.length === 0}
            className="bg-surface-200 hover:bg-surface-300 border border-slate-700 rounded px-3 py-1 disabled:opacity-40"
            data-testid="report-download"
          >
            download
          </button>
          <button
            onClick={printPdf}
            disabled={selectedItems.length === 0}
            className="bg-surface-200 hover:bg-surface-300 border border-slate-700 rounded px-3 py-1 disabled:opacity-40"
            title="Print → Save as PDF"
            data-testid="report-print"
          >
            print → PDF
          </button>
          <button onClick={selectAll} className="text-slate-400 hover:text-slate-200 underline" data-testid="report-select-all">
            select all
          </button>
          <button onClick={clearAll} className="text-slate-400 hover:text-slate-200 underline" data-testid="report-clear">
            clear
          </button>
        </div>

        {isLoading && <div className="text-slate-500 text-sm">loading substrate rows…</div>}
        {error instanceof Error && (
          <div className="text-rose-400 text-sm">error: {error.message}</div>
        )}

        {/* selectable rows — fills remaining height + scrolls; shrinks to share
            space with the preview when one is open. */}
        <div
          className="flex-1 min-h-0 overflow-y-auto mb-2 space-y-1 text-xs"
          data-testid="report-items"
        >
          {items.map((i) => (
            <label
              key={key(i)}
              className="flex items-center gap-2 bg-surface-100 hover:bg-surface-200 border border-slate-800 rounded p-1.5 cursor-pointer"
              data-testid={`report-item-${i.kind}-${i.id}`}
            >
              <input
                type="checkbox"
                checked={selected.has(key(i))}
                onChange={() => toggle(i)}
                data-testid={`report-check-${i.kind}-${i.id}`}
              />
              <span
                className="rounded px-1 bg-slate-700 text-slate-200 shrink-0"
                title={`kind: ${i.metaKind}`}
                data-testid={`report-item-kind-${i.kind}-${i.id}`}
              >
                {i.metaKind}
              </span>
              {i.severity && <span className="text-slate-400 shrink-0">[{i.severity}]</span>}
              <span className="text-slate-200 truncate">{i.title}</span>
            </label>
          ))}
          {items.length === 0 && !isLoading && (
            <div className="text-slate-500 py-2 text-center">
              {filtersActive ? 'no rows match the filters' : 'no substrate rows to export'}
            </div>
          )}
          {/* pagination — pull the next findings page through the cursor. */}
          {findings.hasNextPage && (
            <button
              onClick={() => findings.fetchNextPage()}
              disabled={findings.isFetchingNextPage}
              className="w-full bg-surface-200 hover:bg-surface-300 border border-slate-700 rounded px-3 py-1 disabled:opacity-40 text-slate-300"
              data-testid="report-load-more"
            >
              {findings.isFetchingNextPage ? 'loading…' : 'load more findings'}
            </button>
          )}
        </div>

        {/* preview */}
        {preview && (
          <div className="flex-1 min-h-0 flex flex-col border-t border-slate-800 pt-2">
            <div className="text-[11px] text-slate-500 mb-1">
              preview · <span className="font-mono text-slate-400">{preview.filename}</span>
            </div>
            <pre
              className="flex-1 overflow-auto bg-surface-50 border border-slate-800 rounded p-2 text-[11px] text-slate-300 whitespace-pre-wrap break-words"
              data-testid="report-preview"
            >
              {preview.content}
            </pre>
          </div>
        )}
      </div>

      {/* print-only formatted view — hidden on screen, the only thing printed */}
      <div className="hidden print:block text-black" data-testid="report-print-view">
        <h1 className="text-2xl font-bold mb-1">{title}</h1>
        <div className="text-sm mb-4">
          TLP:{tlp.toUpperCase()} — {printItems.length} item{printItems.length === 1 ? '' : 's'} —{' '}
          {new Date().toLocaleString()}
        </div>
        {printItems.map((i) => (
          <section key={key(i)} className="mb-4 break-inside-avoid">
            <h2 className="text-lg font-semibold">
              {i.severity ? `[${i.severity.toUpperCase()}] ` : ''}
              {i.title}
            </h2>
            <div className="text-xs text-gray-600 mb-1">
              {i.metaKind} · {i.id}
              {i.target_id ? ` · ${i.target_id}` : ''}
              {i.produced_at ? ` · ${i.produced_at}` : ''}
            </div>
            {i.body && <p className="text-sm">{i.body}</p>}
            {i.derived_from.length > 0 && (
              <div className="text-xs text-gray-600 mt-1">
                Provenance: {i.derived_from.length} upstream row
                {i.derived_from.length === 1 ? '' : 's'}
              </div>
            )}
          </section>
        ))}
        {printItems.length === 0 && <p>No items selected.</p>}
      </div>
    </PanelChrome>
  )
}
