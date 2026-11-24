# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W4-F1 — the consult watch fold made semantic, and DEC-19.

What is proven here, and on what:

  * THE FOUR LIVE ROWS (pure, exact texts from country_watch_ir 2026-10-03):
    the arms pair folds; the naval row and the enrichment row fold onto
    nothing — the brief's "two pairs" was measured to be ONE pair (the naval
    row's horizon is 7 days from every other row, and neither it nor the
    enrichment row is the same claim as anything else).
  * WHY A CLAIM CORE (recorded vectors, the live ``embedding-inno1``): on the
    RAW texts the duplicate pair (0.712) scores BELOW a different claim
    (arms vs naval, 0.714); on the cores the duplicate is identical, a true
    paraphrase scores 0.947, every cross pair <= 0.57 and a negation 0.71.
  * THE DECISION with a fake embedder of fixed vectors: threshold, horizon
    window, earliest-wins, the over-cap and unavailable paths.
  * THE BATCH: one ``embed_many`` call; failure, timeout or a short answer is
    ``None`` (fail closed to mint), never raised.
  * THE MINT (real driver): a re-phrased re-ask folds onto the standing row
    (``folded_onto`` + ``similarity``), ONE embed call per turn; an unavailable
    embedder mints and stamps ``fold_check: unavailable`` on entry and marker.
  * DEC-19 (real driver): ``read_desk_open_questions`` and the R-1 backlog
    exclude consult watches with the flag off and include them with it on;
    claim_watch's open set keeps them either way.
  * THE SCRIPT (real driver): dry-run writes nothing; ``--apply`` stamps the
    later row ``folded_onto`` the earlier, status unchanged, nothing deleted;
    a second apply folds nothing.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts import consult_watch_fold as wf
from legba.data.analysts import consult_watch_mint as wm
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext
from legba.data.provenance.models import ConsultResponsePayload, FindingPayload

_REPO = Path(__file__).resolve().parents[2]
_FIXTURE = Path(__file__).parent / "fixtures" / "consult_watch_fold_vectors.json"

H17 = datetime(2026, 10, 17, tzinfo=timezone.utc)
H10 = datetime(2026, 10, 10, tzinfo=timezone.utc)

#: The four live rows on country_watch_ir (2026-10-03), their marker
#: ``resolution_test`` verbatim, their horizon and their mint time.
LIVE = {
    "e6227510": ("major-arms-acquisition indicator observed", H17,
                 datetime(2026, 10, 3, 21, 21, 22, tzinfo=timezone.utc)),
    "534b11fb": ("naval-buildup indicator observed", H10,
                 datetime(2026, 10, 3, 21, 21, 23, tzinfo=timezone.utc)),
    "53d11647": ("detection of a major arms acquisition (e.g., new missile system", H17,
                 datetime(2026, 10, 3, 22, 6, 10, tzinfo=timezone.utc)),
    "fd543959": ("evidence of an enrichment-level increase (e.g., higher-grade uranium", H17,
                 datetime(2026, 10, 3, 22, 6, 11, tzinfo=timezone.utc)),
}


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    return wf.cosine(a, b)  # type: ignore[return-value]


def _recorded() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _retro_fold(vectors: dict[str, list[float]] | None) -> dict[str, str | None]:
    """The four live rows decided oldest-first, exactly as the script does."""
    standing: list[wf.FoldPeer] = []
    out: dict[str, str | None] = {}
    for hid, (text, horizon, produced) in sorted(LIVE.items(), key=lambda kv: kv[1][2]):
        core = wf.claim_core(text)
        vec = vectors.get(core) if vectors is not None else None
        d = wf.decide_fold(core, vec, horizon, standing, embedded=vectors is not None)
        out[hid] = d.onto
        if d.onto is None:
            standing.append(wf.FoldPeer(id=hid, core=core, resolves_by=horizon,
                                        produced_at=produced, vec=vec))
    return out


# ---------------------------------------------------------------------------
# Pure — the claim core and the four live rows
# ---------------------------------------------------------------------------


def test_the_claim_core_strips_the_watch_framing_and_keeps_a_negation():
    assert wf.claim_core(LIVE["e6227510"][0]) == "major arms acquisition"
    assert wf.claim_core(LIVE["53d11647"][0]) == "major arms acquisition"
    assert wf.claim_core(LIVE["534b11fb"][0]) == "naval buildup"
    assert wf.claim_core(LIVE["fd543959"][0]) == "enrichment level increase"
    # Never strips a negated observation, and keeps a plain claim whole.
    assert wf.claim_core("major-arms-acquisition indicator not observed") == (
        "major arms acquisition indicator not observed"
    )
    assert wf.claim_core("no major arms acquisition is observed") == "no major arms acquisition"
    assert wf.claim_core("The Moldovan vote is postponed") == "moldovan vote is postponed"
    assert wf.claim_core("evidence observed") == "evidence"


