# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The STRUCTURAL-CLAIMS verify profile — the second, deterministic critique path.

Extracted from ``verify.py`` (V-G7, 2026-08-03). Two reasons, in this order:

1. The 08-03 counter audit found ``indicator_tracker`` (70 critiques) and
   ``narrative_mapper`` (7) carrying no ``judge_pipeline_version`` and no
   ``counters`` since 07-27 — "a legacy critique path, invisible to every F-A
   receipt". Tracing it: they are not on a legacy path at all. They are on THIS
   one, which is a legitimately different critique kind that had never been given
   a population stamp or receipts of its own. Giving it those is easier to do,
   and far easier to read, in its own module than buried at the tail of the
   faithfulness path it deliberately is not.
2. ``verify.py`` is under a size ceiling that the V-G train would otherwise have
   breached, and this is a cohesive unit with exactly one inbound dependency
   (nothing) and one outbound (the caller).

WHY IT IS A DIFFERENT KIND, not a gap to be closed. These findings are aggregate
COUNTS with ``evidence=[]`` and flat ``confidence=1.0``; ``narrative_mapper``
writes no ``data['citations']`` key at all. There is no cited LLM prose for a
faithfulness judge to grade — the truthmaker is arithmetic over the finding's own
declared basis, and re-deriving it is both stricter and cheaper than asking a
model. So the faithfulness stamp would be a lie on these rows, and every
faithfulness consumer pins ``title LIKE 'Faithfulness verify%'`` before it reads
a stamp, which is exactly why nothing downstream was broken by the omission.

What WAS missing is the thing ``JUDGE_PIPELINE_VERSION`` exists to provide: a
population-split key, so a before/after change to this path never pools, and
receipts, so an audit can read the class from the counters instead of guessing at
a JSONB path. Both land here as :data:`STRUCTURAL_PIPELINE_VERSION` and
``StructuralVerifyReport.counters``.

``verify`` re-exports every name, so ``verify.verify_structural_claims`` and
``verify.STRUCTURAL_CLAIMS_DATA_KEY`` resolve exactly as before.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# C2b (P4-6) — the ``structural_claims`` verify PROFILE
# ---------------------------------------------------------------------------
#
# The honesty architecture's ONE documented exception (S-1-era / C2) is the
# deterministic STRUCTURAL analysts (graph_mining, geo_convergence_scan,
# indicator_tracker, thematic_proposal, …): they emit findings OUTSIDE the
# mandatory faithfulness verify pass at flat conf=1.0, shown with an
# ``unverified — structural`` badge (``STRUCTURAL_VERIFY_EXEMPT_ANALYSTS`` in
# provenance.kinds), because their product is a COUNT / AGGREGATE over substrate
# rows, not cited LLM prose the faithfulness judge can grade.
#
# But a structural finding that ASSERTS A CHECKABLE QUANTITY ("3 distinct source
# families converged in cell X", "currently_formed_bins = cell + country bins",
# "these N sources co-carry claim Y") CAN be verified — not by an LLM
# faithfulness judge (this is not cited prose), but by DETERMINISTIC
# RE-DERIVATION: recompute the asserted quantity from the constituent set the
# finding itself recorded (its ``derived_from`` rows / the per-bin breakdown
# captured in ``data``) and check the finding's number MATCHES. A mismatch flags
# a structural analyst that MISCOUNTS.
#
# CONTRACT — the finding declares its checkable claims in
# ``data['structural_claims']`` as a list of self-describing claim objects, so
# this module re-derives GENERICALLY (verify.py imports nothing analyst-specific
# and stays slim-image-safe; the analyst owns WHAT to claim, this seam owns HOW
# to check it):
#
#   {
#     "id": "families_cell_35_51",                     # optional label
#     "statement": "3 distinct source families in cell 35,51",  # human text
#     "op": "distinct_count" | "count" | "sum" | "equals",
#     "asserted": 3,                                   # the number the finding CLAIMS
#     "basis": ["news", "gis", "health"],              # the recorded constituent set
#     "field": "family",                               # optional dict-projection key
#   }
#
#   * count          — asserted == len(basis)
#   * distinct_count — asserted == len({project(b, field) for b in basis})
#   * sum            — asserted == sum(basis)   (basis = a list of numbers)
#   * equals         — asserted == basis        (a scalar identity; basis is the
#                                                recomputed expected value)
#   * basis == ``"@derived_from"`` (sentinel) — the re-derivation runs against
#     the finding's ACTUAL ``derived_from`` id list (passed in by the caller),
#     so a "N contributing rows" claim is checked against the real lineage,
#     not a number the analyst also typed.
#
# HONESTY. A claim whose op/basis can't be re-derived (malformed, unknown op, a
# non-list basis for a set op) is ``unverifiable_structural`` — NEVER a fake
# pass. A finding with NO structural_claims block is a NO-OP (the caller writes
# no critique; the row keeps its honest ``unverified — structural`` badge). The
# finding is stamped ``structural_verified`` only when EVERY declared claim
# re-derived AND matched (≥1 checkable, zero miscounts, zero unverifiable).
# ---------------------------------------------------------------------------

