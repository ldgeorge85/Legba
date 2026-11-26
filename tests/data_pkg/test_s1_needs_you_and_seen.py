# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The phone's "what needs me" on the server (plan 4.1-4.3).

  * ``GET /v3/since?include=needs_you`` — the alert LOG's open episodes that
    paged (paged-only), opened or paged since the cursor;
    world kinds only below admin; capped with ``total`` / ``truncated``.
    Without ``include`` the response is the pre-change envelope byte for byte.
  * ``GET /v3/since/counts`` — the same counts without rows, reused 30 s.
  * ``GET/PUT /v3/me/seen`` — the signed-in person's own cursor; 404 with the
    sign-in switch off or for a credential with no person behind it.

PURE tests need no database; the rest run over the migrated test DB through
real HTTP (the ``test_v3_since_api`` and ``test_signin_a1_live_app`` shapes).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

import legba.data.registry.me_seen_api as me_seen_api
import legba.data.registry.since_api as since_api
from legba.data import users as U
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry import route_policy as rp
from legba.data.registry import route_roles as rr
from legba.data.registry import session_auth as sa
from legba.data.registry.alert_log_api import (
    NeedsYouEpisode,
    needs_you_item,
    needs_you_kinds,
)
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.shared import auth_signin as S

_REPO = pathlib.Path(__file__).resolve().parents[2]
_MIGRATION = _REPO / "src" / "legba" / "data" / "migrations" / "0259_user_seen_cursors.sql"

os.environ.setdefault(
    MASTER_KEY_ENV, "0011223344556677889900112233445566778899001122334455667788990011",
)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)

#: The /since envelope's keys, in order, as they were before. A caller
#: that does not ask for ``needs_you`` must see exactly these.
PRE_S1_SINCE_KEYS = [
    "cursor", "server_now", "counts", "new_findings", "superseded",
    "band_changes", "situations", "alerts", "forecasts_due",
]
PRE_S1_COUNT_KEYS = [
    "new_findings", "superseded", "band_changes", "situations", "alerts",
    "forecasts_due",
]
NEEDS_YOU_ITEM_KEYS = [
    "id", "kind", "group", "topic", "severity", "summary", "subject_kind",
    "subject_id", "opened_at", "last_raised_at", "last_paged_at",
    "paged_severity", "why", "acked_at", "snoozed_until",
]


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def test_routes_registered() -> None:
    paths = {
        (m, r.path)
        for r in since_api.build_since_router(deps=object()).routes  # type: ignore[arg-type]
        for m in r.methods  # type: ignore[attr-defined]
    }
    assert ("GET", "/since") in paths and ("GET", "/since/counts") in paths
    seen = {
        (m, r.path)
        for r in me_seen_api.build_me_seen_router(deps=object()).routes  # type: ignore[arg-type]
        for m in r.methods  # type: ignore[attr-defined]
    }
    assert seen == {("GET", "/me/seen"), ("PUT", "/me/seen")}


def test_route_roles_declared() -> None:
    v3 = "/api/v1/v3"
    assert rp.ROUTE_MIN_ROLES[("GET", f"{v3}/since")] == S.ROLE_READER
    assert rp.ROUTE_MIN_ROLES[("GET", f"{v3}/since/counts")] == S.ROLE_READER
    assert rp.ROUTE_MIN_ROLES[("GET", f"{v3}/me/seen")] == S.ROLE_READER
    assert rp.ROUTE_MIN_ROLES[("PUT", f"{v3}/me/seen")] == S.ROLE_READER
    # Switch off (the October table): the write is an own-rows write.
    assert rr.ROUTE_ROLES[("PUT", f"{v3}/me/seen")] == rr.ROLE_READER_OWN_ROWS
    assert rr.required_role_for("GET", f"{v3}/since/counts") == rr.ROLE_READ


