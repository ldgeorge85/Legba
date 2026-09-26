# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 7g-3 — the first collection manifest, and the verifier that reads it.

Two halves, both offline:

1. **The shipped manifest** ``descriptors/collection_series_pilot_2016_2026.yaml``
   is checked as DATA — it parses, every series carries every field the design
   note's manifest shape requires, its subjects are exactly the four pilot
   desks, its window is the decade, its firewall names every one of the eight
   surfaces §2 excludes, and its licence block carries text and a URL for both
   providers. The origin and licence classes are checked against the REAL
   closed vocabularies (``legba.data.provenance.origin`` /
   ``.access``) rather than a hand-written list, so a vocabulary change breaks
   this file rather than silently letting a collection declare a class that
   does not exist.

2. **The verifier's parsing half**
   (``scripts/verify_collection_manifest.py``) is driven against fixtures:
   a three-series fixture manifest, two real captured World Bank v2 responses
   and two real captured EIA bulk records. The production functions are
   imported and called — nothing here re-implements a parser.

NO NETWORK. Not one test in this file opens a socket. The fetching half of the
verifier is exercised by running it (the lane report carries the live coverage
table); what is pinned HERE is the parsing, the coverage arithmetic and the
exit-code arithmetic, which are the parts that can be wrong silently.

THE ONE RULE THIS FILE ENFORCES HARDEST: absence is absence. A subject a
provider does not hold must come back with ``first_year is None`` and
``values == 0`` — never a zero that a reader could average.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data.provenance.access import ACCESS_CLASSES          # noqa: E402
from legba.data.provenance.origin import LIVE_CLASSES, ORIGIN_CLASSES  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "descriptors" / "collection_series_pilot_2016_2026.yaml"
FIXTURES = Path(__file__).parent / "fixtures" / "collection_manifest_pilot"

#: The four pilot desks (design note §7), desk id -> ISO2 subject.
PILOT_DESKS = {
    "country_g20_us": "US",
    "country_watch_il": "IL",
    "country_watch_ir": "IR",
    "country_watch_ua": "UA",
}

#: §2 — the registry refuses a collection whose firewall omits any of these.
EXCLUDED_SURFACES = {
    "cadence_analysts",
    "freshness",
    "source_health",
    "calibration",
    "salience",
    "alerts",
    "reactive_triggers",
    "surge_detection",
}

#: §2 — the readers that may opt in.
OPT_IN_READERS = {
    "consult",
    "research",
    "claim_watch",
    "program6_baselines",
    "replay",
}

#: The manifest shape §2 specifies for a series collection.
REQUIRED_SERIES_FIELDS = (
    "series_id",
    "provider",
    "dataset",
    "indicator",
    "unit",
    "subjects",
    "valid_from",
    "valid_to",
    "cadence",
    "source_url_template",
)


def _load_verifier():
    """Import ``scripts/verify_collection_manifest.py`` as a module.

    The script is a CLI, not a package member; this is the same
    spec_from_file_location shape ``tests/test_render_prompt_pack.py`` uses for
    ``scripts/render_prompt_pack.py``, with one addition: the module is put in
    ``sys.modules`` BEFORE it is executed, because ``@dataclass`` resolves a
    class's own module out of ``sys.modules`` while processing its fields and
    raises ``AttributeError: 'NoneType' object has no attribute '__dict__'``
    when it is not there.
    """
    name = "verify_collection_manifest"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "verify_collection_manifest.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


vcm = _load_verifier()


@pytest.fixture(scope="module")
def body() -> dict:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. The shipped manifest as data
# ---------------------------------------------------------------------------


def test_manifest_file_is_collection_prefixed_not_source():
    """`collection_`, never `source_` (§2) — the prefix IS the firewall's
    first line: the source machinery globs ``descriptors/source_*.yaml``, so a
    mis-prefixed collection would be picked up as a live feed."""
    assert MANIFEST_PATH.exists(), MANIFEST_PATH
    assert MANIFEST_PATH.name.startswith("collection_")
    assert not MANIFEST_PATH.name.startswith("source_")