def test_the_four_live_rows_fold_as_one_pair_and_not_across_without_vectors():
    """Identical claim cores fold with no embedder at all; nothing else does."""
    assert _retro_fold({}) == {
        "e6227510": None, "534b11fb": None, "53d11647": "e6227510", "fd543959": None,
    }


def test_the_four_live_rows_on_the_recorded_vectors():
    rec = _recorded()
    assert rec["component"] == "embed.primary.openai_compat"
    assert rec["dim"] == 1024
    assert _retro_fold(rec["cores"]) == {
        "e6227510": None, "534b11fb": None, "53d11647": "e6227510", "fd543959": None,
    }
    cores = rec["cores"]
    arms, naval, enrich = (cores["major arms acquisition"], cores["naval buildup"],
                           cores["enrichment level increase"])
    t = wf.FOLD_COSINE_THRESHOLD
    # Every cross pair sits well under the threshold...
    for a, b in ((arms, naval), (arms, enrich), (naval, enrich)):
        assert _cos(a, b) < t - 0.25
    # ...a true paraphrase clears it, a negation does not.
    assert _cos(arms, cores["major arms purchase"]) >= t + 0.05
    assert _cos(arms, cores["no major arms acquisition"]) < t - 0.1
    assert _cos(arms, cores["major arms acquisition indicator not observed"]) < t - 0.1


def test_the_raw_texts_cannot_separate_the_duplicate_from_a_different_claim():
    """The measurement behind the claim core: on the raw indicator texts the
    duplicate pair scores no higher than arms-vs-naval, so NO raw threshold
    folds the one without the other."""
    raw = _recorded()["raw"]
    arms_a, naval, arms_b, enrich = (raw[LIVE[k][0]] for k in
                                     ("e6227510", "534b11fb", "53d11647", "fd543959"))
    pair = _cos(arms_a, arms_b)
    assert pair < wf.FOLD_COSINE_THRESHOLD
    assert _cos(arms_a, naval) >= pair - 0.01
    assert max(_cos(arms_b, enrich), _cos(arms_a, enrich)) > 0.5


# ---------------------------------------------------------------------------
# Pure — the decision with fixed vectors
# ---------------------------------------------------------------------------

#: Fixed unit-ish vectors: purchase ~ acquisition at 0.95, the negation at 0.70,
#: naval at 0.57 — the recorded shape, in three dimensions.
FIXED = {
    "major arms acquisition": [1.0, 0.0, 0.0],
    "major arms purchase": [0.95, math.sqrt(1 - 0.95 ** 2), 0.0],
    "no major arms acquisition": [0.70, 0.0, math.sqrt(1 - 0.70 ** 2)],
    "naval buildup": [0.57, 0.0, -math.sqrt(1 - 0.57 ** 2)],
    "enrichment level increase": [0.43, -math.sqrt(1 - 0.43 ** 2), 0.0],
}


def _peer(core: str, horizon: datetime = H17, *, hid: str | None = None,
          produced: datetime | None = None) -> wf.FoldPeer:
    return wf.FoldPeer(id=hid or core, core=core, resolves_by=horizon,
                       produced_at=produced, vec=FIXED.get(core))


def test_fixed_vectors_fold_a_paraphrase_and_not_a_negation_or_another_claim():
    peers = [_peer("major arms acquisition")]
    d = wf.decide_fold("major arms purchase", FIXED["major arms purchase"], H17, peers)
    assert (d.check, d.onto, d.similarity) == ("semantic", "major arms acquisition", 0.95)
    for core in ("no major arms acquisition", "naval buildup", "enrichment level increase"):
        d = wf.decide_fold(core, FIXED[core], H17, peers)
        assert d.check == "semantic" and d.onto is None
        assert d.similarity < wf.FOLD_COSINE_THRESHOLD


