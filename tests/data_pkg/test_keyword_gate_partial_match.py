# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The keyword gate's per-term partial match (``*`` at a term end),
and the Bundeswehr procurement gate that needed it.

The Bundeswehr source wrote zero signals: its whole-word gate missed German
compounds ("Beschaffungsamt", "Rüstungsindustrie"). Plain terms must behave
exactly as before; only a term that carries ``*`` changes.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from pydantic import ValidationError

from legba.data.filters.keyword_gate import KeywordGate, KeywordGateConfig
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources._contract import Signal
from legba.data.sources.rss import RSSConfig, RSSSourceHandler
from legba.runtime.source_factory import _unwrap_factory_dict

REPO = pathlib.Path(__file__).resolve().parents[2]


def _sig(title: str, body: str = "") -> Signal:
    return Signal(source_id="s", payload={"title": title, "raw_body": body})


def _admits(terms: list[str], title: str) -> bool:
    return KeywordGate(KeywordGateConfig(include=terms)).evaluate(_sig(title)).admit


@pytest.mark.parametrize("term, title, admit", [
    # plain terms: whole word, exactly as before
    ("iran", "Floods in Mirandola", False),
    ("Iran", "Iran-backed group", True),
    ("Beschaffung", "Das Beschaffungsamt meldet", False),
    ("export control", "export controls widen", False),
    # prefix
    ("Beschaffung*", "Das Beschaffungsamt meldet", True),
    ("Beschaffung*", "Waffenbeschaffung beschlossen", False),
    # suffix
    ("*vertrag", "Rahmenvertrag unterzeichnet", True),
    ("*vertrag", "Vertragsunterzeichnung", False),
    # substring
    ("*rüstung*", "Rüstungsindustrie wächst", True),
    ("*rüstung*", "Neue Ausrüstung für das Heer", True),
    ("*beschaff*", "Bundeswehr beschafft Fahrzeuge", True),
    ("*beschaff*", "Waffenbeschaffung beschlossen", True),
    ("*beschaff*", "Tag der offenen Tür", False),
    # a multi-word starred term still spans whitespace runs
    ("export control*", "export\n  controls widen", True),
])
def test_partial_match_markers(term, title, admit):
    assert _admits([term], title) is admit


@pytest.mark.parametrize("bad", ["*", "**", "Be*schaffung", " * ", "a * b"])
def test_misplaced_star_is_refused_at_config_parse(bad):
    with pytest.raises(ValidationError):
        KeywordGateConfig(include=[bad])
    with pytest.raises(ValidationError):
        RSSConfig(url="https://x.invalid/feed", keyword_include=[bad])


def test_no_other_shipped_descriptor_uses_the_marker():
    """Every other gated source keeps whole-word matching byte for byte."""
    users = []
    for path in sorted((REPO / "descriptors").glob("*.yaml")):
        text = path.read_text()
        if "keyword_include" not in text and "keyword_exclude" not in text and "keyword_gate" not in text:
            continue
        body = yaml.safe_load(text) or {}
        cfg = body.get("config") or {}
        terms = []
        for key in ("keyword_include", "keyword_exclude"):
            v = cfg.get(key)
            terms += (v.get("raw") if isinstance(v, dict) else v) or []
        for stage in (body.get("pipeline") or {}).get("ingestion_filters") or []:
            if stage.get("kind") == "keyword_gate":
                terms += (stage.get("config") or {}).get("include") or []
                terms += (stage.get("config") or {}).get("exclude") or []
        if any("*" in t for t in terms):
            users.append(path.name)
    assert users == ["source_bundeswehr_procurement.yaml"]


def _bundeswehr_gate() -> KeywordGate:
    path = REPO / "descriptors" / "source_bundeswehr_procurement.yaml"
    desc = SourceDescriptor.model_validate(yaml.safe_load(path.read_text()), strict=False)
    handler = RSSSourceHandler(RSSConfig(**_unwrap_factory_dict(desc.config)))
    assert handler.keyword_gate is not None
    return handler.keyword_gate


@pytest.mark.parametrize("title, admit", [
    ("BAAINBw vergibt Auftrag für Schützenpanzer", True),
    ("Beschaffungsamt der Bundeswehr zieht Bilanz", True),
    ("Rüstungsindustrie: neue Fertigungslinie in Unterlüß", True),
    ("Haushaltsausschuss billigt 25-Millionen-Euro-Vorlagen", True),
    ("Rahmenvertrag über Funkgeräte unterzeichnet", True),
    ("Neue Ausrüstung für die Gebirgsjäger", True),
    ("Karriere: Bewerbungstage im Dezember", False),
    ("Bundeswehr-Shop: Kalender 2027", False),
    ("Vor 70 Jahren: die ersten Freiwilligen", False),
])
def test_bundeswehr_gate_admits_german_compounds(title, admit):
    assert _bundeswehr_gate().evaluate(_sig(title)).admit is admit
