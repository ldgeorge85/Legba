# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The administrative audit log as a hash chain (migration 0248; audit A-06/A-07).

On the real driver and the real trigger: a normal insert chains; a tampered,
deleted or reordered row is detected (the tamper first disables the trigger,
as a database owner could); the trigger refuses edits and deletes of chained
rows and content edits without a chained redact event; the backfill chains
pre-existing rows in recorded order, keeps a row whose signature does not
verify as it is, and is idempotent; the daily anchor folds the chain head in.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.provenance import canonical_json
from legba.data.provenance._core import ZERO_HASH
from legba.data.registry import audit_chain as ac
from legba.data.registry.audit import AuditLogger
from legba.data.registry.events import audit_payload
from legba.data.registry.signing import SigningIdentity, sign_audit_payload

_TRIGGER = "trg_descriptor_audit_log_chain_guard"


def _identity() -> SigningIdentity:
    return SigningIdentity(signing_key=SigningKey.generate(), signer_did="did:legba:registry:chain-test")


@pytest.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    await c.execute("TRUNCATE TABLE descriptor_audit_log")
    try:
        yield c
    finally:
        await c.execute(f"ALTER TABLE descriptor_audit_log ENABLE TRIGGER {_TRIGGER}")
        await c.execute("TRUNCATE TABLE descriptor_audit_log")
        await c.close()


async def _record(conn, logger: AuditLogger, n: int, tag: str = "t") -> list:
    return [
        await logger.record(conn, actor_id="operator", namespace="target",
                            descriptor_id=f"{tag}_{i}", action="update",
                            from_version=f"v{i}", to_version=f"v{i + 1}",
                            change_summary={"i": i, "nested": {"x": [1, 2.5]}})
        for i in range(n)
    ]


async def _as_attacker(conn, sql: str, *args) -> None:
    await conn.execute(f"ALTER TABLE descriptor_audit_log DISABLE TRIGGER {_TRIGGER}")
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.execute(f"ALTER TABLE descriptor_audit_log ENABLE TRIGGER {_TRIGGER}")


async def test_a_normal_insert_chains(conn) -> None:
    ident = _identity()
    entries = await _record(conn, AuditLogger(ident), 3)
    assert [e.chain_seq for e in entries] == [1, 2, 3]
    rows = await conn.fetch("SELECT * FROM descriptor_audit_log ORDER BY chain_seq")
    assert rows[0]["prev_hash"] == ZERO_HASH
    assert rows[1]["prev_hash"] == rows[0]["row_hash"] == entries[0].row_hash
    assert rows[2]["prev_hash"] == rows[1]["row_hash"]
    rep = await ac.verify_audit_chain(conn, verify_key=ident.verify_key)
    assert rep.ok and rep.rows == 3 and rep.unchained == 0
    assert rep.signatures == {ac.SIG_VERIFIED: 3}
    assert rep.head == (3, rows[2]["row_hash"]) == await ac.chain_head(conn)


async def test_inserts_inside_an_outer_transaction_chain_in_order(conn) -> None:
    logger = AuditLogger(_identity())
    async with conn.transaction():
        await _record(conn, logger, 2, "tx")
    await _record(conn, logger, 1, "after")
    assert (await ac.verify_audit_chain(conn)).ok


@pytest.mark.parametrize("attack", ["tamper", "delete", "reorder", "insert"])
async def test_each_attack_is_detected(conn, attack) -> None:
    ident = _identity()
    e = await _record(conn, AuditLogger(ident), 4)
    if attack == "tamper":
        await _as_attacker(conn, "UPDATE descriptor_audit_log SET to_version = 'evil' WHERE id = $1",
                           e[1].id)
        expect = (2, "row content does not match its row_hash")
    elif attack == "delete":
        await _as_attacker(conn, "DELETE FROM descriptor_audit_log WHERE id = $1", e[1].id)
        expect = (3, "sequence gap or reorder")
    elif attack == "reorder":
        await _as_attacker(
            conn, "UPDATE descriptor_audit_log SET chain_seq = -chain_seq WHERE chain_seq IN (2, 3)")
        await _as_attacker(
            conn, "UPDATE descriptor_audit_log SET chain_seq = CASE chain_seq WHEN -2 THEN 3 "
            "ELSE 2 END WHERE chain_seq IN (-2, -3)")
        expect = (2, "prev_hash does not match the previous row's hash")
    else:  # a forged row slipped in with a valid-looking chain position
        await _as_attacker(
            conn, "UPDATE descriptor_audit_log SET chain_seq = chain_seq + 1000 WHERE chain_seq >= 3")
        await _as_attacker(
            conn, "UPDATE descriptor_audit_log SET chain_seq = chain_seq - 999 WHERE chain_seq > 1000")
        await conn.execute(
            "INSERT INTO descriptor_audit_log (id, occurred_at, actor_id, actor_role, namespace, "
            "descriptor_id, action, signed_payload, signer_did, chain_seq, prev_hash, row_hash) "
            "VALUES ($1, now(), 'x', 'operator', 'target', 'forged', 'update', '\\x00', 'd', 3, $2, $3)",
            uuid4(), e[1].row_hash, "f" * 64)
        expect = (3, "row content does not match its row_hash")
    rep = await ac.verify_audit_chain(conn, verify_key=ident.verify_key)
    assert not rep.ok
    assert (rep.first_break["chain_seq"], rep.first_break["reason"]) == expect


async def test_the_trigger_makes_chained_rows_insert_only(conn) -> None:
    e = await _record(conn, AuditLogger(_identity()), 2)
    for sql in ("UPDATE descriptor_audit_log SET to_version = 'x' WHERE id = $1",
                "UPDATE descriptor_audit_log SET row_hash = 'x' WHERE id = $1",
                "DELETE FROM descriptor_audit_log WHERE id = $1"):
        with pytest.raises(asyncpg.RaiseError):
            await conn.execute(sql, e[0].id)
    assert (await ac.verify_audit_chain(conn)).ok


