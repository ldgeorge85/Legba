# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D5 — the ``standing_auditor`` deterministic sub-handler (STANDING EXTERNAL AUDIT).

Two layers, deliberately:

**Pure** — the properties that must hold without a database or a network: the
date-seeded rotation is REPLAYABLE (same date + same desks ⇒ same sample) and
actually rotates day-over-day; the verdict parser drops a FABRICATED source URL
and demotes the unsourced verdict that leaves behind; the heartbeat's
``claims_checked`` EXCLUDES ``UNCHECKED``, which is the whole reason the
heartbeat exists.

**End-to-end, through the REAL binding path** — a live migrated Postgres, the
REAL ``Agency.run_pack_tool`` three-way gate, the REAL ``web_access`` ActionPack
loaded from the shipped descriptor, and the REAL ``web_search`` tool handler.
The ONLY doubles are the two sanctioned boundaries: the LLM, and the SEARCH
PROVIDER bound at ``ToolContext.search`` — which is the PACK's own injection
seam, not the analyst's. Nothing in ``standing_auditor`` is monkeypatched, so
if the handler stopped routing through the pack (an ad-hoc httpx call, say) the
fake provider would never be consulted AND no ``action_pack_invocations`` ledger
row would land — the assertion that makes this a binding-path test rather than a
shape test.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts.agency import Agency, AgencyToolBinding, ToolContext
from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import standing_auditor as sa
from legba.data.analysts.deterministic_handlers._external_audit_sampling import (
    CheckableClaim,
    ClaimVerdict,
    SampledHead,
    normalize_core_plane_text,
    parse_claims_reply,
    parse_verdict_reply,
    rotate_desks,
)
from legba.data.provenance.kinds import TRACE_ONLY
from legba.data.schemas.action_pack import ActionPack, ActionPackRef
from legba.runtime.analyst_method import AnalystMethodResult

_DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"


# ---------------------------------------------------------------------------
# Helpers — heads
# ---------------------------------------------------------------------------


def _head(desk: str, *, severity: str | None = "high",
          delta: str | None = "steady", analyst: str = "country_composition",
          output_id: Any = None) -> SampledHead:
    return SampledHead(
        output_id=output_id or uuid4(),
        analyst_id=analyst,
        target_id=desk,
        desk_key=desk,
        title=f"{desk} read",
        body="body",
        severity=severity,
        severity_delta=delta,
    )


# ---------------------------------------------------------------------------
# 1) Registration — the sub-handler is really bound, and TRACE_ONLY
# ---------------------------------------------------------------------------


def test_sub_handler_is_registered_and_trace_only():
    """A summary finding here would put an AUDIT RECEIPT into the finding stream
    the audit exists to grade. The real product is the side-written critique /
    alert rows, which this map does not govern."""
    assert SUB_HANDLERS["standing_auditor"] is sa.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER["standing_auditor"] is TRACE_ONLY


def test_descriptor_ships_the_core_plane_and_the_web_pack():
    """The two house constraints, asserted off the shipped YAML: a scheduled
    analyst runs the $0 core plane (never Anthropic), and every external byte it
    reads comes through a registered PACK tool."""
    body = yaml.safe_load(
        (_DESCRIPTORS / "analyst_standing_auditor.yaml").read_text()
    )
    assert body["identity"]["kind"] == "deterministic"
    assert body["method"]["sub_handler"] == "standing_auditor"
    primary = body["method"]["llm"]["primary"]["raw"]
    assert primary == "llm.primary.openai_compat"
    assert "anthropic" not in primary.lower()
    assert [g["pack_id"] for g in body["action_packs"]] == ["web_access"]


def test_descriptor_validates_and_every_declared_option_resolves():
    """Drive the SHIPPED yaml through the real `AnalystDescriptor` schema and the
    real X-1 resolver. A knob the catalog rejects is dead config that would
    silently fall back to the in-source default at deploy time — the exact
    defect X-1 exists to prevent, and the cheapest place to catch a 422 on the
    registration PUT is here rather than on the deploy train."""
    from legba.data.analysts.handler_options import resolve_handler_options
    from legba.data.schemas.analyst import AnalystDescriptor

    body = yaml.safe_load(
        (_DESCRIPTORS / "analyst_standing_auditor.yaml").read_text()
    )
    d = AnalystDescriptor.model_validate(body, strict=False)
    assert d.method.sub_handler == "standing_auditor"

    resolved = resolve_handler_options(
        "standing_auditor", dict(getattr(d.method, "options", None) or {})
    )
    assert resolved.rejected == (), f"dead config: {resolved.rejected}"
    assert set(resolved.accepted) == {
        "window_hours", "max_desks", "max_claims_per_head",
        "max_claims_total", "search_limit",
    }
    # And the handler really reads them: a descriptor-set cap must bind.
    assert sa._pos(resolved.accepted.get("max_desks"), 99) == 3


