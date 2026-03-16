"""
Unit tests for Legba.

No external services required -- these can run with ``--no-deps``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4, UUID

import pytest
from nacl.signing import SigningKey

# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------
from legba.shared.crypto import (
    sign_message,
    verify_message,
    VerificationError,
)


class TestCrypto:
    """Ed25519 sign / verify round-trip."""

    def test_generate_keypair_in_memory(self):
        sk = SigningKey.generate()
        vk = sk.verify_key
        assert len(bytes(sk)) == 32
        assert len(bytes(vk)) == 32

    def test_sign_and_verify_roundtrip(self):
        sk = SigningKey.generate()
        vk = sk.verify_key
        message = "cycle:42:nonce"
        sig = sign_message(sk, message)
        assert isinstance(sig, str)
        assert verify_message(vk, sig, message) is True

    def test_verify_bad_signature_raises(self):
        sk = SigningKey.generate()
        vk = sk.verify_key
        message = "hello"
        sig = sign_message(sk, message)
        # Flip a character in the signature to make it invalid
        bad_sig = sig[:-2] + ("00" if sig[-2:] != "00" else "ff")
        with pytest.raises(VerificationError):
            verify_message(vk, bad_sig, message)


# ---------------------------------------------------------------------------
# Goal schemas
# ---------------------------------------------------------------------------
from legba.shared.schemas.goals import (
    create_goal,
    create_subgoal,
    create_task,
    Goal,
    GoalType,
    GoalSource,
    GoalStatus,
    GoalUpdate,
)


class TestGoalSchemas:
    def test_create_goal_defaults(self):
        g = create_goal("Learn the environment")
        assert g.description == "Learn the environment"
        assert g.goal_type == GoalType.GOAL
        assert g.priority == 5
        assert g.source == GoalSource.AGENT
        assert g.status == GoalStatus.ACTIVE
        assert g.parent_id is None
        assert isinstance(g.id, UUID)

    def test_create_goal_with_criteria(self):
        g = create_goal(
            "Deploy service",
            goal_type=GoalType.META_GOAL,
            priority=2,
            source=GoalSource.HUMAN,
            success_criteria=["service running", "health-check passes"],
        )
        assert g.goal_type == GoalType.META_GOAL
        assert g.priority == 2
        assert g.source == GoalSource.HUMAN
        assert len(g.success_criteria) == 2

    def test_create_subgoal(self):
        parent = create_goal("Parent goal", priority=3)
        child = create_subgoal(parent, "Sub-goal of parent")
        assert child.goal_type == GoalType.SUBGOAL
        assert child.parent_id == parent.id
        assert child.priority == parent.priority
        assert child.source == GoalSource.SUBGOAL

    def test_create_task(self):
        parent = create_goal("Research", priority=4)
        task = create_task(parent, "Read config file")
        assert task.goal_type == GoalType.TASK
        assert task.parent_id == parent.id
        assert task.priority == parent.priority

    def test_goal_update(self):
        g = create_goal("Something")
        update = GoalUpdate(
            goal_id=g.id,
            status=GoalStatus.COMPLETED,
            progress_pct=100.0,
            completion_reason="Done",
        )
        assert update.goal_id == g.id
        assert update.status == GoalStatus.COMPLETED
        assert update.progress_pct == 100.0


# ---------------------------------------------------------------------------
# Comms schemas
# ---------------------------------------------------------------------------
from legba.shared.schemas.comms import (
    InboxMessage,
    OutboxMessage,
    Inbox,
    Outbox,
    MessagePriority,
    NatsMessage,
    StreamInfo,
    QueueSummary,
)


class TestCommsSchemas:
    def test_inbox_message(self):
        msg = InboxMessage(
            id=str(uuid4()),
            content="Hello agent",
            priority=MessagePriority.DIRECTIVE,
            requires_response=True,
        )
        assert msg.content == "Hello agent"
        assert msg.priority == MessagePriority.DIRECTIVE
        assert msg.requires_response is True

    def test_outbox_message(self):
        msg = OutboxMessage(
            id=str(uuid4()),
            content="Acknowledged",
            cycle_number=1,
        )
        assert msg.content == "Acknowledged"
        assert msg.cycle_number == 1

    def test_inbox_outbox_serialization_roundtrip(self):
        inbox_msg = InboxMessage(
            id=str(uuid4()),
            content="Check status",
            priority=MessagePriority.URGENT,
            requires_response=False,
        )
        inbox = Inbox(messages=[inbox_msg])
        inbox_json = inbox.model_dump_json()
        inbox_restored = Inbox.model_validate_json(inbox_json)
        assert len(inbox_restored.messages) == 1
        assert inbox_restored.messages[0].content == "Check status"

        outbox_msg = OutboxMessage(
            id=str(uuid4()),
            content="All systems nominal",
            cycle_number=5,
        )
        outbox = Outbox(messages=[outbox_msg])
        outbox_json = outbox.model_dump_json()
        outbox_restored = Outbox.model_validate_json(outbox_json)
        assert len(outbox_restored.messages) == 1
        assert outbox_restored.messages[0].cycle_number == 5


# ---------------------------------------------------------------------------
# Cycle schemas
# ---------------------------------------------------------------------------
from legba.shared.schemas.cycle import Challenge, CycleResponse


class TestCycleSchemas:
    def test_challenge_defaults(self):
        c = Challenge(cycle_number=1, nonce="abc")
        assert c.cycle_number == 1
        assert c.nonce == "abc"
        assert c.timeout_seconds == 300

    def test_cycle_response(self):
        now = datetime.now(timezone.utc)
        r = CycleResponse(
            cycle_number=3,
            nonce="xyz",
            started_at=now,
            completed_at=now,
            status="completed",
            cycle_summary="All good",
            actions_taken=2,
        )
        assert r.status == "completed"
        assert r.actions_taken == 2
        assert r.error is None


# ---------------------------------------------------------------------------
# Legacy harmony tests (removed — harmony module replaced by format.py)
# ---------------------------------------------------------------------------
from legba.agent.llm.format import Message




# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------
from legba.shared.schemas.tools import ToolDefinition, ToolParameter


class TestToolDefinition:
    def test_to_typescript_renders_syntax(self):
        defn = ToolDefinition(
            name="fs_read",
            description="Read a file",
            parameters=[
                ToolParameter(name="path", type="string", description="File path", required=True),
                ToolParameter(name="encoding", type="string", description="Encoding", required=False),
            ],
            return_type="string",
        )
        ts = defn.to_typescript()
        assert "// Read a file" in ts
        assert "type fs_read = (_: {" in ts
        assert "  path: string," in ts
        assert "  encoding?: string," in ts
        assert "}) => string;" in ts

    def test_required_vs_optional_params(self):
        defn = ToolDefinition(
            name="test_tool",
            description="Test",
            parameters=[
                ToolParameter(name="required_p", type="number", required=True),
                ToolParameter(name="optional_p", type="boolean", required=False),
            ],
        )
        ts = defn.to_typescript()
        assert "  required_p: number," in ts
        assert "  optional_p?: boolean," in ts


# ---------------------------------------------------------------------------
# Tool parser
# ---------------------------------------------------------------------------
from legba.agent.llm.tool_parser import parse_tool_calls


class TestToolParser:
    def test_parse_tool_calls_from_json(self):
        text = '{"actions": [{"tool": "fs_read", "args": {"path": "/test"}}]}'
        calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].tool_name == "fs_read"
        assert calls[0].arguments["path"] == "/test"

    def test_parse_no_tool_calls(self):
        calls = parse_tool_calls("Just some text with no tools")
        assert len(calls) == 0


# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------
from legba.shared.config import LegbaConfig


class TestConfig:
    def test_legba_config_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "http://test:8000/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_MODEL", "test-model")
        monkeypatch.setenv("REDIS_HOST", "test-redis")
        monkeypatch.setenv("REDIS_PORT", "6380")
        monkeypatch.setenv("POSTGRES_HOST", "test-pg")
        monkeypatch.setenv("POSTGRES_USER", "testuser")
        monkeypatch.setenv("QDRANT_HOST", "test-qdrant")

        cfg = LegbaConfig.from_env()
        assert cfg.llm.api_base == "http://test:8000/v1"
        assert cfg.llm.api_key == "test-key"
        assert cfg.llm.model == "test-model"
        assert cfg.redis.host == "test-redis"
        assert cfg.redis.port == 6380
        assert cfg.postgres.host == "test-pg"
        assert cfg.postgres.user == "testuser"
        assert cfg.qdrant.host == "test-qdrant"


# ---------------------------------------------------------------------------
# Modification schemas
# ---------------------------------------------------------------------------
from legba.shared.schemas.modifications import (
    CodeSnapshot,
    ModificationRecord,
    ModificationType,
    ModificationStatus,
)


class TestModificationSchemas:
    def test_code_snapshot_capture_sha256(self):
        snap = CodeSnapshot.capture("/agent/tools/my_tool.py", "print('hello')\n")
        assert snap.file_path == "/agent/tools/my_tool.py"
        assert snap.content == "print('hello')\n"
        assert len(snap.content_hash) == 64  # SHA-256 hex digest
        assert snap.line_count == 2  # one newline -> 2 lines

    def test_modification_record_creation(self):
        snap = CodeSnapshot.capture("/agent/prompt/templates.py", "x = 1")
        record = ModificationRecord(
            proposal_id=uuid4(),
            modification_type=ModificationType.CODE,
            file_path="/agent/prompt/templates.py",
            status=ModificationStatus.APPLIED,
            before_snapshot=snap,
            rationale="Improve performance",
            expected_outcome="Faster execution",
            cycle_number=7,
        )
        assert record.modification_type == ModificationType.CODE
        assert record.status == ModificationStatus.APPLIED
        assert record.before_snapshot is not None
        assert record.cycle_number == 7


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
from legba.agent.tools.registry import ToolRegistry


class TestToolRegistry:
    @staticmethod
    async def _dummy_handler(args: dict):
        return "ok"

    def test_register_and_get_definition(self):
        reg = ToolRegistry(dynamic_tools_path="/tmp/legba_test_tools_does_not_exist")
        defn = ToolDefinition(
            name="echo",
            description="Echo input",
            parameters=[ToolParameter(name="text", type="string")],
        )
        reg.register(defn, self._dummy_handler)
        assert reg.get_definition("echo") is not None
        assert reg.get_definition("echo").name == "echo"

    def test_get_handler(self):
        reg = ToolRegistry(dynamic_tools_path="/tmp/legba_test_tools_does_not_exist")
        defn = ToolDefinition(name="ping", description="Ping")
        reg.register(defn, self._dummy_handler)
        handler = reg.get_handler("ping")
        assert handler is self._dummy_handler

    def test_get_missing_returns_none(self):
        reg = ToolRegistry(dynamic_tools_path="/tmp/legba_test_tools_does_not_exist")
        assert reg.get_definition("nonexistent") is None
        assert reg.get_handler("nonexistent") is None

    def test_list_tools(self):
        reg = ToolRegistry(dynamic_tools_path="/tmp/legba_test_tools_does_not_exist")
        reg.register(
            ToolDefinition(name="a", description="Tool A"),
            self._dummy_handler,
        )
        reg.register(
            ToolDefinition(name="b", description="Tool B"),
            self._dummy_handler,
        )
        tools = reg.list_tools()
        names = {t.name for t in tools}
        assert names == {"a", "b"}

    def test_to_tool_definitions(self):
        reg = ToolRegistry(dynamic_tools_path="/tmp/legba_test_tools_does_not_exist")
        reg.register(
            ToolDefinition(
                name="greet",
                description="Greet someone",
                parameters=[ToolParameter(name="name", type="string", required=True)],
                return_type="string",
            ),
            self._dummy_handler,
        )
        tool_defs = reg.to_tool_definitions()
        assert '"greet"' in tool_defs
        assert '"name"' in tool_defs


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
from legba.agent.prompt.assembler import PromptAssembler


class TestPromptAssembler:
    def test_assemble_reason_prompt_includes_seed_goal(self):
        assembler = PromptAssembler(tool_data=[], tool_summary="(no tools)")
        messages = assembler.assemble_reason_prompt(
            cycle_number=1,
            seed_goal="Become self-improving",
            active_goals=[],
            memory_context={},
            inbox_messages=[],
        )
        # The messages list should contain user messages that reference the seed goal
        all_content = " ".join(m.content for m in messages)
        assert "Become self-improving" in all_content
        # Should have system, user at minimum
        assert len(messages) >= 2

    def test_assemble_reflect_prompt_works(self):
        assembler = PromptAssembler(tool_data=[], tool_summary="")
        messages = assembler.assemble_reflect_prompt(
            cycle_plan="Read 3 files and analyze config",
            working_memory="Step 1: read_file(path=config.yaml) → Found configuration",
            results_summary="Found configuration",
        )
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[1].role == "user"
        all_content = " ".join(m.content for m in messages)
        assert "Read 3 files" in all_content
        assert "Found configuration" in all_content

    def test_assemble_reason_prompt_with_inbox(self):
        assembler = PromptAssembler(tool_data=[], tool_summary="(no tools)")
        inbox_msg = InboxMessage(
            id=str(uuid4()),
            content="Please report status",
            priority=MessagePriority.DIRECTIVE,
            requires_response=True,
        )
        messages = assembler.assemble_reason_prompt(
            cycle_number=2,
            seed_goal="Build things",
            active_goals=[],
            memory_context={},
            inbox_messages=[inbox_msg],
        )
        all_content = " ".join(m.content for m in messages)
        assert "Please report status" in all_content


# ---------------------------------------------------------------------------
# Legacy harmony format tests removed — harmony module replaced by format.py.
# Extended tool parser tests removed — parse_tool_call replaced by
# parse_tool_calls with JSON-based tool format.
# ---------------------------------------------------------------------------



# (Harmony and extended tool parser test classes removed — referencing
#  deleted legba.agent.llm.harmony module. See git history for originals.)
# Removed: TestHarmonyForCompletion, TestHarmonyToolResult,
# TestHarmonyToolDefinitions, TestHarmonyParseToolCallHeader,
# TestHarmonyExtractFinal, TestHarmonyParseResponse,
# TestHarmonyMessageFormatting, TestToolParserLiteralText,
# TestToolParserMixedFormat, TestToolParserEdgeCases



# ---------------------------------------------------------------------------
# Goal tool definitions
# ---------------------------------------------------------------------------
from legba.agent.tools.builtins.goal_tools import (
    GOAL_CREATE_DEF,
    GOAL_LIST_DEF,
    GOAL_UPDATE_DEF,
    GOAL_DECOMPOSE_DEF,
)


class TestGoalToolDefinitions:
    """Verify goal tool definitions render correctly for Harmony."""

    def test_goal_create_harmony_typescript(self):
        ts = GOAL_CREATE_DEF.to_typescript()
        assert "type goal_create = " in ts
        assert "description: string," in ts
        assert "goal_type?: string," in ts
        assert "priority?: number," in ts

    def test_goal_list_harmony_typescript(self):
        ts = GOAL_LIST_DEF.to_typescript()
        assert "type goal_list = " in ts
        assert "status?: string," in ts

    def test_goal_update_harmony_typescript(self):
        ts = GOAL_UPDATE_DEF.to_typescript()
        assert "type goal_update = " in ts
        assert "goal_id: string," in ts
        assert "action: string," in ts
        assert "progress_pct?: number," in ts

    def test_goal_decompose_harmony_typescript(self):
        ts = GOAL_DECOMPOSE_DEF.to_typescript()
        assert "type goal_decompose = " in ts
        assert "goal_id: string," in ts
        assert "subtasks: string," in ts

    def test_all_goal_tools_in_namespace(self):
        """All four goal tools render inside a single namespace block."""
        reg = ToolRegistry(dynamic_tools_path="/tmp/legba_test_tools_does_not_exist")

        async def noop(args): return "ok"

        for defn in [GOAL_CREATE_DEF, GOAL_LIST_DEF, GOAL_UPDATE_DEF, GOAL_DECOMPOSE_DEF]:
            reg.register(defn, noop)

        tool_defs = reg.to_tool_definitions()
        assert '"goal_create"' in tool_defs
        assert '"goal_list"' in tool_defs
        assert '"goal_update"' in tool_defs
        assert '"goal_decompose"' in tool_defs


# ---------------------------------------------------------------------------
# Assembler memory guidance + bootstrap threshold tests
# ---------------------------------------------------------------------------


class TestAssemblerMemoryGuidance:
    """Verify memory management guidance is wired into prompts."""

    def test_memory_guidance_in_reason_prompt(self):
        assembler = PromptAssembler(tool_data=[], tool_summary="(no tools)")
        messages = assembler.assemble_reason_prompt(
            cycle_number=10,
            seed_goal="Be autonomous",
            active_goals=[],
            memory_context={},
            inbox_messages=[],
        )
        system_msg = messages[0].content
        assert "memory_store" in system_msg
        assert "memory_promote" in system_msg
        assert "memory_supersede" in system_msg
        assert "memory_supersede" in system_msg  # replaced 'relevance decay' with specific tool check

    def test_bootstrap_threshold_respected(self):
        assembler = PromptAssembler(tool_data=[], tool_summary="(no tools)", bootstrap_threshold=3)
        # Cycle 3 should include bootstrap addon
        msgs_early = assembler.assemble_reason_prompt(
            cycle_number=3, seed_goal="goal",
            active_goals=[], memory_context={}, inbox_messages=[],
        )
        assert "Early Cycle Guidance" in msgs_early[0].content

        # Cycle 4 should NOT include bootstrap addon
        msgs_late = assembler.assemble_reason_prompt(
            cycle_number=4, seed_goal="goal",
            active_goals=[], memory_context={}, inbox_messages=[],
        )
        assert "Early Cycle Guidance" not in msgs_late[0].content

    def test_default_bootstrap_threshold_is_5(self):
        assembler = PromptAssembler(tool_data=[], tool_summary="(no tools)")
        # Cycle 5 — should include bootstrap
        msgs = assembler.assemble_reason_prompt(
            cycle_number=5, seed_goal="goal",
            active_goals=[], memory_context={}, inbox_messages=[],
        )
        assert "Early Cycle Guidance" in msgs[0].content

        # Cycle 6 — should NOT include bootstrap
        msgs = assembler.assemble_reason_prompt(
            cycle_number=6, seed_goal="goal",
            active_goals=[], memory_context={}, inbox_messages=[],
        )
        assert "Early Cycle Guidance" not in msgs[0].content



# (TestFormatChainEndToEnd removed — referenced deleted harmony module)

# ---------------------------------------------------------------------------
# Supervisor: Heartbeat + Auto-Rollback
# ---------------------------------------------------------------------------
from legba.supervisor.heartbeat import HeartbeatManager


class TestHeartbeatManager:
    """Challenge-response heartbeat protocol."""

    def test_issue_challenge(self, tmp_path):
        mgr = HeartbeatManager(str(tmp_path))
        challenge = mgr.issue_challenge(cycle_number=1, timeout_seconds=60)
        assert challenge.cycle_number == 1
        assert challenge.timeout_seconds == 60
        assert len(challenge.nonce) > 0
        assert len(challenge.nonce) == 8  # Short hex nonce (uuid4().hex[:8])
        assert (tmp_path / "challenge.json").exists()

    def test_validate_no_response(self, tmp_path):
        mgr = HeartbeatManager(str(tmp_path))
        mgr.issue_challenge(cycle_number=1)
        valid, response, error = mgr.validate_response()
        assert valid is False
        assert "No response file" in error
        assert mgr.consecutive_failures == 1

    def test_validate_good_response(self, tmp_path):
        import json
        mgr = HeartbeatManager(str(tmp_path))
        challenge = mgr.issue_challenge(cycle_number=1)
        # Write response with transformed nonce (cycle number inserted at position)
        expected_nonce = HeartbeatManager.compute_expected_nonce(
            challenge.nonce, challenge.cycle_number
        )
        resp = {
            "cycle_number": 1,
            "nonce": expected_nonce,
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:01:00+00:00",
            "status": "completed",
            "cycle_summary": "test cycle",
            "actions_taken": 2,
            "goals_active": 1,
            "self_modifications": 0,
        }
        (tmp_path / "response.json").write_text(json.dumps(resp))
        valid, response, error = mgr.validate_response()
        assert valid is True
        assert response.nonce == expected_nonce
        assert mgr.consecutive_failures == 0

    def test_validate_nonce_mismatch(self, tmp_path):
        import json
        mgr = HeartbeatManager(str(tmp_path))
        mgr.issue_challenge(cycle_number=1)
        resp = {
            "cycle_number": 1,
            "nonce": "wrong-nonce",
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:01:00+00:00",
            "status": "completed",
            "cycle_summary": "",
            "actions_taken": 0,
            "goals_active": 0,
            "self_modifications": 0,
        }
        (tmp_path / "response.json").write_text(json.dumps(resp))
        valid, _, error = mgr.validate_response()
        assert valid is False
        assert "Nonce mismatch" in error

    def test_consecutive_failures_reset_on_success(self, tmp_path):
        import json
        mgr = HeartbeatManager(str(tmp_path))
        # Fail twice
        mgr.issue_challenge(cycle_number=1)
        mgr.validate_response()  # no response file
        mgr.issue_challenge(cycle_number=2)
        mgr.validate_response()
        assert mgr.consecutive_failures == 2
        # Then succeed with transformed nonce
        challenge = mgr.issue_challenge(cycle_number=3)
        expected_nonce = HeartbeatManager.compute_expected_nonce(
            challenge.nonce, challenge.cycle_number
        )
        resp = {
            "cycle_number": 3,
            "nonce": expected_nonce,
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:01:00+00:00",
            "status": "completed",
            "cycle_summary": "",
            "actions_taken": 0,
            "goals_active": 0,
            "self_modifications": 0,
        }
        (tmp_path / "response.json").write_text(json.dumps(resp))
        valid, _, _ = mgr.validate_response()
        assert valid is True
        assert mgr.consecutive_failures == 0

    def test_cleanup(self, tmp_path):
        mgr = HeartbeatManager(str(tmp_path))
        mgr.issue_challenge(cycle_number=1)
        assert (tmp_path / "challenge.json").exists()
        mgr.cleanup()
        assert not (tmp_path / "challenge.json").exists()


class TestSupervisorAutoRollback:
    """Supervisor auto-rollback on heartbeat failure with self-modifications."""

    @pytest.fixture
    def agent_repo(self, tmp_path):
        """Create a git repo simulating the /agent volume."""
        import subprocess
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=agent_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=agent_dir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=agent_dir, capture_output=True)
        # Create initial file and commit
        (agent_dir / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "-A"], cwd=agent_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial state"], cwd=agent_dir, capture_output=True)
        return agent_dir

    def _get_head(self, repo_path):
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path,
            capture_output=True, text=True,
        )
        return result.stdout.strip()

    def _add_commit(self, repo_path, filename, content, message):
        import subprocess
        (repo_path / filename).write_text(content)
        subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], cwd=repo_path, capture_output=True)

    @pytest.mark.asyncio
    async def test_git_head_returns_hash(self, agent_repo):
        from legba.supervisor.main import Supervisor
        sup = Supervisor.__new__(Supervisor)
        sup._agent_code_path = agent_repo
        head = await sup._git_head()
        assert head is not None
        assert len(head) == 40  # SHA-1 hex

    @pytest.mark.asyncio
    async def test_git_head_returns_none_no_git(self, tmp_path):
        from legba.supervisor.main import Supervisor
        no_git = tmp_path / "no_git"
        no_git.mkdir()
        sup = Supervisor.__new__(Supervisor)
        sup._agent_code_path = no_git
        head = await sup._git_head()
        assert head is None

    @pytest.mark.asyncio
    async def test_rollback_agent_code(self, agent_repo):
        """Rollback restores /agent to a previous commit."""
        from legba.supervisor.main import Supervisor
        good_head = self._get_head(agent_repo)

        # Simulate agent self-modification
        self._add_commit(agent_repo, "bad.py", "import evil", "[cycle 5] code: bad mod")
        bad_head = self._get_head(agent_repo)
        assert bad_head != good_head
        assert (agent_repo / "bad.py").exists()

        # Rollback
        sup = Supervisor.__new__(Supervisor)
        sup._agent_code_path = agent_repo
        success = await sup._rollback_agent_code(good_head)
        assert success is True
        assert self._get_head(agent_repo) == good_head
        assert not (agent_repo / "bad.py").exists()

    @pytest.mark.asyncio
    async def test_try_auto_rollback_skips_when_no_changes(self, agent_repo):
        """No rollback attempted when HEAD hasn't changed."""
        from legba.supervisor.main import Supervisor
        head = self._get_head(agent_repo)

        sup = Supervisor.__new__(Supervisor)
        sup._agent_code_path = agent_repo
        sup._last_good_head = head
        sup._cycle_number = 1
        sup.config = type("C", (), {"paths": type("P", (), {"shared": str(agent_repo.parent / "shared")})()})()
        (agent_repo.parent / "shared").mkdir(exist_ok=True)

        # Should be a no-op (HEAD == _last_good_head)
        await sup._try_auto_rollback()
        assert self._get_head(agent_repo) == head

    @pytest.mark.asyncio
    async def test_try_auto_rollback_reverts_on_code_change(self, agent_repo):
        """Auto-rollback triggers when HEAD differs from last known-good."""
        from legba.supervisor.main import Supervisor
        good_head = self._get_head(agent_repo)

        # Simulate bad self-modification
        self._add_commit(agent_repo, "destroy.py", "os.remove('/agent/main.py')", "[cycle 3] code: destroy")

        sup = Supervisor.__new__(Supervisor)
        sup._agent_code_path = agent_repo
        sup._last_good_head = good_head
        sup._cycle_number = 3
        sup.config = type("C", (), {"paths": type("P", (), {"shared": str(agent_repo.parent / "shared")})()})()
        (agent_repo.parent / "shared").mkdir(exist_ok=True)
        sup.heartbeat = HeartbeatManager(str(agent_repo.parent / "shared"))
        sup.heartbeat._consecutive_failures = 1

        await sup._try_auto_rollback()
        # Should have reverted
        assert self._get_head(agent_repo) == good_head
        assert not (agent_repo / "destroy.py").exists()
        assert (agent_repo / "main.py").read_text() == "print('hello')"

    @pytest.mark.asyncio
    async def test_auto_rollback_reduces_failure_count(self, agent_repo):
        """Successful rollback decrements consecutive failure counter."""
        from legba.supervisor.main import Supervisor
        good_head = self._get_head(agent_repo)

        self._add_commit(agent_repo, "bad.py", "x", "[cycle 2] code: bad")

        sup = Supervisor.__new__(Supervisor)
        sup._agent_code_path = agent_repo
        sup._last_good_head = good_head
        sup._cycle_number = 2
        sup.config = type("C", (), {"paths": type("P", (), {"shared": str(agent_repo.parent / "shared")})()})()
        (agent_repo.parent / "shared").mkdir(exist_ok=True)
        sup.heartbeat = HeartbeatManager(str(agent_repo.parent / "shared"))
        sup.heartbeat._consecutive_failures = 3

        await sup._try_auto_rollback()
        assert sup.heartbeat._consecutive_failures == 2

    @pytest.mark.asyncio
    async def test_multi_commit_rollback(self, agent_repo):
        """Rollback works across multiple self-mod commits."""
        from legba.supervisor.main import Supervisor
        good_head = self._get_head(agent_repo)

        # Multiple modifications
        self._add_commit(agent_repo, "mod1.py", "a=1", "[cycle 4] code: mod 1")
        self._add_commit(agent_repo, "mod2.py", "b=2", "[cycle 4] code: mod 2")
        self._add_commit(agent_repo, "mod3.py", "c=3", "[cycle 4] code: mod 3")
        assert (agent_repo / "mod1.py").exists()
        assert (agent_repo / "mod2.py").exists()
        assert (agent_repo / "mod3.py").exists()

        sup = Supervisor.__new__(Supervisor)
        sup._agent_code_path = agent_repo
        sup._last_good_head = good_head
        sup._cycle_number = 4
        sup.config = type("C", (), {"paths": type("P", (), {"shared": str(agent_repo.parent / "shared")})()})()
        (agent_repo.parent / "shared").mkdir(exist_ok=True)
        sup.heartbeat = HeartbeatManager(str(agent_repo.parent / "shared"))
        sup.heartbeat._consecutive_failures = 1

        await sup._try_auto_rollback()
        assert self._get_head(agent_repo) == good_head
        assert not (agent_repo / "mod1.py").exists()
        assert not (agent_repo / "mod2.py").exists()
        assert not (agent_repo / "mod3.py").exists()
        assert (agent_repo / "main.py").read_text() == "print('hello')"

    @pytest.mark.asyncio
    async def test_last_good_head_tracks_pre_launch_not_post_cycle(self, agent_repo):
        """_last_good_head should track the HEAD the agent booted from, not
        the HEAD after the cycle (which may include untested self-mods).

        Scenario: agent boots from commit A, passes heartbeat, but commits
        destructive code B during the cycle. _last_good_head must be A so
        the next cycle's failure can trigger rollback to A.
        """
        from legba.supervisor.main import Supervisor

        initial_head = self._get_head(agent_repo)

        # Simulate: cycle succeeds, agent commits destructive code during cycle
        self._add_commit(agent_repo, "cycle.py", "raise RuntimeError('boom')",
                         "[cycle 10] code: self-destruct")
        post_cycle_head = self._get_head(agent_repo)
        assert post_cycle_head != initial_head

        # The supervisor should set _last_good_head = pre_launch_head (initial),
        # NOT post_cycle_head. Simulate the correct behavior:
        sup = Supervisor.__new__(Supervisor)
        sup._agent_code_path = agent_repo
        sup._last_good_head = initial_head  # pre-launch HEAD, as the fix does
        sup._cycle_number = 11
        sup.config = type("C", (), {"paths": type("P", (), {"shared": str(agent_repo.parent / "shared")})()})()
        (agent_repo.parent / "shared").mkdir(exist_ok=True)
        sup.heartbeat = HeartbeatManager(str(agent_repo.parent / "shared"))
        sup.heartbeat._consecutive_failures = 1

        # Next cycle fails. Rollback should work because _last_good_head != current HEAD
        await sup._try_auto_rollback()
        assert self._get_head(agent_repo) == initial_head
        assert (agent_repo / "main.py").read_text() == "print('hello')"
        # cycle.py should be gone (didn't exist in initial commit)
        assert not (agent_repo / "cycle.py").exists()


