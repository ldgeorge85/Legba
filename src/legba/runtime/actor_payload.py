# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Payload selection + critique-trace DB write — extracted from dapr_actors.py (#93), behavior-preserving move."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from uuid import UUID

import asyncpg

from ..data.provenance.kinds import OutputKind

logger = logging.getLogger(__name__)


async def _invoke_run_method(
    run_method: Any,
    inputs: list[dict[str, Any]],
    options: dict[str, Any],
    kind_deps: Any | None,
) -> Any:
    """Dispatch the analyst's ``run_method`` with the right arity.

    The L-102 contract is 3-arg ``(inputs, options, deps)``.  The
    integration pass points the host at each kind's module-level
    ``run_method`` directly; the host injects ``kind_deps`` (the bundle
    the kind module declares — e.g. :class:`InlineTargetDeps`,
    :class:`CrossTargetDeps`).

    We retain the legacy 2-arg call site (used by the spike's
    :class:`LLMAnalystRunner` adapter) so existing deps registrations
    keep working unchanged.  The selector is purely positional:

      * if ``kind_deps`` is not None → 3-arg dispatch,
      * else → 2-arg dispatch (back-compat with the spike adapter).
    """
    if kind_deps is not None:
        return await run_method(inputs, options, kind_deps)
    return await run_method(inputs, options)


def _receipt_output_payload(payload: Any) -> Any:
    """Canonicalize ``payload`` into a JSON-able form for the receipt hash.

    The runtime hashes the output payload as part of the per-run receipt
    (per :func:`legba.data.provenance._core.compute_receipt_hash`); the
    canonical-JSON helper accepts pydantic models via ``model_dump`` but
    we coerce up front so the hashed shape is stable and easy to inspect
    in the ``analyst_traces.output_payload`` column.

    None and pre-coerced dicts pass through; pydantic models become
    ``model_dump(mode="json")`` dicts; anything else falls back to
    ``str(...)`` so the hasher's ``default=`` branch isn't relied on for
    chain content (cleaner for forensic re-derivation).
    """
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return {"value": str(payload)}


def _payload_finding(method_result: Any) -> Any:
    """Default selector — return the canonical FindingPayload from ``method_result``."""
    return getattr(method_result, "finding", None)


def _payload_nested(key: str) -> Callable[[Any], Any]:
    """Build a selector that returns ``method_result.finding.data[key]``.

    Used by kinds that stash a typed payload under ``finding.data["<key>"]``
    for back-compat with the FINDING-only dispatcher (predictor, optimizer,
    critic).  The selector falls back to the bare finding when the nested
    blob is absent — ``write_analyst_output`` then DLQ-routes the wrong-
    shape row, which is the correct signal that the analyst module forgot
    to populate its typed payload.
    """
    def _selector(method_result: Any) -> Any:
        finding = getattr(method_result, "finding", None)
        if finding is None:
            return None
        data = getattr(finding, "data", None) or {}
        if isinstance(data, dict):
            value = data.get(key)
            if value:
                return value
        return finding
    _selector.__name__ = f"_payload_nested_{key}"
    return _selector


# Per-kind dispatch table — replaces the prior if/elif chain.  Adding a
# new ``OutputKind`` that stashes its payload somewhere non-default means
# registering a selector here; the runtime then routes the row through
# the correct write surface without touching the actor body.
#
# Pre-L-205 this table was mirrored in the embedded ``runtime/actors.py``;
# that path retired with the rest of the legacy cycle code, so this is now
# the sole authority.
_PAYLOAD_SELECTORS: dict[OutputKind, Callable[[Any], Any]] = {
    OutputKind.FINDING: _payload_finding,
    OutputKind.PREDICTION: _payload_nested("prediction"),
    OutputKind.PROMPT_MODULE_CANDIDATE: _payload_nested("candidate"),
    OutputKind.CRITIQUE: _payload_nested("critique"),
    # A synthesize/analyst kind that returns a typed FactPayload stashes it
    # under finding.data["fact"]; unwrap it (mirrors PREDICTION/CRITIQUE).
    OutputKind.FACT: _payload_nested("fact"),
    # The relationship_reifier returns a typed NexusPayload stashed under
    # finding.data["nexus"]; unwrap it (mirrors FACT/PREDICTION). The reifier
    # ALSO side-writes its nexus rows directly via write_nexus on its own
    # connection (the situation_clustering/hypothesis_lifecycle precedent), so
    # this selector covers the per-run summary path where a kind wraps a single
    # nexus in a finding envelope.
    OutputKind.NEXUS: _payload_nested("nexus"),
}


