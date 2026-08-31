# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FRAME-2 — THE CARRY (``planning/FRAME_PROGRAM_2026-08-20.md`` §2).

CORRECTNESS-R1's largest attributed failure class was one sentence long: **a
72-hour pipeline forgets its own window.** ≈12 of 23 missed majors were events
that happened in the window's first ten days, WERE in some desk's slice then,
and had aged out of every 72h slice by T0 with nothing carrying them forward —
while the product's own heads printed "mass protest: not_observed" (AR), "State
of emergency – not_observed" (GB) and "no new or tightened sanctions" (UA) for a
fortnight that contained exactly those things.

THE ACCEPTANCE CASE, and it is a real row, not a fixture invented for a test:
``analyst_outputs`` holds "Argentina – mass protests over property bill raise
internal instability", ``severity:elevated``, produced 2026-08-07 by
``internal_stability`` on ``country_g20_ar`` — verified, and SUPERSEDED. The
whole train is the claim that a later run of that same unit can no longer print
"mass protest: not_observed" while its own desk's fortnight says otherwise, and
:func:`test_ar_acceptance_the_unit_sees_its_own_august_7_protest_head` is that
claim as a test, driven through the REAL unit prompt assembly.

What the rest holds, in the order the design commits to it:

  * **SUPERSEDED ROWS ARE ADMITTED, DELIBERATELY.** Supersession is a freshness
    relation, not a retraction. Under a head-fold the fortnight's record is
    almost entirely superseded rows — the AR protest head is one — so the gather
    that is right for the basis is exactly wrong here, and the predicate's
    ABSENCE is asserted rather than left to be re-added by a well-meaning edit.
  * **THE BOUNDS ARE DERIVED, NOT CHOSEN.** The own-unit line cap exists so the
    whole rendered block fits inside the capture the judge grades against; a
    block longer than its capture is one whose tail the model reads and the
    judge cannot. The invariant is asserted here so a future edit to the header
    or the line format trips a test instead of silently truncating evidence.
  * **NO FOREIGN MARKER SURVIVES.** The plan expected marker-free titles; the
    live table disagrees (36 finding titles in 30 days carry a ``[N]``-shaped
    marker). Both marker languages are defused into a bracket-free form that
    re-arms under no parser in the tree.
  * **ABSENCE IS BYTE-IDENTICAL.** No qualifying heads ⇒ no block, no receipt,
    no ordinal consumed, no clause fired — never an empty-rendered block.

The second half covers §2.3, the REGISTER REPAIRS: the render selection that put
three same-day "unchanged_checkpoint" lines in front of every AR run while that
frame's three real dated August escalations sat below the cut, and the naming
rule that let whichever absence read landed last own the frame's label.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts import unit_grounding as ug
from legba.data.analysts import window_ledger as wl
from legba.data.analysts import inline_target as it
from legba.data.analysts.deterministic_handlers import situation_clustering as sc
from legba.data.provenance.kinds import GROUNDING_REF_KINDS, is_grounding_citation
from legba.data.situations import trajectory as tj


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

#: The severity CASE the ledger gather generates from the shared rank ladder —
#: present in no other query either layer fires, so it is the honest routing
#: discriminator for the fakes below.
LEDGER_SQL_MARKER = "CASE f.severity"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _CapturingConn:
    """Fake asyncpg.Connection recording every fetch()'s SQL + params, routing
    the LEDGER family to its own canned rows and everything else to ``[]``."""

    def __init__(self, ledger_rows: list[dict[str, Any]] | None = None) -> None:
        self._ledger = ledger_rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if LEDGER_SQL_MARKER in query:
            return [dict(r) for r in self._ledger]
        return []

    def query_of(self, needle: str) -> tuple[str, tuple[Any, ...]]:
        for call in reversed(self.calls):
            if needle in call[0]:
                return call
        raise AssertionError(f"no captured query contains {needle!r}")

    def has_query(self, needle: str) -> bool:
        return any(needle in q for q, _ in self.calls)


class _CannedLLM:
    """Captures the assembled prompt; returns one structured finding."""

    subprovider = "window_ledger_test_double"

    def __init__(self, body: str = "A read with no citation.") -> None:
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

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50
            reasoning_tokens = 0

        resp = SimpleNamespace()
        resp.content = json.dumps(
            {
                "title": "Composed read",
                "body": self._body,
                "confidence": 0.6,
                "evidence": [],
                "tags": ["severity:moderate"],
            }
        )
        resp.usage = _Usage()
        return resp

    def user_prompt(self) -> str:
        assert len(self.calls) == 1, f"expected 1 LLM call, got {len(self.calls)}"
        for m in self.calls[0]["messages"]:
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
            if role == "user":
                return str(
                    m.get("content") if isinstance(m, dict) else getattr(m, "content")
                )
        raise AssertionError("no user message captured")


def _db_head(
    *,
    analyst_id: str = "internal_stability",
    title: str = "Argentina - a dated head",
    severity: str = "elevated",
    day: int = 7,
    hour: int = 7,
    uid: UUID | None = None,
) -> dict[str, Any]:
    """One row as the LEDGER GATHER's SELECT projects it."""
    return {
        "id": uid or uuid4(),
        "analyst_id": analyst_id,
        "title": title,
        "severity": severity,
        "produced_at": datetime(2026, 8, day, hour, tzinfo=timezone.utc),
        "effective_confidence": 0.68,
    }


def _signal_row(n: int) -> dict[str, Any]:
    """A minimal ``signals`` row for the unit slice (never a grounding row)."""
    return {
        "id": str(uuid4()),
        "source_id": "reuters",
        "title": f"signal {n}",
        "canonical_url": f"https://example.test/{n}",
        "source_url": f"https://example.test/{n}",
        "language": "en",
        "geo": ["AR"],
        "tags": [],
        "fetched_at": (NOW - timedelta(hours=2)).isoformat(),
        "target_id": "country_g20_ar",
        "produced_at": (NOW - timedelta(hours=2)).isoformat(),
        "data": {"body": f"body of signal {n}"},
    }


async def _gathered_unit_rows(conn) -> list[dict[str, Any]]:
    return await ug.gather_unit_grounding_rows(
        conn, analyst_id="internal_stability", target_filter="country_g20_ar",
    )


# ===========================================================================
# 1. THE GATHER — the house admissibility, minus ONE predicate on purpose
# ===========================================================================


@pytest.mark.asyncio
async def test_gather_admits_superseded_rows_deliberately():
    """The one predicate the ledger drops, and the reason it drops it.

    Under the composition head-fold the fortnight's record consists almost
    entirely of superseded rows — the AR protest head is superseded — so a
    ``superseded_by IS NULL`` here would empty the carry of exactly the material
    it exists to carry. Asserted as an ABSENCE so a future edit that "restores
    consistency" with the basis gather turns this red.
    """
    conn = _CapturingConn()
    await wl.read_window_ledger(conn, target_id="country_g20_ar")
    sql, _ = conn.query_of(LEDGER_SQL_MARKER)
    assert "superseded_by" not in sql


