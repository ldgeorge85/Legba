# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.config — env-driven configuration for substrate stores.

Per `design/legba_topology_redesign.md` §2.5, the only thing in env vars is
bootstrap state: registry connection string, master encryption key, default
stack IDs. Everything else lives in the stack registry and is hot-reloaded.

The substrate stores still need *bootstrap* env vars to connect — once the
stack registry is online (L-111) substrate components register themselves
there and consumers resolve by ID rather than reading env vars directly.

This module is the single source of truth for those bootstrap env vars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover — dotenv is optional for testing
    def load_dotenv(*_args, **_kwargs) -> bool:  # type: ignore[misc]
        return False


def _load_env() -> None:
    """Load .env from the repo root if present."""
    candidates = [
        Path(".env"),
        Path(__file__).resolve().parents[4] / ".env",
        Path("/usr/local/deployments/active/legba/.env"),
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                load_dotenv(candidate)
                return
        except Exception:
            continue


_load_env()


# ---------------------------------------------------------------------------
# Substrate auth helpers (B-1 — stop publishing the substrate)
# ---------------------------------------------------------------------------
#
# Both helpers are default_factory functions so DIRECT dataclass construction
# (``NatsConfig(url=...)`` in tests/stack handlers) picks up the credential
# from the environment exactly like ``from_env()`` does. Empty/unset means
# "no auth" — the pre-cutover substrate keeps working unauthenticated.


def _env_nats_token() -> str | None:
    """Token for NATS ``--auth`` token authorization (LEGBA_NATS_TOKEN)."""
    return os.getenv("LEGBA_NATS_TOKEN") or None


def _env_redis_password() -> str | None:
    """Redis ``requirepass`` password. LEGBA_DATA_REDIS_PASSWORD stays the
    most-specific override; LEGBA_REDIS_PASSWORD is the canonical cutover
    key (mirrors the compose-side ``--requirepass`` wiring)."""
    return (
        os.getenv("LEGBA_DATA_REDIS_PASSWORD")
        or os.getenv("LEGBA_REDIS_PASSWORD")
        or os.getenv("REDIS_PASSWORD")
        or None
    )


# ---------------------------------------------------------------------------
# Per-store dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostgresConfig:
    """Primary Postgres + AGE cluster (`pg.cluster_main`)."""

    host: str = "localhost"
    port: int = 5432
    user: str = "legba"
    password: str = "legba"
    database: str = "legba"
    pool_min: int = 1
    pool_max: int = 10

    @classmethod
    def from_env(cls) -> "PostgresConfig":
        return cls(
            host=os.getenv("LEGBA_DATA_PG_HOST", os.getenv("POSTGRES_HOST", "localhost")),
            port=int(os.getenv("LEGBA_DATA_PG_PORT", os.getenv("POSTGRES_PORT", "5432"))),
            user=os.getenv("LEGBA_DATA_PG_USER", os.getenv("POSTGRES_USER", "legba")),
            password=os.getenv("LEGBA_DATA_PG_PASSWORD", os.getenv("POSTGRES_PASSWORD", "legba")),
            database=os.getenv("LEGBA_DATA_PG_DB", os.getenv("POSTGRES_DB", "legba")),
            pool_min=int(os.getenv("LEGBA_DATA_PG_POOL_MIN", "2")),
            # Must scale with concurrent demand. The source-first runtime peaks at
            # boot — every SourceActor provisions at once (one per live source: 50+
            # with the S-1 catalog), alongside actor activations + the reconcile
            # loop. A max of 10 was silently catastrophic: the pool saturated, so
            # reconcile pool.acquire() blocked indefinitely, the reconcile plane
            # stalled, and newly-registered analysts never went live (+ durability
            # re-asserts stopped). 50 fits postgres' default max_connections=100
            # alongside the registry pool; very-high-source deployments
            # should raise both LEGBA_DATA_PG_POOL_MAX and postgres max_connections.
            pool_max=int(os.getenv("LEGBA_DATA_PG_POOL_MAX", "50")),
        )

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


@dataclass(frozen=True)
class QdrantConfig:
    """Qdrant vector store. Only the `legba_signals` collection survives.

    Per `design/legba_storage_layout.md` §3.4 / `design/legba_data_mapping.md`,
    the three dormant collections (`legba_short_term`, `legba_long_term`,
    `legba_facts`) are retired.
    """

    host: str = "localhost"
    port: int = 6333
    grpc_port: int = 6334
    api_key: str | None = None
    https: bool = False
    # Single canonical collection; BGE-M3 1024-dim cosine.
    signals_collection: str = "legba_signals"
    signals_dim: int = 1024
    embedding_model: str = "BAAI/bge-m3"
    # Manual-ingest RAG corpus collections (Lane-4, S5-T2). Same dim/distance
    # as `legba_signals` so the one embedder (bge-m3) serves all collections.
    # Named to match the descriptor grounding source token `vector:world_context`
    # (schema `GroundingBlock.sources`) and the docs.jsonl `corpus` field, so a
    # future resolver maps `vector:<corpus>` → collection by name with no table.
    world_context_collection: str = "world_context"
    tradecraft_collection: str = "tradecraft"

    @classmethod
    def from_env(cls) -> "QdrantConfig":
        return cls(
            host=os.getenv("LEGBA_DATA_QDRANT_HOST", os.getenv("QDRANT_HOST", "localhost")),
            port=int(os.getenv("LEGBA_DATA_QDRANT_PORT", os.getenv("QDRANT_PORT", "6333"))),
            grpc_port=int(os.getenv("LEGBA_DATA_QDRANT_GRPC_PORT", "6334")),
            api_key=os.getenv("LEGBA_DATA_QDRANT_API_KEY") or None,
            https=os.getenv("LEGBA_DATA_QDRANT_HTTPS", "false").lower() == "true",
            signals_collection=os.getenv("LEGBA_DATA_QDRANT_SIGNALS", "legba_signals"),
            signals_dim=int(os.getenv("LEGBA_DATA_EMBED_DIM", "1024")),
            embedding_model=os.getenv("LEGBA_DATA_EMBED_MODEL", "BAAI/bge-m3"),
            world_context_collection=os.getenv(
                "LEGBA_DATA_QDRANT_WORLD_CONTEXT", "world_context"
            ),
            tradecraft_collection=os.getenv(
                "LEGBA_DATA_QDRANT_TRADECRAFT", "tradecraft"
            ),
        )


@dataclass(frozen=True)
class OpenSearchConfig:
    """OpenSearch full-text corpus (the INDEX PLANE of the signal pool).

    Single-node, internal-only, no-auth — a lexical MINING substrate over the raw
    signal bodies (BM25 keyword search + keyword/date facets), complementing the
    structured Postgres `signals` and the Qdrant vector RAG corpus. Mirrors
    :class:`QdrantConfig`'s bare-fallback convention (LEGBA_DATA_*  ->  the bare
    OPENSEARCH_* form). ``host`` defaults to the compose service name.
    """

    host: str = "opensearch"
    port: int = 9200
    use_ssl: bool = False
    verify_certs: bool = False
    # Single canonical corpus index (the deterministic corpus_indexer sweep + the
    # backfill both write here; the future substrate_read search tools read here).
    index: str = "legba_signals_corpus"

    @classmethod
    def from_env(cls) -> "OpenSearchConfig":
        return cls(
            host=os.getenv(
                "LEGBA_DATA_OPENSEARCH_HOST", os.getenv("OPENSEARCH_HOST", "opensearch")
            ),
            port=int(
                os.getenv(
                    "LEGBA_DATA_OPENSEARCH_PORT", os.getenv("OPENSEARCH_PORT", "9200")
                )
            ),
            use_ssl=os.getenv("LEGBA_DATA_OPENSEARCH_SSL", "false").lower() == "true",
            verify_certs=os.getenv(
                "LEGBA_DATA_OPENSEARCH_VERIFY_CERTS", "false"
            ).lower()
            == "true",
            index=os.getenv("LEGBA_DATA_OPENSEARCH_INDEX", "legba_signals_corpus"),
        )


@dataclass(frozen=True)
class RedisConfig:
    """Redis hot-state buffer (`kv.redis.cluster_main`)."""

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = field(default_factory=_env_redis_password)
    maxmemory_policy: str = "allkeys-lru"
    # TTL applied to embed-cache keys; addresses L-091 §2.6 finding.
    embed_cache_ttl_seconds: int = 86400

    @classmethod
    def from_env(cls) -> "RedisConfig":
        return cls(
            host=os.getenv("LEGBA_DATA_REDIS_HOST", os.getenv("REDIS_HOST", "localhost")),
            port=int(os.getenv("LEGBA_DATA_REDIS_PORT", os.getenv("REDIS_PORT", "6379"))),
            db=int(os.getenv("LEGBA_DATA_REDIS_DB", os.getenv("REDIS_DB", "0"))),
            password=_env_redis_password(),
            maxmemory_policy=os.getenv("LEGBA_DATA_REDIS_MAXMEMORY_POLICY", "allkeys-lru"),
            embed_cache_ttl_seconds=int(os.getenv("LEGBA_DATA_REDIS_EMBED_TTL", "86400")),
        )

    @property
    def url(self) -> str:
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


@dataclass(frozen=True)
class NatsConfig:
    """NATS JetStream cluster (`bus.nats.cluster_main`)."""

    url: str = "nats://localhost:4222"
    connect_timeout: int = 10
    user: str | None = None
    password: str | None = None
    creds_file: str | None = None
    # B-1: server-wide token authorization (`nats-server --auth`). Sourced
    # from LEGBA_NATS_TOKEN; None/empty connects unauthenticated.
    token: str | None = field(default_factory=_env_nats_token)

    @classmethod
    def from_env(cls) -> "NatsConfig":
        return cls(
            url=os.getenv("LEGBA_DATA_NATS_URL", os.getenv("NATS_URL", "nats://localhost:4222")),
            connect_timeout=int(os.getenv("LEGBA_DATA_NATS_TIMEOUT", "10")),
            user=os.getenv("LEGBA_DATA_NATS_USER"),
            password=os.getenv("LEGBA_DATA_NATS_PASSWORD"),
            creds_file=os.getenv("LEGBA_DATA_NATS_CREDS"),
            token=_env_nats_token(),
        )


@dataclass(frozen=True)
class RegistryBootstrap:
    """Bootstrap config for the descriptor + stack registries.

    Per topology-redesign §2.5: this is the *only* set of env vars that
    survives long-term. Everything else gets registered into the stack
    registry once it lands (L-111).
    """

    # Where the descriptor + stack registry tables live (typically the primary PG).
    registry_pg_dsn: str = ""
    # Master encryption key for secrets stored in the registry vault.
    master_key: str = ""
    # Default stack-component IDs by family. Empty means "no default; resolve via descriptor".
    default_llm_provider: str = ""
    default_vector_store: str = "vector.qdrant.cluster_main"
    default_embedding_service: str = ""
    default_postgres_cluster: str = "pg.cluster_main"
    default_redis_cluster: str = "kv.redis.cluster_main"
    default_nats_cluster: str = "bus.nats.cluster_main"

    @classmethod
    def from_env(cls) -> "RegistryBootstrap":
        pg = PostgresConfig.from_env()
        return cls(
            registry_pg_dsn=os.getenv("LEGBA_DATA_REGISTRY_DSN", pg.dsn),
            master_key=os.getenv("LEGBA_DATA_MASTER_KEY", ""),
            default_llm_provider=os.getenv("LEGBA_DATA_DEFAULT_LLM", ""),
            default_vector_store=os.getenv("LEGBA_DATA_DEFAULT_VECTOR", "vector.qdrant.cluster_main"),
            default_embedding_service=os.getenv("LEGBA_DATA_DEFAULT_EMBEDDING", ""),
            default_postgres_cluster=os.getenv("LEGBA_DATA_DEFAULT_PG", "pg.cluster_main"),
            default_redis_cluster=os.getenv("LEGBA_DATA_DEFAULT_REDIS", "kv.redis.cluster_main"),
            default_nats_cluster=os.getenv("LEGBA_DATA_DEFAULT_NATS", "bus.nats.cluster_main"),
        )


@dataclass(frozen=True)
class DataConfig:
    """Aggregate config — pull once at process start."""

    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    qdrant: QdrantConfig = field(default_factory=QdrantConfig)
    opensearch: OpenSearchConfig = field(default_factory=OpenSearchConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    nats: NatsConfig = field(default_factory=NatsConfig)
    registry: RegistryBootstrap = field(default_factory=RegistryBootstrap)

    @classmethod
    def from_env(cls) -> "DataConfig":
        return cls(
            postgres=PostgresConfig.from_env(),
            qdrant=QdrantConfig.from_env(),
            opensearch=OpenSearchConfig.from_env(),
            redis=RedisConfig.from_env(),
            nats=NatsConfig.from_env(),
            registry=RegistryBootstrap.from_env(),
        )