def test_manifest_parses_and_declares_the_family(body):
    assert body["identity"]["schema_uri"] == "legba/collection/1.0.0"
    assert body["identity"]["id"] == "collection.series_pilot_2016_2026"
    # draft -> reviewed -> loaded -> superseded (§2). Nothing loads from a draft.
    assert body["identity"]["state"] == "draft"
    assert body["origin_shape"] == "archive_only"


def test_origin_class_is_a_real_history_class():
    """`archive` must be in the closed origin vocabulary and NOT live.

    Driven off ``legba.data.provenance.origin`` itself: if the vocabulary ever
    loses ``archive``, this collection's rows become uninsertable and that must
    fail here, not at load time.
    """
    body_ = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert body_["origin_class"] in ORIGIN_CLASSES
    assert body_["origin_class"] not in LIVE_CLASSES


def test_licence_class_is_a_real_access_class_and_fails_closed(body):
    """§2/§D.6: a manifest without a recorded class does not load."""
    assert body["licence_class"] in ACCESS_CLASSES
    assert body["licence_class"] == "public"


def test_licence_block_carries_text_and_a_url_per_provider(body):
    """Every provider named by a series has a licence text AND a licence URL.

    The operator approves this block (§7); an unapproved provider with no
    recorded terms is exactly the fail-closed case.
    """
    providers = body["licence"]["providers"]
    named_by_series = {s["provider"] for s in body["manifest"]["series"]}
    assert named_by_series == set(providers), (
        f"series name {sorted(named_by_series)}; licence block covers "
        f"{sorted(providers)}"
    )
    for name, block in providers.items():
        for field_ in ("holder", "licence", "licence_url", "terms_url", "text",
                       "attribution"):
            assert block.get(field_), f"{name}.{field_} is empty"
        assert block["licence_url"].startswith("https://"), name
        assert block["terms_url"].startswith("https://"), name
    assert "CC BY 4.0" in providers["world_bank"]["licence"]
    assert "public domain" in providers["eia"]["licence"]


def test_firewall_names_every_excluded_surface(body):
    """§2 — the registry refuses a collection whose firewall omits any of the
    eight. Equality, not containment: a NEW surface added to the design note
    must fail this test rather than pass by being absent from both sides."""
    firewall = body["firewall"]
    assert set(firewall["excluded_from"]) == EXCLUDED_SURFACES
    assert set(firewall["readers_opt_in"]) == OPT_IN_READERS
    assert not (set(firewall["readers_opt_in"]) & set(firewall["excluded_from"]))


def test_subjects_are_exactly_the_four_pilot_desks(body):
    assert body["subject_kind"] == "country"
    got = {entry["desk"]: entry["subject"] for entry in body["subjects"]}
    assert got == PILOT_DESKS
    for entry in body["subjects"]:
        assert len(entry["subject"]) == 2, entry
        assert len(entry["iso3"]) == 3, entry


def test_the_window_is_the_decade(body):
    assert body["window"] == {"valid_from": "2016-01-01", "valid_to": "2025-12-31"}
    for series in body["manifest"]["series"]:
        assert series["valid_from"] == "2016-01-01", series["series_id"]
        assert series["valid_to"] == "2025-12-31", series["series_id"]


def test_every_series_carries_every_required_field(body):
    series = body["manifest"]["series"]
    assert len(series) >= 6, "a first collection with fewer than six series"
    ids = [s["series_id"] for s in series]
    assert len(ids) == len(set(ids)), "duplicate series_id"
    for entry in series:
        missing = [f for f in REQUIRED_SERIES_FIELDS if not entry.get(f)]
        assert not missing, f"{entry.get('series_id')}: missing {missing}"
        assert set(entry["subjects"]) == set(PILOT_DESKS.values()), entry["series_id"]
        assert entry["cadence"] in {"annual", "monthly", "quarterly"}, entry["series_id"]
        assert entry["subject_kind"] == "country", entry["series_id"]


