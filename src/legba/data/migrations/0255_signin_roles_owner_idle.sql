-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0255_signin_roles_owner_idle.sql
--
-- SIGN-IN STEP 1 (A-1): the three role names, the owner flag, the idle limit.
--
-- WHY
-- ---
-- The accounts design (users / groups / roles, step 1) turns the October
-- accounts on behind one switch, LEGBA_AUTH_SIGNIN. With it on, people hold
-- one of three roles -- reader, contributor, admin -- and the owner is an
-- admin with a flag. Sessions gain an idle limit beside their absolute
-- lifetime. Migration 0236 allowed only the October class names
-- ('read', 'reader-own-rows', 'operator') and kept no last-seen time.
--
-- WHAT THIS CHANGES
-- -----------------
--   * users.role and user_invites.role: the CHECK is widened to accept the
--     three new names BESIDE the October ones. No row is rewritten: a row
--     with an October name reads through the map in
--     legba.shared.auth_signin (read and reader-own-rows -> reader,
--     operator -> admin). With the switch off nothing writes a new name.
--   * users.is_owner boolean NOT NULL DEFAULT false -- the owner flag. Set
--     only by the one-time bootstrap command (python -m
--     legba.data.users_bootstrap), which refuses when an owner exists. The
--     table does not force a single owner, so a second can be added by hand.
--   * user_sessions.last_seen_at timestamptz (nullable) -- the idle limit's
--     clock. NULL (every session from before this migration) counts from
--     created_at.
--
-- SAFETY
-- ------
-- Additive and idempotent: ADD COLUMN IF NOT EXISTS; each CHECK is dropped
-- IF EXISTS and re-added under the SAME name with a strictly wider set, so
-- every existing row still satisfies it (live: 0 users, 0 invites, 0
-- sessions on 2026-11-13). Routed through the runner (no inline BEGIN /
-- COMMIT).

ALTER TABLE public.users
    DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE public.users
    ADD CONSTRAINT users_role_check
    CHECK (role IN ('read', 'reader-own-rows', 'operator',
                    'reader', 'contributor', 'admin'));

ALTER TABLE public.user_invites
    DROP CONSTRAINT IF EXISTS user_invites_role_check;
ALTER TABLE public.user_invites
    ADD CONSTRAINT user_invites_role_check
    CHECK (role IN ('read', 'reader-own-rows', 'operator',
                    'reader', 'contributor', 'admin'));

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS is_owner boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.users.is_owner IS
    'The instance owner (mig 0255, A-1): an admin with this flag. Set by the '
    'one-time bootstrap command; an owner cannot be demoted or disabled '
    'through the API.';

ALTER TABLE public.user_sessions
    ADD COLUMN IF NOT EXISTS last_seen_at timestamptz;

COMMENT ON COLUMN public.user_sessions.last_seen_at IS
    'Last request on this session (mig 0255, A-1), touched at most every '
    'five minutes; the idle limit (LEGBA_SESSION_IDLE_HOURS, default 12) '
    'counts from it, or from created_at when NULL. Read only with '
    'LEGBA_AUTH_SIGNIN=1.';
