# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Continuity P2 — the ``situation_tracker`` analyst, on the REAL binding path.

The house rule, and the reason this file is shaped the way it is: a wiring test
that hand-builds the deps bundle proves the handler works when someone else
assembles it correctly, and proves nothing about the assembly. ``journal_propose``
was granted, wired, tested — and never ran once, because the test built the
binding the production path did not.

So the DEPS here come from :func:`build_analyst_run_method` fed the REAL
``descriptors/analyst_situation_tracker.yaml`` off disk, the SQL runs against a
really-migrated Postgres, the watermarks land in the real
``alert_trigger_watermarks``, and the ledger rows are written by the same
:func:`record_situation_events` the actor calls. The only stub is the model,
which is the one thing that cannot be exercised here.

Two things this file also pins, because they are where a new kind half-lands:
the runtime's verify DISPATCH would actually fire for this output kind, and the
verify HELPER's scope guard would actually admit it. Either one missing means a
first-class graded claim ships ungraded and nothing says so.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts import discover_analyst_kinds
from legba.data.analysts import situation_tracker as st
from legba.data.config import PostgresConfig
from legba.data.provenance.kinds import KIND_REGISTRY, OutputKind
from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.situations import trajectory as tj
from legba.runtime.analyst_deps_builder import build_analyst_run_method
from legba.runtime.deps import StandardDeps

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTOR = REPO_ROOT / "descriptors" / "analyst_situation_tracker.yaml"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _StubLLM:
    """A model that replies with whatever the test queued, and records prompts."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.calls = 0

    async def chat_complete(self, messages, **kw):  # noqa: ANN001
        self.calls += 1
        self.prompts.append(messages[0]["content"])
        self.systems.append(kw.get("system") or "")
        reply = self._replies.pop(0) if self._replies else "[]"
        if isinstance(reply, Exception):
            raise reply
        return type("R", (), {"content": reply})()


class _HandleBoundLLM:
    """A model that finds ITS OWN situation in whichever batch carries it.

    ``_StubLLM`` queues a reply for call number one, and a verdict binds by the
    handle it echoes. Both of those are fine on an empty substrate, where the
    test's situation is the only candidate and therefore always ``S1`` in the
    only batch. On the shared substrate they are a coin flip: the tracker sweeps
    the twelve worst OPEN situations and batches them four at a time, so a
    sibling file's frame with fresh verified evidence changes both which call
    carries this test's situation and what handle it was given — and a verdict
    echoing the wrong handle is DROPPED (correctly: no positional fallback).

    So this stub reads the prompt the way the model is asked to: it locates the
    ``### handle=<H> — <name>`` block for the situation it was told about, takes
    the ordinal of the item whose title it was told to cite, and answers for
    that handle. Batches without that situation get ``[]``.

    That is also a stronger assertion than the queue was, and it is made where
    it belongs — in the fixture that has to parse the prompt. If the prompt ever
    stops carrying the real name, the real handle, or the real evidence title,
    no verdict binds and the test fails on the ledger row it did not get.
    """

    def __init__(self, *, name: str, cite_title: str, delta: str, why: str) -> None:
        self._name = name
        self._cite_title = cite_title
        self._delta = delta
        self._why = why
        self.prompts: list[str] = []
        self.calls = 0
        self.answered = 0

    def _verdict_for(self, prompt: str) -> str:
        handle: str | None = None
        for line in prompt.splitlines():
            if line.startswith("### handle="):
                # A new block: either mine, or the one that ends mine.
                head, _, rest = line[len("### handle="):].partition(" — ")
                if handle is not None:
                    break
                handle = head.strip() if rest.strip() == self._name else None
                continue
            if handle is None or not line.startswith("["):
                continue
            ordinal, _, tail = line[1:].partition("]")
            if self._cite_title in tail:
                return json.dumps([{
                    "handle": handle, "delta": self._delta, "why": self._why,
                    "cites": [int(ordinal)], "resolution": False,
                }])
        return "[]"

    async def chat_complete(self, messages, **kw):  # noqa: ANN001
        self.calls += 1
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        reply = self._verdict_for(prompt)
        if reply != "[]":
            self.answered += 1
        return type("R", (), {"content": reply})()


class _RaisingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_complete(self, messages, **kw):  # noqa: ANN001
        self.calls += 1
        raise RuntimeError("provider unavailable")


async def _stub_secrets(_ref: Any) -> str:  # pragma: no cover - trivial
    return ""


def _descriptor() -> AnalystDescriptor:
    return AnalystDescriptor.model_validate(
        yaml.safe_load(DESCRIPTOR.read_text()), strict=False,
    )


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    await p.close()


#: The intensity this file's situations carry, and why it is absurd.
#:
#: ``_OPEN_SITUATIONS_SQL`` takes the worst 12 open situations
#: (``ORDER BY intensity_score DESC``) — a real bound, not a test artifact. On a
#: substrate the whole suite shares there are routinely more than twelve open
#: frames, so a situation at the old fixture's intensity 5.0 could be PAGED OUT
#: of the tracker's own sweep and the test would then be asserting about a
#: situation the analyst never looked at. Sorting first is the one property
#: these tests need from intensity (none of them assert on the number), so they
#: take it explicitly instead of inheriting it from an empty table.
_TOP_INTENSITY = 1_000_000.0


@pytest_asyncio.fixture
async def scope(pool):
    """A DESK NOTHING ELSE OWNS, and this analyst's own cursor reset.

    WHAT WAS HERE. A "blank slate": ``TRUNCATE situation_events`` then
    ``DELETE FROM situations`` then ``DELETE FROM analyst_outputs``. The 08-06
    nightly errored all five integration tests in this file on it —

      ForeignKeyViolationError: update or delete on table "situations"
      violates foreign key constraint "hypotheses_situation_id_fkey"

    — because 0184 shipped, the tracker is registered and writing, and a
    sibling's hypothesis now points at a situation. That is the visible half.
    The invisible half is worse: ``DELETE FROM analyst_outputs`` is an unscoped
    wipe of the SUITE's findings, critiques, scorecards and alerts, so this
    fixture was manufacturing order-dependence for every file that ran before
    it while claiming to defend against it.

    Nothing is deleted now. Each test gets a ``target_id`` no other row can
    carry; teardown CLOSES that desk's frames (``status='closed'`` is what every
    open-situation reader in the tower filters on) rather than deleting rows the
    append-only ledger and ``hypotheses`` both reference.

    The ONE reset that stays is this analyst's own watermark class. It is not a
    shared table in any meaningful sense: ``trigger_class='situation_tracker'``
    is the tracker's private cursor store, whether it has SEEDED is precisely
    what the first two tests are about, and the only other writer
    (``test_alert_trigger_scan.clean_slate``) truncates the whole table itself.
    """
    target = f"country_track_{uuid4().hex[:10]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM alert_trigger_watermarks WHERE trigger_class = $1",
            st.WATERMARK_CLASS,
        )
    yield pool, target
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE situations SET status = 'closed' WHERE target_id = $1", target,
        )


def _seed_key() -> str:
    """The class-seeded marker key. Imported at call time, mirroring how
    ``situation_tracker`` itself reaches into the alert plane's owner module."""
    from legba.data.analysts.deterministic_handlers.alert_trigger_scan import (
        SEED_KEY,
    )

    return SEED_KEY


