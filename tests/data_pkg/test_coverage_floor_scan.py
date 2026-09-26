# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#82 — trigger class 9, ``coverage_floor``.

Pure tests (no DB): the alias/demonym representation matcher and the
possessive-stripping normalizer it rests on, the home-country exclusion
(including the two containment hazards a fold comparison gets wrong —
Nigeria/Niger and South Sudan/Sudan), the surface-to-cluster fold, each clause
of the bar in isolation, and the env-then-option config precedence.

Ephemeral-DB tests: the IL-SHAPED REGRESSION, driven through the REAL binding
path — ``legba.data.analysts.deterministic.run_method`` with
``options.sub_handler='alert_trigger_scan'``, i.e. the same dispatcher the
runtime calls — not by poking the sub-module directly. Evidence present and no
frame naming it FIRES exactly once; the same evidence with a frame that names
it (even only by demonym) stays SILENT; a resolved breach re-arms silently and
can fire again; the interval gate declines without advancing anything; and the
alert rides the D2 budget plumbing like every other class.

The SA/Yemen soft-FP fix (clause 6, 2026-09-05,
``planning/COVERAGE_FLOOR_82_2026-09-04.md`` §2.7; CORRECTED 2026-09-06,
``planning/COVERAGE_FLOOR_OVERSUPPRESS_FIX_REPORT.md``): a frame that names
only the ACTOR ("Houthi maritime embargo") must go silent on the missing
POLITY when at least ``frame_evidence_min_findings`` (default 2) of its OWN
cited FINDINGS have their own authored TITLE naming that polity — never
another frame's findings, never a single incidental mention, never the raw
grounding-signal population a finding's ``derived_from`` carries (proven live
to be desk-broad by construction, not a narrow citation — see the module
banner in ``_coverage_floor_scan.py``). The IL/Palestine negative control
proves the fix is discriminating, not a blanket suppressor: a frame with a
cited finding whose title never names the missing polity must still fire; the
regression-shape test proves it is FRAME-fenced, not desk-wide: two DIFFERENT
frames each carrying exactly one incidental mention (desk-wide sum >= the
bar, no single frame reaching it) must still fire.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts import deterministic
from legba.data.analysts.deterministic_handlers import _coverage_floor_scan as cfs
from legba.data.analysts.deterministic_handlers import alert_trigger_scan as ats
from legba.data.config import PostgresConfig
from legba.runtime.analyst_method import AnalystMethodResult

# ---------------------------------------------------------------------------
# Registry wiring — the four places a new trigger class silently half-lands
# ---------------------------------------------------------------------------


def test_coverage_floor_is_registered_in_every_per_class_registry():
    assert ats.TRIGGER_COVERAGE_FLOOR == "coverage_floor"
    assert ats.TRIGGER_COVERAGE_FLOOR in ats.TRIGGER_CLASSES
    assert ats.TRIGGER_COVERAGE_FLOOR in ats._CLASS_PRIORITY
    assert ats.TRIGGER_COVERAGE_FLOOR in ats._UNVERIFIED_REASONS
    assert len(set(ats._CLASS_PRIORITY.values())) == len(ats._CLASS_PRIORITY)


def test_coverage_floor_thresholds_are_declared_handler_options():
    """A ``coverage_floor_*`` knob not declared in HANDLER_OPTIONS is DROPPED
    whole at descriptor-resolution time, so an operator retune would silently
    no-op (the ``gauge_*`` precedent)."""
    from dataclasses import fields

    from legba.data.analysts.handler_options import HANDLER_OPTIONS

    declared = {
        o.name[len(cfs.OPTION_PREFIX):]
        for o in HANDLER_OPTIONS["alert_trigger_scan"]
        if o.name.startswith(cfs.OPTION_PREFIX)
    }
    assert declared == {f.name for f in fields(cfs.CoverageFloorConfig)}


def test_severity_is_low_so_it_never_outranks_a_world_event():
    """The class is a register-quality signal, not an event. At ``low`` it
    sorts last in apply_desk_cap and in the D2 budget, and sits under the
    fleet's ntfy floor (``medium``) — its durable product is the
    ``kind='alert'`` row, never a phone."""
    assert cfs.SEVERITY == "low"
    assert ats._SEVERITY_RANK[cfs.SEVERITY] < ats._SEVERITY_RANK["medium"]


# ---------------------------------------------------------------------------
# Pure — the representation matcher
# ---------------------------------------------------------------------------


def test_normalize_prose_strips_the_possessive_before_punctuation():
    """"Israel's coalition" must not normalize to "israels", which no
    word-boundary probe for "israel" could ever match — the exact shape that
    would have made this detector lie about its own archetype."""
    assert cfs.normalize_prose("Israel’s coalition splinters") == (
        "israel coalition splinters"
    )
    assert cfs.normalize_prose("Israel's coalition") == "israel coalition"
    assert cfs.represented_by("Israel", "Israel’s coalition splinters") is not None


