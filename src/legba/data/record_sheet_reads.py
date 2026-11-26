# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RECORD SHEET's reads — receipts, stored text, integrity.

The console's Record sheet reads a statement top to bottom as a chain:
statement → row → evidence → RECEIPT → later versions → integrity. The row and
the evidence come from the lineage walk (``/lineage/{kind}/{id}``, depth 1).
This leaf answers the three steps no route served:

* :func:`record_receipts` — for up to :data:`MAX_IDS` evidence records
  (signals or series observations): the fetch versions each was built from
  (``fetch_version_records`` → ``fetch_versions``: address, URL, protocol,
  status, content type, size, sha256, when first and last seen, where the
  bytes are held — hot, cold, pruned or never stored), the source and its
  class, the later versions of the record's PAGE address with the drift class
  between consecutive ones, and — when a record has no receipt — WHY, in one
  of three closed reasons (:data:`NO_RECEIPT_REASONS`).
* :func:`version_text` — the text a page version stored (the platform's own
  extraction, ``fetch_versions.extracted_text``), bounded to ``max_chars``,
  with a re-hash of the WHOLE stored text against its stored sha256. Never the
  raw body and never HTML: the column is plain text by construction, and the
  console renders it as text.
* :func:`item_text` (B-1) — the ITEM AS RECEIVED for one signal: the slice of
  the stored response its record is (a feed item, a GDELT export row, a
  Telegram message), or the archived page's text, read from the held bytes
  and re-hashed (``byte_path.signal_item_text``), under the same withholding
  rules as :func:`version_text`. A feed or API response keeps no extracted
  text of its own, so this is the only text a quote of such an item can be
  read against.
* :func:`run_integrity` — the producing run's receipt re-hashed (the lineage
  route's own helper), the signed checkpoint that names it exactly, the first
  later checkpoint of the same unit's chain, and the first daily anchor made
  after the run — each stated in one of :data:`INTEGRITY_STATES`.

Every read is BOUNDED: ids capped at :data:`MAX_IDS`, later versions at
:data:`MAX_LATER` per address, drift rows at :data:`MAX_CHANGES`, text at
:data:`MAX_TEXT_CHARS`. Every query is keyed on a primary key or an existing
index (``fetch_version_records_pkey``, ``fetch_versions_pkey``,
``fetch_versions_address_seen_idx``, ``evidence_drift_classifications_address_idx``,
``evidence_archive_cold_pkey``, ``source_descriptors_head_unique``,
``analyst_traces_pkey``, ``audit_checkpoints_analyst_idx``); the live EXPLAINs
are recorded in the lane report.

The evidence LABEL of each signal (``received bytes`` / ``archived later`` /
``no bytes kept``) is ``byte_path.evidence_states`` — the one function the
statement gate and the drift pass read too; the sheet keeps no
rule of its own for it.

A LEAF: stdlib + ``legba.data._url_canon`` and ``legba.data.byte_path``
(both stdlib-only at module level). The
integrity read imports the lineage route's receipt helper lazily (registry-slim
both). Nothing from ``legba.data.analysts`` or ``legba.runtime``.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

from ._url_canon import canonical_url
from .byte_path import NO_BYTES_KEPT, EvidenceState, evidence_states, signal_item_text
from .claim_support import CLAIM_SOURCE_CLASS, claim_text_withheld
from .trace_bulk import hydrate_traces

__all__ = [
    "FORBID_TEXT_LICENSE_CLASSES",
    "INTEGRITY_STATES",
    "MAX_CHANGES",
    "MAX_IDS",
    "MAX_LATER",
    "MAX_TEXT_CHARS",
    "NO_RECEIPT_KINDS",
    "NO_RECEIPT_REASONS",
    "RECORD_KINDS",
    "item_text",
    "record_receipts",
    "run_integrity",
    "version_text",
]

#: The record kinds a receipt links to (``fetch_version_records.record_kind``).
RECORD_KINDS = ("signal", "observation")

#: Evidence records one receipts read may ask for.
MAX_IDS = 25

#: Later versions listed per page address (one more is read to say "and more").
MAX_LATER = 8

