# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane D-1 — source drift on the real path against a migrated Postgres (the
data_pkg session database, isolated test stack only).

Covered: a drift row classified and its dependants flagged ONCE (a second
pass writes nothing), with the cited span checked against both texts;
cosmetic change counted and never flagged; the dependant walk bounded (cap
and depth, recorded); the flag lifecycle and its trigger (identity columns
never change, state only forward, never deleted; classifications
insert-only); the re-derive request (one pending per dependant); the five
tools through the REAL binding (``action_pack_invocations``); the read route
over the real tables; the ``evidence_drift`` alert through ``raise_alert``
(one episode per source, logged not paged, the daily roll-up once); the
drift re-check batch's selection (priority, back-off, licence refusal, same
cap); the slice mark (off: no read; on: the row stamped).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
import yaml

from tests.data_pkg import _drift_fixtures as fx
from tests.runtime._ingest_fakes import REPO

pytestmark = pytest.mark.asyncio

SRC = "source.test.d1_news"
HOST = "news.d1.example.org"
NOW = datetime.now(timezone.utc)


def _addr(path: str, host: str = HOST) -> str:
    return f"https://{host}/{path}"


def _hash(address: str) -> str:
    return hashlib.sha256(address.encode()).hexdigest()


#: D-2's tables first (they reference the flags and classifications).
_DRIFT_TABLES = ("evidence_drift_would_auto_close", "evidence_drift_auto_closes", "evidence_drift_claim_effects", "evidence_drift_assessments",
                 "evidence_drift_flags", "evidence_drift_classifications")


async def _clean(c) -> None:
    for t in _DRIFT_TABLES:
        await c.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
    await c.execute("DELETE FROM evidence_rederive_requests")
    for t in _DRIFT_TABLES:
        await c.execute(f"DELETE FROM {t}")
    for t in _DRIFT_TABLES:
        await c.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
    await c.execute("DELETE FROM ingestion_request_records")
    await c.execute("DELETE FROM ingestion_requests")
    await c.execute("ALTER TABLE fetch_version_records DISABLE TRIGGER fetch_version_records_insert_only")
    await c.execute("DELETE FROM fetch_version_records")
    await c.execute("ALTER TABLE fetch_version_records ENABLE TRIGGER fetch_version_records_insert_only")
    await c.execute("DELETE FROM fetch_drift_observations")
    await c.execute("DELETE FROM fetch_version_tombstones")
    await c.execute("DELETE FROM fetch_versions")
    await c.execute("DELETE FROM output_consumption WHERE context = 'd1_test'")
    await c.execute("DELETE FROM analyst_outputs WHERE analyst_id LIKE 'test_d1%'")
    await c.execute("DELETE FROM journal_entries WHERE analyst_id = 'test_d1'")
    await c.execute("DELETE FROM evidence_archive WHERE signal_id IN "
                    "(SELECT id FROM signals WHERE source_id = $1)", SRC)
    await c.execute("DELETE FROM signals WHERE source_id = $1", SRC)
    await c.execute("DELETE FROM source_credibility WHERE source_host LIKE '%.d1.example.org'")
    await c.execute("DELETE FROM alert_events WHERE kind = 'evidence_drift'")
    await c.execute("DELETE FROM alerts WHERE kind = 'evidence_drift'")


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=6)
    async with p.acquire() as c:
        await _clean(c)
    yield p
    # The drift tables reference fetch_versions / fetch_drift_observations:
    # leave them empty so other files' table resets keep working.
    async with p.acquire() as c:
        await _clean(c)
    await p.close()


async def _version(conn, address: str, text: str | None, *, at: datetime, role: str = "archiver") -> tuple[int, str]:
    sha = hashlib.sha256(f"{address}|{text}|{at.isoformat()}".encode()).hexdigest()
    vid = await conn.fetchval(
        "INSERT INTO fetch_versions (address, url, sha256, hash_basis, role, status, size_bytes, "
        "store_status, text_sha256, text_chars, extracted_text, first_seen_at, last_seen_at) "
        "VALUES ($1, $1, $2, 'entity', $3, 200, $4, 'not_stored', $5, $6, $7, $8, $8) RETURNING id",
        address, sha, role, len(text or ""),
        None if text is None else hashlib.sha256(text.encode()).hexdigest(),
        None if text is None else len(text), text, at)
    return int(vid), sha


