/**
 * layerAperture — the per-country APERTURE DECLARATION, as the map's layer `i`
 * reads it.
 *
 * ## What this closes
 *
 * `docs/LAYERS.md` — the aperture declaration (`desk_apertures`) is
 * TARGET × LAYER → `present` / `absent` / `unmeasured`, curated per desk, and
 * `absent` REQUIRES the operator's own reason (enforced twice: the pydantic
 * model and a table CHECK). The whole point of it is that **a structurally
 * missing layer must never read as agreement**. Today that declaration is
 * legible on exactly one surface, the Layer Divergence panel. The map — which
 * is where a reader actually looks at layers — offered a checkbox and nothing
 * else.
 *
 * So the map's layer switcher gains an `i` per source layer, and this module is
 * what it reads: the aperture for the country in scope, folded out of the
 * divergence receipt the panel already fetches (`GET /v3/layers/divergence`).
 * **No new route** — the same key, the same response, one more reader.
 *
 * ## The distinction the `i` exists to make
 *
 * "Absent" is two very different facts, and the operator's reasons in the live
 * maps say so in their own words ("absent from our sources, not structurally
 * absent"). This module separates them from the receipt rather than from the
 * prose:
 *
 *   * **declared absent, nothing ingested** — no source on this desk's map is
 *     tagged to the layer (`sources_mapped === 0`). The layer is not being
 *     collected here.
 *   * **declared absent, and rows ARE arriving** — the map carries sources on
 *     that layer and the declaration SUPPRESSES their counts
 *     (`counts_suppressed_by_aperture[layer] > 0`). That is a curation
 *     judgement with live data behind it, and it is a much louder statement
 *     than the first.
 *
 * And two further states that are emphatically not "absent":
 *
 *   * **unmeasured** — the honest default. Nobody has looked; it is not a claim
 *     that the layer is missing.
 *   * **undeclared** — the loaded map carries no row for this layer at all.
 *     A loaded map always declares all six (`LayerMapDescriptor` refuses one
 *     that does not), so this state means the map itself is incomplete.
 *
 * A desk with NO loaded layer map is its own answer again — `no_map` — and
 * never an empty table that would read as six absences.
 *
 * Pure: no fetch, no React, no store. Every field is read off the receipt.
 */

import type { LayerDivergenceDesk, LayerDivergenceResponse } from '@/lib/api'

/**
 * `src/legba/data/layers/_vocab.py:LAYER_VOCAB`, in the order that file
 * declares it. The response echoes it as `layer_vocab`, which is what a render
 * should prefer — this constant is the fallback for a receipt that could not be
 * read at all, so the six rows still render as six honest unknowns.
 */
export const LAYER_VOCAB = [
  'official',
  'domestic_press',
  'foreign_press',
  'social_digest',
  'public_data',
  'physical',
] as const

export type SourceLayer = (typeof LAYER_VOCAB)[number]

/** Short labels for the six — the table's own wording, not re-invented. */
export const LAYER_LABEL: Record<string, string> = {
  official: 'Official',
  domestic_press: 'Domestic press',
  foreign_press: 'Foreign press',
  social_digest: 'Social digest',
  public_data: 'Public data',
  physical: 'Physical',
}

/**
 * What the aperture says, plus the two states that are about the READ rather
 * than about the layer.
 */
export type ApertureState =
  | 'present'
  | 'absent'
  | 'unmeasured'
  | 'undeclared'
  /** No layer map is loaded for this desk. */
  | 'no_map'
  /** The divergence receipt itself could not be read. */
  | 'unread'

export interface LayerApertureRow {
  layer: string
  declared: ApertureState
  /** The operator's own words. Non-blank exactly where the vocabulary requires. */
  reason: string
  /** Sources on this desk's map tagged to this layer. Null when there is no map. */
  sourcesMapped: number | null
  /** Rows the map carries on this layer that the declaration suppresses. */
  suppressed: number | null
  /** Days the layer was thin. Null on a layer nobody counted. */
  thinDays: number | null
}

export interface DeskAperture {
  targetId: string | null
  /** ISO-3166-1 alpha-2, as the receipt carries it. */
  country: string | null
  mapVersion: string | null
  /** The UTC day the run measured at. */
  asOf: string | null
  methodVersion: string | null
  rows: LayerApertureRow[]
  /** Why there is no desk here, when there is none. Null when one was found. */
  note: string | null
}