def _counter(result: Any, key: str) -> int:
    """One receipt counter, from EITHER result shape.

    A cycle that wrote no ledger row returns a bare ``_receipt`` whose counters
    sit at the top of ``data``; a cycle that wrote one returns the graded
    payload, whose counters sit under ``data["counters"]``. Which shape comes
    back is a property of the WHOLE substrate — a foreign situation going
    dormant is enough to move it — so a test that reads one spelling is
    asserting about the suite. Read both.
    """
    data = result.finding.data
    return int(data.get("counters", data).get(key, 0) or 0)


def _my_events(result: Any, situation_id: UUID) -> list[Any]:
    """The pending ledger rows for ONE situation. The tracker sweeps every open
    frame, so ``result.situation_events`` is a statement about the substrate;
    this is the statement about the test's own row."""
    return [
        e for e in (getattr(result, "situation_events", None) or [])
        if e.situation_id == situation_id
    ]


async def _finding(
    conn: Any, *, title: str, body: str, hours_ago: float = 1.0,
    confidence: float = 0.9, faithfulness: float = 0.9,
    analyst_id: str = "escalation", target_id: str = "country_g20_ir",
) -> UUID:
    fid = uuid4()
    produced = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, confidence, data, analyst_id, target_id,
             produced_at, schema_uri)
        VALUES ($1, 'finding', $2, $3, $4, '{}'::jsonb, $5, $6, $7,
                'iglu:legba/finding/jsonschema/1-0-0')
        """,
        fid, title, body, confidence, analyst_id, target_id, produced,
    )
    if faithfulness is not None:
        await conn.execute(
            """
            INSERT INTO analyst_outputs
                (id, kind, title, body, confidence, data, produced_at, schema_uri)
            VALUES ($1, 'critique', 'Faithfulness verify', '', 1.0, $2::jsonb, $3,
                    'iglu:legba/critique/jsonschema/1-0-0')
            """,
            uuid4(),
            json.dumps({
                "analyzed_output_id": str(fid), "overall_score": faithfulness,
            }),
            produced,
        )
    return fid


async def _situation(
    conn: Any, *, name: str, members: list[UUID], target_id: str,
    last_event_hours_ago: float = 1.0, intensity: float = _TOP_INTENSITY,
    status: str = "active",
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO situations
            (id, data, name, status, category, last_event_at, event_count,
             intensity_score, target_id, derived_from, schema_uri,
             situation_signature)
        VALUES ($1, '{}'::jsonb, $2, $3, '', $4, $5, $6, $7, $8,
                'iglu:legba/situation/jsonschema/2-0-0', $9)
        RETURNING id
        """,
        uuid4(), name, status,
        datetime.now(timezone.utc) - timedelta(hours=last_event_hours_ago),
        len(members), intensity, target_id, members, f"sig:{uuid4().hex}",
    )


