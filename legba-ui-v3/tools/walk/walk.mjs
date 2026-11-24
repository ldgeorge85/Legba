#!/usr/bin/env node
/**
 * The first-time-user browser walk — the UI gate (README.md beside this file).
 *
 *   node walk.mjs --mode fixture --dist ../../dist --fixtures fixtures/api.json --out <dir>
 *   node walk.mjs --mode live    --dist /live-dist --registry http://legba-registry:8090 --out <dir> [--record fixtures/api.json]
 *
 * GET ONLY. Every non-GET request to the API is answered here with a 204 and
 * never leaves the browser's sandbox; consult/model routes are refused even as
 * GETs; requests to any other origin are blocked. In live mode the bearer token
 * comes from the LEGBA_TOKEN environment variable and is never printed, logged,
 * written to disk or drawn in a screenshot (every output string is scrubbed).
 * With sign-in on (no bearer reaches a browser), a live walk instead signs in
 * through the real sign-in screen as a named account: LEGBA_WALK_EMAIL and
 * LEGBA_WALK_PASSWORD_FILE (signin.mjs). Only `POST /auth/sign-in` is let
 * through; the session cookie stays in memory; checks that need a higher role
 * than the account's are skipped and named, never failed.
 *
 * Exit 0 = every enforced budget held; 1 = a budget failed (the list is
 * printed); 2 = the walk itself could not run.
 */
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import { DEFAULT_BUDGETS, evaluate, formatReport, heldBy, landingStats } from './budgets.mjs'
import { identifiersFromSource, literalsFromSource, makeSanitizer } from './sanitize.mjs'
import { PICKABLE, pickPlace } from './pick.mjs'
import { walkPhoneShell } from './phone.mjs'
import { withRecordAddress } from './record.mjs'
import { atRequestedWeight, placesDeclaringNoSource } from './weights.mjs'
import { signalPlacement } from './flatSignals.mjs'
import { passwordFromFile, roleFrom, roleGateFor, sessionCookieFrom, skipsForRole } from './signin.mjs'

const require = createRequire(import.meta.url)
const HERE = path.dirname(fileURLToPath(import.meta.url))

// ------------------------------------------------------------------ args
const argv = process.argv.slice(2)
const arg = (name, dflt) => {
  const i = argv.indexOf('--' + name)
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : dflt
}
const flag = (name) => argv.includes('--' + name)
const MODE = arg('mode', 'fixture')
if (!['fixture', 'live'].includes(MODE)) { console.error('--mode must be fixture or live'); process.exit(2) }
const DIST = path.resolve(arg('dist', path.join(HERE, '..', '..', 'dist')))
const FIXTURES = path.resolve(arg('fixtures', path.join(HERE, 'fixtures', 'api.json')))
const REGISTRY = arg('registry', 'http://legba-registry:8090')
const OUT = path.resolve(arg('out', '/tmp/legba-walk'))
const RECORD = arg('record', null)
const PORT = Number(arg('port', '8080'))
const SHOTS = !flag('no-shots')
const CHROME = arg('chrome', process.env.LEGBA_WALK_CHROME || '/ms-playwright/chromium-1148/chrome-linux/chrome')
// live sign-in as a named account (roll 89; signin.mjs): both variables, live only
const WALK_EMAIL = MODE === 'live' ? process.env.LEGBA_WALK_EMAIL || '' : ''
const WALK_PASSWORD_FILE = MODE === 'live' ? process.env.LEGBA_WALK_PASSWORD_FILE || '' : ''
const SIGN_IN = !!(WALK_EMAIL && WALK_PASSWORD_FILE)
let WALK_PASSWORD = ''
if (SIGN_IN) {
  try { WALK_PASSWORD = passwordFromFile(fs.readFileSync(WALK_PASSWORD_FILE, 'utf8')) } catch { WALK_PASSWORD = '' }
  if (!WALK_PASSWORD) { console.error('LEGBA_WALK_PASSWORD_FILE names no readable password'); process.exit(2) }
}
const TOKEN = MODE === 'live' ? (SIGN_IN ? '' : process.env.LEGBA_TOKEN || '') : 'walk-fixture-token'
// the signed-in session: the cookie (name=value, memory only) and the account's role
const session = { cookie: '', role: null, signInStatus: null }
const budgets = { ...DEFAULT_BUDGETS }
if (arg('text-floor')) budgets.textFloorPx = Number(arg('text-floor'))
if (flag('enforce-text-floor')) budgets.enforceTextFloor = true
if (flag('report-text-floor')) budgets.enforceTextFloor = false
if (arg('consult-min')) budgets.consultMinWidthPx = Number(arg('consult-min'))
if (arg('ready-ms')) budgets.landingReadyMs = Number(arg('ready-ms'))
if (MODE === 'live' && !TOKEN && !SIGN_IN) { console.error('live mode needs LEGBA_TOKEN, or LEGBA_WALK_EMAIL and LEGBA_WALK_PASSWORD_FILE, in the environment'); process.exit(2) }
if (RECORD && SIGN_IN) { console.error('--record needs the bearer (a signed-in account may see less than the fixtures hold)'); process.exit(2) }
if (RECORD && MODE !== 'live') { console.error('--record needs --mode live'); process.exit(2) }
if (!fs.existsSync(path.join(DIST, 'index.html'))) { console.error(`no built bundle at ${DIST} (index.html missing)`); process.exit(2) }

const hide = (s, secret) => (secret && secret.length > 3 ? s.split(secret).join('***') : s)
const scrub = (s) => {
  let out = hide(String(s), TOKEN.length > 8 ? TOKEN : '')
  out = hide(out, WALK_PASSWORD)
  const cookieValue = session.cookie.slice(session.cookie.indexOf('=') + 1)
  out = hide(out, cookieValue)
  return out.replace(/Bearer [A-Za-z0-9._-]+/g, 'Bearer ***')
}
const log = (...a) => console.log(scrub(a.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join(' ')))
fs.mkdirSync(OUT, { recursive: true })

// ------------------------------------------------------------------ static bundle
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.geojson': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ico': 'image/x-icon', '.map': 'application/json', '.webmanifest': 'application/manifest+json' }
// P-1 — the phone app step can take the static server "down" (offline: every
// connection is dropped) and publish a "new version" of the worker script (the
// same file plus a trailing comment: byte-different, so the browser installs it).
const staticState = { down: false, swBump: 0 }
const server = http.createServer((req, res) => {
  if (staticState.down) return req.socket.destroy()
  const p = decodeURIComponent(req.url.split('?')[0])
  const fp = path.join(DIST, p)
  if (!fp.startsWith(DIST)) { res.writeHead(403); return res.end() }
  if (p === '/m/sw.js' && staticState.swBump && fs.existsSync(fp)) {
    res.writeHead(200, { 'content-type': 'text/javascript', 'cache-control': 'no-cache' })
    return res.end(fs.readFileSync(fp, 'utf8') + `\n// walk: version bump ${staticState.swBump}\n`)
  }
  fs.stat(fp, (err, st) => {
    if (!err && st.isFile()) {
      res.writeHead(200, { 'content-type': MIME[path.extname(fp)] || 'application/octet-stream' })
      fs.createReadStream(fp).pipe(res)
    } else {
      const idx = p.startsWith('/m') ? path.join(DIST, 'm', 'index.html') : path.join(DIST, 'index.html')
      res.writeHead(200, { 'content-type': 'text/html' })
      fs.createReadStream(idx).pipe(res)
    }
  })
})
server.on('upgrade', (_req, socket) => socket.destroy()) // the event stream is not walked
const BASE = `http://localhost:${PORT}`

// ------------------------------------------------------------------ API answering
const fixtures = MODE === 'fixture' ? JSON.parse(fs.readFileSync(FIXTURES, 'utf8')) : null
// U-9: the Watch surface's INVENTED example answers (`fixtures/watch.json`,
// hand-written, never recorded) are laid over the recorded set — the alert
// log, the source edits, and a source roster that carries every quiet class.
const WATCH_FIXTURES = path.join(path.dirname(FIXTURES), 'watch.json')
if (fixtures && fs.existsSync(WATCH_FIXTURES)) Object.assign(fixtures.entries, JSON.parse(fs.readFileSync(WATCH_FIXTURES, 'utf8')).entries)
// M-2: the phone shell's INVENTED answers (`fixtures/phone.json`): what needs
// you on `/v3/since?include=needs_you`, the seen cursor's "sign-in is off",
// the alert episodes' acknowledgement state, and the situations' records.
const PHONE_FIXTURES = path.join(path.dirname(FIXTURES), 'phone.json')
if (fixtures && fs.existsSync(PHONE_FIXTURES)) Object.assign(fixtures.entries, JSON.parse(fs.readFileSync(PHONE_FIXTURES, 'utf8')).entries)
// RA-1: the reader's own reads. The fixtures were recorded with the bearer
// against the admin lists; the reader routes answer the same rows CUT to the
// reader's shape (`reader_reads_api.py`), derived here in memory so a fixture
// walk reads exactly what a reader would: `/v3/roster?family=` from the
// recorded descriptor and source lists, `/v3/watch/alerts` from the alert log.
const ROSTER_SCOPE_KEYS = ['domain', 'geo', 'tags', 'geometry', 'themes']
function rosterRow(r, family) {
  const body = r?.body && typeof r.body === 'object' ? r.body : {}
  const out = { descriptor_id: r.descriptor_id, family, name: r.name, kind: r.kind ?? null, state: r.state, version: r.version, is_head: true, created_at: r.created_at, body: {}, geo: [], languages: [], tags: [], assurance_grade: null }
  if (body.identity && 'name' in body.identity) out.body.identity = { name: body.identity.name }
  if (body.scope && typeof body.scope === 'object') out.body.scope = Object.fromEntries(ROSTER_SCOPE_KEYS.filter((k) => k in body.scope).map((k) => [k, body.scope[k]]))
  if (Array.isArray(body.sources)) out.body.declared_sources = body.sources.length
  if (family === 'source') {
    if (body.identity?.id) out.body.identity = { ...(out.body.identity ?? {}), id: body.identity.id }
    out.geo = r.geo ?? []; out.languages = r.languages ?? []; out.tags = r.tags ?? []; out.assurance_grade = r.assurance_grade ?? null
  }
  return out
}
if (fixtures) {
  const E = fixtures.entries
  for (const [family, from, limit] of [
    ['target', '/api/v1/registry/descriptors?family=target&head_only=true&limit=500', 500],
    ['analyst', '/api/v1/registry/descriptors?family=analyst&head_only=true&limit=500', 500],
    ['source', '/api/v1/registry/sources', 1000],
  ]) {
    const key = `/api/v1/v3/roster?family=${family}&limit=${limit}`
    if (!(key in E) && E[from]?.status === 200 && Array.isArray(E[from].body)) E[key] = { status: 200, body: E[from].body.map((r) => rosterRow(r, family)) }
  }
  for (const k of Object.keys(E)) {
    if (!k.startsWith('/api/v1/v3/system/alerts/log')) continue
    const alias = k.replace('/api/v1/v3/system/alerts/log', '/api/v1/v3/watch/alerts')
    if (alias in E || E[k].status !== 200) continue
    const b = E[k].body ?? {}
    const strip = (e) => { const { raised_by, ...rest } = e; const a = rest.attention ?? {}; return { ...rest, attention: { acked_at: a.acked_at ?? null, ack_active: !!a.ack_active, snoozed_until: a.snoozed_until ?? null, snooze_option: a.snooze_option ?? null, snooze_active: !!a.snooze_active, resurfaced: !!a.resurfaced, hidden_from_needs_you: !!a.hidden_from_needs_you } } }
    E[alias] = { status: 200, body: { ...b, rule_path: undefined, rule_error: undefined, open: (b.open ?? []).map(strip), closed: (b.closed ?? []).map(strip), paged: (b.paged ?? []).map(({ outcomes, raised_by, ...p }) => p) } }
  }
}
// path → [{ params, entry }]: a request whose exact query was not recorded gets
// the recorded entry for its path that shares the most query parameters
const fixtureByPath = new Map()
// P-8: these list routes answered FULL rows when `fields` was omitted, and the
// fixtures were recorded then; since P-8 the console names `fields=full` on
// every reader that needs the whole row. A recorded key on one of these routes
// with no `fields` is therefore the full weight: alias it under `&fields=full`
// (in memory) so an explicit full read finds the same answer it always did.
const FULL_BY_NAME = /^\/api\/v1\/(findings|journal|signals|analysts\/[^/]+\/outputs)$/
if (fixtures) for (const [k, v] of Object.entries({ ...fixtures.entries })) {
  const [p, q = ''] = k.split('?')
  if (FULL_BY_NAME.test(p) && !/(^|&)fields=/.test(q)) {
    const alias = `${p}?${q ? q + '&' : ''}fields=full`
    if (!(alias in fixtures.entries)) fixtures.entries[alias] = v
  }
}
if (fixtures) for (const [k, v] of Object.entries(fixtures.entries)) {
  const [p, q = ''] = k.split('?')
  if (!fixtureByPath.has(p)) fixtureByPath.set(p, [])
  fixtureByPath.get(p).push({ params: new Set(q.split('&').filter(Boolean)), entry: v })
}
// the same normal form the recorder wrote: decoded path segments, decoded `k=v` pairs
const decodedPath = (u) => u.pathname.split('/').map((seg) => { try { return decodeURIComponent(seg) } catch { return seg } }).join('/')
const decodedPairs = (u) => [...u.searchParams].map(([k, v]) => `${k}=${v}`)
// a path recorded for ANOTHER id of the same route (`/targets/<a>/brief` for
// `/targets/<b>/brief`): the same shape stands in, and the walk notes it
function sameRoute(path) {
  const segs = path.split('/')
  for (const p of fixtureByPath.keys()) {
    const o = p.split('/')
    if (o.length !== segs.length) continue
    let diff = 0
    for (let i = 0; i < o.length && diff < 2; i++) if (o[i] !== segs[i]) diff++
    if (diff === 1) return p
  }
  return null
}
// U-10: a route whose id the fixture cannot know in advance (the Record
// sheet opens on whatever record the walk clicks) is answered by a PATTERN
// (`fixtures.routes`: a path regex and the query pairs it must carry), before
// the same-route stand-in. Matched in order; the first that fits answers.
const fixtureRoutes = (fixtures?.routes ?? []).map((r) => ({ re: new RegExp(r.path), query: r.query ?? [], entry: r.entry }))
function routeFixture(u) {
  const p = decodedPath(u)
  const pairs = new Set(decodedPairs(u))
  const hit = fixtureRoutes.find((r) => r.re.test(p) && r.query.every((q) => pairs.has(q)))
  return hit ? hit.entry : null
}
/**
 * Roll 84 (P-8): `GET /findings/{id}` answers ONE full row. The fixtures were
 * recorded before the route existed, so the row is taken from a recorded
 * `/findings` page that holds it at full weight (a row with `data`), else any.
 */
