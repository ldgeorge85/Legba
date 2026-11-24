/**
 * subjectIndex — what each registered subject DECLARES about itself, read as
 * data (UI-B2; the UI-B4 geometry-contract rule: never parse an id).
 *
 * A subject's display name and its roster membership used to be regexes over
 * the exemplar's id convention (`/^country_(?:g20|watch)_([a-z]{2})$/`,
 * `/^country_([a-z0-9]+)_/`). A subject called `regime_ofac_russia` or
 * `counterparty_acme` has neither shape, and should not need one. This module
 * holds the subject roster as the registry serves it — `GET /registry/
 * descriptors?family=target` (the Target Registry panel's read, the same one
 * `lib/subjectGeometry.ts` keys its geometry index on) — and nothing else:
 *
 *   - `name`   — the descriptor's declared name (`identity.name`);
 *   - `domain` — `scope.domain` (`geo` / `thematic` / …);
 *   - `geo`    — `scope.geo`, the ISO codes the subject declares;
 *   - `tags`   — `scope.tags`, where the profile's subject-roster tags live.
 *
 * A LEAF with no imports, so the test setup can seed it (`src/test-setup.ts`)
 * without loading the profile before a test mocks `profile_ui.json`. The
 * readers — names, roster tags — are `lib/deskNames.ts`; the hook that keeps
 * the index current is `useSubjectIndex` there.
 */

/** One registered subject, as its descriptor declares it. */
export interface DeclaredSubject {
  id: string
  /** The declared name, verbatim; null when the descriptor carries none. */
  name: string | null
  domain: string | null
  geo: string[]
  tags: string[]
  state: string | null
}

export type SubjectIndex = ReadonlyMap<string, DeclaredSubject>

function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v : null
}

function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && !!x) : []
}

/**
 * Index a target listing by descriptor id. Reads the row's `name` (else
 * `body.identity.name`) and `body.scope.{domain, geo, tags}`; a row that is not
 * the route's shape is skipped, never guessed at.
 */
export function subjectIndexFrom(rows: unknown): SubjectIndex {
  const out = new Map<string, DeclaredSubject>()
  if (!Array.isArray(rows)) return out
  for (const r of rows) {
    const row = obj(r)
    const id = str(row.descriptor_id)
    if (!id) continue
    const body = obj(row.body)
    const scope = obj(body.scope)
    out.set(id, {
      id,
      name: str(row.name) ?? str(obj(body.identity).name),
      domain: str(scope.domain),
      geo: strings(scope.geo),
      tags: strings(scope.tags),
      state: str(row.state),
    })
  }
  return out
}

let current: SubjectIndex = new Map()
let version = 0

/** The subject roster this session has read (empty until the listing lands). */
export function getSubjectIndex(): SubjectIndex {
  return current
}

/** Bumped on every {@link setSubjectIndex} — a memo key for a derived view. */
export function subjectIndexVersion(): number {
  return version
}

/** Replace the session's subject roster (the hook does this when the listing lands). */
export function setSubjectIndex(index: SubjectIndex): void {
  current = index
  version += 1
}
