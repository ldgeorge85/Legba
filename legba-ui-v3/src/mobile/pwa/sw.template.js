/*
 * The phone app's service worker (P-1) — served at /m/sw.js, scope /m/ ONLY.
 *
 * GENERATED: `vite.config.mobile.ts` fills the two placeholders below from
 * the built bundle (`src/mobile/pwa/swBuild.ts`) and emits this file as
 * `dist/m/sw.js`. Hand-written, no imports, no importScripts: everything it
 * does is on this page.
 *
 * THE RULES (each one is a test in `src/mobile/pwa/sw.test.ts`):
 *   1. It stores the phone app's SHELL and nothing else: the HTML page, the
 *      hashed JS/CSS the entry loads, the icons and the manifest — the list
 *      the build wrote below, fetched once at install. Nothing is ever added
 *      to the cache at run time.
 *   2. It NEVER stores an API answer, evidence, record text, a Consult answer
 *      or anything under /api/ (nor /ws/, /tiles/). The Cache API ignores
 *      Cache-Control, so the refusal is here, in code: those requests are
 *      never answered by this worker at all (the browser fetches them as if
 *      there were no worker), and `assertCacheable` throws before any write
 *      outside the shell list.
 *   3. It answers only inside its own scope (/m/). The desktop at / is never
 *      controlled — the scope is the script's directory — and the routing
 *      refuses anything outside it as well.
 *   4. A navigation is left to the browser while the device is online (so a
 *      perimeter password prompt still appears: a worker that fetches a
 *      navigation itself can receive a 401 with no prompt, whatwg/fetch#1132).
 *      Offline (`navigator.onLine` false) it answers with the cached shell
 *      page, which then says plainly that it is offline. The page is the
 *      shell only — it carries no data.
 *   5. A new version WAITS. It takes over only when the page asks
 *      (`LEGBA_SW_SKIP_WAITING`, sent when the person taps "Reload" on the
 *      "Update available" banner). Old versions' caches go on activate.
 *   6. On sign-out the page sends `LEGBA_SW_CLEAR`: every cache this worker
 *      made is deleted.
 */
'use strict'

var VERSION = '__LEGBA_SW_VERSION__'
var PRECACHE = __LEGBA_SW_PRECACHE__

var PREFIX = 'legba-m-shell-'
var CACHE = PREFIX + VERSION
var SCOPE = new URL('./', self.location.href).pathname
var SHELL_HTML = SCOPE + 'index.html'
var PRECACHE_SET = new Set(PRECACHE)
var NEVER = [/^\/api(\/|$)/, /^\/ws(\/|$)/, /^\/tiles(\/|$)/]

function isNeverCached(pathname) {
  return NEVER.some(function (re) { return re.test(pathname) })
}

/** 'shell' | 'navigate' | 'pass' — what this worker does with a request. */
function routeFor(url, method, mode) {
  var u
  try { u = new URL(url, self.location.href) } catch (e) { return 'pass' }
  if (u.origin !== self.location.origin) return 'pass'
  if (isNeverCached(u.pathname)) return 'pass'
  if (u.pathname.indexOf(SCOPE) !== 0) return 'pass'
  if (method !== 'GET') return 'pass'
  if (mode === 'navigate') return 'navigate'
  if (u.search === '' && PRECACHE_SET.has(u.pathname)) return 'shell'
  return 'pass'
}

/** Throws unless `path` is one of the shell files this build listed. */
function assertCacheable(path) {
  var u = new URL(path, self.location.href)
  if (u.origin !== self.location.origin || isNeverCached(u.pathname) || u.search !== '' || !PRECACHE_SET.has(u.pathname)) {
    throw new Error('legba sw: refusing to cache ' + u.pathname + u.search)
  }
}

function looksLikeHtml(res) {
  return /text\/html/i.test(res.headers.get('content-type') || '')
}

async function precache() {
  var cache = await caches.open(CACHE)
  for (var i = 0; i < PRECACHE.length; i++) {
    var path = PRECACHE[i]
    assertCacheable(path)
    var res = await fetch(new Request(path, { cache: 'reload', credentials: 'same-origin', redirect: 'error' }))
    // A sign-in screen, a 401, or the SPA fallback page standing in for a
    // missing script is not the shell: install fails and the old version stays.
    if (!res.ok || res.status !== 200) throw new Error('legba sw: ' + path + ' answered ' + res.status)
    if (path !== SHELL_HTML && /\.(js|css|png|svg|webmanifest)$/.test(path) && looksLikeHtml(res)) {
      throw new Error('legba sw: ' + path + ' answered an HTML page')
    }
    await cache.put(path, res)
  }
}

async function dropOldCaches() {
  var keys = await caches.keys()
  await Promise.all(keys.filter(function (k) { return k.indexOf(PREFIX) === 0 && k !== CACHE }).map(function (k) { return caches.delete(k) }))
}

async function dropAllCaches() {
  var keys = await caches.keys()
  await Promise.all(keys.filter(function (k) { return k.indexOf(PREFIX) === 0 }).map(function (k) { return caches.delete(k) }))
}

async function fromShell(request) {
  var cache = await caches.open(CACHE)
  var hit = await cache.match(new URL(request.url).pathname)
  return hit || fetch(request)
}

async function shellPage() {
  var cache = await caches.open(CACHE)
  return cache.match(SHELL_HTML)
}

self.addEventListener('install', function (event) {
  // No skipWaiting here: a new version waits for the person (rule 5).
  event.waitUntil(precache())
})

self.addEventListener('activate', function (event) {
  event.waitUntil(dropOldCaches().then(function () { return self.clients.claim() }))
})

self.addEventListener('fetch', function (event) {
  var req = event.request
  var route = routeFor(req.url, req.method, req.mode)
  if (route === 'shell') {
    event.respondWith(fromShell(req))
  } else if (route === 'navigate' && self.navigator && self.navigator.onLine === false) {
    // Offline: the shell page (rule 4). Online: no respondWith — the browser's own navigation.
    event.respondWith(shellPage().then(function (r) { return r || fetch(req) }))
  }
  // 'pass': no respondWith — the browser fetches it; this worker stores nothing.
})

self.addEventListener('message', function (event) {
  var type = event.data && event.data.type
  if (type === 'LEGBA_SW_SKIP_WAITING') {
    self.skipWaiting()
  } else if (type === 'LEGBA_SW_CLEAR') {
    event.waitUntil(dropAllCaches().then(function () {
      if (event.source && event.source.postMessage) event.source.postMessage({ type: 'LEGBA_SW_CLEARED' })
    }))
  }
})

// The test seam: the rules as the worker runs them.
self.__legbaSw = { VERSION: VERSION, CACHE: CACHE, PRECACHE: PRECACHE, routeFor: routeFor, assertCacheable: assertCacheable }