async def _drift(conn, address: str, earlier: tuple[int, str], later: tuple[int, str] | None, *,
                 at: datetime, gone: bool = False) -> int:
    return int(await conn.fetchval(
        "INSERT INTO fetch_drift_observations (address, source_id, observed_by, earlier_version_id, "
        "later_version_id, earlier_fetched_at, later_fetched_at, earlier_sha256, later_sha256, "
        "later_status, later_gone) VALUES ($1, $2, 'archiver', $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
        address, SRC, earlier[0], None if later is None else later[0], at - timedelta(days=2), at,
        earlier[1], None if later is None else later[1], 404 if gone else 200, gone))


async def _signal(conn, address: str, sha: str, *, fetched: datetime | None = None) -> uuid.UUID:
    sid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, fetched_at, payload, content_hash, canonical_url, "
        "canonical_url_hash) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)",
        sid, SRC, fetched or NOW - timedelta(days=3), json.dumps({"title": "d1 signal"}),
        uuid.uuid4().hex, address, _hash(address))
    await conn.execute("INSERT INTO evidence_archive (signal_id, status, sha256) VALUES ($1, 'archived', $2)",
                       sid, sha)
    return sid


async def _output(conn, *, kind: str = "finding", cites: list[uuid.UUID] = (), span: str | None = None,
                  derived_from: list[uuid.UUID] = (), superseded: bool = False,
                  analyst: str = "test_d1") -> uuid.UUID:
    oid = uuid.uuid4()
    citations = [{"signal_id": str(s), "marker": f"[{i + 1}]", "snippet": span} for i, s in enumerate(cites)]
    await conn.execute(
        "INSERT INTO analyst_outputs (id, kind, title, data, analyst_id, schema_uri, derived_from, "
        "superseded_by) VALUES ($1, $2, 'd1', $3::jsonb, $4, 'iglu:legba/test/jsonschema/1-0-0', $5, $6)",
        oid, kind, json.dumps({"data": {"citations": citations}}), analyst,
        list(derived_from) or list(cites), uuid.uuid4() if superseded else None)
    return oid


async def _journal(conn, refs: list[uuid.UUID]) -> uuid.UUID:
    jid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO journal_entries (id, entry_kind, title, body, claims, cited_substrate_refs, "
        "honesty_flags, period_start, period_end, produced_at, analyst_id, analyst_version) "
        "VALUES ($1, 'note', 'd1', 'd1', '[]'::jsonb, $2::uuid[], '{}'::text[], now(), now(), now(), "
        "'test_d1', 'v1')", jid, refs)
    return jid


async def _world(conn, kind: str = "substantive_edit", *, path: str = "toll") -> dict[str, Any]:
    """One page with an earlier and a later version of ``kind``, a signal
    archived from the earlier copy, a finding citing it with the toll
    sentence, a journal entry citing it, a composition derived from the
    finding and a consumer of the finding."""
    address = _addr(path)
    earlier_text, later_text = fx.PAIRS[kind]
    e = await _version(conn, address, earlier_text, at=NOW - timedelta(days=2))
    lat = await _version(conn, address, later_text, at=NOW - timedelta(hours=1))
    sig = await _signal(conn, address, e[1])
    finding = await _output(conn, cites=[sig], span=fx.CITED_SPAN)
    journal = await _journal(conn, [sig])
    comp = await _output(conn, kind="composition", derived_from=[finding], analyst="test_d1_comp")
    consumer = uuid.uuid4()
    await conn.execute("INSERT INTO output_consumption (consumer_id, consumed_id, consumer_kind, context) "
                       "VALUES ($1, $2, 'journal', 'd1_test')", consumer, finding)
    inputs_only = await _output(conn, cites=[], derived_from=[sig], analyst="test_d1_reader")
    drift = await _drift(conn, address, e, lat, at=NOW - timedelta(hours=1))
    return dict(address=address, earlier=e, later=lat, signal=sig, finding=finding, journal=journal,
                comp=comp, consumer=consumer, inputs_only=inputs_only, drift=drift)


