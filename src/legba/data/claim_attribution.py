# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-2 — does a sentence present a claim AS a claim? (the ``claim_as_fact`` rule)

A sentence that rests only on CLAIM rows may say what the claimant claimed; it
may not state the claimed event as fact. C-1 decided that with one test: the
claimant's name appears AND any attribution verb appears anywhere. That test
had two kinds of error (measured on the labelled set in
``tests/data_pkg/fixtures/claim_attribution_set.jsonl``, see
``tests/data_pkg/test_claim_attribution.py``):

* it MISSED event statements that happen to carry a verb somewhere —
  "LockBit claimed the attack, and Acme's plant was shut down for a week"
  passed, because "claimed" is in it;
* it FLAGGED fair attributions worded another way — "the tracker lists ...",
  "Acme appears on the group's leak site", passive forms, other languages.

The rule here reads the sentence in CLAUSES:

1. Quoted text is the claimant's own words: it is set aside first.
2. The sentence is split at the joints where a new assertion can start
   (``;``, a spaced dash, ", and" / ", but" / "and <Capitalised>", ", which",
   "after / following / despite / because ..." — and a LEADING subordinate
   clause up to its comma).
3. A clause is FRAMED when it carries an attribution frame bound to the
   claimant: "<claimant> [<artifact>] <verb>" ("LockBit posted", "the group's
   leak site lists"), "according to / per / as claimed by <claimant>",
   "<verb>ed by <claimant>", "listed / posted / appears on <claimant>'s
   <site>", "<claimant>'s claim / post / listing", and the same in French,
   Spanish, German, Portuguese and Italian. The claimant is the stamp's
   claimant, its leading name, the descriptor's declared aliases, or a role
   noun (the group, the gang, the tracker ...); a pronoun subject ("it says")
   counts once the claimant is named or role-named in the sentence.
4. The sentence is ATTRIBUTED when at least one clause is framed and no
   UNFRAMED clause states an event (breached, attacked, encrypted, stole,
   leaked, suffered, shut down, paid a ransom, confirmed ... in the same six
   languages). Factive verbs ("confirmed", "revealed", "admitted", "proved")
   are never attribution verbs: "LockBit confirmed it breached Acme" lends the
   claim a truth it does not have.

Phrase matching stays phrase matching: the measured rates are for an
invented set, and a new phrasing can still be misread in either direction.
The severity of the failure is a declared setting
(:func:`claim_as_fact_fail_class`, ``LEGBA_VERIFY_CLAIM_AS_FACT``).

A LEAF (stdlib only). No model calls.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "ATTRIBUTION_VERBS",
    "CLAIM_AS_FACT_SEVERITY_ENV",
    "EVENT_TERMS",
    "FACTIVE_VERBS",
    "ROLE_NOUNS",
    "claim_as_fact_fail_class",
    "claim_as_fact_severity",
    "claimant_names",
    "sentence_attributed",
]

# ---------------------------------------------------------------------------
# The declared severity (the verify profile's setting for this one reason)
# ---------------------------------------------------------------------------

#: ``soft`` (default) — the sentence is a counted soft failure, as C-1 shipped.
#: ``hard`` — the sentence is a hard failure (the overclaim is treated like a
#: contradiction of the evidence). Any other value: soft, logged once.
CLAIM_AS_FACT_SEVERITY_ENV = "LEGBA_VERIFY_CLAIM_AS_FACT"
_SEVERITIES = ("soft", "hard")


def claim_as_fact_severity() -> str:
    raw = (os.getenv(CLAIM_AS_FACT_SEVERITY_ENV) or "soft").strip().lower()
    if raw not in _SEVERITIES:
        _warn_bad_severity(raw)
        return "soft"
    return raw


@lru_cache(maxsize=8)
def _warn_bad_severity(raw: str) -> None:
    logger.warning("verify.claim_as_fact.severity_unknown value=%r (using 'soft'; allowed: %s)",
                   raw, list(_SEVERITIES))


def claim_as_fact_fail_class() -> str:
    """The fail class ``claim_as_fact`` resolves to: ``soft_fail`` / ``hard_fail``."""
    return "hard_fail" if claim_as_fact_severity() == "hard" else "soft_fail"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Reporting verbs that keep a clause ABOUT the claim (any of these forms).
