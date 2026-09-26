# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The composition PROMPT ASSEMBLY — "what is this turn shown", in one place.

Extracted from ``meta_findings_synthesizer`` (2026-09-06) at the seam the
module-size gate had named twice: ``_run`` splits at the PROMPT-ASSEMBLY
boundary, and the ``_PromptBlockAssembler`` splice plus its eleven
``_blocks.add`` calls are one cohesive unit — the ONLY part of ``_run`` whose
job is deciding what text the model is handed. Under the assembly / rollup
regimes it is on the branch that does not send, which is precisely why it can
move without touching the deterministic path.

What lives here
~~~~~~~~~~~~~~~

* :class:`_PromptBlockAssembler` — the C-4 ordered, budget-accounted splice
  (moved verbatim; its own comment carries the two preserved asymmetries).
* :func:`assemble_composition_prompt` — ``_run``'s ``--- PLAN ---`` block:
  the base render, the R2 input-contradiction detection, the eleven guarded
  block splices in their established order, and — on :class:`PromptAssembly` —
  the by-products the sections BELOW the block read: the assembled turn and its
  char count, the claims-by-ref map and the detected contradictions, the
  coverage ledger, the evidence window and the continuity start ordinal.
* The three block renderers with no reader outside that splice —
  :func:`_render_freshness_advisory_block`, :func:`_render_contested_block`,
  :func:`_render_contested_absent_line` — and :func:`_verified_claim_texts`,
  the R2 ledger projection that feeds the detection.

Everything here is imported ONE WAY by ``meta_findings_synthesizer`` and
RE-EXPORTED from it, so ``synth._PromptBlockAssembler``,
``synth._render_contested_block`` and every other historical name resolves
unchanged for every importer and every test.

Why the renderers are INJECTED
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The nine tracked block renderers are NOT called through this module's globals.
They arrive as a :class:`PromptRenderers` bundle that ``_run`` constructs at
the call site from ITS OWN module namespace — so each name is still looked up
in ``meta_findings_synthesizer``'s globals at call time, exactly as the inline
``lambda:`` closures did before the move.

That is not decoration. ``tests/data_pkg/test_composer_prompt_block_equivalence
.py`` proves byte identity against the pre-C-4 ad-hoc splice by wrapping every
name in its ``_BLOCK_FNS`` list with ``monkeypatch.setattr(synth, name, spy)``
and reconstructing the prompt from the observed renderer outputs. Resolving the
renderers here instead would make those patches invisible and quietly turn the
load-bearing proof into a proof about nothing. Injection keeps the resolution
path — and therefore the proof — byte-for-byte what it was.

The bundle is also what keeps the splice LAZY: ``add`` invokes the callable
only when the guard passes, so a renderer that is only valid under its guard is
never reached, exactly as before.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ._llm_budget import CHARS_PER_TOKEN
from .claim_contradiction import detect_contradictions
from .composition_window import (
    build_coverage_ledger,
    build_world_coverage_ledger,
    evidence_window_span,
    max_head_age_hours,
)

# The SYNTHESIZER's logger name, not this module's — the same choice
# ``composition_coercion`` made when it was extracted for the same reason.
# ``meta.composition.input_contradictions`` is emitted from moved code, and a
# pure refactor that silently re-attributed a live log line to a new logger
# would break every filter and grep pointed at it. The line is byte-identical,
# including who it says wrote it.
logger = logging.getLogger("legba.data.analysts.meta_findings_synthesizer")


# ---------------------------------------------------------------------------
# R2 — the verified-claim projection the contradiction check reads
# ---------------------------------------------------------------------------


def _verified_claim_texts(row: Mapping[str, Any]) -> list[str]:
    """The SUPPORTED claim texts from this input's faithfulness verify ledger.

    R2. The ledger (``data.verification.claim_verdicts``) has been persisted per
    finding since P2-4 and read by nothing but the UI; the composition gather now
    projects it (``read_other_analyst_findings``'s lateral). Only ``supported``
    rows are returned — a contradiction between two claims the verify pass already
    rejected is not news, and building a tension block out of failed claims would
    hand the composition our own errors as evidence.

    Tolerates the two shapes asyncpg hands back (parsed list, or a JSON string)
    and every malformed row in between; a row it cannot read contributes nothing.
    """
    raw = row.get("claim_verdicts")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("verdict") != "supported":
            continue
        text = entry.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text.strip())
    return out


