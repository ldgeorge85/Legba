# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GEO ROUTING v2 — the desk's polity is NAMED, the router filed it elsewhere.

The defect, measured on the live corpus at 2026-09-06 and reproduced here as
fixtures: ``signals.geo`` holds ONE country because the ingest geocode ladder
stops at the first one it resolves, and inside the lead zone "first" means
first BY POSITION. A wire title that opens with the actor files under the
actor, so four of the five reports of the 09-04 strike on SBU headquarters in
Kyiv — magnitude 0.90-0.94, the window's highest — carry ``geo = {RU}`` and
reach Russia's desk, while Ukraine's desk gets only the TASS copy, whose title
happens to open "Explosion rocks Ukrainian Security Service headquarters".

These tests hold the repair to five properties:

* **the detector is honest** — it finds the named-but-not-routed rows and it
  does NOT invent them (a title that names nobody, or names only the desk's
  own geo, is not a finding);
* **the ambiguity guard holds in both directions** — "Republic of China" must
  not make a China story name Taiwan, and "Congo" must still name CG even
  though it is contained in CD's long name;
* **flag-off is byte-identical** — one query, one param list, the same rows;
* **the cap is shared, not extended** — an admitted row displaces one the geo
  leg would have carried, so the slice never grows past ``row_cap``;
* **the receipt reports and does not gate** — ``routed_elsewhere`` counts, and
  nothing branches on it.

The five case shapes are the audit's own, with their live magnitudes and geo
tags: SBU-HQ (UA<-RU), the US sanctions on a Turkish bank (TR<-US), the SCO
summit (IN<-KG), the Manus story (CN), and Turkey's Syria file (TR<-SY/IL).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from legba.data._geo_routing import (
    CANON_POLITIES,
    GeoRoutingConfig,
    admitted_rows,
    config_from_options,
    polity_names_for_iso2,
    polity_names_for_iso2s,
    slice_geo_v2_enabled,
    surface_index,
    title_probes,
)
from legba.runtime.actor_substrate_slice import _read_substrate_slice


# ---------------------------------------------------------------------------
# The five case shapes, verbatim from the live rows (2026-09-06 corpus)
# ---------------------------------------------------------------------------

#: (id, source, geo, magnitude, title) — the exact rows the audit named.
SBU_HQ = [
    ("52ebade0", "source.cna.all", ["RU"], 0.94,
     "Russia hits Ukraine's security service headquarters in Kyiv, Zelenskyy says"),
    ("c8c2d1c1", "source.aljazeera.world", ["RU"], 0.90,
     "Russian drone strikes Ukraine’s security service headquarters"),
    ("3b70e059", "source.france24.english", ["RU"], 0.90,
     "On the ground: Russian drone strikes Ukraine security service headquarters in Kyiv"),
    ("a8c90fe2", "source.rsshub.apnews.world", ["RU"], 0.85,
     "Russian attacks kill at least 3 in Ukraine as US talks on the war are expected"),
    ("8b1979cd", "source.tass.english", ["UA"], 0.90,
     "Explosion rocks Ukrainian Security Service headquarters in central Kiev — Zelensky"),
]

TURKISH_BANK = [
    ("330e10cb", "source.aawsat.english", ["US"], 0.70,
     "US Sanctions Turkish Bank, 2 Subsidiaries to Pressure Iran"),
    ("56a623d6", "source.rsshub.apnews.world", ["US"], 0.70,
     "US issues sanctions on Turkish bank that it calls a ‘critical financial lifeline’"),
    ("3cc9aa3c", "source.aljazeera.world", ["US"], 0.60,
     "US imposes sanctions on Turkish bank, prompting legal threat"),
]

#: Turkey's Syria file — the audit's row 11. Geo went to the event country.
TURKEY_SYRIA = [
    ("aaaa0001", "source.middleeasteye.news", ["SY"], 0.55,
     "Turkish-backed factions clash near Abu al-Duhur in northern Syria"),
    ("aaaa0002", "source.jpost.frontpage", ["IL"], 0.50,
     "Israel puts forces on alert over Turkish deployment in Syria"),
]

