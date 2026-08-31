# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`scripts/host_log_collector.sh` — the per-service log-rotation budget.

THE DEFECT THIS PINS. The collector applied ONE global MAX_BYTES x (KEEP+1)
budget (32 MiB x 4 = 128 MiB) to every compose service. legba-runtime-dapr's
by-design high-volume reminder-GC existence-check logging (src/legba/runtime/
reminder_gc.py) outpaces that budget badly enough that retention measured
~9h live (2026-08-29), leaving 08-27..08-29T10:21Z unrecoverable for that one
service — see planning/CAMPAIGN_2026-08-29/LOG_FORENSICS.md and the fix's own
comment block above MAX_BYTES_OVERRIDE in the script for the live-measured
arithmetic (~17.3 MiB/hour -> a 360 MiB/file x 4 = 1440 MiB budget for
~83h retention, ~15% margin over the 72h target).

These tests drive the REAL functions (`rotate_if_needed`, `max_bytes_for`)
sourced straight out of the live script — not a re-implementation — so a
future edit that silently reverts the per-service override, or breaks the
rotation math, fails here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "host_log_collector.sh"


@pytest.fixture(autouse=True)
def _require_bash():
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    if not SCRIPT.is_file():
        pytest.skip(f"missing {SCRIPT}")


def _sourceable_lib() -> str:
    """Everything above the `# --- main` dispatch — function/variable
    definitions only, safe to source with no side effects."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("# --- main"):
            return "".join(lines[:i])
    raise AssertionError("`# --- main` marker not found in host_log_collector.sh")


def _run_bash(body: str, tmp_path: Path) -> subprocess.CompletedProcess:
    lib_path = tmp_path / "lib.sh"
    lib_path.write_text(_sourceable_lib(), encoding="utf-8")
    script_path = tmp_path / "test_body.sh"
    script_path.write_text(
        f"#!/usr/bin/env bash\nsource {lib_path}\n{body}\n", encoding="utf-8",
    )
    script_path.chmod(0o755)
    return subprocess.run(
        ["bash", str(script_path)], capture_output=True, text=True, timeout=30,
        cwd=str(tmp_path),
    )


def test_max_bytes_for_overrides_only_the_named_service(tmp_path: Path):
    proc = _run_bash(
        textwrap.dedent(
            """
            echo "runtime=$(max_bytes_for legba-runtime-dapr)"
            echo "registry=$(max_bytes_for legba-registry)"
            echo "arbitrary=$(max_bytes_for some-other-service)"
            echo "default=$MAX_BYTES"
            """
        ),
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    out = dict(line.split("=", 1) for line in proc.stdout.strip().splitlines())
    assert out["registry"] == out["default"] == "33554432"  # untouched: stock 32 MiB
    assert out["arbitrary"] == out["default"]
    runtime_override = int(out["runtime"])
    assert runtime_override > int(out["default"]), (
        "legba-runtime-dapr must get a LARGER budget than every other "
        "service, not the stock 32 MiB default"
    )
    # The 72h target at the measured ~17.3 MiB/hour rate needs roughly
    # 1247 MiB total (4 files); pin that the per-FILE override alone,
    # times the 4 kept files, clears that bar with room to spare.
    KEEP_PLUS_LIVE = 4
    assert runtime_override * KEEP_PLUS_LIVE >= 1247 * 1024 * 1024, (
        f"runtime override {runtime_override}B x {KEEP_PLUS_LIVE} files does not "
        "clear the ~1247 MiB (72h @ ~17.3 MiB/h) target"
    )


def test_rotate_if_needed_respects_the_override_not_the_global_default(tmp_path: Path):
    """A file sized BETWEEN the global default and the override must rotate
    under the global budget but NOT under the (larger) override — proving
    the per-call max_bytes argument is actually load-bearing, not decorative."""
    big_file = tmp_path / "legba-runtime-dapr.log"
    # 40 MiB: over the stock 32 MiB default, under the ~360 MiB override.
    big_file.write_bytes(b"x" * (40 * 1024 * 1024))

    proc = _run_bash(
        textwrap.dedent(
            f"""
            file="{big_file}"
            override="$(max_bytes_for legba-runtime-dapr)"
            rotate_if_needed "$file" "$override"
            if [ -f "${{file}}.1" ]; then
                echo "ROTATED_WITH_OVERRIDE=yes"
            else
                echo "ROTATED_WITH_OVERRIDE=no"
            fi
            rotate_if_needed "$file" "$MAX_BYTES"
            if [ -f "${{file}}.1" ]; then
                echo "ROTATED_WITH_DEFAULT=yes"
            else
                echo "ROTATED_WITH_DEFAULT=no"
            fi
            """
        ),
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    # rotate_if_needed's own log() call writes an unrelated "ROTATED ..."
    # line to stdout too (host_log_collector.sh's log() echoes directly,
    # rather than appending to a $LOG file the way the heartbeat's does) —
    # keep only our own KEY=VALUE marker lines.
    out = dict(
        line.split("=", 1) for line in proc.stdout.strip().splitlines()
        if re.match(r"^[A-Z_]+=", line)
    )
    assert out["ROTATED_WITH_OVERRIDE"] == "no", (
        "a 40 MiB file must NOT rotate under legba-runtime-dapr's ~360 MiB override"
    )
    assert out["ROTATED_WITH_DEFAULT"] == "yes", (
        "the same 40 MiB file MUST rotate once checked against the stock 32 MiB budget"
    )