# ---------------------------------------------------------------------------
# Block renderers with no reader outside the splice
# ---------------------------------------------------------------------------


def _render_freshness_advisory_block(advisory: Sequence[Mapping[str, Any]]) -> str:
    """Render the compact per-target stale-root advisory (F-1) for the prompt.

    Directive, not decorative: the model is told to DEMOTE any framing resting on
    the superseded reading. Empty advisory → empty string (no block, no data)."""
    if not advisory:
        return ""
    lines = [
        "FRESHNESS ADVISORY (compose-time re-resolution):",
        "Since the findings below were composed, these underlying assessments were",
        "SUPERSEDED by a materially different CURRENT head. Do NOT lead with, or",
        "over-weight, any framing that rests on the superseded reading — prefer the",
        "current reading and, if the earlier one shaped the inputs, say so plainly.",
    ]
    for s in advisory:
        unit = str(s.get("unit") or "unit")
        target = str(s.get("target") or "")
        tgt = f" [{target}]" if target else ""
        lines.append(
            f'- {unit}{tgt}: "{s.get("old_title", "")}" (confidence '
            f'{s.get("old_confidence")}) → SUPERSEDED by "{s.get("new_title", "")}" '
            f"(confidence {s.get('new_confidence')}) at {s.get('superseded_at')}."
        )
    return "\n".join(lines)


def _render_contested_block(groups: Sequence[Mapping[str, Any]]) -> str:
    """Render the open contested groups into the appended CONTESTED FACTS block.

    Each group is labelled with a STABLE ``[[contested:<contention_id>]]`` marker
    naming BOTH surfaced value clusters (winner flagged). Empty ``groups`` → ``""``
    (the block is simply absent; the world prompt's contested rule is then inert).
    """
    if not groups:
        return ""
    lines = [
        "",
        "CONTESTED FACTS (open disputes — surface BOTH sides, mark "
        "[[contested:<id>]], never pick a side the arbiter did not surface):",
    ]
    for g in groups:
        sides = "; ".join(
            (
                f"{v['value_key']}"
                + (" [arbiter-surfaced winner]" if v.get("surfaced_winner") else "")
                + (
                    f" (score={v['arbiter_score']:.2f})"
                    if v.get("arbiter_score") is not None
                    else ""
                )
            )
            for v in g.get("values", [])
        )
        lines.append(
            f"[[contested:{g['contention_id']}]] "
            f"subject={g['subject_key']} predicate={g['predicate_key']} :: {sides}"
        )
    return "\n".join(lines)


def _render_contested_absent_line(
    *, considered: int, suppressed: int, floor: float,
) -> str:
    """The HONEST fallback for the world CONTESTED FACTS block when the read
    attempted the contention gather but NOTHING cleared
    :data:`CONTENTION_SCORE_FLOOR_DEFAULT` (or there was nothing open at all).

    Never render nothing silently here: a silently-absent block is
    indistinguishable from a dead/broken read to anyone reading the prompt or
    the finding's envelope — this line says plainly that the mechanism ran
    and what it found, mirroring the APERTURE block's always-state-it
    posture (P2 gallery Observation 3 on the world capture) rather than the
    prior CONTESTED FACTS behavior of simply vanishing.
    """
    if considered <= 0:
        return "CONTESTED FACTS: no open fact disputes this cycle."
    return (
        "CONTESTED FACTS: "
        f"{considered} open dispute(s) considered this cycle; "
        f"{suppressed} did not clear the arbiter-score floor ({floor:.2f}) — "
        "no contested-fact block above threshold this cycle."
    )


