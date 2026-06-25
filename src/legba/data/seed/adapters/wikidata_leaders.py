# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""wikidata_leaders — a Wikidata SPARQL seed adapter (flavor b, tier 1).

The first *structured-external* seed adapter (the curated-YAML
``world_baseline`` adapter proved the path with zero network dependency; this
one pulls the same shape of knowledge from a live authoritative source). It
queries the Wikidata Query Service (WDQS) SPARQL endpoint for:

  * **Current heads of state / government** (the office-holder a country
    analyst should assume by default) with their real term start dates →
    :class:`SeedFact` rows ``(leader, 'LeaderOf', country, valid_from=term
    start)``.
  * **Alliance / bloc memberships** (NATO / EU / BRICS / … — the
    ``member of`` P463 relation) with accession dates → typed SIGNED
    :class:`SeedNexus` rows ``(country, 'MemberOf', bloc, polarity=+1,
    valid_from=accession)``.

Both map DIRECTLY to typed substrate payloads — alliance memberships are
inherently supportive (``polarity=+1``) so no LLM reifier is needed (operator
decision: relational seeds → nexuses directly; the reifier is only for
free-text). Temporal honesty rides the source: each leader carries the start
of their current term, each membership its accession date, so Piece-B
supersession + decay Just Work.

Network: a single guarded (SSRF-checked) GET against the public WDQS endpoint
returning SPARQL JSON results, parsed in ``map``. ``dry_run`` and the offline
test path are served by an injectable ``_sparql_client_factory`` /
``ctx.options['sparql_json']`` fixture so the mapping is unit-testable without
a live endpoint. Degrade-not-drop: a malformed binding is skipped (the driver
already isolates per-record write failures).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from ...sources._egress import guarded_async_client
from .._base import SeedContext, SeedEntity, SeedFact, SeedNexus, SeedPayload

logger = logging.getLogger(__name__)

#: Public Wikidata Query Service SPARQL endpoint.
WDQS_ENDPOINT: str = "https://query.wikidata.org/sparql"

#: Public Wikidata REST (MediaWiki Action API) endpoint — used to resolve the
#: English label of a bare ``Qxxxx`` id the SPARQL label service left unlabelled.
WIKIDATA_API_ENDPOINT: str = "https://www.wikidata.org/w/api.php"

#: ``wbgetentities`` caps a single request at 50 ids; we chunk above that.
_WBGETENTITIES_CHUNK = 50

#: A bare Wikidata entity id (``Q22686``) with NO label — surfaces when the
#: SPARQL ``wikibase:label`` service can't resolve a value (seen on some P6
#: head_of_government statements, e.g. US/Mexico/Serbia).
_BARE_QID_RE = re.compile(r"^Q[0-9]+$")

_LEADER_PREDICATE = "LeaderOf"
# Country-SUBJECT office predicate (subject=country, value=leader). Mirrors
# world_baseline._HEAD_OF_STATE_PREDICATE: this is the supersession-correct
# shape (keyed on the country) so a leader CHANGE closes the prior officeholder
# (valid_until=now + superseded_by) instead of leaving two open "current"
# rows. The `LeaderOf` fact (subject=leader) CANNOT supersede on a leader
# change because supersession keys on the subject, which is the person. Both
# adapters use the SAME canonical predicate so a fresh Wikidata pull supersedes
# a stale world_baseline curated leader for the same country.
_HEAD_OF_STATE_PREDICATE = "head of state"
# Country-SUBJECT head-of-government fact (P6). Kept on its OWN predicate key
# (distinct from head of state / P35) so a constitutional monarchy's PM doesn't
# masquerade as — or supersede — its ceremonial head of state (the monarch).
# DQ-#85.3: collapsing the two under "head of state" mis-typed e.g. Canada's PM
# as its head of state (Charles III is). Each office now supersedes only its own.
_HEAD_OF_GOVERNMENT_PREDICATE = "head of government"
_ALLIANCE_REL = "MemberOf"
_ALLIANCE_POLARITY = 1
_DEFAULT_CONFIDENCE = 0.92  # slightly below curated (0.95): live extraction