# ---------------------------------------------------------------------------
# classification + flags
# ---------------------------------------------------------------------------

async def test_a_substantive_drift_flags_each_dependant_once_with_the_span_checked(pool) -> None:
    from legba.data.evidence_drift_store import classify_pending

    async with pool.acquire() as conn:
        w = await _world(conn)
    rep = await classify_pending(pool, limit=10)
    assert (rep.classified, rep.by_class) == (1, {"substantive_edit": 1})
    async with pool.acquire() as conn:
        cls = await conn.fetchrow("SELECT * FROM evidence_drift_classifications WHERE drift_id = $1", w["drift"])
        flags = {(r["dependant_kind"], r["dependant_id"]): r for r in await conn.fetch(
            "SELECT * FROM evidence_drift_flags WHERE drift_id = $1", w["drift"])}
    assert (cls["drift_class"], cls["rule"]) == ("substantive_edit", "critical_token_changed")
    assert json.loads(cls["evidence"])["critical"]["numbers"] == ["-19", "+17"]
    assert set(flags) == {("signal", str(w["signal"])), ("finding", str(w["finding"])),
                          ("journal_entry", str(w["journal"])), ("derived_output", str(w["comp"])),
                          ("derived_output", str(w["consumer"]))}
    assert (cls["flags_written"], cls["dependants_found"], cls["inputs_only"], cls["walk_depth"]) == (5, 5, 1, 2)
    f = flags[("finding", str(w["finding"]))]
    assert (f["depth"], f["cited_span"], f["span_in_earlier"], f["span_in_later"]) == \
        (1, fx.CITED_SPAN, True, False)
    assert flags[("signal", str(w["signal"]))]["depth"] == 0
    assert flags[("derived_output", str(w["comp"]))]["depth"] == 2
    assert all(r["state"] == "open" and r["drift_class"] == "substantive_edit" for r in flags.values())
    # idempotent: a second pass finds nothing; a forced re-classify writes nothing
    again = await classify_pending(pool, limit=10)
    assert again.classified == 0
    from legba.data.evidence_drift_store import classify_drift_row

    async with pool.acquire() as conn:
        d = await conn.fetchrow("SELECT * FROM fetch_drift_observations WHERE id = $1", w["drift"])
        s = await classify_drift_row(conn, d, read_titles=False)
        n = await conn.fetchval("SELECT count(*) FROM evidence_drift_flags")
        nc = await conn.fetchval("SELECT count(*) FROM evidence_drift_classifications")
        # the dependants were never touched
        body = await conn.fetchval("SELECT data FROM analyst_outputs WHERE id = $1", w["finding"])
    assert (s["flags_written"], n, nc) == (0, 5, 1)
    assert json.loads(body)["data"]["citations"][0]["snippet"] == fx.CITED_SPAN


@pytest.mark.parametrize("kind", ["cosmetic", "appended", "minor_edit"])
async def test_cosmetic_classes_are_counted_and_never_flagged(pool, kind) -> None:
    from legba.data.evidence_drift_store import classify_pending

    async with pool.acquire() as conn:
        w = await _world(conn, kind)
    rep = await classify_pending(pool)
    assert rep.by_class == {kind: 1} and rep.flags_written == 0
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT drift_class, flags_written, dependants_found "
                                  "FROM evidence_drift_classifications WHERE drift_id = $1", w["drift"])
        n = await conn.fetchval("SELECT count(*) FROM evidence_drift_flags")
    assert (row["drift_class"], row["flags_written"], row["dependants_found"], n) == (kind, 0, 0, 0)


