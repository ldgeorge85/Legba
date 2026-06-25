# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""acled_conflict — an ACLED conflict-events seed adapter (flavor b, tier 1).

ACLED (Armed Conflict Location & Event Data) is a free / non-commercial
dataset of political-violence, conflict, and protest events for 230+
countries. The runtime already has a *streaming* ACLED **source** handler
(``data/sources/acled.py``) that emits one ``Signal`` per record into the live
ingest path; THIS is a different thing — a **seed** adapter that pulls a bulk
historical slice ONCE and folds it into the knowledge layer as:

  * one :class:`SeedFact` per event — ``(actor1, 'InvolvedInConflictEvent',
    country, valid_from=event_date)`` — so the substrate carries the conflict
    history (who was active where, when), geo-stamped from the event coords;
  * one typed SIGNED :class:`SeedNexus` per two-actor event —
    ``(actor1, 'HostileTo', actor2, polarity=-1, valid_from=event_date)`` —
    feeding structural-balance / graph-mining the antagonistic edges directly,
    no LLM reifier (operator decision: relational seeds map to nexuses
    directly; the reifier is only for free-text).

``source_type='backfill'`` (this is bulk historical, not curated-authoritative
like the leaders set). The driver stamps it + the seed_batch_id; idempotency
rides the open-only temporal-triple uniqueness, so re-running the same window
upserts (no duplicate open triples).

Auth: ACLED requires a per-call ``key`` + ``email`` (rate-limit attribution).
The seed CLI supplies them via ``ctx.options['api_key']`` / ``['email']`` (a
seed import is an operator-run one-shot, not a registered descriptor, so there
is no SecretRef vault to resolve here — the operator passes the key on the
command line / env). Network egress is SSRF-guarded. The offline/test path is
served by ``ctx.options['records']`` (a pre-canned record list), so the
mapping is unit-testable without a live key.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from ...sources._egress import guarded_async_client
from .._base import SeedContext, SeedEntity, SeedFact, SeedNexus, SeedPayload

logger = logging.getLogger(__name__)

#: Public ACLED REST read endpoint (shared with the streaming source handler).
ACLED_API_BASE: str = "https://api.acleddata.com/acled/read"
ACLED_PAGE_SIZE_MAX: int = 5000

