# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D-3 — THE ASSEMBLY ARMS: four deterministic auditors over ``assembly.v1``.

THE DEMOTION, in one paragraph. The composition tier stops WRITING prose about
its inputs and starts ASSEMBLING them: every world-claim sentence in the body is
a verbatim span of an origin desk head, carrying that head's id and the byte
offsets it was cut from. The assembler self-checks each span AT CONSTRUCTION and
raises rather than emitting one that does not resolve (D-1 §3.6). These four
arms are the INDEPENDENT AUDIT of that construction, run over the PUBLISHED
payload by the same verify pass that grades everything else.

WHAT THAT MAKES THEM, and the distinction is the whole design (ratified F-12).
**They are an audit, not a withhold.** Verify runs POST-PERSIST — there is no
"do not publish" branch for a finding anywhere in this tree, which is precisely
why D-1 §3.6 put the real enforcement at ASSEMBLE time, where a publish decision
already lives. So an arm here can only ever do three things: fold the violation
into the faithfulness denominator, stamp it HARD, and SAY SO LOUDLY. It does not
gate, it does not suppress, and it does not force a score.

That is not a weakness of the arms; it is what they are for. **A deterministic
arm reading anything but 1.0 is not a bad finding — it is a BROKEN CONSTRUCTOR.**
The assembler promised these spans resolve. If one does not, no model failed: a
generator did, and the right response is a page, not a demotion. Every arm below
therefore logs at ERROR with the offending ids, and the counters are the
alert-worthy signal. The enforcing half — the score cap, the persisted
``hard_fail_count``, the suppression seam that does not exist — is D-3b, and
this is stated in the stamp's own lineage entry rather than in a comment, per
D-1 §3.6's instruction not to ship labels and call it a gate.

INERT BY CONSTRUCTION, which is the second load-bearing property. Every arm
routes off :func:`is_assembly` — ``schema == "assembly.v1"`` **AND**
``regime != "legacy"``. A unit finding has no assembly block; a flag-off
composition has ``{"schema": "assembly.v1", "regime": "legacy"}`` and nothing
behind it, because §5.2 puts the A/B label on every row from D-2's merge. Both
decline, and both decline SILENTLY: no counter, no span, no log line. This is
the same shape ``composition_integrity`` takes one brick over — an arm that
cannot route is byte-identical for every caller — and it is proven the same way,
by a replay over the live fleet that adds zero spans.

THE ARMS READ THE PAYLOAD, NEVER THE RENDERED BODY, and this is a correctness
requirement rather than a preference. D-2's renderer passes quoted text through
``composition_window._defuse_child_ref_markers`` (``[[ref:3]]`` →
``(child ref 3)``) so a span cut from a lower COMPOSITION tier cannot collide
with this tier's ordinal space. The canonical text is ``spans[].text`` in the
payload, byte-identical to the origin; the BODY is a render of it and is not.
An ARM 1 that byte-matched the body would false-fire on every defused marker.
``fold`` therefore takes no ``body`` argument at all — the absence of that
parameter is the guard, and a test pins it.

────────────────────────────────────────────────────────────────────────────
THE FOUR ARMS (D-1 §3.1–§3.4). All deterministic. All HARD.

