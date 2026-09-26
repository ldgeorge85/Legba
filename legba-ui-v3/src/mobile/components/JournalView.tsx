/**
 * JournalView — the consolidation, read on the phone.
 *
 * The v2 Journal panel put the latest consolidation on top and collapsed the
 * individual cycle entries behind it. On a phone only the top half of that is
 * worth the screen: the consolidation IS the journal as far as a morning read
 * is concerned, and `GET /journal` hands it back as its own field rather than
 * as the first of a list, so there is nothing to search through.
 *
 * It renders through the same `CitedProse` the reads use, so a `[[ref:N]]` in
 * the agent's own prose behaves exactly as one in a record does.
 *
 * A journal claim can also cite a raw signal directly as `[[ref:<uuid>]]`
 * (T1.3, planning/JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09.md §6) — the
 * uuid form is disjoint from the ordinal `[[ref:N]]` `CitedProse` tokenizes
 * into chips, so left alone it would render as literal bracket noise (this
 * view passes no `citations`, so there is no chip binding for these refs on
 * the phone today — `stripJournalRefMarkers` at least keeps the noise out of
 * the read, matching the desktop reader's `normalizeMarkers` choice).
 */

import CitedProse from '@/components/CitedProse'
import { longStamp, score } from '@/v4/read/readFormat'
import { stripJournalRefMarkers } from '@/lib/proseText'
import type { JournalEntry } from '@/lib/api'

export interface JournalViewProps {
  entry: JournalEntry
  onBack: () => void
}

export default function JournalView({ entry, onBack }: JournalViewProps) {
  return (
    <div className="m-screen" data-testid="journal-view">
      <header className="m-masthead">
        <button type="button" className="m-back" onClick={onBack} data-testid="journal-back">
          ‹ Reports
        </button>
        <h1 className="m-title">{entry.title || 'Consolidation'}</h1>
        <p className="m-sub">
          Journal · {longStamp(entry.produced_at) ?? entry.produced_at}
        </p>
        <p className="m-verify">
          verify {score(entry.verify_score)}
          {entry.honesty_flags.length > 0 ? ` · ${entry.honesty_flags.join(', ')}` : ''}
        </p>
      </header>
      <div className="m-record">
        <CitedProse text={stripJournalRefMarkers(entry.body)} citations={[]} variant="block" />
      </div>
    </div>
  )
}
