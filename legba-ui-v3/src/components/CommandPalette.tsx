/**
 * Command Palette — the Cmd/Ctrl-K COMMAND LINE (redesign Move 3a; UI-B1/shell).
 *
 * One fuzzy query resolves across every family, ranked by smart defaults
 * (recents, then favorites, then the rest). EVERY family compiles to drive
 * actions (`lib/commands.ts::paletteActions`) and runs through the ONE
 * executor (`drive` → `applyAction`) — the palette holds no opener of its own:
 *
 *   1. RECORDS    — subjects / units / sources. A subject's Enter opens its
 *                   read (scope + focus + its page), ⌘/Ctrl-Enter focuses it;
 *                   a unit's or source's Enter → `focus`, ⌘/Ctrl-Enter →
 *                   `open_panel` on the record's bound primary.
 *   2. PANELS     — the `＋` catalog: every kind that renders on its own (a
 *                   DRILL needs a ref and opens from its parent object). On
 *                   the reader side only the reader's views are listed here;
 *                   anything else is a search away and crosses to the
 *                   operator side when chosen.
 *   3. SURFACES   — Board · Subject · Ledger · Watch and the operator
 *                   sections → `arrange` (U-2 shell; the workspaces, missions
 *                   and presets families are gone with the arrangement cut).
 *   4. CASES      — every consult turn in the store that presented a case →
 *                   a replay through the case player (UI-D1 §3).
 *   5. ACTIONS    — "Investigate · X" → `focus` + the record's reads.
 *   6. COMMANDS   — the SHELL family this shell carries (reset the surface,
 *                   copy a link, density, close the opened view) — never
 *                   recorded.
 *
 * A leading dot+chevron favorite toggle persists to localStorage.
 *
 * THE ROW MODEL (X3/D3; review D §C4, "⌘K opens on 465 alphabetical rows
 * starting with '38 North' and paused sources"). Every row carries a FAMILY,
 * and the empty palette is ordered by it — after the operator's own recents
 * and favorites — never alphabetically across families:
 *
 *   subjects (by recent activity: the newest read on record, newest first;
 *   a subject with no read on record after them) → panels → surfaces →
 *   cases → commands → units → Investigate → sources →
 *   records not active (a draft subject, a paused unit) → PAUSED sources last.
 *
 * A subject is called by the profile's noun (`lib/subjectRead.ts`), never
 * "target"; its Enter opens its READ (the same three actions as a rail
 * click), ⌘Enter only focuses it (the Inspector). A source that is not active
 * says so in its hint ("source · paused") and sorts last under a query too.
 *
 * THE MODAL (UI-B1/tokens): the overlay is the `Dialog` primitive
 * (`src/ui/dialog.tsx`, Radix underneath), replacing a hand-rolled
 * `role="dialog"` div. The dialog owns the modal half of the key handling —
 * Escape closes, focus moves into the search box on open and is TRAPPED (Tab
 * cycles inside the palette, which the hand-rolled one never did), focus
 * returns to the opener on close, a press on the backdrop closes — and it
 * portals into the shell's overlay host. The palette keeps only the LIST keys
 * (↑ / ↓ move, Enter opens, ⌘/Ctrl-Enter the alternate action). The families,
 * the ranking and every test id are unchanged.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { Star } from 'lucide-react'
import type { Mode, PanelKind } from '@/types'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { catalogKinds } from '@/panel-registry/kindPlan'
import { SURFACES, readerKinds, type Side } from '@/lib/surfaces'
import { useBelowContributor } from '@/auth/roles'
import { drive, type WorkstationAction } from '@/lib/applyAction'
import {
  INVESTIGATE_KINDS,
  availableShellCommands,
  paletteActions,
  runCommand,
} from '@/lib/commands'
import { recordedCases, replayRecordedCase } from '@/lib/caseReplay'
import { useConsultSessions } from '@/state/consultSession'
import {
  loadFavorites,
  loadRecents,
  pushRecent,
  toggleFavorite,
} from '@/lib/paletteRecents'
import { usePaletteRecords, type PaletteRecord } from '@/components/usePaletteRecords'
import { useCountryVerdicts } from '@/v4/world/countryVerdicts'
import { useSupplyChainDesks } from '@/v4/world/supplyChainDesks'
import { subjectName, useSubjectIndex, type SubjectIndex } from '@/lib/deskNames'
import { relativeTime } from '@/lib/findingsViews'
import { PROFILE_UI } from '@/lib/profileUi'
import { subjectReadActions } from '@/lib/subjectRead'
import { selectionKindLabel, SUBJECT_NOUN } from '@/lib/subjectNoun'
import { cn } from '@/lib/cn'
import { Dialog, DialogContent, DialogTitle } from '@/ui/dialog'

export interface CommandPaletteProps {
  open: boolean
  mode: Mode
  onClose: () => void
  /** The side the reader stands on: the reader side's panel family lists only reader views. */
  side?: Side
  /**
   * Where the compiled actions go. Defaults to `drive` (the ONE executor via
   * the registered shell); a test passes a recorder.
   */
  onDrive?: (actions: WorkstationAction[]) => void
}

