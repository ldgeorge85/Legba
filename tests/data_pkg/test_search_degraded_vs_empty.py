# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-3b Task 2 — degradation must be LOUD, and distinct from "found nothing".

The failure this file exists to make impossible:

A meta-search whose upstream engines are CAPTCHA'd / rate-limited / banned
still answers **HTTP 200**. The refusing engines are dropped from the merge and
named in ``unresponsive_engines``; ``results[]`` comes back short — or empty. A
live probe of the deployed instance returned 200 with 20 results while
``brave: too many requests``, ``duckduckgo: CAPTCHA`` and
``startpage: CAPTCHA`` sat in that field. Had EVERY engine refused, the same
200 would have carried ``"results": []`` — byte-indistinguishable from "nothing
about this exists on the web".

An intelligence platform that cannot tell those apart manufactures FALSE
ABSENCE EVIDENCE. So:

  * the searxng handler READS ``unresponsive_engines`` on every response;
  * ``SearchStatus.DEGRADED_EMPTY`` is a distinct value from
    ``SearchStatus.EMPTY``, and ``supports_absence_claim`` is False for it;
  * the ``web_search`` tool turns DEGRADED_EMPTY into a clean tool FAILURE, not
    an empty success — because ``completed`` + ``count: 0`` is exactly the
    shape a planner summarizes as "no results found".

R-3d changed the DEFAULT for the remaining case. A clean-looking empty (zero
results, NO engine reported unresponsive) is no longer treated as citable true
absence: over a multi-engine meta-search a genuinely empty result set is close
to impossible, so that shape usually means BROKEN. Absence is now MEASURED —
``supports_absence_claim`` is True only for ``EMPTY_VERIFIED``, which requires a
control probe to have shown the engine set answering. The probe logic itself is
covered in ``test_search_liveness_and_deferral.py``; this file asserts the
parse-layer defaults and the tool-level propagation.

Also covers the LEGACY ``LEGBA_WEB_SEARCH_ENDPOINT`` path end-to-end against a
local HTTP fixture server (allowlisted through the SSRF guard), proving the
re-point to the stack layer did not break it — and that it now inherits the
degradation read it never had.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from legba.data.analysts.agency.tools import ToolCall, ToolContext, ToolResult
from legba.data.analysts.agency.web_tools import web_search_tool
from legba.data.schemas.action_pack import ActionPack
from legba.data.stack.search import (
    CONTROL_PROBE_QUERY,
    DEFAULT_LIVENESS_CACHE,
    SearchResponse,
    SearchStatus,
    parse_generic_payload,
    parse_searxng_payload,
)


@pytest.fixture(autouse=True)
def _fresh_liveness_cache():
    """The control-probe verdict + deferral ladder are process-wide by design
    (one probe budget for every analyst). Reset between tests so a cached
    verdict from one case cannot decide another."""
    DEFAULT_LIVENESS_CACHE.reset()
    yield
    DEFAULT_LIVENESS_CACHE.reset()

# The exact shape the live probe returned: 200, results present, engines
# refusing. SearXNG serialises each entry as [engine, reason].
_LIVE_PARTIAL = {
    "query": "sanctions",
    "results": [{"url": "https://example.org/a", "title": "A", "content": "c"}],
    "unresponsive_engines": [
        ["brave", "too many requests"],
        ["duckduckgo", "CAPTCHA"],
        ["startpage", "CAPTCHA"],
    ],
}

# The shape that would have come back had EVERY engine refused. HTTP 200.
_ALL_ENGINES_REFUSED = {
    "query": "sanctions",
    "results": [],
    "unresponsive_engines": [
        ["brave", "too many requests"],
        ["duckduckgo", "CAPTCHA"],
        ["startpage", "CAPTCHA"],
        ["google", "access denied"],
        ["mojeek", "timeout"],
    ],
}

# A fully-served search that genuinely found nothing.
_TRUE_EMPTY = {"query": "sanctions", "results": [], "unresponsive_engines": []}


# ---------------------------------------------------------------------------
# 1) The handler-level distinction
# ---------------------------------------------------------------------------


