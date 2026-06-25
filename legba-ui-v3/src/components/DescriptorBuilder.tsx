/**
 * UI-4 (Tier D) — Guided descriptor builder.
 *
 * A form that produces a *valid* descriptor body from operator input, the
 * "less raw" alternative to the inline-YAML `DescriptorEditor`. The two are
 * peers: each registry panel offers a "+ build" (guided) action ALONGSIDE
 * the existing "+ new" (raw YAML) action — the operator picks.
 *
 * How it works:
 *   - A declarative `FIELD_SPECS[family]` lists the fields the wizard
 *     surfaces, with hints derived from the pydantic schemas
 *     (`src/legba/data/schemas/*`): types, patterns, required-ness, enums.
 *   - The form seeds from the family's *starter descriptor* skeleton so all
 *     the structural/optional blocks (pipeline, coordination, …) are present
 *     and valid; the operator only edits the load-bearing top-level fields.
 *   - On "Build & save" the field values are merged into the skeleton via
 *     dotted paths, then POSTed to `POST /api/v1/registry/descriptors/{family}`.
 *     The registry's 422 (`{error, message}`) surfaces inline — the same
 *     validation gate the raw editor hits.
 *
 * The wizard does NOT try to expose every field — deep/rare blocks stay in
 * the YAML escape hatch. It covers the fields an operator sets to stand up a
 * basic working descriptor, and hands off the rest to the inline editor via
 * the "edit as YAML" toggle.
 */

import { useMemo, useState } from 'react'
import { apiPost, ApiError } from '@/lib/api'
import { startersForFamily } from '@/lib/starter-descriptors'

/**
 * Families the guided form builder supports — the four that register via the
 * generic `POST /registry/descriptors/{family}` path. (Stack components use
 * the separate `/registry/stack` path + property-factory configs, so the
 * stack panel gets the starter-clone surface instead of this form.)
 */
export type BuilderFamily = 'target' | 'source' | 'analyst' | 'action_pack'

/** One field surfaced in the wizard. */
export interface FieldSpec {
  /** Dotted path into the descriptor body (e.g. "identity.id"). */
  path: string
  /** Field label. */
  label: string
  /** Input type. `list` = comma-separated → string[]. */
  type: 'text' | 'number' | 'enum' | 'list' | 'cron'
  /** Hint text (derived from the schema constraint). */
  hint?: string
  /** For enum fields. */
  options?: readonly string[]
  /** Required (the schema rejects empty). */
  required?: boolean
  /** Client-side regex (mirrors the schema pattern) for early feedback. */
  pattern?: RegExp
}

/* -------------------------------------------------------------------------- */
/* field specs — derived from src/legba/data/schemas/*                        */
/* -------------------------------------------------------------------------- */

const ID_SNAKE = /^[a-z][a-z0-9_]*$/
const ID_DOTTED = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$/

const TARGET_FIELDS: readonly FieldSpec[] = [
  {
    path: 'identity.id',
    label: 'Target id',
    type: 'text',
    required: true,
    pattern: ID_SNAKE,
    hint: 'lowercase snake_case, ≤128 chars (TargetId pattern ^[a-z][a-z0-9_]*$)',
  },
  { path: 'identity.name', label: 'Display name', type: 'text', required: true, hint: 'human-readable' },
  { path: 'identity.owner', label: 'Owner', type: 'text', required: true, hint: 'owning operator / team' },
  {
    path: 'identity.abstraction_level',
    label: 'Abstraction level',
    type: 'enum',
    options: ['L1', 'L2', 'L3'],
    hint: 'L1 = concrete instance; L2/L3 = templates (discovery)',
  },
  {
    path: 'scope.domain',
    label: 'Scope domain',
    type: 'enum',
    options: ['geo', 'estate', 'entity'],
    hint: 'discriminated union — geo (OSINT) / estate (ASM) / entity (single)',
  },
  {
    path: 'scope.geo',
    label: 'Geo codes',
    type: 'list',
    hint: 'comma-separated ISO codes, e.g. BR, US (2–3 uppercase). geo domain.',
  },
  {
    path: 'scope.languages',
    label: 'Languages',
    type: 'list',
    hint: 'comma-separated, e.g. en, pt-BR',
  },
  {
    path: 'scope.entity_classes',
    label: 'Entity classes',
    type: 'list',
    hint: 'comma-separated snake_case, e.g. organization, person',
  },
  {
    path: 'scope.tags',
    label: 'Scope tags',
    type: 'list',
    hint: 'comma-separated snake_case — also the match keys for source-selectors',
  },
  {
    path: 'analyst.cadence.fallback_schedule',
    label: 'Inline analyst cadence (cron)',
    type: 'cron',
    hint: 'cron expression, e.g. */15 * * * *',
  },
]

