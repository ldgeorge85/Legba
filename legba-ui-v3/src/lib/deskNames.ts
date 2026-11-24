/**
 * deskNames — a subject id → its human display name, and the roster leg it
 * sits on, FROM WHAT THE SUBJECT DECLARES (UI-B2; the geometry-contract rule:
 * never a regex over the id).
 *
 * This module used to know the exemplar's id convention: it regexed the ISO
 * code out of `country_g20_br` / `country_watch_sd`, kept a hand table of the
 * supply-chain lanes' names, and stripped `country_g20_` off anything else. A
 * subject called `regime_ofac_russia` has none of those shapes. Now a name is
 * read off the subject's descriptor (`lib/subjectIndex.ts`, the registry's
 * target listing), in this order:
 *
 *   1. under a profile whose subjects declare ISO codes (`map.geometry:
 *      iso2`), a `scope.domain: geo` subject declaring exactly ONE code the
 *      gazetteer knows reads as the gazetteer's name (`lib/countryGeo.ts` —
 *      `Brazil`, `South Korea`, `DR Congo`: today's names, byte for byte);
 *   2. otherwise the declared name (`identity.name`). A declared name of the
 *      house form `<qualifier> — <name>` (`Lane — Strait of Hormuz`,
 *      `Region — Europe`) reads as its name; the qualifier restates the
 *      subject's kind, which the surface already shows;
 *   3. a subject the roster has not served (the listing not read yet, or an id
 *      that is not a registered subject — an analyst, a unit) is humanized
 *      generically: a leading `analyst_` dropped, split on `._-`, title-cased.
 *
 * The roster leg ({@link rosterTagOf}) is the first of the profile's
 * `rosters.subject_tags` the subject's declared `scope.tags` carries.
 *
 * {@link useSubjectIndex} keeps the index current; the shell mounts it once
 * (`App.tsx`), and a surface that derives names inside a memo reads it too so
 * the names re-derive when the roster lands.
 */
import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from './api'
import { COUNTRY_BY_ISO2 } from './countryGeo'
import { PROFILE_UI, type ProfileUi } from './profileUi'
import {
  getSubjectIndex,
  setSubjectIndex,
  subjectIndexFrom,
  type DeclaredSubject,
  type SubjectIndex,
} from './subjectIndex'

export type { DeclaredSubject, SubjectIndex } from './subjectIndex'

/** The declared subject, or null when the roster does not serve the id. */
export function declaredSubject(
  id: string | null | undefined,
  index: SubjectIndex = getSubjectIndex(),
): DeclaredSubject | null {
  if (!id) return null
  return index.get(id.trim()) ?? null
}

/** `Lane — Strait of Hormuz` → `Strait of Hormuz`; a name with no qualifier is itself. */
function nameTail(name: string): string {
  const i = name.indexOf(' — ')
  const tail = i >= 0 ? name.slice(i + 3).trim() : ''
  return tail || name
}

/** The gazetteer name of a geo subject that declares exactly one known code. */
function gazetteerName(s: DeclaredSubject, ui: ProfileUi): string | null {
  if (ui.map.geometry !== 'iso2' || s.domain !== 'geo' || s.geo.length !== 1) return null
  return COUNTRY_BY_ISO2[s.geo[0].toUpperCase()]?.name ?? null
}

/**
 * The subject's display name from its declaration (rules 1–2 above), or
 * `null` when the roster does not serve the id.
 */
export function subjectName(
  id: string | null | undefined,
  index: SubjectIndex = getSubjectIndex(),
  ui: ProfileUi = PROFILE_UI,
): string | null {
  const s = declaredSubject(id, index)
  if (!s) return null
  return gazetteerName(s, ui) ?? (s.name ? nameTail(s.name) : null)
}

/**
 * A geo subject's name (`scope.domain: geo`), or null — the "is this a place
 * desk" question callers used to answer with the id regex.
 */
export function countryNameForTargetId(
  targetId: string | null | undefined,
  index: SubjectIndex = getSubjectIndex(),
): string | null {
  const s = declaredSubject(targetId, index)
  return s && s.domain === 'geo' ? subjectName(targetId, index) : null
}

