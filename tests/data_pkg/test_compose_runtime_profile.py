# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""docker-compose `--profile runtime` profile-shape tests.

Validates that the compose file's runtime profile activates exactly
the services specified in the multi-image containerization brief, as
re-cut by the source-first pivot:

  * Temporal is GONE (L-205 / P-16): the optimizer kind's durable GEPA
    loop now runs as a Dapr Workflow on the existing daprd sidecar.
    No temporal-init-db/temporal-server/temporal-ui cluster, no
    legba-temporal-worker app image, no docker/Dockerfile.temporal-worker.
  * The `dapr` profile gained `dapr-init-db` (P-14 — isolates Dapr actor
    state to its own database on the shared Postgres cluster), so it now
    activates 5 services.

  Substrate (always on, no profile): 4 services
    redis, postgres, qdrant, nats

  Dapr (--profile dapr): 5 services
    dapr-init-db, dapr-placement, dapr-scheduler, dapr-scheduler-init,
    dapr-sidecar

  Runtime app images (--profile runtime): 3 services + the dapr substrate
    legba-registry, legba-runtime-dapr, legba-ui-build
    (legba-caddy carries the `runtime` profile and waits on legba-ui-build.)

  MCP (--profile mcp): 1 service
    legba-mcp

These tests parse the compose file via PyYAML — no docker daemon
required. A separate live-integration test under tests/runtime/ can
exercise the actual build path when docker is around.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


# Resolve relative to this file so the test validates the checked-out tree it
# lives in (worktree-safe). The previous hardcoded deployment path made the
# test silently validate a DIFFERENT tree than the one under test (B-1).
REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose_doc() -> dict:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict), "docker-compose.yml is not a mapping"
    return doc


def _services_for_profile(doc: dict, profile: str | None) -> set[str]:
    """Return the set of service names a particular profile activates.

    `profile=None` returns substrate-only (services with no `profiles:`
    key). Otherwise returns services whose `profiles:` includes the
    given name. Substrate is always included since `up` with any
    profile still brings up substrate.
    """
    services = doc.get("services", {})
    if profile is None:
        return {n for n, body in services.items() if "profiles" not in body}
    return {
        n
        for n, body in services.items()
        if "profiles" not in body or profile in body.get("profiles", [])
    }


# ---------------------------------------------------------------------------
# Substrate (default, no profile)
# ---------------------------------------------------------------------------


def test_substrate_services_unprofiled(compose_doc: dict) -> None:
    """Plain `docker compose up -d` brings the 5 substrate services only.

    Regression guard (B4): `legba-caddy` must carry a `profiles:` key so it
    does NOT leak into the unprofiled substrate set. An earlier drift had
    caddy lose its `profiles:` entry, contaminating this set; the compose
    file now restores `profiles: [runtime]` on caddy and this test asserts
    the substrate set stays clean.

    `opensearch` joined the always-on substrate set by design (the
    signal-content-depth program's full-text corpus, backing
    `search_corpus`/`read_document`, used by base-tier analysts — not gated
    behind `--profile runtime`); docs/RUNBOOK.md already documents "5
    substrate containers."
    """
    expected = {
        "redis",
        "postgres",
        "qdrant",
        "nats",
        "opensearch",
    }
    actual = _services_for_profile(compose_doc, profile=None)
    assert actual == expected, (
        f"unprofiled (substrate-only) services drifted.\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(actual)}\n"
        f"  extra:    {sorted(actual - expected)}\n"
        f"  missing:  {sorted(expected - actual)}"
    )


# ---------------------------------------------------------------------------
# Profile activation matrix
# ---------------------------------------------------------------------------


def test_dapr_profile_activates_5_dapr_services(compose_doc: dict) -> None:
    services = compose_doc["services"]
    dapr_services = {
        n for n, body in services.items() if "dapr" in body.get("profiles", [])
    }
    expected = {
        "dapr-init-db",
        "dapr-placement",
        "dapr-scheduler-init",
        "dapr-scheduler",
        "dapr-sidecar",
    }
    assert dapr_services == expected, (
        f"--profile dapr services drifted.\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(dapr_services)}"
    )


# Temporal was cut (L-205 / P-16) — the optimizer kind's GEPA loop runs as a
# Dapr Workflow on the existing daprd sidecar. There is no temporal profile,
# no temporal-init-db/temporal-server/temporal-ui cluster, and no
# legba-temporal-worker app image, so the old
# `test_temporal_profile_activates_3_temporal_services` is retired.


