/**
 * The Flow — guided wiring modal (F.D).
 *
 * A form-first "wire it up" surface for the v4 Flow room: instead of dragging
 * an edge on the canvas, the operator picks a Target and a Source from two
 * dropdowns and hits Create. The modal speaks the SAME registry REST the rest
 * of the UI uses — there is no dedicated "subscription" endpoint under the
 * source-first pivot (see `panels/source/SubscriptionBuilder.tsx`, which only
 * *composes* a SourceRef for copy-paste, and `panels/registry/Wirings.tsx`,
 * which derives the wiring read-only from descriptor bodies). A subscription
 * IS a `SourceRef` appended to a target descriptor's `sources[]`, persisted by
 * re-PUT-ing the whole target body. This mirrors exactly how
 * `panels/registry/ActionPacks.tsx` grants a pack (read body → mutate a list →
 * re-stamp identity.version → PUT it back; the registry hashes a real, audited
 * new version).
 *
 * Routes used (all on the frozen /api/v1 registry surface):
 *   subscribe :  GET /registry/descriptors/target/{id}   (read the body)
 *                PUT /registry/descriptors/target/{id}    (body.sources += ref)
 *   grant     :  GET /registry/descriptors/{family}/{id}
 *                PUT /registry/descriptors/{family}/{id}  (grant list += pack)
 *
 * Request body for the PUT is the FULL descriptor body (the same dict the
 * generic `/descriptors/{family}` POST/PUT pydantic-parse path consumes), with
 * `identity.version` set to the 16-zero sentinel so the registry re-stamps the
 * content hash on write.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { X, ArrowRight, Loader2 } from 'lucide-react'
import { apiGet, ApiError, readErrorBody } from '@/lib/api'
import { cn } from '@/lib/cn'

/* -------------------------------------------------------------------------- */
/* registry row shapes (the subset this modal reads)                          */
/* -------------------------------------------------------------------------- */

/** A head row from `GET /registry/descriptors?family=target&head_only=true`. */
interface TargetRow {
  descriptor_id: string
  name: string
  state: string
}

/** A row from `GET /registry/sources` (SourceDescriptorOut projection). */
interface SourceRow {
  descriptor_id: string
  name: string
  state: string
  kind: string | null
}

/** A row from `GET /registry/action_packs` (ActionPackOut projection). */
interface ActionPackRow {
  descriptor_id: string
  name: string
  state: string
}

/** Full descriptor row (`GET /registry/descriptors/{family}/{id}`). */
interface DescriptorRow {
  descriptor_id: string
  version: string
  state: string
  body: Record<string, unknown>
}

const VERSION_SENTINEL = '0'.repeat(16)

/** The grant-list field per grant target family (mirrors ActionPacks.tsx). */
const GRANT_FIELD = {
  analyst: 'action_packs',
  target: 'allowed_action_packs',
} as const

type GrantFamily = keyof typeof GRANT_FIELD

type Mode = 'subscribe' | 'grant'

/* -------------------------------------------------------------------------- */
/* helpers                                                                    */
/* -------------------------------------------------------------------------- */

/** Re-stamp `identity.version` with the sentinel so the registry hashes it
 *  (the same convention DescriptorEditor / ActionPacks use on every write). */
function ensureSentinelVersion(body: Record<string, unknown>): Record<string, unknown> {
  const out = { ...body }
  const identity = (out.identity as Record<string, unknown> | undefined) ?? {}
  out.identity = { ...identity, version: VERSION_SENTINEL }
  return out
}

/**
 * PUT a full descriptor body back to the registry. `apiPost` only does POST,
 * and updates need PUT — same thin wrapper ActionPacks/DescriptorEditor use.
 */
