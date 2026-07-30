# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-3d — the binding, the control probe, and the deferral contract.

Three claims, each with its own section:

1. **The seam is BOUND.** ``ToolContext.search`` / ``search_route`` were
   declared by R-3b and bound by nothing, which is what kept the discovery leg
   inert: ``web_search`` could not reach a provider at all. The runtime now
   resolves the configured ``search_provider`` component into the ``web_access``
   pack's ToolContext. A DECLARED route that will not resolve stays a LOUD
   failure — never a silent empty result set.

2. **Absence is MEASURED, not assumed.** Over a multi-engine meta-search a
   genuinely empty result set for a real query is close to impossible — even a
   nonsense query returns unrelated noise — so an empty means BROKEN far more
   often than it means "nothing exists". Reading ``unresponsive_engines`` only
   catches degradation the provider ADMITS; it cannot catch an empty engine
   set, an encoding bug, or an upstream answering 200 + ``[]`` with no error
   field. So a clean-looking empty triggers ONE bounded control probe, and the
   four combinations (empty+live / empty+dead / results+degraded /
   results+clean) each get their own honest verdict.

   The load-bearing invariant, asserted directly: **no path emits
   ``supports_absence_claim=True`` without a verified-live engine set.**

3. **Failure defers, it never retries.** Retrying immediately against engines
   that are already refusing worsens the ban and double-counts against them, so
   a failed search carries backing-off DEFERRAL ADVICE instead — consumed by
   the caller's own next cadence tick, with no new queue table.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from legba.data.analysts.agency.tools import ToolCall, ToolContext
from legba.data.analysts.agency.web_tools import web_search_tool
from legba.data.schemas.action_pack import ActionPack
from legba.data.stack.search import (
    CONTROL_PROBE_QUERY,
    CONTROL_PROBE_TTL_SECONDS,
    DEFAULT_LIVENESS_CACHE,
    HardSearchFailure,
    LivenessVerdict,
    SearchLivenessCache,
    SearchResponse,
    SearchStatus,
    TransientSearchFailure,
    apply_liveness,
    compute_deferral,
    deferral_from_tool_output,
    parse_searxng_payload,
    verify_engine_liveness,
)
from legba.data.stack.search.liveness import (
    DEFER_BASE_SECONDS,
    DEFER_ESCALATE_AFTER,
    DEFER_MAX_SECONDS,
    control_probe_ttl_seconds,
)

# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------

_EMPTY_CLEAN = {"query": "q", "results": [], "unresponsive_engines": []}
_EMPTY_DEGRADED = {
    "query": "q", "results": [],
    "unresponsive_engines": [["brave", "CAPTCHA"], ["duckduckgo", "CAPTCHA"]],
}
_RESULTS_CLEAN = {
    "query": "q",
    "results": [{"url": "https://example.org/a", "title": "A", "content": "c"}],
    "unresponsive_engines": [],
}
_RESULTS_DEGRADED = {
    "query": "q",
    "results": [{"url": "https://example.org/a", "title": "A", "content": "c"}],
    "unresponsive_engines": [["brave", "too many requests"]],
}
_PROBE_LIVE = {
    "query": CONTROL_PROBE_QUERY,
    "results": [{"url": "https://un.org/", "title": "United Nations",
                 "content": "…"}],
    "unresponsive_engines": [],
}


class _Provider:
    """A bound provider whose control-probe answer is set independently."""

    component_id = "search.searxng.local"
    subprovider = "searxng"

    def __init__(self, payload=_EMPTY_CLEAN, probe_payload=_PROBE_LIVE,
                 probe_raises: Exception | None = None):
        self._payload = payload
        self._probe_payload = probe_payload
        self._probe_raises = probe_raises
        self.calls: list[str] = []

    @property
    def probe_calls(self) -> list[str]:
        return [q for q in self.calls if q == CONTROL_PROBE_QUERY]

    async def search(self, query, *, limit=5, **opts) -> SearchResponse:
        self.calls.append(query)
        if query == CONTROL_PROBE_QUERY:
            if self._probe_raises is not None:
                raise self._probe_raises
            return parse_searxng_payload(self._probe_payload, query=query)
        return parse_searxng_payload(self._payload, query=query)


@pytest.fixture
def cache() -> SearchLivenessCache:
    """A fresh, isolated cache per test — the production one is process-wide."""
    return SearchLivenessCache()


@pytest.fixture(autouse=True)
def _reset_default_cache():
    DEFAULT_LIVENESS_CACHE.reset()
    yield
    DEFAULT_LIVENESS_CACHE.reset()


