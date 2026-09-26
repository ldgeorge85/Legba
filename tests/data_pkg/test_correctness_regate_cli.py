# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G3 — ``scripts/correctness_regate.py --packet``: the FROZEN-atom re-gate.

WHY THE OPTION EXISTS. A live draw asks "do the families agree TODAY on TODAY's
claims" — the right question when the RUBRIC moves. It is the wrong question
when a FAMILY moves: OpenRouter removed ``mistralai/mistral-large-2512`` on
2026-09-20 and the judge component now serves ``mistral-medium-3.1``, and the
only way to ask whether the new model reads the rubric the way the gated one
did is to hand it the SAME atoms the passing gate was measured on.

NOTHING HERE SPENDS AND NOTHING HERE TOUCHES THE LIVE DATABASE. The families
are fakes with no network, the store is the session test database, and the
packet is built in the test rather than read from ``planning/`` (which is
gitignored, exactly as the sibling file's recorded fixtures are inlined).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import _correctness_grade as GRADE
from legba.data.analysts.deterministic_handlers import _correctness_packet as PACKET
from legba.data.analysts.deterministic_handlers._correctness_rubric import (
    NOTE_TO_GRADER,
    OUTPUT_CONTRACT,
    RUBRIC_SHA256,
    RUBRIC_TEXT,
)
from legba.data.config import PostgresConfig

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "correctness_regate.py"
)


