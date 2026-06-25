#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed data for Legba — verified world knowledge as of April 2026.

Populates a fresh database with entity profiles, facts, situations,
watchlist items, and goals so the agent starts with a functional world model.

Idempotent — safe to re-run. Uses ON CONFLICT DO NOTHING everywhere.

Usage:
    # From inside the UI container
    docker compose -p legba exec ui python3 /app/scripts/seed_data.py

    # Or directly with path set up
    python3 scripts/seed_data.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg

DB_CONFIG = {
    "host": os.environ.get("POSTGRES_HOST", "localhost"),
    "port": int(os.environ.get("POSTGRES_PORT", "5432")),
    "user": os.environ.get("POSTGRES_USER", "legba"),
    "password": os.environ.get("POSTGRES_PASSWORD", "legba"),
    "database": os.environ.get("POSTGRES_DB", "legba"),
}

NOW = datetime.now(timezone.utc)


# ============================================================================
# ENTITY DATA
# ============================================================================

COUNTRIES = [
    # G7
    "United States", "United Kingdom", "France", "Germany", "Italy", "Canada", "Japan",
    # BRICS
    "Brazil", "Russia", "India", "China", "South Africa", "Egypt", "Ethiopia", "Iran",
    "Saudi Arabia", "United Arab Emirates", "Indonesia",
    # Europe
    "Spain", "Netherlands", "Sweden", "Norway", "Finland", "Poland", "Switzerland",
    "Austria", "Belgium", "Czech Republic", "Romania", "Hungary", "Greece", "Portugal",
    "Denmark", "Ireland", "Ukraine",
    # Asia-Pacific
    "South Korea", "North Korea", "Australia", "New Zealand", "Pakistan", "Bangladesh",
    "Philippines", "Thailand", "Vietnam", "Malaysia", "Singapore", "Taiwan", "Myanmar",
    "Afghanistan",
    # Middle East
    "Israel", "Turkey", "Iraq", "Syria", "Lebanon", "Jordan", "Qatar", "Bahrain",
    "Kuwait", "Oman", "Yemen",
    # Africa
    "Nigeria", "Kenya", "DR Congo", "Sudan", "Somalia", "Morocco", "Algeria",
    "Libya", "Tunisia", "Uganda",
    # Americas
    "Mexico", "Argentina", "Colombia", "Venezuela", "Cuba", "Chile", "Peru",
    # Other
    "Belarus", "Georgia", "Azerbaijan", "Armenia", "Mongolia", "Nepal", "Bhutan",
    "Laos", "Kazakhstan", "Kyrgyzstan", "Tajikistan", "Turkmenistan",
    "Estonia", "Latvia", "Lithuania", "Slovakia", "Slovenia", "Croatia",
    "Bulgaria", "Montenegro", "North Macedonia", "Albania", "Iceland",
    "Luxembourg", "Malta", "Cyprus",
]

# Capital city coordinates (lat, lon) for geo-tagging country entities
COUNTRY_GEO = {
    # G7
    "United States": (38.8951, -77.0364),          # Washington DC
    "United Kingdom": (51.5074, -0.1278),           # London
    "France": (48.8566, 2.3522),                    # Paris
    "Germany": (52.5200, 13.4050),                  # Berlin
    "Italy": (41.9028, 12.4964),                    # Rome
    "Canada": (45.4215, -75.6972),                  # Ottawa
    "Japan": (35.6762, 139.6503),                   # Tokyo
    # BRICS
    "Brazil": (-15.7975, -47.8919),                 # Brasilia
    "Russia": (55.7558, 37.6173),                   # Moscow
    "India": (28.6139, 77.2090),                    # New Delhi
    "China": (39.9042, 116.4074),                   # Beijing
    "South Africa": (-25.7479, 28.2293),            # Pretoria
    "Egypt": (30.0444, 31.2357),                    # Cairo
    "Ethiopia": (9.0250, 38.7469),                  # Addis Ababa
    "Iran": (35.6892, 51.3890),                     # Tehran
    "Saudi Arabia": (24.7136, 46.6753),             # Riyadh
    "United Arab Emirates": (24.4539, 54.3773),     # Abu Dhabi
    "Indonesia": (-6.2088, 106.8456),               # Jakarta
    # Europe
    "Spain": (40.4168, -3.7038),                    # Madrid
    "Netherlands": (52.3676, 4.9041),               # Amsterdam
    "Sweden": (59.3293, 18.0686),                   # Stockholm
    "Norway": (59.9139, 10.7522),                   # Oslo
    "Finland": (60.1699, 24.9384),                  # Helsinki
    "Poland": (52.2297, 21.0122),                   # Warsaw
    "Switzerland": (46.9480, 7.4474),               # Bern
    "Austria": (48.2082, 16.3738),                  # Vienna
    "Belgium": (50.8503, 4.3517),                   # Brussels
    "Czech Republic": (50.0755, 14.4378),           # Prague
    "Romania": (44.4268, 26.1025),                  # Bucharest
    "Hungary": (47.4979, 19.0402),                  # Budapest
    "Greece": (37.9838, 23.7275),                   # Athens
    "Portugal": (38.7223, -9.1393),                 # Lisbon
    "Denmark": (55.6761, 12.5683),                  # Copenhagen
    "Ireland": (53.3498, -6.2603),                  # Dublin
    "Ukraine": (50.4501, 30.5234),                  # Kyiv
    # Asia-Pacific
    "South Korea": (37.5665, 126.9780),             # Seoul
    "North Korea": (39.0392, 125.7625),             # Pyongyang
    "Australia": (-35.2809, 149.1300),              # Canberra
    "New Zealand": (-41.2865, 174.7762),            # Wellington
    "Pakistan": (33.6844, 73.0479),                 # Islamabad
    "Bangladesh": (23.8103, 90.4125),               # Dhaka
    "Philippines": (14.5995, 120.9842),             # Manila
    "Thailand": (13.7563, 100.5018),                # Bangkok
    "Vietnam": (21.0285, 105.8542),                 # Hanoi
    "Malaysia": (3.1390, 101.6869),                 # Kuala Lumpur
    "Singapore": (1.3521, 103.8198),                # Singapore
    "Taiwan": (25.0330, 121.5654),                  # Taipei
    "Myanmar": (19.7633, 96.0785),                  # Naypyidaw
    "Afghanistan": (34.5553, 69.2075),              # Kabul
    # Middle East
    "Israel": (31.7683, 35.2137),                   # Jerusalem
    "Turkey": (39.9334, 32.8597),                   # Ankara
    "Iraq": (33.3152, 44.3661),                     # Baghdad
    "Syria": (33.5138, 36.2765),                    # Damascus
    "Lebanon": (33.8938, 35.5018),                  # Beirut
    "Jordan": (31.9454, 35.9284),                   # Amman
    "Qatar": (25.2854, 51.5310),                    # Doha
    "Bahrain": (26.2285, 50.5860),                  # Manama
    "Kuwait": (29.3759, 47.9774),                   # Kuwait City
    "Oman": (23.5880, 58.3829),                     # Muscat
    "Yemen": (15.3694, 44.1910),                    # Sanaa
    # Africa
    "Nigeria": (9.0579, 7.4951),                    # Abuja
    "Kenya": (-1.2921, 36.8219),                    # Nairobi
    "DR Congo": (-4.4419, 15.2663),                 # Kinshasa
    "Sudan": (15.5007, 32.5599),                    # Khartoum
    "Somalia": (2.0469, 45.3182),                   # Mogadishu
    "Morocco": (33.9716, -6.8498),                  # Rabat
    "Algeria": (36.7538, 3.0588),                   # Algiers
    "Libya": (32.8872, 13.1913),                    # Tripoli
    "Tunisia": (36.8065, 10.1815),                  # Tunis
    "Uganda": (0.3476, 32.5825),                    # Kampala
    # Americas
    "Mexico": (19.4326, -99.1332),                  # Mexico City
    "Argentina": (-34.6037, -58.3816),              # Buenos Aires
    "Colombia": (4.7110, -74.0721),                 # Bogota
    "Venezuela": (10.4806, -66.9036),               # Caracas
    "Cuba": (23.1136, -82.3666),                    # Havana
    "Chile": (-33.4489, -70.6693),                  # Santiago
    "Peru": (-12.0464, -77.0428),                   # Lima
    # Other
    "Belarus": (53.9045, 27.5615),                  # Minsk
    "Georgia": (41.7151, 44.8271),                  # Tbilisi
    "Azerbaijan": (40.4093, 49.8671),               # Baku
    "Armenia": (40.1792, 44.4991),                  # Yerevan
    "Mongolia": (47.8864, 106.9057),                # Ulaanbaatar
    "Nepal": (27.7172, 85.3240),                    # Kathmandu
    "Bhutan": (27.4728, 89.6390),                   # Thimphu
    "Laos": (17.9757, 102.6331),                    # Vientiane
    "Kazakhstan": (51.1694, 71.4491),               # Astana
    "Kyrgyzstan": (42.8746, 74.5698),               # Bishkek
    "Tajikistan": (38.5598, 68.7740),               # Dushanbe
    "Turkmenistan": (37.9601, 58.3261),             # Ashgabat
    "Estonia": (59.4370, 24.7536),                  # Tallinn
    "Latvia": (56.9496, 24.1052),                   # Riga
    "Lithuania": (54.6872, 25.2797),                # Vilnius
    "Slovakia": (48.1486, 17.1077),                 # Bratislava
    "Slovenia": (46.0569, 14.5058),                 # Ljubljana
    "Croatia": (45.8150, 15.9819),                  # Zagreb
    "Bulgaria": (42.6977, 23.3219),                 # Sofia
    "Montenegro": (42.4304, 19.2594),               # Podgorica
    "North Macedonia": (41.9973, 21.4280),          # Skopje
    "Albania": (41.3275, 19.8187),                  # Tirana
    "Iceland": (64.1466, -21.9426),                 # Reykjavik
    "Luxembourg": (49.6117, 6.1300),                # Luxembourg City
    "Malta": (35.8989, 14.5146),                    # Valletta
    "Cyprus": (35.1856, 33.3823),                   # Nicosia
}