@pytest.mark.asyncio
async def test_gather_keeps_every_other_admissibility_leg():
    """Verify GATE (INNER lateral), the floor fold, the severity bar, the meta
    exclusion, the coerce-fallback drop and the window — the house pattern."""
    conn = _CapturingConn()
    await wl.read_window_ledger(conn, target_id="country_g20_ar")
    sql, params = conn.query_of(LEDGER_SQL_MARKER)
    assert "JOIN LATERAL" in sql and "LEFT JOIN" not in sql
    assert "Faithfulness verify%" in sql
    assert "LEAST(f.confidence, v.faithfulness_score) >= $3" in sql
    assert "f.severity = ANY($4::TEXT[])" in sql
    assert "(f.data -> 'data' ->> 'meta') IS DISTINCT FROM 'true'" in sql
    assert "'unstructured','coerce_failed'" in sql
    assert params[0] == "country_g20_ar"
    assert params[1] == wl.LEDGER_WINDOW_HOURS == 336
    assert params[2] == wl.LEDGER_VERIFY_FLOOR == 0.50
    assert set(params[3]) == {"moderate", "elevated", "high", "critical"}
    assert "low" not in params[3], "a 'looked and saw nothing' read is not a record"


@pytest.mark.asyncio
async def test_gather_scopes_to_one_unit_or_to_the_whole_desk():
    conn = _CapturingConn()
    await wl.read_window_ledger(
        conn, target_id="country_g20_ar", analyst_ids=["internal_stability"]
    )
    sql, params = conn.query_of(LEDGER_SQL_MARKER)
    assert "f.analyst_id = ANY($5::TEXT[])" in sql
    assert params[4] == ["internal_stability"]

    desk = _CapturingConn()
    await wl.read_window_ledger(desk, target_id="country_g20_ar")
    sql, params = desk.query_of(LEDGER_SQL_MARKER)
    assert "f.analyst_id = ANY(" not in sql
    assert len(params) == 4


@pytest.mark.asyncio
async def test_gather_refuses_an_empty_scope_rather_than_widening_it():
    """An explicitly empty analyst set reads NOTHING — never the whole desk.
    The ``read_other_analyst_findings`` empty-set contract, one module over."""
    conn = _CapturingConn()
    assert await wl.read_window_ledger(
        conn, target_id="country_g20_ar", analyst_ids=[]
    ) == []
    assert await wl.read_window_ledger(conn, target_id="") == []
    assert conn.calls == []


@pytest.mark.asyncio
async def test_gather_orders_by_severity_so_a_truncation_drops_the_least_severe():
    """The fetch bound is honest only if the SQL sorts by severity FIRST: the
    oldest row is the whole point of a carry, so it must never be what a LIMIT
    throws away."""
    conn = _CapturingConn()
    await wl.read_window_ledger(conn, target_id="country_g20_ar")
    sql, _ = conn.query_of(LEDGER_SQL_MARKER)
    order = sql.rsplit("ORDER BY", 1)[1]
    assert order.index("CASE f.severity") < order.index("f.produced_at")
    assert f"LIMIT {wl.LEDGER_FETCH_LIMIT}" in sql


def test_ledger_entry_refuses_a_row_it_cannot_date_attribute_or_quote():
    """A line's whole value is that it is dated and attributable; a row missing
    either is not worth the ordinal it would consume."""
    assert wl.ledger_entry(_db_head()) is not None
    assert wl.ledger_entry({**_db_head(), "id": None}) is None
    assert wl.ledger_entry({**_db_head(), "title": "   "}) is None
    assert wl.ledger_entry({**_db_head(), "produced_at": None}) is None
    assert wl.ledger_entry({**_db_head(), "produced_at": "not a date"}) is None
    # The block's header STATES every line cleared the severity bar; an ungraded
    # or below-bar line beneath that sentence would make the block lie about
    # itself. The gather's predicate means this never fires in production.
    assert wl.ledger_entry({**_db_head(), "severity": None}) is None
    assert wl.ledger_entry({**_db_head(), "severity": "low"}) is None


def test_ledger_entry_prints_the_house_human_date_never_an_iso_stamp():
    """``NO_INSTRUMENT_READINGS`` forbids the model printing a raw ISO stamp and
    the dated-claim rule forbids it COMPUTING a date — so the block must SHOW
    the date in the form the prose is required to use."""
    entry = wl.ledger_entry(_db_head(day=7))
    assert entry["read_date"] == "7 August 2026"
    assert entry["day"] == "2026-08-07"


# ===========================================================================
# 2. THE SELECTION — pure, so the bounds are testable without a database
# ===========================================================================


def _entries(*specs: tuple[str, str, int]) -> list[dict[str, Any]]:
    """``(analyst_id, severity, day)`` triples → ledger entries."""
    out = []
    for i, (analyst_id, severity, day) in enumerate(specs):
        out.append(
            wl.ledger_entry(
                _db_head(
                    analyst_id=analyst_id,
                    severity=severity,
                    day=day,
                    hour=6 + i,
                    title=f"{analyst_id} {severity} day{day}",
                )
            )
        )
    return out


def test_selection_dedupes_to_one_line_per_unit_per_day_keeping_the_severest():
    """A unit fires twice a day and re-asserts the same standing state each
    time; without the dedupe one story would spend eight lines saying it eight
    ways."""
    kept = wl.select_ledger_entries(
        _entries(
            ("internal_stability", "moderate", 7),
            ("internal_stability", "elevated", 7),
            ("internal_stability", "moderate", 8),
        )
    )
    assert len(kept) == 2
    day7 = [e for e in kept if e["day"] == "2026-08-07"]
    assert [e["severity"] for e in day7] == ["elevated"]


def test_selection_caps_per_unit_at_desk_scope():
    """The plan's fairness bound: without it the loudest dimension on a desk
    crowds the other six out of a 20-line block."""
    kept = wl.select_ledger_entries(
        _entries(*[("escalation", "critical", d) for d in range(6, 14)])
    )
    assert len(kept) == wl.LEDGER_PER_UNIT_CAP == 3


def test_selection_does_not_cap_per_unit_at_own_unit_scope():
    """A unit reading its OWN fortnight has nobody to crowd out, and three
    lines is not a fortnight. The unit-day dedupe is the bound that remains."""
    kept = wl.select_ledger_entries(
        _entries(*[("escalation", "critical", d) for d in range(6, 14)]),
        per_unit_cap=None,
        total_cap=wl.LEDGER_UNIT_TOTAL_CAP,
    )
    assert len(kept) == 8


def test_selection_total_cap_keeps_the_severest_lines():
    kept = wl.select_ledger_entries(
        _entries(
            *[(f"unit_{u}", "moderate", 6 + u) for u in range(9)],
            ("escalation", "critical", 6),
        ),
        total_cap=3,
    )
    assert len(kept) == 3
    assert any(e["severity"] == "critical" for e in kept)


def test_selection_renders_oldest_first_because_a_window_is_read_forward():
    """The house convention for the register / questions / trajectory blocks is
    newest-first; this block deliberately breaks it (and the header says so).
    Those are INDEXES of a current state; this is a RECORD OF A WINDOW."""
    kept = wl.select_ledger_entries(
        _entries(
            ("internal_stability", "moderate", 13),
            ("escalation", "critical", 6),
            ("energy_security", "elevated", 9),
        )
    )
    assert [e["day"] for e in kept] == ["2026-08-06", "2026-08-09", "2026-08-13"]
    assert "OLDEST FIRST" in wl.ledger_block_lines(kept, "[3]")[0]


