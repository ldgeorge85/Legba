# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Roll-28 proof residue (2026-09-28): the core plane cites whole substrate ids
in full-width brackets — ``【084b483a-…】`` — and the write-time normalizer
rewrote only ORDINAL brackets, so the persisted answer kept the variant form
the panel's ``[<uuid>]`` chip parser cannot see."""

from __future__ import annotations

from legba.data.provenance.citation_markers import normalize_inline_citation_markers

U = "084b483a-674d-4eeb-9bbd-99a93a8b1ad8"


def test_full_width_uuid_citation_becomes_ascii():
    assert normalize_inline_citation_markers(f"Frames 【{U}】 rose.") == f"Frames [{U}] rose."


def test_every_variant_bracket_pair_and_upper_case():
    for o, c in ("【】", "［］", "〔〕", "〖〗"):
        assert normalize_inline_citation_markers(f"{o}{U.upper()}{c}") == f"[{U}]"


def test_ordinals_and_annotations_still_normalize_beside_it():
    out = normalize_inline_citation_markers(f"a 【3】 b 【{U}】 c 【none】")
    assert out == f"a [3] b [{U}] c [no citation]"


def test_idempotent_on_ascii():
    text = f"already [{U}] and [2]"
    assert normalize_inline_citation_markers(text) == text