# (name, title, country)
LEADERS = [
    ("Donald Trump", "President", "United States"),
    ("Keir Starmer", "Prime Minister", "United Kingdom"),
    ("Emmanuel Macron", "President", "France"),
    ("Friedrich Merz", "Chancellor", "Germany"),
    ("Giorgia Meloni", "Prime Minister", "Italy"),
    ("Mark Carney", "Prime Minister", "Canada"),
    ("Shigeru Ishiba", "Prime Minister", "Japan"),
    ("Vladimir Putin", "President", "Russia"),
    ("Volodymyr Zelenskyy", "President", "Ukraine"),
    ("Xi Jinping", "President", "China"),
    ("Narendra Modi", "Prime Minister", "India"),
    ("Luiz Inacio Lula da Silva", "President", "Brazil"),
    ("Cyril Ramaphosa", "President", "South Africa"),
    ("Abdel Fattah el-Sisi", "President", "Egypt"),
    ("Mojtaba Khamenei", "Supreme Leader", "Iran"),
    ("Masoud Pezeshkian", "President", "Iran"),
    ("Benjamin Netanyahu", "Prime Minister", "Israel"),
    ("Mohammed bin Salman", "Crown Prince and Prime Minister", "Saudi Arabia"),
    ("Sheikh Mohamed bin Zayed", "President", "United Arab Emirates"),
    ("Prabowo Subianto", "President", "Indonesia"),
    ("Lee Jae-myung", "President", "South Korea"),
    ("Kim Jong-un", "Supreme Leader", "North Korea"),
    ("Recep Tayyip Erdogan", "President", "Turkey"),
    ("Anthony Albanese", "Prime Minister", "Australia"),
    ("Yoweri Museveni", "President", "Uganda"),
    ("Joseph Aoun", "President", "Lebanon"),
    ("Nawaf Salam", "Prime Minister", "Lebanon"),
    ("Ahmed al-Sharaa", "Transitional Leader", "Syria"),
    ("Abdel Fattah al-Burhan", "Head of Sovereignty Council", "Sudan"),
    ("Pope Leo XIV", "Pope", "Vatican"),
    ("Shahbaz Sharif", "Prime Minister", "Pakistan"),
    ("Hibatullah Akhundzada", "Supreme Leader", "Afghanistan"),
]

ORGANIZATIONS = [
    "NATO", "European Union", "United Nations", "BRICS", "African Union", "ASEAN",
    "Arab League", "G7", "G20", "OPEC", "WHO", "IAEA",
    "International Criminal Court", "World Bank", "IMF", "WTO",
    "Shanghai Cooperation Organisation",
]

ARMED_GROUPS = [
    "Hamas", "Hezbollah", "Houthis", "Islamic State",
    "RSF", "Wagner Group", "PKK", "Taliban",
    "al-Qaeda", "Boko Haram", "al-Shabaab", "M23",
    "Balochistan Liberation Army",
]


# ============================================================================
# FACT DATA
# ============================================================================

# (subject, predicate, value, confidence)
LEADER_FACTS = [
    (name, "LeaderOf", country, 0.95)
    for name, _title, country in LEADERS
    if country != "Vatican"  # Pope is special
]
# Pope fact
LEADER_FACTS.append(("Pope Leo XIV", "LeaderOf", "Vatican", 0.95))

# Leader title facts
LEADER_TITLE_FACTS = [
    (name, "HasTitle", title, 0.95)
    for name, title, _country in LEADERS
]

# country, capital
CAPITALS = [
    ("United States", "Washington D.C."),
    ("United Kingdom", "London"),
    ("France", "Paris"),
    ("Germany", "Berlin"),
    ("Italy", "Rome"),
    ("Canada", "Ottawa"),
    ("Japan", "Tokyo"),
    ("Brazil", "Brasilia"),
    ("Russia", "Moscow"),
    ("India", "New Delhi"),
    ("China", "Beijing"),
    ("South Africa", "Pretoria"),
    ("Egypt", "Cairo"),
    ("Ethiopia", "Addis Ababa"),
    ("Iran", "Tehran"),
    ("Saudi Arabia", "Riyadh"),
    ("United Arab Emirates", "Abu Dhabi"),
    ("Indonesia", "Jakarta"),
    ("Spain", "Madrid"),
    ("Netherlands", "Amsterdam"),
    ("Sweden", "Stockholm"),
    ("Norway", "Oslo"),
    ("Finland", "Helsinki"),
    ("Poland", "Warsaw"),
    ("Switzerland", "Bern"),
    ("Austria", "Vienna"),
    ("Belgium", "Brussels"),
    ("Czech Republic", "Prague"),
    ("Romania", "Bucharest"),
    ("Hungary", "Budapest"),
    ("Greece", "Athens"),
    ("Portugal", "Lisbon"),
    ("Denmark", "Copenhagen"),
    ("Ireland", "Dublin"),
    ("Ukraine", "Kyiv"),
    ("South Korea", "Seoul"),
    ("North Korea", "Pyongyang"),
    ("Australia", "Canberra"),
    ("New Zealand", "Wellington"),
    ("Pakistan", "Islamabad"),
    ("Bangladesh", "Dhaka"),
    ("Philippines", "Manila"),
    ("Thailand", "Bangkok"),
    ("Vietnam", "Hanoi"),
    ("Malaysia", "Kuala Lumpur"),
    ("Singapore", "Singapore"),
    ("Taiwan", "Taipei"),
    ("Myanmar", "Naypyidaw"),
    ("Afghanistan", "Kabul"),
    ("Israel", "Jerusalem"),
    ("Turkey", "Ankara"),
    ("Iraq", "Baghdad"),
    ("Syria", "Damascus"),
    ("Lebanon", "Beirut"),
    ("Jordan", "Amman"),
    ("Qatar", "Doha"),
    ("Bahrain", "Manama"),
    ("Kuwait", "Kuwait City"),
    ("Oman", "Muscat"),
    ("Yemen", "Sanaa"),
    ("Nigeria", "Abuja"),
    ("Kenya", "Nairobi"),
    ("DR Congo", "Kinshasa"),
    ("Sudan", "Khartoum"),
    ("Somalia", "Mogadishu"),
    ("Morocco", "Rabat"),
    ("Algeria", "Algiers"),
    ("Libya", "Tripoli"),
    ("Tunisia", "Tunis"),
    ("Uganda", "Kampala"),
    ("Mexico", "Mexico City"),
    ("Argentina", "Buenos Aires"),
    ("Colombia", "Bogota"),
    ("Venezuela", "Caracas"),
    ("Cuba", "Havana"),
    ("Chile", "Santiago"),
    ("Peru", "Lima"),
    ("Belarus", "Minsk"),
    ("Georgia", "Tbilisi"),
    ("Azerbaijan", "Baku"),
    ("Armenia", "Yerevan"),
    ("Mongolia", "Ulaanbaatar"),
    ("Nepal", "Kathmandu"),
    ("Kazakhstan", "Astana"),
    ("Kyrgyzstan", "Bishkek"),
    ("Tajikistan", "Dushanbe"),
    ("Turkmenistan", "Ashgabat"),
    ("Estonia", "Tallinn"),
    ("Latvia", "Riga"),
    ("Lithuania", "Vilnius"),
    ("Slovakia", "Bratislava"),
    ("Slovenia", "Ljubljana"),
    ("Croatia", "Zagreb"),
    ("Bulgaria", "Sofia"),
    ("Montenegro", "Podgorica"),
    ("North Macedonia", "Skopje"),
    ("Albania", "Tirana"),
    ("Iceland", "Reykjavik"),
    ("Luxembourg", "Luxembourg City"),
    ("Malta", "Valletta"),
    ("Cyprus", "Nicosia"),
]

CAPITAL_FACTS = [
    (capital, "CapitalOf", country, 0.99)
    for country, capital in CAPITALS
]

