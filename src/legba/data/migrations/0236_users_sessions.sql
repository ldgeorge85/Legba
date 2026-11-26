-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0236_users_sessions.sql
--
-- LOCAL USERS + SIGN-IN.
--
-- WHY
-- ---
-- Until now every viewer was the operator: caddy's basic_auth perimeter
-- injected the operator bearer upstream for every browser. A dozen invited
-- people need their own identity, private consult sessions, usage per person
-- and a ROLE (the route gate's three classes). Local users with external
-- auth: so the users
-- table is OURS (an internal id every other row keys on) and how a person
-- proves who they are is PLUGGABLE (`legba.data.users`: `local_password`
-- now, `oidc` declared — it maps provider + subject onto a row here and
-- refuses an unknown subject; invite-only either way).
--
-- WHAT THIS ADDS
-- --------------
--   * `users` — internal id (uuid), optional email (unique, case-folded),
--     display name, role ∈ the route gate's classes, status ∈ {invited,
--     active, disabled}, the auth provider, the external subject (OIDC),
--     created / updated / last sign-in. NO password column: a credential is
--     the provider's, not the user's.
--   * `user_credentials` — one row per (user, provider) secret. Only
--     `local_password` writes here, an argon2id hash (libsodium's
--     crypto_pwhash via PyNaCl); never a plaintext.
--   * `user_sessions` — an opaque session id is handed to the browser ONCE
--     (the HttpOnly cookie); only its sha256 is stored, so a read of this
--     table cannot sign anyone in. Expires; revocable.
--   * `user_invites` — an invite token, sha256 at rest, bound to one email +
--     role, expiring, single-use (`used_at`).
--
-- The audit actor of a signed-in user is `user:<users.id>` everywhere
-- (`consult_sessions.principal`, the descriptor audit log, ...); the route
-- gate's subject is the bare id.
--
-- SAFETY
-- ------
-- Additive and idempotent (CREATE ... IF NOT EXISTS); four new tables, and
-- one new index on the existing `consult_sessions` (96 rows live on 10-08;
-- no column touched); routed through the runner (no inline
-- BEGIN/COMMIT). No retention policy: identities and their sessions are the
-- operator's record (expired sessions are inert; a sweep is not needed for
-- correctness).

CREATE TABLE IF NOT EXISTS public.users (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email            text,
    display_name     text NOT NULL DEFAULT '',
    role             text NOT NULL DEFAULT 'read',
    status           text NOT NULL DEFAULT 'invited',
    auth_provider    text NOT NULL DEFAULT 'local_password',
    external_subject text,
    invited_by       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    last_sign_in_at  timestamptz,
    CONSTRAINT users_role_check
        CHECK (role IN ('read', 'reader-own-rows', 'operator')),
    CONSTRAINT users_status_check
        CHECK (status IN ('invited', 'active', 'disabled')),
    CONSTRAINT users_auth_provider_check
        CHECK (auth_provider IN ('local_password', 'oidc')),
    CONSTRAINT users_email_shape_check
        CHECK (email IS NULL OR (email = lower(btrim(email)) AND email LIKE '%_@_%'))
);

COMMENT ON TABLE public.users IS
    'Local identities (mig 0236, X3-B3). role = the route gate class; '
    'the audit actor is user:<id>. Authentication is the provider''s '
    '(legba.data.users).';

-- One account per email (stored case-folded; the CHECK above enforces it).
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email
    ON public.users (email) WHERE email IS NOT NULL;

-- One row per external identity: the OIDC map is provider + subject → id.
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_external_subject
    ON public.users (auth_provider, external_subject)
    WHERE external_subject IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.user_credentials (
    user_id      uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    provider     text NOT NULL,
    secret_hash  text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, provider),
    CONSTRAINT user_credentials_provider_check
        CHECK (provider IN ('local_password')),
    -- A plaintext can never land here: only an argon2id PHC string can.
    CONSTRAINT user_credentials_hash_shape_check
        CHECK (secret_hash LIKE '$argon2id$%')
);

COMMENT ON TABLE public.user_credentials IS
    'Provider secrets (mig 0236). local_password = argon2id PHC string '
    '(libsodium crypto_pwhash); never a plaintext.';

CREATE TABLE IF NOT EXISTS public.user_sessions (
    id_hash      text PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    CONSTRAINT user_sessions_id_hash_shape_check
        CHECK (id_hash ~ '^[0-9a-f]{64}$')
);

COMMENT ON TABLE public.user_sessions IS
    'Sign-in sessions (mig 0236). id_hash = sha256 of the opaque cookie '
    'value; the value itself is never stored.';

-- The sign-out-everywhere / disable path revokes a user's live sessions.
CREATE INDEX IF NOT EXISTS idx_user_sessions_user_live
    ON public.user_sessions (user_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS public.user_invites (
    token_hash   text PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    email        text NOT NULL,
    role         text NOT NULL,
    invited_by   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    used_at      timestamptz,
    CONSTRAINT user_invites_role_check
        CHECK (role IN ('read', 'reader-own-rows', 'operator')),
    CONSTRAINT user_invites_token_hash_shape_check
        CHECK (token_hash ~ '^[0-9a-f]{64}$')
);

COMMENT ON TABLE public.user_invites IS
    'Single-use, expiring invites (mig 0236). token_hash = sha256 of the '
    'token in the invite link; the token itself is never stored.';

CREATE INDEX IF NOT EXISTS idx_user_invites_user
    ON public.user_invites (user_id);

-- The own-sessions read: a signed-in user's history list is
-- `WHERE principal = 'user:<id>' ORDER BY updated_at DESC LIMIT n`, and the
-- consult usage read filters on the same column. An index on an EXISTING
-- table — the only one this migration touches; additive, IF NOT EXISTS.
CREATE INDEX IF NOT EXISTS idx_consult_sessions_principal_updated
    ON public.consult_sessions (principal, updated_at DESC);
