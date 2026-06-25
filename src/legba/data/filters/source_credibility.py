# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source credibility filter (L-152).

Implements the L-102 filter-kind contract (§3 of
``design/legba_kind_contracts.md``) for the ``"source_credibility"`` kind:
annotates each in-flight :class:`legba.data.sources.Signal` with a float
credibility score in ``[0.0, 1.0]`` looked up from the registry-backed
``source_credibility`` table (created by migration 0014).

Behavior
--------

  * ``transform(signal, ctx) -> Signal | None``:
      1. Extract a source host from the signal — preferring
         :attr:`Signal.canonical_url`, falling back to a ``source_url``
         field on ``Signal.payload`` (the RSS handler stores the feed
         URL there).
      2. Normalize the host (lowercase, strip ``www.`` prefix, decode
         punycode for IDN domains).
      3. Look up the host in the ``source_credibility`` table; if no
         match, retry against progressively-trimmed subdomains
         (``news.bbc.co.uk`` → ``bbc.co.uk`` → ``co.uk``). First hit wins.
      4. Set :attr:`Signal.source_credibility` and
         :attr:`Signal.source_credibility_rationale` to the matched row
         (or both ``None`` when no match and no ``default_score`` is
         configured; the configured ``default_score`` overrides the
         null when present).
      5. Set :attr:`Signal.below_credibility_threshold` to ``True`` when
         the matched / default score is strictly below the configured
         ``min_score``. Flagging is informational and **never drops** the
         signal — the handler always returns the (possibly mutated)
         signal, never ``None``.

  * The handler is **read-only**: scores are inserted / updated via a
    separate registry API endpoint (L-113 surface; a
    ``/api/v1/registry/source_credibility`` endpoint set is the
    follow-up).

Caching
-------

A small in-memory LRU caches recent ``(normalized_host) -> (score,
rationale)`` lookups for ``cache_ttl_seconds`` (default 3600). Cache
misses still pay the DB roundtrip; cache hits skip it. The cache is
intentionally per-handler-instance and not shared across actors — the
runtime can choose to lift it into Redis later if scale demands it.

Failure semantics (L-102 §7)
----------------------------

  * No Postgres pool wired (e.g. unit test bootstrap) → handler is a
    pass-through; the signal is returned unmodified.
  * Postgres query raises → the handler logs and returns the signal
    unmodified (TransientFailure equivalent; never drops a signal because
    the credibility lookup blew up).
  * Malformed / missing URL → ``source_credibility`` left ``None``;
    optional ``default_score`` may still apply.

This module never imports from ``legba.data.runtime`` — the runtime
(L-103) is not yet landed. It depends only on the structural-typing
surface in :mod:`legba.data.filters._contract` and an injected asyncpg
pool (which the test harness wires from the migrated test database).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, ClassVar, Mapping
from urllib.parse import urlparse

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from ..sources._contract import Signal
from ._contract import FilterContext, FilterHealth


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class SourceCredibilityConfig(BaseModel):
    """Pydantic config schema for :class:`SourceCredibilityHandler`.

    Used at descriptor-validation time (per L-101 / L-102 §1). The runtime
    parses each filter binding's config block against this model before
    the handler is activated.
    """

    model_config = ConfigDict(extra="forbid")

    # Score floor below which the handler flags `below_credibility_threshold`.
    # `0.3` matches the migration's seeded mid-low band; operator override
    # is per-descriptor.
    min_score: float = Field(default=0.3, ge=0.0, le=1.0)

    # If set, unknown hosts get this score (with `default_score_rationale`
    # as the rationale). Default `None` = unknown hosts stay null.
    default_score: float | None = Field(default=None, ge=0.0, le=1.0)
    default_score_rationale: str = Field(
        default="default applied for host with no registry entry"
    )

    # In-memory cache TTL for `(host) -> (score, rationale)`. A value of 0
    # disables caching (every signal pays the DB roundtrip).
    cache_ttl_seconds: int = Field(default=3600, ge=0, le=86400)

    # Max cached entries. Beyond this, oldest entries are evicted.
    cache_max_entries: int = Field(default=4096, ge=0, le=1_048_576)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_TABLE_NAME = "source_credibility"


# ---------------------------------------------------------------------------
# Host extraction helpers (module-level so they're trivially unit-testable
# without needing a Postgres pool).
# ---------------------------------------------------------------------------


def _decode_idna(host: str) -> str:
    """Decode an IDN-encoded host (``xn--…``) to its Unicode form.

    Hosts already in Unicode are returned unchanged. Failures fall back
    to the raw host — the credibility table is operator-curated; if the
    operator stored the punycode form, exact-match still works.
    """
    # Punycode labels start with the `xn--` prefix; we only IDNA-decode
    # when at least one label looks like one to avoid spending the
    # `idna` dependency on every signal.
    if "xn--" not in host:
        return host
    try:
        import idna
        return idna.decode(host)
    except Exception:
        return host


