# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11 INST-1 — correctness on a non-country roster.

The gate: a non-country desk publishes correctness WITH coverage. Proven here
on the test DB, end to end through ``deterministic.run_method`` (the REAL
binding path) and the registry route:

  * the reference roster is the profile's subjects — the scored tags plus
    ``constants.reference_subject_tags`` (``supply_chain`` on the exemplar) —
    active heads only, each over the units it produced;
  * a lane's reference population is its OWN: its units' query terms, its
    label's name half as the query subject, and an instruction that calls it a
    lane;
  * the grader grades a lane — the units its tags fire, no fabricated country
    gaps — and writes unit rows with a coverage share, and
    ``/v3/eval/grader_roster`` lists it with its denominators;
  * the deterministic (keyed) reference kind cuts a fixture list to a subject
    and window, and the grader scores it EXACTLY, by key, with no model;
  * a profile with no gazetteer still builds a reference for its subjects
    (the gazetteer rung reads ABSENT, every subject named by its descriptor);
  * the geopolitics country desks grade byte-identically to the pin's rule on
    a fixture, and their instruction is the pin's bytes.

Every row this file writes is scrubbed in its teardown (the W8 rule): the
throwaway targets' descriptors, outputs, references, unit rows and the one
calibration row it seeds.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data import gazetteer as GZ
from legba.data import profile_constants as PC
from legba.data import rosters
from legba.data import subject as SUBJECT
from legba.data.analysts import deterministic
from legba.data.analysts.deterministic_handlers import _correctness_grade as GRADE
from legba.data.analysts.deterministic_handlers import _correctness_keyed as KEYED
from legba.data.analysts.deterministic_handlers import _reference_instruction as INSTR
from legba.data.analysts.deterministic_handlers import _reference_keyed as RK
from legba.data.analysts.deterministic_handlers import _reference_manifest as MANIFEST
from legba.data.analysts.deterministic_handlers import _reference_notes as NOTES
from legba.data.analysts.deterministic_handlers import _reference_roster as ROSTER
from legba.data.analysts.deterministic_handlers import correctness_grader as CG
from legba.data.analysts.deterministic_handlers import reference_builder as RB
from legba.data.analysts.deterministic_handlers import reference_deterministic as RD
from legba.data.analysts.deterministic_handlers._correctness_rubric import RUBRIC_SHA256
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.profile import load_profile
from legba.runtime.analyst_method import AnalystMethodResult

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Throwaway subjects — every one scrubbed in teardown
# ---------------------------------------------------------------------------

_LANE = "lane_inst1_strait"
_LANE_PAUSED = "lane_inst1_paused"
_COUNTRY = "country_inst1_zz"
_UNTAGGED = "situation_inst1_untagged"
_ESTATE = "estate_inst1_fortinet"
_TARGETS = (_LANE, _LANE_PAUSED, _COUNTRY, _UNTAGGED, _ESTATE)
_PACKET_SHA = "e1" * 32  # this file's calibration row, by its own packet sha
_T0 = datetime.now(timezone.utc).replace(microsecond=0)


def _head(tid: str, name: str, *, tags: list[str], geo: list[str],
          domain: str = "thematic", state: str = "active") -> tuple:
    body = {
        "identity": {"id": tid, "name": name, "schema_uri": "legba/target/2.0.0"},
        "scope": {"domain": domain, "geo": geo, "tags": tags},
    }
    return tid, state, name, json.dumps(body)


_HEADS = (
    _head(_LANE, "Lane — Inst One Strait", tags=["supply_chain", "disruption", "lane"],
          geo=["IR", "OM"]),
    _head(_LANE_PAUSED, "Lane — Inst One Paused", tags=["supply_chain", "lane"],
          geo=["YE"], state="paused"),
    _head(_COUNTRY, "G20 — Inst One", tags=["geopolitical", "g20"], geo=["ZZ"],
          domain="geo"),
    _head(_UNTAGGED, "Situation — Inst One", tags=["situation"], geo=["IR"]),
    _head(_ESTATE, "Estate — Fortinet FortiGate", tags=["supply_chain"], geo=[]),
)


class _Deps:
    def __init__(self, pool, extras=None) -> None:
        self.pg_pool = pool
        self.extras = dict(extras or {})


async def _run(pool, sub_handler: str, extras=None, **opts) -> AnalystMethodResult:
    """``deterministic.run_method`` — the REAL binding path, never the handler."""
    options = {"sub_handler": sub_handler, "analyst_id": sub_handler,
               "run_id": str(uuid4()), **opts}
    result = await deterministic.run_method([], options, _Deps(pool, extras))
    assert isinstance(result, AnalystMethodResult)
    return result


