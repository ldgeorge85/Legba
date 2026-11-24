/**
 * W11 C-2b-2/facts — how a consult write on the FACT record class renders in
 * the armed turn's block (`ConsultWrites.tsx`), from the ledger row alone.
 *
 * A fact write has no registry head: its "version" is a digest of the pair's
 * open set, which means nothing to the operator. So a fact row reads as the
 * triple and what it closes:
 *
 *   * pending (Tier B — it closes more than one open row):
 *     *Assert fact Iran · leader of = "Jane Roe" (operator, confidence 1.00) ·
 *     would close 2: 1a2b3c4d "Old A" (ingestion); 5e6f7a8b "Old B" (agent)*,
 *     with one line per row it would close under the card;
 *   * receipt: *Written · fact iran | leader of asserted "Jane Roe" (operator) ·
 *     closed 1 · 2 open → 2 open · core · $0.00 · 14:21Z · read on the next query*;
 *   * close: *Written · fact iran | leader of closed 1a2b3c4d "Old A" (ingestion) · …*.
 *
 * Absence renders as absence: a confidence, a cost or a closed row's value the
 * row does not carry prints `not recorded` / `cost not measured`, never a 0.
 */
import { clockZ, type ConsultWriteRow } from '@/lib/consultWrites'

interface FactBrief {
  id: string
  value?: string | null
  source_type?: string | null
}

interface FactArgs {
  subject?: string
  predicate?: string
  value?: string
  confidence?: number | null
  supersede?: string | string[]
  fact_id?: string
}

interface FactPre {
  version?: string | null
  state?: string | null
  subject?: string
  predicate?: string
  open?: FactBrief[]
  closes?: string[]
  fact?: FactBrief
}

/** Is this ledger row a write on the fact class? */
export function isFactWrite(row: Pick<ConsultWriteRow, 'class'>): boolean {
  return row.class === 'fact'
}

function argsOf(row: ConsultWriteRow): FactArgs {
  return (row.args ?? {}) as unknown as FactArgs
}

function preOf(row: ConsultWriteRow): FactPre {
  return (row.pre_image ?? {}) as unknown as FactPre
}

function id8(id: string): string {
  return id.slice(0, 8)
}

/** The rows a fact write closes (or would close), with their value and author where the pre-image has them. */
export function factCloses(row: ConsultWriteRow): FactBrief[] {
  const pre = preOf(row)
  const known = new Map<string, FactBrief>()
  for (const b of pre.open ?? []) known.set(b.id, b)
  if (pre.fact) known.set(pre.fact.id, pre.fact)
  return (pre.closes ?? []).map((id) => known.get(id) ?? { id })
}

function briefText(b: FactBrief): string {
  const value = b.value != null ? `"${b.value}"` : 'value not recorded'
  return `${id8(b.id)} ${value} (${b.source_type ?? 'author not recorded'})`
}

function confidenceText(c: number | null | undefined): string {
  return typeof c === 'number' ? `confidence ${c.toFixed(2)}` : 'confidence not recorded'
}

function tripleText(row: ConsultWriteRow): string {
  const a = argsOf(row)
  const pre = preOf(row)
  const subject = a.subject ?? pre.subject ?? '—'
  const predicate = a.predicate ?? pre.predicate ?? '—'
  return `${subject} · ${predicate}`
}

/** The amber card's sentence for a staged fact write. */
export function factPendingText(row: ConsultWriteRow): string {
  const a = argsOf(row)
  const closes = factCloses(row)
  if (row.verb === 'close') {
    const target = closes[0] ?? (a.fact_id ? { id: a.fact_id } : null)
    return `Close fact on ${tripleText(row)}${target ? `: ${briefText(target)}` : ''}`
  }
  const head = `Assert fact ${tripleText(row)} = "${a.value ?? '—'}" (operator, ${confidenceText(a.confidence)})`
  const tail = closes.length
    ? ` · would close ${closes.length}: ${closes.map(briefText).join('; ')}`
    : ' · closes nothing'
  return head + tail
}

/** The receipt line of an executed fact write (a revert reads as reopening). */
export function factReceiptText(row: ConsultWriteRow): string {
  const a = argsOf(row)
  const r = row.receipt ?? {}
  const pre = (r.pre ?? row.pre_image) as FactPre | null
  const post = r.post ?? row.post_image
  const closes = factCloses(row)
  let what: string
  if (row.verb === 'assert') {
    what = `asserted "${a.value ?? '—'}" (operator) · closed ${closes.length}`
  } else if (row.verb === 'close') {
    what = `closed ${closes[0] ? briefText(closes[0]) : 'one fact'}`
  } else {
    what = 'reverted'
  }
  const move = `${pre?.state ?? 'open set not recorded'} → ${post?.state ?? 'not recorded'}`
  const cost =
    typeof r.turn_cost_usd_at_write === 'number' ? `$${r.turn_cost_usd_at_write.toFixed(2)}` : 'cost not measured'
  return [
    `Written · fact ${row.object_ref} ${what}`,
    move,
    r.plane ?? 'plane not recorded',
    cost,
    clockZ(row.executed_at),
    'read on the next query',
  ].join(' · ')
}

/** One line per row a pending fact write would close, under its card. */
export function FactClosesList({ row }: { row: ConsultWriteRow }) {
  const closes = factCloses(row)
  if (!isFactWrite(row) || closes.length === 0) return null
  return (
    <ul className="w-full space-y-0.5 pl-3 font-mono text-label text-amber-300/90" data-testid="consult-write-fact-closes">
      {closes.map((b) => (
        <li key={b.id}>closes {briefText(b)}</li>
      ))}
    </ul>
  )
}