@pytest.mark.parametrize(
    "country,prose,expected_hit",
    [
        # The RU case: the register never writes "Russia's war on Ukraine",
        # it writes "Ukrainian drone attacks" — and that IS coverage.
        ("Ukraine", "Coordinated narrative on large-scale Ukrainian drone "
                    "attacks in Russia", True),
        # The IR case: frames say "US strikes", the cluster is "United States".
        ("United States", "US maintains high-severity seizure of oil", True),
        ("United States", "U.S. extends Middle East troop deployments", True),
        # Plural demonym.
        ("Palestine", "Palestinians displaced in the West Bank", True),
        # Hyphenated multiword.
        ("United Kingdom", "Great-Britain trade talks resume", True),
        # The IL archetype: nothing in the register names it.
        ("Iran", "Israel receives third Dolphin-class submarine", False),
        ("Lebanon", "Israel maintains punitive defence-export ban on Qatar",
         False),
    ],
)
def test_represented_by_alias_and_demonym_matching(country, prose, expected_hit):
    assert (cfs.represented_by(country, prose) is not None) is expected_hit


def test_represented_by_is_generous_never_strict():
    """Every ambiguity resolves toward "covered", i.e. toward silence: a
    detector that over-reports coverage stays quiet, one that under-reports it
    pages on stories the register already carries."""
    blob = " || ".join(["A frame about Iranian proxies", "Another frame"])
    assert cfs.represented_by("Iran", blob) == "iranian"


# ---------------------------------------------------------------------------
# Pure — home-country exclusion
# ---------------------------------------------------------------------------

#: Every country desk the live fleet runs (2026-09-03), ISO2 -> the newsroom
#: spelling the canon produces. Pinned here because the home exclusion is the
#: one clause whose failure makes EVERY desk a permanent false positive.
_FLEET = {
    "AR": "Argentina", "AU": "Australia", "BR": "Brazil", "CA": "Canada",
    "CN": "China", "DE": "Germany", "FR": "France", "GB": "United Kingdom",
    "ID": "Indonesia", "IN": "India", "IT": "Italy", "JP": "Japan",
    "KR": "South Korea", "MX": "Mexico", "RU": "Russia", "SA": "Saudi Arabia",
    "TR": "Turkey", "US": "United States", "ZA": "South Africa",
    "BF": "Burkina Faso", "CD": "Democratic Republic of the Congo",
    "HT": "Haiti", "IL": "Israel", "IR": "Iran", "KP": "North Korea",
    "ML": "Mali", "MM": "Myanmar", "NE": "Niger", "PK": "Pakistan",
    "SD": "Sudan", "TW": "Taiwan", "UA": "Ukraine",
}


@pytest.mark.parametrize("iso2,name", sorted(_FLEET.items()))
def test_every_fleet_desk_excludes_its_own_country(iso2, name):
    assert cfs.is_home_country(name, cfs.home_prose([iso2]))


@pytest.mark.parametrize(
    "home_iso,foreign",
    [
        # Fold CONTAINMENT gets both of these wrong ("niger" is a substring of
        # "nigeria", "sudan" of "south sudan"); the surface matcher does not.
        ("NE", "Nigeria"),
        ("SD", "South Sudan"),
        # And the desk must still see its real second story.
        ("IL", "Iran"),
        ("TR", "Israel"),
        ("JP", "North Korea"),
    ],
)
def test_a_neighbour_is_not_the_home_country(home_iso, foreign):
    assert not cfs.is_home_country(foreign, cfs.home_prose([home_iso]))


# ---------------------------------------------------------------------------
# Pure — surface folding
# ---------------------------------------------------------------------------


def _row(surface, ner_class, n=30, days=12, mag=0.7, high=25):
    return {
        "surface": surface, "ner_class": ner_class, "n_signals": n,
        "n_days": days, "mean_magnitude": mag, "n_high": high,
        "exemplars": [],
    }


def test_cluster_entities_folds_surfaces_and_drops_everything_else():
    rows = [
        _row("Iran", "country", n=20),
        _row("Iranian", "entity", n=10),
        _row("iran", "country", n=4),
        _row("two", "entity"),            # junk numeral
        _row("Hezbollah", "organization"),  # real, but not a polity (v1 scope)
        _row("Tel Aviv", "location"),     # not a polity
        _row("Israeli", "country"),       # the desk's OWN country
    ]
    clusters = cfs.cluster_entities(rows, home_blob=cfs.home_prose(["IL"]))
    assert set(clusters) == {"iran"}
    iran = clusters["iran"]
    assert iran.name == "Iran"
    assert iran.n_signals == 34
    # n_days is the widest span any surface saw, never the sum.
    assert iran.n_days == 12
    assert iran.mean_magnitude == pytest.approx(0.7)


def test_cluster_entities_keeps_the_shortest_canonical_spelling():
    """Two surfaces that fold to ONE country but canonicalize to different
    spellings ("the Netherlands" / "Netherlands") must display the bare name —
    the alert names a polity, not whichever surface happened to arrive first."""
    clusters = cfs.cluster_entities(
        [_row("the Netherlands", "country"), _row("Netherlands", "country")],
        home_blob=cfs.home_prose(["JP"]),
    )
    assert [c.name for c in clusters.values()] == ["Netherlands"]
    assert clusters["netherlands"].surfaces == {"the netherlands", "netherlands"}


# ---------------------------------------------------------------------------
# Pure — the bar
# ---------------------------------------------------------------------------

_CFG = cfs.CoverageFloorConfig()


def _cluster(**over):
    base = dict(
        fold="iran", name="Iran", n_signals=40, n_days=12, n_high=30,
        magnitude_weight=0.7 * 40,
    )
    base.update(over)
    return cfs.EntityCluster(**base)


def test_clears_bar_at_the_defaults():
    assert cfs.clears_bar(_cluster(), slice_size=100, config=_CFG)


