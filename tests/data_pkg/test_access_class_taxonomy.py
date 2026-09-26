# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wave-E — access-class taxonomy: schema and the classification pass over
every committed ``descriptors/source_*.yaml``.

Mirrors ``test_source_class_taxonomy.py``'s discipline exactly, one axis
over: ``scope.access_class`` is WHO may read what a source produces (see
``legba.data.provenance.access`` for the full axis writeup), orthogonal to
``scope.source_class`` (editorial tier) and ``scope.license_class``
(what may be KEPT of a fetched page).

Covers:
  1. the ``access_class`` field on the real ``SourceScope`` schema round-trips
     WITH and WITHOUT an explicit value (fail-closed default ``restricted``),
     and rejects an off-vocabulary value;
  2. every committed ``descriptors/source_*.yaml`` validates and carries the
     expected class — the RULE TABLE (also in ``docs/DATA_SOURCES.md``'s
     "Access class" section):
       * default ``public`` — a free, no-auth, openly-available feed; no
         separate commercial-licence agreement gates internal read access
         (the large majority: 94 of 103 files);
       * ``licensed_noncommercial`` — a curated dataset carrying its OWN
         noncommercial-bounded reuse terms distinct from open-web reporting
         (ACLED, OpenSanctions bulk + API — 3 files; UCDP GED is CC BY 4.0,
         attribution-only per docs/DATA_SOURCES.md's licence note, and is
         ``public`` like the open-web majority);
       * ``restricted`` (fail-closed) — a never-bound generic/template
         adapter with no concrete editorial source to review terms for
         (common_crawl_news, discord_webhook, firecrawl, generic_webhook,
         query_source_discovery_template — 5 files);
  3. every committed file is pinned in the expected map (guards a future
     ``source_*.yaml`` landing unclassified).
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

from legba.data.provenance.access import ACCESS_CLASSES
from legba.data.schemas.source import AccessClass, SourceDescriptor, SourceScope

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"

VOCAB = set(ACCESS_CLASSES)

