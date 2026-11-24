/**
 * profileUi — the instance's words, read from `profile_ui.json` (UI-D1 §2.3).
 *
 * The platform has nouns (subject, frame, unit, read, case, mission); an
 * INSTANCE names them (the geopolitics exemplar says "Desks", "G20", "Watch
 * list"; a sanctions instance says "regime" and "counterparty"). The shell
 * reads every such label from the profile's `ui` key, exported at build time
 * by PROFILE-1 (`scripts/gen_profile_ui.py`, schema `legba/profile_ui/1`,
 * docs/PROFILES.md "The workstation's labels") into `lib/profile_ui.json`.
 * That file is GENERATED — never edited here — and the geopolitics export
 * reproduces today's literals byte for byte (`profileUi.test.ts` pins them).
 *
 * THE ABSENCE RULE: a key the profile does not declare is left out of the
 * export, and the shell renders the PLATFORM noun ("subject", "units") in its
 * place — never a blank and never the exemplar's word.
 */
import { useQuery } from '@tanstack/react-query'
import raw from '@/lib/profile_ui.json'
import { ApiError, apiGet } from '@/lib/api'

/** A frame kind (a read tier): its label, its parent frame, its producer. */
export interface ProfileFrame {
  id: string
  label: string
  parent: string | null
  producer: string | null
}

export interface ProfileUi {
  schema: string
  profile: string
  /** The projection plane this profile's rows carry (VIEW-1): the declared
   *  `projection.plane`, else the profile's own id. */
  plane: string
  subject: { singular: string; plural: string }
  unit_noun: { singular: string; plural: string }
  frames: ProfileFrame[]
  /** The producer id of the top floor, or null when the profile declares none. */
  top_floor: string | null
  landing: string
  /** Label overrides keyed by the shell's own stance ids. */
  stances: Record<string, { label: string; question: string | null }>
  /** Label overrides keyed by the shell's own mission ids. */
  missions: Record<string, string>
  map: { geometry: 'iso2' | 'declared' | 'none'; title: string }
  labels: {
    overview_title: string
    read_title: string
    unscoped_subject: string
    /** Subject roster tag → its label. */
    roster: Record<string, string>
  }
  rosters: {
    /** The subject roster tags, in the order the profile lists them. */
    subject_tags: string[]
    scored_subject_tags: string[]
    /** The profile's unit roster, or null when it declares none. */
    units: string[] | null
    /** Each roster unit's display data (ROSTER-2), roster order; `[]` when
     *  the profile declares no units. Read through `profileUnits()`. */
    unit_entries: ProfileUnit[]
  }
}

/**
 * One bounded unit as the workstation renders it (ROSTER-2: the profile's
 * `rosters.units[]` entry). The absence rule applies per field: a unit with no
 * declared label reads as its id de-underscored, no glyph as its initials.
 */
export interface ProfileUnit {
  id: string
  label: string
  /** Short chip glyph (1-4 characters). */
  short: string
  /** The tag the unit's fan-out narrows to, or null (every scored subject). */
  scopeTag: string | null
  /** Why a scoped unit has no read on most subjects, or null. */
  scopeNote: string | null
  /** The standing question the unit answers (the profile's question set), or
   *  null when the profile declares none for it (UI-B3's evidence table). */
  question: string | null
}

/** The platform nouns — what renders where the profile declares nothing. */
export const PLATFORM_NOUNS = {
  subject: { singular: 'subject', plural: 'subjects' },
  unit: { singular: 'unit', plural: 'units' },
  read: 'Read',
  overview: 'Overview',
  map: 'Map',
  unscoped: 'no subject (unscoped)',
  noTopFloor: 'no top floor is declared for this instance',
} as const

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v : null
}

function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

function arr(v: unknown): Record<string, unknown>[] {
  return Array.isArray(v) ? v.map(obj) : []
}

function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && !!x) : []
}