function findingByIdFixture(u) {
  const m = /^\/api\/v1\/findings\/([^/]+)$/.exec(decodedPath(u))
  if (!m) return null
  const id = m[1]
  let fallback = null
  for (const [k, e] of Object.entries(fixtures.entries)) {
    if (!k.startsWith('/api/v1/findings?') || e.status !== 200) continue
    const list = Array.isArray(e.body) ? e.body : (e.body?.data ?? [])
    const row = list.find((r) => r && String(r.id) === id)
    if (row && row.data) return { status: 200, body: row }
    if (row) fallback ??= { status: 200, body: row }
  }
  return fallback ?? { status: 404, body: { detail: 'no such finding' } }
}
function nearestFixture(u) {
  let path = decodedPath(u)
  if (!fixtureByPath.has(path)) {
    const alt = sameRoute(path)
    if (!alt) return null
    net.standIns.add(`${path} ← ${alt}`)
    path = alt
  }
  const list = fixtureByPath.get(path)
  // `fields=` (the row weight) is not a filter: it never decides WHICH
  // recording stands in, only breaks a tie (an equal weight wins it), so a
  // reader that now names its weight finds the recording it found before.
  const isWeight = (x) => x.startsWith('fields=')
  const want = decodedPairs(u)
  const wantFilters = want.filter((x) => !isWeight(x))
  const wantWeight = want.find(isWeight)
  let best = null, score = -1
  for (const c of list) {
    const sc = wantFilters.filter((x) => c.params.has(x)).length
      + (wantWeight && c.params.has(wantWeight) ? 0.5 : 0)
    if (sc > score) { best = c.entry; score = sc }
  }
  return best
}
// The map's time-cut layers (events, incidents at sea) are answered with their
// timestamps moved forward by (now − the fixture's `map_clock`), so the walk
// draws the same marks in the same window whenever it runs. Nothing else moves.
const REBASED = new Set([
  '/api/v1/v3/events', '/api/v1/v3/map/at_sea',
  // U-9: the Watch answers move with the same clock, so "3 h ago" stays 3 h ago
  '/api/v1/v3/system/alerts/log', '/api/v1/v3/watch/alerts', '/api/v1/v3/system/source-firing', '/api/v1/v3/evidence/drift/flags', '/api/v1/v3/evidence/drift/history',
])
//
// Roll 84: in fixture mode the BROWSER'S CLOCK is pinned to the fixtures'
// clock instead (`pinBrowserClock`, every context): `Date` in the page starts
// at `map_clock` and runs at real speed. The fixtures' other time-relative
// answers — the Board's "changed since" wash (`/v3/since`), the reads' ages,
// the cursors the page computes from "now" — were recorded at that time, and
// with the real clock they drifted out of their windows as the days passed:
// "map: a place selected — no changed area on screen to click" failed in five
// lanes from the evening of 2026-11-14, a day after the recording. With the
// browser at the recording time the rebase above is not needed (shift 0).
const CLOCK_MS = MODE === 'fixture' && fixtures?.map_clock ? Date.parse(fixtures.map_clock) : null
const MAP_SHIFT_MS = CLOCK_MS != null ? 0 : (fixtures?.map_clock ? Date.now() - Date.parse(fixtures.map_clock) : 0)
const rebase = (body) => (MAP_SHIFT_MS ? body.replace(/"(\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2}))"/g, (m, iso) => {
  const t = Date.parse(iso)
  return Number.isFinite(t) ? `"${new Date(t + MAP_SHIFT_MS).toISOString()}"` : m
}) : body)
const REFUSED_GET = /\/consult\/(ask|stream|turn)|deep_consult|\/model\/|\/llm\//
const net = { total: 0, paths: new Map(), refusedWrites: [], blockedOutside: 0, unfixtured: new Set(), standIns: new Set(), slow: [], inflight: 0, refusedGets: [], log: [], phase: 'start', override: new Map(), signalRows: [], roster: null }
// roll 86: what the map was served — the subject roster (who declares a
// source), and the /signals rows while the flat map is walked
const noteSignals = (u, text) => {
  // RA-1: the console reads the reader's roster (`/v3/roster`); the admin list
  // is still noted when a Registry panel reads it
  if ((u.pathname === '/api/v1/v3/roster' || u.pathname === '/api/v1/registry/descriptors') && u.searchParams.get('family') === 'target' && !net.roster) {
    try { const b = JSON.parse(text); net.roster = Array.isArray(b) ? b : (b?.data ?? null) } catch { /* not JSON */ }
    return
  }
  // the first page only: the layer keeps at most 500 placed rows
  // (mapData SIGNAL_MAX_ROWS), so a later page's tail is served and not drawn
  if (u.pathname !== '/api/v1/signals' || net.phase !== 'flat map' || u.searchParams.has('cursor')) return
  try { const b = JSON.parse(text); for (const r of b?.data ?? []) net.signalRows.push({ id: r.id, data: { geo: r.data?.geo ?? null, published_at: r.data?.published_at ?? null } }) } catch { /* not JSON */ }
}
const WALK_T0 = Date.now()
const recorded = new Map()
let hold = null // { promise, release, discard }
const startHold = () => { let release; const promise = new Promise((r) => (release = r)); hold = { promise, release, discard: false } }
/**
 * Release the held answers. `discard` (a held page that is about to close):
 * the held requests are refused in the browser and NEVER forwarded — before
 * U-5 they reached the live registry after the page had closed, a whole
 * landing wave nobody read, landing on the registry just as the timed landing
 * began (the "first wave twice" of the 2026-11-13 live measurement).
 */
const endHold = ({ discard = false } = {}) => { if (hold) { hold.discard = discard; hold.release(); hold = null } }

/**
 * Roll 89: the one write a signed-in live walk lets through — the sign-in
 * itself, from the real sign-in screen. The registry's Set-Cookie is kept in
 * memory (never handed to the page, never printed); every later GET of a
 * signed-in context carries it.
 */
async function signInThrough(route, req, u, ctxState) {
  try {
    const resp = await fetch(REGISTRY + u.pathname + u.search, {
      method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' }, body: req.postData() ?? '',
    })
    const buf = Buffer.from(await resp.arrayBuffer())
    session.signInStatus = resp.status
    const cookie = resp.ok ? sessionCookieFrom(resp.headers.getSetCookie?.() ?? []) : ''
    if (cookie) {
      session.cookie = cookie
      ctxState.signedIn = true
      try { session.role = roleFrom(JSON.parse(buf.toString('utf8'))) } catch { /* not JSON */ }
    }
    return await route.fulfill({ status: resp.status, contentType: resp.headers.get('content-type') || 'application/json', body: buf })
  } catch (e) {
    session.signInStatus = 'error'
    return route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ walk_proxy_error: 'sign-in did not reach the registry' }) }).catch(() => {})
  }
}

async function answerApi(route, withToken, ctxState = {}) {
  const req = route.request()
  const u = new URL(req.url())
  // P-3: "offline" means the API is unreachable too — the browser's routing
  // would otherwise still answer from the fixtures with the context offline.
  if (staticState.down) return route.abort('internetdisconnected')
  if (SIGN_IN && ctxState.allowSignIn && req.method() === 'POST' && u.pathname === '/api/v1/auth/sign-in') {
    return signInThrough(route, req, u, ctxState)
  }
  if (!['GET', 'HEAD'].includes(req.method())) {
    net.refusedWrites.push(`${req.method()} ${u.pathname}`)
    return route.fulfill({ status: 204, body: '' })
  }
  if (REFUSED_GET.test(u.pathname)) {
    net.refusedGets.push(u.pathname)
    return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"refused by the walk: no consult or model calls"}' })
  }
  net.total++
  net.paths.set(u.pathname, (net.paths.get(u.pathname) || 0) + 1)
  net.inflight++
  // the request log the landing budgets read (path + query, never a header)
  const rec = { phase: net.phase, url: u.pathname + u.search, start: Date.now() - WALK_T0, ms: null, status: null, bytes: 0 }
  net.log.push(rec)
  const done = (status, bytes) => { rec.ms = Date.now() - WALK_T0 - rec.start; rec.status = status; rec.bytes = bytes }
  try {
    if (hold) {
      const h = hold
      await h.promise
      if (h.discard) { done('discarded', 0); return await route.abort('aborted').catch(() => {}) }
    }
    if (MODE === 'fixture') {
      if (!withToken) { done(401, 26); return await route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"not signed in"}' }) }
      const pairs = decodedPairs(u)
      const key = decodedPath(u) + (pairs.length ? '?' + pairs.join('&') : '')
      // --no-map-routes: the registry before it is rebuilt with the map's three
      // read routes (U-4) — they answer 404 and the console falls back
      if (flag('no-map-routes') && u.pathname.startsWith('/api/v1/v3/map/')) {
        done(404, 22)
        return await route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' })
      }
      // a step's own answer for one path (U-9: the edits region before any re-read)
      const hit = net.override.get(u.pathname) ?? fixtures.entries[key] ?? routeFixture(u) ?? findingByIdFixture(u) ?? nearestFixture(u)
      await new Promise((r) => setTimeout(r, 20))
      if (!hit) {
        net.unfixtured.add(u.pathname)
        done(404, 0)
        return await route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"not in the walk fixtures"}' })
      }
      // roll 86: a summary read gets the summary row, not the full recording (weights.mjs)
      let body = typeof hit.body === 'string' ? hit.body : JSON.stringify(atRequestedWeight(u.pathname, u.searchParams, hit.body))
      if (REBASED.has(u.pathname)) body = rebase(body)
      noteSignals(u, body)
      done(hit.status ?? 200, Buffer.byteLength(body))
      return await route.fulfill({ status: hit.status ?? 200, contentType: hit.contentType ?? 'application/json', body })
    }
    // a step's own answer for one path holds live too (roll 86): the reader's
    // /auth/me on the phone, the empty drift summary — the step asserts how the
    // console answers THAT answer, so it is the same answer on both runs
    const ov = net.override.get(u.pathname)
    if (ov) {
      const body = typeof ov.body === 'string' ? ov.body : JSON.stringify(ov.body)
      done(ov.status ?? 200, Buffer.byteLength(body))
      return await route.fulfill({ status: ov.status ?? 200, contentType: ov.contentType ?? 'application/json', body })
    }
    const headers = { ...req.headers() }
    delete headers.host
    delete headers.cookie
    delete headers.authorization
    if (SIGN_IN) { if ((withToken || ctxState.signedIn) && session.cookie) headers.cookie = session.cookie }
    else if (withToken) headers.authorization = 'Bearer ' + TOKEN
    const t0 = Date.now()
    const resp = await fetch(REGISTRY + u.pathname + u.search, { method: req.method(), headers })
    const buf = Buffer.from(await resp.arrayBuffer())
    const dt = Date.now() - t0
    done(resp.status, buf.length)
    // roll 89: with an account, a 403 is the account's role, not the console:
    // the route is counted by name and kept out of the request budgets
    if (SIGN_IN && resp.status === 403) {
      result.roleRefused[u.pathname] = (result.roleRefused[u.pathname] || 0) + 1
      const i = net.log.lastIndexOf(rec)
      if (i >= 0) net.log.splice(i, 1)
    }
    if (resp.ok) noteSignals(u, buf.toString('utf8'))
    if (dt > 3000) net.slow.push(`${u.pathname} ${dt} ms`)
    const ct = resp.headers.get('content-type') || ''
    if (RECORD && withToken && ct.includes('json') && !recorded.has(u.pathname + u.search)) {
      try { recorded.set(u.pathname + u.search, { status: resp.status, body: JSON.parse(buf.toString('utf8')) }) } catch { /* not JSON */ }
    }
    const h = {}
    resp.headers.forEach((v, k) => { if (!['content-encoding', 'transfer-encoding', 'content-length', 'set-cookie'].includes(k)) h[k] = v })
    return await route.fulfill({ status: resp.status, headers: h, body: buf })
  } catch (e) {
    return route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ walk_proxy_error: String(e).slice(0, 80) }) }).catch(() => {})
  } finally {
    net.inflight--
  }
}

// ------------------------------------------------------------------ browser helpers
const { chromium } = require('playwright-core')
const result = {
  mode: MODE, base: MODE === 'live' ? `live bundle ${DIST} + ${REGISTRY}` : `bundle ${DIST} + fixtures ${path.basename(FIXTURES)}`,
  at: new Date().toISOString(), textFloorPx: budgets.textFloorPx,
  steps: [], skipped: [], roleRefused: {}, longTasks: [], unresponsive: [], overflow: [], surfaces: [], consult: [], zeroBeforeRead: [], errors: [], phoneMachineIds: [], flow: [],
  layout: [], drags: [], consultMoves: [],
}
const errorsSeen = new Set()
// the operator side's four sections (U-2 shell); everything else is a reader surface
const OPERATOR_SURFACES = new Set(['engine', 'evaluation', 'gate', 'investigate'])
const ENGINE_WORDS = [/\bsubstrate\b/i, /\bspine\b/i, /\bC-A R\d/, /severity_delta/, /meta_finding/, /prompt_module_candidate/, /no declared scale/i, /\barm that grades\b/i]
const MACHINE_ID = /^[a-z][a-z0-9]*(_[a-z0-9]+){2,}$/
// Routes the console reads whose 404 is a DESIGNED state, each a declared seam:
// the browser logs the 404 as a console error, which is not a defect here.
// Empty since E-3 served /v3/eval/runs (its designed empty state is a 200 with
// `runs: []`; a 404 there is now a registry older than the route, an error).
//   /v3/me/seen — the seen cursor with sign-in off (S-1): "seen_cursor_unavailable"; the phone keeps it on the device
const DESIGNED_404 = ['/api/v1/v3/me/seen']

/**
 * Pin the page's wall clock: `Date.now()` / `new Date()` start at `startMs` and
 * run at real speed. Only `Date` moves — timers, `requestAnimationFrame` and
 * `performance.now()` stay real, so the map's ready stamp, the long-task
 * observer and every budget measure exactly as before.
 */
async function pinBrowserClock(ctx, startMs) {
  await ctx.addInitScript((target) => {
    const Real = Date
    const off = target - Real.now()
    const now = () => Real.now() + off
    // A Proxy, not a subclass: `Date()` called without `new` still returns a
    // string, `instanceof Date` and the prototype are the real ones.
    globalThis.Date = new Proxy(Real, {
      construct: (t, a, nt) => (a.length ? Reflect.construct(t, a, nt) : Reflect.construct(t, [now()], nt)),
      apply: () => new Real(now()).toString(),
      get: (t, p, r) => (p === 'now' ? now : Reflect.get(t, p, r)),
    })
  }, startMs)
}

async function context(browser, opts, { withToken = true, allowSignIn = false } = {}) {
  const ctx = await browser.newContext(opts)
  const ctxState = { allowSignIn, signedIn: false }
  if (CLOCK_MS != null) await pinBrowserClock(ctx, CLOCK_MS + (Date.now() - WALK_T0))
  if (withToken && !SIGN_IN) await ctx.addInitScript((tok) => { try { localStorage.setItem('legba_token', tok) } catch { /* private mode */ } }, TOKEN)
  await ctx.addInitScript(() => {
    window.__lt = []
    try { new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__lt.push({ start: e.startTime, dur: e.duration }) }).observe({ type: 'longtask', buffered: true }) } catch { /* unsupported */ }
  })
  await ctx.route('**/*', async (route) => {
    const u = new URL(route.request().url())
    if (u.origin !== BASE) { net.blockedOutside++; return route.abort('blockedbyclient') }
    if (u.pathname.startsWith('/api/')) return answerApi(route, withToken, ctxState)
    return route.continue()
  })
  return ctx
}

function wire(page, where) {
  page.on('pageerror', (e) => { const m = `${where}: page error: ${String(e).slice(0, 160)}`; if (!errorsSeen.has(m)) { errorsSeen.add(m); result.errors.push(scrub(m)) } })
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    const t = m.text()
    const loc = m.location()?.url || ''
    if (/WebSocket/i.test(t)) return // the event stream is not walked
    if (/ERR_INTERNET_DISCONNECTED/.test(t)) return // P-3: the phone app's offline step cuts the API on purpose
    if (/ERR_BLOCKED_BY_CLIENT|blockedbyclient/i.test(t) || (loc && !loc.startsWith(BASE))) return // outside origins are blocked by design
    if (/status of 503/.test(t) && net.refusedGets.length) return // a consult route the walk refused
    if (SIGN_IN && /status of 403/.test(t)) return // the account's role: counted per route in result.roleRefused
    if (where === 'sign-in' && /status of 401/.test(t)) return // a 401 is how the sign-in screen is reached
    if (/status of 404/.test(t) && loc && DESIGNED_404.includes(new URL(loc).pathname)) return // a declared, not-yet-served route: its 404 is the designed answer
    const msg = `${where}: ${t.slice(0, 160)}${loc && /Failed to load/.test(t) ? ' ' + new URL(loc).pathname : ''}`
    if (!errorsSeen.has(msg)) { errorsSeen.add(msg); result.errors.push(scrub(msg)) }
  })
}

const withTimeout = (p, ms, label) => Promise.race([p, new Promise((_, j) => setTimeout(() => j(new Error('UNRESPONSIVE ' + label)), ms))])

async function stepLongTasks(page, name, since) {
  try {
    const lt = await withTimeout(page.evaluate((t0) => { const l = (window.__lt || []).filter((e) => e.start >= t0); return { n: l.length, max: Math.round(Math.max(0, ...l.map((e) => e.dur))) } }, since), 10000, name)
    result.longTasks.push({ step: name, maxMs: lt.max, count: lt.n })
    return lt
  } catch (e) {
    result.unresponsive.push(name)
    return null
  }
}
const now = (page) => withTimeout(page.evaluate(() => performance.now()), 10000, 'clock').catch(() => 0)

async function quiet(page, maxMs = 30000, quietMs = 1000) {
  const t0 = Date.now()
  let since = null
  while (Date.now() - t0 < maxMs) {
    if (net.inflight === 0) { since ??= Date.now(); if (Date.now() - since >= quietMs) return Date.now() - t0 }
    else since = null
    await page.waitForTimeout(100)
  }
  return null
}

async function shot(page, name) {
  if (!SHOTS) return
  await page.screenshot({ path: path.join(OUT, name + '.png') }).catch(() => {})
}