def _pack(tool_config: dict | None = None) -> ActionPack:
    return ActionPack.model_validate({
        "identity": {
            "id": "web_access", "name": "Web Access Tools",
            "schema_uri": "legba/action_pack/1.0.0", "version": "a" * 16,
            "state": "active", "owner": "s6_agency",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "tools": [{"name": "web_search", "config": tool_config or {}}],
        "governor": {"budget_account": "web_access"},
    }, strict=False)


def _call(query="mali fuel blockade") -> ToolCall:
    return ToolCall(
        pack_id="web_access", tool_name="web_search",
        budget_account="acct", requested_by="analyst.test",
        args={"query": query},
    )


def _ctx(provider=None, cache=None) -> ToolContext:
    return ToolContext(search=provider, search_liveness=cache)


# ---------------------------------------------------------------------------
# 1) THE BINDING — the runtime resolves a component into ToolContext.search
# ---------------------------------------------------------------------------


class _FakeRegistryRow:
    """Just enough of the registry's /stack/{id} body shape."""

    @staticmethod
    def searxng(component_id="search.searxng.local", schema_uri=None) -> dict:
        return {
            "version": "0" * 16,
            "body": {
                "id": component_id,
                "schema_uri": schema_uri or "legba/stack/search_provider/1.0.0",
                "config": {
                    "subprovider": {
                        "factory_kind": "dropdown_static", "raw": "searxng",
                        "options": ["searxng", "json", "firecrawl", "jina",
                                    "tavily", "brave", "agent"],
                    },
                    "endpoint": {"factory_kind": "text",
                                 "raw": "http://searxng:8080/search"},
                    "timeout_seconds": {"factory_kind": "number", "raw": 15,
                                        "minimum": 1, "maximum": 300},
                    "max_results": {"factory_kind": "number", "raw": 10,
                                    "minimum": 1, "maximum": 50},
                },
            },
        }


async def _build(monkeypatch, row):
    """Call the runtime builder with the registry fetch stubbed."""
    from legba.runtime import analyst_deps_builder as adb

    async def _fake_fetch(_client, component_id):
        return row

    monkeypatch.setattr(adb, "_fetch_stack_component", _fake_fetch)

    async def _secrets(_sid: str) -> bytes:  # pragma: no cover — unused here
        return b""

    return await adb.build_search_handler_from_stack_component(
        "search.searxng.local", registry_client=None, secrets_resolve=_secrets,
    )


async def test_builder_resolves_a_registered_component_into_a_live_handler(
    monkeypatch,
):
    """The binding's core: component id -> configured, query-ready handler."""
    handler = await _build(monkeypatch, _FakeRegistryRow.searxng())
    assert handler.subprovider == "searxng"
    assert handler.component_id == "search.searxng.local"
    # Configured, not merely constructed: the endpoint came off the registry row.
    assert handler._cfg.endpoint.raw == "http://searxng:8080/search"


async def test_builder_fails_loudly_when_the_component_is_absent(monkeypatch):
    from legba.runtime.analyst_deps_builder import AnalystDepsBuildError

    with pytest.raises(AnalystDepsBuildError, match="not found in registry"):
        await _build(monkeypatch, None)


async def test_builder_rejects_a_route_pointed_at_the_wrong_family(monkeypatch):
    """``expected_family`` on a StackRef is DOCUMENTATION ONLY (stripped at bind
    time). This is the check that actually runs — a search route pointed at an
    llm component must fail at bind time, not at first query."""
    row = _FakeRegistryRow.searxng(schema_uri="legba/stack/llm_provider/1.0.0")
    with pytest.raises(HardSearchFailure, match="not 'search_provider'"):
        await _build(monkeypatch, row)


async def test_builder_rejects_an_unknown_subprovider(monkeypatch):
    row = _FakeRegistryRow.searxng()
    row["body"]["config"]["subprovider"]["raw"] = "not_a_real_provider"
    with pytest.raises(HardSearchFailure, match="SEARCH_HANDLERS"):
        await _build(monkeypatch, row)


def test_the_runtime_actually_binds_search_into_the_web_access_tool_context():
    """The seam this task exists to close, guarded structurally.

    ``ToolContext.search`` was DECLARED and bound by nothing. A future edit that
    rebuilds the web_access ToolContext without the search fields would silently
    re-inert the whole discovery leg with every unit test still green, so assert
    the binding site directly.
    """
    from pathlib import Path

    import legba.runtime.dapr_host as dapr_host

    text = Path(dapr_host.__file__).with_suffix(".py").read_text()
    assert "_search_handler_factory" in text
    assert "build_search_handler_from_stack_component" in text
    # The web_access ToolContext carries both the handler and its route.
    assert "search=_search_handler" in text
    assert "search_route=_search_route" in text


def test_a_meta_analyst_self_allows_its_web_pack():
    """corpus_researcher is a META analyst (no subscription.targets), so there
    is no target row to hold the ALLOW leg. The write/web binding re-point must
    self-allow its own pack under GLOBAL_SCOPE exactly as the READ binding does
    — without it a granted pack resolves `not_allowed` and the analyst runs
    silently toolless."""
    from pathlib import Path

    import legba.runtime.actor_output_emit as aoe

    text = Path(aoe.__file__).with_suffix(".py").read_text()
    body = text.split("async def _gather_write_bindings_for_target", 1)[1]
    assert "_meta_self_allow" in body
    assert "ActionPackRef(pack_id=base_binding.pack.identity.id)" in body


def test_the_shipped_pack_is_still_inert_until_an_operator_points_it():
    """Granting the pack does not activate search. The ToolSpec's rung-0 opt-in
    gate (no `provider` key) is still closed in the shipped descriptor."""
    from pathlib import Path

    import yaml

    from legba.data.stack.search import resolve_tool_search_route

    root = Path(__file__).resolve().parents[2]
    body = yaml.safe_load(
        (root / "descriptors" / "action_pack_web_access.yaml").read_text()
    )
    cfg = next(t["config"] for t in body["tools"] if t["name"] == "web_search")
    assert resolve_tool_search_route(cfg) is None


# ---------------------------------------------------------------------------
# 2) THE CONTROL PROBE — all four combinations
# ---------------------------------------------------------------------------


async def test_empty_plus_live_engines_is_a_scoped_absence(cache):
    """Combination 1: zero results, control probe returns results ⇒ the engine
    set is demonstrably answering ⇒ the empty is real FOR THIS QUERY."""
    provider = _Provider(payload=_EMPTY_CLEAN)
    result = await web_search_tool(_call(), _pack(), _ctx(provider, cache))
    assert result.status == "completed"
    assert result.output["status"] == "empty_verified"
    assert result.output["liveness"] == "live"
    assert result.output["supports_absence_claim"] is True
    assert "SCOPED absence" in result.output["absence_statement"]
    assert "does NOT establish" in result.output["absence_statement"]
    assert provider.probe_calls  # absence was MEASURED
    assert "deferral" not in result.output


async def test_empty_plus_dead_engines_is_the_degraded_empty_failure(cache):
    """Combination 2: zero results AND the control probe also empty ⇒ the plane
    is broken. This is the shape byte-identical to true absence at the wire."""
    provider = _Provider(payload=_EMPTY_CLEAN, probe_payload=_EMPTY_CLEAN)
    result = await web_search_tool(_call(), _pack(), _ctx(provider, cache))
    assert result.status == "failed"
    assert "search_liveness_unverified" in (result.error or "")
    assert "not absence" in (result.error or "")
    assert result.output["status"] == "degraded_empty"
    assert result.output["liveness"] == "dead"
    assert result.output["supports_absence_claim"] is False
    assert result.output["absence_statement"] == ""


async def test_results_plus_degraded_is_a_usable_completion_that_cannot_claim_absence(
    cache,
):
    """Combination 3: hits arrived but engines refused. Usable — and the flag
    still forbids absence, because the missing engines could have carried the
    contradicting evidence. No retry into a fallback (that hides the ban)."""
    provider = _Provider(payload=_RESULTS_DEGRADED)
    result = await web_search_tool(_call(), _pack(), _ctx(provider, cache))
    assert result.status == "completed"
    assert result.output["count"] == 1
    assert result.output["degraded"] is True
    assert result.output["supports_absence_claim"] is False
    # No probe: a response WITH results tells us the engines are answering.
    assert provider.probe_calls == []
    assert len(provider.calls) == 1


async def test_results_plus_clean_is_a_plain_completion(cache):
    """Combination 4: the ordinary path. No probe, no deferral, no warning."""
    provider = _Provider(payload=_RESULTS_CLEAN)
    result = await web_search_tool(_call(), _pack(), _ctx(provider, cache))
    assert result.status == "completed"
    assert result.output["count"] == 1
    assert result.output["degraded"] is False
    assert result.output["supports_absence_claim"] is False
    assert result.output["absence_warning"] == ""
    assert provider.probe_calls == []


async def test_an_admitted_degraded_empty_is_not_probed(cache):
    """A response that already admits degradation needs no probe — it is
    already not-absence, and the probe costs real upstream goodwill."""
    provider = _Provider(payload=_EMPTY_DEGRADED)
    result = await web_search_tool(_call(), _pack(), _ctx(provider, cache))
    assert result.status == "failed"
    assert "search_degraded_no_results" in (result.error or "")
    assert provider.probe_calls == []
    assert cache.probes == 0


async def test_a_probe_that_cannot_run_is_treated_exactly_like_a_dead_one(cache):
    """An unverifiable plane licenses precisely as much as a broken one."""
    provider = _Provider(
        payload=_EMPTY_CLEAN,
        probe_raises=TransientSearchFailure("HTTP 429"),
    )
    result = await web_search_tool(_call(), _pack(), _ctx(provider, cache))
    assert result.status == "failed"
    assert "search_liveness_unverified" in (result.error or "")
    assert result.output["liveness"] == "probe_failed"
    assert result.output["supports_absence_claim"] is False


async def test_verify_engine_liveness_never_raises(cache):
    """Every failure class collapses into a verdict — the probe cannot become
    a new way for the search plane to crash a run."""
    for exc in (TransientSearchFailure("429"), HardSearchFailure("egress_blocked"),
                RuntimeError("something exotic")):
        verdict, detail = await verify_engine_liveness(
            _Provider(probe_raises=exc), provider_key=f"p::{exc}", cache=cache,
        )
        assert verdict is LivenessVerdict.PROBE_FAILED
        assert detail


# ---- the invariant --------------------------------------------------------


def test_no_response_can_claim_absence_without_a_live_verdict():
    """The load-bearing invariant, over the WHOLE cross-product of shapes."""
    payloads = [_EMPTY_CLEAN, _EMPTY_DEGRADED, _RESULTS_CLEAN, _RESULTS_DEGRADED]
    for payload in payloads:
        for verdict in LivenessVerdict:
            resp = parse_searxng_payload(payload, query="q")
            apply_liveness(resp, verdict, f"{verdict.value} detail")
            claimable = resp.supports_absence_claim
            if claimable:
                assert verdict is LivenessVerdict.LIVE, (payload, verdict)
                assert resp.count == 0
                assert resp.degraded is False
                assert resp.status is SearchStatus.EMPTY_VERIFIED
                assert resp.to_tool_output()["absence_statement"]
            else:
                assert resp.to_tool_output()["absence_statement"] == ""


def test_an_unverified_empty_never_claims_absence():
    """The DEFAULT — no probe ran at all — must be not-claimable, since that is
    the state every raw provider response starts in."""
    resp = parse_searxng_payload(_EMPTY_CLEAN, query="q")
    assert resp.liveness is LivenessVerdict.UNVERIFIED
    assert resp.status is SearchStatus.EMPTY
    assert resp.supports_absence_claim is False
    assert "BROKEN" in resp.to_tool_output()["absence_warning"]


async def test_the_tool_never_returns_an_unverified_empty_as_a_completion(
    cache, monkeypatch,
):
    """Belt-and-braces on the DEFENSIVE branch: if a future edit ever left the
    verdict UNVERIFIED (a probe skipped, a new provider path), a `completed`
    result with count 0 — the exact shape a planner summarizes as "no results
    found" — must still not escape. Forced by stubbing the probe to abstain."""
    import legba.data.analysts.agency.web_tools as wt

    async def _abstain(handler, *, provider_key, cache=None, force=False):
        return LivenessVerdict.UNVERIFIED, "probe skipped"

    monkeypatch.setattr(wt, "verify_engine_liveness", _abstain)

    result = await web_search_tool(
        _call(), _pack(), _ctx(_Provider(payload=_EMPTY_CLEAN), cache),
    )
    assert result.status == "failed"
    assert "search_liveness_unverified" in (result.error or "")
    assert result.output["status"] == "empty"
    assert result.output["supports_absence_claim"] is False
    assert deferral_from_tool_output(result.output) is not None


# ---- caching --------------------------------------------------------------


async def test_n_empties_cost_exactly_one_probe(cache):
    """The liveness question is per-MOMENT, not per-query: one probe answers it
    for the whole window. Upstream-engine goodwill is the scarce resource."""
    provider = _Provider(payload=_EMPTY_CLEAN)
    for i in range(5):
        result = await web_search_tool(
            _call(f"query {i}"), _pack(), _ctx(provider, cache),
        )
        assert result.output["status"] == "empty_verified"
    assert cache.probes == 1
    assert len(provider.probe_calls) == 1
    # 5 real queries + 1 probe.
    assert len(provider.calls) == 6


async def test_a_cached_verdict_expires_with_the_ttl():
    now = {"t": 1_000.0}
    cache = SearchLivenessCache(ttl_seconds=60.0, clock=lambda: now["t"])
    provider = _Provider(payload=_EMPTY_CLEAN)

    await web_search_tool(_call(), _pack(), _ctx(provider, cache))
    assert cache.probes == 1
    now["t"] += 59.0
    await web_search_tool(_call(), _pack(), _ctx(provider, cache))
    assert cache.probes == 1, "verdict still fresh"
    now["t"] += 2.0
    await web_search_tool(_call(), _pack(), _ctx(provider, cache))
    assert cache.probes == 2, "verdict expired — re-measure"


async def test_concurrent_empties_collapse_onto_one_probe(cache):
    import asyncio

    provider = _Provider(payload=_EMPTY_CLEAN)
    results = await asyncio.gather(*[
        web_search_tool(_call(f"q{i}"), _pack(), _ctx(provider, cache))
        for i in range(4)
    ])
    assert all(r.output["status"] == "empty_verified" for r in results)
    assert cache.probes == 1


def test_the_ttl_override_refuses_to_disable_the_cache(monkeypatch):
    """A zero/garbage TTL would burn one upstream query per empty result — the
    exact ban pressure the cache exists to prevent. Ignored, not honoured."""
    from legba.data.stack.search.liveness import CONTROL_PROBE_TTL_ENV

    monkeypatch.setenv(CONTROL_PROBE_TTL_ENV, "0")
    assert control_probe_ttl_seconds() == CONTROL_PROBE_TTL_SECONDS
    monkeypatch.setenv(CONTROL_PROBE_TTL_ENV, "not-a-number")
    assert control_probe_ttl_seconds() == CONTROL_PROBE_TTL_SECONDS
    monkeypatch.setenv(CONTROL_PROBE_TTL_ENV, "45")
    assert control_probe_ttl_seconds() == 45.0


# ---- the canary is the same organ ----------------------------------------


async def test_a_cadence_canary_uses_the_same_code_path(cache):
    """R-3b declared a separate low-cadence control-query canary. It is THIS —
    ``force=True`` refreshes the verdict regardless of the freshness cache, so
    there is one probe query, one threshold, one implementation (the coherence
    audit's ~40-organs finding)."""
    provider = _Provider(payload=_EMPTY_CLEAN)
    v1, _ = await verify_engine_liveness(
        provider, provider_key="search.searxng.local", cache=cache,
    )
    v2, _ = await verify_engine_liveness(
        provider, provider_key="search.searxng.local", cache=cache,
    )
    assert (v1, v2) == (LivenessVerdict.LIVE, LivenessVerdict.LIVE)
    assert cache.probes == 1, "second read served from cache"

    forced, _ = await verify_engine_liveness(
        provider, provider_key="search.searxng.local", cache=cache, force=True,
    )
    assert forced is LivenessVerdict.LIVE
    assert cache.probes == 2, "the cadence hook re-measures on demand"


# ---------------------------------------------------------------------------
# 3) DEFERRAL — bounded, backing off, never an immediate retry
# ---------------------------------------------------------------------------


async def test_a_degraded_empty_carries_deferral_advice(cache):
    provider = _Provider(payload=_EMPTY_DEGRADED)
    result = await web_search_tool(_call(), _pack(), _ctx(provider, cache))
    advice = deferral_from_tool_output(result.output)
    assert advice is not None
    assert advice.defer is True
    assert advice.reason == "search_degraded_no_results"
    assert advice.retry_after_seconds == DEFER_BASE_SECONDS
    assert advice.consecutive_failures == 1
    assert advice.escalate is False
    assert advice.not_before > datetime.now(tz=timezone.utc)


async def test_a_transient_failure_defers_but_a_hard_one_does_not(cache):
    """A transient may well succeed later; a misconfiguration will not — and
    telling a caller to wait for an operator-only fix would be dishonest."""
    class _Raiser(_Provider):
        def __init__(self, exc):
            super().__init__()
            self._exc = exc

        async def search(self, query, *, limit=5, **opts):
            self.calls.append(query)
            raise self._exc

    transient = await web_search_tool(
        _call(), _pack(), _ctx(_Raiser(TransientSearchFailure("429")), cache),
    )
    assert transient.status == "failed"
    assert deferral_from_tool_output(transient.output) is not None

    hard = await web_search_tool(
        _call(), _pack(),
        _ctx(_Raiser(HardSearchFailure("search response not JSON")), cache),
    )
    assert hard.status == "failed"
    assert deferral_from_tool_output(hard.output) is None


async def test_the_tool_never_retries_a_failed_search_in_place(cache):
    """The deferral exists precisely so the tool does NOT retry: hammering
    engines that are already refusing worsens the ban and double-counts the
    query against them."""
    provider = _Provider(payload=_EMPTY_DEGRADED)
    await web_search_tool(_call(), _pack(), _ctx(provider, cache))
    assert provider.calls == ["mali fuel blockade"]


def test_the_backoff_ladder_doubles_caps_and_escalates(cache):
    delays = [
        compute_deferral(
            "search_degraded_no_results", provider_key="p", cache=cache,
        ).retry_after_seconds
        for _ in range(9)
    ]
    assert delays[0] == DEFER_BASE_SECONDS
    assert delays[1] == DEFER_BASE_SECONDS * 2
    assert delays[2] == DEFER_BASE_SECONDS * 4
    assert max(delays) == DEFER_MAX_SECONDS, "bounded"
    assert delays == sorted(delays), "monotonic backoff"

    escalating = compute_deferral(
        "search_degraded_no_results", provider_key="p", cache=cache,
    )
    assert escalating.consecutive_failures == 10
    assert escalating.escalate is True


def test_the_ladder_escalates_exactly_at_the_declared_threshold(cache):
    for i in range(1, DEFER_ESCALATE_AFTER + 1):
        advice = compute_deferral("r", provider_key="p", cache=cache)
        assert advice.escalate is (i >= DEFER_ESCALATE_AFTER)


async def test_a_served_search_resets_the_ladder(cache):
    """The streak measures CONSECUTIVE failures — one served search is proof
    the provider is back, so the next failure starts at the base delay again."""
    degraded = _Provider(payload=_EMPTY_DEGRADED)
    await web_search_tool(_call(), _pack(), _ctx(degraded, cache))
    await web_search_tool(_call(), _pack(), _ctx(degraded, cache))
    assert cache.failure_count("search.searxng.local") == 2

    ok = _Provider(payload=_RESULTS_CLEAN)
    result = await web_search_tool(_call(), _pack(), _ctx(ok, cache))
    assert result.status == "completed"
    assert cache.failure_count("search.searxng.local") == 0

    await web_search_tool(_call(), _pack(), _ctx(degraded, cache))
    advice = compute_deferral("r", provider_key="search.searxng.local",
                              cache=cache)
    assert advice.consecutive_failures == 2  # 1 from the tool + this one


async def test_a_liveness_verified_empty_also_resets_the_ladder(cache):
    """An empty that MEASURED live is a served search — the provider answered."""
    degraded = _Provider(payload=_EMPTY_DEGRADED)
    await web_search_tool(_call(), _pack(), _ctx(degraded, cache))
    assert cache.failure_count("search.searxng.local") == 1

    live_empty = _Provider(payload=_EMPTY_CLEAN)
    result = await web_search_tool(_call(), _pack(), _ctx(live_empty, cache))
    assert result.output["status"] == "empty_verified"
    assert cache.failure_count("search.searxng.local") == 0


def test_deferral_reads_back_off_a_tool_output_and_ignores_a_served_one(cache):
    advice = compute_deferral("search_unavailable", provider_key="p",
                              cache=cache, detail="HTTP 429")
    round_tripped = deferral_from_tool_output({"deferral": advice.to_dict()})
    assert round_tripped is not None
    assert round_tripped.reason == "search_unavailable"
    assert round_tripped.detail == "HTTP 429"
    assert round_tripped.retry_after_seconds == advice.retry_after_seconds

    assert deferral_from_tool_output({"count": 3}) is None
    assert deferral_from_tool_output(None) is None
    assert deferral_from_tool_output({"deferral": {"defer": False}}) is None


def test_deferral_guidance_names_the_consumption_rule(cache):
    """The block is read by a model as well as by code, so it carries the rule
    in words: do not retry now, leave the item open, come back later."""
    advice = compute_deferral("search_degraded_no_results", provider_key="p",
                              cache=cache)
    guidance = advice.to_dict()["guidance"]
    assert "Do NOT retry" in guidance
    assert "OPEN" in guidance
    assert "cadence" in guidance
