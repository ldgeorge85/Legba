/**
 * TensionNote — a declared tension, rendered where it belongs.
 *
 * The mock collected every tension into a "Declared tensions" section after the
 * blocks. That is where a report puts its appendix, and it is the reason the
 * tensions read as metadata rather than as the one thing on the page the tier is
 * allowed to NOTICE (VOICE §4.2 rank 3).
 *
 * Here a tension is rendered immediately after the LATER of the two blocks it
 * names — the reader has just finished both halves, and the note lands in the
 * gap between them. A `carried_vs_dropped` tension anchors on its one carried
 * half and names the other by desk and target, which is the single sharpest
 * argument for the drop ledger existing at all (spec §1.6 rule 3).
 *
 * The copy obeys §1.6 rule 1 — DECLARE, never EXPLAIN. The statement says THAT
 * two blocks pull apart and on what shared dimension. It may not say why: an
 * inferential link between two spans is on the MAY-NOT side of the permission
 * table, and a judged characterisation rides D-6, not the reader.
 */
import type { AssemblyTension } from '@/lib/assemblyModel'
import { scrollToAnchor } from './readFormat'

function scrollToBlock(ordinal: number): void {
  scrollToAnchor(`read-block-${ordinal}`)
}

function BlockRef({ ordinal }: { ordinal: number }) {
  return (
    <button
      type="button"
      onClick={() => scrollToBlock(ordinal)}
      className="underline decoration-dotted underline-offset-2 hover:text-ink-1"
      title={`Go to block ${ordinal}`}
    >
      block {ordinal}
    </button>
  )
}

export default function TensionNote({ tension }: { tension: AssemblyTension }) {
  const carriedVsDropped = tension.kind === 'carried_vs_dropped'
  return (
    <aside className="read-tension" data-testid="read-tension" data-kind={tension.kind}>
      <span className="read-tension-kicker">
        {carriedVsDropped ? 'tension — one half was dropped' : 'tension'}
      </span>
      <p>{tension.statement}</p>
      <p className="read-tension-note">
        {carriedVsDropped && tension.b_ref ? (
          <>
            The other half is <span className="font-mono">{tension.b_ref.desk}</span> /{' '}
            {tension.b_ref.target_name ?? tension.b_ref.target_id ?? 'no target'} — a head that did
            not reach this read. It is named in the ledger below.{' '}
          </>
        ) : null}
        Declared, not explained: <span className="font-mono">{tension.detector}</span> reports that
        these pull apart on a shared dimension. It does not say why, and neither does this page.
        {tension.a && tension.b ? (
          <>
            {' '}
            <BlockRef ordinal={tension.a.ordinal} /> · <BlockRef ordinal={tension.b.ordinal} />
          </>
        ) : tension.a ? (
          <>
            {' '}
            <BlockRef ordinal={tension.a.ordinal} />
          </>
        ) : null}
      </p>
    </aside>
  )
}

/**
 * The CHECKED NEGATIVE (§1.6 rule 2), and it is deterministic.
 *
 * A "no conflict this cycle" line that does not say what it checked is the M-11
 * defect in a new costume — the `## Tension` section that asserted unanimity in
 * a pipeline whose desks are structurally prevented from disagreeing, measured
 * at 13.8% of world bodies. So the counters are rendered, including the scope
 * note that says the detector reads BLUFs and declines ambivalent pairs.
 */
export function TensionNegative({
  pairsExamined,
  scope,
  scopeNote,
  blocks,
}: {
  pairsExamined: number
  scope: string
  scopeNote: string | null
  blocks: number
}) {
  return (
    <p className="mt-4 text-xs leading-relaxed text-ink-3" data-testid="read-tension-negative">
      No conflicting pair was detected among the {blocks} blocks shown — {pairsExamined} pairs
      examined
      {scope === 'shown_and_dropped' ? ', shown and dropped' : ', shown only'}
      {scopeNote ? `, ${scopeNote}` : ''}. The detector chose these pairs, not the tier: it can
      neither manufacture a tension nor dissolve one.
    </p>
  )
}
