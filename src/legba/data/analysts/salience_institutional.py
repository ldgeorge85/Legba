# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``institutional_change`` salience class: a deterministic lift.

WHY THIS EXISTS
---------------
The ``signal_salience`` scorer's closed taxonomy has no political class. A
personnel change at chief-of-staff or cabinet level, an impeachment push
against a cabinet secretary, a coalition's mandate run-up or a parliament's
ratification of a defence pact all landed in ``other`` (or a near class) at
0.30-0.45, under the 0.50 ``MASS_FLOOR`` that ``cited_mass`` measures excess
above. A desk that cited them carried ``cited_mass`` of about zero (US
internal_stability 0.05 on 2026-11-07, with the Wiles to Miller swap in 48
signals), so the composition tier treated politics as background by
construction (world read dig 2026-11-07, item 1).

WHAT IT DOES
------------
:func:`recognise_institutional_change` reads the same text the scorer shows the
model (title plus a short body lead) and the source's S1-T8 class, and answers
with a :class:`InstitutionalMatch` when the text reports one of six acts:

* ``personnel``: appoint / name / replace / fire / resign / promote / sworn in
  next to an office at cabinet, chief-of-staff or command level.
* ``head_of_government``: a president, prime minister or chancellor sworn in,
  resigning or removed.
* ``removal``: impeachment, a no-confidence or censure motion, a recall.
* ``government_formation``: a mandate or bid to form a government, a
  recommendation for prime minister, a coalition break, a dissolution, a snap
  election.
* ``legislative_control``: a party winning, losing, flipping or retaking
  control of (or a majority in) a chamber.
* ``ratification``: a legislature ratifying or approving a treaty, pact or
  accession.
* ``emergency_rule``: a state of emergency, martial law, rule by decree, a
  suspended constitution.

It does NOT lift routine political chatter. A title framed as opinion,
editorial, commentary or analysis, a poll or survey, a campaign rally or
debate, or an explainer (a question title, "Who is", "What to know") never
lifts, and neither does a source of class ``analysis`` (think-tank and NGO
commentary). Those exclusions are read on the title only, so a wire report
whose body mentions a poll still lifts.

THE LIFT
--------
:func:`lifted_magnitude` raises the model's magnitude to the act's anchor
(0.60-0.70) and never lowers it. A model score already at or over the anchor
is left untouched. The anchor sits above ``MASS_FLOOR`` so the signal carries
mass, and below the kinetic / leader-death band (0.85-1.0) so a cabinet
reshuffle never outranks a strike. No LLM call: the same text and source class
give the same answer every time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

#: The class the lift stamps (registered in the ``salience_class`` family).
INSTITUTIONAL_CHANGE: str = "institutional_change"

#: Stamped on every lifted salience dict so a reader can tell the number was
#: raised by this rule set and which revision of it.
INSTITUTIONAL_LIFT_VERSION: str = "institutional_change.v1"

#: Per-act magnitude anchors. All sit over ``MASS_FLOOR`` (0.50) and under the
#: 0.85 kinetic / leader-death band.
ANCHORS: dict[str, float] = {
    "personnel": 0.60,
    "removal": 0.60,
    "government_formation": 0.60,
    "ratification": 0.60,
    "legislative_control": 0.65,
    "head_of_government": 0.70,
    "emergency_rule": 0.70,
}

#: Source classes the lift never applies to: think-tank and NGO commentary is
#: analysis of an act, not a report of one.
NO_LIFT_SOURCE_CLASSES: frozenset[str] = frozenset({"analysis", "claim"})  # C-1: a claim is not an office change

_W = r"[^.;:!?]{0,60}?"   # a short same-clause gap

