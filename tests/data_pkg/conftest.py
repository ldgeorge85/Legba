# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""pytest fixtures for legba.data integration tests.

Per L-001 brief item 6: tests run against real running containers (the
dormant `legba-postgres-1` etc.) — no mocks for substrate boundaries. If
containers aren't up, the fixture brings them up via `docker compose`.

Setup pattern:
  1. Ensure containers are healthy (start them if not).
  2. Apply migrations on a *fresh test database* (created per session)
     so the migration set doesn't depend on host-DB state.
  3. Yield connection bundles to each test.

Teardown: the test database is dropped at session end. The containers are
left running for the next test session — same approach as the L-091 audit.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

# Force the package under test to read from local docker port mappings.
os.environ.setdefault("LEGBA_DATA_PG_HOST", "127.0.0.1")
os.environ.setdefault("LEGBA_DATA_PG_PORT", "5432")
os.environ.setdefault("LEGBA_DATA_PG_USER", "legba")
os.environ.setdefault("LEGBA_DATA_PG_PASSWORD", "legba")
os.environ.setdefault("LEGBA_DATA_QDRANT_HOST", "127.0.0.1")
os.environ.setdefault("LEGBA_DATA_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("LEGBA_DATA_NATS_URL", "nats://127.0.0.1:4222")

from legba.data.config import PostgresConfig
from legba.data.migrate import apply_primary_migrations

from tests._env_isolation_shared_flags import RUNTIME_SHARED_PROGRAM_FLAG_DEFAULTS


REPO_ROOT = Path("/usr/local/deployments/active/legba")


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ensure_containers_up() -> None:
    """Start the substrate containers if any aren't reachable on their ports."""
    targets = {
        "postgres":   ("127.0.0.1", 5432),
        "qdrant":     ("127.0.0.1", 6333),
        "redis":      ("127.0.0.1", 6379),
        "nats":       ("127.0.0.1", 4222),
    }
    need_start = [name for name, (h, p) in targets.items() if not _port_open(h, p)]
    if not need_start:
        return

    subprocess.run(
        [
            "docker", "compose",
            "-f", str(REPO_ROOT / "docker-compose.yml"),
            "up", "-d",
            "redis", "postgres", "qdrant", "nats",
        ],
        check=True,
        cwd=REPO_ROOT,
    )

    # Wait up to 90s for ports to be reachable.
    deadline = time.time() + 90
    while time.time() < deadline:
        remaining = [n for n, (h, p) in targets.items() if not _port_open(h, p)]
        if not remaining:
            return
        time.sleep(2)
    pytest.skip(f"substrate containers not reachable: {remaining}")


@pytest.fixture(scope="session", autouse=True)
def substrate_up():
    _ensure_containers_up()
    _ensure_pivot_test_db()
    yield


#: TEST-ENV ISOLATION — the class, not just today's instance (2026-09-06).
#:
#: ``legba.data.config._load_env()`` loads ``.env`` from the first of: cwd,
#: ``Path(__file__).resolve().parents[4]`` (which, both from a worktree
#: checkout AND from a container bind-mounting a worktree at its own path,
#: resolves to a directory that never holds a ``.env`` — it lands two levels
#: above any repo root), or the HARDCODED
#: ``/usr/local/deployments/active/legba/.env`` — the operator's LIVE
#: production file. In practice candidate 3 always wins: this process's env
#: never gets its own ``.env``, so every LEGBA_* program flag the operator
#: has live-tuned (python-dotenv's ``load_dotenv`` sets any var not already
#: in ``os.environ`` — true here, since nothing exports these in a bare
#: shell) leaks into the suite. Below the line "the code default" and "what
#: this test asserts" quietly diverge, and the failure looks like a flaky
#: test rather than what it is: an ambient host file the test process was
#: never supposed to see.
#:
#: This bit the tree SEVEN times before today, one flag at a time (each a
#: separate dated fixture below, in git history) — B0-1, W-2, C-TIER, D-2,
#: P2-1, W-3g twice. Today it was ``LEGBA_EXTERNAL_GRADING_WIDTH`` (4 of
#: ``test_standing_auditor.py``'s failures — see
#: planning/TEST_ENV_ISOLATION_REPORT.md). Rather than wait for flag #9,
#: this pins the whole enumerated family of LEGBA_* PROGRAM flags (regime
#: switches / behavior gates with a documented shipped default — NOT
#: bootstrap/connection/credential vars, which the fixtures above already
#: force to test-appropriate values, or endpoints/refs exercised outside
#: this directory — see the exclusions note below) to their shipped default
#: for every test in this package. A test that wants the ON/overridden path
#: opts in explicitly with ``monkeypatch.setenv(...)`` — the flag is then
#: whatever that one test set it to, and reverts at the test boundary same
#: as any other monkeypatch.
#:
#: Each entry: (env var, shipped/code default, one line on why).
_PROGRAM_FLAG_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("LEGBA_COMPOSITION_VERIFY_FLOOR", "0.0",
     "B0-1: composition verify floor; live .env carries 0.50"),
    ("LEGBA_ANALYST_TRACES_TTL_DAYS", "unset (disabled)",
     "W-2: analyst-trace retention TTL opt-in"),
    ("LEGBA_SIGNALS_RETENTION_TTL_DAYS", "unset (disabled)",
     "W-2: signal retention TTL opt-in"),
    ("LEGBA_COMPOSITION_TIERED_EVIDENCE", "off",
     "C-TIER: legacy single verify-floored gather vs. tiered periphery"),
    ("LEGBA_COMPOSITION_ASSEMBLY", "off",
     "D-2: legacy composition path vs. the deterministic assembly read"),
    ("LEGBA_STRUCTURAL_VERIFY_GATE", "off",
     "P2-1: compute-and-show vs. structural-gate demotion"),
    ("LEGBA_ALERT_SINK_COOLDOWN_SECONDS", "60",
     "W-3g: AlertSinkDispatcher cooldown window; live .env carries 15"),
    ("LEGBA_CONTENTION_EARNED_WEIGHT", "0.0",
     "W-3g: A6 earned-track-record weight in the contention arbiter"),
    ("LEGBA_EXTERNAL_GRADING_WIDTH", "off",
     "2026-09-06: standing_auditor's WIDTH grading sweep vs. the shipped "
     "6-claim sweep (today's break — see TEST_ENV_ISOLATION_REPORT.md)"),
    ("LEGBA_RESEARCH_EVIDENCE", "off",
     "2026-09-06: the research-evidence write path (off/substrate/desks); "
     "an unrecognised OR leaked value must still read as off"),
    ("LEGBA_DESK_REFERENCE_ENABLED", "off",
     "2026-09-06: desk_reference analyst kill switch"),
    ("LEGBA_REFERENCE_GAP_DISPATCH_ENABLED", "off",
     "A-4: the reference_gap collection-requirement + open-question "
     "side-write; off, desk_reference's payload is byte-identical but for "
     "reference_gap_dispatch={enabled:false}"),
    ("LEGBA_FACT_CONTENTION", "off",
     "sibling of CONTENTION_EARNED_WEIGHT: same-tier coexist-not-close"),
    ("LEGBA_FACT_CONTENTION_LLM_TIEBREAK", "off",
     "sibling: LLM tie-break on a near-tie contention abstain"),
    ("LEGBA_FACT_DECAY_WEIGHTING", "off",
     "sibling: fact_decay_states sidecar join in the grounding read"),
    ("LEGBA_SITUATION_TRACKER_MAX_SITUATIONS", "unset (descriptor/code default)",
     "situation_tracker's env dial WINS over the descriptor by design"),
    ("LEGBA_EGRESS_ALLOW_HOSTS", "unset (deny all internal hosts)",
     "SSRF-guard internal-sidecar allowlist"),
    # 2026-09-06 follow-up: the four flags TEST_ENV_ISOLATION deliberately excluded
    # (see planning/TEST_ENV_ISOLATION_REPORT.md's "Deliberately excluded" note,
    # and the exclusions comment below, which is now updated to match). Each of
    # these gates code exercised from BOTH tests/data_pkg AND tests/runtime, so
    # the table lives once in tests/_env_isolation_shared_flags.py and is
    # imported by both conftests rather than duplicated — see that module's
    # docstring. tests/runtime/conftest.py carries the matching autouse pin.
    *RUNTIME_SHARED_PROGRAM_FLAG_DEFAULTS,
    # Appended 2026-09-07 (Amendment 4b). Kept LAST so the diff against the
    # 09-06 table is one line and the shared-flag splice above stays where the
    # runtime conftest expects it.
    ("LEGBA_LEAD_TEST_V2", "off",
     "Amendment 4b: the three crown rules (roster-relative count bar, "
     "argmax crown, mass-anchored co-lead band). The block ORDER is not "
     "among them and stays severity-first under both regimes. "
     "Off is byte-identical to the shipped read; the descriptor option "
     "meta_findings_synthesizer.lead_test_v2 wins over it"),
    ("LEGBA_ROLLUP_MASS_FLOOR", "0.0",
     "Amendment 4a: the by-mass region carry's noise floor. 0.0 is "
     "byte-identical to carrying any block with mass; documented safe band "
     "is (0, 0.10]. The descriptor option "
     "meta_findings_synthesizer.rollup_mass_floor wins over it"),
    ("LEGBA_EVENTS", "off",
     "V3/P0 (2026-09-21): the event write path ships dark; the flag's "
     "default-off state is what the byte-identical contract requires"),
    ("LEGBA_EDGE_TRANSITION_LEDGER", "off",
     "V3/P3 (2026-09): the entity_edge_events ledger ships dark; a leaked ON "
     "from the live .env would silently write ledger rows inside every "
     "nexus-write byte-identity test"),
    ("LEGBA_GRAPH_PROJECTION", "off",
     "V3/P4b (2026-09-23): the graph_arcs projection ships dark; the live .env "
     "turned it ON at roll 10 and every graph_mining / structural_balance test "
     "that seeds entity_edges then read an empty projection instead of the "
     "legacy substrate path — flag-off is the byte-identical baseline"),
    ("LEGBA_EVENT_CITATIONS", "off",
     "V3/P2 (2026-09-23): the event:<uuid> ref kind ships dark; a leaked ON "
     "would expand event refs inside every citation-builder test"),
    # 2026-09-07 wave: the three knobs whose lanes registered them in the X-1 guard but not here — the live .env now
    # carries SLICE_GEO_V2=1, WINDOW_BASIS=evidence, GRACE=96, and two standing_auditor tests asserting the
    # heads/0 width stamp (2026-09-06/1) read the live values and saw 2026-09-07/1.
    ("LEGBA_SLICE_GEO_V2", "off",
     "J: desk slices admit title-named polities (flag-on changes input_row_refs)"),
    ("LEGBA_JOURNAL_CLUSTER_FIRST", "off",
     "journal cluster-first selection (T2.2, 2026-09-10) — flag-off is the byte-identical baseline every journal test asserts"),
    ("LEGBA_JOURNAL_SLICE_V2", "off",
     "journal salience-aware fetch leg (journal_slice_v2, 2026-09-10) — flag-off keeps the 360-newest leg every substrate_slice test asserts"),
    ("LEGBA_EXTERNAL_AUDIT_WINDOW_BASIS", "heads",
     "width G-3 window basis; evidence re-bases the drained claims' window and moves the width stamp"),
    ("LEGBA_EXTERNAL_AUDIT_WINDOW_GRACE_HOURS", "0",
     "width G-3 grace before window.oldest; >0 moves the width stamp"),
    # 2026-09-16 (S3). Appended LAST, per the discipline noted above.
    ("LEGBA_FETCH_IMPERSONATE", "off",
     "S3 (a): browser-fingerprint impersonation on the page-fetch client "
     "(_egress.fetch_client). Pinned here because the flag-on path swaps the "
     "client for a curl_cffi adapter, which no MockTransport seam in this "
     "suite can intercept — an operator who sets it in the live .env would "
     "silently turn every page-fetch test into a live request"),
    # 2026-09-16 (G2): the composition CORRECTNESS gate and the three
    # operator-set thresholds under it. Pinned for the reason C-TIER is pinned
    # by name above — the live .env is what a deployed gate is switched on in,
    # and a leaked `1` here would quietly demote desks inside every composition
    # byte-identity test in the tree.
    ("LEGBA_COMPOSITION_CORRECTNESS_GATE", "off",
     "G2: compose only over units carrying a passing correctness number; "
     "off is byte-identical to the shipped read"),
    ("LEGBA_COMPOSITION_GATE_MIN_CORRECTNESS", "0.8 (code default)",
     "G2: the operator's correctness bar; only read when the gate is on"),
    ("LEGBA_COMPOSITION_GATE_MIN_COVERAGE", "0.2 (code default)",
     "G2: the operator's coverage bar; only read when the gate is on"),
    ("LEGBA_COMPOSITION_GATE_ALLOW_SINGLE_FAMILY", "off",
     "G2: whether a one-grader-family number may carry a composition"),
    # 2026-09-26 (o6, test-debt baseline). Flag #10 of this class, and the
    # whole consult family at once rather than the three that happened to
    # break. The live .env carries ROUND_RESULT_BYTES=40000 (code default
    # 16_000), KEEP_FULL_ROUNDS=1000 (code default 1) and NATIVE_BATCH_CAP=5
    # (code default 4) — the operator's deliberate "let a consult run wide"
    # tuning. Leaked in, the compaction leg never fires (nothing is ever old
    # enough to digest), the round bound is 2.5x what the test asserts, and a
    # five-call batch is admitted where the shipped cap is four. That is
    # exactly the four test_consult_cost_and_synthesis failures that had sat
    # in the suite's "known baseline" for a week: they pass on their own
    # (nothing has imported legba.data.config yet) and fail in a full run
    # (something has), which reads as flake and is an ambient host file.
    # The other four knobs of the same family are pinned alongside them, for
    # the reason the table's docstring gives: waiting for the next one to
    # break is how this bit the tree ten times.
    ("LEGBA_CONSULT_ROUND_RESULT_BYTES", "16000 (tool_round_compaction.DEFAULT_ROUND_RESULT_BOUND)",
     "per-ROUND tool-result bound; live .env carries 40000"),
    ("LEGBA_CONSULT_KEEP_FULL_ROUNDS", "1 (tool_round_compaction.DEFAULT_KEEP_FULL_ROUNDS)",
     "rounds exempt from compaction; live .env carries 1000, which disables "
     "compaction outright"),
    ("LEGBA_CONSULT_NATIVE_BATCH_CAP", "4 (consult_round_protocol.DEFAULT_NATIVE_BATCH_CAP)",
     "per-round native call cap; live .env carries 5"),
    ("LEGBA_CONSULT_BUDGET_SECONDS", "480.0 (consult_round_protocol.DEFAULT_TOTAL_BUDGET_SECONDS)",
     "sibling knob of the three above; live .env carries 900"),
    ("LEGBA_CONSULT_ROUND_DEADLINE_SECONDS", "150.0 (consult_round_protocol.DEFAULT_ROUND_DEADLINE_SECONDS)",
     "sibling knob; live .env carries 480"),
    ("LEGBA_CONSULT_MAX_INPUT_TOKENS_PER_RUN", "150000 (consult_spend_guard.DEFAULT_MAX_INPUT_TOKENS_PER_RUN)",
     "sibling knob; live .env carries 100000000, i.e. the guard lifted"),
    ("LEGBA_CONSULT_MAX_COST_USD_PER_RUN", "3.00 (consult_spend_guard.DEFAULT_MAX_COST_USD_PER_RUN)",
     "sibling knob; live .env carries 1000000, i.e. the ceiling lifted"),
)
#: Deliberately NOT in this list, checked and excluded rather than missed:
#: ``LEGBA_ALERT_NTFY_MIN_SEVERITY`` / ``LEGBA_ALERT_NTFY_URL`` (single
#: dedicated test file, tests/data_pkg/alerts/test_ntfy_sink.py, already
#: exercises this exact mechanism directly). Bootstrap/connection/credential
#: vars (PG/Qdrant/Redis/NATS host+port+user+password, registry DSN/token,
#: master key, model/API endpoints) are out of scope for this list — they're
#: either forced to test-appropriate values above, preserved (not pinned) by
#: the root ``tests/conftest.py`` snapshot, or not behavior gates at all.


