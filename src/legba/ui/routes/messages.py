"""Message routes — GET /messages, POST /messages, GET /messages/poll."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Form

from ..app import templates
from ..messages import MessageStore, UINatsClient
from ...shared.schemas.comms import InboxMessage, MessagePriority

router = APIRouter()


def _get_msg_store(request: Request) -> MessageStore:
    return request.app.state.msg_store


def _get_nats(request: Request) -> UINatsClient:
    return request.app.state.ui_nats


async def _drain_and_persist(request: Request) -> None:
    """Pull new outbound messages from NATS and persist them."""
    nats_client = _get_nats(request)
    store = _get_msg_store(request)
    for msg in await nats_client.drain_outbound():
        await store.store_outbound(msg)


@router.get("/messages")
async def messages_page(request: Request):
    await _drain_and_persist(request)
    store = _get_msg_store(request)
    thread = await store.get_thread()
    nats_client = _get_nats(request)

    ctx = {
        "request": request,
        "active_page": "messages",
        "thread": thread,
        "nats_available": nats_client.available,
    }

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return templates.TemplateResponse("messages/_thread.html", ctx)
    return templates.TemplateResponse("messages/list.html", ctx)


@router.post("/messages")
async def send_message(
    request: Request,
    content: str = Form(...),
    priority: str = Form("normal"),
    requires_response: bool = Form(False),
):
    store = _get_msg_store(request)
    nats_client = _get_nats(request)

    msg = InboxMessage(
        id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        content=content.strip(),
        priority=MessagePriority(priority),
        requires_response=requires_response,
    )

    await store.store_inbound(msg)
    await nats_client.publish_inbound(msg)

    # Drain any immediate responses
    await _drain_and_persist(request)
    thread = await store.get_thread()

    return templates.TemplateResponse(
        "messages/_thread.html",
        {
            "request": request,
            "active_page": "messages",
            "thread": thread,
            "nats_available": nats_client.available,
        },
    )


@router.get("/messages/poll")
async def messages_poll(request: Request):
    await _drain_and_persist(request)
    store = _get_msg_store(request)
    thread = await store.get_thread()
    nats_client = _get_nats(request)

    return templates.TemplateResponse(
        "messages/_thread.html",
        {
            "request": request,
            "active_page": "messages",
            "thread": thread,
            "nats_available": nats_client.available,
        },
    )
