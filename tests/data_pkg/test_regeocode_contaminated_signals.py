# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-2 historical re-geocode backfill (W-2b).

Exercises ``scripts/regeocode_contaminated_signals.py``:

  * PURE selection/re-derivation (``assess_row``) — matches the two S-2
    contamination signatures (publisher-origin CNA→SG, incidental-dateline
    Yonhap→BR) and does NOT match clean rows: corroborated origin tags,
    geocoder-derived tags, subject datelines, and rows the offline path cannot
    decide (skipped, never guessed);
  * DB-backed — dry-run writes nothing; --apply fixes before/after geo,
    rewrites payload.geo honestly for the dateline class, NULLs the corpus
    dirty-marker (``indexed_at``) in the same statement, and is idempotent.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "regeocode_contaminated_signals.py"
)
_spec = importlib.util.spec_from_file_location("regeocode_contaminated_signals", _SCRIPT)
regeo = importlib.util.module_from_spec(_spec)
# Register BEFORE exec: @dataclass resolves cls.__module__ via sys.modules.
sys.modules[_spec.name] = regeo
_spec.loader.exec_module(regeo)


# ---------------------------------------------------------------------------
# Unit — assess_row selection + re-derivation (no DB)
# ---------------------------------------------------------------------------


def test_cna_world_story_uncorroborated_origin_cleared():
    """The S-2 headline case: a Singapore wire's LeBron story landed geo=SG.
    Content names no country ⇒ the CURRENT gate clears the tag entirely."""
    payload = {
        "publisher_origin": ["SG"],
        "title": "LeBron James breaks the all-time scoring record",
        "raw_body": "A big night in the league.",
    }
    d = regeo.assess_row(payload, ["SG"])
    assert d.action == "fix"
    assert d.mechanism == "publisher_origin"
    assert d.new_geo == []


def test_genuine_domestic_story_keeps_corroborated_origin():
    """A genuinely-domestic story names its country ⇒ the origin tag is what
    the current path would stamp ⇒ clean, untouched."""
    payload = {
        "publisher_origin": ["SG"],
        "title": "Singapore unveils new housing policy",
    }
    d = regeo.assess_row(payload, ["SG"])
    assert d.action == "clean"


def test_country_entity_corroboration_also_keeps_origin():
    payload = {
        "publisher_origin": ["SG"],
        "title": "New housing policy unveiled",
        "entities": [{"class": "country", "text": "Singapore"}],
    }
    d = regeo.assess_row(payload, ["SG"])
    assert d.action == "clean"


def test_geocoder_derived_tag_is_not_a_publisher_origin_candidate():
    """geo carries the geocoder's own promote (payload.geo.country_iso2 in geo)
    ⇒ the tag did NOT come from the fallback ⇒ mechanism 1 must not touch it."""
    payload = {
        "publisher_origin": ["SG"],
        "geo": {"country": "Singapore", "country_iso2": "SG", "lat": 1.29, "lon": 103.85},
        "title": "A story resolved by the geocoder",
    }
    d = regeo.assess_row(payload, ["SG"])
    # Falls through to mechanism 2, which needs an incidental location entity.
    assert d.action in ("clean", "not_candidate")
    assert d.mechanism != "publisher_origin" or d.action == "clean"


def test_yonhap_dateline_repointed_to_country_entity():
    """The S-2 dateline case: an incidental Brasília dateline out-voted an
    inter-Korean story. The country-class entity wins under current rules."""
    payload = {
        "geo": {"country": "Brazil", "country_iso2": "BR",
                "lat": -15.79, "lon": -47.88, "precision": "city"},
        "title": "Inter-Korean envoy discusses denuclearisation steps",
        "entities": [
            {"class": "location", "text": "Brasília"},
            {"class": "country", "text": "South Korea"},
        ],
    }
    d = regeo.assess_row(payload, ["BR"])
    assert d.action == "fix"
    assert d.mechanism == "dateline_location"
    assert d.new_geo == ["KR"]
    assert d.new_geo_block["country_iso2"] == "KR"
    assert d.new_geo_block["precision"] == "country"
    assert "lat" not in d.new_geo_block  # never keep the wrong place's point