def test_the_horizon_window_is_three_days_either_side():
    peers = [_peer("major arms acquisition", H17)]
    vec = FIXED["major arms purchase"]
    assert wf.decide_fold("major arms purchase", vec, H17 + timedelta(days=3), peers).onto
    assert wf.decide_fold("major arms purchase", vec, H17 - timedelta(days=3), peers).onto
    late = wf.decide_fold("major arms purchase", vec, H17 + timedelta(days=4), peers)
    assert (late.check, late.onto) == ("no_peers", None)
    # Identical cores outside the window do not fold either.
    assert wf.decide_fold("major arms acquisition", None, H10, peers).check == "no_peers"


def test_identical_cores_fold_onto_the_earliest_with_no_vector():
    early = _peer("major arms acquisition", hid="early", produced=H10 - timedelta(days=9))
    late = _peer("major arms acquisition", hid="late", produced=H10 - timedelta(days=2))
    d = wf.decide_fold("major arms acquisition", None, H17, [late, early], embedded=False)
    assert (d.check, d.onto, d.similarity) == ("core", "early", 1.0)


def test_comparable_peers_without_vectors_are_unavailable_not_refused():
    d = wf.decide_fold("major arms purchase", None, H17, [_peer("major arms acquisition")],
                       embedded=False)
    assert (d.check, d.onto) == ("unavailable", None)


def test_the_plan_caps_candidates_and_reports_over_cap():
    plan = wf.FoldPlan(peers=[_peer("major arms acquisition")], vectors={}, embedded=True)
    over = plan.decide(wf.FOLD_MAX_CANDIDATES, "major arms purchase", H17)
    assert over.check == "over_cap" and over.onto is None
    # Over the cap an identical core still folds — no vector needed.
    assert plan.decide(wf.FOLD_MAX_CANDIDATES, "major arms acquisition", H17).onto
    unreadable = wf.FoldPlan(peers=[], vectors={}, embedded=False, peers_readable=False)
    assert unreadable.decide(0, "x", H17).check == "unavailable"


# ---------------------------------------------------------------------------
# The batch — one call, fails closed
# ---------------------------------------------------------------------------


class _FixedEmbedder:
    """A fake embedder: fixed vectors by claim core, counting its calls."""

    def __init__(self, vectors: dict[str, list[float]], *, fail: bool = False,
                 delay: float = 0.0) -> None:
        self.vectors = vectors
        self.fail = fail
        self.delay = delay
        self.calls: list[list[str]] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("embedder down")
        return [self.vectors.get(t, [0.0, 0.0, 1.0]) for t in texts]


class _SingleEmbedder:
    def __init__(self) -> None:
        self.n = 0

    async def embed(self, text: str) -> list[float]:
        self.n += 1
        return FIXED[text]


@pytest.mark.asyncio
async def test_embed_batch_is_one_call_and_fails_closed():
    emb = _FixedEmbedder(FIXED)
    vecs = await wf.embed_batch(emb, ["naval buildup", "major arms acquisition"])
    assert len(emb.calls) == 1 and vecs == [FIXED["naval buildup"], FIXED["major arms acquisition"]]
    assert await wf.embed_batch(_FixedEmbedder(FIXED, fail=True), ["a"]) is None
    assert await wf.embed_batch(_FixedEmbedder(FIXED, delay=0.2), ["a"], timeout=0.05) is None
    assert await wf.embed_batch(None, ["a"]) is None
    single = _SingleEmbedder()
    assert await wf.embed_batch(single, ["naval buildup"]) == [FIXED["naval buildup"]]
    assert single.n == 1


def test_the_embedder_is_the_substrate_ports_own():
    from types import SimpleNamespace

    from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

    emb = _FixedEmbedder(FIXED)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=None, qdrant_client=None, embedder=emb)
    assert port.embedder is emb
    assert wf.resolve_fold_embedder(SimpleNamespace(substrate=port)) is emb
    assert wf.resolve_fold_embedder(SimpleNamespace(substrate=object())) is None
    assert wf.resolve_fold_embedder(None) is None


def test_mirrored_names_do_not_drift():
    assert wf._MARKER_KEY == wm.MARKER_KEY
    assert wf._ORIGIN == wm.MARKER_ORIGIN
    assert wf._CREATED_BY == wm.CREATED_BY
    assert json.loads(wf.CONSULT_WATCH_CONTAINMENT) == [
        {"marker": wm.MARKER_KEY, "created_by": wm.CREATED_BY}
    ]