_EVENT_PREDICATE = "InvolvedInConflictEvent"
_HOSTILE_REL = "HostileTo"
_HOSTILE_POLARITY = -1
_DEFAULT_CONFIDENCE = 0.85  # bulk extraction; below curated/structured seeds


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_event_date(raw: Any) -> datetime | None:
    s = (str(raw) if raw is not None else "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class ACLEDConflictSeedSource:
    """ACLED conflict-events seed adapter (implements ``SeedSource``).

    Pulls a bounded historical slice (``country`` / ``event_date>=since`` /
    ``limit``) and maps it to conflict-event facts + signed ``HostileTo``
    nexuses between the two named actors of each event.
    """

    name = "acled_conflict"
    source_type = "backfill"

    #: Test seam — when set, called instead of opening a real guarded client.
    _http_client_factory: Any = None

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        confidence: float = _DEFAULT_CONFIDENCE,
        request_timeout_seconds: float = 90.0,
        max_records: int = ACLED_PAGE_SIZE_MAX,
    ) -> None:
        self._endpoint = endpoint or ACLED_API_BASE
        self._confidence = confidence
        self._timeout = request_timeout_seconds
        self._max_records = min(int(max_records), ACLED_PAGE_SIZE_MAX)

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    async def fetch(self, ctx: SeedContext) -> list[dict[str, Any]]:
        """Pull a bounded ACLED slice → the raw record list.

        Offline / test path: ``ctx.options['records']`` (a pre-canned list of
        ACLED record dicts) short-circuits the network call. Otherwise an
        ``api_key`` + ``email`` are required in ``ctx.options`` (operator
        supplies them; a seed import has no descriptor/SecretRef). Optional
        ``country`` (ISO-3) / ``since`` (ISO date) / ``limit`` narrow the slice.
        """
        opts = (ctx.options or {}) if ctx else {}
        records = opts.get("records")
        if records is not None:
            return [r for r in records if isinstance(r, dict)]

        api_key = opts.get("api_key")
        email = opts.get("email")
        if not api_key or not email:
            raise ValueError(
                "acled_conflict seed requires options 'api_key' and 'email' "
                "(or a 'records' fixture); ACLED rejects unidentified calls"
            )

        params: dict[str, Any] = {
            "key": api_key,
            "email": email,
            "limit": min(int(opts.get("limit", self._max_records)), self._max_records),
        }
        country = opts.get("country")
        if country:
            params["iso3"] = str(country)
        since = opts.get("since")
        if since:
            params["event_date"] = str(since)
            params["event_date_where"] = ">="

        headers = {"User-Agent": "legba-seed-acled/0.1 (https://legba.invalid)"}
        client_cm = (
            self._http_client_factory()
            if self._http_client_factory is not None
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
            raise httpx.HTTPError("ACLED returned a non-object payload")
        if body.get("success") is False:
            err = body.get("error") or body.get("message") or "unknown ACLED error"
            raise httpx.HTTPError(f"ACLED API error: {err!r}")
        data = body.get("data") or []
        if not isinstance(data, list):
            raise httpx.HTTPError("ACLED body.data is not a list")
        return [r for r in data if isinstance(r, dict)]

    # ------------------------------------------------------------------
    # map
    # ------------------------------------------------------------------

    def map(self, raw: list[dict[str, Any]]) -> Iterable[SeedPayload]:
        """Map ACLED records → conflict-event facts + signed HostileTo nexuses.

        Per record:
          * if a parseable ``event_date`` + a named ``actor1`` + ``country``
            exist → a ``InvolvedInConflictEvent`` fact (geo-stamped);
          * if a second distinct named actor (``actor2``) exists → a signed
            ``-1 HostileTo`` nexus between the two actors.
        Records without a usable ``event_date`` (a fabricated valid_from would
        poison decay/supersession) or without a named actor1 are skipped.
        """
        seen_actors: set[str] = set()
        skipped = 0

        for rec in raw:
            event_date = _parse_event_date(rec.get("event_date"))
            actor1 = (rec.get("actor1") or "").strip()
            actor2 = (rec.get("actor2") or "").strip()
            country = (rec.get("country") or "").strip()
            if event_date is None or not actor1:
                skipped += 1
                continue

            lat = _to_float(rec.get("latitude"))
            lon = _to_float(rec.get("longitude"))
            event_type = (rec.get("event_type") or "").strip()
            data_id = str(rec.get("data_id") or rec.get("event_id_cnty") or "")

            if actor1.lower() not in seen_actors:
                yield SeedEntity(canonical_name=actor1, entity_class="organization")
                seen_actors.add(actor1.lower())

            # 1) Conflict-event fact: actor1 was involved in an event in country.
            if country:
                yield SeedFact(
                    subject=actor1,
                    predicate=_EVENT_PREDICATE,
                    value=country,
                    valid_from=event_date,
                    confidence=self._confidence,
                    geo_lat=lat,
                    geo_lon=lon,
                    data={
                        "seed_adapter": self.name,
                        "event_type": event_type,
                        "sub_event_type": (rec.get("sub_event_type") or "").strip(),
                        "fatalities": rec.get("fatalities"),
                        "location": (rec.get("location") or "").strip(),
                        "iso3": (rec.get("iso3") or rec.get("iso") or ""),
                        "acled_id": data_id,
                    },
                )

            # 2) Signed HostileTo nexus between the two named actors.
            if actor2 and actor2.lower() != actor1.lower():
                if actor2.lower() not in seen_actors:
                    yield SeedEntity(
                        canonical_name=actor2, entity_class="organization"
                    )
                    seen_actors.add(actor2.lower())
                yield SeedNexus(
                    subject=actor1,
                    object=actor2,
                    rel_type=_HOSTILE_REL,
                    polarity=_HOSTILE_POLARITY,
                    valid_from=event_date,
                    confidence=self._confidence,
                    label=f"{actor1} {_HOSTILE_REL} {actor2}",
                    intent="hostile",
                    channel="direct",
                    data={
                        "seed_adapter": self.name,
                        "event_type": event_type,
                        "country": country,
                        "acled_id": data_id,
                    },
                )

        if skipped:
            logger.info(
                "seed.%s skipped %d records (no event_date / no actor1)",
                self.name,
                skipped,
            )


__all__ = [
    "ACLED_API_BASE",
    "ACLED_PAGE_SIZE_MAX",
    "ACLEDConflictSeedSource",
]
