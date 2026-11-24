# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W5 — strong activity OUTSIDE the roster, named in the world read's aperture.

The world read could name only roster units, so Paraguay's week (an
impeachment, an arrest, a new president) was invisible to it and a sentence
about it was a guess. ``off_roster_activity`` reads, at world-slice time, the
countries in no subject-roster unit's scope whose 7-day count of signals at the
mass floor reaches the bar; the world record persists them; the declared
aperture names up to three. This suite locks the read's shape, the payload
carry, the line, and the fences that must let the voice use the names.

The fixture is the live read of 2026-11-07 ~17Z (Palestine 87 of 201, Paraguay
56 of 66, Lebanon 52 of 76).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from uuid import uuid4

from legba.data import rosters
from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts import assessment_prompts as apr
from legba.data.analysts import assessment_unsupported as au
from legba.data.analysts import off_roster_activity as ora
from legba.data.analysts import off_roster_aperture as orap
from legba.data.analysts.signal_salience import MASS_FLOOR
from legba.data.provenance.composition_integrity import desk_absence_sentences

LIVE_2026_11_07 = [
    {"code": "PS", "name": "Palestine, State of", "signals": 201, "above_floor": 87},
    {"code": "PY", "name": "Paraguay", "signals": 66, "above_floor": 56},
    {"code": "LB", "name": "Lebanon", "signals": 76, "above_floor": 52},
]


class _Conn:
    def __init__(self, rows=None, exc: Exception | None = None):
        self.rows = rows or []
        self.exc = exc
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any):
        self.calls.append((query, params))
        if self.exc:
            raise self.exc
        return list(self.rows)


# The aperture fixture: the o4 suite's spine, which names Argentina, Australia,
# Sudan, Pakistan and Ukraine and nothing in the Americas or the Levant.
SPINE: dict[str, Any] = {
    "blocks": [
        {"ordinal": 1, "desk": "escalation", "target_id": "country_watch_pk",
         "target_name": "Watch — Pakistan", "question": "escalation",
         "spans": [{"text": "Pakistan’s precision airstrikes into Afghanistan"}]},
    ],
    "coverage_roster": ["country_g20_ar", "country_watch_pk", "country_watch_sd"],
    "coverage": [{"unit": "country_g20_ar", "status": "in_basis"}],
    "unit_names": {"country_g20_ar": "G20 — Argentina",
                   "country_watch_pk": "Watch — Pakistan",
                   "country_watch_sd": "Watch — Sudan"},
    "drops": {"not_selected": [{"desk": "escalation", "target_id": "country_watch_sd",
                                "target_name": "Watch — Sudan",
                                "title": "Sudan: RSF shelling resumes"}],
              "counts": {"invisible_heads": 3}},
    "lead": {"kind": "co_leads", "test": {"n_candidates": 33}},
}


def _with_activity(spine: dict[str, Any], countries=LIVE_2026_11_07) -> dict[str, Any]:
    out = copy.deepcopy(spine)
    out[orap.OFF_ROSTER_PAYLOAD_KEY] = {
        "window_days": 7, "mass_floor": MASS_FLOOR,
        "min_above_floor": ora.OFF_ROSTER_MIN_ABOVE_FLOOR,
        "countries": [dict(c) for c in countries],
    }
    return out


# --- the read -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_read_binds_the_roster_tags_window_floor_bar_and_cap():
    conn = _Conn(rows=LIVE_2026_11_07)
    got = await ora.read_off_roster_activity(conn)
    (sql, params), = conn.calls
    assert "iso_countries" in sql and "canonical_signal_id IS NULL" in sql
    assert params == (
        list(rosters.subject_roster_tags()), 7, float(MASS_FLOOR), 50, 3,
    )
    assert got == {"window_days": 7, "mass_floor": float(MASS_FLOOR),
                   "min_above_floor": 50, "countries": LIVE_2026_11_07}


@pytest.mark.asyncio
async def test_a_failed_read_is_none_and_stamps_nothing():
    got = await ora.read_off_roster_activity(_Conn(exc=RuntimeError("down")))
    assert got is None
    rows = [{"id": 1}]
    ora.stamp_off_roster(rows, got)
    assert rows == [{"id": 1}]
    payload = {"schema": "assembly.v1"}
    assert ora.with_off_roster(payload, rows) == {"schema": "assembly.v1"}


@pytest.mark.asyncio
async def test_the_stamp_rides_the_rows_into_the_payload():
    activity = await ora.read_off_roster_activity(_Conn(rows=LIVE_2026_11_07))
    rows = [{"id": 1}, {"id": 2}]
    ora.stamp_off_roster(rows, activity)
    payload = ora.with_off_roster({"schema": "assembly.v1"}, rows)
    assert payload[orap.OFF_ROSTER_PAYLOAD_KEY]["countries"] == LIVE_2026_11_07


