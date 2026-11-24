/**
 * The URL as an action list (UI-B1/shell; UI-D1 §3): a workstation address is
 * a CASE OF LENGTH ≤ 4, in a fixed order, and it round-trips.
 */
import { describe, expect, it } from 'vitest'
import { addressFor, parseAddress } from './address'
import { isWorkstationAction } from './applyAction'

describe('parseAddress', () => {
  it('compiles #ws&scope&sel&mission to [arrange, set_scope, focus, run_mission] — in that order', () => {
    const { actions, case: c } = parseAddress(
      '#mission=desk_watch&sel=finding:8f21&scope=target:country_g20_br&ws=desk',
    )
    expect(c).toBeNull()
    expect(actions).toEqual([
      { action: 'arrange', args: { layout: 'desk', layout_type: 'stance' } },
      { action: 'set_scope', args: { kind: 'target', id: 'country_g20_br' } },
      { action: 'focus', args: { ref: { kind: 'finding', id: '8f21' } } },
      { action: 'run_mission', args: { name: 'desk_watch' } },
    ])
    for (const a of actions) expect(isWorkstationAction(a)).toBe(true)
  })

  it('is never longer than four actions', () => {
    const { actions } = parseAddress('#ws=trust&scope=report:r&sel=target:t&mission=release&case=s:1&x=y')
    expect(actions.length).toBeLessThanOrEqual(4)
  })

  it('drops one unknown param, never the whole address', () => {
    const { actions } = parseAddress('#ws=nowhere&scope=planet:x&sel=target:country_g20_br&mission=nope')
    expect(actions).toEqual([{ action: 'focus', args: { ref: { kind: 'target', id: 'country_g20_br' } } }])
  })

  it('decodes ids with reserved characters', () => {
    const { actions } = parseAddress(`#sel=${encodeURIComponent(`entity:${encodeURIComponent('a b:c')}`)}`)
    expect(actions[0].args.ref).toEqual({ kind: 'entity', id: 'a b:c' })
  })

  it('reads #case=<session>:<turn>, and the phone\'s ?case= the same way', () => {
    expect(parseAddress('#case=sess-1:a1').case).toEqual({ session: 'sess-1', turn: 'a1' })
    expect(parseAddress('', '?case=sess-2:0').case).toEqual({ session: 'sess-2', turn: '0' })
    // The session id may itself carry a colon; the turn is after the LAST one.
    expect(parseAddress('#case=ns:sess:3').case).toEqual({ session: 'ns:sess', turn: '3' })
    expect(parseAddress('#case=nocolon').case).toBeNull()
    expect(parseAddress('#case=s:').case).toBeNull()
  })
})

describe('addressFor — the inverse', () => {
  it('round-trips an address through the action list', () => {
    const hash = '#ws=investigate&scope=situation%3Asit-1&sel=finding%3Af%252F1&mission=crisis'
    const parsed = parseAddress(hash)
    const again = parseAddress(addressFor(parsed.actions))
    expect(again.actions).toEqual(parsed.actions)
    expect(parsed.actions.find((a) => a.action === 'focus')!.args.ref).toEqual({ kind: 'finding', id: 'f/1' })
  })

  it('round-trips a case address', () => {
    const out = addressFor([], { session: 'sess-1', turn: 'a1' })
    expect(parseAddress(out).case).toEqual({ session: 'sess-1', turn: 'a1' })
  })

  it('writes only the addressable actions — a URL is not a recording', () => {
    expect(
      addressFor([
        { action: 'narrate', args: { text: 'x' } },
        // A window with no span preset has no short form (U-2: \`#win=\` is a preset span).
        { action: 'timeline', args: { start: 'a', end: 'b' } },
        { action: 'arrange', args: { layout: 'wall', layout_type: 'preset' } },
      ]),
    ).toBe('')
    // A preset span is the saved view's window.
    expect(addressFor([{ action: 'timeline', args: { span: '7d' } }])).toBe('#win=7d')
  })
})
