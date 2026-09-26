# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed tests for the V3/P3 temporal readers (spec §3.2/§3.3).

Each test exercises a real ``PostgresQdrantSubstrateQueryPort`` against the
ephemeral migrated database — the same no-mocks posture as
``test_substrate_query_port.py``. The load-bearing claims:

* ``as_of`` is VALIDITY time — a row superseded or closed TODAY is the
  answer on date D, and the same row is absent from the open read (the
  open-row predicate is swapped out, not ANDed in).
* A NULL ``valid_from`` over-includes by construction and is COUNTED in
  ``unbounded_start`` — the honest "how much of this answer has no recorded
  start" number.
* A malformed ``as_of`` refuses loud — an error envelope, never a silent
  ``now()``.
* The graph walks apply the predicate PER HOP on both the seed and the
  recursive arm: a path through an edge closed before D is not a path on D.
* ``since``/``until`` bound ``get_timeline`` on each stream's anchor.
* ``believed_as_of`` is the OTHER clock — decision time on
  ``produced_at``/``superseded_at`` — and the critic fold dates to D too.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
        min_size=1,
        max_size=4,
    )
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def port(pg_pool):
    return PostgresQdrantSubstrateQueryPort(
        pg_pool=pg_pool, qdrant_client=None,
        signals_collection="legba_test_asof__signals")


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _fact(
    pool, *, subject: str, predicate: str = "leader", value: str = "X",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    superseded_by=None,
):
    """One ``facts`` row with a controlled validity span."""
    fid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO facts (
                id, subject, predicate, value, confidence,
                source_cycle, source_type, data, evidence_set,
                valid_from, valid_until, superseded_by,
                produced_at, derived_from, schema_uri
            ) VALUES (
                $1, $2, $3, $4, 0.9,
                NULL, 'agent', NULL, NULL,
                $5, $6, $7,
                NOW(), '{}'::uuid[], 'iglu:legba/fact/jsonschema/2-0-0'
            )
            """,
            fid, subject, predicate, value, valid_from, valid_until,
            superseded_by,
        )
    return fid


async def _edge(
    pool, *, src: str, dst: str, polarity: int = 1,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
):
    """One ``entity_edges`` row (minting endpoint profiles) with a
    controlled validity span — the walk reads entity ids, not names."""
    async with pool.acquire() as conn:
        async def ent(name: str):
            eid = await conn.fetchval(
                "SELECT id FROM entity_profiles "
                " WHERE lower(canonical_name)=lower($1) "
                "   AND merged_into IS NULL LIMIT 1", name)
            if eid is None:
                eid = await conn.fetchval(
                    """INSERT INTO entity_profiles
                         (canonical_name, entity_class, entity_type, data)
                       VALUES ($1, 'organization', 'organization', '{}'::jsonb)
                       RETURNING id""", name)
            return eid
        return await conn.fetchval(
            """
            INSERT INTO entity_edges
                (src_id, dst_id, edge_type, edge_family, polarity,
                 confidence, valid_from, valid_until)
            VALUES ($1, $2, 'supports', 'relation', $3, 0.9, $4, $5)
            RETURNING id
            """,
            await ent(src), await ent(dst), polarity, valid_from, valid_until,
        )


async def _situation(
    pool, *, name: str, status: str = "closed",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
):
    """One ``situations`` frame with a controlled validity span."""
    sid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO situations "
            "(id, data, name, status, category, intensity_score, "
            " situation_signature, valid_from, valid_until, analyst_id) "
            "VALUES ($1, '{}'::jsonb, $2, $3, 'x', 1.7, $4, $5, $6, "
            "        'situation_clustering')",
            sid, name, status, f"sig:{name}", valid_from, valid_until,
        )
    return sid


async def _finding(
    pool, *, title: str, confidence: float = 0.8,
    produced_at: datetime | None = None,
    superseded_at: datetime | None = None,
    target_id: str | None = None,
):
    """One ``analyst_outputs`` finding row on controlled decision clocks."""
    oid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, produced_at, superseded_at,
                derived_from, schema_uri
            ) VALUES (
                $1, 'finding', $2, 'body', $3, 'medium', '{}'::jsonb,
                $4, $5, $6, '{}'::uuid[],
                'iglu:legba/finding/jsonschema/1-0-0'
            )
            """,
            oid, title, confidence, target_id,
            produced_at or datetime.now(timezone.utc), superseded_at,
        )
    return oid


