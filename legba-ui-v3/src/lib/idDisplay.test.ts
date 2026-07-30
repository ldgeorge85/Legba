import { describe, it, expect } from 'vitest'
import { truncateId } from './idDisplay'

describe('truncateId', () => {
  it('truncates a UUID to head…tail', () => {
    expect(truncateId('a1b2c3d4-e5f6-7890-abcd-ef1234567890')).toBe('a1b2c3d4…7890')
  })

  it('truncates a long SHA hash', () => {
    expect(truncateId('9f8e7d6c5b4a3210fedcba9876543210deadbeef')).toBe('9f8e7d6c…beef')
  })

  it('leaves a short id unchanged', () => {
    expect(truncateId('abcd1234')).toBe('abcd1234')
  })

  it('leaves an id exactly at the head+tail+1 boundary unchanged', () => {
    // head=8, tail=4 -> boundary length 13
    const id = '1234567890123'
    expect(id.length).toBe(13)
    expect(truncateId(id)).toBe(id)
  })

  it('honors custom head/tail lengths', () => {
    expect(truncateId('abcdefghijklmnopqrstuvwxyz', 4, 2)).toBe('abcd…yz')
  })
})