def test_every_series_carries_a_coverage_block_per_subject(body):
    """The point of the manifest: what the provider ACTUALLY holds, per subject.

    ``first_valid_year``/``last_valid_year`` may be null — that is absence
    rendered as absence — but then ``values`` must be 0 and a ``reason`` must
    say why, and the null count must fill the window. A held subject's first
    and last year must sit inside the window and the two counts must sum to
    the decade.
    """
    for entry in body["manifest"]["series"]:
        coverage = entry["coverage"]
        assert coverage.get("fetched_at"), entry["series_id"]
        for subject in entry["subjects"]:
            block = coverage.get(subject)
            assert isinstance(block, dict), f"{entry['series_id']}/{subject}"
            values, nulls = block["values"], block["nulls"]
            assert values + nulls == 10, f"{entry['series_id']}/{subject}"
            if values == 0:
                assert block["first_valid_year"] is None
                assert block["last_valid_year"] is None
                assert block.get("held") is False
                assert block.get("reason"), (
                    f"{entry['series_id']}/{subject}: an unheld subject must "
                    "say WHY, so a reader never sees it as a zero"
                )
            else:
                assert 2016 <= block["first_valid_year"] <= 2025
                assert 2016 <= block["last_valid_year"] <= 2025
                assert block["first_valid_year"] <= block["last_valid_year"]


def test_the_two_known_holes_are_recorded_not_filled(body):
    """The findings the fetch produced, pinned so a later edit cannot quietly
    'fix' them by inventing numbers: the World Bank publishes NO external debt
    for the US or Israel (not a DRS borrower), and EIA's international crude
    trade series were frozen by the provider in 2021."""
    by_id = {s["series_id"]: s for s in body["manifest"]["series"]}

    debt = by_id["wb.external_debt_stocks_usd"]["coverage"]
    for subject in ("US", "IL"):
        assert debt[subject]["values"] == 0
        assert debt[subject]["held"] is False
        assert "Debtor Reporting System" in debt[subject]["reason"]
    assert debt["IR"]["values"] == 9
    assert debt["UA"]["values"] == 9

    for sid in ("eia.crude_oil_exports_tbpd", "eia.crude_oil_imports_tbpd"):
        coverage = by_id[sid]["coverage"]
        assert coverage["provider_frozen"] is True
        assert coverage["provider_last_updated"].startswith("2021-07-09")
        assert coverage["US"]["last_valid_year"] == 2020
        assert coverage["IR"]["last_valid_year"] == 2018


def test_no_series_requires_an_api_key(body):
    """The EIA key question, pinned. The bulk route (api.eia.gov/bulk/INTL.zip)
    serves the international petroleum series without one, so this collection
    registers no key and has no credential open item. If a future edit points a
    series at the v2 REST route, this test is where it announces itself."""
    assert body["provider_access"]["eia"]["requires_key"] is False
    assert body["provider_access"]["world_bank"]["requires_key"] is False
    for entry in body["manifest"]["series"]:
        assert entry["fetch"]["requires_key"] is False, entry["series_id"]


def test_loader_declares_the_kind_and_a_zero_token_budget(body):
    loader = body["loader"]
    assert loader["kind"] == "series_api"
    assert loader["priority"] == "low"
    assert loader["resume_key"]
    # House rule: every descriptor carries it; a collection makes no LLM call.
    assert loader["budget_tokens_per_day"] == 0


# ---------------------------------------------------------------------------
# 2. The verifier's parsing half, on fixtures — no network
# ---------------------------------------------------------------------------


def test_parse_manifest_reads_the_shipped_file():
    manifest = vcm.parse_manifest(MANIFEST_PATH)
    assert manifest.collection_id == "collection.series_pilot_2016_2026"
    assert manifest.state == "draft"
    assert manifest.origin_class == "archive"
    assert manifest.licence_class == "public"
    assert set(manifest.subjects) == set(PILOT_DESKS.values())
    assert len(manifest.series) == 11
    assert manifest.iso3("IR") == "IRN"
    assert manifest.series[0].years() == tuple(range(2016, 2026))


