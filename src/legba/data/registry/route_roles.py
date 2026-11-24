# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry ROUTE-ROLE gate — three role classes, one static table.

X2-B2 (W11-F16 half, W6-F11). Before this module every registry route was
"admin or nothing": a bearer either matched the one token and could do
anything, or it was refused. A dozen same-access users need three classes:

  * ``operator``         — everything (descriptor/stack/vault/vocabulary
                           writes, judging inputs, curation, substrate CRUD).
  * ``reader-own-rows``  — everything ``read`` can, plus the writes that only
                           ever touch the caller's OWN rows: consult
                           invocations, the watchlist, read receipts, own
                           exports.
  * ``read``             — every GET the display policy allows (the display
                           policy itself is lane B4's; this gate only decides
                           WHICH ROUTES a class may call).

WHAT LIVES HERE

  * ``ROLES`` / ``ROLE_RANK`` — the ladder (``read`` < ``reader-own-rows`` <
    ``operator``). A principal satisfies a requirement when its rank is >= the
    required rank.
  * ``ROUTE_ROLES`` — the STATIC table, ``(METHOD, mounted path template) →
    role``. Every POST/PUT/PATCH/DELETE the registry app mounts is listed here
    explicitly (a test enumerates the app and asserts none is missing), plus
    the GETs that are NOT ``read`` (the vault). ``docs/API_ROLES.md`` is
    rendered from this table (``scripts/gen_api_roles_doc.py``; drift-tested).
  * ``required_role_for(method, path)`` — table lookup by template match; an
    unlisted GET/HEAD/OPTIONS is ``read``; an unlisted write FAILS CLOSED to
    ``operator``.
  * ``resolve_principal`` / ``current_principal`` — who is calling and in which
    class (below).
  * ``require_role(role)`` — a per-route dependency factory.
  * ``enforce_route_role`` — the APP-LEVEL dependency ``server.create_app``
    installs once; it gates every mounted route through ``ROUTE_ROLES`` so no
    route file has to repeat its own role (and so ``api.py`` / ``consult_api.py``,
    which other lanes own, are gated without being edited).
  * ``require_row_owner`` — the own-rows check a ``reader-own-rows`` route
    applies to a row it is about to change.

