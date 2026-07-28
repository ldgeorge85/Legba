# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""source_ratings_loader — catalog-seed machinery for the source assurance ledger.

P3-1 (A6 layers 1+2). Ingests a curated YAML of per-source rubric grades and
upserts them into ``source_ratings`` (migration 0094) as
``method='catalog_seed'``, ``visibility_class='public'`` rows.

House seed pattern: the MACHINERY ships in-repo (this module +
``scripts/seed_source_ratings.py``); the curated DATA does not. The default
input path is ``seeds/source_ratings.yaml``, which is gitignored per the
existing ``seeds/*.yaml`` rule; the tracked schema doc is
``seeds/source_ratings.example.yaml`` (FAKE example sources only). A missing
data file degrades gracefully — warn + empty summary, never a crash (the
``world_baseline`` adapter precedent).

Upsert semantics (rating history, not overwrite):

  * identity of a CURRENT rating = ``(source_id, rater, visibility_class)``
    (partial unique index over ``superseded_by IS NULL`` rows);
  * content-identical re-run → ``unchanged`` (idempotent, no new row);
  * changed content → INSERT a new current row and stamp the old row's
    ``superseded_by`` with the new id — history is the chain, current is
    ``superseded_by IS NULL``. Ordering is UPDATE-old-then-INSERT-new inside
    one transaction (the deferred self-FK exists for exactly this).

HARD rule carried from the migration: nothing here (or anywhere yet) feeds
these grades into the faithfulness score — display + later weighting only.

YAML shape (see ``seeds/source_ratings.example.yaml``)::

    version: 1
    ratings:
      - source_id: source.rss.example_wire     # source descriptor id
        rater: "catalog:example-catalog"
        admiralty:
          reliability: B                        # A..F (optional)
          credibility: 2                        # 1..6 (optional; int or str)
        rubric:                                 # typed keys, all optional
          type: news_agency
          ownership: "Example Media Group"
          state_affiliation: none
          editorial_posture: "centrist wire service"
          bias_notes: "…"
        references:
          - url: "https://example.org/entry"
            title: "Example directory entry"
        rated_at: "2026-07-01"                  # optional, YYYY-MM-DD or ISO
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml

logger = logging.getLogger(__name__)

# Repo-root-relative default (…/legba/seeds/source_ratings.yaml). This file is
# …/src/legba/data/seed/source_ratings_loader.py → 4 parents up = repo root.
DEFAULT_YAML = Path(__file__).resolve().parents[4] / "seeds" / "source_ratings.yaml"

VALID_RELIABILITY = frozenset("ABCDEF")
VALID_CREDIBILITY = frozenset("123456")

# Documented rubric keys (migration 0094 header). Open by design: unknown keys
# are preserved with a warning, never dropped — private annex raters may extend.
RUBRIC_TYPED_KEYS = (
    "type",
    "ownership",
    "state_affiliation",
    "editorial_posture",
    "bias_notes",
)


@dataclass(frozen=True)
class RatingSpec:
    """One validated curated rating, ready to upsert."""

    source_id: str
    rater: str
    admiralty_reliability: str | None
    admiralty_credibility: str | None
    rubric: dict[str, Any] = field(default_factory=dict)
    references: list[dict[str, Any]] = field(default_factory=list)
    rated_at: datetime | None = None  # None → NOW() at write time


@dataclass
class LoadResult:
    """Summary of one loader run (the bulk-upload errors-list house pattern)."""

    inserted: int = 0
    superseded: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "superseded": self.superseded,
            "unchanged": self.unchanged,
            "errors": list(self.errors),
        }


def _parse_rated_at(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    # YAML may hand us a date object's str() or an ISO timestamp.
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_ratings_yaml(path: Path) -> tuple[list[RatingSpec], list[str]]:
    """Parse + validate a curated ratings YAML.

    Returns ``(specs, errors)`` — bad rows are reported and SKIPPED, the rest
    of the file still loads (the CSV bulk-upload house pattern: one bad row
    never aborts the import).
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = raw.get("ratings") or []
    specs: list[RatingSpec] = []
    errors: list[str] = []
    for i, row in enumerate(rows, start=1):
        try:
            specs.append(_parse_row(row, i))
        except ValueError as exc:
            errors.append(str(exc))
    return specs, errors


def _parse_row(row: Any, n: int) -> RatingSpec:
    if not isinstance(row, dict):
        raise ValueError(f"row {n}: not a mapping")
    source_id = str(row.get("source_id") or "").strip()
    if not source_id:
        raise ValueError(f"row {n}: missing source_id")
    rater = str(row.get("rater") or "").strip()
    if not rater:
        raise ValueError(f"row {n} ({source_id}): missing rater")

    admiralty = row.get("admiralty") or {}
    if not isinstance(admiralty, dict):
        raise ValueError(f"row {n} ({source_id}): admiralty must be a mapping")
    reliability = admiralty.get("reliability")
    reliability = str(reliability).strip().upper() if reliability is not None else None
    if reliability is not None and reliability not in VALID_RELIABILITY:
        raise ValueError(
            f"row {n} ({source_id}): admiralty.reliability {reliability!r} not in A..F"
        )
    credibility = admiralty.get("credibility")
    credibility = str(credibility).strip() if credibility is not None else None
    if credibility is not None and credibility not in VALID_CREDIBILITY:
        raise ValueError(
            f"row {n} ({source_id}): admiralty.credibility {credibility!r} not in 1..6"
        )

    rubric = row.get("rubric") or {}
    if not isinstance(rubric, dict):
        raise ValueError(f"row {n} ({source_id}): rubric must be a mapping")
    unknown = sorted(set(rubric) - set(RUBRIC_TYPED_KEYS))
    if unknown:
        logger.warning(
            "source_ratings seed: row %d (%s) has non-typed rubric keys %s "
            "(kept verbatim; documented keys: %s)",
            n, source_id, unknown, ", ".join(RUBRIC_TYPED_KEYS),
        )

    references = row.get("references") or []
    if not isinstance(references, list) or not all(
        isinstance(r, dict) for r in references
    ):
        raise ValueError(
            f"row {n} ({source_id}): references must be a list of mappings"
        )

    try:
        rated_at = _parse_rated_at(row.get("rated_at"))
    except ValueError as exc:
        raise ValueError(
            f"row {n} ({source_id}): bad rated_at {row.get('rated_at')!r}: {exc}"
        ) from exc

    return RatingSpec(
        source_id=source_id,
        rater=rater,
        admiralty_reliability=reliability,
        admiralty_credibility=credibility,
        rubric=dict(rubric),
        references=list(references),
        rated_at=rated_at,
    )


def _content_key(
    reliability: str | None,
    credibility: str | None,
    rubric: dict[str, Any],
    references: list[dict[str, Any]],
) -> str:
    """Canonical content fingerprint for the idempotency check."""
    return json.dumps(
        [reliability, credibility, rubric, references],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def _as_obj(value: Any) -> Any:
    """jsonb fetch → Python object, codec-agnostic.

    The house ``PostgresStore`` pool registers a jsonb codec (fetches arrive
    as dict/list); a raw ``asyncpg.connect`` does not (fetches arrive as str).
    Writes are uniform — ``json.dumps``-ed strings pass through the codec
    untouched — so only the read path needs this shim.
    """
    return json.loads(value) if isinstance(value, str) else value


async def upsert_rating(
    conn: Any,
    spec: RatingSpec,
    *,
    method: str = "catalog_seed",
    visibility_class: str = "public",
) -> str:
    """Upsert ONE rating with supersession history.

    Returns ``'inserted'`` (no prior current row), ``'unchanged'``
    (content-identical current row — idempotent no-op), or ``'superseded'``
    (prior current row chained behind the new one).

    ``conn`` is an asyncpg connection; runs in its own transaction so the
    UPDATE-old / INSERT-new pair is atomic under the current-row unique index
    (the deferred self-FK lets the old row point at the not-yet-inserted new
    id until commit).
    """
    async with conn.transaction():
        current = await conn.fetchrow(
            """
            SELECT id, admiralty_reliability, admiralty_credibility,
                   rubric, refs
              FROM source_ratings
             WHERE source_id = $1 AND rater = $2 AND visibility_class = $3
               AND superseded_by IS NULL
             FOR UPDATE
            """,
            spec.source_id, spec.rater, visibility_class,
        )
        if current is not None:
            existing_key = _content_key(
                current["admiralty_reliability"],
                current["admiralty_credibility"],
                _as_obj(current["rubric"]),
                _as_obj(current["refs"]),
            )
            new_key = _content_key(
                spec.admiralty_reliability,
                spec.admiralty_credibility,
                spec.rubric,
                spec.references,
            )
            if existing_key == new_key:
                return "unchanged"

        new_id: UUID = uuid4()
        if current is not None:
            # Supersede FIRST so the partial unique index never sees two
            # current rows; the deferred self-FK tolerates the forward pointer.
            await conn.execute(
                "UPDATE source_ratings SET superseded_by = $1 WHERE id = $2",
                new_id, current["id"],
            )
        await conn.execute(
            """
            INSERT INTO source_ratings
              (id, source_id, rater, visibility_class, method,
               admiralty_reliability, admiralty_credibility,
               rubric, refs, rated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7,
                    $8::jsonb, $9::jsonb, COALESCE($10, NOW()))
            """,
            new_id, spec.source_id, spec.rater, visibility_class, method,
            spec.admiralty_reliability, spec.admiralty_credibility,
            json.dumps(spec.rubric), json.dumps(spec.references),
            spec.rated_at,
        )
        return "superseded" if current is not None else "inserted"


async def seed_source_ratings(
    conn: Any,
    yaml_path: Path | str | None = None,
) -> LoadResult:
    """Load the curated ratings YAML and upsert every valid row.

    Missing file → warn + empty result (graceful degrade, the house adapter
    pattern — Legba ships no bundled seed data; see seeds/README.md).
    """
    path = Path(yaml_path) if yaml_path else DEFAULT_YAML
    result = LoadResult()
    if not path.exists():
        logger.warning(
            "source_ratings seed: no seed file at %s — skipping; Legba ships "
            "no bundled seed data, provide your own (see seeds/README.md)",
            path,
        )
        return result

    specs, errors = parse_ratings_yaml(path)
    result.errors.extend(errors)
    for spec in specs:
        try:
            outcome = await upsert_rating(conn, spec)
        except Exception as exc:  # keep loading; report the row
            result.errors.append(f"{spec.source_id} ({spec.rater}): {exc}")
            continue
        if outcome == "inserted":
            result.inserted += 1
        elif outcome == "superseded":
            result.superseded += 1
        else:
            result.unchanged += 1
    logger.info(
        "source_ratings seed: %s — inserted=%d superseded=%d unchanged=%d errors=%d",
        path, result.inserted, result.superseded, result.unchanged,
        len(result.errors),
    )
    return result
