/**
 * recordChain (U-10) — the Record sheet's model: each step from invented rows,
 * and each gap stated in words where it occurs (never an empty box, never a
 * tick by default).
 */
import { describe, expect, it } from 'vitest'
import {
  RECORD_ROUTES,
  canReadItem,
  citedSentence,
  evidenceLabelWords,
  itemWithheldWords,
  evidenceOf,
  integrityLines,
  laterVersionsOf,
  leadSentence,
  noReceiptWords,
  receiptFacts,
  receiptIds,
  rowLine,
  sourceLine,
  statementOf,
  textWithheldWords,
  type EvidenceStateWire,
  type ItemTextWire,
  type LineageWire,
} from '@/lib/recordChain'
import { ARTICLE, FINDING_ID, INTEGRITY, LINEAGE, RECEIPTS, SIG_LINKED, SIG_OLD } from '@/test/recordFixture'

describe('the routes are bounded', () => {
  it('walks lineage at depth 1, reads receipts in one call, the text with a cap', () => {
    expect(RECORD_ROUTES.lineage('finding', 'x')).toBe('/lineage/finding/x?direction=upstream&depth=1')
    expect(RECORD_ROUTES.receipts('signal', ['a', 'b'])).toBe('/v3/record/receipts?kind=signal&ids=a,b')
    expect(RECORD_ROUTES.text(7)).toBe('/v3/record/version/7/text?max_chars=20000')
  })
})

describe('step 1 — the statement', () => {
  it('finds the sentence a marker closes, markers and markdown removed', () => {
    const body = String(LINEAGE.root.body!.body)
    expect(citedSentence(body, 1)).toBe('The cranes at the north quay stood idle for a third day.')
    expect(citedSentence(body, 2)).toBe('A second outlet reported talks set for Friday.')
    expect(citedSentence('One. Two [[ref:4]]. Three.', 4)).toBe('Two.')
    expect(citedSentence(body, 9)).toBeNull()
  })

  it('leads with the first real sentence, skipping an "As of" line', () => {
    expect(leadSentence(String(LINEAGE.root.body!.body))).toBe('The cranes at the north quay stood idle for a third day.')
  })

  it('a cited sentence carries its citation; a missing one says so and falls back to the title', () => {
    const s = statementOf(LINEAGE.root, 1)
    expect(s.basis).toBe('cited_sentence')
    expect(s.citation?.refId).toBe(SIG_LINKED)
    expect(s.gap).toBeNull()
    const miss = statementOf(LINEAGE.root, 7)
    expect(miss.basis).toBe('title')
    expect(miss.text).toBe('Harbour cranes idle for a third day')
    expect(miss.gap).toMatch(/No sentence of this record carries citation 7/)
  })

  it('a marker with no citation behind it maps to no row, in words', () => {
    const s = statementOf(LINEAGE.root, 3)
    expect(s.basis).toBe('cited_sentence')
    expect(s.citation).toBeNull()
    expect(s.gap).toMatch(/maps to no row/)
    expect(evidenceOf(LINEAGE, s, 3).gap).toMatch(/maps to no row/)
  })
})

describe('step 2 — the row', () => {
  it('names who wrote it and when, in reader words', () => {
    expect(rowLine(LINEAGE.root)).toBe('Written by Harbour watch on 13 Nov 2026, 08:10 UTC.')
    expect(rowLine(LINEAGE.nodes[0])).toBe('Received from harbour.example.org on 13 Nov 2026, 06:00 UTC.')
  })
})

describe('step 3 — the evidence', () => {
  it('a cited sentence has exactly its own evidence row', () => {
    const ev = evidenceOf(LINEAGE, statementOf(LINEAGE.root, 1), 1)
    expect(ev.items.map((i) => [i.kind, i.id, i.ordinal])).toEqual([['signal', SIG_LINKED, 1]])
  })

  it('a whole statement lists its citations, deduplicated; receipts are asked for signals only', () => {
    const ev = evidenceOf(LINEAGE, statementOf(LINEAGE.root, null), null)
    expect(ev.items.map((i) => i.id)).toEqual([SIG_LINKED, SIG_OLD])
    expect(receiptIds(ev)).toEqual([SIG_LINKED, SIG_OLD])
  })

  it('a statement with no citations falls back to its lineage parents; none at all is a gap', () => {
    const bare: LineageWire = { ...LINEAGE, root: { ...LINEAGE.root, body: { body: 'Plain.' } } }
    expect(evidenceOf(bare, statementOf(bare.root, null), null).items.map((i) => i.id)).toEqual([SIG_LINKED, SIG_OLD])
    const none: LineageWire = { ...bare, nodes: [] }
    expect(evidenceOf(none, statementOf(none.root, null), null).gap).toMatch(/names no rows/)
  })

  it('the source line carries its class, or says the class is not recorded', () => {
    expect(sourceLine(RECEIPTS.records[0])).toBe('Harbour Gazette · reporting')
    expect(sourceLine(RECEIPTS.records[1])).toBe('Example Wire · class not recorded')
  })
})

