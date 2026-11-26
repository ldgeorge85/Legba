/**
 * trustWords — what a reader is told about how far to trust a finding, in at
 * most two plain words, each with its number where it has one:
 *
 *   1. what the check found: "Checked" (it ran and flagged nothing),
 *      "n claims flagged", "Not yet checked" (the check may still land) or
 *      "Not checked" (none is coming); nothing for a producer the check
 *      never covers;
 *   2. what it rests on: "n sources", "Single source" or "No source cited".
 *
 * Every place a finding's trust is shown uses this one function: the Board's
 * list, the Record sheet, the subject page and the findings rows. The full
 * readout (likelihood, confidence, the per-claim check, the outside
 * reference) lives in the collapsed "How this was produced" disclosure.
 */
import { whyNot } from '@/lib/claimsModel'
import { isVerifyExemptState, unverifiedStateOf, type UnverifiedRowLike } from '@/lib/unverifiedState'
import { countOf } from '@/lib/readerVocabulary'

export interface TrustWord {
  text: string
  /** What the word rests on, for its title. */
  title?: string
}

/** One cited row: the source it names (an outlet id or an origin), when it names one. */
export interface TrustCitation {
  source?: string | null
  source_id?: string | null
  sourceId?: string | null
}

export interface TrustInput extends UnverifiedRowLike {
  verification?: unknown
  citations?: readonly TrustCitation[] | null
}

/** The distinct sources a citation list rests on. */
export function distinctSources(cites: readonly TrustCitation[]): number {
  const s = new Set(
    cites.map((c) => c.source_id ?? c.sourceId ?? c.source).filter((x): x is string => typeof x === 'string' && !!x),
  )
  return s.size
}

/**
 * The trust words of a finding. Without the finding's check row (it fell
 * outside the page read), a verified-finding alert on it still says
 * "Checked"; nothing else is guessed.
 */
export function findingTrust(
  row: TrustInput | null | undefined,
  alertedVerified = false,
  opts: { checkMayBeElsewhere?: boolean } = {},
): TrustWord[] {
  if (!row) return alertedVerified ? [{ text: 'Checked', title: 'An alert fired on it as a checked finding' }] : []
  const out: TrustWord[] = []
  // A read that may simply not carry the check's answer (the record's lineage
  // read leaves it out) says nothing about the check rather than "Not checked".
  const unknown = opts.checkMayBeElsewhere === true && row.verification == null && row.verification_state == null
  const unverified = unknown ? null : unverifiedStateOf(row)
  if (unknown) {
    // nothing said about the check
  } else if (unverified) {
    if (!isVerifyExemptState(unverified)) {
      out.push(
        unverified.state === 'pending'
          ? { text: 'Not yet checked', title: 'The check has not run on it yet' }
          : { text: 'Not checked', title: 'No check ran on it, and none is coming' },
      )
    }
  } else {
    const verification =
      row.verification && typeof row.verification === 'object' ? (row.verification as Record<string, unknown>) : null
    const flagged = whyNot(verification)
    if (flagged && flagged.unsupportedSpans.length > 0) {
      const n = flagged.unsupportedSpans.length
      out.push({ text: `${countOf(n, 'claim')} flagged`, title: 'The check found claims its sources do not support' })
    } else if (verification) {
      out.push({ text: 'Checked', title: 'The check ran and flagged nothing' })
    }
  }
  const cites = Array.isArray(row.citations) ? row.citations : []
  const sources = distinctSources(cites)
  if (cites.length === 0) out.push({ text: 'No source cited' })
  else if (sources <= 1) out.push({ text: 'Single source', title: countOf(cites.length, 'citation') })
  else out.push({ text: countOf(sources, 'source'), title: countOf(cites.length, 'citation') })
  return out.slice(0, 2)
}