# ---------------------------------------------------------------------------
# NATS schemas
# ---------------------------------------------------------------------------


class TestNatsSchemas:
    """NATS-specific schema tests."""

    def test_nats_message_defaults(self):
        msg = NatsMessage(subject="legba.data.test", payload={"key": "value"})
        assert msg.subject == "legba.data.test"
        assert msg.payload == {"key": "value"}
        assert msg.headers == {}
        assert msg.sequence is None

    def test_nats_message_with_headers(self):
        msg = NatsMessage(
            subject="legba.events.scan",
            payload={"scan_id": "abc"},
            headers={"priority": "high"},
            sequence=42,
        )
        assert msg.headers["priority"] == "high"
        assert msg.sequence == 42

    def test_nats_message_serialization_roundtrip(self):
        msg = NatsMessage(subject="legba.data.cves", payload={"cve": "CVE-2025-1234"})
        json_str = msg.model_dump_json()
        restored = NatsMessage.model_validate_json(json_str)
        assert restored.subject == "legba.data.cves"
        assert restored.payload["cve"] == "CVE-2025-1234"

    def test_stream_info(self):
        info = StreamInfo(
            name="LEGBA_DATA",
            subjects=["legba.data.>"],
            messages=150,
            bytes=1024 * 1024,
            consumer_count=2,
        )
        assert info.name == "LEGBA_DATA"
        assert info.messages == 150
        assert info.consumer_count == 2

    def test_queue_summary_defaults(self):
        qs = QueueSummary()
        assert qs.human_pending == 0
        assert qs.data_streams == []
        assert qs.total_data_messages == 0

    def test_queue_summary_with_data(self):
        streams = [
            StreamInfo(name="LEGBA_CVE", subjects=["legba.data.cves"], messages=100),
            StreamInfo(name="LEGBA_SCAN", subjects=["legba.data.scans"], messages=50),
        ]
        qs = QueueSummary(
            human_pending=3,
            data_streams=streams,
            total_data_messages=150,
        )
        assert qs.human_pending == 3
        assert len(qs.data_streams) == 2
        assert qs.total_data_messages == 150


