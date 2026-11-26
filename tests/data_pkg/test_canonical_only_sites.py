# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The canonical-only clause is spelled once (``CANONICAL_ONLY_SQL``).

Eight modules carried the clause as an inline literal. Each now reads it from
``data/signal_slice_scope.py`` (the aliased ``s.`` form through
``canonical_only_sql_for``). Two guards:

* every site's RENDERED statement is pinned by sha256 to the text it rendered
  before the sweep (byte-identical; the one exception is metrics_api, whose
  bare ``WHERE a OR b`` gained the constant's parentheses - the only predicate
  in the statement, so the rows are the same);
* no module under ``src/legba`` spells the clause inline in SQL again.

Pure: the statements are captured off a recording connection, no database.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from legba.data import _geo_routing
# The analysts package first: research_evidence imported cold is a cycle.
from legba.data.analysts.deterministic_handlers import (
    _event_candidates,
    cross_source_coalesce,
    geo_convergence_scan,
)
from legba.data import research_evidence
from legba.data.registry import metrics_api
from legba.data.registry.substrate_reads_api import build_substrate_reads_router
from legba.data.schemas.source import Subscription
from legba.data.signal_slice_scope import (
    CANONICAL_ONLY_SQL,
    canonical_only_sql_for,
)
from legba.runtime.subscription.filter import build_sql_filter


class _Stop(Exception):
    pass