# (country_a, country_b) — borders are bidirectional, we store both directions
_BORDER_PAIRS = [
    # US
    ("United States", "Canada"),
    ("United States", "Mexico"),
    # Russia
    ("Russia", "Ukraine"),
    ("Russia", "China"),
    ("Russia", "Finland"),
    ("Russia", "Norway"),
    ("Russia", "Estonia"),
    ("Russia", "Latvia"),
    ("Russia", "Belarus"),
    ("Russia", "Georgia"),
    ("Russia", "Mongolia"),
    ("Russia", "Kazakhstan"),
    ("Russia", "North Korea"),
    ("Russia", "Azerbaijan"),
    ("Russia", "Lithuania"),  # Kaliningrad
    ("Russia", "Poland"),     # Kaliningrad
    # China
    ("China", "India"),
    ("China", "Pakistan"),
    ("China", "Afghanistan"),
    ("China", "Mongolia"),
    ("China", "North Korea"),
    ("China", "Vietnam"),
    ("China", "Laos"),
    ("China", "Myanmar"),
    ("China", "Nepal"),
    ("China", "Bhutan"),
    ("China", "Kazakhstan"),
    ("China", "Kyrgyzstan"),
    ("China", "Tajikistan"),
    # India
    ("India", "Pakistan"),
    ("India", "Nepal"),
    ("India", "Bhutan"),
    ("India", "Bangladesh"),
    ("India", "Myanmar"),
    # Israel
    ("Israel", "Lebanon"),
    ("Israel", "Syria"),
    ("Israel", "Jordan"),
    ("Israel", "Egypt"),
    # Iran
    ("Iran", "Iraq"),
    ("Iran", "Turkey"),
    ("Iran", "Afghanistan"),
    ("Iran", "Pakistan"),
    ("Iran", "Turkmenistan"),
    ("Iran", "Azerbaijan"),
    ("Iran", "Armenia"),
    # Turkey
    ("Turkey", "Greece"),
    ("Turkey", "Bulgaria"),
    ("Turkey", "Syria"),
    ("Turkey", "Iraq"),
    ("Turkey", "Georgia"),
    ("Turkey", "Armenia"),
    # Germany
    ("Germany", "France"),
    ("Germany", "Netherlands"),
    ("Germany", "Belgium"),
    ("Germany", "Luxembourg"),
    ("Germany", "Austria"),
    ("Germany", "Switzerland"),
    ("Germany", "Czech Republic"),
    ("Germany", "Poland"),
    ("Germany", "Denmark"),
    # France
    ("France", "Belgium"),
    ("France", "Luxembourg"),
    ("France", "Switzerland"),
    ("France", "Italy"),
    ("France", "Spain"),
    # Other Europe
    ("Spain", "Portugal"),
    ("Italy", "Switzerland"),
    ("Italy", "Austria"),
    ("Italy", "Slovenia"),
    ("Austria", "Hungary"),
    ("Austria", "Czech Republic"),
    ("Austria", "Slovakia"),
    ("Austria", "Slovenia"),
    ("Austria", "Switzerland"),
    ("Poland", "Czech Republic"),
    ("Poland", "Slovakia"),
    ("Poland", "Ukraine"),
    ("Poland", "Belarus"),
    ("Poland", "Lithuania"),
    ("Hungary", "Romania"),
    ("Hungary", "Slovakia"),
    ("Hungary", "Croatia"),
    ("Hungary", "Slovenia"),
    ("Hungary", "Ukraine"),
    ("Romania", "Ukraine"),
    ("Romania", "Moldova"),
    ("Romania", "Bulgaria"),
    ("Romania", "Hungary"),
    ("Greece", "Albania"),
    ("Greece", "North Macedonia"),
    ("Greece", "Bulgaria"),
    ("Croatia", "Slovenia"),
    ("Croatia", "Bosnia and Herzegovina"),
    ("Norway", "Sweden"),
    ("Norway", "Finland"),
    ("Sweden", "Finland"),
    # Middle East / Central Asia
    ("Iraq", "Syria"),
    ("Iraq", "Jordan"),
    ("Iraq", "Kuwait"),
    ("Iraq", "Saudi Arabia"),
    ("Syria", "Lebanon"),
    ("Syria", "Jordan"),
    ("Jordan", "Saudi Arabia"),
    ("Saudi Arabia", "Yemen"),
    ("Saudi Arabia", "Oman"),
    ("Saudi Arabia", "United Arab Emirates"),
    ("Saudi Arabia", "Qatar"),
    ("Yemen", "Oman"),
    ("Afghanistan", "Pakistan"),
    ("Afghanistan", "Turkmenistan"),
    ("Afghanistan", "Tajikistan"),
    ("Kazakhstan", "Kyrgyzstan"),
    ("Kazakhstan", "Turkmenistan"),
    ("Kazakhstan", "Uzbekistan"),
    # Asia
    ("Thailand", "Myanmar"),
    ("Thailand", "Laos"),
    ("Thailand", "Malaysia"),
    ("Vietnam", "Laos"),
    ("Vietnam", "Cambodia"),
    ("South Korea", "North Korea"),
    # Africa
    ("Sudan", "Egypt"),
    ("Sudan", "Libya"),
    ("Sudan", "Chad"),
    ("Sudan", "Ethiopia"),
    ("Sudan", "South Sudan"),
    ("Ethiopia", "Somalia"),
    ("Ethiopia", "Kenya"),
    ("DR Congo", "Uganda"),
    ("DR Congo", "Rwanda"),
    ("Kenya", "Somalia"),
    ("Kenya", "Uganda"),
    ("Kenya", "Tanzania"),
    ("Nigeria", "Niger"),
    ("Nigeria", "Chad"),
    ("Nigeria", "Cameroon"),
    ("Algeria", "Morocco"),
    ("Algeria", "Tunisia"),
    ("Algeria", "Libya"),
    ("Libya", "Tunisia"),
    ("Libya", "Egypt"),
    # Americas
    ("Colombia", "Venezuela"),
    ("Colombia", "Peru"),
    ("Colombia", "Brazil"),
    ("Argentina", "Chile"),
    ("Argentina", "Brazil"),
    ("Argentina", "Uruguay"),
    ("Mexico", "Guatemala"),
    ("Mexico", "Belize"),
    # Ukraine
    ("Ukraine", "Belarus"),
    ("Ukraine", "Moldova"),
    ("Ukraine", "Slovakia"),
]

BORDER_FACTS = []
_seen_borders = set()
for a, b in _BORDER_PAIRS:
    key_ab = (a.lower(), b.lower())
    key_ba = (b.lower(), a.lower())
    if key_ab not in _seen_borders:
        BORDER_FACTS.append((a, "BordersWith", b, 0.99))
        _seen_borders.add(key_ab)
    if key_ba not in _seen_borders:
        BORDER_FACTS.append((b, "BordersWith", a, 0.99))
        _seen_borders.add(key_ba)


# NATO members (32)
NATO_MEMBERS = [
    "Albania", "Belgium", "Bulgaria", "Canada", "Croatia", "Czech Republic",
    "Denmark", "Estonia", "Finland", "France", "Germany", "Greece", "Hungary",
    "Iceland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Montenegro",
    "Netherlands", "North Macedonia", "Norway", "Poland", "Portugal", "Romania",
    "Slovakia", "Slovenia", "Spain", "Sweden", "Turkey", "United Kingdom",
    "United States",
]

# EU members (27)
EU_MEMBERS = [
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czech Republic",
    "Denmark", "Estonia", "Finland", "France", "Germany", "Greece", "Hungary",
    "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta",
    "Netherlands", "Poland", "Portugal", "Romania", "Slovakia", "Slovenia",
    "Spain", "Sweden",
]

# BRICS (11 as of 2024 expansion)
BRICS_MEMBERS = [
    "Brazil", "Russia", "India", "China", "South Africa",
    "Egypt", "Ethiopia", "Iran", "Saudi Arabia", "United Arab Emirates", "Indonesia",
]

# ASEAN (10)
ASEAN_MEMBERS = [
    "Indonesia", "Malaysia", "Philippines", "Singapore", "Thailand",
    "Vietnam", "Myanmar", "Laos", "Cambodia", "Brunei",
]

# G7
G7_MEMBERS = [
    "United States", "United Kingdom", "France", "Germany", "Italy", "Canada", "Japan",
]

# SCO
SCO_MEMBERS = [
    "China", "Russia", "India", "Pakistan", "Kazakhstan", "Kyrgyzstan",
    "Tajikistan", "Uzbekistan", "Iran", "Belarus",
]

MEMBERSHIP_FACTS = []
for member in NATO_MEMBERS:
    MEMBERSHIP_FACTS.append((member, "MemberOf", "NATO", 0.99))
for member in EU_MEMBERS:
    MEMBERSHIP_FACTS.append((member, "MemberOf", "European Union", 0.99))
for member in BRICS_MEMBERS:
    MEMBERSHIP_FACTS.append((member, "MemberOf", "BRICS", 0.99))
for member in ASEAN_MEMBERS:
    MEMBERSHIP_FACTS.append((member, "MemberOf", "ASEAN", 0.99))
for member in G7_MEMBERS:
    MEMBERSHIP_FACTS.append((member, "MemberOf", "G7", 0.99))
for member in SCO_MEMBERS:
    MEMBERSHIP_FACTS.append((member, "MemberOf", "Shanghai Cooperation Organisation", 0.99))

