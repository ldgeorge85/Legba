# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ONE subject ladder across the reference/grader and the
slug-tail copies (``src/legba/data/subject.py``).

This file proves:

  * the ladder's three rungs, for the code, the name and the kind, on every
    shape a target arrives in (a descriptor body, a flat SQL row, an asyncpg
    record, a bare id);
  * THE GATE (task row SUBJECT-1): all 32 desks resolve identically — the
    (id, code, name, kind) tuples of the live roster's 32 country desks are
    pinned against the pin's values (fcbafb2e) by sha256, and every
    reference/grader call site and every former slug-tail copy yields the
    same code; the supply-chain lanes and flows (no single geo) resolve to
    their own id, never an ISO guess;
  * the seven slug-tail copies are gone: no ``[-2:]`` / ``rsplit("_")`` /
    ``split("_")[-1]`` parse is left in the listed files;
  * the em-dash label: ``unit_names`` is its one producer and one parser, in
    Python and in SQL, profile-styled, byte-identical on the exemplar's style;
  * the packaged export is the default profile's (stale = red), the
    ``subject_code`` schema field is additive (no tree descriptor's dump
    changes), and the reference templates read ``{subject}`` with a
    ``{country}`` shim that renders byte-identically.

DB-free: the I/O helpers run over a recording fake connection.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
from typing import Any

import pytest
import yaml

from legba.data import subject as SUBJECT
from legba.data._polity_match import home_country_name
from legba.data.analysts import unit_names as UN
from legba.data.analysts.deterministic_handlers import _reference_manifest as MANIFEST
from legba.data.analysts.deterministic_handlers import _reference_roster as ROSTER
from legba.data.analysts.deterministic_handlers import calibration_tracking as CT
from legba.data.analysts.deterministic_handlers import collection_gap as CG
from legba.data.analysts.deterministic_handlers import desk_reference as DR
from legba.data.analysts.deterministic_handlers import forecast_acute as FA
from legba.data.analysts.edge_qualification import DESK_CTES
from legba.data.profile import LabelStyle, Unit, load_profile
from legba.data.schemas.target import TargetDescriptor
from legba.runtime import grounding as GR

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DESCRIPTORS = REPO_ROOT / "descriptors"
ROSTER_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "bringup" / "live_head_roster_2026-10-04.json"
SRC = REPO_ROOT / "src" / "legba"


def _heads() -> list[dict[str, Any]]:
    return json.loads(ROSTER_FIXTURE.read_text(encoding="utf-8"))["heads"]


def _body(target_id: str) -> dict[str, Any]:
    return yaml.safe_load((DESCRIPTORS / f"target_{target_id}.yaml").read_text(encoding="utf-8"))


#: The live roster's 32 country desks.
DESKS: tuple[str, ...] = tuple(sorted(
    h["id"] for h in _heads()
    if h["family"] == "target" and h["state"] == "active" and h["id"].startswith("country_")
))
#: Every non-retired target head on the live roster (the desks, the regions,
#: the situation frame, the supply-chain lanes and flows).
LIVE_TARGETS: tuple[str, ...] = tuple(sorted(
    h["id"] for h in _heads() if h["family"] == "target" and h["state"] != "retired"
))
SUPPLY_CHAIN: tuple[str, ...] = tuple(t for t in LIVE_TARGETS if t.startswith(("lane_", "flow_")))

