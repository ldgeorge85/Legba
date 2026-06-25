# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compile-time source-length cap — the THREAD-SAFE structural bound on predicate
evaluation cost (``compiler._MAX_SOURCE_CHARS``), which is the off-main-thread
enforcement the SIGALRM wall-clock budget cannot provide."""
from __future__ import annotations

import pytest

from legba.data.predicates import (
    PredicateCompilationError,
    PredicateSurface,
    compile_predicate,
)
from legba.data.predicates.compiler import _MAX_SOURCE_CHARS


def test_oversized_predicate_rejected_at_compile():
    # A single boolean or-chain padded well past the cap (no banned tokens — so
    # it is the LENGTH gate, not the grammar gate, that must reject it).
    huge = 'has_tag("g20")' + (' or has_tag("g20")' * 1000)
    assert len(huge) > _MAX_SOURCE_CHARS
    with pytest.raises(PredicateCompilationError) as ei:
        compile_predicate(huge, PredicateSurface.ANALYST_SUBSCRIPTION)
    assert "too long" in str(ei.value)


def test_normal_predicate_under_cap_compiles():
    p = compile_predicate('has_tag("g20")', PredicateSurface.ANALYST_SUBSCRIPTION)
    assert p is not None