/** Visible text metrics for the current view. */
async function measure(page, name, { reader, width, within = null }) {
  const m = await withTimeout(page.evaluate(({ floor, engineSrc, idSrc, within }) => {
    const engine = engineSrc.map(([s, f]) => new RegExp(s, f))
    const idRe = new RegExp(idSrc)
    const vis = (el) => {
      const r = el.getBoundingClientRect()
      if (r.width < 1 || r.height < 1 || r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) return false
      const cs = getComputedStyle(el)
      return cs.visibility !== 'hidden' && cs.display !== 'none' && +cs.opacity > 0.05
    }
    let textEls = 0, small = 0
    const smallSamples = []
    const engineHits = new Set()
    const ids = []
    const root = (within && document.querySelector(within)) || document.body
    for (const el of root.querySelectorAll('*')) {
      if (el.closest('script,style,svg')) continue
      const own = [...el.childNodes].filter((n) => n.nodeType === 3 && n.textContent.trim()).map((n) => n.textContent.trim()).join(' ')
      if (!own || !vis(el)) continue
      textEls++
      const fsz = parseFloat(getComputedStyle(el).fontSize)
      if (fsz < floor) { small++; if (smallSamples.length < 80) smallSamples.push(`${fsz}px "${own.slice(0, 30)}"`) }
      for (const re of engine) { const mm = own.match(re); if (mm) engineHits.add(mm[0]) }
      if (idRe.test(own)) ids.push(own)
    }
    for (const el of document.querySelectorAll('textarea,input')) {
      if (!vis(el)) continue
      for (const re of engine) { const mm = (el.placeholder || '').match(re); if (mm) engineHits.add(mm[0] + ' (placeholder)') }
    }
    return {
      textEls, smallText: small, smallSamples, engineWords: [...engineHits], machineIds: ids.slice(0, 10),
      tiles: document.querySelectorAll('.dv-tab').length, groups: document.querySelectorAll('.dv-groupview').length,
      docWidth: document.documentElement.scrollWidth,
    }
  }, { floor: budgets.textFloorPx, engineSrc: ENGINE_WORDS.map((r) => [r.source, r.flags]), idSrc: MACHINE_ID.source, within }), 10000, name).catch(() => null)
  if (!m) { result.unresponsive.push('measure ' + name); return null }
  result.surfaces.push({ name, width, reader, ...m })
  result.overflow.push({ where: name, width, docWidth: m.docWidth })
  return m
}

/** Elements printing a zero while every API answer is still held (nothing has been read). */
async function zerosBeforeRead(page, where) {
  const z = await withTimeout(page.evaluate(() => {
    const re = /(^|[\s(·:])0(?=$|[\s)·%,/])/
    const out = []
    for (const el of document.querySelectorAll('body *')) {
      const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join('').trim()
      if (!own || own.length > 80) continue
      const r = el.getBoundingClientRect()
      if (r.width < 1 || r.height < 1) continue
      if (re.test(own)) out.push(own.replace(/\s+/g, ' ').slice(0, 60))
    }
    return out
  }), 10000, 'zeros ' + where).catch(() => [])
  for (const t of z) result.zeroBeforeRead.push({ where, text: scrub(t) })
}

async function consultBox(page) {
  return withTimeout(page.evaluate(() => {
    const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 }
    const ctl = document.querySelector('[data-testid="consult-composer-controls"]')
    let t = null
    if (ctl) { let p = ctl.parentElement; while (p && !t) { t = p.querySelector('textarea'); p = p.parentElement } }
    t ??= [...document.querySelectorAll('textarea')].find(vis) ?? null
    if (!t || !vis(t)) return null
    return Math.round(t.getBoundingClientRect().width)
  }), 10000, 'consult').catch(() => null)
}

/**
 * The frame's geometry at this moment (U-6): the widest full-height strip of
 * the main area with no content in it, the median size of its visible text,
 * and the chat's reading width in rem of the root size.
 */
async function layoutMetrics(page, where, width) {
  const m = await withTimeout(page.evaluate(() => {
    const root = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16
    const main = document.querySelector('[data-testid="surface-main"]')
    if (!main) return null
    const mr = main.getBoundingClientRect()
    const spans = []
    const sizes = []
    for (const el of main.querySelectorAll('*')) {
      if (el.closest('script,style')) continue
      const r = el.getBoundingClientRect()
      if (r.width < 1 || r.height < 1 || r.bottom <= mr.top || r.top >= mr.bottom || r.right <= mr.left || r.left >= mr.right) continue
      const cs = getComputedStyle(el)
      if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity < 0.05) continue
      const own = [...el.childNodes].filter((n) => n.nodeType === 3 && n.textContent.trim()).map((n) => n.textContent.trim()).join(' ')
      const tag = el.tagName.toUpperCase()
      const media = /^(CANVAS|IMG|VIDEO|TEXTAREA|INPUT|SELECT|BUTTON)$/.test(tag) || (tag === 'SVG' && r.width > 8)
      if (own || media) spans.push([Math.max(r.left, mr.left), Math.min(r.right, mr.right)])
      if (own && !el.closest('svg')) sizes.push([parseFloat(cs.fontSize), own.length])
    }
    // The map draws on a canvas the shell portals to <body> (over the dock), so
    // canvases count wherever they live, by where they sit on screen.
    for (const c of document.querySelectorAll('canvas')) {
      const r = c.getBoundingClientRect()
      if (r.width < 8 || r.height < 8 || r.right <= mr.left || r.left >= mr.right || r.bottom <= mr.top || r.top >= mr.bottom) continue
      spans.push([Math.max(r.left, mr.left), Math.min(r.right, mr.right)])
    }
    spans.sort((a, b) => a[0] - b[0])
    let cur = mr.left
    let gap = 0
    for (const [l, r] of spans) {
      if (l > cur) gap = Math.max(gap, l - cur)
      cur = Math.max(cur, r)
    }
    gap = Math.max(gap, mr.right - cur)
    sizes.sort((a, b) => a[0] - b[0])
    const total = sizes.reduce((n, x) => n + x[1], 0)
    let acc = 0
    let median = null
    for (const [px, n] of sizes) {
      acc += n
      if (acc >= total / 2) { median = px; break }
    }
    const vis = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 ? r : null }
    const chat = vis(document.querySelector('[data-testid="consult-scroll"]')) ?? vis(document.querySelector('[data-testid="consult-question"]'))
    return {
      root,
      emptyBandPx: Math.round(gap),
      bodyTextPx: median == null ? null : Math.round(median * 10) / 10,
      chatWidthPx: chat ? Math.round(chat.width) : null,
      chatWidthRem: chat ? Math.round((chat.width / root) * 10) / 10 : null,
    }
  }), 10000, 'layout ' + where).catch(() => null)
  if (!m) { result.unresponsive.push('layout ' + where); return null }
  result.layout.push({ where, viewport: width, ...m })
  return m
}

/** A box's size, by selector. */
async function box(page, sel) {
  const el = page.locator(sel).first()
  return (await el.boundingBox().catch(() => null)) ?? null
}

/** Drag the centre of `sel` by (dx, dy), in steps, as a hand would. */
async function drag(page, sel, dx, dy) {
  const b = await box(page, sel)
  if (!b) return false
  const x = b.x + b.width / 2
  const y = b.y + b.height / 2
  await page.mouse.move(x, y)
  await page.mouse.down()
  for (let i = 1; i <= 8; i++) await page.mouse.move(x + (dx * i) / 8, y + (dy * i) / 8)
  await page.mouse.up()
  await page.waitForTimeout(500)
  return true
}

/** The shell is drawn: the surface bar (U-2) or, failing that, a Dockview tab. */
/** How long the landing's request window stays open after "ready" (ms). */
const LANDING_SETTLE_MS = 3000

const SHELL_READY = '[data-testid="surface-bar"] [data-testid^="surface-tab-"], .dv-tab'

const surfaceTabs = (page) => page.$$eval('[data-testid^="surface-tab-"]', (ts) =>
  ts.map((t) => ({ id: t.getAttribute('data-testid').replace(/^surface-tab-/, ''), label: t.textContent.trim().slice(0, 30), testid: t.getAttribute('data-testid') })))

/** Walk every surface tab now in the bar: measure, screenshot, note where Flow is. */
async function walkSurfaces(page, tabs, prefix) {
  let flow = null
  for (const t of tabs) {
    const since = await now(page)
    // U-11: the Ledger's FIRST open is its own request phase (its reads, cold)
    const prevPhase = net.phase
    if (t.id === 'ledger' && !net.ledgerOpened) { net.ledgerOpened = true; net.phase = 'ledger-open' }
    const clicked = await page.click(`[data-testid="${t.testid}"]`, { timeout: 8000 }).then(() => true).catch(() => false)
    if (!clicked) { net.phase = prevPhase; step(`surface ${t.id}`, false, 'tab did not respond'); continue }
    await quiet(page, 25000)
    await page.waitForTimeout(800)
    net.phase = prevPhase
    await stepLongTasks(page, `surface ${t.id}`, since)
    await measure(page, `surface ${t.id}`, { reader: !OPERATOR_SURFACES.has(t.id), width: page.viewportSize()?.width ?? 1680 })
    const hasFlow = await page.locator('.dv-tab', { hasText: /^\s*Flow\s*$/ }).count().catch(() => 0)
    if (hasFlow && !flow) flow = t
    await shot(page, `${prefix}-${t.id}`)
    step(`surface ${t.id}`, true)
  }
  return flow
}

/**
 * Wait until the Board map is SETTLED — on its own signals, never on a clock
 * (roll 84: "a place selected" and "did not reach the stage at 3840" flaked in
 * three lanes under load because the walk slept a fixed time and then acted on
 * a map that was still flying back from the reset, re-reading a changed window
 * or re-laying out after a move). Settled = the map's own ready stamp
 * (`data-ready="1"` with `data-ready-at` set), the maplibre instance loaded
 * with its tiles, and the camera not moving. `timeoutMs` is a ceiling on
 * WAITING, not a budget: the map's ready time is read from its stamp.
 */
async function mapSettled(page, timeoutMs = 30000) {
  return page.waitForFunction(() => {
    const host = document.querySelector('[data-testid="globe-map"]')
    if (!host || host.getAttribute('data-ready') !== '1' || !host.getAttribute('data-ready-at')) return false
    const mm = host.querySelector('.maplibregl-map')?.__legbaMap
    if (!mm) return false
    return mm.loaded() && mm.areTilesLoaded() && !mm.isMoving() && !mm.isZooming() && !mm.isRotating()
  }, null, { timeout: timeoutMs, polling: 100 }).then(() => true).catch(() => false)
}

/** The ISO codes the map's accent outline (`sel-area`) names — the selection as drawn. */
const SEL_ISOS = () => {
  const c = document.querySelector('[data-testid="globe-map"] .maplibregl-map')
  const f = c && c.__legbaMap && c.__legbaMap.getFilter('sel-area')
  const lit = Array.isArray(f) ? f[f.length - 1] : null
  return Array.isArray(lit) && lit[0] === 'literal' && Array.isArray(lit[1]) ? lit[1].map(String) : []
}

/**
 * The map step (U-4): the Board's globe ready, spun and zoomed by real input,
 * one layer toggled on, one place selected and cleared. Records the ready
 * time, the longest main-thread task while spinning and zooming, and the marks
 * drawn. A map drawn as SVG (no WebGL2) is walked the same way, minus the camera.
 */
async function walkMap(page, t0) {
  const m = { renderer: null, readyMs: null, marks: null, marksAfterToggle: null, spinLongTaskMs: null, toggleLongTaskMs: null, selected: null, projection: null }
  const map = page.locator('[data-testid="globe-map"]').first()
  if (!(await map.count().catch(() => 0))) {
    // the globe flag off: the earlier flat map stands on the Board instead
    const flat = (await page.locator('[data-testid="maplibre-world"]').count().catch(() => 0)) > 0
    result.map = { ...m, absent: true, renderer: flat ? 'flat (globe flag off)' : null }
    step('map: the Board map draws', flat, flat ? 'the globe flag is off: the flat map' : 'no map on the Board')
    return
  }
  const ready = await page.waitForSelector('[data-testid="globe-map"][data-ready="1"]', { timeout: 30000 }).then(() => true).catch(() => false)
  const attr = (k) => map.getAttribute(k).catch(() => null)
  // U-8: the map stamps when it was first ready (`data-ready-at`, ms from
  // navigation). The walk only LOOKS after the landing's ready, its settle and
  // its screenshot, so the old figure (when the walk saw it) is kept beside it
  // as `seenMs` and never stands in for the map's own time when the stamp is there.
  m.seenMs = ready ? Date.now() - t0 : null
  const stamped = Number(await attr('data-ready-at'))
  m.readyMs = ready ? (Number.isFinite(stamped) && stamped > 0 ? stamped : m.seenMs) : null
  m.renderer = await attr('data-renderer')
  m.projection = await attr('data-projection')
  m.software = (await attr('data-software')) === '1'
  m.marks = Number(await attr('data-marks'))
  step('map: the Board map is ready', ready, ready ? `${m.renderer}, ${m.projection ?? 'drawn'}, ${m.marks} marks, ready ${m.readyMs} ms from navigation` : 'never ready')
  // roll 86: "not covered" only where the roster says no source (the summary
  // roster once dropped `sources`: every place hatched, over every change)
  {
    const fig = await page.locator('[data-testid="globe-layer-figure-blanks"]').textContent().catch(() => null)
    const shown = Number(/(\d+) uncovered/.exec(fig ?? '')?.[1] ?? NaN)
    const most = net.roster ? placesDeclaringNoSource(net.roster).size : null
    m.uncovered = { shown: Number.isFinite(shown) ? shown : null, rosterAtMost: most }
    const ok = Number.isFinite(shown) && most != null && shown <= most
    step('map: "not covered" only where the roster declares no source', ok,
      !Number.isFinite(shown) ? `the blanks figure was not read (${fig ?? 'absent'})` : most == null ? 'the walk saw no subject roster'
        : `${shown} places hatched "not covered"; the roster declares no source for ${most}`)
  }
  const box = await map.boundingBox().catch(() => null)
  if (box && m.renderer === 'webgl') {
    // spin: three drags across the globe, then zoom in until it is flat, and back out
    const since = await now(page)
    const cx = box.x + box.width / 2
    const cy = box.y + box.height / 2
    for (let i = 0; i < 3; i++) {
      await page.mouse.move(cx - 150, cy)
      await page.mouse.down()
      await page.mouse.move(cx + 150, cy + 20, { steps: 12 })
      await page.mouse.up()
      await page.waitForTimeout(300)
    }
    for (let i = 0; i < 6; i++) { await page.mouse.wheel(0, -400); await page.waitForTimeout(250) }
    await page.waitForTimeout(800)
    m.zoomedProjection = await page.evaluate(() => {
      const c = document.querySelector('[data-testid="globe-map"] .maplibregl-map')
      const mm = c && c.__legbaMap
      return mm ? { zoom: Math.round(mm.getZoom() * 10) / 10 } : null
    }).catch(() => null)
    await shot(page, 'W03b-map-zoomed')
    await page.click('[data-testid="globe-reset"]').catch(() => {})
    // the reset flies the camera home: wait for the flight to end, not a clock
    await mapSettled(page)
    const lt = await stepLongTasks(page, 'map: spin and zoom', since)
    m.spinLongTaskMs = lt?.max ?? null
  }
  // one layer toggled: events and situations, over a 7-day window
  {
    const since = await now(page)
    if (!(await page.locator('[data-testid="globe-layer-toggle-events"]').count().catch(() => 0))) await page.click('[data-testid="globe-layers-button"]').catch(() => {})
    await page.click('[data-testid="time-band"] [aria-label="Window span"] button:has-text("7d")').catch(() => {})
    const ok = await page.click('[data-testid="globe-layer-toggle-events"]', { timeout: 5000 }).then(() => true).catch(() => false)
    const drawn = ok && (await page.waitForSelector('[data-testid="globe-layer-figure-events"][data-state="read"]', { timeout: 20000 }).then(() => true).catch(() => false))
    await quiet(page, 15000)
    await page.waitForTimeout(1500)
    m.marksAfterToggle = Number(await attr('data-marks'))
    m.eventsFigure = await page.locator('[data-testid="globe-layer-figure-events"]').textContent().catch(() => null)
    const lt = await stepLongTasks(page, 'map: a layer toggled', since)
    m.toggleLongTaskMs = lt?.max ?? null
    await shot(page, 'W03c-map-events-on')
    step('map: a layer toggled on draws', drawn, drawn ? `events and situations: ${m.eventsFigure}; ${m.marksAfterToggle} marks` : 'the layer did not read')
    await page.click('[data-testid="globe-layer-toggle-events"]').catch(() => {})
    await page.click('[data-testid="time-band"] [aria-label="Window span"] button:has-text("24h")').catch(() => {})
    // the layer off and the window back redraw the map: let the reads land
    await quiet(page, 15000)
    if (m.renderer === 'webgl') await mapSettled(page)
  }
  // one place selected: a subject's area on the map, then cleared with Escape
  if (m.renderer === 'webgl') {
    // a pixel a hand can click: the map's canvas on top, a washed land area the
    // topmost pickable feature (pick.mjs — the U-8 fix: the old pick clicked
    // under the map's own layer card)
    // pick only on a settled map: a pixel read while the camera moves is not
    // the pixel the click lands on
    await mapSettled(page)
    const pt = await page.evaluate(`(${pickPlace.toString()})(document, ${JSON.stringify(PICKABLE)})`).catch(() => null)
    if (!pt) step('map: a place selected', false, 'no changed area on screen to click')
    else {
      await page.mouse.click(pt.x, pt.y)
      // wait on the selection as the map draws it (the accent outline naming a
      // place), not on a clock
      const sel = await page.waitForFunction(`(${SEL_ISOS.toString()})().length > 0`, null, { timeout: 10000, polling: 100 })
        .then(() => true).catch(() => false)
      const pointLine = await page.locator('[data-testid="consult-dock"]').innerText().catch(() => '')
      m.selected = { iso: pt.iso, ring: sel }
      await shot(page, 'W03d-map-place-selected')
      step('map: a place selected', sel, sel ? `${pt.iso} outlined in the accent; Consult reads ${/asking about[^\n]*/i.exec(pointLine)?.[0]?.slice(0, 80) ?? '(no point line)'}` : 'no accent outline after the click')
      await page.keyboard.press('Escape').catch(() => {})
      // cleared = the outline names nothing again
      await page.waitForFunction(`(${SEL_ISOS.toString()})().length === 0`, null, { timeout: 5000, polling: 100 }).catch(() => {})
    }
  }
  // The globe itself, on a browser without a GPU (where the map opens flat):
  // switched on, settled, its longest task REPORTED (never enforced — a
  // software renderer's frame time is the host's CPU, not the console), then
  // switched back so the reader's stored choice stays flat.
  if (m.renderer === 'webgl' && m.software && m.projection === 'flat') {
    const t0g = await now(page)
    const ok = await page.click('[data-testid="globe-projection-globe"]', { timeout: 5000 }).then(() => true).catch(() => false)
    if (ok) {
      await page.waitForTimeout(4000)
      await shot(page, 'W03e-map-globe-software')
      m.globeOnSoftwareLongTaskMs = await withTimeout(page.evaluate((t) => Math.round(Math.max(0, ...(window.__lt || []).filter((e) => e.start >= t).map((e) => e.dur))), t0g), 15000, 'globe probe').catch(() => null)
      await page.click('[data-testid="globe-projection-flat"]').catch(() => {})
      await page.waitForTimeout(1500)
    }
  }
  result.map = m
}