def _resolve_effective_output_kind(
    *,
    kind: str,
    bind_output_kind: Any,
    options: Mapping[str, Any],
) -> Any:
    """Resolve the per-run effective output kind (an ``OutputKind`` or ``TRACE_ONLY``).

    The bind-time ``output_kind`` (the kind module's ``OUTPUT_KIND``) is the
    default. Two refinements happen here:

      * **deterministic** is a sub-dispatched kind: each sub-handler declares
        its own kind in ``deterministic.OUTPUT_KIND_BY_SUB_HANDLER`` (a genuine
        FINDING for graph_mining / anomaly_detection / situation_clustering /
        …, or ``TRACE_ONLY`` for the maintenance sub-handlers whose real
        product is side-written). The actor injected ``options['sub_handler']``
        upstream, so resolve through the table here.  Unknown / missing
        sub-handler falls back to the bind-time kind (FINDING) — the
        deterministic dispatcher itself fails loud on a bad sub_handler before
        we reach this point.

      * a ``None`` bind-time kind degrades to ``OutputKind.FINDING`` (the
        historical default for the spike-built deps bundle).

    Returns either an :class:`OutputKind` (write a row) or the ``TRACE_ONLY``
    sentinel (skip the analyst_outputs row; the trace + side-writes still run).
    """
    if kind == "deterministic":
        sub_handler = options.get("sub_handler")
        if sub_handler is not None:
            try:
                from ..data.analysts.deterministic import (
                    OUTPUT_KIND_BY_SUB_HANDLER,
                )
            except Exception:  # pragma: no cover - import guard
                OUTPUT_KIND_BY_SUB_HANDLER = {}
            resolved = OUTPUT_KIND_BY_SUB_HANDLER.get(sub_handler)
            if resolved is not None:
                return resolved
    if bind_output_kind is None:
        return OutputKind.FINDING
    return bind_output_kind


def _select_output_payload(method_result: Any, output_kind: OutputKind) -> Any:
    """Pick the payload to persist based on the analyst's declared output kind.

    Dispatch table (per kind):

      * ``OutputKind.FINDING`` → ``method_result.finding``;
      * ``OutputKind.PREDICTION`` → ``method_result.finding.data["prediction"]``
        (the L-174 predictor stashes a PredictionPayload-dump there);
      * ``OutputKind.PROMPT_MODULE_CANDIDATE`` →
        ``method_result.finding.data["candidate"]`` (the L-176 optimizer
        stashes a PromptModuleCandidatePayload-dump there);
      * ``OutputKind.CRITIQUE`` → ``method_result.finding.data["critique"]``
        when present, else ``method_result.finding`` (the L-175 critic
        writes a typed :class:`CritiquePayload` directly — the nested
        slot is only consulted when an analyst kind wraps a critique
        inside a finding envelope).

    Kinds not in :data:`_PAYLOAD_SELECTORS` fall back to the default
    finding selector — preserves the legacy behaviour for kinds whose
    payload IS the finding (situations, hypotheses, alerts, meta-findings,
    signals).

    Returns the chosen payload (a pydantic model or a dict).  The
    runtime's write helper handles dict→model coercion downstream.
    """
    selector = _PAYLOAD_SELECTORS.get(output_kind, _payload_finding)
    return selector(method_result)


# ---------------------------------------------------------------------------
# Failure trace-finalizer: analyst_traces write for a run that DIED
# ---------------------------------------------------------------------------