def normalize_host(url_or_host: str | None) -> str | None:
    """Normalize a URL or bare host string for credibility lookup.

    Steps:
      1. If the value parses as a URL with a ``netloc``, use that
         netloc; otherwise treat the value as a bare host string.
      2. Strip a leading user-info (``user:pass@``) and a trailing port
         (``:8443``) if present.
      3. Lowercase.
      4. Strip a single leading ``www.`` prefix.
      5. Decode punycode (IDN) when the host contains an ``xn--`` label.

    Returns ``None`` when the input is empty or yields nothing usable.
    """
    if not url_or_host:
        return None
    text = url_or_host.strip()
    if not text:
        return None

    # Detect "looks like a URL" cheaply — `urlparse` is forgiving and
    # will happily yield empty netloc / scheme on a bare host string.
    if "://" in text:
        parsed = urlparse(text)
        host = parsed.netloc or parsed.path or ""
    else:
        # Could still be `//example.com/path` (protocol-relative) — urlparse
        # of that yields netloc populated. Try once before falling back.
        parsed = urlparse(text)
        host = parsed.netloc or text

    # Drop user-info.
    if "@" in host:
        host = host.split("@", 1)[1]

    # Drop port.
    if host.startswith("["):
        # IPv6 literal: `[::1]:8443` — keep the bracketed part.
        end = host.find("]")
        if end != -1:
            host = host[: end + 1]
    elif ":" in host:
        host = host.split(":", 1)[0]

    if not host:
        return None

    host = host.lower()

    # Single leading `www.` strip (multi-level `www.www.foo.com` is
    # nonsensical; one strip is sufficient).
    if host.startswith("www."):
        host = host[4:]

    if not host:
        return None

    host = _decode_idna(host)
    return host or None


def extract_lookup_hosts(host: str) -> list[str]:
    """Return the ordered list of hosts to probe in the credibility table.

    Strategy: try the exact host first, then progressively-trimmed
    subdomains. ``news.bbc.co.uk`` yields
    ``["news.bbc.co.uk", "bbc.co.uk", "co.uk"]``. The last single-label
    candidate is included so a hypothetical operator-registered TLD-wide
    score (rare but legitimate, e.g. an ISP-style internal domain) still
    matches; in practice, no PSL hits exist on the canonical baseline.

    A naked single-label host (``localhost``) yields ``["localhost"]``.
    A bare IPv4 / IPv6 literal yields itself only — IP-literal trimming
    is meaningless and would produce false hits.
    """
    if not host:
        return []

    # IP literals: don't trim.
    if _is_ip_literal(host):
        return [host]

    labels = host.split(".")
    candidates: list[str] = []
    while labels:
        candidates.append(".".join(labels))
        labels.pop(0)
    return candidates


def _is_ip_literal(host: str) -> bool:
    """True iff ``host`` is a literal IPv4 or IPv6 address."""
    if host.startswith("["):                          # bracketed IPv6
        return True
    if ":" in host:                                   # bare IPv6 with `:` separators
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        try:
            return all(0 <= int(p) <= 255 for p in parts)
        except ValueError:
            return False
    return False


def _extract_signal_host(signal: Signal) -> str | None:
    """Pull the host from a :class:`Signal`.

    Prefers :attr:`Signal.canonical_url` (the URL of the actual content),
    falls back to a ``source_url`` key on :attr:`Signal.payload` (the RSS
    handler stores the feed URL there), and finally falls back to
    ``payload["url"]``. Returns ``None`` if no candidate URL is present.
    """
    candidates: list[str] = []
    if signal.canonical_url:
        candidates.append(signal.canonical_url)
    if isinstance(signal.payload, Mapping):
        for key in ("source_url", "url", "link"):
            v = signal.payload.get(key)
            if isinstance(v, str) and v:
                candidates.append(v)
    for cand in candidates:
        host = normalize_host(cand)
        if host:
            return host
    return None


# ---------------------------------------------------------------------------
# LRU cache (tiny dict-based; OrderedDict semantics)
# ---------------------------------------------------------------------------


