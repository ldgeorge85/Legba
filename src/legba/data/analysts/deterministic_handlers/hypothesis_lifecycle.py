# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``hypothesis_lifecycle`` sub-handler — the hypotheses producer + evidence tester.

Piece 3, Task D (anchor §5: "hypotheses … forward claims, tested vs evidence").
The write path is ALREADY LIVE — this handler is thin orchestration over it
(anchor §7 discipline rule): it reuses :func:`legba.data.provenance.writes.
write_hypothesis` (→ ``OutputKind.HYPOTHESIS`` → the ``hypotheses`` table) for the
EMIT step and a direct ``UPDATE public.hypotheses`` (the ``fact_decay`` pattern)
for the TEST step. NO new write plumbing.

Shape mirrors :mod:`situation_clustering` (the precedent): a ``deterministic``
META sub-handler that reads the shared analyst-output / situation pool via
``deps.pg_pool``, does its OWN substrate writes (hypothesis rows land directly in
the ``hypotheses`` table), and returns a per-run summary :class:`FindingPayload`
as the cadence receipt. The deterministic dispatcher persists exactly one
``analyst_output`` per run — the FINDING summary — while the hypothesis rows are
side-written here, exactly as situation_clustering side-writes situation rows.

Two responsibilities in one idempotent, forward-progressing sweep (NEVER deletes):

  1. EMIT new hypotheses (forward claims).
     For each ``active`` situation whose ``intensity_score`` is RISING vs the
     intensity recorded on its standing hypothesis (or which clears an initial
     notability floor when none exists yet), materialize / refresh a hypothesis
     ``thesis="<topic> will escalate over the next <horizon>"`` keyed on
     ``(situation_id, analyst_id)``. A re-run with no fresh rise UPDATEs the
     standing row's intensity snapshot rather than inserting a duplicate.

  2. TEST standing hypotheses against LATER evidence.
     For each WORKING (active / supported / weakened, resolved_outcome IS NULL)
     hypothesis, fetch findings produced AFTER the hypothesis ``produced_at``
     that link to its situation, classify each as supporting (situation intensity
     rose further) or refuting (it fell), recompute
     ``evidence_balance = len(supporting) - len(refuting)``, and walk the WORKING
     states ``-> supported`` (balance >= +K) / ``-> weakened`` (balance <= -K).
     DQ P6: intensity drift is a SELF-CONSISTENCY proxy, so it NEVER reaches a
     TERMINAL confirmed/refuted — that is reserved for the exogenous
     subsequent_facts resolver / operator. Forward-claim semantics: evidence
     produced BEFORE the hypothesis is never counted (only LATER evidence tests
     it).

Output ``data`` keys (the per-run summary FindingPayload):
    hypotheses_created   int — new forward-claim rows written
    hypotheses_updated   int — standing rows whose snapshot/evidence was refreshed
    confirmed            int — rows transitioned active -> confirmed this sweep
    refuted              int — rows transitioned active -> refuted this sweep
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from ...provenance import AnalystContext, HypothesisPayload, write_hypothesis
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "hypothesis_lifecycle"

# --- emit tunables ---------------------------------------------------------
# Only `active` situations this fresh are eligible to spawn a forward claim
# (older settled situations aren't a live escalation story).
_SITUATION_LOOKBACK_DAYS = 14
# A situation must clear this intensity floor before its FIRST hypothesis is
# emitted — keeps trivial single-finding situations from each spawning a claim.
_EMIT_INTENSITY_FLOOR = 1.5
# Forward-claim horizon phrasing.
_HORIZON = "the next 14 days"

# --- test tunables ---------------------------------------------------------
# evidence_balance thresholds for status transitions.
_CONFIRM_K = 2
_REFUTE_K = 2
# A later finding linked to the situation counts as support when the
# situation's intensity has risen by at least this delta since the hypothesis
# snapshot, refute when it has fallen by at least this delta.
_INTENSITY_MOVE_EPS = 0.25