@pytest.fixture(autouse=True)
def _pin_program_flags_to_shipped_defaults(monkeypatch):
    """Strip every :data:`_PROGRAM_FLAG_DEFAULTS` key from the process env
    before each test, so this suite is deterministic regardless of what the
    live ``/usr/local/deployments/active/legba/.env`` happens to carry (see
    the module docstring above). A test that wants a flag's overridden/ON
    path sets it explicitly via ``monkeypatch.setenv(...)`` — that always
    wins over this fixture (monkeypatch layers per-test) and self-reverts."""
    for env_key, _default, _why in _PROGRAM_FLAG_DEFAULTS:
        monkeypatch.delenv(env_key, raising=False)


# ---------------------------------------------------------------------------
# Persistent pivot-test database (legba_pivot_test)
# ---------------------------------------------------------------------------
#
# A handful of acceptance tests (analyst cross-source dedup / finding
# supersession / entity resolution, source-actor acquisition, the
# subscription engine, the P-13 discovery rig) connect DIRECTLY to a fixed
# ``legba_pivot_test`` database rather than the per-session ephemeral
# ``legba_test_<uuid>`` DB, and SKIP themselves when it is unreachable. So
# the test recipe is self-standing — and not silently degraded to "lots of
# skips" — the session bootstrap creates + migrates ``legba_pivot_test`` if
# it is absent. Idempotent: an already-migrated DB (the live dev rig)
# applies no new migrations and is left untouched. Override the target name
# with ``LEGBA_PIVOT_PG_DB``.

