"""
Legba Configuration

All configuration loaded from environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _load_env() -> None:
    """Load .env file if it exists."""
    for candidate in [Path(".env"), Path(__file__).parents[3] / ".env"]:
        if candidate.exists():
            load_dotenv(candidate)
            return


_load_env()


@dataclass(frozen=True)
class LLMConfig:
    """LLM connection and generation parameters."""

    provider: str = "vllm"  # "vllm" or "anthropic"
    api_base: str = ""
    api_key: str = ""
    model: str = "InnoGPT-1"
    max_tokens: int = 16384
    temperature: float = 1.0
    top_p: float = 0.9
    timeout: int = 180
    max_context_tokens: int = 128000  # Model context window size

    # Embedding (separate from LLM — Anthropic has no embedding API)
    embedding_api_base: str = ""  # Falls back to api_base if empty
    embedding_api_key: str = ""   # Falls back to api_key if empty
    embedding_model: str = "embedding-inno1"
    embedding_dimensions: int = 1024

    @classmethod
    def from_env(cls) -> LLMConfig:
        return cls(
            provider=os.getenv("LLM_PROVIDER", "vllm"),
            api_base=os.getenv("OPENAI_BASE_URL", "http://localhost:8000/v1"),
            api_key=os.getenv("OPENAI_API_KEY", ""),
            model=os.getenv("OPENAI_MODEL", "InnoGPT-1"),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
            temperature=float(os.getenv("LLM_TEMPERATURE", "1.0")),
            top_p=float(os.getenv("LLM_TOP_P", "0.9")),
            timeout=int(os.getenv("LLM_TIMEOUT", "180")),
            max_context_tokens=int(os.getenv("LLM_MAX_CONTEXT_TOKENS", "128000")),
            embedding_api_base=os.getenv("EMBEDDING_API_BASE", ""),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY", ""),
            embedding_model=os.getenv("MEMORY_EMBEDDING_MODEL", "embedding-inno1"),
            embedding_dimensions=int(os.getenv("MEMORY_VECTOR_DIMENSIONS", "1024")),
        )

    @classmethod
    def consult_from_env(cls) -> LLMConfig:
        """Build LLM config for the consultation engine.

        Reads CONSULT_* env vars, falling back to the main LLM config.
        """
        base = cls.from_env()
        return cls(
            provider=os.getenv("CONSULT_LLM_PROVIDER", base.provider),
            api_base=os.getenv("CONSULT_API_BASE", base.api_base),
            api_key=os.getenv("CONSULT_API_KEY", base.api_key),
            model=os.getenv("CONSULT_MODEL", base.model),
            max_tokens=int(os.getenv("CONSULT_MAX_TOKENS", str(base.max_tokens))),
            temperature=float(os.getenv("CONSULT_TEMPERATURE", str(base.temperature))),
            top_p=base.top_p,
            timeout=int(os.getenv("CONSULT_TIMEOUT", str(base.timeout))),
            max_context_tokens=base.max_context_tokens,
            embedding_api_base=base.embedding_api_base,
            embedding_api_key=base.embedding_api_key,
            embedding_model=base.embedding_model,
            embedding_dimensions=base.embedding_dimensions,
        )


    # Cycle types that should use an alternate LLM provider.
    # Set LLM_PROVIDER_MAP as JSON: {"ANALYSIS": "anthropic", "SYNTHESIZE": "anthropic"}
    # Unspecified types use the default provider.
    _PROVIDER_MAP_CACHE: dict[str, LLMConfig] | None = None

    @classmethod
    def for_cycle_type(cls, cycle_type: str) -> LLMConfig | None:
        """Return an alternate LLM config for a specific cycle type, or None to use default.

        Reads LLM_PROVIDER_MAP env var (JSON dict mapping cycle type to provider).
        For each mapped type, reads LLM_ALT_* env vars for the alternate provider config.
        Returns None if cycle type isn't mapped (use default).
        """
        import json as _json

        raw = os.getenv("LLM_PROVIDER_MAP", "")
        if not raw:
            return None

        try:
            provider_map = _json.loads(raw)
        except _json.JSONDecodeError:
            return None

        alt_provider = provider_map.get(cycle_type.upper())
        if not alt_provider:
            return None

        base = cls.from_env()
        return cls(
            provider=alt_provider,
            api_base=os.getenv("LLM_ALT_API_BASE", base.api_base),
            api_key=os.getenv("LLM_ALT_API_KEY", base.api_key),
            model=os.getenv("LLM_ALT_MODEL", base.model),
            max_tokens=int(os.getenv("LLM_ALT_MAX_TOKENS", str(base.max_tokens))),
            temperature=float(os.getenv("LLM_ALT_TEMPERATURE", "0.7")),
            top_p=base.top_p,
            timeout=int(os.getenv("LLM_ALT_TIMEOUT", str(base.timeout))),
            max_context_tokens=base.max_context_tokens,
            embedding_api_base=base.embedding_api_base,
            embedding_api_key=base.embedding_api_key,
            embedding_model=base.embedding_model,
            embedding_dimensions=base.embedding_dimensions,
        )


@dataclass(frozen=True)
class RedisConfig:
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None

    @classmethod
    def from_env(cls) -> RedisConfig:
        return cls(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            password=os.getenv("REDIS_PASSWORD"),
        )


@dataclass(frozen=True)
class PostgresConfig:
    host: str = "localhost"
    port: int = 5432
    user: str = "legba"
    password: str = "legba"
    database: str = "legba"

    @classmethod
    def from_env(cls) -> PostgresConfig:
        return cls(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.getenv("POSTGRES_USER", "legba"),
            password=os.getenv("POSTGRES_PASSWORD", "legba"),
            database=os.getenv("POSTGRES_DB", "legba"),
        )

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


@dataclass(frozen=True)
class QdrantConfig:
    host: str = "localhost"
    port: int = 6333

    @classmethod
    def from_env(cls) -> QdrantConfig:
        return cls(
            host=os.getenv("QDRANT_HOST", "localhost"),
            port=int(os.getenv("QDRANT_PORT", "6333")),
        )


@dataclass(frozen=True)
class NatsConfig:
    """NATS + JetStream connection parameters."""

    url: str = "nats://localhost:4222"
    connect_timeout: int = 10  # seconds

    @classmethod
    def from_env(cls) -> NatsConfig:
        return cls(
            url=os.getenv("NATS_URL", "nats://localhost:4222"),
            connect_timeout=int(os.getenv("NATS_CONNECT_TIMEOUT", "10")),
        )


@dataclass(frozen=True)
class OpenSearchConfig:
    """OpenSearch connection parameters."""

    host: str = "localhost"
    port: int = 9200
    scheme: str = "http"
    username: str | None = None
    password: str | None = None

    @classmethod
    def from_env(cls) -> OpenSearchConfig:
        return cls(
            host=os.getenv("OPENSEARCH_HOST", "localhost"),
            port=int(os.getenv("OPENSEARCH_PORT", "9200")),
            scheme=os.getenv("OPENSEARCH_SCHEME", "http"),
            username=os.getenv("OPENSEARCH_USERNAME"),
            password=os.getenv("OPENSEARCH_PASSWORD"),
        )

    @classmethod
    def from_audit_env(cls) -> OpenSearchConfig:
        """Read audit OpenSearch config from AUDIT_OPENSEARCH_* env vars."""
        return cls(
            host=os.getenv("AUDIT_OPENSEARCH_HOST", "localhost"),
            port=int(os.getenv("AUDIT_OPENSEARCH_PORT", "9200")),
            scheme=os.getenv("AUDIT_OPENSEARCH_SCHEME", "http"),
            username=os.getenv("AUDIT_OPENSEARCH_USERNAME"),
            password=os.getenv("AUDIT_OPENSEARCH_PASSWORD"),
        )

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class AirflowConfig:
    """Airflow orchestration engine connection parameters."""

    url: str = "http://localhost:8080"
    username: str = "airflow"
    password: str = "airflow"
    dags_path: str = "/airflow/dags"

    @classmethod
    def from_env(cls) -> AirflowConfig:
        return cls(
            url=os.getenv("AIRFLOW_URL", "http://localhost:8080"),
            username=os.getenv("AIRFLOW_ADMIN_USER", "airflow"),
            password=os.getenv("AIRFLOW_ADMIN_PASSWORD", "airflow"),
            dags_path=os.getenv("AIRFLOW_DAGS_PATH", "/airflow/dags"),
        )


@dataclass(frozen=True)
class PathConfig:
    """Filesystem paths for the agent's runtime environment."""

    seed_goal: str = "/seed_goal/goal.txt"
    workspace: str = "/workspace"
    agent_code: str = "/agent"
    agent_tools: str = "/agent/tools"
    shared: str = "/shared"
    logs: str = "/logs"

    @classmethod
    def from_env(cls) -> PathConfig:
        return cls(
            seed_goal=os.getenv("LEGBA_SEED_GOAL", "/seed_goal/goal.txt"),
            workspace=os.getenv("LEGBA_WORKSPACE", "/workspace"),
            agent_code=os.getenv("LEGBA_AGENT_CODE", "/agent"),
            agent_tools=os.getenv("LEGBA_AGENT_TOOLS", "/agent/tools"),
            shared=os.getenv("LEGBA_SHARED", "/shared"),
            logs=os.getenv("LEGBA_LOGS", "/logs"),
        )

    @property
    def inbox(self) -> str:
        return f"{self.shared}/inbox.json"

    @property
    def outbox(self) -> str:
        return f"{self.shared}/outbox.json"

    @property
    def challenge(self) -> str:
        return f"{self.shared}/challenge.json"

    @property
    def response(self) -> str:
        return f"{self.shared}/response.json"


