# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The drift assessment on the real path against a migrated
Postgres (the data_pkg session database, isolated test stack only).

Covered: one code-only assessment per flag-writing change with a claim effect
per flag, written once, insert-only, versioned by the assessor stamp (the
model step adds a second row; a bad reply is ``unassessable``, loudly); the
tracked auto-close (principal, note, its own table, listable apart from
person closes, re-openable as a new flag row; D-1's trigger still refuses a
backward move); re-derive requests for the outputs derived from a dependant
resolved corrected / withdrawn; history_rewrite end to end; the new and
widened tools through the REAL binding (``action_pack_invocations``); the
read route (assessments, review order, closed_by, the edit record); the
slice mark carrying the assessment.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from tests.data_pkg import _drift_fixtures as fx
from tests.data_pkg.test_evidence_drift_db import (  # noqa: F401 — the pool fixture
    NOW,
    SRC,
    _addr,
    _binding,
    _drift,
    _journal,
    _output,
    _signal,
    _version,
    pool,
)

pytestmark = pytest.mark.asyncio


async def _world2(conn, pair: tuple[str, str], *, path: str, span: str = fx.CITED_SPAN,
                  stood_days: float = 0.0) -> dict[str, Any]:
    """A page with two versions, a signal from the earlier, a finding citing
    ``span``, a composition derived from the finding and a consumer of it."""
    address = _addr(path)
    e = await _version(conn, address, pair[0], at=NOW - timedelta(days=stood_days + 2))
    lat = await _version(conn, address, pair[1], at=NOW - timedelta(hours=1))
    await conn.execute("UPDATE fetch_versions SET first_source_id = $1 WHERE address = $2", SRC, address)
    if stood_days:
        await conn.execute("UPDATE fetch_versions SET last_seen_at = $1 WHERE id = $2",
                           NOW - timedelta(days=2), e[0])
    sig = await _signal(conn, address, e[1])
    finding = await _output(conn, cites=[sig], span=span)
    comp = await _output(conn, kind="composition", derived_from=[finding], analyst="test_d1_comp")
    consumer = uuid.uuid4()
    await conn.execute("INSERT INTO output_consumption (consumer_id, consumed_id, consumer_kind, context) "
                       "VALUES ($1, $2, 'journal', 'd1_test')", consumer, finding)
    drift = await _drift(conn, address, e, lat, at=NOW - timedelta(hours=1))
    return dict(address=address, earlier=e, later=lat, signal=sig, finding=finding, comp=comp,
                consumer=consumer, drift=drift)


async def _classify_and_assess(pool, **kw):
    from legba.data.evidence_drift_assess_store import assess_pending
    from legba.data.evidence_drift_store import classify_pending

    await classify_pending(pool, limit=20, max_depth=1)
    return await assess_pending(pool, read_bytes=False, **kw)