async def _critique(
    pool, *, analyzed_id, score: float, produced_at: datetime,
):
    """One faithfulness verdict row (kind='critique', the join the readers
    and the route both use)."""
    cid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                produced_at, derived_from, schema_uri
            ) VALUES (
                $1, 'critique', 'Faithfulness verify', '', $4, NULL,
                $2::jsonb, $3, '{}'::uuid[],
                'iglu:legba/critique/jsonschema/1-0-0'
            )
            """,
            cid,
            json.dumps({
                "analyzed_output_id": str(analyzed_id),
                "overall_score": score,
            }),
            produced_at,
            score,
        )
    return cid


# ---------------------------------------------------------------------------
# query_facts — as_of swaps the open-row gate for the as-of predicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facts_as_of_returns_a_row_superseded_today(pg_pool, port):
    """The §3.2 rule: a row closed today was THE answer on date D."""
    subj = f"TemporalSubj_{uuid4().hex[:8]}"
    old = datetime(2026, 8, 1, tzinfo=timezone.utc)
    closed = datetime(2026, 8, 10, tzinfo=timezone.utc)
    fid = await _fact(
        pg_pool, subject=subj, valid_from=old, valid_until=closed,
        superseded_by=uuid4(),
    )

    as_of = await port.query_facts(subject=subj, as_of="2026-08-05")
    assert [r["id"] for r in as_of["rows"]] == [str(fid)]
    assert as_of["as_of"].startswith("2026-08-05")
    # NULL valid_from rows are counted — this one has a real start, so 0.
    assert as_of["unbounded_start"] == 0

    # The same row is absent from the open read — it is closed TODAY.
    now = await port.query_facts(subject=subj)
    assert now["rows"] == []
    assert "unbounded_start" not in now  # open reads carry no counter


@pytest.mark.asyncio
async def test_facts_as_of_excludes_rows_closed_before_d(pg_pool, port):
    subj = f"EarlyClose_{uuid4().hex[:8]}"
    await _fact(
        pg_pool, subject=subj,
        valid_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
        valid_until=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    out = await port.query_facts(subject=subj, as_of="2026-08-20")
    assert out["rows"] == []


@pytest.mark.asyncio
async def test_facts_as_of_counts_unbounded_starts(pg_pool, port):
    """A NULL valid_from over-includes by construction — and is SAID."""
    subj = f"Unbounded_{uuid4().hex[:8]}"
    await _fact(pg_pool, subject=subj, value="undated")          # NULL start
    await _fact(
        pg_pool, subject=subj, value="dated", predicate="other",
        valid_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    out = await port.query_facts(subject=subj, as_of="2026-08-05")
    assert len(out["rows"]) == 2
    assert out["unbounded_start"] == 1


@pytest.mark.asyncio
async def test_facts_malformed_as_of_refuses_loud(pg_pool, port):
    out = await port.query_facts(subject="x", as_of="last tuesday")
    assert out["rows"] == []
    assert "error" in out and "as_of" in out["error"]


# ---------------------------------------------------------------------------
# query_nexuses — same contract
# ---------------------------------------------------------------------------


async def _nexus(
    pool, *, subject: str, object_: str,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    superseded_by=None,
):
    nid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nexuses (
                id, subject, intermediary, object, rel_type, label,
                polarity, intent, channel, confidence,
                valid_from, valid_until, superseded_by,
                analyst_id, produced_at
            ) VALUES (
                $1, $2, NULL, $3, 'supports', '',
                1, '', 'direct', 0.9,
                $4, $5, $6,
                NULL, NOW()
            )
            """,
            nid, subject, object_, valid_from, valid_until, superseded_by,
        )
    return nid


@pytest.mark.asyncio
async def test_nexuses_as_of_returns_a_row_closed_today(pg_pool, port):
    subj = f"NexusSubj_{uuid4().hex[:8]}"
    nid = await _nexus(
        pg_pool, subject=subj, object_="Obj",
        valid_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
        valid_until=datetime(2026, 8, 10, tzinfo=timezone.utc),
        superseded_by=uuid4(),
    )
    out = await port.query_nexuses(subject=subj, as_of="2026-08-05")
    assert [r["id"] for r in out["rows"]] == [str(nid)]
    now = await port.query_nexuses(subject=subj)
    assert now["rows"] == []


