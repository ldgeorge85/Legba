/**
 * Acknowledge and snooze an alert, on its phone page (M-3b over S-2's routes,
 * `registry/alert_ack_api.py`):
 *
 *   GET    /v3/system/alerts/{id}/attention   the state and its history (reader)
 *   POST   /v3/system/alerts/{id}/ack         {note?}                (contributor+)
 *   DELETE /v3/system/alerts/{id}/ack
 *   POST   /v3/system/alerts/{id}/snooze      {duration: 1h|4h|24h|until_closed}
 *   DELETE /v3/system/alerts/{id}/snooze
 *
 * WHO: a contributor, an admin or the owner (with sign-in off, the shared
 * access, which the server treats as the operator); a user sees the state
 * and who acted, never a button. A service credential never acts.
 *
 * WHAT IT MEANS, said on the page in the server's own words: the act is
 * SHARED (it takes the alert out of "needs you" for everyone) and paging is
 * unchanged. A server without the routes answers 404: the page says acting
 * on an alert is not on this server, and shows no button.
 */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, apiDelete, apiGet, apiPost } from '@/lib/api'
import { useIdentity } from '@/auth/SignInGate'
import { whenText } from '@/v4/watch/model'
import { Quiet } from '@/mobile/shell/ui'

export interface AttentionState {
  acked_at?: string | null
  acked_by?: string | null
  ack_note?: string | null
  ack_active: boolean
  snoozed_at?: string | null
  snoozed_by?: string | null
  snooze_option?: string | null
  snoozed_until?: string | null
  snooze_active: boolean
  resurfaced: boolean
  hidden_from_needs_you: boolean
}

export interface AttentionEvent {
  id: string
  at: string
  action: 'acknowledged' | 'unacknowledged' | 'snoozed' | 'unsnoozed'
  actor: string
  actor_kind: string
  actor_role: string
  note?: string | null
  snooze_option?: string | null
  snoozed_until?: string | null
}

export interface AttentionOut {
  episode_id: string
  state: 'open' | 'closed'
  changed?: boolean
  attention: AttentionState
  history: AttentionEvent[]
  shared_note: string
  paging_note: string
  snooze_options: string[]
}

export const attentionPath = (id: string) => `/v3/system/alerts/${encodeURIComponent(id)}/attention`
export const attentionKey = (id: string) => ['phone', 'alert-attention', id] as const

const SNOOZE_WORDS: Record<string, string> = { '1h': '1 hour', '4h': '4 hours', '24h': '24 hours', until_closed: 'Until it closes' }
const ACTION_WORDS: Record<AttentionEvent['action'], string> = {
  acknowledged: 'acknowledged',
  unacknowledged: 'withdrew the acknowledgement',
  snoozed: 'snoozed',
  unsnoozed: 'ended the snooze',
}

/** Who may act: contributor and above; with sign-in off the shared access (operator). Never a service. */
export function canActOnAlerts(identity: { kind: string; role: string } | null | undefined): boolean {
  if (!identity || identity.kind === 'service') return false
  return identity.role === 'contributor' || identity.role === 'admin' || identity.role === 'operator'
}

function actorWords(e: AttentionEvent): string {
  if (e.actor_kind === 'user') return `a ${e.actor_role === 'admin' ? 'admin' : e.actor_role}`
  return 'the shared access'
}

