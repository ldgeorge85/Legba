# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Acknowledge and snooze an alert episode (plan 4.4, migration 0261).

  * ``POST/DELETE /v3/system/alerts/{id}/ack`` and ``…/snooze`` — contributor
    and admin with sign-in on; the bearer (operator class) with it off; a
    reader and the fleet's service credential refused.
  * An acknowledgement / snooze takes the episode out of "needs you" (and its
    count) for everyone, until it PAGES above the severity it stood at; a
    snooze also lapses with its time. Neither closes the episode or changes a
    page: the real ``raise_alert`` still pages a worsening acknowledged
    episode.
  * Every act is a history row and a signed, chained audit row naming the
    stable actor — never a credential.
  * ``/since?include=needs_you`` and ``/since/counts`` keep the shapes;
    only ``acked_at`` / ``snoozed_until`` become real.

PURE tests need no database; the rest run over the migrated test DB through
the real app (middlewares + the route gate) and real HTTP.
"""
from __future__ import annotations

import os
import pathlib
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

import legba.data.registry.alert_ack_api as ack_api
import legba.data.registry.since_api as since_api
from legba.data import users as U
from legba.data.alerts import AlertSinkDispatcher, NtfyAlertSink
from legba.data.alerts import raise_alert as RA
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.registry import _deps
from legba.data.registry import route_policy as rp
from legba.data.registry import route_roles as rr
from legba.data.registry import session_auth as sa
from legba.data.registry.alert_log_api import (
    ATTENTION_HIDDEN_SQL,
    AlertEpisode,
    attention_baseline,
    attention_state,
    read_alert_log,
)
from legba.data.registry.credentials import MASTER_KEY_ENV
from legba.shared import auth_signin as S

_REPO = pathlib.Path(__file__).resolve().parents[2]
_MIGRATION = _REPO / "src" / "legba" / "data" / "migrations" / "0261_alert_ack_snooze.sql"

os.environ.setdefault(
    MASTER_KEY_ENV, "0011223344556677889900112233445566778899001122334455667788990011",
)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)

V3 = "/api/v1/v3"
ADMIN_TOK = "s2-admin-token-" + "5" * 24
SERVICE_TOK = "s2-service-token-" + "6" * 24
_DOMAIN = "s2ack.example"

#: The shapes (copied from test_s1_needs_you_and_seen; must not move).
NEEDS_YOU_ITEM_KEYS = [
    "id", "kind", "group", "topic", "severity", "summary", "subject_kind",
    "subject_id", "opened_at", "last_raised_at", "last_paged_at",
    "paged_severity", "why", "acked_at", "snoozed_until",
]
SINCE_KEYS_WITH_NY = [
    "cursor", "server_now", "counts", "new_findings", "superseded",
    "band_changes", "situations", "alerts", "forecasts_due", "needs_you",
]
COUNTS_KEYS = ["cursor", "server_now", "counts", "needs_you", "changed", "forecasts_due"]
ATTENTION_OUT_KEYS = [
    "version", "episode_id", "kind", "severity", "paged_severity", "state",
    "action", "changed", "attention", "history", "shared", "shared_note",
    "paging_note", "snooze_options",
]


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def test_routes_declared_for_both_switch_positions() -> None:
    base = f"{V3}/system/alerts/{{episode_id}}"
    for m, leaf in (("POST", "ack"), ("DELETE", "ack"), ("POST", "snooze"), ("DELETE", "snooze")):
        assert rp.ROUTE_MIN_ROLES[(m, f"{base}/{leaf}")] == S.ROLE_CONTRIBUTOR
        assert rr.ROUTE_ROLES[(m, f"{base}/{leaf}")] == rr.ROLE_OPERATOR
    assert rp.ROUTE_MIN_ROLES[("GET", f"{base}/attention")] == S.ROLE_READER
    human_only = {(m, t) for m, t, _ in _deps.HUMAN_ONLY_ROUTES}
    assert ("*", f"{base}/ack") in human_only and ("*", f"{base}/snooze") in human_only


def test_slim_import() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.registry.alert_ack_api
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts")
                        or m.startswith("legba.runtime"))
        assert not leaked, leaked
        print("OK")
        """
    )
    src_root = str(pathlib.Path(ack_api.__file__).resolve().parents[3])
    r = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (src_root, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


def test_snooze_options_closed_and_bounded() -> None:
    now = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)
    assert list(ack_api.SNOOZE_OPTIONS) == ["1h", "4h", "24h", "until_closed"]
    assert ack_api.snooze_until("4h", now=now) == now + timedelta(hours=4)
    assert ack_api.snooze_until("until_closed", now=now) is None
    with pytest.raises(ValueError):
        ack_api.snooze_until("7d", now=now)
    sql = _MIGRATION.read_text()
    assert "'1h', '4h', '24h', 'until_closed'" in sql
    assert "BEGIN;" not in sql.upper()