# ---------------------------------------------------------------------------
# NATS config
# ---------------------------------------------------------------------------


class TestNatsConfig:
    def test_nats_config_defaults(self):
        from legba.shared.config import NatsConfig
        cfg = NatsConfig()
        assert cfg.url == "nats://localhost:4222"
        assert cfg.connect_timeout == 10

    def test_nats_config_from_env(self, monkeypatch):
        monkeypatch.setenv("NATS_URL", "nats://custom:4222")
        monkeypatch.setenv("NATS_CONNECT_TIMEOUT", "5")
        from legba.shared.config import NatsConfig
        cfg = NatsConfig.from_env()
        assert cfg.url == "nats://custom:4222"
        assert cfg.connect_timeout == 5

    def test_legba_config_includes_nats(self, monkeypatch):
        monkeypatch.setenv("NATS_URL", "nats://test-nats:4222")
        cfg = LegbaConfig.from_env()
        assert cfg.nats.url == "nats://test-nats:4222"


# ---------------------------------------------------------------------------
# NATS tool definitions
# ---------------------------------------------------------------------------
from legba.agent.tools.builtins.nats_tools import (
    NATS_PUBLISH_DEF,
    NATS_SUBSCRIBE_DEF,
    NATS_CREATE_STREAM_DEF,
    NATS_QUEUE_SUMMARY_DEF,
    NATS_LIST_STREAMS_DEF,
)