def test_partial_service_with_results_is_degraded_not_ok():
    resp = parse_searxng_payload(_LIVE_PARTIAL, query="sanctions")
    assert resp.count == 1
    assert resp.status is SearchStatus.DEGRADED
    assert resp.degraded is True
    # Even WITH hits, absence is not claimable: the missing engines could have
    # carried the contradicting evidence.
    assert resp.supports_absence_claim is False
    assert resp.unresponsive_engines == [
        "brave: too many requests", "duckduckgo: CAPTCHA", "startpage: CAPTCHA",
    ]
    assert "brave" in resp.degraded_detail


def test_all_engines_refused_is_degraded_empty_not_empty():
    """THE case. HTTP 200, empty results[], every engine banned."""
    resp = parse_searxng_payload(_ALL_ENGINES_REFUSED, query="sanctions")
    assert resp.count == 0
    assert resp.status is SearchStatus.DEGRADED_EMPTY
    assert resp.status is not SearchStatus.EMPTY
    assert resp.supports_absence_claim is False
    assert len(resp.unresponsive_engines) == 5


def test_a_clean_empty_is_suspect_until_liveness_is_measured():
    """R-3d: the parse layer can only report WHAT CAME BACK. Zero results with
    no admitted degradation is EMPTY — suspect, NOT claimable — because nothing
    has yet shown the engine set was answering at all."""
    resp = parse_searxng_payload(_TRUE_EMPTY, query="sanctions")
    assert resp.count == 0
    assert resp.status is SearchStatus.EMPTY
    assert resp.degraded is False
    assert resp.supports_absence_claim is False
    assert resp.liveness.value == "unverified"
    assert resp.absence_statement == ""


def test_empty_and_degraded_empty_are_distinguishable_in_the_wire_output():
    """A caller reading only the serialized output can still tell them apart —
    and neither zero-result shape claims absence before liveness is measured."""
    degraded = parse_searxng_payload(
        _ALL_ENGINES_REFUSED, query="q").to_tool_output()
    empty = parse_searxng_payload(_TRUE_EMPTY, query="q").to_tool_output()
    assert degraded["count"] == empty["count"] == 0
    assert degraded["status"] == "degraded_empty"
    assert empty["status"] == "empty"
    assert degraded["supports_absence_claim"] is False
    assert empty["supports_absence_claim"] is False
    assert empty["liveness"] == "unverified"
    assert "not absence" in degraded["absence_warning"]
    # The unverified empty carries its OWN warning — a different diagnosis
    # (probably broken) reaching the same verdict (not absence).
    assert "liveness was NOT" in empty["absence_warning"]
    assert "UNKNOWN" in empty["absence_warning"]
    assert degraded["absence_statement"] == empty["absence_statement"] == ""


@pytest.mark.parametrize(
    "unresponsive",
    [
        ["brave", "duckduckgo"],                       # bare strings
        [["brave", "too many requests"]],              # pair (SearXNG)
        [["brave", "CAPTCHA", True]],                  # triple
        [{"engine": "brave", "error": "CAPTCHA"}],     # object
    ],
)
def test_degradation_survives_every_unresponsive_engines_shape(unresponsive):
    """A shape surprise must not silently lose the degradation signal."""
    body = {"results": [], "unresponsive_engines": unresponsive}
    resp = parse_searxng_payload(body, query="q")
    assert resp.degraded is True
    assert resp.status is SearchStatus.DEGRADED_EMPTY
    assert resp.unresponsive_engines


def test_a_malformed_body_is_degraded_not_empty():
    """No `results` list at all is a STRUCTURAL surprise — reporting it as a
    clean empty result would be the same lie in a different wrapper."""
    for body in ({"error": "boom"}, [], "not json at all", None):
        resp = parse_searxng_payload(body, query="q")
        assert resp.status is SearchStatus.DEGRADED_EMPTY, body
        assert resp.supports_absence_claim is False


def test_generic_handler_reads_a_degradation_signal_when_present():
    resp = parse_generic_payload(
        {"results": [], "warning": "upstream partially unavailable"}, query="q",
    )
    assert resp.status is SearchStatus.DEGRADED_EMPTY
    assert "upstream partially unavailable" in resp.degraded_detail


def test_generic_handler_does_not_invent_degradation():
    """A well-formed body with no degradation signal must not be reported as
    degraded — the generic handler may not hedge. It is still only an
    UNVERIFIED empty: not degraded is not the same as claimable."""
    resp = parse_generic_payload({"results": []}, query="q")
    assert resp.status is SearchStatus.EMPTY
    assert resp.degraded is False
    assert resp.supports_absence_claim is False