const SOURCE_FIELDS: readonly FieldSpec[] = [
  {
    path: 'identity.id',
    label: 'Source id',
    type: 'text',
    required: true,
    pattern: ID_DOTTED,
    hint: 'source.<provider>.<purpose> (dots allowed), e.g. source.epe.rss',
  },
  { path: 'identity.name', label: 'Display name', type: 'text', required: true },
  {
    path: 'identity.kind',
    label: 'Source kind',
    type: 'text',
    required: true,
    hint: 'source-handler kind, e.g. rss / gdelt_query / qualys_vmdr',
  },
  { path: 'identity.owner', label: 'Owner', type: 'text', required: true },
  {
    path: 'scope.owner_tenant',
    label: 'Owner tenant',
    type: 'text',
    hint: 'tenancy seam — defaults to "default"',
  },
  { path: 'scope.geo', label: 'Geo codes', type: 'list', hint: 'comma-separated, optional' },
  { path: 'scope.tags', label: 'Scope tags', type: 'list', hint: 'what target selectors match against' },
  {
    path: 'cadence.schedule.raw',
    label: 'Poll cron',
    type: 'cron',
    hint: 'cron for poll sources, e.g. */15 * * * * (leave blank for push)',
  },
]

const ANALYST_FIELDS: readonly FieldSpec[] = [
  {
    path: 'identity.id',
    label: 'Analyst id',
    type: 'text',
    required: true,
    pattern: ID_SNAKE,
    hint: 'lowercase snake_case (AnalystId pattern)',
  },
  { path: 'identity.name', label: 'Display name', type: 'text', required: true },
  { path: 'identity.owner', label: 'Owner', type: 'text', required: true },
  {
    path: 'identity.kind',
    label: 'Analyst kind',
    type: 'enum',
    options: [
      'inline_target',
      'cross_target_raw',
      'meta_findings_synthesizer',
      'deterministic',
      'predictor',
      'critic',
      'optimizer',
      'cross_analyst_correlator',
      'consult_on_demand',
    ],
    hint: 'AnalystKind taxonomy — must be a known kind',
  },
  {
    path: 'method.prompt_module',
    label: 'Prompt module',
    type: 'text',
    hint: 'dotted module path, e.g. legba.prompts.generic.v1',
  },
  {
    path: 'method.budget_tokens_per_day',
    label: 'Budget tokens/day',
    type: 'number',
    hint: 'per-analyst daily token ceiling',
  },
  {
    path: 'cadence.fallback_schedule',
    label: 'Cadence (cron)',
    type: 'cron',
    hint: 'reconcile-loop fallback schedule, e.g. */15 * * * *',
  },
]

const ACTION_PACK_FIELDS: readonly FieldSpec[] = [
  {
    path: 'identity.id',
    label: 'Pack id',
    type: 'text',
    required: true,
    pattern: ID_DOTTED,
    hint: 'lowercase, dots allowed, e.g. incident_response',
  },
  { path: 'identity.name', label: 'Display name', type: 'text', required: true },
  { path: 'identity.owner', label: 'Owner', type: 'text', required: true },
  {
    path: 'applies_to_tags',
    label: 'Applies-to tags',
    type: 'list',
    hint: 'comma-separated snake_case context tags',
  },
  {
    path: 'governor.max_invocations_per_hour',
    label: 'Max invocations/hour',
    type: 'number',
    hint: 'per-pack rate cap (≥0)',
  },
  {
    path: 'governor.max_cost_usd_per_day',
    label: 'Max cost USD/day',
    type: 'number',
    hint: 'per-pack daily cost cap (≥0)',
  },
]

export const FIELD_SPECS: Record<BuilderFamily, readonly FieldSpec[]> = {
  target: TARGET_FIELDS,
  source: SOURCE_FIELDS,
  analyst: ANALYST_FIELDS,
  action_pack: ACTION_PACK_FIELDS,
}

/* -------------------------------------------------------------------------- */
/* path helpers                                                               */
/* -------------------------------------------------------------------------- */