# DQ P6 (2026-07-03) — RESOLUTION-CIRCULARITY CAP. Intensity drift is a
# SELF-CONSISTENCY proxy (the same situation-intensity that spawned the claim
# also "confirms" it), so this handler must NEVER walk a hypothesis to a TERMINAL
# confirmed/refuted state on drift alone — that minted 619 endogenous
# resolutions (89% future-dated "will" claims confirmed months before their
# horizon). It caps drift moves at the WORKING states 'supported' / 'weakened';
# a TERMINAL confirmed/refuted is reserved for the EXOGENOUS subsequent_facts
# resolver (grades against facts produced AFTER the claim) or an operator. The
# working states stay in the re-test pool so a hypothesis can move between them
# as evidence accumulates, but the circular terminal is closed off.
_WORKING_SUPPORTED = "supported"
_WORKING_WEAKENED = "weakened"
# Statuses this handler continues to RE-TEST each sweep (working, non-terminal).
_TESTABLE_STATUSES = ("active", _WORKING_SUPPORTED, _WORKING_WEAKENED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_list(raw: Any) -> list[Any]:
    """Coerce a jsonb column value to a Python list.

    asyncpg returns jsonb columns as raw JSON *strings* unless a codec is
    registered, so ``diagnostic_evidence`` arrives as ``str`` from a plain
    ``conn.fetch``. Parse it back; tolerate already-decoded lists and junk.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _thesis_for(name: str) -> str:
    label = (name or "this situation").strip() or "this situation"
    return f"{label} will escalate over {_HORIZON}"[:4096]


def _counter_thesis_for(name: str) -> str:
    label = (name or "this situation").strip() or "this situation"
    return f"{label} will de-escalate or remain stable over {_HORIZON}"[:4096]


# ---------------------------------------------------------------------------
# EMIT — forward claims over rising situations
# ---------------------------------------------------------------------------


async def _emit_forward_claims(
    pool: Any,
    *,
    analyst_ctx_factory,
    analyst_id: str,
    publish_fn: Any | None,
) -> tuple[int, int]:
    """Emit / refresh forward-claim hypotheses over rising situations.

    Returns ``(created, updated)``. Idempotent on ``(situation_id, analyst_id)``.
    """
    created = 0
    updated = 0
    async with pool.acquire() as conn:
        situations = await conn.fetch(
            f"""
            SELECT id, name, intensity_score, derived_from
            FROM situations
            WHERE status = 'active'
              AND last_event_at > NOW() - INTERVAL '{int(_SITUATION_LOOKBACK_DAYS)} days'
            ORDER BY intensity_score DESC
            """,
        )
        for s in situations:
            sit_id: UUID = s["id"]
            intensity = float(s["intensity_score"] or 0.0)
            existing = await conn.fetchrow(
                """
                SELECT id, diagnostic_evidence, status
                FROM hypotheses
                WHERE situation_id = $1 AND analyst_id = $2
                ORDER BY produced_at DESC
                LIMIT 1
                """,
                sit_id, analyst_id,
            )

            if existing is None:
                # First claim: only emit once the situation clears the floor.
                if intensity < _EMIT_INTENSITY_FLOOR:
                    continue
                derived = list(s["derived_from"] or [])
                payload = HypothesisPayload(
                    thesis=_thesis_for(s["name"]),
                    counter_thesis=_counter_thesis_for(s["name"]),
                    situation_id=sit_id,
                    supporting_signals=list(derived),
                    refuting_signals=[],
                    evidence_balance=0,
                    status="active",
                    diagnostic_evidence=[
                        {"intensity_at_emit": intensity, "at": _now().isoformat()}
                    ],
                )
                _, dlq = await write_hypothesis(
                    conn,
                    analyst_ctx=analyst_ctx_factory(),
                    payload=payload,
                    derived_from=list(derived),
                    publish_fn=publish_fn,
                )
                if dlq is None:
                    created += 1
                else:
                    logger.warning(
                        "hypothesis_lifecycle.emit.dlq situation=%s err=%s",
                        sit_id, getattr(dlq, "error", "?"),
                    )
                continue

            # A standing hypothesis exists — refresh its intensity snapshot so
            # the TEST step (and the next EMIT sweep) can see the trajectory.
            # Idempotent: UPDATE, never a duplicate INSERT. Only `active` rows
            # are refreshed (confirmed/refuted are terminal for v1).
            if existing["status"] != "active":
                continue
            audit = _as_list(existing["diagnostic_evidence"])
            audit.append({"intensity_observed": intensity, "at": _now().isoformat()})
            audit = audit[-20:]  # bound the audit trail
            await conn.execute(
                """
                UPDATE hypotheses
                SET diagnostic_evidence = $2::jsonb, last_evaluated_cycle =
                    COALESCE(last_evaluated_cycle, 0) + 1, updated_at = NOW()
                WHERE id = $1
                """,
                existing["id"], json.dumps(audit),
            )
            updated += 1
    return created, updated


# ---------------------------------------------------------------------------
# TEST — standing hypotheses vs LATER evidence
# ---------------------------------------------------------------------------


def _classify_move(intensity_now: float, intensity_at_emit: float) -> int:
    """+1 support (rose), -1 refute (fell), 0 neutral (flat)."""
    delta = intensity_now - intensity_at_emit
    if delta >= _INTENSITY_MOVE_EPS:
        return 1
    if delta <= -_INTENSITY_MOVE_EPS:
        return -1
    return 0


def _intensity_at_emit(diagnostic_evidence: list[Any]) -> float | None:
    """Recover the intensity snapshot stamped at emit time, if any."""
    for entry in diagnostic_evidence or []:
        if isinstance(entry, dict) and "intensity_at_emit" in entry:
            try:
                return float(entry["intensity_at_emit"])
            except (TypeError, ValueError):
                return None
    return None


async def _test_standing_hypotheses(pool: Any, *, analyst_id: str) -> tuple[int, int, int]:
    """Test each ``active`` hypothesis vs LATER evidence.

    Returns ``(updated, confirmed, refuted)``. Direct ``UPDATE`` on the
    pre-existing rows (the ``fact_decay`` pattern); never inserts/deletes.
    """
    updated = 0
    confirmed = 0
    refuted = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, situation_id, diagnostic_evidence, supporting_signals,
                   refuting_signals, produced_at
            FROM hypotheses
            WHERE analyst_id = $1 AND status = ANY($2::text[])
              AND situation_id IS NOT NULL
              AND resolved_outcome IS NULL
            """,
            analyst_id, list(_TESTABLE_STATUSES),
        )
        for h in rows:
            sit_id = h["situation_id"]
            base = _intensity_at_emit(_as_list(h["diagnostic_evidence"]))
            if base is None:
                continue
            # Current situation intensity (the situation handler recency-weights
            # it, so a still-rising situation reads higher than at emit).
            sit = await conn.fetchrow(
                "SELECT intensity_score FROM situations WHERE id = $1", sit_id,
            )
            if sit is None:
                continue
            intensity_now = float(sit["intensity_score"] or 0.0)

            # LATER evidence ONLY: findings on this situation produced AFTER the
            # hypothesis. Forward-claim semantics — the findings that spawned the
            # claim (produced at/before it) must never confirm it.
            later = await conn.fetch(
                """
                SELECT ao.id
                FROM analyst_outputs ao
                JOIN situations s ON s.id = $1
                WHERE ao.kind = 'finding'
                  AND ao.produced_at > $2
                  AND ao.id = ANY (s.derived_from)
                """,
                sit_id, h["produced_at"],
            )
            later_ids = [r["id"] for r in later]
            if not later_ids:
                continue

            move = _classify_move(intensity_now, base)
            supporting = list(h["supporting_signals"] or [])
            refuting = list(h["refuting_signals"] or [])
            existing_supp = set(supporting)
            existing_refu = set(refuting)
            changed = False
            if move > 0:
                for lid in later_ids:
                    if lid not in existing_supp:
                        supporting.append(lid)
                        existing_supp.add(lid)
                        changed = True
            elif move < 0:
                for lid in later_ids:
                    if lid not in existing_refu:
                        refuting.append(lid)
                        existing_refu.add(lid)
                        changed = True
            if not changed:
                continue

            balance = len(supporting) - len(refuting)
            # DQ P6 — cap at WORKING states; NEVER terminal from drift alone. A
            # crossed threshold marks the claim 'supported'/'weakened' (still
            # re-tested next sweep); the EXOGENOUS subsequent_facts resolver /
            # operator owns the terminal confirmed/refuted (and resolved_outcome).
            new_status = "active"
            if balance >= _CONFIRM_K:
                new_status = _WORKING_SUPPORTED
            elif balance <= -_REFUTE_K:
                new_status = _WORKING_WEAKENED

            audit = _as_list(h["diagnostic_evidence"])
            audit.append({
                "test": {
                    "intensity_now": intensity_now,
                    "intensity_at_emit": base,
                    "move": move,
                    "balance": balance,
                },
                "at": _now().isoformat(),
            })
            audit = audit[-20:]

            await conn.execute(
                """
                UPDATE hypotheses
                SET supporting_signals = $2, refuting_signals = $3,
                    evidence_balance = $4, status = $5,
                    diagnostic_evidence = $6::jsonb,
                    last_evaluated_cycle = COALESCE(last_evaluated_cycle, 0) + 1,
                    updated_at = NOW()
                WHERE id = $1
                """,
                h["id"], supporting, refuting, balance, new_status,
                json.dumps(audit),
            )
            updated += 1
            confirmed += new_status == _WORKING_SUPPORTED
            refuted += new_status == _WORKING_WEAKENED
    return updated, confirmed, refuted