/**
 * Roll 86: the flat World map (the globe flag off, `?globe=0`) draws each
 * signal at its own point — the `/signals` rows it was served, read back from
 * what the map rendered (flatSignals.mjs). Its own page, so the desktop
 * page's map, layers and stored choices are not touched.
 */
async function walkFlatSignals(ctx) {
  const p = await ctx.newPage(); wire(p, 'flat map')
  const prev = net.phase
  net.phase = 'flat map'
  net.signalRows = []
  try {
    await p.goto(BASE + '/?globe=0', { timeout: 30000 }).catch(() => {})
    const ready = await p.waitForFunction(() => {
      const c = document.querySelector('[data-testid="maplibre-world"] .maplibregl-map')
      const mm = c && c.__legbaMap
      return !!(mm && mm.getLayer('legba-signal-clusters') && mm.queryRenderedFeatures({ layers: ['legba-signal-clusters'] }).length > 0)
    }, null, { timeout: 45000, polling: 250 }).then(() => true).catch(() => false)
    await quiet(p, 15000)
    const r = ready ? await withTimeout(p.evaluate(`(${signalPlacement.toString()})(document, ${JSON.stringify(net.signalRows)}, 'legba-signal-clusters', ${24 * 3600 * 1000}, 0.1)`), 15000, 'flat signals').catch(() => null) : null
    await shot(p, 'W03f-flat-map-signals')
    result.flatSignals = r
    const ok = !!r && r.expected > 0 && r.missingCount === 0
    step('map (flat): signal points drawn at their own place', ok,
      !ready ? 'the flat map drew no signal point' : !r ? 'could not read the map' : r.expected === 0 ? `no served row with a point in the window (${net.signalRows.length} rows served)`
        : `${r.expected - r.missingCount}/${r.expected} places with a point drawn there (${r.drawn} marks)${r.missingCount ? `; missing e.g. ${r.missing.map((m) => `${m.lat.toFixed(2)},${m.lon.toFixed(2)}`).join(' ')}` : `; e.g. ${r.sample.lat.toFixed(2)},${r.sample.lon.toFixed(2)}`}`)
  } finally {
    net.phase = prev
    net.signalRows = []
    await p.close().catch(() => {})
  }
}

/**
 * The map as a frame panel (U-7): at one screen and one place (a panel
 * column, the stage, the Subject inset) — the map drawn with its data, and
 * its chrome (the layer card or its button, the legend or its button, the
 * projection switch, the zoom and reset controls, the attribution) inside the
 * map, clear of every panel header and the top bar, the layer card and the
 * legend clear of each other and of the controls. Pushed to result.mapFits.
 */
async function mapFit(page, where, width) {
  // the globe flag off: the earlier flat map stands in, and is not measured here
  await page.waitForSelector('[data-testid="globe-map"], [data-testid="maplibre-world"]', { timeout: 20000 }).catch(() => {})
  if (!(await page.locator('[data-testid="globe-map"]').count().catch(() => 0)) && (await page.locator('[data-testid="maplibre-world"]').count().catch(() => 0))) {
    ;(result.mapFits ??= []).push({ where, width, ok: true, issues: [], size: 'flat map (globe flag off)', layers: '-', legend: '-', projection: 'mercator', zoom: null })
    return null
  }
  const ready = await page.waitForSelector('[data-testid="globe-map"][data-ready="1"]', { timeout: 30000 }).then(() => true).catch(() => false)
  const g = await withTimeout(page.evaluate(() => {
    const vis = (el) => {
      if (!el) return null
      const r = el.getBoundingClientRect()
      return r.width > 0 && r.height > 0 ? { x: Math.round(r.left), y: Math.round(r.top), r: Math.round(r.right), b: Math.round(r.bottom) } : null
    }
    const maps = [...document.querySelectorAll('[data-testid="globe-map"]')].map(vis).filter(Boolean)
    const map = maps.sort((a, b) => (b.r - b.x) * (b.b - b.y) - (a.r - a.x) * (a.b - a.y))[0] ?? null
    const q = (sel) => vis(document.querySelector(sel))
    const host = document.querySelector('[data-testid="globe-map"]')
    const chrome = {
      layers: q('[data-testid="globe-layer-card"]') ?? q('[data-testid="globe-layers-button"]'),
      layersOpen: !!document.querySelector('[data-testid="globe-layer-card"]'),
      legend: q('[data-testid="globe-legend"]') ?? q('[data-testid="globe-legend-button"]'),
      legendOpen: !!document.querySelector('[data-testid="globe-legend"]'),
      projection: q('[data-testid="globe-projection"]'),
      controls: q('[data-testid="globe-reset"]'),
      attribution: vis(host?.querySelector('.maplibregl-ctrl-attrib')),
    }
    const headers = [...document.querySelectorAll('.dv-tabs-and-actions-container, [data-testid="surface-bar"]')].map(vis).filter(Boolean)
    const mm = host?.querySelector('.maplibregl-map')?.__legbaMap
    return { map, chrome, headers, projection: host?.getAttribute('data-projection'), zoom: mm ? Math.round(mm.getZoom() * 100) / 100 : null }
  }), 10000, 'map fit').catch(() => null)
  const issues = []
  if (!ready) issues.push('the map never became ready')
  if (!g || !g.map) issues.push('no map on screen')
  else {
    const over = (a, b) => a && b && a.x < b.r && b.x < a.r && a.y < b.b && b.y < a.b
    const inside = (a) => a && a.x >= g.map.x - 1 && a.r <= g.map.r + 1 && a.y >= g.map.y - 1 && a.b <= g.map.b + 1
    for (const [k, b] of Object.entries(g.chrome)) {
      if (!b || typeof b !== 'object') continue
      if (!inside(b)) issues.push(`${k} outside the map`)
      if (g.headers.some((h) => over(h, b))) issues.push(`${k} under a panel header`)
    }
    if (over(g.chrome.layers, g.chrome.legend)) issues.push('the layer card and the legend overlap')
    if (over(g.chrome.layers, g.chrome.projection) || over(g.chrome.layers, g.chrome.controls)) issues.push('the layer card covers the controls')
    if (over(g.chrome.legend, g.chrome.attribution)) issues.push('the legend covers the attribution')
  }
  const row = {
    where, width, ok: issues.length === 0, issues,
    size: g?.map ? `${g.map.r - g.map.x}×${g.map.b - g.map.y}` : null,
    layers: g?.chrome?.layersOpen ? 'open' : 'folded', legend: g?.chrome?.legendOpen ? 'open' : 'folded',
    projection: g?.projection ?? null, zoom: g?.zoom ?? null,
  }
  ;(result.mapFits ??= []).push(row)
  log(`MAP FIT ${where}: ${row.ok ? 'ok' : issues.join('; ')} — ${row.size} px, layer card ${row.layers}, legend ${row.legend}, ${row.projection} z${row.zoom}`)
  return row
}

/** Send the map's region to the stage (the header's to-stage button on the map's group). */
async function mapToStage(page) {
  // roll 84: act on signals, not clocks. The old step clicked the tab and the
  // to-stage button with 5 s click timeouts straight after the surface switch
  // and slept 1.5 s; at 3840 under load the Board's dock had not laid out yet,
  // a click timed out, and the step read "the map did not reach the stage".
  // Now: the Board's map is ready on its own stamp first, the tab and the
  // button are waited for (attached and visible), and the move is done when a
  // map fills the stage AND that map is ready (a moved map may re-mount).
  await page.waitForSelector('[data-testid="globe-map"][data-ready="1"], [data-testid="maplibre-world"]', { timeout: 30000 }).catch(() => {})
  const tab = page.locator('.dv-tab', { hasText: /World Map|Map/ }).first()
  await tab.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {})
  // the map must be the group's front tab
  await tab.click({ timeout: 15000 }).catch(() => {})
  const btn = page.locator('.dv-groupview', { has: page.locator('.dv-tab', { hasText: /World Map|Map/ }) }).locator('[data-testid="region-to-stage"]').first()
  const visible = await btn.waitFor({ state: 'visible', timeout: 15000 }).then(() => true).catch(() => false)
  if (!visible) return false
  const ok = await btn.click({ timeout: 15000 }).then(() => true).catch(() => false)
  if (!ok) return false
  return page.waitForFunction(() => {
    const st = document.querySelector('[data-testid="stage"]')
    if (!st) return false
    const s = st.getBoundingClientRect()
    return [...document.querySelectorAll('[data-testid="globe-map"], [data-testid="maplibre-world"]')].some((e) => {
      const r = e.getBoundingClientRect()
      const filled = r.width > s.width * 0.8 && r.left >= s.left - 2 && r.right <= s.right + 2
      return filled && (e.getAttribute('data-testid') === 'maplibre-world' || e.getAttribute('data-ready') === '1')
    })
  }, null, { timeout: 30000, polling: 100 }).then(() => true).catch(() => false)
}

/**
 * The Watch surface (U-9), opened by its address on a fresh page at 1680: its
 * own landing's requests (the `watch` phase; budgets.mjs), the alert log with
 * rows, a filter that changes them without asking again, a row's subject made
 * the point, the silences on both sides of the line, a flag opened with both
 * versions; then — the answer the server gives until the first re-reads land —
 * the edits region empty, saying why, and reading no list.
 */
async function walkWatch(browser, desk) {
  const w = { requests: null, bytes: null, largest: null, duplicates: null, rows: null, shown: null, expectPaged: null, filtered: null, filteredShown: null, filteredUnpaged: null, filterAsked: null, point: false, silences: null, sides: [], flagsShown: null, flagsApi: null, flags: null, passages: false, empty: false, emptyListRead: null }
  {
    const c = await context(browser, desk(1680, 1050))
    const p = await c.newPage(); wire(p, 'watch')
    net.phase = 'watch'
    await p.goto(`${BASE}/#ws=watch`, { timeout: 30000 }).catch(() => {})
    const drawn = await p.waitForSelector('[data-testid="watch-alert-log"][data-read="1"]', { timeout: 30000 }).then(() => true).catch(() => false)
    await quiet(p, 30000)
    await p.waitForTimeout(LANDING_SETTLE_MS)
    net.phase = 'after-watch'
    const L = landingStats(net.log, 'watch')
    Object.assign(w, { requests: L.requests, bytes: L.bytes, largest: L.largest, duplicates: L.duplicateRequests })
    step('Watch: the alert log draws', drawn, drawn ? undefined : 'no alert log read on the Watch stage')
    await stepLongTasks(p, 'Watch', 0)
    await measure(p, 'watch', { reader: true, width: 1680 })
    await shot(p, 'W14-watch-1680')
    const count = (sel) => p.locator(sel).count().catch(() => 0)
    w.rows = await count('[data-testid="watch-alert-row"]')
    // What the filters select, not what is drawn: the list draws at most one
    // page (40), so on live data "Paged" can select 63 and still draw 40 (roll
    // 84). The log says how many rows its filters select and how many of those
    // are paged; "Paged" must then select exactly those, draw only paged rows,
    // and ask nothing again — true on the fixtures and on any live window.
    const shownAttr = (name) => p.locator('[data-testid="watch-alert-log"]').getAttribute(name).then((v) => (v == null ? null : Number(v))).catch(() => null)
    w.shown = await shownAttr('data-shown')
    w.expectPaged = await shownAttr('data-shown-paged')
    const before = net.log.length
    await p.click('[data-testid="watch-filter-paged-paged"]', { timeout: 5000 }).catch(() => {})
    await p.waitForTimeout(400)
    w.filtered = await count('[data-testid="watch-alert-row"]')
    w.filteredShown = await shownAttr('data-shown')
    w.filteredUnpaged = await count('[data-testid="watch-alert-row"][data-paged="0"]')
    w.filterAsked = net.log.slice(before).filter((r) => r.url.includes('/alerts/log')).length
    step(
      'Watch: a filter narrows the log without asking again',
      w.rows > 0 && w.shown != null && w.expectPaged != null && w.filteredShown === w.expectPaged && w.filteredUnpaged === 0 && w.filterAsked === 0,
      `${w.shown ?? '?'} selected (${w.rows} drawn) → ${w.filteredShown ?? '?'} selected (${w.filtered} drawn, ${w.filteredUnpaged} not paged); ${w.expectPaged ?? '?'} paged expected; ${w.filterAsked} new log reads`,
    )
    await p.click('[data-testid="watch-filter-paged-any"]', { timeout: 5000 }).catch(() => {})
    const link = p.locator('[data-testid="watch-alert-list"] [data-testid="watch-subject-link"]').first()
    await link.click({ timeout: 5000 }).catch(() => {})
    w.point = await p.waitForSelector('[data-testid="watch-alert-list"] [data-testid="watch-subject-link"][data-point="1"]', { timeout: 5000 }).then(() => true).catch(() => false)
    step("Watch: a row's subject becomes the point", w.point, w.point ? undefined : 'no row link took the accent')
    await p.keyboard.press('Escape').catch(() => {})
    w.silences = await count('[data-testid="watch-silence-row"]')
    w.sides = await p.$$eval('[data-testid="watch-silence-row"]', (rs) => [...new Set(rs.map((r) => r.getAttribute('data-side')))]).catch(() => [])
    step('Watch: silences say which side of the line each is on', w.silences > 0 && w.sides.includes('looked') && w.sides.includes('could_not_look'), `${w.silences} rows; sides ${w.sides.join(', ')}`)
    // The open-flag count the page shows must be the count the API returns;
    // a flag is opened only when there is one (live data may have none).
    w.flagsShown = await p.locator('[data-testid="watch-edits-open"][data-read="1"]').textContent({ timeout: 8000 }).then((t) => Number(t)).catch(() => null)
    w.flagsApi = await withTimeout(p.evaluate(async () => {
      const tok = localStorage.getItem('legba_token')
      const r = await fetch('/api/v1/v3/evidence/drift?window_hours=168', { headers: tok ? { authorization: 'Bearer ' + tok } : {} })
      if (!r.ok) return null
      const b = await r.json()
      return b && b.available && typeof b.open_flags_total === 'number' ? b.open_flags_total : null
    }), 15000, 'drift summary').catch(() => null)
    w.flags = await count('[data-testid="watch-edit-flag"]')
    if (w.flags > 0) {
      await p.locator('[data-testid="watch-edit-open"]').first().click({ timeout: 5000 }).catch(() => {})
      w.passages = await p.waitForSelector('[data-testid="watch-edit-versions"]', { timeout: 8000 }).then(() => true).catch(() => false)
    }
    const flagsOk = w.flagsApi != null && w.flagsShown === w.flagsApi && (w.flagsApi === 0 ? w.flags === 0 : w.flags > 0 && w.passages)
    step(
      'Watch: the open-flag count is the one the API returns, and a flag opens with both versions',
      flagsOk,
      `${w.flagsShown ?? '?'} shown, ${w.flagsApi ?? '?'} from the API; ${w.flags} listed${w.flags > 0 ? `; versions ${w.passages ? 'read' : 'not shown'}` : ''}`,
    )
    await shot(p, 'W14-watch-flag-open')
    await p.click('[data-testid="watch-edits-tab-record"]', { timeout: 5000 }).catch(() => {})
    await p.waitForSelector('[data-testid="watch-edit-record"]', { timeout: 8000 }).catch(() => {})
    await shot(p, 'W14-watch-edit-record')
    await c.close()
  }
  {
    // until the first re-reads land the summary is all zeros (the live answer of 2026-11-14)
    const c = await context(browser, desk(1680, 1050))
    const p = await c.newPage(); wire(p, 'watch (no edits yet)')
    net.override.set('/api/v1/v3/evidence/drift', { status: 200, body: { version: '2026-11/d2', available: true, window_hours: 168, classified: 0, by_class: {}, by_source: {}, flags_by_source: {}, flags: {}, open_flags_total: 0, assessments_by_type: {}, history_rewrites: 0, auto_closed: { window: 0, total: 0, reopened: 0 }, late_unexplained_changes: 0, would_auto_close: { window: 0, total: 0, rule_version: null, switch_on: false } } })
    const before = net.log.length
    await p.goto(`${BASE}/#ws=watch`, { timeout: 30000 }).catch(() => {})
    w.empty = await p.waitForSelector('[data-testid="watch-edits-empty"]', { timeout: 30000 }).then(() => true).catch(() => false)
    await quiet(p, 20000)
    w.emptyListRead = net.log.slice(before).filter((r) => r.url.includes('/evidence/drift/flags')).length
    net.override.delete('/api/v1/v3/evidence/drift')
    step('Watch: with no edits yet, the region says what will appear and why, and reads no list', w.empty && w.emptyListRead === 0, `${w.empty ? 'said' : 'not said'}; ${w.emptyListRead} flag-list reads`)
    await shot(p, 'W14-watch-edits-empty')
    await c.close()
  }
  result.watch = w
}