def test_parse_manifest_reads_the_fixture_and_its_three_shapes():
    manifest = vcm.parse_manifest(FIXTURES / "mini_manifest.yaml")
    assert [s.series_id for s in manifest.series] == [
        "fx.json_api_series", "fx.bulk_series", "fx.keyed_series",
    ]
    json_api, bulk, keyed = manifest.series
    assert json_api.mode == "json_api" and not json_api.requires_key
    assert bulk.mode == "bulk_jsonl" and bulk.fetch["bulk_member"] == "BULK.txt"
    assert keyed.requires_key and keyed.key_env == "LEGBA_FIXTURE_PROVIDER_API_KEY"


def test_a_manifest_with_no_series_refuses_rather_than_verifying_green(tmp_path):
    """An empty holding must RAISE, not report 'nothing to check, all green'."""
    path = tmp_path / "empty.yaml"
    path.write_text("identity: {id: x}\nmanifest: {series: []}\n", encoding="utf-8")
    with pytest.raises(vcm.ManifestError) as exc:
        vcm.parse_manifest(path)
    assert "manifest.series" in str(exc.value)


def test_a_series_missing_a_required_field_refuses(tmp_path):
    body = yaml.safe_load((FIXTURES / "mini_manifest.yaml").read_text(encoding="utf-8"))
    del body["manifest"]["series"][0]["unit"]
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    with pytest.raises(vcm.ManifestError) as exc:
        vcm.parse_manifest(path)
    assert "unit" in str(exc.value)


def test_an_undeclared_subject_refuses_rather_than_guessing():
    manifest = vcm.parse_manifest(FIXTURES / "mini_manifest.yaml")
    with pytest.raises(vcm.ManifestError) as exc:
        manifest.iso3("ZZ")
    assert "ZZ" in str(exc.value)


def test_url_and_indicator_templates_substitute_both_codes():
    manifest = vcm.parse_manifest(MANIFEST_PATH)
    by_id = {s.series_id: s for s in manifest.series}

    wb = by_id["wb.gdp_growth_annual_pct"]
    url = vcm.resolve_url(wb, "IR", manifest.iso3("IR"))
    assert url.startswith("https://api.worldbank.org/v2/country/IR/indicator/")
    assert "date=2016:2025" in url

    eia = by_id["eia.crude_oil_production_tbpd"]
    assert vcm.resolve_indicator(eia, "UA", manifest.iso3("UA")) == "INTL.57-1-UKR-TBPD.A"


# --- the coverage arithmetic, on real captured provider bytes ---------------


def _years() -> tuple[int, ...]:
    return tuple(range(2016, 2026))


def test_world_bank_coverage_on_a_real_response_with_one_gap():
    payload = json.loads(
        (FIXTURES / "world_bank_ir_cpi.json").read_text(encoding="utf-8")
    )
    cov = vcm.coverage_from_world_bank(payload, "IR", _years())
    assert cov.subject == "IR"
    assert cov.first_year == 2016
    assert cov.last_year == 2025
    assert cov.values == 10
    assert cov.nulls == 0
    assert cov.held is True
    assert cov.provider_last_updated == "2026-07-13"


def test_world_bank_coverage_renders_a_never_held_subject_as_absence():
    """DT.DOD.DECT.CD for the US: the provider answers 200 with ten rows, every
    value null. That is ABSENCE — first/last year None and zero values — and
    must never collapse into a zero a reader could chart."""
    payload = json.loads(
        (FIXTURES / "world_bank_us_external_debt.json").read_text(encoding="utf-8")
    )
    cov = vcm.coverage_from_world_bank(payload, "US", _years())
    assert cov.first_year is None
    assert cov.last_year is None
    assert cov.values == 0
    assert cov.nulls == 10
    assert cov.held is False
    assert cov.as_declared()["first_valid_year"] is None


def test_world_bank_envelope_without_rows_is_zero_coverage_not_a_crash():
    cov = vcm.coverage_from_world_bank([{"lastupdated": "2026-07-13"}, None],
                                       "IL", _years())
    assert cov.values == 0 and cov.first_year is None


def test_world_bank_non_envelope_body_raises():
    with pytest.raises(vcm.ManifestError):
        vcm.coverage_from_world_bank({"message": "bad"}, "US", _years())