def test_selection_is_deterministic_and_pure():
    entries = _entries(
        ("a", "moderate", 7), ("b", "elevated", 7), ("a", "critical", 9)
    )
    snapshot = [dict(e) for e in entries]
    first = wl.select_ledger_entries(entries)
    second = wl.select_ledger_entries(entries)
    assert [e["finding_id"] for e in first] == [e["finding_id"] for e in second]
    assert entries == snapshot, "selection must not mutate its input"


# ===========================================================================
# 3. THE MARKER DEFUSE — both languages, into a form that re-arms under neither
# ===========================================================================


def test_defuse_kills_both_marker_languages_into_a_bracket_free_form():
    """The V-2 renderer defect, one field over: a title rendered verbatim into a
    prompt whose live ordinals point at DIFFERENT blocks makes inheriting a
    stale marker the cheapest compliant move. 36 live finding titles in 30 days
    carry a ``[N]``-shaped marker, so this is not hypothetical."""
    unit = wl.defuse_ledger_markers("Protests escalate [3] after the vote [12]")
    assert unit == "Protests escalate (ledger ref 3) after the vote (ledger ref 12)"
    child = wl.defuse_ledger_markers("A composed claim [[ref:4]] carried forward")
    assert "[[ref:4]]" not in child and "(child ref 4)" in child
    # BRACKET-FREE by construction: the output must survive being rendered into
    # the unit ``[N]`` space, the composition ``[[ref:N]]`` space AND the
    # write-time marker normalizers.
    assert "[" not in unit and "]" not in unit
    assert "[" not in child and "]" not in child


def test_defuse_is_idempotent_and_allocation_free_on_clean_text():
    clean = "Argentina - mass protests over property bill"
    assert wl.defuse_ledger_markers(clean) is clean
    once = wl.defuse_ledger_markers("cited [7]")
    assert wl.defuse_ledger_markers(once) == once
    assert wl.defuse_ledger_markers("") == ""


def test_the_gather_defuses_at_entry_build_so_no_caller_can_forget():
    entry = wl.ledger_entry(_db_head(title="Strike halts grain exports [15][1]"))
    assert "[15]" not in entry["title"]
    assert "(ledger ref 15)" in entry["title"]


# ===========================================================================
# 4. THE BOUNDS — derived from the judge's capture, not chosen
# ===========================================================================


def _worst_case(n: int) -> list[dict[str, Any]]:
    return [
        {
            "finding_id": str(uuid4()),
            "analyst_id": "composition_lineage_sweep",  # the longest live id, 25
            "severity": "critical",
            "severity_rank": 4,
            "title": "X" * wl.LEDGER_TITLE_CHARS,
            "read_date": "27 September 2026",
            "day": f"2026-09-{(i % 28) + 1:02d}",
            "produced_at": f"2026-09-{(i % 28) + 1:02d}T12:00:00+00:00",
        }
        for i in range(n)
    ]


def test_own_unit_block_fits_inside_the_grounding_evidence_capture():
    """THE INVARIANT the own-unit line cap is DERIVED from. The unit layer
    captures a block's evidence at ``EVIDENCE_TEXT_CHARS`` and the verify path
    re-caps at ``_EVIDENCE_GROUNDING_CHARS`` — both 2400. A block that renders
    longer than its capture is one whose tail the model reads and the judge
    cannot, which false-demotes a faithful claim about a line the model WAS
    shown. If a header or line-format edit breaks this, it must break HERE and
    not in a live judge verdict.
    """
    from legba.data.provenance.verify import _EVIDENCE_GROUNDING_CHARS

    rendered = "\n".join(
        wl.ledger_block_lines(
            _worst_case(wl.LEDGER_UNIT_TOTAL_CAP), "[19]", scope="unit"
        )
    )
    assert len(rendered) <= ug.EVIDENCE_TEXT_CHARS
    assert len(rendered) <= _EVIDENCE_GROUNDING_CHARS


def test_desk_block_fits_inside_its_own_evidence_capture():
    """The composition's ``_ordinal_evidence_map`` applies no cap of its own, so
    the synth-side capture IS what the judge grades against."""
    worst = _worst_case(wl.LEDGER_TOTAL_CAP)
    full = "\n".join(wl.ledger_block_lines(worst, "[[ref:19]]"))
    assert len(full) <= wl.LEDGER_EVIDENCE_CHARS
    # The CAPTURE is the RENDER, uncut — that equality is the whole property.
    assert wl.ledger_evidence_text(worst, "[[ref:19]]") == full


def test_an_empty_selection_renders_nothing_rather_than_an_empty_block():
    assert wl.ledger_block_lines([], "[3]") == []
    assert wl.ledger_evidence_text([], "[3]") == ""
    assert wl.ledger_finding_ids([]) == []


def test_the_two_scopes_make_different_claims_about_their_own_silence():
    entries = _entries(("internal_stability", "elevated", 7))
    unit_header = wl.ledger_block_lines(entries, "[3]", scope="unit")[0]
    desk_header = wl.ledger_block_lines(entries, "[[ref:3]]")[0]
    assert "THIS UNIT's own" in unit_header
    assert "every dimension's" in desk_header


# ===========================================================================
# 5. THE CLAUSE — one definition, two marker languages, three rules
# ===========================================================================


@pytest.mark.parametrize("handle", ["[N]", "[[ref:N]]"])
def test_the_clause_states_all_three_rules_in_the_callers_handle_language(handle):
    rule = wl.window_ledger_rule(handle)
    assert handle in rule
    # (1) dated, and never as new — the carry's OWN risk.
    assert "ALREADY ESTABLISHED, never as news" in rule
    assert "WITH THE DATE PRINTED ON THAT LINE" in rule
    # (2) standing state is licensed HERE and nowhere else.
    assert "standing state or a duration ONLY where the ledger lines support it" in rule
    # (3) the direct kill for the not_observed-against-the-window class.
    assert "NEVER write that something was absent, not observed, or did not occur" in rule
    assert "in this WINDOW when a ledger line records it" in rule
    # ... with the honest alternative NAMED, so the rule is followable.
    assert "nothing on this in the last 72 hours" in rule


def test_both_layers_state_the_identical_contract():
    """One definition, no drift: the two clauses differ ONLY in the handle."""
    unit = wl.window_ledger_rule("[N]")
    comp = wl.window_ledger_rule("[[ref:N]]")
    assert unit.replace("[N]", "@") == comp.replace("[[ref:N]]", "@")
    assert wl.window_ledger_rule("[N]") in ug.UNIT_GROUNDING_CLAUSE
    # ``read_noun`` (VOICE-4) resolves the as-of line's block noun per variant;
    # it sits ahead of the ledger clause and cannot affect it.
    assert wl.window_ledger_rule("[[ref:N]]") in synth._continuity_rule(
        "C", read_noun="unit"
    )