# V-G7 (2026-08-03) — the POPULATION STAMP this path never had.
#
# The verify spec's spine caution built ``judge_pipeline_version`` so before/after
# populations never pool: "calibration/band history splits on it cleanly, and the
# expected UPWARD shift is communicated as a measurement correction, not a quality
# jump." Everything in that sentence is as true of a deterministic re-derivation
# as of an LLM judge — an op added, a basis semantics tightened, and last week's
# structural_verified rate stops meaning what it meant. This path simply never got
# the key, so the 08-03 audit read 118 stamped-day structural critiques as "a
# legacy path, invisible to every receipt" and could not tell one era from another.
#
# Same idiom, same format (``<train date>/<n>``), bumped on any change to the
# re-derivation SEMANTICS — a new op, a changed comparison, a different
# unverifiable boundary. NOT bumped for a refactor that moves no verdict.
STRUCTURAL_PIPELINE_VERSION = "2026-08-03/1"

#: The finding-``data`` key carrying the declared structural claims.
STRUCTURAL_CLAIMS_DATA_KEY = "structural_claims"
#: A ``basis`` sentinel selecting the finding's actual ``derived_from`` id list.
STRUCTURAL_DERIVED_FROM_SENTINEL = "@derived_from"

#: The re-derivation ops this profile understands.
_STRUCTURAL_OPS = frozenset({"count", "distinct_count", "sum", "equals"})

#: Per-claim verdict labels.
STRUCTURAL_SUPPORTED = "supported"
STRUCTURAL_MISCOUNT = "structural_miscount"
STRUCTURAL_UNVERIFIABLE = "unverifiable_structural"

#: V-G7 — verdict label -> receipts counter. An explicit table rather than an
#: f-string, because two of the three labels already carry "structural" and the
#: naive interpolation yields ``structural_structural_miscount``.
_STRUCTURAL_VERDICT_COUNTER: dict[str, str] = {
    STRUCTURAL_SUPPORTED: "structural_supported",
    STRUCTURAL_MISCOUNT: "structural_miscount",
    STRUCTURAL_UNVERIFIABLE: "structural_unverifiable",
}

#: Bound on the persisted per-claim ledger (mirrors the faithfulness cap posture).
_STRUCTURAL_VERDICTS_CAP = 120
_STRUCTURAL_STATEMENT_CHARS = 300

# OFF-SAFE gate (C2b point 4). The structural critique is ALWAYS written and its
# verdict ALWAYS shown (the badge + the data.verification detail). Whether it
# DEMOTES effective_confidence (via the finding↔critique ``overall_score`` gate)
# is behind this flag, code DEFAULT OFF ("compute-and-show, do-not-gate"): when
# off the critique's ``overall_score`` is pinned to 1.0 so a miscount never
# lowers a structural finding's surfaced confidence; when on it carries the
# honest re-derivation fraction so a miscount demotes like any critic score.
_STRUCTURAL_VERIFY_GATE_ENV = "LEGBA_STRUCTURAL_VERIFY_GATE"


def structural_verify_gate_enabled() -> bool:
    """Whether a structural critique's score DEMOTES effective_confidence. Off
    by default (compute-and-show, not-yet-gate — C2b OFF-safe posture)."""
    raw = os.getenv(_STRUCTURAL_VERIFY_GATE_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class StructuralClaimVerdict:
    """One re-derived structural claim's verdict.

    ``verdict`` ∈ {``supported`` (re-derived == asserted), ``structural_miscount``
    (re-derived != asserted — the finding misstates its own evidence),
    ``unverifiable_structural`` (the claim's op/basis could not be re-derived —
    NEVER a fake pass)}.
    """

    claim_id: str
    statement: str
    op: str
    asserted: Any
    rederived: Any
    verdict: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.claim_id[:120],
            "statement": self.statement[:_STRUCTURAL_STATEMENT_CHARS],
            "op": self.op,
            "asserted": self.asserted,
            "rederived": self.rederived,
            "verdict": self.verdict,
            "detail": self.detail[:200],
        }