@pytest.mark.parametrize(
    "over,slice_size,why",
    [
        ({"n_signals": 19, "magnitude_weight": 0.7 * 19}, 100, "too few signals"),
        ({"n_days": 9}, 100, "not persistent enough"),
        ({"magnitude_weight": 0.4 * 40}, 100, "not consequential enough"),
        ({"n_high": 14}, 100, "too few individually-high signals"),
        ({}, 500, "too small a share of the desk's own slice"),
    ],
)
def test_every_clause_of_the_bar_is_load_bearing(over, slice_size, why):
    assert not cfs.clears_bar(
        _cluster(**over), slice_size=slice_size, config=_CFG
    ), why


def test_candidate_clusters_drops_the_named_and_orders_worst_first():
    clusters = {
        "iran": _cluster(fold="iran", name="Iran", n_signals=40,
                         magnitude_weight=0.7 * 40),
        "lebanon": _cluster(fold="lebanon", name="Lebanon", n_signals=60,
                            magnitude_weight=0.7 * 60),
        "syria": _cluster(fold="syria", name="Syria", n_signals=50,
                          magnitude_weight=0.7 * 50),
    }
    out = cfs.candidate_clusters(
        clusters,
        frame_names=["Israel-Syria border buildup continues"],
        config=_CFG,
    )
    assert [c.name for c in out] == ["Lebanon", "Iran"]


def test_candidate_clusters_is_a_superset_never_a_filter():
    """Nomination may only apply the two clauses whose summed values are upper
    bounds. A cluster whose SUMMED day count or magnitude looks too weak must
    still be nominated — the exact recount is what decides those."""
    weak = _cluster(n_days=1, magnitude_weight=0.0)  # would fail clears_bar
    assert not cfs.clears_bar(weak, slice_size=100, config=_CFG)
    assert cfs.candidate_clusters(
        {"iran": weak}, frame_names=["something else"], config=_CFG
    ) == [weak]


def test_a_cluster_with_no_slice_denominator_never_breaches():
    """Share is undefined on an empty slice; an undefined share must read as
    'does not clear', never as 'clears trivially'."""
    assert not cfs.clears_bar(_cluster(), slice_size=0, config=_CFG)


# ---------------------------------------------------------------------------
# Pure — config precedence + the interval gate
# ---------------------------------------------------------------------------


def test_config_defaults_then_env_then_option(monkeypatch):
    assert cfs.CoverageFloorConfig.from_options({}).min_slice_share == 0.10
    monkeypatch.setenv("LEGBA_COVERAGE_FLOOR_MIN_SLICE_SHARE", "0.25")
    assert cfs.CoverageFloorConfig.from_options({}).min_slice_share == 0.25
    # A descriptor option always wins over the env base.
    assert cfs.CoverageFloorConfig.from_options(
        {"coverage_floor_min_slice_share": 0.4}
    ).min_slice_share == 0.4


def test_a_mistyped_knob_keeps_its_predecessor_and_never_raises(monkeypatch):
    monkeypatch.setenv("LEGBA_COVERAGE_FLOOR_MIN_SIGNALS", "not-a-number")
    cfg = cfs.CoverageFloorConfig.from_options(
        {"coverage_floor_min_days": "also-not-a-number"}
    )
    assert cfg.min_signals == 20 and cfg.min_days == 10


def test_unknown_options_are_ignored():
    cfg = cfs.CoverageFloorConfig.from_options(
        {"per_desk_cap": 3, "gauge_window_days": 5}
    )
    assert cfg == cfs.CoverageFloorConfig()


def test_interval_gate_degrades_toward_doing_the_work():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    assert cfs._interval_elapsed(None, now=now, hours=6.0)
    assert cfs._interval_elapsed({"last_scan_at": "garbage"}, now=now, hours=6.0)
    assert cfs._interval_elapsed({}, now=now, hours=6.0)
    fresh = {"last_scan_at": (now - timedelta(hours=1)).isoformat()}
    assert not cfs._interval_elapsed(fresh, now=now, hours=6.0)
    assert cfs._interval_elapsed(fresh, now=now, hours=0.0)
    stale = {"last_scan_at": (now - timedelta(hours=7)).isoformat()}
    assert cfs._interval_elapsed(stale, now=now, hours=6.0)


def test_build_body_states_the_evidence_the_register_and_the_disclaimer():
    body = cfs.build_body(
        "country_watch_il",
        [_cluster()],
        standing=[_cluster()],
        frame_names=["Israel receives third Dolphin-class submarine"],
        slice_size=100,
        config=_CFG,
    )
    assert "Iran: 40 signal(s) over 12 day(s)" in body
    assert "Dolphin-class" in body
    assert "not a claim that the desk's read is wrong" in body


# ---------------------------------------------------------------------------
# DB lifecycle — the IL-shaped regression, through the REAL binding path
# ---------------------------------------------------------------------------


