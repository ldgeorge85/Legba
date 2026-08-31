# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FRAME-3 — SEVERITY AS STATE (``planning/FRAME_PROGRAM_2026-08-20.md`` §0.6, §7).

CORRECTNESS-R1's C-B finding was a single tag answering two questions and
therefore neither. A desk tagged the severity of its SLICE DELTA, so a standing
war banded ``low`` on a week that added nothing to it, and 37/37 non-exact bands
sat BELOW the reference. FRAME-3 splits the tag: ``severity`` is the STANDING
level of the dimension, and the slice movement rides a separate
``severity_delta:<rose|fell|steady|new>``.

What is pinned here, and why each pin is the one that matters:

  * **THE CONTRACT REACHES EVERY DESK, INCLUDING THE VOICE-HELD ONE.** The rule
    lives in the base ``UNIT_READ_CONTRACT`` rather than the D6 amendment,
    because ``narrative_coordination`` is one of the seven scorecard DIMENSIONS
    and a card mixing two meanings of ``severity`` across its own dimensions is
    worse than one uniformly on the old meaning. Asserted on the ASSEMBLED
    prompt of all nine, and driven once through ``inline_target.run_method`` so
    it is a property of what a unit actually runs.
  * **THE DELTA NEVER TOUCHES THE BAND.** R4 bands the standing level; the
    movement is reported beside it and no rule reads it. Letting movement move
    the band would re-import the defect from the other side — a war "steady" for
    a fortnight would decay a rung a fortnight.
  * **THE §7 BAND DIFF, AS AN EXECUTABLE ARTEFACT.** One conflict desk (IR) and
    one quiet desk (JP), same evidence under both contracts. IR's band moves
    ``low`` -> ``high``; JP's does not move at all. The second is the control
    that says the fix is not simply "band everything higher".
  * **AN ABSENT DELTA IS FIRST-CLASS.** Every head written before the flip
    reaches its desk carries none, so absence reads ``None`` — never ``steady``,
    which would be a claim that a comparison was made — and every render omits
    the field entirely, leaving an unflipped desk's prompt byte-identical.

The composition half is driven through ``meta_findings_synthesizer.run_method``
and ``._run`` (the real assembly entries) rather than through the block
renderers, because "the composition render shows both" is a claim about the
prompt a compose actually builds.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from legba.data.analysts import inline_target as it
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts._tradecraft import (
    SEVERITY_AS_STATE_RULE,
    SEVERITY_STATE_READ_RULE,
    UNIT_READ_CONTRACT,
    with_preamble_if_absent,
)
from legba.data.analysts.deterministic_handlers import scorecard_banding as sb
from legba.data.analysts.unit_grounding import with_grounding_clause
from legba.data.provenance.models import (
    SEVERITY_DELTA_LEVELS,
    severity_delta_from_tags,
    severity_from_tags,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTORS = REPO_ROOT / "descriptors"

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)

#: The nine bounded units. Identical to ``test_voice_contract.UNITS`` and
#: deliberately re-listed rather than imported: FRAME-3's whole claim is that
#: the tag contract is identical on every desk, and a fleet list that silently
#: shrank with another train's would make this file agree with itself instead of
#: with the fleet.
UNITS: tuple[str, ...] = (
    "escalation",
    "energy_security",
    "economic_coercion",
    "internal_stability",
    "military_posture",
    "proliferation_watch",
    "leadership_transition",
    "narrative_coordination",
    "disruption_status",
)

#: The desk VOICE-3 held back from the D6 prose rewrite. FRAME-3 does not honor
#: that hold — see the module docstring — and this name exists so the exception
#: is asserted rather than assumed.
VOICE_HELD_UNIT: str = "narrative_coordination"


def _norm(text: str) -> str:
    """Whitespace-normalized text — the contract is a Python constant pasted
    into a YAML block scalar, so the WRAPPING differs by construction."""
    return " ".join(text.split())


def _system_prompt(unit: str) -> str:
    doc = yaml.safe_load((DESCRIPTORS / f"analyst_{unit}.yaml").read_text())
    return doc["method"]["system_prompt"]


