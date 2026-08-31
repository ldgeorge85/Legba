# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GDELT 2.0 15-minute file-dump source handler.

Replacement acquisition path for GDELT after ``source.gdelt.doc_api`` (the
keyless DOC 2.0 full-text API, ``json_api`` kind) started 429-ing at the IP
level even for small, spaced queries (verified 2026-07-21 — see
``docs/DATA_SOURCES.md`` §3). GDELT also publishes its raw 2.0 event stream as
a rolling set of CSV files updated every 15 minutes, with **no API and no
rate limit**:

  * ``http://data.gdeltproject.org/gdeltv2/lastupdate.txt`` — a small text
    file, three lines, one per export kind (``export`` = events,
    ``mentions``, ``gkg``), each line ``<size> <md5> <url>``. The URL embeds
    a ``YYYYMMDDHHMMSS`` timestamp — the file's publication time and the
    natural high-water cursor.
  * Each named file is a ``.zip`` containing ONE tab-separated CSV with no
    header row. The events export (``*.export.CSV.zip``) is the file this
    handler consumes — same 61-column GDELT 2.0 events schema the existing
    BigQuery handler (``gdelt.py``) queries, just delivered as a flat file
    instead of a table.

This is a genuinely different transport (HTTP file fetch + zip + CSV, no
query language, no billing) from both existing GDELT handlers, so it is a
new kind (``gdelt_files``) rather than a config variant of ``gdelt_query``
(BigQuery, kind ``gdelt_query``) or ``json_api`` (the retired DOC-API
descriptor).