async def _build(pool, llm):
    """The REAL bind: descriptor off disk -> discovery -> deps builder."""
    return await build_analyst_run_method(
        _descriptor(),
        deps=StandardDeps(
            pg_pool=pool, nats_publish=None, secrets_resolve=_stub_secrets,
        ),
        registry_client=AsyncMock(),
        pg_pool=pool,
        llm_handler_factory=AsyncMock(return_value=llm),
    )


# ---------------------------------------------------------------------------
# Registration — the places a new kind half-lands
# ---------------------------------------------------------------------------


def test_the_kind_is_discovered_with_its_own_output_kind():
    handler = discover_analyst_kinds()["situation_tracker"]
    assert handler.output_kind is OutputKind.SITUATION_UPDATE
    spec = KIND_REGISTRY[OutputKind.SITUATION_UPDATE]
    # The generic table, NOT `situations`: a trajectory READ is an analyst
    # output, not a re-materialization of the frame.
    assert spec.table == "analyst_outputs"


def test_the_descriptor_binds_and_declares_a_judge_route():
    """Plan D3 — without `method.llm.verify` the host wires no judge and the
    claim ships ungraded, which is the whole thing this kind must not do."""
    from legba.runtime.analyst_deps_builder import resolve_judge_route

    d = _descriptor()
    assert d.identity.kind == "situation_tracker"
    assert resolve_judge_route(d) is not None
    # It ships DRAFT: a new OutputKind is a shared-schema change and the
    # activation checklist runs before it goes active.
    assert d.identity.state.value == "draft"


