/**
 * C-2b-1 — what an ARMED consult turn changed, rendered from the ledger
 * (design C-2a §9). One block per armed turn, under its answer:
 *
 *   * a "changes allowed" stamp (the turn was armed — `writes` is a list);
 *   * an AMBER PENDING CARD per staged write: the sentence, Confirm, Cancel,
 *     the typed-code hint (`type confirm XXXX`) while the registry holds the
 *     code, and the expiry;
 *   * a RECEIPT LINE per executed write with Revert (none when the write
 *     cannot be undone), and the receipt STRUCK THROUGH once reversed;
 *   * a "Not written · cause" line per refusal, expiry or cancellation.
 *
 * Every line comes from a `consult_writes` row, not from the answer's prose;
 * the gate's own refusals (which never reached the ledger) come from the
 * turn's bag entries. Confirm / Cancel / Revert call the executor directly —
 * no model runs.
 */
import { useCallback, useEffect, useState } from 'react'

import {
  cancelConsultWrite,
  clockZ,
  confirmConsultWrite,
  expiryLabel,
  listConsultWrites,
  pendingCardText,
  receiptText,
  refusalText,
  revertConsultWrite,
  writeErrorText,
  type ConsultWriteEntry,
  type ConsultWriteRow,
} from '@/lib/consultWrites'
import { entityPendingText, entityReceiptText, isEntityWrite } from './consultWritesEntity'
import { FactClosesList, factPendingText, factReceiptText, isFactWrite } from './ConsultWritesFact'

interface Props {
  sessionId: string | null
  requestId: string | null
  /** The turn's bag entries; `null`/absent = the turn was not armed (renders nothing). */
  entries: ConsultWriteEntry[] | null | undefined
  compact?: boolean
}

export function ConsultWritesBlock({ sessionId, requestId, entries, compact = false }: Props) {
  const [rows, setRows] = useState<ConsultWriteRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!sessionId) return
    try {
      setRows(await listConsultWrites({ sessionId }))
      setError(null)
    } catch (err) {
      setError(writeErrorText(err))
    }
  }, [sessionId])

  const armed = Array.isArray(entries)
  useEffect(() => {
    if (armed && entries.some((e) => e.write_id)) void load()
  }, [armed, entries, load])

  if (!armed) return null

  const all = rows ?? []
  const mine = all.filter((r) => r.request_id && r.request_id === requestId)
  const ledgered = new Set(mine.map((r) => r.id))
  const unledgered = entries.filter((e) => !e.write_id || (rows !== null && !ledgered.has(e.write_id)))

  const act = async (id: string, fn: (id: string, s?: string | null) => Promise<unknown>) => {
    setBusyId(id)
    try {
      await fn(id, sessionId)
    } catch (err) {
      setError(writeErrorText(err))
    } finally {
      setBusyId(null)
      await load()
    }
  }

  return (
    <div className="space-y-1 text-label" data-testid="consult-writes">
      <span
        className="inline-block rounded border border-amber-500/40 px-1.5 py-0.5 font-mono text-label text-amber-300"
        data-testid="consult-writes-armed"
        title="the operator allowed changes on this turn"
      >
        changes allowed
      </span>
      {mine.map((row) => {
        if (row.status === 'pending_confirmation') {
          return (
            <div
              key={row.id}
              className="flex flex-wrap items-center gap-2 rounded border border-amber-500/50 bg-amber-500/10 px-2 py-1 text-amber-200"
              data-testid="consult-write-pending"
            >
              <span>
                {isEntityWrite(row) ? entityPendingText(row) : isFactWrite(row) ? factPendingText(row) : pendingCardText(row)}
              </span>
              <button
                type="button"
                onClick={() => void act(row.id, confirmConsultWrite)}
                disabled={busyId !== null}
                className="rounded border border-amber-400/60 bg-amber-500/20 px-2 py-0.5 text-amber-100 hover:bg-amber-500/30 disabled:opacity-50"
                data-testid="consult-write-confirm"
              >
                Confirm
              </button>
              <button
                type="button"
                onClick={() => void act(row.id, cancelConsultWrite)}
                disabled={busyId !== null}
                className="rounded border border-line px-2 py-0.5 text-ink-2 hover:text-ink-1 disabled:opacity-50"
                data-testid="consult-write-cancel"
              >
                Cancel
              </button>
              <span className="font-mono text-amber-300/80" data-testid="consult-write-code">
                {row.confirm_code
                  ? `or type confirm ${row.confirm_code}`
                  : 'typed code not available here — use Confirm'}
              </span>
              <span className="font-mono text-amber-300/80">· {expiryLabel(row)}</span>
              <FactClosesList row={row} />
            </div>
          )
        }
        if (row.status === 'executed') {
          const reversal = row.reversed_by_write_id
            ? all.find((r) => r.id === row.reversed_by_write_id) ?? null
            : null
          if (row.reversed_by_write_id) {
            return (
              <div key={row.id} className="text-ink-3" data-testid="consult-write-reversed">
                <span className="line-through">
                  {isEntityWrite(row) ? entityReceiptText(row) : isFactWrite(row) ? factReceiptText(row) : receiptText(row)}
                </span>{' '}
                <span className="font-mono">
                  Reverted {clockZ(reversal?.executed_at)} by w-{row.reversed_by_write_id.slice(0, 8)}
                  {reversal?.turn_index != null ? ` (your turn ${reversal.turn_index + 1})` : ''}
                </span>
              </div>
            )
          }
          const undoable = row.verb !== 'revert' && row.reversal?.kind !== 'none'
          return (
            <div key={row.id} className="flex flex-wrap items-center gap-2 text-emerald-300" data-testid="consult-write-receipt">
              <span className="font-mono">
                {isEntityWrite(row) ? entityReceiptText(row) : isFactWrite(row) ? factReceiptText(row) : receiptText(row)}
              </span>
              {undoable ? (
                <button
                  type="button"
                  onClick={() => void act(row.id, revertConsultWrite)}
                  disabled={busyId !== null}
                  className="rounded border border-line px-2 py-0.5 text-ink-2 hover:text-ink-1 disabled:opacity-50"
                  data-testid="consult-write-revert"
                  title="undo this change now, from its stored pre-image — no model runs"
                >
                  Revert
                </button>
              ) : (
                <span className="text-ink-3">
                  {row.verb === 'revert' ? '' : `cannot be undone${row.reversal?.why ? ` (${row.reversal.why})` : ''}`}
                </span>
              )}
              {!compact && <span className="font-mono text-ink-3">w-{row.id.slice(0, 8)}</span>}
            </div>
          )
        }
        return (
          <div key={row.id} className="font-mono text-ink-3" data-testid="consult-write-refused">
            {refusalText(row)}
          </div>
        )
      })}
      {unledgered.map((e, i) => (
        <div key={`gate-${i}`} className="font-mono text-ink-3" data-testid="consult-write-refused">
          Not written · {e.cause ? `${e.cause}: ` : ''}
          {e.summary ?? e.status}
        </div>
      ))}
      {error && (
        <div className="text-accent-critical" data-testid="consult-writes-error">
          ledger unavailable — {error}
        </div>
      )}
    </div>
  )
}