def test_the_clause_reaches_every_unit_through_the_one_append():
    prompt = ug.with_grounding_clause("You are the INTERNAL STABILITY unit.")
    assert "WINDOW LEDGER" in prompt
    # Idempotent by fingerprint — a re-resolution can never double it.
    assert ug.with_grounding_clause(prompt) == prompt


# ===========================================================================
# 6. THE UNIT LAYER, ON THE REAL BINDING PATH
# ===========================================================================


_AR_PROTEST = "Argentina - mass protests over property bill raise internal instability"


def _ar_desk_rows() -> list[dict[str, Any]]:
    """The AR internal_stability desk's real fortnight shape (severities and
    dates from the live table, read-only 2026-08-20)."""
    return [
        _db_head(title=_AR_PROTEST, severity="elevated", day=7),
        _db_head(
            title="Argentina - elite/regime fracture fuels rising instability",
            severity="moderate", day=7, hour=9,
        ),
        _db_head(
            title="Argentina - waning mass-protest momentum amid concessions",
            severity="moderate", day=8,
        ),
        _db_head(
            title="Argentina - mass protest wave moderates after concession",
            severity="moderate", day=9,
        ),
    ]


@pytest.mark.asyncio
async def test_ar_acceptance_the_unit_sees_its_own_august_7_protest_head():
    """THE ACCEPTANCE TEST. The internal_stability unit's ledger must carry the
    7 August protest head — dated, in the prose's own date form — so a later run
    cannot print "mass protest: not_observed" while its own desk's fortnight
    contradicts it. Driven through the REAL prompt assembly: the gather the
    substrate slice calls, the partition and render ``inline_target.run_method``
    performs, and the clause ``with_grounding_clause`` appends.
    """
    conn = _CapturingConn(ledger_rows=_ar_desk_rows())
    grounding = await _gathered_unit_rows(conn)

    llm = _CannedLLM()
    await it.run_method(
        [_signal_row(1), _signal_row(2), *grounding],
        {"analyst_id": "internal_stability", "target_id": "country_g20_ar"},
        llm,
    )
    prompt = llm.user_prompt()
    assert "WINDOW LEDGER" in prompt
    assert _AR_PROTEST in prompt
    assert "7 August 2026" in prompt, "the carried event must arrive DATED"
    assert "[elevated]" in prompt


@pytest.mark.asyncio
async def test_the_ledger_block_is_citable_and_carries_its_real_member_ids():
    """A synthetic multi-row block: the REAL member uuids, and NO ``ref_id`` —
    pointing at any ONE of N members would be a lie about what the clause rests
    on. And never ``ref_kind='finding'``, which would route the whole unit
    finding to the composition sub-claim verify floor."""
    conn = _CapturingConn(ledger_rows=_ar_desk_rows())
    grounding = await _gathered_unit_rows(conn)
    _text, stamped = ug.render_grounding_section(grounding, start_ordinal=9)
    ordinal, row = next(
        (o, r) for o, r in stamped
        if r[ug.UNIT_GROUNDING_ROW_KEY] == ug.GROUNDING_WINDOW_LEDGER
    )
    citation = ug.citation_for_block(row, ordinal)
    assert citation["ref_kind"] == ug.GROUNDING_WINDOW_LEDGER == "window_ledger"
    assert citation["ref_kind"] in GROUNDING_REF_KINDS
    assert citation["ref_kind"] != "finding"
    assert "ref_id" not in citation
    assert "signal_id" not in citation
    assert len(citation["ledger_finding_ids"]) == 3  # 7th deduped to one line
    assert all(UUID(i) for i in citation["ledger_finding_ids"])
    # The verify path must recognise it, or a ledger-backed clause scores as an
    # unresolved citation and the carry is false-demoted.
    assert is_grounding_citation(citation)


@pytest.mark.asyncio
async def test_the_ledger_takes_the_second_ordinal_and_the_space_stays_contiguous():
    """One flat ``[N]`` space: memory first (prior read, then the fortnight),
    then the open picture. The cite index and the render walk the same order."""
    conn = _CapturingConn(ledger_rows=_ar_desk_rows())
    rows = [
        {ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_SITUATIONS,
         ug.GROUNDING_PAYLOAD_KEY: [{"situation_id": str(uuid4()), "name": "f",
                                     "status": "active"}]},
        *await _gathered_unit_rows(conn),
    ]
    _signals, ordered = ug.partition_grounding_rows(rows)
    text, stamped = ug.render_grounding_section(ordered, start_ordinal=5)
    assert [o for o, _ in stamped] == list(range(5, 5 + len(stamped)))
    kinds = [r[ug.UNIT_GROUNDING_ROW_KEY] for _o, r in stamped]
    assert kinds.index(ug.GROUNDING_WINDOW_LEDGER) < kinds.index(
        ug.GROUNDING_SITUATIONS
    )
    assert "[5] WINDOW LEDGER" in text


@pytest.mark.asyncio
async def test_a_unit_with_no_qualifying_heads_gets_no_block_and_no_receipt():
    """ABSENT, never empty-rendered. A quiet dimension's prompt is
    byte-identical to the pre-FRAME-2 one, and the receipt says 0 so a silently
    FAILED gather is distinguishable from an honestly empty fortnight."""
    conn = _CapturingConn(ledger_rows=[])
    grounding = await _gathered_unit_rows(conn)
    assert grounding == []
    receipts = ug.grounding_receipts(grounding)
    assert receipts["grounding_window_ledger_ref"] == 0
    text, stamped = ug.render_grounding_section(grounding, start_ordinal=9)
    assert (text, stamped) == ("", [])

    present = ug.grounding_receipts(await _gathered_unit_rows(
        _CapturingConn(ledger_rows=_ar_desk_rows())
    ))
    assert present["grounding_window_ledger_ref"] == 1


@pytest.mark.asyncio
async def test_a_failing_ledger_read_never_suppresses_its_sibling_blocks():
    """DEGRADE-NEVER-BREAK, per block: a unit never loses its evidence slice —
    or its other memory — because the carry was unavailable."""

    class _Boom(_CapturingConn):
        async def fetch(self, query: str, *params: Any):
            if LEDGER_SQL_MARKER in query:
                raise RuntimeError("ledger read exploded")
            if "FROM situations" in query:
                return [{"id": uuid4(), "name": "AR - open frame",
                         "status": "active", "intensity_score": 4.0,
                         "event_count": 9, "last_event_at": None,
                         "opened_at": None, "age_days": 3.0, "target_id": None}]
            return []

    rows = await _gathered_unit_rows(_Boom())
    kinds = {r[ug.UNIT_GROUNDING_ROW_KEY] for r in rows}
    assert ug.GROUNDING_WINDOW_LEDGER not in kinds
    assert ug.GROUNDING_SITUATIONS in kinds


@pytest.mark.asyncio
async def test_an_unattributable_run_gets_no_ledger_at_all():
    """The ledger is scoped to THIS unit's own heads; with no ``analyst_id``
    there is no whose. An unscoped read would hand the unit its siblings'
    dimensions, which its descriptor forbids."""
    conn = _CapturingConn(ledger_rows=_ar_desk_rows())
    rows = await ug.gather_unit_grounding_rows(
        conn, analyst_id=None, target_filter="country_g20_ar",
    )
    assert not conn.has_query(LEDGER_SQL_MARKER)
    assert ug.GROUNDING_WINDOW_LEDGER not in {
        r[ug.UNIT_GROUNDING_ROW_KEY] for r in rows
    }