def _assembled(unit: str) -> str:
    """The prompt the unit actually runs: house preamble + descriptor + clause —
    the same composition ``inline_target._effective_system_prompt`` performs."""
    return with_grounding_clause(with_preamble_if_absent(_system_prompt(unit)) or "")


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class _CannedLLM:
    """Captures the assembled prompts; returns one structured finding."""

    subprovider = "severity_as_state_test_double"

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
                "tags": ["severity:moderate", "severity_delta:steady"],
            }
        )
        resp.usage = _Usage()
        return resp

    def _message(self, role: str) -> str:
        assert self.calls, "no LLM call captured"
        for m in self.calls[0]["messages"]:
            m_role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
            if m_role == role:
                return str(
                    m.get("content") if isinstance(m, dict) else getattr(m, "content")
                )
        raise AssertionError(f"no {role} message captured")

    def user_prompt(self) -> str:
        return self._message("user")

    def system_prompt(self) -> str:
        assert self.calls, "no LLM call captured"
        return str(self.calls[0]["system"] or "")


def _head(
    *,
    analyst_id: str = "escalation",
    title: str = "IR escalation head",
    tags: list[str] | None = None,
    effective_confidence: float = 0.70,
    uid: UUID | None = None,
) -> dict[str, Any]:
    """One basis row as ``read_other_analyst_findings`` projects it."""
    return {
        "id": uid or uuid4(),
        "kind": "finding",
        "title": title,
        "body": "A unit body.",
        "confidence": effective_confidence,
        "effective_confidence": effective_confidence,
        "faithfulness_score": 0.9,
        "severity": None,
        "data": {"tags": list(tags or [])},
        "target_id": "country_g20_ir",
        "analyst_id": analyst_id,
        "produced_at": (NOW - timedelta(hours=6)).isoformat(),
        "derived_from": [],
        "run_id": uuid4(),
        "target_version": None,
        "analyst_version": "v1",
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
    }


def _signal_row(n: int) -> dict[str, Any]:
    """A minimal ``signals`` row for a unit slice."""
    return {
        "id": str(uuid4()),
        "source_id": "reuters",
        "title": f"signal {n}",
        "canonical_url": f"https://example.test/{n}",
        "source_url": f"https://example.test/{n}",
        "language": "en",
        "geo": ["IR"],
        "tags": [],
        "fetched_at": (NOW - timedelta(hours=2)).isoformat(),
        "target_id": "country_g20_ir",
        "produced_at": (NOW - timedelta(hours=2)).isoformat(),
        "data": {"body": f"body of signal {n}"},
    }


class _AcquireCtx:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _GatherConn:
    """Fake connection serving ``scorecard_banding._GATHER_SQL``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(sql)
        assert "DISTINCT ON (f.analyst_id)" in sql, sql[:80]
        return self._rows


class _GatherPool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.conn = _GatherConn(rows)

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self.conn)


def _gather_row(
    analyst_id: str,
    *,
    tags: list[str],
    confidence: float = 0.80,
    faithfulness: float = 0.80,
    finding_id: str | None = None,
) -> dict[str, Any]:
    """One row as ``scorecard_banding._GATHER_SQL`` projects it."""
    return {
        "finding_id": finding_id or str(uuid4()),
        "analyst_id": analyst_id,
        "confidence": confidence,
        "faithfulness_score": faithfulness,
        "tags": tags,
        "produced_at": None,
    }


# ===========================================================================
# 1. THE VOCABULARY — one reader, and the prefix collision it must not have
# ===========================================================================


def test_the_four_movement_calls_are_the_whole_vocabulary():
    assert SEVERITY_DELTA_LEVELS == ("rose", "fell", "steady", "new")
    for level in SEVERITY_DELTA_LEVELS:
        assert severity_delta_from_tags([f"severity_delta:{level}"]) == level


def test_an_absent_or_unknown_delta_reads_none_never_steady():
    """The load-bearing default. Every head written before the flip reaches its
    desk carries no delta, and "nobody said" is not "the desk checked and it
    held" — substituting ``steady`` would invent the comparison."""
    assert severity_delta_from_tags([]) is None
    assert severity_delta_from_tags(None) is None
    assert severity_delta_from_tags(["severity:high", "topic:escalation"]) is None
    assert severity_delta_from_tags(["severity_delta:sideways"]) is None
    assert severity_delta_from_tags(["severity_delta:"]) is None


