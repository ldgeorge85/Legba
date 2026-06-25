# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""wikidata_leaders — post-SPARQL bare-QID label resolution (wbgetentities).

The SPARQL ``wikibase:label`` service occasionally returns a BARE ``Qxxxx`` id
instead of a name (live-observed for some P6 head_of_government rows — US
``Q22686``, Mexico, Serbia). For the US BOTH the head_of_state and
head_of_government rows come back unlabelled, so simply dropping bare-QID rows
leaves the US with NO usable office fact and breaks grounding.

These tests prove the fix: ``fetch`` runs a single batched ``wbgetentities``
call (via the same client-factory seam the SPARQL path uses), substitutes the
resolved English label for the bare QID in the binding's ``*Label`` cell, and
so ``map`` emits the NAME (not the QID) — for both the leader FACT value and the
``SeedEntity`` canonical_name. A QID the API can't resolve is still dropped; a
network failure degrades to the same drop (no crash); the plain labelled fixture
path is unaffected.
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.data.seed import SeedContext, SeedEntity, SeedFact
from legba.data.seed.adapters import wikidata_leaders as wl
from legba.data.seed.adapters.wikidata_leaders import WikidataLeadersSeedSource


def _cell(value: str) -> dict[str, str]:
    return {"value": value}


# A leaders fixture whose US rows are BOTH bare QIDs (the live-observed case),
# alongside a normally-labelled leader (France) that must pass through untouched.
def _fixture_with_bare_us() -> dict[str, Any]:
    return {
        "leaders": [
            {
                "country": _cell("http://www.wikidata.org/entity/Q30"),
                "countryLabel": _cell("United States"),
                "leader": _cell("http://www.wikidata.org/entity/Q22686"),
                # label service returned the BARE QID — must be resolved.
                "leaderLabel": _cell("Q22686"),
                "role": _cell("head_of_government"),
                "start": _cell("+2025-01-20T00:00:00Z"),
            },
            {
                "country": _cell("http://www.wikidata.org/entity/Q142"),
                "countryLabel": _cell("France"),
                "leader": _cell("http://www.wikidata.org/entity/Q3052772"),
                "leaderLabel": _cell("Emmanuel Macron"),
                "role": _cell("head_of_state"),
                "start": _cell("+2017-05-14T00:00:00Z"),
            },
        ],
        "alliances": [],
    }


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Stands in for the guarded httpx async client (records the GET call)."""

    def __init__(self, payload: dict[str, Any], record: dict[str, Any]) -> None:
        self._payload = payload
        self._record = record

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(self, url: str, params=None, headers=None, **kw: Any) -> _FakeResponse:
        self._record.setdefault("calls", []).append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        )
        return _FakeResponse(self._payload)


def _factory_for(payload: dict[str, Any], record: dict[str, Any]):
    def _make() -> _FakeClient:
        return _FakeClient(payload, record)

    return _make


class _BoomClient:
    async def __aenter__(self) -> "_BoomClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(self, *a: Any, **k: Any):
        raise RuntimeError("simulated egress failure")


# ---------------------------------------------------------------------------
# resolve → emit NAME
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_qid_leader_resolved_to_name():
    record: dict[str, Any] = {}
    api_payload = {
        "entities": {
            "Q22686": {"labels": {"en": {"language": "en", "value": "Donald Trump"}}}
        }
    }
    adapter = WikidataLeadersSeedSource()
    adapter._sparql_client_factory = _factory_for(api_payload, record)

    raw = await adapter.fetch(SeedContext(options={"sparql_json": _fixture_with_bare_us()}))
    payloads = list(adapter.map(raw))

    facts = [p for p in payloads if isinstance(p, SeedFact)]
    entities = [p for p in payloads if isinstance(p, SeedEntity)]

    # The US leader FACT carries the resolved NAME, never the QID.
    leader_of = [f for f in facts if f.predicate == "LeaderOf"]
    leader_subjects = {f.subject for f in leader_of}
    assert "Donald Trump" in leader_subjects
    assert "Q22686" not in leader_subjects
    assert all(s != "Q22686" for s in leader_subjects)

    # The country-subject office fact for the US — the fixture's role is
    # head_of_government (P6), so it lands on the `head of government` predicate
    # (DQ-#85.3 typing split) — points at the resolved NAME, not the QID.
    office_gov = [f for f in facts if f.predicate == "head of government"]
    us_office = next(f for f in office_gov if f.subject == "United States")
    assert us_office.value == "Donald Trump"

    # The SeedEntity canonical_name is the resolved NAME too (not the QID).
    person_names = {e.canonical_name for e in entities if e.entity_class == "person"}
    assert "Donald Trump" in person_names
    assert "Q22686" not in person_names

    # Exactly ONE batched wbgetentities call, to the Action API, with our UA.
    calls = record.get("calls", [])
    assert len(calls) == 1, "label resolution must be a single batched call"
    call = calls[0]
    assert call["url"] == wl.WIKIDATA_API_ENDPOINT
    assert call["params"]["action"] == "wbgetentities"
    assert call["params"]["ids"] == "Q22686"
    assert call["params"]["props"] == "labels|sitelinks"
    assert call["params"]["languages"] == "en"
    assert "legba-seed-wikidata" in call["headers"].get("User-Agent", "")


@pytest.mark.asyncio
async def test_bare_qid_resolved_via_enwiki_sitelink_fallback():
    # The LIVE-observed shape for Q22686 (Donald Trump): the entity carries
    # labels in dozens of languages but NO ``en`` key, while the enwiki sitelink
    # title IS the human name. The resolver must fall back to that title.
    record: dict[str, Any] = {}
    api_payload = {
        "entities": {
            "Q22686": {
                "labels": {"fr": {"value": "Donald Trump"}},  # no en
                "sitelinks": {"enwiki": {"title": "Donald Trump"}},
            }
        }
    }
    adapter = WikidataLeadersSeedSource()
    adapter._sparql_client_factory = _factory_for(api_payload, record)

    raw = await adapter.fetch(SeedContext(options={"sparql_json": _fixture_with_bare_us()}))
    payloads = list(adapter.map(raw))
    leader_subjects = {
        f.subject for f in payloads if isinstance(f, SeedFact) and f.predicate == "LeaderOf"
    }
    assert "Donald Trump" in leader_subjects
    assert "Q22686" not in leader_subjects
    # The request asked for sitelinks too (so the fallback has data to use).
    assert record["calls"][0]["params"]["props"] == "labels|sitelinks"


@pytest.mark.asyncio
async def test_unresolvable_qid_still_dropped():
    # The API returns NO usable label for Q22686 → it stays bare → map drops it.
    record: dict[str, Any] = {}
    api_payload = {"entities": {"Q22686": {"labels": {}}}}
    adapter = WikidataLeadersSeedSource()
    adapter._sparql_client_factory = _factory_for(api_payload, record)

    raw = await adapter.fetch(SeedContext(options={"sparql_json": _fixture_with_bare_us()}))
    payloads = list(adapter.map(raw))

    facts = [p for p in payloads if isinstance(p, SeedFact)]
    leader_subjects = {f.subject for f in facts if f.predicate == "LeaderOf"}
    values = {f.value for f in facts} | {f.subject for f in facts}
    # The US leader is dropped (unresolvable); France still maps.
    assert "Q22686" not in values, "a labelless QID must never be emitted"
    assert "Emmanuel Macron" in leader_subjects


@pytest.mark.asyncio
async def test_label_api_failure_degrades_to_drop():
    # Network failure on the label call → log + fall back to the bare-QID drop;
    # the seed must NOT crash, and the labelled France leader still maps.
    adapter = WikidataLeadersSeedSource()
    adapter._sparql_client_factory = lambda: _BoomClient()

    raw = await adapter.fetch(SeedContext(options={"sparql_json": _fixture_with_bare_us()}))
    payloads = list(adapter.map(raw))

    facts = [p for p in payloads if isinstance(p, SeedFact)]
    values = {f.value for f in facts} | {f.subject for f in facts}
    leader_subjects = {f.subject for f in facts if f.predicate == "LeaderOf"}
    assert "Q22686" not in values
    assert "Emmanuel Macron" in leader_subjects


@pytest.mark.asyncio
async def test_plain_labelled_fixture_makes_no_api_call():
    # No bare QIDs anywhere → resolution is a no-op (no wbgetentities call) and
    # the existing fixture mapping is unchanged.
    record: dict[str, Any] = {}
    adapter = WikidataLeadersSeedSource()
    adapter._sparql_client_factory = _factory_for({"entities": {}}, record)
    fixture = {
        "leaders": [
            {
                "country": _cell("http://www.wikidata.org/entity/Q142"),
                "countryLabel": _cell("France"),
                "leader": _cell("http://www.wikidata.org/entity/Q3052772"),
                "leaderLabel": _cell("Emmanuel Macron"),
                "role": _cell("head_of_state"),
                "start": _cell("+2017-05-14T00:00:00Z"),
            },
        ],
        "alliances": [],
    }
    raw = await adapter.fetch(SeedContext(options={"sparql_json": fixture}))
    payloads = list(adapter.map(raw))

    facts = [p for p in payloads if isinstance(p, SeedFact)]
    assert any(f.subject == "Emmanuel Macron" for f in facts if f.predicate == "LeaderOf")
    assert record.get("calls") is None, "no bare QIDs → no wbgetentities call"


@pytest.mark.asyncio
async def test_resolves_bare_qid_in_alliance_country_too():
    # A bare-QID country on the alliance path is resolved + chunking is exercised
    # implicitly (single chunk here); the bloc keeps its label.
    record: dict[str, Any] = {}
    api_payload = {
        "entities": {
            "Q145": {"labels": {"en": {"value": "United Kingdom"}}},
        }
    }
    adapter = WikidataLeadersSeedSource()
    adapter._sparql_client_factory = _factory_for(api_payload, record)
    fixture = {
        "leaders": [],
        "alliances": [
            {
                "country": _cell("http://www.wikidata.org/entity/Q145"),
                "countryLabel": _cell("Q145"),
                "bloc": _cell("http://www.wikidata.org/entity/Q7184"),
                "blocLabel": _cell("NATO"),
                "start": _cell("+1949-04-04T00:00:00Z"),
            }
        ],
    }
    raw = await adapter.fetch(SeedContext(options={"sparql_json": fixture}))
    from legba.data.seed import SeedNexus

    payloads = list(adapter.map(raw))
    nexuses = [p for p in payloads if isinstance(p, SeedNexus)]
    assert len(nexuses) == 1
    assert nexuses[0].subject == "United Kingdom"
    assert nexuses[0].object == "NATO"