async def _scrub(conn) -> None:
    for table in ("unit_correctness", "unit_references", "analyst_outputs"):
        await conn.execute(
            f"DELETE FROM {table} WHERE target_id = ANY($1::text[])", list(_TARGETS)
        )
    await conn.execute(
        "DELETE FROM target_descriptors WHERE descriptor_id = ANY($1::text[])",
        list(_TARGETS),
    )
    await conn.execute("DELETE FROM grader_calibrations WHERE packet_sha = $1", _PACKET_SHA)


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def world(pg_pool):
    """The five throwaway heads, seeded; everything scrubbed after."""
    async with pg_pool.acquire() as conn:
        await _scrub(conn)
        for tid, state, name, body in _HEADS:
            await conn.execute(
                "INSERT INTO target_descriptors "
                "  (descriptor_id, version, schema_uri, is_head, state, owner, name, body) "
                "VALUES ($1, 'v1', 'legba/target/2.0.0', TRUE, $2, 'pytest_inst1', $3, $4::jsonb)",
                tid, state, name, body,
            )
    try:
        yield
    finally:
        async with pg_pool.acquire() as conn:
            await _scrub(conn)
        NOTES.clear(_LANE)


async def _finding(conn, target: str, analyst: str, body: str = "body", *,
                   at: datetime | None = None) -> None:
    when = at or (_T0 - timedelta(hours=2))
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, analyst_id, analyst_version, run_id, target_id, kind, title,
             body, confidence, data, produced_at, created_at, schema_uri)
        VALUES ($1,$2,$3,$4,$5,'finding',$6,$7,1.0,$8::jsonb,$9,$9,$10)
        """,
        uuid4(), analyst, "0" * 16, uuid4(), target, f"{analyst} read", body,
        json.dumps({"data": {}}), when, "iglu:legba/finding/jsonschema/1-0-0",
    )


async def _calibration(conn) -> None:
    await conn.execute(
        """
        INSERT INTO grader_calibrations (
            id, rubric_sha, model_ids, pooled, pairwise, gate_pass, packet_sha,
            n_atoms, notes
        ) VALUES ($1,$2,$3::jsonb,0.85,'[]'::jsonb,TRUE,$4,30,'pytest inst1 seed')
        ON CONFLICT DO NOTHING
        """,
        uuid4(), RUBRIC_SHA256, json.dumps(GRADE.model_ids()), _PACKET_SHA,
    )


async def _reference(conn, target: str, reference: dict[str, Any]) -> None:
    await conn.execute(
        """
        INSERT INTO unit_references (
            id, target_id, window_start, window_end, built_at, builder,
            ref_json, span_verified_rate, thin_dimensions, sha256
        ) VALUES ($1,$2,$3,$4,$4,'pytest-inst1',$5::jsonb,NULL,'{}'::text[],$6)
        """,
        uuid4(), target, _T0 - timedelta(days=14), _T0 - timedelta(hours=6),
        json.dumps(reference), hashlib.sha256(json.dumps(reference).encode()).hexdigest(),
    )


# ---------------------------------------------------------------------------
# 1. The roster: the profile's subjects
# ---------------------------------------------------------------------------


def test_the_exemplar_declares_supply_chain_a_reference_leg():
    assert load_profile(os.path.join(_REPO, "profiles", "geopolitics")).constants \
        .reference_subject_tags == ("supply_chain",)
    assert PC.reference_subject_tags() == ("supply_chain",)
    assert ROSTER.reference_roster_tags() == ("g20", "watch", "supply_chain")
    assert ROSTER.reference_units() == (*rosters.scored_subject_units(), "disruption_status")
    assert ROSTER.units_for_tags(["supply_chain", "lane"]) == ("disruption_status",)
    assert ROSTER.units_for_tags(["g20"]) == rosters.scored_unit_roster()
    assert ROSTER.units_for_tags(["watch", "nuclear_watch"]) == (
        *rosters.scored_unit_roster(), "proliferation_watch")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_roster_carries_the_tagged_lane_and_only_active_tagged_heads(pg_pool, world):
    async with pg_pool.acquire() as conn:
        await _finding(conn, _LANE, "disruption_status")
        await _finding(conn, _LANE_PAUSED, "disruption_status")
        await _finding(conn, _COUNTRY, "escalation")
        await _finding(conn, _UNTAGGED, "escalation")
        roster = await RB.resolve_roster(
            conn, t0=_T0, dimensions=ROSTER.reference_units(), roster_window_days=30,
        )
        # the pin's set (no reference leg) never sees the lane
        pin = await RB.resolve_roster(
            conn, t0=_T0, dimensions=rosters.scored_subject_units(),
            roster_window_days=30, tags=rosters.scored_roster_tags(),
        )
    assert roster[_LANE] == ["disruption_status"]
    assert roster[_COUNTRY] == ["escalation"]
    assert _LANE_PAUSED not in roster, "a paused head is not on the roster"
    assert _UNTAGGED not in roster, "a head carrying no roster tag is not on it"
    assert _LANE not in pin and pin[_COUNTRY] == ["escalation"]


# ---------------------------------------------------------------------------
# 2. A lane's reference population is its own
# ---------------------------------------------------------------------------


class _Conn:
    """The ladder's two statements, answered from memory; records the calls."""

    def __init__(self, rows, gazetteer=None):
        self.rows, self.gazetteer, self.calls = rows, dict(gazetteer or {}), []

    async def fetch(self, sql, *args):
        if sql == SUBJECT.HEADS_SQL:
            self.calls.append("heads")
            return [r for r in self.rows if r["target_id"] in args[0]]
        if sql == SUBJECT.GAZETTEER_SQL:
            self.calls.append("gazetteer")
            return [{"iso2": c, "name": self.gazetteer[c]} for c in args[0] if c in self.gazetteer]
        raise AssertionError(sql[:60])