_PIVOT_DB_NAME = os.environ.get("LEGBA_PIVOT_PG_DB", "legba_pivot_test")


def _ensure_pivot_test_db() -> None:
    """Create + migrate ``legba_pivot_test`` if it does not already exist.

    Best-effort: if Postgres is unreachable we return quietly and let the
    individual pivot-DB tests hit their own skip path. Migrations are
    CREATE-only + idempotent, so re-running against the live dev-rig DB is
    a no-op.
    """
    asyncio.run(_ensure_pivot_test_db_async())


async def _ensure_pivot_test_db_async() -> None:
    admin_dsn = "postgresql://legba:legba@127.0.0.1:5432/postgres"
    try:
        conn = await asyncpg.connect(admin_dsn)
    except Exception:
        # Postgres not reachable — pivot-DB tests will skip on their own.
        return
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", _PIVOT_DB_NAME
        )
        if not exists:
            # CREATE DATABASE cannot run inside a transaction block; asyncpg
            # auto-commits single statements outside an explicit tx.
            await conn.execute(f'CREATE DATABASE "{_PIVOT_DB_NAME}"')
    finally:
        await conn.close()

    # Apply (idempotent) primary migrations against the pivot DB.
    cfg = PostgresConfig(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database=_PIVOT_DB_NAME,
    )
    try:
        await apply_primary_migrations(cfg)
    except Exception:
        # A partially-migrated or differently-shaped pre-existing DB
        # surfaces in the individual tests' substrate-presence checks
        # (which skip rather than error). Don't fail the whole session.
        return


