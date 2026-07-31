/**
 * deskNames — target id → human display name (U-2: countries first-class;
 * supply-chain follow-up: thematic lane/flow desks).
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
 * The supply-chain pack's thematic desks (`lane_hormuz`, `flow_semiconductors`,
 * … — `scope.domain: thematic`, see `descriptors/target_lane_*.yaml` /
 * `target_flow_*.yaml`) carry no ISO-2 code to regex out, so they get their own
 * small lookup table (`THEMATIC_DESK_NAMES`) covering every id the pack ships
 * (both the activated lanes and the still-draft ones, so a name is ready the
 * moment an operator flips a future lane to `active`). An id shipped by some
 * future lane not yet in this table still degrades honestly via `humanizeId`'s
 * generic de-prefix fallback below, rather than going unnamed.
 *
 * `humanizeId` is the total fallback used anywhere a substrate id (target,
 * analyst, dimension/unit) needs to render as prose: a recognized country
 * desk resolves to its country name, a recognized thematic desk resolves to
 * its lane/flow name, anything else is de-prefixed, split on separators, and
 * title-cased. Never returns raw snake_case untouched when a split is
 * possible.
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

/** Thematic supply-chain desk id → honest human name. Covers the full pack
 *  (`planning/SUPPLY_CHAIN_PACK_PLAN_2026-07-29.md`) — the three activated
 *  lanes plus the still-`draft` lanes/flows, named ahead of activation so a
 *  future `state: active` flip needs no UI follow-up. */
const THEMATIC_DESK_NAMES: Record<string, string> = {
  lane_hormuz: 'Strait of Hormuz',
  lane_red_sea: 'Red Sea / Bab el-Mandeb',
  lane_malacca_south_china_sea: 'Malacca / South China Sea',
  lane_black_sea: 'Black Sea',
  lane_panama: 'Panama Canal',
  lane_baltic_north_sea: 'Baltic / North Sea',
  flow_semiconductors: 'Semiconductor Supply',
  flow_energy_shipping: 'Energy Shipping',
  flow_critical_minerals: 'Critical Minerals',
  flow_container_freight: 'Container Freight',
}

/** Resolve a thematic supply-chain desk id (`lane_*` / `flow_*`) to its
 *  registered human name, or `null` when the id isn't (yet) in
 *  `THEMATIC_DESK_NAMES` — callers fall back to `humanizeId`'s generic
 *  de-prefix path so an unrecognized future lane never goes unnamed. */
export function thematicDeskName(targetId: string | null | undefined): string | null {
  if (!targetId) return null
  return THEMATIC_DESK_NAMES[targetId.trim()] ?? null
}

/**
 * Humanize ANY substrate id for product prose: a recognized country desk
 * resolves to its country name; a recognized thematic (supply-chain) desk
 * resolves to its lane/flow name; anything else drops a leading
 * `country_g20_`/`country_watch_`/`country_tierN_`/`analyst_` plumbing prefix,
 * splits on `._-`, and title-cases the remainder. An id that carries no
 * splittable content (pure punctuation) is returned unchanged as an honest
 * last resort — this function never fabricates a name.
 */
export function humanizeId(id: string): string {
  const country = countryNameForTargetId(id)
  if (country) return country
  const thematic = thematicDeskName(id)
  if (thematic) return thematic
  const stripped = id
    .replace(/^country_(?:g20_|watch_|tier[0-9]*_)?/i, '')
    .replace(/^analyst_/i, '')
  const words = stripped.replace(/[._-]+/g, ' ').trim()
  return words ? words.replace(/\b\w/g, (c) => c.toUpperCase()) : id
}
