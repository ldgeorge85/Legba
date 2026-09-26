# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE WHOLE-TOKEN SURFACE RULE — Amendment 7g, the squeezed-substring repair.

WHAT WENT WRONG. ``represented_by`` matched a MULTI-WORD surface two ways, and
both were bare containment over a token stream: the phrase itself
(``"u s" in prose``) and the phrase with its spaces squeezed out
(``"us" in prose.replace(" ", "")``). Containment cannot tell a name from the
middle of a longer word. ``United States`` carries the curated surface ``u.s``,
so on the live 72 h pool the matcher read the United States into *russia*,
*australia*, *focus*, *thousands* and — the specimen that cost the most —
*Jerusalem*, which squeezes past ``usa`` (``jer-USA-lem``). ``United Kingdom``
carries ``u.k`` and read Britain into *Ukraine* and *rebuked*.

WHAT IT COST, measured read-only on 2026-09-09 and recorded in
``planning/GEO_POLITY_INDEX_WHOLE_TOKEN_REPORT.md``:

* the index put ``United States`` on 2,641 of 10,507 signal titles (25.1%) and
  ``United Kingdom`` on 663 (6.3%); after the repair, 996 and 359;
* Amendment 6's recovery leg spent **20 of the ``country_g20_us`` desk's 30
  admission slots** on rows naming no American anything — sixteen of them the
  Israel/UK consulate story, admitted because "Jerusalem" contains "usa";
* the ubiquity clause D-t (``max_anchor_ubiquity = 0.75``) read
  ``United States`` at 84.8–100% of every desk's own findings and therefore
  REFUSED it as wire boilerplate on 38 desks. Whole-token it breaches on three
  — ``region_americas`` and ``region_mena`` (region roll-ups name every member
  by design) and ``country_watch_ir`` at 0.828 — while R1-a's five replay desks
  plus GB and AU run 2.3–49.4%. Suppression and pollution were the same bug;
* the home-country exclusion dropped ``United States`` from the AU and RU
  desks' candidate sets (``Australia`` and ``Russia`` contain "us") and
  ``United Kingdom`` from UA's (``Ukraine`` contains "uk").

THE REPAIR. Both multi-word legs respect token boundaries: the phrase leg is
now the SAME ``\\b<surface>s?\\b`` probe the single-word leg has always used,
and the squeezed leg drops to a whole-token test for squeezed forms at or below
``SQUEEZE_TOKEN_MAX_CHARS``. It is a strict NARROWING — nothing newly matches —
and it costs no real naming, because every polity carrying a multi-word surface
also carries its demonym as a single-word one.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import pytest

from legba.data import _polity_match as pm
from legba.data._frame_anchor import (
    DEFAULT_CANDIDATES,
    FrameAnchorConfig,
    PolitySurfaceIndex,
    anchors_for,
    candidate_polities,
)
from legba.data._geo_routing import (
    GeoRoutingConfig,
    admitted_rows,
    polity_names_for_iso2,
    surface_index,
)
from legba.data._polity_match import (
    SQUEEZE_TOKEN_MAX_CHARS,
    entity_surfaces,
    home_prose,
    normalize_prose,
    represented_by,
)


# ---------------------------------------------------------------------------
# The PRE-REPAIR rule, reproduced verbatim from d4fe96b8. It is the reference
# the NARROWING claim is made against and must never be edited to pass a test.
# ---------------------------------------------------------------------------


def _pre_repair_represented_by(canonical_name: str, prose_text: str) -> str | None:
    prose = normalize_prose(prose_text)
    squashed = prose.replace(" ", "")
    for surface in entity_surfaces(canonical_name):
        norm = normalize_prose(surface)
        if not norm:
            continue
        if " " in norm:
            if norm in prose or norm.replace(" ", "") in squashed:
                return surface
        elif re.search(r"\b" + re.escape(norm) + r"s?\b", prose):
            return surface
    return None


#: Live titles the defect actually mis-attributed, taken from the 2026-09-09
#: read-only replay of the ``country_g20_us`` desk's recovery leg.
LIVE_FALSE_ADMITS = [
    "Israel to close British consulate in Jerusalem after sanctions announced",
    "Nauru opens Israel embassy in occupied Jerusalem in violation of "
    "international resolutions",
    "Fighting in Yemen kills, wounds nearly 900, as thousands left displaced",
    "Thousands protest in Ljubljana as Slovenia opens Israel's first embassy",
    "Russia outpacing Ukraine's air defences with daily use of new drones",
    "Australia: 25 Years of Abusive Offshore Detention",
    "Thousands of 'Greater Serbia' supporters attend funeral in Belgrade",
]

