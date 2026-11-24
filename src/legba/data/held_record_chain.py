# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The held family's status log as a hash chain, and its cache rebuilt
(data-model lane 10, DM-C).

THE DATABASE IS THE AUTHORITY. ``assertion_status_log``'s BEFORE INSERT
trigger (migration ``20261115T1940_held_record``) sets ``seq``,
``prev_hash`` (the previous row's ``row_hash`` for the same assertion;
64 zeros for the first), ``recorded_at`` (the server's time) and ``row_hash``
(``assertion_status_row_hash``). A writer that supplies any of the chain
fields is refused. This module is the INDEPENDENT recomputation:

    row_hash = sha256(utf8("assertion_status_log.v1" | f(assertion_id) |
                           f(seq) | f(prev_hash) | f(status) | f(version_id) |
                           f(reason_code) | f(reason) | f(verdict) |
                           f(caused_by) | f(source_ref) | f(recorded_at)))

where ``f(NULL) = "~"`` and ``f(x) = len(x) ":" x`` (length-prefixed, so no
two field lists collide), ``recorded_at`` as ``YYYY-MM-DDTHH:MM:SS.ffffffZ``
in UTC, and ``|`` joins. :func:`verify_chain` walks each assertion's rows in
``seq`` order and reports the FIRST break per assertion: a gap or reorder in
``seq``, a ``prev_hash`` that is not the previous row's hash, or a
``row_hash`` that does not recompute (an edited field). An edited, deleted,
inserted or reordered row breaks the chain at that row.

THE CACHE. ``assertion_status`` holds each assertion's current status and is
maintained by an AFTER INSERT trigger on the log. :func:`rebuild_cache` derives
it from the log alone; ``scripts/held_record_verify.py`` proves rebuild equals
the stored cache (and, with ``--write``, replaces the cache with the rebuild).

A LEAF: stdlib only (the registry's slim image reads it through the reads).
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "CHAIN_TAG",
    "ZERO_HASH",
    "ChainBreak",
    "LOG_COLUMNS",
    "hash_field",
    "rebuild_cache",
    "row_hash",
    "verify_chain",
]

CHAIN_TAG = "assertion_status_log.v1"
ZERO_HASH = "0" * 64
#: The log columns a verification reads (the reads and the script select these).
LOG_COLUMNS = ("assertion_id, seq, status, version_id, reason_code, reason, verdict, caused_by, "
               "source_ref, recorded_at, prev_hash, row_hash")


def hash_field(value: Any) -> str:
    if value is None:
        return "~"
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    text = str(value)
    return f"{len(text)}:{text}"


def row_hash(row: Mapping[str, Any]) -> str:
    """The hash of one log row as the database computes it."""
    parts = [CHAIN_TAG] + [hash_field(row.get(k)) for k in (
        "assertion_id", "seq", "prev_hash", "status", "version_id", "reason_code", "reason",
        "verdict", "caused_by", "source_ref", "recorded_at")]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChainBreak:
    assertion_id: str
    seq: int
    #: ``seq_gap`` / ``prev_hash_mismatch`` / ``row_hash_mismatch``
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"assertion_id": self.assertion_id, "seq": self.seq, "reason": self.reason}


def _by_assertion(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    out: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r["assertion_id"]), []).append(r)
    for v in out.values():
        v.sort(key=lambda r: int(r["seq"]))
    return out


def verify_chain(rows: Iterable[Mapping[str, Any]]) -> tuple[int, list[ChainBreak]]:
    """``(rows checked, the first break of each broken assertion)``."""
    n = 0
    breaks: list[ChainBreak] = []
    for aid, chain in sorted(_by_assertion(rows).items()):
        prev = ZERO_HASH
        for expected, r in enumerate(chain, start=1):
            n += 1
            seq = int(r["seq"])
            reason = None
            if seq != expected:
                reason = "seq_gap"
            elif r["prev_hash"] != prev:
                reason = "prev_hash_mismatch"
            elif row_hash({**r, "assertion_id": aid}) != r["row_hash"]:
                reason = "row_hash_mismatch"
            if reason:
                breaks.append(ChainBreak(aid, seq, reason))
                break
            prev = r["row_hash"]
    return n, breaks


def rebuild_cache(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{assertion_id: cache row}`` from the log alone: the last row's
    status, version, seq, hash and time, and how many rows."""
    out: dict[str, dict[str, Any]] = {}
    for aid, chain in _by_assertion(rows).items():
        last = chain[-1]
        out[aid] = {"status": last["status"], "version_id": int(last["version_id"]),
                    "last_seq": int(last["seq"]), "last_row_hash": last["row_hash"],
                    "status_at": last["recorded_at"], "entries": len(chain)}
    return out