type EntryKind = 'panel' | 'record' | 'action' | 'surface' | 'case' | 'command'

/**
 * Prefix namespacing (design §3.3) — VS Code's mechanism for getting depth out
 * of a flat list without a nested menu: a leading sigil narrows the index to
 * one family before the fuzzy match runs.
 *
 *   `#` → the surfaces (where am I working)
 *   `>` → panels and shell commands ("what can I open / do")
 *
 * The design's other two rows (`/` the substrate, `@` entities) wait on the
 * search fan-out moving under the palette; unprefixed search is unchanged and
 * still spans every family.
 */
const PREFIX_FAMILIES: Record<string, ReadonlySet<EntryKind>> = {
  '#': new Set<EntryKind>(['surface']),
  '>': new Set<EntryKind>(['panel', 'command']),
}

/** Split a leading namespace sigil off the query. */
export function parsePalettePrefix(query: string): { prefix: string | null; rest: string } {
  const head = query.slice(0, 1)
  if (head in PREFIX_FAMILIES) return { prefix: head, rest: query.slice(1).trimStart() }
  return { prefix: null, rest: query }
}

/**
 * The row FAMILY — what the empty palette orders by (see the header). Lower
 * ranks first; `source_paused` is always last, under a query too.
 */
export type PaletteFamily =
  | 'subject'
  | 'panel'
  | 'surface'
  | 'case'
  | 'command'
  | 'unit'
  | 'action'
  | 'source'
  | 'inactive'
  | 'source_paused'

export const PALETTE_FAMILY_ORDER: readonly PaletteFamily[] = [
  'subject',
  'panel',
  'surface',
  'case',
  'command',
  'unit',
  'action',
  'source',
  'inactive',
  'source_paused',
]

const FAMILY_RANK = new Map(PALETTE_FAMILY_ORDER.map((f, i) => [f, i]))

