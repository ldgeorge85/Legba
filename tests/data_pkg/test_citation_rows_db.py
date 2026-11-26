# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citations as rows on the real path (migrated Postgres, the
isolated test stack only).

* ``write_finding`` writes the finding and its rows in ONE transaction: the
  sentences, a typed support row per citation pinned to the receipt version
  the signal was read from (and labelled as ``byte_path`` labels it), the
  block members, a citation of a record that does not exist logged
  ``target_missing``, a marker with no entry logged, the handed list, and the
  receipt row with its counts;
* the switch off writes no row and leaves the finding as before; the refusal
  on refuses the finding with the rows (nothing is written);
* a failure inside the rows never costs the finding;
* the tables are insert-only: an UPDATE and a DELETE of a live row refuse; a
  backfill batch can be deleted, by name, and only it;
* the backfill derives the same rows from a finding's stored fields, skips a
  finding that has them, and rolls back by batch; its dry run reads only;
* the report SQL (both sources) and the Record sheet's support read (rows
  preferred, stored citations as the fallback).

Invented, neutral fixtures; every row this module writes is removed after it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data import citation_rows_backfill as BF
from legba.data import citation_rows_store as CS
from legba.data.citation_rows import HandedRef
from legba.data.citation_rows_audit import report_sql
from legba.data.citation_rows_reads import finding_support
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext, FindingPayload, write_finding

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

