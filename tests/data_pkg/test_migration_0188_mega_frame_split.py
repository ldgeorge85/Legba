# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0188 — THE MEGA-FRAME SPLIT, against a really migrated Postgres.

Everything here runs against real SQL on a real substrate, and that is not
ceremony. This migration's four guarantees are all properties of the DATABASE:
it must be idempotent under a runner that re-globs every file, it must not
violate a partial unique index it writes straight into, it must partition a uuid
array without losing or duplicating a member, and it must leave no
``situation_events`` row pointing at a frame that no longer exists — in a table
whose UPDATE and DELETE paths both raise. A test that mocked the connection
would assert only that the Python called the SQL it was written to call.

Sections:
  1. the token twin — Python and SQL must spell a dimension identically, because
     a disagreement is a duplicate frame under ``uq_situations_signature_analyst``;
  2. the split shape, on the AR mega-frame's real 364-member census;
  3. member conservation + intensity-mass conservation;
  4. idempotency + unique-index safety;
  5. the ledger: no orphans, and the id lands where the evidence is.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import finding_supersession as fs
from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR
from legba.data.situations import trajectory as tj

MIGRATION_NAME = "0188_situation_mega_frame_split.sql"
MIGRATION_SQL = (MIGRATIONS_DIR / MIGRATION_NAME).read_text(encoding="utf-8")

#: The AR frame's shape at the H1 census: 364 members across the desk's units,
#: 42 of them the maritime-pilots story. The split's job is to stop those 364
#: being ONE situation.
_AR_MEMBERS = 364
_AR_PILOT_MEMBERS = 42

#: The seven units a country desk runs, plus the pilots-titled one. These are
#: real registered analyst ids — the point of the re-key is that the vocabulary
#: is the REGISTRY's and therefore closed, not the model's.
_AR_DESKS = [
    "internal_stability",
    "military_posture",
    "economic_coercion",
    "energy_security",
    "narrative_coordination",
    "leadership_transition",
    "proliferation_watch",
]


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    await p.close()


async def _run_migration(conn: Any) -> None:
    """Apply 0188's SQL directly.

    Directly, and not through ``apply_primary_migrations``: the runner records
    the file in ``legba_data_migrations`` and then SKIPS it forever, so a
    re-run through the runner would prove that the ledger works rather than that
    the migration is idempotent. Idempotency is a property of the SQL and has to
    be tested as one.
    """
    await conn.execute(MIGRATION_SQL)


async def _finding(
    conn: Any, *, analyst_id: str | None, target_id: str, title: str,
    hours_ago: float, signature: str | None,
) -> UUID:
    fid = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, confidence, data, analyst_id, target_id,
             produced_at, schema_uri, situation_signature)
        VALUES ($1, 'finding', $2, '', 0.9, '{}'::jsonb, $3, $4, $5,
                'iglu:legba/finding/jsonschema/1-0-0', $6)
        """,
        fid, title, analyst_id, target_id,
        datetime.now(timezone.utc) - timedelta(hours=hours_ago), signature,
    )
    return fid


async def _situation(
    conn: Any, *, signature: str, members: list[UUID], target_id: str,
    intensity: float = 59.34, status: str = "active",
    opened_days_ago: float = 77.8, analyst_id: str = "situation_clustering",
) -> UUID:
    opened = datetime.now(timezone.utc) - timedelta(days=opened_days_ago)
    return await conn.fetchval(
        """
        INSERT INTO situations
            (id, data, name, status, category, last_event_at, event_count,
             intensity_score, target_id, analyst_id, derived_from, schema_uri,
             situation_signature, valid_from)
        VALUES ($1, $2::jsonb, $3, $4, $5, now(), $6, $7, $8, $9, $10,
                'iglu:legba/situation/jsonschema/2-0-0', $11, $12)
        RETURNING id
        """,
        uuid4(),
        json.dumps({
            "situation_signature": signature,
            "member_finding_ids": [str(m) for m in members],
            "sub_handler": "situation_clustering",
            "last_corroborated_at": opened.isoformat(),
            "corroboration_count": 4,
            "persistence": 0.3,
        }),
        "Argentina – maritime pilots' strike halts grain exports",
        status,
        signature[4:].split("|", 1)[0],
        len(members), intensity, target_id, analyst_id, members, signature,
        opened,
    )


async def _seed_ar_mega_frame(conn: Any, target: str) -> tuple[UUID, list[UUID]]:
    """The AR frame as the census found it: ONE row, 364 members, seven units.

    Members are dealt round-robin across the desk's units so every unit owns a
    real share, then the pilots-titled block is concentrated in
    ``internal_stability`` — which is what makes the keeper choice checkable
    below (that unit also owns the ledger's cited evidence).
    """
    signature = f"sig:{target}"
    members: list[UUID] = []
    for i in range(_AR_MEMBERS):
        if i < _AR_PILOT_MEMBERS:
            analyst_id = "internal_stability"
            title = "Argentina – maritime pilot strike drives escalation risk"
        else:
            analyst_id = _AR_DESKS[i % len(_AR_DESKS)]
            title = "Argentina – No observable coercive economic pressure"
        members.append(await _finding(
            conn, analyst_id=analyst_id, target_id=target, title=title,
            hours_ago=1 + i * 0.5, signature=signature,
        ))
    sid = await _situation(
        conn, signature=signature, members=members, target_id=target,
    )
    return sid, members


async def _frames(conn: Any, target: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, situation_signature, derived_from, event_count,
               intensity_score, status, name, category, target_id,
               valid_from, data
          FROM situations
         WHERE target_id = $1
         ORDER BY situation_signature
        """,
        target,
    )
    return [dict(r) for r in rows]