#: Drift classifications listed per read.
MAX_CHANGES = 60

#: The most stored text one read returns; the default is the console's.
MAX_TEXT_CHARS = 100_000
DEFAULT_TEXT_CHARS = 20_000

#: Source kinds whose items are not received over HTTP by us, so no fetch
#: receipt is ever kept (``runtime/fetch_receipts`` module docstring: Telegram's
#: MTProto; a manual batch is typed in, not fetched).
NO_RECEIPT_KINDS = frozenset({"telegram_channel", "manual"})

#: Why a record has no receipt — closed. Given only for a signal whose
#: evidence label (``byte_path``) is ``no_bytes_kept`` with no link at all, or
#: an observation with no link.
NO_RECEIPT_REASONS = (
    "before_receipts_start",   # received before fetch receipts were kept
    "source_kind_without_receipts",  # its source kind is not fetched over HTTP
    "not_linked",              # received after the start, by a fetching source, yet no link was written
)

#: The licence classes whose text is never shown — the evidence archiver's
#: retention refusal (``evidence_archiver.FORBID_RETENTION_CLASSES``), restated
#: here because that module is analyst-side (not in the slim image); a guard
#: test pins the two equal.
FORBID_TEXT_LICENSE_CLASSES = frozenset({"anti_ai_walled", "tos_restrictive", "personal_use_only"})

#: The integrity states, in words the console prints.
INTEGRITY_STATES = ("verified", "covered", "not_covered", "failed")

_PAGE_ROLES = ("page", "archiver")


