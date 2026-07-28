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
 * renders a preview pane, and print-PDF is `window.print()` over a
 * `print:`-only view of that markdown (the rest of the app is print-hidden).
 *
 * REWORK NOTE (what the old panel body lost): the client-side STIX 2.1 bundle
 * builder (+ per-item indicator SDOs + TLP marking picker) and the
 * severity/kind/target/analyst/since slice-picker over `/findings` +
 * `/situations` are GONE from this flow — STIX is demoted to optional-later
 * (operator decision, program doc §A10; the DOM-free `@/lib/reportModel`
 * machinery stays in the repo, unused by this panel) and selection now flows
 * through the basket instead of a parallel filter surface.
 */

import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { FileDown, Printer, X } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { ApiError, exportCollection, type ExportArtifact } from '@/lib/api'
import { MD_COMPONENTS } from '@/lib/markdownComponents'
import { BASKET_MAX_ITEMS, useExportBasket } from '@/state/exportBasket'
import type { PanelProps } from '@/types'

type ExportFormat = 'markdown' | 'json'

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

const KIND_LABELS: Record<string, string> = {
  finding: 'finding',
  journal_entry: 'journal',
}

export default function ReportExportPanel({ registration }: PanelProps) {
  const items = useExportBasket((s) => s.items)
  const remove = useExportBasket((s) => s.remove)
  const clear = useExportBasket((s) => s.clear)

  const [title, setTitle] = useState('Legba export')
  const [format, setFormat] = useState<ExportFormat>('markdown')
  const [preview, setPreview] = useState<ExportArtifact | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)

  async function doExport() {
    if (items.length === 0 || exporting) return
    setError(null)
    setExporting(true)
    try {
      const artifact = await exportCollection({
        items: items.map((i) => ({ kind: i.kind, id: i.id })),
        format,
        title: title.trim() || null,
      })
      downloadArtifact(artifact.filename, artifact.mime, artifact.content)
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

  function printPdf() {
    // The print-only view (below) renders the composed markdown; the rest of
    // the app is print:hidden. Browser print → "Save as PDF".
    window.print()
  }

  const mdPreview = preview && preview.filename.endsWith('.md') ? preview : null

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${items.length} in basket (max ${BASKET_MAX_ITEMS})`}
    >
      {/* screen-only body (hidden when printing) */}
      <div className="print:hidden flex h-full min-h-0 flex-col text-xs">
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
            onClick={doExport}
            disabled={items.length === 0 || exporting}
            className="flex items-center gap-1 rounded border border-accent-info/50 bg-accent-info/20 px-3 py-1 text-accent-info hover:bg-accent-info/30 disabled:opacity-40"
            data-testid="report-export"
          >
            <FileDown className="h-3 w-3" aria-hidden />
            {exporting ? 'composing…' : 'export'}
          </button>
          <button
            onClick={printPdf}
            disabled={!mdPreview}
            className="flex items-center gap-1 rounded border border-slate-700 bg-surface-200 px-3 py-1 hover:bg-surface-300 disabled:opacity-40"
            title="Print the markdown preview → Save as PDF (export markdown first)"
            data-testid="report-print"
          >
            <Printer className="h-3 w-3" aria-hidden />
            print → PDF
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
              <span className="min-w-0 flex-1 truncate text-slate-200" title={i.id}>
                {i.label ?? i.id}
              </span>
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

      {/* print-only view — the composed markdown, the only thing printed */}
      <div className="hidden print:block text-black" data-testid="report-print-view">
        {mdPreview ? (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{mdPreview.content}</ReactMarkdown>
        ) : (
          <p>No markdown export composed yet.</p>
        )}
      </div>
    </PanelChrome>
  )
}
