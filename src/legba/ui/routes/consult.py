"""
Consultation routes — interactive chat with Legba.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()
log = logging.getLogger(__name__)


def _get_engine(request: Request):
    return request.app.state.consult_engine


def _get_templates(request: Request):
    from ..app import templates
    return templates


@router.get("/consult", response_class=HTMLResponse)
async def consult_page(request: Request):
    """Render the consultation chat page."""
    tpl = _get_templates(request)

    # Get or create session ID from cookie
    session_id = request.cookies.get("consult_session")
    if not session_id:
        session_id = str(uuid4())

    # Load existing messages
    engine = _get_engine(request)
    messages = await engine.load_session(session_id) if engine else []

    # Filter to only show user/assistant messages (not tool results)
    display_messages = [
        m for m in messages
        if m["role"] in ("user", "assistant")
        and not m["content"].startswith("[Tool Result:")
    ]

    response = tpl.TemplateResponse(
        "consult/chat.html",
        {
            "request": request,
            "active_page": "consult",
            "session_id": session_id,
            "messages": display_messages,
        },
    )
    response.set_cookie("consult_session", session_id, max_age=3600)
    return response


@router.post("/consult/send")
async def consult_send(request: Request):
    """Send a message and get Legba's response."""
    engine = _get_engine(request)
    if not engine:
        return JSONResponse(
            {"error": "Consultation engine not available — LLM not configured."},
            status_code=503,
        )

    body = await request.json()
    user_message = body.get("message", "").strip()
    if not user_message:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    session_id = request.cookies.get("consult_session", str(uuid4()))

    try:
        response_text, _ = await engine.exchange(session_id, user_message)
    except Exception as e:
        log.exception("Consultation exchange failed")
        return JSONResponse({"error": f"LLM error: {e}"}, status_code=500)

    # Safety net: clean any JSON wrapper that leaked through the tool parser
    from ..consult import _clean_response
    response_text = _clean_response(response_text)

    return JSONResponse({
        "response": response_text,
        "session_id": session_id,
    })


@router.delete("/consult/session")
async def consult_clear(request: Request):
    """Clear the current consultation session."""
    engine = _get_engine(request)
    session_id = request.cookies.get("consult_session")
    if engine and session_id:
        await engine.delete_session(session_id)
    return JSONResponse({"status": "cleared"})
