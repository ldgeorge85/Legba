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
REAL ``wire_standing_auditor_web_pack`` production wiring, the REAL
``Agency.run_pack_tool`` three-way gate, the REAL ``web_access`` ActionPack
fetched from the registry, the REAL ``SearxngSearchHandler`` resolved from the
shipped ``config.provider`` stack_ref, and the REAL ``web_search`` tool handler.
The ONLY doubles are three SOCKETS: the LLM, the registry GET and the search
provider's own HTTP GET. Nothing in ``standing_auditor`` is monkeypatched, so if
the handler stopped routing through the pack (an ad-hoc httpx call, say) the
provider socket would never be consulted AND no ``action_pack_invocations``
ledger row would land — the assertion that makes this a binding-path test rather
than a shape test.

#85 — WHY THE DOUBLE MOVED. This suite used to bind a ``_FakeSearchProvider``
directly into ``ToolContext(search=...)``, calling that "the same construction
external_audit_binding performs in production". It was not: production COPIED
that field off a bring-up context that never carried a provider, so the auditor's
search leg was dead from 2026-07-28 while these tests stayed green. A double
placed at the OUTPUT of the seam under test proves only what lies downstream of
it. The doubles are now at the sockets, and the resolution in between is real.
The rung-by-rung coverage lives in
``tests/runtime/test_external_audit_binding.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts.agency import Agency, AgencyToolBinding
from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import standing_auditor as sa
from legba.data.analysts.deterministic_handlers import (
    _external_audit_width as width_mod,
)
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
        # the shipped 6-claim sweep's knobs
        "window_hours", "max_desks", "max_claims_per_head",
        "max_claims_total", "search_limit",
        # the WIDTH knobs (W-2 / W-9) — inert with the flag off, but declared,
        # validated and reachable so a descriptor PUT that widens the audit is
        # never silent dead config
        "max_claims_per_tick", "max_claims_per_day", "max_serp_per_day",
        "max_queue_depth", "serp_provider_order",
    }
    # And the handler really reads them: a descriptor-set cap must bind.
    assert sa._pos(resolved.accepted.get("max_desks"), 99) == 3
    # The width caps are the operator-facing knobs, and they are the LIFTED
    # ones (2026-09-20): the old 40/600/900/4000 were sized against a
    # web_access governor of 120/h that the operator removed, and the stale
    # copy of it starved the auditor to ~13 claims/h. The code clamp now reads
    # the LIVE pack (see _external_audit_queue.governor_max_claims_per_tick);
    # the schema bound here is only a fat-finger guard and must not be the
    # thing that blocks a widened descriptor.
    assert resolved.accepted.get("max_claims_per_tick") == 200
    assert resolved.accepted.get("max_claims_per_day") == 5000
    assert resolved.accepted.get("max_serp_per_day") == 10000
    assert resolved.accepted.get("max_queue_depth") == 20000


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


