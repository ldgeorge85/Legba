# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""cross_target_raw analyst kind (L-171).

Broader-substrate variant of ``inline_target``: reads raw substrate (signal
rows) across N targets that match the analyst's subscription, then runs a
single LLM-planner pass over the union, and emits a ``FindingPayload``
tagged with ``cross_target: true`` plus the contributing target_ids.

Per ``plans/design/legba_kind_contracts.md`` §5 (analyst kind contract) and
``plans/design/legba_topology_redesign.md`` §5.2:

    Reads:  raw substrate across N targets (subscription predicate selects
            them at bind time; this module accepts a resolved list of
            ``target_ids`` via ``options['target_ids']``).
    Method: LLM planner with a broader-data prompt (multi-target framing).
    Writes: ``FindingPayload`` tagged via ``data.cross_target = True`` and
            ``data.contributing_target_ids = [...]``. The actual
            ``derived_from`` UUID array is stamped by the substrate-write
            wrapper from the signal ids supplied in ``inputs``.

The module conforms to the shape declared in :mod:`legba.data.analysts`
(``KIND_NAME`` + ``run_method`` + ``build_prompt_module``). It is the
sibling of ``inline_target`` and shares the ``LLMHandlerLike`` /
``AnalystMethodResult`` types from :mod:`legba.runtime.analyst_method` —
no duplication, no rebinding.

The runtime registers kinds by walking this package at startup (per
:mod:`legba.data.analysts` docstring). For cross-target reads the runtime
must resolve the subscription predicate to a target_id list before calling
``run_method`` — see :func:`read_cross_target_slice` for a reference query.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

import asyncpg

from ..nats import SIGNALS_EXCLUDE_BACKFILL_SQL
from ..provenance.models import FindingPayload

# Share the package-canonical result+port types with the inline_target sibling
# so the runtime can dispatch either kind through the same actor wrapper. The
# sibling re-declares these locally (rather than re-using
# ``legba.runtime.analyst_method`` ones); we mirror that choice so the
# package init's ``from .inline_target import AnalystMethodResult`` and our
# own ``AnalystMethodResult`` reference the *same* class object.
from .inline_target import AnalystMethodResult, LLMHandlerLike

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

KIND_NAME: str = "cross_target_raw"

# Output kind written by the host's dispatcher for this analyst's run result.
# Read by the runtime's analyst-output dispatcher so the per-kind override
# replaces the legacy hardcoded ``OutputKind.FINDING`` constant.
from ..provenance.kinds import OutputKind as _OutputKind  # noqa: E402

OUTPUT_KIND: _OutputKind = _OutputKind.FINDING

# The kind's deps surface — pydantic could capture this but the runtime
# threads a free-form object here; a Protocol keeps the LLM port discoverable
# without coupling to StandardDeps.extras layout.


@runtime_checkable
class CrossTargetDeps(Protocol):
    """Minimum dep surface ``run_method`` needs.

    The runtime constructs this from ``StandardDeps`` (per
    ``legba.runtime.deps.StandardDeps``) — typically a small adapter that
    surfaces ``deps.extras['llm']``. A plain object with an ``llm``
    attribute satisfies it; tests use a stub.
    """

    llm: LLMHandlerLike


# ---------------------------------------------------------------------------
# Prompt module (DSPy wrapping deferred to L-176 / L-105 §2)
# ---------------------------------------------------------------------------


from ._tradecraft import with_preamble  # noqa: E402

_BROADER_DATA_SYSTEM = with_preamble(
    """TASK — cross-target synthesis. Read raw signals drawn from MULTIPLE targets (countries, sectors, threat actors) and produce ONE FINDING describing the most significant CROSS-TARGET patterns: shared adversaries, common events, contrasting trajectories, or correlated movements. Prefer observations only visible when N>1 targets are considered together — single-target observations belong in the inline_target kind. Be specific about which target_ids contribute to each claim, and cite the signal [N] behind each.
Respond with strict JSON, nothing else: {"title": "...", "body": "...", "confidence": 0.0-1.0, "evidence": ["..."], "tags": ["..."]}"""
)


PROMPT_MODULE_PATH: str = "legba.prompts.cross_target_raw.v1"
"""Dotted import path for the DSPy prompt module (Wave B prereq #4)."""