#: Live titles that genuinely name the United States and must survive.
LIVE_TRUE_NAMES = [
    "Iceland summons U.S. ambassador after Trump posts countries draped with "
    "American flags",
    "Iceland summons US ambassador over Trump map showing country under flag",
    "Canada sets tariffs of up to 50% on U.S. steel and other goods",
    "Iran warns U.S. energy assets in Gulf are vulnerable after clashes",
    "Russia Vows to Keep Selling Oil to India Despite U.S. Tariff Threat",
    "From the Shield of the Americas to the Golan Heights",
]


# ---------------------------------------------------------------------------
# 1. The squeezed leg
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word",
    ["russia", "australia", "focus", "thousands", "jerusalem", "because",
     "industry", "austria", "houses", "pushing"],
)
def test_the_squeezed_us_surface_no_longer_names_america_inside_a_word(word):
    """``u.s`` squeezes to "us", and "us" is inside all ten of these."""
    prose = f"A dispatch about {word} and nothing else"
    assert "us" in normalize_prose(prose).replace(" ", "")
    assert represented_by("United States", prose) is None


@pytest.mark.parametrize(
    "word", ["ukraine", "rebuked", "sukkot", "fukushima", "lukashenko"],
)
def test_the_squeezed_uk_surface_no_longer_names_britain_inside_a_word(word):
    """``u.k`` squeezes to "uk". *rebuked* is the live specimen: it minted a
    United Kingdom anchor on ``country_watch_il``'s internal_stability scope."""
    prose = f"A dispatch about {word} and nothing else"
    assert "uk" in normalize_prose(prose).replace(" ", "")
    assert represented_by("United Kingdom", prose) is None


@pytest.mark.parametrize(
    "prose,polity",
    [
        ("U.S. sanctions a Turkish bank", "United States"),
        ("U.S sanctions a Turkish bank", "United States"),
        ("US-led coalition strikes a convoy", "United States"),
        ("the US and Israel agree terms", "United States"),
        ("USA wins the vote", "United States"),
        ("U.S.A. delegation arrives", "United States"),
        ("The USS Gerald Ford transits the strait", "United States"),
        ("U.K. sanctions the settlements", "United Kingdom"),
        ("the UK summons the ambassador", "United Kingdom"),
        ("U.A.E. brokers the deal", "United Arab Emirates"),
        ("UAE brokers the deal", "United Arab Emirates"),
    ],
)
def test_every_spelling_a_newsroom_writes_still_names_its_polity(prose, polity):
    """The repair narrows the leg; it must not narrow it onto the wire."""
    assert represented_by(polity, prose) is not None


@pytest.mark.parametrize(
    "prose,polity",
    [
        ("SouthAfrica trends after the ruling", "South Africa"),
        ("NewZealand tightens the rules", "New Zealand"),
        ("SriLanka restructures the debt", "Sri Lanka"),
    ],
)
def test_the_squeezed_leg_still_reads_a_run_together_spelling(prose, polity):
    """The leg's REASON FOR EXISTING: a hashtag or headline that runs the words
    together into one token. Long squeezed forms keep containment."""
    assert represented_by(polity, prose) is not None


