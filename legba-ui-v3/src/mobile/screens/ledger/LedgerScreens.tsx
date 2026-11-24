/**
 * Ledger on the phone (M-3c; plan §2.3 row "Ledger").
 *
 *   ROOT   U-11's views, in the design note's order: the forecasts (open by
 *          due date, then resolved — `view:forecasts`, as-is), then the track
 *          record (`view:track_record`) behind its five-resolution gate: below
 *          five, the count toward five and no score. Then one row,
 *          "Evaluations", that pushes the evaluation record.
 *   PROOF  `view:proof` as a page.
 *
 * The narrow variant of the two tables (the track record's 3–4 columns, the
 * evaluation record's 5) is THIS wrapper (`.ph-tables` in `phone.css`): a
 * table that does not fit scrolls in its own box with its first column held,
 * so the page never scrolls sideways. The desktop's layout code is unchanged.
 * Not on the phone: the resolution queue (the Gate's, operator side).
 */
import { PhoneViewHost } from '@/mobile/shell/PhoneViewHost'
import { navigate } from '@/mobile/shell/phoneAddress'
import { AskAboutThis } from '@/mobile/shell/ui'

export function LedgerScreen() {
  return (
    <div className="ph-screen ph-ledger" data-testid="phone-ledger">
      <div className="ph-view">
        <PhoneViewHost view="view:forecasts" />
      </div>
      <div className="ph-view ph-tables">
        <PhoneViewHost view="view:track_record" />
      </div>
      <ul className="ph-list">
        <li>
          <button
            type="button"
            className="ph-row ph-row-nav"
            data-testid="phone-ledger-proof"
            onClick={() => navigate({ tab: 'ledger', page: { kind: 'proof' }, record: null, peek: false }, 'push')}
          >
            <span className="ph-row-head">Evaluations</span>
            <span className="ph-row-meta">Standing tests of how well this system answers</span>
          </button>
        </li>
      </ul>
    </div>
  )
}

export function ProofPage() {
  return (
    <div className="ph-screen ph-ledger" data-testid="phone-proof">
      <div className="ph-view ph-tables">
        <PhoneViewHost view="view:proof" />
      </div>
      <AskAboutThis label="the evaluation record" />
    </div>
  )
}