WHERE THE PRINCIPAL COMES FROM (in order)

  1. A TEST override — ``X-Legba-Role`` (+ optional ``X-Legba-Subject``) —
     honoured ONLY when ``LEGBA_ROLE_OVERRIDE_FOR_TESTS=1`` AND pytest is the
     running program. Set in production it is IGNORED and logged as an error
     (never a silent privilege path).
  2. The request state a MIDDLEWARE stamped before any dependency ran:
     ``state.legba_role`` + ``state.legba_subject`` — the bearer stamp
     (X4 GATE, ``bearer_auth.BearerPrincipalMiddleware``: ``operator`` for
     both bearer principals and for dev mode) or the session stamp (X3-B3,
     ``session_auth.SessionCookieMiddleware``: the user's own class) — else
     B1's principal kind (``state.principal``, ``service`` | ``operator``),
     the ``operator`` class.
  3. Otherwise UNRESOLVED (X4 GATE, W11-F75). Before X4 an unstamped caller
     defaulted to ``operator`` — and because B1's bearer stamp landed inside
     ``require_bearer`` (a ROUTE dependency, which runs AFTER this app-level
     gate), every bearer caller reached the gate unstamped: a route that
     forgot ``Depends(require_bearer)`` was open at the gate as operator.
     Now :func:`enforce_route_role` REFUSES an unresolved caller on every
     route not in ``PUBLIC_ROUTES`` — with the bearer middleware's own
     refusal when it recorded one (401 no credential / 403 bad credential /
     503 misconfigured: the wire answers ``require_bearer`` always gave),
     else ``403 role_unresolved``. An unresolved :class:`Principal` is the
     lowest class (``read``) so a caller that bypasses the gate is confined,
     never widened.

REGISTRY-SLIM: fastapi/starlette + the stdlib only. Never imports ``api``,
any ``*_api`` router, ``legba.runtime`` or ``legba.data.analysts``.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

from fastapi import HTTPException, WebSocketException, status
from starlette.requests import HTTPConnection

from . import route_policy as _rp
from .route_policy import signin as _signin  # legba.shared.auth_signin (A-1)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

ROLE_READ = "read"
ROLE_READER_OWN_ROWS = "reader-own-rows"
ROLE_OPERATOR = "operator"

#: Ascending. Index = rank.
ROLES: tuple[str, ...] = (ROLE_READ, ROLE_READER_OWN_ROWS, ROLE_OPERATOR)
ROLE_RANK: dict[str, int] = {r: i for i, r in enumerate(ROLES)}

#: One line per class — rendered into docs/API_ROLES.md.
ROLE_DESCRIPTIONS: dict[str, str] = {
    ROLE_OPERATOR: (
        "Everything: descriptor / stack / vault / vocabulary writes, judging "
        "and grading inputs, curation, consult-driven substrate CRUD. The "
        "fleet's service principal is this class too."
    ),
    ROLE_READER_OWN_ROWS: (
        "Everything `read` can, plus the writes that only touch the caller's "
        "OWN rows: consult invocations, the watchlist, read receipts, own "
        "exports. A route that changes an existing row checks its owner."
    ),
    ROLE_READ: (
        "Every GET the display policy allows. No writes."
    ),
}

#: Methods that default to ``read`` when a route is not in the table.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Methods that MUST be listed in the table (fail closed to operator if not).
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# ---------------------------------------------------------------------------
# The table — (METHOD, mounted path template) → role
# ---------------------------------------------------------------------------

_REG = "/api/v1/registry"
_V3 = "/api/v1/v3"
_V1 = "/api/v1"

#: Every write route the registry app mounts, plus the non-``read`` GETs.
#: Grouped by owning module; the trailing comment names the file.
ROUTE_ROLES: dict[tuple[str, str], str] = {
    # -- api.py (registry core; B1/B3 own the file — gated from here) ------
    ("POST", f"{_REG}/descriptors/{{family}}"): ROLE_OPERATOR,
    ("PUT", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}"): ROLE_OPERATOR,
    ("POST", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}/retire"): ROLE_OPERATOR,
    ("POST", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}/transition"): ROLE_OPERATOR,
    ("POST", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}/promote"): ROLE_OPERATOR,
    ("POST", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}/rollback"): ROLE_OPERATOR,
    ("POST", f"{_REG}/stack"): ROLE_OPERATOR,
    ("PUT", f"{_REG}/stack/{{component_id}}"): ROLE_OPERATOR,
    ("POST", f"{_REG}/stack/{{component_id}}/retire"): ROLE_OPERATOR,
    ("POST", f"{_REG}/stack/{{component_id}}/rollback"): ROLE_OPERATOR,
    ("POST", f"{_REG}/stack/{{component_id}}/healthcheck"): ROLE_OPERATOR,
    ("POST", f"{_REG}/vault/secrets"): ROLE_OPERATOR,
    ("DELETE", f"{_REG}/vault/secrets/{{secret_id}}"): ROLE_OPERATOR,
    ("GET", f"{_REG}/vault/secrets/{{secret_id}}/exists"): ROLE_OPERATOR,
    ("POST", f"{_REG}/conversions"): ROLE_OPERATOR,
    ("DELETE", f"{_REG}/conversions/{{webhook_id}}"): ROLE_OPERATOR,
    ("POST", f"{_REG}/dead_letter/{{entry_id}}/resubmit"): ROLE_OPERATOR,
    ("POST", f"{_REG}/vocabulary/{{family}}"): ROLE_OPERATOR,
    ("PUT", f"{_REG}/vocabulary/{{family}}/{{entry_id}}"): ROLE_OPERATOR,
    ("DELETE", f"{_REG}/vocabulary/{{family}}/{{entry_id}}"): ROLE_OPERATOR,
    ("POST", f"{_REG}/targets/{{target_id}}/backfill"): ROLE_OPERATOR,
    # -- optimizer_review_api.py (split out of v3_api.py) --------------------
    ("POST", f"{_V3}/optimizer/candidates/{{candidate_id}}/review"): ROLE_OPERATOR,
    # -- journal_proposals_api.py -------------------------------------------
    ("POST", f"{_V1}/journal_proposals/{{proposal_id}}/accept"): ROLE_OPERATOR,
    ("POST", f"{_V1}/journal_proposals/{{proposal_id}}/reject"): ROLE_OPERATOR,
    # -- alert_ack_api.py (S-2) — shared alert state, not own rows: with the
    # switch off only the operator class (the bearer) may acknowledge/snooze.
    ("POST", f"{_V3}/system/alerts/{{episode_id}}/ack"): ROLE_OPERATOR,
    ("DELETE", f"{_V3}/system/alerts/{{episode_id}}/ack"): ROLE_OPERATOR,
    ("POST", f"{_V3}/system/alerts/{{episode_id}}/snooze"): ROLE_OPERATOR,
    ("DELETE", f"{_V3}/system/alerts/{{episode_id}}/snooze"): ROLE_OPERATOR,
    # -- labels_api.py / goldset_api.py (grading inputs) --------------------
    ("POST", f"{_V1}/eval/labels"): ROLE_OPERATOR,
    ("POST", f"{_V3}/eval/goldset/label"): ROLE_OPERATOR,
    # -- forecast_resolution_api.py (F-1): deciding a forecast's outcome ----
    ("GET", f"{_V3}/ledger/resolution-queue"): ROLE_OPERATOR,
    ("POST", f"{_V3}/ledger/resolutions/{{record_id}}/confirm"): ROLE_OPERATOR,
    ("POST", f"{_V3}/ledger/resolutions/{{record_id}}/reject"): ROLE_OPERATOR,
    ("POST", f"{_V3}/ledger/resolutions/{{record_id}}/judgement"): ROLE_OPERATOR,
    ("POST", f"{_V3}/ledger/resolutions/{{record_id}}/correct"): ROLE_OPERATOR,
    # -- collection_requirements_api.py / retention_policies_api.py ---------
    ("PATCH", f"{_V3}/collection-requirements/{{requirement_id}}"): ROLE_OPERATOR,
    ("PATCH", f"{_V3}/retention-policies/{{policy_name}}"): ROLE_OPERATOR,
    # -- origins_api.py (data-model lane 9): which group an origin counts in
    ("PUT", f"{_REG}/origins/{{origin_id}}/group"): ROLE_OPERATOR,
    ("POST", f"{_REG}/origins/derive"): ROLE_OPERATOR,
    # -- source_credibility_api.py (operator-curated) -----------------------
    ("PUT", f"{_V1}/source_credibility/{{host}}"): ROLE_OPERATOR,
    ("DELETE", f"{_V1}/source_credibility/{{host}}"): ROLE_OPERATOR,
    ("POST", f"{_V1}/source_credibility/bulk"): ROLE_OPERATOR,
    # -- consult_writes_api.py (consult-driven SUBSTRATE CRUD: sources,
    #    targets, units, entities, facts — operator, not own rows) ----------
    ("POST", f"{_V1}/consult/writes"): ROLE_OPERATOR,
    ("POST", f"{_V1}/consult/writes/{{write_id}}/confirm"): ROLE_OPERATOR,
    ("POST", f"{_V1}/consult/writes/{{write_id}}/cancel"): ROLE_OPERATOR,
    ("POST", f"{_V1}/consult/writes/{{write_id}}/revert"): ROLE_OPERATOR,
    # -- own rows -----------------------------------------------------------
    # consult_api.py (C1 owns the file — gated from here; the per-run owner
    # check on stop/synthesize is C1's/B3's, see docs/API_ROLES.md)
    ("POST", f"{_V1}/consult"): ROLE_READER_OWN_ROWS,
    ("POST", f"{_V1}/consult/runs/{{request_id}}/stop"): ROLE_READER_OWN_ROWS,
    ("POST", f"{_V1}/consult/runs/{{request_id}}/synthesize"): ROLE_READER_OWN_ROWS,
    # deep_consult_api.py
    ("POST", f"{_V1}/deep_consult"): ROLE_READER_OWN_ROWS,
    # read_events_api.py (append-only receipts of the caller's own reads)
    ("POST", f"{_V1}/read-events"): ROLE_READER_OWN_ROWS,
    # watchlist_api.py (PUT/DELETE check the row's created_by)
    ("POST", f"{_V3}/watchlist"): ROLE_READER_OWN_ROWS,
    ("PUT", f"{_V3}/watchlist/{{watch_id}}"): ROLE_READER_OWN_ROWS,
    ("DELETE", f"{_V3}/watchlist/{{watch_id}}"): ROLE_READER_OWN_ROWS,
    # export_api.py (B4 owns the file — renders the caller's own basket)
    ("POST", f"{_V3}/export"): ROLE_READER_OWN_ROWS,
    # me_seen_api.py (S-1) — keyed by the caller's own user id, never by the
    # request; with the sign-in switch off it answers 404 and writes nothing.
    ("PUT", f"{_V3}/me/seen"): ROLE_READER_OWN_ROWS,
    # -- users_api.py (X3-B3) -------------------------------------------------
    # The operator's user admin.
    ("POST", f"{_V1}/auth/invite"): ROLE_OPERATOR,
    ("GET", f"{_V1}/auth/users"): ROLE_OPERATOR,
    ("PATCH", f"{_V1}/auth/users/{{user_id}}"): ROLE_OPERATOR,
    # PUBLIC (see PUBLIC_ROUTES): they authenticate THEMSELVES — a password,
    # an invite token, the cookie — and never call the bearer gate. `read`,
    # the lowest class, so every signed-in class may call them too.
    ("POST", f"{_V1}/auth/accept"): ROLE_READ,
    ("POST", f"{_V1}/auth/sign-in"): ROLE_READ,
    ("POST", f"{_V1}/auth/sign-out"): ROLE_READ,
    # -- server.py / metrics_api.py (X4 GATE) --------------------------------
    # PUBLIC reads (see PUBLIC_ROUTES): the probes and the API's own
    # description. Listed so the gate admits them UNSTAMPED — every other
    # route refuses an unstamped caller.
    ("GET", f"{_REG}/healthz"): ROLE_READ,
    ("GET", f"{_REG}/docs"): ROLE_READ,
    ("GET", f"{_REG}/openapi.json"): ROLE_READ,
    ("GET", "/metrics"): ROLE_READ,
}

#: Routes that need NO credential, and why (X3-B3; X4 GATE). Each is a
#: ``read`` entry above. The ONLY routes the gate admits unstamped (a ``GET``
#: entry covers ``HEAD``). Rendered into docs/API_ROLES.md.
PUBLIC_ROUTES: dict[tuple[str, str], str] = {
    ("POST", f"{_V1}/auth/accept"): "an invite token (single-use, expiring)",
    ("POST", f"{_V1}/auth/sign-in"): "email + password (local_password provider)",
    ("POST", f"{_V1}/auth/sign-out"): "the session cookie it revokes",
    ("GET", f"{_REG}/healthz"): (
        "nothing — the container HEALTHCHECK and deploy.sh probe it bare"
    ),
    ("GET", "/metrics"): "nothing — Prometheus scrapes carry no token",
    ("GET", f"{_REG}/docs"): (
        "nothing — the Swagger page; every call it makes needs a credential"
    ),
    ("GET", f"{_REG}/openapi.json"): (
        "nothing — a framework route the gate never runs on; route shapes only"
    ),
}

#: Routes that change an EXISTING row and therefore check its owner inside
#: the handler (``require_row_owner``) — the rest of the own-rows class only
#: ever creates rows stamped with the caller. Rendered into docs/API_ROLES.md.
OWNER_CHECKED_ROUTES: dict[tuple[str, str], str] = {
    ("PUT", f"{_V3}/watchlist/{{watch_id}}"): "watchlist.created_by",
    ("DELETE", f"{_V3}/watchlist/{{watch_id}}"): "watchlist.created_by",
    # X3-B3: the run's session principal
    # (session_auth.require_consult_session_owner).
    ("POST", f"{_V1}/consult/runs/{{request_id}}/stop"): "consult_sessions.principal",
    ("POST", f"{_V1}/consult/runs/{{request_id}}/synthesize"): "consult_sessions.principal",
}

#: Own-rows routes whose per-row owner check is NOT in place yet, and who
#: owns closing it. Rendered into docs/API_ROLES.md so the gap is visible.
#: Empty since X3-B3 closed the two consult-run entries.
OWNER_CHECK_PENDING: dict[tuple[str, str], str] = {}


def _validate_table() -> None:
    for (method, path), role in ROUTE_ROLES.items():
        if role not in ROLE_RANK:
            raise ValueError(f"ROUTE_ROLES {method} {path}: unknown role {role!r}")
        if method != method.upper() or method not in (READ_METHODS | WRITE_METHODS):
            raise ValueError(f"ROUTE_ROLES {method} {path}: bad method")
        if not path.startswith("/"):
            raise ValueError(f"ROUTE_ROLES {method} {path}: path must be absolute")
    for key in (*OWNER_CHECKED_ROUTES, *OWNER_CHECK_PENDING):
        if ROUTE_ROLES.get(key) != ROLE_READER_OWN_ROWS:
            raise ValueError(f"{key}: an owner-check entry must be a reader-own-rows route")
    for key in PUBLIC_ROUTES:
        if ROUTE_ROLES.get(key) != ROLE_READ:
            raise ValueError(f"{key}: a public route must be a `read` entry")


_validate_table()

# ---------------------------------------------------------------------------
# Template matching (no reliance on the framework's route-matching internals)
# ---------------------------------------------------------------------------

_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([A-Za-z_]+))?\}")


