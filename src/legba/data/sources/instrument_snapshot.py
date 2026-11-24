# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``instrument_snapshot`` — a published instrument, fetched, hashed, diffed.

A sanctions list or an export-control supplement changing is a CONSTRAINED
event: there is an authoritative source, a previous instrument, an amended
one, the exact entries that moved, and dates. This kind turns that into a
receipt a compliance reader can check:

  1. **Fetch** the instrument's bytes (XML / CSV / HTML / PDF — the kind never
     needs to understand a format to keep it) over the shared SSRF egress
     guard, streamed under :data:`INSTRUMENT_SNAPSHOT_MAX_BYTES`. A conditional
     GET (``If-None-Match`` / ``If-Modified-Since``) is sent when the last
     response carried a validator, so an unchanged 29 MB list costs one 304.
  2. **Hash** them (sha256 — the receipt anchor).
  3. **Archive** them content-addressed on the evidence archive
     (:mod:`legba.data.archive` — ``{LEGBA_ARCHIVE_ROOT}/{sha[:2]}/{sha}``,
     ``cas:sha256/<hex>``). Every snapshot is kept, so the previous instrument
     and the amended one are BOTH on the archive whenever a change is
     reported; the full diff is archived beside them as JSON.
  4. **Compare** with the last snapshot this source archived (the pointer is
     the source's own state row, ``instrument_snapshot_state``).
  5. **Emit ONE dated change signal** per changed instrument, carrying the two
     hashes, the diff summary (added / removed / changed entries keyed on the
     list's own identifier for a structured list; a unified text diff for
     text), the entry count before and after and the instrument's OWN
     published date when it states one. An unchanged instrument emits NO
     signal: it writes a ``checked, unchanged`` RECEIPT instead (state row
     ``instrument_snapshot_receipts``), naming the typed-absence kind
     :data:`UNCHANGED_ABSENCE_KIND` that the read side registers next.

Licence. Every binding declares ``license_class`` (the SourceScope LIC-2
vocabulary) in its config, and it is stamped on the signal's payload. A class
that forbids retaining bytes (:data:`RETENTION_FORBIDDEN_LICENSE_CLASSES` — the
same three the evidence archiver refuses) is refused at CONFIG time: a kind
whose whole method is archiving cannot run against an instrument it may not
keep, so it never fetches one.

Delivery is at-least-once. The last-good pointer advances only AFTER the
consumer has taken the change signal, so a write that fails (or a poll cut at
its budget) re-diffs the same pair next poll; the signal's ``content_hash`` is
the hash of the (previous, current) pair, so the runtime's exact-hash collapse
absorbs a repeat.

The differs themselves are :mod:`legba.data.sources.instrument_diff` (pure,
stdlib-only).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, ClassVar
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..archive import archive_root as configured_archive_root
from ...shared.url_redaction import strip_query
from ..archive_store import ColdTierError, EvidenceStore
from ..schemas.source import LicenseClass
from ._contract import Signal, SourceContext, SourceHandler, SourceHealth
from ._egress import guarded_async_client
from .instrument_diff import (
    DifferKind,
    InstrumentParseError,
    ParsedInstrument,
    attribute_path_error,
    cap_diff,
    diff_instruments,
    parse_csv_records,
    parse_text,
    parse_xml_records,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: THE ARCHIVE SIZE CAP FOR THIS KIND ONLY. The evidence archiver's per-object
#: cap (``_DEFAULT_MAX_OBJECT_BYTES``, 20 MB) is sized for a cited web page;
#: the instruments this kind keeps are larger (the brief's measures: OFAC SDN
#: 29 MB, the UK Sanctions List 22 MB, the EU consolidated list 26 MB). Raised
#: HERE, for this kind, with headroom for list growth — the global cap is
#: untouched. A binding may lower it (``max_bytes``), never raise it.
INSTRUMENT_SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024

#: Licence classes that forbid retaining the bytes — kept equal to the
#: evidence archiver's ``FORBID_RETENTION_CLASSES`` (a test pins the two). A
#: source module cannot import the analyst plane, so the set is restated here
#: and the pin is what stops it drifting.
RETENTION_FORBIDDEN_LICENSE_CLASSES: frozenset[str] = frozenset(
    {"anti_ai_walled", "tos_restrictive", "personal_use_only"}
)

#: The typed-absence kind an unchanged receipt names. NOT yet in
#: ``registry/absence_api.ABSENCE_KINDS`` — registering it (and reading these
#: receipts) is the next wave's; the receipt is written now so that reader has
#: history to read on its first day.
UNCHANGED_ABSENCE_KIND = "instrument_unchanged"

#: State keys (scoped per source by the runtime's FilterStateStore).
_STATE_KEY = "instrument_snapshot_state"
_RECEIPTS_KEY = "instrument_snapshot_receipts"
_HEALTH_KEY = "instrument_snapshot_health"

#: Receipts kept on the state row (newest first). The archive keeps every
#: snapshot; this is the recent check history a reader shows.
RECEIPTS_KEPT = 60

#: Receipt outcomes — a closed vocabulary.
OUTCOME_FIRST = "first_snapshot"
OUTCOME_CHANGED = "changed"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_FETCH_FAILED = "fetch_failed"
OUTCOME_TOO_LARGE = "too_large"
OUTCOME_PARSE_FAILED = "parse_failed"
OUTCOME_ARCHIVE_UNAVAILABLE = "archive_unavailable"
RECEIPT_OUTCOMES: tuple[str, ...] = (
    OUTCOME_FIRST, OUTCOME_CHANGED, OUTCOME_UNCHANGED, OUTCOME_FETCH_FAILED,
    OUTCOME_TOO_LARGE, OUTCOME_PARSE_FAILED, OUTCOME_ARCHIVE_UNAVAILABLE,
)

#: The change signal's MIME type: a JSON change record, not the instrument.
CHANGE_MIME_TYPE = "application/json"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class InstrumentSnapshotConfig(BaseModel):
    """One published instrument and how to read it."""

    model_config = ConfigDict(strict=False, extra="forbid")

    instrument_name: str = Field(..., min_length=1, max_length=200)
    url: str = Field(..., max_length=2048)
    license_class: LicenseClass
    differ: DifferKind = "bytes"

    # xml_records
    record_tag: str | None = None
    key_path: str | None = None
    label_paths: list[str] = Field(default_factory=list)
    summary_paths: list[str] = Field(default_factory=list)
    published_date_path: str | None = None
    published_date_format: str | None = None
    declared_count_path: str | None = None

    # csv_records
    key_column: str | None = None
    csv_has_header: bool = True
    label_columns: list[str] = Field(default_factory=list)
    summary_columns: list[str] = Field(default_factory=list)

    encoding: str = "utf-8"
    max_bytes: int = Field(default=INSTRUMENT_SNAPSHOT_MAX_BYTES, ge=1, le=INSTRUMENT_SNAPSHOT_MAX_BYTES)
    max_diff_entries: int = Field(default=500, ge=1, le=10_000)
    max_text_diff_chars: int = Field(default=200_000, ge=1_000, le=2_000_000)
    emit_first_snapshot: bool = True
    conditional_get: bool = True
    timeout_seconds: int = Field(default=180, ge=5, le=230)
    user_agent: str = Field(default="Legba/2.0 (instrument_snapshot)", max_length=256)

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if not v.startswith(("https://", "http://")):
            raise ValueError("url must be http(s)")
        return v

    @field_validator("license_class")
    @classmethod
    def _retainable(cls, v: str) -> str:
        if v in RETENTION_FORBIDDEN_LICENSE_CLASSES:
            raise ValueError(
                f"license_class {v!r} forbids retaining bytes; instrument_snapshot "
                "archives every snapshot, so it cannot bind this instrument"
            )
        return v

    @model_validator(mode="after")
    def _differ_has_its_keys(self) -> "InstrumentSnapshotConfig":
        if self.differ == "xml_records" and not (self.record_tag and self.key_path):
            raise ValueError("differ xml_records needs record_tag and key_path")
        # I-5: key/label/summary paths may end in an '@attribute' segment; the
        # root-level date/count paths stay element-text only.
        for p in [self.key_path, *self.label_paths, *self.summary_paths]:
            err = attribute_path_error(p)
            if err:
                raise ValueError(err)
        for p in (self.published_date_path, self.declared_count_path):
            if p and "@" in p:
                raise ValueError(f"path {p!r}: published_date_path/declared_count_path read element text")
        if self.differ == "csv_records" and not self.key_column:
            raise ValueError("differ csv_records needs key_column")
        return self


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InstrumentTooLargeError(Exception):
    """The response exceeded this binding's size cap."""


class InstrumentArchiveUnavailable(RuntimeError):
    """The evidence archive root is missing or not writable — this kind keeps
    every snapshot, so it refuses activation (and every pull) without one."""

    reason = "instrument_archive_unavailable"


# ---------------------------------------------------------------------------
# Archive (content-addressed, the evidence archive's address format)
# ---------------------------------------------------------------------------


def usable_archive_root(root: Path | None = None) -> Path | None:
    """``root`` (default: ``LEGBA_ARCHIVE_ROOT``), created if needed; ``None``
    when it cannot be written."""
    path = root if root is not None else configured_archive_root()
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.W_OK):
            return None
        return path
    except OSError:
        return None


def store_object(root: Path, body: bytes) -> tuple[str, str, bool]:
    """Content-address ``body`` under ``root``: ``(sha256, object_ref,
    already_present)``. The shared archive store (temp file + atomic rename,
    at-rest codec per :mod:`legba.data.archive_codec`); an existing object is
    never rewritten (same hash, same bytes)."""
    res = EvidenceStore(root).put(body)
    return res.sha256, res.object_ref, res.existed


def load_object(root: Path, sha256_hex: str) -> bytes | None:
    """The archived ORIGINAL bytes for ``sha256_hex``, decoded and re-verified
    against the hash; ``None`` when absent or corrupted (never bytes that do
    not match). An object retired to the cold tier reads back transparently
    (``EvidenceStore.from_env``: the configured cold backend + the local cold
    index); a cold object that cannot be read raises
    :class:`~legba.data.archive_store.ColdTierError`."""
    return EvidenceStore.from_env(root).get(sha256_hex)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


async def fetch_instrument(
    client: httpx.AsyncClient, url: str, *, max_bytes: int, headers: dict[str, str],
) -> tuple[int, bytes, httpx.Headers, str]:
    """``(status, body, response headers, final url)``, streamed under the cap.

    A 304 returns an empty body. The declared Content-Length is checked first
    and the stream is enforced too, so a lying header cannot blow the cap."""
    async with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code == 304:
            return 304, b"", resp.headers, str(resp.url)
        resp.raise_for_status()
        declared = resp.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise InstrumentTooLargeError(f"declared {declared} bytes > cap {max_bytes}")
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise InstrumentTooLargeError(f"stream exceeded cap {max_bytes}")
            chunks.append(chunk)
        return resp.status_code, b"".join(chunks), resp.headers, str(resp.url)


# ---------------------------------------------------------------------------
# Parse + signal shaping
# ---------------------------------------------------------------------------


def parse_instrument(body: bytes, cfg: InstrumentSnapshotConfig) -> ParsedInstrument:
    """Read ``body`` the way the binding says (raises InstrumentParseError)."""
    if cfg.differ == "xml_records":
        return parse_xml_records(
            body,
            record_tag=cfg.record_tag or "",
            key_path=cfg.key_path or "",
            label_paths=cfg.label_paths,
            summary_paths=cfg.summary_paths,
            published_date_path=cfg.published_date_path,
            published_date_format=cfg.published_date_format,
            declared_count_path=cfg.declared_count_path,
        )
    if cfg.differ == "csv_records":
        return parse_csv_records(
            body,
            key_column=cfg.key_column or "",
            has_header=cfg.csv_has_header,
            label_columns=cfg.label_columns,
            summary_columns=cfg.summary_columns,
            encoding=cfg.encoding,
        )
    if cfg.differ == "text":
        return parse_text(body, encoding=cfg.encoding)
    return ParsedInstrument(differ="bytes")


def redact_url(url: str) -> str:
    """``url`` without its query or fragment. The OFAC download redirects to a
    PRESIGNED object-store URL whose query carries a temporary security token;
    a signal row must never persist it. Scheme, host and path stay — they name
    the published object."""
    return strip_query(url)


def _diff_pair(prev_body: bytes, current: ParsedInstrument, cfg: InstrumentSnapshotConfig) -> dict[str, Any]:
    """Parse the previous snapshot and diff it against the current one (run
    off the event loop — a 30 MB list takes seconds of CPU)."""
    return diff_instruments(parse_instrument(prev_body, cfg), current)


def _snapshot_view(
    *, sha: str, ref: str, size: int, fetched_at: str, parsed: ParsedInstrument | None,
) -> dict[str, Any]:
    return {
        "sha256": sha,
        "object_ref": ref,
        "size_bytes": size,
        "fetched_at": fetched_at,
        "entry_count": parsed.entry_count if parsed else None,
        "declared_count": parsed.declared_count if parsed else None,
        "published_date": parsed.published_date if parsed else None,
    }


def _title(cfg: InstrumentSnapshotConfig, change_kind: str, diff: dict[str, Any] | None,
           published: str | None) -> str:
    when = f" (published {published})" if published else ""
    if change_kind == OUTCOME_FIRST:
        return f"{cfg.instrument_name}: first snapshot on record{when}"
    counts = (diff or {}).get("counts") or {}
    if (diff or {}).get("kind") == "records":
        return (
            f"{cfg.instrument_name}: {counts.get('added', 0)} added, "
            f"{counts.get('removed', 0)} removed, {counts.get('changed', 0)} changed{when}"
        )
    if (diff or {}).get("kind") == "text":
        return (
            f"{cfg.instrument_name}: text changed, +{counts.get('lines_added', 0)} / "
            f"-{counts.get('lines_removed', 0)} lines{when}"
        )
    return f"{cfg.instrument_name}: instrument changed (new sha256){when}"


def _text(diff: dict[str, Any] | None, *, limit: int = 50) -> str:
    """A plain-text rendering of the (capped) diff for readers and the corpus."""
    if not diff:
        return ""
    if diff.get("kind") == "text":
        return str(diff.get("unified_diff") or "")[:20_000]
    if diff.get("kind") != "records":
        return str(diff.get("note") or diff.get("reason") or "")
    lines: list[str] = []
    for sign, name in (("+", "added"), ("-", "removed"), ("~", "changed")):
        for e in (diff.get(name) or [])[:limit]:
            extra = f" [{', '.join(e['fields_changed'])}]" if e.get("fields_changed") else ""
            lines.append(f"{sign} {e.get('key')} {e.get('label', '')}{extra}".rstrip())
    return "\n".join(lines)


def build_change_signal(
    ctx: SourceContext,
    cfg: InstrumentSnapshotConfig,
    *,
    change_kind: str,
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    diff: dict[str, Any] | None,
    diff_object_ref: str | None,
    fetched_at: datetime,
    provenance: dict[str, Any],
) -> Signal:
    """The ONE dated change signal for one changed instrument."""
    capped = (
        cap_diff(diff, max_entries=cfg.max_diff_entries, max_text_chars=cfg.max_text_diff_chars)
        if diff else None
    )
    if capped is not None:
        capped["full_diff_object_ref"] = diff_object_ref
    published = current.get("published_date")
    pair = f"{(previous or {}).get('sha256') or ''}->{current['sha256']}"
    return Signal(
        signal_id=uuid4(),
        source_id=ctx.source_id,
        fetched_at=fetched_at,
        modality="structured",
        mime_type=CHANGE_MIME_TYPE,
        retention_class="evidence_hold",
        object_ref=current["object_ref"],
        content_hash=hashlib.sha256(pair.encode("ascii")).hexdigest(),
        payload={
            "title": _title(cfg, change_kind, capped, published),
            "text": _text(capped),
            "change_kind": change_kind,
            "instrument": {
                "name": cfg.instrument_name,
                "url": cfg.url,
                "differ": cfg.differ,
            },
            "license_class": cfg.license_class,
            "published_at": published,
            "published_date": published,
            "previous": previous,
            "current": current,
            "previous_sha256": (previous or {}).get("sha256"),
            "current_sha256": current["sha256"],
            "entry_count_before": (previous or {}).get("entry_count"),
            "entry_count_after": current.get("entry_count"),
            "diff": capped,
        },
        raw_provenance={"kind": "instrument_snapshot", "fetch_kind": "instrument_snapshot", **provenance},
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class InstrumentSnapshotSourceHandler:
    """L-102 source-kind handler: one published instrument per descriptor.

    State in ``ctx.state_store``:

      * ``instrument_snapshot_state`` — the last-good snapshot pointer
        (sha256, object_ref, size, fetched_at, entry_count, published_date,
        HTTP validators, last_changed_at);
      * ``instrument_snapshot_receipts`` — the newest :data:`RECEIPTS_KEPT`
        check receipts, newest first (outcome from :data:`RECEIPT_OUTCOMES`);
      * ``instrument_snapshot_health`` — the health record the poll-outcome
        writer reads (``health_state_key``).
    """

    kind: ClassVar[str] = "instrument_snapshot"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.instrument_snapshot/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = InstrumentSnapshotConfig
    health_state_key: ClassVar[str] = _HEALTH_KEY
    #: R5: a 30 MB download plus a structured diff does not fit the generic
    #: 30 s poll budget; advertise the actor's ceiling (it clamps to 240 s).
    poll_budget_seconds: ClassVar[float] = 240.0
    max_entries_per_poll: ClassVar[int] = 1

    #: Unit-test hook (an ``httpx.AsyncClient`` factory over MockTransport).
    _http_client_factory: ClassVar[Any] = None

    def __init__(
        self,
        config: InstrumentSnapshotConfig | None = None,
        *,
        archive_root: Path | None = None,
    ) -> None:
        self.config = config
        self._archive_root_override = archive_root

    # -- lifecycle -------------------------------------------------------

    async def activation_check(self, ctx: SourceContext) -> None:
        """Refuse activation without a writable archive (the kind's method)."""
        if usable_archive_root(self._archive_root_override) is None:
            raise InstrumentArchiveUnavailable(
                "instrument_snapshot needs a writable evidence archive "
                "(LEGBA_ARCHIVE_ROOT / the legba_archive volume)"
            )

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

    # -- helpers ---------------------------------------------------------

    def _coerce_config(self, ctx: SourceContext) -> InstrumentSnapshotConfig:
        raw = getattr(ctx, "config", None)
        if isinstance(raw, InstrumentSnapshotConfig):
            return raw
        if self.config is not None:
            return self.config
        data = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw or {})
        unwrapped = {
            k: (v["raw"] if isinstance(v, dict) and "raw" in v else v)
            for k, v in data.items()
        }
        return InstrumentSnapshotConfig.model_validate(unwrapped)

    def _open_client(self, cfg: InstrumentSnapshotConfig) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return guarded_async_client(
            timeout=cfg.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": cfg.user_agent},
        )

    async def _record_health(
        self, ctx: SourceContext, *, state: str, last_error: str | None = None,
        detail: dict[str, Any] | None = None, success_at: datetime | None = None,
    ) -> None:
        record = {
            "state": state,
            "last_success_at": success_at.astimezone(timezone.utc).isoformat() if success_at else None,
            "last_error": last_error,
            "detail": detail or {},
        }
        try:
            await ctx.state_store.set(_HEALTH_KEY, record)
        except Exception:                                # pragma: no cover
            ctx.logger.warning("instrument_snapshot.health.persist_failed", exc_info=True)

    async def _append_receipt(self, ctx: SourceContext, receipt: dict[str, Any]) -> None:
        prior = await ctx.state_store.get(_RECEIPTS_KEY)
        items = prior if isinstance(prior, list) else []
        await ctx.state_store.set(_RECEIPTS_KEY, [receipt, *items][:RECEIPTS_KEPT])

    def _receipt(
        self, ctx: SourceContext, cfg: InstrumentSnapshotConfig, outcome: str,
        checked_at: datetime, **fields: Any,
    ) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "kind": "instrument_snapshot_receipt",
            "outcome": outcome,
            "source_id": ctx.source_id,
            "instrument": cfg.instrument_name,
            "url": cfg.url,
            "checked_at": checked_at.astimezone(timezone.utc).isoformat(),
        }
        if outcome == OUTCOME_UNCHANGED:
            receipt["typed_absence_kind"] = UNCHANGED_ABSENCE_KIND
        receipt.update(fields)
        return receipt

    async def _fail(
        self, ctx: SourceContext, cfg: InstrumentSnapshotConfig, outcome: str,
        now: datetime, error: str, **fields: Any,
    ) -> None:
        await self._append_receipt(ctx, self._receipt(ctx, cfg, outcome, now, error=error, **fields))
        await self._record_health(ctx, state="unhealthy", last_error=f"{outcome}: {error}",
                                  detail={"outcome": outcome})
        ctx.logger.warning("instrument_snapshot.%s source=%s err=%s", outcome, ctx.source_id, error)

    # -- pull ------------------------------------------------------------

    async def pull(
        self, ctx: SourceContext, since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Fetch → hash → archive → compare → (change signal | unchanged receipt).

        ``since`` is ignored: the comparison basis is the last archived
        snapshot, not a time window.
        """
        cfg = self._coerce_config(ctx)
        now = ctx.utcnow()
        root = usable_archive_root(self._archive_root_override)
        if root is None:
            await self._fail(ctx, cfg, OUTCOME_ARCHIVE_UNAVAILABLE, now,
                             "archive root missing or not writable")
            return
        raw_state = await ctx.state_store.get(_STATE_KEY)
        state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
        prev_sha = state.get("sha256") if isinstance(state.get("sha256"), str) else None

        req_headers: dict[str, str] = {}
        if cfg.conditional_get and prev_sha:
            if state.get("etag"):
                req_headers["If-None-Match"] = str(state["etag"])
            if state.get("last_modified"):
                req_headers["If-Modified-Since"] = str(state["last_modified"])
        try:
            async with self._open_client(cfg) as client:
                status, body, headers, final_url = await fetch_instrument(
                    client, cfg.url, max_bytes=cfg.max_bytes, headers=req_headers,
                )
        except InstrumentTooLargeError as exc:
            await self._fail(ctx, cfg, OUTCOME_TOO_LARGE, now, str(exc), max_bytes=cfg.max_bytes)
            return
        except httpx.HTTPError as exc:
            await self._fail(ctx, cfg, OUTCOME_FETCH_FAILED, now, repr(exc))
            return

        validators = {"etag": headers.get("etag"), "last_modified": headers.get("last-modified")}
        unchanged_detail = {
            "outcome": OUTCOME_UNCHANGED,
            "sha256": prev_sha,
            "newest_entry_ts": state.get("last_changed_at"),
        }
        if status == 304:
            if not prev_sha:  # a 304 to an unconditional GET is a broken upstream
                await self._fail(ctx, cfg, OUTCOME_FETCH_FAILED, now, "304 with no prior snapshot")
                return
            await self._append_receipt(ctx, self._receipt(
                ctx, cfg, OUTCOME_UNCHANGED, now, basis="http_not_modified", http_status=304,
                sha256=prev_sha, object_ref=state.get("object_ref"),
                published_date=state.get("published_date"), entry_count=state.get("entry_count"),
            ))
            await ctx.state_store.set(_STATE_KEY, {**state, "last_checked_at": now.isoformat()})
            await self._record_health(ctx, state="healthy", success_at=now, detail=unchanged_detail)
            return

        sha, ref, already = store_object(root, body)
        provenance = {
            "url": cfg.url, "final_url": redact_url(final_url), "http_status": status,
            "content_type": headers.get("content-type"), "sha256": sha,
            "previous_sha256": prev_sha, **validators,
        }
        if sha == prev_sha:
            await self._append_receipt(ctx, self._receipt(
                ctx, cfg, OUTCOME_UNCHANGED, now, basis="sha256_equal", http_status=status,
                sha256=sha, object_ref=ref, size_bytes=len(body),
                published_date=state.get("published_date"), entry_count=state.get("entry_count"),
            ))
            await ctx.state_store.set(_STATE_KEY, {**state, **validators, "last_checked_at": now.isoformat()})
            await self._record_health(ctx, state="healthy", success_at=now, detail=unchanged_detail)
            return

        try:
            current_parsed = await asyncio.to_thread(parse_instrument, body, cfg)
        except InstrumentParseError as exc:
            # The bytes ARE archived (evidence of what was served); the
            # last-good pointer does not move and nothing is emitted.
            await self._fail(ctx, cfg, OUTCOME_PARSE_FAILED, now, str(exc),
                             sha256=sha, object_ref=ref, size_bytes=len(body))
            return

        fetched_iso = now.astimezone(timezone.utc).isoformat()
        current = _snapshot_view(sha=sha, ref=ref, size=len(body), fetched_at=fetched_iso,
                                 parsed=current_parsed)
        current["archive_already_present"] = already
        if current_parsed.duplicate_keys or current_parsed.records_without_key:
            current["duplicate_keys"] = current_parsed.duplicate_keys
            current["records_without_key"] = current_parsed.records_without_key

        previous: dict[str, Any] | None = None
        diff: dict[str, Any] | None = None
        diff_ref: str | None = None
        if prev_sha is None:
            change_kind = OUTCOME_FIRST
        else:
            change_kind = OUTCOME_CHANGED
            previous = {
                "sha256": prev_sha,
                "object_ref": state.get("object_ref"),
                "size_bytes": state.get("size_bytes"),
                "fetched_at": state.get("fetched_at"),
                "entry_count": state.get("entry_count"),
                "declared_count": state.get("declared_count"),
                "published_date": state.get("published_date"),
            }
            try:
                prev_body = load_object(root, prev_sha)
                cold_error = None
            except ColdTierError as exc:  # recorded cold, unreadable now: say so
                prev_body, cold_error = None, str(exc)
            if prev_body is None:
                diff = {"kind": "unavailable", "counts": {},
                        "reason": ("previous snapshot is on the cold tier and could not be "
                                   f"read: {cold_error}"[:500]) if cold_error
                        else "previous snapshot is not on the archive"}
            else:
                try:
                    diff = await asyncio.to_thread(_diff_pair, prev_body, current_parsed, cfg)
                except InstrumentParseError as exc:
                    diff = {"kind": "unavailable", "counts": {},
                            "reason": f"previous snapshot unreadable under this binding: {exc}"}
            diff_blob = json.dumps(
                {"previous_sha256": prev_sha, "current_sha256": sha, "diff": diff},
                ensure_ascii=False, sort_keys=True,
            ).encode("utf-8")
            _, diff_ref, _ = store_object(root, diff_blob)

        emitted_id: str | None = None
        if change_kind == OUTCOME_CHANGED or cfg.emit_first_snapshot:
            signal = build_change_signal(
                ctx, cfg, change_kind=change_kind, previous=previous, current=current,
                diff=diff, diff_object_ref=diff_ref, fetched_at=now, provenance=provenance,
            )
            emitted_id = str(signal.signal_id)
            yield signal
            # Resumed ⇒ the consumer took it. Only now does the pointer move.

        await ctx.state_store.set(_STATE_KEY, {
            "sha256": sha,
            "object_ref": ref,
            "size_bytes": len(body),
            "fetched_at": fetched_iso,
            "entry_count": current["entry_count"],
            "declared_count": current["declared_count"],
            "published_date": current["published_date"],
            "last_checked_at": fetched_iso,
            "last_changed_at": fetched_iso,
            **validators,
        })
        await self._append_receipt(ctx, self._receipt(
            ctx, cfg, change_kind, now, sha256=sha, object_ref=ref, size_bytes=len(body),
            previous_sha256=prev_sha, diff_object_ref=diff_ref,
            counts=(diff or {}).get("counts"), entry_count=current["entry_count"],
            published_date=current["published_date"], signal_id=emitted_id, http_status=status,
        ))
        await self._record_health(ctx, state="healthy", success_at=now, detail={
            "outcome": change_kind, "sha256": sha, "newest_entry_ts": fetched_iso,
        })

    # -- health ----------------------------------------------------------

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """The recorded health — no probe: re-fetching a 30 MB instrument to
        answer a health question would cost more than the poll it reports on."""
        record_raw = await ctx.state_store.get(_HEALTH_KEY)
        record = record_raw if isinstance(record_raw, dict) else {}
        state_raw = await ctx.state_store.get(_STATE_KEY)
        state = state_raw if isinstance(state_raw, dict) else {}
        return SourceHealth(
            state=str(record.get("state") or "healthy"),
            last_success_at=record.get("last_success_at"),
            last_error=record.get("last_error"),
            last_cursor=state.get("sha256"),
            detail={"kind": self.kind, **(record.get("detail") or {}),
                    "archive_writable": usable_archive_root(self._archive_root_override) is not None},
        )


assert isinstance(InstrumentSnapshotSourceHandler(), SourceHandler)  # type: ignore[arg-type]


__all__ = [
    "CHANGE_MIME_TYPE",
    "INSTRUMENT_SNAPSHOT_MAX_BYTES",
    "InstrumentArchiveUnavailable",
    "InstrumentSnapshotConfig",
    "InstrumentSnapshotSourceHandler",
    "InstrumentTooLargeError",
    "RECEIPTS_KEPT",
    "RECEIPT_OUTCOMES",
    "RETENTION_FORBIDDEN_LICENSE_CLASSES",
    "UNCHANGED_ABSENCE_KIND",
    "build_change_signal",
    "fetch_instrument",
    "load_object",
    "parse_instrument",
    "redact_url",
    "store_object",
    "usable_archive_root",
]