# Hostility / Alliance / Geopolitical relationships
GEOPOLITICAL_FACTS = [
    # Hostilities
    ("Israel", "HostileTo", "Iran", 0.9),
    ("Iran", "HostileTo", "Israel", 0.9),
    ("Israel", "HostileTo", "Hamas", 0.95),
    ("Hamas", "HostileTo", "Israel", 0.95),
    ("Israel", "HostileTo", "Hezbollah", 0.9),
    ("Hezbollah", "HostileTo", "Israel", 0.9),
    ("Israel", "HostileTo", "Houthis", 0.85),
    ("Houthis", "HostileTo", "Israel", 0.9),
    ("Russia", "HostileTo", "Ukraine", 0.95),
    ("Ukraine", "HostileTo", "Russia", 0.95),
    ("North Korea", "HostileTo", "South Korea", 0.9),
    ("South Korea", "HostileTo", "North Korea", 0.85),
    ("North Korea", "HostileTo", "United States", 0.85),
    ("Iran", "HostileTo", "United States", 0.95),
    ("United States", "HostileTo", "Iran", 0.95),
    ("Turkey", "HostileTo", "PKK", 0.95),
    ("PKK", "HostileTo", "Turkey", 0.95),
    ("RSF", "HostileTo", "Sudan", 0.9),  # RSF vs SAF
    ("Iran", "HostileTo", "Saudi Arabia", 0.7),
    ("Iran", "HostileTo", "United Arab Emirates", 0.7),
    ("Iran", "HostileTo", "Bahrain", 0.7),
    ("Iran", "HostileTo", "Kuwait", 0.65),
    ("Israel", "ConductsMilitaryOperationsIn", "Lebanon", 0.85),

    # Alliances
    ("United States", "AlliedWith", "United Kingdom", 0.9),
    ("United States", "AlliedWith", "Israel", 0.9),
    ("United States", "AlliedWith", "Japan", 0.9),
    ("United States", "AlliedWith", "South Korea", 0.9),
    ("United States", "AlliedWith", "Australia", 0.9),
    ("United States", "AlliedWith", "Canada", 0.9),
    ("United Kingdom", "AlliedWith", "United States", 0.9),
    ("Russia", "AlliedWith", "China", 0.8),
    ("Russia", "AlliedWith", "Iran", 0.8),
    ("Russia", "AlliedWith", "North Korea", 0.8),
    ("China", "AlliedWith", "Russia", 0.8),
    ("Iran", "AlliedWith", "Russia", 0.8),
    ("Iran", "SponsorOf", "Hamas", 0.85),
    ("Iran", "SponsorOf", "Hezbollah", 0.9),
    ("Iran", "SponsorOf", "Houthis", 0.85),
    ("Saudi Arabia", "AlliedWith", "United Arab Emirates", 0.85),
    ("Saudi Arabia", "AlliedWith", "United States", 0.75),
    ("Japan", "AlliedWith", "United States", 0.9),
    ("Australia", "AlliedWith", "United States", 0.9),

    # Territorial disputes
    ("China", "DisputesWith", "Taiwan", 0.9),
    ("China", "DisputesWith", "Philippines", 0.85),
    ("China", "DisputesWith", "Vietnam", 0.8),
    ("China", "DisputesWith", "Japan", 0.75),
    ("China", "DisputesWith", "India", 0.8),
    ("India", "DisputesWith", "Pakistan", 0.85),
    ("Pakistan", "DisputesWith", "India", 0.85),

    # Sanctions
    ("United States", "Sanctions", "Russia", 0.95),
    ("European Union", "Sanctions", "Russia", 0.95),
    ("United States", "Sanctions", "Iran", 0.95),
    ("United States", "Sanctions", "North Korea", 0.99),
    ("United States", "Sanctions", "Syria", 0.9),
    ("European Union", "Sanctions", "Iran", 0.9),
    ("European Union", "Sanctions", "North Korea", 0.99),
]

# Armed group operations
ARMED_GROUP_FACTS = [
    ("Hamas", "OperatesIn", "Gaza", 0.95),
    ("Hamas", "OperatesIn", "Palestine", 0.9),
    ("Hezbollah", "OperatesIn", "Lebanon", 0.95),
    ("Hezbollah", "OperatesIn", "Syria", 0.7),
    ("Houthis", "OperatesIn", "Yemen", 0.95),
    ("RSF", "OperatesIn", "Sudan", 0.95),
    ("Taliban", "OperatesIn", "Afghanistan", 0.95),
    ("Taliban", "GovernsIn", "Afghanistan", 0.9),
    ("Wagner Group", "OperatesIn", "Russia", 0.8),
    ("Wagner Group", "OperatesIn", "Syria", 0.7),
    ("Wagner Group", "OperatesIn", "Libya", 0.7),
    ("Wagner Group", "OperatesIn", "Mali", 0.75),
    ("Wagner Group", "OperatesIn", "Central African Republic", 0.75),
    ("Islamic State", "OperatesIn", "Iraq", 0.8),
    ("Islamic State", "OperatesIn", "Syria", 0.8),
    ("Islamic State", "OperatesIn", "Sahel", 0.7),
    ("al-Shabaab", "OperatesIn", "Somalia", 0.9),
    ("al-Shabaab", "OperatesIn", "Kenya", 0.7),
    ("Boko Haram", "OperatesIn", "Nigeria", 0.9),
    ("Boko Haram", "OperatesIn", "Niger", 0.7),
    ("Boko Haram", "OperatesIn", "Chad", 0.65),
    ("M23", "OperatesIn", "DR Congo", 0.9),
    ("PKK", "OperatesIn", "Turkey", 0.85),
    ("PKK", "OperatesIn", "Iraq", 0.75),
    ("PKK", "OperatesIn", "Syria", 0.7),
    ("Balochistan Liberation Army", "OperatesIn", "Pakistan", 0.9),
    ("al-Qaeda", "OperatesIn", "Yemen", 0.75),
    ("al-Qaeda", "OperatesIn", "Sahel", 0.7),
    ("al-Qaeda", "OperatesIn", "Afghanistan", 0.6),
]

# Continent/region facts for countries
REGION_FACTS = [
    ("United States", "LocatedIn", "North America", 0.99),
    ("Canada", "LocatedIn", "North America", 0.99),
    ("Mexico", "LocatedIn", "North America", 0.99),
    ("Brazil", "LocatedIn", "South America", 0.99),
    ("Argentina", "LocatedIn", "South America", 0.99),
    ("Colombia", "LocatedIn", "South America", 0.99),
    ("Venezuela", "LocatedIn", "South America", 0.99),
    ("Chile", "LocatedIn", "South America", 0.99),
    ("Peru", "LocatedIn", "South America", 0.99),
    ("Cuba", "LocatedIn", "Caribbean", 0.99),
    ("United Kingdom", "LocatedIn", "Europe", 0.99),
    ("France", "LocatedIn", "Europe", 0.99),
    ("Germany", "LocatedIn", "Europe", 0.99),
    ("Italy", "LocatedIn", "Europe", 0.99),
    ("Spain", "LocatedIn", "Europe", 0.99),
    ("Netherlands", "LocatedIn", "Europe", 0.99),
    ("Belgium", "LocatedIn", "Europe", 0.99),
    ("Sweden", "LocatedIn", "Europe", 0.99),
    ("Norway", "LocatedIn", "Europe", 0.99),
    ("Finland", "LocatedIn", "Europe", 0.99),
    ("Denmark", "LocatedIn", "Europe", 0.99),
    ("Poland", "LocatedIn", "Europe", 0.99),
    ("Ukraine", "LocatedIn", "Europe", 0.99),
    ("Romania", "LocatedIn", "Europe", 0.99),
    ("Hungary", "LocatedIn", "Europe", 0.99),
    ("Czech Republic", "LocatedIn", "Europe", 0.99),
    ("Austria", "LocatedIn", "Europe", 0.99),
    ("Switzerland", "LocatedIn", "Europe", 0.99),
    ("Greece", "LocatedIn", "Europe", 0.99),
    ("Portugal", "LocatedIn", "Europe", 0.99),
    ("Ireland", "LocatedIn", "Europe", 0.99),
    ("Russia", "LocatedIn", "Eurasia", 0.99),
    ("Turkey", "LocatedIn", "Eurasia", 0.99),
    ("China", "LocatedIn", "East Asia", 0.99),
    ("Japan", "LocatedIn", "East Asia", 0.99),
    ("South Korea", "LocatedIn", "East Asia", 0.99),
    ("North Korea", "LocatedIn", "East Asia", 0.99),
    ("Taiwan", "LocatedIn", "East Asia", 0.99),
    ("Mongolia", "LocatedIn", "East Asia", 0.99),
    ("India", "LocatedIn", "South Asia", 0.99),
    ("Pakistan", "LocatedIn", "South Asia", 0.99),
    ("Bangladesh", "LocatedIn", "South Asia", 0.99),
    ("Nepal", "LocatedIn", "South Asia", 0.99),
    ("Afghanistan", "LocatedIn", "South Asia", 0.99),
    ("Indonesia", "LocatedIn", "Southeast Asia", 0.99),
    ("Philippines", "LocatedIn", "Southeast Asia", 0.99),
    ("Vietnam", "LocatedIn", "Southeast Asia", 0.99),
    ("Thailand", "LocatedIn", "Southeast Asia", 0.99),
    ("Myanmar", "LocatedIn", "Southeast Asia", 0.99),
    ("Malaysia", "LocatedIn", "Southeast Asia", 0.99),
    ("Singapore", "LocatedIn", "Southeast Asia", 0.99),
    ("Iran", "LocatedIn", "Middle East", 0.99),
    ("Iraq", "LocatedIn", "Middle East", 0.99),
    ("Saudi Arabia", "LocatedIn", "Middle East", 0.99),
    ("United Arab Emirates", "LocatedIn", "Middle East", 0.99),
    ("Israel", "LocatedIn", "Middle East", 0.99),
    ("Syria", "LocatedIn", "Middle East", 0.99),
    ("Lebanon", "LocatedIn", "Middle East", 0.99),
    ("Jordan", "LocatedIn", "Middle East", 0.99),
    ("Yemen", "LocatedIn", "Middle East", 0.99),
    ("Qatar", "LocatedIn", "Middle East", 0.99),
    ("Kuwait", "LocatedIn", "Middle East", 0.99),
    ("Bahrain", "LocatedIn", "Middle East", 0.99),
    ("Oman", "LocatedIn", "Middle East", 0.99),
    ("Egypt", "LocatedIn", "North Africa", 0.99),
    ("Libya", "LocatedIn", "North Africa", 0.99),
    ("Tunisia", "LocatedIn", "North Africa", 0.99),
    ("Algeria", "LocatedIn", "North Africa", 0.99),
    ("Morocco", "LocatedIn", "North Africa", 0.99),
    ("Nigeria", "LocatedIn", "West Africa", 0.99),
    ("Kenya", "LocatedIn", "East Africa", 0.99),
    ("Ethiopia", "LocatedIn", "East Africa", 0.99),
    ("Somalia", "LocatedIn", "East Africa", 0.99),
    ("Uganda", "LocatedIn", "East Africa", 0.99),
    ("Sudan", "LocatedIn", "East Africa", 0.99),
    ("DR Congo", "LocatedIn", "Central Africa", 0.99),
    ("South Africa", "LocatedIn", "Southern Africa", 0.99),
    ("Australia", "LocatedIn", "Oceania", 0.99),
    ("New Zealand", "LocatedIn", "Oceania", 0.99),
    ("Kazakhstan", "LocatedIn", "Central Asia", 0.99),
    ("Kyrgyzstan", "LocatedIn", "Central Asia", 0.99),
    ("Tajikistan", "LocatedIn", "Central Asia", 0.99),
    ("Turkmenistan", "LocatedIn", "Central Asia", 0.99),
    ("Georgia", "LocatedIn", "Caucasus", 0.99),
    ("Armenia", "LocatedIn", "Caucasus", 0.99),
    ("Azerbaijan", "LocatedIn", "Caucasus", 0.99),
]

