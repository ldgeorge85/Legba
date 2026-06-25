-- 0001_baseline.sql — flattened baseline schema (former migrations 0001..0031).
--
-- Clean-slate release: no live instances to upgrade, so the 30-step historical
-- chain was flattened to one baseline. Derived by pg_dump --column-inserts of a
-- fresh full-chain migrate (schema + seed data); the Apache AGE graph setup is
-- carried VERBATIM from the former 0004 (pg_dump cannot reproduce create_graph/
-- create_vlabel/create_elabel). pg17 \restrict locks stripped + CREATE SCHEMA
-- public made idempotent for the asyncpg runner. History remains in git.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ===== Apache AGE graph + labels (verbatim former 0004) =====
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'legba_graph'
    ) THEN
        PERFORM ag_catalog.create_graph('legba_graph');
    END IF;
END
$$;

-- Vertex labels — 9 retained entity_classes (PascalCase).
-- AGE's create_vlabel takes (cstring, cstring), so cast explicitly.
DO $$
DECLARE
    lbl TEXT;
    vlabels TEXT[] := ARRAY[
        'Entity', 'Location', 'Organization', 'Person', 'Event',
        'Country', 'Concept', 'Corporation', 'Software'
    ];
BEGIN
    FOREACH lbl IN ARRAY vlabels LOOP
        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label
            WHERE name = lbl
              AND graph = (SELECT graphid FROM ag_catalog.ag_graph WHERE name = 'legba_graph')
        ) THEN
            EXECUTE format('SELECT ag_catalog.create_vlabel(%L, %L)', 'legba_graph', lbl);
        END IF;
    END LOOP;
END
$$;

-- Edge labels — 14 retained relationship_types (PascalCase).
DO $$
DECLARE
    lbl TEXT;
    elabels TEXT[] := ARRAY[
        'HostileTo', 'LocatedIn', 'AlliedWith', 'PartyTo', 'Targets',
        'OperatesIn', 'MemberOf', 'LeaderOf', 'ConductedVia',
        'SuppliesWeaponsTo', 'PartOf', 'CoOccursWith', 'AffiliatedWith',
        'InvolvedIn'
    ];
BEGIN
    FOREACH lbl IN ARRAY elabels LOOP
        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label
            WHERE name = lbl
              AND graph = (SELECT graphid FROM ag_catalog.ag_graph WHERE name = 'legba_graph')
        ) THEN
            EXECUTE format('SELECT ag_catalog.create_elabel(%L, %L)', 'legba_graph', lbl);
        END IF;
    END LOOP;
END
$$;

-- ===== public schema + seed data (pg_dump of the full chain) =====
--
-- PostgreSQL database dump
--


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
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS public;


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: action_pack_descriptors; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: action_pack_invocations; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: alert_sink_deliveries; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: analyst_critiques; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: analyst_descriptors; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: analyst_outputs; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: analyst_traces; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: audit_checkpoints; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: budget_demotion_events; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: budget_ledger; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: conversion_executions; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: conversion_webhooks; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: descriptor_audit_log; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: descriptor_conversion_archives; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: descriptor_dead_letter; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: discovery_state; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: entity_profile_versions; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: entity_profiles; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: facts; Type: TABLE; Schema: public; Owner: -
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
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: finding_supersessions; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: global_budget_envelope; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: governor_events; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: graph_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.graph_metrics (
    metric_kind text NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    analyst_id text,
    analyst_version text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    schema_uri text DEFAULT 'iglu:legba/graph_metric/jsonschema/1-0-0'::text NOT NULL
);


--
-- Name: hypotheses; Type: TABLE; Schema: public; Owner: -
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
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: iso_countries; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: output_dead_letter; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: proposed_edges; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: signal_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.signal_aliases (
    alias_signal_id uuid NOT NULL,
    canonical_signal_id uuid NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    score real,
    produced_by text,
    produced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: signal_entity_links; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: signals; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: situations; Type: TABLE; Schema: public; Owner: -
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
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: source_credibility; Type: TABLE; Schema: public; Owner: -
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
    CONSTRAINT source_credibility_tier_check CHECK (((tier)::text = ANY ((ARRAY['wire'::character varying, 'gov'::character varying, 'aggregator'::character varying, 'thinktank'::character varying, 'social'::character varying])::text[])))
);


--
-- Name: source_descriptors; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: stack_components; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: stack_credentials; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: target_descriptors; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: trigger_state; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: ui_panel_registrations; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: vocabulary_entries; Type: TABLE; Schema: public; Owner: -
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


--
-- Name: wiring_descriptors; Type: TABLE; Schema: public; Owner: -
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