# The committed YAML source descriptors and their reviewed access class.
# Default "public"; the two override buckets carry their rationale in the
# per-file inline comment (`grep 'Wave-E —' descriptors/source_*.yaml`).
EXPECTED_YAML_ACCESS: dict[str, str] = {
    "source_38north.yaml": "public",
    "source_aawsat.yaml": "public",
    "source_abc_australia.yaml": "public",
    "source_acled_conflict.yaml": "licensed_noncommercial",
    "source_actuniger_politique.yaml": "public",
    "source_actuniger_societe.yaml": "public",
    "source_agenciabrasil.yaml": "public",
    "source_aib_burkina.yaml": "public",
    "source_aljazeera_world.yaml": "public",
    "source_amap_mali.yaml": "public",
    "source_ansa.yaml": "public",
    "source_antara.yaml": "public",
    "source_armscontrol.yaml": "public",
    "source_bangkokpost.yaml": "public",
    "source_batimes.yaml": "public",
    "source_bbc_world.yaml": "public",
    "source_breakingdefense.yaml": "public",
    "source_cbc_world.yaml": "public",
    "source_cgtn_world.yaml": "public",
    "source_common_crawl_news.yaml": "restricted",
    "source_dabangasudan.yaml": "public",
    "source_dailymaverick.yaml": "public",
    "source_dailynk.yaml": "public",
    "source_dawn.yaml": "public",
    "source_defensenews.yaml": "public",
    "source_dfrlab.yaml": "public",
    "source_digitimes.yaml": "public",
    "source_discord_webhook.yaml": "restricted",
    "source_dw_world.yaml": "public",
    "source_eia_today_in_energy.yaml": "public",
    "source_elpais_english.yaml": "public",
    "source_euronews.yaml": "public",
    "source_euvsdisinfo.yaml": "public",
    "source_firecrawl.yaml": "restricted",
    "source_france24_afrique.yaml": "public",
    "source_gdelt_bigquery.yaml": "public",
    "source_gdelt_doc_api.yaml": "public",
    "source_gdelt_files.yaml": "public",
    "source_generic_webhook.yaml": "restricted",
    "source_groundup.yaml": "public",
    "source_guardian_world.yaml": "public",
    "source_intelmq_cisa_kev.yaml": "public",
    "source_irna_english.yaml": "public",
    "source_isw.yaml": "public",
    "source_japantimes.yaml": "public",
    "source_jpost.yaml": "public",
    "source_kremlin.yaml": "public",
    "source_kyiv_independent.yaml": "public",
    "source_lefaso_net.yaml": "public",
    "source_lemonde_english.yaml": "public",
    "source_malijet.yaml": "public",
    "source_maritime_executive.yaml": "public",
    "source_mediacloud.yaml": "public",
    "source_meduza.yaml": "public",
    "source_mexiconewsdaily.yaml": "public",
    "source_middleeasteye.yaml": "public",
    "source_navalnews.yaml": "public",
    "source_nhk_world.yaml": "public",
    "source_northernminer.yaml": "public",
    "source_oilprice.yaml": "public",
    "source_opensanctions_api.yaml": "licensed_noncommercial",
    "source_opensanctions_bulk.yaml": "licensed_noncommercial",
    "source_pancanal.yaml": "public",
    "source_presstv_english.yaml": "public",
    "source_query_source_discovery_template.yaml": "restricted",
    "source_rbi_press.yaml": "public",
    "source_reliefweb_api.yaml": "public",
    "source_rferl.yaml": "public",
    "source_rigzone.yaml": "public",
    "source_rsshub_aljazeera_drcongo.yaml": "public",
    "source_rsshub_apnews_drcongo.yaml": "public",
    "source_rsshub_apnews_haiti.yaml": "public",
    "source_rsshub_apnews_niger.yaml": "public",
    "source_rsshub_apnews_north_korea.yaml": "public",
    "source_rsshub_apnews_taiwan.yaml": "public",
    "source_rsshub_apnews_world.yaml": "public",
    "source_rsshub_focustaiwan.yaml": "public",
    "source_rsshub_rfa_korea.yaml": "public",
    "source_rsshub_rfi_afrique.yaml": "public",
    "source_rsshub_rfi_ameriques.yaml": "public",
    "source_sahel_intelligence.yaml": "public",
    "source_scmp_china.yaml": "public",
    "source_spiegel_international.yaml": "public",
    "source_splash247.yaml": "public",
    "source_stategov_press.yaml": "public",
    "source_studiokalangou.yaml": "public",
    "source_studiotamani.yaml": "public",
    "source_studioyafa.yaml": "public",
    "source_sudanwarmonitor.yaml": "public",
    "source_taipeitimes.yaml": "public",
    "source_telegram_monitor.yaml": "public",
    "source_theloadstar.yaml": "public",
    "source_timesofisrael.yaml": "public",
    "source_ucdp_ged.yaml": "public",
    "source_ukrinform_english.yaml": "public",
    "source_un_news_africa.yaml": "public",
    "source_un_news_middle_east.yaml": "public",
    "source_un_news_peace_security.yaml": "public",
    "source_un_press.yaml": "public",
    "source_usgs_earthquakes.yaml": "public",
    "source_who_disease_outbreak_news.yaml": "public",
    "source_worldnuclearnews.yaml": "public",
    "source_wto_news.yaml": "public",
}

#: The five files reviewed and found genuinely unclassifiable today — never-
#: bound generic/template adapters with no concrete editorial source to read
#: terms from. Pinned here (as the taxonomy test's own idiom expects) rather
#: than left to be re-derived from the map above.
FAIL_CLOSED_FILES: frozenset[str] = frozenset(
    {
        "source_common_crawl_news.yaml",
        "source_discord_webhook.yaml",
        "source_firecrawl.yaml",
        "source_generic_webhook.yaml",
        "source_query_source_discovery_template.yaml",
    }
)

#: The three curated datasets carrying their own noncommercial-bounded reuse
#: terms. UCDP GED is deliberately NOT here — CC BY 4.0 is attribution-only
#: (docs/DATA_SOURCES.md), so it lands in the `public` default bucket.
LICENSED_NONCOMMERCIAL_FILES: frozenset[str] = frozenset(
    {
        "source_acled_conflict.yaml",
        "source_opensanctions_api.yaml",
        "source_opensanctions_bulk.yaml",
    }
)


def _load_body(name: str) -> dict[str, Any]:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return body


def _load_descriptor(name: str) -> SourceDescriptor:
    return SourceDescriptor.model_validate(_load_body(name), strict=False)


# ---------------------------------------------------------------------------
# 1. Schema round-trip: with, without (fail-closed default), off-vocabulary.
# ---------------------------------------------------------------------------


