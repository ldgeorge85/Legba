# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-G6 (2026-08-03) — the CROSS-SCRIPT TRANSLITERATION guard (person only).

NER read Arabic source text — ``آفي بالوط``, the Arabic transliteration of the
Israeli officer Avi Bluth — through the NLLB translate hop, which romanized it
"Avi Balut". The resolver's mint-time ladder is exact-string all the way down, so
``lookup_key('avi bluth') != lookup_key('avi balut')`` and the fork was
guaranteed. One officer became two, then two dismissals, and the split rode
through country_composition and region_composition into the world read with every
layer passing verify — the judge could not catch it, because the cited Arabic
signal does literally contain the phantom name (2026-08-03 adjudication §6).

The guard is a THIRD probe on the existing ladder, running only when both
exact-string probes miss. These tests pin the WIRING and the SCOPE — the ladder
order, the person-only boundary, the alias-fold outcome, the counter, and
degrade-not-drop — because the predicate itself is SQL and was validated against
the live substrate read-only (see the module block comment and migration 0165):

    Avi Balut     -> Avi Blot      (folded)
    Avi Bluth     -> Avi Blot      (folded)
    Sergei Lavrov -> Sergey Lavrov (folded)
    Vladimir Putin / Donald Trump / Benjamin Netanyahu -> no hit
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from legba.data.analysts.deterministic_handlers import entity_resolution as ER
from legba.data.analysts.deterministic_handlers.entity_resolution import (
    SUB_HANDLER_NAME,
    handle,
)


class _FakeConn:
    """Fake asyncpg conn that records which mint-time probes were reached.

    ``keeper`` (optional) is the row the TRANSLITERATION probe returns; the two
    exact-string probes always miss, which is the new-name condition the guard
    exists for.
    """

    def __init__(
        self, signal_row: dict[str, Any], *, keeper: dict[str, Any] | None = None,
        probe_raises: bool = False,
    ) -> None:
        self._signal_row = signal_row
        self._served = False
        self._keeper = keeper
        self._probe_raises = probe_raises
        self.probe_sqls: list[str] = []
        self.profile_upserts: list[tuple[Any, ...]] = []
        self._entity_id = uuid.uuid4()

    async def __aenter__(self) -> "_FakeConn":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if self._served:
            return []
        self._served = True
        return [self._signal_row]

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO entity_profiles" in sql:
            self.profile_upserts.append((sql, *args))
            return {"id": self._entity_id, "version": 1, "inserted": True}
        self.probe_sqls.append(sql)
        if "dmetaphone" in sql:
            if self._probe_raises:
                raise RuntimeError("function entity_phonetic_key(text) does not exist")
            return self._keeper
        return None  # both exact-string probes miss: this is a NEW name

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "merged_aliases" in sql:
            return "[]"
        return 1

    async def execute(self, sql: str, *args: Any) -> str:
        return "OK"


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeConn:
        return self._conn


class _FakeDeps:
    def __init__(self, pool: _FakePool) -> None:
        self.pg_pool = pool
        self.extras: dict[str, Any] = {}


def _signal(name: str, cls: str = "person") -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "payload": {
            "title": "Katz dismisses Central Command chief",
            "entities": [{"text": name, "class": cls}],
        },
    }


async def _run(
    name: str, *, cls: str = "person", keeper: dict[str, Any] | None = None,
    probe_raises: bool = False,
) -> tuple[_FakeConn, Any]:
    conn = _FakeConn(_signal(name, cls), keeper=keeper, probe_raises=probe_raises)
    result = await handle(
        [], {"sub_handler": SUB_HANDLER_NAME, "run_id": uuid.uuid4()},
        _FakeDeps(_FakePool(conn)),
    )
    return conn, result


_AVI_BLUTH = {
    "id": uuid.uuid4(),
    "entity_class": "person",
    "canonical_name": "Avi Bluth",
}


# ---------------------------------------------------------------------------
# The ladder: probe C runs LAST, and only for persons
# ---------------------------------------------------------------------------


async def test_the_phonetic_probe_runs_only_after_both_exact_probes_miss() -> None:
    conn, _ = await _run("Avi Balut", keeper=_AVI_BLUTH)
    phonetic = [s for s in conn.probe_sqls if "dmetaphone" in s]
    assert len(phonetic) == 1, "exactly one phonetic probe per new person name"
    # …and it is the LAST rung: the exact-name and alias probes precede it.
    assert conn.probe_sqls.index(phonetic[0]) == len(conn.probe_sqls) - 1


