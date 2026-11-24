# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W4 TOKEN-FIX — the registry bearer token is never an actor.

Until W4 ``require_bearer`` returned the PRESENTED TOKEN as the principal and
every handler stamped it: ``descriptor_audit_log.actor_id``,
``consult_sessions.principal``, ``journal_proposals.decided_by`` held the live
secret, and the Audit-Chain Browser rendered it in its "by actor" chips. The
gate now returns ``bearer:<sha256(token)[:12]>``. This file pins:

  * the label's shape, its stability across requests, and that it is never
    equal to (or contains) the configured token — HTTP gate, WS/SSE gate, dev
    mode;
  * through the REAL binding path (the registry router over ASGI): a mutating
    route stamps the label, never the token, and the same label twice;
  * a static tree guard: no registry module can get at the raw credential
    except through the ``_deps`` gates (which only ever hand back the label);
  * the historical scrub (``scripts/scrub_audit_actor_tokens.py``) on the real
    driver: seeded rows carrying a FAKE token are re-stamped to the label, the
    audit rows' re-signatures verify, a row signed under another key is
    relabelled without minting a valid signature for it, ONE
    ``scrub_actor_token`` audit row carries counts (never the value), dry-run
    changes nothing, and a re-run is a no-op.

Every token here is fake and generated per test.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
from nacl.signing import SigningKey
from starlette.testclient import TestClient

from legba.data.registry._deps import (
    API_TOKEN_ENV,
    DEV_MODE_ENV,
    SERVICE_TOKEN_ENV,
    _authorize_ws_token,
    principal_actor,
    principal_label,
    require_bearer,
)
from legba.data.registry.audit import AuditLogger, signed_payload_for_row
from legba.data.registry.signing import SigningIdentity, verify_audit_payload

from .test_registry_api_unit import _build_app, _sample_target_body

_REPO = Path(__file__).resolve().parents[2]
_REGISTRY_PKG = _REPO / "src" / "legba" / "data" / "registry"
_LABEL_RE = re.compile(r"^bearer:[0-9a-f]{12}$")
#: X2-B1: what the gates stamp — the principal kind before the hashed label.
_ACTOR_RE = re.compile(r"^(service|operator):bearer:[0-9a-f]{12}$")


def _fake_token() -> str:
    # Same SHAPE as a real deployment token (64 hex) — generated, never real.
    return uuid4().hex + uuid4().hex


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def test_label_shape_stable_and_not_the_token():
    tok = _fake_token()
    label = principal_label(tok)
    assert _LABEL_RE.match(label), label
    assert label == principal_label(tok)  # deterministic
    assert tok not in label and label != tok
    assert principal_label(_fake_token()) != label  # distinct credentials


def test_http_gate_returns_label_never_token(monkeypatch):
    tok = _fake_token()
    monkeypatch.setenv(API_TOKEN_ENV, tok)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    first = require_bearer(authorization=f"Bearer {tok}")
    second = require_bearer(authorization=f"Bearer {tok}")
    assert first == second == principal_actor("operator", tok)
    assert _ACTOR_RE.match(first), first
    assert first != tok and tok not in first


def test_ws_and_sse_gate_return_label_never_token(monkeypatch):
    tok = _fake_token()
    monkeypatch.setenv(API_TOKEN_ENV, tok)
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    for got in (
        _authorize_ws_token(None, f"Bearer {tok}"),
        _authorize_ws_token(tok, None, surface="sse"),
    ):
        assert got == principal_actor("operator", tok)
        assert tok not in got