#: Every row this file writes carries one of these, so ``clean_slate`` can
#: remove exactly its own leavings and nothing else's.
_SOURCE = "test_82_source"
_DESK_PREFIX = "country_watch_cf_"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _sweep(conn: Any) -> None:
    """This file's own signals/frames/desks — see ``clean_slate`` below for
    why this half is run BOTH before and after every test."""
    await conn.execute("DELETE FROM signals WHERE source_id = $1", _SOURCE)
    await conn.execute(
        "DELETE FROM situations WHERE target_id LIKE $1", f"{_DESK_PREFIX}%"
    )
    await conn.execute(
        "DELETE FROM target_descriptors WHERE descriptor_id LIKE $1",
        f"{_DESK_PREFIX}%",
    )


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    """Fresh watermarks, no leftover trigger alerts — AND no leftover signals,
    frames or desks from this file's own earlier tests.

    The signal/desk sweep is not optional hygiene: every desk here scopes on
    ``geo && ['IL']``, so one test's seeded evidence lands inside the NEXT
    test's window slice, the next test's brand-new desk breaches on scan 1, the
    seed contract adopts it silently, and the test that meant to watch it fire
    watches nothing happen. That is a real property of the scan (a desk sees
    every signal its geo matches, whoever inserted it), so the fixture removes
    the rows rather than the property.

    The sweep also runs AFTER the test (not just before), the same two-sided
    shape ``test_research_dispatch.py``'s own ``clean_slate`` already uses for
    exactly this hazard (see that file's docstring — it names this file by
    name). One-sided-before was the actual bug: this file's LAST test left an
    ``is_head``/``active`` ``country_watch_cf_*`` desk and its geo=['IL']
    signals sitting in the shared session DB with nothing to remove them, so
    whichever suite ran next and stood up its OWN geo=['IL'] desk inherited
    them — sharing entity clusters across files' desks, silently pre-seeding
    (or duplicate-firing) a fold the other suite's own test meant to control
    end to end (the cross-file ``test_research_dispatch.py`` flake this
    fixture now closes).
    """
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE alert_trigger_watermarks")
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = 'alert_trigger_scan'"
        )
        await _sweep(conn)
    yield
    async with pg_pool.acquire() as conn:
        await _sweep(conn)


class _FakeDispatcher:
    def __init__(self) -> None:
        self.payloads: list[Any] = []

    async def fan_out(self, payload: Any) -> list[Any]:
        self.payloads.append(payload)
        return []


class _Deps:
    def __init__(self, pool: Any, dispatcher: Any) -> None:
        self.pg_pool = pool
        self.extras = {"alert_sink_dispatcher": dispatcher}


async def _run(pool: Any, dispatcher: Any | None = None, **opts: Any):
    """THE REAL BINDING PATH: the deterministic-kind dispatcher routes by
    ``options.sub_handler`` exactly as the runtime does — never a direct call
    into the sub-module under test."""
    deps = _Deps(pool, dispatcher if dispatcher is not None else _FakeDispatcher())
    options = {
        "sub_handler": "alert_trigger_scan",
        "analyst_id": "alert_trigger_scan",
        "run_id": str(uuid4()),
        # Every 10-minute tick would otherwise decline the heavy scan; the
        # gate has its own dedicated test below.
        "coverage_floor_min_scan_interval_hours": 0.0,
        "daily_page_budget": 10_000,
        **opts,
    }
    result = await deterministic.run_method([], options, deps)
    assert isinstance(result, AnalystMethodResult)
    return result


def _row_data(row: Any) -> dict[str, Any]:
    d = row["data"]
    full = json.loads(d) if isinstance(d, str) else dict(d)
    inner = full.get("data")
    return inner if isinstance(inner, dict) else full


async def _cf_rows(conn: Any, desk: str) -> list[Any]:
    rows = await conn.fetch(
        "SELECT id, title, severity, target_id, data, derived_from "
        "FROM analyst_outputs "
        "WHERE kind = 'alert' AND analyst_id = 'alert_trigger_scan' "
        "  AND target_id = $1 "
        "ORDER BY produced_at, id",
        desk,
    )
    return [
        r for r in rows
        if _row_data(r).get("trigger_class") == ats.TRIGGER_COVERAGE_FLOOR
    ]


async def _insert_desk(conn: Any, desk: str, geo: list[str]) -> None:
    await conn.execute(
        "INSERT INTO target_descriptors "
        "  (descriptor_id, version, schema_uri, is_head, state, owner, name, "
        "   body) "
        "VALUES ($1, 'v1', 'legba/target/2.0.0', TRUE, 'active', "
        "        'test_82', $1, $2::jsonb) "
        "ON CONFLICT DO NOTHING",
        desk,
        json.dumps({"scope": {"geo": geo, "tags": ["watch"]}}),
    )


async def _insert_frame(
    conn: Any, desk: str, name: str, *, dim: str, derived_from: Any = (),
) -> UUID:
    """``derived_from`` (default empty, matching every pre-existing caller
    byte-for-byte) is the frame's OWN cited-evidence set — finding ids, the
    same attachment relation ``situations.derived_from`` carries live
    (``situation_tracker._NEW_EVIDENCE_SQL``'s own comment). Only the SA/Yemen
    fix tests populate it."""
    fid = uuid4()
    await conn.execute(
        "INSERT INTO situations "
        "  (id, data, name, status, category, target_id, intensity_score, "
        "   event_count, valid_from, derived_from) "
        "VALUES ($1, $2::jsonb, $3, 'dormant', 'country', $4, 1.0, 5, now(), "
        "        $5::uuid[])",
        fid,
        json.dumps({"dimension": dim}),
        name,
        desk,
        list(derived_from),
    )
    return fid