def test_a_long_surface_decides_exactly_as_it_did_before_the_repair():
    """THE BLAST RADIUS, proved rather than sampled.

    The pre-repair multi-word rule was ``phrase in prose OR squeezed in
    squashed``, and the left disjunct IMPLIES the right — a phrase present in a
    token stream is present, spaces removed, in the squeezed stream. So for
    every surface the rule was already just the squeezed test, and the repair
    changes nothing for any surface longer than ``SQUEEZE_TOKEN_MAX_CHARS``.
    Only ``us``, ``usa``, ``uk``, ``un`` and ``uae`` can move at all."""
    long_named = [
        n for n in DEFAULT_CANDIDATES
        for s in entity_surfaces(n)
        if " " in normalize_prose(s)
        and len(normalize_prose(s).replace(" ", "")) > SQUEEZE_TOKEN_MAX_CHARS
    ]
    short_named = {
        n for n in DEFAULT_CANDIDATES
        for s in entity_surfaces(n)
        if " " in normalize_prose(s)
        and len(normalize_prose(s).replace(" ", "")) <= SQUEEZE_TOKEN_MAX_CHARS
    }
    assert short_named == {
        "United States", "United Kingdom", "United Nations",
        "United Arab Emirates",
    }
    corpus = _corpus() + [
        "The People's Republic of China issued a statement",
        "SouthAfrica and NewZealand sign the pact",
        "Bosnia and Herzegovina votes",
        "Papua New Guinea declares an emergency",
    ]
    for polity in sorted(set(long_named) - short_named):
        for prose in corpus:
            assert (
                (represented_by(polity, prose) is not None)
                == (_pre_repair_represented_by(polity, prose) is not None)
            ), (polity, prose)


def test_the_short_squeezed_forms_are_exactly_the_ones_the_canon_carries():
    """The floor is a rule, not a stoplist — so pin what it actually catches,
    and notice in review if the canon ever grows another one."""
    short = sorted({
        normalize_prose(s).replace(" ", "")
        for name in DEFAULT_CANDIDATES
        for s in entity_surfaces(name)
        if " " in normalize_prose(s)
        and len(normalize_prose(s).replace(" ", "")) <= SQUEEZE_TOKEN_MAX_CHARS
    })
    assert short == ["uae", "uk", "un", "us", "usa"]


# ---------------------------------------------------------------------------
# 2. The phrase leg
# ---------------------------------------------------------------------------


def test_a_multi_word_surface_still_matches_across_a_hyphen():
    """The fold turns the hyphen into a space, so the phrase leg sees it."""
    assert represented_by("Israel", "Israel-Lebanon maritime deal holds")
    assert represented_by("Lebanon", "Israel-Lebanon maritime deal holds")
    assert represented_by(
        "United States", "The U.S.-led coalition met in Doha"
    ) is not None


@pytest.mark.parametrize(
    "prose,polity",
    [
        # "netanyahu says" carries "u s" straight across the token boundary.
        ("Netanyahu says he will sue the paper", "United States"),
        ("Switzerland joins EU sanctions on Sudan gold", "United States"),
        ("Anak Krakatau search expands for missing crew", "United States"),
        # "au kenya" and "peru kingdom" carry "u k".
        ("Au Kenya, l'inquietude des commercants", "United Kingdom"),
        ("600-year-old tomb of pre-Incan Chimu kingdom found in Peru",
         "United Kingdom"),
    ],
)
def test_a_multi_word_phrase_must_start_and_end_on_token_boundaries(prose, polity):
    """Every one of these matched before the repair and names nothing."""
    assert _pre_repair_represented_by(polity, prose) is not None
    assert represented_by(polity, prose) is None


@pytest.mark.parametrize(
    "prose,polity",
    [
        ("South Africans protest the ruling", "South Africa"),
        ("Sri Lankans queue for fuel", "Sri Lanka"),
        ("South Koreans back the deal", "South Korea"),
        ("New Zealanders vote today", "New Zealand"),
        ("Costa Ricans reject the plan", "Costa Rica"),
    ],
)
def test_the_demonym_carries_the_adjectival_form_the_phrase_leg_used_to(
    prose, polity,
):
    """WHY THE PHRASE LEG COULD BE BOUNDED AT ALL. Containment used to catch
    "South Africans" as a prefix of the phrase; the boundary probe's ``s?``
    does not reach "-ans". It costs nothing because the canon carries
    "south african" as its own SINGLE-WORD surface, which is the leg that was
    really doing the work."""
    assert represented_by(polity, prose) is not None


def test_republic_of_china_still_needs_the_alias_guard():
    """``_alias_surface_is_ambiguous`` exists because "Republic of China" (TW)
    sits at token boundaries inside "People's Republic of China" (CN). The
    repair does NOT dissolve that hazard, so the guard must stay."""
    assert represented_by(
        "Taiwan", "The People's Republic of China issued a statement"
    ) is None, "the guard must keep the TW alias out of polity_names_for_iso2"
    assert "Republic of China" not in polity_names_for_iso2("TW")


