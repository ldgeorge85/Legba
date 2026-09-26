# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#85 — the standing auditor's SEARCH LEG, through the REAL resolution path.

THE BUG THIS FILE EXISTS FOR. ``search.searxng.local`` was registered, active
and healthcheck-healthy from 2026-07-28, the shipped ``web_access`` descriptor
declared ``config.provider -> search.searxng.local``, and the auditor ran daily
— yet every verdict came back ``UNCHECKED`` and the runtime logged
``web_search.provider_unresolved component=search.searxng.local
source=config.provider`` on every attempt. Rung 1 of the ladder never resolved
in the real binding path.

Cause: ``external_audit_binding`` sourced its provider from
``AGENCY_HOLDER["tool_context"]`` — the process-wide bring-up ToolContext, which
``source_first_runtime`` builds with ``queue`` + ``emit`` and NOTHING else. The
only resolution in the runtime lived inside ``dapr_host``'s in-actor GATHER
branch, which a ``deterministic`` sub-handler never enters. So ``search`` was
structurally ``None`` while the route still resolved, and every search took
``web_search``'s declared-but-unbound branch.

WHY THE OLD TESTS WERE GREEN. The auditor's e2e set hand-built its binding as
``ToolContext(search=_FakeSearchProvider(...))`` — injecting the resolved
provider directly, which is precisely the step that was broken in production.
A double placed at the OUTPUT of the thing under test can only ever prove the
code downstream of it. So the doubles here sit at the two SOCKETS (the registry
GET and the provider GET) and everything between them — route resolution,
component fetch, family assertion, handler construction, configuration, the
three-way gate, the tool ladder — runs for real.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENT_ID = "search.searxng.local"
SERPER_ID = "search.serper.paid"   # 2026-09-20: the pack declares it as fallback_providers[0]
SEARXNG_ENDPOINT = "http://searxng:8080/search"


# ---------------------------------------------------------------------------
# The two sanctioned sockets
# ---------------------------------------------------------------------------


def _stack_row(component_id: str = COMPONENT_ID, *, schema_uri: str | None = None):
    """The registry's ``/stack/{id}`` body for the SHIPPED searxng component."""
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
                "endpoint": {"factory_kind": "text", "raw": SEARXNG_ENDPOINT},
                "timeout_seconds": {"factory_kind": "number", "raw": 15,
                                    "minimum": 1, "maximum": 300},
                "max_results": {"factory_kind": "number", "raw": 10,
                                "minimum": 1, "maximum": 50},
            },
        },
    }


def _shipped_pack_body(*, drop_provider: bool = False) -> dict[str, Any]:
    """The REAL shipped descriptor — not a hand-written stand-in.

    Reading the file is the point: if an operator edits the ref (or deletes it)
    these tests move with the shipped config instead of pinning a copy that
    silently diverges.
    """
    body = yaml.safe_load(
        (REPO_ROOT / "descriptors" / "action_pack_web_access.yaml").read_text()
    )
    body["identity"]["version"] = "0" * 16
    if drop_provider:
        for tool in body["tools"]:
            if tool["name"] == "web_search":
                tool["config"].pop("provider", None)
    return body


class _FakeRegistryClient:
    """SOCKET 1 — the registry. Real ``_fetch_stack_component`` runs against it.

    Exposes the private surface that function uses (``_ensure_client`` /
    ``_api_prefix`` / ``_headers``) over an ``httpx.MockTransport``, so the GET,
    its 404 handling and its JSON decode are all the production code paths.
    """

    _api_prefix = "/api/v1"

    def __init__(self, *, rows: dict[str, Any] | None = None,
                 pack_body: dict[str, Any] | None = None):
        self._rows = {COMPONENT_ID: _stack_row()} if rows is None else rows
        self._pack_body = (
            _shipped_pack_body() if pack_body is None else pack_body
        )
        self.stack_gets: list[str] = []

    def _headers(self) -> dict[str, str]:
        return {}

    async def _ensure_client(self) -> httpx.AsyncClient:
        def _handle(request: httpx.Request) -> httpx.Response:
            cid = request.url.path.rsplit("/stack/", 1)[-1]
            self.stack_gets.append(cid)
            row = self._rows.get(cid)
            if row is None:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json=row)

        return httpx.AsyncClient(
            transport=httpx.MockTransport(_handle), base_url="http://registry",
        )

    async def get_descriptor(self, descriptor_id: str, family: str | None = None):
        if descriptor_id != "web_access":
            return None
        return {"body": self._pack_body}