_LANE_ROW = {"target_id": _LANE, "name": "Lane — Inst One Strait", "geo": ["IR", "OM"],
             "domain": "thematic", "subject_code": None}
_COUNTRY_ROW = {"target_id": "country_g20_ar", "name": "G20 — Argentina", "geo": ["AR"],
                "domain": "geo", "subject_code": None}


@pytest.mark.asyncio
async def test_a_lane_is_named_searched_and_instructed_as_a_lane():
    conn = _Conn([_LANE_ROW, _COUNTRY_ROW], {"AR": "Argentina"})
    ident = await ROSTER.target_identity(conn, [_LANE, "country_g20_ar"], detail=True)
    lane = ident[_LANE]
    assert (lane["code"], lane["name"], lane["kind"], lane["noun"], lane["query_name"]) == (
        _LANE, "Lane — Inst One Strait", "thematic", "lane", "Inst One Strait")
    assert ident["country_g20_ar"]["noun"] == "country"
    assert ident["country_g20_ar"]["query_name"] == "Argentina"
    # its OWN unit, its OWN query terms (disruption_status declares no patterns)
    guidance = MANIFEST.query_guidance(lane["query_name"], ["disruption_status"])
    assert "`Inst One Strait shipping traffic`" in guidance
    assert "`Inst One Strait disruption closure`" in guidance
    assert "disruption status announcement" not in guidance
    text = INSTR.build_instruction(
        country_name=lane["name"], country_code=lane["code"], t0="T0", window_start="W",
        dimensions=["disruption_status"], item_prefix="RD-LANE-C", builder_label="b",
        model_name="m", tool_call_cap=10, subject_noun=lane["noun"],
        query_subject=lane["query_name"],
    )
    assert "a knowledgeable lane analyst" in text and "country analyst" not in text
    assert "this lane" in text and "`disruption_status`" in text


