/**
 * The registry's light list weights, applied to a fixture recorded at full
 * weight.
 *
 * The fixtures were recorded before the list routes grew `fields=summary`,
 * and `nearestFixture` never lets the weight decide which recording stands in
 * — so a reader that asks `fields=summary` was answered the WHOLE row, and the
 * fixture walk could not see what the light row drops. A live walk
 * failed on exactly that: the subject roster at summary weight dropped
 * `body.sources`, the map read every subject as declaring no source and
 * hatched every place "not covered" — the fixture walk passed.
 *
 * These mirror the registry's own projections, one for one:
 *   - `summaryDescriptorBody` = `legba.data.registry.descriptor_list_weight.summary_body`
 *   - `lightSignalData` / `SIGNAL_SUMMARY_COLUMNS` = `legba.data.registry.signal_list_weight`
 *     (`light_data`, `SignalSummaryRow`'s fields)
 *   - `summaryEventRow` / `EVENT_SUMMARY_COLUMNS` = `legba.data.registry.event_list_weight`
 *     (`summary_row`, `EventSummaryRow`'s fields)
 * and `fixtures/weights_golden.json` holds the cases both sides are tested
 * against (`walk.test.ts` here, `tests/data_pkg/test_list_weight_projections.py`
 * there), so the mirror cannot drift silently.
 */

export const SUMMARY_SCOPE_KEYS = ['domain', 'geo', 'tags', 'geometry', 'themes']
export const DECLARED_SOURCES_KEY = 'declared_sources'
export const LIGHT_DATA_KEYS = ['title', 'title_en', 'published_at', 'geo']
export const LIGHT_GEO_KEYS = ['lat', 'lon']
export const SIGNAL_SUMMARY_COLUMNS = ['id', 'title', 'source_id', 'category', 'language', 'geo', 'tags', 'event_timestamp', 'produced_at', 'data']
export const EVENT_SUMMARY_COLUMNS = ['id', 'title', 'severity', 'lifecycle_state', 'category', 'target_id', 'geo', 'geo_lat', 'geo_lon', 'time_start', 'time_end', 'valid_from', 'produced_at']

const isObj = (v) => v != null && typeof v === 'object' && !Array.isArray(v)

export function summaryDescriptorBody(body) {
  const b = isObj(body) ? body : {}
  const out = {}
  if (isObj(b.identity) && 'name' in b.identity) out.identity = { name: b.identity.name }
  if (isObj(b.scope)) {
    out.scope = {}
    for (const k of SUMMARY_SCOPE_KEYS) if (k in b.scope) out.scope[k] = b.scope[k]
  }
  if (Array.isArray(b.sources)) out[DECLARED_SOURCES_KEY] = b.sources.length
  return out
}

export function lightSignalData(payload) {
  const p = isObj(payload) ? payload : {}
  const out = {}
  for (const k of LIGHT_DATA_KEYS) {
    if (!(k in p)) continue
    if (k === 'geo') {
      const g = p.geo
      const point = {}
      if (isObj(g)) for (const gk of LIGHT_GEO_KEYS) if (gk in g) point[gk] = g[gk]
      if (Object.keys(point).length) out.geo = point
      continue
    }
    out[k] = p[k]
  }
  return out
}

export function summarySignalRow(row) {
  const out = {}
  for (const k of SIGNAL_SUMMARY_COLUMNS) {
    if (k === 'data') out.data = lightSignalData(row.data)
    else if (k in row) out[k] = row[k]
  }
  return out
}

/** An `/v3/events` row at `fields=summary` weight (what the map and the time band draw). */
export function summaryEventRow(row) {
  const out = {}
  for (const k of EVENT_SUMMARY_COLUMNS) if (k in row) out[k] = row[k]
  return out
}

/**
 * The places a subject roster says nobody covers: every subject naming the
 * place declares no source (the whole body's empty `sources`, or the
 * summary's `declared_sources: 0`). A subject that says neither is not
 * counted as declaring none. The map's "not covered" count can be at most
 * this (it also needs the place's outline).
 */
export function placesDeclaringNoSource(rows) {
  const byIso = new Map()
  for (const r of Array.isArray(rows) ? rows : []) {
    const b = isObj(r?.body) ? r.body : {}
    const geo = isObj(b.scope) && Array.isArray(b.scope.geo) ? b.scope.geo : []
    const none = Array.isArray(b.sources) ? b.sources.length === 0 : typeof b[DECLARED_SOURCES_KEY] === 'number' ? b[DECLARED_SOURCES_KEY] === 0 : false
    for (const g of geo) {
      if (typeof g !== 'string') continue
      const iso = g.toUpperCase()
      byIso.set(iso, (byIso.get(iso) ?? true) && none)
    }
  }
  return new Set([...byIso].filter(([, none]) => none).map(([iso]) => iso))
}

/**
 * The answer a summary read gets from a full-weight recording: `/signals` at
 * `fields=summary` (or no `fields`: its default is the light weight),
 * `/registry/descriptors` and `/v3/events` at `fields=summary` (their default
 * is the whole row). Any other answer is returned as it came.
 */
export function atRequestedWeight(pathname, params, body) {
  const fields = params.get('fields')
  if (pathname === '/api/v1/signals' && fields !== 'full' && isObj(body) && Array.isArray(body.data)) {
    return { ...body, data: body.data.map((r) => (isObj(r) ? summarySignalRow(r) : r)) }
  }
  if (pathname === '/api/v1/v3/events' && fields === 'summary' && isObj(body) && Array.isArray(body.data)) {
    return { ...body, data: body.data.map((r) => (isObj(r) ? summaryEventRow(r) : r)) }
  }
  if (pathname === '/api/v1/registry/descriptors' && fields === 'summary') {
    const cut = (r) => (isObj(r) ? { ...r, body: summaryDescriptorBody(r.body) } : r)
    if (Array.isArray(body)) return body.map(cut)
    if (isObj(body) && Array.isArray(body.data)) return { ...body, data: body.data.map(cut) }
  }
  return body
}
