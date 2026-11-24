# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Panel tallies: majority, quorum and Borda (plan P4 §2.4 ``panel``, lane G-3).

Rules, all pure:

* **Present judges.** A judge whose verdict row is missing (absent, failed,
  late) is absent. The tally runs only if an ODD number of judges is present
  and that number is at least ``k`` (majority/Borda: a strict majority of the
  panel); otherwise every candidate is ``no_quorum``.
* **majority** — per candidate: ``stands`` if more than half the present
  judges say stands, ``falls`` if more than half say falls, else ``tie``.
* **quorum {k, of, threshold}** — ``stands`` if at least ``k`` say stands and
  their share of the present judges is at least ``threshold``; ``falls`` the
  same way; else ``tie``.
* **borda** — each present judge ranks the candidates (``ranking``); a
  candidate at index i of n earns ``n - 1 - i`` points; unranked candidates
  earn nothing. The unique top scorer ``stands``; candidates sharing the top
  score are a ``tie``; the rest ``fall``.
* **A tie stays a tie.** Nothing breaks it: no seniority, no order, no coin.
* **Gate judges** (``gates_first``) run before the vote; a candidate any gate
  judge rules ``falls`` is removed (kept in the result as ``removed``).
* **Dissent is kept**: every vote against a candidate's outcome is listed.

The panel's overall ``outcome`` is the one candidate's outcome when there is
one; with several it is their common outcome, ``no_quorum`` if the panel had
none, else ``mixed`` (a value this lane adds; see the report). For Borda it
is ``stands`` when one candidate leads and ``tie`` when the lead is shared.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .spec import Tally


@dataclass(frozen=True)
class Vote:
    judge: str
    candidate: str
    outcome: str  # stands | falls | unsure
    reason: str = "other"
    confidence: float | None = None


@dataclass(frozen=True)
class CandidateTally:
    candidate: str
    outcome: str  # stands | falls | tie | no_quorum
    stands: int = 0
    falls: int = 0
    unsure: int = 0
    points: int | None = None
    votes: Mapping[str, str] = field(default_factory=dict)
    removed: bool = False

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"candidate": self.candidate, "outcome": self.outcome,
                               "stands": self.stands, "falls": self.falls,
                               "unsure": self.unsure, "votes": dict(self.votes)}
        if self.points is not None:
            out["points"] = self.points
        if self.removed:
            out["removed"] = True
        return out


@dataclass(frozen=True)
class Dissent:
    judge: str
    candidate: str
    vote: str
    outcome: str

    def as_dict(self) -> dict[str, str]:
        return {"judge": self.judge, "candidate": self.candidate, "vote": self.vote,
                "outcome": self.outcome}


@dataclass(frozen=True)
class TallyResult:
    rule: str
    outcome: str
    candidates: tuple[CandidateTally, ...]
    present: tuple[str, ...]
    absent: tuple[str, ...]
    removed: tuple[str, ...] = ()
    dissent: tuple[Dissent, ...] = ()

    def by_candidate(self) -> dict[str, CandidateTally]:
        return {c.candidate: c for c in self.candidates}

    def as_row(self) -> dict[str, Any]:
        """The ``tally/1`` payload."""
        return {"rule": self.rule, "outcome": self.outcome,
                "candidates": [c.as_dict() for c in self.candidates],
                "present": list(self.present), "absent": list(self.absent),
                "removed": list(self.removed), "dissent": [d.as_dict() for d in self.dissent]}


#: A ``verdict/2`` sentence problem → the verdict reason it counts as.
PROBLEM_REASON = {
    "number_changed": "unsupported", "date_changed": "unsupported",
    "subject_changed": "unsupported", "adds_cause_or_intent": "unsupported",
    "adds_forecast": "unsupported", "claim_as_fact": "unsupported",
    "quote_does_not_say_it": "unsupported", "no_quote": "unsupported",
    "contradicts_quote": "contradicted", "does_not_follow": "illogical",
    "off_question": "off_question",
}


def entry_outcome(v: Mapping[str, Any]) -> tuple[str, str]:
    """One verdict entry's counted (outcome, reason). A ``verdict/2`` entry
    whose sentence checks include one that does not hold FALLS, whatever its
    ``outcome`` says — a judge cannot find a sentence unsupported and let the
    candidate stand. Code applies the rubric's rule; it never makes a
    candidate stand that the judge ruled against."""
    outcome = str(v.get("outcome", "unsure"))
    reason = str(v.get("reason", "other"))
    bad = [c for c in v.get("checks") or () if isinstance(c, Mapping) and c.get("holds") is False]
    if bad:
        problem = str(bad[0].get("problem") or "other")
        return "falls", reason if outcome == "falls" and reason != "supported" \
            else PROBLEM_REASON.get(problem, "other")
    return outcome, reason


def votes_of(judge: str, payload: Mapping[str, Any] | None,
             candidates: Sequence[str]) -> dict[str, Vote]:
    """One judge's votes by candidate, from its ``verdict/1`` payload. A
    candidate the judge did not rule on counts as ``unsure``."""
    out: dict[str, Vote] = {}
    for v in (payload or {}).get("verdicts") or ():
        cand = str(v.get("candidate"))
        if cand in candidates and cand not in out:
            outcome, reason = entry_outcome(v)
            out[cand] = Vote(judge, cand, outcome, reason, v.get("confidence"))
    for cand in candidates:
        out.setdefault(cand, Vote(judge, cand, "unsure", "other"))
    return out


