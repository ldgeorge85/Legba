# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CONTINUITY Phase 1 — temporal continuity for compositions + the world read.

The units are deliberately stateless slice-of-now analyzers, and so was every
composition above them. Phase 1 gives the country / region / thematic
compositions and the WORLD read a memory — as CITABLE REFS only, no new kind, no
schema:

  * PRIOR READ — the SAME target's previous non-superseded, VERIFIED head from
    THIS composition analyst, one ref, carrying its own ``produced_at`` + age;
  * OPEN-SITUATION REGISTER — a bounded, target-scoped register of the open
    ``situations`` frames, rendered as ONE referenceable evidence block.

The two hard lessons the design is bound by, and what asserts them here:

  * the world_context RAG ROLLBACK (an UNCITED prior leaking into cited analysis
    is this platform's named failure mode) ⇒ both refs enter the SAME flat
    ``[[ref:N]]`` ordinal space as the basis + periphery blocks, carry
    ``evidence_text``, and are resolved into ``data['citations']`` like any other
    evidence — asserted end-to-end below, including that the register NEVER
    fabricates a ``ref_id`` it does not have;
  * TEMPORAL COLLAPSE ⇒ the rendered blocks carry their OWN dates (the prior
    read's ``produced_at``/age, each situation's ``last_event_at``/age) and the
    prompt clause anchors "when" on them, never on run time.

Also covered: byte-compatible absence (a first run renders NO section, stamps NO
envelope, and leaves ``derived_from`` untouched), the register's scope + bound,
the receipts, and that the VERIFY path needs no change to consume either ref.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.provenance import verify
from legba.data.registry import scorecard_reconcile


# ---------------------------------------------------------------------------
# helpers (mirroring test_meta_findings_composition.py / _tiered_evidence)
# ---------------------------------------------------------------------------


# The one token unique to the prior-read SQL — the verify LATERAL sub-select
# inside the ordinary evidence gather also ends in ``LIMIT 1``, so routing on
# that alone would mis-attribute the basis read.
_PRIOR_SQL_MARKER = "AS age_hours"


class _RoutingConn:
    """Fake asyncpg.Connection routing by SQL text.

    READ_SLICE now fires up to three query families on a composition branch —
    the evidence read(s) over ``analyst_outputs``, the CONTINUITY prior-read over
    ``analyst_outputs``, and the CONTINUITY register over ``situations`` — so a
    single canned row list can no longer stand in for all of them.
    """

    def __init__(
        self,
        *,
        slice_rows: list[dict[str, Any]] | None = None,
        prior_rows: list[dict[str, Any]] | None = None,
        situation_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._slice_rows = slice_rows or []
        self._prior_rows = prior_rows or []
        self._situation_rows = situation_rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if "FROM situations" in query:
            return [dict(r) for r in self._situation_rows]
        if _PRIOR_SQL_MARKER in query:
            return [dict(r) for r in self._prior_rows]
        return [dict(r) for r in self._slice_rows]

    def query_of(self, needle: str) -> tuple[str, tuple[Any, ...]]:
        """The LAST captured call whose SQL contains ``needle`` (asserts one)."""
        for call in reversed(self.calls):
            if needle in call[0]:
                return call
        raise AssertionError(f"no captured query contains {needle!r}")

    def has_query(self, needle: str) -> bool:
        return any(needle in q for q, _ in self.calls)


def _descriptor(
    others: list[tuple[str, str]],
    *,
    identity_id: str | None = "country_composition",
    substrate: dict[str, Any] | None = None,
    declares_verify: bool = False,
) -> SimpleNamespace:
    entries = [SimpleNamespace(id=i, time_window=w, data_types=[]) for i, w in others]
    desc = SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=entries,
            targets=SimpleNamespace(predicate='has_tag("g20")'),
            substrate=substrate or {},
        ),
        method=SimpleNamespace(llm={"verify": {}} if declares_verify else {}),
    )
    if identity_id is not None:
        desc.identity = SimpleNamespace(id=identity_id)
    return desc


_UNITS = [("leadership_transition", "24h"), ("escalation", "24h")]