# ---------------------------------------------------------------------------
# Offices: cabinet, chief-of-staff and command level only.
# ---------------------------------------------------------------------------
_DEPT = (
    r"(?:defen[cs]e|war|state|treasury|finance|foreign(?: affairs)?|home|interior|"
    r"justice|homeland security|health|energy|commerce|labou?r|education|"
    r"transport(?:ation)?|agriculture|economy|trade|security|intelligence)"
)
_OFFICE = (
    r"(?:"
    r"(?:white house |deputy )?chief of staff"
    r"|chief of (?:the )?(?:general |defen[cs]e |army |naval |air )?staff"
    r"|cabinet(?: secretary| minister| post| seat)?"
    r"|(?<!chief )(?<!deputy )(?:prime |" + _DEPT + r" )?minister(?! of state)"
    r"|(?:secretary of " + _DEPT + r"|" + _DEPT + r" secretary|pentagon chief)"
    r"|attorney[- ]general|national security advis[eo]r"
    r"|(?:central bank|reserve bank|fed) (?:governor|chair(?:man|woman)?|chief)"
    r"|(?:army|military|navy|naval|air force|armed forces|defen[cs]e|intelligence|spy) chief"
    r"|commander[- ]in[- ]chief"
    r"|(?:top|supreme|army|navy|naval|air force|military|armed forces|fleet|theatre|theater|"
    r"joint forces|ground forces|district|front|special forces|marine corps) commander"
    r"|commander of (?:the )?(?:army|navy|air force|armed forces|forces|fleet|ground forces|"
    r"military district|theatre command|theater command|central command|[a-z]+ front)"
    r"|head of (?:the )?(?:army|military|armed forces|navy|air force|intelligence|"
    r"security service|mossad|shin bet|cia|fbi|nsa|irgc|joint chiefs)"
    r"|chairman of the joint chiefs"
    r"|(?:house |senate |parliament(?:ary)? |knesset |assembly )?speaker"
    r"|(?:secretary[- ]general|director[- ]general|director) of (?:the )?"
    r"(?:cia|fbi|nsa|mossad|shin bet|intelligence|alliance|nato)"
    r"|(?:un |u\.n\. |united nations |nato |alliance )secretary[- ]general"
    r")"
)
_PERSONNEL_VERB = (
    r"(?:appoint(?:s|ed|ing|ment)?|names?|named|naming|nominat(?:es?|ed|ing|ion)"
    r"|replac(?:es?|ed|ing)|fires?|fired|sacks?|sacked|dismiss(?:es|ed|al)?"
    r"|ousts?|ousted|ouster|resign(?:s|ed|ation)?|steps? down|stepped down|quits?"
    r"|promot(?:es?|ed|ing|ion)|sworn in|swears in|takes office|took office"
    r"|succeeds?|succeeded|to become|will become|becomes|became|tapped|taps"
    r"|picks? .{0,40}? as|announc(?:es|ed) .{0,60}? as|removed|removes"
    r"|reshuffl(?:es?|ed)|shake-?up)"
)
_HEAD = r"(?:president|prime minister|premier|chancellor|pm)"

_ACCORD = (
    r"(?:pact|treaty|alliance|accession|accord|convention|protocol"
    r"|(?:defen[cs]e|security|military|peace|border|mutual|strategic|partnership|nuclear)"
    r"(?: cooperation)? (?:agreement|deal))"
)
_CHAMBER = (
    r"(?:the\s+|both\s+)?(?:u\.?s\.?\s+)?"
    r"(?:house|senate|congress|parliament|chamber|chambers|legislature|bundestag|"
    r"knesset|national assembly|duma|diet|lok sabha)"
)
_IMPEACH_PROCEEDING = (
    r"(?:proceedings?|articles?|vote|inquiry|trial|push|motion|bid|process|case|"
    r"resolution|hearings?|drive|effort)"
)

