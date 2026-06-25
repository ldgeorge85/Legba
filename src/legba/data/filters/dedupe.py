# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dedupe 4-tier filter handler (L-151).

Implements the L-102 filter-kind contract for progressive signal
deduplication. Four tiers ordered cheapest-first:

  1. **URL exact** — canonicalize (strip fragment, sort query params,
     lowercase host), SHA-256 hash, look up in a Redis set. 7-day TTL.
  2. **Content hash** — normalize body text (HTML strip, whitespace
     collapse, lowercase), SHA-256, look up in a Redis set. 7-day TTL.
  3. **Semantic (vector)** — embed title + summary via an L-122 embedding
     service handed in as a typed sub-connection (per L-102 §5 sub-ports
     pattern); cosine-similar above threshold (default 0.92) against a
     per-target Qdrant dedupe collection.
  4. **Temporal** — same `source_id` + title Levenshtein-normalized
     distance < 0.15 + within N-hour window. Title cache held in Redis
     sorted-set per `(target_id, source_id)`.

Marks duplicates, doesn't drop:

  * On a hit: ``signal.payload["duplicate_of"]`` = the matched external
    id; ``signal.payload["dedupe_tier"]`` = 1..4. The handler returns the
    signal (mutated). Downstream filters / consumers honor the flag.
  * On a miss: the handler inserts the URL hash into Tier 1, content
    hash into Tier 2, embedding vector into Tier 3 Qdrant, and the title
    record into Tier 4 cache. Then returns the signal unchanged.

Performance: tiers short-circuit on first match. Vector lookup is the
expensive tier (~10ms typical); only invoked if Tiers 1 and 2 miss.

Real implementation — no mocks. Substrate boundaries (Redis + Qdrant +
embedding service) are typed Protocols; tests pass real clients
(integration) or process-local fakes for the embedding port (since the
L-122 production handler may not have landed yet at the time this lands).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    ClassVar,
    Mapping,
    Protocol,
    runtime_checkable,
)
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..sources._contract import Signal
from ._contract import FilterContext, FilterHealth


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed sub-connection ports (per L-102 §5 / §7 — typed-port pattern).
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingService(Protocol):
    """Minimal embedding-service port the dedupe handler depends on.

    The L-122 BGE-M3 handler (and any sibling backend) satisfies this
    surface. Until L-122 lands, tests pass a process-local fake that
    implements the same shape — same code path, no production behavior
    change when the real handler arrives.
    """

    async def embed(self, text: str) -> list[float]: ...


@runtime_checkable
class RedisLike(Protocol):
    """Structural subset of ``redis.asyncio.Redis`` the dedupe handler uses.

    All methods are async. Tier 1/2 mapping uses ``set`` / ``get`` /
    ``expire`` on per-hash keys (recovers the matched ``external_id`` in
    one round-trip). Tier 4 uses sorted sets keyed per
    ``(target_id, source_id)``. Tests pass either a real
    ``redis.asyncio.Redis`` instance or a process-local fake implementing
    the same surface.
    """

    async def get(self, name: str) -> Any: ...
    async def set(self, name: str, value: Any, ex: int | None = ...) -> Any: ...
    async def expire(self, name: str, seconds: int) -> bool: ...
    async def zadd(self, name: str, mapping: Mapping[str, float]) -> int: ...
    async def zrangebyscore(
        self,
        name: str,
        min: float,
        max: float,
        withscores: bool = ...,
    ) -> list: ...
    async def zremrangebyscore(
        self, name: str, min: float, max: float
    ) -> int: ...