ATTRIBUTION_VERBS: tuple[str, ...] = (
    # English
    "claim", "claims", "claimed", "claiming",
    "post", "posts", "posted", "posting",
    "list", "lists", "listed", "listing",
    "say", "says", "said", "saying",
    "state", "states", "stated", "stating",
    "assert", "asserts", "asserted", "asserting",
    "allege", "alleges", "alleged", "alleging",
    "announce", "announces", "announced", "announcing",
    "write", "writes", "wrote", "written",
    "publish", "publishes", "published", "publishing",
    "add", "adds", "added",
    "name", "names", "named", "naming",
    "threaten", "threatens", "threatened", "threatening",
    "boast", "boasts", "boasted", "boasting",
    "insist", "insists", "insisted",
    "declare", "declares", "declared",
    "advertise", "advertises", "advertised",
    "display", "displays", "displayed",
    "show", "shows", "showed", "shown",
    "feature", "features", "featured",
    "tell", "tells", "told",
    "report", "reports", "reported", "reporting",
    "record", "records", "recorded",
    "describe", "describes", "described",
    "purport", "purports", "purported",
    "take responsibility", "takes responsibility", "took responsibility",
    "taking responsibility", "accept responsibility", "accepts responsibility",
    "accepted responsibility", "claim responsibility", "claimed responsibility",
    # French / Spanish / German / Portuguese / Italian
    "revendique", "revendiqué", "revendiquée", "affirme", "affirmé", "annonce", "annoncé",
    "publie", "publié", "a publié", "a revendiqué", "déclare", "liste",
    "reivindica", "reivindicó", "reivindicado", "afirma", "afirmó", "asegura", "aseguró",
    "publicó", "anunció", "dice", "dijo", "enumera",
    "behauptet", "behauptete", "beansprucht", "listet", "veröffentlicht", "veröffentlichte",
    "erklärt", "erklärte", "bekennt sich", "bekannte sich", "nennt", "nannte",
    "reivindicou", "afirmou", "alega", "alegou", "publicou", "anunciou",
    "rivendica", "rivendicato", "afferma", "ha pubblicato", "ha rivendicato", "dichiara",
)

#: Verbs that present the content as TRUE; never an attribution frame.
FACTIVE_VERBS: tuple[str, ...] = (
    "confirm", "confirms", "confirmed", "reveal", "reveals", "revealed",
    "admit", "admits", "admitted", "prove", "proves", "proved", "proven",
    "acknowledge", "acknowledges", "acknowledged", "demonstrate", "demonstrated",
    "establish", "established",
)

#: Nouns that stand for the claimant inside a frame ("according to the group").
ROLE_NOUNS: tuple[str, ...] = (
    "group", "gang", "crew", "actor", "actors", "operators", "operator", "tracker",
    "collective", "outfit", "affiliate", "hackers", "threat actor", "ransomware group",
    "extortion group", "groupe", "grupo", "gruppe", "gruppo", "bande", "banda",
)

#: Things a claimant publishes — "<claimant>'s <artifact> lists ...".
_ARTIFACTS = (
    "leak site", "data leak site", "dark web site", "dark-web site", "extortion site",
    "onion site", "site", "blog", "page", "portal", "post", "posting", "listing", "entry",
    "statement", "announcement", "channel", "message", "claim", "feed", "list", "tracker",
    "website", "wall of shame", "victim list", "victims page", "telegram channel",
)