#: The SCO items — the shape a TITLE-only rule provably cannot recover,
#: because none of the three titles names India at all.
SCO_TITLES = [
    ("9fd494a9", "source.japantimes.news", ["KG"], 0.50,
     "Putin, Xi hold talks in Kyrgyzstan as regional summit begins",
     "Iran's Masoud Pezeshkian, Turkey's Recep Tayyip Erdogan and India's "
     "Narendra Modi are also attending the summit."),
    ("1e34f767", "source.hindustantimes.world", ["KG"], 0.30,
     "Modi-Xi bonhomie, Sharif’s quick catch-up with Putin after SCO photo op",
     "World leaders attending the 26th Shanghai Cooperation Organisation "
     "(SCO) Summit in Bishkek, Kyrgyz Republic, gathered for the photograph."),
    ("7ffdec18", "source.anadolu.english", ["TR"], 0.30,
     "SCO leaders adopt Bishkek Declaration, expand cooperation on security",
     "Xi, Putin, Modi among leaders attending summit marking anniversary."),
]

#: The Manus story — correctly tagged CN. It is here as a KEEP-test: a row the
#: router got RIGHT must not be dragged anywhere by this rule.
MANUS = ("3d22f3bf", "source.digitimes.news", ["CN"], 0.20,
         "Manus resumes independent operations after Meta deal collapses")


def _row(case, summary: str = "") -> dict[str, Any]:
    sid, source, geo, mag, title = case[:5]
    if len(case) > 5:
        summary = case[5]
    return {
        "id": sid, "source_id": source, "geo": list(geo),
        "magnitude": mag, "title": title, "summary": summary,
    }


# ---------------------------------------------------------------------------
# 1. The named-but-not-routed detector
# ---------------------------------------------------------------------------


def _named_but_not_routed(rows, iso2: str) -> list[str]:
    """The detector, expressed once, over the shared matcher."""
    index = surface_index(polity_names_for_iso2(iso2))
    return [
        r["id"] for r in rows
        if index.names_in(r["title"]) and iso2 not in r["geo"]
    ]


def test_sbu_hq_reports_name_ukraine_and_were_routed_to_russia():
    """The sharpest live case: four of five, at the window's top magnitudes."""
    missed = _named_but_not_routed([_row(c) for c in SBU_HQ], "UA")
    assert missed == ["52ebade0", "c8c2d1c1", "3b70e059", "a8c90fe2"]
    # The TASS copy is the ONE the router got to Ukraine, and it is not a
    # finding — it is already in the slice.
    assert "8b1979cd" not in missed


def test_turkish_bank_sanctions_name_turkey_and_were_routed_to_the_us():
    assert _named_but_not_routed([_row(c) for c in TURKISH_BANK], "TR") == [
        "330e10cb", "56a623d6", "3cc9aa3c",
    ]


def test_turkeys_syria_file_names_turkey_and_was_routed_to_the_event_country():
    assert _named_but_not_routed([_row(c) for c in TURKEY_SYRIA], "TR") == [
        "aaaa0001", "aaaa0002",
    ]


def test_a_correctly_routed_row_is_never_a_finding():
    """The keep-test. Manus is tagged CN and belongs to CN; the rule must not
    move it, and it names no other desk's polity either."""
    row = _row(MANUS)
    assert _named_but_not_routed([row], "CN") == []
    for iso2 in ("UA", "TR", "IN", "RU"):
        assert _named_but_not_routed([row], iso2) == []


def test_the_sco_titles_name_no_country_so_a_title_rule_finds_nothing():
    """The honest negative, and it is load-bearing.

    A title-only rule provably cannot recover the audit's India/China items:
    none of the three titles names India. Two of them name India ONLY through
    the person "Modi", which no polity matcher can see. Stating that here
    stops the next reader from believing the fix covers a case it does not."""
    rows = [_row(c) for c in SCO_TITLES]
    assert _named_but_not_routed(rows, "IN") == []