def test_the_two_tag_readers_do_not_collide_on_their_shared_prefix():
    """``severity_delta:`` starts with ``severity``, and the standing reader
    splits on the FIRST colon — so a desk emitting both tags must not have its
    standing level read as ``delta`` or its movement read as a severity. The
    guard is asserted rather than trusted to the shape of two prefixes."""
    both = ["severity:high", "severity_delta:steady", "topic:escalation"]
    assert severity_from_tags(both) == "high"
    assert severity_delta_from_tags(both) == "steady"
    # And in the other order, since the last valid tag wins in both readers.
    flipped = ["severity_delta:rose", "severity:low"]
    assert severity_from_tags(flipped) == "low"
    assert severity_delta_from_tags(flipped) == "rose"


def test_a_second_delta_tag_takes_the_last_one_and_never_crashes():
    """The prompt asks for exactly one; this is what a model that emits two
    gets. LAST wins, deterministically, and a non-string entry is skipped."""
    assert severity_delta_from_tags(
        ["severity_delta:rose", 7, "severity_delta:fell"]
    ) == "fell"


def test_the_live_evidenced_rise_near_miss_reads_as_rose():
    """LIVE-EVIDENCED (2026-08-25): the fleet emitted ``severity_delta:rise``
    once (~0.25% of stamped deltas), out-of-vocabulary against
    ``rose|fell|steady|new``. ``rise`` has one unambiguous canonical target —
    ``rose`` — so the real reader normalizes it instead of discarding the
    model's stated movement as ``None``."""
    assert severity_delta_from_tags(["severity_delta:rise"]) == "rose"


def test_the_other_unambiguous_near_miss_spellings_also_normalize():
    """The rest of the narrow, evidenced table
    (:data:`legba.data.provenance.models._SEVERITY_DELTA_NEAR_MISS`) — same
    unambiguous-canonical-target rule as ``rise``, exercised through the real
    reader rather than asserted on the table directly."""
    assert severity_delta_from_tags(["severity_delta:rising"]) == "rose"
    assert severity_delta_from_tags(["severity_delta:fall"]) == "fell"
    assert severity_delta_from_tags(["severity_delta:falls"]) == "fell"
    assert severity_delta_from_tags(["severity_delta:falling"]) == "fell"
    assert severity_delta_from_tags(["severity_delta:fallen"]) == "fell"


def test_the_live_evidenced_fallen_near_miss_reads_as_fell():
    """LIVE-EVIDENCED (2026-08-29 DQ sweep §6): the fleet emitted
    ``severity_delta:fallen`` once (1/2340 stamped deltas), the past-
    participle form of the same verb ``fall``/``falls``/``falling`` already
    normalize — ``fell`` is its one unambiguous canonical target. Before this
    fix it read as ``None`` (fails safe, per the "never guess" rule — the
    finding's ``fallen`` movement call was simply dropped, not fabricated as
    ``steady``)."""
    assert severity_delta_from_tags(["severity_delta:fallen"]) == "fell"


def test_a_near_miss_is_case_and_whitespace_tolerant_like_any_other_value():
    """The near-miss lookup sits AFTER the existing ``.strip().lower()``, so it
    inherits the same tolerance the canonical forms already have — no separate
    normalization path to drift out of sync."""
    assert severity_delta_from_tags(["severity_delta: RISE "]) == "rose"


def test_an_ambiguous_or_unevidenced_near_miss_still_reads_none():
    """Guardrail for the ``never guess`` rule: a plausible-looking but
    UNEVIDENCED / ambiguous movement word (not in the narrow table) is not
    silently mapped onto a level — it stays the honest ``None``, same as any
    other out-of-vocabulary tag."""
    assert severity_delta_from_tags(["severity_delta:sideways"]) is None
    assert severity_delta_from_tags(["severity_delta:up"]) is None
    assert severity_delta_from_tags(["severity_delta:down"]) is None
    assert severity_delta_from_tags(["severity_delta:higher"]) is None


# ===========================================================================
# 2. THE UNIT PROMPT HALF — the contract, identical on every desk
# ===========================================================================


