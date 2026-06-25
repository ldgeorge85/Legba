# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B-2 — fail-closed bearer auth for the registry API.

Asserts the post-B-2 gate contract (`legba.data.registry.api`):

  * `LEGBA_REGISTRY_API_TOKEN` unset/empty AND no `LEGBA_DEV_MODE=1` →
    HTTP 503 on every guarded request (the pre-B-2 behaviour was a silent
    fail-open to an "anonymous" principal — one misconfigured deploy away
    from an open admin API).
  * `LEGBA_DEV_MODE=1` (explicit) → development mode permitted.
  * Token configured → enforced ALWAYS (dev flag does not weaken it):
    missing header → 401, wrong token → 403 via a constant-time
    `hmac.compare_digest` comparison.
  * The WebSocket `?token=` path mirrors all of the above (503 →
    close-code 1011, auth rejection → close-code 1008).

Uses the stub-deps app factory from `test_registry_api_unit` — no
Postgres / NATS needed for the gate itself.
"""

from __future__ import annotations

import hmac as hmac_mod

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from legba.data.registry.api import (
    API_TOKEN_ENV,
    DEV_MODE_ENV,
    MISCONFIGURED_AUTH_DETAIL,
    _authorize_ws_token,
    require_bearer,
)

from .test_registry_api_unit import _build_app

_GUARDED_PATH = "/api/v1/registry/descriptors/target/anything"
_WS_PATH = "/api/v1/registry/events?filter=>"


# ---------------------------------------------------------------------------
# HTTP: fail-closed when unconfigured
# ---------------------------------------------------------------------------


def test_unset_token_without_dev_flag_returns_503(monkeypatch):
    app, _ = _build_app(token=None)  # pops API_TOKEN_ENV
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    client = TestClient(app)
    r = client.get(_GUARDED_PATH)
    assert r.status_code == 503
    assert "misconfigured" in r.json()["detail"]
    # With a bearer header too — still 503, the gate is down entirely.
    r = client.get(_GUARDED_PATH, headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503


def test_unset_token_with_dev_flag_permits(monkeypatch):
    app, _ = _build_app(token=None)
    monkeypatch.setenv(DEV_MODE_ENV, "1")
    client = TestClient(app)
    r = client.get(_GUARDED_PATH)
    assert r.status_code == 200
    r = client.get(_GUARDED_PATH, headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 200


def test_dev_flag_other_values_do_not_enable_dev_mode(monkeypatch):
    app, _ = _build_app(token=None)
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv(DEV_MODE_ENV, value)
        client = TestClient(app)
        assert client.get(_GUARDED_PATH).status_code == 503, value


# ---------------------------------------------------------------------------
# HTTP: configured token enforced (dev flag must NOT weaken it)
# ---------------------------------------------------------------------------


def test_wrong_token_rejected_even_with_dev_flag(monkeypatch):
    app, _ = _build_app(token="b2-secret-token")
    monkeypatch.setenv(DEV_MODE_ENV, "1")
    client = TestClient(app)
    r = client.get(_GUARDED_PATH, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 403
    r = client.get(_GUARDED_PATH)
    assert r.status_code == 401


def test_correct_token_accepted_without_dev_flag(monkeypatch):
    app, _ = _build_app(token="b2-secret-token")
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    client = TestClient(app)
    r = client.get(
        _GUARDED_PATH, headers={"Authorization": "Bearer b2-secret-token"},
    )
    assert r.status_code == 200


def test_token_comparison_goes_through_compare_digest(monkeypatch):
    """Pin the constant-time path: the gate must compare via
    `hmac.compare_digest`, not `==`."""
    calls: list[tuple[bytes, bytes]] = []
    real = hmac_mod.compare_digest

    def spy(a, b):
        calls.append((bytes(a), bytes(b)))
        return real(a, b)

    monkeypatch.setenv(API_TOKEN_ENV, "b2-ct-token")
    monkeypatch.setattr(hmac_mod, "compare_digest", spy)
    principal = require_bearer(authorization="Bearer b2-ct-token")
    assert principal == "b2-ct-token"
    assert calls == [(b"b2-ct-token", b"b2-ct-token")]

    calls.clear()
    with pytest.raises(HTTPException) as exc:
        require_bearer(authorization="Bearer wrong")
    assert exc.value.status_code == 403
    assert calls == [(b"wrong", b"b2-ct-token")]


def test_require_bearer_503_when_unconfigured(monkeypatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    with pytest.raises(HTTPException) as exc:
        require_bearer(authorization="Bearer anything")
    assert exc.value.status_code == 503
    assert exc.value.detail == MISCONFIGURED_AUTH_DETAIL


# ---------------------------------------------------------------------------
# WebSocket ?token= path — identical posture
# ---------------------------------------------------------------------------


def test_ws_unset_token_without_dev_flag_closes_1011(monkeypatch):
    app, _ = _build_app(token=None)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_WS_PATH) as ws:
            ws.receive_json()
    assert exc.value.code == 1011
    reason = getattr(exc.value, "reason", "") or ""
    if reason:  # reason propagation depends on the starlette version
        assert "misconfigured" in reason


def test_ws_wrong_token_closes_1008_when_enforced(monkeypatch):
    app, _ = _build_app(token="b2-ws-token")
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    client = TestClient(app)
    for url in (_WS_PATH, _WS_PATH + "&token=wrong"):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(url) as ws:
                ws.receive_json()
        assert exc.value.code == 1008, url


def test_ws_authorize_fn_fail_closed_and_constant_time(monkeypatch):
    # Unconfigured, no dev flag → 503.
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    with pytest.raises(HTTPException) as exc:
        _authorize_ws_token("anything")
    assert exc.value.status_code == 503

    # Unconfigured + dev flag → permitted, anonymous principal.
    monkeypatch.setenv(DEV_MODE_ENV, "1")
    assert _authorize_ws_token(None) == "anonymous"
    assert _authorize_ws_token("whatever") == "whatever"

    # Configured → enforced through hmac.compare_digest, dev flag or not.
    calls: list[tuple[bytes, bytes]] = []
    real = hmac_mod.compare_digest

    def spy(a, b):
        calls.append((bytes(a), bytes(b)))
        return real(a, b)

    monkeypatch.setenv(API_TOKEN_ENV, "b2-ws-ct")
    monkeypatch.setattr(hmac_mod, "compare_digest", spy)
    assert _authorize_ws_token("b2-ws-ct") == "b2-ws-ct"
    assert calls == [(b"b2-ws-ct", b"b2-ws-ct")]
    with pytest.raises(HTTPException) as exc:
        _authorize_ws_token("nope")
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# WebSocket Authorization: Bearer header path (ITEM 2.5 — Caddy injects the
# bearer on the proxied upgrade for the canonical deployment).
# ---------------------------------------------------------------------------


def test_ws_header_bearer_accepted_when_no_query_token(monkeypatch):
    """A header-only Bearer credential authenticates the WS upgrade — the
    Caddy reverse-proxy path where the operator's browser never carries the
    `?token=` query param."""
    monkeypatch.setenv(API_TOKEN_ENV, "b2-ws-header")
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    assert (
        _authorize_ws_token(None, "Bearer b2-ws-header") == "b2-ws-header"
    )


def test_ws_header_bearer_wrong_token_rejected(monkeypatch):
    """A header Bearer with the wrong secret is rejected 403, just like the
    query path."""
    monkeypatch.setenv(API_TOKEN_ENV, "b2-ws-header")
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    with pytest.raises(HTTPException) as exc:
        _authorize_ws_token(None, "Bearer wrong")
    assert exc.value.status_code == 403
    # Malformed header (not a Bearer credential) → no token → 403.
    with pytest.raises(HTTPException) as exc:
        _authorize_ws_token(None, "Basic Zm9vOmJhcg==")
    assert exc.value.status_code == 403


def test_ws_query_token_still_works_alongside_header(monkeypatch):
    """`?token=` keeps working unchanged, and is preferred when both are
    supplied (the query token is what the SPA controls)."""
    monkeypatch.setenv(API_TOKEN_ENV, "b2-ws-both")
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    # Query alone, no header at all.
    assert _authorize_ws_token("b2-ws-both") == "b2-ws-both"
    assert _authorize_ws_token("b2-ws-both", None) == "b2-ws-both"
    # Both present, both valid → query wins (returned principal).
    assert (
        _authorize_ws_token("b2-ws-both", "Bearer b2-ws-both") == "b2-ws-both"
    )
    # Query present + valid, header garbage → query still authenticates.
    assert (
        _authorize_ws_token("b2-ws-both", "Bearer wrong") == "b2-ws-both"
    )


def test_ws_header_bearer_503_when_unconfigured(monkeypatch):
    """The header path is fail-closed too: unconfigured + no dev flag → 503
    even with a well-formed header."""
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    with pytest.raises(HTTPException) as exc:
        _authorize_ws_token(None, "Bearer anything")
    assert exc.value.status_code == 503
