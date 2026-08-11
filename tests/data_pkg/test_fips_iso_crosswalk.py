# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B-6 — the FIPS 10-4 → ISO 3166-1 crosswalk, and the boundary that applies it.

A hand-written 250-row country table is exactly the kind of artefact that gets
reviewed by nodding at it. So the load-bearing test here does not check the table
against itself or against my typing: it checks it against an INDEPENDENT source
of truth that was already in the substrate.

The geocode filter resolves every GDELT row's country from its ``lat``/``lon``
through Natural-Earth admin-0 polygons. It has never heard of FIPS. Joining
those resolutions against the raw FIPS code GDELT shipped in the same row gives
a few thousand (fips_code → true_iso2) observations produced by geometry, not by
a person. Read off the live substrate 2026-08-03 across 9,350 GDELT rows, the
crosswalk agreed with geometry on 161 of the 175 distinct codes observed,
covering 7,728 rows.

``LIVE_OBSERVED`` below is that evidence, checked in. Every pair carries the row
count it was seen with, so the fixture is auditable rather than assertive.

The disagreements are checked in too, in ``GEOMETRY_ARTIFACTS``, with the reason
each one is the geometry's limitation and not the table's error — Gibraltar's
polygon falls inside Spain, Hong Kong's and Macao's inside China, Monaco's inside
France, Singapore's within a kilometre of the Malaysian border, Svalbard's inside
Norway. Without that list the next person to run this comparison "fixes" the
table toward the artifacts and re-breaks the routing.
"""

from __future__ import annotations

import pytest

from legba.data._fips_iso import COLLIDING_CODES, FIPS_TO_ISO2, fips_to_iso2


# (fips, iso2_resolved_from_geometry, rows_observed) — live substrate 2026-08-03.
# Trimmed to pairs seen on 3+ rows; the long tail agrees identically.
LIVE_OBSERVED: tuple[tuple[str, str, int], ...] = (
    ("US", "US", 2069), ("IN", "IN", 659), ("UK", "GB", 487), ("AS", "AU", 284),
    ("NI", "NG", 274), ("CA", "CA", 271), ("IR", "IR", 239), ("PK", "PK", 243),
    ("SP", "ES", 210), ("IS", "IL", 192), ("CH", "CN", 186), ("FR", "FR", 171),
    ("RS", "RU", 153), ("GR", "GR", 126), ("UP", "UA", 106), ("JA", "JP", 88),
    ("EI", "IE", 74), ("MO", "MA", 74), ("SF", "ZA", 63), ("CE", "LK", 57),
    ("GM", "DE", 54), ("AF", "AF", 50), ("TU", "TR", 47), ("NP", "NP", 45),
    ("BG", "BD", 44), ("NZ", "NZ", 43), ("KE", "KE", 40), ("CU", "CU", 39),
    ("SA", "SA", 39), ("IZ", "IQ", 38), ("TH", "TH", 38), ("ID", "ID", 35),
    ("RP", "PH", 34), ("KS", "KR", 33), ("NL", "NL", 32), ("EG", "EG", 30),
    ("MX", "MX", 30), ("AJ", "AZ", 29), ("GH", "GH", 29), ("JO", "JO", 31),
    ("LE", "LB", 26), ("PL", "PL", 24), ("BA", "BH", 24), ("HA", "HT", 21),
    ("MY", "MY", 21), ("SU", "SD", 21), ("SY", "SY", 18), ("BR", "BR", 17),
    ("AM", "AM", 17), ("RO", "RO", 17), ("VM", "VN", 17), ("JM", "JM", 17),
    ("KU", "KW", 16), ("WE", "PS", 15), ("DA", "DK", 13), ("NO", "NO", 12),
    ("KN", "KP", 12), ("ML", "ML", 12), ("UG", "UG", 12), ("AU", "AT", 11),
    ("BM", "MM", 11), ("BE", "BE", 11), ("CB", "KH", 11), ("CY", "CY", 11),
    ("FJ", "FJ", 11), ("GT", "GT", 11), ("GY", "GY", 11), ("PO", "PT", 11),
    ("ZI", "ZW", 11), ("BK", "BA", 10), ("GG", "GE", 10), ("NG", "NE", 10),
    ("AE", "AE", 9), ("CO", "CO", 9), ("HU", "HU", 9), ("KV", "XK", 9),
    ("MU", "OM", 9), ("SO", "SO", 9), ("CI", "CL", 8), ("MI", "MW", 8),
    ("TD", "TT", 8), ("YM", "YE", 8), ("BN", "BJ", 7), ("CF", "CG", 7),
    ("LU", "LU", 7), ("SZ", "CH", 7), ("AG", "DZ", 6), ("EN", "EE", 6),
    ("ET", "ET", 6), ("EZ", "CZ", 6), ("LA", "LA", 6), ("LH", "LT", 6),
    ("LI", "LR", 6), ("QA", "QA", 6), ("VE", "VE", 6), ("AR", "AR", 5),
    ("BU", "BG", 5), ("CS", "CR", 5), ("FI", "FI", 5), ("KZ", "KZ", 5),
    ("AL", "AL", 4), ("BB", "BB", 4), ("CT", "CF", 4), ("DJ", "DJ", 4),
    ("GJ", "GD", 4), ("GL", "GL", 4), ("HO", "HN", 4), ("LY", "LY", 4),
    ("MZ", "MZ", 4), ("WA", "NA", 4), ("AO", "AO", 3), ("CG", "CD", 3),
    ("CM", "CM", 3), ("GK", "GG", 3), ("IC", "IS", 3), ("MT", "MT", 3),
    ("PM", "PA", 3), ("RI", "RS", 3), ("RW", "RW", 3), ("SE", "SC", 3),
    ("SW", "SE", 3), ("WS", "WS", 3),
)

#: Codes where geometry and the crosswalk disagree, with WHY the geometry is the
#: one that is wrong (or, more precisely, imprecise). Checked in so the next
#: person to re-run the comparison does not "correct" the table toward these.
GEOMETRY_ARTIFACTS: dict[str, tuple[str, str, str]] = {
    # fips: (crosswalk_says, geometry_said, why)
    "SN": ("SG", "MY", "Singapore sits within a kilometre of the Malaysian border"),
    "HK": ("HK", "CN", "Hong Kong's polygon is inside China in Natural-Earth admin-0"),
    "MC": ("MO", "CN", "Macao's polygon is inside China in Natural-Earth admin-0"),
    "MN": ("MC", "FR", "Monaco is 2 km2; the nearest admin-0 polygon is France"),
    "GI": ("GI", "ES", "Gibraltar's polygon resolves inside Spain"),
    "SV": ("SJ", "NO", "Svalbard resolves to Norway proper"),
    "FP": ("PF", "FR", "French Polynesia resolves to metropolitan France"),
    "GQ": ("GU", "US", "Guam is a US territory in the admin-0 set"),
    "OD": ("SS", "UG", "one row on the South Sudan / Uganda border"),
    "JE": ("JE", "US", "GDELT shipped a US lat/lon on a Jersey-coded row"),
    "VQ": ("VI", "GB", "GDELT shipped a mismatched lat/lon"),
}


def test_the_crosswalk_agrees_with_independently_geocoded_live_rows():
    """The table, checked against geometry rather than against itself."""
    disagreements = []
    for fips, geometry_iso2, rows in LIVE_OBSERVED:
        mapped = fips_to_iso2(fips)
        if mapped != geometry_iso2:
            disagreements.append((fips, mapped, geometry_iso2, rows))
    assert not disagreements, (
        "crosswalk disagrees with geometry-resolved live rows: "
        f"{disagreements}. If one of these is a genuine geometry artifact, add "
        "it to GEOMETRY_ARTIFACTS with its reason — do not silently drop it."
    )


def test_every_geometry_artifact_is_still_a_real_disagreement():
    """The artifact list must not rot into a list of things that now agree —
    a stale exemption is a place a real error can hide."""
    for fips, (expected, geometry_said, why) in GEOMETRY_ARTIFACTS.items():
        assert fips_to_iso2(fips) == expected, fips
        assert expected != geometry_said, (
            f"{fips} no longer disagrees with geometry — drop it from "
            f"GEOMETRY_ARTIFACTS instead of carrying a dead exemption ({why})"
        )


@pytest.mark.parametrize(
    "fips,iso2,wrong_country",
    [
        ("CH", "CN", "Switzerland"),      # China read as Switzerland — 186 live rows
        ("RS", "RU", "Serbia"),           # Russia read as Serbia — 153
        ("GM", "DE", "Gambia"),           # Germany read as Gambia — 54
        ("BG", "BD", "Bulgaria"),         # Bangladesh read as Bulgaria — 44
        ("AU", "AT", "Australia"),        # Austria read as Australia — 11
        ("NG", "NE", "Nigeria"),          # Niger read as Nigeria — 10
        ("KN", "KP", "Saint Kitts"),      # North Korea read as Saint Kitts — 12
        ("SZ", "CH", "Eswatini"),         # Switzerland read as Eswatini — 7
        ("BA", "BH", "Bosnia"),           # Bahrain read as Bosnia — 24
        ("NI", "NG", "Nicaragua"),        # Nigeria read as Nicaragua — 274
        ("IS", "IL", "Iceland"),          # Israel read as Iceland — 192
        ("AS", "AU", "American Samoa"),   # Australia read as American Samoa — 284
    ],
)
def test_the_silent_misroutes_translate(fips, iso2, wrong_country):
    """Each of these is a FIPS code that is ALSO a valid ISO code for a different
    country — so treating it as ISO never errors, it just delivers the story to
    ``wrong_country``'s desk. These are the pairs B-6 exists for."""
    assert fips_to_iso2(fips) == iso2
    assert fips in COLLIDING_CODES