async def _insert_finding(
    conn: Any, *, target_id: str, title: str = "seeded finding",
    derived_from: Any = (),
) -> UUID:
    """A minimal 'finding' analyst_outputs row citing ``derived_from`` (signal
    ids, default empty) as its raw grounding population, with its own
    authored ``title`` — the field clause 6 (the SA/Yemen fix) reads per the
    2026-09-06 correction. A finding's raw ``derived_from`` population is the
    analyst's WHOLE trailing grounding window (measured live: every
    non-``country_composition`` dimension cites ~120 signals on IL / ~62 on
    SA, every time it runs), not a narrow per-claim citation, so it can never
    discriminate a genuine per-frame story from desk-wide noise — only the
    finding's own authored title can. See ``_coverage_floor_scan.py``'s
    module banner, clause 6."""
    fid = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, target_id, analyst_id, schema_uri, "
        "   derived_from) "
        "VALUES ($1, 'finding', $2, '', $3, 'test_82', "
        "        'iglu:legba/finding/jsonschema/1-0-0', $4::uuid[])",
        fid,
        title,
        target_id,
        list(derived_from),
    )
    return fid


async def _insert_entity_signals(
    conn: Any,
    geo: list[str],
    *,
    entities: list[str],
    n_days: int,
    per_day: int,
    magnitude: float,
) -> list[str]:
    """``n_days * per_day`` salience-scored, entity-bearing signals spread one
    per day so the persistence clause is exercised for real."""
    payload = {
        "title": "seeded",
        "entities": [
            {"text": e, "class": "country", "confidence": 1.0} for e in entities
        ],
    }
    salience = {"magnitude": magnitude, "authority": "reporting",
                "event_class": "kinetic_strike"}
    ids: list[str] = []
    for day in range(n_days):
        for _ in range(per_day):
            sid = uuid4()
            ids.append(str(sid))
            await conn.execute(
                "INSERT INTO signals "
                "  (id, source_id, payload, salience, geo, fetched_at, "
                "   content_hash) "
                "VALUES ($1, $7, $2::jsonb, $3::jsonb, "
                "        $4::text[], now() - make_interval(days => $5, "
                "        hours => 1), $6)",
                sid,
                json.dumps(payload),
                json.dumps(salience),
                geo,
                day,
                sid.hex,
                _SOURCE,
            )
    return ids


async def _seed_il_shaped_evidence(conn: Any, desk: str) -> list[str]:
    """The IL shape, at the live proportions: a foreign polity in ~every
    entity-bearing signal of the desk's own window slice, high-salience, on
    twelve separate days — and a register whose frames are about something
    else entirely (these are the REAL 2026-09-03 IL frame names)."""
    ids = await _insert_entity_signals(
        conn, ["IL"], entities=["Iran", "Iranian"], n_days=12, per_day=3,
        magnitude=0.8,
    )
    for name, dim in (
        ("Israel’s coalition splinters as right-wing bloc forms", "internal_stability"),
        ("Israel receives third Dolphin-class submarine", "military_posture"),
        ("Israel maintains punitive defence-export ban on Qatar", "economic_coercion"),
    ):
        await _insert_frame(conn, desk, name, dim=dim)
    return ids


async def test_seeds_silently_then_fires_once_then_never_refires(
    pg_pool, clean_slate
):
    """Evidence present, frame absent => it fires. Exactly once.

    Scan 1 runs on a desk with no evidence yet, so the class adopts the fleet's
    standing state and pages nothing (the 0091 seed contract). The IL-shaped
    evidence then lands, and scan 2 pages it. Scan 3 — same standing breach,
    nothing changed — is silent, which is the whole no-refire contract.
    """
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["IL"])

    r1 = await _run(pg_pool)
    assert ats.TRIGGER_COVERAGE_FLOOR in r1.finding.data["seeded_classes"]
    async with pg_pool.acquire() as conn:
        assert await _cf_rows(conn, desk) == []

    async with pg_pool.acquire() as conn:
        await _seed_il_shaped_evidence(conn, desk)

    dispatcher = _FakeDispatcher()
    r2 = await _run(pg_pool, dispatcher)
    async with pg_pool.acquire() as conn:
        rows = await _cf_rows(conn, desk)
    assert len(rows) == 1
    row = rows[0]
    assert row["severity"] == cfs.SEVERITY
    data = _row_data(row)
    assert data["trigger_class"] == ats.TRIGGER_COVERAGE_FLOOR
    assert [c["name"] for c in data["clusters"]] == ["Iran"]
    assert data["clusters"][0]["n_days"] == 12
    assert data["clusters"][0]["slice_share"] == pytest.approx(1.0, abs=0.05)
    # The register it was checked against is IN the payload — an operator can
    # see what was compared without re-running anything.
    assert data["open_frame_count"] == 3
    assert any("Dolphin" in n for n in data["open_frame_names"])
    # Lineage: the alert NAMES exemplar signals, so the P1 walk resolves it.
    assert len(row["derived_from"]) > 0
    assert r2.finding.data["counts_by_class"][ats.TRIGGER_COVERAGE_FLOOR][
        "new_breaches"
    ] == 1
    assert any(
        "Iran" in getattr(p, "summary", "") for p in dispatcher.payloads
    )

    r3 = await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert len(await _cf_rows(conn, desk)) == 1
    counts = r3.finding.data["counts_by_class"][ats.TRIGGER_COVERAGE_FLOOR]
    assert counts["new_breaches"] == 0 and counts["breaches"] >= 1