# ---------------------------------------------------------------------------
# 3. Equivalence, and the narrowing property
# ---------------------------------------------------------------------------


def _corpus() -> list[str]:
    return (
        LIVE_FALSE_ADMITS
        + LIVE_TRUE_NAMES
        + [
            "Israel-Lebanon maritime deal holds",
            "South Africans protest the ruling",
            "SouthAfrica trends after the ruling",
            "The U.K. summons the ambassador over settlements",
            "Ukraine's security service headquarters struck in Kyiv",
            "Israel's coalition splinters over the submarine deal",
            "Trump threatens to ban Canadian jets from the US market",
            "",
            "   ",
        ]
    )


def test_the_batched_index_agrees_with_represented_by_on_the_defect_corpus():
    """P-6, re-asserted on the rows the repair is ABOUT. The class and the
    function are two spellings of one rule and must stay one answer."""
    candidates = candidate_polities()
    index = PolitySurfaceIndex(candidates)
    for prose in _corpus():
        batched = index.names_in(prose)
        reference = {
            p for p in candidates if represented_by(p, prose) is not None
        }
        assert batched == reference, prose


def test_the_repair_is_a_strict_narrowing():
    """Nothing newly matches. Every verdict the repair changes is a match the
    pre-repair rule made and this one refuses — never the other way round."""
    gained = []
    for prose in _corpus():
        for polity in DEFAULT_CANDIDATES:
            if represented_by(polity, prose) is not None:
                if _pre_repair_represented_by(polity, prose) is None:
                    gained.append((polity, prose))
    assert gained == [], f"the repair WIDENED {len(gained)}: {gained[:5]}"


def test_the_repair_removes_exactly_the_specimens_it_was_built_for():
    for prose in LIVE_FALSE_ADMITS:
        assert _pre_repair_represented_by("United States", prose) is not None
        assert represented_by("United States", prose) is None
    for prose in LIVE_TRUE_NAMES:
        assert represented_by("United States", prose) is not None


# ---------------------------------------------------------------------------
# 4. The home-country exclusion — the other half of the same defect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "iso2,foreign",
    [("AU", "United States"), ("RU", "United States"),
     ("UA", "United Kingdom")],
)
def test_a_desk_no_longer_loses_a_foreign_polity_to_its_own_home_blob(
    iso2, foreign,
):
    """``Australia`` and ``Russia`` contain "us"; ``Ukraine`` contains "uk".
    Each of those desks could never anchor on that foreign polity, because
    ``candidate_polities`` had already dropped it as the desk's OWN country."""
    blob = home_prose([iso2])
    assert _pre_repair_represented_by(foreign, blob) is not None
    assert pm.is_home_country(foreign, blob) is False
    assert foreign in candidate_polities(home_blob=blob)


@pytest.mark.parametrize(
    "iso2,home",
    [("AU", "Australia"), ("RU", "Russia"), ("UA", "Ukraine"),
     ("GB", "United Kingdom"), ("US", "United States"), ("IR", "Iran")],
)
def test_a_desk_still_excludes_its_own_country(iso2, home):
    """The repair must not cost the exclusion the thing it is for."""
    blob = home_prose([iso2])
    assert pm.is_home_country(home, blob) is True
    assert home not in candidate_polities(home_blob=blob)


# ---------------------------------------------------------------------------
# 5. The ubiquity ceiling — D-t, and why it was firing
# ---------------------------------------------------------------------------


def _finding(fid: str, title: str, day: int) -> dict[str, Any]:
    return {
        "id": fid, "title": title, "body": "",
        "produced_at": datetime(2026, 9, day, 12, 0, tzinfo=timezone.utc),
    }


def _cfg(**kw) -> FrameAnchorConfig:
    return FrameAnchorConfig(**kw)