_SPAN = "Tanker traffic through the corridor fell by a third after the seizure, the port authority said."
_PAGE = (
    '<html><head><script type="application/ld+json">{"datePublished":"'
    + (_T0 - timedelta(days=3)).date().isoformat()
    + 'T08:00:00Z"}</script></head><body><article><p>' + _SPAN + "</p><p>"
    + ("filler sentence. " * 120) + "</p></article></body></html>"
)
_URL = "https://www.bbc.com/news/inst1-strait"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_builder_builds_the_lane_from_its_own_population(pg_pool, world, monkeypatch):
    """THE REAL PATH: the lane is on the roster, its instruction and queries are
    its own, and the build carries the subject (dry run — nothing written)."""
    from tests.data_pkg.test_reference_builder import (  # the builder's own doubles
        _Call, _Reply, _ScriptedBinding, _ScriptedLLM,
    )
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _finding(conn, _LANE, "disruption_status")
    final = json.dumps({"header": {}, "ref_bands": {"disruption_status": "elevated"},
                        "ref_developments": [{
                            "item_id": "RD-X-C-1", "summary": "Tanker traffic fell.",
                            "decisive_span": _SPAN, "outlet": "BBC",
                            "publish_date": (_T0 - timedelta(days=3)).date().isoformat(),
                            "source_url": _URL, "source_tier": 2,
                            "significance": "major", "dimension": ["disruption_status"]}]})
    llm = _ScriptedLLM([
        _Reply(calls=[_Call("fetch_page", {"url": _URL}, "c1")]),
        _Reply("NOTE: | a | SPAN: b"),
        _Reply("REFERENCE COMPLETE"),
        _Reply(final),
    ])
    binding = _ScriptedBinding(pages={_URL: _PAGE})
    result = await _run(
        pg_pool, "reference_builder",
        extras={RB.LLM_DEPS_EXTRA_KEY: llm, RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), reference_targets=[_LANE],
        reference_dry_run=True, min_developments=1,
    )
    target = result.finding.data["per_target"][0]
    assert target["dimensions"] == ["disruption_status"]
    assert target["subject"]["noun"] == "lane"
    assert target["subject"]["query_name"] == "Inst One Strait"
    assert target["status"] == "built_not_written", target.get("warnings")
    reference = target["reference"]
    assert reference["header"]["subject"]["code"] == _LANE
    assert reference["ref_developments"][0]["item_id"].startswith("RD-LANE_INST1_STRAIT-C-")
    system = llm.kwargs_seen[0]["system"]
    assert "a knowledgeable lane analyst" in system
    assert "`Inst One Strait shipping traffic`" in system


# ---------------------------------------------------------------------------
# 3. The grader grades a lane, with coverage; the route lists it
# ---------------------------------------------------------------------------

_LANE_CLAIM = (
    "Tanker traffic through the corridor fell by a third after the seizure of a "
    "crude carrier near the eastern approach."
)
_LANE_REFERENCE = {
    "header": {"t0": _T0.isoformat()},
    "ref_bands": {"disruption_status": "elevated"},
    "ref_developments": [{
        "item_id": "RD-LANE_INST1_STRAIT-C-1", "summary": "Traffic fell after a seizure.",
        "decisive_span": _SPAN, "outlet": "BBC", "publish_date": "2026-10-01",
        "source_url": _URL, "source_tier": 2, "significance": "major",
        "dimension": ["disruption_status"],
    }],
}


class _AlwaysLLM:
    def __init__(self, verdict: str, span: str) -> None:
        self._reply = json.dumps({"verdict": verdict, "core_claim": "c",
                                  "decisive_span": span, "reason": "r"})
        self.calls = 0

    async def chat_complete(self, messages, **kwargs):
        self.calls += 1

        class _U:
            cost_estimate_usd, prompt_tokens, completion_tokens, model = 0.0, 1, 1, "m"

        class _R:
            content, usage = self._reply, _U()
        return _R()