def gate_removals(candidates: Sequence[str],
                  gate_verdicts: Mapping[str, Mapping[str, Any] | None]) -> dict[str, list[str]]:
    """Candidates a gate judge ruled ``falls``, with the judges that did."""
    removed: dict[str, list[str]] = {}
    for judge in sorted(gate_verdicts):
        for cand, vote in votes_of(judge, gate_verdicts[judge], candidates).items():
            if vote.outcome == "falls":
                removed.setdefault(cand, []).append(judge)
    return removed


def _needed(rule: Tally, n_judges: int) -> int:
    if rule.rule == "quorum" and rule.k:
        return rule.k
    return n_judges // 2 + 1


def _overall(cands: Sequence[CandidateTally]) -> str:
    live = [c.outcome for c in cands if not c.removed]
    if not live:
        return "falls" if cands else "no_quorum"
    if "no_quorum" in live:
        return "no_quorum"
    if len(set(live)) == 1:
        return live[0]
    return "mixed"


def _dissent(cand: CandidateTally) -> list[Dissent]:
    if cand.outcome not in ("stands", "falls"):
        return []
    return [Dissent(j, cand.candidate, v, cand.outcome)
            for j, v in sorted(cand.votes.items()) if v != cand.outcome]


def tally(rule: Tally, judges: Sequence[str], candidates: Sequence[str],
          verdicts: Mapping[str, Mapping[str, Any] | None],
          removed: Mapping[str, Sequence[str]] | None = None) -> TallyResult:
    """Tally one panel. ``verdicts`` maps every voting judge to its verdict
    payload, or ``None`` when the judge is absent."""
    removed = removed or {}
    present = tuple(j for j in judges if verdicts.get(j) is not None)
    absent = tuple(j for j in judges if verdicts.get(j) is None)
    live = [c for c in candidates if c not in removed]
    gone = [CandidateTally(c, "falls", removed=True,
                           votes={j: "falls" for j in removed[c]}) for c in candidates
            if c in removed]
    quorate = len(present) % 2 == 1 and len(present) >= _needed(rule, len(judges))
    if not quorate:
        cands = [CandidateTally(c, "no_quorum") for c in live] + gone
        return TallyResult(rule.rule, "no_quorum" if live else _overall(gone), tuple(cands),
                           present, absent, tuple(removed))
    if rule.rule == "borda":
        cands = _borda(present, live, verdicts)
    else:
        cands = []
        for c in live:
            votes = {j: votes_of(j, verdicts[j], live)[c].outcome for j in present}
            s = sum(v == "stands" for v in votes.values())
            f = sum(v == "falls" for v in votes.values())
            u = len(votes) - s - f
            cands.append(CandidateTally(c, _decide(rule, s, f, len(present)), s, f, u,
                                        votes=votes))
    all_cands = tuple(cands) + tuple(gone)
    dissent = tuple(d for c in cands for d in _dissent(c))
    overall = _overall(all_cands)
    if rule.rule == "borda":
        dissent = tuple(_borda_dissent(present, cands, verdicts))
        leaders = [c for c in cands if c.outcome in ("stands", "tie")]
        overall = "stands" if len(leaders) == 1 else ("tie" if leaders else "no_quorum")
    return TallyResult(rule.rule, overall, all_cands, present, absent, tuple(removed), dissent)


def _decide(rule: Tally, s: int, f: int, present: int) -> str:
    if rule.rule == "quorum":
        k = rule.k or present // 2 + 1
        thr = rule.threshold or 0.0
        if s >= k and s / present >= thr:
            return "stands"
        if f >= k and f / present >= thr:
            return "falls"
        return "tie"
    if 2 * s > present:
        return "stands"
    if 2 * f > present:
        return "falls"
    return "tie"


def _ranking(payload: Mapping[str, Any] | None, live: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for c in (payload or {}).get("ranking") or ():
        if str(c) in live and str(c) not in seen:
            seen.append(str(c))
    return seen


def _borda(present: Sequence[str], live: Sequence[str],
           verdicts: Mapping[str, Mapping[str, Any] | None]) -> list[CandidateTally]:
    n = len(live)
    points = {c: 0 for c in live}
    tops: dict[str, str] = {}
    for j in present:
        ranking = _ranking(verdicts[j], live)
        for i, c in enumerate(ranking):
            points[c] += n - 1 - i
        if ranking:
            tops[j] = ranking[0]
    best = max(points.values(), default=0)
    leaders = [c for c in live if points[c] == best]
    out = []
    for c in live:
        if points[c] == best:
            outcome = "stands" if len(leaders) == 1 else "tie"
        else:
            outcome = "falls"
        votes = {j: ("stands" if tops.get(j) == c else "falls") for j in present}
        out.append(CandidateTally(c, outcome, sum(v == "stands" for v in votes.values()),
                                  sum(v == "falls" for v in votes.values()), 0, points[c], votes))
    return out


def _borda_dissent(present: Sequence[str], cands: Sequence[CandidateTally],
                   verdicts: Mapping[str, Mapping[str, Any] | None]) -> list[Dissent]:
    winners = [c.candidate for c in cands if c.outcome == "stands"]
    if len(winners) != 1:
        return []
    live = [c.candidate for c in cands]
    out = []
    for j in present:
        ranking = _ranking(verdicts[j], live)
        if ranking and ranking[0] != winners[0]:
            out.append(Dissent(j, winners[0], f"ranked {ranking[0]} first", "stands"))
    return out
