# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A planted set for the desk-rows repair rule's party-swap guard, built by
code.

Each case is ``(label, old, new, quotes)`` — a fact row's text before and
after a repair, and the located quotes the repair may take words from (the
rule's ``sources``). Every word of every ``new`` text is in its quotes, so
only the guard under test can tell the two sets apart:

* :func:`swap_cases` — the repair turns two named parties round: the subject
  and the counterpart of an active clause (with an object, a "to"/"on"/
  "against"/"with" counterpart), the actor and the target of a PASSIVE
  clause, an active clause rewritten as a passive one with the roles
  inverted (and the reverse), and the speaker of an attribution traded with
  a party of the reported claim;
* :func:`good_cases` — the repair reorders words WITHOUT swapping roles: an
  active clause rewritten as the faithful passive (and back), an attribution
  moved from the end to the front (and back), a list of co-actors reordered,
  a fronted place phrase moved to the end.
"""
from __future__ import annotations

from itertools import cycle, islice

#: (actor, target) pairs — country and organisation names, one and two words.
PAIRS = [
    ("Russia", "Ukraine"), ("Israel", "Hezbollah"), ("China", "Taiwan"), ("Iran", "Israel"),
    ("Hamas", "Israel"), ("Pakistan", "India"), ("Turkey", "Syria"), ("Serbia", "Kosovo"),
    ("Azerbaijan", "Armenia"), ("Sudan", "Chad"), ("Ethiopia", "Eritrea"), ("Venezuela", "Guyana"),
    ("North Korea", "South Korea"), ("United States", "Iran"), ("Russia", "Poland"),
    ("Rwanda", "Congo"), ("Morocco", "Algeria"), ("Saudi Arabia", "Yemen"), ("Greece", "Turkey"),
    ("Japan", "China"), ("Egypt", "Ethiopia"), ("Thailand", "Cambodia"),
]
#: Speakers for attributions.
SPEAKERS = ["Reuters", "Zelensky", "Netanyahu", "Erdogan", "Lavrov", "Blinken", "Guterres", "Macron"]
PLACES = ["Geneva", "Doha", "Ankara", "Brussels", "Cairo", "Istanbul"]

#: (active verb, passive participle) for a direct object clause.
STRIKE = [("struck", "struck"), ("attacked", "attacked"), ("shelled", "shelled"), ("accused", "accused"),
          ("sanctioned", "sanctioned"), ("expelled diplomats of", None)]
#: Active clauses with a prepositional counterpart: (template, counterpart preposition).
PREP = ["{a} imposed sanctions on {b}", "{a} sold weapons to {b}", "{a} launched drones against {b}",
        "{a} cut gas supplies to {b}", "{a} signed a security pact with {b}", "{a} sent troops to {b}"]


def _take(seq, n):
    return list(islice(cycle(seq), n))


def swap_cases() -> list[tuple[str, str, str, list[str]]]:
    out: list[tuple[str, str, str, list[str]]] = []
    pairs = _take(PAIRS, 30)
    # 1. active subject <-> object
    for (a, b), (verb, _) in zip(pairs[:6], _take(STRIKE, 6)):
        q = f"{a} {verb} {b} on Monday"
        out.append(("active_object", f"{a} {verb} {b}.", f"{b} {verb} {a}.", [q]))
    # 2. active subject <-> prepositional counterpart
    for (a, b), t in zip(pairs[6:12], PREP):
        q = t.format(a=a, b=b) + " this week"
        out.append(("active_counterpart", t.format(a=a, b=b) + ".", t.format(a=b, b=a) + ".", [q]))
    # 3. passive actor <-> target
    for (a, b), (_, part) in zip(pairs[12:17], _take([s for s in STRIKE if s[1]], 5)):
        q = f"{b} was {part} by {a} overnight"
        out.append(("passive_swap", f"{b} was {part} by {a}.", f"{a} was {part} by {b}.", [q]))
    # 4. active -> passive with the roles inverted (names stay in order)
    for (a, b), (verb, part) in zip(pairs[17:21], _take([s for s in STRIKE if s[1]], 4)):
        q = f"{a} {verb} {b}, officials said"
        out.append(("active_to_wrong_passive", f"{a} {verb} {b}.", f"{a} was {part} by {b}.", [q]))
    # 5. passive -> active with the roles inverted (names stay in order)
    for (a, b), (verb, part) in zip(pairs[21:25], _take([s for s in STRIKE if s[1]], 4)):
        q = f"{b} was {part} by {a}, officials said"
        out.append(("passive_to_wrong_active", f"{b} was {part} by {a}.", f"{b} {verb} {a}.", [q]))
    # 6. the speaker traded with a party of the claim
    for (a, b), s in zip(pairs[25:29], SPEAKERS):
        q = f"{s} said {a} {STRIKE[0][0]} {b}"
        out.append(("speaker_swap", f"{s} said {a} struck {b}.", f"{a} said {s} struck {b}.", [q]))
    return out


def good_cases() -> list[tuple[str, str, str, list[str]]]:
    out: list[tuple[str, str, str, list[str]]] = []
    pairs = _take(PAIRS, 30)
    # 1. active -> the faithful passive
    for (a, b), (verb, part) in zip(pairs[:5], _take([s for s in STRIKE if s[1]], 5)):
        q = f"{a} {verb} {b} on Monday"
        out.append(("active_to_passive", f"{a} {verb} {b}.", f"{b} was {part} by {a}.", [q]))
    # 2. passive -> the faithful active
    for (a, b), (verb, part) in zip(pairs[5:9], _take([s for s in STRIKE if s[1]], 4)):
        q = f"{b} was {part} by {a} overnight"
        out.append(("passive_to_active", f"{b} was {part} by {a}.", f"{a} {verb} {b}.", [q]))
    # 3. attribution moved from the end to the front
    for (a, b), s in zip(pairs[9:14], SPEAKERS):
        q = f"{a} struck {b}, {s} said"
        out.append(("attribution_to_front", f"{a} struck {b}, {s} said.", f"{s} said {a} struck {b}.", [q]))
    # 4. attribution moved from the front to the end
    for (a, b), s in zip(pairs[14:18], SPEAKERS[3:]):
        q = f"{s} said {a} sold weapons to {b}"
        out.append(("attribution_to_end", f"{s} said {a} sold weapons to {b}.",
                    f"{a} sold weapons to {b}, {s} said.", [q]))
    # 5. co-actors reordered (one role)
    for (a, b), p in zip(pairs[18:21], PLACES):
        q = f"{a} and {b} signed a ceasefire in {p}"
        out.append(("co_actors_reordered", f"{a} and {b} signed a ceasefire.",
                    f"{b} and {a} signed a ceasefire.", [q]))
    # 6. a fronted place phrase moved to the end
    for (a, b), p in zip(pairs[21:24], PLACES[3:]):
        q = f"In {p}, {a} accused {b} of spying"
        out.append(("place_to_end", f"In {p}, {a} accused {b} of spying.",
                    f"{a} accused {b} of spying in {p}.", [q]))
    return out


# ---------------------------------------------------------------------------
# HELD OUT: written after the role rule was tuned on the sets above and run
# once, as-is, to say how far the rule generalises.
# ---------------------------------------------------------------------------

HELD_PAIRS = [("Lebanon", "Israel"), ("Colombia", "Venezuela"), ("Mali", "Niger"), ("Kenya", "Somalia"),
              ("Georgia", "Russia"), ("Iraq", "Turkey"), ("Libya", "Egypt"), ("Uganda", "Sudan")]


def held_out_swap_cases() -> list[tuple[str, str, str, list[str]]]:
    out: list[tuple[str, str, str, list[str]]] = []
    P = HELD_PAIRS
    for a, b in P[:3]:
        q = f"{a}'s forces struck {b}'s port at dawn"
        out.append(("possessive_swap", f"{a}'s forces struck {b}'s port.", f"{b}'s forces struck {a}'s port.", [q]))
    for a, b in P[3:6]:
        q = f"{b}'s port was hit by {a}'s drones"
        out.append(("possessive_passive_swap", f"{b}'s port was hit by {a}'s drones.",
                    f"{a}'s port was hit by {b}'s drones.", [q]))
    for a, b in P[5:8]:
        q = f"{a} accused {b} of violating the truce"
        out.append(("accused_swap", f"{a} accused {b} of violating the truce.",
                    f"{b} accused {a} of violating the truce.", [q]))
    for a, b in P[:3]:
        q = f"{a} handed over prisoners to {b} on Friday"
        out.append(("handover_swap", f"{a} handed over prisoners to {b}.", f"{b} handed over prisoners to {a}.", [q]))
    return out


def held_out_good_cases() -> list[tuple[str, str, str, list[str]]]:
    out: list[tuple[str, str, str, list[str]]] = []
    P = HELD_PAIRS
    for a, b in P[:3]:
        q = f"{a}'s forces struck {b}'s port at dawn"
        out.append(("possessive_to_passive", f"{a}'s forces struck {b}'s port.",
                    f"{b}'s port was struck by {a}'s forces.", [q]))
    for (a, b), s in zip(P[3:6], ["Guterres", "Macron", "Lavrov"]):
        q = f"according to {s}, {a} struck {b}"
        out.append(("according_to_end", f"According to {s}, {a} struck {b}.",
                    f"{a} struck {b}, according to {s}.", [q]))
    for (a, b), s in zip(P[5:8], ["Reuters", "Blinken", "Erdogan"]):
        q = f"{a} struck {b} on Monday, {s} said"
        out.append(("said_to_front_with_time", f"{a} struck {b} on Monday, {s} said.",
                    f"{s} said {a} struck {b} on Monday.", [q]))
    for (a, b), p in zip(P[:3], ["Nairobi", "Baghdad", "Tripoli"]):
        q = f"{a} and {b} held talks in {p}"
        out.append(("place_to_front", f"{a} and {b} held talks in {p}.", f"In {p}, {a} and {b} held talks.", [q]))
    return out