def _load_script():
    """Import the host script by path — it is a CLI, not a package module."""
    spec = importlib.util.spec_from_file_location("correctness_regate", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["correctness_regate"] = module
    spec.loader.exec_module(module)
    return module


REGATE = _load_script()

# ---------------------------------------------------------------------------
# A frozen packet in build_p1_sample.py's shape
# ---------------------------------------------------------------------------

_SPAN = (
    "Israeli political parties have submitted their slates for the Knesset "
    "elections on October 27."
)
_REFERENCE = {
    "ref_bands": {"leadership_transition": "high", "escalation": "elevated"},
    "ref_developments": [
        {
            "item_id": "RD-IL-A-1",
            "summary": "38 candidate lists were filed by the deadline.",
            "decisive_span": _SPAN,
            "outlet": "Al Jazeera",
            "publish_date": "2026-09-09",
        },
    ],
}
_ASSERTIONS = [
    "The election took formal shape inside the window with 38 lists filed.",
    "Coalition fractures test the prime minister without a snap election.",
    "Fuel prices in the north rose for a third consecutive week.",
    "No new supply disruptions were reported in the latest three days.",
]


def _frozen_packet(**overrides: Any) -> dict[str, Any]:
    packet = {
        "round": "PROGRAM1",
        "packet": "calibration_v4",
        "note_to_grader": NOTE_TO_GRADER,
        "rubric": RUBRIC_TEXT,
        "output_contract": OUTPUT_CONTRACT,
        "items": [
            {
                "p1_id": f"P1-{index:08x}",
                "assertion": assertion,
                "reference": _REFERENCE,
            }
            for index, assertion in enumerate(_ASSERTIONS)
        ],
    }
    packet.update(overrides)
    return packet


def _write(tmp_path: Path, packet: Any, name: str = "packet.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Parsing — a live draw stays the default, and a dead flag is refused
# ---------------------------------------------------------------------------


def _args(argv: list[str]):
    return REGATE.resolve_args(REGATE.build_parser(), argv)


def test_a_live_draw_is_still_the_default_shape():
    args = _args(["--target", "country_watch_il"])
    assert args.packet is None
    assert args.target == "country_watch_il"
    assert args.n == REGATE.CAL.DEFAULT_DRAW_N
    assert args.head_window_days == REGATE.JOB.DEFAULT_HEAD_WINDOW_DAYS


def test_the_packet_option_parses_and_leaves_the_draw_knobs_at_their_defaults(
    tmp_path,
):
    path = _write(tmp_path, _frozen_packet())
    args = _args(["--packet", path, "--grade", "--cap", "0.05"])
    assert args.packet == path
    assert args.target is None
    assert args.grade is True and args.cap == 0.05


@pytest.mark.parametrize("argv", [
    [],
    ["--target", "country_watch_il", "--packet", "/tmp/p.json"],
])
def test_exactly_one_population_or_nothing_runs(argv):
    """Both would leave the script choosing silently, and the row it writes
    names only the packet sha — nobody could tell which population was gated."""
    with pytest.raises(SystemExit):
        _args(argv)


@pytest.mark.parametrize("flag,value", [
    ("--n", "12"), ("--seed", "abc"), ("--as-of", "2026-09-20T00:00:00Z"),
    ("--head-window-days", "3"),
])
def test_a_draw_knob_is_refused_with_packet_rather_than_ignored(flag, value):
    """A flag that silently does nothing is a flag somebody will believe fired."""
    with pytest.raises(SystemExit):
        _args(["--packet", "/tmp/p.json", flag, value])


# ---------------------------------------------------------------------------
# The loader — every invariant build_packet would have enforced
# ---------------------------------------------------------------------------


def test_a_well_formed_frozen_packet_loads_and_keeps_its_bytes(tmp_path):
    path = _write(tmp_path, _frozen_packet())
    loaded = REGATE.load_frozen_packet(path)
    assert [item["p1_id"] for item in loaded["items"]] == [
        f"P1-{i:08x}" for i in range(len(_ASSERTIONS))
    ]
    assert PACKET.packet_sha256(loaded) == PACKET.packet_sha256(_frozen_packet())


def test_a_packet_carrying_another_rubric_is_refused(tmp_path):
    """THE load-bearing check. The row this run writes carries THIS build's
    rubric_sha, so gating on other bytes would pool two rubrics under one
    identity — the exact silent re-labelling the digest exists to prevent."""
    path = _write(tmp_path, _frozen_packet(rubric=RUBRIC_TEXT + "\nedited\n"))
    with pytest.raises(REGATE.FrozenPacketError) as exc:
        REGATE.load_frozen_packet(path)
    assert RUBRIC_SHA256 in str(exc.value)


def test_a_packet_written_against_another_output_contract_is_refused(tmp_path):
    path = _write(tmp_path, _frozen_packet(output_contract='{"verdict": "x"}'))
    with pytest.raises(REGATE.FrozenPacketError):
        REGATE.load_frozen_packet(path)


def test_a_duplicate_atom_id_and_an_empty_assertion_are_both_refused(tmp_path):
    dupe = _frozen_packet()
    dupe["items"][1]["p1_id"] = dupe["items"][0]["p1_id"]
    with pytest.raises(REGATE.FrozenPacketError, match="duplicate"):
        REGATE.load_frozen_packet(_write(tmp_path, dupe, "dupe.json"))

    empty = _frozen_packet()
    empty["items"][2]["assertion"] = "   "
    with pytest.raises(REGATE.FrozenPacketError, match="empty assertion"):
        REGATE.load_frozen_packet(_write(tmp_path, empty, "empty.json"))


def test_a_packet_that_leaks_platform_telemetry_is_refused(tmp_path):
    leaky = _frozen_packet()
    leaky["items"][0]["reference"] = {
        **_REFERENCE, "cited_mass": 3.85, "faithfulness": 0.36,
    }
    with pytest.raises(REGATE.FrozenPacketError, match="LEAK SCAN FAILED"):
        REGATE.load_frozen_packet(_write(tmp_path, leaky, "leak.json"))


def test_a_packet_that_is_not_the_packet_object_is_refused(tmp_path):
    with pytest.raises(REGATE.FrozenPacketError, match="top level"):
        REGATE.load_frozen_packet(_write(tmp_path, ["not", "it"], "list.json"))
    with pytest.raises(REGATE.FrozenPacketError, match="cannot be read"):
        REGATE.load_frozen_packet(str(tmp_path / "does_not_exist.json"))


# ---------------------------------------------------------------------------
# The row it writes — fake families, real scoring, real table
# ---------------------------------------------------------------------------


class _Usage:
    cost_estimate_usd = 0.0
    prompt_tokens = 10
    completion_tokens = 5
    model = "fake"


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _FakeFamily:
    """One verdict per ASSERTION. No network, no key, one call per atom."""

    def __init__(self, by_assertion: dict[str, tuple[str, str]]) -> None:
        self._by_assertion = by_assertion
        self.calls = 0

    async def chat_complete(self, messages, **kwargs):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        verdict, span = self._by_assertion[payload["assertion"]]
        return _Response(json.dumps({
            "verdict": verdict, "core_claim": "c", "decisive_span": span,
            "reason": "r",
        }))


def _all_silent() -> dict[str, tuple[str, str]]:
    return {assertion: ("silent", "") for assertion in _ASSERTIONS}


def _one_disagreement() -> dict[str, tuple[str, str]]:
    labels = _all_silent()
    labels[_ASSERTIONS[0]] = ("contains", _SPAN)
    return labels


class _Store:
    """The script's store surface: ``acquire()`` and ``close()``, nothing more."""

    def __init__(self, pool) -> None:
        self._pool = pool
        self.closed = False

    def acquire(self):
        return self._pool.acquire()

    async def close(self) -> None:
        self.closed = True


@pytest_asyncio.fixture
async def regate_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM grader_calibrations WHERE notes LIKE 'pytest regate%'"
        )
    yield pool
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM grader_calibrations WHERE notes LIKE 'pytest regate%'"
        )
    await pool.close()


