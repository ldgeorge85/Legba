"""
SSE — Server-Sent Events stream for real-time updates.

Polls Redis state every 2 seconds and emits deltas as named events.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..app import get_stores

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sse"])

POLL_INTERVAL = 2  # seconds


@router.get("/sse/stream")
async def event_stream(request: Request):
    stores = get_stores(request)

    async def generate():
        last_cycle = None
        last_event_count = None

        while True:
            if await request.is_disconnected():
                break

            try:
                redis = stores.registers._redis

                # Check cycle state
                cycle_val = await redis.get("legba:cycle")
                current_cycle = int(cycle_val) if cycle_val else None

                if current_cycle and current_cycle != last_cycle:
                    if last_cycle is not None:
                        # Emit cycle event
                        cycle_type = await redis.get("legba:cycle_type")
                        if isinstance(cycle_type, bytes):
                            cycle_type = cycle_type.decode()

                        if current_cycle > last_cycle:
                            yield f"event: cycle:start\ndata: {json.dumps({'cycle_number': current_cycle, 'cycle_type': cycle_type or 'NORMAL'})}\n\n"
                        else:
                            yield f"event: cycle:end\ndata: {json.dumps({'cycle_number': last_cycle})}\n\n"

                    last_cycle = current_cycle

                # Check agent status
                status_val = await redis.get("legba:agent_status")
                if status_val:
                    status = status_val if isinstance(status_val, str) else status_val.decode()
                    phase_val = await redis.get("legba:agent_phase")
                    phase = ""
                    if phase_val:
                        phase = phase_val if isinstance(phase_val, str) else phase_val.decode()
                    yield f"event: agent:status\ndata: {json.dumps({'status': status, 'cycle': current_cycle, 'phase': phase})}\n\n"

                # Check for new signals (signal count changed)
                try:
                    signal_count = await stores.count_signals()
                    if last_event_count is not None and signal_count > last_event_count:
                        # Fetch latest signals
                        from ...shared.schemas.signals import Signal
                        async with stores.structured._pool.acquire() as conn:
                            rows = await conn.fetch(
                                "SELECT data FROM signals ORDER BY created_at DESC LIMIT $1",
                                signal_count - last_event_count,
                            )
                            for row in rows:
                                try:
                                    sig = Signal.model_validate_json(row["data"])
                                    yield f"event: event:new\ndata: {json.dumps({'event_id': str(sig.id), 'title': sig.title, 'category': sig.category if isinstance(sig.category, str) else sig.category.value, 'timestamp': sig.event_timestamp.isoformat() if sig.event_timestamp else ''})}\n\n"
                                except Exception:
                                    continue
                    last_event_count = signal_count
                except Exception:
                    pass

                # Check for watch triggers
                try:
                    triggers = await redis.lrange("legba:watch_triggers", 0, 0)
                    if triggers:
                        t = json.loads(triggers[0] if isinstance(triggers[0], str) else triggers[0].decode())
                        yield f"event: watch:trigger\ndata: {json.dumps(t)}\n\n"
                except Exception:
                    pass

            except Exception as exc:
                logger.debug("SSE poll error: %s", exc)

            await asyncio.sleep(POLL_INTERVAL)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
