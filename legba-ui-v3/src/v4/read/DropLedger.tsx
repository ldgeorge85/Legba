/**
 * DropLedger — what is NOT in today's read, honest and folded.
 *
 * Design §C.3.4: "if only one thing in this program ships, ship that." The
 * numbers it publishes have never been visible anywhere — country reads drop
 * 21.0% of what they are shown, world 16.1%, and the thematic tier drops 67.8%
 * of everything it is handed, invisibly, every run.
 *
 * The design problem is that a ledger is a list of absences, and a list of
 * absences rendered flat outweighs the read it qualifies. So the ledger states
 * itself in ONE sentence made of the counts, and folds the rows beneath it. The
 * sentence is always visible; the fold is always available; nothing is hidden
 * behind a hover.
 *
 * `invisible_heads` is a COUNT, never a list, and the copy says why: no
 * why-class is derivable for those rows because nothing records one — they sit
 * below the target-grain aperture. Publishing a fabricated reason for 421 rows
 * would be worse than publishing the number (spec §1.7).
 */
import { useState } from 'react'
import { selectRow } from '@/state/selection'
import { emitRead } from '@/lib/readTelemetry'
import type { AssemblyPayload, DropRow } from '@/lib/assemblyModel'
import { plural, score } from './readFormat'

/** The closed why-class enum, in the reader's words. */
const WHY_COPY: Record<string, string> = {
  shown_not_selected: 'shown to the composer, not quoted',
  not_selected: 'ranked below the line',
  cap_trimmed: 'trimmed by the input cap before the composer saw it',
  below_floor: 'had a read; it did not clear the verify floor',
  no_head_in_horizon: 'no head inside the 336-hour horizon',
  superseded: 'superseded by a newer head',
  correlated_duplicate: 'folded as a correlated duplicate',
  not_a_candidate: 'never a candidate for this surface',
}