async def test_a_page_gone_flags_its_dependants(pool) -> None:
    from legba.data.evidence_drift_store import classify_pending

    async with pool.acquire() as conn:
        address = _addr("gone")
        e = await _version(conn, address, fx.EARLIER, at=NOW - timedelta(days=2))
        sig = await _signal(conn, address, e[1])
        await _output(conn, cites=[sig], span=fx.CITED_SPAN)
        await _drift(conn, address, e, None, at=NOW, gone=True)
    rep = await classify_pending(pool)
    assert rep.by_class == {"gone": 1} and rep.flags_written == 2


async def test_the_dependant_walk_is_bounded_and_records_its_depth(pool) -> None:
    from legba.data.evidence_drift_walk import walk_dependants

    async with pool.acquire() as conn:
        w = await _world(conn)
        for _ in range(6):
            await _output(conn, cites=[w["signal"]], span=fx.CITED_SPAN)
        capped = await walk_dependants(conn, w["earlier"][0], max_dependants=4)
        records_only = await walk_dependants(conn, w["earlier"][0], max_depth=0)
        one = await walk_dependants(conn, w["earlier"][0], max_depth=1)
    assert len(capped.dependants) == 4 and capped.truncated is True
    assert [d.kind for d in records_only.dependants] == ["signal"] and records_only.depth_reached == 0
    assert one.depth_reached == 1 and {d.kind for d in one.dependants} == {"signal", "finding", "journal_entry"}
    assert sum(d.kind == "finding" for d in one.dependants) == 7 and not one.truncated


async def test_a_record_linked_through_fetch_version_records_is_a_dependant(pool) -> None:
    from legba.data.evidence_drift_walk import walk_dependants

    async with pool.acquire() as conn:
        address = _addr("linked")
        e = await _version(conn, address, fx.EARLIER, at=NOW - timedelta(days=2), role="page")
        rid = uuid.uuid4()
        await conn.execute("INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256) "
                           "VALUES ('observation', $1, 'page', $2, $3)", rid, e[0], e[1])
        walk = await walk_dependants(conn, e[0])
    assert [(d.kind, d.id, d.record_version_id) for d in walk.dependants] == [("observation", str(rid), e[0])]


# ---------------------------------------------------------------------------
# the lifecycle, the guard, re-derive
# ---------------------------------------------------------------------------

async def test_the_flag_lifecycle_moves_forward_only_and_never_rewrites(pool) -> None:
    from legba.data.evidence_drift_store import (
        classify_pending, request_rederive, resolve_flag, review_flag,
    )

    async with pool.acquire() as conn:
        w = await _world(conn)
    await classify_pending(pool)
    async with pool.acquire() as conn:
        fid = await conn.fetchval("SELECT id FROM evidence_drift_flags WHERE dependant_kind = 'finding'")
        other = await conn.fetchval("SELECT id FROM evidence_drift_flags WHERE dependant_kind = 'journal_entry'")
    assert await review_flag(pool, fid, principal="person:analyst", note="read both versions") == "reviewed"
    assert await review_flag(pool, fid, principal="person:analyst") is None
    assert await resolve_flag(pool, fid, principal="person:analyst", resolution="dependant_corrected",
                              note="toll now 17") == "resolved"
    assert await resolve_flag(pool, fid, principal="x", resolution="dependant_withdrawn") is None
    # straight from open to resolved fills the review fields too
    assert await resolve_flag(pool, other, principal="analyst::a", resolution="unchanged_in_substance") == "resolved"
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM evidence_drift_flags WHERE id = $1", other)
        assert (row["reviewed_by"], row["resolved_by"], row["resolution"]) == \
            ("analyst::a", "analyst::a", "unchanged_in_substance")
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await conn.execute("UPDATE evidence_drift_flags SET dependant_id = 'x' WHERE id = $1", fid)
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await conn.execute("UPDATE evidence_drift_flags SET state = 'open', resolution = NULL, "
                               "resolved_at = NULL, resolved_by = NULL WHERE id = $1", fid)
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await conn.execute("DELETE FROM evidence_drift_flags WHERE id = $1", fid)
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await conn.execute("UPDATE evidence_drift_classifications SET rule = 'x'")
    with pytest.raises(ValueError):
        await resolve_flag(pool, fid, principal="p", resolution="deleted")
    first = await request_rederive(pool, fid, requested_by="analyst::a", note="re-read the toll")
    second = await request_rederive(pool, fid, requested_by="analyst::b")
    assert first["status"] == "pending" and first["duplicate"] is False
    assert second == {"status": "pending", "id": first["id"], "duplicate": True}
    assert (await request_rederive(pool, 10**9, requested_by="a"))["status"] == "unknown_flag"
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM evidence_rederive_requests WHERE dependant_kind = 'finding'")
    assert (row["dependant_kind"], row["dependant_id"], row["later_version_id"], row["state"]) == \
        ("finding", str(w["finding"]), w["later"][0], "pending")