--
-- Data for Name: action_pack_descriptors; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: action_pack_invocations; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: alert_sink_deliveries; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: analyst_critiques; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: analyst_descriptors; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: analyst_outputs; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: analyst_traces; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: audit_checkpoints; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: budget_demotion_events; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: budget_ledger; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: conversion_executions; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: conversion_webhooks; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: descriptor_audit_log; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: descriptor_conversion_archives; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: descriptor_dead_letter; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: discovery_state; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: entity_profile_versions; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: entity_profiles; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: facts; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: finding_supersessions; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: global_budget_envelope; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: governor_events; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: graph_metrics; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: hypotheses; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: iso_countries; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AD', 'AND', '020', 'Andorra', 'Principality of Andorra', 'Europe', 'Southern Europe', '["ca-AD"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AE', 'ARE', '784', 'United Arab Emirates', 'United Arab Emirates', 'Asia', 'Western Asia', '["ar-AE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AF', 'AFG', '004', 'Afghanistan', 'Islamic Republic of Afghanistan', 'Asia', 'Southern Asia', '["ps-AF", "fa-AF"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AG', 'ATG', '028', 'Antigua and Barbuda', 'Antigua and Barbuda', 'Americas', 'Latin America and the Caribbean', '["en-AG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AI', 'AIA', '660', 'Anguilla', 'Anguilla', 'Americas', 'Latin America and the Caribbean', '["en-AI"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AL', 'ALB', '008', 'Albania', 'Republic of Albania', 'Europe', 'Southern Europe', '["sq-AL"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AM', 'ARM', '051', 'Armenia', 'Republic of Armenia', 'Asia', 'Western Asia', '["hy-AM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AO', 'AGO', '024', 'Angola', 'Republic of Angola', 'Africa', 'Sub-Saharan Africa', '["pt-AO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AQ', 'ATA', '010', 'Antarctica', 'Antarctica', 'Antarctica', 'Antarctica', '[]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AR', 'ARG', '032', 'Argentina', 'Argentine Republic', 'Americas', 'Latin America and the Caribbean', '["es-AR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AS', 'ASM', '016', 'American Samoa', 'American Samoa', 'Oceania', 'Polynesia', '["en-AS", "sm-AS"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AT', 'AUT', '040', 'Austria', 'Republic of Austria', 'Europe', 'Western Europe', '["de-AT"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AU', 'AUS', '036', 'Australia', 'Australia', 'Oceania', 'Australia and New Zealand', '["en-AU"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AW', 'ABW', '533', 'Aruba', 'Aruba', 'Americas', 'Latin America and the Caribbean', '["nl-AW", "pap-AW"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AX', 'ALA', '248', 'Åland Islands', 'Åland Islands', 'Europe', 'Northern Europe', '["sv-AX"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('AZ', 'AZE', '031', 'Azerbaijan', 'Republic of Azerbaijan', 'Asia', 'Western Asia', '["az-AZ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BA', 'BIH', '070', 'Bosnia and Herzegovina', 'Republic of Bosnia and Herzegovina', 'Europe', 'Southern Europe', '["bs-BA", "hr-BA", "sr-BA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BB', 'BRB', '052', 'Barbados', 'Barbados', 'Americas', 'Latin America and the Caribbean', '["en-BB"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BD', 'BGD', '050', 'Bangladesh', 'People''s Republic of Bangladesh', 'Asia', 'Southern Asia', '["bn-BD"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BE', 'BEL', '056', 'Belgium', 'Kingdom of Belgium', 'Europe', 'Western Europe', '["nl-BE", "fr-BE", "de-BE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BF', 'BFA', '854', 'Burkina Faso', 'Burkina Faso', 'Africa', 'Sub-Saharan Africa', '["fr-BF"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BG', 'BGR', '100', 'Bulgaria', 'Republic of Bulgaria', 'Europe', 'Eastern Europe', '["bg-BG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BH', 'BHR', '048', 'Bahrain', 'Kingdom of Bahrain', 'Asia', 'Western Asia', '["ar-BH"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BI', 'BDI', '108', 'Burundi', 'Republic of Burundi', 'Africa', 'Sub-Saharan Africa', '["fr-BI", "rn-BI"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BJ', 'BEN', '204', 'Benin', 'Republic of Benin', 'Africa', 'Sub-Saharan Africa', '["fr-BJ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BL', 'BLM', '652', 'Saint Barthélemy', 'Saint Barthélemy', 'Americas', 'Latin America and the Caribbean', '["fr-BL"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BM', 'BMU', '060', 'Bermuda', 'Bermuda', 'Americas', 'Northern America', '["en-BM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BN', 'BRN', '096', 'Brunei Darussalam', 'Brunei Darussalam', 'Asia', 'South-eastern Asia', '["ms-BN"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BO', 'BOL', '068', 'Bolivia, Plurinational State of', 'Plurinational State of Bolivia', 'Americas', 'Latin America and the Caribbean', '["es-BO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BQ', 'BES', '535', 'Bonaire, Sint Eustatius and Saba', 'Bonaire, Sint Eustatius and Saba', 'Americas', 'Latin America and the Caribbean', '["nl-BQ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BR', 'BRA', '076', 'Brazil', 'Federative Republic of Brazil', 'Americas', 'Latin America and the Caribbean', '["pt-BR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BS', 'BHS', '044', 'Bahamas', 'Commonwealth of the Bahamas', 'Americas', 'Latin America and the Caribbean', '["en-BS"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BT', 'BTN', '064', 'Bhutan', 'Kingdom of Bhutan', 'Asia', 'Southern Asia', '["dz-BT"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BV', 'BVT', '074', 'Bouvet Island', 'Bouvet Island', 'Antarctica', 'Antarctica', '[]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BW', 'BWA', '072', 'Botswana', 'Republic of Botswana', 'Africa', 'Sub-Saharan Africa', '["en-BW", "tn-BW"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BY', 'BLR', '112', 'Belarus', 'Republic of Belarus', 'Europe', 'Eastern Europe', '["be-BY", "ru-BY"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('BZ', 'BLZ', '084', 'Belize', 'Belize', 'Americas', 'Latin America and the Caribbean', '["en-BZ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CA', 'CAN', '124', 'Canada', 'Canada', 'Americas', 'Northern America', '["en-CA", "fr-CA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CC', 'CCK', '166', 'Cocos (Keeling) Islands', 'Cocos (Keeling) Islands', 'Oceania', 'Australia and New Zealand', '["en-CC"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CD', 'COD', '180', 'Congo, The Democratic Republic of the', 'Congo, The Democratic Republic of the', 'Africa', 'Sub-Saharan Africa', '["fr-CD"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CF', 'CAF', '140', 'Central African Republic', 'Central African Republic', 'Africa', 'Sub-Saharan Africa', '["fr-CF", "sg-CF"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CG', 'COG', '178', 'Congo', 'Republic of the Congo', 'Africa', 'Sub-Saharan Africa', '["fr-CG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CH', 'CHE', '756', 'Switzerland', 'Swiss Confederation', 'Europe', 'Western Europe', '["de-CH", "fr-CH", "it-CH"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CI', 'CIV', '384', 'Côte d''Ivoire', 'Republic of Côte d''Ivoire', 'Africa', 'Sub-Saharan Africa', '["fr-CI"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CK', 'COK', '184', 'Cook Islands', 'Cook Islands', 'Oceania', 'Polynesia', '["en-CK"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CL', 'CHL', '152', 'Chile', 'Republic of Chile', 'Americas', 'Latin America and the Caribbean', '["es-CL"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CM', 'CMR', '120', 'Cameroon', 'Republic of Cameroon', 'Africa', 'Sub-Saharan Africa', '["fr-CM", "en-CM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CN', 'CHN', '156', 'China', 'People''s Republic of China', 'Asia', 'Eastern Asia', '["zh-CN"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CO', 'COL', '170', 'Colombia', 'Republic of Colombia', 'Americas', 'Latin America and the Caribbean', '["es-CO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CR', 'CRI', '188', 'Costa Rica', 'Republic of Costa Rica', 'Americas', 'Latin America and the Caribbean', '["es-CR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CU', 'CUB', '192', 'Cuba', 'Republic of Cuba', 'Americas', 'Latin America and the Caribbean', '["es-CU"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CV', 'CPV', '132', 'Cabo Verde', 'Republic of Cabo Verde', 'Africa', 'Sub-Saharan Africa', '["pt-CV"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CW', 'CUW', '531', 'Curaçao', 'Curaçao', 'Americas', 'Latin America and the Caribbean', '["nl-CW", "pap-CW"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CX', 'CXR', '162', 'Christmas Island', 'Christmas Island', 'Oceania', 'Australia and New Zealand', '["en-CX"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CY', 'CYP', '196', 'Cyprus', 'Republic of Cyprus', 'Asia', 'Western Asia', '["el-CY", "tr-CY"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('CZ', 'CZE', '203', 'Czechia', 'Czech Republic', 'Europe', 'Eastern Europe', '["cs-CZ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('DE', 'DEU', '276', 'Germany', 'Federal Republic of Germany', 'Europe', 'Western Europe', '["de-DE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('DJ', 'DJI', '262', 'Djibouti', 'Republic of Djibouti', 'Africa', 'Sub-Saharan Africa', '["fr-DJ", "ar-DJ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('DK', 'DNK', '208', 'Denmark', 'Kingdom of Denmark', 'Europe', 'Northern Europe', '["da-DK"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('DM', 'DMA', '212', 'Dominica', 'Commonwealth of Dominica', 'Americas', 'Latin America and the Caribbean', '["en-DM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('DO', 'DOM', '214', 'Dominican Republic', 'Dominican Republic', 'Americas', 'Latin America and the Caribbean', '["es-DO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('DZ', 'DZA', '012', 'Algeria', 'People''s Democratic Republic of Algeria', 'Africa', 'Northern Africa', '["ar-DZ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('EC', 'ECU', '218', 'Ecuador', 'Republic of Ecuador', 'Americas', 'Latin America and the Caribbean', '["es-EC"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('EE', 'EST', '233', 'Estonia', 'Republic of Estonia', 'Europe', 'Northern Europe', '["et-EE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('EG', 'EGY', '818', 'Egypt', 'Arab Republic of Egypt', 'Africa', 'Northern Africa', '["ar-EG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('EH', 'ESH', '732', 'Western Sahara', 'Western Sahara', 'Africa', 'Northern Africa', '["ar-EH"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('ER', 'ERI', '232', 'Eritrea', 'the State of Eritrea', 'Africa', 'Sub-Saharan Africa', '["ti-ER", "ar-ER", "en-ER"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('ES', 'ESP', '724', 'Spain', 'Kingdom of Spain', 'Europe', 'Southern Europe', '["es-ES"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('ET', 'ETH', '231', 'Ethiopia', 'Federal Democratic Republic of Ethiopia', 'Africa', 'Sub-Saharan Africa', '["am-ET"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('FI', 'FIN', '246', 'Finland', 'Republic of Finland', 'Europe', 'Northern Europe', '["fi-FI", "sv-FI"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('FJ', 'FJI', '242', 'Fiji', 'Republic of Fiji', 'Oceania', 'Melanesia', '["en-FJ", "fj-FJ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('FK', 'FLK', '238', 'Falkland Islands (Malvinas)', 'Falkland Islands (Malvinas)', 'Americas', 'Latin America and the Caribbean', '["en-FK"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('FM', 'FSM', '583', 'Micronesia, Federated States of', 'Federated States of Micronesia', 'Oceania', 'Micronesia', '["en-FM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('FO', 'FRO', '234', 'Faroe Islands', 'Faroe Islands', 'Europe', 'Northern Europe', '["fo-FO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('FR', 'FRA', '250', 'France', 'French Republic', 'Europe', 'Western Europe', '["fr-FR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GA', 'GAB', '266', 'Gabon', 'Gabonese Republic', 'Africa', 'Sub-Saharan Africa', '["fr-GA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GB', 'GBR', '826', 'United Kingdom', 'United Kingdom of Great Britain and Northern Ireland', 'Europe', 'Northern Europe', '["en-GB"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GD', 'GRD', '308', 'Grenada', 'Grenada', 'Americas', 'Latin America and the Caribbean', '["en-GD"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GE', 'GEO', '268', 'Georgia', 'Georgia', 'Asia', 'Western Asia', '["ka-GE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GF', 'GUF', '254', 'French Guiana', 'French Guiana', 'Americas', 'Latin America and the Caribbean', '["fr-GF"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GG', 'GGY', '831', 'Guernsey', 'Guernsey', 'Europe', 'Northern Europe', '["en-GG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GH', 'GHA', '288', 'Ghana', 'Republic of Ghana', 'Africa', 'Sub-Saharan Africa', '["en-GH"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GI', 'GIB', '292', 'Gibraltar', 'Gibraltar', 'Europe', 'Southern Europe', '["en-GI"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GL', 'GRL', '304', 'Greenland', 'Greenland', 'Americas', 'Northern America', '["kl-GL"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GM', 'GMB', '270', 'Gambia', 'Republic of the Gambia', 'Africa', 'Sub-Saharan Africa', '["en-GM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GN', 'GIN', '324', 'Guinea', 'Republic of Guinea', 'Africa', 'Sub-Saharan Africa', '["fr-GN"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GP', 'GLP', '312', 'Guadeloupe', 'Guadeloupe', 'Americas', 'Latin America and the Caribbean', '["fr-GP"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GQ', 'GNQ', '226', 'Equatorial Guinea', 'Republic of Equatorial Guinea', 'Africa', 'Sub-Saharan Africa', '["es-GQ", "fr-GQ", "pt-GQ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GR', 'GRC', '300', 'Greece', 'Hellenic Republic', 'Europe', 'Southern Europe', '["el-GR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GS', 'SGS', '239', 'South Georgia and the South Sandwich Islands', 'South Georgia and the South Sandwich Islands', 'Antarctica', 'Antarctica', '[]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GT', 'GTM', '320', 'Guatemala', 'Republic of Guatemala', 'Americas', 'Latin America and the Caribbean', '["es-GT"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GU', 'GUM', '316', 'Guam', 'Guam', 'Oceania', 'Micronesia', '["en-GU"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GW', 'GNB', '624', 'Guinea-Bissau', 'Republic of Guinea-Bissau', 'Africa', 'Sub-Saharan Africa', '["pt-GW"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('GY', 'GUY', '328', 'Guyana', 'Republic of Guyana', 'Americas', 'Latin America and the Caribbean', '["en-GY"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('HK', 'HKG', '344', 'Hong Kong', 'Hong Kong Special Administrative Region of China', 'Asia', 'Eastern Asia', '["zh-HK", "en-HK"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('HM', 'HMD', '334', 'Heard Island and McDonald Islands', 'Heard Island and McDonald Islands', 'Oceania', 'Australia and New Zealand', '[]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('HN', 'HND', '340', 'Honduras', 'Republic of Honduras', 'Americas', 'Latin America and the Caribbean', '["es-HN"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('HR', 'HRV', '191', 'Croatia', 'Republic of Croatia', 'Europe', 'Southern Europe', '["hr-HR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('HT', 'HTI', '332', 'Haiti', 'Republic of Haiti', 'Americas', 'Latin America and the Caribbean', '["fr-HT", "ht-HT"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('HU', 'HUN', '348', 'Hungary', 'Hungary', 'Europe', 'Eastern Europe', '["hu-HU"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('ID', 'IDN', '360', 'Indonesia', 'Republic of Indonesia', 'Asia', 'South-eastern Asia', '["id-ID"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('IE', 'IRL', '372', 'Ireland', 'Ireland', 'Europe', 'Northern Europe', '["en-IE", "ga-IE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('IL', 'ISR', '376', 'Israel', 'State of Israel', 'Asia', 'Western Asia', '["he-IL", "ar-IL"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('IM', 'IMN', '833', 'Isle of Man', 'Isle of Man', 'Europe', 'Northern Europe', '["en-IM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('IN', 'IND', '356', 'India', 'Republic of India', 'Asia', 'Southern Asia', '["hi-IN", "en-IN"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('IO', 'IOT', '086', 'British Indian Ocean Territory', 'British Indian Ocean Territory', 'Africa', 'Sub-Saharan Africa', '["en-IO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('IQ', 'IRQ', '368', 'Iraq', 'Republic of Iraq', 'Asia', 'Western Asia', '["ar-IQ", "ku-IQ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('IR', 'IRN', '364', 'Iran, Islamic Republic of', 'Islamic Republic of Iran', 'Asia', 'Southern Asia', '["fa-IR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('IS', 'ISL', '352', 'Iceland', 'Republic of Iceland', 'Europe', 'Northern Europe', '["is-IS"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('IT', 'ITA', '380', 'Italy', 'Italian Republic', 'Europe', 'Southern Europe', '["it-IT"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('JE', 'JEY', '832', 'Jersey', 'Jersey', 'Europe', 'Northern Europe', '["en-JE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('JM', 'JAM', '388', 'Jamaica', 'Jamaica', 'Americas', 'Latin America and the Caribbean', '["en-JM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('JO', 'JOR', '400', 'Jordan', 'Hashemite Kingdom of Jordan', 'Asia', 'Western Asia', '["ar-JO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('JP', 'JPN', '392', 'Japan', 'Japan', 'Asia', 'Eastern Asia', '["ja-JP"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('KE', 'KEN', '404', 'Kenya', 'Republic of Kenya', 'Africa', 'Sub-Saharan Africa', '["en-KE", "sw-KE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('KG', 'KGZ', '417', 'Kyrgyzstan', 'Kyrgyz Republic', 'Asia', 'Central Asia', '["ky-KG", "ru-KG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('KH', 'KHM', '116', 'Cambodia', 'Kingdom of Cambodia', 'Asia', 'South-eastern Asia', '["km-KH"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('KI', 'KIR', '296', 'Kiribati', 'Republic of Kiribati', 'Oceania', 'Micronesia', '["en-KI"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('KM', 'COM', '174', 'Comoros', 'Union of the Comoros', 'Africa', 'Sub-Saharan Africa', '["ar-KM", "fr-KM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('KN', 'KNA', '659', 'Saint Kitts and Nevis', 'Saint Kitts and Nevis', 'Americas', 'Latin America and the Caribbean', '["en-KN"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('KP', 'PRK', '408', 'Korea, Democratic People''s Republic of', 'Democratic People''s Republic of Korea', 'Asia', 'Eastern Asia', '["ko-KP"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('KR', 'KOR', '410', 'Korea, Republic of', 'Korea, Republic of', 'Asia', 'Eastern Asia', '["ko-KR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('KW', 'KWT', '414', 'Kuwait', 'State of Kuwait', 'Asia', 'Western Asia', '["ar-KW"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('KY', 'CYM', '136', 'Cayman Islands', 'Cayman Islands', 'Americas', 'Latin America and the Caribbean', '["en-KY"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('KZ', 'KAZ', '398', 'Kazakhstan', 'Republic of Kazakhstan', 'Asia', 'Central Asia', '["kk-KZ", "ru-KZ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('LA', 'LAO', '418', 'Lao People''s Democratic Republic', 'Lao People''s Democratic Republic', 'Asia', 'South-eastern Asia', '["lo-LA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('LB', 'LBN', '422', 'Lebanon', 'Lebanese Republic', 'Asia', 'Western Asia', '["ar-LB"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('LC', 'LCA', '662', 'Saint Lucia', 'Saint Lucia', 'Americas', 'Latin America and the Caribbean', '["en-LC"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('LI', 'LIE', '438', 'Liechtenstein', 'Principality of Liechtenstein', 'Europe', 'Western Europe', '["de-LI"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('LK', 'LKA', '144', 'Sri Lanka', 'Democratic Socialist Republic of Sri Lanka', 'Asia', 'Southern Asia', '["si-LK", "ta-LK"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('LR', 'LBR', '430', 'Liberia', 'Republic of Liberia', 'Africa', 'Sub-Saharan Africa', '["en-LR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('LS', 'LSO', '426', 'Lesotho', 'Kingdom of Lesotho', 'Africa', 'Sub-Saharan Africa', '["en-LS", "st-LS"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('LT', 'LTU', '440', 'Lithuania', 'Republic of Lithuania', 'Europe', 'Northern Europe', '["lt-LT"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('LU', 'LUX', '442', 'Luxembourg', 'Grand Duchy of Luxembourg', 'Europe', 'Western Europe', '["lb-LU", "fr-LU", "de-LU"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('LV', 'LVA', '428', 'Latvia', 'Republic of Latvia', 'Europe', 'Northern Europe', '["lv-LV"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('LY', 'LBY', '434', 'Libya', 'Libya', 'Africa', 'Northern Africa', '["ar-LY"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MA', 'MAR', '504', 'Morocco', 'Kingdom of Morocco', 'Africa', 'Northern Africa', '["ar-MA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MC', 'MCO', '492', 'Monaco', 'Principality of Monaco', 'Europe', 'Western Europe', '["fr-MC"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MD', 'MDA', '498', 'Moldova, Republic of', 'Republic of Moldova', 'Europe', 'Eastern Europe', '["ro-MD"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('ME', 'MNE', '499', 'Montenegro', 'Montenegro', 'Europe', 'Southern Europe', '["sr-ME"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MF', 'MAF', '663', 'Saint Martin (French part)', 'Saint Martin (French part)', 'Americas', 'Latin America and the Caribbean', '["fr-MF"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MG', 'MDG', '450', 'Madagascar', 'Republic of Madagascar', 'Africa', 'Sub-Saharan Africa', '["fr-MG", "mg-MG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MH', 'MHL', '584', 'Marshall Islands', 'Republic of the Marshall Islands', 'Oceania', 'Micronesia', '["en-MH", "mh-MH"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MK', 'MKD', '807', 'North Macedonia', 'Republic of North Macedonia', 'Europe', 'Southern Europe', '["mk-MK"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('ML', 'MLI', '466', 'Mali', 'Republic of Mali', 'Africa', 'Sub-Saharan Africa', '["fr-ML"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MM', 'MMR', '104', 'Myanmar', 'Republic of Myanmar', 'Asia', 'South-eastern Asia', '["my-MM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MN', 'MNG', '496', 'Mongolia', 'Mongolia', 'Asia', 'Eastern Asia', '["mn-MN"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MO', 'MAC', '446', 'Macao', 'Macao Special Administrative Region of China', 'Asia', 'Eastern Asia', '["zh-MO", "pt-MO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MP', 'MNP', '580', 'Northern Mariana Islands', 'Commonwealth of the Northern Mariana Islands', 'Oceania', 'Micronesia', '["en-MP"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MQ', 'MTQ', '474', 'Martinique', 'Martinique', 'Americas', 'Latin America and the Caribbean', '["fr-MQ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MR', 'MRT', '478', 'Mauritania', 'Islamic Republic of Mauritania', 'Africa', 'Sub-Saharan Africa', '["ar-MR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MS', 'MSR', '500', 'Montserrat', 'Montserrat', 'Americas', 'Latin America and the Caribbean', '["en-MS"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MT', 'MLT', '470', 'Malta', 'Republic of Malta', 'Europe', 'Southern Europe', '["mt-MT", "en-MT"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MU', 'MUS', '480', 'Mauritius', 'Republic of Mauritius', 'Africa', 'Sub-Saharan Africa', '["en-MU", "fr-MU"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MV', 'MDV', '462', 'Maldives', 'Republic of Maldives', 'Asia', 'Southern Asia', '["dv-MV"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MW', 'MWI', '454', 'Malawi', 'Republic of Malawi', 'Africa', 'Sub-Saharan Africa', '["en-MW", "ny-MW"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MX', 'MEX', '484', 'Mexico', 'United Mexican States', 'Americas', 'Latin America and the Caribbean', '["es-MX"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MY', 'MYS', '458', 'Malaysia', 'Malaysia', 'Asia', 'South-eastern Asia', '["ms-MY"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('MZ', 'MOZ', '508', 'Mozambique', 'Republic of Mozambique', 'Africa', 'Sub-Saharan Africa', '["pt-MZ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NA', 'NAM', '516', 'Namibia', 'Republic of Namibia', 'Africa', 'Sub-Saharan Africa', '["en-NA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NC', 'NCL', '540', 'New Caledonia', 'New Caledonia', 'Oceania', 'Melanesia', '["fr-NC"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NE', 'NER', '562', 'Niger', 'Republic of the Niger', 'Africa', 'Sub-Saharan Africa', '["fr-NE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NF', 'NFK', '574', 'Norfolk Island', 'Norfolk Island', 'Oceania', 'Australia and New Zealand', '["en-NF"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NG', 'NGA', '566', 'Nigeria', 'Federal Republic of Nigeria', 'Africa', 'Sub-Saharan Africa', '["en-NG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NI', 'NIC', '558', 'Nicaragua', 'Republic of Nicaragua', 'Americas', 'Latin America and the Caribbean', '["es-NI"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NL', 'NLD', '528', 'Netherlands', 'Kingdom of the Netherlands', 'Europe', 'Western Europe', '["nl-NL"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NO', 'NOR', '578', 'Norway', 'Kingdom of Norway', 'Europe', 'Northern Europe', '["no-NO", "nb-NO", "nn-NO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NP', 'NPL', '524', 'Nepal', 'Federal Democratic Republic of Nepal', 'Asia', 'Southern Asia', '["ne-NP"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NR', 'NRU', '520', 'Nauru', 'Republic of Nauru', 'Oceania', 'Micronesia', '["en-NR", "na-NR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NU', 'NIU', '570', 'Niue', 'Niue', 'Oceania', 'Polynesia', '["en-NU"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('NZ', 'NZL', '554', 'New Zealand', 'New Zealand', 'Oceania', 'Australia and New Zealand', '["en-NZ", "mi-NZ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('OM', 'OMN', '512', 'Oman', 'Sultanate of Oman', 'Asia', 'Western Asia', '["ar-OM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PA', 'PAN', '591', 'Panama', 'Republic of Panama', 'Americas', 'Latin America and the Caribbean', '["es-PA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PE', 'PER', '604', 'Peru', 'Republic of Peru', 'Americas', 'Latin America and the Caribbean', '["es-PE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PF', 'PYF', '258', 'French Polynesia', 'French Polynesia', 'Oceania', 'Polynesia', '["fr-PF"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PG', 'PNG', '598', 'Papua New Guinea', 'Independent State of Papua New Guinea', 'Oceania', 'Melanesia', '["en-PG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PH', 'PHL', '608', 'Philippines', 'Republic of the Philippines', 'Asia', 'South-eastern Asia', '["en-PH", "tl-PH"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PK', 'PAK', '586', 'Pakistan', 'Islamic Republic of Pakistan', 'Asia', 'Southern Asia', '["ur-PK", "en-PK"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PL', 'POL', '616', 'Poland', 'Republic of Poland', 'Europe', 'Eastern Europe', '["pl-PL"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PM', 'SPM', '666', 'Saint Pierre and Miquelon', 'Saint Pierre and Miquelon', 'Americas', 'Northern America', '["fr-PM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PN', 'PCN', '612', 'Pitcairn', 'Pitcairn', 'Oceania', 'Polynesia', '["en-PN"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PR', 'PRI', '630', 'Puerto Rico', 'Puerto Rico', 'Americas', 'Latin America and the Caribbean', '["es-PR", "en-PR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PS', 'PSE', '275', 'Palestine, State of', 'the State of Palestine', 'Asia', 'Western Asia', '["ar-PS"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PT', 'PRT', '620', 'Portugal', 'Portuguese Republic', 'Europe', 'Southern Europe', '["pt-PT"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PW', 'PLW', '585', 'Palau', 'Republic of Palau', 'Oceania', 'Micronesia', '["en-PW"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('PY', 'PRY', '600', 'Paraguay', 'Republic of Paraguay', 'Americas', 'Latin America and the Caribbean', '["es-PY", "gn-PY"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('QA', 'QAT', '634', 'Qatar', 'State of Qatar', 'Asia', 'Western Asia', '["ar-QA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('RE', 'REU', '638', 'Réunion', 'Réunion', 'Africa', 'Sub-Saharan Africa', '["fr-RE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('RO', 'ROU', '642', 'Romania', 'Romania', 'Europe', 'Eastern Europe', '["ro-RO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('RS', 'SRB', '688', 'Serbia', 'Republic of Serbia', 'Europe', 'Southern Europe', '["sr-RS"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('RU', 'RUS', '643', 'Russian Federation', 'Russian Federation', 'Europe', 'Eastern Europe', '["ru-RU"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('RW', 'RWA', '646', 'Rwanda', 'Rwandese Republic', 'Africa', 'Sub-Saharan Africa', '["rw-RW", "en-RW", "fr-RW"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SA', 'SAU', '682', 'Saudi Arabia', 'Kingdom of Saudi Arabia', 'Asia', 'Western Asia', '["ar-SA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SB', 'SLB', '090', 'Solomon Islands', 'Solomon Islands', 'Oceania', 'Melanesia', '["en-SB"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SC', 'SYC', '690', 'Seychelles', 'Republic of Seychelles', 'Africa', 'Sub-Saharan Africa', '["en-SC", "fr-SC"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SD', 'SDN', '729', 'Sudan', 'Republic of the Sudan', 'Africa', 'Northern Africa', '["ar-SD", "en-SD"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SE', 'SWE', '752', 'Sweden', 'Kingdom of Sweden', 'Europe', 'Northern Europe', '["sv-SE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SG', 'SGP', '702', 'Singapore', 'Republic of Singapore', 'Asia', 'South-eastern Asia', '["en-SG", "zh-SG", "ms-SG", "ta-SG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SH', 'SHN', '654', 'Saint Helena, Ascension and Tristan da Cunha', 'Saint Helena, Ascension and Tristan da Cunha', 'Africa', 'Sub-Saharan Africa', '["en-SH"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SI', 'SVN', '705', 'Slovenia', 'Republic of Slovenia', 'Europe', 'Southern Europe', '["sl-SI"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SJ', 'SJM', '744', 'Svalbard and Jan Mayen', 'Svalbard and Jan Mayen', 'Europe', 'Northern Europe', '["no-SJ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SK', 'SVK', '703', 'Slovakia', 'Slovak Republic', 'Europe', 'Eastern Europe', '["sk-SK"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SL', 'SLE', '694', 'Sierra Leone', 'Republic of Sierra Leone', 'Africa', 'Sub-Saharan Africa', '["en-SL"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SM', 'SMR', '674', 'San Marino', 'Republic of San Marino', 'Europe', 'Southern Europe', '["it-SM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SN', 'SEN', '686', 'Senegal', 'Republic of Senegal', 'Africa', 'Sub-Saharan Africa', '["fr-SN"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SO', 'SOM', '706', 'Somalia', 'Federal Republic of Somalia', 'Africa', 'Sub-Saharan Africa', '["so-SO", "ar-SO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SR', 'SUR', '740', 'Suriname', 'Republic of Suriname', 'Americas', 'Latin America and the Caribbean', '["nl-SR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SS', 'SSD', '728', 'South Sudan', 'Republic of South Sudan', 'Africa', 'Sub-Saharan Africa', '["en-SS"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('ST', 'STP', '678', 'Sao Tome and Principe', 'Democratic Republic of Sao Tome and Principe', 'Africa', 'Sub-Saharan Africa', '["pt-ST"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SV', 'SLV', '222', 'El Salvador', 'Republic of El Salvador', 'Americas', 'Latin America and the Caribbean', '["es-SV"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SX', 'SXM', '534', 'Sint Maarten (Dutch part)', 'Sint Maarten (Dutch part)', 'Americas', 'Latin America and the Caribbean', '["nl-SX", "en-SX"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SY', 'SYR', '760', 'Syrian Arab Republic', 'Syrian Arab Republic', 'Asia', 'Western Asia', '["ar-SY"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('SZ', 'SWZ', '748', 'Eswatini', 'Kingdom of Eswatini', 'Africa', 'Sub-Saharan Africa', '["en-SZ", "ss-SZ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TC', 'TCA', '796', 'Turks and Caicos Islands', 'Turks and Caicos Islands', 'Americas', 'Latin America and the Caribbean', '["en-TC"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TD', 'TCD', '148', 'Chad', 'Republic of Chad', 'Africa', 'Sub-Saharan Africa', '["fr-TD", "ar-TD"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TF', 'ATF', '260', 'French Southern Territories', 'French Southern Territories', 'Africa', 'Sub-Saharan Africa', '["fr-TF"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TG', 'TGO', '768', 'Togo', 'Togolese Republic', 'Africa', 'Sub-Saharan Africa', '["fr-TG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TH', 'THA', '764', 'Thailand', 'Kingdom of Thailand', 'Asia', 'South-eastern Asia', '["th-TH"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TJ', 'TJK', '762', 'Tajikistan', 'Republic of Tajikistan', 'Asia', 'Central Asia', '["tg-TJ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TK', 'TKL', '772', 'Tokelau', 'Tokelau', 'Oceania', 'Polynesia', '["en-TK"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TL', 'TLS', '626', 'Timor-Leste', 'Democratic Republic of Timor-Leste', 'Asia', 'South-eastern Asia', '["pt-TL"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TM', 'TKM', '795', 'Turkmenistan', 'Turkmenistan', 'Asia', 'Central Asia', '["tk-TM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TN', 'TUN', '788', 'Tunisia', 'Republic of Tunisia', 'Africa', 'Northern Africa', '["ar-TN"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TO', 'TON', '776', 'Tonga', 'Kingdom of Tonga', 'Oceania', 'Polynesia', '["en-TO", "to-TO"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TR', 'TUR', '792', 'Türkiye', 'Republic of Türkiye', 'Asia', 'Western Asia', '["tr-TR"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TT', 'TTO', '780', 'Trinidad and Tobago', 'Republic of Trinidad and Tobago', 'Americas', 'Latin America and the Caribbean', '["en-TT"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TV', 'TUV', '798', 'Tuvalu', 'Tuvalu', 'Oceania', 'Polynesia', '["en-TV"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TW', 'TWN', '158', 'Taiwan, Province of China', 'Taiwan, Province of China', 'Asia', 'Eastern Asia', '["zh-TW"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('TZ', 'TZA', '834', 'Tanzania, United Republic of', 'United Republic of Tanzania', 'Africa', 'Sub-Saharan Africa', '["sw-TZ", "en-TZ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('UA', 'UKR', '804', 'Ukraine', 'Ukraine', 'Europe', 'Eastern Europe', '["uk-UA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('UG', 'UGA', '800', 'Uganda', 'Republic of Uganda', 'Africa', 'Sub-Saharan Africa', '["en-UG", "sw-UG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('UM', 'UMI', '581', 'United States Minor Outlying Islands', 'United States Minor Outlying Islands', 'Americas', 'Northern America', '["en-UM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('US', 'USA', '840', 'United States', 'United States of America', 'Americas', 'Northern America', '["en-US"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('UY', 'URY', '858', 'Uruguay', 'Eastern Republic of Uruguay', 'Americas', 'Latin America and the Caribbean', '["es-UY"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('UZ', 'UZB', '860', 'Uzbekistan', 'Republic of Uzbekistan', 'Asia', 'Central Asia', '["uz-UZ"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('VA', 'VAT', '336', 'Holy See (Vatican City State)', 'Holy See (Vatican City State)', 'Europe', 'Southern Europe', '["it-VA", "la-VA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('VC', 'VCT', '670', 'Saint Vincent and the Grenadines', 'Saint Vincent and the Grenadines', 'Americas', 'Latin America and the Caribbean', '["en-VC"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('VE', 'VEN', '862', 'Venezuela, Bolivarian Republic of', 'Bolivarian Republic of Venezuela', 'Americas', 'Latin America and the Caribbean', '["es-VE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('VG', 'VGB', '092', 'Virgin Islands, British', 'British Virgin Islands', 'Americas', 'Latin America and the Caribbean', '["en-VG"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('VI', 'VIR', '850', 'Virgin Islands, U.S.', 'Virgin Islands of the United States', 'Americas', 'Latin America and the Caribbean', '["en-VI"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('VN', 'VNM', '704', 'Viet Nam', 'Socialist Republic of Viet Nam', 'Asia', 'South-eastern Asia', '["vi-VN"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('VU', 'VUT', '548', 'Vanuatu', 'Republic of Vanuatu', 'Oceania', 'Melanesia', '["bi-VU", "en-VU", "fr-VU"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('WF', 'WLF', '876', 'Wallis and Futuna', 'Wallis and Futuna', 'Oceania', 'Polynesia', '["fr-WF"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('WS', 'WSM', '882', 'Samoa', 'Independent State of Samoa', 'Oceania', 'Polynesia', '["en-WS", "sm-WS"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('YE', 'YEM', '887', 'Yemen', 'Republic of Yemen', 'Asia', 'Western Asia', '["ar-YE"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('YT', 'MYT', '175', 'Mayotte', 'Mayotte', 'Africa', 'Sub-Saharan Africa', '["fr-YT"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('ZA', 'ZAF', '710', 'South Africa', 'Republic of South Africa', 'Africa', 'Sub-Saharan Africa', '["en-ZA", "af-ZA", "zu-ZA"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('ZM', 'ZMB', '894', 'Zambia', 'Republic of Zambia', 'Africa', 'Sub-Saharan Africa', '["en-ZM"]');
INSERT INTO public.iso_countries (iso2, iso3, "numeric", name, official, region, subregion, languages) VALUES ('ZW', 'ZWE', '716', 'Zimbabwe', 'Republic of Zimbabwe', 'Africa', 'Sub-Saharan Africa', '["en-ZW", "sn-ZW", "nd-ZW"]');


--
-- Data for Name: output_dead_letter; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: proposed_edges; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: signal_aliases; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: signal_entity_links; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: signals; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: situations; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: source_credibility; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('reuters.com', 0.9, 'Top-tier international wire service; long-running corrections process.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('apnews.com', 0.9, 'Associated Press — primary wire service for U.S. and international news.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('afp.com', 0.9, 'Agence France-Presse — top-tier international wire.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('bbc.com', 0.9, 'British Broadcasting Corporation — public broadcaster, strong editorial standards.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('bbc.co.uk', 0.9, 'British Broadcasting Corporation — UK domain.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('npr.org', 0.9, 'National Public Radio — U.S. public broadcaster.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('pbs.org', 0.9, 'Public Broadcasting Service — U.S. public broadcaster.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('bloomberg.com', 0.9, 'Bloomberg News — financial wire with strong fact-checking.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('ft.com', 0.9, 'Financial Times — strong editorial standards.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('economist.com', 0.9, 'The Economist — analytical depth, transparent methodology.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('theguardian.com', 0.9, 'The Guardian — UK quality paper, transparent corrections.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('wsj.com', 0.9, 'Wall Street Journal — strong news desk separation from opinion.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('cnn.com', 0.7, 'Cable News Network — major U.S. broadcaster.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('nytimes.com', 0.7, 'New York Times — major U.S. paper of record.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('washingtonpost.com', 0.7, 'Washington Post — major U.S. paper.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('beforeitsnews.com', 0.1, 'Open user-generated conspiracy aggregator.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'aggregator', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('infowars.com', 0.1, 'Documented conspiracy theory outlet; major defamation findings on record.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'social', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('naturalnews.com', 0.1, 'Documented pseudo-science / conspiracy outlet.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'social', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('worldnewsdailyreport.com', 0.05, 'Self-described satire site frequently mistaken for news.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'social', false);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('dw.com', 0.9, 'Deutsche Welle — Germany public broadcaster.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', true);
INSERT INTO public.source_credibility (source_host, score, score_rationale, last_updated, scored_by, tier, state_affiliation) VALUES ('aljazeera.com', 0.7, 'Al Jazeera English — major international broadcaster; some editorial slant noted.', '2026-06-10 08:44:45.824444+00', 'system.seed', 'wire', true);


--
-- Data for Name: source_descriptors; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: stack_components; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: stack_credentials; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: target_descriptors; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: trigger_state; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: ui_panel_registrations; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: vocabulary_entries; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('8ce4700c-5604-47b1-a8cb-32e94345f8fb', 'entity_class', 'entity', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('cf1b5283-ad56-4295-9efb-8e6a519d3c41', 'entity_class', 'location', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('dc514683-edc5-43ef-8589-08aadb8106b0', 'entity_class', 'organization', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('d46d9f7f-80de-4616-ac17-da8641c07391', 'entity_class', 'person', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('9abb9571-e4a3-42b3-8a8e-5886e39f2a87', 'entity_class', 'event', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('6ce10040-e501-4b39-8c0a-b975399a389c', 'entity_class', 'country', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('6c8041a5-078d-4170-8727-dc487015a5f6', 'entity_class', 'concept', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('fe02bf8a-b417-48b8-ba41-27eba182f273', 'entity_class', 'corporation', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('6925fa1e-163e-4737-b433-72c067966fda', 'entity_class', 'software', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('9538b667-dd5f-462a-ad59-db32157a4965', 'relationship_type', 'HostileTo', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('00e34914-a875-4376-abf3-5c80b012b2fb', 'relationship_type', 'LocatedIn', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('dd3f2311-497e-4b77-bf98-1cc532b272f7', 'relationship_type', 'AlliedWith', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('41cd24cc-6622-4a67-9dd2-1d3e26bdc95d', 'relationship_type', 'PartyTo', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('44311f6b-9481-4992-a1dc-2168fad14199', 'relationship_type', 'Targets', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('c717a8d1-3225-4845-b836-af6e9e224454', 'relationship_type', 'OperatesIn', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('6ca74094-2cd6-4f52-a909-e6383a74d6ae', 'relationship_type', 'MemberOf', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('14fb6fc0-984e-4686-baba-d40e491967d2', 'relationship_type', 'LeaderOf', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('6378e26a-1cd0-4cde-a1a3-d46624122d9c', 'relationship_type', 'ConductedVia', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('2d4907ad-5903-4a3e-8710-9961fc687b55', 'relationship_type', 'SuppliesWeaponsTo', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('9e62fc77-9615-4757-90c2-13b1c73518f1', 'relationship_type', 'PartOf', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{PART_OF}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('91daa394-af3b-4edd-bdad-e9d0591800ec', 'relationship_type', 'CoOccursWith', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('cfb2c8c7-3126-41d3-a217-3f95f381f334', 'relationship_type', 'AffiliatedWith', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('26aef129-ed79-4b50-8781-d2f6037f9452', 'relationship_type', 'InvolvedIn', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.791784+00', NULL, NULL, '{INVOLVED_IN,TRACKED_BY}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('4cef9416-3ebc-442f-9da5-d5b68e87125c', 'entity_class', 'military_unit', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('8cdd254b-0073-4dd8-a5e2-f982857d541a', 'entity_class', 'political_party', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('3b6cbeae-634a-4528-a681-d0487a361bdb', 'entity_class', 'armed_group', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('7434b846-c544-41ae-9296-77a450c076b5', 'entity_class', 'international_org', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('9e701408-ea5f-4df4-99f1-4454742b7577', 'entity_class', 'media_outlet', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('c9a8ed26-d5d4-4575-8ce0-1745392594e2', 'entity_class', 'event_series', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('a3b77404-1451-4f30-92d2-496655ffa7b8', 'entity_class', 'commodity', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('e5a5b8e7-2369-4780-9a80-b2773b4cb693', 'entity_class', 'infrastructure', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('cc5f5497-7da8-4edd-a26a-3a2776b801c7', 'relationship_type', 'TradesWith', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('de9e6ae1-e17e-48ea-86c0-84d584c843b8', 'relationship_type', 'BordersWith', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('4c8de13b-b7f5-44ba-aec2-7d9bf0ab8dab', 'relationship_type', 'SignatoryTo', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('19dc1917-988a-4d17-86c8-858780244a69', 'relationship_type', 'SanctionsAgainst', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('b667f9a0-179f-47a5-88c2-817ab4683f8f', 'relationship_type', 'OccupiedBy', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('e9cf9992-5e01-413c-bfb1-09a361deb7d2', 'relationship_type', 'SubsidiaryOf', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('a00188ee-ae96-48ea-8dc4-b856c0c5516c', 'relationship_type', 'PartnersWith', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('54d60bbf-a5a4-414f-b647-2fda6197c7a9', 'relationship_type', 'CompetesWith', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('eeabfc78-e8b0-4b95-ab75-c1763d0ef5fd', 'relationship_type', 'DiplomaticRelationsWith', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);
INSERT INTO public.vocabulary_entries (id, family, value, schema_uri, introduced, deprecated, notes, aliases, parent) VALUES ('5fcbdf3f-bf30-4187-b125-11022d8e2270', 'relationship_type', 'MilitaryPresenceIn', 'iglu:legba/vocabulary/jsonschema/1-0-0', '2026-06-10 08:44:45.870824+00', NULL, NULL, '{}', NULL);


--
-- Data for Name: wiring_descriptors; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Name: action_pack_descriptors action_pack_descriptors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.action_pack_descriptors
    ADD CONSTRAINT action_pack_descriptors_pkey PRIMARY KEY (descriptor_id, version);


--
-- Name: action_pack_invocations action_pack_invocations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.action_pack_invocations
    ADD CONSTRAINT action_pack_invocations_pkey PRIMARY KEY (id);


--
-- Name: alert_sink_deliveries alert_sink_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_sink_deliveries
    ADD CONSTRAINT alert_sink_deliveries_pkey PRIMARY KEY (id);


--
-- Name: analyst_critiques analyst_critiques_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analyst_critiques
    ADD CONSTRAINT analyst_critiques_pkey PRIMARY KEY (id);


--
-- Name: analyst_descriptors analyst_descriptors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analyst_descriptors
    ADD CONSTRAINT analyst_descriptors_pkey PRIMARY KEY (descriptor_id, version);


--
-- Name: analyst_outputs analyst_outputs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analyst_outputs
    ADD CONSTRAINT analyst_outputs_pkey PRIMARY KEY (id);


--
-- Name: analyst_traces analyst_traces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analyst_traces
    ADD CONSTRAINT analyst_traces_pkey PRIMARY KEY (run_id);


--
-- Name: audit_checkpoints audit_checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_checkpoints
    ADD CONSTRAINT audit_checkpoints_pkey PRIMARY KEY (id);


--
-- Name: budget_demotion_events budget_demotion_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.budget_demotion_events
    ADD CONSTRAINT budget_demotion_events_pkey PRIMARY KEY (id);


--
-- Name: budget_ledger budget_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.budget_ledger
    ADD CONSTRAINT budget_ledger_pkey PRIMARY KEY (analyst_id, analyst_version, bucket);


--
-- Name: conversion_executions conversion_executions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversion_executions
    ADD CONSTRAINT conversion_executions_pkey PRIMARY KEY (id);


--
-- Name: conversion_webhooks conversion_webhooks_from_uri_to_uri_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversion_webhooks
    ADD CONSTRAINT conversion_webhooks_from_uri_to_uri_key UNIQUE (from_uri, to_uri);


--
-- Name: conversion_webhooks conversion_webhooks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversion_webhooks
    ADD CONSTRAINT conversion_webhooks_pkey PRIMARY KEY (id);


--
-- Name: descriptor_audit_log descriptor_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.descriptor_audit_log
    ADD CONSTRAINT descriptor_audit_log_pkey PRIMARY KEY (id);


--
-- Name: descriptor_conversion_archives descriptor_conversion_archives_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.descriptor_conversion_archives
    ADD CONSTRAINT descriptor_conversion_archives_pkey PRIMARY KEY (id);


--
-- Name: descriptor_dead_letter descriptor_dead_letter_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.descriptor_dead_letter
    ADD CONSTRAINT descriptor_dead_letter_pkey PRIMARY KEY (id);


--
-- Name: discovery_state discovery_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovery_state
    ADD CONSTRAINT discovery_state_pkey PRIMARY KEY (discovery_id, natural_key);


--
-- Name: entity_profile_versions entity_profile_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_profile_versions
    ADD CONSTRAINT entity_profile_versions_pkey PRIMARY KEY (id);


--
-- Name: entity_profiles entity_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_profiles
    ADD CONSTRAINT entity_profiles_pkey PRIMARY KEY (id);


--
-- Name: facts facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.facts
    ADD CONSTRAINT facts_pkey PRIMARY KEY (id);


--
-- Name: finding_supersessions finding_supersessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.finding_supersessions
    ADD CONSTRAINT finding_supersessions_pkey PRIMARY KEY (superseded_finding_id, superseding_finding_id);


--
-- Name: global_budget_envelope global_budget_envelope_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.global_budget_envelope
    ADD CONSTRAINT global_budget_envelope_pkey PRIMARY KEY (bucket);


--
-- Name: governor_events governor_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.governor_events
    ADD CONSTRAINT governor_events_pkey PRIMARY KEY (id);


--
-- Name: graph_metrics graph_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_metrics
    ADD CONSTRAINT graph_metrics_pkey PRIMARY KEY (metric_kind, computed_at);


--
-- Name: hypotheses hypotheses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypotheses
    ADD CONSTRAINT hypotheses_pkey PRIMARY KEY (id);


--
-- Name: iso_countries iso_countries_iso3_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.iso_countries
    ADD CONSTRAINT iso_countries_iso3_key UNIQUE (iso3);


--
-- Name: iso_countries iso_countries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.iso_countries
    ADD CONSTRAINT iso_countries_pkey PRIMARY KEY (iso2);


--
-- Name: output_dead_letter output_dead_letter_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.output_dead_letter
    ADD CONSTRAINT output_dead_letter_pkey PRIMARY KEY (id);


--
-- Name: proposed_edges proposed_edges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposed_edges
    ADD CONSTRAINT proposed_edges_pkey PRIMARY KEY (id);


--
-- Name: signal_aliases signal_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signal_aliases
    ADD CONSTRAINT signal_aliases_pkey PRIMARY KEY (alias_signal_id, canonical_signal_id);


--
-- Name: signal_entity_links signal_entity_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signal_entity_links
    ADD CONSTRAINT signal_entity_links_pkey PRIMARY KEY (signal_id, entity_id, role);


--
-- Name: signals signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signals
    ADD CONSTRAINT signals_pkey PRIMARY KEY (id);


--
-- Name: situations situations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.situations
    ADD CONSTRAINT situations_pkey PRIMARY KEY (id);


--
-- Name: source_credibility source_credibility_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_credibility
    ADD CONSTRAINT source_credibility_pkey PRIMARY KEY (source_host);


--
-- Name: source_descriptors source_descriptors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_descriptors
    ADD CONSTRAINT source_descriptors_pkey PRIMARY KEY (descriptor_id, version);


--
-- Name: stack_components stack_components_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stack_components
    ADD CONSTRAINT stack_components_pkey PRIMARY KEY (component_id, version);


--
-- Name: stack_credentials stack_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stack_credentials
    ADD CONSTRAINT stack_credentials_pkey PRIMARY KEY (secret_id, version);


--
-- Name: target_descriptors target_descriptors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.target_descriptors
    ADD CONSTRAINT target_descriptors_pkey PRIMARY KEY (descriptor_id, version);


--
-- Name: trigger_state trigger_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_state
    ADD CONSTRAINT trigger_state_pkey PRIMARY KEY (analyst_id, target_id);


--
-- Name: ui_panel_registrations ui_panel_registrations_descriptor_id_descriptor_version_pan_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ui_panel_registrations
    ADD CONSTRAINT ui_panel_registrations_descriptor_id_descriptor_version_pan_key UNIQUE (descriptor_id, descriptor_version, panel_id);


--
-- Name: ui_panel_registrations ui_panel_registrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ui_panel_registrations
    ADD CONSTRAINT ui_panel_registrations_pkey PRIMARY KEY (id);


--
-- Name: vocabulary_entries vocabulary_entries_family_value_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vocabulary_entries
    ADD CONSTRAINT vocabulary_entries_family_value_key UNIQUE (family, value);


--
-- Name: vocabulary_entries vocabulary_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vocabulary_entries
    ADD CONSTRAINT vocabulary_entries_pkey PRIMARY KEY (id);


--
-- Name: wiring_descriptors wiring_descriptors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wiring_descriptors
    ADD CONSTRAINT wiring_descriptors_pkey PRIMARY KEY (descriptor_id, version);


--
-- Name: action_pack_descriptors_head_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX action_pack_descriptors_head_unique ON public.action_pack_descriptors USING btree (descriptor_id) WHERE is_head;


--
-- Name: action_pack_descriptors_schema_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX action_pack_descriptors_schema_idx ON public.action_pack_descriptors USING btree (schema_uri);


--
-- Name: action_pack_descriptors_state_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX action_pack_descriptors_state_idx ON public.action_pack_descriptors USING btree (state);


--
-- Name: action_pack_invocations_account_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX action_pack_invocations_account_idx ON public.action_pack_invocations USING btree (budget_account, occurred_at DESC);


--
-- Name: action_pack_invocations_tool_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX action_pack_invocations_tool_idx ON public.action_pack_invocations USING btree (pack_id, tool_name);


--
-- Name: action_pack_invocations_window_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX action_pack_invocations_window_idx ON public.action_pack_invocations USING btree (pack_id, budget_account, occurred_at DESC);


--
-- Name: analyst_critiques_judge_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX analyst_critiques_judge_idx ON public.analyst_critiques USING btree (judge_analyst_id, produced_at DESC);


--
-- Name: analyst_critiques_trace_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX analyst_critiques_trace_idx ON public.analyst_critiques USING btree (trace_id);


--
-- Name: analyst_descriptors_head_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX analyst_descriptors_head_unique ON public.analyst_descriptors USING btree (descriptor_id) WHERE is_head;


--
-- Name: analyst_descriptors_kind_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX analyst_descriptors_kind_idx ON public.analyst_descriptors USING btree (kind);


--
-- Name: analyst_descriptors_state_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX analyst_descriptors_state_idx ON public.analyst_descriptors USING btree (state);


--
-- Name: analyst_traces_analyst_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX analyst_traces_analyst_idx ON public.analyst_traces USING btree (analyst_id, run_started_at DESC);


--
-- Name: analyst_traces_input_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX analyst_traces_input_gin ON public.analyst_traces USING gin (input_row_refs);


--
-- Name: analyst_traces_output_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX analyst_traces_output_gin ON public.analyst_traces USING gin (output_row_refs);


--
-- Name: analyst_traces_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX analyst_traces_status_idx ON public.analyst_traces USING btree (status) WHERE (status <> 'success'::text);


--
-- Name: analyst_traces_target_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX analyst_traces_target_idx ON public.analyst_traces USING btree (target_id, run_started_at DESC) WHERE (target_id IS NOT NULL);


--
-- Name: audit_checkpoints_analyst_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX audit_checkpoints_analyst_idx ON public.audit_checkpoints USING btree (analyst_id, checkpointed_at DESC);


--
-- Name: budget_demotion_events_analyst_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX budget_demotion_events_analyst_idx ON public.budget_demotion_events USING btree (analyst_id, bucket DESC);


--
-- Name: budget_demotion_events_bucket_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX budget_demotion_events_bucket_idx ON public.budget_demotion_events USING btree (bucket DESC, occurred_at DESC);


--
-- Name: budget_ledger_bucket_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX budget_ledger_bucket_idx ON public.budget_ledger USING btree (bucket DESC);


--
-- Name: budget_ledger_cost_estimate_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX budget_ledger_cost_estimate_idx ON public.budget_ledger USING btree (bucket DESC, cost_estimate_usd DESC) WHERE (cost_estimate_usd > (0)::numeric);


--
-- Name: conversion_executions_descriptor_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversion_executions_descriptor_idx ON public.conversion_executions USING btree (namespace, descriptor_id, executed_at DESC);


--
-- Name: conversion_executions_failed_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversion_executions_failed_idx ON public.conversion_executions USING btree (executed_at DESC) WHERE (success = false);


--
-- Name: conversion_webhooks_active_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversion_webhooks_active_idx ON public.conversion_webhooks USING btree (from_uri, to_uri) WHERE (retired_at IS NULL);


--
-- Name: conversion_webhooks_from_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversion_webhooks_from_idx ON public.conversion_webhooks USING btree (from_uri);


--
-- Name: conversion_webhooks_to_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversion_webhooks_to_idx ON public.conversion_webhooks USING btree (to_uri);


--
-- Name: descriptor_audit_action_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX descriptor_audit_action_idx ON public.descriptor_audit_log USING btree (action, occurred_at DESC);


--
-- Name: descriptor_audit_actor_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX descriptor_audit_actor_idx ON public.descriptor_audit_log USING btree (actor_id, occurred_at DESC);


--
-- Name: descriptor_audit_descriptor_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX descriptor_audit_descriptor_idx ON public.descriptor_audit_log USING btree (descriptor_id, occurred_at DESC);


--
-- Name: descriptor_conversion_archives_descriptor_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX descriptor_conversion_archives_descriptor_idx ON public.descriptor_conversion_archives USING btree (namespace, descriptor_id, archived_at DESC);


--
-- Name: descriptor_conversion_archives_webhook_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX descriptor_conversion_archives_webhook_idx ON public.descriptor_conversion_archives USING btree (webhook_id);


--
-- Name: descriptor_dl_ns_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX descriptor_dl_ns_idx ON public.descriptor_dead_letter USING btree (namespace, attempted_at DESC);


--
-- Name: descriptor_dl_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX descriptor_dl_open_idx ON public.descriptor_dead_letter USING btree (attempted_at DESC) WHERE (resolution IS NULL);


--
-- Name: discovery_state_descriptor_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX discovery_state_descriptor_idx ON public.discovery_state USING btree (descriptor_id);


--
-- Name: discovery_state_discovery_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX discovery_state_discovery_idx ON public.discovery_state USING btree (discovery_id);


--
-- Name: discovery_state_family_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX discovery_state_family_idx ON public.discovery_state USING btree (family);


--
-- Name: finding_supersessions_situation_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX finding_supersessions_situation_idx ON public.finding_supersessions USING btree (situation_signature);


--
-- Name: finding_supersessions_superseding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX finding_supersessions_superseding_idx ON public.finding_supersessions USING btree (superseding_finding_id);


--
-- Name: governor_events_account_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX governor_events_account_idx ON public.governor_events USING btree (budget_account, occurred_at DESC);


--
-- Name: governor_events_decision_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX governor_events_decision_idx ON public.governor_events USING btree (decision, occurred_at DESC);


--
-- Name: governor_events_pack_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX governor_events_pack_idx ON public.governor_events USING btree (pack_id, occurred_at DESC);


--
-- Name: graph_metrics_kind_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX graph_metrics_kind_idx ON public.graph_metrics USING btree (metric_kind, computed_at DESC);


--
-- Name: idx_analyst_outputs_analyst_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_analyst_outputs_analyst_id ON public.analyst_outputs USING btree (analyst_id) WHERE (analyst_id IS NOT NULL);


--
-- Name: idx_analyst_outputs_derived_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_analyst_outputs_derived_from ON public.analyst_outputs USING gin (derived_from);


--
-- Name: idx_analyst_outputs_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_analyst_outputs_kind ON public.analyst_outputs USING btree (kind, produced_at DESC);


--
-- Name: idx_analyst_outputs_produced_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_analyst_outputs_produced_at ON public.analyst_outputs USING btree (produced_at DESC);


--
-- Name: idx_analyst_outputs_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_analyst_outputs_run_id ON public.analyst_outputs USING btree (run_id) WHERE (run_id IS NOT NULL);


--
-- Name: idx_analyst_outputs_severity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_analyst_outputs_severity ON public.analyst_outputs USING btree (severity) WHERE ((severity IS NOT NULL) AND (severity = ANY (ARRAY['high'::text, 'critical'::text])));


--
-- Name: idx_analyst_outputs_situation_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_analyst_outputs_situation_latest ON public.analyst_outputs USING btree (situation_signature) WHERE ((situation_signature IS NOT NULL) AND (superseded_by IS NULL));


--
-- Name: idx_analyst_outputs_situation_signature; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_analyst_outputs_situation_signature ON public.analyst_outputs USING btree (situation_signature) WHERE (situation_signature IS NOT NULL);


--
-- Name: idx_analyst_outputs_superseded_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_analyst_outputs_superseded_by ON public.analyst_outputs USING btree (superseded_by) WHERE (superseded_by IS NOT NULL);


--
-- Name: idx_analyst_outputs_target_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_analyst_outputs_target_id ON public.analyst_outputs USING btree (target_id) WHERE (target_id IS NOT NULL);


--
-- Name: idx_asd_alert_row; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asd_alert_row ON public.alert_sink_deliveries USING btree (alert_row_id);


--
-- Name: idx_asd_attempted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asd_attempted_at ON public.alert_sink_deliveries USING btree (attempted_at DESC);


--
-- Name: idx_asd_sink_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asd_sink_status ON public.alert_sink_deliveries USING btree (sink_kind, status);


--
-- Name: idx_entity_profiles_analyst_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entity_profiles_analyst_id ON public.entity_profiles USING btree (analyst_id) WHERE (analyst_id IS NOT NULL);


--
-- Name: idx_entity_profiles_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entity_profiles_class ON public.entity_profiles USING btree (entity_class);


--
-- Name: idx_entity_profiles_derived_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entity_profiles_derived_from ON public.entity_profiles USING gin (derived_from);


--
-- Name: idx_entity_profiles_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_entity_profiles_name ON public.entity_profiles USING btree (lower(canonical_name));


--
-- Name: idx_entity_profiles_produced_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entity_profiles_produced_at ON public.entity_profiles USING btree (produced_at DESC);


--
-- Name: idx_entity_profiles_target_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entity_profiles_target_id ON public.entity_profiles USING btree (target_id) WHERE (target_id IS NOT NULL);


--
-- Name: idx_epv_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_epv_created ON public.entity_profile_versions USING btree (entity_id, created_at DESC);


--
-- Name: idx_epv_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_epv_entity ON public.entity_profile_versions USING btree (entity_id, version DESC);


--
-- Name: idx_facts_analyst_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_facts_analyst_id ON public.facts USING btree (analyst_id) WHERE (analyst_id IS NOT NULL);


--
-- Name: idx_facts_derived_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_facts_derived_from ON public.facts USING gin (derived_from);


--
-- Name: idx_facts_predicate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_facts_predicate ON public.facts USING btree (predicate);


--
-- Name: idx_facts_produced_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_facts_produced_at ON public.facts USING btree (produced_at DESC);


--
-- Name: idx_facts_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_facts_subject ON public.facts USING btree (subject);


--
-- Name: idx_facts_target_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_facts_target_id ON public.facts USING btree (target_id) WHERE (target_id IS NOT NULL);


--
-- Name: idx_facts_temporal_triple; Type: INDEX; Schema: public; Owner: -
--
-- NOTE: migration 0032_facts_decay_columns.sql DROPs this FULL unique index and
-- replaces it with the partial-on-open `idx_facts_temporal_triple_open` (PIECE B
-- temporal-fact hardening). A full index over all rows would let the ON CONFLICT
-- upsert match a CLOSED (superseded) row that retains the same triple+valid_from,
-- resurrecting it and dangling its superseded_by pointer. See 0032 for the why.
--

CREATE UNIQUE INDEX idx_facts_temporal_triple ON public.facts USING btree (lower(subject), lower(predicate), lower(value), COALESCE(valid_from, '1970-01-01 00:00:00+00'::timestamp with time zone));


--
-- Name: idx_hypotheses_analyst_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_hypotheses_analyst_id ON public.hypotheses USING btree (analyst_id) WHERE (analyst_id IS NOT NULL);


--
-- Name: idx_hypotheses_derived_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_hypotheses_derived_from ON public.hypotheses USING gin (derived_from);


--
-- Name: idx_hypotheses_produced_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_hypotheses_produced_at ON public.hypotheses USING btree (produced_at DESC);


--
-- Name: idx_hypotheses_situation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_hypotheses_situation ON public.hypotheses USING btree (situation_id);


--
-- Name: idx_hypotheses_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_hypotheses_status ON public.hypotheses USING btree (status);


--
-- Name: idx_hypotheses_target_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_hypotheses_target_id ON public.hypotheses USING btree (target_id) WHERE (target_id IS NOT NULL);


--
-- Name: idx_proposed_edges_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_proposed_edges_status ON public.proposed_edges USING btree (status);


--
-- Name: idx_sel_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sel_entity ON public.signal_entity_links USING btree (entity_id);


--
-- Name: idx_sel_signal_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sel_signal_entity ON public.signal_entity_links USING btree (signal_id);


--
-- Name: idx_signals_entities_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_signals_entities_unresolved ON public.signals USING btree (fetched_at) WHERE (entities_resolved_at IS NULL);


--
-- Name: idx_situations_analyst_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_situations_analyst_id ON public.situations USING btree (analyst_id) WHERE (analyst_id IS NOT NULL);


--
-- Name: idx_situations_derived_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_situations_derived_from ON public.situations USING gin (derived_from);


--
-- Name: idx_situations_produced_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_situations_produced_at ON public.situations USING btree (produced_at DESC);


--
-- Name: idx_situations_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_situations_status ON public.situations USING btree (status);


--
-- Name: idx_situations_target_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_situations_target_id ON public.situations USING btree (target_id) WHERE (target_id IS NOT NULL);


--
-- Name: idx_source_credibility_last_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_source_credibility_last_updated ON public.source_credibility USING btree (last_updated DESC);


--
-- Name: idx_source_credibility_score; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_source_credibility_score ON public.source_credibility USING btree (score);


--
-- Name: iso_countries_region_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX iso_countries_region_idx ON public.iso_countries USING btree (region);


--
-- Name: iso_countries_subregion_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX iso_countries_subregion_idx ON public.iso_countries USING btree (subregion);


--
-- Name: output_dl_analyst_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX output_dl_analyst_idx ON public.output_dead_letter USING btree (analyst_id, produced_at DESC);


--
-- Name: output_dl_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX output_dl_open_idx ON public.output_dead_letter USING btree (produced_at DESC) WHERE (resolution IS NULL);


--
-- Name: signal_aliases_canonical_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signal_aliases_canonical_idx ON public.signal_aliases USING btree (canonical_signal_id);


--
-- Name: signals_canonical_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signals_canonical_idx ON public.signals USING btree (canonical_signal_id);


--
-- Name: signals_content_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signals_content_hash_idx ON public.signals USING btree (content_hash);


--
-- Name: signals_derived_from_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signals_derived_from_gin ON public.signals USING gin (derived_from);


--
-- Name: signals_entity_classes_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signals_entity_classes_gin ON public.signals USING gin (entity_classes);


--
-- Name: signals_fetched_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signals_fetched_at_idx ON public.signals USING btree (fetched_at DESC);


--
-- Name: signals_geo_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signals_geo_gin ON public.signals USING gin (geo);


--
-- Name: signals_language_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signals_language_idx ON public.signals USING btree (language);


--
-- Name: signals_modality_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signals_modality_idx ON public.signals USING btree (modality);


--
-- Name: signals_owner_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signals_owner_tenant_idx ON public.signals USING btree (owner_tenant);


--
-- Name: signals_source_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signals_source_idx ON public.signals USING btree (source_id);


--
-- Name: signals_tags_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX signals_tags_gin ON public.signals USING gin (tags);


--
-- Name: source_descriptors_head_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX source_descriptors_head_unique ON public.source_descriptors USING btree (descriptor_id) WHERE is_head;


--
-- Name: source_descriptors_kind_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX source_descriptors_kind_idx ON public.source_descriptors USING btree (kind);


--
-- Name: source_descriptors_schema_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX source_descriptors_schema_idx ON public.source_descriptors USING btree (schema_uri);


--
-- Name: source_descriptors_state_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX source_descriptors_state_idx ON public.source_descriptors USING btree (state);


--
-- Name: stack_components_head_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX stack_components_head_unique ON public.stack_components USING btree (component_id) WHERE is_head;


--
-- Name: stack_components_kind_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stack_components_kind_idx ON public.stack_components USING btree (kind);


--
-- Name: stack_components_state_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stack_components_state_idx ON public.stack_components USING btree (state);


--
-- Name: stack_credentials_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stack_credentials_created_idx ON public.stack_credentials USING btree (created_at DESC);


--
-- Name: stack_credentials_current_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX stack_credentials_current_unique ON public.stack_credentials USING btree (secret_id) WHERE is_current;


--
-- Name: target_descriptors_head_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX target_descriptors_head_unique ON public.target_descriptors USING btree (descriptor_id) WHERE is_head;


--
-- Name: target_descriptors_schema_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX target_descriptors_schema_idx ON public.target_descriptors USING btree (schema_uri);


--
-- Name: target_descriptors_state_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX target_descriptors_state_idx ON public.target_descriptors USING btree (state);


--
-- Name: trigger_state_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX trigger_state_pending_idx ON public.trigger_state USING btree (pending_count) WHERE (pending_count > 0);


--
-- Name: trigger_state_target_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX trigger_state_target_idx ON public.trigger_state USING btree (target_id);


--
-- Name: ui_panel_registrations_analyst_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ui_panel_registrations_analyst_idx ON public.ui_panel_registrations USING btree (analyst_id) WHERE ((analyst_id IS NOT NULL) AND (retired = false));


--
-- Name: ui_panel_registrations_descriptor_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ui_panel_registrations_descriptor_idx ON public.ui_panel_registrations USING btree (descriptor_id, descriptor_version);


--
-- Name: ui_panel_registrations_layout_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ui_panel_registrations_layout_idx ON public.ui_panel_registrations USING btree (layout_slot) WHERE (retired = false);


--
-- Name: ui_panel_registrations_mode_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ui_panel_registrations_mode_idx ON public.ui_panel_registrations USING btree (mode) WHERE (retired = false);


--
-- Name: ui_panel_registrations_panel_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ui_panel_registrations_panel_idx ON public.ui_panel_registrations USING btree (panel_id) WHERE (retired = false);


--
-- Name: ui_panel_registrations_slot_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ui_panel_registrations_slot_unique ON public.ui_panel_registrations USING btree (mode, layout_slot) WHERE (retired = false);


--
-- Name: uq_proposed_edges_triple; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_proposed_edges_triple ON public.proposed_edges USING btree (lower(source_entity), lower(target_entity), relationship_type);


--
-- Name: vocabulary_entries_active_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vocabulary_entries_active_idx ON public.vocabulary_entries USING btree (family, value) WHERE (deprecated IS NULL);


--
-- Name: vocabulary_entries_family_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vocabulary_entries_family_idx ON public.vocabulary_entries USING btree (family);


--
-- Name: wiring_descriptors_head_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX wiring_descriptors_head_unique ON public.wiring_descriptors USING btree (descriptor_id) WHERE is_head;


--
-- Name: alert_sink_deliveries alert_sink_deliveries_alert_row_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_sink_deliveries
    ADD CONSTRAINT alert_sink_deliveries_alert_row_id_fkey FOREIGN KEY (alert_row_id) REFERENCES public.analyst_outputs(id) ON DELETE CASCADE;


--
-- Name: analyst_critiques analyst_critiques_trace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analyst_critiques
    ADD CONSTRAINT analyst_critiques_trace_id_fkey FOREIGN KEY (trace_id) REFERENCES public.analyst_traces(run_id) ON DELETE CASCADE;


--
-- Name: descriptor_conversion_archives descriptor_conversion_archives_webhook_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.descriptor_conversion_archives
    ADD CONSTRAINT descriptor_conversion_archives_webhook_id_fkey FOREIGN KEY (webhook_id) REFERENCES public.conversion_webhooks(id);


--
-- Name: entity_profile_versions entity_profile_versions_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_profile_versions
    ADD CONSTRAINT entity_profile_versions_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity_profiles(id) ON DELETE CASCADE;


--
-- Name: hypotheses hypotheses_situation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypotheses
    ADD CONSTRAINT hypotheses_situation_id_fkey FOREIGN KEY (situation_id) REFERENCES public.situations(id);


--
-- Name: output_dead_letter output_dead_letter_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.output_dead_letter
    ADD CONSTRAINT output_dead_letter_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.analyst_traces(run_id) ON DELETE SET NULL;


--
-- Name: signal_entity_links signal_entity_links_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signal_entity_links
    ADD CONSTRAINT signal_entity_links_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity_profiles(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--