async def _roster_route(migrated_pg: PostgresConfig) -> list[dict[str, Any]]:
    from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
    from legba.data.registry.audit import AuditLogger
    from legba.data.registry.credentials import MASTER_KEY_ENV, CredentialVault
    from legba.data.registry.descriptor import DescriptorRegistry
    from legba.data.registry.dlq import DescriptorDeadLetter
    from legba.data.registry.grader_roster_api import build_grader_roster_router
    from legba.data.registry.signing import SigningIdentity
    from legba.data.registry.stack import StackRegistry
    from legba.data.registry.vocabulary_cache import VocabularyCache

    os.environ.setdefault(MASTER_KEY_ENV, "0011223344556677889900112233445566778899001122334455667788990011")
    os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "44" * 32)
    saved = os.environ.pop(API_TOKEN_ENV, None)
    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    identity = SigningIdentity(signing_key=SigningKey(b"inst1-grader-roster-route-seed!!"[:32]),
                               signer_did="did:legba:registry:inst1-test")
    audit = AuditLogger(identity=identity)
    dlq, vocab, vault = DescriptorDeadLetter(pg_store), VocabularyCache(pg_store), CredentialVault(pg_store)
    registry = DescriptorRegistry(pg_store, vocabulary_cache=vocab, signing_identity=identity,
                                  audit_logger=audit, dead_letter=dlq)
    await registry.start()
    try:
        deps = RegistryAPIDeps(descriptor_registry=registry,
                               stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
                               vault=vault, dlq=dlq, audit_logger=audit,
                               vocabulary_cache=vocab, nats_store=None)
        app = FastAPI()
        app.state.registry_deps = deps
        app.include_router(build_grader_roster_router(deps), prefix="/api/v1/v3")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            resp = await client.get("/api/v1/v3/eval/grader_roster?nights=7")
        assert resp.status_code == 200, resp.text
        return resp.json()["desks"]
    finally:
        await registry.stop()
        await pg_store.close()
        if saved is not None:
            os.environ[API_TOKEN_ENV] = saved


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_grader_grades_a_lane_with_coverage_and_the_roster_route_lists_it(
    pg_pool, world, monkeypatch, migrated_pg
):
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.delenv(GRADE.CEILING_ENV, raising=False)
    async with pg_pool.acquire() as conn:
        await _calibration(conn)
        await _reference(conn, _LANE, _LANE_REFERENCE)
        await _finding(conn, _LANE, "disruption_status", "## What changed\n" + _LANE_CLAIM)
    llm = _AlwaysLLM("contains", _SPAN)
    result = await _run(pg_pool, "correctness_grader", extras={CG.F0_DEPS_EXTRA_KEY: llm},
                        grader_targets=[_LANE], as_of=_T0.isoformat())
    outcome = result.finding.data["per_target"][0]
    assert outcome["status"] == "ok", outcome
    assert outcome["subject"] == {"id": _LANE, "code": _LANE,
                                  "name": "Lane — Inst One Strait", "kind": "thematic"}
    # NO fabricated country gaps: a lane is not "missing" seven country units
    assert not any("no head as of the stamp" in w for w in outcome["warnings"]), outcome["warnings"]
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM unit_correctness WHERE target_id = $1", _LANE)
    assert row["analyst_id"] == "disruption_status" and row["grain"] == "desk"
    assert row["n_claims"] >= 1 and row["n_contains"] == row["n_claims"]
    assert float(row["coverage_share"]) == 1.0 and float(row["correctness_share"]) == 1.0

    desks = await _roster_route(migrated_pg)
    mine = [d for d in desks if d["target_id"] == _LANE]
    assert len(mine) == 1, desks
    latest = mine[0]["latest"]
    assert (latest["n_claims"], latest["n_decided"]) == (row["n_claims"], row["n_claims"])
    assert latest["coverage_share"] == 1.0 and "coverage" in latest["badge"].lower()


# ---------------------------------------------------------------------------
# 4. The deterministic (keyed) reference kind, scored exactly
# ---------------------------------------------------------------------------


def _kev(t0: datetime) -> dict[str, Any]:
    day = lambda n: (t0 - timedelta(days=n)).date().isoformat()  # noqa: E731
    return {"title": "fixture KEV", "vulnerabilities": [
        {"cveID": "CVE-2026-1001", "vendorProject": "Fortinet", "product": "FortiOS",
         "vulnerabilityName": "FortiOS SSL-VPN heap overflow", "dateAdded": day(2)},
        {"cveID": "CVE-2026-1002", "vendorProject": "Fortinet", "product": "FortiOS",
         "vulnerabilityName": "FortiOS auth bypass", "dateAdded": day(5)},
        {"cveID": "CVE-2026-0999", "vendorProject": "Fortinet", "product": "FortiOS",
         "vulnerabilityName": "old", "dateAdded": day(40)},
        {"cveID": "CVE-2026-2001", "vendorProject": "Ivanti", "product": "EPM",
         "vulnerabilityName": "Ivanti EPM RCE", "dateAdded": day(1)},
    ]}


_KEV_OPTS = {
    "feed_id": "kev_fixture", "feed_url": "https://example.test/kev.json",
    "items_path": "vulnerabilities", "key_field": "cveID",
    "key_pattern": r"CVE-\d{4}-\d{4,7}", "date_field": "dateAdded",
    "subject_field": "vendorProject",
    "summary_fields": ["vendorProject", "product", "vulnerabilityName"],
    "outlet": "CISA KEV (fixture)", "list_complete": False,
}

_KEYED_CLAIMS = {
    "contains": "CISA added CVE-2026-1001 to its exploited catalogue after attackers "
                "chained it against exposed FortiGate appliances.",
    "contradicts": "There is no evidence that CVE-2026-1002 has been exploited against "
                   "any FortiGate deployment in this period.",
    "silent_off_list": "CVE-2026-3333 is reported under active exploitation against "
                       "FortiGate SSL-VPN gateways this week.",
    "silent_no_key": "Fortinet shipped a firmware update for its FortiGate line that "
                     "closes several memory-safety defects.",
}