def _finding_row(
    *,
    analyst_id: str,
    uid: UUID | None = None,
    title: str = "sub-claim title",
    body: str = "sub-claim body",
    produced_at: str = "2026-07-30T00:00:00+00:00",
    target_id: str | None = "country_g20_in",
    effective_confidence: float | None = 0.7,
    derived_from: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": uid or uuid4(),
        "kind": "finding",
        "title": title,
        "body": body,
        "confidence": 0.7,
        "effective_confidence": effective_confidence,
        "faithfulness_score": 0.9,
        "severity": None,
        "data": {"tags": [], "evidence": []},
        "evidence": [],
        "target_id": target_id,
        "target_version": None,
        "analyst_id": analyst_id,
        "analyst_version": "vtest",
        "produced_at": produced_at,
        "derived_from": derived_from or [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }


def _prior_row(
    *,
    uid: UUID | None = None,
    analyst_id: str = "country_composition",
    title: str = "Prior read: leadership contest unresolved",
    body: str = "The units indicated a contested succession as of the prior sweep.",
    produced_at: str = "2026-07-29T06:00:00+00:00",
    age_hours: float | None = 30.0,
    derived_from: list[str] | None = None,
) -> dict[str, Any]:
    row = _finding_row(
        analyst_id=analyst_id,
        uid=uid,
        title=title,
        body=body,
        produced_at=produced_at,
        derived_from=derived_from,
    )
    row["age_hours"] = age_hours
    row[synth.CONTINUITY_ROW_KEY] = synth.CONTINUITY_PRIOR
    return row


def _situation(
    *,
    sid: UUID | None = None,
    name: str = "Succession crisis",
    status: str = "active",
    intensity: float = 0.82,
    events: int = 14,
    last_event_at: str = "2026-07-30T11:00:00+00:00",
    age_days: float = 12.5,
) -> dict[str, Any]:
    return {
        "situation_id": str(sid or uuid4()),
        "name": name,
        "status": status,
        "intensity_score": intensity,
        "event_count": events,
        "last_event_at": last_event_at,
        "opened_at": "2026-07-18T00:00:00+00:00",
        "age_days": age_days,
        "target_id": "country_g20_in",
    }


def _register_row(situations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        synth.CONTINUITY_ROW_KEY: synth.CONTINUITY_SITUATIONS,
        synth.CONTINUITY_SITUATIONS_ROW_KEY: situations,
    }


class _CannedLLM:
    subprovider = "continuity_test_double"

    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Any],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": list(messages), "system": system})
        import json as _json

        class _Usage:
            prompt_tokens = 10
            completion_tokens = 5
            reasoning_tokens = 0

        resp = SimpleNamespace()
        resp.content = _json.dumps(
            {
                "title": "Composed read",
                "body": self._body,
                "confidence": 0.6,
                "evidence": [],
                "tags": [],
            }
        )
        resp.usage = _Usage()
        return resp


def _user_prompt_of(llm: _CannedLLM) -> str:
    assert len(llm.calls) == 1
    for m in llm.calls[0]["messages"]:
        if (m.get("role") if isinstance(m, dict) else None) == "user":
            return str(m.get("content"))
    raise AssertionError("no user message captured")


def _step(result: Any, phase: str) -> dict[str, Any]:
    for s in result.intermediate_steps:
        if s.get("phase") == phase:
            return s
    raise AssertionError(f"no {phase!r} step: {result.intermediate_steps!r}")


# ---------------------------------------------------------------------------
# 1. The PRIOR-READ reader — admissibility, scope, honest absence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prior_read_query_is_verify_gated_and_head_pinned():
    """The prior read rides the SAME admissibility as the basis gather: the
    INNER verify join (verify must have RUN), the effective_confidence floor, the
    coerce-fallback drop — plus ``superseded_by IS NULL`` + newest-first, which is
    what makes "the live head at compose time" mean "the previous cycle's read"."""
    conn = _RoutingConn(prior_rows=[_prior_row()])
    row = await synth.read_prior_composition_head(
        conn, analyst_id="country_composition", target_id="country_g20_in",
        verify_floor=0.5,
    )
    query, params = conn.calls[0]
    assert "JOIN LATERAL" in query and "LEFT JOIN LATERAL" not in query
    assert "Faithfulness verify%" in query
    assert "LEAST(f.confidence, v.faithfulness_score) >= $3" in query
    assert "?| array['unstructured','coerce_failed']" in query
    assert "f.superseded_by IS NULL" in query
    assert "ORDER BY f.produced_at DESC, f.id DESC" in query
    assert "LIMIT 1" in query
    # Scope: this analyst, this target, the continuity look-back (NOT the slice
    # window — "the previous read" is a per-head fact, not a per-slice one).
    assert params[0] == "country_composition"
    assert params[1] == synth.CONTINUITY_PRIOR_LOOKBACK_HOURS
    assert params[2] == pytest.approx(0.5)
    assert params[3] == "country_g20_in"
    assert "f.target_id = $4" in query
    assert row is not None
    assert row[synth.CONTINUITY_ROW_KEY] == synth.CONTINUITY_PRIOR


@pytest.mark.asyncio
async def test_prior_read_target_less_lane_for_world_and_thematic():
    """The world/thematic heads are TARGET-LESS, so their "same target" is the
    target-less lane — never a stray desk's head."""
    conn = _RoutingConn(prior_rows=[_prior_row(analyst_id="world_assessor")])
    await synth.read_prior_composition_head(
        conn, analyst_id="world_assessor", target_id=None, verify_floor=0.0
    )
    query, params = conn.calls[0]
    assert "f.target_id IS NULL" in query
    assert "f.target_id = $4" not in query
    assert len(params) == 3


@pytest.mark.asyncio
async def test_prior_read_absent_returns_none_without_fabricating():
    """FIRST run (no admissible head) → ``None``: no ref, no block, no receipt."""
    conn = _RoutingConn(prior_rows=[])
    assert (
        await synth.read_prior_composition_head(
            conn, analyst_id="country_composition", target_id="country_g20_in",
            verify_floor=0.0,
        )
        is None
    )


