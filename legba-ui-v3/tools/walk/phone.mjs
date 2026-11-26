/**
 * The PHONE SHELL walk (M-2; plan §6) — called from walk.mjs's main with its
 * helpers. Fixture data only (`fixtures/api.json` + `watch.json` + the
 * invented `phone.json`).
 *
 *   sizes      360×740, 390×844, 430×932 portrait; 844×390 landscape; and
 *              820×1180 with a coarse pointer, which must land on /m/
 *   steps      1  a phone reaches /m/ WITHOUT requesting the desktop's entry
 *              2  Changed draws; "Needs you" shows what paged; a window chip
 *                 changes the list, and going back to a chip asks nothing
 *              3  a row opens its read; a citation opens the Record's peek;
 *                 "Open the record" pushes the chain; Back walks up step by step
 *              4  Watch: three segments; an OPEN alert's row opens its page with
 *                 Acknowledge and Snooze for one who may act (the shared
 *                 access, sign-in off) and none for a user (nothing is pressed);
 *                 a CLOSED alert's page says there is nothing to act on
 *              5  Ledger: below five resolutions, no score
 *              6  Subjects: search, open a subject
 *              9  Map: the drawn map with no map-library request; "Open the
 *                 globe" requests it
 *              10 a `#rec=` link opened at phone size lands on that record
 *   per screen API requests and bytes (its own phase), duplicates, the longest
 *              task, text under 12 px, controls under 44 × 44 px, horizontal
 *              overflow, the bottom bar on screen, engine words
 *   JS         the gzip size of the JS the landing loads (initial, and before
 *              any tap), and of the map chunk, from the built files
 *
 *              7  (M-3g) Consult: "Ask about this" from a subject page opens
 *                 Consult with the point shown; a mocked streamed answer; the
 *                 contract is one line; a citation opens the Record's peek and
 *                 Back returns to the thread with its scroll kept; with "Let
 *                 answers open screens" on (Settings), a panel action opens the
 *                 Map with an Undo toast, Undo returns, and a view the phone
 *                 lacks is refused in plain words
 *              8  the keyboard (visual viewport cut to 55 %): the composer
 *                 whole above it, the thread >= 40 % with its last line in view
 * walk.mjs's `phoneConsult` (M-0a/M-1) keeps the composer row, the markdown,
 * the citation targets and Stop.
 */
import fs from 'node:fs'
import path from 'node:path'
import zlib from 'node:zlib'
import { loadEngineWords } from './engineWords.mjs'

// the console's one list of engine words (`engineWords.mjs`), the same the desktop walk holds
const { ENGINE_WORDS: ENGINE } = loadEngineWords(new URL('../../src/lib/engineWords.json', import.meta.url))

/** M-3g: the mocked answer (invented filler), its contract and a recorded case.
 *  The cited records are the fixtures' invented ids on fixtures; live
 *  they are a real signal and a real finding read from the server first — the
 *  peek a citation opens asks /lineage/<kind>/<id> of the real API, and an
 *  invented id there is a 404 (two console errors). */
const M3G_REFS = { signal: 'd4e5f6a7-0000-4000-8000-000000000011', finding: 'e5f6a7b8-0000-4000-8000-000000000012' }
/** The fixtures' finding a `#rec=` link opens (live: the resolved finding). */
const REC_LINK_FINDING = 'ex_o0bkwz'
/** The fixtures' alert the reader opens (live: the alert the walk just opened). */
const READER_ALERT = 'ex-alert-001'
function m3gAnswer(refs) {
  const s = refs.signal.slice(0, 8)
  const f = refs.finding.slice(0, 8)
  return {
    answer: `**Short answer.** Filler words stand where the analysis would be, because this is a synthetic walk [${s}].\n\n` +
      `- A first point that wraps across two lines on a phone screen and carries a citation [${f}].\n` +
      '- A second point with no citation.\n\n' +
      'A closing paragraph of plain filler text that a reader scrolls through on a small screen. '.repeat(3),
    finding_id: null, derived_from: [], tool_calls: [],
    cited_refs: [
      { kind: 'signal', id: refs.signal, description: 'Filler record 1' },
      { kind: 'finding', id: refs.finding, description: 'Filler record 2' },
    ],
    uncertainty: 0.3, unanswered_aspects: [], session_id: 'walk-m3g', model: 'core', status: 'complete',
  }
}
const M3G_CONTRACT = {
  schema: 'consult_contract/1', armed: true,
  window: { start: '2026-11-07T08:00:00+00:00', end: '2026-11-14T08:00:00+00:00', hours: 168, source: 'console', basis: 'console 168h', deictic: false },
  coverage: { state: 'measured', days: ['2026-11-08', '2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13', '2026-11-14'].map((day, i) => ({ day, signals: i === 3 ? 0 : 5, sources: i === 3 ? 0 : 2, findings: i === 3 ? 0 : 1, events: 0 })), zero_days: ['2026-11-11'], sparse_days: [], gaps: { paused_sources: [], error_sources: [] } },
  grounding_mode: 'default',
  grounding: { mode: 'default', applied: true, counts: { kept_with_rows: 3, cut_no_row: 0, labelled_inference: 1 } },
  cost_stop: "> **Stopped for cost:** filler — drilling stopped after round 4 because this synthetic turn's cost ceiling leaves no room.",
}
const M3G_CASE = {
  version: 'case/1', request_id: 'walk-m3g-case', status: 'sealed', as_of: '2026-11-14T08:00:00Z', plane: 'core', vocabulary: 'a'.repeat(64),
  refused: 0, stopped_after_seq: null, complete: true, gap_at_seq: null, summary: null,
  actions: [
    { seq: 1, action: 'panel_action', args: { view: 'v4.map', action: 'open', args: {} }, at: null, cites: [], rests_on: [] },
    { seq: 2, action: 'panel_action', args: { view: 'system.source_health', action: 'open', args: {} }, at: null, cites: [], rests_on: [] },
  ],
}