# ---------------------------------------------------------------------------
# 2) Deterministic sampling — replayable, and it actually rotates
# ---------------------------------------------------------------------------


def test_rotation_is_replayable_for_the_same_date_and_desk_set():
    """The audit's sample must be re-derivable from the date alone a month
    later, or a disputed CONTRADICTED verdict can never be replayed."""
    heads = [_head(f"desk_{i}") for i in range(9)]
    a = rotate_desks(heads, date_key="2026-08-29", take=3)
    b = rotate_desks(list(reversed(heads)), date_key="2026-08-29", take=3)
    assert [h.desk_key for h in a] == [h.desk_key for h in b]


@pytest.mark.parametrize("n,take", [(9, 3), (7, 3), (30, 3), (5, 2)])
def test_rotation_covers_every_desk_within_its_bounded_period(n, take):
    """A STEPPING rotation, not sampling-with-replacement: within ceil(n/take)
    consecutive days every desk is visited, so worst-case time-since-last-audit
    is BOUNDED. A merely hash-offset window has no such bound — a desk can go
    uncovered for a long tail of days by luck."""
    heads = [_head(f"desk_{i}") for i in range(n)]
    period = -(-n // take)  # ceil
    covered: set[str] = set()
    for d in range(1, period + 1):
        covered |= {
            h.desk_key
            for h in rotate_desks(heads, date_key=f"2026-08-{d:02d}", take=take)
        }
    assert covered == {h.desk_key for h in heads}


def test_rotation_advances_by_one_window_per_day():
    """Consecutive days step by exactly `take` — the mechanism behind the bound
    above, asserted directly so a future 'simplification' back to a bare hash
    offset fails here rather than silently unbounding the coverage."""
    heads = [_head(f"desk_{i}") for i in range(9)]
    day1 = rotate_desks(heads, date_key="2026-08-01", take=3)
    day2 = rotate_desks(heads, date_key="2026-08-02", take=3)
    assert not ({h.desk_key for h in day1} & {h.desk_key for h in day2})


def test_rotation_pre_sorts_high_severity_and_movement_first():
    """Within a day's slots, the desks that just MOVED at a high standing band
    come first — but the offset still advances, so they cannot monopolize."""
    heads = [
        _head("quiet", severity="low", delta="steady"),
        _head("hot", severity="critical", delta="rose"),
        _head("warm", severity="high", delta="steady"),
    ]
    assert [h.desk_key for h in rotate_desks(heads, date_key="d", take=3)] == [
        "hot", "warm", "quiet",
    ]


def test_rotation_take_zero_or_no_desks_selects_nothing():
    assert rotate_desks([_head("a")], date_key="d", take=0) == []
    assert rotate_desks([], date_key="d", take=3) == []


# ---------------------------------------------------------------------------
# 3) The auditor cannot itself invent a source
# ---------------------------------------------------------------------------


def _claim() -> CheckableClaim:
    return CheckableClaim(claim="X happened on 3 March", query="X 3 March",
                          head=_head("iran"))


def test_full_width_brackets_are_normalized_before_anything_parses():
    assert normalize_core_plane_text("as 【2】 reports") == "as [2] reports"


def test_claims_parse_survives_a_fenced_full_width_reply():
    """The core plane emits fences it was told not to and CJK brackets it was
    never asked for. Both are normalized before the JSON is read."""
    reply = (
        "```json\n"
        '{"claims": [{"claim": "Iran resumed enrichment 【1】", '
        '"query": "Iran enrichment resumed"}]}\n'
        "```"
    )
    claims = parse_claims_reply(reply, _head("iran"), cap=2)
    assert len(claims) == 1
    assert claims[0].claim == "Iran resumed enrichment [1]"


def test_claims_parse_respects_the_per_head_cap_and_drops_partials():
    reply = json.dumps({"claims": [
        {"claim": "a", "query": "qa"},
        {"claim": "b"},                    # no query — dropped, not invented
        {"claim": "c", "query": "qc"},
        {"claim": "d", "query": "qd"},
    ]})
    claims = parse_claims_reply(reply, _head("iran"), cap=2)
    assert [c.claim for c in claims] == ["a", "c"]


def test_unparsable_extraction_yields_no_claims_rather_than_a_guess():
    assert parse_claims_reply("I could not comply.", _head("iran"), cap=2) == []


def test_a_fabricated_source_url_is_dropped():
    """The exact failure this analyst exists to catch in others, refused in
    itself: a URL the judge never saw in the results is discarded."""
    reply = json.dumps({
        "verdict": "SUPPORTED",
        "rationale": "reported widely",
        "evidence": [
            {"url": "https://real.example/a", "quote": "X happened"},
            {"url": "https://invented.example/b", "quote": "also X"},
        ],
    })
    v = parse_verdict_reply(
        reply, _claim(), allowed_urls=["https://real.example/a"]
    )
    assert v.verdict == "SUPPORTED"
    assert v.source_urls == ["https://real.example/a"]


def test_an_unsourced_verdict_is_demoted_to_not_found():
    """A SUPPORTED/CONTRADICTED left with no surviving URL is not a verdict."""
    reply = json.dumps({
        "verdict": "CONTRADICTED",
        "rationale": "I recall otherwise",
        "evidence": [{"url": "https://invented.example/b", "quote": "no"}],
    })
    v = parse_verdict_reply(reply, _claim(), allowed_urls=["https://real/a"])
    assert v.verdict == "NOT_FOUND"
    assert "demoted" in v.rationale


def test_an_out_of_vocabulary_verdict_becomes_not_found():
    reply = json.dumps({"verdict": "PROBABLY_TRUE", "rationale": "eh"})
    assert parse_verdict_reply(reply, _claim(), allowed_urls=[]).verdict == (
        "NOT_FOUND"
    )


# ---------------------------------------------------------------------------
# 4) The heartbeat — the 08-12 lesson
# ---------------------------------------------------------------------------


def test_heartbeat_excludes_unchecked_claims_from_claims_checked():
    """A run whose search plane is dead still ends status='success'. If
    UNCHECKED counted as checked, the heartbeat would show a dead auditor as a
    busy one — which is exactly how the judge outage stayed invisible."""
    state = sa.build_heartbeat_state(
        ran_at=datetime.now(timezone.utc),
        heads_sampled=["world", "iran"],
        claims_extracted=4,
        claims_checked=1,           # the handler computes this over CHECKED only
        verdict_mix={"SUPPORTED": 1, "UNCHECKED": 3},
        critiques=4, alerts=0, write_failures=0,
        degraded_reason="",
    )
    assert state["claims_checked"] == 1
    assert state["verdicts"]["UNCHECKED"] == 3
    assert state["healthy"] is True


def test_heartbeat_reports_a_degraded_run_as_unhealthy():
    state = sa.build_heartbeat_state(
        ran_at=datetime.now(timezone.utc), heads_sampled=[],
        claims_extracted=0, claims_checked=0, verdict_mix={},
        critiques=0, alerts=0, write_failures=0,
        degraded_reason="no web_access binding wired",
    )
    assert state["degraded"] is True
    assert state["healthy"] is False
    assert "web_access" in state["degraded_reason"]


# ---------------------------------------------------------------------------
# 5) Alert gating + the critique's independence from the faithfulness plane
# ---------------------------------------------------------------------------


def _verdict(v: str, severity: str | None) -> ClaimVerdict:
    return ClaimVerdict(
        claim=CheckableClaim(claim="c", query="q",
                             head=_head("iran", severity=severity)),
        verdict=v,
        rationale="because",
        quotes=["the source says otherwise"],
        source_urls=["https://real.example/a"],
    )


@pytest.mark.parametrize(
    "verdict,severity,expected",
    [
        ("CONTRADICTED", "critical", True),
        ("CONTRADICTED", "high", True),
        ("CONTRADICTED", "moderate", False),   # a DQ note, not a page
        ("CONTRADICTED", None, False),
        ("SUPPORTED", "critical", False),
        ("NOT_FOUND", "critical", False),      # absence never pages
    ],
)
def test_only_a_contradicted_high_severity_claim_is_alertable(
    verdict, severity, expected
):
    assert _verdict(verdict, severity).alertable is expected


def test_critique_title_can_never_be_caught_by_the_faithfulness_pin():
    """Every faithfulness consumer pins ``title LIKE 'Faithfulness verify%'``.
    An external-audit row landing in that population would corrupt it."""
    p = sa.build_audit_critique_payload(_verdict("CONTRADICTED", "high"))
    assert p.title.startswith(sa.CRITIQUE_TITLE_PREFIX)
    assert not p.title.startswith("Faithfulness verify")
    audit = p.data[sa.EXTERNAL_AUDIT_DATA_KEY]
    assert audit["pipeline_version"] == sa.EXTERNAL_AUDIT_PIPELINE_VERSION


def test_the_audit_plane_does_not_borrow_the_judge_pipeline_version():
    """An independent plane needs an independent population key — pooling the
    two would describe a population that never existed."""
    from legba.data.provenance.judge_pipeline_version import (
        JUDGE_PIPELINE_VERSION,
    )

    assert sa.EXTERNAL_AUDIT_PIPELINE_VERSION != JUDGE_PIPELINE_VERSION


def test_not_found_does_not_demote_the_audited_read():
    """``effective_confidence = min(confidence, overall_score)``. NOT_FOUND means
    only that the SEARCH did not settle it; scoring it below 1.0 would let a
    degraded search plane quietly demote sound reads fleet-wide."""
    assert sa.build_audit_critique_payload(
        _verdict("NOT_FOUND", "high")).overall_score == 1.0
    assert sa.build_audit_critique_payload(
        _verdict("SUPPORTED", "high")).overall_score == 1.0
    assert sa.build_audit_critique_payload(
        _verdict("CONTRADICTED", "high")).overall_score == 0.0


def test_alert_payload_carries_the_trigger_class_the_budget_will_read():
    """INTEGRATION POINT: the unmerged alert-suppression-guard branch ranks by
    trigger_class. The row already carries it, in tags and in routing_hint."""
    a = sa.build_audit_alert_payload(_verdict("CONTRADICTED", "high"))
    assert a.routing_hint == sa.ALERT_TRIGGER_CLASS
    assert f"trigger:{sa.ALERT_TRIGGER_CLASS}" in a.tags
    assert a.severity == "high"
    assert sa.build_audit_alert_payload(
        _verdict("CONTRADICTED", "critical")).severity == "critical"


# ---------------------------------------------------------------------------
# 6) Refuse loud on a missing pool
# ---------------------------------------------------------------------------


class _NoPoolDeps:
    pg_pool = None
    extras: dict[str, Any] = {}


@pytest.mark.asyncio
async def test_missing_pool_raises_rather_than_reporting_a_clean_audit():
    with pytest.raises(RuntimeError, match="deps.pg_pool"):
        await sa.handle(None, {}, _NoPoolDeps())


# ---------------------------------------------------------------------------
# 7) END-TO-END through the REAL binding path
# ---------------------------------------------------------------------------

pytestmark_e2e = pytest.mark.asyncio


class _FakeSearchProvider:
    """A ``SearchProviderHandler``-shaped double bound at ``ToolContext.search``.

    This is the PACK's own injection seam (the field exists precisely so a
    caller can supply an isolated provider), so the REAL ``web_search_tool``
    handler still runs: the provider ladder, the degradation read, the
    empty-is-suspect probe and the ToolResult shaping are all live. Records
    every query it was asked, which is how the test proves the analyst went
    through the pack rather than around it.
    """

    def __init__(self, results: list[dict[str, str]]):
        self._results = results
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 5, params=None):
        from legba.data.stack.search.base import (
            SearchResponse,
            SearchResult,
            SearchStatus,
        )

        self.queries.append(query)
        return SearchResponse(
            query=query,
            results=[
                SearchResult(url=r["url"], title=r["title"], snippet=r["snippet"],
                             rank=i + 1)
                for i, r in enumerate(self._results[:limit])
            ],
            provider="test.fake",
            subprovider="fake",
            status=SearchStatus.OK,
        )