/** Which desk the `i` should describe. Either key resolves; `targetId` wins. */
export interface ApertureKey {
  targetId?: string | null
  /** ISO2, e.g. from the map's own country filter. */
  iso2?: string | null
}

function emptyRows(vocab: readonly string[], declared: ApertureState): LayerApertureRow[] {
  return vocab.map((layer) => ({
    layer,
    declared,
    reason: '',
    sourcesMapped: null,
    suppressed: null,
    thinDays: null,
  }))
}

function findDesk(
  res: LayerDivergenceResponse,
  key: ApertureKey,
): LayerDivergenceDesk | undefined {
  const desks = Array.isArray(res.desks) ? res.desks : []
  if (key.targetId) {
    const hit = desks.find((d) => d.target_id === key.targetId)
    if (hit) return hit
  }
  if (key.iso2) {
    const iso = key.iso2.toUpperCase()
    return desks.find((d) => (d.country ?? '').toUpperCase() === iso)
  }
  return undefined
}

/**
 * Fold the divergence receipt into one desk's six aperture rows.
 *
 * Reads `desks[].layers[<layer>]` for the declaration, its reason and its
 * `sources_mapped`, and `desks[].counts_suppressed_by_aperture[<layer>]` for
 * the rows the declaration is shutting out. A layer the receipt does not carry
 * at all is `undeclared` — never silently `unmeasured`, which is a different
 * and weaker statement.
 */
export function deskAperture(
  res: LayerDivergenceResponse | null | undefined,
  key: ApertureKey,
  failed = false,
): DeskAperture {
  const vocab =
    res && Array.isArray(res.layer_vocab) && res.layer_vocab.length > 0
      ? res.layer_vocab
      : LAYER_VOCAB
  const base = {
    targetId: key.targetId ?? null,
    country: key.iso2 ?? null,
    mapVersion: null,
    asOf: null,
    methodVersion: null,
  }
  if (failed || !res || typeof res !== 'object') {
    return {
      ...base,
      rows: emptyRows(vocab, 'unread'),
      note: 'the divergence receipt could not be read — this is a failed read, not a quiet aperture',
    }
  }
  if (res.measured === false) {
    return {
      ...base,
      rows: emptyRows(vocab, 'unread'),
      note: 'the divergence read itself failed — the aperture is not being reported as empty, it is not being reported',
    }
  }
  if (!key.targetId && !key.iso2) {
    return {
      ...base,
      rows: emptyRows(vocab, 'no_map'),
      note: 'no country is in scope — the aperture is declared per desk, so there is nothing to state until one is',
    }
  }
  const desk = findDesk(res, key)
  if (!desk) {
    const loaded = (Array.isArray(res.desks) ? res.desks : [])
      .map((d) => d.country)
      .filter(Boolean)
      .join(', ')
    return {
      ...base,
      rows: emptyRows(vocab, 'no_map'),
      note:
        'no layer map is loaded for this desk, so its aperture has never been declared' +
        (loaded ? ` — the loaded maps cover ${loaded}` : ''),
    }
  }
  const suppressed = desk.counts_suppressed_by_aperture ?? {}
  const rows: LayerApertureRow[] = vocab.map((layer) => {
    const l = desk.layers?.[layer]
    if (!l) {
      return {
        layer,
        declared: 'undeclared',
        reason: '',
        sourcesMapped: null,
        suppressed: suppressed[layer] ?? null,
        thinDays: null,
      }
    }
    const declared: ApertureState =
      l.declared === 'present' || l.declared === 'absent' || l.declared === 'unmeasured'
        ? l.declared
        : 'undeclared'
    return {
      layer,
      declared,
      reason: l.reason ?? '',
      sourcesMapped: typeof l.sources_mapped === 'number' ? l.sources_mapped : null,
      suppressed: suppressed[layer] ?? null,
      thinDays: l.thin_days ?? null,
    }
  })
  return {
    targetId: desk.target_id ?? key.targetId ?? null,
    country: desk.country ?? key.iso2 ?? null,
    mapVersion: desk.map_version ?? null,
    asOf: res.as_of ?? null,
    methodVersion: res.method_version ?? null,
    rows,
    note: null,
  }
}