# #85 — THE DOUBLE MOVED DOWN A LAYER, AND THAT IS THE WHOLE POINT.
#
# This file used to define a `_FakeSearchProvider` and bind it straight into
# `ToolContext(search=provider)`, calling that "the same construction
# external_audit_binding performs in production". It was not. Production built
# that ToolContext by COPYING `AGENCY_HOLDER["tool_context"]`, which carries
# only queue+emit — so `search` was always None, every web_search failed
# `search_provider_unresolved`, and the auditor shipped with a dead search leg
# while this suite stayed green for months.
#
# Injecting a resolved provider at the seam under test can only ever prove the
# code DOWNSTREAM of the seam. So the fake now sits at the provider's SOCKET
# (`SearchProviderHandler._get_json`) and the binding is built by the REAL
# `wire_standing_auditor_web_pack`: route resolution, component fetch, family
# assertion and handler configuration all run for real here.
#
# Reused from the dedicated binding suite so there is ONE definition of these
# sockets; the exhaustive rung-by-rung coverage lives there.
from tests.runtime.test_external_audit_binding import (  # noqa: E402
    _FakeRegistryClient,
    _FakeSearchHTTP,
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


@dataclass
class _Deps:
    """A dataclass because the REAL wiring does ``dataclasses.replace(deps, ...)``
    — the production StandardDeps is one, and a plain object silently could not
    traverse the path this suite now exercises."""

    pg_pool: Any
    extras: dict
    secrets_resolve: Any = None


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


# NOTE: the local `_web_access_pack()` loader is gone. The pack now arrives the
# way production gets it — `fetch_action_pack` off the registry, inside the real
# wiring — so the shipped descriptor is still what is under test, but it reaches
# the binding through the real code path instead of a test-side shortcut.


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


async def _seed_a_fresh_queue_watermark(conn) -> None:
    """Pre-seed the width queue's ``refill_watermark`` to "now", so the next
    refill is NOT a cold start. A cold start reaches back 48 hours and would
    sweep in every width-shaped (``country_composition``/``world_assessor``/…)
    read an earlier test in the SAME suite run already seeded — the exact
    pre-existing order-sensitivity a comment on
    ``test_width_flag_on_grades_assembly_spans_through_the_real_binding``
    already names. Only a test whose OWN counts are global (a heartbeat
    field, not a ``WHERE graded_output_id = $1``-scoped query) needs this."""
    from legba.data.analysts.deterministic_handlers import (
        _external_audit_queue as ext_queue,
    )

    state = ext_queue.empty_state(day=ext_queue.utc_day())
    state["refill_watermark"] = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "INSERT INTO alert_trigger_watermarks "
        "(trigger_class, watermark_key, state, updated_at) "
        "VALUES ($1, $2, $3::jsonb, now())",
        sa.ALERT_TRIGGER_CLASS, ext_queue.QUEUE_KEY, json.dumps(state),
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


async def _binding(pool) -> AgencyToolBinding:
    """The auditor's web binding, built by the REAL production wiring (#85).

    Not a reconstruction of it — ``wire_standing_auditor_web_pack`` itself, over
    the shipped ``web_access`` descriptor and a registry serving the shipped
    ``search.searxng.local`` row. The provider that comes back is a genuine
    ``SearxngSearchHandler`` configured from that row; only its socket is faked
    (see the module-level note above ``_FakeRegistryClient``).

    Callers that want to shape what the engine "returns" patch
    ``SearchProviderHandler._get_json`` — the ``search_http`` fixture.
    """
    from legba.data.analysts.agency import ToolContext as _TC
    from legba.runtime.external_audit_binding import (
        wire_standing_auditor_web_pack,
    )
    from legba.runtime.source_first_runtime import AGENCY_HOLDER

    # Bring-up state, exactly as source_first_runtime publishes it: queue+emit
    # and NOTHING else. The binding must resolve its provider rather than find
    # one here — that emptiness is what #85 was.
    AGENCY_HOLDER["agency"] = Agency()
    AGENCY_HOLDER["tool_context"] = _TC(queue=None, emit=None)

    async def _secrets(_sid: str) -> bytes:
        return b""

    deps = await wire_standing_auditor_web_pack(
        _AuditorDescriptor(),
        _Deps(pool, {}, secrets_resolve=_secrets),
        registry_client=_FakeRegistryClient(),
    )
    binding = deps.extras[sa.WEB_BINDING_DEPS_EXTRA_KEY]
    assert binding.tool_context.search is not None, (
        "the real wiring bound no provider — #85 has regressed"
    )

    # THE ONE CAP THIS SUITE CANNOT HONOUR, AND WHY LIFTING IT KEEPS THE PATH
    # REAL. `action_pack_web_access.yaml` declares `api_rate_per_minute: 20`,
    # and the governor counts it off `action_pack_invocations` in a TRAILING
    # WALL-CLOCK MINUTE. This file runs ~15 real `handle()` ticks in ~10
    # seconds of wall clock, so the cap trips partway through every full-file
    # run and blocks whichever test happens to be mid-flight — a DIFFERENT one
    # each run (`cause=over_rate`, `external_audit.span_fetch_blocked`). That
    # is an artifact of running a day's worth of ticks in ten seconds, not a
    # property any test here asserts: nothing in this file exercises the rate
    # cap, and `governor.py` has its own tests for it.
    #
    # Only this ONE dimension is lifted. The agency gate, the pack resolution,
    # the governor object, every other cap and the `action_pack_invocations`
    # ledger row all still run — the real-binding rule (#85: the auditor's
    # search leg was dead for five weeks while its tests passed) is about
    # traversing the seam, and the seam is fully traversed.
    binding.pack.governor.api_rate_per_minute = None
    return binding


@dataclass
class _AuditorDescriptor:
    """Just the two fields the wiring reads: identity + the GRANT leg."""

    identity: Any = field(default_factory=lambda: SimpleNamespace(
        id="standing_auditor",
    ))
    action_packs: list = field(
        default_factory=lambda: [{"pack_id": "web_access"}],
    )


@pytest.fixture
def search_http(monkeypatch) -> _FakeSearchHTTP:
    """Patch ONLY the provider's HTTP GET. Everything above it stays real."""
    from legba.data.stack.search.base import SearchProviderHandler
    from legba.data.stack.search.liveness import DEFAULT_LIVENESS_CACHE
    from legba.runtime import search_handler_factory as shf

    # Every test in this file now resolves the SAME provider key
    # (search.searxng.local), so the process-wide liveness verdicts + deferral
    # streaks would leak between them. Reset both ends.
    shf.clear_search_handler_cache()
    DEFAULT_LIVENESS_CACHE.reset()
    rec = _FakeSearchHTTP()
    monkeypatch.setattr(SearchProviderHandler, "_get_json", rec)
    yield rec
    shf.clear_search_handler_cache()
    DEFAULT_LIVENESS_CACHE.reset()


# A SearXNG wire payload, not a pre-parsed result list: the real
# `parse_searxng_payload` (field map + the `unresponsive_engines` degradation
# read) now runs between this and the analyst.
_CONTRADICTING_PAYLOAD = {
    "results": [
        {"url": "https://news.example/a",
         "title": "Talks collapse",
         "content": "Officials confirmed the agreement was never signed.",
         "engine": "duckduckgo", "score": 1.0},
    ],
    "unresponsive_engines": [],
}


@pytest.mark.asyncio
async def test_end_to_end_writes_critiques_an_alert_and_a_heartbeat(
    pool, search_http,
):
    """The whole organ, through the real gate AND the real search binding (#85).

    Proves, in one run: the binding RESOLVED the shipped ``config.provider``
    stack_ref into a live handler; the search reached that provider THROUGH the
    pack (a settled ``action_pack_invocations`` row + the provider's socket saw
    the query); a critique row landed per verdict under the External-audit title
    prefix; the CONTRADICTED verdict on the ``severity:high`` desk emitted a
    kind='alert' row; and the heartbeat row records what the run actually
    checked.
    """
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        seeded = await _seed_heads(conn)
        # A watermark, not a count: `action_pack_invocations` carries NO run_id
        # column, and it is a genuinely shared table (every analyst that ever
        # calls through a pack writes here, and nothing truncates it between
        # tests in this session-scoped DB). An EARLIER test in this file
        # (`test_width_decisive_verdict_survives_the_real_span_check` and
        # `test_width_robots_disallowed_page_is_never_fetched` both drive a
        # real `web_fetch` through this SAME `requested_by` identity —
        # `f"analyst::{analyst_id}"` carries no per-test discriminator) can
        # leave a `tool_name='web_fetch'` row that an unscoped re-query below
        # would silently fold into THIS test's ledger. Scoping by
        # ``occurred_at`` is what makes this test's assertions about its own
        # ledger rows regardless of shuffle order.
        ledger_since = await conn.fetchval("SELECT clock_timestamp()")

    search_http.payload = _CONTRADICTING_PAYLOAD
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
        sa.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool),
    })

    result = await sa.handle(None, _run_options(seeded), deps)
    assert isinstance(result, AnalystMethodResult)

    # -- the search went through the PACK, and through the RESOLVED provider --
    assert search_http.queries, "the pack tool never reached the bound provider"
    # It queried the endpoint that came off the registered stack component —
    # proof the rung-1 ref resolved rather than falling through to a legacy env
    # endpoint or a hand-injected double.
    assert set(search_http.endpoints) == {"http://searxng:8080/search"}
    async with pool.acquire() as conn:
        ledger = await conn.fetch(
            "SELECT tool_name, outcome, requested_by, budget_account "
            "FROM action_pack_invocations WHERE pack_id = 'web_access' "
            "AND requested_by = 'analyst::standing_auditor' "
            "AND occurred_at >= $1",
            ledger_since,
        )
        assert ledger, "no new invocation ledger row — the gate was bypassed"
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
async def test_a_degraded_search_plane_yields_unchecked_not_a_clean_bill(
    pool, search_http,
):
    """The failure the heartbeat exists for. The engine answers HTTP-200 with an
    empty result set, the pack's empty-is-suspect probe (which re-queries the
    same provider and also comes back empty) refuses to call that an absence,
    and the claim is recorded UNCHECKED — so ``claims_checked`` is 0 and the run
    is NOT healthy, even though it ended in success.

    The empty now arrives as a real SearXNG wire body through the real parser,
    so this also covers the "every engine banned" shape it stands in for.
    """
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        seeded = await _seed_heads(conn)

    search_http.payload = {"results": [], "unresponsive_engines": []}
    llm = _ScriptedLLM([
        json.dumps({"claims": [{"claim": "c1", "query": "q1"}]}),
        json.dumps({"claims": [{"claim": "c2", "query": "q2"}]}),
    ])
    deps = _Deps(pool, {
        sa.LLM_DEPS_EXTRA_KEY: llm,
        sa.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool),
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
    pool, search_http,
):
    """GLASS-3 surface. The payload builder is pure, so it is driven off the
    SAME rows the run above wrote — no second source of truth."""
    from legba.data.registry.external_audit_api import build_payload

    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        seeded = await _seed_heads(conn)

    search_http.payload = _CONTRADICTING_PAYLOAD
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
        sa.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool),
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


# ===========================================================================
# WIDTH (LEGBA_EXTERNAL_GRADING_WIDTH) — the real binding path, both legs
# ===========================================================================
#
# These ride the SAME migrated Postgres and the SAME real `_binding(pool)` the
# shipped-sweep e2e above uses — the #85 memory rule (the auditor's search leg
# was dead for five weeks while its tests passed) applies to the width leg too:
# it must traverse handle() with the search binding the production wiring built,
# not a hand-injected double.


#: The production evidence-window stamp shape. `composition_window.
#: evidence_window_span` writes `{"oldest", "newest"}` and G-3 only runs when
#: BOTH bounds parse; the `{"earliest", "latest"}` spelling the older fixtures
#: use resolves to an UNMEASURED window, which skips the time gate entirely.
#: Kept as the non-default so the existing pins stay byte-identical.
_MEASURED_WINDOW = {
    "oldest": "2026-08-24T00:00:00+00:00",
    "newest": "2026-09-05T00:00:00+00:00",
}


def _assembly_head_row(
    target: str, *, severity: str = "high", span_text: str | None = None,
    evidence_window: dict | None = None,
):
    """One country_composition ASSEMBLY read — a real assembly.v1 payload with a
    single byte-identified span, so `claims_from_assembly` yields exactly one
    world-claim off it with no model in the loop."""
    head_id = uuid4()
    if span_text is None:
        span_text = (
            f"The {target} central bank raised its policy rate in March 2026."
        )
    return uuid4(), head_id, {
        "tags": [f"severity:{severity}", "severity_delta:rose"],
        "data": {
            "assembly": {
                "schema": "assembly.v1", "regime": "assembly",
                "blocks": [{
                    "ordinal": 1, "finding_id": str(head_id),
                    "desk": "country_composition", "target_id": target,
                    "severity": severity,
                    "spans": [{
                        "role": "bluf", "text": span_text,
                        "origin": {"head_id": str(head_id), "start": 0,
                                   "end": len(span_text.encode("utf-8")),
                                   "body_sha256": "x",
                                   "body_len": len(span_text.encode("utf-8"))},
                        "scope_tokens": [],
                    }],
                }],
            },
            "evidence_window": dict(
                evidence_window
                or {"earliest": "2026-08-24", "latest": "2026-09-05"}
            ),
        },
    }