#: Words that state the claimed EVENT (English + the five languages above).
EVENT_TERMS: tuple[str, ...] = (
    r"breach(?:ed|es|ing)?", r"hack(?:ed|s|ing)?", r"attack(?:ed|s|ing)?", r"cyber-?attack",
    r"compromis(?:e|ed|es|ing)", r"encrypt(?:ed|s|ing|ion)", r"st(?:ole|olen|eal|eals|ealing)",
    r"exfiltrat(?:e|ed|es|ing|ion)", r"leak(?:ed|s)", r"infiltrat(?:e|ed|es|ing)",
    r"penetrat(?:e|ed|es)", r"suffer(?:ed|s|ing)?", r"(?:was|were|been|got|is|are|be)\s+hit",
    r"struck", r"disrupt(?:ed|s|ing|ion)", r"crippl(?:ed|es|ing)", r"paraly[sz](?:ed|es|ing)",
    r"shut\s+down", r"(?:taken|took|knocked|went|go(?:es)?)\s+(?:\w+\s+)?offline",
    r"pa(?:id|ys?)\s+(?:a\s+|the\s+)?ransom", r"ransomed", r"extort(?:ed|s|ing)",
    r"infect(?:ed|s|ing)", r"lost", r"los(?:e|es|ing)", r"exposed", r"victimi[sz]ed",
    r"victim", r"intrusion", r"incident", r"outage", r"downtime", r"data\s+theft",
    r"confirm(?:ed|s|ing)?", r"admit(?:ted|s)?", r"locked\s+out", r"wiped",
    r"reveal(?:ed|s)?", r"prove[ds]?", r"proven", r"acknowledg(?:ed|es)", r"demonstrat(?:ed|es)",
    r"clos(?:ed|ure)", r"halt(?:ed|s)", r"suspend(?:ed|s)",
    r"delay(?:ed|s)", r"cancell?(?:ed|ation)", r"affect(?:ed|s)", r"impact(?:ed|s)", r"restor(?:ed|es|ing)",
    r"pirat[ée]e?s?", r"attaqu[ée]e?s?", r"vol[ée]e?s?", r"chiffr[ée]e?s?", r"victime",
    r"hackead[oa]s?", r"atacad[oa]s?", r"robad[oa]s?", r"cifrad[oa]s?", r"comprometid[oa]s?",
    r"vulnerad[oa]s?", r"v[íi]ctima", r"sufri[óo]", r"invadid[oa]s?",
    r"gehackt", r"angegriffen", r"gestohlen", r"verschl[üu]sselt", r"kompromittiert", r"Opfer",
    r"hackerat[oaie]", r"attaccat[oaie]", r"rubat[oaie]",
)
_EVENT_RE = re.compile(r"(?<![\w-])(?:" + "|".join(EVENT_TERMS) + r")(?![\w-])", re.IGNORECASE)

_AUX = (r"(?:(?:has|have|had|also|reportedly|now|later|then|publicly|again|subsequently|"
        r"previously|first|separately|itself|themselves|allegedly|apparently|"
        r"is|was|were|are|has\s+been|have\s+been|did\s+not|didn't|does\s+not|doesn't|"
        r"has\s+not|hasn't|have\s+not|haven't|never|not|yet|on\s+\w+\s+\d{1,2}|"
        r"on\s+\d{1,2}\s+\w+|on\s+\w+|in\s+a\s+post|online|a\s+few\s+hours\s+later|"
        r"hat|habe|a|ha|hanno|han|ha\s+sido|wurde|wird|sich|ont|est|se)\s+)")
_PRONOUNS = r"(?:it|they|he|she|we|its\s+operators|the\s+site|elle|ils|ella|ellos|sie|er|esso)"
_FOREIGN_ACCORDING = r"(?:selon|d'après|según|de\s+acuerdo\s+con|laut|segundo|secondo|stando\s+a)"
_PASSIVE_PARTS = (r"(?:claimed|listed|posted|named|added|published|announced|alleged|asserted|"
                  r"stated|featured|advertised|displayed|shown|described|reported|reivindicad[oa]|revendiqu[ée]e?|"
                  r"publi[ée]e?|publicad[oa]|reklamiert|aufgelistet|veröffentlicht|elencat[oa]|"
                  r"rivendicat[oa]|listad[oa])")
_BY = r"(?:by|par|por|von|da|from)"
_ON_SITE_VERBS = (r"(?:listed|posted|named|added|published|featured|shown|displayed|appears?|"
                  r"appeared|appearing|showed\s+up|surfaced|figur[ea]|aparece|apareci[óo]|"
                  r"erscheint|erschien|compare|appare)")