#: ``(subclass, pattern, title_only)`` in precedence order (highest anchor
#: first). A ``title_only`` act is read on the title alone: a body that
#: mentions last week's result as background is not a report of it.
_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("emergency_rule", re.compile(
        r"\b(?:declar|impos|extend|lift|proclaim|invok)\w*" + _W
        + r"\b(?:state of (?:emergency|siege)|martial law|emergency rule)\b"
        r"|\b(?:state of (?:emergency|siege)|martial law|emergency rule)\b" + _W
        + r"\b(?:declared|imposed|extended|lifted|proclaimed|invoked)\b"
        r"|\bsuspend\w*\s+(?:the\s+)?constitution\b|\brule by decree\b", re.I), False),
    ("head_of_government", re.compile(
        r"\b(?:sworn in|inaugurated|takes office|took office|elected)\s+as\s+(?:the\s+)?"
        r"(?:new\s+|next\s+|interim\s+)?" + _HEAD + r"\b"
        r"|\b" + _HEAD + r"\s+(?:\w+\s+){0,3}?(?:resigns|resigned|steps down|stepped down|"
        r"ousted|removed from office|impeached)\b"
        r"|\bnew\s+" + _HEAD + r"\s+(?:sworn in|takes office|named|appointed)\b", re.I), False),
    ("legislative_control", re.compile(
        r"\b(?:wins?|won|winning|loses?|lost|losing|flips?|flipped|retakes?|retook|"
        r"seiz(?:es?|ed)|takes?|took|gains?|gained|clinch(?:es|ed)?|secures?|secured|"
        r"wrests?|wrested)\s+"
        r"(?:back\s+)?(?:control of|(?:a|the|an outright|its) majority (?:in|of))\s+"
        + _CHAMBER + r"\b", re.I), True),
    ("removal", re.compile(r"\bimpeach(?:ment|ed|es|ing)?\b", re.I), True),
    ("removal", re.compile(
        r"\barticles of impeachment\b|\bimpeachment\s+" + _IMPEACH_PROCEEDING + r"\b"
        r"|\b(?:vote|votes|voted|move|moves|push|pushes|bid|seeks?|plans?|motion)\s+to\s+impeach\b"
        r"|\bimpeached\b"
        r"|\bno[- ]confidence (?:vote|motion)\b|\b(?:vote|motion) of no[- ]confidence\b"
        r"|\bcensure motion\b|\bremoval from office\b"
        r"|\brecall (?:vote|election|referendum|petition)\b", re.I), False),
    ("ratification", re.compile(
        r"\bratif(?:y|ies|ied|ying)\b" + _W + r"\b" + _ACCORD + r"\b"
        r"|\b" + _ACCORD + r"\b" + _W + r"\bratified\b"
        r"|\b(?:parliament|legislature|senate|knesset|assembly|lawmakers|bundestag|congress|cabinet)\s+"
        r"(?:\w+\s+){0,2}?(?:approves?|approved|passes|passed|endorses|endorsed)\b" + _W
        + r"\b(?:pact|treaty|accord|alliance|accession)\b", re.I), False),
    ("government_formation", re.compile(
        r"\bmandate to (?:form|lead|govern)\b|\bpresidential mandate\b"
        r"|\b(?:hand|grant|give|award)\w*\s+(?:\w+\s+){0,3}?(?:the\s+)?mandate\b"
        r"|\b(?:tasked|tapped|chosen|asked) (?:with|to) form(?:ing)?\b"
        r"|\bto form (?:a |the |israel'?s |its )?(?:new |next |broad |unity |governing |coalition )*"
        r"(?:government|coalition|cabinet)\b"
        r"|\bform(?:s|ed|ing)? (?:a |the )?(?:new |next |broad |unity |governing )*"
        r"(?:government|coalition)\b"
        r"|\b(?:recommend\w*|back(?:s|ed|ing)?|endors\w*|nominat\w*|nods?|support\w*)\b" + _W
        + r"\b(?:as|for|to be|to become) (?:the )?(?:next |new )?"
        r"(?:prime minister|pm|premier|chancellor)\b"
        r"|\bcoalition\s+(?:collapses|collapsed|breaks up|broke up|crumbles|falls|fell|"
        r"splits|split)\b"
        r"|\b(?:quits?|leaves|left|exits?|exited|withdraws?|withdrew|pulls? out of)\s+"
        r"(?:the\s+)?(?:ruling\s+|governing\s+)?(?:coalition|government)\b"
        r"|\bgovernment (?:collapses|collapsed|falls|fell)\b"
        r"|\bsnap (?:election|poll)s? (?:called|set)\b|\bcalls? (?:a )?snap election\b"
        r"|\bdissolv\w*\s+(?:the\s+)?(?:parliament|knesset|assembly|national assembly|"
        r"bundestag|lower house|diet|duma)\b"
        r"|\b(?:parliament|knesset|assembly|bundestag|diet) (?:is )?dissolved\b", re.I), False),
    ("personnel", re.compile(
        r"\b" + _PERSONNEL_VERB + r"\b" + _W + r"\b(?:new |next |acting |interim )?"
        + _OFFICE + r"\b"
        r"|\b" + _OFFICE + r"\b" + _W + r"\b(?:resigns?|resigned|steps? down|stepped down|"
        r"quits?|fired|sacked|dismissed(?!\s+(?:the\s+)?(?:reports?|claims?|speculation|rumou?rs?|"
        r"calls?|criticism|concerns?|allegations?|suggestions?))|ousted|replaced|removed|"
        r"appointed|named|sworn in|takes office)\b", re.I), False),
)

