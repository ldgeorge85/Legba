/**
 * Command Palette — the Cmd/Ctrl-K RECORD-JUMP gateway (redesign Move 3a).
 *
 * Previously this listed only non-binding singleton panels and *skipped every
 * binding-scoped one* (`if (def.requiresBinding) continue`), so the canonical
 * "follow the data" power-move — jump straight to `target.findings:brazil` —
 * was impossible. It is now the primary way into the app: a single fuzzy query
 * resolves across FOUR indexed families, ranked by smart defaults (recents,
 * then favorites, then the rest):
 *
 *   1. RECORDS  — targets / analysts / sources from the registry. Enter opens
 *      the record's bound primary panel (target→Findings, analyst→Outputs,
 *      source→Detail); ⌘/Ctrl-Enter selects it into the Inspector instead.
 *   2. PANELS   — every singleton panel kind (including the §6 hidden-7, which
 *      stay deep-link-only on the sidebar but are discoverable here, Move 3b).
 *   3. PRESETS  — the curated layout workspaces (Monitoring / Investigation /
 *      Operations …), so a workspace is one fuzzy match away.
 *   4. ACTIONS  — "Investigate" entries that open the bound analysis grid for a
 *      target/analyst (same machinery as the sidebar pickers).
 *
 * A leading dot+chevron favorite toggle persists to localStorage. The modal is
 * intentionally self-contained (no portal) — the app has a single root and the
 * fixed overlay sits above the Dockview workspace.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { Star } from 'lucide-react'
import type { Mode, PanelKind } from '@/types'
import type { SelectionKind } from '@/state/selection'
import { PANEL_REGISTRY, type RegistryEntry } from '@/panel-registry/registry'
import { LAYOUT_PRESETS } from '@/lib/layoutPresets'
import {
  loadFavorites,
  loadRecents,
  pushRecent,
  toggleFavorite,
} from '@/lib/paletteRecents'
import { usePaletteRecords, type PaletteRecord } from '@/components/usePaletteRecords'
import { cn } from '@/lib/cn'

export interface CommandPaletteProps {
  open: boolean
  mode: Mode
  onClose: () => void
  /** Same opener the Sidebar uses; null registration = singleton open. */
  onOpen: (kind: PanelKind, registration: null) => void
  /** Open a binding-scoped panel bound to a record id (target/analyst). */
  onOpenBound: (kind: PanelKind, recordKind: PaletteRecord['recordKind'], id: string) => void
  /** Select a record into the unified selection store → the Inspector. */
  onSelectRecord: (recordKind: SelectionKind, id: string, label: string) => void
  /** Apply a named layout preset (curated workspace). */
  onApplyPreset: (presetId: string) => void
  /** Open the target-scoped analysis grid. */
  onInvestigateTarget: (targetId: string) => void
  /** Open the analyst-scoped analysis grid. */
  onInvestigateAnalyst: (analystId: string) => void
}

type EntryKind = 'panel' | 'record' | 'preset' | 'action'

interface PaletteEntry {
  /** Stable composite key — used for recents/favorites addressing + React keys. */
  key: string
  label: string
  /** Secondary right-aligned hint (category / record kind / state). */
  hint: string
  entryKind: EntryKind
  /** Extra fuzzy-match haystack beyond the label (kind string, ids). */
  search: string
  /** Primary action (Enter). */
  open: () => void
  /** Optional alternate action (⌘/Ctrl-Enter) — e.g. "select into Inspector". */
  alt?: () => void
  /** Hint shown for the alternate action. */
  altHint?: string
}

/** The record family → its bound primary panel (the Enter default for a record). */
export const RECORD_PRIMARY_PANEL: Record<PaletteRecord['recordKind'], PanelKind> = {
  target: 'target.findings',
  analyst: 'analyst.outputs',
  source: 'source.detail',
}

/** Subsequence fuzzy match — every query char appears in order in target. */
export function fuzzyMatch(query: string, target: string): boolean {
  if (!query) return true
  const q = query.toLowerCase()
  const t = target.toLowerCase()
  let qi = 0
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) qi++
  }
  return qi === q.length
}