/** Read a `legba/profile_ui/1` document defensively, applying the absence rule. */
export function profileUiFrom(doc: unknown): ProfileUi {
  const d = obj(doc)
  const subject = obj(d.subject)
  const unit = obj(d.unit_noun)
  const map = obj(d.map)
  const labels = obj(d.labels)
  const rosters = obj(d.rosters)
  const geometry = map.geometry === 'iso2' || map.geometry === 'declared' || map.geometry === 'none' ? map.geometry : 'none'
  const stances: ProfileUi['stances'] = {}
  for (const s of arr(d.stances)) {
    const id = str(s.id)
    const label = str(s.label)
    if (id && label) stances[id] = { label, question: str(s.question) }
  }
  const missions: Record<string, string> = {}
  for (const m of arr(d.missions)) {
    const id = str(m.id)
    const label = str(m.label)
    if (id && label) missions[id] = label
  }
  const roster: Record<string, string> = {}
  for (const [k, v] of Object.entries(obj(labels.roster))) if (str(v)) roster[k] = v as string
  const profile = str(d.profile) ?? 'unnamed'
  return {
    schema: str(d.schema) ?? 'unknown',
    profile,
    plane: str(d.plane) ?? profile,
    subject: {
      singular: str(subject.singular) ?? PLATFORM_NOUNS.subject.singular,
      plural: str(subject.plural) ?? PLATFORM_NOUNS.subject.plural,
    },
    unit_noun: {
      singular: str(unit.singular) ?? PLATFORM_NOUNS.unit.singular,
      plural: str(unit.plural) ?? PLATFORM_NOUNS.unit.plural,
    },
    frames: arr(d.frames)
      .filter((f) => str(f.id) && str(f.label))
      .map((f) => ({
        id: f.id as string,
        label: f.label as string,
        parent: str(f.parent),
        producer: str(f.producer),
      })),
    top_floor: str(d.top_floor),
    landing: str(d.landing) ?? 'morning_read',
    stances,
    missions,
    map: { geometry, title: str(map.title) ?? PLATFORM_NOUNS.map },
    labels: {
      overview_title: str(labels.overview_title) ?? PLATFORM_NOUNS.overview,
      read_title: str(labels.read_title) ?? PLATFORM_NOUNS.read,
      unscoped_subject: str(labels.unscoped_subject) ?? PLATFORM_NOUNS.unscoped,
      roster,
    },
    rosters: {
      subject_tags: strings(rosters.subject_tags),
      scored_subject_tags: strings(rosters.scored_subject_tags),
      units: Array.isArray(rosters.units)
        ? arr(rosters.units)
            .map((u) => str(u.id))
            .filter((x): x is string => !!x)
        : null,
      unit_entries: arr(rosters.units)
        .filter((u) => str(u.id))
        .map((u) => {
          const id = u.id as string
          return {
            id,
            label: str(u.label) ?? idWords(id),
            short: str(u.short) ?? initials(id),
            scopeTag: str(u.scope_tag),
            scopeNote: str(u.scope_note),
            question: str(u.question),
          }
        }),
    },
  }
}

/** An id as words, first letter capitalised (`energy_security` → `Energy security`). */
function idWords(id: string): string {
  const words = id.replace(/[_-]+/g, ' ').trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : id
}

/** An id's initials, upper-case (`energy_security` → `ES`), at most four. */
function initials(id: string): string {
  const parts = id.split(/[_-]+/).filter(Boolean)
  const glyph = parts.length > 1 ? parts.map((p) => p.charAt(0)).join('') : (parts[0] ?? id).slice(0, 3)
  return glyph.slice(0, 4).toUpperCase()
}

/** The profile's bounded units, roster order (ROSTER-2 — the ONE unit list the
 *  workstation reads; no unit id is spelled in the UI). */
export function profileUnits(ui: ProfileUi = PROFILE_UI): ProfileUnit[] {
  return ui.rosters.unit_entries
}

/**
 * The units a scored subject's desk carries, roster order: every unit except
 * one scoped to an UNSCORED subject-roster leg (a unit whose scope tag is a
 * roster tag outside `scored_subject_tags` runs on that leg only). The Python
 * twin is `legba.data.rosters.scored_subject_units`.
 */
