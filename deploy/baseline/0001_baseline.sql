--
-- PostgreSQL database dump
--

\restrict 9ne7TpAWavtTpavLOLwNlHisTuouJQUsRub2S2ScG3t3SOV4RthVr8keT7XeKyd

-- Dumped from database version 18.1 (Debian 18.1-1.pgdg13+2)
-- Dumped by pg_dump version 18.1 (Debian 18.1-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: ag_catalog; Type: SCHEMA; Schema: -; Owner: legba
--

CREATE SCHEMA ag_catalog;


ALTER SCHEMA ag_catalog OWNER TO legba;

--
-- Name: age; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS age WITH SCHEMA ag_catalog;


--
-- Name: EXTENSION age; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION age IS 'AGE database extension';


--
-- Name: plpgsql; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS plpgsql WITH SCHEMA pg_catalog;


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;


--
-- Name: EXTENSION "uuid-ossp"; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION "uuid-ossp" IS 'generate universally unique identifiers (UUIDs)';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: action_pack_descriptors; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.action_pack_descriptors (
    descriptor_id text NOT NULL,
    version text NOT NULL,
    schema_uri text NOT NULL,
    is_head boolean DEFAULT true NOT NULL,
    abstraction_level text DEFAULT 'L1'::text NOT NULL,
    state text DEFAULT 'draft'::text NOT NULL,
    owner text NOT NULL,
    name text NOT NULL,
    body jsonb NOT NULL,
    inherits text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retire_after timestamp with time zone
);


ALTER TABLE public.action_pack_descriptors OWNER TO legba;

--
-- Name: action_pack_invocations; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.action_pack_invocations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pack_id text NOT NULL,
    pack_version text DEFAULT ''::text NOT NULL,
    tool_name text NOT NULL,
    budget_account text DEFAULT 'system'::text NOT NULL,
    requested_by text DEFAULT 'system'::text NOT NULL,
    tenant_id text DEFAULT 'default'::text NOT NULL,
    cost_usd numeric(12,6) DEFAULT 0 NOT NULL,
    units integer DEFAULT 1 NOT NULL,
    outcome text DEFAULT 'admitted'::text NOT NULL,
    job_id uuid,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT action_pack_invocations_outcome_check CHECK ((outcome = ANY (ARRAY['admitted'::text, 'completed'::text, 'failed'::text])))
);


ALTER TABLE public.action_pack_invocations OWNER TO legba;

--
-- Name: acute_forecasts; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.acute_forecasts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    region text NOT NULL,
    event_class text NOT NULL,
    window_start timestamp with time zone NOT NULL,
    window_end timestamp with time zone NOT NULL,
    p double precision NOT NULL,
    p_base double precision NOT NULL,
    method text NOT NULL,
    lambda_model double precision,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_outcome integer,
    actual_value integer,
    resolved_by text,
    resolved_at timestamp with time zone
);


ALTER TABLE public.acute_forecasts OWNER TO legba;

--
-- Name: alert_sink_deliveries; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.alert_sink_deliveries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    alert_row_id uuid NOT NULL,
    descriptor_id text NOT NULL,
    descriptor_version text NOT NULL,
    sink_kind text NOT NULL,
    sink_target text,
    attempt_number integer DEFAULT 1 NOT NULL,
    status text NOT NULL,
    error_message text,
    attempted_at timestamp with time zone DEFAULT now() NOT NULL,
    delivered_at timestamp with time zone,
    payload_summary jsonb DEFAULT '{}'::jsonb NOT NULL
);


ALTER TABLE public.alert_sink_deliveries OWNER TO legba;

--
-- Name: analyst_critiques; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.analyst_critiques (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    trace_id uuid NOT NULL,
    judge_analyst_id text NOT NULL,
    judge_analyst_version text NOT NULL,
    rubric_uri text NOT NULL,
    scores jsonb DEFAULT '{}'::jsonb NOT NULL,
    overall_score real,
    revision_delta jsonb,
    produced_at timestamp with time zone DEFAULT now() NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/analyst_critique/jsonschema/1-0-0'::text NOT NULL
);


ALTER TABLE public.analyst_critiques OWNER TO legba;

--
-- Name: analyst_descriptors; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.analyst_descriptors (
    descriptor_id text NOT NULL,
    version text NOT NULL,
    schema_uri text NOT NULL,
    is_head boolean DEFAULT true NOT NULL,
    kind text NOT NULL,
    state text DEFAULT 'draft'::text NOT NULL,
    owner text NOT NULL,
    name text NOT NULL,
    body jsonb NOT NULL,
    type_signature jsonb DEFAULT '{}'::jsonb NOT NULL,
    inherits text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.analyst_descriptors OWNER TO legba;

--
-- Name: analyst_outputs; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.analyst_outputs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    body text DEFAULT ''::text NOT NULL,
    confidence real DEFAULT 0.5 NOT NULL,
    severity text,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    target_id text,
    target_version text,
    analyst_id text,
    analyst_version text,
    produced_at timestamp with time zone DEFAULT now() NOT NULL,
    derived_from uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    schema_uri text NOT NULL,
    run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    situation_signature text,
    superseded_by uuid,
    superseded_at timestamp with time zone
);


ALTER TABLE public.analyst_outputs OWNER TO legba;

--
-- Name: analyst_traces; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.analyst_traces (
    run_id uuid NOT NULL,
    analyst_id text NOT NULL,
    analyst_version text NOT NULL,
    target_id text,
    cadence_trigger text NOT NULL,
    input_row_refs uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    input_payload jsonb,
    prompt_module_hash text,
    prompt_rendered text,
    intermediate_steps jsonb DEFAULT '[]'::jsonb NOT NULL,
    llm_calls jsonb DEFAULT '[]'::jsonb NOT NULL,
    tool_calls jsonb DEFAULT '[]'::jsonb NOT NULL,
    output_row_refs uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    output_payload jsonb,
    status text NOT NULL,
    error_payload jsonb,
    run_started_at timestamp with time zone NOT NULL,
    run_ended_at timestamp with time zone,
    receipt_hash text NOT NULL,
    prev_receipt_hash text,
    schema_uri text DEFAULT 'iglu:legba/analyst_trace/jsonschema/1-0-0'::text NOT NULL
);


ALTER TABLE public.analyst_traces OWNER TO legba;

--
-- Name: audit_checkpoints; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.audit_checkpoints (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    analyst_id text NOT NULL,
    chain_head_hash text NOT NULL,
    trace_count bigint NOT NULL,
    checkpointed_at timestamp with time zone DEFAULT now() NOT NULL,
    signature bytea NOT NULL,
    signer_did text NOT NULL
);


ALTER TABLE public.audit_checkpoints OWNER TO legba;

--
-- Name: budget_demotion_events; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.budget_demotion_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    analyst_id text NOT NULL,
    analyst_version text NOT NULL,
    bucket date NOT NULL,
    cause text NOT NULL,
    tokens_used_at_demote bigint,
    tokens_cap_at_demote bigint,
    primary_llm text,
    fallback_llm text,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT budget_demotion_events_cause_check CHECK ((cause = ANY (ARRAY['per_analyst'::text, 'global'::text])))
);


ALTER TABLE public.budget_demotion_events OWNER TO legba;

--
-- Name: budget_ledger; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.budget_ledger (
    analyst_id text NOT NULL,
    analyst_version text NOT NULL,
    bucket date NOT NULL,
    tokens_used bigint DEFAULT 0 NOT NULL,
    runs integer DEFAULT 0 NOT NULL,
    cost_usd numeric(18,6) DEFAULT 0 NOT NULL,
    last_updated timestamp with time zone DEFAULT now() NOT NULL,
    cost_estimate_usd numeric(12,6) DEFAULT 0 NOT NULL
);


ALTER TABLE public.budget_ledger OWNER TO legba;

--
-- Name: consult_sessions; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.consult_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    mode text DEFAULT 'chat'::text NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    principal text,
    task_id text,
    run_id text,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT consult_sessions_mode_ck CHECK ((mode = ANY (ARRAY['chat'::text, 'deep'::text])))
);


ALTER TABLE public.consult_sessions OWNER TO legba;

--
-- Name: consult_turns; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.consult_turns (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id uuid NOT NULL,
    role text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    steps jsonb DEFAULT '[]'::jsonb NOT NULL,
    tool_calls jsonb DEFAULT '[]'::jsonb NOT NULL,
    cited_refs jsonb DEFAULT '[]'::jsonb NOT NULL,
    finding_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT consult_turns_role_ck CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text])))
);


ALTER TABLE public.consult_turns OWNER TO legba;