/** In-page: the floors and the frame, for what is on screen now. */
function pageChecks(engineSrc) {
  const vw = innerWidth
  const vh = innerHeight
  const shown = (el) => {
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) return false
    if (el.closest('[inert]') || el.closest('[aria-hidden="true"]')) return false
    const cs = getComputedStyle(el)
    if (cs.visibility === 'hidden' || cs.display === 'none') return false
    return r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw
  }
  const smallText = []
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n.textContent.trim()
    if (!t) continue
    const el = n.parentElement
    if (!el || !shown(el)) continue
    const fs = parseFloat(getComputedStyle(el).fontSize)
    if (fs < 12) smallText.push(`${fs}px "${t.slice(0, 30)}"`)
  }
  const smallTargets = []
  for (const el of document.querySelectorAll('button, a[href], input, select, textarea, [role="button"], [role="tab"]')) {
    if (!shown(el)) continue
    if (el.matches('[data-testid="citation-chip"]')) continue // M-0a: a 44 px reach by its pseudo-element, checked apart
    const cs = getComputedStyle(el)
    // an inline text link inside running text is exempt (plan §6)
    if (el.tagName === 'A' && cs.display === 'inline') continue
    // a button styled as a link inside a line of text (a subject's name in a row) is an inline text link
    if (el.tagName === 'BUTTON' && cs.display === 'inline') continue
    const r = el.getBoundingClientRect()
    // a link in a line of text that keeps the line and reaches 44 px through an invisible ::after
    const after = getComputedStyle(el, '::after')
    // (its reach is measured both ways: a short name, "Oz", was 16 px wide)
    if (after.content !== 'none' && parseFloat(after.height) >= 43.5 && Math.max(r.width, parseFloat(after.width) || 0) >= 43.5) continue
    if (r.width < 43.5 || r.height < 43.5) smallTargets.push(`${el.tagName.toLowerCase()} ${Math.round(r.width)}×${Math.round(r.height)} "${(el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 24)}"`)
  }
  const layer = document.querySelector('.ph-layer[data-on="1"]')
  const overflow = document.documentElement.scrollWidth > vw + 1 || (!!layer && layer.scrollWidth > layer.clientWidth + 1)
  const bar = document.querySelector('[data-testid="phone-bar"]')
  const b = bar ? bar.getBoundingClientRect() : null
  const barOk = !!b && b.bottom <= vh + 1 && b.top >= vh - 140 && b.height >= 48
  const text = (document.querySelector('[data-testid="phone-main"]')?.innerText ?? '') + ' ' + (document.querySelector('[data-testid="record-peek"]')?.innerText ?? '')
  const engine = engineSrc.map(([s, f]) => new RegExp(s, f)).filter((re) => re.test(text)).map((re) => re.source)
  return { smallText: smallText.slice(0, 12), smallTextCount: smallText.length, smallTargets: smallTargets.slice(0, 12), smallTargetCount: smallTargets.length, overflow, barOk, engine, vw }
}