@pytest_asyncio.fixture
async def desk(pool):
    """A target id no other row in the shared substrate can carry.

    Nothing is deleted on teardown: ``hypotheses`` and the append-only
    ``situation_events`` both reference ``situations``, and the tracker test file
    next door records what an unscoped wipe of this table did to the 08-06
    nightly. The frames are CLOSED instead, which is what every open-frame reader
    in the tower filters on.
    """
    target = f"country_split_{uuid4().hex[:10]}"
    yield pool, target
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE situations SET status = 'closed' WHERE target_id = $1", target,
        )


# ---------------------------------------------------------------------------
# 1 — the token twin
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dimension_token_python_and_sql_agree(pool):
    """THE DRIFT THAT WOULD SILENTLY UNDO THE REPAIR.

    The migration computes the dimension token in Postgres; every live tick
    computes it in Python. A value the two spell differently does not fail — it
    produces a SECOND frame for the same dimension under
    ``uq_situations_signature_analyst``, which is the exact duplicate-frame
    outcome the whole re-key exists to remove. So the twin is asserted
    row-for-row, including on inputs designed to break either implementation.
    """
    cases = [
        "internal_stability", "military_posture", "country_assessor",
        "Energy_Security", "  narrative_coordination  ", "MiXeD_CaSe",
        "has#hash", "has|pipe", "both#and|", "", "   ", "\tleading_tab",
        "x" * 80, "a-b.c_d", "ünïcödé_desk",
    ]
    async with pool.acquire() as conn:
        for raw in cases:
            got = await conn.fetchval(
                """
                SELECT CASE
                         WHEN btrim(coalesce($1, ''), E' \t\n\r\f\v') = ''
                           THEN '_unattributed'
                         ELSE left(
                                translate(lower(btrim($1, E' \t\n\r\f\v')),
                                          '#|', '__'),
                                64)
                       END
                """,
                raw,
            )
            assert got == fs.dimension_token(raw), (
                f"token drift on {raw!r}: SQL={got!r} Python={fs.dimension_token(raw)!r}"
            )
        # NULL analyst_id — the migration's real path for an unattributed member.
        got_null = await conn.fetchval(
            """
            SELECT CASE
                     WHEN btrim(coalesce($1::text, ''), E' \t\n\r\f\v') = ''
                       THEN '_unattributed' ELSE 'x' END
            """,
            None,
        )
        assert got_null == fs.dimension_token(None) == fs.UNATTRIBUTED_DIMENSION