def build_prompt_module() -> Any:
    """Construct and return the DSPy module bound to this analyst kind.

    Symmetric with :func:`legba.data.analysts.inline_target.build_prompt_module`.
    Lazy-imports the module so this file imports cleanly in environments
    without dspy installed; raises :class:`ModuleNotFoundError` when
    dspy isn't available, matching the inline_target contract.

    Used by:
      * the L-176 optimizer to compile candidates against trace sets,
      * the runtime's analyst actor when DSPy is enabled (the kind's
        direct ``chat_complete`` path is still the default).
    """
    from legba.prompts.cross_target_raw.v1 import build as _build
    return _build()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_target_ids_from_inputs(inputs: Sequence[Mapping[str, Any]]) -> list[str]:
    """Walk substrate rows, collect distinct target_ids, preserve first-seen order.

    The runtime resolves the subscription's target_id list before calling
    ``run_method``, but the resolution can be stale (a target may have been
    retired between bind-time and this run). Deriving the list from the
    actual input rows is the source of truth for the finding's metadata —
    we never claim contribution from a target whose rows aren't present.
    """
    seen: dict[str, None] = {}
    for row in inputs:
        tid = row.get("target_id")
        if isinstance(tid, str) and tid and tid not in seen:
            seen[tid] = None
    return list(seen.keys())


def _render_cross_target_user_prompt(
    inputs: Sequence[Mapping[str, Any]],
    target_ids: Sequence[str],
) -> str:
    """Render the substrate slice with multi-target framing.

    Differs from ``inline_target``'s renderer by:
      * naming all contributing target_ids in the header,
      * annotating each row with its source ``target_id`` so the planner
        can attribute claims correctly,
      * sorting rows by target to make pattern-detection easier across
        a long context.

    Trims to 30 rows (cross-target slices are typically broader than
    inline_target's; the model needs a bit more breadth, but the budget
    is finite). Each row is trimmed to ~500 chars of title+snippet.
    """
    # Group rows by target_id, preserving first-seen target order.
    by_target: dict[str, list[Mapping[str, Any]]] = {tid: [] for tid in target_ids}
    for row in inputs:
        tid = row.get("target_id")
        if isinstance(tid, str) and tid in by_target:
            by_target[tid].append(row)

    header = (
        f"Cross-target slice covering {len(target_ids)} target(s): "
        f"{', '.join(target_ids) or '(none)'}.\n"
        f"Total signals: {len(inputs)}.\n\n"
    )
    body_lines: list[str] = []
    rendered = 0
    for tid in target_ids:
        rows = by_target.get(tid, [])
        if not rows:
            continue
        body_lines.append(f"=== target_id={tid} ({len(rows)} signals) ===")
        for row in rows[:10]:  # at most 10 rows per target
            if rendered >= 30:
                break
            title = str(row.get("title") or "(untitled)")[:200]
            produced_at = row.get("produced_at")
            source = row.get("source_url") or ""
            data = row.get("data")
            snippet = ""
            if isinstance(data, dict):
                snippet = (
                    data.get("summary")
                    or data.get("description")
                    or data.get("content_text")
                    or data.get("snippet")
                    or ""
                )
                if not isinstance(snippet, str):
                    snippet = str(snippet)
                snippet = snippet[:400]
            body_lines.append(
                f"  [{rendered + 1}] {title}\n"
                f"      produced_at={produced_at} source={source}\n"
                f"      snippet={snippet}"
            )
            rendered += 1
        if rendered >= 30:
            break
    return header + "\n".join(body_lines)


def _coerce_finding(
    raw: str,
    *,
    fallback_title: str,
    target_ids: Sequence[str],
) -> FindingPayload:
    """Parse the LLM JSON response into a FindingPayload tagged cross_target.

    Same fail-safe shape as the inline_target sibling: malformed responses
    land as a low-confidence FindingPayload with the raw body, so the actor
    can write *something* to substrate and the eval-loop critic can route
    the bad output via the DLQ at write time rather than the analyst
    silently dropping the run.
    """
    cross_target_meta = {
        "cross_target": True,
        "contributing_target_ids": list(target_ids),
    }

    parsed: Any
    try:
        candidate = raw.strip()
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.lower().startswith("json"):
                candidate = candidate[4:]
            candidate = candidate.strip()
        if candidate.startswith("{"):
            depth = 0
            end = len(candidate)
            for i, c in enumerate(candidate):
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            candidate = candidate[:end]
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("cross_target_raw.finding.parse_failed err=%s", exc)
        return FindingPayload(
            title=fallback_title[:200],
            body=raw[:32000],
            confidence=0.3,
            tags=["unstructured", "cross_target"],
            data={**cross_target_meta, "raw_llm_response": raw[:8000]},
        )

    if not isinstance(parsed, dict):
        return FindingPayload(
            title=fallback_title[:200],
            body=str(parsed)[:32000],
            confidence=0.3,
            tags=["unstructured", "cross_target"],
            data={**cross_target_meta, "raw_llm_response": raw[:8000]},
        )

    try:
        tags_in = [str(t) for t in (parsed.get("tags") or [])][:50]
        # Always carry the cross_target tag so downstream filters can find
        # these without parsing the JSONB data column. Idempotent if the
        # model already returned it.
        if "cross_target" not in tags_in:
            tags_in.append("cross_target")
        return FindingPayload(
            title=str(parsed.get("title") or fallback_title)[:2048],
            body=str(parsed.get("body") or "")[:65536],
            confidence=float(parsed.get("confidence", 0.5)),
            evidence=[str(e) for e in (parsed.get("evidence") or [])][:50],
            tags=tags_in,
            data={**cross_target_meta, "raw_llm_response": raw[:8000]},
        )
    except Exception as exc:
        logger.warning("cross_target_raw.finding.coerce_failed err=%s", exc)
        return FindingPayload(
            title=fallback_title[:200],
            body=raw[:32000],
            confidence=0.3,
            tags=["coerce_failed", "cross_target"],
            data={**cross_target_meta, "raw_llm_response": raw[:8000]},
        )


