# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 — the ONE writer for ``unit_references``, shared by the job and the CLI.

``scripts/load_unit_reference.py`` (G1's deploy step) and the scheduled
``reference_builder`` sub-handler (R2) both put a reference row into the table
the correctness grader reads. Before this module they would have been two
writers, and two writers of one row is how ``thin_dimensions`` comes to mean one
thing when a human loads a file and a different thing when the job writes one —
at which point the grader's ``thin`` contract quietly depends on WHO built the
reference. So the loader now imports every rule below and owns none of them.

WHAT IS DERIVED HERE AND WHAT IS NOT (unchanged from the loader's own rules,
moved verbatim, because they were already right):

  * ``thin_dimensions`` IS derived — the dimensions carrying fewer than
    ``below`` developments, counted over the dimension list the reference's OWN
    band table declares, never a platform constant. A reference that banded
    seven dimensions is thin on the seven it banded.
  * ``span_verified_rate`` is NOT derived and NOT guessed. It is the BUILDER's
    number: only the builder held the archived page text a decisive span is
    checked against, and re-deriving it from the file would measure the file
    against itself. Absent ⇒ NULL, which is not zero.
  * The window is never invented. It comes from ``header.window`` ("A -> B") or
    from explicit arguments; a reference whose window nobody stated cannot be
    matched to an as-of stamp.

IDEMPOTENCE is the schema's, not a convention: ``(target_id, sha256)`` is
unique, so re-writing the same bytes is a no-op and editing the reference mints
a NEW row rather than mutating the old one. A reference a published share was
computed against must stay readable exactly as it was, or the share stops being
re-arguable.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

#: The insert. Mirrors migration 0196's column order exactly.
INSERT_REFERENCE_SQL = """
INSERT INTO unit_references (
    id, target_id, window_start, window_end, built_at, builder, ref_json,
    span_verified_rate, thin_dimensions, sha256
) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::text[], $10)
ON CONFLICT (target_id, sha256) DO NOTHING
RETURNING id
"""

#: Read one reference back by id — the top-up merge's input (``--merge-into``).
SELECT_REFERENCE_SQL = """
SELECT id, target_id, window_start, window_end, built_at, builder, ref_json,
       span_verified_rate, thin_dimensions, sha256
  FROM unit_references
 WHERE id = $1
"""

#: The most recent reference for a target, whatever its window. The scheduler
#: reads it to decide whether this target is DUE (cadence = every N days since
#: the last build), which is what staggers a 33-target roster without a table.
LATEST_REFERENCE_SQL = """
SELECT id, window_start, window_end, built_at, builder, sha256
  FROM unit_references
 WHERE target_id = $1
 ORDER BY built_at DESC, created_at DESC
 LIMIT 1
"""

#: A dimension with fewer than this many VERIFIED developments is thin. Two is
#: the number G1's loader shipped and the number the R1 verdict argued for: one
#: development is an anecdote, and a dimension resting on an anecdote makes
#: every claim on it structurally likelier to read `silent`.
DEFAULT_THIN_BELOW = 2


def canonical_sha256(reference: Mapping[str, Any]) -> str:
    """The reference's identity: sha256 over a CANONICAL JSON encoding.

    The loader hashes the FILE's bytes, which is right for a file — the artefact
    is the bytes on disk. A job has no file, so it hashes the object under a
    fixed encoding (sorted keys, compact separators, UTF-8, no ASCII escaping).
    Both land in the same column and both are stable under re-reading their own
    input; what neither may be is accidental, so the encoding is pinned here
    rather than left to whatever ``json.dumps`` defaults to on the day.
    """
    blob = json.dumps(
        reference, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def iso(value: Any) -> datetime:
    """An ISO instant, tz-aware. Naive input is read as UTC."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_window(
    reference: Mapping[str, Any],
    start: str | None = None,
    end: str | None = None,
) -> tuple[datetime, datetime]:
    """``(window_start, window_end)`` from the arguments, else the header's own.

    No default. A reference whose window nobody stated cannot be matched to an
    as-of stamp, and inventing one would silently point the grader at the wrong
    fortnight.
    """
    if start and end:
        return iso(start), iso(end)
    header = reference.get("header") or {}
    raw = str(header.get("window") or "")
    if "->" in raw:
        left, right = (part.strip() for part in raw.split("->", 1))
        return iso(left), iso(right)
    raise ValueError(
        "cannot determine the reference's window: pass an explicit start and "
        "end, or give the file a header.window of the form '<iso> -> <iso>'. "
        "Refusing to invent a window for a reference the grader will match "
        "against an as-of stamp."
    )


def dev_dimensions(dev: Mapping[str, Any]) -> list[str]:
    """The dimensions ONE development bears on.

    ``dimension`` is a LIST in the shape R3 settled on — a strike on a disputed
    border bears on escalation AND military_posture, and forcing it to pick one
    would undercount both. A bare string is accepted too, because a hand-written
    reference may carry one and silently reading it as zero dimensions would
    mark every dimension thin.
    """
    raw = dev.get("dimension")
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [str(d).strip() for d in raw if str(d).strip()]
    return []


def _verified(dev: Mapping[str, Any]) -> bool:
    """Does this development COUNT toward its dimensions' depth?

    A development the builder marked ``span_verified: false`` is evidence of
    nothing — the quote is not in the page it cites. A reference that has never
    been span-checked carries no such key at all, and there every development
    counts (the loader's historical behaviour, preserved so loading
    ``ref_IL_A.json`` still computes the thin set it always did).
    """
    return dev.get("span_verified") is not False


def thin_dimensions(
    reference: Mapping[str, Any], below: int = DEFAULT_THIN_BELOW
) -> list[str]:
    """Dimensions carrying fewer than ``below`` verified developments.

    The dimension list is the reference's OWN band table. A dimension the
    reference never banded is not thin, it is absent, and those are different
    facts about the instrument.
    """
    bands = reference.get("ref_bands") or {}
    dims = sorted(bands) if isinstance(bands, dict) else []
    counts: dict[str, int] = {d: 0 for d in dims}
    for dev in reference.get("ref_developments") or []:
        if not _verified(dev):
            continue
        for dim in dev_dimensions(dev):
            if dim in counts:
                counts[dim] += 1
    return [d for d in dims if counts[d] < below]


def span_verified_rate(
    reference: Mapping[str, Any], override: float | None = None
) -> float | None:
    """The BUILDER's number, or ``None``. Never re-derived — see the banner."""
    if override is not None:
        return override
    header = reference.get("header") or {}
    raw = header.get("span_verified_rate")
    if raw is None:
        verification = header.get("span_verification")
        if isinstance(verification, Mapping):
            total = verification.get("candidates", verification.get("total"))
            done = verification.get("verified")
            try:
                total_i, done_i = int(total), int(done)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
            return round(done_i / total_i, 6) if total_i > 0 else None
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def write_reference(
    conn: Any,
    *,
    target_id: str,
    reference: Mapping[str, Any],
    window_start: datetime,
    window_end: datetime,
    built_at: datetime,
    builder: str,
    sha256: str | None = None,
    rate: float | None = None,
    thin_below: int = DEFAULT_THIN_BELOW,
) -> tuple[UUID | None, dict[str, Any]]:
    """INSERT one reference. Returns ``(id | None, derived)``.

    ``None`` means the row was already there byte-for-byte — the unique index
    is the idempotence, not a handler convention. ``derived`` carries what this
    function computed (the sha, the thin set, the rate) so a caller can put the
    same numbers on a receipt without recomputing them differently.
    """
    digest = sha256 or canonical_sha256(reference)
    thin = thin_dimensions(reference, thin_below)
    resolved_rate = span_verified_rate(reference, rate)
    derived = {
        "sha256": digest,
        "thin_dimensions": thin,
        "span_verified_rate": resolved_rate,
        "n_developments": len(reference.get("ref_developments") or []),
        "n_bands": len(reference.get("ref_bands") or {}),
    }
    row = await conn.fetchrow(
        INSERT_REFERENCE_SQL,
        uuid4(),
        target_id,
        window_start,
        window_end,
        built_at,
        builder,
        json.dumps(reference, ensure_ascii=False),
        None if resolved_rate is None else Decimal(str(resolved_rate)),
        thin,
        digest,
    )
    return (row["id"] if row is not None else None), derived


def merge_developments(
    base: Sequence[Mapping[str, Any]],
    addition: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Union two development lists BY MATTER, keeping the base's item on a tie.

    The top-up lane is told "these are already established — find what is
    MISSING", and it still sometimes re-reports one. A merge that appended
    blindly would double-count that matter in every per-dimension depth count
    and in ``thin``, which is the one number the grader reads structurally.

    MATTER IDENTITY is deliberately conservative: same normalised source URL AND
    same publish date, or the same decisive span. It will not collapse two
    genuinely different reports of one event at two outlets — which is correct
    for a reference, where two independent outlets on one matter is corroboration
    the grader should see, not duplication.
    """
    seen_url_date: set[tuple[str, str]] = set()
    seen_span: set[str] = set()
    out: list[dict[str, Any]] = []

    def key_url_date(dev: Mapping[str, Any]) -> tuple[str, str]:
        url = str(dev.get("source_url") or "").strip().rstrip("/").lower()
        return url, str(dev.get("publish_date") or "").strip()[:10]

    def key_span(dev: Mapping[str, Any]) -> str:
        return " ".join(str(dev.get("decisive_span") or "").split()).lower()

    stats = {"base": 0, "added": 0, "duplicates_dropped": 0}
    for source, label in ((base, "base"), (addition, "added")):
        for dev in source:
            ud, span = key_url_date(dev), key_span(dev)
            if (ud[0] and ud in seen_url_date) or (span and span in seen_span):
                stats["duplicates_dropped"] += 1
                continue
            if ud[0]:
                seen_url_date.add(ud)
            if span:
                seen_span.add(span)
            out.append(dict(dev))
            stats[label] += 1
    return out, stats


def renumber(
    developments: Sequence[Mapping[str, Any]], prefix: str
) -> list[dict[str, Any]]:
    """Re-stamp ``item_id`` as ``<prefix>-1..n`` in list order.

    A merged list whose ids came from two builders has duplicate ids, and an id
    that does not identify one development is worse than no id at all.
    """
    out: list[dict[str, Any]] = []
    for index, dev in enumerate(developments, 1):
        item = dict(dev)
        item["item_id"] = f"{prefix}-{index}"
        out.append(item)
    return out


__all__ = [
    "DEFAULT_THIN_BELOW",
    "INSERT_REFERENCE_SQL",
    "LATEST_REFERENCE_SQL",
    "SELECT_REFERENCE_SQL",
    "canonical_sha256",
    "dev_dimensions",
    "iso",
    "merge_developments",
    "parse_window",
    "renumber",
    "span_verified_rate",
    "thin_dimensions",
    "write_reference",
]
