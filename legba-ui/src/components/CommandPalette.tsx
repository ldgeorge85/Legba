import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useWorkspaceStore, type PanelType } from '@/stores/workspace'
import { LAYOUT_PRESETS } from '@/layouts/presets'
import {
  loadCustomLayouts,
  saveCustomLayout,
} from '@/layouts/presets'

// ── Result types ──

type ResultKind = 'panel' | 'preset' | 'custom-layout' | 'action'

interface PaletteResult {
  id: string
  label: string
  kind: ResultKind
  description?: string
}

// ── Static panel list (mirrors Sidebar navItems) ──

const PANEL_ITEMS: { type: PanelType; label: string }[] = [
  { type: 'dashboard', label: 'Dashboard' },
  { type: 'situations', label: 'Situations' },
  { type: 'event-stream', label: 'Live Feed' },
  { type: 'map', label: 'Map' },
  { type: 'events', label: 'Events' },
  { type: 'entities', label: 'Entities' },
  { type: 'graph', label: 'Knowledge Graph' },
  { type: 'timeline', label: 'Timeline' },
  { type: 'signals', label: 'Signals' },
  { type: 'hypotheses', label: 'Hypotheses' },
  { type: 'consult', label: 'Consult' },
  { type: 'facts', label: 'Facts' },
  { type: 'analytics', label: 'Analytics' },
  { type: 'briefs', label: 'Situation Briefs' },
  { type: 'reports', label: 'Reports' },
  { type: 'watchlist', label: 'Watchlist' },
  { type: 'goals', label: 'Goals' },
  { type: 'sources', label: 'Sources' },
  { type: 'cycle-monitor', label: 'Cycle Monitor' },
  { type: 'scorecard', label: 'Scorecard' },
  { type: 'proposed-edges', label: 'Edge Queue' },
  { type: 'config', label: 'Config Editor' },
]

// ── Actions ──

const ACTIONS: { id: string; label: string; description: string }[] = [
  { id: 'save-layout', label: 'Save Layout...', description: 'Save current workspace layout' },
]

// ── Component ──