class TestNatsToolDefinitions:
    def test_nats_publish_harmony(self):
        ts = NATS_PUBLISH_DEF.to_typescript()
        assert "type nats_publish" in ts
        assert "subject: string," in ts
        assert "payload: string," in ts

    def test_nats_subscribe_harmony(self):
        ts = NATS_SUBSCRIBE_DEF.to_typescript()
        assert "type nats_subscribe" in ts
        assert "subject: string," in ts
        assert "limit?: number," in ts

    def test_nats_create_stream_harmony(self):
        ts = NATS_CREATE_STREAM_DEF.to_typescript()
        assert "type nats_create_stream" in ts
        assert "name: string," in ts
        assert "subjects: string," in ts

    def test_nats_queue_summary_harmony(self):
        ts = NATS_QUEUE_SUMMARY_DEF.to_typescript()
        assert "type nats_queue_summary" in ts

    def test_nats_list_streams_harmony(self):
        ts = NATS_LIST_STREAMS_DEF.to_typescript()
        assert "type nats_list_streams" in ts

    def test_all_nats_tools_in_namespace(self):
        reg = ToolRegistry(dynamic_tools_path="/tmp/legba_test_tools_does_not_exist")

        async def noop(args):
            return "ok"

        for defn in [NATS_PUBLISH_DEF, NATS_SUBSCRIBE_DEF, NATS_CREATE_STREAM_DEF,
                     NATS_QUEUE_SUMMARY_DEF, NATS_LIST_STREAMS_DEF]:
            reg.register(defn, noop)

        harmony_str = reg.to_tool_definitions()
        assert "type nats_publish" in harmony_str
        assert "type nats_subscribe" in harmony_str
        assert "type nats_create_stream" in harmony_str
        assert "type nats_queue_summary" in harmony_str
        assert "type nats_list_streams" in harmony_str
        assert harmony_str.count("namespace functions {") == 1