class _ScriptedLLM:
    """The one sanctioned boundary double. Replies in call order.

    Carries a ``usage.model`` because the handler reads it for provenance: which
    model rendered a verdict has to survive onto the critique row, or a
    core-plane model swap silently pools two graders' verdicts.
    """

    MODEL = "gpt-oss-120b-test"

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[str] = []

    async def chat_complete(self, messages, **kwargs):
        self.calls.append(str(messages[0]["content"]))
        content = self._replies.pop(0) if self._replies else "{}"
        usage = type("_U", (), {"model": self.MODEL})()
        return type("_R", (), {"content": content, "usage": usage})()


class _Deps:
    def __init__(self, pool, extras):
        self.pg_pool = pool
        self.extras = extras


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    async with p.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('action_pack_invocations')")
        assert await conn.fetchval("SELECT to_regclass('alert_trigger_watermarks')")
    yield p
    await p.close()


def _web_access_pack() -> ActionPack:
    """The SHIPPED web_access pack, loaded from its own descriptor — not a
    hand-built stand-in, so the tool config (provider ref, timeouts) and the
    governor limits under test are the ones production runs."""
    body = yaml.safe_load(
        (_DESCRIPTORS / "action_pack_web_access.yaml").read_text()
    )
    body["identity"]["version"] = "a" * 16
    return ActionPack.model_validate(body, strict=False)