/** A thematic subject's name (`scope.domain: thematic`), or null. */
export function thematicDeskName(
  targetId: string | null | undefined,
  index: SubjectIndex = getSubjectIndex(),
): string | null {
  const s = declaredSubject(targetId, index)
  return s && s.domain === 'thematic' ? subjectName(targetId, index) : null
}

/**
 * The profile roster leg a subject sits on: the first of the profile's
 * `rosters.subject_tags` (profile order) its declared `scope.tags` carries,
 * or null — a subject the roster has not served, or one on no declared leg.
 */
export function rosterTagOf(
  id: string | null | undefined,
  index: SubjectIndex = getSubjectIndex(),
  ui: ProfileUi = PROFILE_UI,
): string | null {
  const s = declaredSubject(id, index)
  if (!s) return null
  return ui.rosters.subject_tags.find((t) => s.tags.includes(t)) ?? null
}

/**
 * Humanize ANY substrate id for product prose: a registered subject reads as
 * its declared name ({@link subjectName}); anything else drops a leading
 * `analyst_`, splits on `._-`, and title-cases the remainder. An id with no
 * splittable content (pure punctuation) is returned unchanged — this function
 * never fabricates a name.
 */
export function humanizeId(id: string): string {
  const declared = subjectName(id)
  if (declared) return declared
  const words = id.replace(/^analyst_/i, '').replace(/[._-]+/g, ' ').trim()
  return words ? words.replace(/\b\w/g, (c) => c.toUpperCase()) : id
}

/** The subject roster's query — one key, one route, one request for every
 *  reader of names and declared scope (`lib/subjectGeometry.ts`, the shell's
 *  bound panels, the supply-chain desks, the phone). It asks the SUMMARY
 *  weight (`registry/descriptor_list_weight.py`: identity name + scope
 *  domain/geo/tags/geometry/themes): roll 84's live walk measured the whole
 *  bodies at 279 KB, the phone landing's largest response over its 0.25 MB
 *  budget. The key is NOT the Target Registry panel's `['registry-targets']`
 *  (that panel edits whole bodies); invalidating that prefix refreshes this too. */
export const SUBJECT_ROSTER_QUERY_KEY = ['registry-targets', 'summary'] as const
/**
 * RA-1: the reader's roster read (`GET /v3/roster`, `reader_reads_api.py`) —
 * the same summary cut WITHOUT the descriptor's owner, tenant, signature
 * columns or any body beyond the roster keys, open to every signed-in role.
 * The admin list (`/registry/descriptors`) stays the Registry panels' read.
 * Rows: descriptor_id, family, name, kind, state, version, created_at, body
 * (identity.name, scope.{domain, geo, tags, geometry, themes},
 * declared_sources); a source row adds geo, languages, tags, assurance_grade.
 */
export function rosterRoute(family: 'target' | 'analyst' | 'source', limit = 500): string {
  return `/v3/roster?family=${family}&limit=${limit}`
}
export const SUBJECT_ROSTER_ROUTE = rosterRoute('target')
/** The analyst roster at the same weight (the shell's bound panels, the palette). */
export const ANALYST_ROSTER_ROUTE = rosterRoute('analyst')
/** The source roster (the Wall's count, the subject's Sources tab, the palette). */
export const SOURCE_ROSTER_ROUTE = rosterRoute('source', 1000)

/**
 * Read the subject roster and keep the session's index current. Returns the
 * index (empty until the listing lands); a component that derives names inside
 * a memo lists it as a dependency.
 */
export function useSubjectIndex(): SubjectIndex {
  const q = useQuery<unknown>({
    queryKey: SUBJECT_ROSTER_QUERY_KEY,
    queryFn: () => apiGet<unknown>(SUBJECT_ROSTER_ROUTE),
    staleTime: 300_000,
  })
  // An empty (or non-listing) body leaves the index as it was: there is
  // nothing in it to name, and a name read earlier is still the subject's own.
  const fresh = useMemo(() => {
    const idx = subjectIndexFrom(q.data)
    return idx.size > 0 ? idx : null
  }, [q.data])
  useEffect(() => {
    if (fresh) setSubjectIndex(fresh)
  }, [fresh])
  // The render that first sees the listing reads it directly (the effect lands after it).
  return fresh ?? getSubjectIndex()
}