async def test_a_frame_that_names_the_polity_is_silent(pg_pool, clean_slate):
    """The negative control, and the whole point of the alias/demonym matcher:
    the SAME evidence, with a register that names the story — by demonym only —
    must produce nothing."""
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["IL"])
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        await _insert_entity_signals(
            conn, ["IL"], entities=["Iran", "Iranian"], n_days=12, per_day=3,
            magnitude=0.8,
        )
        await _insert_frame(
            conn, desk,
            "Israel – Iranian missile salvos drive near-term escalation",
            dim="escalation",
        )

    r = await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _cf_rows(conn, desk) == []
    assert r.finding.data["counts_by_class"][ats.TRIGGER_COVERAGE_FLOOR][
        "new_breaches"
    ] == 0


async def test_a_resolved_breach_rearms_silently_and_can_fire_again(
    pg_pool, clean_slate
):
    """Recovery is SILENT (a gap closing is not an operator event) but it must
    genuinely re-arm — a frame that names the story and is later renamed away
    from it is a fresh breach, not a suppressed one."""
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["IL"])
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        await _seed_il_shaped_evidence(conn, desk)
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert len(await _cf_rows(conn, desk)) == 1
        covering = await conn.fetchval(
            "INSERT INTO situations (id, data, name, status, category, "
            "  target_id, intensity_score, event_count, valid_from) "
            "VALUES ($1, '{}'::jsonb, 'Israel-Iran war continues', 'active', "
            "        'country', $2, 5.0, 9, now()) RETURNING id",
            uuid4(), desk,
        )

    r = await _run(pg_pool)  # covered now -> resolves, silently
    counts = r.finding.data["counts_by_class"][ats.TRIGGER_COVERAGE_FLOOR]
    assert counts["resolved"] == 1 and counts["candidates"] == 0
    async with pg_pool.acquire() as conn:
        assert len(await _cf_rows(conn, desk)) == 1
        await conn.execute(
            "UPDATE situations SET name = 'Israel border-force buildup' "
            "WHERE id = $1", covering,
        )

    await _run(pg_pool)  # uncovered again -> a genuinely new breach
    async with pg_pool.acquire() as conn:
        assert len(await _cf_rows(conn, desk)) == 2


async def test_the_interval_gate_declines_without_advancing_anything(
    pg_pool, clean_slate
):
    """A skipped tick must lose nothing: no watermark moves, no seed is
    recorded, and the very next due scan still fires the transition."""
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["IL"])
        await _seed_il_shaped_evidence(conn, desk)

    r1 = await _run(pg_pool)  # seeds (interval override = 0)
    assert ats.TRIGGER_COVERAGE_FLOOR in r1.finding.data["seeded_classes"]

    r2 = await _run(pg_pool, coverage_floor_min_scan_interval_hours=6.0)
    counts = r2.finding.data["counts_by_class"][ats.TRIGGER_COVERAGE_FLOOR]
    assert counts["skipped_interval"] == 1
    assert counts["targets"] == 0 and counts["candidates"] == 0

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE situations SET name = 'renamed, still not about Iran' "
            "WHERE target_id = $1", desk,
        )
    # A brand-new breach appears only once the class is past its seed; force
    # the due path and confirm the transition survived the skipped tick.
    async with pg_pool.acquire() as conn:
        await _insert_entity_signals(
            conn, ["IL"], entities=["Lebanon"], n_days=12, per_day=4,
            magnitude=0.8,
        )
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _cf_rows(conn, desk)
    assert len(rows) == 1
    assert "Lebanon" in [c["name"] for c in _row_data(rows[0])["clusters"]]


async def test_the_alert_rides_the_d2_budget_plumbing(pg_pool, clean_slate):
    """No special-casing: with the day's page budget exhausted the row is still
    written and tagged ``budget_deferred`` (never a silent drop) and is simply
    never handed to the dispatcher — the same observable-suppression contract
    every other class gets."""
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["IL"])
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        await _seed_il_shaped_evidence(conn, desk)

    dispatcher = _FakeDispatcher()
    await _run(pg_pool, dispatcher, daily_page_budget=0)
    async with pg_pool.acquire() as conn:
        rows = await _cf_rows(conn, desk)
    assert len(rows) == 1
    assert _row_data(rows[0])["budget_deferred"] is True
    assert dispatcher.payloads == []


# ---------------------------------------------------------------------------
# Clause 6 — the SA/Yemen soft-FP fix (2026-09-05, corrected 2026-09-06) and
# its negative control + regression-shape guard.
# ---------------------------------------------------------------------------