#: ``analyst_traces.status`` for a run that started and raised. The column is
#: free text (no CHECK) and every row ever written carried ``'success'`` — the
#: partial index ``analyst_traces_status_idx ... WHERE status <> 'success'``
#: was built for exactly this vocabulary and, until now, indexed nothing.
TRACE_STATUS_FAILED = "failed"

#: ``bucket_kind`` (:func:`legba.runtime.actor_retry._classify_exception`) →
#: the ``ActorRunOutcome`` value the run's except-handler settles on. Kept here
#: so the failure trace can state the outcome WITHOUT the caller having to
#: thread it through before the per-bucket branch decides it. The 'hard' bucket
#: lands HARD_FAIL under every ``hard.strategy`` (pause/drop/dlq_and_alert), so
#: the mapping is total.
_BUCKET_OUTCOME = {
    "budget": "budget_throttled",
    "transient": "transient_fail",
    "hard": "hard_fail",
}


async def _write_failure_trace(
    receipt_chain: Any | None,
    *,
    run_id: UUID,
    analyst_id: str,
    analyst_version: str,
    cadence_trigger: str,
    target_id: str | None,
    exc: BaseException,
    bucket_kind: str,
    attempts_made: int,
    max_attempts: int | None,
    run_started_at: datetime,
) -> bool:
    """Write the ``analyst_traces`` row for a run that STARTED and then died.

    Until this landed, the trace was written on the SUCCESS path only (the
    ``receipt_chain.record(...)`` call after the analyst-output INSERT), so a
    run that exhausted its transient retries — or raised anywhere else past
    the substrate read — left NOTHING behind but a log line. Two production
    incidents hid inside that gap: the run history showed the analyst's last
    SUCCESSFUL run and every staleness read agreed the fleet was healthy,
    because a dead run is indistinguishable from a run that never fired.

    The row carries ``status='failed'`` plus an ``error_payload`` stating the
    error class, the classified retry bucket, the settled outcome, and the
    attempt count — enough to tell "the model 500'd three times" from "the
    descriptor is malformed" without reaching for container logs.

    Chain semantics: a failed run is still a run, so the receipt chain extends
    over it. ``output_row_refs`` is empty (nothing landed) and
    ``output_payload`` restates the error, so the hash is still computed over
    real run content and the chain stays linear.

    Returns ``True`` when a row landed. NEVER raises: the caller is already
    handling an exception and a failing trace write must not mask it (this is
    the path that runs when Postgres itself is the reason the run died). All
    failures log at WARNING and return ``False``.

    ``receipt_chain`` is ``None`` on the spike integration-test path; that
    degrades to a no-op exactly as the success path does.
    """
    if receipt_chain is None:
        return False
    try:
        outcome = _BUCKET_OUTCOME.get(bucket_kind, "hard_fail")
        error_payload = {
            "error_class": type(exc).__name__,
            "error_module": type(exc).__module__,
            "error": str(exc)[:4096],
            "bucket": bucket_kind,
            "outcome": outcome,
            "attempts_made": int(attempts_made),
            "max_attempts": (
                int(max_attempts) if max_attempts is not None else None
            ),
        }
        await receipt_chain.record(
            run_id=run_id,
            analyst_id=analyst_id,
            analyst_version=analyst_version,
            cadence_trigger=cadence_trigger,
            target_id=target_id,
            input_row_refs=[],
            input_payload=None,
            prompt_module_hash=None,
            prompt_rendered=None,
            output_row_refs=[],
            output_payload=error_payload,
            run_started_at=run_started_at,
            run_ended_at=datetime.now(timezone.utc),
            status=TRACE_STATUS_FAILED,
            error_payload=error_payload,
        )
        return True
    except BaseException as trace_exc:   # noqa: BLE001 - must not mask ``exc``
        logger.warning(
            "dapr_actors.analyst.failure_trace.failed "
            "analyst_id=%s run_id=%s err=%s",
            analyst_id, run_id, trace_exc,
        )
        return False


# ---------------------------------------------------------------------------
# Critic trace-finalizer: analyst_critiques write
# ---------------------------------------------------------------------------


