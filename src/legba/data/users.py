# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local users + PLUGGABLE authentication (migration 0236).

WHY. Without local users every viewer was the operator: caddy's basic_auth
perimeter injected the operator bearer for every browser. Identity is local
but authentication is external: the identity is OURS (one ``users`` row, an
internal uuid every other row keys on) and how a person proves who they are
is a PROVIDER behind one interface. Sign-in is INVITE-ONLY under every
provider — no row, no entry.

THE INTERFACE (:class:`AuthProvider`). ``authenticate(conn, credentials)``
returns the active :class:`User` the credentials prove, or raises
:class:`AuthRefused` with a stable ``code`` and a message that names what was
refused. Two providers:

  * ``local_password`` (:class:`LocalPasswordProvider`) — BUILT. Email +
    password; the secret is an **argon2id** hash (libsodium ``crypto_pwhash``
    through PyNaCl, already a dependency of every image — ``nacl.pwhash``;
    interactive limits, 64 MiB / 2 passes) stored in ``user_credentials``;
    never a plaintext. A missing user still pays one hash verify (a decoy), so
    timing does not say whether an email has an account.
  * ``oidc`` (:class:`OidcProvider`) — DECLARED. Maps ``provider + subject``
    onto our row (``users.auth_provider = 'oidc'``, ``external_subject``) and
    REFUSES an unknown subject by name (``oidc_subject_unknown``). The token
    exchange (an authorization code → a verified ID token → its subject) is
    NOT built: :meth:`OidcProvider.exchange_code` refuses with
    ``oidc_exchange_not_built`` naming SEAMS #73.

SESSIONS + INVITES. An opaque token (``secrets.token_urlsafe(32)``, 256 bits)
is handed out ONCE — the session cookie, the invite link — and only its sha256
is stored (``user_sessions.id_hash``, ``user_invites.token_hash``): a read of
either table cannot sign anyone in. sha256, not a password hash, because the
token is uniformly random and long; there is nothing to brute-force.

THE ACTOR. A signed-in user is ``user:<users.id>`` on every row that records
who acted (``consult_sessions.principal``, the audit log); the route gate's
subject is the bare id (:func:`user_actor`).

THE LOCKOUT (X4 GATE, W11-F76). Failed local sign-ins are counted per email
in a sliding window (:data:`SIGN_IN_FAILURE_LIMIT` per
:data:`SIGN_IN_FAILURE_WINDOW_S`). The count of record is SHARED: a
:class:`RedisFailureStore` the registry installs at startup
(``LocalPasswordProvider.use_failure_store``) keeps it in Redis, so it holds
across every registry process and survives a restart. An in-process counter
runs beside it always — the fallback when Redis is unreachable (logged), and
what a process without a store (a test) uses.

A LEAF: stdlib + PyNaCl. Callers pass an asyncpg connection (or anything with
``fetchrow`` / ``fetch`` / ``execute`` / ``transaction``) and, for the shared
lockout, an async Redis client; nothing here imports the registry, fastapi,
redis, the runtime or the analysts.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

import nacl.exceptions
import nacl.pwhash

from legba.shared import auth_signin as _signin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary (mirrors migration 0236's CHECKs and the route gate's ladder)
# ---------------------------------------------------------------------------

#: The route gate's three classes (``registry.route_roles.ROLES``; a test
#: pins the two equal — this leaf does not import the registry).
USER_ROLES: tuple[str, ...] = ("read", "reader-own-rows", "operator")
USER_STATUSES: tuple[str, ...] = ("invited", "active", "disabled")

PROVIDER_LOCAL_PASSWORD = "local_password"
PROVIDER_OIDC = "oidc"
PROVIDER_NAMES: tuple[str, ...] = (PROVIDER_LOCAL_PASSWORD, PROVIDER_OIDC)

#: The audit-actor prefix of a signed-in user.
USER_ACTOR_PREFIX = "user:"

#: Session lifetime (hours) and invite lifetime (hours); env-overridable.
SESSION_TTL_ENV = "LEGBA_SESSION_TTL_HOURS"
SESSION_TTL_HOURS_DEFAULT = 72
INVITE_TTL_ENV = "LEGBA_INVITE_TTL_HOURS"
INVITE_TTL_HOURS_DEFAULT = 168

#: Password length bounds (characters). The upper bound caps the hash's input.
PASSWORD_MIN_CHARS = 12
PASSWORD_MAX_CHARS = 1024

#: Failed local sign-ins per email inside the window before the email is
#: refused (``too_many_attempts``) until the window passes. Shared across
#: processes through :class:`RedisFailureStore` (X4 GATE).
SIGN_IN_FAILURE_LIMIT = 10
SIGN_IN_FAILURE_WINDOW_S = 15 * 60

#: The SEAMS entry that declares the OIDC token exchange as not built.
OIDC_EXCHANGE_SEAM = "SEAMS #73"


def user_actor(user_id: Any) -> str:
    """The audit actor of a signed-in user: ``user:<id>``."""
    return f"{USER_ACTOR_PREFIX}{user_id}"


