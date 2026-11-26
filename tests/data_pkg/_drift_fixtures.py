# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic page texts for the source-drift lane: one earlier article
and a later version per class. Shaped after the live sample (2026-11-13): a
developing story whose toll is revised, a headline rewritten, a live page
whose entries scroll off, a related-links block and a "Published N hours ago"
chip that move between fetches."""
from __future__ import annotations

TITLE = "Death toll from bombing near police station rises"

BODY = [
    "The death toll from Sunday's suicide bombing near a police station in Swat rose to 19 on Monday, police said.",
    "Police spokesperson Nasir Karimi said four more victims had succumbed to their injuries during treatment.",
    "The bombing had initially claimed over a dozen lives and left 18 injured.",
    "A large-scale rescue operation was launched immediately after the attack, with more than 50 rescue workers.",
    "The injured were shifted to the teaching hospital for treatment while security forces sealed off the area.",
    "Provincial officials condemned the attack and announced compensation for the families of the victims.",
]

CHROME_A = ["Related topics", "- Published 10 hours ago", "Share", "123 comments"]
CHROME_B = ["Related topics", "- Published 11 hours ago", "Share this article", "131 comments"]


def page(body: list[str], chrome: list[str] | None = None, extra: list[str] | None = None) -> str:
    return "\n".join(list(body) + list(extra or []) + list(chrome or []))


EARLIER = page(BODY, CHROME_A)

#: The span a finding cited (the toll sentence) and one it did not.
CITED_SPAN = BODY[0]

PAIRS: dict[str, tuple[str, str]] = {
    "cosmetic": (EARLIER, page(BODY, CHROME_B)),
    "appended": (EARLIER, page(BODY, CHROME_A, extra=[
        "Update: The provincial government announced a day of mourning on Tuesday across the district.",
    ])),
    "minor_edit": (EARLIER, page(
        [BODY[0], BODY[1].replace("had succumbed", "succumbed"), *BODY[2:]], CHROME_A)),
    "substantive_edit": (EARLIER, page(
        [BODY[0].replace("rose to 19", "rose to 17"), *BODY[1:]], CHROME_A)),
    "retracted_or_replaced": (EARLIER, page([
        "Markets opened higher on Tuesday as investors weighed new trade figures from the central bank.",
        "The benchmark index gained more than two percent in early trading before paring its gains.",
        "Analysts said the rally reflected optimism about a stabilising currency and easing inflation.",
        "Energy stocks led the advance while consumer companies lagged behind the wider market.",
    ])),
}

# -- D-2: the assessment's fixture pairs (not classes: D-1's PAIRS stays keyed
# by class) -----------------------------------------------------------------

#: A name misspelt in the earlier version and fixed later; the toll sentence
#: (the cited span) is untouched.
NAME_FIX = (
    page([BODY[0], BODY[1].replace("Nasir", "Naser"), *BODY[2:]], CHROME_A),
    page(BODY, CHROME_A),
)
#: A claim removed and not restated; nothing acknowledges it.
CLAIM_REMOVED = (EARLIER, page(BODY[:2] + BODY[3:], CHROME_A))
#: The toll revised down, with an added sentence acknowledging it in-text.
TOLL_REVISED_ACK = (EARLIER, page(
    [BODY[0].replace("rose to 19", "rose to 17"), *BODY[1:],
     "Police revised down the death toll from an earlier tally of 19 shared on Monday morning."], CHROME_A))
#: Attribution changed from one agency to another.
ATTRIBUTION = (
    page([*BODY, "A spokesperson told the AFP news agency that 51 passengers were on board the bus."]),
    page([*BODY, "A spokesperson told the BBC that 51 passengers were on board the bus."]),
)