describe('step 4 — the receipt', () => {
  it('states fetched, from, protocol, hash and stored version', () => {
    const facts = Object.fromEntries(receiptFacts(RECEIPTS.versions['101']).map((f) => [f.label, f.value]))
    expect(facts.Fetched).toBe('13 Nov 2026, 06:00 UTC')
    expect(facts.From).toBe('https://harbour.example.org/feed')
    expect(facts.Protocol).toBe('https · status 200 · application/rss+xml')
    expect(facts['Content hash']).toBe('sha256 dddddddddddd… · 51.1 KB')
    expect(facts['Stored version']).toMatch(/^version 101 · the same bytes seen 3 times/)
  })

  it('names each no-receipt cause', () => {
    expect(noReceiptWords(RECEIPTS.records[1], RECEIPTS.receipts_start)).toMatch(/before fetch receipts were kept \(from 10 Nov 2026/)
    expect(noReceiptWords({ ...RECEIPTS.records[1], no_receipt_reason: 'source_kind_without_receipts', source: { id: 's', name: 'A channel', kind: 'telegram_channel', source_class: null } }, null)).toMatch(
      /A channel is not fetched over the web \(a telegram channel source\)/,
    )
    expect(noReceiptWords({ ...RECEIPTS.records[1], no_receipt_reason: 'not_linked' }, null)).toMatch(/no receipt was linked/)
  })

  it('a withheld text says why', () => {
    const base = { available: true, version_id: 1, found: true, shown: false, chars_total: 9, chars_shown: 0, truncated: false, text_hash_ok: null, full_text: null }
    expect(textWithheldWords({ ...base, withheld_reason: 'reader_display_policy' })).toMatch(/admins and owners only/)
    expect(textWithheldWords({ ...base, withheld_reason: 'license_forbids' })).toMatch(/licence does not allow/)
    expect(textWithheldWords({ ...base, withheld_reason: 'no_text' })).toMatch(/kept as bytes only/)
  })
})

describe('step 5 — later versions', () => {
  it('a page that changed in substance after it was read says so', () => {
    const later = laterVersionsOf(RECEIPTS.records[0], RECEIPTS)!
    expect(later.readAt).toBe('2026-11-13T06:00:05Z')
    expect(later.later.map((l) => [l.version.id, l.drift])).toEqual([[103, 'substantive_edit']])
    expect(later.summary).toMatch(/^Changed after it was read: a substantive edit/)
  })

  it('an address never fetched as a page says the change is not known', () => {
    const later = laterVersionsOf(RECEIPTS.records[1], RECEIPTS)!
    expect(later.later).toEqual([])
    expect(later.summary).toMatch(/never fetched as a page/)
  })

  it('a record with no page address has no later-versions step', () => {
    expect(laterVersionsOf({ ...RECEIPTS.records[0], page_address: null }, RECEIPTS)).toBeNull()
  })

  it('the same page fetched again unchanged has no later version', () => {
    const same = { ...RECEIPTS, pages: { [ARTICLE]: { versions: [{ ...RECEIPTS.versions['102'], last_seen_at: '2026-11-14T00:00:00Z' }], more: false } }, changes: [] }
    expect(laterVersionsOf(RECEIPTS.records[0], same)!.summary).toMatch(/with the same bytes: no later version/)
  })
})

describe('step 6 — integrity', () => {
  it('states verified, covered and not covered in words; never a tick for what was not checked', () => {
    const lines = integrityLines(LINEAGE.root, INTEGRITY)
    expect(lines.map((l) => [l.label, l.state])).toEqual([
      ['Run receipt', 'verified'],
      ['Checkpoint', 'covered'],
      ['Daily anchor', 'covered'],
    ])
    expect(lines[1].words).toMatch(/not re-walked here/)
    expect(lines[2].words).toMatch(/checked offline, not here/)
  })

  it('a failed re-hash is failed; no integrity read is not covered', () => {
    const failed = integrityLines(LINEAGE.root, { ...INTEGRITY, receipt: { state: 'failed' } })
    expect(failed[0].state).toBe('failed')
    const unread = integrityLines(LINEAGE.root, null)
    expect(unread.map((l) => l.state)).toEqual(['verified', 'not_covered'])
  })

  it('a received item has no run receipt, and says what checks it instead', () => {
    const lines = integrityLines({ ...LINEAGE.nodes[0], receipt: null }, null)
    expect(lines).toHaveLength(1)
    expect(lines[0].state).toBe('not_covered')
    expect(lines[0].words).toMatch(/content hash on its fetch receipt/)
  })

  it('a row with no run receipt is not covered', () => {
    const lines = integrityLines({ ...LINEAGE.root, id: FINDING_ID, receipt: null }, null)
    expect(lines[0].state).toBe('not_covered')
  })
})

describe('The evidence label and the item as received', () => {
  const ev = (over: Partial<EvidenceStateWire>): EvidenceStateWire => ({
    label: 'received_bytes', label_text: 'received bytes', version_id: 101, link_role: 'response', has_text: true, hash_only: [], ...over,
  })

  it('reads the item route with the cap', () => {
    expect(RECORD_ROUTES.item('a b')).toBe('/v3/record/signal/a%20b/item?max_chars=20000')
  })

  it('says each of the three labels, and a hash-only receipt', () => {
    expect(evidenceLabelWords(ev({}))).toMatch(/^Received bytes: the bytes the poll received are held/)
    expect(evidenceLabelWords(ev({ label: 'archived_later', label_text: 'archived later' }))).toMatch(/^Archived later: .*may have changed/)
    expect(evidenceLabelWords(ev({ label: 'no_bytes_kept', label_text: 'no bytes kept', version_id: null }))).toBe(
      'No bytes kept: nothing this item arrived in is held.',
    )
    expect(evidenceLabelWords(ev({ label: 'no_bytes_kept', version_id: null, hash_only: [7] }))).toBe(
      'No bytes kept: nothing this item arrived in is held; one receipt is named by hash only.',
    )
    expect(evidenceLabelWords(ev({ label: 'no_bytes_kept', version_id: null, hash_only: [7, 8] }))).toMatch(/2 receipts are named by hash only\.$/)
    expect(evidenceLabelWords(null)).toBeNull()
    expect(evidenceLabelWords(undefined)).toBeNull()
  })

  it('offers the item only on the labelled response or pushed envelope with readable text', () => {
    expect(canReadItem(ev({}), 101, 'response')).toBe(true)
    expect(canReadItem(ev({ link_role: 'inbound' }), 101, 'inbound')).toBe(true)
    expect(canReadItem(ev({}), 102, 'response')).toBe(false)         // another version
    expect(canReadItem(ev({}), 101, 'page')).toBe(false)             // a page shows its own stored text
    expect(canReadItem(ev({ has_text: false }), 101, 'response')).toBe(false)
    expect(canReadItem(null, 101, 'response')).toBe(false)
  })

  it('a withheld item says why', () => {
    const base: ItemTextWire = {
      available: true, signal_id: 's', found: true, evidence: null, version_id: 1, basis: null, exact: null, shown: false,
      withheld_reason: null, chars_total: null, chars_shown: 0, truncated: false, text_sha256: null, item_sha256: null,
      version_sha256: null, full_text: null,
    }
    expect(itemWithheldWords({ ...base, withheld_reason: 'no_bytes_kept' })).toMatch(/No bytes were kept/)
    expect(itemWithheldWords({ ...base, withheld_reason: 'reader_display_policy' })).toMatch(/admins and owners only/)
    expect(itemWithheldWords({ ...base, withheld_reason: 'claim_text_admin_only' })).toMatch(/claim source/)
    expect(itemWithheldWords({ ...base, withheld_reason: 'license_forbids' })).toMatch(/licence does not allow/)
    expect(itemWithheldWords({ ...base, withheld_reason: 'no_item_text' })).toMatch(/holds no item/)
    expect(itemWithheldWords({ ...base, withheld_reason: 'item_reader_unavailable' })).toMatch(/cannot read stored bytes/)
    expect(itemWithheldWords({ ...base, found: false })).toMatch(/not found/)
  })
})
