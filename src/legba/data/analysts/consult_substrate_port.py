# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult kind's substrate-port contract — the Protocol surface.

V3/P3 extraction: this class was the ``Substrate tool ports`` section of
:mod:`legba.data.analysts.consult_on_demand`, lifted verbatim into a leaf
when the temporal-reader parameters (``as_of`` / ``since`` / ``until`` /
``believed_as_of`` / ``belief_as_of``) pushed that module past its
module-size ceiling. :mod:`.consult_on_demand` imports the class back ONE
WAY and re-exports it, so ``from .consult_on_demand import
SubstrateQueryPort`` and every ``isinstance``/type-hint use is unchanged.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Substrate tool ports
# ---------------------------------------------------------------------------


@runtime_checkable
class SubstrateQueryPort(Protocol):
    """The substrate-side tool surface the consult kind invokes.

    The runtime constructs one of these per analyst-actor activation,
    binding it to ``deps.pg_pool`` (+ optional vector store via
    ``deps.extras``).  Tests pass a hand-rolled stub that returns fixed
    rows for a known query — the LLM boundary is the test double, but the
    substrate boundary stays real per the no-mocks rule.

    Each tool returns a JSON-serializable mapping; the dispatcher folds
    that into the next ROUND's tool-result message.  The mapping includes
    a ``"refs"`` list of substrate UUIDs whenever rows were returned so
    the kind can build :attr:`ConsultResponsePayload.cited_substrate_refs`.
    """

    async def search_signals(
        self,
        *,
        query: str,
        limit: int = 20,
        scope_predicate: str | None = None,
    ) -> dict[str, Any]: ...

    async def query_facts(
        self,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        value: str | None = None,
        limit: int = 30,
        as_of: str | None = None,
    ) -> dict[str, Any]: ...

    async def inspect_entity(
        self,
        *,
        name: str,
    ) -> dict[str, Any]: ...

    async def vector_search(
        self,
        *,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]: ...

    async def search_context(
        self,
        *,
        query: str,
        corpus: str | None = None,
        country: str | None = None,
        k: int = 6,
    ) -> dict[str, Any]: ...

    # Stage 1 — the OpenSearch full-text corpus (index legba_signals_corpus):
    # BM25 lexical search over the WHOLE raw body of every ingested signal +
    # a by-id fetch of one signal's full indexed doc.
    async def search_corpus(
        self,
        *,
        query: str,
        filters: dict[str, Any] | None = None,
        size: int = 10,
    ) -> dict[str, Any]: ...

    async def read_document(
        self,
        *,
        doc_id: str,
    ) -> dict[str, Any]: ...

    async def query_nexuses(
        self,
        *,
        subject: str | None = None,
        obj: str | None = None,
        rel_type: str | None = None,
        polarity: int | None = None,
        limit: int = 30,
        as_of: str | None = None,
    ) -> dict[str, Any]: ...

    async def query_hypotheses(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
        situation_id: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]: ...

    async def get_timeline(
        self,
        *,
        subject: str,
        limit: int = 40,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]: ...

    async def compare_targets(
        self,
        *,
        target_ids: list[str],
    ) -> dict[str, Any]: ...

    async def query_paths(
        self,
        *,
        subject: str,
        obj: str,
        max_hops: int = 3,
        polarity_product: int | None = None,
        limit: int = 30,
        families: list[str] | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]: ...

    async def find_proxy_chains(
        self,
        *,
        subject: str,
        obj: str,
        max_hops: int = 3,
        polarity_product: int | None = None,
        limit: int = 30,
        families: list[str] | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]: ...

    async def query_brokers(
        self,
        *,
        camp_a: list[str],
        camp_b: list[str],
        max_hops: int = 3,
        limit: int = 50,
        families: list[str] | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]: ...

    # Finished-intelligence + navigation readers (palette expansion).
    async def list_findings(
        self,
        *,
        target_id: str | None = None,
        analyst_id: str | None = None,
        severity: str | None = None,
        since_hours: int | None = None,
        include_superseded: bool = False,
        limit: int = 20,
        believed_as_of: str | None = None,
    ) -> dict[str, Any]: ...

    async def list_situations(
        self,
        *,
        status: str | None = None,
        target_id: str | None = None,
        since_hours: int | None = None,
        limit: int = 20,
        as_of: str | None = None,
    ) -> dict[str, Any]: ...

    async def belief_as_of(
        self,
        *,
        as_of: str,
        target_id: str | None = None,
        fold_verdicts: str = "as_of",
        limit: int = 20,
    ) -> dict[str, Any]: ...

    # V3/P6 — the event surface (spec §6.1). query_events filters the
    # bounded occurrences; inspect_event is the one-event dossier (event,
    # ranked signals, actors with roles, edges, tracking situations, and the
    # lifecycle ledger).
    async def query_events(
        self,
        *,
        target_id: str | None = None,
        geo: str | list[str] | None = None,
        category: str | None = None,
        lifecycle_state: str | None = None,
        entity: str | None = None,
        since: str | None = None,
        until: str | None = None,
        as_of: str | None = None,
        include_origin: list[str] | None = None,
        situation_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]: ...

    async def inspect_event(
        self,
        *,
        event_id: str,
    ) -> dict[str, Any]: ...

    async def query_predictions(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]: ...

    async def list_targets(self, *, active_only: bool = True) -> dict[str, Any]: ...

    async def list_sources(
        self,
        *,
        active_only: bool = True,
        silent_only: bool = False,
        silent_hours: int = 48,
    ) -> dict[str, Any]: ...

    # Journal self-instrument readers (Journal Assessor Wave 1, plan §5). The
    # journal narrates over the whole organism INCLUDING ITSELF: recent
    # assessments, the graph's shape + tension, critic scores, calibration (incl.
    # the segregated acute-forecast pilot), what fired vs went quiet, source
    # health, governor pressure, and what changed since its last entry. These are
    # on the Protocol so the journal_read pack's handlers type-check against it.
    async def get_assessments(
        self,
        *,
        analyst_id: str | None = None,
        target_id: str | None = None,
        since_hours: int | None = 48,
        limit: int = 20,
    ) -> dict[str, Any]: ...

    async def get_graph_structure(self, *, limit: int = 20) -> dict[str, Any]: ...

    async def get_structural_balance(self, *, limit: int = 20) -> dict[str, Any]: ...

    async def get_critic_scores(
        self,
        *,
        analyst_id: str | None = None,
        since_hours: int | None = 168,
        limit: int = 20,
    ) -> dict[str, Any]: ...

    async def get_calibration(self) -> dict[str, Any]: ...

    # W2-T6 head coverage: the health readers default to whole-fleet limits
    # (the port clamps at its _MAX_ROW_LIMIT=200) — a 40-row default silently
    # dropped analysts/sources past the cap, world_assessor included.
    async def get_run_health(
        self,
        *,
        analyst_id: str | None = None,
        quiet_hours: int = 24,
        limit: int = 200,
    ) -> dict[str, Any]: ...

    async def get_source_health(
        self,
        *,
        silent_only: bool = False,
        silent_hours: int = 48,
        limit: int = 200,
    ) -> dict[str, Any]: ...

    async def get_budget_status(
        self,
        *,
        analyst_id: str | None = None,
        demotion_lookback_hours: int = 168,
        limit: int = 40,
    ) -> dict[str, Any]: ...

    async def get_journal_delta(
        self,
        *,
        since: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]: ...

    # 7g-2 — the COLLECTION series reads. History, not now: these two are the
    # only tools that reach `observations`, the bitemporal store a curated
    # holding loads once and nothing schedules. `since`/`until` bound the
    # VALID time (the period a number is about); `as_of` bounds the RECORD
    # time (which revision the provider had published by then).
    async def series_history(
        self,
        *,
        series_id: str,
        subject: str,
        since: Any = None,
        until: Any = None,
        as_of: Any = None,
        collection_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]: ...

    async def series_compare(
        self,
        *,
        series_id: str,
        subjects: list[str] | None = None,
        since: Any = None,
        until: Any = None,
        as_of: Any = None,
        collection_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]: ...

    # 7g-2 — the provenance census's one read: `origin_class` counts for a
    # bounded list of CITED substrate ids, taken from the rows' own column
    # rather than from which tool returned them.
    async def classify_cited_refs(
        self,
        *,
        refs: list[str],
    ) -> dict[str, Any]: ...
