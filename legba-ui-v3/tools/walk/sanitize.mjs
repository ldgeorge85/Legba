/**
 * sanitize — turns recorded live GET responses into a NEUTRAL fixture set.
 *
 * The walk's fixture mode replays API responses in their live SHAPES, but the
 * owner's content (findings, sources, names, prose, links) must never be
 * committed. So every string in a recorded body is rewritten, keeping only
 * what the UI itself needs to behave the same:
 *
 *   - a string the UI source names as a literal (an enum it compares against,
 *     a route segment, a field value it switches on) is KEPT — it is already
 *     public in the source;
 *   - timestamps, dates, numbers-as-strings and a short list of generic state
 *     words are KEPT;
 *   - an identifier-shaped token (no spaces) is MAPPED to a synthetic token
 *     (`ex_3k9f1q`), consistently across every response and every request path
 *     in one recording, so the UI's joins (a list row's id → its detail route)
 *     still resolve; a `prefix:rest` token keeps a whitelisted prefix;
 *   - prose is rewritten WORD BY WORD into neutral filler words, keeping
 *     numbers, punctuation and markdown (headings, lists, `[1]` markers), so
 *     the layout sees text of the same length and structure;
 *   - links become `https://example.org/r/<token>`.
 *
 * The mapping is keyed by a random secret generated per recording and never
 * written anywhere, so a fixture cannot be reversed by hashing guesses.
 * Numbers are kept (counts are not content).
 */
import { createHmac, randomBytes } from 'node:crypto'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

/** Neutral filler vocabulary — every rewritten prose word is one of these. */
export const FILLER = [
  'alpha', 'amber', 'anchor', 'arbor', 'basin', 'beacon', 'birch', 'bramble', 'canopy', 'cedar',
  'cinder', 'clover', 'copper', 'coral', 'cove', 'delta', 'dune', 'ember', 'fable', 'fern',
  'fjord', 'flint', 'garnet', 'glade', 'granite', 'harbor', 'hazel', 'heron', 'indigo', 'island',
  'juniper', 'kestrel', 'lagoon', 'lantern', 'larch', 'linen', 'maple', 'marble', 'meadow', 'mesa',
  'mineral', 'mosaic', 'nectar', 'nimbus', 'oak', 'ochre', 'orchard', 'otter', 'pebble', 'pine',
  'plume', 'prairie', 'quartz', 'quill', 'raven', 'reed', 'ridge', 'river', 'saffron', 'sage',
  'sable', 'shale', 'sierra', 'slate', 'sorrel', 'spruce', 'summit', 'tamarack', 'thistle', 'timber',
  'topaz', 'tundra', 'umber', 'valley', 'velvet', 'violet', 'walnut', 'willow', 'winter', 'yarrow',
  'zephyr', 'example', 'sample', 'placeholder', 'neutral', 'record', 'item', 'entry', 'note', 'text',
]

/** Generic state words kept as they are (not content; the UI may display them). */
export const GENERIC_WORDS = new Set([
  'active', 'inactive', 'paused', 'retired', 'pending', 'running', 'done', 'failed', 'ok', 'error',
  'low', 'medium', 'moderate', 'high', 'critical', 'elevated', 'severe', 'minor', 'major', 'info',
  'warning', 'steady', 'escalating', 'deescalating', 'stable', 'rising', 'falling', 'new', 'open',
  'closed', 'resolved', 'unresolved', 'sealed', 'verified', 'unverified', 'true', 'false', 'none',
  'null', 'unknown', 'yes', 'no', 'up', 'down', 'green', 'amber', 'red', 'healthy', 'degraded',
  'silent', 'firing', 'quiet', 'stale', 'fresh', 'draft', 'published', 'established', 'revised',
  'retracted', 'supported', 'contradicted', 'mixed', 'pass', 'fail', 'skip', 'all', 'any',
])

