# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""F-1 on the migrated test DB — migration 0262, the write routes through the
REAL app (sign-in on and off), and stored anchor trees with their proofs.

  * 0262 applies twice; its CHECKs refuse a rejection without a reason or
    with a resolution, an unknown actor role; the leaf tables are append-only;
  * the route-role matrix through ``create_app`` with the sign-in switch ON:
    a reader is refused by the gate (naming ``contributor``), a contributor
    is refused on a question where they entered a number and confirms
    another, an admin corrects; a service credential is refused (a person
    decides); every act lands one signed, CHAINED audit row; the record
    carries the actor and the role;
  * switch OFF: the admin bearer confirms, recorded as its stable actor
    (``operator:bearer:<sha12>``), never the token;
  * a stored anchor tree on the real tables: the seal-proof route proves the
    question and its number, the path re-folds to the anchored root.

Every row this file writes is FICTIONAL (``FX-F1-…`` slugs, ``@f1test.example``
users, anchor days in 2041); the record tables are append-only, so the
questions' slugs carry a run id.
"""
from __future__ import annotations

import os
import pathlib
import random
import uuid
from datetime import date, datetime, timedelta, timezone

import asyncpg
import httpx
import pytest
import pytest_asyncio

from legba.data import anchor_proof as ap
from legba.data import forecast_ledger as fl
from legba.data import forecast_resolution as fr
from legba.data import forecast_resolvers as rules
from legba.data import users as U
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.registry import route_roles as rr
from legba.data.registry import session_auth as sa
from legba.shared import auth_signin as S

_REPO = pathlib.Path(__file__).resolve().parents[2]
_MIGRATION = _REPO / "src" / "legba" / "data" / "migrations" / "0262_forecast_confirm_and_anchor_leaves.sql"
_DOMAIN = "f1test.example"
ADMIN_TOK = "f1-admin-token-" + "7" * 24
RUNTIME_TOK = "f1-runtime-token-" + "8" * 22
RUN = uuid.uuid4().hex[:8]
SEALED = datetime(2026, 3, 1, tzinfo=timezone.utc)
DUE = datetime(2026, 3, 20, 23, 59, 59, tzinfo=timezone.utc)
V3 = "/api/v1/v3"


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM user_sessions WHERE user_id IN (SELECT id FROM users WHERE email LIKE $1)",
                           f"%@{_DOMAIN}")
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{_DOMAIN}")
    yield p
    await p.close()


def _question(slug: str) -> fl.ForecastQuestion:
    return fl.ForecastQuestion(
        slug=slug, question=f"An invented event ({slug}) happens in the window.",
        resolver_kind="event_reported",
        resolver_spec={"class": "invented event", "actors": ["XA"], "window": ["2026-03-01", "2026-03-20"]},
        due_at=DUE, window_start=SEALED, window_end=DUE,
        void_rules="Nobody reports on the invented event by due + grace.",
    )


async def _sealed_with_proposal(conn, slug: str, *numbers: tuple[str, str, float]) -> tuple[str, str]:
    _, qid = await fl.upsert_question(conn, _question(slug))
    for kind, ref, p in numbers or (("operator", "operator", 0.6),):
        await fl.state_number(conn, qid, fl.ForecastNumber(kind, ref, p, SEALED - timedelta(days=1)))
    await fl.seal_question(conn, qid, at=SEALED)
    row = dict(await conn.fetchrow(fl._SELECT_QUESTION_BY_ID, qid))  # noqa: SLF001
    prop = rules.Proposal(outcome="yes", resolver_kind="event_reported", as_of=DUE + timedelta(hours=80),
                          evidence=[{"ref": "signal:00000000-0000-0000-0000-0000000000f1",
                                     "url": "https://example.org/f1", "title": "An invented report"}])
    status, rec = await fr.write_proposal(conn, row, prop)
    assert status == "written"
    return qid, rec


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


async def test_migration_0262_applies_twice_and_its_rules_hold(pool) -> None:
    sql = _MIGRATION.read_text()
    async with pool.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
        cols = {r["column_name"] for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'forecast_resolution_records'")}
        assert "actor_role" in cols
        qid, rec = await _sealed_with_proposal(conn, f"FX-F1-mig-{RUN}")
        q = await conn.fetchrow(fl._SELECT_QUESTION_BY_ID, qid)  # noqa: SLF001
        base = fr._record_args(  # noqa: SLF001
            qid, q["content_sha256"], record_kind="rejection", outcome="yes", resolver_kind="event_reported",
            resolver_version="v", as_of=DUE, actor="operator:t", basis_record_id=rec, reason="x",
            fingerprint="ab" * 32)
        bad_reason = list(base)
        bad_reason[16] = " "
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(fr.INSERT_RECORD_SQL, *bad_reason)
        no_basis = list(base)
        no_basis[12] = None
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(fr.INSERT_RECORD_SQL, *no_basis)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(fr.INSERT_RECORD_WITH_ROLE_SQL, *base, "superuser")
        # The leaf tables are append-only.
        day = date(2041, 1, 1) + timedelta(days=random.randint(0, 3000))
        await conn.execute(ap.INSERT_TREE_SQL, day, "cd" * 32, 0, "test")
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE receipt_anchor_trees SET leaf_count = 1 WHERE day = $1", day)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("DELETE FROM receipt_anchor_trees WHERE day = $1", day)


# ---------------------------------------------------------------------------
# The app, both sides of the switch
# ---------------------------------------------------------------------------


def _build_app(migrated_pg: PostgresConfig, monkeypatch):
    from legba.data.registry.credentials import MASTER_KEY_ENV
    from legba.data.registry.server import create_app

    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", ADMIN_TOK)
    monkeypatch.delenv("LEGBA_REGISTRY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv(S.service_token_env("runtime"), RUNTIME_TOK)
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    monkeypatch.delenv(rr.ROLE_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(sa.COOKIE_INSECURE_ENV, raising=False)
    if not os.environ.get(MASTER_KEY_ENV):
        monkeypatch.setenv(MASTER_KEY_ENV, "00112233445566778899" * 3 + "0011")
    if not os.environ.get("LEGBA_REGISTRY_SIGNING_KEY"):
        monkeypatch.setenv("LEGBA_REGISTRY_SIGNING_KEY", "22" * 32)
    return create_app(pg_config=migrated_pg, nats_config=NatsConfig.from_env(),
                      enable_healthcheck_loop=False, enable_vocabulary_subscription=False)


async def _client(app):
    store = app.state.registry_deps.descriptor_registry.pg
    await store.connect()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return store, httpx.AsyncClient(transport=transport, base_url="https://t")


async def _person(pool, name: str, role: str) -> tuple[str, dict[str, str]]:
    async with pool.acquire() as conn:
        uid = await conn.fetchval(
            "INSERT INTO users (email, role, status) VALUES ($1, $2, 'active') RETURNING id::text",
            f"{name}-{RUN}@{_DOMAIN}", role)
        user = await U.get_user(conn, uid)
        sess = await U.create_session(conn, user)
    return uid, {"Cookie": f"{sa.SESSION_COOKIE_NAME}={sess.token}", "Sec-Fetch-Site": "same-origin"}


async def _audit_rows(pool, slug: str) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT actor_id, action, from_version, to_version, chain_seq, row_hash "
            "FROM descriptor_audit_log WHERE namespace = 'forecast_ledger' AND descriptor_id = $1 "
            "ORDER BY occurred_at", slug)


async def test_the_role_matrix_with_sign_in_on(migrated_pg, pool, monkeypatch) -> None:
    monkeypatch.setenv(S.SIGNIN_ENV, "1")
    _, reader = await _person(pool, "rhea", "reader")
    cato_id, cato = await _person(pool, "cato", "contributor")
    adam_id, adam = await _person(pool, "adam", "admin")
    async with pool.acquire() as conn:
        mine_q, mine_p = await _sealed_with_proposal(
            conn, f"FX-F1-mine-{RUN}", ("operator", "operator", 0.6), ("seat", f"user:{cato_id}", 0.4))
        other_q, other_p = await _sealed_with_proposal(conn, f"FX-F1-other-{RUN}")
    store, c = await _client(_build_app(migrated_pg, monkeypatch))
    try:
        body = {"outcome": "yes"}
        # Reader: the gate refuses, naming the role.
        r = await c.post(f"{V3}/ledger/resolutions/{other_p}/confirm", json=body, headers=reader)
        assert r.status_code == 403 and r.json()["detail"]["required_role"] == "contributor", r.text
        r = await c.get(f"{V3}/ledger/resolution-queue", headers=reader)
        assert r.status_code == 403
        # A service credential: refused, a person decides.
        r = await c.post(f"{V3}/ledger/resolutions/{other_p}/confirm", json=body,
                         headers={"Authorization": f"Bearer {RUNTIME_TOK}"})
        assert r.status_code == 403, r.text
        # Contributor: not on the question they hold a number on; yes on the other.
        q = (await c.get(f"{V3}/ledger/resolution-queue", headers=cato)).json()
        slugs = {i["slug"] for i in q["items"]}
        assert f"FX-F1-other-{RUN}" in slugs and f"FX-F1-mine-{RUN}" not in slugs and q["hidden"] >= 1
        r = await c.post(f"{V3}/ledger/resolutions/{mine_p}/confirm", json=body, headers=cato)
        assert r.status_code == 403 and r.json()["detail"]["error"] == "entered_a_number", r.text
        # A cookie write from another site is refused before the route.
        r = await c.post(f"{V3}/ledger/resolutions/{other_p}/confirm", json=body,
                         headers={**cato, "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403 and r.json()["detail"]["error"] == "cross_site_refused"
        r = await c.post(f"{V3}/ledger/resolutions/{other_p}/confirm", json=body, headers=cato)
        assert r.status_code == 201, r.text
        conf = r.json()["record_id"]
        again = await c.post(f"{V3}/ledger/resolutions/{other_p}/confirm", json=body, headers=cato)
        assert again.status_code == 200 and again.json()["record_id"] == conf
        # Contributor may not correct; the admin may.
        corr = {"outcome": "no", "reason": "the report was about another event"}
        r = await c.post(f"{V3}/ledger/resolutions/{conf}/correct", json=corr, headers=cato)
        assert r.status_code == 403 and r.json()["detail"]["required_role"] == "admin"
        r = await c.post(f"{V3}/ledger/resolutions/{conf}/correct", json=corr, headers=adam)
        assert r.status_code == 201, r.text
        # The admin decides the contributor's own question.
        r = await c.post(f"{V3}/ledger/resolutions/{mine_p}/reject", json={"reason": "wrong event"}, headers=adam)
        assert r.status_code == 201, r.text
    finally:
        await c.aclose()
        await store.close()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT record_kind, actor, actor_role, supersedes_id::text AS sup FROM forecast_resolution_records "
            "WHERE question_id = ANY($1::uuid[]) ORDER BY recorded_at", [other_q, mine_q])
    acts = [(r["record_kind"], r["actor"], r["actor_role"]) for r in rows if r["record_kind"] != "proposal"]
    assert acts == [("confirmation", f"user:{cato_id}", "contributor"),
                    ("correction", f"user:{adam_id}", "admin"),
                    ("rejection", f"user:{adam_id}", "admin")]
    other_audit = await _audit_rows(pool, f"FX-F1-other-{RUN}")
    assert [a["action"] for a in other_audit] == ["forecast_resolution_confirm", "forecast_resolution_correct"]
    async with pool.acquire() as conn:
        unchained = await conn.fetchval("SELECT count(*) FROM descriptor_audit_log WHERE row_hash IS NULL")
    if not unchained:  # the shared test DB's chain is whole: these rows joined it
        assert all(a["chain_seq"] is not None and a["row_hash"] for a in other_audit)
    assert other_audit[0]["actor_id"] == f"user:{cato_id}" and other_audit[0]["to_version"] == conf


async def test_switch_off_the_bearer_confirms_as_its_stable_actor(migrated_pg, pool, monkeypatch) -> None:
    monkeypatch.delenv(S.SIGNIN_ENV, raising=False)
    async with pool.acquire() as conn:
        qid, rec = await _sealed_with_proposal(conn, f"FX-F1-off-{RUN}")
    store, c = await _client(_build_app(migrated_pg, monkeypatch))
    try:
        r = await c.post(f"{V3}/ledger/resolutions/{rec}/confirm", json={"outcome": "yes"},
                         headers={"Authorization": f"Bearer {ADMIN_TOK}"})
        assert r.status_code == 201, r.text
        assert r.json()["actor_role"] == "admin"
    finally:
        await c.aclose()
        await store.close()
    async with pool.acquire() as conn:
        actor = await conn.fetchval(
            "SELECT actor FROM forecast_resolution_records WHERE question_id = $1::uuid "
            "AND record_kind = 'confirmation'", qid)
    assert actor.startswith("operator:bearer:") and ADMIN_TOK not in actor


# ---------------------------------------------------------------------------
# A stored tree and its proof, on the real tables
# ---------------------------------------------------------------------------


async def test_a_stored_tree_proves_a_seal_through_the_route(migrated_pg, pool, monkeypatch) -> None:
    monkeypatch.delenv(S.SIGNIN_ENV, raising=False)
    day = date(2041, 1, 1) + timedelta(days=random.randint(0, 3000))
    async with pool.acquire() as conn:
        qid, _ = await _sealed_with_proposal(conn, f"FX-F1-proof-{RUN}")
        rows = await conn.fetch(fl.LEDGER_LEAVES_SQL)
        tree = [ap.AnchorLeaf(ap.HEAD_KIND, "analyst_f1", "f1" * 32)] + ap.leaves_from_rows(rows)
        root = ap.merkle_root([lf.digest for lf in tree]).hex()
        await conn.execute("DELETE FROM receipt_anchors WHERE day = $1", day)
        for cal in ("test-calendar-a", "test-calendar-b"):
            await conn.execute(
                "INSERT INTO receipt_anchors (day, root_hash, leaf_count, calendar, status) "
                "VALUES ($1, $2, $3, $4, 'pending')", day, root, len(tree), cal)
        assert await ap.store_tree(conn, day, root, tree, anchor_version="2026-11/f1-leaves") == "stored"
        assert await ap.store_tree(conn, day, root, tree, anchor_version="2026-11/f1-leaves") == "already_stored"
    store, c = await _client(_build_app(migrated_pg, monkeypatch))
    try:
        r = await c.get(f"{V3}/ledger/seal-proof", params={"question_id": qid, "day": day.isoformat()},
                        headers={"Authorization": f"Bearer {ADMIN_TOK}"})
        assert r.status_code == 200, r.text
        body = r.json()
    finally:
        await c.aclose()
        await store.close()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM receipt_anchors WHERE day = $1", day)
    q = body["question"]
    assert body["available"] and q["included"] and q["day"] == day.isoformat() and q["root"] == root
    assert q["recomputed"] and q["anchor_root_matches"]
    assert all(t["verified_here"] is False for t in q["timestamps"])
    path = [(s["side"], bytes.fromhex(s["hash"])) for s in q["path"]]
    assert ap.fold_path(fl.ledger_leaf("forecast_question", qid, q["content_sha256"]), path).hex() == root
    assert len(body["numbers"]) == 1 and body["numbers"][0]["included"]
