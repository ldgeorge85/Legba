# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``page`` — ONE web page, fetched and landed as ONE document (lane I-2).

The kind the ingestion intake runs an address under: the intake's address
adapter puts the address in ``config.url`` and the run is this handler's
pull. It yields exactly one :class:`Signal` shaped like a feed entry's
(``title``, ``raw_body``, ``published_at``, ``link``, ``summary``,
``author``, ``tags``, ``source_url``, ``external_id`` — the RSS handler's
payload keys), so enrichment, dedupe, the keyword gate, geocoding, the
summariser, the desks' slices and verify read it as they read any document
— or it yields nothing and records WHY (``last_outcome`` and the
``page_health`` state key; the intake copies it onto the request's result).

THE FETCH. Through the guarded egress client (private addresses refused at
every hop; the per-source ``fetch.http_version`` posture applies, as it
does to every source poll), redirects bounded by ``max_redirects``, the
body read as a stream and cut at ``max_bytes``, under fetch role ``page``
so the receipt writer keeps the bytes AND the extracted text as a page
version (a refused page keeps its receipt: the bytes ARE what the address
answered). ``robots.txt`` is honoured exactly as the ``scraper`` kind does
(fetched once per host per handler, 2xx obeyed, 404 = no rules, unreachable
= permitted, RFC 9309 §2.3.1.4's MAY), and is NOT kept as a receipt — it is
not evidence about the page.

HOST POLICY. :mod:`._fetch_policy` is the one per-host registry the
evidence archiver and this kind both read. As in the archiver, the first
fetch is the plain guarded client; when an edge refuses it (401/403/429 or a
challenge page) and the host's policy resolves impersonation ON
(``_fetch_policy.impersonate_kwarg``), the fetch is re-issued ONCE through
the impersonated client (``_egress.fetch_client(impersonate=True)``: the
browser TLS fingerprint, our own User-Agent, every redirect hop
re-asserted against the egress guard). That leg is not an httpx transport,
so its body is added to the capture log here — the receipt is kept either
way. A host without curl_cffi in the image is refused with that reason.

EXTRACTION. The archiver's (:mod:`.page_text`: the same Trafilatura call, so
``raw_body`` equals the text the receipt stores for the version), plus the
markup reading of :mod:`.page_meta`, both in a worker thread — a large page
costs 0.1–0.7 s of CPU and the loop-lag monitor is live.

QUALITY. :mod:`.page_quality` — every refusal named, every flag recorded.

PUBLISHED_AT. Only what the page declares (:mod:`.page_meta`). Absent stays
absent (``payload.published_at`` is ``None``); ``payload.page.fetched_at``
records the fetch time AS the fetch time — never promoted to a publication
date.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, ClassVar
from urllib import robotparser
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._contract import Signal, SourceContext, SourceHealth
from ._egress import EgressBlockedError, fetch_client, guarded_async_client
from ._fetch_policy import fetch_policy_for, impersonate_kwarg
from .fetch_capture import _ACTIVE as _ACTIVE_CAPTURE, FetchCapture, capture_scope, fetch_role
from .page_quality import (
    DEFAULT_MAX_LINK_DENSITY,
    DEFAULT_MIN_TEXT_CHARS,
    Verdict,
    assess,
    content_type_refusal,
    status_refusal,
    wall_host,
)

logger = logging.getLogger(__name__)

__all__ = ["PAGE_KIND", "PageConfig", "PageOutcome", "PageSourceHandler"]

PAGE_KIND = "page"
_HEALTH_KEY = "page_health"
DEFAULT_USER_AGENT = "legba-page/0.1 (+https://github.com/ldgeorge85/legba)"
#: The payload ``summary`` (the page's own description) is cut like a feed's.
_SUMMARY_MAX_CHARS = 16_000


class PageConfig(BaseModel):
    """Config of a ``page`` pipeline. ``url`` is empty on the pipeline
    descriptor; the intake's address adapter fills it per request."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, max_length=4096)
    user_agent: str = Field(default=DEFAULT_USER_AGENT, max_length=256)
    respect_robots: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_redirects: int = Field(default=5, ge=0, le=10)
    #: Body cap: a page above it is refused ``too_large`` (and, cut short,
    #: keeps no receipt bytes — the capture is incomplete).
    max_bytes: int = Field(default=5 * 1024 * 1024, ge=64 * 1024, le=20 * 1024 * 1024)
    min_text_chars: int = Field(default=DEFAULT_MIN_TEXT_CHARS, ge=50, le=20_000)
    max_link_density: float = Field(default=DEFAULT_MAX_LINK_DENSITY, gt=0, le=1)
    #: The archiver's / the receipt writer's extracted-text cap.
    max_text_chars: int = Field(default=200_000, ge=1_000, le=512_000)
    #: The credibility a page gets when its host has no row in the
    #: ``source_credibility`` ledger — applied at landing and FLAGGED
    #: (``payload.page.credibility.basis = "declared_default"``). ``None`` =
    #: leave it unscored, as a feed signal from an unknown host is.
    unknown_host_credibility: float | None = Field(default=0.3, ge=0, le=1)
    poll_budget_seconds: float = Field(default=90.0, gt=0, le=240)
    #: Set by the intake run, never in a descriptor: the request this fetch
    #: serves (stamped as ``payload.intake``) and the registered source that
    #: shares the address's host (``runtime/page_landing.config_overlay``).
    intake: dict[str, Any] | None = None
    publisher: dict[str, Any] | None = None

    @field_validator("url")
    @classmethod
    def _http_only(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        parts = urlsplit(v.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"url {v!r} must be an http(s) URL with a host")
        return v.strip()


@dataclass
class PageOutcome:
    """What one pull did — ``refusal`` set = no document."""

    url: str | None
    final_url: str | None = None
    status: int | None = None
    content_type: str | None = None
    transport: str = "guarded"
    robots: str | None = None
    refusal: str | None = None
    detail: str | None = None
    flags: list[str] = field(default_factory=list)
    text_chars: int = 0
    body_sha256: str | None = None
    #: A refusal a retry cannot change (robots, quality, content type).
    permanent: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], 0) or k == "refusal"}


def _charset(content_type: str | None) -> str | None:
    if content_type and "charset=" in content_type:
        return content_type.split("charset=", 1)[1].split(";")[0].strip().strip('"') or None
    return None


def _analyse(body: bytes, content_type: str | None, final_url: str, cfg: PageConfig,
             requested_url: str | None = None) -> tuple[str | None, Any, Verdict]:
    """Extraction + markup reading + the quality gates — CPU-bound, run in a
    worker thread."""
    from .page_meta import parse_page_meta  # noqa: PLC0415
    from .page_text import _skip_text_extraction, extract_main_text  # noqa: PLC0415

    charset = _charset(content_type)
    html = body.decode(charset or "utf-8", errors="replace")
    meta = parse_page_meta(html, final_url)
    if _skip_text_extraction(final_url):
        return None, meta, Verdict("no_text_extracted", "host has no article region (a chat widget page)")
    text = extract_main_text(body, charset, max_chars=cfg.max_text_chars)
    verdict = assess(text=text, raw=body, content_type=content_type, final_url=final_url, meta=meta,
                     min_text_chars=cfg.min_text_chars, max_link_density=cfg.max_link_density,
                     requested_url=requested_url)
    return text, meta, verdict


class PageSourceHandler:
    """L-102 source handler for the ``page`` kind (module docstring)."""

    kind: ClassVar[str] = PAGE_KIND
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.page/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = PageConfig
    health_state_key: ClassVar[str] = _HEALTH_KEY

    def __init__(self, config: PageConfig, *, http_client: httpx.AsyncClient | None = None,
                 impersonated_client: Any = None) -> None:
        self._config = config
        self._client = http_client
        self._impersonated = impersonated_client
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.poll_budget_seconds = config.poll_budget_seconds
        self.last_outcome: dict[str, Any] | None = None

    # -- the fetch -----------------------------------------------------------

    def _open(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return guarded_async_client(
            timeout=self._config.timeout_seconds, follow_redirects=True,
            max_redirects=self._config.max_redirects,
            headers={"User-Agent": self._config.user_agent},
        )

    async def _robots_allow(self, client: Any, url: str) -> str:
        """``allowed`` / ``disallowed`` / ``no_rules`` / ``unreachable`` —
        the scraper kind's posture; robots.txt is fetched outside the capture
        scope (it is not a receipt of the page)."""
        parts = urlsplit(url)
        host = parts.netloc
        if host not in self._robots:
            rp: robotparser.RobotFileParser | None = None
            with capture_scope(None):
                try:
                    resp = await client.get(f"{parts.scheme}://{host}/robots.txt")
                except Exception as exc:  # unreachable: permitted (scraper posture)
                    logger.debug("page.robots_unreachable host=%s err=%s", host, exc)
                    resp = None
            if resp is not None and 200 <= resp.status_code < 300:
                rp = robotparser.RobotFileParser()
                rp.parse(resp.text.splitlines())
            elif resp is not None and 400 <= resp.status_code < 500:
                rp = robotparser.RobotFileParser()
                rp.parse([])
            self._robots[host] = rp
        rp = self._robots[host]
        if rp is None:
            return "unreachable"
        return "allowed" if rp.can_fetch(self._config.user_agent, url) else "disallowed"

    async def _read(self, client: Any, url: str) -> tuple[int, str | None, str, bytes, bool]:
        """``(status, content_type, final_url, body, too_large)`` — streamed,
        cut at ``max_bytes``, under fetch role ``page``."""
        cap = self._config.max_bytes
        chunks: list[bytes] = []
        size = 0
        too_large = False
        with fetch_role("page"):
            async with client.stream("GET", url) as resp:
                status = int(resp.status_code)
                ctype = resp.headers.get("content-type")
                final = str(resp.url)
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > cap:
                        too_large = True
                        break
                    chunks.append(chunk)
        return status, ctype, final, b"".join(chunks), too_large

    async def _impersonated_read(self, url: str) -> tuple[int, str | None, str, bytes, bool]:
        client_cm = self._impersonated if self._impersonated is not None else fetch_client(
            guarded=guarded_async_client, impersonate=True, timeout=self._config.timeout_seconds,
            follow_redirects=True, headers={"User-Agent": self._config.user_agent},
        )
        async with client_cm as client:
            status, ctype, final, body, too_large = await self._read(client, url)
        log = _ACTIVE_CAPTURE.get()
        if log is not None and not too_large and 200 <= status < 300:
            # Not an httpx transport, so the tee never saw it: keep the receipt here.
            log.add(FetchCapture(
                url=final, method="GET", status=status, content_type=ctype, content_encoding=None,
                role="page", fetched_at=datetime.now(tz=timezone.utc), complete=True, body=body,
                sha256=hashlib.sha256(body).hexdigest(), size=len(body),
            ))
        return status, ctype, final, body, too_large

    async def _fetch(self, url: str, out: PageOutcome) -> bytes | None:
        client = self._open()
        owns = client is not self._client
        try:
            if self._config.respect_robots:
                out.robots = await self._robots_allow(client, url)
                if out.robots == "disallowed":
                    out.refusal, out.detail, out.permanent = "robots_disallowed", "robots.txt disallows this path", True
                    return None
            status, ctype, final, body, too_large = await self._read(client, url)
        finally:
            if owns:
                await client.aclose()
        blocked = status in (401, 403, 429)
        if not blocked and 200 <= status < 300 and not too_large:
            from ..analysts.deterministic_handlers._challenge_detect import detect_challenge_page  # noqa: PLC0415

            blocked = detect_challenge_page(body, ctype) is not None
        if blocked and impersonate_kwarg(fetch_policy_for(url)) is True:
            logger.info("page.escalate_impersonated url=%s first_status=%s", url, status)
            try:
                status, ctype, final, body, too_large = await self._impersonated_read(url)
                out.transport = "impersonated"
            except RuntimeError as exc:  # curl_cffi absent: refuse, never degrade quietly
                out.refusal, out.detail = "blocked_by_challenge", f"impersonation unavailable: {exc}"[:300]
                return None
        out.status, out.content_type, out.final_url = status, ctype, final
        if too_large:
            out.refusal, out.detail, out.permanent = "too_large", f"> {self._config.max_bytes} bytes", True
            return None
        out.body_sha256 = hashlib.sha256(body).hexdigest()
        if not 200 <= status < 300:
            verdict = status_refusal(status)
            if verdict is not None:
                out.refusal, out.detail = verdict
                out.permanent = verdict[0] == "paywall"
            else:
                out.refusal, out.detail = "http_status", f"HTTP {status}"
            return None
        why = content_type_refusal(ctype, body[:1024])
        if why is not None:
            out.refusal, out.detail, out.permanent = "unsupported_content_type", why, True
            return None
        return body

    # -- pull ----------------------------------------------------------------

    async def pull(self, ctx: SourceContext, since: datetime | None = None) -> AsyncIterator[Signal]:
        url = self._config.url
        out = PageOutcome(url=url)
        if url is None:
            out.refusal, out.detail = "no_address", "a page pipeline runs one address per request"
            await self._finish(ctx, out)
            return
        try:
            body = await self._fetch(url, out)
        except EgressBlockedError as exc:
            out.refusal, out.detail, out.permanent = "egress_blocked", str(exc)[:300], True
            body = None
        except httpx.HTTPError as exc:
            out.refusal, out.detail = "transport_error", f"{type(exc).__name__}: {exc}"[:300]
            body = None
        if body is None:
            await self._finish(ctx, out)
            return
        fetched_at = datetime.now(tz=timezone.utc)
        text, meta, verdict = await asyncio.to_thread(
            _analyse, body, out.content_type, out.final_url or url, self._config, url,
        )
        out.flags = list(verdict.flags)
        if verdict.refusal is not None:
            out.refusal, out.detail, out.permanent = verdict.refusal, verdict.detail, True
            await self._finish(ctx, out)
            return
        out.text_chars = len(text or "")
        signal = self._to_signal(ctx, text or "", meta, out, fetched_at)
        await self._finish(ctx, out)
        yield signal

    def _to_signal(self, ctx: SourceContext, text: str, meta: Any, out: PageOutcome,
                   fetched_at: datetime) -> Signal:
        from .page_meta import same_site  # noqa: PLC0415

        final = out.final_url or out.url or ""
        canonical = meta.canonical or final
        if wall_host(canonical) or wall_host(final):
            # W11-F259: never a wall's address as the document's own. The gates
            # refuse such a page first; this is the last line, not the rule.
            raise ValueError(f"page document on a wall address: {canonical or final}")
        host = (urlsplit(canonical).hostname or "").lower()
        title = meta.title or ""
        summary = (meta.description or "")[:_SUMMARY_MAX_CHARS]
        intake = self._config.intake
        publisher: dict[str, Any] = {"host": host, "site_name": meta.site_name}
        payload: dict[str, Any] = {
            "external_id": canonical,
            "published_at": meta.published_at.isoformat() if meta.published_at else None,
            "title": title,
            "link": canonical,
            "summary": summary,
            "author": meta.author or "",
            "tags": [],
            "raw_body": text,
            "source_url": final,
            "publisher": publisher,
            "references": meta.references,
            "page": {
                "requested_url": out.url, "final_url": final, "fetched_at": fetched_at.isoformat(),
                "published_basis": meta.published_basis, "title_basis": meta.title_basis,
                "canonical_basis": meta.canonical_basis, "canonical_declared": meta.canonical_declared,
                "language": meta.language, "http_status": out.status, "content_type": out.content_type,
                "transport": out.transport, "robots": out.robots, "text_chars": len(text),
                "link_density": meta.link_density, "flags": list(out.flags),
                "body_sha256": out.body_sha256,
                "credibility_default": self._config.unknown_host_credibility,
            },
        }
        if meta.published_at is not None:
            payload["_published_at_dt"] = meta.published_at
        if intake:
            payload["intake"] = dict(intake)
            pub = self._config.publisher or {}
            if pub.get("host") and same_site(f"https://{pub['host']}/", canonical):
                publisher["registered_source_id"] = pub.get("source_id")
                if pub.get("geo"):
                    # The geo fallback reads it exactly as it reads a feed's
                    # scope_geo (data/sources/baseline.py) — the publisher's
                    # home, a fallback only.
                    payload["publisher_origin"] = list(pub["geo"])
        content_hash = hashlib.sha256(
            (canonical + "\x1f" + title + "\x1f" + text).encode("utf-8"),
        ).hexdigest()
        return Signal(
            source_id=ctx.source_id,
            modality="text",
            fetched_at=fetched_at,
            payload=payload,
            content_hash=content_hash,
            canonical_url=canonical,
            language_hint=meta.language,
            raw_provenance={
                "fetch_kind": PAGE_KIND, "page_url": out.url, "final_url": final,
                "http_status": out.status, "transport": out.transport,
                "intake_request_id": (intake or {}).get("request_id"),
            },
        )

    async def _finish(self, ctx: SourceContext, out: PageOutcome) -> None:
        self.last_outcome = out.as_dict()
        state = "healthy" if out.refusal is None else "degraded"
        if out.refusal is not None:
            logger.info("page.refused url=%s reason=%s detail=%s", out.url, out.refusal, out.detail)
        await ctx.state_store.set(_HEALTH_KEY, {
            "state": state, "last_error": None if out.refusal is None else f"{out.refusal}: {out.detail}",
            "detail": self.last_outcome,
            "last_success_at": ctx.utcnow().isoformat() if out.refusal is None else None,
        })

    # -- health + lifecycle --------------------------------------------------

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        rec = await ctx.state_store.get(_HEALTH_KEY)
        if not isinstance(rec, dict):
            return SourceHealth(state="healthy", detail={"note": "runs per request; never polled"})
        return SourceHealth(state=str(rec.get("state") or "healthy"), last_error=rec.get("last_error"),
                            detail=rec.get("detail") or {})

    async def on_configure(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_activate(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_pause(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_resume(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_retire(self, ctx: SourceContext | None = None) -> None:
        self._robots.clear()
