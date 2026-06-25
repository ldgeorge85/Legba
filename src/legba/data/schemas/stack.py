# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stack-component descriptor schemas (per L-101 §5).

One model per substrate component family. Discriminated by `schema_uri`.
Credentials referenced via `Property.Secret`; never embedded.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .lifecycle import LifecycleState
from .properties import (
    DropdownStatic,
    Number,
    Property,
    Secret,
    Text,
    TypedList,
)


class StackComponentBase(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_.]*$", max_length=128)
    name: str
    schema_uri: str
    version: str = Field(pattern=r"^[a-f0-9]{16,64}$")
    state: LifecycleState = LifecycleState.DRAFT
    owner: str


# ---------------------------------------------------------------------------
# LLM provider
# ---------------------------------------------------------------------------


class LLMProviderConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    api_endpoint: Text
    api_key: Secret
    model_name: Text
    max_tokens: Number
    timeout_seconds: Number = Field(
        default_factory=lambda: Property.Number.of(60, minimum=1, maximum=600)
    )
    tier: DropdownStatic = Field(
        default_factory=lambda: Property.Dropdown.Static.of(
            "primary", ["primary", "fallback", "cheap"]
        )
    )


class LLMProvider(StackComponentBase):
    schema_uri: str = Field(pattern=r"^legba/stack/llm_provider/\d+\.\d+\.\d+$")
    config: LLMProviderConfig


# ---------------------------------------------------------------------------
# Vector store
# ---------------------------------------------------------------------------


class VectorStoreConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    endpoint: Text
    api_key: Secret | None = None
    collection_prefix: Text
    default_dim: Number = Field(
        default_factory=lambda: Property.Number.of(1024, minimum=64, maximum=8192)
    )
    default_metric: DropdownStatic = Field(
        default_factory=lambda: Property.Dropdown.Static.of(
            "cosine", ["cosine", "dot", "euclid"]
        )
    )


class VectorStore(StackComponentBase):
    schema_uri: str = Field(pattern=r"^legba/stack/vector_store/\d+\.\d+\.\d+$")
    config: VectorStoreConfig


# ---------------------------------------------------------------------------
# Embedding service — BAAI/bge-m3 default
# ---------------------------------------------------------------------------


class EmbeddingServiceConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    endpoint: Text
    api_key: Secret | None = None
    model_name: Text = Field(
        default_factory=lambda: Property.Text.of("BAAI/bge-m3")
    )
    dim: Number = Field(
        default_factory=lambda: Property.Number.of(1024, minimum=64, maximum=8192)
    )
    normalize: DropdownStatic = Field(
        default_factory=lambda: Property.Dropdown.Static.of(
            "true", ["true", "false"]
        )
    )
    batch_size: Number = Field(
        default_factory=lambda: Property.Number.of(64, minimum=1, maximum=1024)
    )


class EmbeddingService(StackComponentBase):
    schema_uri: str = Field(pattern=r"^legba/stack/embedding/\d+\.\d+\.\d+$")
    config: EmbeddingServiceConfig


# ---------------------------------------------------------------------------
# NLP service — Legba-models GPU service (translate / classify / extract / summarize)
#
# Phase-4 architectural correction (2026-05-22): the filter handlers
# (``ner_multilingual``, ``classify``, optional ``translate``) call the
# hosted Legba-models FastAPI service over HTTP rather than loading the
# underlying transformer models in-process. The endpoint catalog lives in
# ``docs/AI_MODELS.md`` and the per-endpoint payload shapes in
# ``legba-models/USAGE.md``. The service is at
# ``https://nlp.example.internal`` (HTTPS + Basic Auth) and
# internally as ``http://legba-models:8700`` on the ``fastchat`` docker net.
# ---------------------------------------------------------------------------