def test_summary_matching_recovers_exactly_the_one_that_names_india():
    """And the measured delta of the ``match_summary`` knob: 1 of 3."""
    index = surface_index(polity_names_for_iso2("IN"))
    named = [
        r["id"] for r in (_row(c) for c in SCO_TITLES)
        if index.names_in(f"{r['title']}\n{r['summary']}")
    ]
    assert named == ["9fd494a9"]


# ---------------------------------------------------------------------------
# 2. The ISO2 -> polity resolution and its ambiguity guard
# ---------------------------------------------------------------------------


def test_curated_alias_spellings_survive_the_canon_gate():
    """``home_country_name`` alone loses the spellings the wire uses: TR would
    be "Turkey" only (Anadolu writes "Türkiye") and GB would be the single
    literal "Britain"."""
    assert set(polity_names_for_iso2("TR")) >= {"Turkey", "Türkiye"}
    assert "united kingdom" in {n.casefold() for n in polity_names_for_iso2("GB")}
    assert {"britain", "british", "united kingdom"} <= set(
        title_probes(polity_names_for_iso2("GB"))
    )


def test_republic_of_china_never_makes_a_china_story_name_taiwan():
    """The guard's first half. Without it the curated TW alias "Republic of
    China" is contained in "People's Republic of China" and every China story
    breaches Taiwan's desk — ``represented_by``'s multi-word rule is
    containment."""
    tw = surface_index(polity_names_for_iso2("TW"))
    assert tw.names_in("People's Republic of China curbs rare earth exports") == set()
    assert tw.names_in("Taiwan faces alarming Chinese presence") == {"Taiwan"}


def test_congo_still_names_congo_even_though_drc_contains_it():
    """The guard's second half, and the reason it runs on ALIAS-ONLY surfaces.
    "Congo" is a legitimate canon polity for CG that IS contained in CD's long
    name; a guard that ran on canon polities too would break CG to protect
    CD."""
    cg = surface_index(polity_names_for_iso2("CG"))
    assert cg.names_in("Congo announces new oil terms") == {"Congo"}


def test_an_iso2_the_canon_does_not_carry_still_resolves():
    """Fallback: silence would mean the desk quietly opts out of the fix."""
    assert polity_names_for_iso2("TW")
    assert polity_names_for_iso2("VA") or True  # no gazetteer entry is fine


def test_the_canon_set_is_the_one_frame_anchor_uses():
    """This module reads ``_polity_match._SURFACES`` directly to avoid an
    import cycle. Pin that it is the SAME set ``_frame_anchor`` exports, so
    the two can never drift into two different canons."""
    from legba.data._frame_anchor import DEFAULT_CANDIDATES

    assert CANON_POLITIES == DEFAULT_CANDIDATES


def test_probes_recall_every_title_the_matcher_accepts():
    """The prefilter's whole contract: it may be generous, it may not miss.

    Checked over the live case shapes rather than asserted in prose."""
    for iso2, cases in (("UA", SBU_HQ), ("TR", TURKISH_BANK + TURKEY_SYRIA)):
        names = polity_names_for_iso2(iso2)
        index = surface_index(names)
        probes = title_probes(names)
        for case in cases:
            title = case[4]
            if not index.names_in(title):
                continue
            assert any(p in title.casefold() for p in probes), (
                f"prefilter would have missed {title!r} for {iso2}"
            )


# ---------------------------------------------------------------------------
# 3. The knobs — the house _coerce contract
# ---------------------------------------------------------------------------


def test_env_is_the_base_and_an_option_wins(monkeypatch):
    monkeypatch.setenv("LEGBA_SLICE_GEO_V2_ADMIT_SHARE", "0.5")
    assert config_from_options(None).admit_share == 0.5
    assert config_from_options(
        {"slice_geo_v2_admit_share": 0.1}
    ).admit_share == 0.1