class _LLM:
    """A fake core-plane handler answering with fixed replies."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kw):
        self.calls.append(kw)
        return type("R", (), {"content": self.replies.pop(0) if self.replies else "not json"})()


# ---------------------------------------------------------------------------
# the assessment rows
# ---------------------------------------------------------------------------

async def test_one_code_only_assessment_per_change_with_a_claim_effect_per_flag(pool) -> None:
    async with pool.acquire() as conn:
        w = await _world2(conn, fx.PAIRS["substantive_edit"], path="toll")
        await _world2(conn, fx.PAIRS["cosmetic"], path="furniture")
    rep = await _classify_and_assess(pool)
    assert (rep.code_only, rep.with_model, rep.errors) == (1, 0, 0)
    async with pool.acquire() as conn:
        a = await conn.fetchrow("SELECT * FROM evidence_drift_assessments")
        effects = {r["dependant_kind"]: r for r in await conn.fetch(
            "SELECT e.*, f.dependant_kind FROM evidence_drift_claim_effects e "
            "JOIN evidence_drift_flags f ON f.id = e.flag_id")}
    assert (a["drift_id"], a["assessor"], a["assessor_stamp"]) == (w["drift"], "code_only", "drift-assess/2|code_only")
    # the toll revised DOWN in a count sentence: the earlier value was corrected
    assert (a["change_type"], a["type_source"], a["model_status"]) == ("fact_corrected", "code", "not_run")
    assert a["summary"] == "figure corrected 19 → 17" and a["acknowledged"] == "silent"
    assert list(a["acknowledged_signals"]) == [] and a["late_unexplained_change"] is False
    assert a["history_rewrite"] is False and json.loads(a["history_rewrite_inputs"])["late_hours"] == 72.0
    assert effects["finding"]["claim_effect"] == "cited_text_changed"
    assert "rose to 17" in effects["finding"]["after_text"]
    assert effects["signal"]["claim_effect"] == "not_checkable"
    # once: a second pass writes nothing
    again = await _classify_and_assess(pool)
    assert again.code_only == 0
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM evidence_drift_assessments") == 1
        for sql in ("UPDATE evidence_drift_assessments SET summary = 'x'",
                    "DELETE FROM evidence_drift_assessments",
                    "UPDATE evidence_drift_claim_effects SET claim_effect = 'not_checkable'"):
            with pytest.raises(asyncpg.exceptions.RestrictViolationError):
                await conn.execute(sql)


async def test_the_model_step_adds_a_versioned_row_and_a_bad_reply_is_unassessable(pool) -> None:
    from legba.data.evidence_drift_assess_store import assess_pending
    from legba.shared.sampling import HOUSE_TEMPERATURE

    async with pool.acquire() as conn:
        await _world2(conn, fx.CLAIM_REMOVED, path="removed", span="A feed summary sentence not on the page.")
        await _world2(conn, fx.PAIRS["substantive_edit"], path="toll")
    await _classify_and_assess(pool)
    good = json.dumps({"summary": "the initial casualty sentence was removed",
                       "code_type_doubt": {"type": "clarification", "before": fx.BODY[2], "after": None}})
    llm = _LLM([good, '{"effect": "gone", "after_quote": null}', "no json at all"])
    rep = await assess_pending(pool, llm=llm, model_id="llm.test:fast", model_cap_n=5, read_bytes=False)
    assert (rep.code_only, rep.with_model, rep.model_failed) == (0, 2, 1)
    assert all(c["temperature"] == HOUSE_TEMPERATURE and "max_tokens" not in c for c in llm.calls)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM evidence_drift_assessments WHERE assessor = 'code_and_model' "
                                "ORDER BY id")
        eff = await conn.fetchrow(
            "SELECT e.* FROM evidence_drift_claim_effects e JOIN evidence_drift_flags f ON f.id = e.flag_id "
            "WHERE e.assessment_id = $1 AND f.dependant_kind = 'finding'", rows[0]["id"])
    assert rows[0]["assessor_stamp"] == "drift-assess/2|drift-assess-prompt/2|llm.test:fast"
    # the model never types: the code type stands, the doubt sits beside it
    assert (rows[0]["change_type"], rows[0]["type_source"], rows[0]["model_status"]) == \
        ("claim_removed", "code", "ok")
    assert rows[0]["summary"] == "the initial casualty sentence was removed"
    assert json.loads(rows[0]["model_type_doubt"])["type"] == "clarification"
    assert (eff["claim_effect"], eff["model_status"], eff["model_effect"]) == ("not_checkable", "ok", "gone")
    # a bad reply: loud in model_status, the code assessment and summary stand
    assert (rows[1]["change_type"], rows[1]["model_status"], rows[1]["summary"]) == \
        ("fact_corrected", "parse_failed", "figure corrected 19 → 17")
    assert json.loads(rows[1]["model_output"])["reason"].startswith("not_json")
    async with pool.acquire() as conn:
        # the code result is never overwritten by the model: a model answer
        # only sits beside a not_checkable code verdict
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO evidence_drift_claim_effects (flag_id, assessment_id, claim_effect, model_status) "
                "SELECT f.id, $1, 'cited_text_gone', 'ok' FROM evidence_drift_flags f LIMIT 1", rows[1]["id"])


async def test_history_rewrite_end_to_end(pool) -> None:
    async with pool.acquire() as conn:
        await _world2(conn, fx.CLAIM_REMOVED, path="rewrite", stood_days=5)
    await _classify_and_assess(pool)
    async with pool.acquire() as conn:
        a = await conn.fetchrow("SELECT * FROM evidence_drift_assessments")
    inputs = json.loads(a["history_rewrite_inputs"])
    assert (a["change_type"], a["acknowledged"], a["history_rewrite"]) == ("claim_removed", "silent", True)
    assert a["late_unexplained_change"] is True
    assert inputs["standing_hours"] >= 72 and inputs["late"] is True and a["timing_basis"] == "first_read"
    assert json.loads(a["late_unexplained_inputs"])["correction_notice"] is False


# ---------------------------------------------------------------------------
# auto-close, re-open, re-derive
# ---------------------------------------------------------------------------

async def test_the_tracked_auto_close_and_the_reopen(pool) -> None:
    from legba.data.evidence_drift_assess_store import AUTO_CLOSE_PRINCIPAL, auto_close_pending, reopen_flag
    from legba.data.evidence_drift_reads import drift_counts, open_flags
    from legba.data.evidence_drift_store import resolve_flag

    async with pool.acquire() as conn:
        w = await _world2(conn, fx.NAME_FIX, path="namefix")
        other = await _world2(conn, fx.PAIRS["substantive_edit"], path="toll")
    await _classify_and_assess(pool)
    # the switch off: the shadow count records the flag and changes nothing
    shadow = await auto_close_pending(pool, close=False)
    assert (shadow.would_close, shadow.closed) == (1, 0)
    assert (await auto_close_pending(pool, close=False)).would_close == 0, "recorded once per rule version"
    async with pool.acquire() as conn:
        would = await open_flags(conn, state="open", would_auto_close=True)
        counts = await drift_counts(conn, window_hours=24)
        st = await conn.fetchval("SELECT state FROM evidence_drift_flags WHERE dependant_id = $1",
                                 str(w["finding"]))
    assert [r["dependant_id"] for r in would] == [str(w["finding"])] and st == "open"
    assert counts["would_auto_close"] == {"window": 1, "total": 1, "rule_version": "drift-autoclose/1",
                                          "switch_on": False}
    assert (await auto_close_pending(pool, close=True)).closed == 1
    assert (await auto_close_pending(pool, close=True)).closed == 0
    async with pool.acquire() as conn:
        f = await conn.fetchrow("SELECT * FROM evidence_drift_flags WHERE dependant_id = $1", str(w["finding"]))
        sig = await conn.fetchrow("SELECT * FROM evidence_drift_flags WHERE dependant_id = $1", str(w["signal"]))
        x = await conn.fetchrow("SELECT * FROM evidence_drift_auto_closes")
        a_id = await conn.fetchval("SELECT id FROM evidence_drift_assessments WHERE drift_id = $1", w["drift"])
        person = await conn.fetchval("SELECT id FROM evidence_drift_flags WHERE dependant_id = $1",
                                     str(other["finding"]))
    assert (f["state"], f["resolution"], f["resolved_by"]) == ("resolved", "unchanged_in_substance",
                                                               AUTO_CLOSE_PRINCIPAL)
    assert f"assessment {a_id}" in f["resolution_note"] and "drift-autoclose/1" in f["resolution_note"]
    assert (x["flag_id"], x["assessment_id"], x["rule_version"]) == (f["id"], a_id, "drift-autoclose/1")
    assert sig["state"] == "open", "a signal flag has no cited span: never auto-closed"
    await resolve_flag(pool, person, principal="person:analyst", resolution="unchanged_in_substance", note="ok")
    async with pool.acquire() as conn:
        system = await open_flags(conn, state="resolved", closed_by="system")
        people = await open_flags(conn, state="resolved", closed_by="other")
        counts = await drift_counts(conn, window_hours=24)
    assert [r["id"] for r in system] == [f["id"]] and system[0]["auto_closed"] is True
    assert [r["id"] for r in people] == [person]
    assert counts["auto_closed"] == {"window": 1, "total": 1, "reopened": 0}
    # re-open: a NEW open flag row; D-1's trigger still refuses a backward move
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await conn.execute("UPDATE evidence_drift_flags SET state = 'open', resolution = NULL, "
                               "resolved_at = NULL, resolved_by = NULL WHERE id = $1", f["id"])
    assert (await reopen_flag(pool, person, principal="person:x", note="n"))["status"] == "not_auto_closed"
    with pytest.raises(ValueError):
        await reopen_flag(pool, f["id"], principal="person:analyst", note="")
    out = await reopen_flag(pool, f["id"], principal="person:analyst", note="the name was the claim")
    assert out["status"] == "reopened" and out["reopened_from"] == f["id"]
    assert (await reopen_flag(pool, f["id"], principal="person:analyst", note="again"))["status"] == \
        "already_reopened"
    async with pool.acquire() as conn:
        new = await conn.fetchrow("SELECT * FROM evidence_drift_flags WHERE id = $1", out["flag_id"])
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await conn.execute("UPDATE evidence_drift_flags SET reopened_from = NULL WHERE id = $1", new["id"])
        counts = await drift_counts(conn, window_hours=24)
    assert (new["state"], new["dependant_id"], new["reopened_by"], new["reopen_note"]) == \
        ("open", str(w["finding"]), "person:analyst", "the name was the claim")
    assert counts["auto_closed"]["reopened"] == 1
    assert (await auto_close_pending(pool, close=True)).closed == 0, "a re-opened flag is never auto-closed again"


async def test_resolving_corrected_or_withdrawn_requests_rederive_of_derived_outputs(pool) -> None:
    from legba.data.evidence_drift_store import rederive_derived_outputs, resolve_flag

    async with pool.acquire() as conn:
        w = await _world2(conn, fx.PAIRS["substantive_edit"], path="toll")
    await _classify_and_assess(pool)  # walked at depth 1: no derived_output flags
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM evidence_drift_flags WHERE depth = 2") == 0
        fid = await conn.fetchval("SELECT id FROM evidence_drift_flags WHERE dependant_kind = 'finding'")
        sid = await conn.fetchval("SELECT id FROM evidence_drift_flags WHERE dependant_kind = 'signal'")
    assert await resolve_flag(pool, fid, principal="analyst::a", resolution="dependant_corrected",
                              note="now cites 17") == "resolved"
    assert await resolve_flag(pool, sid, principal="analyst::a", resolution="dependant_withdrawn") == "resolved"
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM evidence_rederive_requests ORDER BY dependant_id")
        again = await rederive_derived_outputs(conn, fid, requested_by="analyst::b")
    assert {r["dependant_id"] for r in rows} == {str(w["comp"]), str(w["consumer"])}
    assert all(r["dependant_kind"] == "derived_output" and r["state"] == "pending" and r["flag_id"] == fid
               and r["requested_by"] == "analyst::a" for r in rows)
    assert again == 0, "one pending request per output"


# ---------------------------------------------------------------------------
# tools, route, slice
# ---------------------------------------------------------------------------

async def test_the_assessment_tools_through_the_real_binding(pool, monkeypatch) -> None:
    from legba.data.evidence_drift_assess_store import auto_close_pending

    async with pool.acquire() as conn:
        w = await _world2(conn, fx.NAME_FIX, path="namefix")
        marker = {r["id"] for r in await conn.fetch("SELECT id FROM action_pack_invocations")}
    await _classify_and_assess(pool)
    await auto_close_pending(pool, close=False)
    b = _binding(pool, "test_d2_agent")
    shadow = await b.run_tool("drift_flags", {"state": "open", "would_auto_close": True})
    assert shadow.tool_result.output["would_auto_close_count"] == 1
    await auto_close_pending(pool, close=True)
    got = await b.run_tool("evidence_drift_assessment", {"drift_id": w["drift"]})
    assert got.admitted is True and got.tool_result.status == "completed", got.tool_result.error
    a = got.tool_result.output["assessments"][0]
    assert a["change_type"] == "spelling_or_name_correction" and "model_output" not in a
    assert {e["dependant_kind"]: e["claim_effect"] for e in a["claim_effects"]}["finding"] == \
        "cited_text_survives"
    by_record = await b.run_tool("evidence_drift_assessment", {"record_id": str(w["signal"])})
    assert by_record.tool_result.output["count"] == 1
    bad = await b.run_tool("evidence_drift_assessment", {"drift_id": 1, "address": w["address"]})
    assert bad.tool_result.status == "failed"
    listed = await b.run_tool("drift_flags", {"state": "resolved", "closed_by": "system"})
    flags = listed.tool_result.output["flags"]
    assert len(flags) == 1 and flags[0]["auto_closed"] is True and flags[0]["resolved_by"] == "system::drift_assessor"
    reopened = await b.run_tool("reopen_drift_flag", {"flag_id": flags[0]["id"], "note": "the name matters here"})
    assert reopened.tool_result.status == "completed"
    vers = await b.run_tool("evidence_versions", {"address": w["address"]})
    assert vers.tool_result.output["transitions"][0]["assessment"]["change_type"] == "spelling_or_name_correction"
    diff = await b.run_tool("evidence_diff", {"address": w["address"]})
    assert diff.tool_result.output["assessment"]["summary"] == "name corrected Naser -> Nasir"
    async with pool.acquire() as conn:
        invs = await conn.fetch("SELECT id, pack_id, tool_name, outcome FROM action_pack_invocations")
        new_flag = await conn.fetchrow("SELECT reopened_by FROM evidence_drift_flags WHERE reopened_from IS NOT NULL")
    assert new_flag["reopened_by"] == "analyst::test_d2_agent"
    new = {(r["pack_id"], r["tool_name"], r["outcome"]) for r in invs if r["id"] not in marker}
    for tool in ("evidence_drift_assessment", "drift_flags", "reopen_drift_flag", "evidence_versions",
                 "evidence_diff"):
        assert ("evidence_drift", tool, "completed") in new, new


async def test_the_route_review_order_assessments_and_edit_record(pool) -> None:
    from types import SimpleNamespace

    from fastapi import FastAPI

    from legba.data.registry.api import require_bearer
    from legba.data.registry.evidence_drift_api import build_evidence_drift_router

    async with pool.acquire() as conn:
        await _world2(conn, fx.NAME_FIX, path="namefix")
        rewrite = await _world2(conn, fx.CLAIM_REMOVED, path="rewrite", stood_days=5)
        await _world2(conn, fx.PAIRS["substantive_edit"], path="toll")
    await _classify_and_assess(pool)
    app = FastAPI()
    app.include_router(build_evidence_drift_router(SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))),
                       prefix="/api/v1/v3")
    app.dependency_overrides[require_bearer] = lambda: "test"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        flags = (await client.get("/api/v1/v3/evidence/drift/flags?state=open&limit=50")).json()
        one = (await client.get(f"/api/v1/v3/evidence/drift/assessments?drift_id={rewrite['drift']}")).json()
        two_keys = await client.get(f"/api/v1/v3/evidence/drift/assessments?drift_id=1&flag_id=2")
        editors = (await client.get("/api/v1/v3/evidence/drift/editors")).json()
        summary = (await client.get("/api/v1/v3/evidence/drift?window_hours=24")).json()
    prios = [f["review_priority"] for f in flags["flags"]]
    assert prios == sorted(prios) and prios[0] == 0 and prios[-1] == 4
    assert flags["flags"][0]["history_rewrite"] is True and flags["flags"][-1]["change_type"] == \
        "spelling_or_name_correction"
    assert one["available"] is True and one["assessments"][0]["history_rewrite"] is True
    assert two_keys.status_code == 422
    rec = editors["sources"][0]["edit_record"]
    assert rec["30d"]["assessed"] == 3 and rec["7d"]["history_rewrite"] == 1
    assert rec["30d"]["acknowledged"]["silent"] == 3 and rec["30d"]["pages_rechecked"] == 3
    assert summary["history_rewrites"] == 1 and sum(summary["assessments_by_type"].values()) == 3
    assert summary["late_unexplained_changes"] == 1 and rec["30d"]["late_unexplained_change"] == 1


async def test_the_slice_mark_carries_the_assessment(pool) -> None:
    from legba.data.evidence_drift_slice import SOURCE_DRIFT_ROW_KEY, annotate_source_drift, source_drift_line

    async with pool.acquire() as conn:
        w = await _world2(conn, fx.CLAIM_REMOVED, path="removed")
    await _classify_and_assess(pool)
    rows = [{"id": w["signal"]}, {"id": uuid.uuid4()}]
    async with pool.acquire() as conn:
        assert await annotate_source_drift(conn, rows) == 1
    mark = rows[0][SOURCE_DRIFT_ROW_KEY]
    assert mark["assessment"]["type"] == "claim_removed"
    assert source_drift_line(rows[0]) == (
        f"    source text changed on {mark['date']}: 1 earlier sentence(s) removed (claim removed, silent)")
    assert SOURCE_DRIFT_ROW_KEY not in rows[1]
    _ = _journal  # (imported for parity with D-1's world; unused here)
