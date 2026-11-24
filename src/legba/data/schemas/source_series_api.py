# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``series_api`` — the declarative config of a generic numeric-series source.

A LEAF (pydantic + stdlib only): the source descriptor schema validates a
``series_api`` source's ``config`` against :class:`SeriesApiConfig` AT SAVE,
in the slim registry image, and cross-checks every output against the
source's own series registry (``sink.config.series``). The handler
(:mod:`legba.data.sources.series_api`) parses the same model at activation.

WHAT IT DESCRIBES. One HTTP GET endpoint (JSON or CSV) that returns a time
series as rows:

* ``url`` + ``params`` (ordered ``[name, value]`` pairs, repeats allowed —
  ``facets[series][]`` twice is two pairs) + an optional ``auth`` (a vault
  SecretRef applied as a query parameter or a header, never a literal);
* ``since_param`` — the parameter that asks for rows from a date on, the
  format the endpoint wants it in (closed vocabulary :data:`SINCE_FORMATS`)
  and an optional ``template`` with one ``{date}`` slot (a filter expression
  such as ``record_date:gte:{date}``); every poll asks from ``today - (lag_days +
  revision_days)`` (the first poll from ``today - backfill_days``) so a late
  or revised value is re-read and the observation sink writes only what
  changed (an unchanged re-read writes nothing);
* ``pagination`` — none, or offset/limit with a page size and a page cap;
* ``format`` + ``rows_path`` (JSON: a dotted path to the row array; ``$``
  is the document root, for an endpoint that returns a TOP-LEVEL array; a
  segment that meets an array is a decimal index, so ``1`` reads the second
  element of a ``[meta, rows]`` pair) — CSV rows are the records, keyed by
  the header; ``row_columns`` names the cells of POSITIONAL rows (an array of
  arrays), which are then read as records with those field names;
* ``format: jsonstat`` — a JSON-stat 2.0 dataset (dimension ids and sizes, a
  dense value array or a sparse value object, an optional status): every
  cell becomes one row whose fields are the dimension ids (the category
  code), ``value`` and ``status`` (:mod:`legba.data.sources.jsonstat`); a
  cube over ``jsonstat_max_cells`` is refused, never truncated;
* ``date_field`` + ``date_format`` (closed vocabulary :data:`DATE_FORMATS`:
  a format is DECLARED per source, never guessed — a row whose date does
  not match the declared format exactly is skipped, not re-read another way);
* ``outputs`` — one per series: the ``value_field``, the subject (a constant
  ``subject`` or a ``subject_field``), optional ``where`` filters (a row
  feeds the output only when every filter matches), and the series'
  ``unit`` / ``period`` / ``subject_kind`` / ``methodology_version``, which
  must EQUAL the declaration in ``sink.config.series`` (refused at save
  otherwise — the sink would reject every row at the first poll).