# ---------------------------------------------------------------------------
# Fresh test database
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def test_pg_config() -> PostgresConfig:
    """Create a fresh `legba_test_<uuid>` database, return its config, drop at session end."""
    admin_dsn = "postgresql://legba:legba@127.0.0.1:5432/postgres"
    db_name = f"legba_test_{uuid4().hex[:10]}"

    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()

    cfg = PostgresConfig(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database=db_name,
    )
    yield cfg

    # Teardown: drop the test database.
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname='{db_name}' AND pid <> pg_backend_pid()"
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def migrated_pg(test_pg_config: PostgresConfig) -> PostgresConfig:
    """Apply all primary migrations to the fresh test DB and return config."""
    applied = await apply_primary_migrations(test_pg_config)
    assert applied, "expected at least one migration to apply"
    return test_pg_config


# ---------------------------------------------------------------------------
# clean_tables — centralized, opt-in truncation primitive.
# ---------------------------------------------------------------------------
#
# Root-caused by the 2026-08-29 shuffle-pollution-class train
# (planning/CAMPAIGN_2026-08-29/CODE_STATUS.md §4 / LOG_FORENSICS.md finding
# 3): 134 of ~398 tests/data_pkg/ files depend on `migrated_pg`, which is
# session-scoped by necessity (re-applying all migrations per-file/per-test
# is a measured 130x/1929x runtime multiplier — not viable). Only 14 of those
# 134 had ANY cleanup fixture, each a bespoke, file-local `clean_slate`
# reimplementing the same "DELETE my rows before I run" shape. This is that
# shape, centralized, so the other 120 have a one-line opt-in instead of
# hand-rolling scoped DELETEs — and so a future file doesn't silently join
# the 120.
#
# TRUNCATE, not a per-file scoped DELETE: instant regardless of row count
# (no WHERE-clause bookkeeping needed), and safe here specifically because
# every named table is opt-in per-test — a test only requests truncation of
# a table it EXCLUSIVELY owns for the duration of its own assertions, the
# same contract the existing bespoke `clean_slate` fixtures already assume.
# Runs at SETUP (matches the established idiom: every existing `clean_slate`
# fixture in this directory cleans BEFORE `yield`, not after — the NEXT
# test's own setup-time clean is what protects it, not this test's
# teardown). A test that also needs a teardown-time reset (e.g. restoring a
# migration-seeded config row rather than emptying a table) still needs its
# own bespoke fixture — this primitive is for the "the table is scratch
# space I fully own" shape, which is the common case.
@pytest_asyncio.fixture
def clean_tables(migrated_pg: PostgresConfig):
    """Factory fixture — call as ``await clean_tables("table_a", "table_b")``
    (typically from inside a file-local wrapping fixture, mirroring the
    existing ``clean_slate`` idiom) to TRUNCATE the named tables right
    before a test runs. Opens its own short-lived connection rather than
    depending on any file's differently-scoped ``pg_pool`` fixture, so it
    works the same way regardless of how (or whether) a file defines one.

    Tolerates a table that doesn't exist YET: at least one real target
    (``actor_state``) lives outside the migration set entirely — it's
    created on-demand by ``ActorStateStore.ensure_schema()`` the first time
    ANY test in the session calls it, not by a migration — so a
    ``clean_tables("actor_state")`` that runs as the very first
    actor_state-touching fixture in a session (e.g. this file's tests run
    standalone, or first in a shuffle) would hit a table that doesn't exist
    yet. ``UndefinedTableError`` there means "already clean" (nothing to
    truncate), not a real failure — any OTHER missing-relation error still
    raises normally.
    """

    async def _clean(*names: str) -> None:
        if not names:
            return
        conn = await asyncpg.connect(migrated_pg.dsn)
        try:
            quoted = ", ".join(f'"{n}"' for n in names)
            try:
                # CASCADE (2026-09-22, V3/P0): 0202/0203 hang FK dependents off
                # `situations` (situation_event_links), `entity_profiles`
                # (event_entity_links) and `events`; Postgres refuses to TRUNCATE a
                # referenced parent alone, and a dependent row is meaningless
                # without its parent, so a file that owns the parent owns the tail.
                await conn.execute(f"TRUNCATE TABLE {quoted} CASCADE")
            except asyncpg.exceptions.UndefinedTableError:
                pass
        finally:
            await conn.close()

    return _clean


