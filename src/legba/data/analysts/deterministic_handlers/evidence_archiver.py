# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``evidence_archiver`` sub-handler — NATIVE cited-evidence archival (P2-1).

Program §A3 (planning/PROGRAM_RECOMMENDATIONS_2026-07-24.md): the receipt
chain today terminates in a ``canonical_url`` — a link that rots. This sweep
archives the ORIGINAL evidence behind CITED signals (not the firehose) so the
chain terminates in OUR verifiable copy:

  * **Selection — cited-only.** Signals referenced by the ``derived_from`` of
    VERIFIED findings (``kind='finding'``, not superseded, latest faithfulness
    critique exists AND ``LEAST(confidence, overall_score) >= verify_floor`` —
    the alert_trigger_scan / since_api verified bar) whose ``object_ref IS
    NULL`` and ``canonical_url`` is non-empty. Uncited signals are NEVER
    archived by this handler (bounded, moat-serving; the per-source depth
    lane is a separate concern).
  * **Fetch.** Original bytes over the SSRF egress guard
    (:func:`legba.data.sources._egress.guarded_async_client` — same guard as
    every source fetcher; an egress-blocked URL is terminal, it can never
    become fetchable). Streaming with a hard size cap; per-host politeness
    delay; bounded fetch budget per run; honest counters for every skip class.
  * **Store — content-addressed.** Bytes land at
    ``{LEGBA_ARCHIVE_ROOT}/{sha256[:2]}/{sha256}`` (write-to-temp + atomic
    rename; an existing object is a dedup hit, never rewritten). The recorded
    ``object_ref`` is the RELATIVE content address ``cas:sha256/<hex>`` — the
    root can move (or become MinIO/SeaweedFS later) without rewriting rows,
    and every read surface can derive ``archived`` + ``archive_sha256`` from
    the existing ``signals.object_ref`` column alone. The sha256 of the
    original bytes IS the receipt anchor: anyone holding our copy can re-hash
    it against the stamped value.
  * **Record.** Upsert into the ``evidence_archive`` sidecar (migration 0104
    — status / attempts / license verdict / media leg; see the migration
    header for the sidecar-vs-columns rationale) AND stamp the signal row in
    ONE update: ``object_ref`` (the cas address), ``retention_class`` upgraded
    to ``evidence_hold`` (the signals_retention purge already exempts it — an
    archived citation must never be TTL-purged out from under its archive),
    plus the corpus dirty-marker (below) when text extraction succeeded.
  * **Text extraction (bonus, S-10 rider).** Trafilatura main-text extraction
    over HTML/text objects → ``payload.archived_text``. The BYTES are the
    archive; extraction failure only means no corpus upgrade. On success the
    signal is re-queued for the OpenSearch corpus via the corpus_indexer
    DIRTY-MARKER CONTRACT (``SET indexed_at = NULL`` AND ``updated_at =
    now()`` in the SAME UPDATE — both load-bearing, see
    :mod:`.corpus_indexer`), so the archived FULL text replaces the thin
    teaser doc in ``legba_signals_corpus`` within a tick. This handler never
    touches OpenSearch directly — the index plane stays owned by
    corpus_indexer.
  * **Media leg.** ``media_ref`` bytes (when present and http/https) are
    fetched + stored the same way — NO processing (the process_media plane
    backfills extraction later). A media failure never fails the row; the
    canonical_url bytes are the primary archive.

**P2-2 LICENSE GATE (posture documented here, on purpose).** Each candidate's
``license_class`` is resolved from the signal (``payload.license_class`` — the
LIC-2 SourceScope→signal stamp — falling back to the manual-batch
``raw_provenance`` license, via :func:`legba.data.opensearch.
signal_license_class`). Sources whose class FORBIDS retention
(:data:`FORBID_RETENTION_CLASSES`: ``anti_ai_walled`` — never fetch, per the
LIC-1 ledger §E.4 enforcement hooks; ``tos_restrictive`` — reviewed-
restrictive terms; ``personal_use_only``) are SKIPPED with an honest
``skipped_license`` counter and a recorded ``skipped_license`` sidecar row —
never silently. The DEFAULT posture for unknown/unset license is **ARCHIVE**:
this is an open-web news quotation-for-evidence product, the LIC-1 ledger
found zero active sources with an anti-LLM EULA, and the evaluated class is
recorded on every row precisely so a future policy flip (e.g. fail-closed on
``unknown``) can re-evaluate mechanically instead of by memory.

