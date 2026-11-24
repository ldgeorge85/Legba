# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W5 — a THEME-filtered thematic composition (hybrid_threat_composition).

escalation_composition fuses ONE unit across every desk. hybrid_threat_composition
fuses SEVERAL units across every desk and keeps only the heads whose quoted lead
carries a declared term (``thematic_theme``). This suite locks:

  * the descriptor: draft, target-less, verify-declaring, a valid term list, in
    the bringup set;
  * the term grammar and its two compilations (Python and Postgres ARE);
  * READ_SLICE: the SQL pre-filter on the basis read (and the periphery read
    under C-TIER), the lead-only admission, cross-desk carriage, and coverage
    that calls a non-matching desk ``quiet`` rather than a gap;
  * the assembly composes it with zero model calls, and the prose path refuses
    (SEAMS #75) before any model call;
  * regression: escalation_composition carries no term list and reads unfiltered.
"""

from __future__ import annotations

from tests._lifecycle_rows import lifecycle_text
import importlib.util
import pathlib
import re
from typing import Any
from uuid import uuid4

import pytest
import yaml

from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts import thematic_theme as theme
from legba.data.schemas.analyst import AnalystDescriptor

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DESCRIPTORS_DIR = _ROOT / "descriptors"
_FILE = "analyst_hybrid_threat_composition.yaml"

_IT, _GB, _FR = "country_g20_it", "country_g20_gb", "country_g20_fr"

# Lead sentences cut from the 2026-11-07 heads (the BLUF the assembly quotes).
IT_ESCALATION = (
    "*As of 2026-11-07; slice covers the trailing 72h to that date; 71 signals.*\n\n"
    "**BLUF:** The aviation bomb‑hoax wave remains the driver of Italy's "
    "escalation risk, now joined by pro-Russian hacking against the Brenner "
    "Base Tunnel operator [3].\n\n## What changed\n- Six Italian flights were "
    "among 18 European targets [4].\n"
)
GB_ESCALATION = (
    "**BLUF:** The Iran-linked subversion threat remains the driver of UK "
    "escalation risk, with bomb threats against European airlines disrupting "
    "flights on 6 November [100].\n"
)
# On theme in the TITLE and the fourth bullet only: the quoted lead is about
# strikes, so carrying it under a hybrid heading would be an off-theme quote.
IT_STABILITY = (
    "**BLUF:** No protest or strike event appears in this desk's collection "
    "through 7 November, but grievance is deepening as the Taranto furnaces shut "
    "down [12].\n\n## What changed\n- Bomb hoaxes continued against flights [29].\n"
)
FR_ESCALATION = (
    "**BLUF:** Police lines held at Place de la Concorde on 7 November, a "
    "repetition of October's arson and looting [5].\n"
)


def _load(name: str) -> AnalystDescriptor:
    body = yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return AnalystDescriptor.model_validate(body, strict=False)


def _head(*, analyst_id: str, target_id: str, body: str, title: str = "t",
          signals: tuple[str, ...] = ("s1",)) -> dict[str, Any]:
    return {
        "id": uuid4(), "kind": "finding", "title": title, "body": body,
        "confidence": 0.7, "effective_confidence": 0.7, "faithfulness_score": 0.9,
        "severity": "high", "evidence": [],
        "data": {"data": {"citations": [
            {"marker": f"[{i}]", "signal_id": s, "source_id": f"source.{s}",
             "title": f"signal {s}", "source": f"https://example.test/{s}"}
            for i, s in enumerate(signals, start=1)
        ]}},
        "target_id": target_id, "target_version": None,
        "analyst_id": analyst_id, "analyst_version": "vtest",
        "produced_at": "2026-11-07T10:00:00+00:00", "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0", "run_id": uuid4(),
    }


class _Conn:
    """Routes the roster read; the theme-filtered read gets the canned heads and
    every other query (freshness, continuity) gets nothing."""

    def __init__(self, heads: list[dict[str, Any]], roster: list[tuple[str, str]]):
        self._heads = heads
        self._roster = [{"descriptor_id": d, "name": n} for d, n in roster]
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if "target_descriptors" in query:
            return list(self._roster)
        if "~*" in query:
            return [dict(h) for h in self._heads]
        return []

    async def fetchrow(self, query: str, *params: Any) -> None:
        self.calls.append((query, params))
        return None

    async def fetchval(self, query: str, *params: Any) -> None:
        self.calls.append((query, params))
        return None


_ROSTER = [(_FR, "France"), (_GB, "United Kingdom"), (_IT, "Italy")]


def _heads() -> list[dict[str, Any]]:
    return [
        _head(analyst_id="escalation", target_id=_IT, body=IT_ESCALATION),
        _head(analyst_id="escalation", target_id=_GB, body=GB_ESCALATION),
        _head(analyst_id="internal_stability", target_id=_IT, body=IT_STABILITY,
              title="Italy: streets quiet as bomb hoaxes hit elites"),
        _head(analyst_id="escalation", target_id=_FR, body=FR_ESCALATION),
    ]


# --- 1. the descriptor --------------------------------------------------------


def test_descriptor_is_a_draft_target_less_verify_declaring_theme():
    desc = _load(_FILE)
    assert desc.identity.id == "hybrid_threat_composition"
    assert desc.identity.kind == "meta_findings_synthesizer"
    assert desc.identity.state == "draft"
    assert desc.subscription.targets is None
    assert "verify" in desc.method.llm
    assert "max_tokens" not in desc.method.llm
    assert desc.method.budget_tokens_per_day == 0
    assert synth.thematic_dimension(desc) == "hybrid_threat"
    assert synth.thematic_desks(desc) is None
    units = [a.id for a in desc.subscription.other_analysts]
    assert {"escalation", "internal_stability", "military_posture"} <= set(units)
    terms = synth.thematic_terms(desc)
    assert terms and "bomb hoax" in terms and "sabotag" in terms


def test_in_bringup_set():
    spec = importlib.util.spec_from_file_location(
        "_bringup_register_analysts", _ROOT / "scripts" / "bringup_register_analysts.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert _FILE in mod.ANALYST_FILES


# --- 2. the term grammar ------------------------------------------------------


@pytest.mark.parametrize("bad", ["Bomb (hoax)", "-sabotage", "gps|jamming", ".*"])
def test_a_term_outside_the_grammar_fails_loud(bad):
    desc = _load(_FILE)
    desc.subscription.substrate["thematic_terms"] = ["bomb hoax", bad]
    with pytest.raises(ValueError, match="outside the grammar"):
        theme.thematic_terms(desc)


def test_a_non_list_term_value_fails_loud():
    desc = _load(_FILE)
    desc.subscription.substrate["thematic_terms"] = "bomb hoax"
    with pytest.raises(ValueError, match="must be a list"):
        theme.thematic_terms(desc)


def test_terms_match_at_a_word_start_and_across_spaces_or_hyphens():
    p = theme.py_theme_pattern(["bomb hoax", "sabotag", "gps jamming"])
    assert p.search("the aviation bomb-hoax wave")
    assert p.search("the aviation bomb\u2011hoax wave")     # U+2011, the live shape
    assert p.search("Bomb  hoaxes continued")
    assert p.search("rail lines were sabotaged overnight")
    assert p.search("GPS-jamming over the Baltic")
    assert not p.search("antisabotage drills")      # not at a word start
    assert not p.search("a bomb threat")             # not a declared term


def test_the_postgres_pattern_is_the_same_alternation_as_an_are():
    pg = theme.pg_theme_pattern(["bomb hoax", "sabotag"])
    assert pg == r"\m(?:bomb" + theme._PG_SEP + "hoax|sabotag)"
    for dash in ("-", "\u2011", "\u2013", "\u2014", "\u00a0", " "):
        assert re.fullmatch(theme._PY_SEP, dash)
    # Translated to Python's dialect it accepts what the Python pattern does
    # (checked against a live server's ARE on 2026-11-07: the same verdicts).
    py = re.compile(
        pg.replace(r"\m", r"(?<!\w)").replace(theme._PG_SEP, theme._PY_SEP), re.I
    )
    assert py.search("bomb\u2011hoax") and py.search("Sabotage")
    assert not py.search("antisabotage")


# --- 3. READ_SLICE ------------------------------------------------------------


@pytest.mark.asyncio
async def test_slice_keeps_on_theme_leads_across_desks_and_units():
    desc = _load(_FILE)
    conn = _Conn(_heads(), _ROSTER)
    rows = await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)

    basis_sql, basis_params = next(
        (q, p) for q, p in conn.calls if "~*" in q
    )
    assert "(f.title || ' ' || f.body) ~*" in basis_sql
    assert theme.pg_theme_pattern(synth.thematic_terms(desc)) in basis_params

    kept = {(r["analyst_id"], r["target_id"]) for r in rows if r.get("_desk_mode")}
    # IT + GB escalation leads carry the theme; the IT stability head is on
    # theme only in its title and fourth bullet, and France's arson is a riot.
    assert kept == {("escalation", _IT), ("escalation", _GB)}
    cov = {c["desk_id"]: c["mode"] for c in rows[0]["_thematic_coverage"]}
    assert cov == {_IT: "present", _GB: "present", _FR: theme.THEMATIC_MODE_QUIET}
    # A quiet desk is not handed to the prose as an unassessed gap.
    assert synth._render_desk_coverage_block(rows[0]["_thematic_coverage"]) == ""


@pytest.mark.asyncio
async def test_tiered_periphery_read_is_theme_filtered_too(monkeypatch):
    monkeypatch.setenv(synth.TIERED_EVIDENCE_ENV, "1")
    desc = _load(_FILE)
    conn = _Conn(_heads(), _ROSTER)
    await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)
    filtered = [q for q, _ in conn.calls if "~*" in q]
    assert len(filtered) == 2, "the basis AND the periphery read carry the theme"
    assert any("LEFT JOIN v" in q for q in filtered)


@pytest.mark.asyncio
async def test_no_on_theme_head_is_an_empty_slice():
    desc = _load(_FILE)
    conn = _Conn([_head(analyst_id="escalation", target_id=_FR, body=FR_ESCALATION)],
                 _ROSTER)
    assert await synth.READ_SLICE(conn, descriptor=desc, target_filter=None) == []


@pytest.mark.asyncio
async def test_regression_escalation_composition_reads_unfiltered():
    comp = _load("analyst_escalation_composition.yaml")
    assert synth.thematic_terms(comp) is None
    conn = _Conn([], _ROSTER)
    await synth.READ_SLICE(conn, descriptor=comp, target_filter=None)
    assert conn.calls and not any("~*" in q for q, _ in conn.calls)


# --- 4. the two compose paths -------------------------------------------------


class _NeverCalledLLM:
    subprovider = "never_called"

    async def chat_complete(self, *a, **k):  # pragma: no cover
        raise AssertionError("a theme-filtered thematic must not call the model")


class _Deps:
    def __init__(self, llm: Any) -> None:
        self.llm = llm


_OPTIONS = {"analyst_id": "hybrid_threat_composition",
            "thematic_dimension": "hybrid_threat", "composition": True}


async def _theme_rows() -> list[dict[str, Any]]:
    desc = _load(_FILE)
    rows = await synth.READ_SLICE(_Conn(_heads(), _ROSTER), descriptor=desc,
                                  target_filter=None)
    ap.attach_cited_salience(rows, {"s1": 0.9})
    return rows


@pytest.mark.asyncio
async def test_the_assembly_composes_it_with_zero_model_calls(monkeypatch):
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "hybrid_threat_composition")
    result = await synth.run_method(
        await _theme_rows(), dict(_OPTIONS, run_id=uuid4()), _Deps(_NeverCalledLLM())
    )
    assert result.finding.title.startswith("Cross-desk read")
    assert "bomb" in result.finding.body.lower()
    assert result.finding.data["situation_signature"] == (
        "composition:hybrid_threat_composition:world"
    )
    assert {c["mode"] for c in result.finding.data["desk_coverage"]} == {
        "present", theme.THEMATIC_MODE_QUIET,
    }
    assert "desk_gaps" not in result.finding.data
    assert not any(s.get("kind") == "llm_call" for s in result.intermediate_steps)


@pytest.mark.asyncio
async def test_the_prose_path_refuses_before_any_model_call(monkeypatch):
    monkeypatch.delenv(ap.ASSEMBLY_ENV, raising=False)
    with pytest.raises(theme.ThemeProseNotBuilt, match="SEAMS #75"):
        await synth.run_method(
            await _theme_rows(), dict(_OPTIONS, run_id=uuid4()),
            _Deps(_NeverCalledLLM()),
        )


# --- 5. the activation (descriptors/LIFECYCLE.md + the world's subscription) --


def test_activation_row_and_world_subscription_ship_together():
    """The walker activates the draft through its LIFECYCLE row, and the world
    reads it as a declared thematic lane. The two ship in ONE commit so the
    world never declares a lane that cannot produce a head."""
    rows = [
        [c.strip() for c in line.strip().strip("|").split("|")]
        for line in lifecycle_text(_DESCRIPTORS_DIR).splitlines()
        if line.startswith("| analyst | hybrid_threat_composition ")
    ]
    assert rows == [["analyst", "hybrid_threat_composition", "active", "draft"]]
    world = _load("analyst_world_assessor.yaml")
    ids = [a.id for a in world.subscription.other_analysts]
    assert "hybrid_threat_composition" in ids
    # Admitted as its own thematic subject group beside escalation_composition.
    from legba.data.analysts import composition_slice as cs
    assert "hybrid_threat_composition" in cs.world_admissible_analyst_ids(ids)


def test_every_composition_producer_list_naming_escalation_names_hybrid():
    """Parity with escalation_composition across the hard-coded producer lists
    (an unlisted thematic producer reads as "not a live producer", or — the
    supersession floor — fails OPEN into the situation clusterer)."""
    from legba.data import subject
    from legba.data.analysts.deterministic_handlers import _correctness_packet as pkt
    from legba.data.analysts.deterministic_handlers import _external_audit_width as width
    from legba.data.analysts.deterministic_handlers import composition_lineage_sweep as cls
    from legba.data.analysts.deterministic_handlers import finding_supersession as fs
    from legba.runtime import substrate_query_port as sqp

    for name, ids in (
        ("_ASSESSMENT_PRODUCER_ANALYSTS", sqp._ASSESSMENT_PRODUCER_ANALYSTS),
        ("WIDTH_READ_ANALYST_IDS", width.WIDTH_READ_ANALYST_IDS),
        ("_GAUGE_ANALYSTS", cls._GAUGE_ANALYSTS),
        ("_COMPOSITION_ANALYST_IDS", fs._COMPOSITION_ANALYST_IDS),
        ("FORBIDDEN_IDENTIFIERS", pkt.FORBIDDEN_IDENTIFIERS),
    ):
        assert "escalation_composition" in ids, name
        assert "hybrid_threat_composition" in ids, name
    # The packaged subject export carries the new thematic frame.
    frames = subject._from_export(subject.PACKAGED_EXPORT).frames
    assert frames["hybrid_threat_composition"] == "thematic"
