# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/fleet_stop.sh and scripts/fleet_start.sh, the one-command
halt and resume of the two model-calling processes.

Every run here is ``--dry-run``, through the REAL scripts, with a ``docker``
and a ``touch``/``rm`` on PATH that record any call and fail — so the tests
prove both the printed plan and that a dry run touches nothing (no compose
call, no flag file under /etc). ``bash -n`` covers the syntax of both.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
STOP = REPO / "scripts" / "fleet_stop.sh"
START = REPO / "scripts" / "fleet_start.sh"

FLAG = "/etc/legba-watchdog.disabled"
DCP = (
    "env COMPOSE_PROFILES=runtime,dapr-workflow docker compose -p legba "
    "--env-file .env -f docker-compose.yml"
)


@pytest.fixture
def trap_path(tmp_path):
    """A PATH whose docker / touch / rm log the call and exit 1."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("docker", "touch", "rm"):
        stub = bindir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 1\n')
        stub.chmod(0o755)
    return bindir, log


def _run(script, *args, trap_path):
    bindir, log = trap_path
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}
    res = subprocess.run(["bash", str(script), *args], env=env,
                         capture_output=True, text=True, timeout=60)
    calls = log.read_text() if log.exists() else ""
    return res, calls


def _commands(stdout: str) -> list[str]:
    return [ln[2:] for ln in stdout.splitlines() if ln.startswith("$ ")]


@pytest.mark.parametrize("script", [STOP, START], ids=lambda p: p.name)
def test_bash_syntax(script):
    res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert os.access(script, os.X_OK)
    assert "set -euo pipefail" in script.read_text()


def test_stop_dry_run_prints_the_plan_and_touches_nothing(trap_path):
    res, calls = _run(STOP, "--dry-run", trap_path=trap_path)
    assert res.returncode == 0, res.stderr
    assert _commands(res.stdout) == [
        f"touch {FLAG}",
        f"{DCP} stop legba-runtime-dapr legba-dapr-workflow-worker",
    ]
    assert "STOPPED legba-runtime-dapr legba-dapr-workflow-worker" in res.stdout
    assert "scripts/fleet_start.sh --resume-watchdog" in res.stdout
    assert calls == ""


def test_start_dry_run_default_keeps_the_watchdog_off(trap_path):
    res, calls = _run(START, "--dry-run", trap_path=trap_path)
    assert res.returncode == 0, res.stderr
    assert _commands(res.stdout) == [
        f"{DCP} up -d --no-deps legba-runtime-dapr",
        f"{DCP} up -d --no-deps legba-dapr-workflow-worker",
    ]
    out = res.stdout
    # the runtime is waited on BEFORE the worker starts
    assert out.index("legba-runtime-dapr' for Health=healthy") < out.index(
        "up -d --no-deps legba-dapr-workflow-worker")
    assert f"rm -f {FLAG}" in out and "REMINDER" in out
    assert calls == ""


def test_start_dry_run_recreate_and_resume_watchdog(trap_path):
    res, calls = _run(START, "--dry-run", "--recreate", "--resume-watchdog",
                      trap_path=trap_path)
    assert res.returncode == 0, res.stderr
    assert _commands(res.stdout) == [
        f"{DCP} up -d --force-recreate --no-deps legba-runtime-dapr",
        f"{DCP} up -d --force-recreate --no-deps legba-dapr-workflow-worker",
        f"rm -f {FLAG}",
    ]
    assert "REMINDER" not in res.stdout
    assert calls == ""


def test_env_file_is_passed_by_path(trap_path):
    res, _ = _run(START, "--dry-run", "--env-file", "/srv/alt.env", trap_path=trap_path)
    assert res.returncode == 0, res.stderr
    assert all("--env-file /srv/alt.env" in c for c in _commands(res.stdout)[:2])


@pytest.mark.parametrize("script", [STOP, START], ids=lambda p: p.name)
def test_unknown_argument_is_refused(script, trap_path):
    res, calls = _run(script, "--bogus", trap_path=trap_path)
    assert res.returncode == 1
    assert "unknown argument" in res.stderr
    assert calls == ""
