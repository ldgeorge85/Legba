/**
 * A10 — Report Export (`system.report_export`): the collection-basket export
 * surface.
 *
 * The operator collects findings / analyst reports / journal entries into the
 * persistent export basket (`@/state/exportBasket`) from wherever selection
 * already flows — the Inspector's "add to export" button, the feed row hover
 * action, a Journal entry card — then composes here: basket list (removable),
 * document title, markdown/JSON format toggle, Export.
 *
 * Composition is SERVER-SIDE (`POST /api/v1/v3/export`, registry
 * `export_api.py`): the route resolves each finding's citations to live signal
 * titles + canonical_urls, folds the verify state (faithfulness or an explicit
 * `unverified — <reason>`), stamps the lineage receipt link, and frames
 * journal entries with their tier label + the reflective off-product-chain
 * VOICE note. The panel downloads what the server composed; markdown also
 * renders a preview pane.
 *
 * "Print / save as PDF" (7b-iii) hands the composed markdown to
 * `@/lib/printDocument`, which builds a DEDICATED, self-contained print
 * document — running header with the title and the export's own as-of, A4/
 * Letter margins, citations as numbered endnotes, a page break before the
 * appendix — and prints it from a hidden iframe. Printing a frame prints the
 * frame's document alone, so no workstation chrome reaches the page and no
 * route is added. Enabled only once a MARKDOWN export exists: there is
 * nothing composed to print before that, and a print view is never allowed to
 * invent one.
 *
 * REWORK NOTE (what the old panel body lost): the client-side STIX 2.1 bundle
 * builder (+ per-item indicator SDOs + TLP marking picker) and the
 * severity/kind/target/analyst/since slice-picker over `/findings` +
 * `/situations` are GONE from this flow — STIX is demoted to optional-later
 * (operator decision, program doc §A10; the DOM-free `@/lib/reportModel`
 * machinery stays in the repo, unused by this panel) and selection now flows
 * through the basket instead of a parallel filter surface.
 *
 * "Desk brief" (7b-iii) is a one-click shortcut over the same flow: it calls
 * `@/state/exportBasket:buildDeskBrief` against the CURRENT SCOPE's single
 * desk to fill the basket (composition, then each unit's latest admitted
 * read, in the composition's own declared order) and exports it through this
 * panel's own `doExport`, carrying the desk's open situations/tracked events
 * as the export route's `appendix` field. Disabled until the wall is scoped
 * to exactly one desk (a Navigator report click or a desk click).
 */

import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { FileDown, Newspaper, Printer, X } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { CopyableId } from '@/components/CopyableId'
import { ApiError, downloadExportArtifact, exportCollection, type ExportArtifact } from '@/lib/api'
import { MD_COMPONENTS } from '@/lib/markdownComponents'
import { printExportDocument } from '@/lib/printDocument'
import {
  BASKET_MAX_ITEMS,
  buildDeskBrief,
  deskBriefAppendix,
  useExportBasket,
  type DeskBriefAppendixPayload,
} from '@/state/exportBasket'
import { useScope } from '@/state/scope'
import type { PanelProps } from '@/types'

type ExportFormat = 'markdown' | 'json'

const KIND_LABELS: Record<string, string> = {
  finding: 'finding',
  journal_entry: 'journal',
}