class _Seeded(NamedTuple):
    world_id: UUID
    desk_id: UUID
    desk_key: str
    run_id: UUID


async def _seed_heads(conn) -> _Seeded:
    """One world read + one desk head NOTHING ELSE IN THE SUITE OWNS.

    NOTHING IS DELETED HERE, and that is the point. The obvious shape for these
    tests was a blank slate — ``TRUNCATE analyst_outputs`` before each one — and
    it is exactly the defect ``test_analyst_situation_tracker``'s ``scope``
    fixture documents at length: an unscoped wipe of the SUITE's findings,
    critiques, scorecards and alerts manufactures order-dependence for every
    file that ran earlier while claiming to defend against it. (This file
    shipped that wipe for one draft; the full-suite run turned three unrelated
    files red and that is how it was caught.)

    So instead, isolation comes from two handles the run itself carries:

      * a ``desk_key`` no other row can hold, prefixed ``aaa_`` so the audit's
        (severity, delta, desk_key) pre-sort puts it FIRST among the live desks
        at ``severity:critical`` + ``severity_delta:rose``. Combined with a
        ``max_desks`` above the fetch cap — which makes ``rotate_desks`` return
        every desk in pre-sorted order with no rotation offset — the head this
        test audits is deterministic no matter what else the suite has left in
        ``analyst_outputs``;
      * a ``run_id`` the caller passes into the handler and every assertion
        scopes on, so a critique or alert count can never be inflated by a
        sibling test's rows.

    The world read needs no such trick: ``_WORLD_HEAD_SQL`` takes the newest
    non-superseded ``world_assessor`` row, and ``now()`` is transaction-start
    time, so a row inserted here is strictly newer than anything a serially
    earlier test wrote.

    The desk head DOES need one more step, and it is the second thing the first
    draft got wrong. Each call mints a fresh ``aaa_audit_<hex>`` desk, so after
    two tests there are two equally-ranked ``critical``/``rose`` desks and the
    pre-sort's final tiebreak — ``desk_key`` ascending — hands the slot to
    whichever hex happened to sort lower, i.e. an EARLIER test's desk. So every
    previously-seeded audit desk is SUPERSEDED here (``superseded_by`` pointing
    at this call's head, the same column the head query already filters on).
    Nothing is deleted, and the update can only ever touch rows this file
    created — ``target_id LIKE 'aaa_audit_%'`` matches nothing else in the tree.
    """
    world_id, desk_id = uuid4(), uuid4()
    desk_key = f"aaa_audit_{uuid4().hex[:8]}"
    for row_id, analyst_id, target_id, title, tags in (
        (world_id, "world_assessor", None, "World read",
         ["severity:moderate", "severity_delta:steady"]),
        (desk_id, "country_composition", desk_key, "Audit desk read",
         ["severity:critical", "severity_delta:rose"]),
    ):
        await conn.execute(
            """
            INSERT INTO analyst_outputs
                (id, kind, title, body, confidence, data, target_id,
                 analyst_id, analyst_version, schema_uri, produced_at)
            VALUES ($1, 'finding', $2, $3, 0.8, $4::jsonb, $5, $6, $7, $8, now())
            """,
            row_id, title, f"{title} body",
            json.dumps({"tags": tags, "data": {"meta": True}}),
            target_id, analyst_id, "b" * 16,
            "iglu:legba/finding/jsonschema/1-0-0",
        )
    # Retire every audit desk an earlier test in this file seeded, so exactly
    # ONE `aaa_audit_%` head is live and the pre-sort cannot hand the slot to a
    # sibling's leftover. Scoped to this file's own rows; nothing is deleted.
    await conn.execute(
        """
        UPDATE analyst_outputs
           SET superseded_by = $1, superseded_at = now()
         WHERE target_id LIKE 'aaa_audit_%'
           AND id <> $1
           AND superseded_by IS NULL
        """,
        desk_id,
    )
    return _Seeded(world_id, desk_id, desk_key, uuid4())