@pytest.mark.asyncio
async def test_the_unit_scope_cap_binds_on_a_chatty_dimension():
    """Ten lines, one per calendar day, severest first — the bound the judge's
    capture derives (see :func:`test_own_unit_block_fits...`)."""
    conn = _CapturingConn(
        ledger_rows=[
            _db_head(title=f"Argentina - day {d}", severity="moderate", day=d)
            for d in range(6, 20)
        ]
    )
    grounding = await _gathered_unit_rows(conn)
    entries = grounding[0][ug.GROUNDING_PAYLOAD_KEY]
    assert len(entries) == wl.LEDGER_UNIT_TOTAL_CAP == 10
    assert len({e["day"] for e in entries}) == 10


@pytest.mark.asyncio
async def test_a_marker_bearing_head_title_arrives_defused_on_the_real_path():
    """The defuse must happen where no caller can forget it — at entry build,
    inside the gather — so the prompt the model reads never carries a live
    stale ordinal."""
    conn = _CapturingConn(
        ledger_rows=[_db_head(title="Strike halts exports [15][1] amid protests")]
    )
    llm = _CannedLLM()
    await it.run_method(
        [_signal_row(1), *await _gathered_unit_rows(conn)],
        {"analyst_id": "internal_stability", "target_id": "country_g20_ar"},
        llm,
    )
    prompt = llm.user_prompt()
    assert "(ledger ref 15)" in prompt
    ledger_block = prompt.split("WINDOW LEDGER", 1)[1].split("\n\n", 1)[0]
    assert "[15]" not in ledger_block and "[1]" not in ledger_block


# ===========================================================================
# 7. THE COMPOSITION LAYER, ON THE REAL BINDING PATH
# ===========================================================================


def _descriptor(units: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        identity=SimpleNamespace(id="country_composition"),
        subscription=SimpleNamespace(
            other_analysts=[
                SimpleNamespace(id=u, time_window="336h", data_types=[]) for u in units
            ],
            targets=SimpleNamespace(predicate='has_tag("g20")'),
        ),
    )


@pytest.mark.asyncio
async def test_read_slice_gathers_the_desk_ledger_for_a_country_composition():
    conn = _CapturingConn(ledger_rows=_ar_desk_rows())
    rows = await synth.READ_SLICE(
        conn,
        descriptor=_descriptor(["internal_stability", "escalation"]),
        target_filter="country_g20_ar",
    )
    sql, params = conn.query_of(LEDGER_SQL_MARKER)
    assert params[0] == "country_g20_ar"
    assert "f.analyst_id = ANY(" not in sql, "the country read gets the WHOLE desk"
    ledger_rows = [
        r for r in rows
        if r.get(synth.CONTINUITY_ROW_KEY) == synth.CONTINUITY_WINDOW_LEDGER
    ]
    assert len(ledger_rows) == 1
    assert synth._ledger_selection(rows) is ledger_rows[0]


@pytest.mark.asyncio
async def test_region_and_world_reads_get_no_ledger():
    """§2.2 scope discipline: a region or world read would be handed five to
    twenty-four desks' fortnights, which is a second evidence slice wearing a
    ledger's clothes. Widening it is an R2 decision, not a round-1 one."""
    region = _CapturingConn(ledger_rows=_ar_desk_rows())
    await synth.READ_SLICE(
        region, descriptor=_descriptor(["country_composition"]),
        target_filter="region_latam",
    )
    assert not region.has_query(LEDGER_SQL_MARKER)

    world_desc = _descriptor(["region_composition"])
    world_desc.method = SimpleNamespace(llm=SimpleNamespace(verify={"enabled": True}))
    world = _CapturingConn(ledger_rows=_ar_desk_rows())
    await synth.READ_SLICE(world, descriptor=world_desc, target_filter=None)
    assert not world.has_query(LEDGER_SQL_MARKER)


@pytest.mark.asyncio
async def test_the_composition_renders_cites_and_stamps_its_fortnight():
    """The whole composition-side chain on the REAL entry (``run_method`` →
    ``_run``): the block renders inside CONTINUITY with a contiguous ordinal,
    the model's marker resolves to the ledger's honest citation shape, the
    receipt lands on the trace, and the envelope records WHICH fortnight this
    compose was shown."""
    entries = wl.select_ledger_entries(
        [wl.ledger_entry(r) for r in _ar_desk_rows()]
    )
    head = {
        "id": uuid4(), "kind": "finding", "title": "AR internal stability head",
        "body": "A unit body.", "confidence": 0.7, "effective_confidence": 0.7,
        "faithfulness_score": 0.9, "severity": "moderate", "data": {"tags": []},
        "target_id": "country_g20_ar", "analyst_id": "internal_stability",
        "produced_at": (NOW - timedelta(hours=6)).isoformat(),
        "derived_from": [], "run_id": uuid4(), "target_version": None,
        "analyst_version": "v1", "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
    }
    ledger_row = {
        synth.CONTINUITY_ROW_KEY: synth.CONTINUITY_WINDOW_LEDGER,
        synth.CONTINUITY_LEDGER_ROW_KEY: entries,
    }
    llm = _CannedLLM(body="Protests on 7 August [[ref:2]] have since subsided.")
    result = await synth.run_method(
        [head, ledger_row],
        {"analyst_id": "country_composition", "target_id": "country_g20_ar"},
        SimpleNamespace(llm=llm),
    )
    prompt = llm.user_prompt()
    assert "=== CONTINUITY" in prompt
    assert "[[ref:2]] WINDOW LEDGER" in prompt
    assert _AR_PROTEST in prompt and "7 August 2026" in prompt

    citation = next(
        c for c in result.finding.data["citations"]
        if c["ref_kind"] == wl.WINDOW_LEDGER_REF_KIND
    )
    assert citation["ordinal"] == 2
    assert "ref_id" not in citation
    assert citation["ledger_finding_ids"] == wl.ledger_finding_ids(entries)
    assert _AR_PROTEST in citation["evidence_text"]

    env = result.finding.data["continuity"]
    assert env[synth.CONTINUITY_LEDGER_RECEIPT] == 1
    assert env["window_ledger_lines"] == len(entries)
    assert env["window_ledger_ids"] == wl.ledger_finding_ids(entries)
    orient = next(s for s in result.intermediate_steps if s["phase"] == "orient")
    assert orient[synth.CONTINUITY_LEDGER_RECEIPT] == 1
    cont = next(s for s in result.intermediate_steps if s["phase"] == "continuity")
    assert cont["window_ledger_lines"] == len(entries)
    cited = next(s for s in result.intermediate_steps if s["phase"] == "cite")
    assert cited["continuity_cited"] == 1, "the model USED its fortnight"