class _FakeSearchHTTP:
    """SOCKET 2 — the provider's own GET, patched at ``_get_json``.

    Everything ABOVE the socket stays real: endpoint validation, param build,
    ``parse_searxng_payload``, the ``unresponsive_engines`` degradation read and
    the response's provider/subprovider stamping. Records the queries it was
    asked, which is how a test proves the analyst reached the provider THROUGH
    the resolved handler rather than around it.
    """

    def __init__(self, payload: Any | None = None):
        self.payload = payload if payload is not None else {
            "results": [
                {"url": "https://news.example/a", "title": "Talks collapse",
                 "content": "Officials confirmed it was never signed.",
                 "engine": "duckduckgo", "score": 1.0},
            ],
            "unresponsive_engines": [],
        }
        self.queries: list[str] = []
        self.endpoints: list[str] = []

    # Patched on as a plain class ATTRIBUTE (an instance, not a function), so
    # Python's descriptor protocol never binds it — there is no `self` here.
    async def __call__(self, endpoint, *, params, timeout):
        self.endpoints.append(endpoint)
        self.queries.append(params.get("q", ""))
        return self.payload


@pytest.fixture
def search_http(monkeypatch) -> _FakeSearchHTTP:
    from legba.data.stack.search.base import SearchProviderHandler

    rec = _FakeSearchHTTP()
    monkeypatch.setattr(SearchProviderHandler, "_get_json", rec)
    return rec


# ---------------------------------------------------------------------------
# Deps / descriptor carriers (the shapes the real wire function reads)
# ---------------------------------------------------------------------------


@dataclass
class _Ident:
    id: str = "standing_auditor"


@dataclass
class _Descriptor:
    identity: _Ident = field(default_factory=_Ident)
    action_packs: list = field(default_factory=lambda: [{"pack_id": "web_access"}])


@dataclass
class _Deps:
    pg_pool: Any = "POOL"
    extras: dict = field(default_factory=dict)
    secrets_resolve: Any = None


async def _secrets(_secret_id: str) -> bytes:
    return b""


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    """A clean handler cache + a bring-up AGENCY_HOLDER for every test.

    The holder is populated EXACTLY as ``source_first_runtime`` populates it:
    ``queue`` + ``emit`` and nothing else. That emptiness is the bug's home, so
    no test may quietly enrich it.

    Also resets the PROCESS-WIDE ``DEFAULT_LIVENESS_CACHE``
    (``legba.data.stack.search.liveness``). Every binding built in this file
    carries ``search_liveness=None`` off the bring-up context (see
    ``wire_standing_auditor_web_pack``), so ``web_search_tool`` falls back to
    that one global cache — exactly as it does in production, sharing "ONE
    control-probe budget, ONE deferral ladder" across every caller. Several
    tests here (``test_an_unresolvable_ref_binds_none_and_still_fails_loud``,
    ``test_a_missing_secrets_resolver_is_loud_not_a_silent_unbind``, ...)
    deliberately drive an unresolved ``search.searxng.local`` route through
    ``web_search_tool``, and that call records a deferral against the SAME
    key in that SAME cache via ``compute_deferral``. Under a shuffled test
    order one of those can run before
    ``test_a_search_through_the_auditor_binding_reaches_the_provider``, whose
    rung 0 is then found still "deferred" and the ladder falls through to the
    unbound ``search.serper.paid`` fallback rung, failing with
    ``search_provider_unresolved`` — a real search-through-the-provider test
    that never touched the paid rung at all. Resetting the cache on both
    sides of every test in this file (the same convention
    ``test_search_provider_ladder.py`` / ``test_search_liveness_and_deferral.py``
    / ``test_standing_auditor.py`` already use) makes every test here own its
    own precondition regardless of what ran before it.
    """
    from legba.data.analysts.agency import Agency, ToolContext
    from legba.data.stack.search.liveness import DEFAULT_LIVENESS_CACHE
    from legba.runtime import search_handler_factory as shf
    from legba.runtime.source_first_runtime import AGENCY_HOLDER

    shf.clear_search_handler_cache()
    DEFAULT_LIVENESS_CACHE.reset()
    saved = dict(AGENCY_HOLDER)
    AGENCY_HOLDER["agency"] = Agency()
    AGENCY_HOLDER["tool_context"] = ToolContext(queue=object(), emit=object())
    yield
    AGENCY_HOLDER.clear()
    AGENCY_HOLDER.update(saved)
    shf.clear_search_handler_cache()
    DEFAULT_LIVENESS_CACHE.reset()


