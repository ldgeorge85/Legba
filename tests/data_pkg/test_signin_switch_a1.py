# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sign-in switch (``LEGBA_AUTH_SIGNIN``), the parts that need no
database: the credential list, the stamps, the minimum-role table and its
generator check, the same-origin rule, the session read's SQL, the author
rule, the client's own service token.

Every block pins BOTH sides: with the switch OFF the answer is the one the
code gave before the switch existed (the off-path proof), and with it ON the
new behaviour. The October suites (``test_route_roles``,
``test_bearer_gate_x4``, ``test_users_signin_b3``) run unchanged beside this
file and are the rest of the off-path proof.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from legba.data import users as U
from legba.data.registry import _deps
from legba.data.registry import route_policy as rp
from legba.data.registry import route_roles as rr
from legba.data.registry import session_auth as sa
from legba.shared import auth_signin as S
from legba.shared import registry_principal as RP

_REPO = pathlib.Path(__file__).resolve().parents[2]

ADMIN_TOK = "a1-admin-token-" + "0" * 24
FLEET_TOK = "a1-fleet-token-" + "1" * 24
RUNTIME_TOK = "a1-runtime-token-" + "2" * 22
WORKER_TOK = "a1-worker-token-" + "3" * 23


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", ADMIN_TOK)
    monkeypatch.setenv("LEGBA_REGISTRY_SERVICE_TOKEN", FLEET_TOK)
    monkeypatch.setenv(S.service_token_env("runtime"), RUNTIME_TOK)
    monkeypatch.setenv(S.service_token_env("worker"), WORKER_TOK)
    # A host .env with sign-in switched on carries these two as well; the
    # tests below name exactly the credentials they set.
    monkeypatch.delenv(S.service_token_env("registry"), raising=False)
    monkeypatch.delenv(S.service_token_env("console"), raising=False)
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    monkeypatch.delenv(rr.ROLE_OVERRIDE_ENV, raising=False)


@pytest.fixture
def off(monkeypatch):
    monkeypatch.delenv(S.SIGNIN_ENV, raising=False)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(S.SIGNIN_ENV, "1")


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


def test_the_switch_is_one_exact_value() -> None:
    assert S.SIGNIN_ENV == "LEGBA_AUTH_SIGNIN"
    assert S.signin_enabled({}) is False
    for v in ("0", "", "true", "yes", "on", " 2"):
        assert S.signin_enabled({S.SIGNIN_ENV: v}) is False, v
    assert S.signin_enabled({S.SIGNIN_ENV: "1"}) is True
    assert S.signin_enabled({S.SIGNIN_ENV: " 1 "}) is True


def test_role_vocabulary_and_legacy_map() -> None:
    assert S.ROLES == ("reader", "contributor", "admin")
    assert S.normalize_role("read") == "reader"
    assert S.normalize_role("reader-own-rows") == "reader"
    assert S.normalize_role("operator") == "admin"
    assert S.normalize_role("nope") is None
    assert S.role_satisfies("admin", "contributor")
    assert S.role_satisfies("operator", "admin")
    assert not S.role_satisfies("reader", "contributor")
    assert not S.role_satisfies("reader-own-rows", "contributor")
    assert S.SERVICE_TOKEN_ENVS == {
        "registry": "LEGBA_SERVICE_TOKEN_REGISTRY",
        "runtime": "LEGBA_SERVICE_TOKEN_RUNTIME",
        "worker": "LEGBA_SERVICE_TOKEN_WORKER",
        "console": "LEGBA_SERVICE_TOKEN_CONSOLE",
    }


# ---------------------------------------------------------------------------
# The registry's credential list and stamps
# ---------------------------------------------------------------------------


def test_off_credentials_are_the_two_they_always_were(creds, off) -> None:
    assert _deps._configured_principals() == [
        ("operator", ADMIN_TOK), ("service", FLEET_TOK),
    ]
    # A per-service token is NOT a credential while the switch is off.
    with pytest.raises(HTTPException) as exc:
        _deps.resolve_bearer_header(f"Bearer {RUNTIME_TOK}")
    assert (exc.value.status_code, exc.value.detail) == (403, "invalid bearer token")
    kind, actor = _deps.resolve_bearer_header(f"Bearer {ADMIN_TOK}")
    assert kind == "operator" and actor.startswith("operator:bearer:")
    state: dict[str, Any] = {}
    _deps.stamp_bearer(state, kind, actor)
    assert state[rr.STATE_ROLE_ATTR] == "operator"
    assert state[rr.STATE_SUBJECT_ATTR] == "operator"