class _Recorder:
    """A pool + connection that records every statement it is handed and
    stops the caller once the canonical-only statement has been seen."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def acquire(self) -> "_Recorder":
        return self

    async def __aenter__(self) -> "_Recorder":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def _seen(self, sql: str) -> None:
        self.statements.append(sql)
        if "canonical_signal_id" in sql:
            raise _Stop

    async def fetch(self, sql: str, *_a: Any) -> list:
        self._seen(sql)
        return []

    async def fetchval(self, sql: str, *_a: Any) -> Any:
        self._seen(sql)
        return 0

    async def fetchrow(self, sql: str, *_a: Any) -> Any:
        self._seen(sql)
        return None


def _captured(coro_factory) -> str:
    rec = _Recorder()
    try:
        asyncio.run(coro_factory(rec))
    except _Stop:
        pass
    hits = [s for s in rec.statements if "canonical_signal_id" in s]
    assert len(hits) == 1, rec.statements
    return hits[0]


def _render_all(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(research_evidence, "research_reaches_desks", lambda: True)
    out: dict[str, str] = {
        "_geo_routing.ROUTED_ELSEWHERE_SQL": _geo_routing.ROUTED_ELSEWHERE_SQL,
        "geo_convergence_scan._POINT_SIGNALS_SQL":
            geo_convergence_scan._POINT_SIGNALS_SQL,
        "geo_convergence_scan._COUNTRY_SIGNALS_SQL":
            geo_convergence_scan._COUNTRY_SIGNALS_SQL,
        "_event_candidates._SIGNAL_SLICE_SQL": _event_candidates._SIGNAL_SLICE_SQL,
        "research_evidence._slice_clauses":
            " AND ".join(research_evidence._slice_clauses(24, 3)),
        "subscription.filter.build_sql_filter": build_sql_filter(
            source_id="source.x", owner_tenant="t",
            subscription=Subscription(canonical_only=True),
        ).select_signals(columns="id", limit=10),
    }
    out["cross_source_coalesce._fetch_window_rows"] = _captured(
        lambda pool: cross_source_coalesce._fetch_window_rows(
            pool, window_hours=24, owner_tenant=None, max_signals=500,
        )
    )
    out["metrics_api._collect_families"] = _captured(
        lambda pool: metrics_api._collect_families(pool)
    )

    def _reads(pool: _Recorder):
        deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))
        router = build_substrate_reads_router(deps)  # type: ignore[arg-type]
        route = next(
            r for r in router.routes
            if getattr(r, "path", "") == "/signals"
            and "GET" in getattr(r, "methods", ())
        )
        return route.endpoint(
            target_id=None, since=None, source_id=None, language=None,
            access_class_in=None, limit=50, cursor=None, principal="p",
        )

    out["substrate_reads_api.list_signals"] = _captured(_reads)
    return out


#: sha256 of each site's rendered statement at the pin (09be8bea), before the
#: sweep. metrics_api's AFTER hash differs by the constant's parentheses only.
_PINNED: dict[str, str] = {
    "_geo_routing.ROUTED_ELSEWHERE_SQL":
        "144a10ec8290fff31f9e3b4d8e88ca8c42628421741764ef34e79db7a06db2c3",
    "geo_convergence_scan._POINT_SIGNALS_SQL":
        "0f6327fae46c7b351e1141a82970021e394d39255160d058742942480a7024d9",
    "geo_convergence_scan._COUNTRY_SIGNALS_SQL":
        "d1a505fb4de9b9dc756988848cb7b53bbdd323bbc56bdec0a7c3bc81ab29104b",
    # N-Z (2026-11-14): re-pinned for ONE added predicate, the catch-up
    # backfill's exclusive upper bound ``($5 IS NULL OR fetched_at < $5)``;
    # the canonical-only clause itself is unchanged.
    # Re-pinned for 'claim' added to the source_class
    # CASE list (a claim source's rows keep their class); the canonical-only
    # clause itself is unchanged. BEFORE dedf4927...
    # Re-pinned for the row's own claim stamp read first
    # (a Telegram claim channel's row is 'claim' on a 'reporting' descriptor);
    # the canonical-only clause itself is unchanged. BEFORE bec2216b...
    "_event_candidates._SIGNAL_SLICE_SQL":
        "b9dc2a3a0de8b9f739ab5649826d8a36f62c81ce7455f259bd28a5b071282e08",
    "research_evidence._slice_clauses":
        "fbc64e0fa07edbf784bc84abf2fa068e40c8b3563297bedfe5530d4e1684296a",
    "subscription.filter.build_sql_filter":
        "1778b4f469058c29654620da5a163516d002ac2ed25a47967be5072df44eebf5",
    "cross_source_coalesce._fetch_window_rows":
        "aa4974317f096a7a60d2c0b38ac18605b34e2abbc341d0e5ee339244b5445b85",
    # BEFORE 5b77a1903766e5cd062ff08392f55a2b0834c46591df9139bd45ec10d61943be
    # ("... WHERE canonical_signal_id IS NULL OR canonical_signal_id = id");
    # AFTER the same text with the constant's parentheses.
    "metrics_api._collect_families":
        "a1abc795ec3ba5f9447215400684a5894f3612399f680306a71d4fe405afda77",
    "substrate_reads_api.list_signals":
        "9919d40f2f1a8b63b6393f9d53823cc2bd0870ef375837bef0564ececce3a7b0",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_every_site_renders_byte_identical_sql(monkeypatch):
    rendered = _render_all(monkeypatch)
    assert {k: _sha(v) for k, v in rendered.items()} == _PINNED


def test_every_site_carries_the_one_spelling(monkeypatch):
    for name, sql in _render_all(monkeypatch).items():
        assert (
            CANONICAL_ONLY_SQL in sql or canonical_only_sql_for("s") in sql
        ), name


def test_the_aliased_form_is_the_constant_qualified():
    assert canonical_only_sql_for("") == CANONICAL_ONLY_SQL
    assert canonical_only_sql_for("s") == (
        "(s.canonical_signal_id IS NULL OR s.canonical_signal_id = s.id)"
    )


_INLINE = re.compile(
    r"canonical_signal_id\s+IS\s+NULL\s+OR\s+\w*\.?canonical_signal_id\s*=\s*\w*\.?id\b"
)


def test_no_module_spells_the_clause_inline():
    """Comments and docstrings may NAME the clause in backticks; SQL may not
    spell it - a line carrying it outside a backticked span is a regression."""
    root = Path(__file__).resolve().parents[2] / "src" / "legba"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "signal_slice_scope.py":
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = re.sub(r"``[^`]*``|`[^`]*`", "", line)
            if _INLINE.search(stripped) and not stripped.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(root)}:{i}")
    assert offenders == []