@dataclass(frozen=True)
class AgentConfig:
    """Tuning knobs for the agent cycle."""

    # Reasoning loop
    max_reasoning_steps: int = 20       # Max tool call iterations per cycle
    max_subagent_steps: int = 10        # Default max steps for sub-agents

    # Tool defaults
    shell_timeout: int = 60             # Default shell exec timeout (seconds)
    http_timeout: int = 30              # Default HTTP request timeout (seconds)

    # Memory retrieval
    memory_retrieval_limit: int = 12    # Episodic episodes to retrieve per query
    facts_retrieval_limit: int = 20    # Facts to retrieve per query

    # Postgres pool
    pg_pool_min: int = 1
    pg_pool_max: int = 5

    # Bootstrap
    bootstrap_threshold: int = 5        # Cycles considered "early" (extra guidance)

    # Context budget
    max_context_tokens: int = 120000    # Budget for assembled prompts (128k window, leave room for output)

    # Mission review
    mission_review_interval: int = 15   # Strategic review every N cycles (0 = disabled)

    # Report relevance filtering
    report_primary_domains: frozenset[str] = frozenset({
        "conflict", "political", "economic", "disaster",
    })

    # Qdrant collection names
    qdrant_short_term: str = "legba_short_term"
    qdrant_long_term: str = "legba_long_term"
    qdrant_facts: str = "legba_facts"

    @classmethod
    def from_env(cls) -> AgentConfig:
        return cls(
            max_reasoning_steps=int(os.getenv("AGENT_MAX_REASONING_STEPS", "20")),
            max_subagent_steps=int(os.getenv("AGENT_MAX_SUBAGENT_STEPS", "10")),
            shell_timeout=int(os.getenv("AGENT_SHELL_TIMEOUT", "60")),
            http_timeout=int(os.getenv("AGENT_HTTP_TIMEOUT", "30")),
            memory_retrieval_limit=int(os.getenv("AGENT_MEMORY_RETRIEVAL_LIMIT", "12")),
            facts_retrieval_limit=int(os.getenv("AGENT_FACTS_RETRIEVAL_LIMIT", "20")),
            pg_pool_min=int(os.getenv("AGENT_PG_POOL_MIN", "1")),
            pg_pool_max=int(os.getenv("AGENT_PG_POOL_MAX", "5")),
            bootstrap_threshold=int(os.getenv("AGENT_BOOTSTRAP_THRESHOLD", "5")),
            max_context_tokens=int(os.getenv("AGENT_MAX_CONTEXT_TOKENS", "120000")),
            mission_review_interval=int(os.getenv("AGENT_MISSION_REVIEW_INTERVAL", "15")),
            report_primary_domains=frozenset(
                os.getenv("REPORT_PRIMARY_DOMAINS", "conflict,political,economic,disaster").split(",")
            ),
            qdrant_short_term=os.getenv("AGENT_QDRANT_SHORT_TERM", "legba_short_term"),
            qdrant_long_term=os.getenv("AGENT_QDRANT_LONG_TERM", "legba_long_term"),
            qdrant_facts=os.getenv("AGENT_QDRANT_FACTS", "legba_facts"),
        )


