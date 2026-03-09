"""
Integration tests for Legba.

These tests require running Redis, Postgres, and Qdrant services.
Mark all tests with ``@pytest.mark.integration`` so they can be skipped
when external dependencies are unavailable.

Expected service endpoints:
    Redis:    redis://redis:6379/0
    Postgres: postgresql://legba:legba@postgres:5432/legba
    Qdrant:   host='qdrant', port=6333
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://legba:legba@postgres:5432/legba",
)

QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
VECTOR_SIZE = 1024


# ---------------------------------------------------------------------------
# Redis registers
# ---------------------------------------------------------------------------
from legba.agent.memory.registers import RegisterStore


@pytest.mark.integration
class TestRedisRegisters:
    @pytest.fixture
    async def store(self):
        s = RegisterStore(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
        await s.connect()
        yield s
        await s.close()

    async def test_set_get(self, store: RegisterStore):
        key = f"test:scalar:{uuid4().hex[:8]}"
        await store.set(key, "hello")
        assert await store.get(key) == "hello"

    async def test_incr_get_int(self, store: RegisterStore):
        key = f"test:counter:{uuid4().hex[:8]}"
        v1 = await store.incr(key)
        v2 = await store.incr(key)
        assert v2 == v1 + 1
        assert await store.get_int(key) == v2

    async def test_set_flag_get_flag(self, store: RegisterStore):
        key = f"test:flag:{uuid4().hex[:8]}"
        await store.set_flag(key, True)
        assert await store.get_flag(key) is True
        await store.set_flag(key, False)
        assert await store.get_flag(key) is False

    async def test_set_json_get_json(self, store: RegisterStore):
        key = f"test:json:{uuid4().hex[:8]}"
        payload = {"cycle": 42, "tags": ["a", "b"]}
        await store.set_json(key, payload)
        result = await store.get_json(key)
        assert result == payload


# ---------------------------------------------------------------------------
# Postgres structured store
# ---------------------------------------------------------------------------
from legba.agent.memory.structured import StructuredStore
from legba.shared.schemas.goals import (
    create_goal,
    GoalType,
    GoalSource,
    GoalStatus,
)
from legba.shared.schemas.memory import Fact


@pytest.mark.integration
class TestPostgresStructured:
    @pytest.fixture
    async def store(self):
        s = StructuredStore(dsn=POSTGRES_DSN)
        await s.connect()
        yield s
        await s.close()

    async def test_save_goal_and_get_active(self, store: StructuredStore):
        goal = create_goal(
            f"Integration test goal {uuid4().hex[:8]}",
            goal_type=GoalType.GOAL,
            priority=3,
        )
        saved = await store.save_goal(goal)
        assert saved is True

        active = await store.get_active_goals()
        ids = [g.id for g in active]
        assert goal.id in ids

    async def test_store_fact_and_query(self, store: StructuredStore):
        unique = uuid4().hex[:8]
        fact = Fact(
            subject=f"test_subject_{unique}",
            predicate="has_property",
            value="integration_test_value",
            confidence=0.95,
            source_cycle=1,
        )
        stored = await store.store_fact(fact)
        assert stored is True

        results = await store.query_facts(subject=f"test_subject_{unique}")
        assert len(results) >= 1
        assert any(f.value == "integration_test_value" for f in results)

    async def test_query_facts_by_predicate(self, store: StructuredStore):
        unique = uuid4().hex[:8]
        fact = Fact(
            subject="server_alpha",
            predicate=f"runs_service_{unique}",
            value="nginx",
            confidence=1.0,
            source_cycle=2,
        )
        await store.store_fact(fact)

        results = await store.query_facts(predicate=f"runs_service_{unique}")
        assert len(results) >= 1
        assert any(f.subject == "server_alpha" for f in results)


# ---------------------------------------------------------------------------
# Qdrant episodic store
# ---------------------------------------------------------------------------
from legba.agent.memory.episodic import EpisodicStore
from legba.shared.schemas.memory import Episode, EpisodeType


def _make_vector(seed: float = 0.1) -> list[float]:
    """Create a deterministic test vector of the right size."""
    import math

    return [math.sin(seed * (i + 1)) for i in range(VECTOR_SIZE)]


@pytest.mark.integration
class TestQdrantEpisodic:
    @pytest.fixture
    async def store(self):
        s = EpisodicStore(host=QDRANT_HOST, port=QDRANT_PORT, vector_size=VECTOR_SIZE)
        await s.connect()
        yield s
        await s.close()

    async def test_store_episode_and_search_similar(self, store: EpisodicStore):
        vec = _make_vector(0.5)
        episode = Episode(
            cycle_number=10,
            episode_type=EpisodeType.OBSERVATION,
            content="Noticed the config file was missing",
            significance=0.8,
            embedding=vec,
        )
        stored = await store.store_episode(episode)
        assert stored is True

        results = await store.search_similar(query_vector=vec, limit=3)
        assert len(results) >= 1
        assert any(r["content"] == "Noticed the config file was missing" for r in results)

    async def test_search_both_collections(self, store: EpisodicStore):
        vec = _make_vector(0.7)
        ep = Episode(
            cycle_number=20,
            episode_type=EpisodeType.LESSON,
            content="Search-both test episode",
            significance=0.9,
            embedding=vec,
        )
        await store.store_episode(ep, collection=EpisodicStore.SHORT_TERM)

        results = await store.search_both(query_vector=vec, limit=5)
        assert isinstance(results, list)
        # At least the episode we just stored should appear
        assert any(r["content"] == "Search-both test episode" for r in results)

    async def test_promote_to_long_term(self, store: EpisodicStore):
        vec = _make_vector(0.3)
        episode_id = str(uuid4())
        ep = Episode(
            id=episode_id,
            cycle_number=30,
            episode_type=EpisodeType.CYCLE_SUMMARY,
            content="Promote test episode",
            significance=1.0,
            embedding=vec,
        )
        # Store in short-term first
        await store.store_episode(ep, collection=EpisodicStore.SHORT_TERM)

        payload = {
            "cycle_number": 30,
            "episode_type": "cycle_summary",
            "content": "Promote test episode",
            "significance": 1.0,
        }
        promoted = await store.promote_to_long_term(episode_id, vec, payload)
        assert promoted is True

        # Should now be in long-term
        long_results = await store.search_similar(
            query_vector=vec,
            collection=EpisodicStore.LONG_TERM,
            limit=5,
        )
        assert any(r["content"] == "Promote test episode" for r in long_results)


# ---------------------------------------------------------------------------
# Memory manager
# ---------------------------------------------------------------------------
from legba.shared.config import LegbaConfig, LLMConfig, RedisConfig, PostgresConfig, QdrantConfig, PathConfig
from legba.agent.log import CycleLogger
from legba.agent.memory.manager import MemoryManager


def _make_test_config() -> LegbaConfig:
    return LegbaConfig(
        llm=LLMConfig(embedding_dimensions=VECTOR_SIZE),
        redis=RedisConfig(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB),
        postgres=PostgresConfig(
            host="postgres",
            port=5432,
            user="legba",
            password="legba",
            database="legba",
        ),
        qdrant=QdrantConfig(host=QDRANT_HOST, port=QDRANT_PORT),
        paths=PathConfig(),
    )


@pytest.mark.integration
class TestMemoryManager:
    @pytest.fixture
    async def manager(self, tmp_path):
        log_dir = str(tmp_path / "logs")
        logger = CycleLogger(log_dir=log_dir, cycle_number=0)
        cfg = _make_test_config()
        mgr = MemoryManager(config=cfg, logger=logger)
        await mgr.connect()
        yield mgr
        await mgr.close()
        logger.close()

    async def test_connect(self, manager: MemoryManager):
        # If we got here the fixture connected successfully
        assert manager.registers is not None
        assert manager.episodic is not None
        assert manager.structured is not None

    async def test_retrieve_context(self, manager: MemoryManager):
        vec = _make_vector(0.9)
        ctx = await manager.retrieve_context(query_embedding=vec, limit=3)
        assert "registers" in ctx
        assert "episodes" in ctx
        assert "goals" in ctx
        assert "facts" in ctx

    async def test_get_cycle_number(self, manager: MemoryManager):
        cycle = await manager.get_cycle_number()
        assert isinstance(cycle, int)
        assert cycle >= 0

    async def test_store_episode(self, manager: MemoryManager):
        vec = _make_vector(0.2)
        ep = Episode(
            cycle_number=99,
            episode_type=EpisodeType.ACTION,
            content="Manager store_episode test",
            significance=0.6,
            embedding=vec,
        )
        success = await manager.store_episode(ep)
        assert success is True


# ---------------------------------------------------------------------------
# Heartbeat manager
# ---------------------------------------------------------------------------
from legba.supervisor.heartbeat import HeartbeatManager
from legba.shared.schemas.cycle import CycleResponse


@pytest.mark.integration
class TestHeartbeatManager:
    @pytest.fixture
    def hb(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        return HeartbeatManager(shared_path=str(shared))

    def test_issue_challenge(self, hb: HeartbeatManager):
        challenge = hb.issue_challenge(cycle_number=1)
        assert challenge.cycle_number == 1
        assert len(challenge.nonce) > 0
        assert challenge.timeout_seconds == 300
        # Challenge file should exist
        assert hb.challenge_path.exists()
        data = json.loads(hb.challenge_path.read_text())
        assert data["nonce"] == challenge.nonce

    def test_validate_response_success(self, hb: HeartbeatManager):
        challenge = hb.issue_challenge(cycle_number=5)
        now = datetime.now(timezone.utc)
        expected_nonce = HeartbeatManager.compute_expected_nonce(
            challenge.nonce, challenge.cycle_number
        )
        response = CycleResponse(
            cycle_number=5,
            nonce=expected_nonce,
            started_at=now,
            completed_at=now,
            status="completed",
            cycle_summary="Test cycle",
            actions_taken=1,
        )
        hb.response_path.write_text(response.model_dump_json())

        valid, resp, err = hb.validate_response()
        assert valid is True
        assert resp is not None
        assert resp.nonce == expected_nonce
        assert err == ""

    def test_validate_response_missing_file(self, hb: HeartbeatManager):
        hb.issue_challenge(cycle_number=1)
        valid, resp, err = hb.validate_response()
        assert valid is False
        assert "No response file" in err

    def test_validate_response_nonce_mismatch(self, hb: HeartbeatManager):
        hb.issue_challenge(cycle_number=1)
        now = datetime.now(timezone.utc)
        bad_response = CycleResponse(
            cycle_number=1,
            nonce="wrong-nonce",
            started_at=now,
            completed_at=now,
            status="completed",
            cycle_summary="Bad",
        )
        hb.response_path.write_text(bad_response.model_dump_json())

        valid, resp, err = hb.validate_response()
        assert valid is False
        assert "Nonce mismatch" in err


# ---------------------------------------------------------------------------
# NATS integration
# ---------------------------------------------------------------------------
from legba.agent.comms.nats_client import LegbaNatsClient
from legba.shared.schemas.comms import InboxMessage, OutboxMessage, MessagePriority


NATS_URL = os.getenv("NATS_URL", "nats://nats:4222")


@pytest.mark.integration
class TestNatsClient:
    """Integration tests for NATS pub/sub — requires running NATS server."""

    @pytest.fixture
    async def client(self):
        c = LegbaNatsClient(url=NATS_URL, connect_timeout=5)
        connected = await c.connect()
        if not connected:
            pytest.skip("NATS not available")
        yield c
        await c.close()

    async def test_connect(self, client: LegbaNatsClient):
        assert client.available is True

    async def test_human_inbound_publish_and_drain(self, client: LegbaNatsClient):
        """Publish a human inbound message and drain it."""
        msg = InboxMessage(
            id=str(uuid4()),
            content=f"Integration test message {uuid4().hex[:8]}",
            priority=MessagePriority.NORMAL,
            requires_response=False,
        )
        ok = await client.publish_human_inbound(msg)
        assert ok is True

        # Drain should return the message
        messages = await client.drain_human_inbound()
        assert len(messages) >= 1
        assert any(m.content == msg.content for m in messages)

    async def test_human_outbound_publish_and_drain(self, client: LegbaNatsClient):
        """Publish an agent outbound message and drain it."""
        msg = OutboxMessage(
            id=str(uuid4()),
            content=f"Agent response {uuid4().hex[:8]}",
            cycle_number=99,
        )
        ok = await client.publish_human_outbound(msg)
        assert ok is True

        messages = await client.drain_human_outbound()
        assert len(messages) >= 1
        assert any(m.content == msg.content for m in messages)

    async def test_create_stream_and_publish(self, client: LegbaNatsClient):
        """Create a data stream, publish to it, verify via list_streams."""
        stream_name = f"TEST_STREAM_{uuid4().hex[:8]}"
        subject = f"legba.test.{uuid4().hex[:8]}"

        result = await client.create_stream(
            name=stream_name,
            subjects=[subject],
            max_msgs=100,
            max_age=60,  # 1 minute retention
        )
        assert "error" not in result
        assert result["name"] == stream_name

        # Publish a message
        ok = await client.publish(subject, {"test": "data"})
        assert ok is True

        # Verify via list_streams
        streams = await client.list_streams()
        names = [s.name for s in streams]
        assert stream_name in names

    async def test_queue_summary(self, client: LegbaNatsClient):
        """Queue summary returns valid structure."""
        summary = await client.queue_summary()
        assert isinstance(summary.human_pending, int)
        assert isinstance(summary.data_streams, list)
        assert isinstance(summary.total_data_messages, int)

    async def test_graceful_degradation_on_disconnect(self):
        """Client returns empty results when disconnected."""
        c = LegbaNatsClient(url="nats://nonexistent:4222", connect_timeout=1)
        connected = await c.connect()
        assert connected is False
        assert c.available is False

        # All operations should return gracefully
        messages = await c.drain_human_inbound()
        assert messages == []
        ok = await c.publish("legba.test", {"x": 1})
        assert ok is False
        summary = await c.queue_summary()
        assert summary.human_pending == 0


# ---------------------------------------------------------------------------
# OpenSearch integration
# ---------------------------------------------------------------------------
from legba.shared.config import OpenSearchConfig
from legba.agent.memory.opensearch import OpenSearchStore

OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST", "opensearch")
OPENSEARCH_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))


@pytest.mark.integration
class TestOpenSearch:
    @pytest.fixture
    async def store(self):
        cfg = OpenSearchConfig(host=OPENSEARCH_HOST, port=OPENSEARCH_PORT)
        s = OpenSearchStore(cfg)
        connected = await s.connect()
        if not connected:
            pytest.skip("OpenSearch not available")
        yield s
        await s.close()

    @pytest.fixture
    def test_index(self):
        """Generate a unique test index name."""
        return f"legba-test-{uuid4().hex[:8]}"

    async def test_connect(self, store: OpenSearchStore):
        assert store.available is True

    async def test_create_and_delete_index(self, store: OpenSearchStore, test_index: str):
        # Create
        result = await store.create_index(test_index, settings={"number_of_shards": 1, "number_of_replicas": 0})
        assert result.get("acknowledged") is True

        # Create again — should succeed (already exists)
        result2 = await store.create_index(test_index)
        assert result2.get("acknowledged") is True
        assert result2.get("already_exists") is True

        # Delete
        result3 = await store.delete_index(test_index)
        assert result3.get("acknowledged") is True

        # Delete again — should error
        result4 = await store.delete_index(test_index)
        assert "error" in result4

    async def test_index_and_search_document(self, store: OpenSearchStore, test_index: str):
        # Create index with mappings
        await store.create_index(test_index, mappings={
            "properties": {
                "title": {"type": "text"},
                "severity": {"type": "keyword"},
                "score": {"type": "float"},
            }
        }, settings={"number_of_shards": 1, "number_of_replicas": 0})

        try:
            # Index a document
            doc = {"title": "Critical Apache vulnerability", "severity": "critical", "score": 9.8}
            result = await store.index_document(test_index, doc, doc_id="cve-001")
            assert result.get("_id") == "cve-001"

            # Search
            search_result = await store.search(test_index, {"match": {"title": "Apache"}})
            assert search_result["total"] >= 1
            assert any(h.get("title") == "Critical Apache vulnerability" for h in search_result["hits"])

            # Search with fields filter
            search_result2 = await store.search(test_index, {"match_all": {}}, source=["severity"])
            assert search_result2["total"] >= 1
            hit = search_result2["hits"][0]
            assert "severity" in hit
        finally:
            await store.delete_index(test_index)

    async def test_bulk_index(self, store: OpenSearchStore, test_index: str):
        await store.create_index(test_index, settings={"number_of_shards": 1, "number_of_replicas": 0})
        try:
            docs = [
                {"title": f"Doc {i}", "value": i}
                for i in range(5)
            ]
            result = await store.bulk_index(test_index, docs)
            assert result["indexed"] == 5
            assert result["errors"] == 0

            # Verify count
            search_result = await store.search(test_index, {"match_all": {}}, size=10)
            assert search_result["total"] == 5
        finally:
            await store.delete_index(test_index)

    async def test_aggregate(self, store: OpenSearchStore, test_index: str):
        await store.create_index(test_index, mappings={
            "properties": {
                "category": {"type": "keyword"},
                "value": {"type": "integer"},
            }
        }, settings={"number_of_shards": 1, "number_of_replicas": 0})

        try:
            docs = [
                {"category": "a", "value": 10},
                {"category": "a", "value": 20},
                {"category": "b", "value": 30},
            ]
            await store.bulk_index(test_index, docs)

            result = await store.aggregate(
                test_index,
                {"categories": {"terms": {"field": "category"}}},
            )
            agg = result["aggregations"]["categories"]
            buckets = agg["buckets"]
            assert len(buckets) == 2
            names = {b["key"] for b in buckets}
            assert names == {"a", "b"}
        finally:
            await store.delete_index(test_index)

    async def test_list_indices(self, store: OpenSearchStore, test_index: str):
        await store.create_index(test_index, settings={"number_of_shards": 1, "number_of_replicas": 0})
        try:
            indices = await store.list_indices(pattern="legba-test-*")
            names = [i["index"] for i in indices]
            assert test_index in names
        finally:
            await store.delete_index(test_index)

    async def test_get_and_delete_document(self, store: OpenSearchStore, test_index: str):
        await store.create_index(test_index, settings={"number_of_shards": 1, "number_of_replicas": 0})
        try:
            await store.index_document(test_index, {"msg": "hello"}, doc_id="doc-1")

            doc = await store.get_document(test_index, "doc-1")
            assert doc is not None
            assert doc["msg"] == "hello"

            deleted = await store.delete_document(test_index, "doc-1")
            assert deleted is True

            doc2 = await store.get_document(test_index, "doc-1")
            assert doc2 is None
        finally:
            await store.delete_index(test_index)

    async def test_graceful_degradation(self):
        cfg = OpenSearchConfig(host="nonexistent", port=9200)
        s = OpenSearchStore(cfg)
        connected = await s.connect()
        assert connected is False
        assert s.available is False

        # All operations should return gracefully
        result = await s.search("any", {"match_all": {}})
        assert result["hits"] == []
        indices = await s.list_indices()
        assert indices == []
        create = await s.create_index("any")
        assert "error" in create