def test_baseline_and_state_rule() -> None:
    now = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)
    assert attention_baseline("medium", None) == "medium"
    assert attention_baseline("medium", "high") == "high"
    assert attention_baseline("critical", "high") == "critical"
    row = {"state": "open", "acked_at": now, "acked_by": "a", "ack_severity": "medium",
           "paged_severity": "medium"}
    assert attention_state(row, now=now).ack_active is True
    up = attention_state({**row, "paged_severity": "high"}, now=now)
    assert up.ack_active is False and up.resurfaced is True
    closed = attention_state({**row, "state": "closed"}, now=now)
    assert closed.ack_active is False and closed.acked_by == "a"
    snz = {"state": "open", "snoozed_at": now, "snoozed_by": "a", "snooze_option": "1h",
           "snoozed_until": now + timedelta(hours=1), "snooze_severity": "high",
           "paged_severity": "high"}
    assert attention_state(snz, now=now).snooze_active is True
    assert attention_state(snz, now=now + timedelta(hours=2)).snooze_active is False
    forever = {**snz, "snooze_option": "until_closed", "snoozed_until": None}
    assert attention_state(forever, now=now + timedelta(days=9)).snooze_active is True
    assert attention_state({**forever, "paged_severity": "critical"}, now=now).snooze_active is False


def test_alert_log_episode_shape_is_additive() -> None:
    fields = list(AlertEpisode.model_fields)
    assert fields[-1] == "attention" and "close_reason" in fields


# ---------------------------------------------------------------------------
# The real app
# ---------------------------------------------------------------------------


