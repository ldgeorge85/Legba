# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A live-shaped, fully synthetic ``event_clustering`` substrate (N-W).

Shapes measured on live 2026-11-13 (no live text is copied): an open set of
~2,000 events per producer with headline-length titles and tens of entity
names each, a signal page of 59-300 rows where many rows retell a story an
open event already carries (so the match walk reattaches, mints and refuses),
and a handful of tower candidates. Everything derives from a seeded RNG, and
every id is a ``uuid5`` of its position, so two runs build the same world.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from legba.data.analysts.deterministic_handlers._event_candidates import (
    EventEvidence,
)

NOW = datetime(2026, 11, 13, 18, 0, 0, tzinfo=timezone.utc)

_PLACES = [
    ("Basra", "iq"), ("Lima", "pe"), ("Manila", "ph"), ("Ankara", "tr"),
    ("Antwerp", "be"), ("Khartoum", "sd"), ("Kyiv", "ua"), ("Gaza", "ps"),
    ("Taipei", "tw"), ("Caracas", "ve"), ("Lagos", "ng"), ("Dhaka", "bd"),
    ("North Kivu", "cd"), ("South Sudan", "ss"), ("West Bank", "ps"),
    ("Tehran", "ir"), ("Seoul", "kr"), ("Quito", "ec"), ("Beirut", "lb"),
]
_ACTORS = [
    "United Nations", "European Union", "NATO", "World Bank", "IMF",
    "Red Cross", "African Union", "OPEC", "the Kremlin", "the Pentagon",
    "Hamas", "Hezbollah", "Wagner Group", "RSF", "M23", "ASEAN",
    "Iran", "Russia", "China", "United States", "Turkey", "Israel",
]
_VERBS = [
    "strike hits", "talks resume in", "floods swamp", "protest grips",
    "ceasefire holds in", "blast rocks", "election called in",
    "fuel shortage hits", "port closed in", "sanctions target",
]
_CATEGORIES = [
    "conflict", "politics", "disaster", "economy", "labor", "health",
    "security", "energy", "diplomacy", "migration",
]
_DETAILS = [
    "dead", "displaced", "arrested", "missing", "injured", "evacuated",
    "tonnes stranded", "flights cancelled", "homes destroyed", "troops moved",
]
_FILLER = [
    "officials say", "after overnight clashes", "as talks stall",
    "amid rising prices", "witnesses report", "state media says",
    "for a second day", "despite warnings", "according to reports",
]


def _uid(*parts: Any) -> UUID:
    return uuid5(NAMESPACE_URL, "nw/" + "/".join(str(p) for p in parts))


def _story(rng: random.Random, n: int) -> list[dict[str, Any]]:
    stories = []
    for i in range(n):
        place, geo = rng.choice(_PLACES)
        stories.append({
            "i": i,
            "place": place,
            "geo": geo,
            "verb": rng.choice(_VERBS),
            "category": rng.choice(_CATEGORIES),
            "actors": rng.sample(_ACTORS, rng.randint(2, 5)),
            "detail": f"{rng.randint(2, 900)} {rng.choice(_DETAILS)}",
        })
    return stories


def _headline(rng: random.Random, story: dict[str, Any]) -> str:
    words = [story["actors"][0], story["verb"], story["place"], story["detail"]]
    words += rng.sample(_FILLER, rng.randint(0, 2))
    if rng.random() < 0.4:
        words.insert(0, rng.choice(story["actors"]))
    return " ".join(words)


def _entity_names(rng: random.Random, story: dict[str, Any], k: int) -> list[str]:
    names = list(story["actors"]) + [story["place"]]
    names += [f"{rng.choice(_ACTORS)} spokesperson {rng.randint(1, 40)}"
              for _ in range(k)]
    return names


def open_events(
    n: int, *, seed: int, producer: str, stories: int = 400, mentions: int = 8
) -> list[dict[str, Any]]:
    """``n`` open-event rows in ``_open_event_read``'s projection."""
    rng = random.Random(f"{seed}/{producer}")
    pool = _story(random.Random(seed), stories)
    rows = []
    for i in range(n):
        st = pool[i % len(pool)]
        at = NOW - timedelta(hours=rng.uniform(1, 72))
        rows.append({
            "id": _uid(producer, "event", i),
            "event_signature": f"sig/{producer}/{i}",
            "title": _headline(rng, st),
            "category": st["category"],
            "geo": [st["geo"]],
            "time_start": at - timedelta(hours=6),
            "time_end": at,
            "updated_at": at,
            "latest_linked_at": at,
            "signal_ids": [_uid(producer, "sig", i, k) for k in range(3)],
            "source_ids": [f"src_{rng.randint(1, 60)}"],
            "entity_ids": [],
            "entity_names": _entity_names(rng, st, rng.randint(0, mentions)),
        })
    return rows


def signal_page(
    n: int, *, seed: int, stories: int = 400
) -> list[EventEvidence]:
    """``n`` signals over the SAME story pool, in ``(fetched_at, id)`` order:
    most retell an open event's story (reattach), some pair up across sources
    (mint), the rest stand alone (decline)."""
    rng = random.Random(f"{seed}/signals")
    pool = _story(random.Random(seed), stories)
    out = []
    for i in range(n):
        st = rng.choice(pool[: max(1, len(pool) // 3)]) if rng.random() < 0.7 \
            else rng.choice(pool)
        at = NOW - timedelta(hours=60) + timedelta(minutes=10 * i)
        out.append(EventEvidence(
            id=_uid("signal", seed, i),
            title=_headline(rng, st),
            category=st["category"],
            fetched_at=at,
            source_id=f"rss_{rng.randint(1, 80)}",
            source_class="reporting",
            geo=(st["geo"],),
            entity_names=tuple(_entity_names(rng, st, rng.randint(0, 4))),
        ))
    out.sort(key=lambda e: (e.fetched_at, str(e.id)))
    return out


def tower_candidates(candidates: list[Any], *, k: int) -> list[Any]:
    """``k`` tower-leg candidates derived from cluster candidates (kind
    ``finding``, a source row id and a cursor of their own)."""
    out = []
    for i, c in enumerate(candidates[:k]):
        out.append(replace(
            c,
            kind="finding",
            promoted=True,
            decline_reason="",
            source_ref_id=_uid("finding", i),
            cursor_ts=NOW - timedelta(days=2, minutes=-i),
            cursor_id=_uid("finding", i),
        ))
    return out
