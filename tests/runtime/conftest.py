# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures for runtime tests.

Re-exports the migrated_pg / substrate fixtures from tests/data_pkg/conftest.py
so the runtime tests can hit a real Postgres without copying the bring-up
code.

Wave B prereq #3 — daprd test isolation
---------------------------------------

The integration tests in ``test_spike_integration.py`` route every actor
invocation through the SHARED production daprd sidecar (its statestore
points at the production ``legba`` database).  Two test isolation rails
defend against cross-session collision:

  1. A session-scoped ``dapr_actor_session_prefix`` fixture mints a fresh
     UUID-derived prefix.  Tests embed this prefix in every actor_id they
     register, so the dapr_state rows for one test session are byte-
     disjoint from another session's rows.
  2. A session-scoped ``dapr_test_statestore_component`` fixture renders
     a ``statestore.test.<session>.yaml`` pointing at the per-session
     ``legba_test_<uuid>`` DB.  When ``LEGBA_TEST_SIDECAR=1`` is set the
     fixture additionally spawns a separate daprd subprocess pointed at
     this component (full isolation); otherwise it just emits the YAML
     so an operator can hand-load it.  Spawning a per-session sidecar
     across CI / local-dev / branch-divergent daprd binary versions is
     the follow-up — pinned in DESIGN §19.

Rail 1 ships today and is enforced by ``test_spike_integration.py``
embedding the prefix in target/analyst_actor_id construction.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Iterator

import pytest

# Pull in data_pkg fixtures via direct import; pytest's rootdir-level
# conftest discovery picks it up automatically when both sit under
# the same root conftest tree.
_HERE = Path(__file__).parent
_DATA_PKG = _HERE.parent / "data_pkg"
sys.path.insert(0, str(_DATA_PKG))

from tests.data_pkg.conftest import (  # noqa: F401,E402
    substrate_up,
    test_pg_config,
    migrated_pg,
)


@pytest.fixture(scope="session")
def dapr_actor_session_prefix() -> str:
    """Per-session actor-id prefix.

    Tests must include this in every actor_id they register so dapr_state
    rows from concurrent / re-run sessions don't collide.  Embedding it
    early in the actor_id (before the descriptor id) means daprd's
    actor placement + state keying naturally fan out across sessions.

    The prefix is short (8 hex chars) — daprd's actor_id is bounded at
    ~256 chars in practice and we still want descriptor + version slice
    to fit comfortably.
    """
    return f"sess-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def dapr_test_statestore_component(
    tmp_path_factory: pytest.TempPathFactory,
    dapr_actor_session_prefix: str,
) -> Iterator[Path]:
    """Render a per-session statestore component YAML and yield its path.

    The rendered component points at the same Postgres host as the
    production component but at the session-specific ``legba_test_<uuid>``
    DB (matched against the data_pkg ``test_pg_config`` fixture's naming
    pattern).  Today this is informational — it doesn't replace the
    running daprd's loaded component (Dapr loads its components-path at
    boot and doesn't hot-reload).

    To use full isolation, set ``LEGBA_TEST_SIDECAR=1`` and bring up a
    side daprd manually pointed at this file via ``--components-path``;
    the wrapper auto-spawn is the L-002a follow-up tracked in DESIGN §19.

    The actor-id prefix (``dapr_actor_session_prefix``) ships TODAY and
    is the primary isolation rail — see that fixture's docstring.
    """
    root = tmp_path_factory.mktemp("dapr-components")
    yaml_path = root / f"statestore.test.{dapr_actor_session_prefix}.yaml"
    # Mirror the production statestore.yaml shape verbatim — daprd's
    # state.postgresql v1 schema is identical.  The only delta is the
    # connection string's dbname and the component name (so loading both
    # at once is non-conflicting).
    body = (
        "apiVersion: dapr.io/v1alpha1\n"
        "kind: Component\n"
        "metadata:\n"
        f"  name: legba-actor-state-test-{dapr_actor_session_prefix}\n"
        "spec:\n"
        "  type: state.postgresql\n"
        "  version: v1\n"
        "  metadata:\n"
        "    - name: connectionString\n"
        "      value: \"host=postgres user=legba password=legba "
        f"dbname=legba_session_{dapr_actor_session_prefix} sslmode=disable\"\n"
        "    - name: actorStateStore\n"
        "      value: \"true\"\n"
        "    - name: tableName\n"
        f"      value: \"dapr_state_{dapr_actor_session_prefix}\"\n"
        "    - name: cleanupIntervalInSeconds\n"
        "      value: \"600\"\n"
        "    - name: actorStateStore.actorIdPrefix\n"
        f"      value: \"{dapr_actor_session_prefix}\"\n"
    )
    yaml_path.write_text(body, encoding="utf-8")

    # Optional: spawn a separate sidecar via subprocess.  Off by default
    # because most CI / local runs don't have daprd / placement spare
    # ports available.  When LEGBA_TEST_SIDECAR=1 the operator is
    # responsible for the ports being open — we don't probe.
    if os.environ.get("LEGBA_TEST_SIDECAR") == "1":  # pragma: no cover
        # The full subprocess bring-up is the follow-up.  We expose the
        # rendered path so an operator's external wrapper can pick it up.
        os.environ["LEGBA_TEST_STATESTORE_PATH"] = str(yaml_path)

    yield yaml_path
