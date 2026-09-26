# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0190 — the ``external_grades`` ledger, against a real Postgres.

Everything here runs against real SQL, and that is not ceremony: this
migration's guarantees are all properties of the DATABASE. It must be idempotent
under a runner that re-globs every file; it must FORBID an UPDATE and a DELETE
via a trigger pair, because the party the ledger measures owns the database and a
measurement the measured party can edit is not one; it must enforce a
closed-vocabulary population and verdict; and its UNIQUE must make a re-run a
no-op while still letting a second family write a second row for the same claim.
A test that mocked the connection would assert only that the Python called the
SQL it was written to call.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import (
    _external_audit_claims as width_claims,
)
from legba.data.analysts.deterministic_handlers import (
    _external_audit_grader as width_grader,
)
from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR
from legba.data.provenance import external_grades as eg

MIGRATION_NAME = "0190_external_grades.sql"


def _row(**over):
    base = dict(
        claim_key="k-" + uuid4().hex,
        population="assembly_span",
        graded_output_id=uuid4(),
        origin_head_id=uuid4(),
        block_ordinal=1,
        span_role="bluf",
        analyst_id="country_composition",
        target_id="tr",
        desk_key="tr",
        claim_text="A thing happened in March.",
        claim_severity="high",
        assembly_regime="assembly",
        scope_bounded=False,
        absence_shaped=False,
        verdict="SUPPORTED",
        uncheckable_class=None,
        unchecked_reason=None,
        decisive_url="https://news.example/a",
        decisive_span="the thing happened",
        decisive_span_sha256="deadbeef",
        decisive_source_tier=1,
        archive_ref="grade://sha256/abc",
        source_urls=["https://news.example/a"],
        search_provider="searxng",
        search_status="completed",
        search_liveness="unverified",
        search_degraded=True,
        grader_family="google_gemma",
        grader_component_id="llm.judge.cerebras_gemma4_31b.openai_compat",
        grader_model_name="gemma-4-31b",
        grader_served_by="cerebras",
        grader_pipeline_version="2026-09-05/1",
        rubric_version="external_world_check/2026-09-05/1",
        rater_role="primary",
        sample_fraction=1.0,
    )
    base.update(over)
    return base


_INSERT = """
INSERT INTO external_grades (
    claim_key, population, graded_output_id, origin_head_id, block_ordinal,
    span_role, analyst_id, target_id, desk_key, claim_text, claim_severity,
    assembly_regime, scope_bounded, absence_shaped, verdict, uncheckable_class,
    unchecked_reason, decisive_url, decisive_span, decisive_span_sha256,
    decisive_source_tier, archive_ref, source_urls, search_provider,
    search_status, search_liveness, search_degraded, grader_family,
    grader_component_id, grader_model_name, grader_served_by,
    grader_pipeline_version, rubric_version, rater_role, sample_fraction
) VALUES (
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
    $21,$22,$23::text[],$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34,$35
)
"""

_COLS = [
    "claim_key", "population", "graded_output_id", "origin_head_id",
    "block_ordinal", "span_role", "analyst_id", "target_id", "desk_key",
    "claim_text", "claim_severity", "assembly_regime", "scope_bounded",
    "absence_shaped", "verdict", "uncheckable_class", "unchecked_reason",
    "decisive_url", "decisive_span", "decisive_span_sha256",
    "decisive_source_tier", "archive_ref", "source_urls", "search_provider",
    "search_status", "search_liveness", "search_degraded", "grader_family",
    "grader_component_id", "grader_model_name", "grader_served_by",
    "grader_pipeline_version", "rubric_version", "rater_role", "sample_fraction",
]


async def _insert(conn, row):
    await conn.execute(_INSERT, *[row[c] for c in _COLS])


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    # Scratch space this file exclusively owns for the duration of its own
    # assertions — no other writer in the tree touches external_grades under a
    # test, and the append-only guard means a teardown DELETE would RAISE, so
    # isolation comes from a unique claim_key per row, not a wipe.
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_the_table_and_its_guard_triggers_exist(conn):
    """The migration created the table and both mutation-forbidding triggers."""
    assert await conn.fetchval("SELECT to_regclass('public.external_grades')")
    triggers = {
        r["tgname"] for r in await conn.fetch(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = "
            "'public.external_grades'::regclass AND NOT tgisinternal"
        )
    }
    assert "trg_external_grades_forbid_update" in triggers
    assert "trg_external_grades_forbid_delete" in triggers


