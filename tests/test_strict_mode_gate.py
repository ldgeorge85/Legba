# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-1 — unit coverage for the strict no-skip classifier (tests/conftest.py).

``LEGBA_TEST_STRICT=1`` escalates INFRA-GATED skips to failures via the
central ``pytest_runtest_makereport`` hook. The hook's behaviour hinges on
one pure function — ``_is_infra_gate_reason`` — so the classification
contract is pinned here against the REAL skip reasons used across the
suite (copied verbatim from their call sites), in both directions:

  * rig-infra gates (Postgres/NATS/pivot-DB/daprd/Qdrant/Redis/substrate
    tables) MUST be escalated;
  * opt-in env gates, external-credential gates, optional libraries,
    docker-CLI capability probes and permanent ``retired:`` markers MUST
    NOT be (strict mode cannot will a third-party token into existence).

The end-to-end escalation path (report outcome mutation) is exercised by
running any infra-skipping test with LEGBA_TEST_STRICT=1 on a rig with
that dependency down — by design that cannot be simulated here without
faking the infra state.
"""

from __future__ import annotations

import pytest

from tests.conftest import _is_infra_gate_reason, strict_mode_enabled


# Verbatim reasons from the suite's infra gates → MUST escalate.
_INFRA_REASONS = [
    "dev-rig Postgres not reachable on 127.0.0.1:5432",
    "dev-rig NATS not reachable on 127.0.0.1:4222",
    "NATS not reachable: [Errno 111] Connection refused",
    "substrate containers not reachable: ['postgres', 'nats']",
    "legba_pivot_test unreachable: connection refused",
    "pivot substrate (signal_aliases / canonical_signal_id) not present",
    "P-FS substrate (finding_supersessions / superseded_by) not present",
    "migration 0029 (entity-resolution substrate) not present",
    "daprd not running on localhost:3500/50001/50005 — bring up with "
    "`docker compose --profile dapr up -d`",
    "daprd outbound channel unhealthy (placement/sidecar down)",
    "redis container not reachable on 6379",
]

# Verbatim reasons from opt-in / external / retired gates → MUST NOT escalate.
_EXEMPT_REASONS = [
    "LEGBA_TEST_DAPR_WORKFLOW=1 not set; skipping live daprd-sidecar test",
    "LEGBA_TEST_TEMPORAL=1 not set; skipping Temporal-server tests",
    "LEGBA_TEST_QDRANT=1 not set; skipping live Qdrant tests",
    "LEGBA_MEDIACLOUD_API_KEY not set",
    "LEGBA_VLLM_TOKEN not set",
    "MNEMOSYNE_A2A_URL is required for the live round-trip",
    "LEGBA_RSS_TEST_URL not set; skipping live RSS integration",
    "google-cloud-bigquery not installed; skipping live test",
    "dspy is installed; negative case not reachable here",
    "docker not on PATH; skipping --check path",
    "docker version lacks --check support; stderr='...'",
    "retired: the `predictions` table was DROPPED in migration 0024 "
    "(succeeded by `hypotheses`) ...",
]


@pytest.mark.parametrize("reason", _INFRA_REASONS)
def test_infra_gate_reasons_escalate(reason: str):
    assert _is_infra_gate_reason(reason), (
        f"infra gate NOT classified for escalation: {reason!r}"
    )


@pytest.mark.parametrize("reason", _EXEMPT_REASONS)
def test_opt_in_and_retired_reasons_do_not_escalate(reason: str):
    assert not _is_infra_gate_reason(reason), (
        f"opt-in/external/retired skip wrongly classified as infra: {reason!r}"
    )


def test_strict_mode_flag_reads_env(monkeypatch: pytest.MonkeyPatch):
    # Strict is the DEFAULT (review1 §4.4): unset / 1 → ON, only explicit
    # 0 opts out.
    monkeypatch.delenv("LEGBA_TEST_STRICT", raising=False)
    assert strict_mode_enabled() is True
    monkeypatch.setenv("LEGBA_TEST_STRICT", "1")
    assert strict_mode_enabled() is True
    monkeypatch.setenv("LEGBA_TEST_STRICT", "0")
    assert strict_mode_enabled() is False
