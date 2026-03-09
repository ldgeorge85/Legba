"""
Legba Operator Console — FastAPI application.

Server-rendered UI using Jinja2 + htmx + Tailwind CSS.
Single entry point: python -m uvicorn legba.ui.app:app --host 0.0.0.0 --port 8501
"""

from __future__ import annotations

import math
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..shared.config import PostgresConfig, RedisConfig, OpenSearchConfig, NatsConfig, QdrantConfig, LLMConfig
from .stores import StoreHolder
from .messages import UINatsClient, MessageStore

UI_DIR = Path(__file__).parent
TEMPLATES_DIR = UI_DIR / "templates"
STATIC_DIR = UI_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    stores = StoreHolder(
        pg=PostgresConfig.from_env(),
        redis_cfg=RedisConfig.from_env(),
        os_cfg=OpenSearchConfig.from_env(),
        audit_cfg=OpenSearchConfig.from_audit_env(),
        qdrant_cfg=QdrantConfig.from_env(),
        llm_cfg=LLMConfig.from_env(),
    )
    await stores.connect()
    app.state.stores = stores

    # Messages: NATS client + message store
    ui_nats = UINatsClient(url=NatsConfig.from_env().url)
    await ui_nats.connect()
    app.state.ui_nats = ui_nats

    msg_store = MessageStore(redis_client=stores.registers._redis)
    app.state.msg_store = msg_store

    yield

    await ui_nats.close()
    await stores.close()


app = FastAPI(title="Legba Operator Console", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ------------------------------------------------------------------
# Jinja2 custom filters
# ------------------------------------------------------------------

def _timeago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = delta.total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 30:
        return f"{int(days)}d ago"
    return f"{int(days / 30)}mo ago"


def _format_confidence(val: float | None) -> str:
    if val is None:
        return "—"
    return f"{val:.0%}"


def _truncate_uuid(val: str | None) -> str:
    if val is None:
        return "—"
    s = str(val)
    return s[:8] if len(s) > 8 else s


def _format_pct(val: float | None) -> str:
    if val is None:
        return "0%"
    return f"{val:.0%}"


def _render_message(text: str | None) -> str:
    """Render message content as HTML. Handles markdown, raw JSON, and plain text."""
    if not text:
        return ""
    text = text.strip()
    # Strip the [STATUS REPORT — Cycle N] / [SUPERVISOR ALERT] headers — shown as badges
    header_match = re.match(
        r"\[(STATUS REPORT|SUPERVISOR ALERT|ANALYSIS REPORT)\s*[—–-]?\s*[^]]*\]\s*\n*", text
    )
    if header_match:
        text = text[header_match.end():].strip()
    # Detect raw JSON tool calls (leaked from LLM) — wrap in code block
    if text and text[0] in ("{", "["):
        from markupsafe import escape
        return f'<pre class="text-xs bg-gray-900 rounded p-2 overflow-x-auto text-gray-400"><code>{escape(text)}</code></pre>'
    # Render markdown
    try:
        from markdown_it import MarkdownIt
        md = MarkdownIt()
        return md.render(text)
    except Exception:
        from markupsafe import escape
        return f"<p>{escape(text)}</p>"


templates.env.filters["timeago"] = _timeago
templates.env.filters["format_confidence"] = _format_confidence
templates.env.filters["truncate_uuid"] = _truncate_uuid
templates.env.filters["format_pct"] = _format_pct
templates.env.filters["render_message"] = _render_message


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def get_stores(request: Request) -> StoreHolder:
    return request.app.state.stores


# ------------------------------------------------------------------
# Health endpoint
# ------------------------------------------------------------------

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


# ------------------------------------------------------------------
# Include route modules
# ------------------------------------------------------------------

from .routes.dashboard import router as dashboard_router
from .routes.entities import router as entities_router
from .routes.events import router as events_router
from .routes.sources import router as sources_router
from .routes.cycles import router as cycles_router
from .routes.messages import router as messages_router
from .routes.goals import router as goals_router
from .routes.graph import router as graph_router
from .routes.journal import router as journal_router
from .routes.reports import router as reports_router
from .routes.facts import router as facts_router
from .routes.memory import router as memory_router

app.include_router(dashboard_router)
app.include_router(entities_router)
app.include_router(events_router)
app.include_router(facts_router)
app.include_router(sources_router)
app.include_router(goals_router)
app.include_router(cycles_router)
app.include_router(messages_router)
app.include_router(journal_router)
app.include_router(reports_router)
app.include_router(graph_router)
app.include_router(memory_router)