@pytest.mark.asyncio
async def test_prior_read_drops_an_unciteable_row():
    """A prior head with no resolvable id has no drill target — it is DROPPED
    rather than rendered as an unciteable "previous read" (the uncited prior)."""
    bad = _prior_row()
    bad["id"] = "not-a-uuid"
    conn = _RoutingConn(prior_rows=[bad])
    assert (
        await synth.read_prior_composition_head(
            conn, analyst_id="country_composition", target_id="country_g20_in",
            verify_floor=0.0,
        )
        is None
    )


@pytest.mark.asyncio
async def test_prior_read_refuses_an_empty_analyst_id():
    conn = _RoutingConn(prior_rows=[_prior_row()])
    assert (
        await synth.read_prior_composition_head(
            conn, analyst_id="", target_id=None, verify_floor=0.0
        )
        is None
    )
    assert conn.calls == []


# ---------------------------------------------------------------------------
# 2. The OPEN-SITUATION register reader — open predicate, scope, bound
# ---------------------------------------------------------------------------


def _situation_db_row(
    *,
    sid: Any = None,
    name: str | None = "Succession crisis",
    intensity: float = 0.8,
) -> dict[str, Any]:
    return {
        "id": sid or uuid4(),
        "name": name,
        "status": "active",
        "category": "country_g20_in",
        "intensity_score": intensity,
        "event_count": 9,
        "last_event_at": "2026-07-30T11:00:00+00:00",
        "target_id": "country_g20_in",
        "opened_at": "2026-07-18T00:00:00+00:00",
        "age_days": 12.5,
    }


@pytest.mark.asyncio
async def test_situation_register_open_predicate_bound_and_projection():
    conn = _RoutingConn(situation_rows=[_situation_db_row()])
    out = await synth.read_open_situations(conn, target_id="country_g20_in")
    query, params = conn.calls[0]
    # OPEN = the same frame the thematic proposer reads: not superseded, still
    # valid, not closed. Worst-first (intensity, then recency) and BOUNDED.
    assert "s.superseded_by IS NULL" in query
    assert "(s.valid_until IS NULL OR s.valid_until > NOW())" in query
    assert "s.status <> 'closed'" in query
    assert "ORDER BY s.intensity_score DESC, s.last_event_at DESC NULLS LAST" in query
    assert f"LIMIT {synth.SITUATION_REGISTER_CAP}" in query
    assert "AND s.target_id = $1" in query
    assert params[0] == "country_g20_in"
    # The projection the register renders + cites.
    assert len(out) == 1
    entry = out[0]
    assert set(entry) >= {
        "situation_id", "name", "status", "intensity_score", "event_count",
        "last_event_at", "age_days",
    }
    assert entry["intensity_score"] == pytest.approx(0.8)
    assert entry["event_count"] == 9
    assert entry["last_event_at"] == "2026-07-30T11:00:00+00:00"


@pytest.mark.asyncio
async def test_situation_register_scopes_like_the_rest_of_the_slice():
    """A region reads the member-desk SET; a target-less world/thematic read is
    UNSCOPED — the same ``target_id``/``target_ids``/neither split the evidence
    gather takes, so the register never annotates a different aperture."""
    conn = _RoutingConn(situation_rows=[])
    await synth.read_open_situations(
        conn, target_ids=["country_g20_sa", "country_watch_ir"]
    )
    query, params = conn.calls[0]
    assert "AND s.target_id = ANY($1::TEXT[])" in query
    assert params[0] == ["country_g20_sa", "country_watch_ir"]

    conn = _RoutingConn(situation_rows=[])
    await synth.read_open_situations(conn)
    query, params = conn.calls[0]
    assert "AND s.target_id" not in query
    assert params == ()


@pytest.mark.asyncio
async def test_situation_register_empty_scope_reads_nothing_not_everything():
    """An EMPTY member set is HONORED (guarded on ``is not None``) — a region with
    no member desks reads ZERO frames, never the whole table."""
    conn = _RoutingConn(situation_rows=[])
    await synth.read_open_situations(conn, target_ids=[])
    query, params = conn.calls[0]
    assert "AND s.target_id = ANY($1::TEXT[])" in query
    assert params[0] == []


@pytest.mark.asyncio
async def test_situation_register_skips_rows_it_cannot_name():
    """The register may only name frames that actually exist: a row with no id or
    no name is skipped, never padded with a placeholder."""
    conn = _RoutingConn(
        situation_rows=[
            _situation_db_row(sid="not-a-uuid"),
            _situation_db_row(name=None),
            _situation_db_row(name="   "),
            _situation_db_row(name="Real frame"),
        ]
    )
    out = await synth.read_open_situations(conn)
    assert [s["name"] for s in out] == ["Real frame"]