export function CommandPalette() {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [selectedIndex, setSelectedIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const { openPanel, applyPreset, restoreCustomLayout, saveCurrentLayout } =
    useWorkspaceStore()

  // Build the full result set
  const allResults = useMemo<PaletteResult[]>(() => {
    const results: PaletteResult[] = []

    // Panels
    for (const p of PANEL_ITEMS) {
      results.push({
        id: `panel:${p.type}`,
        label: p.label,
        kind: 'panel',
        description: 'Open panel',
      })
    }

    // Layout presets
    for (const preset of LAYOUT_PRESETS) {
      results.push({
        id: `preset:${preset.name}`,
        label: preset.label,
        kind: 'preset',
        description: 'Layout preset',
      })
    }

    // Custom layouts
    const custom = loadCustomLayouts()
    for (const name of Object.keys(custom)) {
      results.push({
        id: `custom:${name}`,
        label: name,
        kind: 'custom-layout',
        description: 'Custom layout',
      })
    }

    // Actions
    for (const a of ACTIONS) {
      results.push({
        id: `action:${a.id}`,
        label: a.label,
        kind: 'action',
        description: a.description,
      })
    }

    return results
  }, [open]) // Re-build when palette opens (custom layouts may change)

  // Filtered results
  const filtered = useMemo(() => {
    if (!query.trim()) return allResults.slice(0, 10)
    const q = query.toLowerCase()
    return allResults
      .filter((r) => r.label.toLowerCase().includes(q))
      .slice(0, 10)
  }, [query, allResults])

  // Reset selection when results change
  useEffect(() => {
    setSelectedIndex(0)
  }, [filtered])

  // Focus input when opening
  useEffect(() => {
    if (open) {
      setQuery('')
      setSelectedIndex(0)
      // Small delay to ensure DOM is ready
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [open])

  // Global keyboard listener
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault()
        setOpen((v) => !v)
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])

  // Execute a result
  const execute = useCallback(
    (result: PaletteResult) => {
      setOpen(false)

      if (result.kind === 'panel') {
        const panelType = result.id.replace('panel:', '') as PanelType
        openPanel(panelType)
      } else if (result.kind === 'preset') {
        const presetName = result.id.replace('preset:', '')
        const preset = LAYOUT_PRESETS.find((p) => p.name === presetName)
        if (preset) applyPreset(preset.panels)
      } else if (result.kind === 'custom-layout') {
        const name = result.id.replace('custom:', '')
        const layouts = loadCustomLayouts()
        if (layouts[name]) restoreCustomLayout(layouts[name])
      } else if (result.kind === 'action') {
        const actionId = result.id.replace('action:', '')
        if (actionId === 'save-layout') {
          const name = prompt('Layout name:')
          if (name?.trim()) {
            const json = saveCurrentLayout()
            if (json) saveCustomLayout(name.trim(), json)
          }
        }
      }
    },
    [openPanel, applyPreset, restoreCustomLayout, saveCurrentLayout],
  )

  // Handle keyboard navigation in the list
  const onInputKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false)
        return
      }
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelectedIndex((i) => Math.min(i + 1, filtered.length - 1))
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelectedIndex((i) => Math.max(i - 1, 0))
        return
      }
      if (e.key === 'Enter') {
        e.preventDefault()
        if (filtered[selectedIndex]) {
          execute(filtered[selectedIndex])
        }
        return
      }
    },
    [filtered, selectedIndex, execute],
  )

  // Scroll selected item into view
  useEffect(() => {
    if (!listRef.current) return
    const items = listRef.current.querySelectorAll('[data-palette-item]')
    items[selectedIndex]?.scrollIntoView({ block: 'nearest' })
  }, [selectedIndex])

  if (!open) return null

  const kindLabel: Record<ResultKind, string> = {
    panel: 'Panel',
    preset: 'Layout',
    'custom-layout': 'Custom',
    action: 'Action',
  }

  const kindColor: Record<ResultKind, string> = {
    panel: 'text-blue-400',
    preset: 'text-amber-400',
    'custom-layout': 'text-emerald-400',
    action: 'text-purple-400',
  }

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-start justify-center pt-[15vh]"
      onClick={() => setOpen(false)}
    >
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/50" />

      {/* Palette */}
      <div
        className="relative w-full max-w-lg rounded-lg border border-border bg-popover shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Input */}
        <div className="flex items-center border-b border-border px-3">
          <span className="text-muted-foreground text-sm mr-2">&gt;</span>
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onInputKeyDown}
            placeholder="Type to search panels, layouts, actions..."
            className="flex-1 bg-transparent py-3 text-sm text-foreground placeholder:text-muted-foreground outline-none"
          />
          <kbd className="ml-2 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">
            ESC
          </kbd>
        </div>

        {/* Results */}
        <div ref={listRef} className="max-h-64 overflow-y-auto py-1">
          {filtered.length === 0 && (
            <div className="px-3 py-6 text-center text-sm text-muted-foreground">
              No results found
            </div>
          )}
          {filtered.map((result, idx) => (
            <button
              key={result.id}
              data-palette-item
              onClick={() => execute(result)}
              onMouseEnter={() => setSelectedIndex(idx)}
              className={`flex items-center gap-2 w-full px-3 py-2 text-sm text-left transition-colors ${
                idx === selectedIndex
                  ? 'bg-secondary text-foreground'
                  : 'text-muted-foreground hover:bg-secondary/50'
              }`}
            >
              <span className="flex-1">{result.label}</span>
              <span className={`text-[10px] uppercase tracking-wider ${kindColor[result.kind]}`}>
                {kindLabel[result.kind]}
              </span>
            </button>
          ))}
        </div>

        {/* Footer hint */}
        <div className="flex items-center gap-3 border-t border-border px-3 py-1.5 text-[10px] text-muted-foreground">
          <span>
            <kbd className="rounded border border-border px-1 py-0.5 mr-0.5">↑</kbd>
            <kbd className="rounded border border-border px-1 py-0.5">↓</kbd> navigate
          </span>
          <span>
            <kbd className="rounded border border-border px-1 py-0.5">Enter</kbd> select
          </span>
          <span>
            <kbd className="rounded border border-border px-1 py-0.5">Esc</kbd> close
          </span>
        </div>
      </div>
    </div>
  )
}