def test_the_exclusion_clause_follows_the_flag(monkeypatch):
    monkeypatch.delenv(wf.DESK_GROUNDING_FLAG, raising=False)
    assert "created_by" in wf.consult_watch_exclusion_sql()
    assert wf.drop_consult_watches([{"diagnostic_evidence": [
        {"marker": wm.MARKER_KEY, "created_by": "consult"}]}]) == []
    monkeypatch.setenv(wf.DESK_GROUNDING_FLAG, "1")
    assert wf.consult_watch_exclusion_sql() == ""
    assert len(wf.drop_consult_watches([{"diagnostic_evidence": [
        {"marker": wm.MARKER_KEY, "created_by": "consult"}]}])) == 1


# ---------------------------------------------------------------------------
# Real driver
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    try:
        async with p.acquire() as c:
            await c.execute(
                "DELETE FROM hypotheses WHERE status = 'open_question' "
                "AND (diagnostic_evidence @> $1::jsonb OR analyst_id = 'w4f1_desk_unit')",
                json.dumps([{"marker": wm.MARKER_KEY, "origin": "consult"}]),
            )
            await c.execute("DELETE FROM analyst_outputs WHERE analyst_id = 'w4f1_desk_unit'")
    finally:
        await p.close()


def _future(days: int) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )


def _options() -> dict[str, Any]:
    return {"analyst_id": "consult_default", "analyst_version": "v-w4f1", "run_id": uuid4()}


async def _finding(pool: Any, desk: str) -> UUID:
    from legba.data.provenance.writes import write_finding

    async with pool.acquire() as c:
        row, _ = await write_finding(
            c,
            analyst_ctx=AnalystContext(analyst_id="w4f1_desk_unit", analyst_version="v1",
                                       run_id=uuid4(), target_id=desk),
            payload=FindingPayload(title=f"finding on {desk}"),
            derived_from=[],
        )
    return row.id


def _consult(candidates: list[str], refs: list[UUID]) -> ConsultResponsePayload:
    return ConsultResponsePayload(
        question=f"What is Iran's posture? {uuid4().hex[:6]}",
        answer="## Bottom line\n\nbody", cited_substrate_refs=refs,
        data={"watch_candidates": candidates},
    )


async def _mint(pool: Any, cands: list[str], refs: list[UUID], embedder: Any) -> list[dict]:
    consult = _consult(cands, refs)
    return await wm.mint_watch_candidates(
        consult, first={"question": consult.question}, options=_options(),
        pool=pool, embedder=embedder,
    )


async def _marker(pool: Any, hid: str) -> dict[str, Any]:
    async with pool.acquire() as c:
        raw = await c.fetchval(
            "SELECT diagnostic_evidence FROM hypotheses WHERE id = $1", UUID(hid))
    return json.loads(raw)[0]


async def _count(pool: Any, desk: str) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            "SELECT count(*) FROM hypotheses WHERE status = 'open_question' "
            "AND target_id = $1", desk)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rephrased_reask_folds_onto_the_standing_watch(pool):
    desk = f"country_w4f1_{uuid4().hex[:6]}"
    refs = [await _finding(pool, desk)]
    h = _future(14).date().isoformat()
    h_naval = _future(7).date().isoformat()
    emb = _FixedEmbedder(FIXED)
    first = await _mint(pool, [
        f"major-arms-acquisition indicator observed by {h}",
        f"naval-buildup indicator observed by {h_naval}",
    ], refs, emb)
    assert [e["fold_check"] for e in first] == ["no_peers", "no_peers"]
    assert emb.calls == []  # nothing to compare: no embed call at all
    arms_id = first[0]["hypothesis_id"]

    second = await _mint(pool, [
        f"detection of a major arms acquisition (e.g., new missile system) by {h}",
        f"evidence of an enrichment-level increase (e.g., higher-grade uranium) by {h}",
        f"major arms purchase observed by {h}",
    ], refs, emb)
    arms, enrich, purchase = second
    # The live pair: identical cores, folded onto the standing row, no write.
    assert (arms["folded"], arms["folded_onto"], arms["fold_check"]) == (True, arms_id, "core")
    assert arms["hypothesis_id"] == arms_id and arms["similarity"] == 1.0
    # A different claim on the same desk and horizon: checked, minted.
    assert enrich["folded"] is False and enrich["fold_check"] == "semantic"
    assert enrich["similarity"] < wf.FOLD_COSINE_THRESHOLD
    marker = await _marker(pool, enrich["hypothesis_id"])
    assert marker["fold_check"] == "semantic"
    assert marker["fold_similarity"] == enrich["similarity"]
    # A paraphrase: folded on the cosine.
    assert (purchase["folded"], purchase["folded_onto"]) == (True, arms_id)
    assert purchase["fold_check"] == "semantic" and purchase["similarity"] >= 0.86
    # ONE batched embed call for the whole turn.
    assert len(emb.calls) == 1
    assert await _count(pool, desk) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_rephrasings_in_one_turn_fold_onto_each_other(pool):
    desk = f"country_w4f1_{uuid4().hex[:6]}"
    refs = [await _finding(pool, desk)]
    h = _future(14).date().isoformat()
    emb = _FixedEmbedder(FIXED)
    out = await _mint(pool, [
        f"major-arms-acquisition indicator observed by {h}",
        f"major arms purchase observed by {h}",
    ], refs, emb)
    assert out[0]["folded"] is False
    assert (out[1]["folded"], out[1]["folded_onto"]) == (True, out[0]["hypothesis_id"])
    assert len(emb.calls) == 1
    assert await _count(pool, desk) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unavailable_embedder_mints_and_stamps_unavailable(pool):
    desk = f"country_w4f1_{uuid4().hex[:6]}"
    refs = [await _finding(pool, desk)]
    h = _future(14).date().isoformat()
    await _mint(pool, [f"major-arms-acquisition indicator observed by {h}"], refs, None)
    for emb in (None, _FixedEmbedder(FIXED, fail=True)):
        out = await _mint(pool, [f"major arms purchase {uuid4().hex[:4]} observed by {h}"],
                          refs, emb)
        entry = out[0]
        assert entry["refused"] is False and entry["folded"] is False
        assert entry["fold_check"] == "unavailable"
        assert (await _marker(pool, entry["hypothesis_id"]))["fold_check"] == "unavailable"
    assert await _count(pool, desk) == 3