async def _seed_assembly_read(
    conn, target: str, *, severity: str = "high",
    span_text: str | None = None, evidence_window: dict | None = None,
) -> UUID:
    row_id, _head_id, data = _assembly_head_row(
        target, severity=severity, span_text=span_text,
        evidence_window=evidence_window,
    )
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, confidence, data, target_id,
             analyst_id, analyst_version, schema_uri, produced_at)
        VALUES ($1, 'finding', $2, $3, 0.8, $4::jsonb, $5, 'country_composition',
                $6, $7, now())
        """,
        row_id, f"{target} assembled read", f"{target} body",
        json.dumps(data), target, "b" * 16,
        "iglu:legba/finding/jsonschema/1-0-0",
    )
    return row_id


@pytest.mark.asyncio
async def test_width_flag_off_is_the_shipped_sweep(pool, search_http, monkeypatch):
    """The flag-off guarantee, pinned on the heartbeat JSON field-for-field.

    With LEGBA_EXTERNAL_GRADING_WIDTH unset the auditor runs its shipped 6-claim
    sweep: the heartbeat carries EXACTLY the shipped keys (no width block), the
    pipeline stamp is the 2026-08-29 one, and NOT ONE external_grades row is
    written — the width table is a strict no-op fleet-wide when the flag is off.
    """
    monkeypatch.delenv(sa.WIDTH_FLAG_ENV, raising=False)
    assert sa.width_enabled() is False
    assert sa.pipeline_version() == sa.EXTERNAL_AUDIT_PIPELINE_VERSION

    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        seeded = await _seed_heads(conn)
        ledger_before = await conn.fetchval("SELECT count(*) FROM external_grades")

    search_http.payload = _CONTRADICTING_PAYLOAD
    llm = _ScriptedLLM([
        json.dumps({"claims": [{"claim": "c", "query": "q"}]}),
        json.dumps({"verdict": "NOT_FOUND", "rationale": "unsettled"}),
        json.dumps({"claims": [{"claim": "d", "query": "q2"}]}),
        json.dumps({"verdict": "NOT_FOUND", "rationale": "unsettled"}),
    ])
    deps = _Deps(pool, {
        sa.LLM_DEPS_EXTRA_KEY: llm,
        sa.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool),
    })
    await sa.handle(None, _run_options(seeded), deps)

    async with pool.acquire() as conn:
        state = json.loads(await conn.fetchval(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            sa.ALERT_TRIGGER_CLASS, sa.HEARTBEAT_KEY,
        ))
        ledger_after = await conn.fetchval("SELECT count(*) FROM external_grades")
        queue_rows = await conn.fetchval(
            "SELECT count(*) FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = '_queue'",
            sa.ALERT_TRIGGER_CLASS,
        )

    # THE FIELD-FOR-FIELD PIN. The shipped heartbeat has exactly these keys and
    # no others — a width block (`width`, `sample_fraction`, `queue_pending`, ...)
    # appearing here is the regression this test exists to catch.
    assert set(state.keys()) == {
        "sub_handler", "pipeline_version", "ran_at", "heads_sampled",
        "claims_extracted", "claims_checked", "verdicts", "critiques_written",
        "alerts_written", "write_failures", "degraded", "degraded_reason",
        "healthy",
    }
    assert state["pipeline_version"] == sa.EXTERNAL_AUDIT_PIPELINE_VERSION
    # No ledger row, no queue row — flag-off is a strict no-op on the width plane.
    assert ledger_after == ledger_before
    assert queue_rows == 0


@pytest.mark.asyncio
async def test_width_flag_on_grades_assembly_spans_through_the_real_binding(
    pool, search_http, monkeypatch,
):
    """The width leg, end to end: the queue refills from an assembled read, the
    drain runs one claim through the REAL search binding and the grader, W-3's
    span check is ABSENT so the decisive verdict degrades honestly, a ledger row
    and ONE critique-per-read land, and the heartbeat carries the width block
    with sample_fraction 1.0.

    ``critiques`` below is scoped to THIS run's ``run_id`` but NOT to one
    ``graded_output_id`` the way ``ledger``/``cdata`` are — one drain tick
    grades every queued claim under the same run_id. Without
    ``_seed_a_fresh_queue_watermark`` a cold-start refill reaches back 48h
    (``_REFILL_COLD_START_HOURS``) and sweeps in every width-shaped
    (country_composition/world_assessor/…) 'finding' read an EARLIER test in
    this same session seeded (``migrated_pg`` is session-scoped; nothing
    truncates ``analyst_outputs`` between tests) — draining and critiquing
    those too, in the SAME tick, so ``len(critiques)`` silently depends on
    which tests already ran. Pre-seeding the watermark to "now" makes this
    test's refill see only what it itself just seeded, in every run order."""
    monkeypatch.setenv(sa.WIDTH_FLAG_ENV, "1")
    assert sa.width_enabled() is True
    # The stamp is a function of the WINDOW CONFIGURATION (2026-09-07). Nothing
    # here sets either window knob, so this is the heads/0 instrument and its
    # rows carry the heads stamp.
    assert sa.pipeline_version() == sa.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH_HEADS
    assert sa.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH_HEADS == "2026-09-21/3"

    target = f"wtar_{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        await _seed_a_fresh_queue_watermark(conn)
        read_id = await _seed_assembly_read(conn, target)

    search_http.payload = _CONTRADICTING_PAYLOAD
    # The grader says NOT_FOUND, which is unaffected by the missing span check —
    # a clean, decisive-independent verdict to assert on.
    grader_llm = _ScriptedLLM([
        json.dumps({"verdict": "NOT_FOUND", "rationale": "the results are adjacent"}),
    ] * 60)
    deps = _Deps(pool, {
        sa.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool),
        "standing_auditor_grader": grader_llm,
        "standing_auditor_grader_ref": "llm.judge.cerebras_gemma4_31b.openai_compat",
        "standing_auditor_grader_family": "google_gemma",
    })
    run_id = uuid4()
    await sa.handle(None, {"analyst_id": "standing_auditor", "run_id": run_id,
                           "max_claims_per_tick": 40}, deps)

    async with pool.acquire() as conn:
        # THE LEDGER — the per-claim detail lives here now, scoped to my read.
        ledger = await conn.fetch(
            "SELECT verdict, population, grader_family, rater_role, "
            "grader_pipeline_version, sample_fraction FROM external_grades "
            "WHERE graded_output_id = $1",
            read_id,
        )
        # ONE critique per graded READ, under the audit's own title prefix.
        critiques = await conn.fetch(
            "SELECT title, data FROM analyst_outputs WHERE kind = 'critique' "
            "AND run_id = $1",
            run_id,
        )
        hb = json.loads(await conn.fetchval(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            sa.ALERT_TRIGGER_CLASS, sa.HEARTBEAT_KEY,
        ))
        queue_present = await conn.fetchval(
            "SELECT count(*) FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = '_queue'",
            sa.ALERT_TRIGGER_CLASS,
        )

    assert len(ledger) == 1, "exactly the one span this read carries"
    row = ledger[0]
    assert row["verdict"] == "NOT_FOUND"
    assert row["population"] == "assembly_span"
    assert row["grader_family"] == "google_gemma"
    assert row["rater_role"] == "primary"
    assert (
        row["grader_pipeline_version"]
        == sa.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH_HEADS
    )
    assert float(row["sample_fraction"]) == 1.0

    assert len(critiques) == 1
    assert critiques[0]["title"].startswith(sa.CRITIQUE_TITLE_PREFIX)
    cdata = json.loads(critiques[0]["data"])["data"]["external_audit"]
    assert cdata["width"] is True
    assert cdata["graded_output_id"] == str(read_id)

    # The heartbeat carries BOTH the shipped keys and the width block.
    assert hb["width"] is True
    assert hb["sample_fraction"] == 1.0
    assert hb["pipeline_version"] == sa.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH_HEADS
    assert hb["window_basis"] == "heads"
    assert hb["evidence_window"] is None, "the lineage walk did not run"
    assert "grader" not in hb.get("degraded_reason", "")  # the grader was wired
    assert queue_present == 1, "the durable queue row was created"


