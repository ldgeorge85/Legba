# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Critic context resolution (DB-reading free fns) — extracted from dapr_actors.py (#93), behavior-preserving move."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Mapping

import asyncpg

from ..data.schemas.analyst import AnalystDescriptor

if TYPE_CHECKING:  # pragma: no cover - typing-only; avoids dapr_actors import cycle
    from .dapr_actors import _AnalystDeps


def _extract_primary_model_ref(descriptor: AnalystDescriptor) -> str:
    """Resolve an LLM model string from an analyst descriptor.

    The descriptor's ``method.llm.primary`` slot carries a property-factory
    StackRef dump — typically ``{"raw": "llm.primary.openai_compat", ...}``.
    We surface the ``raw`` value (the StackRef path) as the canonical model
    identity so the critic's heterogeneity guard can compare it against
    the critic's own LLM subprovider string.

    Returns an empty string if the descriptor has no resolvable LLM
    primary — the heterogeneity guard handles missing identity as an
    audit-gap warning rather than a hard failure.
    """
    method = getattr(descriptor, "method", None)
    if method is None:
        return ""
    llm = getattr(method, "llm", None) or {}
    if not isinstance(llm, Mapping):
        return ""
    primary = llm.get("primary")
    if isinstance(primary, Mapping):
        return str(primary.get("raw") or "")
    if isinstance(primary, str):
        return primary
    return ""


async def _resolve_critic_context(
    conn: asyncpg.Connection,
    *,
    deps: "_AnalystDeps",
    target_filter: str | None,
    payload_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the critic-kind's per-run ``options`` dict.

    The critic kind's :func:`run_method` needs four runtime-supplied
    options keys (per L-105 / L-175):

      * ``rubric`` — the analyzed analyst's ``descriptor.eval.rubric``;
      * ``analyzed_model`` — primary LLM stack-ref of the analyzed
        analyst (so the heterogeneity guard can refuse self-correlation);
      * ``analyzed_output_id`` — the ``analyst_outputs.id`` (UUID) of the
        row being graded;
      * ``allow_self_correlated`` — the analyzed analyst's
        ``descriptor.eval.allow_self_correlated`` (typed schema field
        per L-105 §3 / Wave-B integration; legacy
        ``eval.optimizer["allow_self_correlated"]`` is accepted as a
        fall-through for descriptors that predate the typed field).

    The helper looks up the analyzed analyst's descriptor via the
    ``analyst_descriptors`` table (head row), parses the JSONB body
    locally to avoid coupling to the registry's Python surface, and
    returns the assembled options dict ready for ``options.update(...)``
    in the actor's run path.

    The analyzed analyst's id is resolved in this priority order:

      1. ``payload_options["analyzed_analyst_id"]`` (caller passes
         explicitly — the production runtime path),
      2. critic descriptor's ``eval.optimizer["analyzed_analyst_id"]``
         (descriptor-pinned target — a critic that exclusively grades
         one analyst can hardcode this).
      3. ``target_filter`` (legacy code-paths that pass the analyzed
         analyst's id via the ``target_filter`` channel).

    Returns an empty dict when the analyzed analyst can't be resolved —
    the critic kind's own missing-rubric / missing-model handling then
    surfaces the gap (raises :class:`MissingRubricError` or logs the
    heterogeneity-guard warning).
    """
    payload_options = payload_options or {}
    out: dict[str, Any] = {}

    # 1. Identify the analyzed analyst id.
    analyzed_id: str | None = (
        payload_options.get("analyzed_analyst_id")
        or _critic_descriptor_pinned_analyst_id(deps.descriptor)
        or target_filter
    )
    if not analyzed_id:
        return out

    # 2. Identify the analyzed-output row id (the row being graded).
    analyzed_output_id = payload_options.get("analyzed_output_id")
    if analyzed_output_id is not None:
        out["analyzed_output_id"] = str(analyzed_output_id)

    # 3. Look up the analyzed analyst's descriptor body.
    row = await conn.fetchrow(
        "SELECT body FROM analyst_descriptors "
        "WHERE descriptor_id = $1 AND is_head = TRUE",
        analyzed_id,
    )
    if row is None:
        # No descriptor found — return what we have so far; the critic
        # kind's MissingRubricError surfaces the gap downstream.
        return out

    body = row["body"]
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            body = None
    if not isinstance(body, dict):
        return out

    eval_block = body.get("eval") if isinstance(body.get("eval"), dict) else None
    method_block = body.get("method") if isinstance(body.get("method"), dict) else None

    # rubric
    if eval_block and isinstance(eval_block.get("rubric"), str):
        out["rubric"] = eval_block["rubric"]

    # allow_self_correlated — typed field first, legacy optimizer-dict second.
    if eval_block is not None:
        typed = eval_block.get("allow_self_correlated")
        if isinstance(typed, bool):
            out["allow_self_correlated"] = typed
        else:
            opt = eval_block.get("optimizer") or {}
            if isinstance(opt, dict):
                legacy = opt.get("allow_self_correlated")
                if isinstance(legacy, bool):
                    out["allow_self_correlated"] = legacy

    # analyzed_model — primary LLM stack ref of the analyzed analyst.
    if method_block is not None:
        llm = method_block.get("llm") or {}
        if isinstance(llm, dict):
            primary = llm.get("primary")
            if isinstance(primary, dict):
                raw = primary.get("raw")
                if isinstance(raw, str) and raw:
                    out["analyzed_model"] = raw
            elif isinstance(primary, str) and primary:
                out["analyzed_model"] = primary

    # Stamp the analyzed analyst id + version so the critic's row
    # carries the full provenance without the kind re-querying.
    out.setdefault("analyzed_analyst_id", analyzed_id)
    version = body.get("identity", {}).get("version") if isinstance(body.get("identity"), dict) else None
    if isinstance(version, str) and version:
        out.setdefault("analyzed_analyst_version", version)

    return out


def _critic_descriptor_pinned_analyst_id(descriptor: AnalystDescriptor) -> str | None:
    """Return the descriptor-pinned analyzed analyst id, if any.

    A critic descriptor that exclusively grades one analyst can stamp
    the target via ``eval.optimizer["analyzed_analyst_id"]`` (mirrors
    the L-176 optimizer's analyzed-target pointer).  Returns ``None``
    when unset.
    """
    eval_block = getattr(descriptor, "eval", None)
    if eval_block is None:
        return None
    opt = getattr(eval_block, "optimizer", None) or {}
    if not isinstance(opt, Mapping):
        return None
    pinned = opt.get("analyzed_analyst_id")
    return pinned if isinstance(pinned, str) and pinned else None