# ---------------------------------------------------------------------------
# THE ONE prompt-block interface (C-4)
# ---------------------------------------------------------------------------
# The composition user turn is a BASE render (``_render_user_prompt``) plus
# EIGHT optional blocks, each with its own guard, its own join separator, and its
# own POSITION — appended after the findings (evidence sections) or prepended
# ahead of them (directives the model must read first). That assembly was eight
# ad-hoc ``user_prompt = user_prompt + "\n" + block`` statements whose ORDER,
# separators and empty-checks were all load-bearing but implicit; the final order
# [freshness -> salience -> base -> periphery -> contested -> region -> aperture
# -> desk] only emerged from the interleaving of appends and prepends.
#
# It is ONE ordered walk here, with shared char/token accounting so every block's
# footprint is measured in one place instead of nowhere.
#
# BYTE-IDENTICAL BY CONSTRUCTION: blocks are applied in the same order with the
# same separators and the same guards, and each renderer is invoked LAZILY (only
# when its guard passes) exactly as before. Two asymmetries are preserved
# deliberately rather than "cleaned up":
#   * the CONTESTED block appends WITHOUT an empty-render check (every other
#     block skips an empty render) — hence ``require_non_empty=False``. It is
#     unreachable today (its renderer returns "" only for empty groups, and its
#     guard already requires non-empty groups) but it is not this lane's call to
#     change what happens if that ever stops holding.
#   * PREPENDS run AFTER appends, and salience prepends BEFORE freshness, which
#     is what leaves freshness first in the final turn.

#: Rough token estimate divisor — THE shared chars/4 convention
#: (``_llm_budget.CHARS_PER_TOKEN``), no tokenizer on the hot path. Aliased
#: rather than re-spelled so the composition and unit estimates cannot drift.
_PROMPT_CHARS_PER_TOKEN = CHARS_PER_TOKEN

_BLOCK_APPEND = "append"
_BLOCK_PREPEND = "prepend"


class _PromptBlockAssembler:
    """Ordered, budget-accounted assembly of the composition prompt blocks.

    Usage is declarative: construct with the base render, then ``add`` each block
    in its established order. ``add`` is a no-op when the block's guard is false,
    so the caller keeps its existing conditions in one readable place and the
    renderer stays lazy.
    """

    __slots__ = ("_text", "_ledger")

    def __init__(self, base: str) -> None:
        self._text = base
        # (block name, rendered chars) in APPLICATION order; the base is first.
        self._ledger: list[tuple[str, int]] = [("base", len(base))]

    def add(
        self,
        name: str,
        render: Any,
        *,
        when: Any,
        position: str,
        separator: str,
        require_non_empty: bool = True,
    ) -> None:
        """Render and splice one optional block.

        ``render`` is a zero-arg callable invoked ONLY when ``when`` is truthy —
        preserving the original lazy evaluation (several renderers are only valid
        under their guard). ``require_non_empty=False`` splices even an empty
        render, separator included.
        """
        if not when:
            return
        block = render()
        if require_non_empty and not block:
            return
        if position == _BLOCK_PREPEND:
            self._text = block + separator + self._text
        elif position == _BLOCK_APPEND:
            self._text = self._text + separator + block
        else:  # pragma: no cover - programming error
            raise ValueError(f"unknown prompt-block position: {position!r}")
        self._ledger.append((name, len(block)))

    @property
    def prompt(self) -> str:
        """The assembled user turn."""
        return self._text

    @property
    def total_chars(self) -> int:
        """Total assembled size — what the ``plan`` trace step records."""
        return len(self._text)

    @property
    def est_tokens(self) -> int:
        """Cheap chars/4 estimate of the assembled turn's input footprint."""
        return (len(self._text) + _PROMPT_CHARS_PER_TOKEN - 1) // _PROMPT_CHARS_PER_TOKEN

    @property
    def block_ledger(self) -> list[tuple[str, int]]:
        """Per-block (name, chars) in application order — the shared accounting."""
        return list(self._ledger)


# ---------------------------------------------------------------------------
# The injected renderer bundle + the assembly result
# ---------------------------------------------------------------------------



#: W-2b — the two grains a coverage roster can be counted at.
#: ``analyst`` is the DESK grain (one unit = one source analyst) and is the
#: country tier's, unchanged. ``world_unit`` is the world's: one unit is a
#: country DESK (32 rows sharing one ``analyst_id``, told apart by target) or a
#: target-less thematic LANE. Spelled as constants rather than a bool because a
#: third tier asking the same question later should read as a value, not as
#: ``not is_world``.
LEDGER_GRAIN_ANALYST: str = "analyst"
LEDGER_GRAIN_WORLD_UNIT: str = "world_unit"