async def _wire(registry: _FakeRegistryClient, *, secrets=_secrets):
    """Run the REAL production wiring and return its binding (or None)."""
    from legba.runtime.external_audit_binding import wire_standing_auditor_web_pack
    from legba.data.analysts.deterministic_handlers.standing_auditor import (
        WEB_BINDING_DEPS_EXTRA_KEY,
    )

    deps = await wire_standing_auditor_web_pack(
        _Descriptor(), _Deps(secrets_resolve=secrets), registry_client=registry,
    )
    return deps.extras.get(WEB_BINDING_DEPS_EXTRA_KEY)


# ---------------------------------------------------------------------------
# 1) The regression: rung 1 resolves in the REAL binding path
# ---------------------------------------------------------------------------


async def test_the_auditor_binding_resolves_the_registered_component():
    """#85 head-on: the shipped ref must become a CONFIGURED handler.

    Before the fix this bound ``None`` with the component registered, active and
    healthy — the whole outage in one assertion.
    """
    registry = _FakeRegistryClient()
    binding = await _wire(registry)

    assert binding is not None
    ctx = binding.tool_context
    assert ctx.search is not None, (
        "the auditor's ToolContext bound NO provider though the shipped pack "
        "declares config.provider -> search.searxng.local (this is #85)"
    )
    # Resolved, not merely constructed: the family, subprovider and endpoint all
    # came off the registry row through the real builder.
    assert ctx.search.subprovider == "searxng"
    assert ctx.search.component_id == COMPONENT_ID
    assert ctx.search._cfg.endpoint.raw == SEARXNG_ENDPOINT
    # The route travels with the handler so a finding can record WHICH provider
    # introduced which claim.
    assert ctx.search_route.component_id == COMPONENT_ID
    assert ctx.search_route.source == "config.provider"
    # It really went to the registry for it.
    # rung 0 first, once; the serper fallback rung is looked up too (404 here —
    # it binds nothing and the primary still resolves)
    assert registry.stack_gets[0] == COMPONENT_ID
    assert registry.stack_gets.count(COMPONENT_ID) == 1
    assert set(registry.stack_gets) <= {COMPONENT_ID, SERPER_ID}


async def test_the_bringup_context_carries_no_provider_to_copy():
    """The CAUSE, pinned so it cannot silently return.

    ``source_first_runtime`` builds the process-wide ToolContext with queue +
    emit only. Any binding that obtains its provider by COPYING that context is
    dead on arrival — which is exactly how the auditor shipped.
    """
    from legba.runtime.source_first_runtime import AGENCY_HOLDER

    base = AGENCY_HOLDER["tool_context"]
    assert base.search is None and base.search_route is None

    # The wiring must therefore RESOLVE, not copy.
    binding = await _wire(_FakeRegistryClient())
    assert binding.tool_context.search is not None


async def test_a_search_through_the_auditor_binding_reaches_the_provider(
    search_http,
):
    """End of the leg: the REAL web_search tool, over the REAL resolved handler.

    Only the provider's socket is faked, so a result here means route
    resolution, component fetch, handler configuration and the tool's ladder all
    worked — the exact chain that produced ``provider_unresolved`` in prod.
    """
    from legba.data.analysts.agency.tools import ToolCall
    from legba.data.analysts.agency.web_tools import web_search_tool

    binding = await _wire(_FakeRegistryClient())
    result = await web_search_tool(
        ToolCall(pack_id="web_access", tool_name="web_search",
                 args={"query": "was the agreement signed"}),
        binding.pack, binding.tool_context,
    )

    assert result.status == "completed", result.error
    assert search_http.queries == ["was the agreement signed"]
    assert search_http.endpoints == [SEARXNG_ENDPOINT]
    assert result.output["results"][0]["url"] == "https://news.example/a"
    # Provenance: the response is stamped with the component that answered.
    assert result.output["provider"] == COMPONENT_ID


