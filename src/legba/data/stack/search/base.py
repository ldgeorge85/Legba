# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search-provider stack-component handler base — the DISCOVERY leg.

Legba's *retrieval* and *extraction* legs are already built (``web_fetch`` →
the evidence archive → Trafilatura). What was missing is **discovery**: going
from a question to a list of candidate URLs. This package is that leg, modelled
as a stack-component family exactly like ``stack/llm`` — one handler per
subprovider behind a component id, selected by a descriptor ``StackRef``
(``factory_kind: stack_ref``), never by sniffing a string.

The module defines:

  * :class:`SearchResult` / :class:`SearchResponse` — the NORMALIZED result
    schema every provider maps onto (see :mod:`.searxng` for the field map).
  * :class:`SearchStatus` — the four caller-visible outcomes, and the reason
    this package exists at all (below).
  * :class:`FetchedDocument` — the OPTIONAL fetch/extract return shape.
  * :class:`SearchProviderHandler` — the base class; concrete subproviders
    override :meth:`_build_params` / :meth:`_parse_payload` only.
  * :class:`TransientSearchFailure` / :class:`HardSearchFailure` — the typed
    failure split, mirroring ``TransientLLMFailure`` / ``HardLLMFailure``.

TWO ABSOLUTE RULES FOR EVERY HANDLER
------------------------------------
1. **Egress only through** :func:`legba.data.sources._egress.guarded_async_client`.
   No bare ``httpx`` client anywhere in this package. Result URLs are
   attacker-influenceable in a way operator-authored descriptor URLs were not,
   so the same SSRF guard every ingress fetcher uses bounds where a search — and
   every follow-on fetch — can land, re-checked on every redirect hop.
2. **An empty result list is SUSPECT until liveness is MEASURED.** ``results ==
   []`` on its own means "we got nothing back" — which over a multi-engine
   meta-search is far more likely to mean BROKEN than to mean the web is empty.
   A handler still never uses ``[]`` to signal a failure it knows about (that
   either raises or sets ``degraded``), but a clean-looking empty is NOT
   promoted to absence until :mod:`.liveness` proves the engine set answers.

WHY RULE 2 IS THE POINT (the failure this package exists to prevent)
--------------------------------------------------------------------
A meta-search instance forwards queries to upstream engines that classify it as
a bot and CAPTCHA / rate-limit / ban it. A banned engine is silently dropped
from the merge and listed in ``unresponsive_engines``; the HTTP response is
still **200** with a shorter — or completely empty — ``results[]``.

A verified live probe of the deployed instance returned HTTP 200 with 20
results while ``unresponsive_engines`` reported ``brave: too many requests``,
``duckduckgo: CAPTCHA``, ``startpage: CAPTCHA``. Had every engine refused, the
same 200 would have carried an EMPTY ``results[]`` — byte-indistinguishable
from "nothing about this exists on the web".

For an intelligence platform that is the worst failure available: it
manufactures FALSE ABSENCE EVIDENCE. An analyst reading a bare empty list can
write "no reporting exists on X" when the truth is "we were blocked".

Reading ``unresponsive_engines`` catches only the case where the provider KNOWS
it served partial results. It cannot catch an empty engine set, a query-encoding
bug, or an upstream that answers 200 + ``[]`` with no error field — all of which
are byte-identical to true absence. So the honest default is that an empty is
UNVERIFIED, and absence is MEASURED by a bounded control probe
(:func:`legba.data.stack.search.liveness.verify_engine_liveness`). The
distinction is carried in the type system, not in a comment:

  * :attr:`SearchStatus.EMPTY` — ran fully, found nothing, engine-set liveness
    NOT measured. SUSPECT. An absence claim is NOT admissible.
  * :attr:`SearchStatus.EMPTY_VERIFIED` — found nothing AND a control probe
    proved the engine set is answering. This licenses a SCOPED absence only
    ("these engines returned nothing for this query"), never "X does not exist".
  * :attr:`SearchStatus.DEGRADED_EMPTY` — partial service (or a dead/failed
    control probe), found nothing. UNKNOWN. Never admissible.

:attr:`SearchResponse.supports_absence_claim` is the one predicate callers ask,
and it is True for exactly one of those four.

EXTENSION POINTS — adding a provider without touching a caller
---------------------------------------------------------------
A handler advertises :attr:`~SearchProviderHandler.capabilities`, a subset of
``{"search", "fetch", "extract"}``. ``"search"`` is REQUIRED of every handler;
the other two are ADVERTISED, never sniffed. That is the whole extension seam:

* **A fetch-and-extract provider** (Firecrawl-style ``POST /search`` returning
  clean markdown per hit; a Jina-Reader-style ``r.jina.ai``/``s.jina.ai`` URL
  prefix) declares ``capabilities = {"search", "fetch", "extract"}``, fills
  :attr:`SearchResult.extracted_text` with the text the provider genuinely
  returned, stamps :attr:`SearchResult.extract_source` = ``"provider"``, and
  implements :meth:`~SearchProviderHandler.fetch`. Callers that only want URLs
  are unaffected; callers that want text check ``extracted_text is not None``
  and otherwise fall through to the existing ``web_fetch`` → evidence-archive →
  Trafilatura path. ``extracted_text`` is NEVER synthesized from a snippet —
  ``None`` means "this provider returned no clean text", not "no text exists".
* **An agentic searcher** (``search.agent.<name>``, ``subprovider =
  "agent_<name>"``) is just another subprovider: internally it may plan, issue
  many sub-queries, follow links and call an LLM, and none of that leaks into
  the contract — one ``search()`` in, one :class:`SearchResponse` out. Three
  constraints are NOT optional for such a handler:
    1. **It returns documents, never conclusions.** Every ``SearchResult.url``
       must be a URL the agent actually retrieved. An agent returning
       synthesized prose with post-hoc citations launders an unjudged LLM
       answer into a lineage that must be verifiable to source bytes — and it
       defeats the faithfulness verify leg by handing it text that was never
       grounded in the cited bytes.
    2. **Its own inference resolves through the stack registry** (``llm.*``
       component ids), never a private client — otherwise its tokens sit
       outside budget accounting, the temperature policy and the model-plane
       rules.
    3. **:attr:`SearchResult.raw` carries the trajectory** (queries issued,
       pages visited, in order) so a finding's provenance can reconstruct how a
       URL was reached. An agent that cannot explain its path is not admissible
       evidence provenance.
  Being orders of magnitude slower and costlier per call, an agent provider
  must be selected by an EXPLICIT descriptor ref — never a global default — and
  its governor cap must count agent invocations, not internal sub-queries.

Adding either means: one new module here, one line in ``SEARCH_HANDLERS``, one
``COMPONENTS`` entry in ``scripts/bringup_register_stack.py``. No caller edits.

HEALTHCHECK DISCIPLINE
----------------------
:meth:`~SearchProviderHandler.health_check` does TCP reachability only — it must
never burn a real query per poll (the same rule the LLM family states about
tokens; here the cost is upstream-engine goodwill, which is what keeps the
instance unbanned). Note honestly that this check reports HEALTHY while every
upstream engine is banned, because the service genuinely IS up. Detecting THAT
state is :func:`legba.data.stack.search.liveness.verify_engine_liveness` — a
fixed control query with a known-nonzero expected count, cached per moment,
issued on demand when a search comes back empty and callable on a cadence with
``force=True``. That is ONE organ, not a healthcheck extension and not a second
canary.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Mapping

import httpx
from pydantic import BaseModel, Field

from ...registry.health import HealthState, StackComponentHealth
from ...schemas.stack import SearchProviderConfig
from ...sources._egress import EgressBlockedError, guarded_async_client

logger = logging.getLogger(__name__)


#: Field caps — DELIBERATELY the ones ``web_tools._parse_search_results`` has
#: always applied, so re-pointing the legacy tool at this layer is lossless.
MAX_TITLE_CHARS = 512
MAX_SNIPPET_CHARS = 1024

#: Absolute ceiling on results returned to a caller, whatever it asks for.
MAX_RESULTS_CAP = 10

DEFAULT_TIMEOUT_SECONDS = 15.0

USER_AGENT = "legba-search/1.0 (+https://github.com/ldgeorge85/legba)"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Typed failures (mirrors stack/llm/base.py)
# ---------------------------------------------------------------------------


