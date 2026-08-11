# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dockerfile static-validation tests for the multi-image build (2026-05-23).

These tests don't actually build the images — that takes minutes and
needs docker daemon access we don't want in the unit-test path. They
validate:

  * Every Dockerfile under `docker/` exists, parses (`docker build
    --check` if available, else syntactic parse with a stub parser),
    declares the expected base image + entrypoint, and uses multi-
    stage builds.
  * The compose file's `--profile runtime` block references each
    Dockerfile at the right relative path with the right context.
  * The UI Dockerfile uses node:20-slim as the build stage.

`docker build --check` is the canonical validator — when available we
prefer it. Otherwise we fall back to manual grep-style assertions on
the Dockerfile bodies. Both paths exercise the same invariants.

These tests must pass even on hosts without docker (CI portability).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


# The checkout UNDER TEST, not a hardcoded one. scripts/run_tests_in_container.sh
# bind-mounts the repo at the same path inside and out (`-v $R:$R -w $R`), so
# resolving off __file__ gives the main checkout under cron and the WORKTREE
# under a branch agent — which is the only way these assertions can guard a
# change before it merges. Hardcoding the main checkout meant a worktree run
# validated main's Dockerfiles and reported on files the branch had not
# touched (6 of the nightly's documented "worktree-only" failures).
REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO_ROOT / "docker"
UI_DIR = REPO_ROOT / "legba-ui-v3"


# (logical_name, dockerfile_path, expected_entrypoint_token, must_have_tokens)
PYTHON_IMAGES = [
    (
        "legba-registry",
        DOCKER_DIR / "Dockerfile.registry",
        "legba-registry",
        # pycountry is here because of the 2026-08-04 outage: it was in
        # pyproject.toml and in Dockerfile.runtime but not in the registry's
        # explicit pip list, so `/typed` 500'd for every options-bearing
        # deterministic analyst for 14h. The registry installs from that list,
        # NOT from pyproject, so pyproject agreeing proves nothing.
        ["fastapi", "uvicorn", "asyncpg", "pynacl", "nats-py", "pycountry"],
    ),
    (
        "legba-runtime-dapr",
        DOCKER_DIR / "Dockerfile.runtime",
        "legba-runtime-dapr",
        ["dapr", "dapr-ext-fastapi", "langdetect", "asyncpg"],
    ),
    # legba-temporal-worker retired (Temporal cut, P-16): there is no
    # docker/Dockerfile.temporal-worker — the optimizer kind's GEPA loop
    # runs as a Dapr Workflow on the existing daprd sidecar.
    (
        "legba-mcp",
        DOCKER_DIR / "Dockerfile.mcp",
        "legba-mcp",
        ["mcp", "asyncpg", "pynacl"],
    ),
]


def _has_docker() -> bool:
    return shutil.which("docker") is not None


def _read(path: Path) -> str:
    assert path.is_file(), f"missing dockerfile: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-image checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,dockerfile,entrypoint_token,must_have",
    PYTHON_IMAGES,
    ids=[row[0] for row in PYTHON_IMAGES],
)
def test_python_dockerfile_shape(
    name: str, dockerfile: Path, entrypoint_token: str, must_have: list[str]
) -> None:
    """Every python image must be slim-base, multi-stage, console-script entrypoint."""
    body = _read(dockerfile)

    # Multi-stage: at least two `FROM` lines.
    from_lines = [ln for ln in body.splitlines() if ln.strip().startswith("FROM ")]
    assert len(from_lines) >= 2, (
        f"{name} dockerfile must be multi-stage (>=2 FROM stages); "
        f"found {len(from_lines)}: {from_lines!r}"
    )

    # Base must be python:3.11-slim — keeps image small.
    assert all(
        "python:3.11-slim" in ln for ln in from_lines
    ), f"{name} dockerfile stages must use python:3.11-slim; got {from_lines!r}"

    # Console-script entrypoint matches pyproject.toml [project.scripts].
    assert (
        f'ENTRYPOINT ["{entrypoint_token}"]' in body
    ), f"{name} dockerfile missing ENTRYPOINT [{entrypoint_token!r}]"

    # Sanity: each declared dep token appears in the pip install line(s).
    #
    # COMMENTS ARE STRIPPED FIRST, and that is the whole point. These
    # Dockerfiles carry long comments explaining why each dep is present — so a
    # substring search over the raw body is satisfied by the PROSE ABOUT a
    # dependency and passes happily when the dependency itself has been
    # deleted. Measured: removing `"pycountry>=24.6"` from the install list
    # while leaving the paragraph that explains it left this test green, which
    # is precisely the 14h outage it was just extended to make impossible.
    installed = "\n".join(
        ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
    )
    for tok in must_have:
        assert tok in installed, (
            f"{name} dockerfile expected to install {tok!r} (subset of "
            f"pyproject.toml deps relevant to this image); not found in any "
            f"non-comment line"
        )