--
-- Name: conversion_executions; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.conversion_executions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    executed_at timestamp with time zone DEFAULT now() NOT NULL,
    namespace text NOT NULL,
    descriptor_id text,
    from_uri text NOT NULL,
    to_uri text NOT NULL,
    path_webhook_ids uuid[] NOT NULL,
    path_uri_chain text[] NOT NULL,
    success boolean NOT NULL,
    failed_at_step integer,
    error_kind text,
    error_message text
);


ALTER TABLE public.conversion_executions OWNER TO legba;

--
-- Name: conversion_webhooks; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.conversion_webhooks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    from_uri text NOT NULL,
    to_uri text NOT NULL,
    impl text NOT NULL,
    direction text DEFAULT 'forward'::text NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone,
    retired_by text,
    retired_reason text
);


ALTER TABLE public.conversion_webhooks OWNER TO legba;

--
-- Name: descriptor_audit_log; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.descriptor_audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    actor_id text NOT NULL,
    actor_role text NOT NULL,
    namespace text NOT NULL,
    descriptor_id text NOT NULL,
    action text NOT NULL,
    from_version text,
    to_version text,
    change_summary jsonb,
    signed_payload bytea NOT NULL,
    signer_did text NOT NULL
);


ALTER TABLE public.descriptor_audit_log OWNER TO legba;

--
-- Name: descriptor_conversion_archives; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.descriptor_conversion_archives (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    archived_at timestamp with time zone DEFAULT now() NOT NULL,
    namespace text NOT NULL,
    descriptor_id text NOT NULL,
    from_uri text NOT NULL,
    to_uri text NOT NULL,
    webhook_id uuid NOT NULL,
    legacy_fields jsonb NOT NULL
);


ALTER TABLE public.descriptor_conversion_archives OWNER TO legba;

--
-- Name: descriptor_dead_letter; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.descriptor_dead_letter (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    attempted_at timestamp with time zone DEFAULT now() NOT NULL,
    actor text NOT NULL,
    namespace text NOT NULL,
    attempted_payload jsonb NOT NULL,
    declared_schema_uri text,
    validation_error jsonb NOT NULL,
    resolution text,
    resolution_at timestamp with time zone,
    resolution_ref uuid
);


ALTER TABLE public.descriptor_dead_letter OWNER TO legba;

--
-- Name: discovery_state; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.discovery_state (
    discovery_id text NOT NULL,
    natural_key text NOT NULL,
    family text NOT NULL,
    descriptor_id text NOT NULL,
    descriptor_version text DEFAULT ''::text NOT NULL,
    state text DEFAULT 'active'::text NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    cycle_count integer DEFAULT 1 NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL
);


ALTER TABLE public.discovery_state OWNER TO legba;

--
-- Name: entity_profile_versions; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.entity_profile_versions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_id uuid NOT NULL,
    version integer NOT NULL,
    data jsonb NOT NULL,
    cycle_number integer,
    analyst_id text,
    analyst_version text,
    run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.entity_profile_versions OWNER TO legba;

--
-- Name: entity_profiles; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.entity_profiles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    data jsonb NOT NULL,
    canonical_name text NOT NULL,
    entity_type text DEFAULT 'entity'::text NOT NULL,
    entity_class text DEFAULT 'entity'::text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    completeness_score real DEFAULT 0.0 NOT NULL,
    last_event_link_at timestamp with time zone,
    last_verified_at timestamp with time zone,
    geo_lat double precision,
    geo_lon double precision,
    geo_country text,
    geo_region text,
    target_id text,
    target_version text,
    analyst_id text,
    analyst_version text,
    produced_at timestamp with time zone DEFAULT now() NOT NULL,
    derived_from uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/entity_profile/jsonschema/2-0-0'::text NOT NULL,
    run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.entity_profiles OWNER TO legba;

--
-- Name: facts; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.facts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    subject text NOT NULL,
    predicate text NOT NULL,
    value text NOT NULL,
    confidence real DEFAULT 1.0 NOT NULL,
    source_cycle integer,
    source_type text DEFAULT 'agent'::text NOT NULL,
    data jsonb,
    evidence_set jsonb,
    valid_from timestamp with time zone,
    geo_lat double precision,
    geo_lon double precision,
    target_id text,
    target_version text,
    analyst_id text,
    analyst_version text,
    produced_at timestamp with time zone DEFAULT now() NOT NULL,
    derived_from uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/fact/jsonschema/2-0-0'::text NOT NULL,
    run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone,
    superseded_by uuid,
    confidence_components jsonb,
    seed_batch_id uuid
);


ALTER TABLE public.facts OWNER TO legba;

--
-- Name: finding_supersessions; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.finding_supersessions (
    superseded_finding_id uuid NOT NULL,
    superseding_finding_id uuid NOT NULL,
    situation_signature text NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    score real,
    produced_by text,
    produced_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.finding_supersessions OWNER TO legba;

--
-- Name: global_budget_envelope; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.global_budget_envelope (
    bucket date NOT NULL,
    tokens_cap bigint,
    usd_cap numeric(12,6),
    on_exceeded text DEFAULT 'demote_all'::text NOT NULL,
    note text,
    last_updated timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT global_budget_envelope_on_exceeded_check CHECK ((on_exceeded = ANY (ARRAY['demote_all'::text, 'pause_all'::text, 'alert_only'::text])))
);


ALTER TABLE public.global_budget_envelope OWNER TO legba;

--
-- Name: governor_events; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.governor_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pack_id text NOT NULL,
    tool_name text,
    budget_account text DEFAULT 'system'::text NOT NULL,
    requested_by text DEFAULT 'system'::text NOT NULL,
    tenant_id text DEFAULT 'default'::text NOT NULL,
    decision text NOT NULL,
    cause text DEFAULT 'ok'::text NOT NULL,
    cap_dimension text,
    cap_limit numeric(14,6),
    observed_value numeric(14,6),
    detail text DEFAULT ''::text NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT governor_events_decision_check CHECK ((decision = ANY (ARRAY['allow'::text, 'block'::text])))
);


ALTER TABLE public.governor_events OWNER TO legba;

--
-- Name: graph_metrics; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.graph_metrics (
    metric_kind text NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    analyst_id text,
    analyst_version text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/graph_metric/jsonschema/1-0-0'::text NOT NULL
);


ALTER TABLE public.graph_metrics OWNER TO legba;

--
-- Name: hypotheses; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.hypotheses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    situation_id uuid,
    thesis text NOT NULL,
    counter_thesis text DEFAULT ''::text NOT NULL,
    diagnostic_evidence jsonb DEFAULT '[]'::jsonb NOT NULL,
    supporting_signals uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    refuting_signals uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    evidence_balance integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_cycle integer,
    last_evaluated_cycle integer,
    target_id text,
    target_version text,
    analyst_id text,
    analyst_version text,
    produced_at timestamp with time zone DEFAULT now() NOT NULL,
    derived_from uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/hypothesis/jsonschema/2-0-0'::text NOT NULL,
    run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_outcome smallint,
    resolved_at timestamp with time zone,
    resolved_by text,
    CONSTRAINT hypotheses_resolved_outcome_chk CHECK (((resolved_outcome IS NULL) OR (resolved_outcome = ANY (ARRAY[0, 1]))))
);


ALTER TABLE public.hypotheses OWNER TO legba;

--
-- Name: iso_countries; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.iso_countries (
    iso2 text NOT NULL,
    iso3 text NOT NULL,
    "numeric" text NOT NULL,
    name text NOT NULL,
    official text DEFAULT ''::text NOT NULL,
    region text DEFAULT ''::text NOT NULL,
    subregion text DEFAULT ''::text NOT NULL,
    languages jsonb DEFAULT '[]'::jsonb NOT NULL
);


ALTER TABLE public.iso_countries OWNER TO legba;

--
-- Name: journal_entries; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.journal_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    entry_kind text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    claims jsonb DEFAULT '[]'::jsonb NOT NULL,
    cited_substrate_refs uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    period_start timestamp with time zone NOT NULL,
    period_end timestamp with time zone NOT NULL,
    honesty_flags text[] DEFAULT '{}'::text[] NOT NULL,
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone,
    superseded_by uuid,
    target_id text,
    target_version text,
    analyst_id text,
    analyst_version text,
    produced_at timestamp with time zone NOT NULL,
    derived_from uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/journal/jsonschema/1-0-0'::text NOT NULL,
    run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.journal_entries OWNER TO legba;

--
-- Name: journal_proposals; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.journal_proposals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    proposal_kind text NOT NULL,
    proposed_by_analyst_id text NOT NULL,
    run_id uuid,
    rationale text DEFAULT ''::text NOT NULL,
    diff jsonb DEFAULT '{}'::jsonb NOT NULL,
    cited_substrate_refs uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    decided_by text,
    decision_reason text,
    decided_at timestamp with time zone,
    produced_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.journal_proposals OWNER TO legba;