async def _reset_audit_watermark(conn) -> None:
    """The ONE reset that is safe, and the same carve-out
    ``test_analyst_situation_tracker`` keeps: a single ``trigger_class`` this
    analyst exclusively owns. It is not a shared table in any meaningful sense —
    no other writer in the tree uses ``external_audit``."""
    await conn.execute(
        "DELETE FROM alert_trigger_watermarks WHERE trigger_class = $1",
        sa.ALERT_TRIGGER_CLASS,
    )


def _run_options(seeded: _Seeded) -> dict[str, Any]:
    """Options that make the sampled head set deterministic against a suite-dirty
    ``analyst_outputs``: take EVERY desk (so `rotate_desks` returns the pre-sorted
    list with no rotation offset — the seeded `aaa_`/critical/rose desk first),
    then let `max_claims_total` stop the run after the world head and that desk."""
    return {
        "analyst_id": "standing_auditor",
        "run_id": seeded.run_id,
        "max_desks": 500,          # > _DESK_FETCH_CAP ⇒ take >= n ⇒ no rotation
        "max_claims_total": 2,     # world head + the seeded desk, then stop
    }


def _binding(pool, provider: _FakeSearchProvider) -> AgencyToolBinding:
    """The REAL binding, self-allowing its own pack under GLOBAL_SCOPE — the
    same construction ``external_audit_binding`` performs in production."""
    return AgencyToolBinding(
        agency=Agency(),
        pack=_web_access_pack(),
        pg_pool=pool,
        tool_context=ToolContext(search=provider),
        analyst_grants=[ActionPackRef(pack_id="web_access")],
        target_allows=[ActionPackRef(pack_id="web_access")],
        requested_by="analyst::standing_auditor",
        budget_account="standing_auditor",
    )


