# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-W — the bit-parallel Levenshtein returns exactly what the DP returned.

The cell-by-cell DP below is the pre-N-W ``normalized_levenshtein`` kept
verbatim as the reference; every case compares the integer distance and the
normalized float bit for bit.
"""

from __future__ import annotations

import random
import time

import pytest

from legba.data._url_canon import levenshtein_distance, normalized_levenshtein
from legba.data.filters.dedupe import _normalized_levenshtein


def _dp_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    n, m = len(a), len(b)
    if n < m:
        a, b = b, a
        n, m = m, n
    previous = list(range(m + 1))
    for i in range(1, n + 1):
        current = [i] + [0] * m
        ca = a[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ca == b[j - 1] else 1
            current[j] = min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + cost,
            )
        previous = current
    return previous[m]


def _dp_normalized(a: str, b: str) -> float:
    """The pre-N-W ``normalized_levenshtein``, verbatim in behaviour."""
    if a == b:
        return 0.0
    if not a or not b:
        return 1.0
    return _dp_distance(a, b) / float(max(len(a), len(b)))


_EDGE = [
    ("", ""), ("", "a"), ("a", ""), ("a", "a"), ("a", "b"), ("ab", "ba"),
    ("kitten", "sitting"), ("flaw", "lawn"), ("abc", "abcabc"),
    ("aaaa", "aaa"), ("ïran", "iran"), ("北京 talks", "北京 talk"),
    ("x" * 70, "y" * 70), ("a" * 200, "a" * 199 + "b"),
]


@pytest.mark.parametrize("a,b", _EDGE)
def test_edge_cases_match_the_dp(a: str, b: str) -> None:
    assert levenshtein_distance(a, b) == _dp_distance(a, b)
    assert levenshtein_distance(b, a) == _dp_distance(a, b)
    assert normalized_levenshtein(a, b) == _dp_normalized(a, b)


@pytest.mark.parametrize("alphabet", ["ab", "abcd", "abcdefghij klmnop", "aé北 "])
def test_random_strings_match_the_dp(alphabet: str) -> None:
    rng = random.Random(alphabet)
    for _ in range(600):
        a = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 90)))
        if rng.random() < 0.5:     # a near-duplicate of a, the common case
            b = list(a)
            for _ in range(rng.randint(0, 6)):
                op = rng.random()
                pos = rng.randint(0, len(b))
                if op < 0.33:
                    b.insert(pos, rng.choice(alphabet))
                elif b and op < 0.66:
                    del b[min(pos, len(b) - 1)]
                elif b:
                    b[min(pos, len(b) - 1)] = rng.choice(alphabet)
            b = "".join(b)
        else:
            b = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 90)))
        assert levenshtein_distance(a, b) == _dp_distance(a, b), (a, b)
        assert normalized_levenshtein(a, b) == _dp_normalized(a, b), (a, b)


def test_long_patterns_past_a_machine_word_match_the_dp() -> None:
    rng = random.Random(5)
    for _ in range(20):
        a = "".join(rng.choice("abcde ") for _ in range(rng.randint(64, 400)))
        b = "".join(rng.choice("abcde ") for _ in range(rng.randint(64, 400)))
        assert levenshtein_distance(a, b) == _dp_distance(a, b)


def test_the_dedupe_alias_is_the_same_function() -> None:
    assert _normalized_levenshtein is normalized_levenshtein


def test_headline_pairs_are_an_order_of_magnitude_faster() -> None:
    rng = random.Random(9)
    words = "iran talks strike port fire minister rate flood ceasefire".split()
    titles = [
        " ".join(rng.choice(words) for _ in range(rng.randint(8, 14)))
        for _ in range(60)
    ]
    pairs = [(a, b) for a in titles[:30] for b in titles[30:]]
    t0 = time.perf_counter()
    for a, b in pairs:
        _dp_normalized(a, b)
    dp = time.perf_counter() - t0
    t0 = time.perf_counter()
    for a, b in pairs:
        normalized_levenshtein(a, b)
    fast = time.perf_counter() - t0
    assert fast * 5 < dp, f"bit-parallel {fast:.3f}s vs DP {dp:.3f}s"