def _template_regex(template: str) -> re.Pattern[str]:
    """Compile ``/a/{x}/b/{p:path}`` into an anchored regex."""
    out: list[str] = []
    pos = 0
    for m in _PARAM_RE.finditer(template):
        out.append(re.escape(template[pos:m.start()]))
        out.append(".+" if m.group(2) == "path" else "[^/]+")
        pos = m.end()
    out.append(re.escape(template[pos:]))
    return re.compile("^" + "".join(out) + "$")


#: method → [(regex, role)].
_COMPILED: dict[str, list[tuple[re.Pattern[str], str]]] = {}
for (_m, _p), _r in ROUTE_ROLES.items():
    _COMPILED.setdefault(_m, []).append((_template_regex(_p), _r))


#: method → [regex] of the PUBLIC routes (``HEAD`` reads the ``GET`` list).
_PUBLIC_COMPILED: dict[str, list[re.Pattern[str]]] = {}
for (_m, _p) in PUBLIC_ROUTES:
    _PUBLIC_COMPILED.setdefault(_m, []).append(_template_regex(_p))


def is_public_route(method: str, path: str) -> bool:
    """Whether ``method path`` is a :data:`PUBLIC_ROUTES` entry."""
    method = method.upper()
    if method == "HEAD":
        method = "GET"
    return any(rx.match(path) for rx in _PUBLIC_COMPILED.get(method, ()))