# ---------------------------------------------------------------------------
# THE REPAIRED SEAM, THROUGH THE REAL BINDING (2026-09-06)
# ---------------------------------------------------------------------------
#
# The test above pins the state the width leg shipped in: "W-3's span check is
# ABSENT so the decisive verdict degrades honestly". It was not absent. It was
# imported and then called with keyword arguments it does not accept
# (`claim_text=/url=/span=` against `(claim, candidate_url, fetched_text, ...)`),
# and — underneath that — nothing anywhere fetched the decisive page. Every call
# raised TypeError into the wrapper's except-branch, which produced exactly the
# UNCHECKED/span_check_unavailable row that "absent" would have produced. 168
# warnings and 211 unchecked decisive proposals later, that is the defect this
# test exists so that nobody can reintroduce.
#
# It traverses the SAME real binding as every other test here (#85's rule), and
# it fakes ONE layer lower than the seam under test: `web_fetch`'s HTTP socket,
# not `binding.run_tool`. The agency gate, the governor, the pack resolution and
# the real `web_fetch_tool` all run. A test that stubbed `run_tool` would prove
# nothing about a call it had replaced — which is the whole lesson of #85 and of
# this defect both.

_TIER2_URL = "https://www.reuters.com/world/rates-2026"

_DECISIVE_PAYLOAD = {
    "results": [
        {"url": _TIER2_URL,
         "title": "Central bank raises policy rate",
         "content": "The central bank raised its policy rate in March 2026.",
         "engine": "duckduckgo", "score": 1.0},
    ],
    "unresponsive_engines": [],
}

#: The page the fetch leg will read. Carries the span VERBATIM (G-2) and an
#: `article:published_time` inside the read's own window (G-3).
_DECISIVE_PAGE_HTML = (
    "<html><head>"
    '<meta property="article:published_time" content="2026-09-01T09:00:00Z"/>'
    "<title>Central bank raises policy rate</title></head><body><article>"
    "<p>NAIROBI, Sept 1 - The central bank raised its policy rate in "
    "March 2026, its sharpest move in two years.</p>"
    "</article></body></html>"
)


@pytest.fixture
def fetch_http(monkeypatch):
    """Serve robots.txt and the article page at the REAL egress socket.

    Patches `guarded_async_client` on `web_tools` (the page) and on `robots`
    (the rules) — the same seam `tests/data_pkg/agency/test_research_slice_and_
    gather.py` uses. Everything above it is production code: the pack governor,
    the hard gate, `web_fetch_tool`'s SSRF-guarded GET and its ToolResult shape.

    `rules` is mutable so one test can flip robots.txt to Disallow and assert
    the page was never requested.
    """
    import httpx

    from legba.data.analysts.agency import robots as robots_mod
    from legba.data.analysts.agency import web_tools

    state = {"rules": "User-agent: *\nAllow: /\n", "page_calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=state["rules"])
        state["page_calls"].append(str(request.url))
        return httpx.Response(
            200, text=_DECISIVE_PAGE_HTML,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    def fake_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(**kwargs)

    monkeypatch.setattr(web_tools, "guarded_async_client", fake_client)
    monkeypatch.setattr(robots_mod, "guarded_async_client", fake_client)
    # The robots cache is per-PageCache, so nothing leaks between tests; the
    # module-level default is never used by the drain.
    return state


def _decisive_grader_llm(span: str) -> "_ScriptedLLM":
    return _ScriptedLLM([
        json.dumps({
            "verdict": "SUPPORTED",
            "rationale": "the wire report states it directly",
            "evidence": [{"url": _TIER2_URL, "quote": span}],
        }),
    ] * 60)


@pytest.mark.asyncio
async def test_width_decisive_verdict_survives_the_real_span_check(
    pool, search_http, fetch_http, monkeypatch, tmp_path,
):
    """THE REPAIR, END TO END: a decisive verdict that actually got checked.

    One width sweep through `handle()` with the real binding. The grader
    proposes SUPPORTED with a span; the drain fetches the decisive URL through
    the real `web_fetch` (robots-gated); G-1 tiers the domain, G-2 matches the
    span verbatim under the shared fold and content-addresses it, G-3 anchors
    the publication date inside the read's own window. The ledger row lands
    DECISIVE under the REPAIRED instrument's stamp.

    Every assertion here was false yesterday: the row was UNCHECKED, the reason
    was `span_check_unavailable`, the sha256 and tier and archive_ref were all
    NULL, and no HTTP request for the page was ever made.
    """
    monkeypatch.setenv(sa.WIDTH_FLAG_ENV, "1")
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path))
    span = "raised its policy rate in March 2026"

    target = f"wdec_{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        await _seed_a_fresh_queue_watermark(conn)
        read_id = await _seed_assembly_read(
            conn, target,
            span_text=f"The central bank {span}.",
            evidence_window=_MEASURED_WINDOW,
        )

    search_http.payload = _DECISIVE_PAYLOAD
    deps = _Deps(pool, {
        sa.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool),
        "standing_auditor_grader": _decisive_grader_llm(span),
        "standing_auditor_grader_ref": "llm.judge.cerebras_gemma4_31b.openai_compat",
        "standing_auditor_grader_family": "google_gemma",
    })
    run_id = uuid4()
    await sa.handle(None, {"analyst_id": "standing_auditor", "run_id": run_id,
                           "max_claims_per_tick": 40}, deps)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT verdict, unchecked_reason, decisive_url, decisive_span, "
            "decisive_span_sha256, decisive_source_tier, decisive_published_at, "
            "archive_ref, grader_pipeline_version "
            "FROM external_grades WHERE graded_output_id = $1 "
            "AND rater_role = 'primary'",
            read_id,
        )
        hb = json.loads(await conn.fetchval(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            sa.ALERT_TRIGGER_CLASS, sa.HEARTBEAT_KEY,
        ))

    assert row is not None, "the one span this read carries must be graded"

    # THE STAMP. A verdict from the repaired instrument must never pool with one
    # that never checked a span.
    assert row["grader_pipeline_version"] == "2026-09-21/3"
    assert (
        row["grader_pipeline_version"]
        == sa.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH_HEADS
    )

    # THE VERDICT SURVIVED, AND IT WAS EARNED.
    assert row["verdict"] == "SUPPORTED"
    assert row["unchecked_reason"] is None
    assert row["decisive_url"] == _TIER2_URL
    assert row["decisive_span"] == span
    assert len(row["decisive_span_sha256"] or "") == 64, "G-2 addressed the span"
    assert row["decisive_source_tier"] == 2, "G-1 tiered the domain"
    assert row["decisive_published_at"] is not None, "G-3 anchored it in time"
    assert row["decisive_published_at"].isoformat().startswith("2026-09-01")
    assert (row["archive_ref"] or "").startswith("cas:grading/sha256/"), (
        "the grading archive has its own scheme — never a signal's cas: ref"
    )

    # THE PAGE WAS ACTUALLY FETCHED — the half of the seam that did not exist.
    assert fetch_http["page_calls"] == [_TIER2_URL]

    # And the bytes reached the grading archive's own subtree, not the
    # evidence archive's.
    digest = row["archive_ref"].rsplit("/", 1)[-1]
    assert (tmp_path / "grading" / digest[:2] / digest).is_file()

    # The heartbeat counts the new leg's spend.
    assert hb["fetches"] == 1
    assert hb["robots_refused"] == 0
    assert hb["fetch_failed"] == 0


