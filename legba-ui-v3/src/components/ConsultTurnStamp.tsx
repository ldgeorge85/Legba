import {
  usageLabel,
  verificationLabel,
  type ConsultUsageReceipt,
  type ConsultVerification,
} from '@/lib/consultTurnStamp'

const TONE: Record<'judged' | 'unassessable' | 'not_judged', string> = {
  judged: 'text-emerald-400',
  unassessable: 'text-amber-400',
  not_judged: 'text-ink-3',
}

/**
 * X2 / C1 — the consult answer's turn-header cell: what the turn used and
 * whether its answer was judged. The judged label is always present — a
 * score, `unassessable · why`, or `not judged · why` — never blank.
 */
export function ConsultTurnStamp({
  usage,
  verification,
}: {
  usage: ConsultUsageReceipt | null | undefined
  verification: ConsultVerification | null | undefined
}) {
  const v = verificationLabel(verification)
  const u = usageLabel(usage ?? null)
  return (
    <>
      <span
        className={'font-mono text-label ' + TONE[v.tone]}
        data-testid="consult-turn-verification"
        data-state={v.tone}
        title={v.title}
      >
        {v.text}
      </span>
      <span
        className="font-mono text-label text-ink-3"
        data-testid="consult-turn-usage"
        title={u.title}
      >
        {u.text}
      </span>
    </>
  )
}