class NLPServiceConfig(BaseModel):
    """Hosted NLP inference service config (Legba-models).

    Endpoints expected: ``GET /health``, ``POST /translate``, ``POST /classify``,
    ``POST /extract``, ``POST /summarize``. Auth is HTTP Basic when the
    service is fronted by Caddy (production HTTPS path); the internal docker
    path has no auth — credentials are still resolved but the client treats
    a missing secret value as anonymous access.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    endpoint: Text
    api_user: Secret | None = None
    api_pass: Secret | None = None
    timeout_seconds: Number = Field(
        default_factory=lambda: Property.Number.of(60, minimum=1, maximum=600)
    )
    # Per-endpoint path overrides — defaults match the legba-models contract.
    translate_path: Text = Field(
        default_factory=lambda: Property.Text.of("/translate")
    )
    classify_path: Text = Field(
        default_factory=lambda: Property.Text.of("/classify")
    )
    extract_path: Text = Field(
        default_factory=lambda: Property.Text.of("/extract")
    )
    summarize_path: Text = Field(
        default_factory=lambda: Property.Text.of("/summarize")
    )
    health_path: Text = Field(
        default_factory=lambda: Property.Text.of("/health")
    )


class NLPService(StackComponentBase):
    schema_uri: str = Field(pattern=r"^legba/stack/nlp_service/\d+\.\d+\.\d+$")
    config: NLPServiceConfig


# ---------------------------------------------------------------------------
# NATS
# ---------------------------------------------------------------------------


class NATSClusterConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    servers: TypedList = Field(
        default_factory=lambda: Property.List(raw=[], item_kind="text")
    )
    credentials: Secret | None = None
    jetstream: DropdownStatic = Field(
        default_factory=lambda: Property.Dropdown.Static.of(
            "enabled", ["enabled", "disabled"]
        )
    )


class NATSCluster(StackComponentBase):
    schema_uri: str = Field(pattern=r"^legba/stack/nats/\d+\.\d+\.\d+$")
    config: NATSClusterConfig


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


class PostgresClusterConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    host: Text
    port: Number = Field(
        default_factory=lambda: Property.Number.of(5432, minimum=1, maximum=65535)
    )
    database: Text
    user: Text
    password: Secret
    extensions: TypedList = Field(
        default_factory=lambda: Property.List(raw=["age"], item_kind="text")
    )
    pool_size: Number = Field(
        default_factory=lambda: Property.Number.of(10, minimum=1, maximum=200)
    )


class PostgresCluster(StackComponentBase):
    schema_uri: str = Field(pattern=r"^legba/stack/postgres/\d+\.\d+\.\d+$")
    config: PostgresClusterConfig


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


class RedisClusterConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    host: Text
    port: Number = Field(
        default_factory=lambda: Property.Number.of(6379, minimum=1, maximum=65535)
    )
    password: Secret | None = None
    maxmemory_policy: DropdownStatic = Field(
        default_factory=lambda: Property.Dropdown.Static.of(
            "allkeys-lru",
            ["noeviction", "allkeys-lru", "volatile-lru", "allkeys-random"],
        )
    )


class RedisCluster(StackComponentBase):
    schema_uri: str = Field(pattern=r"^legba/stack/redis/\d+\.\d+\.\d+$")
    config: RedisClusterConfig


# ---------------------------------------------------------------------------
# Proxy pool
# ---------------------------------------------------------------------------


class ProxyPoolConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    provider: DropdownStatic = Field(
        default_factory=lambda: Property.Dropdown.Static.of(
            "none", ["none", "bright_data", "oxylabs", "self_managed"]
        )
    )
    endpoint: Text | None = None
    credentials: Secret | None = None
    geo_targeting: TypedList = Field(
        default_factory=lambda: Property.List(raw=[], item_kind="text")
    )
    rotation: DropdownStatic = Field(
        default_factory=lambda: Property.Dropdown.Static.of(
            "session", ["session", "request", "sticky_30s", "sticky_5m"]
        )
    )


class ProxyPool(StackComponentBase):
    schema_uri: str = Field(pattern=r"^legba/stack/proxy_pool/\d+\.\d+\.\d+$")
    config: ProxyPoolConfig
