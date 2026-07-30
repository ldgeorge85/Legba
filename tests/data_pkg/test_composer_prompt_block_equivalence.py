# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-4 equivalence proof for the ONE composition prompt-block interface.

The composition user turn was assembled by eight ad-hoc
``user_prompt = user_prompt + "\\n" + block`` statements whose order, separators
and empty-checks were load-bearing but implicit. They were collapsed onto
:class:`legba.data.analysts.meta_findings_synthesizer._PromptBlockAssembler`.

Two independent proofs:

**Part A — assembler semantics.** Exhaustive unit coverage of position, separator,
guard, lazy-render and the ``require_non_empty`` asymmetry, plus the shared
char/token accounting.

**Part B — END-TO-END byte identity against the pre-refactor splice.** The real
``_run`` is exercised across every composition path; each ``_render_*`` block
function is wrapped in a spy, and the prompt the pre-refactor code WOULD have
produced is reconstructed from the observed renderer outputs using the verbatim
068d515 splice logic (:func:`_ref_splice`). The reconstruction is compared
byte-for-byte with the prompt production actually sent to the LLM.

Part B is the load-bearing proof: it does not re-implement ``_run``'s internals,
it replays the ORIGINAL splice over the REAL blocks of a REAL run.
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import meta_findings_synthesizer as synth


# ===========================================================================
# Part A — assembler semantics
# ===========================================================================
A = synth._PromptBlockAssembler
_APPEND = synth._BLOCK_APPEND
_PREPEND = synth._BLOCK_PREPEND


def test_assembler_base_only() -> None:
    a = A("BASE")
    assert a.prompt == "BASE"
    assert a.total_chars == 4
    assert a.block_ledger == [("base", 4)]


def test_assembler_append_and_prepend_order() -> None:
    a = A("BASE")
    a.add("x", lambda: "X", when=True, position=_APPEND, separator="\n")
    a.add("y", lambda: "Y", when=True, position=_PREPEND, separator="\n\n")
    assert a.prompt == "Y\n\nBASE\nX"


def test_assembler_prepends_stack_outermost_last() -> None:
    """Later prepends land FIRST — the freshness-before-salience ordering."""
    a = A("BASE")
    a.add("sal", lambda: "SAL", when=True, position=_PREPEND, separator="\n\n")
    a.add("fresh", lambda: "FRESH", when=True, position=_PREPEND, separator="\n\n")
    assert a.prompt == "FRESH\n\nSAL\n\nBASE"


def test_assembler_false_guard_skips_and_never_renders() -> None:
    calls: list[int] = []

    def _render() -> str:
        calls.append(1)
        return "NOPE"

    a = A("BASE")
    a.add("x", _render, when=False, position=_APPEND, separator="\n")
    a.add("y", _render, when=[], position=_APPEND, separator="\n")
    a.add("z", _render, when=None, position=_APPEND, separator="\n")
    a.add("w", _render, when=0, position=_APPEND, separator="\n")
    assert a.prompt == "BASE"
    assert calls == [], "renderer must stay LAZY behind a false guard"
    assert a.block_ledger == [("base", 4)]


def test_assembler_empty_render_skipped_by_default() -> None:
    a = A("BASE")
    a.add("x", lambda: "", when=True, position=_APPEND, separator="\n")
    a.add("y", lambda: "", when=True, position=_PREPEND, separator="\n\n")
    assert a.prompt == "BASE"
    assert a.block_ledger == [("base", 4)]


def test_assembler_require_non_empty_false_splices_empty_block() -> None:
    """The CONTESTED asymmetry: an empty render still splices its separator."""
    a = A("BASE")
    a.add(
        "contested",
        lambda: "",
        when=True,
        position=_APPEND,
        separator="\n",
        require_non_empty=False,
    )
    assert a.prompt == "BASE\n"
    assert a.block_ledger == [("base", 4), ("contested", 0)]


def test_assembler_truthy_non_bool_guard() -> None:
    a = A("BASE")
    a.add("x", lambda: "X", when=[{"g": 1}], position=_APPEND, separator="\n")
    assert a.prompt == "BASE\nX"


