# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2.2 — the cluster-first journal window: grouping, ranking, fill, header,
roster, and the flag-off byte-identity proof.

The byte-identity layer mirrors ``test_journal_slice_extraction.py``'s shape: the
PRE-T2.2 ``journal_assessor.py`` is loaded via ``importlib`` from
``git show <BASE_SHA>:...`` written to a temp file (never ``git stash`` on a
shared repo), with ``__package__`` pinned so its relative imports resolve against
the real, currently-checked-out siblings. That last detail is what makes the
proof worth having: the old renderer's ``_labeled_journal_slice`` is THIS tree's,
so the comparison exercises the live composed path (``journal_window`` ->
``_select_journal_slice``) rather than a frozen copy of it.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from legba.data.analysts import journal_assessor as ja
from legba.data.analysts import journal_clusters as jc
from legba.data.analysts import journal_slice as js

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The worktree's pinned base — journal_assessor.py immediately BEFORE T2.2.
BASE_SHA = "f42d769ad538f8fe819669257b8a1737a2ec01d2"
OLD_REL_PATH = "src/legba/data/analysts/journal_assessor.py"

TIERS = ("entry", "chronicle", "lens", "lens_diff")


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    """Every test starts with the flag OFF; the flag-on tests set it explicitly."""
    monkeypatch.delenv(jc.CLUSTER_FIRST_ENV, raising=False)
    yield


def _row(
    sid: str,
    title: str,
    *,
    source: str = "source.a",
    geo: tuple[str, ...] = (),
    mag: float | None = 0.5,
    when: str = "2026-09-08T12:00:00+00:00",
    authority: str = "reporting",
) -> dict:
    row: dict = {
        "id": sid, "source_id": source, "title": title, "geo": list(geo),
        "produced_at": when, "data": {"title": title},
    }
    if mag is not None:
        row["salience"] = {
            "magnitude": mag, "authority": authority, "event_class": "other",
        }
    return row


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_two_shared_anchors_group_and_a_third_polity_does_not_split_them():
    rows = [
        _row("a", "UK to announce trade ban on goods from Israeli settlements",
             source="source.a"),
        _row("b", "US lawmaker lashes out at UK over Israeli settlements ban",
             source="source.b"),
        _row("c", "Britain will ban goods from Israeli settlements",
             source="source.c"),
        # Same words, different polities — must NOT join.
        _row("d", "Russia advocates a long-term settlement of the Ukraine war",
             source="source.d"),
    ]
    clusters = jc.cluster_pool(rows)
    by_key = {c.key: c for c in clusters}
    assert "anchors|Israel|United Kingdom" in by_key
    settlements = by_key["anchors|Israel|United Kingdom"]
    assert {r["id"] for r in settlements.members} == {"a", "b", "c"}
    # "settlement" the homonym does not join the story: row d is a Russia/Ukraine
    # single, so it clusters with nothing and stays in the magnitude tail.
    assert all("d" not in {r["id"] for r in c.members} for c in clusters)


def test_a_three_polity_row_joins_the_pair_the_pool_supports_most():
    """A row naming three polities joins the biggest story it belongs to."""
    rows = [
        _row(f"uk{i}", f"UK trade ban on Israeli settlements, report {i}",
             source=f"source.uk{i}")
        for i in range(5)
    ] + [
        _row("x", "US lawmaker attacks UK plan to sanction Israeli settlements",
             source="source.x"),
        # A US/UK pair the pool barely supports must not win over UK/Israel.
        _row("y", "US and UK sign an unrelated fisheries memorandum",
             source="source.y"),
    ]
    by_key = {c.key: c for c in jc.cluster_pool(rows)}
    assert "x" in {r["id"] for r in by_key["anchors|Israel|United Kingdom"].members}


def test_one_anchor_plus_geo_needs_a_shared_term_and_gets_one():
    shared = [
        _row("p1", "Ilya Espino takes office as administrator of the Panama Canal",
             geo=("PA",), source="source.a"),
        _row("p2", "New Panama Canal chief takes office with expansion in focus",
             geo=("PA",), source="source.b"),
    ]
    cluster = {c.key: c for c in jc.cluster_pool(shared)}["geo|Panama|PA"]
    assert cluster.terms and "canal" in cluster.terms
    assert cluster.geo == "PA"


