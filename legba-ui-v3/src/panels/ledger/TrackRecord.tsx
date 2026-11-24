/**
 * TrackRecord — how good the forecasts have been (`view:track_record`, the
 * Ledger page's first panel; U-11).
 *
 * THE OWNER'S RULE: no forecast score is shown until five questions have
 * resolved. The ONE switch is `lib/ledgerProof.ts::trackRecordGate`. Below
 * the floor this region prints the count toward it and, in words, what will
 * appear — no score, no partial score, and no good/warning/critical colour
 * anywhere in it (nothing that reads as a verdict). From the floor: each
 * forecaster's Brier score beside the baseline of always saying 50 %, with
 * its n; calibration by probability band with the count in each band; and
 * every resolved forecast with its outcome — a miss as plainly as a hit,
 * never folded away.
 */
import { cn } from '@/lib/cn'
import {
  WILL_APPEAR,
  armScores,
  calibration,
  countWord,
  resolvedRows,
  trackRecordGate,
  type ArmScore,
} from '@/lib/ledgerProof'
import { useLedgerScorecard } from './ledgerQueries'

const VERSUS_CLASS: Record<ArmScore['versusKind'], string> = {
  better: 'text-accent-ok',
  worse: 'text-accent-critical',
  level: 'text-ink-2',
  none: 'text-ink-3',
}

export default function TrackRecord() {
  const q = useLedgerScorecard()
  const gate = trackRecordGate(q.isPending ? undefined : q.data, { failed: q.isError })
  const data = q.data ?? null

  return (
    <section className="space-y-3" data-testid="track-record" data-state={gate.state} data-scored={gate.state === 'scored' ? '1' : '0'}>
      <h3 className="text-heading font-semibold text-ink-1">Track record</h3>
      <p className="text-body-lg text-ink-1" data-testid="track-record-line">
        {gate.line}
      </p>

      {gate.state === 'withheld' && (
        <div className="space-y-3" data-testid="track-record-withheld">
          <p className="font-mono text-body text-ink-2" data-testid="track-record-count">
            {gate.resolved} / {gate.floor} resolved
          </p>
          <div className="text-body text-ink-2">
            <p>
              No score is shown until {countWord(gate.floor)} forecasts have resolved
              {gate.remaining ? ` (${countWord(gate.remaining)} to go)` : ''}. Then this panel shows:
            </p>
            <ul className="mt-1 list-disc space-y-1 pl-5" data-testid="track-record-will-appear">
              {WILL_APPEAR.map((w) => (
                <li key={w}>{w}.</li>
              ))}
            </ul>
          </div>
          <p className="text-label text-ink-3">
            A voided forecast does not count toward {countWord(gate.floor)}. Results are shown as they are, losses included.
          </p>
        </div>
      )}

      {gate.state === 'scored' && data && <Scored gate={gate} data={data} />}
    </section>
  )
}

function Scored({ gate, data }: { gate: ReturnType<typeof trackRecordGate>; data: NonNullable<ReturnType<typeof useLedgerScorecard>['data']> }) {
  const arms = armScores(data, gate)
  const cal = calibration(data, gate)
  const resolved = resolvedRows(data, gate)
  return (
    <div className="space-y-4" data-testid="track-record-scored">
      <div>
        <h4 className="text-label uppercase tracking-[0.06em] text-ink-3">Brier score (lower is better)</h4>
        <p className="mt-1 text-label text-ink-3">
          Baseline: always saying 50 %, scored on the same questions. A score below the baseline's is skill; above it, worse
          than a coin.
        </p>
        <table className="mt-2 w-full border-t border-line text-body" data-testid="track-record-score">
          <thead>
            <tr className="text-left text-label text-ink-3">
              <th className="py-1 pr-3 font-normal">Forecaster</th>
              <th className="py-1 pr-3 font-normal">Brier</th>
              <th className="py-1 pr-3 font-normal">Baseline</th>
              <th className="py-1 pr-3 font-normal">n</th>
            </tr>
          </thead>
          <tbody>
            {arms.map((a) => (
              <tr key={a.kind} className="border-t border-line align-baseline" data-testid="track-record-arm">
                <td className="py-1.5 pr-3 text-ink-1">{a.who}</td>
                <td className="py-1.5 pr-3 font-mono text-ink-1">
                  {a.brier}
                  {a.versus && <div className={cn('font-sans text-label', VERSUS_CLASS[a.versusKind])}>{a.versus}</div>}
                </td>
                <td className="py-1.5 pr-3 font-mono text-ink-2">{a.baseline}</td>
                <td className="py-1.5 pr-3 font-mono text-ink-2">{a.n}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {cal && (
        <div data-testid="track-record-calibration">
          <h4 className="text-label uppercase tracking-[0.06em] text-ink-3">Calibration — {cal.who}'s forecasts by band</h4>
          <table className="mt-2 w-full border-t border-line text-body">
            <thead>
              <tr className="text-left text-label text-ink-3">
                <th className="py-1 pr-3 font-normal">Said</th>
                <th className="py-1 pr-3 font-normal">Forecasts</th>
                <th className="py-1 pr-3 font-normal">Came true</th>
              </tr>
            </thead>
            <tbody>
              {cal.rows.map((r) => (
                <tr key={r.band} className="border-t border-line" data-testid="track-record-band">
                  <td className="py-1 pr-3 font-mono text-ink-2">{r.band}</td>
                  <td className="py-1 pr-3 font-mono text-ink-1">{r.n}</td>
                  <td className="py-1 pr-3 font-mono text-ink-2">{r.observed}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {cal.note && <p className="mt-1 text-label text-ink-3">{cal.note}</p>}
        </div>
      )}

      <div>
        <h4 className="text-label uppercase tracking-[0.06em] text-ink-3">Every resolved forecast</h4>
        <ol className="mt-2 border-t border-line" data-testid="track-record-resolved">
          {resolved.map((r) => (
            <li key={r.id} className="border-b border-line py-2" data-testid="track-record-resolved-row" data-verdict={r.verdictKind}>
              <p className="font-serif text-body-lg text-ink-1">{r.question}</p>
              <p className="mt-1 text-label text-ink-2">
                <span className="text-ink-1">Outcome: {r.outcome}</span> · {r.who} said <span className="font-mono">{r.p}</span> ·{' '}
                <span className="text-ink-1" data-testid="track-record-verdict">
                  {r.verdict}
                </span>{' '}
                · resolved <span className="font-mono">{r.resolvedOn}</span>
              </p>
              {r.briers.length > 0 && (
                <p className="mt-0.5 font-mono text-label text-ink-3">{r.briers.map((b) => `${b.who} ${b.brier}`).join(' · ')}</p>
              )}
            </li>
          ))}
        </ol>
      </div>
    </div>
  )
}