@pytest.mark.parametrize("fips,iso2", [("UK", "GB"), ("SP", "ES"), ("UP", "UA"),
                                       ("IZ", "IQ"), ("EI", "IE"), ("TU", "TR")])
def test_the_silent_droppers_translate(fips, iso2):
    """The quieter half: codes with no ISO assignment at all. Untranslated they
    match NO desk, so the story never arrives and nothing reports a miss — 487
    UK-coded rows alone."""
    assert fips_to_iso2(fips) == iso2


def test_unknown_and_malformed_input_says_nothing_rather_than_guessing():
    """``None`` means "say nothing". The caller must never fall back to passing
    the input through — that is the defect this module exists to stop."""
    for bad in (None, "", "   ", "X", "XXX", "12", "zz", "NT"):
        assert fips_to_iso2(bad) is None


def test_lookup_is_case_and_whitespace_insensitive():
    assert fips_to_iso2(" gm ") == "DE"
    assert fips_to_iso2("Gm") == "DE"


def test_every_mapped_value_is_a_real_iso_alpha2():
    """No typo may enter the table as a plausible-looking code."""
    pycountry = pytest.importorskip("pycountry")
    known = {c.alpha_2 for c in pycountry.countries}
    # XK (Kosovo) is user-assigned, not in ISO 3166-1 proper, but IS what the
    # substrate's own geometry resolver emits — so it is the correct target here.
    known.add("XK")
    unknown = sorted({v for v in FIPS_TO_ISO2.values() if v not in known})
    assert not unknown, f"not ISO 3166-1 alpha-2: {unknown}"