#: THE PIN — (id, code, name, kind) per desk, plus the desk reference's
#: newsroom subject (``target_subject``), as fcbafb2e resolved them. The code is
#: what the pin's grader sliced off the id and the pin's reference roster read
#: off ``scope.geo``; the name is the descriptor's; the subject is the polity
#: matcher's newsroom spelling.
PINNED: tuple[tuple[str, str, str, str, str], ...] = (
    ("country_g20_ar", "AR", "G20 — Argentina", "geo", "Argentina"),
    ("country_g20_au", "AU", "G20 — Australia", "geo", "Australia"),
    ("country_g20_br", "BR", "G20 — Brazil", "geo", "Brazil"),
    ("country_g20_ca", "CA", "G20 — Canada", "geo", "Canada"),
    ("country_g20_cn", "CN", "G20 — China", "geo", "China"),
    ("country_g20_de", "DE", "G20 — Germany", "geo", "Germany"),
    ("country_g20_fr", "FR", "G20 — France", "geo", "France"),
    ("country_g20_gb", "GB", "G20 — United Kingdom", "geo", "Britain"),
    ("country_g20_id", "ID", "G20 — Indonesia", "geo", "Indonesia"),
    ("country_g20_in", "IN", "G20 — India", "geo", "India"),
    ("country_g20_it", "IT", "G20 — Italy", "geo", "Italy"),
    ("country_g20_jp", "JP", "G20 — Japan", "geo", "Japan"),
    ("country_g20_kr", "KR", "G20 — Korea, Republic of", "geo", "South Korea"),
    ("country_g20_mx", "MX", "G20 — Mexico", "geo", "Mexico"),
    ("country_g20_ru", "RU", "G20 — Russian Federation", "geo", "Russia"),
    ("country_g20_sa", "SA", "G20 — Saudi Arabia", "geo", "Saudi Arabia"),
    ("country_g20_tr", "TR", "G20 — Türkiye", "geo", "Turkey"),
    ("country_g20_us", "US", "G20 — United States", "geo", "United States"),
    ("country_g20_za", "ZA", "G20 — South Africa", "geo", "South Africa"),
    ("country_watch_bf", "BF", "Watch — Burkina Faso", "geo", "Burkina Faso"),
    ("country_watch_cd", "CD", "Watch — Congo, The Democratic Republic of the", "geo",
     "Democratic Republic of the Congo"),
    ("country_watch_ht", "HT", "Watch — Haiti", "geo", "Haiti"),
    ("country_watch_il", "IL", "Watch — Israel", "geo", "Israel"),
    ("country_watch_ir", "IR", "Watch — Iran, Islamic Republic of", "geo", "Iran"),
    ("country_watch_kp", "KP", "Watch — Korea, Democratic People's Republic of", "geo",
     "North Korea"),
    ("country_watch_ml", "ML", "Watch — Mali", "geo", "Mali"),
    ("country_watch_mm", "MM", "Watch — Myanmar", "geo", "Myanmar"),
    ("country_watch_ne", "NE", "Watch — Niger", "geo", "Niger"),
    ("country_watch_pk", "PK", "Watch — Pakistan", "geo", "Pakistan"),
    ("country_watch_sd", "SD", "Watch — Sudan", "geo", "Sudan"),
    ("country_watch_tw", "TW", "Watch — Taiwan, Province of China", "geo", "Taiwan"),
    ("country_watch_ua", "UA", "Watch — Ukraine", "geo", "Ukraine"),
)
#: sha256 of the (id, code, name, kind) list, compact JSON.
PINNED_SHA256 = "3990ac730564fc840ad0124022879751dea189870c9b4109a83060cca029a03a"

#: The live ``iso_countries`` names of the 32 desks' codes (read 2026-10-04):
#: the reference builder's SUBJECT line at the pin.
GAZETTEER: dict[str, str] = {
    "AR": "Argentina", "AU": "Australia", "BF": "Burkina Faso", "BR": "Brazil",
    "CA": "Canada", "CD": "Congo, The Democratic Republic of the", "CN": "China",
    "DE": "Germany", "FR": "France", "GB": "United Kingdom", "HT": "Haiti",
    "ID": "Indonesia", "IL": "Israel", "IN": "India", "IR": "Iran, Islamic Republic of",
    "IT": "Italy", "JP": "Japan", "KP": "Korea, Democratic People's Republic of",
    "KR": "Korea, Republic of", "ML": "Mali", "MM": "Myanmar", "MX": "Mexico",
    "NE": "Niger", "PK": "Pakistan", "RU": "Russian Federation", "SA": "Saudi Arabia",
    "SD": "Sudan", "TR": "Türkiye", "TW": "Taiwan, Province of China", "UA": "Ukraine",
    "US": "United States", "ZA": "South Africa",
}


class _Conn:
    """A connection that answers the ladder's two statements from the tree's
    target files and :data:`GAZETTEER`, recording every call."""

    def __init__(self, bodies: dict[str, dict[str, Any]], gazetteer: dict[str, str] | None = None):
        self.bodies = bodies
        self.gazetteer = GAZETTEER if gazetteer is None else gazetteer
        self.calls: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if sql == SUBJECT.HEADS_SQL:
            self.calls.append("heads")
            out = []
            for tid in args[0]:
                b = self.bodies.get(tid)
                if b is None:
                    continue
                scope = b.get("scope") or {}
                out.append({
                    "target_id": tid, "name": b["identity"]["name"],
                    "geo": list(scope.get("geo") or []), "domain": scope.get("domain"),
                    "subject_code": scope.get("subject_code"),
                })
            return out
        if sql == SUBJECT.GAZETTEER_SQL:
            self.calls.append("gazetteer")
            return [{"iso2": c, "name": self.gazetteer[c]} for c in args[0] if c in self.gazetteer]
        raise AssertionError(f"unexpected statement: {sql[:60]!r}")

    async def fetchrow(self, sql: str, *args: Any) -> None:
        self.calls.append("fetchrow")
        return None