SRC = "source.test.dm_a_rows"
ANALYST = "an_dm_a_rows"
NOW = datetime.now(timezone.utc)
#: The "written before the dual write" finding's time: a fixed past instant
#: in a window only the backfill test reads.
OLD_AT = datetime(2003, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
URL = "https://news.dm-a.example.org/2026/03/velmara-harbour"
PAGE_TEXT = "Harbour traffic at Velmara stopped on 3 March. Cranes stood idle."
_TABLES = ("citation_support", "citation_unresolved", "run_handed", "finding_sentences",
           "finding_citation_receipts")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


async def _clean(c, finding_ids) -> None:
    ids = [UUID(str(f)) for f in finding_ids]
    for t in _TABLES:
        await c.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
    try:
        for t in _TABLES:
            await c.execute(f"DELETE FROM {t} WHERE finding_id = ANY($1::uuid[])", ids)
    finally:
        for t in _TABLES:
            await c.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
    await c.execute("DELETE FROM analyst_outputs WHERE id = ANY($1::uuid[]) OR analyst_id = $2", ids, ANALYST)


@pytest_asyncio.fixture
async def world(migrated_pg: PostgresConfig, monkeypatch):
    monkeypatch.delenv(CS.CITATION_ROWS_ENV, raising=False)
    monkeypatch.delenv(CS.REFUSE_UNRESOLVED_ENV, raising=False)
    conn = await asyncpg.connect(migrated_pg.dsn)
    made: dict = {"findings": []}

    async def signal(title: str) -> str:
        sid = uuid4()
        await conn.execute(
            "INSERT INTO signals (id, source_id, fetched_at, payload, content_hash, canonical_url, schema_uri) "
            "VALUES ($1, $2, $3, $4::jsonb, $5, $6, 'iglu:legba/signal/jsonschema/3-0-0')",
            sid, SRC, NOW, json.dumps({"title": title}), uuid4().hex, URL)
        return str(sid)

    body = f"<html>{PAGE_TEXT}</html>"
    vid = await conn.fetchval(
        "INSERT INTO fetch_versions (address, url, sha256, hash_basis, role, first_source_id, last_source_id, "
        "status, content_type, size_bytes, object_ref, store_status, text_sha256, text_chars, extracted_text, "
        "first_seen_at, last_seen_at) VALUES ($1, $1, $2, 'entity', 'page', $3, $3, 200, 'text/html', $4, $5, "
        "'stored', $6, $7, $8, $9, $9) RETURNING id",
        URL + "#" + uuid4().hex, _sha(body + uuid4().hex), SRC, len(body), f"cas:sha256/{_sha(body)}",
        _sha(PAGE_TEXT), len(PAGE_TEXT), PAGE_TEXT, NOW)
    vsha = await conn.fetchval("SELECT sha256 FROM fetch_versions WHERE id = $1", vid)
    received = await signal("Velmara harbour traffic stops")
    bare = await signal("A wire item with no receipt")
    await conn.execute(
        "INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256) "
        "VALUES ('signal', $1, 'page', $2, $3)", UUID(received), vid, vsha)
    prior = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs (id, kind, title, body, data, analyst_id, analyst_version, produced_at, "
        "derived_from, schema_uri, run_id) VALUES ($1, 'finding', 'Prior read', 'Earlier.', '{}'::jsonb, $2, 'v1', "
        "$3, '{}', 'iglu:legba/finding/jsonschema/1-0-0', $4)", prior, ANALYST, NOW - timedelta(days=1), uuid4())
    made.update(conn=conn, received=received, bare=bare, version_id=vid, version_sha=vsha, prior=str(prior))
    made["findings"].append(str(prior))
    yield made
    await _clean(conn, made["findings"])
    await conn.execute(
        "ALTER TABLE fetch_version_records DISABLE TRIGGER fetch_version_records_insert_only")
    await conn.execute("DELETE FROM fetch_version_records WHERE version_id = $1", vid)
    await conn.execute(
        "ALTER TABLE fetch_version_records ENABLE TRIGGER fetch_version_records_insert_only")
    await conn.execute("DELETE FROM fetch_versions WHERE id = $1", vid)
    await conn.execute("DELETE FROM signals WHERE source_id = $1", SRC)
    await conn.close()


def _payload(w, *, missing: str | None = None) -> FindingPayload:
    missing = missing or str(uuid4())
    body = (f"## Velmara\nTraffic stopped on 3 March [1]. A second wire agreed [2]. "
            f"The earlier read said so too [3]. A ledger member is gone [4]. Unknown [6].\nNo citation here.")
    return FindingPayload(
        title="Velmara harbour", body=body, confidence=0.6,
        data={"citations": [
            {"marker": "[1]", "signal_id": w["received"], "source_id": SRC, "source_text": PAGE_TEXT},
            {"marker": "[2]", "signal_id": w["bare"], "source_id": SRC, "snippet": "wire"},
            {"marker": "[3]", "ref_kind": "prior_read", "ref_id": w["prior"], "evidence_text": "Earlier."},
            {"marker": "[4]", "ref_kind": "window_ledger", "ledger_finding_ids": [w["prior"], missing],
             "evidence_text": "Ledger."},
        ]},
    )


def _ctx() -> AnalystContext:
    return AnalystContext(analyst_id=ANALYST, analyst_version="v1", run_id=uuid4(),
                          target_id="tgt_dm_a", target_version="abc123def456")


async def test_a_finding_and_its_rows_land_together(world):
    c = world["conn"]
    ctx = _ctx()
    handed = (HandedRef(1, "signal", world["received"]), HandedRef(2, "signal", world["bare"]),
              HandedRef(3, "finding", world["prior"]))
    row, dlq = await write_finding(c, analyst_ctx=ctx, payload=_payload(world), derived_from=[],
                                   handed_refs=handed)
    assert dlq is None
    world["findings"].append(str(row.id))
    rec = await c.fetchrow("SELECT * FROM finding_citation_receipts WHERE finding_id = $1", row.id)
    assert rec["run_id"] == ctx.run_id and rec["analyst_id"] == ANALYST and rec["origin"] == "live"
    assert rec["produced_at"] == row.produced_at
    assert (rec["sentences"], rec["cited_sentences"], rec["markers"]) == (7, 5, 5)
    assert rec["handed_recorded"] and rec["handed"] == 3
    sentences = await c.fetch("SELECT * FROM finding_sentences WHERE finding_id = $1 ORDER BY ordinal", row.id)
    assert [s["ordinal"] for s in sentences] == list(range(7)) and sentences[0]["kind"] == "heading"
    sup = {r["marker"] + ":" + (r["target_kind"]): r for r in await c.fetch(
        "SELECT * FROM citation_support WHERE finding_id = $1", row.id)}
    first = sup["[1]:signal"]
    assert str(first["signal_id"]) == world["received"] and first["role"] == "cites"
    assert first["fetch_version_id"] == world["version_id"] and first["fetch_link_role"] == "page"
    assert first["bytes_label"] == "received_bytes" and first["bytes_sha256"] == world["version_sha"]
    assert first["text_sha256"] == _sha(PAGE_TEXT) and first["shown_sha256"] == _sha(PAGE_TEXT)
    assert first["stance"] == "report" and first["source_id"] == SRC
    assert first["sentence_id"] == sentences[1]["id"]
    second = sup["[2]:signal"]
    assert second["fetch_version_id"] is None and second["bytes_label"] == "no_bytes_kept"
    assert sup["[3]:finding"]["role"] == "restates" and str(sup["[3]:finding"]["output_id"]) == world["prior"]
    ledger = await c.fetch("SELECT * FROM citation_support WHERE finding_id = $1 AND marker = '[4]'", row.id)
    assert len(ledger) == 2 and {r["via_block"] for r in ledger} == {"window_ledger"}
    unresolved = sorted((r["marker"], r["reason"]) for r in await c.fetch(
        "SELECT marker, reason FROM citation_unresolved WHERE finding_id = $1", row.id))
    assert unresolved == [("[4]", "target_missing"), ("[6]", "marker_not_in_citations")]
    assert rec["unresolved"] == 2 and rec["supports"] == 5
    handed_rows = {r["target_kind"]: r for r in await c.fetch(
        "SELECT * FROM run_handed WHERE run_id = $1", ctx.run_id)}
    assert [str(i) for i in handed_rows["signal"]["ids"]] == [world["received"], world["bare"]]
    assert list(handed_rows["signal"]["ordinals"]) == [1, 2]
    # "cited something it was never shown" is a query: one ledger member [4] was not handed.
    never_shown = await c.fetch(
        "SELECT s.marker FROM citation_support s WHERE s.finding_id = $1 AND NOT EXISTS ("
        " SELECT 1 FROM run_handed h WHERE h.run_id = s.run_id AND h.target_kind = s.target_kind"
        " AND coalesce(s.signal_id, s.output_id) = ANY(h.ids))", row.id)
    assert [r["marker"] for r in never_shown] == ["[4]"]  # the missing member; the prior read was handed
    # Who wrote the rows: the finding's seat, one principal on every row of the write.
    seat = await c.fetchval("SELECT id FROM principals WHERE handle = $1", f"agent:{ANALYST}@v1")
    assert seat is not None and rec["written_by"] == seat
    for t in _TABLES:
        col = "run_id" if t == "run_handed" else "finding_id"
        key = ctx.run_id if t == "run_handed" else row.id
        rows = await c.fetch(f"SELECT written_by FROM {t} WHERE {col} = $1", key)
        assert rows and {r["written_by"] for r in rows} == {seat}, t
    # The rows are written once: the writer is idempotent on the receipt.
    again = await CS.write_citation_rows(
        c, finding_id=row.id, body="x", citations=[], produced_at=row.produced_at)
    assert again is None
    # The Record sheet reads the rows.
    sheet = await finding_support(c, str(row.id))
    assert sheet["basis"] == "support_rows" and len(sheet["supports"]) == 5
    assert {s["markers"] for s in sheet["sentences"]} == {1}
    assert any(s["bytes"] == "received_bytes" and s["fetch_version_id"] == world["version_id"]
               for s in sheet["supports"])


async def test_switch_off_writes_no_rows_and_the_sheet_falls_back(world, monkeypatch):
    c = world["conn"]
    monkeypatch.setenv(CS.CITATION_ROWS_ENV, "0")
    row, _ = await write_finding(c, analyst_ctx=_ctx(), payload=_payload(world), derived_from=[])
    world["findings"].append(str(row.id))
    assert await c.fetchval("SELECT count(*) FROM finding_citation_receipts WHERE finding_id = $1", row.id) == 0
    sheet = await finding_support(c, str(row.id))
    assert sheet["basis"] == "citations_json" and sheet["found"]
    by = {(s["marker"], s["kind"]) for s in sheet["supports"]}
    assert ("[1]", "signal") in by and ("[3]", "finding") in by
    first = next(s for s in sheet["supports"] if s["marker"] == "[1]")
    assert first["bytes"] == "received_bytes" and first["fetch_version_id"] == world["version_id"]
    assert (await finding_support(c, str(uuid4())))["found"] is False


async def test_the_refusal_refuses_the_finding_with_its_rows(world, monkeypatch):
    c = world["conn"]
    monkeypatch.setenv(CS.REFUSE_UNRESOLVED_ENV, "1")
    ctx = _ctx()
    with pytest.raises(CS.UnresolvedCitationRefused) as err:
        await write_finding(c, analyst_ctx=ctx, payload=_payload(world), derived_from=[])
    assert err.value.reasons == {"marker_not_in_citations": 1, "target_missing": 1}
    assert await c.fetchval("SELECT count(*) FROM analyst_outputs WHERE run_id = $1", ctx.run_id) == 0
    assert await c.fetchval("SELECT count(*) FROM finding_citation_receipts WHERE run_id = $1", ctx.run_id) == 0


async def test_a_failure_in_the_rows_never_costs_the_finding(world, monkeypatch):
    c = world["conn"]

    async def boom(*a, **kw):
        await c.execute("SELECT 1/0")

    monkeypatch.setattr(CS, "write_citation_rows", boom)
    row, _ = await write_finding(c, analyst_ctx=_ctx(), payload=_payload(world), derived_from=[])
    world["findings"].append(str(row.id))
    assert await c.fetchval("SELECT count(*) FROM analyst_outputs WHERE id = $1", row.id) == 1
    assert await c.fetchval("SELECT count(*) FROM finding_citation_receipts WHERE finding_id = $1", row.id) == 0



async def test_the_rows_report_counts_a_citation_once_however_many_sentences_use_it(world):
    # Live: 11 findings, 360 support rows, 185 links in the json source.
    # A citation used by two sentences is two support rows and ONE link.
    c = world["conn"]
    p = _payload(world)
    body = p.body.replace(
        "A second wire agreed [2].",
        "A second wire agreed [2]. Traffic was still stopped on 4 March [1]. The read held [3].")
    row, _ = await write_finding(
        c, analyst_ctx=_ctx(), derived_from=[],
        payload=FindingPayload(title=p.title, body=body, confidence=p.confidence, data=p.data))
    world["findings"].append(str(row.id))
    assert await c.fetchval(
        "SELECT count(*) FROM citation_support WHERE finding_id = $1 AND marker = '[1]'", row.id) == 2
    at = await c.fetchval("SELECT produced_at FROM finding_citation_receipts WHERE finding_id = $1", row.id)
    lo, hi = at.isoformat(), (at + timedelta(microseconds=1)).isoformat()

    async def total(source: str) -> tuple[int, int, int]:
        r = next(x for x in await c.fetch(report_sql(source, day_start=lo, day_end=hi)) if x["cut"] == "total")
        return r["links"], r["joins_record"], r["joins_record_and_bytes"]

    assert await total("rows") == await total("json") == (5, 4, 1)

async def test_insert_only_and_the_backfill_by_batch(world):
    c = world["conn"]
    # A finding written before the dual write (inserted directly, as today's rows are).
    old = uuid4()
    payload = _payload(world)
    await c.execute(
        "INSERT INTO analyst_outputs (id, kind, title, body, data, analyst_id, analyst_version, produced_at, "
        "derived_from, schema_uri, run_id, target_id) VALUES ($1, 'finding', $2, $3, $4::jsonb, $5, 'v1', $6, '{}', "
        "'iglu:legba/finding/jsonschema/1-0-0', $7, 'tgt_dm_a')",
        old, payload.title, payload.body, json.dumps({"data": payload.data}), ANALYST,
        OLD_AT, uuid4())
    world["findings"].append(str(old))
    # A window no other test writes into: the dry run and the batches count
    # EVERY row-less finding in it, and a shuffled suite can run tests whose
    # findings sat at NOW - 2 h before this one.
    since, until = OLD_AT - timedelta(minutes=1), OLD_AT + timedelta(minutes=1)
    dry = await BF.dry_run(c, since=since, until=until)
    assert dry["tables_present"] and dry["pending_findings"] == 1 and dry["sampled"] == 1
    assert dry["sample"]["supports"] == 5 and dry["sample"]["unresolved_by_reason"] == {"marker_not_in_citations": 1}
    assert await c.fetchval("SELECT count(*) FROM finding_citation_receipts WHERE finding_id = $1", old) == 0
    out = await BF.apply_batches(c, batch_id="bf-dm-a-test", since=since, until=until, batch_size=10)
    assert out["findings_written"] == 1 and out["batches"] == 1
    rec = await c.fetchrow("SELECT * FROM finding_citation_receipts WHERE finding_id = $1", old)
    assert rec["origin"] == "backfill" and rec["backfill_batch"] == "bf-dm-a-test" and not rec["handed_recorded"]
    assert rec["supports"] == 5 and rec["unresolved"] == 2
    # A backfill row names the backfill rule, never the finding's seat.
    bf_rule = await c.fetchval("SELECT id FROM principals WHERE handle = 'rule:citation_rows_backfill'")
    assert bf_rule is not None and rec["written_by"] == bf_rule
    assert {r["written_by"] for r in await c.fetch(
        "SELECT written_by FROM citation_support WHERE finding_id = $1", old)} == {bf_rule}
    again = await BF.apply_batches(c, batch_id="bf-dm-a-test2", since=since, until=until)
    assert again["findings_written"] == 0 and again["batches"] == 0
    # Insert-only: no edit, no delete of a row, live or backfill, without the batch named.
    with pytest.raises(asyncpg.RestrictViolationError):
        await c.execute("UPDATE citation_support SET role = 'cites' WHERE finding_id = $1", old)
    with pytest.raises(asyncpg.RestrictViolationError):
        await c.execute("UPDATE finding_citation_receipts SET written_by = NULL WHERE finding_id = $1", old)
    with pytest.raises(asyncpg.RestrictViolationError):
        await c.execute("DELETE FROM finding_sentences WHERE finding_id = $1", old)
    # The report reads the rows (the day window holds the backfilled finding).
    sql = report_sql("rows", day_start=since.isoformat(), day_end=until.isoformat())
    total = next(r for r in await c.fetch(sql) if r["cut"] == "total")
    assert (total["links"], total["joins_record"], total["joins_record_and_bytes"]) == (5, 4, 1)
    jsql = report_sql("json", day_start=since.isoformat(), day_end=until.isoformat())
    jtotal = next(r for r in await c.fetch(jsql) if r["cut"] == "total")
    assert (jtotal["links"], jtotal["joins_record"], jtotal["joins_record_and_bytes"]) == (5, 4, 1)
    # Roll the batch back, by name.
    gone = await BF.rollback_batch(c, "bf-dm-a-test")
    assert gone["finding_citation_receipts"] == 1 and gone["citation_support"] == 5
    assert await c.fetchval("SELECT count(*) FROM finding_sentences WHERE finding_id = $1", old) == 0


async def test_written_by_references_principals_and_its_migration_runs_first(migrated_pg: PostgresConfig):
    # Convention on these tables: written_by uuid REFERENCES
    # principals(id), the principals migration (…T1827) applied before this
    # migration (…T1828) on a fresh database.
    c = await asyncpg.connect(migrated_pg.dsn)
    try:
        ledger = {r["name"]: r["applied_at"] for r in await c.fetch(
            "SELECT name, applied_at FROM legba_data_migrations WHERE name LIKE '20261115T18%'")}
        assert ledger["20261115T1827_principals_origins_written_by.sql"] \
            <= ledger["20261115T1828_citations_as_rows.sql"]
        for t in _TABLES:
            typ = await c.fetchval(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = $1 AND column_name = 'written_by'", t)
            assert typ == "uuid", t
            ref = await c.fetchval(
                "SELECT confrelid::regclass::text FROM pg_constraint "
                "WHERE conrelid = $1::regclass AND contype = 'f' "
                "AND conkey = ARRAY[(SELECT attnum FROM pg_attribute "
                "                    WHERE attrelid = $1::regclass AND attname = 'written_by')]::int2[]", t)
            assert ref == "principals", t
    finally:
        await c.close()