export function subjectUnits(ui: ProfileUi = PROFILE_UI): ProfileUnit[] {
  const scored = new Set(ui.rosters.scored_subject_tags)
  const otherLegs = new Set(ui.rosters.subject_tags.filter((t) => !scored.has(t)))
  return profileUnits(ui).filter((u) => u.scopeTag === null || !otherLegs.has(u.scopeTag))
}

/** This build's profile. */
export const PROFILE_UI: ProfileUi = profileUiFrom(raw)

/** The frames that carry a producer — the read tiers, top floor first. */
export function readTiers(ui: ProfileUi = PROFILE_UI): ProfileFrame[] {
  const tiers = ui.frames.filter((f) => f.producer)
  return [...tiers].sort((a, b) => Number(b.producer === ui.top_floor) - Number(a.producer === ui.top_floor))
}

/**
 * A subject roster tag's label. A tag the profile lists without a label reads
 * as the tag itself, de-underscored — the tag is the profile's own word, not
 * the exemplar's.
 */
export function rosterLabel(tag: string, ui: ProfileUi = PROFILE_UI): string {
  return ui.labels.roster[tag] || idWords(tag)
}

/** A stance's label: the profile's override, else the shell's own literal. */
export function stanceLabel(id: string, fallback: string, ui: ProfileUi = PROFILE_UI): string {
  return ui.stances[id]?.label ?? fallback
}

/** A mission's label: the profile's override, else the shell's own literal. */
export function missionLabel(id: string, fallback: string, ui: ProfileUi = PROFILE_UI): string {
  return ui.missions[id] ?? fallback
}

/** A stance's question: the profile's override, else the shell's own literal. */
export function stanceQuestion(id: string, fallback: string, ui: ProfileUi = PROFILE_UI): string {
  return ui.stances[id]?.question ?? fallback
}

/**
 * THE ID ALIASES. A profile renames a stance or a mission by LABEL; the id
 * never changes (it is the shell's slot key, a recorded case's address and the
 * `#stance=` hash). An operator — or a case — that names one by the word on
 * the screen still reaches it: the id, the shell's own label and the profile's
 * label all resolve to the id (case-insensitive, whitespace-trimmed). Unknown
 * → null, never a guess.
 */
export function resolveAliasedId<T extends string>(
  word: string,
  known: readonly { id: T; label: string; defaultLabel?: string }[],
): T | null {
  const w = word.trim().toLowerCase()
  if (!w) return null
  for (const k of known) {
    if (k.id.toLowerCase() === w) return k.id
  }
  for (const k of known) {
    const words = [k.label, k.defaultLabel].filter((x): x is string => !!x).map((x) => x.toLowerCase())
    if (words.includes(w)) return k.id
  }
  return null
}

// ---------------------------------------------------------------------------
// The read tiers (frames that carry a producer).
// ---------------------------------------------------------------------------

/** The tier a producer's reads sit on, or null for a producer outside the read set. */
export function tierForProducer(
  producer: string | null | undefined,
  tiers: readonly ProfileFrame[],
): ProfileFrame | null {
  if (!producer) return null
  return tiers.find((t) => t.producer === producer) ?? null
}

/**
 * What an untargeted read is ABOUT, in the profile's words: a read on a
 * parentless tier (an instance-wide read — the exemplar's world read and its
 * assessment) names the top floor's frame; a read on a nested tier names its
 * own tier. With no top floor declared, every tier names itself.
 */
export function untargetedSubject(
  tier: ProfileFrame,
  tiers: readonly ProfileFrame[],
  ui: ProfileUi = PROFILE_UI,
): string {
  if (tier.parent || !ui.top_floor) return tier.label
  return tiers.find((t) => t.producer === ui.top_floor)?.label ?? tier.label
}

/**
 * The tiers whose reads are one per ROSTER subject — a nested frame no other
 * frame hangs under (the exemplar's country tier). The roster legs sub-group
 * their reads. Empty when the profile nests no tier.
 */
export function subjectTierIds(tiers: readonly ProfileFrame[]): Set<string> {
  const parents = new Set(tiers.map((f) => f.parent).filter((p): p is string => !!p))
  return new Set(tiers.filter((t) => t.parent && !parents.has(t.id)).map((t) => t.id))
}

