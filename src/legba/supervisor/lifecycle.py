"""
Agent Lifecycle Manager

Manages the agent container: build, launch, monitor, kill.
The supervisor launches one agent container per cycle via the Docker API.

Graceful shutdown: when the soft timeout is reached, writes a stop flag
to the shared volume. The agent checks between reasoning steps and pings
back, allowing REFLECT and PERSIST to run before exit.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# How long to wait for the agent to acknowledge the stop flag.
# Must exceed worst-case LLM inference time (observed up to ~120s).
PING_WAIT_SECONDS = 150

# After a ping, extend the timeout by this fraction of the original.
EXTENSION_FACTOR = 0.5

# Default maximum number of timeout extensions (overridable via config).
DEFAULT_MAX_EXTENSIONS = 2

# Polling interval when monitoring the container.
POLL_INTERVAL = 2.0


@dataclass
class CycleResult:
    """Result of running an agent cycle."""

    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str
    timed_out: bool = False
    graceful_shutdown: bool = False


class LifecycleManager:
    """
    Manages the agent's process lifecycle.

    In production: launches Docker containers per cycle.
    For development: can also run agent as a subprocess.
    """

    def __init__(
        self,
        agent_image: str = "legba-agent",
        network: str = "legba_default",
        container_name: str = "legba-agent-cycle",
    ):
        self.agent_image = agent_image
        self.network = network
        self.container_name = container_name

    async def run_cycle_docker(
        self,
        env_vars: dict[str, str],
        volumes: dict[str, str],
        timeout_seconds: int = 300,
        bind_mounts: dict[str, str] | None = None,
        shared_path: str = "",
        max_extensions: int = DEFAULT_MAX_EXTENSIONS,
        extension_map: dict[str, int] | None = None,
    ) -> CycleResult:
        """
        Launch the agent as a Docker container for a single cycle.

        Uses a soft timeout with graceful shutdown: when the timeout is
        reached, writes a stop flag to the shared volume. If the agent
        acknowledges with a ping within PING_WAIT_SECONDS, the timeout
        is extended to allow REFLECT and PERSIST to complete.

        Args:
            volumes: Named Docker volumes {volume_name: container_path}
            bind_mounts: Host directory bind mounts {host_path: container_path}
            shared_path: Path to the shared volume (for signal files)
            max_extensions: Default max timeout extensions (pings allowed)
            extension_map: Per-cycle-type overrides {cycle_type: max_extensions}
        """
        # Build docker run command
        cmd = ["docker", "run", "--rm", "--name", self.container_name]

        # Network
        cmd.extend(["--network", self.network])

        # Environment variables
        for key, value in env_vars.items():
            cmd.extend(["-e", f"{key}={value}"])

        # Named volume mounts
        for host_path, container_path in volumes.items():
            cmd.extend(["-v", f"{host_path}:{container_path}"])

        # Bind mounts (host directories)
        if bind_mounts:
            for host_path, container_path in bind_mounts.items():
                cmd.extend(["-v", f"{host_path}:{container_path}"])

        # Image
        cmd.append(self.agent_image)

        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Monitor with graceful shutdown support
            return await self._monitor_with_graceful_shutdown(
                proc, start, timeout_seconds, shared_path,
                max_extensions=max_extensions,
                extension_map=extension_map,
            )

        except Exception as e:
            duration = time.monotonic() - start
            return CycleResult(
                exit_code=-1,
                duration_seconds=duration,
                stdout="",
                stderr=f"Failed to launch agent: {e}",
            )

    async def _monitor_with_graceful_shutdown(
        self,
        proc: asyncio.subprocess.Process,
        start: float,
        timeout_seconds: int,
        shared_path: str,
        max_extensions: int = DEFAULT_MAX_EXTENSIONS,
        extension_map: dict[str, int] | None = None,
    ) -> CycleResult:
        """
        Monitor the agent process with soft timeout and graceful shutdown.

        Flow:
        1. Wait for container exit or soft timeout
        2. On soft timeout: write stop_flag.json, wait up to PING_WAIT_SECONDS
        3. If agent writes stop_ping.json: extend timeout by EXTENSION_FACTOR
        4. If no ping or max extensions exceeded: hard kill

        The agent writes cycle_type.json to the shared volume early in WAKE.
        If extension_map has an override for that cycle type, it replaces
        max_extensions dynamically.
        """
        comm_task = asyncio.create_task(proc.communicate())
        soft_deadline = start + timeout_seconds
        extensions = 0
        stop_flag_written = False
        graceful = False
        effective_max = max_extensions
        _type_resolved = False

        while True:
            now = time.monotonic()
            remaining = soft_deadline - now

            # Check if the container has already exited
            done, _ = await asyncio.wait(
                {comm_task}, timeout=min(max(remaining, 0), POLL_INTERVAL),
            )

            if done:
                # Container exited on its own
                stdout_bytes, stderr_bytes = comm_task.result()
                duration = time.monotonic() - start
                return CycleResult(
                    exit_code=proc.returncode or 0,
                    duration_seconds=duration,
                    stdout=stdout_bytes.decode("utf-8", errors="replace"),
                    stderr=stderr_bytes.decode("utf-8", errors="replace"),
                    graceful_shutdown=graceful,
                )

            # Resolve per-cycle-type extension override (once)
            if not _type_resolved and extension_map and shared_path:
                ct = self._read_cycle_type(shared_path)
                if ct:
                    _type_resolved = True
                    if ct in extension_map:
                        effective_max = extension_map[ct]
                        print(
                            f"[lifecycle] Cycle type {ct}: "
                            f"max_extensions={effective_max}",
                            flush=True,
                        )

            # Soft timeout reached?
            if time.monotonic() >= soft_deadline:
                if not stop_flag_written and shared_path:
                    # Write the stop flag
                    self._write_stop_flag(shared_path)
                    stop_flag_written = True
                    print(
                        f"[lifecycle] Soft timeout at {time.monotonic() - start:.0f}s, "
                        f"wrote stop flag",
                        flush=True,
                    )

                    # Wait for ping (up to PING_WAIT_SECONDS)
                    ping_deadline = time.monotonic() + PING_WAIT_SECONDS

                    while time.monotonic() < ping_deadline:
                        done, _ = await asyncio.wait(
                            {comm_task}, timeout=POLL_INTERVAL,
                        )
                        if done:
                            stdout_bytes, stderr_bytes = comm_task.result()
                            duration = time.monotonic() - start
                            return CycleResult(
                                exit_code=proc.returncode or 0,
                                duration_seconds=duration,
                                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                                graceful_shutdown=True,
                            )

                        if self._check_ping(shared_path):
                            extensions += 1
                            bonus = timeout_seconds * EXTENSION_FACTOR
                            soft_deadline = time.monotonic() + bonus
                            stop_flag_written = False  # allow another flag if needed
                            graceful = True
                            print(
                                f"[lifecycle] Ping received, extending by {bonus:.0f}s "
                                f"(extension {extensions}/{effective_max})",
                                flush=True,
                            )
                            # Remove the ping file so we can detect the next one
                            self._remove_file(shared_path, "stop_ping.json")
                            break
                    else:
                        # No ping within deadline — hard kill
                        print(
                            "[lifecycle] No ping received within "
                            f"{PING_WAIT_SECONDS}s, hard killing agent",
                            flush=True,
                        )
                        await self.kill_agent()
                        try:
                            await asyncio.wait_for(comm_task, timeout=10)
                        except (asyncio.TimeoutError, Exception):
                            pass
                        duration = time.monotonic() - start
                        return CycleResult(
                            exit_code=-1,
                            duration_seconds=duration,
                            stdout="",
                            stderr="Agent did not acknowledge stop flag",
                            timed_out=True,
                        )

                elif extensions >= effective_max:
                    # Max extensions exhausted — hard kill
                    print(
                        f"[lifecycle] Max extensions ({effective_max}) exhausted, "
                        f"hard killing agent",
                        flush=True,
                    )
                    await self.kill_agent()
                    try:
                        await asyncio.wait_for(comm_task, timeout=10)
                    except (asyncio.TimeoutError, Exception):
                        pass
                    duration = time.monotonic() - start
                    return CycleResult(
                        exit_code=-1,
                        duration_seconds=duration,
                        stdout="",
                        stderr="Agent exceeded max timeout extensions",
                        timed_out=True,
                    )

                elif not shared_path:
                    # No shared path — fall back to hard kill
                    await self.kill_agent()
                    try:
                        await asyncio.wait_for(comm_task, timeout=10)
                    except (asyncio.TimeoutError, Exception):
                        pass
                    duration = time.monotonic() - start
                    return CycleResult(
                        exit_code=-1,
                        duration_seconds=duration,
                        stdout="",
                        stderr="Agent cycle timed out",
                        timed_out=True,
                    )

    @staticmethod
    def _read_cycle_type(shared_path: str) -> str:
        """Read the cycle type written by the agent during WAKE."""
        path = os.path.join(shared_path, "cycle_type.json")
        try:
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                return data.get("cycle_type", "").upper()
        except (OSError, json.JSONDecodeError):
            pass
        return ""

    @staticmethod
    def _write_stop_flag(shared_path: str) -> None:
        """Write the stop flag file for the agent to detect."""
        flag = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": "timeout",
        }
        path = os.path.join(shared_path, "stop_flag.json")
        with open(path, "w") as f:
            json.dump(flag, f)

    @staticmethod
    def _check_ping(shared_path: str) -> bool:
        """Check if the agent has written a ping response."""
        return os.path.exists(os.path.join(shared_path, "stop_ping.json"))

    @staticmethod
    def _remove_file(shared_path: str, filename: str) -> None:
        """Remove a file if it exists."""
        path = os.path.join(shared_path, filename)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    @staticmethod
    def cleanup_signals(shared_path: str) -> None:
        """Remove stale signal files from a previous cycle."""
        for name in ("stop_flag.json", "stop_ping.json", "cycle_type.json"):
            path = os.path.join(shared_path, name)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    async def run_cycle_subprocess(
        self,
        timeout_seconds: int = 300,
    ) -> CycleResult:
        """
        Run the agent as a Python subprocess (for development).

        Uses the same Python interpreter as the supervisor.
        """
        cmd = [sys.executable, "-m", "legba.agent.main"]
        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
                duration = time.monotonic() - start

                return CycleResult(
                    exit_code=proc.returncode or 0,
                    duration_seconds=duration,
                    stdout=stdout_bytes.decode("utf-8", errors="replace"),
                    stderr=stderr_bytes.decode("utf-8", errors="replace"),
                )

            except asyncio.TimeoutError:
                proc.kill()
                duration = time.monotonic() - start
                return CycleResult(
                    exit_code=-1,
                    duration_seconds=duration,
                    stdout="",
                    stderr="Agent cycle timed out",
                    timed_out=True,
                )

        except Exception as e:
            duration = time.monotonic() - start
            return CycleResult(
                exit_code=-1,
                duration_seconds=duration,
                stdout="",
                stderr=f"Failed to launch agent: {e}",
            )

    async def kill_agent(self) -> None:
        """Force-kill the agent container."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "kill", self.container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except Exception:
            pass  # Container may already be dead

    async def is_agent_running(self) -> bool:
        """Check if the agent container is currently running."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "inspect", "--format", "{{.State.Running}}",
                self.container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode().strip() == "true"
        except Exception:
            return False
