# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FIPS 10-4 → ISO 3166-1 alpha-2 country crosswalk (B-6).

WHY THIS EXISTS. GDELT's ``ActionGeo_CountryCode`` / ``Actor*CountryCode``
columns are **FIPS 10-4**, the CIA World Factbook codelist — not ISO 3166. The
two overlap enough to look interchangeable and disagree exactly where it hurts:
both are two uppercase letters, roughly half the codes are identical, and the
rest are silently, confidently wrong.

The failure is not a parse error. It is a *routing* error, because a country
desk subscribes on ``geo && ARRAY['XX']`` — so a FIPS code that happens to be a
valid ISO code for some OTHER country delivers a story to the wrong desk with no
error anywhere. Measured on the live substrate 2026-08-03, GDELT rows tagged:

    CH  meaning China          → read as Switzerland   (186 rows)
    RS  meaning Russia         → read as Serbia        (153 rows)
    GM  meaning Germany        → read as Gambia         (54 rows)
    BG  meaning Bangladesh     → read as Bulgaria       (44 rows)
    AU  meaning Austria        → read as Australia      (11 rows)
    NG  meaning Niger          → read as Nigeria        (10 rows)
    KN  meaning North Korea    → read as St Kitts       (12 rows)
    SZ  meaning Switzerland    → read as Eswatini        (7 rows)

...and 30-odd more pairs. The other half of the damage is quieter: codes with no
ISO assignment at all (``UK`` for the United Kingdom — ISO says ``GB`` — 487
rows; ``SP`` for Spain, 210; ``UP`` for Ukraine, 106) route to NO desk, so the
story simply never arrives and nothing reports a miss.

HOW THIS TABLE WAS CHECKED. Every mapping below was cross-checked against an
INDEPENDENT source of truth already present in the substrate: the geocode
filter resolves each GDELT row's ISO2 from its ``lat``/``lon`` through
Natural-Earth admin-0 polygons, with no knowledge of FIPS. Comparing this table
against 3,554 live rows so resolved, every dominant pair agrees. The handful
that do not are geometry artifacts, not crosswalk errors, and are named
explicitly in ``tests/data_pkg/test_fips_iso_crosswalk.py`` so a future reader
does not "fix" the table to chase them: Gibraltar's polygon resolves inside
Spain, Hong Kong's inside China, Monaco's inside France, Singapore's within a
kilometre of Malaysia.

