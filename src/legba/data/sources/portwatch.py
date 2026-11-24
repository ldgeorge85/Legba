# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``portwatch`` — IMF PortWatch daily chokepoint transit counts, as SERIES.

The first source of the ``observation`` family (N-L). It yields
:class:`~legba.data.sources.records.ObservationRecord` s, never a Signal:
nothing it reads reaches ``signals``, NATS, the corpus or the vector index.

THE API (confirmed 2026-11-13 by three GETs through the egress guard): an
ArcGIS FeatureServer layer, ``Daily_Chokepoints_Data/FeatureServer/0``,
no key. ``/query`` with ``where``, ``outFields``, ``orderByFields``,
``resultOffset`` / ``resultRecordCount`` (``maxRecordCount`` 1000,
``advancedQueryCapabilities.supportsPagination``) and ``f=json``. A page
carries ``exceededTransferLimit: true`` when more rows follow. One row per
(``date``, ``portid``): ``date`` is an ``esriFieldTypeDateOnly`` returned as
``YYYY-MM-DD``; counts ``n_total``, ``n_tanker``, ``n_container``,
``n_dry_bulk``, ``n_general_cargo``, ``n_roro``, ``n_cargo`` (integers) and
``capacity_*``. 28 chokepoints. A ``DATE 'YYYY-MM-DD'`` literal filters the
date. Responses carry ETag and Last-Modified.

THE LAG AND REVISIONS. On 2026-11-13 the newest row was 2026-11-08 and the
layer's Last-Modified was 2026-11-10: rows land in weekly-ish batches about
four to five days behind. So every poll re-reads a TRAILING WINDOW —
``lag_days + revision_days`` back from today (default 4 + 10) — and the
ObservationSink writes only what changed: an unchanged day costs nothing, a
late-arriving day is a new row, a revised day is a new vintage. The FIRST poll
reads ``backfill_days`` (default 45, at most 90); ``backfill_from`` asks once
for an explicit older start (consumed and cleared after a complete walk).

VOLUME. This handler is deliberately not a bulk downloader: a bounded
trailing window (~400 rows) once a day, at most ``max_pages`` pages per poll,
backfill capped at 90 days. The descriptor ships ``draft``.

LINKING. Each page's body is hashed here (sha256 of the decoded body — the
same bytes the fetch receipt hashes) and stamped on every record parsed from
it, so a record from page 2 never points at page 1.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, ClassVar, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ._contract import SourceContext, SourceHealth
from ._egress import guarded_async_client
from .records import ObservationRecord

__all__ = ["PortWatchConfig", "PortWatchSourceHandler", "COUNT_FIELDS"]

DEFAULT_SERVICE_URL = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
    "Daily_Chokepoints_Data/FeatureServer/0"
)
COUNT_FIELDS: tuple[str, ...] = (
    "n_total", "n_tanker", "n_container", "n_dry_bulk", "n_general_cargo",
    "n_roro", "n_cargo",
)
_HEALTH_KEY = "portwatch_health"
_CURSOR_KEY = "portwatch_cursor"
_MAX_PAGE = 1000   # the layer's maxRecordCount


class PortWatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_url: str = DEFAULT_SERVICE_URL
    fields: list[str] = Field(default_factory=lambda: list(COUNT_FIELDS), min_length=1)
    portids: list[str] = Field(default_factory=list)      # empty = every chokepoint
    series_prefix: str = "imf_portwatch.chokepoint"
    methodology_version: str = "portwatch-daily-chokepoints/1"
    # N-R: each row's subject is ONE chokepoint (subject = the provider's
    # ``portid``, the name in provenance.portname) — a named non-country
    # place, i.e. ``entity`` in the closed observations vocabulary, not
    # ``global`` (one world-level figure). The descriptor declares ``entity``
    # here AND on every series (the sink rejects a mismatch). The default stays
    # the pre-lane ``global`` so a code roll that lands before the new
    # descriptor head cannot turn a day's rows into subject_kind rejections.
    subject_kind: Literal["country", "entity", "target", "global"] = "global"
    lag_days: int = Field(default=4, ge=0, le=30)
    revision_days: int = Field(default=10, ge=1, le=60)
    backfill_days: int = Field(default=45, ge=1, le=90)
    backfill_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    page_size: int = Field(default=_MAX_PAGE, ge=50, le=_MAX_PAGE)
    max_pages: int = Field(default=10, ge=1, le=20)
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    user_agent: str = "legba-source-portwatch/0.1"
    # The actor reads this (descriptor > handler > default). Observation
    # records are bounded by the actor's per-poll sink-record ceiling, not by
    # the document entry cap.
    poll_budget_seconds: float = 120.0


def _unit_of(field: str) -> str:
    return "vessels" if field.startswith("n_") else "deadweight_tonnes"


def window_start(today: date, cfg: PortWatchConfig, *, first_poll: bool) -> date:
    """The earliest ``date`` this poll asks for (pure, tested)."""
    if cfg.backfill_from:
        return date.fromisoformat(cfg.backfill_from)
    if first_poll:
        return today - timedelta(days=cfg.backfill_days)
    return today - timedelta(days=cfg.lag_days + cfg.revision_days)


def build_where(start: date, portids: list[str]) -> str:
    where = f"date >= DATE '{start.isoformat()}'"
    if portids:
        quoted = ",".join("'" + p.replace("'", "''") + "'" for p in portids)
        where += f" AND portid IN ({quoted})"
    return where