#: `_DECISIVE_PAGE_HTML`'s `article:published_time` is 2026-09-01T09:00:00Z.
#: This window opens exactly 48h AFTER that — a same-event wire report the
#: read's own grounding window had not started yet, the live 2026-09-06
#: sweep's dominant demoted-but-resolved shape. `newest` is chosen well past
#: it so only the BEFORE bound is under test.
_GRACE_WINDOW = {
    "oldest": "2026-09-03T09:00:00+00:00",
    "newest": "2026-09-10T00:00:00+00:00",
}


@pytest.mark.asyncio
async def test_width_grace_hours_option_admits_a_before_window_source(
    pool, search_http, fetch_http, monkeypatch, tmp_path,
):
    """The `window_grace_hours` knob, end to end through `handle()`.

    Same decisive page and grader as ``test_width_decisive_verdict_survives_
    the_real_span_check``, but the read's own window opens 48h AFTER the
    page's publish date — G-3 demotes it at grace=0 (today's byte-identical
    rule) and admits it, span resolved and archived, once the run's own
    ``options`` carry ``window_grace_hours=72``.
    """
    monkeypatch.setenv(sa.WIDTH_FLAG_ENV, "1")
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path))
    monkeypatch.delenv(width_mod.WINDOW_GRACE_HOURS_ENV, raising=False)
    span = "raised its policy rate in March 2026"

    async def _deps():
        return _Deps(pool, {
            sa.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool),
            "standing_auditor_grader": _decisive_grader_llm(span),
            "standing_auditor_grader_ref": "llm.judge.cerebras_gemma4_31b.openai_compat",
            "standing_auditor_grader_family": "google_gemma",
        })

    # -- grace=0 (unset): the source is demoted -----------------------------
    target_a = f"wgra_{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        await _seed_a_fresh_queue_watermark(conn)
        read_a = await _seed_assembly_read(
            conn, target_a,
            span_text=f"The central bank {span}.",
            evidence_window=_GRACE_WINDOW,
        )
    search_http.payload = _DECISIVE_PAYLOAD
    await sa.handle(
        None,
        {"analyst_id": "standing_auditor", "run_id": uuid4(), "max_claims_per_tick": 40},
        await _deps(),
    )
    async with pool.acquire() as conn:
        row_a = await conn.fetchrow(
            "SELECT verdict, unchecked_reason, decisive_span_sha256, archive_ref "
            "FROM external_grades WHERE graded_output_id = $1 AND rater_role = 'primary'",
            read_a,
        )
    assert row_a["verdict"] == "NOT_FOUND"
    assert row_a["unchecked_reason"] == "out_of_window"
    # G-2 still resolved and archived the span — "a demotion keeps its
    # evidence" (``_external_audit_grader.apply_span_check``'s own doctrine).
    # This IS the "resolved-but-out-of-window" row
    # ``out_of_window_gap_buckets`` counts.
    assert len(row_a["decisive_span_sha256"] or "") == 64
    assert (row_a["archive_ref"] or "").startswith("cas:grading/sha256/")

    # -- grace=72 via the run's options: the SAME shape now admits ----------
    target_b = f"wgra_{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        await _seed_a_fresh_queue_watermark(conn)
        read_b = await _seed_assembly_read(
            conn, target_b,
            span_text=f"The central bank {span}.",
            evidence_window=_GRACE_WINDOW,
        )
    search_http.payload = _DECISIVE_PAYLOAD
    await sa.handle(
        None,
        {
            "analyst_id": "standing_auditor", "run_id": uuid4(),
            "max_claims_per_tick": 40, "window_grace_hours": 72,
        },
        await _deps(),
    )
    async with pool.acquire() as conn:
        row_b = await conn.fetchrow(
            "SELECT verdict, unchecked_reason, decisive_span_sha256, archive_ref, "
            "decisive_source_tier FROM external_grades "
            "WHERE graded_output_id = $1 AND rater_role = 'primary'",
            read_b,
        )
    assert row_b["verdict"] == "SUPPORTED"
    assert row_b["unchecked_reason"] is None
    assert len(row_b["decisive_span_sha256"] or "") == 64
    assert (row_b["archive_ref"] or "").startswith("cas:grading/sha256/")
    assert row_b["decisive_source_tier"] == 2


@pytest.mark.asyncio
async def test_width_grace_hours_option_wins_over_the_env_value(
    pool, search_http, fetch_http, monkeypatch, tmp_path,
):
    """The house `_coerce` precedence, proven through the real binding: an
    env value too small to admit the source is overridden by the run's own
    ``window_grace_hours`` option."""
    monkeypatch.setenv(sa.WIDTH_FLAG_ENV, "1")
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path))
    # 10h of env grace is not enough to cross the 48h gap on its own.
    monkeypatch.setenv(width_mod.WINDOW_GRACE_HOURS_ENV, "10")
    span = "raised its policy rate in March 2026"

    target = f"wgre_{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        await _seed_a_fresh_queue_watermark(conn)
        read_id = await _seed_assembly_read(
            conn, target,
            span_text=f"The central bank {span}.",
            evidence_window=_GRACE_WINDOW,
        )

    search_http.payload = _DECISIVE_PAYLOAD
    deps = _Deps(pool, {
        sa.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool),
        "standing_auditor_grader": _decisive_grader_llm(span),
        "standing_auditor_grader_ref": "llm.judge.cerebras_gemma4_31b.openai_compat",
        "standing_auditor_grader_family": "google_gemma",
    })
    await sa.handle(
        None,
        {
            "analyst_id": "standing_auditor", "run_id": uuid4(),
            "max_claims_per_tick": 40, "window_grace_hours": 72,
        },
        deps,
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT verdict, decisive_span_sha256 FROM external_grades "
            "WHERE graded_output_id = $1 AND rater_role = 'primary'",
            read_id,
        )
    # The env's 10h alone would have left this demoted; the option's 72h wins.
    assert row["verdict"] == "SUPPORTED"
    assert len(row["decisive_span_sha256"] or "") == 64


@pytest.mark.asyncio
async def test_width_robots_disallowed_page_is_never_fetched(
    pool, search_http, fetch_http, monkeypatch,
):
    """F-7, through the real binding: robots.txt says no, so the page is not
    requested and the verdict degrades to UNCHECKED with the new reason.

    The load-bearing assertion is `page_calls == []`. A verdict-only assertion
    would pass against an implementation that fetched the page and discarded it,
    which is not what honouring robots.txt means.
    """
    monkeypatch.setenv(sa.WIDTH_FLAG_ENV, "1")
    fetch_http["rules"] = "User-agent: *\nDisallow: /\n"
    span = "raised its policy rate in March 2026"

    target = f"wrob_{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        await _seed_a_fresh_queue_watermark(conn)
        read_id = await _seed_assembly_read(
            conn, target,
            span_text=f"The central bank {span}.",
            evidence_window=_MEASURED_WINDOW,
        )

    search_http.payload = _DECISIVE_PAYLOAD
    deps = _Deps(pool, {
        sa.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool),
        "standing_auditor_grader": _decisive_grader_llm(span),
        "standing_auditor_grader_ref": "llm.judge.cerebras_gemma4_31b.openai_compat",
        "standing_auditor_grader_family": "google_gemma",
    })
    await sa.handle(None, {"analyst_id": "standing_auditor", "run_id": uuid4(),
                           "max_claims_per_tick": 40}, deps)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT verdict, unchecked_reason, decisive_url, "
            "decisive_span_sha256 FROM external_grades "
            "WHERE graded_output_id = $1 AND rater_role = 'primary'",
            read_id,
        )
        hb = json.loads(await conn.fetchval(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            sa.ALERT_TRIGGER_CLASS, sa.HEARTBEAT_KEY,
        ))

    assert fetch_http["page_calls"] == [], (
        "a robots-disallowed page must never be requested"
    )
    assert row["verdict"] == "UNCHECKED"
    assert row["unchecked_reason"] == "robots_disallowed"
    assert row["decisive_span_sha256"] is None
    # The URL SURVIVES on the row: the operator has to be able to see which
    # publisher's rules cost this verdict.
    assert row["decisive_url"] == _TIER2_URL
    assert hb["robots_refused"] == 1
    assert hb["fetch_failed"] == 0