**RETENTION HONESTY.** Archived objects are evidence — this handler NEVER
deletes them, and nothing else does either (the sidecar has no TTL; archived
signals are upgraded to ``evidence_hold``, which signals_retention exempts).
The future retention interplay — ``media_ref_expires_at`` sweeps, object-store
GC, operator-gated erasure on a license policy flip — is a declared seam
(docs/SEAMS.md), not built here.

Idempotency: a successful archive stamps ``signals.object_ref`` → the row
leaves the selection predicate forever. Failures are retried up to
``max_attempts`` (attempt-counted in the sidecar); ``skipped_license`` /
``skipped_size`` rows are recorded once and excluded from re-selection so the
budget is never re-burned (a policy/cap change re-evaluates them explicitly).
An already-present CAS object (same bytes archived via another signal) is a
dedup hit: counted, stamped, not rewritten.

Output ``data`` keys (the cadence receipt the operator reads):
    examined            int — candidate rows pulled this run
    archived            int — signals whose original bytes are now archived
    already_present     int — of ``archived``, CAS dedup hits (bytes existed)
    media_archived      int — media_ref objects stored (the media leg)
    media_failed        int — media_ref fetches that failed (row still archived)
    text_extracted      int — archived objects whose main text reached payload
    text_extract_failed int — HTML/text objects Trafilatura could not extract
    skipped_license     int — P2-2 gate refusals (recorded, never silent)
    skipped_size        int — objects over the size cap (recorded)
    fetch_failed        int — fetch/store failures this run (attempt-counted)
    egress_blocked      int — of ``fetch_failed``, SSRF-guard refusals (terminal)
    bytes_stored        int — NEW bytes written to the archive this run
    deadline_stop       int — 1 when the run stopped at the soft deadline
    skipped_no_root     int — 1 when the archive root was absent/unwritable
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from ...archive import (
    ARCHIVE_ROOT_ENV,
    CAS_PREFIX,
    DEFAULT_ARCHIVE_ROOT,
    cas_object_ref,
    cas_path,
    sha256_from_object_ref,
)
from ...opensearch import signal_license_class
from ...provenance.models import FindingPayload
from ...sources._egress import EgressBlockedError, guarded_async_client
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "evidence_archiver"

#: P2-2 — license classes that FORBID retention of the original bytes (the
#: LIC-1 ledger §E.4 vocabulary). ``anti_ai_walled`` = never fetch (hard
#: denial); ``tos_restrictive`` = reviewed-restrictive publisher terms;
#: ``personal_use_only`` = personal-use grants. Everything else — including
#: unknown/unset (recorded as NULL) — ARCHIVES under the open-web
#: quotation-for-evidence posture (module docstring). Overridable per-run via
#: ``options.forbid_license_classes`` for a policy flip without a code change.
FORBID_RETENTION_CLASSES: frozenset[str] = frozenset(
    {"anti_ai_walled", "tos_restrictive", "personal_use_only"}
)

_USER_AGENT = "legba-evidence-archiver/0.1"

