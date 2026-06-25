# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-172 meta_findings_synthesizer analyst-kind tests.

Covers:

  * Unit — orient/render/coerce shape: contributing_analysts derived in
    first-seen order, derived_from is the row id list, prompt rendering
    folds in analyst attribution + body excerpt + evidence.

  * Test-double LLM boundary — a canned-JSON ``chat_complete`` returns a
    valid synth, and ``run_method`` emits a :class:`FindingPayload` with
    ``data.meta = True``, ``data.contributing_analysts`` covering ALL
    source analysts, and ``AnalystMethodResult.derived_from`` populated
    with every contributing finding's UUID.

  * Empty-input guard — ``run_method`` short-circuits to a low-confidence
    diagnostic meta-finding (matching the sibling kinds' contract) rather
    than raising or calling the LLM.

  * Substrate-read helper — :func:`read_other_analyst_findings` filters
    to ``kind = 'finding'`` AND ``analyst_id IN <set>`` AND excludes
    rows already marked ``data.meta = true`` (so the synthesizer doesn't
    recurse on its own output).

Test doubles are scoped to the LLM boundary only, per the L-172 brief
(no mocks for substrate boundaries — that part runs against the same
migrated_pg fixture used by the rest of the legba.data suite).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import asyncpg

from legba.data.analysts.meta_findings_synthesizer import (
    DEFAULT_MAX_TOKENS,
    KIND_NAME,
    MAX_INPUT_FINDINGS,
    MetaFindingsSynthesizerRunner,
    _coerce_finding,
    _orient,
    _render_user_prompt,
    build_prompt_module,
    read_other_analyst_findings,
    run_method,
)
from legba.data.config import PostgresConfig
from legba.data.provenance import (
    AnalystContext,
    FindingPayload,
    TargetContext,
    write_finding,
)


# ---------------------------------------------------------------------------
# Constants + test doubles
# ---------------------------------------------------------------------------


KIND_NAME_EXPECTED: str = "meta_findings_synthesizer"


class _CapturedCallLLM:
    """Test double for the LLM boundary.

    Captures the last call's messages + system + max_tokens so tests can
    assert what the synth prompt actually looked like, and returns a
    canned JSON synthesis. ``subprovider`` is exposed because the kind's
    intermediate_steps trace records it.
    """

    subprovider = "test_double"

    def __init__(
        self,
        canned: dict[str, Any] | None = None,
        *,
        prompt_tokens: int = 311,
        completion_tokens: int = 122,
    ) -> None:
        self._canned = canned or {
            "title": "Synth: convergent claim across 3 sources",
            "body": (
                "Three analysts converge on the same underlying situation: "
                "analyst.alpha emphasizes supply-side dynamics, analyst.beta "
                "highlights demand-side movement, analyst.gamma surfaces a "
                "regulatory inflection that explains both."
            ),
            "confidence": 0.78,
            "evidence": [
                "alpha cites May 18 production data",
                "beta cites May 19 consumption series",
                "gamma cites the May 20 regulator press release",
            ],
            "tags": ["synth", "convergence"],
        }
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        # Capture state for assertions
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({
            "messages": list(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
        })

        canned = self._canned
        pt = self._prompt_tokens
        ct = self._completion_tokens

        class _Usage:
            prompt_tokens = pt
            completion_tokens = ct
            reasoning_tokens = 0

        class _Response:
            content = json.dumps(canned)
            usage = _Usage()

        return _Response()


class _RaisingLLM:
    """LLM stub that raises — used to assert the runner propagates errors."""

    subprovider = "raising"

    async def chat_complete(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("synthetic LLM failure")


# ---------------------------------------------------------------------------
# Synthetic finding-row builder
# ---------------------------------------------------------------------------


def _finding_row(
    *,
    analyst_id: str,
    title: str,
    body: str = "",
    confidence: float = 0.7,
    evidence: list[str] | None = None,
    produced_at: datetime | None = None,
    row_id: UUID | None = None,
) -> dict[str, Any]:
    """Build a row dict shaped like a result of ``read_other_analyst_findings``.

    Mirrors the column projection of ``analyst_outputs`` so unit tests can
    exercise the orient/render/coerce path without a DB hit.
    """
    return {
        "id": row_id or uuid4(),
        "kind": "finding",
        "title": title,
        "body": body,
        "confidence": confidence,
        "severity": None,
        "data": {"evidence": list(evidence or [])},
        "evidence": list(evidence or []),
        "target_id": None,
        "target_version": None,
        "analyst_id": analyst_id,
        "analyst_version": "v" + uuid4().hex[:8],
        "produced_at": produced_at
        or datetime.now(tz=timezone.utc) - timedelta(minutes=5),
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }


# ---------------------------------------------------------------------------
# Unit tests — kind identity + prompt module
# ---------------------------------------------------------------------------


def test_kind_name_matches_taxonomy():
    """KIND_NAME must exactly match the topology v2 §5.3 taxonomy + the
    analyst-kind enum value. Drift here breaks the host's kind-dispatch."""
    assert KIND_NAME == KIND_NAME_EXPECTED


def test_build_prompt_module_returns_dspy_module():
    """Wave B prereq #4 (2026-05-21): build_prompt_module now returns a
    real dspy.Module instance instead of the placeholder dict descriptor.

    PROMPT_MODULE_PATH was also migrated from the dotted analysts path
    to the canonical ``legba.prompts.<kind>.v1`` location.
    """
    pytest.importorskip("dspy")
    from legba.prompts.meta_findings_synthesizer.v1 import MetaFindingsSynthesizerCycle
    from legba.data.analysts.meta_findings_synthesizer import PROMPT_MODULE_PATH
    assert PROMPT_MODULE_PATH == "legba.prompts.meta_findings_synthesizer.v1"
    mod = build_prompt_module()
    assert isinstance(mod, MetaFindingsSynthesizerCycle)


def test_default_token_budget_narrower_than_inline_target():
    """Documented invariant: meta-synth context is narrower than the LLM
    kinds that read raw substrate. ``inline_target`` defaults to 1024,
    ``cross_target_raw`` to 1536; meta-synth runs *under* both."""
    from legba.data.analysts.inline_target import InlineTargetDeps

    inline_default = InlineTargetDeps.__dataclass_fields__["max_tokens"].default
    assert DEFAULT_MAX_TOKENS < inline_default, (
        f"meta synth max_tokens={DEFAULT_MAX_TOKENS} must be < "
        f"inline_target's {inline_default} (kind is narrower-context)"
    )


# ---------------------------------------------------------------------------
# Unit tests — _orient
# ---------------------------------------------------------------------------


def test_orient_dedups_analysts_in_first_seen_order():
    """Same analyst_id appearing in multiple rows is collapsed into one
    entry in contributing_analysts, ordered by first appearance after
    the newest-first sort."""
    now = datetime.now(tz=timezone.utc)
    rows = [
        _finding_row(
            analyst_id="analyst.beta",
            title="beta first (newest)",
            produced_at=now - timedelta(minutes=1),
        ),
        _finding_row(
            analyst_id="analyst.alpha",
            title="alpha",
            produced_at=now - timedelta(minutes=2),
        ),
        _finding_row(
            analyst_id="analyst.beta",
            title="beta second",
            produced_at=now - timedelta(minutes=3),
        ),
    ]
    sliced, derived_from, contributing = _orient(rows)
    assert len(sliced) == 3
    # All three row UUIDs are in derived_from.
    assert len(derived_from) == 3
    assert {r["id"] for r in rows} == set(derived_from)
    # Contributing is unique + first-seen order.
    assert contributing == ["analyst.beta", "analyst.alpha"]


def test_orient_caps_to_max_input_findings():
    """More than MAX_INPUT_FINDINGS rows → oldest are dropped silently."""
    now = datetime.now(tz=timezone.utc)
    rows = [
        _finding_row(
            analyst_id=f"analyst.a{i}",
            title=f"finding {i}",
            produced_at=now - timedelta(minutes=i),  # i=0 newest
        )
        for i in range(MAX_INPUT_FINDINGS + 5)
    ]
    sliced, derived_from, contributing = _orient(rows)
    assert len(sliced) == MAX_INPUT_FINDINGS
    assert len(derived_from) == MAX_INPUT_FINDINGS
    assert len(contributing) == MAX_INPUT_FINDINGS
    # Newest-first; oldest are the ones dropped.
    assert sliced[0]["title"] == "finding 0"


def test_orient_skips_malformed_ids():
    """A row with a non-UUID id contributes to the prompt but not to
    derived_from — the lineage walker tolerates partial lists."""
    rows = [
        _finding_row(analyst_id="analyst.ok", title="ok"),
        {
            **_finding_row(analyst_id="analyst.bad", title="bad-id"),
            "id": "not-a-uuid",
        },
    ]
    sliced, derived_from, contributing = _orient(rows)
    assert len(sliced) == 2
    assert len(derived_from) == 1
    assert "analyst.ok" in contributing
    assert "analyst.bad" in contributing


# ---------------------------------------------------------------------------
# Unit tests — _render_user_prompt
# ---------------------------------------------------------------------------


def test_render_prompt_contains_attribution_for_every_kept_row():
    rows = [
        _finding_row(
            analyst_id=f"analyst.{name}",
            title=f"{name} finding title",
            body=f"{name} body excerpt",
            evidence=[f"{name}-ev-1", f"{name}-ev-2"],
        )
        for name in ("alpha", "beta", "gamma")
    ]
    sliced, _, contributing = _orient(rows)
    rendered = _render_user_prompt(sliced, contributing)
    # Every analyst is attributed in the rendered prompt.
    for a in contributing:
        assert a in rendered, f"expected {a} attribution in rendered prompt"
    # Every body excerpt landed.
    for name in ("alpha", "beta", "gamma"):
        assert f"{name} body excerpt" in rendered
    # First evidence bullet landed.
    assert "alpha-ev-1" in rendered


# ---------------------------------------------------------------------------
# Unit tests — _coerce_finding
# ---------------------------------------------------------------------------


def test_coerce_finding_stamps_meta_marker_and_contributing_analysts():
    raw = json.dumps(
        {
            "title": "synth",
            "body": "synth body",
            "confidence": 0.6,
            "evidence": ["x"],
            "tags": ["t1"],
        }
    )
    f = _coerce_finding(
        raw,
        fallback_title="fallback",
        contributing_analysts=["analyst.a", "analyst.b"],
    )
    assert isinstance(f, FindingPayload)
    assert f.title == "synth"
    assert f.data.get("meta") is True
    assert f.data.get("contributing_analysts") == ["analyst.a", "analyst.b"]
    # `meta` tag is always present, idempotently.
    assert "meta" in f.tags


def test_coerce_finding_malformed_json_falls_back_with_meta_marker():
    f = _coerce_finding(
        "not-json-at-all",
        fallback_title="fb-title",
        contributing_analysts=["analyst.x"],
    )
    assert f.title == "fb-title"
    assert "unstructured" in f.tags
    assert "meta" in f.tags
    # Still stamped with the meta marker.
    assert f.data.get("meta") is True
    assert f.data.get("contributing_analysts") == ["analyst.x"]


# ---------------------------------------------------------------------------
# run_method — primary acceptance test per the brief:
#   "Synthetic findings from 2-3 analysts → assert the meta-synth correctly
#    cites all of them in derived_from"
# ---------------------------------------------------------------------------


class _DepsBundle:
    """Minimal MetaFindingsDeps-compatible object."""

    def __init__(self, llm: Any) -> None:
        self.llm = llm


@pytest.mark.asyncio
async def test_run_method_cites_all_contributing_analysts_in_derived_from():
    """Three first-order findings from three distinct analysts → the
    meta-synth's ``derived_from`` carries all three finding-UUIDs and
    its payload's ``contributing_analysts`` carries all three analyst_ids.
    This is the L-172 brief's core acceptance criterion."""
    rows = [
        _finding_row(analyst_id="analyst.alpha", title="alpha first-order finding"),
        _finding_row(analyst_id="analyst.beta", title="beta first-order finding"),
        _finding_row(analyst_id="analyst.gamma", title="gamma first-order finding"),
    ]
    expected_ids = {r["id"] for r in rows}

    llm = _CapturedCallLLM()
    deps = _DepsBundle(llm=llm)

    result = await run_method(
        list(rows),
        {
            "analyst_id": "analyst.meta_synth_01",
            "run_id": uuid4(),
        },
        deps,
    )

    # Lineage: every input finding's UUID is in derived_from.
    assert set(result.derived_from) == expected_ids, (
        f"meta-synth derived_from {result.derived_from} did not cover all "
        f"contributing findings {expected_ids}"
    )

    # Contributing analysts: all three are stamped in the payload's data
    # (operator-facing) and at least one as a content-tag.
    contributing = result.finding.data.get("contributing_analysts")
    assert isinstance(contributing, list)
    assert set(contributing) == {"analyst.alpha", "analyst.beta", "analyst.gamma"}, (
        f"contributing_analysts {contributing} did not cover all three sources"
    )
    assert result.finding.data.get("meta") is True
    assert "meta" in result.finding.tags

    # Token usage flows through to budget recording.
    assert result.usage["prompt_tokens"] > 0
    assert result.usage["completion_tokens"] > 0

    # LLM was called exactly once with the narrower max_tokens default.
    assert len(llm.calls) == 1
    assert llm.calls[0]["max_tokens"] == DEFAULT_MAX_TOKENS


@pytest.mark.asyncio
async def test_run_method_with_two_analysts_still_cites_both():
    """Brief specifies 2-3 analysts; the 2-analyst case must also work."""
    rows = [
        _finding_row(analyst_id="analyst.east", title="east finding"),
        _finding_row(analyst_id="analyst.west", title="west finding"),
    ]
    expected_ids = {r["id"] for r in rows}

    llm = _CapturedCallLLM()
    deps = _DepsBundle(llm=llm)

    result = await run_method(rows, {"analyst_id": "synth"}, deps)
    assert set(result.derived_from) == expected_ids
    contributing = result.finding.data["contributing_analysts"]
    assert set(contributing) == {"analyst.east", "analyst.west"}


@pytest.mark.asyncio
async def test_run_method_honors_source_analyst_ids_ordering():
    """When options['source_analyst_ids'] is supplied, its order seeds
    contributing_analysts (subscription resolution is the source of truth
    on intent); any analyst that *also* shows up in inputs but wasn't
    listed in options gets appended at the end."""
    rows = [
        _finding_row(analyst_id="analyst.late_riser", title="extra"),
        _finding_row(analyst_id="analyst.b", title="b"),
        _finding_row(analyst_id="analyst.a", title="a"),
    ]
    llm = _CapturedCallLLM()
    deps = _DepsBundle(llm=llm)

    result = await run_method(
        rows,
        {"source_analyst_ids": ["analyst.a", "analyst.b"]},
        deps,
    )
    contributing = result.finding.data["contributing_analysts"]
    # Provided ordering is preserved AND analyst.late_riser is appended.
    assert contributing == ["analyst.a", "analyst.b", "analyst.late_riser"]


@pytest.mark.asyncio
async def test_run_method_empty_input_no_llm_call():
    """Empty input short-circuits before the LLM call, returning a
    low-confidence meta-finding tagged ``empty_slice``. Matches the
    sibling kinds' contract."""
    llm = _CapturedCallLLM()
    deps = _DepsBundle(llm=llm)

    result = await run_method([], {"analyst_id": "synth"}, deps)

    assert llm.calls == []  # LLM not called
    assert result.derived_from == []
    assert result.finding.confidence == 0.0
    assert "empty_slice" in result.finding.tags
    assert "meta" in result.finding.tags
    assert result.finding.data.get("meta") is True


@pytest.mark.asyncio
async def test_run_method_propagates_llm_errors():
    """LLM failure surfaces to the caller so the actor's failure
    classifier can route it (transient / hard-fail / budget). The kind
    does not swallow."""
    rows = [_finding_row(analyst_id="a", title="x")]
    deps = _DepsBundle(llm=_RaisingLLM())
    with pytest.raises(RuntimeError, match="synthetic LLM failure"):
        await run_method(rows, {}, deps)


# ---------------------------------------------------------------------------
# Runner adapter — same shape as InlineTargetRunner / CrossTargetRawRunner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_callable_two_arg_signature():
    """The Runner wrapper exposes the ``(inputs, options) -> result`` shape
    the dapr-actor layer dispatches against, with the runner-level
    max_tokens override actually flowing into the LLM call."""
    rows = [_finding_row(analyst_id="a", title="x")]
    llm = _CapturedCallLLM()
    runner = MetaFindingsSynthesizerRunner(llm, max_tokens=256)
    result = await runner(rows, {})
    assert len(llm.calls) == 1
    assert llm.calls[0]["max_tokens"] == 256
    assert result.finding.data.get("meta") is True


# ---------------------------------------------------------------------------
# Integration — substrate-read helper
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


def _target_ctx() -> TargetContext:
    return TargetContext(
        target_id="meta_synth_test_target",
        target_version="abc123def456",
    )


def _analyst_ctx_for(analyst_id: str) -> AnalystContext:
    return AnalystContext(
        analyst_id=analyst_id,
        analyst_version="v" + uuid4().hex[:8],
        run_id=uuid4(),
        target_id="meta_synth_test_target",
        target_version="abc123def456",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_other_analyst_findings_filters_to_kind_finding_and_analyst_set(
    pg_conn,
):
    """Seed the DB with three analysts' findings + one meta-finding-shaped
    row, then read with a 2-analyst subscription. Only those two analysts'
    first-order findings come back.

    Pivot migration: signals are now target-agnostic (migration
    the source-first pivot dropped target_id from the signals table), so this
    test no longer seeds a target-owned signal via write_target_signal. The
    lineage root is a synthetic UUID — analyst_outputs.derived_from is a
    plain UUID[] with no FK, so the findings still carry a valid lineage
    edge and this test exercises read_other_analyst_findings unchanged.
    """
    # Synthetic lineage root (no FK on derived_from post-pivot).
    sig_id = uuid4()

    # Three analysts each emit a finding.
    f_alpha, _ = await write_finding(
        pg_conn,
        analyst_ctx=_analyst_ctx_for("analyst.alpha_meta_test"),
        payload=FindingPayload(title="alpha finding", body="alpha body"),
        derived_from=[sig_id],
    )
    f_beta, _ = await write_finding(
        pg_conn,
        analyst_ctx=_analyst_ctx_for("analyst.beta_meta_test"),
        payload=FindingPayload(title="beta finding", body="beta body"),
        derived_from=[sig_id],
    )
    f_gamma_excluded, _ = await write_finding(
        pg_conn,
        analyst_ctx=_analyst_ctx_for("analyst.gamma_meta_test"),
        payload=FindingPayload(title="gamma finding (not subscribed)"),
        derived_from=[sig_id],
    )

    # One *already-meta* finding from alpha — must be excluded so the synth
    # doesn't recurse on its own output.
    f_alpha_meta, _ = await write_finding(
        pg_conn,
        analyst_ctx=_analyst_ctx_for("analyst.alpha_meta_test"),
        payload=FindingPayload(
            title="alpha META finding (should be excluded)",
            body="meta body",
            data={"meta": True, "contributing_analysts": ["analyst.x"]},
        ),
        derived_from=[f_alpha.id],
    )

    rows = await read_other_analyst_findings(
        pg_conn,
        analyst_ids=["analyst.alpha_meta_test", "analyst.beta_meta_test"],
        time_window_hours=24,
        limit=50,
    )
    ids = {r["id"] for r in rows}

    assert f_alpha.id in ids
    assert f_beta.id in ids
    # gamma was not in the subscription list.
    assert f_gamma_excluded.id not in ids
    # The pre-existing meta-finding is excluded by the meta filter.
    assert f_alpha_meta.id not in ids

    # All returned rows have kind='finding' and an expected analyst_id.
    for r in rows:
        assert r["kind"] == "finding"
        assert r["analyst_id"] in {
            "analyst.alpha_meta_test",
            "analyst.beta_meta_test",
        }


@pytest.mark.asyncio
async def test_read_other_analyst_findings_empty_set_returns_empty(pg_conn=None):
    """Empty analyst_ids short-circuits — no DB scan."""
    # Pass a sentinel that would explode if the helper *did* try to use it.
    class _ExplodingConn:
        async def fetch(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("should not have queried the DB")

    rows = await read_other_analyst_findings(
        _ExplodingConn(),  # type: ignore[arg-type]
        analyst_ids=[],
        time_window_hours=24,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Integration — full round-trip: synth result -> write_analyst_output ->
# DB row carries derived_from = contributing finding UUIDs.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_synth_result_round_trips_through_write_analyst_output(pg_conn):
    """End-to-end sanity: seed two source findings, call run_method, then
    feed the result through write_finding using
    ``result.derived_from`` — the row's ``derived_from`` column must
    contain the two source finding UUIDs.

    This mirrors what the dapr-actor host does (see
    ``dapr_actors.AnalystActor.run`` around line 700) for the meta-synth
    kind. We don't mock the write path.

    Pivot migration: signals are target-agnostic post-pivot (migration
    the source-first pivot dropped target_id from the signals table), so the
    first-order findings' lineage root is a synthetic UUID rather than a
    target-owned signal written via write_target_signal. The write path
    under test (write_finding -> analyst_outputs) is unaffected.
    """
    sig_id = uuid4()
    f1, _ = await write_finding(
        pg_conn,
        analyst_ctx=_analyst_ctx_for("analyst.src_one"),
        payload=FindingPayload(title="src one", body="body one"),
        derived_from=[sig_id],
    )
    f2, _ = await write_finding(
        pg_conn,
        analyst_ctx=_analyst_ctx_for("analyst.src_two"),
        payload=FindingPayload(title="src two", body="body two"),
        derived_from=[sig_id],
    )

    # Read with the kind's own helper so the row shape matches what the
    # runtime would actually pass into run_method.
    inputs = await read_other_analyst_findings(
        pg_conn,
        analyst_ids=["analyst.src_one", "analyst.src_two"],
        time_window_hours=24,
    )
    assert {r["id"] for r in inputs} == {f1.id, f2.id}

    deps = _DepsBundle(llm=_CapturedCallLLM())
    result = await run_method(inputs, {"analyst_id": "analyst.synth_01"}, deps)
    assert set(result.derived_from) == {f1.id, f2.id}

    # Write the synth result back to substrate exactly as the actor would.
    synth_ctx = _analyst_ctx_for("analyst.synth_01")
    out_row, dlq = await write_finding(
        pg_conn,
        analyst_ctx=synth_ctx,
        payload=result.finding,
        derived_from=result.derived_from,
    )
    assert dlq is None
    assert out_row is not None
    fetched = await pg_conn.fetchrow(
        "SELECT kind, data, derived_from FROM analyst_outputs WHERE id = $1",
        out_row.id,
    )
    assert fetched["kind"] == "finding"
    assert set(fetched["derived_from"]) == {f1.id, f2.id}
    # The `data` column stores the entire payload model_dump (per
    # ``writes._insert_analyst_output``), so the payload's own ``data``
    # field is nested under the column's ``data`` key.
    column_data = (
        fetched["data"] if isinstance(fetched["data"], dict)
        else json.loads(fetched["data"])
    )
    payload_data = column_data.get("data") or {}
    assert payload_data.get("meta") is True
    assert set(payload_data.get("contributing_analysts") or []) == {
        "analyst.src_one",
        "analyst.src_two",
    }