async function putDescriptor(
  family: string,
  id: string,
  body: Record<string, unknown>,
): Promise<{ version: string }> {
  const token = localStorage.getItem('legba_token')
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
  const res = await fetch(`/api/v1/registry/descriptors/${family}/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers,
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    throw new ApiError(res.status, await readErrorBody(res))
  }
  return res.json() as Promise<{ version: string }>
}

/** Turn an ApiError / Error into a single human line for inline display. */
function errMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail
      if (typeof detail === 'string') return detail
      if (detail && typeof detail === 'object' && 'message' in detail) {
        return String((detail as { message: unknown }).message)
      }
      return JSON.stringify(detail)
    }
    return typeof body === 'string' && body ? body : `HTTP ${e.status}`
  }
  return e instanceof Error ? e.message : String(e)
}

/** Read a target's existing SourceRefs as a list (defensive against shape). */
function readSourceRefs(body: Record<string, unknown> | undefined): Array<Record<string, unknown>> {
  const raw = body?.sources
  if (!Array.isArray(raw)) return []
  return raw.filter((r): r is Record<string, unknown> => !!r && typeof r === 'object')
}

/** Read a grant list (`{pack_id}` refs) off a body field. */
function readGrantList(
  body: Record<string, unknown> | undefined,
  field: string,
): Array<Record<string, unknown>> {
  const raw = body?.[field]
  if (!Array.isArray(raw)) return []
  return raw.filter((r): r is Record<string, unknown> => !!r && typeof r === 'object')
}

/* -------------------------------------------------------------------------- */
/* component                                                                  */
/* -------------------------------------------------------------------------- */

interface WiringModalProps {
  open: boolean
  onClose: () => void
  initialTargetId?: string
  initialSourceId?: string
}

export default function WiringModal({
  open,
  onClose,
  initialTargetId,
  initialSourceId,
}: WiringModalProps) {
  const qc = useQueryClient()

  const [mode, setMode] = useState<Mode>('subscribe')
  const [targetId, setTargetId] = useState(initialTargetId ?? '')
  const [sourceId, setSourceId] = useState(initialSourceId ?? '')
  // grant mode: which family the pack is granted to (+ which descriptor / pack)
  const [grantFamily, setGrantFamily] = useState<GrantFamily>('target')
  const [grantScopeId, setGrantScopeId] = useState(initialTargetId ?? '')
  const [packId, setPackId] = useState('')

  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const closeRef = useRef(onClose)
  closeRef.current = onClose

  // Reset the form whenever the modal (re)opens, honouring the initial ids.
  useEffect(() => {
    if (!open) return
    setMode('subscribe')
    setTargetId(initialTargetId ?? '')
    setSourceId(initialSourceId ?? '')
    setGrantFamily('target')
    setGrantScopeId(initialTargetId ?? '')
    setPackId('')
    setPending(false)
    setError(null)
  }, [open, initialTargetId, initialSourceId])

  // Esc to dismiss (only while open + not mid-flight).
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeRef.current()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // ---- dropdown option sources (only fetched while open) -------------------
  const targetsQ = useQuery<TargetRow[]>({
    enabled: open,
    queryKey: ['wiring-modal', 'targets'],
    queryFn: () =>
      apiGet<TargetRow[]>('/registry/descriptors?family=target&head_only=true&limit=500'),
  })
  const sourcesQ = useQuery<SourceRow[]>({
    enabled: open,
    queryKey: ['wiring-modal', 'sources'],
    queryFn: () => apiGet<SourceRow[]>('/registry/sources?head_only=true&limit=500'),
  })
  const analystsQ = useQuery<TargetRow[]>({
    enabled: open && mode === 'grant',
    queryKey: ['wiring-modal', 'analysts'],
    queryFn: () =>
      apiGet<TargetRow[]>('/registry/descriptors?family=analyst&head_only=true&limit=500'),
  })
  const packsQ = useQuery<ActionPackRow[]>({
    enabled: open && mode === 'grant',
    queryKey: ['wiring-modal', 'action_packs'],
    queryFn: () => apiGet<ActionPackRow[]>('/registry/action_packs?head_only=true&limit=500'),
  })

  const targets = useMemo(
    () => (targetsQ.data ?? []).filter((t) => t.state !== 'retired'),
    [targetsQ.data],
  )
  const sources = useMemo(
    () => (sourcesQ.data ?? []).filter((s) => s.state !== 'retired'),
    [sourcesQ.data],
  )
  const analysts = useMemo(
    () => (analystsQ.data ?? []).filter((a) => a.state !== 'retired'),
    [analystsQ.data],
  )
  const packs = useMemo(
    () => (packsQ.data ?? []).filter((p) => p.state !== 'retired'),
    [packsQ.data],
  )

  // grant mode picks scope rows from the chosen family
  const grantScopeRows = grantFamily === 'analyst' ? analysts : targets

  const canSubmit =
    !pending &&
    (mode === 'subscribe'
      ? !!targetId && !!sourceId
      : !!grantScopeId && !!packId)

  // ---- preview line --------------------------------------------------------
  const targetName = targets.find((t) => t.descriptor_id === targetId)?.name ?? targetId
  const sourceName = sources.find((s) => s.descriptor_id === sourceId)?.name ?? sourceId
  const grantScopeName =
    grantScopeRows.find((r) => r.descriptor_id === grantScopeId)?.name ?? grantScopeId
  const packName = packs.find((p) => p.descriptor_id === packId)?.name ?? packId

  // ---- submit --------------------------------------------------------------
  async function submit() {
    if (!canSubmit) return
    setPending(true)
    setError(null)
    try {
      if (mode === 'subscribe') {
        // 1. read the current target body
        const row = await apiGet<DescriptorRow>(
          `/registry/descriptors/target/${encodeURIComponent(targetId)}`,
        )
        const refs = readSourceRefs(row.body)
        // idempotent: don't double-wire an explicit source_id
        const already = refs.some((r) => r.source_id === sourceId)
        if (already) {
          throw new Error(`target already subscribes to source "${sourceId}"`)
        }
        // 2. append the SourceRef (explicit source_id, the SourceRef invariant:
        //    exactly one of source_id / source_selector)
        const nextRefs = [...refs, { source_id: sourceId }]
        const body = ensureSentinelVersion({ ...row.body, sources: nextRefs })
        // 3. PUT the whole body back — registry re-stamps the content hash
        await putDescriptor('target', targetId, body)
      } else {
        const field = GRANT_FIELD[grantFamily]
        const row = await apiGet<DescriptorRow>(
          `/registry/descriptors/${grantFamily}/${encodeURIComponent(grantScopeId)}`,
        )
        const current = readGrantList(row.body, field)
        const already = current.some((r) => r.pack_id === packId)
        if (already) {
          throw new Error(`${grantFamily} "${grantScopeId}" already has pack "${packId}"`)
        }
        const next = [...current, { pack_id: packId }]
        const body = ensureSentinelVersion({ ...row.body, [field]: next })
        await putDescriptor(grantFamily, grantScopeId, body)
      }

      // refresh the registry-backed queries the rest of the UI shares
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['registry/descriptors'] }),
        qc.invalidateQueries({ queryKey: ['registry/sources'] }),
        qc.invalidateQueries({ queryKey: ['wiring-modal'] }),
        // the Flow projection + Wirings panel read these keys
        qc.invalidateQueries({ queryKey: ['registry-wiring'] }),
        qc.invalidateQueries({ queryKey: ['flow', 'projection'] }),
      ])
      onClose()
    } catch (e) {
      setError(errMessage(e))
    } finally {
      setPending(false)
    }
  }

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Wire a subscription or grant"
      onMouseDown={(e) => {
        // backdrop click (not a click that started inside the card) dismisses
        if (e.target === e.currentTarget && !pending) onClose()
      }}
      data-testid="wiring-modal"
    >
      <div className="w-[520px] max-w-full rounded-lg border border-slate-800 bg-surface-100 shadow-2xl">
        {/* header */}
        <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-200">Wire it up</h2>
          <button
            type="button"
            onClick={() => !pending && onClose()}
            disabled={pending}
            className="text-slate-500 hover:text-slate-200 disabled:opacity-50"
            aria-label="Close"
            data-testid="wiring-modal-close"
          >
            <X size={16} />
          </button>
        </div>

        <div className="space-y-4 px-4 py-4 text-xs">
          {/* mode toggle */}
          <div className="flex gap-1" data-testid="wiring-modal-mode">
            {(['subscribe', 'grant'] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => {
                  setMode(m)
                  setError(null)
                }}
                disabled={pending}
                className={cn(
                  'rounded px-3 py-1 text-[11px] capitalize disabled:opacity-50',
                  mode === m
                    ? 'bg-surface-50 text-slate-200 border border-slate-700'
                    : 'bg-surface-200 text-slate-500 hover:text-slate-300',
                )}
                data-testid={`wiring-modal-mode-${m}`}
              >
                {m === 'subscribe' ? 'Subscribe to source' : 'Grant action-pack'}
              </button>
            ))}
          </div>

          {mode === 'subscribe' ? (
            <>
              <Field label="Target">
                <Select
                  value={targetId}
                  onChange={setTargetId}
                  disabled={pending || targetsQ.isLoading}
                  placeholder={targetsQ.isLoading ? 'loading targets…' : 'select a target…'}
                  options={targets.map((t) => ({
                    value: t.descriptor_id,
                    label: t.name ? `${t.name} · ${t.descriptor_id}` : t.descriptor_id,
                  }))}
                  testId="wiring-modal-target"
                />
              </Field>

              <Field label="Source">
                <Select
                  value={sourceId}
                  onChange={setSourceId}
                  disabled={pending || sourcesQ.isLoading}
                  placeholder={sourcesQ.isLoading ? 'loading sources…' : 'select a source…'}
                  options={sources.map((s) => ({
                    value: s.descriptor_id,
                    label: s.kind
                      ? `${s.name || s.descriptor_id} · ${s.kind}`
                      : s.name || s.descriptor_id,
                  }))}
                  testId="wiring-modal-source"
                />
              </Field>

              <Preview testId="wiring-modal-preview">
                {targetId && sourceId ? (
                  <span>
                    subscribe <strong className="text-slate-200">{targetName}</strong>{' '}
                    <ArrowRight size={11} className="inline -mt-0.5 text-slate-500" /> source{' '}
                    <strong className="text-slate-200">{sourceName}</strong>
                  </span>
                ) : (
                  <span className="text-slate-500">pick a target and a source</span>
                )}
              </Preview>
            </>
          ) : (
            <>
              <Field label="Grant to">
                <div className="flex gap-1">
                  {(['target', 'analyst'] as const).map((fam) => (
                    <button
                      key={fam}
                      type="button"
                      onClick={() => {
                        setGrantFamily(fam)
                        setGrantScopeId('')
                        setError(null)
                      }}
                      disabled={pending}
                      className={cn(
                        'rounded px-3 py-1 text-[11px] capitalize disabled:opacity-50',
                        grantFamily === fam
                          ? 'bg-surface-50 text-slate-200 border border-slate-700'
                          : 'bg-surface-200 text-slate-500 hover:text-slate-300',
                      )}
                      data-testid={`wiring-modal-grant-family-${fam}`}
                    >
                      {fam}
                    </button>
                  ))}
                </div>
              </Field>

              <Field label={grantFamily === 'analyst' ? 'Analyst' : 'Target'}>
                <Select
                  value={grantScopeId}
                  onChange={setGrantScopeId}
                  disabled={pending || analystsQ.isLoading || targetsQ.isLoading}
                  placeholder={`select a ${grantFamily}…`}
                  options={grantScopeRows.map((r) => ({
                    value: r.descriptor_id,
                    label: r.name ? `${r.name} · ${r.descriptor_id}` : r.descriptor_id,
                  }))}
                  testId="wiring-modal-grant-scope"
                />
              </Field>

              <Field label="Action pack">
                <Select
                  value={packId}
                  onChange={setPackId}
                  disabled={pending || packsQ.isLoading}
                  placeholder={packsQ.isLoading ? 'loading packs…' : 'select a pack…'}
                  options={packs.map((p) => ({
                    value: p.descriptor_id,
                    label: p.name ? `${p.name} · ${p.descriptor_id}` : p.descriptor_id,
                  }))}
                  testId="wiring-modal-pack"
                />
              </Field>

              <Preview testId="wiring-modal-preview">
                {grantScopeId && packId ? (
                  <span>
                    grant pack <strong className="text-slate-200">{packName}</strong>{' '}
                    <ArrowRight size={11} className="inline -mt-0.5 text-slate-500" /> {grantFamily}{' '}
                    <strong className="text-slate-200">{grantScopeName}</strong>
                    <span className="text-slate-500">
                      {' '}
                      ({grantFamily === 'analyst' ? 'action_packs' : 'allowed_action_packs'})
                    </span>
                  </span>
                ) : (
                  <span className="text-slate-500">pick a {grantFamily} and a pack</span>
                )}
              </Preview>
            </>
          )}

          {error && (
            <div
              className="rounded border border-rose-800 bg-rose-900/20 px-3 py-2 text-[11px] text-rose-300"
              data-testid="wiring-modal-error"
            >
              {error}
            </div>
          )}
        </div>

        {/* footer */}
        <div className="flex items-center justify-end gap-2 border-t border-slate-800 px-4 py-3">
          <button
            type="button"
            onClick={() => !pending && onClose()}
            disabled={pending}
            className="rounded px-3 py-1.5 text-xs text-slate-400 hover:text-slate-200 disabled:opacity-50"
            data-testid="wiring-modal-cancel"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={!canSubmit}
            className={cn(
              'flex items-center gap-1.5 rounded px-3 py-1.5 text-xs font-medium',
              'bg-accent-info/20 text-sky-200 border border-accent-info/40',
              'hover:bg-accent-info/30 disabled:cursor-not-allowed disabled:opacity-40',
            )}
            data-testid="wiring-modal-create"
          >
            {pending && <Loader2 size={13} className="animate-spin" />}
            {pending ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* small presentational helpers                                               */
/* -------------------------------------------------------------------------- */

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-[10px] uppercase tracking-wide text-slate-500">{label}</span>
      {children}
    </label>
  )
}

interface SelectOption {
  value: string
  label: string
}

function Select({
  value,
  onChange,
  options,
  placeholder,
  disabled,
  testId,
}: {
  value: string
  onChange: (v: string) => void
  options: SelectOption[]
  placeholder: string
  disabled?: boolean
  testId?: string
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      className={cn(
        'w-full rounded border border-slate-800 bg-surface-200 px-2 py-1.5 text-[11px] text-slate-200',
        'focus:border-slate-600 focus:outline-none disabled:opacity-50',
      )}
      data-testid={testId}
    >
      <option value="">{placeholder}</option>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  )
}

function Preview({ children, testId }: { children: React.ReactNode; testId?: string }) {
  return (
    <div
      className="rounded border border-slate-800 bg-surface-200 px-3 py-2 text-[11px] text-slate-400"
      data-testid={testId}
    >
      {children}
    </div>
  )
}