@dataclass
class StructuralVerifyReport:
    """Result of the ``structural_claims`` re-derivation over ONE finding.

    ``had_claims`` is False when the finding carried no (non-empty)
    ``structural_claims`` block — the caller then writes NO critique (a no-op;
    the row keeps its honest ``unverified — structural`` badge). Otherwise the
    per-claim ``claim_verdicts`` carry each re-derivation outcome.
    """

    claim_verdicts: list[StructuralClaimVerdict] = field(default_factory=list)
    had_claims: bool = False
    #: V-G7 — the RECEIPTS counters, mirroring ``FaithfulnessReport.counters``.
    #: Sparse: a key appears only when it fired, so a payload diff shows what
    #: actually happened rather than a wall of zeroes. Per verdict CLASS and per
    #: re-derivation OP, because "which op is producing the unverifiables" is the
    #: first question any audit of this path asks and the ledger makes you count
    #: it by hand.
    counters: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str, n: int = 1) -> None:
        """Record ``n`` more of receipt ``key`` (mirrors ``FaithfulnessReport.bump``)."""
        if n:
            self.counters[key] = self.counters.get(key, 0) + n

    @property
    def supported(self) -> int:
        return sum(1 for v in self.claim_verdicts if v.verdict == STRUCTURAL_SUPPORTED)

    @property
    def miscount(self) -> int:
        return sum(1 for v in self.claim_verdicts if v.verdict == STRUCTURAL_MISCOUNT)

    @property
    def unverifiable(self) -> int:
        return sum(
            1 for v in self.claim_verdicts if v.verdict == STRUCTURAL_UNVERIFIABLE
        )

    @property
    def checkable(self) -> int:
        """Claims that WERE re-derivable (supported + miscount)."""
        return self.supported + self.miscount

    @property
    def structural_verified(self) -> bool:
        """True only when EVERY declared claim re-derived AND matched (≥1
        checkable, zero miscounts, zero unverifiable) — the honest bar for the
        ``structural-verified`` badge. Any mismatch or any non-re-derivable
        claim keeps the finding UN-certified (honest ``unverified — structural``).
        """
        return (
            self.had_claims
            and self.checkable >= 1
            and self.miscount == 0
            and self.unverifiable == 0
        )

    @property
    def score(self) -> float:
        """Fraction of RE-DERIVABLE claims that matched; 1.0 when none is
        re-derivable (we never fabricate a demotion for an unverifiable claim —
        the badge stays honest via ``structural_verified``, not the score)."""
        c = self.checkable
        return 1.0 if c == 0 else self.supported / c


def _structural_project_distinct(basis: Any, field_name: Any) -> tuple[int | None, bool]:
    """``(distinct_count, ok)`` — the count of distinct projected members, or
    ``ok=False`` when the basis/field can't be projected (→ unverifiable)."""
    if not isinstance(basis, (list, tuple)):
        return None, False
    keys: list[Any] = []
    for item in basis:
        if field_name is not None:
            if not isinstance(item, Mapping) or field_name not in item:
                return None, False
            keys.append(item[field_name])
        else:
            keys.append(item)
    try:
        return len(set(keys)), True
    except TypeError:
        return None, False  # unhashable members → not re-derivable, honest


def _structural_sum(basis: Any) -> tuple[float | int | None, bool]:
    """``(sum, ok)`` over a list of numbers (bools rejected — a stray ``True``
    is not a number). Preserves int when every member is int."""
    if not isinstance(basis, (list, tuple)):
        return None, False
    total: float = 0.0
    all_int = True
    for item in basis:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None, False
        total += item
        if not isinstance(item, int):
            all_int = False
    return (int(total) if all_int else total), True


def _structural_values_match(asserted: Any, rederived: Any) -> bool:
    """Equality with float tolerance for numeric pairs (bools compared exactly)."""
    if isinstance(asserted, bool) or isinstance(rederived, bool):
        return asserted == rederived
    if isinstance(asserted, (int, float)) and isinstance(rederived, (int, float)):
        return abs(float(asserted) - float(rederived)) <= 1e-9
    return asserted == rederived