def test_assembler_rejects_unknown_position() -> None:
    a = A("BASE")
    with pytest.raises(ValueError, match="unknown prompt-block position"):
        a.add("x", lambda: "X", when=True, position="sideways", separator="\n")


def test_assembler_accounting_matches_prompt() -> None:
    a = A("BASE")
    a.add("x", lambda: "XXX", when=True, position=_APPEND, separator="\n")
    a.add("y", lambda: "YY", when=True, position=_PREPEND, separator="\n\n")
    assert a.prompt == "YY\n\nBASE\nXXX"
    assert a.total_chars == len(a.prompt)
    assert a.block_ledger == [("base", 4), ("x", 3), ("y", 2)]
    # chars/4 ceiling — the house convention (inline_target._estimate_tokens).
    assert a.est_tokens == (len(a.prompt) + 3) // 4


def test_assembler_est_tokens_ceiling() -> None:
    assert A("").est_tokens == 0
    assert A("a").est_tokens == 1
    assert A("abcd").est_tokens == 1
    assert A("abcde").est_tokens == 2


def test_assembler_ledger_is_a_copy() -> None:
    a = A("BASE")
    led = a.block_ledger
    led.append(("bogus", 1))
    assert a.block_ledger == [("base", 4)]


@pytest.mark.parametrize(
    "flags", list(itertools.product([False, True], repeat=4))
)
def test_assembler_matches_hand_splice_over_guard_matrix(flags: tuple[bool, ...]) -> None:
    """Exhaustive: assembler == the equivalent hand-written splice."""
    g1, g2, g3, g4 = flags
    a = A("BASE")
    a.add("p1", lambda: "P1", when=g1, position=_APPEND, separator="\n\n")
    a.add("p2", lambda: "P2", when=g2, position=_APPEND, separator="\n")
    a.add("p3", lambda: "P3", when=g3, position=_PREPEND, separator="\n\n")
    a.add("p4", lambda: "P4", when=g4, position=_PREPEND, separator="\n\n")

    expected = "BASE"
    if g1:
        expected = expected + "\n\n" + "P1"
    if g2:
        expected = expected + "\n" + "P2"
    if g3:
        expected = "P3" + "\n\n" + expected
    if g4:
        expected = "P4" + "\n\n" + expected
    assert a.prompt == expected


# ===========================================================================
# Part B — end-to-end byte identity against the pre-refactor splice
# ===========================================================================
#: The block renderers the composer splices, in the order the ORIGINAL code
#: applied them.
_BLOCK_FNS = (
    "_render_user_prompt",
    "_render_periphery_block",
    "_render_contested_block",
    "_render_region_coverage_block",
    "_render_world_aperture_block",
    "_render_desk_coverage_block",
    "_render_salience_lead_block",
    "_render_freshness_advisory_block",
)


class _BlockSpy:
    """Records each block renderer's RETURN value as ``_run`` executes."""

    def __init__(self) -> None:
        self.returns: dict[str, list[str]] = {name: [] for name in _BLOCK_FNS}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in _BLOCK_FNS:
            original = getattr(synth, name)
            monkeypatch.setattr(
                synth, name, self._wrap(name, original), raising=True
            )

    def _wrap(self, name: str, original: Any) -> Any:
        def _spy(*args: Any, **kwargs: Any) -> Any:
            out = original(*args, **kwargs)
            self.returns[name].append(out)
            return out

        return _spy

    def one(self, name: str) -> str | None:
        """The single recorded return for ``name``, or ``None`` if never called.

        A renderer that was never called means its GUARD was false — the
        renderers are lazy, which the original code relied on too.
        """
        vals = self.returns[name]
        assert len(vals) <= 1, f"{name} rendered {len(vals)}x, expected <=1"
        return vals[0] if vals else None