export async function walkPhoneShell(h) {
  const { browser, context, wire, net, step, shot, quiet, stepLongTasks, now, result, BASE, DIST, withTimeout, landingStats, log } = h
  const LIVE = h.MODE === 'live'
  const out = { screens: [], sizes: [], js: null, back: null, globe: null, recLink: null, chips: null, ids: null }
  result.phoneShell = out
  const gzCache = new Map()
  const gz = (urlPath) => {
    if (gzCache.has(urlPath)) return gzCache.get(urlPath)
    const fp = path.join(DIST, urlPath)
    let n = null
    try {
      n = zlib.gzipSync(fs.readFileSync(fp), { level: 9 }).length
    } catch {
      n = null
    }
    gzCache.set(urlPath, n)
    return n
  }
  const engineSrc = ENGINE.map((r) => [r.source, r.flags])
  // the device's screen is the viewport's size (a landscape phone: 844 × 390, its short side 390)
  const phone = (w, hgt, extra = {}) => ({ viewport: { width: w, height: hgt }, screen: { width: w, height: hgt }, isMobile: true, hasTouch: true, deviceScaleFactor: 2, ...extra })

  async function settle(p, ms = 15000) {
    await quiet(p, ms)
    await p.waitForTimeout(400)
  }

  async function screen(p, name, width, phase, since, { shotName, extra } = {}) {
    await settle(p)
    const lt = await stepLongTasks(p, `phone ${width}: ${name}`, since)
    const c = await withTimeout(p.evaluate(pageChecks, engineSrc), 15000, `phone checks ${name}`).catch(() => null)
    // The Changed badge polls /v3/since/counts once a minute on every tab — a
    // background read, not the screen's own, so a poll landing inside a screen's window is not counted
    const L = landingStats(net.log.filter((r) => !r.url.includes('/v3/since/counts?')), phase)
    const row = { name, width, requests: L.requests, bytes: L.bytes, duplicates: L.duplicateRequests, dupList: L.duplicates, longTaskMs: lt?.max ?? null, ...(c ?? { smallTextCount: null, smallTargetCount: null, overflow: null, barOk: null, engine: [] }), ...(extra ?? {}) }
    out.screens.push(row)
    log(`PHONE ${width} ${name}: ${JSON.stringify({ requests: row.requests, bytes: row.bytes, dup: row.duplicates, lt: row.longTaskMs, smallText: row.smallTextCount, smallTargets: row.smallTargetCount, overflow: row.overflow, bar: row.barOk })}${row.smallTargets?.length ? ' targets: ' + row.smallTargets.join('; ') : ''}${row.smallText?.length ? ' text: ' + row.smallText.join('; ') : ''}`)
    if (shotName) await shot(p, `P${width}-${shotName}`)
    return row
  }

  async function tap(p, sel, timeout = 8000) {
    return p.locator(sel).first().click({ timeout }).then(() => true).catch(() => false)
  }

  async function phase(p, name) {
    net.phase = name
    return now(p)
  }

  // Live: the records the mocked Consult answer cites and the
  // `#rec=` link opens are real ones, read from the server through the page
  // (the walk's proxy adds the token) under their own phase, so no screen
  // counts them. Fixtures keep the invented ids their recordings answer.
  const refs = { ...M3G_REFS, recFinding: REC_LINK_FINDING }
  async function resolveLiveIds(p) {
    if (!LIVE) return
    const prev = net.phase
    net.phase = 'ph-live-ids'
    const first = (path) => p.evaluate((u) => fetch(u).then((r) => (r.ok ? r.json() : null)).then((j) => j?.data?.[0]?.id ?? null).catch(() => null), path).catch(() => null)
    const sig = await first('/api/v1/signals?limit=1&fields=summary')
    const fnd = await first('/api/v1/findings?limit=1&fields=summary')
    net.phase = prev
    if (sig) refs.signal = sig
    if (fnd) { refs.finding = fnd; refs.recFinding = fnd }
    out.ids = { signal: !!sig, finding: !!fnd }
    log(`PHONE live ids: signal ${sig ? 'resolved' : 'NOT resolved'}, finding ${fnd ? 'resolved' : 'NOT resolved'}`)
  }

  // ── Consult (M-3g) — everything mocked inside the page: no consult request
  // reaches the registry and no model is called. Steps 7 and 8 of the plan.
  async function walkPhoneConsult(p, w, fromSubject) {
    const turn = { posts: 0, streams: 0, bodies: [] }
    await p.route('**/api/v1/consult', (route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      turn.posts++
      let rid = 'walk'
      try { const b = JSON.parse(route.request().postData() || '{}'); rid = b.request_id || rid; turn.bodies.push(b) } catch {}
      return route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ answer: '', status: 'accepted', request_id: rid, session_id: 'walk-m3g', finding_id: null, derived_from: [], tool_calls: [], cited_refs: [] }) })
    })
    await p.route('**/api/v1/consult/stream/**', (route) => {
      turn.streams++
      const sse = (frames) => frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('')
      if (turn.streams === 1) {
        const answer = m3gAnswer(refs)
        const words = answer.answer.split(/(?<= )/)
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse([
          ...words.map((t) => ({ type: 'step', kind: 'answer_delta', text: t })),
          { type: 'final', final_source: 'registry', status: 'complete', session_id: 'walk-m3g', response: { ...answer, contract: M3G_CONTRACT } },
        ]) })
      }
      // the second question, asked with "Let answers open screens" on: the
      // answer opens the Map (a panel action), and asks for one view the
      // phone does not have (refused, by name, on the Consult screen)
      const kase = { ...M3G_CASE, request_id: turn.bodies[turn.bodies.length - 1]?.request_id ?? M3G_CASE.request_id }
      const text = 'The map is open for you. Filler words for the synthetic walk.'
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse([
        ...kase.actions.map((a) => ({ type: 'step', kind: 'case_action', case_action: a })),
        { type: 'step', kind: 'answer_delta', text },
        { type: 'final', final_source: 'registry', status: 'complete', session_id: 'walk-m3g', response: { answer: text, finding_id: null, derived_from: [], tool_calls: [], cited_refs: [], session_id: 'walk-m3g', case: kase } },
      ]) })
    })
    const consultState = () => p.evaluate(() => ({ tab: document.querySelector('[data-testid="phone-shell"]')?.getAttribute('data-tab'), depth: document.querySelector('[data-testid="phone-shell"]')?.getAttribute('data-depth') })).catch(() => ({}))

    // 7a — "Ask about this" on the subject page: Consult opens with the point shown
    let since = await phase(p, `ph${w}-consult`)
    if (fromSubject) await tap(p, '[data-testid="phone-subject-page"] [data-testid="phone-ask-about-this"]')
    else await tap(p, '[data-testid="tab-consult"]')
    await p.waitForSelector('[data-testid="phone-consult"]', { timeout: 10000 }).catch(() => {})
    const pin = await p.locator('[data-testid="phone-asking-label"]').first().textContent({ timeout: 3000 }).catch(() => null)
    step(`phone ${w}: "Ask about this" opens Consult with the point above the composer`, !!fromSubject && !!pin && (await consultState()).tab === 'consult', pin ? `asking about "${pin.slice(0, 4)}…"` : 'no pin shown')
    const examples = await p.locator('[data-testid="consult-example"]').count().catch(() => 0)
    step(`phone ${w}: the empty Consult says what it does, with examples from the screen`, examples >= 2 && examples <= 3, `${examples} examples`)
    await screen(p, 'Consult', w, `ph${w}-consult`, since, { shotName: '14-consult' })

    // 7b — a mocked streamed answer
    since = await phase(p, `ph${w}-consult-answer`)
    await p.fill('[data-testid="consult-input"]', 'What changed for this subject this week?').catch(() => {})
    await tap(p, '[data-testid="consult-send"]')
    const got = await p.waitForSelector('[data-testid="consult-turn-assistant"] [data-testid="citation-chip"]', { timeout: 15000 }).then(() => true).catch(() => false)
    const sent = turn.bodies[0] ?? {}
    const sends = { pin: Array.isArray(sent.pinned_context) && sent.pinned_context.some((x) => x.kind === 'target'), prefix: /^\[Pinned target: /.test(sent.question || ''), window: sent.window?.hours ?? null, onScreen: sent.on_screen?.chat?.placement ?? null, views: (sent.on_screen?.views ?? []).map((v) => v.id.split(':')[0]), model: sent.model ?? null, writes: 'allow_writes' in sent, kase: 'present_case' in sent }
    step(`phone ${w}: the answer streams in, the pin and the shell's screen ride the request`, got && sends.pin && sends.prefix && sends.onScreen === 'tab' && sends.views.includes('subject.page') && sends.model === 'core' && !sends.writes && !sends.kase, JSON.stringify(sends))
    const contract = (await p.locator('[data-testid="consult-turn-assistant"] [data-testid="phone-contract-toggle"]').first().textContent({ timeout: 3000 }).catch(() => '')) ?? ''
    step(`phone ${w}: the turn contract is one line under the answer`, /Partial answer/.test(contract) && /records on \d+ of \d+ days?/.test(contract), contract.slice(0, 80))
    await screen(p, 'Consult (answer)', w, `ph${w}-consult-answer`, since, { shotName: '15-consult-answer' })

    // 7c — a citation opens the Record's peek; Back returns to the thread, its scroll kept
    since = await phase(p, `ph${w}-consult-peek`)
    const chip = p.locator('[data-testid="consult-turn-assistant"] [data-testid="citation-chip"]').first()
    await chip.scrollIntoViewIfNeeded().catch(() => {})
    const top0 = await p.evaluate(() => document.querySelector('[data-testid="consult-transcript"]')?.scrollTop ?? null).catch(() => null)
    await chip.click({ timeout: 4000 }).catch(() => {})
    const peek = await p.waitForSelector('[data-testid="record-peek"]', { timeout: 10000 }).then(() => true).catch(() => false)
    const hash = new URL(p.url()).hash
    step(`phone ${w}: a citation in the answer opens the Record's peek`, peek && /rec=/.test(hash) && /peek=1/.test(hash) && /screen=consult/.test(hash), hash.replace(/[0-9a-f]{8}-[0-9a-f-]{27}/g, '<id>'))
    if (peek) await screen(p, 'Consult → Record peek', w, `ph${w}-consult-peek`, since, { shotName: '16-consult-record-peek' })
    await tap(p, '[data-testid="record-peek-close"]', 4000)
    await p.waitForSelector('[data-testid="record-peek"]', { state: 'detached', timeout: 5000 }).catch(() => {})
    await p.waitForTimeout(300)
    const top1 = await p.evaluate(() => document.querySelector('[data-testid="consult-transcript"]')?.scrollTop ?? null).catch(() => null)
    const back = await consultState()
    step(`phone ${w}: Back from the peek returns to the thread, its scroll kept`, back.tab === 'consult' && back.depth === '0' && top0 !== null && Math.abs((top1 ?? -999) - top0) <= 1, `scroll ${top0} → ${top1}; ${back.tab} depth ${back.depth}`)

    // 8 — the keyboard (the visual viewport cut to 55 %, as the audit did): the
    // composer stays whole above it, the thread keeps >= 40 % and its last line
    await p.locator('[data-testid="consult-input"]').focus().catch(() => {})
    const fromBottom = () => p.evaluate(() => { const t = document.querySelector('[data-testid="consult-transcript"]'); return t ? Math.round(t.scrollHeight - t.scrollTop - t.clientHeight) : null })
    await p.evaluate(() => { const t = document.querySelector('[data-testid="consult-transcript"]'); if (t) { t.scrollTop = t.scrollHeight; t.dispatchEvent(new Event('scroll')) } }).catch(() => {})
    await p.waitForTimeout(150)
    await p.evaluate(() => {
      const vv = window.visualViewport
      const h = Math.round(innerHeight * 0.55)
      Object.defineProperty(vv, 'height', { configurable: true, get: () => h })
      vv.dispatchEvent(new Event('resize'))
    }).catch(() => {})
    await p.waitForTimeout(400)
    const kb = await p.evaluate(() => {
      const h = window.visualViewport.height
      const c = document.querySelector('[data-testid="consult-composer"]')?.getBoundingClientRect()
      const t = document.querySelector('[data-testid="consult-transcript"]')
      const tr = t?.getBoundingClientRect()
      const bar = document.querySelector('[data-testid="phone-bar"]')
      return {
        kb: document.querySelector('[data-testid="phone-shell"]')?.getAttribute('data-kb'),
        composer: !!c && c.top >= 0 && c.bottom <= h + 1,
        share: tr ? +(tr.height / h).toFixed(2) : null,
        fromBottom: t ? Math.round(t.scrollHeight - t.scrollTop - t.clientHeight) : null,
        bar: bar ? getComputedStyle(bar).display : null,
      }
    }).catch(() => null)
    await shot(p, `P${w}-17-consult-keyboard`)
    step(`phone ${w}: keyboard up — the composer whole above it, the thread >= 40 % with its last line in view`, !!kb && kb.kb === '1' && kb.composer && kb.share >= 0.4 && kb.fromBottom !== null && kb.fromBottom <= 2 && kb.bar === 'none', JSON.stringify(kb))
    await p.evaluate(() => { delete window.visualViewport.height; window.visualViewport.dispatchEvent(new Event('resize')) }).catch(() => {})
    await p.waitForTimeout(400)
    const after = await fromBottom().catch(() => null)
    const kbOff = await p.evaluate(() => document.querySelector('[data-testid="phone-shell"]')?.getAttribute('data-kb')).catch(() => null)
    step(`phone ${w}: keyboard down — the bar returns, the last line still in view`, kbOff === '0' && after !== null && after <= 2, `kb ${kbOff}; ${after} px from the bottom`)
    await p.locator('[data-testid="consult-input"]').blur().catch(() => {})

    // Settings: "Let answers open screens" (the desktop's drive mode, off by default)
    since = await phase(p, `ph${w}-settings`)
    await tap(p, '[data-testid="phone-settings-open"]')
    await p.waitForSelector('[data-testid="phone-settings"]', { timeout: 10000 }).catch(() => {})
    const driveDefault = await p.getAttribute('[data-testid="phone-settings-drive"]', 'aria-checked').catch(() => null)
    await screen(p, 'Settings', w, `ph${w}-settings`, since, { shotName: '13-settings' })
    await tap(p, '[data-testid="phone-settings-drive"]')
    const driveOn = await p.getAttribute('[data-testid="phone-settings-drive"]', 'aria-checked').catch(() => null)
    step(`phone ${w}: Settings has the drive mode, off by default as on the desktop`, driveDefault === 'false' && driveOn === 'true', `default ${driveDefault}; after the tap ${driveOn}`)
    await tap(p, '[data-testid="phone-back"]', 4000)
    await p.waitForSelector('[data-testid="phone-shell"][data-tab="consult"]', { timeout: 5000 }).catch(() => {})

    // 4 — a panel action navigates (the Map), with an Undo toast; the view the phone lacks is refused by name
    since = await phase(p, `ph${w}-consult-drive`)
    await p.fill('[data-testid="consult-input"]', 'Show me where this is on the map.').catch(() => {})
    await tap(p, '[data-testid="consult-send"]')
    const onMap = await p.waitForSelector('[data-testid="phone-map-box"]', { timeout: 15000 }).then(() => true).catch(() => false)
    const toast = await p.waitForSelector('[data-testid="phone-drive-toast"]', { timeout: 8000 }).then(() => true).catch(() => false)
    const asCase = turn.bodies[1]?.present_case === true
    if (onMap) await screen(p, 'Map (opened by Consult)', w, `ph${w}-consult-drive`, since, { shotName: '18-consult-opened-map' })
    await tap(p, '[data-testid="phone-drive-undo"]', 4000)
    await p.waitForSelector('[data-testid="phone-shell"][data-tab="consult"][data-depth="0"]', { timeout: 5000 }).catch(() => {})
    const undone = await consultState()
    const refusal = (await p.locator('[data-testid="consult-drive-refusal"]').first().textContent({ timeout: 3000 }).catch(() => '')) ?? ''
    step(`phone ${w}: an answer's panel action opens the Map; Undo returns to the thread`, asCase && onMap && toast && undone.tab === 'consult' && undone.depth === '0', `${asCase ? 'sent as a case' : 'NOT a case'}; ${onMap ? 'map' : 'no map'}; ${toast ? 'toast' : 'no toast'}; after Undo ${undone.tab} depth ${undone.depth}`)
    step(`phone ${w}: what the phone does not have is refused by name, in plain words`, /desktop only/.test(refusal) && !/system\.|not opened|_/.test(refusal), refusal.slice(0, 80))
    await screen(p, 'Consult (after Undo)', w, `ph${w}-consult-undone`, await now(p), { shotName: '19-consult-after-undo' })
    await p.unroute('**/api/v1/consult').catch(() => {})
    await p.unroute('**/api/v1/consult/stream/**').catch(() => {})
    out.consult = out.consult ?? {}
    out.consult[w] = { sends, kb, contract: contract.slice(0, 80), posts: turn.posts, streams: turn.streams }
  }

  // ── the full walk at 390 and 360 (screenshots at both); the floors at 430 and in landscape ──
  for (const [w, hgt, full] of [[390, 844, true], [360, 740, true], [430, 932, false], [844, 390, false]]) {
    const c = await context(browser, phone(w, hgt))
    const p = await c.newPage(); wire(p, `phone ${w}`)
    const assets = []
    p.on('request', (r) => {
      const u = new URL(r.url())
      if (!u.pathname.startsWith('/api/')) assets.push({ path: u.pathname, phase: net.phase })
    })
    // the newest since-with-needs-you answer this page read (what
    // "Needs you" is checked against — the server's answer, on fixtures and live)
    let sinceSeen = null
    p.on('response', (r) => {
      if (!/\/api\/v1\/v3\/since\?/.test(r.url()) || !/include=needs_you/.test(r.url()) || r.status() !== 200) return
      r.json().then((j) => { sinceSeen = j }).catch(() => {})
    })
    // 1 — the landing, from the desktop's address
    await phase(p, `ph${w}-landing`)
    await p.goto(BASE + '/', { timeout: 30000 }).catch(() => {})
    const drawn = await p.waitForSelector('[data-testid="phone-changed"][data-read="1"]', { timeout: 30000 }).then(() => true).catch(() => false)
    const onM = new URL(p.url()).pathname.startsWith('/m')
    const desktopEntry = assets.filter((a) => /^\/assets\/index-[^/]+\.js$/.test(a.path))
    step(`phone ${w}: lands on /m/ and draws Changed`, onM && drawn, `${new URL(p.url()).pathname}; ${drawn ? 'drawn' : 'not drawn'}`)
    step(`phone ${w}: the desktop's entry is never requested`, desktopEntry.length === 0, desktopEntry.length ? desktopEntry.map((a) => a.path).join(', ') : 'none')
    const landing = await screen(p, 'Changed (landing)', w, `ph${w}-landing`, 0, { shotName: '01-changed' })
    if (w === 390) await resolveLiveIds(p)
    if (w === 390) {
      const js = assets.filter((a) => a.path.startsWith('/m/') && a.path.endsWith('.js')).map((a) => ({ path: a.path, gz: gz(a.path) }))
      const html = fs.readFileSync(path.join(DIST, 'm', 'index.html'), 'utf8')
      const initial = [...html.matchAll(/(?:src|href)="(\/m\/assets\/[^"]+\.js)"/g)].map((m) => m[1])
      out.js = {
        initialGz: initial.reduce((n, f) => n + (gz(f) ?? 0), 0),
        landingGz: js.reduce((n, f) => n + (f.gz ?? 0), 0),
        landingFiles: js,
      }
      out.landing = landing
    }
    if (!full) {
      // the floors on this size: Changed, then Watch
      await tap(p, '[data-testid="tab-watch"]')
      await p.waitForSelector('[data-testid="watch-alert-log"][data-read="1"]', { timeout: 20000 }).catch(() => {})
      await screen(p, 'Watch', w, `ph${w}-x`, 0, { shotName: '02-watch' })
      await c.close()
      continue
    }

    // 2 — needs you; the window chips
    // Checked against the answer the page read, not the fixture's
    // count — every episode the server says paged is listed, and nothing else.
    // On fixtures the answer must also hold one open episode that did NOT page
    // (2 paged + 1 at medium), so "and only that" is exercised there.
    const needs = await p.locator('[data-testid="phone-needs-you-row"]').count().catch(() => 0)
    const items = sinceSeen?.needs_you?.items ?? null
    const pagedN = items ? items.filter((e) => e.why === 'paged' || !!e.last_paged_at).length : null
    const notPagedN = items ? items.length - pagedN : null
    const fixtureShape = LIVE || (pagedN === 2 && notPagedN === 1)
    step(`phone ${w}: "Needs you" lists what paged, and only that`, items !== null && needs === pagedN && fixtureShape, items === null ? `${needs} rows; no since answer with needs_you read` : `${needs} rows; the answer: ${pagedN} paged, ${notPagedN} open that did not page${sinceSeen.needs_you.truncated ? ' (truncated)' : ''}`)
    if (w === 390) {
      const t0 = net.log.length
      await tap(p, '[data-testid="phone-window-7d"]')
      await p.waitForSelector('[data-testid="phone-changed"][data-read="1"]', { timeout: 15000 }).catch(() => {})
      await settle(p, 8000)
      const after7 = net.log.slice(t0).filter((r) => r.url.includes('/v3/since?')).length
      await tap(p, '[data-testid="phone-window-24h"]')
      await settle(p, 8000)
      const back24 = net.log.slice(t0).filter((r) => r.url.includes('/v3/since?')).length - after7
      out.chips = { after7, back24 }
      step('phone 390: a window chip asks for its window once; going back asks nothing', after7 === 1 && back24 === 0, `7 d: ${after7} read(s); back to 24 h: ${back24}`)
    }

    // 3 — a read, the Record's peek, the chain, Back step by step
    let since = await phase(p, `ph${w}-read`)
    const opened = (await tap(p, '[data-testid="changed-row"][data-kind="finding"]')) && (await p.waitForSelector('[data-testid="report-view"]', { timeout: 20000 }).then(() => true).catch(() => false))
    step(`phone ${w}: a Changed row opens its read`, opened, opened ? undefined : 'no read page')
    if (opened) await screen(p, 'Read', w, `ph${w}-read`, since, { shotName: '03-read' })
    since = await phase(p, `ph${w}-peek`)
    let peek = false
    if (opened) {
      // a citation in the read (open a section first when the lead has none)
      if (!(await p.locator('[data-testid="report-view"] [data-testid="citation-chip"]').count().catch(() => 0))) {
        await tap(p, '[data-testid^="section-toggle-"]', 4000)
      }
      await tap(p, '[data-testid="report-view"] [data-testid="citation-chip"]', 5000)
      peek = await p.waitForSelector('[data-testid="record-peek"]', { timeout: 10000 }).then(() => true).catch(() => false)
    }
    if (!peek) {
      // a statement with no citation chip: the Record of a situation row on Changed
      await p.goBack().catch(() => {})
      await tap(p, '[data-testid="changed-row"][data-kind="situation"]')
      peek = await p.waitForSelector('[data-testid="record-peek"]', { timeout: 10000 }).then(() => true).catch(() => false)
    }
    step(`phone ${w}: a statement opens the Record's peek`, peek, peek ? undefined : 'no peek sheet')
    if (peek) {
      await screen(p, 'Record peek', w, `ph${w}-peek`, since, { shotName: '04-record-peek' })
      since = await phase(p, `ph${w}-record`)
      await tap(p, '[data-testid="record-peek-open"]')
      const chain = await p.waitForSelector('[data-testid="record-page"] [data-testid="record-chain"]', { timeout: 15000 }).then(() => true).catch(() => false)
      step(`phone ${w}: "Open the record" pushes the chain`, chain, chain ? undefined : 'no record page')
      if (chain) await screen(p, 'Record page', w, `ph${w}-record`, since, { shotName: '05-record-page' })
      // Back, one step at a time, to the list
      const trail = []
      for (let i = 0; i < 4; i++) {
        const before = await p.evaluate(() => document.querySelector('[data-testid="phone-shell"]')?.getAttribute('data-depth')).catch(() => null)
        if (before === '0') break
        // the peek sheet covers the screen: its own close (×) is Back there, as is the scrim
        const peekOpen = await p.locator('[data-testid="record-peek"]').count().catch(() => 0)
        await tap(p, peekOpen ? '[data-testid="record-peek-close"]' : '[data-testid="phone-back"]', 4000)
        await p.waitForTimeout(400)
        trail.push(await p.evaluate(() => `${document.querySelector('[data-testid="phone-title"]')?.textContent}·${document.querySelector('[data-testid="phone-record-peek-wrap"]') ? 'peek' : ''}`).catch(() => '?'))
      }
      const home = await p.evaluate(() => document.querySelector('[data-testid="phone-shell"]')?.getAttribute('data-depth') === '0' && !!document.querySelector('[data-testid="phone-root-changed"][data-on="1"]')).catch(() => false)
      out.back = out.back ?? { trail }
      step(`phone ${w}: Back walks up step by step to the list`, home && trail.length >= 2, trail.join(' → '))
    }

    // 9 — the map: drawn, no map library; the globe on demand
    since = await phase(p, `ph${w}-map`)
    await tap(p, '[data-testid="phone-open-map"]')
    const svg = await p.waitForSelector('[data-testid="phone-map-box"] svg', { timeout: 20000 }).then(() => true).catch(() => false)
    await settle(p, 10000)
    const mapLib = () => assets.filter((a) => /maplibre/i.test(a.path))
    const before = mapLib().length
    step(`phone ${w}: the map draws without the map library`, svg && before === 0, `${svg ? 'drawn' : 'not drawn'}; ${before} map-library request(s)`)
    await screen(p, 'Map (drawn)', w, `ph${w}-map`, since, { shotName: '06-map' })
    if (w === 390) {
      await tap(p, '[data-testid="phone-globe-open"]')
      const req = await p.waitForRequest((r) => /maplibre/i.test(r.url()), { timeout: 15000 }).then(() => true).catch(() => mapLib().length > 0)
      await p.waitForTimeout(2500)
      const chunk = mapLib()[0]
      out.globe = { requested: req, chunk: chunk ? { path: chunk.path, gz: gz(chunk.path) } : null }
      step('phone 390: "Open the globe" loads the map library', req, chunk ? `${chunk.path} ${((gz(chunk.path) ?? 0) / 1000).toFixed(0)} kB gz` : 'no request')
      await shot(p, `P${w}-06b-globe`)
      await tap(p, '[data-testid="phone-globe-close"]', 3000)
    }
    await tap(p, '[data-testid="phone-back"]', 4000)

    // 4 — Watch, its segments, an alert's page
    since = await phase(p, `ph${w}-watch`)
    await tap(p, '[data-testid="tab-watch"]')
    const log1 = await p.waitForSelector('[data-testid="watch-alert-log"][data-read="1"]', { timeout: 20000 }).then(() => true).catch(() => false)
    step(`phone ${w}: Watch draws the alert log`, log1)
    await screen(p, 'Watch · Alerts', w, `ph${w}-watch`, since, { shotName: '07-watch-alerts' })
    const segs = []
    for (const s of ['silences', 'edits']) {
      await tap(p, `[data-testid="phone-watch-seg-${s}"]`)
      const ok = await p.waitForSelector(`[data-testid="phone-watch-view-${s}"] section, [data-testid="phone-watch-view-${s}"] div`, { timeout: 15000 }).then(() => true).catch(() => false)
      segs.push(`${s} ${ok ? 'drawn' : 'missing'}`)
      await screen(p, `Watch · ${s}`, w, `ph${w}-watch-${s}`, await now(p), { shotName: `07-watch-${s}` })
    }
    step(`phone ${w}: the three Watch segments draw`, segs.every((s) => s.endsWith('drawn')), segs.join(', '))
    await tap(p, '[data-testid="phone-watch-seg-alerts"]')
    await p.waitForSelector('[data-testid="watch-alert-row"]', { timeout: 10000 }).catch(() => {})
    since = await phase(p, `ph${w}-alert`)
    // An OPEN episode: a closed one takes no Acknowledge or Snooze (at 360 px
    // the newest row can have closed a minute before, and the walk then
    // counts 0 controls). The closed one is walked below.
    const openRows = await p.locator('[data-testid="watch-alert-row"][data-open="1"]').count().catch(() => 0)
    await p.locator(openRows ? '[data-testid="watch-alert-row"][data-open="1"]' : '[data-testid="watch-alert-row"]').first().click({ position: { x: 8, y: 8 }, timeout: 5000 }).catch(() => {})
    const alertPage = await p.waitForSelector('[data-testid="phone-alert-page"][data-state="read"]', { timeout: 10000 }).then(() => true).catch(() => false)
    // the alert this page opened (its screen=alert:<id>): the reader opens the same one
    const openedAlert = (() => { try { const m = /(?:^|[#&])screen=alert(?:%3A|:)([^&]+)/.exec(new URL(p.url()).hash); return m ? decodeURIComponent(m[1]) : null } catch { return null } })()
    await p.waitForSelector('[data-testid="phone-alert-attention"]', { timeout: 10000 }).catch(() => {})
    // the fixture's caller is the shared access (sign-in off): it may act — nothing is pressed (a write)
    const ack = await p.locator('[data-testid="phone-alert-ack"], [data-testid="phone-alert-unack"]').count().catch(() => 0)
    const snooze = await p.locator('[data-testid^="phone-alert-snooze-"], [data-testid="phone-alert-unsnooze"]').count().catch(() => 0)
    step(`phone ${w}: an alert row opens its page; Acknowledge and Snooze for one who may act`, openRows > 0 && alertPage && ack === 1 && snooze >= 1, `${openRows ? '' : 'no open alert in the log; '}${alertPage ? 'page' : 'no page'}; ${ack} acknowledge, ${snooze} snooze controls`)
    if (alertPage) await screen(p, 'Alert page', w, `ph${w}-alert`, since, { shotName: '08-alert' })
    await tap(p, '[data-testid="phone-back"]', 4000)
    // A CLOSED episode: its page says there is nothing to act on, with no
    // control — and its controls (the subject's name, short or long) hold the
    // 44 px floor both ways (e.g. "Iran" 28 × 44 at 360 px).
    const closedRow = p.locator('[data-testid="watch-alert-row"][data-open="0"]').first()
    if (await closedRow.count().catch(() => 0)) {
      since = await phase(p, `ph${w}-alert-closed`)
      await closedRow.click({ position: { x: 8, y: 8 }, timeout: 5000 }).catch(() => {})
      const closedPage = await p.waitForSelector('[data-testid="phone-alert-page"][data-state="read"]', { timeout: 10000 }).then(() => true).catch(() => false)
      const said = await p.waitForSelector('[data-testid="phone-alert-closed"]', { timeout: 10000 }).then(() => true).catch(() => false)
      const controls = await p.locator('[data-testid="phone-alert-ack"], [data-testid="phone-alert-unack"], [data-testid^="phone-alert-snooze-"], [data-testid="phone-alert-unsnooze"]').count().catch(() => 0)
      step(`phone ${w}: a closed alert's page says there is nothing to act on, and has no Acknowledge or Snooze`, closedPage && said && controls === 0, `${closedPage ? 'page' : 'no page'}; ${said ? 'said' : 'not said'}; ${controls} controls`)
      if (closedPage) await screen(p, 'Alert page (closed)', w, `ph${w}-alert-closed`, since, { shotName: '08c-alert-closed' })
      await tap(p, '[data-testid="phone-back"]', 4000)
    } else {
      log(`PHONE ${w} no closed alert in the log: the closed page is not walked`)
    }
    if (w === 390) {
      // a user (the reader class) sees the state, never a button
      const c2 = await context(browser, phone(w, hgt))
      const p2 = await c2.newPage(); wire(p2, `phone ${w} reader`)
      net.override.set('/api/v1/auth/me', { status: 200, body: { kind: 'user', actor: 'user:ex_reader', role: 'reader', user: { id: 'ex_reader', actor: 'user:ex_reader', email: null, display_name: 'Example reader', role: 'reader', status: 'active', auth_provider: 'password', last_sign_in_at: null }, session_rejected: false, signin: true } })
      net.phase = `ph${w}-alert-reader`
      // live: the alert just opened — the fixtures' invented id is not on the server
      const readerAlert = LIVE ? openedAlert : READER_ALERT
      await p2.goto(`${BASE}/m/#ws=watch&screen=alert%3A${encodeURIComponent(readerAlert ?? READER_ALERT)}`, { timeout: 30000 }).catch(() => {})
      const seen = await p2.waitForSelector('[data-testid="phone-alert-attention"]', { timeout: 20000 }).then(() => true).catch(() => false)
      const buttons = await p2.locator('[data-testid="phone-alert-ack"], [data-testid^="phone-alert-snooze-"]').count().catch(() => 0)
      const said = await p2.locator('[data-testid="phone-alert-reader"]').count().catch(() => 0)
      net.override.delete('/api/v1/auth/me')
      step(`phone ${w}: a user sees an alert's state and no Acknowledge or Snooze`, !!readerAlert && seen && buttons === 0 && said === 1, `${readerAlert ? '' : 'no alert id to open; '}${seen ? 'state shown' : 'no state'}; ${buttons} controls; ${said} reader line`)
      await shot(p2, `P${w}-08b-alert-as-user`)
      await c2.close()
    }

    // 5 — Ledger
    since = await phase(p, `ph${w}-ledger`)
    await tap(p, '[data-testid="tab-ledger"]')
    const ledgerOk = await p.waitForFunction(() => {
      const t = document.querySelector('[data-testid="phone-ledger"] [data-testid="track-record"]')
      return !!document.querySelector('[data-testid="phone-ledger"] [data-testid="ledger-forecasts"]') && t && t.getAttribute('data-state') !== 'reading'
    }, null, { timeout: 20000 }).then(() => true).catch(() => false)
    const score = await p.evaluate(() => {
      const t = document.querySelector('[data-testid="phone-ledger"] [data-testid="track-record"]')
      return { state: t?.getAttribute('data-state') ?? null, score: !!document.querySelector('[data-testid="phone-ledger"] [data-testid="track-record-score"]') }
    }).catch(() => ({ state: null, score: null }))
    step(`phone ${w}: Ledger — below five resolutions, no score`, ledgerOk && !(score.state === 'withheld' && score.score), `track record ${score.state}; score ${score.score ? 'SHOWN' : 'not shown'}`)
    await screen(p, 'Ledger', w, `ph${w}-ledger`, since, { shotName: '09-ledger' })
    since = await phase(p, `ph${w}-proof`)
    await tap(p, '[data-testid="phone-ledger-proof"]')
    await p.waitForSelector('[data-testid="phone-proof"] [data-testid="proof-record"]', { timeout: 15000 }).catch(() => {})
    await screen(p, 'Evaluations', w, `ph${w}-proof`, since, { shotName: '10-proof' })
    await tap(p, '[data-testid="phone-back"]', 4000)

    // 6 — Subjects: search, open one
    since = await phase(p, `ph${w}-subjects`)
    await tap(p, '[data-testid="tab-subjects"]')
    await p.waitForSelector('[data-testid="phone-subject-row"]', { timeout: 20000 }).catch(() => {})
    await screen(p, 'Subjects', w, `ph${w}-subjects`, since, { shotName: '11-subjects' })
    const firstName = await p.locator('[data-testid="phone-subject-list"] [data-testid="phone-subject-row"] .ph-row-head').first().textContent().catch(() => '')
    await p.fill('[data-testid="phone-subject-search"]', (firstName ?? '').slice(0, 4)).catch(() => {})
    await p.waitForTimeout(300)
    since = await phase(p, `ph${w}-subject`)
    await tap(p, '[data-testid="phone-subject-list"] [data-testid="phone-subject-row"]')
    const subj = await p.waitForSelector('[data-testid="phone-subject-page"]', { timeout: 15000 }).then(() => true).catch(() => false)
    step(`phone ${w}: search the subjects and open one`, subj && !!firstName, `${firstName ? `"${firstName.slice(0, 4)}…"` : 'no roster'}; ${subj ? 'page' : 'no page'}`)
    if (subj) await screen(p, 'Subject page', w, `ph${w}-subject`, since, { shotName: '12-subject' })

    // 7–8 — Consult (M-3g): asked from the subject page, a mocked streamed
    // answer, a citation to the Record's peek and Back, the keyboard, and a
    // panel action that navigates with Undo (all mocked in the page)
    await walkPhoneConsult(p, w, subj)
    net.phase = `ph${w}-end`
    await c.close()
  }

  // 820×1180, coarse pointer: a small tablet gets the phone shell
  {
    const c = await context(browser, { viewport: { width: 820, height: 1180 }, screen: { width: 820, height: 1180 }, isMobile: true, hasTouch: true })
    const p = await c.newPage(); wire(p, 'tablet 820')
    net.phase = 'ph820-landing'
    await p.goto(BASE + '/', { timeout: 30000 }).catch(() => {})
    await p.waitForSelector('[data-testid="phone-shell"]', { timeout: 20000 }).catch(() => {})
    const onM = new URL(p.url()).pathname.startsWith('/m')
    step('a touch screen 820 px wide lands on /m/', onM, new URL(p.url()).pathname)
    await settle(p, 10000)
    await shot(p, 'P820-01-changed')
    await c.close()
  }

  // 10 — a #rec= link at phone size lands on that record
  {
    const c = await context(browser, phone(390, 844))
    const p = await c.newPage(); wire(p, 'phone rec link')
    net.phase = 'ph-rec-link'
    const id = refs.recFinding
    await p.goto(`${BASE}/#rec=finding:${id}`, { timeout: 30000 }).catch(() => {})
    const ok = await p.waitForSelector(`[data-testid="record-chain"][data-id="${id}"]`, { timeout: 20000 }).then(() => true).catch(() => false)
    const url = new URL(p.url())
    out.recLink = { path: url.pathname, hash: url.hash, ok }
    step('a #rec= link opened at phone size lands on that record', ok && url.pathname.startsWith('/m') && url.hash.includes(`rec=finding`), `${url.pathname}${url.hash}`)
    await settle(p, 8000)
    await shot(p, 'P390-15-rec-link')
    // and Back leaves the record for the landing, not the browser's previous page
    await p.locator('[data-testid="phone-back"]').first().click({ timeout: 4000 }).catch(() => {})
    await p.waitForTimeout(400)
    const home = await p.evaluate(() => document.querySelector('[data-testid="phone-shell"]')?.getAttribute('data-depth')).catch(() => null)
    step('Back from a cold record link returns to the landing', home === '0', `depth ${home}`)
    await c.close()
  }
  net.phase = 'end'
}
