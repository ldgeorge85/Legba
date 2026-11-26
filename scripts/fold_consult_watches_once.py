#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fold the consult watches minted before the semantic fold landed (W4-F1, one-off).

WHY. Until the semantic fold, a consult's watch candidate folded onto a standing watch only on
IDENTICAL normalised text, so a re-asked question re-phrased by the model minted
a second row (live 2026-10-03 on country_watch_ir: e6227510 "major-arms-
acquisition indicator observed" and 53d11647 "detection of a major arms
acquisition (e.g., new missile system", both ``by 2026-10-17``). The mint now
folds such a candidate at mint time; this script folds the rows that already
stand.

WHAT IT DOES. Per desk (``target_id``; desk-less watches are their own bucket),
the OPEN consult watches not yet folded, oldest first. Each row is decided
against the rows before it that stay standing, by the SAME code the mint uses
(``consult_watch_fold.claim_core`` / ``decide_fold``: same desk, horizon within
±3 days, identical claim cores or core cosine >= ``FOLD_COSINE_THRESHOLD``). A
row that folds is stamped by ``consult_watch_fold.mark_folded``: ``folded_onto``
(the earlier row), ``fold_similarity``, ``fold_check``, ``folded_at`` on its
marker. NOTHING IS DELETED and the status is unchanged here — the
``watch_horizon_sweep`` handler (W5) closes a stamped row (``status='withdrawn'``,
reason ``folded``) on its first tick, so until that descriptor is active
claim_watch keeps watching both rows; the stamp is the record that they are one
watch.

DRY-RUN BY DEFAULT — prints counts (and the folded id pairs), writes nothing.
``--apply`` stamps. Idempotent: a folded row is never a candidate again, so a
second run folds nothing. ``--no-embedder`` decides on identical claim cores
only (the live arms pair folds that way); without it the embedder is built from
the registry (``embed.primary.openai_compat``) exactly as the runtime builds it,
and a failure to build or call it degrades to cores-only and says so.

Run inside the live runtime container after the roll that ships this:

    docker exec -i legba-legba-runtime-dapr-1 python3 - < scripts/fold_consult_watches_once.py
    docker exec -i legba-legba-runtime-dapr-1 python3 - --apply < scripts/fold_consult_watches_once.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any, Sequence

from legba.data.analysts.consult_watch_fold import (
    FoldPeer,
    claim_core,
    decide_fold,
    embed_batch,
    mark_folded,
)

_MARKER = "open_question_origin"

# Every OPEN, not-yet-folded consult watch whose horizon has not passed, oldest
# first within its desk. Bounded; the live set is a handful of rows.
_ROWS_SQL = """
    SELECT h.id, h.target_id, h.thesis, h.produced_at, m.e AS marker
      FROM hypotheses h
      CROSS JOIN LATERAL (
            SELECT e FROM jsonb_array_elements(h.diagnostic_evidence) e
             WHERE e ->> 'marker' = 'open_question_origin' AND e ->> 'origin' = 'consult'
             LIMIT 1
      ) m
     WHERE h.status = 'open_question'
       AND h.diagnostic_evidence @> $1::jsonb
       AND NOT (m.e ? 'folded_onto')
       AND (m.e ->> 'resolves_by')::timestamptz > now()
       AND ($2::text[] IS NULL OR h.target_id = ANY($2::text[]))
     ORDER BY h.target_id NULLS FIRST, h.produced_at, h.id
     LIMIT 2000
"""

#: Texts per embed call (the endpoint's configured batch size).
_EMBED_CHUNK = 64


def _marker(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    return dict(raw) if isinstance(raw, dict) else {}


async def _embed_all(embedder: Any, texts: Sequence[str]) -> list[list[float]] | None:
    out: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_CHUNK):
        got = await embed_batch(embedder, texts[start:start + _EMBED_CHUNK], timeout=60.0)
        if got is None:
            return None
        out.extend(got)
    return out


async def fold_once(
    conn: Any,
    *,
    embedder: Any | None,
    apply: bool,
    now: datetime | None = None,
    desks: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Decide (and with ``apply`` stamp) every standing consult watch. Returns
    the counts and the ``(row, onto, check, similarity)`` folds."""
    now = now or datetime.now(timezone.utc)
    probe = json.dumps([{"marker": _MARKER, "origin": "consult"}])
    rows = await conn.fetch(_ROWS_SQL, probe, list(desks) if desks else None)
    by_desk: dict[str | None, list[FoldPeer]] = {}
    for r in rows:
        m = _marker(r["marker"])
        try:
            horizon = datetime.fromisoformat(str(m.get("resolves_by")).replace("Z", "+00:00"))
        except ValueError:
            continue
        by_desk.setdefault(r["target_id"], []).append(FoldPeer(
            id=str(r["id"]), core=claim_core(str(m.get("resolution_test") or r["thesis"])),
            resolves_by=horizon, produced_at=r["produced_at"],
        ))
    embedded = embedder is not None
    if embedded:
        allrows = [p for peers in by_desk.values() for p in peers]
        vecs = await _embed_all(embedder, [p.core for p in allrows])
        if vecs is None:
            embedded = False
        else:
            for p, v in zip(allrows, vecs):
                p.vec = v
    folds: list[dict[str, Any]] = []
    unavailable = 0
    for _desk, peers in by_desk.items():
        standing: list[FoldPeer] = []
        for row in peers:
            decision = decide_fold(
                row.core, row.vec, row.resolves_by, standing, embedded=embedded,
            )
            if decision.onto is None:
                unavailable += decision.check == "unavailable"
                standing.append(row)
                continue
            fold = {"row": row.id, "onto": decision.onto, "check": decision.check,
                    "similarity": decision.similarity, "stamped": False}
            if apply:
                fold["stamped"] = await mark_folded(
                    conn, row.id, onto=decision.onto, similarity=decision.similarity,
                    check=decision.check, now=now,
                )
            folds.append(fold)
    return {
        "mode": "apply" if apply else "dry-run",
        "scanned": sum(len(p) for p in by_desk.values()),
        "desks": len(by_desk),
        "embedder": "used" if embedded else ("failed" if embedder is not None else "none"),
        "folds": len(folds),
        "stamped": sum(1 for f in folds if f["stamped"]),
        "undecidable_without_embedder": unavailable,
        "pairs": folds,
    }


async def _build_embedder(pg: Any) -> Any | None:
    try:
        from legba.data.registry.credentials import CredentialVault
        from legba.runtime.embedding_factory import (
            build_embedding_service_from_stack_component,
        )
        from legba.runtime.registry_client import RegistryHTTPClient

        vault = CredentialVault(pg)

        async def _resolve(secret_id: str) -> bytes:
            return await vault.resolve(secret_id)

        return await build_embedding_service_from_stack_component(
            "embed.primary.openai_compat",
            registry_client=RegistryHTTPClient(), secrets_resolve=_resolve,
        )
    except Exception as exc:  # noqa: BLE001 — cores-only, said out loud
        print(f"embedder unavailable ({type(exc).__name__}: {exc}); deciding on "
              "identical claim cores only", file=sys.stderr)
        return None


async def _main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="stamp the folds (default: dry-run)")
    ap.add_argument("--no-embedder", action="store_true", help="identical claim cores only")
    ap.add_argument("--desk", action="append", default=None, help="limit to these target_ids")
    args = ap.parse_args(list(argv))

    from legba.data.config import PostgresConfig
    from legba.data.postgres import PostgresStore

    pg = PostgresStore(PostgresConfig.from_env())
    await pg.connect()
    try:
        embedder = None if args.no_embedder else await _build_embedder(pg)
        async with pg.pool.acquire() as conn:
            result = await fold_once(
                conn, embedder=embedder, apply=args.apply, desks=args.desk,
            )
    finally:
        await pg.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
