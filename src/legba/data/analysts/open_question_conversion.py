# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-2b — post-persist OPEN-QUESTION CONVERSION (payload → ``hypotheses`` rows).

The unit is a single-shot JSON emitter (no tool loop), so its unresolved
questions arrive as the OPTIONAL ``data.open_questions`` payload block
(validated by ``schemas.analyst.validate_open_questions``). The actor run path
calls :func:`convert_open_questions` AFTER the finding row lands
(``runtime.dapr_actors`` — the same connection) to turn each entry into the SAME
first-class, queryable object the ``open_question`` agency tool writes: a
``hypotheses`` row with ``status='open_question'``, lineage to the resolved
citation signals plus the producing finding, and the unit + target stamped via
the run's ``AnalystContext``.

Contract, unchanged by the move:

  * **DEGRADE-NOT-BREAK** — no failure in here may fail the run; the caller
    wraps the call, and each entry is additionally isolated so one bad entry
    never sinks its siblings.
  * **IDEMPOTENT per (finding, question-text)** — a durable marker object in the
    persisted ``diagnostic_evidence`` jsonb (the ``hypotheses`` table has no
    ``data`` column; ``writes._insert_hypothesis`` drops payload extras), the
    same ``open_question_origin`` marker key the K-2a harvest uses, deduped by
    jsonb containment.

WHY IT LIVES HERE. Extracted from ``inline_target.py`` 2026-08-30 (the [N+1]
consumer-repair train), which found that module at 3,909 lines against a 3,915
DO-NOT-RAISE ceiling and the train about to spend four of the six remaining
lines. This block was the seam: it carries its own section banner, its own
dedicated test file (``tests/data_pkg/test_open_question_faucet.py``), it runs
POST-persist — after ``run_method`` has returned, on the actor's connection, so
it is not part of the analyst pass at all — and its only edges outward are
``MAX_OPEN_QUESTIONS`` (a schema constant) and two lazily-imported provenance
writers.

THE IMPORT EDGE is one way and shallow: this module imports nothing from
``inline_target``. ``inline_target`` RE-EXPORTS both public names, so
``inline_target.convert_open_questions`` and
``inline_target.OPEN_QUESTION_MARKER_KEY`` resolve exactly as before — which
matters because the caller is ``runtime.dapr_actors`` (frozen) and the existing
test file imports them from there.

ONE THING DID CHANGE, recorded rather than left to be discovered: the
``logging.getLogger(__name__)`` name on the degrade path is now this module's,
not ``inline_target``'s. The log MESSAGE key —
``inline_target.open_question.convert_entry_failed`` — is deliberately kept
verbatim, because that string is the ops-queryable handle and the logger name
is not.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping
from uuid import UUID

from ..schemas.analyst import MAX_OPEN_QUESTIONS

logger = logging.getLogger(__name__)


#: The shared marker key — one containment-queryable vocabulary for every
#: harvested/converted open-question row (K-2a uses origin='harvest').
OPEN_QUESTION_MARKER_KEY = "open_question_origin"


def _citation_signal_map(finding_data: Mapping[str, Any]) -> dict[int, UUID]:
    """``{N -> signal UUID}`` from the finding's persisted ``data.citations``."""
    out: dict[int, UUID] = {}
    citations = finding_data.get("citations")
    if not isinstance(citations, list):
        return out
    for c in citations:
        if not isinstance(c, Mapping):
            continue
        marker = str(c.get("marker") or "")
        m = re.fullmatch(r"\[(\d+)\]", marker)
        if not m:
            continue
        try:
            out[int(m.group(1))] = UUID(str(c.get("signal_id")))
        except (TypeError, ValueError):
            continue
    return out


async def convert_open_questions(
    conn: Any,
    *,
    finding_data: Mapping[str, Any] | None,
    finding_id: UUID,
    analyst_ctx: Any,
) -> int:
    """Convert a persisted finding's ``data.open_questions`` into hypotheses rows.

    Returns how many rows were written (0 when the block is absent/empty, every
    entry already exists, or every entry degraded). Never raises on malformed
    payload data; an unexpected substrate error propagates to the caller's
    degrade guard (the actor wraps this call in try/except).
    """
    import hashlib

    from ..provenance.models import HypothesisPayload
    from ..provenance.writes import write_hypothesis

    if not isinstance(finding_data, Mapping):
        return 0
    entries = finding_data.get("open_questions")
    if not isinstance(entries, list) or not entries:
        return 0
    signal_by_ref = _citation_signal_map(finding_data)

    written = 0
    for entry in entries[:MAX_OPEN_QUESTIONS]:
        try:
            if not isinstance(entry, Mapping):
                continue
            question = str(entry.get("question") or "").strip()
            if not question:
                continue
            qhash = hashlib.sha256(question.lower().encode("utf-8")).hexdigest()[:16]
            source_id = f"{finding_id}:{qhash}"
            probe = json.dumps([{
                "marker": OPEN_QUESTION_MARKER_KEY,
                "origin": "unit_payload",
                "source_id": source_id,
            }])
            exists = await conn.fetchval(
                "SELECT 1 FROM hypotheses "
                "WHERE status = 'open_question' "
                "AND diagnostic_evidence @> $1::jsonb LIMIT 1",
                probe,
            )
            if exists is not None:
                continue
            # Lineage: the producing finding + every citation signal the
            # question's refs resolve to (an unresolvable ref degrades to
            # finding-only lineage — counted nowhere, fabricated never).
            derived: list[UUID] = [finding_id]
            refs_raw = entry.get("refs")
            refs: list[int] = []
            if isinstance(refs_raw, (list, tuple)):
                for r in refs_raw:
                    try:
                        refs.append(int(r))
                    except (TypeError, ValueError):
                        continue
            for n in refs:
                sid = signal_by_ref.get(n)
                if sid is not None and sid not in derived:
                    derived.append(sid)
            payload = HypothesisPayload(
                thesis=question[:4096],
                status="open_question",
                diagnostic_evidence=[{
                    "marker": OPEN_QUESTION_MARKER_KEY,
                    "origin": "unit_payload",
                    "source_id": source_id,
                    "finding_id": str(finding_id),
                    "question_sha256": qhash,
                    "refs": refs[:32],
                }],
            )
            row, _dlq = await write_hypothesis(
                conn,
                analyst_ctx=analyst_ctx,
                payload=payload,
                derived_from=derived,
            )
            if row is not None:
                written += 1
        except Exception as exc:  # noqa: BLE001 — one bad entry never sinks siblings
            logger.warning(
                "inline_target.open_question.convert_entry_failed finding=%s "
                "err=%s", finding_id, exc,
            )
            continue
    return written