SCOPE. Country-level codes only. FIPS also codes subdivisions (``ADM1Code`` is
``<country><adm1>``); that is a different, larger table and no consumer needs it
yet. Codes with no ISO 3166 counterpart are deliberately ABSENT rather than
guessed — the uninhabited French Indian-Ocean claims (Bassas da India, Europa,
Glorioso, Juan de Nova, Tromelin), the disputed Paracel and Spratly groups, and
the dissolved Netherlands Antilles. An absent code resolves to ``None``, which
means "say nothing" — the caller then leaves geo to the geometry resolver rather
than stamping a guess.
"""

from __future__ import annotations

#: FIPS 10-4 country code → ISO 3166-1 alpha-2.
#:
#: Identity pairs (``AF`` → ``AF``) are listed explicitly rather than implied, so
#: this dict is a COMPLETE statement about every code GDELT can emit: a lookup
#: miss means "not a country-level FIPS code", never "identical, assume it".
FIPS_TO_ISO2: dict[str, str] = {
    "AA": "AW",  # Aruba
    "AC": "AG",  # Antigua and Barbuda
    "AE": "AE",  # United Arab Emirates
    "AF": "AF",  # Afghanistan
    "AG": "DZ",  # Algeria
    "AJ": "AZ",  # Azerbaijan
    "AL": "AL",  # Albania
    "AM": "AM",  # Armenia
    "AN": "AD",  # Andorra
    "AO": "AO",  # Angola
    "AQ": "AS",  # American Samoa
    "AR": "AR",  # Argentina
    "AS": "AU",  # Australia
    "AT": "AU",  # Ashmore and Cartier Islands (Australian external territory)
    "AU": "AT",  # Austria
    "AV": "AI",  # Anguilla
    "AY": "AQ",  # Antarctica
    "BA": "BH",  # Bahrain
    "BB": "BB",  # Barbados
    "BC": "BW",  # Botswana
    "BD": "BM",  # Bermuda
    "BE": "BE",  # Belgium
    "BF": "BS",  # Bahamas
    "BG": "BD",  # Bangladesh
    "BH": "BZ",  # Belize
    "BK": "BA",  # Bosnia and Herzegovina
    "BL": "BO",  # Bolivia
    "BM": "MM",  # Burma / Myanmar
    "BN": "BJ",  # Benin
    "BO": "BY",  # Belarus
    "BP": "SB",  # Solomon Islands
    "BQ": "UM",  # Navassa Island
    "BR": "BR",  # Brazil
    "BT": "BT",  # Bhutan
    "BU": "BG",  # Bulgaria
    "BV": "BV",  # Bouvet Island
    "BX": "BN",  # Brunei
    "BY": "BI",  # Burundi
    "CA": "CA",  # Canada
    "CB": "KH",  # Cambodia
    "CD": "TD",  # Chad
    "CE": "LK",  # Sri Lanka
    "CF": "CG",  # Congo (Brazzaville)
    "CG": "CD",  # Congo (Kinshasa) — the DRC
    "CH": "CN",  # China
    "CI": "CL",  # Chile
    "CJ": "KY",  # Cayman Islands
    "CK": "CC",  # Cocos (Keeling) Islands
    "CM": "CM",  # Cameroon
    "CN": "KM",  # Comoros
    "CO": "CO",  # Colombia
    "CQ": "MP",  # Northern Mariana Islands
    "CR": "AU",  # Coral Sea Islands (Australian external territory)
    "CS": "CR",  # Costa Rica
    "CT": "CF",  # Central African Republic
    "CU": "CU",  # Cuba
    "CV": "CV",  # Cabo Verde
    "CW": "CK",  # Cook Islands
    "CY": "CY",  # Cyprus
    "DA": "DK",  # Denmark
    "DJ": "DJ",  # Djibouti
    "DO": "DM",  # Dominica
    "DQ": "UM",  # Jarvis Island
    "DR": "DO",  # Dominican Republic
    "EC": "EC",  # Ecuador
    "EG": "EG",  # Egypt
    "EI": "IE",  # Ireland
    "EK": "GQ",  # Equatorial Guinea
    "EN": "EE",  # Estonia
    "ER": "ER",  # Eritrea
    "ES": "SV",  # El Salvador
    "ET": "ET",  # Ethiopia
    "EZ": "CZ",  # Czechia
    "FG": "GF",  # French Guiana
    "FI": "FI",  # Finland
    "FJ": "FJ",  # Fiji
    "FK": "FK",  # Falkland Islands
    "FM": "FM",  # Micronesia
    "FO": "FO",  # Faroe Islands
    "FP": "PF",  # French Polynesia
    "FR": "FR",  # France
    "FS": "TF",  # French Southern and Antarctic Lands
    "GA": "GM",  # Gambia
    "GB": "GA",  # Gabon
    "GG": "GE",  # Georgia
    "GH": "GH",  # Ghana
    "GI": "GI",  # Gibraltar
    "GJ": "GD",  # Grenada
    "GK": "GG",  # Guernsey
    "GL": "GL",  # Greenland
    "GM": "DE",  # Germany
    "GP": "GP",  # Guadeloupe
    "GQ": "GU",  # Guam
    "GR": "GR",  # Greece
    "GT": "GT",  # Guatemala
    "GV": "GN",  # Guinea
    "GY": "GY",  # Guyana
    "HA": "HT",  # Haiti
    "HK": "HK",  # Hong Kong
    "HM": "HM",  # Heard Island and McDonald Islands
    "HO": "HN",  # Honduras
    "HQ": "UM",  # Howland Island
    "HR": "HR",  # Croatia
    "HU": "HU",  # Hungary
    "IC": "IS",  # Iceland
    "ID": "ID",  # Indonesia
    "IM": "IM",  # Isle of Man
    "IN": "IN",  # India
    "IO": "IO",  # British Indian Ocean Territory
    "IR": "IR",  # Iran
    "IS": "IL",  # Israel
    "IT": "IT",  # Italy
    "IV": "CI",  # Côte d'Ivoire
    "IZ": "IQ",  # Iraq
    "JA": "JP",  # Japan
    "JE": "JE",  # Jersey
    "JM": "JM",  # Jamaica
    "JN": "SJ",  # Jan Mayen (Svalbard and Jan Mayen)
    "JO": "JO",  # Jordan
    "JQ": "UM",  # Johnston Atoll
    "KE": "KE",  # Kenya
    "KG": "KG",  # Kyrgyzstan
    "KN": "KP",  # Korea, North
    "KQ": "UM",  # Kingman Reef
    "KR": "KI",  # Kiribati
    "KS": "KR",  # Korea, South
    "KT": "CX",  # Christmas Island
    "KU": "KW",  # Kuwait
    "KV": "XK",  # Kosovo — user-assigned ISO code; see the note below
    "KZ": "KZ",  # Kazakhstan
    "LA": "LA",  # Laos
    "LE": "LB",  # Lebanon
    "LG": "LV",  # Latvia
    "LH": "LT",  # Lithuania
    "LI": "LR",  # Liberia
    "LO": "SK",  # Slovakia
    "LS": "LI",  # Liechtenstein
    "LT": "LS",  # Lesotho
    "LU": "LU",  # Luxembourg
    "LY": "LY",  # Libya
    "MA": "MG",  # Madagascar
    "MB": "MQ",  # Martinique
    "MC": "MO",  # Macao
    "MD": "MD",  # Moldova
    "MF": "YT",  # Mayotte
    "MG": "MN",  # Mongolia
    "MH": "MS",  # Montserrat
    "MI": "MW",  # Malawi
    "MJ": "ME",  # Montenegro
    "MK": "MK",  # North Macedonia
    "ML": "ML",  # Mali
    "MN": "MC",  # Monaco
    "MO": "MA",  # Morocco
    "MP": "MU",  # Mauritius
    "MQ": "UM",  # Midway Islands
    "MR": "MR",  # Mauritania
    "MT": "MT",  # Malta
    "MU": "OM",  # Oman
    "MV": "MV",  # Maldives
    "MX": "MX",  # Mexico
    "MY": "MY",  # Malaysia
    "MZ": "MZ",  # Mozambique
    "NC": "NC",  # New Caledonia
    "NE": "NU",  # Niue
    "NF": "NF",  # Norfolk Island
    "NG": "NE",  # Niger
    "NH": "VU",  # Vanuatu
    "NI": "NG",  # Nigeria
    "NL": "NL",  # Netherlands
    "NO": "NO",  # Norway
    "NP": "NP",  # Nepal
    "NR": "NR",  # Nauru
    "NS": "SR",  # Suriname
    "NU": "NI",  # Nicaragua
    "NZ": "NZ",  # New Zealand
    "OD": "SS",  # South Sudan
    "PA": "PY",  # Paraguay
    "PC": "PN",  # Pitcairn Islands
    "PE": "PE",  # Peru
    "PK": "PK",  # Pakistan
    "PL": "PL",  # Poland
    "PM": "PA",  # Panama
    "PO": "PT",  # Portugal
    "PP": "PG",  # Papua New Guinea
    "PS": "PW",  # Palau
    "PU": "GW",  # Guinea-Bissau
    "QA": "QA",  # Qatar
    "RE": "RE",  # Réunion
    "RI": "RS",  # Serbia
    "RM": "MH",  # Marshall Islands
    "RN": "MF",  # Saint Martin (French part)
    "RO": "RO",  # Romania
    "RP": "PH",  # Philippines
    "RQ": "PR",  # Puerto Rico
    "RS": "RU",  # Russia
    "RW": "RW",  # Rwanda
    "SA": "SA",  # Saudi Arabia
    "SB": "PM",  # Saint Pierre and Miquelon
    "SC": "KN",  # Saint Kitts and Nevis
    "SE": "SC",  # Seychelles
    "SF": "ZA",  # South Africa
    "SG": "SN",  # Senegal
    "SH": "SH",  # Saint Helena
    "SI": "SI",  # Slovenia
    "SL": "SL",  # Sierra Leone
    "SM": "SM",  # San Marino
    "SN": "SG",  # Singapore
    "SO": "SO",  # Somalia
    "SP": "ES",  # Spain
    "ST": "LC",  # Saint Lucia
    "SU": "SD",  # Sudan
    "SV": "SJ",  # Svalbard (Svalbard and Jan Mayen)
    "SW": "SE",  # Sweden
    "SX": "GS",  # South Georgia and the South Sandwich Islands
    "SY": "SY",  # Syria
    "SZ": "CH",  # Switzerland
    "TB": "BL",  # Saint Barthélemy
    "TD": "TT",  # Trinidad and Tobago
    "TH": "TH",  # Thailand
    "TI": "TJ",  # Tajikistan
    "TK": "TC",  # Turks and Caicos Islands
    "TL": "TK",  # Tokelau
    "TN": "TO",  # Tonga
    "TO": "TG",  # Togo
    "TP": "ST",  # Sao Tome and Principe
    "TS": "TN",  # Tunisia
    "TT": "TL",  # Timor-Leste
    "TU": "TR",  # Turkey
    "TV": "TV",  # Tuvalu
    "TW": "TW",  # Taiwan
    "TX": "TM",  # Turkmenistan
    "TZ": "TZ",  # Tanzania
    "UG": "UG",  # Uganda
    "UK": "GB",  # United Kingdom
    "UP": "UA",  # Ukraine
    "US": "US",  # United States
    "UV": "BF",  # Burkina Faso
    "UY": "UY",  # Uruguay
    "UZ": "UZ",  # Uzbekistan
    "VC": "VC",  # Saint Vincent and the Grenadines
    "VE": "VE",  # Venezuela
    "VI": "VG",  # Virgin Islands, British
    "VM": "VN",  # Viet Nam
    "VQ": "VI",  # Virgin Islands, U.S.
    "VT": "VA",  # Holy See (Vatican City)
    "WA": "NA",  # Namibia
    "WE": "PS",  # West Bank — ISO's State of Palestine
    "WF": "WF",  # Wallis and Futuna
    "WI": "EH",  # Western Sahara
    "WQ": "UM",  # Wake Island
    "WS": "WS",  # Samoa
    "WZ": "SZ",  # Eswatini
    "YM": "YE",  # Yemen
    "ZA": "ZM",  # Zambia
    "ZI": "ZW",  # Zimbabwe
    "GZ": "PS",  # Gaza Strip — ISO's State of Palestine
}

#: Codes that are BOTH a valid FIPS code and a valid ISO code, meaning DIFFERENT
#: countries. These are the silent misroutes — the pairs where treating a FIPS
#: code as ISO does not fail, it just delivers the story to the wrong desk.
#: Derived, not hand-listed, so it can never drift from the table above.
COLLIDING_CODES: frozenset[str] = frozenset(
    code for code, iso in FIPS_TO_ISO2.items()
    if code != iso and code in set(FIPS_TO_ISO2.values())
)


def fips_to_iso2(code: str | None) -> str | None:
    """Translate one FIPS 10-4 country code to ISO 3166-1 alpha-2.

    Returns ``None`` for anything this table cannot speak to — an empty value, a
    malformed code, a subdivision code, or one of the deliberately-absent
    entries (uninhabited or disputed territories with no ISO counterpart). The
    caller must treat ``None`` as "say nothing", never as "pass the input
    through": passing an untranslated FIPS code onward is the exact defect this
    module exists to stop.
    """
    if not code:
        return None
    return FIPS_TO_ISO2.get(code.strip().upper())
