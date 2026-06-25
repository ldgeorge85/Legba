# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generic polled JSON/CSV HTTP API source (S-3).

Closes the one real capability gap vs the original release: a *generic API
poller*. ``generic_webhook`` covers push-only integration; this kind covers
the long tail of read-only HTTP APIs that return a JSON document (or a CSV
table) containing an array of items — ReliefWeb, GDELT DOC 2.0, CKAN portals,
status APIs, etc. — without writing a bespoke handler per provider.

Shape (L-102 source-kind contract, mirroring the RSS / GeoJSON handlers):

  * ``pull(ctx, since)``: render ``url_template`` against a cursor-driven
    time window (substitutions ``{date_today}`` / ``{date_yesterday}`` /
    ``{window_start_iso}`` / ``{window_end_iso}``), GET it via ``httpx``,
    resolve the item array via a small dot/bracket JSONPath-lite resolver
    (``items_path``), then map each item to a :class:`Signal` through the
    configured field paths (``id_path`` / ``title_path`` / ``url_path`` /
    ``timestamp_path`` / ``body_path`` / ``geo_path``).
  * Cursor: ``state_store["json_api_cursor"] = {"last_pulled_at": iso}`` —
    the next window starts where the last successful pull ended. Items whose
    parsed timestamp is **not after** the window start are skipped; items
    without a parsable timestamp are emitted (downstream content-hash dedupe
    absorbs the overlap).
  * Optional auth: ``auth`` declares a vault SecretRef applied as either a
    request header or a query parameter. The secret is resolved per pull via
    ``ctx.secrets_resolve`` (or the constructor-injected resolver) and never
    cached or logged; the rendered URL stamped into payload/provenance never
    contains it.

FAIL-LOUD rule (no-stubs, decision D3): when ``auth`` declares a SecretRef
but no secrets resolver is wired, the handler **refuses** — ``on_configure``
/ ``on_activate`` / ``pull`` raise :class:`JsonApiAuthNotConfigured` and
``health_check`` reports ``unhealthy``. There is no literal-secret fallback
for this kind.

Failure semantics (L-102 §7), mirroring the RSS handler:

  * Transient network error / 5xx → one retry, then yield nothing; degraded.
  * 4xx → no retry, yield nothing; unhealthy.
  * Parse failure (not JSON / items_path misses / not an array) → yield
    nothing; unhealthy (a malformed document does not self-heal).

This module never imports from ``legba.data.runtime`` — it depends only on
the structural-typing surface in ``_contract.py`` plus ``httpx`` (a hard
dep) and the stdlib (``json`` / ``csv``). No new dependencies.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import string
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator, ClassVar, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._contract import Signal, SourceContext, SourceHealth
from ._egress import guarded_async_client


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_JSON_API_CURSOR_KEY = "json_api_cursor"
_JSON_API_HEALTH_KEY = "json_api_health"
_DEFAULT_RETRIES_FOR_TRANSIENT = 1
_TRANSIENT_STATUS = {502, 503, 504}

#: The only placeholders ``url_template`` may use. All are derived from the
#: poll window (cursor-driven): ``window_start`` = last successful pull (or
#: ``since`` / ``now - lookback_minutes`` on first run), ``window_end`` = now.
URL_TEMPLATE_SUBSTITUTIONS: frozenset[str] = frozenset({
    "date_today",
    "date_yesterday",
    "window_start_iso",
    "window_end_iso",
})

#: ISO 3166-ish geo code accepted from ``geo_path`` extraction (matches the
#: descriptor-side ``GeoCode`` pattern).
_GEO_CODE_RE = re.compile(r"^[A-Z]{2,3}$")

#: Header names: printable ASCII, no whitespace (header-injection guard).
_HEADER_NAME_RE = re.compile(r"^[\x21-\x7e]+$")