def test_one_anchor_plus_geo_with_no_shared_term_is_NOT_a_cluster():
    """"Same country, same geo tag" is a desk, not a story."""
    rows = [
        _row("i1", "Israeli parties submit candidate lists before the deadline",
             geo=("IL",), source="source.a"),
        _row("i2", "Trains run on a special Rosh Hashanah schedule", geo=("IL",),
             source="source.b"),
        _row("i3", "Israel sees a 58% drop in returning citizens", geo=("IL",),
             source="source.c"),
    ]
    assert [c.key for c in jc.cluster_pool(rows)] == []


def test_a_geo_cluster_drops_a_member_that_shares_none_of_its_terms():
    rows = [
        _row("g1", "POLICE: coerce in Berkeley Vale, New South Wales, Australia",
             geo=("AU",), source="source.gdelt"),
        _row("g2", "POLICE: coerce in Brisbane, Queensland, Australia",
             geo=("AU",), source="source.gdelt"),
        _row("g3", "Returning Rams superstar to miss MCG NFL clash in Australia",
             geo=("AU",), source="source.abc"),
    ]
    cluster = {c.key: c for c in jc.cluster_pool(rows)}["geo|Australia|AU"]
    assert {r["id"] for r in cluster.members} == {"g1", "g2"}


def test_different_geo_tags_keep_one_anchor_rows_apart():
    rows = [
        _row("a1", "Wildfire notification issued in Australia", geo=("AU",)),
        _row("a2", "Wildfire notification issued in Australia", geo=("AU",),
             source="source.b"),
        _row("b1", "Wildfire notification issued in Brazil", geo=("BR",)),
        _row("b2", "Wildfire notification issued in Brazil", geo=("BR",),
             source="source.b"),
    ]
    keys = {c.key for c in jc.cluster_pool(rows)}
    assert "geo|Australia|AU" in keys and "geo|Brazil|BR" in keys


def test_a_row_naming_no_polity_is_never_clustered():
    rows = [_row("n1", "Markets drift lower on thin volume"),
            _row("n2", "Markets drift lower on thin volume", source="source.b")]
    assert jc.cluster_pool(rows) == []


def test_an_over_ubiquitous_anchor_is_dropped_from_the_key():
    """A polity naming >= the ceiling share of the pool is a fact about the
    pool, not about a story — so it cannot be half of a key."""
    rows = [
        _row(f"us{i}", f"United States budget note {i} from Washington",
             geo=("US",), source=f"source.{i}")
        for i in range(54)
    ] + [
        _row(f"c{i}", f"United States and Canada lumber dispute talks {i}",
             geo=("CA",), source=f"source.c{i}")
        for i in range(6)
    ]
    # United States is in 60/60 rows (>= both the 20% share and the 12-row
    # floor) -> never a key half. Canada is in 6 -> under both -> still keys.
    clusters = jc.cluster_pool(rows)
    assert clusters, "the non-ubiquitous anchor must still cluster"
    for cluster in clusters:
        assert "United States" not in cluster.anchors


def test_titleless_rows_are_neither_members_nor_leads():
    rows = [
        _row("t1", "Panama Canal chief takes office", geo=("PA",)),
        _row("t2", "Panama Canal expansion projects in focus", geo=("PA",),
             source="source.b"),
        {"id": "t3", "source_id": "source.c", "geo": ["PA"],
         "data": {"text": "Panama Canal something"}, "title": None,
         "produced_at": "2026-09-08T12:00:00+00:00"},
    ]
    cluster = jc.cluster_pool(rows)[0]
    assert {r["id"] for r in cluster.members} == {"t1", "t2"}


