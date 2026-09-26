# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G1 — THE RUBRIC, and the digest that makes it an identity.

The rubric is loaded from :mod:`legba.data.analysts.rubrics` (a data file in the
tree, not a Python string) and its sha256 is checked AT IMPORT against the value
pinned here. That check is not ceremony:

  * ``unit_correctness.rubric_sha`` and ``grader_calibrations.rubric_sha`` are
    the join between a published number and the instrument that produced it. An
    edit to the rubric file that did not move the pin would let rows graded
    under two different rubrics pool under one sha — exactly the silent
    re-labelling the digest exists to prevent;
  * the calibration gate PASSED on ``ANNEX_C_v4.md``'s bytes, not on its ideas
    (VERDICT_P1v4: pooled 0.8444, every pair above the 0.70 floor). A changed
    rubric is an UNCALIBRATED rubric and the job must refuse to grade under it
    until a re-gate row exists (:mod:`_correctness_calibration`).

Changing the rubric is therefore a THREE-part commit: the file, the pin here,
and a fresh passing ``grader_calibrations`` row for the new sha
(``scripts/correctness_regate.py``). Any one of the three alone fails loud.

The LABEL VOCABULARY and the SPAN POLICY are READ OUT OF the rubric text — never
hardcoded — carried from ``planning/PROGRAM1_2026-09-16/grade_p1.py``. A script
that pinned ``contains/contradicts/silent`` would keep passing the day the
rubric moved, which is the staleness this whole module exists to prevent.
"""

from __future__ import annotations

import hashlib
import os
import re

from ..rubrics import RUBRICS_DIR

#: The rubric this build grades under. Its digest is pinned below.
RUBRIC_NAME = "ANNEX_C_v4.md"
RUBRIC_PATH = os.path.join(RUBRICS_DIR, RUBRIC_NAME)

#: sha256 of ``ANNEX_C_v4.md``, verbatim from the file Program 1's gate passed
#: on (planning/PROGRAM1_2026-09-16/VERDICT_P1v4.md names it as ``1b51d7f5…``).
RUBRIC_SHA256 = "1b51d7f5187c7f93af2e2cccc0775e21ab7efc41678bc28c4a2dcbe287fe7d8c"

#: The note that goes in front of the rubric in the system message, keyed to the
#: rubric VERSION exactly as ``build_p1_sample.NOTES_BY_RUBRIC_VERSION`` keys it.
#: The note is part of the packet's bytes, so it is part of the packet sha256;
#: retyping it anywhere else would create a second source of truth for a frozen
#: document.
NOTE_V4 = (
    "Apply the rubric that follows to the item you are given, one item per "
    "reply. You cannot browse; grade the assertion from the committed "
    "reference's developments exactly as written and from nothing else."
)
NOTES_BY_RUBRIC_VERSION: dict[str, str] = {"v4": NOTE_V4}

_RUBRIC_VERSION_RE = re.compile(r"^ANNEX_C_(v\d+)\.md$")


class RubricDigestError(RuntimeError):
    """The rubric file on disk is not the rubric this build was calibrated on.

    A ``RuntimeError`` and not a stub: nothing is faked, nothing degrades. The
    module refuses to import, which takes the grader offline rather than letting
    it publish numbers under a rubric sha that no longer describes them.
    """


def _read_rubric() -> tuple[str, str]:
    with open(RUBRIC_PATH, encoding="utf-8") as fh:
        text = fh.read()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, digest


RUBRIC_TEXT, _ACTUAL_SHA = _read_rubric()
if _ACTUAL_SHA != RUBRIC_SHA256:
    raise RubricDigestError(
        f"{RUBRIC_NAME} on disk hashes to {_ACTUAL_SHA}, not the pinned "
        f"{RUBRIC_SHA256}. Every unit_correctness row carries a rubric_sha; "
        "grading under a rubric whose bytes moved would pool two instruments "
        "under one identity. Edit the pin AND land a fresh passing "
        "grader_calibrations row (scripts/correctness_regate.py) in the same "
        "commit, or restore the file."
    )


def rubric_version(rubric_name: str = RUBRIC_NAME) -> str:
    """``v4`` — read off the rubric's OWN filename (build_p1_sample's rule).

    The version selects the note, which lands in the packet's bytes. Reading it
    from the file being shipped means the two can never disagree; a flag could.
    """
    match = _RUBRIC_VERSION_RE.match(os.path.basename(rubric_name))
    if not match:
        raise RubricDigestError(
            f"cannot read a rubric version off {os.path.basename(rubric_name)!r} "
            "— expected ANNEX_C_v<N>.md. The version selects the packet's note; "
            "this module will not guess it."
        )
    return match.group(1)


def note_for(version: str) -> str:
    """The note registered for this rubric version, or a loud stop."""
    if version not in NOTES_BY_RUBRIC_VERSION:
        raise RubricDigestError(
            f"no note_to_grader registered for rubric {version} — the note is "
            "part of the packet and part of its sha256. Register it in "
            "NOTES_BY_RUBRIC_VERSION before grading under that rubric."
        )
    return NOTES_BY_RUBRIC_VERSION[version]


# ---------------------------------------------------------------------------
# Label extraction — READ from the rubric, never hardcoded
# ---------------------------------------------------------------------------

#: ``- **contains** — …`` — the bolded head of a bullet in the labels section.
#: Anchored to the bullet so a bold run anywhere else (a rule heading, emphasis)
#: cannot be mistaken for a label.
_LABEL_BULLET_RE = re.compile(
    r"^-\s+\*\*([A-Za-z_][A-Za-z0-9_\-]*)\*\*", re.MULTILINE
)
#: The output contract's alternation: ``{"verdict": "a|b|c", …}``
_CONTRACT_ALT_RE = re.compile(r'"verdict"\s*:\s*"([^"]+)"')


def extract_labels(rubric: str) -> tuple[str, ...]:
    """The rubric's own label vocabulary, in the rubric's own order.

    Read TWICE and cross-checked — once from the bolded bullet heads of the
    labels section, once from the output contract's alternation. They must
    agree and there must be exactly three, or this raises. A rubric whose two
    statements of its own vocabulary disagree is not gradable, and a hardcoded
    fallback would hide exactly that.
    """
    section = ""
    for block in re.split(r"^##\s+", rubric or "", flags=re.MULTILINE):
        if re.match(r"the three labels\b", block.strip(), re.IGNORECASE):
            section = block
            break
    bulleted = tuple(_LABEL_BULLET_RE.findall(section))
    match = _CONTRACT_ALT_RE.search(rubric or "")
    contract = (
        tuple(p.strip() for p in match.group(1).split("|")) if match else ()
    )
    if len(bulleted) != 3:
        raise RubricDigestError(
            f"the rubric's labels section yielded {len(bulleted)} bolded "
            f"labels {bulleted!r}, expected exactly three. Refusing to guess a "
            "label set for a frozen rubric this code cannot read."
        )
    if contract and contract != bulleted:
        raise RubricDigestError(
            f"the rubric states its labels twice and they DISAGREE: bullets "
            f"{bulleted!r} vs output contract {contract!r}. Refusing to grade "
            "against an ambiguous vocabulary."
        )
    return bulleted


def extract_output_contract(rubric_text: str) -> str:
    """The JSON object shape from the rubric's LAST section, read out of the
    rubric rather than retyped here (``build_p1_sample.extract_output_contract``).
    """
    tail = rubric_text.rsplit("## ", 1)[-1]
    for line in (ln.strip() for ln in tail.splitlines()):
        if line.startswith("{") and line.endswith("}"):
            return line
    raise RubricDigestError(
        "no JSON object line in the rubric's last section — the output "
        "contract is part of the frozen rubric and this module will not "
        "invent one"
    )


#: The rubric's labels, resolved once at import off the pinned text.
LABELS: tuple[str, ...] = extract_labels(RUBRIC_TEXT)
#: The contract line, likewise.
OUTPUT_CONTRACT: str = extract_output_contract(RUBRIC_TEXT)
#: The note that pairs with this rubric's version.
NOTE_TO_GRADER: str = note_for(rubric_version())

__all__ = [
    "LABELS",
    "NOTES_BY_RUBRIC_VERSION",
    "NOTE_TO_GRADER",
    "NOTE_V4",
    "OUTPUT_CONTRACT",
    "RUBRIC_NAME",
    "RUBRIC_PATH",
    "RUBRIC_SHA256",
    "RUBRIC_TEXT",
    "RubricDigestError",
    "extract_labels",
    "extract_output_contract",
    "note_for",
    "rubric_version",
]