def required_role_for(method: str, path: str) -> str:
    """The role a ``method`` request to concrete ``path`` requires.

    A table entry whose template matches wins; when several match, the
    STRICTEST role among them is required (fail closed). An unlisted read
    method is ``read``; an unlisted write method is ``operator`` — a route
    nobody classified is never opened to a lesser class by omission.
    """
    method = method.upper()
    matched = [role for rx, role in _COMPILED.get(method, ()) if rx.match(path)]
    if matched:
        return max(matched, key=ROLE_RANK.__getitem__)
    if method in READ_METHODS:
        return ROLE_READ
    return ROLE_OPERATOR


def role_satisfies(principal_role: str, required: str) -> bool:
    return ROLE_RANK.get(principal_role, -1) >= ROLE_RANK[required]


# ---------------------------------------------------------------------------
# A-1 — the sign-in switch's table (route_policy.ROUTE_MIN_ROLES)
# ---------------------------------------------------------------------------

#: method → [(regex, minimum role)] over route_policy.ROUTE_MIN_ROLES.
_MIN_COMPILED: dict[str, list[tuple[re.Pattern[str], str]]] = {}
for (_m, _p), _r in _rp.ROUTE_MIN_ROLES.items():
    _MIN_COMPILED.setdefault(_m, []).append((_template_regex(_p), _r))


def required_min_role(method: str, path: str) -> str:
    """The minimum role (``reader`` / ``contributor`` / ``admin``) of a
    concrete request when the sign-in switch is on. ``HEAD`` reads ``GET``;
    several matches → the strictest; NO match → ``admin`` (every route is
    declared, so an undeclared one is never opened by omission)."""
    method = method.upper()
    if method == "HEAD":
        method = "GET"
    matched = [r for rx, r in _MIN_COMPILED.get(method, ()) if rx.match(path)]
    if matched:
        return max(matched, key=_signin.ROLE_RANK.__getitem__)
    return _signin.ROLE_ADMIN


# ---------------------------------------------------------------------------
# The principal
# ---------------------------------------------------------------------------

#: The env flag that allows the ``X-Legba-Role`` override (tests ONLY).
ROLE_OVERRIDE_ENV = "LEGBA_ROLE_OVERRIDE_FOR_TESTS"
ROLE_HEADER = "x-legba-role"
SUBJECT_HEADER = "x-legba-subject"

