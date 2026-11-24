// @vitest-environment node
/**
 * The walk's pure parts: the fixture sanitizer (no owner content can reach a
 * committed fixture) and the budget evaluation (what fails the gate).
 */
import { readFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
// @ts-expect-error — plain ESM tool modules, no type declarations
import { FILLER, addLiterals, literalsFromSource, makeSanitizer, unexpectedWords } from './sanitize.mjs'
// @ts-expect-error — plain ESM tool modules, no type declarations
import { DEFAULT_BUDGETS, evaluate, formatReport, heldBy, landingStats, recordReadUnbounded, recordRouteOf, recordSheetStats } from './budgets.mjs'
// @ts-expect-error — plain ESM tool modules, no type declarations
import { PICKABLE, pickPlace } from './pick.mjs'
// @ts-expect-error — plain ESM tool modules, no type declarations
import { EVENT_SUMMARY_COLUMNS, SIGNAL_SUMMARY_COLUMNS, atRequestedWeight, lightSignalData, placesDeclaringNoSource, summaryDescriptorBody, summaryEventRow } from './weights.mjs'
import { withRecordAddress } from './record.mjs'
// @ts-expect-error — plain ESM tool modules, no type declarations
import { ROLE_GATED, ROLE_RANK, passwordFromFile, roleFrom, roleGateFor, sessionCookieFrom, skipsForRole } from './signin.mjs'

const HERE = __dirname
const secret = Buffer.alloc(32, 7)

describe('sanitize: recorded live shapes become neutral fixtures', () => {
  const whitelist = addLiterals(`const a = 'escalating'; const b = '/v3/ledger/scorecard'; const k = 'target'; const s = 'not measured'; const u = \`/api/v1/targets/\${id}/brief\``)
  const san = makeSanitizer({ whitelist, secret })

  it('rewrites every prose word into filler, keeping numbers, dates and markdown', () => {
    const out = san.string('## Ruritania border talks\n\nThe ministry said 12 units moved on 2026-11-12 [1].')
    expect(out).toMatch(/^## /)
    expect(out).toContain('12')
    expect(out).toContain('2026-11-12')
    expect(out).toContain('[1]')
    for (const w of out.match(/[A-Za-z]{3,}/g)) expect(FILLER.map((f: string) => f.toLowerCase())).toContain(w.toLowerCase())
    expect(out).not.toMatch(/Ruritania|ministry|border/)
  })

  it('maps ids consistently in bodies and in request paths, so joins still resolve', () => {
    const body = san.value({ target_id: 'country_zz_rt', items: [{ id: 'country_zz_rt' }] })
    expect(body.target_id).toBe(body.items[0].id)
    expect(body.target_id).not.toContain('zz_rt')
    const key = san.requestKey('/api/v1/targets/country_zz_rt/brief?subject=country_zz_rt&limit=20')
    expect(key).toBe(`/api/v1/targets/${body.target_id}/brief?subject=${body.target_id}&limit=20`)
  })

  it('keeps literals the UI compares against, generic state words and timestamps; maps prefixed ids', () => {
    expect(san.string('escalating')).toBe('escalating')
    expect(san.string('not measured')).toBe('not measured')
    expect(san.string('2026-11-13T08:00:00Z')).toBe('2026-11-13T08:00:00Z')
    expect(san.string('moderate')).toBe('moderate')
    expect(san.string('target:country_zz_rt')).toMatch(/^target:/)
    expect(san.string('target:country_zz_rt')).not.toContain('zz_rt')
    expect(san.string('https://news.example.com/a/b')).toMatch(/^https:\/\/example\.org\/r\//)
  })

  it('rewrites the keys of an object keyed by ids, not ordinary field names', () => {
    const v = san.value({ by_subject: { ruritania_desk: 1, freedonia_desk: 2, sylvania_desk: 3 }, count: 3 })
    expect(Object.keys(v)).toEqual(['by_subject', 'count'])
    expect(Object.keys(v.by_subject).join(' ')).not.toMatch(/ruritania|freedonia|sylvania/)
  })

  it('the leak check catches a word that is not filler', () => {
    expect(unexpectedWords({ a: 'alpha beta Ruritania' }, new Set())).toEqual(['beta', 'Ruritania'])
    expect(unexpectedWords(san.value({ a: 'The Ruritania talks stalled' }), whitelist)).toEqual([])
  })
})

describe('the committed fixture set carries no owner content', () => {
  const file = join(HERE, 'fixtures', 'api.json')
  it.runIf(existsSync(file))('every string in fixtures/api.json is filler, a UI literal, a mapped id, a number or a date', () => {
    const fx = JSON.parse(readFileSync(file, 'utf8'))
    const whitelist = literalsFromSource(join(HERE, '..', '..', 'src'))
    const left = unexpectedWords(fx.entries, whitelist)
    expect(left.slice(0, 20)).toEqual([])
    // U-10: the pattern-matched routes (`fixtures.routes`) are held to the same rule
    expect(unexpectedWords((fx.routes ?? []).map((r: { entry: unknown }) => r.entry), whitelist).slice(0, 20)).toEqual([])
  })
})

describe('budgets: what fails the walk', () => {
  const clean = {
    mode: 'fixture', base: 'b', at: 't', textFloorPx: 12,
    longTasks: [{ step: 'landing', maxMs: 200 }], landingReadyMs: 3000,
    overflow: [1680, 1366, 1920, 2560, 3840, 390].map((w) => ({ where: 'x', width: w, docWidth: w })),
    surfaces: [{ name: 'landing', reader: true, smallText: 40, width: 1680 }],
    consult: [{ width: 1680, boxWidth: 398 }, { width: 1366, boxWidth: 319 }],
    zeroBeforeRead: [], errors: [], phoneMachineIds: [], steps: [{ name: 'landing', ok: true }],
    layout: [
      { where: 'landing 1920', viewport: 1920, emptyBandPx: 40, bodyTextPx: 14, chatWidthRem: 46, chatWidthPx: 736 },
      { where: 'landing 3840', viewport: 3840, emptyBandPx: 90, bodyTextPx: 19.5, chatWidthRem: 46, chatWidthPx: 1104 },
    ],
    drags: ['the chat divider', 'the centre edge', 'a panel splitter'].map((what) => ({ what, before: 400, after: 580, reloaded: 580, moved: true, kept: true })),
    consultMoves: [{ what: 'moves to a right column', ok: true, note: 'ok' }],
  }

  it('a clean walk passes; reader text under the floor fails by default (U-3) and is only reported when asked', () => {
    const e = evaluate(clean, { ...DEFAULT_BUDGETS, enforceTextFloor: false }, 'fixture')
    expect(e.failures).toEqual([])
    expect(e.table.find((r: { check: string }) => r.check.startsWith('reader text')).verdict).toBe('report')
    expect(DEFAULT_BUDGETS.enforceTextFloor).toBe(true)
    const enforced = evaluate(clean, DEFAULT_BUDGETS, 'fixture')
    expect(enforced.failures.join()).toMatch(/reader text under 12 px/)
    const floorClean = { ...clean, surfaces: [{ ...clean.surfaces[0], smallText: 0 }] }
    expect(formatReport(floorClean, evaluate(floorClean, DEFAULT_BUDGETS, 'fixture'))).toMatch(/WALK_VERDICT PASS/)
  })

  it('each breach is named in the failure list', () => {
    const bad = {
      ...clean,
      longTasks: [{ step: 'flow: default', maxMs: 4200 }],
      landingReadyMs: null,
      overflow: [...clean.overflow, { where: 'phone', width: 390, docWidth: 512 }],
      consult: [{ width: 1366, boxWidth: 34 }],
      zeroBeforeRead: [{ where: 'landing', text: '0 active situations' }],
      errors: ['desktop: page error: boom'],
      phoneMachineIds: ['phone: country_zz_rt'],
      steps: [{ name: 'Flow panel', ok: false, note: 'no Flow tab' }],
    }
    const f = evaluate(bad, DEFAULT_BUDGETS, 'fixture').failures.join('\n')
    for (const needle of ['4200 ms', 'landing ready', 'overflow at 390', '34 px', '0 active situations', 'boom', 'country_zz_rt', 'Flow panel']) {
      expect(f).toContain(needle)
    }
  })

  it('the frame budgets (U-6): an empty band, tiny text at 3840, a ribbon of a chat, a splitter that does not stick', () => {
    const shell = {
      ...clean,
      layout: [
        // the owner's 3840 screenshot: a dead band, 13 px text
        { where: 'landing 3840', viewport: 3840, emptyBandPx: 700, bodyTextPx: 13, chatWidthRem: 46, chatWidthPx: 1104 },
        // the owner's 1920 screenshot: Consult a 380 px ribbon (23.75 rem)
        { where: 'landing 1920', viewport: 1920, emptyBandPx: 20, bodyTextPx: 12, chatWidthRem: 23.8, chatWidthPx: 380 },
      ],
      drags: [{ what: 'the chat divider', before: 400, after: 580, reloaded: 400, moved: true, kept: false }],
      consultMoves: [{ what: 'moves to a right column', ok: false, note: 'no consult-move-right button' }],
    }
    const f = evaluate(shell, DEFAULT_BUDGETS, 'fixture').failures.join('\n')
    for (const needle of ['no empty band at landing 3840', 'body text at landing 3840', 'chat reading width at landing 1920', 'drag the chat divider', 'drag the centre edge: not dragged', 'Consult moves to a right column']) {
      expect(f).toContain(needle)
    }
    expect(f).not.toContain('body text at landing 1920')
  })
})

describe('the landing request budget (U-5)', () => {
  const log = [
    { phase: 'start', url: '/api/v1/auth/me', bytes: 50 },
    { phase: 'landing', url: '/api/v1/auth/me', bytes: 110 },
    { phase: 'landing', url: '/api/v1/findings?limit=1', bytes: 30_000 },
    { phase: 'landing', url: '/api/v1/signals?limit=500', bytes: 20_000 },
    { phase: 'landing', url: '/api/v1/signals?limit=500', bytes: 20_000 },
    { phase: 'after-landing', url: '/api/v1/findings?limit=500', bytes: 9e6 },
  ]

  it('summarises only the landing window: requests, bytes, /findings, the largest, duplicates, hidden fetches', () => {
    const s = landingStats(log, 'landing', [{ key: '["wall"]', hidden: true, tile: 'tile:system.wall' }, { key: '["x"]', hidden: false }])
    expect(s.requests).toBe(4)
    expect(s.bytes).toBe(70_110)
    expect(s.findingsRequests).toBe(1)
    expect(s.findingsBytes).toBe(30_000)
    expect(s.largest).toEqual({ url: '/api/v1/findings?limit=1', bytes: 30_000 })
    expect(s.duplicates).toEqual([{ url: '/api/v1/signals?limit=500', n: 2 }])
    expect(s.duplicateRequests).toBe(1)
    expect(s.hidden).toEqual(['tile:system.wall: ["wall"]'])
    expect(landingStats(log, 'landing', null).hidden).toBeNull()
  })

  it('fails on a duplicate, a hidden-region fetch, a missing fetch log, and each size budget', () => {
    const base = {
      mode: 'fixture', longTasks: [], landingReadyMs: 1000, surfaces: [], consult: [],
      overflow: [1680, 1366, 1920, 2560, 3840, 390].map((w) => ({ where: 'x', width: w, docWidth: w })),
      zeroBeforeRead: [], errors: [], phoneMachineIds: [], steps: [],
      // the U-6 frame's drags, held (this test is about the request budget)
      drags: ['the chat divider', 'the centre edge', 'a panel splitter'].map((what) => ({ what, before: 400, after: 580, reloaded: 580, moved: true, kept: true })),
    }
    const budgets = { ...DEFAULT_BUDGETS, landingRequests: { fixture: 3 }, landingBytes: { fixture: 60_000 }, largestResponseBytes: { fixture: 25_000 } }
    const f = evaluate({ ...base, landing: landingStats(log, 'landing', [{ key: '["wall"]', hidden: true, tile: 'tile:w' }]) }, budgets, 'fixture').failures.join('\n')
    for (const needle of ['landing requests', 'landing bytes', 'largest landing response', 'duplicate landing requests', 'fetches by hidden regions']) {
      expect(f).toContain(needle)
    }
    const noLog = evaluate({ ...base, landing: landingStats(log.slice(0, 3), 'landing', null) }, DEFAULT_BUDGETS, 'fixture').failures.join('\n')
    expect(noLog).toMatch(/fetches by hidden regions: not measured/)
    const clean = evaluate({ ...base, landing: landingStats(log.slice(0, 4), 'landing', []) }, DEFAULT_BUDGETS, 'fixture')
    expect(clean.failures).toEqual([])
  })
})

describe('the phone budgets (M-0a): what the 2026-11-14 phone audit found', () => {
  const base = {
    mode: 'fixture', longTasks: [], landingReadyMs: 1000, surfaces: [], consult: [],
    overflow: [1680, 1366, 1920, 2560, 3840, 390].map((w) => ({ where: 'x', width: w, docWidth: w })),
    zeroBeforeRead: [], errors: [], phoneMachineIds: [], steps: [],
    drags: ['the chat divider', 'the centre edge', 'a panel splitter'].map((what) => ({ what, before: 400, after: 580, reloaded: 580, moved: true, kept: true })),
  }
  const okChip = { tag: 'BUTTON', box: '23×12', ok: true }
  const good = {
    ...base,
    phoneLanding: landingStats([
      { phase: 'phone-landing', url: '/api/v1/auth/me', bytes: 150 },
      { phase: 'phone-landing', url: '/api/v1/findings?analyst_id_in=a&limit=80&fields=summary', bytes: 7_500 },
    ], 'phone-landing'),
    phoneOpen: landingStats([{ phase: 'phone-open', url: '/api/v1/findings?analyst_id=a&target_id_null=true&since=t&limit=1', bytes: 36_000 }], 'phone-open'),
    phoneConsult: [
      { width: 390, composer: { row: 362, box: 290, fraction: 0.8, controls: 1, sendVisible: true }, answer: { rawBold: false, pipeTable: false, table: true, rawIds: false, chips: [okChip, okChip] } },
      { width: 360, composer: { row: 332, box: 260, fraction: 0.78, controls: 1, sendVisible: true }, answer: null },
    ],
    favicon: [{ path: '/favicon.svg', status: 200, type: 'image/svg+xml' }, { path: '/m/favicon.svg', status: 200, type: 'image/svg+xml' }],
  }

  it('the fixed phone passes', () => {
    expect(evaluate(good, DEFAULT_BUDGETS, 'fixture').failures).toEqual([])
  })

  it('the audit\'s phone fails, each defect by name', () => {
    const audit = {
      ...good,
      // the full-weight navigator read (live: 9.2 MB)
      phoneLanding: landingStats([{ phase: 'phone-landing', url: '/api/v1/findings?analyst_id_in=a&limit=80', bytes: 9_200_000 }], 'phone-landing'),
      phoneConsult: [
        // the question box squeezed by "Allow changes" and Ask; raw markdown; [N] as text
        { width: 390, composer: { row: 362, box: 158, fraction: 0.44, controls: 3, sendVisible: true }, answer: { rawBold: true, pipeTable: true, table: false, rawIds: false, chips: [] } },
        { width: 360, composer: { row: 332, box: 128, fraction: 0.39, controls: 3, sendVisible: false }, answer: null },
      ],
      // the icon came back as index.html
      favicon: [{ path: '/favicon.svg', status: 200, type: 'text/html' }],
    }
    const f = evaluate(audit, DEFAULT_BUDGETS, 'fixture').failures.join('\n')
    for (const needle of [
      'phone landing bytes', 'largest phone landing response',
      'phone 390: question box share', 'phone 360: question box share', 'phone 360: Ask reachable',
      'phone 390: answer renders markdown', 'phone 390: answer citations are buttons', 'icon /favicon.svg',
    ]) expect(f).toContain(needle)
  })

  it('M-1: a phone turn that streams, draws its contract and stops passes; each defect fails by name', () => {
    const turn = {
      streamed: true, contract: true, partial: true,
      sends: { window: true, grounding: true, onScreen: true, model: 'core', writes: false },
      stop: { shown: true, stops: 1, synths: 1, settled: true, askBack: true, synthModel: 'core' },
    }
    const withTurn = (t: unknown) => ({ ...good, phoneConsult: [{ ...good.phoneConsult[0], turn: t }, good.phoneConsult[1]] })
    expect(evaluate(withTurn(turn), DEFAULT_BUDGETS, 'fixture').failures).toEqual([])
    const broken = {
      streamed: false, contract: false, partial: false,
      sends: { window: false, grounding: true, onScreen: false, model: 'opus', writes: true },
      stop: { shown: false, stops: 0, synths: 0, settled: false, askBack: true, synthModel: null },
    }
    const f = evaluate(withTurn(broken), DEFAULT_BUDGETS, 'fixture').failures.join('\n')
    for (const needle of [
      'phone 390: the answer streams', 'phone 390: the turn contract is drawn',
      "phone 390: the request carries the desktop's fields", 'phone 390: Stop works',
    ]) expect(f).toContain(needle)
  })

  it('a citation that is not a button, or whose target is short of 44 px, fails', () => {
    const r = { ...good, phoneConsult: [{ ...good.phoneConsult[0], answer: { ...good.phoneConsult[0].answer, chips: [okChip, { tag: 'SPAN', box: '27×32', ok: true }, { tag: 'BUTTON', box: '27×32', ok: false }] } }] }
    expect(evaluate(r, DEFAULT_BUDGETS, 'fixture').failures.join('\n')).toMatch(/2 of 3 fail/)
  })
})

describe('a summary read gets the summary row from a full recording (roll 86: the fixture walk could not see what the light roster dropped)', () => {
  const golden = JSON.parse(readFileSync(join(HERE, 'fixtures', 'weights_golden.json'), 'utf8'))

  it('mirrors the registry\'s projections case for case (fixtures/weights_golden.json, held on the Python side too)', () => {
    for (const c of golden.descriptor_body) expect(summaryDescriptorBody(c.in)).toEqual(c.out)
    for (const c of golden.signal_data) expect(lightSignalData(c.in)).toEqual(c.out)
    expect(SIGNAL_SUMMARY_COLUMNS).toEqual(golden.signal_summary_columns)
    for (const c of golden.event_rows) expect(summaryEventRow(c.in)).toEqual(c.out)
    expect(EVENT_SUMMARY_COLUMNS).toEqual(golden.event_summary_columns)
  })

  it('cuts the events at fields=summary only (roll 87: the Watch page over its 2 MB budget); the default stays whole', () => {
    const q = (s: string) => new URLSearchParams(s)
    const page = { data: [{ id: 'e', title: 't', summary: 'long', locations: ['x'], geo_lat: 1, geo_lon: 2 }], next_cursor: null }
    expect(atRequestedWeight('/api/v1/v3/events', q('limit=500&fields=summary'), page).data[0]).toEqual({ id: 'e', title: 't', geo_lat: 1, geo_lon: 2 })
    expect(atRequestedWeight('/api/v1/v3/events', q('limit=500'), page)).toBe(page)
    expect(atRequestedWeight('/api/v1/v3/events', q('fields=full'), page)).toBe(page)
  })

  it('cuts the roster at fields=summary and the signals at summary or no fields; full stays full', () => {
    const roster = [{ descriptor_id: 'x', body: { scope: { geo: ['FR'] }, sources: [{ id: 'a' }] } }]
    const q = (s: string) => new URLSearchParams(s)
    expect(atRequestedWeight('/api/v1/registry/descriptors', q('fields=summary'), roster)[0].body).toEqual({ scope: { geo: ['FR'] }, declared_sources: 1 })
    expect(atRequestedWeight('/api/v1/registry/descriptors', q(''), roster)).toBe(roster)
    const page = { data: [{ id: 's', title: 't', archived: false, data: { body: 'x', geo: { lat: 1, lon: 2, country: 'Y' } } }], next_cursor: null }
    const light = atRequestedWeight('/api/v1/signals', q('limit=500&fields=summary'), page)
    expect(light.data[0]).toEqual({ id: 's', title: 't', data: { geo: { lat: 1, lon: 2 } } })
    expect(atRequestedWeight('/api/v1/signals', q('limit=500'), page).data[0]).toEqual(light.data[0])
    expect(atRequestedWeight('/api/v1/signals', q('fields=full'), page)).toBe(page)
  })

  it('counts a place as declared uncovered only when every subject naming it says no source', () => {
    const rows = [
      { body: { scope: { geo: ['fr'] }, declared_sources: 0 } },
      { body: { scope: { geo: ['FR', 'DE'] }, declared_sources: 2 } },
      { body: { scope: { geo: ['GH'] }, sources: [] } },
      { body: { scope: { geo: ['OM'] }, declared_sources: 0 } },
      { body: { scope: { geo: ['YE'] } } }, // says nothing: not counted
    ]
    expect([...placesDeclaringNoSource(rows)].sort()).toEqual(['GH', 'OM'])
  })
})

describe('the place pick clicks the map, not its chrome (U-8, roll 80 "no accent outline")', () => {
  // A 400×300 map at (100, 50). Washed land everywhere; the layer card covers
  // the top-left 200×120; a forecast ring sits on top of the land at (216, 200).
  function fakeDoc({ ringAt = null as null | [number, number], hatched = false } = {}) {
    const canvas = { tag: 'canvas' }
    const card = { tag: 'layer-card' }
    const map = {
      getLayer: () => true,
      getLayoutProperty: () => 'visible',
      queryRenderedFeatures: ([x, y]: [number, number], o: { layers: string[] }) => {
        const out = []
        if (ringAt && x === ringAt[0] && y === ringAt[1] && o.layers.includes('forecast-rings')) out.push({ layer: { id: 'forecast-rings' }, state: {}, properties: {} })
        if (hatched) out.push({ layer: { id: 'hatch-sparse' }, state: {}, properties: { iso: x < 300 ? 'CA' : 'US', kind: 'sparse' } })
        out.push({ layer: { id: 'land' }, state: { wash: 2 }, properties: { ISO_A2_EH: x < 300 ? 'CA' : 'US' } })
        return out
      },
    }
    const container = {
      __legbaMap: map,
      querySelector: () => canvas,
      getBoundingClientRect: () => ({ left: 100, top: 50, width: 400, height: 300 }),
    }
    return {
      querySelector: () => container,
      elementFromPoint: (cx: number, cy: number) => (cx - 100 < 200 && cy - 50 < 120 ? card : canvas),
    }
  }

  it('skips pixels under the map\'s own layer card (the roll-80 failure)', () => {
    const pt = pickPlace(fakeDoc(), PICKABLE)
    expect(pt).not.toBeNull()
    // the first clear pixel scanning from the top-left: to the right of the card
    expect(pt.x - 100).toBeGreaterThanOrEqual(200)
    expect(pt.y - 50).toBeLessThan(120)
  })

  it('skips a pixel where another pickable feature is on top of the land', () => {
    const first = pickPlace(fakeDoc(), PICKABLE)
    const pt = pickPlace(fakeDoc({ ringAt: [first.x - 100, first.y - 50] }), PICKABLE)
    expect(pt).not.toEqual(first)
  })

  it('a blank\'s hatch over a changed place is the place: the map\'s click selects it by iso (roll 86 live: every changed area was hatched)', () => {
    const pt = pickPlace(fakeDoc({ hatched: true }), PICKABLE)
    expect(pt).not.toBeNull()
    expect(pt).toEqual(pickPlace(fakeDoc(), PICKABLE))
    expect(pt.iso).toBe('CA')
  })

  it('returns null when no map is mounted', () => {
    expect(pickPlace({ querySelector: () => null }, PICKABLE)).toBeNull()
  })

  it('names the request that held the landing ready open', () => {
    const log = [
      { phase: 'landing', url: '/a', start: 0, ms: 100 },
      { phase: 'landing', url: '/api/v1/v3/system/judge-stats?days=14', start: 50, ms: 18_000 },
      { phase: 'landing', url: '/c', start: 10_000, ms: 200 },
      { phase: 'after-landing', url: '/late', start: 30_000, ms: 1 },
      { phase: 'landing', url: '/pending', start: 1, ms: null },
    ]
    expect(heldBy(log)).toEqual({ url: '/api/v1/v3/system/judge-stats?days=14', ms: 18_000 })
    expect(heldBy([])).toBeNull()
  })
})

describe('the Watch page (U-9)', () => {
  const file = join(HERE, 'fixtures', 'watch.json')
  it.runIf(existsSync(file))('its fixtures are invented: example sources, example.org pages — nothing recorded', () => {
    const fx = JSON.parse(readFileSync(file, 'utf8'))
    expect(fx.note).toMatch(/INVENTED/)
    const text = JSON.stringify(fx.entries)
    for (const id of text.match(/"source\.[a-z0-9_.]+"/g) ?? []) expect(id).toMatch(/^"source\.example\./)
    for (const url of text.match(/https?:\/\/[^"\s]+/g) ?? []) expect(url).toMatch(/^https:\/\/example\.org\//)
    for (const k of Object.keys(fx.entries)) expect(k).toMatch(/^\/api\/v1\/(v3\/(system\/alerts\/log|system\/source-firing|evidence\/drift)|registry\/descriptors\/source\/source\.example\.)/)
  })

  it('fails on too many requests, too many bytes and a duplicate; passes within them', () => {
    const base = {
      mode: 'fixture', longTasks: [], landingReadyMs: 1000, surfaces: [], consult: [],
      overflow: [1680, 1366, 1920, 2560, 3840, 390].map((w) => ({ where: 'x', width: w, docWidth: w })),
      zeroBeforeRead: [], errors: [], phoneMachineIds: [], steps: [],
      drags: ['the chat divider', 'the centre edge', 'a panel splitter'].map((what) => ({ what, before: 400, after: 580, reloaded: 580, moved: true, kept: true })),
    }
    const over = evaluate({ ...base, watch: { requests: 99, bytes: 9e6, largest: null, duplicates: 1 } }, DEFAULT_BUDGETS, 'fixture').failures.join('\n')
    for (const needle of ['Watch requests', 'Watch bytes', 'duplicate Watch requests']) expect(over).toContain(needle)
    const ok = evaluate({ ...base, watch: { requests: 10, bytes: 200_000, largest: null, duplicates: 0 } }, DEFAULT_BUDGETS, 'fixture')
    expect(ok.failures).toEqual([])
    expect(ok.table.filter((r: { check: string }) => r.check.includes('Watch')).length).toBe(3)
  })
})


describe('the Record sheet budget (U-10)', () => {
  const F = 'ex_u10fnd'
  const log = [
    { phase: 'landing', url: '/api/v1/findings?limit=40', bytes: 900 },
    { phase: 'record: a Board row', url: `/api/v1/lineage/finding/${F}?direction=upstream&depth=1`, bytes: 6000 },
    { phase: 'record: a Board row', url: '/api/v1/v3/record/receipts?kind=signal&ids=a,b', bytes: 4000 },
    { phase: 'record: a Board row', url: '/api/v1/v3/record/integrity?run_id=r', bytes: 600 },
    { phase: 'record: a Board row', url: `/api/v1/lineage/finding/${F}?direction=upstream&depth=6`, bytes: 9000 },
    { phase: 'record: a Board row (text)', url: '/api/v1/v3/record/version/102/text?max_chars=20000', bytes: 800 },
  ]

  it('names the sheet routes and their bounds', () => {
    expect(recordRouteOf(`/api/v1/lineage/finding/${F}?direction=upstream&depth=1`)).toBe('lineage')
    expect(recordRouteOf(`/api/v1/lineage/finding/${F}?direction=upstream&depth=6`)).toBeNull()
    expect(recordReadUnbounded('/api/v1/v3/record/receipts?kind=signal&ids=' + Array.from({ length: 9 }, (_, i) => `s${i}`).join(','))).toBe(true)
    expect(recordReadUnbounded('/api/v1/v3/record/version/1/text?max_chars=50000')).toBe(true)
    expect(recordReadUnbounded('/api/v1/v3/record/version/1/text')).toBe(true)
    expect(recordReadUnbounded('/api/v1/v3/record/version/1/text?max_chars=20000')).toBe(false)
  })

  it('counts only the sheet reads of one phase, and reports the others', () => {
    const st = recordSheetStats(log, 'record: a Board row')
    expect(st.requests).toBe(3)
    expect(st.bytes).toBe(10600)
    expect(st.routes).toEqual(['lineage', 'receipts', 'integrity'])
    expect(st.others).toEqual([`/api/v1/lineage/finding/${F}`])
  })

  it('passes a bounded open and fails a slow one, a fourth read, a missing address, or a sheet read in the landing', () => {
    const open = { from: 'a Board row', phase: 'record: a Board row', opened: true, readyMs: 900, address: `finding:${F}`, text: { shown: true, kind: 'text' } }
    const good = evaluate({ steps: [], requestLog: log, recordSheet: [open] }, DEFAULT_BUDGETS, 'fixture')
    expect(good.failures.filter((f: string) => /record|Record/.test(f))).toEqual([])
    const bad = evaluate(
      {
        steps: [],
        requestLog: [...log, { phase: 'record: a Board row', url: '/api/v1/v3/record/integrity?run_id=x', bytes: 1 }, { phase: 'landing', url: '/api/v1/v3/record/integrity?run_id=y', bytes: 1 }],
        recordSheet: [{ ...open, readyMs: 9000, address: null }],
      },
      DEFAULT_BUDGETS,
      'fixture',
    )
    const f = bad.failures.join('\n')
    expect(f).toMatch(/record sheet ready from a Board row: 9000 ms/)
    expect(f).toMatch(/record sheet reads from a Board row: 4/)
    expect(f).toMatch(/record sheet address from a Board row: none/)
    expect(f).toMatch(/the Record sheet's reads in the landing: 1/)
  })
})
describe('the Ledger budget (U-11): no score below five, light reads', () => {
  const base = {
    mode: 'fixture', longTasks: [], landingReadyMs: 1000, surfaces: [], consult: [],
    overflow: [1680, 1366, 1920, 2560, 3840, 390].map((w) => ({ where: 'x', width: w, docWidth: w })),
    zeroBeforeRead: [], errors: [], phoneMachineIds: [], steps: [],
    drags: ['the chat divider', 'the centre edge', 'a panel splitter'].map((what) => ({ what, before: 400, after: 580, reloaded: 580, moved: true, kept: true })),
  }
  const reads = landingStats(
    [
      { phase: 'ledger', url: '/api/v1/v3/ledger/forecasts', start: 0, ms: 20, bytes: 20_000 },
      { phase: 'ledger', url: '/api/v1/v3/system/receipt-anchors?limit=60', start: 0, ms: 20, bytes: 3_000 },
      { phase: 'ledger', url: '/api/v1/v3/eval/runs', start: 0, ms: 20, bytes: 2 },
    ],
    'ledger',
  )
  const ledger = { forecasts: 8, trackState: 'withheld', scored: false, resolved: 4, floor: 5, scoreShown: false, brierText: false, proofState: 'none_published', requests: reads }

  it('a withheld track record with no score passes', () => {
    expect(evaluate({ ...base, ledger }, DEFAULT_BUDGETS, 'fixture').failures).toEqual([])
  })
  it('a score, or a Brier figure, shown below five fails', () => {
    for (const leak of [{ scoreShown: true }, { brierText: true }, { scored: true }]) {
      const f = evaluate({ ...base, ledger: { ...ledger, ...leak } }, DEFAULT_BUDGETS, 'fixture').failures.join('\n')
      expect(f).toMatch(/ledger: no score below five resolutions/)
    }
  })
  it('the evaluation record must answer: published or none published, never not served or unreadable (E-3)', () => {
    expect(evaluate({ ...base, ledger: { ...ledger, proofState: 'published', proofRuns: ['current', 'withdrawn'] } }, DEFAULT_BUDGETS, 'fixture').failures).toEqual([])
    for (const proofState of ['not_served', 'unreadable', 'error']) {
      const f = evaluate({ ...base, ledger: { ...ledger, proofState } }, DEFAULT_BUDGETS, 'fixture').failures.join('\n')
      expect(f).toMatch(/ledger: evaluation record read/)
    }
  })
  it('a score at five passes; the request and byte budgets bind', () => {
    const at5 = { ...ledger, trackState: 'scored', scored: true, resolved: null, floor: null, scoreShown: true, brierText: true }
    expect(evaluate({ ...base, ledger: at5 }, DEFAULT_BUDGETS, 'fixture').failures).toEqual([])
    const tight = { ...DEFAULT_BUDGETS, ledgerRequests: { fixture: 2 }, ledgerBytes: { fixture: 10_000 } }
    const f = evaluate({ ...base, ledger }, tight, 'fixture').failures.join('\n')
    expect(f).toMatch(/ledger requests/)
    expect(f).toMatch(/ledger bytes/)
  })
})

describe('the phone shell (M-2)', () => {
  const file = join(HERE, 'fixtures', 'phone.json')
  it.runIf(existsSync(file))('its overlay is invented: example subjects, the Watch fixture alert ids, no recorded episode', () => {
    const fx = JSON.parse(readFileSync(file, 'utf8'))
    expect(fx.note).toMatch(/INVENTED/)
    for (const k of Object.keys(fx.entries)) expect(k).toMatch(/^\/api\/v1\/v3\/(since\?cursor=[^&]+&include=needs_you|since\/counts\?cursor=[^&]+|me\/seen|system\/alerts\/ex-alert-\d+\/attention)$|^\/api\/v1\/lineage\/situation\/ex_[a-z0-9]+\?direction=upstream&depth=1$/)
    const since = (Object.entries(fx.entries).find(([k]) => k.includes('/since')) as [string, { body: { needs_you: { items: Array<{ id: string; subject_id: string }> }; forecasts_due: { items: Array<{ region: string }> } } }])[1]
    for (const e of since.body.needs_you.items) {
      expect(e.id).toMatch(/^ex-alert-/)
      expect(e.subject_id).toMatch(/^example_/)
    }
    for (const f of since.body.forecasts_due.items) expect(f.region).toMatch(/^example_/)
  })

  it('fails a screen over its requests, a duplicate, a long task, small text or a small control, sideways scroll, a missing bar, and JS over budget', () => {
    const screen = { name: 'Changed (landing)', width: 390, requests: 3, bytes: 30_000, duplicates: 0, dupList: [], longTaskMs: 50, smallTextCount: 0, smallText: [], smallTargetCount: 0, smallTargets: [], overflow: false, barOk: true, engine: [] }
    const js = { initialGz: 140_000, landingGz: 200_000, landingFiles: [{ path: '/m/assets/m-x.js', gz: 140_000 }] }
    const ok = evaluate({ steps: [], phoneShell: { screens: [screen], js } }, DEFAULT_BUDGETS, 'fixture')
    expect(ok.failures.filter((x: string) => /phone/.test(x))).toEqual([])
    const bad = evaluate(
      {
        steps: [],
        phoneShell: {
          screens: [{ ...screen, requests: 40, duplicates: 1, dupList: [{ url: '/x', n: 2 }], longTaskMs: 900, smallTextCount: 2, smallText: ['10px "a"'], smallTargetCount: 1, smallTargets: ['button 20×20'], overflow: true, barOk: false, engine: ['spine'] }],
          js: { initialGz: 400_000, landingGz: 500_000, landingFiles: [{ path: '/m/assets/maplibre-gl-x.js', gz: 283_000 }] },
        },
      },
      DEFAULT_BUDGETS,
      'fixture',
    )
    const f = bad.failures.join('\n')
    for (const needle of ['API requests', 'duplicate requests', 'longest task', 'text under 12 px', 'controls under 44', 'sideways scroll', 'bottom bar', 'engine words', 'initial JS', 'JS before any tap', 'no map library']) expect(f).toContain(needle)
  })
})

describe('the Record address the live fallback opens (roll 87: it wiped the frame params and was never read)', () => {
  it('sets rec= beside the frame params, drops a stale cite, keeps everything else', () => {
    const h = withRecordAddress('#scope=target%3Acountry_x&ws=board&cite=3', 'finding', 'f-1')
    const p = new URLSearchParams(h.slice(1))
    expect(p.get('rec')).toBe('finding:f-1')
    expect(p.get('scope')).toBe('target:country_x')
    expect(p.get('ws')).toBe('board')
    expect(p.has('cite')).toBe(false)
    expect(withRecordAddress('', 'finding', 'f-2')).toBe('#rec=finding%3Af-2')
  })
})

describe('signin: a live walk as a named account (roll 89)', () => {
  it('reads the password from a `password:` line, else the whole file', () => {
    expect(passwordFromFile('email: a@b\npassword: s3cret word  \n')).toBe('s3cret word')
    expect(passwordFromFile('  only-the-password\n')).toBe('only-the-password')
    expect(passwordFromFile('')).toBe('')
  })

  it('keeps only the name=value of the first session cookie', () => {
    expect(sessionCookieFrom(['legba_session=abc.def; Path=/; HttpOnly; SameSite=Strict'])).toBe('legba_session=abc.def')
    expect(sessionCookieFrom(['empty=; Max-Age=0', 'next=1; Path=/'])).toBe('next=1')
    expect(sessionCookieFrom([])).toBe('')
  })

  it('names the role from a sign-in or /auth/me answer, and only a known one', () => {
    expect(roleFrom({ user: { role: 'reader' } })).toBe('reader')
    expect(roleFrom({ role: 'admin' })).toBe('admin')
    expect(roleFrom({ user: { role: 'owner-ish' } })).toBe(null)
    expect(Object.keys(ROLE_RANK)).toEqual(['reader', 'contributor', 'admin'])
  })

  it('skips a check only for an account below the role it needs; never without an account', () => {
    expect(skipsForRole('reader', 'admin')).toBe(true)
    expect(skipsForRole('reader', 'contributor')).toBe(true)
    expect(skipsForRole('contributor', 'contributor')).toBe(false)
    expect(skipsForRole('admin', 'admin')).toBe(false)
    expect(skipsForRole(null, 'admin')).toBe(false)
  })

  it('names, for every role-gated check, a role above the reader and the reads that decide it', () => {
    for (const g of ROLE_GATED) {
      expect(ROLE_RANK[g.needs]).toBeGreaterThan(ROLE_RANK.reader)
      expect(g.reads.length).toBeGreaterThan(0)
    }
    expect(roleGateFor('Flow: one expansion draws')?.needs).toBe('admin')
    expect(roleGateFor('the Gate shows the forecast resolution queue')?.needs).toBe('contributor')
    expect(roleGateFor('the operator side has its four sections')?.needs).toBe('contributor')
    expect(roleGateFor('phone 360: an alert row opens its page; Acknowledge and Snooze for one who may act')?.needs).toBe('contributor')
    // the checks a reader CAN make are never gated (RA-1: the reader's reads)
    expect(roleGateFor('landing')).toBe(null)
    expect(roleGateFor('Watch: with no edits yet, the region says what will appear and why, and reads no list')).toBe(null)
    for (const name of [
      'map: "not covered" only where the roster declares no source',
      'map: a place selected',
      'Watch: the alert log draws',
      'Watch: a filter narrows the log without asking again',
      "Watch: a row's subject becomes the point",
      'Watch: silences say which side of the line each is on',
      'phone 360: Watch draws the alert log',
      "phone 360: a user sees an alert's state and no Acknowledge or Snooze",
      'phone 360: search the subjects and open one',
      'phone 360: "Ask about this" opens Consult with the point above the composer',
      "phone 360: the answer streams in, the pin and the shell's screen ride the request",
    ]) expect(roleGateFor(name)).toBe(null)
    // only change actions and the operator side remain
    expect(ROLE_GATED.length).toBe(6)
  })
})