# ---------------------------------------------------------------------------
# tools through the real binding
# ---------------------------------------------------------------------------

def _pack():
    from legba.data.schemas.action_pack import ActionPack

    body = yaml.safe_load((REPO / "descriptors" / "action_pack_evidence_drift.yaml").read_text())
    body["identity"]["version"] = "d" * 16
    return ActionPack.model_validate(body, strict=False)


def _binding(pool, analyst_id: str):
    from legba.data.analysts.agency import Agency, AgencyToolBinding, ToolContext
    from legba.data.analysts.agency.binding import GLOBAL_SCOPE
    from legba.data.analysts.agency.tools import WritebackContext
    from legba.data.provenance import AnalystContext

    wb = WritebackContext(pg_pool=pool, analyst_ctx=AnalystContext(
        analyst_id=analyst_id, analyst_version="0" * 16, run_id=uuid.uuid4()))
    return AgencyToolBinding(
        agency=Agency(), pack=_pack(), pg_pool=pool, tool_context=ToolContext(writeback=wb),
        analyst_grants=[{"pack_id": "evidence_drift"}], target_allows=[{"pack_id": "evidence_drift"}],
        scope=GLOBAL_SCOPE, requested_by=f"analyst::{analyst_id}", budget_account="evidence_drift",
    )


async def test_the_five_tools_through_the_real_binding(pool, monkeypatch) -> None:
    from legba.data.analysts.agency.evidence_drift_tools import (
        EVIDENCE_DRIFT_TOOLS, register_evidence_drift_tools,
    )
    from legba.data.evidence_drift_store import classify_pending

    class _Reg:
        def __init__(self) -> None:
            self.names: list[str] = []

        def register(self, name, _fn):
            self.names.append(name)

    reg = _Reg()
    register_evidence_drift_tools(reg)
    assert tuple(reg.names) == EVIDENCE_DRIFT_TOOLS == tuple(t.name for t in _pack().tools)

    async with pool.acquire() as conn:
        w = await _world(conn)
        marker = {r["id"] for r in await conn.fetch("SELECT id FROM action_pack_invocations")}
    await classify_pending(pool)
    b = _binding(pool, "test_d1_agent")
    vers = await b.run_tool("evidence_versions", {"record_id": str(w["signal"])})
    assert vers.admitted is True and vers.tool_result.status == "completed", vers.tool_result.error
    out = vers.tool_result.output
    assert [v["id"] for v in out["versions"]] == [w["earlier"][0], w["later"][0]]
    assert out["transitions"][0]["class"] == "substantive_edit" and out["transitions"][0]["recorded"] is True

    diff = await b.run_tool("evidence_diff", {"address": w["address"]})
    d = diff.tool_result.output
    assert d["class"] == "substantive_edit" and d["critical"]["numbers"] == ["-19", "+17"]
    assert "rose to 19" in d["passages"][0]["earlier"] and "rose to 17" in d["passages"][0]["later"]

    async with pool.acquire() as conn:
        fid = await conn.fetchval("SELECT id FROM evidence_drift_flags WHERE dependant_kind = 'finding'")
    red = await b.run_tool("request_rederive", {"flag_id": fid, "note": "toll revised"})
    assert red.tool_result.output["status"] == "pending"
    bad = await b.run_tool("review_drift_flag", {"flag_id": fid, "outcome": "deleted", "note": "x"})
    assert bad.tool_result.status == "failed"
    rev = await b.run_tool("review_drift_flag", {"flag_id": fid, "outcome": "dependant_corrected",
                                                 "note": "the finding now cites 17"})
    assert rev.tool_result.output["state"] == "resolved"

    monkeypatch.delenv("LEGBA_INTAKE_AGENT_REQUESTS", raising=False)
    off = await b.run_tool("request_evidence_reread", {"address": w["address"]})
    assert off.tool_result.status == "failed" and "intake_agent_requests_off" in off.tool_result.error

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT resolved_by FROM evidence_drift_flags WHERE id = $1", fid)
        rq = await conn.fetchrow("SELECT requested_by FROM evidence_rederive_requests "
                                 "WHERE dependant_kind = 'finding'")
        invs = await conn.fetch("SELECT id, pack_id, tool_name, outcome FROM action_pack_invocations")
    assert row["resolved_by"] == rq["requested_by"] == "analyst::test_d1_agent"
    new = {(r["pack_id"], r["tool_name"], r["outcome"]) for r in invs if r["id"] not in marker}
    for tool in ("evidence_versions", "evidence_diff", "request_rederive", "review_drift_flag"):
        assert ("evidence_drift", tool, "completed") in new, new