def test_grouping_is_order_independent():
    rows = [
        _row("a", "UK trade ban on Israeli settlements", mag=0.9),
        _row("b", "Israel condemns the UK settlements ban", source="source.b",
             mag=0.8),
        _row("c", "Britain confirms the Israeli settlements ban",
             source="source.c", mag=0.7),
        _row("d", "Panama Canal chief takes office", geo=("PA",), mag=0.6),
        _row("e", "Panama Canal expansion in focus", geo=("PA",),
             source="source.b", mag=0.5),
    ]
    forward = [(c.key, [r["id"] for r in c.members]) for c in jc.cluster_pool(rows)]
    backward = [
        (c.key, [r["id"] for r in c.members])
        for c in jc.cluster_pool(list(reversed(rows)))
    ]
    assert forward == backward


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_mass_is_members_times_distinct_sources():
    rows = [
        _row("a", "UK trade ban on Israeli settlements", source="source.a"),
        _row("b", "Israel rejects the UK settlements ban", source="source.a"),
        _row("c", "Britain confirms the Israeli settlements ban",
             source="source.b"),
    ]
    cluster = jc.cluster_pool(rows)[0]
    assert (len(cluster.members), cluster.sources, cluster.mass) == (3, 2, 6)


def test_ranking_is_mass_then_max_magnitude_then_key():
    """Two clusters of equal mass rank by their highest member magnitude."""
    rows = [
        _row("p1", "Panama Canal chief takes office", geo=("PA",), mag=0.4),
        _row("p2", "Panama Canal expansion in focus", geo=("PA",),
             source="source.b", mag=0.3),
        _row("k1", "Kenya drought response scales up in Kenya", geo=("KE",),
             mag=0.9),
        _row("k2", "Kenya drought response reaches new counties", geo=("KE",),
             source="source.b", mag=0.2),
    ]
    ranked = jc.cluster_pool(rows)
    assert [c.mass for c in ranked] == [4, 4]
    assert ranked[0].key == "geo|Kenya|KE"


# ---------------------------------------------------------------------------
# Window fill
# ---------------------------------------------------------------------------


def _big_pool() -> list[dict]:
    """Eight clusters of six rows each + 40 unrelated scored singles."""
    pool: list[dict] = []
    stories = [
        ("Panama", "PA", "Panama Canal traffic"),
        ("Kenya", "KE", "Kenya drought response"),
        ("Norway", "NO", "Norway sovereign fund"),
        ("Chile", "CL", "Chile copper strike"),
        ("Peru", "PE", "Peru highway blockade"),
        ("Ghana", "GH", "Ghana cocoa harvest"),
        ("Nepal", "NP", "Nepal monsoon flooding"),
        ("Fiji", "FJ", "Fiji cyclone warning"),
    ]
    for n, (polity, geo, phrase) in enumerate(stories):
        for i in range(6):
            pool.append(_row(
                f"{polity}-{i}", f"{phrase} update {i} in {polity}",
                source=f"source.{polity}.{i}", geo=(geo,),
                mag=0.6 - n * 0.01 - i * 0.001,
            ))
    for i in range(40):
        pool.append(_row(f"single-{i}", f"Unrelated market note {i}",
                         source=f"source.single{i}", mag=0.9 - i * 0.001))
    return pool


def test_window_fill_leads_with_N_clusters_of_lead_plus_K(monkeypatch):
    monkeypatch.setenv(jc.CLUSTER_FIRST_ENV, "1")
    pool = _big_pool()
    window = jc.journal_window(pool)
    assert len(window) == js._JOURNAL_RENDER_CAP
    headers = [r for r in window if r.get(jc.CLUSTER_HEADER_KEY)]
    assert len(headers) == jc._JOURNAL_CLUSTER_N
    block = window[: jc._JOURNAL_CLUSTER_N * (jc._JOURNAL_CLUSTER_K + 1)]
    assert len(block) == 24
    # The block's first row is each cluster's lead, every (K+1)th row.
    for i in range(jc._JOURNAL_CLUSTER_N):
        assert block[i * (jc._JOURNAL_CLUSTER_K + 1)].get(jc.CLUSTER_HEADER_KEY)


