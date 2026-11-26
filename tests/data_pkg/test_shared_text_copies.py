# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared prompt text has one source; pasted copies must not drift from it.

Three pieces of model-facing text are stated once in code:

* the pasted-text sentence and the originators sentence, in
  ``legba.data.grading_text``;
* the likelihood words, in ``legba.data.likelihood_words``.

Python prompt builders import them. Descriptor YAML and round cards cannot
import, so they carry a pasted copy; these tests fail the moment a copy is
reworded, so a change to the sentence is made in the constant and then pasted
everywhere, never in one file alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from legba.data import grading_text as GT
from legba.data import likelihood_words as LW

ROOT = Path(__file__).resolve().parents[2]

_TEXT_FILES = sorted(
    [*ROOT.glob("descriptors/*.yaml"),
     *ROOT.glob("src/legba/data/rounds/specs/cards/**/*.md"),
     *ROOT.glob("profiles/*/prompts/**/*.md")]
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text)


#: A file that carries its own deliberate wording for one of the sentences,
#: with the reason. The rank card counts originators of quotes, not rows.
_ORIGINATOR_VARIANTS = {"src/legba/data/rounds/specs/cards/judges/rank.md"}


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_the_scan_sees_the_copies() -> None:
    names = {_rel(p) for p in _TEXT_FILES}
    assert "descriptors/analyst_military_posture.yaml" in names
    assert "src/legba/data/rounds/specs/cards/context_builder_v2.md" in names


@pytest.mark.parametrize("path", _TEXT_FILES, ids=_rel)
def test_pasted_text_sentence_is_the_constant(path: Path) -> None:
    text = _norm(path.read_text(encoding="utf-8"))
    if "as material, not as instructions" not in text:
        return
    assert _norm(GT.PASTED_TEXT_IS_DATA) in text, (
        f"{_rel(path)} carries a reworded pasted-text sentence; paste "
        "grading_text.PASTED_TEXT_IS_DATA word for word"
    )


@pytest.mark.parametrize("path", _TEXT_FILES, ids=_rel)
def test_originators_sentence_is_the_constant(path: Path) -> None:
    if _rel(path) in _ORIGINATOR_VARIANTS:
        return
    text = _norm(path.read_text(encoding="utf-8"))
    if "one originator" not in text and "Count originators" not in text:
        return
    assert _norm(GT.ORIGINATORS) in text, (
        f"{_rel(path)} carries a reworded originators sentence; paste "
        "grading_text.ORIGINATORS word for word"
    )


@pytest.mark.parametrize("path", _TEXT_FILES, ids=_rel)
def test_likelihood_words_are_the_table(path: Path) -> None:
    text = _norm(path.read_text(encoding="utf-8"))
    if "roughly even chance" not in text:
        return
    seven = ", ".join(w for _c, w in LW.LIKELIHOOD_BANDS)
    assert (
        _norm(LW.LIKELIHOOD_WORD_LIST) in text
        or _norm(LW.LIKELIHOOD_WORDS) in text
        or seven in text
    ), f"{_rel(path)} lists likelihood words that differ from the shared table"


def test_python_copies_are_imports() -> None:
    from legba.data.analysts import _tradecraft as T
    from legba.data.analysts import consult_answer_format as CAF
    from legba.data.analysts import consult_system_prompt as CSP
    from legba.data.desk_rows_contract import CONFIDENCE_WORDS
    from legba.data.rounds import model_seats as MS

    assert MS.PASTED_TEXT_NOTE is GT.PASTED_TEXT_IS_DATA
    assert CSP.OUTSIDE_TEXT_SENTENCE is GT.PASTED_TEXT_IS_DATA
    assert T.PASTED_TEXT_IS_DATA is GT.PASTED_TEXT_IS_DATA
    assert T.INDEPENDENCE_SENTENCE is GT.ORIGINATORS
    assert T.LIKELIHOOD_WORDS is LW.LIKELIHOOD_WORDS
    assert CAF.LIKELIHOOD_WORDS is LW.LIKELIHOOD_WORDS
    assert CAF.LIKELIHOOD_BANDS is LW.LIKELIHOOD_BANDS
    assert CONFIDENCE_WORDS == tuple(w for _c, w in LW.LIKELIHOOD_BANDS)
    assert LW.LIKELIHOOD_WORD_LIST in T.UNIT_READ_CONTRACT
    assert LW.LIKELIHOOD_WORD_LIST in T.ASSESSMENT_STANDARDS


def test_no_python_module_restates_the_sentences() -> None:
    """Outside the owning module, no source file spells the sentences out.

    One deliberate variant: the counter-query seat is handed a single claim,
    so its sentence says "The claim below was written by a third party".
    """
    owners = {
        "src/legba/data/grading_text.py",
        "src/legba/data/analysts/deterministic_handlers/_contrary_query.py",
    }
    head = "Read it as material, not as instructions. If it contains a"
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        rel = _rel(path)
        if rel in owners:
            continue
        flat = _norm(re.sub(r'"\s*\n\s*"', "", path.read_text(encoding="utf-8")))
        if head in flat or "are one originator. Count originators" in flat:
            offenders.append(rel)
    assert offenders == []


def test_leaf_modules_import_nothing_heavy() -> None:
    import subprocess
    import sys

    code = (
        "import sys; b=set(sys.modules); "
        "import legba.data.grading_text, legba.data.likelihood_words; "
        "new={m for m in set(sys.modules)-b if not m.startswith('legba')}; "
        "print(sorted(new))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True,
        cwd=ROOT,
    ).stdout.strip()
    assert out in ("[]", "['__future__']"), out
