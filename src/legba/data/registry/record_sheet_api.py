# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/record/*`` — the Record sheet's receipt, text and integrity reads.

The console's Record sheet reads a statement as a chain: the statement → its
row → the evidence → the RECEIPT → later versions → the integrity line. The
row and the evidence come from ``/lineage/{kind}/{id}`` at depth 1. These
three routes serve the rest (the reads live in the stdlib leaf
``legba.data.record_sheet_reads``):

* ``/record/receipts?kind=signal&ids=a,b,…`` (at most 25 ids) — each record's
  fetch versions (address, URL, protocol, status, content type, size, sha256,
  first/last seen, where the bytes are held), its source and the source's
  class, the versions of its page address with the drift class between them,
  and, for a record with no receipt, why (``before_receipts_start`` /
  ``source_kind_without_receipts`` / ``not_linked``).
* ``/record/version/{version_id}/text?max_chars=`` — the text a version stored,
  bounded (default 20,000 characters, at most 100,000), with ``truncated`` and
  a re-hash of the whole stored text against its stored sha256. ACCESS: the
  display policy's rule — a READER gets no publisher text beyond a cited span
  (``display_policy`` module docstring), so a reader's answer withholds the
  text and says so (``withheld_reason: reader_display_policy``); a version
  whose recorded licence is a retention refusal (``anti_ai_walled``,
  ``tos_restrictive``, ``personal_use_only`` — the evidence archiver's set) is
  withheld from everyone (``license_forbids``). The text rides ``full_text``,
  one of ``display_policy.PUBLISHER_TEXT_FIELDS``, so the registry's
  ``DisplayPolicyMiddleware`` withholds it from a reader a second time even if
  this check were ever bypassed.
* ``/record/signal/{signal_id}/item?max_chars=`` (B-1) — the item AS
  RECEIVED: the slice of the stored response the signal is (a feed item, a
  GDELT row, a Telegram message) or the archived page's text, read from the
  held bytes (``byte_path.signal_item_text``) under the version-text route's
  withholding rules, with its evidence label (``received bytes`` /
  ``archived later`` / ``no bytes kept``). The text rides ``full_text`` too.
* ``/record/finding/{finding_id}/support`` (DM-A) — what each sentence of a
  finding rests on: the citation rows (sentence → record → the receipt version
  it was read from) when the finding has them, else today's path (the
  citations in its document, signals labelled by ``byte_path``), saying which
  (``basis``). Leaf: ``legba.data.citation_rows_reads``.
* ``/record/assertions`` (DM-C) — the held family, LIGHT by default
  (``fields=summary``: class, type, subject, current status, the first 240
  characters of the current text; ``fields=full`` the whole text and slots),
  filtered by subject key, class and status, keyset-paged by ``cursor``; and
  ``/record/assertions/{assertion_id}`` — one assertion's versions, its
  hash-chained status log re-verified, the cache against its rebuild, and its
  support with bytes labels. Leaf: ``legba.data.held_record_reads``. Nothing
  feeds these tables but the desk shadow behind ``LEGBA_HELD_RECORD_SHADOW``
  (off); no console surface reads them.
* ``/record/integrity?run_id=`` — the producing run's receipt re-hashed, the
  signed checkpoint that names it (or the first later one), and the first
  daily external anchor after it, each ``verified`` / ``covered`` /
  ``not_covered`` / ``failed``.