#: The subject every principal resolves to until B3 lands users: the one
#: human token is "the operator" (and watchlist rows default to it).
DEFAULT_SUBJECT = "operator"

#: request.state attribute names (the B3 / B1 seam — see the module docstring).
STATE_ROLE_ATTR = "legba_role"
STATE_SUBJECT_ATTR = "legba_subject"
# B1 stamps ``request.state.principal`` (merge follow-up: the two lanes could
# not see each other's name); the other two spellings are kept for callers.
STATE_PRINCIPAL_KIND_ATTRS = ("principal", "principal_kind", "principal_class")
#: B1's principal kinds; both are the operator class.
OPERATOR_PRINCIPAL_KINDS = frozenset({"service", "operator"})

#: X4 GATE — the request-state keys the two auth MIDDLEWARES write, declared
#: in this leaf because ``_deps`` / ``bearer_auth`` and ``session_auth`` all
#: import it:
#:   * which middleware stamped the caller (``bearer`` | ``dev`` | ``session``);
STATE_AUTH_SOURCE_ATTR = "legba_auth_source"
AUTH_SOURCE_BEARER = "bearer"
AUTH_SOURCE_DEV = "dev"
AUTH_SOURCE_SESSION = "session"
#:   * the bearer middleware's refusal when it could not stamp — ``{"status_code",
#:     "detail"}``, exactly what ``require_bearer`` raises for that request; the
#:     gate answers it to an unresolved caller;
STATE_AUTH_REFUSAL_ATTR = "legba_auth_refusal"
#:   * whether ANY bearer credential was presented (an ``Authorization: Bearer``
#:     header, a ``legba.bearer.v1`` subprotocol, the SSE / WS ``?token=``) —
#:     the session middleware resolves a cookie only when none was.
STATE_BEARER_PRESENTED_ATTR = "legba_bearer_presented"

#: ``Principal.source`` of a caller no middleware stamped.
SOURCE_UNRESOLVED = "unresolved"
#: ``Principal.subject`` of an unresolved caller — no user and no row owner is
#: ever called this, so an own-rows filter built from it confines to nothing.
UNRESOLVED_SUBJECT = "unresolved"


@dataclass(frozen=True)
class Principal:
    """Who is calling, in which role class, and how we know."""

    role: str
    subject: str
    source: str  # "request_state" | "principal_kind" | "test_override" | "unresolved"

    @property
    def is_operator(self) -> bool:
        """The top class: ``operator`` (switch off) or ``admin`` (A-1, on)."""
        return self.role in (ROLE_OPERATOR, _signin.ROLE_ADMIN)

    @property
    def is_admin(self) -> bool:
        """A-1 spelling of :attr:`is_operator`."""
        return self.is_operator

    @property
    def resolved(self) -> bool:
        return self.source != SOURCE_UNRESOLVED


_override_misuse_logged = False


def _override_allowed() -> bool:
    """The override is a TEST affordance: flag set AND pytest running."""
    global _override_misuse_logged
    if os.getenv(ROLE_OVERRIDE_ENV, "").strip() != "1":
        return False
    if "pytest" in sys.modules:
        return True
    if not _override_misuse_logged:
        logger.error(
            "registry.role_gate.override_ignored — %s=1 is set outside a test "
            "run; the %s header is IGNORED. Unset it.",
            ROLE_OVERRIDE_ENV, ROLE_HEADER,
        )
        _override_misuse_logged = True
    return False


def resolve_principal(conn: HTTPConnection) -> Principal:
    """The calling principal (see the module docstring for the order)."""
    if _override_allowed():
        raw = (conn.headers.get(ROLE_HEADER) or "").strip()
        if raw:
            if raw not in ROLE_RANK and not (
                _signin.signin_enabled() and raw in _signin.ROLE_RANK
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"unknown {ROLE_HEADER} {raw!r}; one of {list(ROLES)}",
                )
            subject = (conn.headers.get(SUBJECT_HEADER) or "").strip() or DEFAULT_SUBJECT
            return Principal(role=raw, subject=subject, source="test_override")
    state = getattr(conn, "state", None)
    role = getattr(state, STATE_ROLE_ATTR, None) if state is not None else None
    if isinstance(role, str) and (role in ROLE_RANK or role in _signin.ROLE_RANK):
        subject = getattr(state, STATE_SUBJECT_ATTR, None)
        return Principal(
            role=role,
            subject=str(subject) if subject else DEFAULT_SUBJECT,
            source="request_state",
        )
    if state is not None:
        for attr in STATE_PRINCIPAL_KIND_ATTRS:
            kind = getattr(state, attr, None)
            if isinstance(kind, str) and kind in OPERATOR_PRINCIPAL_KINDS:
                return Principal(role=ROLE_OPERATOR, subject=kind, source="principal_kind")
    # X4 GATE: nobody stamped this caller — the LOWEST class, never operator.
    return Principal(role=ROLE_READ, subject=UNRESOLVED_SUBJECT, source=SOURCE_UNRESOLVED)


def current_principal(conn: HTTPConnection) -> Principal:
    """FastAPI dependency: the resolved :class:`Principal`."""
    return resolve_principal(conn)


