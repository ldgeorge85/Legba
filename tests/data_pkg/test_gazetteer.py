# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W10 CANON-1 — the five gazetteers are ONE profile file behind ONE loader.

``profiles/<id>/rosters/gazetteer.yaml`` (the manifest's ``rosters.gazetteer``)
is read through ``src/legba/data/gazetteer.py``. This file proves:

  * BYTE-IDENTITY — every table the geopolitics file supplies equals the code
    literal it replaced at the pin ``fcbafb2e``: a sha256 per table over a
    canonical form that keeps ORDER for dicts and tuples (the officeholder
    groups feed a regex alternation) and sorts sets; the tables the consumers
    DERIVE (the screen stoplist, the polity canon, the country gazetteer) are
    pinned the same way;
  * the two officeholder extractor regexes compile to the SAME pattern strings
    the pin's code builds from the pin's literals, and the CAMEO classifier is
    the pin's pattern string;
  * VERDICT PARITY — 50 invented signal titles (machine-coded event titles,
    officeholder-shaped headlines, colon headlines) get exactly the recorded
    verdicts through the title classifier, the officeholder
    extractor, the stale-leader guard, the cross-target guard and the V-B
    claim-scope resolver;
  * NOT APPLICABLE BY NAME — a profile that declares a guard's sections not
    gets a typed ``NotApplicable`` (counted, logged once, labelled on the
    verify report), never a silent pass; a section reads as ``Absent`` naming
    itself;
  * grounding REFUSES an unresolvable scope by name, takes a declared
    single-code scope off the target head, and refuses a filter it cannot
    build;
  * the sanctions skeleton's gazetteer loads and declares only what it can;
  * the packaged export is the default profile's (stale = red), and the file
    model is closed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import re
import sys
from typing import Any

import pytest
from pydantic import ValidationError

from legba.data import gazetteer as G
from legba.data.profile import load_profile

# The consumers build module constants from the gazetteer AT IMPORT (the entity
# canon's maps, the polity canon, the screen stoplist). Import them here, under
# the process's own gazetteer, so a test below that injects an empty one can
# never be the first importer (a shuffled run would otherwise freeze it in).
from legba.data import _entity_canon, _geo_routing, _polity_match  # noqa: E402,F401
from legba.data.provenance import absence_slice, verify, world_knowledge_guards  # noqa: E402,F401
from legba.runtime import grounding  # noqa: E402,F401

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GEO = REPO_ROOT / "profiles" / "geopolitics"
SANCTIONS = REPO_ROOT / "profiles" / "sanctions_export_control"
VERDICTS = pathlib.Path(__file__).parent / "fixtures" / "gazetteer" / "title_verdicts_pin.json"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_profile_gazetteer  # noqa: E402


# ---------------------------------------------------------------------------
# The canonical digest (order kept for dict/tuple/list, sets sorted)
# ---------------------------------------------------------------------------


def _canon(v: Any) -> Any:
    if isinstance(v, dict):
        return ["dict", [[_canon(k), _canon(x)] for k, x in v.items()]]
    if isinstance(v, tuple):
        return ["tuple", [_canon(x) for x in v]]
    if isinstance(v, list):
        return ["list", [_canon(x) for x in v]]
    if isinstance(v, (set, frozenset)):
        return ["set", sorted(json.dumps(_canon(x), ensure_ascii=False) for x in v)]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    raise TypeError(type(v))


def _digest(v: Any) -> str:
    return hashlib.sha256(
        json.dumps(_canon(v), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


#: The pin's (fcbafb2e) literal per table, sha256 of :func:`_canon` — computed
#: by importing the pin's modules, before any line moved.
PIN_DIGESTS: dict[str, str] = {
    "absence_slice._SLICE_DESK_COUNTRIES": "49aafe928e01236e76fcc00ea8952d31ae6311264a434daacdc26dfa9f6ddd47",
    "absence_slice._SLICE_GEO_COUNTRIES": "105b168853e00e475f366c7b083d82eadea23df42382b4c1cb7cbf3d3e0c69da",
    "absence_slice._TARGET_SLUG_TO_COUNTRY": "9d2790aec983f21c4b82a2943c537de0d67f9c03a341a9287e3e59b49cb2c453",
    "absence_slice._COUNTRY_TOKENS": "2b3cdbb30815a3343cd8f9afe6a65b0bec880a9bc16271bf245d87f346e868cb",
    "absence_slice._TARGET_SLUG_TO_ABBREV": "48846dcf3c935b134345b4176db3f38ecdb9810dee28eedc7725e9e3242d8bd6",
    "absence_slice._IRREGULAR_DEMONYMS": "57cd3626d49d98170f6c4071e744df42c2a32d9305b86b66ce4c41d48c103b8f",
    "absence_slice._ABSENCE_SCREEN_STOPWORDS": "01537a1a06c58293d4231e2dd70bcc624d9f3674f558300dc0c17f8c9c2bb5e6",
    "absence_slice._CAMEO_TITLE_RE.pattern": "fe303a2371030bfaca4108f57d83ccd778572b214c830260bd8dbb1acfbe8eae",
    "world_knowledge_guards._OFFICEHOLDER_COUNTRY_GROUPS": "178e0e756aa23e35837788f4d18745a2e3cf048dc7bab0c68f19b03a0f394775",
    "world_knowledge_guards._OFFICEHOLDER_ACRONYMS": "ba6eaf755c420804c6cc3be4f30719104dcd747b2db695119ef17fe93838ad36",
    "world_knowledge_guards._OFFICEHOLDER_ROLE_PREDICATES": "e2c1a7eab54a53cbf209de605238ebc820e97976b583781db5f981d28f294b9e",
    "world_knowledge_guards._OFFICEHOLDER_FAMILY_PREDICATES": "025bf23ccf0f082d6954f19656deca16eea677fb7c7430b1e549ac679526857a",
    "world_knowledge_guards._LEADER_OF_PREDICATE": "d74849540562e61b1332faae768f63bed58fd9a18cc4559884b05ab42f749792",
    "writes_supersession._FUNCTIONAL_ROLE_COUNTRY_SIDE": "53789eb488eea891100711185b6d445a9a25ca3ff03a07cd923e56f54b76fa42",
    "_polity_match._ISO2_HOME_ALIASES": "185a1fcf06b835de6485b638ab6d88f31b74ee9bfab47493dd426fb8335ef4ab",
    "_polity_match._SURFACES": "a4e886179e3ad9f15fd3b507c3dd2b797874bc2944aa40298566492ce13c4397",
    "_geo_routing.CANON_POLITIES": "82e89f896aa65e7e4974ead5362b35d669f02b6266ebaca968e6b231a3439fd6",
    "_entity_canon._ALIAS_MAP": "f8a71df79fb5b722d472c6de8378514a5c9333d030e406e413325be559e5de82",
    "_entity_canon._ALIAS_ORG": "4b9cb98f3f5fdef8b2641f1ec4ee092d7d4722d02fb9dd7b143dd0a8d5cf318b",
    "_entity_canon._DEMONYM_MAP": "33d9ac6efcf8f2d66101869863b16f3b3cfd23c672c5403a9c0c007309dff8be",
    "_entity_canon._REGION_ADJECTIVE_MAP": "0e5afef26562468f0fbe5e24df29f45bc9b3a073f5922fa6f43c6b81944a641d",
    "_entity_canon._KNOWN_REGIONS": "ea68812b6793eb788e6a8e38fef62983ce649caa7e6c84ca10592e49d21506d4",
    # the inline curated set of _entity_canon._country_name_set (20 names)
    "_entity_canon._country_name_set.curated": "6413aee60e5ba4bd6b9c26121961bbf8afb8b3d537322119776baefee50205b6",
    "grounding._TARGET_SLUG_TO_NAMES": "cad30ae809302ee4961db293ac96b74487efc3fe93a8e3cfd8d453099b3e6823",
    "grounding._KNOWN_COUNTRY_TOKENS": "da4c9a428d011c38550841b29ca0f8a05dcf3b111f88040459e4a073a12e3e1f",
}

#: The pin's officeholder literals, frozen (34 groups, 5 acronyms) — the input
#: the pin's alternation code ran on, to prove the pattern STRINGS equal.
_PIN_GROUPS: tuple[tuple[str, ...], ...] = (
    ("united states", "united states of america", "america", "usa"),
    ("united kingdom", "great britain", "britain"),
    ("russia", "russian federation"),
    ("china", "people's republic of china"),
    ("india",), ("france",), ("germany",), ("italy",), ("japan",),
    ("canada",), ("mexico",), ("brazil",), ("argentina",), ("australia",),
    ("turkey", "turkiye"), ("iran",), ("israel",), ("ukraine",),
    ("saudi arabia",), ("south korea",), ("north korea",), ("south africa",),
    ("indonesia",), ("pakistan",), ("venezuela",), ("slovenia",),
    ("somalia",), ("sudan",), ("egypt",), ("nigeria",), ("poland",),
    ("spain",), ("netherlands",),
    ("democratic republic of the congo", "dr congo", "drc", "congo-kinshasa"),
)
_PIN_ACRONYMS: dict[str, tuple[str, ...]] = {
    "US": _PIN_GROUPS[0], "U.S.": _PIN_GROUPS[0], "USA": _PIN_GROUPS[0],
    "UK": _PIN_GROUPS[1], "DRC": _PIN_GROUPS[-1],
}
_PIN_CAMEO = (
    r"^\s*[A-Z][A-Z0-9 .,'’()&/\-]{1,}"
    r"(?:<->\s*[A-Z0-9 .,'’()&/\-]+)?"
    r":\s+[a-z]"
)


def _pin_patterns() -> tuple[str, str]:
    """The pin's two extractor patterns, by the pin's own code, verbatim."""
    names = sorted({n for grp in _PIN_GROUPS for n in grp}, key=len, reverse=True)
    ci = "|".join(re.escape(n) for n in names)
    acro = "|".join(re.escape(a) for a in _PIN_ACRONYMS)
    alt = f"(?:(?i:{ci})|(?:{acro}))"
    name_re = r"[A-Z][\w'’.\-]+(?:\s+[A-Z][\w'’.\-]+){0,3}"
    role_re = r"(?i:prime\s+minister|president|chancellor|premier)"
    country_first = re.compile(
        rf"\b(?P<country>{alt})(?:['’]s)?\s+"
        rf"(?:(?i:current|sitting|incumbent|new)\s+)?"
        rf"(?P<role>{role_re})\s+"
        rf"(?P<name>{name_re})"
    )
    role_first = re.compile(
        rf"\b(?P<role>{role_re})\s+"
        rf"(?P<name>{name_re})\s+of\s+(?:(?i:the)\s+)?"
        rf"(?P<country>{alt})(?![a-z0-9])"
    )
    return country_first.pattern, role_first.pattern


@pytest.fixture
def restore_gazetteer():
    """A test that injects a gazetteer puts the process's back, census cleared."""
    G.reset_census()
    yield
    G.refresh()
    G.reset_census()


def _empty_gazetteer() -> G.Gazetteer:
    return G.build(None, profile="no_gazetteer", origin="test:empty")


# ---------------------------------------------------------------------------
# Byte identity
# ---------------------------------------------------------------------------


def _loaded_tables() -> dict[str, Any]:
    from legba.data import _entity_canon as canon
    from legba.data import _geo_routing as geo
    from legba.data import _polity_match as pm
    from legba.data.provenance import absence_slice as AS

    gz = G.gazetteer()
    offices = gz.get("polity_offices")
    return {
        "absence_slice._SLICE_DESK_COUNTRIES": gz.get("desk_countries"),
        "absence_slice._SLICE_GEO_COUNTRIES": gz.get("slice_geo_countries"),
        "absence_slice._TARGET_SLUG_TO_COUNTRY": gz.get("slug_to_country"),
        "absence_slice._COUNTRY_TOKENS": gz.get("country_tokens"),
        "absence_slice._TARGET_SLUG_TO_ABBREV": gz.get("slug_to_abbrev"),
        "absence_slice._IRREGULAR_DEMONYMS": gz.get("irregular_demonyms"),
        "absence_slice._ABSENCE_SCREEN_STOPWORDS": AS._ABSENCE_SCREEN_STOPWORDS,
        "absence_slice._CAMEO_TITLE_RE.pattern": AS._machine_title_re().pattern,
        "world_knowledge_guards._OFFICEHOLDER_COUNTRY_GROUPS": gz.get("officeholder_country_groups"),
        "world_knowledge_guards._OFFICEHOLDER_ACRONYMS": gz.get("officeholder_acronyms"),
        "world_knowledge_guards._OFFICEHOLDER_ROLE_PREDICATES": offices.role_predicates,
        "world_knowledge_guards._OFFICEHOLDER_FAMILY_PREDICATES": offices.family_predicates,
        "world_knowledge_guards._LEADER_OF_PREDICATE": offices.leader_of,
        "writes_supersession._FUNCTIONAL_ROLE_COUNTRY_SIDE": gz.get("functional_role_country_side"),
        "_polity_match._ISO2_HOME_ALIASES": pm._ISO2_HOME_ALIASES,
        "_polity_match._SURFACES": pm._SURFACES,
        "_geo_routing.CANON_POLITIES": geo.CANON_POLITIES,
        "_entity_canon._ALIAS_MAP": canon._ALIAS_MAP,
        "_entity_canon._ALIAS_ORG": canon._ALIAS_ORG,
        "_entity_canon._DEMONYM_MAP": canon._DEMONYM_MAP,
        "_entity_canon._REGION_ADJECTIVE_MAP": canon._REGION_ADJECTIVE_MAP,
        "_entity_canon._KNOWN_REGIONS": canon._KNOWN_REGIONS,
        "_entity_canon._country_name_set.curated": gz.get("polities").curated,
        "grounding._TARGET_SLUG_TO_NAMES": gz.get("target_slug_names"),
        "grounding._KNOWN_COUNTRY_TOKENS": gz.get("known_country_tokens"),
    }


@pytest.mark.parametrize("table", sorted(PIN_DIGESTS))
def test_every_table_is_the_pins_literal_byte_for_byte(table):
    assert _digest(_loaded_tables()[table]) == PIN_DIGESTS[table], table


def test_the_tables_keep_the_literals_python_types():
    gz = G.gazetteer()
    assert isinstance(gz.get("officeholder_country_groups"), tuple)
    assert all(isinstance(g, tuple) for g in gz.get("officeholder_country_groups"))
    # an acronym resolves to the very group tuple, as the pin's GROUPS[i] did
    groups = gz.get("officeholder_country_groups")
    assert gz.get("officeholder_acronyms")["DRC"] is groups[-1]
    for name in ("country_tokens", "known_regions", "alias_org", "known_country_tokens",
                 "slice_geo_countries", "screen_stopwords"):
        assert isinstance(gz.get(name), frozenset), name
    assert list(gz.get("desk_countries"))[:3] == ["ar", "au", "br"]  # file order kept


def test_the_country_gazetteer_is_derived_from_pycountry_plus_the_curated_names():
    pycountry = pytest.importorskip("pycountry")
    from legba.data import _entity_canon as canon

    expected: set[str] = set()
    for c in pycountry.countries:
        expected.add(c.name.lower())
        for attr in ("common_name", "official_name"):
            v = getattr(c, attr, None)
            if isinstance(v, str):
                expected.add(v.lower())
    expected |= set(G.gazetteer().get("polities").curated)
    assert canon._country_name_set() == frozenset(expected)
    assert G.gazetteer().get("polities").universe == "pycountry"


def test_the_two_officeholder_regexes_compile_to_the_pins_pattern_strings():
    from legba.data.provenance import world_knowledge_guards as W

    assert G.gazetteer().get("officeholder_country_groups") == _PIN_GROUPS
    assert G.gazetteer().get("officeholder_acronyms") == _PIN_ACRONYMS
    tables = W._officeholder_tables(G.gazetteer())
    country_first, role_first = _pin_patterns()
    assert tables.country_first.pattern == country_first
    assert tables.role_first.pattern == role_first
    assert tables.country_first.flags == re.compile(country_first).flags


def test_the_cameo_classifier_is_the_pins_pattern_string():
    from legba.data.provenance import absence_slice as AS

    shape = AS._machine_title_re()
    assert shape.pattern == _PIN_CAMEO
    assert shape.flags == re.compile(_PIN_CAMEO).flags


# ---------------------------------------------------------------------------
# Verdict parity over 50 invented titles
# ---------------------------------------------------------------------------

_DOC = json.loads(VERDICTS.read_text(encoding="utf-8"))


def test_the_fixture_is_fifty_invented_titles():
    assert _DOC["pin"] == "fcbafb2e"
    rows = _DOC["rows"]
    assert len(rows) == 50
    assert sum(r["verdicts"]["cameo_title"] for r in rows) == 15
    assert sum(any(r["verdicts"]["cross_target_leak"].values()) for r in rows) > 0
    assert sum(bool(r["verdicts"]["officeholder_claims"]) for r in rows) > 0


@pytest.mark.parametrize("i", range(50))
def test_fifty_titles_get_the_recorded_verdicts(i):
    from legba.data.provenance import absence_slice as AS
    from legba.data.provenance import world_knowledge_guards as W

    row = _DOC["rows"][i]
    t = row["title"]
    got = {
        "cameo_title": bool(AS._machine_title_re().match(t)),
        "machine_structured": AS._is_machine_structured_row(
            source_id=row["source_id"], provenance_kind="", title=t,
        ),
        "officeholder_claims": [
            [c.role, c.person, c.country_surface, list(c.country_aliases)]
            for c in W.extract_officeholder_claims(t)
        ],
        "stale_leader": len(W.stale_leader_spans(t)),
        "cross_target_leak": {
            d: W.cross_target_leak_span(title=t, body="", target_id=d) is not None
            for d in row["verdicts"]["cross_target_leak"]
        },
        "slice_scope": sorted(AS._slice_scope_countries(t, target_id="country_g20_us")),
    }
    assert got == row["verdicts"], t


# ---------------------------------------------------------------------------
# Not applicable, by name
# ---------------------------------------------------------------------------


def test_an_undeclared_section_reads_absent_by_name(restore_gazetteer):
    gz = G.refresh(_empty_gazetteer())
    assert gz.declared == ()
    absent = gz.section("country_tokens")
    assert isinstance(absent, G.Absent) and absent.section == "country_tokens"
    assert not absent and "country_tokens" in repr(absent)
    with pytest.raises(KeyError, match="no gazetteer section"):
        gz.section("no_such_section")
    # a canonicaliser reads the empty value of the type, recorded by name
    assert G.or_empty("alias_map", consumer="t") == {}
    assert G.or_empty("known_regions", consumer="t") == frozenset()
    assert G.absent_reads() == {"t:alias_map": 1, "t:known_regions": 1}


def test_every_guard_is_not_applicable_by_name_without_its_sections(restore_gazetteer, caplog):
    from legba.data.provenance import absence_slice as AS
    from legba.data.provenance import world_knowledge_guards as W
    from legba.runtime.grounding import finding_is_off_target

    G.refresh(_empty_gazetteer())
    with caplog.at_level(logging.WARNING, logger="legba.data.gazetteer"):
        leak = W.cross_target_leak_span(
            title="Romania names only Romania", body="", target_id="country_g20_tr",
        )
        assert isinstance(leak, G.NotApplicable) and not leak
        assert leak.guard == "cross_target_leak" and leak.seam == 67
        assert leak.missing == ("desk_ids", "slug_to_country", "country_tokens")
        assert leak.counter == "gazetteer_not_applicable_cross_target_leak"
        # a second call is counted, not logged again
        W.cross_target_leak_span(title="x", body="", target_id="country_g20_tr")
        assert W.extract_officeholder_claims("US President Donald Trump said") == []
        assert AS._slice_scope_countries("No new sanctions on Haiti", target_id="country_watch_ht") == frozenset()
        assert AS._is_machine_structured_row(
            source_id="", provenance_kind="", title="COLLEGE: protest in Japan",
        ) is False
        assert finding_is_off_target(
            target_id="country_g20_id", text="The United States and China agreed",
        ) is False
    census = G.not_applicable_census()
    assert census == {
        "cross_target_leak": 2, "officeholder_vs_facts": 1, "absence_slice_scope": 1,
        "machine_structured_title": 1, "off_target": 1,
    }
    logged = [r.getMessage() for r in caplog.records if "not_applicable" in r.getMessage()]
    assert len(logged) == 5  # once per guard, not once per call
    assert all("SEAMS #67" in m for m in logged)


async def test_functional_role_supersession_is_not_applicable_and_touches_nothing(restore_gazetteer):
    from uuid import uuid4

    from legba.data.provenance.writes_supersession import supersede_prior_functional_role_facts

    class _NoConn:
        async def execute(self, *a: Any, **k: Any) -> str:
            raise AssertionError("a not-applicable guard must not write")

    G.refresh(_empty_gazetteer())
    closed = await supersede_prior_functional_role_facts(
        _NoConn(), subject="X", predicate="leader of", value="Y", role="president",
        new_fact_id=uuid4(),
    )
    assert closed == 0
    assert G.not_applicable_census() == {"functional_role_supersession": 1}


def test_the_verify_fold_labels_the_report_by_name(restore_gazetteer):
    from legba.data.provenance import verify as V

    G.refresh(_empty_gazetteer())
    floor = V.FaithfulnessReport(faithfulness_score=1.0, checkable_claims=0, supported_claims=0)
    out = V._fold_world_knowledge_guards(
        floor, title="Romania only", body="Romania again", target_id="country_g20_tr",
    )
    assert out.counters.get("gazetteer_not_applicable_cross_target_leak") == 1
    assert out.unsupported_spans == []  # labelled, never flagged and never passed silently


def test_on_the_exemplar_every_guard_is_applicable():
    gz = G.gazetteer()
    assert gz.profile == "geopolitics"
    for guard in G.GUARD_SECTIONS:
        assert G.applicable(guard, gz, record=False) is None, guard
    assert set(gz.declared) == set(G.SECTIONS)


# ---------------------------------------------------------------------------
# Grounding: an unresolvable scope is refused by name
# ---------------------------------------------------------------------------


def test_grounding_refuses_an_unresolvable_scope_by_name():
    from legba.runtime.grounding_scope import (
        REASON_UNRESOLVABLE,
        SCOPE_COUNTRY,
        SCOPE_GLOBAL,
        SCOPE_NONE,
        SOURCE_DESCRIPTOR_GEO,
        DeclaredScope,
        classify_world_context_scope,
        corpus_filter_values,
        is_scoped_target,
    )

    # a non-geo instance's target whose head declares no geo and no global kind
    none = classify_world_context_scope(
        "acme_supplier_desk", None, None, DeclaredScope(tags=("corporate",)),
    )
    assert (none.scope, none.reason, none.retrieves) == (SCOPE_NONE, REASON_UNRESOLVABLE, False)
    # a target whose head declares ONE code is scoped by its declared scope
    one = classify_world_context_scope(
        "acme_supplier_desk", None, None, DeclaredScope(tags=("corporate",), geo=("de",)),
    )
    assert (one.scope, one.source, one.country_values) == (
        SCOPE_COUNTRY, SOURCE_DESCRIPTOR_GEO, ("de", "DE"),
    )
    assert is_scoped_target("acme_supplier_desk", None, DeclaredScope(tags=(), geo=("de",)))
    # several codes are not one subject; a declared global kind still wins
    assert corpus_filter_values(DeclaredScope(tags=(), geo=("de", "fr"))) is None
    region = classify_world_context_scope(
        "region_europe", None, None, DeclaredScope(tags=("region",), geo=("de",)),
    )
    assert region.scope == SCOPE_GLOBAL and region.reason == "region"
    # a scope with no tags key keeps the id-family fallback
    lane = classify_world_context_scope("lane_x", None, None, DeclaredScope(tags=None))
    assert lane.scope == SCOPE_GLOBAL and lane.source == "id_prefix"
    assert not is_scoped_target(None, ["us"], None)


class _Point:
    def __init__(self, id: str) -> None:
        self.id = id
        self.payload = {"corpus": "world_context", "title": id, "text": "a prior",
                        "countries": ["de", "DE"]}
        self.score = 0.8


class _Qdrant:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def query_points(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        class _R:
            points = [_Point("de-1")]

        return _R()


class _Embedder:
    dim = 4

    async def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.1, 0.2, 0.3, 0.4]


class _Pool:
    def __init__(self, heads: dict[str, Any]) -> None:
        self.heads = heads
        self.sql: list[str] = []

    def acquire(self) -> Any:
        pool = self

        class _A:
            async def __aenter__(self) -> Any:
                class _C:
                    async def fetchval(self, sql: str, tid: str) -> Any:
                        pool.sql.append(sql)
                        return pool.heads.get(tid)

                return _C()

            async def __aexit__(self, *exc: Any) -> None:
                return None

        return _A()


async def test_a_declared_single_code_scopes_retrieval_and_an_unbuildable_filter_refuses(monkeypatch):
    from legba.runtime.grounding import SubstrateGroundingResolver
    from legba.runtime.grounding_scope import REASON_FILTER_UNBUILDABLE, SOURCE_DESCRIPTOR_GEO

    pool = _Pool({"acme_de_desk": json.dumps({"domain": "entity", "geo": ["DE"], "tags": ["supplier"]})})
    qdrant = _Qdrant()
    resolver = SubstrateGroundingResolver(pg_pool=pool, embedder=_Embedder(), qdrant=qdrant)
    chunks = await resolver.resolve_world_context("q", limit=5, target_id="acme_de_desk")
    assert chunks.scope.source == SOURCE_DESCRIPTOR_GEO
    assert list(qdrant.calls[-1]["query_filter"].must[0].match.any) == ["de", "DE"]
    assert "body->'scope'" in pool.sql[-1]

    # the filter cannot be built -> refused by name, nothing searched
    monkeypatch.setattr(
        SubstrateGroundingResolver, "_world_context_country_filter",
        staticmethod(lambda values: None),
    )
    n = len(qdrant.calls)
    refused = await resolver.resolve_world_context("q", limit=5, target_id="country_g20_fr")
    assert refused == [] and refused.reason == REASON_FILTER_UNBUILDABLE
    assert not refused.scope.retrieves
    assert len(qdrant.calls) == n


# ---------------------------------------------------------------------------
# The sanctions skeleton, the export, the closed model
# ---------------------------------------------------------------------------


def test_the_sanctions_gazetteer_loads_and_declares_only_what_it_can(restore_gazetteer):
    profile = load_profile(SANCTIONS)
    assert profile.gazetteer_path == (SANCTIONS / "rosters" / "gazetteer.yaml").resolve()
    gz = G.gazetteer_of(profile)
    assert gz.profile == "sanctions_export_control"
    assert gz.declared == ("polities", "alias_map", "alias_org")
    assert gz.get("polities") == G.PolityUniverse("pycountry", frozenset())
    assert set(gz.get("alias_map").values()) == {
        "United States", "United Kingdom", "European Union", "United Nations",
    }
    for guard in G.GUARD_SECTIONS:
        assert G.applicable(guard, gz, record=False) is not None, guard
    # and it exports deterministically
    assert json.loads(G.export_gazetteer(profile))["gazetteer"]["alias_org"] == [
        "European Union", "United Nations",
    ]


def test_a_profile_with_no_gazetteer_reads_every_section_absent(tmp_path):
    import yaml

    data = yaml.safe_load((SANCTIONS / "profile.yaml").read_text(encoding="utf-8"))
    data["rosters"].pop("gazetteer")
    d = tmp_path / "sanctions_export_control"
    (d / "descriptors").mkdir(parents=True)
    (d / "profile.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    gz = G.gazetteer_of(load_profile(d))
    assert gz.declared == () and gz.origin.endswith("(no gazetteer)")
    assert json.loads(G.export_gazetteer(load_profile(d)))["gazetteer"] is None


def test_the_packaged_export_is_the_default_profiles():
    assert gen_profile_gazetteer.OUT == G.PACKAGED_EXPORT
    assert G.PACKAGED_EXPORT.read_text(encoding="utf-8") == gen_profile_gazetteer.render(None), (
        "src/legba/data/profile_gazetteer.json is stale — run scripts/gen_profile_gazetteer.py"
    )
    assert load_profile(GEO).gazetteer_path == (GEO / "rosters" / "gazetteer.yaml").resolve()


def test_legba_profile_selects_the_profiles_gazetteer(monkeypatch, restore_gazetteer):
    monkeypatch.setenv("LEGBA_PROFILE", str(SANCTIONS))
    assert G.refresh().profile == "sanctions_export_control"
    monkeypatch.delenv("LEGBA_PROFILE")
    gz = G.refresh()
    assert gz.profile == "geopolitics" and gz.origin.startswith("packaged:")


@pytest.mark.parametrize(
    "mutate, err",
    [
        (lambda d: d.update(no_such_section=[]), "no_such_section"),
        (lambda d: d.update(schema="legba/gazetteer/9"), "schema"),
        (lambda d: d["classifier_patterns"].update(machine_structured_title="(unclosed"),
         "does not compile"),
        (lambda d: d["officeholder_acronyms"].update(XX="atlantis"), "first spelling"),
        (lambda d: d["functional_role_country_side"].update(**{"leader of": "both"}), "subject"),
        (lambda d: d["polities"].update(universe="geonames"), "pycountry"),
    ],
)
def test_the_file_model_is_closed(tmp_path, mutate, err):
    import yaml

    data = yaml.safe_load((GEO / "rosters" / "gazetteer.yaml").read_text(encoding="utf-8"))
    mutate(data)
    p = tmp_path / "gazetteer.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValidationError, match=err):
        G.load_gazetteer_file(p)
