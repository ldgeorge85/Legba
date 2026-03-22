"""
Legba Supervisor — Main Entry Point

The supervisor runs on the host (or in its own container with Docker socket).
It manages the agent's lifecycle:

1. Issue challenge (nonce)
2. Launch agent container (one cycle)
3. Wait for completion or timeout
4. Validate heartbeat response
5. Collect logs
6. Handle human communication
7. Repeat

The agent cannot reach, modify, or disable the supervisor.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import redis.asyncio as aioredis

from ..shared.config import LegbaConfig
from ..shared.schemas.comms import MessagePriority
from ..agent.comms.nats_client import LegbaNatsClient
from .heartbeat import HeartbeatManager
from .comms import CommsManager
from .lifecycle import LifecycleManager, CycleResult
from .audit import AuditIndexer
from .drain import LogDrain


class Supervisor:
    """
    Main supervisor loop.

    Manages the agent's lifecycle cycle by cycle.
    """

    def __init__(self, config: LegbaConfig | None = None):
        self.config = config or LegbaConfig.from_env()
        self.heartbeat = HeartbeatManager(self.config.paths.shared)
        self._nats_client = LegbaNatsClient(
            url=self.config.nats.url,
            connect_timeout=self.config.nats.connect_timeout,
        )
        self.comms = CommsManager(self.config.paths.shared, nats_client=self._nats_client)
        self.lifecycle = LifecycleManager(
            agent_image=os.getenv("LEGBA_AGENT_IMAGE", "legba-agent"),
            network=os.getenv("LEGBA_DOCKER_NETWORK", "legba_default"),
            container_name=os.getenv("LEGBA_AGENT_CONTAINER", "legba-agent-cycle"),
        )
        self.drain = LogDrain(self.config.paths.logs)
        self.audit = AuditIndexer(self.config.audit_opensearch)

        self._cycle_number: int = 0
        self._running: bool = False
        self._max_consecutive_failures: int = self.config.supervisor.max_consecutive_failures
        self._total_self_mods: int = 0
        self._recent_self_mods: list[int] = []  # self-mod counts for recent cycles
        self._last_stale_count_sent: int = 0  # L.6: avoid spamming same stale count
        self._last_stale_alert_cycle: int = 0  # Throttle: max once per 50 cycles

        # Auto-rollback: track git HEAD on /agent volume
        self._agent_code_path = Path("/agent")
        self._last_good_head: str | None = None  # HEAD the agent last successfully booted from

        # Host-side seed goal dir for bind-mounting into agent containers.
        # When the supervisor runs in a container, container-internal paths
        # don't work for `docker run -v` — Docker daemon needs host paths.
        self._seed_goal_host_dir: str = os.getenv("LEGBA_SEED_GOAL_HOST_DIR", "")

    async def run(self) -> None:
        """Main supervisor loop. Runs until stopped."""
        self._running = True
        print("[supervisor] Starting Legba supervisor", flush=True)
        print(f"[supervisor] Shared path: {self.config.paths.shared}", flush=True)
        print(f"[supervisor] Log path: {self.config.paths.logs}", flush=True)

        # Connect to NATS
        nats_ok = await self._nats_client.connect()
        if nats_ok:
            print(f"[supervisor] NATS connected: {self.config.nats.url}", flush=True)
        else:
            print("[supervisor] NATS unavailable — using file-based comms", flush=True)

        # Connect to audit OpenSearch
        audit_ok = await self.audit.connect()
        if audit_ok:
            print(f"[supervisor] Audit OpenSearch connected: {self.config.audit_opensearch.url}", flush=True)
        else:
            print("[supervisor] Audit OpenSearch unavailable — logs archived to disk only", flush=True)

        # Clean up any stale agent container from a previous run
        await self._cleanup_stale_agent()

        # Capture initial known-good agent code state
        self._last_good_head = await self._git_head()
        if self._last_good_head:
            print(f"[supervisor] Agent code HEAD: {self._last_good_head[:8]}...", flush=True)

        # Resume cycle count from Redis (persistent across restarts)
        self._cycle_number = await self._read_cycle_number_from_redis()
        if self._cycle_number > 0:
            print(f"[supervisor] Resuming from cycle {self._cycle_number}", flush=True)

        # Ensure seed goal exists
        seed_goal_path = Path(self.config.paths.seed_goal)
        if not seed_goal_path.exists():
            print(f"[supervisor] WARNING: Seed goal not found at {seed_goal_path}", flush=True)
            print("[supervisor] Creating placeholder seed goal", flush=True)
            seed_goal_path.parent.mkdir(parents=True, exist_ok=True)
            seed_goal_path.write_text("No seed goal configured. Awaiting operator directive.")

        while self._running:
            self._cycle_number += 1
            await self._run_cycle()

            # Check for kill conditions
            if self.heartbeat.consecutive_failures >= self._max_consecutive_failures:
                print(
                    f"[supervisor] KILL: {self.heartbeat.consecutive_failures} "
                    f"consecutive heartbeat failures. Stopping.",
                    flush=True,
                )
                break

            # Check for human outbox messages — display and forward to NATS for UI.
            # Only print messages from the current cycle to avoid replaying old history
            # on supervisor restart (stale messages stay in NATS for the UI).
            outbox_messages = await self.comms.read_outbox_async()
            for msg in outbox_messages:
                if msg.cycle_number and msg.cycle_number != self._cycle_number:
                    continue
                reply_tag = f" (reply to {msg.in_reply_to})" if msg.in_reply_to else ""
                print(f"\n[agent→human]{reply_tag} {msg.content}\n", flush=True)

            # Brief pause between cycles (supervisor-controlled timing)
            await asyncio.sleep(self.config.supervisor.cycle_sleep)

        await self.audit.close()
        await self._nats_client.close()
        print("[supervisor] Supervisor stopped", flush=True)

    async def _run_cycle(self) -> None:
        """Execute a single agent cycle."""
        ts = datetime.now(timezone.utc).isoformat()
        print(f"\n[supervisor] === Cycle {self._cycle_number} === {ts}", flush=True)

        # 1. Clean up previous cycle files and stale signals
        self.heartbeat.cleanup()
        self.lifecycle.cleanup_signals(self.config.paths.shared)

        # 2. Issue challenge
        challenge = self.heartbeat.issue_challenge(
            cycle_number=self._cycle_number,
            timeout_seconds=self.config.supervisor.heartbeat_timeout,
        )
        print(f"[supervisor] Challenge issued: nonce={challenge.nonce[:8]}...", flush=True)

        # 3. Check for human input (non-blocking stdin read could go here)
        # For now, the human uses `send_message()` or `send_directive()` externally

        # 4. Launch agent
        # Capture HEAD before launch — this is the state the agent boots from.
        # If this cycle succeeds, we know this HEAD is safe to boot from.
        pre_launch_head = await self._git_head()

        print("[supervisor] Launching agent...", flush=True)

        # Build env vars, only passing non-empty values so agent defaults work
        env_vars = {
            # Paths (always set)
            "LEGBA_SHARED": "/shared",
            "LEGBA_LOGS": "/logs",
            "LEGBA_SEED_GOAL": "/seed_goal/goal.txt",
            "LEGBA_WORKSPACE": "/workspace",
            "LEGBA_AGENT_CODE": "/agent",
            "LEGBA_AGENT_TOOLS": "/agent/tools",
            # Services (always set)
            "REDIS_HOST": "redis",
            "POSTGRES_HOST": "postgres",
            "QDRANT_HOST": "qdrant",
            "NATS_URL": self.config.nats.url,
        }
        # LLM, memory, and agent config: only pass through if set in supervisor env
        for var in [
            "LLM_PROVIDER",
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
            "LLM_TEMPERATURE", "LLM_TOP_P", "LLM_MAX_TOKENS", "LLM_TIMEOUT",
            "LLM_MAX_CONTEXT_TOKENS",
            "EMBEDDING_API_BASE", "EMBEDDING_API_KEY",
            "MEMORY_EMBEDDING_MODEL", "MEMORY_VECTOR_DIMENSIONS",
            "AGENT_MAX_REASONING_STEPS", "AGENT_MAX_SUBAGENT_STEPS",
            "AGENT_SHELL_TIMEOUT", "AGENT_HTTP_TIMEOUT",
            "AGENT_MEMORY_RETRIEVAL_LIMIT", "AGENT_FACTS_RETRIEVAL_LIMIT",
            "AGENT_BOOTSTRAP_THRESHOLD", "AGENT_MAX_CONTEXT_TOKENS",
            "OPENSEARCH_HOST", "OPENSEARCH_PORT", "OPENSEARCH_SCHEME",
            "AIRFLOW_URL", "AIRFLOW_ADMIN_USER", "AIRFLOW_ADMIN_PASSWORD",
            "AIRFLOW_DAGS_PATH",
            "INGESTION_SERVICE_ACTIVE",
        ]:
            val = os.getenv(var, "")
            if val:
                env_vars[var] = val

        vol_prefix = os.getenv("LEGBA_VOLUME_PREFIX", "legba")
        result = await self.lifecycle.run_cycle_docker(
            env_vars=env_vars,
            volumes={
                f"{vol_prefix}_shared_data": "/shared",
                f"{vol_prefix}_log_data": "/logs",
                f"{vol_prefix}_workspace_data": "/workspace",
                f"{vol_prefix}_agent_code": "/agent",
            },
            bind_mounts={
                self._seed_goal_host_dir or str(seed_goal_path.parent): "/seed_goal:ro",
            },
            timeout_seconds=challenge.timeout_seconds,
            shared_path=self.config.paths.shared,
        )

        self._log_cycle_result(result)

        # 5. Validate heartbeat
        valid, response, error = self.heartbeat.validate_response()
        if valid and response:
            print(
                f"[supervisor] Heartbeat OK: status={response.status}, "
                f"actions={response.actions_taken}, goals={response.goals_active}",
                flush=True,
            )
            if response.cycle_summary:
                print(f"[supervisor] Summary: {response.cycle_summary}", flush=True)

            # Track self-modification rate
            if response.self_modifications > 0:
                self._total_self_mods += response.self_modifications
                self._recent_self_mods.append(response.self_modifications)
                if len(self._recent_self_mods) > 10:
                    self._recent_self_mods.pop(0)
                recent_total = sum(self._recent_self_mods)
                print(
                    f"[supervisor] Self-mods this cycle: {response.self_modifications}, "
                    f"last 10 cycles: {recent_total}, total: {self._total_self_mods}",
                    flush=True,
                )
                # Warn if modification rate is high
                if recent_total >= 10:
                    print(
                        f"[supervisor] WARNING: High self-modification rate "
                        f"({recent_total} in last {len(self._recent_self_mods)} cycles)",
                        flush=True,
                    )
            else:
                self._recent_self_mods.append(0)
                if len(self._recent_self_mods) > 10:
                    self._recent_self_mods.pop(0)

            # Heartbeat OK — the pre-launch HEAD is proven safe (agent booted from it).
            # Don't use current HEAD here: the agent may have committed new code
            # during this cycle that hasn't been tested yet (takes effect next boot).
            if pre_launch_head:
                self._last_good_head = pre_launch_head

            # L.6: Check for stale goals and alert via inbox
            await self._check_stale_goals()
        else:
            print(f"[supervisor] Heartbeat FAILED: {error}", flush=True)
            self._write_outbox_alert(
                f"[SUPERVISOR ALERT] Heartbeat failure on cycle {self._cycle_number}: {error}. "
                f"Consecutive failures: {self.heartbeat.consecutive_failures}/"
                f"{self._max_consecutive_failures}."
            )

            # Auto-rollback: if agent code changed since last known-good state, revert
            await self._try_auto_rollback()

        # 6. Index logs to audit OpenSearch, then archive
        log_entries = self.drain.read_cycle_logs(self._cycle_number)
        if self.audit.available and log_entries:
            await self.audit.index_cycle_logs(self._cycle_number, log_entries)
        self.drain.archive_cycle(self._cycle_number)

    def _log_cycle_result(self, result: CycleResult) -> None:
        """Log the result of an agent cycle."""
        if result.timed_out:
            print(
                f"[supervisor] Agent TIMED OUT after {result.duration_seconds:.1f}s",
                flush=True,
            )
        elif result.graceful_shutdown:
            print(
                f"[supervisor] Agent completed (graceful shutdown) "
                f"in {result.duration_seconds:.1f}s",
                flush=True,
            )
        elif result.exit_code != 0:
            print(
                f"[supervisor] Agent FAILED (exit={result.exit_code}) "
                f"after {result.duration_seconds:.1f}s",
                flush=True,
            )
            if result.stderr:
                # Print last few lines of stderr
                lines = result.stderr.strip().split("\n")
                for line in lines[-5:]:
                    print(f"[supervisor]   stderr: {line}", flush=True)
        else:
            print(
                f"[supervisor] Agent completed in {result.duration_seconds:.1f}s",
                flush=True,
            )

    async def _read_cycle_number_from_redis(self) -> int:
        """Read the persistent cycle number from Redis."""
        try:
            r = aioredis.Redis(host="redis", port=6379, decode_responses=True)
            val = await r.get("legba:cycle_number")
            await r.aclose()
            return int(val) if val else 0
        except Exception:
            return 0

    async def _cleanup_stale_agent(self) -> None:
        """Remove any leftover agent container from a previous supervisor run."""
        container_name = os.getenv("LEGBA_AGENT_CONTAINER", "legba-agent-cycle")
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                print("[supervisor] Cleaned up stale agent container", flush=True)
        except Exception:
            pass

    async def _git_head(self) -> str | None:
        """Get current git HEAD hash on /agent volume."""
        if not (self._agent_code_path / ".git").exists():
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                cwd=str(self._agent_code_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode().strip()
        except Exception:
            pass
        return None

    async def _rollback_agent_code(self, target_head: str) -> bool:
        """Reset /agent to a specific git commit."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "reset", "--hard", target_head,
                cwd=str(self._agent_code_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, _ = await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    async def _try_auto_rollback(self) -> None:
        """
        Check if agent code changed since last known-good state.
        If so, roll back to prevent degraded agent from persisting bad code.
        """
        current_head = await self._git_head()
        if not current_head or not self._last_good_head:
            return
        if current_head == self._last_good_head:
            return

        print(
            f"[supervisor] Agent code changed since last good cycle "
            f"({self._last_good_head[:8]}... → {current_head[:8]}...)",
            flush=True,
        )
        print("[supervisor] Attempting auto-rollback...", flush=True)

        if await self._rollback_agent_code(self._last_good_head):
            print(
                f"[supervisor] Auto-rollback SUCCESS. "
                f"Agent code restored to {self._last_good_head[:8]}...",
                flush=True,
            )
            # Notify human
            self._write_outbox_alert(
                f"[SUPERVISOR] Auto-rollback triggered. Agent code reverted to "
                f"{self._last_good_head[:8]}... after heartbeat failure with "
                f"self-modifications detected."
            )
            # Reset consecutive failures — give the rolled-back code a fresh chance
            self.heartbeat._consecutive_failures = max(
                0, self.heartbeat._consecutive_failures - 1
            )
        else:
            print(
                "[supervisor] Auto-rollback FAILED. Manual intervention may be needed.",
                flush=True,
            )
            self._write_outbox_alert(
                f"[SUPERVISOR] Auto-rollback FAILED on cycle {self._cycle_number}. "
                f"Agent code is in a modified state. Manual intervention recommended."
            )

    async def _check_stale_goals(self) -> None:
        """Read reflection_forward from Redis; send inbox alert if goals are stale.

        Throttled to at most once per 50 cycles to avoid flooding the inbox.
        """
        try:
            r = aioredis.Redis(host="redis", port=6379, decode_responses=True)
            rf_raw = await r.get("legba:reflection_forward")
            await r.aclose()
            if not rf_raw:
                return
            rf = json.loads(rf_raw)
            stale = rf.get("stale_goal_count", 0)
            cycles_since = self._cycle_number - self._last_stale_alert_cycle
            if stale > 0 and cycles_since >= 50:
                await self.comms.send_message_async(
                    content=(
                        f"Note: {stale} goal(s) have had no progress for over 1 hour. "
                        "Consider completing, abandoning, or reprioritizing stuck goals."
                    ),
                    priority=MessagePriority.NORMAL,
                )
                self._last_stale_count_sent = stale
                self._last_stale_alert_cycle = self._cycle_number
                print(
                    f"[supervisor] Stale goal alert sent: {stale} goal(s)",
                    flush=True,
                )
            elif stale == 0:
                self._last_stale_count_sent = 0
        except Exception:
            pass  # Non-critical — don't crash supervisor over this

    def _write_outbox_alert(self, content: str) -> None:
        """Write a supervisor alert to the outbox (NATS + file fallback)."""
        from ..shared.schemas.comms import OutboxMessage, Outbox
        msg = OutboxMessage(
            id=str(uuid4()),
            content=content,
            cycle_number=self._cycle_number,
            metadata={"source": "supervisor", "type": "alert"},
        )
        # Publish to NATS so the UI picks it up
        nats_ok = False
        if self._nats_client and self._nats_client.available:
            try:
                asyncio.get_event_loop().create_task(
                    self._nats_client.publish_human_outbound(msg)
                )
                nats_ok = True
            except Exception:
                pass
        # File fallback — only when NATS is unavailable to prevent stale accumulation
        if not nats_ok:
            outbox_path = Path(self.config.paths.shared) / "outbox.json"
            try:
                existing = Outbox.model_validate_json(outbox_path.read_text()) if outbox_path.exists() else Outbox()
                existing.messages.append(msg)
                outbox_path.write_text(existing.model_dump_json(indent=2))
            except Exception:
                pass

    def stop(self) -> None:
        """Signal the supervisor to stop after the current cycle."""
        self._running = False
        print("[supervisor] Stop signal received", flush=True)


def main() -> None:
    """Entry point for `python -m legba.supervisor.main`."""
    supervisor = Supervisor()

    try:
        asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        print("\n[supervisor] Interrupted by operator", flush=True)
        supervisor.stop()


if __name__ == "__main__":
    main()