/**
 * Tiered relevance score for `query` vs `target` (higher = better, 0 = no
 * match). The tiers rank a match by HOW it matched, not just whether — so a
 * query resolves to its best target instead of an arbitrary alphabetical pick:
 *
 *   exact  >  prefix  >  word-boundary  >  contiguous substring  >  subsequence
 *
 * e.g. for "us" a word-boundary hit on `country_g20_us` (…_us) outranks the
 * loose subsequence hit inside "australia"/"russia". Length/position break ties
 * within a tier (shorter / earlier is more relevant). Absolute values only
 * matter for the relative ordering.
 */
export function scoreMatch(query: string, target: string): number {
  if (!query) return 1
  const q = query.toLowerCase()
  const t = target.toLowerCase()
  if (t === q) return 1000
  if (t.startsWith(q)) return 800 - Math.min(t.length, 200)
  for (const word of t.split(/[^a-z0-9]+/)) {
    if (word && word.startsWith(q)) return 600 - Math.min(t.length, 200)
  }
  const idx = t.indexOf(q)
  if (idx >= 0) return 400 - Math.min(idx, 200)
  return fuzzyMatch(q, t) ? 100 : 0
}

/** Singleton panel entries (binding-scoped panels reach the index via records). */
function panelEntries(mode: Mode, onOpen: CommandPaletteProps['onOpen']): PaletteEntry[] {
  const out: PaletteEntry[] = []
  for (const [kind, entry] of Object.entries(PANEL_REGISTRY) as Array<[PanelKind, RegistryEntry]>) {
    const def = entry.definition
    // Binding-scoped panels are reachable through their RECORD (target.findings
    // opens when you pick the brazil target), so we don't list them unbound here
    // — an unbound open would only render a placeholder.
    if (def.requiresBinding) continue
    if (def.modes.length > 0 && !def.modes.includes(mode)) continue
    // NOTE (Move 3b): hidden panels (the §6 DROP set) are NO LONGER skipped —
    // they stay off the sidebar but are discoverable + openable here.
    out.push({
      key: `panel:${kind}`,
      label: def.defaultTitle,
      hint: def.hidden ? `${def.category} · hidden` : def.category,
      entryKind: 'panel',
      search: `${def.defaultTitle} ${kind}`,
      open: () => onOpen(kind, null),
    })
  }
  return out
}

/** Layout-preset (curated workspace) entries. */
function presetEntries(onApplyPreset: CommandPaletteProps['onApplyPreset']): PaletteEntry[] {
  return LAYOUT_PRESETS.map((p) => ({
    key: `preset:${p.id}`,
    label: `Workspace · ${p.label}`,
    hint: 'workspace',
    entryKind: 'preset' as const,
    search: `${p.label} ${p.description} workspace preset layout`,
    open: () => onApplyPreset(p.id),
  }))
}

/** Record entries (targets / analysts / sources). */
function recordEntries(
  records: PaletteRecord[],
  onOpenBound: CommandPaletteProps['onOpenBound'],
  onSelectRecord: CommandPaletteProps['onSelectRecord'],
  onInvestigateTarget: CommandPaletteProps['onInvestigateTarget'],
  onInvestigateAnalyst: CommandPaletteProps['onInvestigateAnalyst'],
): PaletteEntry[] {
  const out: PaletteEntry[] = []
  for (const rec of records) {
    const primaryPanel = RECORD_PRIMARY_PANEL[rec.recordKind]
    // Defensive guard: the record→primary-panel mapping is test-asserted, but a
    // missing binding-scoped panel (target.findings / analyst.outputs /
    // source.detail) must degrade — skip the record, never throw at runtime.
    const primaryDef = PANEL_REGISTRY[primaryPanel]?.definition
    if (!primaryDef) continue
    const stateHint = rec.state && rec.state !== 'active' ? ` · ${rec.state}` : ''
    out.push({
      key: `record:${rec.recordKind}:${rec.id}`,
      label: rec.label === rec.id ? rec.id : `${rec.label} · ${rec.id}`,
      hint: `${rec.recordKind}${stateHint}`,
      entryKind: 'record',
      search: `${rec.label} ${rec.id} ${rec.recordKind} ${primaryDef.defaultTitle}`,
      // Enter → SET THE DESK SELECTION (the keystone): the feed, map, and
      // Inspector all follow the shared selection, so "pick a country → see its
      // findings" works from the palette without opening a placeholder panel.
      open: () => onSelectRecord(rec.recordKind, rec.id, rec.label),
      // ⌘/Ctrl-Enter → open the record's bound primary panel (grid) instead.
      alt: () => onOpenBound(primaryPanel, rec.recordKind, rec.id),
      altHint: 'open panel',
    })
    // Targets + analysts additionally get an "Investigate" grid action.
    if (rec.recordKind === 'target') {
      out.push({
        key: `action:investigate-target:${rec.id}`,
        label: `Investigate · ${rec.id}`,
        hint: 'grid',
        entryKind: 'action',
        search: `investigate target grid ${rec.label} ${rec.id}`,
        open: () => onInvestigateTarget(rec.id),
      })
    } else if (rec.recordKind === 'analyst') {
      out.push({
        key: `action:investigate-analyst:${rec.id}`,
        label: `Investigate · ${rec.id}`,
        hint: 'grid',
        entryKind: 'action',
        search: `investigate analyst grid ${rec.label} ${rec.id}`,
        open: () => onInvestigateAnalyst(rec.id),
      })
    }
  }
  return out
}

