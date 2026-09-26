/**
 * EvidenceMap — the signals + correlation grain, as an OVERVIEW.
 *
 * Ruling 5 requires all three grains present and correctly contexted, and
 * Layout B puts this one first. The trap is that the grain is genuinely large —
 * a typical block carries seven wire signals, and on the specimen day 892
 * citation rows sat across 227 desk heads. Rendered flat that is a wall, and a
 * wall in front of the record is worse than no evidence map at all.
 *
 * So this band is not the signal list. It is the two things the per-block drill
 * CANNOT show, because they are properties of the whole read:
 *
 *   1. THE SHAPE OF THE EVIDENCE, in a sentence. How many signals the quoted
 *      heads cite, over how many sources, how many carry a salience score.
 *   2. THE CORRELATION. Which signals were cited by MORE THAN ONE desk — 167 of
 *      628 distinct signals on the specimen day. This is the only place in the
 *      product where a reader can see that two independent-looking blocks are
 *      standing on the same wire item, and it is the reason the strip exists.
 *
 * Everything else is one click down, inside the block that cited it. The band
 * says so in its own last line rather than leaving the reader to discover it.
 */
import { useState } from 'react'
import type { AssemblyPayload } from '@/lib/assemblyModel'
import { evidenceTotals, sharedSignals } from '@/lib/assemblyModel'
import { plural, score } from './readFormat'

/** How many correlated signals to show before folding the rest. */
const SHOWN = 6

export default function EvidenceMap({ assembly }: { assembly: AssemblyPayload }) {
  const [expanded, setExpanded] = useState(false)
  const totals = evidenceTotals(assembly)
  const shared = sharedSignals(assembly)
  const visible = expanded ? shared : shared.slice(0, SHOWN)

  return (
    <section className="read-col mb-8" data-band="evidence" data-testid="read-evidence-map">
      <div className="read-band-head">
        <h2>Signals &amp; correlation</h2>
        <div className="read-rule" />
        <span className="read-band-note">what the quoted heads were standing on</span>
      </div>

      <p className="text-sm leading-relaxed text-ink-2" data-testid="read-evidence-summary">
        The {plural(assembly.blocks.length, 'head')} quoted below cite{' '}
        {plural(totals.signals, 'wire item')} between them &mdash; {totals.distinctSignals} distinct,
        across {plural(totals.sources, 'source')}, {totals.scored} of them carrying a salience score.{' '}
        {shared.length > 0 ? (
          <>
            {plural(shared.length, 'item is', 'items are')} cited by more than one desk, so the
            blocks that use them are not independent of each other.
          </>
        ) : (
          <>No item is cited by more than one desk in this read: every block stands on its own wire.</>
        )}
      </p>

      {shared.length > 0 && (
        <ul className="mt-4 space-y-3" data-testid="read-correlated-list">
          {visible.map((s, i) => (
            <li key={s.signal.signal_id ?? i} className="text-xs leading-relaxed" data-testid="read-correlated">
              <div className="text-ink-2">{s.signal.title}</div>
              <div className="mt-0.5 flex flex-wrap items-baseline gap-1.5 text-ink-3">
                {s.signal.source_id && <span>{s.signal.source_id.replace(/^source\./, '')}</span>}
                {s.signal.salience_magnitude !== null && (
                  <span>· salience {score(s.signal.salience_magnitude)}</span>
                )}
                {s.citedByOrdinals.length > 1 && (
                  <span>· cited by blocks {s.citedByOrdinals.join(', ')} on this page</span>
                )}
                {s.sharedWith.length > 0 && (
                  <span>
                    · also cited by{' '}
                    {s.sharedWith
                      .map((d) => `${d.desk}${d.target_id ? ` / ${d.target_id}` : ''}`)
                      .join(', ')}
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {shared.length > SHOWN && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-3 text-xs text-ink-3 underline decoration-dotted underline-offset-2 hover:text-ink-2"
          data-testid="read-correlated-more"
        >
          {expanded
            ? 'Fold the correlated items'
            : `Show the other ${shared.length - SHOWN} correlated ${
                shared.length - SHOWN === 1 ? 'item' : 'items'
              }`}
        </button>
      )}

      <p className="mt-4 text-xs text-ink-3">
        Every other wire item is one click down, inside the block that cited it &mdash; the drill
        lands on the sentence&rsquo;s own evidence, not on a page of everything.
      </p>
    </section>
  )
}
