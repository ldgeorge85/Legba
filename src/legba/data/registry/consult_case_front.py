# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult front door's CASE half (C-1b; design C-1a §1, §3, §4).

Kept out of ``consult_api.py`` (which must stay under the 1,500-line size-gate
entry threshold) and kept a LEAF: it imports the vocabulary and the record
composer, both leaves, and nothing from the analyst or runtime packages — the
registry ships a slim image.

* :func:`require_case_available` — an ARMED request (``present_case``) on a
  registry where the ``workstation_drive`` pack is not registered, or the
  consult descriptor does not grant it, is refused **409
  ``case_unavailable: workstation_drive not bound``** before anything runs. It
  never runs as a silent plain answer (the no-stubs rule). The runtime checks
  the same thing again (an actor without the binding raises the same words,
  which :func:`is_case_unavailable` maps back to 409).
* :func:`arm` — threads ``present_case`` onto the actor's first input row ONLY
  when it is true, so a plain request's invoke body is byte-identical to
  before; a case turn the client gave no round count defaults to 20.
* :func:`start_accumulator` / :func:`stop_case` — the registry's frame
  accumulator for the run, and the operator's stop (``executed_through_seq``).
* :func:`partial_bag` — the response bag a stopped / failed CASE turn persists:
  ``case`` set, every measurement key ``None`` ("not measured").
* :func:`project_case` — the ``case`` key off the analyst's payload, with the
  friendly plane the front door knows stamped on it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from .. import workstation_vocabulary as V
from ..consult_case_record import CaseFrameAccumulator
from .descriptor import Family
from .errors import DescriptorNotFound

logger = logging.getLogger(__name__)

__all__ = [
    "CASE_UNAVAILABLE",
    "StopBody",
    "arm",
    "is_case_unavailable",
    "partial_bag",
    "project_case",
    "require_case_available",
    "start_accumulator",
    "stop_case",
]

CASE_UNAVAILABLE = f"case_unavailable: {V.WORKSTATION_DRIVE_PACK_ID} not bound"


class StopBody(BaseModel):
    """``POST /consult/runs/{id}/stop`` body — optional, closed."""

    model_config = ConfigDict(extra="forbid")

    executed_through_seq: int | None = Field(default=None, ge=0)


def _grants(body: dict[str, Any] | None) -> bool:
    for ref in (body or {}).get("action_packs") or ():
        if isinstance(ref, dict) and ref.get("pack_id") == V.WORKSTATION_DRIVE_PACK_ID:
            return True
    return False


async def require_case_available(descriptor_registry: Any, consult_row: Any) -> None:
    """409 unless the pack is registered AND the consult descriptor grants it."""
    if not _grants(getattr(consult_row, "body", None)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=CASE_UNAVAILABLE)
    try:
        await descriptor_registry.get(V.WORKSTATION_DRIVE_PACK_ID, family=Family.ACTION_PACK)
    except DescriptorNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=CASE_UNAVAILABLE,
        ) from exc


def is_case_unavailable(text: Any) -> bool:
    """Did the actor refuse an armed turn for want of the binding?"""
    return "case_unavailable" in str(text or "")


def arm(first_input: dict[str, Any], *, present_case: bool, rounds_chosen: bool) -> None:
    """Thread the flag onto the actor row — and nothing at all when unarmed."""
    if not present_case:
        return
    first_input["present_case"] = True
    if not rounds_chosen:
        first_input["max_tool_rounds"] = V.LIMITS["case_rounds_default"]


def start_accumulator(*, present_case: bool, request_id: str, plane: str | None) -> Any | None:
    return CaseFrameAccumulator(request_id=request_id, plane=plane) if present_case else None


async def stop_case(nats_store: Any | None, run: Any, body: StopBody | None) -> int | None:
    """Acknowledge the operator's stop on a case run; publish ``case_stopped``.

    Returns ``stopped_after_seq`` (None for a plain run). The frame goes on the
    run's own step subject, so the live player and the trace both see it.
    """
    acc = getattr(run, "case", None)
    if acc is None:
        return None
    n = acc.stop(body.executed_through_seq if body is not None else None)
    if nats_store is not None:
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        frame = {
            "type": "step", "phase": V.CASE_PHASE, "kind": "case_stopped",
            "stopped_after_seq": n, "at": at,
        }
        try:
            await nats_store.nc.publish(
                f"legba.consult.steps.{run.request_id}", json.dumps(frame).encode("utf-8"),
            )
        except Exception:  # noqa: BLE001 — telemetry never fails the stop
            logger.debug("consult.case.stop_publish_failed", exc_info=True)
    return n


def partial_bag(run: Any, bag_keys: tuple[str, ...]) -> dict[str, Any] | None:
    """The bag a cancelled / failed CASE turn writes; None for a plain run."""
    acc = getattr(run, "case", None)
    if acc is None:
        return None
    bag: dict[str, Any] = dict.fromkeys(bag_keys)
    bag["case"] = acc.compose()
    bag["model"] = acc.plane
    return bag


def project_case(inner_data: dict[str, Any], model: str | None) -> dict[str, Any] | None:
    case = inner_data.get("case")
    if not isinstance(case, dict):
        return None
    return {**case, "plane": case.get("plane") or model}