--
-- Name: legba_data_migrations; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.legba_data_migrations (
    name text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    sha256 text NOT NULL,
    notes text
);


ALTER TABLE public.legba_data_migrations OWNER TO legba;

--
-- Name: nexuses; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.nexuses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    subject text NOT NULL,
    intermediary text,
    object text NOT NULL,
    rel_type text NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    polarity smallint DEFAULT 0 NOT NULL,
    intent text DEFAULT ''::text NOT NULL,
    channel text DEFAULT 'direct'::text NOT NULL,
    confidence real DEFAULT 1.0 NOT NULL,
    valid_from timestamp with time zone,
    valid_until timestamp with time zone,
    superseded_by uuid,
    derived_from uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    source_signal_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    target_id text,
    target_version text,
    analyst_id text,
    analyst_version text,
    produced_at timestamp with time zone DEFAULT now() NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/nexus/jsonschema/1-0-0'::text NOT NULL,
    run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    seed_batch_id uuid,
    source_type text DEFAULT 'agent'::text NOT NULL,
    CONSTRAINT nexuses_polarity_ck CHECK ((polarity = ANY (ARRAY['-1'::integer, 0, 1])))
);


ALTER TABLE public.nexuses OWNER TO legba;

--
-- Name: output_dead_letter; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.output_dead_letter (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    produced_at timestamp with time zone DEFAULT now() NOT NULL,
    run_id uuid,
    analyst_id text NOT NULL,
    analyst_version text NOT NULL,
    declared_schema_uri text NOT NULL,
    attempted_payload jsonb NOT NULL,
    validation_error jsonb NOT NULL,
    resolution text,
    resolution_at timestamp with time zone
);


ALTER TABLE public.output_dead_letter OWNER TO legba;