def test_on_credentials_name_each_service_and_break_glass(creds, on) -> None:
    kinds = [k for k, _ in _deps._configured_principals()]
    assert kinds == ["admin-token", "service:runtime", "service:worker", "service"]
    kind, actor = _deps.resolve_bearer_header(f"Bearer {RUNTIME_TOK}")
    assert (kind, actor) == ("service:runtime", "service:runtime")
    state: dict[str, Any] = {}
    _deps.stamp_bearer(state, kind, actor)
    assert state[rr.STATE_ROLE_ATTR] == "admin"
    assert state[rr.STATE_SUBJECT_ATTR] == "service:runtime"
    # The shared fleet token keeps working, labelled as before.
    kind, actor = _deps.resolve_bearer_header(f"Bearer {FLEET_TOK}")
    assert kind == "service" and actor.startswith("service:bearer:")
    # The admin token is break-glass: admitted, counted, never "operator".
    before = _deps.break_glass_count()
    kind, actor = _deps.resolve_bearer_header(f"Bearer {ADMIN_TOK}", method="GET", path="/x")
    assert kind == "admin-token" and actor.startswith("admin-token:bearer:")
    assert ADMIN_TOK not in actor
    assert _deps.break_glass_count() == before + 1


def test_on_a_named_service_is_refused_on_human_only_routes(creds, on) -> None:
    with pytest.raises(HTTPException) as exc:
        _deps.resolve_bearer_header(
            f"Bearer {WORKER_TOK}", method="POST",
            path="/api/v1/registry/vault/secrets",
        )
    assert exc.value.status_code == 403
    assert "service_principal_refused" in exc.value.detail


# ---------------------------------------------------------------------------
# The minimum-role table
# ---------------------------------------------------------------------------


def test_min_role_table_counts_and_defaults() -> None:
    counts = rp.role_counts()
    assert sum(counts.values()) == len(rp.ROUTE_MIN_ROLES)
    # 7 + ack / snooze (POST+DELETE each) + queue, confirm, reject,
    # judgement + resolution records (contributor); correct is admin.
    assert counts["contributor"] == 16
    assert rr.required_min_role("GET", "/api/v1/registry/descriptors") == "admin"
    assert rr.required_min_role("HEAD", "/api/v1/v3/timeline") == "reader"
    assert rr.required_min_role("POST", "/api/v1/eval/labels") == "contributor"
    assert rr.required_min_role("POST", "/api/v1/v3/watchlist") == "reader"
    assert rr.required_min_role("PUT", "/api/v1/v3/watchlist/abc") == "reader"
    # Undeclared — any method — is admin.
    assert rr.required_min_role("GET", "/api/v1/v3/not-declared") == "admin"
    assert rr.required_min_role("POST", "/api/v1/not/declared") == "admin"
    # Every public route is a reader entry.
    for (m, p) in rr.PUBLIC_ROUTES:
        assert rp.ROUTE_MIN_ROLES[(m, p)] == "reader", (m, p)


def _fake_conn(role: str | None, path: str, method: str = "GET") -> Any:
    state = SimpleNamespace()
    if role is not None:
        state.legba_role = role
        state.legba_subject = "u1"
    return SimpleNamespace(
        state=state, headers={}, scope={"type": "http", "method": method, "path": path},
    )