// ---------------------------------------------------------------------------
// THE PROJECTION (VIEW-1) — `GET /registry/projection/{plane}/{section}`.
// ---------------------------------------------------------------------------
//
// PRECEDENCE. `profile_ui.json` is the build's own export of the profile: it
// is synchronous, so every label paints on the FIRST frame and never waits on
// a route. The projection is the same manifest resolved by the registry plus
// what the substrate adds (the frame family's subject groups, the units'
// cadences). A surface that reads a section takes it when the plane answers
// `ok` with rows, and otherwise keeps the packaged export (tiers) or the
// platform's own default (the Morning Read's bands) — and SAYS which, naming
// the refusal (`projection_disabled` / `projection_empty` /
// `projection_stale`, the 404s `plane_unknown` / `section_undeclared`, or the
// transport's `projection_unreachable`). Never a blank: a section only the
// projection carries (the subject groups) renders the refusal by name.

export type ProjectionSection =
  | 'subject_groups'
  | 'units'
  | 'tiers'
  | 'labels'
  | 'bands'
  | 'scope'
  | 'crosswalk'

export interface ProjectionRow {
  key: string
  ord: number
  value: unknown
}

/** A projection read, typed: loading, the section's rows, or the refusal BY NAME. */
export type ProjectionRead =
  | { state: 'loading'; section: ProjectionSection; plane: string }
  | {
      state: 'ok'
      section: ProjectionSection
      plane: string
      rows: ProjectionRow[]
      /** The build stamp — every 200 publishes it (the as-of of every row). */
      projectedAt: string | null
      ageSeconds: number | null
      /** Why the build could not measure (part of) the section, in words; null when it could. */
      unmeasured: string | null
    }
  | {
      state: 'absent'
      section: ProjectionSection
      plane: string
      /** The refusal's machine name (`projection_empty`, …) or the transport's. */
      name: string
      reason: string
    }

