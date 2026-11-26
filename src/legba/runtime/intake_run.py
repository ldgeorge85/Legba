# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run ONE claimed ingestion request through the SAME acquisition code and
sinks a poll uses — there is no second write path.

How each target runs:

* ``address`` / ``stored_version`` — the pipeline descriptor (a source) is
  copied with its config's address substituted by the kind's ADDRESS ADAPTER
  (:data:`legba.data.intake_contract.ADDRESS_ADAPTERS`: a ``scraper``
  pipeline gets ``seed_urls=[address], max_depth=0``; an ``rss`` pipeline
  gets ``url=address``). Nothing else of the descriptor changes: scope,
  licence stamps, ingestion filters, the enrichment chain, the sink family
  and the poll bounds are the pipeline's own.
* ``source_window`` — the source's own descriptor, unchanged, with its cursor
  started at the window start.

The run is :meth:`SourceCore.pull_once` itself, on an :class:`IntakeCore`
whose actor id is ``intake::<request id>`` — so its cursor and handler state
(ETags, the scraper's visited list) live under the REQUEST, never under the
source's live actor, and are deleted when the run ends. Two things differ
from a scheduled poll, both outside acquisition:

1. the per-poll provenance row goes to the request's ``result`` instead of
   ``source_poll_outcomes`` — a request is not the source's scheduled poll,
   and writing it into that ledger would move the liveness watchdog's runs
   and the auto-pause latch;
2. a ``receipt`` sink runs the handler under the same capture scope and the
   same receipt writer, then drops the records instead of handing them to a
   sink: the fetched version (and the drift row the receipt writer notes for
   a changed page) is the whole product.

What the request learns about its own fetch comes from the capture log the
receipt scope keeps (the same log the receipts are written from): the status
the address answered, or that it was never fetched (robots disallowed, the
transport's egress guard refused a name that resolves privately, a transport
error) — the latter is retried under the retry policy; a 4xx is a ``done``
request that records the page as gone (and, for a page with a held version,
one ``gone`` drift row).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..data.intake_contract import address_config
from ..data.sources.fetch_capture import CaptureLog, _ACTIVE as _ACTIVE_CAPTURE_LOG
from .fetch_receipts import ReceiptRecorder, VersionWriter
from . import page_landing
from .intake_context import IntakeRun, intake_run_scope
from .source_actor import SourceCore, SourceDeps, resolve_poll_bounds, resolve_source_deps
from .source_poll_records import captured, protocol_posture

logger = logging.getLogger(__name__)

__all__ = ["IntakeCore", "RunOutcome", "run_request"]

DepsResolver = Callable[[str], Awaitable[SourceDeps | None]]


@dataclass
class RunOutcome:
    """What a run did. ``error`` set = the run failed (``permanent`` = do
    not retry)."""

    result: dict[str, Any] = field(default_factory=dict)
    version_id: int | None = None
    records: list[tuple[str, Any]] = field(default_factory=list)
    error: str | None = None
    permanent: bool = False


class _ObservedHandler:
    """The pipeline's handler, unchanged, with one addition: it keeps a
    reference to the capture log its steps run under (the receipt scope the
    poll opens around every handler step), so the run can read what the
    address answered. Every attribute is the inner handler's."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.capture_log: CaptureLog | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def pull(self, ctx: Any, since: Any = None) -> Any:
        self.capture_log = _ACTIVE_CAPTURE_LOG.get()
        async for item in self._inner.pull(ctx, since):
            yield item


class IntakeCore(SourceCore):
    """A :class:`SourceCore` serving one request: the provenance row of the
    run is kept on the core (the request's result), not written to the
    source's poll ledger. Everything else is :class:`SourceCore`."""

    def __init__(self, actor_id: str, sd: SourceDeps) -> None:
        super().__init__(actor_id, sd)
        self.observed: _ObservedHandler | None = None
        self.poll_outcome: dict[str, Any] | None = None
        #: I-2: how each page record landed (new / collapsed / filled) — the
        #: document sink appends here (runtime/page_landing.land).
        self.page_landings: list[Any] = []

    def _build_handler(self) -> Any:
        if self.observed is None:
            self.observed = _ObservedHandler(super()._build_handler())
        return self.observed

    async def _record_poll_outcome(self, ctx: Any, handler: Any, **kw: Any) -> None:
        self.poll_outcome = {
            "errored": kw.get("errored"), "capped": bool(kw.get("capped")),
            "signals_written": int(kw.get("signals_written") or 0),
            "reserve_unchanged": int(kw.get("reserve_unchanged") or 0),
            "sink_counts": kw.get("sink_counts") or None,
        }

    async def receipt_pass(self) -> dict[str, Any]:
        """The handler's fetch under the same capture scope and receipt writer
        a poll uses; records are counted and dropped (``receipt`` sink)."""
        ctx = self._make_context()
        handler = self._build_handler()
        budget_s, max_entries = resolve_poll_bounds(self.descriptor, handler)
        deadline = time.monotonic() + budget_s
        receipts = ReceiptRecorder(source_id=self.descriptor.identity.id)
        posture = protocol_posture(self.descriptor, {})
        seen = 0
        errored = None
        try:
            async for _record in captured(handler.pull(ctx, None), receipts.log, posture):
                seen += 1
                if seen >= max_entries or time.monotonic() > deadline:
                    break
        except Exception as exc:  # the handler's own failure: the run reports it
            errored = str(exc)
        async with self.deps.pg_pool.acquire() as conn:
            await receipts.flush(conn)
        return {"records_seen_not_written": seen, "errored": errored}


#: Pipeline deps are re-read from the registry at most this often.
_DEPS_TTL_S = 300.0
_DEPS_CACHE: dict[str, tuple[float, SourceDeps]] = {}


async def _registry_resolve(source_id: str) -> SourceDeps | None:
    """The pipeline's head through the host's registered source-deps
    resolver, kept for :data:`_DEPS_TTL_S` — not the actors' process-lifetime
    cache, so a pipeline descriptor edited after boot is honoured within
    five minutes, and its enrichment chain is not rebuilt per request."""
    from . import source_actor  # noqa: PLC0415

    hit = _DEPS_CACHE.get(source_id)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    resolver = source_actor._SOURCE_DEPS_RESOLVER
    if resolver is None:   # no host wired (a bare process): the cache path
        return await resolve_source_deps(f"source::{source_id}::")
    sd = await resolver(f"source::{source_id}::")
    if sd is not None:
        _DEPS_CACHE[source_id] = (time.monotonic() + _DEPS_TTL_S, sd)
    return sd


def _captures_for(log: CaptureLog | None, address: str) -> list[Any]:
    if log is None:
        return []
    from ..data._url_canon import canonical_url  # noqa: PLC0415

    want = canonical_url(address) or address
    return [c for c in log.captures if (canonical_url(c.url) or c.url) == want]


async def _cleanup_state(pool: Any, actor_id: str) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM public.actor_filter_state WHERE actor_id = $1", actor_id)
    except Exception:
        logger.warning("intake.state_cleanup_failed actor_id=%s", actor_id, exc_info=True)


async def _version_after(
    pool: Any, canonical: str, since: datetime, *, final: str | None = None, sha256: str | None = None,
) -> tuple[int | None, int]:
    """The version this run's fetch wrote (and the drift rows on the address).

    A redirected page's receipt is written under the address the bytes came
    FROM (its final URL — the receipt says what that address answered). When
    nothing landed under the requested address, the version under ``final``
    whose body is exactly the page's (``sha256``) is the request's receipt:
    linked whether the page was landed or refused (the Federal
    Register wall left ``result_version_id`` null)."""
    async with pool.acquire() as conn:
        vid = await conn.fetchval(
            "SELECT id FROM fetch_versions WHERE address = $1 AND last_seen_at >= $2 "
            "ORDER BY last_seen_at DESC, id DESC LIMIT 1", canonical, since,
        )
        if vid is None and final and sha256 and final != canonical:
            vid = await conn.fetchval(
                "SELECT id FROM fetch_versions WHERE address = $1 AND sha256 = $2 AND last_seen_at >= $3 "
                "ORDER BY last_seen_at DESC, id DESC LIMIT 1", final, sha256, since,
            )
        drift = await conn.fetchval(
            "SELECT count(*) FROM fetch_drift_observations WHERE address = $1 AND observed_at >= $2",
            canonical, since,
        )
    return (int(vid) if vid is not None else None), int(drift or 0)


async def run_request(
    req: Any, *, pool: Any, resolve: DepsResolver | None = None,
) -> RunOutcome:
    """Run one claimed request (``req`` has the row's columns as attributes).
    Never raises for a run failure — the outcome says what happened."""
    resolve = resolve or _registry_resolve
    started = datetime.now(tz=timezone.utc)
    sd = await resolve(req.pipeline_source_id)
    if sd is None:
        return RunOutcome(error=f"pipeline_unresolved: {req.pipeline_source_id}", permanent=True)
    desc = sd.descriptor
    address = req.address
    run = IntakeRun(
        request_id=req.id, depth=int(req.depth),
        **{k: getattr(req, k, None) for k in (
            "reason", "requested_by", "on_behalf_of", "link_kind", "parent_request_id", "parent_signal_id")},
    )
    is_page = desc.identity.kind == "page"
    if req.target_kind in ("address", "stored_version"):
        cfg = address_config(desc.identity.kind, desc.config or {}, address)
        if cfg is None:
            return RunOutcome(error=f"pipeline_not_address_capable: {desc.identity.kind}", permanent=True)
        if is_page and address:
            # I-2: the host licence ledger (a refusing host is never fetched)
            # and the publisher, both need the database the handler lacks.
            prep = await page_landing.prepare(pool, address)
            if prep.refusal is not None:
                return RunOutcome(result={"page": {"refusal": "license_refused"}}, error=prep.refusal,
                                  permanent=True)
            cfg = {**cfg, **page_landing.config_overlay(run, prep)}
        desc = desc.model_copy(update={"config": cfg})
    actor_id = f"intake::{req.id}"
    core = IntakeCore(actor_id, SourceDeps(
        descriptor=desc, deps=sd.deps, handler=None, extractors=sd.extractors,
        enrichment_stage=sd.enrichment_stage,
    ))
    out = RunOutcome()
    try:
        if req.target_kind == "source_window":
            await core._state_store().set("cursor", {
                "source_id": desc.identity.id,
                "last_pulled_at": req.window_start.isoformat(),
            })
        with intake_run_scope(run):
            if req.sink == "receipt":
                summary = await core.receipt_pass()
                out.result = dict(summary)
                errored = summary.get("errored")
            else:
                summary = await core.pull_once()
                written = list(summary.get("signal_ids") or [])
                out.records = [("signal", sid) for sid in written]
                out.result = {
                    "signals_written": len(written),
                    "sink_counts": summary.get("sink_counts"),
                    "poll_outcome": core.poll_outcome,
                }
                errored = summary.get("error")
    except Exception as exc:
        logger.warning("intake.run.error id=%s err=%s", req.id, exc, exc_info=True)
        await _cleanup_state(pool, actor_id)
        return RunOutcome(error=f"run_error: {exc}"[:2000])
    await _cleanup_state(pool, actor_id)
    if errored:
        return RunOutcome(result=out.result, error=f"handler_error: {errored}"[:2000])
    page = getattr(core.observed, "last_outcome", None) if (is_page and core.observed) else None
    if core.page_landings:   # I-2: collapsed into / filled in a held document
        out.result["landing"] = [lnd.as_result() for lnd in core.page_landings]
        out.records += [("signal", lnd.existing_signal_id) for lnd in core.page_landings
                        if lnd.action != "new" and lnd.existing_signal_id is not None]
        await page_landing.link_landings(pool, core.page_landings)
    if address:
        from ..data._url_canon import canonical_url  # noqa: PLC0415

        canonical = canonical_url(address) or address
        caps = _captures_for(core.observed.capture_log if core.observed else None, address)
        status = caps[-1].status if caps else None
        if page is not None:
            # The page kind says what it did (a redirected page's capture is
            # under its FINAL url, so the address filter can miss it).
            out.result["page"] = page
            status = page.get("status", status)
            err, permanent = page_landing.run_error(page)
            if err is not None:
                return RunOutcome(result=out.result, error=err, permanent=permanent)
        out.result["address_status"] = status
        if status is not None and not (200 <= status < 300):
            if status >= 500 or status == 429:
                return RunOutcome(result=out.result, error=f"upstream_status: {status}")
            if page is not None and page.get("refusal") == "blocked_by_challenge":
                out.result["blocked"] = page.get("detail")   # refused by an edge, not gone
            else:
                out.result["gone"] = True
            if req.sink == "receipt" and caps and not out.result.get("blocked"):
                try:
                    async with pool.acquire() as conn:
                        await VersionWriter().note_gone(
                            conn, url=address, status=status, fetched_at=caps[-1].fetched_at,
                            source_id=desc.identity.id, observed_by="ingest",
                        )
                except Exception:
                    logger.warning("intake.note_gone_failed id=%s", req.id, exc_info=True)
        elif page is None and not caps and core.observed is not None and core.observed.capture_log is not None:
            return RunOutcome(
                result=out.result,
                error="not_fetched: the address was not fetched (robots disallowed, the "
                      "egress guard refused it, or a transport error)",
            )
        final_url = (page or {}).get("final_url")
        out.version_id, drift = await _version_after(
            pool, canonical, started,
            final=(canonical_url(final_url) or final_url) if final_url else None,
            sha256=(page or {}).get("body_sha256"),
        )
        out.result["version_id"] = out.version_id
        out.result["drift_observed"] = drift
        if req.target_kind == "stored_version":
            out.result["rechecked_version_id"] = int(req.version_id)
            out.result["changed"] = out.version_id is not None and out.version_id != int(req.version_id)
    return out
