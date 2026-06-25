# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenSanctions source handler (L-134).

Implements the L-102 source-kind contract for OpenSanctions
(`https://www.opensanctions.org/`) — the consolidated catalog of sanctions
lists, politically-exposed persons (PEPs), wanted/criminal lists, and other
entities-of-interest. The handler adopts the **FollowTheMoney (FTM)**
schema for entity normalization (Sanctions, Person, Organization,
Company, Vessel, …) — see `https://followthemoney.tech/`.

Three data-access modes are supported (operator chooses per descriptor):

  * ``api`` — paginated calls against ``https://api.opensanctions.org``.
    Low volume / drill-down use case. Requires an API key (free tier
    available). Cursor: ``max(entity.last_seen)``.

  * ``bulk_csv`` — stream ``targets.simple.csv`` from
    ``https://data.opensanctions.org/datasets/latest/<dataset>/`` over
    HTTP, parsing rows as a streaming text iterator. Full-catalogue use
    case. No auth required. Cursor: ``max(row.last_seen)``.

  * ``self_hosted`` — call a locally-deployed OpenSanctions API container
    (per their license model for commercial use). Same wire format as
    ``api`` mode but on an operator-supplied base URL.

Each yielded :class:`Signal` carries the **FTM entity** in
``payload["entity_payload"]`` preserving the upstream ``properties`` dict
unchanged. Lightweight projections (``external_id``, ``published_at``,
``entity_type``, ``countries``, ``topics``) are also exposed on the
payload so downstream filters / enrichments don't have to re-parse the
FTM object.

FollowTheMoney adoption:

  * If the ``followthemoney`` Python package is installed, the handler
    constructs FTM ``EntityProxy`` instances and round-trips them via
    ``EntityProxy.to_dict()`` for embedded payload normalization.
  * If the package is absent, the handler **passes through** the upstream
    dict structure unchanged. This is intentional — we don't want to hard-
    require FTM at the handler layer, but we do want to use it whenever
    available so downstream tooling sees the canonical shape.

This module never imports from ``legba.data.runtime`` (L-103 not yet
landed). It depends only on the structural-typing surface in
``_contract.py``.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    ClassVar,
    Iterable,
    Literal,
    Mapping,
)

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ._contract import Signal, SourceContext, SourceHealth
from ._egress import guarded_async_client


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional followthemoney integration
# ---------------------------------------------------------------------------

try:                                                        # pragma: no cover
    # The `followthemoney` package ships the canonical schema catalog and
    # the `EntityProxy` model. We import lazily so this handler module is
    # safe to load whether or not the dep is installed (per L-134 brief).
    from followthemoney import model as _ftm_model           # type: ignore
    from followthemoney.proxy import EntityProxy             # type: ignore
    _HAS_FTM = True
except Exception:                                            # pragma: no cover
    _ftm_model = None                                        # type: ignore
    EntityProxy = None                                       # type: ignore
    _HAS_FTM = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class OpenSanctionsConfig(BaseModel):
    """Pydantic config schema for :class:`OpenSanctionsSourceHandler`.

    The runtime parses each ``SourceBinding.config`` against this model
    before the handler is activated (L-101 / L-102 §1).
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["api", "bulk_csv", "self_hosted"] = "bulk_csv"
    """Operator picks the access mode.

    * ``api`` — low volume / drill-down. Requires ``api_key_secret``.
    * ``bulk_csv`` — full dataset. No auth.
    * ``self_hosted`` — license-compliant high volume. Requires ``base_url``.
    """

    api_key_secret: str | None = Field(default=None, max_length=256)
    """Vault reference name (NOT the secret value). The runtime resolves
    it at call time via ``ctx.secrets`` per L-102 §7."""

    dataset: str = Field(default="default", min_length=1, max_length=128)
    """OpenSanctions dataset id, e.g. ``all`` / ``sanctions`` /
    ``us_ofac_sdn`` / ``peps`` / ``default``."""

    schema_filter: list[str] | None = Field(default=None)
    """Optional FollowTheMoney schemas to keep
    (e.g. ``["Person", "Organization", "Sanction"]``). ``None`` = keep all."""

    base_url: str | None = Field(default=None, max_length=1024)
    """Base URL override.

    * ``api`` mode: defaults to ``https://api.opensanctions.org``.
    * ``self_hosted`` mode: required, no default.
    * ``bulk_csv`` mode: defaults to
      ``https://data.opensanctions.org``.
    """

    api_page_size: int = Field(default=100, ge=1, le=1000)
    """Page size hint for ``api`` / ``self_hosted`` modes."""

    timeout_seconds: int = Field(default=60, ge=1, le=600)
    user_agent: str = Field(default="Legba/2.0 (opensanctions)", max_length=256)
    max_bulk_rows: int = Field(default=0, ge=0)
    """Cap on rows yielded per bulk pull (0 = uncapped). Useful for
    incremental backfill from a fresh cursor."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_OS_CURSOR_KEY = "opensanctions_cursor"