async def test_a_non_person_class_never_reaches_the_phonetic_probe() -> None:
    """Scoped tightly on purpose — a digit or a direction is often the whole
    distinction in an org or location surface ("Southwest"/"Southeast Asian")."""
    for cls in ("organization", "location", "country", "entity"):
        conn, _ = await _run("Northern Command", cls=cls)
        assert not [s for s in conn.probe_sqls if "dmetaphone" in s], cls


async def test_the_probe_helper_is_person_gated_at_its_own_boundary() -> None:
    """Belt and braces: the gate lives in the helper, not only at the call site."""

    class _Boom:
        async def fetchrow(self, *a: Any, **kw: Any) -> Any:
            raise AssertionError("must not query for a non-person class")

    assert await ER._probe_transliteration_variant(_Boom(), "Acme Corp", "organization") is None


# ---------------------------------------------------------------------------
# The outcome: an alias on the real officer, not a second officer
# ---------------------------------------------------------------------------


async def test_a_phonetic_hit_folds_the_variant_onto_the_keeper() -> None:
    conn, result = await _run("Avi Balut", keeper=_AVI_BLUTH)
    assert conn.profile_upserts, "the upsert must still run"
    written_name = conn.profile_upserts[0][1]
    assert written_name == "Avi Bluth", "the write converges onto the keeper's surface"


async def test_the_counter_records_the_fold() -> None:
    _, result = await _run("Avi Balut", keeper=_AVI_BLUTH)
    body = result.finding.body
    assert f"{ER._TRANSLIT_COUNTER}=1" in body


async def test_no_hit_leaves_the_historical_behaviour_and_a_zero_counter() -> None:
    conn, result = await _run("Benjamin Netanyahu", keeper=None)
    assert conn.profile_upserts
    assert conn.profile_upserts[0][1] == "Benjamin Netanyahu", "a new row is minted"
    assert f"{ER._TRANSLIT_COUNTER}=0" in result.finding.body


async def test_a_probe_failure_degrades_rather_than_breaking_ingestion() -> None:
    """The safety argument for adding a rung to a live mint path.

    An older database without migration 0165 has no ``entity_phonetic_key``; the
    probe must lose, not the sweep.
    """
    conn, result = await _run("Avi Balut", keeper=_AVI_BLUTH, probe_raises=True)
    assert conn.profile_upserts
    assert conn.profile_upserts[0][1] == "Avi Balut", "historical behaviour"
    assert f"{ER._TRANSLIT_COUNTER}=0" in result.finding.body


# ---------------------------------------------------------------------------
# The predicate's four conditions are all present in the shipped SQL
# ---------------------------------------------------------------------------


def test_every_measured_condition_is_in_the_shipped_predicate() -> None:
    """Each was earned against the live table; none may quietly fall out.

    Without (1) and (2) the naive predicate accepted 1,720 pairs of junk
    (Khan/Ken, cae/Gay, "209 PM EDT"/"602 PM EDT"); with all four it accepts 720
    at roughly 90% precision. And ``dmetaphone_alt`` is load-bearing, not
    decoration: dmetaphone('bluth')='PL0' vs dmetaphone('balut')='PLT', so a
    primary-only test MISSES the very case this exists for — the alt codes match.
    """
    sql = ER._TRANSLIT_PROBE_SQL
    assert "array_length(i.toks, 1) >= 2" in sql            # (1) >= 2 tokens
    assert re.search(r"array_length\(regexp_split_to_array\(lower\(btrim\(p\.canonical_name",
                     sql)                                    # (1) same token count
    assert "!~ '[0-9]'" in sql                               # (2) no digits
    assert "levenshtein(" in sql and "<= 2" in sql           # (3) edit distance
    assert "dmetaphone(" in sql and "dmetaphone_alt(" in sql  # (4) primary AND alt
    assert "generate_subscripts(" in sql                     # (4) PER TOKEN
    assert "entity_class = 'person'" in sql                  # scope
    # Never resurrect a GC'd row (the DQ P4 re-animation guard) — the same rule
    # the two probes above it carry.
    assert "merged_into IS NULL" in sql
    assert "NOT IN ('merged', 'junk')" in sql
    # The index-backed prefilter from migration 0165.
    assert "entity_phonetic_key" in sql
