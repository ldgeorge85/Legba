# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resilience-observability W-1b §4 — registry readiness probe.

`/api/v1/registry/healthz` is upgraded from a static-`ok` liveness stub into a
*readiness* probe that pings Postgres (``SELECT 1``) + NATS (``is_connected``).
The Docker HEALTHCHECK and the Caddy upstream point at it, so the registry is
only routed to once it can actually serve its substrate dependencies.

Runs against the live substrate (the `migrated_pg` fixture brings up the
containers + a fresh DB). The probe carries no auth dependency, so no bearer is
sent. We assert:

  * a healthy substrate → 200 with both checks ``ok``;
  * a downed Postgres pool → 503 naming postgres as the failing check (the
    fail-loud contract the orchestrator needs to pull the container).
"""

from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.registry.credentials import MASTER_KEY_ENV
from legba.data.registry.server import create_app

_HEALTHZ = "/api/v1/registry/healthz"

# create_app builds the credential vault + audit signer; supply the same test
# secrets the registry integration suite uses so app construction succeeds.
_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "22" * 32)


def _app(pg_config: PostgresConfig):
    return create_app(
        pg_config=pg_config,
        nats_config=NatsConfig.from_env(),
        # No background health loop / vocabulary subscription needed for the probe.
        enable_healthcheck_loop=False,
        enable_vocabulary_subscription=False,
    )


@pytest.mark.integration
def test_healthz_ready_when_substrate_up(migrated_pg: PostgresConfig) -> None:
    # TestClient runs the lifespan → both stores connect before requests.
    with TestClient(_app(migrated_pg)) as client:
        r = client.get(_HEALTHZ)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["checks"]["postgres"] == "ok"
        assert body["checks"]["nats"] == "ok"


@pytest.mark.integration
def test_healthz_unavailable_when_postgres_down(migrated_pg: PostgresConfig) -> None:
    app = _app(migrated_pg)
    with TestClient(app) as client:
        # Sanity: ready first.
        assert client.get(_HEALTHZ).status_code == 200

        # Simulate a substrate outage: close the live PG pool out from under the
        # probe (run on the lifespan event loop via the client's blocking
        # portal). The readiness check must now fail loud (503) and name
        # postgres as the failing dependency.
        deps = app.state.registry_deps
        client.portal.call(deps.descriptor_registry.pg.close)

        r = client.get(_HEALTHZ)
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["status"] == "unavailable"
        assert body["checks"]["postgres"].startswith(("error", "unexpected"))
