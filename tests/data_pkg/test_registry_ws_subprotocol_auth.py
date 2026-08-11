# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The events WebSocket credential must not travel in the URL.

`/api/v1/registry/events?token=...` put LEGBA_REGISTRY_API_TOKEN — the admin
credential for the whole registry API, byte-identical — into a URL. Browsers
print that URL verbatim in console warnings when an upgrade fails, keep it in
history/referrer surfaces, and every proxy that logs a request line writes it
to disk. The gallery pass caught it on screen.

A browser cannot set `Authorization` on a WS upgrade, but it CAN offer
subprotocols, and those travel as a header. So the credential moves to
`Sec-WebSocket-Protocol: legba.bearer.v1, <base64url>`; base64url (unpadded)
keeps any secret inside RFC 6455's subprotocol `token` grammar, and the server
echoes back the scheme name only.

`?token=` still authenticates during the rollout window — a stale SPA build
must not hard-break the moment the server rolls — but every use logs a
deprecation warning naming the path.
"""

from __future__ import annotations

import base64
import logging

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from legba.data.registry.api import (
    DEV_MODE_ENV,
    WS_BEARER_SUBPROTOCOL,
    _authorize_ws_token,
    _bearer_from_subprotocol,
)

from .test_registry_api_unit import _build_app

_WS_PATH = "/api/v1/registry/events?filter=>"
_TOKEN = "ws-subproto-token"


def _offer(token: str) -> str:
    encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")
    return f"{WS_BEARER_SUBPROTOCOL}, {encoded.rstrip('=')}"


def _subprotocols(token: str) -> list[str]:
    """What the browser puts in `Sec-WebSocket-Protocol` — the client's shape."""
    encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")
    return [WS_BEARER_SUBPROTOCOL, encoded.rstrip("=")]


class _FakeSub:
    async def unsubscribe(self) -> None:
        return None


