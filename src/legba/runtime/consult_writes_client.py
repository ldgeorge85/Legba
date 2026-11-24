# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The runtime's THIN CLIENT of the registry's consult write executor (C-2b-1; C-2a §6, D10).

The write handlers (``data/analysts/agency/consult_write.py``) call
:meth:`ConsultWritesClient.submit`, which

1. reads the run's request id off the binding's ``requested_by``
   (``…::request::<id>``) — a call with none is ``writes_not_armed``;
2. PREFLIGHTS a create / update / transition through the runtime's own parser
   for the class (``consult_write_patch.preflight``: the source actor's
   ``SourceDescriptor.model_validate``, the target resolver's
   ``TargetDescriptor.model_validate`` for a subject, over the ``/typed``
   shape), so a body
   the runtime could not parse is refused ``preflight_failed`` and never lands
   — the executor records the refusal and re-runs the same check itself;
3. posts ``POST /api/v1/consult/writes`` and returns the executor's answer.

A transport failure is returned as ``failed`` (never a fabricated success); a
501 is ``kind_not_built`` with its seam; a 403 is ``writes_not_armed``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from ..data import consult_write_patch as P

logger = logging.getLogger(__name__)

__all__ = ["ConsultWritesClient", "request_id_of"]

_WRITES_PATH = "/api/v1/consult/writes"

#: class → the registry family its ``/typed`` head is read from for the preflight.
_PREFLIGHT_FAMILY: dict[str, str] = {
    "source": "source",
    "subject": "target",
}


def request_id_of(requested_by: str | None) -> str | None:
    marker = "::request::"
    if not requested_by or marker not in requested_by:
        return None
    return requested_by.rsplit(marker, 1)[1] or None


class ConsultWritesClient:
    def __init__(self, registry_client: Any) -> None:
        self._rc = registry_client

    async def _preflight(self, tool: str, args: Mapping[str, Any]) -> str | None:
        cls = str(args.get("class") or "")
        if cls not in _PREFLIGHT_FAMILY or tool not in {"create_object", "update_object", "transition_object"}:
            return None
        if tool == "create_object":
            body = P.normalize_create(args.get("body") or {}, now_iso=datetime.now(timezone.utc).isoformat())
            return P.preflight(cls, body)
        try:
            head = await self._rc.get_descriptor_typed(str(args.get("id")), family=_PREFLIGHT_FAMILY[cls])
        except Exception as exc:  # noqa: BLE001 — the executor decides; we just could not look
            logger.warning("consult_writes.preflight_fetch_failed id=%s err=%s", args.get("id"), exc)
            return None
        if not isinstance(head, Mapping):
            return None
        if tool == "update_object":
            body, problem = P.apply_patch(cls, head, args.get("patch") or {})
            if problem is not None or body is None:
                return None  # the executor refuses it with the patch cause
        else:
            if args.get("to_state") == "retired":
                return None
            body = {**dict(head), "identity": {**dict(head.get("identity") or {}), "state": args.get("to_state")}}
        return P.preflight(cls, body)

    async def submit(
        self, *, tool: str, args: Mapping[str, Any], requested_by: str | None,
        turn_cost_usd: float | None,
    ) -> dict[str, Any]:
        request_id = request_id_of(requested_by)
        if request_id is None:
            return {"status": "refused", "cause": "writes_not_armed",
                    "detail": "this call carries no consult request; writes run only on an armed turn."}
        problem = await self._preflight(tool, args)
        try:
            code, body = await self._rc.request_json(
                "POST", _WRITES_PATH,
                json_body={
                    "request_id": request_id, "tool": tool, "args": dict(args),
                    "requested_by": requested_by, "turn_cost_usd": turn_cost_usd,
                    "runtime_preflight": problem,
                },
                timeout_seconds=60.0,
            )
        except Exception as exc:  # noqa: BLE001 — failed, never a fabricated success
            return {"status": "failed", "detail": f"registry unreachable: {exc}"[:400]}
        detail = body.get("detail") if isinstance(body, Mapping) else body
        if code == 501:
            seam = detail.get("seam") if isinstance(detail, Mapping) else None
            return {"status": "refused", "cause": "kind_not_built", "seam": seam,
                    "detail": (detail.get("detail") if isinstance(detail, Mapping) else str(detail))}
        if code == 403:
            return {"status": "refused", "cause": "writes_not_armed", "detail": str(detail)[:400]}
        if code >= 400 or not isinstance(body, Mapping):
            return {"status": "failed", "detail": f"registry {code}: {str(detail)[:300]}"}
        return dict(body)