WHAT IS REFUSED AT SAVE: a config that cannot produce a dated numeric value —
no outputs, an output without a value field or without a subject, a date
format outside the vocabulary, a JSON source without a row path, a series id
the sink does not declare, a unit/period/subject-kind/methodology that
disagrees with it, a non-https URL, an auth block holding something other
than a dotted vault reference; (I-5) a since format or template outside its
vocabulary, a malformed ``rows_path``, ``row_columns`` on a non-JSON source,
and a URL carrying its own query string while the config also adds
parameters (the HTTP client replaces the URL's query whenever it sends
params, so that query would silently vanish).
"""

from __future__ import annotations

import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .source_sink import PERIODS, UNITS, ObservationSinkConfig

__all__ = [
    "DATE_FORMATS",
    "JSONSTAT_MAX_CELLS_CEILING",
    "SINCE_FORMATS",
    "SERIES_API_KIND",
    "SeriesApiAuth",
    "SeriesApiConfig",
    "SeriesApiOutput",
    "SeriesApiPagination",
    "SeriesApiSince",
    "SeriesApiWhere",
    "unwrap_factory_values",
    "validate_series_api_descriptor",
]

SERIES_API_KIND = "series_api"

#: How a row's date field is written. ``iso_date`` YYYY-MM-DD; ``iso_datetime``
#: an ISO 8601 instant (its UTC date is taken); ``year_month`` YYYY-MM;
#: ``year`` YYYY; ``year_quarter`` YYYY-Qn; ``epoch_ms`` / ``epoch_s``.
#: Lane I-5 adds three day-first forms, each matched EXACTLY (no free-form
#: parsing, no day/month guessing): ``dmy_dotted`` DD.MM.YYYY (the National
#: Bank of Ukraine), ``dmy_slashed`` DD/MM/YYYY (Banco Central do Brasil),
#: ``day_mon_year`` "DD Mon YYYY" with an English three-letter month (the Bank
#: of England database CSV). Roll 84 adds ``ymd_compact`` YYYYMMDD, eight
#: digits exactly (the National Bank of Ukraine's key-rate directory, ``dt``).
DATE_FORMATS: tuple[str, ...] = (
    "iso_date", "iso_datetime", "year_month", "year", "year_quarter", "epoch_ms", "epoch_s",
    "dmy_dotted", "dmy_slashed", "day_mon_year", "ymd_compact",
)
#: How a since parameter is RENDERED for the endpoint. The three originals,
#: the two day-first forms above, and ``day_mon_year_slashed`` DD/Mon/YYYY
#: (the Bank of England database's ``Datefrom``).
SINCE_FORMATS: tuple[str, ...] = (
    "iso_date", "year_month", "year", "dmy_dotted", "dmy_slashed", "day_mon_year",
    "day_mon_year_slashed",
)
#: JSON-stat: the hard ceiling on cells a source may declare it reads.
JSONSTAT_MAX_CELLS_CEILING = 1_000_000
_ROWS_PATH_RE = re.compile(r"^(\$|[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*)$")
_FIELD_RE = re.compile(r"^[A-Za-z0-9_\-\[\]. ]{1,128}$")
_REF_RE = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)+$")


def _field(v: str) -> str:
    if not _FIELD_RE.match(v):
        raise ValueError(f"field name {v!r} must be 1-128 of [A-Za-z0-9_-[]. ]")
    return v


class SeriesApiAuth(BaseModel):
    """A vault SecretRef applied as a query parameter or a header."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["query", "header"] = "query"
    name: str = Field(min_length=1, max_length=64)
    secret_ref: str = Field(min_length=3, max_length=256)
    value_template: str = Field(default="{secret}", max_length=256)

    @field_validator("secret_ref")
    @classmethod
    def _dotted(cls, v: str) -> str:
        if not _REF_RE.match(v):
            raise ValueError("auth.secret_ref must be a dotted vault reference, never the secret itself")
        return v

    @field_validator("value_template")
    @classmethod
    def _one_secret_slot(cls, v: str) -> str:
        if v.count("{secret}") != 1:
            raise ValueError("auth.value_template must contain '{secret}' exactly once")
        return v