@dataclass(frozen=True)
class RequestPrincipal:
    """Who made THIS request, for a handler to record on a write (A-1).

    ``actor`` is the audit handle (``user:<id>``, ``service:<name>``,
    ``service:bearer:…``, ``admin-token:bearer:…``; with the switch off the
    October labels); ``kind`` is ``user`` / ``service`` / ``admin-token`` /
    ``operator`` / ``dev``; ``user_id`` is set for a signed-in person only.
    Read from the stamps the two auth middlewares wrote — never from the
    request body."""

    actor: str
    kind: str
    role: str
    user_id: str | None
    is_owner: bool

    @property
    def is_user(self) -> bool:
        return self.user_id is not None


def recorded_author(actor: str, claimed: Any) -> Any:
    """The author a write records in a column a request body used to fill
    (``reviewed_by``, ``labeled_by``, ``scored_by``, ``reviewer``) — A-1.

    Sign-in switch ON: the authenticated ``actor``, whatever the body says
    (the design's rule: stamped on the server, never taken from the body).
    OFF: ``claimed`` unchanged — today's behaviour."""
    return actor if _signin.signin_enabled() else claimed


def request_principal(conn: HTTPConnection) -> RequestPrincipal:
    """FastAPI dependency: the :class:`RequestPrincipal` of the request."""
    state = getattr(conn, "state", None)
    principal = resolve_principal(conn)
    actor = _state_get(conn, "principal_actor")
    user = _state_get(conn, "legba_session_user")
    user_id = str(user["id"]) if isinstance(user, dict) and user.get("id") else None
    kind_raw = _state_get(conn, "principal")
    if user_id is not None:
        kind = "user"
    elif isinstance(kind_raw, str) and kind_raw.startswith("service"):
        kind = "service"
    elif isinstance(kind_raw, str) and kind_raw:
        kind = kind_raw
    elif state is not None and _state_get(conn, STATE_AUTH_SOURCE_ATTR) == AUTH_SOURCE_DEV:
        kind = "dev"
    else:
        kind = "unresolved"
    role = principal.role
    if _signin.signin_enabled():
        role = _signin.normalize_role(role) or role
    return RequestPrincipal(
        actor=actor if isinstance(actor, str) and actor else principal.subject,
        kind=kind,
        role=role,
        user_id=user_id,
        is_owner=bool(isinstance(user, dict) and user.get("is_owner")),
    )


def _forbidden(*, required: str, principal: Principal, method: str, path: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "role_forbidden",
            "required_role": required,
            "principal_role": principal.role,
            "method": method,
            "path": path,
            "message": (
                f"{method} {path} requires role {required!r}; the caller's "
                f"role is {principal.role!r}"
            ),
        },
    )


def require_role(role: str) -> Callable[[HTTPConnection], Principal]:
    """Dependency factory: refuse (403, naming the role) below ``role``.

    For a route that must state its role at the declaration site. The
    app-level :func:`enforce_route_role` already gates every mounted route
    through ``ROUTE_ROLES``; use this where a route is mounted outside the
    registry app or needs a role stricter than its table entry.
    """
    if role not in ROLE_RANK:
        raise ValueError(f"unknown role {role!r}")

    def _dep(conn: HTTPConnection) -> Principal:
        principal = resolve_principal(conn)
        if not role_satisfies(principal.role, role):
            raise _forbidden(
                required=role, principal=principal,
                method=conn.scope.get("method", "WEBSOCKET"),
                path=conn.scope.get("path", ""),
            )
        return principal

    _dep.__name__ = f"require_role_{role.replace('-', '_')}"
    return _dep


def _state_get(conn: HTTPConnection, attr: str) -> Any:
    state = conn.scope.get("state")
    if isinstance(state, dict):
        return state.get(attr)
    return getattr(state, attr, None) if state is not None else None


def _unresolved_refusal(
    conn: HTTPConnection, method: str, path: str,
) -> HTTPException | WebSocketException:
    """The answer to an UNRESOLVED caller (module docstring, step 3).

    The bearer middleware's recorded refusal when there is one — the status
    and detail ``require_bearer`` raised before X4, so a client keyed on the
    401 (the UI's sign-in screen) sees no change — else ``403
    role_unresolved``: no auth middleware stamped this request at all.
    """
    refusal = _state_get(conn, STATE_AUTH_REFUSAL_ATTR)
    if isinstance(refusal, dict) and isinstance(refusal.get("status_code"), int):
        code, detail = refusal["status_code"], refusal.get("detail")
    else:
        code = status.HTTP_403_FORBIDDEN
        detail = {
            "error": "role_unresolved",
            "method": method,
            "path": path,
            "message": (
                f"{method} {path}: no credential was resolved before the route "
                "gate (neither the bearer nor the session middleware stamped "
                "this request), so its role class is unknown; refused"
            ),
        }
    if conn.scope.get("type") == "websocket":
        # The events socket's own mapping: 503 → 1011, any refusal → 1008.
        close = (
            status.WS_1011_INTERNAL_ERROR
            if code == status.HTTP_503_SERVICE_UNAVAILABLE
            else status.WS_1008_POLICY_VIOLATION
        )
        reason = detail.get("error") if isinstance(detail, dict) else detail
        return WebSocketException(code=close, reason=str(reason)[:120])
    return HTTPException(status_code=code, detail=detail)


