/**
 * The phone's touch floor is 44 × 44 px, BOTH ways (a live walk at
 * 360 px found a subject button reading "Iran" 28 × 44 — `.ph-link` was 44 px
 * tall at any width, so a short name made a narrow control).
 *
 * Every rule in the phone's stylesheet that lifts a CONTROL (a button, a link,
 * a select, a chip, a tab) to the 44 px height must also hold it at least
 * 44 px wide, so the floor holds whatever the label. A pseudo-element's reach
 * (`::after`) and a layout container are not controls and are not held here.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const CSS = readFileSync(join(__dirname, 'phone.css'), 'utf8')

interface Rule {
  selector: string
  body: string
}

function rules(css: string): Rule[] {
  const out: Rule[] = []
  const noComments = css.replace(/\/\*[\s\S]*?\*\//g, '')
  for (const m of noComments.matchAll(/([^{}]+)\{([^{}]*)\}/g)) out.push({ selector: m[1].trim().replace(/\s+/g, ' '), body: m[2] })
  return out
}

const px = (body: string, prop: string): number | null => {
  const m = new RegExp(`(?:^|;|\\s)${prop}\\s*:\\s*(\\d+(?:\\.\\d+)?)px`).exec(body)
  return m ? Number(m[1]) : null
}

/** A selector naming a control: a button, a link, a select, a chip, a tab. */
const CONTROL = /(^|[\s,>.(])(button|a|select)\b|-(btn|link|chip|select|tab)\b|\.ph-(link|chip|select|btn)/

describe('the phone touch floor (44 × 44 px)', () => {
  const all = rules(CSS)

  it('reads the stylesheet', () => {
    expect(all.length).toBeGreaterThan(50)
  })

  it('every control lifted to 44 px tall is held 44 px wide too', () => {
    const narrow = all
      .filter((r) => !r.selector.includes('::'))
      .filter((r) => CONTROL.test(r.selector))
      .filter((r) => (px(r.body, 'min-height') ?? px(r.body, 'height') ?? 0) >= 44)
      .filter((r) => (px(r.body, 'min-width') ?? px(r.body, 'width') ?? 0) < 44)
      .map((r) => r.selector)
    expect(narrow).toEqual([])
  })

  it('an invisible ::after reach of 44 px tall is 44 px wide too (a two-letter name in an alert row was 16 px)', () => {
    const reaches = all.filter((r) => r.selector.endsWith('::after') && (px(r.body, 'height') ?? 0) >= 44)
    expect(reaches.length).toBeGreaterThan(0)
    for (const r of reaches) expect(r.body, r.selector).toMatch(/width:\s*max\(100%,\s*44px\)/)
  })

  it('the subject link (a short name, "Iran") is one of them', () => {
    const link = all.find((r) => r.selector === '.ph-link')
    expect(link).toBeDefined()
    expect(px(link!.body, 'min-height')).toBeGreaterThanOrEqual(44)
    expect(px(link!.body, 'min-width')).toBeGreaterThanOrEqual(44)
  })
})
