# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D26 RESIDUAL — the ``entity_profiles.analyst_id`` stamp must LAND.

W2 made the resolver stamp ``completeness_score`` (computed) and ``derived_from``
(the source-signal id) on the ``entity_profiles`` upsert. Those landed live
(10/10 completeness, 8/10 derived_from) because they do not depend on the
``options`` mapping. ``analyst_id`` did NOT (live: 0/10 ``with_analyst_id``):
the deterministic sub-handler invocation does not reliably carry ``analyst_id``
in ``options``, so a bare ``options.get("analyst_id")`` resolved to ``None`` and
the upsert's ``analyst_id = COALESCE(entity_profiles.analyst_id,
EXCLUDED.analyst_id)`` always kept NULL.

The fix falls back to ``SUB_HANDLER_NAME`` (the sibling deterministic-handler
pattern) so the stamp is never NULL and lands on the SAME write path completeness
and derived_from already land on.

Hermetic — no DB, no SLM, no network. A fake asyncpg pool/conn captures the
parameters bound to the ``entity_profiles`` upsert (the exact ``fetchrow`` the
live 0/10 rows hit) and the test asserts the ``analyst_id`` positional argument
is non-NULL.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from legba.data.analysts.deterministic_handlers import entity_resolution
from legba.data.analysts.deterministic_handlers.entity_resolution import (
    SUB_HANDLER_NAME,
    handle,
)

# Positional index of ``analyst_id`` in the ``entity_profiles`` INSERT VALUES
# list (after the SQL string): canonical_name, entity_type, entity_class, data,
# geo_lat, geo_lon, geo_country, completeness_score, ANALYST_ID, ...
#   args = (sql, text, cls, cls, data, lat, lon, country, completeness,
#           analyst_id, analyst_version, run_id, derived_arr)
# → analyst_id is args[9] when the SQL string is args[0].
_ANALYST_ID_ARG_INDEX = 9
_COMPLETENESS_ARG_INDEX = 8
_DERIVED_FROM_ARG_INDEX = 12


class _FakeConn:
    """Captures the ``entity_profiles`` upsert ``fetchrow`` and no-ops the rest.

    Every coroutine returns a value plausible enough for ``_resolve_batch`` to
    finish its single-signal sweep without touching a real DB.
    """

    def __init__(self, signal_row: dict[str, Any]) -> None:
        self._signal_row = signal_row
        self._signals_served = False
        self.profile_upserts: list[tuple[Any, ...]] = []
        self._entity_id = uuid.uuid4()

    # async context manager protocol for ``pool.acquire()``
    async def __aenter__(self) -> "_FakeConn":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        # The only ``fetch`` in the sweep is the un-resolved signals query.
        if self._signals_served:
            return []
        self._signals_served = True
        return [self._signal_row]

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO entity_profiles" in sql:
            # This is the write path completeness + derived_from + analyst_id
            # all land on — capture its bound parameters.
            self.profile_upserts.append((sql, *args))
            return {"id": self._entity_id, "version": 1, "inserted": True}
        # _record_provenance never calls fetchrow; defensive default.
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        # Version lookups + the merged_aliases read in _record_provenance.
        if "merged_aliases" in sql:
            return "[]"
        return 1

    async def execute(self, sql: str, *args: Any) -> str:
        # links / edges / signals-stamp / provenance UPDATE + version INSERT.
        return "OK"


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeConn:
        # asyncpg's ``pool.acquire()`` returns an async context manager.
        return self._conn


class _FakeDeps:
    def __init__(self, pool: _FakePool) -> None:
        self.pg_pool = pool
        self.extras: dict[str, Any] = {}


def _one_signal_row() -> dict[str, Any]:
    """A single un-resolved signal carrying one clean NER mention.

    "Iran" canonicalizes clean (country), is above MIN_NAME_LEN, and is not
    junk — so it reaches the ``entity_profiles`` upsert.
    """
    return {
        "id": uuid.uuid4(),
        "payload": {
            "title": "Iran announces budget",
            "entities": [{"text": "Iran", "class": "country"}],
        },
    }


async def _run(options: dict[str, Any]) -> _FakeConn:
    conn = _FakeConn(_one_signal_row())
    deps = _FakeDeps(_FakePool(conn))
    await handle([], options, deps)
    return conn


async def test_analyst_id_lands_when_options_omits_it():
    """The live 0/10 condition: ``options`` carries NO ``analyst_id``.

    The fix must fall back to ``SUB_HANDLER_NAME`` so the value bound to the
    ``entity_profiles`` upsert is non-NULL.
    """
    conn = await _run({"sub_handler": SUB_HANDLER_NAME, "run_id": uuid.uuid4()})
    assert conn.profile_upserts, "entity_profiles upsert never executed"
    args = conn.profile_upserts[0]
    analyst_id = args[_ANALYST_ID_ARG_INDEX]
    assert analyst_id is not None, "analyst_id bound NULL — the D26 residual"
    assert analyst_id == SUB_HANDLER_NAME, analyst_id


async def test_analyst_id_lands_on_same_path_as_completeness_and_derived_from():
    """analyst_id rides the SAME write payload completeness + derived_from do."""
    sig = _one_signal_row()
    conn = _FakeConn(sig)
    deps = _FakeDeps(_FakePool(conn))
    await handle([], {"sub_handler": SUB_HANDLER_NAME, "run_id": uuid.uuid4()}, deps)

    assert conn.profile_upserts
    args = conn.profile_upserts[0]
    # All three D26/W2 stamps are bound on the one upsert call.
    assert args[_ANALYST_ID_ARG_INDEX] is not None      # the residual being fixed
    assert isinstance(args[_COMPLETENESS_ARG_INDEX], float)  # W2 completeness
    assert args[_COMPLETENESS_ARG_INDEX] > 0.0
    # derived_from carries the originating signal id (the W2 8/10 stamp).
    derived = args[_DERIVED_FROM_ARG_INDEX]
    assert isinstance(derived, list)
    assert sig["id"] in derived


async def test_explicit_options_analyst_id_is_preserved():
    """An explicit ``options['analyst_id']`` is NOT clobbered by the fallback."""
    conn = await _run(
        {"sub_handler": SUB_HANDLER_NAME, "analyst_id": "er_custom",
         "run_id": uuid.uuid4()}
    )
    assert conn.profile_upserts
    assert conn.profile_upserts[0][_ANALYST_ID_ARG_INDEX] == "er_custom"


async def test_falsy_options_analyst_id_falls_back():
    """An empty-string / falsy ``analyst_id`` still falls back (never binds '')."""
    conn = await _run(
        {"sub_handler": SUB_HANDLER_NAME, "analyst_id": "", "run_id": uuid.uuid4()}
    )
    assert conn.profile_upserts
    assert conn.profile_upserts[0][_ANALYST_ID_ARG_INDEX] == SUB_HANDLER_NAME


def test_sub_handler_name_constant():
    # The fallback identity the stamp uses — guard against a rename drifting the
    # live analyst_id silently.
    assert entity_resolution.SUB_HANDLER_NAME == "entity_resolution"