/*
 * M-0a — the phone Consult. The answer is MOCKED in the browser (the page's own
 * route, ahead of the context's GET-only answerer): no consult request reaches
 * the registry in either mode, and no model is called. Invented filler with
 * the markdown a real answer carries (bold, a list, a pipe table) and three
 * row references bound to invented cited refs.
 */
const PHONE_ANSWER_REFS = ['a1b2c3d4-0000-4000-8000-000000000001', 'b2c3d4e5-0000-4000-8000-000000000002', 'c3d4e5f6-0000-4000-8000-000000000003']
const PHONE_ANSWER = {
  answer: '**Short answer.** Filler words stand where the analysis would be, because this is a synthetic fixture walk [a1b2c3d4].\n\n' +
    '- The first point carries a citation and runs long enough to wrap across two lines on a phone screen [b2c3d4e5].\n' +
    '- The second point names a place and a window.\n\n' +
    '| Measure | Last 7 d | Prior 7 d |\n|---|---|---|\n| Events | 41 | 33 |\n| Sources | 12 | 9 |\n\n' +
    'A closing paragraph of plain filler text that a reader would scroll through on a small screen, with one more citation [c3d4e5f6].',
  finding_id: null, derived_from: [], tool_calls: [],
  cited_refs: PHONE_ANSWER_REFS.map((id, i) => ({ kind: i === 1 ? 'finding' : 'signal', id, description: `Filler record ${i + 1}` })),
  uncertainty: 0.3, unanswered_aspects: [], session_id: 'walk-session', model: 'core', status: 'complete',
}

/*
 * M-1 — the phone turn is the desktop's: the POST answers 202 and the answer
 * arrives on the step stream (`/consult/stream/{id}`), with the turn contract
 * (here carrying a cost stop, so the block reads "Partial answer"). The second
 * question's stream is HELD open so Stop can be pressed; stop and synthesize
 * are answered here too. All of it is mocked in the page: nothing leaves it.
 */
const PHONE_CONTRACT = {
  schema: 'consult_contract/1', armed: true,
  window: { start: '2026-11-07T08:00:00+00:00', end: '2026-11-14T08:00:00+00:00', hours: 168, source: 'console', basis: 'console 168h', deictic: false },
  grounding_mode: 'default',
  grounding: { mode: 'default', applied: true, counts: { kept_with_rows: 3, cut_no_row: 0, labelled_inference: 1 } },
  cost_stop: "> **Stopped for cost:** filler — drilling stopped after round 4 because this synthetic turn's cost ceiling leaves no room.",
}
const PHONE_SALVAGE = { answer: '**Answered from what it found so far.** Filler text for the stopped turn [a1b2c3d4].', cited_refs: PHONE_ANSWER.cited_refs, synthesis_status: 'complete', model: 'core' }
const sse = (frames) => frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('')

async function phoneConsult(p, width, { answer }) {
  const out = { width, composer: null, answer: null }
  const turn = { posts: 0, streams: 0, stops: 0, synths: 0 }
  let releaseHeld = null
  await p.route('**/api/v1/consult', (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    turn.posts++
    let rid = 'walk'
    try { rid = JSON.parse(route.request().postData() || '{}').request_id || rid } catch {}
    turn.lastBody = route.request().postData() || ''
    return route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ answer: '', status: 'accepted', request_id: rid, session_id: 'walk-session', finding_id: null, derived_from: [], tool_calls: [], cited_refs: [] }) })
  })
  await p.route('**/api/v1/consult/stream/**', async (route) => {
    turn.streams++
    if (turn.streams === 1) {
      const words = PHONE_ANSWER.answer.split(/(?<= )/)
      const frames = [
        { type: 'step', phase: 'act', kind: 'tool_call', tool: 'search_signals', round: 1 },
        ...words.map((w) => ({ type: 'step', kind: 'answer_delta', text: w })),
        { type: 'final', output_id: 'walk' },
        { type: 'final', final_source: 'registry', status: 'complete', session_id: 'walk-session', response: { ...PHONE_ANSWER, contract: PHONE_CONTRACT } },
      ]
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse(frames) })
    }
    // the second question: held open until Stop has been pressed
    await new Promise((r) => (releaseHeld = r))
    return route.abort('aborted').catch(() => {})
  })
  await p.route('**/api/v1/consult/runs/**', (route) => {
    const req = route.request()
    if (req.method() !== 'POST') return route.fallback()
    if (req.url().endsWith('/stop')) { turn.stops++; return route.fulfill({ status: 200, contentType: 'application/json', body: '{"stopped":true}' }) }
    if (req.url().endsWith('/synthesize')) { turn.synths++; turn.synthBody = req.postData() || ''; return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PHONE_SALVAGE) }) }
    return route.fallback()
  })
  // M-2: Consult is a tab of the phone shell (the sheet opens in it); the
  // September sheet's own toggle is the fallback for an older bundle.
  const opened = (await p.locator('[data-testid="tab-consult"]').count().catch(() => 0))
    ? await p.click('[data-testid="tab-consult"]', { timeout: 8000 }).then(() => p.waitForSelector('[data-testid="consult-composer"]', { timeout: 10000 })).then(() => true).catch(() => false)
    : await p.click('[data-testid="consult-toggle"]', { timeout: 8000 }).then(() => true).catch(() => false)
  await p.waitForTimeout(500)
  // the composer row: the question box's share of the row, Ask reachable
  out.composer = await withTimeout(p.evaluate(() => {
    const row = document.querySelector('[data-testid="consult-composer"]') || document.querySelector('.m-sheet-compose')
    const box = document.querySelector('[data-testid="consult-input"]')
    const send = document.querySelector('[data-testid="consult-send"]')
    if (!row || !box) return null
    const r = row.getBoundingClientRect(), b = box.getBoundingClientRect(), s = send?.getBoundingClientRect()
    return {
      row: Math.round(r.width), box: Math.round(b.width), fraction: +(b.width / r.width).toFixed(3),
      controls: row.querySelectorAll('button, select').length,
      sendVisible: !!s && s.width >= 44 && s.height >= 44 && s.right <= innerWidth + 1 && s.bottom <= innerHeight + 1 && s.left >= 0,
    }
  }), 10000, 'phone composer').catch(() => null)
  if (!opened) step(`phone ${width}: the Consult sheet opens`, false, 'no consult-toggle')
  if (answer && opened) {
    await p.fill('[data-testid="consult-input"]', 'What changed in the last week, and which sources disagree?').catch(() => {})
    await shot(p, `W12b-phone-consult-typed-${width}`)
    await p.click('[data-testid="consult-send"]', { timeout: 5000 }).catch(() => {})
    const got = await p.waitForSelector('[data-testid="consult-turn-assistant"]', { timeout: 15000 }).then(() => true).catch(() => false)
    step(`phone ${width}: a Consult answer renders`, got, got ? undefined : 'no assistant turn')
    if (got) {
      await p.waitForTimeout(500)
      out.answer = await withTimeout(p.evaluate(() => {
        const turn = [...document.querySelectorAll('[data-testid="consult-turn-assistant"]')].pop()
        const text = turn.innerText
        const chips = [...turn.querySelectorAll('[data-testid="citation-chip"]')]
        const res = []
        for (const el of chips) {
          el.scrollIntoView({ block: 'center' })
          const r = el.getBoundingClientRect()
          const cx = r.left + r.width / 2, cy = r.top + r.height / 2
          const D = 21.5 // ±21.5 px from the centre on both axes: a 44 px reach (the hit is its pseudo-element too)
          const pts = [[cx, cy - D], [cx, cy + D], [cx - D, cy], [cx + D, cy], [cx, cy]]
          const hits = pts.map(([x, y]) => { const h = document.elementFromPoint(x, y); return !!h && (h === el || el.contains(h)) })
          res.push({ tag: el.tagName, box: `${Math.round(r.width)}×${Math.round(r.height)}`, ok: hits.every(Boolean), hits })
        }
        return {
          rawBold: /\*\*/.test(text),
          pipeTable: /\|\s*-{3,}|^\s*\|.*\|\s*$/m.test(text),
          table: !!turn.querySelector('table'),
          rawIds: /\b[0-9a-f]{8}-0000-4000/.test(text),
          chips: res,
        }
      }), 15000, 'phone answer').catch(() => null)
      await shot(p, `W12c-phone-consult-answer-${width}`)
      // M-1 — the answer came on the stream (the POST said 202), and the turn
      // contract is drawn under it with the cost stop read as a partial answer.
      // M-3g: on the phone the contract is one line (window · coverage ·
      // "Partial answer") that opens to the shared block on a tap.
      await p.locator('[data-testid="consult-turn-assistant"]').last().locator('[data-testid="phone-contract-toggle"]').click({ timeout: 4000 }).catch(() => {})
      const drawn = await withTimeout(p.evaluate(() => {
        const t = [...document.querySelectorAll('[data-testid="consult-turn-assistant"]')].pop()
        const line = t?.querySelector('[data-testid="phone-contract-toggle"]')
        const block = t?.querySelector('[data-testid="consult-contract"]')
        return { contract: !!block, partial: !!block?.querySelector('[data-testid="consult-contract-partial"]') && /Partial answer/.test(block.textContent || '') && (!line || /Partial answer/.test(line.textContent || '')) }
      }), 10000, 'phone contract').catch(() => null)
      await shot(p, `W12c2-phone-consult-contract-${width}`)
      let sent = {}
      try { sent = JSON.parse(turn.lastBody || '{}') } catch {}
      out.turn = {
        streamed: turn.posts === 1 && turn.streams === 1,
        contract: !!drawn?.contract,
        partial: !!drawn?.partial,
        // what the phone now sends beside the question (M-1)
        sends: { window: !!sent.window, grounding: !!sent.grounding_mode, onScreen: sent.on_screen?.chat?.placement === 'tab', model: sent.model ?? null, writes: 'allow_writes' in sent },
        stop: null,
      }
      // Stop: a second question whose stream is held open, then Stop.
      await p.fill('[data-testid="consult-input"]', 'And what should I watch next?').catch(() => {})
      await p.click('[data-testid="consult-send"]', { timeout: 5000 }).catch(() => {})
      const stopShown = await p.waitForSelector('[data-testid="consult-stop"]', { timeout: 8000 }).then(() => true).catch(() => false)
      if (stopShown) await shot(p, `W12d-phone-consult-running-${width}`)
      await p.click('[data-testid="consult-stop"]', { timeout: 5000 }).catch(() => {})
      const settled = await p.waitForFunction(() => {
        const ts = [...document.querySelectorAll('[data-testid="consult-turn-assistant"]')]
        return ts.length >= 2 && /Answered from what it found so far/.test(ts[ts.length - 1].textContent || '')
      }, null, { timeout: 10000 }).then(() => true).catch(() => false)
      releaseHeld?.()
      const askBack = await p.$('[data-testid="consult-send"]').then((h) => !!h).catch(() => false)
      let synthModel = null
      try { synthModel = JSON.parse(turn.synthBody || '{}').model ?? null } catch {}
      out.turn.stop = { shown: stopShown, stops: turn.stops, synths: turn.synths, settled, askBack, synthModel }
      await shot(p, `W12e-phone-consult-stopped-${width}`)
      step(`phone ${width}: the answer streams, the contract is drawn, Stop works`, out.turn.streamed && out.turn.contract && stopShown && settled, JSON.stringify(out.turn))
    }
  }
  releaseHeld?.()
  ;(result.phoneConsult ??= []).push(out)
  log(`PHONE CONSULT ${width}: ${JSON.stringify(out)}`)
}

/**
 * P-1 / P-3 — the installable phone app, in the walk's browser:
 *   1. installability, Lighthouse-style: the manifest parses with id/scope
 *      /m/, start_url in scope, standalone, a name, 192 and 512 icons and a
 *      maskable one, and every icon decodes at its declared size;
 *   2. the worker registers with scope /m/ and controls the phone page, and
 *      the desktop at / is neither controlled nor registered;
 *   3. Cache Storage holds the shell only: no /api/ response in any cache;
 *   4. offline (the context offline AND the static server dropping every
 *      connection), a reload draws the shell from the worker's cache and says
 *      plainly it is offline; still no /api/ in any cache; back online, the
 *      line goes;
 *   5. a new worker version waits, the page says "Update available", nothing
 *      reloads by itself, and the Reload tap hands over with one reload.
 */
