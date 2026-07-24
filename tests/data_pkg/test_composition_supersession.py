# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S8-T3 — composition supersession.

meta_findings_synthesizer COMPOSITION findings (per-country + world) carry no
entity/topic content, so ``finding_supersession.derive_signature`` returned None
and they never clustered — every cadence cycle left another live head (~8
concurrent US composition heads in the live symptom). The fix stamps a per-head
supersession signature that ENCODES ``target_id`` onto the composition payload's
``data['situation_signature']``, and ``derive_signature`` now reads that nested
key, so the heads fold to ONE canonical head per ``(analyst_id, target_id)``.

Layers (all non-infra unless noted):

  * ``_composition_signature`` encodes target_id (world → the 'world' literal).
  * The kind stamps the signature on target-scoped / world / honest-empty
    compositions, and NEVER on the legacy global meta.
  * ``derive_signature`` reads the nested composition signature.
  * End-to-end synthetic clustering: composition heads fold to one canonical head
    per (analyst_id, target_id); per-country heads of the SAME analyst do NOT
    collapse (the target-encoding requirement).
  * Live pivot-DB (env-gated): the 0058 migration folds the historical heads and
    a re-run is a no-op.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts.deterministic import run_method as det_run_method
from legba.data.analysts.deterministic_handlers import finding_supersession
from legba.data.migrations import MIGRATIONS_DIR
from legba.data.provenance.models import FindingPayload


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _CannedLLM:
    """Returns a caller-supplied JSON payload as the synthesis completion."""

    subprovider = "sup_test_double"

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kwargs):
        class _Usage:
            prompt_tokens = 10
            completion_tokens = 5
            reasoning_tokens = 0

        class _Response:
            pass

        resp = _Response()
        resp.content = json.dumps(self._payload)
        resp.usage = _Usage()
        return resp


class _NeverCalledLLM:
    subprovider = "never_called"

    async def chat_complete(self, *a, **k):  # pragma: no cover
        raise AssertionError("LLM must not be called on the empty-slice path")


class _Deps:
    def __init__(self, llm) -> None:
        self.llm = llm


def _subclaim(analyst_id: str = "leadership_transition"):
    return {
        "id": uuid4(),
        "kind": "finding",
        "title": "unit sub-claim",
        "body": "sub-claim body",
        "confidence": 0.7,
        "data": {"evidence": []},
        "evidence": [],
        "target_id": None,
        "analyst_id": analyst_id,
        "produced_at": "2026-06-30T00:00:00+00:00",
        "derived_from": [],
        "run_id": uuid4(),
    }


async def _compose(target_id: str | None, analyst_id: str) -> FindingPayload:
    """Run the kind once for a composition head and return its FindingPayload."""
    llm = _CannedLLM(
        {"title": "c", "body": "BLUF body [[ref:1]].", "confidence": 0.6,
         "evidence": [], "tags": ["composition"]}
    )
    options = {"analyst_id": analyst_id, "run_id": uuid4()}
    if target_id is not None:
        options["target_id"] = target_id
    else:
        options["composition"] = True
    result = await synth.run_method([_subclaim()], options, _Deps(llm))
    return result.finding


# ---------------------------------------------------------------------------
# _composition_signature — encodes target_id
# ---------------------------------------------------------------------------


def test_composition_signature_encodes_target():
    us = synth._composition_signature("country_composition", "country_g20_us")
    inn = synth._composition_signature("country_composition", "country_g20_in")
    world = synth._composition_signature("world_assessor", None)
    assert us == "composition:country_composition:country_g20_us"
    assert inn == "composition:country_composition:country_g20_in"
    # The world head (no target_id) uses the 'world' literal.
    assert world == "composition:world_assessor:world"
    # SAME analyst, different countries → DIFFERENT signatures (a bare per-analyst
    # sig would collapse every country's composition into one head).
    assert us != inn


def test_composition_signature_falls_back_on_missing_analyst():
    assert synth._composition_signature(None, "country_g20_us") == (
        "composition:unknown:country_g20_us"
    )


# ---------------------------------------------------------------------------
# The kind stamps the signature (composition) / never (legacy global meta)
# ---------------------------------------------------------------------------


async def test_target_scoped_composition_stamps_signature():
    finding = await _compose("country_g20_us", "country_composition")
    assert finding.data["situation_signature"] == (
        "composition:country_composition:country_g20_us"
    )


async def test_world_composition_stamps_signature():
    finding = await _compose(None, "world_assessor")
    assert finding.data["situation_signature"] == "composition:world_assessor:world"


async def test_honest_empty_composition_stamps_signature():
    """A country with no verified sub-claims (empty slice) short-circuits before
    the LLM but STILL carries the signature, so its diagnostic head folds too."""
    result = await synth.run_method(
        [],
        {"analyst_id": "country_composition", "target_id": "country_g20_zz",
         "run_id": uuid4()},
        _Deps(_NeverCalledLLM()),
    )
    assert result.finding.confidence == 0.0
    assert "empty_slice" in result.finding.tags
    assert result.finding.data["situation_signature"] == (
        "composition:country_composition:country_g20_zz"
    )