async def _raw_unchained(conn, ident: SigningIdentity, when: datetime, tag: str):
    """A pre-0248 row: signed and inserted with no chain columns."""
    rid = uuid4()
    payload = audit_payload(action="register", family="target", descriptor_id=tag,
                            actor_id="operator", actor_role="operator",
                            change_summary={"tag": tag}, occurred_at=when.isoformat())
    await conn.execute(
        "INSERT INTO descriptor_audit_log (id, occurred_at, actor_id, actor_role, namespace, "
        "descriptor_id, action, change_summary, signed_payload, signer_did) "
        "VALUES ($1, $2, 'operator', 'operator', 'target', $3, 'register', $4::jsonb, $5, $6)",
        rid, when, tag, canonical_json({"tag": tag}).decode(), sign_audit_payload(ident, payload),
        ident.signer_did)
    return rid


async def test_backfill_chains_history_in_order_keeps_bad_signatures_and_is_idempotent(conn) -> None:
    ident, old_key = _identity(), _identity()
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    ids = [await _raw_unchained(conn, ident, t0 + timedelta(hours=h), f"h{h}") for h in (2, 0, 1)]
    bad = await _raw_unchained(conn, old_key, t0 + timedelta(hours=3), "june_other_key")
    # A write landing before the backfill stays unchained (no hole in the middle).
    late = (await _record(conn, AuditLogger(ident), 1, "late"))[0]
    assert late.chain_seq is None and late.row_hash is None

    plan = await ac.backfill(conn)                                  # dry-run
    assert (plan.applied, plan.already_chained, plan.to_chain, plan.first_seq) == (False, 0, 5, 1)
    assert await ac.unchained_count(conn) == 5

    # The trigger lets an unchained row be chained, never edited.
    with pytest.raises(asyncpg.RaiseError):
        await conn.execute("UPDATE descriptor_audit_log SET actor_id = 'x' WHERE id = $1", ids[0])

    plan = await ac.backfill(conn, apply=True)
    assert plan.applied and plan.to_chain == 5 and plan.head[0] == 5
    order = [r["id"] for r in await conn.fetch(
        "SELECT id FROM descriptor_audit_log ORDER BY chain_seq")]
    assert order == [ids[1], ids[2], ids[0], bad, late.id]          # recorded order
    rep = await ac.verify_audit_chain(conn, verify_key=ident.verify_key)
    assert rep.ok and rep.rows == 5
    assert rep.signature_not_verified_ids == [str(bad)]
    assert rep.signatures == {ac.SIG_VERIFIED: 4, ac.SIG_NOT_VERIFIED: 1}

    again = await ac.backfill(conn, apply=True)
    assert again.to_chain == 0 and again.already_chained == 5 and again.head == plan.head
    nxt = (await _record(conn, AuditLogger(ident), 1, "next"))[0]
    assert nxt.chain_seq == 6
    assert (await ac.verify_audit_chain(conn)).ok


async def test_redaction_is_an_appended_chained_event(conn) -> None:
    ident = _identity()
    logger = AuditLogger(ident)
    e = await _record(conn, logger, 3)
    with pytest.raises(asyncpg.RaiseError):                         # no event, no edit
        await conn.execute("UPDATE descriptor_audit_log SET actor_id = 'x' WHERE id = $1", e[1].id)
    ev = await ac.redact_row(conn, logger, row_id=e[1].id, reason="secret in actor_id",
                             replace={"actor_id": "bearer:abc"}, actor_id="system:test")
    assert ev.chain_seq == 4
    row = await conn.fetchrow("SELECT * FROM descriptor_audit_log WHERE id = $1", e[1].id)
    assert row["actor_id"] == "bearer:abc" and row["row_hash"] == e[1].row_hash
    rep = await ac.verify_audit_chain(conn, verify_key=ident.verify_key)
    assert rep.ok and rep.redacted_ids == [str(e[1].id)]
    assert rep.signatures == {ac.SIG_VERIFIED: 3, ac.SIG_REDACTED: 1}
    with pytest.raises(ValueError):
        await ac.redact_row(conn, logger, row_id=e[0].id, reason=" ",
                            replace={"actor_id": "x"}, actor_id="system:test")


async def test_the_daily_anchor_folds_in_the_chain_head(conn, migrated_pg, monkeypatch) -> None:
    from legba.data.analysts.deterministic_handlers import receipt_anchor as ra

    await _record(conn, AuditLogger(_identity()), 2)
    head = await ac.chain_head(conn)
    folded: list[list[bytes]] = []
    real_root = ra.merkle_root

    def _capture(leaves):
        folded.append(list(leaves))
        return real_root(leaves)

    async def _down(calendar, digest):
        raise RuntimeError("calendar unreachable in tests")

    monkeypatch.setattr(ra, "merkle_root", _capture)
    monkeypatch.setattr(ra, "_submit_digest", _down)
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    try:
        res = await ra.handle([], {}, SimpleNamespace(pg_pool=pool))
    finally:
        await pool.close()
    await conn.execute(
        "DELETE FROM receipt_anchors WHERE status = 'pending' AND root_hash = $1",
        res.finding.data["root_hash"])
    assert folded and folded[-1][-1] == ra.audit_chain_leaf(*head)
    data = res.finding.data
    assert data["audit_chain_head"] == {"chain_seq": head[0], "row_hash": head[1]}
    assert data["receipt_anchor_version"] == ra.RECEIPT_ANCHOR_VERSION