/** Read a dotted path out of an object (undefined if any segment missing). */
export function getPath(obj: Record<string, unknown>, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, key) => {
    if (acc && typeof acc === 'object') return (acc as Record<string, unknown>)[key]
    return undefined
  }, obj)
}

/** Immutably set a dotted path, creating intermediate objects as needed. */
export function setPath(
  obj: Record<string, unknown>,
  path: string,
  value: unknown,
): Record<string, unknown> {
  const keys = path.split('.')
  const root: Record<string, unknown> = { ...obj }
  let cursor: Record<string, unknown> = root
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i]
    const existing = cursor[k]
    const next: Record<string, unknown> =
      existing && typeof existing === 'object' && !Array.isArray(existing)
        ? { ...(existing as Record<string, unknown>) }
        : {}
    cursor[k] = next
    cursor = next
  }
  cursor[keys[keys.length - 1]] = value
  return root
}

/** Coerce a raw form string into the typed value the field expects. */
export function coerceValue(spec: FieldSpec, raw: string): unknown {
  if (spec.type === 'list') {
    return raw
      .split(',')
      .map((s) => s.trim())
      .filter((s) => s.length > 0)
  }
  if (spec.type === 'number') {
    const n = Number(raw)
    return raw.trim() === '' || Number.isNaN(n) ? undefined : n
  }
  if (spec.type === 'cron') {
    return raw.trim() === '' ? undefined : raw.trim()
  }
  return raw
}

/**
 * Build a complete descriptor body from the wizard's field values.
 *
 * Starts from the family's first starter skeleton (so every structural
 * block is present + valid), then overlays the operator's edits by dotted
 * path. Empty optional values are skipped (leaving the skeleton default);
 * required values always overlay.
 */
export function buildDescriptorBody(
  family: BuilderFamily,
  values: Record<string, string>,
): Record<string, unknown> {
  const starters = startersForFamily(family)
  let body: Record<string, unknown> =
    starters.length > 0 ? starters[0].build() : {}
  for (const spec of FIELD_SPECS[family]) {
    const raw = values[spec.path] ?? ''
    const coerced = coerceValue(spec, raw)
    const isEmpty =
      coerced === undefined ||
      (Array.isArray(coerced) && coerced.length === 0) ||
      (typeof coerced === 'string' && coerced.trim() === '')
    // Required fields overlay even with the skeleton default already present;
    // optional empties keep the skeleton's default so the body stays valid.
    if (isEmpty && !spec.required) continue
    if (isEmpty && spec.required) continue // leave skeleton placeholder; client validation flags it
    body = setPath(body, spec.path, coerced)
  }
  return body
}

/** Collect client-side validation errors (required + pattern). */
export function validateValues(
  family: BuilderFamily,
  values: Record<string, string>,
): Record<string, string> {
  const errors: Record<string, string> = {}
  for (const spec of FIELD_SPECS[family]) {
    const raw = (values[spec.path] ?? '').trim()
    if (spec.required && raw === '') {
      errors[spec.path] = 'required'
      continue
    }
    if (raw !== '' && spec.pattern && !spec.pattern.test(raw)) {
      errors[spec.path] = `must match ${spec.pattern.source}`
    }
  }
  return errors
}

/* -------------------------------------------------------------------------- */
/* component                                                                  */
/* -------------------------------------------------------------------------- */

interface DescriptorBuilderProps {
  family: BuilderFamily
  /** Seed field values (e.g. when cloning a starter into the wizard). */
  initialValues?: Record<string, string>
  /** Called after a successful register with the new version hash. */
  onSaved?: (version: string) => void
  onCancel?: () => void
}