# --- the aperture line ----------------------------------------------------------


def test_the_aperture_names_the_three_and_says_nothing_about_them():
    text = apr.declared_aperture(_with_activity(SPINE))
    (line,) = [ln for ln in text.splitlines() if "OUTSIDE the roster" in ln]
    assert "Palestine, State of (PS): 87 of 201 signals" in line
    assert "Paraguay (PY): 56 of 66 signals" in line
    assert "Lebanon (LB): 52 of 76 signals" in line
    assert "50+ signals at magnitude 0.50 or more in the last 7 days" in line
    assert "state nothing about what is happening there" in line
    # The line rides into every citation's evidence_text: it must not read as a
    # desk negative there.
    assert desk_absence_sentences(line) == []


def test_no_payload_key_or_nothing_over_the_bar_is_the_shipped_aperture():
    shipped = apr.declared_aperture(SPINE)
    assert apr.declared_aperture(_with_activity(SPINE, countries=[])) == shipped
    assert "OUTSIDE the roster" not in shipped


# --- the fences let the voice use the names ------------------------------------


def test_the_names_join_the_aperture_vocabulary_and_unit_set():
    spine = _with_activity(SPINE)
    assert {"paraguay", "lebanon", "palestine"} <= au.aperture_vocabulary(spine)
    units = au.aperture_declared_units(spine)
    assert {"paraguay", "lebanon", "palestine"} <= units
    assert "paraguay" not in au.aperture_declared_units(SPINE)


def _body(line: str) -> str:
    return (
        "**One thread**\n\n**BLUF:** Pakistan's airstrikes lead [[ref:1]].\n\n"
        "## What this reading misses\n\n" + line + "\n"
    )


SENTENCE = (
    "Outside the roster, Paraguay and Lebanon are loud in the substrate this "
    "week, and this record carries no reading of either [[ref:1]]."
)


def test_a_sentence_naming_them_is_not_marked_once_the_record_declares_them():
    classes = {m["class"] for m in au.find_unsupported(_body(SENTENCE), SPINE)[0]}
    assert classes & {"aperture_unrostered", "aperture_guess"}, (
        "without the line the names are a guess (the 2026-11-07 behaviour)"
    )
    marks, _ = au.find_unsupported(_body(SENTENCE), _with_activity(SPINE))
    assert not {m["class"] for m in marks} & {"aperture_unrostered", "aperture_guess"}


# --- the carry: slice rows -> the world record's payload ------------------------


class _NeverCalledLLM:
    subprovider = "never_called"

    async def chat_complete(self, *a, **k):  # pragma: no cover
        raise AssertionError("the assembly path must not call the model")


class _Deps:
    def __init__(self, llm: Any) -> None:
        self.llm = llm


def _world_row(target_id: str) -> dict[str, Any]:
    return {
        "id": uuid4(), "kind": "finding", "title": f"{target_id} read",
        "body": "**BLUF:** The desk read leads on a verified event this week [1].\n",
        "confidence": 0.7, "effective_confidence": 0.7, "faithfulness_score": 0.9,
        "severity": "high", "evidence": [], "target_id": target_id,
        "target_version": None, "analyst_id": "country_composition",
        "analyst_version": "v", "produced_at": "2026-11-07T10:00:00+00:00",
        "derived_from": [], "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(), "data": {"data": {"meta": True}},
    }


_WORLD = {"analyst_id": "world_assessor", "composition": True}


@pytest.mark.asyncio
async def test_the_world_assembly_persists_the_stamp(monkeypatch):
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    activity = await ora.read_off_roster_activity(_Conn(rows=LIVE_2026_11_07))
    rows = [_world_row("country_g20_it"), _world_row("country_g20_gb")]
    ora.stamp_off_roster(rows, activity)
    result = await synth.run_method(rows, dict(_WORLD), _Deps(_NeverCalledLLM()))
    payload = result.finding.data["assembly"]
    assert payload[orap.OFF_ROSTER_PAYLOAD_KEY]["countries"] == LIVE_2026_11_07
    assert "Paraguay (PY)" in apr.declared_aperture(payload)

    unstamped = await synth.run_method(
        [_world_row("country_g20_it")], dict(_WORLD), _Deps(_NeverCalledLLM())
    )
    assert orap.OFF_ROSTER_PAYLOAD_KEY not in unstamped.finding.data["assembly"]
