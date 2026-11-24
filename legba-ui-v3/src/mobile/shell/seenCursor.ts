/**
 * seenCursor — "since you last looked", per person when sign-in is on, per
 * device when it is off (M-2/M-3a; plan §4.3, S-1's `GET/PUT /v3/me/seen`).
 *
 *   sign-in ON   the server keeps one time per surface per person, so the
 *                cursor follows the person across devices; it only moves
 *                forward (the server's GREATEST).
 *   sign-in OFF  the route answers 404 `seen_cursor_unavailable` (there is no
 *                person to key on); the cursor stays on THIS device, in
 *                `localStorage` under {@link DEVICE_SEEN_KEY} — a time, never
 *                any of the record.
 *
 * The answer is asked once per page (`ME_SEEN_KEY`), and the shell says
 * which of the two is in force ("kept on this phone").
 */
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, apiGet, apiPut } from '@/lib/api'

export type SeenSurface = 'changed' | 'watch' | 'ledger' | 'consult' | 'subjects'

export const ME_SEEN_ROUTE = '/v3/me/seen'
export const ME_SEEN_KEY = ['me', 'seen'] as const
export const DEVICE_SEEN_KEY = 'legba_phone_seen'

export interface SeenAnswer {
  /** Where the cursor lives: the person's account, or this device. */
  where: 'account' | 'device'
  seen: Partial<Record<SeenSurface, string>>
  /** Why the server's cursor is not used (sign-in off, a credential with no person). */
  reason: string | null
}

function readDevice(): Partial<Record<SeenSurface, string>> {
  try {
    const raw = window.localStorage.getItem(DEVICE_SEEN_KEY)
    const v = raw ? (JSON.parse(raw) as unknown) : null
    if (!v || typeof v !== 'object') return {}
    const out: Partial<Record<SeenSurface, string>> = {}
    for (const [k, t] of Object.entries(v as Record<string, unknown>)) {
      if (typeof t === 'string' && Number.isFinite(Date.parse(t))) out[k as SeenSurface] = t
    }
    return out
  } catch {
    return {}
  }
}

function writeDevice(surface: SeenSurface, at: string): void {
  try {
    const cur = readDevice()
    const prev = cur[surface]
    if (prev && Date.parse(prev) >= Date.parse(at)) return
    window.localStorage.setItem(DEVICE_SEEN_KEY, JSON.stringify({ ...cur, [surface]: at }))
  } catch {
    // storage blocked: the cursor lasts for this page only
  }
}

function reasonOf(err: unknown): string {
  if (err instanceof ApiError) {
    const d = (err.body as { detail?: { reason?: string } } | null)?.detail
    if (d && typeof d === 'object' && typeof d.reason === 'string') return d.reason
    return `status ${err.status}`
  }
  return 'unreadable'
}

/** Read the cursor: the account's when the server keeps one, else the device's. */
export async function fetchSeen(): Promise<SeenAnswer> {
  try {
    const r = await apiGet<{ seen?: Record<string, string> }>(ME_SEEN_ROUTE)
    return { where: 'account', seen: (r?.seen ?? {}) as SeenAnswer['seen'], reason: null }
  } catch (err) {
    // 404: sign-in is off, or no person stands behind the credential —
    // the plan's rule: the cursor stays on the device.
    return { where: 'device', seen: readDevice(), reason: reasonOf(err) }
  }
}

export function useSeen() {
  return useQuery({ queryKey: ME_SEEN_KEY, queryFn: fetchSeen, staleTime: Infinity, retry: false })
}

/** Move a surface's cursor forward to `at` (an ISO instant, the server's `server_now`). */
export function useMarkSeen() {
  const qc = useQueryClient()
  return async (surface: SeenSurface, at: string, where: SeenAnswer['where']) => {
    if (where === 'account') {
      try {
        await apiPut(ME_SEEN_ROUTE, { seen: { [surface]: at } })
      } catch {
        writeDevice(surface, at)
      }
    } else {
      writeDevice(surface, at)
    }
    qc.setQueryData<SeenAnswer>(ME_SEEN_KEY, (prev) =>
      prev ? { ...prev, seen: { ...prev.seen, [surface]: at } } : { where, seen: { [surface]: at }, reason: null },
    )
  }
}

/** The words for where the cursor lives, for the Settings page and the Changed line. */
export function seenWhereWords(a: SeenAnswer | undefined): string {
  if (!a) return ''
  if (a.where === 'account') return 'kept with your account'
  return a.reason === 'signin_off' ? 'kept on this phone (sign-in is off)' : 'kept on this phone'
}