class TransientSearchFailure(Exception):
    """Timeout / 429 / upstream 5xx / a suspended engine set.

    Retryable, and the ONE case in which a caller may fall through to a
    declared fallback provider — once, never in a loop.
    """

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class HardSearchFailure(Exception):
    """Misconfiguration, auth failure, non-JSON body, unknown subprovider.

    Never retried, never silently swallowed — surfaced to the caller.
    """

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class SearchProviderUnresolved(HardSearchFailure):
    """No provider could be resolved for a search route.

    A subclass of :class:`HardSearchFailure` on purpose: an unresolved provider
    is a CLEAN, LOUD failure naming the seam — never an empty result list. The
    honest-degradation contract when nothing is configured is "say so", not
    "return nothing found".
    """


# ---------------------------------------------------------------------------
# The normalized result schema
# ---------------------------------------------------------------------------


class SearchStatus(str, Enum):
    """The five caller-visible outcomes of a search.

    The ``DEGRADED_EMPTY`` / ``EMPTY`` / ``EMPTY_VERIFIED`` split is the whole
    reason this enum exists — see the module docstring.
    """

    #: Provider ran fully; at least one result.
    OK = "ok"
    #: Provider ran fully; zero results; engine-set liveness NOT measured.
    #: SUSPECT, not absence — over a multi-engine meta-search a genuinely empty
    #: result set is close to impossible, so this shape is much more likely to
    #: mean the plane is broken. Never citable.
    EMPTY = "empty"
    #: Zero results AND a control probe proved the engine set is answering.
    #: The empty is real FOR THIS QUERY: a SCOPED absence statement ("these
    #: engines returned nothing for this query") is admissible; "X does not
    #: exist" is not.
    EMPTY_VERIFIED = "empty_verified"
    #: Provider admitted PARTIAL service; results present but incomplete.
    DEGRADED = "degraded"
    #: Provider admitted PARTIAL service AND returned nothing — or a control
    #: probe found the engine set dead/unverifiable. Absence is UNKNOWN — never
    #: renderable as "no evidence exists".
    DEGRADED_EMPTY = "degraded_empty"


class LivenessVerdict(str, Enum):
    """Whether the provider's ENGINE SET was measured to be answering.

    Set by :func:`legba.data.stack.search.liveness.verify_engine_liveness`,
    which is the single control-probe code path (it also subsumes the
    low-cadence canary). Only :attr:`LIVE` can promote an empty result set to a
    scoped absence.
    """

    #: No probe ran. The default for every raw provider response.
    UNVERIFIED = "unverified"
    #: A fixed high-yield control query returned results through this provider.
    LIVE = "live"
    #: The control query ALSO returned zero results. The plane is broken.
    DEAD = "dead"
    #: The control query could not be issued (transient/hard failure). Liveness
    #: is UNVERIFIABLE, which licenses exactly as much as DEAD: nothing.
    PROBE_FAILED = "probe_failed"