def test_a_polity_refused_only_because_of_the_squeeze_now_anchors():
    """THE SUPPRESSION HALF. A desk whose prose says "Jerusalem" in every
    finding and names the United States in six of them: before the repair the
    squeeze put the US on 100% of the pool, D-t called it boilerplate and
    refused it. Now its ubiquity is 6/16 and it anchors."""
    scope = [
        _finding(f"u{i}", f"US envoy meets officials in Jerusalem", 1 + i)
        for i in range(6)
    ]
    filler = [
        _finding(f"j{i}", "Jerusalem municipality approves the plan", 1 + i % 5)
        for i in range(10)
    ]
    pool = scope + filler
    index_pre = {
        p for p in candidate_polities()
        if _pre_repair_represented_by(p, "Jerusalem municipality approves") is not None
    }
    assert "United States" in index_pre, "fixture drift: the squeeze must fire"

    got = anchors_for(scope, desk_rows=pool, config=_cfg())
    anchored = {a.polity: a for a in got}
    assert "United States" in anchored
    assert anchored["United States"].evidence_mass == 6
    assert anchored["United States"].desk_ubiquity == pytest.approx(6 / 16)


def test_a_genuinely_ubiquitous_polity_is_still_refused():
    """D-t still does its job: a polity every finding on the desk names is wire
    boilerplate whether or not the squeeze was ever involved."""
    scope = [
        _finding(f"r{i}", "Russia escalates along the front", 1 + i)
        for i in range(8)
    ]
    got = anchors_for(scope, desk_rows=scope, config=_cfg())
    assert [a.polity for a in got] == []


def test_a_refused_anchor_still_does_not_consume_a_slot():
    """§2.1's order — ceiling BEFORE cap — is untouched by the repair."""
    scope = [
        _finding(f"m{i}", "Russia and Iran and Yemen and Lebanon meet", 1 + i)
        for i in range(8)
    ]
    pool = scope + [
        _finding(f"p{i}", "Russia files a protest", 1 + i % 5) for i in range(40)
    ]
    got = anchors_for(scope, desk_rows=pool, config=_cfg(max_anchors_per_scope=3))
    assert len(got) == 3
    assert "Russia" not in [a.polity for a in got]


# ---------------------------------------------------------------------------
# 6. Amendment 6's recovery leg — what the desk now reads
# ---------------------------------------------------------------------------


def _sig(sid: str, title: str, mag: float, geo: list[str]) -> dict[str, Any]:
    return {"id": sid, "source_id": f"src.{sid}", "geo": geo,
            "magnitude": mag, "title": title, "summary": ""}


def test_the_us_desks_recovery_leg_no_longer_admits_a_jerusalem_row():
    index = surface_index(polity_names_for_iso2("US"))
    rows = [_sig(f"f{i}", t, 0.90 - i * 0.01, ["IL"])
            for i, t in enumerate(LIVE_FALSE_ADMITS)]
    assert admitted_rows(rows, index, config=GeoRoutingConfig(),
                         row_cap=120) == []


def test_the_admission_cap_now_spends_its_slots_on_rows_that_name_the_desk():
    """The ceiling BINDS on the US desk — 30 slots for far more matches — so a
    false admit is not merely noise, it DISPLACES a row that names the desk.
    Twenty of the live thirty were false; here the budget is three."""
    index = surface_index(polity_names_for_iso2("US"))
    false_rows = [_sig(f"x{i}", t, 0.90, ["IL"])
                  for i, t in enumerate(LIVE_FALSE_ADMITS)]
    true_rows = [_sig(f"t{i}", t, 0.50, ["IS"])
                 for i, t in enumerate(LIVE_TRUE_NAMES)]
    cfg = GeoRoutingConfig(admit_share=0.025)   # 0.025 * 120 = 3 slots
    out = admitted_rows(false_rows + true_rows, index, config=cfg, row_cap=120)
    assert len(out) == 3
    assert all(r["id"].startswith("t") for r in out), [r["title"] for r in out]


@pytest.mark.parametrize("iso2", ["AU", "IR", "RU"])
def test_a_desk_with_no_short_squeezed_surface_is_untouched(iso2):
    """AU, IR and RU carry no multi-word surface at all, so their admissions
    are byte-identical across the repair — measured, not assumed."""
    index = surface_index(polity_names_for_iso2(iso2))
    rows = [_sig(f"a{i}", t, 0.5, ["XX"])
            for i, t in enumerate(LIVE_FALSE_ADMITS + LIVE_TRUE_NAMES)]
    now = [r["id"] for r in admitted_rows(
        rows, index, config=GeoRoutingConfig(), row_cap=120)]
    names = polity_names_for_iso2(iso2)
    before = [
        r["id"] for r in rows
        if any(_pre_repair_represented_by(n, r["title"]) for n in names)
    ]
    assert sorted(now) == sorted(before)