# Nuclear weapon states
NUCLEAR_FACTS = [
    ("United States", "PossessesNuclearWeapons", "true", 0.99),
    ("Russia", "PossessesNuclearWeapons", "true", 0.99),
    ("China", "PossessesNuclearWeapons", "true", 0.99),
    ("United Kingdom", "PossessesNuclearWeapons", "true", 0.99),
    ("France", "PossessesNuclearWeapons", "true", 0.99),
    ("India", "PossessesNuclearWeapons", "true", 0.99),
    ("Pakistan", "PossessesNuclearWeapons", "true", 0.99),
    ("Israel", "PossessesNuclearWeapons", "true", 0.85),
    ("North Korea", "PossessesNuclearWeapons", "true", 0.95),
]

# UN Security Council permanent members
UNSC_FACTS = [
    ("United States", "PermanentMemberOf", "UN Security Council", 0.99),
    ("Russia", "PermanentMemberOf", "UN Security Council", 0.99),
    ("China", "PermanentMemberOf", "UN Security Council", 0.99),
    ("United Kingdom", "PermanentMemberOf", "UN Security Council", 0.99),
    ("France", "PermanentMemberOf", "UN Security Council", 0.99),
]

# Key strategic waterways/chokepoints
WATERWAY_FACTS = [
    ("Strait of Hormuz", "ConnectsWaterways", "Persian Gulf to Gulf of Oman", 0.99),
    ("Strait of Hormuz", "StrategicControlledBy", "Iran", 0.85),
    ("Suez Canal", "ConnectsWaterways", "Mediterranean Sea to Red Sea", 0.99),
    ("Suez Canal", "ControlledBy", "Egypt", 0.99),
    ("Bab el-Mandeb", "ConnectsWaterways", "Red Sea to Gulf of Aden", 0.99),
    ("Taiwan Strait", "SeparatesEntities", "China and Taiwan", 0.99),
    ("Strait of Malacca", "ConnectsWaterways", "Indian Ocean to Pacific Ocean", 0.99),
]

# Key ongoing conflicts / status (as of April 2026)
CONFLICT_STATUS_FACTS = [
    ("Russia", "AtWarWith", "Ukraine", 0.95),
    ("Israel", "ConductsMilitaryOperationsIn", "Gaza", 0.95),
    ("Sudan", "InCivilWar", "true", 0.95),
    ("Myanmar", "InCivilWar", "true", 0.9),
    ("DR Congo", "InConflict", "true", 0.85),
    ("United States", "AtWarWith", "Iran", 0.9),
    ("Israel", "AtWarWith", "Iran", 0.9),
    ("Pakistan", "AtWarWith", "Afghanistan", 0.85),
    ("Strait of Hormuz", "StatusIs", "disrupted", 0.9),
    ("Venezuela", "RegimeChange", "true", 0.9),
    ("Israel", "ConductsMilitaryOperationsIn", "Lebanon", 0.85),
    ("Iran", "InternalCrisis", "true", 0.9),
    ("Haiti", "InCrisis", "true", 0.85),
]

# Population magnitude (order of magnitude, useful for context)
POPULATION_FACTS = [
    ("China", "PopulationApprox", "1.4 billion", 0.9),
    ("India", "PopulationApprox", "1.4 billion", 0.9),
    ("United States", "PopulationApprox", "340 million", 0.9),
    ("Indonesia", "PopulationApprox", "280 million", 0.9),
    ("Pakistan", "PopulationApprox", "240 million", 0.9),
    ("Brazil", "PopulationApprox", "215 million", 0.9),
    ("Nigeria", "PopulationApprox", "230 million", 0.9),
    ("Bangladesh", "PopulationApprox", "170 million", 0.9),
    ("Russia", "PopulationApprox", "145 million", 0.9),
    ("Japan", "PopulationApprox", "124 million", 0.9),
    ("Ethiopia", "PopulationApprox", "126 million", 0.9),
    ("Egypt", "PopulationApprox", "110 million", 0.9),
    ("DR Congo", "PopulationApprox", "105 million", 0.9),
    ("Germany", "PopulationApprox", "84 million", 0.9),
    ("Turkey", "PopulationApprox", "85 million", 0.9),
    ("Iran", "PopulationApprox", "88 million", 0.9),
    ("United Kingdom", "PopulationApprox", "68 million", 0.9),
    ("France", "PopulationApprox", "68 million", 0.9),
    ("Italy", "PopulationApprox", "59 million", 0.9),
    ("South Korea", "PopulationApprox", "52 million", 0.9),
    ("South Africa", "PopulationApprox", "62 million", 0.9),
    ("Ukraine", "PopulationApprox", "37 million", 0.85),
    ("Saudi Arabia", "PopulationApprox", "36 million", 0.9),
    ("Australia", "PopulationApprox", "26 million", 0.9),
    ("Israel", "PopulationApprox", "9.8 million", 0.9),
    ("North Korea", "PopulationApprox", "26 million", 0.85),
]


# ============================================================================
# SITUATION DATA
# ============================================================================