# ---------------------------------------------------------------------------
# 2 — the AR mega-frame case
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_ar_mega_frame_splits_into_one_frame_per_dimension(desk):
    """THE HEADLINE, on the census's own numbers.

    One row holding 364 members from seven units becomes seven rows, one per
    unit, each keyed ``sig:<target>#dim:<unit>`` — and every one keeps its
    country home, because ``target_id`` is resolved from the topic BEFORE the
    marker and a split frame that lost it would vanish from its own desk's
    grounding read.
    """
    pool, target = desk
    async with pool.acquire() as conn:
        parent_id, members = await _seed_ar_mega_frame(conn, target)
        before = await _frames(conn, target)
        assert len(before) == 1
        assert before[0]["situation_signature"] == f"sig:{target}"
        assert len(before[0]["derived_from"]) == _AR_MEMBERS

        await _run_migration(conn)

        after = await _frames(conn, target)
        assert len(after) == len(_AR_DESKS), (
            f"expected one frame per unit, got {len(after)}: "
            f"{[f['situation_signature'] for f in after]}"
        )
        assert {f["situation_signature"] for f in after} == {
            f"sig:{target}#dim:{d}" for d in _AR_DESKS
        }
        # The country home survives the re-key on EVERY split frame.
        for frame in after:
            assert frame["category"] == target
            assert frame["target_id"] == target
        # The parent row is still here, still its own id, now keyed to one unit.
        parent = [f for f in after if f["id"] == parent_id]
        assert len(parent) == 1
        assert parent[0]["situation_signature"].startswith(f"sig:{target}#dim:")
        # The pilots block is 42 of the 364 and belongs to ONE unit now, instead
        # of being 12% of an undifferentiated country blob.
        stability = [
            f for f in after
            if f["situation_signature"] == f"sig:{target}#dim:internal_stability"
        ][0]
        pilots = await conn.fetchval(
            "SELECT count(*) FROM analyst_outputs "
            "WHERE id = ANY($1::uuid[]) AND title LIKE '%pilot strike%'",
            list(stability["derived_from"]),
        )
        assert pilots == _AR_PILOT_MEMBERS


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unattributed_member_gets_its_own_frame_not_someone_elses(desk):
    """``analyst_id`` is nullable and a member's producing row can be gone. Such
    a member is a fact about our bookkeeping, not evidence about a dimension it
    was never produced by, so it is never folded into one."""
    pool, target = desk
    async with pool.acquire() as conn:
        signature = f"sig:{target}"
        attributed = [
            await _finding(conn, analyst_id="military_posture", target_id=target,
                           title="posture read", hours_ago=i, signature=signature)
            for i in range(1, 4)
        ]
        orphan = uuid4()  # no analyst_outputs row AT ALL
        nameless = await _finding(
            conn, analyst_id=None, target_id=target, title="nameless",
            hours_ago=5, signature=signature,
        )
        members = attributed + [orphan, nameless]
        await _situation(
            conn, signature=signature, members=members, target_id=target,
        )
        await _run_migration(conn)

        after = {f["situation_signature"]: f for f in await _frames(conn, target)}
        assert set(after) == {
            f"sig:{target}#dim:military_posture",
            f"sig:{target}#dim:{fs.UNATTRIBUTED_DIMENSION}",
        }
        unattributed = after[f"sig:{target}#dim:{fs.UNATTRIBUTED_DIMENSION}"]
        assert set(unattributed["derived_from"]) == {orphan, nameless}