_OS_HEALTH_KEY = "opensanctions_health"

# Bulk high-water-mark traversal (shared with the actor's bulk-resume cursor).
# The ``bulk_csv`` mode streams one ~50k-row snapshot whose rows share a coarse
# daily ``last_seen`` — so the ``since`` cursor can NOT paginate WITHIN a
# snapshot and a since-only cursor restarts every capped pull from row 0,
# never reaching the tail. The actor publishes a row OFFSET (rows already
# traversed) under ``bulk_resume_offset``; this handler skips that many DATA
# rows and reports its per-pull walk under ``bulk_traversed`` so the actor can
# advance / reset the high-water mark. Key names mirror
# ``legba.runtime.source_actor.BULK_RESUME_OFFSET_KEY`` /
# ``...BULK_TRAVERSED_KEY`` (duplicated here to avoid a runtime import in the
# handler layer — handlers never import the Dapr runtime per L-103).
_BULK_RESUME_OFFSET_KEY = "bulk_resume_offset"
_BULK_TRAVERSED_KEY = "bulk_traversed"

_API_BASE_DEFAULT = "https://api.opensanctions.org"
_BULK_BASE_DEFAULT = "https://data.opensanctions.org"

_BULK_PATH_TEMPLATE = "/datasets/latest/{dataset}/targets.simple.csv"

_TRANSIENT_STATUS = {502, 503, 504}
_DEFAULT_RETRIES_FOR_TRANSIENT = 1

# Default topic mapping: OpenSanctions ships a `topics` field with values
# from a controlled vocabulary (sanction, role.pep, crime, etc.). The list
# below is preserved verbatim on the Signal payload; downstream filters
# (L-152 source credibility, L-154 NER, L-155 classification) consume it.

