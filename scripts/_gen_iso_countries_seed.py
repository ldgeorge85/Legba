# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One-shot generator: emit ``0019_iso_countries_seed.sql``.

Owned by the L-181 brief. Re-running regenerates the same file deterministically
because pycountry's underlying ISO-3166-1 dataset is fixed per pycountry
release, and the UN M49 region / subregion / languages mappings below are
inlined verbatim from the official UN Statistics Division M49 listing
(https://unstats.un.org/unsd/methodology/m49/) snapshot as of 2025-Q1.

This script is *not* runtime code — it produces the seed migration once.
The migration file is the source of truth at runtime.

Regenerate via::

    python3 scripts/_gen_iso_countries_seed.py > src/legba/data/migrations/0019_iso_countries_seed.sql
"""

from __future__ import annotations

import json
import sys

import pycountry


# ---------------------------------------------------------------------------
# UN M49 region + subregion mapping (alpha-2 → region, subregion)
# ---------------------------------------------------------------------------
# Source: UN Statistics Division M49 / Standard Country or Area Codes.
# Five geographic regions: Africa, Americas, Asia, Europe, Oceania, Antarctica
# (treated as a separate top-level region for the L-106 §3 worked-example
# `keep` predicate). Subregions per UN M49 ("Sub-Saharan Africa", etc.).
# Codes derived from the UN Statistics Division standard country area codes
# (latest revision available without external network fetch at generation time).
M49: dict[str, tuple[str, str]] = {
    # --- Africa ---
    "DZ": ("Africa", "Northern Africa"),
    "EG": ("Africa", "Northern Africa"),
    "LY": ("Africa", "Northern Africa"),
    "MA": ("Africa", "Northern Africa"),
    "SD": ("Africa", "Northern Africa"),
    "TN": ("Africa", "Northern Africa"),
    "EH": ("Africa", "Northern Africa"),
    "BF": ("Africa", "Sub-Saharan Africa"),
    "BI": ("Africa", "Sub-Saharan Africa"),
    "BJ": ("Africa", "Sub-Saharan Africa"),
    "BW": ("Africa", "Sub-Saharan Africa"),
    "CD": ("Africa", "Sub-Saharan Africa"),
    "CF": ("Africa", "Sub-Saharan Africa"),
    "CG": ("Africa", "Sub-Saharan Africa"),
    "CI": ("Africa", "Sub-Saharan Africa"),
    "CM": ("Africa", "Sub-Saharan Africa"),
    "CV": ("Africa", "Sub-Saharan Africa"),
    "DJ": ("Africa", "Sub-Saharan Africa"),
    "ER": ("Africa", "Sub-Saharan Africa"),
    "ET": ("Africa", "Sub-Saharan Africa"),
    "GA": ("Africa", "Sub-Saharan Africa"),
    "GH": ("Africa", "Sub-Saharan Africa"),
    "GM": ("Africa", "Sub-Saharan Africa"),
    "GN": ("Africa", "Sub-Saharan Africa"),
    "GQ": ("Africa", "Sub-Saharan Africa"),
    "GW": ("Africa", "Sub-Saharan Africa"),
    "IO": ("Africa", "Sub-Saharan Africa"),
    "KE": ("Africa", "Sub-Saharan Africa"),
    "KM": ("Africa", "Sub-Saharan Africa"),
    "LR": ("Africa", "Sub-Saharan Africa"),
    "LS": ("Africa", "Sub-Saharan Africa"),
    "MG": ("Africa", "Sub-Saharan Africa"),
    "ML": ("Africa", "Sub-Saharan Africa"),
    "MR": ("Africa", "Sub-Saharan Africa"),
    "MU": ("Africa", "Sub-Saharan Africa"),
    "MW": ("Africa", "Sub-Saharan Africa"),
    "MZ": ("Africa", "Sub-Saharan Africa"),
    "NA": ("Africa", "Sub-Saharan Africa"),
    "NE": ("Africa", "Sub-Saharan Africa"),
    "NG": ("Africa", "Sub-Saharan Africa"),
    "RE": ("Africa", "Sub-Saharan Africa"),
    "RW": ("Africa", "Sub-Saharan Africa"),
    "SC": ("Africa", "Sub-Saharan Africa"),
    "SH": ("Africa", "Sub-Saharan Africa"),
    "SL": ("Africa", "Sub-Saharan Africa"),
    "SN": ("Africa", "Sub-Saharan Africa"),
    "SO": ("Africa", "Sub-Saharan Africa"),
    "SS": ("Africa", "Sub-Saharan Africa"),
    "ST": ("Africa", "Sub-Saharan Africa"),
    "SZ": ("Africa", "Sub-Saharan Africa"),
    "TD": ("Africa", "Sub-Saharan Africa"),
    "TF": ("Africa", "Sub-Saharan Africa"),
    "TG": ("Africa", "Sub-Saharan Africa"),
    "TZ": ("Africa", "Sub-Saharan Africa"),
    "UG": ("Africa", "Sub-Saharan Africa"),
    "YT": ("Africa", "Sub-Saharan Africa"),
    "ZA": ("Africa", "Sub-Saharan Africa"),
    "ZM": ("Africa", "Sub-Saharan Africa"),
    "ZW": ("Africa", "Sub-Saharan Africa"),
    "AO": ("Africa", "Sub-Saharan Africa"),

    # --- Americas ---
    "AG": ("Americas", "Latin America and the Caribbean"),
    "AI": ("Americas", "Latin America and the Caribbean"),
    "AR": ("Americas", "Latin America and the Caribbean"),
    "AW": ("Americas", "Latin America and the Caribbean"),
    "BB": ("Americas", "Latin America and the Caribbean"),
    "BL": ("Americas", "Latin America and the Caribbean"),
    "BO": ("Americas", "Latin America and the Caribbean"),
    "BQ": ("Americas", "Latin America and the Caribbean"),
    "BR": ("Americas", "Latin America and the Caribbean"),
    "BS": ("Americas", "Latin America and the Caribbean"),
    "BZ": ("Americas", "Latin America and the Caribbean"),
    "CL": ("Americas", "Latin America and the Caribbean"),
    "CO": ("Americas", "Latin America and the Caribbean"),
    "CR": ("Americas", "Latin America and the Caribbean"),
    "CU": ("Americas", "Latin America and the Caribbean"),
    "CW": ("Americas", "Latin America and the Caribbean"),
    "DM": ("Americas", "Latin America and the Caribbean"),
    "DO": ("Americas", "Latin America and the Caribbean"),
    "EC": ("Americas", "Latin America and the Caribbean"),
    "FK": ("Americas", "Latin America and the Caribbean"),
    "GD": ("Americas", "Latin America and the Caribbean"),
    "GF": ("Americas", "Latin America and the Caribbean"),
    "GP": ("Americas", "Latin America and the Caribbean"),
    "GT": ("Americas", "Latin America and the Caribbean"),
    "GY": ("Americas", "Latin America and the Caribbean"),
    "HN": ("Americas", "Latin America and the Caribbean"),
    "HT": ("Americas", "Latin America and the Caribbean"),
    "JM": ("Americas", "Latin America and the Caribbean"),
    "KN": ("Americas", "Latin America and the Caribbean"),
    "KY": ("Americas", "Latin America and the Caribbean"),
    "LC": ("Americas", "Latin America and the Caribbean"),
    "MF": ("Americas", "Latin America and the Caribbean"),
    "MQ": ("Americas", "Latin America and the Caribbean"),
    "MS": ("Americas", "Latin America and the Caribbean"),
    "MX": ("Americas", "Latin America and the Caribbean"),
    "NI": ("Americas", "Latin America and the Caribbean"),
    "PA": ("Americas", "Latin America and the Caribbean"),
    "PE": ("Americas", "Latin America and the Caribbean"),
    "PR": ("Americas", "Latin America and the Caribbean"),
    "PY": ("Americas", "Latin America and the Caribbean"),
    "SR": ("Americas", "Latin America and the Caribbean"),
    "SV": ("Americas", "Latin America and the Caribbean"),
    "SX": ("Americas", "Latin America and the Caribbean"),
    "TC": ("Americas", "Latin America and the Caribbean"),
    "TT": ("Americas", "Latin America and the Caribbean"),
    "UY": ("Americas", "Latin America and the Caribbean"),
    "VC": ("Americas", "Latin America and the Caribbean"),
    "VE": ("Americas", "Latin America and the Caribbean"),
    "VG": ("Americas", "Latin America and the Caribbean"),
    "VI": ("Americas", "Latin America and the Caribbean"),
    "BM": ("Americas", "Northern America"),
    "CA": ("Americas", "Northern America"),
    "GL": ("Americas", "Northern America"),
    "PM": ("Americas", "Northern America"),
    "US": ("Americas", "Northern America"),
    "UM": ("Americas", "Northern America"),

    # --- Asia ---
    "TM": ("Asia", "Central Asia"),
    "TJ": ("Asia", "Central Asia"),
    "KG": ("Asia", "Central Asia"),
    "KZ": ("Asia", "Central Asia"),
    "UZ": ("Asia", "Central Asia"),
    "CN": ("Asia", "Eastern Asia"),
    "HK": ("Asia", "Eastern Asia"),
    "JP": ("Asia", "Eastern Asia"),
    "KP": ("Asia", "Eastern Asia"),
    "KR": ("Asia", "Eastern Asia"),
    "MN": ("Asia", "Eastern Asia"),
    "MO": ("Asia", "Eastern Asia"),
    "TW": ("Asia", "Eastern Asia"),
    "BN": ("Asia", "South-eastern Asia"),
    "ID": ("Asia", "South-eastern Asia"),
    "KH": ("Asia", "South-eastern Asia"),
    "LA": ("Asia", "South-eastern Asia"),
    "MM": ("Asia", "South-eastern Asia"),
    "MY": ("Asia", "South-eastern Asia"),
    "PH": ("Asia", "South-eastern Asia"),
    "SG": ("Asia", "South-eastern Asia"),
    "TH": ("Asia", "South-eastern Asia"),
    "TL": ("Asia", "South-eastern Asia"),
    "VN": ("Asia", "South-eastern Asia"),
    "AF": ("Asia", "Southern Asia"),
    "BD": ("Asia", "Southern Asia"),
    "BT": ("Asia", "Southern Asia"),
    "IN": ("Asia", "Southern Asia"),
    "IR": ("Asia", "Southern Asia"),
    "LK": ("Asia", "Southern Asia"),
    "MV": ("Asia", "Southern Asia"),
    "NP": ("Asia", "Southern Asia"),
    "PK": ("Asia", "Southern Asia"),
    "AE": ("Asia", "Western Asia"),
    "AM": ("Asia", "Western Asia"),
    "AZ": ("Asia", "Western Asia"),
    "BH": ("Asia", "Western Asia"),
    "CY": ("Asia", "Western Asia"),
    "GE": ("Asia", "Western Asia"),
    "IL": ("Asia", "Western Asia"),
    "IQ": ("Asia", "Western Asia"),
    "JO": ("Asia", "Western Asia"),
    "KW": ("Asia", "Western Asia"),
    "LB": ("Asia", "Western Asia"),
    "OM": ("Asia", "Western Asia"),
    "PS": ("Asia", "Western Asia"),
    "QA": ("Asia", "Western Asia"),
    "SA": ("Asia", "Western Asia"),
    "SY": ("Asia", "Western Asia"),
    "TR": ("Asia", "Western Asia"),
    "YE": ("Asia", "Western Asia"),

    # --- Europe ---
    "BY": ("Europe", "Eastern Europe"),
    "BG": ("Europe", "Eastern Europe"),
    "CZ": ("Europe", "Eastern Europe"),
    "HU": ("Europe", "Eastern Europe"),
    "MD": ("Europe", "Eastern Europe"),
    "PL": ("Europe", "Eastern Europe"),
    "RO": ("Europe", "Eastern Europe"),
    "RU": ("Europe", "Eastern Europe"),
    "SK": ("Europe", "Eastern Europe"),
    "UA": ("Europe", "Eastern Europe"),
    "AX": ("Europe", "Northern Europe"),
    "DK": ("Europe", "Northern Europe"),
    "EE": ("Europe", "Northern Europe"),
    "FI": ("Europe", "Northern Europe"),
    "FO": ("Europe", "Northern Europe"),
    "GB": ("Europe", "Northern Europe"),
    "GG": ("Europe", "Northern Europe"),
    "IE": ("Europe", "Northern Europe"),
    "IM": ("Europe", "Northern Europe"),
    "IS": ("Europe", "Northern Europe"),
    "JE": ("Europe", "Northern Europe"),
    "LT": ("Europe", "Northern Europe"),
    "LV": ("Europe", "Northern Europe"),
    "NO": ("Europe", "Northern Europe"),
    "SE": ("Europe", "Northern Europe"),
    "SJ": ("Europe", "Northern Europe"),
    "AD": ("Europe", "Southern Europe"),
    "AL": ("Europe", "Southern Europe"),
    "BA": ("Europe", "Southern Europe"),
    "ES": ("Europe", "Southern Europe"),
    "GI": ("Europe", "Southern Europe"),
    "GR": ("Europe", "Southern Europe"),
    "HR": ("Europe", "Southern Europe"),
    "IT": ("Europe", "Southern Europe"),
    "ME": ("Europe", "Southern Europe"),
    "MK": ("Europe", "Southern Europe"),
    "MT": ("Europe", "Southern Europe"),
    "PT": ("Europe", "Southern Europe"),
    "RS": ("Europe", "Southern Europe"),
    "SI": ("Europe", "Southern Europe"),
    "SM": ("Europe", "Southern Europe"),
    "VA": ("Europe", "Southern Europe"),
    "XK": ("Europe", "Southern Europe"),
    "AT": ("Europe", "Western Europe"),
    "BE": ("Europe", "Western Europe"),
    "CH": ("Europe", "Western Europe"),
    "DE": ("Europe", "Western Europe"),
    "FR": ("Europe", "Western Europe"),
    "LI": ("Europe", "Western Europe"),
    "LU": ("Europe", "Western Europe"),
    "MC": ("Europe", "Western Europe"),
    "NL": ("Europe", "Western Europe"),

    # --- Oceania ---
    "AU": ("Oceania", "Australia and New Zealand"),
    "CX": ("Oceania", "Australia and New Zealand"),
    "CC": ("Oceania", "Australia and New Zealand"),
    "HM": ("Oceania", "Australia and New Zealand"),
    "NF": ("Oceania", "Australia and New Zealand"),
    "NZ": ("Oceania", "Australia and New Zealand"),
    "FJ": ("Oceania", "Melanesia"),
    "NC": ("Oceania", "Melanesia"),
    "PG": ("Oceania", "Melanesia"),
    "SB": ("Oceania", "Melanesia"),
    "VU": ("Oceania", "Melanesia"),
    "FM": ("Oceania", "Micronesia"),
    "GU": ("Oceania", "Micronesia"),
    "KI": ("Oceania", "Micronesia"),
    "MH": ("Oceania", "Micronesia"),
    "MP": ("Oceania", "Micronesia"),
    "NR": ("Oceania", "Micronesia"),
    "PW": ("Oceania", "Micronesia"),
    "AS": ("Oceania", "Polynesia"),
    "CK": ("Oceania", "Polynesia"),
    "PF": ("Oceania", "Polynesia"),
    "NU": ("Oceania", "Polynesia"),
    "PN": ("Oceania", "Polynesia"),
    "TK": ("Oceania", "Polynesia"),
    "TO": ("Oceania", "Polynesia"),
    "TV": ("Oceania", "Polynesia"),
    "WF": ("Oceania", "Polynesia"),
    "WS": ("Oceania", "Polynesia"),

    # --- Antarctica (own region per L-106 §3 worked example) ---
    "AQ": ("Antarctica", "Antarctica"),
    "BV": ("Antarctica", "Antarctica"),
    "GS": ("Antarctica", "Antarctica"),
}


# ---------------------------------------------------------------------------
# Languages mapping (alpha-2 → list of BCP-47 locales) — non-exhaustive seed.
# ---------------------------------------------------------------------------
# Pulled from CLDR locale-likely-subtags / Wikipedia ISO-639-1 → country
# correspondence. Coverage spans the largest-population country per locale +
# the L-106 worked-example set + multilingual countries with 2+ official
# locales. Countries missing from this table get `[]` and the relabel
# evaluator's lookup_languages action falls back to the descriptor-provided
# fallback list (typically `['en']`).
LANGS: dict[str, list[str]] = {
    "AD": ["ca-AD"],
    "AE": ["ar-AE"],
    "AF": ["ps-AF", "fa-AF"],
    "AG": ["en-AG"],
    "AI": ["en-AI"],
    "AL": ["sq-AL"],
    "AM": ["hy-AM"],
    "AO": ["pt-AO"],
    "AQ": [],
    "AR": ["es-AR"],
    "AS": ["en-AS", "sm-AS"],
    "AT": ["de-AT"],
    "AU": ["en-AU"],
    "AW": ["nl-AW", "pap-AW"],
    "AX": ["sv-AX"],
    "AZ": ["az-AZ"],
    "BA": ["bs-BA", "hr-BA", "sr-BA"],
    "BB": ["en-BB"],
    "BD": ["bn-BD"],
    "BE": ["nl-BE", "fr-BE", "de-BE"],
    "BF": ["fr-BF"],
    "BG": ["bg-BG"],
    "BH": ["ar-BH"],
    "BI": ["fr-BI", "rn-BI"],
    "BJ": ["fr-BJ"],
    "BL": ["fr-BL"],
    "BM": ["en-BM"],
    "BN": ["ms-BN"],
    "BO": ["es-BO"],
    "BQ": ["nl-BQ"],
    "BR": ["pt-BR"],
    "BS": ["en-BS"],
    "BT": ["dz-BT"],
    "BV": [],
    "BW": ["en-BW", "tn-BW"],
    "BY": ["be-BY", "ru-BY"],
    "BZ": ["en-BZ"],
    "CA": ["en-CA", "fr-CA"],
    "CC": ["en-CC"],
    "CD": ["fr-CD"],
    "CF": ["fr-CF", "sg-CF"],
    "CG": ["fr-CG"],
    "CH": ["de-CH", "fr-CH", "it-CH"],
    "CI": ["fr-CI"],
    "CK": ["en-CK"],
    "CL": ["es-CL"],
    "CM": ["fr-CM", "en-CM"],
    "CN": ["zh-CN"],
    "CO": ["es-CO"],
    "CR": ["es-CR"],
    "CU": ["es-CU"],
    "CV": ["pt-CV"],
    "CW": ["nl-CW", "pap-CW"],
    "CX": ["en-CX"],
    "CY": ["el-CY", "tr-CY"],
    "CZ": ["cs-CZ"],
    "DE": ["de-DE"],
    "DJ": ["fr-DJ", "ar-DJ"],
    "DK": ["da-DK"],
    "DM": ["en-DM"],
    "DO": ["es-DO"],
    "DZ": ["ar-DZ"],
    "EC": ["es-EC"],
    "EE": ["et-EE"],
    "EG": ["ar-EG"],
    "EH": ["ar-EH"],
    "ER": ["ti-ER", "ar-ER", "en-ER"],
    "ES": ["es-ES"],
    "ET": ["am-ET"],
    "FI": ["fi-FI", "sv-FI"],
    "FJ": ["en-FJ", "fj-FJ"],
    "FK": ["en-FK"],
    "FM": ["en-FM"],
    "FO": ["fo-FO"],
    "FR": ["fr-FR"],
    "GA": ["fr-GA"],
    "GB": ["en-GB"],
    "GD": ["en-GD"],
    "GE": ["ka-GE"],
    "GF": ["fr-GF"],
    "GG": ["en-GG"],
    "GH": ["en-GH"],
    "GI": ["en-GI"],
    "GL": ["kl-GL"],
    "GM": ["en-GM"],
    "GN": ["fr-GN"],
    "GP": ["fr-GP"],
    "GQ": ["es-GQ", "fr-GQ", "pt-GQ"],
    "GR": ["el-GR"],
    "GS": [],
    "GT": ["es-GT"],
    "GU": ["en-GU"],
    "GW": ["pt-GW"],
    "GY": ["en-GY"],
    "HK": ["zh-HK", "en-HK"],
    "HM": [],
    "HN": ["es-HN"],
    "HR": ["hr-HR"],
    "HT": ["fr-HT", "ht-HT"],
    "HU": ["hu-HU"],
    "ID": ["id-ID"],
    "IE": ["en-IE", "ga-IE"],
    "IL": ["he-IL", "ar-IL"],
    "IM": ["en-IM"],
    "IN": ["hi-IN", "en-IN"],
    "IO": ["en-IO"],
    "IQ": ["ar-IQ", "ku-IQ"],
    "IR": ["fa-IR"],
    "IS": ["is-IS"],
    "IT": ["it-IT"],
    "JE": ["en-JE"],
    "JM": ["en-JM"],
    "JO": ["ar-JO"],
    "JP": ["ja-JP"],
    "KE": ["en-KE", "sw-KE"],
    "KG": ["ky-KG", "ru-KG"],
    "KH": ["km-KH"],
    "KI": ["en-KI"],
    "KM": ["ar-KM", "fr-KM"],
    "KN": ["en-KN"],
    "KP": ["ko-KP"],
    "KR": ["ko-KR"],
    "KW": ["ar-KW"],
    "KY": ["en-KY"],
    "KZ": ["kk-KZ", "ru-KZ"],
    "LA": ["lo-LA"],
    "LB": ["ar-LB"],
    "LC": ["en-LC"],
    "LI": ["de-LI"],
    "LK": ["si-LK", "ta-LK"],
    "LR": ["en-LR"],
    "LS": ["en-LS", "st-LS"],
    "LT": ["lt-LT"],
    "LU": ["lb-LU", "fr-LU", "de-LU"],
    "LV": ["lv-LV"],
    "LY": ["ar-LY"],
    "MA": ["ar-MA"],
    "MC": ["fr-MC"],
    "MD": ["ro-MD"],
    "ME": ["sr-ME"],
    "MF": ["fr-MF"],
    "MG": ["fr-MG", "mg-MG"],
    "MH": ["en-MH", "mh-MH"],
    "MK": ["mk-MK"],
    "ML": ["fr-ML"],
    "MM": ["my-MM"],
    "MN": ["mn-MN"],
    "MO": ["zh-MO", "pt-MO"],
    "MP": ["en-MP"],
    "MQ": ["fr-MQ"],
    "MR": ["ar-MR"],
    "MS": ["en-MS"],
    "MT": ["mt-MT", "en-MT"],
    "MU": ["en-MU", "fr-MU"],
    "MV": ["dv-MV"],
    "MW": ["en-MW", "ny-MW"],
    "MX": ["es-MX"],
    "MY": ["ms-MY"],
    "MZ": ["pt-MZ"],
    "NA": ["en-NA"],
    "NC": ["fr-NC"],
    "NE": ["fr-NE"],
    "NF": ["en-NF"],
    "NG": ["en-NG"],
    "NI": ["es-NI"],
    "NL": ["nl-NL"],
    "NO": ["no-NO", "nb-NO", "nn-NO"],
    "NP": ["ne-NP"],
    "NR": ["en-NR", "na-NR"],
    "NU": ["en-NU"],
    "NZ": ["en-NZ", "mi-NZ"],
    "OM": ["ar-OM"],
    "PA": ["es-PA"],
    "PE": ["es-PE"],
    "PF": ["fr-PF"],
    "PG": ["en-PG"],
    "PH": ["en-PH", "tl-PH"],
    "PK": ["ur-PK", "en-PK"],
    "PL": ["pl-PL"],
    "PM": ["fr-PM"],
    "PN": ["en-PN"],
    "PR": ["es-PR", "en-PR"],
    "PS": ["ar-PS"],
    "PT": ["pt-PT"],
    "PW": ["en-PW"],
    "PY": ["es-PY", "gn-PY"],
    "QA": ["ar-QA"],
    "RE": ["fr-RE"],
    "RO": ["ro-RO"],
    "RS": ["sr-RS"],
    "RU": ["ru-RU"],
    "RW": ["rw-RW", "en-RW", "fr-RW"],
    "SA": ["ar-SA"],
    "SB": ["en-SB"],
    "SC": ["en-SC", "fr-SC"],
    "SD": ["ar-SD", "en-SD"],
    "SE": ["sv-SE"],
    "SG": ["en-SG", "zh-SG", "ms-SG", "ta-SG"],
    "SH": ["en-SH"],
    "SI": ["sl-SI"],
    "SJ": ["no-SJ"],
    "SK": ["sk-SK"],
    "SL": ["en-SL"],
    "SM": ["it-SM"],
    "SN": ["fr-SN"],
    "SO": ["so-SO", "ar-SO"],
    "SR": ["nl-SR"],
    "SS": ["en-SS"],
    "ST": ["pt-ST"],
    "SV": ["es-SV"],
    "SX": ["nl-SX", "en-SX"],
    "SY": ["ar-SY"],
    "SZ": ["en-SZ", "ss-SZ"],
    "TC": ["en-TC"],
    "TD": ["fr-TD", "ar-TD"],
    "TF": ["fr-TF"],
    "TG": ["fr-TG"],
    "TH": ["th-TH"],
    "TJ": ["tg-TJ"],
    "TK": ["en-TK"],
    "TL": ["pt-TL"],
    "TM": ["tk-TM"],
    "TN": ["ar-TN"],
    "TO": ["en-TO", "to-TO"],
    "TR": ["tr-TR"],
    "TT": ["en-TT"],
    "TV": ["en-TV"],
    "TW": ["zh-TW"],
    "TZ": ["sw-TZ", "en-TZ"],
    "UA": ["uk-UA"],
    "UG": ["en-UG", "sw-UG"],
    "UM": ["en-UM"],
    "US": ["en-US"],
    "UY": ["es-UY"],
    "UZ": ["uz-UZ"],
    "VA": ["it-VA", "la-VA"],
    "VC": ["en-VC"],
    "VE": ["es-VE"],
    "VG": ["en-VG"],
    "VI": ["en-VI"],
    "VN": ["vi-VN"],
    "VU": ["bi-VU", "en-VU", "fr-VU"],
    "WF": ["fr-WF"],
    "WS": ["en-WS", "sm-WS"],
    "XK": ["sq-XK", "sr-XK"],
    "YE": ["ar-YE"],
    "YT": ["fr-YT"],
    "ZA": ["en-ZA", "af-ZA", "zu-ZA"],
    "ZM": ["en-ZM"],
    "ZW": ["en-ZW", "sn-ZW", "nd-ZW"],
}


def sql_escape(s: str) -> str:
    """Single-quote escape for inline SQL string literals."""
    return s.replace("'", "''")


def main() -> int:
    rows: list[tuple[str, str, str, str, str, str, str]] = []
    missing_m49: list[str] = []
    for c in sorted(pycountry.countries, key=lambda x: x.alpha_2):
        iso2 = c.alpha_2
        iso3 = c.alpha_3
        name = c.name
        official = getattr(c, "official_name", "") or name
        numeric = c.numeric
        region, subregion = M49.get(iso2, ("", ""))
        if not region:
            missing_m49.append(iso2)
        languages_json = json.dumps(LANGS.get(iso2, []), separators=(",", ":"))
        rows.append((iso2, iso3, numeric, name, official, region, subregion, languages_json))

    out = sys.stdout
    out.write("-- L-181 migration 0019 — ISO 3166-1 country snapshot.\n")
    out.write("--\n")
    out.write("-- Per-row snapshot of the ISO 3166-1 country list, the default backing\n")
    out.write("-- list for the L-181 `country_list_discovery` discovery kind. One row per\n")
    out.write("-- country / dependent territory recognised by ISO 3166-1 (249 entries as\n")
    out.write("-- of the pycountry snapshot used at generation time). Columns mirror what\n")
    out.write("-- the kind's CandidateTarget.label_set exposes:\n")
    out.write("--\n")
    out.write("--   iso2         ISO 3166-1 alpha-2 (`BR`, `US`, …) — the natural key.\n")
    out.write("--   iso3         ISO 3166-1 alpha-3 (`BRA`, `USA`, …).\n")
    out.write("--   numeric      ISO 3166-1 numeric (`076`, `840`, …).\n")
    out.write("--   name         Short / common name (`Brazil`, `United States`).\n")
    out.write("--   official     Official name when distinct from `name`.\n")
    out.write("--   region       UN M49 region (`Americas`, `Africa`, `Asia`, `Europe`,\n")
    out.write("--                `Oceania`, `Antarctica`). The `keep`/`drop` predicates in the\n")
    out.write("--                L-106 §3 worked example branch on this column.\n")
    out.write("--   subregion    UN M49 subregion (`Northern America`, `Latin America and\n")
    out.write("--                the Caribbean`, …).\n")
    out.write("--   languages    JSONB list of BCP-47 locale strings, ordered by usage.\n")
    out.write("--                Used by the `lookup_languages` relabel action.\n")
    out.write("--\n")
    out.write("-- Generated by `scripts/_gen_iso_countries_seed.py`. Re-running the\n")
    out.write("-- generator is deterministic — pycountry's ISO-3166-1 dataset is fixed\n")
    out.write("-- per release, and the M49 + languages tables in the generator are\n")
    out.write("-- inlined snapshots. Don't hand-edit this file; regenerate.\n")
    out.write("\n")
    out.write("CREATE TABLE IF NOT EXISTS iso_countries (\n")
    out.write("    iso2        TEXT PRIMARY KEY,                  -- ISO 3166-1 alpha-2\n")
    out.write("    iso3        TEXT NOT NULL UNIQUE,              -- ISO 3166-1 alpha-3\n")
    out.write("    numeric     TEXT NOT NULL,                     -- ISO 3166-1 numeric\n")
    out.write("    name        TEXT NOT NULL,                     -- Common name\n")
    out.write("    official    TEXT NOT NULL DEFAULT '',          -- Official name\n")
    out.write("    region      TEXT NOT NULL DEFAULT '',          -- UN M49 region\n")
    out.write("    subregion   TEXT NOT NULL DEFAULT '',          -- UN M49 subregion\n")
    out.write("    languages   JSONB NOT NULL DEFAULT '[]'::JSONB -- BCP-47 list\n")
    out.write(");\n")
    out.write("\n")
    out.write("CREATE INDEX IF NOT EXISTS iso_countries_region_idx ON iso_countries(region);\n")
    out.write("CREATE INDEX IF NOT EXISTS iso_countries_subregion_idx ON iso_countries(subregion);\n")
    out.write("\n")
    out.write(f"-- {len(rows)} rows.\n")
    out.write("INSERT INTO iso_countries (iso2, iso3, numeric, name, official, region, subregion, languages) VALUES\n")
    values_lines = []
    for iso2, iso3, numeric, name, official, region, subregion, langs in rows:
        values_lines.append(
            "    ("
            f"'{sql_escape(iso2)}', "
            f"'{sql_escape(iso3)}', "
            f"'{sql_escape(numeric)}', "
            f"'{sql_escape(name)}', "
            f"'{sql_escape(official)}', "
            f"'{sql_escape(region)}', "
            f"'{sql_escape(subregion)}', "
            f"'{sql_escape(langs)}'::JSONB"
            ")"
        )
    out.write(",\n".join(values_lines))
    out.write("\nON CONFLICT (iso2) DO NOTHING;\n")

    if missing_m49:
        # Surface but don't fail — the kind tolerates blank region.
        sys.stderr.write(f"WARN: {len(missing_m49)} ISO-3166 codes missing M49 mapping: {missing_m49}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
