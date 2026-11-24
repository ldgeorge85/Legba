# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``series_api`` — a generic numeric-series source, declared in config (S-1).

The second kind of the ``observation`` family and the first GENERIC one: a
public endpoint that returns a time series as JSON or CSV rows is added by a
descriptor, not by code. The config (:class:`~legba.data.schemas.source_series_api.SeriesApiConfig`)
says where the rows are, which field is the date and how it is written, which
field(s) are values, which field or constant is the subject, how to ask for
rows from a date on, how to page, and how far back to re-read. There is no
provider-specific code here; PortWatch keeps its own kind.

It yields :class:`~legba.data.sources.records.ObservationRecord` s only —
nothing reaches ``signals``, NATS, the corpus or the vector index. The
observation sink validates each record against the source's series registry
and writes only a change (an unchanged re-read writes nothing; a revised value
is one new vintage).

THE WINDOW. Every poll asks from ``today - (lag_days + revision_days)``; the
first poll from ``today - backfill_days``; ``backfill_from`` asks once for an
explicit older start (consumed after a complete walk). A late value or a
revision inside the window is re-read and caught; a row older than the
window is never re-asked (that is what ``backfill_from`` is for).

KEYS. A keyed endpoint names a vault SecretRef (``auth.secret_ref``), resolved
per poll through ``ctx.secrets_resolve`` (or the factory-threaded resolver);
with neither wired the handler REFUSES (no-stubs rule: a keyed source never
polls unauthenticated). The resolved key never reaches a record, a receipt URL
or the ``httpx`` request log: the request runs inside
:func:`~legba.data.sources.fetch_capture.redact_secrets`.

I-5 (2026-11-14). Top-level and indexed row paths (``$``, ``1``), positional
rows (``row_columns``), a JSON-stat 2.0 reader (:mod:`.jsonstat`), three
day-first date formats matched exactly, a since parameter rendered in the
endpoint's own format inside an optional template — and the window test reads
a row's period END: a month, quarter or year that ends inside the window is
re-read (its start may precede the window). For a ``day`` period start and
end are the same day, so every daily source reads exactly as before.

LINKING. Each page's decoded body is hashed here and stamped on every record
parsed from it (the same bytes the fetch receipt hashes).
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, ClassVar, Iterable, Mapping

import httpx
from pydantic import BaseModel

from ..schemas.source_series_api import SeriesApiConfig, SeriesApiOutput
from ._contract import SourceContext, SourceHealth
from ._egress import guarded_async_client
from .fetch_capture import redact_secrets, redacted
from .jsonstat import cube_rows
from .records import ObservationRecord

__all__ = [
    "SeriesApiAuthNotConfigured",
    "SeriesApiSourceHandler",
    "format_since",
    "parse_date",
    "parse_rows",
    "period_end",
    "records_from_rows",
    "window_start",
]

_HEALTH_KEY = "series_api_health"
_CURSOR_KEY = "series_api_cursor"


class SeriesApiAuthNotConfigured(RuntimeError):
    """A keyed series source with no secrets resolver wired (fail loud)."""


# ---------------------------------------------------------------------------
# Pure parsing (tested without a network)
# ---------------------------------------------------------------------------


def window_start(today: date, cfg: SeriesApiConfig, *, first_poll: bool) -> date:
    if cfg.backfill_from:
        return date.fromisoformat(cfg.backfill_from)
    if first_poll:
        return today - timedelta(days=cfg.backfill_days)
    return today - timedelta(days=cfg.lag_days + cfg.revision_days)