--
-- Name: proposed_edges; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.proposed_edges (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_entity text NOT NULL,
    target_entity text NOT NULL,
    relationship_type text NOT NULL,
    confidence real DEFAULT 0.5 NOT NULL,
    evidence_text text DEFAULT ''::text NOT NULL,
    source_cycle integer,
    status text DEFAULT 'pending'::text NOT NULL,
    reviewed_at timestamp with time zone,
    target_id text,
    target_version text,
    analyst_id text,
    analyst_version text,
    produced_at timestamp with time zone DEFAULT now() NOT NULL,
    derived_from uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/proposed_edge/jsonschema/1-0-0'::text NOT NULL,
    run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.proposed_edges OWNER TO legba;

--
-- Name: seed_batches; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.seed_batches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source text NOT NULL,
    kind text DEFAULT ''::text NOT NULL,
    source_type text DEFAULT 'seed'::text NOT NULL,
    imported_at timestamp with time zone DEFAULT now() NOT NULL,
    counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    manifest jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.seed_batches OWNER TO legba;

--
-- Name: signal_aliases; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.signal_aliases (
    alias_signal_id uuid NOT NULL,
    canonical_signal_id uuid NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    score real,
    produced_by text,
    produced_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.signal_aliases OWNER TO legba;

--
-- Name: signal_entity_links; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.signal_entity_links (
    signal_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    role text DEFAULT 'mentioned'::text NOT NULL,
    confidence real DEFAULT 0.8 NOT NULL,
    analyst_id text,
    analyst_version text,
    run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.signal_entity_links OWNER TO legba;

--
-- Name: signals; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.signals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_id text NOT NULL,
    source_version text DEFAULT ''::text NOT NULL,
    produced_by_id text,
    produced_by_kind text DEFAULT 'source'::text NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    owner_tenant text DEFAULT 'default'::text NOT NULL,
    modality text DEFAULT 'text'::text NOT NULL,
    mime_type text,
    media_ref text,
    embedding_ref text,
    retention_class text DEFAULT 'reference_only'::text NOT NULL,
    media_ref_expires_at timestamp with time zone,
    object_ref text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    canonical_url text,
    language_hint text,
    raw_provenance jsonb DEFAULT '{}'::jsonb NOT NULL,
    language text,
    geo text[] DEFAULT '{}'::text[] NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    entity_classes text[] DEFAULT '{}'::text[] NOT NULL,
    source_credibility real,
    content_hash text DEFAULT ''::text NOT NULL,
    canonical_signal_id uuid,
    derived_from uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/signal/jsonschema/3-0-0'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    entities_resolved_at timestamp with time zone
);


ALTER TABLE public.signals OWNER TO legba;

--
-- Name: situations; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.situations (
    id uuid NOT NULL,
    data jsonb NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    category text DEFAULT ''::text NOT NULL,
    last_event_at timestamp with time zone,
    event_count integer DEFAULT 0 NOT NULL,
    intensity_score real DEFAULT 0.0 NOT NULL,
    target_id text,
    target_version text,
    analyst_id text,
    analyst_version text,
    produced_at timestamp with time zone DEFAULT now() NOT NULL,
    derived_from uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/situation/jsonschema/2-0-0'::text NOT NULL,
    run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    situation_signature text,
    valid_from timestamp with time zone,
    valid_until timestamp with time zone,
    superseded_by uuid
);


ALTER TABLE public.situations OWNER TO legba;

--
-- Name: source_credibility; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.source_credibility (
    source_host character varying NOT NULL,
    score double precision NOT NULL,
    score_rationale text,
    last_updated timestamp with time zone DEFAULT now() NOT NULL,
    scored_by character varying DEFAULT 'system.seed'::character varying NOT NULL,
    tier character varying,
    state_affiliation boolean DEFAULT false NOT NULL,
    CONSTRAINT source_credibility_score_check CHECK (((score >= (0.0)::double precision) AND (score <= (1.0)::double precision))),
    CONSTRAINT source_credibility_tier_check CHECK (((tier)::text = ANY (ARRAY[('wire'::character varying)::text, ('gov'::character varying)::text, ('aggregator'::character varying)::text, ('thinktank'::character varying)::text, ('social'::character varying)::text])))
);


ALTER TABLE public.source_credibility OWNER TO legba;

--
-- Name: source_descriptors; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.source_descriptors (
    descriptor_id text NOT NULL,
    version text NOT NULL,
    schema_uri text NOT NULL,
    is_head boolean DEFAULT true NOT NULL,
    abstraction_level text DEFAULT 'L1'::text NOT NULL,
    kind text NOT NULL,
    state text DEFAULT 'draft'::text NOT NULL,
    owner text NOT NULL,
    name text NOT NULL,
    body jsonb NOT NULL,
    inherits text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retire_after timestamp with time zone
);


ALTER TABLE public.source_descriptors OWNER TO legba;

--
-- Name: source_poll_outcomes; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.source_poll_outcomes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_id text NOT NULL,
    source_version text,
    owner_tenant text DEFAULT 'default'::text NOT NULL,
    outcome text NOT NULL,
    health_state text,
    capped boolean DEFAULT false NOT NULL,
    signals_written integer DEFAULT 0 NOT NULL,
    error text,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT source_poll_outcomes_outcome_chk CHECK ((outcome = ANY (ARRAY['empty'::text, 'error'::text])))
);


ALTER TABLE public.source_poll_outcomes OWNER TO legba;

--
-- Name: stack_components; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.stack_components (
    component_id text NOT NULL,
    version text NOT NULL,
    schema_uri text NOT NULL,
    kind text NOT NULL,
    is_head boolean DEFAULT true NOT NULL,
    state text DEFAULT 'draft'::text NOT NULL,
    owner text NOT NULL,
    name text NOT NULL,
    body jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.stack_components OWNER TO legba;

--
-- Name: stack_credentials; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.stack_credentials (
    secret_id text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    is_current boolean DEFAULT true NOT NULL,
    nonce bytea NOT NULL,
    ciphertext bytea NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text NOT NULL,
    notes text
);


ALTER TABLE public.stack_credentials OWNER TO legba;

--
-- Name: target_descriptors; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.target_descriptors (
    descriptor_id text NOT NULL,
    version text NOT NULL,
    schema_uri text NOT NULL,
    is_head boolean DEFAULT true NOT NULL,
    abstraction_level text DEFAULT 'L1'::text NOT NULL,
    state text DEFAULT 'draft'::text NOT NULL,
    owner text NOT NULL,
    name text NOT NULL,
    body jsonb NOT NULL,
    inherits text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retire_after timestamp with time zone
);


ALTER TABLE public.target_descriptors OWNER TO legba;

--
-- Name: trigger_state; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.trigger_state (
    analyst_id text NOT NULL,
    target_id text NOT NULL,
    tenant text DEFAULT 'default'::text NOT NULL,
    pending_count integer DEFAULT 0 NOT NULL,
    max_pending_rank integer DEFAULT '-1'::integer NOT NULL,
    last_fired_at timestamp with time zone,
    first_dirty_at timestamp with time zone,
    seen_signal_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    fire_count bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.trigger_state OWNER TO legba;

--
-- Name: ui_panel_registrations; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.ui_panel_registrations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    panel_id text NOT NULL,
    descriptor_id text NOT NULL,
    descriptor_version text NOT NULL,
    descriptor_family text NOT NULL,
    analyst_id text,
    title text NOT NULL,
    mode text NOT NULL,
    data_query jsonb DEFAULT '{}'::jsonb NOT NULL,
    layout_slot text NOT NULL,
    binding jsonb DEFAULT '{}'::jsonb NOT NULL,
    retired boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone
);


ALTER TABLE public.ui_panel_registrations OWNER TO legba;

--
-- Name: vocabulary_entries; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.vocabulary_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    family text NOT NULL,
    value text NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/vocabulary/jsonschema/1-0-0'::text NOT NULL,
    introduced timestamp with time zone DEFAULT now() NOT NULL,
    deprecated timestamp with time zone,
    notes text,
    aliases text[] DEFAULT '{}'::text[] NOT NULL,
    parent text
);


ALTER TABLE public.vocabulary_entries OWNER TO legba;

--
-- Name: wiring_descriptors; Type: TABLE; Schema: public; Owner: legba
--

CREATE TABLE public.wiring_descriptors (
    descriptor_id text NOT NULL,
    version text NOT NULL,
    schema_uri text NOT NULL,
    is_head boolean DEFAULT true NOT NULL,
    state text DEFAULT 'draft'::text NOT NULL,
    owner text NOT NULL,
    name text NOT NULL,
    body jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.wiring_descriptors OWNER TO legba;

--
-- Name: action_pack_descriptors action_pack_descriptors_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.action_pack_descriptors
    ADD CONSTRAINT action_pack_descriptors_pkey PRIMARY KEY (descriptor_id, version);


--
-- Name: action_pack_invocations action_pack_invocations_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.action_pack_invocations
    ADD CONSTRAINT action_pack_invocations_pkey PRIMARY KEY (id);


--
-- Name: acute_forecasts acute_forecasts_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.acute_forecasts
    ADD CONSTRAINT acute_forecasts_pkey PRIMARY KEY (id);


--
-- Name: alert_sink_deliveries alert_sink_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.alert_sink_deliveries
    ADD CONSTRAINT alert_sink_deliveries_pkey PRIMARY KEY (id);


--
-- Name: analyst_critiques analyst_critiques_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.analyst_critiques
    ADD CONSTRAINT analyst_critiques_pkey PRIMARY KEY (id);


--
-- Name: analyst_descriptors analyst_descriptors_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.analyst_descriptors
    ADD CONSTRAINT analyst_descriptors_pkey PRIMARY KEY (descriptor_id, version);


--
-- Name: analyst_outputs analyst_outputs_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.analyst_outputs
    ADD CONSTRAINT analyst_outputs_pkey PRIMARY KEY (id);


--
-- Name: analyst_traces analyst_traces_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.analyst_traces
    ADD CONSTRAINT analyst_traces_pkey PRIMARY KEY (run_id);


--
-- Name: audit_checkpoints audit_checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.audit_checkpoints
    ADD CONSTRAINT audit_checkpoints_pkey PRIMARY KEY (id);


--
-- Name: budget_demotion_events budget_demotion_events_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.budget_demotion_events
    ADD CONSTRAINT budget_demotion_events_pkey PRIMARY KEY (id);


--
-- Name: budget_ledger budget_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.budget_ledger
    ADD CONSTRAINT budget_ledger_pkey PRIMARY KEY (analyst_id, analyst_version, bucket);


--
-- Name: consult_sessions consult_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.consult_sessions
    ADD CONSTRAINT consult_sessions_pkey PRIMARY KEY (id);


--
-- Name: consult_turns consult_turns_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.consult_turns
    ADD CONSTRAINT consult_turns_pkey PRIMARY KEY (id);


--
-- Name: conversion_executions conversion_executions_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.conversion_executions
    ADD CONSTRAINT conversion_executions_pkey PRIMARY KEY (id);


--
-- Name: conversion_webhooks conversion_webhooks_from_uri_to_uri_key; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.conversion_webhooks
    ADD CONSTRAINT conversion_webhooks_from_uri_to_uri_key UNIQUE (from_uri, to_uri);


--
-- Name: conversion_webhooks conversion_webhooks_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.conversion_webhooks
    ADD CONSTRAINT conversion_webhooks_pkey PRIMARY KEY (id);


--
-- Name: descriptor_audit_log descriptor_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.descriptor_audit_log
    ADD CONSTRAINT descriptor_audit_log_pkey PRIMARY KEY (id);


--
-- Name: descriptor_conversion_archives descriptor_conversion_archives_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.descriptor_conversion_archives
    ADD CONSTRAINT descriptor_conversion_archives_pkey PRIMARY KEY (id);


--
-- Name: descriptor_dead_letter descriptor_dead_letter_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.descriptor_dead_letter
    ADD CONSTRAINT descriptor_dead_letter_pkey PRIMARY KEY (id);


--
-- Name: discovery_state discovery_state_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.discovery_state
    ADD CONSTRAINT discovery_state_pkey PRIMARY KEY (discovery_id, natural_key);


--
-- Name: entity_profile_versions entity_profile_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.entity_profile_versions
    ADD CONSTRAINT entity_profile_versions_pkey PRIMARY KEY (id);


--
-- Name: entity_profiles entity_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.entity_profiles
    ADD CONSTRAINT entity_profiles_pkey PRIMARY KEY (id);


--
-- Name: facts facts_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.facts
    ADD CONSTRAINT facts_pkey PRIMARY KEY (id);


--
-- Name: finding_supersessions finding_supersessions_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.finding_supersessions
    ADD CONSTRAINT finding_supersessions_pkey PRIMARY KEY (superseded_finding_id, superseding_finding_id);


--
-- Name: global_budget_envelope global_budget_envelope_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.global_budget_envelope
    ADD CONSTRAINT global_budget_envelope_pkey PRIMARY KEY (bucket);


--
-- Name: governor_events governor_events_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.governor_events
    ADD CONSTRAINT governor_events_pkey PRIMARY KEY (id);


--
-- Name: graph_metrics graph_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.graph_metrics
    ADD CONSTRAINT graph_metrics_pkey PRIMARY KEY (metric_kind, computed_at);


--
-- Name: hypotheses hypotheses_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.hypotheses
    ADD CONSTRAINT hypotheses_pkey PRIMARY KEY (id);


--
-- Name: iso_countries iso_countries_iso3_key; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.iso_countries
    ADD CONSTRAINT iso_countries_iso3_key UNIQUE (iso3);


--
-- Name: iso_countries iso_countries_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.iso_countries
    ADD CONSTRAINT iso_countries_pkey PRIMARY KEY (iso2);


--
-- Name: journal_entries journal_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.journal_entries
    ADD CONSTRAINT journal_entries_pkey PRIMARY KEY (id);


--
-- Name: journal_proposals journal_proposals_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.journal_proposals
    ADD CONSTRAINT journal_proposals_pkey PRIMARY KEY (id);


--
-- Name: legba_data_migrations legba_data_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.legba_data_migrations
    ADD CONSTRAINT legba_data_migrations_pkey PRIMARY KEY (name);


--
-- Name: nexuses nexuses_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.nexuses
    ADD CONSTRAINT nexuses_pkey PRIMARY KEY (id);


--
-- Name: output_dead_letter output_dead_letter_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.output_dead_letter
    ADD CONSTRAINT output_dead_letter_pkey PRIMARY KEY (id);


--
-- Name: proposed_edges proposed_edges_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.proposed_edges
    ADD CONSTRAINT proposed_edges_pkey PRIMARY KEY (id);


--
-- Name: seed_batches seed_batches_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.seed_batches
    ADD CONSTRAINT seed_batches_pkey PRIMARY KEY (id);


--
-- Name: signal_aliases signal_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.signal_aliases
    ADD CONSTRAINT signal_aliases_pkey PRIMARY KEY (alias_signal_id, canonical_signal_id);


--
-- Name: signal_entity_links signal_entity_links_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.signal_entity_links
    ADD CONSTRAINT signal_entity_links_pkey PRIMARY KEY (signal_id, entity_id, role);


--
-- Name: signals signals_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.signals
    ADD CONSTRAINT signals_pkey PRIMARY KEY (id);


--
-- Name: situations situations_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.situations
    ADD CONSTRAINT situations_pkey PRIMARY KEY (id);


--
-- Name: source_credibility source_credibility_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.source_credibility
    ADD CONSTRAINT source_credibility_pkey PRIMARY KEY (source_host);


--
-- Name: source_descriptors source_descriptors_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.source_descriptors
    ADD CONSTRAINT source_descriptors_pkey PRIMARY KEY (descriptor_id, version);


--
-- Name: source_poll_outcomes source_poll_outcomes_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.source_poll_outcomes
    ADD CONSTRAINT source_poll_outcomes_pkey PRIMARY KEY (id);


--
-- Name: stack_components stack_components_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.stack_components
    ADD CONSTRAINT stack_components_pkey PRIMARY KEY (component_id, version);


--
-- Name: stack_credentials stack_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.stack_credentials
    ADD CONSTRAINT stack_credentials_pkey PRIMARY KEY (secret_id, version);


--
-- Name: target_descriptors target_descriptors_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.target_descriptors
    ADD CONSTRAINT target_descriptors_pkey PRIMARY KEY (descriptor_id, version);


--
-- Name: trigger_state trigger_state_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.trigger_state
    ADD CONSTRAINT trigger_state_pkey PRIMARY KEY (analyst_id, target_id);


--
-- Name: ui_panel_registrations ui_panel_registrations_descriptor_id_descriptor_version_pan_key; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.ui_panel_registrations
    ADD CONSTRAINT ui_panel_registrations_descriptor_id_descriptor_version_pan_key UNIQUE (descriptor_id, descriptor_version, panel_id);


--
-- Name: ui_panel_registrations ui_panel_registrations_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.ui_panel_registrations
    ADD CONSTRAINT ui_panel_registrations_pkey PRIMARY KEY (id);


--
-- Name: vocabulary_entries vocabulary_entries_family_value_key; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.vocabulary_entries
    ADD CONSTRAINT vocabulary_entries_family_value_key UNIQUE (family, value);


--
-- Name: vocabulary_entries vocabulary_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.vocabulary_entries
    ADD CONSTRAINT vocabulary_entries_pkey PRIMARY KEY (id);


--
-- Name: wiring_descriptors wiring_descriptors_pkey; Type: CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.wiring_descriptors
    ADD CONSTRAINT wiring_descriptors_pkey PRIMARY KEY (descriptor_id, version);


--
-- Name: action_pack_descriptors_head_unique; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX action_pack_descriptors_head_unique ON public.action_pack_descriptors USING btree (descriptor_id) WHERE is_head;


--
-- Name: action_pack_descriptors_schema_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX action_pack_descriptors_schema_idx ON public.action_pack_descriptors USING btree (schema_uri);


--
-- Name: action_pack_descriptors_state_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX action_pack_descriptors_state_idx ON public.action_pack_descriptors USING btree (state);


--
-- Name: action_pack_invocations_account_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX action_pack_invocations_account_idx ON public.action_pack_invocations USING btree (budget_account, occurred_at DESC);


--
-- Name: action_pack_invocations_tool_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX action_pack_invocations_tool_idx ON public.action_pack_invocations USING btree (pack_id, tool_name);


--
-- Name: action_pack_invocations_window_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX action_pack_invocations_window_idx ON public.action_pack_invocations USING btree (pack_id, budget_account, occurred_at DESC);


--
-- Name: acute_forecasts_open_window_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX acute_forecasts_open_window_idx ON public.acute_forecasts USING btree (window_end) WHERE (resolved_outcome IS NULL);


--
-- Name: acute_forecasts_region_class_window_uq; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX acute_forecasts_region_class_window_uq ON public.acute_forecasts USING btree (region, event_class, window_start);


--
-- Name: acute_forecasts_resolved_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX acute_forecasts_resolved_idx ON public.acute_forecasts USING btree (resolved_at) WHERE (resolved_outcome IS NOT NULL);


--
-- Name: analyst_critiques_judge_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX analyst_critiques_judge_idx ON public.analyst_critiques USING btree (judge_analyst_id, produced_at DESC);


--
-- Name: analyst_critiques_trace_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX analyst_critiques_trace_idx ON public.analyst_critiques USING btree (trace_id);


--
-- Name: analyst_descriptors_head_unique; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX analyst_descriptors_head_unique ON public.analyst_descriptors USING btree (descriptor_id) WHERE is_head;


--
-- Name: analyst_descriptors_kind_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX analyst_descriptors_kind_idx ON public.analyst_descriptors USING btree (kind);


--
-- Name: analyst_descriptors_state_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX analyst_descriptors_state_idx ON public.analyst_descriptors USING btree (state);


--
-- Name: analyst_traces_analyst_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX analyst_traces_analyst_idx ON public.analyst_traces USING btree (analyst_id, run_started_at DESC);


--
-- Name: analyst_traces_input_gin; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX analyst_traces_input_gin ON public.analyst_traces USING gin (input_row_refs);


--
-- Name: analyst_traces_output_gin; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX analyst_traces_output_gin ON public.analyst_traces USING gin (output_row_refs);


--
-- Name: analyst_traces_status_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX analyst_traces_status_idx ON public.analyst_traces USING btree (status) WHERE (status <> 'success'::text);


--
-- Name: analyst_traces_target_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX analyst_traces_target_idx ON public.analyst_traces USING btree (target_id, run_started_at DESC) WHERE (target_id IS NOT NULL);


--
-- Name: audit_checkpoints_analyst_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX audit_checkpoints_analyst_idx ON public.audit_checkpoints USING btree (analyst_id, checkpointed_at DESC);


--
-- Name: budget_demotion_events_analyst_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX budget_demotion_events_analyst_idx ON public.budget_demotion_events USING btree (analyst_id, bucket DESC);


--
-- Name: budget_demotion_events_bucket_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX budget_demotion_events_bucket_idx ON public.budget_demotion_events USING btree (bucket DESC, occurred_at DESC);


--
-- Name: budget_ledger_bucket_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX budget_ledger_bucket_idx ON public.budget_ledger USING btree (bucket DESC);


--
-- Name: budget_ledger_cost_estimate_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX budget_ledger_cost_estimate_idx ON public.budget_ledger USING btree (bucket DESC, cost_estimate_usd DESC) WHERE (cost_estimate_usd > (0)::numeric);


--
-- Name: conversion_executions_descriptor_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX conversion_executions_descriptor_idx ON public.conversion_executions USING btree (namespace, descriptor_id, executed_at DESC);


--
-- Name: conversion_executions_failed_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX conversion_executions_failed_idx ON public.conversion_executions USING btree (executed_at DESC) WHERE (success = false);


--
-- Name: conversion_webhooks_active_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX conversion_webhooks_active_idx ON public.conversion_webhooks USING btree (from_uri, to_uri) WHERE (retired_at IS NULL);


--
-- Name: conversion_webhooks_from_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX conversion_webhooks_from_idx ON public.conversion_webhooks USING btree (from_uri);


--
-- Name: conversion_webhooks_to_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX conversion_webhooks_to_idx ON public.conversion_webhooks USING btree (to_uri);


--
-- Name: descriptor_audit_action_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX descriptor_audit_action_idx ON public.descriptor_audit_log USING btree (action, occurred_at DESC);


--
-- Name: descriptor_audit_actor_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX descriptor_audit_actor_idx ON public.descriptor_audit_log USING btree (actor_id, occurred_at DESC);


--
-- Name: descriptor_audit_descriptor_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX descriptor_audit_descriptor_idx ON public.descriptor_audit_log USING btree (descriptor_id, occurred_at DESC);


--
-- Name: descriptor_conversion_archives_descriptor_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX descriptor_conversion_archives_descriptor_idx ON public.descriptor_conversion_archives USING btree (namespace, descriptor_id, archived_at DESC);


--
-- Name: descriptor_conversion_archives_webhook_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX descriptor_conversion_archives_webhook_idx ON public.descriptor_conversion_archives USING btree (webhook_id);


--
-- Name: descriptor_dl_ns_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX descriptor_dl_ns_idx ON public.descriptor_dead_letter USING btree (namespace, attempted_at DESC);


--
-- Name: descriptor_dl_open_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX descriptor_dl_open_idx ON public.descriptor_dead_letter USING btree (attempted_at DESC) WHERE (resolution IS NULL);


--
-- Name: discovery_state_descriptor_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX discovery_state_descriptor_idx ON public.discovery_state USING btree (descriptor_id);


--
-- Name: discovery_state_discovery_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX discovery_state_discovery_idx ON public.discovery_state USING btree (discovery_id);


--
-- Name: discovery_state_family_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX discovery_state_family_idx ON public.discovery_state USING btree (family);


--
-- Name: finding_supersessions_situation_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX finding_supersessions_situation_idx ON public.finding_supersessions USING btree (situation_signature);


--
-- Name: finding_supersessions_superseding_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX finding_supersessions_superseding_idx ON public.finding_supersessions USING btree (superseding_finding_id);


--
-- Name: governor_events_account_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX governor_events_account_idx ON public.governor_events USING btree (budget_account, occurred_at DESC);


--
-- Name: governor_events_decision_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX governor_events_decision_idx ON public.governor_events USING btree (decision, occurred_at DESC);


--
-- Name: governor_events_pack_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX governor_events_pack_idx ON public.governor_events USING btree (pack_id, occurred_at DESC);


--
-- Name: graph_metrics_kind_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX graph_metrics_kind_idx ON public.graph_metrics USING btree (metric_kind, computed_at DESC);


--
-- Name: idx_analyst_outputs_analyst_id; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_analyst_outputs_analyst_id ON public.analyst_outputs USING btree (analyst_id) WHERE (analyst_id IS NOT NULL);


--
-- Name: idx_analyst_outputs_derived_from; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_analyst_outputs_derived_from ON public.analyst_outputs USING gin (derived_from);


--
-- Name: idx_analyst_outputs_kind; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_analyst_outputs_kind ON public.analyst_outputs USING btree (kind, produced_at DESC);


--
-- Name: idx_analyst_outputs_produced_at; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_analyst_outputs_produced_at ON public.analyst_outputs USING btree (produced_at DESC);


--
-- Name: idx_analyst_outputs_run_id; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_analyst_outputs_run_id ON public.analyst_outputs USING btree (run_id) WHERE (run_id IS NOT NULL);


--
-- Name: idx_analyst_outputs_severity; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_analyst_outputs_severity ON public.analyst_outputs USING btree (severity) WHERE ((severity IS NOT NULL) AND (severity = ANY (ARRAY['high'::text, 'critical'::text])));


--
-- Name: idx_analyst_outputs_situation_latest; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_analyst_outputs_situation_latest ON public.analyst_outputs USING btree (situation_signature) WHERE ((situation_signature IS NOT NULL) AND (superseded_by IS NULL));


--
-- Name: idx_analyst_outputs_situation_signature; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_analyst_outputs_situation_signature ON public.analyst_outputs USING btree (situation_signature) WHERE (situation_signature IS NOT NULL);


--
-- Name: idx_analyst_outputs_superseded_by; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_analyst_outputs_superseded_by ON public.analyst_outputs USING btree (superseded_by) WHERE (superseded_by IS NOT NULL);


--
-- Name: idx_analyst_outputs_target_id; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_analyst_outputs_target_id ON public.analyst_outputs USING btree (target_id) WHERE (target_id IS NOT NULL);


--
-- Name: idx_asd_alert_row; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_asd_alert_row ON public.alert_sink_deliveries USING btree (alert_row_id);


--
-- Name: idx_asd_attempted_at; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_asd_attempted_at ON public.alert_sink_deliveries USING btree (attempted_at DESC);


--
-- Name: idx_asd_sink_status; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_asd_sink_status ON public.alert_sink_deliveries USING btree (sink_kind, status);


--
-- Name: idx_consult_sessions_task; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_consult_sessions_task ON public.consult_sessions USING btree (task_id) WHERE (task_id IS NOT NULL);


--
-- Name: idx_consult_sessions_updated; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_consult_sessions_updated ON public.consult_sessions USING btree (updated_at DESC);


--
-- Name: idx_consult_turns_session_created; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_consult_turns_session_created ON public.consult_turns USING btree (session_id, created_at);


--
-- Name: idx_entity_profiles_analyst_id; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_entity_profiles_analyst_id ON public.entity_profiles USING btree (analyst_id) WHERE (analyst_id IS NOT NULL);


--
-- Name: idx_entity_profiles_class; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_entity_profiles_class ON public.entity_profiles USING btree (entity_class);


--
-- Name: idx_entity_profiles_derived_from; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_entity_profiles_derived_from ON public.entity_profiles USING gin (derived_from);


--
-- Name: idx_entity_profiles_name_class; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX idx_entity_profiles_name_class ON public.entity_profiles USING btree (lower(canonical_name), entity_class);


--
-- Name: idx_entity_profiles_produced_at; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_entity_profiles_produced_at ON public.entity_profiles USING btree (produced_at DESC);


--
-- Name: idx_entity_profiles_target_id; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_entity_profiles_target_id ON public.entity_profiles USING btree (target_id) WHERE (target_id IS NOT NULL);


--
-- Name: idx_epv_created; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_epv_created ON public.entity_profile_versions USING btree (entity_id, created_at DESC);


--
-- Name: idx_epv_entity; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_epv_entity ON public.entity_profile_versions USING btree (entity_id, version DESC);


--
-- Name: idx_facts_analyst_id; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_facts_analyst_id ON public.facts USING btree (analyst_id) WHERE (analyst_id IS NOT NULL);


--
-- Name: idx_facts_decay_sweep; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_facts_decay_sweep ON public.facts USING btree (updated_at) WHERE (superseded_by IS NULL);


--
-- Name: idx_facts_derived_from; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_facts_derived_from ON public.facts USING gin (derived_from);


--
-- Name: idx_facts_open_subject_predicate; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_facts_open_subject_predicate ON public.facts USING btree (lower(subject), lower(predicate)) WHERE ((valid_until IS NULL) AND (superseded_by IS NULL));


--
-- Name: idx_facts_predicate; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_facts_predicate ON public.facts USING btree (predicate);


--
-- Name: idx_facts_produced_at; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_facts_produced_at ON public.facts USING btree (produced_at DESC);


--
-- Name: idx_facts_seed_batch; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_facts_seed_batch ON public.facts USING btree (seed_batch_id) WHERE (seed_batch_id IS NOT NULL);


--
-- Name: idx_facts_subject; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_facts_subject ON public.facts USING btree (subject);


--
-- Name: idx_facts_target_id; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_facts_target_id ON public.facts USING btree (target_id) WHERE (target_id IS NOT NULL);


--
-- Name: idx_facts_temporal_triple_open; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX idx_facts_temporal_triple_open ON public.facts USING btree (lower(subject), lower(predicate), lower(value), COALESCE(valid_from, '1970-01-01 00:00:00+00'::timestamp with time zone)) WHERE ((valid_until IS NULL) AND (superseded_by IS NULL));


--
-- Name: idx_hypotheses_analyst_id; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_hypotheses_analyst_id ON public.hypotheses USING btree (analyst_id) WHERE (analyst_id IS NOT NULL);


--
-- Name: idx_hypotheses_derived_from; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_hypotheses_derived_from ON public.hypotheses USING gin (derived_from);


--
-- Name: idx_hypotheses_produced_at; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_hypotheses_produced_at ON public.hypotheses USING btree (produced_at DESC);


--
-- Name: idx_hypotheses_resolved_outcome; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_hypotheses_resolved_outcome ON public.hypotheses USING btree (resolved_at) WHERE (resolved_outcome IS NOT NULL);


--
-- Name: idx_hypotheses_situation; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_hypotheses_situation ON public.hypotheses USING btree (situation_id);


--
-- Name: idx_hypotheses_status; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_hypotheses_status ON public.hypotheses USING btree (status);


--
-- Name: idx_hypotheses_target_id; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_hypotheses_target_id ON public.hypotheses USING btree (target_id) WHERE (target_id IS NOT NULL);


--
-- Name: idx_journal_entries_period; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_journal_entries_period ON public.journal_entries USING btree (period_end DESC);


--
-- Name: idx_journal_open_consolidation; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_journal_open_consolidation ON public.journal_entries USING btree (produced_at DESC) WHERE ((entry_kind = 'consolidation'::text) AND (valid_until IS NULL) AND (superseded_by IS NULL));


--
-- Name: idx_journal_proposals_pending; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_journal_proposals_pending ON public.journal_proposals USING btree (produced_at DESC) WHERE (status = 'pending'::text);


--
-- Name: idx_nexuses_decay_sweep; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_nexuses_decay_sweep ON public.nexuses USING btree (created_at) WHERE (superseded_by IS NULL);


--
-- Name: idx_nexuses_open_triple; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_nexuses_open_triple ON public.nexuses USING btree (lower(subject), lower(COALESCE(intermediary, ''::text)), lower(object)) WHERE ((valid_until IS NULL) AND (superseded_by IS NULL));


--
-- Name: idx_nexuses_seed_batch; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_nexuses_seed_batch ON public.nexuses USING btree (seed_batch_id) WHERE (seed_batch_id IS NOT NULL);


--
-- Name: idx_nexuses_signed_open; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_nexuses_signed_open ON public.nexuses USING btree (rel_type) WHERE ((valid_until IS NULL) AND (superseded_by IS NULL) AND (polarity <> 0));


--
-- Name: idx_nexuses_triple_open; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX idx_nexuses_triple_open ON public.nexuses USING btree (lower(subject), lower(COALESCE(intermediary, ''::text)), lower(object), lower(rel_type)) WHERE ((valid_until IS NULL) AND (superseded_by IS NULL));


--
-- Name: idx_proposed_edges_status; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_proposed_edges_status ON public.proposed_edges USING btree (status);


--
-- Name: idx_seed_batches_source; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_seed_batches_source ON public.seed_batches USING btree (source, imported_at DESC);


--
-- Name: idx_sel_entity; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_sel_entity ON public.signal_entity_links USING btree (entity_id);


--
-- Name: idx_sel_signal_entity; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_sel_signal_entity ON public.signal_entity_links USING btree (signal_id);


--
-- Name: idx_signals_entities_unresolved; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_signals_entities_unresolved ON public.signals USING btree (fetched_at) WHERE (entities_resolved_at IS NULL);


--
-- Name: idx_signals_retention_fetched_at; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_signals_retention_fetched_at ON public.signals USING btree (retention_class, fetched_at);


--
-- Name: idx_situations_analyst_id; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_situations_analyst_id ON public.situations USING btree (analyst_id) WHERE (analyst_id IS NOT NULL);


--
-- Name: idx_situations_derived_from; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_situations_derived_from ON public.situations USING gin (derived_from);


--
-- Name: idx_situations_open_grounding; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_situations_open_grounding ON public.situations USING btree (intensity_score DESC) WHERE ((status <> 'closed'::text) AND (superseded_by IS NULL));


--
-- Name: idx_situations_produced_at; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_situations_produced_at ON public.situations USING btree (produced_at DESC);


--
-- Name: idx_situations_status; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_situations_status ON public.situations USING btree (status);


--
-- Name: idx_situations_target_id; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_situations_target_id ON public.situations USING btree (target_id) WHERE (target_id IS NOT NULL);


--
-- Name: idx_source_credibility_last_updated; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_source_credibility_last_updated ON public.source_credibility USING btree (last_updated DESC);


--
-- Name: idx_source_credibility_score; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX idx_source_credibility_score ON public.source_credibility USING btree (score);


--
-- Name: iso_countries_region_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX iso_countries_region_idx ON public.iso_countries USING btree (region);


--
-- Name: iso_countries_subregion_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX iso_countries_subregion_idx ON public.iso_countries USING btree (subregion);


--
-- Name: output_dl_analyst_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX output_dl_analyst_idx ON public.output_dead_letter USING btree (analyst_id, produced_at DESC);


--
-- Name: output_dl_open_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX output_dl_open_idx ON public.output_dead_letter USING btree (produced_at DESC) WHERE (resolution IS NULL);


--
-- Name: signal_aliases_canonical_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signal_aliases_canonical_idx ON public.signal_aliases USING btree (canonical_signal_id);


--
-- Name: signals_canonical_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signals_canonical_idx ON public.signals USING btree (canonical_signal_id);


--
-- Name: signals_content_hash_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signals_content_hash_idx ON public.signals USING btree (content_hash);


--
-- Name: signals_derived_from_gin; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signals_derived_from_gin ON public.signals USING gin (derived_from);


--
-- Name: signals_entity_classes_gin; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signals_entity_classes_gin ON public.signals USING gin (entity_classes);


--
-- Name: signals_fetched_at_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signals_fetched_at_idx ON public.signals USING btree (fetched_at DESC);


--
-- Name: signals_geo_gin; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signals_geo_gin ON public.signals USING gin (geo);


--
-- Name: signals_language_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signals_language_idx ON public.signals USING btree (language);


--
-- Name: signals_modality_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signals_modality_idx ON public.signals USING btree (modality);


--
-- Name: signals_owner_tenant_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signals_owner_tenant_idx ON public.signals USING btree (owner_tenant);


--
-- Name: signals_source_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signals_source_idx ON public.signals USING btree (source_id);


--
-- Name: signals_tags_gin; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX signals_tags_gin ON public.signals USING gin (tags);


--
-- Name: source_descriptors_head_unique; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX source_descriptors_head_unique ON public.source_descriptors USING btree (descriptor_id) WHERE is_head;


--
-- Name: source_descriptors_kind_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX source_descriptors_kind_idx ON public.source_descriptors USING btree (kind);


--
-- Name: source_descriptors_schema_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX source_descriptors_schema_idx ON public.source_descriptors USING btree (schema_uri);


--
-- Name: source_descriptors_state_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX source_descriptors_state_idx ON public.source_descriptors USING btree (state);


--
-- Name: source_poll_outcomes_source_time_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX source_poll_outcomes_source_time_idx ON public.source_poll_outcomes USING btree (source_id, occurred_at DESC);


--
-- Name: stack_components_head_unique; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX stack_components_head_unique ON public.stack_components USING btree (component_id) WHERE is_head;


--
-- Name: stack_components_kind_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX stack_components_kind_idx ON public.stack_components USING btree (kind);


--
-- Name: stack_components_state_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX stack_components_state_idx ON public.stack_components USING btree (state);


--
-- Name: stack_credentials_created_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX stack_credentials_created_idx ON public.stack_credentials USING btree (created_at DESC);


--
-- Name: stack_credentials_current_unique; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX stack_credentials_current_unique ON public.stack_credentials USING btree (secret_id) WHERE is_current;


--
-- Name: target_descriptors_head_unique; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX target_descriptors_head_unique ON public.target_descriptors USING btree (descriptor_id) WHERE is_head;


--
-- Name: target_descriptors_schema_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX target_descriptors_schema_idx ON public.target_descriptors USING btree (schema_uri);


--
-- Name: target_descriptors_state_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX target_descriptors_state_idx ON public.target_descriptors USING btree (state);


--
-- Name: trigger_state_pending_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX trigger_state_pending_idx ON public.trigger_state USING btree (pending_count) WHERE (pending_count > 0);


--
-- Name: trigger_state_target_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX trigger_state_target_idx ON public.trigger_state USING btree (target_id);


--
-- Name: ui_panel_registrations_analyst_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX ui_panel_registrations_analyst_idx ON public.ui_panel_registrations USING btree (analyst_id) WHERE ((analyst_id IS NOT NULL) AND (retired = false));


--
-- Name: ui_panel_registrations_descriptor_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX ui_panel_registrations_descriptor_idx ON public.ui_panel_registrations USING btree (descriptor_id, descriptor_version);


--
-- Name: ui_panel_registrations_layout_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX ui_panel_registrations_layout_idx ON public.ui_panel_registrations USING btree (layout_slot) WHERE (retired = false);


--
-- Name: ui_panel_registrations_mode_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX ui_panel_registrations_mode_idx ON public.ui_panel_registrations USING btree (mode) WHERE (retired = false);


--
-- Name: ui_panel_registrations_panel_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX ui_panel_registrations_panel_idx ON public.ui_panel_registrations USING btree (panel_id) WHERE (retired = false);


--
-- Name: ui_panel_registrations_slot_unique; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX ui_panel_registrations_slot_unique ON public.ui_panel_registrations USING btree (mode, layout_slot) WHERE (retired = false);


--
-- Name: uq_journal_single_open_consolidation; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX uq_journal_single_open_consolidation ON public.journal_entries USING btree ((true)) WHERE ((entry_kind = 'consolidation'::text) AND (valid_until IS NULL) AND (superseded_by IS NULL));


--
-- Name: uq_proposed_edges_triple; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX uq_proposed_edges_triple ON public.proposed_edges USING btree (lower(source_entity), lower(target_entity), relationship_type);


--
-- Name: uq_situations_signature_analyst; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX uq_situations_signature_analyst ON public.situations USING btree (situation_signature, analyst_id) WHERE (situation_signature IS NOT NULL);


--
-- Name: vocabulary_entries_active_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX vocabulary_entries_active_idx ON public.vocabulary_entries USING btree (family, value) WHERE (deprecated IS NULL);


--
-- Name: vocabulary_entries_family_idx; Type: INDEX; Schema: public; Owner: legba
--

CREATE INDEX vocabulary_entries_family_idx ON public.vocabulary_entries USING btree (family);


--
-- Name: wiring_descriptors_head_unique; Type: INDEX; Schema: public; Owner: legba
--

CREATE UNIQUE INDEX wiring_descriptors_head_unique ON public.wiring_descriptors USING btree (descriptor_id) WHERE is_head;


--
-- Name: alert_sink_deliveries alert_sink_deliveries_alert_row_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.alert_sink_deliveries
    ADD CONSTRAINT alert_sink_deliveries_alert_row_id_fkey FOREIGN KEY (alert_row_id) REFERENCES public.analyst_outputs(id) ON DELETE CASCADE;


--
-- Name: analyst_critiques analyst_critiques_trace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.analyst_critiques
    ADD CONSTRAINT analyst_critiques_trace_id_fkey FOREIGN KEY (trace_id) REFERENCES public.analyst_traces(run_id) ON DELETE CASCADE;


--
-- Name: consult_turns consult_turns_session_fk; Type: FK CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.consult_turns
    ADD CONSTRAINT consult_turns_session_fk FOREIGN KEY (session_id) REFERENCES public.consult_sessions(id) ON DELETE CASCADE;


--
-- Name: descriptor_conversion_archives descriptor_conversion_archives_webhook_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.descriptor_conversion_archives
    ADD CONSTRAINT descriptor_conversion_archives_webhook_id_fkey FOREIGN KEY (webhook_id) REFERENCES public.conversion_webhooks(id);


--
-- Name: entity_profile_versions entity_profile_versions_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.entity_profile_versions
    ADD CONSTRAINT entity_profile_versions_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity_profiles(id) ON DELETE CASCADE;


--
-- Name: hypotheses hypotheses_situation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.hypotheses
    ADD CONSTRAINT hypotheses_situation_id_fkey FOREIGN KEY (situation_id) REFERENCES public.situations(id);


--
-- Name: output_dead_letter output_dead_letter_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.output_dead_letter
    ADD CONSTRAINT output_dead_letter_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.analyst_traces(run_id) ON DELETE SET NULL;


--
-- Name: signal_entity_links signal_entity_links_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: legba
--

ALTER TABLE ONLY public.signal_entity_links
    ADD CONSTRAINT signal_entity_links_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity_profiles(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict 9ne7TpAWavtTpavLOLwNlHisTuouJQUsRub2S2ScG3t3SOV4RthVr8keT7XeKyd



-- ===========================================================================
-- Apache AGE graph: legba_graph
--
-- Built with AGE's OWN catalog functions (create_graph / create_vlabel /
-- create_elabel) so the ag_catalog.ag_graph + ag_catalog.ag_label rows are
-- registered correctly. This REPLACES the pg_dump --schema-only AGE block,
-- which was orphaned (it recreated the per-label tables but not the catalog
-- rows, so the _label_id() defaults failed).
-- ===========================================================================
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

SELECT create_graph('legba_graph');

-- Vertex labels
SELECT create_vlabel('legba_graph', 'Concept');
SELECT create_vlabel('legba_graph', 'Corporation');
SELECT create_vlabel('legba_graph', 'Country');
SELECT create_vlabel('legba_graph', 'Entity');
SELECT create_vlabel('legba_graph', 'Event');
SELECT create_vlabel('legba_graph', 'Location');
SELECT create_vlabel('legba_graph', 'Organization');
SELECT create_vlabel('legba_graph', 'Output');
SELECT create_vlabel('legba_graph', 'Person');
SELECT create_vlabel('legba_graph', 'Software');

-- Edge labels
SELECT create_elabel('legba_graph', 'AffiliatedWith');
SELECT create_elabel('legba_graph', 'AlliedWith');
SELECT create_elabel('legba_graph', 'CoOccursWith');
SELECT create_elabel('legba_graph', 'ConductedVia');
SELECT create_elabel('legba_graph', 'DerivedFrom');
SELECT create_elabel('legba_graph', 'HostileTo');
SELECT create_elabel('legba_graph', 'InvolvedIn');
SELECT create_elabel('legba_graph', 'LeaderOf');
SELECT create_elabel('legba_graph', 'LocatedIn');
SELECT create_elabel('legba_graph', 'MemberOf');
SELECT create_elabel('legba_graph', 'OperatesIn');
SELECT create_elabel('legba_graph', 'PartOf');
SELECT create_elabel('legba_graph', 'PartyTo');
SELECT create_elabel('legba_graph', 'SuppliesWeaponsTo');
SELECT create_elabel('legba_graph', 'Targets');

RESET search_path;


-- ===========================================================================
-- Migration ledger pre-seed
--
-- This baseline collapses the canonical 23-migration history (0001 .. 0053).
-- We record all 23 as already-applied so the migration runner
-- (legba.data.migrate) treats them as done and only runs FUTURE (0054+)
-- migrations. name = idempotency key (legba_data_migrations.name PK);
-- sha256 = the file's content digest, copied from the reference DB.
-- ===========================================================================
INSERT INTO public.legba_data_migrations (name, sha256) VALUES
    ('0001_baseline.sql', '7fccbfcd310bc4b7a327f868a12328b33aeab066e25eca9e8732d5cc471f2a57'),
    ('0032_facts_decay_columns.sql', '35ad876e7dd1d6cc26e72e5be4d8734c0706e52e0514f421896b20e35c38b504'),
    ('0033_nexuses.sql', 'e493277af4a23794e7757d5919b29835f9498ffd1b7f201cc4487235c4f05c24'),
    ('0034_seed_batches.sql', '774128fc3065d777b306a0998561ce532bf74e1e791ddfd865a2ae34fada8ef8'),
    ('0035_entity_profiles_composite_key.sql', '62d8d8f6afd2fe1a96edecf59abfa3dfca18de146ec0f2ebca6aff8211ab4ca6'),
    ('0036_signals_retention.sql', '5c6866a92158f21ff866481172d273eddfdc41595ed0dcb530cd38813c557042'),
    ('0037_age_output_label.sql', '6f3eeab8a61efc39af5d7202b4322af2bea92b41e4311339f4760e22c591783f'),
    ('0038_hypotheses_resolved_outcome.sql', '825daf0dbf3fbcae83304e74ecde8c8626da9e6f37f88eac915707622814bc28'),
    ('0039_consult_sessions.sql', '752a0efa8a458807f76a79fd42b0abd27b618641f5200d3ea7adf220b28a5f49'),
    ('0040_situations_first_class.sql', '55f0341fe13cbca01204a95117dfccac49d1c667ed2d6f51c587259749d023ad'),
    ('0041_situations_valid_from_repair.sql', '700f84aa7e6d237ef8fe959d7578a79cbbf7fcea3f0390ace9617664844cc224'),
    ('0042_situations_target_id_backfill.sql', 'c025a0ebb25e1416320857248fde536d6d616365b3062e2c855c846005aa76c0'),
    ('0043_ingestion_conf1_backfill.sql', '2e58709839e51cf9343b6c7c14047f074fb4e985532a0c3819923ba89841442d'),
    ('0044_purge_ingestion_leader_junk.sql', '3f832a677e172bd1c339a7439f8dfaac442a191c50b2fcb403073a09cd79bf7e'),
    ('0045_backfill_demonym_nexuses.sql', '6f778ad6be36f8dd6fb289dbab00365885e022ab2e04b47270e963b5a9340c26'),
    ('0046_source_poll_outcomes.sql', '4e1b94286b83cf3442f90212dfe6f9f101e05efd6dd9077d2d97672fe72c6905'),
    ('0047_acute_forecasts.sql', 'fbd22f61c35c941713c0afe5377c84cdb24ab4ac3e42ed823305a040de5b8b99'),
    ('0048_journal.sql', 'ab276435e2558655e88c4dded04a6a29ac5752cb644091f806f0af811dd0c95f'),
    ('0049_facts_collapse_dup_open.sql', 'b2e7cae58904933eafe07617e164f621f3732dbdfa9ebaa56983d97dcb31fc45'),
    ('0050_receipt_chain_fork_tombstone.sql', '13d493ba7d4a6cfe5bbe58f679796236eea93574c8491b5a331ce854e0fec9cc'),
    ('0051_prune_dangling_derived_from.sql', '2f04c9238abc49473e6b251e15235ef1664952b14d35e80bf58fab56cbd31ab7'),
    ('0052_remediation_data_cleanup.sql', '28429034b729d7e8c4c6febd7e569d89181e73feece3681c77834939460b481d'),
    ('0053_retire_template_junk_sources.sql', '3f45bfe8a0151c5cf46ace94b0b730a26dcb84b8e3068d16aa6bfc1ccb42bfc3')
ON CONFLICT (name) DO NOTHING;