@pytest.mark.parametrize("unit", UNITS)
def test_every_unit_prompt_states_the_severity_as_state_contract(unit: str):
    """All NINE, on the ASSEMBLED prompt. The rule is one of the things a
    scorecard reads across dimensions, so a desk that missed the flip would make
    its card's seven bands mean two different things at once."""
    assembled = _norm(_assembled(unit))
    assert _norm(SEVERITY_AS_STATE_RULE) in assembled
    assert "SEVERITY IS THE STANDING STATE" in assembled
    assert "severity_delta:<rose|fell|steady|new>" in assembled


def test_the_rule_rides_the_base_contract_so_the_voice_held_desk_gets_it_too():
    """FRAME-3 does NOT honor the VOICE-3 hold, and this is where that decision
    is written down. The hold is on ``narrative_coordination``'s PROSE (its
    replay could not catch the coordination signal); FRAME-3 is a tag contract,
    the desk is one of the seven scorecard DIMENSIONS, and a card mixing two
    meanings of ``severity`` across its own dimensions is worse than one
    uniformly on the old meaning. Putting the rule in the BASE contract is what
    lets both facts be true at once: the held desk keeps its pre-D6 body shape
    and still gets the tag split."""
    assert _norm(SEVERITY_AS_STATE_RULE) in _norm(UNIT_READ_CONTRACT)
    assert _norm(SEVERITY_AS_STATE_RULE) in _norm(_assembled(VOICE_HELD_UNIT))


def test_the_rule_names_the_defect_it_exists_to_kill():
    """Not a wording test — these three obligations ARE the fix, and a reword
    that drops one silently restores the C-B defect. (1) the tag is the standing
    state; (2) a HIGH level that held is high + steady, never a demotion;
    (3) ``new`` when there is no prior read, never ``steady``."""
    rule = _norm(SEVERITY_AS_STATE_RULE)
    assert "NOT how far it moved in the last 72 hours" in rule
    assert "never a demotion" in rule
    assert "never 'steady', which claims a comparison you did not make" in rule
    assert "never print either one" in rule


@pytest.mark.parametrize("unit", UNITS)
def test_the_rule_outranks_the_schema_paragraph_it_contradicts(unit: str):
    """The sentence that decides whether the train is real.

    Every unit descriptor already carries a paragraph of the form "the tags
    array MUST contain the topic tag X PLUS EXACTLY ONE severity tag" — an
    EXHAUSTIVE-sounding list, sitting under a JSON schema example showing
    exactly those two entries. Adding a third required tag elsewhere in the same
    prompt is a genuine conflict, and the cheapest resolution available to a
    model is to drop the newcomer, which would make FRAME-3 a no-op that no
    prompt-side test could see. So the rule states its own precedence, and this
    asserts BOTH halves are really in the assembled prompt together — the
    conflicting list and the sentence that settles it.
    """
    assembled = _norm(_assembled(unit))
    # Cased loosely because the nine desks word it nine ways ("EXACTLY ONE
    # severity tag drawn from", "exactly ONE severity tag from", "ALWAYS exactly
    # one severity tag scaled to") — which is itself why the override lives in
    # the shared contract instead of in nine edits to those paragraphs.
    assert "exactly one severity tag" in assembled.lower(), (
        f"{unit}: the schema paragraph this rule outranks is gone — if a desk "
        "stopped listing its required tags, re-check that the override still "
        "has something to override"
    )
    assert "THIS TAG IS REQUIRED IN ADDITION to every tag" in assembled
    assert "read it as that list PLUS this one" in assembled


@pytest.mark.asyncio
async def test_a_unit_run_carries_the_contract_on_the_real_binding_path():
    """The pin above is a property of the descriptor; this is the property of
    the RUN. ``inline_target.run_method`` with the production deps bundle —
    descriptor prompt in, ``_effective_system_prompt`` assembling, the clause
    appended — is what a live unit executes."""
    llm = _CannedLLM()
    deps = it.InlineTargetDeps(
        llm=llm, system_prompt=with_preamble_if_absent(_system_prompt("escalation"))
    )
    await it.run_method(
        [_signal_row(1), _signal_row(2)],
        {"analyst_id": "escalation", "target_id": "country_g20_ir"},
        deps,
    )
    assert _norm(SEVERITY_AS_STATE_RULE) in _norm(llm.system_prompt())