def _verify_one_structural_claim(
    raw: Any, idx: int, derived_from_ids: list[str]
) -> StructuralClaimVerdict:
    """Re-derive ONE declared claim and classify it. Never raises — a malformed
    claim is ``unverifiable_structural`` (honest), never a fabricated pass."""
    if not isinstance(raw, Mapping):
        return StructuralClaimVerdict(
            claim_id=f"claim_{idx}", statement="", op="", asserted=None,
            rederived=None, verdict=STRUCTURAL_UNVERIFIABLE,
            detail="claim is not an object",
        )
    claim_id = str(raw.get("id") or f"claim_{idx}")
    statement = str(raw.get("statement") or "")
    op = str(raw.get("op") or "")
    asserted = raw.get("asserted")
    field_name = raw.get("field")
    basis = raw.get("basis")
    # The ``@derived_from`` sentinel re-derives against the finding's ACTUAL
    # lineage ids (the substrate rows the finding derives from), not a number
    # the analyst also typed into its own payload.
    if basis == STRUCTURAL_DERIVED_FROM_SENTINEL:
        basis = list(derived_from_ids)

    def _unverifiable(detail: str) -> StructuralClaimVerdict:
        return StructuralClaimVerdict(
            claim_id=claim_id, statement=statement, op=op, asserted=asserted,
            rederived=None, verdict=STRUCTURAL_UNVERIFIABLE, detail=detail,
        )

    if op not in _STRUCTURAL_OPS:
        return _unverifiable(f"unknown op {op!r}")
    if asserted is None:
        return _unverifiable("no asserted value")

    if op == "count":
        if not isinstance(basis, (list, tuple)):
            return _unverifiable("basis is not a list")
        rederived: Any = len(basis)
    elif op == "distinct_count":
        rederived, ok = _structural_project_distinct(basis, field_name)
        if not ok:
            return _unverifiable("basis/field not projectable")
    elif op == "sum":
        rederived, ok = _structural_sum(basis)
        if not ok:
            return _unverifiable("basis is not a list of numbers")
    else:  # equals — basis IS the recomputed expected scalar
        if basis is None:
            return _unverifiable("no expected value")
        rederived = basis

    matched = _structural_values_match(asserted, rederived)
    return StructuralClaimVerdict(
        claim_id=claim_id,
        statement=statement,
        op=op,
        asserted=asserted,
        rederived=rederived,
        verdict=STRUCTURAL_SUPPORTED if matched else STRUCTURAL_MISCOUNT,
        detail="" if matched else f"asserted {asserted!r} != re-derived {rederived!r}",
    )


def verify_structural_claims(
    *,
    data: Any,
    derived_from: list[Any] | None = None,
) -> StructuralVerifyReport:
    """The ``structural_claims`` verify profile — DETERMINISTIC re-derivation of
    a structural finding's asserted quantities (C2b / P4-6).

    Reads ``data[STRUCTURAL_CLAIMS_DATA_KEY]`` (a list of self-describing claim
    objects), re-derives each asserted quantity from the constituent set the
    finding recorded, and returns a :class:`StructuralVerifyReport`. A claim that
    can't be re-derived is ``unverifiable_structural`` (never a fake pass). A
    finding carrying no claims block returns ``had_claims=False`` (the caller
    writes no critique). ``derived_from`` supplies the finding's actual lineage
    ids for the ``@derived_from`` basis sentinel; DB-free + pure so verify.py
    stays slim-image-safe.
    """
    claims = data.get(STRUCTURAL_CLAIMS_DATA_KEY) if isinstance(data, Mapping) else None
    if not isinstance(claims, (list, tuple)) or not claims:
        return StructuralVerifyReport(claim_verdicts=[], had_claims=False)
    df_ids = [str(x) for x in (derived_from or []) if x is not None and str(x)]
    verdicts = [
        _verify_one_structural_claim(raw, i, df_ids) for i, raw in enumerate(claims)
    ]
    report = StructuralVerifyReport(claim_verdicts=verdicts, had_claims=True)
    # V-G7 receipts: per verdict class, per re-derivation op, and the
    # ``@derived_from`` sentinel — the one basis form that checks a claim against
    # the REAL lineage rather than a number the analyst also typed, and therefore
    # the one an audit most wants a rate for.
    for v in verdicts:
        report.bump(_STRUCTURAL_VERDICT_COUNTER[v.verdict])
        # BOUNDED cardinality: an unknown op is analyst-supplied text, and a
        # counter map keyed by arbitrary strings is a counter map nobody can
        # aggregate. Known ops get their own key; everything else pools.
        op = v.op if v.op in _STRUCTURAL_OPS else "unknown"
        report.bump(f"structural_op_{op}")
    sentinel_claims = sum(
        1
        for raw in claims
        if isinstance(raw, Mapping)
        and raw.get("basis") == STRUCTURAL_DERIVED_FROM_SENTINEL
    )
    report.bump("structural_derived_from_basis", sentinel_claims)
    return report