def test_the_runtime_verify_dispatch_admits_this_output_kind():
    """The actor's fire condition is a literal source-level clause; a new kind
    that is missing from it is silently never graded."""
    import inspect

    from legba.runtime import dapr_actors

    src = inspect.getsource(dapr_actors.AnalystActor.run)
    assert "OutputKind.SITUATION_UPDATE" in src
    assert 'identity.kind == "situation_tracker"' in src


def test_the_verify_helper_scope_guard_admits_this_kind():
    """...and the helper's own guard, which returns None for anything it does
    not recognize — the second gate, and just as silent."""
    import inspect

    from legba.runtime import actor_critic

    src = inspect.getsource(actor_critic.verify_inline_target_finding)
    assert "situation_tracker" in src


# ---------------------------------------------------------------------------
# Parse discipline
# ---------------------------------------------------------------------------


def _candidate(handle: str = "S1", *, evidence: int = 2, **kw) -> st.SituationCandidate:
    now = datetime.now(timezone.utc)
    return st.SituationCandidate(
        situation_id=kw.pop("situation_id", uuid4()),
        handle=handle,
        name=kw.pop("name", "Situation: strait transit"),
        category="", target_id="country_g20_ir", status="active",
        intensity_score=5.0, event_count=4,
        last_event_at=now - timedelta(hours=1), opened_at=now - timedelta(days=9),
        state=kw.pop("state", tj.INITIAL_STATE),
        evidence=[
            st.EvidenceItem(
                ordinal=i, finding_id=uuid4(), title=f"item {i}",
                body="body", produced_at=now - timedelta(hours=i),
                analyst_id="escalation", target_id="country_g20_ir",
                effective_confidence=0.8,
            )
            for i in range(1, evidence + 1)
        ],
    )


def test_a_verdict_binds_only_by_echoed_handle():
    """No positional fallback. Binding 'escalates' to the wrong situation is
    the precise misattribution the ledger exists to make impossible."""
    batch = [_candidate("S1"), _candidate("S2")]
    verdicts, dropped = st.parse_verdicts(
        json.dumps([
            {"handle": "S9", "delta": "escalates", "why": "w", "cites": [1]},
        ]),
        batch,
    )
    assert verdicts == []
    assert dropped == 1


def test_a_delta_citing_another_situations_items_is_dropped():
    """S2 may not rest on S1's evidence — the ordinals are one flat space, so
    this is the failure mode a shared numbering invites."""
    s1, s2 = _candidate("S1"), _candidate("S2")
    # Give S2 ordinals 3/4 so S1's 1/2 are genuinely foreign to it.
    for n, item in enumerate(s2.evidence, start=3):
        item.ordinal = n
    verdicts, dropped = st.parse_verdicts(
        json.dumps([
            {"handle": "S2", "delta": "escalates", "why": "w", "cites": [1, 2]},
        ]),
        [s1, s2],
    )
    assert verdicts == []
    assert dropped == 1


def test_an_uncited_delta_is_dropped_but_an_uncited_checkpoint_is_kept():
    batch = [_candidate("S1")]
    verdicts, dropped = st.parse_verdicts(
        json.dumps([{"handle": "S1", "delta": "escalates", "why": "w", "cites": []}]),
        batch,
    )
    assert (verdicts, dropped) == ([], 1)

    verdicts, dropped = st.parse_verdicts(
        json.dumps([
            {"handle": "S1", "delta": "unchanged_checkpoint",
             "why": "routine coverage of an ongoing blockade", "cites": []},
        ]),
        batch,
    )
    assert dropped == 0
    assert verdicts[0].delta == tj.DELTA_UNCHANGED_CHECKPOINT
    assert verdicts[0].cited == []


def test_an_unknown_delta_or_empty_why_is_dropped():
    batch = [_candidate("S1")]
    for payload in (
        {"handle": "S1", "delta": "worsens", "why": "w", "cites": [1]},
        {"handle": "S1", "delta": "escalates", "why": "  ", "cites": [1]},
    ):
        verdicts, dropped = st.parse_verdicts(json.dumps([payload]), batch)
        assert (verdicts, dropped) == ([], 1), payload


