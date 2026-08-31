# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`scripts/host_llm_heartbeat.sh` — the COMPLETION/LONG-CONTEXT probe's
failure classification, and the container-target regression.

THE DEFECT THIS PINS. From introduction (commit `22277b43`, 2026-08-02)
through 2026-08-29, `APP_CONTAINER` defaulted to `legba-legba-registry-1`,
which does not carry `feedparser` (a transitive import of the probe's own
dependency chain: `build_llm_handler_from_stack_component` ->
`legba.data.analysts` -> `evidence_archiver` -> `sources/rss.py` ->
`feedparser`). That import lived OUTSIDE the probe's own try/except, so a
`ModuleNotFoundError` propagated out of `main()` entirely, printed nothing
matching `^(OK|FAIL) `, and the shell side reported

    FAIL mode=short reason=no_probe_output (container legba-legba-registry-1
    unreachable?)

474 times in the 08-27..08-29 forensics window alone (4,331 total since
08-04) — every single tick, always false: the container was
`Up ... (healthy)` throughout, and the one leading-signal check built to
catch a repeat of the 2026-07-29 9h silent-completions outage never once
actually ran.

Two independent fixes, two independent tests:

  1. The python probe body now wraps its OWN imports in the try/except, so
     an import failure prints a classified `FAIL ... reason=probe_broken:...`
     line instead of vanishing. `test_import_failure_inside_probe_reports_
     probe_broken_not_silence` drives the REAL embedded probe body (extracted
     from the live script, not a re-implementation) with `feedparser`
     poisoned via `sys.modules`, hermetically — no docker, no postgres, no
     LLM endpoint required.
  2. `APP_CONTAINER` now defaults to `legba-legba-runtime-dapr-1` (the actor
     host that actually carries the full `legba` package — confirmed via
     `docker/Dockerfile.runtime` installing `feedparser>=6.0` and
     `docker/Dockerfile.registry` deliberately not). The end-to-end bash
     tests below drive the REAL script through a stub `docker` binary on
     PATH (same technique as `test_host_nightly_suite_classifier.py`'s stub
     `run_tests_in_container.sh`) to pin BOTH failure classes staying
     distinct: `probe_broken` (docker exec ran, container is up, but the
     probe itself produced no verdict) must never read as
     `container_unreachable`, and vice versa.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "host_llm_heartbeat.sh"


@pytest.fixture(autouse=True)
def _require_bash_tools():
    for tool in ("bash",):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not available")
    if not SCRIPT.is_file():
        pytest.skip(f"missing {SCRIPT}")


def _extract_embedded_probe() -> str:
    """Pull the python heredoc body OUT of the live script — traversing the
    real binding path rather than trusting a copy that could drift from it."""
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"<<'PY'[^\n]*\n(.*?)\nPY\n", src, re.S)
    assert m is not None, "host_llm_heartbeat.sh's python heredoc went missing or was re-shaped"
    return m.group(1)


def test_app_container_no_longer_defaults_to_the_module_light_registry_image():
    """Regression pin for the container retarget. legba-legba-registry-1
    (Dockerfile.registry) never carries feedparser/trafilatura/aiobotocore;
    legba-legba-runtime-dapr-1 (Dockerfile.runtime) does — confirmed live via
    `docker exec <container> python3 -c "import feedparser"` against both."""
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'APP_CONTAINER="\$\{APP_CONTAINER:-([^}]+)\}"', src)
    assert m is not None, "APP_CONTAINER default declaration not found"
    default = m.group(1)
    assert default == "legba-legba-runtime-dapr-1", (
        f"APP_CONTAINER default is {default!r} — it must be a container "
        "whose image carries the full legba package (feedparser included), "
        "not the lighter registry control-plane image"
    )


def test_import_failure_inside_probe_reports_probe_broken_not_silence():
    """THE CORE REGRESSION. Drives the REAL probe body (extracted straight
    from the script) with `feedparser` poisoned via `sys.modules`, simulating
    the exact 2026-08-02..29 defect (a missing transitive import) without
    needing docker, postgres, or a live LLM endpoint. Before the fix, this
    raised ModuleNotFoundError OUTSIDE any try/except and printed nothing
    matching `^(OK|FAIL) ` at all — the silent-crash shape that got
    misreported as "container unreachable?" for 27 days. After the fix, it
    must print a classified FAIL line."""
    probe_src = _extract_embedded_probe()

    with tempfile.TemporaryDirectory() as td:
        probe_path = Path(td) / "probe.py"
        probe_path.write_text(probe_src, encoding="utf-8")

        runner_path = Path(td) / "run_poisoned.py"
        runner_path.write_text(
            "import sys\n"
            "sys.modules['feedparser'] = None\n"
            f"src = open({str(probe_path)!r}).read()\n"
            "exec(compile(src, '<probe>', 'exec'), {'__name__': '__main__'})\n",
            encoding="utf-8",
        )

        env = {**os.environ, "PROBE_MODE": "short", "PROBE_TIMEOUT": "5"}
        proc = subprocess.run(
            ["python3", str(runner_path)],
            env=env, capture_output=True, text=True, timeout=30,
        )

    assert proc.returncode == 1, (
        f"expected the probe's own FAIL-and-return-1 path, got rc={proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    out_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    assert out_line.startswith("FAIL "), (
        "an import failure produced NO classified verdict line — this is "
        f"exactly the silent-crash shape the 27-day defect hid behind\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "reason=probe_broken:" in out_line, (
        f"import failure must be labeled distinctly as probe_broken, got: {out_line!r}"
    )
    assert "ModuleNotFoundError" in out_line
    assert "unreachable" not in out_line.lower(), (
        "an import failure inside the probe must never read as a reachability problem"
    )