export function CommandPalette({
  open,
  mode,
  onClose,
  onOpen,
  onOpenBound,
  onSelectRecord,
  onApplyPreset,
  onInvestigateTarget,
  onInvestigateAnalyst,
}: CommandPaletteProps) {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const [favorites, setFavorites] = useState<Set<string>>(() => loadFavorites())
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)

  // Records are fetched only while the palette is open (lazy index).
  const { records } = usePaletteRecords(open)

  // The full index: panels ∪ records ∪ presets ∪ investigate actions.
  const allEntries = useMemo(
    () => [
      ...recordEntries(
        records,
        onOpenBound,
        onSelectRecord,
        onInvestigateTarget,
        onInvestigateAnalyst,
      ),
      ...panelEntries(mode, onOpen),
      ...presetEntries(onApplyPreset),
    ],
    [
      records,
      mode,
      onOpen,
      onOpenBound,
      onSelectRecord,
      onApplyPreset,
      onInvestigateTarget,
      onInvestigateAnalyst,
    ],
  )

  // Smart-default ranking: with no query, recents first, then favorites, then
  // the rest — never an empty alphabetical wall. With a query, fuzzy-filter then
  // float favorites to the top of the matches.
  const filtered = useMemo(() => {
    if (!query) {
      const recents = loadRecents()
      const recentRank = new Map(recents.map((id, i) => [id, i]))
      return allEntries.slice().sort((a, b) => {
        const ra = recentRank.has(a.key) ? recentRank.get(a.key)! : Infinity
        const rb = recentRank.has(b.key) ? recentRank.get(b.key)! : Infinity
        if (ra !== rb) return ra - rb
        const fa = favorites.has(a.key) ? 0 : 1
        const fb = favorites.has(b.key) ? 0 : 1
        if (fa !== fb) return fa - fb
        return a.label.localeCompare(b.label)
      })
    }
    // Scored match: rank by HOW each entry matched (exact > prefix > word-
    // boundary > substring > subsequence), best of the label vs the search
    // haystack. Ties break on favorites, then label.
    const scored = allEntries
      .map((e) => ({ e, score: Math.max(scoreMatch(query, e.label), scoreMatch(query, e.search)) }))
      .filter((x) => x.score > 0)
    scored.sort((a, b) => {
      if (a.score !== b.score) return b.score - a.score
      const fa = favorites.has(a.e.key) ? 0 : 1
      const fb = favorites.has(b.e.key) ? 0 : 1
      if (fa !== fb) return fa - fb
      return a.e.label.localeCompare(b.e.label)
    })
    return scored.map((x) => x.e)
  }, [allEntries, query, favorites])

  // Reset transient state each time the palette opens, and focus the input.
  useEffect(() => {
    if (!open) return
    setQuery('')
    setActive(0)
    setFavorites(loadFavorites())
    const id = window.requestAnimationFrame(() => inputRef.current?.focus())
    return () => window.cancelAnimationFrame(id)
  }, [open])

  // Keep the active index in range as the filtered list shrinks.
  useEffect(() => {
    setActive((a) => (a >= filtered.length ? Math.max(0, filtered.length - 1) : a))
  }, [filtered.length])

  // Scroll the active row into view.
  useEffect(() => {
    if (!open) return
    const el = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [active, open])

  if (!open) return null

  function choose(entry: PaletteEntry | undefined, useAlt: boolean) {
    if (!entry) return
    pushRecent(entry.key)
    if (useAlt && entry.alt) entry.alt()
    else entry.open()
    onClose()
  }

  function onToggleFavorite(entry: PaletteEntry, e: React.MouseEvent) {
    e.stopPropagation()
    toggleFavorite(entry.key)
    setFavorites(loadFavorites())
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((a) => Math.min(filtered.length - 1, a + 1))
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((a) => Math.max(0, a - 1))
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault()
      choose(filtered[active], e.metaKey || e.ctrlKey)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[12vh] bg-black/50"
      role="presentation"
      data-testid="command-palette-backdrop"
      onMouseDown={(e) => {
        // Backdrop click (not a click inside the panel) closes.
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        role="dialog"
        aria-label="Command palette"
        aria-modal="true"
        data-testid="command-palette"
        className="w-[34rem] max-w-[90vw] rounded-lg border border-line bg-surf-base shadow-2xl overflow-hidden"
        onKeyDown={onKeyDown}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value)
            setActive(0)
          }}
          placeholder="Jump to a target, analyst, source, panel, or workspace…"
          aria-label="Search records, panels, and workspaces"
          data-testid="command-palette-input"
          className="w-full bg-surf-1 px-4 py-3 text-body text-ink-1 placeholder:text-ink-3 outline-none border-b border-line"
        />
        <ul ref={listRef} className="max-h-[26rem] overflow-y-auto py-1" data-testid="command-palette-list">
          {filtered.length === 0 && (
            <li className="px-4 py-3 text-label text-ink-3" data-testid="command-palette-empty">
              No matches for “{query}”.
            </li>
          )}
          {filtered.map((entry, i) => (
            <li
              key={entry.key}
              data-index={i}
              className={cn(
                'group flex items-center',
                i === active ? 'bg-surf-2 text-ink-1' : 'text-ink-2 hover:bg-surf-2/60',
              )}
            >
              {/* Favorite toggle — a real <button> (keyboard-focusable + Enter/
                  Space activatable, WCAG 2.1.1), kept as a SIBLING of the row
                  button so we never nest interactive controls (invalid HTML). */}
              <button
                type="button"
                aria-label={favorites.has(entry.key) ? 'Unfavorite' : 'Favorite'}
                aria-pressed={favorites.has(entry.key)}
                data-testid={`command-palette-fav-${entry.key}`}
                onClick={(e) => onToggleFavorite(entry, e)}
                className={cn(
                  'shrink-0 ml-3 rounded p-0.5 focus:outline-none focus-visible:ring-1 focus-visible:ring-[var(--accent)]',
                  favorites.has(entry.key)
                    ? 'text-amber-300'
                    : 'text-ink-3 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:text-ink-1',
                )}
              >
                <Star size={12} fill={favorites.has(entry.key) ? 'currentColor' : 'none'} />
              </button>
              <button
                type="button"
                data-testid={`command-palette-item-${entry.key}`}
                data-entry-kind={entry.entryKind}
                onMouseEnter={() => setActive(i)}
                onClick={(e) => choose(entry, e.metaKey || e.ctrlKey)}
                className="flex flex-1 min-w-0 items-center gap-3 py-2 pl-2 pr-4 text-left text-body"
              >
                <span className="flex-1 truncate">{entry.label}</span>
                <span className="shrink-0 text-label uppercase tracking-wider text-ink-3">
                  {entry.hint}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <div className="flex items-center justify-between px-4 py-2 text-label text-ink-3 border-t border-line">
          <span>↑↓ navigate · ↵ open/select · ⌘↵ panel · ★ favorite · esc close</span>
          <span>
            {filtered.length} result{filtered.length === 1 ? '' : 's'}
          </span>
        </div>
      </div>
    </div>
  )
}