class _FakeNatsConn:
    """The two calls the events route makes on a connection, in order.

    ``flush`` is not optional padding. The route SUBSCRIBES and then FLUSHES
    before it sends the hello frame, because ``subscribe()`` only queues the
    SUB line into the client's write buffer — anything published before the
    broker registers it is not delayed, it is LOST, and the hello frame is the
    only readiness signal a client gets. A fake that answers ``subscribe`` and
    not ``flush`` is not a smaller real client, it is a DIFFERENT one: when the
    flush landed in ``src/legba/data/registry/api.py`` (a9a762fc) this fake
    stayed at yesterday's surface and both live-endpoint tests below started
    dying inside the route with ``AttributeError``, in BOTH orders, every
    night. Recording the calls rather than merely tolerating them turns that
    silence into an assertion: :func:`_ws_call_log` is what the tests read.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def subscribe(self, _subject: str, cb=None):  # noqa: ANN001
        self.calls.append("subscribe")
        return _FakeSub()

    async def flush(self) -> None:
        self.calls.append("flush")


class _FakeNatsStore:
    """Minimum surface the events route touches past the auth gate.

    The stub app ships `nats_store=None`, which closes 1011 right after a
    SUCCESSFUL auth — that would hide the accept handshake, and the
    subprotocol echo is exactly the part a real browser hard-fails on.
    """

    def __init__(self) -> None:
        self.nc = _FakeNatsConn()
        self.cfg = None


def _app_with_nats(token: str | None):
    """The stub app plus the fake NATS store, returned so a test can read the
    call log the route left on it."""
    app, deps = _build_app(token=token)
    store = _FakeNatsStore()
    deps.nats_store = store  # type: ignore[assignment]
    app.state._ws_fake_nats = store
    return app


def _ws_call_log(app) -> list[str]:  # noqa: ANN001
    return list(app.state._ws_fake_nats.nc.calls)


# ---------------------------------------------------------------------------
# The credential parser
# ---------------------------------------------------------------------------


def test_subprotocol_offer_round_trips_any_credential() -> None:
    """Including bytes that are illegal in a raw subprotocol token."""
    for raw in (_TOKEN, "a,b c=d/e+f", "ünïcode", "x" * 512):
        assert _bearer_from_subprotocol(_offer(raw)) == raw


def test_a_foreign_subprotocol_is_not_a_credential() -> None:
    """An unrelated negotiation falls through to the other auth paths."""
    assert _bearer_from_subprotocol(None) is None
    assert _bearer_from_subprotocol("") is None
    assert _bearer_from_subprotocol("graphql-ws") is None
    assert _bearer_from_subprotocol(WS_BEARER_SUBPROTOCOL) is None  # no value
    assert _bearer_from_subprotocol("other.scheme, abc") is None


def test_undecodable_offer_is_not_silently_treated_as_absent(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="legba.data.registry.api")
    assert _bearer_from_subprotocol(f"{WS_BEARER_SUBPROTOCOL}, not!base64!") is None
    assert any(
        "subprotocol_undecodable" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_subprotocol_wins_and_the_older_paths_are_untouched(monkeypatch) -> None:
    """The new credential sits ON TOP of the existing ladder.

    Below it the original query-then-header order is deliberately unchanged —
    this commit adds a path, it does not re-rank the two that were there
    (``test_registry_auth_fail_closed`` pins that order).
    """
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", _TOKEN)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)

    # Subprotocol alone authenticates.
    assert _authorize_ws_token(None, None, _offer(_TOKEN)) == _TOKEN
    # And WINS over the other two, even when they disagree.
    assert _authorize_ws_token(
        "wrong-query", "Bearer wrong-header", _offer(_TOKEN),
    ) == _TOKEN
    # Header bearer still works (Caddy injects it on the proxied upgrade).
    assert _authorize_ws_token(None, f"Bearer {_TOKEN}", None) == _TOKEN
    # Query token still works — the deprecation window.
    assert _authorize_ws_token(_TOKEN, None, None) == _TOKEN


def test_query_token_use_logs_a_deprecation_warning(monkeypatch, caplog) -> None:
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", _TOKEN)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    caplog.set_level(logging.WARNING, logger="legba.data.registry.api")

    _authorize_ws_token(_TOKEN, None, None)

    assert any(
        "deprecated_query_token" in r.getMessage() for r in caplog.records
    ), "the query-token path must announce itself while it still exists"


def test_sse_query_token_is_not_deprecated(monkeypatch, caplog) -> None:
    """The consult SSE relay shares this gate and has no replacement.

    `EventSource` can set neither headers nor subprotocols, so its `?token=`
    is not going anywhere — warning about it would be a false alarm that reads
    exactly like a stale UI build.
    """
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", _TOKEN)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    caplog.set_level(logging.WARNING, logger="legba.data.registry.api")

    assert _authorize_ws_token(_TOKEN, None, surface="sse") == _TOKEN

    assert not any(
        "deprecated_query_token" in r.getMessage() for r in caplog.records
    )


def test_subprotocol_auth_logs_no_deprecation(monkeypatch, caplog) -> None:
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", _TOKEN)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    caplog.set_level(logging.WARNING, logger="legba.data.registry.api")

    _authorize_ws_token(None, None, _offer(_TOKEN))

    assert not any(
        "deprecated_query_token" in r.getMessage() for r in caplog.records
    )


def test_a_wrong_subprotocol_credential_is_rejected(monkeypatch) -> None:
    from fastapi import HTTPException

    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", _TOKEN)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)

    with pytest.raises(HTTPException) as exc:
        _authorize_ws_token(None, None, _offer("not-the-token"))
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# The live endpoint
# ---------------------------------------------------------------------------


def test_ws_accepts_subprotocol_auth_and_echoes_only_the_scheme(monkeypatch) -> None:
    """The connection opens with NO credential in the URL.

    RFC 6455 requires the server to echo one offered subprotocol; it must echo
    the scheme name, never the credential half.
    """
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    app = _app_with_nats(_TOKEN)
    client = TestClient(app)

    with client.websocket_connect(
        _WS_PATH, subprotocols=_subprotocols(_TOKEN),
    ) as ws:
        assert ws.accepted_subprotocol == WS_BEARER_SUBPROTOCOL
        assert ws.receive_json()["type"] == "subscribed"
    # The hello frame means "I am receiving", so the SUB must have reached the
    # broker before it was sent — subscribe, then flush, then hello.
    assert _ws_call_log(app) == ["subscribe", "flush"]


def test_ws_rejects_a_connection_with_no_credential_at_all(monkeypatch) -> None:
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    client = TestClient(_app_with_nats(_TOKEN))

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_WS_PATH) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_ws_rejects_a_wrong_subprotocol_credential(monkeypatch) -> None:
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    client = TestClient(_app_with_nats(_TOKEN))

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            _WS_PATH, subprotocols=_subprotocols("not-the-token"),
        ) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_ws_still_accepts_the_deprecated_query_token(monkeypatch) -> None:
    """The rollout window: a stale UI build must not hard-break on deploy."""
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    app = _app_with_nats(_TOKEN)
    client = TestClient(app)

    with client.websocket_connect(f"{_WS_PATH}&token={_TOKEN}") as ws:
        assert ws.receive_json()["type"] == "subscribed"
        # No subprotocol was offered, so none is negotiated.
        assert not ws.accepted_subprotocol
    assert _ws_call_log(app) == ["subscribe", "flush"]
