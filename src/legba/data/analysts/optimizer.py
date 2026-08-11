# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-176 optimizer analyst kind — DSPy + GEPA self-improvement loop.

The single analyst kind in Legba that uses a durable workflow (per L-103
hybrid runtime design; Dapr Workflow since P-16/P-CUT).  Reads another
analyst's recorded ``analyst_traces`` + ``analyst_critiques`` rows, runs
the GEPA evolutionary loop (arxiv 2507.19457) over them, and emits a
candidate ``prompt_module`` whose promotion to live is gated by the
descriptor's ``eval.promotion`` policy.

Per L-105 §4 (eval-loop spec) and the L-176 brief
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Read:    (a) ``analyst_traces`` rows for the analyst being optimized,
         (b) ``analyst_critiques`` rows joined by trace_id.
Method:  ``dspy_compile`` — GEPA reflective Pareto-frontier prompt
         evolution.  Multi-hour deterministic outer loop + non-
         deterministic LLM activities → dispatched as a Dapr Workflow
         (see :mod:`legba.runtime.dapr_workflow`).
Write:   :class:`PromptModuleCandidatePayload` to ``analyst_outputs``
         with ``kind = OutputKind.PROMPT_MODULE_CANDIDATE``.
Promote: HUMAN-GATED — an operator flips ``promotion_gate`` to
         ``'promoted'`` and ``resolve_promoted_system_prompt`` admits the
         prompt ONLY when the MEASURED delta is promotable
         (``gepa._delta_gates_ok``). There is no auto-promotion path;
         the ``auto_with_threshold`` policy LABEL is retained on the
         candidate row but nothing auto-acts on it (it too requires the
         operator flip).

Wave B prereqs (commit ``8a1fd5c``):
  * ``MethodBlock.kind`` has the ``dspy_compile`` literal.
  * ``dspy_compile`` is exempt from the ``prompt_module`` requirement
    on :class:`MethodBlock` — the optimizer COMPILES prompts; it
    doesn't have a static one of its own.

For the in-process / no-sidecar path used by tests, see
:class:`legba.runtime.dapr_workflow.gepa.InProcessWorkflowClient`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

import asyncpg

