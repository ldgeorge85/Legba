#!/usr/bin/env node
/**
 * make-phone-icons — the installed phone app's icons, drawn from ONE SVG (P-1).
 *
 *   node tools/make-phone-icons.mjs           # (re)write public-m/icons/*.png
 *   node tools/make-phone-icons.mjs --check   # exit 1 if a committed PNG differs
 *
 * The source is `public-m/favicon.svg`: the console's own mark (M-0a) — the
 * dark ground, the crossroads in the line grey, the magenta point. Nothing
 * else is drawn; there is no third-party or brand imagery anywhere.
 *
 * Two variants come out of the one drawing:
 *   - `any`  — the SVG as it is (its rounded ground, transparent corners):
 *              what Chrome shows in the install sheet and the app drawer when
 *              it does not mask.
 *   - `full` — the ground filling the whole square, the mark scaled to 80%
 *              about the centre so it sits inside the maskable safe zone (a
 *              circle of 40% of the size): Android's adaptive mask and iOS's
 *              rounded corners crop the ground, never the mark. iOS wants an
 *              opaque square for `apple-touch-icon`, so it uses this variant.
 *
 * REPRODUCIBLE and dependency-free (runs in the bare node build image): the
 * rasteriser below understands exactly the three elements the mark uses
 * (`rect` with `rx`, a stroked `path` of straight segments with round caps,
 * `circle`) and refuses anything else, so a change to the SVG that it cannot
 * draw fails loud instead of drawing something else. Every pixel is 8×8
 * supersampled with plain float maths; `src/mobile/pwa/icons.test.ts` redraws
 * each icon and compares PIXELS with the committed PNG (pixels, not bytes, so
 * a different zlib build cannot fail it).
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { deflateSync, inflateSync } from 'node:zlib'

const UI = join(dirname(fileURLToPath(import.meta.url)), '..')
export const SOURCE = 'public-m/favicon.svg'
export const OUT_DIR = 'public-m/icons'

/** Every icon the manifest and m.html name: file → [size, variant]. */
export const ICONS = {
  'icon-192.png': [192, 'any'],
  'icon-512.png': [512, 'any'],
  'maskable-192.png': [192, 'full'],
  'maskable-512.png': [512, 'full'],
  'apple-touch-icon.png': [180, 'full'],
}
const FULL_MARK_SCALE = 0.8
const SS = 8 // supersamples per axis

// ------------------------------------------------------------------ the SVG
const attrs = (tag) => Object.fromEntries([...tag.matchAll(/([a-z-]+)="([^"]*)"/g)].map((m) => [m[1], m[2]]))
const hex = (c) => {
  const m = /^#([0-9a-f]{6})$/i.exec(c || '')
  if (!m) throw new Error(`make-phone-icons: colour ${c} is not #rrggbb`)
  return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16))
}

/** The drawing as shapes, in paint order, in the SVG's own units. */
export function parseMark(svg) {
  const vb = /viewBox="0 0 ([\d.]+) ([\d.]+)"/.exec(svg)
  if (!vb || vb[1] !== vb[2]) throw new Error('make-phone-icons: the source needs a square viewBox "0 0 N N"')
  const size = Number(vb[1])
  const shapes = []
  const body = svg.replace(/^<svg[^>]*>/, '').replace(/<\/svg>\s*$/, '')
  for (const m of body.matchAll(/<([a-z]+)\b([^>]*)\/>/g)) {
    const [, name, raw] = m
    const a = attrs(raw)
    if (name === 'rect') {
      shapes.push({ kind: 'rect', x: +(a.x || 0), y: +(a.y || 0), w: +a.width, h: +a.height, rx: +(a.rx || 0), color: hex(a.fill) })
    } else if (name === 'circle') {
      shapes.push({ kind: 'circle', cx: +a.cx, cy: +a.cy, r: +a.r, color: hex(a.fill) })
    } else if (name === 'path') {
      if (a['stroke-linecap'] !== 'round' || (a.fill && a.fill !== 'none')) throw new Error('make-phone-icons: only round-capped unfilled strokes are drawn')
      shapes.push({ kind: 'stroke', segs: pathSegments(a.d), w: +a['stroke-width'], color: hex(a.stroke) })
    } else {
      throw new Error(`make-phone-icons: <${name}> is not drawn by this script`)
    }
  }
  const leftover = body.replace(/<([a-z]+)\b([^>]*)\/>/g, '').trim()
  if (leftover) throw new Error(`make-phone-icons: unexpected content in the source: ${leftover.slice(0, 40)}`)
  if (!shapes.length || shapes[0].kind !== 'rect') throw new Error('make-phone-icons: the first shape must be the ground rect')
  return { size, shapes }
}

