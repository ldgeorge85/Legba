# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X4 / ESC (W11-F52) - the uncited-sentence share, measured on the BODY.

WHY THIS EXISTS. A unit read can lose most of its score to ``no_citation``: the
deterministic floor counts every FACT-ASSERTING sentence that carries no marker
(``verify._deterministic_floor``), and the BLUF is one of them whenever it names
a place, an actor, a number or a date (``verify._asserts_beyond_exemption``).
Under the LLM judge the same sentences are graded leniently as synthesis, so the
cost is invisible until the judge is out (10-09: the escalation read that scored
0.25 with three ``no_citation`` of four claims was a floor-only read). The prompts
for ``escalation`` and ``military_posture`` now ask for a marker on every fact
sentence and on the BLUF; this module is the instrument that says whether the ask
reached the model, ON EVERY READ, whichever path graded it.

WHY THE BODY AND NOT THE LEDGER. ``claim_verdicts`` is the wrong population for
this question: on the floor path it lists only the sentences the floor COUNTED
(an exempt BLUF is not a row), and on the judge path it lists what the judge was
asked, capped at 120 rows. The body is the one source both paths share, and the
segmentation, the fact test and the marker reader below are the floor's OWN
(``verify._segment_claims`` - read as its raw spans filtered by the same
``_is_propositional`` test - / ``_is_fact_asserting`` / ``_markers_in_claim``), so
``uncited_fact_sentences`` is, by construction, the number of ``no_citation``
spans the floor would write for this body.

DEFINITIONS (stamped on the critique's ``verification`` block, read by
``GET /v3/system/judge-stats`` as ``uncited_by_unit_day``):

  * ``fact_sentences``          sentences the floor treats as fact-asserting
  * ``uncited_fact_sentences``  of those, the ones with no ``[N]`` / ``[a-b]`` /
                                ``[[ref:N]]`` marker
  * ``bluf_marker``             ``"cited"`` | ``"uncited"`` | ``"none"`` (no BLUF
                                line in the body). ``uncited`` is any BLUF with no
                                marker, whether or not the floor would count it.
                                A label alone on its line (``**BLUF:**`` then
                                the verdict below it) is read off the verdict
                                sentence, not the label (W11-F86).

A scoped negative ("not observed in collected reporting") is fact-asserting to the
floor only when it names something; one that does stays in the numerator, which is
the floor's own reading and is why this is a trend instrument over the fleet, not a
per-read pass/fail.
"""

from __future__ import annotations

import re
from typing import Any

#: A BLUF line, with any heading hashes / bullets / emphasis in front of it.
_BLUF_LINE_RE = re.compile(r"^[\s#>*_\-]*bluf\b", re.IGNORECASE)

#: W11-F86 — a BLUF LABEL standing alone on its line (``**BLUF:**`` / ``## BLUF``)
#: with the verdict on the next line. The segmenter keeps the label as its own
#: span, so the instrument used to read the bare label as the BLUF and stamp
#: ``uncited`` on a read whose BLUF sentence carried its markers. The BLUF is
#: then the NEXT span — the same reading ``assembly_spans._BLUF_RE`` (which runs
#: past the label's newline) gives the composition tier's lead span.
_BLUF_LABEL_ONLY_RE = re.compile(r"^[\s#>*_\-]*bluf[\s*_:\-]*$", re.IGNORECASE)

BLUF_CITED = "cited"
BLUF_UNCITED = "uncited"
BLUF_NONE = "none"


def uncited_share_fields(body: str) -> dict[str, Any]:
    """The ``verification``-block fragment for one finding body.

    Pure and total: an empty or unparseable body yields zero counts and
    ``bluf_marker == "none"`` - never an exception into the verify path.
    """
    from .verify import (
        _is_fact_asserting,
        _is_propositional,
        _markers_in_claim,
        _segment_claims_raw,
    )

    fact = 0
    uncited = 0
    bluf = BLUF_NONE
    label_pending = False
    try:
        # The RAW spans, so a bare ``BLUF:`` label (one token, which V-F drops
        # as non-propositional) is still seen as the label it is; the counts
        # below read only the propositional spans, i.e. exactly
        # ``_segment_claims``' list.
        for claim in _segment_claims_raw(body or ""):
            cited = bool(
                _markers_in_claim(claim, subclaim=False)
                or _markers_in_claim(claim, subclaim=True)
            )
            propositional = _is_propositional(claim)
            if bluf == BLUF_NONE:
                if _BLUF_LABEL_ONLY_RE.match(claim):
                    label_pending = True
                elif propositional and (label_pending or _BLUF_LINE_RE.match(claim)):
                    bluf = BLUF_CITED if cited else BLUF_UNCITED
            if not propositional or not _is_fact_asserting(claim):
                continue
            fact += 1
            if not cited:
                uncited += 1
    except Exception:  # noqa: BLE001 - an instrument must never break a run
        return {"fact_sentences": 0, "uncited_fact_sentences": 0, "bluf_marker": BLUF_NONE}
    if label_pending and bluf == BLUF_NONE:
        bluf = BLUF_UNCITED  # a BLUF label with nothing after it cites nothing
    return {
        "fact_sentences": fact,
        "uncited_fact_sentences": uncited,
        "bluf_marker": bluf,
    }


__all__ = [
    "BLUF_CITED",
    "BLUF_NONE",
    "BLUF_UNCITED",
    "uncited_share_fields",
]