def test_cluster_members_are_source_diverse(monkeypatch):
    monkeypatch.setenv(jc.CLUSTER_FIRST_ENV, "1")
    rows = [
        _row("a", "Panama Canal traffic rises", geo=("PA",), source="source.a",
             mag=0.9),
        _row("b", "Panama Canal traffic dips", geo=("PA",), source="source.a",
             mag=0.8),
        _row("c", "Panama Canal traffic steady", geo=("PA",), source="source.b",
             mag=0.7),
        _row("d", "Panama Canal traffic record", geo=("PA",), source="source.c",
             mag=0.6),
    ]
    picked = jc._cluster_rows(jc.cluster_pool(rows)[0], 2)
    assert [r["id"] for r in picked] == ["a", "c", "d"]


def test_a_single_source_cluster_still_contributes_k_members():
    rows = [
        _row(f"s{i}", f"Panama Canal traffic note {i}", geo=("PA",),
             source="source.a", mag=0.9 - i * 0.01)
        for i in range(5)
    ]
    picked = jc._cluster_rows(jc.cluster_pool(rows)[0], 3)
    assert [r["id"] for r in picked] == ["s0", "s1", "s2", "s3"]


def test_the_tail_is_the_existing_magnitude_order(monkeypatch):
    monkeypatch.setenv(jc.CLUSTER_FIRST_ENV, "1")
    pool = _big_pool()
    window = jc.journal_window(pool)
    taken = {id(r) for r in window[:24]}
    tail = window[24:]
    remainder = [r for r in pool if id(r) not in taken]
    assert tail == jc._fill_remainder(remainder, js._JOURNAL_RENDER_CAP - 24)


def test_the_unscored_freshness_floor_keeps_its_full_reserve(monkeypatch):
    monkeypatch.setenv(jc.CLUSTER_FIRST_ENV, "1")
    pool = _big_pool()
    fresh = [
        _row(f"breaking-{i}", f"Breaking wire {i}", source=f"source.br{i}",
             mag=None, when=f"2026-09-09T2{i}:00:00+00:00")
        for i in range(5)
    ]
    window = jc.journal_window(pool + fresh)
    ids = {r["id"] for r in window}
    assert {r["id"] for r in fresh} <= ids


def test_flag_on_window_is_still_capped_and_has_no_duplicates(monkeypatch):
    monkeypatch.setenv(jc.CLUSTER_FIRST_ENV, "1")
    window = jc.journal_window(_big_pool())
    ids = [r["id"] for r in window]
    assert len(ids) == len(set(ids)) == js._JOURNAL_RENDER_CAP


def test_a_stale_header_never_survives_a_second_selection(monkeypatch):
    monkeypatch.setenv(jc.CLUSTER_FIRST_ENV, "1")
    pool = _big_pool()
    jc.journal_window(pool)
    for row in pool:
        row[jc.CLUSTER_HEADER_KEY] = "▸ stale"
    window = jc.journal_window(pool)
    assert not any(
        r.get(jc.CLUSTER_HEADER_KEY) == "▸ stale" for r in window
    )


# ---------------------------------------------------------------------------
# Header render
# ---------------------------------------------------------------------------


def test_header_shape_and_placement(monkeypatch):
    monkeypatch.setenv(jc.CLUSTER_FIRST_ENV, "1")
    rows = [
        _row("a", "Panama Canal chief takes office", geo=("PA",),
             source="source.a", mag=0.9),
        _row("b", "Panama Canal expansion projects in focus", geo=("PA",),
             source="source.b", mag=0.8),
    ]
    prompt = ja._render_user_prompt(rows, tier="entry")
    lines = prompt.splitlines()
    header_at = [i for i, ln in enumerate(lines) if ln.startswith("▸")]
    assert len(header_at) == 1
    header = lines[header_at[0]]
    assert header == (
        "▸ Panama · geo PA · canal — 2 rows · 2 sources · "
        "lead: Panama Canal chief takes office"
    )
    # The header sits immediately above its lead row, and the lead is citable.
    assert lines[header_at[0] + 1].startswith("- ")
    assert "[[ref:a]]" in lines[header_at[0] + 1]
    assert jc.CLUSTER_PREAMBLE in prompt


def test_no_header_and_no_preamble_when_the_flag_is_off():
    rows = [
        _row("a", "Panama Canal chief takes office", geo=("PA",), mag=0.9),
        _row("b", "Panama Canal expansion projects in focus", geo=("PA",),
             source="source.b", mag=0.8),
    ]
    prompt = ja._render_user_prompt(rows, tier="entry")
    assert "▸" not in prompt
    assert jc.CLUSTER_PREAMBLE not in prompt