#: An act framed as a possibility, a forecast or a denial is not an act. Read
#: in the few words before the cue (and a modal right after it).
_HEDGE_BEFORE = re.compile(
    r"\b(?:if|could|would|might|may|whether|predicted to|poised to|expected to|set to|"
    r"likely to|hopes? to|bid to|prevent|stop|denies|denied|deny|rules? out|ruled out|"
    r"rejects?|dismisses|warns? against|potential|possible|not|never|no longer|"
    r"declines? to|declined to|refuses? to|refused to)\b[^.;:!?]{0,30}$",
    re.I)
_HEDGE_AFTER = re.compile(r"^[^.;:!?]{0,12}\b(?:could|might|may|would)\b", re.I)
_HEDGE_INSIDE = re.compile(r"\b(?:could|might|may|would|denies|denied|rules? out)\b", re.I)
#: A state of emergency over a drought, a storm or a flood is disaster
#: management, not emergency rule (``disaster_natural`` already covers it).
_DISASTER = re.compile(
    r"\b(?:el ni[nñ]o|la ni[nñ]a|drought|floods?|flooding|rains?|rainfall|storms?|hurricane|"
    r"typhoon|cyclone|wildfires?|fires?|earthquake|quake|heat ?wave|snow|blizzard|outbreak|"
    r"epidemic|disease|cholera|landslides?|volcan\w*|power outages?|blackouts?|counties)\b",
    re.I)

# ---------------------------------------------------------------------------
# Routine political chatter: read on the TITLE only.
# ---------------------------------------------------------------------------
_OPINION = re.compile(
    r"\b(?:opinion|op-?ed|editorial|commentary|column|columnist|perspective|"
    r"analysis|letters? to the editor|viewpoint)\b"
    r"|\bbluf:?\**\s*(?:an?|the)\s+[^.]{0,60}\b(?:op-?ed|commentary|editorial|column|opinion)\b",
    re.I)
_POLL = re.compile(
    r"\b(?:polls?|polling|pollsters?|survey|surveys|approval ratings?|exit polls?|"
    r"opinion polls?)\b", re.I)
_CAMPAIGN = re.compile(
    r"\b(?:rally|rallies|rallied|campaign trail|campaign stop|campaigning|stump|"
    r"fundraiser|town hall|debate|debates|canvass\w*)\b", re.I)
_EXPLAINER = re.compile(
    r"^\W*(?:explainer|who is|who are|who could|who will|what is|what are|what to know|"
    r"what now|what next|what's next|how\b|why\b|five things|\d+ things|things to know|"
    r"here'?s|q&a|profile)\b",
    re.I)
#: Offices and votes outside the state: a faculty's no-confidence vote or a
#: crew commander is not an institutional change of a polity.
_NON_STATE = re.compile(
    r"\b(?:universit\w*|college|faculty|school|students?|club|coach|team|squad|league|"
    r"cosmonauts?|astronauts?|crew|church|diocese|ceo|chief executive|"
    r"board of directors|shareholders?)\b", re.I)


@dataclass(frozen=True)
class InstitutionalMatch:
    """One recognised act: its sub-class, its magnitude anchor and the cue."""

    subclass: str
    anchor: float
    cue: str


def _title_of(text: str) -> str:
    """The title half of the scorer's ``title — body`` text."""
    head, sep, _ = (text or "").partition(" — ")
    return head if sep else (text or "")[:200]