@pytest.mark.asyncio
async def test_a_composition_with_no_fortnight_is_byte_identical_to_before():
    head = {
        "id": uuid4(), "kind": "finding", "title": "AR head", "body": "b",
        "confidence": 0.7, "effective_confidence": 0.7, "faithfulness_score": 0.9,
        "severity": None, "data": {"tags": []}, "target_id": "country_g20_ar",
        "analyst_id": "internal_stability", "analyst_version": "v1",
        "produced_at": (NOW - timedelta(hours=6)).isoformat(), "derived_from": [],
        "run_id": uuid4(), "target_version": None,
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
    }
    llm = _CannedLLM()
    result = await synth.run_method(
        [head],
        {"analyst_id": "country_composition", "target_id": "country_g20_ar"},
        SimpleNamespace(llm=llm),
    )
    assert "WINDOW LEDGER —" not in llm.user_prompt()
    assert "continuity" not in result.finding.data
    orient = next(s for s in result.intermediate_steps if s["phase"] == "orient")
    assert orient[synth.CONTINUITY_LEDGER_RECEIPT] == 0


# ===========================================================================
# 8. THE REGISTER REPAIRS (§2.3)
# ===========================================================================


class _TrajectoryConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return []


@pytest.mark.asyncio
async def test_trajectory_read_ranks_significant_deltas_separately_from_checkpoints():
    """§2.3.1 — the defect and its fix in one query. At T0 the three rendered AR
    trajectory lines were three SAME-DAY "unchanged_checkpoint" rows while that
    frame's three real dated August escalations sat below the render cut: the
    record of movement existed, and the render selected the noise."""
    conn = _TrajectoryConn()
    sid = uuid4()
    await tj.read_trajectories(conn, [sid], per_situation=3)
    sql, params = conn.calls[0]
    assert "e.delta <> 'unchanged_checkpoint'" in sql
    assert "e.delta = 'unchanged_checkpoint'" in sql
    assert "UNION ALL" in sql
    # The movement window is bounded; the checkpoint leg deliberately is NOT (a
    # checkpoint asserts nothing about the world, so an old one cannot mislead).
    assert sql.count("make_interval(hours => $3)") == 1
    assert params[1] == 3
    assert params[2] == tj.SIGNIFICANT_WINDOW_HOURS == 336
    assert params[3] == tj.CHECKPOINT_LINES == 1
    # ORDER IS THE PRODUCT: movement first, quiet last.
    order = sql.rsplit("ORDER BY", 1)[1]
    assert order.index("row_class") < order.index("occurred_at")


@pytest.mark.asyncio
async def test_trajectory_read_returns_the_escalations_before_the_checkpoint():
    """The AR shape end-to-end: three August escalations plus one same-day
    checkpoint, and the caller renders the list AS IT ARRIVES."""
    sid = uuid4()

    class _Rows(_TrajectoryConn):
        async def fetch(self, query: str, *params: Any):
            self.calls.append((query, params))
            rows = [
                {"id": uuid4(), "situation_id": sid,
                 "occurred_at": datetime(2026, 8, d, tzinfo=timezone.utc),
                 "delta": "escalates", "state_from": "watching",
                 "state_to": "escalating", "why": f"escalation on {d} August",
                 "derived_from": [uuid4()], "source_output_id": uuid4(),
                 "created_at": NOW, "row_class": 0}
                for d in (9, 7, 6)
            ]
            rows.append(
                {"id": uuid4(), "situation_id": sid, "occurred_at": NOW,
                 "delta": "unchanged_checkpoint", "state_from": "escalating",
                 "state_to": "escalating", "why": "no coercive measures observed",
                 "derived_from": [], "source_output_id": uuid4(),
                 "created_at": NOW, "row_class": 1}
            )
            return rows

    got = await tj.read_trajectories(_Rows(), [sid], per_situation=3)
    deltas = [r["delta"] for r in got[str(sid)]]
    assert deltas[:3] == ["escalates", "escalates", "escalates"]
    assert deltas[-1] == "unchanged_checkpoint"
    assert deltas.count("unchanged_checkpoint") == 1


def test_the_register_prints_a_trajectory_date_the_prose_may_actually_copy():
    """The clause tells the model to date a trajectory claim from the line it
    was shown; ``_DATED_CLAIM_RULE`` forbids it COMPUTING a date. A line
    printing only an ISO stamp asks for a date the same prompt forbids — so the
    human form rides beside the machine one, as FRAME-1 did for head ages."""
    lines = wl._render_situation_register_lines(
        [
            {
                "name": "AR frame", "status": "active", "intensity_score": 4.2,
                "event_count": 377, "last_event_at": "2026-08-20T13:31:42+00:00",
                "age_days": 30.1, "trajectory_state": "escalating",
                "trajectory": [
                    {"delta": "escalates",
                     "occurred_at": "2026-08-09T03:15:01+00:00",
                     "why": "new financial sanctions on an Ecuadorian gang"},
                ],
            }
        ],
        7,
    )
    delta_line = lines[-1]
    assert "9 August 2026" in delta_line
    # The machine value is KEPT beside it (the operator/debug provenance).
    assert "2026-08-09T03:15:01+00:00" in delta_line
    # An undatable row still renders, and never as a guessed date.
    undated = wl._render_situation_register_lines(
        [{"name": "f", "status": "active",
          "trajectory": [{"delta": "escalates", "occurred_at": None, "why": "w"}]}],
        7,
    )
    assert "(undated)" in undated[-1]


def test_a_rendered_checkpoint_shows_its_date_and_never_its_prose():
    """REGISTER-1c (2026-08-29) — THE LOOP'S SENTENCE, cut at the render.

    H1 exempted ``unchanged_checkpoint`` from the evidence requirement because
    "a checkpoint asserts nothing about the world". True of the delta TYPE and
    false of the ROW: the row carries ``why``, free model prose written under a
    prompt whose own instruction is "situations mostly continue", and this
    render printed it verbatim. Of the fleet's 1,095 checkpoint rows, 375 (34%)
    carry currency language and 53 carry CORROBORATION language; of the ones
    actually RENDERED (one per frame, UNWINDOWED, mean age 3.7d, max 17.3d)
    HALF asserted currency or confirmation.

    The negative below is the LIVE sentence, verbatim from the AR frame's
    rendered checkpoint dated 23 August — still in the desk prompt 24 days after
    the maritime pilots' strike ended, beside ``trajectory=escalating``.

    THE ASYMMETRY IS THE POINT and both halves are asserted here: a SIGNIFICANT
    delta keeps its ``why`` (it cannot be written without cited evidence and it
    is windowed to the read's own fortnight, so its prose is a dated,
    evidence-backed statement of what moved), while the checkpoint keeps only
    the fact its exemption actually claims for it — "we looked on <date> and
    nothing had changed".
    """
    ar_sentence = (
        "No material change was observed; the maritime pilot strike continues "
        "with port operations disrupted"
    )
    lines = wl._render_situation_register_lines(
        [
            {
                "name": "AR frame", "status": "active", "intensity_score": 59.1,
                "event_count": 377, "last_event_at": "2026-08-20T13:31:42+00:00",
                "age_days": 30.1, "trajectory_state": "escalating",
                "trajectory": [
                    {"delta": "escalates",
                     "occurred_at": "2026-08-09T03:15:01+00:00",
                     "why": "new financial sanctions on an Ecuadorian gang"},
                    {"delta": tj.DELTA_UNCHANGED_CHECKPOINT,
                     "occurred_at": "2026-08-23T09:00:00+00:00",
                     "why": ar_sentence},
                ],
            }
        ],
        7,
    )
    block = "\n".join(lines)

    # The checkpoint's PROSE is gone — the whole sentence and every word in it
    # that made the sentence an assertion about the world.
    assert ar_sentence not in block
    assert "maritime pilot strike" not in block
    assert "continues" not in block
    assert "No material change" not in block

    # Its FACT survives: the checkpoint line is still rendered, still dated, and
    # still named. Dropping the line outright would tell a frame with real
    # August escalations and nothing since that it is still moving.
    checkpoint_line = lines[-1]
    assert tj.DELTA_UNCHANGED_CHECKPOINT in checkpoint_line
    assert "23 August 2026" in checkpoint_line
    assert "2026-08-23T09:00:00+00:00" in checkpoint_line

    # The SIGNIFICANT delta above it is untouched — evidence-backed, windowed
    # prose is exactly what this block exists to carry.
    assert "new financial sanctions on an Ecuadorian gang" in block

    # An undated checkpoint still renders, and still without its prose.
    undated_cp = wl._render_situation_register_lines(
        [{"name": "f", "status": "active",
          "trajectory": [{"delta": tj.DELTA_UNCHANGED_CHECKPOINT,
                          "occurred_at": None,
                          "why": "the blockade remains in force"}]}],
        7,
    )
    assert "(undated)" in undated_cp[-1]
    assert "remains in force" not in "\n".join(undated_cp)