def test_the_gate_reads_the_new_table_only_when_on(monkeypatch) -> None:
    path = "/api/v1/v3/system/production-gauge"  # admin under the switch
    monkeypatch.delenv(S.SIGNIN_ENV, raising=False)
    # OFF: an unlisted GET is `read` — a `read` user passes, as before.
    rr.enforce_route_role(_fake_conn("read", path))
    monkeypatch.setenv(S.SIGNIN_ENV, "1")
    with pytest.raises(HTTPException) as exc:
        rr.enforce_route_role(_fake_conn("reader", path))
    assert exc.value.status_code == 403
    assert exc.value.detail["required_role"] == "admin"
    assert exc.value.detail["principal_role"] == "reader"
    rr.enforce_route_role(_fake_conn("admin", path))
    # A legacy row's role reads through the map: operator → admin.
    rr.enforce_route_role(_fake_conn("operator", path))
    # Contributor routes.
    with pytest.raises(HTTPException):
        rr.enforce_route_role(_fake_conn("reader", "/api/v1/eval/labels", "POST"))
    rr.enforce_route_role(_fake_conn("contributor", "/api/v1/eval/labels", "POST"))
    # The events socket (a WebSocket scope has no method) reads its own entry.
    ws = _fake_conn("reader", "/api/v1/registry/events")
    ws.scope = {"type": "websocket", "path": "/api/v1/registry/events"}
    rr.enforce_route_role(ws)
    # Unresolved callers are still refused before any role check.
    with pytest.raises(HTTPException):
        rr.enforce_route_role(_fake_conn(None, "/api/v1/v3/timeline"))


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "gen_api_roles_doc", _REPO / "scripts" / "gen_api_roles_doc.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_generator_check_fails_on_an_undeclared_and_a_stale_route() -> None:
    gen = _load_generator()
    mounted = {declared for declared in rp.ROUTE_MIN_ROLES}
    mounted.add(("HEAD", "/api/v1/registry/openapi.json"))
    assert gen.check_declared(mounted) == []
    problems = gen.check_declared(mounted | {("POST", "/api/v1/brand/new")})
    assert problems == ["undeclared route (no minimum role): POST /api/v1/brand/new"]
    missing = set(mounted)
    missing.discard(("GET", "/api/v1/v3/timeline"))
    assert gen.check_declared(missing) == [
        "ROUTE_MIN_ROLES entry the app does not mount: GET /api/v1/v3/timeline"
    ]


def test_every_mounted_route_of_the_real_app_is_declared(monkeypatch) -> None:
    from legba.data.registry.credentials import MASTER_KEY_ENV

    if not __import__("os").environ.get(MASTER_KEY_ENV):
        monkeypatch.setenv(MASTER_KEY_ENV, "00112233445566778899" * 3 + "0011")
    if not __import__("os").environ.get("LEGBA_REGISTRY_SIGNING_KEY"):
        monkeypatch.setenv("LEGBA_REGISTRY_SIGNING_KEY", "22" * 32)
    gen = _load_generator()
    mounted = gen.mounted_routes()
    assert len(mounted) >= 180, len(mounted)
    assert gen.check_declared(mounted) == []


def test_api_roles_doc_carries_the_new_table() -> None:
    doc = (_REPO / "docs" / "API_ROLES.md").read_text()
    assert doc == rr.render_api_roles_md()
    assert "# With sign-in on (`LEGBA_AUTH_SIGNIN=1`)" in doc
    assert "| POST | `/api/v1/eval/labels` | `contributor` |" in doc


# ---------------------------------------------------------------------------
# Sessions: the same-origin rule and the idle read
# ---------------------------------------------------------------------------


def _scope(method: str, path: str, **headers: str) -> dict[str, Any]:
    return {
        "type": "http", "method": method, "path": path,
        "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()],
        "state": {},
    }


def test_cross_site_writes_are_refused() -> None:
    cookie = f"{sa.SESSION_COOKIE_NAME}=abc"
    f = sa.cross_site_write_refusal
    assert f(_scope("POST", "/api/v1/v3/watchlist", cookie=cookie, sec_fetch_site="cross-site"))
    assert f(_scope("POST", "/api/v1/v3/watchlist", cookie=cookie, sec_fetch_site="same-site"))
    assert f(_scope("POST", "/api/v1/v3/watchlist", cookie=cookie,
                    origin="https://evil.example", host="console.example"))
    assert f(_scope("POST", "/api/v1/auth/sign-in", origin="https://evil.example",
                    host="console.example"))
    # Same origin, reads, bearer clients and non-browsers pass.
    assert f(_scope("POST", "/api/v1/v3/watchlist", cookie=cookie, sec_fetch_site="same-origin")) is None
    assert f(_scope("POST", "/api/v1/v3/watchlist", cookie=cookie,
                    origin="https://console.example", host="console.example")) is None
    assert f(_scope("GET", "/api/v1/v3/watchlist", cookie=cookie, sec_fetch_site="cross-site")) is None
    assert f(_scope("POST", "/api/v1/v3/watchlist", cookie=cookie)) is None
    assert f(_scope("POST", "/api/v1/v3/watchlist", sec_fetch_site="cross-site")) is None
    bearer = _scope("POST", "/api/v1/v3/watchlist", cookie=cookie, sec_fetch_site="cross-site")
    bearer["state"][rr.STATE_BEARER_PRESENTED_ATTR] = True  # the bearer middleware's flag
    assert f(bearer) is None


