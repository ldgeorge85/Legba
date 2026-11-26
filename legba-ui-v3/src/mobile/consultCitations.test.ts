import { describe, expect, it } from 'vitest'
import { consultAnswerCitations } from './consultCitations'

const A = 'a1b2c3d4-0000-4000-8000-000000000001'
const B = 'b2c3d4e5-0000-4000-8000-000000000002'
const A2 = 'a1b2c3d4-9999-4000-8000-000000000009' // shares A's 8-char prefix
const refs = [
  { kind: 'signal', id: A, description: 'one' },
  { kind: 'finding', id: B },
]

describe('consultAnswerCitations', () => {
  it('binds every bracketed row reference that names exactly one cited ref, in order of first use', () => {
    const r = consultAnswerCitations(`x [${B}] y [a1b2c3d4] z [b2c3d4e5]`, refs)
    expect(r.text).toBe('x [[ref:1]] y [[ref:2]] z [[ref:1]]')
    expect(r.citations.map((c) => [c.marker, c.refId, c.refKind])).toEqual([
      ['[[ref:1]]', B, 'finding'],
      ['[[ref:2]]', A, 'signal'],
    ])
    expect(r.citations[1].title).toBe('one')
  })

  it('binds a group and the [[ref:<uuid>]] form', () => {
    expect(consultAnswerCitations(`p [a1b2c3d4, b2c3d4e5] q [[ref:${A}]]`, refs).text).toBe(
      'p [[ref:1]][[ref:2]] q [[ref:1]]',
    )
  })

  it('leaves alone what it cannot bind: an uncited id, an ambiguous prefix, an ordinal, a link, prose', () => {
    const both = [...refs, { kind: 'signal', id: A2 }]
    const src = 'a [ffffffff] b [3] c [the text](https://example.org) d [a note in brackets]'
    expect(consultAnswerCitations(src, refs).text).toBe(src)
    expect(consultAnswerCitations('e [a1b2c3d4]', both).text).toBe('e [a1b2c3d4]')
    expect(consultAnswerCitations('f [a1b2c3d4, ffffffff]', refs).text).toBe('f [a1b2c3d4, ffffffff]')
  })

  it('no cited refs: the answer is returned as written, with no citations', () => {
    expect(consultAnswerCitations('**x** [1]', [])).toEqual({ text: '**x** [1]', citations: [] })
    expect(consultAnswerCitations('y', undefined)).toEqual({ text: 'y', citations: [] })
  })
})
