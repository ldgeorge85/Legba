# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R1-d — the frame-content gauge (``legba.data._frame_content``).

Pure tests: the ``COVERAGE_FLOOR_82`` §1.1 NAME test reproduced on fixture rows
shaped like the live register (and its three exclusions each shown to bite);
the name test's INDEPENDENCE from the anchor — a frame with an ``#evt:`` slot
and a contentless name is still contentless, which is what stops the gauge that
judges the repair from being computed by the repair; the domestic/retiring
split; ``None``-not-zero on every empty denominator; the design §2.2 census and
its monotone predictor; the mint-floor twin.

Guard tests: the AST fence (no LLM, no producer self-description, and the
finding projection as the structural half of that fence), the acyclic import
direction, and the instrument's verify exemption.

DB tests (ephemeral/pivot): the gauge runs and publishes through the REAL
binding path — ``deterministic.run_method`` with
``options.sub_handler='situation_clustering'``, the dispatcher the runtime
calls — with the R-1 flag ABSENT, which is the whole point: this establishes
the baseline it will later be judged against. And the receipt's pre-gauge
fields are byte-identical with the gauge present.
"""
from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data import _frame_content as fc
from legba.data._polity_match import home_prose
from legba.data.analysts import deterministic
from legba.data.analysts.deterministic_handlers import situation_clustering as sc
from legba.data.config import PostgresConfig
from legba.data.provenance.kinds import STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
from legba.runtime.analyst_method import AnalystMethodResult

_IL = home_prose(["IL"])
_AR = home_prose(["AR"])


def _frame(
    target: str, name: str, *, dim: str = "military_posture",
    anchor: str | None = None, intensity: float = 1.0,
) -> dict[str, Any]:
    signature = f"sig:{target}#dim:{dim}"
    if anchor is not None:
        signature = f"{signature}#evt:{anchor}"
    return {
        "target_id": target,
        "name": name,
        "situation_signature": signature,
        "intensity_score": intensity,
        "dimension": dim,
    }


# ---------------------------------------------------------------------------
# THE NAME TEST — COVERAGE_FLOOR_82 §1.1, reproduced as a METHOD
# ---------------------------------------------------------------------------


def test_the_name_test_reproduces_the_census_method_on_fixture_rows():
    """The §1.1 census's own method, on eight REAL live frame names.

    Four are the register's characteristic empty shape ("grammatically frames
    and informationally empty") and four carry a distinguishing proper noun.
    The live fleet reading this same code produces 139 contentless of 232
    open frames (the census's own read, hours earlier, was 138) and 114 of
    the 192 the desks actually SEE (against its 112). Those live numbers are
    recorded in the R1-d report; what is pinned HERE is the METHOD, because
    the register is re-named every twenty minutes and a pinned live number
    would be a flake by design.
    """
    empty = [
        _frame("country_g20_ar",
               "Argentina’s military posture unchanged as naval base "
               "remains pending"),
        _frame("country_g20_ar",
               "Argentina – mass protests over property bill raise internal "
               "instability"),
        _frame("country_watch_il",
               "Israel’s energy-security pressure stays moderate as no new "
               "disruption is observed"),
        _frame("country_watch_il",
               "Escalating rhetoric heightens Israeli escalation risk"),
    ]
    named = [
        _frame("country_g20_ar",
               "Argentina’s naval base funding and Falklands sanctions drive "
               "escalation risk"),
        _frame("country_g20_ar",
               "Milei’s Falklands push dominates, but no sign of leadership "
               "shake-up"),
        _frame("country_watch_il",
               "Israel receives third Dolphin-class submarine"),
        _frame("country_watch_il",
               "Israel expands deployment in southern Lebanon by seizing Ali "
               "Taher ridge"),
    ]
    blobs = {"country_g20_ar": _AR, "country_watch_il": _IL}
    for row in empty:
        assert fc.is_contentless(
            row["name"], home_blob=blobs[row["target_id"]]
        ), row["name"]
    for row in named:
        assert not fc.is_contentless(
            row["name"], home_blob=blobs[row["target_id"]]
        ), row["name"]

    gauge = fc.frame_content_rows(
        empty + named,
        home_blobs=blobs,
        touched_signatures=[r["situation_signature"] for r in empty + named],
    )
    assert gauge["country_g20_ar"]["contentless"] == 2
    assert gauge["country_watch_il"]["contentless"] == 2
    assert gauge["country_g20_ar"]["contentless_rate"] == 0.5


@pytest.mark.parametrize(
    "name, blob, why",
    [
        ("Israel’s military posture unchanged", _IL,
         "dimension boilerplate is stop-listed"),
        ("Israel receives no new capability", _IL,
         "the desk's own country is resolved through the shipped matcher"),
        ("Israeli deployment holds steady", _IL,
         "...including by demonym, which a fold containment would miss"),
        ("Escalating tensions heighten regional risk", _IL,
         "a driving verb is not an entity, wherever it is capitalised"),
    ],
)
def test_each_of_the_census_exclusions_bites(name, blob, why):
    """Every exclusion is load-bearing: drop any one and these read as
    content. Each case is a live frame-name shape, not a constructed one."""
    assert fc.is_contentless(name, home_blob=blob), why


def test_the_leading_sentence_word_is_excluded_and_it_matters():
    """§1.1's third exclusion, stated apart because it is the one that moves
    the number: with it the live fleet reads 139 of 232 contentless, without
    it 120 — the difference between reproducing the census and reporting a
    different, more flattering claim."""
    name = "Argentina-US anti-drug partnership narrative gains coverage"
    assert fc.leading_token(name) == "Argentina-US"
    assert fc.is_contentless(name, home_blob=_AR)
    # ...and a proper noun anywhere ELSE in the sentence still counts.
    assert not fc.is_contentless(
        "Argentina ramps up sanctions around the Falklands", home_blob=_AR
    )


def test_the_name_test_is_independent_of_any_anchor_field():
    """THE INDEPENDENCE, which is the amendment's §4 obligation 2.

    A frame carrying a minted ``#evt:palestine`` anchor whose NAME says
    nothing is still CONTENTLESS, and a frame with no anchor at all whose name
    names Lebanon is still CONTENTFUL. If this ever inverted, the gauge that
    judges the repair would be computed by the repair and would report success
    by construction.
    """
    anchored_but_empty = _frame(
        "country_watch_il",
        "Israel’s military posture unchanged as no new deployment observed",
        anchor="palestine",
    )
    domestic_but_named = _frame(
        "country_watch_il",
        "Israel expands deployment in southern Lebanon",
        dim="escalation", anchor=fc.DOMESTIC_ANCHOR,
    )
    rows = [anchored_but_empty, domestic_but_named]
    gauge = fc.frame_content_rows(
        rows,
        home_blobs={"country_watch_il": _IL},
        touched_signatures=[r["situation_signature"] for r in rows],
        evidence_mass={("country_watch_il", "military_posture", "palestine"): 12},
    )["country_watch_il"]
    assert gauge["anchored"] == 1
    assert gauge["domestic"] == 1
    assert gauge["contentless"] == 1  # the ANCHORED one
    assert gauge["distinct_anchors"] == 1
    assert gauge["evidence_mass_p50"] == 12.0


# ---------------------------------------------------------------------------
# The reserved anchor slot, and the domestic/retiring split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signature, expected",
    [
        ("sig:country_watch_il#dim:escalation", None),
        ("sig:country_watch_il#dim:escalation#evt:_domestic", None),
        ("sig:country_watch_il#dim:escalation#evt:palestine", "palestine"),
        ("sig:country_watch_il#dim:escalation#evt:", None),
        ("", None),
        (None, None),
    ],
)
def test_signature_anchor_reads_the_reserved_slot(signature, expected):
    """Flag-off, BOTH shapes mean the same thing — no slot at all (today) and
    an explicit ``_domestic`` slot (after migration 0193) are both "this frame
    is the dimension's residue"."""
    assert fc.signature_anchor(signature) == expected


def test_domestic_is_split_from_retiring():
    """Amendment §4, obligation 4: D-l's retirement ladder must not read as
    growth. A stored open frame that received NO members this run is on its
    way out through the unchanged status ladder; one that received members is
    a live domestic residue. Counting them together would make a register
    DRAINING look like a register the desks are still feeding."""
    fed = _frame("country_watch_il", "Israel – border buildup", dim="escalation")
    starved = _frame(
        "country_watch_il", "Israel’s energy posture unchanged",
        dim="energy_security",
    )
    gauge = fc.frame_content_rows(
        [fed, starved],
        home_blobs={"country_watch_il": _IL},
        touched_signatures=[fed["situation_signature"]],
    )["country_watch_il"]
    assert gauge["open"] == 2
    assert gauge["domestic"] == 1
    assert gauge["retiring"] == 1
    assert gauge["anchored"] == 0


# ---------------------------------------------------------------------------
# None, never zero
# ---------------------------------------------------------------------------


def test_none_not_zero_on_every_empty_denominator():
    """The design's own words: ``None`` on an empty denominator, never 0.0.

    A confident zero for "no data" is indistinguishable from a real zero, and
    the whole post-R4 comparison is a series of these numbers.
    """
    empty = fc.distribution([])
    assert empty["n"] == 0
    for key in ("min", "p25", "p50", "p75", "max"):
        assert empty[key] is None

    # Flag-off: no anchored frames anywhere => the p50 is absent, not zero.
    row = _frame("country_watch_il", "Israel’s posture unchanged")
    gauge = fc.frame_content_rows(
        [row], home_blobs={"country_watch_il": _IL},
        touched_signatures=[row["situation_signature"]],
    )["country_watch_il"]
    assert gauge["anchored"] == 0
    assert gauge["distinct_anchors"] == 0
    assert gauge["evidence_mass_p50"] is None
    assert gauge["contentless_rate"] == 1.0  # a real 1.0, not an absent one

    # An empty desk set: the churn ceilings have nothing to be a ceiling over.
    churn = fc.churn_rows({})
    assert churn["c1_open_frames_per_desk_mean"] is None
    assert churn["c2_open_frames_per_desk_max"] is None
    assert churn["c3_distinct_anchors_per_desk_max"] is None
    # C-4 and C-5 are None BY CONSTRUCTION flag-off, and say why in the row.
    assert churn["c4_anchor_survival_7d"] is None
    assert churn["c5_name_changes_per_frame_week"] is None
    assert churn["c4_reason"] and churn["c5_reason"]

    # No unlicensed pair => M-2's pooled rate has no denominator.
    census = fc.naming_census_rows((), frames=(), heads={})
    assert census["unlicensed_pooled_engagement"]["rate"] is None
    assert census["pairs"] == []


# ---------------------------------------------------------------------------
# The census (design §2.2) and the twin
# ---------------------------------------------------------------------------


def test_the_census_reproduces_the_design_pair_table_and_its_2x2():
    """The §2.2 table's own shape on fixture rows, at the live proportions.

    IL/Palestine: 0 naming frames, 0 engaging desks. IL/Lebanon: 1 naming
    frame, and the desks engage — the transition the design watched close in
    real time. The predictor's monotonicity (M-4) is the relation the 2×2
    exists to track, and the pooled unlicensed rate is M-2's number.
    """
    desk = "country_watch_il"
    frames = [
        _frame(desk, "Israel receives third Dolphin-class submarine"),
        _frame(desk, "Israel expands deployment in southern Lebanon",
               dim="escalation"),
        _frame(desk, "Israel’s coalition splinters", dim="internal_stability"),
    ]
    heads = {
        desk: [
            {"title": "Israel expands Lebanon deployment",
             "body": "Forces moved into southern Lebanon overnight."},
            {"title": "Coalition fracture deepens",
             "body": "The right-wing bloc pressed its demands."},
        ]
    }
    census = fc.naming_census_rows(
        [(desk, "Palestine"), (desk, "Lebanon")],
        frames=frames, heads=heads,
        naming_findings={(desk, "Palestine"): 44, (desk, "Lebanon"): 44},
    )
    by_polity = {r["polity"]: r for r in census["pairs"]}
    assert by_polity["Palestine"]["frames_naming"] == 0
    assert by_polity["Palestine"]["desks_engaging"] == 0
    assert by_polity["Palestine"]["open_frames"] == 3
    assert by_polity["Palestine"]["naming_findings"] == 44
    assert by_polity["Lebanon"]["frames_naming"] == 1
    assert by_polity["Lebanon"]["desks_engaging"] == 1
    assert census["licensed_engaged_2x2"] == {
        "licensed_engaged": 1,
        "licensed_unengaged": 0,
        "unlicensed_engaged": 0,
        "unlicensed_unengaged": 1,
    }
    # M-2 pools over the UNLICENSED pairs only: Palestine, 0 of 2 head slots.
    assert census["unlicensed_pooled_engagement"] == {
        "engaging": 0, "desk_slots": 2, "rate": 0.0,
    }


def test_the_mint_floor_twin_drops_thin_cells_and_wire_boilerplate():
    """Amendment §4, obligation 3 — the twin must be taken where the mint will
    be, or the post-flip p50 records a change of denominator as a change.

    Two families go: cells below the recurrence floor, and a polity so
    ubiquitous on its own desk that it is wire copy rather than a second story
    (live, the United States is named in 94-97% of every desk's bodies, which
    is exactly why D-t exists).
    """
    desk = "country_watch_il"
    by_dimension = {
        (desk, "escalation", "Palestine"): 12,
        (desk, "military_posture", "Palestine"): 7,
        (desk, "energy_security", "Lebanon"): 2,      # below the floor
        (desk, "escalation", "United States"): 26,    # boilerplate
    }
    by_desk = {(desk, "Palestine"): 19, (desk, "Lebanon"): 2,
               (desk, "United States"): 96}
    twin = fc.mint_floor_twin(
        by_dimension, by_desk=by_desk, desk_findings={desk: 100},
    )
    assert twin["n"] == 2
    assert twin["min"] == 7.0 and twin["max"] == 12.0
    assert twin["min_findings"] == 5 and twin["max_ubiquity"] == 0.75
    # ...and the unrestricted reading keeps all four, so the restriction is
    # visible rather than baked in.
    assert fc.distribution(by_dimension.values())["n"] == 4


# ---------------------------------------------------------------------------
# The interval cursor
# ---------------------------------------------------------------------------


def test_the_interval_cursor_spaces_the_heavy_read():
    """0 runs every tick (what the tests do); a positive interval runs once
    and then declines until it has elapsed."""
    fc.reset_cursor()
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    assert fc.due("k", now=now, interval_hours=0.0)
    assert fc.due("k", now=now, interval_hours=0.0)
    fc.reset_cursor()
    assert fc.due("k", now=now, interval_hours=6.0)
    assert not fc.due("k", now=now + timedelta(hours=5), interval_hours=6.0)
    assert fc.due("k", now=now + timedelta(hours=7), interval_hours=6.0)
    fc.reset_cursor()


def test_the_knobs_are_declared_handler_options():
    """A retune must not silently no-op — every ``FrameContentConfig`` field is
    declared against ``situation_clustering`` in the registry's option
    catalog, which is also what makes the registry rebuild on deploy real."""
    from dataclasses import fields as dc_fields

    from legba.data.analysts.handler_options import HANDLER_OPTIONS

    declared = {
        s.name for s in HANDLER_OPTIONS["situation_clustering"]
    }
    for f in dc_fields(fc.FrameContentConfig):
        assert fc.OPTION_PREFIX + f.name in declared, f.name


def test_an_option_wins_over_env_and_a_bad_value_keeps_its_predecessor(
    monkeypatch,
):
    monkeypatch.setenv(fc.ENV_PREFIX + "WINDOW_DAYS", "21")
    assert fc.FrameContentConfig.from_options(None).window_days == 21
    assert fc.FrameContentConfig.from_options(
        {fc.OPTION_PREFIX + "window_days": 7}
    ).window_days == 7
    # A mistyped knob degrades the instrument's resolution, never the run.
    assert fc.FrameContentConfig.from_options(
        {fc.OPTION_PREFIX + "window_days": "not-a-number"}
    ).window_days == 21


# ---------------------------------------------------------------------------
# THE FENCE — D-n, narrowed by amendment §2.4
# ---------------------------------------------------------------------------


def test_the_gauge_has_no_llm_and_reads_no_producer_self_description():
    """D-n's AST guard, in the D-6 fence idiom, narrowed by amendment §2.4.

    The LLM ban is unchanged: the whole point of the repair is that the frame
    stops being a model's label for itself, and a gauge that asked a model
    what a frame was about would have conceded the argument before measuring
    it. The five PRODUCER SELF-DESCRIPTION keys stay forbidden for the same
    reason — ``data.key_entities`` and its siblings are the producer's
    structured account of itself.

    ``title`` and ``body`` are NOT banned and this test says so out loud: they
    are the analyst's authored prose about the world, they are what the
    QUOTATION regime makes the product, and amendment §1.4 measures them as
    the only projection that discriminates at frame grain — a title-only
    anchor is a no-op on R-1's founding case, and the signal-entity walk the
    original D-n presumed is desk-uniform by construction.
    """
    tree = ast.parse(inspect.getsource(fc))
    imported: set[str] = set()
    consts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(a.asname or a.name.split(".")[0] for a in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.update(node.module.split("."))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            consts.append(node.value)
    for banned in (
        "llm", "dspy", "litellm", "openai", "anthropic", "actor_critic",
        "prompts", "prompt_registry", "verify", "judge",
    ):
        assert banned not in imported, (
            f"the frame-content gauge imports {banned!r} — it counts and "
            f"names, and a model has no part in either"
        )
    for banned in ("key_entities", "entities", "actors", "locations"):
        assert banned not in consts, (
            f"the gauge names {banned!r} — that is the producer's structured "
            f"self-description, which D-n forbids the mint path to read"
        )
    # ``geo`` appears EXACTLY once, and it is the DESK DESCRIPTOR's scope geo
    # (the home-country exclusion's input), never a finding's ``data.geo``.
    assert consts.count("geo") == 1

    # THE STRUCTURAL HALF, and the stronger one: the finding projection IS
    # the fence. The gauge cannot read a self-description key it never
    # selects, whatever any future edit above this line says.
    projected = fc._FINDINGS_SQL.lower()
    assert "data" not in projected
    for column in ("target_id", "analyst_id", "produced_at", "title", "body"):
        assert column in projected

    # And title/body are affirmatively PRESENT — the narrowing is the point.
    assert "title" in projected and "body" in projected


def test_the_gauges_import_direction_is_acyclic():
    """The module imports UPWARD into ``deterministic_handlers`` (one stoplist,
    one bar, one signature grammar — never a second copy of any of them), and
    the direction must hold from a cold interpreter in BOTH orders."""
    for first in (
        "import legba.data._frame_content",
        "import legba.data.analysts.deterministic",
    ):
        proc = subprocess.run(
            [sys.executable, "-c",
             f"{first}; "
             "import legba.data._frame_content as m; "
             "import legba.data.analysts.deterministic as d; "
             "assert 'situation_clustering' in d.SUB_HANDLERS; "
             "print(m.RENDER_CAP)"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "6"


def test_the_gauge_row_is_an_instrument_and_is_verify_exempt():
    """The gauge rides ``situation_clustering``'s receipt, and that analyst is
    already registered structural/verify-exempt — so the row is an INSTRUMENT,
    not a claim, and no kinds-vocabulary change is needed to make it one. It
    is asserted rather than assumed because the exemption is what keeps this
    design out of the ~84% of LLM calls that are the system watching itself.
    """
    assert sc.SUB_HANDLER_NAME in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


def test_the_receipt_is_byte_identical_on_every_pre_gauge_field():
    """ADDITIVE, pinned as bytes. The gauge contributes exactly three keys and
    changes none of the four the receipt already carried."""
    clusters = [
        {"situation_signature": "sig:country_watch_il#dim:escalation",
         "event_count": 3, "action": "updated"},
    ]
    before = sc._build_finding(
        created=1, updated=2, clusters=clusters, target_id="country_watch_il",
    )
    after = sc._build_finding(
        created=1, updated=2, clusters=clusters, target_id="country_watch_il",
        gauge={"frame_content_gauge": {"open": 1}, "naming_census": {},
               "churn": {}},
    )
    pre_gauge = ("sub_handler", "situations_created", "situations_updated",
                 "clusters")
    assert json.dumps({k: before.data[k] for k in pre_gauge}) == json.dumps(
        {k: after.data[k] for k in pre_gauge}
    )
    assert before.title == after.title and before.body == after.body
    assert set(after.data) - set(before.data) == {
        "frame_content_gauge", "naming_census", "churn"
    }


# ---------------------------------------------------------------------------
# THE REAL BINDING PATH — ephemeral / pivot DB
# ---------------------------------------------------------------------------

_DESK_PREFIX = "country_watch_fcg_"


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def desk(pool):
    """This lane's own desk + analyst id.

    The clusterer is a fleet-wide sweep on a substrate the whole suite shares,
    so every assertion below is scoped to THIS desk's row inside the gauge —
    never to a fleet total. Teardown CLOSES rather than deletes (the ledger and
    ``hypotheses`` both reference ``situations``) and removes only this desk's
    own descriptor.
    """
    target = f"{_DESK_PREFIX}{uuid4().hex[:8]}"
    analyst_id = f"situation_clustering_{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO target_descriptors "
            "  (descriptor_id, version, schema_uri, is_head, state, owner, "
            "   name, body) "
            "VALUES ($1, 'v1', 'legba/target/2.0.0', TRUE, 'active', "
            "        'test_r1d', $1, $2::jsonb)",
            target,
            json.dumps({"scope": {"geo": ["IL"], "tags": ["watch"]}}),
        )
    fc.reset_cursor()
    yield pool, target, analyst_id
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE situations SET status = 'closed' WHERE analyst_id = $1",
            analyst_id,
        )
        await conn.execute(
            "DELETE FROM target_descriptors WHERE descriptor_id = $1", target
        )
    fc.reset_cursor()


class _Deps:
    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool


async def _run(pool: Any, analyst_id: str, **opts: Any) -> AnalystMethodResult:
    """THE REAL BINDING PATH — the deterministic dispatcher resolves
    ``options['sub_handler']`` exactly as the runtime does."""
    return await deterministic.run_method(
        [],
        {
            "sub_handler": "situation_clustering",
            "analyst_id": analyst_id,
            "run_id": str(uuid4()),
            **opts,
        },
        _Deps(pool),
    )


async def _seed(conn: Any, *, target: str, analyst_id: str) -> None:
    """Two dimensions' worth of stamped findings, so the clusterer opens two
    frames and both are TOUCHED this run; plus one stored open frame the
    findings do NOT feed, which must land in ``retiring``."""
    for dim, title in (
        ("escalation",
         "Israel expands deployment in southern Lebanon by seizing a ridge"),
        ("military_posture", "Israel’s military posture unchanged this week"),
    ):
        for hours in (2.0, 5.0):
            await conn.execute(
                "INSERT INTO analyst_outputs "
                "  (id, kind, title, body, confidence, data, analyst_id, "
                "   target_id, produced_at, schema_uri, situation_signature, "
                "   severity) "
                "VALUES ($1, 'finding', $2, $3, 0.9, '{}'::jsonb, $4, $5, $6, "
                "        'iglu:legba/finding/jsonschema/1-0-0', $7, "
                "        'moderate')",
                uuid4(), title,
                "Lebanon and Palestine both appear in this desk's prose.",
                f"{analyst_id}_{dim}", target,
                datetime.now(timezone.utc) - timedelta(hours=hours),
                f"sig:{target}#dim:{dim}",
            )
    await conn.execute(
        "INSERT INTO situations "
        "  (id, data, name, status, category, target_id, analyst_id, "
        "   intensity_score, event_count, valid_from, situation_signature) "
        "VALUES ($1, $2::jsonb, $3, 'dormant', 'country', $4, $5, 0.2, 1, "
        "        now(), $6)",
        uuid4(), json.dumps({"dimension": "energy_security"}),
        "Israel’s energy-security pressure stays moderate", target,
        analyst_id, f"sig:{target}#dim:energy_security",
    )


@pytest.mark.integration
async def test_the_gauge_publishes_flag_off_through_the_real_handler_path(desk):
    """R1-d's own acceptance: the gauge RUNS AND PUBLISHES with the flag OFF.

    That is not a concession, it is the requirement — it must measure the
    baseline it will later be judged against, and ``LEGBA_REGISTER_CONTENTFUL
    _FRAMES`` does not exist yet. So the flag-off shape is asserted exactly:
    ``anchored: 0``, ``distinct_anchors: 0``, ``evidence_mass_p50: None``,
    every open frame domestic-or-retiring, and the guard's two numbers carried
    side by side rather than recomputed.
    """
    pool, target, analyst_id = desk
    async with pool.acquire() as conn:
        await _seed(conn, target=target, analyst_id=analyst_id)

    result = await _run(
        pool, analyst_id, frame_content_gauge_min_interval_hours=0.0
    )
    data = result.finding.data
    assert set(data) >= {"frame_content_gauge", "naming_census", "churn"}
    gauge = data["frame_content_gauge"]
    assert gauge["register_flag"]["env"] == "LEGBA_REGISTER_CONTENTFUL_FRAMES"
    assert gauge["register_flag"]["present"] is False

    row = gauge["desks"][target]
    assert row["open"] == 3
    assert row["anchored"] == 0
    assert row["distinct_anchors"] == 0
    assert row["evidence_mass_p50"] is None
    assert row["domestic"] == 2          # the two the findings fed
    assert row["retiring"] == 1          # the stored frame they did not
    assert row["contentless"] >= 1       # "military posture unchanged"
    assert row["rendered"] == row["open"]  # below the render cap
    assert row["contentless_rate"] is not None

    # The guard rides the gauge, carried from the detector's own receipt —
    # None (never 0) on a substrate with no recent real scan.
    guard = gauge["guard"]
    assert set(guard) == {"source", "as_of", "breaches", "breaches_naming_only"}
    assert guard["breaches"] is None or isinstance(guard["breaches"], int)

    churn = data["churn"]
    assert churn["c6_render_cap"] == fc.RENDER_CAP == 6
    assert churn["c4_anchor_survival_7d"] is None
    assert churn["c1_open_frames_per_desk_mean"] is not None

    census = data["naming_census"]
    assert set(census) == {
        "pairs", "licensed_engaged_2x2", "unlicensed_pooled_engagement"
    }
    assert sum(census["licensed_engaged_2x2"].values()) == len(census["pairs"])


@pytest.mark.integration
async def test_the_interval_gate_declines_and_leaves_the_receipt_pre_gauge(
    desk,
):
    """The gauge is SPACED, and a declined tick publishes nothing rather than
    a stale or partial row — while the four pre-gauge receipt fields are
    exactly as they always were. This is the byte-identity claim on the real
    path: whatever the gauge does, the clusterer's own receipt does not
    notice."""
    pool, target, analyst_id = desk
    async with pool.acquire() as conn:
        await _seed(conn, target=target, analyst_id=analyst_id)

    first = await _run(
        pool, analyst_id, frame_content_gauge_min_interval_hours=6.0
    )
    second = await _run(
        pool, analyst_id, frame_content_gauge_min_interval_hours=6.0
    )
    assert "frame_content_gauge" in first.finding.data
    assert "frame_content_gauge" not in second.finding.data
    assert "naming_census" not in second.finding.data
    assert "churn" not in second.finding.data
    for receipt in (first, second):
        assert set(receipt.finding.data) >= {
            "sub_handler", "situations_created", "situations_updated",
            "clusters",
        }
        assert receipt.finding.data["sub_handler"] == "situation_clustering"


@pytest.mark.integration
async def test_a_failing_gauge_never_costs_the_clustering_run(desk, monkeypatch):
    """An instrument may not cost the thing it measures. If the gauge raises,
    the clusterer still materializes its frames and still returns its receipt
    — without the three keys, which is the honest degradation."""
    pool, target, analyst_id = desk
    async with pool.acquire() as conn:
        await _seed(conn, target=target, analyst_id=analyst_id)

    async def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("the aggregate fell over")

    monkeypatch.setattr(fc, "collect", _boom)
    result = await _run(
        pool, analyst_id, frame_content_gauge_min_interval_hours=0.0
    )
    assert "frame_content_gauge" not in result.finding.data
    assert result.finding.data["sub_handler"] == "situation_clustering"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT situation_signature FROM situations "
            " WHERE analyst_id = $1 AND target_id = $2",
            analyst_id, target,
        )
    assert len(rows) >= 2