# ---------------------------------------------------------------------------
# 3. READ_SLICE wiring — present when it exists, absent (byte-compatible) when not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_slice_appends_both_continuity_refs_per_country():
    conn = _RoutingConn(
        slice_rows=[_finding_row(analyst_id="leadership_transition")],
        prior_rows=[_prior_row()],
        situation_rows=[_situation_db_row()],
    )
    rows = await synth.READ_SLICE(
        conn, descriptor=_descriptor(_UNITS), target_filter="country_g20_in"
    )
    prior, register = synth._continuity_selection(rows)
    assert prior is not None
    assert register is not None
    assert len(synth._register_situations(register)) == 1
    # The prior-read query is scoped to THIS composition analyst (identity.id)
    # and THIS desk; the register to the same desk.
    _, prior_params = conn.query_of(_PRIOR_SQL_MARKER)
    assert prior_params[0] == "country_composition"
    assert prior_params[3] == "country_g20_in"
    _, sit_params = conn.query_of("FROM situations")
    assert sit_params[0] == "country_g20_in"


@pytest.mark.asyncio
async def test_read_slice_omits_continuity_cleanly_on_a_first_run():
    """No prior head + no open situations ⇒ NO continuity rows at all: the slice
    is exactly the evidence rows, byte-compatible with the pre-continuity read."""
    evidence = _finding_row(analyst_id="leadership_transition")
    conn = _RoutingConn(slice_rows=[evidence], prior_rows=[], situation_rows=[])
    rows = await synth.READ_SLICE(
        conn, descriptor=_descriptor(_UNITS), target_filter="country_g20_in"
    )
    assert [r.get(synth.CONTINUITY_ROW_KEY) for r in rows] == [None]
    assert synth._continuity_selection(rows) == (None, None)


@pytest.mark.asyncio
async def test_read_slice_without_an_identity_block_omits_the_prior_ref_only():
    """An unattributable "prior read" is exactly the uncited prior this design
    refuses — with no ``identity.id`` we cannot know WHOSE head it is, so the ref
    is omitted. The register (which needs no analyst attribution) still lands."""
    conn = _RoutingConn(
        slice_rows=[_finding_row(analyst_id="leadership_transition")],
        prior_rows=[_prior_row()],
        situation_rows=[_situation_db_row()],
    )
    rows = await synth.READ_SLICE(
        conn,
        descriptor=_descriptor(_UNITS, identity_id=None),
        target_filter="country_g20_in",
    )
    prior, register = synth._continuity_selection(rows)
    assert prior is None
    assert register is not None
    assert not conn.has_query(_PRIOR_SQL_MARKER)


@pytest.mark.asyncio
async def test_read_slice_legacy_global_meta_gets_no_continuity():
    """The LEGACY global meta (target-less, no verify) keeps the standing
    byte-for-byte discipline: ONE query, no continuity reads."""
    conn = _RoutingConn(slice_rows=[], prior_rows=[_prior_row()],
                        situation_rows=[_situation_db_row()])
    rows = await synth.READ_SLICE(
        conn, descriptor=_descriptor(_UNITS), target_filter=None
    )
    assert len(conn.calls) == 1
    assert not conn.has_query("FROM situations")
    assert synth._continuity_selection(rows) == (None, None)


@pytest.mark.asyncio
async def test_read_slice_empty_source_roster_fires_no_continuity_query():
    """No resolved source analysts ⇒ the compose emits an honest-empty head with
    no LLM call, so there is no prose for a memory to annotate. The "refuse the
    query rather than scan" contract extends to the continuity reads."""
    conn = _RoutingConn(prior_rows=[_prior_row()], situation_rows=[_situation_db_row()])
    rows = await synth.READ_SLICE(
        conn, descriptor=_descriptor([]), target_filter="country_g20_in"
    )
    assert rows == []
    assert conn.calls == []


@pytest.mark.asyncio
async def test_read_slice_world_branch_scopes_prior_target_less_and_register_global():
    conn = _RoutingConn(
        slice_rows=[], prior_rows=[_prior_row(analyst_id="world_assessor")],
        situation_rows=[_situation_db_row()],
    )
    await synth.READ_SLICE(
        conn,
        descriptor=_descriptor(
            [("region_composition", "24h")],
            identity_id="world_assessor",
            declares_verify=True,
        ),
        target_filter=None,
    )
    prior_q, prior_p = conn.query_of(_PRIOR_SQL_MARKER)
    assert "f.target_id IS NULL" in prior_q
    assert prior_p[0] == "world_assessor"
    # The world read's aperture is the whole roster, so its register is UNSCOPED.
    sit_q, sit_p = conn.query_of("FROM situations")
    assert "AND s.target_id" not in sit_q
    assert sit_p == ()


@pytest.mark.asyncio
async def test_read_slice_region_branch_splits_prior_frame_from_member_register():
    """The one branch where the two scopes DIVERGE, and correctly so: a region's
    prior read is the FRAME's own head (``target_id = region_<slug>``), while its
    register follows the MEMBER desks — the same scope its evidence is read over.
    """

    class _RegionConn(_RoutingConn):
        async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
            if "target_descriptors" in query:
                self.calls.append((query, params))
                return [
                    {"descriptor_id": "country_g20_sa"},
                    {"descriptor_id": "country_watch_ir"},
                ]
            return await super().fetch(query, *params)

    conn = _RegionConn(
        slice_rows=[],
        prior_rows=[_prior_row(analyst_id="region_composition")],
        situation_rows=[_situation_db_row()],
    )
    await synth.READ_SLICE(
        conn,
        descriptor=_descriptor(
            [("country_composition", "24h")], identity_id="region_composition"
        ),
        target_filter="region_mena",
    )
    prior_q, prior_p = conn.query_of(_PRIOR_SQL_MARKER)
    assert "f.target_id = $4" in prior_q
    assert prior_p[0] == "region_composition"
    assert prior_p[3] == "region_mena"
    _, sit_p = conn.query_of("FROM situations")
    assert sit_p[0] == ["country_g20_sa", "country_watch_ir"]


