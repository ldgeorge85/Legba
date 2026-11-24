import { describe, expect, it } from 'vitest'
import { cn } from './cn'

describe('cn — the type scale survives a colour class (U-3)', () => {
  it('keeps a named size beside a text colour', () => {
    expect(cn('rounded text-label', 'text-ink-1')).toBe('rounded text-label text-ink-1')
    expect(cn('text-body text-ink-3', 'hover:text-ink-1')).toBe('text-body text-ink-3 hover:text-ink-1')
  })

  it('still lets a later size replace an earlier one, and a later colour an earlier colour', () => {
    expect(cn('text-label', 'text-heading')).toBe('text-heading')
    expect(cn('text-[10px] text-ink-3', 'text-label')).toBe('text-ink-3 text-label')
    expect(cn('text-ink-3', 'text-ink-1')).toBe('text-ink-1')
  })
})
