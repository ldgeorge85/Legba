/**
 * Command-palette recents + favorites (redesign Move 3a — smart defaults).
 *
 * The palette indexes panels AND records (targets / analysts / sources). On a
 * cold open it should not show an empty alphabetical wall — it should show what
 * the operator reaches for: recently-opened entries first, then favorites.
 *
 * Both are localStorage-backed and tolerant of junk: a corrupt store degrades to
 * empty rather than throwing (the palette is a convenience surface, never load-
 * bearing). Entries are addressed by a stable composite key the palette mints
 * (`<kind>:<id>` for records/presets/actions, the panel kind for singletons) so
 * the same logical entry dedupes regardless of how it was constructed this run.
 */

const RECENTS_KEY = 'legba_palette_recents'
const FAVORITES_KEY = 'legba_palette_favorites'
const MAX_RECENTS = 12

function readList(key: string): string[] {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

function writeList(key: string, ids: string[]): void {
  try {
    localStorage.setItem(key, JSON.stringify(ids))
  } catch {
    // localStorage unavailable (private mode / quota) — recents are a
    // non-critical convenience; ignore.
  }
}

/** The recents list, newest first. */
export function loadRecents(): string[] {
  return readList(RECENTS_KEY)
}

/** Push an entry id to the front of the recents list (deduped, capped). */
export function pushRecent(entryId: string): void {
  if (!entryId) return
  const prev = readList(RECENTS_KEY).filter((id) => id !== entryId)
  writeList(RECENTS_KEY, [entryId, ...prev].slice(0, MAX_RECENTS))
}

/** The favorites set. */
export function loadFavorites(): Set<string> {
  return new Set(readList(FAVORITES_KEY))
}

/** Toggle an entry's favorite flag; returns the new favorite state. */
export function toggleFavorite(entryId: string): boolean {
  const set = new Set(readList(FAVORITES_KEY))
  let now: boolean
  if (set.has(entryId)) {
    set.delete(entryId)
    now = false
  } else {
    set.add(entryId)
    now = true
  }
  writeList(FAVORITES_KEY, [...set])
  return now
}
