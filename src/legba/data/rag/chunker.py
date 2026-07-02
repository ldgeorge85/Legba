# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.rag.chunker — heading-aware document chunker (S5-T2).

Splits a document into retrieval chunks of ~400-800 tokens with a small
overlap, respecting markdown heading structure so a chunk never straddles a
section boundary and each chunk carries the heading trail it came from (the
``section`` payload field the RAG plan asks for).

Design (RAG plan §B "Collections"):

  * ``max_tokens`` (hard cap, default 800) — a chunk never exceeds this; an
    over-long paragraph is hard-split on sentence then word boundaries.
  * ``target_tokens`` (soft flush, default 512) — the packer flushes a chunk
    once it reaches this, keeping chunks in the ~400-800 band.
  * ``overlap_tokens`` (default 64) — the tail of a flushed chunk is prepended
    to the next chunk WITHIN THE SAME SECTION so a fact split across a chunk
    boundary is still retrievable from either side. Overlap never crosses a
    heading (a new section starts clean).

Token counting: the runtime image carries NO tokenizer (sentence-transformers
retired in L-205; bge-m3's tokenizer is not loaded), so :func:`estimate_tokens`
is a fast whitespace-word proxy scaled by ``_TOKENS_PER_WORD`` (~1.3, a
conservative English word→subword-token ratio). It is an ESTIMATE — the bands
are guidance, not a hard contract against a specific tokenizer. Callers that
need exact counts can pass their own ``token_counter``.

The chunker is pure + deterministic (same input → same chunks), which is what
makes the Lane-4 loader's re-run idempotency and force delete-and-reload work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ATX markdown heading: 1-6 leading '#', then the title text.
_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# Sentence boundary for hard-splitting an over-long paragraph. Deliberately
# simple (terminator + whitespace) — good enough for chunk sizing, not NLP.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"\S+")

# English word → subword-token inflation factor (a conservative over-estimate
# so chunks land at/under the intended token band rather than over it).
_TOKENS_PER_WORD = 1.3


def estimate_tokens(text: str) -> int:
    """Approximate the token count of ``text`` (whitespace-word proxy).

    ``round(word_count * _TOKENS_PER_WORD)``. An estimate — see the module
    docstring on why no real tokenizer is loaded in the runtime image.
    """
    if not text:
        return 0
    words = len(_WORD_RE.findall(text))
    return max(1, round(words * _TOKENS_PER_WORD))


@dataclass(frozen=True)
class Chunk:
    """One retrieval chunk.

    ``seq`` is the 0-based index of this chunk within the chunked input (the
    Lane-4 loader uses it as the ``chunk_part`` sub-index of the source
    record). ``section`` is the heading trail (e.g. ``"Economy"`` or
    ``"Background > Trade"``) or the caller's ``base_section`` when the text
    carries no headings.
    """

    text: str
    section: str
    seq: int
    token_estimate: int


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass
class _Block:
    """A paragraph-sized unit tagged with the heading trail it sits under."""

    section: str
    text: str


def _join_trail(trail: list[str], base_section: str) -> str:
    """Render a heading stack as a ``' > '``-joined section label."""
    parts = [p for p in trail if p]
    if not parts:
        return base_section
    return " > ".join(parts)


def _split_into_blocks(text: str, base_section: str) -> list[_Block]:
    """Break text into (heading-tagged) paragraph blocks.

    ATX headings update the running heading stack (a level-N heading replaces
    everything at level >= N); blank lines separate paragraphs. Each non-empty
    paragraph becomes one block stamped with the current heading trail.
    """
    # heading stack indexed by level 1..6 (0 unused).
    stack: list[str] = ["", "", "", "", "", "", ""]
    blocks: list[_Block] = []
    buf: list[str] = []

    def _flush_para() -> None:
        if not buf:
            return
        para = "\n".join(buf).strip()
        buf.clear()
        if para:
            blocks.append(_Block(section=_join_trail(stack[1:], base_section), text=para))

    for raw_line in text.splitlines():
        m = _ATX_HEADING_RE.match(raw_line.strip())
        if m:
            _flush_para()
            level = len(m.group(1))
            stack[level] = m.group(2).strip()
            for deeper in range(level + 1, 7):
                stack[deeper] = ""
            continue
        if not raw_line.strip():
            _flush_para()
            continue
        buf.append(raw_line.rstrip())
    _flush_para()
    return blocks


def _count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


def _words_for(tokens: int) -> int:
    """Convert a token budget to a whitespace-word budget (the inverse of
    :func:`estimate_tokens`). Sizing happens in WORDS internally so a chunk's
    reported ``estimate_tokens`` never drifts over the cap the way summing
    per-unit rounded token counts would."""
    return max(1, int(tokens / _TOKENS_PER_WORD))


def _hard_split(text: str, max_words: int) -> list[str]:
    """Split an over-long block into <= max_words pieces (sentences → words)."""
    pieces: list[str] = []
    cur: list[str] = []
    cur_w = 0

    def _flush() -> None:
        nonlocal cur, cur_w
        if cur:
            pieces.append(" ".join(cur).strip())
            cur = []
            cur_w = 0

    units = [s for s in _SENTENCE_RE.split(text) if s.strip()]
    for unit in units:
        uw = _count_words(unit)
        if uw > max_words:
            # A single sentence still too big — fall back to fixed-size word
            # windows so no piece exceeds the cap.
            _flush()
            words = _WORD_RE.findall(unit)
            for i in range(0, len(words), max_words):
                pieces.append(" ".join(words[i : i + max_words]))
            continue
        if cur and cur_w + uw > max_words:
            _flush()
        cur.append(unit.strip())
        cur_w += uw
    _flush()
    return pieces or ([text.strip()] if text.strip() else [])


def _overlap_tail(text: str, overlap_words: int) -> str:
    """Return the trailing ``overlap_words`` words of ``text`` (chunk overlap)."""
    if overlap_words <= 0:
        return ""
    words = _WORD_RE.findall(text)
    if not words:
        return ""
    return " ".join(words[-overlap_words:])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chunk_text(
    text: str,
    *,
    max_tokens: int = 800,
    target_tokens: int = 512,
    overlap_tokens: int = 64,
    base_section: str = "",
) -> list[Chunk]:
    """Chunk ``text`` heading-aware into ~target..max-token :class:`Chunk`\\ s.

    Greedy packer: paragraph blocks (tagged by heading trail) accumulate into
    the current chunk until adding the next block would exceed ``max_tokens``
    OR the chunk has reached ``target_tokens``; then it flushes. A heading
    change always flushes (chunks never span sections). An individual block
    larger than ``max_tokens`` is hard-split (sentences → words). Overlap
    (``overlap_tokens`` trailing words of the just-flushed chunk) seeds the
    next chunk only within the same section.

    Sizing is done in WORDS (derived from the token budgets via the
    :func:`estimate_tokens` ratio) so a chunk's reported ``token_estimate``
    never exceeds ``max_tokens``. Returns ``[]`` for empty/whitespace input;
    ``seq`` is contiguous 0..N-1.
    """
    if not text or not text.strip():
        return []
    if target_tokens > max_tokens:
        target_tokens = max_tokens

    max_words = _words_for(max_tokens)
    target_words = _words_for(target_tokens)
    overlap_words = _words_for(overlap_tokens) if overlap_tokens > 0 else 0
    overlap_words = min(overlap_words, max(0, max_words - 1))
    # Leave room for the prepended overlap so tail + piece never exceeds the cap.
    piece_max = max(1, max_words - overlap_words)

    blocks = _split_into_blocks(text, base_section)
    chunks: list[Chunk] = []
    cur_section = base_section
    cur_parts: list[str] = []
    cur_w = 0

    def _flush(next_section: str | None) -> None:
        nonlocal cur_parts, cur_w, cur_section
        if cur_parts:
            body = "\n\n".join(p for p in cur_parts if p).strip()
            if body:
                chunks.append(
                    Chunk(
                        text=body,
                        section=cur_section,
                        seq=len(chunks),
                        token_estimate=estimate_tokens(body),
                    )
                )
        cur_parts = []
        cur_w = 0
        if next_section is not None:
            cur_section = next_section

    for block in blocks:
        # Section boundary → flush and reset (no cross-section overlap).
        if block.section != cur_section and cur_parts:
            _flush(block.section)
        elif not cur_parts:
            cur_section = block.section

        # Break an over-long block into <= piece_max-word pieces up front.
        for piece in _hard_split(block.text, piece_max):
            pw = _count_words(piece)
            if cur_parts and (cur_w + pw > max_words or cur_w >= target_words):
                tail = _overlap_tail("\n\n".join(cur_parts), overlap_words)
                _flush(None)
                if tail:
                    cur_parts.append(tail)
                    cur_w += _count_words(tail)
            cur_parts.append(piece)
            cur_w += pw
    _flush(None)
    return chunks


__all__ = ["Chunk", "chunk_text", "estimate_tokens"]