async def test_both_binding_sites_resolve_through_the_one_factory():
    """The GATHER path and the auditor path must share ONE resolver.

    The predecessor of this test grepped ``dapr_host`` alone for the binding
    site. That is how #85 shipped: a SECOND binding site existed
    (``external_audit_binding``, for the deterministic auditor) and no test
    asked whether IT resolved anything. Guard the seam, not one address.
    """
    from pathlib import Path

    import legba.runtime.dapr_host as dapr_host
    import legba.runtime.external_audit_binding as eab

    for mod in (dapr_host, eab):
        text = Path(mod.__file__).with_suffix(".py").read_text()
        assert "search_handler_factory" in text, (
            f"{mod.__name__} binds ToolContext.search without going through the "
            "shared resolver — the #85 divergence"
        )
    host_text = Path(dapr_host.__file__).with_suffix(".py").read_text()
    assert "search=_search_handler" in host_text
    assert "search_route=_search_route" in host_text
    eab_text = Path(eab.__file__).with_suffix(".py").read_text()
    assert "search=search_handler" in eab_text
    assert "search_route=search_route" in eab_text


async def test_the_two_sites_share_one_handler_instance():
    """"ONE provider, ONE control-probe budget, ONE deferral ladder" — real.

    ``external_audit_binding`` has always CLAIMED this in a comment. It is true
    only because both sites resolve through the shared factory's cache.
    """
    from legba.runtime.search_handler_factory import resolve_search_handler

    registry = _FakeRegistryClient()
    binding = await _wire(registry)
    # The GATHER path's factory call, same component id.
    gather_handler = await resolve_search_handler(
        COMPONENT_ID, registry_client=registry, secrets_resolve=_secrets,
    )
    assert gather_handler is binding.tool_context.search
    # And it only paid the registry once.
    # rung 0 first, once; the serper fallback rung is looked up too (404 here —
    # it binds nothing and the primary still resolves)
    assert registry.stack_gets[0] == COMPONENT_ID
    assert registry.stack_gets.count(COMPONENT_ID) == 1
    assert set(registry.stack_gets) <= {COMPONENT_ID, SERPER_ID}


# ---------------------------------------------------------------------------
# 2) The other rungs, unbroken
# ---------------------------------------------------------------------------


async def test_an_unresolvable_ref_binds_none_and_still_fails_loud(search_http):
    """Rung 4. A DECLARED route the runtime cannot build must NEVER look like
    an empty web: no query is issued and the failure names the component."""
    from legba.data.analysts.agency.tools import ToolCall
    from legba.data.analysts.agency.web_tools import web_search_tool

    registry = _FakeRegistryClient(rows={})  # registry 404s the component
    binding = await _wire(registry)

    ctx = binding.tool_context
    assert ctx.search is None
    # The ROUTE survives — that is what lets the tool name the gap honestly.
    assert ctx.search_route.component_id == COMPONENT_ID

    result = await web_search_tool(
        ToolCall(pack_id="web_access", tool_name="web_search",
                 args={"query": "anything"}),
        binding.pack, ctx,
    )
    assert result.status == "failed"
    assert "search_provider_unresolved" in result.error
    assert COMPONENT_ID in result.error or SERPER_ID in result.error
    assert "NO query was issued" in result.error
    assert result.output["deferral"]["reason"] == "search_provider_unresolved"
    # It really issued nothing.
    assert search_http.queries == []


async def test_a_missing_secrets_resolver_is_loud_not_a_silent_unbind(caplog):
    """A caller that lost its secrets plumbing must not be indistinguishable
    from an operator who never registered the component."""
    import logging

    with caplog.at_level(logging.ERROR):
        binding = await _wire(_FakeRegistryClient(), secrets=None)

    assert binding.tool_context.search is None
    assert binding.tool_context.search_route.component_id == COMPONENT_ID
    assert any("no_secrets_resolver" in r.message for r in caplog.records)