class SearchResult(BaseModel):
    """One normalized hit. Provider-native extras survive in :attr:`raw`."""

    url: str
    title: str = ""
    snippet: str = ""
    published_at: str | None = None
    #: Upstream engine that produced the hit (meta-search), else the provider.
    engine: str | None = None
    #: Provider-native relevance. NOT comparable across providers.
    score: float | None = None
    #: 1-based position in the provider's own ordering, after our cap.
    rank: int = 0
    #: Clean main text IFF the provider genuinely returned it. Never
    #: synthesized from ``snippet``.
    extracted_text: str | None = None
    #: ``"provider"`` when ``extracted_text`` came off the wire, else ``None``.
    #: Never guessed.
    extract_source: str | None = None
    #: Always ``None`` off a search provider: a search hit arrives from an
    #: unbounded, unreviewed domain set with no license verdict. Populating
    #: this from a guess would defeat the retention gate that reads it.
    license_class: str | None = None
    #: The provider's item, unmodified.
    raw: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    """One normalized search response.

    ``degraded`` is the answer to silent partial failure: a provider that KNOWS
    it served a partial result says so here, and the flag propagates all the way
    into the tool output the planner reads.
    """

    query: str
    results: list[SearchResult] = Field(default_factory=list)
    #: Resolved component id (e.g. ``search.searxng.local``) — stamped into
    #: provenance so it is later auditable WHICH provider introduced a claim.
    #: ``legacy:<env-or-config>`` for the pre-family operator-pinned endpoint.
    provider: str = ""
    subprovider: str = ""
    #: The provider admitted PARTIAL service.
    degraded: bool = False
    #: Human-readable reason, e.g. ``"unresponsive_engines: brave, duckduckgo"``.
    degraded_detail: str = ""
    #: Engines the provider reported as unresponsive, normalized to strings.
    unresponsive_engines: list[str] = Field(default_factory=list)
    #: Whether the engine set was MEASURED to be answering. Stamped by
    #: :func:`legba.data.stack.search.liveness.apply_liveness` after a control
    #: probe; ``UNVERIFIED`` on every raw provider response. This is the field
    #: that keeps a broken search from manufacturing false absence evidence.
    liveness: LivenessVerdict = LivenessVerdict.UNVERIFIED
    #: Human-readable probe outcome, e.g. ``"control probe 'united nations'
    #: returned 3 result(s) — the engine set is answering…"``.
    liveness_detail: str = ""
    retrieved_at: datetime = Field(default_factory=_now)

    @property
    def count(self) -> int:
        return len(self.results)

    @property
    def status(self) -> SearchStatus:
        if self.degraded:
            return (
                SearchStatus.DEGRADED_EMPTY if not self.results
                else SearchStatus.DEGRADED
            )
        if self.results:
            return SearchStatus.OK
        # Zero results with no admitted degradation. Which of the two empties
        # this is depends ENTIRELY on whether liveness was measured — an
        # unmeasured empty is suspect, not absence.
        return (
            SearchStatus.EMPTY_VERIFIED
            if self.liveness == LivenessVerdict.LIVE
            else SearchStatus.EMPTY
        )

    @property
    def supports_absence_claim(self) -> bool:
        """True ONLY for a zero-result search whose engine set was PROVEN live.

        The single predicate a caller asks before writing any absence
        statement — and even then the licensed statement is SCOPED (see
        :attr:`absence_statement`), never "no reporting on X exists".

        False for:
          * every degraded response, including one that DID return hits (the
            missing engines could have carried the contradicting evidence);
          * every UNVERIFIED empty — the common shape when the plane is broken.
        """
        return self.status is SearchStatus.EMPTY_VERIFIED

    @property
    def absence_statement(self) -> str:
        """The ONLY absence phrasing this response licenses (else ``""``).

        Deliberately scoped to the measured fact — which engines, which query,
        at which moment. The unscoped claim ("there is no reporting on X") is
        never licensed by a search result, because a search covers the engines
        that answered, not the world.
        """
        if not self.supports_absence_claim:
            return ""
        return (
            f"These search engines returned no results for the query "
            f"{self.query!r} at {self.retrieved_at.isoformat()}, and a control "
            "probe confirmed the engine set was answering at that moment. That "
            "is a SCOPED absence — it does NOT establish that no such "
            "reporting or subject exists."
        )

    def to_tool_output(self) -> dict[str, Any]:
        """The flat dict the ``web_search`` ToolResult carries.

        ``title`` / ``url`` / ``snippet`` per hit are byte-compatible with the
        pre-family shape; every honesty field is ADDITIVE, so an existing
        consumer keeps working and a new one can see the degradation.
        """
        return {
            "query": self.query,
            "results": [
                {
                    "title": r.title,
                    "url": r.url,
                    "snippet": r.snippet,
                    "engine": r.engine,
                    "rank": r.rank,
                    **({"extracted_text": r.extracted_text}
                       if r.extracted_text is not None else {}),
                }
                for r in self.results
            ],
            "count": self.count,
            "provider": self.provider,
            "subprovider": self.subprovider,
            "retrieved_at": self.retrieved_at.isoformat(),
            "status": self.status.value,
            "degraded": self.degraded,
            "degraded_detail": self.degraded_detail,
            "unresponsive_engines": list(self.unresponsive_engines),
            "liveness": self.liveness.value,
            "liveness_detail": self.liveness_detail,
            "supports_absence_claim": self.supports_absence_claim,
            "absence_statement": self.absence_statement,
            "absence_warning": self._absence_warning(),
        }

    def _absence_warning(self) -> str:
        """The warning text a planner reads when absence is NOT claimable."""
        if self.results or self.supports_absence_claim:
            return ""
        if self.status is SearchStatus.DEGRADED_EMPTY:
            return (
                "Search returned no results while the search plane reported "
                "PARTIAL service or failed a liveness probe — this is UNKNOWN, "
                "not absence. Do NOT write that no evidence exists."
            )
        return (
            "Search returned no results and engine-set liveness was NOT "
            "verified. Over a multi-engine meta-search a genuinely empty "
            "result set is close to impossible, so this shape usually means "
            "BROKEN, not absent. This is UNKNOWN. Do NOT write that no "
            "evidence exists."
        )


