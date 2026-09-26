# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The PAID SERP rung: reached only where rung 0 answered and could not decide.

SearXNG cannot verify its own emptiness at volume, so an absence-shaped claim
behind it is ungradable — 16-41% of every read. serper is a first-party index
with no partial-service channel, so its empty means something. That is the
whole purchase, and this file pins exactly where the money may be spent:

  * rung 0 DECIDED the claim (SUPPORTED/CONTRADICTED) -> the paid rung is
    NEVER asked;
  * rung 0 answered an absence-shaped claim it could not verify, or a
    NOT_FOUND with no reformulation to offer -> ONE query on the paid rung;
  * a NOT_FOUND the grader CAN reformulate (2026-09-21/3) -> the
    reformulated query runs on the PAID rung when one is declared — a second
    query against the index that just answered nothing asks it to answer
    again — and on rung 0 only when no paid rung is declared;
  * at most ONE second search per claim, ever, because
    ``EGRESS_CALLS_PER_CLAIM`` (and through it ``GOVERNOR_MAX_CLAIMS_PER_TICK``
    and the day's ``max_serp_per_day``) is derived from that number;
  * no ``serp_provider_order`` second rung -> the whole thing is INERT, which
    is the shipped state;
  * a refusal — no spend cap declared, an undeclared rung, no key — is
    COUNTED and never mistaken for an empty result set;
  * the rung that answered is recorded on the ledger row.

The metered brake itself (``agency/search_cost.py``) is tested here at the
contract level it enforces: a pack with no ``max_cost_usd_per_day`` refuses a
metered rung, and that refusal reaches the audit as a reason, not as results.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from legba.data.analysts.agency import web_tools
from legba.data.analysts.agency.search_cost import (
    SearchCostDecision,
    check_paid_rung_budget,
)
from legba.data.analysts.agency.tools import ToolCall, ToolContext
from legba.data.analysts.deterministic_handlers import _external_audit_claims as claims
from legba.data.analysts.deterministic_handlers import _external_audit_grader as grader
from legba.data.analysts.deterministic_handlers import _external_audit_width as width
from legba.data.analysts.deterministic_handlers._external_audit_queue import (
    DEFAULT_SERP_PROVIDER_ORDER,
    EGRESS_CALLS_PER_CLAIM,
    DEFAULT_GOVERNOR_INVOCATIONS_PER_HOUR,
    governor_max_claims_per_tick,
)
from legba.data.schemas.action_pack import ActionPack

ONE_RUNG = ("searxng",)
TWO_RUNGS = ("searxng", "serper")


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _ToolResult:
    def __init__(self, output=None, status="completed", error=""):
        self.output = output or {}
        self.status = status
        self.error = error


class _Outcome:
    def __init__(self, tool_result, admitted=True, block_cause=None):
        self.tool_result = tool_result
        self.admitted = admitted
        self.block_cause = block_cause


def _hit(url="https://abc.net.au/story"):
    return {"url": url, "title": "t", "snippet": "s"}


class _SearchBinding:
    """Records every ``web_search``, and answers per rung.

    ``by_provider`` maps the PIN (``""`` for rung 0) to the tool output.
    """

    def __init__(self, by_provider):
        self.by_provider = dict(by_provider)
        self.calls: list[dict[str, Any]] = []

    async def run_tool(self, tool_name, args, **kwargs):
        self.calls.append({"tool": tool_name, **dict(args)})
        if tool_name != "web_search":
            # The span-fetch leg. Never the subject of this file.
            return _Outcome(_ToolResult({"url": args.get("url"),
                                         "status_code": 200,
                                         "body": "<html><p>x</p></html>"}))
        pin = str(args.get("provider") or "")
        entry = self.by_provider.get(pin)
        if entry is None:
            return _Outcome(_ToolResult(
                status="failed",
                error=(f"search_provider_not_declared: web_search was pinned "
                       f"to {pin!r}"),
            ))
        return _Outcome(_ToolResult(dict(entry)))

    @property
    def searches(self):
        return [c for c in self.calls if c["tool"] == "web_search"]

    @property
    def pins(self):
        return [str(c.get("provider") or "") for c in self.searches]


class _ScriptedGrader:
    """Returns a scripted verdict per call, so the ladder is what is tested."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.envelopes: list[grader.EvidenceEnvelope] = []


async def _fake_grade_claim(llm, claim, envelope, **kwargs):
    llm.envelopes.append(envelope)
    verdict, retry = llm.verdicts.pop(0) if llm.verdicts else ("NOT_FOUND", "")
    return grader.WidthGrade(
        claim=claim, verdict=verdict,
        rater_role=kwargs.get("rater_role", grader.RATER_PRIMARY),
        retry_query=retry,
        search_provider=envelope.provider,
        grader_family=kwargs.get("grader_family", ""),
        grader_component_id=kwargs.get("grader_component_id", ""),
        sample_fraction=kwargs.get("sample_fraction", 1.0),
    )


def _claim(*, absence=False):
    return claims.WidthClaim(
        claim_text="no strike has been reported on the port",
        population="assembly_span", graded_output_id="o",
        analyst_id="country_composition", origin_head_id="h",
        start=0, end=10, absence_shaped=absence,
    )


async def _grade_one(binding, grader_llm, *, claim, provider_order,
                     monkeypatch):
    monkeypatch.setattr(width, "grade_claim", _fake_grade_claim)

    async def _no_span(*a, **k):
        return None

    monkeypatch.setattr(width, "check_one_decisive", _no_span)
    result = width.WidthRunResult()
    grades = await width.grade_one(
        claim, binding=binding, grader=grader_llm, audit_rater=None,
        grader_family="fam", grader_component_id="g", rater_family="",
        rater_component_id="", search_limit=10,
        provider_order=provider_order, sample_fraction=1.0,
        span_checker=None, pages=width.PageCache(), archive_dir=None,
        result=result,
    )
    return grades, result


# ---------------------------------------------------------------------------
# 1) The shipped state: one rung, and the escalation is inert
# ---------------------------------------------------------------------------


def test_the_shipped_ladder_is_still_one_rung():
    assert DEFAULT_SERP_PROVIDER_ORDER == ONE_RUNG
    assert width.paid_rungs(ONE_RUNG) == ()
    assert width.paid_rungs(TWO_RUNGS) == ("serper",)


@pytest.mark.asyncio
async def test_with_one_rung_nothing_escalates(monkeypatch):
    binding = _SearchBinding({"": {"results": [], "provider": "searxng",
                                   "supports_absence_claim": False}})
    llm = _ScriptedGrader([("NOT_FOUND", "")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(), provider_order=ONE_RUNG,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == [""], "exactly one search, on rung 0"
    assert result.paid_escalations == 0
    assert grades[0].verdict == "NOT_FOUND"


@pytest.mark.asyncio
async def test_an_absence_claim_with_one_rung_is_still_unchecked(monkeypatch):
    binding = _SearchBinding({"": {"results": [], "provider": "searxng",
                                   "supports_absence_claim": False}})
    llm = _ScriptedGrader([])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(absence=True), provider_order=ONE_RUNG,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == [""]
    assert result.paid_escalations == 0
    assert grades[0].verdict == grader.VERDICT_UNCHECKED


# ---------------------------------------------------------------------------
# 2) Rung 0 decided it -> the paid rung is never asked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", ["SUPPORTED", "CONTRADICTED"])
@pytest.mark.asyncio
async def test_a_decided_claim_never_reaches_the_paid_rung(monkeypatch, verdict):
    binding = _SearchBinding({
        "": {"results": [_hit()], "provider": "searxng"},
        "serper": {"results": [_hit()], "provider": "search.serper.paid"},
    })
    llm = _ScriptedGrader([(verdict, "")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(), provider_order=TWO_RUNGS,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == [""], "money spent confirming an answer we had"
    assert result.paid_escalations == 0
    assert grades[0].verdict == verdict


@pytest.mark.asyncio
async def test_a_not_found_with_a_better_query_reformulates_on_rung_zero(
    monkeypatch,
):
    """The reformulation and the escalation share ONE budget slot. With no
    paid rung declared, the free rung gets the better query — the shipped
    behaviour, kept."""
    binding = _SearchBinding({
        "": {"results": [_hit()], "provider": "searxng"},
        "serper": {"results": [_hit()], "provider": "search.serper.paid"},
    })
    llm = _ScriptedGrader([("NOT_FOUND", "a better query"), ("SUPPORTED", "")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(), provider_order=ONE_RUNG,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == ["", ""], "two free searches, no paid one"
    assert result.reformulations == 1
    assert result.paid_escalations == 0


# ---------------------------------------------------------------------------
# 3) Rung 0 could not decide -> ONE paid query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_found_with_no_reformulation_escalates_once(monkeypatch):
    binding = _SearchBinding({
        "": {"results": [], "provider": "searxng"},
        "serper": {"results": [_hit()], "provider": "search.serper.paid"},
    })
    llm = _ScriptedGrader([("NOT_FOUND", ""), ("SUPPORTED", "")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(), provider_order=TWO_RUNGS,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == ["", "serper"]
    assert result.paid_escalations == 1
    assert grades[0].verdict == "SUPPORTED"
    # THE LEDGER RECORDS THE RUNG THAT ANSWERED.
    assert grades[0].search_provider == "search.serper.paid"
    assert grades[0].as_dict()["search_provider"] == "search.serper.paid"


@pytest.mark.asyncio
async def test_a_not_found_with_a_better_query_reformulates_on_the_paid_rung(
    monkeypatch,
):
    """2026-09-21/3 — THE REACHABILITY FIX. With a paid rung declared, the
    reformulated query is pinned to it: asking the index that just answered
    nothing to answer again cannot change the answer. Rung 0 is NOT searched
    a second time, and it is still exactly ONE second search."""
    binding = _SearchBinding({
        "": {"results": [_hit()], "provider": "searxng"},
        "serper": {"results": [_hit()], "provider": "search.serper.paid"},
    })
    llm = _ScriptedGrader([("NOT_FOUND", "a better query"), ("SUPPORTED", "")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(), provider_order=TWO_RUNGS,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == ["", "serper"], "the retry rides the paid rung"
    assert binding.searches[1]["query"] == "a better query"
    assert result.reformulations == 1
    assert result.paid_escalations == 1
    assert result.searches + 1 <= EGRESS_CALLS_PER_CLAIM
    assert grades[0].verdict == "SUPPORTED"
    # THE LEDGER RECORDS THE RUNG THAT ANSWERED.
    assert grades[0].search_provider == "search.serper.paid"
    assert grades[0].as_dict()["search_provider"] == "search.serper.paid"


@pytest.mark.asyncio
async def test_a_refused_paid_reformulation_is_counted_and_not_fallen_back(
    monkeypatch,
):
    """A declared rung that REFUSES (no spend cap, undeclared pin, no key) is
    counted as a refusal, never as an empty web — and the claim keeps rung
    0's verdict rather than silently re-asking the index that just answered
    nothing."""
    binding = _SearchBinding({
        "": {"results": [], "provider": "searxng"},
    })  # no "serper" key -> the pin fails, loudly
    llm = _ScriptedGrader([("NOT_FOUND", "a better query")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(), provider_order=TWO_RUNGS,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == ["", "serper"]
    assert result.reformulations == 1
    assert result.paid_escalations == 0
    assert result.paid_escalations_refused == 1
    assert grades[0].verdict == "NOT_FOUND"
    assert grades[0].search_provider == "searxng"


@pytest.mark.asyncio
async def test_an_unverifiable_absence_claim_escalates_and_can_be_graded(
    monkeypatch,
):
    """The claim the rung was bought for: rung 0's empty proves nothing, the
    paid rung's verified empty does."""
    binding = _SearchBinding({
        "": {"results": [], "provider": "searxng",
             "supports_absence_claim": False},
        "serper": {"results": [], "provider": "search.serper.paid",
                   "status": "empty_verified", "liveness": "live",
                   "supports_absence_claim": True},
    })
    llm = _ScriptedGrader([("SUPPORTED", "")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(absence=True), provider_order=TWO_RUNGS,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == ["", "serper"]
    assert result.paid_escalations == 1
    assert grades[0].verdict == "SUPPORTED"
    assert grades[0].search_provider == "search.serper.paid"


@pytest.mark.asyncio
async def test_a_paid_rung_that_also_cannot_verify_stays_unchecked(monkeypatch):
    """Escalating does not license a verdict — only a VERIFIED empty does."""
    binding = _SearchBinding({
        "": {"results": [], "provider": "searxng",
             "supports_absence_claim": False},
        "serper": {"results": [], "provider": "search.serper.paid",
                   "supports_absence_claim": False},
    })
    llm = _ScriptedGrader([])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(absence=True), provider_order=TWO_RUNGS,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == ["", "serper"]
    assert result.paid_escalations == 1
    assert grades[0].verdict == grader.VERDICT_UNCHECKED


# ---------------------------------------------------------------------------
# 4) At most ONE second search per claim — the budget invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_never_spends_more_than_one_second_search(monkeypatch):
    """An absence claim that escalates must NOT then also reformulate: the
    queue reserved EGRESS_CALLS_PER_CLAIM before the tick started."""
    binding = _SearchBinding({
        "": {"results": [], "provider": "searxng",
             "supports_absence_claim": False},
        "serper": {"results": [_hit()], "provider": "search.serper.paid",
                   "supports_absence_claim": True},
    })
    llm = _ScriptedGrader([("NOT_FOUND", "a better query")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(absence=True), provider_order=TWO_RUNGS,
        monkeypatch=monkeypatch,
    )
    assert len(binding.searches) == 2
    assert result.searches + 1 <= EGRESS_CALLS_PER_CLAIM
    assert result.reformulations == 0


def test_the_egress_reservation_is_unchanged_by_this_build():
    """If either number moves, the day's SERP budget silently overruns."""
    assert EGRESS_CALLS_PER_CLAIM == 3
    # L1 (same wave) made the tick clamp read the LIVE pack governor; the
    # arithmetic this lane must not move is calls-per-claim and the division.
    assert governor_max_claims_per_tick(None) == (
        DEFAULT_GOVERNOR_INVOCATIONS_PER_HOUR // EGRESS_CALLS_PER_CLAIM
    )


# ---------------------------------------------------------------------------
# 5) Refusals are counted, and never read as an empty web
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_undeclared_rung_is_a_counted_refusal_not_an_empty(monkeypatch):
    """The pin failed because the operator never added the component to the
    ToolSpec. Rung 0's verdict stands; nothing is invented."""
    binding = _SearchBinding({
        "": {"results": [], "provider": "searxng"},
    })  # no "serper" key -> the pin fails, loudly
    llm = _ScriptedGrader([("NOT_FOUND", "")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(), provider_order=TWO_RUNGS,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == ["", "serper"]
    assert result.paid_escalations == 0
    assert result.paid_escalations_refused == 1
    assert grades[0].verdict == "NOT_FOUND"
    assert grades[0].search_provider == "searxng"


@pytest.mark.asyncio
async def test_a_pack_with_no_spend_cap_refuses_the_metered_rung():
    """The brake, at the contract level the audit depends on: no declared
    ``max_cost_usd_per_day`` means the paid rung is NOT queried."""
    pack = ActionPack.model_validate({
        "identity": {
            "id": "web_access", "name": "web_access",
            "schema_uri": "legba/action_pack/1.0.0", "version": "a" * 16,
            "state": "active", "owner": "t",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "tools": [{"name": "web_search"}],
    }, strict=False)

    class _Ledger:
        async def spent_today_usd(self, *, pack_id, budget_account):
            return 0.0

    decision = await check_paid_rung_budget(
        pack=pack, ledger=_Ledger(), budget_account="auditor",
        cost_usd=0.001, component_id="search.serper.paid",
    )
    assert isinstance(decision, SearchCostDecision)
    assert decision.admitted is False
    assert decision.cause == "no_cap_declared"


# ---------------------------------------------------------------------------
# 6) The pin itself: a selection among DECLARED rungs, never a free-for-all
# ---------------------------------------------------------------------------


def _pack_with_search():
    return ActionPack.model_validate({
        "identity": {
            "id": "web_access", "name": "web_access",
            "schema_uri": "legba/action_pack/1.0.0", "version": "a" * 16,
            "state": "active", "owner": "t",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "tools": [{"name": "web_search"}],
    }, strict=False)


class _Handler:
    def __init__(self, subprovider, results, cost=0.0):
        self.subprovider = subprovider
        self._results = results
        self.cost_usd_per_query = cost
        self.queries = 0

    async def search(self, query, *, limit=5, **opts):
        from legba.data.stack.search import SearchResponse, SearchResult

        self.queries += 1
        return SearchResponse(
            query=query,
            results=[SearchResult(url=u, title="t", snippet="s", rank=i + 1)
                     for i, u in enumerate(self._results)],
        )


class _Route:
    def __init__(self, component_id):
        self.component_id = component_id
        self.source = "test"
        self.route_class = "stack_ref"


@pytest.mark.asyncio
async def test_a_pin_selects_a_declared_rung_and_skips_rung_zero():
    rung0 = _Handler("searxng", ["https://free/1"])
    paid = _Handler("serper", ["https://paid/1"])
    ctx = ToolContext()
    ctx.search = rung0
    ctx.search_fallbacks = [(paid, _Route("search.serper.paid"))]
    result = await web_tools.web_search_tool(
        ToolCall(pack_id="web_access", tool_name="web_search",
                 args={"query": "q", "provider": "serper"}),
        _pack_with_search(), ctx,
    )
    assert result.status == "completed"
    assert rung0.queries == 0, "the pin must not fall back to rung 0"
    assert paid.queries == 1
    assert result.output["provider"] == "search.serper.paid"
    assert result.output["results"][0]["url"] == "https://paid/1"


@pytest.mark.asyncio
async def test_an_unpinned_call_still_lands_on_rung_zero():
    rung0 = _Handler("searxng", ["https://free/1"])
    paid = _Handler("serper", ["https://paid/1"])
    ctx = ToolContext()
    ctx.search = rung0
    ctx.search_fallbacks = [(paid, _Route("search.serper.paid"))]
    result = await web_tools.web_search_tool(
        ToolCall(pack_id="web_access", tool_name="web_search",
                 args={"query": "q"}),
        _pack_with_search(), ctx,
    )
    assert result.status == "completed"
    assert rung0.queries == 1 and paid.queries == 0


@pytest.mark.asyncio
async def test_a_pin_naming_no_declared_rung_fails_loudly():
    rung0 = _Handler("searxng", ["https://free/1"])
    ctx = ToolContext()
    ctx.search = rung0
    result = await web_tools.web_search_tool(
        ToolCall(pack_id="web_access", tool_name="web_search",
                 args={"query": "q", "provider": "serper"}),
        _pack_with_search(), ctx,
    )
    assert result.status == "failed"
    assert "search_provider_not_declared" in (result.error or "")
    assert "not an empty result set" in (result.error or "")
    assert rung0.queries == 0, "nothing was queried"


@pytest.mark.asyncio
async def test_a_pinned_METERED_rung_still_goes_through_the_spend_brake():
    """Pinning must not bypass the cap. The pin says WHICH rung; the brake
    says whether it may spend, and it is the same brake either way."""
    rung0 = _Handler("searxng", ["https://free/1"])
    paid = _Handler("serper", ["https://paid/1"], cost=0.001)
    ctx = ToolContext()
    ctx.search = rung0
    ctx.search_fallbacks = [(paid, _Route("search.serper.paid"))]
    # The pack declares NO max_cost_usd_per_day -> fail closed.
    result = await web_tools.web_search_tool(
        ToolCall(pack_id="web_access", tool_name="web_search",
                 args={"query": "q", "provider": "serper"}),
        _pack_with_search(), ctx,
    )
    assert result.status == "failed"
    assert "search_cost_no_cap_declared" in (result.error or "")
    assert "we did not look" in (result.error or "")
    assert paid.queries == 0, "NOT queried — this is a budget refusal"
    assert rung0.queries == 0, "and still no silent fall back to rung 0"


# ---------------------------------------------------------------------------
# 6) 2026-09-21/1 — the absence gate applies to an EMPTY, not to hits
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_absence_claim_whose_rung0_returned_hits_is_graded_not_refused(
    monkeypatch,
):
    """THE LIVE DEFECT (2026-09-21, 104 of 104 absence claims in 24 h): rung 0
    answered with HITS, ``supports_absence_claim`` is (correctly) false for a
    non-empty, and the gate refused the claim before the grader saw the hits.
    A hit that evidences the event is a CONTRADICTION of a non-event claim."""
    binding = _SearchBinding({"": {"results": [_hit()], "provider": "searxng",
                                   "supports_absence_claim": False}})
    llm = _ScriptedGrader([("CONTRADICTED", "")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(absence=True), provider_order=TWO_RUNGS,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == [""], "hits on rung 0 are graded — no escalation first"
    assert result.paid_escalations == 0
    assert grades[0].verdict == "CONTRADICTED"
    assert grades[0].unchecked_reason != grader.UNCHECKED_ABSENCE_LIVENESS


@pytest.mark.asyncio
async def test_paid_rung_hits_for_an_absence_claim_reach_the_grader(monkeypatch):
    """The exact shape of the 40 discarded serper answers: rung 0 an unverified
    empty, the paid rung HITS. The hits are graded, never refused."""
    binding = _SearchBinding({
        "": {"results": [], "provider": "searxng",
             "supports_absence_claim": False},
        "serper": {"results": [_hit()], "provider": "search.serper.paid",
                   "supports_absence_claim": False},
    })
    llm = _ScriptedGrader([("NOT_FOUND", "")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(absence=True), provider_order=TWO_RUNGS,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == ["", "serper"]
    assert result.paid_escalations == 1
    assert grades[0].verdict == "NOT_FOUND"
    assert grades[0].search_provider == "search.serper.paid"
    assert grades[0].unchecked_reason != grader.UNCHECKED_ABSENCE_LIVENESS


@pytest.mark.asyncio
async def test_rung0_hits_that_decide_nothing_still_escalate_once(monkeypatch):
    """Hits that mention nothing grade NOT_FOUND; with no reformulation offered
    the existing NOT_FOUND path spends the ONE second search on the paid rung."""
    binding = _SearchBinding({
        "": {"results": [_hit()], "provider": "searxng",
             "supports_absence_claim": False},
        "serper": {"results": [_hit()], "provider": "search.serper.paid",
                   "supports_absence_claim": False},
    })
    llm = _ScriptedGrader([("NOT_FOUND", ""), ("CONTRADICTED", "")])
    grades, result = await _grade_one(
        binding, llm, claim=_claim(absence=True), provider_order=TWO_RUNGS,
        monkeypatch=monkeypatch,
    )
    assert binding.pins == ["", "serper"]
    assert result.paid_escalations == 1
    assert result.searches + 1 <= EGRESS_CALLS_PER_CLAIM
    assert grades[0].verdict == "CONTRADICTED"
    assert grades[0].search_provider == "search.serper.paid"

