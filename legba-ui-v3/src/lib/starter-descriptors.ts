/**
 * UI-4 (Tier D) — starter-descriptor library.
 *
 * "Make the registries less raw." A small set of *basic, working*
 * descriptors — one per family (target / source / analyst / action_pack)
 * — that the operator can **clone-and-edit** instead of starting from a
 * blank YAML textarea or a blank wizard.
 *
 * Each starter is a complete, schema-valid descriptor body modelled on the
 * real pydantic schemas in `src/legba/data/schemas/*` and the seed
 * descriptors under `descriptors/`:
 *   - target  → `legba.data.schemas.target.TargetDescriptor`
 *   - source  → `legba.data.schemas.source.SourceDescriptor`
 *   - analyst → `legba.data.schemas.analyst.AnalystDescriptor`
 *   - action_pack → `legba.data.schemas.action_pack.ActionPack`
 *
 * `identity.version` is the registry sentinel (16 zero hex chars) — the
 * registry re-stamps the real content hash at register/update time, the
 * same convention the inline `DescriptorEditor` uses.
 *
 * These are UI-level constants — no backend call; they compose the
 * existing registry REST (POST /descriptors/{family}). The DescriptorBuilder
 * wizard and the inline DescriptorEditor both consume them.
 */

/** Registry version sentinel — registry stamps the real content hash on write. */
export const VERSION_SENTINEL = '0'.repeat(16)

/** A placeholder ISO timestamp for `identity.created` — operator edits before save. */
function nowIso(): string {
  return new Date().toISOString()
}

export type StarterFamily = 'target' | 'source' | 'analyst' | 'action_pack' | 'stack'

export interface StarterDescriptor {
  /** Stable key for the picker. */
  key: string
  family: StarterFamily
  /** Short label shown in the "clone a starter" list. */
  label: string
  /** One-line description of what the starter does. */
  blurb: string
  /** Factory — returns a *fresh* deep-cloned body each call (no shared refs). */
  build: () => Record<string, unknown>
}

/* -------------------------------------------------------------------------- */
/* target                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * Basic geopolitical/OSINT target: one country, one RSS source, an inline
 * LLM-planner analyst, an A2A skill output. Models `target_india_energy_infra`.
 */
function starterTargetGeo(): Record<string, unknown> {
  return {
    identity: {
      id: 'example_target',
      name: 'Example Target',
      schema_uri: 'legba/target/2.0.0',
      version: VERSION_SENTINEL,
      abstraction_level: 'L1',
      state: 'draft',
      owner: 'operator',
      created: nowIso(),
    },
    scope: {
      domain: 'geo',
      geo: ['BR'],
      languages: ['en'],
      entity_classes: ['organization'],
      relationship_types: [],
      time_horizon_days: 90,
      tags: ['example'],
      predicate: null,
    },
    // SourceRef (source-first pivot): a target references shared sources by an
    // explicit `source_id` OR a `source_selector` over source SCOPE. Here a
    // selector auto-wires any source advertising the `example` tag.
    sources: [
      {
        source_selector: { tags: ['example'] },
      },
    ],
    allowed_action_packs: [],
    pipeline: {
      ingestion_filters: [
        { kind: 'language_detect', config: {} },
        { kind: 'dedupe_tier_1', config: {} },
      ],
      enrichment: [],
      routing: [],
    },
    analyst: {
      use: 'inline_target',
      cadence: { fallback_schedule: '*/15 * * * *' },
      method: {
        kind: 'llm_planner',
        llm: { primary: 'llm.primary.openai_compat' },
      },
    },
    outputs: [
      {
        kind: 'a2a_skill',
        config: { skill_id: 'intelligence.example_assessment' },
      },
    ],
    coordination: {
      subscribes_to: [],
      publishes: [],
      allow_cycles: false,
      cycle_hop_limit: 0,
    },
  }
}

/* -------------------------------------------------------------------------- */
/* source                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * Basic poll source: an RSS feed on a 15-minute cron, advertising a tag a
 * target source-selector can match against. Models the source-first pivot's
 * SourceDescriptor shape (`legba.data.schemas.source`).
 */
function starterSourceRss(): Record<string, unknown> {
  return {
    identity: {
      id: 'source.example.rss',
      name: 'Example RSS Source',
      kind: 'rss',
      schema_uri: 'legba/source/1.0.0',
      version: VERSION_SENTINEL,
      abstraction_level: 'L1',
      state: 'draft',
      owner: 'operator',
      created: nowIso(),
    },
    scope: {
      owner_tenant: 'default',
      geo: [],
      languages: ['en'],
      tags: ['example'],
    },
    cadence: {
      schedule: { factory_kind: 'cron', raw: '*/15 * * * *' },
      cooldown_seconds: 0,
      jitter_seconds: 0,
    },
    config: {
      url: { factory_kind: 'text', raw: 'https://example.com/feed.xml' },
    },
  }
}

/* -------------------------------------------------------------------------- */
/* analyst                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * Basic cross-target raw analyst: reads the substrate slice, runs an LLM
 * planner, emits to a NATS stream. Models `analyst_*` seed descriptors.
 */