@pytest_asyncio.fixture
def wired(monkeypatch, regate_pool):
    """The script with its two outside edges replaced: the store and the
    families. Everything between them — loader, cap guard, grade_one, the
    agreement math, the INSERT — is the shipped code."""
    store = _Store(regate_pool)
    families = {
        "F0": _FakeFamily(_all_silent()),
        "F2": _FakeFamily(_all_silent()),
        "F3": _FakeFamily(_one_disagreement()),
    }

    async def _open_store():
        return store

    async def _build_handlers(component_ids, _store):
        assert set(component_ids) == set(GRADE.FAMILY_ORDER)
        return dict(families)

    monkeypatch.setattr(REGATE, "_open_store", _open_store)
    monkeypatch.setattr(REGATE, "build_handlers", _build_handlers)
    return {"store": store, "families": families, "pool": regate_pool}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_frozen_packet_is_graded_by_every_family_and_scored(
    tmp_path, wired
):
    """All three families, every atom, the shipped bars — and the MEASURED
    numbers on the row.

    F0 and F2 agree on all four; F3 differs on one. 4/4, 3/4, 3/4 = pooled
    0.8333 over a 0.75 bar with the weakest pair at 0.75 over a 0.70 floor, so
    the gate PASSES on a number that is neither 1.0 nor invented.
    """
    packet = _frozen_packet()
    path = _write(tmp_path, packet)
    args = _args([
        "--packet", path, "--grade", "--cap", "0.05",
        "--notes", "pytest regate — frozen packet",
    ])

    assert await REGATE.run(args) == 0
    assert wired["store"].closed is True
    for family in GRADE.FAMILY_ORDER:
        assert wired["families"][family].calls == len(_ASSERTIONS)

    async with wired["pool"].acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM grader_calibrations "
            " WHERE notes = 'pytest regate — frozen packet'"
        )
    assert row is not None
    assert row["gate_pass"] is True
    assert float(row["pooled"]) == pytest.approx(0.8333)
    assert row["rubric_sha"] == RUBRIC_SHA256
    assert row["packet_sha"] == PACKET.packet_sha256(packet)
    assert row["n_atoms"] == len(_ASSERTIONS)
    assert json.loads(row["model_ids"]) == GRADE.model_ids()
    pairs = {f"{p['a']}x{p['b']}": p["rate"] for p in json.loads(row["pairwise"])}
    assert pairs == {"F0xF2": 1.0, "F0xF3": 0.75, "F2xF3": 0.75}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_grades_the_frozen_packet_and_writes_no_row(
    tmp_path, wired
):
    args = _args([
        "--packet", _write(tmp_path, _frozen_packet()), "--grade",
        "--cap", "0.05", "--dry-run",
        "--notes", "pytest regate — dry run",
    ])
    assert await REGATE.run(args) == 0
    async with wired["pool"].acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM grader_calibrations WHERE notes LIKE "
            "'pytest regate%'"
        ) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_without_grade_the_frozen_run_prices_the_work_and_spends_nothing(
    tmp_path, wired, capsys
):
    args = _args(["--packet", _write(tmp_path, _frozen_packet())])
    assert await REGATE.run(args) == 0
    out = capsys.readouterr().out
    assert "NOT GRADING (no --grade)" in out
    assert "FROZEN atom set" in out
    for family in GRADE.FAMILY_ORDER:
        assert wired["families"][family].calls == 0


@pytest.mark.asyncio
async def test_a_refused_packet_never_even_opens_the_store(tmp_path, monkeypatch):
    """A packet this script would refuse must cost nothing at all — not a
    connection, not a registry call, certainly not a paid one."""
    async def _boom():
        raise AssertionError("the store must not be opened for a bad packet")

    monkeypatch.setattr(REGATE, "_open_store", _boom)
    path = _write(tmp_path, _frozen_packet(rubric="not the rubric"))
    args = _args(["--packet", path, "--grade", "--cap", "0.05"])
    assert await REGATE.run(args) == 2