def routine_reason(text: str) -> str | None:
    """Why a text reads as routine political chatter or a non-state item
    (``opinion`` / ``explainer`` / ``poll`` / ``campaign`` / ``non_state``),
    judged on its title; ``None`` otherwise."""
    title = _title_of(text)
    if _OPINION.search(title) or _OPINION.search((text or "")[:200]):
        return "opinion"
    if _EXPLAINER.search(title) or title.rstrip(" *").endswith("?"):
        return "explainer"
    if _POLL.search(title):
        return "poll"
    if _CAMPAIGN.search(title):
        return "campaign"
    if _NON_STATE.search(title):
        return "non_state"
    return None


def _hedged(scope: str, m: re.Match[str]) -> bool:
    return bool(
        _HEDGE_BEFORE.search(scope[max(0, m.start() - 40):m.start()])
        or _HEDGE_AFTER.search(scope[m.end():m.end() + 16])
        or _HEDGE_INSIDE.search(m.group(0))
    )


def recognise_institutional_change(
    text: str, *, source_class: str | None = None,
) -> InstitutionalMatch | None:
    """The act ``text`` reports, or ``None``.

    ``text`` is the scorer's own signal text (title plus a short body lead).
    Deterministic: a lexicon over the text plus the source's S1-T8 class. The
    title is read first and the body lead only when the title names no act;
    within one scope the first act in :data:`_PATTERNS` order wins."""
    if not isinstance(text, str) or not text.strip():
        return None
    if (source_class or "").strip().lower() in NO_LIFT_SOURCE_CLASSES:
        return None
    if routine_reason(text) is not None:
        return None
    title = _title_of(text)
    for scope, is_title in ((title, True), (text, False)):
        for subclass, rx, title_only in _PATTERNS:
            if title_only and not is_title:
                continue
            if subclass == "emergency_rule" and _DISASTER.search(text):
                continue
            for m in rx.finditer(scope):
                if _hedged(scope, m):
                    continue
                return InstitutionalMatch(subclass, ANCHORS[subclass], m.group(0)[:120])
    return None


def lifted_magnitude(magnitude: float | None, match: InstitutionalMatch | None) -> float | None:
    """The magnitude after the lift: ``max(magnitude, anchor)``. A degraded
    (``None``) magnitude stays ``None``: an unscored row is unmeasured, and the
    lift raises a score, it never invents one."""
    if magnitude is None or match is None:
        return magnitude
    return max(float(magnitude), match.anchor)


def lift_stamp(match: InstitutionalMatch, *, from_magnitude: float, from_class: str) -> dict[str, Any]:
    """The ``lift`` sub-dict a lifted salience row carries."""
    return {
        "rule": INSTITUTIONAL_LIFT_VERSION,
        "subclass": match.subclass,
        "cue": match.cue,
        "from_magnitude": from_magnitude,
        "from_class": from_class,
    }


def lift_salience(
    salience: Mapping[str, Any] | None, text: str, *, source_class: str | None = None,
    bound_class: str | None = INSTITUTIONAL_CHANGE,
) -> dict[str, Any] | None:
    """Re-apply the lift to an already-stamped ``signals.salience`` dict.

    Returns the new dict when the lift raises the magnitude, else ``None`` (no
    change: not recognised, already at or over the anchor, degraded, or
    already lifted). ``bound_class`` is the class to stamp; pass ``None`` to
    keep the model's class (the ``salience_class`` family does not hold
    ``institutional_change`` yet). Pure: the caller writes."""
    if not isinstance(salience, Mapping) or salience.get("degraded"):
        return None
    if isinstance(salience.get("lift"), Mapping):
        return None
    try:
        mag = float(salience.get("magnitude"))
    except (TypeError, ValueError):
        return None
    match = recognise_institutional_change(text, source_class=source_class)
    new = lifted_magnitude(mag, match)
    if match is None or new is None or new <= mag:
        return None
    out = dict(salience)
    from_class = str(salience.get("event_class") or "other")
    out["magnitude"] = new
    if bound_class:
        out["event_class"] = bound_class
    out["lift"] = lift_stamp(match, from_magnitude=mag, from_class=from_class)
    return out


__all__ = [
    "ANCHORS", "INSTITUTIONAL_CHANGE", "INSTITUTIONAL_LIFT_VERSION",
    "InstitutionalMatch", "NO_LIFT_SOURCE_CLASSES",
    "lift_salience", "lift_stamp", "lifted_magnitude",
    "recognise_institutional_change", "routine_reason",
]
