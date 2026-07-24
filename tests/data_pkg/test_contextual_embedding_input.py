# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M22 — the corpus embedding convention (lane4_loader.contextual_embedding_input).

The loader now embeds a LEAN "<Country> — <section-leaf>" context prefix + the
chunk body (not the raw body alone), so a chunk vector carries the country + topic
anchor a focused "<country> <theme>" query matches. These tests pin the exact
prefix shape (the loader AND the in-place re-embed script share this single helper,
so a drift would silently split the two paths)."""

from __future__ import annotations

from legba.data.rag.lane4_loader import contextual_embedding_input


def test_lean_prefix_country_and_section_leaf():
    out = contextual_embedding_input(
        title="Iran — CIA World Factbook country background",
        section="Iran — CIA World Factbook (stable background) > Government",
        countries=["ir", "IR", "Iran"],
        text="The supreme leader is head of state.",
    )
    # Country NAME (not the ISO codes) + the LAST heading segment; the
    # "CIA World Factbook (stable background)" boilerplate is dropped.
    assert out.startswith("Iran — Government\n\n")
    assert out.endswith("The supreme leader is head of state.")
    assert "stable background" not in out.split("\n\n", 1)[0]


def test_country_prefers_name_over_iso_codes():
    out = contextual_embedding_input(
        title="t", section="Economy", countries=["de", "DE", "Germany"],
        text="body",
    )
    assert out.startswith("Germany — Economy\n\n")


def test_no_heading_marker_uses_whole_section():
    out = contextual_embedding_input(
        title="t", section="Military and Security", countries=["Brazil"], text="body",
    )
    assert out.startswith("Brazil — Military and Security\n\nbody")


def test_degrades_to_raw_text_without_anchor():
    # No country + no section → the raw body, never a fabricated anchor.
    assert contextual_embedding_input(
        title=None, section=None, countries=[], text="just the body",
    ) == "just the body"


def test_section_only_when_no_country():
    out = contextual_embedding_input(
        title=None, section="Overview", countries=None, text="body",
    )
    assert out == "Overview\n\nbody"


def test_country_only_when_no_section():
    out = contextual_embedding_input(
        title=None, section=None, countries=["ir", "IR", "Iran"], text="body",
    )
    assert out == "Iran\n\nbody"