ARM 1 — QUOTE FIDELITY. Every ``blocks[].spans[]`` must resolve into its origin
head's captured body: the sha must match, the byte offsets must cut exactly the
span's text under the fold, and the text must be contained. Four classes, in
PRECEDENCE order, because an earlier one explains a later one:
``quote_origin_truncated`` (the capture window does not reach the offsets — see
P0c below) > ``quote_origin_drift`` (the body changed, or the span's origin is
not the block's origin) > ``quote_offset_mismatch`` (the bytes are elsewhere) >
``quote_not_contained`` (the text is not in that body at all).

  THE PREDECESSOR, and why this is an extension rather than a rewrite.
  ``composition_integrity.attribution_ungrounded_quote`` is structurally the
  same test and is already immune to MECH-6 (it folds on both sides). It has
  fired **0 times, ever** — not because compositions quote faithfully but
  because **82% of composition bodies contain no quotation mark at all**. It
  needed a population, and the assembly is that population. It stays exactly
  where it is, grading the legacy prose path; this arm grades the assembled one.

ARM 2 — SCOPE PRESERVATION. The M-8 class, moved from a judged rubric to a span
boundary rule. If the SOURCE SENTENCE the span was cut from bounds its negative
to a COLLECTION ("in this desk's collection"), the span must carry that bound.
A span that truncates away the qualifier and leaves *"Japan's information
environment"* is ``scope_truncated``. The mirror direction —
``scope_widened`` — is a payload DECLARING ``scope_tokens`` its own source
sentence does not contain, i.e. a reader told the negative was bounded when it
never was. The predicate is
``composition_integrity.has_collection_denominator_scope``, reused VERBATIM and
not rewritten: that reuse is what keeps the false-positive risk bounded, and
this class has false-positived three times, which is why both directions are
replayed.

ARM 3 — ATTRIBUTION EQUALITY. ``blocks[].desk`` / ``target_id`` /
``produced_at`` / ``finding_id`` must equal what the CITATION BRIDGE captured
for that origin head. Under the assembly these strings are GENERATED, so this
arm is a regression test on the generator, not a check on a model — which is
exactly what kills R3 §4.2's *"assessment attributed to a 'proliferation-watch
desk' that does not exist among the seven unit heads"*. There is no desk name to
resolve any more, so ``_resolve_head``'s unresolvable-desk counter becomes this
arm's zero-expectation.

ARM 4 — SELECTION HONESTY. (a) COVERAGE COMPLETENESS: the persisted ledger must
account for every declared ROSTER unit exactly once, with a status from the
closed set. The roster is the only honest denominator —
``build_coverage_ledger``'s own docstring says why: deriving it from the rows
that ARRIVED makes a missing unit invisible, which is the original defect.
(b) DROP DISCLOSURE: the ledger's arithmetic must close, every drop must carry a
why-class from the closed enum, and the ranked not-selected list must sit
strictly below the carried prefix.

  WHAT (a) AND (b) ARE BLIND TO, stated here rather than discovered later
  (D-1 §3.4(c)). Both operate over BLOCKS AND UNITS THAT EXIST. The
  frame-monopoly failure (§A.5) — evidence present in the slice, entity absent
  from the output entirely — produces no block and no coverage row, so neither
  arm can see it. Its detector is the ENTITY-COVERAGE FLOOR, it lives at the
  DESK, and it ships as P0e/R-0. **D-3 owns this interlock, not the build.**
  P0e is a co-requisite of the program, not a follow-on: without it the arms
  below certify a read whose largest possible defect is invisible to them.

────────────────────────────────────────────────────────────────────────────
TWO INPUTS THIS ARM SET NEEDED THAT THE ROW DID NOT CARRY — **BOTH LANDED IN
D-2b ON 2026-09-05** (``97ebaa28``). Kept here rather than deleted, because the
DECLINE PATH each one describes is still live for every row written before that
merge, and a silent decline is how a check dies:

  1. ``citations[].produced_at`` — the composition citation bridge captured
     ``marker · ordinal · ref_id · ref_kind · source · target_id · title ·
     evidence_text · effective_confidence · derived_from`` and **no timestamp**
     (verified live, 2026-09-03). D-2b adds it. Without it ARM 3 cannot decide
     ``attribution_date_mismatch``; it DECLINES and counts
     ``assembly_attribution_date_unverifiable`` rather than fabricating a pass.
  2. ``assembly.coverage_roster`` — the subscription-resolved
     ``options['source_analyst_ids']``, persisted beside the ledger it is the
     denominator of. D-2b persists it. Without it ARM 4(a) can still check
     duplication and status validity (both self-contained) but CANNOT check
     completeness; it counts ``assembly_coverage_roster_absent``.

NO ARM WIRING WAS REQUIRED BY EITHER: :func:`origin_records` and
:func:`_coverage_completeness` were written for the fields' arrival and read them
as they are. The pre-D-2b population — every assembly row of 2026-09-05 before
~21:30Z, including the 35 the verify-regime fix replayed — carries neither, so
both counters are non-zero on it and both checks stay counted-as-undecided,
never counted-as-passed. Those two counters falling to zero is how D-2b's
rollout is read off the receipts rather than assumed.

P0c — THE CAPTURE WINDOW, and why ``quote_origin_truncated`` exists at all.
``_build_composition_citation`` used to capture ``evidence_text = body[:3600]``
while the renderer showed up to 4000, so a span cut from chars 3600-4000 was a
FALSE hard reject (live sizing: max body 3,649 chars; 2 rows over 3,600; 0 over
4,000). Fixed landed before R4 T0: ``MAX_EVIDENCE_TEXT_CHARS`` now equals
``MAX_FULL_BODY_CHARS`` (both 4000, PRODUCER-side, D-2), pinned so they cannot
drift apart again. This arm still checks the gap rather than trusting the pin:
``spans[].origin.body_len`` is the FULL length, so a span reaching past the
captured prefix is ``quote_origin_truncated`` — a named, counted class — and the
sha check over an incomplete capture is DECLINED
(``assembly_quote_sha_unverifiable``), never passed. A body that somehow still
exceeds 4000 chars (none has, live) or a stale citation captured before this fix
would still be caught here, which is the point of an independent audit.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from collections import Counter
from typing import Any, Mapping, Sequence

from .composition_integrity import has_collection_denominator_scope
from .text_fold import fold_contains, fold_equal, normalize_for_match

logger = logging.getLogger(__name__)


def _verify():
    """Lazy accessor — this module is imported BY verify, so the edge runs one
    way at call time only (the ``composition_integrity`` pattern)."""
    from . import verify

    return verify


#: The payload version these arms grade. A bump on the producer side must bump
#: this and the arms must branch, per D-1 §1.1b — the ``absence.v4`` discipline.
ASSEMBLY_SCHEMA = "assembly.v1"

#: ``assembly.carried_origins`` — THE DEPTH-1 BRIDGE FOR A CARRIED BLOCK (P3-B).
#:
#: A world read's blocks are CARRIED from its country and thematic candidates
#: rather than cut from their bodies: the block object is the child's own, so
#: ``finding_id`` and ``spans[].origin.head_id`` name the DESK HEAD that wrote
#: the sentence, and the candidate the block came through is recorded beside it
#: as ``via_head_id``. That is what makes quote fidelity at the world tier the
#: same depth-1 test as everywhere else — and it means the row's own citation
#: bridge, which names the CANDIDATE, cannot resolve the origin. So the producer
#: republishes the child's citation for that head here, in citation shape, and
#: :func:`audit` reads it with :func:`origin_records` — the same reader, not a
#: second parser for the same object.
#:
#: DEFINED HERE, on the provenance side, and imported by the producer
#: (``analysts.assembly_carry``) — the direction every shared marker in this
#: tree takes (``kinds.ROLLUP_PAYLOAD_SCHEMA``), because a guard may not import
#: an analyst module to recognise what it is grading.
CARRIED_ORIGINS_KEY = "carried_origins"

# ---------------------------------------------------------------------------
# THE SIXTEEN REASONS. Spelled here, next to the argument for each, and merged
# into ``verify._FAIL_CLASS_BY_REASON`` — which stays THE table (one lookup, one
# drift guard), exactly as ``composition_integrity.FAIL_CLASSES`` is.
# ---------------------------------------------------------------------------

#: ARM 1 — the span's text does not occur in its origin head's captured body at
#: all, under the fold. The assembler claimed a quotation and there is none.
QUOTE_NOT_CONTAINED = "quote_not_contained"

#: ARM 1 — the text IS in the origin body, but not at the offsets the span
#: recorded. The quote is real and the PROVENANCE is wrong, which is worse than
#: a missing quote for the one thing spans exist to do: point at bytes.
QUOTE_OFFSET_MISMATCH = "quote_offset_mismatch"

#: ARM 1 — ``sha256(origin body) != span.origin.body_sha256``, or the span's
#: ``origin.head_id`` is not the block's own ``finding_id``. Either way the span
#: is anchored to a body that is not the one it says it is. Depth-1 is the
#: contract (D-1 §0.3: "never a path"), and this is the class that enforces it.
QUOTE_ORIGIN_DRIFT = "quote_origin_drift"

#: ARM 1 — the span's ``end`` reaches past the captured ``evidence_text``. P0c
#: (see the banner): DETECTABLE rather than silently mis-verified.
QUOTE_ORIGIN_TRUNCATED = "quote_origin_truncated"

#: ARM 2 — the source sentence bounded its negative to a COLLECTION and the span
#: does not carry the bound. THE JP MECHANISM, as a boundary rule instead of a
#: judged rubric: "No coordinated narrative appears in THIS DESK'S COLLECTION"
#: cut down to "No coordinated narrative appears in JAPAN'S INFORMATION
#: ENVIRONMENT". Abstention laundered into a world claim.
SCOPE_TRUNCATED = "scope_truncated"

#: ARM 2 — the span DECLARES ``scope_tokens`` its own source sentence does not
#: contain. The mirror error: a reader told the negative was collection-bounded
#: when the origin never bounded it. Both directions, because this class has
#: false-positived three times and the replay must cover the way it fails too.
SCOPE_WIDENED = "scope_widened"

#: THE ONE TOKEN ``scope_tokens`` CARRIES, and it is an IDENTIFIER rather than a
#: phrase. ``assembly_spans.py:286`` writes exactly this string when the injected
#: ``has_collection_denominator_scope`` fires on the span's source sentence, and
#: ``test_composition_assembly_d2.py:189`` pins it; the external-audit brick spells
#: the same constant at ``_external_audit_claims.SCOPE_TOKEN_COLLECTION_DENOMINATOR``.
#: It is restated here rather than imported because ``data.provenance`` must not
#: import ``data.analysts`` — the edge runs the other way — and the three sites are
#: held together by :func:`scope_preservation`'s test instead of by an import.
SCOPE_TOKEN_COLLECTION_DENOMINATOR = "collection_denominator"

#: P3 LANE A — THE CONTEXT ROLE, restated here for the same reason and under the
#: same discipline as the token above: ``data.provenance`` must not import
#: ``data.analysts``. ``assembly_spans.SPAN_ROLE_CONTEXT_BODY`` writes exactly
#: this string, and ``test_country_assessment_p3.py`` pins the two equal.
#:
#: WHAT IT IS. A COUNTRY block carries, beside its quoted lead, its origin desk
#: head in FULL — offsets ``[0:len(body)]``, the same digest, the same
#: construction gate. It exists so the fenced ``country_assessment`` voice can
#: argue ACROSS dimensions; it is never rendered into the record.
#:
#: WHY THE ARMS SKIP IT, and this is the part that must not be misread as a
#: convenience. A span spanning a whole body PASSES arm 1 by construction —
#: ``[0:len]`` of a body always equals that body. Auditing it therefore measures
#: nothing and, worse, PADS THE DENOMINATOR with a guaranteed pass per block: on
#: a 8-block country assembly the fidelity rate would move from 7/8 to 15/16 for
#: the same single real failure, which is a dilution of the one number D-1 §3.6
#: says must read 1.0 or page. Skipping by role keeps the arm's denominator
#: exactly the set of QUOTATIONS the record makes, which is what the arm claims
#: to measure. Arm 2 is unaffected either way — a context span declares no
#: ``scope_tokens`` — and it shares arm 1's ``(block, span)`` index space, so
#: the filter has to live in ONE accessor rather than at two call sites.
SPAN_ROLE_CONTEXT_BODY = "context_body"

#: ARM 3 — ``blocks[].desk`` is not the origin head's producer. R3 §4.2's
#: "proliferation-watch desk that does not exist among the seven unit heads",
#: made impossible by construction and then CHECKED anyway.
ATTRIBUTION_DESK_MISMATCH = "attribution_desk_mismatch"

#: ARM 3 — ``blocks[].produced_at`` is not the origin head's timestamp. An
#: attribution line dated wrong is a false currency claim about the evidence.
ATTRIBUTION_DATE_MISMATCH = "attribution_date_mismatch"

#: ARM 3 — ``blocks[].target_id`` is not the origin head's target. The
#: cross-target leak class, at the assembly grain.
ATTRIBUTION_TARGET_MISMATCH = "attribution_target_mismatch"

#: ARM 3 — the block's ``finding_id`` resolves to no citation at all, so nothing
#: on the page can be traced. Counted, never guessed: putting the wrong head
#: behind an attribution would manufacture the defect this arm exists to catch.
ATTRIBUTION_HEAD_UNRESOLVED = "attribution_head_unresolved"

#: ARM 4(a) — a declared roster unit has no coverage row. THE original defect:
#: the model inferred "gap" for a dimension it was never shown.
COVERAGE_UNIT_MISSING = "coverage_unit_missing"

#: ARM 4(a) — one unit, two coverage rows. Two answers to "was this covered?".
COVERAGE_UNIT_DUPLICATED = "coverage_unit_duplicated"

#: ARM 4(a) — a coverage status outside the closed set. An unknown status is an
#: unreadable ledger, and the ledger is what retires ``metadata_mismatch``.
COVERAGE_STATUS_INVALID = "coverage_status_invalid"

#: ARM 4(b) — the drop ledger's arithmetic does not close. The identity is
#: ``|derived_from| - |blocks| == |shown_not_carried| + |trimmed|`` (D-1 §1.3):
#: what was shown and not carried IS the drop count, and a ledger that does not
#: say so is publishing a number nobody can check.
DROP_COUNT_MISMATCH = "drop_count_mismatch"

#: ARM 4(b) — a drop carrying a why-class outside the closed enum. A fabricated
#: reason is worse than a published count (the ``invisible_heads`` doctrine).
DROP_WHY_UNKNOWN = "drop_why_unknown"

#: ARM 4(b) — a not-selected candidate ranked ABOVE a carried block. Selection
#: is a strict prefix of the order by construction (D-1 §1.4b), so this cannot
#: happen unless the constructor is broken — which is the point of checking it.
DROP_ORDER_VIOLATION = "drop_order_violation"

#: ALL SIXTEEN ARE HARD, and the reason is one sentence: a deterministic arm
#: over a generated payload cannot produce a "the model outran its evidence"
#: verdict. It can only produce "the record does not say what it says it says",
#: which is the house definition of hard verbatim — a claim its own cited source
#: contradicts. D-1 §3.6's verify row rules it in terms.
#:
#: HARD AND SOFT ARE STILL ARITHMETICALLY INDISTINGUISHABLE in this plane
#: (``verify.py:1019-1036``: "LABELS ONLY"), and this train does not change
#: that — see the module banner on D-3b. The label is what a future gate weighs
#: and what the UI badge reads today; it is not a score.
FAIL_CLASSES: dict[str, str] = {
    QUOTE_NOT_CONTAINED: "hard_fail",
    QUOTE_OFFSET_MISMATCH: "hard_fail",
    QUOTE_ORIGIN_DRIFT: "hard_fail",
    QUOTE_ORIGIN_TRUNCATED: "hard_fail",
    SCOPE_TRUNCATED: "hard_fail",
    SCOPE_WIDENED: "hard_fail",
    ATTRIBUTION_DESK_MISMATCH: "hard_fail",
    ATTRIBUTION_DATE_MISMATCH: "hard_fail",
    ATTRIBUTION_TARGET_MISMATCH: "hard_fail",
    ATTRIBUTION_HEAD_UNRESOLVED: "hard_fail",
    COVERAGE_UNIT_MISSING: "hard_fail",
    COVERAGE_UNIT_DUPLICATED: "hard_fail",
    COVERAGE_STATUS_INVALID: "hard_fail",
    DROP_COUNT_MISMATCH: "hard_fail",
    DROP_WHY_UNKNOWN: "hard_fail",
    DROP_ORDER_VIOLATION: "hard_fail",
}

#: Every counter these arms can bump, so the receipts are enumerable from code
#: rather than by grepping for ``bump(`` (the V-G8 fidelity rule: an attempt and
#: a survival must both be countable). The ``assembly_drops_*`` block is D-1
#: §3.4(b)'s "counted, not gated" — the drop numbers R4 publishes and R5 moves.
COUNTERS: tuple[str, ...] = (
    "assembly_arms_reads_audited",
    "assembly_arms_schema_unknown",
    "assembly_quote_spans_checked",
    "assembly_quote_spans_ok",
    "assembly_quote_not_contained",
    "assembly_quote_offset_mismatch",
    "assembly_quote_origin_drift",
    "assembly_quote_origin_truncated",
    "assembly_quote_origin_unresolved",
    "assembly_quote_sha_unverifiable",
    "assembly_scope_spans_checked",
    "assembly_scope_bounded_sentences",
    "assembly_scope_truncated",
    "assembly_scope_widened",
    "assembly_scope_token_unknown",
    "assembly_attribution_blocks_checked",
    "assembly_attribution_desk_mismatch",
    "assembly_attribution_date_mismatch",
    "assembly_attribution_target_mismatch",
    "assembly_attribution_head_unresolved",
    "assembly_attribution_date_unverifiable",
    "assembly_coverage_units_checked",
    "assembly_coverage_unit_missing",
    "assembly_coverage_unit_duplicated",
    "assembly_coverage_status_invalid",
    "assembly_coverage_roster_absent",
    "assembly_drop_count_mismatch",
    "assembly_drop_why_unknown",
    "assembly_drops_why_absent",
    "assembly_drop_order_violation",
    "assembly_drops_shown",
    "assembly_drops_carried",
    "assembly_drops_shown_not_carried",
    "assembly_drops_not_selected",
    "assembly_drops_trimmed",
    "assembly_drops_below_floor",
    "assembly_drops_no_head",
    "assembly_drops_invisible_heads",
    # 2026-09-05/1 — the receipt for :func:`regrade_to_arms`. One per audited
    # assembly-regime read, so "whose number is this?" is answerable from the
    # critique row instead of from the stamp date.
    "assembly_headline_regraded_to_arms",
    # P3-B — how many carried blocks resolved their DESK head through the
    # payload's own bridge rather than through the row's citations. The receipt
    # for the world tier's carry: it is 0 on every country read and every
    # pre-train row, and |carried blocks| on a healthy world read.
    "assembly_carried_origins_bridged",
)

#: The closed coverage-status set. DUPLICATED as literals from
#: ``composition_window.COVERAGE_*`` on purpose: ``data.provenance`` sits UNDER
#: ``data.analysts`` in the import graph, so importing the constants would close
#: a cycle. This is the ``verify._REGISTER_REF_KIND`` precedent, and it carries
#: the same obligation — a drift-guard test asserts the two sets are equal.
COVERAGE_STATUSES: tuple[str, ...] = (
    "in_basis",
    "below_floor",
    "unverified",
    "no_head_in_horizon",
)

#: The closed drop why-class enum (D-1 §1.7). Same duplication rule, same guard.
DROP_WHY_CLASSES: tuple[str, ...] = (
    "shown_not_selected",
    "not_selected",
    "cap_trimmed",
    "below_floor",
    "no_head_in_horizon",
    "superseded",
    "correlated_duplicate",
    "not_a_candidate",
)

#: The five LIST grains of the drop ledger, paired with the ``counts`` key each
#: must reconcile against. Order is the ledger's own.
_DROP_GRAINS: tuple[tuple[str, str], ...] = (
    ("shown_not_carried", "shown_not_carried"),
    ("not_selected", "not_selected"),
    ("trimmed", "trimmed"),
    ("below_floor", "below_floor"),
    ("no_head", "no_head"),
)

#: The ``drops.counts`` keys published verbatim as counters (§3.4(b)).
_DROP_COUNT_KEYS: tuple[str, ...] = (
    "shown",
    "carried",
    "shown_not_carried",
    "not_selected",
    "trimmed",
    "below_floor",
    "no_head",
    "invisible_heads",
)

#: Sentence terminators for the ARM 2 source-sentence re-derivation. Deliberately
#: crude and deliberately INCLUSIVE of the newline: a desk head's negatives live
#: in ``## What changed`` bullets as often as in prose, and a bullet is a
#: sentence for scope purposes.
_SENT_BREAK_RE = re.compile(r"[.!?\n]")

#: Bound on a detail string, mirroring the 400-char cut ``composition_integrity``
#: takes: a detail is evidence for a verdict, not a transcript.
_DETAIL_CHARS = 400


@dataclass(frozen=True)
class ArmFinding:
    """One violation. ``reason`` is the fail class; ``text`` is what the ledger
    row carries; ``detail`` is the evidence for the verdict, which for every arm
    here NAMES the ids and the bytes it convicts on (the V-D rule: a hard
    verdict must point at the thing it convicts on)."""

    reason: str
    text: str
    markers: list[int | str] = field(default_factory=list)
    detail: str | None = None


@dataclass
class ArmResult:
    """``(findings, counters)`` — every arm is PURE and returns both, so a caller
    can run one arm in a test without a report object.

    ``charged`` carries the ``(block index, span index)`` keys ARM 1 convicted,
    so ARM 2 can decline them. A span whose bytes do not resolve has no
    trustworthy source sentence, and charging it twice would cost the read two
    claims for one defect — the precedence rule ``composition_integrity``
    applies within its four arms, applied across two.
    """

    findings: list[ArmFinding] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    charged: set[tuple[int, int]] = field(default_factory=set)

    def bump(self, name: str, n: int = 1) -> None:
        if n:
            self.counters[name] = self.counters.get(name, 0) + n

    def extend(self, other: "ArmResult") -> None:
        self.findings.extend(other.findings)
        self.charged |= other.charged
        for k, v in other.counters.items():
            self.bump(k, v)


# ---------------------------------------------------------------------------
# READING THE PAYLOAD — total, never raises, never guesses.
# ---------------------------------------------------------------------------


#: The A/B label D-2 stamps on EVERY composition row from its merge (§5.2) —
#: ``"legacy"`` while ``LEGBA_COMPOSITION_ASSEMBLY`` is off, ``"assembly"`` when
#: on. It is the ONE field that makes the cutover visible inside a single stamp,
#: and it is why a ``schema`` key alone cannot be the route gate: a flag-off row
#: carries ``{schema, regime: "legacy"}`` and nothing else.
REGIME_LEGACY = "legacy"

#: The regime whose rows the arms GRADE rather than merely audit — see
#: :func:`is_grader_of_record`. Narrower than :func:`is_assembly` on purpose:
#: D-5's ``"rollup"`` also passes the route gate (it is an ``assembly.v1`` stamp
#: with no blocks) and must NOT have its headline rewritten by six ledger units
#: it never populated.
REGIME_ASSEMBLY = "assembly"


def is_assembly(assembly: Any) -> bool:
    """Is this an ASSEMBLED read the arms should audit? THE ROUTE GATE.

    False for ``None``, for a non-mapping, for a finding with no assembly block
    at all, for a FUTURE schema version (the arms branch on the version rather
    than grading a shape they were not written for), and — the case that needs
    stating — for a **LEGACY-REGIME row**.

    WHY THE REGIME IS PART OF THE GATE AND NOT AN AFTERTHOUGHT. §5.2 requires
    ``data.data.assembly.regime`` on EVERY composition row from D-2's merge, flag
    on or off, so the A/B boundary is legible inside one stamp. A flag-off row
    therefore carries ``{"schema": "assembly.v1", "regime": "legacy"}`` — a real
    ``assembly.v1`` key with no blocks, no coverage and no drops behind it. A
    gate keyed on ``schema`` alone would "audit" it: one
    ``assembly_arms_reads_audited``, a ``branch_scores`` entry and six ledger
    check units, on every legacy composition in the fleet. That is not an audit,
    it is noise on a row nobody assembled, and it would break §5.2's other
    requirement — that flag-off restores byte-identical legacy behaviour.

    So a legacy row is declined **silently and without a counter**, exactly like
    a unit finding. There is nothing to measure about a read that was never
    assembled, and a counter on every flag-off row would be a receipt for a
    non-event.

    A row whose ``regime`` is ABSENT is AUDITED. That direction is deliberate: a
    producer that forgets the label must not thereby switch the audit off. Only
    the explicit word ``"legacy"`` declines.
    """
    if not isinstance(assembly, Mapping):
        return False
    if str(assembly.get("schema") or "") != ASSEMBLY_SCHEMA:
        return False
    return str(assembly.get("regime") or "") != REGIME_LEGACY


def _blocks(assembly: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = assembly.get("blocks")
    if not isinstance(raw, (list, tuple)):
        return []
    return [b for b in raw if isinstance(b, Mapping)]


def _spans(block: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The block's QUOTATIONS — every span except the P3-A context body.

    THE ONE ACCESSOR both span arms read, so the ``(block_index, span_index)``
    pairs arm 1 charges and arm 2 skips index the same list. See
    :data:`SPAN_ROLE_CONTEXT_BODY` for why a context span is excluded rather
    than checked: it passes by construction, so checking it would dilute the
    denominator of the one number that is supposed to page at 0.999.
    """
    raw = block.get("spans")
    if not isinstance(raw, (list, tuple)):
        return []
    return [
        s for s in raw
        if isinstance(s, Mapping)
        and str(s.get("role") or "") != SPAN_ROLE_CONTEXT_BODY
    ]


def _ordinal(block: Mapping[str, Any]) -> int | str:
    n = block.get("ordinal")
    if isinstance(n, int):
        return n
    return str(n or "?")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def origin_records(citations: Any) -> dict[str, dict[str, Any]]:
    """``finding_id -> {evidence_text, source, target_id, produced_at, ordinal}``.

    The CITATION BRIDGE, keyed by the origin head id rather than by ordinal —
    which is the whole point of D-1 §0.3's span-carries-origin rule: the span
    names a HEAD, not a position, so a re-ordering can never silently re-point a
    quote. ``ref_id`` is that head id and is already on every composition
    citation today (verified live).

    Never raises: a malformed citation list yields ``{}`` and every arm then
    declines rather than convicting on an absence of evidence.
    """
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        ref = _str(entry.get("ref_id"))
        if not ref or ref in out:
            continue
        text = entry.get("evidence_text")
        out[ref] = {
            "evidence_text": text if isinstance(text, str) else "",
            "has_evidence_text": isinstance(text, str) and bool(text),
            "source": _str(entry.get("source") or entry.get("analyst_id")),
            "target_id": _str(entry.get("target_id")),
            # CAPTURED BY THE BRIDGE SINCE D-2b (2026-09-05) — the module banner
            # and this line said "not captured today", and that stopped being
            # true when D-2b landed ``citations[].produced_at`` on the producer.
            # No arm wiring was needed: this reader and ARM 3's date comparison
            # were written for the field's arrival and take it as it is.
            #
            # ROWS WRITTEN BEFORE D-2b STILL CARRY NOTHING HERE, and they are the
            # reason the decline stays: ARM 3 counts
            # ``assembly_attribution_date_unverifiable`` rather than convicting on
            # an absent field, so the pre-D-2b population is measured instead of
            # being either charged or silently passed. That counter falling to
            # zero is how D-2b's rollout gets read off the receipts.
            "produced_at": _str(entry.get("produced_at")),
            "ordinal": entry.get("ordinal"),
        }
    return out


# ---------------------------------------------------------------------------
# ARM 1 — QUOTE FIDELITY
# ---------------------------------------------------------------------------


def _byte_view(text: str) -> bytes:
    return text.encode("utf-8")


def quote_fidelity(
    assembly: Mapping[str, Any], origins: Mapping[str, Mapping[str, Any]]
) -> ArmResult:
    """ARM 1 — every span resolves into its origin head's captured body.

    Offsets are UTF-8 BYTE offsets into the FULL origin body and ``body_len`` is
    that body's length in bytes, so an over-cap origin is DETECTABLE
    (``end > len(captured)``) rather than silently mis-verified — D-1 §1.3b.
    Slicing bytes can split a codepoint; the decode replaces it, the fold cannot
    match it, and the span lands as ``quote_offset_mismatch``, which is the
    correct answer for offsets that do not sit on a character boundary.

    THE SHA IS NEVER FAKED. It is computed over the captured bytes, and the
    comparison only runs when the capture is COMPLETE
    (``len(captured) == body_len``). An incomplete capture counts
    ``assembly_quote_sha_unverifiable`` and the span is graded on offsets and
    containment alone — undecided is not the same as passed, and this counter is
    how the P0c window's closure gets measured rather than assumed.
    """
    res = ArmResult()
    for b_i, block in enumerate(_blocks(assembly)):
        ordinal = _ordinal(block)
        finding_id = _str(block.get("finding_id"))
        for s_i, span in enumerate(_spans(block)):
            res.bump("assembly_quote_spans_checked")
            finding = _grade_span(
                span, block_id=finding_id, ordinal=ordinal, origins=origins, res=res
            )
            if finding is None:
                res.bump("assembly_quote_spans_ok")
                continue
            res.charged.add((b_i, s_i))
            res.findings.append(finding)
    return res


def _grade_span(
    span: Mapping[str, Any],
    *,
    block_id: str,
    ordinal: int | str,
    origins: Mapping[str, Mapping[str, Any]],
    res: ArmResult,
) -> ArmFinding | None:
    """One span → an :class:`ArmFinding` or ``None``.

    The branch order IS the precedence order from the module banner, and it is
    that order because an earlier failure EXPLAINS a later one: there is no point
    telling an operator the offsets are wrong when the body underneath them
    changed, and none at all when the capture never reached those bytes.

    Every reason is named as a MODULE CONSTANT at its emission site rather than
    computed, because the AST drift guard resolves ``ast.Name`` against this
    module's globals — a reason assembled at runtime would be invisible to it.
    """
    text = _str(span.get("text"))
    origin = span.get("origin")
    if not isinstance(origin, Mapping):
        res.bump("assembly_quote_origin_drift")
        return ArmFinding(
            reason=QUOTE_ORIGIN_DRIFT,
            text=text[:2000],
            markers=[ordinal],
            detail=(
                "the span carries no origin anchor at all; depth-1 byte "
                "traceability is the assembly's whole contract"
            )[:_DETAIL_CHARS],
        )
    head_id = _str(origin.get("head_id"))
    # DEPTH-1, enforced. D-1 §0.3: the span's origin is ALWAYS the block's own
    # head — never a path through an intermediate composition.
    if block_id and head_id and head_id != block_id:
        res.bump("assembly_quote_origin_drift")
        return ArmFinding(
            reason=QUOTE_ORIGIN_DRIFT,
            text=text[:2000],
            markers=[ordinal],
            detail=(
                f"span origin head {head_id!r} is not the block's own origin head "
                f"{block_id!r}; a span may never point through an intermediate row"
            )[:_DETAIL_CHARS],
        )
    record = origins.get(head_id) or origins.get(block_id)
    if record is None or not record.get("has_evidence_text"):
        # ARM 3 charges this block ``attribution_head_unresolved``; charging it
        # here as well would cost the read two claims for one defect.
        res.bump("assembly_quote_origin_unresolved")
        return None

    body = str(record.get("evidence_text") or "")
    captured = _byte_view(body)
    start, end = _as_int(origin.get("start")), _as_int(origin.get("end"))
    body_len = _as_int(origin.get("body_len"))
    if start is None or end is None or start < 0 or end < start:
        res.bump("assembly_quote_offset_mismatch")
        return ArmFinding(
            reason=QUOTE_OFFSET_MISMATCH,
            text=text[:2000],
            markers=[ordinal],
            detail=(
                "span offsets are not a usable byte range: "
                f"start={origin.get('start')!r} end={origin.get('end')!r}"
            )[:_DETAIL_CHARS],
        )
    if end > len(captured):
        res.bump("assembly_quote_origin_truncated")
        return ArmFinding(
            reason=QUOTE_ORIGIN_TRUNCATED,
            text=text[:2000],
            markers=[ordinal],
            detail=(
                f"span ends at byte {end} of an origin body captured to only "
                f"{len(captured)} bytes (full body_len={body_len}); the capture "
                "window does not reach the quotation (P0c)"
            )[:_DETAIL_CHARS],
        )
    declared_sha = _str(origin.get("body_sha256")).lower()
    if declared_sha and body_len is not None and body_len == len(captured):
        actual = hashlib.sha256(captured).hexdigest()
        if actual != declared_sha:
            res.bump("assembly_quote_origin_drift")
            return ArmFinding(
                reason=QUOTE_ORIGIN_DRIFT,
                text=text[:2000],
                markers=[ordinal],
                detail=(
                    f"origin body sha256 is {actual[:16]}… and the span recorded "
                    f"{declared_sha[:16]}…; the body this span was cut from is "
                    "not the body on the row"
                )[:_DETAIL_CHARS],
            )
    else:
        res.bump("assembly_quote_sha_unverifiable")

    cut = captured[start:end].decode("utf-8", "replace")
    if fold_equal(cut, text):
        return None
    if not fold_contains(body, text):
        res.bump("assembly_quote_not_contained")
        return ArmFinding(
            reason=QUOTE_NOT_CONTAINED,
            text=text[:2000],
            markers=[ordinal],
            detail=(
                f"the span text does not occur anywhere in origin head "
                f"{head_id!r}'s body under the shared fold; bytes "
                f"[{start}:{end}] hold {cut[:120]!r}"
            )[:_DETAIL_CHARS],
        )
    res.bump("assembly_quote_offset_mismatch")
    return ArmFinding(
        reason=QUOTE_OFFSET_MISMATCH,
        text=text[:2000],
        markers=[ordinal],
        detail=(
            f"bytes [{start}:{end}] of origin head {head_id!r} hold {cut[:120]!r}, "
            f"not the span's own {text[:120]!r} — the quotation is real and its "
            "provenance is wrong"
        )[:_DETAIL_CHARS],
    )


# ---------------------------------------------------------------------------
# ARM 2 — SCOPE PRESERVATION
# ---------------------------------------------------------------------------


def source_sentence(body: str, start: int, end: int) -> str:
    """The sentence a span was cut from, RE-DERIVED from the origin body.

    D-1 §3.2 says the assembler records the sentence bounds and the arm
    re-derives them — deliberately, so the arm's denominator is the ORIGIN's own
    prose and not a number the constructor handed it. Byte offsets in, character
    bounds out: the prefix is decoded to find the character position, which is
    exact for any offset that sits on a character boundary and conservative for
    one that does not.
    """
    if not body:
        return ""
    raw = _byte_view(body)
    start = max(0, min(start, len(raw)))
    end = max(start, min(end, len(raw)))
    c_start = len(raw[:start].decode("utf-8", "ignore"))
    c_end = c_start + len(raw[start:end].decode("utf-8", "ignore"))
    left = 0
    for m in _SENT_BREAK_RE.finditer(body, 0, c_start):
        left = m.end()
    right = len(body)
    m = _SENT_BREAK_RE.search(body, max(c_end - 1, c_start))
    if m is not None:
        right = m.end()
    return body[left:right].strip()


def scope_preservation(
    assembly: Mapping[str, Any],
    origins: Mapping[str, Mapping[str, Any]],
    *,
    skip: set[tuple[int, int]] | None = None,
) -> ArmResult:
    """ARM 2 — a span may not cut away the denominator its sentence carried.

    BOTH DIRECTIONS, and the house rule that mandates it has caught this class
    three times:

      * ``scope_truncated`` — the SOURCE SENTENCE has a collection-denominator
        bound and the SPAN does not. The qualifier was cut away by the boundary.
      * ``scope_widened`` — the span DECLARES ``scope_tokens`` the source
        sentence does not contain. The payload claims a bound the origin never
        made, which reads to a consumer as a scoped negative and is not one.

    The predicate is ``composition_integrity.has_collection_denominator_scope``,
    reused VERBATIM. It is sharper than the shared scope lexicon by exactly two
    entries — "slice" and "window" — because a time bound answers WHEN a desk
    looked and a collection bound answers WHAT IT SEARCHED, and only the second
    is the qualifier a laundered absence deletes. Rewriting it here would be how
    the two drift apart.

    2026-09-05 — BOTH DIRECTIONS NOW USE THAT ONE PREDICATE, and the fix is what
    the docstring above already claimed. ``scope_tokens`` is an IDENTIFIER list,
    not a phrase list: D-2 stores the single token
    :data:`SCOPE_TOKEN_COLLECTION_DENOMINATOR` (``assembly_spans.py:286``, pinned
    by ``test_composition_assembly_d2.py:189``). SCOPE_WIDENED shipped testing it
    with ``fold_contains(sentence, tok)`` — a LITERAL substring match, which looks
    for the word "collection_denominator" in prose, where it never appears — so
    the arm fired on EVERY collection-scoped span in the fleet: 26 ERROR lines and
    an ~0.9 assembly branch on about half the country reads in the first live
    cycle, on reads whose quote fidelity was 1.000. Its SIBLING, six lines below,
    had the predicate right all along. So the declared token is now VERIFIED with
    the predicate that owns it, and a token this arm does not know is COUNTED
    (``assembly_scope_token_unknown``) and never charged — an unrecognised
    identifier is the arm's ignorance, not the payload's defect, and inventing a
    violation out of it is exactly how this class false-positived a fourth time.
    """
    res = ArmResult()
    charged = skip or set()
    for b_i, block in enumerate(_blocks(assembly)):
        ordinal = _ordinal(block)
        finding_id = _str(block.get("finding_id"))
        record = origins.get(finding_id)
        if record is None or not record.get("has_evidence_text"):
            continue
        body = str(record.get("evidence_text") or "")
        for s_i, span in enumerate(_spans(block)):
            if (b_i, s_i) in charged:
                continue
            text = _str(span.get("text"))
            if not text:
                continue
            origin = span.get("origin")
            if not isinstance(origin, Mapping):
                continue
            start, end = _as_int(origin.get("start")), _as_int(origin.get("end"))
            if start is None or end is None or end < start:
                continue
            res.bump("assembly_scope_spans_checked")
            sentence = source_sentence(body, start, end)
            declared = _scope_tokens(span)
            unknown = [
                tok for tok in declared
                if tok != SCOPE_TOKEN_COLLECTION_DENOMINATOR
            ]
            if unknown:
                res.bump("assembly_scope_token_unknown", len(unknown))
            missing = [
                tok for tok in declared
                if tok == SCOPE_TOKEN_COLLECTION_DENOMINATOR
                and not has_collection_denominator_scope(sentence)
            ]
            if missing:
                res.bump("assembly_scope_widened")
                res.findings.append(
                    ArmFinding(
                        reason=SCOPE_WIDENED,
                        text=text[:2000],
                        markers=[ordinal],
                        detail=(
                            f"the span declares scope token(s) "
                            f"{', '.join(repr(t) for t in missing)} that its own "
                            f"source sentence does not carry — the sentence has "
                            f"no collection denominator under "
                            f"has_collection_denominator_scope: {sentence[:240]!r}"
                        )[:_DETAIL_CHARS],
                    )
                )
                continue
            if not has_collection_denominator_scope(sentence):
                continue
            res.bump("assembly_scope_bounded_sentences")
            if has_collection_denominator_scope(text):
                continue
            res.bump("assembly_scope_truncated")
            res.findings.append(
                ArmFinding(
                    reason=SCOPE_TRUNCATED,
                    text=text[:2000],
                    markers=[ordinal],
                    detail=(
                        "the span drops the collection bound its source sentence "
                        f"carried: {sentence[:240]!r}"
                    )[:_DETAIL_CHARS],
                )
            )
    return res


def _scope_tokens(span: Mapping[str, Any]) -> list[str]:
    raw = span.get("scope_tokens")
    if not isinstance(raw, (list, tuple)):
        return []
    return [t for t in (_str(x) for x in raw) if t]


# ---------------------------------------------------------------------------
# ARM 3 — ATTRIBUTION EQUALITY
# ---------------------------------------------------------------------------

#: Timestamp trailing forms that carry no information for an equality test.
_TS_TRIM_RE = re.compile(r"(?:\.\d+)?(?:z|\+00:?00)$", re.IGNORECASE)


def _instant(value: str) -> str:
    """A timestamp reduced to what two capture paths can honestly be compared on:
    the ISO instant, ``T``/space unified, sub-second precision and the UTC suffix
    dropped. Not a parser — a normaliser. A value that is not ISO-shaped is
    returned folded, so two identical strings still compare equal and two
    different ones still differ."""
    low = normalize_for_match(value).replace(" ", "t")
    return _TS_TRIM_RE.sub("", low)


def attribution_equality(
    assembly: Mapping[str, Any], origins: Mapping[str, Mapping[str, Any]]
) -> ArmResult:
    """ARM 3 — the generated attribution line equals the captured origin record.

    At most ONE finding per block, precedence ``head_unresolved`` > ``desk`` >
    ``target`` > ``date``: the same "a block wrong in two ways costs what a block
    wrong in one way costs" rule ``composition_integrity._grade_one`` applies to
    a claim. Unresolved comes first because the other three are undecidable
    without a record to compare against.

    A field the CITATION BRIDGE does not carry is DECLINED and COUNTED, never
    passed. Until D-2b (2026-09-05) that was ``produced_at`` on EVERY row; since
    D-2b it is ``produced_at`` on rows written BEFORE that merge only, and the
    decline is what keeps the two populations distinguishable from the counters
    rather than from the clock. See the module banner.
    """
    res = ArmResult()
    for block in _blocks(assembly):
        ordinal = _ordinal(block)
        finding_id = _str(block.get("finding_id"))
        res.bump("assembly_attribution_blocks_checked")
        record = origins.get(finding_id)
        text = (
            f"block {ordinal}: {_str(block.get('desk')) or '?'} / "
            f"{_str(block.get('target_id')) or '?'} @ "
            f"{_str(block.get('produced_at')) or '?'}"
        )
        if record is None:
            res.bump("assembly_attribution_head_unresolved")
            res.findings.append(
                ArmFinding(
                    reason=ATTRIBUTION_HEAD_UNRESOLVED,
                    text=text,
                    markers=[ordinal],
                    detail=(
                        f"block {ordinal} names origin head "
                        f"{finding_id or '(none)'!r}, which resolves to no "
                        "citation on this row; nothing on the page can be traced"
                    )[:_DETAIL_CHARS],
                )
            )
            continue
        finding = _grade_block_attribution(block, record, ordinal, text, res)
        if finding is not None:
            res.findings.append(finding)
    return res


def _grade_block_attribution(
    block: Mapping[str, Any],
    record: Mapping[str, Any],
    ordinal: int | str,
    text: str,
    res: ArmResult,
) -> ArmFinding | None:
    desk, want_desk = _str(block.get("desk")), _str(record.get("source"))
    if want_desk and not fold_equal(desk, want_desk):
        res.bump("assembly_attribution_desk_mismatch")
        return ArmFinding(
            reason=ATTRIBUTION_DESK_MISMATCH,
            text=text,
            markers=[ordinal],
            detail=(
                f"block {ordinal} attributes its span to desk {desk!r}; the "
                f"origin head was produced by {want_desk!r}"
            )[:_DETAIL_CHARS],
        )
    target, want_target = _str(block.get("target_id")), _str(record.get("target_id"))
    if want_target and not fold_equal(target, want_target):
        res.bump("assembly_attribution_target_mismatch")
        return ArmFinding(
            reason=ATTRIBUTION_TARGET_MISMATCH,
            text=text,
            markers=[ordinal],
            detail=(
                f"block {ordinal} attributes its span to target {target!r}; the "
                f"origin head's target is {want_target!r}"
            )[:_DETAIL_CHARS],
        )
    produced, want_produced = (
        _str(block.get("produced_at")),
        _str(record.get("produced_at")),
    )
    if not want_produced:
        res.bump("assembly_attribution_date_unverifiable")
        return None
    if _instant(produced) != _instant(want_produced):
        res.bump("assembly_attribution_date_mismatch")
        return ArmFinding(
            reason=ATTRIBUTION_DATE_MISMATCH,
            text=text,
            markers=[ordinal],
            detail=(
                f"block {ordinal} dates its span {produced!r}; the origin head "
                f"was produced at {want_produced!r}"
            )[:_DETAIL_CHARS],
        )
    return None


# ---------------------------------------------------------------------------
# ARM 4 — SELECTION HONESTY
# ---------------------------------------------------------------------------


def selection_honesty(assembly: Mapping[str, Any]) -> ArmResult:
    """ARM 4 — (a) the coverage ledger, (b) the drop ledger.

    ONE finding per failing IDENTITY, not one per offending row: a broken ledger
    is a single construction defect and must cost the read one claim, not
    twenty. The detail carries the whole list, so nothing is hidden by the cap.
    """
    res = ArmResult()
    res.extend(_coverage_completeness(assembly))
    res.extend(_drop_disclosure(assembly))
    return res


def _coverage_completeness(assembly: Mapping[str, Any]) -> ArmResult:
    """ARM 4(a). The roster is the denominator; the ledger is the answer.

    Persisting the ledger is what makes ``metadata_mismatch`` impossible — the
    class fires today because the model's ``## Coverage`` prose disagrees with
    the ledger it was handed (a live specimen: a country read asserting
    ``tier=periphery`` for eight blocks the ledger records as ``basis``). With
    the ledger on the row, §B.6.4(a) is a diff between two persisted arrays.
    """
    res = ArmResult()
    raw = assembly.get("coverage")
    ledger = [c for c in raw if isinstance(c, Mapping)] if isinstance(raw, (list, tuple)) else []
    units = [_str(c.get("unit")) for c in ledger]
    res.bump("assembly_coverage_units_checked", len(ledger))

    seen = Counter(u for u in units if u)
    dupes = sorted(u for u, n in seen.items() if n > 1)
    if dupes:
        res.bump("assembly_coverage_unit_duplicated")
        res.findings.append(
            ArmFinding(
                reason=COVERAGE_UNIT_DUPLICATED,
                text=f"coverage ledger ({len(ledger)} rows)",
                detail=(
                    "the coverage ledger gives two answers for: "
                    + ", ".join(dupes)
                )[:_DETAIL_CHARS],
            )
        )
    bad = sorted(
        {
            _str(c.get("status"))
            for c in ledger
            if _str(c.get("status")) not in COVERAGE_STATUSES
        }
    )
    if bad:
        res.bump("assembly_coverage_status_invalid")
        res.findings.append(
            ArmFinding(
                reason=COVERAGE_STATUS_INVALID,
                text=f"coverage ledger ({len(ledger)} rows)",
                detail=(
                    "coverage status(es) outside the closed set "
                    f"{list(COVERAGE_STATUSES)}: {bad}"
                )[:_DETAIL_CHARS],
            )
        )
    roster = assembly.get("coverage_roster")
    if not isinstance(roster, (list, tuple)) or not roster:
        # DECLINED, not passed. Deriving the roster from the rows that arrived is
        # the original defect (build_coverage_ledger's own docstring), so this
        # arm refuses to reconstruct one.
        res.bump("assembly_coverage_roster_absent")
        return res
    declared = [_str(u) for u in roster if _str(u)]
    missing = [u for u in declared if u not in seen]
    if missing:
        res.bump("assembly_coverage_unit_missing")
        res.findings.append(
            ArmFinding(
                reason=COVERAGE_UNIT_MISSING,
                text=f"coverage ledger ({len(ledger)} of {len(declared)} roster units)",
                detail=(
                    "declared roster unit(s) with no coverage row: "
                    + ", ".join(missing)
                )[:_DETAIL_CHARS],
            )
        )
    return res


def _drop_disclosure(assembly: Mapping[str, Any]) -> ArmResult:
    """ARM 4(b). The ledger's arithmetic, its enum, and the prefix property.

    WHAT THIS ARM CAN AND CANNOT SEE, because F-7 is explicit about it. The
    strict-prefix property is FREE — selection takes a prefix of the order by
    construction, so ``drop_order_violation`` is unfalsifiable for the ordering
    half and fires only on a broken constructor. The DELIVERABLE is the ranked
    ``not_selected`` list itself: what the order put below the line, published.
    Both ship; the second is what R5 reads.

    The identity is checked PAYLOAD-INTERNALLY (``counts.shown`` standing for
    ``|derived_from|``) because an auditor over a published row is DB-free by
    design. The other half — ``counts.shown == |derived_from|`` — is D-2's own
    test (D-1 §1.3), and the two together close the loop.
    """
    res = ArmResult()
    drops = assembly.get("drops")
    if not isinstance(drops, Mapping):
        return res
    blocks = _blocks(assembly)
    counts_raw = drops.get("counts")
    counts = counts_raw if isinstance(counts_raw, Mapping) else {}
    for key in _DROP_COUNT_KEYS:
        n = _as_int(counts.get(key))
        if n is not None:
            res.bump(f"assembly_drops_{key}", n)

    lists: dict[str, list[Mapping[str, Any]]] = {}
    for list_key, _count_key in _DROP_GRAINS:
        raw = drops.get(list_key)
        lists[list_key] = (
            [e for e in raw if isinstance(e, Mapping)]
            if isinstance(raw, (list, tuple))
            else []
        )

    broken: list[str] = []
    carried = _as_int(counts.get("carried"))
    if carried is not None and carried != len(blocks):
        broken.append(f"counts.carried={carried} but the read carries {len(blocks)} blocks")
    for list_key, count_key in _DROP_GRAINS:
        n = _as_int(counts.get(count_key))
        if n is not None and n != len(lists[list_key]):
            broken.append(
                f"counts.{count_key}={n} but drops.{list_key} has "
                f"{len(lists[list_key])} entries"
            )
    shown = _as_int(counts.get("shown"))
    if shown is not None:
        want = len(lists["shown_not_carried"]) + len(lists["trimmed"])
        if shown - len(blocks) != want:
            broken.append(
                f"|shown|-|blocks| = {shown}-{len(blocks)} = {shown - len(blocks)} "
                f"but shown_not_carried+trimmed = {want}"
            )
    if broken:
        res.bump("assembly_drop_count_mismatch")
        res.findings.append(
            ArmFinding(
                reason=DROP_COUNT_MISMATCH,
                text=f"drop ledger ({len(blocks)} blocks carried)",
                detail=("; ".join(broken))[:_DETAIL_CHARS],
            )
        )

    # THE WHY-CLASS RULE, and the conservative half of it is deliberate. An entry
    # that CARRIES a ``why`` must carry one from the closed enum — a fabricated
    # reason is worse than a published count, which is the same doctrine that
    # keeps ``invisible_heads`` a number and never a list. An entry with NO
    # ``why`` is COUNTED, not charged: three of the five grains
    # (``trimmed`` / ``below_floor`` / ``no_head``) have their class implied by
    # the grain they are in, the producer's schema is the authority on which
    # grains stamp one, and a HARD arm that fires on every read at merge would be
    # withdrawn within a day. The counter is what keeps the silence measurable.
    absent = sum(
        1 for entries in lists.values() for e in entries if not _str(e.get("why"))
    )
    res.bump("assembly_drops_why_absent", absent)
    unknown = sorted(
        {
            why
            for entries in lists.values()
            for e in entries
            if (why := _str(e.get("why"))) and why not in DROP_WHY_CLASSES
        }
    )
    if unknown:
        res.bump("assembly_drop_why_unknown")
        res.findings.append(
            ArmFinding(
                reason=DROP_WHY_UNKNOWN,
                text=f"drop ledger ({len(blocks)} blocks carried)",
                detail=(
                    "drop why-class(es) outside the closed enum "
                    f"{list(DROP_WHY_CLASSES)}: {unknown}"
                )[:_DETAIL_CHARS],
            )
        )

    violations = _prefix_violations(lists["not_selected"], len(blocks))
    if violations:
        res.bump("assembly_drop_order_violation")
        res.findings.append(
            ArmFinding(
                reason=DROP_ORDER_VIOLATION,
                text=f"drop ledger ({len(blocks)} blocks carried)",
                detail=("; ".join(violations))[:_DETAIL_CHARS],
            )
        )
    return res


def _prefix_violations(
    not_selected: Sequence[Mapping[str, Any]], n_blocks: int
) -> list[str]:
    """The strict-prefix property, stated as what would falsify it.

    Selection takes ranks ``1..n_blocks`` of the candidate order, so every
    not-selected candidate ranks strictly below ``n_blocks`` — and no two
    candidates share a rank, because the order is TOTAL by construction
    (severity, cited_mass, produced_at, finding_id — no five-second tiebreak).
    """
    out: list[str] = []
    ranks: list[int] = []
    for entry in not_selected:
        rank = _as_int(entry.get("rank"))
        if rank is None:
            continue
        ranks.append(rank)
        if rank <= n_blocks:
            out.append(
                f"not-selected candidate {_str(entry.get('finding_id'))[:12]!r} "
                f"ranks {rank}, at or above the {n_blocks} carried blocks"
            )
    dupes = sorted(r for r, n in Counter(ranks).items() if n > 1)
    if dupes:
        out.append(f"the candidate order is not total — duplicate rank(s) {dupes}")
    return out


# ---------------------------------------------------------------------------
# THE FOLD
# ---------------------------------------------------------------------------


def audit(assembly: Any, citations: Any) -> ArmResult:
    """All four arms over one payload. PURE — no report, no logging, no DB.

    This is the surface a replay harness and the D-4 reader both use; ``fold``
    below is the same result folded into a :class:`verify.FaithfulnessReport`.
    Returns an EMPTY result for every non-assembly payload, which is the
    inertness property stated as a return value.
    """
    res = ArmResult()
    if not is_assembly(assembly):
        # A FUTURE schema counts — the miss must be visible, because an arm set
        # that silently ignores a payload it was not written for is worse than
        # one that fires wrongly. A LEGACY-REGIME row does NOT count: it declines
        # for the same reason a unit finding does, and a counter on every
        # flag-off composition would be a receipt for a non-event.
        if (
            isinstance(assembly, Mapping)
            and assembly.get("schema")
            and str(assembly.get("schema")) != ASSEMBLY_SCHEMA
        ):
            res.bump("assembly_arms_schema_unknown")
        return res
    res.bump("assembly_arms_reads_audited")
    origins = dict(origin_records(citations))
    # P3-B — the CARRIED blocks' bridge, merged UNDER the row's own citations.
    # Inert by construction on every row that carries nothing: the key is absent,
    # ``origin_records`` returns ``{}`` and this loop does not execute, so the
    # audit of every payload written before this train is byte-identical. The
    # row's citations win a collision on purpose — a candidate id and a desk head
    # id cannot collide today, and if one ever did the row's own bridge is the
    # one the drill-down uses.
    bridged = origin_records(assembly.get(CARRIED_ORIGINS_KEY))
    for _head_id, _record in bridged.items():
        origins.setdefault(_head_id, _record)
    if bridged:
        res.bump("assembly_carried_origins_bridged", len(bridged))
    quotes = quote_fidelity(assembly, origins)
    res.extend(quotes)
    res.extend(scope_preservation(assembly, origins, skip=quotes.charged))
    res.extend(attribution_equality(assembly, origins))
    res.extend(selection_honesty(assembly))
    return res


#: ARM 4's fixed check units — coverage duplication / status / completeness, and
#: the drop ledger's arithmetic / enum / order. Six identities, each of which can
#: cost the read exactly one claim however many rows it names.
LEDGER_CHECK_UNITS = 6


def branch_score(result: ArmResult) -> dict[str, Any] | None:
    """The ``branch_scores['assembly']`` entry, or ``None`` when nothing ran.

    THE UNIT IS THE CHECK, not the sentence, and that is the honest denominator
    for a deterministic instrument: one span for ARM 1/2 (they share it — ARM 2
    declines what ARM 1 charged), one block for ARM 3, one identity for each of
    ARM 4's six. ``len(findings) <= checkable`` holds by construction, which is
    what keeps the sub-score a ratio rather than a coincidence.

    Its presence is what stamps ``branch_versions.assembly = "assembly_arms.v1"``
    onto the critique — the same visible per-kind version contract the five prose
    kinds carry, for an instrument that has no judge at all.
    """
    checkable = (
        result.counters.get("assembly_quote_spans_checked", 0)
        + result.counters.get("assembly_attribution_blocks_checked", 0)
        + LEDGER_CHECK_UNITS
    )
    if not result.counters.get("assembly_arms_reads_audited"):
        return None
    supported = max(0, checkable - len(result.findings))
    return {
        "checkable": checkable,
        "supported": supported,
        "score": round(supported / checkable, 4) if checkable else 1.0,
    }


def quote_fidelity_score(result: ArmResult) -> float | None:
    """``ok_spans / spans`` for the read (D-1 §3.1's per-read output), or
    ``None`` when no span was checked.

    THE BAR IS 1.000, n = all spans — G2, and it is stated as an INVARIANT
    rather than a threshold so nobody reports 0.98 as a pass. The 0.75 analog
    belongs to the PROSE tier; the assembly's equivalent is a construction
    property, and anything below 1.0 is a bug.
    """
    n = result.counters.get("assembly_quote_spans_checked", 0)
    if not n:
        return None
    return result.counters.get("assembly_quote_spans_ok", 0) / n


def is_grader_of_record(assembly: Any) -> bool:
    """Are the ARMS the grader of this row's headline faithfulness number?

    True for exactly one population: a row the arms audit whose regime is the
    explicit word ``"assembly"``. Deliberately NARROWER than :func:`is_assembly`,
    which is the AUDIT gate — D-5's ``"rollup"`` stamp and a regime-less row both
    pass that one, and neither may have its published score rewritten:

      * a **rollup** carries no blocks and no spans, so the arms' denominator
        would be the six ledger units alone and the headline would read 1.000 for
        a row nothing checked. (Today it never reaches this path at all — the
        rollup is deterministic and takes the ``structural_claims`` critique
        instead — and this gate is what keeps that true if it ever does.)
      * a row whose regime is ABSENT is audited on purpose (a producer that
        forgets the label must not silence its own auditor), but "audit it
        anyway" is not "believe its arithmetic instead of the judge's".
    """
    return (
        is_assembly(assembly)
        and str((assembly or {}).get("regime") or "") == REGIME_ASSEMBLY
    )


def regrade_to_arms(report: Any, branch: Mapping[str, Any] | None) -> Any:
    """THE ASSEMBLY ROW'S HEADLINE IS THE ARMS' RATIO. (2026-09-05/1.)

    WHAT WENT WRONG. ``faithfulness_score`` is ``supported / checkable`` over the
    claims the LLM judge graded, partitioned into ``absence`` / ``synthesis`` /
    ``citation_support``. Those branches ask ONE question — *does this authored
    sentence follow from the evidence it cites?* — and an assembly row has no
    authored sentence to ask it of. Every substantive line is a span ARM 1 proved
    byte-identical to its origin; every other line is machine-printed from the
    ledger ARM 3 and ARM 4 audit. So the judge was handed a coverage row
    ("leadership_transition: in basis, 10.5h old"), a generated attribution line
    and a desk's own quoted BLUF, and asked whether they follow from the
    sub-claims — a category error whose answer was 0.18 on the first live cycle
    while ``branch_scores["assembly"]`` read 1.000 on the same row.

    That number was not cosmetic. ``LEGBA_COMPOSITION_VERIFY_FLOOR`` = 0.50 and
    the world assembler is verify-floored on its inputs, so on 2026-09-05 it
    floored out **31 of 32 byte-correct country assemblies** and published a
    world read of one block.

    THE RULE. For an assembly-regime row the graded population is the ARMS'
    checks — one per quoted span, one per attributed block, one per ledger
    identity — and the headline is their ratio. Nothing is deleted: every judge
    verdict, every deterministic mark and every branch sub-score stays on the row
    in ``branch_scores``, ``claim_verdicts`` and ``unsupported_spans``, where a
    reader can see what each grader said. They are simply no longer the score of
    a read that authored nothing.

    WHY THIS AND NOT "SKIP THE LEGACY BRANCHES". Not sending those claims to the
    judge would leave the DETERMINISTIC floor's denominator behind — it counts
    every fact-asserting span before any judge runs, and
    ``_maybe_llm_judge`` floors the published count at it — so the headline would
    have kept the same denominator with a smaller numerator and scored WORSE.
    Deferring to the arms is the change that is true at the point where the whole
    tally is known, and it is the one a legacy row cannot reach.

    THE CEILING SURVIVES. ``confidence_ceiling`` is untouched, so
    ``overall_score = min(arms, ceiling)`` still caps an assembly at its strongest
    INDEPENDENT cited sub-claim. A read assembled perfectly out of weak inputs is
    still a weak read; this only stops a read assembled perfectly out of strong
    inputs from being scored as if a model had written it.

    NOT A GATE. D-1 §3.6's forced ``0.0`` on a firing arm is still D-3b and is
    still not built, so a construction bug reads (say) 0.90 here rather than
    zero. That is the same labels-only posture the rest of the plane holds, and
    it is stated so nobody reads this function as the gate arriving.
    """
    if not branch:
        return report
    checkable = int(branch.get("checkable") or 0)
    if checkable <= 0:
        return report
    supported = int(branch.get("supported") or 0)
    report.faithfulness_score = supported / checkable
    report.checkable_claims = checkable
    report.supported_claims = supported
    report.score_denominator = checkable
    report.bump("assembly_headline_regraded_to_arms")
    return report


def _fold_one(report: Any, finding: ArmFinding) -> Any:
    """One violation + its ledger row. Byte-identical arithmetic to
    ``verify._fold_guard_spans``, ``judge_input_checks._fold_soft`` and
    ``composition_integrity._fold_soft``: the denominator grows by one, the
    numerator does not. The hard/soft SEVERITY is not decided here — it comes
    off the one ``_FAIL_CLASS_BY_REASON`` table via ``ClaimVerdict.failed``, so
    this helper cannot disagree with the table."""
    v = _verify()
    span = v.UnsupportedSpan(
        text=finding.text[:2000],
        reason=finding.reason,
        markers=list(finding.markers),
        detail=finding.detail,
    )
    checkable = report.checkable_claims + 1
    supported = report.supported_claims
    return v.FaithfulnessReport(
        faithfulness_score=(1.0 if checkable == 0 else supported / checkable),
        checkable_claims=checkable,
        supported_claims=supported,
        unsupported_spans=list(report.unsupported_spans) + [span],
        judge_status=report.judge_status,
        judge_unavailable_reason=report.judge_unavailable_reason,
        # 2026-09-08/1 — the judge TRANSPORT receipts ride every rebuild of a
        # report, or a fold that adds one span would erase the evidence that
        # the judge had to be asked three times. ``report`` is typed ``Any``
        # here (these folds take report-SHAPED objects, doubles included), so
        # the read is defensive — a missing field is absent, never a raise.
        judge_attempts=getattr(report, "judge_attempts", None),
        judge_http_statuses=list(
            getattr(report, "judge_http_statuses", None) or []
        ),
        # H3 (2026-09-25/1) — the same defensive carry as the pair above.
        judge_miscount_claims=getattr(report, "judge_miscount_claims", 0) or 0,
        confidence_ceiling=report.confidence_ceiling,
        branch_scores=report.branch_scores,
        claim_verdicts=list(report.claim_verdicts)
        + [
            v.ClaimVerdict.failed(
                span.text, finding.reason, list(finding.markers), finding.detail
            )
        ],
        counters=dict(report.counters),
        score_denominator=checkable,
        score_state=report.score_state,
        score_state_reason=report.score_state_reason,
    )


def fold(report: Any, *, assembly: Any, citations: Any) -> Any:
    """D-3 — the four arms, folded into the faithfulness report.

    Named for MODULE-QUALIFIED use (``assembly_arms.fold(...)``), the
    ``composition_integrity`` idiom, because ``verify.py`` has ~130 lines of
    ceiling and the call site should read as what it is.

    NO-OP for every non-assembly caller — including a LEGACY-REGIME row, which
    carries a real ``assembly.v1`` schema key and nothing behind it: no counters,
    no spans, no log lines, byte-identical. Never raises — a malformed payload
    degrades to no flag, which is the honest direction and the one every other
    fold in this plane takes.

    **THERE IS NO ``body`` PARAMETER, DELIBERATELY.** The rendered body defuses
    child ``[[ref:N]]`` markers; the payload's ``spans[].text`` does not. Every
    byte comparison in these arms runs payload-against-origin, never
    body-against-anything, and the missing parameter is what makes that
    unbreakable by a later edit.

    THE LOUD PART. Every violation logs at ERROR, not WARNING, and the message
    says why: these arms grade a GENERATED payload, so a fire is a broken
    constructor rather than a bad finding, and it is the one class in this plane
    that should page.
    """
    if not is_assembly(assembly):
        return report
    try:
        result = audit(assembly, citations)
    except Exception as exc:  # noqa: BLE001 — degrade-not-drop, never break verify
        logger.warning("verify.assembly_arms.audit_failed err=%s", exc)
        return report

    out = report
    for name, n in result.counters.items():
        out.bump(name, n)
    branch = branch_score(result)
    if branch is not None:
        # Set BEFORE the folds: ``_fold_one`` carries ``branch_scores`` through by
        # reference, so the entry rides every rebuilt report and lands in
        # ``branch_versions`` on the critique.
        out.branch_scores = {**(out.branch_scores or {}), "assembly": branch}
    for finding in result.findings:
        logger.error(
            "verify.assembly_arms.%s markers=%s — %s | THE CONSTRUCTOR IS BROKEN: "
            "this payload is GENERATED, so a deterministic arm firing is a bug in "
            "the assembler, not a verdict about a model",
            finding.reason, finding.markers, finding.detail,
        )
        out = _fold_one(out, finding)
    score = quote_fidelity_score(result)
    logger.log(
        logging.ERROR if result.findings else logging.INFO,
        "verify.assembly_arms.audited blocks=%d spans=%d quote_fidelity=%s "
        "violations=%d",
        len(_blocks(assembly)),
        result.counters.get("assembly_quote_spans_checked", 0),
        "n/a" if score is None else f"{score:.4f}",
        len(result.findings),
    )
    # 2026-09-05/1 — and it is the LAST thing that touches the tally, which is
    # why it lives here rather than at a sixth call site in ``verify.py``: every
    # fold that can move ``checkable_claims`` has already run by the time this
    # module is called, and ``resolve_score_state`` is all that follows.
    if is_grader_of_record(assembly):
        out = regrade_to_arms(out, branch)
    return out


# ``fold``, ``audit``, ``FAIL_CLASSES`` and ``COUNTERS`` are deliberately short:
# they are only ever reached MODULE-QUALIFIED (``assembly_arms.fold``), where the
# module name carries the meaning. Everything else is spelled in full.
__all__ = [
    "ASSEMBLY_SCHEMA",
    "ATTRIBUTION_DATE_MISMATCH",
    "ATTRIBUTION_DESK_MISMATCH",
    "ATTRIBUTION_HEAD_UNRESOLVED",
    "ATTRIBUTION_TARGET_MISMATCH",
    "ArmFinding",
    "ArmResult",
    "CARRIED_ORIGINS_KEY",
    "COUNTERS",
    "COVERAGE_STATUSES",
    "COVERAGE_STATUS_INVALID",
    "COVERAGE_UNIT_DUPLICATED",
    "COVERAGE_UNIT_MISSING",
    "DROP_COUNT_MISMATCH",
    "DROP_ORDER_VIOLATION",
    "DROP_WHY_CLASSES",
    "DROP_WHY_UNKNOWN",
    "FAIL_CLASSES",
    "LEDGER_CHECK_UNITS",
    "QUOTE_NOT_CONTAINED",
    "QUOTE_OFFSET_MISMATCH",
    "QUOTE_ORIGIN_DRIFT",
    "QUOTE_ORIGIN_TRUNCATED",
    "REGIME_ASSEMBLY",
    "SCOPE_TOKEN_COLLECTION_DENOMINATOR",
    "SCOPE_TRUNCATED",
    "SCOPE_WIDENED",
    "attribution_equality",
    "audit",
    "branch_score",
    "fold",
    "is_assembly",
    "is_grader_of_record",
    "origin_records",
    "quote_fidelity",
    "quote_fidelity_score",
    "regrade_to_arms",
    "scope_preservation",
    "selection_honesty",
    "source_sentence",
]