@dataclass(frozen=True)
class SupervisorConfig:
    """Tuning knobs for the supervisor."""

    max_consecutive_failures: int = 5   # Kill agent after this many heartbeat failures
    cycle_sleep: float = 2.0            # Seconds between cycles
    heartbeat_timeout: int = 360        # Seconds to wait for agent heartbeat
    max_extensions: int = 2             # Default max timeout extensions (pings)
    extension_map: dict[str, int] = field(default_factory=dict)  # Per-cycle-type overrides

    @classmethod
    def from_env(cls) -> SupervisorConfig:
        ext_map: dict[str, int] = {}
        raw = os.getenv("SUPERVISOR_EXTENSION_MAP", "")
        if raw:
            import json as _json
            try:
                ext_map = {k.upper(): int(v) for k, v in _json.loads(raw).items()}
            except (ValueError, AttributeError):
                pass
        return cls(
            max_consecutive_failures=int(os.getenv("SUPERVISOR_MAX_FAILURES", "5")),
            cycle_sleep=float(os.getenv("SUPERVISOR_CYCLE_SLEEP", "2.0")),
            heartbeat_timeout=int(os.getenv("SUPERVISOR_HEARTBEAT_TIMEOUT", "360")),
            max_extensions=int(os.getenv("SUPERVISOR_MAX_EXTENSIONS", "2")),
            extension_map=ext_map,
        )


@dataclass(frozen=True)
class LegbaConfig:
    """Top-level configuration aggregating all subsystem configs."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    qdrant: QdrantConfig = field(default_factory=QdrantConfig)
    nats: NatsConfig = field(default_factory=NatsConfig)
    opensearch: OpenSearchConfig = field(default_factory=OpenSearchConfig)
    audit_opensearch: OpenSearchConfig = field(default_factory=OpenSearchConfig)
    airflow: AirflowConfig = field(default_factory=AirflowConfig)
    paths: PathConfig = field(default_factory=PathConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)

    @classmethod
    def from_env(cls) -> LegbaConfig:
        return cls(
            llm=LLMConfig.from_env(),
            redis=RedisConfig.from_env(),
            postgres=PostgresConfig.from_env(),
            qdrant=QdrantConfig.from_env(),
            nats=NatsConfig.from_env(),
            opensearch=OpenSearchConfig.from_env(),
            audit_opensearch=OpenSearchConfig.from_audit_env(),
            airflow=AirflowConfig.from_env(),
            paths=PathConfig.from_env(),
            agent=AgentConfig.from_env(),
            supervisor=SupervisorConfig.from_env(),
        )
