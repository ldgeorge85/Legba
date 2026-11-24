# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Translation on the innoai gateway's chat model (W11-F109) — the pure parts.

The gateway has no translation route, but it serves a multilingual chat model
(alias ``fast``: thinking off) on ``POST /chat/completions``.
:meth:`GatewayNlpClient.translate` asks it for the translation, one completion
per chunk; this module holds what does not touch the network:

  * :func:`system_prompt` — the strict instruction (output ONLY the
    translation, keep names / numbers / dates / paragraph breaks, return
    already-target-language text unchanged);
  * :func:`split_for_translation` — paragraph-boundary chunking with the
    original separators kept, so the joined output keeps the source layout;
  * :func:`chat_content` / :func:`clean_translation` — read the completion and
    strip what a chat model wraps an answer in (code fences, a wrapping quote
    pair, a ``Translation:`` label, an empty think block);
  * :func:`within_size_bound` — the garbage guard: an answer more than
    :data:`MAX_OUTPUT_RATIO` times its input is refused, never stamped.

Nothing here logs; nothing here sees a key.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

#: Chunk ceiling in characters. Every caller truncates before calling (the NER
#: filter at ``max_text_chars`` = 2,000, the translation sweep at 4,000), so a
#: live call is ONE completion; the ceiling exists for a caller that does not.
#: 6,000 characters of Cyrillic or Arabic is roughly 2-3k tokens in and as many
#: out, one bounded completion on the flash model inside the client's 30 s
#: timeout and the sweep's 60 s per-call belt, without sending ``max_tokens``.
DEFAULT_CHUNK_CHARS = 6_000

#: An answer longer than this multiple of its input is refused (unavailable).
#: Measured in UTF-8 BYTES: a CJK character is 3 bytes and its English rendering
#: runs 3-4 characters, so a character ratio would refuse honest CJK titles,
#: while a byte ratio stays ~1x for every script and still catches a preamble,
#: a commentary or a run-on generation.
MAX_OUTPUT_RATIO = 3

#: ISO 639-1 codes the platform routes through translation, plus the common
#: Latin-script sources, as the English names the prompt uses. An unlisted code
#: is named by its code; no code (``None`` / ``und``) is "text".
LANGUAGE_NAMES: dict[str, str] = {
    "ar": "Arabic", "de": "German", "en": "English", "es": "Spanish",
    "fa": "Persian", "fr": "French", "he": "Hebrew", "hi": "Hindi",
    "it": "Italian", "ja": "Japanese", "ko": "Korean", "pl": "Polish",
    "pt": "Portuguese", "ru": "Russian", "th": "Thai", "tr": "Turkish",
    "uk": "Ukrainian", "ur": "Urdu", "zh": "Chinese",
}

_UNDETERMINED = frozenset({"", "und", "unknown", "mul", "zxx"})


def normalise_lang(code: str | None) -> str | None:
    """``"ru-RU"`` → ``"ru"``; ``None`` / blank / ``und`` → ``None``."""
    if not isinstance(code, str):
        return None
    base = code.strip().lower().split("-", 1)[0].split("_", 1)[0]
    return None if base in _UNDETERMINED else base


def system_prompt(source_lang: str | None, target_lang: str = "en") -> str:
    """The strict translation instruction for one completion."""
    src = normalise_lang(source_lang)
    if src is None:
        source = "text"
    elif src in LANGUAGE_NAMES:
        source = f"{LANGUAGE_NAMES[src]} text"
    else:
        source = f"text (language code '{src}')"
    tgt = normalise_lang(target_lang) or "en"
    target = LANGUAGE_NAMES.get(tgt, f"the language with code '{tgt}'")
    return (
        f"Translate the following {source} into {target}. Output ONLY the "
        "translation, no preamble, no notes, preserve names, numbers, dates and "
        f"paragraph breaks; if the text is already {target} return it unchanged."
    )


_SPLIT_LEVELS: tuple[str, ...] = (
    r"\n[ \t]*\n\s*",                       # blank-line paragraphs
    r"\n",                                  # single-newline lines (telegram)
    r"(?<=[.!?。！？؟।])\s+",  # sentence ends, any script
)


