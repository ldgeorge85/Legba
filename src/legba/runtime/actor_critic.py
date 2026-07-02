# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Critic context resolution (DB-reading free fns) — extracted from dapr_actors.py (#93), behavior-preserving move."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Mapping
from uuid import UUID

import asyncpg

from ..data.schemas.analyst import AnalystDescriptor

if TYPE_CHECKING:  # pragma: no cover - typing-only; avoids dapr_actors import cycle
    from .dapr_actors import _AnalystDeps

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# P0-T2 — MANDATORY faithfulness verify, persisted as a critique (the gate)
# ---------------------------------------------------------------------------


async def verify_inline_target_finding(
    conn: asyncpg.Connection,
    *,
    deps: "_AnalystDeps",
    finding_id: UUID,
    finding_payload: Any,
    run_id: Any,
) -> dict[str, Any] | None:
    """Run the faithfulness verify pass over a just-emitted FINDING and PERSIST
    the verdict as a ``critique`` so the existing critic-actuation gate folds
    ``overall_score`` into ``effective_confidence = min(confidence,
    overall_score)``.

    Handles TWO citation conventions through the single generalized verify pass
    (name retained for its callers):

      * ``inline_target`` — the unit ``[N]`` → signal bridge (P0-T2), ALWAYS
        verified (a finding with no citations floors honestly-low).
      * ``meta_findings_synthesizer`` — the per-country COMPOSITION's
        ``[[ref:<uuid>]]`` → sub-claim bridge (P3-T3/T7). Verified only when the
        payload carries a ``data['citations']`` key: an honest-EMPTY composition
        (no citations key) and the GLOBAL meta (never sets one) are no-ops. The
        composition path additionally passes ``finding_confidence`` so the T7
        hedge-laundering / anti-double-counting cap folds through the gate.

    The DETERMINISTIC citation floor ALWAYS runs; the optional LLM judge engages
    only when ``deps.verify_judge`` is wired (the host sets it iff the descriptor
    declares ``method.llm.verify`` AND ``LEGBA_VERIFY_LLM_JUDGE`` is on). The
    critique row is written ON THE SAME ``conn`` so the verdict lands in the same
    actor turn.

    Returns the verification dict (for the trace/return envelope) or ``None`` when
    nothing was verified. Best-effort: NEVER raises into the run path — a verify
    failure logs and the finding stays durable + un-demoted.
    """
    kind = getattr(deps.descriptor.identity, "kind", None)
    body = str(getattr(finding_payload, "body", "") or "")
    data = getattr(finding_payload, "data", None)
    citations = data.get("citations") if isinstance(data, Mapping) else None

    # SCOPE GUARD — the unit inline_target kind (always) OR a COMPOSITION
    # meta_findings_synthesizer finding that actually emitted a citation bridge.
    # The honest-EMPTY composition returns before its CITE block with NO citations
    # key, and the GLOBAL meta never sets one → both are no-ops here (the second
    # gate; the first is the dapr_actors fire condition on target_id).
    is_composition = kind == "meta_findings_synthesizer"
    if kind == "inline_target":
        pass
    elif is_composition:
        if citations is None:
            return None
    else:
        return None

    # COMPOSITION only: pass the finding's own confidence so the T7 hedge-
    # laundering check can compare an asserted clause confidence against its cited
    # sub-claim's ceiling. The unit path passes None → byte-identical.
    finding_confidence: float | None = None
    if is_composition:
        try:
            finding_confidence = float(getattr(finding_payload, "confidence"))
        except (TypeError, ValueError):
            finding_confidence = None

    from ..data.provenance._core import AnalystContext
    from ..data.provenance.verify import (
        build_faithfulness_critique_payload,
        verify_finding_faithfulness,
    )
    from ..data.provenance.writes import write_critique

    try:
        report = await verify_finding_faithfulness(
            body=body,
            citations=citations,
            judge_llm=deps.verify_judge,
            finding_confidence=finding_confidence,
        )
    except Exception as exc:  # pragma: no cover — verify must never break a run
        logger.warning(
            "actor_critic.verify.failed finding_id=%s err=%s", finding_id, exc,
        )
        return None

    # Identity of the analyzed analyst (the finding's producer) + the judge.
    analyzed_analyst_id = str(deps.descriptor.identity.id)
    analyzed_analyst_version = str(deps.descriptor.identity.version)
    analyzed_model = _extract_primary_model_ref(deps.descriptor)
    judge_model = str(getattr(deps.verify_judge, "subprovider", "") or "deterministic-floor")

    payload = build_faithfulness_critique_payload(
        report,
        analyzed_output_id=finding_id,
        analyzed_analyst_id=analyzed_analyst_id,
        analyzed_analyst_version=analyzed_analyst_version,
        analyzed_model=analyzed_model,
        judge_model=judge_model,
    )

    # The verify pass IS the critic here — stamp the analyst_ctx with this
    # analyst's identity (the verify is an in-run side-write, not a separate
    # critic actor). target_id NULL: a faithfulness critique is not target-scoped.
    ctx = AnalystContext(
        analyst_id=analyzed_analyst_id,
        analyst_version=analyzed_analyst_version,
        run_id=run_id,
        target_id=None,
        target_version=None,
    )
    try:
        row, dlq = await write_critique(
            conn,
            analyst_ctx=ctx,
            payload=payload,
            derived_from=[finding_id],
        )
        if row is None:
            logger.warning(
                "actor_critic.verify.critique_dlq finding_id=%s — faithfulness "
                "critique failed validation (sent to DLQ)", finding_id,
            )
    except Exception as exc:  # pragma: no cover — best-effort persist
        logger.warning(
            "actor_critic.verify.persist_failed finding_id=%s err=%s",
            finding_id, exc,
        )

    return report.as_dict()