# ---------------------------------------------------------------------------
# the route, the alert, the batch, the slice mark
# ---------------------------------------------------------------------------

async def test_the_read_route_over_the_real_tables(pool) -> None:
    from types import SimpleNamespace

    from fastapi import FastAPI

    from legba.data.evidence_drift_store import classify_pending
    from legba.data.registry.api import require_bearer
    from legba.data.registry.evidence_drift_api import build_evidence_drift_router

    async with pool.acquire() as conn:
        w = await _world(conn)
        await _world(conn, "cosmetic", path="furniture")
    await classify_pending(pool)
    app = FastAPI()
    app.include_router(build_evidence_drift_router(SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))),
                       prefix="/api/v1/v3")
    app.dependency_overrides[require_bearer] = lambda: "test"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        summary = (await client.get("/api/v1/v3/evidence/drift?window_hours=24")).json()
        flags = (await client.get("/api/v1/v3/evidence/drift/flags?state=open")).json()
        hist = (await client.get("/api/v1/v3/evidence/drift/history", params={"address": w["address"]})).json()
        editors = (await client.get("/api/v1/v3/evidence/drift/editors")).json()
    assert summary["available"] is True
    assert summary["by_class"] == {"substantive_edit": 1, "cosmetic": 1}
    assert summary["by_source"] == {SRC: {"substantive_edit": 1, "cosmetic": 1}}
    assert summary["flags"] == {"open": 5} and summary["open_flags_total"] == 5
    finding = next(f for f in flags["flags"] if f["dependant_kind"] == "finding")
    assert finding["span_in_later"] is False and finding["critical"]["numbers"] == ["-19", "+17"]
    assert [v["id"] for v in hist["versions"]] == [w["earlier"][0], w["later"][0]]
    assert hist["changes"][0]["drift_class"] == "substantive_edit"
    assert editors["sources"][0]["subject"] == SRC
    assert (editors["sources"][0]["substantive_or_worse"], editors["sources"][0]["classified"]) == (1, 2)


async def test_the_alert_goes_through_raise_alert_one_episode_per_source_logged_not_paged(pool) -> None:
    from legba.runtime.evidence_drift_loop import raise_daily_rollup, run_pass

    async with pool.acquire() as conn:
        await _world(conn)
    await run_pass(pool)
    async with pool.acquire() as conn:
        await _world(conn, path="second")
    await run_pass(pool)
    async with pool.acquire() as conn:
        eps = await conn.fetch("SELECT * FROM alerts WHERE kind = 'evidence_drift'")
        evs = await conn.fetch("SELECT * FROM alert_events WHERE kind = 'evidence_drift' ORDER BY raised_at")
    assert len(eps) == 1 and eps[0]["subject_id"] == SRC and eps[0]["occurrences"] == 2
    assert eps[0]["severity"] == "low" and eps[0]["paged_count"] == 0
    assert [e["decision"] for e in evs] == ["logged", "logged"]
    assert await raise_daily_rollup(pool) is True
    assert await raise_daily_rollup(pool) is False, "once per UTC day"
    async with pool.acquire() as conn:
        roll = await conn.fetchrow("SELECT * FROM alerts WHERE kind = 'evidence_drift' AND subject_kind = 'platform'")
    assert roll["severity"] == "info" and "2 changed page(s)" in roll["summary"]