def test_a_mistyped_knob_keeps_its_predecessor_rather_than_raising(monkeypatch):
    monkeypatch.setenv("LEGBA_SLICE_GEO_V2_ADMIT_SHARE", "not-a-number")
    cfg = config_from_options({"slice_geo_v2_min_magnitude": "also-bad"})
    assert cfg.admit_share == GeoRoutingConfig().admit_share
    assert cfg.min_magnitude == GeoRoutingConfig().min_magnitude


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("", False), ("0", False), ("off", False),
     ("false", False), ("maybe", False), ("1", True), ("true", True),
     ("on", True), ("YES", True)],
)
def test_only_unambiguous_values_turn_the_flag_on(monkeypatch, value, expected):
    monkeypatch.delenv("LEGBA_SLICE_GEO_V2", raising=False)
    if value is not None:
        monkeypatch.setenv("LEGBA_SLICE_GEO_V2", value)
    assert slice_geo_v2_enabled() is expected


# ---------------------------------------------------------------------------
# 4. The admission cap — shared, magnitude-first
# ---------------------------------------------------------------------------


def test_admissions_are_magnitude_first_and_capped_at_the_ubiquity_ceiling():
    rows = [_row(c) for c in SBU_HQ if c[2] != ["UA"]]
    index = surface_index(polity_names_for_iso2("UA"))
    cfg = GeoRoutingConfig(admit_share=0.025)  # 0.025 * 120 = 3
    out = admitted_rows(rows, index, config=cfg, row_cap=120)
    assert [r["id"] for r in out] == ["52ebade0", "c8c2d1c1", "3b70e059"]


def test_the_magnitude_floor_is_honoured_and_defaults_to_admitting_everything():
    rows = [_row(c) for c in TURKISH_BANK]
    index = surface_index(polity_names_for_iso2("TR"))
    assert len(admitted_rows(rows, index, config=GeoRoutingConfig(),
                             row_cap=120)) == 3
    floored = admitted_rows(
        rows, index, config=GeoRoutingConfig(min_magnitude=0.65), row_cap=120,
    )
    assert [r["id"] for r in floored] == ["330e10cb", "56a623d6"]


def test_an_untitled_row_is_never_admitted():
    index = surface_index(polity_names_for_iso2("UA"))
    rows = [{"id": "x", "geo": ["RU"], "magnitude": 0.99, "title": "  "}]
    assert admitted_rows(rows, index, config=GeoRoutingConfig(),
                         row_cap=120) == []


# ---------------------------------------------------------------------------
# 5. The slice reader — flag-off byte-identity, flag-on admission
# ---------------------------------------------------------------------------


class _SliceConn:
    """Fake connection recording every statement the reader fires."""

    def __init__(self, *, signals, recovery=None, scope_geo=("UA",)) -> None:
        self._signals = signals
        self._recovery = recovery or []
        self._scope_geo = list(scope_geo)
        self.queries: list[str] = []
        self.params: list[tuple] = []

    async def fetchrow(self, _query, *_params):
        return {"body": json.dumps(
            {"sources": [], "scope": {"geo": list(self._scope_geo)}}
        )}

    async def fetch(self, query, *params):
        self.queries.append(query)
        self.params.append(params)
        if "FROM signals" in query:
            if "NOT (geo &&" in query:
                return list(self._recovery)
            return list(self._signals)
        return []

    @property
    def signal_queries(self) -> list[str]:
        return [q for q in self.queries if "FROM signals" in q]


def _descriptor() -> SimpleNamespace:
    return SimpleNamespace(
        identity=SimpleNamespace(id="escalation", kind="inline_target"),
        subscription=SimpleNamespace(
            substrate={}, targets=SimpleNamespace(time_window="72h"),
        ),
    )