def test_instrument_and_routine_labels_ride_through_unchanged(monkeypatch):
    monkeypatch.setenv(jc.CLUSTER_FIRST_ENV, "1")
    rows = [
        _row("g1", "POLICE: coerce in Berkeley Vale, New South Wales, Australia",
             geo=("AU",), source="source.gdelt.files", mag=0.3),
        _row("g2", "POLICE: coerce in Brisbane, Queensland, Australia",
             geo=("AU",), source="source.gdelt.files", mag=0.2),
        _row("r1", "Flood warning issued for the Ohio valley",
             source="source.nws.active_alerts", mag=0.4),
    ]
    prompt = ja._render_user_prompt(rows, tier="entry")
    assert "[instrument]" in prompt
    assert "[routine]" in prompt


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


_WORLD_BODY = """# World read

## Tension

Something.

## Coverage

- G20 — United Kingdom (country_g20_gb): in basis, 0.5h old
- Watch — Sudan (country_watch_sd): in basis, 0.4h old
- escalation_composition: in basis, 3.5h old

## Not carried

- rank 9: country_composition / Watch — Congo (country_watch_cd) — cited mass 2.88 — shown_not_selected

## The record

Prose.
"""


def test_roster_block_counts_basis_and_drops():
    block = jc.coverage_roster_block(_WORLD_BODY)
    assert "in basis (3): country_g20_gb, country_watch_sd, escalation_composition" in block
    assert "not carried (1)" in block
    assert "country_watch_cd" in block


def test_roster_block_is_never_citable():
    assert "[[ref:" not in jc.coverage_roster_block(_WORLD_BODY)
    assert "CONTEXT ONLY" in jc.coverage_roster_block(_WORLD_BODY)


def test_roster_block_is_empty_for_a_body_with_no_roster():
    assert jc.coverage_roster_block("# World read\n\n## The record\n\nProse.") == ""
    assert jc.coverage_roster_block("") == ""


def test_roster_rides_the_entry_prompt_and_only_the_entry_prompt():
    rows = [_row("a", "Panama Canal chief takes office", geo=("PA",))]
    block = jc.coverage_roster_block(_WORLD_BODY)
    assert block in ja._render_user_prompt(rows, tier="entry", coverage_roster=block)
    for tier in ("chronicle", "lens", "lens_diff"):
        rendered = ja._render_user_prompt(rows, tier=tier, coverage_roster=block)
        assert "Desks today" not in rendered


# ---------------------------------------------------------------------------
# Flag-off byte identity
# ---------------------------------------------------------------------------