async def test_legacy_global_meta_gets_no_signature():
    """No target_id and no composition flag ⇒ the legacy global meta: byte-for-byte
    unchanged, NO signature stamp (so its clustering behavior is untouched)."""
    llm = _CannedLLM(
        {"title": "t", "body": "global synthesis", "confidence": 0.6,
         "evidence": [], "tags": ["synth"]}
    )
    result = await synth.run_method(
        [_subclaim(analyst_id="country_assessor")],
        {"analyst_id": "meta_synthesizer", "run_id": uuid4()},
        _Deps(llm),
    )
    assert "situation_signature" not in result.finding.data
    assert "citations" not in result.finding.data


# ---------------------------------------------------------------------------
# derive_signature reads the nested composition signature
# ---------------------------------------------------------------------------


def test_derive_signature_reads_nested_composition_signature():
    """The persisted analyst_outputs.data column is the full payload model_dump,
    so a composition's stamped ``data['situation_signature']`` lands at
    data->'data'->'situation_signature'. derive_signature must read it there."""
    finding = FindingPayload(
        title="US composition",
        body="body",
        data={
            "meta": True,
            "citations": [],
            "situation_signature": "composition:country_composition:country_g20_us",
        },
    )
    dump = finding.model_dump(mode="python")
    # The signature is NOT at the top level (FindingPayload is extra='forbid').
    assert "situation_signature" not in dump
    sig = finding_supersession.derive_signature(dump)
    assert sig == "sit:composition:country_composition:country_g20_us"


def test_derive_signature_still_none_for_contentless_meta():
    """A meta finding with NO stamped signature and no entities still yields None
    (the pre-fix behavior for a legacy global-meta finding)."""
    dump = FindingPayload(
        title="legacy meta", body="b",
        data={"meta": True, "contributing_analysts": ["a"]},
    ).model_dump(mode="python")
    assert finding_supersession.derive_signature(dump) is None


# ---------------------------------------------------------------------------
# End-to-end synthetic clustering — one canonical head per (analyst_id, target)
# ---------------------------------------------------------------------------
#
# test_composition_heads_fold_to_one_canonical_per_analyst_target (+ its
# _sup_row helper) was REMOVED 2026-07-23 (TEST_DEBT_RECON.md Bucket H). It
# drove country_composition/world_assessor findings through
# det_run_method(..., {"sub_handler": "finding_supersession"}, None) and
# asserted clustered_count == 3 — but finding_supersession._cluster()
# unconditionally skips every row whose analyst_id is in
# _COMPOSITION_ANALYST_IDS (which includes BOTH analyst_ids this test used),
# so clustered_count/superseded_count were structurally guaranteed 0, not a
# flaky edge case. The exclusion and this test were added in the SAME commit
# (4204d7e, S8-T3); the exclusion's own docstring says composition-head
# folding was moved to a DIFFERENT function, fold_prior_composition_heads
# (added FU6, commit 9bda4db), invoked from the composition WRITE path — not
# from det_run_method/_cluster. That function already has its own dedicated,
# passing coverage in test_correlation_head_fold.py and test_dq_followups.py
# (both directly import + call it). `git log -S_COMPOSITION_ANALYST_IDS`
# turned up no incident where the exclusion silently regressed (only the
# introducing commit + one unrelated hygiene touch), so per this recon's own
# stated criterion ("recommend (a) [delete] unless a prior incident is
# found") this E2E path was dead weight testing a mechanism the code
# deliberately routes around, superseded by the more precise sibling
# coverage. If a prior incident surfaces later, recovering this test from
# git history (option (b): repurpose to assert clustered_count == 0, proving
# the exclusion itself) is straightforward.


# ---------------------------------------------------------------------------
# Live pivot-DB (env-gated) — 0058 migration fold + idempotency
# ---------------------------------------------------------------------------


_PIVOT_DB = {
    "host": os.environ.get("LEGBA_PIVOT_PG_HOST", "127.0.0.1"),
    "port": int(os.environ.get("LEGBA_PIVOT_PG_PORT", "5432")),
    "user": os.environ.get("LEGBA_PIVOT_PG_USER", "legba"),
    "password": os.environ.get("LEGBA_PIVOT_PG_PASSWORD", "legba"),
    "database": os.environ.get("LEGBA_PIVOT_PG_DB", "legba_pivot_test"),
}

_MIGRATION_SQL = (MIGRATIONS_DIR / "0058_composition_supersession_fold.sql").read_text(
    encoding="utf-8"
)


@pytest.fixture
async def pivot_pool():
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool(min_size=1, max_size=4, **_PIVOT_DB)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"legba_pivot_test unreachable: {exc}")
    async with pool.acquire() as conn:
        ok = await conn.fetchval("SELECT to_regclass('finding_supersessions')")
        has_col = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='analyst_outputs' AND column_name='superseded_by'"
        )
    if not ok or not has_col:
        await pool.close()
        pytest.skip("supersession substrate not present")
    yield pool
    await pool.close()