def _ref_splice(spy: _BlockSpy) -> str:
    """The VERBATIM pre-refactor (068d515) block splice.

    Reconstructs what the ad-hoc assembly WOULD have produced, from the blocks
    the real run actually rendered. Kept structurally identical to the original
    statements — do NOT tidy this into a loop; being a frozen copy is the point.
    """
    user_prompt = spy.one("_render_user_prompt")
    assert user_prompt is not None, "base prompt was never rendered"

    # if is_composition and periphery_sel:
    _peri_block = spy.one("_render_periphery_block")
    if _peri_block is not None:
        if _peri_block:
            user_prompt = user_prompt + "\n\n" + _peri_block
    # if contention_groups:   (NOTE: no empty-check in the original)
    _contested = spy.one("_render_contested_block")
    if _contested is not None:
        user_prompt = user_prompt + "\n" + _contested
    # if region_coverage:
    _coverage_block = spy.one("_render_region_coverage_block")
    if _coverage_block is not None:
        if _coverage_block:
            user_prompt = user_prompt + "\n" + _coverage_block
    # if world_composition and region_coverage:
    _aperture_block = spy.one("_render_world_aperture_block")
    if _aperture_block is not None:
        if _aperture_block:
            user_prompt = user_prompt + "\n" + _aperture_block
    # if desk_coverage:
    _desk_block = spy.one("_render_desk_coverage_block")
    if _desk_block is not None:
        if _desk_block:
            user_prompt = user_prompt + "\n" + _desk_block
    # if is_composition:
    _sal_block = spy.one("_render_salience_lead_block")
    if _sal_block is not None:
        if _sal_block:
            user_prompt = _sal_block + "\n\n" + user_prompt
    # if freshness_advisory:
    _fresh_block = spy.one("_render_freshness_advisory_block")
    if _fresh_block is not None:
        if _fresh_block:
            user_prompt = _fresh_block + "\n\n" + user_prompt
    return user_prompt


class _CapturingLLM:
    """LLM double returning a canned payload; captures the user turn."""

    subprovider = "c4_block_equivalence_double"

    def __init__(self) -> None:
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
                "body": "A composed clause [[ref:1]].",
                "confidence": 0.6,
                "evidence": [],
                "tags": [],
            }
        )
        resp.usage = _Usage()
        return resp

    @property
    def user_prompt(self) -> str:
        assert len(self.calls) == 1, f"expected 1 LLM call, got {len(self.calls)}"
        for m in self.calls[0]["messages"]:
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
            if role == "user":
                content = (
                    m.get("content") if isinstance(m, dict) else getattr(m, "content")
                )
                return str(content)
        raise AssertionError("no user message captured")