# SPARQL — current heads of state/government per country, with the term start
# qualifier (P580) of the CURRENT (no end / open) tenure. ``P35`` = head of
# state, ``P6`` = head of government; we take both and let the country fact
# carry whichever holds. ``wikibase:label`` via the label service.
_LEADERS_SPARQL = """
SELECT ?country ?countryLabel ?leader ?leaderLabel ?role ?start WHERE {
  ?country wdt:P31 wd:Q3624078 .            # sovereign state
  ?country p:P6|p:P35 ?stmt .               # head of govt OR head of state
  ?stmt ps:P6|ps:P35 ?leader .
  ?stmt a wikibase:BestRank .
  OPTIONAL { ?stmt pq:P580 ?start . }
  FILTER NOT EXISTS { ?stmt pq:P582 ?end . }   # no end date = current holder
  BIND( IF(EXISTS { ?stmt ps:P6 ?leader }, "head_of_government", "head_of_state") AS ?role )
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

# SPARQL — country `member of` (P463) an international bloc, with the accession
# (P580) qualifier when present. The driver writes one signed +1 MemberOf nexus
# per (country, bloc) pair.
_ALLIANCES_SPARQL = """
SELECT ?country ?countryLabel ?bloc ?blocLabel ?start WHERE {
  ?country wdt:P31 wd:Q3624078 .            # sovereign state
  ?country p:P463 ?stmt .                   # member of
  ?stmt ps:P463 ?bloc .
  ?stmt a wikibase:BestRank .
  ?bloc wdt:P31/wdt:P279* wd:Q245065 .       # international organization
  OPTIONAL { ?stmt pq:P580 ?start . }
  FILTER NOT EXISTS { ?stmt pq:P582 ?end . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""


def _parse_wikidata_time(raw: str | None) -> datetime | None:
    """Parse a Wikidata time literal (``+2021-01-20T00:00:00Z``) → UTC datetime.

    Wikidata times carry a leading ``+`` sign and can be truncated to year
    precision (``+2021-00-00T00:00:00Z``). We normalise the sign, repair a
    zero month/day (Wikidata's "unknown precision" sentinel) to ``01``, and
    parse. Returns ``None`` on anything unparseable (→ the caller falls back).
    """
    if not raw:
        return None
    s = raw.strip().lstrip("+")
    # Split date / time; repair month/day "00" placeholders to "01".
    date_part, _, _time_part = s.partition("T")
    bits = date_part.split("-")
    if len(bits) == 3:
        year, month, day = bits
        month = "01" if month in ("", "00") else month
        day = "01" if day in ("", "00") else day
        date_part = f"{year}-{month}-{day}"
    try:
        dt = datetime.fromisoformat(date_part)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc)


def _binding(row: dict[str, Any], key: str) -> str | None:
    """Pull the ``value`` of a SPARQL JSON binding (``{"value": …}``) or None."""
    cell = row.get(key)
    if not isinstance(cell, dict):
        return None
    val = cell.get("value")
    return str(val) if val not in (None, "") else None


class WikidataLeadersSeedSource:
    """Wikidata SPARQL seed adapter (implements ``SeedSource``).

    Emits current-head-of-state/government ``LeaderOf`` facts + alliance
    ``MemberOf`` signed nexuses pulled live from the Wikidata Query Service.
    """

    name = "wikidata_leaders"
    source_type = "seed"

    #: Test seam — when set, called instead of opening a real guarded client.
    _sparql_client_factory: Any = None

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        confidence: float = _DEFAULT_CONFIDENCE,
        request_timeout_seconds: float = 90.0,
    ) -> None:
        self._endpoint = endpoint or WDQS_ENDPOINT
        self._confidence = confidence
        self._timeout = request_timeout_seconds

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    async def fetch(self, ctx: SeedContext) -> dict[str, Any]:
        """Run both SPARQL queries → raw ``{"leaders": [...], "alliances": [...]}``.

        Offline / test path: when ``ctx.options['sparql_json']`` is supplied
        (a pre-canned ``{"leaders": <bindings>, "alliances": <bindings>}`` where
        each value is the SPARQL JSON ``results.bindings`` list), no network
        call is made — used by unit tests and by a ``--dry-run`` that wants to
        exercise mapping against a fixture. A real ``dry_run`` with no fixture
        still hits the endpoint (read-only) so the operator sees real counts.
        """
        fixture = (ctx.options or {}).get("sparql_json") if ctx else None
        if fixture is not None:
            leaders = list(fixture.get("leaders") or [])
            alliances = list(fixture.get("alliances") or [])
            # Even on the fixture path, run bare-QID label resolution so a
            # fixture that carries an unlabelled leader (the live-observed
            # US/Mexico/Serbia case) is grounded by name — and so the resolution
            # path is unit-testable end-to-end with a mocked label API.
            await self._resolve_bare_qid_labels(leaders, alliances)
            return {
                "leaders": leaders,
                "alliances": alliances,
                "_source": "fixture",
            }

        # The leaders query is the critical one (the head-of-state/government
        # facts) — let it raise so a partial write never lands. The alliances
        # query is heavy (transitive P279* over every sovereign state) and WDQS
        # 502s/times-out under load far more often; a failure there must NOT
        # abort the leader seed — degrade to no alliances (the existing MemberOf
        # nexuses stay untouched). DQ-#85.3 robustness.
        leaders = await self._query(_LEADERS_SPARQL)
        try:
            alliances = await self._query(_ALLIANCES_SPARQL)
        except Exception as exc:  # noqa: BLE001 — degrade-not-abort the seed
            logger.warning(
                "seed.%s alliances query failed (%s); proceeding with %d "
                "leader binding(s) only",
                self.name, exc, len(leaders),
            )
            alliances = []
        await self._resolve_bare_qid_labels(leaders, alliances)
        return {
            "leaders": leaders,
            "alliances": alliances,
            "_source": self._endpoint,
        }

    async def _query(self, sparql: str) -> list[dict[str, Any]]:
        """Execute one SPARQL query against WDQS → the ``results.bindings`` list."""
        params = {"query": sparql, "format": "json"}
        headers = {
            "Accept": "application/sparql-results+json",
            # WDQS asks every client to identify itself (its UA policy).
            "User-Agent": "legba-seed-wikidata/0.1 (https://legba.invalid)",
        }
        client_cm = (
            self._sparql_client_factory()
            if self._sparql_client_factory is not None
            else guarded_async_client(
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=True,
                headers=headers,
            )
        )
        async with client_cm as client:
            resp = await client.get(self._endpoint, params=params, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        if not isinstance(body, dict):
            raise httpx.HTTPError("WDQS returned a non-object payload")
        bindings = (body.get("results") or {}).get("bindings") or []
        if not isinstance(bindings, list):
            raise httpx.HTTPError("WDQS results.bindings is not a list")
        return bindings

    # ------------------------------------------------------------------
    # bare-QID label resolution (post-SPARQL)
    # ------------------------------------------------------------------

    async def _resolve_bare_qid_labels(
        self,
        leaders: list[dict[str, Any]],
        alliances: list[dict[str, Any]],
    ) -> None:
        """Resolve unlabelled (``Qxxxx``) ``*Label`` cells via wbgetentities.

        The SPARQL ``wikibase:label`` service occasionally returns a BARE QID
        instead of a name (live-observed for some P6 head_of_government rows —
        US ``Q22686``, Mexico, Serbia). A bare QID is useless for grounding (the
        LLM can't read "Q22686") and `map` would DROP it; for the US BOTH the
        head_of_state and head_of_government rows come back unlabelled, so the
        drop would leave the US with no usable office fact.

        We gather every bare-QID ``*Label`` value across leaders + alliances, do
        ONE batched (chunked at 50) ``wbgetentities`` call to fetch English
        labels, and rewrite the binding's ``*Label`` cell IN PLACE with the
        resolved name. A QID the API can't resolve is left as the bare id (so
        `map` still drops it — never emitting a ``Qxxxx`` value). Network
        failure degrades to the same drop (logged, not raised).
        """
        label_keys = (
            ("leaderLabel", "countryLabel"),
            ("countryLabel", "blocLabel"),
        )
        # Collect the bare QIDs needing resolution + remember every cell to patch.
        bare_ids: set[str] = set()
        cells_by_qid: dict[str, list[dict[str, Any]]] = {}
        for rows, keys in zip((leaders, alliances), label_keys):
            for row in rows:
                for key in keys:
                    cell = row.get(key)
                    if not isinstance(cell, dict):
                        continue
                    val = cell.get("value")
                    if isinstance(val, str) and _BARE_QID_RE.match(val):
                        bare_ids.add(val)
                        cells_by_qid.setdefault(val, []).append(cell)
        if not bare_ids:
            return

        try:
            labels = await self._fetch_entity_labels(sorted(bare_ids))
        except Exception as exc:  # noqa: BLE001 — degrade-not-crash on egress fail
            logger.warning(
                "seed.%s wbgetentities label resolution failed (%s); "
                "falling back to dropping %d bare-QID leader(s)",
                self.name,
                exc,
                len(bare_ids),
            )
            return

        resolved = 0
        for qid, cells in cells_by_qid.items():
            name = labels.get(qid)
            if not name:
                continue  # unresolvable → leave bare → map() drops it
            for cell in cells:
                cell["value"] = name
            resolved += 1
        logger.info(
            "seed.%s resolved %d/%d bare-QID label(s) via wbgetentities",
            self.name,
            resolved,
            len(bare_ids),
        )

    async def _fetch_entity_labels(self, ids: list[str]) -> dict[str, str]:
        """Batch-resolve ``{QID: English name}`` via the Wikidata Action API.

        Uses the SAME guarded egress client + user-agent the SPARQL path uses.
        Chunks at 50 ids (the ``wbgetentities`` per-request cap). Prefers
        ``entities[QID].labels.en.value``; FALLS BACK to the English-Wikipedia
        sitelink title (``entities[QID].sitelinks.enwiki.title``) when the en
        label is absent — live-observed for the exact flagged entity (US head of
        government ``Q22686``): its ``labels`` carry dozens of languages but no
        ``en`` key, while the ``enwiki`` sitelink title IS the human-readable
        name ("Donald Trump"). That fallback is what makes the US case actually
        ground. ``props=labels|sitelinks`` + ``sitefilter=enwiki`` keeps the
        payload bounded. Missing/labelless ids are simply absent (the caller
        treats absence as unresolved → the bare-QID drop fallback).
        """
        headers = {
            "Accept": "application/json",
            "User-Agent": "legba-seed-wikidata/0.1 (https://legba.invalid)",
        }
        out: dict[str, str] = {}
        for start in range(0, len(ids), _WBGETENTITIES_CHUNK):
            chunk = ids[start : start + _WBGETENTITIES_CHUNK]
            params = {
                "action": "wbgetentities",
                "ids": "|".join(chunk),
                "props": "labels|sitelinks",
                "languages": "en",
                "sitefilter": "enwiki",
                "format": "json",
            }
            client_cm = (
                self._sparql_client_factory()
                if self._sparql_client_factory is not None
                else guarded_async_client(
                    timeout=httpx.Timeout(self._timeout),
                    follow_redirects=True,
                    headers=headers,
                )
            )
            async with client_cm as client:
                resp = await client.get(
                    WIKIDATA_API_ENDPOINT, params=params, headers=headers
                )
                resp.raise_for_status()
                body = resp.json()
            entities = (body or {}).get("entities") or {}
            if not isinstance(entities, dict):
                continue
            for qid, ent in entities.items():
                if not isinstance(ent, dict):
                    continue
                label = (((ent.get("labels") or {}).get("en") or {})).get("value")
                if not (isinstance(label, str) and label):
                    # No en label → fall back to the enwiki sitelink title.
                    label = (
                        ((ent.get("sitelinks") or {}).get("enwiki") or {})
                    ).get("title")
                if isinstance(label, str) and label:
                    out[qid] = label
        return out

    # ------------------------------------------------------------------
    # map
    # ------------------------------------------------------------------

    def map(self, raw: dict[str, Any]) -> Iterable[SeedPayload]:
        """Map raw SPARQL bindings → typed seed payloads.

        Yields, per source: country/leader/bloc :class:`SeedEntity` enrichment,
        leader :class:`SeedFact` rows, then alliance :class:`SeedNexus` rows.
        Temporal honesty matters: a leader WITHOUT a parseable term-start
        qualifier is SKIPPED (a fact with a fabricated valid_from would poison
        decay/supersession); a membership without an accession date defaults
        valid_from to the observation instant (a membership is current-as-of
        observed, which is true).
        """
        leaders = raw.get("leaders") or []
        alliances = raw.get("alliances") or []
        now = datetime.now(tz=timezone.utc)
        countries: set[str] = set()
        skipped = 0

        # 1) Leaders → LeaderOf facts (subject=leader) + country-subject office
        #    facts (the supersession-correct shape, keyed on the country). The
        #    SPARQL returns BOTH head-of-government (P6) and head-of-state (P35)
        #    rows; DQ-#85.3 keeps them on SEPARATE predicates — `head of state`
        #    (P35) vs `head of government` (P6) — so a parliamentary state's PM
        #    (head of government) is NOT written as its head of state (the
        #    monarch / ceremonial president), and each office supersedes only its
        #    own key on a leader change. One holder per (country, office).
        state_by_country: dict[str, dict[str, Any]] = {}
        gov_by_country: dict[str, dict[str, Any]] = {}
        for row in leaders:
            country = _binding(row, "countryLabel") or _binding(row, "country")
            leader = _binding(row, "leaderLabel") or _binding(row, "leader")
            if not country or not leader:
                skipped += 1
                continue
            # Drop unlabelled entities. They surface either as a URI OR — when
            # the label service can't resolve a value (seen on some P6
            # head_of_government statements, e.g. US/Mexico/Serbia) — as a bare
            # ``Qxxxx`` id. ``fetch`` already ran wbgetentities label resolution
            # (substituting the name for the QID), so a bare QID reaching here is
            # one the Action API ALSO couldn't resolve (truly labelless). A bare
            # QID is useless for grounding (the LLM can't read "Q22686"); this
            # drop is the last-resort fallback so we NEVER emit a ``Qxxxx`` value.
            if (
                leader.startswith("http")
                or country.startswith("http")
                or (leader[:1] == "Q" and leader[1:].isdigit())
                or (country[:1] == "Q" and country[1:].isdigit())
            ):
                skipped += 1
                continue
            valid_from = _parse_wikidata_time(_binding(row, "start"))
            if valid_from is None:
                # No real term-start → would be a fabricated date; skip.
                skipped += 1
                continue
            role = _binding(row, "role") or "head_of_state"
            country = country.strip()
            leader = leader.strip()
            countries.add(country)

            yield SeedEntity(canonical_name=leader, entity_class="person")
            yield SeedEntity(canonical_name=country, entity_class="country")
            yield SeedFact(
                subject=leader,
                predicate=_LEADER_PREDICATE,
                value=country,
                valid_from=valid_from,
                confidence=self._confidence,
                data={
                    "seed_adapter": self.name,
                    "relation": "leader_of",
                    "role": role,
                    "wikidata": {
                        "country_uri": _binding(row, "country"),
                        "leader_uri": _binding(row, "leader"),
                    },
                },
            )
            # Bucket the office-holder by its REAL office (P35 head of state vs
            # P6 head of government), keeping the first seen per (country,
            # office). Each is emitted on its own predicate below so the two
            # never collide / supersede each other.
            bucket = gov_by_country if role == "head_of_government" else state_by_country
            if country not in bucket:
                bucket[country] = {"leader": leader, "valid_from": valid_from}

        # 1b) Country-subject office facts — head of state (P35) and head of
        #     government (P6) on SEPARATE supersession keys (DQ-#85.3 typing fix).
        for country, office in state_by_country.items():
            yield SeedFact(
                subject=country,
                predicate=_HEAD_OF_STATE_PREDICATE,
                value=office["leader"],
                valid_from=office["valid_from"],
                confidence=self._confidence,
                data={
                    "seed_adapter": self.name,
                    "relation": "head_of_state",
                    "role": "head_of_state",
                },
            )
        for country, office in gov_by_country.items():
            yield SeedFact(
                subject=country,
                predicate=_HEAD_OF_GOVERNMENT_PREDICATE,
                value=office["leader"],
                valid_from=office["valid_from"],
                confidence=self._confidence,
                data={
                    "seed_adapter": self.name,
                    "relation": "head_of_government",
                    "role": "head_of_government",
                },
            )

        # 2) Alliances → signed MemberOf nexuses.
        for row in alliances:
            country = _binding(row, "countryLabel") or _binding(row, "country")
            bloc = _binding(row, "blocLabel") or _binding(row, "bloc")
            if not country or not bloc:
                skipped += 1
                continue
            if country.startswith("http") or bloc.startswith("http"):
                skipped += 1
                continue
            country = country.strip()
            bloc = bloc.strip()
            valid_from = _parse_wikidata_time(_binding(row, "start")) or now

            if country not in countries:
                yield SeedEntity(canonical_name=country, entity_class="country")
                countries.add(country)
            yield SeedEntity(canonical_name=bloc, entity_class="organization")
            yield SeedNexus(
                subject=country,
                object=bloc,
                rel_type=_ALLIANCE_REL,
                polarity=_ALLIANCE_POLARITY,
                valid_from=valid_from,
                confidence=self._confidence,
                label=f"{country} {_ALLIANCE_REL} {bloc}",
                intent="alliance",
                channel="institutional",
                data={
                    "seed_adapter": self.name,
                    "bloc": bloc,
                    "wikidata": {
                        "country_uri": _binding(row, "country"),
                        "bloc_uri": _binding(row, "bloc"),
                    },
                },
            )

        if skipped:
            logger.info("seed.%s skipped %d malformed/undated bindings", self.name, skipped)


__all__ = [
    "WDQS_ENDPOINT",
    "WIKIDATA_API_ENDPOINT",
    "WikidataLeadersSeedSource",
]
