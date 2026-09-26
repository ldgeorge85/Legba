/**
 * Unit test for `stripJournalRefMarkers` (T1.3,
 * planning/JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09.md §6) — a journal
 * claim cites a raw signal directly as `[[ref:<uuid>]]`, a form disjoint from
 * the ordinal `[[ref:N]]` markers `citationsModel`'s tokenizer resolves into
 * chips (digit-only). Left alone, a uuid-form marker renders as literal
 * bracket noise in the prose; this is the mobile-safe (DOM-free) home for the
 * same strip the desktop reader (`panels/system/Journal.tsx`'s
 * `normalizeMarkers`) already applies, so both readers agree.
 */
import { describe, it, expect } from 'vitest'
import { stripJournalRefMarkers } from './proseText'

describe('stripJournalRefMarkers', () => {
  it('strips a single [[ref:<uuid>]] marker', () => {
    expect(
      stripJournalRefMarkers(
        'a claim about the Kansas prison [[ref:233bd806-5bee-4c3c-b0c3-1c9e794c52ee]].',
      ),
    ).toBe('a claim about the Kansas prison .')
  })

  it('strips multiple adjacent markers on one span', () => {
    expect(
      stripJournalRefMarkers(
        'coercive encounters [[ref:2185d60b-608d-4a5c-a68c-b2653e7177e8]], ' +
          '[[ref:22248ab6-7ef0-4c0c-921b-52ba706c562d]], ' +
          '[[ref:233bd806-5bee-4c3c-b0c3-1c9e794c52ee]].',
      ),
    ).toBe('coercive encounters , , .')
  })

  it('leaves the ORDINAL [[ref:N]] form untouched — a distinct marker family', () => {
    expect(stripJournalRefMarkers('the Gulf tension escalates [[ref:1]].')).toBe(
      'the Gulf tension escalates [[ref:1]].',
    )
  })

  it('leaves prose with no markers untouched', () => {
    expect(stripJournalRefMarkers('plain prose, no markers here.')).toBe(
      'plain prose, no markers here.',
    )
  })

  it('is a no-op on an empty string', () => {
    expect(stripJournalRefMarkers('')).toBe('')
  })
})