class SeriesApiSince(BaseModel):
    """The parameter that asks for rows from a date on."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    format: str = "iso_date"
    #: I-5 — the parameter VALUE around the rendered date, with exactly one
    #: ``{date}`` slot (``record_date:gte:{date}``); unset = the date alone.
    template: str | None = Field(default=None, min_length=6, max_length=128)

    @field_validator("format")
    @classmethod
    def _closed(cls, v: str) -> str:
        if v not in SINCE_FORMATS:
            raise ValueError(f"since_param.format {v!r} is not in {list(SINCE_FORMATS)}")
        return v

    @field_validator("template")
    @classmethod
    def _one_slot(cls, v: str | None) -> str | None:
        if v is not None and (v.count("{date}") != 1 or v.replace("{date}", "").count("{")
                              or v.count("}") != 1):
            raise ValueError("since_param.template must contain '{date}' exactly once and no other braces")
        return v


class SeriesApiPagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "offset"] = "none"
    offset_param: str = Field(default="offset", min_length=1, max_length=64)
    limit_param: str = Field(default="length", min_length=1, max_length=64)
    page_size: int = Field(default=1000, ge=10, le=10000)
    max_pages: int = Field(default=10, ge=1, le=50)


class SeriesApiWhere(BaseModel):
    """A row feeds an output only when ``field`` is (``negate``: is not) one of ``values``."""

    model_config = ConfigDict(extra="forbid")

    field: str
    values: list[str] = Field(min_length=1, max_length=200)
    negate: bool = False

    @field_validator("field")
    @classmethod
    def _name(cls, v: str) -> str:
        return _field(v)


class SeriesApiOutput(BaseModel):
    """One series this source writes."""

    model_config = ConfigDict(extra="forbid")

    series: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$", max_length=128)
    value_field: str
    subject: str | None = Field(default=None, min_length=1, max_length=256)
    subject_field: str | None = None
    where: list[SeriesApiWhere] = Field(default_factory=list, max_length=8)
    unit: str
    period: str = "day"
    subject_kind: Literal["country", "entity", "target", "global"] = "global"
    methodology_version: str = Field(min_length=1, max_length=64)

    @field_validator("value_field", "subject_field")
    @classmethod
    def _names(cls, v: str | None) -> str | None:
        return None if v is None else _field(v)

    @model_validator(mode="after")
    def _shape(self) -> "SeriesApiOutput":
        if (self.subject is None) == (self.subject_field is None):
            raise ValueError(f"output {self.series!r}: declare exactly one of subject / subject_field")
        if self.unit not in UNITS:
            raise ValueError(f"output {self.series!r}: unit {self.unit!r} is not in {sorted(UNITS)}")
        if self.period not in PERIODS:
            raise ValueError(f"output {self.series!r}: period {self.period!r} is not in {sorted(PERIODS)}")
        return self


class SeriesApiConfig(BaseModel):
    """The ``series_api`` kind's config (see the module docstring)."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(pattern=r"^https://[^\s{}]+$", max_length=2048)
    params: list[tuple[str, str]] = Field(default_factory=list, max_length=40)
    auth: SeriesApiAuth | None = None
    since_param: SeriesApiSince | None = None
    pagination: SeriesApiPagination = Field(default_factory=SeriesApiPagination)
    format: Literal["json", "csv", "jsonstat"] = "json"
    rows_path: str = Field(default="", max_length=256)
    #: I-5 — the field names of POSITIONAL rows (an array of arrays), in cell
    #: order; a row of another length is a shape error, never padded.
    row_columns: list[str] = Field(default_factory=list, max_length=64)
    #: I-5 — the most cells a JSON-stat cube may hold (product of its sizes);
    #: a larger cube is refused, never read in part.
    jsonstat_max_cells: int = Field(default=10_000, ge=1, le=JSONSTAT_MAX_CELLS_CEILING)
    csv_delimiter: str = Field(default=",", min_length=1, max_length=1)
    date_field: str
    date_format: str = "iso_date"
    #: Cell values read as ABSENT (never zero): the provider's missing markers.
    missing_values: list[str] = Field(default_factory=lambda: ["", "NaN", "NA", "N/A", ".", "-"], max_length=16)
    outputs: list[SeriesApiOutput] = Field(min_length=1, max_length=64)
    #: Row fields copied verbatim into each observation's provenance.
    provenance_fields: list[str] = Field(default_factory=list, max_length=8)
    lag_days: int = Field(default=2, ge=0, le=120)
    revision_days: int = Field(default=30, ge=1, le=400)
    backfill_days: int = Field(default=400, ge=1, le=4000)
    backfill_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    timeout_seconds: float = Field(default=60.0, gt=0, le=240)
    user_agent: str = Field(default="legba-source-series-api/0.1", min_length=1, max_length=128)
    poll_budget_seconds: float = Field(default=180.0, gt=0, le=240)

    @field_validator("date_field")
    @classmethod
    def _date_field(cls, v: str) -> str:
        return _field(v)

    @field_validator("provenance_fields", "row_columns")
    @classmethod
    def _prov(cls, v: list[str]) -> list[str]:
        return [_field(x) for x in v]

    @model_validator(mode="after")
    def _produces_dated_numbers(self) -> "SeriesApiConfig":
        if self.date_format not in DATE_FORMATS:
            raise ValueError(f"date_format {self.date_format!r} is not in {list(DATE_FORMATS)}")
        if self.format == "json" and not self.rows_path.strip():
            raise ValueError("a JSON series_api source names rows_path (the dotted path to its row "
                             "array; '$' when the document root is the array)")
        if self.format == "json" and not _ROWS_PATH_RE.match(self.rows_path):
            raise ValueError(f"rows_path {self.rows_path!r} must be '$' or dotted segments of [A-Za-z0-9_-]")
        if self.format in ("csv", "jsonstat") and self.rows_path:
            raise ValueError(f"rows_path is for JSON; {self.format} rows are the records")
        if self.format != "json" and self.row_columns:
            raise ValueError("row_columns names the cells of positional JSON rows; it is for format json")
        if len(self.row_columns) != len(set(self.row_columns)):
            raise ValueError("row_columns must be distinct")
        if "?" in self.url and (self.params or self.since_param is not None
                                or self.pagination.mode != "none"
                                or (self.auth is not None and self.auth.mode == "query")):
            # The HTTP client REPLACES a URL's own query when it sends params:
            # a query in the URL is kept verbatim only when nothing is added.
            raise ValueError("a url with its own query string cannot also add params, a since "
                             "parameter, offset pagination or a query-mode key (the query would be dropped)")
        ids = [o.series for o in self.outputs]
        if len(ids) != len(set(ids)):
            raise ValueError("series_api outputs must name distinct series")
        if self.auth is not None and self.auth.mode == "query" and any(k == self.auth.name for k, _ in self.params):
            raise ValueError(f"params must not carry the auth parameter {self.auth.name!r}")
        return self


