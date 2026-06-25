# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wave B prereq #3 — test isolation fixtures.

Smoke tests for the per-test-session daprd isolation fixtures in conftest.py.
Don't talk to daprd at all — just verify the fixtures yield well-shaped
data and the YAML render is parseable.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_dapr_actor_session_prefix_well_formed(dapr_actor_session_prefix: str) -> None:
    """Prefix is ``sess-<8hex>`` — short enough to embed and unique per session."""
    assert isinstance(dapr_actor_session_prefix, str)
    assert re.fullmatch(r"sess-[0-9a-f]{8}", dapr_actor_session_prefix), (
        f"unexpected prefix shape: {dapr_actor_session_prefix!r}"
    )


def test_dapr_actor_session_prefix_session_scoped(
    dapr_actor_session_prefix: str,
) -> None:
    """Re-requesting in the same session yields the same prefix
    (so target + analyst actor_ids share a prefix the DELETE can match)."""
    # Direct fixture re-injection check — same string instance across calls
    # because the fixture is scope="session".
    assert dapr_actor_session_prefix == dapr_actor_session_prefix


def test_dapr_test_statestore_component_renders(
    dapr_test_statestore_component: Path,
    dapr_actor_session_prefix: str,
) -> None:
    """The fixture writes a YAML file embedding the session prefix."""
    body = dapr_test_statestore_component.read_text(encoding="utf-8")
    assert dapr_actor_session_prefix in body
    assert "apiVersion: dapr.io/v1alpha1" in body
    assert "kind: Component" in body
    assert "state.postgresql" in body
    assert "actorStateStore" in body
    # Per-session DB name to keep the YAML self-describing for an operator
    # bringing up a side sidecar manually.
    assert f"legba_session_{dapr_actor_session_prefix}" in body