# ---------------------------------------------------------------------------
# 2) The tool-level propagation
# ---------------------------------------------------------------------------


#: A clean, fully-served response — what a LIVE control probe returns.
_PROBE_LIVE = {
    "query": CONTROL_PROBE_QUERY,
    "results": [
        {"url": "https://un.org/", "title": "United Nations", "content": "…"},
    ],
    "unresponsive_engines": [],
}


class _StubProvider:
    """A bound provider handler, standing in for what the runtime binds.

    Answers the CONTROL PROBE separately from the query under test: the probe
    is a real second query through the same provider, so a stub that returned
    the same canned response to both could never exercise the empty+live case.
    Default probe response is LIVE, which is the honest default for a rig whose
    provider is obviously working.
    """

    component_id = "search.searxng.local"

    def __init__(
        self,
        response: SearchResponse | Exception,
        probe: SearchResponse | Exception | None = None,
    ):
        self._response = response
        self._probe = (
            probe if probe is not None
            else parse_searxng_payload(_PROBE_LIVE, query=CONTROL_PROBE_QUERY)
        )
        self.calls: list[tuple[str, int]] = []

    @property
    def probe_calls(self) -> list[tuple[str, int]]:
        return [c for c in self.calls if c[0] == CONTROL_PROBE_QUERY]

    async def search(self, query, *, limit=5, **opts):
        self.calls.append((query, limit))
        outcome = self._probe if query == CONTROL_PROBE_QUERY else self._response
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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


def _call(query="sanctions", **args) -> ToolCall:
    return ToolCall(
        pack_id="web_access", tool_name="web_search",
        budget_account="acct", requested_by="analyst.test",
        args={"query": query, **args},
    )


async def test_tool_reports_degraded_with_results_as_a_usable_completion():
    provider = _StubProvider(parse_searxng_payload(_LIVE_PARTIAL, query="sanctions"))
    result = await web_search_tool(
        _call(), _pack(), ToolContext(search=provider),
    )
    assert result.status == "completed"
    assert result.output["count"] == 1
    assert result.output["degraded"] is True
    assert result.output["supports_absence_claim"] is False
    assert "brave" in result.output["degraded_detail"]
    # Spec §4.6 rung 3: use the results, propagate the flag, do NOT silently
    # retry into a fallback (which would hide the ban and double-count the
    # query against engines already unhappy with us).
    assert len(provider.calls) == 1


async def test_tool_fails_loudly_when_degradation_ate_every_result():
    provider = _StubProvider(
        parse_searxng_payload(_ALL_ENGINES_REFUSED, query="sanctions")
    )
    result = await web_search_tool(
        _call(), _pack(), ToolContext(search=provider),
    )
    # NOT `completed` with count 0 — that is the shape read as "nothing found".
    assert result.status == "failed"
    assert "search_degraded_no_results" in (result.error or "")
    assert "not absence" in (result.error or "")
    # The evidence is still attached so the planner can name the engines.
    assert result.output["status"] == "degraded_empty"
    assert result.output["unresponsive_engines"]


async def test_tool_reports_a_liveness_verified_empty_as_a_scoped_absence():
    """The one path that may claim absence — and only a SCOPED one."""
    provider = _StubProvider(parse_searxng_payload(_TRUE_EMPTY, query="sanctions"))
    result = await web_search_tool(
        _call(), _pack(), ToolContext(search=provider),
    )
    assert result.status == "completed"
    assert result.output["count"] == 0
    assert result.output["status"] == "empty_verified"
    assert result.output["liveness"] == "live"
    assert result.output["supports_absence_claim"] is True
    # The control probe ran — absence was MEASURED, not assumed.
    assert provider.probe_calls
    # …and the licensed phrasing is scoped, never "X does not exist".
    statement = result.output["absence_statement"]
    assert "SCOPED absence" in statement
    assert "does NOT establish" in statement


async def test_tool_stamps_the_provider_for_provenance():
    provider = _StubProvider(parse_searxng_payload(_LIVE_PARTIAL, query="q"))
    pack = _pack({"provider": {"factory_kind": "stack_ref",
                               "raw": "search.searxng.local"}})
    result = await web_search_tool(_call(), pack, ToolContext(search=provider))
    assert result.output["provider"] == "search.searxng.local"
    assert result.output["provider_route"] == "config.provider"
    assert result.output["provider_route_class"] == "configured"


