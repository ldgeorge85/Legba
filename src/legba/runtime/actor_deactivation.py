# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Idempotent actor deactivation — ``DELETE /actors/<Type>/<id>``.

WHY (lane N-U, 11-13). The daprd sidecar (``legba-dapr-sidecar-1``) is a
separate container that outlives every runtime roll — up since 09-23 while the
runtime restarted at 13:21. Its in-memory active-actor table still holds the
actors the PREVIOUS app process activated; when their idle timeout expires it
sends the deactivation DELETE to the NEW process, which never activated them.
The dapr SDK's ``ActorManager.deactivate_actor`` raises ``ValueError('<id> is
not activated')`` and ``dapr.ext.fastapi`` turns every exception into a bare
500 with no traceback and no log line. daprd logs ``Failed to halt actor ...
is not activated`` and drops the actor from its table anyway, so nothing
leaks: the app holds no state for it, reminders live in the scheduler and are
untouched by deactivation, and the next invocation activates the actor fresh
in this process. Measured: 4,859 such failures in the collected sidecar log
since 08-01, in a burst about an hour after each roll; 44 of 44 DELETEs in the
50 minutes after today's.

This route answers that case 200 (deactivating an actor that is not active is
already done) with one info line naming it, and LOGS any other deactivation
failure with its traceback before returning the same 500 shape — the logging
the SDK never did. It is registered BEFORE ``DaprActor(app)`` so FastAPI
matches it first; the SDK's own handler stays mounted behind it, unused.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_NOT_ACTIVATED = "is not activated"


def _response(status: int, message: str, error_code: str | None = None) -> JSONResponse:
    """The ``dapr.ext.fastapi`` ``_wrap_response`` shape."""
    body: dict[str, Any] = {"message": message}
    if error_code:
        body["errorCode"] = error_code
    return JSONResponse(content=body, status_code=status)


async def deactivate_actor(actor_type_name: str, actor_id: str) -> JSONResponse:
    """Deactivate ``actor_id``; a never-activated actor is a 200 no-op."""
    from dapr.actor import ActorRuntime

    try:
        await ActorRuntime.deactivate(actor_type_name, actor_id)
    except ValueError as exc:
        if _NOT_ACTIVATED in str(exc):
            logger.info(
                "dapr_host.actor.deactivate_not_active type=%s actor_id=%s — "
                "the sidecar outlived the app process that activated it; "
                "nothing to release", actor_type_name, actor_id,
            )
            return _response(200, f"actor not active: {actor_type_name}.{actor_id}")
        logger.exception(
            "dapr_host.actor.deactivate_failed type=%s actor_id=%s", actor_type_name, actor_id)
        return _response(500, repr(exc), "UNKNOWN")
    except Exception as exc:  # noqa: BLE001 — the SDK route's contract, now logged
        logger.exception(
            "dapr_host.actor.deactivate_failed type=%s actor_id=%s", actor_type_name, actor_id)
        return _response(500, repr(exc), "UNKNOWN")
    return _response(200, f"deactivated actor: {actor_type_name}.{actor_id}")


def mount_idempotent_deactivation(app: FastAPI) -> None:
    """Register the DELETE route; call BEFORE ``DaprActor(app)``."""
    app.add_api_route(
        "/actors/{actor_type_name}/{actor_id}", deactivate_actor,
        methods=["DELETE"], tags=["Actor"],
    )


__all__ = ["deactivate_actor", "mount_idempotent_deactivation"]