def parse_page(body: bytes, *, cfg: PortWatchConfig, page_url: str) -> tuple[list[ObservationRecord], bool, str]:
    """``(records, more_pages, sha256)`` for one ``/query`` page body."""
    sha = hashlib.sha256(body).hexdigest()
    doc = json.loads(body)
    if not isinstance(doc, dict) or "error" in doc:
        raise ValueError(f"portwatch query error: {str(doc.get('error') if isinstance(doc, dict) else doc)[:300]}")
    out: list[ObservationRecord] = []
    for feat in doc.get("features") or []:
        attrs = (feat or {}).get("attributes") or {}
        raw_date, portid = attrs.get("date"), attrs.get("portid")
        if not isinstance(raw_date, str) or not isinstance(portid, str) or not portid:
            continue
        day = date.fromisoformat(raw_date[:10])
        for field in cfg.fields:
            value = attrs.get(field)
            if value is None or isinstance(value, bool):
                continue   # absence is a missing row, never a zero
            out.append(ObservationRecord(
                series_id=f"{cfg.series_prefix}.{field}",
                subject_kind=cfg.subject_kind, subject=portid,
                valid_from=day, valid_to=day, period="day",
                value=Decimal(str(value)), unit=_unit_of(field),
                methodology_version=cfg.methodology_version,
                source_url=page_url, fetch_sha256=sha,
                provenance={"portname": attrs.get("portname"),
                            "object_id": attrs.get("ObjectId"), "field": field},
            ))
    return out, bool(doc.get("exceededTransferLimit")), sha


class PortWatchSourceHandler:
    kind: ClassVar[str] = "portwatch"
    schema_version: ClassVar[str] = "legba/source.portwatch/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = PortWatchConfig
    health_state_key: ClassVar[str] = _HEALTH_KEY

    def __init__(self, config: PortWatchConfig, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = http_client
        self.poll_budget_seconds = config.poll_budget_seconds

    def _open(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return guarded_async_client(
            timeout=self._config.timeout_seconds, follow_redirects=True,
            headers={"User-Agent": self._config.user_agent},
        )

    async def pull(self, ctx: SourceContext, since: datetime | None = None) -> AsyncIterator[ObservationRecord]:
        cfg = self._config
        cursor = await ctx.state_store.get(_CURSOR_KEY)
        first_poll = not isinstance(cursor, dict)
        if isinstance(cursor, dict) and cursor.get("backfill_done") == cfg.backfill_from:
            cfg = cfg.model_copy(update={"backfill_from": None})
        today = ctx.utcnow().date()
        start = window_start(today, cfg, first_poll=first_poll)
        query = cfg.service_url.rstrip("/") + "/query"
        params = {
            "where": build_where(start, cfg.portids),
            "outFields": ",".join(["date", "portid", "portname", "ObjectId", *cfg.fields]),
            "orderByFields": "date ASC,portid ASC", "returnGeometry": "false",
            "resultRecordCount": str(cfg.page_size), "f": "json",
        }
        client = self._open()
        owns = client is not self._client
        newest: date | None = None
        pages = rows = 0
        complete = False
        try:
            for page in range(cfg.max_pages):
                params["resultOffset"] = str(page * cfg.page_size)
                resp = await client.get(query, params=params)
                if resp.status_code != 200:
                    await self._health(ctx, "unhealthy", error=f"HTTP {resp.status_code}")
                    return
                records, more, _sha = parse_page(resp.content, cfg=cfg, page_url=str(resp.url))
                pages += 1
                for rec in records:
                    rows += 1
                    newest = rec.valid_from if newest is None or rec.valid_from > newest else newest
                    yield rec
                if not more:
                    complete = True
                    break
        except (httpx.HTTPError, ValueError) as exc:
            await self._health(ctx, "degraded", error=f"{type(exc).__name__}: {exc}"[:500])
            return
        finally:
            if owns:
                await client.aclose()
        state = {"last_window_start": start.isoformat(), "complete": complete}
        if complete and cfg.backfill_from:
            state["backfill_done"] = cfg.backfill_from
        elif isinstance(cursor, dict) and cursor.get("backfill_done"):
            state["backfill_done"] = cursor["backfill_done"]
        await ctx.state_store.set(_CURSOR_KEY, state)
        newest_ts = None if newest is None else datetime.combine(newest, datetime.min.time(), timezone.utc).isoformat()
        await self._health(
            ctx, "healthy" if complete else "degraded",
            error=None if complete else f"truncated at max_pages={cfg.max_pages}",
            detail={"pages": pages, "records": rows, "window_start": start.isoformat(),
                    "newest_entry_ts": newest_ts},
        )

    async def _health(self, ctx: SourceContext, state: str, *, error: str | None = None,
                      detail: dict[str, Any] | None = None) -> None:
        await ctx.state_store.set(_HEALTH_KEY, {
            "state": state, "last_error": error, "detail": detail or {},
            "last_success_at": ctx.utcnow().isoformat() if state == "healthy" else None,
        })

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        rec = await ctx.state_store.get(_HEALTH_KEY)
        if not isinstance(rec, dict):
            return SourceHealth(state="healthy", detail={"note": "never polled"})
        return SourceHealth(state=str(rec.get("state") or "healthy"),
                            last_error=rec.get("last_error"), detail=rec.get("detail") or {})