def enforce_route_role(conn: HTTPConnection) -> None:
    """APP-LEVEL dependency: gate the request through ``ROUTE_ROLES``.

    Installed once in ``server.create_app`` (``FastAPI(dependencies=...)``),
    so it runs for every mounted route. It admits nobody the auth
    middlewares did not stamp: an UNRESOLVED caller is refused on every
    route outside ``PUBLIC_ROUTES`` (X4 GATE), and a resolved principal is
    refused below the route's role. The bearer gate in a route still
    authenticates. A WebSocket upgrade has no method; it is a read.
    """
    method = str(conn.scope.get("method") or "GET").upper()
    path = str(conn.scope.get("path") or "")
    principal = resolve_principal(conn)
    if not principal.resolved:
        if is_public_route(method, path):
            return
        logger.info("registry.role_gate.unresolved method=%s path=%s", method, path)
        raise _unresolved_refusal(conn, method, path)
    if _signin.signin_enabled():
        is_ws = conn.scope.get("type") == "websocket"
        _enforce_min_role(principal, "WEBSOCKET" if is_ws else method, path)
        return
    required = required_role_for(method, path)
    if not role_satisfies(principal.role, required):
        logger.info(
            "registry.role_gate.refused method=%s path=%s required=%s role=%s source=%s",
            method, path, required, principal.role, principal.source,
        )
        raise _forbidden(required=required, principal=principal, method=method, path=path)


def _enforce_min_role(principal: Principal, method: str, path: str) -> None:
    """A-1 (switch on): refuse a principal below the route's minimum role in
    ``route_policy.ROUTE_MIN_ROLES``. Legacy role names (a user row from
    before the switch) read through ``auth_signin.LEGACY_ROLE_MAP``."""
    required = required_min_role(method, path)
    if _signin.role_satisfies(principal.role, required):
        return
    have = _signin.normalize_role(principal.role) or principal.role
    logger.info(
        "registry.role_gate.refused method=%s path=%s required=%s role=%s source=%s",
        method, path, required, have, principal.source,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "role_forbidden",
            "required_role": required,
            "principal_role": have,
            "method": method,
            "path": path,
            "message": (
                f"{method} {path} requires role {required!r}; the caller's "
                f"role is {have!r}"
            ),
        },
    )


def require_row_owner(principal: Principal, owner: Any, *, what: str) -> None:
    """Refuse (403) a non-operator principal touching a row it does not own.

    ``owner`` is the row's owner column value; ``what`` names the row for the
    error body (e.g. ``"watch <id>"``). Operators may touch any row.
    """
    if principal.is_operator:
        return
    if owner is None or str(owner) != principal.subject:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "not_row_owner",
                "required_role": ROLE_OPERATOR,
                "principal_role": principal.role,
                "message": (
                    f"{what} belongs to another user; role {principal.role!r} "
                    f"may change only its own rows (role {ROLE_OPERATOR!r} "
                    f"may change any)"
                ),
            },
        )


# ---------------------------------------------------------------------------
# Enumeration + the generated doc
# ---------------------------------------------------------------------------


def iter_mounted_routes(app: Any) -> Iterator[tuple[str, str]]:
    """Yield ``(METHOD, path template)`` for every HTTP route ``app`` mounts.

    Walks lazily-included routers too (newer FastAPI keeps an included router
    as one entry; ``fastapi.routing.iter_route_contexts`` flattens it). A
    WebSocket route yields ``("WEBSOCKET", path)``.
    """
    try:
        from fastapi.routing import iter_route_contexts  # type: ignore[attr-defined]
    except ImportError:  # older FastAPI: app.routes is already flat
        iter_route_contexts = None
    routes: Iterable[Any] = (
        iter_route_contexts(app.routes) if iter_route_contexts else app.routes
    )
    for route in routes:
        path = getattr(route, "path_format", None) or getattr(route, "path", None)
        if not path:
            continue
        methods = getattr(route, "methods", None)
        if methods:
            for m in sorted(methods):
                yield m.upper(), path
        else:
            original = getattr(route, "original_route", route)
            if "websocket" in type(original).__name__.lower():
                yield "WEBSOCKET", path