@pytest.mark.asyncio
async def test_read_slice_thematic_branch_uses_the_desk_allow_list():
    """A THEMATIC composition head is target-less, so its prior read is the
    target-less lane; its register follows the dyad desk allow-list."""
    conn = _RoutingConn(
        slice_rows=[],
        prior_rows=[_prior_row(analyst_id="escalation_composition")],
        situation_rows=[_situation_db_row()],
    )
    await synth.READ_SLICE(
        conn,
        descriptor=_descriptor(
            [("escalation", "24h")],
            identity_id="escalation_composition",
            substrate={
                synth.THEMATIC_DIMENSION_KEY: "escalation",
                synth.THEMATIC_DESKS_KEY: ["country_watch_ir", "country_watch_il"],
            },
            declares_verify=True,
        ),
        target_filter=None,
    )
    prior_q, _ = conn.query_of(_PRIOR_SQL_MARKER)
    assert "f.target_id IS NULL" in prior_q
    _, sit_p = conn.query_of("FROM situations")
    assert sit_p[0] == ["country_watch_ir", "country_watch_il"]


@pytest.mark.asyncio
async def test_continuity_gather_degrades_and_never_breaks_the_slice():
    """Best-effort by contract: a continuity read that RAISES must cost the
    composition nothing — a compose never fails because its memory was
    unavailable. The two refs degrade INDEPENDENTLY."""

    class _ExplodingConn(_RoutingConn):
        async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
            if "FROM situations" in query:
                raise RuntimeError("situations relation unavailable")
            return await super().fetch(query, *params)

    conn = _ExplodingConn(
        slice_rows=[_finding_row(analyst_id="leadership_transition")],
        prior_rows=[_prior_row()],
    )
    rows = await synth.READ_SLICE(
        conn, descriptor=_descriptor(_UNITS), target_filter="country_g20_in"
    )
    prior, register = synth._continuity_selection(rows)
    assert prior is not None            # the surviving ref still lands
    assert register is None             # the failed one is simply absent
    assert any(r.get(synth.CONTINUITY_ROW_KEY) is None for r in rows)


# ---------------------------------------------------------------------------
# 4. Rendering — one flat ordinal space, dated by the blocks' OWN clocks
# ---------------------------------------------------------------------------


def test_render_continuity_block_is_empty_without_either_ref():
    """The byte-identity precondition: a FIRST run renders NO section."""
    assert synth._render_continuity_block(None, [], start_ordinal=4) == ""


def test_render_continuity_block_carries_its_own_dates_and_ordinals():
    block = synth._render_continuity_block(
        _prior_row(), [_situation(), _situation(name="Border standoff", intensity=0.4)],
        start_ordinal=4,
    )
    # Ordinals CONTINUE the basis+periphery numbering, in render order.
    assert "[[ref:4]] PRIOR READ" in block
    assert "[[ref:5]] OPEN SITUATION REGISTER" in block
    # TEMPORAL ANCHORING — each block dates itself; the clause forbids run time.
    assert "produced_at=2026-07-29T06:00:00+00:00" in block
    assert "age=30.0h" in block
    assert "last_event_at=2026-07-30T11:00:00+00:00" in block
    assert "open_for=12.5d" in block
    # The register states each frame's OWN status/intensity/event count.
    assert "status=active intensity=0.82 events=14" in block
    assert "Border standoff" in block
    # The section's own honesty framing.
    assert "CONTINUITY (what this desk already knew)" in block
    assert "ONLY licensed source of 'before'" in block
    assert "state what CHANGED" in block


def test_render_continuity_block_register_only_takes_the_first_ordinal():
    """With no prior read the register takes ``start_ordinal`` itself — the
    ordinal space stays contiguous rather than leaving a hole where the absent
    prior read would have been."""
    block = synth._render_continuity_block(None, [_situation()], start_ordinal=4)
    assert "[[ref:4]] OPEN SITUATION REGISTER" in block
    assert "PRIOR READ" not in block


# ---------------------------------------------------------------------------
# 5. Prompt contract — the clause is in every composition prompt, not the legacy
# ---------------------------------------------------------------------------


_COMPOSITION_PROMPTS = (
    synth._COMPOSITION_SYSTEM,
    synth._REGION_COMPOSITION_SYSTEM,
    synth._WORLD_OVER_REGIONS_SYSTEM,
    synth._THEMATIC_COMPOSITION_SYSTEM,
)