# ---------------------------------------------------------------------------
# 3 — conservation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_split_conserves_members_and_intensity_mass(desk):
    """A split that lost or duplicated a member would be a provenance break in
    the array the tracker and ``hypothesis_lifecycle`` both join on.

    Intensity is conserved too, and that one is an operational property rather
    than a bookkeeping nicety: a frame paging at 59 must not become seven frames
    paging at 59 for the twenty minutes before the next tick re-derives them.
    """
    pool, target = desk
    async with pool.acquire() as conn:
        parent_id, members = await _seed_ar_mega_frame(conn, target)
        before = (await _frames(conn, target))[0]
        members_before = list(before["derived_from"])
        intensity_before = float(before["intensity_score"])
        assert len(members_before) == _AR_MEMBERS

        await _run_migration(conn)
        after = await _frames(conn, target)

        seen: list[UUID] = []
        for frame in after:
            seen.extend(frame["derived_from"])
            # event_count is re-based onto the array it actually holds.
            assert frame["event_count"] == len(frame["derived_from"])
        assert len(seen) == len(members_before), "member count changed"
        assert sorted(str(m) for m in seen) == sorted(str(m) for m in members_before)

        intensity_after = sum(float(f["intensity_score"]) for f in after)
        assert intensity_after == pytest.approx(intensity_before, rel=1e-3)
        # ...and no single split frame outranks the frame it came out of.
        assert max(float(f["intensity_score"]) for f in after) < intensity_before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_split_frame_inherits_the_parents_opening(desk):
    """The split does not reset any frame's clock.

    ``valid_from`` is the anchor H1 decays a never-corroborated frame from, and
    it is the one the #64 dormancy re-key keys the tracker's horizon on. A child
    that took its own earliest member would look YOUNGER than the history it
    inherited — a 77-day-old blob would become seven fresh-looking frames, and
    the fortnight horizon would restart for every one of them.
    """
    pool, target = desk
    async with pool.acquire() as conn:
        parent_id, _ = await _seed_ar_mega_frame(conn, target)
        parent_before = (await _frames(conn, target))[0]
        opened = parent_before["valid_from"]
        await _run_migration(conn)
        for frame in await _frames(conn, target):
            assert frame["valid_from"] == opened, frame["situation_signature"]
        # And the anchor is genuinely past the tracker's fortnight horizon, so
        # every split frame is eligible for a dormancy verdict at once rather
        # than serving another two weeks of manufactured youth.
        age = datetime.now(timezone.utc) - opened
        assert age > timedelta(days=tj.DORMANCY_DAYS)