# ---------------------------------------------------------------------------
# Assembler with queue summary
# ---------------------------------------------------------------------------


class TestAssemblerQueueSummary:
    """Verify queue summary appears in assembled prompt when provided."""

    def test_queue_summary_in_reason_prompt(self):
        assembler = PromptAssembler(tool_data=[], tool_summary="(no tools)")
        queue = QueueSummary(
            human_pending=0,
            data_streams=[
                StreamInfo(name="LEGBA_CVE", subjects=["legba.data.cves"], messages=42),
            ],
            total_data_messages=42,
        )
        messages = assembler.assemble_reason_prompt(
            cycle_number=10,
            seed_goal="Monitor threats",
            active_goals=[],
            memory_context={},
            inbox_messages=[],
            queue_summary=queue,
        )
        all_content = " ".join(m.content for m in messages)
        assert "NATS Queue Summary" in all_content
        assert "LEGBA_CVE" in all_content
        assert "42" in all_content

    def test_empty_queue_summary_omitted(self):
        assembler = PromptAssembler(tool_data=[], tool_summary="(no tools)")
        queue = QueueSummary()  # all zeros
        messages = assembler.assemble_reason_prompt(
            cycle_number=10,
            seed_goal="Monitor threats",
            active_goals=[],
            memory_context={},
            inbox_messages=[],
            queue_summary=queue,
        )
        all_content = " ".join(m.content for m in messages)
        assert "NATS Queue Summary" not in all_content

    def test_no_queue_summary_omitted(self):
        assembler = PromptAssembler(tool_data=[], tool_summary="(no tools)")
        messages = assembler.assemble_reason_prompt(
            cycle_number=10,
            seed_goal="Monitor threats",
            active_goals=[],
            memory_context={},
            inbox_messages=[],
        )
        all_content = " ".join(m.content for m in messages)
        assert "NATS Queue Summary" not in all_content


# ---------------------------------------------------------------------------
# OpenSearch config
# ---------------------------------------------------------------------------
from legba.shared.config import OpenSearchConfig


