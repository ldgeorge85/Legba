# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-C — the SEARCH PROVIDER LADDER: a paid rung beside the free one.

Three claims, and every one of them is exercised over the REAL binding path.
"Real binding path" here means the same chain production uses:

    registry row  →  build_search_handler_from_stack_component
                  →  legba.data.stack.search.build_handler
                  →  SEARCH_HANDLERS[config.subprovider]
                  →  handler.on_configure(secrets)
                  →  ToolContext.search / .search_fallbacks
                  →  web_search_tool
                  →  handler.search()  →  guarded httpx  →  a real HTTP server

No handler is ever hand-constructed and dropped onto the context in the tests
that matter, and no `handler.search` is monkeypatched. That is deliberate and
it is the whole reason this file is shaped the way it is: the search leg was
DEAD FOR FIVE WEEKS (`search_handler_factory.py`'s module docstring) while its
tests passed, because the tests built the handler themselves and never
traversed the binding site that was broken. A mocked handler proves the parser;
only the factory chain proves the wiring.

The three claims:

  1. **BYTE-IDENTITY.** With no `fallback_providers` and no Brave component,
     `web_search` returns the EXACT dict it returned before the ladder existed —
     asserted as full dict equality against a no-ladder baseline, not a spot
     check, and including the ABSENCE of the two ladder-only keys.
  2. **ESCALATION ON DEGRADATION.** Rung 0 degrading to zero results hands off
     to rung 1; a rung 0 that merely returns partial results does NOT.
  3. **THE MONEY IS GATED.** A metered rung is priced against the pack's
     `max_cost_usd_per_day` BEFORE it queries, refuses loudly rather than
     spending when it cannot be priced, and settles the true cost — control
     probe included — onto the ToolResult when it answers.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from legba.data.analysts.agency.search_cost import (
    SearchCostDecision,
    check_paid_rung_budget,
    pack_day_cost_cap,
)
from legba.data.analysts.agency.tools import ToolCall, ToolContext
from legba.data.analysts.agency.web_tools import web_search_tool
from legba.data.registry.health import KEY_REQUIRED_SEARCH_SUBPROVIDERS
from legba.data.schemas.action_pack import ActionPack
from legba.data.stack.search import (
    SEARCH_HANDLERS,
    BraveSearchHandler,
    SearchLivenessCache,
    SearchProviderUnresolved,
    resolve_tool_search_ladder,
)
from legba.data.stack.search.brave import (
    BRAVE_API_HOST,
    BRAVE_API_KEY_ENV,
    BRAVE_API_KEY_SECRET_ID,
    parse_brave_payload,
)
from legba.runtime.analyst_deps_builder import (
    build_search_handler_from_stack_component,
)
from legba.runtime.registry_client import RegistryHTTPClient
from legba.runtime.search_handler_factory import (
    clear_search_handler_cache,
    resolve_pack_search_fallback_bindings,
    resolve_pack_search_ladder,
)

BRAVE_COMPONENT = "search.brave.paid"
SEARXNG_COMPONENT = "search.searxng.local"
REGISTRY_PREFIX = "/api/v1/registry"


# ---------------------------------------------------------------------------
# A real HTTP server speaking both providers' wire shapes
# ---------------------------------------------------------------------------

#: SearXNG, healthy.
_SEARX_OK = {
    "query": "q",
    "results": [
        {"url": "https://searx.example/a", "title": "SearxA", "content": "sc",
         "engine": "duckduckgo"},
    ],
    "unresponsive_engines": [],
}
#: SearXNG with EVERY engine refused — HTTP 200, empty results, degradation
#: admitted. The exact shape the paid rung exists to take over from.
_SEARX_ALL_BANNED = {
    "query": "q",
    "results": [],
    "unresponsive_engines": [
        ["brave", "too many requests"], ["duckduckgo", "CAPTCHA"],
        ["startpage", "CAPTCHA"],
    ],
}
#: SearXNG serving PARTIAL results — usable hits plus named dead engines. This
#: must NOT escalate (retrying doubles down on engines already refusing).
_SEARX_PARTIAL = {
    "query": "q",
    "results": [
        {"url": "https://searx.example/p", "title": "Partial", "content": "pc",
         "engine": "mojeek"},
    ],
    "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
}
#: Brave web corpus, healthy.
_BRAVE_WEB = {
    "type": "search",
    "query": {"original": "q"},
    "web": {
        "type": "search",
        "results": [
            {"title": "BraveA", "url": "https://brave.example/a",
             "description": "bd", "page_age": "2026-09-01T00:00:00",
             "extra_snippets": ["more", "snippets"]},
            {"title": "BraveB", "url": "https://brave.example/b",
             "description": "bd2", "age": "3 days ago"},
        ],
    },
}
#: Brave news corpus — hits at TOP LEVEL, not under `web`.
_BRAVE_NEWS = {
    "type": "news",
    "results": [
        {"type": "news_result", "title": "NewsA", "url": "https://brave.example/n",
         "description": "nd", "age": "5 hours ago", "breaking": True},
    ],
}


class _LadderFixtureHandler(BaseHTTPRequestHandler):
    """Serves SearXNG and Brave bodies, and RECORDS every request.

    The recording is what lets the tests assert on the wire itself — that Brave
    got an `X-Subscription-Token` and never an `Authorization`, that the news
    corpus was reached at `/news/search`, and that a skipped rung issued NO
    request at all.
    """

    requests: list[dict] = []
    #: Stack-component rows this server serves at the REGISTRY route, so the
    #: tests traverse a real ``RegistryHTTPClient`` rather than a stand-in.
    stack_rows: dict[str, dict] = {}

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's API
        parsed = urlparse(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        type(self).requests.append({
            "path": path,
            "params": params,
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })
        # --- the REGISTRY surface: GET /api/v1/registry/stack/{component_id}
        if path.startswith(REGISTRY_PREFIX + "/stack/"):
            component_id = path[len(REGISTRY_PREFIX) + len("/stack/"):]
            row = type(self).stack_rows.get(component_id)
            if row is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._json(row)
            return
        bodies = {
            "/search": _SEARX_OK,
            "/search-banned": _SEARX_ALL_BANNED,
            "/search-partial": _SEARX_PARTIAL,
            "/res/v1/web/search": _BRAVE_WEB,
            "/res/v1/news/search": _BRAVE_NEWS,
        }
        if path == "/search-500":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in bodies:
            body = json.dumps(bodies[path]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), _LadderFixtureHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Exact-hostname egress permit + a clean handler cache and request log.

    ``127.0.0.1`` is on the allowlist because the fixture server is local; that
    is the SAME opt-in an operator gives the compose-network ``searxng``. Note
    what is NOT needed anywhere in this file: an allowlist entry for a PUBLIC
    host. See ``test_brave_api_host_needs_no_egress_allowlist_entry``.
    """
    monkeypatch.setenv("LEGBA_EGRESS_ALLOW_HOSTS", "127.0.0.1")
    monkeypatch.delenv("LEGBA_SEARCH_STACK_REF", raising=False)
    monkeypatch.delenv("LEGBA_WEB_SEARCH_ENDPOINT", raising=False)
    _LadderFixtureHandler.requests = []
    _LadderFixtureHandler.stack_rows = {}
    clear_search_handler_cache()
    yield
    clear_search_handler_cache()


# ---------------------------------------------------------------------------
# The REAL binding path: a fake registry serving real component rows
# ---------------------------------------------------------------------------


def _searxng_row(endpoint: str) -> dict:
    return {
        "component_id": SEARXNG_COMPONENT,
        "body": {
            "id": SEARXNG_COMPONENT,
            "schema_uri": "legba/stack/search_provider/1.0.0",
            "config": {
                "subprovider": {
                    "factory_kind": "dropdown_static", "raw": "searxng",
                    "options": ["searxng", "json", "firecrawl", "jina",
                                "tavily", "brave", "agent"],
                },
                "endpoint": {"factory_kind": "text", "raw": endpoint},
            },
        },
    }


def _brave_row(endpoint: str, *, cost: float = 0.005, api_key: bool = True) -> dict:
    config: dict = {
        "subprovider": {
            "factory_kind": "dropdown_static", "raw": "brave",
            "options": ["searxng", "json", "firecrawl", "jina", "tavily",
                        "brave", "agent"],
        },
        "endpoint": {"factory_kind": "text", "raw": endpoint},
        "cost_usd_per_query": {"factory_kind": "number", "raw": cost},
    }
    if api_key:
        config["api_key"] = {
            "factory_kind": "secret", "raw": BRAVE_API_KEY_SECRET_ID,
        }
    return {
        "component_id": BRAVE_COMPONENT,
        "body": {
            "id": BRAVE_COMPONENT,
            "schema_uri": "legba/stack/search_provider/1.0.0",
            "config": config,
        },
    }


def _registry(server: str, rows: dict[str, dict]) -> RegistryHTTPClient:
    """A REAL ``RegistryHTTPClient`` pointed at the fixture server.

    Not a stand-in object with a convenient method: the component rows are
    served over HTTP at the registry's own ``/stack/{id}`` route, so
    ``_fetch_stack_component`` runs its real path construction, its real httpx
    GET, its real 404 handling and its real JSON decode. The five-week dead
    search leg was invisible precisely because the tests stopped short of this.
    """
    _LadderFixtureHandler.stack_rows = dict(rows)
    return RegistryHTTPClient(
        base_url=server, api_prefix=REGISTRY_PREFIX, token="test",
    )


async def _secrets(secret_id: str) -> bytes:
    assert secret_id == BRAVE_API_KEY_SECRET_ID, secret_id
    return b"test-subscription-token"


async def _no_secrets(secret_id: str) -> bytes:
    raise KeyError(f"vault missing secret {secret_id!r}")


def _pack(web_search_config: dict, *, max_cost_usd_per_day=None) -> ActionPack:
    governor = {"budget_account": "web_access", "max_invocations_per_hour": 120}
    if max_cost_usd_per_day is not None:
        governor["max_cost_usd_per_day"] = max_cost_usd_per_day
    return ActionPack.model_validate({
        "identity": {
            "id": "web_access", "name": "Web Access Tools",
            "schema_uri": "legba/action_pack/1.0.0", "version": "a" * 16,
            "state": "active", "owner": "s6_agency",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "tools": [{"name": "web_search", "config": web_search_config}],
        "governor": governor,
    }, strict=False)


def _call(query: str = "mali fuel blockade") -> ToolCall:
    return ToolCall(
        pack_id="web_access", tool_name="web_search", args={"query": query},
        budget_account="web_access",
    )


class _Ledger:
    """A cost ledger double. Three lines, per the protocol's whole point."""

    def __init__(self, spent: float = 0.0):
        self.spent = spent
        self.calls: list[tuple[str, str]] = []

    async def spent_today_usd(self, *, pack_id: str, budget_account: str) -> float:
        self.calls.append((pack_id, budget_account))
        return self.spent


class _ExplodingLedger:
    async def spent_today_usd(self, *, pack_id: str, budget_account: str) -> float:
        raise RuntimeError("pool exhausted")


async def _bind(pack, registry, secrets=_secrets):
    """THE REAL BINDING PATH, end to end — the chain production runs.

    Returns the ToolContext a runtime binding site would assemble: rung 0 via
    ``build_search_handler_from_stack_component`` (the same call
    ``resolve_search_handler`` makes) and the fallback rungs via
    ``resolve_pack_search_fallback_bindings`` (the same call both binding sites
    now make). Nothing here constructs a handler class directly.
    """
    rungs = resolve_pack_search_ladder(pack)
    rung0 = None
    route0 = None
    if rungs:
        route0 = rungs[0]
        try:
            rung0 = await build_search_handler_from_stack_component(
                route0.component_id, registry_client=registry,
                secrets_resolve=secrets,
            )
        except Exception:
            rung0 = None
    fallbacks = await resolve_pack_search_fallback_bindings(
        pack, registry_client=registry, secrets_resolve=secrets,
    )
    return ToolContext(
        search=rung0, search_route=route0, search_fallbacks=fallbacks,
        search_liveness=SearchLivenessCache(),
    )


def _ref(component_id: str) -> dict:
    return {"factory_kind": "stack_ref", "raw": component_id}


# ---------------------------------------------------------------------------
# CLAIM 1 — byte-identity when no ladder is configured
# ---------------------------------------------------------------------------


async def test_ladder_absent_is_byte_identical(server):
    """No `fallback_providers`, no Brave component ⇒ the pre-ladder output.

    Full DICT EQUALITY against a baseline captured from the same run, plus an
    explicit assertion that neither ladder-only key exists. A spot check would
    not catch the regression this guards: an additive key silently appearing in
    every searxng response and changing what a planner reads.
    """
    registry = _registry(server, {SEARXNG_COMPONENT: _searxng_row(f"{server}/search")})
    pack = _pack({"provider": _ref(SEARXNG_COMPONENT)})
    ctx = await _bind(pack, registry)
    assert ctx.search is not None, "rung 0 must bind through the real factory"
    assert ctx.search_fallbacks == [], "no fallback_providers ⇒ no rungs below"

    result = await web_search_tool(_call(), pack, ctx)

    assert result.status == "completed"
    assert result.cost_usd == 0.0, "a free rung must never stamp a cost"
    assert result.units == 1
    # The ladder-only keys are ABSENT — not empty, absent.
    assert "provider_used" not in result.output
    assert "ladder" not in result.output
    # And the whole dict is what the single-provider path always produced.
    expected = {
        "query": "mali fuel blockade",
        "results": [{
            "title": "SearxA", "url": "https://searx.example/a",
            "snippet": "sc", "engine": "duckduckgo", "rank": 1,
        }],
        "count": 1,
        "provider": SEARXNG_COMPONENT,
        "subprovider": "searxng",
        "retrieved_at": result.output["retrieved_at"],
        "status": "ok",
        "degraded": False,
        "degraded_detail": "",
        "unresponsive_engines": [],
        "liveness": "unverified",
        "liveness_detail": "",
        "supports_absence_claim": False,
        "absence_statement": "",
        "absence_warning": "",
        "provider_route": "config.provider",
        "provider_route_class": "configured",
    }
    assert result.output == expected


async def test_ladder_absent_keeps_the_degraded_empty_failure_shape(server):
    """The honesty contract is untouched by the ladder machinery.

    A degraded-empty on a one-rung ladder is still the SAME loud failure, with
    the same error prefix the pack rules and the planner match on, and still no
    ladder keys.
    """
    registry = _registry(server, {
        SEARXNG_COMPONENT: _searxng_row(f"{server}/search-banned"),
    })
    pack = _pack({"provider": _ref(SEARXNG_COMPONENT)})
    result = await web_search_tool(_call(), pack, await _bind(pack, registry))
    assert result.status == "failed"
    assert result.error.startswith("search_degraded_no_results:")
    assert "not absence" in result.error
    assert result.output["supports_absence_claim"] is False
    assert result.output["deferral"]["defer"] is True
    assert "ladder" not in result.output
    assert result.cost_usd == 0.0


# ---------------------------------------------------------------------------
# CLAIM 2 — escalation, over the real binding path
# ---------------------------------------------------------------------------


async def test_degraded_rung0_escalates_to_the_paid_rung(server):
    """THE headline case, end to end over real HTTP.

    SearXNG answers 200 with every engine banned and zero results — the exact
    false-absence shape — and the ladder hands off to Brave, which answers. The
    ToolResult says WHICH rung answered and carries the per-rung log.
    """
    registry = _registry(server, {
        SEARXNG_COMPONENT: _searxng_row(f"{server}/search-banned"),
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search"),
    })
    pack = _pack(
        {"provider": _ref(SEARXNG_COMPONENT),
         "fallback_providers": [_ref(BRAVE_COMPONENT)]},
        max_cost_usd_per_day=1.0,
    )
    ctx = await _bind(pack, registry)
    # Both rungs bound through the factory chain, not by hand.
    assert ctx.search is not None
    assert len(ctx.search_fallbacks) == 1
    fallback_handler, fallback_route = ctx.search_fallbacks[0]
    assert isinstance(fallback_handler, BraveSearchHandler)
    assert fallback_route.component_id == BRAVE_COMPONENT
    assert fallback_route.route_class == "fallback"
    ctx.search_cost_ledger = _Ledger(spent=0.0)

    result = await web_search_tool(_call(), pack, ctx)

    assert result.status == "completed"
    assert result.output["provider_used"] == BRAVE_COMPONENT
    assert result.output["provider"] == BRAVE_COMPONENT
    assert result.output["subprovider"] == "brave"
    assert [r["url"] for r in result.output["results"]] == [
        "https://brave.example/a", "https://brave.example/b",
    ]
    # The ladder log names rung 0's failure AND rung 1's success.
    ladder = result.output["ladder"]
    assert [e["provider"] for e in ladder] == [SEARXNG_COMPONENT, BRAVE_COMPONENT]
    assert ladder[0]["outcome"] == "search_degraded_no_results"
    assert ladder[1]["outcome"] == "ok"
    # Both rungs really went to the wire.
    paths = [r["path"] for r in _LadderFixtureHandler.requests]
    assert "/search-banned" in paths
    assert "/res/v1/web/search" in paths


async def test_partial_results_do_not_escalate(server):
    """Degraded WITH hits stays on rung 0 — the no-retry-on-degraded rule.

    Escalating here would double-count the query against engines that are
    already refusing us, which is the reason the rule exists. The paid rung must
    not be touched at all: asserted on the WIRE, not just on the output.
    """
    registry = _registry(server, {
        SEARXNG_COMPONENT: _searxng_row(f"{server}/search-partial"),
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search"),
    })
    pack = _pack(
        {"provider": _ref(SEARXNG_COMPONENT),
         "fallback_providers": [_ref(BRAVE_COMPONENT)]},
        max_cost_usd_per_day=1.0,
    )
    ctx = await _bind(pack, registry)
    ctx.search_cost_ledger = _Ledger()

    result = await web_search_tool(_call(), pack, ctx)

    assert result.status == "completed"
    assert result.output["degraded"] is True
    assert result.output["provider_used"] == SEARXNG_COMPONENT
    assert result.output["supports_absence_claim"] is False
    assert result.cost_usd == 0.0, "no paid query was issued"
    assert not any(
        r["path"].startswith("/res/v1") for r in _LadderFixtureHandler.requests
    ), "the paid rung must not be queried when rung 0 served usable hits"


async def test_a_transient_rung0_escalates(server):
    """A 503 on rung 0 is retryable — and the retry is the NEXT rung, once."""
    registry = _registry(server, {
        SEARXNG_COMPONENT: _searxng_row(f"{server}/search-500"),
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search"),
    })
    pack = _pack(
        {"provider": _ref(SEARXNG_COMPONENT),
         "fallback_providers": [_ref(BRAVE_COMPONENT)]},
        max_cost_usd_per_day=1.0,
    )
    ctx = await _bind(pack, registry)
    ctx.search_cost_ledger = _Ledger()
    result = await web_search_tool(_call(), pack, ctx)
    assert result.status == "completed"
    assert result.output["provider_used"] == BRAVE_COMPONENT
    assert result.output["ladder"][0]["outcome"] == "search_unavailable"
    assert sum(
        1 for r in _LadderFixtureHandler.requests if r["path"] == "/search-500"
    ) == 1, "a rung is attempted AT MOST ONCE per run"


async def test_a_deferred_rung_is_skipped_not_hammered(server):
    """The deferral ladder holds ACROSS rungs, not only across ticks.

    Call 1 degrades rung 0, which arms its backoff. Call 2 must SKIP rung 0
    entirely — no second request to the banned instance — and go straight to
    the paid rung. Hammering engines that are already refusing is the failure
    this rule exists to prevent, and having somewhere else to go does not
    suspend it.
    """
    registry = _registry(server, {
        SEARXNG_COMPONENT: _searxng_row(f"{server}/search-banned"),
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search"),
    })
    pack = _pack(
        {"provider": _ref(SEARXNG_COMPONENT),
         "fallback_providers": [_ref(BRAVE_COMPONENT)]},
        max_cost_usd_per_day=1.0,
    )
    ctx = await _bind(pack, registry)
    ctx.search_cost_ledger = _Ledger()

    first = await web_search_tool(_call("one"), pack, ctx)
    assert first.output["provider_used"] == BRAVE_COMPONENT
    banned_hits = sum(
        1 for r in _LadderFixtureHandler.requests if r["path"] == "/search-banned"
    )
    assert banned_hits == 1

    second = await web_search_tool(_call("two"), pack, ctx)
    assert second.status == "completed"
    assert second.output["provider_used"] == BRAVE_COMPONENT
    assert second.output["ladder"][0]["outcome"] == "deferred"
    assert sum(
        1 for r in _LadderFixtureHandler.requests if r["path"] == "/search-banned"
    ) == banned_hits, "a deferred rung must issue NO further request"


async def test_a_declared_but_unbuildable_rung_is_named_not_dropped(server):
    """A fallback whose component is missing is reported BY NAME.

    A ladder that silently gets shorter is how "we had no paid provider today"
    becomes indistinguishable from "the paid provider found nothing".
    """
    registry = _registry(server, {
        SEARXNG_COMPONENT: _searxng_row(f"{server}/search-banned"),
    })
    pack = _pack(
        {"provider": _ref(SEARXNG_COMPONENT),
         "fallback_providers": [_ref(BRAVE_COMPONENT)]},
        max_cost_usd_per_day=1.0,
    )
    ctx = await _bind(pack, registry)
    assert ctx.search_fallbacks == [(None, ctx.search_fallbacks[0][1])]
    result = await web_search_tool(_call(), pack, ctx)
    assert result.status == "failed"
    assert result.output["provider_used"] == ""
    outcomes = {e["provider"]: e["outcome"] for e in result.output["ladder"]}
    assert outcomes[BRAVE_COMPONENT] == "unresolved"
    assert "search_provider_unresolved" in result.error
    assert "NO query was issued" in result.error


# ---------------------------------------------------------------------------
# CLAIM 3 — the money is gated
# ---------------------------------------------------------------------------


async def test_paid_rung_settles_its_true_cost_including_the_probe(server):
    """`cost_usd` counts the search AND the liveness control probe.

    Both are real, billed queries. Rung 0 is degraded-empty (free), then Brave
    is asked something the fixture answers with results, so exactly one paid
    query runs: 1 x 0.005.
    """
    registry = _registry(server, {
        SEARXNG_COMPONENT: _searxng_row(f"{server}/search-banned"),
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search", cost=0.005),
    })
    pack = _pack(
        {"provider": _ref(SEARXNG_COMPONENT),
         "fallback_providers": [_ref(BRAVE_COMPONENT)]},
        max_cost_usd_per_day=1.0,
    )
    ctx = await _bind(pack, registry)
    ledger = _Ledger(spent=0.10)
    ctx.search_cost_ledger = ledger

    result = await web_search_tool(_call(), pack, ctx)

    assert result.status == "completed"
    assert result.cost_usd == pytest.approx(0.005)
    assert result.output["ladder"][1]["queries"] == 1
    assert result.output["ladder"][1]["cost_usd"] == pytest.approx(0.005)
    # It priced against the pack + budget account, once.
    assert ledger.calls == [("web_access", "web_access")]


async def test_cap_exhausted_refuses_the_paid_rung_loudly(server):
    """Over budget ⇒ the paid rung is NOT queried, and the failure says so.

    The distinction the error text has to carry is "we did not look" vs "we
    looked and found nothing" — a budget refusal reported as a zero-result
    success would manufacture exactly the false absence this plane exists to
    prevent.
    """
    registry = _registry(server, {
        SEARXNG_COMPONENT: _searxng_row(f"{server}/search-banned"),
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search", cost=0.005),
    })
    pack = _pack(
        {"provider": _ref(SEARXNG_COMPONENT),
         "fallback_providers": [_ref(BRAVE_COMPONENT)]},
        max_cost_usd_per_day=1.0,
    )
    ctx = await _bind(pack, registry)
    ctx.search_cost_ledger = _Ledger(spent=0.999)  # 0.999 + 0.005 > 1.00

    result = await web_search_tool(_call(), pack, ctx)

    assert result.status == "failed"
    assert result.error.startswith("search_cost_cap_exhausted:")
    assert "NOT queried" in result.error
    assert "we did not look" in result.error
    assert "NOT an empty result set" in result.error
    assert result.cost_usd == 0.0
    assert not any(
        r["path"].startswith("/res/v1") for r in _LadderFixtureHandler.requests
    ), "an over-cap rung must issue NO request"


async def test_no_cap_declared_refuses_rather_than_opening_a_tab(server):
    """A pack with no `max_cost_usd_per_day` cannot run a metered rung.

    This inverts the governor's own "an unset cap is unenforced" default, on
    purpose: an unenforced RATE cap costs nothing, an unenforced SPEND cap on a
    metered external API is an open tab.
    """
    registry = _registry(server, {
        SEARXNG_COMPONENT: _searxng_row(f"{server}/search-banned"),
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search"),
    })
    pack = _pack({"provider": _ref(SEARXNG_COMPONENT),
                  "fallback_providers": [_ref(BRAVE_COMPONENT)]})
    assert pack_day_cost_cap(pack) is None
    ctx = await _bind(pack, registry)
    ctx.search_cost_ledger = _Ledger()

    result = await web_search_tool(_call(), pack, ctx)
    assert result.status == "failed"
    assert result.error.startswith("search_cost_no_cap_declared:")
    assert not any(
        r["path"].startswith("/res/v1") for r in _LadderFixtureHandler.requests
    )


async def test_no_ledger_bound_refuses_rather_than_billing_blind(server):
    """No cost ledger ⇒ the paid rung is refused, never spent-and-hoped."""
    registry = _registry(server, {
        SEARXNG_COMPONENT: _searxng_row(f"{server}/search-banned"),
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search"),
    })
    pack = _pack(
        {"provider": _ref(SEARXNG_COMPONENT),
         "fallback_providers": [_ref(BRAVE_COMPONENT)]},
        max_cost_usd_per_day=1.0,
    )
    ctx = await _bind(pack, registry)
    assert ctx.search_cost_ledger is None
    result = await web_search_tool(_call(), pack, ctx)
    assert result.status == "failed"
    assert result.error.startswith("search_cost_no_ledger_bound:")
    assert not any(
        r["path"].startswith("/res/v1") for r in _LadderFixtureHandler.requests
    )


async def test_an_unreadable_ledger_refuses_rather_than_assuming_headroom():
    """A ledger that raises is not evidence of head-room."""
    pack = _pack({"provider": _ref(SEARXNG_COMPONENT)}, max_cost_usd_per_day=5.0)
    decision = await check_paid_rung_budget(
        pack=pack, ledger=_ExplodingLedger(), budget_account="web_access",
        cost_usd=0.005, component_id=BRAVE_COMPONENT,
    )
    assert isinstance(decision, SearchCostDecision)
    assert decision.admitted is False
    assert decision.cause == "ledger_unavailable"
    assert "not evidence of head-room" in decision.detail


async def test_a_free_rung_never_consults_the_ledger(server):
    """searxng is $0 — the budget machinery must not be in its path at all."""
    registry = _registry(server, {SEARXNG_COMPONENT: _searxng_row(f"{server}/search")})
    pack = _pack({"provider": _ref(SEARXNG_COMPONENT)}, max_cost_usd_per_day=1.0)
    ctx = await _bind(pack, registry)
    ledger = _Ledger()
    ctx.search_cost_ledger = ledger
    result = await web_search_tool(_call(), pack, ctx)
    assert result.status == "completed"
    assert ledger.calls == []
    assert result.cost_usd == 0.0


# ---------------------------------------------------------------------------
# The Brave handler itself — key, headers, both corpora
# ---------------------------------------------------------------------------


async def test_brave_is_a_registered_subprovider_bound_by_config_not_sniffing():
    assert SEARCH_HANDLERS["brave"] is BraveSearchHandler
    assert BraveSearchHandler.subprovider == "brave"
    assert BraveSearchHandler.capabilities == frozenset({"search"})


async def test_unbound_key_is_a_refusal_that_says_no_query_was_issued(server):
    """THE fail-loud requirement, through the real build path.

    A component whose vault entry is missing must not bind into a handler that
    then answers every query with zero results — that is the manufactured
    false absence the whole plane exists to prevent.
    """
    registry = _registry(server, {
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search"),
    })
    with pytest.raises(SearchProviderUnresolved) as exc:
        await build_search_handler_from_stack_component(
            BRAVE_COMPONENT, registry_client=registry, secrets_resolve=_no_secrets,
        )
    assert "NO query was issued" in str(exc.value)
    assert BRAVE_API_KEY_ENV in str(exc.value)


async def test_a_component_declaring_no_api_key_refuses_at_bind_time(server):
    registry = _registry(server, {
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search", api_key=False),
    })
    with pytest.raises(SearchProviderUnresolved) as exc:
        await build_search_handler_from_stack_component(
            BRAVE_COMPONENT, registry_client=registry, secrets_resolve=_secrets,
        )
    assert "declares no api_key" in str(exc.value)
    assert "NO query was issued" in str(exc.value)


async def test_an_empty_resolved_key_refuses_too(server):
    async def _blank(_secret_id: str) -> bytes:
        return b"   "

    registry = _registry(server, {
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search"),
    })
    with pytest.raises(SearchProviderUnresolved) as exc:
        await build_search_handler_from_stack_component(
            BRAVE_COMPONENT, registry_client=registry, secrets_resolve=_blank,
        )
    assert "resolved EMPTY" in str(exc.value)


async def test_brave_sends_its_own_auth_header_never_a_bearer(server):
    """The reason this is a module and not a `search.json.*` config.

    The generic handler would send `Authorization: Bearer` — which Brave
    rejects, and the rejection reads downstream like a provider problem rather
    than a wiring one.
    """
    registry = _registry(server, {
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search"),
    })
    handler = await build_search_handler_from_stack_component(
        BRAVE_COMPONENT, registry_client=registry, secrets_resolve=_secrets,
    )
    await handler.search("q", limit=5)
    sent = _LadderFixtureHandler.requests[-1]
    assert sent["headers"]["x-subscription-token"] == "test-subscription-token"
    assert "authorization" not in sent["headers"]
    assert sent["headers"]["accept"] == "application/json"
    # And it pages with Brave's own parameter.
    assert sent["params"]["count"] == "5"
    assert sent["params"]["q"] == "q"


async def test_brave_reaches_the_news_corpus_off_the_same_component(server):
    """Both endpoints, one registered component — the second config reason."""
    registry = _registry(server, {
        BRAVE_COMPONENT: _brave_row(f"{server}/res/v1/web/search"),
    })
    handler = await build_search_handler_from_stack_component(
        BRAVE_COMPONENT, registry_client=registry, secrets_resolve=_secrets,
    )
    response = await handler.search("q", limit=5, mode="news")
    assert _LadderFixtureHandler.requests[-1]["path"] == "/res/v1/news/search"
    assert [r.url for r in response.results] == ["https://brave.example/n"]
    assert response.results[0].engine == "brave_news"
    assert response.results[0].published_at == "5 hours ago"


async def test_brave_refuses_to_send_its_token_to_another_host(server):
    """The credential fence the SSRF guard structurally cannot provide.

    A public host that is not Brave passes the egress guard cleanly — with the
    paid subscription token attached — so the fence has to live in the handler.
    """
    registry = _registry(server, {
        BRAVE_COMPONENT: _brave_row("https://evil.example/res/v1/web/search"),
    })
    handler = await build_search_handler_from_stack_component(
        BRAVE_COMPONENT, registry_client=registry, secrets_resolve=_secrets,
    )
    with pytest.raises(Exception) as exc:
        await handler.search("q", limit=5)
    assert "not one of" in str(exc.value)
    assert "NO query was issued" in str(exc.value)
    # The fence fires BEFORE egress: the only traffic was the registry read
    # that built the handler — no search request was ever sent anywhere.
    assert all(
        r["path"].startswith(REGISTRY_PREFIX)
        for r in _LadderFixtureHandler.requests
    )


def test_brave_api_host_needs_no_egress_allowlist_entry(monkeypatch):
    """The corollary that is easy to get backwards, pinned as a test.

    `LEGBA_EGRESS_ALLOW_HOSTS` permits RFC-1918 sidecars the guard would
    otherwise REFUSE. api.search.brave.com is public, so it is already
    permitted with an EMPTY allowlist — adding it there would be a no-op that
    teaches a false model. The guard still refuses a private target.
    """
    from legba.data.sources._egress import EgressBlockedError, assert_public_host

    monkeypatch.delenv("LEGBA_EGRESS_ALLOW_HOSTS", raising=False)
    assert_public_host(BRAVE_API_HOST, 443)  # no allowlist entry, no raise
    with pytest.raises(EgressBlockedError):
        assert_public_host("127.0.0.1", 8080)


def test_brave_never_fabricates_extracted_text_or_a_license():
    """`extra_snippets` is MORE SNIPPETS, not clean main text.

    Joining them would manufacture a document that was never retrieved, and
    hand the faithfulness verify leg text that was never grounded in the cited
    bytes.
    """
    response = parse_brave_payload(_BRAVE_WEB, query="q")
    assert response.results, "fixture must yield hits"
    for hit in response.results:
        assert hit.extracted_text is None
        assert hit.extract_source is None
        assert hit.license_class is None
        assert hit.score is None


def test_brave_error_envelope_is_degraded_never_a_clean_empty():
    """Brave serves its ErrorResponse under 2xx too — a status check misses it."""
    response = parse_brave_payload(
        {"type": "ErrorResponse",
         "error": {"code": "VALIDATION", "status": 422, "detail": "bad count"}},
        query="q",
    )
    assert response.results == []
    assert response.degraded is True
    assert "VALIDATION" in response.degraded_detail
    assert response.supports_absence_claim is False


def test_a_structurally_surprising_brave_body_is_degraded_not_absence():
    for payload in ({"type": "search"}, {"type": "search", "web": {}}, ["nope"]):
        response = parse_brave_payload(payload, query="q")
        assert response.degraded is True, payload
        assert response.supports_absence_claim is False


def test_a_clean_brave_empty_is_a_plain_empty_not_a_degradation():
    """Brave is ONE first-party index, so it really can just have nothing.

    Reporting that as degraded would be as dishonest as the reverse; the
    liveness control probe is what decides whether the empty is admissible.
    """
    response = parse_brave_payload(
        {"type": "search", "web": {"results": []}}, query="q",
    )
    assert response.results == []
    assert response.degraded is False
    assert response.status.value == "empty"
    assert response.supports_absence_claim is False


# ---------------------------------------------------------------------------
# Route + health + config plumbing
# ---------------------------------------------------------------------------


def test_fallbacks_alone_never_enable_search(monkeypatch):
    """Rung 0 is the opt-in gate for the WHOLE ladder, not just for itself.

    A fallback list that could switch search on would be a second, quieter way
    to conscript an analyst that never asked for external retrieval.
    """
    monkeypatch.delenv("LEGBA_SEARCH_STACK_REF", raising=False)
    assert resolve_tool_search_ladder(
        {"fallback_providers": [_ref(BRAVE_COMPONENT)]}
    ) == []


def test_a_duplicate_rung_is_dropped(monkeypatch):
    monkeypatch.delenv("LEGBA_SEARCH_STACK_REF", raising=False)
    rungs = resolve_tool_search_ladder({
        "provider": _ref(SEARXNG_COMPONENT),
        "fallback_providers": [
            _ref(BRAVE_COMPONENT), _ref(SEARXNG_COMPONENT), _ref(BRAVE_COMPONENT),
        ],
    })
    assert [r.component_id for r in rungs] == [SEARXNG_COMPONENT, BRAVE_COMPONENT]


def test_fallback_rungs_classify_as_fallback_for_provenance(monkeypatch):
    monkeypatch.delenv("LEGBA_SEARCH_STACK_REF", raising=False)
    rungs = resolve_tool_search_ladder({
        "provider": _ref(SEARXNG_COMPONENT),
        "fallback_providers": [_ref(BRAVE_COMPONENT)],
    })
    assert rungs[0].route_class == "configured"
    assert rungs[1].route_class == "fallback"
    assert rungs[1].source == "config.fallback_providers[0]"


def test_cost_per_query_defaults_to_zero_so_existing_components_are_free():
    """The field is additive: the live searxng body omits it and stays $0."""
    from legba.data.schemas.stack import SearchProviderConfig

    cfg = SearchProviderConfig.model_validate({
        "subprovider": {
            "factory_kind": "dropdown_static", "raw": "searxng",
            "options": ["searxng", "json", "firecrawl", "jina", "tavily",
                        "brave", "agent"],
        },
        "endpoint": {"factory_kind": "text", "raw": "http://searxng:8080/search"},
    })
    assert cfg.cost_usd_per_query.raw == 0


async def test_health_reports_a_keyless_brave_component_as_unhealthy():
    """The one false-HEALTHY the TCP checker CAN close.

    api.search.brave.com:443 answers from anywhere, so without this branch an
    operator who registered the component but never loaded the vault entry gets
    a green light for a provider that refuses 100% of queries.
    """
    from legba.data.registry.health import HealthState, SearchProviderChecker
    from legba.data.schemas.stack import SearchProviderConfig

    assert "brave" in KEY_REQUIRED_SEARCH_SUBPROVIDERS

    class _Resolved:
        def __init__(self, config):
            self.config = config

        async def secret(self, factory_value):
            return None

    cfg = SearchProviderConfig.model_validate({
        "subprovider": {
            "factory_kind": "dropdown_static", "raw": "brave",
            "options": ["searxng", "json", "firecrawl", "jina", "tavily",
                        "brave", "agent"],
        },
        "endpoint": {
            "factory_kind": "text",
            "raw": f"https://{BRAVE_API_HOST}/res/v1/web/search",
        },
        "cost_usd_per_query": {"factory_kind": "number", "raw": 0.005},
    })
    health = await SearchProviderChecker().check(BRAVE_COMPONENT, _Resolved(cfg))
    assert health.state is HealthState.UNHEALTHY
    assert "requires an api_key" in health.detail
    assert health.extra["key_declared"] is False
    assert health.extra["cost_usd_per_query"] == 0.005


def test_the_committed_descriptor_is_the_registrar_source_of_truth():
    """One body, one place — the tree file IS what the registrar registers."""
    import importlib.util
    from pathlib import Path

    import yaml

    from legba.data.schemas.stack import SearchProvider

    repo = Path(__file__).resolve().parents[2]
    path = repo / "descriptors" / "stack_component_search_brave.yaml"
    body = yaml.safe_load(path.read_text())
    model = SearchProvider.model_validate(body)
    assert model.id == BRAVE_COMPONENT
    assert model.state.value == "draft", "must not ship active — no key exists yet"
    assert model.config.subprovider.raw == "brave"
    assert model.config.api_key.raw == BRAVE_API_KEY_SECRET_ID
    assert model.config.cost_usd_per_query.raw > 0
    assert BRAVE_API_HOST in model.config.endpoint.raw

    spec = importlib.util.spec_from_file_location(
        "_brs", repo / "scripts" / "bringup_register_stack.py",
    )
    registrar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registrar)
    ids = [cid for cid, _ in registrar.COMPONENTS]
    assert BRAVE_COMPONENT in ids, "the registrar must list the paid rung"
    registered = next(b for cid, b in registrar.COMPONENTS if cid == BRAVE_COMPONENT)
    assert registered == body, "the registrar must LOAD the descriptor, not copy it"


def test_the_vault_loader_maps_the_brave_key_from_env():
    """`LEGBA_BRAVE_SEARCH_API_KEY` → the vault id the descriptor points at."""
    import importlib.util
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_bvl", repo / "scripts" / "bringup_vault_load.py",
    )
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    assert (BRAVE_API_KEY_SECRET_ID, BRAVE_API_KEY_ENV) in loader.MAPPING