#: English month abbreviations — fixed here, never the process locale.
_MONTHS: tuple[str, ...] = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_NO = {m: i for i, m in enumerate(_MONTHS, start=1)}
_DMY_DOTTED = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
_DMY_SLASHED = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_DAY_MON_YEAR = re.compile(r"^(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})$")
_YMD_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def format_since(day: date, fmt: str, template: str | None = None) -> str:
    """``day`` rendered the way the endpoint asks for it (``SINCE_FORMATS``),
    placed in ``template``'s ``{date}`` slot when one is declared."""
    if fmt == "year_month":
        text = f"{day.year:04d}-{day.month:02d}"
    elif fmt == "year":
        text = f"{day.year:04d}"
    elif fmt == "dmy_dotted":
        text = f"{day.day:02d}.{day.month:02d}.{day.year:04d}"
    elif fmt == "dmy_slashed":
        text = f"{day.day:02d}/{day.month:02d}/{day.year:04d}"
    elif fmt == "day_mon_year":
        text = f"{day.day:02d} {_MONTHS[day.month - 1]} {day.year:04d}"
    elif fmt == "day_mon_year_slashed":
        text = f"{day.day:02d}/{_MONTHS[day.month - 1]}/{day.year:04d}"
    else:
        text = day.isoformat()
    return text if template is None else template.replace("{date}", text)