# ===========================================================================
# 3. THE COMPOSITION RENDER — both halves, on the real assembly entry
# ===========================================================================


@pytest.mark.asyncio
async def test_the_basis_block_renders_the_standing_level_and_the_movement():
    """"The composition render shows both" (§7), on ``run_method``. Rendering
    only the standing half would hand this layer a number whose MEANING changed
    under it with nothing saying so — the C-B defect moved up one floor."""
    llm = _CannedLLM()
    await synth.run_method(
        [_head(tags=["severity:high", "severity_delta:steady"])],
        {"analyst_id": "country_composition", "target_id": "country_g20_ir"},
        SimpleNamespace(llm=llm),
    )
    assert "severity=high severity_delta=steady" in llm.user_prompt()


@pytest.mark.asyncio
async def test_an_unflipped_head_renders_exactly_as_it_did_before():
    """ABSENT, never defaulted. Until a desk's next run lands under the new
    prompt its heads carry no delta, and the field must vanish rather than
    render ``severity_delta=None`` (which the model would read as a level) or
    ``severity_delta=steady`` (which nobody claimed)."""
    llm = _CannedLLM()
    await synth.run_method(
        [_head(tags=["severity:high"])],
        {"analyst_id": "country_composition", "target_id": "country_g20_ir"},
        SimpleNamespace(llm=llm),
    )
    prompt = llm.user_prompt()
    assert "severity=high" in prompt
    assert "severity_delta" not in prompt


@pytest.mark.asyncio
async def test_the_periphery_block_shows_the_pair_too():
    """The floor-withheld tier renders from the same row-readers, so a block
    that surfaces as hedged periphery must not silently lose its movement call
    — it is exactly the tier where "is this still running?" decides whether the
    hedge is worth writing."""
    basis = _head(analyst_id="leadership_transition", tags=["severity:moderate"])
    peri = _head(
        analyst_id="escalation",
        title="Weak convoy report",
        tags=["severity:high", "severity_delta:rose"],
        effective_confidence=0.31,
    )
    peri[synth._EVIDENCE_TIER_KEY] = synth.PERIPHERY_TIER
    peri[synth._EVIDENCE_FLOOR_KEY] = 0.5
    peri["faithfulness_score"] = 0.31
    llm = _CannedLLM()
    await synth._run(
        [basis, peri],
        {"target_id": "country_g20_ir", "analyst_id": "country_composition"},
        llm=llm,
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused-global",
    )
    prompt = llm.user_prompt()
    assert "WEAKLY-SUPPORTED / UNVERIFIED SIGNALS" in prompt
    periphery = prompt.split("WEAKLY-SUPPORTED", 1)[1]
    assert "severity=high severity_delta=rose" in periphery


@pytest.mark.parametrize(
    "name,prompt",
    (
        ("country", synth._COMPOSITION_SYSTEM),
        ("region", synth._REGION_COMPOSITION_SYSTEM),
        ("world", synth._WORLD_OVER_REGIONS_SYSTEM),
        ("thematic", synth._THEMATIC_COMPOSITION_SYSTEM),
    ),
)
def test_every_composition_prompt_says_how_to_read_the_pair(name: str, prompt: str):
    """A rendered field with no rule is a field the model will invent a use for
    — the D7 lesson, one field over. All four, because the render is shared."""
    assert _norm(SEVERITY_STATE_READ_RULE) in _norm(prompt), name
    assert "A steady delta is NEVER a reason to demote" in prompt, name
    assert "infer none, and do not read its absence as steady" in prompt, name


def test_the_ranking_rule_stops_suppressing_a_standing_high_that_held():
    """The regression FRAME-3 would otherwise CAUSE. D7's rule barred a block
    its unit called "holding steady" from leading — correct when severity was
    the delta, and exactly backwards once ``severity:high`` means a condition
    that is still running. The suppression now keys on the tag and the phrases
    that describe stakes, and names the standing/steady case as eligible."""
    from legba.data.analysts._tradecraft import CONSEQUENCE_RULE

    rule = _norm(CONSEQUENCE_RULE)
    assert "tagged it severity:low, may NOT be your lead" in rule
    assert "stays fully eligible to lead" in rule
    assert '"holding steady", or tagged it severity:low' not in rule