async def test_the_recheck_batch_puts_open_heads_first_backs_off_stable_pages_and_skips_refused_hosts(
        pool) -> None:
    from legba.runtime import intake_drift

    old = NOW - timedelta(days=10)
    async with pool.acquire() as conn:
        pages: dict[str, int] = {}
        for name, host in (("plain", HOST), ("head", HOST), ("journal", HOST), ("stable", HOST),
                           ("walled", "walled.d1.example.org"), ("superseded", HOST)):
            address = _addr(name, host)
            v = await _version(conn, address, fx.EARLIER, at=old, role="page")
            pages[name] = v[0]
            sig = await _signal(conn, address, v[1])
            if name == "head":
                await _output(conn, cites=[sig], span=fx.CITED_SPAN)
            if name == "superseded":
                await _output(conn, cites=[sig], span=fx.CITED_SPAN, superseded=True)
            if name == "journal":
                await _journal(conn, [sig])
        await conn.execute("INSERT INTO source_credibility (source_host, score, license_class) "
                           "VALUES ('walled.d1.example.org', 0.5, 'anti_ai_walled')")
        await conn.execute("UPDATE fetch_versions SET first_seen_at = $1, last_seen_at = $1 WHERE id = $2",
                           NOW - timedelta(days=12), pages["plain"])
        # two done re-checks of "stable" that saw no change: back-off x4 (4 weeks) > 10 days
        for i in range(2):
            await conn.execute(
                "INSERT INTO ingestion_requests (id, target_kind, address, address_canonical, host, version_id, "
                "pipeline_source_id, sink, reason, budget_class, requested_by, dedupe_key, state, "
                "finished_at, result) VALUES ($1, 'stored_version', $2, $2, $3, $4, 'source.test.d1_pages', "
                "'receipt', 'drift_recheck', 'drift', 'test:d1', $5, 'done', $6, "
                "'{\"changed\": false, \"drift_observed\": 0}'::jsonb)",
                uuid.uuid4(), _addr("stable"), HOST, pages["stable"], f"d1-stable-{i}", NOW - timedelta(days=9))
        rows = await conn.fetch(intake_drift._CANDIDATES_SQL, NOW, 3, 168.0,
                                intake_drift._forbidden_license_classes(), 14.0)
        allrows = await conn.fetch(intake_drift._CANDIDATES_SQL, NOW, 50, 168.0,
                                   intake_drift._forbidden_license_classes(), 14.0)
    by_id = {v: k for k, v in pages.items()}
    assert [by_id[r["id"]] for r in rows] == ["head", "journal", "plain"], "open head, published, then oldest"
    assert {by_id[r["id"]] for r in allrows} == {"head", "journal", "plain", "superseded"}
    assert next(r for r in allrows if by_id[r["id"]] == "superseded")["open_head"] is False


async def test_the_slice_mark_stamps_a_flagged_signal(pool) -> None:
    from legba.data.evidence_drift_slice import SOURCE_DRIFT_ROW_KEY, annotate_source_drift
    from legba.data.evidence_drift_store import classify_pending

    async with pool.acquire() as conn:
        w = await _world(conn)
    await classify_pending(pool)
    rows = [{"id": w["signal"]}, {"id": uuid.uuid4()}]
    async with pool.acquire() as conn:
        assert await annotate_source_drift(conn, rows) == 1
    assert rows[0][SOURCE_DRIFT_ROW_KEY] == {"date": (NOW - timedelta(hours=1)).date().isoformat(),
                                             "class": "substantive_edit"}
    assert SOURCE_DRIFT_ROW_KEY not in rows[1]