def test_the_register_citation_grades_against_the_deprosed_checkpoint():
    """The captured ``evidence_text`` IS the rendered block, so REGISTER-1c has
    to reach the judge's copy too — otherwise the model is shown one register
    and graded against another.

    It does, and the coupling is a shared FUNCTION, not a second copy of the
    render: the synth citation branch calls the very object asserted identical
    below (``meta_findings_synthesizer`` imports it by name from
    ``window_ledger``). Pinning the identity is what makes the render assertion
    that follows a statement about the judge's copy as well as the model's — if
    a future change re-implements the capture, this line fails first.
    """
    assert synth._render_situation_register_lines is (
        wl._render_situation_register_lines
    )
    register = [
        {
            "situation_id": str(uuid4()),
            "name": "AR frame", "status": "active", "intensity_score": 59.1,
            "event_count": 377, "age_days": 30.1,
            "trajectory_state": "escalating",
            "trajectory": [
                {"delta": tj.DELTA_UNCHANGED_CHECKPOINT,
                 "occurred_at": "2026-08-23T09:00:00+00:00",
                 "why": "the maritime pilot strike continues"},
            ],
        }
    ]
    evidence_text = "\n".join(wl._render_situation_register_lines(register, 4))
    assert "maritime pilot strike continues" not in evidence_text
    assert tj.DELTA_UNCHANGED_CHECKPOINT in evidence_text


@pytest.mark.asyncio
async def test_trajectory_read_refuses_the_query_when_nothing_is_asked_for():
    conn = _TrajectoryConn()
    assert await tj.read_trajectories(conn, [], per_situation=3) == {}
    assert await tj.read_trajectories(
        conn, [uuid4()], per_situation=0, checkpoint_lines=0
    ) == {}
    assert conn.calls == []


@pytest.mark.asyncio
async def test_the_register_asks_for_significant_deltas_on_the_real_path():
    """The composition's register enrichment is the ONE production caller of
    ``read_trajectories``. Driven through it, with the REAL reader, so the
    repair is proven where the defect actually lived — the register the units
    and the compositions are shown."""
    conn = _TrajectoryConn()
    await synth._attach_trajectory(conn, [{"situation_id": str(uuid4())}])
    ledger_calls = [
        (q, p) for q, p in conn.calls if "FROM situation_events" in q and "rn" in q
    ]
    assert ledger_calls, "the register must still ask for a trajectory"
    sql, params = ledger_calls[-1]
    assert "e.delta <> 'unchanged_checkpoint'" in sql
    assert params[1] == synth.SITUATION_REGISTER_TRAJECTORY_DEPTH == 3
    assert params[3] == tj.CHECKPOINT_LINES


def test_frame_naming_prefers_the_severe_member_over_a_stale_absence_title():
    """§2.3.2, in the LIVE diagnosis shape. At T0 the AR frame was named
    "Argentina – No Coercive Economic Measures Observed…" because that absence
    read landed last — and note the shared non-event NAME predicate does not
    catch it (its trailing-noun anchor has no "measures"), so severity is what
    must do the work."""
    from legba.runtime.grounding import is_non_event_situation_name

    absence = "Argentina - No Coercive Economic Measures Observed"
    assert not is_non_event_situation_name(absence), (
        "if this ever starts matching, the severity leg is still the guarantee"
    )
    rows = [
        {"id": "1", "title": _AR_PROTEST, "severity": "elevated",
         "produced_at": "2026-08-07T10:00:00+00:00"},
        {"id": "2", "title": absence, "severity": "low",
         "produced_at": "2026-08-12T10:00:00+00:00"},
    ]
    assert sc._situation_name(rows, "sig:country_g20_ar") == _AR_PROTEST


def test_frame_naming_refuses_a_non_event_title_even_at_high_severity():
    """The BF / CD live shape: "… – No observable shift in standing military
    posture" DOES match the shared predicate, and a label that names a non-thing
    is a bad label whatever its severity."""
    rows = [
        {"id": "1", "title": "BF - No observable shift in standing military posture",
         "severity": "high", "produced_at": "2026-08-18T10:00:00+00:00"},
        {"id": "2", "title": "BF - Zongo trial opens in Ouagadougou",
         "severity": "low", "produced_at": "2026-08-14T10:00:00+00:00"},
    ]
    assert sc._situation_name(rows, "sig:country_watch_bf") == (
        "BF - Zongo trial opens in Ouagadougou"
    )


def test_frame_naming_keeps_the_latest_member_rule_as_the_tiebreak():
    """Nothing changes for a frame whose members rank alike — the previous rule
    is the final tiebreak, so an unscored fleet behaves exactly as before."""
    rows = [
        {"id": "1", "title": "Alpha", "produced_at": "2026-08-10T10:00:00+00:00"},
        {"id": "2", "title": "Beta", "produced_at": "2026-08-11T10:00:00+00:00"},
    ]
    assert sc._situation_name(rows, "sig:x") == "Beta"


def test_frame_naming_still_rejects_a_dated_snapshot_title():
    """DQ P6 survives the repair: a report receipt never names a frame, however
    severe or recent."""
    rows = [
        {"id": "1", "title": "World situational assessment - 2026-06-30",
         "severity": "critical", "produced_at": "2026-08-19T10:00:00+00:00"},
    ]
    assert sc._situation_name(rows, "sig:country_g20_ar") == (
        "Situation: country_g20_ar"
    )
    assert sc._situation_name([], "sig:country_g20_ar") == "Situation sig:country_g20_ar"