# ---------------------------------------------------------------------------
# THE REQUEUE — the 211 rows the broken seam left behind
# ---------------------------------------------------------------------------


def _requeue_script():
    """Load the one-off script as a module (it is not a package member)."""
    import importlib.util

    path = (
        Path(__file__).resolve().parents[2]
        / "scripts" / "width_requeue_span_check_unavailable.py"
    )
    spec = importlib.util.spec_from_file_location("_width_requeue", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_the_requeue_script_finds_and_re_enqueues_an_unchecked_decisive(
    pool, monkeypatch,
):
    """The recovery path for the 211 rows, proven on a real ledger + queue.

    Scoped by the script's own ``--stamp`` filter to a stamp this test mints, so
    it cannot see (or move) any other test's rows in the session-scoped
    database — the same discipline the suite's other width tests keep with
    ``WHERE graded_output_id = $1``.

    Asserts the two things the script exists to guarantee: a DRY RUN changes
    nothing, and an APPLY re-derives the claim off its READ (through
    ``claims_from_read``, so the requeued claim is byte-identical to a freshly
    refilled one) and puts it back on the durable queue.
    """
    from legba.data.analysts.deterministic_handlers import (
        _external_audit_claims as ext_claims,
    )
    from legba.data.analysts.deterministic_handlers import (
        _external_audit_grader as ext_grader,
    )
    from legba.data.analysts.deterministic_handlers import (
        _external_audit_queue as ext_queue,
    )
    from legba.data.provenance import external_grades as eg

    rq = _requeue_script()
    stamp = f"test-requeue-{uuid4().hex[:8]}/1"
    span = "raised its policy rate in March 2026"
    target = f"wrq_{uuid4().hex[:8]}"

    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        await _seed_a_fresh_queue_watermark(conn)
        read_id = await _seed_assembly_read(
            conn, target, span_text=f"The central bank {span}.",
        )
        row = dict(await conn.fetchrow(
            "SELECT id, analyst_id, target_id, title, body, data, produced_at "
            "FROM analyst_outputs WHERE id = $1",
            read_id,
        ))
        row["data"] = json.loads(row["data"])
        enumerated = list(ext_claims.claims_from_read(row))
        assert len(enumerated) == 1
        claim = enumerated[0]

        # The row the broken instrument wrote: a decisive proposal, thrown away.
        grade = ext_grader.WidthGrade(
            claim=claim,
            verdict="UNCHECKED",
            unchecked_reason="span_check_unavailable",
            decisive_url="https://www.reuters.com/world/rates-2026",
            decisive_span=span,
            grader_family="google_gemma",
            grader_component_id="llm.judge.cerebras_gemma4_31b.openai_compat",
        )
        written, _skipped, _outcomes = await eg.write_grades(
            conn, [grade.as_dict()], pipeline_version=stamp,
            graded_at=datetime.now(timezone.utc),
        )
        assert written == 1

        # -- DRY RUN: it finds the row and writes nothing -------------------
        report = await rq.run(conn, apply=False, stamp=stamp, max_depth=4000)
        assert report["affected_rows"] == 1
        assert report["affected_claim_keys"] == 1
        assert report["reads"] == 1
        assert report["rederived_claims"] == 1
        assert report["unresolved_claim_keys"] == 0
        assert report["refill"]["added"] == 1
        assert report["applied"] is False

        state = await ext_queue.load_queue(
            conn, trigger_class=sa.ALERT_TRIGGER_CLASS,
            day=ext_queue.utc_day(),
        )
        assert claim.key not in {
            e["claim_key"] for e in state["entries"]
        }, "a dry run must not touch the queue"

        # -- APPLY: the claim goes back on the queue ------------------------
        report = await rq.run(conn, apply=True, stamp=stamp, max_depth=4000)
        assert report["applied"] is True
        assert report["refill"]["added"] == 1

        state = await ext_queue.load_queue(
            conn, trigger_class=sa.ALERT_TRIGGER_CLASS,
            day=ext_queue.utc_day(),
        )
        assert claim.key in {e["claim_key"] for e in state["entries"]}

        # -- IDEMPOTENT: a second apply adds nothing ------------------------
        report = await rq.run(conn, apply=True, stamp=stamp, max_depth=4000)
        assert report["refill"]["added"] == 0
        assert report["refill"]["already_queued"] == 1

        # The ledger was never mutated — it is append-only, and a re-grade is a
        # NEW row under the new stamp, which the drain writes, not this script.
        assert await conn.fetchval(
            "SELECT count(*) FROM external_grades WHERE graded_output_id = $1",
            read_id,
        ) == 1


@pytest.mark.asyncio
async def test_the_requeue_script_ignores_unchecked_rows_with_no_decisive_url(
    pool,
):
    """An UNCHECKED row with no decisive URL is a SEARCH-plane statement, not a
    thrown-away decisive verdict. Re-grading it would spend the budget to
    reproduce it, so the script's filter must not reach it."""
    from legba.data.analysts.deterministic_handlers import (
        _external_audit_claims as ext_claims,
    )
    from legba.data.analysts.deterministic_handlers import (
        _external_audit_grader as ext_grader,
    )
    from legba.data.provenance import external_grades as eg

    rq = _requeue_script()
    stamp = f"test-requeue-{uuid4().hex[:8]}/1"
    target = f"wrqn_{uuid4().hex[:8]}"

    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        read_id = await _seed_assembly_read(conn, target)
        row = dict(await conn.fetchrow(
            "SELECT id, analyst_id, target_id, title, body, data, produced_at "
            "FROM analyst_outputs WHERE id = $1",
            read_id,
        ))
        row["data"] = json.loads(row["data"])
        claim = list(ext_claims.claims_from_read(row))[0]
        grade = ext_grader.WidthGrade(
            claim=claim, verdict="UNCHECKED",
            unchecked_reason="span_check_unavailable",
            grader_family="google_gemma",
            grader_component_id="llm.judge.cerebras_gemma4_31b.openai_compat",
        )
        written, _s, _o = await eg.write_grades(
            conn, [grade.as_dict()], pipeline_version=stamp,
            graded_at=datetime.now(timezone.utc),
        )
        assert written == 1
        report = await rq.run(conn, apply=False, stamp=stamp, max_depth=4000)
        assert report["affected_rows"] == 0
        assert report["rederived_claims"] == 0


@pytest.mark.asyncio
async def test_width_run_with_no_grader_writes_a_naming_heartbeat(
    pool, search_http, monkeypatch,
):
    """The fence refused (or nothing wired): the width run completes, writes a
    heartbeat naming the gap, and grades nothing — the same degrade-not-die
    posture the shipped sweep has, on the width plane."""
    monkeypatch.setenv(sa.WIDTH_FLAG_ENV, "1")
    target = f"wtng_{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        read_id = await _seed_assembly_read(conn, target)

    deps = _Deps(pool, {sa.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool)})
    run_id = uuid4()
    await sa.handle(None, {"analyst_id": "standing_auditor", "run_id": run_id}, deps)

    async with pool.acquire() as conn:
        ledger = await conn.fetchval(
            "SELECT count(*) FROM external_grades WHERE graded_output_id = $1",
            read_id,
        )
        hb = json.loads(await conn.fetchval(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            sa.ALERT_TRIGGER_CLASS, sa.HEARTBEAT_KEY,
        ))
    assert ledger == 0
    assert hb["degraded"] is True
    assert "grader" in hb["degraded_reason"]
    assert hb["healthy"] is False