# Defaults (all overridable via descriptor options).
_DEFAULT_FETCH_BUDGET = 200          # candidate signals per run
_DEFAULT_WINDOW_HOURS = 720          # finding-recency window for the citation join
_DEFAULT_VERIFY_FLOOR = 0.50         # the house verified bar (floor=0.50)
_DEFAULT_MAX_OBJECT_BYTES = 20 * 1024 * 1024   # 20MB size cap per object
_DEFAULT_PER_HOST_DELAY_S = 2.0      # politeness delay between same-host fetches
_DEFAULT_MAX_ATTEMPTS = 3            # failed-fetch retry cap (across runs)
_DEFAULT_MAX_TEXT_CHARS = 200_000    # extracted-text cap stored into payload
_DEFAULT_TIMEOUT_S = 30.0            # per-request timeout
_DEFAULT_RUN_DEADLINE_S = 1200.0     # soft per-run wall-clock stop (20 min)

#: Content-type prefixes Trafilatura extraction is attempted for.
_TEXTUAL_CONTENT_TYPES = ("text/html", "application/xhtml", "text/plain", "application/xml", "text/xml")


# ---------------------------------------------------------------------------
# Selection — signals cited by VERIFIED findings, not yet archived
# ---------------------------------------------------------------------------
#
# The verified bar mirrors alert_trigger_scan._VERIFIED_FINDINGS_SQL / the
# since_api: the LATEST faithfulness critique is pinned in a lateral, the
# floor applied over LEAST(confidence, overall_score). derived_from is the
# universal lineage array — the unnest joined against signals(id) naturally
# drops non-signal lineage members. The evidence_archive anti-join excludes
# terminal rows (archived / skipped_* / attempt-exhausted failures) so every
# run's budget goes to NEW work; signals.object_ref IS NULL is the primary
# idempotency gate (stamped on success → the row leaves this scan forever).
_SELECT_CANDIDATES_SQL = """
    WITH cited AS (
        SELECT DISTINCT d.sid
          FROM analyst_outputs f
          JOIN LATERAL (
              SELECT (cr.data->>'overall_score')::real AS faithfulness_score
                FROM analyst_outputs cr
               WHERE cr.kind = 'critique'
                 AND cr.data->>'analyzed_output_id' = f.id::text
                 AND cr.data->>'overall_score' IS NOT NULL
                 AND cr.title LIKE 'Faithfulness verify%'
               ORDER BY cr.produced_at DESC, cr.id DESC
               LIMIT 1
          ) v ON TRUE
          CROSS JOIN LATERAL unnest(f.derived_from) AS d(sid)
         WHERE f.kind = 'finding'
           AND f.superseded_by IS NULL
           AND f.produced_at > now() - make_interval(hours => $1)
           AND LEAST(f.confidence, v.faithfulness_score) >= $2
    )
    SELECT s.id, s.source_id, s.canonical_url, s.media_ref, s.payload,
           s.raw_provenance, s.retention_class,
           COALESCE(ea.attempts, 0) AS prior_attempts
      FROM signals s
      JOIN cited c ON c.sid = s.id
      LEFT JOIN evidence_archive ea ON ea.signal_id = s.id
     WHERE s.object_ref IS NULL
       AND s.canonical_url IS NOT NULL
       AND s.canonical_url <> ''
       AND (ea.signal_id IS NULL
            OR (ea.status = 'failed' AND ea.attempts < $3))
     ORDER BY s.fetched_at DESC
     LIMIT $4
"""

# Sidecar upsert — ONE statement for every terminal/attempt outcome. jsonb-free
# and additive; `attempts` accumulates across runs, `archived_at` only set on
# success, `created_at` preserved on conflict.
_UPSERT_ARCHIVE_SQL = """
    INSERT INTO evidence_archive (
        signal_id, status, object_ref, sha256, size_bytes, content_type,
        fetched_url, media_object_ref, media_sha256, media_size_bytes,
        license_class, text_extracted, attempts, last_error, archived_at,
        updated_at
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
        CASE WHEN $2 = 'archived' THEN now() ELSE NULL END, now()
    )
    ON CONFLICT (signal_id) DO UPDATE SET
        status           = EXCLUDED.status,
        object_ref       = EXCLUDED.object_ref,
        sha256           = EXCLUDED.sha256,
        size_bytes       = EXCLUDED.size_bytes,
        content_type     = EXCLUDED.content_type,
        fetched_url      = EXCLUDED.fetched_url,
        media_object_ref = EXCLUDED.media_object_ref,
        media_sha256     = EXCLUDED.media_sha256,
        media_size_bytes = EXCLUDED.media_size_bytes,
        license_class    = EXCLUDED.license_class,
        text_extracted   = EXCLUDED.text_extracted,
        attempts         = EXCLUDED.attempts,
        last_error       = EXCLUDED.last_error,
        archived_at      = COALESCE(evidence_archive.archived_at, EXCLUDED.archived_at),
        updated_at       = now()
"""

