# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E4b — the entity_researcher ADJUDICATOR (LLM verdicts -> entity_judgement).

Side-effect-free w.r.t. the graph: it only writes the ``entity_judgement`` audit
table (0086). Covered here (stub LLM, real migrated DB):

  * a batch is parsed + persisted; verdicts map back by ``n``;
  * the pair_key CACHE short-circuits a second pass (LLM not re-called);
  * an LLM that RAISES degrades the whole batch to ``unsure`` (never ``same``);
  * tolerant JSON parse (fenced array; a missing pair -> unsure);
  * a HUMAN verdict is never clobbered by an llm pass;
  * model_id is stamped; verdict-string coercion.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data._entity_candidates import CandidatePair
from legba.data.analysts.entity_researcher import (
    Verdict,
    _coerce_verdict,
    _extract_json_array,
    adjudicate_pairs,
)
from legba.data.config import PostgresConfig


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubLLM:
    """Returns a fixed ``content`` for every call; records call count."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kw):
        self.calls.append({"messages": messages, "system": system})
        return _Resp(self._content)


class _RaisingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_complete(self, *a, **k):
        self.calls += 1
        raise RuntimeError("model down")


def _pair(a_name: str, b_name: str, *, a_cls="person", b_cls="person",
          a_id=None, b_id=None, band="gray") -> CandidatePair:
    a_id = a_id or str(uuid4())
    b_id = b_id or str(uuid4())
    lo, hi = sorted((a_id, b_id))
    return CandidatePair(
        left_id=a_id, left_name=a_name, left_class=a_cls,
        right_id=b_id, right_name=b_name, right_class=b_cls,
        band=band, score=0.7, signals=("trgm:0.7",), block_key="",
    )


# ---------------------------------------------------------------------------
# Pure parse / coerce
# ---------------------------------------------------------------------------


def test_extract_json_array_tolerant():
    assert _extract_json_array('[{"n":1,"verdict":"same"}]')[0]["verdict"] == "same"
    fenced = "```json\n[{\"n\":1,\"verdict\":\"not_same\"}]\n```"
    assert _extract_json_array(fenced)[0]["verdict"] == "not_same"
    # prose around the array
    noisy = 'Here you go:\n[{"n":1,"verdict":"unsure"}]\nHope that helps'
    assert _extract_json_array(noisy)[0]["verdict"] == "unsure"
    assert _extract_json_array("not json at all") == []


def test_coerce_verdict_defaults_to_unsure():
    assert _coerce_verdict("same") == "same"
    assert _coerce_verdict("NOT-SAME") == "not_same"
    assert _coerce_verdict("different") == "not_same"
    assert _coerce_verdict("yes") == "same"
    assert _coerce_verdict("banana") == "unsure"
    assert _coerce_verdict(None) == "unsure"


# ---------------------------------------------------------------------------
# DB-backed adjudication
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_persisted_and_mapped(pg_pool):
    p1 = _pair("United States", "the United States of America")
    p2 = _pair("Ali Khamenei", "Mojtaba Khamenei")
    llm = _StubLLM(json.dumps([
        {"n": 1, "verdict": "same", "confidence": 0.98, "why": "one country"},
        {"n": 2, "verdict": "not_same", "confidence": 0.95, "why": "father/son"},
    ]))
    async with pg_pool.acquire() as conn:
        verdicts = await adjudicate_pairs(conn, llm, [p1, p2], model_id="stub-1")
        assert {v.pair_key: v.verdict for v in verdicts} == {
            p1.pair_key: "same", p2.pair_key: "not_same",
        }
        assert all(v.model_id == "stub-1" for v in verdicts)
        # persisted
        row = await conn.fetchrow(
            "SELECT verdict, decided_by, model_id FROM entity_judgement "
            "WHERE pair_key=$1", p2.pair_key)
    assert row["verdict"] == "not_same"
    assert row["decided_by"] == "llm"
    assert row["model_id"] == "stub-1"
    assert len(llm.calls) == 1  # one batch


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cache_short_circuits_second_pass(pg_pool):
    p1 = _pair("Hezbollah", "Hizbullah", a_cls="organization", b_cls="organization")
    llm = _StubLLM(json.dumps([{"n": 1, "verdict": "same", "confidence": 0.9}]))
    async with pg_pool.acquire() as conn:
        await adjudicate_pairs(conn, llm, [p1])
        assert len(llm.calls) == 1
        # second pass: cached -> the LLM must NOT be called again
        again = await adjudicate_pairs(conn, llm, [p1])
    assert len(llm.calls) == 1, "cached pair must not re-hit the LLM"
    assert again[0].verdict == "same"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_raising_llm_degrades_to_unsure(pg_pool):
    p1 = _pair("Foo", "Bar")
    llm = _RaisingLLM()
    async with pg_pool.acquire() as conn:
        verdicts = await adjudicate_pairs(conn, llm, [p1])
    assert verdicts[0].verdict == "unsure"  # never a silent 'same'
    assert llm.calls == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_pair_in_reply_defaults_unsure(pg_pool):
    p1 = _pair("Aaa", "Bbb")
    p2 = _pair("Ccc", "Ddd")
    # model only answered pair 1
    llm = _StubLLM(json.dumps([{"n": 1, "verdict": "same", "confidence": 0.8}]))
    async with pg_pool.acquire() as conn:
        verdicts = await adjudicate_pairs(conn, llm, [p1, p2])
    by_key = {v.pair_key: v.verdict for v in verdicts}
    assert by_key[p1.pair_key] == "same"
    assert by_key[p2.pair_key] == "unsure"  # omitted -> safe default


@pytest.mark.integration
@pytest.mark.asyncio
async def test_name_echo_overrides_wrong_n(pg_pool):
    # Review HIGH fix: the model echoes pair1's NAMES but mislabels it n=2
    # (off-by-one). The verdict must bind to pair1 BY NAMES, never to pair2 (the
    # father/son pair), which stays unsure — no cross-assigned 'same'.
    p1 = _pair("United States", "USA")
    p2 = _pair("Ali Khamenei", "Mojtaba Khamenei")
    llm = _StubLLM(json.dumps([
        {"n": 2, "a": "United States", "b": "USA", "verdict": "same",
         "confidence": 0.95},
    ]))
    async with pg_pool.acquire() as conn:
        verdicts = await adjudicate_pairs(conn, llm, [p1, p2])
    by = {v.pair_key: v.verdict for v in verdicts}
    assert by[p1.pair_key] == "same"     # bound by echoed names
    assert by[p2.pair_key] == "unsure"   # father/son NOT wrongly assigned 'same'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_echoed_names_matching_nothing_are_dropped(pg_pool):
    p1 = _pair("Foo Corp", "Foo Incorporated",
               a_cls="organization", b_cls="organization")
    llm = _StubLLM(json.dumps([
        {"n": 1, "a": "Totally", "b": "Different", "verdict": "same",
         "confidence": 0.9},
    ]))
    async with pg_pool.acquire() as conn:
        verdicts = await adjudicate_pairs(conn, llm, [p1])
    assert verdicts[0].verdict == "unsure"  # echoed names match no pair -> dropped


@pytest.mark.asyncio
async def test_prompt_carries_transliteration_guidance():
    """E4a recall lever: the system prompt must carry the transliteration /
    honorific-prefix guidance (the LLM wrongly split "Ali Khamenei" vs "Seyyed
    Ali Khameni" and "Imam Hussein" vs "Imam Hussain") while KEEPING the
    father/son conservative guard. No DB needed: use_cache/persist off means
    the conn is never touched, so the assembled system prompt is observable
    straight off the stub."""
    p1 = _pair("Imam Zzhussein", "Imam Zzhussain")
    llm = _StubLLM(json.dumps([{"n": 1, "verdict": "same", "confidence": 0.9}]))
    verdicts = await adjudicate_pairs(
        None, llm, [p1], use_cache=False, persist=False)
    assert verdicts[0].verdict == "same"
    system = llm.calls[0]["system"]
    # the new guidance
    assert "TRANSLITERATION" in system
    assert "Seyyed" in system and "Sayyid" in system
    assert '"Hussein"/"Hussain"' in system
    assert '"Khamenei"/"Khameni"' in system
    assert "diacritic" in system
    # the conservative posture is intact: father/son stays the canonical error
    assert "Mojtaba Khamenei" in system
    assert 'NEVER guess "same"' in system
    assert "be conservative" in system


@pytest.mark.integration
@pytest.mark.asyncio
async def test_human_verdict_not_clobbered(pg_pool):
    p1 = _pair("Manual", "Manually Decided")
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO entity_judgement
                 (pair_key, entity_a, entity_b, verdict, justification, decided_by)
               VALUES ($1,$2::uuid,$3::uuid,'not_same','human said no','human')""",
            p1.pair_key, p1.left_id, p1.right_id)
        # An llm pass that says 'same' with use_cache off must NOT overwrite human.
        llm = _StubLLM(json.dumps([{"n": 1, "verdict": "same", "confidence": 0.99}]))
        await adjudicate_pairs(conn, llm, [p1], use_cache=False)
        row = await conn.fetchrow(
            "SELECT verdict, decided_by FROM entity_judgement WHERE pair_key=$1",
            p1.pair_key)
    assert row["verdict"] == "not_same" and row["decided_by"] == "human"