async def test_declared_route_with_no_bound_provider_fails_instead_of_returning_empty():
    """An unresolved provider and an empty web must never share a wire shape."""
    pack = _pack({"provider": {"factory_kind": "stack_ref",
                               "raw": "search.searxng.local"}})
    result = await web_search_tool(_call(), pack, ToolContext())
    assert result.status == "failed"
    assert "search_provider_unresolved" in (result.error or "")
    assert "not an empty result set" in (result.error or "")
    # No search evidence (no query was issued) — only the deferral advice, so
    # the caller knows to come back later rather than hammer the seam.
    assert set(result.output) == {"deferral"}
    assert result.output["deferral"]["defer"] is True
    assert result.output["deferral"]["reason"] == "search_provider_unresolved"


async def test_transient_and_hard_failures_are_classified_distinctly():
    from legba.data.stack.search import HardSearchFailure, TransientSearchFailure

    transient = await web_search_tool(
        _call(), _pack(),
        ToolContext(search=_StubProvider(TransientSearchFailure("HTTP 429"))),
    )
    assert transient.status == "failed"
    assert "search_unavailable" in (transient.error or "")

    hard = await web_search_tool(
        _call(), _pack(),
        ToolContext(search=_StubProvider(HardSearchFailure("egress_blocked: nope"))),
    )
    assert hard.status == "failed"
    assert "egress_blocked" in (hard.error or "")


async def test_tool_requires_a_query():
    result = await web_search_tool(
        ToolCall(pack_id="web_access", tool_name="web_search",
                 budget_account="a", requested_by="r", args={}),
        _pack(), ToolContext(),
    )
    assert result.status == "failed"
    assert "requires a 'query' arg" in (result.error or "")


# ---------------------------------------------------------------------------
# 3) LEGACY endpoint compatibility — the path that must not break
# ---------------------------------------------------------------------------