def test_dev_mode_keeps_anonymous_and_never_echoes_a_bearer(monkeypatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.setenv(DEV_MODE_ENV, "1")
    assert require_bearer(authorization=None) == "anonymous"
    assert _authorize_ws_token(None) == "anonymous"
    tok = _fake_token()
    assert require_bearer(authorization=f"Bearer {tok}") == principal_label(tok)
    assert _authorize_ws_token(tok) == principal_label(tok)


# ---------------------------------------------------------------------------
# Through the real binding path — a mutating route's stamped actor
# ---------------------------------------------------------------------------


def test_registry_route_stamps_label_not_token(monkeypatch):
    tok = _fake_token()
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    app, deps = _build_app(token=tok)
    try:
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {tok}"}
        for i in range(2):
            r = client.post(
                "/api/v1/registry/descriptors/target",
                json=_sample_target_body(f"t_tokenfix_{i}"),
                headers=headers,
            )
            assert r.status_code == 201, r.text
        actors = [
            call[1][1] for call in deps.descriptor_registry.calls
            if call[0] == "register"
        ]
        assert len(actors) == 2
        assert actors[0] == actors[1] == principal_actor("operator", tok)
        assert all(tok not in a for a in actors)
    finally:
        monkeypatch.delenv(API_TOKEN_ENV, raising=False)


# ---------------------------------------------------------------------------
# Static tree guard — only the _deps gates touch the raw credential
# ---------------------------------------------------------------------------

#: Modules allowed to hold the raw credential, and why. X4 GATE: the bearer
#: MIDDLEWARE is the one place a request's Authorization header is read; the
#: _deps kernel is the resolver it (and the gates' no-middleware fallback)
#: hands it to. rate_limit.py left the list (it keys on the stamped actor) and
#: session_auth.py never joined it (it reads the middleware's presented flag).
_CREDENTIAL_READERS = {
    "_deps.py": "the gate kernel — resolves the credential, returns principal_actor()",
    "bearer_auth.py": "the bearer middleware — reads the header ONCE, hands it to _deps",
}

#: Modules allowed to declare an ``authorization`` Header parameter: they hand
#: it straight to ``_deps._authorize_ws_token`` (label or raise), nothing else.
_HEADER_PASSTHROUGH = {"api.py", "consult_stream_api.py"}


def _literal_text(node: ast.AST) -> str | None:
    """A str OR bytes constant's text (W11-F77: a bytes read of
    ``b"authorization"`` off the ASGI scope was invisible to a str-only scan)."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        if isinstance(node.value, bytes):
            return node.value.decode("latin-1")
    return None


def test_no_registry_module_reads_the_raw_credential():
    offenders: list[str] = []
    for path in sorted(_REGISTRY_PKG.glob("*.py")):
        name = path.name
        src = path.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # request.headers.get("authorization") / headers["authorization"] /
            # an ASGI scope header compared to b"authorization"
            text = _literal_text(node)
            if text is not None:
                if text.lower() == "authorization" and name not in _CREDENTIAL_READERS:
                    offenders.append(f"{name}:{node.lineno} reads the authorization header")
                if text in (API_TOKEN_ENV, SERVICE_TOKEN_ENV) and (
                    name not in _CREDENTIAL_READERS
                ):
                    offenders.append(f"{name}:{node.lineno} reads {text}")
            # def route(..., authorization: str | None = Header(...))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.args + node.args.kwonlyargs:
                    if arg.arg == "authorization" and name not in (
                        set(_CREDENTIAL_READERS) | _HEADER_PASSTHROUGH
                    ):
                        offenders.append(
                            f"{name}:{node.lineno} {node.name}() takes the raw "
                            "authorization header"
                        )
    assert not offenders, (
        "a registry module can reach the raw bearer credential outside the "
        "_deps gates — stamp `Depends(require_bearer)` (a hashed label) instead:\n"
        + "\n".join(offenders)
    )


def test_the_guard_sees_bytes_literals():
    """W11-F77: the scan must flag a BYTES read of the header — the shape
    ``session_auth`` used (``key.lower() == b"authorization"``) before X4."""
    tree = ast.parse('def f(scope):\n    return [v for k, v in scope if k == b"authorization"]\n')
    hits = [n for n in ast.walk(tree) if (_literal_text(n) or "").lower() == "authorization"]
    assert len(hits) == 1


def test_bearer_middleware_is_the_one_request_header_reader():
    """X4 GATE: exactly one place in the registry reads a request's
    Authorization header off the scope or the request — the bearer middleware.
    ``_deps`` receives it (a ``Header`` parameter of the fallback gate, the
    resolvers' argument), never reads it off a request itself."""
    readers = []
    for path in sorted(_REGISTRY_PKG.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (_literal_text(node) or "").lower() == "authorization":
                readers.append(path.name)
    assert sorted(set(readers)) == ["bearer_auth.py"], readers


def test_passthrough_modules_only_hand_the_header_to_the_gate():
    for name in _HEADER_PASSTHROUGH:
        src = (_REGISTRY_PKG / name).read_text()
        # Every use of the `authorization` name sits on a line that declares it
        # or passes it into _authorize_ws_token (possibly across a wrapped call).
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(
                isinstance(a, ast.Name) and a.id == "authorization" for a in node.args
            ):
                fn = node.func
                fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                assert fname == "_authorize_ws_token", (
                    f"{name}:{node.lineno} passes the raw authorization header to "
                    f"{fname}() — only the _deps gate may see it"
                )


# ---------------------------------------------------------------------------
# The historical scrub on the real driver
# ---------------------------------------------------------------------------


def _load_scrub_module():
    path = _REPO / "scripts" / "scrub_audit_actor_tokens.py"
    spec = importlib.util.spec_from_file_location("scrub_audit_actor_tokens", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _identity(did: str) -> SigningIdentity:
    return SigningIdentity(signing_key=SigningKey.generate(), signer_did=did)


async def _seed(conn: asyncpg.Connection, tok: str, ident: SigningIdentity,
                other: SigningIdentity, tag: str) -> dict:
    logger = AuditLogger(ident)
    ids = {}
    e1 = await logger.record(
        conn, actor_id=tok, namespace="target", descriptor_id=f"{tag}_a",
        action="register", to_version="v1", change_summary={"k": 1, "nested": {"z": [1, 2]}},
    )
    e2 = await logger.record(
        conn, actor_id=tok, namespace="analyst", descriptor_id=f"{tag}_b",
        action="update", from_version="v1", to_version="v2",
    )
    # Signed under ANOTHER key: its signature never verified under `ident`.
    e3 = await AuditLogger(other).record(
        conn, actor_id=tok, namespace="target", descriptor_id=f"{tag}_c",
        action="retire",
    )
    # A different actor — must be left alone.
    e4 = await logger.record(
        conn, actor_id="operator", namespace="target", descriptor_id=f"{tag}_d",
        action="register",
    )
    ids["audit"] = [e1.id, e2.id, e3.id, e4.id]
    ids["session"] = await conn.fetchval(
        "INSERT INTO consult_sessions (mode, title, principal) VALUES ('chat', $1, $2) "
        "RETURNING id", tag, tok,
    )
    ids["proposal"] = await conn.fetchval(
        "INSERT INTO journal_proposals (proposal_kind, proposed_by_analyst_id, status, "
        "decided_by) VALUES ('tokenfix_test', $1, 'accepted', $2) RETURNING id",
        tag, tok,
    )
    ids["label_row"] = await conn.fetchval(
        "INSERT INTO unit_reference_labels (unit_analyst_id, reference_answer, labeled_by) "
        "VALUES ($1, 'x', $2) RETURNING id", tag, tok,
    )
    return ids


async def _audit_rows(conn, ids):
    return {
        r["id"]: r for r in await conn.fetch(
            "SELECT * FROM descriptor_audit_log WHERE id = ANY($1::uuid[])", ids,
        )
    }


async def _scrub_rows(conn, label):
    return await conn.fetch(
        "SELECT * FROM descriptor_audit_log WHERE action = 'scrub_actor_token' "
        "AND change_summary->>'label' = $1", label,
    )


async def test_scrub_script_redacts_through_the_chain_and_is_idempotent(migrated_pg):
    """Since migration 0248 the scrub never re-signs: every matched audit row
    is REDACTED by an appended, chained, signed ``redact`` event; the original
    row_hash stays in the chain and the row reads ``redacted``."""
    from legba.data.registry.audit_chain import SIG_REDACTED, verify_audit_chain

    scrub = _load_scrub_module()
    tok = _fake_token()
    label = principal_label(tok)
    tag = f"tokenfix_{uuid4().hex[:8]}"
    ident = _identity("did:legba:registry:tokenfix-test")
    other = _identity("did:legba:registry:other-key")

    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        ids = await _seed(conn, tok, ident, other, tag)
        before = await _audit_rows(conn, ids["audit"])

        # ---- dry-run: exact counts, nothing written ----
        dry = await scrub.run(conn, token=tok, identity=ident, apply=False)
        assert dry["applied"] is False
        assert dry["label"] == label
        assert dry["audit"] == {"matched": 3, "redacted": 0, "unchained_rows": 0}
        assert dry["tables"]["consult_sessions.principal"] == 1
        assert dry["tables"]["journal_proposals.decided_by"] == 1
        assert dry["tables"]["unit_reference_labels.labeled_by"] == 1
        assert dry["total"] == 6
        assert tok not in json.dumps(dry, default=str)
        assert tok not in scrub._render(dry)
        after_dry = await _audit_rows(conn, ids["audit"])
        assert {k: dict(v) for k, v in after_dry.items()} == {
            k: dict(v) for k, v in before.items()
        }
        assert await conn.fetchval(
            "SELECT principal FROM consult_sessions WHERE id=$1", ids["session"],
        ) == tok
        assert not await _scrub_rows(conn, label)

        # ---- apply ----
        res = await scrub.run(conn, token=tok, identity=ident, apply=True)
        assert res["audit"] == {"matched": 3, "redacted": 3, "unchained_rows": 0}
        assert res["total"] == 6
        rows = await _audit_rows(conn, ids["audit"])
        a, b, c, d = (rows[i] for i in ids["audit"])
        for r in (a, b, c):
            assert r["actor_id"] == label
            # Never re-signed; the chain columns never move.
            assert bytes(r["signed_payload"]) == bytes(before[r["id"]]["signed_payload"])
            assert r["row_hash"] == before[r["id"]]["row_hash"] is not None
            assert await conn.fetchval(
                "SELECT count(*) FROM descriptor_audit_log WHERE action = 'redact' "
                "AND change_summary->>'redacts_row_id' = $1 "
                "AND change_summary->>'redacted_row_hash' = $2",
                str(r["id"]), r["row_hash"]) == 1
        # Untouched actor.
        assert d["actor_id"] == "operator"
        assert bytes(d["signed_payload"]) == bytes(before[d["id"]]["signed_payload"])
        rep = await verify_audit_chain(conn, verify_key=ident.verify_key)
        assert rep.ok, rep.first_break
        assert {str(a["id"]), str(b["id"]), str(c["id"])} <= set(rep.redacted_ids)
        assert rep.signatures.get(SIG_REDACTED, 0) >= 3

        assert await conn.fetchval(
            "SELECT principal FROM consult_sessions WHERE id=$1", ids["session"],
        ) == label
        assert await conn.fetchval(
            "SELECT decided_by FROM journal_proposals WHERE id=$1", ids["proposal"],
        ) == label
        assert await conn.fetchval(
            "SELECT labeled_by FROM unit_reference_labels WHERE id=$1", ids["label_row"],
        ) == label

        # ONE signed scrub row, counts only, never the value.
        scrub_rows = await _scrub_rows(conn, label)
        assert len(scrub_rows) == 1
        sr = scrub_rows[0]
        assert str(sr["id"]) == res["scrub_row_id"]
        assert sr["actor_id"] == scrub.SCRUB_ACTOR
        summary = json.loads(sr["change_summary"]) if isinstance(
            sr["change_summary"], str) else sr["change_summary"]
        assert summary["audit_rows_redacted"] == 3
        assert summary["total"] == 6
        assert tok not in json.dumps(summary)
        assert verify_audit_payload(
            ident.verify_key, signed_payload_for_row(sr), bytes(sr["signed_payload"]),
        )

        # No column anywhere in the seeded rows still holds the token.
        assert await conn.fetchval(
            "SELECT count(*) FROM descriptor_audit_log WHERE actor_id = $1", tok,
        ) == 0

        # ---- idempotent: nothing left, nothing written ----
        again = await scrub.run(conn, token=tok, identity=ident, apply=True)
        assert again["total"] == 0
        assert again["scrub_row_id"] is None
        assert len(await _scrub_rows(conn, label)) == 1
    finally:
        await conn.close()