# The signal stamp — ONE update. object_ref = the cas address (the read
# surfaces derive archived/archive_sha256 from this existing column alone);
# retention_class upgraded to evidence_hold unless the row already sits in a
# keep-class (retain_always stays retain_always); and, WHEN text was extracted
# ($3 non-null), payload.archived_text is set AND the corpus dirty-marker
# contract is honored: indexed_at = NULL re-queues the doc, updated_at = now()
# protects the re-null from the indexer's version-guarded stamp (BOTH in this
# same UPDATE — see corpus_indexer's DIRTY-MARKER CONTRACT). With no text,
# updated_at still bumps (harmless) and indexed_at is left alone.
_STAMP_SIGNAL_SQL = """
    UPDATE signals
       SET object_ref = $2,
           retention_class = CASE
               WHEN retention_class IN ('retain_always', 'evidence_hold')
               THEN retention_class ELSE 'evidence_hold' END,
           payload = CASE WHEN $3::text IS NULL THEN payload
                          ELSE jsonb_set(payload, '{archived_text}', to_jsonb($3::text)) END,
           indexed_at = CASE WHEN $3::text IS NULL THEN indexed_at ELSE NULL END,
           updated_at = now()
     WHERE id = $1
"""


# ---------------------------------------------------------------------------
# Small pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------
# The CAS address format (cas_object_ref / cas_path / sha256_from_object_ref)
# lives in :mod:`legba.data.archive` — ONE owner shared with the read
# projections; re-exported here for the handler's callers/tests.


def resolve_license_class(row: Mapping[str, Any]) -> str | None:
    """The signal's license class as the P2-2 gate sees it.

    ``payload.license_class`` (the LIC-2 SourceScope→signal ingest stamp)
    first, then the manual-batch ``raw_provenance`` license — the same
    resolution the corpus doc projection uses (one truth, one helper)."""
    return signal_license_class(row)


def license_forbids_retention(
    license_class: str | None, forbid: frozenset[str] | set[str],
) -> bool:
    """True when the class forbids archiving the original bytes.

    Unknown/unset (``None``) ARCHIVES — the open-web quotation-for-evidence
    default posture (module docstring); the class is recorded either way so a
    policy flip can re-evaluate."""
    return license_class is not None and license_class in forbid


def _is_textual(content_type: str | None, body: bytes) -> bool:
    """Whether Trafilatura extraction should be attempted on this object."""
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if any(ct.startswith(p) for p in _TEXTUAL_CONTENT_TYPES):
            return True
        # A declared non-text type (image/pdf/…) is authoritative — don't sniff.
        return False
    head = body[:512].lstrip().lower()
    return head.startswith((b"<!doctype", b"<html", b"<?xml"))


def _extract_text(body: bytes, encoding: str | None, *, max_chars: int) -> str | None:
    """Trafilatura main-text extraction — bonus only; ``None`` on any failure.

    Lazy import (trafilatura is a base dep, but its import is heavy and every
    other sub-handler would pay it at package import time otherwise)."""
    try:
        import trafilatura  # noqa: PLC0415 — deliberate lazy import (see docstring)

        html = body.decode(encoding or "utf-8", errors="replace")
        text = trafilatura.extract(html, include_comments=False)
        if text and text.strip():
            return text.strip()[:max_chars]
        return None
    except Exception as exc:  # extraction is best-effort by contract
        logger.debug("evidence_archiver.extract_failed err=%s", exc)
        return None