@pytest.mark.parametrize("prompt", _COMPOSITION_PROMPTS)
def test_continuity_clause_is_in_every_composition_prompt(prompt):
    assert "CONTINUITY —" in prompt
    # (1) say what CHANGED versus the cited prior read.
    assert "state EXPLICITLY what CHANGED versus the cited prior read" in prompt
    # (2) anchor "when" on the blocks' OWN dates — never on run time.
    assert "anchor EVERY temporal statement on the dates and ages printed IN those blocks" in prompt
    assert "NEVER on 'today', 'now', 'as of this run', or the time you are running" in prompt
    # (3) no material change is an ANSWER, not a licence to re-derive.
    assert "if nothing material changed, SAY SO plainly" in prompt
    assert "rather than re-deriving the same picture in different words" in prompt
    # (4) never assert ungrounded continuity; a first read says so.
    assert "NEVER assert continuity of ANY kind" in prompt
    assert "unless it is grounded in the cited PRIOR READ block or the SITUATION REGISTER block" in prompt
    assert "this is a FIRST read of this target" in prompt
    # The register is described only as the register states it.
    assert "describe a situation ONLY as the register states it" in prompt


def test_continuity_clause_is_lettered_into_each_prompt_rule_sequence():
    """The clause is generated, not pasted, so every composition floor states it
    identically while still slotting into its own rule lettering."""
    # Phase-V: region and world each gained a COVERAGE rule (D6 — the unit
    # roll-call became a footer), which pushed their continuity slot down one
    # letter. The country and thematic sequences are unchanged.
    assert "(i) CONTINUITY —" in synth._COMPOSITION_SYSTEM
    assert "(j) CONTINUITY —" in synth._REGION_COMPOSITION_SYSTEM
    assert "(k) CONTINUITY —" in synth._WORLD_OVER_REGIONS_SYSTEM
    assert "(j) CONTINUITY —" in synth._THEMATIC_COMPOSITION_SYSTEM
    # Still ahead of the JSON-shape instruction in every prompt.
    for prompt in _COMPOSITION_PROMPTS:
        assert prompt.index("CONTINUITY —") < prompt.index("Respond with strict JSON")


def test_legacy_global_meta_prompt_is_untouched():
    """The legacy global meta gets no continuity slice, so it must get no
    continuity clause — a rule about blocks it will never see is a fabrication
    prompt."""
    assert "CONTINUITY —" not in synth._SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 6. _run end-to-end — citable refs, receipts, envelope, byte-compatible absence
# ---------------------------------------------------------------------------


_OPTIONS = {"target_id": "country_g20_in", "analyst_id": "country_composition"}


@pytest.mark.asyncio
async def test_run_renders_continuity_and_resolves_both_refs_as_citations():
    basis_uid, prior_uid = uuid4(), uuid4()
    sit_uid = uuid4()
    basis = _finding_row(analyst_id="leadership_transition", uid=basis_uid)
    prior = _prior_row(uid=prior_uid, derived_from=[str(uuid4())])
    register = _register_row([_situation(sid=sit_uid)])
    llm = _CannedLLM(
        "Leadership remains contested [[ref:1]]. That is unchanged since the "
        "prior read [[ref:2]], and the open register still shows the same frame "
        "[[ref:3]]."
    )
    result = await synth._run(
        [basis, prior, register], _OPTIONS,
        llm=llm, max_tokens=256, temperature=0.2,
        system_prompt=synth._SYSTEM_PROMPT,
    )

    prompt = _user_prompt_of(llm)
    assert "CONTINUITY (what this desk already knew)" in prompt
    assert "[[ref:2]] PRIOR READ" in prompt
    assert "[[ref:3]] OPEN SITUATION REGISTER" in prompt

    cites = {c["ordinal"]: c for c in result.finding.data["citations"]}
    assert set(cites) == {1, 2, 3}

    # The PRIOR READ is a real finding: ordinary ref_id/ref_kind (its drill
    # target IS the previous read) + the continuity stamp.
    prior_cite = cites[2]
    assert prior_cite["ref_kind"] == "finding"
    assert prior_cite["ref_id"] == str(prior_uid)
    assert prior_cite[synth.CONTINUITY_CITATION_KEY] == synth.CONTINUITY_PRIOR
    assert prior_cite["produced_at"] == "2026-07-29T06:00:00+00:00"
    assert prior_cite["evidence_text"]
    # MEMORY, NOT CORROBORATION: the T7 pair is deliberately absent so last
    # cycle's own conclusion can neither raise this cycle's de-duplicated
    # evidence ceiling nor be folded into a shared-lineage component with a
    # current sub-claim.
    assert "effective_confidence" not in prior_cite
    assert "derived_from" not in prior_cite

    # The REGISTER is not an analyst_outputs row and has NO single substrate id,
    # so it carries the real situation uuids and NEVER a fabricated ref_id.
    reg_cite = cites[3]
    assert reg_cite["ref_kind"] == synth.SITUATION_REGISTER_REF_KIND
    assert reg_cite[synth.CONTINUITY_CITATION_KEY] == synth.CONTINUITY_SITUATIONS
    assert "ref_id" not in reg_cite
    assert reg_cite["situation_ids"] == [str(sit_uid)]
    assert "OPEN SITUATION REGISTER" in reg_cite["evidence_text"]

    # The composition is ANNOTATED by its memory, not DERIVED from it.
    assert result.derived_from == [basis_uid]


