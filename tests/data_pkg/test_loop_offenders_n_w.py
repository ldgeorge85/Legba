# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-W — the smaller loop-lag offenders roll 76's monitor named on live.

Each fix keeps its output bit-equal to the code it replaces (the old code is
kept here as the reference) and only removes repeated work.
"""

from __future__ import annotations

import math
import random
import time
from typing import Sequence

from legba.data.analysts.deterministic_handlers import claim_watch as cw


def _old_cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def test_claim_watch_cosine_with_precomputed_norms_is_bit_equal() -> None:
    rng = random.Random(4)
    vecs = [[rng.gauss(0, 1) for _ in range(384)] for _ in range(40)]
    vecs += [[0.0] * 384, [1e-300] * 384, []]
    for a in vecs:
        na = cw.vector_norm(a) if a else 0.0
        for b in vecs:
            want = _old_cosine(a, b)
            assert cw.cosine_similarity(a, b) == want
            assert cw.cosine_similarity(
                a, b, norm_a=na, norm_b=cw.vector_norm(b)
            ) == want
    assert cw.cosine_similarity([1.0, 2.0], [1.0]) == 0.0


def test_claim_watch_cosine_with_norms_is_several_times_faster() -> None:
    rng = random.Random(6)
    a = [rng.gauss(0, 1) for _ in range(1024)]
    qs = [[rng.gauss(0, 1) for _ in range(1024)] for _ in range(200)]
    t0 = time.perf_counter()
    for q in qs:
        _old_cosine(a, q)
    old = time.perf_counter() - t0
    na = cw.vector_norm(a)
    norms = [cw.vector_norm(q) for q in qs]
    t0 = time.perf_counter()
    for q, nq in zip(qs, norms):
        cw.cosine_similarity(a, q, norm_a=na, norm_b=nq)
    new = time.perf_counter() - t0
    assert new * 3 < old, f"{new:.4f}s vs {old:.4f}s"


async def test_the_egress_guard_resolves_off_the_event_loop(monkeypatch) -> None:
    """A slow resolver (live: 2-3.6 s) no longer holds the loop; a private
    answer is still refused with the same error."""
    import asyncio
    import socket

    import httpx
    import pytest

    from legba.data.sources import _egress

    def slow_resolve(host, port, *a, **kw):
        time.sleep(0.6)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    monkeypatch.setattr(_egress.socket, "getaddrinfo", slow_resolve)
    transport = _egress.SsrfGuardedTransport()
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    with pytest.raises(_egress.EgressBlockedError, match="non-public"):
        await transport.handle_async_request(
            httpx.Request("GET", "https://slow.example.org/feed")
        )
    stop.set()
    await beat
    await transport.aclose()
    assert max(gaps) < 0.3, f"the loop was held {max(gaps):.2f}s"


def _old_naming_counts(findings, *, candidates_by_desk):
    from legba.data._polity_match import represented_by

    by_dimension: dict = {}
    by_desk: dict = {}
    for row in findings:
        desk = str(row.get("target_id") or "")
        candidates = candidates_by_desk.get(desk)
        if not candidates:
            continue
        prose = f"{row.get('title') or ''} || {row.get('body') or ''}"
        dimension = str(row.get("analyst_id") or "")
        for polity in candidates:
            if represented_by(polity, prose) is None:
                continue
            key = (desk, dimension, polity)
            by_dimension[key] = by_dimension.get(key, 0) + 1
            by_desk[(desk, polity)] = by_desk.get((desk, polity), 0) + 1
    return by_dimension, by_desk


def test_frame_naming_counts_fold_once_and_count_the_same() -> None:
    """The gauge folded a finding's whole body once PER candidate polity
    (live: 3.5 s on the loop); it now folds once, with the same counts."""
    from legba.data import _frame_content, _polity_match

    polities = sorted(_polity_match._SURFACES)[:120]
    rng = random.Random(12)
    words = ["talks", "the", "Palestinians'", "South-African", "U.S.", "Russia's",
             "minister", "Israel-Lebanon", "SouthAfrica", "border", "says"]
    findings = []
    for i in range(300):
        named = rng.sample(polities, 4)
        body = " ".join(
            rng.choice(words + named) for _ in range(rng.randint(40, 200))
        )
        findings.append({
            "target_id": f"desk{i % 5}", "analyst_id": f"dim{i % 7}",
            "title": f"{rng.choice(named)} {rng.choice(words)}", "body": body,
        })
    desks = {f"desk{d}": polities[d * 20: d * 20 + 40] for d in range(5)}
    want = _old_naming_counts(findings, candidates_by_desk=desks)
    got = _frame_content._naming_counts(findings, candidates_by_desk=desks)
    assert got == want
    assert sum(want[1].values()) > 100