def test_dateline_repointed_by_text_sweep_when_no_country_entity():
    payload = {
        "geo": {"country": "Brazil", "country_iso2": "BR"},
        "title": "Ukraine reports strikes on the grid",
        "entities": [{"class": "location", "text": "Brasília"}],
    }
    d = regeo.assess_row(payload, ["BR"])
    assert d.action == "fix"
    assert d.new_geo == ["UA"]


def test_promoted_iso_attested_by_content_is_clean():
    """A story genuinely about Brazil keeps its BR tag even with an
    incidental location present."""
    payload = {
        "geo": {"country": "Brazil", "country_iso2": "BR"},
        "title": "Brazil launches a new satellite program",
        "entities": [{"class": "location", "text": "Kourou"}],
    }
    d = regeo.assess_row(payload, ["BR"])
    assert d.action == "clean"


def test_subject_dateline_needs_backend_is_skipped_not_guessed():
    """The location IS the subject (in the title) but only the online gazetteer
    can resolve it ⇒ skip, never guess."""
    payload = {
        "geo": {"country": "Brazil", "country_iso2": "BR"},
        "title": "Summit opens in Brasília",
        "entities": [
            {"class": "location", "text": "Brasília"},
            {"class": "location", "text": "Geneva"},
        ],
    }
    d = regeo.assess_row(payload, ["BR"])
    # 'Brasília' is a subject location; 'Geneva' is incidental; no country
    # entity and no text-sweep hit ⇒ offline cannot out-rank the subject.
    assert d.action == "skip_ambiguous"


def test_location_name_hint_is_skipped_not_guessed():
    payload = {
        "geo": {"country": "Brazil", "country_iso2": "BR",
                "location_name": "Brasília"},
        "title": "Envoy speaks about Korea",
        "entities": [{"class": "location", "text": "Brasília"}],
    }
    d = regeo.assess_row(payload, ["BR"])
    assert d.action == "skip_ambiguous"


def test_incidental_only_row_left_alone():
    """No higher-priority candidate at all ⇒ the incidental location may still
    legitimately resolve under current rules ⇒ not provably wrong ⇒ clean."""
    payload = {
        "geo": {"country": "Brazil", "country_iso2": "BR"},
        "title": "Officials hold closed-door talks",
        "entities": [{"class": "location", "text": "Brasília"}],
    }
    d = regeo.assess_row(payload, ["BR"])
    assert d.action == "clean"


def test_empty_geo_and_plain_rows_not_candidates():
    assert regeo.assess_row({}, []).action == "not_candidate"
    assert regeo.assess_row({"title": "x"}, ["DE"]).action == "not_candidate"


def test_fixed_rows_reassess_clean_idempotent():
    """The decision applied to its own output is a no-op (both mechanisms)."""
    # Class 2 after the fix: promoted=KR, attested by the country entity.
    payload2 = {
        "geo": {"country": "South Korea", "country_iso2": "KR",
                "precision": "country", "backfill": "s2_regeocode_2026_07"},
        "title": "Inter-Korean envoy discusses denuclearisation steps",
        "entities": [
            {"class": "location", "text": "Brasília"},
            {"class": "country", "text": "South Korea"},
        ],
    }
    assert regeo.assess_row(payload2, ["KR"]).action == "clean"


# ---------------------------------------------------------------------------
# DB-backed — dry-run/apply/idempotency against the migrated test DB
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.close()