# ---------------------------------------------------------------------------
# restore_source_credibility_seed — canonical baseline-seed restore.
# ---------------------------------------------------------------------------
#
# Lifted out of two independently-written, file-local inlined copies
# (tests/data_pkg/agency/test_research_web_evidence_e2e.py and
# test_research_slice_and_gather.py) added during the 2026-09-05 merge-wave
# hygiene pass. Both files' reset fixtures run an UNSCOPED
# ``DELETE FROM source_credibility`` that commits on the SHARED
# session-scoped test database (``migrated_pg`` above is session-scoped for
# performance reasons — see the ``clean_tables`` docstring), which poisoned
# every later ``source_credibility``-touching test in the suite depending on
# run order. Centralized here so the next file that scrubs a seeded table has
# a one-line remedy instead of a third copy-paste.
#
# Read straight from the baseline migration's own INSERT statements — never a
# hand-maintained row count — so a future edit to the seed data (e.g. an
# added source_host) is picked up automatically instead of drifting from a
# second, staler copy.
_SC_SEED_SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "legba" / "data" / "migrations" / "0001_baseline.sql"
)

_SC_SEED_INSERTS = tuple(
    line.strip()
    for line in _SC_SEED_SQL_PATH.read_text().splitlines()
    if line.startswith("INSERT INTO public.source_credibility ")
)


@pytest_asyncio.fixture
def restore_source_credibility_seed():
    """Factory fixture — call as
    ``await restore_source_credibility_seed(conn)`` (typically from a
    file-local reset fixture's teardown, mirroring the ``clean_tables``
    call-a-factory idiom above) to re-apply the baseline
    ``source_credibility`` rows onto an open connection. Idempotent
    (``ON CONFLICT (source_host) DO NOTHING``), so it's safe to call even
    when the table was never actually wiped.
    """

    async def _restore(conn) -> None:
        for stmt in _SC_SEED_INSERTS:
            await conn.execute(stmt.rstrip(";") + " ON CONFLICT (source_host) DO NOTHING")

    return _restore