export default function ReportExportPanel({ registration }: PanelProps) {
  const items = useExportBasket((s) => s.items)
  const remove = useExportBasket((s) => s.remove)
  const clear = useExportBasket((s) => s.clear)
  const scope = useScope((s) => s.scope)
  // The desk a "Desk brief" click targets — the SAME single-target derivation
  // `state/scope.ts:scopeParams` uses for `/findings?target_id=`: a bare desk
  // scope or a composition ('report') scope both carry exactly one target id
  // on every block, so this covers a Navigator composition click too.
  const deskBriefTargetId =
    scope && scope.members.targetIds.length === 1 ? scope.members.targetIds[0] : null

  const [title, setTitle] = useState('Legba export')
  const [format, setFormat] = useState<ExportFormat>('markdown')
  const [preview, setPreview] = useState<ExportArtifact | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [briefing, setBriefing] = useState(false)

  async function doExport(
    opts: {
      appendix?: string | DeskBriefAppendixPayload | null
      title?: string
    } = {},
  ) {
    // Read the basket FRESH rather than off the subscribed `items` — a
    // `doExport` called right after `buildDeskBrief` fills the basket would
    // otherwise see this render's stale (pre-brief) closure.
    const basketItems = useExportBasket.getState().items
    if (basketItems.length === 0 || exporting) return
    setError(null)
    setExporting(true)
    try {
      const artifact = await exportCollection({
        items: basketItems.map((i) => ({ kind: i.kind, id: i.id })),
        format,
        title: (opts.title ?? title).trim() || null,
        appendix: opts.appendix ?? undefined,
      })
      downloadExportArtifact(artifact)
      setPreview(artifact)
    } catch (e) {
      if (e instanceof ApiError) {
        const detail =
          e.body && typeof e.body === 'object' && 'detail' in e.body
            ? String((e.body as { detail: unknown }).detail)
            : e.message
        setError(`export failed (${e.status}): ${detail}`)
      } else {
        setError(`export failed: ${e instanceof Error ? e.message : String(e)}`)
      }
    } finally {
      setExporting(false)
    }
  }

  /**
   * A10/7b-iii — one click: fill the basket with the scoped desk's current
   * composition + each unit's latest admitted read (in the composition's own
   * declared order), then export straight through the SAME `doExport` path,
   * carrying the desk's open situations/tracked events as the appendix and
   * asking the route for the desk's typed absence beside them (k5b).
   */
  async function doDeskBrief() {
    if (!deskBriefTargetId || exporting || briefing) return
    setError(null)
    setBriefing(true)
    try {
      const brief = await buildDeskBrief(deskBriefTargetId)
      await doExport({
        // k5b — the situations markdown AND the ask for this desk's typed
        // absence, composed server-side so the printed brief and the JSON
        // carry the same block.
        appendix: deskBriefAppendix(deskBriefTargetId, brief.appendix),
        title: `Desk brief — ${scope?.label ?? deskBriefTargetId}`,
      })
    } catch (e) {
      setError(`desk brief failed: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBriefing(false)
    }
  }

  const mdPreview = preview && preview.filename.endsWith('.md') ? preview : null

  function printPdf() {
    // The composed markdown, re-laid-out into its own print document and
    // printed from a hidden frame — never a print-styled workstation. No
    // markdown composed yet means nothing to print; the button is disabled.
    if (!mdPreview) return
    setError(null)
    // No title override: the printed header must name the document that is
    // actually on the page, which is the `# ` heading the export composed —
    // not whatever the title field holds after a later edit.
    const opened = printExportDocument({
      markdown: mdPreview.content,
      filename: mdPreview.filename,
      printedAt: new Date().toISOString(),
    })
    if (!opened) {
      setError(
        'print failed: the browser refused a print frame — the downloaded ' +
          'markdown file carries the same document',
      )
    }
  }

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${items.length} in basket (max ${BASKET_MAX_ITEMS})`}
    >
      {/* The panel body. Printing goes through its OWN document in a hidden
          frame (`lib/printDocument`), so this surface is never the printed
          page and needs no print-variant classes. */}
      <div className="flex h-full min-h-0 flex-col text-xs">
        {/* compose controls */}
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <input
            className="min-w-[160px] flex-1 rounded border border-slate-700 bg-surface-200 p-1 px-2"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="document title…"
            data-testid="report-title"
          />
          <div
            className="inline-flex overflow-hidden rounded border border-slate-700"
            role="group"
            aria-label="export format"
          >
            <button
              className={`px-2 py-1 ${format === 'markdown' ? 'bg-surface-300 text-slate-100' : 'text-slate-400 hover:text-slate-200'}`}
              onClick={() => setFormat('markdown')}
              data-testid="report-format-markdown"
            >
              markdown
            </button>
            <button
              className={`border-l border-slate-700 px-2 py-1 ${format === 'json' ? 'bg-surface-300 text-slate-100' : 'text-slate-400 hover:text-slate-200'}`}
              onClick={() => setFormat('json')}
              data-testid="report-format-json"
            >
              json
            </button>
          </div>
          <button
            onClick={() => doExport()}
            disabled={items.length === 0 || exporting}
            className="flex items-center gap-1 rounded border border-accent-info/50 bg-accent-info/20 px-3 py-1 text-accent-info hover:bg-accent-info/30 disabled:opacity-40"
            data-testid="report-export"
          >
            <FileDown className="h-3 w-3" aria-hidden />
            {exporting ? 'composing…' : 'export'}
          </button>
          <button
            onClick={doDeskBrief}
            disabled={!deskBriefTargetId || exporting || briefing}
            className="flex items-center gap-1 rounded border border-slate-700 bg-surface-200 px-3 py-1 hover:bg-surface-300 disabled:opacity-40"
            title={
              deskBriefTargetId
                ? `Fill the basket with ${scope?.label ?? deskBriefTargetId}'s current read and export it`
                : 'Scope the wall to one desk first (a Navigator report or a desk click)'
            }
            data-testid="desk-brief-button"
          >
            <Newspaper className="h-3 w-3" aria-hidden />
            {briefing ? 'briefing…' : 'desk brief'}
          </button>
          <button
            type="button"
            onClick={printPdf}
            disabled={!mdPreview}
            className="flex items-center gap-1 rounded border border-slate-700 bg-surface-200 px-3 py-1 hover:bg-surface-300 disabled:opacity-40"
            title={
              mdPreview
                ? 'Print the composed document (citations as numbered endnotes) → Save as PDF'
                : 'Export a markdown document first — there is nothing composed to print'
            }
            data-testid="report-print"
          >
            <Printer className="h-3 w-3" aria-hidden />
            print / save as PDF
          </button>
          {items.length > 0 && (
            <button
              onClick={clear}
              className="text-slate-400 underline hover:text-slate-200"
              data-testid="report-clear"
            >
              clear basket
            </button>
          )}
        </div>

        {error && (
          <div className="mb-2 text-rose-400" data-testid="report-error">
            {error}
          </div>
        )}

        {/* basket list — removable rows */}
        <div
          className="mb-2 min-h-0 flex-1 space-y-1 overflow-y-auto"
          data-testid="report-basket"
        >
          {items.map((i) => (
            <div
              key={`${i.kind}:${i.id}`}
              className="flex items-center gap-2 rounded border border-slate-800 bg-surface-100 p-1.5"
              data-testid={`report-basket-item-${i.kind}-${i.id}`}
            >
              <span className="shrink-0 rounded bg-slate-700 px-1 text-slate-200">
                {KIND_LABELS[i.kind] ?? i.kind}
              </span>
              {i.label ? (
                <span className="min-w-0 flex-1 truncate text-slate-200" title={i.id}>
                  {i.label}
                </span>
              ) : (
                // U-5 — no label to show: a raw record id is a UUID in most
                // cases; truncate + offer a copy affordance rather than
                // spilling the full id into the basket row.
                <CopyableId id={i.id} className="min-w-0 flex-1 text-slate-200" />
              )}
              <button
                onClick={() => remove(i.kind, i.id)}
                className="shrink-0 text-slate-500 hover:text-slate-200"
                title="remove from basket"
                aria-label="remove from basket"
                data-testid={`report-basket-remove-${i.kind}-${i.id}`}
              >
                <X className="h-3 w-3" aria-hidden />
              </button>
            </div>
          ))}
          {items.length === 0 && (
            <div className="py-4 text-center text-slate-500" data-testid="report-basket-empty">
              Basket is empty. Add items from the Inspector (&ldquo;add to
              export&rdquo; on a selected finding), a feed row&rsquo;s + action,
              or a Journal entry card — then export them here as one markdown or
              JSON document.
            </div>
          )}
        </div>

        {/* preview — rendered markdown, or the raw JSON document */}
        {preview && (
          <div className="flex min-h-0 flex-1 flex-col border-t border-slate-800 pt-2">
            <div className="mb-1 text-[11px] text-slate-500">
              preview · <span className="font-mono text-slate-400">{preview.filename}</span>{' '}
              (downloaded)
            </div>
            <div
              className="flex-1 overflow-auto rounded border border-slate-800 bg-surface-50 p-2"
              data-testid="report-preview"
            >
              {mdPreview ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                  {mdPreview.content}
                </ReactMarkdown>
              ) : (
                <pre className="whitespace-pre-wrap break-words text-[11px] text-slate-300">
                  {preview.content}
                </pre>
              )}
            </div>
          </div>
        )}
      </div>
    </PanelChrome>
  )
}