_CONTRADICTING_RESULTS = [
    {"url": "https://news.example/a",
     "title": "Talks collapse",
     "snippet": "Officials confirmed the agreement was never signed."},
]


@pytest.mark.asyncio
async def test_end_to_end_writes_critiques_an_alert_and_a_heartbeat(pool):
    """The whole organ, through the real gate.

    Proves, in one run: the search reached the provider THROUGH the pack (a
    settled ``action_pack_invocations`` row + the provider saw the query); a
    critique row landed per verdict under the External-audit title prefix; the
    CONTRADICTED verdict on the ``severity:high`` desk emitted a kind='alert'
    row; and the heartbeat row records what the run actually checked.
    """
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        seeded = await _seed_heads(conn)
        ledger_before = await conn.fetchval(
            "SELECT count(*) FROM action_pack_invocations WHERE pack_id = "
            "'web_access' AND requested_by = 'analyst::standing_auditor'"
        )

    provider = _FakeSearchProvider(_CONTRADICTING_RESULTS)
    llm = _ScriptedLLM([
        # world head — extraction, then verdict
        json.dumps({"claims": [
            {"claim": "The agreement was signed in March",
             "query": "agreement signed March"},
        ]}),
        json.dumps({"verdict": "NOT_FOUND",
                    "rationale": "the results do not settle it"}),
        # the seeded desk head — extraction, then a CONTRADICTED verdict
        json.dumps({"claims": [
            {"claim": "The desk signed the agreement 【1】",
             "query": "desk agreement signed"},
        ]}),
        json.dumps({
            "verdict": "CONTRADICTED",
            "rationale": "reporting says it was never signed",
            "evidence": [{"url": "https://news.example/a",
                          "quote": "the agreement was never signed"}],
        }),
    ])
    deps = _Deps(pool, {
        sa.LLM_DEPS_EXTRA_KEY: llm,
        sa.WEB_BINDING_DEPS_EXTRA_KEY: _binding(pool, provider),
    })

    result = await sa.handle(None, _run_options(seeded), deps)
    assert isinstance(result, AnalystMethodResult)

    # -- the search went through the PACK, not around it --------------------
    assert provider.queries, "the pack tool never reached the bound provider"
    async with pool.acquire() as conn:
        ledger = await conn.fetch(
            "SELECT tool_name, outcome, requested_by, budget_account "
            "FROM action_pack_invocations WHERE pack_id = 'web_access' "
            "AND requested_by = 'analyst::standing_auditor'"
        )
        assert len(ledger) > ledger_before, (
            "no new invocation ledger row — the gate was bypassed"
        )
        assert {r["tool_name"] for r in ledger} == {"web_search"}
        assert "completed" in {r["outcome"] for r in ledger}
        # The budget account stays the PACK's own (`web_access`): the pack owns
        # its governor and every caller shares that one external-egress budget
        # by design, while `requested_by` carries the analyst attribution.
        assert {r["budget_account"] for r in ledger} == {"web_access"}

        # -- one critique per verdict, under the audit's own title prefix ----
        # Scoped by run_id: this suite shares one database, and an unscoped
        # count would be inflated by every sibling that ever wrote a critique.
        critiques = await conn.fetch(
            "SELECT title, confidence, data, derived_from FROM analyst_outputs "
            "WHERE kind = 'critique' AND run_id = $1 ORDER BY title",
            seeded.run_id,
        )
        assert len(critiques) == 2
        assert all(
            r["title"].startswith(sa.CRITIQUE_TITLE_PREFIX) for r in critiques
        )
        payloads = [json.loads(r["data"]) for r in critiques]
        verdicts = {
            p["data"][sa.EXTERNAL_AUDIT_DATA_KEY]["verdict"] for p in payloads
        }
        assert verdicts == {"NOT_FOUND", "CONTRADICTED"}
        # Provenance: WHICH model graded, on the row, so a core-plane swap can
        # never silently pool two graders' verdicts.
        assert {p["judge_model"] for p in payloads} == {_ScriptedLLM.MODEL}
        # lineage: each critique points back at the head it audited
        cited = set()
        for r in critiques:
            cited |= set(r["derived_from"])
        assert cited == {seeded.world_id, seeded.desk_id}

        # -- the alert, gated on the AUDITED claim's standing severity -------
        alerts = await conn.fetch(
            "SELECT title, severity, data FROM analyst_outputs "
            "WHERE kind = 'alert' AND run_id = $1",
            seeded.run_id,
        )
        assert len(alerts) == 1, "expected exactly the one contradiction"
        # The seeded desk stands at severity:critical, so the alert inherits it.
        assert alerts[0]["severity"] == "critical"
        assert json.loads(alerts[0]["data"])["data"]["trigger_class"] == (
            sa.ALERT_TRIGGER_CLASS
        )

        # -- the heartbeat ---------------------------------------------------
        hb = await conn.fetchrow(
            "SELECT state, fired_at FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            sa.ALERT_TRIGGER_CLASS, sa.HEARTBEAT_KEY,
        )
        assert hb is not None, "the auditor ran without recording that it ran"
        state = json.loads(hb["state"])
        assert state["claims_checked"] == 2
        assert state["verdicts"]["CONTRADICTED"] == 1
        assert state["alerts_written"] == 1
        assert state["healthy"] is True
        # The world read is always sampled; the seeded desk sorts first among
        # the live desks, so it is the one the capped run reached.
        assert state["heads_sampled"][:2] == ["world", seeded.desk_key]
        assert hb["fired_at"] is not None, "a run that PAGED must stamp fired_at"


