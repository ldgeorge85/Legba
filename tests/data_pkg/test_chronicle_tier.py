# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Chronicle tier (planning/CHRONICLE_BUILD_2026-07-21.md) — the public-record
third id on the journal_assessor kind.

Locks the tier plumbing:

  * ``_entry_kind_for_analyst`` distills the chronicle id → ``'chronicle'``
    (append tier — the consolidation-supersession branch keys on
    ``'consolidation'`` and must never fire for it);
  * the chronicle user prompt carries the public-record disciplines (mandatory
    citations, no-self) and NONE of the diary's apparatus blocks — the
    entry/consolidation render is byte-identical to before;
  * the persona module resolves standalone (no first-person voice, no
    self-anatomy import) and the descriptor YAML validates.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from legba.data.analysts.journal_assessor import (
    CHRONICLE_ANALYST_ID,
    CONSOLIDATOR_ANALYST_ID,
    _entry_kind_for_analyst,
    _render_user_prompt,
)

_REPO = Path(__file__).resolve().parents[2]

_ROWS = [
    {"id": "x1", "title": "Strikes hit the port", "source_id": "s1",
     "produced_at": "2026-07-21T00:00:00+00:00",
     "salience": {"magnitude": 0.95, "event_class": "escalation"}},
    {"id": "x2", "title": "Ceasefire talks stall", "source_id": "s2",
     "produced_at": "2026-07-21T01:00:00+00:00",
     "salience": {"magnitude": 0.4, "event_class": "other"}},
]


def test_entry_kind_distills_all_three_tiers() -> None:
    assert _entry_kind_for_analyst("journal_assessor") == "entry"
    assert _entry_kind_for_analyst(CONSOLIDATOR_ANALYST_ID) == "consolidation"
    assert _entry_kind_for_analyst(CHRONICLE_ANALYST_ID) == "chronicle"
    assert _entry_kind_for_analyst(None) == "entry"


def test_chronicle_prompt_is_public_record_not_diary() -> None:
    out = _render_user_prompt(_ROWS, tier="chronicle")
    # public-record disciplines present
    assert "CITATIONS" in out and "[[ref:<uuid>]]" in out
    assert "no self" in out
    assert "the temporal gate" in out
    # the diary's apparatus contract must NOT leak into the public chronicle
    assert "the apparatus is your POSTSCRIPT" not in out
    assert "INSTRUMENT CITATIONS" not in out
    assert "[[instrument]]" not in out
    assert "DISTINCT wired sources" not in out
    assert "get_source_health" not in out
    # the slice rows render citable, salience-tagged
    assert "[[ref:x1]]" in out and "(salience 0.95" in out


def test_entry_and_consolidation_render_unchanged() -> None:
    default = _render_user_prompt(_ROWS)
    explicit = _render_user_prompt(_ROWS, tier="entry")
    assert default == explicit
    assert "the apparatus is your POSTSCRIPT" in default
    assert "INSTRUMENT CITATIONS" in default
    # consolidation shares the diary render (its tier differs at write time)
    assert _render_user_prompt(_ROWS, tier="consolidation") == default


def test_chronicle_persona_resolves_and_is_third_person() -> None:
    from legba.prompts.chronicle_assessor import CHRONICLE_SYSTEM
    assert "You record; you do not judge." in CHRONICLE_SYSTEM
    assert "Here continues the account of the year 2026." in CHRONICLE_SYSTEM
    assert "[[ref:<uuid>]]" in CHRONICLE_SYSTEM
    # the diary's self-anatomy must not be composed in
    assert "get_journal_delta" not in CHRONICLE_SYSTEM.split("THIS RUN")[0]


def test_chronicle_descriptor_yaml_validates() -> None:
    from legba.data.schemas.analyst import AnalystDescriptor
    body = yaml.safe_load(
        (_REPO / "descriptors/analyst_chronicle_assessor.yaml").read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    assert desc.identity.id == "chronicle_assessor"
    assert desc.identity.kind == "journal_assessor"      # shared kind module
    assert desc.identity.state.value == "active"
    # the V1 chronicle gate must be declared
    assert "verify" in body["method"]["llm"]
    # weekly beat, off the 00:00/12:00 burst window
    assert body["cadence"]["fallback_schedule"] == "0 6 * * 1"
    assert int(body["cadence"]["cooldown_seconds"]) < 7 * 86400
    # read-only pack grants; no propose lane, no sinks
    packs = {p["pack_id"] for p in body["action_packs"]}
    assert packs == {"journal_read", "substrate_read"}
    assert body["outputs"] == []