_STUB_DOCKER = r"""#!/usr/bin/env bash
set -u
CALLS_LOG="${STUB_DOCKER_CALLS_LOG:-/dev/null}"
sub="${1:-}"
printf '%s\n' "$*" >> "$CALLS_LOG"
case "$sub" in
  exec)
    shift
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -i) shift ;;
        -e) shift 2 ;;
        *) break ;;
      esac
    done
    container="${1:-}"; shift || true
    cmd="${1:-}"; shift || true
    case "$cmd" in
      psql)
        echo "${STUB_PSQL_AGE:-60}"
        exit "${STUB_PSQL_EXIT:-0}"
        ;;
      python3)
        cat > /dev/null   # drain the heredoc so the caller never blocks
        case "${STUB_PY_MODE:-ok}" in
          ok)
            echo "OK mode=short model=stub latency=0.1s chars=10 reply_chars=4 sentinel=hit"
            exit 0 ;;
          silent_ok)
            exit 0 ;;
          silent_fail)
            echo "unrelated stderr noise, no OK/FAIL line" >&2
            exit 1 ;;
        esac
        ;;
      *) exit 0 ;;
    esac
    ;;
  inspect)
    if [ "${STUB_CONTAINER_RUNNING:-true}" = "true" ]; then
      echo "true"; exit 0
    else
      echo "Error: No such object: stub" >&2
      exit 1
    fi
    ;;
  *) exit 0 ;;
esac
"""


def _write_stub_docker(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(_STUB_DOCKER, encoding="utf-8")
    docker.chmod(0o755)


def _run_heartbeat(tmp_path: Path, extra_env: dict) -> tuple[str, str]:
    """Drive the REAL script end-to-end with a stub `docker` on PATH.
    Returns (log_text, docker_calls_text)."""
    bin_dir = tmp_path / "bin"
    _write_stub_docker(bin_dir)
    log_path = tmp_path / "watchdog.log"
    calls_log = tmp_path / "docker_calls.log"

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "LOG": str(log_path),
        "NTFY_URL": "http://127.0.0.1:9/none",  # refused fast, no hang
        "PROBE_ENABLED": "1",
        "LONGCTX_ENABLED": "0",  # isolate to the COMPLETION check only
        "COOLDOWN_STAMP": str(tmp_path / "silence.cooldown"),
        "SHORT_COOLDOWN_STAMP": str(tmp_path / "completion.cooldown"),
        "LONGCTX_COOLDOWN_STAMP": str(tmp_path / "longctx.cooldown"),
        "TICK_COUNTER": str(tmp_path / "tick"),
        "STUB_DOCKER_CALLS_LOG": str(calls_log),
        **extra_env,
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60,
    )
    log_text = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
    calls_text = calls_log.read_text(encoding="utf-8") if calls_log.is_file() else ""
    assert proc.returncode == 0, (
        f"host_llm_heartbeat.sh itself must always exit 0 (alert-only design)\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}\nlog={log_text!r}"
    )
    return log_text, calls_text


def test_default_container_used_is_the_runtime_not_registry(tmp_path: Path):
    """End-to-end: with APP_CONTAINER left UNSET, the script must exec into
    the runtime container, never the registry one, and the probe must
    succeed cleanly through the stub."""
    log, calls = _run_heartbeat(tmp_path, {"STUB_PY_MODE": "ok"})
    assert "legba-legba-runtime-dapr-1" in calls
    assert "legba-legba-registry-1" not in calls
    assert "FIRE completion probe failed" not in log, log
    assert "probe.completion OK" in log, log


def test_probe_broken_and_container_unreachable_stay_distinct(tmp_path: Path):
    """The two failure classes must never collapse into each other."""
    # Case A: docker exec succeeds, container is genuinely up, but the probe
    # produced no OK/FAIL line at all — a probe bug, not a reachability
    # problem. This is the residual case the bash-side classifier exists for
    # now that the python side catches import errors itself.
    dir_a = tmp_path / "case_a"
    dir_a.mkdir()
    log_a, _ = _run_heartbeat(
        dir_a, {"STUB_PY_MODE": "silent_ok", "STUB_CONTAINER_RUNNING": "true"},
    )
    assert "reason=probe_broken" in log_a, log_a
    assert "container_unreachable" not in log_a, log_a

    # Case B: docker exec fails and the container genuinely is not running —
    # the ONLY case that may say "unreachable".
    dir_b = tmp_path / "case_b"
    dir_b.mkdir()
    log_b, _ = _run_heartbeat(
        dir_b, {"STUB_PY_MODE": "silent_fail", "STUB_CONTAINER_RUNNING": "false"},
    )
    assert "reason=container_unreachable" in log_b, log_b
    assert "probe_broken" not in log_b, log_b