@pytest.mark.asyncio
async def test_a_degraded_search_plane_yields_unchecked_not_a_clean_bill(pool):
    """The failure the heartbeat exists for. The provider answers HTTP-200 with
    an empty result set, the pack's empty-is-suspect probe refuses to call that
    absence, and the claim is recorded UNCHECKED — so ``claims_checked`` is 0
    and the run is NOT healthy, even though it ended in success."""

    class _EmptyProvider(_FakeSearchProvider):
        async def search(self, query: str, *, limit: int = 5, params=None):
            from legba.data.stack.search.base import (
                SearchResponse, SearchStatus,
            )

            self.queries.append(query)
            return SearchResponse(
                query=query, results=[], provider="test.fake",
                subprovider="fake", status=SearchStatus.EMPTY,
            )

    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        seeded = await _seed_heads(conn)

    provider = _EmptyProvider([])
    llm = _ScriptedLLM([
        json.dumps({"claims": [{"claim": "c1", "query": "q1"}]}),
        json.dumps({"claims": [{"claim": "c2", "query": "q2"}]}),
    ])
    deps = _Deps(pool, {
        sa.LLM_DEPS_EXTRA_KEY: llm,
        sa.WEB_BINDING_DEPS_EXTRA_KEY: _binding(pool, provider),
    })

    await sa.handle(None, _run_options(seeded), deps)

    async with pool.acquire() as conn:
        hb = await conn.fetchrow(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            sa.ALERT_TRIGGER_CLASS, sa.HEARTBEAT_KEY,
        )
        state = json.loads(hb["state"])
        assert state["claims_checked"] == 0
        assert state["verdicts"].get("UNCHECKED") == 2
        assert state["healthy"] is False
        alerts = await conn.fetchval(
            "SELECT count(*) FROM analyst_outputs WHERE kind = 'alert' "
            "AND run_id = $1",
            seeded.run_id,
        )
        assert alerts == 0, "an unverified empty must never page"


