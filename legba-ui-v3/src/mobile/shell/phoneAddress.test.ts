/**
 * M-2 — the phone's address: the desktop's keys plus `#screen=`, and the
 * history as the stack (Back from every drill-in, a cold deep link seeded).
 */
import { afterEach, describe, expect, it } from 'vitest'
import { addressOfScreen, back, depthOf, navigate, parentOf, phoneScreenOf, seedHistory, tabOfSurface, type PhoneScreen } from './phoneAddress'

const root = (tab: PhoneScreen['tab']): PhoneScreen => ({ tab, page: null, record: null, peek: false })

describe('phoneScreenOf', () => {
  it('lands on Changed with no address', () => {
    expect(phoneScreenOf('')).toEqual(root('changed'))
  })

  it('reads the desktop surfaces as tabs', () => {
    expect(phoneScreenOf('#ws=watch').tab).toBe('watch')
    expect(phoneScreenOf('#ws=ledger').tab).toBe('ledger')
    expect(phoneScreenOf('#ws=subject').tab).toBe('subjects')
    expect(phoneScreenOf('#ws=board').tab).toBe('changed')
    expect(tabOfSurface('engine')).toBeNull()
    // an operator surface is not on the phone: the landing
    expect(phoneScreenOf('#ws=engine').tab).toBe('changed')
  })

  it('reads #screen= pages and Consult', () => {
    expect(phoneScreenOf('#screen=consult').tab).toBe('consult')
    expect(phoneScreenOf('#screen=read:ex_abc').page).toEqual({ kind: 'read', id: 'ex_abc' })
    expect(phoneScreenOf('#screen=read:ex_abc').tab).toBe('changed')
    expect(phoneScreenOf('#screen=alert:ex-alert-1').tab).toBe('watch')
    expect(phoneScreenOf('#screen=subject:country%20x').page).toEqual({ kind: 'subject', id: 'country x' })
    expect(phoneScreenOf('#ws=subject&screen=map').page).toEqual({ kind: 'map' })
    expect(phoneScreenOf('#screen=nonsense').page).toBeNull()
  })

  it('keeps #rec= working: the Record as a page, or the peek', () => {
    const s = phoneScreenOf('#rec=finding:ex_1&cite=2')
    expect(s.record).toEqual({ ref: { kind: 'finding', id: 'ex_1' }, cite: 2 })
    expect(s.peek).toBe(false)
    expect(phoneScreenOf('#rec=finding:ex_1&peek=1').peek).toBe(true)
    // a kind the chain cannot read is no record
    expect(phoneScreenOf('#rec=target:x').record).toBeNull()
  })
})

describe('addressOfScreen', () => {
  it('round-trips and keeps the point and the window', () => {
    const s: PhoneScreen = { tab: 'watch', page: { kind: 'alert', id: 'a 1' }, record: { ref: { kind: 'signal', id: 's1' }, cite: null }, peek: true }
    const a = addressOfScreen(s, '#sel=target:x&win=7d&ws=board')
    expect(a.startsWith('#ws=watch&screen=')).toBe(true)
    expect(phoneScreenOf(a)).toEqual(s)
    const p = new URLSearchParams(a.slice(1))
    expect(p.get('sel')).toBe('target:x')
    expect(p.get('win')).toBe('7d')
    expect(p.getAll('ws')).toEqual(['watch'])
  })

  it('a Consult address carries no surface', () => {
    expect(addressOfScreen(root('consult'))).toBe('#screen=consult')
  })
})

describe('depth and parents', () => {
  it('walks up one screen at a time', () => {
    const s: PhoneScreen = { tab: 'changed', page: { kind: 'read', id: 'r' }, record: { ref: { kind: 'finding', id: 'f' }, cite: null }, peek: false }
    expect(depthOf(s)).toBe(2)
    expect(parentOf(s)).toEqual({ ...s, record: null })
    expect(parentOf(parentOf(s)!)).toEqual(root('changed'))
    expect(parentOf(root('changed'))).toBeNull()
  })
})

describe('navigation through history', () => {
  afterEach(() => {
    window.history.replaceState(null, '', '/m/')
  })

  it('a drill-in pushes, Back pops; a tab switch replaces', async () => {
    window.history.replaceState(null, '', '/m/')
    const start = window.history.length
    navigate({ tab: 'changed', page: { kind: 'read', id: 'r1' }, record: null, peek: false }, 'push')
    expect(window.location.hash).toBe('#ws=board&screen=read%3Ar1')
    expect(window.history.length).toBe(start + 1)
    navigate(root('ledger'), 'replace')
    expect(window.history.length).toBe(start + 1)
    expect(phoneScreenOf(window.location.hash)).toEqual(root('ledger'))
  })

  it('a cold deep link is seeded with its parents', () => {
    window.history.replaceState(null, '', '/m/#ws=board&screen=read%3Ar1&rec=finding%3Af1')
    const start = window.history.length
    seedHistory()
    expect(window.history.length).toBe(start + 2)
    expect(phoneScreenOf(window.location.hash).record?.ref.id).toBe('f1')
    expect(typeof back).toBe('function')
  })
})