SITUATIONS = [
    {
        "name": "Ukraine-Russia War",
        "description": "Full-scale Russian invasion of Ukraine ongoing since February 2022. Involves territorial occupation in eastern and southern Ukraine, large-scale conventional warfare, nuclear threats, and extensive international sanctions against Russia. NATO countries providing military aid to Ukraine.",
        "status": "active",
        "category": "conflict",
        "key_entities": ["Russia", "Ukraine", "Vladimir Putin", "Volodymyr Zelenskyy", "NATO", "United States"],
        "regions": ["Eastern Europe", "Ukraine", "Russia"],
        "tags": ["war", "invasion", "sanctions", "nuclear-risk", "nato"],
        "intensity_score": 0.9,
    },
    {
        "name": "Iran-Israel-US War",
        "description": "Full-scale war involving Iran, Israel, and the United States. Evolved from proxy warfare to direct US military involvement following the assassination of Supreme Leader Khamenei in Operation Epic Fury (Feb 28, 2026). Sustained US-Israeli strikes have hit 24 of 31 Iranian provinces, destroying 82,000+ civilian structures. Iran retaliated with strikes on Gulf states (Saudi Arabia, UAE, Kuwait) and attacks on 27 US bases. Strait of Hormuz disrupted with selective transit policy. Oil at $126/barrel peak. US casualties: 13 KIA, 230+ wounded. Kata'ib Hezbollah offered conditional ceasefire. Congress seeking exit plan.",
        "status": "active",
        "category": "conflict",
        "key_entities": ["Iran", "Israel", "Hamas", "Hezbollah", "Houthis", "United States", "Benjamin Netanyahu", "Mojtaba Khamenei"],
        "regions": ["Middle East", "Iran", "Israel", "Lebanon", "Gaza", "Yemen", "Persian Gulf"],
        "tags": ["proxy-war", "nuclear", "strikes", "regional-escalation", "hormuz", "oil-crisis"],
        "intensity_score": 0.95,
    },
    {
        "name": "Sudan Civil War",
        "description": "Armed conflict between the Sudanese Armed Forces (SAF) under Abdel Fattah al-Burhan and the Rapid Support Forces (RSF) since April 2023. Massive displacement, famine conditions, and widespread atrocities across Darfur and Khartoum.",
        "status": "active",
        "category": "conflict",
        "key_entities": ["Sudan", "RSF", "Abdel Fattah al-Burhan"],
        "regions": ["East Africa", "Sudan", "Darfur"],
        "tags": ["civil-war", "humanitarian-crisis", "famine", "displacement"],
        "intensity_score": 0.85,
    },
    {
        "name": "South China Sea Tensions",
        "description": "Ongoing territorial disputes in the South China Sea involving China's expansive claims versus those of Philippines, Vietnam, Malaysia, and other ASEAN claimants. Regular naval confrontations, island militarization, and freedom of navigation operations by US Navy.",
        "status": "active",
        "category": "political",
        "key_entities": ["China", "Philippines", "Vietnam", "United States", "ASEAN"],
        "regions": ["Southeast Asia", "South China Sea", "Indo-Pacific"],
        "tags": ["territorial-dispute", "maritime", "military-buildup", "freedom-of-navigation"],
        "intensity_score": 0.6,
    },
    {
        "name": "Pakistan-Afghanistan War",
        "description": "Open war between Pakistan and Taliban-governed Afghanistan. Pakistan launched airstrikes on Kabul and Kandahar in February 2026, escalating from border skirmishes to full military operations. TTP safe havens, cross-border militant attacks, and Durand Line disputes remain central drivers.",
        "status": "active",
        "category": "conflict",
        "key_entities": ["Pakistan", "Afghanistan", "Taliban", "Balochistan Liberation Army"],
        "regions": ["South Asia", "Pakistan", "Afghanistan"],
        "tags": ["border-conflict", "terrorism", "militant-groups", "airstrikes"],
        "intensity_score": 0.8,
    },
    {
        "name": "Myanmar Civil War",
        "description": "Multi-front civil war between the military junta and an array of ethnic armed organizations and resistance forces. Territorial control fragmented across the country with heavy fighting in Shan, Rakhine, and Sagaing regions.",
        "status": "active",
        "category": "conflict",
        "key_entities": ["Myanmar"],
        "regions": ["Southeast Asia", "Myanmar"],
        "tags": ["civil-war", "ethnic-conflict", "military-junta"],
        "intensity_score": 0.8,
    },
    {
        "name": "Iran Internal Crisis",
        "description": "Severe political instability inside Iran following the assassination of Supreme Leader Khamenei and sustained US-Israeli military strikes. Regime cohesion under extreme stress, succession uncertainty, and growing domestic unrest.",
        "status": "active",
        "category": "political",
        "key_entities": ["Iran", "Mojtaba Khamenei", "Masoud Pezeshkian"],
        "regions": ["Middle East", "Iran"],
        "tags": ["regime-stability", "succession", "unrest"],
        "intensity_score": 0.85,
    },
    {
        "name": "US Global Trade War",
        "description": "Broad trade war driven by US tariff escalation against China, the EU, and other trading partners. Retaliatory tariffs, supply chain disruption, and economic decoupling accelerating across multiple sectors.",
        "status": "active",
        "category": "economic",
        "key_entities": ["United States", "China", "European Union", "Donald Trump"],
        "regions": ["Global"],
        "tags": ["tariffs", "trade-war", "sanctions", "decoupling"],
        "intensity_score": 0.7,
    },
    {
        "name": "Venezuela Political Transition",
        "description": "Political transition in Venezuela following regime change. Uncertain governance trajectory, international recognition dynamics, and economic reconstruction challenges.",
        "status": "active",
        "category": "political",
        "key_entities": ["Venezuela"],
        "regions": ["South America", "Venezuela"],
        "tags": ["regime-change", "political-transition", "governance"],
        "intensity_score": 0.7,
    },
    {
        "name": "Strait of Hormuz Crisis",
        "description": "Disruption of maritime traffic through the Strait of Hormuz linked to the Iran-Israel-US war. Iran announced selective transit policy in March 2026 -- passage allowed for non-belligerent nations (Japan, China, India, Pakistan) but closed to 'enemy' shipping. IEA called it the largest supply disruption in global oil market history (~10M bpd Gulf production cuts). Brent surged to $126/barrel. Ras Laffan (Qatar, world's largest LNG facility) struck by Iranian missiles. Saudi refineries also hit.",
        "status": "active",
        "category": "economic",
        "key_entities": ["Iran", "United States", "Saudi Arabia", "Qatar", "Strait of Hormuz"],
        "regions": ["Persian Gulf", "Middle East"],
        "tags": ["maritime", "oil-transit", "disruption", "chokepoint", "energy-crisis"],
        "intensity_score": 0.9,
    },
    {
        "name": "Ukraine-Russia Ceasefire Stalemate",
        "description": "Trilateral ceasefire talks (US/Ukraine/Russia) paused due to Iran war as of March 2026. Russian territorial gains at 20-month low. Ukraine recovered 244 sq km in southern Ukraine since January 2026. 500 POWs exchanged. Kremlin confirmed talks 'on pause.' Zelenskyy stated Russia 'does not want to move towards peace.'",
        "status": "active",
        "category": "political",
        "key_entities": ["Russia", "Ukraine", "United States", "Volodymyr Zelenskyy", "Vladimir Putin"],
        "regions": ["Eastern Europe", "Ukraine", "Russia"],
        "tags": ["ceasefire", "diplomacy", "stalemate"],
        "intensity_score": 0.7,
    },
    {
        "name": "Iran Black Rain Environmental Crisis",
        "description": "Toxic 'black rain' phenomenon across multiple Iranian provinces starting mid-March 2026, likely caused by toxic aerosols from damaged oil/petrochemical facilities from airstrikes. Marine pollution along Gulf coast. Respiratory illness surge. WHO issued respiratory advisory for Iran. Environmental groups describe it as 'environmental disaster of the Iran war.'",
        "status": "active",
        "category": "environment",
        "key_entities": ["Iran"],
        "regions": ["Middle East", "Iran", "Persian Gulf"],
        "tags": ["environmental-disaster", "pollution", "health-crisis", "black-rain"],
        "intensity_score": 0.75,
    },
    {
        "name": "South China Sea Escalation",
        "description": "China's land reclamation at Antelope Reef (Paracels) condemned by Vietnam. Preliminary airstrip construction and 50+ structures. Chinese forces confronting Filipino fishing boats near Scarborough Shoal with 6 CCG vessels, 20 militia ships, and 1 PLA Navy warship. Floating barrier deployed at Scarborough Shoal in Dec 2025.",
        "status": "active",
        "category": "conflict",
        "key_entities": ["China", "Philippines", "Vietnam", "United States"],
        "regions": ["Southeast Asia", "South China Sea", "Indo-Pacific"],
        "tags": ["territorial-dispute", "maritime", "military-buildup", "land-reclamation"],
        "intensity_score": 0.65,
    },
    {
        "name": "Global Energy Crisis (Iran War Spillover)",
        "description": "Largest oil supply disruption in history triggered by Iran-Israel-US war. ~10M bpd Gulf production cuts. Brent peaked at $126/barrel. Saudi and Qatari energy infrastructure struck by Iranian missiles. S&P 500 fell ~4.55% in three weeks. India's industry disrupted by gas supply shortages. Russia offering additional oil/gas to Europe. US lifting some Iran oil sanctions under energy pressure.",
        "status": "active",
        "category": "economic",
        "key_entities": ["Iran", "Saudi Arabia", "Qatar", "United States", "OPEC"],
        "regions": ["Global", "Persian Gulf", "Middle East"],
        "tags": ["energy-crisis", "oil-price", "supply-disruption", "economic-impact"],
        "intensity_score": 0.85,
    },
]


# ============================================================================
# WATCHLIST DATA
# ============================================================================