export interface PaletteEntry {
  /** Stable composite key — used for recents/favorites addressing + React keys. */
  key: string
  /** The row family (ordering). */
  family: PaletteFamily
  /** A subject's newest read on record (epoch ms), or null — never invented. */
  activity?: number | null
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
  // UI-B3 — a subject's bound primary surface is its page (Brief tab).
  target: 'subject.page',
  analyst: 'analyst.outputs',
  // `source.detail` retired onto the Registry's Sources tab (UI-B1/merges); a
  // source record's Enter is a `focus`, which that tab's detail follows.
  source: 'system.registry',
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

type Drive = (actions: WorkstationAction[]) => void

/**
 * The `＋` catalog as palette entries — a drill opens from its parent instead.
 * On the reader side a non-reader view is still listed (search must reach
 * everything) but says it crosses to the operator side.
 */
function panelEntries(mode: Mode, run: Drive, side: Side = 'operator', readerOnly = false): PaletteEntry[] {
  const out: PaletteEntry[] = []
  const reader = new Set<string>(readerKinds())
  for (const kind of catalogKinds()) {
    const def = PANEL_REGISTRY[kind].definition
    if (def.modes.length > 0 && !def.modes.includes(mode)) continue
    if (readerOnly && !reader.has(kind)) continue
    out.push({
      key: `panel:${kind}`,
      family: 'panel',
      label: def.defaultTitle,
      hint: side === 'reader' && !reader.has(kind) ? 'operator side' : selectionKindLabel(def.category),
      entryKind: 'panel',
      search: `${def.defaultTitle} ${kind}`,
      open: () => run(paletteActions.panel(kind)),
    })
  }
  return out
}

/** Surface entries — the `#` family, and the guaranteed switcher. */
function surfaceEntries(run: Drive, readerOnly = false): PaletteEntry[] {
  return SURFACES.filter((sf) => !readerOnly || sf.side === 'reader').map((sf) => ({
    key: `surface:${sf.id}`,
    family: 'surface' as const,
    label: `${sf.side === 'reader' ? 'Surface' : 'Operator'} · ${sf.label}`,
    hint: `alt+${sf.key}`,
    entryKind: 'surface' as const,
    search: `${sf.label} ${sf.question} surface ${sf.side} ${sf.id}`,
    open: () => run(paletteActions.surface(sf.id)),
  }))
}

/** Case entries — each recorded case in the Consult store replays. */
function caseEntries(cases: ReturnType<typeof recordedCases>): PaletteEntry[] {
  return cases.map((c) => ({
    key: `case:${c.panelId}:${c.turnIndex}`,
    family: 'case' as const,
    label: `Case · ${c.label}`,
    hint: `replay · ${c.record.actions.length} step${c.record.actions.length === 1 ? '' : 's'}`,
    entryKind: 'case' as const,
    search: `${c.label} case replay ${c.record.summary ?? ''}`,
    open: () => replayRecordedCase(c),
  }))
}

/** The SHELL commands this shell carries — run locally, never recorded. */
function commandEntries(): PaletteEntry[] {
  return availableShellCommands()
    .filter((c) => c.id !== 'palette.search')
    .map((c) => ({
      key: `command:${c.id}`,
      family: 'command' as const,
      label: `Command · ${c.label}`,
      hint: 'shell',
      entryKind: 'command' as const,
      search: `${c.label} ${c.id} command`,
      open: () => {
        runCommand(c.id)
      },
    }))
}

/** The family a registry record sorts in: an inactive record sinks, a paused source sinks last. */
export function recordFamily(rec: PaletteRecord): PaletteFamily {
  const active = !rec.state || rec.state === 'active'
  if (rec.recordKind === 'source') return active ? 'source' : 'source_paused'
  if (!active) return 'inactive'
  return rec.recordKind === 'target' ? 'subject' : 'unit'
}

/** A record's kind as the screen names it — the profile's nouns, never `target` / `analyst`. */
function recordNoun(kind: PaletteRecord['recordKind']): string {
  if (kind === 'target') return SUBJECT_NOUN.toLowerCase()
  if (kind === 'analyst') return PROFILE_UI.unit_noun.singular.toLowerCase()
  return 'source'
}

/** `G20 — Brazil` → `Brazil`: a declared name's qualifier dropped; a name with none is itself. */
function nameTail(name: string): string {
  const i = name.indexOf(' — ')
  const tail = i >= 0 ? name.slice(i + 3).trim() : ''
  return tail || name
}

/**
 * Record entries (subjects / units / sources). `activity` maps a subject id to
 * its newest read on record (epoch ms); a subject absent from it has no read
 * on record and says so.
 */
export function recordEntries(
  records: PaletteRecord[],
  run: Drive,
  activity: ReadonlyMap<string, number> = new Map(),
  subjects?: SubjectIndex,
): PaletteEntry[] {
  const out: PaletteEntry[] = []
  for (const rec of records) {
    const primaryPanel = RECORD_PRIMARY_PANEL[rec.recordKind]
    // Defensive guard: a missing bound primary must degrade — skip the record,
    // never throw at runtime.
    const primaryDef = PANEL_REGISTRY[primaryPanel]?.definition
    if (!primaryDef) continue
    const family = recordFamily(rec)
    const noun = recordNoun(rec.recordKind)
    const stateHint = rec.state && rec.state !== 'active' ? ` · ${rec.state}` : ''
    if (rec.recordKind === 'target') {
      // A SUBJECT: its declared name, its noun, its last read — and Enter
      // opens its READ (scope + focus + its page), exactly as a rail click.
      // Its DECLARED name, as the rail reads it (never a humanized id).
      const name = (subjects ? subjectName(rec.id, subjects) : null) ?? nameTail(rec.label)
      const at = activity.get(rec.id) ?? null
      const when = at != null ? relativeTime(new Date(at).toISOString()) : 'no read on record'
      out.push({
        key: `record:${rec.recordKind}:${rec.id}`,
        family,
        activity: at,
        label: name,
        hint: `${noun}${stateHint} · ${when}`,
        entryKind: 'record',
        search: `${name} ${rec.label} ${rec.id} ${noun} subject`,
        open: () => run(subjectReadActions(rec.id, name)),
        // ⌘/Ctrl-Enter → focus only: the Inspector's read state, then its configuration.
        alt: () => run(paletteActions.selectRecord(rec.recordKind, rec.id, name)),
        altHint: 'inspect',
      })
      out.push({
        key: `action:investigate-target:${rec.id}`,
        family: 'action',
        label: `Investigate · ${name}`,
        hint: 'reads',
        entryKind: 'action',
        search: `investigate ${noun} grid ${name} ${rec.id}`,
        open: () => run(paletteActions.investigate('target', rec.id, INVESTIGATE_KINDS.target)),
      })
      continue
    }
    out.push({
      key: `record:${rec.recordKind}:${rec.id}`,
      family,
      label: rec.label === rec.id ? rec.id : `${rec.label} · ${rec.id}`,
      hint: `${noun}${stateHint}`,
      entryKind: 'record',
      search: `${rec.label} ${rec.id} ${noun} ${rec.recordKind} ${primaryDef.defaultTitle}`,
      // Enter → `focus`: the feed, map, and Inspector all follow the shared
      // selection.
      open: () => run(paletteActions.selectRecord(rec.recordKind, rec.id, rec.label)),
      // ⌘/Ctrl-Enter → `open_panel` on the record's bound primary.
      alt: () => run(paletteActions.openRecord(primaryPanel, rec.recordKind, rec.id)),
      altHint: 'open panel',
    })
    if (rec.recordKind === 'analyst') {
      out.push({
        key: `action:investigate-analyst:${rec.id}`,
        family: 'action',
        label: `Investigate · ${rec.label === rec.id ? rec.id : rec.label}`,
        hint: 'reads',
        entryKind: 'action',
        search: `investigate ${noun} grid ${rec.label} ${rec.id}`,
        open: () => run(paletteActions.investigate('analyst', rec.id, INVESTIGATE_KINDS.analyst)),
      })
    }
  }
  return out
}

/** Within a family: newest activity first, an entry with none after every one that has some, then label. */
function byActivityThenLabel(a: PaletteEntry, b: PaletteEntry): number {
  const aa = a.activity ?? null
  const bb = b.activity ?? null
  if (aa != null && bb != null && aa !== bb) return bb - aa
  if (aa != null && bb == null) return -1
  if (aa == null && bb != null) return 1
  return a.label.localeCompare(b.label)
}

/**
 * THE ORDER (X3/D3). Empty query: the operator's recents, then favorites,
 * then by family (subjects first, by activity, newest first), then label.
 * With a query: a paused source still sinks below every other match, then by
 * how well it matched, favorites, family, activity, label.
 */
export function orderPaletteEntries(
  entries: readonly PaletteEntry[],
  rest: string,
  opts: { recents?: readonly string[]; favorites?: ReadonlySet<string> } = {},
): PaletteEntry[] {
  const favorites = opts.favorites ?? new Set<string>()
  const fav = (e: PaletteEntry) => (favorites.has(e.key) ? 0 : 1)
  const fam = (e: PaletteEntry) => FAMILY_RANK.get(e.family) ?? PALETTE_FAMILY_ORDER.length
  if (!rest) {
    const recentRank = new Map((opts.recents ?? []).map((id, i) => [id, i]))
    return entries.slice().sort((a, b) => {
      const ra = recentRank.get(a.key) ?? Infinity
      const rb = recentRank.get(b.key) ?? Infinity
      if (ra !== rb) return ra - rb
      if (fav(a) !== fav(b)) return fav(a) - fav(b)
      if (fam(a) !== fam(b)) return fam(a) - fam(b)
      return byActivityThenLabel(a, b)
    })
  }
  const paused = (e: PaletteEntry) => (e.family === 'source_paused' ? 1 : 0)
  const scored = entries
    .map((e) => ({ e, score: Math.max(scoreMatch(rest, e.label), scoreMatch(rest, e.search)) }))
    .filter((x) => x.score > 0)
  scored.sort((a, b) => {
    if (paused(a.e) !== paused(b.e)) return paused(a.e) - paused(b.e)
    if (a.score !== b.score) return b.score - a.score
    if (fav(a.e) !== fav(b.e)) return fav(a.e) - fav(b.e)
    if (fam(a.e) !== fam(b.e)) return fam(a.e) - fam(b.e)
    return byActivityThenLabel(a.e, b.e)
  })
  return scored.map((x) => x.e)
}

const driveAll: Drive = (actions) => {
  drive(actions)
}

export function CommandPalette({ open, mode, onClose, side = 'operator', onDrive = driveAll }: CommandPaletteProps) {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const [favorites, setFavorites] = useState<Set<string>>(() => loadFavorites())
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)