def _store_bytes(root: Path, body: bytes) -> tuple[str, str, bool]:
    """Content-address ``body`` under ``root``.

    Returns ``(sha256_hex, object_ref, was_already_present)``. Write is
    temp-file + atomic rename; an existing object is NEVER rewritten (CAS —
    same hash IS same bytes)."""
    digest = hashlib.sha256(body).hexdigest()
    dest = cas_path(root, digest)
    if dest.exists():
        return digest, cas_object_ref(digest), True
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".tmp.{os.getpid()}.{digest[:12]}"
    tmp.write_bytes(body)
    os.replace(tmp, dest)  # atomic on the same filesystem
    return digest, cas_object_ref(digest), False


class _ObjectTooLargeError(Exception):
    """The response exceeded the archiver's size cap."""


async def _fetch_bytes(
    client: httpx.AsyncClient, url: str, *, max_bytes: int,
) -> tuple[bytes, str | None, str | None]:
    """Stream ``url`` → ``(body, content_type, encoding)`` under the size cap.

    Raises :class:`_ObjectTooLargeError` past the cap (checked against the
    declared Content-Length first, then enforced on the actual stream — a
    lying header can't blow the cap), and lets httpx/egress errors propagate
    to the per-row handler."""
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        declared = resp.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise _ObjectTooLargeError(f"declared {declared} bytes > cap {max_bytes}")
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise _ObjectTooLargeError(f"stream exceeded cap {max_bytes}")
            chunks.append(chunk)
        return (
            b"".join(chunks),
            resp.headers.get("content-type"),
            resp.charset_encoding,
        )