def _row(
    *,
    analyst_id: str = "leadership_transition",
    uid: UUID | None = None,
    title: str = "sub-claim title",
    body: str = "sub-claim body",
    confidence: float = 0.7,
    effective_confidence: float | None = 0.7,
    faithfulness_score: float | None = 0.9,
    produced_at: str = "2026-06-30T00:00:00+00:00",
    target_id: str = "country_g20_in",
    salience: float | None = None,
    periphery: bool = False,
    floor: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inner: dict[str, Any] = {"tags": [], "evidence": []}
    if salience is not None:
        inner["data"] = {"salience": {"magnitude": salience, "top_title": title}}
    r: dict[str, Any] = {
        "id": uid or uuid4(),
        "kind": "finding",
        "title": title,
        "body": body,
        "confidence": confidence,
        "effective_confidence": effective_confidence,
        "faithfulness_score": faithfulness_score,
        "severity": None,
        "data": inner,
        "evidence": [],
        "target_id": target_id,
        "target_version": None,
        "analyst_id": analyst_id,
        "analyst_version": "vtest",
        "produced_at": produced_at,
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }
    if periphery:
        r[synth._EVIDENCE_TIER_KEY] = synth.PERIPHERY_TIER
    if floor is not None:
        r[synth._EVIDENCE_FLOOR_KEY] = floor
    if extra:
        r.update(extra)
    return r


_GAP_COVERAGE = [
    {"region_id": "region_emea", "region_name": "EMEA", "mode": "region"},
    {"region_id": "region_latam", "region_name": "LATAM", "mode": synth.REGION_MODE_GAP},
]
_NO_GAP_COVERAGE = [
    {"region_id": "region_emea", "region_name": "EMEA", "mode": "region"},
]
_DESK_COVERAGE = [
    {"desk_id": "country_g20_in", "desk_name": "India", "mode": "present"},
    {"desk_id": "country_g20_br", "desk_name": "Brazil", "mode": synth.REGION_MODE_GAP},
]
_CONTENTION = [
    {
        "contention_id": "c-1",
        "subject_key": "kyiv",
        "predicate_key": "casualties",
        "values": [
            {"value_key": "12", "surfaced_winner": True, "arbiter_score": 0.71},
            {"value_key": "30", "surfaced_winner": False, "arbiter_score": None},
        ],
    }
]
_FRESHNESS = {
    "advisory": [
        {
            "unit": "leadership_transition",
            "target": "country_g20_in",
            "old_title": "Coalition stable",
            "old_confidence": 0.8,
            "new_title": "Coalition fractured",
            "new_confidence": 0.7,
            "superseded_at": "2026-07-20T00:00:00+00:00",
        }
    ],
    "stale_roots": ["root-1"],
    "inputs_as_of": [],
}


def _scenarios() -> list[tuple[str, list[dict[str, Any]], dict[str, Any]]]:
    """(name, input rows, options) per composition path."""
    out: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    # --- legacy GLOBAL meta (not a composition: no salience, no periphery) ---
    out.append(("legacy_meta", [_row()], {"analyst_id": "meta_synth"}))

    # --- COUNTRY composition -------------------------------------------
    country_opts = {"target_id": "country_g20_in", "analyst_id": "country_composition"}
    out.append(("country_plain", [_row()], dict(country_opts)))
    out.append(
        ("country_salience", [_row(salience=0.8), _row(salience=0.2)], dict(country_opts))
    )
    out.append(
        (
            "country_periphery",
            [
                _row(floor=0.5),
                _row(
                    analyst_id="escalation",
                    title="Weak convoy report",
                    effective_confidence=0.31,
                    faithfulness_score=0.31,
                    periphery=True,
                    floor=0.5,
                ),
            ],
            dict(country_opts),
        )
    )
    out.append(
        (
            "country_periphery_and_salience",
            [
                _row(salience=0.9, floor=0.5),
                _row(
                    analyst_id="escalation",
                    effective_confidence=0.2,
                    faithfulness_score=0.2,
                    periphery=True,
                    floor=0.5,
                ),
            ],
            dict(country_opts),
        )
    )
    out.append(
        (
            "country_freshness",
            [_row(salience=0.6, extra={"_freshness": _FRESHNESS})],
            dict(country_opts),
        )
    )

    # --- REGION composition ---------------------------------------------
    region_opts = {
        "target_id": f"{synth.REGION_TARGET_PREFIX}emea",
        "analyst_id": "region_composition",
    }
    out.append(("region_plain", [_row(analyst_id="country_composition")], dict(region_opts)))
    out.append(
        (
            "region_salience_freshness",
            [
                _row(
                    analyst_id="country_composition",
                    salience=0.7,
                    extra={"_freshness": _FRESHNESS},
                )
            ],
            dict(region_opts),
        )
    )

    # --- WORLD composition ----------------------------------------------
    world_opts = {"composition": True, "analyst_id": "world_assessor"}
    out.append(
        ("world_plain", [_row(analyst_id="region_composition")], dict(world_opts))
    )
    out.append(
        (
            "world_gap_coverage_and_aperture",
            [
                _row(
                    analyst_id="region_composition",
                    extra={"_region_coverage": _GAP_COVERAGE},
                )
            ],
            dict(world_opts),
        )
    )
    out.append(
        (
            "world_nogap_coverage_aperture_only",
            [
                _row(
                    analyst_id="region_composition",
                    extra={"_region_coverage": _NO_GAP_COVERAGE},
                )
            ],
            dict(world_opts),
        )
    )
    out.append(
        (
            "world_contested",
            [_row(analyst_id="region_composition")],
            dict(world_opts, contention_groups=_CONTENTION),
        )
    )
    out.append(
        (
            "world_everything",
            [
                _row(
                    analyst_id="region_composition",
                    salience=0.95,
                    floor=0.5,
                    extra={
                        "_region_coverage": _GAP_COVERAGE,
                        "_freshness": _FRESHNESS,
                    },
                ),
                _row(
                    analyst_id="region_composition",
                    title="Weak world signal",
                    effective_confidence=0.2,
                    faithfulness_score=0.2,
                    periphery=True,
                    floor=0.5,
                ),
            ],
            dict(world_opts, contention_groups=_CONTENTION),
        )
    )

    # --- THEMATIC composition -------------------------------------------
    thematic_opts = {
        "thematic_dimension": "escalation",
        "analyst_id": "escalation_composition",
    }
    out.append(
        ("thematic_plain", [_row(analyst_id="escalation")], dict(thematic_opts))
    )
    out.append(
        (
            "thematic_desk_coverage",
            [
                _row(
                    analyst_id="escalation",
                    extra={"_thematic_coverage": _DESK_COVERAGE},
                )
            ],
            dict(thematic_opts),
        )
    )
    out.append(
        (
            "thematic_desk_salience_freshness",
            [
                _row(
                    analyst_id="escalation",
                    salience=0.55,
                    extra={
                        "_thematic_coverage": _DESK_COVERAGE,
                        "_freshness": _FRESHNESS,
                    },
                )
            ],
            dict(thematic_opts),
        )
    )
    return out


_SCENARIOS = _scenarios()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,rows,options", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
async def test_run_prompt_byte_identical_to_pre_refactor_splice(
    name: str,
    rows: list[dict[str, Any]],
    options: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt production sends == the pre-refactor splice, byte-for-byte."""
    spy = _BlockSpy()
    spy.install(monkeypatch)
    llm = _CapturingLLM()

    await synth._run(
        rows,
        options,
        llm=llm,
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused-global",
    )

    produced = llm.user_prompt
    expected = _ref_splice(spy)
    assert produced == expected, (
        f"[{name}] composer prompt diverged from the pre-refactor splice.\n"
        f"--- produced ---\n{produced!r}\n--- expected ---\n{expected!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name,rows,options", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
async def test_run_prompt_chars_step_matches_prompt(
    name: str,
    rows: list[dict[str, Any]],
    options: dict[str, Any],
) -> None:
    """The ``plan`` trace step's ``prompt_chars`` still equals the prompt length.

    It is now read off the assembler's shared accounting rather than
    ``len(user_prompt)``; this pins the two to the same number.
    """
    llm = _CapturingLLM()
    result = await synth._run(
        rows,
        options,
        llm=llm,
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused-global",
    )
    plan = next(
        s for s in result.intermediate_steps if s.get("phase") == "plan"
    )
    assert plan["prompt_chars"] == len(llm.user_prompt)


@pytest.mark.asyncio
async def test_scenarios_actually_exercise_every_block() -> None:
    """Coverage guard: the matrix must actually render all eight blocks.

    Without this, a scenario set that silently stopped triggering (say) the
    aperture block would make the equivalence suite vacuously green.
    """
    import pytest as _pytest

    rendered: set[str] = set()
    for _name, rows, options in _SCENARIOS:
        mp = _pytest.MonkeyPatch()
        try:
            spy = _BlockSpy()
            spy.install(mp)
            llm = _CapturingLLM()
            await synth._run(
                rows,
                options,
                llm=llm,
                max_tokens=512,
                temperature=0.2,
                system_prompt="unused-global",
            )
            for fn_name in _BLOCK_FNS:
                val = spy.one(fn_name)
                if val:
                    rendered.add(fn_name)
        finally:
            mp.undo()
    assert rendered == set(_BLOCK_FNS), (
        "scenario matrix does not exercise every prompt block; missing: "
        f"{sorted(set(_BLOCK_FNS) - rendered)}"
    )


@pytest.mark.asyncio
async def test_world_prompt_block_order_is_freshness_salience_findings() -> None:
    """The emergent ordering the collapse had to preserve."""
    rows = [
        _row(
            analyst_id="region_composition",
            salience=0.95,
            extra={"_region_coverage": _GAP_COVERAGE, "_freshness": _FRESHNESS},
        )
    ]
    llm = _CapturingLLM()
    await synth._run(
        rows,
        {"composition": True, "analyst_id": "world_assessor"},
        llm=llm,
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused-global",
    )
    p = llm.user_prompt
    i_fresh = p.index("FRESHNESS ADVISORY")
    i_sal = p.index("SALIENCE ORDERING")
    i_findings = p.index("First-order findings to synthesize")
    i_region = p.index("REGION COVERAGE")
    i_aperture = p.index("APERTURE")
    assert i_fresh < i_sal < i_findings < i_region < i_aperture
