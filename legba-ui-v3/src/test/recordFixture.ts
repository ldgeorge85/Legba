/**
 * Invented rows for the Record sheet's tests (U-10) — never live content.
 *
 * One statement (`f-port`) written by an invented desk, citing two received
 * items: `s-linked` (two receipts — the feed response and its article page —
 * whose page later changed in substance) and `s-old` (arrived before receipts
 * began). The run that wrote the statement is covered by a later checkpoint
 * and a daily anchor.
 */
import type { IntegrityWire, LineageWire, ReceiptsWire, VersionTextWire } from '@/lib/recordChain'

export const FINDING_ID = '0f0e0d0c-0000-4000-8000-00000000f001'
export const SIG_LINKED = '0f0e0d0c-0000-4000-8000-00000000a001'
export const SIG_OLD = '0f0e0d0c-0000-4000-8000-00000000a002'
export const RUN_ID = '0f0e0d0c-0000-4000-8000-0000000000b1'
export const ARTICLE = 'https://harbour.example.org/2026/11/cranes-idle'

export const LINEAGE: LineageWire = {
  root: {
    id: FINDING_ID,
    row_kind: 'finding',
    title: 'Harbour cranes idle for a third day',
    produced_at: '2026-11-13T08:10:00Z',
    analyst_id: 'harbour_watch',
    target_id: 'subject_harbour',
    depth: 0,
    body: {
      title: 'Harbour cranes idle for a third day',
      body:
        '*As of 13 November 2026.*\n\n**BLUF:** The cranes at the north quay stood idle for a third day [1]. ' +
        'A second outlet reported talks set for Friday [2].\n\n- Earlier reports were thin [3].',
      data: {
        citations: [
          { marker: '[1]', signal_id: SIG_LINKED, title: 'Cranes idle at the north quay', source: ARTICLE, snippet: 'The cranes stood idle again on Thursday.' },
          { marker: '[2]', signal_id: SIG_OLD, title: 'Talks set for Friday', source: 'https://wire.example.net/talks' },
        ],
      },
    },
    receipt: { run_id: RUN_ID, receipt_hash: 'ab'.repeat(32), chain_consistent: true, signer_did: null },
  },
  nodes: [
    { id: SIG_LINKED, row_kind: 'signal', title: 'Cranes idle at the north quay', produced_at: '2026-11-13T06:00:00Z', depth: 1, canonical_url: ARTICLE, archived: true },
    { id: SIG_OLD, row_kind: 'signal', title: 'Talks set for Friday', produced_at: '2026-11-01T06:00:00Z', depth: 1, canonical_url: 'https://wire.example.net/talks', archived: false },
  ],
  edges: [
    { parent: SIG_LINKED, child: FINDING_ID },
    { parent: SIG_OLD, child: FINDING_ID },
  ],
  truncated_at_depth: false,
}

const v = (id: number, over: Partial<ReceiptsWire['versions'][string]>): ReceiptsWire['versions'][string] => ({
  id,
  receipt_kind: 'fetch',
  address: ARTICLE,
  url: ARTICLE,
  protocol: 'https',
  role: 'page',
  status: 200,
  content_type: 'text/html; charset=utf-8',
  size_bytes: 18_840,
  sha256: 'c'.repeat(64),
  first_seen_at: '2026-11-13T06:00:05Z',
  last_seen_at: '2026-11-13T06:00:05Z',
  fetch_count: 1,
  source_id: 'source.harbour_gazette',
  held: 'hot',
  text_chars: 412,
  has_text: true,
  ...over,
})

export const RECEIPTS: ReceiptsWire = {
  available: true,
  receipts_start: '2026-11-10T00:00:00Z',
  records: [
    {
      kind: 'signal',
      id: SIG_LINKED,
      found: true,
      fetched_at: '2026-11-13T06:00:00Z',
      canonical_url: ARTICLE,
      page_address: ARTICLE,
      license_class: null,
      source: { id: 'source.harbour_gazette', name: 'Harbour Gazette', kind: 'rss', source_class: 'reporting' },
      links: [
        { role: 'response', version_id: 101, sha256: 'd'.repeat(64), linked_at: '2026-11-13T06:00:01Z' },
        { role: 'page', version_id: 102, sha256: 'c'.repeat(64), linked_at: '2026-11-13T06:00:06Z' },
      ],
      no_receipt_reason: null,
    },
    {
      kind: 'signal',
      id: SIG_OLD,
      found: true,
      fetched_at: '2026-11-01T06:00:00Z',
      canonical_url: 'https://wire.example.net/talks',
      page_address: 'https://wire.example.net/talks',
      license_class: null,
      source: { id: 'source.wire', name: 'Example Wire', kind: 'rss', source_class: null },
      links: [],
      no_receipt_reason: 'before_receipts_start',
    },
  ],
  versions: {
    '101': v(101, { address: 'https://harbour.example.org/feed', url: 'https://harbour.example.org/feed', role: 'response', content_type: 'application/rss+xml', size_bytes: 52_300, sha256: 'd'.repeat(64), has_text: false, text_chars: null, fetch_count: 3, last_seen_at: '2026-11-13T07:00:00Z' }),
    '102': v(102, { held: 'cold' }),
  },
  pages: {
    [ARTICLE]: {
      versions: [v(102, { held: 'cold' }), v(103, { first_seen_at: '2026-11-14T02:00:00Z', last_seen_at: '2026-11-14T02:00:00Z', sha256: 'e'.repeat(64), role: 'archiver' })],
      more: false,
    },
  },
  changes: [
    { address: ARTICLE, earlier_version_id: 102, later_version_id: 103, drift_class: 'substantive_edit', later_fetched_at: '2026-11-14T02:00:00Z', later_gone: false },
  ],
}

export const INTEGRITY: IntegrityWire = {
  available: true,
  run_id: RUN_ID,
  found: true,
  unit: 'harbour_watch',
  run_ended_at: '2026-11-13T08:10:00Z',
  receipt: { state: 'verified', receipt_hash: 'ab'.repeat(32) },
  checkpoint: { state: 'covered', exact: false, checkpointed_at: '2026-11-13T09:00:00Z', signer_did: 'did:example:registry', trace_count: 40 },
  anchor: { state: 'covered', day: '2026-11-13', calendars: 2 },
}

export const TEXT_102: VersionTextWire = {
  available: true,
  version_id: 102,
  found: true,
  shown: true,
  withheld_reason: null,
  chars_total: 412,
  chars_shown: 60,
  truncated: true,
  text_hash_ok: true,
  full_text: '<script>alert(1)</script> The cranes stood idle again on Thursday.',
}