# ---------------------------------------------------------------------------
# Summary finding (the cadence receipt)
# ---------------------------------------------------------------------------


def _build_finding(
    *, created: int, refreshed: int, tested: int, supported: int, weakened: int,
    target_id: str | None,
) -> FindingPayload:
    # DQ P6 — the receipt reports WORKING-state moves (supported/weakened), NOT
    # terminal confirmations: this handler never confirms/refutes from intensity
    # drift (that would be self-consistency). Terminal resolution is the
    # exogenous subsequent_facts resolver's job.
    title = (
        f"Hypothesis lifecycle: {created} created, "
        f"{supported} supported, {weakened} weakened"
    )
    if target_id:
        title = f"{title} for {target_id}"
    body = "\n".join([
        f"hypotheses_created={created}",
        f"hypotheses_updated={refreshed + tested}",
        f"supported={supported}",
        f"weakened={weakened}",
    ])
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "hypotheses_created": created,
            "hypotheses_updated": refreshed + tested,
            "supported": supported,
            "weakened": weakened,
        },
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Reads/writes via ``deps.pg_pool`` (the situation_clustering precedent). The
    ``deps=None`` unit-test path skips the substrate work and returns a zero
    summary, matching the sibling handlers' synthetic path.
    """
    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    analyst_version = str(options.get("analyst_version") or "")
    target_id = options.get("target_id")
    run_id = options.get("run_id")

    def _make_ctx() -> AnalystContext:
        rid = run_id
        if not isinstance(rid, UUID):
            try:
                rid = UUID(str(rid))
            except (TypeError, ValueError):
                from uuid import uuid4

                rid = uuid4()
        return AnalystContext(
            analyst_id=analyst_id,
            analyst_version=analyst_version,
            run_id=rid,
            target_id=str(target_id) if target_id else None,
        )

    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    publish_fn = getattr(deps, "nats_publish", None) if deps is not None else None

    created = refreshed = tested = supported = weakened = 0
    if pool is not None:
        try:
            created, refreshed = await _emit_forward_claims(
                pool,
                analyst_ctx_factory=_make_ctx,
                analyst_id=analyst_id,
                publish_fn=publish_fn,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hypothesis_lifecycle.emit_failed err=%s", exc)
        try:
            tested, supported, weakened = await _test_standing_hypotheses(
                pool, analyst_id=analyst_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hypothesis_lifecycle.test_failed err=%s", exc)

    finding = _build_finding(
        created=created, refreshed=refreshed, tested=tested,
        supported=supported, weakened=weakened, target_id=target_id,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]