def test_cross_site_refusal_runs_only_when_on(monkeypatch) -> None:
    sent: list[dict[str, Any]] = []
    reached: list[bool] = []

    async def inner(scope, receive, send):
        reached.append(True)

    async def send(msg):
        sent.append(msg)

    mw = sa.SessionCookieMiddleware(inner)
    scope = _scope("POST", "/api/v1/auth/sign-in", sec_fetch_site="cross-site")
    monkeypatch.delenv(S.SIGNIN_ENV, raising=False)
    asyncio.run(mw(dict(scope, state={}), None, send))
    assert reached == [True] and sent == []
    monkeypatch.setenv(S.SIGNIN_ENV, "1")
    asyncio.run(mw(dict(scope, state={}), None, send))
    assert reached == [True]
    assert sent[0]["status"] == 403
    assert b"cross_site_refused" in sent[1]["body"]


class _RecConn:
    def __init__(self, row: Any = None) -> None:
        self.row = row
        self.fetched: list[tuple[str, tuple]] = []
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.fetched.append((sql, args))
        return self.row

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "UPDATE 1"


def _user_row(**extra: Any) -> dict[str, Any]:
    row = {c: None for c in U._USER_COLUMN_NAMES}
    row.update(id="00000000-0000-0000-0000-0000000000a1", email="x@a1.example",
               display_name="", role="operator", status="active",
               auth_provider="local_password")
    row.update(extra)
    return row


def test_session_read_off_is_the_october_query(off) -> None:
    conn = _RecConn(_user_row())
    user = asyncio.run(U.resolve_session(conn, "tok"))
    assert conn.fetched[0][0] == U.RESOLVE_SESSION_SQL
    assert conn.executed == []
    assert user is not None and user.role == "operator" and user.is_owner is None
    assert "is_owner" not in user.public()
    assert user.effective_role == "operator"


def test_session_read_on_checks_idle_and_touches(on, monkeypatch) -> None:
    monkeypatch.setenv(S.IDLE_HOURS_ENV, "6")
    conn = _RecConn(_user_row(is_owner=True, touch_due=True))
    user = asyncio.run(U.resolve_session(conn, "tok"))
    sql, args = conn.fetched[0]
    assert sql == U.RESOLVE_SESSION_SIGNIN_SQL
    assert "make_interval(hours => $2)" in sql
    assert args[1] == 6
    assert len(conn.executed) == 1 and "last_seen_at = now()" in conn.executed[0][0]
    assert user is not None and user.is_owner is True
    assert user.public()["role"] == "admin" and user.public()["is_owner"] is True
    conn = _RecConn(_user_row(is_owner=False, touch_due=False))
    asyncio.run(U.resolve_session(conn, "tok"))
    assert conn.executed == []
    assert asyncio.run(U.resolve_session(_RecConn(None), "tok")) is None


def test_role_check_follows_the_switch(monkeypatch) -> None:
    monkeypatch.delenv(S.SIGNIN_ENV, raising=False)
    assert U._check_role("operator") == "operator"
    with pytest.raises(U.AuthRefused):
        U._check_role("admin")
    monkeypatch.setenv(S.SIGNIN_ENV, "1")
    assert U._check_role("contributor") == "contributor"
    with pytest.raises(U.AuthRefused):
        U._check_role("operator")


# ---------------------------------------------------------------------------
# Authors and the client's own token
# ---------------------------------------------------------------------------


def test_recorded_author_follows_the_switch(monkeypatch) -> None:
    monkeypatch.delenv(S.SIGNIN_ENV, raising=False)
    assert rr.recorded_author("user:1", "someone-else") == "someone-else"
    monkeypatch.setenv(S.SIGNIN_ENV, "1")
    assert rr.recorded_author("user:1", "someone-else") == "user:1"


def test_the_runtime_presents_its_own_token_only_when_on() -> None:
    env = {
        "LEGBA_REGISTRY_API_TOKEN": ADMIN_TOK,
        "LEGBA_REGISTRY_SERVICE_TOKEN": FLEET_TOK,
        S.service_token_env("runtime"): RUNTIME_TOK,
        S.SERVICE_NAME_ENV: "runtime",
    }
    assert RP.service_token(env) == FLEET_TOK  # off: today's token
    assert RP.service_token({**env, S.SIGNIN_ENV: "1"}) == RUNTIME_TOK
    # On, but this process's own key unset → the shared token, as before.
    no_own = {k: v for k, v in env.items() if k != S.service_token_env("runtime")}
    assert RP.service_token({**no_own, S.SIGNIN_ENV: "1"}) == FLEET_TOK
    assert RP.check_service_principal("runtime", {**env, S.SIGNIN_ENV: "1"}) == "service"


