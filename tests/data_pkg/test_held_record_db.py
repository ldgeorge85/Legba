# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane DM-C — the held family on a migrated Postgres (the isolated test
stack only).

* the migration sorts after the DM-A / DM-B / DM-G migrations and applies on a
  fresh database (the session's ``migrated_pg``);
* the writer: one invented shadow record → assertions by class with the
  statuses the rules give, versions, a hash-chained status log, the cache,
  support pinned to a receipt version with its bytes label, a verdict link,
  premise links; idempotent per record; every stored type id is declared;
* the matching rule across records: a restatement link (no duplicate), a
  record not held made held by a later verified row, an absence in a later
  window as a new version;
* insert-only: UPDATE / DELETE refused on every table but the cache; a replay
  batch deletes by name; chain fields cannot be supplied;
* the chain: the database's hash equals the Python mirror; a tampered row is
  detected; the cache equals its rebuild, and a tampered cache is detected;
* the support guards: derived records are never evidence; a fact promoted
  from a statement can never support it;
* the reads: the light list's byte budget on long texts, filters, the detail.

Invented fixtures; every row written is removed after the module.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data import held_record_store as HS
from legba.data.config import PostgresConfig
from legba.data.held_record_chain import row_hash
from legba.data.held_record_reads import assertion_detail, chain_report, list_assertions
from legba.data.migrations import MIGRATIONS_DIR
from legba.data.registry.list_weights import LIST_ROUTES
from legba.data.type_registry import types
from tests.data_pkg.test_held_record import SIG, shadow_record

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

SRC = "source.test.dm_c_held"
NOW = datetime.now(timezone.utc)
URL = "https://news.dm-c.example.org/2026/11/northaven"
PAGE_TEXT = "Northaven port closed to tankers on 3 November, the Eastmark authority said."
_TABLES = ("assertion_support", "assertion_status", "assertion_status_log", "assertion_versions",
           "assertion_shadow_receipts", "assertions")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


async def _wipe(c) -> None:
    for t in _TABLES:
        await c.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
    try:
        for t in _TABLES:
            await c.execute(f"DELETE FROM {t}")
    finally:
        for t in _TABLES:
            await c.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")


@pytest_asyncio.fixture
async def world(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    await _wipe(conn)
    body = f"<html>{PAGE_TEXT}</html>"
    vid = await conn.fetchval(
        "INSERT INTO fetch_versions (address, url, sha256, hash_basis, role, first_source_id, last_source_id, "
        "status, content_type, size_bytes, object_ref, store_status, text_sha256, text_chars, extracted_text, "
        "first_seen_at, last_seen_at) VALUES ($1, $1, $2, 'entity', 'page', $3, $3, 200, 'text/html', $4, $5, "
        "'stored', $6, $7, $8, $9, $9) RETURNING id",
        URL + "#" + uuid4().hex, _sha(body + uuid4().hex), SRC, len(body), f"cas:sha256/{_sha(body)}",
        _sha(PAGE_TEXT), len(PAGE_TEXT), PAGE_TEXT, NOW)
    vsha = await conn.fetchval("SELECT sha256 FROM fetch_versions WHERE id = $1", vid)
    await conn.execute(
        "INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256) "
        "VALUES ('signal', $1, 'page', $2, $3)", UUID(SIG[0]), vid, vsha)
    yield {"conn": conn, "version_id": vid}
    await _wipe(conn)
    await conn.execute("ALTER TABLE fetch_version_records DISABLE TRIGGER fetch_version_records_insert_only")
    await conn.execute("DELETE FROM fetch_version_records WHERE version_id = $1", vid)
    await conn.execute("ALTER TABLE fetch_version_records ENABLE TRIGGER fetch_version_records_insert_only")
    await conn.execute("DELETE FROM fetch_versions WHERE id = $1", vid)
    await conn.close()


async def _write(c, rec, **kw):
    async with c.transaction():
        return await HS.write_shadow_record(c, rec, **kw)


async def _status(c):
    rows = await c.fetch("SELECT v.shadow_row, a.class, s.status FROM assertions a "
                         "JOIN assertion_status s ON s.assertion_id = a.id "
                         "JOIN assertion_versions v ON v.assertion_id = a.id AND v.version_no = 1")
    return {r["shadow_row"]: (r["class"], r["status"]) for r in rows}


# ---------------------------------------------------------------------------


async def test_the_migration_sorts_after_its_dependencies_and_lane_dm_g():
    names = sorted(p.name for p in MIGRATIONS_DIR.glob("20261115T*.sql"))
    mine = "20261115T1940_held_record.sql"
    assert mine in names
    for dep in ("20261115T1827_principals_origins_written_by.sql", "20261115T1828_citations_as_rows.sql",
                "20261115T1829_zzzz"):
        assert dep < mine
    body = (MIGRATIONS_DIR / mine).read_text()
    for word in ("graph_arcs", "ag_catalog", "legba_graph"):   # independent of DM-G's graph drop
        assert word not in body


async def test_one_record_lands_by_the_class_rules(world):
    c = world["conn"]
    res = await _write(c, shadow_record())
    assert res.written
    st = await _status(c)
    assert st == {"F1": ("statement", "held"), "F2": ("attributed", "held"), "F3": ("statement", "not_held"),
                  "A1": ("absence", "held"), "J1": ("assessment", "held"), "J2": ("assessment", "not_held")}
    # F6 restated F1 in the same record: a link, not a record
    links = await c.fetch("SELECT claimant_shadow_row, role, target_kind FROM assertion_support "
                          "WHERE claimant_shadow_row IS NOT NULL")
    assert [(r["claimant_shadow_row"], r["role"], r["target_kind"]) for r in links] == [
        ("shadow:101:F6", "restates", "assertion_version")]
    assert res.counts["restatement_prior_read_unresolved"] == 1          # no run_handed row for the run
    # the evidence row is pinned to the receipt version, labelled
    ev = await c.fetchrow("SELECT * FROM assertion_support WHERE signal_id = $1", UUID(SIG[0]))
    assert (ev["fetch_version_id"], ev["bytes_label"], ev["role"]) == (world["version_id"], "received_bytes",
                                                                      "evidence")
    bare = await c.fetchrow("SELECT * FROM assertion_support WHERE signal_id = $1", UUID(SIG[1]))
    assert bare["fetch_version_id"] is None and bare["bytes_label"] is None
    assert res.counts["evidence_with_bytes"] == 1 and res.counts["evidence_without_bytes"] == 2
    verdicts = await c.fetch("SELECT verdict, verdict_key FROM assertion_support WHERE role = 'verdict' ORDER BY 2")
    assert [r["verdict"] for r in verdicts] == ["supports", "supports", "partly"]
    premises = await c.fetch("SELECT target_basis FROM assertion_support WHERE role = 'premise'")
    assert len(premises) == 3 and {r["target_basis"] for r in premises} == {"derived"}
    # who: the rule wrote and decided; nothing names a person
    handles = {r["handle"] for r in await c.fetch(
        "SELECT DISTINCT p.handle FROM assertions a JOIN principals p ON p.id = a.written_by "
        "UNION SELECT DISTINCT p.handle FROM assertion_status_log l JOIN principals p ON p.id = l.caused_by")}
    assert handles == {"rule:held_record_writer"}
    # every stored type id is a declared assertion_type
    stored = {r["type_id"] for r in await c.fetch("SELECT DISTINCT type_id FROM assertions")}
    assert stored <= {d.id for d in types("assertion_type")}


async def test_the_writer_is_idempotent_per_record(world):
    c = world["conn"]
    assert (await _write(c, shadow_record())).written
    before = {t: await c.fetchval(f"SELECT count(*) FROM {t}") for t in _TABLES}
    again = await _write(c, shadow_record())
    assert not again.written
    assert {t: await c.fetchval(f"SELECT count(*) FROM {t}") for t in _TABLES} == before


async def test_the_matching_rule_across_records(world):
    c = world["conn"]
    await _write(c, shadow_record(101))
    second = copy.deepcopy(shadow_record(102))
    outs = second["record"]["outcomes"]
    outs[2]["verdict"] = "supports"                                         # F3 now verified
    outs[6]["detail"]["scope"]["window_end"] = "2026-11-06T00:00:00+00:00"  # A1 in a later window
    res = await _write(c, second)
    assert res.counts.get("assertions", 0) == 0          # nothing new: every row matched
    # F1 F2 F3 A1 J1 J2 match the table; F6 matches F1 inside the record; F3
    # (now verified) and J2 (its premises now held) become held
    assert res.counts["match:table"] == 6 and res.counts["match:in_record"] == 1
    assert res.counts["match:now_held"] == 2
    assert res.counts["match:new_version"] == 1
    st = await _status(c)
    assert st["F3"] == ("statement", "held")
    a1 = await c.fetchrow("SELECT s.status, v.version_no, v.reason_code FROM assertions a "
                          "JOIN assertion_status s ON s.assertion_id = a.id "
                          "JOIN assertion_versions v ON v.id = s.version_id WHERE a.class = 'absence'")
    assert (a1["status"], a1["version_no"], a1["reason_code"]) == ("held", 2, "later_window")
    assert await c.fetchval("SELECT count(*) FROM assertions") == 6
    links = await c.fetchval("SELECT count(*) FROM assertion_support WHERE claimant_shadow_row LIKE 'shadow:102:%'")
    assert links == res.counts["match:restatement_link"] >= 5


async def test_insert_only_and_the_replay_rollback(world):
    c = world["conn"]
    await _write(c, shadow_record())
    for t in ("assertions", "assertion_versions", "assertion_status_log", "assertion_support",
              "assertion_shadow_receipts"):
        with pytest.raises(asyncpg.PostgresError, match="insert-only"):
            await c.execute(f"UPDATE {t} SET batch = 'x'")
        with pytest.raises(asyncpg.PostgresError, match="insert-only"):
            await c.execute(f"DELETE FROM {t}")
    # chain fields are the database's
    aid, vid = await c.fetchrow("SELECT assertion_id, version_id FROM assertion_status LIMIT 1")
    with pytest.raises(asyncpg.PostgresError, match="set by the database"):
        await c.execute("INSERT INTO assertion_status_log (assertion_id, seq, status, version_id, reason_code, "
                        "caused_by) VALUES ($1, 99, 'held', $2, 'x', legba_principal('rule:x'))", aid, vid)
    # a replay batch is removable by name, and only it
    await _write(c, shadow_record(555), origin="replay", batch="replay_t1", lookups=HS.NoLookups())
    async with c.transaction():
        await c.execute("SET LOCAL legba.held_record_rollback = 'replay_t1'")
        await c.execute("DELETE FROM assertion_status WHERE batch = 'replay_t1'")
        for t in _TABLES[:-1] + ("assertions",):
            if t != "assertion_status":
                await c.execute(f"DELETE FROM {t} WHERE batch = 'replay_t1'")
    assert await c.fetchval("SELECT count(*) FROM assertions WHERE batch IS NOT NULL") == 0
    assert await c.fetchval("SELECT count(*) FROM assertions") == 6


async def test_the_chain_and_the_cache(world):
    c = world["conn"]
    await _write(c, shadow_record(101))
    second = shadow_record(102)
    second["record"]["outcomes"][2]["verdict"] = "supports"
    await _write(c, second)
    rows = await c.fetch("SELECT * FROM assertion_status_log ORDER BY assertion_id, seq")
    assert all(row_hash(dict(r)) == r["row_hash"] for r in rows)          # the database = the mirror
    assert max(r["seq"] for r in rows) == 2
    rep = await chain_report(c)
    assert rep["breaks"] == [] and rep["cache_differs"] == [] and rep["rows"] == len(rows)
    # tamper with one log row: detected at that row
    target = next(r for r in rows if r["seq"] == 2)
    await c.execute("ALTER TABLE assertion_status_log DISABLE TRIGGER assertion_status_log_insert_only")
    try:
        await c.execute("UPDATE assertion_status_log SET reason = 'edited' WHERE id = $1", target["id"])
        rep = await chain_report(c)
        assert [(b["assertion_id"], b["seq"], b["reason"]) for b in rep["breaks"]] == [
            (str(target["assertion_id"]), 2, "row_hash_mismatch")]
        await c.execute("UPDATE assertion_status_log SET reason = $2 WHERE id = $1", target["id"], target["reason"])
    finally:
        await c.execute("ALTER TABLE assertion_status_log ENABLE TRIGGER assertion_status_log_insert_only")
    # tamper with the cache: the rebuild names it
    await c.execute("UPDATE assertion_status SET status = 'withdrawn' WHERE assertion_id = $1", target["assertion_id"])
    rep = await chain_report(c)
    assert rep["breaks"] == [] and rep["cache_differs"] == [str(target["assertion_id"])]


async def test_the_support_guards(world):
    c = world["conn"]
    await _write(c, shadow_record())
    v1, v2 = [r["id"] for r in await c.fetch("SELECT id FROM assertion_versions ORDER BY id LIMIT 2")]
    # a derived record is never NEW evidence for another
    with pytest.raises(asyncpg.CheckViolationError):
        await c.execute("INSERT INTO assertion_support (claimant_version_id, role, target_kind, target_version_id, "
                        "written_by) VALUES ($1, 'evidence', 'assertion_version', $2, legba_principal('rule:t'))",
                        v1, v2)
    # a record never rests on itself
    with pytest.raises(asyncpg.PostgresError, match="itself"):
        await c.execute("INSERT INTO assertion_support (claimant_version_id, role, target_kind, target_version_id, "
                        "written_by) VALUES ($1, 'premise', 'assertion_version', $1, legba_principal('rule:t'))", v1)
    # a fact promoted from a statement can never support it (promotion itself is not built)
    stmt = await c.fetchval("SELECT assertion_id FROM assertion_versions WHERE id = $1", v1)
    fact = await c.fetchval(
        "INSERT INTO assertions (class, type_id, basis, subject_kind, subject_key, claim_key, promoted_from, "
        "written_by) VALUES ('statement', 'event', 'declared', 'target', 'desk_x', $1, $2, "
        "legba_principal('user:0b6c2f0e-1111-4c2a-9d3e-000000000001')) RETURNING id", _sha("fact"), stmt)
    fv = await c.fetchval(
        "INSERT INTO assertion_versions (assertion_id, version_no, reason_code, text, text_sha256, written_by) "
        "VALUES ($1, 1, 'first', 'Entered.', $2, legba_principal('rule:t')) RETURNING id", fact, _sha("Entered."))
    for role in ("evidence", "premise"):
        with pytest.raises(asyncpg.PostgresError, match="promoted"):
            await c.execute("INSERT INTO assertion_support (claimant_version_id, role, target_kind, "
                            "target_version_id, written_by) VALUES ($1, $2, 'assertion_version', $3, "
                            "legba_principal('rule:t'))", v1, role, fv)
    # ...while a declared entry may be evidence for ANOTHER record (DM-D decides when)
    await c.execute("INSERT INTO assertion_support (claimant_version_id, role, target_kind, target_version_id, "
                    "written_by) VALUES ($1, 'evidence', 'assertion_version', $2, legba_principal('rule:t'))", v2, fv)


async def test_the_light_list_keeps_its_budget_and_filters(world):
    c = world["conn"]
    long_text = "Northaven tanker traffic " * 120          # ~3 KB per row
    for i in range(50):
        rec = shadow_record(1000 + i, target=f"desk_t{i}")
        rec["record"]["outcomes"] = [
            {**rec["record"]["outcomes"][0], "text": f"{long_text} {i}"},
            rec["record"]["outcomes"][6]]
        await _write(c, rec)
    page = await list_assertions(c)
    budget = LIST_ROUTES["/api/v1/v3/record/assertions"].page_budget
    size = len(json.dumps({"available": True, **page}, default=str).encode())
    assert len(page["items"]) == 50 and size < budget, size
    assert all(it["text_truncated"] for it in page["items"] if it["class"] == "statement")
    full = await list_assertions(c, limit=5, full=True)
    # A record's two assertions share created_at (one transaction), so the
    # id tie-break (a random uuid) decides which comes first: read the newest
    # STATEMENT, not items[0] (it was the absence row about half the time).
    stmt = next(i for i in full["items"] if i["class"] == "statement")
    assert len(stmt["text"]) > 240 and "slots" in stmt
    nxt = await list_assertions(c, cursor=page["next_cursor"])
    assert nxt["items"] and not {i["id"] for i in nxt["items"]} & {i["id"] for i in page["items"]}
    only = await list_assertions(c, class_="absence", status="held", subject="desk_t3")
    assert [i["subject_key"] for i in only["items"]] == ["desk_t3"]


async def test_the_detail_read(world):
    c = world["conn"]
    await _write(c, shadow_record())
    aid = await c.fetchval("SELECT a.id::text FROM assertions a JOIN assertion_versions v ON v.assertion_id = a.id "
                           "WHERE v.shadow_row = 'F1'")
    d = await assertion_detail(c, aid)
    assert d["found"] and d["chain"]["ok"] and d["chain"]["cache_equals_rebuild"]
    assert [v["version_no"] for v in d["versions"]] == [1]
    assert [r["status"] for r in d["status_log"]] == ["held"] and d["status_log"][0]["caused_by"] == \
        "rule:held_record_writer"
    ev = [s for s in d["rests_on"] if s["role"] == "evidence"]
    assert [s["bytes"] for s in ev] == ["received_bytes"]
    used = {(s["role"], s["claimant_shadow_row"] or "version") for s in d["restated_or_used_by"]}
    assert ("restates", "shadow:101:F6") in used and ("premise", "version") in used
    assert (await assertion_detail(c, str(uuid4())))["found"] is False