# ---------------------------------------------------------------------------
# DEC-19 — the scheduled desks' grounding (real driver)
# ---------------------------------------------------------------------------


async def _seed_watch(pool: Any, desk: str | None, text: str, horizon: datetime,
                      produced: datetime) -> str:
    marker = {"marker": wm.MARKER_KEY, "origin": "consult", "created_by": "consult",
              "source_id": wm.fold_key(text), "resolution_test": text,
              "resolves_by": horizon.isoformat(), "candidate": text}
    async with pool.acquire() as c:
        return str(await c.fetchval(
            "INSERT INTO hypotheses (thesis, status, target_id, analyst_id, "
            "diagnostic_evidence, produced_at) VALUES ($1, 'open_question', $2, "
            "'consult_default', $3::jsonb, $4) RETURNING id",
            text, desk, json.dumps([marker]), produced,
        ))


async def _seed_desk_question(pool: Any, desk: str) -> str:
    async with pool.acquire() as c:
        return str(await c.fetchval(
            "INSERT INTO hypotheses (thesis, status, target_id, analyst_id, "
            "diagnostic_evidence) VALUES ($1, 'open_question', $2, 'w4f1_desk_unit', "
            "'[]'::jsonb) RETURNING id",
            "has the desk's own question been answered?", desk,
        ))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_desk_grounding_excludes_consult_watches_unless_flagged(pool, monkeypatch):
    from legba.data.analysts.deterministic_handlers.claim_watch import (
        DEFAULT_QUESTION_CAP,
        DEFAULT_QUESTION_STATUSES,
        _OPEN_QUESTIONS_SQL,
    )
    from legba.data.analysts.unit_grounding import read_desk_open_questions

    desk = f"country_w4f1_{uuid4().hex[:6]}"
    watch = await _seed_watch(pool, desk, "naval-buildup indicator observed",
                              _future(9), datetime.now(timezone.utc))
    own = await _seed_desk_question(pool, desk)

    monkeypatch.delenv(wf.DESK_GROUNDING_FLAG, raising=False)
    async with pool.acquire() as c:
        off = {q["question_id"] for q in await read_desk_open_questions(c, target_id=desk)}
    assert off == {own}
    monkeypatch.setenv(wf.DESK_GROUNDING_FLAG, "1")
    async with pool.acquire() as c:
        on = {q["question_id"] for q in await read_desk_open_questions(c, target_id=desk)}
        watched = {str(r["id"]) for r in await c.fetch(
            _OPEN_QUESTIONS_SQL, list(DEFAULT_QUESTION_STATUSES), DEFAULT_QUESTION_CAP)}
    assert on == {own, watch}
    # claim_watch's open set is untouched by DEC-19.
    assert watch in watched


