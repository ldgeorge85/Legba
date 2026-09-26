# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WHEN AN EXTERNAL CONTRADICTION MAY WAKE SOMEBODY — W-8 (design §4, §3.5.7).

THE ONE SENTENCE THIS MODULE EXISTS FOR, quoted from the design's own list of
what the instrument cannot claim:

    *"A CONTRADICTED verdict is one grader's reading of one span on one page.
    Two families must agree before it pages, and even then it is an operator
    event, not a retraction."*

At width the standing auditor produces on the order of 470 verdicts a day. A
CONTRADICTED among them is the single most consequential thing this platform can
say — *the world appears to disagree with a read we published* — and it is also
the verdict most likely to be an artefact: a stale page, an unregistered domain,
a span the grader half-remembered, a low-severity footnote. So the page is gated
on FIVE preconditions, each of which failed somewhere in the round record:

1. **The verdict is CONTRADICTED.** SUPPORTED and NOT_FOUND never page.
   NOT_FOUND especially: it is a statement about the SEARCH, and paging on it
   would turn every SearXNG engine ban into an operator event.
2. **Tier 1 or Tier 2.** R2-C4 case #10 was overturned as scored because the
   scorer never read the tier. An unregistered domain (F-3's ``tier_unknown``)
   is COUNTED and PUBLISHED — that is the whole point of the visible class — but
   it does not page: publishing a number nobody is woken by is a different act
   from waking somebody.
3. **The decisive span resolved verbatim** in a fetch of the cited page, through
   the shared fold. A page can be in a result set and not say what the grader
   claims it says; R1/R2/R3's span sweeps are why those rounds' verdicts survived
   audit.
4. **The audited claim's own standing severity is high or critical.** The floor
   is the AUDITED read's band, never the auditor's opinion — a contradiction on a
   ``severity:low`` desk read is a data-quality note, and the auditor already
   holds this rule for the row it writes.
5. **A second grader family confirmed it.** §2.4 double-grades 100% of
   CONTRADICTED over the byte-identical cached evidence envelope precisely so
   this precondition can exist. One family's reading is a ledger row.

**A single-rater CONTRADICTED writes a row and does NOT page.** That is the
designed outcome, not a degradation: the verdict is in the ledger, it is in the
published number, it is in the read's critique — it simply does not wake anyone
until a second family has seen the same span on the same bytes.

WHY THIS IS A SIBLING MODULE. Same reason ``_daily_page_budget`` is: the
alert_trigger_scan module-size ceiling is a ratchet, and a per-class decision
with a page of rationale is exactly the cohesive unit the gate wants extracted.
Every name here is re-exported from ``alert_trigger_scan`` so ``ats.*`` keeps
working for callers and tests.

WHAT THIS MODULE DOES NOT DECIDE. Whether the page actually goes out: that is
the fleet-wide daily budget's call (5/day, of which this class may take at most
:data:`EXTERNAL_AUDIT_PAGE_CAP`), applied after every filter. A precondition
failure means "never"; a budget deferral means "not today, and the row says so".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ...provenance.external_span_check import (
    TIER_CLASS_1,
    TIER_CLASS_2,
    VERDICT_CONTRADICTED,
)

#: The trigger class. The SAME string the standing auditor already stamps on the
#: rows it writes (``standing_auditor.ALERT_TRIGGER_CLASS``) and on its
#: ``alert_trigger_watermarks`` partition, so registering it here is a vocabulary
#: registration and not a rename — no migration, no watermark discontinuity.
TRIGGER_CLASS = "external_audit"

#: Outward channel identity. Mirrors the geo_convergence precedent: the class
#: keeps the name it already carries in its own ledger rows, so filtering by
#: source across the fold is unaffected. The dispatcher's cooldown is keyed by
#: sink_kind, never channel_name, so this has no bearing on dedup semantics.
CHANNEL_NAME = "external_audit"

#: Per-class verify posture for the outward payload. An external audit is not a
#: faithfulness verdict and must never be read as one — the read may be perfectly
#: faithful to the signals it cited and still be wrong about the world.
UNVERIFIED_REASON = (
    "external world check, not a faithfulness verdict: two grader families "
    "agreed that a Tier-1/2 source published inside the read's own evidence "
    "window carries a resolved span contradicting this claim"
)