export function AlertActions({ id }: { id: string }) {
  const qc = useQueryClient()
  const who = useIdentity()
  const identity = who.data?.state === 'signed_in' ? who.data.identity : null
  const can = canActOnAlerts(identity)
  const q = useQuery<AttentionOut>({
    queryKey: attentionKey(id),
    queryFn: () => apiGet<AttentionOut>(attentionPath(id)),
    retry: (n, e) => !(e instanceof ApiError && e.status === 404) && n < 1,
    staleTime: 30_000,
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const act = async (fn: () => Promise<AttentionOut>) => {
    setBusy(true)
    setError(null)
    try {
      const out = await fn()
      qc.setQueryData(attentionKey(id), out)
      // "needs you" and its badge follow the act
      void qc.invalidateQueries({ queryKey: ['phone', 'changed'] })
    } catch (e) {
      setError(e instanceof ApiError ? (e.status === 403 ? 'You may not act on this alert.' : `The server refused it (${e.status}).`) : 'It could not be sent.')
    } finally {
      setBusy(false)
    }
  }

  if (q.isPending) return <Quiet>Reading who has acted on this alert…</Quiet>
  if (q.isError) {
    const missing = q.error instanceof ApiError && q.error.status === 404
    return (
      <Quiet testId="phone-alert-no-ack">
        {missing
          ? 'Acknowledging or snoozing an alert is not recorded on this server. The alert keeps its state until its condition clears.'
          : 'Who has acted on this alert could not be read.'}
      </Quiet>
    )
  }
  const a = q.data.attention
  const base = `/v3/system/alerts/${encodeURIComponent(id)}`
  const open = q.data.state === 'open'
  return (
    <section className="ph-attention" data-testid="phone-alert-attention" data-acked={a.ack_active ? '1' : '0'} data-snoozed={a.snooze_active ? '1' : '0'}>
      <p className="ph-body" data-testid="phone-alert-attention-state">
        {a.ack_active
          ? `Acknowledged ${whenText(a.acked_at)}${a.ack_note ? ` — “${a.ack_note}”` : ''}.`
          : a.snooze_active
            ? `Snoozed ${a.snoozed_until ? `until ${whenText(a.snoozed_until)}` : 'until it closes'}.`
            : a.resurfaced
              ? 'It paged again above where it stood when someone acted, so it needs you again.'
              : 'No one has acknowledged or snoozed it.'}
      </p>
      {can && open ? (
        <div className="ph-attention-actions">
          {a.ack_active ? (
            <button type="button" className="ph-btn" disabled={busy} data-testid="phone-alert-unack" onClick={() => act(() => apiDelete<AttentionOut>(`${base}/ack`))}>
              Withdraw acknowledgement
            </button>
          ) : (
            <button type="button" className="ph-btn ph-btn-act" disabled={busy} data-testid="phone-alert-ack" onClick={() => act(() => apiPost<AttentionOut>(`${base}/ack`, {}))}>
              Acknowledge
            </button>
          )}
          {a.snooze_active ? (
            <button type="button" className="ph-btn" disabled={busy} data-testid="phone-alert-unsnooze" onClick={() => act(() => apiDelete<AttentionOut>(`${base}/snooze`))}>
              End the snooze
            </button>
          ) : (
            <div className="ph-snooze" role="group" aria-label="Snooze for">
              <span className="ph-dim">Snooze for</span>
              {(q.data.snooze_options?.length ? q.data.snooze_options : Object.keys(SNOOZE_WORDS)).map((o) => (
                <button key={o} type="button" className="ph-chip" disabled={busy} data-testid={`phone-alert-snooze-${o}`} onClick={() => act(() => apiPost<AttentionOut>(`${base}/snooze`, { duration: o }))}>
                  {SNOOZE_WORDS[o] ?? o}
                </button>
              ))}
            </div>
          )}
          <p className="ph-fine" data-testid="phone-alert-shared">
            {q.data.shared_note} {q.data.paging_note}
          </p>
        </div>
      ) : open ? (
        // open, and the viewer may not act (a reader): say who can
        <Quiet testId="phone-alert-reader">A contributor or an admin can acknowledge or snooze an alert.</Quiet>
      ) : (
        // Roll 87 (360 px): a closed episode takes no act — say so, rather
        // than leave the page without its controls and without a reason.
        <Quiet testId="phone-alert-closed">This alert has closed, so there is nothing to acknowledge or snooze.</Quiet>
      )}
      {error ? <p className="ph-error" role="alert">{error}</p> : null}
      {q.data.history.length > 0 ? (
        <ul className="ph-history" data-testid="phone-alert-history">
          {q.data.history.slice(-5).reverse().map((e) => (
            <li key={e.id}>
              {whenText(e.at)} · {actorWords(e)} {ACTION_WORDS[e.action]}
              {e.action === 'snoozed' && e.snooze_option ? ` for ${(SNOOZE_WORDS[e.snooze_option] ?? e.snooze_option).toLowerCase()}` : ''}
              {e.note ? ` — “${e.note}”` : ''}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  )
}