@pytest.mark.asyncio
async def test_a_grade_row_inserts(conn):
    row = _row()
    await _insert(conn, row)
    got = await conn.fetchval(
        "SELECT verdict FROM external_grades WHERE claim_key = $1",
        row["claim_key"],
    )
    assert got == "SUPPORTED"


@pytest.mark.asyncio
async def test_the_forbid_update_trigger_fires(conn):
    """An UPDATE RAISES — the append-only guard, on the UPDATE path."""
    row = _row()
    await _insert(conn, row)
    with pytest.raises(asyncpg.PostgresError) as exc:
        await conn.execute(
            "UPDATE external_grades SET verdict = 'CONTRADICTED' "
            "WHERE claim_key = $1",
            row["claim_key"],
        )
    assert "append-only" in str(exc.value)
    # And the row is untouched.
    assert await conn.fetchval(
        "SELECT verdict FROM external_grades WHERE claim_key = $1",
        row["claim_key"],
    ) == "SUPPORTED"


@pytest.mark.asyncio
async def test_the_forbid_delete_trigger_fires(conn):
    """A DELETE RAISES — the append-only guard, on the DELETE path."""
    row = _row()
    await _insert(conn, row)
    with pytest.raises(asyncpg.PostgresError) as exc:
        await conn.execute(
            "DELETE FROM external_grades WHERE claim_key = $1", row["claim_key"]
        )
    assert "append-only" in str(exc.value)
    assert await conn.fetchval(
        "SELECT count(*) FROM external_grades WHERE claim_key = $1",
        row["claim_key"],
    ) == 1


@pytest.mark.asyncio
async def test_the_unique_is_claim_pipeline_family(conn):
    """Same (claim, pipeline, family) collides; a DIFFERENT family does not.

    This is the row shape the double-grade rests on: a claim graded by two
    families is TWO rows, and collapsing them would delete the only evidence
    that two families were ever asked.
    """
    key = "k-" + uuid4().hex
    await _insert(conn, _row(claim_key=key, grader_family="google_gemma"))
    # Re-inserting the identical (claim, pipeline, family) violates the UNIQUE.
    with pytest.raises(asyncpg.UniqueViolationError):
        await _insert(conn, _row(claim_key=key, grader_family="google_gemma"))
    # A fourth-family audit rater row for the SAME claim lands fine.
    await _insert(conn, _row(
        claim_key=key, grader_family="meta_llama", rater_role="audit"
    ))
    assert await conn.fetchval(
        "SELECT count(*) FROM external_grades WHERE claim_key = $1", key
    ) == 2