class FetchedDocument(BaseModel):
    """Return shape of the OPTIONAL ``fetch`` capability."""

    url: str
    status_code: int | None = None
    content_type: str = ""
    text: str = ""
    #: Clean main text IFF the provider extracted it. Never synthesized.
    extracted_text: str | None = None
    extract_source: str | None = None
    truncated: bool = False
    retrieved_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Normalization helpers shared by every handler
# ---------------------------------------------------------------------------


def clamp_title(value: Any) -> str:
    return str(value or "")[:MAX_TITLE_CHARS]


def clamp_snippet(value: Any) -> str:
    return str(value or "")[:MAX_SNIPPET_CHARS]


def coerce_engine_names(value: Any) -> list[str]:
    """Normalize a provider's unresponsive/degradation list to plain strings.

    Tolerates every shape seen in the wild: ``["brave", …]``,
    ``[["brave", "too many requests"], …]`` (the SearXNG JSON shape),
    ``[["brave", "CAPTCHA", true], …]`` and ``[{"engine": …, "error": …}]``.
    An unparseable member is stringified rather than dropped — a degradation
    signal must never be lost to a shape surprise.
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        text = value.decode() if isinstance(value, bytes) else value
        return [text] if text else []
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return [str(value)]
    out: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            engine = item.get("engine") or item.get("name") or ""
            reason = item.get("error") or item.get("reason") or ""
            out.append(f"{engine}: {reason}".strip(": ") if engine or reason
                       else str(item))
        elif isinstance(item, (list, tuple)):
            parts = [str(p) for p in item if p not in (None, "", True, False)]
            out.append(": ".join(parts) if parts else str(item))
        elif item:
            out.append(str(item))
    return [s for s in out if s]


# ---------------------------------------------------------------------------
# Base handler
# ---------------------------------------------------------------------------


def _split_endpoint(endpoint: str, default_port: int) -> tuple[str | None, int, str]:
    """Parse ``scheme://host:port[/path]`` → ``(host, port, scheme)``."""
    if not endpoint:
        return None, default_port, "https"
    scheme = "https"
    rest = endpoint
    if "://" in endpoint:
        scheme, rest = endpoint.split("://", 1)
    rest = rest.split("/", 1)[0].split("?", 1)[0]
    if ":" in rest:
        host, _, port_str = rest.partition(":")
        try:
            port = int(port_str)
        except ValueError:
            port = default_port
    else:
        host = rest
        port = 80 if scheme == "http" else 443 if scheme == "https" else default_port
    return (host or None), port, scheme


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class SearchProviderHandler:
    """Base class for search-provider stack-component handlers.

    Mirrors :class:`legba.data.stack.llm.base.LLMProviderHandler` — the richest
    in-repo expression of the stack-component handler contract (kind / family /
    schema_version / config_schema / handler_version + lifecycle hooks +
    ``health_check`` + ``telemetry``), plus the search-specific
    :meth:`search` / :meth:`fetch` surface.

    Subclasses set :attr:`subprovider` + :attr:`capabilities` and implement
    :meth:`_build_params` and :meth:`_parse_payload`. Everything else — egress,
    status handling, the typed failure split, health — lives here so a new
    provider cannot accidentally re-introduce silent degradation.
    """

    # ---- stack-component classvars ---------------------------------------
    kind: ClassVar[str] = "search_provider"
    family: ClassVar[str] = "stack"
    schema_version: ClassVar[str] = "legba/stack.search_provider/1-0-0"
    config_schema: ClassVar[type] = SearchProviderConfig
    handler_version: ClassVar[str] = "0.1.0"

    #: Explicit, looked up in ``SEARCH_HANDLERS`` — NEVER inferred from the
    #: component id or the endpoint host. (A deliberate departure from
    #: ``infer_llm_subprovider``'s six-rung string ladder, which exists to
    #: tolerate legacy ids and is a standing source of surprise.)
    subprovider: ClassVar[str] = "base"

    #: Advertised, never sniffed. Subset of ``{"search", "fetch", "extract"}``;
    #: ``"search"`` is required of every handler.
    capabilities: ClassVar[frozenset[str]] = frozenset({"search"})

    default_port: ClassVar[int] = 443

    def __init__(self) -> None:
        self._cfg: SearchProviderConfig | None = None
        self._instance_id: str = ""
        self._instance_version: str = ""
        self._tel: Any | None = None
        self._api_key: str | None = None

    # ---- identity ---------------------------------------------------------

    @property
    def component_id(self) -> str:
        return self._instance_id

    def telemetry(self) -> Any:
        return self._tel if self._tel is not None else _NoopTelemetry()

    # ---- lifecycle --------------------------------------------------------

    async def on_configure(self, ctx: Any) -> None:
        """Bind the parsed :class:`SearchProviderConfig`. Idempotent.

        A handler bound to a METERED provider must hard-fail here when no
        budget account is configured rather than silently billing; the
        self-hosted subproviders in this package are $0 per query and so carry
        no such gate.
        """
        cfg = self._extract_config(ctx)
        self._instance_id = getattr(ctx, "instance_id", "") or ""
        self._instance_version = getattr(ctx, "instance_version", "") or ""
        tel = getattr(ctx, "telemetry", None)
        self._tel = tel() if callable(tel) else None
        self._api_key = None
        if cfg.api_key is not None:
            secrets = getattr(ctx, "secrets", None)
            if secrets is None:
                raise HardSearchFailure(
                    f"{self.subprovider} handler: config declares api_key "
                    f"{cfg.api_key.raw!r} but the context has no credential resolver"
                )
            raw = await secrets.resolve(cfg.api_key.raw)
            self._api_key = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        self._cfg = cfg

    async def on_activate(self, ctx: Any) -> None:
        """No persistent client to open — every call opens a guarded client for
        its own lifetime (the egress guard's transport is per-client)."""
        self._require_configured()

    async def on_pause(self, ctx: Any) -> None:
        return None

    async def on_resume(self, ctx: Any) -> None:
        self._require_configured()

    async def on_retire(self, ctx: Any) -> None:
        self._api_key = None
        self._cfg = None

    async def health_check(self, ctx: Any | None = None) -> StackComponentHealth:
        """TCP reachability ONLY — never a real query (see module docstring).

        HONEST CAVEAT recorded in ``extra``: a HEALTHY verdict here means the
        service is up, NOT that its upstream engines are answering.
        """
        if self._cfg is None:
            return StackComponentHealth(
                component_id=self._instance_id or "<unconfigured>",
                kind=self.kind, state=HealthState.UNHEALTHY, checked_at=_now(),
                detail="handler not configured (call on_configure first)",
            )
        endpoint = self._cfg.endpoint.raw
        host, port, scheme = _split_endpoint(endpoint, default_port=self.default_port)
        if not host:
            return StackComponentHealth(
                component_id=self._instance_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"unparseable endpoint {endpoint!r}",
            )
        reachable = await asyncio.to_thread(_tcp_reachable, host, port)
        return StackComponentHealth(
            component_id=self._instance_id, kind=self.kind,
            state=HealthState.HEALTHY if reachable else HealthState.UNHEALTHY,
            checked_at=_now(),
            detail=f"{scheme}://{host}:{port} reachable={reachable}",
            last_success_at=_now() if reachable else None,
            extra={
                "subprovider": self.subprovider,
                "endpoint": endpoint,
                "capabilities": sorted(self.capabilities),
                "probe": "tcp_only",
                "caveat": (
                    "reachable != serving results; every upstream engine can be "
                    "banned while this probe reports healthy — the engine-health "
                    "signal is liveness.verify_engine_liveness (the control "
                    "probe), not this check"
                ),
            },
        )

    # ---- the REQUIRED domain op ------------------------------------------

    async def search(
        self, query: str, *, limit: int = 5, **opts: Any,
    ) -> SearchResponse:
        """Run one query and return the NORMALIZED response.

        Raises :class:`TransientSearchFailure` / :class:`HardSearchFailure`; it
        never returns an empty list to mean "something went wrong".
        """
        cfg = self._require_configured()
        query = str(query or "").strip()
        if not query:
            raise HardSearchFailure("search requires a non-empty query")
        endpoint = str(cfg.endpoint.raw or "").strip()
        if not (endpoint.startswith("http://") or endpoint.startswith("https://")):
            raise HardSearchFailure(
                f"{self.subprovider} endpoint must be http(s), got {endpoint!r}"
            )
        capped = max(1, min(int(cfg.max_results.raw or MAX_RESULTS_CAP),
                            MAX_RESULTS_CAP, int(limit)))
        timeout = float(cfg.timeout_seconds.raw or DEFAULT_TIMEOUT_SECONDS)
        params = self._build_params(query, limit=capped, **opts)

        payload = await self._get_json(endpoint, params=params, timeout=timeout)
        response = self._parse_payload(payload, query=query, limit=capped)
        response.provider = self._instance_id or self.subprovider
        response.subprovider = self.subprovider
        return response

    # ---- the OPTIONAL fetch capability ------------------------------------

    async def fetch(
        self, url: str, *, timeout: float | None = None,
    ) -> FetchedDocument:
        """Retrieve one URL through the provider. Gated on ``"fetch"`` in
        :attr:`capabilities`; the base refuses rather than pretending."""
        raise HardSearchFailure(
            f"{self.subprovider} does not advertise the 'fetch' capability "
            f"(advertised: {sorted(self.capabilities)})"
        )

    # ---- subclass hooks ---------------------------------------------------

    def _build_params(self, query: str, *, limit: int, **opts: Any) -> dict[str, str]:
        raise NotImplementedError

    def _parse_payload(
        self, payload: Any, *, query: str, limit: int,
    ) -> SearchResponse:
        raise NotImplementedError

    # ---- shared egress ----------------------------------------------------

    async def _get_json(
        self, endpoint: str, *, params: Mapping[str, str], timeout: float,
    ) -> Any:
        """GET + JSON-decode through the SSRF-guarded transport.

        Failure classification is the point: 429/5xx/network → transient (a
        fallback may be tried ONCE); 4xx/non-JSON/egress-blocked → hard.
        """
        headers = {"User-Agent": USER_AGENT}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with guarded_async_client(
                follow_redirects=True, timeout=timeout, headers=headers,
            ) as client:
                response = await client.get(endpoint, params=dict(params))
        except EgressBlockedError as exc:
            raise HardSearchFailure(f"egress_blocked: {exc!s}") from exc
        except (httpx.TimeoutException, httpx.ConnectError,
                httpx.RemoteProtocolError) as exc:
            raise TransientSearchFailure(f"network error: {exc!s}") from exc
        except httpx.HTTPError as exc:
            raise HardSearchFailure(f"search_failed: {exc!s}") from exc

        status = response.status_code
        if status == 429 or status >= 500:
            raise TransientSearchFailure(
                f"{self.subprovider} HTTP {status}", status=status,
            )
        if status >= 400:
            raise HardSearchFailure(
                f"{self.subprovider} HTTP {status}", status=status,
                body=response.text[:1000],
            )
        try:
            return response.json()
        except ValueError as exc:
            raise HardSearchFailure(
                f"search response not JSON: {exc!s}",
                body=response.text[:200],
            ) from exc

    # ---- helpers ----------------------------------------------------------

    def _require_configured(self) -> SearchProviderConfig:
        if self._cfg is None:
            raise HardSearchFailure(
                f"{self.subprovider} handler not configured; "
                "call on_configure() before search()"
            )
        return self._cfg

    def _extract_config(self, ctx: Any) -> SearchProviderConfig:
        cfg = getattr(ctx, "config", None)
        if cfg is None:
            cfg = getattr(ctx, "cfg", None)
        if cfg is None:
            raise HardSearchFailure(
                "HandlerContext missing `config` (SearchProviderConfig)"
            )
        if isinstance(cfg, SearchProviderConfig):
            return cfg
        if isinstance(cfg, Mapping):
            return SearchProviderConfig.model_validate(dict(cfg))
        raise HardSearchFailure(
            f"unexpected config type {type(cfg).__name__}; "
            "expected SearchProviderConfig"
        )


class _NoopTelemetry:
    def log(self, level: int, msg: str, /, **fields: Any) -> None:
        pass

    def event(self, name: str, payload: Mapping[str, Any] | None = None) -> None:
        pass

    def span(self, name: str, /, **attrs: Any):  # pragma: no cover
        return _NoopSpan()


class _NoopSpan:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FetchedDocument",
    "HardSearchFailure",
    "LivenessVerdict",
    "MAX_RESULTS_CAP",
    "MAX_SNIPPET_CHARS",
    "MAX_TITLE_CHARS",
    "SearchProviderHandler",
    "SearchProviderUnresolved",
    "SearchResponse",
    "SearchResult",
    "SearchStatus",
    "TransientSearchFailure",
    "USER_AGENT",
    "clamp_snippet",
    "clamp_title",
    "coerce_engine_names",
]