/** The badge word for a state — the receipt's vocabulary, never a synonym. */
export const APERTURE_WORD: Record<ApertureState, string> = {
  present: 'present',
  absent: 'absent',
  unmeasured: 'unmeasured',
  undeclared: 'undeclared',
  no_map: 'no map',
  unread: 'unread',
}

/**
 * The `i` popover's sentence for one layer.
 *
 * Every clause is derived from the row and the desk; nothing is written per
 * layer, so the six explanations cannot drift apart. The `absent` branch is the
 * one that does real work: it separates "we do not ingest this here" from "we
 * ingest it and the declaration is suppressing it", which is the difference
 * between a collection gap and a curation judgement.
 */
export function apertureExplainer(row: LayerApertureRow, desk: DeskAperture): string {
  const where = desk.country ?? desk.targetId ?? 'this desk'
  const stamp =
    desk.mapVersion || desk.asOf
      ? ` (${[desk.mapVersion && `map ${desk.mapVersion}`, desk.asOf && `as of ${desk.asOf}`]
          .filter(Boolean)
          .join(', ')})`
      : ''
  switch (row.declared) {
    case 'present':
      return (
        `${LAYER_LABEL[row.layer] ?? row.layer} is declared PRESENT for ${where}: ` +
        `${row.sourcesMapped ?? 0} source${row.sourcesMapped === 1 ? '' : 's'} on this desk's ` +
        `layer map carry it` +
        (row.thinDays != null
          ? `, and it was thin on ${row.thinDays} day${row.thinDays === 1 ? '' : 's'} of the measured window`
          : '') +
        `.${stamp}`
      )
    case 'absent': {
      const ingested = (row.suppressed ?? 0) > 0
      const head = ingested
        ? `${LAYER_LABEL[row.layer] ?? row.layer} is declared ABSENT for ${where} even though rows ARE arriving: ` +
          `${row.suppressed} row${row.suppressed === 1 ? '' : 's'} on this layer are suppressed by the declaration. ` +
          `That is a curation judgement with live data behind it.`
        : `${LAYER_LABEL[row.layer] ?? row.layer} is declared ABSENT for ${where} and nothing is ingested on it: ` +
          `no source on this desk's layer map is tagged to it.`
      return (
        `${head} An absent layer contributes no count, so its silence never reads as agreement. ` +
        (row.reason ? `The operator's reason: "${row.reason}"` : 'No reason is recorded, which the table forbids — read this as a defect.') +
        stamp
      )
    }
    case 'unmeasured':
      return (
        `${LAYER_LABEL[row.layer] ?? row.layer} is UNMEASURED for ${where}: nobody has looked. ` +
        `This is the honest default, not a claim that the layer is missing — an unmeasured layer ` +
        `and an absent one are different findings.` +
        (row.reason ? ` Note: "${row.reason}"` : '') +
        stamp
      )
    case 'undeclared':
      return (
        `${LAYER_LABEL[row.layer] ?? row.layer} is UNDECLARED for ${where}: the loaded map carries ` +
        `no row for it. A loaded map is required to declare all six layers, so this is the map ` +
        `being incomplete rather than the layer being absent.${stamp}`
      )
    case 'no_map':
      return desk.note ?? `No layer map is loaded for ${where}, so nothing has been declared.`
    case 'unread':
      return desk.note ?? 'The aperture could not be read.'
  }
}

/** The switcher's one-line header over the six rows. */
export function apertureSummary(desk: DeskAperture): string {
  if (desk.note) return desk.note
  const counts = { present: 0, absent: 0, unmeasured: 0, undeclared: 0 }
  for (const r of desk.rows) {
    if (r.declared in counts) counts[r.declared as keyof typeof counts]++
  }
  const total = desk.rows.length
  const parts = [`${counts.present} of ${total} present`]
  if (counts.absent) parts.push(`${counts.absent} declared absent`)
  if (counts.unmeasured) parts.push(`${counts.unmeasured} unmeasured`)
  if (counts.undeclared) parts.push(`${counts.undeclared} undeclared`)
  const where = desk.country ?? desk.targetId
  return `${where ? `${where}: ` : ''}${parts.join(' · ')}`
}