@pytest.mark.asyncio
async def test_a_ledger_write_failure_requeues_the_claim_then_dead_letters_it(
    pool, search_http, monkeypatch,
):
    """Follow-up to the two 2026-09-05 ledger-writer incidents (see
    EXTERNAL_GRADES_PUBLISHED_AT_FIX_REPORT.md's re-queue recommendation): a
    claim whose ``external_grades`` write fails must not vanish from the
    queue and must not be marked graded.

    Forced here with an empty ``grader_family`` — the SAME real CHECK
    constraint (``external_grades_grader_family_nonempty``) the grader-stamp
    incident hit — so the ONE claim this read carries fails its ledger write
    for a genuine, DB-enforced reason on every tick it is drained: tick 1 and
    tick 2 requeue it (``write_attempts`` 1, 2); tick 3 hits
    ``DEFAULT_MAX_WRITE_ATTEMPTS`` (3) and dead-letters it; a FOURTH tick,
    even with the grader FIXED, still grades nothing for this claim — it is
    quarantined, not silently dropped and not retried forever.

    The queue's ``refill_watermark`` is pre-seeded to "now" (see
    ``_seed_a_fresh_queue_watermark`` below) — WITHOUT it a cold-start refill
    reaches back 48 hours and sweeps in every width-shaped read an EARLIER
    test in this same file seeded, which is exactly the pre-existing
    order-sensitivity ``test_width_flag_on_grades_assembly_spans_through_the_
    real_binding`` already carries a note about. This test's own counts are
    per-tick GLOBAL heartbeat fields (``write_failed``/``requeued``), not
    scoped to one read the way a ``WHERE graded_output_id = $1`` query is, so
    it needs the isolation those existing tests do not.
    """
    monkeypatch.setenv(sa.WIDTH_FLAG_ENV, "1")
    target = f"wreq_{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        await _seed_a_fresh_queue_watermark(conn)
        read_id = await _seed_assembly_read(conn, target)

    search_http.payload = _CONTRADICTING_PAYLOAD
    grader_llm = _ScriptedLLM([
        json.dumps({"verdict": "NOT_FOUND", "rationale": "the results are adjacent"}),
    ] * 60)
    binding = await _binding(pool)

    def _deps(*, family: str) -> _Deps:
        return _Deps(pool, {
            sa.WEB_BINDING_DEPS_EXTRA_KEY: binding,
            "standing_auditor_grader": grader_llm,
            "standing_auditor_grader_ref": "llm.judge.cerebras_gemma4_31b.openai_compat",
            "standing_auditor_grader_family": family,
        })

    async def _tick(*, family: str) -> dict:
        await sa.handle(
            None,
            {"analyst_id": "standing_auditor", "run_id": uuid4(),
             "max_claims_per_tick": 40},
            _deps(family=family),
        )
        async with pool.acquire() as conn:
            hb = json.loads(await conn.fetchval(
                "SELECT state FROM alert_trigger_watermarks "
                "WHERE trigger_class = $1 AND watermark_key = $2",
                sa.ALERT_TRIGGER_CLASS, sa.HEARTBEAT_KEY,
            ))
            queue_state = json.loads(await conn.fetchval(
                "SELECT state FROM alert_trigger_watermarks "
                "WHERE trigger_class = $1 AND watermark_key = '_queue'",
                sa.ALERT_TRIGGER_CLASS,
            ))
            ledger = await conn.fetchval(
                "SELECT count(*) FROM external_grades WHERE graded_output_id = $1",
                read_id,
            )
        return {"hb": hb, "queue": queue_state, "ledger": ledger}

    # Ticks 1 and 2: every write for this claim trips the real CHECK
    # constraint (empty grader_family) and is requeued, never lost.
    for attempt in (1, 2):
        r = await _tick(family="")
        assert r["ledger"] == 0, f"attempt {attempt}"
        assert r["hb"]["write_failed"] == 1, f"attempt {attempt}"
        assert r["hb"]["requeued"] == 1, f"attempt {attempt}"
        assert r["hb"]["write_dead_lettered"] == 0, f"attempt {attempt}"
        assert r["queue"]["dead_letter"] == [], f"attempt {attempt}"
        assert len(r["queue"]["entries"]) == 1, (
            f"attempt {attempt}: the claim must still be pending"
        )
        assert list(r["queue"]["write_attempts"].values()) == [attempt]

    # Tick 3: the third straight failure hits the bound and dead-letters it.
    r3 = await _tick(family="")
    assert r3["ledger"] == 0
    assert r3["hb"]["write_failed"] == 1
    assert r3["hb"]["requeued"] == 0
    assert r3["hb"]["write_dead_lettered"] == 1
    assert r3["queue"]["entries"] == []  # not put back a fourth time
    assert r3["queue"]["write_attempts"] == {}  # the counter is retired
    assert len(r3["queue"]["dead_letter"]) == 1
    assert r3["queue"]["dead_letter"][0]["write_attempts"] == 3
    assert r3["queue"]["dead_letter"][0]["last_error"] == "CheckViolationError"

    # Tick 4: the grader is FIXED (a real, non-empty family) but the claim is
    # quarantined — no read re-enumerates it (refill's watermark is already
    # past this read) and it is not in `entries` — so it grades nothing.
    r4 = await _tick(family="google_gemma")
    assert r4["ledger"] == 0
    assert r4["hb"]["write_failed"] == 0
    assert r4["hb"]["requeued"] == 0
    assert len(r4["queue"]["dead_letter"]) == 1  # unchanged — still quarantined


# ---------------------------------------------------------------------------
# G-3'S WINDOW BASIS — window_basis="evidence", against real Postgres
# ---------------------------------------------------------------------------
#
# The lineage walk (`_external_audit_width._EVIDENCE_WINDOW_SQL`) is a RECURSIVE
# CTE over `analyst_outputs.derived_from` and `signals`. Its correctness is a
# SQL fact, so it is proven here — on the migrated fixture, over a lineage
# shaped exactly like the live one — and not against a stub.

#: Well before `_GRACE_WINDOW["oldest"]` (2026-09-03) AND before the decisive
#: page's own `article:published_time` (2026-09-01T09:00:00Z): the read's
#: evidence reaches back where its heads' arrival spread does not.
_EVIDENCE_FETCHED_AT = datetime(2026, 8, 25, 3, 0, tzinfo=timezone.utc)


async def _seed_signal(conn, *, fetched_at: datetime) -> UUID:
    signal_id = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, fetched_at, payload, content_hash) "
        "VALUES ($1, $2, $3, $4::jsonb, $5)",
        signal_id, f"src_{signal_id.hex[:8]}", fetched_at,
        json.dumps({"published_at": fetched_at.isoformat()}),
        signal_id.hex,
    )
    return signal_id