WATCHLIST_ITEMS = [
    {
        "name": "Strait of Hormuz Closure/Blockade",
        "description": "Strait of Hormuz is currently disrupted due to Iran-Israel-US conflict. Monitor for full closure, blockade escalation, mine-clearing operations, and impact on ~20% of global oil transit.",
        "entities": ["Iran", "United States", "Saudi Arabia", "Strait of Hormuz"],
        "keywords": ["Hormuz", "strait closure", "oil blockade", "naval blockade", "shipping disruption", "mine laying"],
        "categories": ["conflict", "economic"],
        "regions": ["Middle East", "Persian Gulf"],
        "priority": "critical",
    },
    {
        "name": "Iran Nuclear Program Status",
        "description": "Track developments in Iran's nuclear enrichment program, IAEA inspections, breakout timeline estimates, and potential military dimensions.",
        "entities": ["Iran", "IAEA", "United States", "Israel"],
        "keywords": ["uranium enrichment", "nuclear weapon", "IAEA inspection", "breakout time", "nuclear deal", "centrifuge"],
        "categories": ["technology", "conflict", "political"],
        "regions": ["Middle East", "Iran"],
        "priority": "critical",
    },
    {
        "name": "Ukraine Ceasefire Negotiations",
        "description": "Monitor any ceasefire talks, peace negotiations, or diplomatic initiatives between Russia and Ukraine. Track territorial concessions and security guarantee discussions.",
        "entities": ["Russia", "Ukraine", "United States", "European Union", "NATO", "Vladimir Putin", "Volodymyr Zelenskyy", "Donald Trump"],
        "keywords": ["ceasefire", "peace talks", "negotiations", "territorial concession", "security guarantees", "peace deal", "armistice"],
        "categories": ["conflict", "political"],
        "regions": ["Eastern Europe", "Ukraine", "Russia"],
        "priority": "high",
    },
    {
        "name": "Sudan Humanitarian Crisis Escalation",
        "description": "Track humanitarian situation in Sudan including famine declarations, mass displacement, civilian casualties, and aid access disruptions.",
        "entities": ["Sudan", "RSF", "Abdel Fattah al-Burhan", "WHO", "United Nations"],
        "keywords": ["famine", "humanitarian crisis", "displacement", "civilian casualties", "aid access", "Darfur", "Khartoum"],
        "categories": ["disaster", "conflict", "health"],
        "regions": ["East Africa", "Sudan"],
        "priority": "high",
    },
    {
        "name": "North Korea Missile/Nuclear Tests",
        "description": "Monitor North Korean ballistic missile launches, nuclear tests, satellite launches, and provocative military activities.",
        "entities": ["North Korea", "Kim Jong-un", "South Korea", "Japan", "United States"],
        "keywords": ["missile launch", "ICBM", "nuclear test", "ballistic missile", "satellite launch", "Hwasong", "provocat"],
        "categories": ["conflict", "technology"],
        "regions": ["East Asia", "Korean Peninsula"],
        "priority": "high",
    },
    {
        "name": "Taiwan Strait Military Activity",
        "description": "Track Chinese military activity around Taiwan including PLA exercises, air defense zone incursions, naval movements, and escalatory rhetoric about reunification.",
        "entities": ["China", "Taiwan", "United States", "Xi Jinping"],
        "keywords": ["Taiwan Strait", "PLA exercise", "air defense zone", "reunification", "invasion", "blockade", "military drill"],
        "categories": ["conflict", "political"],
        "regions": ["East Asia", "Taiwan Strait", "Indo-Pacific"],
        "priority": "high",
    },
    {
        "name": "Global Oil Price Shocks",
        "description": "Monitor events that could cause sudden large moves in global oil prices: OPEC decisions, supply disruptions, geopolitical events affecting production or transit.",
        "entities": ["OPEC", "Saudi Arabia", "Russia", "United States", "Iran"],
        "keywords": ["oil price", "crude oil", "OPEC production", "supply disruption", "oil shock", "barrel", "Brent", "WTI"],
        "categories": ["economic"],
        "regions": ["Middle East", "Global"],
        "priority": "normal",
    },
    {
        "name": "ISIS Resurgence Indicators",
        "description": "Track indicators of Islamic State resurgence including prison breaks, major attacks, territorial gains, leadership changes, or increased propaganda output.",
        "entities": ["Islamic State", "Iraq", "Syria"],
        "keywords": ["ISIS resurgence", "Islamic State attack", "prison break", "caliphate", "ISIL", "Daesh", "ISIS-K"],
        "categories": ["conflict"],
        "regions": ["Middle East", "Sahel", "Central Asia"],
        "priority": "normal",
    },
    {
        "name": "Iran Regime Stability",
        "description": "Monitor internal stability of the Iranian regime following Khamenei assassination and sustained military strikes. Track succession dynamics, IRGC cohesion, public unrest, and government continuity.",
        "entities": ["Iran", "Mojtaba Khamenei", "Masoud Pezeshkian"],
        "keywords": ["Iran regime", "succession", "IRGC", "supreme leader", "unrest", "stability", "Iran collapse"],
        "categories": ["political", "conflict"],
        "regions": ["Middle East", "Iran"],
        "priority": "critical",
    },
    {
        "name": "Pakistan-Afghanistan War Escalation",
        "description": "Monitor escalation of Pakistan-Afghanistan war including further airstrikes, ground incursions, refugee flows, and nuclear risk from destabilization of a nuclear-armed state.",
        "entities": ["Pakistan", "Afghanistan", "Taliban", "Balochistan Liberation Army"],
        "keywords": ["Pakistan airstrikes", "Kabul", "Kandahar", "Durand Line", "TTP", "Pakistan-Afghanistan war", "escalation"],
        "categories": ["conflict"],
        "regions": ["South Asia", "Pakistan", "Afghanistan"],
        "priority": "high",
    },
    {
        "name": "US-China Trade Tensions",
        "description": "Track escalation or de-escalation of US-China trade tensions including tariff actions, export controls, supply chain shifts, and retaliatory measures.",
        "entities": ["United States", "China", "Donald Trump", "Xi Jinping"],
        "keywords": ["tariff", "trade war", "export controls", "decoupling", "semiconductor", "rare earth", "retaliation"],
        "categories": ["economic", "political"],
        "regions": ["Global", "East Asia", "North America"],
        "priority": "high",
    },
    {
        "name": "Venezuela Political Stability",
        "description": "Monitor political transition in Venezuela following regime change. Track governance formation, international recognition, economic recovery, and potential for counter-revolution or instability.",
        "entities": ["Venezuela"],
        "keywords": ["Venezuela", "regime change", "transition", "governance", "recognition", "Maduro", "opposition"],
        "categories": ["political"],
        "regions": ["South America", "Venezuela"],
        "priority": "normal",
    },
    {
        "name": "Iran Black Rain / Environmental Contamination",
        "description": "Monitor reports of toxic rainfall, atmospheric contamination, and environmental health impacts in Iran linked to damage from airstrikes on petrochemical facilities. Track lab analyses, WHO advisories, and marine pollution reports.",
        "entities": ["Iran"],
        "keywords": ["black rain", "toxic rain", "environmental contamination", "petrochemical", "respiratory", "pollution", "marine contamination"],
        "categories": ["environment", "health"],
        "regions": ["Middle East", "Iran", "Persian Gulf"],
        "priority": "high",
    },
    {
        "name": "Iran War Ceasefire/Exit Signals",
        "description": "Monitor for ceasefire offers, exit plans, congressional actions, or diplomatic back-channels related to ending the Iran-Israel-US war. Track Kata'ib Hezbollah conditional ceasefire and congressional budget debates.",
        "entities": ["Iran", "United States", "Israel"],
        "keywords": ["ceasefire", "exit plan", "peace deal", "conditional ceasefire", "congressional", "war powers", "de-escalation"],
        "categories": ["conflict", "political"],
        "regions": ["Middle East", "North America"],
        "priority": "critical",
    },
    {
        "name": "Gulf Energy Infrastructure Attacks",
        "description": "Monitor attacks on energy infrastructure in the Persian Gulf region, including Saudi refineries, Qatari LNG facilities, and oil shipping. Track operational status of Ras Laffan and major refinery complexes.",
        "entities": ["Iran", "Saudi Arabia", "Qatar", "United Arab Emirates", "Kuwait"],
        "keywords": ["refinery attack", "LNG facility", "Ras Laffan", "oil infrastructure", "pipeline", "energy attack", "Aramco"],
        "categories": ["conflict", "economic"],
        "regions": ["Persian Gulf", "Middle East"],
        "priority": "critical",
    },
    {
        "name": "South China Sea Land Reclamation",
        "description": "Monitor Chinese military construction and land reclamation activities in disputed South China Sea features, particularly Antelope Reef airstrip construction and Scarborough Shoal barrier.",
        "entities": ["China", "Philippines", "Vietnam"],
        "keywords": ["land reclamation", "airstrip", "Scarborough Shoal", "Antelope Reef", "Paracels", "Spratlys", "artificial island"],
        "categories": ["conflict", "political"],
        "regions": ["Southeast Asia", "South China Sea"],
        "priority": "high",
    },
]


# ============================================================================
# GOALS DATA
# ============================================================================

GOALS = [
    {
        "description": "Build comprehensive knowledge graph of active conflicts",
        "priority": 3,
        "success_criteria": [
            "All major active conflicts represented with situation tracking",
            "Key actors, relationships, and alliances mapped in graph",
            "Conflict timelines populated with verified events",
            "Entity profiles for major belligerents at completeness > 0.6",
        ],
    },
    {
        "description": "Establish diverse source portfolio across all regions",
        "priority": 3,
        "success_criteria": [
            "At least 5 active sources per major region",
            "Mix of wire services, local media, and data feeds",
            "Source reliability scores calibrated through operation",
            "Coverage gaps identified and addressed",
        ],
    },
    {
        "description": "Track and profile key actors in Iran-Israel-US conflict",
        "priority": 2,
        "success_criteria": [
            "Entity profiles for all key state and non-state actors",
            "Proxy network relationships mapped in graph",
            "Military capability assessments for Iran, Israel, Hezbollah, Hamas",
            "Timeline of major escalatory events maintained",
        ],
    },
    {
        "description": "Monitor humanitarian crises (Sudan, Gaza, Yemen, Iran)",
        "priority": 3,
        "success_criteria": [
            "Situation tracking active for each crisis",
            "Displacement and casualty data tracked as facts",
            "Aid organization operations monitored",
            "Escalation indicators on watchlist",
        ],
    },
    {
        "description": "Develop analytical coverage of economic and trade developments",
        "priority": 5,
        "success_criteria": [
            "Major trade policy changes tracked as events",
            "Sanctions regimes documented in fact store",
            "Energy market developments monitored",
            "Key economic indicators available for major economies",
        ],
    },
]


# ============================================================================
# SEED FUNCTIONS
# ============================================================================