def test_the_keyed_rules_are_the_table():
    ref = RK.build_keyed_reference(
        target_id=_ESTATE, identity={"code": _ESTATE, "name": "E"},
        items=[{"cveID": "CVE-2026-1001", "vendorProject": "Fortinet", "dateAdded": "2026-10-01"}],
        spec=RD.feed_spec(_KEV_OPTS), dimensions=["disruption_status"],
        t0=_T0, window_start=_T0 - timedelta(days=14), feed_sha256="0" * 64,
    )
    spec, index = KEYED.keyed_spec(ref), KEYED.reference_index(ref)
    labels = {k: KEYED.label_claim(t, spec, index)["verdict"] for k, t in _KEYED_CLAIMS.items()}
    assert labels == {"contains": "contains", "contradicts": "silent",
                      "silent_off_list": "silent", "silent_no_key": "silent"}
    # on a COMPLETE list an absent key grades, and negation flips it
    complete = dict(spec, complete=True)
    assert KEYED.label_claim(_KEYED_CLAIMS["silent_off_list"], complete, index)["verdict"] == "contradicts"
    assert KEYED.label_claim("CVE-2026-4444 has not been exploited anywhere, the vendor said.",
                             complete, index)["verdict"] == "contains"
    assert KEYED.label_claim("Exploitation of CVE-2026-1001 was denied by the vendor today.",
                             spec, index)["verdict"] == "contradicts"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_keyed_kind_builds_from_a_fixture_list_and_is_graded_exactly(
    pg_pool, world, monkeypatch
):
    monkeypatch.setenv(RD.ENABLED_ENV, "1")
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    fetched: list[str] = []

    async def fetch(url: str) -> str:
        fetched.append(url)
        return json.dumps(_kev(_T0))

    result = await _run(pg_pool, "reference_deterministic",
                        extras={RD.FETCH_DEPS_EXTRA_KEY: fetch},
                        as_of=_T0.isoformat(), bindings=[f"{_ESTATE}=Fortinet"], **_KEV_OPTS)
    assert fetched == ["https://example.test/kev.json"]
    built = result.finding.data["per_target"]
    assert [(t["target_id"], t["status"], t["n_developments"], t["dimensions"]) for t in built] == [
        (_ESTATE, "built", 2, ["disruption_status"])]
    async with pg_pool.acquire() as conn:
        ref = await conn.fetchrow("SELECT * FROM unit_references WHERE target_id = $1", _ESTATE)
    assert ref["builder"] == "deterministic:kev_fixture"
    ref_json = json.loads(ref["ref_json"]) if isinstance(ref["ref_json"], str) else ref["ref_json"]
    assert [d["key"] for d in ref_json["ref_developments"]] == ["CVE-2026-1002", "CVE-2026-1001"]
    assert ref_json["keyed"]["complete"] is False
    assert ref_json["header"]["subject"]["name"] == "Estate — Fortinet FortiGate"
    assert float(ref["span_verified_rate"]) == 1.0

    # Grade a head against it: no model wired at all.
    async with pg_pool.acquire() as conn:
        await _calibration(conn)
        await _finding(conn, _ESTATE, "disruption_status",
                       "## What changed\n" + "\n".join(_KEYED_CLAIMS.values()))
    graded = await _run(pg_pool, "correctness_grader", grader_targets=[_ESTATE],
                        as_of=(_T0 + timedelta(minutes=5)).isoformat())
    outcome = graded.finding.data["per_target"][0]
    assert outcome["status"] == "ok", outcome
    async with pg_pool.acquire() as conn:
        unit = await conn.fetchrow("SELECT * FROM unit_correctness WHERE target_id = $1", _ESTATE)
        claims = await conn.fetch(
            "SELECT claim_text, adjudicated, label_by_family FROM unit_correctness_claims "
            "WHERE correctness_id = $1", unit["id"])
    by_text = {c["claim_text"]: c["adjudicated"] for c in claims}
    for key, text in _KEYED_CLAIMS.items():
        expected = key.split("_")[0]
        got = [v for t, v in by_text.items() if text[:40] in t]
        assert got == [expected], (key, by_text)
    families = json.loads(unit["families"]) if isinstance(unit["families"], str) else unit["families"]
    assert families["D0"]["model"] == KEYED.MODEL and families["keyed"]["complete"] is False
    assert float(unit["cost_usd"]) == 0.0
    assert (unit["n_contains"], unit["n_contradicts"], unit["n_silent"]) == (1, 1, 2)
    assert float(unit["correctness_share"]) == 0.5 and float(unit["coverage_share"]) == 0.5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_incomplete_list_with_nothing_bound_in_window_writes_nothing(pg_pool, world, monkeypatch):
    monkeypatch.setenv(RD.ENABLED_ENV, "1")

    async def fetch(url: str) -> str:
        return json.dumps(_kev(_T0))

    result = await _run(pg_pool, "reference_deterministic",
                        extras={RD.FETCH_DEPS_EXTRA_KEY: fetch},
                        as_of=_T0.isoformat(), bindings=[f"{_ESTATE}=Citrix"], **_KEV_OPTS)
    assert [t["status"] for t in result.finding.data["per_target"]] == ["empty_window"]
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM unit_references WHERE target_id = $1", _ESTATE) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_keyed_kind_is_inert_off_and_unbound(pg_pool, monkeypatch):
    async def fetch(url: str) -> str:  # pragma: no cover — must never run
        raise AssertionError("fetched while off")

    monkeypatch.delenv(RD.ENABLED_ENV, raising=False)
    off = await _run(pg_pool, "reference_deterministic", extras={RD.FETCH_DEPS_EXTRA_KEY: fetch},
                     bindings=[f"{_ESTATE}=Fortinet"], **_KEV_OPTS)
    assert off.finding.data["enabled"] is False
    monkeypatch.setenv(RD.ENABLED_ENV, "1")
    unbound = await _run(pg_pool, "reference_deterministic",
                         extras={RD.FETCH_DEPS_EXTRA_KEY: fetch}, bindings=[], **_KEV_OPTS)
    assert unbound.finding.data["per_target"] == []
    assert "no subject is bound" in unbound.finding.body