def unwrap_factory_values(raw: Mapping[str, Any]) -> dict[str, Any]:
    """``{key: {"factory_kind": ..., "raw": v}}`` → ``{key: v}`` (top level;
    a bare value passes through). The runtime factory's unwrap reads the same
    shape; this leaf copy keeps the registry image free of the runtime."""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, Mapping) and "raw" in v and set(v) - {"raw"} <= {
                "factory_kind", "ui_hint", "item_kind", "key_kind", "value_kind"}:
            out[k] = v["raw"]
        else:
            out[k] = v
    return out


def validate_series_api_descriptor(config: Mapping[str, Any], sink: Any) -> SeriesApiConfig:
    """The save-time check: the config parses and every output agrees with the
    series registry the observation sink enforces. Raises ``ValueError``."""
    if getattr(sink, "family", None) != "observation":
        raise ValueError("a series_api source declares sink.family: observation")
    cfg = SeriesApiConfig.model_validate(unwrap_factory_values(config))
    declared = {s.id: s for s in ObservationSinkConfig.model_validate(sink.config, strict=False).series}
    for out in cfg.outputs:
        spec = declared.get(out.series)
        if spec is None:
            raise ValueError(f"series_api output {out.series!r} is not declared in sink.config.series")
        for attr in ("unit", "period", "subject_kind", "methodology_version"):
            if getattr(out, attr) != getattr(spec, attr):
                raise ValueError(
                    f"series_api output {out.series!r}: {attr} {getattr(out, attr)!r} != the series "
                    f"registry's {getattr(spec, attr)!r}")
        if spec.field != out.value_field:
            raise ValueError(f"series_api output {out.series!r}: value_field {out.value_field!r} != "
                             f"the series registry's field {spec.field!r}")
    return cfg