@pytest.mark.asyncio
async def test_an_unwired_search_plane_still_writes_a_naming_heartbeat(pool):
    """The 08-12 shape exactly: the run succeeds, audits nothing, and SAYS SO.
    A crash would be invisible to everything but a log; this row is actionable."""
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        seeded = await _seed_heads(conn)

    llm = _ScriptedLLM([])
    deps = _Deps(pool, {sa.LLM_DEPS_EXTRA_KEY: llm})
    await sa.handle(None, _run_options(seeded), deps)

    async with pool.acquire() as conn:
        state = json.loads(await conn.fetchval(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            sa.ALERT_TRIGGER_CLASS, sa.HEARTBEAT_KEY,
        ))
        critiques = await conn.fetchval(
            "SELECT count(*) FROM analyst_outputs WHERE kind = 'critique' "
            "AND run_id = $1",
            seeded.run_id,
        )
    assert state["claims_checked"] == 0
    assert state["degraded"] is True
    assert "web_access binding" in state["degraded_reason"]
    # BOTH planes or neither: with no search binding the run must not spend the
    # core plane extracting claims it could never check, nor write UNCHECKED
    # critique rows that say nothing the degraded_reason does not.
    assert llm.calls == []
    assert state["claims_extracted"] == 0
    assert critiques == 0


@pytest.mark.asyncio
async def test_the_ops_endpoint_reads_the_heartbeat_and_names_contradictions(
    pool,
):
    """GLASS-3 surface. The payload builder is pure, so it is driven off the
    SAME rows the run above wrote — no second source of truth."""
    from legba.data.registry.external_audit_api import build_payload

    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        seeded = await _seed_heads(conn)

    provider = _FakeSearchProvider(_CONTRADICTING_RESULTS)
    llm = _ScriptedLLM([
        json.dumps({"claims": [{"claim": "world claim", "query": "wq"}]}),
        json.dumps({"verdict": "NOT_FOUND", "rationale": "unsettled"}),
        json.dumps({"claims": [{"claim": "desk claim", "query": "dq"}]}),
        json.dumps({
            "verdict": "CONTRADICTED", "rationale": "never signed",
            "evidence": [{"url": "https://news.example/a", "quote": "never"}],
        }),
    ])
    deps = _Deps(pool, {
        sa.LLM_DEPS_EXTRA_KEY: llm,
        sa.WEB_BINDING_DEPS_EXTRA_KEY: _binding(pool, provider),
    })
    await sa.handle(None, _run_options(seeded), deps)

    async with pool.acquire() as conn:
        hb = await conn.fetchrow(
            "SELECT state, fired_at, first_seen, updated_at "
            "FROM alert_trigger_watermarks WHERE trigger_class = $1",
            sa.ALERT_TRIGGER_CLASS,
        )
        # The SAME projection the route runs, scoped to this run so a sibling
        # test's critiques can never enter the window being asserted on.
        rows = await conn.fetch(
            """
            SELECT ao.id, ao.produced_at, ao.target_id,
                   ao.data->'data'->'external_audit'->>'verdict'    AS verdict,
                   ao.data->'data'->'external_audit'->>'desk_key'   AS desk_key,
                   ao.data->'data'->'external_audit'->>'claim'      AS claim,
                   ao.data->'data'->'external_audit'->>'rationale'  AS rationale,
                   ao.data->'data'->'external_audit'->>'severity'
                       AS audited_severity,
                   ao.data->'data'->'external_audit'->>'analyst_id'
                       AS audited_analyst_id,
                   ao.data->'data'->'external_audit'->'source_urls' AS source_urls,
                   ao.data->'data'->'external_audit'->>'pipeline_version'
                       AS pipeline_version
            FROM analyst_outputs ao
            WHERE ao.kind = 'critique' AND ao.title LIKE 'External audit%'
              AND ao.run_id = $1
            """,
            seeded.run_id,
        )

    out = build_payload(
        dict(hb), [dict(r) for r in rows],
        window_days=14, generated_at=datetime.now(timezone.utc),
    )
    assert out.measured is True
    assert out.heartbeat.present is True
    assert out.heartbeat.stale is False
    assert out.heartbeat.claims_checked == 2
    assert out.n == 2 and out.checked == 2
    assert out.by_verdict["CONTRADICTED"] == 1
    assert out.contradiction_rate == 0.5
    assert out.pipeline_versions == [sa.EXTERNAL_AUDIT_PIPELINE_VERSION]
    # The rare verdict is NAMED, with its source, not merely counted.
    assert len(out.contradictions) == 1
    assert out.contradictions[0].desk_key == seeded.desk_key
    assert out.contradictions[0].source_urls == ["https://news.example/a"]


def test_ops_endpoint_reports_an_absent_heartbeat_as_absent_not_healthy():
    """'The auditor never ran' must not render as an all-clear."""
    from legba.data.registry.external_audit_api import build_payload

    out = build_payload(
        None, [], window_days=14, generated_at=datetime.now(timezone.utc),
    )
    assert out.heartbeat.present is False
    assert out.heartbeat.healthy is False
    assert out.n == 0
    # A rate over zero rows is None, never 0.0 — the standing house rule.
    assert out.contradiction_rate is None
