# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""resolve_promoted_system_prompt — closes the optimizer loop (#37 stage D).

A GEPA candidate an operator flips to ``promotion_gate='promoted'`` becomes
the analyst's live system prompt. This resolver is what inference reads; it
must be fully best-effort (never break a run on a lookup hiccup).
"""
from __future__ import annotations

import asyncio

from legba.data.analysts.optimizer import resolve_promoted_system_prompt


class _FakeConn:
    def __init__(self, row, *, raise_exc=False):
        self._row = row
        self._raise = raise_exc

    async def fetchrow(self, query, *args):
        if self._raise:
            raise RuntimeError("db boom")
        return self._row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, row, *, raise_exc=False):
        self._conn = _FakeConn(row, raise_exc=raise_exc)

    def acquire(self):
        return _FakeAcquire(self._conn)


def _run(coro):
    # Fresh loop per call — robust when prior tests have created/closed loops
    # (e.g. the dspy bridge tests), unlike get_event_loop().run_until_complete.
    return asyncio.run(coro)


def test_returns_promoted_champion_text():
    pool = _FakePool({"text": "EVOLVED PROMPT: be sharper."})
    out = _run(resolve_promoted_system_prompt(pool, "country_assessor", default="BASE"))
    assert out == "EVOLVED PROMPT: be sharper."


def test_no_promoted_row_returns_default():
    pool = _FakePool(None)
    out = _run(resolve_promoted_system_prompt(pool, "country_assessor", default="BASE"))
    assert out == "BASE"


def test_empty_text_returns_default():
    pool = _FakePool({"text": None})
    out = _run(resolve_promoted_system_prompt(pool, "country_assessor", default="BASE"))
    assert out == "BASE"


def test_none_pool_returns_default():
    out = _run(resolve_promoted_system_prompt(None, "country_assessor", default="BASE"))
    assert out == "BASE"


def test_db_error_returns_default_never_raises():
    pool = _FakePool(None, raise_exc=True)
    out = _run(resolve_promoted_system_prompt(pool, "country_assessor", default="BASE"))
    assert out == "BASE"
