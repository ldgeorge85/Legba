"""
Structured JSON logging for Legba.

All log entries written as JSON lines to the drain volume.
The supervisor collects these from the host side.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class CycleLogger:
    """
    Structured logger that writes JSON lines to the log drain volume.

    One logger per cycle. Captures everything: LLM calls, tool executions,
    memory operations, errors, and cycle state transitions.
    """

    def __init__(self, log_dir: str = "/logs", cycle_number: int = 0):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._cycle_number = cycle_number
        self._entries: list[dict[str, Any]] = []

        # Per-cycle log file
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._log_file = self._log_dir / f"cycle_{cycle_number:06d}_{ts}.jsonl"
        self._file_handle = open(self._log_file, "a")

    def update_cycle_number(self, cycle_number: int) -> None:
        """Update cycle number and rename the log file to match.

        Called once per cycle after the challenge is read and the real
        cycle number is known (the logger is created before the challenge
        is parsed, so the initial file is named with cycle 0).
        """
        if cycle_number == self._cycle_number:
            return
        self._cycle_number = cycle_number
        # Build new filename preserving the original timestamp suffix
        # Current name: cycle_000000_20260218T191612Z.jsonl
        parts = self._log_file.name.split("_", 2)  # ['cycle', '000000', '20260218T...jsonl']
        new_name = f"cycle_{cycle_number:06d}_{parts[2]}"
        new_path = self._log_dir / new_name
        self._file_handle.flush()
        self._file_handle.close()
        self._log_file.rename(new_path)
        self._log_file = new_path
        self._file_handle = open(self._log_file, "a")

    def log(self, event: str, **data: Any) -> None:
        """Write a structured log entry."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cycle": self._cycle_number,
            "event": event,
            **data,
        }
        self._entries.append(entry)
        self._write(entry)

    def log_llm_call(
        self,
        purpose: str,
        prompt: str,
        response: str,
        finish_reason: str,
        usage: dict[str, int],
        latency_ms: float,
        tool_calls: list[dict] | None = None,
        error: str | None = None,
        provider: str | None = None,
    ) -> None:
        """Log a complete LLM call with full prompt/response."""
        self.log(
            "llm_call",
            purpose=purpose,
            prompt=prompt,
            response=response,
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=latency_ms,
            tool_calls=tool_calls or [],
            error=error,
            provider=provider,
        )

    def log_tool_call(
        self,
        tool_name: str,
        arguments: dict,
        result: Any = None,
        error: str | None = None,
        duration_ms: float = 0.0,
    ) -> None:
        """Log a tool execution."""
        self.log(
            "tool_call",
            tool_name=tool_name,
            arguments=arguments,
            result=str(result) if result is not None else None,
            error=error,
            duration_ms=duration_ms,
        )

    def log_phase(self, phase: str, **data: Any) -> None:
        """Log a cycle phase transition."""
        self.log("phase", phase=phase, **data)

    def log_error(self, error: str, **data: Any) -> None:
        """Log an error."""
        self.log("error", error=error, **data)
        # Also print to stderr for immediate visibility
        print(f"[ERROR] cycle={self._cycle_number} {error}", file=sys.stderr)

    def log_memory(self, operation: str, store: str, **data: Any) -> None:
        """Log a memory operation."""
        self.log("memory", operation=operation, store=store, **data)

    def log_self_mod(self, action: str, file_path: str, **data: Any) -> None:
        """Log a self-modification event."""
        self.log("self_modification", action=action, file_path=file_path, **data)

    def close(self) -> None:
        """Flush and close the log file."""
        if self._file_handle and not self._file_handle.closed:
            self._file_handle.flush()
            self._file_handle.close()

    def _write(self, entry: dict) -> None:
        """Write a single entry to the log file."""
        try:
            line = json.dumps(entry, default=str)
            self._file_handle.write(line + "\n")
            self._file_handle.flush()
        except Exception as e:
            print(f"[LOG ERROR] Failed to write log entry: {e}", file=sys.stderr)

    def __enter__(self) -> CycleLogger:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
