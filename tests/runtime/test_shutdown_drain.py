# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F84 — the bounded drain of the actor host on a rolling deploy.

uvicorn's graceful shutdown would drain the in-flight requests (each an actor
turn), but ``dapr_host.main``'s own loop-level signal handler stopped the loop
at the same signal and abandoned them. Proven here:

  * THE DEFECT, reproduced on a real event loop (SIGUSR1 stands in for
    SIGTERM, and a ``signal.signal`` override stands in for uvicorn's
    ``capture_signals``): with no drain configured the loop handler still
    fires and the in-flight work is abandoned; with a drain it completes;
  * the handler's decision — no drain: stop at once; drain: arm the backstop
    only, and a second signal stops at once;
  * ``runtime_drain_seconds`` — unset / 0 / junk is ``None`` (no drain,
    byte-identical), values clamp at 60;
  * ``dapr_host.main`` wires both (source-level: main() starts a server, so
    it is not called here);
  * ``scripts/rolling_deploy.sh --drain-seconds N`` refuses a value outside
    0-60 before anything runs, and under ``--dry-run`` announces the runtime
    recreate with ``--timeout N`` (and without it when the flag is absent).
    ``docker`` and ``git`` are STUBS on PATH: no container, no repository, no
    daemon is touched.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from legba.runtime import shutdown_drain as SD

ROOT = Path(__file__).resolve().parents[2]
ROLL = ROOT / "scripts" / "rolling_deploy.sh"


@pytest.mark.parametrize("raw, expected", [
    (None, None), ("", None), ("0", None), ("-5", None), ("soon", None),
    ("45", 45), ("60", 60), ("600", 60),
])
def test_runtime_drain_seconds(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(SD.DRAIN_SECONDS_ENV, raising=False)
    else:
        monkeypatch.setenv(SD.DRAIN_SECONDS_ENV, raw)
    assert SD.runtime_drain_seconds() == expected


def test_the_host_wires_the_bound_into_uvicorn_and_the_handler():
    src = (ROOT / "src" / "legba" / "runtime" / "dapr_host.py").read_text()
    assert "drain_seconds = runtime_drain_seconds()" in src
    assert "timeout_graceful_shutdown=drain_seconds" in src
    assert "install_shutdown_signal_handlers(loop, drain_seconds=drain_seconds)" in src
    # The old unconditional loop.stop() handler is gone.
    assert "def _signal_handler" not in src


class _FakeLoop:
    def __init__(self):
        self.calls: list[tuple] = []

    def call_soon_threadsafe(self, fn):
        self.calls.append(("soon", fn.__name__))

    def call_later(self, delay, fn):
        self.calls.append(("later", delay, fn.__name__))

    def stop(self):  # pragma: no cover — only referenced
        pass


def test_no_drain_stops_at_the_first_signal():
    loop = _FakeLoop()
    handler = SD.install_shutdown_signal_handlers(loop, drain_seconds=None, signals=())
    handler()
    assert loop.calls == [("soon", "stop")]


def test_a_drain_arms_only_the_backstop_and_a_second_signal_stops_at_once():
    loop = _FakeLoop()
    handler = SD.install_shutdown_signal_handlers(loop, drain_seconds=40, signals=())
    handler()
    assert loop.calls == [("later", 40 + SD.BACKSTOP_MARGIN_S, "stop")]
    handler()
    assert loop.calls[-1] == ("soon", "stop")


def _simulate_uvicorn_shutdown(drain_seconds):
    """A real loop, the real handler on SIGUSR1, and uvicorn's capture_signals
    (``signal.signal`` over the loop's handler) around an in-flight request."""
    events: list[str] = []
    loop = asyncio.new_event_loop()
    sig = signal.SIGUSR1
    try:
        SD.install_shutdown_signal_handlers(loop, drain_seconds=drain_seconds, signals=(sig,))

        async def serve():
            original = signal.signal(sig, lambda *a: events.append("uvicorn.handle_exit"))
            try:
                os.kill(os.getpid(), sig)
                for _ in range(10):  # the in-flight actor turn
                    await asyncio.sleep(0.02)
                events.append("in_flight_completed")
            finally:
                signal.signal(sig, original)

        try:
            loop.run_until_complete(serve())
            events.append("served")
        except RuntimeError:
            events.append("loop_stopped")
    finally:
        loop.remove_signal_handler(sig)
        loop.close()
    return events


def test_without_a_drain_the_loop_handler_abandons_the_in_flight_turn():
    # The pre-fix behaviour, kept as the default: both handlers fire.
    events = _simulate_uvicorn_shutdown(None)
    assert "uvicorn.handle_exit" in events
    assert events[-1] == "loop_stopped" and "in_flight_completed" not in events


def test_with_a_drain_the_in_flight_turn_completes():
    events = _simulate_uvicorn_shutdown(30)
    assert events == ["uvicorn.handle_exit", "in_flight_completed", "served"]


def _stub_bin(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in *rev-parse*) echo " + "b" * 40 + "; exit 0 ;; esac\n"
        "exit 1\n"
    )
    # Only the read-only image-age guard calls docker under --dry-run; answer it
    # with a fresh creation time, and refuse anything else loudly.
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = inspect ]; then date -u +%Y-%m-%dT%H:%M:%SZ; exit 0; fi\n"
        "echo \"stub docker: unscripted call: $*\" >&2; exit 2\n"
    )
    for f in ("git", "docker"):
        (bindir / f).chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}


def _roll(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(ROLL, repo / "scripts" / "rolling_deploy.sh")
    args = ["bash", str(repo / "scripts" / "rolling_deploy.sh"), "--dry-run", "--skip-build",
            "--nightly-log", str(tmp_path / "none.log"), *extra]
    return subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=120,
                          env=_stub_bin(tmp_path))


@pytest.mark.parametrize("value, needle", [
    ("61", "at most 60"), ("abc", "must be an integer"), ("-3", "must be an integer"),
])
def test_deploy_refuses_an_out_of_bound_drain(tmp_path, value, needle):
    proc = _roll(tmp_path, "--drain-seconds", value)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert needle in proc.stdout + proc.stderr


def test_deploy_dry_run_recreates_the_runtime_with_the_drain_timeout(tmp_path):
    proc = _roll(tmp_path, "--drain-seconds=45")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "drain-seconds=45" in out
    assert "--force-recreate --timeout 45 legba-runtime-dapr" in out
    # The roll reads the RUNNING container's drain bound (announced under dry-run).
    assert "would read LEGBA_RUNTIME_DRAIN_SECONDS from the running legba-runtime-dapr" in out
    # Only the runtime drains: the registry and worker recreates are unchanged.
    assert "--force-recreate legba-registry" in out
    assert "--force-recreate legba-dapr-workflow-worker" in out


def test_deploy_without_the_flag_is_unchanged(tmp_path):
    proc = _roll(tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "--force-recreate legba-runtime-dapr" in out
    assert "--timeout" not in out