async def _make_app(migrated_pg: PostgresConfig, monkeypatch, *, signin: bool):
    from legba.data.registry.server import create_app

    if signin:
        monkeypatch.setenv(S.SIGNIN_ENV, "1")
    else:
        monkeypatch.delenv(S.SIGNIN_ENV, raising=False)
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", ADMIN_TOK)
    monkeypatch.setenv("LEGBA_REGISTRY_SERVICE_TOKEN", SERVICE_TOK)
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    monkeypatch.delenv(rr.ROLE_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(sa.COOKIE_INSECURE_ENV, raising=False)
    app = create_app(
        pg_config=migrated_pg, nats_config=NatsConfig.from_env(),
        enable_healthcheck_loop=False, enable_vocabulary_subscription=False,
    )
    store = app.state.registry_deps.descriptor_registry.pg
    await store.connect()
    return app, store


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{_DOMAIN}")
    since_api.invalidate_since_counts()
    yield p
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{_DOMAIN}")
        await conn.execute(
            "DELETE FROM alert_episode_actions WHERE alert_id IN "
            "(SELECT id FROM alerts WHERE raised_by = 's2_test')")
        await conn.execute(
            "DELETE FROM alert_sink_deliveries WHERE alert_id IN "
            "(SELECT id FROM alerts WHERE raised_by = 's2_test')")
        await conn.execute("DELETE FROM alert_events WHERE raised_by = 's2_test'")
        await conn.execute("DELETE FROM alerts WHERE raised_by = 's2_test'")
    since_api.invalidate_since_counts()
    RA.reset_dispatchers()
    await p.close()


@pytest_asyncio.fixture
async def on_app(migrated_pg: PostgresConfig, monkeypatch, pool):
    """Sign-in ON: a reader, a contributor and an admin with cookies."""
    app, store = await _make_app(migrated_pg, monkeypatch, signin=True)
    who: dict[str, dict[str, str]] = {}
    ids: dict[str, str] = {}
    async with pool.acquire() as conn:
        for name, role in (("rhea", "reader"), ("cole", "contributor"), ("ada", "admin")):
            uid = await conn.fetchval(
                "INSERT INTO users (email, role, status) VALUES ($1, $2, 'active') "
                "RETURNING id", f"{name}@{_DOMAIN}", role,
            )
            ids[name] = str(uid)
            session = await U.create_session(conn, await U.get_user(conn, uid))
            who[name] = {
                "Cookie": f"{sa.SESSION_COOKIE_NAME}={session.token}",
                "Sec-Fetch-Site": "same-origin",
            }
    who["admin_token"] = {"Authorization": f"Bearer {ADMIN_TOK}"}
    who["service"] = {"Authorization": f"Bearer {SERVICE_TOK}"}
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            yield c, who, ids
    finally:
        await store.close()


@pytest_asyncio.fixture
async def off_app(migrated_pg: PostgresConfig, monkeypatch, pool):
    """Sign-in OFF (live today): one principal, the bearer."""
    app, store = await _make_app(migrated_pg, monkeypatch, signin=False)
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            yield c
    finally:
        await store.close()


async def _episode(
    pool: Any, *, kind: str = "verified_finding", severity: str = "medium",
    paged: bool = True, opened_at: datetime | None = None, state: str = "open",
) -> str:
    at = opened_at or datetime.now(timezone.utc) - timedelta(minutes=2)
    subject = f"s2-{uuid.uuid4().hex[:10]}"
    async with pool.acquire() as conn:
        return str(await conn.fetchval(
            """
            INSERT INTO alerts (kind, subject_kind, subject_id, dedupe_key, state,
                                severity, max_severity, summary, raised_by,
                                opened_at, last_raised_at, closed_at, close_reason,
                                paged_count, paged_severity, last_paged_at)
            VALUES ($1, 'target', $2, $3, $4, $5, $5, 's2 episode', 's2_test',
                    $6, $6, $7, $8, $9, $10, $11)
            RETURNING id
            """,
            kind, subject, f"{kind}:{subject}", state, severity, at,
            at if state == "closed" else None, "test" if state == "closed" else None,
            1 if paged else 0, severity if paged else None, at if paged else None,
        ))


async def _needs_you(c: httpx.AsyncClient, h: dict[str, str], cursor: datetime) -> dict:
    r = await c.get(f"{V3}/since", headers=h,
                    params={"cursor": cursor.isoformat(), "include": "needs_you"})
    assert r.status_code == 200, r.text
    return r.json()


def _ids(body: dict) -> set[str]:
    return {i["id"] for i in body["needs_you"]["items"]}


async def _audit_rows(pool: Any, episode_id: str) -> list[dict]:
    return [dict(r) for r in await pool.fetch(
        "SELECT actor_id, actor_role, namespace, action, change_summary::text AS s, "
        "row_hash FROM descriptor_audit_log WHERE namespace = 'alert' "
        "AND descriptor_id = $1 ORDER BY occurred_at", episode_id)]


def _url(eid: str, leaf: str) -> str:
    return f"{V3}/system/alerts/{eid}/{leaf}"


# -- the role matrix, sign-in ON ---------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_role_matrix_signin_on(on_app, pool) -> None:
    c, who, ids = on_app
    eid = await _episode(pool)

    # A reader may LOOK, not change.
    r = await c.get(_url(eid, "attention"), headers=who["rhea"])
    assert r.status_code == 200, r.text
    assert list(r.json()) == ATTENTION_OUT_KEYS
    for method, leaf, body in (("POST", "ack", {}), ("DELETE", "ack", None),
                               ("POST", "snooze", {"duration": "1h"}),
                               ("DELETE", "snooze", None)):
        r = await c.request(method, _url(eid, leaf), headers=who["rhea"], json=body)
        assert r.status_code == 403, (method, leaf, r.text)
        assert r.json()["detail"]["required_role"] == "contributor"

    # The fleet's service credential: refused (a person's act).
    r = await c.post(_url(eid, "ack"), headers=who["service"], json={})
    assert r.status_code == 403, r.text

    # A cross-site cookie write is refused before the route.
    r = await c.post(_url(eid, "ack"),
                     headers={**who["cole"], "Sec-Fetch-Site": "cross-site"}, json={})
    assert r.status_code == 403 and "cross_site" in r.text

    # Contributor: allowed; recorded as the person, never anything from the body.
    r = await c.post(_url(eid, "ack"), headers=who["cole"], json={"note": "on it"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["changed"] is True and out["action"] == "acknowledged"
    assert out["attention"]["acked_by"] == f"user:{ids['cole']}"
    assert out["attention"]["ack_active"] and out["attention"]["hidden_from_needs_you"]
    assert out["shared"] is True and "everyone" in out["shared_note"]
    assert "Notifications are unchanged" in out["paging_note"]
    assert (await c.post(_url(eid, "ack"), headers=who["cole"],
                         json={"acked_by": "someone-else"})).status_code == 422

    # Admin (a person) and the admin token: allowed.
    r = await c.delete(_url(eid, "ack"), headers=who["ada"])
    assert r.status_code == 200 and r.json()["changed"] is True
    r = await c.post(_url(eid, "snooze"), headers=who["admin_token"], json={"duration": "4h"})
    assert r.status_code == 200, r.text
    assert r.json()["attention"]["snoozed_by"].startswith("admin-token:bearer:")

    # Below admin only the world's kinds exist: an engine episode is 404.
    engine = await _episode(pool, kind="source_degraded", severity="high")
    assert (await c.post(_url(engine, "ack"), headers=who["cole"], json={})).status_code == 404
    assert (await c.get(_url(engine, "attention"), headers=who["rhea"])).status_code == 404
    assert (await c.post(_url(engine, "ack"), headers=who["ada"], json={})).status_code == 200
    # Unknown / malformed ids.
    assert (await c.post(_url(str(uuid.uuid4()), "ack"), headers=who["ada"], json={})).status_code == 404
    assert (await c.post(_url("nope", "ack"), headers=who["ada"], json={})).status_code == 404

    # Audit: one signed, chained row per act, the actor named, no credential.
    rows = await _audit_rows(pool, eid)
    assert [r["action"] for r in rows] == [
        "alert_acknowledge", "alert_unacknowledge", "alert_snooze",
    ]
    assert rows[0]["actor_id"] == f"user:{ids['cole']}"
    assert rows[0]["actor_role"] == "contributor"
    assert rows[1]["actor_id"] == f"user:{ids['ada']}"
    assert all(r["row_hash"] for r in rows)
    history = (await c.get(_url(eid, "attention"), headers=who["rhea"])).json()["history"]
    assert [h["action"] for h in history] == ["acknowledged", "unacknowledged", "snoozed"]
    assert history[0]["note"] == "on it" and history[0]["actor_role"] == "contributor"
    blob = repr(rows) + repr(history)
    for secret in (ADMIN_TOK, SERVICE_TOK):
        assert secret not in blob
    for h in (who["rhea"], who["cole"], who["ada"]):
        raw = h["Cookie"].split("=", 1)[1]
        assert raw not in blob


# -- sign-in OFF: the bearer ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signin_off_bearer_is_the_one_principal(off_app, pool) -> None:
    c = off_app
    eid = await _episode(pool)
    bearer = {"Authorization": f"Bearer {ADMIN_TOK}"}
    r = await c.post(_url(eid, "ack"), headers=bearer)  # no body at all
    assert r.status_code == 200, r.text
    actor = r.json()["attention"]["acked_by"]
    assert actor.startswith("operator:bearer:") and ADMIN_TOK not in actor
    assert actor == _deps.principal_actor("operator", ADMIN_TOK)
    # The service credential is refused; no credential is 401.
    assert (await c.post(_url(eid, "ack"),
                         headers={"Authorization": f"Bearer {SERVICE_TOK}"})).status_code == 403
    assert (await c.post(_url(eid, "ack"))).status_code == 401
    rows = await _audit_rows(pool, eid)
    assert len(rows) == 1 and rows[0]["actor_id"] == actor
    assert ADMIN_TOK not in repr(rows)


# -- idempotence -------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotent_acts(off_app, pool) -> None:
    c = off_app
    h = {"Authorization": f"Bearer {ADMIN_TOK}"}
    eid = await _episode(pool)

    assert (await c.delete(_url(eid, "ack"), headers=h)).json()["changed"] is False
    assert (await c.delete(_url(eid, "snooze"), headers=h)).json()["changed"] is False
    first = (await c.post(_url(eid, "ack"), headers=h, json={"note": "x"})).json()
    again = (await c.post(_url(eid, "ack"), headers=h, json={"note": "y"})).json()
    assert first["changed"] is True and again["changed"] is False
    assert again["attention"]["acked_at"] == first["attention"]["acked_at"]
    assert again["attention"]["ack_note"] == "x"

    s1 = (await c.post(_url(eid, "snooze"), headers=h, json={"duration": "1h"})).json()
    s2 = (await c.post(_url(eid, "snooze"), headers=h, json={"duration": "1h"})).json()
    assert s1["changed"] is True and s2["changed"] is False
    s3 = (await c.post(_url(eid, "snooze"), headers=h, json={"duration": "24h"})).json()
    assert s3["changed"] is True and s3["attention"]["snooze_option"] == "24h"

    # Bounded: no free-form duration, no far-future date, a short note.
    for body in ({"duration": "7d"}, {"duration": "2026-12-31T00:00:00Z"},
                 {"until": "2099-01-01T00:00:00Z"}, {"duration": "1h", "note": "n" * 281}):
        assert (await c.post(_url(eid, "snooze"), headers=h, json=body)).status_code == 422

    history = (await c.get(_url(eid, "attention"), headers=h)).json()["history"]
    assert [x["action"] for x in history] == ["acknowledged", "snoozed", "snoozed"]
    assert len(await _audit_rows(pool, eid)) == 3

    # A closed episode cannot be acted on; its history stays readable.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE alerts SET state = 'closed', closed_at = now(), close_reason = 't' "
            "WHERE id = $1", uuid.UUID(eid))
    r = await c.post(_url(eid, "ack"), headers=h)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "alert_episode_closed"
    got = (await c.get(_url(eid, "attention"), headers=h)).json()
    assert got["state"] == "closed" and len(got["history"]) == 3
    assert got["attention"]["acked_by"] and got["attention"]["ack_active"] is False


# -- needs_you + counts honour both; the higher-severity re-surface rule --------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_needs_you_hides_and_resurfaces(on_app, pool) -> None:
    c, who, _ = on_app
    cursor = datetime.now(timezone.utc) - timedelta(minutes=30)
    acked = await _episode(pool, severity="medium")
    snoozed = await _episode(pool, severity="high")
    other = await _episode(pool, severity="medium")

    body = await _needs_you(c, who["rhea"], cursor)
    assert {acked, snoozed, other} <= _ids(body)
    before = (await c.get(f"{V3}/since/counts", headers=who["rhea"],
                          params={"cursor": cursor.isoformat()})).json()

    assert (await c.post(_url(acked, "ack"), headers=who["cole"], json={})).status_code == 200
    assert (await c.post(_url(snoozed, "snooze"), headers=who["cole"],
                         json={"duration": "1h"})).status_code == 200

    # Hidden for EVERYONE — the reader and the admin, not only the actor.
    for h in (who["rhea"], who["ada"], who["cole"]):
        body = await _needs_you(c, h, cursor)
        assert acked not in _ids(body) and snoozed not in _ids(body)
        assert other in _ids(body)
    # Shapes unchanged.
    assert list(body) == SINCE_KEYS_WITH_NY
    assert all(list(i) == NEEDS_YOU_ITEM_KEYS for i in body["needs_you"]["items"])
    # The badge reflects the act at once (the 30 s reuse is dropped).
    after = (await c.get(f"{V3}/since/counts", headers=who["rhea"],
                         params={"cursor": cursor.isoformat()})).json()
    assert list(after) == COUNTS_KEYS
    assert after["needs_you"] == before["needs_you"] - 2
    assert after["needs_you"] == body["needs_you"]["total"]

    # A raise at a higher severity that does NOT page: still hidden.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE alerts SET severity = 'high', max_severity = 'high' WHERE id = $1",
            uuid.UUID(acked))
    assert acked not in _ids(await _needs_you(c, who["rhea"], cursor))
    # It PAGES at that higher severity: back, carrying when it was acknowledged.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE alerts SET paged_severity = 'high', paged_count = paged_count + 1, "
            "last_paged_at = now() WHERE id = $1", uuid.UUID(acked))
    body = await _needs_you(c, who["rhea"], cursor)
    assert acked in _ids(body)
    item = next(i for i in body["needs_you"]["items"] if i["id"] == acked)
    assert item["acked_at"] is not None and item["snoozed_until"] is None
    state = (await c.get(_url(acked, "attention"), headers=who["rhea"])).json()["attention"]
    assert state["resurfaced"] is True and state["ack_active"] is False
    # Acknowledging again sets a new baseline (high): hidden again.
    r = await c.post(_url(acked, "ack"), headers=who["cole"], json={})
    assert r.json()["changed"] is True and r.json()["attention"]["ack_severity"] == "high"
    assert acked not in _ids(await _needs_you(c, who["rhea"], cursor))

    # Snooze expiry: when its time has passed the episode is back.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE alerts SET snoozed_at = now() - interval '2 hours', "
            "snoozed_until = now() - interval '1 hour' WHERE id = $1", uuid.UUID(snoozed))
    body = await _needs_you(c, who["rhea"], cursor)
    assert snoozed in _ids(body)
    item = next(i for i in body["needs_you"]["items"] if i["id"] == snoozed)
    assert item["snoozed_until"] is not None and item["acked_at"] is None
    # Un-snooze / un-acknowledge bring an item back at once.
    r = await c.post(_url(other, "snooze"), headers=who["cole"], json={"duration": "until_closed"})
    assert r.json()["attention"]["snoozed_until"] is None
    assert other not in _ids(await _needs_you(c, who["rhea"], cursor))
    await c.delete(_url(other, "snooze"), headers=who["cole"])
    assert other in _ids(await _needs_you(c, who["rhea"], cursor))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_and_python_rule_agree(pool) -> None:
    """The SQL predicate and :func:`attention_state` give the same answer on
    a grid of stored states."""
    now_db = None
    cases = []
    for ack_sev in (None, "medium", "high"):
        for paged in (None, "medium", "high", "critical"):
            for snooze in (None, ("1h", 1), ("1h", -1), ("until_closed", None)):
                cases.append((ack_sev, paged, snooze))
    async with pool.acquire() as conn:
        now_db = await conn.fetchval("SELECT now()")
        for ack_sev, paged, snooze in cases:
            eid = await _episode(pool, severity="medium", paged=False)
            await conn.execute(
                """
                UPDATE alerts SET paged_severity = $2,
                    acked_at = CASE WHEN $3::text IS NULL THEN NULL ELSE now() END,
                    acked_by = CASE WHEN $3::text IS NULL THEN NULL ELSE 't' END,
                    ack_severity = $3
                 WHERE id = $1
                """, uuid.UUID(eid), paged, ack_sev)
            if snooze is not None:
                opt, hours = snooze
                await conn.execute(
                    """
                    UPDATE alerts SET snoozed_at = now() - interval '2 hours',
                        snoozed_by = 't', snooze_option = $2, snooze_severity = 'medium',
                        snoozed_until = CASE WHEN $3::int IS NULL THEN NULL
                                        ELSE now() + make_interval(hours => $3::int) END
                     WHERE id = $1
                    """, uuid.UUID(eid), opt, hours)
            row = await conn.fetchrow(
                f"SELECT *, {ATTENTION_HIDDEN_SQL} AS hidden FROM alerts WHERE id = $1",
                uuid.UUID(eid))
            py = attention_state(dict(row), now=now_db)
            assert py.hidden_from_needs_you is row["hidden"], (ack_sev, paged, snooze)


