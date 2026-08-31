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
    status: str = "active", opened_days_ago: float = 1.0,
) -> UUID:
    """One open frame.

    ``opened_days_ago`` writes a REAL ``valid_from``, and since #64 that is
    load-bearing rather than cosmetic: for a frame the ledger has never moved,
    the opening IS the evidence clock the dormancy horizon is measured against
    (``trajectory.evidence_anchor``). A fixture leaving it NULL would make every
    seeded frame read as opened just now — which is how a dormancy test can pass
    or fail for a reason that has nothing to do with the code under test.
    """
    return await conn.fetchval(
        """
        INSERT INTO situations
            (id, data, name, status, category, last_event_at, event_count,
             intensity_score, target_id, derived_from, schema_uri,
             situation_signature, valid_from)
        VALUES ($1, '{}'::jsonb, $2, $3, '', $4, $5, $6, $7, $8,
                'iglu:legba/situation/jsonschema/2-0-0', $9, $10)
        RETURNING id
        """,
        uuid4(), name, status,
        datetime.now(timezone.utc) - timedelta(hours=last_event_hours_ago),
        len(members), intensity, target_id, members, f"sig:{uuid4().hex}",
        datetime.now(timezone.utc) - timedelta(days=opened_days_ago),
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
    """One tracker candidate.

    ``anchor_days_ago`` / ``corroborated`` set the EVIDENCE clock (#64) — the
    field ``_dormancy_verdict`` now reads. It defaults to a fresh anchor, so a
    candidate built without saying otherwise is one the world touched today and
    no dormancy test fires on it by accident.

    ``member_hours_ago`` sets the MEMBER clock independently, which is the whole
    point of the re-key: the two are allowed to disagree, and before #64 only the
    second one was consulted.
    """
    now = datetime.now(timezone.utc)
    anchor_days_ago = kw.pop("anchor_days_ago", 0.5)
    return st.SituationCandidate(
        situation_id=kw.pop("situation_id", uuid4()),
        handle=handle,
        name=kw.pop("name", "Situation: strait transit"),
        category="", target_id="country_g20_ir", status="active",
        intensity_score=5.0, event_count=4,
        last_event_at=now - timedelta(hours=kw.pop("member_hours_ago", 1)),
        opened_at=now - timedelta(days=9),
        state=kw.pop("state", tj.INITIAL_STATE),
        evidence_anchor_at=(
            None if anchor_days_ago is None
            else now - timedelta(days=anchor_days_ago)
        ),
        corroborated=kw.pop("corroborated", True),
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
    """D4: no attached news for the horizon => a checkpoint row, never a close.

    Since #64 "no attached news" is measured on the EVIDENCE clock, so this frame
    is seeded 30 days OPEN as well as 30 days quiet — a frame the ledger has
    never moved anchors on its own opening.
    """
    pool, target = scope
    name = f"Situation: quiet {uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        old = await _finding(conn, title="Old", body="old", hours_ago=24 * 30,
                             target_id=target)
        sid = await _situation(conn, name=name, members=[old],
                               last_event_hours_ago=24 * 30, target_id=target,
                               opened_days_ago=30)
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


# ---------------------------------------------------------------------------
# #64 — THE DORMANCY RE-KEY: which clock, and what it is actually worth
#
# `_dormancy_verdict` used to read `cand.last_event_at` — the newest MEMBER
# finding's produced_at, i.e. the last time a DESK WROTE. Eight units write into
# a country frame daily, so `now - last_event_at` was never a fortnight and the
# transition was structurally unreachable: 0 of 2,052 live ledger rows have ever
# reached `dormant`. It now reads the EVIDENCE clock (`evidence_anchor_at`), the
# one H1 established and the register already decays on.
# ---------------------------------------------------------------------------


def test_dormancy_reads_the_evidence_clock_not_the_member_clock():
    """THE HEADLINE. Desks wrote an hour ago; the world last moved this frame a
    month ago. The member clock says active forever; the evidence clock says
    dormant, which is what it is."""
    fresh_desks_stale_world = _candidate(
        "S1", evidence=0, member_hours_ago=1, anchor_days_ago=30,
    )
    verdict = st._dormancy_verdict(
        fresh_desks_stale_world,
        now=datetime.now(timezone.utc), dormancy_days=tj.DORMANCY_DAYS,
    )
    assert verdict is not None, (
        "a frame whose desks are still typing but which the world has not "
        "touched in a month is exactly the case the re-key exists for"
    )
    assert verdict.dormant is True
    assert verdict.delta == tj.DELTA_UNCHANGED_CHECKPOINT
    assert verdict.cited == []
    # The sentence names the date it is dormant SINCE — the anchor, never "now".
    assert "30 days" in verdict.why
    assert st._day(fresh_desks_stale_world.evidence_anchor_at) in verdict.why


def test_a_frame_the_world_moved_recently_does_not_go_dormant():
    """The counterweight. Fresh evidence keeps a frame awake however quiet its
    desks have been — and a frame already dormant or closed is never re-marked
    (an append-only table with no correction path)."""
    assert st._dormancy_verdict(
        _candidate("S1", evidence=0, member_hours_ago=24 * 40, anchor_days_ago=2),
        now=datetime.now(timezone.utc), dormancy_days=tj.DORMANCY_DAYS,
    ) is None
    for state in (tj.STATE_DORMANT, tj.STATE_CLOSED):
        assert st._dormancy_verdict(
            _candidate("S1", evidence=0, anchor_days_ago=99, state=state),
            now=datetime.now(timezone.utc), dormancy_days=tj.DORMANCY_DAYS,
        ) is None, state
    # No clock at all — neither ledger nor opening — claims nothing. A dormancy
    # row needs a date to be dormant SINCE and this analyst does not invent one.
    assert st._dormancy_verdict(
        _candidate("S1", evidence=0, anchor_days_ago=None),
        now=datetime.now(timezone.utc), dormancy_days=tj.DORMANCY_DAYS,
    ) is None


def test_the_dormancy_verdict_is_stable_across_repeated_evaluation():
    """TWO stabilities, and the second one is a defect the re-key would have
    shipped.

    (1) The function is pure: same candidate, same clock, same verdict.

    (2) The ROW it produces must settle the frame. `read_current_states` takes
    the ledger's newest row by `occurred_at DESC` — and a checkpoint used to be
    dated `cand.last_event_at`, which the horizon guarantees is at least a
    fortnight old and therefore OLDER than any recent row on that ledger. The
    dormancy row would have filed itself behind the existing history, the frame's
    current state would never have read `dormant`, and the verdict would have
    fired again every hour into a table with no DELETE. Latent while the horizon
    was unreachable; live the moment it is re-keyed.
    """
    now = datetime.now(timezone.utc)
    cand = _candidate("S1", evidence=0, member_hours_ago=1, anchor_days_ago=30)
    first = st._dormancy_verdict(cand, now=now, dormancy_days=tj.DORMANCY_DAYS)
    second = st._dormancy_verdict(cand, now=now, dormancy_days=tj.DORMANCY_DAYS)
    assert first is not None and second is not None
    assert (first.delta, first.why, first.dormant) == (
        second.delta, second.why, second.dormant
    )

    _p, events, _d, refused = st.build_update([first], now=now)
    assert refused == 0
    assert events[0].state_to == tj.STATE_DORMANT
    assert events[0].occurred_at == now, (
        "a dormancy row dated at the member clock files itself BEHIND the "
        "ledger it is meant to settle, and re-fires forever"
    )
    # Having settled, the frame is not re-marked: the next tick reads `dormant`
    # off the ledger and the verdict declines.
    settled = _candidate(
        "S1", evidence=0, member_hours_ago=1, anchor_days_ago=31,
        state=tj.STATE_DORMANT,
    )
    assert st._dormancy_verdict(
        settled, now=now, dormancy_days=tj.DORMANCY_DAYS,
    ) is None


def test_an_uncorroborated_frames_checkpoint_says_so_in_words():
    """"The world stopped moving this" and "we have never adjudicated this" are
    different facts that land in the same column, so the sentence has to carry
    the difference — the NEVER-CORROBORATED discipline H1 set on the render
    side."""
    corroborated = st._dormancy_verdict(
        _candidate("S1", evidence=0, anchor_days_ago=30, corroborated=True),
        now=datetime.now(timezone.utc), dormancy_days=tj.DORMANCY_DAYS,
    )
    never = st._dormancy_verdict(
        _candidate("S2", evidence=0, anchor_days_ago=30, corroborated=False),
        now=datetime.now(timezone.utc), dormancy_days=tj.DORMANCY_DAYS,
    )
    assert "No evidence has moved this situation since" in corroborated.why
    assert "never been corroborated" in never.why
    assert "stood open since" in never.why


# ---------------------------------------------------------------------------
# THE MEASURED RATE — what this re-key is actually worth, on the live shape
#
# The 2026-08-29 register premise review measured the corroboration clock as
# WINDABLE at pipeline time: 931/957 significant deltas carry an `occurred_at`
# byte-equal to the cited desk finding's `produced_at`, the median gap between
# ticks on a rendered frame is 0.38 days, and only 2 of 580 gaps clear even the
# 3.0-day demotion bar. So the expected dormancy volume cannot be asserted from
# the horizon alone — it has to be measured against a population shaped like the
# live one. These two tests are that measurement.
# ---------------------------------------------------------------------------

#: The live active stratum: n=13 rendered frames, mean evidence-anchor age 0.84d,
#: max ~3.5d (the two gaps in 580 that clear the register's 3.0d bar).
_WINDABLE_ANCHOR_AGES_DAYS = [
    0.1, 0.2, 0.38, 0.4, 0.5, 0.62, 0.8, 0.9, 1.1, 1.4, 2.0, 3.2, 3.5,
]
#: The never-adjudicated class: n=21 open frames with ZERO ledger rows of any
#: kind, running 27.7 to 77.8 days old. Anchored on the frame's OPENING.
_NEVER_CORROBORATED_AGES_DAYS = [
    27.7, 29.0, 31.4, 33.0, 35.9, 38.2, 40.0, 42.6, 45.1, 47.3, 50.0,
    52.8, 55.4, 58.0, 60.7, 63.2, 66.0, 68.9, 71.5, 74.4, 77.8,
]


def test_the_windable_corroboration_branch_produces_no_dormancy_at_all():
    """MEASURED, AND REPORTED AS A LIMIT RATHER THAN A WIN.

    Every frame the tracker is still adjudicating has its clock rewound about
    every nine hours. Against a FORTNIGHT horizon that branch contributes
    nothing — not "a little", nothing — and it would contribute nothing at the
    3.0-day bar either, since only 2 of 580 measured gaps reach even that. The
    re-key does not make an adjudicated frame go dormant and must not be sold as
    though it does; making that branch stall needs a world anchor (the newest
    signal NEW TO THE FRAME), which is a different and larger change.
    """
    now = datetime.now(timezone.utc)
    fired = [
        st._dormancy_verdict(
            _candidate(f"S{i}", evidence=0, member_hours_ago=1,
                       anchor_days_ago=age, corroborated=True),
            now=now, dormancy_days=tj.DORMANCY_DAYS,
        )
        for i, age in enumerate(_WINDABLE_ANCHOR_AGES_DAYS)
    ]
    assert [v for v in fired if v is not None] == [], (
        "the corroboration branch is windable at pipeline time; a fortnight gap "
        "essentially never occurs while the tracker is still adjudicating"
    )
    assert max(_WINDABLE_ANCHOR_AGES_DAYS) < tj.DORMANCY_DAYS


def test_every_predicted_dormancy_row_comes_from_the_unwindable_branch():
    """...and this is where the volume actually is.

    A frame the ledger has never moved anchors on its OPENING, and
    `situations.valid_from` only ever moves EARLIER — the upsert writes
    `LEAST(stored, min(members))`. So no amount of desk activity can make such a
    frame look younger, which is precisely the property the windability critique
    does not reach. All 21 clear the horizon on the first tick that examines
    them, and the population is one-shot: each writes ONE row and then reads
    `dormant` off its own ledger forever after.
    """
    now = datetime.now(timezone.utc)
    population = [
        _candidate(f"S{i}", evidence=0, member_hours_ago=1,
                   anchor_days_ago=age, corroborated=False)
        for i, age in enumerate(_NEVER_CORROBORATED_AGES_DAYS)
    ]
    fired = [
        st._dormancy_verdict(c, now=now, dormancy_days=tj.DORMANCY_DAYS)
        for c in population
    ]
    assert all(v is not None for v in fired)
    assert len(fired) == len(_NEVER_CORROBORATED_AGES_DAYS) == 21

    # THE RATE, stated as the report states it: 21 of a 34-frame population, all
    # from the branch a re-citing pipeline cannot wind, none from the branch it
    # can.
    whole_population = population + [
        _candidate(f"W{i}", evidence=0, anchor_days_ago=age, corroborated=True)
        for i, age in enumerate(_WINDABLE_ANCHOR_AGES_DAYS)
    ]
    total = [
        st._dormancy_verdict(c, now=now, dormancy_days=tj.DORMANCY_DAYS)
        for c in whole_population
    ]
    assert len([v for v in total if v is not None]) == 21
    assert len(whole_population) == 34

    # ONE-SHOT, not a flood: once settled, the same population declines.
    settled = [
        st._dormancy_verdict(
            _candidate(f"S{i}", evidence=0, anchor_days_ago=age,
                       corroborated=False, state=tj.STATE_DORMANT),
            now=now, dormancy_days=tj.DORMANCY_DAYS,
        )
        for i, age in enumerate(_NEVER_CORROBORATED_AGES_DAYS)
    ]
    assert [v for v in settled if v is not None] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_frame_with_fresh_desk_writes_but_stale_evidence_goes_dormant(scope):
    """THE RE-KEY THROUGH THE REAL BINDING PATH — descriptor off disk, deps
    builder, `run_method`, live pool.

    The frame's members are HOURS old (its desks are still writing) but they all
    sit behind the watermark, and the ledger has never moved it. On the member
    clock this frame could never go dormant; on the evidence clock it goes
    dormant on the first tick that examines it.
    """
    pool, target = scope
    name = f"Situation: busy desks quiet world {uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        members = [
            await _finding(conn, title=f"desk read {i}", body="no material change",
                           hours_ago=i, target_id=target)
            for i in range(1, 4)
        ]
        sid = await _situation(
            conn, name=name, members=members, target_id=target,
            last_event_hours_ago=1,        # the MEMBER clock: an hour old
            opened_days_ago=40,            # the EVIDENCE clock: never corroborated
        )
    # Seed run: records watermarks for every open frame and emits nothing, so
    # this frame's own fresh members are behind the cursor on the next tick.
    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, _StubLLM([]))
    await run_method([], {}, kind_deps)

    llm = _StubLLM([])
    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, llm)
    result = await run_method([], {}, kind_deps)

    mine = _my_events(result, sid)
    assert len(mine) == 1, (
        "the member clock reads one hour; only the evidence clock can make this "
        "frame dormant"
    )
    assert mine[0].delta == tj.DELTA_UNCHANGED_CHECKPOINT
    assert mine[0].state_to == tj.STATE_DORMANT
    assert mine[0].derived_from == ()
    assert "never been corroborated" in mine[0].why

    async with pool.acquire() as conn:
        assert await tj.record_situation_events(
            conn, events=mine, source_output_id=uuid4(),
        ) == 1
        # It STICKS: the dormancy row is the ledger's newest by `occurred_at`,
        # which is what makes the verdict stable rather than hourly.
        assert (await tj.read_current_states(conn, [sid]))[str(sid)] == (
            tj.STATE_DORMANT
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_corroborated_frame_inside_the_horizon_is_left_alone(scope):
    """The other half, through the same real path: a frame the ledger moved
    yesterday keeps its state, whatever its members' age. `read_corroboration`
    is the read that decides, and this proves it is actually WIRED — before #64
    the tracker never called it."""
    pool, target = scope
    async with pool.acquire() as conn:
        member = await _finding(conn, title="Old read", body="old",
                                hours_ago=24 * 40, target_id=target)
        sid = await _situation(
            conn, name=f"Situation: recently moved {uuid4().hex[:8]}",
            members=[member], target_id=target,
            last_event_hours_ago=24 * 40, opened_days_ago=60,
        )
        # A real significant delta, yesterday: the frame IS corroborated.
        await tj.record_situation_events(
            conn,
            events=[tj.TrajectoryEvent(
                situation_id=sid,
                occurred_at=datetime.now(timezone.utc) - timedelta(days=1),
                delta=tj.DELTA_ESCALATES,
                why="the cited item reports a new deployment",
                state_from=tj.STATE_WATCHING, state_to=tj.STATE_ESCALATING,
                derived_from=(member,),
            )],
            source_output_id=uuid4(),
            verification={"faithfulness_score": 0.9},
        )
    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, _StubLLM([]))
    await run_method([], {}, kind_deps)

    run_method, kind_deps, _ok, _rc, _rs = await _build(pool, _StubLLM([]))
    result = await run_method([], {}, kind_deps)
    assert _my_events(result, sid) == [], (
        "the world moved this frame yesterday; a 60-day-old opening must not "
        "override a real corroboration"
    )