def test_eia_coverage_on_a_real_full_record():
    record = json.loads(
        (FIXTURES / "eia_intl_57_1_usa_tbpd_a.json").read_text(encoding="utf-8")
    )
    cov = vcm.coverage_from_eia_record(record, "US", _years())
    assert (cov.first_year, cov.last_year, cov.values, cov.nulls) == (2016, 2025, 10, 0)


def test_eia_coverage_on_a_real_provider_frozen_record():
    """INTL.57-4-IRN: the provider stopped publishing in 2021 and the record
    still answers. Seven of the ten years are simply not held."""
    record = json.loads(
        (FIXTURES / "eia_intl_57_4_irn_tbpd_a.json").read_text(encoding="utf-8")
    )
    cov = vcm.coverage_from_eia_record(record, "IR", _years())
    assert (cov.first_year, cov.last_year, cov.values, cov.nulls) == (2016, 2018, 3, 7)
    assert cov.provider_last_updated.startswith("2021-07-09")


def test_eia_coverage_for_a_record_absent_from_the_bulk_file():
    cov = vcm.coverage_from_eia_record(None, "IL", _years())
    assert cov.values == 0 and cov.first_year is None and cov.nulls == 10


def test_eia_coverage_ignores_sub_annual_periods():
    """A monthly period (``202601``) in an annual series' data must not be
    counted as the year 2026 — the window arithmetic would silently shift."""
    record = {"last_updated": "2026-01-01", "data": [["202601", 1.0], ["2016", 2.0]]}
    cov = vcm.coverage_from_eia_record(record, "US", _years())
    assert (cov.first_year, cov.last_year, cov.values) == (2016, 2016, 1)


# --- the bulk scanner, over a zip built in-process (stdlib, no network) -----


def test_scan_bulk_jsonl_keeps_only_the_records_asked_for(tmp_path):
    archive = tmp_path / "BULK.zip"
    lines = [
        json.dumps({"series_id": "BULK.9-9-USA-UNIT.A", "data": [["2016", 1.0]]}),
        json.dumps({"series_id": "BULK.9-9-OTH-UNIT.A", "data": [["2016", 9.0]]}),
        json.dumps({"series_id": "BULK.9-9-IRN-UNIT.A", "data": [["2016", 2.0]]}),
    ]
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("BULK.txt", "\n".join(lines) + "\n")

    found = vcm.scan_bulk_jsonl(
        archive, "BULK.txt",
        {"BULK.9-9-USA-UNIT.A", "BULK.9-9-IRN-UNIT.A"}, "series_id",
    )
    assert set(found) == {"BULK.9-9-USA-UNIT.A", "BULK.9-9-IRN-UNIT.A"}
    assert found["BULK.9-9-IRN-UNIT.A"]["data"] == [["2016", 2.0]]


def test_sha256_of_matches_hashlib(tmp_path):
    import hashlib

    path = tmp_path / "blob.bin"
    path.write_bytes(b"legba-collection")
    assert vcm.sha256_of(path) == hashlib.sha256(b"legba-collection").hexdigest()


# --- drift, skips and the exit-code arithmetic ------------------------------


def _spec_and_cov(series_id: str, subject: str, **kw):
    manifest = vcm.parse_manifest(FIXTURES / "mini_manifest.yaml")
    spec = {s.series_id: s for s in manifest.series}[series_id]
    return spec, vcm.SubjectCoverage(subject=subject, **kw)


def test_declared_matches_agrees_disagrees_and_abstains():
    spec, cov = _spec_and_cov(
        "fx.json_api_series", "IR",
        first_year=2016, last_year=2024, values=9, nulls=1,
    )
    assert vcm.declared_matches(spec, cov) is True

    spec, drifted = _spec_and_cov(
        "fx.json_api_series", "IR",
        first_year=2016, last_year=2025, values=10, nulls=0,
    )
    assert vcm.declared_matches(spec, drifted) is False

    # The keyed series declares no coverage at all — abstain, never "agrees".
    spec, cov = _spec_and_cov(
        "fx.keyed_series", "US", first_year=2016, last_year=2025, values=10, nulls=0,
    )
    assert vcm.declared_matches(spec, cov) is None