# ---------------------------------------------------------------------------
# The graph walks — per-hop as-of on BOTH arms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_walk_as_of_sees_edges_open_on_d(pg_pool, port):
    """A→B→C where B→C closed after D: the path exists on D, not today."""
    a = f"WalkA_{uuid4().hex[:6]}"
    b = f"WalkB_{uuid4().hex[:6]}"
    c = f"WalkC_{uuid4().hex[:6]}"
    await _edge(pg_pool, src=a, dst=b,
                valid_from=datetime(2026, 8, 1, tzinfo=timezone.utc))
    await _edge(pg_pool, src=b, dst=c,
                valid_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
                valid_until=datetime(2026, 8, 10, tzinfo=timezone.utc))

    # On D the chain held: seed edge open + recursive edge open.
    as_of = await port.query_paths(subject=a, obj=c, as_of="2026-08-05")
    assert len(as_of["paths"]) == 1
    assert as_of["paths"][0]["hops"] == 2
    assert "unbounded_start" in as_of

    # Today the recursive edge is closed — no path.
    now = await port.query_paths(subject=a, obj=c)
    assert now["paths"] == []
    assert "unbounded_start" not in now

    # And before the recursive edge closed it did not yet exist... a D
    # AFTER the close sees nothing either.
    later = await port.query_paths(subject=a, obj=c, as_of="2026-08-20")
    assert later["paths"] == []


@pytest.mark.asyncio
async def test_walk_malformed_as_of_refuses_loud(pg_pool, port):
    out = await port.query_paths(subject="a", obj="b", as_of="someday")
    assert out["paths"] == [] and "error" in out


# ---------------------------------------------------------------------------
# list_situations — as_of narrows to the frames that held on D
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_situations_as_of_returns_a_frame_closed_since(pg_pool, port):
    name = f"Sit_{uuid4().hex[:8]}"
    sid = await _situation(
        pool=pg_pool, name=name,
        valid_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
        valid_until=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    out = await port.list_situations(as_of="2026-08-05")
    ids = [r["id"] for r in out["rows"]]
    assert str(sid) in ids
    assert out["as_of"].startswith("2026-08-05")
    assert "unbounded_start" in out
    # After the close the same read does not see it.
    later = await port.list_situations(as_of="2026-08-20")
    assert str(sid) not in [r["id"] for r in later["rows"]]


# ---------------------------------------------------------------------------
# get_timeline — since/until bound each stream's anchor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeline_since_until_windows_the_anchor(pg_pool, port):
    subj = f"Win_{uuid4().hex[:6]}"
    in_window = await _fact(
        pg_pool, subject=subj, value="inside",
        valid_from=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )
    outside = await _fact(
        pg_pool, subject=subj, value="outside", predicate="other",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    out = await port.get_timeline(
        subject=subj, since="2026-08-01", until="2026-09-01")
    ids = {i["id"] for i in out["items"]}
    assert str(in_window) in ids
    assert str(outside) not in ids
    assert out["since"].startswith("2026-08-01")
    assert out["until"].startswith("2026-09-01")

    # No window — the undated-window read is today's behavior verbatim.
    full = await port.get_timeline(subject=subj)
    assert str(outside) in {i["id"] for i in full["items"]}
    assert "since" not in full


@pytest.mark.asyncio
async def test_timeline_malformed_window_refuses_loud(pg_pool, port):
    out = await port.get_timeline(subject="x", since="a while ago")
    assert out["items"] == [] and "error" in out


# ---------------------------------------------------------------------------
# list_findings — believed_as_of is DECISION time, and the fold dates to D
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_findings_believed_as_of_returns_superseded_since(pg_pool, port):
    """A finding superseded after D is the belief on D; it is absent from
    the open read and absent from a belief read AFTER supersession."""
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 10, tzinfo=timezone.utc)
    fid = await _finding(
        pg_pool, title=f"belief_{uuid4().hex[:6]}",
        produced_at=t0, superseded_at=t2,
    )
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE analyst_outputs SET superseded_by = $1 WHERE id = $2",
            uuid4(), fid)

    on_d = await port.list_findings(believed_as_of="2026-08-05")
    assert str(fid) in [r["id"] for r in on_d["rows"]]
    assert on_d["believed_as_of"].startswith("2026-08-05")

    after = await port.list_findings(believed_as_of="2026-08-20")
    assert str(fid) not in [r["id"] for r in after["rows"]]

    now = await port.list_findings()  # open read: superseded → absent
    assert str(fid) not in [r["id"] for r in now["rows"]]