# -- paging is unchanged: the real raise path still pages a worsening episode ----


class _Resp:
    status_code = 200
    text = ""


class _Http:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def post(self, url, *, content=None, headers=None, timeout=None, **kw):  # noqa: ANN001
        self.calls.append(url)
        return _Resp()


async def _noop_sleep(_s: float) -> None:
    return None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acknowledged_or_snoozed_episode_still_pages_when_it_worsens(
    off_app, pool,
) -> None:
    c = off_app
    h = {"Authorization": f"Bearer {ADMIN_TOK}"}
    http = _Http()
    sink = NtfyAlertSink(url="http://ntfy.s2.test/x", http=http, sleep=_noop_sleep,
                         min_severity="medium")
    disp = AlertSinkDispatcher(pg_pool=pool, sinks=[sink], cooldown_seconds=0)
    for leaf, body in (("ack", {}), ("snooze", {"duration": "24h"})):
        kw = dict(kind="escalations", subject_id=f"s2-{uuid.uuid4().hex[:8]}",
                  raised_by="s2_test", summary="s2", dispatcher=disp)
        r1 = await RA.raise_alert(pool, severity="medium", **kw)
        assert r1.decision == "paged", r1
        resp = await c.post(_url(r1.alert_id, leaf), headers=h, json=body)
        assert resp.status_code == 200 and resp.json()["changed"] is True
        same = await RA.raise_alert(pool, severity="medium", **kw)
        assert same.decision == "deduped"  # as it would have been anyway
        worse = await RA.raise_alert(pool, severity="high", **kw)
        assert worse.decision == "paged", worse  # the act did not stop the page
        state = (await c.get(_url(r1.alert_id, "attention"), headers=h)).json()
        assert state["state"] == "open"  # neither act closes the episode
        assert state["attention"]["resurfaced"] is True
        assert state["attention"]["hidden_from_needs_you"] is False
    assert len(http.calls) >= 4