def test_unparseable_output_yields_nothing_rather_than_a_guess():
    assert st.parse_verdicts("I'm sorry, I can't do that.", [_candidate()]) == ([], 0)


# ---------------------------------------------------------------------------
# The fold into ledger rows + the graded document
# ---------------------------------------------------------------------------


def test_every_evidence_bearing_delta_ships_a_citation_bridge():
    """An unmarked delta claim would floor to zero at verify. The body's
    [[ref:N]] markers are composed from the ordinals the MODEL named."""
    cand = _candidate("S1")
    verdict = st.Verdict(
        candidate=cand, delta=tj.DELTA_ESCALATES,
        why="the strait was closed to tankers", cited=[cand.evidence[0]],
        resolution=False,
    )
    payload, events, derived, refused = st.build_update(
        [verdict], now=datetime.now(timezone.utc),
    )
    assert refused == 0
    assert "[[ref:1]]" in payload.body
    citations = payload.data["citations"]
    assert [c["ref_kind"] for c in citations] == ["finding"]
    assert citations[0]["ordinal"] == 1
    assert citations[0]["evidence_text"]          # the judge grades against this
    assert derived == [cand.evidence[0].finding_id]
    assert events[0].delta == tj.DELTA_ESCALATES
    assert events[0].state_to == tj.STATE_ESCALATING
    # occurred_at is EVIDENCE time, not run time.
    assert events[0].occurred_at == cand.evidence[0].produced_at

    from legba.data.provenance.verify import _uses_subclaim_convention

    assert _uses_subclaim_convention(citations), (
        "the bridge must route to the sub-claim floor, like a composition"
    )


def test_confidence_is_the_weakest_cited_link():
    cand = _candidate("S1")
    cand.evidence[0].effective_confidence = 0.62
    cand.evidence[1].effective_confidence = 0.95
    payload, _e, _d, _r = st.build_update(
        [st.Verdict(cand, tj.DELTA_ESCALATES, "why", list(cand.evidence), False)],
        now=datetime.now(timezone.utc),
    )
    assert payload.confidence == pytest.approx(0.62)


def test_a_checkpoint_only_cycle_asserts_nothing_and_omits_the_citations_key():
    """The KEY must be absent, not empty. actor_critic's scope guard returns on
    `citations is None`, so its absence is what makes verify a no-op; an empty
    LIST would be graded, and a document truthfully reporting "nothing moved"
    would floor to zero for having been honest."""
    cand = _candidate("S1", evidence=1)
    payload, events, derived, _r = st.build_update(
        [st.Verdict(cand, tj.DELTA_UNCHANGED_CHECKPOINT, "nothing moved", [], False)],
        now=datetime.now(timezone.utc),
    )
    assert "citations" not in payload.data
    assert derived == []
    assert payload.data["moved"] == 0
    assert events[0].derived_from == ()
    # ...and the checkpoint is NOT prose. It asserts nothing about the world, so
    # there is nothing in it to grade; it lives in `data` and on the ledger.
    assert "nothing moved" not in payload.body


def test_an_uncited_checkpoint_on_fresh_news_never_marks_a_situation_dormant():
    """THE bug this pins: dormancy is a 14-day silence, not "the model cited
    nothing". The prompt makes an uncited unchanged_checkpoint the COMMON reply
    on a situation that just received evidence, and mislabelling that as dormant
    is permanent — the ledger has no UPDATE and no DELETE."""
    cand = _candidate("S1", evidence=2, state=tj.STATE_ESCALATING)
    _p, events, _d, refused = st.build_update(
        [st.Verdict(cand, tj.DELTA_UNCHANGED_CHECKPOINT, "routine coverage", [],
                    False)],
        now=datetime.now(timezone.utc),
    )
    assert refused == 0
    assert events[0].state_to == tj.STATE_ESCALATING, (
        "an uncited checkpoint must leave the state where it was"
    )
    # Only the dormancy path may set it, and it says so explicitly.
    cand2 = _candidate("S2", evidence=0)
    _p, events2, _d, _r = st.build_update(
        [st.Verdict(cand2, tj.DELTA_UNCHANGED_CHECKPOINT, "quiet for 30 days", [],
                    False, dormant=True)],
        now=datetime.now(timezone.utc),
    )
    assert events2[0].state_to == tj.STATE_DORMANT