# ---------------------------------------------------------------------------
# REGISTER-1f — the MIXED budget breaks the top-N absorbing state
# ---------------------------------------------------------------------------

#: Intensities for the 1f fixture, set ABOVE this file's ``_TOP_INTENSITY`` and
#: strictly ordered against each other.
#:
#: ``_OPEN_SITUATIONS_SQL`` is GLOBAL — no target filter, by design, because the
#: tracker is a global META sweep — so a selection test on a shared session DB is
#: competing with every other open frame the suite has left lying around. Rather
#: than truncate a table the append-only ledger and ``hypotheses`` both reference,
#: the fixture wins both orderings OUTRIGHT: the loud frames are the highest
#: intensity in the database, and the quiet frame is the highest-intensity
#: NEVER-ADJUDICATED one, which is what the staleness leg's tie-break sorts on.
_LOUD_INTENSITY = _TOP_INTENSITY * 2       # wins the intensity leg globally
_QUIET_INTENSITY = _TOP_INTENSITY * 1.5    # loses it, wins the staleness leg
_QUIET_2_INTENSITY = _TOP_INTENSITY * 1.4  # the next one in the backlog queue


async def _mark_adjudicated(conn: Any, sid: UUID, *, hours_ago: float) -> None:
    """Give a frame a ledger row, i.e. make it a frame the tracker HAS looked at.

    ``created_at`` is set explicitly because that is the column the staleness leg
    orders on — write time ("when did we last look"), never ``occurred_at`` (the
    corroboration clock, which is the self-referential number 1f is getting OUT
    of the selector).
    """
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    await conn.execute(
        """
        INSERT INTO situation_events
            (situation_id, occurred_at, delta, state_from, state_to, why,
             derived_from, source_output_id, created_at)
        VALUES ($1, $2, 'unchanged_checkpoint', 'escalating', 'escalating',
                'seeded by the REGISTER-1f fixture', '{}'::uuid[], $3, $2)
        """,
        sid, when, uuid4(),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_quiet_never_adjudicated_frame_is_picked_up_by_the_staleness_leg(
    scope,
):
    """REGISTER-1f. ``ORDER BY intensity_score DESC LIMIT 12`` was an ABSORBING
    state: adjudicating a frame winds its corroboration clock, which raises its
    persistence, which raises its intensity, which is what selects it again. Live
    on 2026-08-29 the separation was total and gapless — 12/12 frames inside the
    window adjudicated at mean persistence 0.956, 21 of the 37 below it NEVER
    adjudicated at 0.296, and no frame anywhere between intensity 49.83 and 54.41.

    Same seed, both selectors, through the REAL ``gather_candidates`` against the
    REAL SQL. With the staleness leg switched off the quiet frame is invisible;
    with it on the frame is picked up — and the loud frames are NOT evicted,
    because the total budget never changed.
    """
    pool, target = scope
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        # THREE LOUD FRAMES — top of the intensity ranking, all adjudicated
        # within the hour. These are the top-12 window.
        loud: list[UUID] = []
        for i in range(3):
            sid = await _situation(
                conn, name=f"1f loud {i} {uuid4().hex[:8]}", members=[],
                target_id=target, intensity=_LOUD_INTENSITY,
            )
            await _mark_adjudicated(conn, sid, hours_ago=1)
            loud.append(sid)

        # TWO QUIET FRAMES — below the cut and NEVER adjudicated: zero ledger
        # rows, which is 21 of the live register's 49 open frames. Two, not one,
        # so the drain assertion at the end has somewhere for the slot to GO;
        # with a single backlog frame "it keeps its slot" would be the correct
        # answer and the assertion would be measuring the fixture.
        quiet = await _situation(
            conn, name=f"1f quiet {uuid4().hex[:8]}", members=[],
            target_id=target, intensity=_QUIET_INTENSITY,
        )
        quiet_2 = await _situation(
            conn, name=f"1f quiet-next {uuid4().hex[:8]}", members=[],
            target_id=target, intensity=_QUIET_2_INTENSITY,
        )
        for sid in (quiet, quiet_2):
            assert await conn.fetchval(
                "SELECT count(*) FROM situation_events WHERE situation_id = $1",
                sid,
            ) == 0

        common = dict(
            watermarks={}, now=now, max_evidence=1,
            window_hours=st.DEFAULT_WINDOW_HOURS, floor=st.DEFAULT_FLOOR,
        )

        # THE OLD SELECTOR — pure intensity top-N. The budget is spent on the
        # three loud frames and the quiet one is never looked at.
        before, _ = await st.gather_candidates(
            conn, max_situations=3, staleness_fraction=0.0, **common,
        )
        before_ids = {c.situation_id for c in before}
        assert set(loud) <= before_ids, "the loud frames must win the intensity leg"
        assert quiet not in before_ids and quiet_2 not in before_ids, (
            "fixture is not reproducing the defect: the quiet frames must be "
            "BELOW the cut under a pure intensity ranking"
        )
        assert not any(c.selected_by_staleness for c in before)

        # THE MIXED BUDGET — same TOTAL, one slot reserved. 4 slots at a 1/3
        # fraction is 3 by intensity + 1 by staleness.
        assert st.staleness_slots(4, st.DEFAULT_STALENESS_FRACTION) == 1
        after, examined = await st.gather_candidates(
            conn, max_situations=4, **common,
        )
        after_ids = {c.situation_id for c in after}
        assert quiet in after_ids, (
            "a never-adjudicated frame below the intensity cut must now be "
            "picked up — this is the whole of 1f"
        )
        # The loud frames are NOT evicted: the relief valve is additive within a
        # budget the operator set, not a reallocation away from real signal.
        assert set(loud) <= after_ids
        assert examined == len(after) == 4

        # The leg is ATTRIBUTED, so the run receipt can say whether the valve is
        # doing anything — and the quiet frame is on the staleness side of it.
        by_id = {c.situation_id: c for c in after}
        assert by_id[quiet].selected_by_staleness is True
        assert all(by_id[s].selected_by_staleness is False for s in loud)

        # AND THE LEG DRAINS: once the quiet frame has been adjudicated it goes
        # to the BACK of the staleness queue rather than staying pinned to the
        # front. That negative feedback sign is what makes this leg something the
        # tracker cannot wind, unlike the intensity it produces.
        await _mark_adjudicated(conn, quiet, hours_ago=0)
        drained, _ = await st.gather_candidates(
            conn, max_situations=4, **common,
        )
        stale_ids = {c.situation_id for c in drained if c.selected_by_staleness}
        assert quiet not in stale_ids, (
            "an adjudicated frame must yield its staleness slot to the next one"
        )
        assert quiet_2 in stale_ids, (
            "and the next never-adjudicated frame must take it — a backlog that "
            "does not advance is the absorbing state with an extra step"
        )


@pytest.mark.asyncio
async def test_the_tick_budget_is_env_gated_and_the_split_is_proportional(
    monkeypatch,
):
    """REGISTER-1f, the DIAL. #64's migration 0188 splits the ``sig:country_*``
    mega-frames and multiplies the open-frame population an expected 4-6x, so the
    budget has to be raisable on the day that lands — from the environment, not
    from a registry write plus a redeploy.

    And the split must be a RATIO, or raising the budget silently returns the
    tick to a pure top-N with a fixed garnish: at 12 the reserved leg is 4, at 60
    it must be 20, not 4.
    """
    monkeypatch.delenv(st._MAX_SITUATIONS_ENV, raising=False)
    assert st.max_situations_budget() == st.DEFAULT_MAX_SITUATIONS == 12
    assert st.max_situations_budget(20) == 20, "descriptor option is honored"

    monkeypatch.setenv(st._MAX_SITUATIONS_ENV, "60")
    assert st.max_situations_budget(12) == 60, "env wins over the descriptor"

    # Degrade-not-break: a typo must not stop the tracker adjudicating.
    monkeypatch.setenv(st._MAX_SITUATIONS_ENV, "not-a-number")
    assert st.max_situations_budget(12) == 12
    # Runaway guard: one tick fans every selected frame out to an LLM call.
    monkeypatch.setenv(st._MAX_SITUATIONS_ENV, "100000")
    assert st.max_situations_budget(12) == st.MAX_SITUATIONS_CEILING

    # The split rides the budget.
    assert st.staleness_slots(12) == 4      # 8 + 4, today
    assert st.staleness_slots(60) == 20     # 40 + 20, post-0188
    assert st.staleness_slots(24) == 8
    # Neither leg can be starved to nothing by a degenerate budget/fraction.
    assert st.staleness_slots(2) == 1
    assert st.staleness_slots(1) == 0
    assert st.staleness_slots(12, 0.0) == 0
    assert st.staleness_slots(12, 1.0) == 11