async def seed_entities(conn: asyncpg.Connection) -> int:
    """Seed entity profiles for countries, leaders, organizations, armed groups."""
    count = 0

    # Countries
    for name in COUNTRIES:
        eid = uuid4()
        country_data = {
            "id": str(eid),
            "name": name,
            "entity_type": "country",
            "aliases": [],
            "assertions": {},
            "tags": ["seed"],
            "completeness_score": 0.5,
        }
        geo = COUNTRY_GEO.get(name)
        if geo:
            country_data["geo_lat"] = geo[0]
            country_data["geo_lon"] = geo[1]
        data = json.dumps(country_data)
        result = await conn.execute(
            """INSERT INTO entity_profiles (id, data, canonical_name, entity_type, entity_class, completeness_score, created_at, updated_at)
               VALUES ($1, $2::jsonb, $3, 'country', 'country', 0.5, $4, $4)
               ON CONFLICT (LOWER(canonical_name), entity_class) DO NOTHING""",
            eid, data, name, NOW,
        )
        if "INSERT 0 1" in result:
            count += 1

    # Leaders
    for name, title, country in LEADERS:
        eid = uuid4()
        data = json.dumps({
            "id": str(eid),
            "name": name,
            "entity_type": "person",
            "aliases": [],
            "assertions": {
                "title": title,
                "country": country,
            },
            "tags": ["seed", "head-of-state"],
            "completeness_score": 0.4,
        })
        result = await conn.execute(
            """INSERT INTO entity_profiles (id, data, canonical_name, entity_type, entity_class, completeness_score, created_at, updated_at)
               VALUES ($1, $2::jsonb, $3, 'person', 'person', 0.4, $4, $4)
               ON CONFLICT (LOWER(canonical_name), entity_class) DO NOTHING""",
            eid, data, name, NOW,
        )
        if "INSERT 0 1" in result:
            count += 1

    # Organizations
    for name in ORGANIZATIONS:
        eid = uuid4()
        data = json.dumps({
            "id": str(eid),
            "name": name,
            "entity_type": "organization",
            "aliases": [],
            "assertions": {},
            "tags": ["seed"],
            "completeness_score": 0.3,
        })
        result = await conn.execute(
            """INSERT INTO entity_profiles (id, data, canonical_name, entity_type, entity_class, completeness_score, created_at, updated_at)
               VALUES ($1, $2::jsonb, $3, 'organization', 'organization', 0.3, $4, $4)
               ON CONFLICT (LOWER(canonical_name), entity_class) DO NOTHING""",
            eid, data, name, NOW,
        )
        if "INSERT 0 1" in result:
            count += 1

    # Armed groups
    for name in ARMED_GROUPS:
        eid = uuid4()
        aliases = []
        if name == "Islamic State":
            aliases = ["ISIS", "ISIL", "Daesh", "IS"]
        elif name == "Houthis":
            aliases = ["Ansar Allah"]
        elif name == "RSF":
            aliases = ["Rapid Support Forces"]
        elif name == "PKK":
            aliases = ["Kurdistan Workers Party"]
        elif name == "M23":
            aliases = ["March 23 Movement"]

        data = json.dumps({
            "id": str(eid),
            "name": name,
            "entity_type": "armed_group",
            "aliases": aliases,
            "assertions": {},
            "tags": ["seed"],
            "completeness_score": 0.3,
        })
        result = await conn.execute(
            """INSERT INTO entity_profiles (id, data, canonical_name, entity_type, entity_class, completeness_score, created_at, updated_at)
               VALUES ($1, $2::jsonb, $3, 'armed_group', 'armed_group', 0.3, $4, $4)
               ON CONFLICT (LOWER(canonical_name), entity_class) DO NOTHING""",
            eid, data, name, NOW,
        )
        if "INSERT 0 1" in result:
            count += 1

    return count


async def seed_facts(conn: asyncpg.Connection) -> int:
    """Seed verified facts: leaders, capitals, borders, memberships, geopolitics."""
    count = 0

    all_facts = (
        LEADER_FACTS
        + LEADER_TITLE_FACTS
        + CAPITAL_FACTS
        + BORDER_FACTS
        + MEMBERSHIP_FACTS
        + GEOPOLITICAL_FACTS
        + ARMED_GROUP_FACTS
        + REGION_FACTS
        + NUCLEAR_FACTS
        + UNSC_FACTS
        + WATERWAY_FACTS
        + CONFLICT_STATUS_FACTS
        + POPULATION_FACTS
    )

    for subject, predicate, value, confidence in all_facts:
        fid = uuid4()
        data = json.dumps({
            "id": str(fid),
            "subject": subject,
            "predicate": predicate,
            "value": str(value),
            "confidence": confidence,
            "source_cycle": 0,
            "source": "seed_data",
        })
        try:
            result = await conn.execute(
                """INSERT INTO facts (id, subject, predicate, value, confidence, source_cycle, data, created_at)
                   VALUES ($1, $2, $3, $4, $5, 0, $6::jsonb, $7)
                   ON CONFLICT (id) DO NOTHING""",
                fid, subject, predicate, str(value), confidence, data, NOW,
            )
            if "INSERT 0 1" in result:
                count += 1
        except Exception:
            pass  # Skip duplicates from existing seed dump

    return count


async def seed_situations(conn: asyncpg.Connection) -> int:
    """Seed active situations."""
    count = 0

    for sit in SITUATIONS:
        sid = uuid4()
        data = json.dumps({
            "id": str(sid),
            "name": sit["name"],
            "description": sit["description"],
            "status": sit["status"],
            "category": sit["category"],
            "key_entities": sit["key_entities"],
            "regions": sit["regions"],
            "tags": sit["tags"],
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "event_count": 0,
            "intensity_score": sit["intensity_score"],
        })
        result = await conn.execute(
            """INSERT INTO situations (id, data, name, status, category, intensity_score, created_at, updated_at)
               VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7, $7)
               ON CONFLICT (id) DO NOTHING""",
            sid, data, sit["name"], sit["status"], sit["category"],
            sit["intensity_score"], NOW,
        )
        if "INSERT 0 1" in result:
            count += 1

    return count


async def seed_watchlist(conn: asyncpg.Connection) -> int:
    """Seed watchlist items."""
    count = 0

    for item in WATCHLIST_ITEMS:
        wid = uuid4()
        data = json.dumps({
            "id": str(wid),
            "name": item["name"],
            "description": item["description"],
            "entities": item["entities"],
            "keywords": item["keywords"],
            "categories": item["categories"],
            "regions": item["regions"],
            "priority": item["priority"],
            "active": True,
            "created_at": NOW.isoformat(),
            "last_triggered_at": None,
            "trigger_count": 0,
        }, default=str)

        # Check for existing watch with same name
        existing = await conn.fetchrow(
            "SELECT id FROM watchlist WHERE lower(name) = $1 LIMIT 1",
            item["name"].lower(),
        )
        if existing:
            continue

        result = await conn.execute(
            """INSERT INTO watchlist (id, data, name, priority, active, created_at)
               VALUES ($1, $2::jsonb, $3, $4, true, $5)""",
            wid, data, item["name"], item["priority"], NOW,
        )
        if "INSERT 0 1" in result:
            count += 1

    return count


async def seed_goals(conn: asyncpg.Connection) -> int:
    """Seed initial goals."""
    count = 0

    for goal in GOALS:
        gid = uuid4()
        data = json.dumps({
            "id": str(gid),
            "description": goal["description"],
            "goal_type": "goal",
            "priority": goal["priority"],
            "status": "active",
            "source": "seed",
            "parent_id": None,
            "child_ids": [],
            "context": {},
            "constraints": [],
            "success_criteria": goal["success_criteria"],
            "progress_pct": 0.0,
            "milestones": [],
            "blocked_by": [],
            "blocks": [],
            "created_at": NOW.isoformat(),
            "started_at": None,
            "completed_at": None,
            "last_progress_at": None,
            "deferred_until_cycle": None,
            "defer_reason": None,
            "completion_reason": None,
            "result_summary": None,
        }, default=str)

        # Check for existing goal with same description
        existing = await conn.fetchrow(
            "SELECT id FROM goals WHERE data->>'description' = $1 LIMIT 1",
            goal["description"],
        )
        if existing:
            continue

        result = await conn.execute(
            """INSERT INTO goals (id, data, status, goal_type, priority, created_at, updated_at)
               VALUES ($1, $2::jsonb, 'active', 'goal', $3, $4, $4)""",
            gid, data, goal["priority"], NOW,
        )
        if "INSERT 0 1" in result:
            count += 1

    return count


# ============================================================================
# MAIN
# ============================================================================

async def main():
    print("Connecting to Postgres...")
    try:
        conn = await asyncpg.connect(**DB_CONFIG)
    except Exception as e:
        print(f"  ERROR: Could not connect to Postgres: {e}")
        print(f"  Config: host={DB_CONFIG['host']} port={DB_CONFIG['port']} db={DB_CONFIG['database']}")
        sys.exit(1)

    try:
        print("Seeding entity profiles...")
        n = await seed_entities(conn)
        print(f"  -> {n} entities seeded")

        print("Seeding facts...")
        n = await seed_facts(conn)
        print(f"  -> {n} facts seeded")

        # NOTE: Situations, watchlist, hypotheses, and goals are NOT seeded.
        # These are reasoning-derived analytical products that the agent creates
        # through its own analysis of events and the mission. The seed is a
        # knowledge package (entities, facts, sources), not operational state.

        # Summary counts
        entity_count = await conn.fetchval("SELECT count(*) FROM entity_profiles")
        fact_count = await conn.fetchval("SELECT count(*) FROM facts")

        print()
        print("=== Database totals ===")
        print(f"  Entity profiles: {entity_count}")
        print(f"  Facts:           {fact_count}")
        print()
        print("Seed data complete. Agent will derive situations, watchlist,")
        print("goals, and hypotheses from the mission and incoming data.")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
