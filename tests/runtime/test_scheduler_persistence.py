# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for dapr-scheduler etcd-data persistence (Phase 5 hardening item 3).

The pre-hardening docker-compose mounted the scheduler's etcd dir as
tmpfs (RAM-only), which meant every container restart wiped all
registered Dapr Reminders. The hardening replaces that with a host bind
dir under ``./deploy/dapr-scheduler-data/`` chowned 65532:65532 by an
init container before the scheduler boots.

These tests verify the docker-compose shape (no docker daemon needed).
The full "spin up scheduler, register reminder, restart, confirm
survival" pass lives in the docker-side integration suite — it requires
the dapr profile up and is too heavyweight for the unit-test ladder.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"


def _load_compose() -> dict:
    with open(_COMPOSE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_dapr_scheduler_init_service_exists() -> None:
    """An init container chowns the host bind dir before the scheduler boots."""
    compose = _load_compose()
    services = compose["services"]
    assert "dapr-scheduler-init" in services, (
        "scheduler-init service must exist to chown the host bind dir "
        "for the daprio/dapr nonroot UID (65532)"
    )
    init = services["dapr-scheduler-init"]
    # Gated by the dapr profile (matches the scheduler's gating).
    assert "dapr" in init.get("profiles", []), (
        "scheduler-init must be on the dapr profile so it runs alongside "
        "the rest of the dapr stack"
    )
    # Command does mkdir + chown 65532:65532.
    command = init["command"]
    # The command may be a list or a single shell string; normalize.
    if isinstance(command, list):
        cmd_str = " ".join(str(c) for c in command)
    else:
        cmd_str = str(command)
    assert "65532:65532" in cmd_str, (
        f"init command must chown to the daprio/dapr nonroot UID; got: {cmd_str}"
    )
    assert "mkdir" in cmd_str, (
        f"init command must create the bind dir; got: {cmd_str}"
    )


def test_dapr_scheduler_uses_host_bind_not_tmpfs() -> None:
    """The scheduler service mounts a host bind dir, not tmpfs."""
    compose = _load_compose()
    services = compose["services"]
    assert "dapr-scheduler" in services
    scheduler = services["dapr-scheduler"]

    # No tmpfs mount — the hardening replaced it with a host bind.
    assert "tmpfs" not in scheduler, (
        "dapr-scheduler must NOT use tmpfs (it loses state on restart). "
        "Use a host bind dir chowned 65532:65532 instead."
    )

    # The volumes list contains a host bind dir under ./deploy/.
    volumes = scheduler.get("volumes", [])
    assert volumes, "dapr-scheduler must declare a volume mount for the etcd data dir"

    bind_found = False
    for v in volumes:
        # Volumes can be strings (short form) or dicts (long form).
        if isinstance(v, str):
            src, _, _ = v.partition(":")
            if src.startswith("./deploy/") or src.startswith("/"):
                bind_found = True
        elif isinstance(v, dict):
            if v.get("type") == "bind" and v.get("source"):
                bind_found = True
    assert bind_found, (
        f"dapr-scheduler must use a host bind for the etcd dir; volumes={volumes}"
    )


def test_dapr_scheduler_depends_on_init() -> None:
    """The scheduler must wait for the init container to succeed."""
    compose = _load_compose()
    scheduler = compose["services"]["dapr-scheduler"]
    deps = scheduler.get("depends_on", {})
    assert "dapr-scheduler-init" in deps, (
        "dapr-scheduler must depend on dapr-scheduler-init so the chown "
        "completes before the scheduler tries to write to the data dir"
    )
    init_dep = deps["dapr-scheduler-init"]
    # service_completed_successfully — the init container exits after
    # the chown, so the scheduler waits for completion (not just start).
    if isinstance(init_dep, dict):
        assert init_dep.get("condition") == "service_completed_successfully", (
            "dependency condition must be service_completed_successfully; "
            "service_started would race against the chown"
        )


def test_dapr_scheduler_etcd_data_dir_matches_bind_mountpoint() -> None:
    """The --etcd-data-dir flag points at the host bind's mountpoint inside the container."""
    compose = _load_compose()
    scheduler = compose["services"]["dapr-scheduler"]
    command = scheduler["command"]
    # The command is a YAML list of args; find --etcd-data-dir and its
    # value, then verify the value matches the volume's container path.
    cmd_str = " ".join(str(c) for c in command) if isinstance(command, list) else str(command)
    m = re.search(r"--etcd-data-dir\s+(\S+)", cmd_str)
    assert m, f"--etcd-data-dir flag missing from scheduler command: {cmd_str}"
    etcd_dir = m.group(1)

    # The container-side path must be the same path used as the bind
    # mountpoint. Otherwise the scheduler writes to a tmpfs-shaped layer
    # and the bind never gets populated.
    volumes = scheduler.get("volumes", [])
    container_paths: list[str] = []
    for v in volumes:
        if isinstance(v, str):
            _, _, dst = v.partition(":")
            container_paths.append(dst.split(":")[0])  # strip mode if present
        elif isinstance(v, dict):
            container_paths.append(v.get("target", ""))
    assert etcd_dir in container_paths, (
        f"--etcd-data-dir={etcd_dir} must match the host bind's container "
        f"mountpoint; got mountpoints={container_paths}"
    )


def test_deploy_gitignore_excludes_scheduler_data() -> None:
    """The persistent etcd data dir must NOT be committed."""
    gitignore = _REPO_ROOT / "deploy" / ".gitignore"
    assert gitignore.exists(), (
        "deploy/.gitignore missing — without it the etcd data dir would "
        "land in git"
    )
    content = gitignore.read_text(encoding="utf-8")
    assert "dapr-scheduler-data" in content, (
        f"deploy/.gitignore must exclude the scheduler data dir; got: {content}"
    )
