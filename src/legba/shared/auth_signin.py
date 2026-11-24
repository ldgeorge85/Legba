# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SIGN-IN switch and its vocabulary (A-1, users / groups / roles step 1).

ONE environment switch, ``LEGBA_AUTH_SIGNIN``, turns on the first step of the
accounts design (planning note B2, section 8, step 1):

  * real sign-in — local accounts, argon2id passwords, the HttpOnly cookie
    session with an idle limit and a same-origin check on cookie writes;
  * three roles — ``reader`` < ``contributor`` < ``admin``; the owner is an
    admin with ``users.is_owner`` set — gated through the route-role table
    (``registry.route_policy.ROUTE_MIN_ROLES``, every mounted route);
  * a token per service — ``LEGBA_SERVICE_TOKEN_<NAME>`` for the registry,
    the runtime, the workflow worker and the console; the shared admin token
    becomes a logged break-glass credential;
  * the edge stops handing a browser the admin token (``docker/Caddyfile``).

OFF (unset, or anything but ``1``) is the default, and every code path that
reads this module keeps its behaviour from before the switch existed: the
callers branch on :func:`signin_enabled` and the off branch is the old code,
untouched.

A LEAF: stdlib only. The registry gate, the runtime's registry client, the
workflow worker and the bootstrap command all import the names below, so each
env key and role name has exactly one spelling.
"""

from __future__ import annotations

import os
from typing import Mapping

#: The switch. ``1`` = on; anything else (or unset) = off, today's behaviour.
SIGNIN_ENV = "LEGBA_AUTH_SIGNIN"

# ---------------------------------------------------------------------------
# Roles (switch on)
# ---------------------------------------------------------------------------

ROLE_READER = "reader"
ROLE_CONTRIBUTOR = "contributor"
ROLE_ADMIN = "admin"
#: Ascending; index = rank. The owner is an admin with ``users.is_owner``.
ROLES: tuple[str, ...] = (ROLE_READER, ROLE_CONTRIBUTOR, ROLE_ADMIN)
ROLE_RANK: dict[str, int] = {r: i for i, r in enumerate(ROLES)}

#: The October role classes (migration 0236) and the role each reads as once
#: the switch is on — the design's mapping (B2 section 8, step 1.1). Stored
#: rows are never rewritten; they are read through this map.
LEGACY_ROLE_MAP: dict[str, str] = {
    "read": ROLE_READER,
    "reader-own-rows": ROLE_READER,
    "operator": ROLE_ADMIN,
}


def normalize_role(role: object) -> str | None:
    """``role`` in the new vocabulary, or ``None`` when it is neither a new
    nor a legacy role name."""
    if not isinstance(role, str):
        return None
    if role in ROLE_RANK:
        return role
    return LEGACY_ROLE_MAP.get(role)


def role_satisfies(role: str, required: str) -> bool:
    """Whether ``role`` (new or legacy name) meets ``required`` (new name)."""
    have = normalize_role(role)
    return have is not None and ROLE_RANK[have] >= ROLE_RANK[required]


# ---------------------------------------------------------------------------
# Service credentials (switch on)
# ---------------------------------------------------------------------------

#: The services that hold their own registry credential. ``console`` is the
#: console's server side; with the switch on the edge proxy forwards the
#: browser's session and presents no credential of its own, so the name is
#: reserved for a console-side caller and unused by the stock deployment.
SERVICE_NAMES: tuple[str, ...] = ("registry", "runtime", "worker", "console")

#: ``check_service_principal(component)`` names → the service a process is.
COMPONENT_SERVICE: dict[str, str] = {
    "registry": "registry",
    "runtime": "runtime",
    "dapr_workflow_worker": "worker",
    "worker": "worker",
    "console": "console",
}

#: Optional explicit override of which service this process is.
SERVICE_NAME_ENV = "LEGBA_SERVICE_NAME"

#: Actor prefix of a named service (``service:runtime``).
SERVICE_ACTOR_PREFIX = "service:"
#: Principal kind + actor prefix of the shared admin token once the switch is
#: on: a break-glass credential, logged on use.
ADMIN_TOKEN_KIND = "admin-token"


def service_token_env(name: str) -> str:
    """``LEGBA_SERVICE_TOKEN_<NAME>`` — the env/vault key of one service."""
    return f"LEGBA_SERVICE_TOKEN_{name.upper()}"


SERVICE_TOKEN_ENVS: dict[str, str] = {n: service_token_env(n) for n in SERVICE_NAMES}

# ---------------------------------------------------------------------------
# Sessions (switch on)
# ---------------------------------------------------------------------------

#: Idle limit in hours beside the absolute lifetime (B2 section 5, gap 2).
IDLE_HOURS_ENV = "LEGBA_SESSION_IDLE_HOURS"
IDLE_HOURS_DEFAULT = 12
#: How stale ``user_sessions.last_seen_at`` may get before a request touches
#: it — one write per session per window, not one per request.
LAST_SEEN_TOUCH_SECONDS = 300


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def signin_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the switch is on. Read on every call (tests flip it)."""
    return (_env(environ).get(SIGNIN_ENV) or "").strip() == "1"


def idle_hours(environ: Mapping[str, str] | None = None) -> int:
    raw = (_env(environ).get(IDLE_HOURS_ENV) or "").strip()
    try:
        value = int(raw) if raw else IDLE_HOURS_DEFAULT
    except ValueError:
        value = IDLE_HOURS_DEFAULT
    return value if value > 0 else IDLE_HOURS_DEFAULT


def configured_service_tokens(
    environ: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    """``[(service name, token), ...]`` for every service credential set."""
    env = _env(environ)
    out: list[tuple[str, str]] = []
    for name, key in SERVICE_TOKEN_ENVS.items():
        tok = (env.get(key) or "").strip()
        if tok:
            out.append((name, tok))
    return out


__all__ = [
    "ADMIN_TOKEN_KIND",
    "COMPONENT_SERVICE",
    "IDLE_HOURS_DEFAULT",
    "IDLE_HOURS_ENV",
    "LAST_SEEN_TOUCH_SECONDS",
    "LEGACY_ROLE_MAP",
    "ROLES",
    "ROLE_ADMIN",
    "ROLE_CONTRIBUTOR",
    "ROLE_RANK",
    "ROLE_READER",
    "SERVICE_ACTOR_PREFIX",
    "SERVICE_NAMES",
    "SERVICE_NAME_ENV",
    "SERVICE_TOKEN_ENVS",
    "SIGNIN_ENV",
    "configured_service_tokens",
    "idle_hours",
    "normalize_role",
    "role_satisfies",
    "service_token_env",
    "signin_enabled",
]