@runtime_checkable
class QdrantLike(Protocol):
    """Structural subset of ``qdrant_client.AsyncQdrantClient`` we depend on.

    The real client implements a much larger surface — we only use these
    four methods plus collection management. Uses ``query_points`` (the
    qdrant-client 1.10+ unified query API; ``search`` is deprecated).
    """

    async def get_collections(self) -> Any: ...
    async def create_collection(self, **kwargs: Any) -> Any: ...
    async def query_points(self, **kwargs: Any) -> Any: ...
    async def upsert(self, **kwargs: Any) -> Any: ...


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TierToggle(BaseModel):
    """Per-tier enable / disable + tunables."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class Tier3Config(TierToggle):
    """Tier 3 vector similarity tunables."""

    threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    embedding_dim: int = Field(default=1024, ge=8, le=8192)
    collection_prefix: str = Field(default="legba_dedup")


class Tier4Config(TierToggle):
    """Tier 4 temporal tunables."""

    window_hours: int = Field(default=24, ge=1, le=24 * 30)
    # Levenshtein-normalized distance < threshold => duplicate.
    distance_threshold: float = Field(default=0.15, ge=0.0, le=1.0)
    # Per-source title cache size cap (most-recent N).
    max_cached_titles_per_source: int = Field(default=500, ge=10, le=10_000)


class Dedupe4TierConfig(BaseModel):
    """Pydantic config schema for :class:`Dedupe4TierHandler`.

    Operator-configurable tiers (L-248): the ``tiers`` field is the
    authoritative gate on which tiers run for this descriptor. Defaults to
    all four. Operators set ``[1, 2]`` for cheap-only targets where the
    semantic+temporal long tail doesn't pay (skipping tier 3 also avoids
    creating a per-target Qdrant collection — the linear-in-targets storage
    cost the L-248 review item flags).

    The per-tier ``tierN.enabled`` toggles still exist for back-compat with
    callers (and the Phase 5a pipeline runner that constructs configs with
    tiers 3+4 individually disabled). At runtime a tier executes iff it
    appears in ``tiers`` AND its ``enabled`` flag is True — the AND is
    deliberate so a descriptor opting down via ``tiers=[1, 2]`` and a
    runtime opting down via ``tier3.enabled=False`` agree on the result.
    """

    model_config = ConfigDict(extra="forbid")

    tier1: TierToggle = Field(default_factory=TierToggle)
    tier2: TierToggle = Field(default_factory=TierToggle)
    tier3: Tier3Config = Field(default_factory=Tier3Config)
    tier4: Tier4Config = Field(default_factory=Tier4Config)

    # L-248: per-descriptor selector. Default = all four. Must be a sorted-
    # ascending subset of {1, 2, 3, 4} with no duplicates.
    tiers: list[int] = Field(
        default=[1, 2, 3, 4],
        description=(
            "Per-descriptor selector for which dedupe tiers run. Subset "
            "of [1, 2, 3, 4], sorted ascending, no duplicates. Operator "
            "opts down by listing only the tiers they want (e.g. [1, 2] "
            "for cheap-only targets — skips tier-3 Qdrant collection too)."
        ),
    )

    # Common 7-day TTL on URL / content sets per the brief.
    set_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=60)
    # Redis key prefix; lets multiple deployments share a Redis without
    # colliding on dedup sets.
    redis_key_prefix: str = Field(default="legba:dedup")

    @field_validator("tiers", mode="after")
    @classmethod
    def _validate_tiers(cls, v: list[int]) -> list[int]:
        """Enforce: non-empty subset of {1, 2, 3, 4}, sorted ascending, unique."""
        allowed = {1, 2, 3, 4}
        if not v:
            raise ValueError(
                "tiers must be non-empty; specify at least one of [1, 2, 3, 4]"
            )
        bad = [t for t in v if t not in allowed]
        if bad:
            raise ValueError(
                f"tiers entries must be in {sorted(allowed)}; got invalid: {bad}"
            )
        if len(set(v)) != len(v):
            raise ValueError(f"tiers must not contain duplicates; got {v}")
        if list(v) != sorted(v):
            raise ValueError(f"tiers must be sorted ascending; got {v}")
        return list(v)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


_TIER_URL = 1
_TIER_CONTENT = 2
_TIER_SEMANTIC = 3
_TIER_TEMPORAL = 4


class Dedupe4TierHandler:
    """Four-tier progressive deduplication filter.

    L-102 conformance:

      * ``kind = "dedupe_4tier"``, ``family = "filter"``.
      * ``transform(signal, ctx) -> Signal`` — never returns ``None``
        (this handler marks, doesn't drop).
      * Idempotent on ``(signal.content_hash, handler_version)`` — re-
        running the same signal against the same substrate yields the
        same annotation (the second run hits the tier 1 / 2 set we just
        populated and returns the signal marked as a duplicate of
        itself; downstream handlers honor or ignore as configured).
      * Exposes ``health_check`` and lifecycle hooks.
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = "dedupe_4tier"
    family: ClassVar[str] = "filter"
    schema_version: ClassVar[str] = "legba/filter.dedupe_4tier/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = Dedupe4TierConfig
    handler_version: ClassVar[str] = "0.1.0"

    # output_contract: per L-102 §3, names → expected types on Signal.payload
    # this filter adds.
    output_contract: ClassVar[Mapping[str, type]] = {
        "payload.duplicate_of": str,
        "payload.dedupe_tier": int,
    }

    def __init__(
        self,
        config: Dedupe4TierConfig,
        *,
        redis: RedisLike,
        qdrant: QdrantLike | None = None,
        embedder: EmbeddingService | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Construct a handler.

        Parameters
        ----------
        config:
            Validated handler config.
        redis:
            Async Redis-like client. Used for Tier 1 / 2 sets and Tier 4
            sorted sets.
        qdrant:
            Async Qdrant-like client. Required if Tier 3 is enabled;
            ignored otherwise.
        embedder:
            L-122 embedding-service port. Required if Tier 3 is enabled.
        clock:
            ``time.time``-shaped callable. Tests inject a deterministic
            clock; production uses ``time.time``.
        """
        self._config = config
        self._redis = redis
        self._qdrant = qdrant
        self._embedder = embedder
        self._clock = clock or time.time

        # L-248: resolve the effective per-tier on/off from the AND of the
        # descriptor-level ``tiers`` selector and the per-tier ``enabled``
        # toggle. Computed once at construction so ``transform`` is a single
        # set membership check per tier.
        selected = set(self._config.tiers)
        self._active_tiers: frozenset[int] = frozenset({
            t for t in (_TIER_URL, _TIER_CONTENT, _TIER_SEMANTIC, _TIER_TEMPORAL)
            if t in selected and self._tier_enabled(t)
        })

        # Per-actor counters for health-check; reset by the runtime per
        # rolling-24h window (not implemented here; runtime owns it).
        self._counters = {
            "signals_in": 0,
            "signals_out": 0,
            "signals_dropped": 0,
            "tier_hits": {1: 0, 2: 0, 3: 0, 4: 0},
        }

        # Lazy: track collections we've ensure-created so repeat target
        # writes don't pay the round-trip.
        self._ensured_collections: set[str] = set()

        # Last error surfaced via health_check.
        self._last_error: str | None = None
        self._last_success_at: datetime | None = None

    # --------------------------------------------------------- tier gating

    def _tier_enabled(self, tier: int) -> bool:
        """Map tier number → the per-tier ``enabled`` flag on the config.

        The handler also AND-gates tier-3 on the qdrant + embedder ports
        being supplied (a missing port at construction time silently
        disables tier 3 — the integration path L-122 owns).
        """
        if tier == _TIER_URL:
            return self._config.tier1.enabled
        if tier == _TIER_CONTENT:
            return self._config.tier2.enabled
        if tier == _TIER_SEMANTIC:
            return (
                self._config.tier3.enabled
                and self._qdrant is not None
                and self._embedder is not None
            )
        if tier == _TIER_TEMPORAL:
            return self._config.tier4.enabled
        return False                                            # pragma: no cover

    def is_tier_active(self, tier: int) -> bool:
        """Public probe: is ``tier`` active for this handler?

        True iff the tier is both listed in ``config.tiers`` AND its
        per-tier ``enabled`` flag is True (AND for tier 3, the embedder +
        qdrant ports were supplied at construction).
        """
        return tier in self._active_tiers

    # --------------------------------------------------------- transform

    async def transform(
        self,
        signal: Signal,
        ctx: FilterContext,
    ) -> Signal | None:
        """Run the four tiers in order; mark on first match.

        Brief: ``transform(signal, target_ctx) -> Signal | None``. This
        handler never returns ``None`` — it marks duplicates and lets
        downstream filtering decide drop policy.
        """
        self._counters["signals_in"] += 1
        ext_id = _external_id(signal)

        try:
            # Tiers execute in order. The L-248 ``tiers`` selector + each
            # per-tier ``enabled`` toggle were resolved into
            # ``self._active_tiers`` at construction; one membership check
            # per tier is all the gate needs at the hot path.

            # ---------- Tier 1: URL exact ------------------------------
            if _TIER_URL in self._active_tiers:
                url_hash = self._canonical_url_hash(signal)
                if url_hash is not None:
                    hit = await self._tier1_lookup(ctx, url_hash)
                    if hit is not None:
                        return self._mark(signal, hit, _TIER_URL)

            # ---------- Tier 2: content hash ---------------------------
            if _TIER_CONTENT in self._active_tiers:
                content_hash = self._normalized_content_hash(signal)
                if content_hash:
                    hit = await self._tier2_lookup(ctx, content_hash)
                    if hit is not None:
                        return self._mark(signal, hit, _TIER_CONTENT)

            # ---------- Tier 3: semantic vector ------------------------
            # When tier 3 isn't active we skip the embedding call AND the
            # per-target Qdrant collection ensure: the L-248 storage-cost
            # win lives here.
            if _TIER_SEMANTIC in self._active_tiers:
                vec = await self._embed_for_signal(signal)
                if vec is not None:
                    hit = await self._tier3_lookup(ctx, vec)
                    if hit is not None:
                        return self._mark(signal, hit, _TIER_SEMANTIC)
            else:
                vec = None  # not produced

            # ---------- Tier 4: temporal -------------------------------
            if _TIER_TEMPORAL in self._active_tiers:
                hit = await self._tier4_lookup(ctx, signal)
                if hit is not None:
                    return self._mark(signal, hit, _TIER_TEMPORAL)

            # ---------- Miss: insert into all active tiers -------------
            await self._insert_unique(ctx, signal, ext_id, vec)
            self._last_success_at = datetime.now(tz=timezone.utc)
            self._counters["signals_out"] += 1
            return signal
        except Exception as exc:                            # pragma: no cover
            self._last_error = f"transform: {exc!s}"
            ctx.logger.warning(
                "dedupe.transform.error target=%s err=%s",
                ctx.target_id, exc,
            )
            # Fail-open: emit the signal unmarked so the pipeline doesn't
            # stall on a transient backend hiccup.
            self._counters["signals_out"] += 1
            return signal

    # ----------------------------------------------------- health_check

    async def health_check(self, ctx: FilterContext) -> FilterHealth:
        return FilterHealth(
            state="healthy" if self._last_error is None else "degraded",
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            signals_in_24h=self._counters["signals_in"],
            signals_out_24h=self._counters["signals_out"],
            signals_dropped_24h=self._counters["signals_dropped"],
            detail={
                "tier_hits": dict(self._counters["tier_hits"]),
                # L-248: the resolved active tier set the handler is
                # actually running (post the AND of config.tiers + per-
                # tier enabled flags + port availability for tier 3).
                "tiers_configured": list(self._config.tiers),
                "tiers_active": sorted(self._active_tiers),
                "tier1_enabled": _TIER_URL in self._active_tiers,
                "tier2_enabled": _TIER_CONTENT in self._active_tiers,
                "tier3_enabled": _TIER_SEMANTIC in self._active_tiers,
                "tier4_enabled": _TIER_TEMPORAL in self._active_tiers,
            },
        )

    # ----------------------------------------------- lifecycle (defaults)

    async def on_configure(self, ctx: FilterContext) -> None:
        return None

    async def on_activate(self, ctx: FilterContext) -> None:
        return None

    async def on_pause(self, ctx: FilterContext) -> None:
        return None

    async def on_resume(self, ctx: FilterContext) -> None:
        return None

    async def on_retire(self, ctx: FilterContext) -> None:
        return None

    # ============================================================== TIER 1

    @staticmethod
    def canonical_url(url: str) -> str:
        """Canonicalize a URL for Tier 1 hashing.

        Rules:

          * Lowercase scheme + host.
          * Strip fragment (``#...``).
          * Sort query params lexicographically by name; preserve
            duplicate keys via stable order on (name, value).
          * Drop empty query params.
          * Strip default ports (``:80`` http, ``:443`` https).
          * Preserve path case (paths are case-sensitive per RFC 3986).

        Returns the canonical URL as a string. Idempotent — running it on
        an already-canonical URL is a no-op.
        """
        if not url:
            return ""
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            return url.strip()

        scheme = (parts.scheme or "").lower()
        netloc = (parts.hostname or "").lower()
        if parts.port is not None:
            default = (scheme == "http" and parts.port == 80) or (
                scheme == "https" and parts.port == 443
            )
            if not default:
                netloc = f"{netloc}:{parts.port}"
        if parts.username or parts.password:
            # Preserve userinfo only if present (rare for normal URLs).
            user = parts.username or ""
            pw = f":{parts.password}" if parts.password else ""
            netloc = f"{user}{pw}@{netloc}" if user or pw else netloc

        # Sort query: parse, drop empty-value pairs, sort by (key, value).
        query_pairs = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=False)
            if k
        ]
        query_pairs.sort(key=lambda kv: (kv[0], kv[1]))
        query = urlencode(query_pairs, doseq=False)

        return urlunsplit((scheme, netloc, parts.path or "", query, ""))

    def _canonical_url_hash(self, signal: Signal) -> str | None:
        url = (
            signal.canonical_url
            or _payload_str(signal, "url")
            or _payload_str(signal, "link")
            or _payload_str(signal, "source_url")
        )
        if not url:
            return None
        canon = self.canonical_url(url)
        if not canon:
            return None
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    async def _tier1_lookup(
        self, ctx: FilterContext, url_hash: str
    ) -> str | None:
        """Return matched external id if URL hash present, else None."""
        key = self._kv_key(ctx, "url", url_hash)
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return _to_str(raw) or ""

    # ============================================================== TIER 2

    @staticmethod
    def normalized_content(signal: Signal) -> str:
        """Normalize signal body text for Tier 2 hashing.

        Rules:

          * Concatenate ``payload.title`` + ``payload.summary`` +
            ``payload.body`` / ``payload.raw_body`` if present.
          * Strip HTML tags.
          * Collapse whitespace (multi-space, tabs, newlines) to single
            space.
          * Lowercase.
          * Strip leading/trailing whitespace.
        """
        chunks: list[str] = []
        for field in ("title", "summary", "body", "raw_body"):
            val = _payload_str(signal, field)
            if val:
                chunks.append(val)
        joined = " ".join(chunks)
        stripped = _STRIP_HTML_RE.sub(" ", joined)
        collapsed = _WS_RE.sub(" ", stripped).strip().lower()
        return collapsed

    def _normalized_content_hash(self, signal: Signal) -> str:
        text = self.normalized_content(signal)
        if not text:
            return ""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    async def _tier2_lookup(
        self, ctx: FilterContext, content_hash: str
    ) -> str | None:
        key = self._kv_key(ctx, "content", content_hash)
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return _to_str(raw) or ""

    # ============================================================== TIER 3

    async def _embed_for_signal(self, signal: Signal) -> list[float] | None:
        """Embed title + summary; returns None if there's nothing to embed."""
        title = _payload_str(signal, "title")
        summary = (
            _payload_str(signal, "summary")
            or _payload_str(signal, "body")
            or _payload_str(signal, "raw_body")
        )
        text = (title + "\n" + summary).strip()
        if not text:
            return None
        assert self._embedder is not None
        vec = await self._embedder.embed(text)
        if not vec:
            return None
        return list(vec)

    def _tier3_collection_name(self, ctx: FilterContext) -> str:
        return (
            f"{self._config.tier3.collection_prefix}__"
            f"{_safe_name(ctx.target_id)}"
        )

    async def _ensure_tier3_collection(self, ctx: FilterContext) -> str:
        name = self._tier3_collection_name(ctx)
        if name in self._ensured_collections:
            return name
        # Best-effort: list collections; create if absent. Real qdrant-
        # client exposes ``get_collections()`` returning an object with
        # ``.collections`` of items with ``.name``. We try / except
        # because the L-122 work owns canonical collection lifecycle;
        # this handler must not duplicate that logic when L-122 lands.
        try:
            from qdrant_client.http import models as qmodels

            existing = await self._qdrant.get_collections()  # type: ignore[union-attr]
            names: set[str] = {
                c.name for c in getattr(existing, "collections", []) or []
            }
            if name not in names:
                await self._qdrant.create_collection(  # type: ignore[union-attr]
                    collection_name=name,
                    vectors_config=qmodels.VectorParams(
                        size=self._config.tier3.embedding_dim,
                        distance=qmodels.Distance.COSINE,
                    ),
                )
        except Exception as exc:                            # pragma: no cover
            ctx.logger.warning(
                "dedupe.tier3.ensure_collection.failed name=%s err=%s",
                name, exc,
            )
        self._ensured_collections.add(name)
        return name

    async def _tier3_lookup(
        self, ctx: FilterContext, vec: list[float]
    ) -> str | None:
        """Cosine search; return the matched external id if score >= threshold.

        Uses ``query_points`` (qdrant-client 1.10+ unified API). The
        response is an object with a ``.points`` attribute of scored
        points; we fall back to treating ``hits`` as a plain list when the
        underlying client (e.g. a process-local fake) returns one.
        """
        name = await self._ensure_tier3_collection(ctx)
        try:
            resp = await self._qdrant.query_points(  # type: ignore[union-attr]
                collection_name=name,
                query=vec,
                limit=1,
                score_threshold=self._config.tier3.threshold,
                with_payload=True,
            )
        except Exception as exc:                            # pragma: no cover
            ctx.logger.warning(
                "dedupe.tier3.query.failed name=%s err=%s", name, exc,
            )
            return None
        hits = getattr(resp, "points", None)
        if hits is None and isinstance(resp, list):
            hits = resp
        if not hits:
            return None
        first = hits[0]
        payload = getattr(first, "payload", None) or {}
        ext_id = payload.get("external_id") or payload.get("signal_id")
        if not ext_id:
            return None
        return str(ext_id)

    async def _tier3_insert(
        self,
        ctx: FilterContext,
        vec: list[float],
        signal: Signal,
        ext_id: str,
    ) -> None:
        name = await self._ensure_tier3_collection(ctx)
        try:
            from qdrant_client.http import models as qmodels

            await self._qdrant.upsert(  # type: ignore[union-attr]
                collection_name=name,
                points=[
                    qmodels.PointStruct(
                        id=str(signal.signal_id),
                        vector=vec,
                        payload={
                            "external_id": ext_id,
                            "source_id": signal.source_id,
                            "target_id": ctx.target_id,
                            "fetched_at": signal.fetched_at.isoformat(),
                        },
                    )
                ],
            )
        except Exception as exc:                            # pragma: no cover
            ctx.logger.warning(
                "dedupe.tier3.upsert.failed name=%s err=%s", name, exc,
            )

    # ============================================================== TIER 4

    @staticmethod
    def normalized_title(text: str) -> str:
        """Normalize a title for Tier 4 Levenshtein comparison."""
        if not text:
            return ""
        text = _STRIP_HTML_RE.sub(" ", text)
        text = _WS_RE.sub(" ", text).strip().lower()
        return text

    async def _tier4_lookup(
        self, ctx: FilterContext, signal: Signal
    ) -> str | None:
        title = self.normalized_title(_payload_str(signal, "title"))
        if not title:
            return None
        source_id = signal.source_id
        if not source_id:
            return None

        key = self._zset_key(ctx, "temporal", source_id)
        now = self._clock()
        cutoff = now - self._config.tier4.window_hours * 3600.0

        # Drop expired entries before scanning.
        await self._redis.zremrangebyscore(key, 0, cutoff)
        # Pull the in-window cache.
        entries = await self._redis.zrangebyscore(
            key, cutoff, now + 1.0, withscores=False,
        )

        for raw in entries or []:
            decoded = _to_str(raw)
            if not decoded:
                continue
            # Stored as "<external_id>\x1f<normalized_title>".
            sep = decoded.find("\x1f")
            if sep < 0:
                continue
            cached_ext = decoded[:sep]
            cached_title = decoded[sep + 1 :]
            if not cached_title:
                continue
            dist = _normalized_levenshtein(title, cached_title)
            if dist < self._config.tier4.distance_threshold:
                # Match. Bump cached entry's score so it stays fresh.
                return cached_ext
        return None

    async def _tier4_insert(
        self,
        ctx: FilterContext,
        signal: Signal,
        ext_id: str,
    ) -> None:
        title = self.normalized_title(_payload_str(signal, "title"))
        if not title or not signal.source_id:
            return
        key = self._zset_key(ctx, "temporal", signal.source_id)
        member = f"{ext_id}\x1f{title}"
        now = self._clock()
        await self._redis.zadd(key, {member: now})
        # Trim out-of-window entries opportunistically.
        cutoff = now - self._config.tier4.window_hours * 3600.0
        await self._redis.zremrangebyscore(key, 0, cutoff)

    # ============================================================== insert

    async def _insert_unique(
        self,
        ctx: FilterContext,
        signal: Signal,
        ext_id: str,
        vec: list[float] | None,
    ) -> None:
        """Insert the (non-duplicate) signal into all active tiers."""
        if _TIER_URL in self._active_tiers:
            url_hash = self._canonical_url_hash(signal)
            if url_hash:
                key = self._kv_key(ctx, "url", url_hash)
                await self._redis.set(
                    key, ext_id, ex=self._config.set_ttl_seconds,
                )

        if _TIER_CONTENT in self._active_tiers:
            content_hash = self._normalized_content_hash(signal)
            if content_hash:
                key = self._kv_key(ctx, "content", content_hash)
                await self._redis.set(
                    key, ext_id, ex=self._config.set_ttl_seconds,
                )

        if _TIER_SEMANTIC in self._active_tiers:
            # Reuse the embedding we already computed during lookup; if
            # the embed failed (empty text), skip insert.
            if vec is None:
                vec = await self._embed_for_signal(signal)
            if vec is not None:
                await self._tier3_insert(ctx, vec, signal, ext_id)

        if _TIER_TEMPORAL in self._active_tiers:
            await self._tier4_insert(ctx, signal, ext_id)

    # ============================================================ helpers

    def _kv_key(
        self, ctx: FilterContext, tier_name: str, member_hash: str
    ) -> str:
        return (
            f"{self._config.redis_key_prefix}:"
            f"{_safe_name(ctx.target_id)}:{tier_name}:{member_hash}"
        )

    def _zset_key(
        self, ctx: FilterContext, tier_name: str, source_id: str
    ) -> str:
        return (
            f"{self._config.redis_key_prefix}:"
            f"{_safe_name(ctx.target_id)}:{tier_name}:"
            f"{_safe_name(source_id)}:zset"
        )

    def _mark(self, signal: Signal, ext_id: str, tier: int) -> Signal:
        new_payload = dict(signal.payload)
        new_payload["duplicate_of"] = ext_id or ""
        new_payload["dedupe_tier"] = tier
        self._counters["tier_hits"][tier] += 1
        self._counters["signals_out"] += 1
        return signal.model_copy(update={"payload": new_payload})


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


_STRIP_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\-]")


def _safe_name(name: str) -> str:
    """Sanitize a name for use in Redis / Qdrant keys."""
    if not name:
        return "unknown"
    return _SAFE_NAME_RE.sub("_", name)


def _payload_str(signal: Signal, key: str) -> str:
    val = signal.payload.get(key)
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", "replace")
    return str(val)


def _external_id(signal: Signal) -> str:
    """Recover a stable external id from a signal payload.

    Order of preference:
      1. ``payload.external_id``
      2. ``payload.guid``
      3. ``payload.id``
      4. ``payload.link`` / ``payload.url``
      5. ``signal.signal_id`` (fallback)
    """
    for key in ("external_id", "guid", "id", "link", "url"):
        val = _payload_str(signal, key)
        if val:
            return val
    return str(signal.signal_id)


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:                          # pragma: no cover
            return value.decode("utf-8", "replace")
    return str(value)


def _normalized_levenshtein(a: str, b: str) -> float:
    """Return Levenshtein distance / max(len(a), len(b)).

    Pure Python implementation. ``rapidfuzz`` / ``python-Levenshtein``
    aren't pinned in pyproject; for the ~100-char titles this filter
    sees, the naive O(n*m) DP is < 50us in CPython 3.11+. If profiling
    shows it as a hotspot at scale, swap for ``rapidfuzz`` behind a
    feature flag.
    """
    if a == b:
        return 0.0
    if not a or not b:
        return 1.0
    n, m = len(a), len(b)
    if n < m:
        a, b = b, a
        n, m = m, n
    previous = list(range(m + 1))
    for i in range(1, n + 1):
        current = [i] + [0] * m
        ca = a[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ca == b[j - 1] else 1
            current[j] = min(
                current[j - 1] + 1,      # insertion
                previous[j] + 1,         # deletion
                previous[j - 1] + cost,  # substitution
            )
        previous = current
    distance = previous[m]
    return distance / float(n)


__all__ = [
    "Dedupe4TierConfig",
    "Dedupe4TierHandler",
    "EmbeddingService",
    "QdrantLike",
    "RedisLike",
    "Tier3Config",
    "Tier4Config",
    "TierToggle",
]