async def _write_critique_trace_record(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    trace_written: bool,
    judge_analyst_id: str,
    judge_analyst_version: str,
    payload: Any,
    options: Mapping[str, Any],
) -> None:
    """Insert a row into ``analyst_critiques`` for an L-175 critic run.

    Per ``data/migrations/0005_runtime_tables.sql`` the table is the
    trace-level critique sink keyed by ``trace_id`` (the run_id of the
    critic's ``analyst_traces`` row).  The critic kind handler writes the
    full :class:`CritiquePayload` via the standard analyst-output dispatch
    (``analyst_outputs`` with ``kind='critique'``); this helper mirrors
    the load-bearing fields into ``analyst_critiques`` so:

      * the L-176 optimizer's training-window query (joins
        ``analyst_traces`` LEFT JOIN ``analyst_critiques`` on
        ``run_id = trace_id``) sees the row;
      * the runtime telemetry ``/api/v1/registry/critiques`` endpoint
        (joins same) returns it.

    Skips the write when no ``analyst_traces`` row exists for ``run_id``
    (the FK would fail otherwise) — that happens on the spike's
    integration test path which deliberately leaves ``receipt_chain``
    unset.  ``trace_written`` is the caller's signal (receipt_hash was
    successfully recorded).

    Failure surfaces as the asyncpg exception; the caller's outer
    try/except logs + continues so a malformed payload doesn't block the
    analyst_outputs row from landing.
    """
    if not trace_written:
        # No analyst_traces row → FK would fail.  Skip cleanly.
        logger.info(
            "dapr_actors.analyst.critique_trace.skip_no_trace run_id=%s",
            run_id,
        )
        return

    # Extract fields from the CritiquePayload — accept either the typed
    # pydantic model or a dict (the analyst-output dispatcher tolerates
    # both and the helper here should match).
    if hasattr(payload, "model_dump"):
        body = payload.model_dump(mode="json")
    elif isinstance(payload, Mapping):
        body = dict(payload)
    else:
        body = {}

    scores = body.get("scores") or {}
    if not isinstance(scores, dict):
        scores = {}

    overall_score = body.get("overall_score")
    try:
        overall_score_f: float | None = (
            float(overall_score) if overall_score is not None else None
        )
    except (TypeError, ValueError):
        overall_score_f = None

    revision_delta = body.get("revision_delta")
    if revision_delta is not None and not isinstance(revision_delta, (str, dict, list)):
        revision_delta = str(revision_delta)

    # rubric_uri — the analyzed analyst's descriptor doesn't carry a true
    # iglu URI for its rubric (the rubric is free-form JSON/text), so we
    # derive a deterministic descriptor-anchored URI of the form
    # ``descriptor://<analyzed_analyst_id>@<version>#eval.rubric``.
    # This keeps the column non-null + searchable by analyst, and the
    # raw rubric content is on the analyst_outputs row for full audit.
    analyzed_analyst_id = (
        options.get("analyzed_analyst_id")
        or body.get("analyzed_analyst_id")
        or ""
    )
    analyzed_analyst_version = (
        options.get("analyzed_analyst_version")
        or body.get("analyzed_analyst_version")
        or ""
    )
    if analyzed_analyst_id:
        rubric_uri = (
            f"descriptor://{analyzed_analyst_id}"
            f"@{analyzed_analyst_version}#eval.rubric"
            if analyzed_analyst_version
            else f"descriptor://{analyzed_analyst_id}#eval.rubric"
        )
    else:
        rubric_uri = "descriptor://unknown#eval.rubric"

    await conn.execute(
        """
        INSERT INTO analyst_critiques (
            trace_id, judge_analyst_id, judge_analyst_version,
            rubric_uri, scores, overall_score, revision_delta
        ) VALUES (
            $1, $2, $3, $4, $5::jsonb, $6, $7::jsonb
        )
        """,
        run_id,
        judge_analyst_id[:256],
        (judge_analyst_version or "")[:64],
        rubric_uri[:1024],
        json.dumps(scores),
        overall_score_f,
        (
            json.dumps(revision_delta)
            if revision_delta is not None
            else None
        ),
    )
