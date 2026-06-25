/**
 * CaseRail — the right-hand casework sidebar for the /case room (v4 Wave 3).
 *
 * A ~280px dark rail that lists the pinned cards, lets the analyst draw typed
 * edges between them (supports / contradicts / derived_from), lists those
 * edges, and exports the whole case ({cards, edges}) as JSON. It is the
 * structured counterpart to the freeform CaseBoard: everything here reads and
 * writes the same orchestrator-owned `useCaseStore`.
 */
import { useMemo, useState } from 'react'
import { Trash2, X, Link2, Download } from 'lucide-react'
import { cn } from '@/lib/cn'
import {
  useCaseStore,
  CASE_KIND_COLOR,
  RELATION_COLOR,
  type CaseRelation,
} from './caseStore'

const RELATIONS: CaseRelation[] = ['supports', 'contradicts', 'derived_from']

/** Human label for a relation (used in the form + edge list). */
function relationLabel(rel: CaseRelation): string {
  return rel.replace('_', ' ')
}

const selectClass = cn(
  'w-full rounded-md border border-slate-800 bg-surface-100 px-2 py-1.5',
  'text-xs text-slate-200',
  'focus:outline-none focus:ring-1 focus:ring-accent-info',
)

export default function CaseRail() {
  const cards = useCaseStore((s) => s.cards)
  const edges = useCaseStore((s) => s.edges)
  const removeCard = useCaseStore((s) => s.removeCard)
  const addEdge = useCaseStore((s) => s.addEdge)
  const removeEdge = useCaseStore((s) => s.removeEdge)
  const clear = useCaseStore((s) => s.clear)

  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [relation, setRelation] = useState<CaseRelation>('supports')

  // Resolve a card id back to its label for the edge list.
  const labelById = useMemo(() => {
    const m = new Map<string, string>()
    for (const c of cards) m.set(c.id, c.label)
    return m
  }, [cards])

  const canAddLink = from !== '' && to !== '' && from !== to

  const handleAddLink = () => {
    if (!canAddLink) return
    addEdge({ from, to, relation })
  }

  const handleClear = () => {
    if (cards.length === 0 && edges.length === 0) return
    if (window.confirm('Clear the entire case (cards and links)? This cannot be undone.')) {
      clear()
    }
  }

  const handleExport = () => {
    const blob = new Blob([JSON.stringify({ cards, edges }, null, 2)], {
      type: 'application/json',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'legba-case.json'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  return (
    <aside className="flex h-full w-[280px] shrink-0 flex-col border-l border-slate-800 bg-surface-200">
      {/* Header */}
      <header className="flex h-10 shrink-0 items-center gap-2 border-b border-slate-800 px-3">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-300">
          Case
        </span>
        <span className="rounded-full bg-surface-50 px-1.5 py-0.5 text-[10px] font-medium tabular-nums text-slate-400">
          {cards.length}
        </span>
        <button
          type="button"
          onClick={handleClear}
          disabled={cards.length === 0 && edges.length === 0}
          title="Clear the case"
          className={cn(
            'ml-auto inline-flex items-center gap-1 rounded-md border border-slate-800 px-2 py-1',
            'text-[11px] text-slate-400 transition-colors',
            'hover:border-accent-critical/60 hover:text-accent-critical',
            'focus:outline-none focus:ring-1 focus:ring-accent-info',
            'disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-slate-800 disabled:hover:text-slate-400',
          )}
        >
          <Trash2 className="h-3 w-3" aria-hidden />
          Clear
        </button>
      </header>

      {/* Card list */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {cards.length === 0 ? (
          <p className="px-3 py-6 text-center text-xs text-slate-500">No cards pinned yet.</p>
        ) : (
          <ul className="py-1">
            {cards.map((card) => (
              <li
                key={card.id}
                className="group flex items-center gap-2 px-3 py-1.5 transition-colors hover:bg-surface-100"
              >
                <span
                  aria-hidden
                  className="h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: CASE_KIND_COLOR[card.kind] }}
                />
                <span className="min-w-0 flex-1 truncate text-xs text-slate-200" title={card.label}>
                  {card.label}
                </span>
                <span className="shrink-0 text-[10px] uppercase tracking-wide text-slate-600">
                  {card.kind}
                </span>
                <button
                  type="button"
                  onClick={() => removeCard(card.id)}
                  title={`Remove "${card.label}"`}
                  aria-label={`Remove "${card.label}"`}
                  className={cn(
                    'shrink-0 rounded p-0.5 text-slate-600 transition-colors',
                    'hover:bg-surface-50 hover:text-accent-critical',
                    'focus:outline-none focus:ring-1 focus:ring-accent-info',
                  )}
                >
                  <X className="h-3.5 w-3.5" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Relate form */}
      <div className="shrink-0 space-y-2 border-t border-slate-800 px-3 py-3">
        <span className="block text-[10px] font-semibold uppercase tracking-wide text-slate-500">
          Relate
        </span>
        <select
          value={from}
          onChange={(e) => setFrom(e.target.value)}
          aria-label="Link source card"
          className={selectClass}
        >
          <option value="">From…</option>
          {cards.map((card) => (
            <option key={card.id} value={card.id}>
              {card.label}
            </option>
          ))}
        </select>
        <select
          value={relation}
          onChange={(e) => setRelation(e.target.value as CaseRelation)}
          aria-label="Relation type"
          className={selectClass}
          style={{ color: RELATION_COLOR[relation] }}
        >
          {RELATIONS.map((rel) => (
            <option key={rel} value={rel} style={{ color: RELATION_COLOR[rel] }}>
              {relationLabel(rel)}
            </option>
          ))}
        </select>
        <select
          value={to}
          onChange={(e) => setTo(e.target.value)}
          aria-label="Link target card"
          className={selectClass}
        >
          <option value="">To…</option>
          {cards.map((card) => (
            <option key={card.id} value={card.id}>
              {card.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={handleAddLink}
          disabled={!canAddLink}
          className={cn(
            'inline-flex w-full items-center justify-center gap-1.5 rounded-md border px-2 py-1.5',
            'text-xs font-medium transition-colors',
            'border-slate-700 bg-surface-100 text-slate-200',
            'hover:border-slate-600 hover:bg-surface-50',
            'focus:outline-none focus:ring-1 focus:ring-accent-info',
            'disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-slate-700 disabled:hover:bg-surface-100',
          )}
        >
          <Link2 className="h-3.5 w-3.5" aria-hidden />
          Add link
        </button>
      </div>

      {/* Edge list */}
      {edges.length > 0 && (
        <ul className="max-h-40 shrink-0 overflow-y-auto border-t border-slate-800 py-1">
          {edges.map((edge) => {
            const fromLabel = labelById.get(edge.from) ?? edge.from
            const toLabel = labelById.get(edge.to) ?? edge.to
            return (
              <li
                key={edge.id}
                className="group flex items-center gap-1.5 px-3 py-1 text-[11px] leading-snug"
              >
                <span className="min-w-0 flex-1 truncate text-slate-300" title={`${fromLabel} ${relationLabel(edge.relation)} ${toLabel}`}>
                  <span className="text-slate-400">{fromLabel}</span>
                  <span className="mx-1 font-mono" style={{ color: RELATION_COLOR[edge.relation] }}>
                    --{relationLabel(edge.relation)}--&gt;
                  </span>
                  <span className="text-slate-400">{toLabel}</span>
                </span>
                <button
                  type="button"
                  onClick={() => removeEdge(edge.id)}
                  title="Remove link"
                  aria-label="Remove link"
                  className={cn(
                    'shrink-0 rounded p-0.5 text-slate-600 transition-colors',
                    'hover:bg-surface-50 hover:text-accent-critical',
                    'focus:outline-none focus:ring-1 focus:ring-accent-info',
                  )}
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              </li>
            )
          })}
        </ul>
      )}

      {/* Export */}
      <div className="shrink-0 border-t border-slate-800 px-3 py-3">
        <button
          type="button"
          onClick={handleExport}
          disabled={cards.length === 0 && edges.length === 0}
          className={cn(
            'inline-flex w-full items-center justify-center gap-1.5 rounded-md border px-2 py-1.5',
            'text-xs font-medium transition-colors',
            'border-slate-800 bg-surface-100 text-slate-300',
            'hover:border-slate-700 hover:text-slate-200',
            'focus:outline-none focus:ring-1 focus:ring-accent-info',
            'disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-slate-800 disabled:hover:text-slate-300',
          )}
        >
          <Download className="h-3.5 w-3.5" aria-hidden />
          Export JSON
        </button>
      </div>
    </aside>
  )
}