function starterAnalystLlm(): Record<string, unknown> {
  return {
    identity: {
      id: 'example_analyst',
      name: 'Example Analyst',
      schema_uri: 'legba/analyst/1.0.0',
      version: VERSION_SENTINEL,
      kind: 'cross_target_raw',
      type_signature: {
        input_type: 'legba.runtime.SubstrateSlice',
        output_type: 'legba.runtime.Finding',
      },
      state: 'draft',
      owner: 'operator',
    },
    subscription: {
      targets: { predicate: null, data_types: [], time_window: '24h' },
      other_analysts: [],
      substrate: { direct_queries: false },
    },
    mapping: { fields: [], schema_drift_policy: 'warn_and_continue' },
    method: {
      // method.kind is MethodKind (how it runs), distinct from identity.kind
      // (AnalystKind — what it is). llm_planner is the default planning method.
      kind: 'llm_planner',
      prompt_module: 'legba.prompts.generic.v1',
      llm: {
        primary: {
          factory_kind: 'stack_ref',
          raw: 'llm.primary.openai_compat',
          expected_family: 'llm_provider',
        },
        max_tokens: 1536,
      },
      budget_tokens_per_day: 50000,
    },
    action_packs: [],
    cadence: { fallback_schedule: '*/15 * * * *', cooldown_seconds: 60 },
    outputs: [{ kind: 'nats_stream', config: { channel: 'findings' } }],
  }
}

/* -------------------------------------------------------------------------- */
/* action_pack                                                                */
/* -------------------------------------------------------------------------- */

/**
 * Basic action pack: one tool + an alert channel + a per-pack governor.
 * Models the `incident_response` seed shape (`legba.data.schemas.action_pack`).
 */
function starterActionPack(): Record<string, unknown> {
  return {
    identity: {
      id: 'example_pack',
      name: 'Example Action Pack',
      schema_uri: 'legba/action_pack/1.0.0',
      version: VERSION_SENTINEL,
      abstraction_level: 'L1',
      state: 'draft',
      owner: 'operator',
      created: nowIso(),
    },
    tools: [{ name: 'example_tool', impl: null, config: {}, async_job: false }],
    prompt_fragments: [],
    rules: [],
    channels: [
      {
        name: 'ops_alert',
        kind: 'alert',
        config: { severity_threshold: 'high' },
      },
    ],
    governor: {
      max_invocations_per_hour: 60,
      max_cost_usd_per_day: 5.0,
    },
    applies_to_tags: ['example'],
    applicability_predicate: null,
  }
}

/* -------------------------------------------------------------------------- */
/* stack component                                                            */
/* -------------------------------------------------------------------------- */

/**
 * Basic LLM-provider stack component — the smallest required-field set of
 * the nine stack kinds (`legba.data.schemas.stack.LLMProvider`). Config
 * fields are property-factory values (`{factory_kind, raw}`); the `api_key`
 * is a Secret *reference* (a vault key name, never the credential).
 *
 * Stack components register via the dedicated `/registry/stack` path (not
 * the generic descriptor POST), so this starter is consumed by the inline
 * `DescriptorEditor` (family="stack"), not the form `DescriptorBuilder`.
 */
function starterStackLlmProvider(): Record<string, unknown> {
  return {
    id: 'llm.example.openai_compat',
    name: 'Example LLM Provider',
    schema_uri: 'legba/stack/llm_provider/1.0.0',
    version: VERSION_SENTINEL,
    state: 'draft',
    owner: 'operator',
    config: {
      api_endpoint: { factory_kind: 'text', raw: 'https://api.example.com/v1' },
      api_key: { factory_kind: 'secret', raw: 'example.llm.api_key' },
      model_name: { factory_kind: 'text', raw: 'gpt-4o-mini' },
      max_tokens: { factory_kind: 'number', raw: 2048, minimum: 1, maximum: 128000 },
    },
  }
}

/* -------------------------------------------------------------------------- */
/* registry                                                                   */
/* -------------------------------------------------------------------------- */

export const STARTER_DESCRIPTORS: readonly StarterDescriptor[] = [
  {
    key: 'target.geo_basic',
    family: 'target',
    label: 'Basic geo target',
    blurb: 'One country, a tag-selector source, an inline LLM analyst, an A2A skill output.',
    build: starterTargetGeo,
  },
  {
    key: 'source.rss_basic',
    family: 'source',
    label: 'Basic RSS source',
    blurb: 'A polled RSS feed on a 15-minute cron, tagged for target selectors.',
    build: starterSourceRss,
  },
  {
    key: 'analyst.llm_basic',
    family: 'analyst',
    label: 'Basic LLM analyst',
    blurb: 'Cross-target raw analyst — reads the substrate slice, emits findings.',
    build: starterAnalystLlm,
  },
  {
    key: 'action_pack.basic',
    family: 'action_pack',
    label: 'Basic action pack',
    blurb: 'One tool + an alert channel + a per-pack governor.',
    build: starterActionPack,
  },
  {
    key: 'stack.llm_provider',
    family: 'stack',
    label: 'Basic LLM provider',
    blurb: 'An OpenAI-compatible LLM provider — endpoint + Secret-ref key + model.',
    build: starterStackLlmProvider,
  },
] as const

/** All starters for a given family (the registry panel picks its own family). */
export function startersForFamily(family: StarterFamily): readonly StarterDescriptor[] {
  return STARTER_DESCRIPTORS.filter((s) => s.family === family)
}

/** Look up a single starter by its stable key. */
export function starterByKey(key: string): StarterDescriptor | undefined {
  return STARTER_DESCRIPTORS.find((s) => s.key === key)
}