# -- the alert-log route shows the state ------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_alert_log_shows_attention(off_app, pool) -> None:
    c = off_app
    h = {"Authorization": f"Bearer {ADMIN_TOK}"}
    eid = await _episode(pool)
    closed = await _episode(pool)
    await c.post(_url(eid, "snooze"), headers=h, json={"duration": "4h", "note": "later"})
    await c.post(_url(closed, "ack"), headers=h)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE alerts SET state = 'closed', closed_at = now(), close_reason = 't' "
            "WHERE id = $1", uuid.UUID(closed))
        log = await read_alert_log(conn, hours=24)
    assert log.measured, log.not_measured_reason
    ep = next(e for e in log.open if e.id == eid)
    assert ep.attention.snooze_active and ep.attention.snooze_option == "4h"
    gone = next(e for e in log.closed if e.id == closed)
    assert gone.attention.acked_by and gone.attention.ack_active is False


# -- migration 0261 ----------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0261_idempotent_and_bounded(pool) -> None:
    sql = _MIGRATION.read_text()
    async with pool.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
        eid = uuid.UUID(await _episode(pool))
        with pytest.raises(asyncpg.CheckViolationError):  # far-future date
            await conn.execute(
                "UPDATE alerts SET snoozed_at = now(), snoozed_by = 't', "
                "snooze_option = '24h', snooze_severity = 'medium', "
                "snoozed_until = now() + interval '30 days' WHERE id = $1", eid)
        with pytest.raises(asyncpg.CheckViolationError):  # free-form option
            await conn.execute(
                "UPDATE alerts SET snoozed_at = now(), snoozed_by = 't', "
                "snooze_option = '7d', snooze_severity = 'medium', "
                "snoozed_until = now() + interval '1 hour' WHERE id = $1", eid)
        with pytest.raises(asyncpg.CheckViolationError):  # ack without a who
            await conn.execute(
                "UPDATE alerts SET acked_at = now(), ack_severity = 'medium' WHERE id = $1",
                eid)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO alert_episode_actions (alert_id, action, actor, actor_kind, "
                "actor_role, episode_severity) VALUES ($1, 'closed', 't', 't', 't', 'low')",
                eid)