function pathSegments(d) {
  const toks = d.match(/[MLHVmlhv]|-?[\d.]+/g) || []
  const segs = []
  let x = 0, y = 0, cmd = null, i = 0
  const num = () => Number(toks[i++])
  while (i < toks.length) {
    if (/[A-Za-z]/.test(toks[i])) cmd = toks[i++]
    const x0 = x, y0 = y
    switch (cmd) {
      case 'M': x = num(); y = num(); cmd = 'L'; continue
      case 'm': x += num(); y += num(); cmd = 'l'; continue
      case 'L': x = num(); y = num(); break
      case 'l': x += num(); y += num(); break
      case 'H': x = num(); break
      case 'h': x += num(); break
      case 'V': y = num(); break
      case 'v': y += num(); break
      default: throw new Error(`make-phone-icons: path command ${cmd} is not drawn`)
    }
    segs.push([x0, y0, x, y])
  }
  return segs
}

// ------------------------------------------------------------------ the raster
function inside(s, x, y) {
  if (s.kind === 'rect') {
    const hx = s.w / 2, hy = s.h / 2
    const qx = Math.abs(x - (s.x + hx)) - (hx - s.rx)
    const qy = Math.abs(y - (s.y + hy)) - (hy - s.rx)
    const out = Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - s.rx
    return out <= 0
  }
  if (s.kind === 'circle') return Math.hypot(x - s.cx, y - s.cy) <= s.r
  for (const [ax, ay, bx, by] of s.segs) {
    const dx = bx - ax, dy = by - ay
    const len2 = dx * dx + dy * dy
    const t = len2 ? Math.min(1, Math.max(0, ((x - ax) * dx + (y - ay) * dy) / len2)) : 0
    if (Math.hypot(x - (ax + t * dx), y - (ay + t * dy)) <= s.w / 2) return true
  }
  return false
}

/** The variant's shapes: `full` = ground to the edges, the mark scaled about the centre. */
function variantShapes(mark, variant) {
  if (variant === 'any') return mark.shapes
  if (variant !== 'full') throw new Error(`unknown variant ${variant}`)
  const c = mark.size / 2, k = FULL_MARK_SCALE
  const sc = (v) => c + (v - c) * k
  return mark.shapes.map((s, i) => {
    if (i === 0) return { ...s, x: 0, y: 0, w: mark.size, h: mark.size, rx: 0 }
    if (s.kind === 'circle') return { ...s, cx: sc(s.cx), cy: sc(s.cy), r: s.r * k }
    if (s.kind === 'stroke') return { ...s, w: s.w * k, segs: s.segs.map(([a, b, d, e]) => [sc(a), sc(b), sc(d), sc(e)]) }
    return { ...s, x: sc(s.x), y: sc(s.y), w: s.w * k, h: s.h * k, rx: s.rx * k }
  })
}