def _iso(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _uuid_list(ids: Iterable[str]) -> list[UUID]:
    out: list[UUID] = []
    for raw in ids:
        try:
            out.append(UUID(str(raw)))
        except (ValueError, TypeError):
            continue
    return list(dict.fromkeys(out))[:MAX_IDS]


def _protocol(url: str | None, receipt_kind: str | None) -> str:
    """How it reached us, in one word: the URL scheme of a fetch, or ``push``."""
    if receipt_kind == "inbound":
        return "push"
    if isinstance(url, str) and "://" in url:
        return url.split("://", 1)[0].lower()
    return "unknown"


async def _has_table(conn: Any, name: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{name}"))


_RECEIPTS_START_SQL = "SELECT first_seen_at FROM fetch_versions ORDER BY id LIMIT 1"

_SIGNALS_SQL = """
SELECT s.id, s.source_id, s.fetched_at, s.canonical_url, s.access_class,
       s.payload->>'license_class' AS license_class
  FROM signals s
 WHERE s.id = ANY($1::uuid[])
"""

_OBSERVATIONS_SQL = """
SELECT o.id, o.source_id, o.record_time AS fetched_at, o.source_url AS canonical_url,
       NULL::text AS access_class, NULL::text AS license_class
  FROM observations o
 WHERE o.id = ANY($1::uuid[])
"""

_SOURCES_SQL = """
SELECT descriptor_id, name, kind,
       body->'scope'->>'source_class'  AS source_class,
       body->'scope'->>'license_class' AS license_class,
       body->'scope'->>'access_class'  AS access_class
  FROM source_descriptors
 WHERE descriptor_id = ANY($1::text[]) AND is_head
"""

_LINKS_SQL = """
SELECT record_id, role, version_id, sha256, linked_at
  FROM fetch_version_records
 WHERE record_kind = $1 AND record_id = ANY($2::uuid[])
 ORDER BY record_id, role
"""

_VERSION_COLUMNS = """
v.id, v.receipt_kind, v.address, v.url, v.role, v.status, v.content_type, v.content_encoding,
v.size_bytes, v.sha256, v.hash_basis, v.first_seen_at, v.last_seen_at, v.fetch_count,
v.first_source_id, v.store_status, (v.object_ref IS NOT NULL) AS bytes_ref, v.bytes_pruned_at,
v.text_sha256, v.text_chars, (v.extracted_text IS NOT NULL) AS has_text
"""

_VERSIONS_SQL = f"SELECT {_VERSION_COLUMNS} FROM fetch_versions v WHERE v.id = ANY($1::bigint[])"

#: Page versions of an address, oldest first, capped per address (the index on
#: (address, last_seen_at) serves the address match; the per-address rows are few).
_ADDRESS_VERSIONS_SQL = f"""
SELECT a.address AS for_address, x.*
  FROM unnest($1::text[]) AS a(address)
  CROSS JOIN LATERAL (
        SELECT {_VERSION_COLUMNS}
          FROM fetch_versions v
         WHERE v.address = a.address AND v.receipt_kind = 'fetch'
           AND v.role = ANY($2::text[])
         ORDER BY v.first_seen_at, v.id
         LIMIT $3
  ) x
"""

_CHANGES_SQL = """
SELECT c.address, c.earlier_version_id, c.later_version_id, c.drift_class,
       c.later_fetched_at, d.later_gone, d.later_status
  FROM evidence_drift_classifications c
  JOIN fetch_drift_observations d ON d.id = c.drift_id
 WHERE c.address = ANY($1::text[])
 ORDER BY c.address, c.later_fetched_at, c.id
 LIMIT $2
"""

_COLD_SQL = """
SELECT sha256, hot_removed_at FROM evidence_archive_cold WHERE sha256 = ANY($1::text[])
"""


def _held(row: Mapping[str, Any], cold: Mapping[str, Any]) -> str:
    """Where the received bytes are: ``hot`` | ``cold`` | ``pruned`` | ``not_stored``."""
    if not row.get("bytes_ref"):
        return "not_stored"
    if row.get("bytes_pruned_at") is not None:
        return "pruned"
    if row.get("sha256") in cold and cold[row["sha256"]] is not None:
        return "cold"
    return "hot"


def _version_out(row: Mapping[str, Any], cold: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "receipt_kind": row["receipt_kind"],
        "address": row["address"],
        "url": row["url"],
        "protocol": _protocol(row["url"], row["receipt_kind"]),
        "role": row["role"],
        "status": row["status"],
        "content_type": row["content_type"],
        "content_encoding": row["content_encoding"],
        "size_bytes": int(row["size_bytes"]) if row["size_bytes"] is not None else None,
        "sha256": row["sha256"],
        "hash_basis": row["hash_basis"],
        "first_seen_at": _iso(row["first_seen_at"]),
        "last_seen_at": _iso(row["last_seen_at"]),
        "fetch_count": int(row["fetch_count"]) if row["fetch_count"] is not None else None,
        "source_id": row["first_source_id"],
        "store_status": row["store_status"],
        "held": _held(row, cold),
        "text_sha256": row["text_sha256"],
        "text_chars": row["text_chars"],
        "has_text": bool(row["has_text"]),
    }


def _evidence_out(state: EvidenceState | None) -> dict[str, Any] | None:
    """The sheet's evidence label for one signal — ``byte_path``'s, as data."""
    if state is None:
        return None
    return {
        "label": state.label, "label_text": state.label_text,
        "version_id": state.version_id, "link_role": state.link_role,
        "has_text": state.has_text, "hash_only": list(state.hash_only),
    }


async def record_receipts(conn: Any, kind: str, ids: Iterable[str]) -> Optional[dict[str, Any]]:
    """The receipts behind up to :data:`MAX_IDS` evidence records of ``kind``.

    ``None`` when the receipt tables are absent (migration 0245 not applied).
    """
    if kind not in RECORD_KINDS:
        raise ValueError(f"kind must be one of {RECORD_KINDS}")
    wanted = _uuid_list(ids)
    if not await _has_table(conn, "fetch_versions"):
        return None
    start = await conn.fetchval(_RECEIPTS_START_SQL)
    if not wanted:
        return {"receipts_start": _iso(start), "records": [], "versions": {}, "pages": {}, "changes": []}

    rows = await conn.fetch(_SIGNALS_SQL if kind == "signal" else _OBSERVATIONS_SQL, wanted)
    by_id = {r["id"]: r for r in rows}
    links = await conn.fetch(_LINKS_SQL, kind, wanted)
    links_by: dict[UUID, list[Mapping[str, Any]]] = {}
    for link in links:
        links_by.setdefault(link["record_id"], []).append(link)

    source_ids = sorted({r["source_id"] for r in rows if r["source_id"]})
    sources = {s["descriptor_id"]: s for s in await conn.fetch(_SOURCES_SQL, source_ids)} if source_ids else {}

    # The record's PAGE address — where a later change of the page shows.
    page_address: dict[UUID, str] = {}
    for rid, r in by_id.items():
        url = r["canonical_url"]
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            page_address[rid] = canonical_url(url) or url
    addresses = sorted(set(page_address.values()))

    version_ids = sorted({int(link["version_id"]) for link in links})
    version_rows = list(await conn.fetch(_VERSIONS_SQL, version_ids)) if version_ids else []
    page_rows = (
        list(await conn.fetch(_ADDRESS_VERSIONS_SQL, addresses, list(_PAGE_ROLES), MAX_LATER + 1))
        if addresses else []
    )
    shas = sorted({r["sha256"] for r in [*version_rows, *page_rows]})
    cold: dict[str, Any] = {}
    if shas and await _has_table(conn, "evidence_archive_cold"):
        cold = {r["sha256"]: r["hot_removed_at"] for r in await conn.fetch(_COLD_SQL, shas)}

    versions = {str(int(r["id"])): _version_out(r, cold) for r in version_rows}
    pages: dict[str, dict[str, Any]] = {}
    for r in page_rows:
        entry = pages.setdefault(r["for_address"], {"versions": [], "more": False})
        if len(entry["versions"]) >= MAX_LATER:
            entry["more"] = True
            continue
        entry["versions"].append(_version_out(r, cold))

    # The evidence label of each signal: byte_path's one rule (B-1).
    states = await evidence_states(conn, wanted) if kind == "signal" else {}

    changes: list[dict[str, Any]] = []
    if addresses and await _has_table(conn, "evidence_drift_classifications"):
        for c in await conn.fetch(_CHANGES_SQL, addresses, MAX_CHANGES):
            changes.append({k: _iso(c[k]) for k in c.keys()})

    records: list[dict[str, Any]] = []
    for rid in wanted:
        r = by_id.get(rid)
        own = links_by.get(rid, [])
        src = sources.get(r["source_id"]) if r is not None and r["source_id"] else None
        out: dict[str, Any] = {
            "kind": kind,
            "id": str(rid),
            "found": r is not None,
            "fetched_at": _iso(r["fetched_at"]) if r is not None else None,
            "canonical_url": r["canonical_url"] if r is not None else None,
            "page_address": page_address.get(rid),
            "access_class": (r["access_class"] if r is not None else None),
            "license_class": (r["license_class"] if r is not None else None)
                or (src["license_class"] if src is not None else None),
            "source": None if r is None or not r["source_id"] else {
                "id": r["source_id"],
                "name": src["name"] if src is not None else None,
                "kind": src["kind"] if src is not None else None,
                "source_class": src["source_class"] if src is not None else None,
            },
            "links": [
                {"role": link["role"], "version_id": int(link["version_id"]),
                 "sha256": link["sha256"], "linked_at": _iso(link["linked_at"])}
                for link in own
            ],
            "evidence": _evidence_out(states.get(str(rid))) if r is not None else None,
            "no_receipt_reason": None,
        }
        state = states.get(str(rid))
        unlinked = (not own) if state is None else (state.label == NO_BYTES_KEPT and not state.links)
        if r is not None and unlinked:
            src_kind = src["kind"] if src is not None else None
            fetched = r["fetched_at"]
            if start is None or (fetched is not None and fetched < start):
                out["no_receipt_reason"] = "before_receipts_start"
            elif src_kind in NO_RECEIPT_KINDS:
                out["no_receipt_reason"] = "source_kind_without_receipts"
            else:
                out["no_receipt_reason"] = "not_linked"
        records.append(out)

    return {
        "receipts_start": _iso(start),
        "records": records,
        "versions": versions,
        "pages": pages,
        "changes": changes,
    }


_TEXT_META_SQL = """
SELECT v.id, v.receipt_kind, v.role, v.sha256, v.size_bytes, v.text_sha256, v.text_chars,
       v.first_source_id, v.last_source_id, (v.extracted_text IS NOT NULL) AS has_text
  FROM fetch_versions v WHERE v.id = $1
"""

#: The version's licence, read where it is recorded: the linked records'
#: ingest stamp (at most five rows) and the fetching sources' descriptors.
_TEXT_LICENSE_SQL = """
SELECT s.payload->>'license_class' AS license_class, s.access_class,
       (s.payload->'claim'->>'source_class') = 'claim' AS is_claim
  FROM fetch_version_records r JOIN signals s ON s.id = r.record_id
 WHERE r.version_id = $1 AND r.record_kind = 'signal'
 LIMIT 5
"""

_TEXT_SOURCE_LICENSE_SQL = """
SELECT body->'scope'->>'license_class' AS license_class,
       body->'scope'->>'source_class' AS source_class
  FROM source_descriptors WHERE descriptor_id = ANY($1::text[]) AND is_head
"""

_TEXT_SQL = """
SELECT left(v.extracted_text, $2) AS shown,
       length(v.extracted_text) AS chars_total,
       (v.text_sha256 = encode(sha256(convert_to(v.extracted_text, 'UTF8')), 'hex')) AS hash_ok
  FROM fetch_versions v WHERE v.id = $1
"""


async def version_text(
    conn: Any, version_id: int, *, max_chars: int = DEFAULT_TEXT_CHARS, reader: bool,
    display_class: str | None = None,
) -> Optional[dict[str, Any]]:
    """What one version stored as text — bounded, and only to whom it may go.

    ``reader`` is the display class (``display_policy``): a READER never gets
    publisher text beyond a cited span, so no text is read for one.
    ``display_class``: a CLAIM source's
    stored text — a version whose linked signals carry the claim stamp, or
    whose source declares ``source_class: claim`` — goes to the operator
    class only (``claim_support.claim_text_withheld`` →
    ``claim_text_admin_only``), whatever the reader flag says. A version
    whose recorded licence is a retention refusal is withheld from everyone.
    ``None`` when the receipt tables are absent; ``{"found": False}`` for an
    unknown id.
    """
    cap = max(1, min(int(max_chars), MAX_TEXT_CHARS))
    if not await _has_table(conn, "fetch_versions"):
        return None
    meta = await conn.fetchrow(_TEXT_META_SQL, int(version_id))
    if meta is None:
        return {"version_id": int(version_id), "found": False}
    licences, reason = await _withholding(conn, meta, reader=reader, display_class=display_class)
    out: dict[str, Any] = {
        "version_id": int(meta["id"]),
        "found": True,
        "role": meta["role"],
        "sha256": meta["sha256"],
        "size_bytes": int(meta["size_bytes"]) if meta["size_bytes"] is not None else None,
        "text_sha256": meta["text_sha256"],
        "license_classes": sorted(licences),
        "shown": False,
        "withheld_reason": None,
        "chars_total": meta["text_chars"],
        "chars_shown": 0,
        "truncated": False,
        "text_hash_ok": None,
        "full_text": None,
        "max_chars": cap,
    }
    out["withheld_reason"] = "no_text" if not meta["has_text"] else reason
    if out["withheld_reason"] is not None:
        return out
    row = await conn.fetchrow(_TEXT_SQL, int(version_id), cap)
    shown = row["shown"] or ""
    total = int(row["chars_total"] or 0)
    out.update({
        "shown": True,
        "full_text": shown,
        "chars_total": total,
        "chars_shown": len(shown),
        "truncated": total > len(shown),
        "text_hash_ok": None if meta["text_sha256"] is None else bool(row["hash_ok"]),
    })
    return out


async def _withholding(
    conn: Any, meta: Mapping[str, Any], *, reader: bool, display_class: str | None,
) -> tuple[set[str], str | None]:
    """``(licences, withheld_reason)`` for one version's text — the same rule
    for the version's stored text and an item cut from its bytes: a retention
    refusal withholds from everyone, a claim source's text goes to the
    operator class only, a reader gets no publisher text."""
    version_id = int(meta["id"])
    licences: set[str] = set()
    is_claim = False
    for r in await conn.fetch(_TEXT_LICENSE_SQL, int(version_id)):
        if r["license_class"]:
            licences.add(str(r["license_class"]))
        is_claim = is_claim or bool(r["is_claim"])
    srcs = [s for s in (meta["first_source_id"], meta["last_source_id"]) if s]
    if srcs:
        for r in await conn.fetch(_TEXT_SOURCE_LICENSE_SQL, sorted(set(srcs))):
            if r["license_class"]:
                licences.add(str(r["license_class"]))
            is_claim = is_claim or r["source_class"] == CLAIM_SOURCE_CLASS
    # Without a display class the caller is treated as a reader for the claim rule.
    claim_reason = claim_text_withheld(display_class or "reader", is_claim=is_claim)
    if licences & FORBID_TEXT_LICENSE_CLASSES:
        return licences, "license_forbids"
    if claim_reason is not None:
        return licences, claim_reason
    if reader:
        return licences, "reader_display_policy"
    return licences, None


_SIGNAL_EXISTS_SQL = "SELECT 1 FROM signals WHERE id = $1"


async def item_text(
    conn: Any, signal_id: str, *, max_chars: int = DEFAULT_TEXT_CHARS, reader: bool,
    display_class: str | None = None, root: Any = None,
) -> Optional[dict[str, Any]]:
    """The item AS RECEIVED for one signal (B-1), bounded, to whom it may go.

    The evidence label comes first (``byte_path.evidence_states``); a signal
    with ``no_bytes_kept`` has no item to read. The version the label rests on
    is checked with :func:`version_text`'s withholding rule BEFORE any byte is
    read; then ``byte_path.signal_item_text`` cuts the item from the held,
    re-verified bytes (or reads the archived page's text). Withheld reasons:
    ``no_bytes_kept``, ``license_forbids``, ``claim_text_admin_only``,
    ``reader_display_policy``, ``no_item_text`` (the bytes hold no item at its
    locator), ``item_reader_unavailable`` (this image cannot read stored
    bytes). ``None`` when the receipt tables are absent.
    """
    cap = max(1, min(int(max_chars), MAX_TEXT_CHARS))
    if not await _has_table(conn, "fetch_versions"):
        return None
    try:
        sid = UUID(str(signal_id))
    except (ValueError, TypeError):
        return {"signal_id": str(signal_id), "found": False, "max_chars": cap}
    if await conn.fetchval(_SIGNAL_EXISTS_SQL, sid) is None:
        return {"signal_id": str(sid), "found": False, "max_chars": cap}
    state = (await evidence_states(conn, [sid]))[str(sid)]
    out: dict[str, Any] = {
        "signal_id": str(sid), "found": True, "evidence": _evidence_out(state),
        "version_id": state.version_id, "basis": None, "exact": None,
        "license_classes": [], "shown": False, "withheld_reason": None,
        "chars_total": None, "chars_shown": 0, "truncated": False,
        "text_sha256": None, "item_sha256": None, "version_sha256": None,
        "full_text": None, "max_chars": cap,
    }
    if state.version_id is None:
        out["withheld_reason"] = "no_bytes_kept"
        return out
    meta = await conn.fetchrow(_TEXT_META_SQL, int(state.version_id))
    licences, reason = await _withholding(conn, meta, reader=reader, display_class=display_class)
    out["license_classes"] = sorted(licences)
    if reason is not None:
        out["withheld_reason"] = reason
        return out
    try:
        _, item = await signal_item_text(conn, sid, root=root)
    except ImportError:  # the stored-bytes reader is not in this image
        out["withheld_reason"] = "item_reader_unavailable"
        return out
    if item is None:
        out["withheld_reason"] = "no_item_text"
        return out
    shown = item.text[:cap]
    out.update({
        "basis": item.basis, "exact": item.exact, "shown": True, "full_text": shown,
        "chars_total": len(item.text), "chars_shown": len(shown),
        "truncated": len(item.text) > len(shown), "text_sha256": item.text_sha256,
        "item_sha256": item.item_sha256, "version_sha256": item.version_sha256,
    })
    return out


_LATER_CHECKPOINT_SQL = """
SELECT checkpointed_at, signer_did, trace_count, chain_head_hash
  FROM audit_checkpoints
 WHERE analyst_id = $1 AND checkpointed_at >= $2
 ORDER BY checkpointed_at ASC
 LIMIT 1
"""

_ANCHOR_SQL = """
SELECT day, calendar, status, submitted_at, leaf_count, created_at
  FROM receipt_anchors
 WHERE created_at >= $1
 ORDER BY created_at, calendar
 LIMIT 2
"""


async def run_integrity(conn: Any, run_id: str) -> Optional[dict[str, Any]]:
    """The integrity line for one producing run.

    * ``receipt`` — the run's trace re-hashed against its stored hash
      (``verified`` / ``failed``), or ``not_covered`` when no trace exists;
    * ``checkpoint`` — ``verified`` when a signed checkpoint names THIS receipt
      as its head; ``covered`` when a later checkpoint of the same unit's
      chain exists (the links from this receipt to that head are not re-walked
      here, and the answer says so); else ``not_covered``;
    * ``anchor`` — ``covered`` when a daily external anchor was submitted after
      the run (its proof is checked offline, not here); ``not_covered`` when
      none was, or it is still pending.
    """
    from .registry.lineage_api import (  # noqa: PLC0415 — registry-slim, lazy
        _TRACE_COLUMNS, _covering_checkpoints, _receipt_node_from_trace,
    )

    try:
        rid = UUID(str(run_id))
    except (ValueError, TypeError):
        return {"run_id": str(run_id), "found": False}
    row = await conn.fetchrow(
        f"SELECT {_TRACE_COLUMNS}, run_started_at FROM analyst_traces WHERE run_id = $1", rid,
    )
    trace = (await hydrate_traces(conn, [row]))[0] if row is not None else None
    if trace is None:
        return {
            "run_id": str(rid), "found": False,
            "receipt": {"state": "not_covered"},
            "checkpoint": {"state": "not_covered"},
            "anchor": {"state": "not_covered"},
        }
    covering = await _covering_checkpoints(conn, [trace])
    exact = covering.get((trace["analyst_id"], trace["receipt_hash"]))
    node = _receipt_node_from_trace(trace, covering_checkpoint=exact)
    ended = trace["run_ended_at"] or trace["run_started_at"]

    receipt = {
        "state": "verified" if node.chain_consistent else "failed",
        "receipt_hash": node.receipt_hash,
        "prev_receipt_hash": node.prev_receipt_hash,
    }
    if node.signer_did:
        checkpoint: dict[str, Any] = {"state": "verified", "signer_did": node.signer_did, "exact": True}
    else:
        later = await conn.fetchrow(_LATER_CHECKPOINT_SQL, trace["analyst_id"], ended)
        checkpoint = (
            {"state": "not_covered"} if later is None else {
                "state": "covered", "exact": False,
                "checkpointed_at": _iso(later["checkpointed_at"]),
                "signer_did": later["signer_did"],
                "trace_count": int(later["trace_count"]),
            }
        )
    anchor: dict[str, Any] = {"state": "not_covered"}
    if await _has_table(conn, "receipt_anchors"):
        rows = await conn.fetch(_ANCHOR_SQL, ended)
        submitted = [r for r in rows if r["status"] == "submitted"]
        if submitted:
            first = submitted[0]
            anchor = {
                "state": "covered", "day": _iso(first["day"]),
                "calendars": len(submitted), "submitted_at": _iso(first["submitted_at"]),
            }
        elif rows:
            anchor = {"state": "not_covered", "pending": True, "day": _iso(rows[0]["day"])}
    return {
        "run_id": str(rid),
        "found": True,
        "unit": trace["analyst_id"],
        "run_ended_at": _iso(ended),
        "receipt": receipt,
        "checkpoint": checkpoint,
        "anchor": anchor,
    }
