# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the heading-aware RAG chunker (S5-T2).

Pure + deterministic — no infra. Cover: token estimate, single-chunk short
text, heading-aware section splitting, oversized-section hard split with
overlap, section-label trails, and empty input.
"""

from __future__ import annotations

from legba.data.rag.chunker import Chunk, chunk_text, estimate_tokens


def test_estimate_tokens_scales_with_words() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("one") == max(1, round(1 * 1.3))
    # Monotonic in word count.
    assert estimate_tokens("a b c d e") > estimate_tokens("a b")


def test_short_text_is_a_single_chunk() -> None:
    chunks = chunk_text("A short synthetic brief.", base_section="overview")
    assert len(chunks) == 1
    c = chunks[0]
    assert isinstance(c, Chunk)
    assert c.seq == 0
    assert c.section == "overview"
    assert "synthetic" in c.text


def test_empty_or_blank_yields_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  \t ") == []


def test_headings_split_sections_and_label_them() -> None:
    text = (
        "# Overview\n\n"
        "Testlandia is a synthetic polity.\n\n"
        "# Economy\n\n"
        "Its economy is fictional and small.\n"
    )
    chunks = chunk_text(text)
    sections = {c.section for c in chunks}
    assert "Overview" in sections
    assert "Economy" in sections
    # No chunk straddles the two headings (each chunk sits in one section).
    for c in chunks:
        assert c.section in {"Overview", "Economy"}
    # seq is contiguous from 0.
    assert [c.seq for c in chunks] == list(range(len(chunks)))


def test_nested_heading_trail_is_joined() -> None:
    text = "# Background\n\n## Trade\n\nExports are synthetic goods.\n"
    chunks = chunk_text(text)
    assert any(c.section == "Background > Trade" for c in chunks)


def test_oversized_section_hard_splits_with_overlap() -> None:
    # One heading, a body far over the token band → multiple chunks.
    sentence = "The synthetic ministry issued a fictional communique today. "
    body = sentence * 120  # ~ hundreds of words
    text = "# Report\n\n" + body
    chunks = chunk_text(text, max_tokens=200, target_tokens=150, overlap_tokens=32)
    assert len(chunks) >= 2
    # Every chunk respects the hard cap (token estimate).
    for c in chunks:
        assert c.token_estimate <= 200
        assert c.section == "Report"
    # Overlap: consecutive chunks share some trailing/leading words.
    first_tail = set(chunks[0].text.split()[-8:])
    second_head = set(chunks[1].text.split()[:16])
    assert first_tail & second_head


def test_deterministic_same_input_same_output() -> None:
    text = "# A\n\npara one here.\n\n# B\n\npara two here.\n"
    a = chunk_text(text)
    b = chunk_text(text)
    assert [(c.text, c.section, c.seq) for c in a] == [
        (c.text, c.section, c.seq) for c in b
    ]