def test_collisions_are_derived_not_hand_listed():
    """COLLIDING_CODES must stay a function of the table, so it cannot drift."""
    expected = {c for c, i in FIPS_TO_ISO2.items()
                if c != i and c in set(FIPS_TO_ISO2.values())}
    assert COLLIDING_CODES == expected
    # Sanity: this is a large set, not an empty one that would pass vacuously.
    assert len(COLLIDING_CODES) > 50


# ---------------------------------------------------------------------------
# The boundary that applies it
# ---------------------------------------------------------------------------


def test_gdelt_files_stamps_iso_into_geo_and_keeps_fips_in_payload():
    from legba.data.sources.gdelt_files import row_to_signal
    from tests.data_pkg.test_source_gdelt_files import _make_ctx, _make_row

    row = _make_row()
    row["ActionGeo_CountryCode"] = "GM"   # FIPS Germany
    sig = row_to_signal(row, ctx=_make_ctx(), export_url="https://x/y.zip")

    assert sig.geo == ["DE"]                                  # routes to Germany
    assert "GM" not in sig.geo                                # not to Gambia
    assert sig.payload["geo"]["country_code_fips"] == "GM"    # raw value kept


def test_the_backfill_migrations_crosswalk_cannot_drift_from_the_modules():
    """Migration 0142 embeds the crosswalk as a SQL VALUES list, because a
    migration must be a self-contained artefact. That copy is the drift hazard:
    the module governs every FUTURE row, the SQL governs every PAST one, and a
    silent divergence would leave the substrate half-repaired in a way nothing
    reports. Re-derive it here and compare."""
    import re
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[1].parent
        / "src" / "legba" / "data" / "migrations"
        / "0142_backfill_gdelt_fips_geo.sql"
    ).read_text()
    # Only the VALUES body — the header prose quotes codes too.
    body = sql.split("FROM (VALUES", 1)[1].split(") AS x(", 1)[0]
    in_sql = dict(re.findall(r"\('([A-Z]{2})','([A-Z]{2})'\)", body))
    expected = {f: i for f, i in FIPS_TO_ISO2.items() if f != i}

    assert in_sql == expected, (
        "migration 0142's embedded crosswalk has drifted from "
        "legba.data._fips_iso.FIPS_TO_ISO2 — regenerate the migration"
    )
    # Identity pairs are deliberately excluded: they have nothing to repair, and
    # including them would make the UPDATE match rows it must not touch.
    assert not any(f == i for f, i in in_sql.items())


def test_gdelt_files_says_nothing_rather_than_guessing_on_an_untranslatable_code():
    """An unmappable code must leave ``geo`` EMPTY so the geocode filter resolves
    it from the row's own lat/lon — passing the raw code through would be the
    original defect, and stamping a guess would be worse."""
    from legba.data.sources.gdelt_files import row_to_signal
    from tests.data_pkg.test_source_gdelt_files import _make_ctx, _make_row

    row = _make_row()
    row["ActionGeo_CountryCode"] = "OS"   # observed live; not a FIPS country code
    sig = row_to_signal(row, ctx=_make_ctx(), export_url="https://x/y.zip")

    assert sig.geo == []
    assert sig.payload["geo"]["country_code_fips"] == "OS"
