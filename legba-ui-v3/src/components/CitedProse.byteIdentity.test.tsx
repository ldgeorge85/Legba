/**
 * The flip-kit byte-identity bar on the reading surface (UI-D1 §9b): a quoted
 * span goes through `CitedProse` — the one prose renderer — byte for byte. No
 * typographic smartening (straight quotes stay straight, `--` stays two
 * hyphens, `...` stays three dots), no truncation, no added or dropped
 * characters around a citation chip.
 *
 * Pinned in UI-B1/tokens because a token/type pass is exactly where a
 * "nicer quotes" plugin or a `font-feature-settings` habit creeps in; the
 * text the reader sees must stay the text the unit wrote.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import CitedProse from './CitedProse'

/** Characters a smartening pass would rewrite, all in one quoted span. */
const SPAN =
  `"Straight double" and 'straight single' quotes -- a double hyphen, ` +
  `three dots... (c) 1/2 -> => <= != x'y "z"`

/** What a smartening pass would have produced (curly quotes, dashes, …, ©, ½, arrows, ≤, ≠). */
const SMART = /[‘’“”–—…©½→⇒≤≠]/

describe('CitedProse — quoted spans are byte-identical', () => {
  it('renders a span exactly as written, with no typographic replacement', () => {
    const { container } = render(<CitedProse text={SPAN} citations={[]} />)
    const out = container.textContent ?? ''
    expect(out).toContain(SPAN)
    expect(out).not.toMatch(SMART)
  })

  it('a citation chip neither eats nor adds a character of the span around it', () => {
    const text = `${SPAN} [1] and after.`
    const { container } = render(
      <CitedProse
        text={text}
        citations={[{ marker: '[1]', signal_id: 'ffffffff-9999-4999-8999-ffffffffffff', title: 't' }] as never}
      />,
    )
    const out = container.textContent ?? ''
    expect(out).toContain(SPAN)
    expect(out).toContain('and after.')
  })
})