class _HostPoliteness:
    """Per-host minimum spacing between fetches (monotonic-clock based)."""

    def __init__(self, delay_s: float) -> None:
        self._delay = max(0.0, float(delay_s))
        self._last: dict[str, float] = {}

    async def wait(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if not host or self._delay <= 0:
            return
        now = time.monotonic()
        last = self._last.get(host)
        if last is not None:
            remaining = self._delay - (now - last)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last[host] = time.monotonic()


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _zero_counters() -> dict[str, int]:
    return {
        "examined": 0,
        "archived": 0,
        "already_present": 0,
        "media_archived": 0,
        "media_failed": 0,
        "text_extracted": 0,
        "text_extract_failed": 0,
        "skipped_license": 0,
        "skipped_size": 0,
        "fetch_failed": 0,
        "egress_blocked": 0,
        "bytes_stored": 0,
        "deadline_stop": 0,
        "skipped_no_root": 0,
    }


def _archive_root() -> Path | None:
    """The archive root, created if needed; ``None`` when unusable (the run
    then no-ops LOUDLY via ``skipped_no_root`` — never a silent drop)."""
    raw = os.environ.get(ARCHIVE_ROOT_ENV, DEFAULT_ARCHIVE_ROOT)
    root = Path(raw)
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not os.access(root, os.W_OK):
            raise PermissionError(f"{root} not writable")
        return root
    except Exception as exc:
        logger.warning(
            "evidence_archiver.no_root — archive root %s unusable (%s); "
            "set %s / mount the legba_archive volume. Skipping this tick.",
            raw, exc, ARCHIVE_ROOT_ENV,
        )
        return None


async def _record(
    pool: Any,
    *,
    signal_id: Any,
    status: str,
    attempts: int,
    object_ref: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
    content_type: str | None = None,
    fetched_url: str | None = None,
    media_object_ref: str | None = None,
    media_sha256: str | None = None,
    media_size_bytes: int | None = None,
    license_class: str | None = None,
    text_extracted: bool = False,
    last_error: str | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _UPSERT_ARCHIVE_SQL,
            signal_id, status, object_ref, sha256, size_bytes, content_type,
            fetched_url, media_object_ref, media_sha256, media_size_bytes,
            license_class, text_extracted, attempts,
            str(last_error)[:2048] if last_error else None,
        )


async def _archive_one(
    pool: Any,
    client: httpx.AsyncClient,
    row: Mapping[str, Any],
    *,
    root: Path,
    politeness: _HostPoliteness,
    counters: dict[str, int],
    forbid: frozenset[str],
    max_bytes: int,
    max_text_chars: int,
    max_attempts: int,
) -> None:
    """Fetch + store + record ONE candidate signal (all outcomes recorded)."""
    signal_id = row["id"]
    url = str(row["canonical_url"])
    attempts = int(row.get("prior_attempts") or 0) + 1
    license_class = resolve_license_class(row)

    # ---- P2-2 license gate (recorded, never silent) ----
    if license_forbids_retention(license_class, forbid):
        counters["skipped_license"] += 1
        await _record(
            pool, signal_id=signal_id, status="skipped_license",
            attempts=attempts - 1, fetched_url=url, license_class=license_class,
            last_error=f"license_class {license_class!r} forbids retention",
        )
        return

    # ---- fetch the original bytes (egress-guarded, size-capped, polite) ----
    try:
        await politeness.wait(url)
        body, content_type, encoding = await _fetch_bytes(
            client, url, max_bytes=max_bytes,
        )
    except _ObjectTooLargeError as exc:
        counters["skipped_size"] += 1
        await _record(
            pool, signal_id=signal_id, status="skipped_size",
            attempts=attempts, fetched_url=url, license_class=license_class,
            last_error=str(exc),
        )
        return
    except EgressBlockedError as exc:
        # Terminal — a non-public target never becomes fetchable. Cap the
        # attempts immediately so the budget is never re-burned on it.
        counters["fetch_failed"] += 1
        counters["egress_blocked"] += 1
        await _record(
            pool, signal_id=signal_id, status="failed",
            attempts=max(attempts, max_attempts), fetched_url=url,
            license_class=license_class, last_error=f"egress blocked: {exc}",
        )
        return
    except Exception as exc:
        counters["fetch_failed"] += 1
        await _record(
            pool, signal_id=signal_id, status="failed",
            attempts=attempts, fetched_url=url, license_class=license_class,
            last_error=repr(exc),
        )
        return

    # ---- store (content-addressed, atomic, dedup-aware) ----
    try:
        digest, object_ref, existed = _store_bytes(root, body)
    except OSError as exc:
        counters["fetch_failed"] += 1
        await _record(
            pool, signal_id=signal_id, status="failed",
            attempts=attempts, fetched_url=url, license_class=license_class,
            last_error=f"store failed: {exc}",
        )
        return
    if existed:
        counters["already_present"] += 1
    else:
        counters["bytes_stored"] += len(body)

    # ---- bonus text extraction (bytes are the archive either way) ----
    archived_text: str | None = None
    if _is_textual(content_type, body):
        archived_text = _extract_text(body, encoding, max_chars=max_text_chars)
        if archived_text is not None:
            counters["text_extracted"] += 1
        else:
            counters["text_extract_failed"] += 1

    # ---- media leg (best-effort; never fails the row; no processing) ----
    media_object_ref: str | None = None
    media_sha256: str | None = None
    media_size: int | None = None
    media_ref = row.get("media_ref")
    if isinstance(media_ref, str) and media_ref.startswith(("http://", "https://")):
        try:
            await politeness.wait(media_ref)
            media_body, _, _ = await _fetch_bytes(
                client, media_ref, max_bytes=max_bytes,
            )
            media_sha256, media_object_ref, media_existed = (
                _store_bytes(root, media_body)
            )
            media_size = len(media_body)
            if not media_existed:
                counters["bytes_stored"] += media_size
            counters["media_archived"] += 1
        except Exception as exc:
            counters["media_failed"] += 1
            logger.debug(
                "evidence_archiver.media_failed signal=%s err=%s", signal_id, exc,
            )

    # ---- record + stamp (sidecar row, then the signal's object_ref) ----
    await _record(
        pool, signal_id=signal_id, status="archived", attempts=attempts,
        object_ref=object_ref, sha256=digest, size_bytes=len(body),
        content_type=content_type, fetched_url=url,
        media_object_ref=media_object_ref, media_sha256=media_sha256,
        media_size_bytes=media_size, license_class=license_class,
        text_extracted=archived_text is not None,
    )
    async with pool.acquire() as conn:
        await conn.execute(_STAMP_SIGNAL_SQL, signal_id, object_ref, archived_text)
    counters["archived"] += 1


async def _sweep(pool: Any, options: Mapping[str, Any]) -> dict[str, int]:
    counters = _zero_counters()

    root = _archive_root()
    if root is None:
        counters["skipped_no_root"] = 1
        return counters

    window_hours = int(options.get("window_hours", _DEFAULT_WINDOW_HOURS))
    verify_floor = float(options.get("verify_floor", _DEFAULT_VERIFY_FLOOR))
    fetch_budget = int(options.get("fetch_budget", _DEFAULT_FETCH_BUDGET))
    max_attempts = int(options.get("max_attempts", _DEFAULT_MAX_ATTEMPTS))
    max_bytes = int(options.get("max_object_bytes", _DEFAULT_MAX_OBJECT_BYTES))
    max_text_chars = int(options.get("max_text_chars", _DEFAULT_MAX_TEXT_CHARS))
    per_host_delay = float(
        options.get("per_host_delay_seconds", _DEFAULT_PER_HOST_DELAY_S)
    )
    timeout_s = float(options.get("timeout_seconds", _DEFAULT_TIMEOUT_S))
    deadline_s = float(options.get("run_deadline_seconds", _DEFAULT_RUN_DEADLINE_S))
    forbid_raw = options.get("forbid_license_classes")
    forbid = (
        frozenset(str(c) for c in forbid_raw)
        if isinstance(forbid_raw, (list, tuple, set))
        else FORBID_RETENTION_CLASSES
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _SELECT_CANDIDATES_SQL,
            window_hours, verify_floor, max_attempts, fetch_budget,
        )
    if not rows:
        return counters
    counters["examined"] = len(rows)

    politeness = _HostPoliteness(per_host_delay)
    started = time.monotonic()
    async with guarded_async_client(
        timeout=timeout_s,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        for row in rows:
            if time.monotonic() - started > deadline_s:
                counters["deadline_stop"] = 1
                break
            await _archive_one(
                pool, client, row,
                root=root, politeness=politeness, counters=counters,
                forbid=forbid, max_bytes=max_bytes,
                max_text_chars=max_text_chars, max_attempts=max_attempts,
            )
    return counters


def _build_finding(counters: Mapping[str, int]) -> FindingPayload:
    title = (
        f"Evidence archiver: archived {counters.get('archived', 0)} of "
        f"{counters.get('examined', 0)} cited signal(s), "
        f"{counters.get('skipped_license', 0)} license-skipped, "
        f"{counters.get('fetch_failed', 0)} failed"
    )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "evidence_archiver"]
    if counters.get("archived", 0):
        tags.append("archived")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, **dict(counters)},
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Sweeps the substrate directly via ``deps.pg_pool`` (the ``inputs`` slice is
    ignored — the unit of work is "the next budgeted batch of un-archived cited
    signals"). ``deps is None`` (unit-test path) yields a zeroed run. Usage is
    always zeroed (deterministic kind, no LLM)."""
    counters = _zero_counters()
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        try:
            counters = await _sweep(pool, options)
        except Exception as exc:
            logger.warning("evidence_archiver.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "handle",
    "SUB_HANDLER_NAME",
    "ARCHIVE_ROOT_ENV",
    "DEFAULT_ARCHIVE_ROOT",
    "FORBID_RETENTION_CLASSES",
    "CAS_PREFIX",
    "cas_object_ref",
    "cas_path",
    "sha256_from_object_ref",
    "resolve_license_class",
    "license_forbids_retention",
]
