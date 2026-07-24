# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M17 / M18(b) — cross_correlator + composition write-path head fold.

DB-free unit tests over ``fold_prior_correlation_heads`` (the M17 correlator
supersession + blind_spot decay) and ``fold_prior_composition_heads`` (the
world_assessor synchronous write-time supersession the dapr_actors write path
calls inline). A small in-memory ``_FakeConn`` models the two tables the folds
touch (``analyst_outputs`` heads + ``finding_supersessions`` edges) and dispatches
on the fold's SQL — no substrate container, mirroring the no-mocks-past-the-
boundary convention (the boundary here is asyncpg's ``fetch``/``execute``/
``fetchval``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic_handlers.finding_supersession import (
    fold_prior_composition_heads,
    fold_prior_correlation_heads,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolved_sig(row: dict[str, Any]) -> str | None:
    data = row.get("data") or {}
    inner = data.get("data") if isinstance(data, dict) else None
    if isinstance(inner, dict) and inner.get("situation_signature"):
        return str(inner["situation_signature"])
    return str(data["situation_signature"]) if data.get("situation_signature") else None


def _resolved_ctype(row: dict[str, Any]) -> str | None:
    data = row.get("data") or {}
    inner = data.get("data") if isinstance(data, dict) else None
    if isinstance(inner, dict) and inner.get("correlation_type"):
        return str(inner["correlation_type"])
    return str(data["correlation_type"]) if data.get("correlation_type") else None


def _xcorr_targets(row: dict[str, Any]) -> list[str]:
    data = row.get("data") or {}
    inner = data.get("data") if isinstance(data, dict) else None
    if isinstance(inner, dict) and isinstance(inner.get("xcorr_targets"), list):
        return [str(t) for t in inner["xcorr_targets"]]
    return []


class _FakeConn:
    """Minimal asyncpg-conn stand-in modeling the two tables the folds touch."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {r["id"]: r for r in rows}
        self.edges: set[tuple[Any, Any]] = set()
        self.edge_log: list[dict[str, Any]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        if "superseded_by = $2" in sql:
            # _link_supersession UPDATE: (superseded_id, superseding_id, sig)
            row = self.rows.get(args[0])
            if row is not None:
                row["superseded_by"] = args[1]
                row["superseded_at"] = _now()
                row["situation_signature"] = args[2]
        elif "SET situation_signature = $2" in sql:
            # stamp the new head's column: (id, sig)
            row = self.rows.get(args[0])
            if row is not None and row.get("situation_signature") != args[1]:
                row["situation_signature"] = args[1]
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        analyst_id, new_head_id = args[0], args[1]
        if "correlation_type" in sql:
            ttl = int(args[2])
            cutoff = _now() - timedelta(hours=ttl)
            out: list[dict[str, Any]] = []
            for h in self.rows.values():
                if not (
                    h.get("analyst_id") == analyst_id
                    and h.get("kind") == "finding"
                    and h.get("superseded_by") is None
                    and h["id"] != new_head_id
                    and h.get("produced_at") is not None
                    and h["produced_at"] < cutoff
                    and _resolved_ctype(h) == "blind_spot"
                ):
                    continue
                # scope-revisited EXISTS: a NEWER live head whose target-set is a
                # SUPERSET of (or equal to) H's target-set. jsonb `<@` semantics =
                # subset-of, so h_targets <= n_targets.
                h_t = set(_xcorr_targets(h))
                revisited = any(
                    n["id"] != h["id"]
                    and n.get("analyst_id") == analyst_id
                    and n.get("kind") == "finding"
                    and n.get("superseded_by") is None
                    and n.get("produced_at") is not None
                    and n["produced_at"] > h["produced_at"]
                    and h_t <= set(_xcorr_targets(n))
                    for n in self.rows.values()
                )
                if revisited:
                    out.append({"id": h["id"]})
            return out
        raw_signature = args[2]
        return [
            {"id": r["id"]}
            for r in self.rows.values()
            if r.get("analyst_id") == analyst_id
            and r.get("kind") == "finding"
            and r.get("superseded_by") is None
            and r["id"] != new_head_id
            and _resolved_sig(r) == raw_signature
        ]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "finding_supersessions" in sql:
            key = (args[0], args[1])
            if key in self.edges:
                return None  # ON CONFLICT DO NOTHING
            self.edges.add(key)
            self.edge_log.append({
                "superseded": args[0], "superseding": args[1],
                "situation_signature": args[2], "reason": args[3],
                "produced_by": args[5],
            })
            return args[0]
        return None


def _head(
    analyst_id: str,
    sig: str,
    *,
    ctype: str,
    age_hours: float,
    targets: tuple[str, ...] = (),
) -> dict[str, Any]:
    hid = uuid4()
    return {
        "id": hid,
        "analyst_id": analyst_id,
        "kind": "finding",
        "superseded_by": None,
        "superseded_at": None,
        "situation_signature": None,
        "data": {"data": {
            "situation_signature": sig,
            "correlation_type": ctype,
            "xcorr_targets": list(targets),
        }},
        "produced_at": _now() - timedelta(hours=age_hours),
    }


# ---------------------------------------------------------------------------
# M17 — same-signature supersession
# ---------------------------------------------------------------------------


async def test_fold_correlation_supersedes_same_signature() -> None:
    sig = "xcorr:blind_spot:country_watch_ir"
    prior = _head("cross_correlator", sig, ctype="blind_spot", age_hours=12)
    new = _head("cross_correlator", sig, ctype="blind_spot", age_hours=0)
    conn = _FakeConn([prior, new])

    folded, decayed = await fold_prior_correlation_heads(
        conn, analyst_id="cross_correlator", raw_signature=sig,
        new_head_id=new["id"], blind_spot_ttl_hours=72,
    )
    assert (folded, decayed) == (1, 0)
    assert conn.rows[prior["id"]]["superseded_by"] == new["id"]
    assert conn.rows[new["id"]]["superseded_by"] is None  # the new head stays live
    # An audit edge was mirrored.
    assert (prior["id"], new["id"]) in conn.edges


async def test_fold_correlation_leaves_a_different_signature_head_live() -> None:
    """A head with a DIFFERENT signature is not the same correlation — untouched."""
    keep = _head(
        "cross_correlator", "xcorr:blind_spot:country_g20_us",
        ctype="blind_spot", age_hours=6,
    )
    new = _head(
        "cross_correlator", "xcorr:contradiction:country_g20_tr",
        ctype="contradiction", age_hours=0,
    )
    conn = _FakeConn([keep, new])
    folded, decayed = await fold_prior_correlation_heads(
        conn, analyst_id="cross_correlator",
        raw_signature="xcorr:contradiction:country_g20_tr",
        new_head_id=new["id"], blind_spot_ttl_hours=72,
    )
    # keep is recent (6h < 72h TTL) → not decayed; different sig → not folded.
    assert (folded, decayed) == (0, 0)
    assert conn.rows[keep["id"]]["superseded_by"] is None


# ---------------------------------------------------------------------------
# M17 — blind_spot decay
# ---------------------------------------------------------------------------


async def test_fold_correlation_stale_gap_stays_live_when_scope_not_revisited() -> None:
    """Adversarial FIX #1 — a >TTL blind_spot whose SCOPE was NOT revisited by any
    newer head MUST stay LIVE. run_method emits one finding per run by strict
    priority, so a still-open gap that keeps getting preempted is never re-emitted;
    age alone must NOT close it. Here the new head's scope {US} does not cover the
    stale gap's scope {IR}, so the Iran gap is a standing, un-revisited warning."""
    stale = _head(
        "cross_correlator", "xcorr:blind_spot:country_watch_ir",
        ctype="blind_spot", age_hours=200, targets=("country_watch_ir",),
    )
    new = _head(
        "cross_correlator", "xcorr:contradiction:country_g20_us",
        ctype="contradiction", age_hours=0, targets=("country_g20_us",),
    )
    conn = _FakeConn([stale, new])
    folded, decayed = await fold_prior_correlation_heads(
        conn, analyst_id="cross_correlator",
        raw_signature="xcorr:contradiction:country_g20_us",
        new_head_id=new["id"], blind_spot_ttl_hours=72,
    )
    assert (folded, decayed) == (0, 0)
    assert conn.rows[stale["id"]]["superseded_by"] is None, (
        "a still-open, un-revisited coverage gap must not be closed by age alone"
    )


async def test_fold_correlation_decays_stale_gap_when_scope_revisited() -> None:
    """A >TTL blind_spot IS decayed when a NEWER head revisits its scope (target-set
    superset) without re-raising the gap — evidence the correlator looked again."""
    stale = _head(
        "cross_correlator", "xcorr:blind_spot:country_watch_ir",
        ctype="blind_spot", age_hours=200, targets=("country_watch_ir",),
    )
    fresh = _head(
        "cross_correlator", "xcorr:blind_spot:country_g20_cn",
        ctype="blind_spot", age_hours=10,  # < 72h TTL → not a candidate
        targets=("country_g20_cn",),
    )
    # New head covers a SUPERSET of the stale gap's scope ({IR} ⊆ {IR, TR}).
    new = _head(
        "cross_correlator", "xcorr:contradiction:country_g20_tr,country_watch_ir",
        ctype="contradiction", age_hours=0,
        targets=("country_g20_tr", "country_watch_ir"),
    )
    conn = _FakeConn([stale, fresh, new])
    folded, decayed = await fold_prior_correlation_heads(
        conn, analyst_id="cross_correlator",
        raw_signature="xcorr:contradiction:country_g20_tr,country_watch_ir",
        new_head_id=new["id"], blind_spot_ttl_hours=72,
    )
    assert folded == 0 and decayed == 1
    assert conn.rows[stale["id"]]["superseded_by"] == new["id"]
    assert conn.rows[fresh["id"]]["superseded_by"] is None  # < TTL → held


async def test_fold_correlation_global_gap_decays_on_any_newer_head() -> None:
    """A GLOBAL (empty target-set) stale blind_spot has an empty scope, which is a
    subset of every newer head's scope — so any newer live head revisits it. This
    is intentional: a target-less 'insufficient data' gap is stale once the
    correlator produces any newer substantive output."""
    stale = _head(
        "cross_correlator", "xcorr:blind_spot:_global",
        ctype="blind_spot", age_hours=200, targets=(),
    )
    new = _head(
        "cross_correlator", "xcorr:contradiction:country_g20_us",
        ctype="contradiction", age_hours=0, targets=("country_g20_us",),
    )
    conn = _FakeConn([stale, new])
    _, decayed = await fold_prior_correlation_heads(
        conn, analyst_id="cross_correlator",
        raw_signature="xcorr:contradiction:country_g20_us",
        new_head_id=new["id"], blind_spot_ttl_hours=72,
    )
    assert decayed == 1
    assert conn.rows[stale["id"]]["superseded_by"] == new["id"]


async def test_fold_correlation_ttl_zero_disables_decay() -> None:
    """TTL=0 disables decay even when the scope WAS revisited (a superset new head)."""
    stale = _head(
        "cross_correlator", "xcorr:blind_spot:x", ctype="blind_spot",
        age_hours=500, targets=("x",),
    )
    new = _head(
        "cross_correlator", "xcorr:contradiction:x",
        ctype="contradiction", age_hours=0, targets=("x",),
    )
    conn = _FakeConn([stale, new])
    folded, decayed = await fold_prior_correlation_heads(
        conn, analyst_id="cross_correlator", raw_signature="xcorr:contradiction:x",
        new_head_id=new["id"], blind_spot_ttl_hours=0,
    )
    assert decayed == 0
    assert conn.rows[stale["id"]]["superseded_by"] is None


# ---------------------------------------------------------------------------
# M17 — guards + idempotency
# ---------------------------------------------------------------------------


async def test_fold_correlation_noop_on_composition_signature() -> None:
    """The prefix guard: a composition signature must never route through the
    correlation fold (and vice-versa)."""
    head = _head(
        "world_assessor", "composition:world_assessor:world",
        ctype="blind_spot", age_hours=0,
    )
    conn = _FakeConn([head])
    folded, decayed = await fold_prior_correlation_heads(
        conn, analyst_id="world_assessor",
        raw_signature="composition:world_assessor:world",
        new_head_id=head["id"], blind_spot_ttl_hours=72,
    )
    assert (folded, decayed) == (0, 0)


async def test_fold_correlation_missing_args_noop() -> None:
    conn = _FakeConn([])
    assert await fold_prior_correlation_heads(
        conn, analyst_id=None, raw_signature="xcorr:blind_spot:x",
        new_head_id=uuid4(),
    ) == (0, 0)


async def test_fold_correlation_idempotent() -> None:
    sig = "xcorr:blind_spot:_global"
    prior = _head("cross_correlator", sig, ctype="blind_spot", age_hours=12)
    new = _head("cross_correlator", sig, ctype="blind_spot", age_hours=0)
    conn = _FakeConn([prior, new])
    first = await fold_prior_correlation_heads(
        conn, analyst_id="cross_correlator", raw_signature=sig,
        new_head_id=new["id"], blind_spot_ttl_hours=72,
    )
    second = await fold_prior_correlation_heads(
        conn, analyst_id="cross_correlator", raw_signature=sig,
        new_head_id=new["id"], blind_spot_ttl_hours=72,
    )
    assert first == (1, 0)
    assert second == (0, 0)  # prior already superseded → nothing to fold


# ---------------------------------------------------------------------------
# M18(b) — world_assessor synchronous write-time supersession
# ---------------------------------------------------------------------------


async def test_world_composition_fold_retires_prior_same_signature_head() -> None:
    """The world_assessor write path retires the prior same-signature head AT
    WRITE time (this fold is called inline by dapr_actors right after the row
    lands), so two sequential world snapshots never both stay live."""
    sig = "composition:world_assessor:world"
    prior = _head("world_assessor", sig, ctype="blind_spot", age_hours=12)
    new = _head("world_assessor", sig, ctype="blind_spot", age_hours=0)
    conn = _FakeConn([prior, new])

    closed = await fold_prior_composition_heads(
        conn, analyst_id="world_assessor", raw_signature=sig, new_head_id=new["id"],
    )
    assert closed == 1
    assert conn.rows[prior["id"]]["superseded_by"] == new["id"]
    assert conn.rows[new["id"]]["superseded_by"] is None
    # The new head's signature column is stamped (sit: prefix) for latest-read.
    assert conn.rows[new["id"]]["situation_signature"] == f"sit:{sig}"
