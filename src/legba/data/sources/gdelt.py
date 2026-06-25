# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GDELT 2.0 BigQuery source handler (L-131).

GDELT (Global Database of Events, Language, and Tone) is the highest-leverage
source the platform pulls from:

  * Free. No license fee, no API key, no per-request cost beyond Google
    BigQuery scan billing.
  * 100+ languages — GDELT translates news in real time.
  * 15-minute refresh — new events appear in
    ``gdelt-bq.gdeltv2.events`` within ~15 minutes of publication.
  * CAMEO/FIPS coding — actors, event types, geography all normalized to
    well-known taxonomies. See
    http://data.gdeltproject.org/documentation/CAMEO.Manual.1.1b3.pdf
    for the event-type root codes (e.g. ``14`` = protest, ``18`` = assault,
    ``19`` = fight, ``20`` = mass violence).
  * Country codes on the events table are **FIPS 10-4** three-letter codes
    (Brazil = ``BR``, USA = ``US``) — *not* ISO 3166. The descriptor schema
    keeps the FIPS naming for fidelity to the source; runtime taxonomy
    crosswalks happen downstream in the enrichment pipeline (L-153).

Cost discipline matters here because BigQuery bills on bytes scanned. The
handler:

  1. Always partitions the query on ``SQLDATE`` (the event date) — that
     pushes the scan boundary down into the BigQuery storage layer.
  2. Runs a dry-run before any real query and refuses to execute if the
     estimated bytes-scanned exceeds the configured cap.
  3. Reports cost (``estimated_bytes`` and ``total_bytes_processed``) on the
     health probe so operators can see per-pull billing impact.

The handler follows the L-102 §2 source-kind contract. The
``google-cloud-bigquery`` package is an *optional* dependency — the handler
imports it lazily and gracefully degrades to an "unhealthy" health status
with a clear error message when the package is missing. Tests inject a fake
client to exercise SQL composition, filter translation, and cost-estimation
logic without ever touching BigQuery.

Credential resolution. The service-account JSON lives in the credentials
vault keyed by ``bq_credentials_secret`` (a dotted vault id, e.g.
``creds.gdelt.bigquery_sa``). The handler accepts a ``credential_resolver``
callable on construction so the runtime can wire in the L-111 vault without
the handler needing to import it directly; this also keeps the unit tests
free of vault setup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._contract import Signal, SourceContext, SourceHealth


logger = logging.getLogger("legba.source.gdelt")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fully-qualified BigQuery table for GDELT 2.0 events.
EVENTS_TABLE = "gdelt-bq.gdeltv2.events"

#: Fully-qualified BigQuery table for the Global Knowledge Graph (gkg).
#: Not pulled by default in v1 — events are richer for the structured signal
#: surface. Kept as a constant so a future ``include_gkg`` flag can join it.
GKG_TABLE = "gdelt-bq.gdeltv2.gkg"

#: GDELT publishes a new file every 15 minutes; the default lookback matches.
DEFAULT_LOOKBACK_MINUTES = 15

#: Hard-cap on bytes scanned per pull, regardless of config. A safety net
#: against accidental cost blowouts (10 GiB). Config caps may be tighter.
HARD_BYTES_CAP = 10 * 1024 * 1024 * 1024

#: Warn (do not refuse) above this threshold (1 GiB).
WARN_BYTES_THRESHOLD = 1 * 1024 * 1024 * 1024

