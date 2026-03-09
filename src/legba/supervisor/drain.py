"""
Log Drain

Collects and archives agent logs from the shared volume.
Logs are append-only from the agent's perspective; the supervisor
owns the storage and can organize/archive/rotate.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class LogDrain:
    """Collects agent logs from the drain volume."""

    def __init__(self, log_path: str = "/logs", archive_path: str = "/logs/archive"):
        self._log_path = Path(log_path)
        self._archive_path = Path(archive_path)
        self._log_path.mkdir(parents=True, exist_ok=True)
        self._archive_path.mkdir(parents=True, exist_ok=True)

    def get_cycle_logs(self, cycle_number: int) -> list[Path]:
        """Get all log files for a specific cycle."""
        pattern = f"cycle_{cycle_number:06d}_*.jsonl"
        return sorted(self._log_path.glob(pattern))

    def get_recent_logs(self, limit: int = 10) -> list[Path]:
        """Get the most recent log files."""
        all_logs = sorted(self._log_path.glob("cycle_*.jsonl"), reverse=True)
        return all_logs[:limit]

    def archive_cycle(self, cycle_number: int) -> Path | None:
        """Move a cycle's logs to the archive directory."""
        logs = self.get_cycle_logs(cycle_number)
        if not logs:
            return None

        cycle_dir = self._archive_path / f"cycle_{cycle_number:06d}"
        cycle_dir.mkdir(parents=True, exist_ok=True)

        for log_file in logs:
            shutil.move(str(log_file), str(cycle_dir / log_file.name))

        return cycle_dir

    def read_cycle_logs(self, cycle_number: int) -> list[dict[str, Any]]:
        """Read all log entries for a cycle as parsed dicts.

        Call this before archive_cycle() to get entries for audit indexing.
        """
        logs = self.get_cycle_logs(cycle_number)
        entries: list[dict[str, Any]] = []
        for log_file in logs:
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        return entries

    def get_total_log_size(self) -> int:
        """Get total size of all log files in bytes."""
        return sum(f.stat().st_size for f in self._log_path.glob("*.jsonl"))
