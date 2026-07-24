# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Root-level pytest fixtures shared across the whole suite.

This file exists primarily to defend the single-process full-suite run
against cross-test environment leakage. Several registry/API test modules
construct an in-process FastAPI app in *dev mode* by popping
``LEGBA_REGISTRY_API_TOKEN`` out of ``os.environ`` (so any/no bearer is
accepted). The pop is correct for those tests, but the token is loaded
once from the repo ``.env`` (via ``legba.data.config._load_env``) and
those fixtures never put it back — so once any of them runs, the token is
gone for the remainder of the process. Tests that legitimately talk to the
LIVE registry (e.g. ``test_deps_fallback.py``'s 404→None round-trip) then
get a 401 instead of a 404 and fail, but ONLY in the full single-process
run — they pass standalone where the token is still present.

The autouse fixture below snapshots a small set of process-global
environment variables before every test and restores them afterwards, so a
test that mutates one of them can't leak that mutation into a sibling. It
does not change any test's own behaviour — within a test the variable is
whatever that test set it to; the snapshot/restore happens at the test
boundary.
"""

from __future__ import annotations

import os
import re

import pytest

# C-1: deterministic predicate evals under full-suite load. The predicate
# DSL's DEFAULT 5 ms wall-clock budget (spec §3) counts scheduler preemption
# against the predicate — under a busy full-suite run the FIRST cold
# ``starlark.eval`` (and occasionally a warm one) breached 5 ms and raised
# ``PredicateBudgetExceeded`` in whichever test happened to pay the cold
# cost first (the documented ``test_mentions_*`` / ctx-parity /
# applicability-predicate order-dependent flake family). Give the suite a
# generous DEFAULT budget envelope via the env override; production keeps
# 5 ms (the env var is unset there) and every budget-ENFORCEMENT test
# passes its own explicit ``EvalBudget(wall_clock_ms=...)``, so the
# enforcement path itself stays fully exercised.
os.environ.setdefault("LEGBA_PREDICATE_WALL_CLOCK_MS", "250")

# B-2: the registry API gate is fail-closed — an unset/empty
# LEGBA_REGISTRY_API_TOKEN now means HTTP 503 on every guarded request
# unless LEGBA_DEV_MODE=1 is set explicitly. Many test modules build their
# in-process apps by popping the token ("dev mode"); default the suite to
# the explicit dev flag so those fixtures keep their permissive posture.
# Tests that assert the fail-closed 503 path monkeypatch.delenv this key.
os.environ.setdefault("LEGBA_DEV_MODE", "1")

# Env vars that individual test modules legitimately mutate for their own
# in-process apps but that other tests depend on being at their .env-loaded
# (or unset) baseline. Snapshot + restore these at every test boundary.
_PRESERVED_ENV_KEYS = (
    "LEGBA_REGISTRY_API_TOKEN",
    "LEGBA_REGISTRY_API_URL",
    "LEGBA_REGISTRY_SIGNING_KEY",
    "LEGBA_REGISTRY_SIGNING_KEY_FILE",
    "LEGBA_DEPS_FALLBACK_ENABLED",
    "LEGBA_DEV_MODE",
    "LEGBA_A2A_ENABLED",
    "LEGBA_A2A_TRUSTED_KEYS",
)


@pytest.fixture(autouse=True)
def _preserve_process_env():
    """Restore a handful of process-global env vars after each test.

    Guards the single-process full-suite run from cross-test leakage of
    these keys (see module docstring). Standalone runs are unaffected.
    """
    saved = {k: os.environ.get(k) for k in _PRESERVED_ENV_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def _preserve_analyst_kind_registry():
    """Restore the process-wide ``ANALYST_KIND_REGISTRY`` extension set after
    each test (TEST_DEBT_RECON.md Bucket I).

    Same class of bug as ``_preserve_process_env`` above, one layer deeper:
    ``DescriptorRegistry.start()`` / ``.sync_analyst_kinds()`` calls
    ``ANALYST_KIND_REGISTRY.replace_extensions(...)`` — a full REPLACE (not a
    union) sourced from whatever that registry's own ``vocabulary_entries``
    table happens to contain. In a fresh single-session test DB that table has
    no ``analyst_kind`` rows, so ANY test anywhere in the suite that
    constructs a real ``DescriptorRegistry`` and calls ``.start()`` /
    ``.sync_analyst_kinds()`` wipes the in-code extension-kind registrations
    ``legba.data.analysts`` does at import time (``journal_assessor`` /
    ``entity_researcher`` / ``signal_salience``) for the REMAINDER of the
    single-process full-suite run — poisoning any later test/file that
    assumes those kinds validate, regardless of whether that later test
    itself re-imports ``legba.data.analysts`` (the module-level registration
    only fires on first import; it's already cached).

    Several individual fixtures across the suite (``test_registry_descriptor_
    integration.py``'s ``registry``/``registry_no_nats``, and similar
    ``DescriptorRegistry(...).start()`` fixtures in other files) snapshot +
    restore this locally, but new ones keep appearing — centralizing the
    guard here closes the leak for the whole suite in one place, the same
    way ``_preserve_process_env`` centralizes the env-var class of this bug.
    Does not change any test's own behaviour — within a test the registry
    holds whatever that test/fixture set it to; the snapshot/restore happens
    only at the test boundary.
    """
    from legba.data.schemas import ANALYST_KIND_REGISTRY

    saved = ANALYST_KIND_REGISTRY.extension_values()
    try:
        yield
    finally:
        ANALYST_KIND_REGISTRY.replace_extensions(saved)


# ---------------------------------------------------------------------------
# C-1 strict mode — LEGBA_TEST_STRICT=1 turns INFRA-GATED skips into failures
# ---------------------------------------------------------------------------
#
# Review §3.4: two live bugs shipped through 2300+ green tests partly because
# infra-gated tests degrade to silent skips when a rig dependency (Postgres /
# NATS / the fixed pivot DB / daprd / Qdrant / Redis) is down or
# a substrate table is missing. On the canonical rig NOTHING should skip for
# infra reasons — a skip there means the rig is broken, not that coverage is
# optional. Strict mode makes that loud.
#
# Mechanism: a central report hook (no per-site edits). When
# ``LEGBA_TEST_STRICT=1``, any skipped outcome whose reason matches the
# infra-gate patterns below is escalated to a FAILURE. Skips that gate on
# optional EXTERNAL credentials / live third-party endpoints (LLM provider
# tokens, MediaCloud/RSS/BigQuery live tests, Mnemosyne peer URLs, docker
# CLI capabilities) are NOT escalated — they are opt-in by design and not
# part of the canonical rig contract.
#
# Strict mode is the library DEFAULT (review1 §4.4 cutover): it stays ON
# unless the caller explicitly opts OUT with ``LEGBA_TEST_STRICT=0`` (the
# local no-infra escape hatch). The container runner / release gate are
# unaffected — they already pass strict through.

#: Exemptions, checked FIRST: skip reasons that are NOT infra gates even
#: when they mention infra nouns. Permanent test retirements, explicit
#: opt-in env gates (`LEGBA_TEST_X=1 not set`), missing external creds /
#: endpoints, optional libraries, and host-CLI capability probes.
_STRICT_EXEMPT_REASON_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bretired:",                           # permanently retired test
        r"\b[A-Z][A-Z0-9_]*\s*=\s*1\b.*not set", # opt-in: LEGBA_TEST_X=1 not set
        r"\b[A-Z][A-Z0-9_]*(_TOKEN|_API_KEY|_URL|_DID|_KEY)\b.*"
        r"(not set|required|non-existent)",      # external creds / peer URLs
        r"not installed",                        # optional library (dspy, bigquery)
        r"is installed",                         # inverse-gate (negative-case tests)
        r"docker (not on PATH|version lacks)",   # host CLI capability probe
    )
)

#: Reason patterns identifying an INFRA gate (rig substrate, not opt-in
#: external creds). Case-insensitive search over the skip reason text.
_STRICT_INFRA_REASON_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"not reachable",                       # ports: PG/NATS/Redis/Qdrant/...
        r"unreachable",                         # legba_pivot_test unreachable
        r"containers",                          # substrate containers ...
        r"substrate",                           # pivot substrate ... not present
        r"not present",                         # missing tables / migrations
        r"\bdaprd?\b",                          # daprd sidecar / dapr placement
        r"\bplacement\b",
        r"\bnats\b",
        r"\bpostgres\b",
        r"\bqdrant\b",
        r"\bredis\b",
        r"\bmigration\b",
        r"pivot[-_ ]?db|legba_pivot_test",
    )
)


def strict_mode_enabled() -> bool:
    """True when the suite runs in strict no-skip mode.

    Strict is now the library DEFAULT (review1 §4.4 / item 2.8): infra-gated
    skips fail loud unless the caller explicitly opts OUT with
    ``LEGBA_TEST_STRICT=0``. Any other value (unset, ``1``, anything truthy)
    keeps strict mode ON. The explicit ``0`` is the documented escape hatch
    for local runs without the rig substrate.
    """
    return os.environ.get("LEGBA_TEST_STRICT", "").strip() != "0"


def _is_infra_gate_reason(reason: str) -> bool:
    if any(p.search(reason) for p in _STRICT_EXEMPT_REASON_PATTERNS):
        return False
    return any(p.search(reason) for p in _STRICT_INFRA_REASON_PATTERNS)


def _skip_reason_from_report(report) -> str:
    """Best-effort extraction of the skip reason text from a report."""
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])  # (path, lineno, reason)
    return str(longrepr or "")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not report.skipped or not strict_mode_enabled():
        return
    # xfail-marked outcomes also surface as skipped reports — leave them be.
    if hasattr(report, "wasxfail"):
        return
    reason = _skip_reason_from_report(report)
    if not _is_infra_gate_reason(reason):
        return
    report.outcome = "failed"
    report.longrepr = (
        f"[LEGBA_TEST_STRICT] infra-gated skip escalated to FAILURE "
        f"({item.nodeid}): {reason}\n"
        "Strict mode means the canonical rig must provide this dependency — "
        "fix the rig (or the gate) instead of skipping."
    )
