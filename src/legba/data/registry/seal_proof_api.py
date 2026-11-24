# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/ledger/seal-proof`` — a forecast's seal, proven inside a daily anchor (F-1).

A sealed forecast is a question hash and one hash per stated number
(``forecast_ledger``: ``content_sha256``). The daily ``receipt_anchor`` folds
every ledger hash into one Merkle root and timestamps the root outside. From
migration 0262 on it also stores the ordered leaves of the tree it anchored
(``anchor_proof.store_tree``), so this route can PROVE a leaf is in a root:

  * for the question: its leaf (``sha256('forecast_question:' id ||
    content_sha256)``), the leaf's input text, and — when a stored tree holds
    it — the first such anchor's day, the leaf's position, the sibling path
    (``[{side, hash}]`` from the leaf up), the root, and the day's
    external-timestamp rows as the system knows them (submitted / pending,
    never verified here);
  * the same for each stated number, against the same day's tree.

The console re-computes the leaf from the hash the Ledger route serves, folds
the path itself and compares the result with the root the anchors route lists
— it says "included in the anchor of <day>" only when that holds.

``included: false`` is said with the first day whose leaves are stored: a
forecast sealed before the first stored day was anchored by its root only
(the root is timestamped; inclusion was not provable before 0262).

Read-only, bearer-gated, the reader role. A missing 0262 (or any read
failure) answers ``available: false`` at HTTP 200; an unknown question 404.
Registry-slim: stdlib + fastapi + the leaf ledger modules.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..anchor_proof import inclusion_proof, leaf_input
from ..forecast_ledger import ledger_leaf
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

SEAL_PROOF_ROUTE_VERSION = "2026-11/f1"
_ROUTE = "/ledger/seal-proof"
_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

QUESTION_SQL = """
SELECT id::text AS id, slug, content_sha256, sealed_at
  FROM forecast_questions WHERE id = $1::uuid
"""
NUMBERS_SQL = """
SELECT id::text AS id, issuer_kind, issuer_ref, content_sha256
  FROM forecast_numbers WHERE question_id = $1::uuid
 ORDER BY issuer_kind, issuer_ref
"""


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    iso = getattr(v, "isoformat", None)
    return iso() if callable(iso) else str(v)


async def _prove(conn: Any, kind: str, row_id: str, sha: str, *, day: Optional[date],
                 cache: dict[Any, Any]) -> dict[str, Any]:
    leaf = ledger_leaf(kind, row_id, sha).hex()
    proof = await inclusion_proof(conn, leaf, day=day, cache=cache)
    return {"kind": kind, "id": row_id, "content_sha256": sha,
            "leaf_input": leaf_input(kind, row_id, sha), **proof}


def build_seal_proof_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["ledger"])

    def _pool(request: Request) -> Any:
        return getattr(request.app.state, "registry_deps", deps).descriptor_registry.pg

    @router.get(_ROUTE)
    async def ledger_seal_proof(
        request: Request,
        question_id: str = Query(..., pattern=_UUID),
        day: Optional[date] = Query(default=None, description="prove against this anchored day"),
        _bearer: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        qid = question_id.lower()
        base: dict[str, Any] = {"version": SEAL_PROOF_ROUTE_VERSION, "question_id": qid}
        try:
            async with _pool(request).acquire() as conn:
                q = await conn.fetchrow(QUESTION_SQL, qid)
                if q is None:
                    raise HTTPException(status_code=404, detail={"error": "unknown_question",
                                                                 "message": f"no forecast question {qid}"})
                cache: dict[Any, Any] = {}
                question = await _prove(conn, "forecast_question", q["id"], str(q["content_sha256"]),
                                        day=day, cache=cache)
                on_day = date.fromisoformat(question["day"]) if question.get("included") else day
                numbers = [
                    {**await _prove(conn, "forecast_number", n["id"], str(n["content_sha256"]),
                                    day=on_day, cache=cache),
                     "issuer_kind": n["issuer_kind"]}
                    for n in await conn.fetch(NUMBERS_SQL, qid)
                ]
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — 0262 not applied / unreadable: said, HTTP 200
            logger.info("v3.ledger.seal_proof.unavailable err_class=%s", type(exc).__name__)
            return {**base, "available": False}
        return {
            **base, "available": True, "slug": q["slug"], "sealed_at": _iso(q["sealed_at"]),
            "question": question, "numbers": numbers,
            "first_stored_day": question.get("first_stored_day") if not question.get("included") else None,
        }

    return router