def test_the_graded_body_clears_the_faithfulness_floor():
    """MEASURED, not assumed. An earlier shape put a machine-composed state
    sentence beside the model's `why` and rendered a section per checkpoint;
    every one of those is a checkable claim with no marker, and the real floor
    scored a mixed cycle at 0.25 against a 0.50 bar — which would have demoted
    every trajectory read and left the escalation alert class unable to fire."""
    from legba.data.provenance.verify import _deterministic_floor_subclaim

    moved = _candidate("S1", evidence=1)
    quiet = _candidate("S2", evidence=1)
    for n, item in enumerate(quiet.evidence, start=2):
        item.ordinal = n
    payload, _e, _d, _r = st.build_update(
        [
            st.Verdict(moved, tj.DELTA_ESCALATES,
                       "the strait was closed to all tanker traffic",
                       [moved.evidence[0]], False),
            st.Verdict(quiet, tj.DELTA_UNCHANGED_CHECKPOINT,
                       "routine coverage of an ongoing blockade", [], False),
        ],
        now=datetime.now(timezone.utc),
    )
    report = _deterministic_floor_subclaim(
        payload.body, payload.data["citations"],
    )
    assert report.faithfulness_score >= 0.50, (
        f"score {report.faithfulness_score} with unsupported "
        f"{[s.text for s in report.unsupported_spans]}"
    )
    assert report.checkable_claims == 1, (
        "only the cited delta is a claim; the checkpoint is not prose"
    )


def test_a_refused_transition_leaves_the_prose_and_the_ledger_agreed():
    """A verdict the state machine rejects must not survive in EITHER place —
    a document asserting a delta the ledger does not carry is the drift this
    whole design exists to prevent."""
    cand = _candidate("S1", state="watching")
    good = st.Verdict(cand, tj.DELTA_ESCALATES, "real", [cand.evidence[0]], False)
    bad = st.Verdict(cand, tj.DELTA_ESCALATES, "", [cand.evidence[1]], False)
    payload, events, _d, refused = st.build_update(
        [good, bad], now=datetime.now(timezone.utc),
    )
    assert refused == 1
    assert len(events) == 1
    assert payload.body.count("escalates") == 1