# ===========================================================================
# 4. THE SCORECARD — R4 bands the STANDING level, and only that
# ===========================================================================


@pytest.mark.asyncio
async def test_r4_bands_the_standing_level_and_reports_the_movement_beside_it():
    """The acceptance entry (``gather_and_band``), not the pure rule. A standing
    war that added nothing this slice bands ``high`` and SAYS it is steady —
    which is the whole train in one row."""
    pool = _GatherPool(
        [_gather_row("escalation", tags=["severity:high", "severity_delta:steady"])]
    )
    verdict = await sb.gather_and_band(pool, "country_g20_ir")
    dim = verdict["dimensions"]["escalation"]
    assert dim["band"] == "high"
    assert dim["severity_tag"] == "high"
    assert dim["severity_delta"] == "steady"
    assert dim["damped"] is False
    assert dim["reason"] == "qualified"


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", (*SEVERITY_DELTA_LEVELS, None))
async def test_the_movement_call_never_moves_the_band(delta: str | None):
    """The band is a statement about a CONDITION. If movement could move it, a
    war "steady" for a fortnight would decay a rung a fortnight — the R1 defect
    re-imported from the other side."""
    tags = ["severity:elevated"] + (
        [f"severity_delta:{delta}"] if delta is not None else []
    )
    pool = _GatherPool([_gather_row("escalation", tags=tags)])
    verdict = await sb.gather_and_band(pool, "country_g20_ir")
    dim = verdict["dimensions"]["escalation"]
    assert dim["band"] == "elevated"
    assert dim["severity_delta"] == delta


def test_the_movement_call_never_reaches_the_retired_damper_either():
    """FRAME-3 §0.6 left damping untouched; H3 RETIRED it — and the property this
    test was written to protect survives the retirement unchanged, which is the
    point of keeping it.

    The damper keyed on effective confidence and nothing else. Its retirement
    record (``damped_would_have_been``) keys on effective confidence and nothing
    else. So the same standing level at the same confidence produces the same
    band AND the same record whether the desk called the slice ``rose``,
    ``steady``, or said nothing at all — the movement call has never touched the
    band, and it does not touch the band the band used to be either.
    """
    bands = {
        label: sb.band_dimension(
            sb.Claim(
                finding_id=str(uuid4()),
                analyst_id="escalation",
                confidence=0.55,
                faithfulness_score=0.55,
                tags=["severity:high"] + extra,
            )
        )
        for label, extra in (
            ("rose", ["severity_delta:rose"]),
            ("steady", ["severity_delta:steady"]),
            ("absent", []),
        )
    }
    for label, verdict in bands.items():
        assert verdict.band == "high", label            # the tag's band
        assert verdict.damped is False, label
        assert verdict.reason == "qualified-low-confidence", label
        # identical across all three deltas — the retired rung, recorded
        assert verdict.damped_would_have_been == "elevated", label
    assert bands["steady"].severity_delta == "steady"
    assert bands["absent"].severity_delta is None


def test_an_insufficient_verdict_reports_no_movement_at_all():
    """R0-R3 have no claim they are entitled to report anything about. A delta
    on an insufficient-evidence dimension would be a fact sourced from a row the
    engine just refused to band."""
    for claim in (
        None,
        sb.Claim(
            finding_id=str(uuid4()),
            analyst_id="escalation",
            confidence=0.9,
            faithfulness_score=None,
            tags=["severity:high", "severity_delta:rose"],
        ),
        sb.Claim(
            finding_id=str(uuid4()),
            analyst_id="escalation",
            confidence=0.20,
            faithfulness_score=0.20,
            tags=["severity:high", "severity_delta:rose"],
        ),
    ):
        verdict = sb.band_dimension(claim)
        assert verdict.band == sb.INSUFFICIENT
        assert verdict.severity_delta is None
        assert verdict.basis == []