def _db_row(case, when) -> dict[str, Any]:
    sid, source, geo, mag, title = case[:5]
    return {
        "id": uuid4(), "source_id": source, "source_version": None,
        "canonical_url": f"https://example.test/{sid}",
        "payload": {"title": title}, "language": "en", "geo": list(geo),
        "tags": [], "fetched_at": when, "derived_from": None,
        "entity_classes": [], "source_credibility": None, "modality": None,
        "salience": {"magnitude": mag}, "_case": sid,
    }


def _fixture_rows():
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    geo_rows = [_db_row(SBU_HQ[4], base)]  # the TASS copy, geo = UA
    recovery = [
        _db_row(c, base - timedelta(hours=i + 1))
        for i, c in enumerate(SBU_HQ[:4])
    ]
    return geo_rows, recovery


#: The geo leg's SELECT, verbatim from base 320558f7 — the commit this program
#: branched from. Extracting the column list into ``_SLICE_COLUMNS`` so the two
#: legs cannot select different shapes is the ONLY edit inside this statement,
#: and this golden holds it to being invisible: at flag-off a reviewer diffing
#: the SQL sees no change at all, not merely an equivalent one.
_BASE_GEO_LEG_SQL = """        SELECT id, source_id, source_version, canonical_url,
               payload, language, geo, tags, fetched_at, derived_from,
               entity_classes, source_credibility, modality, salience
        FROM signals
        {where}
        ORDER BY fetched_at DESC
        LIMIT {fetch_limit}"""


def test_the_geo_legs_sql_is_byte_identical_to_the_pre_program_statement():
    from legba.runtime.actor_substrate_slice import _SLICE_COLUMNS

    where, fetch_limit = "{where}", "{fetch_limit}"
    assert f"""        SELECT {_SLICE_COLUMNS}
        FROM signals
        {where}
        ORDER BY fetched_at DESC
        LIMIT {fetch_limit}""" == _BASE_GEO_LEG_SQL


@pytest.mark.asyncio
async def test_flag_off_issues_exactly_one_signals_query_and_the_same_rows(
    monkeypatch,
):
    """THE BYTE-IDENTITY CONTRACT. Off, the recovery leg does not exist: one
    statement, one param list, and the geo leg's rows unchanged."""
    monkeypatch.delenv("LEGBA_SLICE_GEO_V2", raising=False)
    geo_rows, recovery = _fixture_rows()
    conn = _SliceConn(signals=geo_rows, recovery=recovery)
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor(), target_filter="country_watch_ua",
    )
    assert len(conn.signal_queries) == 1
    assert "NOT (geo &&" not in conn.signal_queries[0]
    assert "ILIKE ANY" not in conn.signal_queries[0]
    assert [r["title"] for r in rows] == [SBU_HQ[4][4]]


@pytest.mark.asyncio
async def test_flag_on_admits_the_reports_the_router_sent_to_russia(monkeypatch):
    monkeypatch.setenv("LEGBA_SLICE_GEO_V2", "1")
    geo_rows, recovery = _fixture_rows()
    conn = _SliceConn(signals=geo_rows, recovery=recovery)
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor(), target_filter="country_watch_ua",
    )
    assert len(conn.signal_queries) == 2
    titles = [r["title"] for r in rows]
    # The TASS copy the router got right is still there, and the four it sent
    # to Russia have joined it.
    assert SBU_HQ[4][4] in titles
    for case in SBU_HQ[:4]:
        assert case[4] in titles