@dataclass(frozen=True)
class PromptRenderers:
    """The block renderers, resolved by the CALLER in its own namespace.

    Every field is the callable ``_run`` looked up by bare name at the call
    site, which is what keeps ``monkeypatch.setattr(synth, "_render_*", ...)``
    effective through this module (see the module docstring). Fields are named
    for the BLOCK, not for the historical function, so the splice below reads as
    the ordered walk it is.
    """

    user_prompt: Callable[..., str]
    periphery: Callable[..., str]
    continuity: Callable[..., str]
    tension: Callable[..., str]
    contested: Callable[..., str]
    contested_absent: Callable[..., str]
    region_coverage: Callable[..., str]
    world_aperture: Callable[..., str]
    desk_coverage: Callable[..., str]
    coverage_ledger: Callable[..., str]
    evidence_window: Callable[..., str]
    salience_lead: Callable[..., str]
    freshness_advisory: Callable[..., str]


@dataclass(frozen=True)
class PromptAssembly:
    """The assembled turn plus the by-products ``_run`` carries forward.

    Everything on here was a local of ``_run``'s ``--- PLAN ---`` block that a
    LATER section reads: the two contradiction values feed the ``eval`` stamp and
    its trace step, the coverage ledger and its roster feed ``build_assembly``,
    the evidence window is stamped on the envelope, and the continuity start
    ordinal is reported on the ``continuity`` step. Returning them explicitly is
    what lets the block move without the rest of ``_run`` changing a line.
    """

    prompt: str
    total_chars: int
    claims_by_ref: dict[int, list[str]]
    input_contradictions: list[Any]
    head_ledger: Any
    evidence_window: Any
    continuity_start_ordinal: int