/** Straight-alpha RGBA pixels, row-major. */
export function rasterize(mark, px, variant) {
  const shapes = variantShapes(mark, variant)
  const out = new Uint8Array(px * px * 4)
  const unit = mark.size / px
  for (let py = 0; py < px; py++) {
    for (let pxx = 0; pxx < px; pxx++) {
      let r = 0, g = 0, b = 0, a = 0
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const x = (pxx + (sx + 0.5) / SS) * unit
          const y = (py + (sy + 0.5) / SS) * unit
          for (let i = shapes.length - 1; i >= 0; i--) {
            if (inside(shapes[i], x, y)) {
              const col = shapes[i].color
              r += col[0]; g += col[1]; b += col[2]; a += 1
              break
            }
          }
        }
      }
      const o = (py * px + pxx) * 4
      if (a) {
        out[o] = Math.round(r / a); out[o + 1] = Math.round(g / a); out[o + 2] = Math.round(b / a)
        out[o + 3] = Math.round((a / (SS * SS)) * 255)
      }
    }
  }
  return out
}

// ------------------------------------------------------------------ PNG
const CRC = (() => {
  const t = new Uint32Array(256)
  for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0 }
  return t
})()
const crc32 = (buf) => { let c = 0xffffffff; for (const v of buf) c = CRC[(c ^ v) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0 }
function chunk(type, data) {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length)
  const td = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td))
  return Buffer.concat([len, td, crc])
}
export function encodePng(rgba, px) {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(px, 0); ihdr.writeUInt32BE(px, 4)
  ihdr[8] = 8; ihdr[9] = 6 // 8-bit RGBA
  const raw = Buffer.alloc(px * (px * 4 + 1))
  for (let y = 0; y < px; y++) Buffer.from(rgba.buffer, rgba.byteOffset + y * px * 4, px * 4).copy(raw, y * (px * 4 + 1) + 1)
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr), chunk('IDAT', deflateSync(raw, { level: 9 })), chunk('IEND', Buffer.alloc(0)),
  ])
}
/** Decode a PNG this script wrote (8-bit RGBA, filter 0) → { px, rgba }. */
export function decodeOwnPng(buf) {
  let off = 8, px = 0
  const idat = []
  while (off < buf.length) {
    const len = buf.readUInt32BE(off), type = buf.toString('ascii', off + 4, off + 8)
    const data = buf.subarray(off + 8, off + 8 + len)
    if (type === 'IHDR') { px = data.readUInt32BE(0); if (data[8] !== 8 || data[9] !== 6) throw new Error('not 8-bit RGBA') }
    if (type === 'IDAT') idat.push(data)
    off += 12 + len
  }
  const raw = inflateSync(Buffer.concat(idat))
  const rgba = new Uint8Array(px * px * 4)
  for (let y = 0; y < px; y++) {
    if (raw[y * (px * 4 + 1)] !== 0) throw new Error('unexpected PNG filter')
    raw.copy(Buffer.from(rgba.buffer), y * px * 4, y * (px * 4 + 1) + 1, (y + 1) * (px * 4 + 1))
  }
  return { px, rgba }
}

export function renderAll(svgText = readFileSync(join(UI, SOURCE), 'utf8')) {
  const mark = parseMark(svgText)
  return Object.fromEntries(Object.entries(ICONS).map(([name, [px, variant]]) => [name, { px, rgba: rasterize(mark, px, variant) }]))
}

// ------------------------------------------------------------------ CLI
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const check = process.argv.includes('--check')
  const all = renderAll()
  let bad = 0
  mkdirSync(join(UI, OUT_DIR), { recursive: true })
  for (const [name, { px, rgba }] of Object.entries(all)) {
    const file = join(UI, OUT_DIR, name)
    if (check) {
      let same = false
      try { const got = decodeOwnPng(readFileSync(file)); same = got.px === px && Buffer.compare(Buffer.from(got.rgba), Buffer.from(rgba)) === 0 } catch { same = false }
      console.log(`${same ? 'ok  ' : 'DIFF'} ${OUT_DIR}/${name} (${px}px)`)
      if (!same) bad++
    } else {
      writeFileSync(file, encodePng(rgba, px))
      console.log(`wrote ${OUT_DIR}/${name} (${px}px)`)
    }
  }
  process.exit(bad ? 1 : 0)
}