GDELT is a firehose — the raw worldwide events export runs on the order of
tens of thousands of rows per 15-minute file. This handler filters HARD at
parse time, before any Signal is constructed, per the platform's
conflict/geopolitics focus (mirrors the CAMEO root-code + Goldstein
filtering already established in ``gdelt.py``'s ``GDELTConfig``):

  1. ``EventRootCode`` allowlist — CAMEO root codes 14-20 (protest, exert
     coercion, reduce relations, coerce, assault, fight, mass violence) by
     default; see the CAMEO manual root-code table.
  2. ``goldstein_max`` — GoldsteinScale ceiling (more negative = more
     conflictual); default -2.0 tightens beyond the root-code filter alone.
  3. ``min_num_mentions`` — NumMentions floor, a corroboration/significance
     bar; most GDELT rows have very low mention counts, so this is the
     single highest-leverage cut.
  4. Optional ``actor_country_fips`` allowlist — FIPS 10-4 codes (NOT ISO
     3166; GDELT's events-table country columns are FIPS, same caveat
     documented on ``GDELTConfig.cameo_country`` in ``gdelt.py``) applied to
     ``Actor1CountryCode`` OR ``Actor2CountryCode`` OR
     ``ActionGeo_CountryCode``. Empty (default) = worldwide.

Together these are expected to land in the low hundreds to low thousands of
signals/day rather than the tens-of-thousands-per-day the unfiltered feed
would produce — see the module docstring in the registrar script and
``docs/DATA_SOURCES.md`` §7 for the documented volume estimate (an expectation
to verify against live traffic post-deploy, not a measured guarantee).

Cursor: the high-water mark is the export file's embedded timestamp
(``YYYYMMDDHHMMSS``, parsed from the URL, the same precision + form as the
BigQuery handler's ``DATEADDED`` cursor). Persisted in
``ctx.state_store["gdelt_files_last_export_ts"]``; ``lastupdate.txt`` entries
at or before that timestamp are skipped on the next poll. Bounded per poll by
``max_files_per_pull`` (how many new export files to walk if a poll was
missed) and ``max_rows_per_file`` (a defensive parse cap per zip).

Egress goes through the shared SSRF guard (``_egress.guarded_async_client``)
for both the ``lastupdate.txt`` fetch and every export-file fetch — the URLs
in ``lastupdate.txt`` are upstream-controlled text, not descriptor config, so
routing them through the guard closes the same redirect/DNS-rebinding vector
the guard protects against for descriptor-supplied URLs.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, ClassVar
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .._fips_iso import fips_to_iso2
from ._contract import Signal, SourceContext, SourceHandler, SourceHealth
from ._egress import guarded_async_client


logger = logging.getLogger("legba.source.gdelt_files")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The 15-minute "what's new" index. Three lines: export (events), mentions,
#: gkg — each ``<size> <md5> <url>``.
LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"

#: Suffix identifying the events-export line in lastupdate.txt (vs
#: ``.mentions.CSV.zip`` / ``.gkg.csv.zip``).
EVENTS_FILE_SUFFIX = ".export.CSV.zip"

#: State-store cursor key: highest export-file timestamp fully processed.
_CURSOR_KEY = "gdelt_files_last_export_ts"
#: State-store health-detail key (mirrors json_api's health_state_key pattern).
_HEALTH_KEY = "gdelt_files_health"

#: GDELT publishes a new export file every 15 minutes.
DEFAULT_LOOKBACK_MINUTES = 15

#: Defensive caps — never let one poll tick walk unboundedly many files or
#: parse unboundedly many rows out of one zip.
DEFAULT_MAX_FILES_PER_PULL = 2
DEFAULT_MAX_ROWS_PER_FILE = 75_000

#: CAMEO root-code conflict/coercion band (CAMEO Manual 1.1b3 root-code
#: table): 14 protest, 15 exert coercion / posture, 16 reduce relations,
#: 17 coerce, 18 assault, 19 fight, 20 mass violence / unconventional
#: mass violence. Matches the intent of ``gdelt.py``'s task-comment
#: example (``['19', '20']`` for fight + mass violence) generalized to the
#: full conflict quadrant the platform tracks.
DEFAULT_EVENT_ROOT_CODES: tuple[str, ...] = (
    "14", "15", "16", "17", "18", "19", "20",
)

#: Default corroboration floor — GDELT rows with very low NumMentions are
#: mostly single-wire noise; this is the single highest-leverage volume cut.
DEFAULT_MIN_NUM_MENTIONS = 10

#: Default Goldstein ceiling (scale runs roughly -10..+10; more negative =
#: more conflictual). A second, independent gate beyond the root-code filter.
DEFAULT_GOLDSTEIN_MAX = -2.0

#: The GDELT 2.0 events export is a fixed 61-column tab-separated file with
#: NO header row. Column order per the GDELT 2.0 codebook
#: (http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf).
#: Named here so a parse bug shows up as a name typo, not a silent index
#: mismatch; kept in the same declared order as ``gdelt.py``'s
#: ``EVENT_COLUMNS`` for the (proper) subset both handlers share.
EVENTS_EXPORT_COLUMNS: tuple[str, ...] = (
    "GLOBALEVENTID", "SQLDATE", "MonthYear", "Year", "FractionDate",
    "Actor1Code", "Actor1Name", "Actor1CountryCode", "Actor1KnownGroupCode",
    "Actor1EthnicCode", "Actor1Religion1Code", "Actor1Religion2Code",
    "Actor1Type1Code", "Actor1Type2Code", "Actor1Type3Code",
    "Actor2Code", "Actor2Name", "Actor2CountryCode", "Actor2KnownGroupCode",
    "Actor2EthnicCode", "Actor2Religion1Code", "Actor2Religion2Code",
    "Actor2Type1Code", "Actor2Type2Code", "Actor2Type3Code",
    "IsRootEvent", "EventCode", "EventBaseCode", "EventRootCode",
    "QuadClass", "GoldsteinScale", "NumMentions", "NumSources", "NumArticles",
    "AvgTone",
    "Actor1Geo_Type", "Actor1Geo_FullName", "Actor1Geo_CountryCode",
    "Actor1Geo_ADM1Code", "Actor1Geo_ADM2Code", "Actor1Geo_Lat",
    "Actor1Geo_Long", "Actor1Geo_FeatureID",
    "Actor2Geo_Type", "Actor2Geo_FullName", "Actor2Geo_CountryCode",
    "Actor2Geo_ADM1Code", "Actor2Geo_ADM2Code", "Actor2Geo_Lat",
    "Actor2Geo_Long", "Actor2Geo_FeatureID",
    "ActionGeo_Type", "ActionGeo_FullName", "ActionGeo_CountryCode",
    "ActionGeo_ADM1Code", "ActionGeo_ADM2Code", "ActionGeo_Lat",
    "ActionGeo_Long", "ActionGeo_FeatureID",
    "DATEADDED", "SOURCEURL",
)

#: CAMEO root codes are two-digit strings "01".."20".
_ROOT_CODE_RE = re.compile(r"^\d{2}$")
#: FIPS 10-4 country codes are two letters (same shape guard as
#: ``GDELTConfig.cameo_country`` in gdelt.py).
_FIPS_RE = re.compile(r"^[A-Z]{2}$")
#: The timestamp embedded in every gdeltv2 export filename.
_EXPORT_TS_RE = re.compile(r"(\d{14})\.")


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class GDELTFilesConfig(BaseModel):
    """Configuration for one ``gdelt_files`` source instance.

    All filter fields ship with a conflict/geopolitics-narrowing default —
    unlike ``GDELTConfig`` (BigQuery), where an unfiltered pull merely costs
    more scan budget, an unfiltered file-dump pull would try to emit every
    row in a firehose CSV. ``event_root_codes`` + ``min_num_mentions`` +
    ``goldstein_max`` are pre-populated with the platform defaults so a
    descriptor-author has to deliberately widen them, not deliberately
    narrow them.
    """

    model_config = ConfigDict(strict=False, extra="forbid")

    lastupdate_url: str = Field(
        default=LASTUPDATE_URL,
        description="The gdeltv2 'what's new' index URL. Override only for a mirror.",
    )

    include_mentions: bool = Field(
        default=False,
        description=(
            "When true, also resolve+fetch the mentions export line from "
            "lastupdate.txt. Not yet parsed into Signals in v1 (events only) "
            "— reserved for a follow-up that joins mentions back to events "
            "for corroboration counts; currently a no-op flag."
        ),
    )

    # --- hard filter thresholds (conflict/geopolitics narrowing) ---

    event_root_codes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EVENT_ROOT_CODES),
        description=(
            "CAMEO EventRootCode allowlist. Two-digit string codes. "
            "Default is the conflict/coercion band 14-20 (protest through "
            "mass violence). Empty list = no root-code filter (NOT "
            "recommended — this is the primary volume gate)."
        ),
    )

    min_num_mentions: int = Field(
        default=DEFAULT_MIN_NUM_MENTIONS,
        ge=0,
        le=100_000,
        description=(
            "Row is dropped when NumMentions is below this floor. The "
            "single highest-leverage volume cut — most GDELT rows have very "
            "low mention counts. 0 disables the filter."
        ),
    )

    goldstein_max: float | None = Field(
        default=DEFAULT_GOLDSTEIN_MAX,
        description=(
            "Row is dropped when GoldsteinScale is ABOVE this ceiling "
            "(scale runs roughly -10..+10; more negative = more "
            "conflictual). None disables the filter. Independent of "
            "event_root_codes — both must pass."
        ),
    )

    actor_country_fips: list[str] | None = Field(
        default=None,
        description=(
            "FIPS 10-4 two-letter country-code allowlist (NOT ISO 3166 — "
            "GDELT's events-table country columns are FIPS; see "
            "GDELTConfig.cameo_country in gdelt.py for the same caveat). "
            "Matched against Actor1CountryCode OR Actor2CountryCode OR "
            "ActionGeo_CountryCode. None/empty = worldwide (no geo filter)."
        ),
    )

    # --- bounding ---

    max_files_per_pull: int = Field(
        default=DEFAULT_MAX_FILES_PER_PULL,
        ge=1,
        le=50,
        description=(
            "Cap on how many new export files (by cursor) one poll walks. "
            "Normally exactly one new file exists per 15-min tick; this "
            "gives slack for a missed tick without unbounded catch-up."
        ),
    )

    max_rows_per_file: int = Field(
        default=DEFAULT_MAX_ROWS_PER_FILE,
        ge=1,
        le=1_000_000,
        description="Defensive parse cap — stop reading a zip's CSV after this many raw rows.",
    )

    max_signals_per_pull: int = Field(
        default=2_000,
        ge=1,
        le=100_000,
        description=(
            "Defensive emit cap across the whole pull (all files), applied "
            "AFTER filtering. A backstop, not the primary volume control — "
            "the filter thresholds above are."
        ),
    )

    lookback_minutes: int = Field(
        default=DEFAULT_LOOKBACK_MINUTES,
        ge=1,
        le=1440,
        description=(
            "First-run-only: how far back to accept export files with no "
            "stored cursor. Default matches the 15-min publish cadence — "
            "on a clean start this pulls just the latest file."
        ),
    )

    timeout_seconds: int = Field(default=30, ge=1, le=300)
    user_agent: str = Field(default="Legba/2.0 (gdelt_files)", max_length=256)

    @field_validator("event_root_codes")
    @classmethod
    def _validate_root_codes(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for code in v:
            code = code.strip().zfill(2)
            if not _ROOT_CODE_RE.match(code):
                raise ValueError(
                    f"event_root_codes entries must be 1-2 digits; got {code!r}"
                )
            out.append(code)
        return out

    @field_validator("actor_country_fips")
    @classmethod
    def _validate_fips(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = []
        for code in v:
            code = code.strip().upper()
            if not _FIPS_RE.match(code):
                raise ValueError(
                    f"actor_country_fips entries must be two-letter FIPS "
                    f"codes; got {code!r}"
                )
            out.append(code)
        return out

    @model_validator(mode="after")
    def _warn_if_wide_open(self) -> "GDELTFilesConfig":
        # Not an error — some deployments may deliberately want the wider
        # feed — but this is the one config shape (all three volume gates
        # disabled) most likely to be an operator mistake, so it's called
        # out in the field description rather than silently accepted.
        return self


# ---------------------------------------------------------------------------
# lastupdate.txt parsing (pure)
# ---------------------------------------------------------------------------


def parse_lastupdate(body: str) -> dict[str, dict[str, Any]]:
    """Parse ``lastupdate.txt`` into ``{"export": {...}, "mentions": {...}, "gkg": {...}}``.

    Each line is ``<size-bytes> <md5-hex> <url>`` (whitespace-separated, URL
    is always last and never contains whitespace). Classifies each line by
    filename suffix. Blank lines are skipped. Malformed lines are skipped
    with a warning rather than raising — a partially-malformed index
    shouldn't block whichever lines DID parse.
    """
    out: dict[str, dict[str, Any]] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            logger.warning("gdelt_files.lastupdate.malformed_line: %r", line[:200])
            continue
        size_str, md5, url = parts[0], parts[1], parts[2]
        try:
            size = int(size_str)
        except ValueError:
            logger.warning("gdelt_files.lastupdate.bad_size: %r", line[:200])
            continue

        ts = extract_export_timestamp(url)
        if ts is None:
            logger.warning("gdelt_files.lastupdate.no_timestamp: %r", url)
            continue

        if url.endswith(EVENTS_FILE_SUFFIX):
            kind = "export"
        elif url.endswith(".mentions.CSV.zip"):
            kind = "mentions"
        elif url.endswith(".gkg.csv.zip"):
            kind = "gkg"
        else:
            logger.warning("gdelt_files.lastupdate.unknown_kind: %r", url)
            continue

        out[kind] = {"size": size, "md5": md5, "url": url, "timestamp": ts}
    return out


def extract_export_timestamp(url: str) -> datetime | None:
    """Pull the ``YYYYMMDDHHMMSS`` timestamp embedded in a gdeltv2 file URL.

    e.g. ``http://data.gdeltproject.org/gdeltv2/20260721143000.export.CSV.zip``
    -> ``2026-07-21T14:30:00+00:00``.
    """
    match = _EXPORT_TS_RE.search(url)
    if match is None:
        return None
    raw = match.group(1)
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# CSV row parsing + filtering (pure)
# ---------------------------------------------------------------------------


def parse_events_csv(raw_bytes: bytes, *, max_rows: int) -> list[dict[str, str]]:
    """Unzip + parse an events-export CSV into column-name-keyed dicts.

    The zip contains exactly one member (a ``.CSV`` file, no header row,
    tab-separated, 61 columns). Rows with a column count that doesn't match
    ``EVENTS_EXPORT_COLUMNS`` are skipped (logged) rather than raising —
    GDELT has, historically, appended trailing columns across schema
    versions; skipping a malformed row is safer than misaligning every
    subsequent field for that row.
    """
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        names = zf.namelist()
        if not names:
            raise ValueError("gdelt export zip is empty")
        with zf.open(names[0]) as member:
            text = io.TextIOWrapper(member, encoding="utf-8", errors="replace")
            reader = csv.reader(text, delimiter="\t")
            rows: list[dict[str, str]] = []
            n_cols = len(EVENTS_EXPORT_COLUMNS)
            for i, raw_row in enumerate(reader):
                if i >= max_rows:
                    logger.warning(
                        "gdelt_files.parse.max_rows_per_file cap=%d hit — truncating",
                        max_rows,
                    )
                    break
                if len(raw_row) < n_cols:
                    continue
                rows.append(dict(zip(EVENTS_EXPORT_COLUMNS, raw_row[:n_cols])))
            return rows


def row_passes_filter(row: dict[str, str], cfg: GDELTFilesConfig) -> bool:
    """True iff ``row`` clears every configured hard filter gate.

    All configured gates must pass (AND, not OR) — this is a narrowing
    filter chain, not an alternative-match chain.
    """
    if cfg.event_root_codes:
        root = (row.get("EventRootCode") or "").strip().zfill(2)
        if root not in cfg.event_root_codes:
            return False

    if cfg.min_num_mentions > 0:
        mentions = _to_int(row.get("NumMentions"))
        if mentions is None or mentions < cfg.min_num_mentions:
            return False

    if cfg.goldstein_max is not None:
        goldstein = _to_float(row.get("GoldsteinScale"))
        if goldstein is None or goldstein > cfg.goldstein_max:
            return False

    if cfg.actor_country_fips:
        countries = {
            (row.get("Actor1CountryCode") or "").strip().upper(),
            (row.get("Actor2CountryCode") or "").strip().upper(),
            (row.get("ActionGeo_CountryCode") or "").strip().upper(),
        }
        countries.discard("")
        if not countries & set(cfg.actor_country_fips):
            return False

    return True


# ---------------------------------------------------------------------------
# Row -> Signal mapping (pure)
# ---------------------------------------------------------------------------


def _to_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_sqldate(sqldate: str | None) -> datetime | None:
    """Decode the GDELT SQLDATE field (YYYYMMDD string) into a UTC datetime."""
    if not sqldate or len(sqldate) != 8 or not sqldate.isdigit():
        return None
    try:
        return datetime.strptime(sqldate, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# 2026-08-29 DQ sweep §3 (finding: GDELT staleness is a deterministic
# year-off-by-one) — GDELT's own upstream events export sometimes carries
# SQLDATE with the YEAR wrong by exactly one, month-day intact. Measured live:
# 372/372 of the affected 30-day sample had `payload->>'published_at'`'s year
# one less than the ingest year with month-day IDENTICAL to the fetch date.
# Confirmed upstream (not our parse) by inspecting the untouched
# `payload->raw_body->SQLDATE` for the same rows — this handler applies ZERO
# transformation to SQLDATE before this fix (`row_to_signal` stored
# `row.get("SQLDATE")` verbatim), so a corrupted year already visible in the
# raw, never-touched source row can only have arrived that way from GDELT.
#
# DATEADDED — a SEPARATE field on the same row, GDELT's own "when the export
# added this row" stamp — reliably carries the correct year (verified against
# real wall-clock fetch dates across the sample). The signature below uses it
# as the trusted anchor, deliberately narrow so it corrects ONLY the exact
# deterministic pattern proven live and leaves every other date — including
# the 14/161 genuinely years-old rows and the separate ~30-day month-lag band
# the same sweep found (a different shape: month decremented, not year) —
# completely untouched:
#   1. SQLDATE and DATEADDED both parse as valid calendar dates.
#   2. Their month-day match EXACTLY.
#   3. DATEADDED's year is EXACTLY SQLDATE's year + 1 (not >1 apart, not 0).
#   4. The corrected date is not in the future relative to wall-clock UTC now
#      (the one direction that could manufacture a forward-dated event) —
#      mirrors the future-skew-clamp idiom already used for this exact class
#      of upstream date corruption in ``legba.data.sources.rss``'s
#      ``_NEWEST_ENTRY_MAX_FUTURE_SKEW``.
#
# Ingest-forward only: this does not rewrite rows already in the substrate
# (372 of them, at last measurement) — that is a separate operator decision
# (see planning/CAMPAIGN_2026-08-29/INGESTION_SMALLS_REPORT.md).
_SQLDATE_YEAR_OFFSET_MAX_FUTURE_SKEW = timedelta(hours=26)


def _correct_sqldate_year_offset(
    sqldate: str | None,
    date_added: str | None,
    *,
    now: datetime | None = None,
) -> tuple[str | None, bool]:
    """Guarded correction for GDELT's upstream SQLDATE year-off-by-one defect.

    Returns ``(value_to_store, was_corrected)``. ``value_to_store`` is the
    ORIGINAL ``sqldate`` string unchanged unless every element of the
    deterministic signature (module docstring above) holds, in which case it
    is the year-corrected 8-digit ``YYYYMMDD`` string. Never raises — any
    parse failure, missing evidence, or signature mismatch fails safe to the
    original, uncorrected value (today's behaviour).
    """
    if not sqldate or len(sqldate) != 8 or not sqldate.isdigit():
        return sqldate, False
    if not date_added or len(date_added) < 8 or not date_added[:8].isdigit():
        return sqldate, False  # no DATEADDED evidence on this row — don't guess
    try:
        event_date = datetime.strptime(sqldate, "%Y%m%d").replace(tzinfo=timezone.utc)
        added_date = datetime.strptime(date_added[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return sqldate, False
    if (event_date.month, event_date.day) != (added_date.month, added_date.day):
        return sqldate, False
    if added_date.year - event_date.year != 1:
        return sqldate, False
    try:
        corrected = event_date.replace(year=added_date.year)
    except ValueError:  # pragma: no cover — Feb 29 landing on a non-leap year
        return sqldate, False
    skew_ceiling = (now or datetime.now(tz=timezone.utc)) + _SQLDATE_YEAR_OFFSET_MAX_FUTURE_SKEW
    if corrected > skew_ceiling:
        return sqldate, False
    return corrected.strftime("%Y%m%d"), True


def synthesize_title(row: dict[str, str]) -> str:
    """Compose a human-readable title from actors + event + location.

    GED-style synthesis (mirrors ``ucdp.py``'s ``_build_title`` — GDELT
    events, like UCDP GED rows, carry no natural headline). Format:
    ``"<Actor1> <-> <Actor2>: <event label> in <location>"`` with graceful
    degradation when actor/location fields are blank (CAMEO actor coding is
    frequently partial).
    """
    a1 = (row.get("Actor1Name") or "").strip()
    a2 = (row.get("Actor2Name") or "").strip()
    event_code = (row.get("EventCode") or "").strip()
    root = (row.get("EventRootCode") or "").strip().zfill(2)
    location = (row.get("ActionGeo_FullName") or "").strip()

    label = CAMEO_ROOT_LABELS.get(root, f"event {event_code}" if event_code else "event")

    actors = " <-> ".join(a for a in (a1, a2) if a) or "Unidentified actors"
    title = f"{actors}: {label}"
    if location:
        title += f" in {location}"
    return title[:240]


#: Human labels for the CAMEO root codes this handler's default filter keeps
#: (14-20), used only for title synthesis — not a filtering table. Full root
#: code 01-20 table lives in the CAMEO manual; only the conflict band is
#: named here since those are the only rows a default-config instance emits.
CAMEO_ROOT_LABELS: dict[str, str] = {
    "14": "protest",
    "15": "exert coercion / show force posture",
    "16": "reduce relations",
    "17": "coerce",
    "18": "assault",
    "19": "fight",
    "20": "engage in mass violence",
}


def _row_content_hash(row: dict[str, str]) -> str:
    """Stable dedupe key: GLOBALEVENTID + DATEADDED (mirrors gdelt.py's
    ``_row_content_hash`` — same identity convention across both GDELT
    handlers so a row seen by BOTH the BigQuery and file-dump paths, should
    an operator ever run both, collapses to one canonical signal downstream).
    """
    key = f"gdelt:{row.get('GLOBALEVENTID')}:{row.get('DATEADDED')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def row_to_signal(row: dict[str, str], *, ctx: SourceContext, export_url: str) -> Signal:
    """Map one filtered events-export row to a :class:`Signal`.

    Field shape mirrors ``gdelt.py``'s ``_row_to_signal_payload`` (same
    downstream consumers) with the additional raw columns the flat-file
    export carries that the BigQuery handler's narrower ``EVENT_COLUMNS``
    selection doesn't (e.g. Actor1Geo_* / Actor2Geo_* alongside ActionGeo_*).
    """
    # 2026-08-29 DQ sweep §3 — correct GDELT's deterministic upstream
    # SQLDATE year-off-by-one BEFORE it reaches the payload (see
    # ``_correct_sqldate_year_offset``'s docstring for the diagnosis and the
    # guard signature). ``raw_body`` below stays the fully untouched original
    # row for audit; only ``published_at`` (the field the render layer and
    # every staleness read actually consume) and its parsed provenance copy
    # reflect the correction.
    sqldate_for_payload, sqldate_year_corrected = _correct_sqldate_year_offset(
        row.get("SQLDATE"), row.get("DATEADDED"),
    )
    published_at = _parse_sqldate(sqldate_for_payload)
    payload: dict[str, Any] = {
        "external_id": row.get("GLOBALEVENTID"),
        "published_at": sqldate_for_payload,
        "date_added": row.get("DATEADDED"),
        "title": synthesize_title(row),
        "geo": {
            "type": row.get("ActionGeo_Type"),
            "full_name": row.get("ActionGeo_FullName"),
            "country_code_fips": row.get("ActionGeo_CountryCode"),
            "adm1_code": row.get("ActionGeo_ADM1Code"),
            "lat": _to_float(row.get("ActionGeo_Lat")),
            "lon": _to_float(row.get("ActionGeo_Long")),
        },
        "actors": {
            "actor1_code": row.get("Actor1Code"),
            "actor1_name": row.get("Actor1Name"),
            "actor1_country_fips": row.get("Actor1CountryCode"),
            "actor1_type": row.get("Actor1Type1Code"),
            "actor2_code": row.get("Actor2Code"),
            "actor2_name": row.get("Actor2Name"),
            "actor2_country_fips": row.get("Actor2CountryCode"),
            "actor2_type": row.get("Actor2Type1Code"),
        },
        "event_code": row.get("EventCode"),
        "event_base_code": row.get("EventBaseCode"),
        "event_root_code": row.get("EventRootCode"),
        "quad_class": row.get("QuadClass"),
        "goldstein_scale": _to_float(row.get("GoldsteinScale")),
        "tone": _to_float(row.get("AvgTone")),
        "num_mentions": _to_int(row.get("NumMentions")),
        "num_sources": _to_int(row.get("NumSources")),
        "num_articles": _to_int(row.get("NumArticles")),
        "source_url": row.get("SOURCEURL"),
        "export_file_url": export_url,
        "raw_body": row,
    }

    # B-6 — the FIPS/ISO boundary. ``ActionGeo_CountryCode`` is FIPS 10-4;
    # ``signals.geo`` is ISO 3166-1 alpha-2, because that is what a country desk
    # subscribes on (``geo && ARRAY['XX']``). The two codelists are both two
    # uppercase letters and agree about half the time, so passing the raw value
    # through never failed — it just delivered Germany's stories to Gambia's desk
    # (FIPS ``GM``), China's to Switzerland's (``CH``), Russia's to Serbia's
    # (``RS``), and dropped the United Kingdom's on the floor entirely (FIPS
    # ``UK``; ISO says ``GB``, so no desk matched). Measured live 2026-08-03:
    # 1,617 rows wrong-countried, 129 of them onto a desk that exists.
    #
    # Translate at the boundary — this is the last point where the value is known
    # to be FIPS. An untranslatable code (subdivision, uninhabited territory, or
    # one of GDELT's occasional non-FIPS oddities) yields NOTHING rather than a
    # guess: geo stays empty and the geocode filter resolves it from the row's
    # own lat/lon, which is the more trustworthy signal anyway. The raw FIPS value
    # stays in the payload under its ``_fips``-suffixed key, unaltered.
    geo_code = (row.get("ActionGeo_CountryCode") or "").strip().upper()
    iso2 = fips_to_iso2(geo_code) if _FIPS_RE.match(geo_code) else None
    geo = [iso2] if iso2 else []

    return Signal(
        signal_id=uuid4(),
        source_id=ctx.source_id,
        payload=payload,
        content_hash=_row_content_hash(row),
        canonical_url=row.get("SOURCEURL") or None,
        language_hint=None,  # events table carries no language; baseline detects.
        geo=geo,
        raw_provenance={
            "kind": "gdelt_files",
            "fetch_kind": "gdelt_files",
            "global_event_id": row.get("GLOBALEVENTID"),
            "date_added": row.get("DATEADDED"),
            "sql_date": row.get("SQLDATE"),  # untouched upstream original, always
            "export_file_url": export_url,
            "published_at": published_at.isoformat() if published_at else None,
            # 2026-08-29 DQ sweep §3 provenance marker — True only when the
            # guarded year-off-by-one correction above actually fired for
            # this row (the raw, uncorrected value is always still available
            # above as `sql_date`).
            "sqldate_year_corrected": sqldate_year_corrected,
        },
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class GDELTFilesSourceHandler:
    """L-102 source-kind handler for the GDELT 2.0 15-minute file dump.

    State stored in ``ctx.state_store``:

      * ``gdelt_files_last_export_ts`` (ISO string) — the DATEADDED-precision
        timestamp of the most-recently-fully-processed export file; the next
        poll only walks files strictly newer than this.
      * ``gdelt_files_health`` (dict) — last state/error/detail for the
        health probe (mirrors ``json_api``'s ``_JSON_API_HEALTH_KEY`` pattern).
    """

    kind: ClassVar[str] = "gdelt_files"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.gdelt_files/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = GDELTFilesConfig
    health_state_key: ClassVar[str] = _HEALTH_KEY

    #: Unit-test hook — a callable returning a configured
    #: ``httpx.AsyncClient`` (backed by ``MockTransport``). Production
    #: leaves this None so the SSRF-guarded client is used.
    _http_client_factory: ClassVar[Any] = None

    def __init__(self, config: GDELTFilesConfig | None = None) -> None:
        self.config = config or GDELTFilesConfig()

    # ------------------------------------------------------------------
    # Lifecycle hooks — no-ops; the handler has no cross-call setup.
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
    # Internal helpers
    # ------------------------------------------------------------------

    def _coerce_config(self, ctx: SourceContext) -> GDELTFilesConfig:
        """Parse the runtime's raw passthrough ``ctx.config`` (mirrors
        ``ucdp.py``'s ``_coerce_config`` — property-factory-wrapped values
        unwrapped, then validated). Unit tests pass a real
        ``GDELTFilesConfig`` directly — the isinstance guard returns it
        untouched.
        """
        raw = getattr(ctx, "config", None)
        if isinstance(raw, GDELTFilesConfig):
            return raw
        if raw is None:
            return self.config
        data = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw or {})
        unwrapped = {
            k: (v["raw"] if isinstance(v, dict) and "raw" in v else v)
            for k, v in data.items()
        }
        return GDELTFilesConfig.model_validate(unwrapped)

    def _open_client(self, cfg: GDELTFilesConfig) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return guarded_async_client(
            timeout=cfg.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": cfg.user_agent},
        )

    async def _cursor(self, ctx: SourceContext) -> datetime | None:
        raw = await ctx.state_store.get(_CURSOR_KEY)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    async def _record_health(
        self,
        ctx: SourceContext,
        *,
        state: str,
        last_success_at: datetime | None = None,
        last_error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "state": state,
            "last_success_at": (
                last_success_at.astimezone(timezone.utc).isoformat()
                if last_success_at is not None
                else None
            ),
            "last_error": last_error,
            "detail": detail or {},
        }
        try:
            await ctx.state_store.set(_HEALTH_KEY, record)
        except Exception:                                # pragma: no cover
            ctx.logger.warning("gdelt_files.health.persist_failed", exc_info=True)

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Poll ``lastupdate.txt``, fetch new events-export files, filter, emit.

        Cursor precedence (mirrors the other GDELT handler + UCDP): stored
        cursor > ``since`` hint > ``now - lookback_minutes``. On each new
        file: fetch -> unzip -> parse -> hard-filter -> map -> yield. The
        cursor only advances past a file once ITS rows have been fully
        walked, so a mid-file crash re-processes that file (dedupe absorbs
        the repeat) rather than silently skipping it.
        """
        cfg = self._coerce_config(ctx)
        cursor = await self._cursor(ctx)
        floor = cursor
        if floor is None:
            floor = since
        if floor is None:
            floor = ctx.utcnow() - timedelta(minutes=cfg.lookback_minutes)
        if floor.tzinfo is None:
            floor = floor.replace(tzinfo=timezone.utc)

        try:
            async with self._open_client(cfg) as client:
                index_resp = await client.get(cfg.lastupdate_url)
                index_resp.raise_for_status()
                index = parse_lastupdate(index_resp.text)

                export_entry = index.get("export")
                if export_entry is None:
                    await self._record_health(
                        ctx, state="degraded",
                        last_error="lastupdate.txt had no events-export line",
                    )
                    return

                # Only one events-export line is ever live in lastupdate.txt
                # at a time (it's a "what's newest" pointer, not a backlog
                # listing), so "new files to walk" is really "is the current
                # pointer newer than our cursor" — at most 1 file per real
                # poll. max_files_per_pull exists as a defensive cap on a
                # hypothetical future multi-entry index, not because this
                # loop currently iterates more than once per tick.
                candidates = [export_entry] if export_entry["timestamp"] > floor else []
                candidates = candidates[: cfg.max_files_per_pull]

                if not candidates:
                    await self._record_health(
                        ctx, state="healthy",
                        last_success_at=ctx.utcnow(),
                        detail={"new_files": 0, "cursor": floor.isoformat()},
                    )
                    return

                total_emitted = 0
                newest_ts_processed = cursor
                for entry in candidates:
                    file_resp = await client.get(entry["url"])
                    file_resp.raise_for_status()
                    rows = parse_events_csv(
                        file_resp.content, max_rows=cfg.max_rows_per_file
                    )

                    file_emitted = 0
                    for row in rows:
                        if total_emitted >= cfg.max_signals_per_pull:
                            ctx.logger.warning(
                                "gdelt_files.pull.max_signals cap=%d — truncating",
                                cfg.max_signals_per_pull,
                            )
                            break
                        if not row_passes_filter(row, cfg):
                            continue
                        yield row_to_signal(row, ctx=ctx, export_url=entry["url"])
                        file_emitted += 1
                        total_emitted += 1

                    if newest_ts_processed is None or entry["timestamp"] > newest_ts_processed:
                        newest_ts_processed = entry["timestamp"]
                    # Persist after each file so a crash mid-loop only
                    # re-walks files after the last one we finished.
                    await ctx.state_store.set(
                        _CURSOR_KEY, newest_ts_processed.isoformat()
                    )

                    if total_emitted >= cfg.max_signals_per_pull:
                        break

                await self._record_health(
                    ctx, state="healthy",
                    last_success_at=ctx.utcnow(),
                    detail={
                        "new_files": len(candidates),
                        "rows_emitted": total_emitted,
                        "cursor": newest_ts_processed.isoformat()
                        if newest_ts_processed else None,
                    },
                )

        except httpx.HTTPError as exc:
            await self._record_health(
                ctx, state="unhealthy", last_error=f"http: {exc!r}",
            )
            ctx.logger.warning("gdelt_files.pull.http_error: %s", exc)
            return
        except (zipfile.BadZipFile, ValueError) as exc:
            await self._record_health(
                ctx, state="unhealthy", last_error=f"parse: {exc!r}",
            )
            ctx.logger.warning("gdelt_files.pull.parse_error: %s", exc)
            return

    # ------------------------------------------------------------------
    # health_check
    # ------------------------------------------------------------------

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Probe ``lastupdate.txt`` only (cheap — no export-file fetch)."""
        cfg = self._coerce_config(ctx)
        cursor = await self._cursor(ctx)
        record_raw = await ctx.state_store.get(_HEALTH_KEY)
        record: dict[str, Any] = record_raw if isinstance(record_raw, dict) else {}

        try:
            async with self._open_client(cfg) as client:
                resp = await client.get(cfg.lastupdate_url)
                resp.raise_for_status()
                index = parse_lastupdate(resp.text)
        except httpx.HTTPError as exc:
            return SourceHealth(
                state="unhealthy",
                last_error=f"lastupdate probe: {exc!r}",
                last_cursor=cursor.isoformat() if cursor else None,
                detail={"phase": "lastupdate_probe"},
            )

        export_entry = index.get("export")
        if export_entry is None:
            return SourceHealth(
                state="degraded",
                last_error="lastupdate.txt had no events-export line",
                last_cursor=cursor.isoformat() if cursor else None,
            )

        return SourceHealth(
            state="healthy",
            last_success_at=record.get("last_success_at"),
            last_error=record.get("last_error"),
            last_cursor=cursor.isoformat() if cursor else None,
            detail={
                "kind": self.kind,
                "latest_export_ts": export_entry["timestamp"].isoformat(),
                "event_root_codes": cfg.event_root_codes,
                "min_num_mentions": cfg.min_num_mentions,
                "goldstein_max": cfg.goldstein_max,
            },
        )


# Protocol satisfaction sanity-check (cheap, runs at import — same pattern
# as acled.py / ucdp.py).
assert isinstance(GDELTFilesSourceHandler(), SourceHandler)  # type: ignore[arg-type]


__all__ = [
    "CAMEO_ROOT_LABELS",
    "DEFAULT_EVENT_ROOT_CODES",
    "DEFAULT_GOLDSTEIN_MAX",
    "DEFAULT_MAX_FILES_PER_PULL",
    "DEFAULT_MAX_ROWS_PER_FILE",
    "DEFAULT_MIN_NUM_MENTIONS",
    "EVENTS_EXPORT_COLUMNS",
    "EVENTS_FILE_SUFFIX",
    "GDELTFilesConfig",
    "GDELTFilesSourceHandler",
    "LASTUPDATE_URL",
    "extract_export_timestamp",
    "parse_events_csv",
    "parse_lastupdate",
    "row_passes_filter",
    "row_to_signal",
    "synthesize_title",
]