@pytest.mark.asyncio
async def test_run_stamps_continuity_receipts_and_envelope():
    prior_uid, sit_uid = uuid4(), uuid4()
    basis = _finding_row(analyst_id="leadership_transition")
    result = await synth._run(
        [basis, _prior_row(uid=prior_uid), _register_row([_situation(sid=sit_uid)])],
        _OPTIONS,
        llm=_CannedLLM("No material change since the prior read [[ref:2]]."),
        max_tokens=256, temperature=0.2, system_prompt=synth._SYSTEM_PROMPT,
    )
    # RECEIPTS — reported wherever the slice reports its composition stats.
    for phase in ("orient", "continuity"):
        step = _step(result, phase)
        assert step[synth.CONTINUITY_PRIOR_RECEIPT] == 1
        assert step[synth.CONTINUITY_SITUATIONS_RECEIPT] == 1
    assert _step(result, "continuity")["situations"] == 1
    assert _step(result, "continuity")["start_ordinal"] == 2
    # Offered vs actually USED — "shown and ignored" is separately countable.
    assert _step(result, "cite")["continuity_cited"] == 1

    env = result.finding.data["continuity"]
    assert env[synth.CONTINUITY_PRIOR_RECEIPT] == 1
    assert env["prior_finding_id"] == str(prior_uid)
    assert env["prior_produced_at"] == "2026-07-29T06:00:00+00:00"
    assert env["prior_age_hours"] == pytest.approx(30.0)
    assert env["situation_ids"] == [str(sit_uid)]


@pytest.mark.asyncio
async def test_run_without_continuity_is_byte_compatible():
    """A FIRST run: no section in the prompt, no envelope on the finding, no
    receipts above zero — the pre-continuity behavior exactly."""
    basis = _finding_row(analyst_id="leadership_transition")
    llm = _CannedLLM("Leadership remains contested [[ref:1]].")
    result = await synth._run(
        [basis], _OPTIONS, llm=llm, max_tokens=256, temperature=0.2,
        system_prompt=synth._SYSTEM_PROMPT,
    )
    assert "CONTINUITY (what this desk already knew)" not in _user_prompt_of(llm)
    assert "continuity" not in result.finding.data
    step = _step(result, "continuity")
    assert step[synth.CONTINUITY_PRIOR_RECEIPT] == 0
    assert step[synth.CONTINUITY_SITUATIONS_RECEIPT] == 0
    assert _step(result, "cite")["continuity_cited"] == 0


@pytest.mark.asyncio
async def test_run_continuity_ordinals_follow_the_periphery_tier():
    """One flat ordinal space across all three sections: basis, then periphery,
    then continuity — so ``[[ref:N]]`` still means "the Nth rendered block"."""
    basis = _finding_row(analyst_id="leadership_transition")
    peri = _finding_row(analyst_id="escalation", title="Weak convoy report")
    peri["_evidence_tier"] = synth.PERIPHERY_TIER
    peri["_evidence_floor"] = 0.5
    basis["_evidence_floor"] = 0.5
    llm = _CannedLLM("Contested [[ref:1]]; weak [[ref:2]]; unchanged [[ref:3]].")
    result = await synth._run(
        [basis, peri, _prior_row()], _OPTIONS,
        llm=llm, max_tokens=256, temperature=0.2,
        system_prompt=synth._SYSTEM_PROMPT,
    )
    prompt = _user_prompt_of(llm)
    assert prompt.index("[[ref:2]] Weak convoy report") < prompt.index("[[ref:3]] PRIOR READ")
    cites = {c["ordinal"]: c for c in result.finding.data["citations"]}
    assert cites[2]["tier"] == synth.PERIPHERY_TIER
    assert cites[3][synth.CONTINUITY_CITATION_KEY] == synth.CONTINUITY_PRIOR


@pytest.mark.asyncio
async def test_run_continuity_never_enters_the_basis_tier():
    """Continuity rows must not consume the input cap, drive salience, or count
    as contributing analysts — they are annotation, not evidence."""
    basis = _finding_row(analyst_id="leadership_transition")
    result = await synth._run(
        [basis, _prior_row(), _register_row([_situation()])], _OPTIONS,
        llm=_CannedLLM("Contested [[ref:1]]."),
        max_tokens=256, temperature=0.2, system_prompt=synth._SYSTEM_PROMPT,
    )
    orient = _step(result, "orient")
    assert orient["kept_count"] == 1
    assert orient["derived_count"] == 1
    assert result.finding.data["contributing_analysts"] == ["leadership_transition"]


@pytest.mark.asyncio
async def test_run_legacy_global_meta_ignores_continuity_rows():
    """Defense in depth: even if a continuity row reached the legacy global meta
    (READ_SLICE never sends one), it stays out of the basis and out of the
    un-cited legacy render."""
    basis = _finding_row(analyst_id="country_assessor")
    llm = _CannedLLM("A global pattern.")
    result = await synth._run(
        [basis, _prior_row()], {},
        llm=llm, max_tokens=256, temperature=0.2,
        system_prompt=synth._SYSTEM_PROMPT,
    )
    prompt = _user_prompt_of(llm)
    assert "PRIOR READ" not in prompt
    assert "citations" not in result.finding.data
    assert "continuity" not in result.finding.data