def render_api_roles_md() -> str:
    """Render docs/API_ROLES.md from the table (the drift test compares)."""
    lines = [
        "<!-- GENERATED by scripts/gen_api_roles_doc.py from",
        "     src/legba/data/registry/route_roles.py::ROUTE_ROLES — do not edit",
        "     by hand; tests/data_pkg/test_route_roles.py fails on drift. -->",
        "",
        "# Registry API roles",
        "",
        "Every registry route is gated by ONE table, `ROUTE_ROLES` in",
        "`src/legba/data/registry/route_roles.py`, through the app-level",
        "dependency `enforce_route_role` (installed once in `server.create_app`).",
        "It refuses a caller whose role class is below the route's — `403` with",
        "`detail.error = \"role_forbidden\"` and `detail.required_role` naming",
        "the role — and a caller no auth middleware resolved (below).",
        "",
        "## Role classes",
        "",
        "Ascending: a class may call every route a lower class may.",
        "",
        "| role | may |",
        "|---|---|",
    ]
    for role in ROLES:
        lines.append(f"| `{role}` | {ROLE_DESCRIPTIONS[role]} |")
    lines += [
        "",
        "## Defaults",
        "",
        "- A GET/HEAD/OPTIONS route not listed below requires `read`.",
        "- A POST/PUT/PATCH/DELETE route not listed below requires `operator`",
        "  (fail closed); a test asserts every mounted write route IS listed.",
        "- A WebSocket upgrade is a read.",
        "",
        "## Who the caller is",
        "",
        "Two pure-ASGI middlewares resolve the caller BEFORE this gate runs and",
        f"stamp `request.state.{STATE_ROLE_ATTR}` / `request.state.{STATE_SUBJECT_ATTR}`:",
        "",
        "- the BEARER stamp (`bearer_auth.BearerPrincipalMiddleware`, outermost):",
        "  the operator's token and the fleet's service token are both `operator`",
        "  (subject `operator` / `service`); dev mode (no token configured,",
        "  `LEGBA_DEV_MODE=1`) is `operator` too. The SSE relay's `?token=` and",
        "  the events socket's `legba.bearer.v1` subprotocol resolve here as well.",
        "- the SESSION stamp (`session_auth.SessionCookieMiddleware`): a signed-in",
        "  USER (the `legba_session` cookie, no bearer credential) is their own",
        "  role class; the subject is the local user id.",
        "",
        "A caller neither middleware stamped is UNRESOLVED and is refused on",
        "every route except the public ones below: with the bearer middleware's",
        "own answer when it recorded one (`401` no credential, `403` a bad one,",
        "`503` no token configured — the answers `require_bearer` always gave),",
        "else `403` with `detail.error = \"role_unresolved\"`. A route that forgets",
        "`Depends(require_bearer)` is therefore never open. Tests may set",
        f"`{ROLE_OVERRIDE_ENV}=1` and send `X-Legba-Role` / `X-Legba-Subject`;",
        "outside pytest that flag is ignored and logged as an error.",
        "",
        "## Public routes",
        "",
        "The only routes the gate admits unstamped. The auth writes take no",
        "credential because they are the credential step (each authenticates",
        "itself); the reads are the probes and the API's own description. Each",
        "is a `read` entry in the table.",
        "",
        "| method | path | authenticates with |",
        "|---|---|---|",
        *[
            f"| {m} | `{p}` | {why} |"
            for (m, p), why in sorted(PUBLIC_ROUTES.items(), key=lambda kv: kv[0][1])
        ],
        "",
        "## The table",
        "",
        "| method | path | role | owner check |",
        "|---|---|---|---|",
    ]
    for (method, path), role in sorted(
        ROUTE_ROLES.items(), key=lambda kv: (kv[0][1], kv[0][0])
    ):
        key = (method, path)
        if key in OWNER_CHECKED_ROUTES:
            owner = f"`{OWNER_CHECKED_ROUTES[key]}`"
        elif key in OWNER_CHECK_PENDING:
            owner = f"PENDING — {OWNER_CHECK_PENDING[key]}"
        else:
            owner = "—"
        lines.append(f"| {method} | `{path}` | `{role}` | {owner} |")
    counts = {r: sum(1 for v in ROUTE_ROLES.values() if v == r) for r in ROLES}
    lines += [
        "",
        f"{len(ROUTE_ROLES)} entries: "
        + ", ".join(f"{counts[r]} `{r}`" for r in reversed(ROLES))
        + ".",
        "",
    ]
    lines += _render_min_role_section()
    return "\n".join(lines)


def _render_min_role_section() -> list[str]:
    """The A-1 section: the minimum role of EVERY route under the sign-in
    switch (``route_policy.ROUTE_MIN_ROLES``)."""
    lines = [
        f"# With sign-in on (`{_signin.SIGNIN_ENV}=1`)",
        "",
        "The tables above are the answer while the switch is off (the default).",
        f"With `{_signin.SIGNIN_ENV}=1` the same gate reads",
        "`ROUTE_MIN_ROLES` in `src/legba/data/registry/route_policy.py`",
        "instead: three roles, every mounted route declared. A route with no",
        "entry requires `admin`, and `scripts/gen_api_roles_doc.py --check`",
        "fails on any mounted route without an entry. `HEAD` reads its `GET`",
        "entry; public routes are the same list as above.",
        "",
        "## Roles",
        "",
        "Ascending: a role may call every route a lower role may. A user row",
        "written before the switch reads as: "
        + ", ".join(
            f"`{old}` → `{new}`" for old, new in _signin.LEGACY_ROLE_MAP.items()
        )
        + ".",
        "",
        "| role | may |",
        "|---|---|",
    ]
    for role in _signin.ROLES:
        lines.append(f"| `{role}` | {_rp.ROLE_DESCRIPTIONS[role]} |")
    lines += [
        "",
        "## Who the caller is",
        "",
        "- a signed-in person (the `legba_session` cookie): their role; the",
        "  subject is the user id, the audit actor `user:<id>`.",
        "- a service credential `LEGBA_SERVICE_TOKEN_<NAME>` (names: "
        + ", ".join(f"`{n}`" for n in _signin.SERVICE_NAMES)
        + "): `admin`, actor",
        "  `service:<name>`; refused on the human-only routes. The shared fleet",
        "  token still works as `service`.",
        "- the shared admin token: `admin`, actor `admin-token:bearer:<sha12>`,",
        "  logged at WARNING as break-glass use.",
        "",
        "## Minimum role per route",
        "",
        "| method | path | minimum role |",
        "|---|---|---|",
    ]
    for (method, path), role in sorted(
        _rp.ROUTE_MIN_ROLES.items(), key=lambda kv: (kv[0][1], kv[0][0])
    ):
        lines.append(f"| {method} | `{path}` | `{role}` |")
    counts = _rp.role_counts()
    lines += [
        "",
        f"{len(_rp.ROUTE_MIN_ROLES)} routes: "
        + ", ".join(f"{counts[r]} `{r}`" for r in reversed(_signin.ROLES))
        + ".",
        "",
    ]
    return lines
