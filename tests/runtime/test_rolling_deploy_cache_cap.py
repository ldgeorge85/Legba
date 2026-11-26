# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/rolling_deploy.sh`` caps the build cache after a roll.

Each roll left ~8 GB of build cache behind (``--keep-storage 8GB``). The cap is now ``BUILD_CACHE_KEEP_GB`` (default
4, env ``LEGBA_BUILD_CACHE_KEEP_GB``), spelled ``--max-used-space`` (the
ceiling; ``--reserved-space`` is a floor and reclaimed 0 B).
Driven under ``--dry-run --skip-build`` with ``docker`` and ``git`` STUBS on
PATH: no container, repository or daemon is touched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROLL = ROOT / "scripts" / "rolling_deploy.sh"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in *rev-parse*) echo " + "c" * 40 + "; exit 0 ;; esac\n"
        "exit 1\n"
    )
    # Under --dry-run only the read-only image-age guard may call docker.
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = inspect ]; then date -u +%Y-%m-%dT%H:%M:%SZ; exit 0; fi\n"
        "echo \"stub docker: unscripted call: $*\" >&2; exit 2\n"
    )
    for f in ("git", "docker"):
        (bindir / f).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "LEGBA_BUILD_CACHE_KEEP_GB"}
    return {**env, "PATH": f"{bindir}{os.pathsep}{env.get('PATH', '')}", **extra}


def _roll(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(ROLL, repo / "scripts" / "rolling_deploy.sh")
    args = ["bash", str(repo / "scripts" / "rolling_deploy.sh"), "--dry-run", "--skip-build",
            "--nightly-log", str(tmp_path / "none.log")]
    return subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=120,
                          env=_env(tmp_path, **env))


def test_the_default_cap_is_4gb_and_runs_last(tmp_path):
    proc = _roll(tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "$ docker builder prune -f --max-used-space 4GB" in out
    assert "--keep-storage 8GB" not in out
    assert "unscripted call" not in out  # nothing but the guard touched docker
    # After every recreate and verification step: the cap is the roll's last act.
    assert out.index("builder prune") > out.index("--force-recreate legba-dapr-workflow-worker")
    assert out.index("builder prune") < out.index("rolling deploy done")


def test_the_cap_is_tunable(tmp_path):
    proc = _roll(tmp_path, LEGBA_BUILD_CACHE_KEEP_GB="6")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "$ docker builder prune -f --max-used-space 6GB" in out


@pytest.mark.parametrize("bad", ["", "4.5", "lots", "-1"])
def test_a_bad_cap_refuses_before_anything_runs(tmp_path, bad):
    proc = _roll(tmp_path, LEGBA_BUILD_CACHE_KEEP_GB=bad)
    out = proc.stdout + proc.stderr
    if bad == "":
        # Empty = unset: the default applies.
        assert proc.returncode == 0, out
        assert "--max-used-space 4GB" in out
        return
    assert proc.returncode == 1, out
    assert "LEGBA_BUILD_CACHE_KEEP_GB must be a whole number" in out
    assert "builder prune" not in out


def test_the_cap_never_prunes_images():
    text = ROLL.read_text()
    assert "docker builder prune -f \"${flag}\"" in text
    for forbidden in ("image prune", "system prune", "builder prune -a", "builder prune --all"):
        assert forbidden not in text