def test_a_frame_that_gains_a_positive_name_stops_being_marked_steady_state():
    """One predicate, three consumers, no drift: the naming repair also lifts
    the ``steady_state`` stamp that was demoting these frames in the register's
    intensity ranking."""
    rows = [
        {"id": "1", "title": "CD - M23 offensive seizes Kivu towns",
         "severity": "critical", "produced_at": "2026-08-09T10:00:00+00:00"},
        {"id": "2", "title": "DRC - No discernible shift in standing military posture",
         "severity": "low", "produced_at": "2026-08-18T10:00:00+00:00"},
    ]
    fields = sc._situation_fields("sig:country_watch_cd", rows, now=NOW)
    assert fields["name"] == "CD - M23 offensive seizes Kivu towns"
    assert fields["steady_state"] is False


def test_the_cluster_read_projects_severity_so_the_name_can_use_it():
    """A read-column projection, not a join: ``analyst_outputs.severity`` is
    lifted from the ``severity:<level>`` tag at write (S3-T4)."""
    import inspect

    source = inspect.getsource(sc._resolve_pool)
    assert "severity" in source.split("FROM analyst_outputs", 1)[0]


# ---------------------------------------------------------------------------
# H1 — THE REGISTER RENDER: evidence age beside bookkeeping age, and the rule
# that forbids citing the register as corroboration (CORRECTNESS-R2 M-1).
# ---------------------------------------------------------------------------


def _h1_frame(**over):
    """The AR frame as it stood at the round's T0, minus whatever the case under
    test overrides. Numbers are the live-DB values (396 members, intensity
    59.34, last desk write an hour before T0)."""
    frame = {
        "name": "Argentina – maritime pilot strike sustains escalation risk",
        "status": "dormant",
        "intensity_score": 22.45,
        "event_count": 396,
        "last_event_at": "2026-08-25T17:01:16+00:00",
        "age_days": 30.0,
        "last_corroborated_at": "2026-08-21T14:01:09+00:00",
        "evidence_age_days": 4.21,
        "corroboration_count": 4,
        "trajectory_state": "escalating",
    }
    frame.update(over)
    return frame


def test_register_line_carries_the_evidence_clock_beside_the_bookkeeping_clock():
    """Both dates, on the same line, always. The register already printed
    ``last_event_at`` and a desk read it as a world date — "the latest event
    timestamp on 20 August 2026" — for a strike that ended on 5 August."""
    lines = wl._render_situation_register_lines([_h1_frame()], 7)
    frame_line = next(ln for ln in lines if "::" in ln)
    assert "last_event_at=2026-08-25T17:01:16+00:00" in frame_line
    assert "last_corroborated_at=2026-08-21T14:01:09+00:00" in frame_line
    assert "evidence_age=4.2d" in frame_line


def test_a_frame_past_the_horizon_is_labelled_stale():
    """Past the desk's own 72h slice the frame carries the loud label — the
    reader-facing half of 'never renders as active intensity'."""
    line = next(
        ln for ln in wl._render_situation_register_lines([_h1_frame()], 7)
        if "::" in ln
    )
    assert wl.REGISTER_STALE_LABEL in line
    assert "status=dormant" in line
    assert wl.is_stale_frame(_h1_frame()) is True


def test_a_freshly_corroborated_frame_is_not_labelled():
    fresh = _h1_frame(status="active", evidence_age_days=0.4,
                      last_corroborated_at="2026-08-25T06:00:00+00:00")
    line = next(
        ln for ln in wl._render_situation_register_lines([fresh], 7) if "::" in ln
    )
    assert wl.REGISTER_STALE_LABEL not in line
    assert "evidence_age=0.4d" in line
    assert wl.is_stale_frame(fresh) is False


def test_never_corroborated_renders_as_never_not_as_a_borrowed_date():
    """The honesty line the whole mechanism turns on: a frame the ledger has
    never moved must NOT borrow ``last_event_at`` for its evidence date. It says
    NEVER-CORROBORATED, in words.

    This is the fleet's largest class — the 2026-08-27 DQ sweep found 24 of 50
    non-closed frames here, 22 with no ledger rows at all, all rendering
    ``active`` at intensity up to 60.9."""
    old = _h1_frame(last_corroborated_at=None, evidence_age_days=73.0,
                    corroboration_count=0)
    line = next(
        ln for ln in wl._render_situation_register_lines([old], 7) if "::" in ln
    )
    assert f"last_corroborated_at={wl.REGISTER_NO_CORROBORATION}" in line
    assert "2026-08-25T17:01:16" not in line.split("last_corroborated_at=")[1]
    # OLD and never corroborated is the WORST case, not an exempt one — the
    # 73-day Saudi frame from the sweep. It carries the stale label too.
    assert "evidence_age=73.0d" in line
    assert wl.REGISTER_STALE_LABEL in line
    assert wl.is_stale_frame(old) is True


def test_a_brand_new_frame_is_never_labelled_stale():
    """The counterweight: a frame materialized an hour ago has not been
    adjudicated because the tracker runs hourly, not because it is dead. Its
    evidence anchor is its own opening, so it reads fresh."""
    fresh = _h1_frame(status="active", last_corroborated_at=None,
                      evidence_age_days=0.04, corroboration_count=0,
                      age_days=0.04)
    line = next(
        ln for ln in wl._render_situation_register_lines([fresh], 7) if "::" in ln
    )
    assert wl.REGISTER_NO_CORROBORATION in line
    assert wl.REGISTER_STALE_LABEL not in line
    assert wl.is_stale_frame(fresh) is False


def test_the_block_states_the_anti_self_corroboration_rule():
    """The register is [[ref:N]]-citable, so the standing 'no claim may rest on
    orientation alone' clause does not reach it. The rule therefore ships INSIDE
    the block, above the frames it governs."""
    text = "\n".join(wl._render_situation_register_lines([_h1_frame()], 7))
    assert wl.REGISTER_SELF_CORROBORATION_RULE in text
    low = text.lower()
    assert "never be your evidence that an event is ongoing" in low
    assert "'confirms'" in low and "'corroborates'" in low
    assert "product's own bookkeeping" in low
    # It precedes the frames — a rule printed after the data it governs is a
    # footnote, and the defect happened in a BLUF.
    assert text.index(wl.REGISTER_SELF_CORROBORATION_RULE) < text.index("  - Argentina")


def test_the_desk_facing_block_carries_the_identical_repair():
    """The AR escalation desk cited [44][45] — these grounding blocks, not the
    wire. The unit render is a thin adapter over the SAME helper and the SAME
    rule constant, so the two surfaces cannot drift."""
    lines = ug._render_situations([_h1_frame()], 44)
    text = "\n".join(lines)
    assert text.startswith("[44] OPEN SITUATION REGISTER")
    assert wl.REGISTER_SELF_CORROBORATION_RULE in text
    frame_line = next(ln for ln in lines if "::" in ln)
    assert "last_corroborated_at=2026-08-21T14:01:09+00:00" in frame_line
    assert "evidence_age=4.2d" in frame_line
    assert wl.REGISTER_STALE_LABEL in frame_line