  // Records are fetched only while the palette is open (lazy index).
  const { records } = usePaletteRecords(open)
  const consultPanels = useConsultSessions((s) => s.panels)
  const cases = useMemo(() => recordedCases(consultPanels), [consultPanels])
  // Subject ACTIVITY — the newest read on record per subject, off the same
  // reads the rail's Subjects section already holds (one cache entry each).
  const { verdicts } = useCountryVerdicts()
  const { desks } = useSupplyChainDesks()
  const subjects = useSubjectIndex()
  // RA-1: a reader is never offered the operator side (its reads are refused)
  const readerOnly = useBelowContributor()
  const activity = useMemo(() => {
    const m = new Map<string, number>()
    const note = (id: string, iso: string | null) => {
      const t = iso ? Date.parse(iso) : NaN
      if (id && Number.isFinite(t) && t > (m.get(id) ?? -Infinity)) m.set(id, t)
    }
    for (const v of verdicts.values()) note(v.targetId, v.producedAt)
    for (const d of desks) note(d.targetId, d.latestFindingAt)
    return m
  }, [verdicts, desks])

  // The full index — every family compiles to drive actions (or, for a case,
  // to a replay through the player; for a command, to its shell handler).
  const allEntries = useMemo(
    () => [
      ...surfaceEntries(onDrive, readerOnly),
      ...caseEntries(cases),
      ...recordEntries(records, onDrive, activity, subjects),
      ...panelEntries(mode, onDrive, side, readerOnly),
      ...(open ? commandEntries() : []),
    ],
    [records, mode, onDrive, cases, open, activity, subjects, side, readerOnly],
  )