def assemble_composition_prompt(
    *,
    renderers: PromptRenderers,
    sliced: Sequence[Mapping[str, Any]],
    contributing_analysts: Sequence[str],
    is_composition: bool,
    periphery_sel: Sequence[Mapping[str, Any]],
    periphery_rows: Sequence[Mapping[str, Any]],
    tier_floor: float | None,
    prior_row: Mapping[str, Any] | None,
    register_situations: Sequence[Mapping[str, Any]],
    ledger_entries: Sequence[Mapping[str, Any]],
    contention_attempted: bool,
    contention_groups: Sequence[Mapping[str, Any]],
    contention_considered: int,
    contention_suppressed: int,
    contention_floor: float,
    region_coverage: Sequence[Mapping[str, Any]],
    world_composition: bool,
    desk_coverage: Sequence[Mapping[str, Any]],
    horizon_hours: int | None,
    ledger_roster: Sequence[str],
    # W-2b — which grain the roster is counted at. Defaulted to the DESK grain,
    # so every existing caller and the whole country tier are byte-identical.
    ledger_grain: str = LEDGER_GRAIN_ANALYST,
    freshness_advisory: Sequence[Mapping[str, Any]],
) -> PromptAssembly:
    """``_run``'s PLAN phase: render the user turn the composition is shown.

    Moved verbatim from ``meta_findings_synthesizer._run`` — the base render,
    the R2 contradiction detection over the SHOWN ordinals, and the eleven
    guarded ``add`` calls in their established order. The block comments are the
    originals; each says why its block sits where it sits, and that ordering is
    load-bearing (the equivalence proof replays it).
    """
    user_prompt = renderers.user_prompt(
        sliced, contributing_analysts, include_source_ids=is_composition
    )
    # R2: compare the shown findings' VERIFIED claims against EACH OTHER before
    # composing. Keyed on the rendered ordinal (i, 1-based) so a detected pair's
    # handles are the ones the model can actually cite. Non-composition paths and
    # rows whose critique carried no ledger contribute nothing — the check is
    # silently inert rather than absent, and ``contradictions_checked`` below
    # records which of those two it was.
    _claims_by_ref: dict[int, list[str]] = {}
    if is_composition:
        for _i, _row in enumerate(sliced, start=1):
            _verified = _verified_claim_texts(_row)
            if _verified:
                _claims_by_ref[_i] = _verified
    _input_contradictions = (
        detect_contradictions(_claims_by_ref) if _claims_by_ref else []
    )
    if _input_contradictions:
        logger.warning(
            "meta.composition.input_contradictions n=%d refs=%s — the input set "
            "asserts incompatible states; the tension block is being rendered",
            len(_input_contradictions),
            [(c.a_ref, c.b_ref, c.group) for c in _input_contradictions],
        )
    # The EIGHT optional prompt blocks, through the ONE budgeted interface
    # (:class:`_PromptBlockAssembler`). Order, separators and guards are the
    # established ones — see the class comment for the two preserved asymmetries
    # (contested splices without an empty-check; prepends run after appends, so
    # the final turn reads [freshness → salience → findings → …]).
    _blocks = _PromptBlockAssembler(user_prompt)
    # C-TIER: the PERIPHERY section renders APPENDED to (never interleaved
    # with) the basis blocks, under its explicit delimiter + hedge/conflict
    # rules, with ordinals continuing the basis numbering. Empty periphery ⇒
    # no section ⇒ the prompt is byte-identical to the untiered render.
    _blocks.add(
        "periphery",
        lambda: renderers.periphery(
            periphery_sel, start_ordinal=len(sliced) + 1, floor=tier_floor
        ),
        when=is_composition and periphery_sel,
        position=_BLOCK_APPEND,
        separator="\n\n",
    )
    # CONTINUITY: appended DIRECTLY after the periphery so the rendered order and
    # the ordinal order are the same walk — basis 1..K, periphery K+1..K+P,
    # continuity K+P+1.. — and a reader of either the prompt or ``data.citations``
    # sees one flat, contiguous [[ref:N]] space. It sits ahead of the coverage /
    # contested blocks because those are DIRECTIVES about the current slice,
    # while this is EVIDENCE that carries its own citable handles.
    _continuity_start_ordinal = len(sliced) + len(periphery_sel) + 1
    _blocks.add(
        "continuity",
        lambda: renderers.continuity(
            prior_row,
            register_situations,
            start_ordinal=_continuity_start_ordinal,
            ledger=ledger_entries,
        ),
        when=is_composition
        and (prior_row is not None or register_situations or ledger_entries),
        position=_BLOCK_APPEND,
        separator="\n\n",
    )
    # R2: the COMPUTED tension. Appended right after continuity and ahead of the
    # contested (fact-plane) block, because the two are the same idea at two
    # altitudes — this one is disagreement between the desk's own FINDINGS, that
    # one between the world's facts — and a reader should meet them together.
    # ``_input_contradictions`` was computed over the SAME ``sliced`` list the
    # findings block rendered, so its [[ref:N]] handles are the shown ordinals.
    _blocks.add(
        "input_contradictions",
        lambda: renderers.tension(_input_contradictions),
        when=is_composition and bool(_input_contradictions),
        position=_BLOCK_APPEND,
        separator="\n\n",
    )
    _blocks.add(
        "contested",
        lambda: (
            renderers.contested(contention_groups)
            if contention_groups
            else renderers.contested_absent(
                considered=contention_considered,
                suppressed=contention_suppressed,
                floor=contention_floor,
            )
        ),
        when=contention_attempted,
        position=_BLOCK_APPEND,
        separator="\n",
        require_non_empty=False,
    )
    _blocks.add(
        "region_coverage",
        lambda: renderers.region_coverage(region_coverage),
        when=region_coverage,
        position=_BLOCK_APPEND,
        separator="\n",
    )
    # B0-10 (MASTER_PLAN 2026-07-10) — APERTURE honesty for the WORLD compose:
    # the sample is the registered desk roster (operator-chosen), not global
    # coverage. Rendered ALWAYS (not just on gaps) so the world prose names its
    # own bounds — faithfulness verify is silent about what was never collected,
    # so the aperture must be stated, not implied (review W8).
    _blocks.add(
        "world_aperture",
        lambda: renderers.world_aperture(region_coverage),
        when=world_composition and region_coverage,
        position=_BLOCK_APPEND,
        separator="\n",
    )
    _blocks.add(
        "desk_coverage",
        lambda: renderers.desk_coverage(desk_coverage),
        when=desk_coverage,
        position=_BLOCK_APPEND,
        separator="\n",
    )
    # FRAME-1 (§3 + §4.2/4.3): the HEAD WINDOW block — the horizon this read
    # admitted heads under, the staleness the model is obliged to disclose, and
    # the deterministic per-unit COVERAGE LEDGER (in basis / below floor / not
    # verified / no read at all). A DIRECTIVE, appended beside the other
    # coverage blocks and carrying NO [[ref:N]] ordinal: it is the run's own
    # bookkeeping about what it was shown, and minting an ordinal for
    # bookkeeping would put a fabricated anchor in the citation space.
    #
    # The ledger half needs a DENOMINATOR — the subscription-resolved unit roster
    # on ``options['source_analyst_ids']``. Without it we could only enumerate the
    # units that DID arrive, which is precisely the blindness the ledger exists to
    # remove, so the ledger is simply omitted (and the block degrades to the horizon
    # + staleness lines). Roster-based coverage is per-COUNTRY only: region / world
    # / thematic runs carry their own coverage blocks over their own denominators.
    # D-2b names that denominator ONCE in ``_run`` and persists it beside the ledger
    # it built (``assembly.coverage_roster``) so D-3's ARM 4(a) diffs two arrays
    # rather than counting ``assembly_coverage_roster_absent``; an empty roster
    # yields an empty ledger, byte-identically to the guard this replaces.
    _head_ledger = (
        build_world_coverage_ledger(ledger_roster, sliced, periphery_rows)
        if ledger_grain == LEDGER_GRAIN_WORLD_UNIT
        else build_coverage_ledger(ledger_roster, sliced, periphery_rows)
    )
    _max_head_age = max_head_age_hours(sliced)
    _blocks.add(
        "head_window",
        lambda: renderers.coverage_ledger(
            _head_ledger,
            horizon_hours=horizon_hours,
            floor=tier_floor,
            max_age_hours=_max_head_age,
        ),
        when=is_composition and (horizon_hours is not None or _head_ledger),
        position=_BLOCK_APPEND,
        separator="\n",
    )
    # H4 — the EVIDENCE WINDOW: the real oldest/newest dates among the heads
    # consumed (``sliced``), computed once and reused below to stamp
    # ``data.evidence_window`` — one source of truth for prompt and envelope.
    # A DIRECTIVE like ``head_window`` above: the model COPIES the two dates
    # rather than deriving them by scanning every block (the arithmetic that
    # produced the self-inconsistent "01:40 UTC" stamp against heads produced
    # 16-17 UTC).
    _evidence_window = evidence_window_span(sliced) if is_composition else None
    _blocks.add(
        "evidence_window",
        lambda: renderers.evidence_window(_evidence_window),
        when=is_composition and _evidence_window,
        position=_BLOCK_APPEND,
        separator="\n",
    )
    # S-2b: PREPEND the salience-lead directive (composition only) so the model
    # leads by CONSEQUENCE, not by which matter more blocks happen to mention.
    # Prepended BEFORE the freshness block below, so the final order is
    # [freshness → salience → findings] — freshness (demote stale) stays first.
    _blocks.add(
        "salience_lead",
        lambda: renderers.salience_lead(sliced),
        when=is_composition,
        position=_BLOCK_PREPEND,
        separator="\n\n",
    )
    # F-1: PREPEND the freshness advisory (a directive: demote/caveat any framing
    # that rests on a since-superseded reading) so the model reads it BEFORE the
    # findings — the earliest, highest-priority instruction in the user turn.
    _blocks.add(
        "freshness_advisory",
        lambda: renderers.freshness_advisory(freshness_advisory),
        when=freshness_advisory,
        position=_BLOCK_PREPEND,
        separator="\n\n",
    )
    return PromptAssembly(
        prompt=_blocks.prompt,
        # Same number as ``len(prompt)`` — read off the shared block accounting
        # so the assembler is the one place prompt size is known.
        total_chars=_blocks.total_chars,
        claims_by_ref=_claims_by_ref,
        input_contradictions=_input_contradictions,
        head_ledger=_head_ledger,
        evidence_window=_evidence_window,
        continuity_start_ordinal=_continuity_start_ordinal,
    )


__all__ = [
    "PromptAssembly",
    "PromptRenderers",
    "_BLOCK_APPEND",
    "_BLOCK_PREPEND",
    "_PROMPT_CHARS_PER_TOKEN",
    "_PromptBlockAssembler",
    "_render_contested_absent_line",
    "_render_contested_block",
    "_render_freshness_advisory_block",
    "_verified_claim_texts",
    "assemble_composition_prompt",
]