def split_for_translation(
    text: str, limit: int = DEFAULT_CHUNK_CHARS,
) -> tuple[list[str], list[str]]:
    """Split ``text`` into chunks of at most ``limit`` characters.

    Returns ``(chunks, joins)`` with ``len(joins) == len(chunks) - 1``: the
    original separator between consecutive chunks, so
    ``chunks[0] + joins[0] + chunks[1] + ...`` is ``text`` exactly. Splits at
    the coarsest boundary that exists — blank-line paragraphs, then lines, then
    sentence ends — and cuts hard only inside a sentence longer than ``limit``.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    return _split(text, limit, 0)


def _split(text: str, limit: int, level: int) -> tuple[list[str], list[str]]:
    if len(text) <= limit:
        return [text], []
    if level >= len(_SPLIT_LEVELS):
        chunks = [text[i:i + limit] for i in range(0, len(text), limit)]
        return chunks, [""] * (len(chunks) - 1)
    parts = re.split(f"({_SPLIT_LEVELS[level]})", text)
    pieces, seps = parts[0::2], parts[1::2]
    if len(pieces) == 1:
        return _split(text, limit, level + 1)
    # Greedy pack: consecutive pieces share a chunk while they fit.
    packed: list[str] = []
    packed_joins: list[str] = []
    cur = pieces[0]
    for sep, piece in zip(seps, pieces[1:]):
        if len(cur) + len(sep) + len(piece) <= limit:
            cur += sep + piece
        else:
            packed.append(cur)
            packed_joins.append(sep)
            cur = piece
    packed.append(cur)
    # A single piece still over the limit splits at the next-finer boundary.
    chunks: list[str] = []
    joins: list[str] = []
    for i, chunk in enumerate(packed):
        sub, sub_joins = _split(chunk, limit, level + 1)
        if i:
            joins.append(packed_joins[i - 1])
        chunks.extend(sub)
        joins.extend(sub_joins)
    return chunks, joins


def chat_content(data: Any) -> str | None:
    """``choices[0].message.content`` as a string (content parts joined), or
    ``None`` when the completion carries no readable content."""
    if not isinstance(data, Mapping):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return None
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            p.get("text") for p in content
            if isinstance(p, Mapping) and isinstance(p.get("text"), str)
        ]
        return "".join(texts) if texts else None
    return None


_THINK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
_FENCE_RE = re.compile(r"^```[\w-]*[ \t]*\n?(.*?)\n?```$", re.DOTALL)
_LABEL_RE = re.compile(
    r"^(?:english\s+)?translation(?:\s+into\s+english)?\s*:\s*", re.IGNORECASE,
)
_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (
    ('"', '"'), ("'", "'"), ("“", "”"), ("«", "»"),
    ("「", "」"), ("‘", "’"),
)
_OPENING_QUOTES = frozenset(opening for opening, _ in _QUOTE_PAIRS)


def clean_translation(output: str, source: str) -> str:
    """Strip the wrappers a chat model adds around an answer.

    A wrapping quote pair is removed only when the SOURCE does not itself open
    with a quote mark, and only when the pair encloses the whole answer (no
    inner use of the closing mark), so a quoted headline keeps its quotes.
    """
    text = _THINK_RE.sub("", output).strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    text = _LABEL_RE.sub("", text, count=1).strip()
    if source.strip()[:1] in _OPENING_QUOTES:
        return text
    for opening, closing in _QUOTE_PAIRS:
        if (
            len(text) >= 2 and text.startswith(opening) and text.endswith(closing)
            and closing not in text[1:-1]
        ):
            return text[1:-1].strip()
    return text


def within_size_bound(output: str, source: str) -> bool:
    """``True`` when the answer is at most :data:`MAX_OUTPUT_RATIO` times its
    input, in UTF-8 bytes."""
    return len(output.encode("utf-8")) <= MAX_OUTPUT_RATIO * len(source.encode("utf-8"))


__all__ = [
    "DEFAULT_CHUNK_CHARS",
    "LANGUAGE_NAMES",
    "MAX_OUTPUT_RATIO",
    "chat_content",
    "clean_translation",
    "normalise_lang",
    "split_for_translation",
    "system_prompt",
    "within_size_bound",
]