def _load_old_module(tmp_path: Path):
    if shutil.which("git") is None:
        # The nightly rig runs in the legba-test image, which ships no git —
        # the base-commit identity proof is host-only; the fixture-based
        # identity tests in this file still run there (2026-09-11).
        pytest.skip("git is not available on this rig; base-commit identity proof is host-only")
    old_source = subprocess.run(
        ["git", "show", f"{BASE_SHA}:{OLD_REL_PATH}"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    old_path = tmp_path / "journal_assessor_pre_t22.py"
    old_path.write_text(old_source, encoding="utf-8")
    mod_name = "legba.data.analysts._journal_assessor_pre_t22_test"
    spec = importlib.util.spec_from_file_location(mod_name, old_path)
    assert spec is not None and spec.loader is not None
    old_module = importlib.util.module_from_spec(spec)
    old_module.__package__ = "legba.data.analysts"
    sys.modules[mod_name] = old_module
    try:
        spec.loader.exec_module(old_module)
    finally:
        sys.modules.pop(mod_name, None)
    return old_module


SYNTHETIC_POOLS = {
    # 1: nothing scored -> the delivered order, plain [:cap] cut.
    "nothing_scored": [
        _row(f"n{i}", f"Unscored wire {i} about Panama and Kenya",
             source=f"source.{i % 7}", geo=("PA",), mag=None,
             when=f"2026-09-0{1 + i % 8}T0{i % 10}:00:00+00:00")
        for i in range(80)
    ],
    # 2: everything scored, under the cap.
    "under_cap_scored": [
        _row("khamenei", "Iran leader dies in Tehran", mag=0.95,
             authority="state_media", geo=("IR",)),
        _row("graham", "Senator speaks about the UK", mag=0.30, geo=("US",)),
        _row("official", "Israeli strike hits southern Lebanon", mag=0.9,
             authority="official", geo=("LB",)),
    ],
    # 3: more than the cap scored + a fresh unscored breaking row.
    "fresh_floor": (
        [_row(f"r{i}", f"Routine Panama Canal note {i}", mag=0.2,
              source=f"source.{i % 5}", geo=("PA",),
              when="2026-09-01T00:00:00+00:00") for i in range(65)]
        + [_row("breaking", "Breaking: Kenya drought emergency declared",
                mag=None, geo=("KE",), when="2026-09-09T23:00:00+00:00")]
    ),
}


@pytest.mark.parametrize("pool_name", sorted(SYNTHETIC_POOLS))
def test_flag_off_selection_is_the_pre_t22_function(pool_name):
    rows = [dict(r) for r in SYNTHETIC_POOLS[pool_name]]
    assert jc.journal_window(rows) == js._select_journal_slice(rows)


@pytest.mark.parametrize("pool_name", sorted(SYNTHETIC_POOLS))
@pytest.mark.parametrize("tier", TIERS)
def test_flag_off_render_is_byte_identical_to_the_base_commit(
    pool_name, tier, tmp_path
):
    old = _load_old_module(tmp_path)
    rows = [dict(r) for r in SYNTHETIC_POOLS[pool_name]]
    old_rendered = old._render_user_prompt(rows, tier=tier)
    new_rendered = ja._render_user_prompt(rows, tier=tier)
    assert new_rendered == old_rendered


# --- the live 24h pool, read-only -----------------------------------------

_PG_HOST = os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1")
_PG_PORT = int(os.environ.get("LEGBA_DATA_PG_PORT", "5432"))
_PG_USER = os.environ.get("LEGBA_DATA_PG_USER", "legba")
_PG_PASSWORD = os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba")
_PG_DB = os.environ.get("LEGBA_DATA_PG_DB", "legba")


def _live_pg_port_open() -> bool:
    try:
        with socket.create_connection((_PG_HOST, _PG_PORT), timeout=1.5):
            return True
    except OSError:
        return False


@pytest.mark.skipif(
    not _live_pg_port_open(),
    reason=f"live legba Postgres not reachable at {_PG_HOST}:{_PG_PORT}",
)
async def test_flag_off_is_byte_identical_on_the_live_24h_pool(tmp_path) -> None:
    import asyncpg

    dsn = (
        f"postgresql://{_PG_USER}:{_PG_PASSWORD}@{_PG_HOST}:{_PG_PORT}/{_PG_DB}"
    )
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"could not connect to live signals db: {exc!r}")
        return
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        records = await conn.fetch(
            "SELECT id::text, source_id, geo, "
            "coalesce(payload->>'title', payload->>'headline', '') AS title, "
            "salience, fetched_at AS produced_at FROM signals "
            "WHERE fetched_at >= $1 ORDER BY fetched_at DESC LIMIT 300",
            since,
        )
    finally:
        await conn.close()
    if not records:
        pytest.skip("live signals table has no rows in the last 24h to check")

    import json as _json

    rows = []
    for r in records:
        sal = r["salience"]
        if isinstance(sal, str):
            sal = _json.loads(sal)
        rows.append({
            "id": r["id"], "source_id": r["source_id"], "title": r["title"],
            "geo": list(r["geo"] or []), "salience": sal,
            "data": {"title": r["title"]},
            "produced_at": r["produced_at"].isoformat(),
        })

    assert jc.journal_window([dict(r) for r in rows]) == js._select_journal_slice(
        [dict(r) for r in rows]
    )
    old = _load_old_module(tmp_path)
    for tier in TIERS:
        assert ja._render_user_prompt([dict(r) for r in rows], tier=tier) == (
            old._render_user_prompt([dict(r) for r in rows], tier=tier)
        )