_QUOTE_RE = re.compile(r"[\"“”«»„](?:[^\"“”«»„]{0,600})[\"“”«»„]")
_SPLIT_RE = re.compile(
    r"\s*;\s*|\s+[—–-]{1,2}\s+|,\s*(?:and|but|yet|so|while|whereas|although|though)\s+"
    r"|\s+(?:but|whereas|although|though)\s+|,\s*(?:which|who|whom|where)\s+|\s+which\s+"
    r"|\s+and\s+(?=(?:the|a|an|its|their|this|that|these|those|[A-Z]))"
    r"|,\s*(?:whose|where)\s+|,\s*(?:an?|the)\s+(?:[\w-]+\s+){0,3}?(?:that|which)\s+"
    r"|(?:,\s*|\s+)(?:after|following|despite|since(?!\s+\d)|because|amid|in\s+the\s+wake\s+of|"
    r"as\s+a\s+result\s+of)\s+"
)
_LEADING_SUB_RE = re.compile(
    r"^\s*(?:after|following|despite|since|because|amid|in\s+the\s+wake\s+of|although|though|"
    r"while|when|once)\b([^,]+),(.*)$",
    re.IGNORECASE | re.S,
)


def _alt(words: Iterable[str]) -> str:
    return "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in sorted(set(words), key=len, reverse=True))


_VERBS = _alt(ATTRIBUTION_VERBS)
_ARTIFACT_ALT = _alt(_ARTIFACTS)
_ROLE_ALT = r"(?:the\s+|this\s+|that\s+|le\s+|la\s+|el\s+|die\s+|der\s+|il\s+)(?:\w+\s+)?(?:" + _alt(ROLE_NOUNS) + r")"


def claimant_names(claim: Mapping[str, Any] | None) -> list[str]:
    """Every name the claimant may be written as: the stamp's claimant, its
    leading name ("ransomware.live (leak-site tracker)" -> "ransomware.live"),
    the declared aliases, and every claimant of a multi-claimant (hop-2) stamp."""
    if not claim:
        return []
    raw: list[str] = []
    for key in ("claimant",):
        if claim.get(key):
            raw.append(str(claim[key]))
    for key in ("aliases", "claimants"):
        val = claim.get(key)
        if isinstance(val, (list, tuple)):
            raw.extend(str(v) for v in val if v)
    names: list[str] = []
    for name in raw:
        name = name.strip()
        if not name:
            continue
        names.append(name)
        head = re.split(r"\s*[(,;]", name, maxsplit=1)[0].strip()
        if head and head != name:
            names.append(head)
    return list(dict.fromkeys(n for n in names if len(n) >= 2))


@lru_cache(maxsize=256)
def _frames(names: tuple[str, ...]) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    name_alt = "|".join(
        r"(?<![\w.])" + re.escape(n).replace(r"\ ", r"\s+") + r"(?![\w])" for n in
        sorted(names, key=len, reverse=True)
    )
    who = rf"(?:{name_alt}|{_ROLE_ALT})" if name_alt else _ROLE_ALT
    subj = rf"(?:{who}|{_PRONOUNS})"
    poss = rf"(?:'s?|’s?)\s+(?:[\w-]+\s+){{0,2}}?(?:{_ARTIFACT_ALT})"
    appos = r"(?:\s*,[^,]{1,80},)?"
    patterns = [
        # F1 "<who> [<artifact>] [aux] <verb>"  (subject frame)
        rf"\b{subj}(?:{poss})?{appos}\s+{_AUX}{{0,3}}(?:{_VERBS})(?![\w])",
        # F1b inverted reporting at the END of the sentence only (", said
        # the group."): mid-sentence "<verb> <who>" is too often someone
        # else reporting ABOUT the claimant ("police said the group ...").
        rf",\s*(?:said|says|wrote|writes|claimed|claims|posted|added|stated|announced)\s+{who}"
        rf"\s*(?:\[\d+\]\s*)*[.!]?\s*$",
        # F2 "according to / per / citing / as claimed by <who>"
        rf"\b(?:according\s+to|per|citing|as\s+(?:claimed|stated|posted|listed|alleged|announced|"
        rf"reported|written)\s+by|{_FOREIGN_ACCORDING})\s+(?:(?:a|an|the)\s+(?:[\w-]+\s+){{0,2}}?"
        rf"(?:{_ARTIFACT_ALT})\s+(?:by|from|on|of)\s+)?{who}",
        # F2b postposed German "<who> zufolge"
        rf"{who}\s+zufolge",
        # F3 passive "<claimed/listed ...> [as a victim] by <who>"
        rf"\b{_PASSIVE_PARTS}\s+(?:as\s+(?:a\s+)?victim\s+)?(?:\w+\s+){{0,2}}?{_BY}\s+{who}",
        # F4 "<listed/appears ...> on/in <who>'s <artifact>"
        rf"\b{_ON_SITE_VERBS}\s+(?:as\s+(?:a\s+)?victim\s+)?(?:\w+\s+){{0,3}}?(?:on|in|to|onto|sur|en|auf|su)\s+"
        rf"(?:the\s+)?{who}{poss}",
        rf"\b(?:on|in|sur|en|auf|su)\s+(?:the\s+)?{who}{poss}",
        # F5 claim-nouns: "<who>'s claim/post/listing", "a claim by <who>"
        rf"{who}(?:'s?|’s?)\s+(?:[\w-]+\s+){{0,2}}?(?:claims?|assertions?|allegations?|listing|"
        rf"posts?|announcement|statement|boast|threat)\b",
        rf"\b(?:claims?|assertions?|allegations?|listing|posts?|announcement|statement)\s+"
        rf"(?:\w+\s+){{0,2}}?{_BY}\s+{who}",
    ]
    frame = re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)
    mention = re.compile(rf"(?:{who})", re.IGNORECASE)
    factive = re.compile(rf"\b{subj}(?:{poss})?{appos}\s+{_AUX}{{0,3}}(?:{_alt(FACTIVE_VERBS)})(?![\w])",
                         re.IGNORECASE)
    return frame, mention, factive