def parse_date(raw: Any, fmt: str) -> date | None:
    """The period START a row's date names, or ``None`` when unreadable."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        if fmt in ("epoch_ms", "epoch_s"):
            secs = float(raw) / (1000.0 if fmt == "epoch_ms" else 1.0)
            return datetime.fromtimestamp(secs, tz=timezone.utc).date()
        s = str(raw).strip()
        if fmt == "iso_date":
            return date.fromisoformat(s[:10])
        if fmt == "iso_datetime":
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()
        if fmt == "year_month":
            y, m = s[:7].split("-")
            return date(int(y), int(m), 1)
        if fmt == "year":
            return date(int(s[:4]), 1, 1)
        if fmt == "year_quarter":
            y, q = s.upper().replace(" ", "").split("-Q")
            return date(int(y), 3 * (int(q) - 1) + 1, 1)
        if fmt in ("dmy_dotted", "dmy_slashed"):
            m = (_DMY_DOTTED if fmt == "dmy_dotted" else _DMY_SLASHED).match(s)
            return None if m is None else date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if fmt == "ymd_compact":
            m = _YMD_COMPACT.match(s)
            return None if m is None else date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if fmt == "day_mon_year":
            m = _DAY_MON_YEAR.match(s)
            if m is None or m.group(2) not in _MONTH_NO:
                return None
            return date(int(m.group(3)), _MONTH_NO[m.group(2)], int(m.group(1)))
    except (ValueError, TypeError, OverflowError):
        return None
    return None


def period_end(start: date, period: str) -> date:
    if period == "week":
        return start + timedelta(days=6)
    if period == "month":
        return date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    if period == "quarter":
        last = start.month + 2
        return date(start.year, last, calendar.monthrange(start.year, last)[1])
    if period == "year":
        return date(start.year, 12, 31)
    return start


def _get(row: Mapping[str, Any], path: str) -> Any:
    """A field of a row: the exact key first (CSV headers and JSON keys may
    contain dots or brackets), then a dotted walk."""
    if path in row:
        return row[path]
    cur: Any = row
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _error_hint(doc: Any) -> Any:
    """An error document's message: ``{"error": ...}``, or the World Bank's
    ``[{"message": [...]}]``."""
    if isinstance(doc, Mapping):
        return doc.get("error")
    if isinstance(doc, list) and doc and isinstance(doc[0], Mapping):
        return doc[0].get("message") or doc[0].get("error")
    return None


def parse_rows(body: bytes, cfg: SeriesApiConfig) -> list[Mapping[str, Any]]:
    """The rows of one response body. Raises ``ValueError`` on a body that is
    not the declared shape (an error document, a missing row path)."""
    if cfg.format == "csv":
        text = body.decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text), delimiter=cfg.csv_delimiter))
    doc = json.loads(body)
    if cfg.format == "jsonstat":
        return cube_rows(doc, max_cells=cfg.jsonstat_max_cells)
    cur: Any = doc
    parts = [] if cfg.rows_path == "$" else cfg.rows_path.split(".")
    for part in parts:
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]              # an array meets an index: [meta, rows] -> "1"
        else:
            hint = _error_hint(doc)
            raise ValueError(f"rows_path {cfg.rows_path!r} not in the response"
                             + (f" (error: {str(hint)[:200]})" if hint else ""))
    if not isinstance(cur, list):
        raise ValueError(f"rows_path {cfg.rows_path!r} is not an array")
    if cfg.row_columns:
        width = len(cfg.row_columns)
        rows: list[Mapping[str, Any]] = []
        for r in cur:
            if not isinstance(r, list) or len(r) != width:
                raise ValueError(f"row_columns declares {width} cells; a row is "
                                 f"{type(r).__name__} of {len(r) if isinstance(r, list) else '-'}")
            rows.append(dict(zip(cfg.row_columns, r)))
        return rows
    return [r for r in cur if isinstance(r, Mapping)]


def _value(raw: Any, missing: Iterable[str]) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str) and raw.strip() in missing:
        return None
    try:
        val = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return val if val.is_finite() else None


def _matches(row: Mapping[str, Any], out: SeriesApiOutput) -> bool:
    for w in out.where:
        cell = _get(row, w.field)
        hit = ("" if cell is None else str(cell).strip()) in w.values
        if hit == w.negate:
            return False
    return True


def records_from_rows(
    rows: Iterable[Mapping[str, Any]], cfg: SeriesApiConfig, *, page_url: str, sha256: str,
    since: date | None = None,
) -> tuple[list[ObservationRecord], int]:
    """``(records, skipped)``: one record per (row, matching output) with a
    readable date and a finite numeric value. ``skipped`` counts the rows an
    output matched but could not read (absent value, bad date, no subject) —
    absence is never written as zero."""
    missing = set(cfg.missing_values)
    out: list[ObservationRecord] = []
    skipped = 0
    for row in rows:
        start = parse_date(_get(row, cfg.date_field), cfg.date_format)
        for spec in cfg.outputs:
            if not _matches(row, spec):
                continue
            value = _value(_get(row, spec.value_field), missing)
            subject = spec.subject or str(_get(row, spec.subject_field or "") or "").strip()
            if start is None or value is None or not subject:
                skipped += 1
                continue
            if since is not None and period_end(start, spec.period) < since:
                continue   # the provider returned more than asked: keep the window honest
            prov = {f: _get(row, f) for f in cfg.provenance_fields if _get(row, f) is not None}
            out.append(ObservationRecord(
                series_id=spec.series, subject_kind=spec.subject_kind, subject=subject[:256],
                valid_from=start, valid_to=period_end(start, spec.period), period=spec.period,
                value=value, unit=spec.unit, methodology_version=spec.methodology_version,
                source_url=page_url[:4096], fetch_sha256=sha256,
                provenance={**prov, "value_field": spec.value_field},
            ))
    return out, skipped


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


class SeriesApiSourceHandler:
    kind: ClassVar[str] = "series_api"
    schema_version: ClassVar[str] = "legba/source.series_api/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = SeriesApiConfig
    health_state_key: ClassVar[str] = _HEALTH_KEY

    def __init__(self, config: SeriesApiConfig, *, http_client: httpx.AsyncClient | None = None,
                 secret_resolver: Any = None) -> None:
        self._config = config
        self._client = http_client
        self._secret_resolver = secret_resolver
        self.poll_budget_seconds = config.poll_budget_seconds

    def _open(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return guarded_async_client(
            timeout=self._config.timeout_seconds, follow_redirects=True,
            headers={"User-Agent": self._config.user_agent},
        )

    async def _secret(self, ctx: SourceContext) -> str | None:
        auth = self._config.auth
        if auth is None:
            return None
        resolver = getattr(ctx, "secrets_resolve", None) or self._secret_resolver
        if resolver is None:
            raise SeriesApiAuthNotConfigured(
                f"series_api source declares auth secret_ref={auth.secret_ref!r} but no secrets "
                "resolver is wired — refusing to poll a keyed endpoint unauthenticated")
        raw = await resolver(auth.secret_ref)
        secret = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        if not secret.strip():
            raise SeriesApiAuthNotConfigured(f"secret {auth.secret_ref!r} resolved empty")
        return secret.strip()

    def _request(self, start: date, page: int, secret: str | None) -> tuple[list[tuple[str, str]], dict[str, str]]:
        cfg = self._config
        params = [(k, v) for k, v in cfg.params]
        headers: dict[str, str] = {}
        if cfg.since_param is not None:
            params.append((cfg.since_param.name,
                           format_since(start, cfg.since_param.format, cfg.since_param.template)))
        if cfg.pagination.mode == "offset":
            params += [(cfg.pagination.offset_param, str(page * cfg.pagination.page_size)),
                       (cfg.pagination.limit_param, str(cfg.pagination.page_size))]
        if secret is not None and cfg.auth is not None:
            value = cfg.auth.value_template.replace("{secret}", secret)
            if cfg.auth.mode == "query":
                params.append((cfg.auth.name, value))
            else:
                headers[cfg.auth.name] = value
        return params, headers

    async def pull(self, ctx: SourceContext, since: datetime | None = None) -> AsyncIterator[ObservationRecord]:
        cfg = self._config
        cursor = await ctx.state_store.get(_CURSOR_KEY)
        first_poll = not isinstance(cursor, dict)
        if isinstance(cursor, dict) and cursor.get("backfill_done") == cfg.backfill_from:
            cfg = cfg.model_copy(update={"backfill_from": None})
        today = ctx.utcnow().date()
        start = window_start(today, cfg, first_poll=first_poll)
        try:
            secret = await self._secret(ctx)
        except SeriesApiAuthNotConfigured as exc:
            await self._health(ctx, "unhealthy", error=str(exc))
            raise
        client = self._open()
        owns = client is not self._client
        newest: date | None = None
        pages = rows_seen = records = skipped = 0
        complete = False
        max_pages = cfg.pagination.max_pages if cfg.pagination.mode == "offset" else 1
        try:
            for page in range(max_pages):
                params, headers = self._request(start, page, secret)
                with redact_secrets(*([secret] if secret else [])):
                    # I-5: no parameters at all = the URL verbatim (httpx replaces a
                    # URL's own query whenever params are passed, even an empty
                    # list, and renders a bare flag like NBU's `?json` as `json=`).
                    resp = await client.get(cfg.url, params=params or None, headers=headers)
                    page_url = redacted(str(resp.url))
                if resp.status_code != 200:
                    await self._health(ctx, "unhealthy", error=f"HTTP {resp.status_code}")
                    return
                body = resp.content
                sha = hashlib.sha256(body).hexdigest()
                rows = parse_rows(body, cfg)
                pages += 1
                rows_seen += len(rows)
                recs, skip = records_from_rows(rows, cfg, page_url=page_url, sha256=sha, since=start)
                skipped += skip
                for rec in recs:
                    records += 1
                    newest = rec.valid_from if newest is None or rec.valid_from > newest else newest
                    yield rec
                if cfg.pagination.mode == "none" or len(rows) < cfg.pagination.page_size:
                    complete = True
                    break
        except (httpx.HTTPError, ValueError, UnicodeDecodeError, csv.Error) as exc:
            await self._health(ctx, "degraded", error=redacted(f"{type(exc).__name__}: {exc}")[:500])
            return
        finally:
            if owns:
                await client.aclose()
        state: dict[str, Any] = {"last_window_start": start.isoformat(), "complete": complete}
        if complete and cfg.backfill_from:
            state["backfill_done"] = cfg.backfill_from
        elif isinstance(cursor, dict) and cursor.get("backfill_done"):
            state["backfill_done"] = cursor["backfill_done"]
        await ctx.state_store.set(_CURSOR_KEY, state)
        newest_ts = None if newest is None else datetime.combine(newest, datetime.min.time(), timezone.utc).isoformat()
        await self._health(
            ctx, "healthy" if complete else "degraded",
            error=None if complete else f"truncated at max_pages={max_pages}",
            detail={"pages": pages, "rows": rows_seen, "records": records, "skipped": skipped,
                    "window_start": start.isoformat(), "newest_entry_ts": newest_ts},
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