def test_the_principal_leaf_mirrors_the_switch_names() -> None:
    """``registry_principal`` stays stdlib-only, so it mirrors four names."""
    m = RP._signin
    assert m.SIGNIN_ENV == S.SIGNIN_ENV
    assert m.SERVICE_NAME_ENV == S.SERVICE_NAME_ENV
    assert m.SERVICE_NAMES == S.SERVICE_NAMES
    assert m.COMPONENT_SERVICE == S.COMPONENT_SERVICE
    for name in S.SERVICE_NAMES:
        assert m.service_token_env(name) == S.service_token_env(name)
    for v in ("1", " 1 ", "0", "", "true"):
        assert m.signin_enabled({S.SIGNIN_ENV: v}) == S.signin_enabled({S.SIGNIN_ENV: v})


def test_request_principal_reads_the_stamps(on) -> None:
    state = SimpleNamespace(
        legba_role="contributor", legba_subject="u9", principal="user",
        principal_actor="user:u9", legba_session_user={"id": "u9", "is_owner": False},
        legba_auth_source="session",
    )
    conn = SimpleNamespace(state=state, headers={}, scope={"type": "http", "state": state})
    p = rr.request_principal(conn)
    assert (p.actor, p.kind, p.role, p.user_id, p.is_owner) == (
        "user:u9", "user", "contributor", "u9", False,
    )


# ---------------------------------------------------------------------------
# The edge (docker/Caddyfile)
# ---------------------------------------------------------------------------


def _snippet(body: str, name: str) -> str:
    start = body.index(f"({name}) {{")
    end = body.index("\n}\n", start)
    return body[start:end]


def test_the_edge_hands_out_the_admin_token_only_with_the_switch_off() -> None:
    """Every mode-A upstream goes through a switch snippet; only the `_0`
    snippets inject the admin bearer, the `_1` ones strip the header. (The
    adapted JSON with the switch unset / empty / 0 was compared with the
    pre-switch Caddyfile's in a throwaway caddy container: identical.)"""
    body = (_REPO / "docker" / "Caddyfile").read_text()
    inject = 'header_up Authorization "Bearer {$LEGBA_REGISTRY_API_TOKEN}"'
    assert body.count(inject) == 2
    for name in ("mode_a_api_0", "mode_a_plain_0"):
        assert inject in _snippet(body, name)
    for name in ("mode_a_api_1", "mode_a_plain_1"):
        snip = _snippet(body, name)
        assert "Bearer" not in snip and "header_up -Authorization" in snip
    for name in ("mode_a_api_", "mode_a_plain_"):
        assert f"import {name}0" in _snippet(body, name)
    assert body.count("import mode_a_api_{$LEGBA_AUTH_SIGNIN:0}") == 1
    assert body.count("import mode_a_plain_{$LEGBA_AUTH_SIGNIN:0}") == 2


def test_the_console_perimeter_drops_only_with_sign_in_on() -> None:
    """`LEGBA_EDGE_PERIMETER=0` removes the console's basic_auth wall; the edge
    has no snippet for 0 with sign-in off, so caddy refuses to start there
    instead of handing the injected admin bearer to anyone. ntfy keeps it."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "docker" / "Caddyfile").read_text()
    snippets = set(re.findall(r"^\((console_perimeter_[0-9_]*)\) \{", text, re.M))
    assert snippets == {
        "console_perimeter_1_0", "console_perimeter_1_1", "console_perimeter_1_",
        "console_perimeter_0_1",
    }
    off_body = text.split("(console_perimeter_0_1) {", 1)[1].split("}", 1)[0]
    assert off_body.strip() == ""
    for name in ("console_perimeter_1_0", "console_perimeter_1_1", "console_perimeter_1_"):
        body = text.split(f"({name}) {{", 1)[1].split("}", 1)[0]
        assert "import basic_auth_perimeter" in body
    console, alerts = text.split("alerts.{$LEGBA_PUBLIC_DOMAIN:localhost} {", 1)
    site = console.split("\n{$LEGBA_PUBLIC_DOMAIN:localhost} {", 1)[1]
    assert "import basic_auth_perimeter" not in site
    assert site.count(
        "import console_perimeter_{$LEGBA_EDGE_PERIMETER:1}_{$LEGBA_AUTH_SIGNIN:0}"
    ) == 6
    assert "import basic_auth_perimeter" in alerts