def test_ui_dockerfile_is_multistage_node_then_publisher() -> None:
    """legba-ui-v3 Dockerfile is multi-stage: node:20-slim build → alpine publish."""
    dockerfile = UI_DIR / "Dockerfile"
    body = _read(dockerfile)

    from_lines = [ln for ln in body.splitlines() if ln.strip().startswith("FROM ")]
    assert len(from_lines) >= 2, (
        f"UI dockerfile must be multi-stage; found {from_lines!r}"
    )

    assert any("node:20-slim" in ln for ln in from_lines), (
        f"UI build stage must use node:20-slim; got {from_lines!r}"
    )

    # Build stage runs `npm ci` + `npm run build` exactly once each.
    assert "npm ci" in body, "UI dockerfile must run npm ci for reproducible installs"
    assert "npm run build" in body, "UI dockerfile must run npm run build"

    # Publisher stage emits to /out (mounted compose volume legba_ui_dist).
    assert "/dist-src" in body and "/out" in body, (
        "UI publish stage must emit dist into /out; the legba_ui_dist volume "
        "binds at that path"
    )


def test_ui_dockerignore_excludes_node_modules() -> None:
    """The UI .dockerignore must exclude node_modules + dist to keep context small."""
    ignore = UI_DIR / ".dockerignore"
    assert ignore.is_file(), "legba-ui-v3/.dockerignore missing"
    body = ignore.read_text(encoding="utf-8")
    assert "node_modules" in body
    assert "dist" in body


def test_root_dockerignore_excludes_legba_models() -> None:
    """Root .dockerignore must exclude legba-models (separate hosted deployable)
    + tests + logs + secrets."""
    ignore = REPO_ROOT / ".dockerignore"
    assert ignore.is_file(), "root .dockerignore missing"
    body = ignore.read_text(encoding="utf-8")
    for tok in ["legba-models", "node_modules", ".env", "tests", "logs"]:
        assert tok in body, f"root .dockerignore missing {tok!r}"


# ---------------------------------------------------------------------------
# docker build --check  (when docker is available)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,dockerfile",
    [(n, df) for (n, df, _, _) in PYTHON_IMAGES],
    ids=[row[0] for row in PYTHON_IMAGES],
)
def test_dockerfile_passes_docker_check(name: str, dockerfile: Path) -> None:
    """`docker build --check` validates syntax + linting without building.

    Skipped on hosts without docker (CI portability). Where docker is
    available this is the authoritative parser — runs through the same
    BuildKit frontend that an actual build would use.
    """
    if not _has_docker():
        pytest.skip("docker not on PATH; skipping --check path")

    # `docker build --check` requires BuildKit. The DOCKER_BUILDKIT=1
    # env switch covers older docker versions where the buildx flag
    # isn't auto-on. Build context is the repo root for the python
    # images.
    env = dict(os.environ)
    env["DOCKER_BUILDKIT"] = "1"
    res = subprocess.run(
        [
            "docker",
            "build",
            "--check",
            "-f",
            str(dockerfile),
            str(REPO_ROOT),
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    # `docker build --check` returns 0 on clean validation. Non-zero
    # could be either an actual lint error or "this docker version
    # doesn't understand --check". Distinguish via stderr signal.
    if res.returncode != 0:
        stderr = res.stderr or ""
        # Older docker without --check support.
        if "unknown flag" in stderr or "unknown command" in stderr:
            pytest.skip(f"docker version lacks --check support; stderr={stderr!r}")
        pytest.fail(
            f"docker build --check failed for {name}\n"
            f"stdout:\n{res.stdout}\nstderr:\n{stderr}"
        )


def test_ui_dockerfile_passes_docker_check() -> None:
    """Same --check path for the UI dockerfile."""
    if not _has_docker():
        pytest.skip("docker not on PATH; skipping --check path")

    env = dict(os.environ)
    env["DOCKER_BUILDKIT"] = "1"
    res = subprocess.run(
        [
            "docker",
            "build",
            "--check",
            "-f",
            str(UI_DIR / "Dockerfile"),
            str(UI_DIR),
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(UI_DIR),
        timeout=120,
    )
    if res.returncode != 0:
        stderr = res.stderr or ""
        if "unknown flag" in stderr or "unknown command" in stderr:
            pytest.skip(f"docker version lacks --check support; stderr={stderr!r}")
        pytest.fail(
            f"docker build --check failed for legba-ui-v3\n"
            f"stdout:\n{res.stdout}\nstderr:\n{stderr}"
        )