async def test_sa_yemen_actor_named_frame_is_silenced_by_its_own_cited_evidence(
    pg_pool, clean_slate,
):
    """THE FIX, corrected 2026-09-06 (planning/COVERAGE_FLOOR_OVERSUPPRESS_
    FIX_REPORT.md). ``planning/COVERAGE_FLOOR_82_2026-09-04.md`` §2.7's live
    census: SA's register names only the actor ("Houthi maritime embargo
    disrupts Red Sea shipping"), never the polity Yemen, even though SA's
    desks DO engage (4/8) — a soft false positive on the outcome measure.
    The first cut of this fix (df088954) read a cited finding's raw grounding
    SIGNAL population; live measurement (2026-09-06) showed that population is
    the analyst's WHOLE trailing window, not a narrow citation, so it can
    never discriminate a real per-frame story from noise (it cleared IL's
    founding case too). The corrected clause instead reads the frame's own
    cited findings' AUTHORED TITLES and requires RECURRENCE
    (``config.frame_evidence_min_findings``, default 2), so one incidental
    mention cannot silence a real gap. Reproduced here at the live shape: a
    frame whose own TWO cited findings each independently name Yemen in their
    own title — SA's genuinely-covering Houthi frames carry 2-6 such findings
    apiece live."""
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["SA"])
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        await _insert_entity_signals(
            conn, ["SA"], entities=["Yemen"], n_days=12, per_day=4,
            magnitude=0.8,
        )
        finding_1 = await _insert_finding(
            conn, target_id=desk,
            title="Saudi Arabia – punitive trade blockade on Yemen "
                  "(wielder)",
        )
        finding_2 = await _insert_finding(
            conn, target_id=desk,
            title="Yemeni maritime embargo keeps Saudi oil exports choked",
        )
        await _insert_frame(
            conn, desk,
            "Houthi maritime embargo disrupts Red Sea shipping",
            dim="military_posture",
            derived_from=[finding_1, finding_2],
        )

    r = await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _cf_rows(conn, desk) == []
    assert r.finding.data["counts_by_class"][ats.TRIGGER_COVERAGE_FLOOR][
        "new_breaches"
    ] == 0


async def test_il_palestine_still_fires_when_cited_evidence_does_not_name_it(
    pg_pool, clean_slate,
):
    """THE NEGATIVE CONTROL for clause 6 — IL/Palestine, the detector's own
    founding case (20.5% of IL's own entity-bearing slice at the live
    2026-09-06 recount), must STILL fire even after the SA/Yemen fix lands.
    The frame here HAS its own cited finding (unlike the bare-frame shape
    every other IL test in this file uses) — but that finding's title never
    names Palestine, so clause 6 must correctly decline to suppress. This is
    what proves the fix is discriminating (a frame's OWN authored titles, not
    a blanket 'any frame with cited findings is covered' pass)."""
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["IL"])
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        await _insert_entity_signals(
            conn, ["IL"], entities=["Palestine", "Palestinian"], n_days=12,
            per_day=3, magnitude=0.8,
        )
        # A frame WITH its own cited finding — but its title names a
        # submarine deal, never Palestine, so clause 6 has nothing to clear
        # it with.
        finding_id = await _insert_finding(
            conn, target_id=desk,
            title="Israel acquires third Dolphin-class submarine",
        )
        await _insert_frame(
            conn, desk, "Israel receives third Dolphin-class submarine",
            dim="military_posture", derived_from=[finding_id],
        )

    r = await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _cf_rows(conn, desk)
    assert len(rows) == 1
    assert "Palestine" in [c["name"] for c in _row_data(rows[0])["clusters"]]
    assert r.finding.data["counts_by_class"][ats.TRIGGER_COVERAGE_FLOOR][
        "new_breaches"
    ] == 1


async def test_two_frames_each_with_one_incidental_mention_does_not_clear(
    pg_pool, clean_slate,
):
    """THE REGRESSION'S EXACT SHAPE (planning/COVERAGE_FLOOR_OVERSUPPRESS_
    FIX_REPORT.md). df088954's shipped clause 6 pooled EVERY open frame's
    cited evidence into one blob PER DESK, so a polity mentioned once each in
    TWO DIFFERENT frames' evidence cleared the breach even though NEITHER
    frame's own evidence recurringly named it — live, this is exactly
    IL/Palestine's shape (six of IL's eight frames each carried a single
    incidental Palestinian-adjacent finding; no ONE frame ever reached the
    recurrence bar, yet the pooled desk-wide read cleared every time).
    Reproduced here: two frames, each with exactly ONE of its own cited
    findings naming the polity — below ``config.frame_evidence_min_findings``
    (2) on EITHER frame alone, though a desk-wide pool would sum to 2. The
    corrected, FRAME-FENCED clause 6 must decline to clear, and the breach
    must still fire."""
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["IL"])
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        await _insert_entity_signals(
            conn, ["IL"], entities=["Palestine", "Palestinian"], n_days=12,
            per_day=3, magnitude=0.8,
        )
        finding_a = await _insert_finding(
            conn, target_id=desk,
            title="Coordinated push for forced Palestinian displacement "
                  "from Gaza emerges",
        )
        await _insert_frame(
            conn, desk, "Coordinated narrative pushes ridge capture",
            dim="narrative_coordination", derived_from=[finding_a],
        )
        finding_b = await _insert_finding(
            conn, target_id=desk,
            title="Israel's coalition splinters as right-wing bloc raises "
                  "Palestinian statehood question",
        )
        await _insert_frame(
            conn, desk,
            "Israel's right-wing bloc tightens, deepening coalition "
            "fracture",
            dim="leadership_transition", derived_from=[finding_b],
        )

    r = await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _cf_rows(conn, desk)
    assert len(rows) == 1
    assert "Palestine" in [c["name"] for c in _row_data(rows[0])["clusters"]]
    assert r.finding.data["counts_by_class"][ats.TRIGGER_COVERAGE_FLOOR][
        "new_breaches"
    ] == 1


# ---------------------------------------------------------------------------
# R1-e — ``breaches_naming_only``, the design's B-1 producer
# (planning/R1_FRAME_REPAIR_AMENDMENT_2026-09-06.md §3).
#
# BEHAVIOUR-NEUTRAL by contract: the three tests above are the neutrality
# proof (they pass unchanged, alerts and watermarks byte-identical), and these
# four pin the counter itself — its presence in the receipt, its ordering
# against ``breaches``, and the two live shapes the amendment names.
# ---------------------------------------------------------------------------