def test_slim_imports() -> None:
    """The three modules import with the heavy stack poisoned and pull in no
    analyst / runtime package (the registry slim-image rule)."""
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.registry.since_api
        import legba.data.registry.me_seen_api
        import legba.data.registry.alert_log_api
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts")
                        or m.startswith("legba.runtime"))
        assert not leaked, leaked
        print("OK")
        """
    )
    src_root = str(pathlib.Path(since_api.__file__).resolve().parents[4])
    r = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (src_root, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


def test_since_envelope_fields_unchanged() -> None:
    assert list(since_api.SinceResponse.model_fields) == PRE_S1_SINCE_KEYS


def test_counts_response_shape() -> None:
    assert list(since_api.SinceCountsResponse.model_fields) == [
        "cursor", "server_now", "counts", "needs_you", "changed", "forecasts_due",
    ]
    assert since_api.SINCE_COUNTS_TTL_S == 30.0
    assert since_api.CHANGED_SECTIONS == tuple(PRE_S1_COUNT_KEYS[:5])


def test_parse_include() -> None:
    assert since_api._parse_include(None) == frozenset()
    assert since_api._parse_include("needs_you") == {"needs_you"}
    assert since_api._parse_include(" needs_you , ") == {"needs_you"}
    for bad in ("bogus", "needs_you,alerts", "x" * 200):
        with pytest.raises(HTTPException) as exc:
            since_api._parse_include(bad)
        assert exc.value.status_code == 400


def test_needs_you_kinds_by_class() -> None:
    from legba.data.alerts.kinds import KINDS

    assert needs_you_kinds(is_admin=True) is None
    world = needs_you_kinds(is_admin=False)
    assert world and all(KINDS[k].topic == "world" for k in world)
    assert "source_degraded" not in world and "host_disk" not in world


def test_needs_you_item_shape() -> None:
    at = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)
    row = {
        "id": "e1", "kind": "verified_finding", "subject_kind": "target",
        "subject_id": "US", "severity": "high", "summary": "s",
        "opened_at": at, "last_raised_at": at, "last_paged_at": None,
        "paged_count": 0, "paged_severity": None,
    }
    item = needs_you_item(row)
    assert list(NeedsYouEpisode.model_fields) == NEEDS_YOU_ITEM_KEYS
    assert item.why == "not_paged" and item.topic == "world"  # never claims a page
    assert item.acked_at is None and item.snoozed_until is None
    assert needs_you_item({**row, "paged_count": 2, "last_paged_at": at}).why == "paged"


def test_seen_surfaces_match_migration_check() -> None:
    sql = _MIGRATION.read_text()
    m = re.search(r"surface IN \(([^)]*)\)", sql)
    assert m is not None
    in_sql = tuple(s.strip().strip("'") for s in m.group(1).split(","))
    assert in_sql == me_seen_api.SEEN_SURFACES


def test_validate_seen_bounds() -> None:
    now = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)
    surfaces, stamps = me_seen_api.validate_seen(
        {"watch": now - timedelta(hours=1), "changed": now + timedelta(seconds=30)},
        now=now,
    )
    assert surfaces == ["changed", "watch"]
    assert stamps == [now, now - timedelta(hours=1)]  # skew clamps to now
    naive = me_seen_api.validate_seen({"ledger": datetime(2026, 11, 14, 5)}, now=now)
    assert naive[1][0].tzinfo is not None
    for bad in (
        {"board": now},
        {"watch": now + timedelta(minutes=10)},
        {"watch": now - timedelta(days=91)},
    ):
        with pytest.raises(HTTPException) as exc:
            me_seen_api.validate_seen(bad, now=now)
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# App fixture (since + me/seen routers, no auth middleware — dev mode)
# ---------------------------------------------------------------------------


class _Frozen(datetime):
    """``datetime`` with a pinned ``now`` (so two calls share server_now)."""

    pinned: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.pinned


@pytest_asyncio.fixture
async def s1_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)
    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    nats_store = NatsStore(NatsConfig.from_env())
    await nats_store.connect()
    identity = SigningIdentity(
        signing_key=SigningKey(b"s1-needs-you-test-signing-seed-x"),
        signer_did="did:legba:registry:s1-test",
    )
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    registry = DescriptorRegistry(
        pg_store, nats_store=nats_store, vocabulary_cache=vocab,
        signing_identity=identity, audit_logger=audit, dead_letter=dlq,
    )
    await registry.start()
    deps = RegistryAPIDeps(
        descriptor_registry=registry,
        stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
        vault=vault, dlq=dlq, audit_logger=audit, vocabulary_cache=vocab,
        nats_store=nats_store, conversion_registry=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(since_api.build_since_router(deps), prefix="/api/v1/v3")
    app.include_router(me_seen_api.build_me_seen_router(deps), prefix="/api/v1/v3")
    since_api._COUNTS_CACHE.clear()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        yield client, pg_store
    since_api._COUNTS_CACHE.clear()
    await registry.stop()
    await nats_store.close()
    await pg_store.close()


async def _episode(
    pg: PostgresStore, *, kind: str, severity: str, opened_at: datetime,
    paged_at: datetime | None = None, state: str = "open",
) -> str:
    """One ``alerts`` row (migration 0250) with a unique subject."""
    subject = f"s1-{uuid.uuid4().hex[:10]}"
    async with pg.acquire() as conn:
        return str(await conn.fetchval(
            """
            INSERT INTO alerts (kind, subject_kind, subject_id, dedupe_key, state,
                                severity, max_severity, summary, raised_by,
                                opened_at, last_raised_at, closed_at,
                                close_reason, paged_count, paged_severity,
                                last_paged_at)
            VALUES ($1, 'target', $2, $3, $4, $5, $5, 's1 episode', 's1_test',
                    $6, $6, $7, $8, $9, $10, $11)
            RETURNING id
            """,
            kind, subject, f"{kind}:{subject}", state, severity, opened_at,
            opened_at if state == "closed" else None,
            "test" if state == "closed" else None,
            1 if paged_at else 0, severity if paged_at else None, paged_at,
        ))


def _ids(section: dict[str, Any]) -> set[str]:
    return {i["id"] for i in section["items"]}


# ---------------------------------------------------------------------------
# /since — existing callers byte-identical; needs_you section
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_without_include_is_the_pre_s1_bytes(s1_app, monkeypatch) -> None:
    client, pg = s1_app
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _Frozen.pinned = now
    monkeypatch.setattr(since_api, "datetime", _Frozen)
    cursor = now - timedelta(minutes=5)
    await _episode(pg, kind="verified_finding", severity="high",
                   opened_at=now - timedelta(minutes=1), paged_at=now - timedelta(minutes=1))
    params = {"cursor": cursor.isoformat()}

    plain = await client.get("/api/v1/v3/since", params=params)
    assert plain.status_code == 200, plain.text
    body = plain.json()
    assert list(body) == PRE_S1_SINCE_KEYS
    assert list(body["counts"]) == PRE_S1_COUNT_KEYS
    assert "needs_you" not in plain.text

    extended = await client.get(
        "/api/v1/v3/since", params={**params, "include": "needs_you"},
    )
    assert extended.status_code == 200, extended.text
    ext = extended.json()
    assert list(ext) == PRE_S1_SINCE_KEYS + ["needs_you"]
    assert list(ext["counts"]) == PRE_S1_COUNT_KEYS + ["needs_you"]
    # Removing the one section and its count gives the plain response's
    # EXACT bytes (FastAPI's JSONResponse rendering).
    ext.pop("needs_you")
    ext["counts"].pop("needs_you")
    rendered = json.dumps(
        ext, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":"),
    ).encode("utf-8")
    assert rendered == plain.content


@pytest.mark.integration
@pytest.mark.asyncio
async def test_needs_you_inclusion_rule_and_class(s1_app, monkeypatch) -> None:
    client, pg = s1_app
    now = datetime.now(timezone.utc)
    cursor = now - timedelta(minutes=30)
    before = cursor - timedelta(hours=2)
    after = now - timedelta(minutes=5)
    paged_world = await _episode(pg, kind="verified_finding", severity="high",
                                 opened_at=after, paged_at=after)
    medium_world = await _episode(pg, kind="volume_surge", severity="medium",
                                  opened_at=after)
    low_world = await _episode(pg, kind="baseline_deviation", severity="low",
                               opened_at=after)
    repaged = await _episode(pg, kind="escalations", severity="critical",
                             opened_at=before, paged_at=after)
    stale = await _episode(pg, kind="escalations", severity="high",
                           opened_at=before, paged_at=before)
    closed = await _episode(pg, kind="verified_finding", severity="high",
                            opened_at=after, paged_at=after, state="closed")
    engine = await _episode(pg, kind="source_degraded", severity="high",
                            opened_at=after, paged_at=after)
    params = {"cursor": cursor.isoformat(), "include": "needs_you"}

    # Below admin — the world's kinds only. (Dev mode stamps every caller
    # operator, so the class is set through the test override.)
    monkeypatch.setenv(rr.ROLE_OVERRIDE_ENV, "1")
    r = await client.get("/api/v1/v3/since", params=params,
                         headers={rr.ROLE_HEADER: "read"})
    assert r.status_code == 200, r.text
    ny = r.json()["needs_you"]
    ids = _ids(ny)
    assert {paged_world, repaged} <= ids
    # Paged-only — an open medium (or higher) episode that never
    # paged is NOT in needs-you.
    assert not ({medium_world, low_world, stale, closed, engine} & ids)
    assert ny["total"] >= 2 and ny["total"] == r.json()["counts"]["needs_you"]
    item = next(i for i in ny["items"] if i["id"] == paged_world)
    assert list(item) == NEEDS_YOU_ITEM_KEYS
    assert item["why"] == "paged" and item["acked_at"] is None
    assert all(i["why"] == "paged" for i in ny["items"])
    # Worst first.
    ranks = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    sev = [ranks[i["severity"]] for i in ny["items"]]
    assert sev == sorted(sev, reverse=True)

    # Admin class — the engine's kinds too.
    r = await client.get("/api/v1/v3/since", params=params,
                         headers={rr.ROLE_HEADER: "operator"})
    assert engine in _ids(r.json()["needs_you"])

    bad = await client.get("/api/v1/v3/since",
                           params={"cursor": cursor.isoformat(), "include": "bogus"})
    assert bad.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_needs_you_truncation_is_honest(s1_app, monkeypatch) -> None:
    client, pg = s1_app
    now = datetime.now(timezone.utc)
    for _ in range(2):
        await _episode(pg, kind="verified_finding", severity="high",
                       opened_at=now - timedelta(seconds=30),
                       paged_at=now - timedelta(seconds=30))
    monkeypatch.setattr(since_api, "SECTION_CAP", 1)
    r = await client.get("/api/v1/v3/since", params={
        "cursor": (now - timedelta(minutes=1)).isoformat(), "include": "needs_you",
    })
    ny = r.json()["needs_you"]
    assert len(ny["items"]) == 1 and ny["total"] >= 2 and ny["truncated"] is True


# ---------------------------------------------------------------------------
# /since/counts
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_counts_match_since_and_are_reused(s1_app, monkeypatch) -> None:
    client, pg = s1_app
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _Frozen.pinned = now
    monkeypatch.setattr(since_api, "datetime", _Frozen)
    await _episode(pg, kind="verified_finding", severity="high",
                   opened_at=now - timedelta(minutes=1), paged_at=now - timedelta(minutes=1))
    # SEAMS #93 closed: open at medium, never paged — in neither
    # the list nor the badge's count.
    unpaged = await _episode(pg, kind="volume_surge", severity="medium",
                             opened_at=now - timedelta(minutes=2))
    cursor = (now - timedelta(minutes=10)).isoformat()

    full = (await client.get("/api/v1/v3/since",
                             params={"cursor": cursor, "include": "needs_you"})).json()
    assert unpaged not in _ids(full["needs_you"])
    paged_listed = [i for i in full["needs_you"]["items"] if i["why"] == "paged"]
    assert len(paged_listed) == len(full["needs_you"]["items"]) == full["needs_you"]["total"]
    r = await client.get("/api/v1/v3/since/counts", params={"cursor": cursor})
    assert r.status_code == 200, r.text
    counts = r.json()
    assert list(counts) == [
        "cursor", "server_now", "counts", "needs_you", "changed", "forecasts_due",
    ]
    assert counts["counts"] == full["counts"]
    assert counts["needs_you"] == full["counts"]["needs_you"] >= 1
    assert counts["needs_you"] == len(paged_listed)   # the badge == the paged-only list
    assert counts["forecasts_due"] == full["counts"]["forecasts_due"]
    assert counts["changed"] == sum(
        full["counts"][k] for k in since_api.CHANGED_SECTIONS
    )
    # Reused inside the TTL: a new episode does not show until it expires.
    await _episode(pg, kind="verified_finding", severity="critical",
                   opened_at=now - timedelta(seconds=10), paged_at=now - timedelta(seconds=10))
    again = (await client.get("/api/v1/v3/since/counts", params={"cursor": cursor})).json()
    assert again == counts
    since_api._COUNTS_CACHE.clear()
    fresh = (await client.get("/api/v1/v3/since/counts", params={"cursor": cursor})).json()
    assert fresh["needs_you"] == counts["needs_you"] + 1

    assert (await client.get("/api/v1/v3/since/counts",
                             params={"cursor": "nope"})).status_code == 400


# ---------------------------------------------------------------------------
# /me/seen — switch off
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_me_seen_404_with_signin_off(s1_app, monkeypatch) -> None:
    client, _ = s1_app
    monkeypatch.delenv(S.SIGNIN_ENV, raising=False)
    for r in (
        await client.get("/api/v1/v3/me/seen"),
        await client.put("/api/v1/v3/me/seen", json={
            "seen": {"watch": datetime.now(timezone.utc).isoformat()},
        }),
    ):
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "seen_cursor_unavailable"
        assert r.json()["detail"]["reason"] == "signin_off"


# ---------------------------------------------------------------------------
# /me/seen — switch on, through the real app (middlewares + gate)
# ---------------------------------------------------------------------------

_DOMAIN = "s1seen.example"
ADMIN_TOK = "s1-admin-token-" + "7" * 24


@pytest_asyncio.fixture
async def seen_on(migrated_pg: PostgresConfig, monkeypatch):
    from legba.data.registry.server import create_app

    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{_DOMAIN}")
    monkeypatch.setenv(S.SIGNIN_ENV, "1")
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", ADMIN_TOK)
    monkeypatch.delenv("LEGBA_REGISTRY_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    monkeypatch.delenv(rr.ROLE_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(sa.COOKIE_INSECURE_ENV, raising=False)
    app = create_app(
        pg_config=migrated_pg, nats_config=NatsConfig.from_env(),
        enable_healthcheck_loop=False, enable_vocabulary_subscription=False,
    )
    store = app.state.registry_deps.descriptor_registry.pg
    await store.connect()
    cookies: dict[str, dict[str, str]] = {}
    async with pool.acquire() as conn:
        for name in ("ana", "ben"):
            uid = await conn.fetchval(
                "INSERT INTO users (email, role, status) "
                "VALUES ($1, 'reader', 'active') RETURNING id",
                f"{name}@{_DOMAIN}",
            )
            user = await U.get_user(conn, uid)
            session = await U.create_session(conn, user)
            cookies[name] = {
                "Cookie": f"{sa.SESSION_COOKIE_NAME}={session.token}",
                "Sec-Fetch-Site": "same-origin",
            }
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            yield c, cookies, pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{_DOMAIN}")
        await store.close()
        await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_me_seen_own_rows_with_signin_on(seen_on) -> None:
    c, cookies, pool = seen_on
    ana, ben = cookies["ana"], cookies["ben"]
    url = "/api/v1/v3/me/seen"
    now = datetime.now(timezone.utc)

    r = await c.get(url, headers=ana)
    assert r.status_code == 200, r.text
    assert list(r.json()) == ["seen", "surfaces", "server_now"]
    assert r.json()["seen"] == {}
    assert r.json()["surfaces"] == list(me_seen_api.SEEN_SURFACES)

    t1 = now - timedelta(minutes=10)
    r = await c.put(url, headers=ana, json={"seen": {"watch": t1.isoformat()}})
    assert r.status_code == 200, r.text
    assert datetime.fromisoformat(r.json()["seen"]["watch"]) == t1

    # Another reader sees only their own (nothing).
    assert (await c.get(url, headers=ben)).json()["seen"] == {}

    # Monotonic: an older time does not rewind; a newer one moves it.
    older = (now - timedelta(hours=1)).isoformat()
    r = await c.put(url, headers=ana, json={"seen": {"watch": older}})
    assert datetime.fromisoformat(r.json()["seen"]["watch"]) == t1
    t2 = now - timedelta(minutes=1)
    r = await c.put(url, headers=ana, json={
        "seen": {"watch": t2.isoformat(), "changed": t2.isoformat()},
    })
    assert set(r.json()["seen"]) == {"watch", "changed"}

    # Bounds.
    assert (await c.put(url, headers=ana, json={"seen": {"board": t2.isoformat()}})).status_code == 400
    future = (now + timedelta(hours=1)).isoformat()
    assert (await c.put(url, headers=ana, json={"seen": {"watch": future}})).status_code == 400
    assert (await c.put(url, headers=ana, json={"seen": {}})).status_code == 422
    assert (await c.put(url, headers=ana, json={
        "seen": {"watch": t2.isoformat()}, "user_id": "someone-else",
    })).status_code == 422

    # A cross-site write with the cookie is refused before the route.
    r = await c.put(url, headers={**ana, "Sec-Fetch-Site": "cross-site"},
                    json={"seen": {"watch": t2.isoformat()}})
    assert r.status_code == 403

    # The admin token is not a person: 404 no_user, nothing written.
    r = await c.get(url, headers={"Authorization": f"Bearer {ADMIN_TOK}"})
    assert r.status_code == 404 and r.json()["detail"]["reason"] == "no_user"

    # Stored rows: ana's two, nothing for ben; deleted with the user.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT u.email, c.surface FROM user_seen_cursors c "
            "JOIN users u ON u.id = c.user_id WHERE u.email LIKE $1 ORDER BY 1, 2",
            f"%@{_DOMAIN}",
        )
        assert [(r["email"].split("@")[0], r["surface"]) for r in rows] == [
            ("ana", "changed"), ("ana", "watch"),
        ]


# ---------------------------------------------------------------------------
# Migration 0259
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0259_idempotent_bounded_cascading(migrated_pg) -> None:
    sql = _MIGRATION.read_text()
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        await conn.execute(sql)
        await conn.execute(sql)
        uid = await conn.fetchval(
            "INSERT INTO users (email, role) VALUES ($1, 'reader') RETURNING id",
            f"mig-{uuid.uuid4().hex[:8]}@{_DOMAIN}",
        )
        await conn.execute(
            "INSERT INTO user_seen_cursors (user_id, surface, seen_at) "
            "VALUES ($1, 'watch', now())", uid,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO user_seen_cursors (user_id, surface, seen_at) "
                "VALUES ($1, 'free text', now())", uid,
            )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO user_seen_cursors (user_id, surface, seen_at) "
                "VALUES ($1, 'watch', now())", uid,
            )
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        assert await conn.fetchval(
            "SELECT count(*) FROM user_seen_cursors WHERE user_id = $1", uid,
        ) == 0
    finally:
        await conn.close()
