# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.stack.search — search-provider stack-component handlers.

The ninth stack family, standing up alongside ``llm_provider`` /
``vector_store`` / ``embedding`` / ``nlp_service`` / ``nats`` / ``postgres`` /
``redis`` / ``proxy_pool``. Same shape as :mod:`legba.data.stack.llm`: a base
class, one module per subprovider, and a ``SEARCH_HANDLERS`` lookup so the
runtime can bind a component id to a handler with NO import side effects and NO
string sniffing.

Two handlers ship:

  * :class:`~.searxng.SearxngSearchHandler` — the deployed local metasearch
    engine (AGPL-3.0, $0/query, no key).
  * :class:`~.json_generic.GenericJsonSearchHandler` — any HTTP endpoint that
    answers a query with a compatible ``results[]``.

A Firecrawl/Jina-style fetch-and-extract provider, and an agentic searcher, are
declared extension points — see the "EXTENSION POINTS" section of
:mod:`.base`. Adding either is one module here plus one line below; no caller
changes.

Selection is by COMPONENT ID from a descriptor ``StackRef``
(``factory_kind: stack_ref``) resolved through :func:`.route.resolve_search_route`,
and the subprovider is an EXPLICIT ``config.subprovider`` value looked up in
``SEARCH_HANDLERS`` — never inferred from the id or the endpoint host.

:mod:`.liveness` carries the third piece: an empty result set is SUSPECT until
a bounded control probe proves the engine set is answering, and a failed search
yields a backing-off DEFERRAL rather than an immediate retry. That module is
also the (single) control-query canary — there is no second one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .base import (
    DEFAULT_TIMEOUT_SECONDS,
    FetchedDocument,
    HardSearchFailure,
    LivenessVerdict,
    MAX_RESULTS_CAP,
    SearchProviderHandler,
    SearchProviderUnresolved,
    SearchResponse,
    SearchResult,
    SearchStatus,
    TransientSearchFailure,
)
from .json_generic import GenericJsonSearchHandler, parse_generic_payload
from .liveness import (
    CONTROL_PROBE_QUERY,
    CONTROL_PROBE_TTL_SECONDS,
    DEFAULT_LIVENESS_CACHE,
    DeferralAdvice,
    SearchLivenessCache,
    apply_liveness,
    compute_deferral,
    deferral_from_tool_output,
    verify_engine_liveness,
)
from .route import (
    SEARCH_PROVIDER_KIND,
    SEARCH_STACK_REF_ENV,
    SearchRoute,
    assert_search_component,
    resolve_search_route,
    resolve_tool_search_route,
    stack_ref_raw,
)
from .searxng import SearxngSearchHandler, parse_searxng_payload

SEARCH_HANDLERS: dict[str, type[SearchProviderHandler]] = {
    SearxngSearchHandler.subprovider: SearxngSearchHandler,
    GenericJsonSearchHandler.subprovider: GenericJsonSearchHandler,
}


def resolve_handler(subprovider: str) -> type[SearchProviderHandler]:
    """Look up a handler class by EXPLICIT subprovider id.

    Raises :class:`KeyError` naming the known set — the same loud shape the LLM
    path uses (``AnalystDepsBuildError("… is not in LLM_HANDLERS (known: …)")``)
    so a typo in a component config fails at bind time, not at first query.
    """
    if subprovider not in SEARCH_HANDLERS:
        raise KeyError(
            f"unknown search subprovider {subprovider!r}; "
            f"known: {sorted(SEARCH_HANDLERS)}"
        )
    return SEARCH_HANDLERS[subprovider]


@dataclass
class SearchHandlerContext:
    """Minimal handler context for building a handler outside the runtime.

    Structurally compatible with the runtime's ``ConfigureContext`` slice the
    handlers actually read (``instance_id`` / ``instance_version`` / ``config``
    / ``secrets`` / ``telemetry()``), so the same handler code binds in tests,
    in the tool layer, and under the runtime.
    """

    instance_id: str
    config: Any
    instance_version: str = ""
    secrets: Any | None = None
    telemetry_handle: Any | None = field(default=None, repr=False)

    def telemetry(self) -> Any:
        return self.telemetry_handle


async def build_handler(
    component: Any, *, secrets: Any | None = None,
) -> SearchProviderHandler:
    """Build + configure a handler from a resolved ``search_provider`` component.

    ``component`` is either a :class:`legba.data.schemas.stack.SearchProvider`
    model or the equivalent mapping (``{"id", "schema_uri", "config"}``) as the
    registry serves it. Validates the FAMILY (``assert_search_component`` — the
    check ``expected_family`` does not perform) and then the SUBPROVIDER, both
    loudly.
    """
    from ...schemas.stack import SearchProvider, SearchProviderConfig

    if isinstance(component, SearchProvider):
        component_id = component.id
        schema_uri = component.schema_uri
        config: Any = component.config
    elif isinstance(component, Mapping):
        component_id = str(component.get("id") or "")
        schema_uri = str(component.get("schema_uri") or "")
        config = component.get("config")
    else:
        raise HardSearchFailure(
            f"cannot build a search handler from {type(component).__name__}"
        )
    if not component_id:
        raise HardSearchFailure("search component has no id")
    assert_search_component(schema_uri, component_id)

    if isinstance(config, Mapping):
        config = SearchProviderConfig.model_validate(dict(config))
    if not isinstance(config, SearchProviderConfig):
        raise HardSearchFailure(
            f"stack-component {component_id!r}: config is "
            f"{type(config).__name__}, expected SearchProviderConfig"
        )

    subprovider = str(config.subprovider.raw or "").strip()
    try:
        handler_cls = resolve_handler(subprovider)
    except KeyError as exc:
        raise HardSearchFailure(
            f"stack-component {component_id!r}: subprovider {subprovider!r} is "
            f"not in SEARCH_HANDLERS (known: {sorted(SEARCH_HANDLERS)})"
        ) from exc

    handler = handler_cls()
    await handler.on_configure(
        SearchHandlerContext(
            instance_id=component_id, config=config, secrets=secrets,
        )
    )
    return handler


__all__ = [
    # Base + shapes
    "SearchProviderHandler",
    "SearchProviderUnresolved",
    "SearchResponse",
    "SearchResult",
    "SearchStatus",
    "FetchedDocument",
    "HardSearchFailure",
    "TransientSearchFailure",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_RESULTS_CAP",
    # Liveness — absence is MEASURED, never assumed (one organ; also the
    # cadence canary's entry point).
    "LivenessVerdict",
    "SearchLivenessCache",
    "DEFAULT_LIVENESS_CACHE",
    "CONTROL_PROBE_QUERY",
    "CONTROL_PROBE_TTL_SECONDS",
    "verify_engine_liveness",
    "apply_liveness",
    # Deferred requeue — bounded backoff, never an immediate retry.
    "DeferralAdvice",
    "compute_deferral",
    "deferral_from_tool_output",
    # Subproviders
    "SearxngSearchHandler",
    "GenericJsonSearchHandler",
    "parse_searxng_payload",
    "parse_generic_payload",
    # Route
    "SearchRoute",
    "SEARCH_PROVIDER_KIND",
    "SEARCH_STACK_REF_ENV",
    "assert_search_component",
    "resolve_search_route",
    "resolve_tool_search_route",
    "stack_ref_raw",
    # Registry
    "SEARCH_HANDLERS",
    "SearchHandlerContext",
    "build_handler",
    "resolve_handler",
]