/** The route of one section of the profile's plane. */
export function projectionPath(section: ProjectionSection, ui: ProfileUi = PROFILE_UI): string {
  return `/registry/projection/${encodeURIComponent(ui.plane)}/${section}`
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** A 200 body → the typed read; a body that is not the route's shape is `projection_malformed`. */
export function projectionOk(body: unknown, section: ProjectionSection, plane: string): ProjectionRead {
  const b = obj(body)
  if (!Array.isArray(b.rows)) {
    return { state: 'absent', section, plane, name: 'projection_malformed', reason: 'the 200 body carries no rows array' }
  }
  const rows = arr(b.rows)
    .filter((r) => str(r.key))
    .map((r) => ({ key: r.key as string, ord: num(r.ord) ?? 0, value: r.value }))
    .sort((x, y) => x.ord - y.ord)
  return {
    state: 'ok',
    section,
    plane,
    rows,
    projectedAt: str(b.projected_at),
    ageSeconds: num(b.age_seconds),
    unmeasured: str(b.unmeasured),
  }
}

/** A thrown read → the refusal by name: the route's `detail.state` for a 404/409, else the transport's. */
export function projectionRefusal(err: unknown, section: ProjectionSection, plane: string): ProjectionRead {
  if (err instanceof ApiError) {
    const detail = obj(obj(err.body).detail)
    const name = str(detail.state)
    if (name) return { state: 'absent', section, plane, name, reason: str(detail.reason) ?? name }
    return { state: 'absent', section, plane, name: 'projection_unreachable', reason: `HTTP ${err.status}` }
  }
  return {
    state: 'absent',
    section,
    plane,
    name: 'projection_unreachable',
    reason: err instanceof Error ? err.message : 'the route did not answer',
  }
}

/** A refusal is an answer (4xx): retrying it re-asks a settled question. */
function retryProjection(count: number, err: unknown): boolean {
  if (err instanceof ApiError && err.status < 500) return false
  return count < 2
}

/** One section of this profile's plane, typed (see PRECEDENCE above). */
export function useProjection(section: ProjectionSection, ui: ProfileUi = PROFILE_UI): ProjectionRead {
  const path = projectionPath(section, ui)
  const q = useQuery<unknown>({
    queryKey: ['profile-projection', ui.plane, section],
    queryFn: () => apiGet<unknown>(path),
    staleTime: 300_000,
    retry: retryProjection,
  })
  if (q.isError) return projectionRefusal(q.error, section, ui.plane)
  if (q.data === undefined) return { state: 'loading', section, plane: ui.plane }
  return projectionOk(q.data, section, ui.plane)
}

/** The refusal (or the wait) as one line: `projection_empty — <reason>`; null when the plane answered. */
export function projectionAbsenceText(read: ProjectionRead): string | null {
  if (read.state === 'absent') return `${read.name} — ${read.reason}`
  if (read.state === 'loading') return 'projection not read yet'
  return null
}

// ── section decoders (pure) ──────────────────────────────────────────────────

/** `tiers` rows → frames (manifest order kept by `ord`). */
export function tiersFromProjection(rows: readonly ProjectionRow[]): ProfileFrame[] {
  const out: ProfileFrame[] = []
  for (const r of rows) {
    const v = obj(r.value)
    const label = str(v.label)
    if (label) out.push({ id: r.key, label, parent: str(v.parent), producer: str(v.producer) })
  }
  return out
}

/** One Morning Read band as the plane declares it. */
export interface ProjectedBand {
  id: string
  label: string
  question: string
}

/** `bands` rows → bands, reading order. */
export function bandsFromProjection(rows: readonly ProjectionRow[]): ProjectedBand[] {
  const out: ProjectedBand[] = []
  for (const r of rows) {
    const v = obj(r.value)
    const label = str(v.label)
    const question = str(v.question)
    if (label && question) out.push({ id: r.key, label, question })
  }
  return out
}

/** One subject group of the frame family (FRAME-1), as the plane carries it. */
export interface SubjectGroup {
  id: string
  /** The declared kind whose subtree it is in (`region`, `thematic`). */
  kind: string
  label: string
  parent: string | null
  /** The frame's own member rule (`tag:region_africa`, `analyst:…`); empty for
   *  a kind-of-kinds, which groups nothing of its own (FRAME-1's `groups()`). */
  memberRule: string[]
  /** The member target ids the frame resolves to (may be empty — a lane is an
   *  analyst; a kind-of-kinds resolves its whole subtree). */
  members: string[]
}

/** `subject_groups` rows → groups (kinds in declared order, the kind first). */
export function subjectGroupsFromProjection(rows: readonly ProjectionRow[]): SubjectGroup[] {
  return rows.map((r) => {
    const v = obj(r.value)
    return {
      id: r.key,
      kind: str(v.kind) ?? r.key,
      label: str(v.label) ?? idWords(r.key),
      parent: str(v.parent),
      memberRule: strings(v.member_rule),
      members: strings(v.members),
    }
  })
}

/**
 * The subject groups a surface lists: frames that carry their OWN member rule
 * and resolve at least one member, in the plane's order — a kind-of-kinds
 * (`region`, whose `members` is its whole subtree) and a lane that resolves no
 * subject (an analyst lane) head nothing.
 */
export function subjectGroupsToList(rows: readonly ProjectionRow[]): SubjectGroup[] {
  return subjectGroupsFromProjection(rows).filter((g) => g.memberRule.length > 0 && g.members.length > 0)
}

/**
 * The read tiers with the projection's precedence: the plane's `tiers` when it
 * answers with rows, else the packaged export's frames. `source` says which;
 * `read` carries the refusal for the surface to name.
 */
export function useReadTiers(ui: ProfileUi = PROFILE_UI): {
  tiers: ProfileFrame[]
  source: 'projection' | 'profile'
  read: ProjectionRead
} {
  const read = useProjection('tiers', ui)
  if (read.state === 'ok') {
    const projected = tiersFromProjection(read.rows)
    if (projected.length > 0) return { tiers: readTiers({ ...ui, frames: projected }), source: 'projection', read }
  }
  return { tiers: readTiers(ui), source: 'profile', read }
}