#: Columns selected from the events table. Held as a constant so SQL-shape
#: tests don't drift from the actual schema definition.
EVENT_COLUMNS: tuple[str, ...] = (
    "GLOBALEVENTID",
    "SQLDATE",
    "MonthYear",
    "Year",
    "Actor1Code",
    "Actor1Name",
    "Actor1CountryCode",
    "Actor1Type1Code",
    "Actor2Code",
    "Actor2Name",
    "Actor2CountryCode",
    "Actor2Type1Code",
    "IsRootEvent",
    "EventCode",
    "EventBaseCode",
    "EventRootCode",
    "QuadClass",
    "GoldsteinScale",
    "NumMentions",
    "NumSources",
    "NumArticles",
    "AvgTone",
    "ActionGeo_Type",
    "ActionGeo_FullName",
    "ActionGeo_CountryCode",
    "ActionGeo_ADM1Code",
    "ActionGeo_Lat",
    "ActionGeo_Long",
    "DATEADDED",
    "SOURCEURL",
)


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class GDELTConfig(BaseModel):
    """Configuration for one GDELT source instance.

    All filter fields are optional — an instance with no filters set will
    return *every* event in the lookback window (which is rarely what an
    operator wants, given the cost). The descriptor-author is expected to
    set at least one of ``cameo_country`` / ``event_root_codes``.
    """

    model_config = ConfigDict(strict=False, extra="forbid")

    cameo_country: str | None = Field(
        default=None,
        description=(
            "FIPS 10-4 country code applied to ActionGeo_CountryCode. "
            "Two letters (per FIPS); the field is stored uppercase. "
            "Examples: 'BR' (Brazil), 'US' (USA), 'IR' (Iran)."
        ),
    )

    actor_filter: str | None = Field(
        default=None,
        description=(
            "Regex matched against Actor1Type1Code OR Actor2Type1Code. "
            "Examples: 'ENERGY|UTIL', 'MIL|COP', 'GOV|JUD'. "
            "Compiled at validation time so bad patterns surface immediately."
        ),
    )

    event_root_codes: list[str] | None = Field(
        default=None,
        description=(
            "CAMEO event root-code filter applied to EventRootCode. "
            "Two-digit string codes. Examples: ['14', '18'] for protest + "
            "assault, ['19', '20'] for fight + mass violence."
        ),
    )

    tone_filter: tuple[float, float] | None = Field(
        default=None,
        description=(
            "Inclusive (min, max) range applied to AvgTone. "
            "AvgTone ranges roughly -100 to +100 in practice; "
            "GDELT documents the column as 'typically -10 to +10'."
        ),
    )

    lookback_minutes: int = Field(
        default=DEFAULT_LOOKBACK_MINUTES,
        ge=1,
        le=10080,  # one week
        description=(
            "How far back to query when no cursor is set. Default matches "
            "the GDELT 15-minute refresh cadence."
        ),
    )

    bq_credentials_secret: str = Field(
        ...,
        description=(
            "Vault credential id holding the Google service-account JSON "
            "key (dotted identifier, e.g. 'creds.gdelt.bigquery_sa'). "
            "The handler resolves this via the runtime credential resolver."
        ),
    )

    bq_project_id: str | None = Field(
        default=None,
        description=(
            "Google Cloud project to bill the query against. Falls back to "
            "the service-account JSON's 'project_id' field when unset."
        ),
    )

    bq_location: str = Field(
        default="US",
        description=(
            "BigQuery location for the query job. GDELT public tables live "
            "in the US multi-region; overriding is rare."
        ),
    )

    cost_cap_bytes_per_pull: int = Field(
        default=WARN_BYTES_THRESHOLD,
        ge=1,
        le=HARD_BYTES_CAP,
        description=(
            "Refuse any pull whose dry-run estimate exceeds this many "
            "bytes scanned. Defaults to 1 GiB; bump deliberately."
        ),
    )

    daily_cap_bytes: int | None = Field(
        default=None,
        description=(
            "Optional per-day rolling cap on bytes scanned. When set, the "
            "handler tracks cumulative bytes in state_store and refuses "
            "pulls that would exceed the cap in the current UTC day."
        ),
    )

    max_rows_per_pull: int = Field(
        default=10_000,
        ge=1,
        le=1_000_000,
        description=(
            "LIMIT clause applied to the query. Defensive cap — even a "
            "single-country pull on a normal 15-minute window is typically "
            "well under 1k rows."
        ),
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("cameo_country")
    @classmethod
    def _validate_country(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", v):
            raise ValueError(
                f"cameo_country must be a two-letter FIPS code; got {v!r}"
            )
        return v

    @field_validator("actor_filter")
    @classmethod
    def _validate_actor_regex(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"actor_filter must be a valid regex: {exc}") from exc
        return v

    @field_validator("event_root_codes")
    @classmethod
    def _validate_root_codes(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out: list[str] = []
        for code in v:
            code = code.strip()
            if not re.fullmatch(r"\d{1,2}", code):
                raise ValueError(
                    f"event_root_codes entries must be 1-2 digits; got {code!r}"
                )
            # Pad to two digits — CAMEO root codes are conventionally "01".."20".
            out.append(code.zfill(2))
        return out

    @model_validator(mode="after")
    def _validate_tone_range(self) -> "GDELTConfig":
        if self.tone_filter is not None:
            lo, hi = self.tone_filter
            if lo > hi:
                raise ValueError(
                    f"tone_filter min must be <= max; got ({lo}, {hi})"
                )
        if self.daily_cap_bytes is not None and self.daily_cap_bytes < self.cost_cap_bytes_per_pull:
            # Not strictly illegal — a tight daily cap may exceed per-pull —
            # but a daily cap lower than the per-pull cap is almost always
            # a config mistake.
            raise ValueError(
                "daily_cap_bytes is lower than cost_cap_bytes_per_pull "
                "— a single pull would exceed the daily budget"
            )
        return self


# ---------------------------------------------------------------------------
# BigQuery client abstraction
# ---------------------------------------------------------------------------


# Injection seam: tests substitute a fake client implementing these two
# methods. The handler never imports google.cloud.bigquery at top level.

CredentialResolver = Callable[[str], "Awaitable[bytes]"]
"""Async callable: vault-id (string) -> service-account JSON bytes.

Provided by the runtime. The handler never decrypts credentials itself.
Resolution happens per-pull (not cached) so credential rotation is
immediate, per L-102 §7.
"""

ClientFactory = Callable[[dict[str, Any], str | None, str], Any]
"""Sync callable: (service_account_info, project_id, location) -> client.

The returned client must expose ``query(sql, job_config) -> job`` and
``query(sql, job_config=QueryJobConfig(dry_run=True))`` returning a job
with ``total_bytes_processed``. In production the runtime injects a
factory that constructs ``google.cloud.bigquery.Client``; tests inject a
fake.
"""


class _StubParam:
    """Duck-typed stand-in for ``bigquery.ScalarQueryParameter``.

    Exposes the same ``name`` + ``value`` attribute surface so callers
    (production and test client alike) can iterate ``query_parameters``
    without branching on whether google-cloud-bigquery is installed.
    """

    __slots__ = ("name", "value")

    def __init__(self, *, name: str, value: Any) -> None:
        self.name = name
        self.value = value

    def __repr__(self) -> str:
        return f"_StubParam(name={self.name!r}, value={self.value!r})"


def _default_client_factory(
    service_account_info: dict[str, Any],
    project_id: str | None,
    location: str,
) -> Any:
    """Lazy-import google-cloud-bigquery and build a Client.

    Imported here, not at module top, so missing-dependency cases surface
    as a clean ``ImportError`` at activation time rather than at import
    time — which would block test collection for every consumer of this
    module.
    """
    try:
        from google.cloud import bigquery
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover — exercised via integration
        raise ImportError(
            "google-cloud-bigquery and google-auth are required to run the "
            "GDELT source handler. Install with: pip install google-cloud-bigquery"
        ) from exc

    creds = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/bigquery.readonly"],
    )
    effective_project = project_id or service_account_info.get("project_id")
    if not effective_project:
        raise ValueError(
            "no BigQuery project_id configured; set bq_project_id or use a "
            "service-account JSON that includes project_id"
        )
    return bigquery.Client(
        project=effective_project,
        credentials=creds,
        location=location,
    )


# ---------------------------------------------------------------------------
# SQL composition (pure)
# ---------------------------------------------------------------------------


def build_gdelt_sql(
    cfg: GDELTConfig,
    since: datetime | None,
    *,
    table: str = EVENTS_TABLE,
) -> tuple[str, dict[str, Any]]:
    """Compose the GDELT-events SQL + the parameter map for the job.

    Pure function — no side effects, no I/O. Exposed at module level so
    unit tests can assert the SQL shape directly. Uses BigQuery's named
    parameters (``@name`` placeholders) for every dynamic value; never
    interpolates user-controlled strings into the SQL text.
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(minutes=cfg.lookback_minutes)
    elif since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    where: list[str] = []
    params: dict[str, Any] = {}

    # SQLDATE is an INT64 in GDELT (YYYYMMDD). Partition the scan by date.
    # We push the lower bound to a date int to make the partition prune
    # effective; the DATEADDED filter below tightens to the minute.
    sql_date_int = int(since.strftime("%Y%m%d"))
    where.append("SQLDATE >= @sql_date_min")
    params["sql_date_min"] = sql_date_int

    # DATEADDED is an INT64 too, in YYYYMMDDHHMMSS form. Use it as the
    # secondary cursor: precise minute-level filtering inside the partition.
    date_added_int = int(since.strftime("%Y%m%d%H%M%S"))
    where.append("DATEADDED >= @date_added_min")
    params["date_added_min"] = date_added_int

    if cfg.cameo_country is not None:
        where.append("ActionGeo_CountryCode = @country")
        params["country"] = cfg.cameo_country

    if cfg.event_root_codes:
        where.append("EventRootCode IN UNNEST(@root_codes)")
        params["root_codes"] = list(cfg.event_root_codes)

    if cfg.actor_filter is not None:
        where.append(
            "(REGEXP_CONTAINS(IFNULL(Actor1Type1Code, ''), @actor_re) "
            "OR REGEXP_CONTAINS(IFNULL(Actor2Type1Code, ''), @actor_re))"
        )
        params["actor_re"] = cfg.actor_filter

    if cfg.tone_filter is not None:
        lo, hi = cfg.tone_filter
        where.append("AvgTone BETWEEN @tone_lo AND @tone_hi")
        params["tone_lo"] = float(lo)
        params["tone_hi"] = float(hi)

    columns = ",\n  ".join(EVENT_COLUMNS)
    where_clause = "\n  AND ".join(where)
    sql = (
        f"SELECT\n  {columns}\n"
        f"FROM `{table}`\n"
        f"WHERE {where_clause}\n"
        f"ORDER BY DATEADDED ASC\n"
        f"LIMIT @row_limit"
    )
    params["row_limit"] = cfg.max_rows_per_pull
    return sql, params


def _row_content_hash(row: dict[str, Any]) -> str:
    """Stable content hash for dedupe. Keyed on GLOBALEVENTID + DATEADDED.

    GLOBALEVENTID alone is *not* always unique across re-runs of the same
    event when GDELT re-emits with updated mentions/sources counts; pairing
    it with DATEADDED yields a stable identity for the *first observation*
    of the row at that ingestion timestamp.
    """
    key = f"gdelt:{row.get('GLOBALEVENTID')}:{row.get('DATEADDED')}"
    return sha256(key.encode("utf-8")).hexdigest()


def _row_to_signal_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Project a BigQuery result row to the L-102 Signal.payload shape.

    Field names mirror what the downstream enrichment pipeline expects:
    ``geo``, ``actors``, ``event_code``, ``tone``, ``source_url``, plus
    ``raw_body`` carrying the full row for any later analyst that wants
    to dig in.
    """
    return {
        "external_id": row.get("GLOBALEVENTID"),
        "published_at": row.get("SQLDATE"),
        "date_added": row.get("DATEADDED"),
        "geo": {
            "type": row.get("ActionGeo_Type"),
            "full_name": row.get("ActionGeo_FullName"),
            "country_code": row.get("ActionGeo_CountryCode"),
            "adm1_code": row.get("ActionGeo_ADM1Code"),
            "lat": row.get("ActionGeo_Lat"),
            "lon": row.get("ActionGeo_Long"),
        },
        "actors": {
            "actor1_code": row.get("Actor1Code"),
            "actor1_name": row.get("Actor1Name"),
            "actor1_country": row.get("Actor1CountryCode"),
            "actor1_type": row.get("Actor1Type1Code"),
            "actor2_code": row.get("Actor2Code"),
            "actor2_name": row.get("Actor2Name"),
            "actor2_country": row.get("Actor2CountryCode"),
            "actor2_type": row.get("Actor2Type1Code"),
        },
        "event_code": row.get("EventCode"),
        "event_base_code": row.get("EventBaseCode"),
        "event_root_code": row.get("EventRootCode"),
        "quad_class": row.get("QuadClass"),
        "goldstein_scale": row.get("GoldsteinScale"),
        "tone": row.get("AvgTone"),
        "num_mentions": row.get("NumMentions"),
        "num_sources": row.get("NumSources"),
        "num_articles": row.get("NumArticles"),
        "source_url": row.get("SOURCEURL"),
        "raw_body": row,
    }


def _parse_sqldate(sqldate: Any) -> datetime | None:
    """Decode the GDELT SQLDATE integer (YYYYMMDD) into a UTC datetime.

    Tolerates the column being already-stringified (some BigQuery
    drivers return INT64 as a Python int, others as str).
    """
    if sqldate is None:
        return None
    s = str(sqldate)
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_dateadded(date_added: Any) -> datetime | None:
    """Decode GDELT DATEADDED (YYYYMMDDHHMMSS) into a UTC datetime."""
    if date_added is None:
        return None
    s = str(date_added)
    if len(s) != 14 or not s.isdigit():
        return None
    try:
        return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Cost accounting helpers
# ---------------------------------------------------------------------------


def _utc_day_key(when: datetime) -> str:
    """Day bucket key for daily-cap accounting."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class CostCapExceeded(RuntimeError):
    """Raised when a dry-run estimate exceeds the configured per-pull or
    daily cap. The runtime treats this as a non-transient failure (the
    caller has to bump the cap deliberately).
    """

    def __init__(self, estimated: int, cap: int, *, scope: str):
        super().__init__(
            f"GDELT pull aborted: estimated {estimated:,} bytes exceeds "
            f"{scope} cap of {cap:,} bytes"
        )
        self.estimated = estimated
        self.cap = cap
        self.scope = scope


class GDELTBigQuerySourceHandler:
    """L-102 source-kind handler for GDELT 2.0 via BigQuery.

    Implements the :class:`SourceHandler` protocol structurally — no base
    class. The runtime hands in a credential resolver (Phase-2 vault
    integration) and, optionally, a BigQuery client factory (tests inject
    a fake; production uses the lazy ``_default_client_factory``).

    State stored in ``ctx.state_store``:

      * ``gdelt_last_dateadded`` (int) — DATEADDED of the most-recent row
        observed; consulted as the cursor on the next pull.
      * ``gdelt_daily_bytes`` (dict[str, int]) — day-bucketed running total
        of bytes scanned, for ``daily_cap_bytes`` accounting.
      * ``gdelt_last_success_at`` (str) — ISO timestamp; surfaced on the
        health probe.
      * ``gdelt_rows_pulled_24h`` (list[tuple[str, int]]) — rolling list
        of (iso-timestamp, row-count) entries within the last 24h.
      * ``gdelt_last_error`` (str | None) — most-recent error message for
        the health probe.
    """

    # KindHandler / SourceHandler classvar surface (L-102 §1 + §2).
    kind: ClassVar[str] = "gdelt_query"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.gdelt_query/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = GDELTConfig

    def __init__(
        self,
        config: GDELTConfig,
        *,
        credential_resolver: CredentialResolver | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.config = config
        self._credential_resolver = credential_resolver
        self._client_factory = client_factory or _default_client_factory

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_service_account(
        self,
        ctx: SourceContext | None = None,
    ) -> dict[str, Any]:
        """Resolve the service-account JSON.

        Precedence: ``ctx.secrets_resolve`` (the L-103/L-111 contract) wins
        if set; otherwise the constructor-injected resolver is used. This
        lets the runtime drive credential resolution via SourceContext
        without the handler caring, while tests can still inject a fake
        via the constructor.
        """
        resolver = None
        if ctx is not None and getattr(ctx, "secrets_resolve", None) is not None:
            resolver = ctx.secrets_resolve
        if resolver is None:
            resolver = self._credential_resolver
        if resolver is None:
            raise RuntimeError(
                "GDELT handler has no credential resolver bound; the "
                "runtime must inject one via SourceContext.secrets_resolve "
                "or via the handler constructor."
            )
        raw = await resolver(self.config.bq_credentials_secret)
        if isinstance(raw, (bytes, bytearray)):
            raw_str = bytes(raw).decode("utf-8")
        else:
            raw_str = str(raw)
        try:
            return json.loads(raw_str)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"vault credential {self.config.bq_credentials_secret!r} "
                "is not valid service-account JSON"
            ) from exc

    def _build_client(self, service_account_info: dict[str, Any]) -> Any:
        return self._client_factory(
            service_account_info,
            self.config.bq_project_id,
            self.config.bq_location,
        )

    def _build_query_job_config(self, params: dict[str, Any], *, dry_run: bool) -> Any:
        """Build a google-cloud-bigquery ``QueryJobConfig``.

        Imported lazily; only when the handler is actually about to talk
        to BigQuery. Falls back to a duck-typed object exposing the same
        ``dry_run`` / ``use_query_cache`` / ``query_parameters`` attribute
        surface when the package isn't installed — keeps the test client
        shape identical to production.
        """
        named_params = [_StubParam(name=k, value=v) for k, v in params.items()]
        try:
            from google.cloud import bigquery
        except ImportError:  # pragma: no cover — tests inject a fake client
            class _Stub:
                pass
            cfg = _Stub()
            cfg.dry_run = dry_run
            cfg.use_query_cache = not dry_run
            cfg.query_parameters = named_params
            return cfg

        job_params = []
        for name, value in params.items():
            if isinstance(value, list):
                element_type = "STRING" if value and isinstance(value[0], str) else "INT64"
                job_params.append(
                    bigquery.ArrayQueryParameter(name, element_type, value)
                )
            elif isinstance(value, bool):
                job_params.append(bigquery.ScalarQueryParameter(name, "BOOL", value))
            elif isinstance(value, int):
                job_params.append(bigquery.ScalarQueryParameter(name, "INT64", value))
            elif isinstance(value, float):
                job_params.append(bigquery.ScalarQueryParameter(name, "FLOAT64", value))
            else:
                job_params.append(bigquery.ScalarQueryParameter(name, "STRING", str(value)))

        return bigquery.QueryJobConfig(
            dry_run=dry_run,
            use_query_cache=not dry_run,
            query_parameters=job_params,
        )

    async def _enforce_cost_cap(
        self,
        ctx: SourceContext,
        client: Any,
        sql: str,
        params: dict[str, Any],
    ) -> int:
        """Run a dry-run query and refuse to proceed if it's too expensive.

        Returns the estimated bytes-scanned so the caller can record it for
        daily-cap accounting. Raises :class:`CostCapExceeded` on overrun.
        Emits a structured warning if the estimate crosses the soft
        ``WARN_BYTES_THRESHOLD`` even when below the configured cap.
        """
        dry_cfg = self._build_query_job_config(params, dry_run=True)
        dry_job = await asyncio.to_thread(client.query, sql, job_config=dry_cfg)
        estimated = int(getattr(dry_job, "total_bytes_processed", 0) or 0)

        if estimated > self.config.cost_cap_bytes_per_pull:
            raise CostCapExceeded(
                estimated, self.config.cost_cap_bytes_per_pull, scope="per-pull"
            )

        if self.config.daily_cap_bytes is not None:
            daily = await ctx.state_store.get("gdelt_daily_bytes") or {}
            today = _utc_day_key(datetime.now(timezone.utc))
            already = int(daily.get(today, 0))
            if already + estimated > self.config.daily_cap_bytes:
                raise CostCapExceeded(
                    already + estimated,
                    self.config.daily_cap_bytes,
                    scope="daily",
                )

        if estimated > WARN_BYTES_THRESHOLD:
            ctx.logger.warning(
                "GDELT pull estimate %d bytes exceeds soft threshold %d "
                "(target=%s source=%s)",
                estimated,
                WARN_BYTES_THRESHOLD,
                ctx.target_id,
                ctx.source_id,
            )

        return estimated

    async def _record_bytes(self, ctx: SourceContext, bytes_scanned: int) -> None:
        if not bytes_scanned:
            return
        daily = await ctx.state_store.get("gdelt_daily_bytes") or {}
        today = _utc_day_key(datetime.now(timezone.utc))
        # Prune day-buckets older than 7 days to keep the dict bounded.
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        cutoff_key = _utc_day_key(cutoff)
        daily = {k: v for k, v in daily.items() if k >= cutoff_key}
        daily[today] = int(daily.get(today, 0)) + int(bytes_scanned)
        await ctx.state_store.set("gdelt_daily_bytes", daily)

    async def _record_rows(self, ctx: SourceContext, row_count: int) -> None:
        if not row_count:
            return
        now = datetime.now(timezone.utc)
        rolling = await ctx.state_store.get("gdelt_rows_pulled_24h") or []
        cutoff = now - timedelta(hours=24)
        rolling = [
            (ts, n) for (ts, n) in rolling
            if datetime.fromisoformat(ts) > cutoff
        ]
        rolling.append((now.isoformat(), int(row_count)))
        await ctx.state_store.set("gdelt_rows_pulled_24h", rolling)

    # ------------------------------------------------------------------
    # Lifecycle hooks (no-ops; runtime calls them on state transitions)
    # ------------------------------------------------------------------

    async def on_configure(self, ctx: SourceContext) -> None:
        return None

    async def on_activate(self, ctx: SourceContext) -> None:
        return None

    async def on_pause(self, ctx: SourceContext) -> None:
        return None

    async def on_resume(self, ctx: SourceContext) -> None:
        return None

    async def on_retire(self, ctx: SourceContext) -> None:
        return None

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Yield Signals for events newer than the stored cursor.

        ``since`` is a hint per L-102 §2; the handler's own cursor in
        ``state_store`` is the authoritative lower bound. Overlapping
        re-emission of rows is allowed — downstream dedupe (L-151) will
        suppress duplicates.
        """
        # Cursor precedence: explicit ``since`` argument > stored cursor >
        # ``now - lookback_minutes``. Stored cursor is the DATEADDED int
        # we observed last time.
        stored_cursor_int = await ctx.state_store.get("gdelt_last_dateadded")
        effective_since: datetime | None = since
        if effective_since is None and stored_cursor_int is not None:
            effective_since = _parse_dateadded(stored_cursor_int)
        if effective_since is None:
            effective_since = datetime.now(timezone.utc) - timedelta(
                minutes=self.config.lookback_minutes
            )

        sql, params = build_gdelt_sql(self.config, effective_since)

        try:
            sa_info = await self._resolve_service_account(ctx)
            client = self._build_client(sa_info)
        except Exception as exc:
            await ctx.state_store.set("gdelt_last_error", f"setup: {exc!r}")
            ctx.logger.exception("GDELT setup failed: %s", exc)
            raise

        try:
            estimated_bytes = await self._enforce_cost_cap(
                ctx, client, sql, params
            )
        except CostCapExceeded as exc:
            await ctx.state_store.set("gdelt_last_error", str(exc))
            ctx.logger.error("%s", exc)
            raise

        # Real query.
        real_cfg = self._build_query_job_config(params, dry_run=False)
        try:
            job = await asyncio.to_thread(client.query, sql, job_config=real_cfg)
            rows = await asyncio.to_thread(_materialize_rows, job)
        except Exception as exc:
            await ctx.state_store.set("gdelt_last_error", f"query: {exc!r}")
            ctx.logger.exception("GDELT query failed: %s", exc)
            raise

        # Record actual bytes scanned (may differ from estimate).
        actual_bytes = int(getattr(job, "total_bytes_processed", estimated_bytes) or 0)
        await self._record_bytes(ctx, actual_bytes)

        max_date_added_seen: int | None = None
        emitted = 0
        for row in rows:
            payload = _row_to_signal_payload(row)
            published_at = _parse_sqldate(row.get("SQLDATE"))
            language_hint = None  # GDELT events table doesn't carry lang;
            # gkg does. Left None — language_detect filter (L-150) populates.

            sig = Signal(
                signal_id=uuid4(),
                source_id=ctx.source_id,
                payload=payload,
                content_hash=_row_content_hash(row),
                canonical_url=row.get("SOURCEURL") or None,
                language_hint=language_hint,
                raw_provenance={
                    "kind": "gdelt_query",
                    "table": EVENTS_TABLE,
                    "global_event_id": row.get("GLOBALEVENTID"),
                    "date_added": row.get("DATEADDED"),
                    "sql_date": row.get("SQLDATE"),
                    "estimated_bytes": estimated_bytes,
                    "actual_bytes": actual_bytes,
                    "published_at": published_at.isoformat() if published_at else None,
                },
            )
            yield sig

            emitted += 1
            try:
                da = int(row.get("DATEADDED")) if row.get("DATEADDED") is not None else None
            except (TypeError, ValueError):
                da = None
            if da is not None and (max_date_added_seen is None or da > max_date_added_seen):
                max_date_added_seen = da

        # Advance cursor & metrics after the iterator is exhausted.
        if max_date_added_seen is not None:
            await ctx.state_store.set("gdelt_last_dateadded", max_date_added_seen)
        await self._record_rows(ctx, emitted)
        await ctx.state_store.set(
            "gdelt_last_success_at",
            datetime.now(timezone.utc).isoformat(),
        )
        await ctx.state_store.set("gdelt_last_error", None)

    # ------------------------------------------------------------------
    # health_check
    # ------------------------------------------------------------------

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Dry-run the configured query for cost & connectivity check.

        Resolves the credential, builds a client, and issues a *dry-run*
        query. Returns ``healthy`` if both succeed; ``degraded`` if the
        dry-run succeeds but the estimate exceeds the soft warn threshold;
        ``unhealthy`` if anything raises.
        """
        last_success = await ctx.state_store.get("gdelt_last_success_at")
        last_success_dt: datetime | None = None
        if last_success:
            try:
                last_success_dt = datetime.fromisoformat(last_success)
            except (TypeError, ValueError):
                last_success_dt = None

        rolling = await ctx.state_store.get("gdelt_rows_pulled_24h") or []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        rows_24h = sum(
            n for (ts, n) in rolling
            if _safe_iso(ts) and _safe_iso(ts) > cutoff
        )

        cursor_int = await ctx.state_store.get("gdelt_last_dateadded")
        last_error = await ctx.state_store.get("gdelt_last_error")

        try:
            sa_info = await self._resolve_service_account(ctx)
            client = self._build_client(sa_info)
        except Exception as exc:
            return SourceHealth(
                state="unhealthy",
                last_success_at=last_success_dt,
                last_error=f"credential/client setup: {exc!r}",
                rows_pulled_24h=rows_24h,
                last_cursor=str(cursor_int) if cursor_int is not None else None,
                detail={
                    "phase": "setup",
                    "kind": self.kind,
                },
            )

        sql, params = build_gdelt_sql(self.config, since=None)
        try:
            dry_cfg = self._build_query_job_config(params, dry_run=True)
            dry_job = await asyncio.to_thread(client.query, sql, job_config=dry_cfg)
            estimated = int(getattr(dry_job, "total_bytes_processed", 0) or 0)
        except Exception as exc:
            return SourceHealth(
                state="unhealthy",
                last_success_at=last_success_dt,
                last_error=f"dry_run: {exc!r}",
                rows_pulled_24h=rows_24h,
                last_cursor=str(cursor_int) if cursor_int is not None else None,
                detail={
                    "phase": "dry_run",
                    "kind": self.kind,
                },
            )

        state: str = "healthy"
        if estimated > self.config.cost_cap_bytes_per_pull:
            state = "degraded"
        elif estimated > WARN_BYTES_THRESHOLD:
            state = "degraded"

        return SourceHealth(
            state=state,
            last_success_at=last_success_dt,
            last_error=last_error,
            rows_pulled_24h=rows_24h,
            last_cursor=str(cursor_int) if cursor_int is not None else None,
            detail={
                "kind": self.kind,
                "estimated_bytes_next_pull": estimated,
                "cost_cap_bytes_per_pull": self.config.cost_cap_bytes_per_pull,
                "daily_cap_bytes": self.config.daily_cap_bytes,
                "warn_threshold_bytes": WARN_BYTES_THRESHOLD,
                "project_id": self.config.bq_project_id
                    or sa_info.get("project_id"),
                "location": self.config.bq_location,
            },
        )


def _safe_iso(s: Any) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _materialize_rows(job: Any) -> list[dict[str, Any]]:
    """Convert a BigQuery RowIterator to a list of plain dicts.

    Done on a thread because the underlying iterator blocks on network I/O.
    Tests inject a fake job whose ``result()`` returns a sequence of dicts.
    """
    result = job.result() if hasattr(job, "result") else job
    out: list[dict[str, Any]] = []
    for row in result:
        if isinstance(row, dict):
            out.append(dict(row))
        elif hasattr(row, "items"):
            out.append({k: v for k, v in row.items()})
        elif hasattr(row, "_mapping"):
            out.append(dict(row._mapping))
        else:
            # Unknown shape — best-effort attribute walk.
            out.append({k: getattr(row, k) for k in EVENT_COLUMNS if hasattr(row, k)})
    return out


__all__ = [
    "CostCapExceeded",
    "EVENT_COLUMNS",
    "EVENTS_TABLE",
    "GDELTBigQuerySourceHandler",
    "GDELTConfig",
    "GKG_TABLE",
    "HARD_BYTES_CAP",
    "WARN_BYTES_THRESHOLD",
    "build_gdelt_sql",
]