def _cf_counts(result: Any) -> dict[str, Any]:
    return result.finding.data["counts_by_class"][ats.TRIGGER_COVERAGE_FLOOR]


async def test_the_naming_only_counter_rides_every_receipt_with_a_stable_shape(
    pg_pool, clean_slate,
):
    """THE RECEIPT SHAPE, pinned on both branches of the interval gate.

    A skipped tick and a real scan must carry the SAME keys — a counter that
    appears only on the ticks that did work cannot be read as a series, and
    the post-R4 comparison is a series."""
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["IL"])
    real = _cf_counts(await _run(pg_pool))
    # A tick the interval gate declines: same shape, all zeros.
    skipped = _cf_counts(
        await _run(pg_pool, coverage_floor_min_scan_interval_hours=24.0)
    )
    for counts in (real, skipped):
        assert counts["nominated_naming_only"] >= 0
        assert counts["breaches_naming_only"] >= 0
        assert counts["naming_only_recount_bound_hit"] == 0
    assert skipped["skipped_interval"] == 1
    assert skipped["breaches_naming_only"] == 0
    # The counter is ADDITIVE: every pre-existing key is still there.
    for key in ("breaches", "new_breaches", "nominated", "resolved", "seeded"):
        assert key in real


async def test_naming_only_never_undercounts_the_breach_count(
    pg_pool, clean_slate,
):
    """``breaches_naming_only >= breaches``, on the fixture that breaches.

    Structural, not incidental: the naming-only nomination set is a strict
    superset of the clause-6-suppressed one (clause 6 can only REMOVE
    candidates) and both sets pass through the identical exact recount and the
    identical :func:`clears_bar`. If this inverts, the two passes are not
    reading the same bar."""
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["IL"])
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        await _seed_il_shaped_evidence(conn, desk)
    counts = _cf_counts(await _run(pg_pool))
    assert counts["breaches"] >= 1
    assert counts["breaches_naming_only"] >= counts["breaches"]
    assert counts["nominated_naming_only"] >= counts["nominated"]


async def test_il_shape_one_qualifying_title_leaves_both_counts_equal(
    pg_pool, clean_slate,
):
    """THE IL SHAPE (amendment §1.3): ONE cited finding whose authored title
    names Palestine — one short of ``frame_evidence_min_findings`` (2).

    Clause 6 suppresses nothing here, so the two passes must agree exactly.
    This is the case that makes the counter honest: it is not a second,
    looser detector, it is the SAME detector with one clause removed, and
    where that clause is inert the numbers must be identical."""
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["IL"])
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        await _insert_entity_signals(
            conn, ["IL"], entities=["Palestine", "Palestinian"], n_days=12,
            per_day=3, magnitude=0.8,
        )
        # The live 2026-09-06 finding, on the live frame (the amendment's
        # §1.3 attribution correction).
        naming = await _insert_finding(
            conn, target_id=desk,
            title="Coordinated push for forced Palestinian displacement "
                  "from Gaza emerges",
        )
        await _insert_frame(
            conn, desk, "Coordinated narrative pushes ridge capture",
            dim="narrative_coordination", derived_from=[naming],
        )

    counts = _cf_counts(await _run(pg_pool))
    assert counts["breaches"] == 1
    assert counts["breaches_naming_only"] == 1
    assert counts["nominated_naming_only"] == counts["nominated"]


async def test_sa_shape_naming_only_exceeds_the_breach_count_by_the_pair(
    pg_pool, clean_slate,
):
    """THE SA SHAPE (amendment §1.3 / the fix report): three frames carrying
    6, 5 and 2 of their own cited findings naming Yemen — the live count on
    SA's genuinely-covering Houthi frames.

    Clause 6 clears the pair, so the detector is silent and ``breaches`` is 0.
    The naming-only pass sees the same evidence with clause 6 removed and
    counts it: exactly ONE more, the suppressed pair. That difference IS the
    number bar B-1 tracks to zero BY REPAIR, and it must be visible while the
    detector stays correctly quiet — otherwise R-1's founding disagreement
    (amendment F-11) has no producer at all."""
    desk = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["SA"])
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        await _insert_entity_signals(
            conn, ["SA"], entities=["Yemen"], n_days=12, per_day=4,
            magnitude=0.8,
        )
        for dim, n_naming in (
            ("military_posture", 6),
            ("economic_coercion", 5),
            ("escalation", 2),
        ):
            findings = [
                await _insert_finding(
                    conn, target_id=desk,
                    title=f"Saudi Arabia – Yemen corridor pressure {dim} {i}",
                )
                for i in range(n_naming)
            ]
            await _insert_frame(
                conn, desk,
                f"Houthi maritime embargo disrupts Red Sea shipping ({dim})",
                dim=dim, derived_from=findings,
            )

    r = await _run(pg_pool)
    counts = _cf_counts(r)
    assert counts["breaches"] == 0
    assert counts["new_breaches"] == 0
    assert counts["breaches_naming_only"] == 1
    # ...and the counter changed NOTHING outward: no alert row, and no
    # breached watermark for the pair clause 6 correctly cleared.
    async with pg_pool.acquire() as conn:
        assert await _cf_rows(conn, desk) == []
        marks = await conn.fetch(
            "SELECT watermark_key, state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key LIKE $2",
            cfs.TRIGGER_CLASS, f"{desk}|%",
        )
    assert marks == []
