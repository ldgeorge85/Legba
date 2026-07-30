/**
 * deskNames — target id → human display name (U-2: countries first-class).
 *
 * The new-model country desks encode their tier + ISO-2 code in the id itself
 * (`country_g20_br`, `country_watch_sd` — see
 * `scripts/bringup_register_g20_country_targets.py` /
 * `bringup_register_watch_country_targets.py`). Product surfaces (the Desks
 * nav group, the Wall's movers list, the Scorecard) must never show that raw
 * id — this module is the ONE place that turns it back into a country name.
 *
 * Derives from data already in the codebase rather than hand-rolling a second
 * roster: `lib/countryGeo.ts`'s `COUNTRY_BY_ISO2` gazetteer (built for the
 * map/entity-linking backfill) already carries a canonical name for every
 * ISO-3166-1 alpha-2 code the g20 + watch sets use, so resolution is a pure
 * regex-extract + lookup — no new country list to keep in sync.
 *
 * `humanizeId` is the total fallback used anywhere a substrate id (target,
 * analyst, dimension/unit) needs to render as prose: a recognized country
 * desk resolves to its country name, anything else is de-prefixed, split on
 * separators, and title-cased. Never returns raw snake_case untouched when a
 * split is possible.
 */
import { COUNTRY_BY_ISO2 } from './countryGeo'

/** `country_g20_br` / `country_watch_sd` → `br` / `sd`; anything else → null.
 *  Anchored full-match: these ids are minted exactly in this shape by the
 *  bringup scripts, never with extra segments. */
const DESK_TARGET_ID_RE = /^country_(?:g20|watch)_([a-z]{2})$/i

/** Extract the lower-case ISO-2 code from a g20/watch country desk's target
 *  id, or `null` when the id isn't one of those (e.g. a legacy single-country
 *  target like `japan_news`, or a non-country id entirely). */
export function iso2FromTargetId(targetId: string | null | undefined): string | null {
  if (!targetId) return null
  const m = DESK_TARGET_ID_RE.exec(targetId.trim())
  return m ? m[1].toLowerCase() : null
}

/** True when `targetId` is one of the new-model g20/watch country desks. */
export function isDeskTargetId(targetId: string | null | undefined): boolean {
  return iso2FromTargetId(targetId) !== null
}

/** Resolve a g20/watch country desk's target id to its canonical country
 *  name (`country_g20_br` → `"Brazil"`), or `null` when the id doesn't
 *  resolve (not a recognized desk id, or an ISO-2 code outside the
 *  gazetteer). */
export function countryNameForTargetId(targetId: string | null | undefined): string | null {
  const iso2 = iso2FromTargetId(targetId)
  if (!iso2) return null
  return COUNTRY_BY_ISO2[iso2.toUpperCase()]?.name ?? null
}

/**
 * Humanize ANY substrate id for product prose: a recognized country desk
 * resolves to its country name; anything else drops a leading
 * `country_g20_`/`country_watch_`/`country_tierN_`/`analyst_` plumbing prefix,
 * splits on `._-`, and title-cases the remainder. An id that carries no
 * splittable content (pure punctuation) is returned unchanged as an honest
 * last resort — this function never fabricates a name.
 */
export function humanizeId(id: string): string {
  const country = countryNameForTargetId(id)
  if (country) return country
  const stripped = id
    .replace(/^country_(?:g20_|watch_|tier[0-9]*_)?/i, '')
    .replace(/^analyst_/i, '')
  const words = stripped.replace(/[._-]+/g, ' ').trim()
  return words ? words.replace(/\b\w/g, (c) => c.toUpperCase()) : id
}