async function phoneApp(browser, phone) {
  const out = {}
  const c = await context(browser, phone)
  const p = await c.newPage(); wire(p, 'phone app')
  net.phase = 'phone-app'
  await p.goto(BASE + '/m/', { timeout: 30000 }).catch(() => {})
  await quiet(p, 20000)

  // 1. the manifest, as an installing browser reads it
  const man = await withTimeout(p.evaluate(async () => {
    const link = document.querySelector('link[rel="manifest"]')
    if (!link) return { error: 'no <link rel="manifest">' }
    const href = new URL(link.getAttribute('href'), location.href)
    const r = await fetch(href, { credentials: 'include' })
    const ct = r.headers.get('content-type') || ''
    let m
    try { m = await r.json() } catch { return { error: 'the manifest does not parse', status: r.status, ct } }
    const icons = []
    for (const ic of m.icons || []) {
      const u = new URL(ic.src, href)
      const img = new Image(); img.src = u.href
      const ok = await img.decode().then(() => true).catch(() => false)
      icons.push({ src: u.pathname, sizes: ic.sizes, purpose: ic.purpose || 'any', ok, size: `${img.naturalWidth}x${img.naturalHeight}` })
    }
    const apple = document.querySelector('link[rel="apple-touch-icon"]')?.getAttribute('href') || null
    let appleOk = false
    if (apple) { const img = new Image(); img.src = new URL(apple, location.href).href; appleOk = await img.decode().then(() => true).catch(() => false) }
    return { href: href.pathname, status: r.status, ct, crossorigin: link.getAttribute('crossorigin'), id: m.id, scope: m.scope, start_url: m.start_url, display: m.display, name: m.name, short_name: m.short_name, theme: m.theme_color, bg: m.background_color, icons, apple, appleOk }
  }), 20000, 'manifest').catch((e) => ({ error: String(e).slice(0, 120) }))
  out.manifest = man
  {
    const icons = man.icons ?? []
    const has = (sz, purpose) => icons.some((i) => i.ok && i.sizes === sz && i.size === sz && i.purpose.split(' ').includes(purpose))
    const why = []
    if (man.error) why.push(man.error)
    if (man.status !== 200) why.push(`status ${man.status}`)
    if (man.href !== '/m/manifest.webmanifest') why.push(`href ${man.href}`)
    if (man.id !== '/m/' || man.scope !== '/m/') why.push(`id ${man.id} scope ${man.scope}`)
    if (!man.start_url || !String(man.start_url).startsWith('/m/')) why.push(`start_url ${man.start_url}`)
    if (man.display !== 'standalone') why.push(`display ${man.display}`)
    if (!man.name && !man.short_name) why.push('no name')
    if (!has('192x192', 'any') || !has('512x512', 'any')) why.push('no loadable 192 and 512 icons')
    if (!has('512x512', 'maskable')) why.push('no loadable maskable icon')
    step('phone app: the manifest parses and meets the install criteria (scope /m/, standalone, 192 + 512 + maskable icons)', !why.length, why.join('; ') || undefined)
    const bad = icons.filter((i) => !i.ok || i.size !== i.sizes)
    step('phone app: every manifest icon and the Home Screen icon load at their declared size', icons.length > 0 && !bad.length && man.appleOk,
      bad.length ? bad.map((i) => `${i.src} ${i.size}≠${i.sizes}`).join(', ') : man.appleOk ? undefined : 'apple-touch-icon did not load')
  }

  // 2. the worker controls /m/ (after a reload at the latest), never /
  const reg = await withTimeout(p.evaluate(async () => {
    const r = await Promise.race([navigator.serviceWorker.ready, new Promise((res) => setTimeout(() => res(null), 15000))])
    return r ? { scope: new URL(r.scope).pathname, script: new URL(r.active.scriptURL).pathname } : null
  }), 20000, 'worker').catch(() => null)
  await p.reload({ timeout: 30000 }).catch(() => {})
  await quiet(p, 20000)
  const controlled = await p.evaluate(() => !!navigator.serviceWorker.controller).catch(() => false)
  out.worker = { ...reg, controlled }
  step('phone app: the worker registers with scope /m/ and controls the phone page', !!reg && reg.scope === '/m/' && reg.script === '/m/sw.js' && controlled,
    reg ? `scope ${reg.scope}, script ${reg.script}, controlled ${controlled}` : 'no registration became ready')
  {
    const d = await c.newPage(); wire(d, 'phone app: desktop')
    await d.goto(BASE + '/?desktop=1', { timeout: 30000 }).catch(() => {})
    await d.waitForTimeout(1500)
    const desk = await d.evaluate(async () => ({ path: location.pathname, controlled: !!navigator.serviceWorker.controller, reg: (await navigator.serviceWorker.getRegistration('/'))?.scope ?? null })).catch(() => null)
    out.desktop = desk
    step('phone app: the desktop at / is not controlled and has no registration', !!desk && desk.path === '/' && !desk.controlled && !desk.reg, desk ? JSON.stringify(desk) : 'not measured')
    await d.close()
  }

  // 3. what the device stores
  const cached = () => p.evaluate(async () => {
    const rows = []
    for (const n of await caches.keys()) { const cc = await caches.open(n); for (const r of await cc.keys()) { const u = new URL(r.url); rows.push(u.pathname + u.search) } }
    return rows
  }).catch(() => null)
  const judgeCache = (rows, when) => {
    const api = (rows ?? []).filter((x) => /^\/(api|ws|tiles)(\/|$)/.test(x))
    const outside = (rows ?? []).filter((x) => !x.startsWith('/m/'))
    step(`phone app: ${when}, Cache Storage holds the shell only — no /api/ response in any cache`, !!rows && rows.length > 0 && !api.length && !outside.length,
      !rows ? 'not measured' : `${rows.length} entries${api.length ? `; API: ${api.slice(0, 3).join(', ')}` : ''}${outside.length ? `; outside /m/: ${outside.slice(0, 3).join(', ')}` : ''}`)
  }
  out.cacheOnline = await cached()
  judgeCache(out.cacheOnline, 'online')

  // 4. offline: a reload still draws the shell, and says so
  await c.setOffline(true)
  staticState.down = true
  await p.reload({ timeout: 20000 }).catch(() => {})
  const offline = await p.waitForSelector('[data-testid="pwa-offline"]', { timeout: 15000 }).then(() => true).catch(() => false)
  const offText = offline ? await p.locator('[data-testid="pwa-offline"]').innerText().catch(() => '') : ''
  const drawn = await p.evaluate(() => (document.getElementById('root')?.children.length ?? 0) > 0 && !!document.querySelector('script[type="module"]')).catch(() => false)
  out.offline = { banner: offline, text: offText, drawn }
  await shot(p, 'W14-phone-app-offline')
  step('phone app: offline, a reload draws the shell from the device and says plainly it is offline', offline && drawn && /No connection/.test(offText),
    offline ? (drawn ? undefined : 'the shell did not draw') : 'no offline line (the page did not load from the worker, or did not say so)')
  out.cacheOffline = await cached()
  judgeCache(out.cacheOffline, 'after the offline reload')
  staticState.down = false
  await c.setOffline(false)
  const back = await p.waitForSelector('[data-testid="pwa-offline"]', { state: 'detached', timeout: 10000 }).then(() => true).catch(() => false)
  step('phone app: back online, the offline line goes', back, back ? undefined : 'still showing "No connection"')
  await quiet(p, 20000)

  // 5. the update flow: a byte-different worker waits; only the tap applies it
  staticState.swBump = 1
  await p.evaluate(() => { window.__walkBeforeUpdate = 1 }).catch(() => {})
  await p.evaluate(async () => { const r = await navigator.serviceWorker.getRegistration('/m/'); await r?.update() }).catch(() => {})
  const banner = await p.waitForSelector('[data-testid="pwa-update"]', { timeout: 20000 }).then(() => true).catch(() => false)
  await p.waitForTimeout(2000)
  const notReloaded = await p.evaluate(() => window.__walkBeforeUpdate === 1).catch(() => false)
  step('phone app: a new version waits; the page says "Update available" and does not reload by itself', banner && notReloaded,
    banner ? (notReloaded ? undefined : 'the page reloaded without a tap') : 'no "Update available" line')
  await shot(p, 'W15-phone-app-update')
  if (banner) {
    // F312 (2 of ~11 full walks: no hand-over, no reload, no click error). The
    // record below tells the two causes apart on the next failure:
    //   * the tap did not land — `applying` stays 0, the button still reads
    //     "Reload", the waiting worker never leaves `installed`;
    //   * activation stalled — `applying` 1 / "Reloading…", and the waiting
    //     worker's state timeline (`swLog`) stops short of `activated`, or
    //     reaches it with no `controllerchange`.
    // A snapshot before the tap, 1 s after it, and at the end; the timeline
    // is installed on the page before the tap (it dies with a reload, which
    // is the passing case).
    const swSnap = () => p.evaluate(async () => {
      const r = await navigator.serviceWorker.getRegistration('/m/')
      const line = document.querySelector('[data-testid="pwa-update"]')
      const btn = document.querySelector('[data-testid="pwa-update-reload"]')
      const name = (w) => (w ? `${w.state} ${new URL(w.scriptURL).pathname}` : null)
      return {
        installing: name(r?.installing), waiting: name(r?.waiting), active: name(r?.active),
        controller: name(navigator.serviceWorker.controller),
        banner: !!line, applying: line ? line.getAttribute('data-applying') : null,
        button: btn ? { text: (btn.textContent || '').trim(), disabled: btn.disabled } : null,
        swLog: window.__walkSwLog ?? null,
      }
    }).catch((e) => ({ error: String(e).slice(0, 120) }))
    await p.evaluate(async () => {
      const r = await navigator.serviceWorker.getRegistration('/m/')
      const t0 = performance.now()
      const log = (window.__walkSwLog = [])
      const at = () => Math.round(performance.now() - t0)
      const w = r?.waiting
      if (w) w.addEventListener('statechange', () => log.push({ t: at(), waitingWorker: w.state }))
      navigator.serviceWorker.addEventListener('controllerchange', () => log.push({ t: at(), controllerchange: navigator.serviceWorker.controller?.state ?? null }))
      document.addEventListener('click', (e) => log.push({ t: at(), click: e.target?.getAttribute?.('data-testid') ?? e.target?.tagName ?? null }), { capture: true })
    }).catch(() => {})
    out.updateBefore = await swSnap()
    await p.click('[data-testid="pwa-update-reload"]', { timeout: 5000 }).catch((e) => { out.updateClickError = String(e).slice(0, 120) })
    await p.waitForTimeout(1000)
    out.updateAt1s = await swSnap()
    const reloaded = await p.waitForFunction(() => window.__walkBeforeUpdate === undefined, null, { timeout: 15000 }).then(() => true).catch(() => false)
    await quiet(p, 20000)
    const after = await p.evaluate(async () => {
      const r = await navigator.serviceWorker.getRegistration('/m/')
      return { waiting: !!r?.waiting, controlled: !!navigator.serviceWorker.controller, banner: !!document.querySelector('[data-testid="pwa-update"]') }
    }).catch(() => null)
    out.updateEnd = reloaded ? null : await swSnap()
    out.update = { before: out.updateBefore, clickError: out.updateClickError, at1s: out.updateAt1s, reloaded, after, end: out.updateEnd }
    const ok = reloaded && !!after && !after.waiting && after.controlled && !after.banner
    // on a failure, name which of the two it was
    const landed = out.updateAt1s?.applying === '1' || /Reloading/.test(out.updateAt1s?.button?.text ?? '') || (out.updateAt1s?.swLog ?? []).some((x) => x.click === 'pwa-update-reload')
    const why = ok ? undefined : landed ? 'the tap landed (applying); activation stalled' : 'the tap did not land (not applying)'
    step('phone app: the Reload tap hands over to the new version with one reload', ok,
      JSON.stringify({ reloaded, ...after, ...(why ? { why, before: out.updateBefore, at1s: out.updateAt1s, end: out.updateEnd } : {}) }))
  }
  staticState.swBump = 0
  result.phoneApp = out
  log(`PHONE APP: ${JSON.stringify({ worker: out.worker, desktop: out.desktop, cacheEntries: out.cacheOnline?.length, offline: out.offline, update: out.update })}`)
  await c.close()
}

/** The icon both HTML entries reference: it must come back as an image, not the SPA's index.html. */
async function phoneFavicon(c) {
  for (const p of ['/favicon.svg', '/m/favicon.svg']) {
    const r = await c.request.get(BASE + p).catch(() => null)
    ;(result.favicon ??= []).push({ path: p, status: r ? r.status() : null, type: r ? (r.headers()['content-type'] || '') : '' })
  }
}

function step(name, ok, note) {
  if (skipForRole(name)) return
  result.steps.push({ name, ok, note: note ? scrub(note) : undefined }); log(`STEP ${ok ? 'ok  ' : 'FAIL'} ${name}${note ? ' — ' + scrub(note) : ''}`)
}
/**
 * Roll 89: true, and the check is recorded as SKIPPED by name, when a
 * signed-in account's role is below what the check rests on (signin.mjs
 * ROLE_GATED). Without an account (fixtures, or the bearer) nothing is skipped.
 */
function skipForRole(name) {
  if (!SIGN_IN) return false
  const gate = roleGateFor(name)
  if (!gate || !skipsForRole(session.role ?? 'reader', gate.needs)) return false
  result.skipped.push({ name, needs: gate.needs, reads: gate.reads })
  log(`STEP skip ${name} — needs ${gate.needs} (${gate.reads.join(', ')}); the walk account is ${session.role ?? 'unknown'}`)
  return true
}