class _RowsPool:
    """Hands ``resolve_open_questions`` the rows the real driver returned."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def acquire(self) -> "_RowsPool":
        return self

    async def __aenter__(self) -> "_RowsPool":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def fetch(self, *_a: Any) -> list[Any]:
        return self.rows


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_research_backlog_excludes_consult_watches_unless_flagged(pool, monkeypatch):
    """The R-1 drain would CLAIM a consult watch out of claim_watch's open set.
    The backlog SQL runs on the real driver; its rows for this test's two
    questions are then ranked by the real resolver (the global backlog's other
    rows would make a top-8 assertion depend on the shared test DB)."""
    from legba.runtime.grounding import (
        _OPEN_QUESTION_CANDIDATE_FETCH_CAP,
        _OPEN_QUESTION_FORWARD_WALK_MAX_DEPTH,
        _OPEN_QUESTIONS_BACKLOG_SQL,
        SubstrateGroundingResolver,
    )

    desk = f"country_w4f1_{uuid4().hex[:6]}"
    watch = await _seed_watch(pool, desk, "naval-buildup indicator observed",
                              _future(9), datetime.now(timezone.utc))
    own = await _seed_desk_question(pool, desk)
    async with pool.acquire() as c:
        rows = [r for r in await c.fetch(
            _OPEN_QUESTIONS_BACKLOG_SQL, _OPEN_QUESTION_FORWARD_WALK_MAX_DEPTH,
            _OPEN_QUESTION_CANDIDATE_FETCH_CAP,
        ) if str(r["id"]) in (watch, own)]
    assert len(rows) == 2
    resolver = SubstrateGroundingResolver(pg_pool=_RowsPool(rows))
    monkeypatch.delenv(wf.DESK_GROUNDING_FLAG, raising=False)
    off = {str(q.id) for q in await resolver.resolve_open_questions(limit=8)}
    monkeypatch.setenv(wf.DESK_GROUNDING_FLAG, "on")
    on = {str(q.id) for q in await resolver.resolve_open_questions(limit=8)}
    assert off == {own}
    assert on == {own, watch}


# ---------------------------------------------------------------------------
# The one-off script (real driver)
# ---------------------------------------------------------------------------


def _script() -> Any:
    path = _REPO / "scripts" / "fold_consult_watches_once.py"
    spec = importlib.util.spec_from_file_location("fold_consult_watches_once", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_script_dry_runs_then_folds_the_live_shape(pool):
    script = _script()
    desk = f"country_w4f1_{uuid4().hex[:6]}"
    h17, h10 = _future(14), _future(7)
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    ids = {}
    for k, (text, _h, _p) in LIVE.items():
        horizon = h10 if k == "534b11fb" else h17
        produced = t0 + (timedelta(minutes=45) if k in ("53d11647", "fd543959") else timedelta())
        produced += timedelta(seconds=list(LIVE).index(k))
        ids[k] = await _seed_watch(pool, desk, text, horizon, produced)

    async with pool.acquire() as c:
        dry = await script.fold_once(c, embedder=_FixedEmbedder(FIXED), apply=False,
                                     desks=[desk])
    assert dry["mode"] == "dry-run" and dry["scanned"] == 4 and dry["folds"] == 1
    assert dry["stamped"] == 0
    assert (dry["pairs"][0]["row"], dry["pairs"][0]["onto"]) == (ids["53d11647"], ids["e6227510"])
    assert "folded_onto" not in await _marker(pool, ids["53d11647"])

    async with pool.acquire() as c:
        applied = await script.fold_once(c, embedder=None, apply=True, desks=[desk])
    assert applied["folds"] == 1 and applied["stamped"] == 1
    marker = await _marker(pool, ids["53d11647"])
    assert marker["folded_onto"] == ids["e6227510"]
    assert marker["fold_check"] == "core" and marker["fold_similarity"] == 1.0
    async with pool.acquire() as c:
        statuses = await c.fetch(
            "SELECT id, status FROM hypotheses WHERE id = ANY($1::uuid[])",
            [UUID(i) for i in ids.values()])
    assert {r["status"] for r in statuses} == {"open_question"} and len(statuses) == 4
    for k in ("e6227510", "534b11fb", "fd543959"):
        assert "folded_onto" not in await _marker(pool, ids[k])

    async with pool.acquire() as c:
        again = await script.fold_once(c, embedder=None, apply=True, desks=[desk])
    assert again["scanned"] == 3 and again["folds"] == 0
    # A folded row is no longer a fold target for a NEW mint either.
    async with pool.acquire() as c:
        peers = await wf.read_fold_peers(c, desk)
    assert ids["53d11647"] not in {p.id for p in peers} and len(peers) == 3