def test_scope_defaults_access_class_to_restricted_when_absent():
    """Omitting access_class validates (backward-compatible) and defaults to
    the FAIL-CLOSED ``restricted`` bucket — never ``public``."""
    scope = SourceScope()
    assert scope.access_class == "restricted"
    desc = SourceDescriptor.model_validate(
        {
            "identity": {
                "id": "source.test.noaccessscope",
                "name": "no access scope",
                "kind": "rss",
                "schema_uri": "legba/source/1.0.0",
                "version": "0" * 16,
                "owner": "t",
                "created": "2026-07-02T00:00:00Z",
            }
        },
        strict=False,
    )
    assert desc.scope.access_class == "restricted"


@pytest.mark.parametrize("cls", sorted(VOCAB))
def test_scope_accepts_every_vocabulary_class(cls: str):
    scope = SourceScope(access_class=cls)
    assert scope.access_class == cls
    reparsed = SourceScope.model_validate(scope.model_dump(mode="python"))
    assert reparsed.access_class == cls


def test_scope_rejects_off_vocabulary_access_class():
    with pytest.raises(Exception):
        SourceScope(access_class="unreviewed")  # not in the Literal vocabulary


def test_access_class_literal_vocabulary_matches_the_provenance_module():
    from typing import get_args

    assert set(get_args(AccessClass)) == VOCAB
    assert VOCAB == {
        "public", "licensed_commercial", "licensed_noncommercial",
        "restricted", "internal",
    }


def test_descriptor_round_trip_preserves_access_class():
    desc = _load_descriptor("source_acled_conflict.yaml")
    reparsed = SourceDescriptor.model_validate(
        desc.model_dump(mode="python"), strict=False
    )
    assert reparsed.scope.access_class == "licensed_noncommercial"


# ---------------------------------------------------------------------------
# 2. Every committed YAML validates + carries the expected class.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fname,expected", sorted(EXPECTED_YAML_ACCESS.items()))
def test_yaml_descriptor_access_class(fname: str, expected: str):
    desc = _load_descriptor(fname)
    assert desc.scope.access_class == expected
    assert desc.scope.access_class in VOCAB


def test_every_committed_source_yaml_is_access_classified_in_vocab():
    """Guard against a future source_*.yaml landing without a reviewed
    access_class — mirrors test_source_class_taxonomy's own completeness
    guard exactly."""
    for path in sorted(DESCRIPTORS_DIR.glob("source_*.yaml")):
        desc = _load_descriptor(path.name)
        assert desc.scope.access_class in VOCAB, path.name
        assert path.name in EXPECTED_YAML_ACCESS, (
            f"unmapped source descriptor: {path.name}"
        )


def test_fail_closed_files_are_restricted_not_guessed_public():
    """The five never-bound generic/template adapters could not be honestly
    classified — they are `restricted`, the fail-closed default, and no
    other file in the tree carries that class (a `restricted` result
    anywhere else would mean a real source was never reviewed)."""
    assert FAIL_CLOSED_FILES
    for fname in FAIL_CLOSED_FILES:
        assert EXPECTED_YAML_ACCESS[fname] == "restricted"
    restricted_in_map = {
        f for f, c in EXPECTED_YAML_ACCESS.items() if c == "restricted"
    }
    assert restricted_in_map == FAIL_CLOSED_FILES


def test_licensed_noncommercial_files_are_the_four_curated_datasets():
    assert LICENSED_NONCOMMERCIAL_FILES
    for fname in LICENSED_NONCOMMERCIAL_FILES:
        assert EXPECTED_YAML_ACCESS[fname] == "licensed_noncommercial"
    nc_in_map = {
        f for f, c in EXPECTED_YAML_ACCESS.items() if c == "licensed_noncommercial"
    }
    assert nc_in_map == LICENSED_NONCOMMERCIAL_FILES


def test_public_is_the_overwhelming_default():
    """95 of 103 committed sources are free no-auth feeds (or, for UCDP,
    an attribution-only CC BY 4.0 dataset) with no separate agreement
    gating internal read access — the large majority, pinned as a sanity
    count so a future bulk mis-classification (e.g. everything flipping to
    `restricted`) is visible as a number, not just per-file diffs."""
    public_count = sum(
        1 for c in EXPECTED_YAML_ACCESS.values() if c == "public"
    )
    assert public_count == 95
    assert len(EXPECTED_YAML_ACCESS) == 103