# ---------------------------------------------------------------------------
# 7. The VERIFY path consumes both refs with NO change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_resolves_continuity_citations_unchanged():
    """A continuity block is just another rendered block with an ordinal and an
    ``evidence_text``, so the faithfulness pass grades a continuity-backed clause
    against exactly what the model was shown — no verify-side change."""
    basis = _finding_row(analyst_id="leadership_transition")
    result = await synth._run(
        [basis, _prior_row(), _register_row([_situation()])], _OPTIONS,
        llm=_CannedLLM("Contested [[ref:1]]; unchanged [[ref:2]]; frame open [[ref:3]]."),
        max_tokens=256, temperature=0.2, system_prompt=synth._SYSTEM_PROMPT,
    )
    citations = result.finding.data["citations"]
    # Still routed to the SUB-CLAIM floor (the prior read carries ref_kind
    # 'finding'; every marker is a [[ref: one), and every ordinal resolves.
    assert verify._uses_subclaim_convention(citations) is True
    assert verify._resolved_citation_ordinals(citations) == {1, 2, 3}
    evidence = verify._ordinal_evidence_map(citations)
    assert "OPEN SITUATION REGISTER" in evidence[3]
    assert "prior sweep" in evidence[2]
    # The prior read carries NO effective_confidence, so it cannot move the
    # composition's evidence ceiling.
    assert 2 not in verify._ordinal_effconf_map(citations)


@pytest.mark.asyncio
async def test_full_register_survives_into_the_judges_evidence():
    """``verify._ordinal_evidence_map`` applies NO cap of its own, so the
    synth-side capture IS what the judge grades against. A FULL register (cap
    frames, cap-length names) must arrive whole — a 600-char cut would hide the
    tail and false-demote a faithful claim about a frame the model was shown."""
    situations = [
        _situation(name=f"{i:02d} " + "frame-name-" * 12, intensity=0.9 - i * 0.01)
        for i in range(synth.SITUATION_REGISTER_CAP)
    ]
    # Names are already bounded by the reader; mirror that bound here.
    for s in situations:
        s["name"] = s["name"][: synth.SITUATION_REGISTER_NAME_CHARS]
    result = await synth._run(
        [_finding_row(analyst_id="leadership_transition"), _register_row(situations)],
        _OPTIONS,
        llm=_CannedLLM("Contested [[ref:1]]; the register still shows [[ref:2]]."),
        max_tokens=256, temperature=0.2, system_prompt=synth._SYSTEM_PROMPT,
    )
    evidence = verify._ordinal_evidence_map(result.finding.data["citations"])[2]
    assert len(evidence) < synth.SITUATION_REGISTER_EVIDENCE_CHARS  # not truncated
    for s in situations:
        assert s["name"] in evidence


@pytest.mark.asyncio
async def test_scorecard_reconcile_skips_the_register_citation():
    """A non-``finding`` ref is skipped, never guessed — the register must not
    read as a unit's finding in the scorecard reconciliation."""
    basis_uid, prior_uid = uuid4(), uuid4()
    basis = _finding_row(analyst_id="leadership_transition", uid=basis_uid)
    result = await synth._run(
        [basis, _prior_row(uid=prior_uid), _register_row([_situation()])], _OPTIONS,
        llm=_CannedLLM("Contested [[ref:1]]; unchanged [[ref:2]]; open [[ref:3]]."),
        max_tokens=256, temperature=0.2, system_prompt=synth._SYSTEM_PROMPT,
    )
    usages = scorecard_reconcile.composition_usages(
        result.finding.data["citations"], [], {}
    )
    assert str(basis_uid) in usages
    assert usages[str(basis_uid)] == ("leadership_transition", "cited")
    # The prior read IS a finding ref (attributed to the composition analyst,
    # never a scorecard dimension); the register carries no ref_id at all.
    assert usages[str(prior_uid)] == ("country_composition", "cited")
    assert len(usages) == 2


@pytest.mark.asyncio
async def test_correlation_guard_is_not_moved_by_the_prior_read():
    """The prior read must not bootstrap this cycle's confidence off last
    cycle's conclusion: it contributes no effective_confidence, so the
    de-duplicated evidence ceiling is the CURRENT evidence's alone."""
    shared = str(uuid4())
    basis = _finding_row(
        analyst_id="leadership_transition",
        effective_confidence=0.4,
        derived_from=[shared],
    )
    result = await synth._run(
        [basis, _prior_row(derived_from=[shared])], _OPTIONS,
        llm=_CannedLLM("Contested [[ref:1]]; unchanged [[ref:2]]."),
        max_tokens=256, temperature=0.2, system_prompt=synth._SYSTEM_PROMPT,
    )
    guard = result.finding.data["correlation_guard"]
    assert guard["dedup_confidence_ceiling"] == pytest.approx(0.4)
    # The prior read shares lineage with the basis head, but is NOT folded into
    # its component — "what we said before" is not "what we see now".
    assert guard["shared_lineage_detected"] is False
    assert result.finding.confidence == pytest.approx(0.4)