class _Record:
    """The asyncpg ``Record`` surface the ladder relies on: ``keys()`` and
    ``[key]``, and NOT a ``Mapping``."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def keys(self):
        return self._data.keys()

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


# ---------------------------------------------------------------------------
# The ladder's three rungs
# ---------------------------------------------------------------------------


def test_code_rung_one_a_declared_subject_code_wins() -> None:
    body = _body("lane_hormuz")
    body["scope"]["subject_code"] = "HORMUZ"
    assert SUBJECT.subject_code(body) == "HORMUZ"
    flat = {"target_id": "x", "geo": ["AR"], "subject_code": "ARX"}
    assert SUBJECT.subject_code(flat) == "ARX"


def test_code_rung_two_the_single_declared_geo_code() -> None:
    assert SUBJECT.subject_code(_body("country_watch_kp")) == "KP"
    assert SUBJECT.subject_code({"target_id": "anything_at_all", "geo": ["pk"]}) == "PK"
    assert SUBJECT.subject_code(_Record({"target_id": "t", "geo": ["IL"]})) == "IL"
    # The geo array as asyncpg hands a jsonb column back: a JSON string.
    assert SUBJECT.subject_code({"descriptor_id": "t", "geo": '["UA"]'}) == "UA"


def test_code_rung_three_the_id_never_its_tail() -> None:
    # The pin's grader sliced these: UZ, RS, AR, HT, NA.
    assert SUBJECT.subject_code(_body("lane_hormuz")) == "lane_hormuz"
    assert SUBJECT.subject_code(_body("flow_semiconductors")) == "flow_semiconductors"
    assert SUBJECT.subject_code(_body("situation_iran_war")) == "situation_iran_war"
    assert SUBJECT.subject_code(_body("flow_container_freight")) == "flow_container_freight"
    assert SUBJECT.subject_code(_body("region_mena")) == "region_mena"
    # An id nothing declares is itself — not "ZZ".
    assert SUBJECT.subject_code("country_watch_zz") == "country_watch_zz"
    assert SUBJECT.subject_code({"target_id": "country_watch_zz", "geo": []}) == "country_watch_zz"


def test_a_bare_id_resolves_through_the_profiles_declared_subjects() -> None:
    assert SUBJECT.resolve("country_g20_ar").as_tuple() == (
        "country_g20_ar", "AR", "G20 — Argentina", "geo",
    )
    assert SUBJECT.subject_geo("lane_red_sea") == ("YE", "ER", "DJ", "EG", "SD", "SO")
    assert SUBJECT.subject_geo("country_watch_zz") == ()


def test_name_rungs_gazetteer_then_descriptor_then_deslugged_id() -> None:
    body = _body("country_g20_tr")
    # Rung 1: the gazetteer (the reference builder), or its newsroom spelling.
    assert SUBJECT.subject_name(body, gazetteer=GAZETTEER) == "Türkiye"
    assert SUBJECT.subject_name(body, gazetteer={}, spell=home_country_name) == "Turkey"
    # Rung 2: no gazetteer handed, or the code is not a declared geo code.
    assert SUBJECT.subject_name(body) == "G20 — Türkiye"
    assert SUBJECT.subject_name(_body("lane_hormuz"), gazetteer=GAZETTEER,
                                spell=home_country_name) == "Lane — Strait of Hormuz"
    # A gazetteer miss falls through to the descriptor name.
    assert SUBJECT.subject_name(body, gazetteer={"AR": "Argentina"}) == "G20 — Türkiye"
    # Rung 3: nothing declared.
    assert SUBJECT.subject_name({"target_id": "some_new_desk"}) == "some new desk"
    assert SUBJECT.subject_name("country_watch_zz") == "country watch zz"


def test_kind_rungs_frame_then_domain_then_nothing() -> None:
    assert SUBJECT.subject_kind("region_mena") == "region"
    assert SUBJECT.subject_kind(_body("region_mena"), frame_kinds={}) == "thematic"
    assert SUBJECT.subject_kind(_body("country_watch_pk")) == "geo"
    assert SUBJECT.subject_kind(_body("lane_hormuz")) == "thematic"
    assert SUBJECT.subject_kind("country_watch_zz") == ""
    assert SUBJECT.subject_kind({"target_id": "x"}, frame_kinds={"x": "regime"}) == "regime"


def test_item_code_is_the_resolved_code_upper_cased() -> None:
    assert SUBJECT.item_code("AR") == "AR"
    assert SUBJECT.item_code("lane_hormuz") == "LANE_HORMUZ"
    assert SUBJECT.item_code(" il ") == "IL"


# ---------------------------------------------------------------------------
# THE GATE — all 32 desks resolve identically
# ---------------------------------------------------------------------------


def test_the_roster_carries_32_desks_and_the_pin_names_each_once() -> None:
    assert len(DESKS) == 32
    assert tuple(p[0] for p in PINNED) == DESKS


def test_all_32_desks_resolve_to_the_pins_values() -> None:
    got = [list(SUBJECT.resolve(_body(d)).as_tuple()) for d in DESKS]
    assert got == [list(p[:4]) for p in PINNED]
    blob = json.dumps(got, ensure_ascii=False, separators=(",", ":"))
    assert hashlib.sha256(blob.encode("utf-8")).hexdigest() == PINNED_SHA256
    # The two pin-era derivations the ladder replaces agree with it on every
    # desk — the identity this change must preserve.
    for tid, code, *_ in PINNED:
        assert tid[-2:].upper() == code                       # the pin's grader
        assert _body(tid)["scope"]["geo"][0].upper() == code  # the pin's reference roster
        assert SUBJECT.resolve(tid).as_tuple() == SUBJECT.resolve(_body(tid)).as_tuple()


async def test_every_reference_and_grader_call_site_yields_the_same_code() -> None:
    bodies = {t: _body(t) for t in LIVE_TARGETS}
    conn = _Conn(bodies)
    ladder = {t: SUBJECT.resolve(bodies[t]) for t in LIVE_TARGETS}

    # Layer 1/2: the reference roster (code + gazetteer name).
    identity = await ROSTER.target_identity(conn, list(LIVE_TARGETS))
    # Layer 3/4: the grader's code (item id, packet kind).
    loaded = await SUBJECT.load_subjects(conn, list(LIVE_TARGETS))
    for tid in LIVE_TARGETS:
        code = ladder[tid].code
        assert identity[tid]["code"] == code, tid
        assert loaded[tid].code == code, tid
        assert (await SUBJECT.load_subject(conn, tid)).code == code
    for tid, code, name, _kind, newsroom in PINNED:
        assert identity[tid]["name"] == GAZETTEER[code]          # the pin's SUBJECT line
        # Layer 5: the desk reference's query subject.
        b = bodies[tid]
        assert DR.target_subject(
            target_id=tid, name=b["identity"]["name"], geo=b["scope"]["geo"],
            iso_names=GAZETTEER,
        ) == newsroom
        # Layer 6: the anchorability rule's home polity.
        target = {"geo": b["scope"]["geo"], "iso_names": GAZETTEER}
        assert GAZETTEER[code] in DR._home_blob_for(target)


async def test_every_former_slug_tail_copy_yields_the_ladders_code() -> None:
    conn = _Conn({})  # no live head: every copy falls through to the ladder
    for tid, code, *_ in PINNED:
        assert CG._desk_iso2(tid) == code
        assert tuple(GR._target_id_geo_names(tid)) == (code.lower(),)
        assert GR.world_context_country_filter_values(tid) == [code.lower(), code]
        assert await FA._region_geo(conn, tid) == [code]
        assert await CT._resolve_region_geo(conn, tid) == [code]


async def test_supply_chain_lanes_and_flows_resolve_to_their_id_not_an_iso_guess() -> None:
    assert SUPPLY_CHAIN, "the roster carries the supply-chain targets"
    conn = _Conn({t: _body(t) for t in SUPPLY_CHAIN})
    identity = await ROSTER.target_identity(conn, list(SUPPLY_CHAIN))
    for tid in SUPPLY_CHAIN:
        body = _body(tid)
        assert SUBJECT.subject_code(body) == tid
        assert identity[tid] == {"code": tid, "name": body["identity"]["name"]}
        assert SUBJECT.item_code(SUBJECT.subject_code(body)) == tid.upper()
        assert CG._desk_iso2(tid) is None
        assert tuple(GR._target_id_geo_names(tid)) == ()


async def test_a_target_whose_head_does_not_answer_degrades_by_the_ladder() -> None:
    identity = await ROSTER.target_identity(_Conn({}), ["country_g20_ar", "country_watch_zz"])
    assert identity["country_g20_ar"] == {"code": "AR", "name": "G20 — Argentina"}
    assert identity["country_watch_zz"] == {"code": "country_watch_zz", "name": "country watch zz"}


# ---------------------------------------------------------------------------
# The seven copies are gone
# ---------------------------------------------------------------------------

#: The reference/grader layers and the seven slug-tail copies (Review A
#: §1.3/§1.4), as files.
LISTED_FILES: tuple[str, ...] = (
    "data/analysts/deterministic_handlers/correctness_grader.py",
    "data/analysts/deterministic_handlers/_reference_roster.py",
    "data/analysts/deterministic_handlers/_correctness_packet.py",
    "data/analysts/deterministic_handlers/_reference_manifest.py",
    "data/analysts/deterministic_handlers/_reference_diff.py",
    "data/analysts/deterministic_handlers/_reference_gap_dispatch.py",
    "data/analysts/deterministic_handlers/reference_builder.py",
    "data/analysts/deterministic_handlers/desk_reference.py",
    "data/analysts/deterministic_handlers/scorecard_producer.py",
    "data/analysts/deterministic_handlers/forecast_acute.py",
    "data/analysts/deterministic_handlers/calibration_tracking.py",
    "data/analysts/deterministic_handlers/collection_gap.py",
    "runtime/grounding.py",
)
_SLUG_TAIL = re.compile(
    r"""\[\s*-2\s*:\s*\]"""                              # id[-2:]
    r"""|\.rsplit\(\s*["']_["']"""                       # .rsplit("_", 1)
    r"""|\.split\(\s*["']_["']\s*\)\s*\[\s*-\s*1\s*\]"""  # .split("_")[-1]
)


def test_no_slug_tail_iso_parse_remains_in_the_listed_files() -> None:
    hits = []
    for rel in LISTED_FILES:
        for n, line in enumerate((SRC / rel).read_text(encoding="utf-8").splitlines(), 1):
            if _SLUG_TAIL.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert hits == []


def test_the_grader_and_the_builder_take_the_ladders_code() -> None:
    grader = (SRC / LISTED_FILES[0]).read_text(encoding="utf-8")
    assert "SUBJECT.item_code((await SUBJECT.load_subject(conn, target_id)).code)" in grader
    builder = (SRC / "data/analysts/deterministic_handlers/reference_builder.py").read_text(encoding="utf-8")
    assert 'f"RD-{SUBJECT.item_code(code or target_id)}-C"' in builder


# ---------------------------------------------------------------------------
# The label: one producer, one parser
# ---------------------------------------------------------------------------

#: The pin's parser (assessment_unsupported) and the pin's SQL (edge_qualification).
_PIN_SPLIT = re.compile(r"\s+[—–-]\s+")
_PIN_DESK_CTES_SHA256 = "978faf5e3454cbdc24650c0fd871828c2938691078fa4be096c711b3d6964a97"


def _names() -> list[str]:
    return [_body(t)["identity"]["name"] for t in LIVE_TARGETS]


def test_label_round_trips_on_every_live_target_name() -> None:
    for tid, display in zip(LIVE_TARGETS, _names()):
        parsed = UN.parse_unit_label(UN.unit_label(display, tid))
        assert parsed.slug == tid
        assert UN.subject_display(parsed.tier, parsed.name) == display
        assert UN.unit_label(parsed.name, tid, tier=parsed.tier) == UN.unit_label(display, tid)


def test_label_name_is_the_pins_split_byte_for_byte() -> None:
    samples = _names() + ["plain", "x-y", "A – B - C", " lead — trail ", ""]
    for text in samples:
        assert UN.label_name(text) == _PIN_SPLIT.split(text)[-1], text


def test_the_sql_parser_renders_the_pins_desk_ctes() -> None:
    assert hashlib.sha256(DESK_CTES.encode("utf-8")).hexdigest() == _PIN_DESK_CTES_SHA256
    assert UN.label_name_sql("td.name") in DESK_CTES


def test_the_label_is_profile_styled() -> None:
    style = LabelStyle(separator=":", separator_aliases=(), order=("name", "tier"))
    assert UN.subject_display("Regime", "BIS Entity List", style=style) == "BIS Entity List : Regime"
    parsed = UN.parse_unit_label("BIS Entity List : Regime (regime_bis_entity_list)", style=style)
    assert parsed == UN.UnitLabel(tier="Regime", name="BIS Entity List", slug="regime_bis_entity_list")
    assert UN.label_name("BIS Entity List : Regime", style=style) == "BIS Entity List"
    assert "split_part(td.name, ':', 1)" in UN.label_name_sql("td.name", style=style)
    with pytest.raises(ValueError):
        LabelStyle(separator="'")
    with pytest.raises(ValueError):
        LabelStyle(order=("tier", "tier"))


def test_unit_label_without_a_tier_is_unchanged() -> None:
    assert UN.unit_label("Watch — Pakistan", "country_watch_pk") == "Watch — Pakistan (country_watch_pk)"
    assert UN.unit_label("", "country_watch_pk") == "country_watch_pk"
    assert UN.unit_label("country_watch_pk", "country_watch_pk") == "country_watch_pk"
    assert UN.unit_label("Pakistan", "country_watch_pk", tier="Watch") == "Watch — Pakistan (country_watch_pk)"


# ---------------------------------------------------------------------------
# The export, the schema field, the templates
# ---------------------------------------------------------------------------


def test_the_packaged_export_is_the_default_profiles() -> None:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import gen_profile_subjects  # noqa: E402

    assert SUBJECT.PACKAGED_EXPORT.read_text(encoding="utf-8") == gen_profile_subjects.render(None)
    assert gen_profile_subjects.main(["--check"]) == 0
    index = SUBJECT.current()
    assert index.profile == "geopolitics"
    assert set(LIVE_TARGETS) <= set(index.subjects)
    assert index.label == load_profile(REPO_ROOT / "profiles" / "geopolitics").subjects.label


def test_another_profile_exports_its_own_subjects() -> None:
    other = load_profile(REPO_ROOT / "profiles" / "sanctions_export_control")
    doc = json.loads(SUBJECT.export_subjects(other))
    assert doc["profile"] == "sanctions_export_control"
    ids = {s["id"] for s in doc["subjects"]}
    assert ids and not ids & set(DESKS)


def test_subject_code_on_the_schema_is_additive() -> None:
    for tid in LIVE_TARGETS:
        model = TargetDescriptor.model_validate(_body(tid), strict=False)
        assert "subject_code" not in model.model_dump(mode="json")["scope"], tid
    body = _body("lane_hormuz")
    body["scope"]["subject_code"] = "HORMUZ"
    model = TargetDescriptor.model_validate(body, strict=False)
    dumped = model.model_dump(mode="json")
    assert dumped["scope"]["subject_code"] == "HORMUZ"
    assert SUBJECT.subject_code(dumped) == "HORMUZ"
    body["scope"]["subject_code"] = "not a code"
    with pytest.raises(ValueError):
        TargetDescriptor.model_validate(body, strict=False)


def test_templates_read_subject_with_a_country_shim() -> None:
    subject_form = ("{subject} cabinet reshuffle",)
    country_form = ("{country} cabinet reshuffle",)
    old = MANIFEST.QUERY_PATTERNS
    try:
        MANIFEST.QUERY_PATTERNS = {"leadership_transition": subject_form}
        a = MANIFEST.query_guidance("Australia", ["leadership_transition", "new_unit"])
        MANIFEST.QUERY_PATTERNS = {"leadership_transition": country_form}
        b = MANIFEST.query_guidance("Australia", ["leadership_transition", "new_unit"])
    finally:
        MANIFEST.QUERY_PATTERNS = old
    assert a == b
    assert "`Australia cabinet reshuffle`" in a
    assert "`Australia new unit announcement`" in a


def test_the_profile_accepts_either_spelling_and_refuses_neither() -> None:
    Unit(id="u", reference_queries=("{subject} event",))
    Unit(id="u", reference_queries=("{country} event",))
    with pytest.raises(ValueError):
        Unit(id="u", reference_queries=("event with no subject",))
    with pytest.raises(ValueError):
        Unit(id="u", reference_queries=("{region} event",))