def test_runtime_profile_activates_app_services(compose_doc: dict) -> None:
    """The `runtime` profile is the canonical bring-up. After the Temporal
    cut (P-16) it activates 3 app surfaces (registry + runtime-dapr +
    ui-build), and transitively activates the dapr substrate (sidecar +
    placement + scheduler + init-db) because those services declare BOTH
    `runtime` and `dapr` so either spelling brings them up.

    legba-caddy carries `profiles: [runtime]` so the canonical bring-up
    includes the edge — it is part of the expected set below."""
    services = compose_doc["services"]
    runtime_services = {
        n for n, body in services.items() if "runtime" in body.get("profiles", [])
    }
    expected_app = {
        "legba-registry",
        "legba-runtime-dapr",
        "legba-ui-build",
        "legba-caddy",
    }
    expected_substrate = {
        # Dapr profile (transitively under runtime).
        "dapr-init-db",
        "dapr-placement",
        "dapr-scheduler-init",
        "dapr-scheduler",
        "dapr-sidecar",
    }
    expected = expected_app | expected_substrate
    assert runtime_services == expected, (
        f"--profile runtime services drifted.\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(runtime_services)}\n"
        f"  extra:    {sorted(runtime_services - expected)}\n"
        f"  missing:  {sorted(expected - runtime_services)}"
    )


def test_runtime_profile_separately_counts_app_vs_substrate(
    compose_doc: dict,
) -> None:
    """Sanity: app services + dapr substrate carry distinct profile lists.
    Catches accidental overrides where someone drops a profile during
    edits."""
    svc = compose_doc["services"]

    # The runtime-profiled app images.
    for app in [
        "legba-registry",
        "legba-runtime-dapr",
        "legba-ui-build",
        "legba-caddy",
    ]:
        profiles = svc[app].get("profiles", [])
        assert "runtime" in profiles, f"{app} must carry the runtime profile"

    # Dapr substrate carries both its dapr profile and runtime.
    for dapr_svc in [
        "dapr-init-db",
        "dapr-placement",
        "dapr-scheduler",
        "dapr-sidecar",
    ]:
        profiles = svc[dapr_svc].get("profiles", [])
        assert "dapr" in profiles, f"{dapr_svc} keeps its `dapr` profile spelling"
        assert "runtime" in profiles, (
            f"{dapr_svc} must also activate under `runtime` so the canonical "
            f"bring-up works without specifying --profile dapr separately"
        )


def test_mcp_profile_activates_legba_mcp(compose_doc: dict) -> None:
    services = compose_doc["services"]
    mcp_services = {
        n for n, body in services.items() if "mcp" in body.get("profiles", [])
    }
    assert mcp_services == {"legba-mcp"}, (
        f"--profile mcp services drifted: {sorted(mcp_services)}"
    )


# ---------------------------------------------------------------------------
# Per-service Dockerfile + build-context wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "service,context,dockerfile",
    [
        ("legba-registry", ".", "docker/Dockerfile.registry"),
        ("legba-runtime-dapr", ".", "docker/Dockerfile.runtime"),
        # legba-temporal-worker retired (Temporal cut, P-16).
        ("legba-mcp", ".", "docker/Dockerfile.mcp"),
        ("legba-ui-build", "./legba-ui-v3", "Dockerfile"),
    ],
)
def test_service_build_context_and_dockerfile(
    compose_doc: dict, service: str, context: str, dockerfile: str
) -> None:
    """Each app service's build context + dockerfile path must point at the
    expected place. Catches regressions where a Dockerfile rename leaves
    compose pointing at a stale path."""
    services = compose_doc["services"]
    assert service in services, f"service {service} not in compose"
    build = services[service].get("build")
    assert isinstance(build, dict), f"{service} has no `build:` mapping"
    assert build.get("context") == context, (
        f"{service} build.context: expected {context!r}, got {build.get('context')!r}"
    )
    assert build.get("dockerfile") == dockerfile, (
        f"{service} build.dockerfile: expected {dockerfile!r}, "
        f"got {build.get('dockerfile')!r}"
    )


# ---------------------------------------------------------------------------
# Env wiring + secret discipline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "service",
    [
        "legba-registry",
        "legba-runtime-dapr",
        # legba-temporal-worker retired (Temporal cut, P-16).
        "legba-mcp",
    ],
)
def test_app_services_use_env_file(compose_doc: dict, service: str) -> None:
    """Secrets (LEGBA_DATA_MASTER_KEY, vendor API keys) must come via
    env_file: .env, NOT inline `environment:`. Inline secrets land in
    image history; env_file injects at run-time."""
    body = compose_doc["services"][service]
    assert body.get("env_file") == ".env", (
        f"{service} must declare env_file: .env (got {body.get('env_file')!r}). "
        f"Secrets like LEGBA_DATA_MASTER_KEY must flow in via env_file, "
        f"never via inline environment: with literal values."
    )