def _ttl_hours(env: str, default: int) -> int:
    raw = os.getenv(env, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return value if value > 0 else default


def session_ttl() -> timedelta:
    return timedelta(hours=_ttl_hours(SESSION_TTL_ENV, SESSION_TTL_HOURS_DEFAULT))


def invite_ttl() -> timedelta:
    return timedelta(hours=_ttl_hours(INVITE_TTL_ENV, INVITE_TTL_HOURS_DEFAULT))


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class AuthRefused(Exception):
    """An authentication / account step refused, with a stable ``code``.

    ``status`` is the HTTP status the registry answers with (401 for a failed
    proof, 403 for a known-but-not-allowed account, 400/404/409 for bad
    input); the message names what was refused and never echoes a secret.
    """

    def __init__(self, code: str, message: str, *, status: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_detail(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


# ---------------------------------------------------------------------------
# Tokens + secrets
# ---------------------------------------------------------------------------


def new_token() -> str:
    """A fresh opaque token (256 bits, url-safe)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """sha256 hex of an opaque token — what the tables store."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """argon2id PHC string for ``password`` (libsodium interactive limits)."""
    check_password_policy(password)
    return nacl.pwhash.argon2id.str(password.encode("utf-8")).decode("ascii")


def verify_password(secret_hash: str, password: str) -> bool:
    """Constant-time verify of ``password`` against an argon2id PHC string."""
    try:
        return bool(nacl.pwhash.verify(
            secret_hash.encode("ascii"), password.encode("utf-8"),
        ))
    except (nacl.exceptions.InvalidkeyError, ValueError, TypeError):
        return False


def check_password_policy(password: Any) -> None:
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_CHARS:
        raise AuthRefused(
            "password_too_short",
            f"a password must be at least {PASSWORD_MIN_CHARS} characters",
            status=400,
        )
    if len(password) > PASSWORD_MAX_CHARS:
        raise AuthRefused(
            "password_too_long",
            f"a password must be at most {PASSWORD_MAX_CHARS} characters",
            status=400,
        )


_decoy_lock = threading.Lock()
_decoy_hash: str | None = None


def _timing_decoy_hash() -> str:
    """A real argon2id hash of a random secret, computed once, verified when
    the email has no account so the miss costs what a hit costs."""
    global _decoy_hash
    with _decoy_lock:
        if _decoy_hash is None:
            _decoy_hash = nacl.pwhash.argon2id.str(
                new_token().encode("utf-8"),
            ).decode("ascii")
        return _decoy_hash


def normalize_email(email: Any) -> str:
    """Case-fold + trim; refuse a value that is not an address shape."""
    if not isinstance(email, str):
        raise AuthRefused("email_invalid", "an email address is required", status=400)
    out = email.strip().lower()
    local, _, domain = out.partition("@")
    if not local or not domain or " " in out or len(out) > 320:
        raise AuthRefused(
            "email_invalid", f"{email!r} is not an email address", status=400,
        )
    return out


def assignable_roles() -> tuple[str, ...]:
    """The role names an invite / an edit may set: the October classes with
    the sign-in switch off, ``reader`` / ``contributor`` / ``admin`` with it
    on."""
    return _signin.ROLES if _signin.signin_enabled() else USER_ROLES


def _check_role(role: Any) -> str:
    if _signin.signin_enabled():
        if role not in _signin.ROLES:
            raise AuthRefused(
                "role_unknown",
                f"role {role!r} is not one of {list(_signin.ROLES)}",
                status=400,
            )
        return str(role)
    if role not in USER_ROLES:
        raise AuthRefused(
            "role_unknown", f"role {role!r} is not one of {list(USER_ROLES)}",
            status=400,
        )
    return str(role)


# ---------------------------------------------------------------------------
# The user row
# ---------------------------------------------------------------------------

_USER_COLUMN_NAMES = (
    "id", "email", "display_name", "role", "status", "auth_provider",
    "external_subject", "invited_by", "created_at", "updated_at",
    "last_sign_in_at",
)
_USER_COLUMNS = ", ".join(_USER_COLUMN_NAMES)
#: The same columns qualified by the ``u`` alias (joins).
_U_COLUMNS = ", ".join(f"u.{c}" for c in _USER_COLUMN_NAMES)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


@dataclass(frozen=True)
class User:
    id: str
    email: str | None
    display_name: str
    role: str
    status: str
    auth_provider: str
    external_subject: str | None = None
    invited_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_sign_in_at: str | None = None
    #: The owner flag (migration 0255). ``None`` = not read (every
    #: switch-off query), and then :meth:`public` leaves the key out.
    is_owner: bool | None = None

    @property
    def actor(self) -> str:
        return user_actor(self.id)

    @property
    def effective_role(self) -> str:
        """The role the gate stamps: the stored name with the switch off;
        with it on, the new name (October names read through the map)."""
        if _signin.signin_enabled():
            return _signin.normalize_role(self.role) or _signin.ROLE_READER
        return self.role

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "User":
        keys = row.keys() if hasattr(row, "keys") else ()
        is_owner = bool(row["is_owner"]) if "is_owner" in keys else None
        return cls(
            id=str(row["id"]),
            email=row["email"],
            display_name=row["display_name"] or "",
            role=row["role"],
            status=row["status"],
            auth_provider=row["auth_provider"],
            external_subject=row["external_subject"],
            invited_by=row["invited_by"],
            created_at=_iso(row["created_at"]),
            updated_at=_iso(row["updated_at"]),
            last_sign_in_at=_iso(row["last_sign_in_at"]),
            is_owner=is_owner,
        )

    def public(self) -> dict[str, Any]:
        """The shape every route returns (no secret exists on this row)."""
        out = self._public_base()
        if self.is_owner is not None:
            out["is_owner"] = self.is_owner
            out["role"] = self.effective_role
        return out

    def _public_base(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "actor": self.actor,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
            "status": self.status,
            "auth_provider": self.auth_provider,
            "external_subject": self.external_subject,
            "invited_by": self.invited_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_sign_in_at": self.last_sign_in_at,
        }


def _require_active(user: User) -> User:
    if user.status == "disabled":
        raise AuthRefused(
            "user_disabled", f"the account {user.email or user.id} is disabled",
            status=403,
        )
    if user.status != "active":
        raise AuthRefused(
            "user_not_active",
            f"the account {user.email or user.id} has not accepted its invite",
            status=403,
        )
    return user


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class AuthProvider(Protocol):
    """One way of proving who you are. Invite-only: a provider maps a proof
    onto an EXISTING ``users`` row and never creates one."""

    name: str
    built: bool

    async def authenticate(self, conn: Any, credentials: Mapping[str, Any]) -> User:
        """The active user the credentials prove, else :class:`AuthRefused`."""
        ...


class _FailureCounter:
    """Failed sign-ins per key in a sliding window (in-process)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def _live(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, ()) if now - t < SIGN_IN_FAILURE_WINDOW_S]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._live(key, time.monotonic())) >= SIGN_IN_FAILURE_LIMIT

    def record(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._hits[key] = [*self._live(key, now), now]

    def clear(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


class FailureStore(Protocol):
    """A SHARED failed-sign-in counter (the lockout of record)."""

    async def blocked(self, key: str) -> bool: ...

    async def record(self, key: str) -> None: ...

    async def clear(self, key: str) -> None: ...


class RedisFailureStore:
    """Failed sign-ins per email in Redis — one sorted set per email (a hashed
    key: no address in the keyspace), scored by wall-clock time, pruned to the
    window on every touch and expiring with it. Every registry process shares
    it and a restart does not reset it (X4 GATE, W11-F76).

    ``client`` is an async Redis client (``redis.asyncio.Redis`` — the
    registry passes ``legba.data.redis.RedisStore``'s).
    """

    KEY_PREFIX = "legba:auth:signin_failures:"

    def __init__(
        self, client: Any, *, limit: int = SIGN_IN_FAILURE_LIMIT,
        window_s: float = SIGN_IN_FAILURE_WINDOW_S,
    ) -> None:
        self._client = client
        self.limit = limit
        self.window_s = window_s

    def key(self, email: str) -> str:
        digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]
        return f"{self.KEY_PREFIX}{digest}"

    async def blocked(self, key: str) -> bool:
        k, now = self.key(key), time.time()
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(k, "-inf", now - self.window_s)
            pipe.zcard(k)
            _, count = await pipe.execute()
        return int(count) >= self.limit

    async def record(self, key: str) -> None:
        k, now = self.key(key), time.time()
        member = f"{now:.6f}:{secrets.token_hex(4)}"
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.zadd(k, {member: now})
            pipe.zremrangebyscore(k, "-inf", now - self.window_s)
            pipe.expire(k, int(self.window_s) + 1)
            await pipe.execute()

    async def clear(self, key: str) -> None:
        await self._client.delete(self.key(key))


class LocalPasswordProvider:
    """Email + password, argon2id at rest. BUILT."""

    name = PROVIDER_LOCAL_PASSWORD
    built = True

    def __init__(self) -> None:
        #: In-process — always counted; the fallback when the store fails.
        self.failures = _FailureCounter()
        #: The SHARED counter of record, when installed (X4 GATE).
        self.failure_store: FailureStore | None = None

    def use_failure_store(self, store: FailureStore | None) -> None:
        """Install (or, with ``None``, remove) the shared lockout store."""
        self.failure_store = store

    async def _store_call(self, op: str, email: str) -> Any:
        store = self.failure_store
        if store is None:
            return None
        try:
            return await getattr(store, op)(email)
        except Exception as exc:  # noqa: BLE001 — degrade to in-process, loudly
            logger.warning(
                "auth.lockout.store_unavailable op=%s err=%s — the sign-in "
                "lockout is PER PROCESS until the shared store answers", op, exc,
            )
            return None

    async def _blocked(self, email: str) -> bool:
        if self.failures.blocked(email):
            return True
        return bool(await self._store_call("blocked", email))

    async def _record_failure(self, email: str) -> None:
        self.failures.record(email)
        await self._store_call("record", email)

    async def _clear_failures(self, email: str) -> None:
        self.failures.clear(email)
        await self._store_call("clear", email)

    async def authenticate(self, conn: Any, credentials: Mapping[str, Any]) -> User:
        email = normalize_email(credentials.get("email"))
        password = credentials.get("password")
        if not isinstance(password, str) or not password:
            raise AuthRefused("invalid_credentials", "email or password is wrong")
        if await self._blocked(email):
            raise AuthRefused(
                "too_many_attempts",
                f"too many failed sign-ins for this email; wait "
                f"{SIGN_IN_FAILURE_WINDOW_S // 60} minutes",
                status=429,
            )
        row = await conn.fetchrow(
            f"""
            SELECT {_U_COLUMNS}, c.secret_hash
              FROM users u
              LEFT JOIN user_credentials c
                ON c.user_id = u.id AND c.provider = $2
             WHERE u.email = $1 AND u.auth_provider = $2
            """,
            email, PROVIDER_LOCAL_PASSWORD,
        )
        secret_hash = row["secret_hash"] if row is not None else None
        ok = await asyncio.to_thread(
            verify_password, secret_hash or _timing_decoy_hash(), password,
        )
        if row is None or secret_hash is None or not ok:
            await self._record_failure(email)
            raise AuthRefused("invalid_credentials", "email or password is wrong")
        await self._clear_failures(email)
        return _require_active(User.from_row(row))

    async def set_secret(self, conn: Any, user_id: str, password: str) -> None:
        secret_hash = await asyncio.to_thread(hash_password, password)
        await conn.execute(
            """
            INSERT INTO user_credentials (user_id, provider, secret_hash)
            VALUES ($1::uuid, $2, $3)
            ON CONFLICT (user_id, provider)
            DO UPDATE SET secret_hash = EXCLUDED.secret_hash, updated_at = now()
            """,
            user_id, PROVIDER_LOCAL_PASSWORD, secret_hash,
        )


class OidcProvider:
    """OpenID Connect — DECLARED. The subject map is built; the exchange is
    not (SEAMS #73)."""

    name = PROVIDER_OIDC
    built = False

    async def authenticate(self, conn: Any, credentials: Mapping[str, Any]) -> User:
        """Map a VERIFIED subject onto our row. ``credentials["subject"]`` must
        come from a verified ID token — which only :meth:`exchange_code` could
        produce, and it is not built; this is the map half, tested on its own."""
        subject = credentials.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthRefused("oidc_subject_missing", "no OIDC subject presented")
        subject = subject.strip()
        row = await conn.fetchrow(
            f"SELECT {_USER_COLUMNS} FROM users "
            "WHERE auth_provider = $1 AND external_subject = $2",
            PROVIDER_OIDC, subject,
        )
        if row is None:
            raise AuthRefused(
                "oidc_subject_unknown",
                f"OIDC subject {subject!r} is not an invited user — sign-in is "
                "invite-only; the operator must bind this subject to a user first",
                status=403,
            )
        return _require_active(User.from_row(row))

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        """Authorization code → verified ID token → subject. NOT BUILT."""
        raise AuthRefused(
            "oidc_exchange_not_built",
            "the OIDC token exchange is not built (" + OIDC_EXCHANGE_SEAM + "); "
            "sign in with a local password",
            status=501,
        )


PROVIDERS: dict[str, AuthProvider] = {
    PROVIDER_LOCAL_PASSWORD: LocalPasswordProvider(),
    PROVIDER_OIDC: OidcProvider(),
}


def get_provider(name: str) -> AuthProvider:
    provider = PROVIDERS.get(name)
    if provider is None:
        raise AuthRefused(
            "provider_unknown",
            f"auth provider {name!r} is not one of {list(PROVIDERS)}",
            status=400,
        )
    return provider


def local_password_provider() -> LocalPasswordProvider:
    provider = PROVIDERS[PROVIDER_LOCAL_PASSWORD]
    assert isinstance(provider, LocalPasswordProvider)
    return provider


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


async def get_user(conn: Any, user_id: Any) -> User | None:
    uid = _as_uuid(user_id)
    if uid is None:
        return None
    row = await conn.fetchrow(f"SELECT {_USER_COLUMNS} FROM users WHERE id = $1", uid)
    return User.from_row(row) if row is not None else None


async def list_users(conn: Any) -> list[User]:
    rows = await conn.fetch(f"SELECT {_USER_COLUMNS} FROM users ORDER BY created_at, id")
    return [User.from_row(r) for r in rows]


async def update_user(
    conn: Any,
    user_id: str,
    *,
    role: str | None = None,
    status: str | None = None,
    display_name: str | None = None,
    auth_provider: str | None = None,
    external_subject: str | None = None,
) -> User:
    """The operator's edit. Disabling revokes every live session at once;
    activating requires a way to sign in (a local secret, or an OIDC
    subject). ``invited`` is not settable — it is what an invite creates."""
    async with conn.transaction():
        current = await get_user(conn, user_id)
        if current is None:
            raise AuthRefused("user_unknown", f"no user {user_id!r}", status=404)
        if _signin.signin_enabled():
            await _refuse_owner_change(conn, current.id, role=role, status=status)
            await _refuse_last_admin(conn, current, role=role, status=status)
        new_role = _check_role(role) if role is not None else current.role
        if status is not None and status not in ("active", "disabled"):
            raise AuthRefused(
                "status_invalid",
                f"status {status!r} cannot be set; one of ['active', 'disabled']",
                status=400,
            )
        new_status = status or current.status
        provider = auth_provider if auth_provider is not None else current.auth_provider
        if provider not in PROVIDER_NAMES:
            raise AuthRefused(
                "provider_unknown", f"auth provider {provider!r} is not one of "
                f"{list(PROVIDER_NAMES)}", status=400,
            )
        subject = (
            external_subject.strip() or None
            if isinstance(external_subject, str) else current.external_subject
        )
        if new_status == "active" and current.status != "active":
            if provider == PROVIDER_OIDC:
                can_sign_in = subject is not None
            else:
                can_sign_in = await conn.fetchval(
                    "SELECT 1 FROM user_credentials WHERE user_id = $1::uuid "
                    "AND provider = $2", current.id, PROVIDER_LOCAL_PASSWORD,
                ) is not None
            if not can_sign_in:
                raise AuthRefused(
                    "no_credential",
                    f"user {current.id} has no way to sign in yet "
                    f"({provider}); it stays {current.status}",
                    status=409,
                )
        row = await conn.fetchrow(
            f"""
            UPDATE users
               SET role = $2, status = $3, display_name = $4,
                   auth_provider = $5, external_subject = $6, updated_at = now()
             WHERE id = $1::uuid
         RETURNING {_USER_COLUMNS}
            """,
            current.id, new_role, new_status,
            display_name if display_name is not None else current.display_name,
            provider, subject,
        )
        if new_status == "disabled":
            await revoke_user_sessions(conn, current.id)
    return User.from_row(row)


async def _refuse_owner_change(
    conn: Any, user_id: str, *, role: str | None, status: str | None,
) -> None:
    """The owner stays an active admin through the API (409
    ``owner_protected``); changing the owner is a host-side act."""
    is_owner = await conn.fetchval(
        "SELECT is_owner FROM users WHERE id = $1::uuid", user_id,
    )
    if not is_owner:
        return
    if (role is not None and role != _signin.ROLE_ADMIN) or status == "disabled":
        raise AuthRefused(
            "owner_protected",
            "the owner account cannot be demoted or disabled through the API",
            status=409,
        )


#: Stored role names the sign-in gate reads as admin (the October
#: ``operator`` reads through the legacy map).
ADMIN_ROLE_NAMES: tuple[str, ...] = tuple(
    sorted({_signin.ROLE_ADMIN, *(
        old for old, new in _signin.LEGACY_ROLE_MAP.items() if new == _signin.ROLE_ADMIN
    )})
)


async def _refuse_last_admin(
    conn: Any, current: User, *, role: str | None, status: str | None,
) -> None:
    """Refuse (409 ``last_admin``) a change that would leave no active admin:
    demoting or disabling the only one. Every active admin row is locked
    first (in id order), so two admins demoting each other at once cannot
    both succeed: the second waits, then sees the first change."""
    if current.status != "active" or current.role not in ADMIN_ROLE_NAMES:
        return
    keeps_admin = role is None or role in ADMIN_ROLE_NAMES
    stays_active = status is None or status == "active"
    if keeps_admin and stays_active:
        return
    admins = await conn.fetch(
        "SELECT id FROM users WHERE status = 'active' AND role = ANY($1::text[]) "
        "ORDER BY id FOR UPDATE",
        list(ADMIN_ROLE_NAMES),
    )
    others = [r for r in admins if str(r["id"]) != str(current.id)]
    if not others:
        raise AuthRefused(
            "last_admin",
            "this is the only active admin; make someone else an admin first",
            status=409,
        )


# ---------------------------------------------------------------------------
# Account administration (the console's Users page; sign-in on)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountRow:
    """One account as the Users page lists it: the user (with the owner
    flag), when it was last seen on any session, and its live sessions."""

    user: User
    last_seen_at: str | None
    live_sessions: int

    def public(self) -> dict[str, Any]:
        out = self.user.public()
        out["last_seen_at"] = self.last_seen_at
        out["live_sessions"] = self.live_sessions
        return out


@dataclass(frozen=True)
class PendingInvite:
    """An account still waiting on its invite, with its newest unused invite.
    No token and no token hash: neither is ever read back out."""

    user_id: str
    email: str | None
    display_name: str
    role: str
    invited_by: str | None
    created_at: str | None
    expires_at: str | None
    expired: bool

    def public(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "display_name": self.display_name,
            "role": _signin.normalize_role(self.role) or self.role,
            "invited_by": self.invited_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "expired": self.expired,
        }


async def list_accounts(conn: Any) -> list[AccountRow]:
    """Every account past its invite (active or disabled), oldest first."""
    rows = await conn.fetch(
        f"""
        SELECT {_U_COLUMNS}, u.is_owner, s.last_seen_at, s.live_sessions
          FROM users u
          LEFT JOIN LATERAL (
                SELECT max(coalesce(last_seen_at, created_at)) AS last_seen_at,
                       count(*) FILTER (
                           WHERE revoked_at IS NULL AND expires_at > now()
                             AND coalesce(last_seen_at, created_at)
                                 > now() - make_interval(hours => $1)
                       ) AS live_sessions
                  FROM user_sessions WHERE user_id = u.id
          ) s ON true
         WHERE u.status <> 'invited'
         ORDER BY u.created_at, u.id
        """,
        _signin.idle_hours(),
    )
    return [
        AccountRow(
            user=User.from_row(r),
            last_seen_at=_iso(r["last_seen_at"]),
            live_sessions=int(r["live_sessions"] or 0),
        )
        for r in rows
    ]


async def list_pending_invites(conn: Any) -> list[PendingInvite]:
    """Every account still waiting on its invite, newest first."""
    rows = await conn.fetch(
        """
        SELECT u.id, u.email, u.display_name, u.role AS user_role,
               i.role, i.invited_by, i.created_at, i.expires_at
          FROM users u
          LEFT JOIN LATERAL (
                SELECT role, invited_by, created_at, expires_at
                  FROM user_invites
                 WHERE user_id = u.id AND used_at IS NULL
                 ORDER BY created_at DESC LIMIT 1
          ) i ON true
         WHERE u.status = 'invited'
         ORDER BY coalesce(i.created_at, u.created_at) DESC, u.id
        """,
    )
    now = datetime.now(tz=timezone.utc)
    out: list[PendingInvite] = []
    for r in rows:
        expires = r["expires_at"]
        out.append(PendingInvite(
            user_id=str(r["id"]),
            email=r["email"],
            display_name=r["display_name"] or "",
            role=r["role"] or r["user_role"],
            invited_by=r["invited_by"],
            created_at=_iso(r["created_at"]),
            expires_at=_iso(expires),
            expired=expires is None or expires <= now,
        ))
    return out


async def revoke_invite(conn: Any, user_id: Any) -> User:
    """Withdraw a pending invite: the waiting account and its invites are
    removed (it never signed in, so nothing else names it). The same email
    can be invited again. Refuses an account that is past its invite."""
    uid = _as_uuid(user_id)
    if uid is None:
        raise AuthRefused("invite_unknown", "no pending invite for that account", status=404)
    async with conn.transaction():
        row = await conn.fetchrow(
            f"SELECT {_USER_COLUMNS} FROM users WHERE id = $1 FOR UPDATE", uid,
        )
        if row is None or row["status"] != "invited":
            raise AuthRefused(
                "invite_unknown", "no pending invite for that account", status=404,
            )
        await conn.execute("DELETE FROM users WHERE id = $1 AND status = 'invited'", uid)
    return User.from_row(row)


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invite:
    token: str  # the plaintext — returned ONCE, never stored
    user: User
    role: str
    expires_at: str


async def create_invite(
    conn: Any,
    *,
    email: str,
    role: str,
    invited_by: str | None,
    display_name: str = "",
) -> Invite:
    """Invite ``email`` as ``role``: an ``invited`` user row (reused when the
    email was invited before) and a fresh single-use token. Earlier unused
    invites of that user stop working. An active or disabled account is not
    re-invited (409)."""
    email = normalize_email(email)
    role = _check_role(role)
    token = new_token()
    expires = datetime.now(tz=timezone.utc) + invite_ttl()
    async with conn.transaction():
        existing = await conn.fetchrow(
            f"SELECT {_USER_COLUMNS} FROM users WHERE email = $1 FOR UPDATE", email,
        )
        if existing is not None and existing["status"] != "invited":
            raise AuthRefused(
                "user_exists",
                f"{email} already has an account ({existing['status']}); "
                "edit it instead of inviting",
                status=409,
            )
        if existing is None:
            row = await conn.fetchrow(
                f"""
                INSERT INTO users (email, display_name, role, status,
                                   auth_provider, invited_by)
                VALUES ($1, $2, $3, 'invited', $4, $5)
             RETURNING {_USER_COLUMNS}
                """,
                email, display_name or "", role, PROVIDER_LOCAL_PASSWORD, invited_by,
            )
        else:
            row = await conn.fetchrow(
                f"""
                UPDATE users SET role = $2, display_name = COALESCE(NULLIF($3, ''),
                       display_name), invited_by = $4, updated_at = now()
                 WHERE id = $1 RETURNING {_USER_COLUMNS}
                """,
                existing["id"], role, display_name or "", invited_by,
            )
            await conn.execute(
                "UPDATE user_invites SET expires_at = least(expires_at, now()) "
                "WHERE user_id = $1 AND used_at IS NULL", existing["id"],
            )
        await conn.execute(
            """
            INSERT INTO user_invites (token_hash, user_id, email, role,
                                      invited_by, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            hash_token(token), row["id"], email, role, invited_by, expires,
        )
    return Invite(token=token, user=User.from_row(row), role=role,
                  expires_at=expires.isoformat())


async def accept_invite(
    conn: Any, *, token: str, password: str, display_name: str | None = None,
) -> User:
    """Spend an invite: set the local password, activate the user with the
    invite's role. Refuses an unknown, used or expired token by name."""
    check_password_policy(password)
    if not isinstance(token, str) or not token:
        raise AuthRefused("invite_invalid", "this invite link is not valid")
    secret_hash = await asyncio.to_thread(hash_password, password)
    async with conn.transaction():
        invite = await conn.fetchrow(
            """
            SELECT token_hash, user_id, role, expires_at, used_at
              FROM user_invites WHERE token_hash = $1 FOR UPDATE
            """,
            hash_token(token),
        )
        if invite is None:
            raise AuthRefused("invite_invalid", "this invite link is not valid")
        if invite["used_at"] is not None:
            raise AuthRefused("invite_used", "this invite link was already used")
        if invite["expires_at"] <= datetime.now(tz=timezone.utc):
            raise AuthRefused(
                "invite_expired", "this invite link has expired; ask for a new one",
            )
        user = await get_user(conn, str(invite["user_id"]))
        if user is None or user.status != "invited":
            raise AuthRefused(
                "invite_invalid",
                "this invite's account is no longer waiting for an invite",
                status=409,
            )
        await conn.execute(
            """
            INSERT INTO user_credentials (user_id, provider, secret_hash)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, provider)
            DO UPDATE SET secret_hash = EXCLUDED.secret_hash, updated_at = now()
            """,
            invite["user_id"], PROVIDER_LOCAL_PASSWORD, secret_hash,
        )
        row = await conn.fetchrow(
            f"""
            UPDATE users
               SET status = 'active', role = $2, auth_provider = $3,
                   display_name = COALESCE(NULLIF($4, ''), display_name),
                   updated_at = now()
             WHERE id = $1 RETURNING {_USER_COLUMNS}
            """,
            invite["user_id"], invite["role"], PROVIDER_LOCAL_PASSWORD,
            (display_name or "").strip(),
        )
        await conn.execute(
            "UPDATE user_invites SET used_at = now() WHERE token_hash = $1",
            invite["token_hash"],
        )
    return User.from_row(row)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    token: str  # the cookie value — returned ONCE, never stored
    user: User
    expires_at: datetime


async def create_session(conn: Any, user: User) -> Session:
    """A new session for an ACTIVE user; stamps ``last_sign_in_at``."""
    _require_active(user)
    token = new_token()
    expires = datetime.now(tz=timezone.utc) + session_ttl()
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO user_sessions (id_hash, user_id, expires_at) "
            "VALUES ($1, $2::uuid, $3)",
            hash_token(token), user.id, expires,
        )
        row = await conn.fetchrow(
            f"UPDATE users SET last_sign_in_at = now() WHERE id = $1::uuid "
            f"RETURNING {_USER_COLUMNS}",
            user.id,
        )
    return Session(token=token, user=User.from_row(row), expires_at=expires)


#: The per-request read. Served by ``user_sessions_pkey`` + ``users_pkey``.
RESOLVE_SESSION_SQL = f"""
SELECT {_U_COLUMNS}
  FROM user_sessions s
  JOIN users u ON u.id = s.user_id
 WHERE s.id_hash = $1
   AND s.revoked_at IS NULL
   AND s.expires_at > now()
   AND u.status = 'active'
"""


#: Sign-in switch on: the same read plus the owner flag and the IDLE
#: limit — ``$2`` hours since ``last_seen_at`` (or ``created_at`` when never
#: touched). Also returns whether ``last_seen_at`` is due a touch (``$3``
#: seconds old), so a busy session costs one write per window.
RESOLVE_SESSION_SIGNIN_SQL = f"""
SELECT {_U_COLUMNS}, u.is_owner,
       (s.last_seen_at IS NULL
        OR s.last_seen_at < now() - make_interval(secs => $3)) AS touch_due
  FROM user_sessions s
  JOIN users u ON u.id = s.user_id
 WHERE s.id_hash = $1
   AND s.revoked_at IS NULL
   AND s.expires_at > now()
   AND coalesce(s.last_seen_at, s.created_at) > now() - make_interval(hours => $2)
   AND u.status = 'active'
"""


async def resolve_session(conn: Any, token: str | None) -> User | None:
    """The active user a session token belongs to, or ``None`` (unknown,
    revoked, expired, or the user is no longer active).

    Sign-in switch on: also ``None`` past the idle limit
    (``LEGBA_SESSION_IDLE_HOURS``); a live session's ``last_seen_at`` is
    touched at most every five minutes, and the user carries ``is_owner``."""
    if not token or len(token) > 256:
        return None
    if _signin.signin_enabled():
        return await _resolve_session_signin(conn, token)
    row = await conn.fetchrow(RESOLVE_SESSION_SQL, hash_token(token))
    return User.from_row(row) if row is not None else None


async def _resolve_session_signin(conn: Any, token: str) -> User | None:
    id_hash = hash_token(token)
    row = await conn.fetchrow(
        RESOLVE_SESSION_SIGNIN_SQL, id_hash, _signin.idle_hours(),
        float(_signin.LAST_SEEN_TOUCH_SECONDS),
    )
    if row is None:
        return None
    if row["touch_due"]:
        try:
            await conn.execute(
                "UPDATE user_sessions SET last_seen_at = now() WHERE id_hash = $1",
                id_hash,
            )
        except Exception as exc:  # noqa: BLE001 — the read stands; the clock lags
            logger.warning("auth.session.touch_failed err=%s", exc)
    return User.from_row(row)


async def get_user_with_owner(conn: Any, user_id: Any) -> User | None:
    """:func:`get_user` plus ``is_owner`` (needs migration 0255)."""
    uid = _as_uuid(user_id)
    if uid is None:
        return None
    row = await conn.fetchrow(
        f"SELECT {_USER_COLUMNS}, is_owner FROM users WHERE id = $1", uid,
    )
    return User.from_row(row) if row is not None else None


async def owner_count(conn: Any) -> int:
    """Active owners."""
    return int(await conn.fetchval(
        "SELECT count(*) FROM users WHERE is_owner AND status = 'active'",
    ) or 0)


async def create_owner(
    conn: Any, *, email: str, password: str, display_name: str = "",
) -> User:
    """The FIRST OWNER (bootstrap): an active ``admin`` with
    ``is_owner``, its argon2id password set, created in one transaction.
    Refuses (409 ``owner_exists``) when an active owner exists, and
    (409 ``user_exists``) when the email already has an account. The
    password is never logged or returned."""
    email = normalize_email(email)
    secret_hash = await asyncio.to_thread(hash_password, password)
    async with conn.transaction():
        await conn.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
        if await owner_count(conn) > 0:
            raise AuthRefused(
                "owner_exists",
                "an owner account already exists; the bootstrap runs once. "
                "Invite further people from the console",
                status=409,
            )
        existing = await conn.fetchval("SELECT 1 FROM users WHERE email = $1", email)
        if existing is not None:
            raise AuthRefused(
                "user_exists", f"{email} already has an account", status=409,
            )
        row = await conn.fetchrow(
            f"""
            INSERT INTO users (email, display_name, role, status, auth_provider,
                               invited_by, is_owner)
            VALUES ($1, $2, $3, 'active', $4, $5, true)
         RETURNING {_USER_COLUMNS}, is_owner
            """,
            email, display_name or "", _signin.ROLE_ADMIN,
            PROVIDER_LOCAL_PASSWORD, "system:bootstrap",
        )
        await conn.execute(
            """
            INSERT INTO user_credentials (user_id, provider, secret_hash)
            VALUES ($1, $2, $3)
            """,
            row["id"], PROVIDER_LOCAL_PASSWORD, secret_hash,
        )
    return User.from_row(row)


async def revoke_session(conn: Any, token: str | None) -> bool:
    if not token:
        return False
    status = await conn.execute(
        "UPDATE user_sessions SET revoked_at = now() "
        "WHERE id_hash = $1 AND revoked_at IS NULL",
        hash_token(token),
    )
    return str(status).endswith(" 1")


async def revoke_user_sessions(conn: Any, user_id: str) -> int:
    status = await conn.execute(
        "UPDATE user_sessions SET revoked_at = now() "
        "WHERE user_id = $1::uuid AND revoked_at IS NULL",
        str(user_id),
    )
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


__all__ = [
    "ADMIN_ROLE_NAMES",
    "AccountRow",
    "PendingInvite",
    "list_accounts",
    "list_pending_invites",
    "revoke_invite",
    "AuthProvider",
    "AuthRefused",
    "INVITE_TTL_ENV",
    "Invite",
    "LocalPasswordProvider",
    "OIDC_EXCHANGE_SEAM",
    "OidcProvider",
    "PASSWORD_MIN_CHARS",
    "PROVIDERS",
    "PROVIDER_LOCAL_PASSWORD",
    "PROVIDER_OIDC",
    "RESOLVE_SESSION_SQL",
    "SESSION_TTL_ENV",
    "Session",
    "USER_ACTOR_PREFIX",
    "USER_ROLES",
    "USER_STATUSES",
    "User",
    "RESOLVE_SESSION_SIGNIN_SQL",
    "accept_invite",
    "assignable_roles",
    "create_invite",
    "create_owner",
    "create_session",
    "get_provider",
    "get_user",
    "get_user_with_owner",
    "owner_count",
    "hash_password",
    "hash_token",
    "list_users",
    "local_password_provider",
    "new_token",
    "normalize_email",
    "resolve_session",
    "revoke_session",
    "revoke_user_sessions",
    "update_user",
    "user_actor",
    "verify_password",
]