@pytest.mark.asyncio
async def test_the_population_and_verdict_vocabularies_are_closed(conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(conn, _row(population="the_voice"))
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(conn, _row(verdict="MAYBE"))


@pytest.mark.asyncio
async def test_uncheckable_and_its_class_are_paired(conn):
    """UNCHECKABLE requires a class; every other verdict forbids one."""
    # UNCHECKABLE without a class -> the pair CHECK rejects it.
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(conn, _row(verdict="UNCHECKABLE", uncheckable_class=None))
    # SUPPORTED WITH a class -> also rejected.
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(conn, _row(
            verdict="SUPPORTED", uncheckable_class="perspective"
        ))
    # UNCHECKABLE with a class -> fine.
    await _insert(conn, _row(
        verdict="UNCHECKABLE", uncheckable_class="perspective",
        decisive_source_tier=None, decisive_url=None, decisive_span=None,
    ))


@pytest.mark.asyncio
async def test_reapplying_the_migration_is_idempotent(conn):
    """The runner re-globs every file; 0190 must survive a second application."""
    sql = (MIGRATIONS_DIR / MIGRATION_NAME).read_text(encoding="utf-8")
    # Twice more, in its own transaction each time (the runner owns the txn).
    await conn.execute(sql)
    await conn.execute(sql)
    assert await conn.fetchval("SELECT to_regclass('public.external_grades')")
    # The guard survived the re-CREATE OR REPLACE + DROP/CREATE TRIGGER idiom.
    row = _row()
    await _insert(conn, row)
    with pytest.raises(asyncpg.PostgresError):
        await conn.execute(
            "DELETE FROM external_grades WHERE claim_key = $1", row["claim_key"]
        )


@pytest.mark.asyncio
async def test_0190_is_in_the_applied_ledger(conn):
    """The migration ran as part of the standard set (not hand-applied here)."""
    present = await conn.fetchval(
        "SELECT count(*) FROM legba_data_migrations WHERE name = $1",
        MIGRATION_NAME,
    )
    assert present == 1


# ---------------------------------------------------------------------------
# THE LIVE DEFECT (first width sweep, 2026-09-05) — grader stamping on rows
# written BEFORE any grader call. `standing_auditor.width_ran` logged
# `drained=40 checked=3 ledger=19`: 21 of 40 writes failed
# `external_grades_grader_component_nonempty` because the degraded-search
# absence path (`absence_unverified_grade`) built its row with
# grader_family='' and grader_component_id='' — the exact shape these two
# CHECK constraints exist to reject. The fix threads `grade_one`'s already-
# resolved grader route into that constructor (and into `uncheckable_grade`,
# which had the identical bug). These two tests pin the fix against the REAL,
# migrated table rather than the constraint's SQL text alone.
# ---------------------------------------------------------------------------


def _absence_claim_and_envelope():
    """The live specimen, reconstructed: an absence-shaped claim, and the
    degraded/unverified envelope that makes the search plane refuse to grade
    it — `envelope.supports_absence_claim` False, `liveness` 'unverified',
    matching the sweep's DETAIL line verbatim (``search.searxng.local,
    degraded, unverified, t``)."""
    claim = width_claims.WidthClaim(
        claim_text="No reported disruption to the border crossing in March 2026.",
        population="assembly_span",
        graded_output_id=str(uuid4()),
        analyst_id="country_composition",
        target_id="tr", desk_key="tr",
        origin_head_id=str(uuid4()),
        start=0, end=10,
        claim_severity="moderate",
        absence_shaped=True,
    )
    envelope = width_grader.EvidenceEnvelope(
        query="border crossing disruption March 2026",
        results=(),
        search_status={"status": "empty", "liveness": "unverified",
                       "degraded": True, "supports_absence_claim": False},
        provider="search.searxng.local",
    )
    return claim, envelope


@pytest.mark.asyncio
async def test_an_absence_unverified_row_carries_the_configured_grader_and_inserts(
    conn,
):
    """FIXED shape: `absence_unverified_grade` stamped with the caller's
    resolved grader route inserts cleanly into the real `external_grades`
    table, with `grader_served_by` left unset — nobody answered."""
    claim, envelope = _absence_claim_and_envelope()
    grade = width_grader.absence_unverified_grade(
        claim, envelope,
        grader_family="mistral",
        grader_component_id="llm.judge.openrouter_mistral_large.openai_compat",
    )
    row = grade.as_dict()
    assert row["grader_family"] == "mistral"
    assert row["grader_component_id"] == (
        "llm.judge.openrouter_mistral_large.openai_compat"
    )

    landed = await eg.write_grade(
        conn, row, pipeline_version="2026-09-05/1",
        graded_at=datetime.now(timezone.utc),
    )
    assert landed is True

    db_row = await conn.fetchrow(
        "SELECT verdict, unchecked_reason, search_provider, search_liveness, "
        "search_degraded, grader_family, grader_component_id, grader_served_by "
        "FROM external_grades WHERE claim_key = $1",
        claim.key,
    )
    assert db_row["verdict"] == "UNCHECKED"
    assert db_row["unchecked_reason"] == "absence_liveness_unverified"
    assert db_row["search_provider"] == "search.searxng.local"
    assert db_row["search_liveness"] == "unverified"
    assert db_row["search_degraded"] is True
    assert db_row["grader_family"] == "mistral"
    assert db_row["grader_component_id"] == (
        "llm.judge.openrouter_mistral_large.openai_compat"
    )
    assert db_row["grader_served_by"] is None


@pytest.mark.asyncio
async def test_the_21_row_failure_shape_is_rejected_never_silently_written(conn):
    """Regression, pinned on the exact failure shape: a caller that (like the
    pre-fix code) omits the resolved route still produces grader_family=''/
    grader_component_id='' — `uncheckable_grade`/`absence_unverified_grade`
    default to it rather than inventing a value, on purpose, so a REGRESSION
    at the call site fails the same way it failed on 2026-09-05: loudly,
    at the database, as a rejected write — never as a silently accepted row
    under a hollow attribution.
    """
    claim, envelope = _absence_claim_and_envelope()
    bare = width_grader.absence_unverified_grade(claim, envelope).as_dict()
    assert bare["grader_family"] == ""
    assert bare["grader_component_id"] == ""

    landed = await eg.write_grade(
        conn, bare, pipeline_version="2026-09-05/1",
        graded_at=datetime.now(timezone.utc),
    )
    # write_grade() never raises — "one malformed grade must not cost the tick
    # its other rows" — it reports the CHECK violation as a skip.
    assert landed is False
    assert await conn.fetchval(
        "SELECT count(*) FROM external_grades WHERE claim_key = $1", claim.key,
    ) == 0

    # The raw SQL path (what the DETAIL line in the incident actually showed)
    # hits the same two named constraints directly.
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await _insert(conn, _row(
            claim_key=claim.key, verdict="UNCHECKED", uncheckable_class=None,
            unchecked_reason="absence_liveness_unverified",
            decisive_url=None, decisive_span=None, decisive_source_tier=None,
            grader_family="", grader_component_id="",
        ))
    assert "external_grades_grader_family_nonempty" in str(exc.value) or (
        "external_grades_grader_component_nonempty" in str(exc.value)
    )


# ---------------------------------------------------------------------------
# THE LIVE DEFECT (23:07Z sweep) — `decisive_published_at`. 14 of 40 writes
# failed with `invalid input for query argument $22: '2026-07' (expected a
# datetime.date or datetime.datetime instance, got 'str')` — the writer
# passed the search result's RAW published-date string straight into a
# `timestamptz` column. `write_grade()` now normalizes it (or None) before
# the INSERT; these two tests pin the fix against the REAL, migrated table.
# ---------------------------------------------------------------------------


def _dated_row(**over):
    """A full `write_grade()`-shaped row (unlike `_row()` above, which feeds
    the reduced raw-SQL `_INSERT`): every key `write_grade` reads, so the
    normalization boundary under test runs exactly as it does in production.
    """
    base = dict(
        claim_key="k-" + uuid4().hex,
        population="assembly_span",
        graded_output_id=uuid4(),
        origin_head_id=uuid4(),
        block_ordinal=1,
        span_role="bluf",
        analyst_id="country_composition",
        target_id="tr",
        desk_key="tr",
        claim_text="No reported disruption to the border crossing.",
        claim_severity="moderate",
        assembly_regime="assembly",
        scope_bounded=False,
        absence_shaped=True,
        verdict="NOT_FOUND",
        uncheckable_class=None,
        unchecked_reason="out_of_window",
        decisive_url=None,
        decisive_span=None,
        decisive_span_sha256=None,
        decisive_source_tier=None,
        decisive_published_at=None,
        archive_ref=None,
        source_urls=[],
        search_provider="search.searxng.local",
        search_status="completed",
        search_liveness="unverified",
        search_degraded=True,
        grader_family="mistral",
        grader_component_id="llm.judge.openrouter_mistral_large.openai_compat",
        grader_model_name=None,
        grader_served_by=None,
        rubric_version="external_world_check/2026-09-05/1",
        rater_role="primary",
        retrieval_origin_mix={},
        read_evidence_window={"earliest": "2026-08-24", "latest": "2026-09-05"},
        sample_fraction=1.0,
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_a_not_found_row_carrying_1_day_ago_inserts_cleanly(conn):
    """The incident's own specimen: a NOT_FOUND row whose search result
    carried `decisive_published_at='1 day ago'` — one of the 14 dated rows
    the 23:07Z sweep lost. Must land, with a real `datetime` in the column
    (resolved against the tick's `graded_at`, not wall-clock `now()`), and
    the raw string kept honest under `read_evidence_window.decisive_published`
    without disturbing the read's own `earliest`/`latest` window.
    """
    graded_at = datetime(2026, 9, 5, 23, 7, 0, tzinfo=timezone.utc)
    row = _dated_row(decisive_published_at="1 day ago")

    landed = await eg.write_grade(
        conn, row, pipeline_version="2026-09-05/1", graded_at=graded_at,
    )
    assert landed is True

    db_row = await conn.fetchrow(
        "SELECT verdict, decisive_published_at, read_evidence_window "
        "FROM external_grades WHERE claim_key = $1",
        row["claim_key"],
    )
    assert db_row["verdict"] == "NOT_FOUND"
    assert db_row["decisive_published_at"] == graded_at - timedelta(days=1)
    window = json.loads(db_row["read_evidence_window"])
    assert window["earliest"] == "2026-08-24" and window["latest"] == "2026-09-05"
    assert window["decisive_published"] == {
        "published_raw": "1 day ago",
        "published_precision": "relative",
    }


@pytest.mark.asyncio
async def test_every_observed_shape_inserts_cleanly_with_the_right_precision(conn):
    """All 12 distinct raw strings the sweep actually logged (`'1 day ago'`
    occurred twice), each on its own row, each landing with the expected
    datetime and precision tag."""
    graded_at = datetime(2026, 9, 5, 23, 7, 0, tzinfo=timezone.utc)
    cases = [
        ("2026-09-05", datetime(2026, 9, 5, tzinfo=timezone.utc), "day"),
        ("1 day ago", datetime(2026, 9, 4, 23, 7, 0, tzinfo=timezone.utc), "relative"),
        ("2026-07", datetime(2026, 7, 1, tzinfo=timezone.utc), "month"),
        ("Aug 20, 2026", datetime(2026, 8, 20, tzinfo=timezone.utc), "day"),
        ("2025-11-22", datetime(2025, 11, 22, tzinfo=timezone.utc), "day"),
        ("Jan 1, 2026", datetime(2026, 1, 1, tzinfo=timezone.utc), "day"),
        ("2026-09", datetime(2026, 9, 1, tzinfo=timezone.utc), "month"),
        ("Feb 27, 2026", datetime(2026, 2, 27, tzinfo=timezone.utc), "day"),
        ("Jul 12, 2026", datetime(2026, 7, 12, tzinfo=timezone.utc), "day"),
        ("2026-09-01", datetime(2026, 9, 1, tzinfo=timezone.utc), "day"),
        ("Oct 25, 2025", datetime(2025, 10, 25, tzinfo=timezone.utc), "day"),
        ("2026-09-02", datetime(2026, 9, 2, tzinfo=timezone.utc), "day"),
    ]
    for raw, expected_dt, expected_precision in cases:
        row = _dated_row(decisive_published_at=raw)
        landed = await eg.write_grade(
            conn, row, pipeline_version="2026-09-05/1", graded_at=graded_at,
        )
        assert landed is True, f"row carrying {raw!r} failed to land"
        db_row = await conn.fetchrow(
            "SELECT decisive_published_at, read_evidence_window "
            "FROM external_grades WHERE claim_key = $1",
            row["claim_key"],
        )
        assert db_row["decisive_published_at"] == expected_dt, raw
        window = json.loads(db_row["read_evidence_window"])
        assert window["decisive_published"]["published_raw"] == raw
        assert window["decisive_published"]["published_precision"] == expected_precision


@pytest.mark.asyncio
async def test_a_raw_published_date_string_is_rejected_by_asyncpg_confirming_root_cause(
    conn,
):
    """Reproduces the incident mechanism directly, at the database: asyncpg's
    own encoder refuses a `str` for a `timestamptz` positional argument. This
    is the exact class of failure `external_grades.write_failed` logged for
    all 14 dated rows on the 23:07Z sweep, and it is WHY the fix normalizes
    the value in Python before the call rather than touching the column type
    or the constraint (both stay untouched — see migration 0190).
    """
    with pytest.raises((asyncpg.exceptions.DataError, asyncpg.exceptions.InterfaceError)):
        await conn.execute(
            "INSERT INTO external_grades ("
            "claim_key, population, graded_output_id, analyst_id, claim_text, "
            "verdict, grader_family, grader_component_id, "
            "grader_pipeline_version, rubric_version, decisive_published_at"
            ") VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::timestamptz)",
            "k-" + uuid4().hex, "assembly_span", uuid4(), "country_composition",
            "x", "NOT_FOUND", "mistral", "llm.judge.mistral",
            "external_world_check/2026-09-05/1", "external_world_check/2026-09-05/1",
            "2026-07",  # the exact raw value from the incident's DETAIL line
        )


@pytest.mark.asyncio
async def test_write_grade_never_raises_on_an_unparseable_published_at(conn):
    """`write_grade()`'s own contract — never raise for one malformed field —
    holds for a published-date value this parser cannot read: the row still
    lands, with `decisive_published_at IS NULL` rather than a skipped write."""
    row = _dated_row(decisive_published_at="not a date at all")
    landed = await eg.write_grade(
        conn, row, pipeline_version="2026-09-05/1",
        graded_at=datetime.now(timezone.utc),
    )
    assert landed is True
    db_row = await conn.fetchrow(
        "SELECT decisive_published_at, read_evidence_window "
        "FROM external_grades WHERE claim_key = $1",
        row["claim_key"],
    )
    assert db_row["decisive_published_at"] is None
    window = json.loads(db_row["read_evidence_window"])
    assert window["decisive_published"] == {
        "published_raw": "not a date at all",
        "published_precision": "unparsed",
    }


# ---------------------------------------------------------------------------
# `write_grades()`'s per-row outcomes, against the REAL constraint. The fix's
# other half: a caller (`run_width_tick`) needs to tell a landed claim from a
# failed one to requeue only the failed ones — see
# `_external_audit_queue.requeue_failed_writes`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_grades_names_a_real_check_violation_as_the_failed_outcome(
    conn,
):
    """The exact incident mechanism (grader_family='' trips
    ``external_grades_grader_family_nonempty``), but through `write_grades`
    instead of `write_grade`: the failed row's outcome names the REAL
    exception class asyncpg raised, so a caller can log and requeue with a
    reason that traces back to an actual database error, not a guess."""
    bad = _dated_row(grader_family="", grader_component_id="")
    good = _dated_row()
    written, skipped, outcomes = await eg.write_grades(
        conn, [bad, good], pipeline_version="2026-09-05/1",
        graded_at=datetime.now(timezone.utc),
    )
    assert written == 1 and skipped == 1
    assert outcomes[0].claim_key == bad["claim_key"]
    assert outcomes[0].landed is False
    assert outcomes[0].error_class == "CheckViolationError"
    assert outcomes[1].claim_key == good["claim_key"]
    assert outcomes[1].landed is True and outcomes[1].error_class == ""

    assert await conn.fetchval(
        "SELECT count(*) FROM external_grades WHERE claim_key = $1", bad["claim_key"],
    ) == 0
    assert await conn.fetchval(
        "SELECT count(*) FROM external_grades WHERE claim_key = $1", good["claim_key"],
    ) == 1


@pytest.mark.asyncio
async def test_write_grades_treats_an_idempotent_conflict_as_landed_not_failed(
    conn,
):
    """A re-run of the SAME (claim_key, pipeline, family) triple hits the
    real UNIQUE and lands zero new rows — but it must NOT be reported as a
    failure: the claim genuinely is graded (the first write already landed
    it), and a caller that requeued it on this signal would grade it again
    for nothing, forever."""
    row = _dated_row(claim_key="k-" + uuid4().hex)
    pipeline = "2026-09-05/1"
    graded_at = datetime.now(timezone.utc)

    first_written, first_skipped, first_outcomes = await eg.write_grades(
        conn, [row], pipeline_version=pipeline, graded_at=graded_at,
    )
    assert first_written == 1 and first_skipped == 0
    assert first_outcomes[0].landed is True

    second_written, second_skipped, second_outcomes = await eg.write_grades(
        conn, [row], pipeline_version=pipeline, graded_at=graded_at,
    )
    assert second_written == 0 and second_skipped == 1  # skipped count unchanged
    assert second_outcomes[0].claim_key == row["claim_key"]
    assert second_outcomes[0].landed is True  # but NOT a failure
    assert second_outcomes[0].error_class == ""

    assert await conn.fetchval(
        "SELECT count(*) FROM external_grades WHERE claim_key = $1", row["claim_key"],
    ) == 1  # still exactly one row — the conflict added nothing, as designed