#: The per-class share of the fleet-wide daily page budget (design §4 W-8). Two
#: a day, inside the existing 5/day fleet cap. Deliberately BELOW the generic
#: ``DEFAULT_BUDGET_PER_KIND_CAP`` of 3: this class is new, it is the only one
#: whose subject is "a read we published may be wrong", and a new class that can
#: take 60% of the day's budget on its first live week is how an alert plane
#: loses an operator's attention permanently.
EXTERNAL_AUDIT_PAGE_CAP = 2

#: Severities whose contradiction is load-bearing. Same vocabulary the auditor's
#: own ``is_high_severity`` uses; named here so the paging bar is readable in one
#: place rather than inferred from a rank table.
PAGING_SEVERITIES: frozenset[str] = frozenset({"high", "critical"})

#: Source tier classes admissible for a PAGE. ``tier_unknown`` is deliberately
#: absent — see precondition 2 in the module docstring.
PAGING_TIER_CLASSES: frozenset[str] = frozenset({TIER_CLASS_1, TIER_CLASS_2})

#: The five precondition names, in the order :func:`external_audit_page_decision`
#: evaluates them. Exported so the receipt, the test and an operator's "why did
#: this not page?" all read the same list.
PAGING_PRECONDITIONS: tuple[str, ...] = (
    "verdict_contradicted",
    "tier_1_or_2",
    "span_resolved",
    "severity_high_or_critical",
    "audit_rater_confirmed",
)


@dataclass(frozen=True)
class PageDecision:
    """Why an external-audit contradiction may (or may not) wake somebody.

    ``pages`` is the answer; ``failed`` is the complete list of preconditions
    that did not hold, never just the first. A contradiction that is both
    low-severity and single-rated is two different reasons not to page, and a
    receipt naming one of them invites a fix that changes nothing.
    """

    pages: bool
    passed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        if self.pages:
            return "all five paging preconditions hold"
        return "withheld — " + ", ".join(self.failed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "preconditions_passed": list(self.passed),
            "preconditions_failed": list(self.failed),
            "reason": self.reason,
            "trigger_class": TRIGGER_CLASS,
        }


def external_audit_page_decision(verdict: Mapping[str, Any]) -> PageDecision:
    """Evaluate all five preconditions on one graded claim. Never raises.

    ``verdict`` is the ledger-row-shaped mapping the auditor's drain already
    holds: the :class:`~...provenance.external_span_check.SpanCheck` projection
    (``verdict``, ``decisive_tier_class``, ``span_resolved``) plus the audited
    claim's own ``claim_severity`` and the double-grade outcome
    (``audit_verdict`` — the fourth family's verdict over the byte-identical
    envelope, or absent when the claim was not double-graded).

    Every precondition is evaluated; none short-circuits. A missing key is a
    FAILED precondition, never an assumed one: an absent ``audit_verdict`` means
    no second family looked, which is exactly the single-rater case that must
    write a row and not page.
    """
    passed: list[str] = []
    failed: list[str] = []

    def _check(name: str, ok: bool) -> None:
        (passed if ok else failed).append(name)

    _check(
        "verdict_contradicted",
        str(verdict.get("verdict") or "").upper() == VERDICT_CONTRADICTED,
    )
    _check(
        "tier_1_or_2",
        str(verdict.get("decisive_tier_class") or "") in PAGING_TIER_CLASSES,
    )
    _check("span_resolved", verdict.get("span_resolved") is True)
    _check(
        "severity_high_or_critical",
        str(
            verdict.get("claim_severity") or verdict.get("severity") or ""
        ).strip().lower() in PAGING_SEVERITIES,
    )
    # The audit rater must have seen the SAME claim and reached the SAME
    # verdict. A rater that disagreed is the instrument working — the row keeps
    # both verdicts and the week's overlap number moves — and it is emphatically
    # not a page.
    _check(
        "audit_rater_confirmed",
        str(verdict.get("audit_verdict") or "").upper() == VERDICT_CONTRADICTED,
    )

    return PageDecision(
        pages=not failed, passed=tuple(passed), failed=tuple(failed)
    )


def external_audit_pages(verdict: Mapping[str, Any]) -> bool:
    """``True`` iff all five preconditions hold — the one-line caller's form."""
    return external_audit_page_decision(verdict).pages


__all__ = [
    "CHANNEL_NAME",
    "EXTERNAL_AUDIT_PAGE_CAP",
    "PAGING_PRECONDITIONS",
    "PAGING_SEVERITIES",
    "PAGING_TIER_CLASSES",
    "TRIGGER_CLASS",
    "UNVERIFIED_REASON",
    "PageDecision",
    "external_audit_page_decision",
    "external_audit_pages",
]