# ---------------------------------------------------------------------------
# Substrate-read helper — cross-target slice
# ---------------------------------------------------------------------------


async def read_cross_target_slice(
    conn: asyncpg.Connection,
    *,
    target_ids: Sequence[str],
    time_window_hours: int = 24,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Fetch a raw substrate slice across N targets.

    Mirrors the column projection + back-compat shaping of
    :func:`legba.runtime.dapr_actors._read_substrate_slice` so cross-target
    rows are interchangeable with inline_target rows at the actor layer.

    Source-first pivot (§4, migration 0024): signals are TARGET-AGNOSTIC —
    there is no ``target_id`` column. A target's slice is the union of
    signals from its subscribed ``source_id`` refs and/or its geo scope.
    We resolve each requested target's ``source_id`` refs + geo from
    ``target_descriptors``, union them across all requested targets, and
    narrow the signals query by ``source_id = ANY(...)`` and/or
    ``geo && ...``. Each returned row is annotated with the originating
    ``target_id`` (the first requested target whose source/geo scope the
    signal falls under) so the runner's per-target grouping + the finding's
    ``contributing_target_ids`` metadata keep working unchanged.

    Empty ``target_ids`` returns ``[]`` immediately — refusing the query is
    safer than scanning the entire ``signals`` table when the subscription
    predicate matched no targets.

    The runtime calls this (or its own equivalent) before invoking
    :func:`run_method`. Surfaced as a module-level helper so subscription-
    resolution code paths (registry-side or planner-side) can be tested
    independently and reused by downstream tooling.
    """
    if not target_ids:
        return []

    # Resolve each target's source_id refs + geo scope. Keep them keyed per
    # target so we can annotate each returned signal with the originating
    # target_id (preserving the requested-target order for first-match).
    per_target_sources: dict[str, list[str]] = {}
    per_target_geo: dict[str, list[str]] = {}
    union_sources: list[str] = []
    union_geo: list[str] = []
    for tid in target_ids:
        srcs: list[str] = []
        geo: list[str] = []
        try:
            trow = await conn.fetchrow(
                "SELECT body FROM target_descriptors "
                "WHERE descriptor_id = $1 AND is_head = TRUE",
                tid,
            )
        except Exception:                                       # pragma: no cover
            trow = None
        if trow and trow["body"]:
            tbody = trow["body"]
            if isinstance(tbody, str):
                try:
                    tbody = json.loads(tbody)
                except Exception:                               # pragma: no cover
                    tbody = {}
            if isinstance(tbody, dict):
                for sref in (tbody.get("sources") or []):
                    sid = sref.get("source_id") if isinstance(sref, dict) else None
                    if sid:
                        srcs.append(str(sid))
                scope = tbody.get("scope") or {}
                geo = [str(g) for g in (scope.get("geo") or []) if g]
        per_target_sources[tid] = srcs
        per_target_geo[tid] = geo
        for s in srcs:
            if s not in union_sources:
                union_sources.append(s)
        for g in geo:
            if g not in union_geo:
                union_geo.append(g)

    # Fresh cross-target reactive window — exclude backfill (S4-T4): a backdated
    # manual observation (fetched_at=load-time) must not surface as fresh
    # cross-target raw input; it still informs facts/grounding via accumulation.
    clauses = [
        f"fetched_at > NOW() - make_interval(hours => $1)",
        SIGNALS_EXCLUDE_BACKFILL_SQL,
        # C2b: canonical-only — drop re-polled alias duplicates so the same
        # event isn't counted N times as independent cross-target corroboration.
        # Mirrors the subscription path (subscription/filter.py canonical_only)
        # and the reads API (registry/substrate_reads_api.py).
        "(canonical_signal_id IS NULL OR canonical_signal_id = id)",
    ]
    params: list[Any] = [int(time_window_hours)]
    scope_clauses: list[str] = []
    if union_sources:
        params.append(union_sources)
        scope_clauses.append(f"source_id = ANY(${len(params)}::TEXT[])")
    if union_geo:
        params.append(union_geo)
        scope_clauses.append(f"geo && ${len(params)}::text[]")
    if not scope_clauses:
        # No resolvable source/geo scope across any requested target —
        # refuse the query rather than scan the whole table.
        return []
    clauses.append("(" + " OR ".join(scope_clauses) + ")")
    where = "WHERE " + " AND ".join(clauses)
    rows = await conn.fetch(
        f"""
        SELECT id, source_id, source_version, canonical_url,
               payload, language, geo, tags, fetched_at, derived_from
        FROM signals
        {where}
        ORDER BY fetched_at DESC
        LIMIT {int(limit)}
        """,
        *params,
    )

    # Back-compat shaping: the runner's renderer + finding-metadata code read
    # the historical signal-row keys (target_id/source_url/title/data/
    # produced_at). Map the new columns onto them, and attribute each signal
    # to the first requested target whose source/geo scope it falls under.
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        payload = d.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:                                   # pragma: no cover
                payload = {}
        sid = d.get("source_id")
        sig_geo = list(d.get("geo") or [])
        attributed: str | None = None
        for tid in target_ids:
            if sid and sid in per_target_sources.get(tid, []):
                attributed = tid
                break
            tgeo = per_target_geo.get(tid, [])
            if tgeo and any(g in tgeo for g in sig_geo):
                attributed = tid
                break
        d["target_id"] = attributed
        d["target_version"] = None
        d["source_url"] = d.get("canonical_url")
        d["title"] = payload.get("title") if isinstance(payload, dict) else None
        d["data"] = payload
        d["produced_at"] = d.get("fetched_at")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Runner — wires the broader-data LLM call together
# ---------------------------------------------------------------------------


class CrossTargetRawRunner:
    """Callable conforming to the runtime's ``AnalystRunFn`` shape.

    Constructed once per analyst actor; the runtime injects a configured
    LLM handler instance via :class:`CrossTargetDeps`. Each call makes one
    chat_complete invocation and returns one finding.

    Signature parity with ``InlineTargetRunner`` (the sibling kind) is
    intentional — the actor layer in :mod:`legba.runtime.dapr_actors`
    treats them interchangeably. Differences are entirely in:

      * the rendered user prompt (multi-target framing, target-grouped),
      * the system prompt (broader-data planner),
      * the finding metadata (``cross_target: True`` + target_ids).
    """

    def __init__(
        self,
        llm: LLMHandlerLike,
        *,
        max_tokens: int = 1536,  # slightly bigger than inline; multi-target slices
        temperature: float = 0.2,
        system_prompt: str | None = None,
    ) -> None:
        self._llm = llm
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._system_prompt = system_prompt or _BROADER_DATA_SYSTEM

    async def __call__(
        self,
        inputs: list[dict[str, Any]],
        options: Mapping[str, Any],
    ) -> AnalystMethodResult:
        # The runtime can supply target_ids explicitly via options; if not,
        # derive from the rows actually present in the slice. We always
        # union the two so a stale resolved list doesn't leak rows from
        # outside the runtime's view.
        provided: list[str] = []
        raw_provided = options.get("target_ids")
        if isinstance(raw_provided, (list, tuple)):
            provided = [str(t) for t in raw_provided if isinstance(t, str) and t]
        derived = _extract_target_ids_from_inputs(inputs)
        # Preserve provided ordering when present; otherwise use derived.
        target_ids: list[str]
        if provided:
            seen = set(provided)
            target_ids = list(provided) + [t for t in derived if t not in seen]
        else:
            target_ids = derived

        user_text = _render_cross_target_user_prompt(inputs, target_ids)
        messages = [{"role": "user", "content": user_text}]
        # TODO(L-170/L-176): wrap as dspy.Module — signature + Predict +
        # traceable Eval. See ``build_prompt_module`` above.
        response = await self._llm.chat_complete(
            messages,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            system=self._system_prompt,
        )
        content = getattr(response, "content", "") or ""
        usage_raw = getattr(response, "usage", None)
        usage_dict = {
            "prompt_tokens": getattr(usage_raw, "prompt_tokens", 0) if usage_raw else 0,
            "completion_tokens": (
                getattr(usage_raw, "completion_tokens", 0) if usage_raw else 0
            ),
            "reasoning_tokens": (
                getattr(usage_raw, "reasoning_tokens", 0) if usage_raw else 0
            ),
        }
        fallback_title = (
            f"Cross-target assessment ({len(target_ids)} targets)"
            if target_ids
            else "Cross-target assessment"
        )
        finding = _coerce_finding(
            content,
            fallback_title=fallback_title,
            target_ids=target_ids,
        )
        return AnalystMethodResult(finding=finding, usage=usage_dict)


# ---------------------------------------------------------------------------
# Module-level run_method — the kind's entry point
# ---------------------------------------------------------------------------


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: CrossTargetDeps,
) -> AnalystMethodResult:
    """Entry point the runtime calls per analyst-actor run for this kind.

    The host walks :mod:`legba.data.analysts` at startup, binds
    ``KIND_NAME`` -> this function, and dispatches by descriptor.kind.

    Parameters
    ----------
    inputs:
        Substrate-row dicts (the cross-target signal slice). The runtime
        resolves the subscription predicate to a target_id list, calls
        :func:`read_cross_target_slice` (or equivalent), and passes the
        rows here. Empty input is permitted — the runner emits a
        zero-target finding rather than raising.
    options:
        Per-run metadata. Conventional keys:
          * ``analyst_id``, ``analyst_version``, ``run_id`` — provenance.
          * ``target_ids`` — *optional* explicit list (sequence of strings)
            from subscription resolution. When supplied, used as the
            authoritative ordering; missing/empty falls back to the set
            derived from ``inputs``.
        Additional keys are ignored to keep the actor wrapper free of
        kind-specific surface assumptions.
    deps:
        Object satisfying :class:`CrossTargetDeps` — at minimum carries an
        ``llm`` attribute conforming to
        :class:`legba.runtime.analyst_method.LLMHandlerLike`.

    Returns
    -------
    AnalystMethodResult
        Carrying a :class:`FindingPayload` whose ``data`` field includes
        ``cross_target=True`` and ``contributing_target_ids=[...]``.
        Token usage rolls up under the ``usage`` dict for budget recording.
    """
    runner = CrossTargetRawRunner(deps.llm)
    return await runner(inputs, options)


# ---------------------------------------------------------------------------
# Per-kind substrate-slice reader bound to the actor-host dispatcher.
# The actor dispatcher's ``_read_substrate_slice`` walks ``READ_SLICE``
# instead of its default signals-only reader when this kind runs.
# ---------------------------------------------------------------------------


async def READ_SLICE(  # noqa: N802 — host-discovered constant alias
    conn,  # type: ignore[no-untyped-def]
    *,
    descriptor,  # type: ignore[no-untyped-def]
    target_filter,  # type: ignore[no-untyped-def]
    target_ids: list[str] | None = None,
    time_window_hours: int = 24,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Adapter exposing :func:`read_cross_target_slice` under the host
    dispatcher's signature.

    The host calls ``READ_SLICE(conn, descriptor=..., target_filter=...)``.
    For ``cross_target_raw`` we derive the candidate target_ids from
    options (provided via ``target_filter``) or default to the
    descriptor's ``subscription.targets.id_list`` if it carries one.

    Returns the same column projection as the default signals reader so
    downstream code paths (derived_from extraction, NATS publishing) keep
    working unchanged.
    """
    # Resolve target_ids set:
    #   1) explicit argument (test path),
    #   2) options.target_ids (when the runtime passes scope explicitly),
    #   3) the subscription's id_list block (if descriptor exposes one),
    #   4) the singleton ``target_filter`` (fall-back to inline behavior).
    if target_ids:
        ids = list(target_ids)
    else:
        sub = getattr(descriptor, "subscription", None)
        sub_targets = getattr(sub, "targets", None) if sub is not None else None
        id_list = getattr(sub_targets, "id_list", None) if sub_targets is not None else None
        if id_list:
            ids = [str(t) for t in id_list]
        elif target_filter:
            ids = [str(target_filter)]
        else:
            ids = []

    return await read_cross_target_slice(
        conn,
        target_ids=ids,
        time_window_hours=time_window_hours,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------


__all__ = [
    "AnalystMethodResult",
    "CrossTargetDeps",
    "CrossTargetRawRunner",
    "KIND_NAME",
    "LLMHandlerLike",
    "OUTPUT_KIND",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "build_prompt_module",
    "read_cross_target_slice",
    "run_method",
]