// ------------------------------------------------------------------ the walk
async function main() {
  await new Promise((r) => server.listen(PORT, r))
  const browser = await chromium.launch({ executablePath: fs.existsSync(CHROME) ? CHROME : undefined, args: ['--no-sandbox'] })
  const desk = (w, h) => ({ viewport: { width: w, height: h } })
  const phone = { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }

  // 1. sign-in without a token
  {
    const ctx = await context(browser, desk(1680, 1050), { withToken: false, allowSignIn: SIGN_IN })
    const p = await ctx.newPage(); wire(p, 'sign-in')
    await p.goto(BASE + '/', { timeout: 30000 }).catch(() => {})
    const found = await p.waitForSelector('[data-testid="sign-in-email"], [data-testid="sign-in-submit"]', { timeout: 20000 }).then(() => true).catch(() => false)
    step('sign-in screen without a token', found, found ? undefined : 'no sign-in form appeared')
    await shot(p, 'W01-sign-in')
    // roll 89: a live walk with an account signs in here, through the real screen
    if (SIGN_IN) {
      await p.fill('[data-testid="sign-in-email"]', WALK_EMAIL, { timeout: 10000 }).catch(() => {})
      await p.fill('[data-testid="sign-in-password"]', WALK_PASSWORD, { timeout: 10000 }).catch(() => {})
      await p.click('[data-testid="sign-in-submit"]', { timeout: 10000 }).catch(() => {})
      const shell = await p.waitForSelector(SHELL_READY, { timeout: 30000 }).then(() => true).catch(() => false)
      const ok = !!session.cookie && shell
      step('sign-in: the walk account signs in through the sign-in screen', ok,
        ok ? `role ${session.role ?? 'unknown'}` : `sign-in answered ${session.signInStatus ?? 'nothing'}${session.cookie ? '; no shell after it' : '; no session'}`)
      if (!session.cookie) { console.error('walk: the account did not sign in; nothing else can be walked'); await ctx.close(); await browser.close(); server.close(); process.exit(2) }
    }
    await ctx.close()
  }

  // 2. the landing with nothing read yet: every API answer held
  {
    const ctx = await context(browser, desk(1680, 1050))
    const p = await ctx.newPage(); wire(p, 'landing (held)')
    startHold()
    await p.goto(BASE + '/', { timeout: 30000 }).catch(() => {})
    await p.waitForSelector(SHELL_READY, { timeout: 20000 }).catch(() => {})
    await p.waitForTimeout(2500)
    await zerosBeforeRead(p, 'landing')
    await shot(p, 'W02-landing-nothing-read')
    endHold({ discard: !flag('forward-held') })
    await p.waitForTimeout(300)
    await ctx.close()
  }

  // 3. the landing at 1680, then each surface, a subject from the rail, Consult, Flow
  const ctx = await context(browser, desk(1680, 1050))
  const page = await ctx.newPage(); wire(page, 'desktop')
  page.setDefaultTimeout(30000)
  const t0 = Date.now()
  net.phase = 'landing'
  await page.goto(BASE + '/', { timeout: 30000 }).catch(() => {})
  const shell = await page.waitForSelector(SHELL_READY, { timeout: 30000 }).then(() => true).catch(() => false)
  const q = shell ? await quiet(page, 60000) : null
  result.landingReadyMs = shell && q != null ? Date.now() - t0 : null
  result.landingHeldBy = heldBy(net.log, 'landing')
  step('landing', shell, shell ? `ready in ${result.landingReadyMs ?? 'never'} ms` : 'no shell drawn')
  // the landing's request window stays open LANDING_SETTLE_MS past "ready",
  // so work deferred to idle (or a pager that resumes) is still counted
  await page.waitForTimeout(LANDING_SETTLE_MS)
  await quiet(page, 20000)
  net.phase = 'after-landing'
  result.hiddenFetches = await withTimeout(page.evaluate(() => (window.__legbaFetchLog ?? null)), 10000, 'fetch log').catch(() => null)
  await stepLongTasks(page, 'landing', 0)
  await measure(page, 'landing', { reader: true, width: 1680 })
  result.consult.push({ width: 1680, where: 'landing', boxWidth: await consultBox(page) })
  await shot(page, 'W03-landing-1680')
  await walkMap(page, t0)
  await walkFlatSignals(ctx)

  // the frame (U-6): the top bar, the rail, the stage, Consult under it, the panels
  for (const id of ['surface-bar', 'frame-rail', 'stage', 'consult-dock', 'panel-side']) {
    const there = (await page.locator(`[data-testid="${id}"]`).count().catch(() => 0)) > 0
    step(`the frame draws ${id}`, there, there ? undefined : 'not in the page')
  }

  // the reader side's surfaces
  const tabs = await surfaceTabs(page)
  log('READER SURFACES', tabs.map((t) => t.id).join(', '))
  step('the reader side has four surfaces', tabs.length === 4 && tabs.every((t) => !OPERATOR_SURFACES.has(t.id)), tabs.map((t) => t.id).join(', '))
  await walkSurfaces(page, tabs, 'W04-surface')

  // THE RECORD SHEET (U-10): opened twice, as a reader would — from a change
  // on the Board, and from a citation in a Consult answer (a prior
  // conversation reopened from the rail) — each timed from the click to the
  // chain read to its last step, its own reads counted in their own phase
  // (`record: …`), its address checked, and "Show what was received" opened
  // once. Live with no change row, a finding is opened by its address.
  {
    const chainReady = (p) => p.waitForFunction(() => {
      const sheet = document.querySelector('[data-testid="record-sheet"]')
      if (!sheet) return false
      const chain = sheet.querySelector('[data-testid="record-chain"]')
      if (!chain) return false
      return !chain.querySelector('[data-testid="record-reading"]') && chain.querySelector('[data-testid="record-step-integrity"] li, [data-testid="record-step-integrity"] [data-testid="record-gap"]')
    }, null, { timeout: 20000 }).then(() => true).catch(() => false)
    const sheetRecord = (p) => p.locator('[data-testid="record-sheet"]').getAttribute('data-record').catch(() => null)
    const address = (p) => p.evaluate(() => new URLSearchParams(location.hash.slice(1)).get('rec')).catch(() => null)
    const openSheet = async (from, phase, click, shotName) => {
      await quiet(page, 20000)
      const prev = net.phase
      net.phase = phase
      const since = await now(page)
      const t0 = Date.now()
      const clicked = await click()
      const ready = clicked && (await chainReady(page))
      const readyMs = ready ? Date.now() - t0 : null
      await quiet(page, 15000)
      const row = { from, phase, clicked, opened: ready, readyMs, record: await sheetRecord(page), address: await address(page), text: null }
      if (ready) {
        await stepLongTasks(page, `record sheet (${from})`, since)
        await measure(page, `record sheet (${from})`, { reader: true, width: 1680, within: '[data-testid="record-sheet"]' })
        await shot(page, shotName)
        // "Show what was received": the first receipt that kept text
        const btns = page.locator('[data-testid="record-sheet"] [data-testid="record-show-received"]')
        const n = await btns.count().catch(() => 0)
        if (n > 0) {
          net.phase = `${phase} (text)`
          await btns.nth(n - 1).click({ timeout: 5000 }).catch(() => {})
          const shown = await page.waitForSelector('[data-testid="record-sheet"] [data-testid="record-text"], [data-testid="record-sheet"] [data-testid="record-text-withheld"], [data-testid="record-sheet"] [data-testid="record-gap"]', { timeout: 15000 }).then(() => true).catch(() => false)
          await quiet(page, 10000)
          row.text = { shown, kind: await page.locator('[data-testid="record-sheet"] [data-testid="record-text"]').count().then((c) => (c ? 'text' : 'withheld or gap')).catch(() => null) }
          await shot(page, `${shotName}-received`)
        }
        await page.click('[data-testid="record-sheet-close"]').catch(() => page.keyboard.press('Escape'))
        await page.waitForTimeout(300)
      }
      net.phase = prev
      ;(result.recordSheet ??= []).push(row)
      log(`RECORD SHEET ${from}: ${ready ? `chain in ${readyMs} ms` : 'did not open'} — ${row.record ?? '-'} · address ${row.address ?? 'none'}`)
      return row
    }

    await page.click('[data-testid="surface-tab-board"]').catch(() => {})
    await quiet(page, 20000)
    let how = 'a change on the Board clicked'
    const fromBoard = await openSheet('a Board row', 'record: a Board row', async () => {
      // the stage may hold a page an earlier step opened: back to today's read first
      for (let i = 0; i < 3; i++) {
        if (await page.locator('[data-testid="morning-read-changed-row"]').first().isVisible().catch(() => false)) break
        if (!(await page.click('[data-testid="stage-back"]', { timeout: 3000 }).then(() => true).catch(() => false))) break
        await page.waitForTimeout(500)
      }
      // the first change row ON SCREEN (`:visible`): a hidden copy earlier in
      // the page must not take the click
      const row = page.locator('[data-testid="morning-read-changed-row"]:visible').first()
      let why = null
      if (await row.click({ timeout: 8000 }).then(() => true).catch((e) => { why = String(e?.message ?? e).split('\n')[0]; return false })) return true
      // roll 87: say WHY no row took the click, so a failure names its cause
      const seen = await page.evaluate(() => {
        const rows = [...document.querySelectorAll('[data-testid="morning-read-changed-row"]')]
        const shown = rows.filter((r) => r.getClientRects().length > 0)
        return `${rows.length} change rows, ${shown.length} on screen; ${location.hash || 'no address'}`
      }).catch(() => 'not read')
      log(`RECORD SHEET a Board row: no row took the click (${why ?? 'no error'}) — ${seen}`)
      if (MODE !== 'live') return false
      how = `a finding opened by its address (no change row took the click: ${seen})`
      const fid = await withTimeout(page.evaluate(async () => {
        const tok = localStorage.getItem('legba_token')
        const r = await fetch('/api/v1/findings?limit=40&fields=summary', { headers: tok ? { authorization: 'Bearer ' + tok } : {} })
        if (!r.ok) return null
        const b = await r.json()
        const list = Array.isArray(b) ? b : (b.data ?? b.findings ?? b.items ?? [])
        const f = list.find((x) => x && typeof x.id === 'string')
        return f ? f.id : null
      }), 15000, 'record id').catch(() => null)
      if (!fid) return false
      // the shell reads `#rec=` once, at load (record.mjs): set it beside the
      // frame's own params and reload, as a shared link opens
      const hash = await page.evaluate(() => location.hash).catch(() => '')
      await page.evaluate((h) => { history.replaceState(null, '', h) }, withRecordAddress(hash, 'finding', fid)).catch(() => {})
      await page.reload({ timeout: 30000 }).catch(() => {})
      return true
    }, 'W06b-record-sheet')
    step('the Record sheet opens from a Board row', fromBoard.opened, fromBoard.opened ? how : 'the chain did not read from a change row')

    // a citation in a Consult answer: a prior conversation reopened from the rail
    const fromConsult = await openSheet('a Consult citation', 'record: a Consult citation', async () => {
      await page.click('[data-testid="rail-conversations-show"]', { timeout: 5000 }).catch(() => {})
      const conv = page.locator('[data-testid="rail-conversation"]').first()
      if (!(await conv.click({ timeout: 8000 }).then(() => true).catch(() => false))) return false
      if (!(await page.click('[data-testid="consult-cited-toggle"]', { timeout: 8000 }).then(() => true).catch(() => false))) return false
      return page.locator('[data-testid="consult-cited-row-0"] [data-testid="record-link"]').first().click({ timeout: 8000 }).then(() => true).catch(() => false)
    }, 'W06c-record-sheet-consult')
    // live, the owner's conversations may cite nothing: that is reported, not failed
    const consultOk = fromConsult.opened || (MODE === 'live' && !fromConsult.clicked)
    step('the Record sheet opens from a Consult citation', consultOk, fromConsult.opened ? fromConsult.record ?? undefined : fromConsult.clicked ? 'the chain did not read from the citation' : 'no prior conversation with a cited record to open')
  }

  // one subject opened from the switcher
  let flowSurface = null
  let subjectId = null
  if (tabs[0]) await page.click(`[data-testid="${tabs[0].testid}"]`).catch(() => {})
  await quiet(page, 20000)
  {
    const since = await now(page)
    let row = page.locator('[data-testid^="rail-subject-"]').first()
    if (!(await row.isVisible().catch(() => false))) {
      // the reader side picks a subject from the top bar's switcher (U-2)
      await page.click('[data-testid="subject-switcher"]', { timeout: 5000 }).catch(() => {})
      await row.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {})
    }
    if (!(await row.isVisible().catch(() => false))) {
      for (const tg of await page.locator('[data-testid^="rail-section-toggle-"]').all()) {
        await tg.click().catch(() => {})
        await page.waitForTimeout(300)
        if (await row.isVisible().catch(() => false)) break
      }
      row = page.locator('[data-testid^="rail-subject-"]').first()
    }
    const ok = await row.click({ timeout: 8000 }).then(() => true).catch(() => false)
    const opened = ok && (await page.waitForSelector('[data-testid="subject-page"]', { timeout: 25000 }).then(() => true).catch(() => false))
    if (opened) {
      await quiet(page, 25000)
      await page.waitForTimeout(1000)
      subjectId = await page.locator('[data-testid="subject-page"]').first().getAttribute('data-subject').catch(() => null)
      await stepLongTasks(page, 'subject page', since)
      await measure(page, 'subject page', { reader: true, width: 1680 })
      await shot(page, 'W05-subject-from-rail')
    }
    step('a subject opened from the switcher', opened, opened ? undefined : ok ? 'the subject page did not open' : 'no subject row in the switcher')
  }

  // the Consult composer: focus the box, type nothing, send nothing
  {
    const ta = page.locator('[data-testid="consult-dock"] textarea').first()
    const ok = await ta.focus({ timeout: 5000 }).then(() => true).catch(() => false)
    result.consult.push({ width: 1680, where: 'with a subject open', boxWidth: await consultBox(page) })
    step('the Consult composer takes focus', ok, ok ? undefined : 'no question box to focus')
    await shot(page, 'W06-consult-composer')
  }

  // the Ledger (U-11): the forecasts on the stage, the track record behind
  // its five-resolution gate, the evaluation record; its reads counted
  {
    net.phase = 'ledger'
    const since = await now(page)
    const opened = await page.click('[data-testid="surface-tab-ledger"]', { timeout: 8000 }).then(() => true).catch(() => false)
    const ready = opened && (await page.waitForFunction(() => {
      const f = document.querySelector('[data-testid="ledger-forecasts"]')
      const t = document.querySelector('[data-testid="track-record"]')
      const p = document.querySelector('[data-testid="proof-record"]')
      return f && t && p && t.getAttribute('data-state') !== 'reading' && p.getAttribute('data-state') !== 'reading'
    }, null, { timeout: 25000 }).then(() => true).catch(() => false))
    await quiet(page, 20000)
    await page.waitForTimeout(800)
    net.phase = 'after-ledger'
    if (ready) {
      result.ledger = await withTimeout(page.evaluate(() => {
        const t = document.querySelector('[data-testid="track-record"]')
        const count = document.querySelector('[data-testid="track-record-count"]')?.textContent ?? null
        const m = /^(\d+) \/ (\d+)/.exec(count ?? '')
        return {
          forecasts: document.querySelectorAll('[data-testid="ledger-forecast-row"]').length,
          trackState: t?.getAttribute('data-state') ?? null,
          scored: t?.getAttribute('data-scored') === '1',
          resolved: m ? +m[1] : null,
          floor: m ? +m[2] : null,
          scoreShown: !!document.querySelector('[data-testid="track-record-score"]'),
          brierText: /\b\d\.\d{3}\b/.test(t?.textContent ?? ''),
          proofState: document.querySelector('[data-testid="proof-record"]')?.getAttribute('data-state') ?? null,
          proofRuns: [...document.querySelectorAll('[data-testid="proof-run"]')].map((e) => e.getAttribute('data-status')),
          seals: [...document.querySelectorAll('[data-testid="ledger-forecast-seal"]')].map((e) => e.textContent.trim()).slice(0, 12),
        }
      }), 10000, 'ledger state').catch(() => null)
      await stepLongTasks(page, 'ledger', since)
      await measure(page, 'ledger page', { reader: true, width: 1680 })
      await shot(page, 'W06c-ledger')
    }
    // the Ledger's reads on its FIRST open (the surfaces walk, cold) — this
    // second visit is answered from the shared query cache
    if (result.ledger) result.ledger.requests = landingStats(net.log, 'ledger-open')
    step('the Ledger opens: forecasts, track record, evaluations', !!ready && !!result.ledger && result.ledger.forecasts > 0, ready ? `${result.ledger?.forecasts ?? 0} forecasts · track record ${result.ledger?.trackState} · evaluations ${result.ledger?.proofState}${result.ledger?.proofRuns?.length ? ` (${result.ledger.proofRuns.join(', ')})` : ''}` : opened ? 'the Ledger views did not finish reading' : 'no Ledger tab')
  }

  // the operator side: one entry across, its four sections
  {
    const crossed = await page.click('[data-testid="to-operator-side"]', { timeout: 8000 }).then(() => true).catch(() => false)
    await quiet(page, 25000)
    const opTabs = crossed ? await surfaceTabs(page) : []
    log('OPERATOR SURFACES', opTabs.map((t) => t.id).join(', '))
    step('the operator side has its four sections', opTabs.length === 4 && opTabs.every((t) => OPERATOR_SURFACES.has(t.id)), crossed ? opTabs.map((t) => t.id).join(', ') : 'no way across to the operator side')
    flowSurface = await walkSurfaces(page, opTabs, 'W04-operator')
    // U-11: the Gate carries the forecast resolution queue (admin side only)
    if (crossed) {
      await page.click('[data-testid="surface-tab-gate"]', { timeout: 8000 }).catch(() => {})
      const queue = await page.waitForSelector('[data-testid="resolution-queue"]', { state: 'visible', timeout: 15000 }).then(() => true).catch(() => false)
      step('the Gate shows the forecast resolution queue', queue, queue ? undefined : 'no resolution queue on the Gate')
      // a proposal awaiting a person, when the record has one: opened, its evidence read, nothing written
      const item = page.locator('[data-testid="resolution-queue-item"] button').first()
      if (queue && (await item.count()) > 0) {
        await item.click().catch(() => {})
        const ev = await page.waitForSelector('[data-testid="resolution-evidence"], [data-testid="resolution-records-missing"]', { timeout: 15000 }).then(() => true).catch(() => false)
        step('a proposed resolution opens with its evidence', ev, ev ? undefined : 'the proposal did not open')
      }
      if (queue) await shot(page, 'W04g-gate-resolutions')
    }
  }

  // the Flow panel: default, one expansion, one refused expansion
  if (!flowSurface) step('Flow panel', false, 'no Flow tab on any surface')
  else {
    await page.click(`[data-testid="${flowSurface.testid}"]`).catch(() => {})
    await quiet(page, 20000)
    const flowState = () => withTimeout(page.evaluate(() => ({
      nodes: document.querySelectorAll('.react-flow__node').length,
      over: !!document.querySelector('[data-testid="flow-over-budget"]'),
      counts: document.querySelector('[data-testid="flow-counts"]')?.textContent ?? null,
    })), 10000, 'flow state')
    const flowReady = () => page.waitForFunction(() => {
      const p = document.querySelector('[data-testid="flow-panel"]')
      return p && (document.querySelectorAll('.react-flow__node').length > 0 || document.querySelector('[data-testid="flow-over-budget"]')) && !/Laying out|Deriving/.test(p.innerText)
    }, null, { timeout: 60000 }).then(() => true).catch(() => false)
    const flowStep = async (name, action) => {
      const since = await now(page)
      const w0 = Date.now()
      await action()
      const ok = await flowReady()
      const readyMs = Date.now() - w0
      await page.waitForTimeout(600)
      const lt = await stepLongTasks(page, 'flow: ' + name, since)
      const st = await flowState().catch(() => { result.unresponsive.push('flow ' + name); return null })
      result.flow.push({ step: name, readyMs, ok, longTaskMaxMs: lt?.max ?? null, ...(st ?? {}) })
      return st
    }
    await flowStep('default', () => page.locator('.dv-tab', { hasText: /^\s*Flow\s*$/ }).first().click())
    await shot(page, 'W07-flow-default')
    await measure(page, 'flow', { reader: false, width: 1680 })
    const groups = await page.$$eval('[data-testid^="flow-group-toggle-"]', (els) => els.map((e) => ({ id: e.getAttribute('data-testid').replace('flow-group-toggle-', ''), n: Math.max(0, ...[...e.textContent.matchAll(/\d+/g)].map((x) => +x[0])) })))
    if (!groups.length) step('Flow panel', false, 'no groups to expand')
    else {
      const sorted = [...groups].sort((a, b) => a.n - b.n)
      const small = sorted.find((g) => g.n > 1) ?? sorted[0]
      const s1 = await flowStep(`expand ${small.id} (${small.n})`, () => page.click(`[data-testid="flow-group-toggle-${small.id}"]`))
      await shot(page, 'W08-flow-expanded')
      step('Flow: one expansion draws', !!s1 && !s1.over && s1.nodes > 0, s1 ? `${s1.nodes} nodes; ${s1.counts}` : 'unresponsive')
      // collapse it again, then expand the largest groups until the budget refuses a view (at most three)
      await page.click(`[data-testid="flow-group-toggle-${small.id}"]`).catch(() => {})
      await flowReady()
      let s2 = null
      for (const big of [...sorted].reverse().slice(0, 3)) {
        s2 = await flowStep(`expand ${big.id} (${big.n})`, () => page.click(`[data-testid="flow-group-toggle-${big.id}"]`))
        if (!s2 || s2.over) break
      }
      await shot(page, 'W09-flow-refused')
      step('Flow: an over-budget expansion is refused, not drawn', !!s2 && s2.over, s2 ? (s2.over ? 'refused with the budget message' : `drew ${s2.nodes} nodes`) : 'unresponsive')
    }
  }
  await ctx.close()

  // 3c. the Watch surface (U-9)
  await walkWatch(browser, desk)

  // 4. the frame at four screens (U-6): 1366×768, 1920×1080, 2560×1440,
  // 3840×2160 — the landing and each reader page, measured (text floor, body
  // text, empty bands, the chat's width, the Consult box) and photographed
  for (const [w, h] of [[1366, 768], [1920, 1080], [2560, 1440], [3840, 2160]]) {
    const c = await context(browser, desk(w, h))
    const p = await c.newPage(); wire(p, `screen ${w}`)
    await p.goto(BASE + '/', { timeout: 30000 }).catch(() => {})
    await p.waitForSelector(SHELL_READY, { timeout: 30000 }).catch(() => {})
    await quiet(p, 40000)
    await p.waitForTimeout(800)
    await stepLongTasks(p, `landing ${w}`, 0)
    await measure(p, `landing ${w}`, { reader: true, width: w })
    await layoutMetrics(p, `landing ${w}`, w)
    result.consult.push({ width: w, where: 'landing', boxWidth: await consultBox(p) })
    await shot(p, `W10-landing-${w}`)
    // the map in the frame (U-7): as the lead panel column on the Board
    await mapFit(p, `board panel ${w}`, w)
    const tabs = (await surfaceTabs(p)).filter((t) => !OPERATOR_SURFACES.has(t.id))
    for (const t of tabs) {
      await p.click(`[data-testid="${t.testid}"]`, { timeout: 8000 }).catch(() => {})
      await quiet(p, 25000)
      await p.waitForTimeout(600)
      await measure(p, `surface ${t.id} ${w}`, { reader: true, width: w })
      await layoutMetrics(p, `${t.id} ${w}`, w)
      result.consult.push({ width: w, where: t.id, boxWidth: await consultBox(p) })
      await shot(p, `W10-surface-${t.id}-${w}`)
      // the Subject page's flat inset, when the page shows the map
      if (t.id === 'subject' && (await p.locator('.dv-tab', { hasText: /World Map|Map/ }).count().catch(() => 0))) await mapFit(p, `subject inset ${w}`, w)
    }
    // and the map sent to the stage (the wide centre)
    await p.click('[data-testid="surface-tab-board"]', { timeout: 8000 }).catch(() => {})
    await quiet(p, 20000)
    const staged = await mapToStage(p)
    if (staged) {
      await quiet(p, 20000)
      await p.waitForTimeout(800)
      await mapFit(p, `stage ${w}`, w)
      await measure(p, `map on the stage ${w}`, { reader: true, width: w })
      await shot(p, `W10-map-stage-${w}`)
    } else (result.mapFits ??= []).push({ where: `stage ${w}`, width: w, ok: false, issues: ['the map did not reach the stage'] })
    await c.close()
  }

  // 4a. the map's projection is the reader's (U-7): on this software renderer
  // it opens flat; the reader's switch to the globe is kept across a reload,
  // then put back. Nothing here is a timed step: the globe's frame time on a
  // software renderer is reported by the map step above, never enforced.
  {
    const c = await context(browser, desk(1680, 1050))
    const p = await c.newPage(); wire(p, 'projection')
    const proj = () => p.locator('[data-testid="globe-map"]').first().getAttribute('data-projection').catch(() => null)
    await p.goto(BASE + '/', { timeout: 30000 }).catch(() => {})
    const ready = await p.waitForSelector('[data-testid="globe-map"][data-ready="1"]', { timeout: 30000 }).then(() => true).catch(() => false)
    if (!ready) {
      const flat = (await p.locator('[data-testid="maplibre-world"]').count().catch(() => 0)) > 0
      step('map: the projection choice is remembered', flat, flat ? 'the globe flag is off: no projection switch' : 'the map never became ready')
    } else {
      const software = (await p.locator('[data-testid="globe-map"]').first().getAttribute('data-software').catch(() => null)) === '1'
      const first = await proj()
      await p.click('[data-testid="globe-projection-globe"]', { timeout: 5000 }).catch(() => {})
      await p.waitForTimeout(1500)
      await p.reload({ timeout: 30000 }).catch(() => {})
      await p.waitForSelector('[data-testid="globe-map"][data-ready="1"]', { timeout: 30000 }).catch(() => {})
      const kept = await proj()
      await p.click('[data-testid="globe-projection-flat"]', { timeout: 5000 }).catch(() => {})
      await p.waitForTimeout(800)
      const ok = (!software || first === 'flat') && kept === 'globe'
      step('map: the projection choice is remembered', ok, `opened ${first}${software ? ' (software renderer)' : ''}; the globe chosen, after a reload ${kept}; put back to flat`)
    }
    await c.close()
  }

  // 4b. the frame is composable (U-6), at 1920×1080: the divider, the centre's
  // edge and a panel splitter drag AND are remembered across a reload; Consult
  // moves to a column and back, collapses to the ask bar and restores, pops to
  // the full centre and back — and the question box is usable in each state
  {
    const c = await context(browser, desk(1920, 1080))
    const p = await c.newPage(); wire(p, 'composable')
    await p.goto(BASE + '/', { timeout: 30000 }).catch(() => {})
    await p.waitForSelector(SHELL_READY, { timeout: 30000 }).catch(() => {})
    await quiet(p, 40000)
    await p.waitForTimeout(600)
    const reload = async () => {
      await p.waitForTimeout(700)
      await p.reload({ timeout: 30000 }).catch(() => {})
      await p.waitForSelector(SHELL_READY, { timeout: 30000 }).catch(() => {})
      await quiet(p, 30000)
      await p.waitForTimeout(600)
    }
    const record = (what, before, after, reloaded) => {
      const moved = before != null && after != null && Math.abs(after - before) >= 20
      const kept = after != null && reloaded != null && Math.abs(reloaded - after) <= 6
      result.drags.push({ what, before: Math.round(before ?? -1), after: Math.round(after ?? -1), reloaded: Math.round(reloaded ?? -1), moved, kept })
      step(`drag ${what}`, moved && kept, `${Math.round(before ?? -1)} → ${Math.round(after ?? -1)} → reload ${Math.round(reloaded ?? -1)} px`)
    }
    // the chat divider: up by 180 px (the chat grows)
    {
      const h0 = (await box(p, '[data-testid="chat-slot"]'))?.height ?? null
      await drag(p, '[data-testid="chat-divider"]', 0, -180)
      const h1 = (await box(p, '[data-testid="chat-slot"]'))?.height ?? null
      await reload()
      const h2 = (await box(p, '[data-testid="chat-slot"]'))?.height ?? null
      record('the chat divider', h0, h1, h2)
    }
    // the centre's edge: right by 120 px (the centre widens)
    {
      const w0 = (await box(p, '[data-testid="centre"]'))?.width ?? null
      await drag(p, '[data-testid="centre-edge"]', 120, 0)
      const w1 = (await box(p, '[data-testid="centre"]'))?.width ?? null
      await reload()
      const w2 = (await box(p, '[data-testid="centre"]'))?.width ?? null
      record('the centre edge', w0, w1, w2)
    }
    // a panel splitter (Dockview's sash between two panel regions)
    {
      const sashes = await p.locator('[data-testid="panel-side"] .dv-sash').all()
      let done = false
      for (const s of sashes) {
        const b = await s.boundingBox().catch(() => null)
        if (!b || b.width < 1 || b.height < 1) continue
        const horizontalBar = b.width > b.height
        const size = (g) => (g ? (horizontalBar ? g.height : g.width) : null)
        const g0 = await box(p, '[data-testid="panel-side"] .dv-groupview')
        const x = b.x + b.width / 2
        const y = b.y + b.height / 2
        await p.mouse.move(x, y)
        await p.mouse.down()
        for (let i = 1; i <= 8; i++) await p.mouse.move(x + (horizontalBar ? 0 : (-120 * i) / 8), y + (horizontalBar ? (-120 * i) / 8 : 0))
        await p.mouse.up()
        await p.waitForTimeout(500)
        const g1 = await box(p, '[data-testid="panel-side"] .dv-groupview')
        await reload()
        const g2 = await box(p, '[data-testid="panel-side"] .dv-groupview')
        record('a panel splitter', size(g0), size(g1), size(g2))
        done = true
        break
      }
      if (!done) record('a panel splitter', null, null, null)
    }
    await shot(p, 'W13-dragged-1920')
    // Consult moves, collapses, pops out — and is usable in each state
    const usable = async () => {
      const ta = await box(p, '[data-testid="consult-dock"] textarea')
      const stage = await box(p, '[data-testid="stage"]')
      return { box: ta ? Math.round(ta.width) : null, stage: !!stage }
    }
    const moveCase = async (what, click, check, shotName) => {
      const clicked = await p.click(`[data-testid="${click}"]`, { timeout: 5000 }).then(() => true).catch(() => false)
      await p.waitForTimeout(500)
      const u = await usable()
      const r = clicked ? await check(u) : { ok: false, note: `no ${click} button` }
      result.consultMoves.push({ what, ok: r.ok, note: r.note })
      step(`Consult ${what}`, r.ok, r.note)
      if (shotName) await shot(p, shotName)
    }
    await moveCase('moves to a right column', 'consult-move-right', async (u) => {
      const d = await box(p, '[data-testid="consult-dock"]')
      const ps = await box(p, '[data-testid="panel-side"]')
      const ok = !!d && !!ps && d.x >= ps.x + ps.width - 2 && u.box >= 300 && u.stage
      return { ok, note: `dock at x=${Math.round(d?.x ?? -1)}, panels end at ${Math.round((ps?.x ?? 0) + (ps?.width ?? 0))}; box ${u.box} px; stage ${u.stage ? 'shown' : 'gone'}` }
    }, 'W13-consult-right-1920')
    await moveCase('comes back under the stage', 'consult-move-bottom', async (u) => {
      const d = await box(p, '[data-testid="consult-dock"]')
      const st = await box(p, '[data-testid="stage"]')
      const ok = !!d && !!st && d.y >= st.y + st.height - 6 && u.box >= 300
      return { ok, note: `dock top ${Math.round(d?.y ?? -1)}, the stage's foot ${Math.round((st?.y ?? 0) + (st?.height ?? 0))}` }
    })
    await moveCase('collapses to the ask bar', 'consult-collapse', async (u) => {
      const h = (await box(p, '[data-testid="chat-slot"]'))?.height ?? 9999
      return { ok: h <= 200 && u.box >= 300 && u.stage, note: `chat ${Math.round(h)} px tall; box ${u.box} px` }
    }, 'W13-consult-collapsed-1920')
    await moveCase('restores from the ask bar', 'consult-expand', async (u) => {
      const h = (await box(p, '[data-testid="chat-slot"]'))?.height ?? 0
      return { ok: h >= 300 && u.box >= 300, note: `chat ${Math.round(h)} px tall` }
    })
    await moveCase('pops to the full centre', 'consult-full', async (u) => ({ ok: !u.stage && u.box >= 300, note: `stage ${u.stage ? 'still shown' : 'folded'}; box ${u.box} px` }), 'W13-consult-full-1920')
    await moveCase('comes back from the full centre', 'stage-peek', async (u) => ({ ok: u.stage && u.box >= 300, note: `stage ${u.stage ? 'shown' : 'missing'}` }))
    await c.close()
  }

  // 5. the subject page with nothing read yet
  if (subjectId) {
    const c = await context(browser, desk(1680, 1050))
    const p = await c.newPage(); wire(p, 'subject (held)')
    startHold()
    await p.goto(`${BASE}/#ws=subject&sel=target:${encodeURIComponent(subjectId)}`, { timeout: 30000 }).catch(() => {})
    await p.waitForSelector('[data-testid="subject-page"]', { timeout: 15000 }).catch(() => {})
    await p.waitForTimeout(2500)
    await zerosBeforeRead(p, 'subject page')
    endHold({ discard: !flag('forward-held') })
    await p.waitForTimeout(300)
    await c.close()
  }

  // 6. the phone reader at 390
  {
    const c = await context(browser, phone)
    const p = await c.newPage(); wire(p, 'phone')
    // M-0a — the phone landing's own request window: from navigation (the
    // held answers are released, not discarded, so they count) to quiet.
    net.phase = 'phone-landing'
    startHold()
    await p.goto(BASE + '/', { timeout: 30000 }).catch(() => {})
    await p.waitForTimeout(3000)
    await zerosBeforeRead(p, 'phone')
    endHold()
    await quiet(p, 40000)
    await p.waitForTimeout(1000)
    net.phase = 'phone-after-landing'
    const onPhone = new URL(p.url()).pathname.startsWith('/m')
    step('a phone is sent to the phone reader', onPhone, onPhone ? undefined : `stayed on ${new URL(p.url()).pathname}`)
    await stepLongTasks(p, 'phone', 0)
    const m = await measure(p, 'phone', { reader: true, width: 390 })
    for (const id of m?.machineIds ?? []) result.phoneMachineIds.push(`phone: ${id}`)
    await shot(p, 'W11-phone')
    // M-2: the phone shell's landing is Changed; a read opens from its rows
    // (the September list's cards are the fallback for an older bundle)
    await p.waitForSelector('[data-testid="changed-row"][data-kind="finding"], [data-testid="report-row"]', { timeout: 10000 }).catch(() => {})
    const card = (await p.locator('[data-testid="changed-row"][data-kind="finding"]').count().catch(() => 0))
      ? p.locator('[data-testid="changed-row"][data-kind="finding"]').first()
      : p.locator('[data-testid="report-row"]').first()
    if (await card.count().catch(() => 0)) {
      const since = await now(p)
      net.phase = 'phone-open'
      await card.click({ timeout: 5000 }).catch(() => {})
      const opened = await p.waitForSelector('[data-testid="report-view"]', { timeout: 20000 }).then(() => true).catch(() => false)
      await quiet(p, 20000)
      await p.waitForTimeout(800)
      net.phase = 'phone-after-open'
      step('phone: a card opens its read', opened, opened ? undefined : 'no report view after the tap')
      await stepLongTasks(p, 'phone: one card opened', since)
      const m2 = await measure(p, 'phone card', { reader: true, width: 390 })
      for (const id of m2?.machineIds ?? []) result.phoneMachineIds.push(`phone card: ${id}`)
      await shot(p, 'W12-phone-card')
    }
    // M-0a — Consult on the phone: the composer row at 390, then a mocked answer.
    await phoneConsult(p, 390, { answer: true })
    await phoneFavicon(c)
    await c.close()
  }

  // 6b. M-0a — the composer row at 360 (the narrowest phone in the plan's set)
  {
    const c = await context(browser, { ...phone, viewport: { width: 360, height: 740 } })
    const p = await c.newPage(); wire(p, 'phone 360')
    net.phase = 'phone-360'
    await p.goto(BASE + '/m/', { timeout: 30000 }).catch(() => {})
    await p.waitForSelector('[data-testid="tab-consult"], [data-testid="consult-toggle"]', { timeout: 20000 }).catch(() => {})
    await quiet(p, 20000)
    await phoneConsult(p, 360, { answer: false })
    await c.close()
  }

  // 6c. P-1 / P-3 — the installable phone app: manifest, worker, cache, offline, update
  await phoneApp(browser, phone)
  // 6c. M-2 — the phone shell: every screen at 390 and 360, the floors at 430
  // and in landscape, a touch tablet, a #rec= link (tools/walk/phone.mjs)
  if (fs.existsSync(path.join(DIST, 'm', 'index.html'))) {
    await walkPhoneShell({ browser, context, wire, net, step, shot, quiet, stepLongTasks, now, result, BASE, DIST, withTimeout, landingStats, log, MODE })
  }
  net.phase = 'end'

  await browser.close()
  server.close()

  result.requests = {
    total: net.total, paths: net.paths.size, refusedWrites: net.refusedWrites.length, blockedOutside: net.blockedOutside,
    unfixtured: [...net.unfixtured], standIns: [...net.standIns], slow: net.slow, refusedGets: [...new Set(net.refusedGets)],
  }
  result.requestLog = net.log
  result.landing = landingStats(net.log, 'landing', result.hiddenFetches)
  // M-0a — the phone landing (to quiet) and the one read a tap opens
  result.phoneLanding = landingStats(net.log, 'phone-landing')
  result.phoneOpen = landingStats(net.log, 'phone-open')
  // phone machine ids are deduplicated across the two views
  result.phoneMachineIds = [...new Set(result.phoneMachineIds)]

  if (RECORD) {
    const src = path.join(HERE, '..', '..', 'src')
    const san = makeSanitizer({ whitelist: literalsFromSource(src), keyNames: identifiersFromSource(src), arrayCap: 10, stringCap: 600 })
    const entries = {}
    const firstPages = new Set([...recorded.keys()].filter((k) => !/[?&]cursor=/.test(k)).map((k) => k.split('?')[0]))
    for (const [k, v] of recorded) {
      if (/[?&]cursor=/.test(k) && firstPages.has(k.split('?')[0])) continue // pagination stops at the first page
      // the Flow graph keeps every node (the walk's refused expansion needs a group over the budget) and its first 500 edges
      const flow = /\/flow\//.test(k)
      const body = san.value(v.body, flow ? 0 : 10)
      if (flow && body && Array.isArray(body.edges)) body.edges = body.edges.slice(0, 500)
      entries[san.requestKey(k)] = { status: v.status, body }
    }
    fs.mkdirSync(path.dirname(path.resolve(RECORD)), { recursive: true })
    fs.writeFileSync(path.resolve(RECORD), JSON.stringify({ note: 'Synthetic: recorded from live GET shapes, every string rewritten by tools/walk/sanitize.mjs. No owner content.', recorded_at: result.at.slice(0, 10), entries }) + '\n')
    log(`RECORDED ${Object.keys(entries).length} responses (sanitized) to ${RECORD}`)
  }

  const evaluation = evaluate(result, budgets, MODE)
  // roll 89: the checks a signed-in account's role kept the walk from making, by name
  const refused = Object.entries(result.roleRefused).sort((a, b) => b[1] - a[1])
  const skippedLines = (result.skipped.length
    ? `\nSKIPPED for the account's role (${session.role ?? 'unknown'}): ${result.skipped.length}\n` +
      result.skipped.map((k) => `  [skip  ] ${k.name} — needs ${k.needs} (${k.reads.join(', ')})`).join('\n')
    : '') + (refused.length
    ? `\nREFUSED for the account's role (403, kept out of the budgets): ${refused.reduce((n, [, c]) => n + c, 0)} answers on ${refused.length} routes\n` +
      refused.map(([p, c]) => `  [403   ] ${p} ×${c}`).join('\n')
    : '')
  const report = scrub(formatReport(result, evaluation) + skippedLines)
  fs.writeFileSync(path.join(OUT, 'report.txt'), report + '\n')
  fs.writeFileSync(path.join(OUT, 'result.json'), scrub(JSON.stringify({ ...result, evaluation }, null, 1)))
  console.log(report)
  process.exit(evaluation.failures.length ? 1 : 0)
}

main().catch((e) => { console.error('WALK_ERROR', scrub(String(e?.stack ?? e).slice(0, 600))); process.exit(2) })