# ---------------------------------------------------------------------------
# 5. A profile with no gazetteer still builds a reference for its subjects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_profile_with_no_gazetteer_names_every_subject_by_its_descriptor():
    regime = {"target_id": "regime_ofac_russia", "name": "Regime — OFAC Russia-related sanctions",
              "geo": [], "domain": "thematic", "subject_code": None}
    single = {"target_id": "desk_single_geo", "name": "Desk — Single Geo", "geo": ["AR"],
              "domain": "geo", "subject_code": None}
    try:
        GZ.refresh(GZ.build(None, profile="no_gazetteer", origin="pytest"))
        conn = _Conn([regime, single], {"AR": "Argentina"})
        ident = await ROSTER.target_identity(conn, ["regime_ofac_russia", "desk_single_geo"],
                                             detail=True)
    finally:
        GZ.refresh()
    assert "gazetteer" not in conn.calls, "an ABSENT gazetteer is never read"
    assert ident["desk_single_geo"]["name"] == "Desk — Single Geo"
    assert ident["desk_single_geo"]["polity"] is False
    reg = ident["regime_ofac_russia"]
    assert (reg["code"], reg["noun"], reg["query_name"]) == (
        "regime_ofac_russia", "regime", "OFAC Russia-related sanctions")
    # and the instruction it builds is about a regime, not a country
    text = INSTR.build_instruction(
        country_name=reg["name"], country_code=reg["code"], t0="T0", window_start="W",
        dimensions=["designation_change"], item_prefix="RD-X-C", builder_label="b",
        model_name="m", tool_call_cap=10, subject_noun=reg["noun"], query_subject=reg["query_name"],
    )
    assert "knowledgeable regime analyst" in text
    assert "`OFAC Russia-related sanctions designation change announcement`" in text


# ---------------------------------------------------------------------------
# 6. The geopolitics country desks: byte-identical to the pin
# ---------------------------------------------------------------------------

#: sha256 of the pin's (09be8bea) instruction / query block for this fixture.
_PIN_INSTRUCTION_SHA = "65d1a36820f3be54a7d4f1c9528bbb87176b341a93c1062797a640c43b4d0c8f"
_PIN_QUERY_SHA = "eae26bf066906d8b229e40f8a40ea164eca18a583b861b4d4bd626cf981dffcb"


@pytest.mark.asyncio
async def test_a_country_desks_instruction_and_queries_are_the_pins_bytes():
    ident = (await ROSTER.target_identity(
        _Conn([_COUNTRY_ROW], {"AR": "Argentina"}), ["country_g20_ar"], detail=True,
    ))["country_g20_ar"]
    dims = list(rosters.scored_subject_units())
    text = INSTR.build_instruction(
        country_name=ident["name"], country_code=ident["code"],
        t0="2026-10-01T00:00:00+00:00", window_start="2026-09-17T00:00:00+00:00",
        dimensions=dims, item_prefix="RD-AR-C", builder_label="core-plane-lane",
        model_name="pytest-core", tool_call_cap=200, min_developments=5,
        subject_noun=ident["noun"], query_subject=ident["query_name"],
    )
    assert hashlib.sha256(text.encode()).hexdigest() == _PIN_INSTRUCTION_SHA
    assert hashlib.sha256(MANIFEST.query_guidance(ident["query_name"], dims).encode()
                          ).hexdigest() == _PIN_QUERY_SHA