@pytest.mark.asyncio
async def test_findings_believed_as_of_folds_the_verdict_at_d(pg_pool, port):
    """The critic score on a believed_as_of read is the verdict Legba HELD
    on D — a critique landed after D must not leak in."""
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    fid = await _finding(
        pg_pool, title=f"verdict_{uuid4().hex[:6]}",
        confidence=0.9, produced_at=t0,
    )
    # The verdict lands AFTER D — on D the finding was ungraded.
    await _critique(
        pg_pool, analyzed_id=fid, score=0.5,
        produced_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    on_d = await port.list_findings(believed_as_of="2026-08-05")
    row = next(r for r in on_d["rows"] if r["id"] == str(fid))
    assert row["critic_score"] is None            # ungraded on D
    assert row["effective_confidence"] == pytest.approx(0.9)     # confidence alone then

    now = await port.list_findings()
    row_now = next(r for r in now["rows"] if r["id"] == str(fid))
    assert row_now["critic_score"] == pytest.approx(0.5)         # graded today
    assert row_now["effective_confidence"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# belief_as_of — the register: folds stamped, pending counted, nothing pooled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_belief_as_of_requires_a_date(pg_pool, port):
    out = await port.belief_as_of(as_of="")
    assert "error" in out
    out = await port.belief_as_of(as_of="next friday")
    assert "error" in out


@pytest.mark.asyncio
async def test_belief_as_of_rejects_an_unknown_fold(pg_pool, port):
    out = await port.belief_as_of(as_of="2026-08-05", fold_verdicts="best")
    assert "error" in out and "fold_verdicts" in out["error"]


@pytest.mark.asyncio
async def test_belief_as_of_as_of_fold_counts_pending(pg_pool, port):
    """fold='as_of': the finding ungraded by D is pending; the graded one
    carries its verdict. No pooled number anywhere."""
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    graded = await _finding(
        pg_pool, title=f"graded_{uuid4().hex[:6]}", confidence=0.9,
        produced_at=t0)
    pending = await _finding(
        pg_pool, title=f"pending_{uuid4().hex[:6]}", confidence=0.7,
        produced_at=t0)
    await _critique(
        pg_pool, analyzed_id=graded, score=0.6,
        produced_at=datetime(2026, 8, 3, tzinfo=timezone.utc))

    out = await port.belief_as_of(as_of="2026-08-05", fold_verdicts="as_of")
    assert out["fold_verdicts"] == "as_of"
    by_id = {r["id"]: r for r in out["rows"]}
    grow = by_id[str(graded)]
    prow = by_id[str(pending)]
    assert grow["verdict_score"] == pytest.approx(0.6)
    assert grow["effective_confidence"] == pytest.approx(0.6)    # min(0.9, 0.6)
    assert grow["verdict_pending_at_as_of"] is False
    assert prow["verdict_score"] is None
    assert prow["effective_confidence"] is None   # pending — no score
    assert prow["verdict_pending_at_as_of"] is True
    assert out["verdict_pending_at_as_of"] >= 1
    assert "pooled" not in out                    # never a pooled score


@pytest.mark.asyncio
async def test_belief_as_of_latest_fold_uses_todays_verdict(pg_pool, port):
    """fold='latest': the verdict that landed AFTER D still scores the row —
    but pending_at_as_of still reports it was ungraded on D."""
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    fid = await _finding(
        pg_pool, title=f"late_{uuid4().hex[:6]}", confidence=0.9,
        produced_at=t0)
    await _critique(
        pg_pool, analyzed_id=fid, score=0.4,
        produced_at=datetime(2026, 8, 10, tzinfo=timezone.utc))  # after D

    out = await port.belief_as_of(as_of="2026-08-05", fold_verdicts="latest")
    row = next(r for r in out["rows"] if r["id"] == str(fid))
    assert row["verdict_score"] == pytest.approx(0.4)            # today's verdict
    assert row["effective_confidence"] == pytest.approx(0.4)
    assert row["verdict_pending_at_as_of"] is True  # ungraded ON D — said so


@pytest.mark.asyncio
async def test_belief_as_of_scopes_to_target(pg_pool, port):
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    tid = f"target_{uuid4().hex[:6]}"
    hit = await _finding(
        pg_pool, title=f"t_{uuid4().hex[:6]}", produced_at=t0, target_id=tid)
    await _finding(
        pg_pool, title=f"o_{uuid4().hex[:6]}", produced_at=t0,
        target_id=f"other_{uuid4().hex[:6]}")
    out = await port.belief_as_of(as_of="2026-08-05", target_id=tid)
    ids = {r["id"] for r in out["rows"]}
    assert str(hit) in ids
    assert all(r["target_id"] == tid for r in out["rows"])