async def _seed(conn, *, tenant, source, geo, payload) -> object:
    sid = uuid4()
    await conn.execute(
        """
        INSERT INTO signals (id, source_id, owner_tenant, modality, payload,
                             content_hash, geo, indexed_at)
        VALUES ($1,$2,$3,'text',$4::jsonb,$5,$6::text[],NOW())
        """,
        sid, source, tenant, json.dumps(payload), f"h_{uuid4().hex}", geo,
    )
    return sid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_fixes_contaminated_and_spares_clean_rows(conn):
    tenant = f"t_{uuid4().hex[:8]}"
    cna = await _seed(
        conn, tenant=tenant, source="src.cna", geo=["SG"],
        payload={"publisher_origin": ["SG"],
                 "title": "F1 season preview: the title fight"},
    )
    sg_ok = await _seed(
        conn, tenant=tenant, source="src.cna", geo=["SG"],
        payload={"publisher_origin": ["SG"],
                 "title": "Singapore expands the port"},
    )
    yonhap = await _seed(
        conn, tenant=tenant, source="src.yonhap", geo=["BR"],
        payload={"geo": {"country": "Brazil", "country_iso2": "BR",
                         "lat": -15.79, "lon": -47.88},
                 "title": "Inter-Korean envoy discusses next steps",
                 "entities": [{"class": "location", "text": "Brasília"},
                              {"class": "country", "text": "South Korea"}]},
    )
    plain = await _seed(
        conn, tenant=tenant, source="src.other", geo=["DE"],
        payload={"title": "Germany story with no enrichment payload"},
    )

    # Dry-run: reports, changes nothing.
    dry = await regeo.run(conn, tenant=tenant, quiet=True)
    assert dry["fixed_publisher_origin"] == 1
    assert dry["fixed_dateline_location"] == 1
    assert dry["cleared_geo"] == 1
    fixed_ids = {f["id"] for f in dry["fixes"]}
    assert fixed_ids == {str(cna), str(yonhap)}
    for sid, geo in [(cna, ["SG"]), (sg_ok, ["SG"]), (yonhap, ["BR"]), (plain, ["DE"])]:
        assert await conn.fetchval("SELECT geo FROM signals WHERE id=$1", sid) == geo
        assert await conn.fetchval(
            "SELECT indexed_at FROM signals WHERE id=$1", sid) is not None

    # Apply: contaminated rows corrected, clean rows untouched.
    res = await regeo.run(conn, tenant=tenant, apply=True, quiet=True)
    assert res["fixed_publisher_origin"] == 1
    assert res["fixed_dateline_location"] == 1

    assert await conn.fetchval("SELECT geo FROM signals WHERE id=$1", cna) == []
    assert await conn.fetchval("SELECT geo FROM signals WHERE id=$1", yonhap) == ["KR"]
    assert await conn.fetchval("SELECT geo FROM signals WHERE id=$1", sg_ok) == ["SG"]
    assert await conn.fetchval("SELECT geo FROM signals WHERE id=$1", plain) == ["DE"]

    # Corpus dirty-marker NULLed on the changed rows ONLY (0082 rule).
    for sid in (cna, yonhap):
        assert await conn.fetchval(
            "SELECT indexed_at FROM signals WHERE id=$1", sid) is None
    for sid in (sg_ok, plain):
        assert await conn.fetchval(
            "SELECT indexed_at FROM signals WHERE id=$1", sid) is not None

    # payload.geo rewritten honestly for the dateline class (wrong point gone).
    pl = json.loads(await conn.fetchval(
        "SELECT payload FROM signals WHERE id=$1", yonhap))
    assert pl["geo"]["country_iso2"] == "KR"
    assert pl["geo"]["precision"] == "country"
    assert "lat" not in pl["geo"]
    # Class-1 payload untouched (origin stays parked, only the column cleared).
    pl_cna = json.loads(await conn.fetchval(
        "SELECT payload FROM signals WHERE id=$1", cna))
    assert pl_cna["publisher_origin"] == ["SG"]

    # Idempotent: a second apply fixes nothing further.
    again = await regeo.run(conn, tenant=tenant, apply=True, quiet=True)
    assert again["fixed_publisher_origin"] == 0
    assert again["fixed_dateline_location"] == 0
