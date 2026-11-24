-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0259_user_seen_cursors.sql
--
-- S-1 (the phone's "what needs me", 2026-11-14): a per-user SEEN CURSOR.
--
-- WHY
-- ---
-- GET /api/v1/v3/since is stateless: the client keeps its cursor. A person
-- who reads on two devices (the phone shell and the desktop) needs the cursor
-- to follow them, so "since you last looked" means since THEY last looked,
-- not since this browser did. GET/PUT /api/v1/v3/me/seen read and move it.
--
-- THE TABLE
-- ---------
--   user_seen_cursors — one row per (user, surface): when that user last
--   looked at that surface. The surface is one of a CLOSED list (the phone
--   shell's five destinations; the CHECK below is the bound, so a user holds
--   at most five rows). No other text is stored: the owner is the users row
--   (deleted with it), and the only value is a timestamp.
--
-- It is written only with the sign-in switch on (LEGBA_AUTH_SIGNIN=1) and
-- only for a signed-in person's own rows; with the switch off the route
-- answers 404 and the client keeps its cursor on the device.
--
-- SAFETY
-- ------
-- Additive and idempotent: CREATE TABLE IF NOT EXISTS, no existing table or
-- row changes. Routed through the runner (no inline BEGIN / COMMIT). Adding
-- a surface later is a new migration that drops and re-adds the CHECK under
-- the same name with a strictly wider list (the 0255 pattern).

CREATE TABLE IF NOT EXISTS public.user_seen_cursors (
    user_id     uuid        NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    surface     text        NOT NULL,
    seen_at     timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, surface),
    CONSTRAINT user_seen_cursors_surface_vocab CHECK (
        surface IN ('changed', 'watch', 'ledger', 'consult', 'subjects')
    )
);

COMMENT ON TABLE public.user_seen_cursors IS
    'Per-user seen cursor (mig 0259, S-1): when each signed-in person last '
    'looked at each phone-shell surface. Read and moved only through '
    'GET/PUT /api/v1/v3/me/seen, own rows only, sign-in switch on.';