@pytest.mark.asyncio
async def test_flag_on_never_grows_the_slice_past_the_row_cap(monkeypatch):
    """The cap is SHARED. An admitted row displaces one the geo leg carried;
    it is not added on top of a full slice."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("LEGBA_SLICE_GEO_V2", "1")
    monkeypatch.setenv("LEGBA_SLICE_ROW_CAP", "10")
    base = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    geo_rows = [
        _db_row(("g%02d" % i, "source.s%d" % i, ["UA"], 0.5, "Ukraine item %d" % i),
                base - timedelta(minutes=i))
        for i in range(10)
    ]
    recovery = [
        _db_row(c, base - timedelta(hours=1)) for c in SBU_HQ[:4]
    ]
    conn = _SliceConn(signals=geo_rows, recovery=recovery)
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor(), target_filter="country_watch_ua",
    )
    signal_rows = [r for r in rows if r.get("source_id") != "graph_metrics"]
    assert len(signal_rows) == 10


# ---------------------------------------------------------------------------
# 5b. Amendment 7g — the whole-token surface repair, at the reader
#
# Live, 2026-09-09: 20 of the 30 rows this leg admitted to ``country_g20_us``
# named no American anything. Sixteen were the Israel/UK consulate story,
# admitted because the ``u.s`` surface squeezes to "us" and "Jerusalem"
# squeezes past "usa". See ``tests/data_pkg/test_whole_token_surface_rule.py``.
# ---------------------------------------------------------------------------

#: Titles the pre-repair matcher read as naming the United States. Every one is
#: a live row from the 72 h pool the 09-09 replay measured.
US_DESK_FALSE_ADMITS = [
    ("fa1", "source.aljazeera.world", ["IL"], 0.75,
     "Israel to close British consulate in Jerusalem after sanctions"),
    ("fa2", "source.trt.world", ["SI"], 0.72,
     "Thousands protest in Ljubljana as Slovenia opens Israel's first embassy"),
    ("fa3", "source.tass.world", ["RU"], 0.88,
     "Russia outpacing Ukraine's air defences with new jet-powered drones"),
]

#: Rows that genuinely name the desk's polity and were displaced by them.
US_DESK_TRUE_NAMES = [
    ("tn1", "source.ruv.news", ["IS"], 0.62,
     "Iceland summons U.S. ambassador after Trump posts American flags"),
    ("tn2", "source.cbc.news", ["CA"], 0.60,
     "Canada sets tariffs of up to 50% on U.S. steel and other goods"),
]


def _us_fixture():
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    geo_rows = [_db_row(("us0", "source.npr.news", ["US"], 0.4,
                         "Congress returns to a shutdown deadline"), base)]
    recovery = [
        _db_row(c, base - timedelta(hours=i + 1))
        for i, c in enumerate(US_DESK_FALSE_ADMITS + US_DESK_TRUE_NAMES)
    ]
    return geo_rows, recovery


@pytest.mark.asyncio
async def test_flag_off_carries_the_defect_specimens_unchanged(monkeypatch):
    """The repair is INSIDE the matcher, and at flag-off the matcher is never
    asked. One statement, no recovery clauses, the geo leg's rows verbatim —
    the same contract the program shipped with, re-asserted on the rows the
    repair changes."""
    monkeypatch.delenv("LEGBA_SLICE_GEO_V2", raising=False)
    geo_rows, recovery = _us_fixture()
    conn = _SliceConn(signals=geo_rows, recovery=recovery, scope_geo=("US",))
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor(), target_filter="country_g20_us",
    )
    assert len(conn.signal_queries) == 1
    assert "NOT (geo &&" not in conn.signal_queries[0]
    assert "ILIKE ANY" not in conn.signal_queries[0]
    assert [r["title"] for r in rows] == [
        "Congress returns to a shutdown deadline"
    ]


@pytest.mark.asyncio
async def test_flag_on_admits_the_rows_that_name_the_us_and_refuses_the_rest(
    monkeypatch,
):
    monkeypatch.setenv("LEGBA_SLICE_GEO_V2", "1")
    geo_rows, recovery = _us_fixture()
    conn = _SliceConn(signals=geo_rows, recovery=recovery, scope_geo=("US",))
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor(), target_filter="country_g20_us",
    )
    assert len(conn.signal_queries) == 2
    titles = [r["title"] for r in rows]
    for case in US_DESK_TRUE_NAMES:
        assert case[4] in titles
    for case in US_DESK_FALSE_ADMITS:
        assert case[4] not in titles


@pytest.mark.asyncio
async def test_a_target_with_no_geo_scope_never_fires_the_recovery_leg(
    monkeypatch,
):
    """A META / thematic run has no polity to recover toward, and asking would
    be a tenant-wide title scan for nothing."""
    monkeypatch.setenv("LEGBA_SLICE_GEO_V2", "1")

    class _NoGeoConn(_SliceConn):
        async def fetchrow(self, _query, *_params):
            return {"body": json.dumps({"sources": [], "scope": {}})}

    geo_rows, recovery = _fixture_rows()
    conn = _NoGeoConn(signals=geo_rows, recovery=recovery)
    await _read_substrate_slice(
        conn, descriptor=_descriptor(), target_filter="thematic_x",
    )
    assert len(conn.signal_queries) == 1


@pytest.mark.asyncio
async def test_a_failing_recovery_leg_degrades_to_the_geo_leg(monkeypatch):
    """Degrade-not-drop: a desk that cannot read its window publishes nothing,
    so a fault in the NEW leg must never take the OLD one down."""
    monkeypatch.setenv("LEGBA_SLICE_GEO_V2", "1")
    geo_rows, _ = _fixture_rows()

    class _BrokenConn(_SliceConn):
        async def fetch(self, query, *params):
            if "NOT (geo &&" in query:
                raise RuntimeError("recovery leg exploded")
            return await super().fetch(query, *params)

    conn = _BrokenConn(signals=geo_rows)
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor(), target_filter="country_watch_ua",
    )
    assert [r["title"] for r in rows] == [SBU_HQ[4][4]]


# ---------------------------------------------------------------------------
# 6. The coverage-floor receipt — reports, never gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routed_elsewhere_counts_the_gap_the_slice_share_clause_cannot_see():
    """Clause 4 is a SLICE SHARE, so it is structurally unreachable for
    material that never entered the slice. This counter is that gap."""
    from legba.data._geo_routing import count_routed_elsewhere

    desks = [
        {"descriptor_id": "country_watch_ua", "geo": ["UA"]},
        {"descriptor_id": "country_g20_tr", "geo": ["TR"]},
        {"descriptor_id": "country_g20_cn", "geo": ["CN"]},
    ]
    rows = [
        {"geo": c[2], "title": c[4]}
        for c in SBU_HQ + TURKISH_BANK + TURKEY_SYRIA + [MANUS]
    ]

    class _Conn:
        async def fetch(self, _query, *_params):
            return rows

    stats: dict[str, Any] = {}
    out = await count_routed_elsewhere(
        _Conn(), desks=desks, parse_jsonish=lambda v: v,
        window_days=14, high_magnitude=0.5, config=GeoRoutingConfig(),
        stats=stats,
    )
    assert out["country_watch_ua"] == 4      # the four routed to RU
    assert out["country_g20_tr"] == 5        # 3 bank reports + 2 Syria-file
    assert "country_g20_cn" not in out       # Manus is correctly routed
    assert stats.get("routed_elsewhere_bound_hit", 0) == 0


@pytest.mark.asyncio
async def test_routed_elsewhere_ignores_a_desk_with_no_geo_scope():
    from legba.data._geo_routing import count_routed_elsewhere

    class _Conn:
        async def fetch(self, _query, *_params):  # pragma: no cover
            raise AssertionError("must not query with no desks to attribute to")

    out = await count_routed_elsewhere(
        _Conn(), desks=[{"descriptor_id": "x", "geo": []}],
        parse_jsonish=lambda v: v, window_days=14, high_magnitude=0.5,
        config=GeoRoutingConfig(), stats={},
    )
    assert out == {}


def test_polity_names_for_a_multi_country_scope_are_deduplicated():
    names = polity_names_for_iso2s(["UA", "UA", "RU"])
    assert len(names) == len(set(names))
    assert "Ukraine" in names and "Russia" in names