function DropRows({ rows, testid }: { rows: DropRow[]; testid: string }) {
  if (rows.length === 0) return null
  return (
    <ul className="mt-2 space-y-1.5" data-testid={testid}>
      {rows.map((r) => (
        <li key={r.finding_id} className="text-xs leading-relaxed" data-testid="read-drop-row">
          <button
            type="button"
            onClick={() => {
              emitRead('finding_open', { subjectKind: 'finding', subjectId: r.finding_id })
              selectRow('finding', r.finding_id, r.title ?? r.desk, { origin: 'morning-read-ledger' })
            }}
            className="text-left text-ink-2 underline decoration-dotted underline-offset-2 hover:text-ink-1"
            title="Open this head in the Inspector"
          >
            {r.rank !== null && <span className="font-mono text-ink-3">#{r.rank} </span>}
            {r.desk} · {r.target_name ?? r.target_id ?? 'no target'}
          </button>
          <span className="text-ink-3">
            {r.severity ? ` · ${r.severity}` : ''}
            {r.cited_mass !== null ? ` · cited-mass ${score(r.cited_mass)}` : ''}
            {` · ${WHY_COPY[r.why] ?? r.why}`}
          </span>
          {r.title && <div className="text-ink-3">{r.title}</div>}
        </li>
      ))}
    </ul>
  )
}

export default function DropLedger({ assembly }: { assembly: AssemblyPayload }) {
  const [open, setOpen] = useState(false)
  const drops = assembly.drops
  if (!drops) return null
  const c = drops.counts
  const noHeadUnits = assembly.coverage.filter((u) => u.status === 'no_head_in_horizon')

  return (
    <section className="read-col mb-8" data-band="ledger" data-testid="read-drop-ledger">
      <div className="read-band-head">
        <h2>Not in this read</h2>
        <div className="read-rule" />
        <span className="read-band-note">the selection, published</span>
      </div>

      <p className="text-sm leading-relaxed text-ink-2" data-testid="read-drop-summary">
        {c.candidates > 0 && (
          <>
            {plural(c.candidates, 'lead span was', 'lead spans were')} candidates for this read;{' '}
            {c.carried} {c.carried === 1 ? 'is' : 'are'} quoted above.{' '}
          </>
        )}
        {c.not_selected > 0 && <>{plural(c.not_selected, 'ranked')} below the line. </>}
        {c.shown_not_carried > 0 && (
          <>
            {plural(c.shown_not_carried, 'head was', 'heads were')} shown to the composer and not
            quoted.{' '}
          </>
        )}
        {c.below_floor > 0 && <>{plural(c.below_floor, 'read')} did not clear the verify floor. </>}
        {c.no_head > 0 && (
          <>{plural(c.no_head, 'roster unit has', 'roster units have')} no head in the horizon. </>
        )}
        {c.invisible_heads > 0 && (
          <>
            A further {c.invisible_heads} desk heads never entered this surface&rsquo;s aperture at
            all.
          </>
        )}
      </p>

      {c.invisible_heads > 0 && (
        <p className="mt-2 text-xs leading-relaxed text-ink-3" data-testid="read-invisible-heads">
          Those {c.invisible_heads} are published as a count and not as a list, because nothing
          records a reason for them &mdash; they sit below this surface&rsquo;s target grain, not
          below a bar it applied. They are one click away on their own country boards.
        </p>
      )}

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="mt-3 text-xs text-ink-3 underline decoration-dotted underline-offset-2 hover:text-ink-2"
        data-testid="read-drop-toggle"
      >
        {open ? 'Fold the ledger' : 'Open the ledger'}
      </button>

      {open && (
        <div className="mt-3 space-y-4" data-testid="read-drop-detail">
          {drops.shown_not_carried.length > 0 && (
            <div>
              <div className="text-label uppercase tracking-wider text-ink-3">
                Shown to the composer, not quoted
              </div>
              <DropRows rows={drops.shown_not_carried} testid="read-drops-shown" />
            </div>
          )}
          {drops.not_selected.length > 0 && (
            <div>
              <div className="text-label uppercase tracking-wider text-ink-3">
                Ranked below the line
              </div>
              <p className="mt-1 text-xs text-ink-3">
                Selection is a strict prefix of the order, so &ldquo;a higher-ranked block was left
                out&rdquo; cannot happen by construction. The disclosure that carries weight is this
                list: what the order put below the cut, and by how much.
              </p>
              <DropRows rows={drops.not_selected} testid="read-drops-notselected" />
            </div>
          )}
          {drops.trimmed.length > 0 && (
            <div>
              <div className="text-label uppercase tracking-wider text-ink-3">
                Trimmed before the composer saw them
              </div>
              <DropRows rows={drops.trimmed} testid="read-drops-trimmed" />
            </div>
          )}
          {drops.below_floor.length > 0 && (
            <div>
              <div className="text-label uppercase tracking-wider text-ink-3">
                Below the verify floor
              </div>
              <DropRows rows={drops.below_floor} testid="read-drops-belowfloor" />
            </div>
          )}
          {noHeadUnits.length > 0 && (
            <div data-testid="read-drops-nohead">
              <div className="text-label uppercase tracking-wider text-ink-3">
                No head inside the horizon
              </div>
              <p className="mt-1 text-xs leading-relaxed text-ink-2">
                {noHeadUnits.map((u) => u.unit_name ?? u.unit).join(' · ')}
              </p>
            </div>
          )}
        </div>
      )}
    </section>
  )
}

/**
 * The coverage ledger, rendered from structured data rather than parsed out of
 * prose (VOICE §3.4 A3, spec §1.4c). `build_coverage_ledger` has computed this
 * every run since it shipped and thrown it away; the assembly persists it, which
 * is also what makes the `metadata_mismatch` fail class impossible.
 */
export function CoverageLine({ assembly }: { assembly: AssemblyPayload }) {
  const rows = assembly.coverage
  if (rows.length === 0) return null
  const byStatus = new Map<string, number>()
  for (const r of rows) byStatus.set(r.status, (byStatus.get(r.status) ?? 0) + 1)
  const parts = [...byStatus.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([status, n]) => `${n} ${status.replace(/_/g, ' ')}`)
  return (
    <p className="mt-3 text-xs leading-relaxed text-ink-3" data-testid="read-coverage">
      Coverage, from the persisted ledger and not from prose: {rows.length} roster units accounted
      for &mdash; {parts.join(', ')}.
    </p>
  )
}