#: A factive verb anywhere in a clause (not negated) asserts truth: the clause
#: is read as unframed even when a frame is also present ("Acme confirmed the
#: breach that LockBit claimed").
_FACTIVE_ANY_RE = re.compile(
    r"(?<![\w-])(?:confirm(?:ed|s)?|reveal(?:ed|s)?|admit(?:ted|s)?|prove[ds]?|proven|"
    r"acknowledg(?:ed|es)|demonstrat(?:ed|es))(?![\w-])"
    r"|\bshow(?:s|ed|n)?\s+that\b|\bas\s+(?:the\s+)?(?:[\w'’-]+\s+){0,3}?(?:shows?|showed|demonstrates?|proves?)\b",
    re.IGNORECASE,
)
_NEGATION_BEFORE_RE = re.compile(r"(?:\bnot|\bnever|n't|\byet\s+to|\bno|\bneither|\bnor)\s+(?:\w+\s+){0,2}$",
                                 re.IGNORECASE)
#: An event PRESUPPOSED as an adjective ("posted the stolen files").
_PRESUPPOSED_RE = re.compile(
    r"\b(?:the|its|their|this|these|those)\s+(?:stolen|leaked|exfiltrated|encrypted|breached|hacked|"
    r"compromised|attacked|ransomed)\s+\w+", re.IGNORECASE)


def _factive(clause: str) -> bool:
    for m in _FACTIVE_ANY_RE.finditer(clause):
        if not _NEGATION_BEFORE_RE.search(clause[: m.start()]):
            return True
    return False


def _clauses(text: str) -> list[str]:
    pieces: list[str] = []
    for part in _SPLIT_RE.split(text):
        if not part or not part.strip():
            continue
        m = _LEADING_SUB_RE.match(part)
        if m:
            pieces.extend(p for p in (m.group(1), m.group(2)) if p.strip())
        else:
            pieces.append(part)
    return pieces


def sentence_attributed(sentence: str, claim: Mapping[str, Any] | None) -> bool:
    """True when ``sentence`` presents the claim AS a claim (module docstring)."""
    if not claim or not sentence:
        return False
    names = tuple(claimant_names(claim))
    if not names:
        return False
    frame, mention, factive = _frames(names)
    text = _QUOTE_RE.sub(" QUOTE ", sentence)
    if not mention.search(text):
        return False
    framed_any = False
    for clause in _clauses(text):
        framed = (bool(frame.search(clause)) and not factive.search(clause)
                  and not _factive(clause) and not _PRESUPPOSED_RE.search(clause))
        if framed:
            framed_any = True
            continue
        if _EVENT_RE.search(clause) or _PRESUPPOSED_RE.search(clause):
            return False
    return framed_any