class _HostCache:
    """Per-handler-instance LRU cache of ``host -> (score, rationale, expires_at)``.

    Not thread-safe — handler instances are owned by a single async task
    per topology v2 §7.1 (Dapr virtual actor). Cache entries past
    ``expires_at`` are dropped lazily on access. Size bounded by
    ``max_entries`` — oldest evicted when full.
    """

    def __init__(self, *, ttl_seconds: int, max_entries: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        # dict preserves insertion order (Py3.7+), so we get LRU-ish
        # behavior by deleting + re-inserting on hit.
        self._data: dict[str, tuple[float | None, str | None, float]] = {}

    def get(self, key: str) -> tuple[float | None, str | None] | None:
        if self.ttl_seconds <= 0:
            return None
        entry = self._data.get(key)
        if entry is None:
            return None
        score, rationale, expires_at = entry
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        # Refresh access order.
        self._data.pop(key, None)
        self._data[key] = entry
        return score, rationale

    def put(
        self, key: str, score: float | None, rationale: str | None,
    ) -> None:
        if self.ttl_seconds <= 0 or self.max_entries <= 0:
            return
        expires_at = time.monotonic() + self.ttl_seconds
        if key in self._data:
            self._data.pop(key, None)
        self._data[key] = (score, rationale, expires_at)
        while len(self._data) > self.max_entries:
            # Evict oldest.
            oldest = next(iter(self._data))
            self._data.pop(oldest, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


# Sentinel for "no row found" inside the cache — we distinguish "looked
# up, no match" (cache hit returning None score) from "never looked up"
# (cache miss). Encoded as score=None, rationale=None in the cache.


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class SourceCredibilityHandler:
    """Filter handler for the ``"source_credibility"`` kind.

    L-102 conformance:

      * ``kind = "source_credibility"``, ``family = "filter"``.
      * ``transform(signal, ctx)`` annotates the signal with credibility
        info; always returns the signal, never ``None`` (informational
        flagging only — never drops).
      * Exposes ``health_check`` and lifecycle hooks (mostly no-op; cache
        cleared on retire).
      * ``output_contract`` declares the three fields the handler writes
        on the Signal so the registry can flag composition gaps.
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = "source_credibility"
    family: ClassVar[str] = "filter"
    schema_version: ClassVar[str] = "legba/filter.source_credibility/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = SourceCredibilityConfig
    handler_version: ClassVar[str] = "0.1.0"

    # L-102 §3: declared output type for pipeline-composition checking.
    output_contract: ClassVar[Mapping[str, type]] = {
        "source_credibility": float,
        "source_credibility_rationale": str,
        "below_credibility_threshold": bool,
    }

    # Idempotent per the L-102 idempotency convention.
    idempotent: ClassVar[bool] = True

    def __init__(
        self,
        config: SourceCredibilityConfig,
        *,
        pool: asyncpg.Pool | None = None,
    ) -> None:
        """Construct a handler bound to a parsed
        :class:`SourceCredibilityConfig`.

        Parameters
        ----------
        config:
            Validated handler config. The runtime constructs this from
            the descriptor's filter-binding ``config`` block.
        pool:
            Async Postgres pool against the L-001 substrate cluster. The
            runtime injects this from the resolved stack component
            (`pg.cluster_main` per L-125). Unit tests can pass ``None``;
            in that case the handler is a pass-through (logs a warning,
            returns the signal unmodified — never raises).
        """
        self._config = config
        self._pool = pool
        self._cache = _HostCache(
            ttl_seconds=config.cache_ttl_seconds,
            max_entries=config.cache_max_entries,
        )
        self._signals_in = 0
        self._signals_annotated = 0
        self._signals_flagged = 0
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------ transform

    async def transform(
        self,
        signal: Signal,
        ctx: FilterContext,
    ) -> Signal:
        """Annotate a signal with credibility info.

        Always returns a Signal — never ``None``. Per the brief, this
        handler is informational: sub-threshold signals are flagged but
        not dropped.
        """
        self._signals_in += 1

        host = _extract_signal_host(signal)
        if host is None:
            # No host to probe. If a default_score is configured, apply
            # it; otherwise leave the credibility annotations null.
            score, rationale = self._apply_default()
            return self._stamp(signal, score=score, rationale=rationale, host=None)

        try:
            score, rationale = await self._lookup_with_cache(host)
        except Exception as exc:                # pragma: no cover — broad safety net
            # Never block the pipeline on a credibility-lookup failure.
            self._last_error = f"{type(exc).__name__}: {exc}"
            ctx.logger.warning(
                "source_credibility.lookup_failed host=%s err=%s", host, exc,
            )
            score, rationale = self._apply_default()
            return self._stamp(signal, score=score, rationale=rationale, host=host)

        if score is None:
            score, rationale = self._apply_default(matched_none=True)

        self._last_success_at = datetime.now(tz=timezone.utc)
        return self._stamp(signal, score=score, rationale=rationale, host=host)

    # ----------------------------------------------------------- health_check

    async def health_check(self, ctx: FilterContext) -> FilterHealth:
        """Health probe — verifies the credibility table is reachable.

        ``healthy`` when the count query succeeds; ``unhealthy`` when the
        pool isn't wired or the query raises.
        """
        if self._pool is None:
            return FilterHealth(
                state="unhealthy",
                last_error="postgres pool not injected",
                signals_in_24h=self._signals_in,
                signals_out_24h=self._signals_annotated,
                detail={"reason": "pool_missing", "cache_size": len(self._cache)},
            )
        try:
            async with self._pool.acquire() as conn:
                row_count = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {_TABLE_NAME}"
                )
        except Exception as exc:
            return FilterHealth(
                state="unhealthy",
                last_error=f"{type(exc).__name__}: {exc}",
                signals_in_24h=self._signals_in,
                signals_out_24h=self._signals_annotated,
                detail={"reason": "query_failed"},
            )
        return FilterHealth(
            state="healthy",
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            signals_in_24h=self._signals_in,
            signals_out_24h=self._signals_annotated,
            detail={
                "registry_rows": int(row_count or 0),
                "cache_size": len(self._cache),
                "signals_flagged": self._signals_flagged,
            },
        )

    # ------------------------------------------------------- lifecycle hooks

    async def on_configure(self, ctx: FilterContext) -> None:
        return None

    async def on_activate(self, ctx: FilterContext) -> None:
        return None

    async def on_pause(self, ctx: FilterContext) -> None:
        return None

    async def on_resume(self, ctx: FilterContext) -> None:
        return None

    async def on_retire(self, ctx: FilterContext) -> None:
        self._cache.clear()

    # ------------------------------------------------------------- internals

    def _apply_default(
        self, *, matched_none: bool = False,
    ) -> tuple[float | None, str | None]:
        """Return the ``default_score`` config (or null)."""
        if self._config.default_score is None:
            return None, None
        rationale = self._config.default_score_rationale
        if matched_none:
            return self._config.default_score, rationale
        return self._config.default_score, rationale

    def _stamp(
        self,
        signal: Signal,
        *,
        score: float | None,
        rationale: str | None,
        host: str | None,
    ) -> Signal:
        """Return a copy of ``signal`` with credibility fields set."""
        below_threshold: bool | None = None
        if score is not None:
            below_threshold = score < self._config.min_score
            if below_threshold:
                self._signals_flagged += 1
            self._signals_annotated += 1
        return signal.model_copy(
            update={
                "source_credibility": score,
                "source_credibility_rationale": rationale,
                "below_credibility_threshold": below_threshold,
            }
        )

    async def _lookup_with_cache(
        self, host: str,
    ) -> tuple[float | None, str | None]:
        """Look up a host's credibility, going through the in-memory cache.

        Cache hit returns the cached ``(score, rationale)``. Cache miss
        runs :meth:`_lookup` and caches whatever it returns (including
        ``(None, None)`` for "no match" — we don't want a repeated DB
        roundtrip for the same unknown host).
        """
        cached = self._cache.get(host)
        if cached is not None:
            return cached
        score, rationale = await self._lookup(host)
        self._cache.put(host, score, rationale)
        return score, rationale

    async def _lookup(
        self, host: str,
    ) -> tuple[float | None, str | None]:
        """Run the DB lookup. Returns ``(None, None)`` on no match.

        Caller is responsible for caching the result.
        """
        if self._pool is None:
            # No pool: pass-through (the constructor docstring is explicit
            # about this; logging is at warning level once at startup,
            # not per-signal).
            return None, None

        candidates = extract_lookup_hosts(host)
        if not candidates:
            return None, None

        # Single round-trip with ANY($1::text[]) so subdomain stripping
        # doesn't pay N roundtrips per signal. Then resolve "first match
        # in candidates order" client-side because Postgres has no
        # natural ordering for the input array.
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT source_host, score, score_rationale
                FROM {_TABLE_NAME}
                WHERE source_host = ANY($1::text[])
                """,
                candidates,
            )

        if not rows:
            return None, None

        by_host = {r["source_host"]: r for r in rows}
        for candidate in candidates:
            row = by_host.get(candidate)
            if row is not None:
                score = float(row["score"]) if row["score"] is not None else None
                rationale = row["score_rationale"]
                return score, rationale
        return None, None

    # ---- test-only inspection helpers (not part of the contract) ----

    def cache_size(self) -> int:
        """Test helper — return the number of cached entries."""
        return len(self._cache)

    def clear_cache(self) -> None:
        """Test helper — drop every cached entry."""
        self._cache.clear()


__all__ = [
    "SourceCredibilityConfig",
    "SourceCredibilityHandler",
    "extract_lookup_hosts",
    "normalize_host",
]
