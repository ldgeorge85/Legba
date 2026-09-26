# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B4 (JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §6) — the census script's
(a) connective-fault detector lexicon narrowing.

The live 14-day hand-graded run (JOURNAL_CONNECTIVE_CENSUS.md §3) found (a)
at 13% precision (2 TP / 13 FP): "as" alone caused 7/13 FPs (almost always
internal to the trailing clause, not bridging the gap) and bare "the
same"/"same conflict"/"same crisis" caught "at the same time"/"the same
day" — a temporal transition, not an identity claim (3/13 FPs). This locks
in the fix: "as" dropped; "same" fires ONLY as "same <conflict|crisis|war|
front|theatre|campaign>"; "at the same time"/"meanwhile"/"amid" stay in the
WEAK bucket (never gated on referent overlap). No DB — loads
``scripts/journal_connective_census.py`` directly (it lives outside the
``legba`` package, the same way the script's own ``sys.path`` insert does)
and exercises the lexicon + detector in isolation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "journal_connective_census.py"


def _load_census_module():
    spec = importlib.util.spec_from_file_location(
        "journal_connective_census_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_census = _load_census_module()


def test_bare_as_no_longer_fires_strong() -> None:
    text = "copper surged as traders priced in tighter supplies"
    assert _census._find_strong_cues(text) == []


def test_same_time_and_same_day_no_longer_fire_strong() -> None:
    assert _census._find_strong_cues("at the same time, markets opened lower") == []
    assert _census._find_strong_cues("the same day, a rally began") == []


def test_same_conflict_still_fires_strong() -> None:
    assert _census._find_strong_cues("reporting on the same conflict continues") == [
        "same conflict"
    ]


def test_same_fires_for_every_referent_word() -> None:
    for word in ("crisis", "war", "front", "theatre", "campaign"):
        assert _census._find_strong_cues(f"reporting on the same {word} continues") == [
            f"same {word}"
        ]


def test_other_strong_cues_unaffected() -> None:
    assert _census._find_strong_cues("this underscores the point") == ["underscores"]
    assert _census._find_strong_cues("produced a shift") == ["produced"]


def test_at_the_same_time_lives_in_weak_bucket() -> None:
    assert "at the same time" in _census._WEAK_CUES
    assert "meanwhile" in _census._WEAK_CUES
    assert "amid" in _census._WEAK_CUES


def test_strong_cues_no_longer_contain_as_or_bare_same() -> None:
    assert "as" not in _census._STRONG_CUES
    assert "the same" not in _census._STRONG_CUES
    # regex-driven now, never a literal list entry
    assert "same conflict" not in _census._STRONG_CUES


# ---------------------------------------------------------------------------
# Integration — _detect_connectives wired to _find_strong_cues
# ---------------------------------------------------------------------------


def _mk_signal(source_id: str, geo: list[str]) -> dict:
    return {
        "source_id": source_id, "geo": set(geo), "entity_classes": set(),
        "tags": set(), "title": "",
    }


def test_detect_connectives_no_fault_on_as_bridging_disjoint_geo() -> None:
    a, b = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    claim = {
        "kind": "fact", "refs": [a, b],
        "text_span": (
            f"Copper surged [[ref:{a}]] as traders priced in tighter supplies "
            f"[[ref:{b}]]."
        ),
    }
    signals = {a: _mk_signal("source.a", ["SA"]), b: _mk_signal("source.b", ["US"])}
    faults, weak = _census._detect_connectives(claim, signals, {})
    assert faults == []


def test_detect_connectives_still_fires_on_same_conflict_disjoint_referents() -> None:
    a, b = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    claim = {
        "kind": "fact", "refs": [a, b],
        "text_span": (
            f"Russian attacks disrupt the new school year [[ref:{a}]]. The same "
            f"conflict produced a grim tally of wounded troops [[ref:{b}]]."
        ),
    }
    signals = {a: _mk_signal("source.hrw", ["UA"]), b: _mk_signal("source.mee", ["US"])}
    faults, weak = _census._detect_connectives(claim, signals, {})
    assert len(faults) == 1
    assert "same conflict" in faults[0]["cues_strong"]