def test_summarise_reports_nowhere_drift_and_skips_separately():
    manifest = vcm.parse_manifest(FIXTURES / "mini_manifest.yaml")
    by_id = {s.series_id: s for s in manifest.series}
    results = [
        # held for one subject, absent for the other -> NOT "nowhere"
        (by_id["fx.json_api_series"],
         vcm.SubjectCoverage("US", 2016, 2025, 10, 0)),
        (by_id["fx.json_api_series"],
         vcm.SubjectCoverage("IR", None, None, 0, 10)),
        # held for neither subject -> resolves NOWHERE
        (by_id["fx.bulk_series"], vcm.SubjectCoverage("US", None, None, 0, 10)),
        (by_id["fx.bulk_series"], vcm.SubjectCoverage("IR", None, None, 0, 10)),
        # skipped for a missing key -> neither held nor nowhere
        (by_id["fx.keyed_series"],
         vcm.SubjectCoverage("US", None, None, 0, 10, skipped_reason="no key")),
    ]
    nowhere, drifted, skipped = vcm.summarise(results)
    # "nowhere" is per SERIES, not per subject: fx.json_api_series held US, so
    # its empty IR is a gap, not a failure. fx.bulk_series held neither.
    assert nowhere == ["fx.bulk_series"]
    # Every subject whose measurement contradicts the fixture's declared block
    # (fx.json_api_series declares IR 9v/1n; fx.bulk_series declares US 10v/0n
    # and IR 3v/7n) — a skipped series is never counted as drift.
    assert drifted == [
        "fx.json_api_series/IR", "fx.bulk_series/US", "fx.bulk_series/IR",
    ]
    assert skipped == ["fx.keyed_series: no key"]


def test_skip_reason_fires_only_for_a_keyed_series_without_its_env(monkeypatch):
    manifest = vcm.parse_manifest(FIXTURES / "mini_manifest.yaml")
    by_id = {s.series_id: s for s in manifest.series}

    monkeypatch.delenv("LEGBA_FIXTURE_PROVIDER_API_KEY", raising=False)
    reason = vcm._skip_reason(by_id["fx.keyed_series"])
    assert reason and "LEGBA_FIXTURE_PROVIDER_API_KEY" in reason
    assert vcm._skip_reason(by_id["fx.json_api_series"]) is None

    monkeypatch.setenv("LEGBA_FIXTURE_PROVIDER_API_KEY", "set")
    assert vcm._skip_reason(by_id["fx.keyed_series"]) is None


def test_render_table_prints_an_em_dash_for_absence_never_a_zero_year():
    manifest = vcm.parse_manifest(FIXTURES / "mini_manifest.yaml")
    spec = manifest.series[0]
    table = vcm.render_table([
        (spec, vcm.SubjectCoverage("US", 2016, 2025, 10, 0)),
        (spec, vcm.SubjectCoverage("IR", None, None, 0, 10)),
    ])
    lines = table.splitlines()
    assert "series" in lines[0] and "status" in lines[0]
    rows = [line for line in lines[2:] if line.startswith(spec.series_id)]
    assert len(rows) == 2
    us_row = next(line for line in rows if line.split()[1] == "US")
    ir_row = next(line for line in rows if line.split()[1] == "IR")
    assert "2016" in us_row and "ok" in us_row
    assert "—" in ir_row
    assert "DRIFT" in ir_row   # the fixture declares IR held 9v/1n


def test_bulk_groups_refuses_a_bulk_series_without_its_archive_fields(tmp_path):
    body = yaml.safe_load((FIXTURES / "mini_manifest.yaml").read_text(encoding="utf-8"))
    del body["manifest"]["series"][1]["fetch"]["bulk_url"]
    path = tmp_path / "broken_bulk.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    manifest = vcm.parse_manifest(path)
    with pytest.raises(vcm.ManifestError) as exc:
        vcm._bulk_groups(manifest)
    assert "bulk_url" in str(exc.value)