# ---------------------------------------------------------------------------
# The real run: descriptor -> deps builder -> run_method -> real substrate
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_first_run_seeds_silently_and_writes_no_deltas(scope):
    pool, target = scope
    async with pool.acquire() as conn:
        f = await _finding(conn, title="Tanker held", body="A tanker was held.",
                           target_id=target)
        sid = await _situation(conn, name="Situation: strait", members=[f],
                               target_id=target)

    llm = _StubLLM([])
    run_method, kind_deps, output_kind, _rc, _rs = await _build(pool, llm)
    result = await run_method([], {}, kind_deps)

    assert output_kind is OutputKind.SITUATION_UPDATE
    assert llm.calls == 0, "seeding must not spend a token"
    assert result.force_trace_only is True
    assert result.finding.data["seeded"] is True
    assert not getattr(result, "situation_events", [])
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM situation_events WHERE situation_id = $1", sid,
        ) == 0
        # MY situation was watermarked, and the class was marked seeded. The old
        # `count(*) == 2` said "this analyst has watermarked exactly one
        # situation in the entire database", which is a statement about how many
        # frames the suite happens to leave open, not about seeding.
        keys = {
            r["watermark_key"] for r in await conn.fetch(
                "SELECT watermark_key FROM alert_trigger_watermarks "
                "WHERE trigger_class = $1", st.WATERMARK_CLASS,
            )
        }
    assert str(sid) in keys, "the seed must record where MY situation stands"
    assert _seed_key() in keys, "…and mark the class seeded, or it seeds twice"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_second_run_adjudicates_new_evidence_and_writes_the_ledger(scope):
    pool, target = scope
    name = f"Situation: strait {uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        old = await _finding(conn, title="Routine transit", body="Traffic normal.",
                             hours_ago=48, target_id=target)
        sid = await _situation(conn, name=name, members=[old],
                               last_event_hours_ago=48, target_id=target)

    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, _StubLLM([]))
    await run_method([], {}, kind_deps)          # seed

    async with pool.acquire() as conn:
        fresh = await _finding(
            conn, title="Strait closed to tankers",
            body="Authorities closed the strait to all tanker traffic.",
            hours_ago=1, target_id=target,
        )
        await conn.execute(
            "UPDATE situations SET derived_from = derived_from || $2::uuid[], "
            "last_event_at = now() WHERE id = $1", sid, [fresh],
        )

    llm = _HandleBoundLLM(
        name=name, cite_title="Strait closed to tankers", delta="escalates",
        why="the strait was closed to all tanker traffic",
    )
    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, llm)
    result = await run_method([], {}, kind_deps)

    # Exactly one batch carried MY situation, and it was adjudicated there.
    assert llm.answered == 1
    # The prompt carries the REAL fresh finding and its date, not a placeholder
    # — the stub could not have bound a handle otherwise.
    mine_prompt = next(p for p in llm.prompts if name in p)
    assert "Strait closed to tankers" in mine_prompt
    assert result.force_trace_only is False
    assert len(_my_events(result, sid)) == 1
    assert fresh in result.derived_from

    # The runtime's step: materialize the ledger against the output row id,
    # carrying the faithfulness verdict the actor just produced.
    source_output_id = uuid4()
    async with pool.acquire() as conn:
        written = await tj.record_situation_events(
            conn, events=_my_events(result, sid),
            source_output_id=source_output_id,
            verification={"faithfulness_score": 0.9},
        )
        assert written == 1
        row = await conn.fetchrow(
            "SELECT * FROM situation_events WHERE situation_id = $1", sid,
        )
        assert row["delta"] == "escalates"
        assert row["state_from"] == tj.STATE_WATCHING
        assert row["state_to"] == tj.STATE_ESCALATING
        assert [str(u) for u in row["derived_from"]] == [str(fresh)]
        assert row["source_output_id"] == source_output_id
        # occurred_at tracks the EVIDENCE, so it is safely in the past.
        assert row["occurred_at"] < datetime.now(timezone.utc)
        # The watermark advanced to the evidence, not to run time.
        state = await conn.fetchval(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            st.WATERMARK_CLASS, str(sid),
        )
        mark = datetime.fromisoformat(json.loads(state)["last_evidence_at"])
        assert mark < datetime.now(timezone.utc)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unverified_evidence_cannot_move_a_situation(scope):
    """Plan D2 says VERIFIED findings. A finding with no faithfulness verdict,
    or one below the floor, is not evidence — the tracker must not even see it.

    "Must not even see it" is what is asserted, and it is asserted about THIS
    situation: the frame is swept (its ``### handle=`` block is in a prompt only
    if it had news), so the claim is that its block never appears and that it
    produces no ledger row. ``llm.calls == 0`` said instead that NOTHING in the
    shared database had verified news this tick, which is a fact about the
    suite, and one that stopped being true the moment a sibling file left an
    open frame with a graded finding on it.
    """
    pool, target = scope
    name = f"Situation: strait {uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        seed = await _finding(conn, title="Old", body="old", hours_ago=72,
                              target_id=target)
        sid = await _situation(conn, name=name, members=[seed],
                               last_event_hours_ago=72, target_id=target)
    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, _StubLLM([]))
    await run_method([], {}, kind_deps)          # seed

    async with pool.acquire() as conn:
        ungraded = await _finding(conn, title="Ungraded", body="x", hours_ago=1,
                                  faithfulness=None, target_id=target)
        demoted = await _finding(conn, title="Demoted", body="y", hours_ago=1,
                                 faithfulness=0.20, target_id=target)
        await conn.execute(
            "UPDATE situations SET derived_from = derived_from || $2::uuid[], "
            "last_event_at = now() WHERE id = $1", sid, [ungraded, demoted],
        )

    llm = _StubLLM([])
    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, llm)
    result = await run_method([], {}, kind_deps)
    assert not any(name in p for p in llm.prompts), (
        "unverified evidence must not put this situation in front of the model"
    )
    assert _my_events(result, sid) == [], "…and must not move it"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failed_batch_defers_rather_than_claiming_nothing_moved(scope):
    """Defaulting a failed call to 'unchanged' would write a positive
    'we looked and nothing moved' claim on the strength of an exception."""
    pool, target = scope
    async with pool.acquire() as conn:
        seed = await _finding(conn, title="Old", body="old", hours_ago=72,
                              target_id=target)
        sid = await _situation(conn, name="Situation: strait", members=[seed],
                               last_event_hours_ago=72, target_id=target)
    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, _StubLLM([]))
    await run_method([], {}, kind_deps)

    async with pool.acquire() as conn:
        fresh = await _finding(conn, title="New", body="n", hours_ago=1,
                               target_id=target)
        await conn.execute(
            "UPDATE situations SET derived_from = derived_from || $2::uuid[], "
            "last_event_at = now() WHERE id = $1", sid, [fresh],
        )
        before = await conn.fetchval(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            st.WATERMARK_CLASS, str(sid),
        )

    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, _RaisingLLM())
    result = await run_method([], {}, kind_deps)
    # A deferral was reported (>= 1, not == 1: every situation in the failed
    # batch defers, and the batch is filled from the whole open set), and MY
    # situation produced no ledger row and no fabricated 'unchanged'.
    assert _counter(result, "situations_deferred") >= 1
    assert _my_events(result, sid) == []
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM situation_events WHERE situation_id = $1", sid,
        ) == 0
        after = await conn.fetchval(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            st.WATERMARK_CLASS, str(sid),
        )
    assert after == before, "a deferred situation must be re-asked, not skipped past"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_long_quiet_situation_gets_a_dormancy_checkpoint(scope):
    """D4: no attached news for the horizon => a checkpoint row, never a close."""
    pool, target = scope
    name = f"Situation: quiet {uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        old = await _finding(conn, title="Old", body="old", hours_ago=24 * 30,
                             target_id=target)
        sid = await _situation(conn, name=name, members=[old],
                               last_event_hours_ago=24 * 30, target_id=target)
    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, _StubLLM([]))
    await run_method([], {}, kind_deps)          # seed

    llm = _StubLLM([])
    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, llm)
    result = await run_method([], {}, kind_deps)

    assert not any(name in p for p in llm.prompts), (
        "a dormancy checkpoint needs no model — this frame is never asked about"
    )
    mine = _my_events(result, sid)
    assert len(mine) == 1
    event = mine[0]
    assert event.delta == tj.DELTA_UNCHANGED_CHECKPOINT
    assert event.state_to == tj.STATE_DORMANT
    assert event.derived_from == ()
    async with pool.acquire() as conn:
        # No verification block: this cycle made no cited claim, so it was never
        # graded — and a checkpoint needs no verdict, because it asserts nothing.
        assert await tj.record_situation_events(
            conn, events=mine, source_output_id=uuid4(),
        ) == 1
        assert (await tj.read_current_states(conn, [sid]))[str(sid)] == (
            tj.STATE_DORMANT
        )
