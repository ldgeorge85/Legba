-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0039_consult_sessions.sql — consult audit trail (prior-chats + continue +
-- deep-consult task history).
--
-- WHY:
--   The consult panel (chat) and Deep Consult panel hold their transcripts
--   ENTIRELY client-side: the registry consult proxy
--   (data/registry/consult_api.py) re-sends `messages[]` on every turn and the
--   answer comes back in the envelope (chat) or a finding row (deep). Nothing is
--   persisted server-side, so a closed tab loses the conversation and there is
--   no operator-visible audit trail of what was asked / answered / which
--   substrate rows were cited. Deep-consult submissions are likewise fire-and-
--   poll with no durable task-history list.
--
--   This migration lands the audit trail: a `consult_sessions` header per
--   conversation/task + a `consult_turns` append-only log of each user/assistant
--   turn (chat OR deep), carrying the ReAct `steps`, `tool_calls`, and
--   `cited_refs` projections plus the optional `finding_id` (deep mode). The
--   server writes each turn (see consult_api.py / deep_consult_api.py); the read
--   routes GET /api/v1/consult/sessions (list) + /consult/sessions/{id} (load)
--   re-seed the client transcript so a prior session can be continued.
--
-- TABLE SHAPE:
--   consult_sessions — one row per conversation (chat) or deep-consult task.
--     `mode` is 'chat' | 'deep'; `task_id`/`run_id` populated for deep tasks so
--     the deep-consult status poll can correlate. `title` is a short label
--     (first question, truncated) for the history sidebar.
--   consult_turns — append-only; (session_id, role, content) plus the jsonb
--     `steps` / `tool_calls` / `cited_refs` projections and a nullable
--     `finding_id` (the durable deep-mode finding, when one was written).
--
-- SAFETY (idempotent, additive, CREATE-only):
--   Every statement is CREATE TABLE / INDEX IF NOT EXISTS — re-running against
--   an already-migrated DB is a no-op. No data migration (clean-slate policy).

CREATE TABLE IF NOT EXISTS public.consult_sessions (
    id            uuid DEFAULT gen_random_uuid() NOT NULL,

    -- 'chat' (ephemeral chat transcript) | 'deep' (detached workflow task).
    mode          text DEFAULT 'chat'::text NOT NULL,
    -- Short human label for the history list (first question, truncated).
    title         text DEFAULT ''::text NOT NULL,
    -- The bearer principal that opened the session (audit attribution). The
    -- dev-mode token is optional, so this is nullable.
    principal     text,

    -- Deep-consult correlation: the detached task id + workflow run id the
    -- status poll keys on. NULL for chat sessions.
    task_id       text,
    run_id        text,

    data          jsonb DEFAULT '{}'::jsonb NOT NULL,

    created_at    timestamp with time zone DEFAULT now() NOT NULL,
    updated_at    timestamp with time zone DEFAULT now() NOT NULL,

    CONSTRAINT consult_sessions_pkey PRIMARY KEY (id),
    CONSTRAINT consult_sessions_mode_ck CHECK (mode IN ('chat', 'deep'))
);

-- History-list ordering: the sidebar lists most-recently-active first.
CREATE INDEX IF NOT EXISTS idx_consult_sessions_updated
    ON public.consult_sessions (updated_at DESC);

-- Deep-consult task correlation lookup (status poll / task-history join).
CREATE INDEX IF NOT EXISTS idx_consult_sessions_task
    ON public.consult_sessions (task_id)
    WHERE task_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.consult_turns (
    id            uuid DEFAULT gen_random_uuid() NOT NULL,

    -- The owning session. FK with ON DELETE CASCADE so deleting a session
    -- reaps its turns (no orphan log rows).
    session_id    uuid NOT NULL,

    -- 'user' (the asked question) | 'assistant' (the synthesised answer).
    role          text NOT NULL,
    content       text DEFAULT ''::text NOT NULL,

    -- The ReAct step trace, projected tool calls, and cited substrate refs for
    -- this turn (assistant turns; empty for user turns). jsonb so the load
    -- route can re-seed the client transcript verbatim.
    steps         jsonb DEFAULT '[]'::jsonb NOT NULL,
    tool_calls    jsonb DEFAULT '[]'::jsonb NOT NULL,
    cited_refs    jsonb DEFAULT '[]'::jsonb NOT NULL,

    -- The durable finding this assistant turn wrote (deep mode), if any. Plain
    -- text (the finding id is a uuid but we keep it text to avoid coupling the
    -- log to analyst_outputs' lifecycle — a deleted finding shouldn't break the
    -- audit row).
    finding_id    text,

    created_at    timestamp with time zone DEFAULT now() NOT NULL,

    CONSTRAINT consult_turns_pkey PRIMARY KEY (id),
    CONSTRAINT consult_turns_role_ck CHECK (role IN ('user', 'assistant')),
    CONSTRAINT consult_turns_session_fk
        FOREIGN KEY (session_id) REFERENCES public.consult_sessions (id)
        ON DELETE CASCADE
);

-- Load-route ordering: turns replay oldest-first within a session.
CREATE INDEX IF NOT EXISTS idx_consult_turns_session_created
    ON public.consult_turns (session_id, created_at);