Read-only. Defensive: the receipt tables absent → ``available: false`` at HTTP
200. Registry-slim: stdlib + fastapi + pydantic + ``.api`` +
``display_class`` + the stdlib-only reads leaf (a guard test probes it with the
slim image's blocked modules).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from ..citation_rows_reads import finding_support
from ..display_policy import READER
from ..held_record_reads import CLASSES, DEFAULT_LIMIT, MAX_LIMIT, STATUSES, assertion_detail, list_assertions
from ..record_sheet_reads import (
    DEFAULT_TEXT_CHARS,
    MAX_IDS,
    MAX_TEXT_CHARS,
    item_text,
    record_receipts,
    run_integrity,
    version_text,
)
from .api import RegistryAPIDeps, require_bearer
from .display_class import request_display_class

logger = logging.getLogger(__name__)

RECORD_SHEET_ROUTE_VERSION = "2026-11/u10"


class RecordReceiptsOut(BaseModel):
    version: str = RECORD_SHEET_ROUTE_VERSION
    available: bool = False
    kind: str = "signal"
    receipts_start: Optional[str] = None
    records: list[dict[str, Any]] = Field(default_factory=list)
    versions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pages: dict[str, dict[str, Any]] = Field(default_factory=dict)
    changes: list[dict[str, Any]] = Field(default_factory=list)


class VersionTextOut(BaseModel):
    version: str = RECORD_SHEET_ROUTE_VERSION
    available: bool = False
    version_id: int
    found: bool = False
    role: Optional[str] = None
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    text_sha256: Optional[str] = None
    license_classes: list[str] = Field(default_factory=list)
    shown: bool = False
    withheld_reason: Optional[str] = None
    chars_total: Optional[int] = None
    chars_shown: int = 0
    truncated: bool = False
    text_hash_ok: Optional[bool] = None
    max_chars: int = DEFAULT_TEXT_CHARS
    full_text: Optional[str] = None


class ItemTextOut(BaseModel):
    version: str = RECORD_SHEET_ROUTE_VERSION
    available: bool = False
    signal_id: str
    found: bool = False
    evidence: Optional[dict[str, Any]] = None
    version_id: Optional[int] = None
    basis: Optional[str] = None
    exact: Optional[bool] = None
    license_classes: list[str] = Field(default_factory=list)
    shown: bool = False
    withheld_reason: Optional[str] = None
    chars_total: Optional[int] = None
    chars_shown: int = 0
    truncated: bool = False
    text_sha256: Optional[str] = None
    item_sha256: Optional[str] = None
    version_sha256: Optional[str] = None
    max_chars: int = DEFAULT_TEXT_CHARS
    full_text: Optional[str] = None


class RunIntegrityOut(BaseModel):
    version: str = RECORD_SHEET_ROUTE_VERSION
    available: bool = False
    run_id: str
    found: bool = False
    unit: Optional[str] = None
    run_ended_at: Optional[str] = None
    receipt: dict[str, Any] = Field(default_factory=lambda: {"state": "not_covered"})
    checkpoint: dict[str, Any] = Field(default_factory=lambda: {"state": "not_covered"})
    anchor: dict[str, Any] = Field(default_factory=lambda: {"state": "not_covered"})


class FindingSupportOut(BaseModel):
    version: str = RECORD_SHEET_ROUTE_VERSION
    available: bool = False
    finding_id: str
    found: bool = False
    #: ``support_rows`` (DM-A rows) / ``citations_json`` (today's path) / ``none``.
    basis: str = "none"
    receipt: Optional[dict[str, Any]] = None
    sentences: list[dict[str, Any]] = Field(default_factory=list)
    supports: list[dict[str, Any]] = Field(default_factory=list)
    unresolved: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False


class AssertionListOut(BaseModel):
    version: str = RECORD_SHEET_ROUTE_VERSION
    available: bool = False
    weight: str = "summary"
    items: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: Optional[str] = None


class AssertionDetailOut(BaseModel):
    version: str = RECORD_SHEET_ROUTE_VERSION
    available: bool = False
    assertion_id: str
    found: bool = False
    assertion: Optional[dict[str, Any]] = None
    versions: list[dict[str, Any]] = Field(default_factory=list)
    status_log: list[dict[str, Any]] = Field(default_factory=list)
    status: Optional[dict[str, Any]] = None
    chain: Optional[dict[str, Any]] = None
    rests_on: list[dict[str, Any]] = Field(default_factory=list)
    restated_or_used_by: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False


_CLASS_RE = "^(" + "|".join(CLASSES) + ")$"
_STATUS_RE = "^(" + "|".join(STATUSES) + ")$"


def build_record_sheet_router(deps: RegistryAPIDeps) -> APIRouter:
    """Read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["evidence"])

    def _pool(request: Request) -> Any:
        return getattr(request.app.state, "registry_deps", deps).descriptor_registry.pg

    async def _read(request: Request, fn: Any, *args: Any, **kw: Any) -> Any:
        try:
            async with _pool(request).acquire() as conn:
                return await fn(conn, *args, **kw)
        except Exception as exc:  # noqa: BLE001 — degrade, HTTP 200
            logger.info("v3.record.unavailable fn=%s err_class=%s",
                        getattr(fn, "__name__", "?"), type(exc).__name__)
            return None

    @router.get("/record/receipts", response_model=RecordReceiptsOut)
    async def record_receipts_route(
        request: Request,
        kind: str = Query(default="signal", pattern="^(signal|observation)$"),
        ids: str = Query(..., min_length=1, max_length=MAX_IDS * 40),
        principal: str = Depends(require_bearer),
    ) -> RecordReceiptsOut:
        wanted = [i.strip() for i in ids.split(",") if i.strip()]
        if len(wanted) > MAX_IDS:
            raise HTTPException(status_code=422, detail=f"at most {MAX_IDS} ids")
        body = await _read(request, record_receipts, kind, wanted)
        if body is None:
            return RecordReceiptsOut(kind=kind)
        return RecordReceiptsOut(available=True, kind=kind, **body)

    @router.get("/record/version/{version_id}/text", response_model=VersionTextOut)
    async def record_version_text_route(
        request: Request,
        version_id: int = Path(..., ge=1),
        max_chars: int = Query(default=DEFAULT_TEXT_CHARS, ge=1, le=MAX_TEXT_CHARS),
        principal: str = Depends(require_bearer),
    ) -> VersionTextOut:
        cls = request_display_class(request)
        body = await _read(request, version_text, version_id, max_chars=max_chars,
                           reader=cls == READER, display_class=cls)
        if body is None:
            return VersionTextOut(version_id=version_id, max_chars=max_chars)
        return VersionTextOut(available=True, **body)

    @router.get("/record/signal/{signal_id}/item", response_model=ItemTextOut)
    async def record_item_text_route(
        request: Request,
        signal_id: str = Path(..., min_length=1, max_length=64),
        max_chars: int = Query(default=DEFAULT_TEXT_CHARS, ge=1, le=MAX_TEXT_CHARS),
        principal: str = Depends(require_bearer),
    ) -> ItemTextOut:
        cls = request_display_class(request)
        body = await _read(request, item_text, signal_id, max_chars=max_chars,
                           reader=cls == READER, display_class=cls)
        if body is None:
            return ItemTextOut(signal_id=signal_id, max_chars=max_chars)
        return ItemTextOut(available=True, **body)

    @router.get("/record/finding/{finding_id}/support", response_model=FindingSupportOut)
    async def record_finding_support_route(
        request: Request,
        finding_id: str = Path(..., min_length=1, max_length=64),
        principal: str = Depends(require_bearer),
    ) -> FindingSupportOut:
        body = await _read(request, finding_support, finding_id)
        if body is None:
            return FindingSupportOut(finding_id=finding_id)
        return FindingSupportOut(available=True, **body)

    @router.get("/record/assertions", response_model=AssertionListOut)
    async def record_assertions_route(
        request: Request,
        subject: Optional[str] = Query(default=None, min_length=1, max_length=300),
        assertion_class: Optional[str] = Query(default=None, alias="class", pattern=_CLASS_RE),
        status: Optional[str] = Query(default=None, pattern=_STATUS_RE),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        cursor: Optional[str] = Query(default=None, max_length=120),
        fields: str = Query(default="summary", pattern="^(summary|full)$"),
        principal: str = Depends(require_bearer),
    ) -> AssertionListOut:
        body = await _read(request, list_assertions, subject=subject, class_=assertion_class, status=status,
                           limit=limit, cursor=cursor, full=fields == "full")
        if body is None:
            return AssertionListOut(weight=fields)
        return AssertionListOut(available=True, **body)

    @router.get("/record/assertions/{assertion_id}", response_model=AssertionDetailOut)
    async def record_assertion_route(
        request: Request,
        assertion_id: str = Path(..., pattern="^[0-9a-fA-F-]{36}$"),
        principal: str = Depends(require_bearer),
    ) -> AssertionDetailOut:
        body = await _read(request, assertion_detail, assertion_id)
        if body is None:
            return AssertionDetailOut(assertion_id=assertion_id)
        return AssertionDetailOut(available=True, assertion_id=assertion_id, **body)

    @router.get("/record/integrity", response_model=RunIntegrityOut)
    async def record_integrity_route(
        request: Request,
        run_id: str = Query(..., min_length=1, max_length=64),
        principal: str = Depends(require_bearer),
    ) -> RunIntegrityOut:
        body = await _read(request, run_integrity, run_id)
        if body is None:
            return RunIntegrityOut(run_id=run_id)
        return RunIntegrityOut(available=True, **body)

    return router