async def _seed_desk_head(conn, *, derived_from: list[UUID]) -> UUID:
    """One desk head between a composition and its signals — the middle hop."""
    head_id = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, confidence, data, analyst_id,
             analyst_version, schema_uri, derived_from, produced_at)
        VALUES ($1, 'finding', 'desk head', 'body', 0.8, '{}'::jsonb,
                'economic_coercion', $2, $3, $4::uuid[], now())
        """,
        head_id, "b" * 16, "iglu:legba/finding/jsonschema/1-0-0", derived_from,
    )
    return head_id


async def _link_lineage(conn, read_id: UUID, derived_from: list[UUID]) -> None:
    await conn.execute(
        "UPDATE analyst_outputs SET derived_from = $2::uuid[] WHERE id = $1",
        read_id, derived_from,
    )


@pytest.mark.asyncio
async def test_the_evidence_walk_reaches_signals_through_every_live_hop(pool):
    """The recursive CTE itself: two hops, three hops, per-root grouping, and a
    read whose lineage bottoms out in nothing.

    The live shapes it must cover (measured 2026-09-06): a country/escalation
    composition reaches signals in 2 hops, the world read in 3 through a country
    composition, and the Assessment in 4+ because D-6 fenced it to a single
    spine head. One query answers all of them at once, per root.
    """
    async with pool.acquire() as conn:
        old_signal = await _seed_signal(conn, fetched_at=_EVIDENCE_FETCHED_AT)
        new_signal = await _seed_signal(
            conn, fetched_at=_EVIDENCE_FETCHED_AT + timedelta(days=6),
        )
        desk = await _seed_desk_head(conn, derived_from=[old_signal, new_signal])
        composition = await _seed_assembly_read(conn, f"wev_{uuid4().hex[:8]}")
        await _link_lineage(conn, composition, [desk])
        world = await _seed_assembly_read(conn, f"wev_{uuid4().hex[:8]}")
        await _link_lineage(conn, world, [composition])
        barren = await _seed_assembly_read(conn, f"wev_{uuid4().hex[:8]}")

        out = await width_mod.evidence_oldest_by_read(
            conn, [str(composition), str(world), str(barren)],
        )

    # 2 hops (composition -> desk -> signals) and 3 (world -> composition -> …)
    # both land on the OLDEST fetched_at, and both count the same two signals.
    assert out[str(composition)] == (_EVIDENCE_FETCHED_AT, 2)
    assert out[str(world)] == (_EVIDENCE_FETCHED_AT, 2)
    # A read with no lineage is ABSENT, not (None, 0) — and `evidence_window`
    # then keeps its heads window rather than inventing an unmeasured one.
    assert str(barren) not in out


@pytest.mark.asyncio
async def test_the_walk_stops_at_the_depth_cap_instead_of_following_a_cycle(pool):
    """`derived_from` is a DAG nobody promised is acyclic. An audit tick must
    not be the thing that discovers it is not."""
    async with pool.acquire() as conn:
        signal = await _seed_signal(conn, fetched_at=_EVIDENCE_FETCHED_AT)
        a = await _seed_assembly_read(conn, f"wcy_{uuid4().hex[:8]}")
        b = await _seed_assembly_read(conn, f"wcy_{uuid4().hex[:8]}")
        await _link_lineage(conn, a, [b])
        await _link_lineage(conn, b, [a, signal])

        out = await width_mod.evidence_oldest_by_read(conn, [str(a)])
        shallow = await width_mod.evidence_oldest_by_read(
            conn, [str(a)], max_depth=1,
        )

    assert out[str(a)] == (_EVIDENCE_FETCHED_AT, 1)
    assert str(a) not in shallow, "one hop cannot reach a signal two hops down"


@pytest.mark.asyncio
async def test_width_evidence_basis_lands_a_decisive_row_at_the_new_stamp(
    pool, search_http, fetch_http, monkeypatch, tmp_path,
):
    """THE FLIP, end to end through `handle()`.

    Same decisive page, grader and read as the grace tests — the read's own
    heads window opens 48h AFTER the page's publish date, so `heads`/0 demotes
    it `out_of_window` (pinned by
    ``test_width_grace_hours_option_admits_a_before_window_source``). With
    ``window_basis=evidence`` the drain walks the read's OWN lineage down to a
    signal fetched 2026-08-25, the window opens there, the verdict survives —
    and the row carries the 2026-09-07 stamp, because a verdict admitted by a
    re-based window is not a measurement the 09-06 instrument took.
    """
    monkeypatch.setenv(sa.WIDTH_FLAG_ENV, "1")
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path))
    monkeypatch.delenv(width_mod.WINDOW_GRACE_HOURS_ENV, raising=False)
    monkeypatch.delenv(width_mod.WINDOW_BASIS_ENV, raising=False)
    span = "raised its policy rate in March 2026"
    target = f"wevb_{uuid4().hex[:8]}"

    async with pool.acquire() as conn:
        await _reset_audit_watermark(conn)
        await _seed_a_fresh_queue_watermark(conn)
        signal = await _seed_signal(conn, fetched_at=_EVIDENCE_FETCHED_AT)
        desk = await _seed_desk_head(conn, derived_from=[signal])
        read_id = await _seed_assembly_read(
            conn, target,
            span_text=f"The central bank {span}.",
            evidence_window=_GRACE_WINDOW,
        )
        await _link_lineage(conn, read_id, [desk])

    search_http.payload = _DECISIVE_PAYLOAD
    deps = _Deps(pool, {
        sa.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool),
        "standing_auditor_grader": _decisive_grader_llm(span),
        "standing_auditor_grader_ref": "llm.judge.cerebras_gemma4_31b.openai_compat",
        "standing_auditor_grader_family": "google_gemma",
    })
    await sa.handle(
        None,
        {
            "analyst_id": "standing_auditor", "run_id": uuid4(),
            "max_claims_per_tick": 40, "window_basis": "evidence",
        },
        deps,
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT verdict, unchecked_reason, decisive_span_sha256, archive_ref, "
            "decisive_source_tier, grader_pipeline_version, read_evidence_window "
            "FROM external_grades "
            "WHERE graded_output_id = $1 AND rater_role = 'primary'",
            read_id,
        )
        hb = json.loads(await conn.fetchval(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            sa.ALERT_TRIGGER_CLASS, sa.HEARTBEAT_KEY,
        ))

    assert row is not None, "the one span this read carries must be graded"

    # THE VERDICT SURVIVED, on the window the read's own evidence describes.
    assert row["verdict"] == "SUPPORTED"
    assert row["unchecked_reason"] is None
    assert len(row["decisive_span_sha256"] or "") == 64
    assert (row["archive_ref"] or "").startswith("cas:grading/sha256/")
    assert row["decisive_source_tier"] == 2

    # THE STAMP MOVED WITH THE INSTRUMENT. A row admitted by a re-based window
    # must never pool with one that required the source inside the heads span.
    assert row["grader_pipeline_version"] == "2026-09-21/4"
    assert row["grader_pipeline_version"] == sa.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH
    assert (
        sa.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH
        != sa.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH_HEADS
    )

    # AND THE LEDGER ROW SHOWS BOTH WINDOWS — the gap is readable off one row.
    window = json.loads(row["read_evidence_window"])
    assert window["basis"] == "evidence"
    assert window["oldest"] == _EVIDENCE_FETCHED_AT.isoformat()
    assert window["oldest_heads"] == _GRACE_WINDOW["oldest"]
    assert window["evidence_signals"] == 1
    assert window["newest"] == _GRACE_WINDOW["newest"], "the after bound never moves"

    # The receipt says which instrument ran and what the walk resolved.
    assert hb["window_basis"] == "evidence"
    assert hb["window_grace_hours"] == 0.0, "the basis alone did this"
    assert hb["pipeline_version"] == "2026-09-21/4"
    assert hb["evidence_window"]["resolved"] >= 1
    assert hb["evidence_window"]["unresolved"] == 0
    assert hb["evidence_window"]["claims_rebased"] >= 1


@pytest.mark.asyncio
async def test_width_evidence_basis_env_is_the_base_and_the_option_wins(
    pool, monkeypatch,
):
    """The house `_coerce` precedence for the basis, read the way the tick
    reads it — env first, descriptor option over the top, in both directions."""
    monkeypatch.setenv(width_mod.WINDOW_BASIS_ENV, "evidence")
    monkeypatch.delenv(width_mod.WINDOW_GRACE_HOURS_ENV, raising=False)

    from_env = width_mod.resolve_window_config({})
    assert from_env.basis == "evidence"
    assert from_env.pipeline_version == "2026-09-21/4"

    overridden = width_mod.resolve_window_config({"window_basis": "heads"})
    assert overridden.basis == "heads"
    assert overridden.pipeline_version == "2026-09-21/3"

    monkeypatch.setenv(width_mod.WINDOW_BASIS_ENV, "heads")
    assert width_mod.resolve_window_config(
        {"window_basis": "evidence"}
    ).basis == "evidence"