def build_structural_critique_payload(
    report: StructuralVerifyReport,
    *,
    analyzed_output_id: UUID,
    analyzed_analyst_id: str = "",
    analyzed_analyst_version: str = "",
    gate: bool | None = None,
) -> dict[str, Any]:
    """Build the ``CritiquePayload``-shaped dict for a structural verdict.

    Uses the EXISTING critique contract (``analyzed_output_id`` + top-level
    ``overall_score`` + ``data.verification``) so every faithfulness reader — the
    finding↔critique gate, the reads-API verification surface — works unchanged.

    OFF-safe (C2b point 4): ``overall_score`` is pinned to **1.0** unless the
    ``LEGBA_STRUCTURAL_VERIFY_GATE`` flag is on (``gate`` overrides the env for
    tests). Off ⇒ the critique is written + the verdict shown (badge +
    verification detail) but effective_confidence is NEVER demoted (min(conf,
    1.0) == conf) — compute-and-show, do-not-gate. On ⇒ ``overall_score`` is the
    honest re-derivation fraction, so a miscount demotes via the same
    ``effective_confidence = min(confidence, overall_score)`` gate as any critic.

    ``data.verification`` carries a ``structural_verify: true`` MARKER and the
    ``structural_verified`` boolean the reads-API reads to flip the badge from
    ``unverified — structural`` to ``structural-verified``, plus the per-claim
    ledger so the operator sees WHAT was re-derived (and any miscount).
    """
    gated = structural_verify_gate_enabled() if gate is None else gate
    honest_score = report.score
    overall = honest_score if gated else 1.0
    verified = report.structural_verified

    if report.miscount:
        headline = f"FLAGGED — {report.miscount} miscount(s)"
    elif verified:
        headline = "verified"
    else:
        headline = "unverifiable"

    body_lines = [
        f"Structural verify of finding {analyzed_output_id}",
        f"  structural_verified={verified}",
        f"  claims: checkable={report.checkable} supported={report.supported} "
        f"miscount={report.miscount} unverifiable={report.unverifiable}",
        f"  gate={'on' if gated else 'off (compute-and-show, not demoting)'}"
        f" overall_score={overall:.2f}",
    ]
    for v in report.claim_verdicts[:20]:
        body_lines.append(f"  - [{v.verdict}] {v.statement[:160] or v.claim_id}")

    ledger = [v.as_dict() for v in report.claim_verdicts[:_STRUCTURAL_VERDICTS_CAP]]
    ledger_truncated = len(report.claim_verdicts) > _STRUCTURAL_VERDICTS_CAP
    return {
        "title": f"Structural verify ({headline})",
        "body": "\n".join(body_lines)[:65536],
        "confidence": overall,
        "tags": ["verify", "structural", "structural_verified" if verified else "structural_unverified"],
        "analyzed_output_id": analyzed_output_id,
        "analyzed_analyst_id": analyzed_analyst_id[:256],
        "analyzed_analyst_version": analyzed_analyst_version[:64],
        "scores": {"structural": honest_score},
        # The gate JOIN key (data->>'overall_score'). Pinned to 1.0 when the gate
        # flag is off so no consumer (reads-API is title-pinned to Faithfulness
        # anyway; the query-port laterals are unpinned) can demote a structural
        # finding — the OFF-safe default.
        "overall_score": overall,
        "data": {
            "verification": {
                # MARKER — this is a structural re-derivation verdict, not a
                # faithfulness one. The reads-API badge derivation keys on it.
                "structural_verify": True,
                "structural_verified": verified,
                "checkable_claims": report.checkable,
                "supported_claims": report.supported,
                "miscount_claims": report.miscount,
                "unverifiable_claims": report.unverifiable,
                "overall_score": round(overall, 4),
                "structural_score": round(honest_score, 4),
                "gate": gated,
                "claim_verdicts": ledger,
                "claim_verdicts_truncated": ledger_truncated,
                # V-G7 — the two keys the 08-03 counter audit went looking for and
                # did not find on 118 stamped-day structural critiques. Additive
                # JSONB; substrate_reads_api projects this block wholesale, so the
                # operator surface gets them for free. The version key is
                # DELIBERATELY named ``structural_pipeline_version`` and not
                # ``judge_pipeline_version``: every faithfulness consumer pins
                # ``title LIKE 'Faithfulness verify%'`` before reading a stamp, and
                # putting the faithfulness key on a row no judge ever graded would
                # be the one way to actually break them.
                "structural_pipeline_version": STRUCTURAL_PIPELINE_VERSION,
                "counters": dict(report.counters),
            }
        },
    }