def test_app_services_reach_substrate_by_service_name(compose_doc: dict) -> None:
    """The host-mode env vars used `127.0.0.1`; container-mode must use
    docker service names (`postgres`, `nats`, etc.). Catches the
    accidental copy-paste of host-mode env."""
    # legba-temporal-worker retired (Temporal cut, P-16).
    for service in ["legba-registry", "legba-runtime-dapr"]:
        env = compose_doc["services"][service].get("environment", {})
        host = env.get("LEGBA_DATA_PG_HOST")
        assert host == "postgres", (
            f"{service}: LEGBA_DATA_PG_HOST must be the docker service name "
            f"'postgres' (got {host!r})"
        )
        nats_url = env.get("LEGBA_DATA_NATS_URL", "")
        assert "nats:4222" in nats_url, (
            f"{service}: LEGBA_DATA_NATS_URL must use the nats service hostname "
            f"(got {nats_url!r})"
        )


# ---------------------------------------------------------------------------
# UI build → caddy serve wiring
# ---------------------------------------------------------------------------


def test_ui_build_emits_to_shared_volume(compose_doc: dict) -> None:
    """legba-ui-build mounts the `ui_dist` volume at /out and is a
    one-shot (`restart: no`)."""
    body = compose_doc["services"]["legba-ui-build"]
    volumes = body.get("volumes", [])
    assert any("ui_dist:/out" in v for v in volumes), (
        f"legba-ui-build must mount ui_dist:/out; got {volumes!r}"
    )
    assert body.get("restart") == "no", (
        f"legba-ui-build must be `restart: no` (one-shot); got {body.get('restart')!r}"
    )


def test_caddy_serves_ui_volume(compose_doc: dict) -> None:
    """legba-caddy mounts the same ui_dist volume (read-only).

    The volume mount itself is intact; the `depends_on: legba-ui-build`
    ordering is asserted separately below.
    """
    body = compose_doc["services"]["legba-caddy"]
    volumes = body.get("volumes", [])
    assert any("ui_dist:/srv/legba-ui:ro" in v for v in volumes), (
        f"legba-caddy must mount ui_dist:/srv/legba-ui:ro; got {volumes!r}"
    )


def test_caddy_depends_on_ui_build(compose_doc: dict) -> None:
    """legba-caddy must wait for legba-ui-build to complete before starting
    so the volume is populated before caddy serves. The compose file
    declares `depends_on: legba-ui-build` with
    `condition: service_completed_successfully`; this test guards against a
    re-drop of that ordering."""
    body = compose_doc["services"]["legba-caddy"]
    depends = body.get("depends_on", {})
    assert "legba-ui-build" in depends, (
        f"legba-caddy must depend on legba-ui-build; got {sorted(depends)!r}"
    )
    assert (
        depends["legba-ui-build"].get("condition") == "service_completed_successfully"
    ), (
        "legba-caddy must wait for the build job to complete (not just start) "
        "so the volume is populated before caddy serves."
    )


def test_caddy_carries_runtime_profile(compose_doc: dict) -> None:
    """legba-caddy must carry the `runtime` profile so the canonical
    `--profile runtime` bring-up includes the edge."""
    body = compose_doc["services"]["legba-caddy"]
    profiles = body.get("profiles", [])
    assert "runtime" in profiles, (
        f"legba-caddy must carry the runtime profile; got {profiles!r}"
    )


def test_caddy_image_is_official(compose_doc: dict) -> None:
    """Lewis: caddy serves the UI, not nginx. Verify the image is caddy."""
    body = compose_doc["services"]["legba-caddy"]
    image = body.get("image", "")
    assert image.startswith("caddy:"), (
        f"legba-caddy must use the official caddy image; got {image!r}"
    )


# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------


def test_required_volumes_declared(compose_doc: dict) -> None:
    """The container path introduces three new named volumes; declare them
    explicitly so `docker volume ls` shows them and `docker compose down -v`
    cleans them up."""
    volumes = compose_doc.get("volumes", {})
    for name in ["ui_dist", "caddy_data", "caddy_config"]:
        assert name in volumes, f"compose volumes must include {name!r}"


# ---------------------------------------------------------------------------
# Caddyfile presence
# ---------------------------------------------------------------------------


def test_caddyfile_exists_and_proxies_registry() -> None:
    """The Caddyfile lives at docker/Caddyfile and proxies /api/* to the
    legba-registry container."""
    caddyfile = REPO_ROOT / "docker" / "Caddyfile"
    assert caddyfile.is_file(), "docker/Caddyfile missing"
    body = caddyfile.read_text(encoding="utf-8")
    assert "reverse_proxy legba-registry:8090" in body, (
        "Caddyfile must proxy /api/* to legba-registry:8090"
    )
    assert "/srv/legba-ui" in body, (
        "Caddyfile must serve the UI from /srv/legba-ui (the volume mount)"
    )
