"""Subconscious service configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

from legba.shared.config import (
    NatsConfig,
    PostgresConfig,
    RedisConfig,
)


@dataclass(frozen=True)
class SubconsciousConfig:
    """Subconscious service tuning knobs.

    Intervals are expressed in ticks. One tick = check_interval seconds.
    For example, with check_interval=60 and signal_validation_interval=15,
    signal validation runs every 15 * 60 = 900 seconds (15 minutes).
    """

    # Core timing
    check_interval: int = 60               # Seconds between scheduler ticks
    health_port: int = 8800                # Health/metrics HTTP port
    log_level: str = "INFO"

    # Task intervals (in ticks)
    signal_validation_interval: int = 15   # 15 min at 60s ticks
    entity_resolution_interval: int = 30   # 30 min
    classification_interval: int = 30      # 30 min
    fact_refresh_interval: int = 60        # 60 min
    graph_consistency_interval: int = 1440 # Daily (24h * 60min / 1min tick)
    source_reliability_interval: int = 1440  # Daily

    # Uncertainty thresholds for signal validation
    uncertainty_low: float = 0.3           # Signals below this are rejected
    uncertainty_high: float = 0.7          # Signals above this are accepted

    # Batch sizes
    signal_batch_size: int = 10            # Max signals per validation batch
    entity_batch_size: int = 10            # Max entities per resolution batch
    classification_batch_size: int = 10    # Max signals per classification batch

    # SLM provider config
    llm_provider: str = "vllm"             # "vllm" or "anthropic"
    llm_base_url: str = ""                 # vLLM endpoint base URL
    llm_api_key: str = ""                  # API key for the SLM
    llm_model: str = "meta-llama/Llama-3.1-8B-Instruct"
    max_tokens: int = 2048                 # Max output tokens per SLM call
    llm_timeout: int = 60                  # SLM request timeout (seconds)
    llm_temperature: float = 0.1           # Low temperature for deterministic validation

    # Backing store configs
    postgres: PostgresConfig = PostgresConfig()
    redis: RedisConfig = RedisConfig()
    nats: NatsConfig = NatsConfig()

    @classmethod
    def from_env(cls) -> SubconsciousConfig:
        return cls(
            check_interval=int(os.getenv("SUBCONSCIOUS_CHECK_INTERVAL", "60")),
            health_port=int(os.getenv("SUBCONSCIOUS_HEALTH_PORT", "8800")),
            log_level=os.getenv("SUBCONSCIOUS_LOG_LEVEL", "INFO"),
            signal_validation_interval=int(os.getenv("SUBCONSCIOUS_SIGNAL_VALIDATION_INTERVAL", "15")),
            entity_resolution_interval=int(os.getenv("SUBCONSCIOUS_ENTITY_RESOLUTION_INTERVAL", "30")),
            classification_interval=int(os.getenv("SUBCONSCIOUS_CLASSIFICATION_INTERVAL", "30")),
            fact_refresh_interval=int(os.getenv("SUBCONSCIOUS_FACT_REFRESH_INTERVAL", "60")),
            graph_consistency_interval=int(os.getenv("SUBCONSCIOUS_GRAPH_CONSISTENCY_INTERVAL", "1440")),
            source_reliability_interval=int(os.getenv("SUBCONSCIOUS_SOURCE_RELIABILITY_INTERVAL", "1440")),
            uncertainty_low=float(os.getenv("SUBCONSCIOUS_UNCERTAINTY_LOW", "0.3")),
            uncertainty_high=float(os.getenv("SUBCONSCIOUS_UNCERTAINTY_HIGH", "0.7")),
            signal_batch_size=int(os.getenv("SUBCONSCIOUS_SIGNAL_BATCH_SIZE", "10")),
            entity_batch_size=int(os.getenv("SUBCONSCIOUS_ENTITY_BATCH_SIZE", "10")),
            classification_batch_size=int(os.getenv("SUBCONSCIOUS_CLASSIFICATION_BATCH_SIZE", "10")),
            llm_provider=os.getenv("SUBCONSCIOUS_LLM_PROVIDER", "vllm"),
            llm_base_url=os.getenv("SUBCONSCIOUS_LLM_BASE_URL", os.getenv("SLM_BASE_URL", "")),
            llm_api_key=os.getenv("SUBCONSCIOUS_LLM_API_KEY", os.getenv("SLM_API_KEY", "")),
            llm_model=os.getenv("SUBCONSCIOUS_LLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct"),
            max_tokens=int(os.getenv("SUBCONSCIOUS_MAX_TOKENS", "2048")),
            llm_timeout=int(os.getenv("SUBCONSCIOUS_LLM_TIMEOUT", "60")),
            llm_temperature=float(os.getenv("SUBCONSCIOUS_LLM_TEMPERATURE", "0.1")),
            postgres=PostgresConfig.from_env(),
            redis=RedisConfig.from_env(),
            nats=NatsConfig.from_env(),
        )