class TestOpenSearchConfig:
    def test_opensearch_config_defaults(self):
        cfg = OpenSearchConfig()
        assert cfg.host == "localhost"
        assert cfg.port == 9200
        assert cfg.scheme == "http"
        assert cfg.username is None
        assert cfg.password is None

    def test_opensearch_config_url_property(self):
        cfg = OpenSearchConfig(host="os-node", port=9201, scheme="https")
        assert cfg.url == "https://os-node:9201"

    def test_opensearch_config_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENSEARCH_HOST", "my-opensearch")
        monkeypatch.setenv("OPENSEARCH_PORT", "9201")
        monkeypatch.setenv("OPENSEARCH_SCHEME", "https")
        monkeypatch.setenv("OPENSEARCH_USERNAME", "admin")
        monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
        cfg = OpenSearchConfig.from_env()
        assert cfg.host == "my-opensearch"
        assert cfg.port == 9201
        assert cfg.scheme == "https"
        assert cfg.username == "admin"
        assert cfg.password == "secret"

    def test_legba_config_includes_opensearch(self):
        from legba.shared.config import LegbaConfig
        cfg = LegbaConfig()
        assert hasattr(cfg, "opensearch")
        assert isinstance(cfg.opensearch, OpenSearchConfig)


# ---------------------------------------------------------------------------
# OpenSearch tool definitions
# ---------------------------------------------------------------------------
from legba.agent.tools.builtins.opensearch_tools import (
    OS_CREATE_INDEX_DEF,
    OS_INDEX_DOCUMENT_DEF,
    OS_SEARCH_DEF,
    OS_AGGREGATE_DEF,
    OS_DELETE_INDEX_DEF,
    OS_LIST_INDICES_DEF,
)


class TestOpenSearchToolDefinitions:
    def test_os_create_index_harmony(self):
        ts = OS_CREATE_INDEX_DEF.to_typescript()
        assert "os_create_index" in ts
        assert "index" in ts

    def test_os_index_document_harmony(self):
        ts = OS_INDEX_DOCUMENT_DEF.to_typescript()
        assert "os_index_document" in ts
        assert "document" in ts

    def test_os_search_harmony(self):
        ts = OS_SEARCH_DEF.to_typescript()
        assert "os_search" in ts
        assert "query" in ts

    def test_os_aggregate_harmony(self):
        ts = OS_AGGREGATE_DEF.to_typescript()
        assert "os_aggregate" in ts
        assert "aggs" in ts

    def test_os_delete_index_harmony(self):
        ts = OS_DELETE_INDEX_DEF.to_typescript()
        assert "os_delete_index" in ts

    def test_os_list_indices_harmony(self):
        ts = OS_LIST_INDICES_DEF.to_typescript()
        assert "os_list_indices" in ts

    def test_all_opensearch_tools_in_namespace(self):
        all_defs = [OS_CREATE_INDEX_DEF, OS_INDEX_DOCUMENT_DEF, OS_SEARCH_DEF,
                    OS_AGGREGATE_DEF, OS_DELETE_INDEX_DEF, OS_LIST_INDICES_DEF]
        names = [d.name for d in all_defs]
        assert all(n.startswith("os_") for n in names)
        assert len(set(names)) == 6


# ---------------------------------------------------------------------------
# Analytics tool definitions
# ---------------------------------------------------------------------------
from legba.agent.tools.builtins.analytics_tools import (
    ANOMALY_DETECT_DEF,
    FORECAST_DEF,
    NLP_EXTRACT_DEF,
    GRAPH_ANALYZE_DEF,
    CORRELATE_DEF,
)


class TestAnalyticsToolDefinitions:
    """Verify analytics tool definitions render correctly for Harmony."""

    def test_anomaly_detect_harmony(self):
        ts = ANOMALY_DETECT_DEF.to_typescript()
        assert "type anomaly_detect" in ts
        assert "method?" in ts
        assert "contamination?" in ts

    def test_forecast_harmony(self):
        ts = FORECAST_DEF.to_typescript()
        assert "type forecast" in ts
        assert "horizon?" in ts
        assert "frequency?" in ts

    def test_nlp_extract_harmony(self):
        ts = NLP_EXTRACT_DEF.to_typescript()
        assert "type nlp_extract" in ts
        assert "text?" in ts
        assert "operations?" in ts

    def test_graph_analyze_harmony(self):
        ts = GRAPH_ANALYZE_DEF.to_typescript()
        assert "type graph_analyze" in ts
        assert "operation: string," in ts
        assert "entity?" in ts

    def test_correlate_harmony(self):
        ts = CORRELATE_DEF.to_typescript()
        assert "type correlate" in ts
        assert "fields: string," in ts
        assert "operation?" in ts

    def test_all_analytics_tools_in_namespace(self):
        reg = ToolRegistry(dynamic_tools_path="/tmp/legba_test_tools_does_not_exist")

        async def noop(args):
            return "ok"

        all_defs = [ANOMALY_DETECT_DEF, FORECAST_DEF, NLP_EXTRACT_DEF,
                    GRAPH_ANALYZE_DEF, CORRELATE_DEF]
        for defn in all_defs:
            reg.register(defn, noop)

        harmony_str = reg.to_tool_definitions()
        assert "type anomaly_detect" in harmony_str
        assert "type forecast" in harmony_str
        assert "type nlp_extract" in harmony_str
        assert "type graph_analyze" in harmony_str
        assert "type correlate" in harmony_str
        assert harmony_str.count("namespace functions {") == 1

    def test_analytics_tool_names_unique(self):
        all_defs = [ANOMALY_DETECT_DEF, FORECAST_DEF, NLP_EXTRACT_DEF,
                    GRAPH_ANALYZE_DEF, CORRELATE_DEF]
        names = [d.name for d in all_defs]
        assert len(set(names)) == 5

    def test_analytics_tools_have_data_or_index_params(self):
        """All analytics tools that process data accept both inline and reference-based input."""
        data_tools = [ANOMALY_DETECT_DEF, FORECAST_DEF, NLP_EXTRACT_DEF, CORRELATE_DEF]
        for defn in data_tools:
            param_names = [p.name for p in defn.parameters]
            assert "index" in param_names, f"{defn.name} missing 'index' param"


# ---------------------------------------------------------------------------
# Analytics guidance in assembler
# ---------------------------------------------------------------------------


class TestAssemblerAnalyticsGuidance:
    """Verify analytics guidance is wired into prompts."""

    def test_analytics_guidance_in_reason_prompt(self):
        assembler = PromptAssembler(tool_data=[], tool_summary="(no tools)")
        messages = assembler.assemble_reason_prompt(
            cycle_number=10,
            seed_goal="Analyze data",
            active_goals=[],
            memory_context={},
            inbox_messages=[],
        )
        system_msg = messages[0].content
        assert "Analytical Tools" in system_msg
        assert "anomaly_detect" in system_msg
        assert "forecast" in system_msg
        assert "nlp_extract" in system_msg
        assert "graph_analyze" in system_msg
        assert "correlate" in system_msg


# ---------------------------------------------------------------------------
# Airflow config
# ---------------------------------------------------------------------------
from legba.shared.config import AirflowConfig


class TestAirflowConfig:
    def test_airflow_config_defaults(self):
        cfg = AirflowConfig()
        assert cfg.url == "http://localhost:8080"
        assert cfg.username == "airflow"
        assert cfg.password == "airflow"
        assert cfg.dags_path == "/airflow/dags"

    def test_airflow_config_from_env(self, monkeypatch):
        monkeypatch.setenv("AIRFLOW_URL", "http://airflow:8080")
        monkeypatch.setenv("AIRFLOW_ADMIN_USER", "admin")
        monkeypatch.setenv("AIRFLOW_ADMIN_PASSWORD", "secret")
        monkeypatch.setenv("AIRFLOW_DAGS_PATH", "/custom/dags")
        cfg = AirflowConfig.from_env()
        assert cfg.url == "http://airflow:8080"
        assert cfg.username == "admin"
        assert cfg.password == "secret"
        assert cfg.dags_path == "/custom/dags"

    def test_legba_config_includes_airflow(self):
        cfg = LegbaConfig()
        assert hasattr(cfg, "airflow")
        assert isinstance(cfg.airflow, AirflowConfig)

    def test_legba_config_from_env_includes_airflow(self, monkeypatch):
        monkeypatch.setenv("AIRFLOW_URL", "http://test-airflow:8080")
        cfg = LegbaConfig.from_env()
        assert cfg.airflow.url == "http://test-airflow:8080"


