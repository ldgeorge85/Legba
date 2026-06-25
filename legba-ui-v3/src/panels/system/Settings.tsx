/**
 * Settings — model-component configuration + first-run wizard
 * (config-honesty stream).
 *
 * The RUNTIME source of truth for the LLM / embedding / NLP endpoints is the
 * `stack_components` registry, NOT `.env` (which only seeds those rows once at
 * bring-up). This panel reads + writes those rows directly:
 *
 *   - reads  GET /api/v1/registry/config/status   (first-run readiness)
 *            GET /api/v1/registry/stack?kind=…     (current component bodies)
 *   - writes POST /api/v1/registry/vault/secrets   (credentials → vault)
 *            POST/PUT /api/v1/registry/stack        (config → registry, with
 *                                                    vault REFERENCES only)
 *
 * Secrets are never read back from the server and never placed plaintext into
 * a component body — the body carries `{factory_kind:'secret', raw:<vaultId>}`
 * references. A blank credential field on an existing component leaves the
 * stored secret untouched.
 *
 * First-run: when `config/status` reports `first_run` (one or more required
 * components have no row yet), the panel leads with a "required components
 * unconfigured → configure here" banner instead of assuming a live stack.
 */

import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { PanelChrome } from '@/components/PanelChrome'
import {
  ApiError,
  fetchConfigStatus,
  listStackComponents,
  registerStackComponent,
  storeSecret,
  updateStackComponent,
  type ConfigStatus,
  type StackComponentRow,
} from '@/lib/api'
import type { PanelProps } from '@/types'

// ---------------------------------------------------------------------------
// Per-kind form spec — the editable fields + their vault wiring.
// ---------------------------------------------------------------------------

interface SecretField {
  /** Form label. */
  label: string
  /** Component-body config key that holds the secret ref. */
  configKey: string
  /** Default vault secret id (seed convention) when registering fresh. */
  defaultVaultId: string
}

interface TextField {
  label: string
  configKey: string
  placeholder: string
}

interface KindSpec {
  kind: 'llm_provider' | 'embedding' | 'nlp_service'
  title: string
  blurb: string
  schemaUri: string
  /** Default component id when registering a brand-new row. */
  defaultComponentId: string
  defaultName: string
  /** Text fields shown for this kind (endpoint, model name…). */
  textFields: TextField[]
  /** Secret fields (credentials → vault). */
  secretFields: SecretField[]
  /** Extra config the body needs that the form doesn't surface. */
  staticConfig: Record<string, unknown>
}

const KIND_SPECS: KindSpec[] = [
  {
    kind: 'llm_provider',
    title: 'LLM provider',
    blurb: 'Primary OpenAI-compatible chat endpoint (the analyst inference path).',
    schemaUri: 'legba/stack/llm_provider/1.0.0',
    defaultComponentId: 'llm.primary.openai_compat',
    defaultName: 'Primary LLM (OpenAI-compatible)',
    textFields: [
      { label: 'API endpoint (base host, no /v1)', configKey: 'api_endpoint', placeholder: 'https://llm.example.internal' },
      { label: 'Model name', configKey: 'model_name', placeholder: 'gpt-oss-120b' },
    ],
    secretFields: [
      { label: 'API key', configKey: 'api_key', defaultVaultId: 'llm.primary.api_key' },
    ],
    staticConfig: {
      max_tokens: num(16384, 1, 1_000_000),
      tier: dropdown('primary', ['primary', 'fallback', 'cheap']),
    },
  },
  {
    kind: 'embedding',
    title: 'Embedding service',
    blurb: 'Vector-embedding endpoint (OpenAI-compatible /v1/embeddings).',
    schemaUri: 'legba/stack/embedding/1.0.0',
    defaultComponentId: 'embed.primary.openai_compat',
    defaultName: 'Primary embedding service',
    textFields: [
      { label: 'API endpoint', configKey: 'endpoint', placeholder: 'https://llm.example.internal' },
      { label: 'Model name', configKey: 'model_name', placeholder: 'bge-m3' },
    ],
    secretFields: [
      { label: 'API key (optional)', configKey: 'api_key', defaultVaultId: 'embed.primary.api_key' },
    ],
    staticConfig: {
      dim: num(1024, 64, 8192),
      normalize: dropdown('true', ['true', 'false']),
      batch_size: num(64, 1, 1024),
    },
  },
  {
    kind: 'nlp_service',
    title: 'NLP service',
    blurb: 'Hosted translate / classify / extract / summarize endpoint.',
    schemaUri: 'legba/stack/nlp_service/1.0.0',
    defaultComponentId: 'nlp.local.legba_models',
    defaultName: 'NLP service',
    textFields: [
      { label: 'Endpoint', configKey: 'endpoint', placeholder: 'https://nlp.example.internal' },
    ],
    secretFields: [
      { label: 'Basic-auth user (optional)', configKey: 'api_user', defaultVaultId: 'nlp.local.legba_models.api_user' },
      { label: 'Basic-auth password (optional)', configKey: 'api_pass', defaultVaultId: 'nlp.local.legba_models.api_pass' },
    ],
    staticConfig: {
      timeout_seconds: num(60, 1, 600),
      translate_path: text('/translate'),
      classify_path: text('/classify'),
      extract_path: text('/extract'),
      summarize_path: text('/summarize'),
      health_path: text('/health'),
    },
  },
]