@pytest.mark.asyncio
async def test_every_card_records_which_severity_contract_produced_it():
    """A ``low`` band from before the flip and a ``low`` band after it are the
    same three characters meaning two different things. The stamp is what makes
    the §7 before/after diff a machine comparison instead of a guess about when
    each desk flipped."""
    pool = _GatherPool([_gather_row("escalation", tags=["severity:high"])])
    verdict = await sb.gather_and_band(pool, "country_g20_ir")
    assert verdict["banding_semantics"] == sb.BANDING_SEMANTICS == "standing"
    # Every dimension carries the field, present or not, so a consumer never has
    # to distinguish "no delta" from "this build predates the field".
    assert all(
        "severity_delta" in d for d in verdict["dimensions"].values()
    )


# ===========================================================================
# 5. THE §7 BAND DIFF — one conflict desk, one quiet desk
# ===========================================================================
#
# The gate §7 asks for before the fleet sees this: "a before/after band diff on
# one conflict desk (IR: the 4-rung miss) and one quiet desk (JP)". Written as a
# test rather than left to a post-deploy query because the difference is
# DETERMINISTIC — same desk, same evidence, same confidence, one tag re-read —
# and a pure rule engine can state it exactly. The live snapshot the operator
# takes after the soak is the same comparison over real rows, discriminated by
# the ``banding_semantics`` stamp above.


def _claim(tags: list[str], *, confidence: float = 0.80) -> sb.Claim:
    return sb.Claim(
        finding_id=str(uuid4()),
        analyst_id="escalation",
        confidence=confidence,
        faithfulness_score=confidence,
        tags=tags,
    )


def test_band_diff_ir_the_conflict_desk_stops_reading_its_own_war_as_quiet():
    """IR — the conflict desk. Same standing conflict, same slice, same
    confidence; the ONLY difference is which question the severity tag answers.

    BEFORE: the desk tags the SLICE DELTA. Nothing new happened this cycle, so
    it tags ``severity:low`` and the dimension bands ``low`` — the C-B defect,
    verbatim.

    AFTER: the desk tags the STANDING STATE (``high``) and says separately that
    the slice was ``steady``. The band is ``high`` and the quiet is still on the
    record, in the field that was always the right place for it.
    """
    before = sb.band_dimension(_claim(["severity:low"]))
    after = sb.band_dimension(_claim(["severity:high", "severity_delta:steady"]))

    assert before.band == "low"
    assert after.band == "high"
    moved = sb.BAND_LADDER.index(after.band) - sb.BAND_LADDER.index(before.band)
    assert moved == 3, "low -> high is three rungs up the five-rung ladder"
    # Nothing but the tag changed: the confidence, the damping and the basis
    # shape are identical on both sides, so the move is attributable.
    assert before.damped is after.damped is False
    assert before.effective_confidence == after.effective_confidence
    assert after.severity_delta == "steady" and before.severity_delta is None


def test_band_diff_jp_the_quiet_desk_does_not_move_at_all():
    """JP — the control, and the more important half of the gate. A genuinely
    quiet desk's standing state IS low, so re-reading the tag must move NOTHING.
    Without this the train's evidence would be equally consistent with "the fix
    simply bands everything higher", which is not a fix."""
    before = sb.band_dimension(_claim(["severity:low"]))
    after = sb.band_dimension(_claim(["severity:low", "severity_delta:steady"]))

    assert before.band == after.band == "low"
    # Field-wise rather than whole-object, because ``basis`` names two different
    # (real) finding ids by construction and ``severity_delta`` is the one field
    # that is SUPPOSED to differ: the desk now says "and it held".
    for field in ("band", "severity_tag", "effective_confidence", "damped", "reason"):
        assert getattr(before, field) == getattr(after, field), field
    assert (before.severity_delta, after.severity_delta) == (None, "steady")


def test_band_diff_a_desk_that_actually_escalated_still_reads_as_escalating():
    """The third case the pair above does not cover: a desk whose standing level
    genuinely ROSE this slice. The band follows the new standing level and the
    delta says how it got there — the two facts a single tag could never carry
    at the same time."""
    verdict = sb.band_dimension(_claim(["severity:high", "severity_delta:rose"]))
    assert verdict.band == "high"
    assert verdict.severity_delta == "rose"