async def test_rung3_survives_when_the_pack_declares_no_provider(monkeypatch):
    """Rung 0/3. No ``provider`` key ⇒ no route ⇒ nothing bound ⇒ web_search
    falls through to the operator-pinned endpoint. The env rung must keep
    working exactly as before — this fix must not have made rung 1 mandatory."""
    from legba.data.analysts.agency.tools import ToolCall
    from legba.data.analysts.agency.web_tools import web_search_tool
    from legba.data.stack.search.base import SearchProviderHandler

    registry = _FakeRegistryClient(
        pack_body=_shipped_pack_body(drop_provider=True),
    )
    binding = await _wire(registry)

    assert binding.tool_context.search is None
    assert binding.tool_context.search_route is None
    # No route ⇒ the resolver never touched the registry for a component.
    assert registry.stack_gets == []

    rec = _FakeSearchHTTP()
    monkeypatch.setattr(SearchProviderHandler, "_get_json", rec)
    monkeypatch.setenv("LEGBA_WEB_SEARCH_ENDPOINT", "https://searx.operator/search")

    result = await web_search_tool(
        ToolCall(pack_id="web_access", tool_name="web_search",
                 args={"query": "legacy rung"}),
        binding.pack, binding.tool_context,
    )
    assert result.status == "completed", result.error
    assert rec.endpoints == ["https://searx.operator/search"]


async def test_rung2_a_runtime_bound_provider_survives_with_no_ref():
    """Rung 2. A provider the runtime bound with NO descriptor ref still wins
    when the pack declares none — the bring-up context stays a real source."""
    from legba.runtime.source_first_runtime import AGENCY_HOLDER

    sentinel = object()
    AGENCY_HOLDER["tool_context"].search = sentinel

    registry = _FakeRegistryClient(
        pack_body=_shipped_pack_body(drop_provider=True),
    )
    binding = await _wire(registry)
    assert binding.tool_context.search is sentinel


async def test_a_declared_ref_beats_a_runtime_bound_provider():
    """Ladder ORDER: rung 1 outranks rung 2. An operator who points the ToolSpec
    at a component means it, even if something else was bound at bring-up."""
    from legba.runtime.source_first_runtime import AGENCY_HOLDER

    AGENCY_HOLDER["tool_context"].search = object()
    binding = await _wire(_FakeRegistryClient())
    assert binding.tool_context.search.component_id == COMPONENT_ID


async def test_a_route_pointed_at_the_wrong_family_binds_none(caplog):
    """The family check is load-bearing (``expected_family`` on the StackRef is
    documentation only). An llm component behind a search ref must not build."""
    import logging

    registry = _FakeRegistryClient(rows={
        COMPONENT_ID: _stack_row(schema_uri="legba/stack/llm_provider/1.0.0"),
    })
    with caplog.at_level(logging.ERROR):
        binding = await _wire(registry)

    assert binding.tool_context.search is None
    assert any("search_handler_factory.unresolved" in r.message
               for r in caplog.records)


async def test_a_failure_is_never_cached_so_a_late_registration_heals():
    """#235's lesson on this leg: a component registered minutes after boot must
    heal on the NEXT deps build, not the next container recreate."""
    empty = _FakeRegistryClient(rows={})
    assert (await _wire(empty)).tool_context.search is None

    # The operator registers it; the very next deps build must pick it up.
    healed = _FakeRegistryClient()
    assert (await _wire(healed)).tool_context.search is not None


async def test_the_grant_leg_still_gates_the_binding():
    """A descriptor that does not grant web_access gets no binding at all —
    the fix must not have turned resolution into an implicit grant."""
    from legba.runtime.external_audit_binding import wire_standing_auditor_web_pack
    from legba.data.analysts.deterministic_handlers.standing_auditor import (
        WEB_BINDING_DEPS_EXTRA_KEY,
    )

    descriptor = _Descriptor(action_packs=[])
    registry = _FakeRegistryClient()
    deps = await wire_standing_auditor_web_pack(
        descriptor, _Deps(secrets_resolve=_secrets), registry_client=registry,
    )
    assert WEB_BINDING_DEPS_EXTRA_KEY not in deps.extras
    assert registry.stack_gets == []


async def test_the_shipped_descriptor_still_declares_the_rung1_ref():
    """If an operator removes the ref, the auditor goes quiet by DESIGN — but it
    should be a deliberate edit, not a silent drift. Pin the shipped state."""
    cfg = next(
        t["config"] for t in _shipped_pack_body()["tools"]
        if t["name"] == "web_search"
    )
    assert cfg["provider"]["raw"] == COMPONENT_ID
    assert cfg["provider"]["factory_kind"] == "stack_ref"