from ..provenance.kinds import OutputKind
from ..provenance.models import FindingPayload, PromptModuleCandidatePayload
from ...runtime.analyst_method import AnalystMethodResult
from ...runtime.dapr_workflow.gepa import (
    InProcessWorkflowClient,
    OptimizerWorkflowInput,
    OptimizerWorkflowResult,
    TrainingSetRef,
    _delta_gates_ok,
    build_default_client,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind identity — host's discover_analyst_kinds picks these up
# ---------------------------------------------------------------------------


KIND_NAME: str = "optimizer"
SCHEMA_VERSION: str = "legba/analyst.optimizer/1-0-0"
HANDLER_VERSION: str = "0.1.0"

# The kind writes PROMPT_MODULE_CANDIDATE rows.  Lands in the generic
# `analyst_outputs` table per the new spec in
# :mod:`legba.data.provenance.kinds`.
OUTPUT_KIND: OutputKind = OutputKind.PROMPT_MODULE_CANDIDATE

# Wave B prereq: the optimizer kind is exempt from build_prompt_module()
# because its job is to COMPILE prompts, not to carry a static one.  The
# host's discover layer still calls this (defensive) — returning None is
# the contract for kinds that don't expose a prompt module.
PROMPT_MODULE_PATH: str | None = None


def build_prompt_module() -> Any:
    """Optimizer is exempt from carrying a prompt module (Wave B prereq).

    Returns None so the host's discover layer can still look this
    attribute up without crashing.  The optimizer compiles OTHER
    kinds' prompt modules; it doesn't have one of its own.
    """
    return None


# ---------------------------------------------------------------------------
# Tunables — module-level so tests can override
# ---------------------------------------------------------------------------


# Per 2026-05-16 ratified decision: 50 ground-truth rows per analyst by
# default.  Descriptors override via ``eval.optimizer.min_traces_required``.
DEFAULT_MIN_TRACES_REQUIRED: int = 50
DEFAULT_MIN_CRITIQUES_REQUIRED: int = 0

# GEPA hyperparameters — descriptors override via ``eval.optimizer.*``.
DEFAULT_MAX_GENERATIONS: int = 5
DEFAULT_REFLECTION_MINIBATCH_SIZE: int = 3
DEFAULT_AUTO_MODE: str = "light"

# Per-run trace + critique read window.  Default 30 days — generous
# enough to accumulate 50 critiqued runs for most analysts on a daily
# cadence.
DEFAULT_READ_WINDOW_DAYS: int = 30
MAX_TRAINING_ROWS: int = 500     # hard cap regardless of window

# Outer wall-clock bound on awaiting the GEPA workflow result. The silent-
# death class that left the optimizer leg dormant ~4 days was the workflow's
# ``compile()`` hanging → ``handle.result()`` never returning → ``run_method``
# never completing → NO ``analyst_trace`` ever written. Bounding the await
# means the actor ALWAYS records a trace (a visible ``workflow_timeout`` row)
# rather than looking dormant. 30 min — comfortably above the worker's own
# per-LM-call bound, so a healthy run returns its real result first. Env-tunable.
DEFAULT_DISPATCH_TIMEOUT_S: float = 1800.0


def _dispatch_timeout_s() -> float:
    """Resolve the optimizer dispatch wall-clock bound from the environment."""
    raw = os.environ.get(
        "LEGBA_OPTIMIZER_DISPATCH_TIMEOUT_S", str(DEFAULT_DISPATCH_TIMEOUT_S),
    )
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DISPATCH_TIMEOUT_S
    return v if v > 0 else DEFAULT_DISPATCH_TIMEOUT_S


# ---------------------------------------------------------------------------
# Parent prompt-module resolution
# ---------------------------------------------------------------------------


#: Prompt packages are named by analyst KIND, not by analyst id:
#: ``src/legba/prompts/<kind>/v1.py`` — ``inline_target``, ``predictor``,
#: ``critic``, ``cross_target_raw``. (``country_assessor`` is the one legacy
#: package named for an analyst; that optimizer declares its path explicitly.)
PROMPT_MODULE_CONVENTION: str = "legba.prompts.{kind}.v1"


def convention_parent_prompt_module_path(analyzed_analyst_kind: str) -> str:
    """The prompt module an analyst of ``analyzed_analyst_kind`` uses.

    Derived from the KIND. The convention used to be built from the analyzed
    ANALYST id, which silently produced non-existent modules for every unit
    analyst — ``unit_optimizer`` optimizes ``leadership_transition`` (kind
    ``inline_target``) and the derived ``legba.prompts.leadership_transition.v1``
    has never existed. See K5_BLAST_RADIUS §3.2.
    """
    return PROMPT_MODULE_CONVENTION.format(kind=analyzed_analyst_kind)


def resolve_parent_prompt_module_path(
    options: Mapping[str, Any],
) -> str | None:
    """Resolve the parent prompt module GEPA evolves, or ``None``.

    Ladder:

      1. ``options['parent_prompt_module_path']`` — an explicit per-run
         override, or the optimizer descriptor's declared
         ``eval.optimizer.parent_prompt_module_path`` (the actor plumbs it
         through this key; before that plumbing existed the declared value
         never reached here and the convention below always won).
      2. the convention, from ``options['analyzed_analyst_kind']``.

    ``None`` — nothing to resolve from. The caller emits a loud no-op rather
    than handing GEPA a module path it invented.
    """
    declared = options.get("parent_prompt_module_path")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    kind = options.get("analyzed_analyst_kind")
    if isinstance(kind, str) and kind.strip():
        return convention_parent_prompt_module_path(kind.strip())
    return None


# ---------------------------------------------------------------------------
# READ_SLICE — joins analyst_traces + analyst_critiques for the target analyst
# ---------------------------------------------------------------------------


async def read_traces_and_critiques(
    conn: asyncpg.Connection,
    *,
    analyzed_analyst_id: str,
    analyzed_analyst_version: str | None = None,
    read_window_days: int = DEFAULT_READ_WINDOW_DAYS,
    limit: int = MAX_TRAINING_ROWS,
    until_ts: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch the GEPA training set for the analyst being optimized.

    Joins ``analyst_traces`` LEFT JOIN ``analyst_critiques`` on
    ``trace.run_id = critique.trace_id`` — LEFT so traces without a
    critique still land (they're informative even uncritiqued, just
    weighted lower by the workflow's metric).

    Returns row dicts with the columns the GEPA loop's metric needs:

      * ``run_id`` — trace run UUID. NB: this is an ``analyst_traces``
        primary key, NOT a lineage-catalog row id — it is recorded only
        under ``data.derived_trace_ids`` on the candidate, never in the
        candidate's ``derived_from`` (D10: ``analyst_traces`` is not in
        the ``derived_from`` lineage catalog, so trace run_ids there are
        100%-dangling edges).
      * ``output_row_refs`` — the substrate-row UUIDs the analyzed run
        actually produced (``analyst_traces.output_row_refs``). THESE are
        the real lineage roots that populate the candidate's
        ``derived_from`` (D10 fix).
      * ``input`` — the trace's ``prompt_rendered`` (what the analyst
        was asked to reason over).
      * ``gold`` — the critic's narrative-corrected output if present,
        else the original ``output_payload`` text.
      * ``critique_score`` — the critic's ``overall_score``, NULL when
        the trace was never critiqued.
      * ``analyzed_analyst_id`` / ``analyzed_analyst_version`` — pass-
        through so the workflow can audit-trail.

    Window anchoring (``until_ts``)
    -------------------------------
    The recent end of the read window is normally ``NOW()`` — fine for the
    READ_SLICE path (the host reads the slice once, immediately before the
    run). But the GEPA workflow worker RE-fetches this exact training set
    by reference (the rows are no longer inlined across the Dapr gRPC
    channel — they'd overflow the 4 MB cap). The re-fetch happens seconds
    to minutes after the original read, so a bare ``NOW()`` would float the
    window and a freshly-landed trace could shift which rows fall inside
    ``LIMIT`` (DESC + LIMIT pushes out the oldest). Passing ``until_ts``
    pins the recent end to the original read's wall-clock instant
    (``run_started_at <= until_ts``), so the re-fetch returns the IDENTICAL
    row set regardless of inserts in between — the transport refactor stays
    a pure transport refactor (byte-for-byte equivalent training set).

    Empty result is meaningful — the workflow will short-circuit with
    a "no training data" candidate (zero delta) which the kind module
    then writes as an audit row anyway, so an operator can see the
    optimizer fired and bailed.
    """
    if not analyzed_analyst_id:
        return []

    params: list[Any] = [analyzed_analyst_id, int(read_window_days)]
    # Recent-end bound: pinned to ``until_ts`` for the re-fetch path (window
    # anchoring, above), else open-ended ``NOW()`` for the live READ_SLICE.
    if until_ts is not None:
        params.append(until_ts)
        recent_bound = (
            f"AND t.run_started_at <= ${len(params)} "
            f"AND t.run_started_at > ${len(params)} - make_interval(days => $2)"
        )
    else:
        recent_bound = "AND t.run_started_at > NOW() - make_interval(days => $2)"

    version_filter = ""
    if analyzed_analyst_version:
        params.append(analyzed_analyst_version)
        version_filter = f" AND t.analyst_version = ${len(params)}"

    query = f"""
        SELECT
            t.run_id              AS run_id,
            t.analyst_id          AS analyzed_analyst_id,
            t.analyst_version     AS analyzed_analyst_version,
            t.prompt_rendered     AS input,
            t.output_payload      AS gold,
            t.status              AS trace_status,
            t.output_row_refs     AS output_row_refs,
            c.overall_score       AS critique_score,
            c.scores              AS critique_scores,
            c.revision_delta      AS critique_revision_delta,
            c.id                  AS critique_id,
            t.run_started_at      AS run_started_at
        FROM analyst_traces t
        LEFT JOIN analyst_critiques c ON c.trace_id = t.run_id
        WHERE t.analyst_id = $1
          {recent_bound}
          {version_filter}
        ORDER BY t.run_started_at DESC
        LIMIT {int(limit)}
    """
    rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


async def READ_SLICE(  # noqa: N802 — host-discovered constant alias
    conn,  # type: ignore[no-untyped-def]
    *,
    descriptor,  # type: ignore[no-untyped-def]
    target_filter,  # type: ignore[no-untyped-def]
    analyzed_analyst_id: str | None = None,
    analyzed_analyst_version: str | None = None,
    read_window_days: int = DEFAULT_READ_WINDOW_DAYS,
    limit: int = MAX_TRAINING_ROWS,
) -> list[dict[str, Any]]:
    """Host-dispatcher adapter for :func:`read_traces_and_critiques`.

    Resolves the analyzed-analyst id in this priority order:

      1. ``analyzed_analyst_id=`` argument (test path),
      2. the descriptor's ``eval.optimizer['analyzed_analyst_id']``
         dict entry (production path — the optimizer descriptor names
         which analyst it optimizes via its eval block),
      3. ``target_filter`` cast to string (fallback for the rare case
         where the runtime passes the analyst id through target_filter,
         e.g. on a per-run override).

    Returns ``[]`` if no analyzed-analyst id resolves — the run_method
    then emits a "noop, no target analyst" candidate row.
    """
    analyst_id = analyzed_analyst_id
    analyst_version = analyzed_analyst_version

    if not analyst_id:
        eval_block = getattr(descriptor, "eval", None) if descriptor else None
        optimizer_cfg = getattr(eval_block, "optimizer", None) if eval_block else None
        if isinstance(optimizer_cfg, dict):
            analyst_id = optimizer_cfg.get("analyzed_analyst_id")
            analyst_version = analyst_version or optimizer_cfg.get(
                "analyzed_analyst_version"
            )

    if not analyst_id and target_filter:
        analyst_id = str(target_filter)

    if not analyst_id:
        logger.info(
            "optimizer.read_slice.no_target_analyst descriptor=%s",
            getattr(getattr(descriptor, "identity", None), "id", None),
        )
        return []

    return await read_traces_and_critiques(
        conn,
        analyzed_analyst_id=str(analyst_id),
        analyzed_analyst_version=(
            str(analyst_version) if analyst_version else None
        ),
        read_window_days=read_window_days,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Workflow-client resolution (P-16 — Dapr Workflow port)
# ---------------------------------------------------------------------------


def _resolve_workflow_client() -> Any:
    """Pick the durable-workflow client backing the optimizer's GEPA loop.

    Resolution order (P-16 cutover-aware):

      1. ``LEGBA_OPTIMIZER_DAPR_WORKFLOW=1`` → the Dapr-Workflow client
         (:func:`legba.runtime.dapr_workflow.build_dapr_workflow_client`).
         When that returns ``None`` (dep missing / in-process forced) we
         fall through so the optimizer still runs.
      2. Otherwise → the in-process GEPA fallback
         (:func:`legba.runtime.dapr_workflow.gepa.build_default_client`).

    The returned object satisfies the same
    ``start_optimizer_workflow(wf_input, *, workflow_id) -> handle``
    contract regardless of backend, so the kind's ``run_method`` is
    backend-agnostic — this is what keeps the cutover from touching the
    kind's external interface.
    """
    if os.environ.get("LEGBA_OPTIMIZER_DAPR_WORKFLOW") == "1":
        try:
            from ...runtime.dapr_workflow import build_dapr_workflow_client

            client = build_dapr_workflow_client()
            if client is not None:
                logger.info("optimizer.workflow_client backend=dapr_workflow")
                return client
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "optimizer.dapr_workflow_client.unavailable err=%s "
                "(falling back to the in-process GEPA path)", exc,
            )
    return build_default_client()


# ---------------------------------------------------------------------------
# Deps surface
# ---------------------------------------------------------------------------


@dataclass
class OptimizerDeps:
    """Dependency bundle the optimizer kind needs.

    Most kinds wire an LLM here; the optimizer's LLM access is mediated
    by dspy.GEPA + dspy.settings.lm, so the kind itself doesn't need a
    direct LLM port.  Instead it carries:

      * a durable-workflow client (production:
        :class:`legba.runtime.dapr_workflow.client.DaprOptimizerWorkflowClient`,
        tests: :class:`legba.runtime.dapr_workflow.gepa.InProcessWorkflowClient`
        or a stub).  Defaults to :func:`_resolve_workflow_client` which
        auto-picks based on environment.  The field name ``temporal_client``
        is historical and kept stable — it is just "the workflow client".
      * GEPA tuning knobs (max_generations, auto mode, etc.) — same
        defaults as the workflow input; descriptors override via
        ``eval.optimizer.*``.
    """

    temporal_client: Any = None
    max_generations: int = DEFAULT_MAX_GENERATIONS
    reflection_minibatch_size: int = DEFAULT_REFLECTION_MINIBATCH_SIZE
    auto_mode: str = DEFAULT_AUTO_MODE
    min_traces_required: int = DEFAULT_MIN_TRACES_REQUIRED
    min_critiques_required: int = DEFAULT_MIN_CRITIQUES_REQUIRED

    def __post_init__(self) -> None:
        if self.temporal_client is None:
            self.temporal_client = _resolve_workflow_client()


# ---------------------------------------------------------------------------
# Promotion gate — consults descriptor + historic outcomes
# ---------------------------------------------------------------------------


async def resolve_promoted_system_prompt(
    pg_pool: Any,
    analyst_id: str,
    *,
    default: str | None,
) -> str | None:
    """Return the live system prompt for ``analyst_id`` — closing the loop.

    A GEPA candidate whose ``data->>'promotion_gate'`` an operator flipped to
    ``'promoted'`` becomes the analyst's live system prompt: this returns the
    newest such candidate's ``candidate_prompt_module_text``, else ``default``
    (the descriptor/baseline prompt). This is what makes the optimizer's
    evolved instructions actually reach inference (the analyst still runs its
    own direct ``chat_complete`` — no dspy on the hot path).

    P4-T6 — the DB-edit-proof measured-delta guard. A candidate that carries a
    measured-faithfulness eval block (every unit_optimizer candidate does)
    reaches inference ONLY when the eval's ``promotable`` flag is ``'true'`` — a
    positive, non-degenerate, judge-scored, sufficiently-paired delta. So even a
    hand-flipped ``promotion_gate='promoted'`` on a degenerate/absent-delta unit
    candidate resolves to the baseline default, NOT the evolved text. Candidates
    WITHOUT an eval block (the frozen critique_proxy monolith + any legacy
    candidate) are UNAFFECTED — the eval is SQL-NULL there, so the guard is a
    no-op and their existing human-promotion path is byte-unchanged.

    JSONB PATH — the eval block persists ONE LEVEL DEEP, at
    ``data->'data'->'eval'``, NOT ``data->'eval'``. The row's ``data`` column is
    the serialized ``PromptModuleCandidatePayload``, whose OWN top-level field is
    literally named ``data`` (holding ``{diagnostics, eval, ...}``). Reading the
    wrong path (``data->'eval'``, always SQL-NULL) short-circuited the ``OR`` and
    silently made this guard a NO-OP — a hand-promoted degenerate candidate DID
    reach inference. Verified live: 0/15 candidate rows have ``data ? 'eval'``;
    the eval is at ``data->'data'->'eval'`` (P4 pre-push review C3).

    Best-effort by design: any error (no pool, query failure, malformed row)
    returns ``default`` so a promotion-lookup hiccup can never break an
    analyst run.
    """
    if pg_pool is None:
        return default
    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT data->>'candidate_prompt_module_text' AS text
                FROM analyst_outputs
                WHERE kind = 'prompt_module_candidate'
                  AND data->>'analyst_id' = $1
                  AND data->>'promotion_gate' = 'promoted'
                  -- P4-T6 DB-edit-proof guard: a MEASURED candidate (carries an
                  -- eval block) must additionally be promotable; a legacy /
                  -- critique_proxy candidate (no eval block) is unaffected.
                  -- The eval persists at data->'data'->'eval' (the row's data
                  -- column IS the serialized payload, whose own 'data' field
                  -- holds diagnostics/eval) — NOT data->'eval', which is always
                  -- NULL and short-circuited this guard into a no-op.
                  AND (
                      data->'data'->'eval' IS NULL
                      OR (data->'data'->'eval'->>'promotable') = 'true'
                  )
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(analyst_id),
            )
    except Exception as exc:  # noqa: BLE001 — never break inference on lookup
        logger.warning(
            "optimizer.promoted_prompt.lookup_failed analyst=%s err=%r",
            analyst_id, exc,
        )
        return default
    if row and row["text"]:
        logger.info(
            "optimizer.promoted_prompt.active analyst=%s chars=%d",
            analyst_id, len(row["text"]),
        )
        return str(row["text"])
    return default


# NOTE — there is NO auto-promotion path. Promotion is human-gated end to end:
# an operator flips a candidate's ``data->>'promotion_gate'`` to ``'promoted'``,
# and :func:`resolve_promoted_system_prompt` (wired at
# ``analyst_deps_builder`` — the LIVE inference path) admits that evolved prompt
# ONLY when the MEASURED delta is promotable. The single measurement gate is
# :func:`legba.runtime.dapr_workflow.gepa._delta_gates_ok` — it stamps
# ``data.eval.promotable`` at candidate write time (see ``run_method`` below)
# and rejects an absent / degenerate / NON-FINITE / judge-unavailable /
# under-sampled / sub-margin delta. An earlier ``should_auto_promote`` policy
# helper (``auto_with_threshold``) was removed (P4 pre-push review): it had ZERO
# production call sites (no descriptor declares that policy; ``run_method`` +
# ``OptimizerDeps`` carry no DB connection to reach it), so the honesty suite
# was testing dead code. The honesty contracts now pin the LIVE gate directly.


# ---------------------------------------------------------------------------
# Public entry — run_method
# ---------------------------------------------------------------------------


async def run_method(
    inputs: list[Mapping[str, Any]],
    options: Mapping[str, Any],
    deps: OptimizerDeps | None = None,
) -> AnalystMethodResult:
    """Execute one optimizer run.

    Inputs are the rows :func:`READ_SLICE` produced — joined trace +
    critique rows for the analyzed analyst.  Options carry:

      * ``analyst_id`` / ``analyst_version`` — the OPTIMIZER analyst's
        own identity (not the analyzed one).  The runtime stamps these
        from the descriptor.
      * ``analyzed_analyst_id`` — the analyst being optimized.  Pulled
        from the row when present (since READ_SLICE projects it), or
        from options as a runtime-passthrough.
      * ``parent_prompt_module_path`` — the parent module GEPA evolves.
        The actor plumbs the descriptor's declared
        ``eval.optimizer.parent_prompt_module_path`` here; an explicit
        per-run value overrides it.
      * ``analyzed_analyst_kind`` — the analyzed analyst's ``identity.kind``,
        plumbed by the actor. Only used when nothing is declared, to build
        the convention path (see
        :func:`resolve_parent_prompt_module_path`).
      * ``promotion_policy`` — override; defaults to the descriptor's
        ``eval.promotion`` value.
      * ``run_id`` — supplied by the runtime; we forward it so the
        candidate row's run_id matches the actor's trace.

    Returns an :class:`AnalystMethodResult` whose ``finding`` field
    carries a :class:`FindingPayload` with the
    :class:`PromptModuleCandidatePayload` stashed under ``data['candidate']``.
    The actor's ``_select_output_payload`` reads the kind's OUTPUT_KIND
    and the candidate payload from this slot.
    """
    deps = deps or OptimizerDeps()

    analyzed_analyst_id, analyzed_analyst_version = _resolve_analyzed_identity(
        inputs, options,
    )
    if not analyzed_analyst_id:
        return _no_op_result(
            reason="no_analyzed_analyst",
            options=options,
            training_size=len(inputs),
        )

    parent_path = resolve_parent_prompt_module_path(options)
    if not parent_path:
        # Neither a declared path nor a kind to build the convention from.
        # Refusing beats guessing: the old analyst-id convention produced a
        # module that does not exist, GEPA then optimized the placeholder text
        # it got back, and a promoted candidate could become a live analyst's
        # system prompt (K5_BLAST_RADIUS §3.2). An audit row saying so is the
        # honest outcome.
        logger.error(
            "optimizer.parent_prompt.unresolved analyzed_analyst=%s optimizer=%s "
            "— declare eval.optimizer.parent_prompt_module_path on the "
            "optimizer descriptor, or make the analyzed analyst's kind "
            "resolvable; refusing to guess a prompt module",
            analyzed_analyst_id, options.get("analyst_id"),
        )
        return _no_op_result(
            reason="unresolved_parent_prompt_module",
            options=options,
            training_size=len(inputs),
        )
    promotion_policy = str(options.get("promotion_policy") or "human_gated")

    # PASS-BY-REFERENCE (Dapr-Workflow payload-size fix)
    # --------------------------------------------------
    # The training set is up to MAX_TRAINING_ROWS (500) joined trace+critique
    # rows of ~8 KiB text each. Inlining the whole list into the workflow input
    # and serializing it across the Dapr Workflow internal gRPC channel blows
    # the default 4 MB message cap (RESOURCE_EXHAUSTED 4234332 vs 4194304) — the
    # orchestrator then never resumes and the stuck workflow leaks orphan actor
    # reminders. So for the durable (Dapr) backend we pass only a small
    # TrainingSetRef; the workflow worker re-fetches the IDENTICAL rows inside
    # the activity (materialize_training_set, mirroring deep_consult). The
    # in-process backend has no gRPC hop (+ is the test path), so it still gets
    # the rows inlined and never re-fetches — keeping that path byte-identical.
    use_in_process = isinstance(deps.temporal_client, InProcessWorkflowClient)
    training_set_ref = _build_training_set_ref(
        inputs,
        analyzed_analyst_id=str(analyzed_analyst_id),
        analyzed_analyst_version=str(analyzed_analyst_version or "") or None,
    )
    # P4-T6 — carry the OPTIMIZER analyst's OWN id + any option-supplied
    # fitness config into the workflow input. ``fitness_metric`` defaults to
    # ``critique_proxy`` (country_optimizer untouched); the worker re-resolves
    # it (+ the paired gates + parent_system_prompt_source) from THIS optimizer's
    # descriptor eval.optimizer when left at the default (single source of truth
    # on the descriptor, not re-plumbed through the actor's options). Options
    # override is the test / on-demand injection path.
    optimizer_own_id = str(options.get("analyst_id") or "")
    workflow_input = OptimizerWorkflowInput(
        analyst_id=str(analyzed_analyst_id),
        analyst_version=str(analyzed_analyst_version or ""),
        parent_prompt_module_path=str(parent_path),
        # Inline ONLY for the in-process path; the Dapr path carries the ref.
        training_set=_shape_training_set(inputs) if use_in_process else [],
        training_set_ref=None if use_in_process else training_set_ref,
        max_generations=int(deps.max_generations),
        reflection_minibatch_size=int(deps.reflection_minibatch_size),
        auto=str(deps.auto_mode),
        promotion_policy=promotion_policy,
        min_traces_required=int(deps.min_traces_required),
        min_critiques_required=int(deps.min_critiques_required),
        fitness_metric=str(options.get("fitness_metric") or "critique_proxy"),
        min_paired=int(options.get("min_paired") or 8),
        min_promote_delta=float(options.get("min_promote_delta") or 0.03),
        faithfulness_valset_max=int(options.get("faithfulness_valset_max") or 12),
        parent_system_prompt_source=str(
            options.get("parent_system_prompt_source") or ""
        ),
        optimizer_analyst_id=optimizer_own_id,
    )

    # Dispatch the workflow.  In production this returns a real
    # workflow handle whose result() blocks until the engine completes
    # the run; in test + dev (no sidecar) it returns a StubWorkflowHandle
    # with the result already computed.
    # NB: separator is '.', NOT '::'. Dapr Workflow derives each activity's
    # internal actor id as "<workflow_instance_id>::<taskId>::<gen>" and strips
    # that '::' suffix to recover the parent workflow when an activity reports
    # its result. A '::' inside the instance id itself makes that strip mis-parse
    # (it recovered just "optimizer"), so the activity result is reported to a
    # non-existent workflow → "workflow actor instance not found" → the
    # orchestrator never resumes and the GEPA run hangs forever. Keep this id
    # free of '::'.
    workflow_id = (
        f"optimizer.{analyzed_analyst_id}."
        f"{str(options.get('run_id') or uuid4())[:8]}"
    )
    workflow_result, workflow_meta = await _dispatch_workflow(
        deps.temporal_client, workflow_input, workflow_id=workflow_id,
        # The Dapr path leaves training_set empty (passed by reference); give
        # the timeout-synthesis path the true row count from the host's fetch.
        training_size_hint=len(inputs),
    )

    # G5 — surface the workflow's REAL token usage so the actor records spend
    # against country_optimizer's per-day token cap. The GEPA workflow deltas
    # ``dspy.settings.lm`` history across the compile and stamps it into
    # ``diagnostics['usage']`` (zero on the no-LLM naive / empty paths).
    usage = _usage_from_workflow_result(workflow_result)

    # The REAL method the workflow took (dspy_gepa / naive_best_of_n /
    # noop_empty_training / skipped_validation) — read from the workflow's own
    # diagnostics rather than hardcoding. A deploy without the GEPA worker (or
    # any dspy-unavailable path) silently runs the naive fallback; surfacing
    # the true method lets the UI flag it instead of falsely labelling every
    # row 'dspy_gepa'. Degrades to 'unknown' if diagnostics lacks the key.
    diagnostics = dict(workflow_result.diagnostics or {})
    actual_method = str(diagnostics.get("method") or "unknown")

    # P4-T6 — the measured-faithfulness eval record (the honest before/after
    # delta). Present ONLY for a fitness_metric=faithfulness run; the
    # critique_proxy monolith path carries no ``eval`` block, so its candidate
    # data has no ``eval`` key and the resolve_promoted_system_prompt promotable
    # guard is a no-op for it (backward compatible — the frozen monolith's
    # promotion path is unchanged).
    actual_fitness = str(diagnostics.get("fitness_metric") or "critique_proxy")
    eval_block = diagnostics.get("eval")
    if isinstance(eval_block, dict):
        # RE-STAMP ``data.eval.promotable`` at WRITE time from the measured
        # fields. This is the DB-edit-proof structural guard: even a hand-flipped
        # ``promotion_gate='promoted'`` row cannot reach inference unless
        # resolve_promoted_system_prompt sees ``data.eval.promotable='true'``, and
        # that flag is TRUE only for a positive (>= min_promote_delta), non-
        # degenerate, judge-scored, sufficiently-paired MEASURED delta.
        promotable, _preason = _delta_gates_ok(
            eval_block.get("candidate_faithfulness_mean"),
            eval_block.get("parent_faithfulness_mean"),
            eval_degenerate=bool(eval_block.get("degenerate", True)),
            judge_available=bool(eval_block.get("judge_available", False)),
            n_paired=int(eval_block.get("n_paired", 0) or 0),
            min_paired=int(eval_block.get("min_paired", 8) or 8),
            min_delta=float(eval_block.get("min_promote_delta", 0.03) or 0.03),
        )
        eval_block["promotable"] = bool(promotable)

    # Build the candidate payload.
    # D10: ``derived_from`` carries the substrate-row UUIDs the analyzed runs
    # PRODUCED (analyst_traces.output_row_refs) — real lineage-catalog rows.
    # The trace + critique ids go to ``data.derived_trace_ids`` ONLY (audit
    # slot; not walked as lineage), since analyst_traces/analyst_critiques are
    # not in the derived_from catalog.
    derived_from = _collect_derived_from(inputs)
    derived_trace_ids = _collect_derived_trace_ids(inputs)
    candidate = PromptModuleCandidatePayload(
        analyst_id=str(analyzed_analyst_id),
        analyst_version=str(analyzed_analyst_version or ""),
        parent_prompt_module_path=str(parent_path),
        candidate_prompt_module_text=str(workflow_result.candidate_prompt_module_text),
        # Snapshot the parent text the candidate was scored against so the
        # operator diff route renders current-vs-candidate with no dspy import
        # (capped to the model's 128KiB ceiling; empty on the skip path).
        parent_prompt_module_text=str(
            getattr(workflow_result, "parent_prompt_module_text", "") or "",
        )[:131072],
        training_set_size=int(workflow_result.training_set_size),
        eval_score=float(workflow_result.eval_score),
        eval_score_delta=float(workflow_result.eval_score_delta),
        gepa_generation=int(workflow_result.gepa_generation),
        promotion_gate=promotion_policy if promotion_policy in (
            "human_gated", "auto_with_threshold", "rejected",
        ) else "human_gated",
        temporal_workflow_id=str(workflow_meta.get("workflow_id") or ""),
        temporal_run_id=str(workflow_meta.get("run_id") or ""),
        data={
            "diagnostics": diagnostics,
            "method": actual_method,
            "fitness_metric": actual_fitness,
            # The honest measured record (means, delta, n_paired, judge status,
            # correctness_vs_reference, promotable). Present only for the P4-T6
            # faithfulness path; carries the DB-edit-proof ``promotable`` flag.
            **({"eval": eval_block} if isinstance(eval_block, dict) else {}),
            "derived_trace_ids": [str(u) for u in derived_trace_ids],
            "optimizer_options": {
                "max_generations": deps.max_generations,
                "auto_mode": deps.auto_mode,
                "reflection_minibatch_size": deps.reflection_minibatch_size,
            },
        },
    )

    # The actor's ``_select_output_payload`` doesn't yet know about
    # PROMPT_MODULE_CANDIDATE; it returns ``method_result.finding``.
    # We hide the candidate payload inside a FindingPayload-shaped
    # wrapper for transport, and the actor's existing dispatch will
    # validate it against PROMPT_MODULE_CANDIDATE's spec via
    # ``write_analyst_output(kind=OUTPUT_KIND, output_payload=...)``.
    #
    # To make that path work cleanly with the actor's current
    # ``_select_output_payload`` (which only branches on PREDICTION),
    # we stash the candidate in finding.data['candidate'] AND surface
    # the candidate directly via a custom ``candidate`` attribute on
    # the result.  The runtime in dapr_actors._select_output_payload
    # currently returns ``method_result.finding`` for non-PREDICTION
    # kinds, so we make the finding's body equal a serialized JSON
    # representation of the candidate.  The substrate write helper
    # then validates whatever the actor passes to
    # ``write_analyst_output`` against PromptModuleCandidatePayload's
    # schema.  See "Bug surfaced" in the L-176 report — the
    # ``_select_output_payload`` dispatch needs a clean extension
    # point for kinds beyond PREDICTION.
    summary_body = (
        f"Optimizer candidate for {analyzed_analyst_id} "
        f"(generation {workflow_result.gepa_generation}, "
        f"score {workflow_result.eval_score:.3f}, "
        f"Δ {workflow_result.eval_score_delta:+.3f})"
    )
    finding = FindingPayload(
        title=summary_body[:200],
        body=summary_body[:65000],
        confidence=max(0.0, min(1.0, float(workflow_result.eval_score))),
        evidence=[str(u) for u in derived_from[:50]],
        tags=[
            "optimizer",
            f"analyzed:{analyzed_analyst_id}",
            f"generation:{workflow_result.gepa_generation}",
            promotion_policy,
            # P4-T6 — mark the MEASURED bounded-unit experiment on the finding so
            # it is unmistakable on the trust surface (never confused with the
            # frozen critique_proxy monolith). Additive; absent for critique_proxy.
            *(
                ["unit_experiment", "fitness:faithfulness"]
                if actual_fitness == "faithfulness"
                else []
            ),
        ],
        data={
            # Hold the candidate payload dump here so the actor + a
            # future _select_output_payload extension can pivot off it
            # without re-doing the workflow.
            "candidate": candidate.model_dump(),
            "temporal_workflow_id": workflow_meta.get("workflow_id"),
            "temporal_run_id": workflow_meta.get("run_id"),
            # The REAL method the GEPA workflow took (not always dspy_gepa — a
            # worker-less deploy or dspy-unavailable path runs naive_best_of_n).
            "method": actual_method,
        },
    )
    return AnalystMethodResult(
        finding=finding,
        usage=usage,
        derived_from=derived_from,
        intermediate_steps=[
            {"phase": "read", "kind": "trace_critique_join",
             "input_rows": len(inputs)},
            {"phase": "dispatch", "kind": "temporal_workflow",
             "workflow_id": workflow_meta.get("workflow_id"),
             "in_process": workflow_meta.get("in_process", False)},
            {"phase": "candidate", "kind": "build_payload",
             "score": float(workflow_result.eval_score),
             "delta": float(workflow_result.eval_score_delta),
             "generation": int(workflow_result.gepa_generation)},
        ],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_analyzed_identity(
    inputs: list[Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    options: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Pick (analyzed_analyst_id, analyzed_analyst_version) from inputs+options.

    Inputs are the trace/critique join rows; each carries the analyzed
    analyst's id + version (the optimizer's READ_SLICE projects them).
    Options can override (used by the on-demand path).
    """
    aid = options.get("analyzed_analyst_id")
    aver = options.get("analyzed_analyst_version")
    if aid:
        return str(aid), str(aver) if aver else None
    for row in inputs:
        aid = row.get("analyzed_analyst_id") or row.get("analyst_id")
        if aid:
            aver = (
                row.get("analyzed_analyst_version")
                or row.get("analyst_version")
            )
            return str(aid), str(aver) if aver else None
    return None, None


def _build_training_set_ref(
    inputs: list[Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    *,
    analyzed_analyst_id: str,
    analyzed_analyst_version: str | None,
) -> TrainingSetRef:
    """Build the small re-fetch handle the workflow worker materializes from.

    Carries the SAME parameters READ_SLICE keyed on
    (``analyzed_analyst_id`` / version + the default
    ``DEFAULT_READ_WINDOW_DAYS`` window + ``MAX_TRAINING_ROWS`` limit — the
    host calls READ_SLICE with only ``conn``/``descriptor``/``target_filter``,
    so the window + limit ARE those defaults) PLUS ``until_ts`` — the recent-
    end anchor.

    ``until_ts`` is pinned to the NEWEST ``run_started_at`` present in the rows
    the host already fetched (not ``now``): the original READ ordered
    ``run_started_at DESC LIMIT N`` up to its ``NOW()``, so the newest fetched
    row IS that window's recent edge. Re-fetching with
    ``run_started_at <= until_ts`` therefore returns the EXACT same row set even
    if newer traces landed between READ and the worker's re-fetch (those would
    be ``> until_ts`` and excluded) — the invariant that keeps this a pure
    transport refactor. Falls back to ``now`` only when no row carries a usable
    timestamp (degenerate; the set is tiny or empty anyway).
    """
    max_ts: datetime | None = None
    for row in inputs:
        ts = row.get("run_started_at")
        if isinstance(ts, datetime) and (max_ts is None or ts > max_ts):
            max_ts = ts
    if max_ts is None:
        max_ts = datetime.now(timezone.utc)
    return TrainingSetRef(
        analyzed_analyst_id=analyzed_analyst_id,
        analyzed_analyst_version=analyzed_analyst_version,
        read_window_days=DEFAULT_READ_WINDOW_DAYS,
        limit=MAX_TRAINING_ROWS,
        until_ts=max_ts.isoformat(),
    )


def _shape_training_set(
    inputs: list[Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project the trace+critique rows into the workflow's input shape.

    Workflow needs JSON-serializable dicts (the engine's JSON
    serialization).  We keep the load-bearing fields and drop the rest.

    R-tail (2026-08-04) — LOUD on the empty-input degradation. ``input`` comes
    from ``analyst_traces.prompt_rendered``, which is NULL **by design**
    (``run_accounting``: persisting the rendered prompt would put up to the
    full 32k-token input budget on every trace; the bounded
    ``llm_calls[].prompt_sha256`` + ``prompt_chars`` digest carries the
    evidence instead, and does so on 100% of LLM-bearing traces). Live: 0 of
    187,550 rows carry it. So every training row's ``input`` is ``""`` and has
    been all along — GEPA optimizes against empty inputs and says nothing.
    That is a real defect, but it is a SCOPED one (the fix is to source the
    training input from somewhere other than a deliberately-NULL column), so
    the honest interim behaviour is to make it audible instead of silent: a
    wholly-empty training set is now a warning, not a shrug.
    """
    out: list[dict[str, Any]] = []
    empty_inputs = 0
    for row in inputs:
        input_text = row.get("input") or row.get("prompt_rendered") or ""
        gold = row.get("gold") or row.get("output_payload") or ""
        if not isinstance(input_text, str):
            input_text = str(input_text)
        if not isinstance(gold, str):
            gold = str(gold)
        if not input_text.strip():
            empty_inputs += 1
        score = row.get("critique_score")
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = None
        out.append({
            "run_id": str(row.get("run_id") or ""),
            "input": input_text[:8000],
            "gold": gold[:8000],
            "critique_score": score,
            "trace_status": row.get("trace_status"),
        })
    if out and empty_inputs == len(out):
        logger.warning(
            "optimizer.training_set.all_inputs_empty rows=%s — every training "
            "row's `input` is empty because it is read from "
            "analyst_traces.prompt_rendered, which is NULL BY DESIGN (the "
            "bounded llm_calls[].prompt_sha256 digest carries the prompt "
            "evidence instead). GEPA is optimizing against empty inputs; the "
            "training input needs a different source before any promotion off "
            "this run should be trusted.",
            len(out),
        )
    return out


def _usage_from_workflow_result(workflow_result: Any) -> dict[str, int]:
    """Lift the workflow's observed token usage into a budget-shaped dict (G5).

    The GEPA workflow stamps real reflection-LM token usage into
    ``diagnostics['usage']`` (zero on the no-LLM naive / empty-training paths).
    Returning it as the method result's ``usage`` makes
    :meth:`dapr_actors` record spend — previously this returned ``{}`` so the
    optimizer was silently exempt from its OWN per-day token cap. Defensive: a
    missing / malformed diagnostics blob yields an all-zero dict rather than
    raising (the run still produced a candidate; we just observed no tokens).
    """
    diagnostics = getattr(workflow_result, "diagnostics", None) or {}
    raw = diagnostics.get("usage") if isinstance(diagnostics, dict) else None
    if not isinstance(raw, dict):
        return {
            "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "total_tokens": 0,
        }

    def _int(key: str) -> int:
        try:
            return int(raw.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    prompt = _int("prompt_tokens")
    completion = _int("completion_tokens")
    reasoning = _int("reasoning_tokens")
    total = _int("total_tokens") or (prompt + completion + reasoning)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
    }


def _coerce_uuid(raw: Any) -> UUID | None:
    """Coerce a scalar into a :class:`UUID`, or ``None`` when unparseable."""
    if raw is None:
        return None
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _collect_derived_from(
    inputs: list[Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> list[UUID]:
    """Resolve the candidate's ``derived_from`` to REAL substrate-row UUIDs (D10).

    The lineage catalog walked by ``derived_from`` (the recursive-CTE in
    :mod:`legba.data.provenance._core`) has NO ``analyst_traces`` table, so a
    trace ``run_id`` placed in ``derived_from`` is a permanently-dangling edge —
    that was D10 (``country_optimizer`` wrote 100% broken candidate lineage).

    The correct lineage roots are the substrate rows the analyzed analyst's runs
    actually PRODUCED: ``analyst_traces.output_row_refs`` (a ``uuid[]`` of
    ``analyst_outputs`` ids), projected onto each input row by
    :func:`read_traces_and_critiques`. We dedupe (multiple traces can share a
    produced row) while preserving first-seen order.

    The trace + critique ids themselves are still recorded — but only in
    ``data.derived_trace_ids`` (see :func:`_collect_derived_trace_ids`), the
    audit slot that is NOT walked as lineage.
    """
    seen: set[UUID] = set()
    out: list[UUID] = []
    for row in inputs:
        refs = row.get("output_row_refs")
        if not refs:
            continue
        for raw in refs:
            u = _coerce_uuid(raw)
            if u is not None and u not in seen:
                seen.add(u)
                out.append(u)
    return out


def _collect_derived_trace_ids(
    inputs: list[Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> list[UUID]:
    """Collect the trace ``run_id`` + ``critique_id`` UUIDs for the audit slot.

    These point at ``analyst_traces`` / ``analyst_critiques`` rows — which are
    NOT in the ``derived_from`` lineage catalog — so they are recorded ONLY under
    the candidate's ``data.derived_trace_ids`` (kept for audit / debugging of
    which traces the GEPA run consumed), never in ``derived_from`` (D10).
    """
    seen: set[UUID] = set()
    out: list[UUID] = []
    for row in inputs:
        for key in ("run_id", "critique_id"):
            u = _coerce_uuid(row.get(key))
            if u is not None and u not in seen:
                seen.add(u)
                out.append(u)
    return out


async def _dispatch_workflow(
    temporal_client: Any,
    workflow_input: OptimizerWorkflowInput,
    *,
    workflow_id: str,
    training_size_hint: int | None = None,
) -> tuple[OptimizerWorkflowResult, dict[str, Any]]:
    """Start the workflow + await its result.

    Returns ``(result, meta)`` where ``meta`` carries the
    ``workflow_id`` + ``run_id`` for the candidate payload to record.
    Failures bubble up — the actor's failure-classification path
    handles them.

    ``training_size_hint`` — the row count the worker will re-fetch (the
    host's fetched-rows count). Used only to populate the synthesized
    timeout result's ``training_set_size`` since, on the pass-by-reference
    Dapr path, ``workflow_input.training_set`` is intentionally empty.
    """
    handle = await temporal_client.start_optimizer_workflow(
        workflow_input, workflow_id=workflow_id,
    )
    in_process = type(temporal_client).__name__ == "InProcessWorkflowClient"
    timeout_s = _dispatch_timeout_s()
    timeout_size = (
        training_size_hint
        if training_size_hint is not None
        else len(workflow_input.training_set)
    )
    try:
        result = await asyncio.wait_for(handle.result(), timeout=timeout_s)
    except (asyncio.TimeoutError, TimeoutError):
        # The GEPA workflow exceeded its wall-clock bound — the silent-death
        # class: compile() hangs → result() never returns → run_method never
        # completes → NO analyst_trace is written, so the leg looks dormant
        # rather than broken. Synthesize a timeout result so run_method
        # finishes and the actor records a trace: silence becomes an
        # observable 'workflow_timeout' row. The orphaned worker-side workflow
        # is bounded independently by the per-LM-call timeout in dspy_lm.
        logger.warning(
            "optimizer.dispatch.timeout workflow_id=%s timeout=%ss "
            "— recording a timeout trace instead of hanging",
            workflow_id, timeout_s,
        )
        timeout_result = OptimizerWorkflowResult(
            candidate_prompt_module_text=(
                f"<<workflow_timeout: exceeded {timeout_s}s>>"
            ),
            training_set_size=timeout_size,
            eval_score=0.0,
            eval_score_delta=0.0,
            gepa_generation=0,
            diagnostics={
                "method": "workflow_timeout",
                "reason": f"workflow exceeded {timeout_s}s",
                "usage": {
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "reasoning_tokens": 0, "total_tokens": 0,
                },
            },
        )
        return timeout_result, {
            "workflow_id": getattr(handle, "id", workflow_id),
            "run_id": getattr(handle, "result_run_id", ""),
            "in_process": in_process,
            "timed_out": True,
        }
    return result, {
        "workflow_id": getattr(handle, "id", workflow_id),
        "run_id": getattr(handle, "result_run_id", ""),
        "in_process": in_process,
    }


def _no_op_result(
    *,
    reason: str,
    options: Mapping[str, Any],
    training_size: int,
) -> AnalystMethodResult:
    """Emit an audit-only result when the optimizer has nothing to do."""
    analyst_id = str(options.get("analyst_id") or "optimizer")
    finding = FindingPayload(
        title=f"Optimizer noop: {reason}"[:200],
        body=(
            f"Optimizer for {analyst_id} ran but emitted no candidate: {reason}. "
            f"Training rows examined: {training_size}."
        )[:65000],
        confidence=0.0,
        evidence=[],
        tags=["optimizer", "noop", reason],
        data={
            "reason": reason,
            "training_size": training_size,
            "method": "noop",
        },
    )
    return AnalystMethodResult(
        finding=finding,
        usage={},
        derived_from=[],
        intermediate_steps=[
            {"phase": "noop", "reason": reason, "training_size": training_size},
        ],
    )


__all__ = [
    "DEFAULT_MAX_GENERATIONS",
    "DEFAULT_MIN_CRITIQUES_REQUIRED",
    "DEFAULT_MIN_TRACES_REQUIRED",
    "DEFAULT_READ_WINDOW_DAYS",
    "DEFAULT_REFLECTION_MINIBATCH_SIZE",
    "HANDLER_VERSION",
    "KIND_NAME",
    "MAX_TRAINING_ROWS",
    "OUTPUT_KIND",
    "OptimizerDeps",
    "PROMPT_MODULE_CONVENTION",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "SCHEMA_VERSION",
    "build_prompt_module",
    "convention_parent_prompt_module_path",
    "read_traces_and_critiques",
    "resolve_parent_prompt_module_path",
    "run_method",
    "_collect_derived_from",
    "_collect_derived_trace_ids",
]
