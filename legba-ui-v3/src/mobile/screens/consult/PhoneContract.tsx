/**
 * The turn contract under a phone answer (M-3g; plan §2.3 "Consult").
 *
 * The desktop draws the whole contract (`components/ConsultContract`) under
 * every answer. At 360 px that block is taller than most answers, so on the
 * phone it starts as ONE line — the window the answer looked at, how much of
 * it held records, and "Partial answer" when the turn stopped for cost — and
 * opens to the shared block, unchanged, on a tap.
 */
import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { ConsultContractBlock } from '@/components/ConsultContract'
import type { ContractCoverage, ContractWindow, TurnContract } from '@/lib/consultContract'
import type { ConsultUsageReceipt } from '@/lib/consultTurnStamp'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function day(iso: string): string {
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}` : iso
}

/** "7 Nov – 14 Nov", and where the window came from when it was the question. */
export function windowSummary(w: ContractWindow | null): string | null {
  if (!w) return null
  const a = new Date(w.start)
  const b = new Date(w.end)
  const sameMonth =
    Number.isFinite(a.getTime()) && Number.isFinite(b.getTime()) && a.getUTCMonth() === b.getUTCMonth() && a.getUTCFullYear() === b.getUTCFullYear()
  const span =
    day(w.start) === day(w.end) ? day(w.end) : sameMonth ? `${a.getUTCDate()}–${day(w.end)}` : `${day(w.start)} – ${day(w.end)}`
  return w.source === 'user_words' ? `${span} (from your question)` : span
}

/** "6 of 7 days hold records", or that it was not measured. */
export function coverageSummary(c: ContractCoverage | null): string | null {
  if (!c) return null
  if (c.state !== 'measured') return 'coverage not measured'
  const covered = c.days.length - c.zeroDays.length
  return `records on ${covered} of ${c.days.length} day${c.days.length === 1 ? '' : 's'}`
}

/** The one line: Partial answer · window · coverage. */
export function contractSummary(contract: TurnContract): string {
  const rest = [windowSummary(contract.window), coverageSummary(contract.coverage)].filter((x): x is string => !!x).join(' · ')
  return `${contract.costStop ? 'Partial answer · ' : ''}${rest || 'What this answer looked at'}`
}

export function PhoneContract({ contract, usage }: { contract: TurnContract; usage?: ConsultUsageReceipt | null }) {
  const [open, setOpen] = useState(false)
  const parts = [contract.window, contract.coverage, contract.hypotheses, contract.grounding, contract.costStop]
  if (parts.every((p) => p == null)) return null
  const Chevron = open ? ChevronDown : ChevronRight
  return (
    <div className="ph-contract" data-testid="phone-contract" data-open={open ? '1' : '0'} data-partial={contract.costStop ? '1' : '0'}>
      <button
        type="button"
        className="ph-contract-line"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        data-testid="phone-contract-toggle"
      >
        <Chevron size={16} aria-hidden className="ph-contract-chevron" />
        <span className="ph-contract-text">
          {contract.costStop ? <span className="ph-contract-partial">Partial answer · </span> : null}
          {[windowSummary(contract.window), coverageSummary(contract.coverage)].filter(Boolean).join(' · ') || 'What this answer looked at'}
        </span>
      </button>
      {open ? (
        <div className="ph-contract-full">
          <ConsultContractBlock contract={contract} usage={usage} />
        </div>
      ) : null}
    </div>
  )
}