  // The order (`orderPaletteEntries`): with no query, recents, favorites, then
  // by FAMILY — subjects first by activity, paused sources last — never an
  // alphabetical wall across families. With a query, scored, paused last.
  const filtered = useMemo(() => {
    // Prefix namespacing narrows the FAMILY before the match runs (`#` = the
    // surfaces, `>` = panels + commands); the sigil itself is not part of the
    // query.
    const { prefix, rest } = parsePalettePrefix(query)
    const inScope = prefix ? allEntries.filter((e) => PREFIX_FAMILIES[prefix].has(e.entryKind)) : allEntries
    return orderPaletteEntries(inScope, rest, { recents: rest ? [] : loadRecents(), favorites })
  }, [allEntries, query, favorites])

  // Reset transient state each time the palette opens. (Focus is the
  // dialog's job: `onOpenAutoFocus` below puts it in the search box.)
  useEffect(() => {
    if (!open) return
    setQuery('')
    setActive(0)
    setFavorites(loadFavorites())
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

  // The LIST keys. Escape, Tab and the backdrop belong to the Dialog.
  function onKeyDown(e: React.KeyboardEvent) {
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
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent
        overlayTestId="command-palette-backdrop"
        data-testid="command-palette"
        aria-describedby={undefined}
        className="top-[12vh] w-[36rem] max-w-[90vw] translate-y-0 overflow-hidden"
        onOpenAutoFocus={(e) => {
          // Into the search box, not the first favorite star.
          e.preventDefault()
          inputRef.current?.focus()
        }}
        onKeyDown={onKeyDown}
      >
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value)
            setActive(0)
          }}
          placeholder="Jump to a subject, record, view, case… (# surfaces, > views & commands)"
          aria-label="Search records, views and surfaces"
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
                  'shrink-0 ml-3 rounded-control p-0.5',
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
                data-family={entry.family}
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
          <span>↑↓ navigate · ↵ open/select · ⌘↵ view · # surface · ★ favorite · esc</span>
          <span>
            {filtered.length} result{filtered.length === 1 ? '' : 's'}
          </span>
        </div>
      </DialogContent>
    </Dialog>
  )
}