const ISO_TS = /^\d{4}-\d{2}-\d{2}([T ][0-9:.]+(Z|[+-]\d{2}:?\d{2})?)?$/
const NUMERIC = /^[-+]?\d+(\.\d+)?(e[-+]?\d+)?%?$/i
const URL_RE = /^(https?|wss?):\/\//i
const WORD_RE = /[A-Za-z\u00C0-\u024F][A-Za-z\u00C0-\u024F']*/g

/**
 * The whitelist: every string literal in the UI source, plus each `/`, `:`,
 * `.` and space-separated piece of one. Built at record time from `src/`.
 */
export function literalsFromSource(srcDir) {
  const out = new Set()
  const walk = (d) => {
    for (const name of readdirSync(d)) {
      const p = join(d, name)
      const st = statSync(p)
      if (st.isDirectory()) walk(p)
      else if (/\.(ts|tsx|js|mjs)$/.test(name) && !/\.test\./.test(name)) addLiterals(readFileSync(p, 'utf8'), out)
    }
  }
  walk(srcDir)
  return out
}

/** Every identifier in the UI source (property names the UI reads: `row.target_id`). */
export function identifiersFromSource(srcDir) {
  const out = new Set()
  const walk = (d) => {
    for (const name of readdirSync(d)) {
      const p = join(d, name)
      const st = statSync(p)
      if (st.isDirectory()) walk(p)
      else if (/\.(ts|tsx|js|mjs)$/.test(name) && !/\.test\./.test(name)) {
        for (const m of readFileSync(p, 'utf8').matchAll(/[A-Za-z_][A-Za-z0-9_]*/g)) out.add(m[0])
      }
    }
  }
  walk(srcDir)
  return out
}

export function addLiterals(code, out = new Set()) {
  const add = (str) => {
    out.add(str)
    for (const part of str.split(/[/:.?=&\s,]+/)) if (part) out.add(part)
  }
  const re = /(['"])((?:(?!\1)[^\\\n]){1,80})\1/g
  let m
  while ((m = re.exec(code))) add(m[2])
  // template literals: the static pieces between `${…}` (route paths live here)
  const tre = /`([^`]{1,300})`/g
  while ((m = tre.exec(code))) {
    for (const piece of m[1].split(/\$\{[^}]*\}/)) if (piece.trim()) add(piece)
  }
  return out
}

export function makeSanitizer({ whitelist = new Set(), keyNames = new Set(), secret = randomBytes(32), arrayCap = 0, stringCap = 0 } = {}) {
  const h = (s, n = 6) => {
    const d = createHmac('sha256', secret).update(s).digest()
    return BigInt('0x' + d.subarray(0, 8).toString('hex')).toString(36).slice(0, n).padStart(n, '0')
  }
  const filler = (word) => {
    const base = FILLER[createHmac('sha256', secret).update('w:' + word.toLowerCase()).digest()[0] % FILLER.length]
    if (word.length > 1 && word === word.toUpperCase()) return base.toUpperCase()
    if (word[0] === word[0].toUpperCase() && word[0] !== word[0].toLowerCase()) return base[0].toUpperCase() + base.slice(1)
    return base
  }
  const keep = (s) => whitelist.has(s) || GENERIC_WORDS.has(s.toLowerCase()) || ISO_TS.test(s) || NUMERIC.test(s)

  /** One whitespace-free token (an id, a slug, a code, a path segment). */
  const token = (s) => {
    if (s === '' || keep(s)) return s
    if (URL_RE.test(s)) return 'https://example.org/r/' + h(s, 8)
    if (s.includes('/')) return s.split('/').map(token).join('/')
    if (s.includes(':')) return s.split(':').map(token).join(':')
    if (/^[A-Za-z\u00C0-\u024F']+$/.test(s) && !/[_\d]/.test(s)) return filler(s)
    const first = s.split(/[_-]/)[0]
    if (first && first !== s && /^[a-z]+$/.test(first) && keep(first)) return first + '_' + h(s)
    return 'ex_' + h(s)
  }

  /** Prose: each word becomes a filler word; numbers, punctuation and markdown stay. */
  const prose = (s) => {
    if (keep(s)) return s
    return s.replace(/\S+/g, (chunk) => {
      // inside prose only numbers, dates, generic state words and identifier-
      // shaped UI literals survive; every plain word becomes filler
      if (ISO_TS.test(chunk) || NUMERIC.test(chunk) || GENERIC_WORDS.has(chunk.toLowerCase())) return chunk
      if (/[_:]/.test(chunk) && whitelist.has(chunk)) return chunk
      if (URL_RE.test(chunk)) return 'https://example.org/r/' + h(chunk, 8)
      // an id inside prose (snake_case or with digits) maps like a token, so a
      // reference in text agrees with the same id in a field
      if (/^[A-Za-z0-9]+([_:][A-Za-z0-9]+)+$/.test(chunk)) return token(chunk)
      return chunk.replace(WORD_RE, (w) => (w.length <= 2 ? w : filler(w)))
    })
  }

  const string = (s) => (/\s/.test(s.trim()) ? prose(s) : token(s))

  // a field name is one the UI source names (as a literal or an identifier);
  // an object none of whose keys are field names is a map keyed by ids
  const knownKey = (k) => keep(k) || keyNames.has(k)
  const looksLikeIdMap = (obj) => {
    const keys = Object.keys(obj)
    return keys.length >= 3 && keys.every((k) => !knownKey(k))
  }
  const key = (k, idMap) => {
    if (knownKey(k)) return k
    if (idMap || /[A-Z\d:\s]/.test(k)) return string(k)
    return k
  }

  // `cap` bounds the outermost arrays (0 = no bound); arrays nested inside
  // them keep at most 4, and deeper ones 2: a fixture needs the SHAPE, not
  // the whole corpus. Pagination stops at the first page (`next_cursor` null).
  const value = (v, cap = arrayCap, level = 0) => {
    if (typeof v === 'string') {
      const out = string(v)
      if (stringCap <= 0 || out.length <= stringCap) return out
      // cut at a word boundary, so no filler word is left half-written
      const cut = out.lastIndexOf(' ', stringCap)
      return out.slice(0, cut > 0 ? cut : stringCap)
    }
    if (Array.isArray(v)) {
      const n = cap > 0 ? (level === 0 ? cap : level === 1 ? Math.min(cap, 4) : Math.min(cap, 2)) : 0
      return (n > 0 ? v.slice(0, n) : v).map((x) => value(x, cap, level + 1))
    }
    if (v && typeof v === 'object') {
      const idMap = looksLikeIdMap(v)
      const o = {}
      for (const [k, x] of Object.entries(v)) o[key(k, idMap)] = k === 'next_cursor' ? null : value(x, cap, level)
      return o
    }
    return v
  }

  /** A request key: `/api/v1/a/b?x=1&y=2` → the same with each segment and value mapped. */
  const requestKey = (pathAndQuery) => {
    const [p, q = ''] = pathAndQuery.split('?')
    const path = p.split('/').map((seg) => (seg ? token(decodeURIComponent(seg)) : seg)).join('/')
    if (!q) return path
    const params = new URLSearchParams(q)
    const outQ = []
    for (const [k, v] of params) outQ.push(`${k}=${v.split(',').map((x) => token(x)).join(',')}`)
    return `${path}?${outQ.join('&')}`
  }

  return { value, string, token, prose, requestKey }
}

/**
 * The leak check the fixture test runs over the committed file. Every string
 * value must be one of: a whole UI literal; a timestamp or number; an
 * example.org link; a token made of kept pieces, filler words and mapped
 * pieces (`ex_3k9f1q`, `prefix_3k9f1q`); or prose whose every 3+ letter word
 * is filler or a generic state word. Returns what is left over (empty = clean).
 */
export function unexpectedWords(fixture, whitelist) {
  const allowed = new Set([...FILLER, ...GENERIC_WORDS].map((w) => w.toLowerCase()))
  const mapped = /^([a-z][a-z0-9]*_)?[0-9a-z]{6}$/
  const okPiece = (p) =>
    p === '' || whitelist.has(p) || allowed.has(p.toLowerCase()) || ISO_TS.test(p) || NUMERIC.test(p) || mapped.test(p) || p.startsWith('ex_')
  const okToken = (t) => t.split(/[/:]/).every(okPiece)
  const bad = new Set()
  const visit = (v) => {
    if (typeof v === 'string') {
      if (whitelist.has(v) || ISO_TS.test(v) || /^https:\/\/example\.org\//.test(v)) return
      if (!/\s/.test(v.trim())) {
        if (!okToken(v)) bad.add(v)
        return
      }
      for (const chunk of v.split(/\s+/)) {
        if (!chunk || ISO_TS.test(chunk) || whitelist.has(chunk) || /^https:\/\/example\.org\//.test(chunk)) continue
        if (/^[A-Za-z0-9]+([_:][A-Za-z0-9]+)+$/.test(chunk)) {
          if (!okToken(chunk)) bad.add(chunk)
          continue
        }
        for (const w of chunk.match(WORD_RE) ?? []) {
          if (w.length > 2 && !allowed.has(w.toLowerCase())) bad.add(w)
        }
      }
    } else if (Array.isArray(v)) v.forEach(visit)
    else if (v && typeof v === 'object') Object.values(v).forEach(visit)
  }
  visit(fixture)
  return [...bad]
}