_DESK_TEXT = (
    "These intra‑elite disputes raise the base probability of a coup‑type seizure "
    "despite the country’s traditionally low coup‑vulnerability, because they erode "
    "regime cohesion and create openings for rival factions."
)
_COUNTRY_REFERENCE = {
    "header": {"t0": _T0.isoformat()},
    "ref_bands": {u: "elevated" for u in rosters.scored_subject_units()},
    "ref_developments": [{
        "item_id": "RD-ZZ-C-1", "summary": "Elite disputes widened.",
        "decisive_span": "Rival factions inside the ruling elite traded public accusations.",
        "outlet": "Wire", "publish_date": "2026-10-01", "source_url": "https://example.test/zz",
        "source_tier": 2, "significance": "major", "dimension": ["internal_stability"],
    }],
}


async def _grade_country(pool) -> tuple[list[dict], list[dict], dict]:
    llm = _AlwaysLLM("contains", "Rival factions inside the ruling elite traded public accusations.")
    result = await _run(pool, "correctness_grader", extras={CG.F0_DEPS_EXTRA_KEY: llm},
                        grader_targets=[_COUNTRY], as_of=_T0.isoformat())
    async with pool.acquire() as conn:
        units = await conn.fetch(
            "SELECT analyst_id, head_id, grain, n_claims, n_contains, n_contradicts, n_silent, "
            "n_split, n_unparseable, n_single_family, correctness_share, coverage_share, "
            "families, cost_usd, rubric_sha FROM unit_correctness WHERE target_id = $1 "
            "ORDER BY analyst_id", _COUNTRY)
        claims = await conn.fetch(
            "SELECT c.claim_id, c.grain, c.claim_text, c.label_by_family, c.adjudicated, "
            "c.n_families, c.single_family, c.spans FROM unit_correctness_claims c "
            "JOIN unit_correctness u ON u.id = c.correctness_id WHERE u.target_id = $1 "
            "ORDER BY c.claim_id", _COUNTRY)
        await conn.execute("DELETE FROM unit_correctness WHERE target_id = $1", _COUNTRY)
    return [dict(u) for u in units], [dict(c) for c in claims], result.finding.data["per_target"][0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_country_desks_grades_are_byte_identical_to_the_pins_rule(
    pg_pool, world, monkeypatch
):
    """The SAME fixture graded under the pin's rule (the scored units, every
    unit expected) and under INST-1's (the reference units, the head's tags):
    every unit row and every claim row equal, byte for byte. The ONE intended
    difference is the receipt's `missing` line — the pin named
    proliferation_watch missing on a g20 desk that does not run it."""
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.delenv(GRADE.CEILING_ENV, raising=False)
    async with pg_pool.acquire() as conn:
        await _calibration(conn)
        await _reference(conn, _COUNTRY, _COUNTRY_REFERENCE)
        await _finding(conn, _COUNTRY, "internal_stability", "## What changed\n" + _DESK_TEXT)
        await _finding(conn, _COUNTRY, "escalation", "## What changed\n" + _DESK_TEXT.replace(
            "intra‑elite disputes", "border frictions"))

    async def _pin_expected(conn, target_id, units):
        return None

    with monkeypatch.context() as pin:
        pin.setattr(ROSTER, "reference_units", rosters.scored_subject_units)
        pin.setattr(ROSTER, "expected_units", _pin_expected)
        pin_units, pin_claims, pin_outcome = await _grade_country(pg_pool)
    new_units, new_claims, new_outcome = await _grade_country(pg_pool)

    assert pin_units and pin_claims
    assert new_units == pin_units
    assert new_claims == pin_claims
    pin_missing = [w for w in pin_outcome["warnings"] if "no head as of the stamp" in w]
    new_missing = [w for w in new_outcome["warnings"] if "no head as of the stamp" in w]
    assert "proliferation_watch" in pin_missing[0]
    assert all("proliferation_watch" not in w for w in new_missing)
    assert all("disruption_status" not in w for w in new_missing)