// Property-factory helpers (match scripts/bringup_register_stack.py).
function text(s: string) {
  return { factory_kind: 'text', raw: s }
}
function num(n: number, minimum: number, maximum: number) {
  return { factory_kind: 'number', raw: n, minimum, maximum }
}
function secret(vaultId: string) {
  return { factory_kind: 'secret', raw: vaultId }
}
function dropdown(value: string, options: string[]) {
  return { factory_kind: 'dropdown_static', raw: value, options }
}

function readRaw(body: Record<string, unknown>, key: string): string {
  const config = (body?.config ?? {}) as Record<string, unknown>
  const v = config[key] as { raw?: unknown } | undefined
  if (v && typeof v === 'object' && 'raw' in v) return String(v.raw ?? '')
  return ''
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

interface KindFormState {
  text: Record<string, string>
  /** Plaintext secret entry (write-only; cleared after save). */
  secret: Record<string, string>
}

export default function SettingsPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [forms, setForms] = useState<Record<string, KindFormState>>({})
  const [saving, setSaving] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ kind: string; text: string; ok: boolean } | null>(null)

  const status = useQuery<ConfigStatus>({
    queryKey: ['config-status'],
    queryFn: fetchConfigStatus,
    refetchInterval: 30_000,
  })

  const components = useQuery<StackComponentRow[]>({
    queryKey: ['settings-stack'],
    queryFn: async () => {
      const all = await Promise.all(
        KIND_SPECS.map((s) => listStackComponents(s.kind)),
      )
      return all.flat()
    },
  })

  const byKind = useMemo(() => {
    const out: Record<string, StackComponentRow | undefined> = {}
    for (const row of components.data ?? []) {
      // Prefer the head row; ignore retired.
      if (row.state === 'retired') continue
      if (!out[row.kind]) out[row.kind] = row
    }
    return out
  }, [components.data])

  const formFor = (spec: KindSpec): KindFormState => {
    const existing = forms[spec.kind]
    if (existing) return existing
    const row = byKind[spec.kind]
    const t: Record<string, string> = {}
    for (const f of spec.textFields) t[f.configKey] = row ? readRaw(row.body, f.configKey) : ''
    return { text: t, secret: {} }
  }

  const setText = (kind: string, key: string, value: string) =>
    setForms((prev) => {
      const cur = prev[kind] ?? { text: {}, secret: {} }
      return { ...prev, [kind]: { ...cur, text: { ...cur.text, [key]: value } } }
    })

  const setSecret = (kind: string, key: string, value: string) =>
    setForms((prev) => {
      const cur = prev[kind] ?? { text: {}, secret: {} }
      return { ...prev, [kind]: { ...cur, secret: { ...cur.secret, [key]: value } } }
    })

  async function save(spec: KindSpec) {
    setSaving(spec.kind)
    setMsg(null)
    try {
      const form = formFor(spec)
      const row = byKind[spec.kind]

      // 1. Route any newly-entered credentials through the vault FIRST so the
      //    component body only ever references existing vault entries.
      const secretRefs: Record<string, ReturnType<typeof secret> | undefined> = {}
      for (const sf of spec.secretFields) {
        const plaintext = (form.secret[sf.configKey] ?? '').trim()
        const vaultId = sf.defaultVaultId
        if (plaintext) {
          await storeSecret(vaultId, plaintext, `${spec.title} ${sf.label} (set via Settings)`)
          secretRefs[sf.configKey] = secret(vaultId)
        } else if (row) {
          // Keep the existing ref untouched on an update.
          const cfg = (row.body?.config ?? {}) as Record<string, unknown>
          if (cfg[sf.configKey]) secretRefs[sf.configKey] = secret(vaultId)
        }
      }

      // 2. Assemble the component body (config = vault refs + text + statics).
      const config: Record<string, unknown> = { ...spec.staticConfig }
      for (const tf of spec.textFields) {
        config[tf.configKey] = text((form.text[tf.configKey] ?? '').trim())
      }
      for (const [k, ref] of Object.entries(secretRefs)) {
        if (ref) config[k] = ref
      }

      const componentId = row?.component_id ?? spec.defaultComponentId
      const body: Record<string, unknown> = {
        id: componentId,
        name: row?.name ?? spec.defaultName,
        schema_uri: spec.schemaUri,
        // 16-zero placeholder; the registry stamps the content-hash version.
        version: '0'.repeat(16),
        state: 'active',
        owner: 'operator',
        config,
      }

      if (row) {
        await updateStackComponent(componentId, body)
      } else {
        await registerStackComponent(body)
      }

      // Clear the write-only secret entries; refresh status + rows.
      setForms((prev) => ({ ...prev, [spec.kind]: { ...formFor(spec), secret: {} } }))
      await qc.invalidateQueries({ queryKey: ['config-status'] })
      await qc.invalidateQueries({ queryKey: ['settings-stack'] })
      setMsg({ kind: spec.kind, text: 'Saved.', ok: true })
    } catch (err) {
      const detail =
        err instanceof ApiError
          ? typeof err.body === 'string'
            ? err.body
            : JSON.stringify(err.body)
          : err instanceof Error
            ? err.message
            : 'save failed'
      setMsg({ kind: spec.kind, text: detail, ok: false })
    } finally {
      setSaving(null)
    }
  }

  const firstRun = status.data?.first_run ?? false

  return (
    <PanelChrome
      registration={registration}
      subtitle="Model-serving components (registry is the runtime source of truth)"
      onRefresh={() => {
        status.refetch()
        components.refetch()
      }}
    >
      {status.isError && (
        <div className="text-rose-400 text-sm mb-2" data-testid="settings-status-error">
          could not load config status: {(status.error as Error)?.message}
        </div>
      )}

      {firstRun && (
        <div
          className="mb-3 rounded border border-amber-700 bg-amber-950/50 p-3 text-amber-200 text-sm"
          data-testid="settings-first-run"
        >
          <div className="font-semibold mb-1">Required components unconfigured</div>
          <div className="text-amber-300/90">
            One or more of the required model-serving components below have no
            registry entry yet. Configure them here to bring the analysis layer
            online. These rows are the live source of truth — editing{' '}
            <code>.env</code> after bring-up has no effect.
          </div>
        </div>
      )}

      <div className="flex-1 overflow-auto space-y-4 text-xs">
        {KIND_SPECS.map((spec) => {
          const row = byKind[spec.kind]
          const st = (status.data?.required ?? []).find((r) => r.kind === spec.kind)
          const form = formFor(spec)
          return (
            <section
              key={spec.kind}
              className="rounded border border-slate-800 bg-surface-100 p-3"
              data-testid={`settings-kind-${spec.kind}`}
            >
              <div className="flex items-baseline gap-2 mb-1">
                <span className="text-slate-200 font-semibold">{spec.title}</span>
                <span
                  className={`rounded px-1 text-[10px] ${
                    st?.active
                      ? 'bg-emerald-900 text-emerald-200'
                      : st?.configured
                        ? 'bg-amber-900 text-amber-200'
                        : 'bg-rose-900 text-rose-200'
                  }`}
                  data-testid={`settings-badge-${spec.kind}`}
                >
                  {st?.active ? 'active' : st?.configured ? `configured (${st?.state})` : 'not configured'}
                </span>
                {row && (
                  <span className="text-slate-600 font-mono text-[10px]">{row.component_id}</span>
                )}
              </div>
              <div className="text-slate-500 mb-2">{spec.blurb}</div>

              {spec.textFields.map((tf) => (
                <label key={tf.configKey} className="block mb-2">
                  <span className="text-slate-400">{tf.label}</span>
                  <input
                    className="w-full mt-0.5 bg-surface-200 border border-slate-700 rounded p-1 px-2"
                    placeholder={tf.placeholder}
                    value={form.text[tf.configKey] ?? ''}
                    onChange={(e) => setText(spec.kind, tf.configKey, e.target.value)}
                    data-testid={`settings-${spec.kind}-${tf.configKey}`}
                  />
                </label>
              ))}

              {spec.secretFields.map((sf) => (
                <label key={sf.configKey} className="block mb-2">
                  <span className="text-slate-400">{sf.label}</span>
                  <input
                    type="password"
                    autoComplete="new-password"
                    className="w-full mt-0.5 bg-surface-200 border border-slate-700 rounded p-1 px-2"
                    placeholder={row ? '•••••• (unchanged unless typed)' : 'enter credential'}
                    value={form.secret[sf.configKey] ?? ''}
                    onChange={(e) => setSecret(spec.kind, sf.configKey, e.target.value)}
                    data-testid={`settings-${spec.kind}-${sf.configKey}`}
                  />
                  <span className="text-slate-600 text-[10px]">
                    stored in the vault as <code>{sf.defaultVaultId}</code>; never read back
                  </span>
                </label>
              ))}

              <div className="flex items-center gap-2 mt-2">
                <button
                  onClick={() => save(spec)}
                  disabled={saving === spec.kind}
                  className="bg-sky-900 hover:bg-sky-800 disabled:opacity-50 text-sky-200 rounded px-3 py-1"
                  data-testid={`settings-save-${spec.kind}`}
                >
                  {saving === spec.kind ? 'saving…' : row ? 'Update' : 'Configure'}
                </button>
                {msg && msg.kind === spec.kind && (
                  <span
                    className={msg.ok ? 'text-emerald-400' : 'text-rose-400'}
                    data-testid={`settings-msg-${spec.kind}`}
                  >
                    {msg.text}
                  </span>
                )}
              </div>
            </section>
          )
        })}
      </div>
    </PanelChrome>
  )
}
