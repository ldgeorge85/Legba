# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every verify span reason has a plain-English label in the UI (W1 F-5).

``legba-ui-v3/src/lib/claimVerdicts.ts`` ``CLAIM_REASON_LABELS`` says it is a
superset of every ``UnsupportedSpan.reason``. ``verify._FAIL_CLASS_BY_REASON``
is THE table of those reasons (the composition-integrity and assembly-arm
classes are merged into it), and it grew past the labels: 32 reasons rendered
as a de-underscored key instead of a sentence. The test reads the TS file as
text — no node on the host — and asserts every key of the table has a label.
A label for a reason the table no longer carries is allowed (the UI may name a
reason a persisted critique still holds); a MISSING label is not.
"""

from __future__ import annotations

import re
from pathlib import Path

from legba.data.provenance.verify import _FAIL_CLASS_BY_REASON

_TS = (
    Path(__file__).resolve().parents[2]
    / "legba-ui-v3" / "src" / "lib" / "claimVerdicts.ts"
)


def _label_keys() -> dict[str, str]:
    """``CLAIM_REASON_LABELS`` entries: key -> label text (single-quoted)."""
    src = _TS.read_text()
    start = src.index("export const CLAIM_REASON_LABELS")
    end = src.index("\n}\n", start)
    body = src[start:end]
    out: dict[str, str] = {}
    for m in re.finditer(
        r"^\s{2}([a-z][a-z0-9_]*):\s*'((?:[^'\\]|\\.)*)'", body, re.M | re.S,
    ):
        out[m.group(1)] = m.group(2)
    return out


def test_the_label_table_parses():
    labels = _label_keys()
    assert len(labels) >= 16, "the CLAIM_REASON_LABELS parse found too few keys"


def test_every_fail_class_reason_has_a_label():
    labels = _label_keys()
    missing = sorted(set(_FAIL_CLASS_BY_REASON) - set(labels))
    assert not missing, (
        "verify._FAIL_CLASS_BY_REASON has reasons with no entry in "
        f"claimVerdicts.ts CLAIM_REASON_LABELS: {missing}"
    )


def test_every_label_is_one_plain_line():
    for key, text in _label_keys().items():
        assert text.strip(), f"{key}: empty label"
        assert "\n" not in text, f"{key}: label spans lines"
        assert key not in text, f"{key}: the label repeats the raw key"