# CSV column names from `targets.simple.csv` per OpenSanctions docs.
_BULK_CSV_COLUMNS = {
    "id", "schema", "name", "aliases", "birth_date", "countries",
    "addresses", "identifiers", "sanctions", "phones", "emails",
    "dataset", "first_seen", "last_seen", "last_change",
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class OpenSanctionsSourceHandler:
    """Source handler for OpenSanctions (PEPs + sanctions + criminal lists).

    L-102 conformance:

      * ``kind = "opensanctions"``, ``family = "source"``.
      * Owns its cursor in ``ctx.state_store`` (``max(last_seen)``).
      * Yields :class:`Signal` instances with FTM entities embedded.
      * Exposes ``health_check`` and lifecycle hooks.
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = "opensanctions"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.opensanctions/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = OpenSanctionsConfig
    handler_version: ClassVar[str] = "0.1.0"
    # DQ-H5b (#88) — state-store key under which this handler records its poll
    # health, so the source actor can read the WHY for a non-productive poll.
    health_state_key: ClassVar[str] = _OS_HEALTH_KEY

    def __init__(
        self,
        config: OpenSanctionsConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        secret_resolver: Callable[[str], Awaitable[Any]] | None = None,
        bulk_local_path: str | None = None,
    ) -> None:
        """Construct a handler bound to a parsed :class:`OpenSanctionsConfig`.

        Parameters
        ----------
        config:
            Validated handler config.
        http_client:
            Optional pre-built ``httpx.AsyncClient``. Tests inject a client
            wired to a mock transport; production uses the per-pull client
            this class creates on demand.
        api_key:
            Pre-resolved API key (tests / bootstrap scripts). May be
            ``None`` for ``bulk_csv`` mode. When ``None`` and
            ``config.api_key_secret`` is set, the handler resolves the
            vault ref through ``secret_resolver`` (or
            ``ctx.secrets_resolve``) at pull time.
        secret_resolver:
            Async ``(vault_id: str) -> str | bytes`` credential resolver.
            The runtime's source factory threads
            ``StandardDeps.secrets_resolve`` (``CredentialVault.resolve``)
            into this slot; a missing vault key then raises
            ``MissingSecretError`` — the loud activation-gating failure.
        bulk_local_path:
            Test/dev hook — when set, the bulk CSV mode reads from a local
            file path instead of HTTP. Production never sets this.
        """
        self._config = config
        self._client = http_client
        self._api_key = api_key
        self._secret_resolver = secret_resolver
        self._bulk_local_path = bulk_local_path
        # Per-pull flag — set by ``_record_health`` whenever an explicit
        # failure (unhealthy/degraded) is persisted so the post-loop
        # "healthy" write in ``pull`` doesn't overwrite it.
        self._problem_recorded: bool = False

        if config.mode == "self_hosted" and not config.base_url:
            raise ValueError(
                "opensanctions self_hosted mode requires base_url"
            )

    # ------------------------------------------------------------------ pull

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Async-generator yielding :class:`Signal` per OpenSanctions entity.

        ``since`` is a hint — entities whose ``last_seen`` is strictly
        after this timestamp are emitted. Downstream dedupe handles
        overlap. The handler maintains its own cursor in
        ``ctx.state_store`` keyed by :data:`_OS_CURSOR_KEY` carrying the
        max-observed ``last_seen`` so subsequent pulls progress.
        """
        cursor_since = await self._effective_since(ctx, since)

        # Reset the per-pull problem flag — set by inner helpers when they
        # record an unhealthy/degraded health row, so the post-loop
        # "healthy" write doesn't clobber an explicit failure.
        self._problem_recorded = False

        mode = self._config.mode

        # Keyed modes: resolve the vault ref BEFORE any HTTP. A missing
        # vault key fails the pull loudly here (``MissingSecretError`` from
        # ``CredentialVault.resolve``) instead of running unauthenticated.
        if (
            mode in ("api", "self_hosted")
            and self._api_key is None
            and self._config.api_key_secret
        ):
            self._api_key = await self._resolve_api_key(ctx)

        if mode == "bulk_csv":
            iterator = self._pull_bulk_csv(ctx, cursor_since)
        elif mode == "api":
            iterator = self._pull_api(ctx, cursor_since, base_url=self._api_base())
        elif mode == "self_hosted":
            iterator = self._pull_api(
                ctx, cursor_since, base_url=str(self._config.base_url),
            )
        else:                                                # pragma: no cover
            raise ValueError(f"unknown opensanctions mode: {mode!r}")

        emitted = 0
        max_last_seen: datetime | None = None
        async for signal in iterator:
            emitted += 1
            ls = signal.payload.get("_last_seen_dt")
            if isinstance(ls, datetime):
                if max_last_seen is None or ls > max_last_seen:
                    max_last_seen = ls
            yield signal

        if max_last_seen is not None:
            await ctx.state_store.set(
                _OS_CURSOR_KEY,
                {"last_seen": max_last_seen.astimezone(timezone.utc).isoformat()},
            )

        # Only declare healthy if the inner iterator didn't already record
        # an explicit failure for this pull. Otherwise the failure record
        # stands so the operator + health endpoint surface the real cause.
        if not self._problem_recorded:
            await self._record_health(
                ctx,
                state="healthy",
                last_success_at=datetime.now(tz=timezone.utc),
                detail={
                    "mode": mode,
                    "dataset": self._config.dataset,
                    "entries_yielded": emitted,
                    "cursor_last_seen": (
                        max_last_seen.isoformat() if max_last_seen else None
                    ),
                },
            )

    # ----------------------------------------------------------- health_check

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Lightweight per-mode probe.

          * ``api`` / ``self_hosted``: GET ``/healthz`` or ``/info``.
          * ``bulk_csv``: HTTP HEAD on the dataset URL.

        Returns the last persisted health summary's cursor under
        ``last_cursor`` so callers can see incremental progress.
        """
        previous = await ctx.state_store.get(_OS_HEALTH_KEY) or {}
        last_cursor = (previous.get("detail") or {}).get("cursor_last_seen")

        try:
            client = await self._get_or_create_client()
        except Exception as exc:                             # pragma: no cover
            return SourceHealth(
                state="unhealthy",
                last_error=f"client construction failed: {exc!s}",
                last_cursor=last_cursor,
            )

        if self._config.mode == "bulk_csv":
            url = self._bulk_url()
            try:
                response = await client.head(
                    url,
                    headers=self._headers(),
                    timeout=self._config.timeout_seconds,
                    follow_redirects=True,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                return SourceHealth(
                    state="degraded",
                    last_error=f"probe transient: {exc!s}",
                    detail={"mode": "bulk_csv", "url": url},
                    last_cursor=last_cursor,
                )
            return _health_from_response(
                response,
                detail={"mode": "bulk_csv", "url": url},
                last_cursor=last_cursor,
            )

        # API / self_hosted: lightweight GET on the info endpoint.
        base = self._api_base() if self._config.mode == "api" else str(self._config.base_url)
        url = base.rstrip("/") + "/info"
        try:
            response = await client.get(
                url,
                headers=self._headers(),
                timeout=self._config.timeout_seconds,
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            return SourceHealth(
                state="degraded",
                last_error=f"probe transient: {exc!s}",
                detail={"mode": self._config.mode, "url": url},
                last_cursor=last_cursor,
            )
        return _health_from_response(
            response,
            detail={"mode": self._config.mode, "url": url},
            last_cursor=last_cursor,
        )

    # ------------------------------------------------------- lifecycle hooks

    async def on_configure(self, ctx: SourceContext) -> None:
        return None

    async def on_activate(self, ctx: SourceContext) -> None:
        return None

    async def on_pause(self, ctx: SourceContext) -> None:
        return None

    async def on_resume(self, ctx: SourceContext) -> None:
        return None

    async def on_retire(self, ctx: SourceContext) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:                                # pragma: no cover
                pass
            self._client = None

    # --------------------------------------------------------------- bulk_csv

    async def _pull_bulk_csv(
        self,
        ctx: SourceContext,
        since: datetime | None,
    ) -> AsyncIterator[Signal]:
        """Stream ``targets.simple.csv`` rows, resuming from a high-water mark.

        Uses ``httpx`` streaming GET with a chunked text-line decoder. A
        ``bulk_local_path`` test hook lets unit tests parse a fixture file
        without HTTP.

        High-water-mark traversal (the actor's bulk-resume cursor): the actor
        publishes ``bulk_resume_offset`` = the number of EMITTED signals already
        consumed from this snapshot in prior pulls. We skip exactly that many
        emit-eligible rows so each (capped) pull RESUMES where the prior one
        stopped — walking the ~50k-row dataset across pulls instead of
        restarting from row 0 every time.

        The offset unit is the EMITTED signal (the same unit the actor counts
        when it caps), NOT the raw CSV row, so the actor's processed-count and
        our skip stay aligned across the generator/consumer cap boundary: when
        the actor breaks after N processed signals it may have pulled one extra
        emit out of us (suspended at our ``yield``); advancing the offset by the
        actor's N and skipping N next pull re-emits exactly that extra row — it
        is resumed, never skipped. We report ``reached_end`` (did we drain the
        whole stream?) so the actor can tell a full walk (reset offset to 0,
        re-walk the refreshed snapshot) from a mid-stream stop (keep advancing);
        the rows count in the report is informational — the actor advances the
        mark by its own processed-count.
        """
        resume_offset = await self._bulk_resume_offset(ctx)

        # When resuming MID-SNAPSHOT (offset > 0) the row OFFSET is the cursor —
        # so suppress the per-row ``since`` filter. ``since`` here is the merged
        # ``max(caller_hint, stored max(last_seen))``; within one snapshot the
        # stored watermark would otherwise drop backlog rows whose individual
        # ``last_seen`` is older than a row already emitted this snapshot,
        # truncating the walk. On a FRESH snapshot (offset 0) the ``since``
        # filter still applies — it skips an entire already-processed older
        # snapshot (the existing incremental-backfill behaviour).
        row_since = None if resume_offset > 0 else since

        if self._bulk_local_path:
            text_iter = _aiter_local_file_lines(self._bulk_local_path)
        else:
            text_iter = self._aiter_bulk_http_lines(ctx)

        reader: csv.DictReader | None = None
        buffer: list[str] = []
        rows_yielded = 0
        cap = self._config.max_bulk_rows
        emit_index = 0        # emit-eligible rows encountered this stream
        async for line in text_iter:
            if reader is None:
                # First non-empty line is the header. Buffer it and
                # construct a DictReader keyed off it.
                if not line.strip():
                    continue
                buffer.append(line)
                reader = csv.DictReader(buffer)
                # Consume the header row immediately so subsequent lines
                # are data; the DictReader is fed line-by-line below.
                continue

            # Feed one line into the reader and re-iterate to get exactly
            # one row dict back. Building a fresh DictReader per row keeps
            # memory bounded and avoids buffering the whole file.
            row_dict = _parse_one_csv_row(reader.fieldnames or [], line)
            if not row_dict:
                continue

            entity = _bulk_row_to_ftm_entity(row_dict)
            if entity is None:
                continue

            if not self._schema_keep(entity.get("schema")):
                continue

            signal = self._entity_to_signal(
                ctx=ctx,
                entity=entity,
                since=row_since,
                provenance={
                    "mode": "bulk_csv",
                    "dataset": self._config.dataset,
                    "url": self._bulk_url(),
                },
            )
            if signal is None:
                continue

            # This row is emit-eligible. Skip the ones prior pulls already
            # consumed (the high-water mark) so we resume mid-snapshot. The
            # offset unit is the emitted signal — identical to what the actor
            # counts when it caps — so skip + advance stay boundary-aligned.
            emit_index += 1
            if emit_index <= resume_offset:
                continue

            rows_yielded += 1
            yield signal
            if cap and rows_yielded >= cap:
                # Internal handler cap (max_bulk_rows) — NOT a full walk. Report
                # not-end so the actor keeps advancing the mark past these rows.
                await self._record_bulk_traversal(
                    ctx, emitted=rows_yielded, reached_end=False,
                )
                return

        # Stream fully consumed (no internal cap) — a complete walk of the
        # snapshot from the offset to the tail. Report reached_end so the actor
        # resets the offset to 0 and the next pull re-walks the refreshed
        # snapshot from the top. (The actor ALSO infers a full walk from not
        # hitting its OWN per-poll cap; this report disambiguates the handler's
        # ``max_bulk_rows`` cap, which is NOT end-of-stream.)
        await self._record_bulk_traversal(
            ctx, emitted=rows_yielded, reached_end=True,
        )

    async def _bulk_resume_offset(self, ctx: SourceContext) -> int:
        """Read the actor-published high-water offset (emitted rows to skip)."""
        try:
            raw = await ctx.state_store.get(_BULK_RESUME_OFFSET_KEY)
        except Exception:                                        # pragma: no cover
            return 0
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    async def _record_bulk_traversal(
        self, ctx: SourceContext, *, emitted: int, reached_end: bool,
    ) -> None:
        """Persist this pull's traversal report for the actor's bulk cursor.

        ``reached_end`` is the load-bearing field — it tells the actor whether
        the snapshot was fully walked (reset the offset) or stopped early (keep
        advancing). ``emitted`` is informational only.
        """
        try:
            await ctx.state_store.set(
                _BULK_TRAVERSED_KEY,
                {"rows": int(emitted), "reached_end": bool(reached_end)},
            )
        except Exception:                                        # pragma: no cover
            ctx.logger.warning(
                "opensanctions.bulk.traversal_persist_failed", exc_info=True,
            )

    async def _aiter_bulk_http_lines(
        self, ctx: SourceContext,
    ) -> AsyncIterator[str]:
        url = self._bulk_url()
        client = await self._get_or_create_client()
        try:
            async with client.stream(
                "GET",
                url,
                headers=self._headers(),
                timeout=self._config.timeout_seconds,
                follow_redirects=True,
            ) as response:
                if response.status_code >= 400:
                    await self._record_health(
                        ctx,
                        state="unhealthy",
                        last_error=f"HTTP {response.status_code}",
                        detail={"mode": "bulk_csv", "url": url},
                    )
                    return
                async for line in response.aiter_lines():
                    yield line
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            await self._record_health(
                ctx,
                state="degraded",
                last_error=f"bulk transient: {exc!s}",
                detail={"mode": "bulk_csv", "url": url},
            )
            return

    def _bulk_url(self) -> str:
        base = self._config.base_url or _BULK_BASE_DEFAULT
        path = _BULK_PATH_TEMPLATE.format(dataset=self._config.dataset)
        return base.rstrip("/") + path

    # -------------------------------------------------------------- api mode

    async def _pull_api(
        self,
        ctx: SourceContext,
        since: datetime | None,
        *,
        base_url: str,
    ) -> AsyncIterator[Signal]:
        """Paginate the OpenSanctions ``/entities/`` listing endpoint.

        Per OpenSanctions API docs, the listing endpoint accepts a
        ``dataset`` filter and supports paging via an ``offset`` query
        param (some installations expose ``next_offset`` in the response
        envelope). We follow ``next_offset`` if present, otherwise step
        ``offset`` by ``limit``. ``modified_since`` filters to entities
        whose ``last_change`` is >= ``since`` (server-side).
        """
        client = await self._get_or_create_client()
        url = base_url.rstrip("/") + "/entities/"

        params: dict[str, Any] = {
            "dataset": self._config.dataset,
            "limit": self._config.api_page_size,
        }
        if since is not None:
            params["modified_since"] = since.astimezone(timezone.utc).isoformat()
        if self._config.schema_filter:
            # Some installations accept repeated `schema=` params; httpx
            # serializes lists as repeated keys when passed as tuples.
            params["schema"] = list(self._config.schema_filter)

        offset = 0
        seen_total = 0
        while True:
            page_params = dict(params)
            page_params["offset"] = offset
            try:
                response = await self._api_get_with_retry(
                    client, url, page_params, ctx,
                )
            except _APIPullFailed as exc:
                ctx.logger.warning("opensanctions.api.fail: %s", exc)
                return

            if response is None:
                return

            try:
                body = response.json()
            except Exception as exc:                          # pragma: no cover
                await self._record_health(
                    ctx,
                    state="degraded",
                    last_error=f"json decode: {exc!s}",
                    detail={"mode": self._config.mode, "url": url},
                )
                return

            results = body.get("results") or body.get("entities") or []
            if not isinstance(results, list) or not results:
                return

            for entity in results:
                if not isinstance(entity, Mapping):
                    continue
                if not self._schema_keep(entity.get("schema")):
                    continue
                signal = self._entity_to_signal(
                    ctx=ctx,
                    entity=dict(entity),
                    since=since,
                    provenance={
                        "mode": self._config.mode,
                        "dataset": self._config.dataset,
                        "url": url,
                    },
                )
                if signal is None:
                    continue
                yield signal
                seen_total += 1

            # Pagination handling.
            next_offset = body.get("next_offset")
            total = body.get("total") or body.get("count")
            page_count = len(results)
            if isinstance(next_offset, int):
                if next_offset == offset:
                    return                                   # safeguard against loops
                offset = next_offset
            else:
                offset += page_count
            if isinstance(total, int) and seen_total >= total:
                return
            if page_count < self._config.api_page_size:
                # Short page = end of stream for envelopes without `next_offset`.
                return

    async def _api_get_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: Mapping[str, Any],
        ctx: SourceContext,
    ) -> httpx.Response | None:
        attempts = 0
        last_err: Exception | None = None
        while attempts <= _DEFAULT_RETRIES_FOR_TRANSIENT:
            attempts += 1
            try:
                response = await client.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=self._config.timeout_seconds,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_err = exc
                if attempts > _DEFAULT_RETRIES_FOR_TRANSIENT:
                    break
                await asyncio.sleep(0)
                continue

            if response.status_code in _TRANSIENT_STATUS:
                last_err = httpx.HTTPStatusError(
                    f"transient {response.status_code}",
                    request=response.request,
                    response=response,
                )
                if attempts > _DEFAULT_RETRIES_FOR_TRANSIENT:
                    return response
                await asyncio.sleep(0)
                continue

            if response.status_code >= 400:
                await self._record_health(
                    ctx,
                    state="unhealthy",
                    last_error=f"HTTP {response.status_code}",
                    detail={"mode": self._config.mode, "url": url},
                )
                raise _APIPullFailed(
                    f"opensanctions HTTP {response.status_code}"
                )

            return response

        if last_err is not None:
            await self._record_health(
                ctx,
                state="degraded",
                last_error=f"transient retries exhausted: {last_err!s}",
                detail={"mode": self._config.mode, "url": url},
            )
        return None

    def _api_base(self) -> str:
        return self._config.base_url or _API_BASE_DEFAULT

    async def _resolve_api_key(self, ctx: SourceContext | None = None) -> str:
        """Resolve ``config.api_key_secret`` to the live API key.

        Precedence mirrors the GDELT handler: ``ctx.secrets_resolve`` (the
        L-103/L-111 runtime contract) wins when set; otherwise the
        constructor-injected ``secret_resolver`` (threaded in by
        ``legba.runtime.source_factory.build_source_handler``). With
        neither, the handler refuses loudly rather than calling the keyed
        API unauthenticated. A vault miss propagates as
        ``MissingSecretError`` from ``CredentialVault.resolve`` — the
        activation-gating failure for keyed descriptors.
        """
        resolver = None
        if ctx is not None and getattr(ctx, "secrets_resolve", None) is not None:
            resolver = ctx.secrets_resolve
        if resolver is None:
            resolver = self._secret_resolver
        if resolver is None:
            raise RuntimeError(
                "OpenSanctions handler has api_key_secret="
                f"{self._config.api_key_secret!r} but no credential resolver "
                "is bound; the runtime must inject one via "
                "SourceContext.secrets_resolve or the handler constructor."
            )
        resolved = await resolver(self._config.api_key_secret)
        if isinstance(resolved, (bytes, bytearray)):
            return bytes(resolved).decode("utf-8")
        return str(resolved)

    # ----------------------------------------------------------- HTTP plumbing

    async def _get_or_create_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = guarded_async_client(
                follow_redirects=True,
                timeout=self._config.timeout_seconds,
                headers={"User-Agent": self._config.user_agent},
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": self._config.user_agent,
            "Accept": "application/json",
        }
        if self._api_key:
            # OpenSanctions API uses `Authorization: ApiKey <key>` per
            # their docs (`https://api.opensanctions.org/`).
            headers["Authorization"] = f"ApiKey {self._api_key}"
        return headers

    # ----------------------------------------------------------------- helpers

    async def _effective_since(
        self,
        ctx: SourceContext,
        hint: datetime | None,
    ) -> datetime | None:
        """Pick the later of ``hint`` and the persisted cursor.

        ``hint`` is the runtime's idea of "we last pulled at T"; the
        persisted cursor is our own ``max(last_seen)`` from the previous
        run. We always advance — never re-pull a strictly older window.
        """
        cursor = await ctx.state_store.get(_OS_CURSOR_KEY)
        stored: datetime | None = None
        if isinstance(cursor, Mapping):
            raw = cursor.get("last_seen")
            if isinstance(raw, str):
                stored = _parse_iso8601(raw)
            elif isinstance(raw, datetime):
                stored = raw

        if hint is None:
            return stored
        if stored is None:
            return hint
        a = hint if hint.tzinfo else hint.replace(tzinfo=timezone.utc)
        b = stored if stored.tzinfo else stored.replace(tzinfo=timezone.utc)
        return max(a, b)

    def _schema_keep(self, entity_schema: Any) -> bool:
        if not self._config.schema_filter:
            return True
        if not isinstance(entity_schema, str):
            return False
        return entity_schema in self._config.schema_filter

    def _entity_to_signal(
        self,
        *,
        ctx: SourceContext,
        entity: dict[str, Any],
        since: datetime | None,
        provenance: dict[str, Any],
    ) -> Signal | None:
        """Map an OpenSanctions FTM entity dict to a :class:`Signal`.

        Embeds the FTM entity verbatim under ``payload.entity_payload``;
        also surfaces lightweight projections (external_id, published_at,
        entity_type, countries, topics) so downstream filters don't have
        to re-parse the FTM block.
        """
        external_id = (entity.get("id") or "").strip()
        schema_name = entity.get("schema")
        if not external_id and not schema_name:
            return None

        # Normalize FTM via the library if available.
        ftm_payload, ftm_props = _normalize_ftm(entity)

        # last_seen → cursor + published_at.
        last_seen_raw = (
            entity.get("last_seen")
            or _first(ftm_props.get("modifiedAt"))
            or _first(ftm_props.get("retrievedAt"))
        )
        last_seen_dt = _parse_iso8601(str(last_seen_raw)) if last_seen_raw else None

        if since is not None and last_seen_dt is not None:
            a = last_seen_dt if last_seen_dt.tzinfo else last_seen_dt.replace(tzinfo=timezone.utc)
            b = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            if a <= b:
                return None

        countries = _coerce_str_list(
            entity.get("countries") or ftm_props.get("country") or []
        )
        topics = _coerce_str_list(
            entity.get("topics") or ftm_props.get("topics") or []
        )

        canonical_url = (
            entity.get("urls")[0] if isinstance(entity.get("urls"), list) and entity.get("urls")
            else f"https://www.opensanctions.org/entities/{external_id}/" if external_id
            else None
        )

        # Title — prefer FTM caption, fall back to first name property.
        title = (
            entity.get("caption")
            or _first(ftm_props.get("name"))
            or external_id
            or schema_name
            or ""
        )

        payload: dict[str, Any] = {
            "entity_payload": ftm_payload,
            "external_id": external_id,
            "published_at": (
                last_seen_dt.astimezone(timezone.utc).isoformat()
                if last_seen_dt is not None else None
            ),
            "entity_type": schema_name,
            "countries": countries,
            "topics": topics,
            "title": title,
            "dataset": self._config.dataset,
            "datasets": _coerce_str_list(
                entity.get("datasets")
                or ftm_props.get("datasets")
                or []
            ),
            "source_url": canonical_url,
            "ftm_normalized": _HAS_FTM,
        }
        if last_seen_dt is not None:
            payload["_last_seen_dt"] = last_seen_dt

        content_basis = json.dumps(ftm_payload, sort_keys=True, default=str)
        content_hash = hashlib.sha256(content_basis.encode("utf-8")).hexdigest()

        return Signal(
            source_id=ctx.source_id,
            payload=payload,
            content_hash=content_hash,
            canonical_url=canonical_url,
            language_hint=None,
            raw_provenance={
                "fetch_kind": "opensanctions",
                **provenance,
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
                if last_success_at is not None else None
            ),
            "last_error": last_error,
            "detail": detail or {},
        }
        if state in ("unhealthy", "degraded"):
            self._problem_recorded = True
        try:
            await ctx.state_store.set(_OS_HEALTH_KEY, record)
        except Exception:                                    # pragma: no cover
            ctx.logger.warning("opensanctions.health.persist_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class _APIPullFailed(Exception):
    """Raised when a non-transient API error should terminate the pull."""


def _normalize_ftm(entity: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(entity_payload, properties_dict)``.

    If FTM is installed, builds an ``EntityProxy`` and round-trips via
    ``to_dict()`` — this validates schema names and coerces property
    values to lists per FTM semantics. If FTM is absent, the upstream
    dict is passed through verbatim with a best-effort ``properties``
    extraction.
    """
    raw_props = entity.get("properties")
    if not isinstance(raw_props, dict):
        raw_props = {}

    if _HAS_FTM and entity.get("schema"):                   # pragma: no cover
        try:
            proxy = EntityProxy.from_dict(_ftm_model, entity, cleaned=False)
            normalized = proxy.to_dict()
            props = normalized.get("properties") or {}
            if not isinstance(props, dict):
                props = {}
            return normalized, props
        except Exception as exc:
            logger.debug("ftm normalize failed (%s); passing through", exc)

    # Pass-through path. Preserve the original dict shape but ensure each
    # property value is a list (FTM convention — even single-valued
    # properties are stored as 1-element lists). This keeps downstream
    # consumers consistent across both code paths.
    listified: dict[str, Any] = {}
    for k, v in raw_props.items():
        if isinstance(v, list):
            listified[k] = v
        elif v is None:
            listified[k] = []
        else:
            listified[k] = [v]
    out = dict(entity)
    out["properties"] = listified
    return out, listified


def _bulk_row_to_ftm_entity(row: Mapping[str, str]) -> dict[str, Any] | None:
    """Construct a FTM-shaped entity dict from a ``targets.simple.csv`` row.

    ``targets.simple.csv`` is OpenSanctions' denormalized projection:
    each row is one entity with semicolon-separated multi-value cells.
    We rebuild a ``properties`` dict so downstream code sees the same
    shape regardless of whether ``api`` or ``bulk_csv`` mode produced it.
    """
    entity_id = (row.get("id") or "").strip()
    schema_name = (row.get("schema") or "").strip()
    if not entity_id or not schema_name:
        return None

    def _split(value: str | None) -> list[str]:
        if not value:
            return []
        # OpenSanctions uses `;` as the multi-value separator in the
        # `.simple.csv` format (per their dataset README).
        return [p.strip() for p in value.split(";") if p.strip()]

    name = (row.get("name") or "").strip()
    aliases = _split(row.get("aliases"))
    countries = _split(row.get("countries"))
    addresses = _split(row.get("addresses"))
    identifiers = _split(row.get("identifiers"))
    sanctions = _split(row.get("sanctions"))
    phones = _split(row.get("phones"))
    emails = _split(row.get("emails"))
    datasets = _split(row.get("dataset"))
    birth_date = (row.get("birth_date") or "").strip()
    first_seen = (row.get("first_seen") or "").strip() or None
    last_seen = (row.get("last_seen") or "").strip() or None
    last_change = (row.get("last_change") or "").strip() or None
    topics = _split(row.get("topics"))

    properties: dict[str, Any] = {}
    if name:
        properties["name"] = [name]
    if aliases:
        properties["alias"] = aliases
    if countries:
        properties["country"] = countries
    if addresses:
        properties["address"] = addresses
    if identifiers:
        properties["idNumber"] = identifiers
    if phones:
        properties["phone"] = phones
    if emails:
        properties["email"] = emails
    if birth_date:
        properties["birthDate"] = [birth_date]
    if topics:
        properties["topics"] = topics

    entity: dict[str, Any] = {
        "id": entity_id,
        "schema": schema_name,
        "caption": name or entity_id,
        "properties": properties,
        "datasets": datasets,
        "countries": countries,
        "topics": topics,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "last_change": last_change,
        "sanctions": sanctions,
    }
    return entity


def _parse_one_csv_row(field_names: Iterable[str], line: str) -> dict[str, str]:
    """Parse a single CSV line into a ``{field: value}`` dict.

    Building a fresh DictReader per line is wasteful in the general case,
    but for streaming OpenSanctions bulk dumps the cost is negligible vs.
    the network read time and it keeps memory bounded.
    """
    line = line.rstrip("\r\n")
    if not line:
        return {}
    reader = csv.reader(io.StringIO(line))
    row_values = next(reader, None)
    if not row_values:
        return {}
    field_list = list(field_names)
    result: dict[str, str] = {}
    for i, name in enumerate(field_list):
        result[name] = row_values[i] if i < len(row_values) else ""
    return result


async def _aiter_local_file_lines(path: str) -> AsyncIterator[str]:
    """Async-iterator over local file lines (test/dev hook)."""
    # Synchronous read inside the iterator is fine — local fixtures only.
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            yield line.rstrip("\n")


def _coerce_str_list(value: Any) -> list[str]:
    """Coerce any iterable-ish into a flat ``list[str]``."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    if isinstance(value, tuple):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _first(value: Any) -> Any:
    """Return the first element of a list/tuple, else the value itself."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _parse_iso8601(raw: str) -> datetime | None:
    """Parse an OpenSanctions timestamp (ISO-8601, ``Z`` allowed)."""
    if not raw:
        return None
    try:
        # Allow both `2024-01-02T03:04:05Z` and ``2024-01-02 03:04:05``.
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _health_from_response(
    response: httpx.Response,
    *,
    detail: dict[str, Any],
    last_cursor: str | None,
) -> SourceHealth:
    if response.status_code in (200, 304):
        return SourceHealth(
            state="healthy",
            last_success_at=datetime.now(tz=timezone.utc),
            detail={**detail, "status": response.status_code},
            last_cursor=last_cursor,
        )
    if response.status_code in _TRANSIENT_STATUS:
        return SourceHealth(
            state="degraded",
            last_error=f"HTTP {response.status_code}",
            detail={**detail, "status": response.status_code},
            last_cursor=last_cursor,
        )
    return SourceHealth(
        state="unhealthy",
        last_error=f"HTTP {response.status_code}",
        detail={**detail, "status": response.status_code},
        last_cursor=last_cursor,
    )


__all__ = [
    "OpenSanctionsConfig",
    "OpenSanctionsSourceHandler",
]