export function DescriptorBuilder({
  family,
  initialValues,
  onSaved,
  onCancel,
}: DescriptorBuilderProps) {
  const specs = FIELD_SPECS[family]
  const [values, setValues] = useState<Record<string, string>>(initialValues ?? {})
  const [saving, setSaving] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)
  const [showPreview, setShowPreview] = useState(false)

  const clientErrors = useMemo(() => validateValues(family, values), [family, values])
  const hasClientErrors = Object.keys(clientErrors).length > 0

  const previewBody = useMemo(() => buildDescriptorBody(family, values), [family, values])

  function setField(path: string, raw: string) {
    setValues((prev) => ({ ...prev, [path]: raw }))
  }

  async function save() {
    if (hasClientErrors) return
    setSaving(true)
    setServerError(null)
    try {
      const body = buildDescriptorBody(family, values)
      // action_pack/source use the same generic descriptor POST path.
      const result = await apiPost<{ version: string }>(
        `/registry/descriptors/${family}`,
        body,
      )
      onSaved?.(result.version)
    } catch (e) {
      if (e instanceof ApiError) {
        const detail =
          typeof e.body === 'object' && e.body && 'detail' in e.body
            ? (e.body as { detail: unknown }).detail
            : e.body
        // The registry 422 detail is `{error, message}` — surface the message.
        const msg =
          detail && typeof detail === 'object' && 'message' in detail
            ? String((detail as { message: unknown }).message)
            : typeof detail === 'string'
              ? detail
              : JSON.stringify(detail, null, 2)
        setServerError(`${e.status}: ${msg}`)
      } else {
        setServerError((e as Error).message)
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="bg-surface-200 border border-emerald-800/60 rounded p-2 space-y-2 mt-2"
      data-testid="descriptor-builder"
    >
      <div className="flex items-center justify-between">
        <div className="text-emerald-300 text-[10px] uppercase tracking-wide">
          guided {family} builder
        </div>
        <div className="text-slate-600 text-[10px]">
          identity.version stamped at save · fields hinted from the schema
        </div>
      </div>

      <div className="grid grid-cols-1 gap-2">
        {specs.map((spec) => {
          const err = clientErrors[spec.path]
          const val = values[spec.path] ?? ''
          return (
            <label key={spec.path} className="block text-[11px]">
              <span className="text-slate-300">
                {spec.label}
                {spec.required && <span className="text-rose-400"> *</span>}
              </span>
              {spec.type === 'enum' ? (
                <select
                  className="w-full bg-surface-100 border border-slate-800 rounded p-1 px-2 mt-0.5 text-slate-200"
                  value={val}
                  onChange={(e) => setField(spec.path, e.target.value)}
                  data-testid={`builder-field-${spec.path}`}
                >
                  <option value="">(default)</option>
                  {spec.options?.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  className={`w-full bg-surface-100 border rounded p-1 px-2 mt-0.5 text-slate-200 font-mono ${
                    err ? 'border-rose-700' : 'border-slate-800'
                  }`}
                  type={spec.type === 'number' ? 'number' : 'text'}
                  value={val}
                  onChange={(e) => setField(spec.path, e.target.value)}
                  placeholder={spec.type === 'list' ? 'comma, separated' : ''}
                  spellCheck={false}
                  data-testid={`builder-field-${spec.path}`}
                />
              )}
              <span className="flex items-center justify-between">
                {spec.hint && <span className="text-slate-600 text-[10px]">{spec.hint}</span>}
                {err && (
                  <span className="text-rose-400 text-[10px]" data-testid={`builder-err-${spec.path}`}>
                    {err}
                  </span>
                )}
              </span>
            </label>
          )
        })}
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => setShowPreview((v) => !v)}
          className="text-slate-400 hover:text-slate-200 text-[10px] underline"
          data-testid="builder-toggle-preview"
        >
          {showPreview ? 'hide preview' : 'preview descriptor'}
        </button>
      </div>

      {showPreview && (
        <pre
          className="bg-surface-100 border border-slate-800 rounded p-2 text-[10px] text-slate-300 overflow-x-auto max-h-72"
          data-testid="builder-preview"
        >
          {JSON.stringify(previewBody, null, 2)}
        </pre>
      )}

      {serverError && (
        <pre
          className="bg-rose-900/20 border border-rose-700 rounded p-2 text-[10px] text-rose-200 overflow-x-auto whitespace-pre-wrap"
          data-testid="builder-error"
        >
          {serverError}
        </pre>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={save}
          disabled={saving || hasClientErrors}
          className="bg-emerald-900 hover:bg-emerald-800 disabled:opacity-50 text-emerald-200 rounded px-3 py-1 text-xs"
          data-testid="builder-save"
        >
          {saving ? 'saving…' : 'Build & save'}
        </button>
        {hasClientErrors && (
          <span className="text-rose-400 text-[10px]">fix the highlighted fields</span>
        )}
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          className="bg-surface-100 hover:bg-surface-100 disabled:opacity-50 text-slate-300 rounded px-3 py-1 text-xs ml-auto"
          data-testid="builder-cancel"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}