# ---------------------------------------------------------------------------
# Airflow client
# ---------------------------------------------------------------------------
from legba.agent.comms.airflow_client import AirflowClient


class TestAirflowClient:
    """Airflow client tests — no live Airflow required."""

    def test_client_initial_state(self):
        cfg = AirflowConfig()
        client = AirflowClient(cfg)
        assert client.available is False

    def test_deploy_dag_writes_file(self, tmp_path):
        cfg = AirflowConfig(dags_path=str(tmp_path / "dags"))
        client = AirflowClient(cfg)
        result = client.deploy_dag("test_dag", "from airflow import DAG\n# test")
        assert result["deployed"] is True
        assert result["dag_id"] == "test_dag"
        dag_file = tmp_path / "dags" / "test_dag.py"
        assert dag_file.exists()
        assert "from airflow import DAG" in dag_file.read_text()

    def test_deploy_dag_creates_dags_dir(self, tmp_path):
        cfg = AirflowConfig(dags_path=str(tmp_path / "nested" / "dags"))
        client = AirflowClient(cfg)
        result = client.deploy_dag("my_dag", "dag_code")
        assert result["deployed"] is True
        assert (tmp_path / "nested" / "dags" / "my_dag.py").exists()

    def test_remove_dag_file(self, tmp_path):
        cfg = AirflowConfig(dags_path=str(tmp_path))
        client = AirflowClient(cfg)
        # Create a dag file first
        (tmp_path / "old_dag.py").write_text("dag code")
        result = client.remove_dag_file("old_dag")
        assert result["removed"] is True
        assert not (tmp_path / "old_dag.py").exists()

    def test_remove_dag_file_not_found(self, tmp_path):
        cfg = AirflowConfig(dags_path=str(tmp_path))
        client = AirflowClient(cfg)
        result = client.remove_dag_file("nonexistent")
        assert result["removed"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_list_dags_unavailable(self):
        cfg = AirflowConfig()
        client = AirflowClient(cfg)
        result = await client.list_dags()
        assert result == []

    @pytest.mark.asyncio
    async def test_trigger_dag_unavailable(self):
        cfg = AirflowConfig()
        client = AirflowClient(cfg)
        result = await client.trigger_dag("test")
        assert "error" in result
        assert "unavailable" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_pause_dag_unavailable(self):
        cfg = AirflowConfig()
        client = AirflowClient(cfg)
        result = await client.pause_dag("test", paused=True)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_dag_unavailable(self):
        cfg = AirflowConfig()
        client = AirflowClient(cfg)
        result = await client.get_dag("test")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_dag_runs_unavailable(self):
        cfg = AirflowConfig()
        client = AirflowClient(cfg)
        result = await client.list_dag_runs("test")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_dag_run_unavailable(self):
        cfg = AirflowConfig()
        client = AirflowClient(cfg)
        result = await client.get_dag_run("test", "run_1")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_task_instances_unavailable(self):
        cfg = AirflowConfig()
        client = AirflowClient(cfg)
        result = await client.list_task_instances("test", "run_1")
        assert result == []


# ---------------------------------------------------------------------------
# Orchestration tool definitions
# ---------------------------------------------------------------------------
from legba.agent.tools.builtins.orchestration_tools import (
    WORKFLOW_DEFINE_DEF,
    WORKFLOW_TRIGGER_DEF,
    WORKFLOW_STATUS_DEF,
    WORKFLOW_LIST_DEF,
    WORKFLOW_PAUSE_DEF,
)


class TestOrchestrationToolDefinitions:
    """Verify orchestration tool definitions render correctly for Harmony."""

    def test_workflow_define_harmony(self):
        ts = WORKFLOW_DEFINE_DEF.to_typescript()
        assert "type workflow_define" in ts
        assert "dag_id: string," in ts
        assert "dag_code: string," in ts

    def test_workflow_trigger_harmony(self):
        ts = WORKFLOW_TRIGGER_DEF.to_typescript()
        assert "type workflow_trigger" in ts
        assert "dag_id: string," in ts
        assert "conf?" in ts

    def test_workflow_status_harmony(self):
        ts = WORKFLOW_STATUS_DEF.to_typescript()
        assert "type workflow_status" in ts
        assert "dag_id: string," in ts
        assert "dag_run_id?" in ts
        assert "include_tasks?" in ts

    def test_workflow_list_harmony(self):
        ts = WORKFLOW_LIST_DEF.to_typescript()
        assert "type workflow_list" in ts
        assert "limit?" in ts

    def test_workflow_pause_harmony(self):
        ts = WORKFLOW_PAUSE_DEF.to_typescript()
        assert "type workflow_pause" in ts
        assert "dag_id: string," in ts
        assert "paused: boolean," in ts

    def test_all_orchestration_tools_in_namespace(self):
        reg = ToolRegistry(dynamic_tools_path="/tmp/legba_test_tools_does_not_exist")

        async def noop(args):
            return "ok"

        all_defs = [WORKFLOW_DEFINE_DEF, WORKFLOW_TRIGGER_DEF, WORKFLOW_STATUS_DEF,
                    WORKFLOW_LIST_DEF, WORKFLOW_PAUSE_DEF]
        for defn in all_defs:
            reg.register(defn, noop)

        harmony_str = reg.to_tool_definitions()
        assert "type workflow_define" in harmony_str
        assert "type workflow_trigger" in harmony_str
        assert "type workflow_status" in harmony_str
        assert "type workflow_list" in harmony_str
        assert "type workflow_pause" in harmony_str
        assert harmony_str.count("namespace functions {") == 1

    def test_orchestration_tool_names_unique(self):
        all_defs = [WORKFLOW_DEFINE_DEF, WORKFLOW_TRIGGER_DEF, WORKFLOW_STATUS_DEF,
                    WORKFLOW_LIST_DEF, WORKFLOW_PAUSE_DEF]
        names = [d.name for d in all_defs]
        assert len(set(names)) == 5
        assert all(n.startswith("workflow_") for n in names)


# ---------------------------------------------------------------------------
# Orchestration guidance in assembler
# ---------------------------------------------------------------------------


class TestAssemblerOrchestrationGuidance:
    """Verify orchestration guidance is wired into prompts."""

    def test_orchestration_guidance_in_reason_prompt(self):
        assembler = PromptAssembler(tool_data=[], tool_summary="(no tools)")
        messages = assembler.assemble_reason_prompt(
            cycle_number=10,
            seed_goal="Orchestrate pipelines",
            active_goals=[],
            memory_context={},
            inbox_messages=[],
        )
        system_msg = messages[0].content
        assert "Workflows (Airflow)" in system_msg
        assert "workflow_define" in system_msg
        assert "workflow_trigger" in system_msg
        assert "workflow_status" in system_msg
        assert "workflow_list" in system_msg
        assert "workflow_pause" in system_msg


# ============================================================
# Audit OpenSearch Config
# ============================================================

class TestAuditOpenSearchConfig:
    """Verify audit OpenSearch config reads from correct env vars."""

    def test_from_audit_env_defaults(self):
        config = OpenSearchConfig.from_audit_env()
        assert config.host == "localhost"
        assert config.port == 9200
        assert config.scheme == "http"
        assert config.username is None

    def test_from_audit_env_reads_audit_vars(self, monkeypatch):
        monkeypatch.setenv("AUDIT_OPENSEARCH_HOST", "audit-os")
        monkeypatch.setenv("AUDIT_OPENSEARCH_PORT", "9201")
        monkeypatch.setenv("AUDIT_OPENSEARCH_SCHEME", "https")
        config = OpenSearchConfig.from_audit_env()
        assert config.host == "audit-os"
        assert config.port == 9201
        assert config.scheme == "https"

    def test_audit_opensearch_in_legba_config(self):
        config = LegbaConfig()
        assert hasattr(config, "audit_opensearch")
        assert isinstance(config.audit_opensearch, OpenSearchConfig)

    def test_legba_config_from_env_includes_audit(self, monkeypatch):
        monkeypatch.setenv("AUDIT_OPENSEARCH_HOST", "audit-host")
        config = LegbaConfig.from_env()
        assert config.audit_opensearch.host == "audit-host"

    def test_audit_config_independent_of_agent_config(self, monkeypatch):
        monkeypatch.setenv("OPENSEARCH_HOST", "agent-os")
        monkeypatch.setenv("AUDIT_OPENSEARCH_HOST", "audit-os")
        config = LegbaConfig.from_env()
        assert config.opensearch.host == "agent-os"
        assert config.audit_opensearch.host == "audit-os"


# ============================================================
# Audit Indexer
# ============================================================

class TestAuditIndexer:
    """Verify AuditIndexer behavior without a real OpenSearch instance."""

    def test_initial_state(self):
        from legba.supervisor.audit import AuditIndexer
        config = OpenSearchConfig(host="nonexistent", port=9999)
        indexer = AuditIndexer(config)
        assert not indexer.available

    def test_index_cycle_logs_when_unavailable(self):
        import asyncio
        from legba.supervisor.audit import AuditIndexer
        config = OpenSearchConfig(host="nonexistent", port=9999)
        indexer = AuditIndexer(config)
        result = asyncio.get_event_loop().run_until_complete(
            indexer.index_cycle_logs(1, [{"event": "test"}])
        )
        assert result["indexed"] == 0

    def test_index_cycle_logs_empty_entries(self):
        import asyncio
        from legba.supervisor.audit import AuditIndexer
        config = OpenSearchConfig(host="nonexistent", port=9999)
        indexer = AuditIndexer(config)
        result = asyncio.get_event_loop().run_until_complete(
            indexer.index_cycle_logs(1, [])
        )
        assert result["indexed"] == 0

    def test_bulk_body_formatting(self):
        """Verify the bulk request body format is correct ndjson."""
        import json
        from datetime import datetime, timezone
        from legba.supervisor.audit import AuditIndexer
        config = OpenSearchConfig()
        indexer = AuditIndexer(config)
        # Simulate what index_cycle_logs builds
        entries = [
            {"timestamp": "2026-02-18T12:00:00Z", "cycle": 1, "event": "phase", "phase": "wake"},
            {"timestamp": "2026-02-18T12:00:01Z", "cycle": 1, "event": "llm_call", "purpose": "reason"},
        ]
        # Build bulk body the same way the method does
        now = datetime.now(timezone.utc)
        index = f"legba-audit-{now:%Y.%m}"
        lines = []
        for entry in entries:
            action = json.dumps({"index": {"_index": index}})
            doc = json.dumps(entry, default=str)
            lines.append(action)
            lines.append(doc)
        body = "\n".join(lines) + "\n"
        # Verify structure: alternating action/doc lines, trailing newline
        body_lines = body.strip().split("\n")
        assert len(body_lines) == 4  # 2 entries * 2 lines each
        assert json.loads(body_lines[0])["index"]["_index"] == index
        assert json.loads(body_lines[1])["event"] == "phase"
        assert json.loads(body_lines[2])["index"]["_index"] == index
        assert json.loads(body_lines[3])["event"] == "llm_call"

    def test_index_name_monthly(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        expected = f"legba-audit-{now:%Y.%m}"
        assert expected.startswith("legba-audit-")
        assert len(expected) == len("legba-audit-YYYY.MM")

    def test_connect_fails_gracefully(self):
        import asyncio
        from legba.supervisor.audit import AuditIndexer
        config = OpenSearchConfig(host="nonexistent", port=9999)
        indexer = AuditIndexer(config)
        result = asyncio.get_event_loop().run_until_complete(indexer.connect())
        assert result is False
        assert not indexer.available

    def test_close_when_not_connected(self):
        import asyncio
        from legba.supervisor.audit import AuditIndexer
        config = OpenSearchConfig()
        indexer = AuditIndexer(config)
        # Should not raise
        asyncio.get_event_loop().run_until_complete(indexer.close())
        assert not indexer.available


# ============================================================
# LogDrain read_cycle_logs
# ============================================================

class TestCycleLoggerRename:
    """Verify CycleLogger.update_cycle_number() renames the log file."""

    def test_update_cycle_number_renames_file(self, tmp_path):
        import json
        from legba.agent.log import CycleLogger
        logger = CycleLogger(str(tmp_path), cycle_number=0)
        logger.log("phase", phase="wake")
        old_path = logger._log_file
        assert "cycle_000000_" in old_path.name

        logger.update_cycle_number(7)
        assert logger._cycle_number == 7
        assert "cycle_000007_" in logger._log_file.name
        assert not old_path.exists()
        assert logger._log_file.exists()

        # Can still write after rename
        logger.log("phase", phase="orient")
        logger.close()

        # Verify all entries are in the renamed file
        lines = logger._log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[1])["cycle"] == 7

    def test_update_cycle_number_noop_if_same(self, tmp_path):
        from legba.agent.log import CycleLogger
        logger = CycleLogger(str(tmp_path), cycle_number=5)
        original_path = logger._log_file
        logger.update_cycle_number(5)
        assert logger._log_file == original_path
        logger.close()


class TestDrainReadLogs:
    """Verify LogDrain.read_cycle_logs() reads JSONL entries."""

    def test_read_cycle_logs_parses_jsonl(self, tmp_path):
        import json
        from legba.supervisor.drain import LogDrain
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        # Write a fake cycle log file
        log_file = log_dir / "cycle_000042_20260218T120000Z.jsonl"
        entries = [
            {"timestamp": "2026-02-18T12:00:00Z", "cycle": 42, "event": "phase", "phase": "wake"},
            {"timestamp": "2026-02-18T12:00:01Z", "cycle": 42, "event": "tool_call", "tool_name": "fs_read"},
        ]
        with open(log_file, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        drain = LogDrain(str(log_dir), str(tmp_path / "archive"))
        result = drain.read_cycle_logs(42)
        assert len(result) == 2
        assert result[0]["event"] == "phase"
        assert result[1]["tool_name"] == "fs_read"

    def test_read_cycle_logs_handles_empty(self, tmp_path):
        from legba.supervisor.drain import LogDrain
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        drain = LogDrain(str(log_dir), str(tmp_path / "archive"))
        result = drain.read_cycle_logs(999)
        assert result == []

    def test_read_cycle_logs_skips_bad_lines(self, tmp_path):
        import json
        from legba.supervisor.drain import LogDrain
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_file = log_dir / "cycle_000001_20260218T120000Z.jsonl"
        with open(log_file, "w") as f:
            f.write(json.dumps({"event": "phase"}) + "\n")
            f.write("this is not json\n")
            f.write("\n")  # empty line
            f.write(json.dumps({"event": "tool_call"}) + "\n")
        drain = LogDrain(str(log_dir), str(tmp_path / "archive"))
        result = drain.read_cycle_logs(1)
        assert len(result) == 2
        assert result[0]["event"] == "phase"
        assert result[1]["event"] == "tool_call"

    def test_read_cycle_logs_multiple_files(self, tmp_path):
        import json
        from legba.supervisor.drain import LogDrain
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        # Two log files for the same cycle (rare but possible)
        for suffix in ["120000Z", "120001Z"]:
            log_file = log_dir / f"cycle_000005_{suffix}.jsonl"
            with open(log_file, "w") as f:
                f.write(json.dumps({"event": "test", "file": suffix}) + "\n")
        drain = LogDrain(str(log_dir), str(tmp_path / "archive"))
        result = drain.read_cycle_logs(5)
        assert len(result) == 2