# ---------------------------------------------------------------------------
# 4 — idempotency + the unique index
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_migration_is_idempotent(desk):
    """Re-running changes nothing. Every statement is guarded by the ABSENCE of
    the marker every statement adds, so a second pass selects no rows — which
    matters because a partial failure, an operator re-run, or a restored backup
    all replay this file against a substrate it has already touched."""
    pool, target = desk
    async with pool.acquire() as conn:
        await _seed_ar_mega_frame(conn, target)
        await _run_migration(conn)
        first = await _frames(conn, target)
        first_key = {
            f["situation_signature"]: (
                sorted(str(m) for m in f["derived_from"]),
                f["event_count"],
                round(float(f["intensity_score"]), 4),
                str(f["id"]),
            )
            for f in first
        }

        await _run_migration(conn)
        await _run_migration(conn)

        second = await _frames(conn, target)
        second_key = {
            f["situation_signature"]: (
                sorted(str(m) for m in f["derived_from"]),
                f["event_count"],
                round(float(f["intensity_score"]), 4),
                str(f["id"]),
            )
            for f in second
        }
        assert first_key == second_key
        # No doubled dimension tail — `with_dimension` and the SQL both refuse to
        # re-key a key that already carries one.
        for sig in second_key:
            assert sig.count("#dim:") == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_split_never_violates_the_unique_index(desk):
    """``uq_situations_signature_analyst`` is a PARTIAL unique index on
    ``(situation_signature, analyst_id)`` and this migration writes straight into
    it. The hostile case is a substrate where the live upsert has ALREADY minted
    a dimensioned frame for one of the dimensions about to be split out — the
    migration must yield to it, not collide with it."""
    pool, target = desk
    async with pool.acquire() as conn:
        signature = f"sig:{target}"
        posture = [
            await _finding(conn, analyst_id="military_posture", target_id=target,
                           title="posture", hours_ago=i, signature=signature)
            for i in range(1, 4)
        ]
        energy = [
            await _finding(conn, analyst_id="energy_security", target_id=target,
                           title="energy", hours_ago=i, signature=signature)
            for i in range(4, 9)
        ]
        await _situation(
            conn, signature=signature, members=posture + energy, target_id=target,
        )
        # The live path got there first for ONE dimension.
        incumbent_member = await _finding(
            conn, analyst_id="military_posture", target_id=target,
            title="already dimensioned", hours_ago=0.5,
            signature=f"{signature}#dim:military_posture",
        )
        incumbent = await _situation(
            conn, signature=f"{signature}#dim:military_posture",
            members=[incumbent_member], target_id=target, intensity=1.0,
        )

        await _run_migration(conn)  # must not raise

        after = {f["situation_signature"]: f for f in await _frames(conn, target)}
        # The incumbent is untouched — it belongs to the live writer.
        assert after[f"{signature}#dim:military_posture"]["id"] == incumbent
        assert list(
            after[f"{signature}#dim:military_posture"]["derived_from"]
        ) == [incumbent_member]
        # Exactly one row per key, still.
        dupes = await conn.fetchval(
            """
            SELECT count(*) FROM (
                SELECT situation_signature, analyst_id
                  FROM situations WHERE target_id = $1
                 GROUP BY 1, 2 HAVING count(*) > 1
            ) d
            """,
            target,
        )
        assert dupes == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_single_dimension_frame_is_re_keyed_but_not_split(desk):
    """A real population, not a corner: the census's 19 non-country `sig:*`
    frames (shipping lanes, flows, severity buckets, regions) can have one
    producer. Such a frame is re-keyed in place and nothing else moves — no child
    rows, no intensity change, and the hypothesis re-base correctly declines
    because there is no scale change to guard against."""
    pool, target = desk
    async with pool.acquire() as conn:
        signature = f"sig:{target}"
        members = [
            await _finding(conn, analyst_id="disruption_status", target_id=target,
                           title=f"lane read {i}", hours_ago=i, signature=signature)
            for i in range(1, 5)
        ]
        parent_id = await _situation(
            conn, signature=signature, members=members, target_id=target,
            intensity=12.5,
        )
        hyp = await conn.fetchval(
            """
            INSERT INTO hypotheses
                (id, situation_id, thesis, diagnostic_evidence, target_id,
                 schema_uri)
            VALUES ($1, $2, 'the lane stays disrupted', $3::jsonb, $4,
                    'iglu:legba/hypothesis/jsonschema/2-0-0')
            RETURNING id
            """,
            uuid4(), parent_id, json.dumps([{"intensity_at_emit": 12.5}]), target,
        )

        await _run_migration(conn)

        after = await _frames(conn, target)
        assert len(after) == 1
        assert after[0]["id"] == parent_id
        assert after[0]["situation_signature"] == f"{signature}#dim:disruption_status"
        assert set(after[0]["derived_from"]) == set(members)
        # Share is 1.0 — the frame kept all its members, so intensity is
        # untouched and the snapshot must NOT be rewritten.
        assert float(after[0]["intensity_score"]) == pytest.approx(12.5)
        assert json.loads(after[0]["data"])["split_from"]["split_share"] == 1
        entry = json.loads(await conn.fetchval(
            "SELECT diagnostic_evidence FROM hypotheses WHERE id = $1", hyp,
        ))[0]
        assert "rebased_by" not in entry
        assert float(entry["intensity_at_emit"]) == pytest.approx(12.5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_closed_and_superseded_frames_are_left_alone(desk):
    """Scope: OPEN frames only. A closed frame is settled history and its legacy
    key is a correct record of what was materialized at the time; re-keying
    history is what the CREATE-only policy exists to prevent."""
    pool, target = desk
    async with pool.acquire() as conn:
        signature = f"sig:{target}"
        member = await _finding(
            conn, analyst_id="military_posture", target_id=target,
            title="old", hours_ago=900, signature=signature,
        )
        closed = await _situation(
            conn, signature=signature, members=[member], target_id=target,
            status="closed",
        )
        await _run_migration(conn)
        row = await conn.fetchrow(
            "SELECT situation_signature FROM situations WHERE id = $1", closed,
        )
        assert row["situation_signature"] == signature


# ---------------------------------------------------------------------------
# 5 — the ledger
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_trajectory_row_is_orphaned_and_the_id_follows_the_evidence(desk):
    """WHY THE PARENT KEEPS ITS ID, proved on the table that makes it necessary.

    ``situation_events`` is append-only ENFORCED — migration 0184 installs
    triggers that raise on both UPDATE and DELETE — so a ledger row cannot be
    re-homed to a different frame at any price. The split therefore has exactly
    one way to avoid orphaning it: leave the parent id in place. Which DIMENSION
    inherits that id is then a provenance question, and the migration answers it
    with the dimension that produced the plurality of the evidence the ledger
    actually cited.
    """
    pool, target = desk
    async with pool.acquire() as conn:
        parent_id, members = await _seed_ar_mega_frame(conn, target)

        # THE TEST DISCRIMINATES, and that is arranged deliberately. The seeding
        # makes `internal_stability` the LARGEST bucket (88 members — the 42
        # pilots plus its round-robin share, against 46 for every other unit), so
        # a plurality-of-MEMBERS keeper rule would choose it. The ledger instead
        # cites `proliferation_watch`, one of the smallest. If the migration
        # picked its keeper by member count this assertion fails; only the
        # cited-evidence rule puts the id where the ledger's own rows point.
        cited_dim = "proliferation_watch"
        cited_members = [
            r["id"] for r in await conn.fetch(
                "SELECT id FROM analyst_outputs WHERE id = ANY($1::uuid[]) "
                "AND analyst_id = $2 ORDER BY produced_at LIMIT 3",
                members, cited_dim,
            )
        ]
        assert len(cited_members) == 3
        for i, cited in enumerate(cited_members):
            await tj.record_situation_events(
                conn,
                events=[tj.TrajectoryEvent(
                    situation_id=parent_id,
                    occurred_at=datetime.now(timezone.utc) - timedelta(days=3 + i),
                    delta=tj.DELTA_ESCALATES,
                    why="the cited item reports a new proliferation indicator",
                    state_from=tj.STATE_WATCHING,
                    state_to=tj.STATE_ESCALATING,
                    derived_from=(cited,),
                )],
                source_output_id=uuid4(),
                verification={"faithfulness_score": 0.9},
            )
        ledger_before = await conn.fetchval(
            "SELECT count(*) FROM situation_events WHERE situation_id = $1",
            parent_id,
        )
        assert ledger_before == 3

        await _run_migration(conn)

        # (a) every ledger row still points at a frame that exists.
        orphans = await conn.fetchval(
            """
            SELECT count(*) FROM situation_events e
             WHERE e.situation_id = $1
               AND NOT EXISTS (SELECT 1 FROM situations s WHERE s.id = e.situation_id)
            """,
            parent_id,
        )
        assert orphans == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM situation_events WHERE situation_id = $1",
            parent_id,
        ) == ledger_before

        # (b) the id stayed with the dimension whose findings the ledger cited —
        # NOT with the largest bucket, which is the whole point.
        keeper = await conn.fetchrow(
            "SELECT situation_signature, data, event_count FROM situations "
            "WHERE id = $1",
            parent_id,
        )
        assert keeper["situation_signature"] == f"sig:{target}#dim:{cited_dim}"
        assert json.loads(keeper["data"])["split_from"]["kept_ledger"] is True
        biggest = max(
            (f["event_count"], f["situation_signature"])
            for f in await _frames(conn, target)
        )
        assert biggest[1] == f"sig:{target}#dim:internal_stability"
        assert keeper["event_count"] < biggest[0], (
            "the keeper must be chosen by cited evidence, not by member count"
        )

        # (c) every OTHER split frame can be walked back to that ledger.
        for frame in await _frames(conn, target):
            data = json.loads(frame["data"])
            if frame["id"] == parent_id:
                continue
            assert data["trajectory_parent_id"] == str(parent_id)
            assert data["split_from"]["parent_signature"] == f"sig:{target}"
            assert data["split_from"]["migration"] == "0188"
            # A child never inherits the parent's evidence-clock stamps: it has
            # no ledger of its own, and carrying `last_corroborated_at` onto a
            # frame the ledger has never moved is the exact substitution H1
            # exists to forbid.
            assert "last_corroborated_at" not in data


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_split_does_not_mass_refute_the_hypothesis_plane(desk):
    """THE SEMANTICS-MIGRATION GUARD, and the largest thing this file could have
    broken.

    ``hypothesis_lifecycle._test_standing_hypotheses`` adjudicates by comparing
    the frame's intensity NOW against the ``intensity_at_emit`` snapshotted when
    the hypothesis was minted, and a drop past ``_INTENSITY_MOVE_EPS`` (0.25)
    appends every later member to ``refuting_signals``. The split divides a
    frame's intensity by its member share — 59 becomes about 8 — so every
    standing hypothesis on every split frame would have read a ~50-point collapse
    with no world event behind it. 4,405 live hypotheses carry a
    ``situation_id``.

    So the snapshot is re-based by the SAME share the frame kept: the comparison
    stops straddling a scale change, and the migration's verdict-moving power
    over the lifecycle is zero.
    """
    pool, target = desk
    async with pool.acquire() as conn:
        parent_id, members = await _seed_ar_mega_frame(conn, target)
        parent = (await _frames(conn, target))[0]
        intensity_before = float(parent["intensity_score"])

        hyp = await conn.fetchval(
            """
            INSERT INTO hypotheses
                (id, situation_id, thesis, diagnostic_evidence, target_id,
                 schema_uri)
            VALUES ($1, $2, 'the strike will halt grain exports', $3::jsonb, $4,
                    'iglu:legba/hypothesis/jsonschema/2-0-0')
            RETURNING id
            """,
            uuid4(), parent_id,
            json.dumps([{
                "intensity_at_emit": intensity_before,
                "at": datetime.now(timezone.utc).isoformat(),
            }]),
            target,
        )

        await _run_migration(conn)

        keeper = await conn.fetchrow(
            "SELECT intensity_score, data FROM situations WHERE id = $1", parent_id,
        )
        intensity_now = float(keeper["intensity_score"])
        share = float(json.loads(keeper["data"])["split_from"]["split_share"])
        assert share < 1

        entry = json.loads(await conn.fetchval(
            "SELECT diagnostic_evidence FROM hypotheses WHERE id = $1", hyp,
        ))[0]
        rebased = float(entry["intensity_at_emit"])
        assert entry["rebased_by"] == "0188"
        # The pre-split number is kept, never silently overwritten.
        assert float(entry["intensity_at_emit_pre_0188"]) == pytest.approx(
            intensity_before
        )
        # THE PROPERTY: the comparison the lifecycle actually makes no longer
        # sees a move. Without the re-base this delta is about -50.
        from legba.data.analysts.deterministic_handlers import (
            hypothesis_lifecycle as hl,
        )
        assert abs(intensity_now - rebased) < hl._INTENSITY_MOVE_EPS
        assert hl._classify_move(intensity_now, rebased) == 0
        assert hl._classify_move(intensity_now, intensity_before) == -1, (
            "the unguarded comparison is the mass-refutation this test pins"
        )

        # Idempotent: a second pass must not scale the snapshot twice.
        await _run_migration(conn)
        again = json.loads(await conn.fetchval(
            "SELECT diagnostic_evidence FROM hypotheses WHERE id = $1", hyp,
        ))[0]
        assert float(again["intensity_at_emit"]) == pytest.approx(rebased)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_findings_column_is_re_stamped_within_the_horizon(desk):
    """The stored column must agree with what the live path computes, or the next
    materialization tick re-derives group membership from one spelling while the
    supersession audit trail records another."""
    pool, target = desk
    async with pool.acquire() as conn:
        signature = f"sig:{target}"
        fresh = await _finding(
            conn, analyst_id="energy_security", target_id=target,
            title="fresh", hours_ago=2, signature=signature,
        )
        superseded = await _finding(
            conn, analyst_id="energy_security", target_id=target,
            title="older", hours_ago=48, signature=signature,
        )
        await conn.execute(
            """
            INSERT INTO finding_supersessions
                (superseded_finding_id, superseding_finding_id,
                 situation_signature, reason, score, produced_by)
            VALUES ($1, $2, $3, 'derived_signature', 1.0, 'test')
            """,
            superseded, fresh, signature,
        )
        await _run_migration(conn)

        for fid in (fresh, superseded):
            got = await conn.fetchval(
                "SELECT situation_signature FROM analyst_outputs WHERE id = $1", fid,
            )
            assert got == f"{signature}#dim:energy_security"
            # ...and it is what the LIVE path would compute for the same row.
            assert got == fs.with_dimension(signature, "energy_security")
        link = await conn.fetchval(
            "SELECT situation_signature FROM finding_supersessions "
            "WHERE superseded_finding_id = $1", superseded,
        )
        assert link == f"{signature}#dim:energy_security"