class _SearchFixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler contract
        parsed = urlparse(self.path)
        path = parsed.path
        query = (parse_qs(parsed.query).get("q") or [""])[0]
        bodies = {
            "/search": _LIVE_PARTIAL,
            # An instance that is ANSWERING but has nothing for this query: the
            # control probe (a different query) comes back full.
            "/search-empty": (
                _PROBE_LIVE if query == CONTROL_PROBE_QUERY else _TRUE_EMPTY
            ),
            # An instance that answers 200 + [] to EVERYTHING, with no
            # degradation signal at all — the silent-breakage shape the control
            # probe exists to catch.
            "/search-dead": _TRUE_EMPTY,
            "/search-blocked": _ALL_ENGINES_REFUSED,
        }
        if path in bodies:
            body = json.dumps(bodies[path]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/not-json":
            body = b"<html>SearXNG JSON format is off by default</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture(scope="module")
def search_fixture():
    server = HTTPServer(("127.0.0.1", 0), _SearchFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def allow_local_egress(monkeypatch):
    """Exact-hostname permit through the SSRF guard; everything else private
    stays blocked (the guard is the reason search can be pointed at the open
    web at all)."""
    monkeypatch.setenv("LEGBA_EGRESS_ALLOW_HOSTS", "127.0.0.1")


async def test_legacy_env_endpoint_still_works(
    search_fixture, allow_local_egress, monkeypatch,
):
    """LEGBA_WEB_SEARCH_ENDPOINT — the zero-code-change deployment path."""
    monkeypatch.setenv("LEGBA_WEB_SEARCH_ENDPOINT", f"{search_fixture}/search")
    result = await web_search_tool(_call(), _pack(), ToolContext())
    assert result.status == "completed"
    # Byte-compatible with the pre-family output shape.
    assert result.output["query"] == "sanctions"
    assert result.output["count"] == 1
    assert result.output["results"][0]["url"] == "https://example.org/a"
    assert result.output["results"][0]["title"] == "A"
    assert result.output["results"][0]["snippet"] == "c"
    # …and it now inherits the degradation read it never had.
    assert result.output["degraded"] is True
    assert result.output["provider"] == "legacy:env:LEGBA_WEB_SEARCH_ENDPOINT"


async def test_legacy_tool_config_endpoint_wins_over_env(
    search_fixture, allow_local_egress, monkeypatch,
):
    monkeypatch.setenv("LEGBA_WEB_SEARCH_ENDPOINT", f"{search_fixture}/search-empty")
    pack = _pack({"endpoint": f"{search_fixture}/search"})
    result = await web_search_tool(_call(), pack, ToolContext())
    assert result.status == "completed"
    assert result.output["count"] == 1
    assert result.output["provider"] == (
        "legacy:web_access.web_search.config.endpoint"
    )


async def test_legacy_path_gets_the_degraded_empty_failure_too(
    search_fixture, allow_local_egress, monkeypatch,
):
    monkeypatch.setenv(
        "LEGBA_WEB_SEARCH_ENDPOINT", f"{search_fixture}/search-blocked",
    )
    result = await web_search_tool(_call(), _pack(), ToolContext())
    assert result.status == "failed"
    assert "search_degraded_no_results" in (result.error or "")


async def test_legacy_liveness_verified_empty_stays_a_completion(
    search_fixture, allow_local_egress, monkeypatch,
):
    """End-to-end over HTTP: empty for the query, full for the control probe ⇒
    the engine set is demonstrably live ⇒ a scoped absence is admissible."""
    monkeypatch.setenv("LEGBA_WEB_SEARCH_ENDPOINT", f"{search_fixture}/search-empty")
    result = await web_search_tool(_call(), _pack(), ToolContext())
    assert result.status == "completed"
    assert result.output["count"] == 0
    assert result.output["status"] == "empty_verified"
    assert result.output["supports_absence_claim"] is True


async def test_legacy_silently_dead_instance_is_a_failure_not_an_absence(
    search_fixture, allow_local_egress, monkeypatch,
):
    """THE R-3d case, end-to-end. HTTP 200 + [] + NO degradation signal, for
    every query including the control probe. Byte-identical to a true absence
    at the wire; only the probe tells them apart."""
    monkeypatch.setenv("LEGBA_WEB_SEARCH_ENDPOINT", f"{search_fixture}/search-dead")
    result = await web_search_tool(_call(), _pack(), ToolContext())
    assert result.status == "failed"
    assert "search_liveness_unverified" in (result.error or "")
    assert "not absence" in (result.error or "")
    assert result.output["supports_absence_claim"] is False
    assert result.output["liveness"] == "dead"
    assert result.output["deferral"]["defer"] is True


async def test_legacy_html_body_is_the_json_format_off_failure(
    search_fixture, allow_local_egress, monkeypatch,
):
    """SearXNG ships with JSON output OFF; this is the failure an operator who
    forgot `search.formats: [html, json]` must see."""
    monkeypatch.setenv("LEGBA_WEB_SEARCH_ENDPOINT", f"{search_fixture}/not-json")
    result = await web_search_tool(_call(), _pack(), ToolContext())
    assert result.status == "failed"
    assert "not JSON" in (result.error or "")


async def test_legacy_no_endpoint_configured_message_is_unchanged(monkeypatch):
    monkeypatch.delenv("LEGBA_WEB_SEARCH_ENDPOINT", raising=False)
    result = await web_search_tool(_call(), _pack(), ToolContext())
    assert result.status == "failed"
    assert "no endpoint configured" in (result.error or "")


async def test_legacy_non_http_endpoint_message_is_unchanged(monkeypatch):
    monkeypatch.setenv("LEGBA_WEB_SEARCH_ENDPOINT", "ftp://example.invalid/s")
    result = await web_search_tool(_call(), _pack(), ToolContext())
    assert result.status == "failed"
    assert "must be http(s)" in (result.error or "")


async def test_legacy_private_endpoint_still_blocked_by_the_guard(monkeypatch):
    monkeypatch.delenv("LEGBA_EGRESS_ALLOW_HOSTS", raising=False)
    monkeypatch.setenv("LEGBA_WEB_SEARCH_ENDPOINT", "http://127.0.0.1:8888/search")
    result = await web_search_tool(_call(), _pack(), ToolContext())
    assert result.status == "failed"
    assert "egress_blocked" in (result.error or "")


async def test_tool_result_is_the_declared_type(search_fixture, allow_local_egress,
                                                monkeypatch):
    monkeypatch.setenv("LEGBA_WEB_SEARCH_ENDPOINT", f"{search_fixture}/search")
    result = await web_search_tool(_call(), _pack(), ToolContext())
    assert isinstance(result, ToolResult)
    assert result.units == 1
