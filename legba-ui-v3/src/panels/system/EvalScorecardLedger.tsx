/**
 * Accuracy page · section 2 — the sealed ledgers (UI-1).
 *
 * The forecast ledger is not here: its one home is the Ledger surface's
 * Forecasts region (`panels/ledger/ForecastLedger`), and this section points
 * to it in one line. The operator gold set
 * is a stage table: entries → resolved → scored → voided → awaiting a judge,
 * never pooled with the forecasts. Every stage is a count WITH the
 * denominator it is read against and its as-of (`lib/accuracyPage.ts`
 * composes the rows).
 */
import { goldsetLedger, type AccuracyInputs, type Ledger } from '@/lib/accuracyPage'
import { FigureCell, Section } from './EvalScorecardParts'

function LedgerTable({ ledger }: { ledger: Ledger }) {
  const id = ledger.id
  return (
    <div className="space-y-1" data-testid={`ledger-${id}`}>
      <h4 className="text-label text-ink-1">{ledger.title}</h4>
      <p className="text-label leading-relaxed text-ink-3">{ledger.definition}</p>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-label">
          <thead>
            <tr className="border-b border-line text-left text-label text-ink-3">
              <th className="py-0.5 pr-2 font-normal">stage</th>
              <th className="py-0.5 pr-2 font-normal">count</th>
              <th className="py-0.5 pr-2 font-normal">of</th>
              <th className="py-0.5 font-normal">source</th>
            </tr>
          </thead>
          <tbody>
            {ledger.stages.map((s) => (
              <tr key={s.id} className="border-b border-line align-baseline" data-testid={`ledger-${id}-${s.id}`}>
                <td className="py-1 pr-2 text-ink-2">
                  {s.label}
                  {s.note && (
                    <div className="text-label text-ink-3" data-testid={`ledger-${id}-${s.id}-note`}>
                      {s.note}
                    </div>
                  )}
                </td>
                <td className="py-1 pr-2">
                  <FigureCell fig={s.count} testId={`ledger-${id}-${s.id}-count`} />
                </td>
                <td className="py-1 pr-2">
                  <FigureCell fig={s.of.figure} label={`${s.of.label}:`} testId={`ledger-${id}-${s.id}-of`} />
                </td>
                <td className="py-1 font-mono text-label text-ink-3">{s.route}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {ledger.gap && (
        <p className="text-label leading-relaxed text-ink-3" data-testid={`ledger-${id}-gap`}>
          {ledger.gap}
        </p>
      )}
    </div>
  )
}

/**
 * The forecast ledger's one home is the Ledger surface's Forecasts region
 * (`panels/ledger/ForecastLedger`, beside this page); this section names it in
 * one line and carries the gold set's stages.
 */
export function LedgerSection({ inputs }: { inputs: AccuracyInputs }) {
  return (
    <Section
      n={2}
      title="Sealed ledgers"
      testId="accuracy-ledger"
      lead="The operator gold set, each stage counted against its own denominator. A voided entry is counted for coverage and kept out of every score; a stage awaiting a judge is an ordinary state of the ledger, not a fault."
    >
      <p className="text-body text-ink-2" data-testid="accuracy-forecasts-pointer">
        The forecast ledger and its scorecard are on the Ledger, under Forecasts.
      </p>
      <LedgerTable ledger={goldsetLedger(inputs)} />
    </Section>
  )
}