#: JSONPath-lite tokenizer — dotted identifiers, ``[0]`` numeric indexes,
#: and ``['key']`` / ``["key"]`` bracket-quoted keys.
_PATH_TOKEN_RE = re.compile(
    r"""
      \.?(?P<ident>[A-Za-z_][A-Za-z0-9_\-]*)   # .identifier (hyphens ok)
    | \[(?P<index>\d+)\]                       # [0]
    | \['(?P<squote>[^']*)'\]                  # ['key']
    | \["(?P<dquote>[^"]*)"\]                  # ["key"]
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# JSONPath-lite (no new deps — a small, safe dot/bracket resolver)
# ---------------------------------------------------------------------------


def parse_path(path: str) -> list[str | int]:
    """Parse a dot/bracket path into segments. Raises ``ValueError`` on junk.

    Supported forms: ``a.b.c``, ``a[0].b``, ``a['k with spaces'].b``,
    ``["k"]``, ``[2]``. An empty path parses to ``[]`` (document root).
    No wildcards, no filters, no recursive descent — deliberately tiny.
    """
    segments: list[str | int] = []
    pos = 0
    while pos < len(path):
        match = _PATH_TOKEN_RE.match(path, pos)
        if match is None or match.start() != pos:
            raise ValueError(f"bad path segment at offset {pos} in {path!r}")
        if match.group("ident") is not None:
            if match.group(0).startswith(".") and pos == 0:
                raise ValueError(f"path may not start with '.': {path!r}")
            segments.append(match.group("ident"))
        elif match.group("index") is not None:
            segments.append(int(match.group("index")))
        elif match.group("squote") is not None:
            segments.append(match.group("squote"))
        else:
            segments.append(match.group("dquote"))
        pos = match.end()
    return segments


def resolve_path(obj: Any, segments: list[str | int]) -> Any:
    """Walk ``segments`` into ``obj``. Returns ``None`` on any miss/mismatch."""
    node = obj
    for seg in segments:
        if isinstance(seg, int):
            if isinstance(node, list) and 0 <= seg < len(node):
                node = node[seg]
            else:
                return None
        else:
            if isinstance(node, dict):
                node = node.get(seg)
            else:
                return None
        if node is None:
            return None
    return node


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class JsonApiAuth(BaseModel):
    """Auth block — a vault SecretRef applied as a header or query param.

    ``secret_ref`` follows the existing vault-ref pattern (ACLED / GDELT /
    MediaCloud): the descriptor stores only the dotted credential id, never
    the secret; the runtime resolves it at call time. ``value_template``
    shapes the applied value (e.g. ``"Bearer {secret}"`` for an
    ``Authorization`` header); ``{secret}`` is the only placeholder.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["header", "query"] = "header"
    name: str = Field(..., min_length=1, max_length=128)
    secret_ref: str = Field(..., min_length=1, max_length=256)
    value_template: str = Field(default="{secret}", min_length=8, max_length=512)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _HEADER_NAME_RE.match(v):
            raise ValueError(
                "auth.name must be printable ASCII with no whitespace"
            )
        return v

    @field_validator("secret_ref")
    @classmethod
    def _validate_secret_ref(cls, v: str) -> str:
        # Mirror Property.Secret.of: a non-empty dotted identifier.
        if not v or "/" in v or " " in v:
            raise ValueError(
                "auth.secret_ref must be a non-empty dotted credential id "
                "(vault reference), never the raw secret"
            )
        return v

    @field_validator("value_template")
    @classmethod
    def _validate_value_template(cls, v: str) -> str:
        if "{secret}" not in v:
            raise ValueError("auth.value_template must contain '{secret}'")
        return v


class JsonApiConfig(BaseModel):
    """Pydantic config schema for :class:`JsonApiSourceHandler`.

    Validated at descriptor-registration time (L-101 / L-102 §1); the
    runtime parses each ``SourceBinding.config`` against this model before
    the handler is activated.
    """

    model_config = ConfigDict(extra="forbid")

    #: GET URL with optional ``{date_today}`` / ``{date_yesterday}`` /
    #: ``{window_start_iso}`` / ``{window_end_iso}`` placeholders. Substituted
    #: values are URL-quoted before insertion.
    url_template: str = Field(..., min_length=1, max_length=4096)
    #: Only GET is implemented; declared so descriptors are explicit and a
    #: future POST mode is a visible schema change, not a silent default.
    method: Literal["GET"] = "GET"
    response_format: Literal["json", "csv"] = "json"

    #: Optional vault-backed auth. When declared, a missing secrets resolver
    #: is a hard activation/pull failure (no-stubs fail-loud rule).
    auth: JsonApiAuth | None = None

    #: Dot/bracket path to the item array inside a JSON response. Empty ⇒
    #: the response root must itself be an array. Ignored-and-forbidden for
    #: ``response_format="csv"`` (rows are the items).
    items_path: str = Field(default="", max_length=512)

    # --- per-item field mappings (same path syntax, relative to one item) ---
    id_path: str = Field(default="id", max_length=512)
    title_path: str | None = Field(default=None, max_length=512)
    url_path: str | None = Field(default=None, max_length=512)
    timestamp_path: str | None = Field(default=None, max_length=512)
    body_path: str | None = Field(default=None, max_length=512)
    geo_path: str | None = Field(default=None, max_length=512)

    #: Stamped on every emitted signal (config flag per S-3).
    modality: Literal["text", "structured"] = "text"
    #: Static descriptor-driven tags stamped on every signal.
    static_tags: list[str] = Field(default_factory=list, max_length=16)
    #: Optional ISO language hint (pre-detection); falls back to the source
    #: scope language when the scope declares exactly one.
    language: str | None = Field(default=None, max_length=8)

    #: First-run window: how far back ``window_start`` reaches when there is
    #: no cursor and no ``since``. Default 24h.
    lookback_minutes: int = Field(default=1440, ge=1, le=129_600)
    #: Defensive per-poll cap. Default matches the source plane's hard
    #: per-poll count bound (``_MAX_ENTRIES_PER_POLL`` in the source actor).
    max_items_per_pull: int = Field(default=100, ge=1, le=10_000)

    user_agent: str = Field(default="Legba/2.0", max_length=256)
    timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("url_template")
    @classmethod
    def _validate_url_template(cls, v: str) -> str:
        try:
            parsed = list(string.Formatter().parse(v))
        except ValueError as exc:
            raise ValueError(f"malformed url_template braces: {exc}") from exc
        for _literal, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if field_name == "":
                raise ValueError("url_template placeholders must be named")
            if field_name not in URL_TEMPLATE_SUBSTITUTIONS:
                raise ValueError(
                    f"unknown url_template placeholder {{{field_name}}}; "
                    f"allowed: {sorted(URL_TEMPLATE_SUBSTITUTIONS)}"
                )
            if format_spec or conversion:
                raise ValueError(
                    "url_template placeholders may not use format specs "
                    "or conversions"
                )
        return v

    @field_validator(
        "items_path", "id_path", "title_path", "url_path",
        "timestamp_path", "body_path", "geo_path",
    )
    @classmethod
    def _validate_paths(cls, v: str | None) -> str | None:
        if v is None:
            return v
        parse_path(v)  # raises ValueError on junk
        return v

    @model_validator(mode="after")
    def _csv_constraints(self) -> "JsonApiConfig":
        if self.response_format == "csv" and self.items_path:
            raise ValueError(
                "items_path must be empty for response_format='csv' "
                "(CSV rows are the items; field paths are column names)"
            )
        return self


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class JsonApiAuthNotConfigured(RuntimeError):
    """Raised when config declares an auth SecretRef but no secrets resolver
    is wired. Fail-loud per the no-stubs rule — the source refuses to
    activate or pull rather than polling unauthenticated."""


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class JsonApiSourceHandler:
    """Generic polled JSON/CSV HTTP API source.

    L-102 conformance:

      * ``kind = "json_api"``, ``family = "source"``.
      * Owns its cursor in ``ctx.state_store`` (``last_pulled_at`` window).
      * Yields :class:`Signal` instances; idempotent — downstream
        content-hash dedupe handles window overlap.
      * Exposes ``health_check`` + lifecycle hooks; ``on_configure`` /
        ``on_activate`` refuse when declared auth cannot resolve.
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = "json_api"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.json_api/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = JsonApiConfig
    handler_version: ClassVar[str] = "0.1.0"
    # DQ-H5b (#88) — state-store key under which this handler records its poll
    # health, so the source actor can read the WHY for a non-productive poll.
    health_state_key: ClassVar[str] = _JSON_API_HEALTH_KEY

    def __init__(
        self,
        config: JsonApiConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        secret_resolver: Any = None,
    ) -> None:
        """Construct a handler bound to a parsed :class:`JsonApiConfig`.

        ``http_client`` is optional — tests inject a client wired to an
        ``httpx.MockTransport``; production creates one on demand.
        ``secret_resolver`` is the factory-threaded ``secrets_resolve``
        callable (``async (vault_id) -> str | bytes``); at pull time the
        context-supplied ``ctx.secrets_resolve`` takes precedence.
        """
        self._config = config
        self._client = http_client
        self._secret_resolver = secret_resolver

    # ------------------------------------------------------------------ pull

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Async-generator yielding one :class:`Signal` per new item.

        Window: ``window_start`` = handler cursor ``last_pulled_at`` (or
        ``since``, or ``now - lookback_minutes``); ``window_end`` = now.
        The cursor advances to ``window_end`` only after a successful fetch
        + parse, so a failed pull retries the same window.
        """
        window_start, window_end = await self._window(ctx, since)
        request_url = self._render_url(window_start, window_end)
        # Fail-loud auth resolution BEFORE any network traffic.
        auth_headers, auth_params = await self._resolve_auth(ctx)

        response = await self._fetch_with_retry(
            request_url, headers=auth_headers, params=auth_params, ctx=ctx,
        )
        if response is None:
            # Transient + retry exhausted; health probe surfaces the cause.
            return

        if response.status_code >= 400:
            await self._record_health(
                ctx,
                state="unhealthy",
                last_error=f"HTTP {response.status_code}",
                detail={"status": response.status_code},
            )
            return

        items = self._extract_items(response.text)
        if items is None:
            await self._record_health(
                ctx,
                state="unhealthy",
                last_error="response parse failure (format/items_path mismatch)",
                detail={"status": response.status_code},
            )
            return

        emitted = 0
        for item in items:
            if emitted >= self._config.max_items_per_pull:
                ctx.logger.warning(
                    "json_api.pull.max_items cap=%d url_template=%s — truncating",
                    self._config.max_items_per_pull, self._config.url_template,
                )
                break
            signal = self._item_to_signal(
                item,
                ctx=ctx,
                request_url=request_url,
                window_start=window_start,
                window_end=window_end,
            )
            if signal is None:
                continue
            if _is_not_after(signal, window_start):
                continue
            emitted += 1
            yield signal

        await ctx.state_store.set(
            _JSON_API_CURSOR_KEY,
            {"last_pulled_at": window_end.isoformat()},
        )
        await self._record_health(
            ctx,
            state="healthy",
            last_success_at=datetime.now(tz=timezone.utc),
            detail={
                "status": response.status_code,
                "items_seen": len(items),
                "items_yielded": emitted,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
            },
        )

    # ----------------------------------------------------------- health_check

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """GET probe against the rendered URL. 200 → healthy; transient →
        degraded; 4xx → unhealthy. Declared-but-unresolvable auth reports
        ``unhealthy`` (the activation guard is the raising path)."""
        window_start, window_end = await self._window(ctx, None)
        url = self._render_url(window_start, window_end)
        cursor_raw = await ctx.state_store.get(_JSON_API_CURSOR_KEY)
        cursor: dict[str, Any] = cursor_raw if isinstance(cursor_raw, dict) else {}

        try:
            auth_headers, auth_params = await self._resolve_auth(ctx)
        except JsonApiAuthNotConfigured as exc:
            return SourceHealth(
                state="unhealthy",
                last_error=str(exc),
                detail={"probe": "auth"},
            )

        try:
            client = await self._get_or_create_client()
            response = await client.get(
                url,
                headers=self._merge_with_useragent(auth_headers),
                params=auth_params or None,
                timeout=self._config.timeout_seconds,
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            return SourceHealth(
                state="degraded",
                last_error=f"probe transient: {exc!s}",
                detail={"probe": "network"},
                last_cursor=cursor.get("last_pulled_at") or None,
            )

        if response.status_code == 200:
            return SourceHealth(
                state="healthy",
                last_success_at=datetime.now(tz=timezone.utc),
                detail={"status": response.status_code},
                last_cursor=cursor.get("last_pulled_at") or None,
            )
        if response.status_code in _TRANSIENT_STATUS:
            return SourceHealth(
                state="degraded",
                last_error=f"HTTP {response.status_code}",
                detail={"status": response.status_code},
                last_cursor=cursor.get("last_pulled_at") or None,
            )
        return SourceHealth(
            state="unhealthy",
            last_error=f"HTTP {response.status_code}",
            detail={"status": response.status_code},
            last_cursor=cursor.get("last_pulled_at") or None,
        )

    # ------------------------------------------------------- lifecycle hooks

    async def on_configure(self, ctx: SourceContext) -> None:
        """Refuse configuration when declared auth cannot resolve (fail loud)."""
        self._assert_auth_resolvable(ctx)

    async def on_activate(self, ctx: SourceContext) -> None:
        """Refuse activation when declared auth cannot resolve (fail loud)."""
        self._assert_auth_resolvable(ctx)

    async def on_pause(self, ctx: SourceContext) -> None:
        return None

    async def on_resume(self, ctx: SourceContext) -> None:
        return None

    async def on_retire(self, ctx: SourceContext) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this handler owns one."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:                       # pragma: no cover
                pass
            self._client = None

    # ------------------------------------------------------------- internals

    def _assert_auth_resolvable(self, ctx: SourceContext) -> None:
        if self._config.auth is None:
            return
        if getattr(ctx, "secrets_resolve", None) is None and self._secret_resolver is None:
            raise JsonApiAuthNotConfigured(
                f"json_api source declares auth secret_ref="
                f"{self._config.auth.secret_ref!r} but no secrets resolver is "
                "wired — refusing (no-stubs rule: a keyed source never polls "
                "unauthenticated or with a fabricated credential)"
            )

    async def _resolve_auth(
        self, ctx: SourceContext,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Resolve the auth SecretRef into ``(headers, query_params)``.

        Resolved per pull, never cached past the call (DATA_SOURCES §3
        contract). Raises :class:`JsonApiAuthNotConfigured` when a resolver
        is required and absent.
        """
        auth = self._config.auth
        if auth is None:
            return {}, {}
        self._assert_auth_resolvable(ctx)
        resolver = getattr(ctx, "secrets_resolve", None) or self._secret_resolver
        raw = await resolver(auth.secret_ref)
        secret = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        value = auth.value_template.replace("{secret}", secret)
        if auth.mode == "header":
            return {auth.name: value}, {}
        return {}, {auth.name: value}

    async def _window(
        self, ctx: SourceContext, since: datetime | None,
    ) -> tuple[datetime, datetime]:
        """Compute the (start, end] poll window from the handler cursor.

        Precedence for ``start`` (mirrors the ACLED floor): handler cursor
        ``last_pulled_at`` → ``since`` → ``now - lookback_minutes``.
        """
        window_end = ctx.utcnow()
        if window_end.tzinfo is None:
            window_end = window_end.replace(tzinfo=timezone.utc)
        start: datetime | None = None

        raw = await ctx.state_store.get(_JSON_API_CURSOR_KEY)
        if isinstance(raw, dict):
            iso = raw.get("last_pulled_at")
            if isinstance(iso, str) and iso:
                try:
                    start = datetime.fromisoformat(iso)
                except ValueError:
                    start = None
        if start is None and since is not None:
            start = since
        if start is None:
            start = window_end - timedelta(minutes=self._config.lookback_minutes)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if start > window_end:
            start = window_end
        return start, window_end

    def _render_url(self, window_start: datetime, window_end: datetime) -> str:
        """Substitute the window placeholders into ``url_template``.

        Values are URL-quoted (``+`` / ``:`` in ISO timestamps) so the
        rendered URL is wire-safe. Auth is **never** rendered here — query
        auth is applied as a separate request param so the rendered URL can
        be stamped into payload/provenance without leaking a secret.
        """
        mapping = {
            "date_today": window_end.date().isoformat(),
            "date_yesterday": (window_end - timedelta(days=1)).date().isoformat(),
            "window_start_iso": quote(window_start.isoformat(), safe=""),
            "window_end_iso": quote(window_end.isoformat(), safe=""),
        }
        return self._config.url_template.format_map(mapping)

    async def _get_or_create_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = guarded_async_client(
                follow_redirects=True,
                timeout=self._config.timeout_seconds,
                headers={"User-Agent": self._config.user_agent},
            )
        return self._client

    def _merge_with_useragent(self, headers: dict[str, str]) -> dict[str, str]:
        merged = dict(headers)
        merged.setdefault("User-Agent", self._config.user_agent)
        return merged

    async def _fetch_with_retry(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        ctx: SourceContext,
    ) -> httpx.Response | None:
        """Single retry on transient (network / timeout / 5xx-transient)."""
        attempts = 0
        last_err: Exception | None = None
        while attempts <= _DEFAULT_RETRIES_FOR_TRANSIENT:
            attempts += 1
            try:
                client = await self._get_or_create_client()
                response = await client.get(
                    url,
                    headers=self._merge_with_useragent(headers),
                    params=params or None,
                    timeout=self._config.timeout_seconds,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_err = exc
                ctx.logger.warning(
                    "json_api.fetch.transient attempt=%d url=%s err=%s",
                    attempts, url, exc,
                )
                if attempts > _DEFAULT_RETRIES_FOR_TRANSIENT:
                    break
                await asyncio.sleep(0)  # cooperative yield
                continue

            if response.status_code in _TRANSIENT_STATUS:
                last_err = httpx.HTTPStatusError(
                    f"transient {response.status_code}",
                    request=response.request,
                    response=response,
                )
                ctx.logger.warning(
                    "json_api.fetch.transient attempt=%d url=%s status=%d",
                    attempts, url, response.status_code,
                )
                if attempts > _DEFAULT_RETRIES_FOR_TRANSIENT:
                    return response
                await asyncio.sleep(0)
                continue

            return response

        if last_err is not None:
            await self._record_health(
                ctx,
                state="degraded",
                last_error=f"transient retries exhausted: {last_err!s}",
                detail={"url_template": self._config.url_template},
            )
        return None

    def _extract_items(self, body_text: str) -> list[dict[str, Any]] | None:
        """Resolve the item array from the response body.

        JSON: parse, walk ``items_path``, require a list. CSV: stdlib
        ``csv.DictReader`` rows are the items (field paths = column names).
        Returns ``None`` on hard parse failure (caller maps to unhealthy).
        Non-dict array entries are skipped (field paths need objects).
        """
        if self._config.response_format == "csv":
            try:
                reader = csv.DictReader(io.StringIO(body_text))
                return [dict(row) for row in reader]
            except csv.Error as exc:
                logger.warning("json_api.parse.csv_failure: %s", exc)
                return None

        try:
            doc = json.loads(body_text)
        except (ValueError, TypeError) as exc:
            logger.warning("json_api.parse.not_json: %s", exc)
            return None

        segments = parse_path(self._config.items_path)
        node = resolve_path(doc, segments) if segments else doc
        if not isinstance(node, list):
            logger.warning(
                "json_api.parse.items_path_miss path=%r resolved_type=%s",
                self._config.items_path, type(node).__name__,
            )
            return None
        return [item for item in node if isinstance(item, dict)]

    def _item_to_signal(
        self,
        item: dict[str, Any],
        *,
        ctx: SourceContext,
        request_url: str,
        window_start: datetime,
        window_end: datetime,
    ) -> Signal | None:
        """Map one item dict to a :class:`Signal` via the configured paths."""
        cfg = self._config

        title = _resolve_str(item, cfg.title_path)
        link = _resolve_str(item, cfg.url_path)
        body = _resolve_str(item, cfg.body_path)
        external_id = _resolve_str(item, cfg.id_path) or link
        published_at = _parse_timestamp(_resolve_raw(item, cfg.timestamp_path))

        if not external_id:
            # Stable fallback: hash the item body so the same item gets the
            # same id across pulls (dedupe key).
            external_id = hashlib.sha256(
                json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
                .encode("utf-8")
            ).hexdigest()[:24]

        payload: dict[str, Any] = {
            "external_id": external_id,
            "title": title,
            "link": link,
            "raw_body": body,
            "published_at": (
                published_at.astimezone(timezone.utc).isoformat()
                if published_at is not None
                else None
            ),
            "item": item,
            "source_url": request_url,
        }
        # Stash the parsed datetime so window filtering doesn't re-parse.
        if published_at is not None:
            payload["_published_at_dt"] = published_at

        content_hash = hashlib.sha256(
            (
                (external_id or "")
                + "\x1f"
                + (title or "")
                + "\x1f"
                + (body or "")
            ).encode("utf-8")
        ).hexdigest()

        geo = _extract_geo(_resolve_raw(item, cfg.geo_path))
        if not geo:
            geo = [c for c in ctx.scope_geo if _GEO_CODE_RE.match(c or "")]

        language_hint = cfg.language
        if language_hint is None and len(ctx.scope_languages) == 1:
            language_hint = ctx.scope_languages[0]

        mime_type: str | None = None
        if cfg.modality == "structured":
            mime_type = "text/csv" if cfg.response_format == "csv" else "application/json"

        # Source-first pivot (P-06): the Signal is target-agnostic; stamp the
        # ORIGIN source id + modality. Baseline enrichment fills the rest.
        return Signal(
            source_id=ctx.source_id,
            modality=cfg.modality,
            mime_type=mime_type,
            payload=payload,
            content_hash=content_hash,
            canonical_url=link or None,
            language_hint=language_hint,
            geo=geo,
            tags=list(cfg.static_tags),
            raw_provenance={
                "kind": self.kind,
                "schema_version": self.schema_version,
                "fetch_kind": "json_api",
                "url_template": cfg.url_template,
                "request_url": request_url,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
            },
        )

    async def _record_health(
        self,
        ctx: SourceContext,
        *,
        state: str,
        last_success_at: datetime | None = None,
        last_error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "state": state,
            "last_success_at": (
                last_success_at.astimezone(timezone.utc).isoformat()
                if last_success_at is not None
                else None
            ),
            "last_error": last_error,
            "detail": detail or {},
        }
        try:
            await ctx.state_store.set(_JSON_API_HEALTH_KEY, record)
        except Exception:                                # pragma: no cover
            ctx.logger.warning("json_api.health.persist_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_raw(item: dict[str, Any], path: str | None) -> Any:
    """Resolve a configured path against one item; ``None`` path → ``None``."""
    if not path:
        return None
    return resolve_path(item, parse_path(path))


def _resolve_str(item: dict[str, Any], path: str | None) -> str:
    """Resolve a path to a trimmed string (numbers coerced; junk → '')."""
    val = _resolve_raw(item, path)
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, bool):
        return ""
    if isinstance(val, (int, float)):
        return str(val)
    return ""


#: Compact wire formats seen in the field (GDELT DOC ``seendate`` etc.).
_COMPACT_TS_FORMATS: tuple[str, ...] = (
    "%Y%m%dT%H%M%SZ",     # 20260608T120000Z (GDELT DOC artlist)
    "%Y%m%d%H%M%S",       # 20260608120000
)


def _parse_timestamp(value: Any) -> datetime | None:
    """Best-effort timestamp parse: datetime / epoch / ISO-8601 / RFC 2822 /
    compact forms. Returns a UTC-aware datetime or ``None``."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if seconds > 1e12:          # epoch milliseconds
            seconds /= 1000.0
        try:
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        dt = None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
        if dt is None:
            for fmt in _COMPACT_TS_FORMATS:
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return None
        if dt is None:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _extract_geo(value: Any) -> list[str]:
    """Normalize a ``geo_path`` extraction to ISO-ish codes (2–3 uppercase
    letters). A string or list of strings; anything else → []."""
    candidates: list[Any]
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        if not isinstance(cand, str):
            continue
        code = cand.strip().upper()
        if _GEO_CODE_RE.match(code) and code not in seen:
            seen.add(code)
            out.append(code)
    return out[:16]


def _is_not_after(signal: Signal, window_start: datetime) -> bool:
    """True iff the signal's parsed timestamp is <= ``window_start``.

    Items without a parsable timestamp are NOT filtered — they're emitted
    and downstream content-hash dedupe absorbs re-pulls.
    """
    payload_dt = signal.payload.get("_published_at_dt")
    if isinstance(payload_dt, datetime):
        a = payload_dt if payload_dt.tzinfo else payload_dt.replace(tzinfo=timezone.utc)
        b = window_start if window_start.tzinfo else window_start.replace(tzinfo=timezone.utc)
        return a <= b
    return False


__all__ = [
    "URL_TEMPLATE_SUBSTITUTIONS",
    "JsonApiAuth",
    "JsonApiAuthNotConfigured",
    "JsonApiConfig",
    "JsonApiSourceHandler",
    "parse_path",
    "resolve_path",
]
