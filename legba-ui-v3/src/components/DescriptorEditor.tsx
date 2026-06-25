/**
 * Reusable inline descriptor editor for the registry panels.
 *
 * Used by:
 *   - registry.targets    (family=target, descriptor CRUD)
 *   - registry.analysts   (family=analyst)
 *   - registry.stack      (kind=*, stack-component CRUD)
 *
 * Modes:
 *   - 'create' — empty YAML textarea + POST /descriptors/{family}
 *   - 'update' — pre-filled with the existing body + PUT /descriptors/{family}/{id}
 *
 * Validation errors from the registry (422 pydantic) surface inline.
 * The `identity.version` field accepts the special sentinel "0" * 16 on
 * register / update — the registry stamps the real content hash at
 * write time.
 */

import { useState, useEffect } from 'react'
import yaml from 'js-yaml'
import { apiPost, ApiError, readErrorBody } from '@/lib/api'

export type EditorMode = 'create' | 'update'

interface DescriptorEditorProps {
  /**
   * Family: 'target' / 'analyst' / 'source' for descriptor families, OR
   * 'stack' for stack components (which use a different API path
   * — `/registry/stack/{component_id}` instead of
   * `/registry/descriptors/{family}/{id}`).
   */
  family: 'target' | 'analyst' | 'source' | 'stack'
  /** Update mode only — the existing id/component_id. */
  descriptorId?: string
  /** Update mode only — the existing body to seed the editor. */
  initialBody?: Record<string, unknown>
  /** Called after a successful save with the new version hash. */
  onSaved?: (version: string) => void
  /** Called when the operator hits Cancel. */
  onCancel?: () => void
}

const VERSION_SENTINEL = '0'.repeat(16)

function ensureSentinelVersion(body: Record<string, unknown>): Record<string, unknown> {
  const out = { ...body }
  const identity = (out.identity as Record<string, unknown> | undefined) ?? {}
  if (!identity.version || /^[0a-fA-F]{0,15}$/.test(String(identity.version))) {
    out.identity = { ...identity, version: VERSION_SENTINEL }
  }
  return out
}

async function putDescriptor(
  family: string,
  id: string,
  body: Record<string, unknown>,
): Promise<{ version: string }> {
  // apiPost only does POST; we need PUT for updates.
  const token = localStorage.getItem('legba_token')
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
  const path = family === 'stack' ? `/registry/stack/${encodeURIComponent(id)}` : `/registry/descriptors/${family}/${encodeURIComponent(id)}`
  const res = await fetch(`/api/v1${path}`, {
    method: 'PUT',
    headers,
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    throw new ApiError(res.status, await readErrorBody(res))
  }
  return res.json() as Promise<{ version: string }>
}

async function postDescriptor(
  family: string,
  body: Record<string, unknown>,
): Promise<{ version: string }> {
  const path = family === 'stack' ? '/registry/stack' : `/registry/descriptors/${family}`
  return apiPost<{ version: string }>(path, body)
}

export function DescriptorEditor({
  family,
  descriptorId,
  initialBody,
  onSaved,
  onCancel,
}: DescriptorEditorProps) {
  const mode: EditorMode = descriptorId ? 'update' : 'create'
  const [yamlText, setYamlText] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmRetire, setConfirmRetire] = useState(false)

  useEffect(() => {
    if (initialBody) {
      setYamlText(yaml.dump(initialBody, { lineWidth: 100 }))
    } else {
      setYamlText('')
    }
  }, [initialBody])

  async function save() {
    setSaving(true)
    setError(null)
    try {
      let parsed: unknown
      try {
        parsed = yaml.load(yamlText)
      } catch (e) {
        throw new Error(`YAML parse error: ${(e as Error).message}`)
      }
      if (!parsed || typeof parsed !== 'object') {
        throw new Error('Descriptor body must be a YAML mapping')
      }
      const body = ensureSentinelVersion(parsed as Record<string, unknown>)
      const result =
        mode === 'update'
          ? await putDescriptor(family, descriptorId!, body)
          : await postDescriptor(family, body)
      onSaved?.(result.version)
    } catch (e) {
      if (e instanceof ApiError) {
        const detail = typeof e.body === 'object' && e.body && 'detail' in e.body
          ? (e.body as { detail: unknown }).detail
          : e.body
        setError(typeof detail === 'string' ? detail : JSON.stringify(detail, null, 2))
      } else {
        setError((e as Error).message)
      }
    } finally {
      setSaving(false)
    }
  }

  async function retire() {
    if (!descriptorId) return
    if (!confirmRetire) {
      setConfirmRetire(true)
      return
    }
    setSaving(true)
    setError(null)
    try {
      const reason = window.prompt('Retire reason (optional)') ?? ''
      await apiPost(
        family === 'stack'
          ? `/registry/stack/${encodeURIComponent(descriptorId)}/retire`
          : `/registry/descriptors/${family}/${encodeURIComponent(descriptorId)}/retire`,
        { reason },
      )
      onSaved?.('retired')
    } catch (e) {
      setError(e instanceof ApiError ? JSON.stringify(e.body, null, 2) : (e as Error).message)
    } finally {
      setSaving(false)
      setConfirmRetire(false)
    }
  }

  return (
    <div className="bg-surface-200 border border-slate-700 rounded p-2 space-y-2 mt-2">
      <div className="flex items-center justify-between">
        <div className="text-slate-400 text-[10px] uppercase tracking-wide">
          {mode === 'create' ? `new ${family} descriptor` : `edit ${family}/${descriptorId}`}
        </div>
        <div className="text-slate-600 text-[10px]">
          {mode === 'update'
            ? `identity.version will be re-stamped at save (use "${VERSION_SENTINEL}" or leave empty)`
            : 'identity.version: registry stamps content hash at write'}
        </div>
      </div>

      <textarea
        className="w-full bg-surface-100 border border-slate-800 rounded p-2 font-mono text-[11px] text-slate-200"
        rows={20}
        value={yamlText}
        onChange={(e) => setYamlText(e.target.value)}
        placeholder={mode === 'create' ? 'paste descriptor YAML here…' : ''}
        spellCheck={false}
      />

      {error && (
        <pre className="bg-rose-900/20 border border-rose-700 rounded p-2 text-[10px] text-rose-200 overflow-x-auto whitespace-pre-wrap">
          {error}
        </pre>
      )}

      <div className="flex items-center gap-2">
        <button
          onClick={save}
          disabled={saving || !yamlText.trim()}
          className="bg-emerald-900 hover:bg-emerald-800 disabled:opacity-50 text-emerald-200 rounded px-3 py-1 text-xs"
        >
          {saving ? 'saving…' : mode === 'create' ? 'Create' : 'Save'}
        </button>
        {mode === 'update' && (
          <button
            onClick={retire}
            disabled={saving}
            className="bg-amber-900 hover:bg-amber-800 disabled:opacity-50 text-amber-200 rounded px-3 py-1 text-xs"
          >
            {confirmRetire ? 'click again to confirm retire' : 'Retire'}
          </button>
        )}
        <button
          onClick={onCancel}
          disabled={saving}
          className="bg-surface-100 hover:bg-surface-100 disabled:opacity-50 text-slate-300 rounded px-3 py-1 text-xs ml-auto"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}