async def _insert_composition(conn, *, fid, analyst_id, target_id, produced_at):
    """Insert a composition-shaped analyst_outputs row (meta=true + citations)."""
    data = {
        "kind_marker": "finding",
        "title": "composition",
        "body": "b",
        "data": {"meta": True, "citations": [], "contributing_analysts": []},
    }
    await conn.execute(
        """INSERT INTO analyst_outputs
               (id, kind, title, body, confidence, data, analyst_id, target_id,
                produced_at, schema_uri)
           VALUES ($1,'finding','composition','',0.6,$2::jsonb,$3,$4,$5,
                   'iglu:legba/finding/jsonschema/1-0-0')""",
        fid, json.dumps(data), analyst_id, target_id, produced_at,
    )


async def test_live_migration_0058_folds_and_is_idempotent(pivot_pool):
    """The 0058 migration folds each (analyst_id, target_id) composition cluster to
    one canonical head; a re-run stamps/supersedes/links NOTHING new. Runs inside a
    rolled-back transaction so it never persists to the dev DB."""
    tag = f"suptest_{uuid4().hex[:8]}"
    us_a = f"{tag}_country_composition"
    world_a = f"{tag}_world_assessor"
    t0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)

    us_ids = [uuid4() for _ in range(3)]
    in_ids = [uuid4() for _ in range(2)]
    world_ids = [uuid4() for _ in range(2)]

    async with pivot_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            for h, fid in enumerate(us_ids):
                await _insert_composition(conn, fid=fid, analyst_id=us_a,
                                          target_id="country_g20_us",
                                          produced_at=t0 + timedelta(hours=h))
            for h, fid in enumerate(in_ids):
                await _insert_composition(conn, fid=fid, analyst_id=us_a,
                                          target_id="country_g20_in",
                                          produced_at=t0 + timedelta(hours=h))
            for h, fid in enumerate(world_ids):
                await _insert_composition(conn, fid=fid, analyst_id=world_a,
                                          target_id=None,
                                          produced_at=t0 + timedelta(hours=h))

            await conn.execute(_MIGRATION_SQL)

            async def _heads(analyst_id, target_id):
                if target_id is None:
                    return await conn.fetchval(
                        "SELECT count(*) FROM analyst_outputs WHERE analyst_id=$1 "
                        "AND target_id IS NULL AND superseded_by IS NULL", analyst_id)
                return await conn.fetchval(
                    "SELECT count(*) FROM analyst_outputs WHERE analyst_id=$1 "
                    "AND target_id=$2 AND superseded_by IS NULL", analyst_id, target_id)

            # Exactly ONE canonical head per (analyst_id, target_id) cluster.
            assert await _heads(us_a, "country_g20_us") == 1
            assert await _heads(us_a, "country_g20_in") == 1
            assert await _heads(world_a, None) == 1

            # Newest row is the surviving head; all rows preserved (append-only).
            head_us = await conn.fetchval(
                "SELECT id FROM analyst_outputs WHERE analyst_id=$1 "
                "AND target_id='country_g20_us' AND superseded_by IS NULL", us_a)
            assert head_us == us_ids[-1]
            assert await conn.fetchval(
                "SELECT count(*) FROM analyst_outputs WHERE analyst_id=$1", us_a) == 5

            # Signature stamped on every US cluster member.
            sig_us = "sit:composition:%s:country_g20_us" % us_a
            assert await conn.fetchval(
                "SELECT count(*) FROM analyst_outputs WHERE analyst_id=$1 "
                "AND target_id='country_g20_us' AND situation_signature=$2",
                us_a, sig_us) == 3

            # Link rows mirror the fold: 2 (US) + 1 (India) + 1 (world) = 4.
            all_ids = us_ids + in_ids + world_ids
            links_1 = await conn.fetchval(
                "SELECT count(*) FROM finding_supersessions "
                "WHERE superseding_finding_id = ANY($1::uuid[])", all_ids)
            assert links_1 == 4

            superseded_1 = await conn.fetchval(
                "SELECT count(*) FROM analyst_outputs "
                "WHERE id = ANY($1::uuid[]) AND superseded_by IS NOT NULL", all_ids)
            assert superseded_1 == 4

            # RE-RUN: pure no-op (same head count, same link count, same superseded).
            await conn.execute(_MIGRATION_SQL)
            assert await _heads(us_a, "country_g20_us") == 1
            assert await _heads(us_a, "country_g20_in") == 1
            assert await _heads(world_a, None) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM finding_supersessions "
                "WHERE superseding_finding_id = ANY($1::uuid[])", all_ids) == links_1
            assert await conn.fetchval(
                "SELECT count(*) FROM analyst_outputs "
                "WHERE id = ANY($1::uuid[]) AND superseded_by IS NOT NULL",
                all_ids) == superseded_1
        finally:
            await tx.rollback()
