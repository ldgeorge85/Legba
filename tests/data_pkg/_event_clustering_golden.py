# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Digests of what the PRE-N-Z handler (base ee814cfe) wrote over the
``_event_clustering_fixture`` worlds, kept as the equivalence fixture for the
front split (``_event_clustering_front``).

Regenerate only on purpose: ``python3 tests/data_pkg/_event_clustering_golden.py``
with ``PYTHONPATH`` pointing at a checkout of the handler to record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

#: (events, signals, tower, seed, include_tower) -> sha256 of the writes and
#: the funnel, recorded from ee814cfe.
WORLDS = [
    (200, 70, 6, 3, True),
    (300, 100, 0, 29, False),
    (400, 260, 4, 51, True),
]
GOLDEN: dict[tuple, str] = {
    (200, 70, 6, 3, True):
        "0e6b08a7af4a36f819431a69c988a8682b896bd92971678e5930a6ed4e836c94",
    (300, 100, 0, 29, False):
        "785f514d2755e429a85158502718b7aadc08b82ac4c33a3a800142ac6aaef5a8",
    (400, 260, 4, 51, True):
        "9beebba340fe8586d88f08a70d30e81cb4d22edc7936d805fed829a27c03df64",
}


def digest(writes, funnel) -> str:
    body = json.dumps({"writes": writes, "funnel": dict(funnel)},
                      sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


async def compute(monkeypatch, world_key) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_event_clustering_loop_n_w import _run, _world

    events, signals, tower, seed, include_tower = world_key
    world = _world(events=events, signals=signals, tower=tower, seed=seed)
    writes, data = await _run(monkeypatch, world, legacy=False,
                              include_tower=include_tower)
    return digest(writes, data["funnel"])


if __name__ == "__main__":
    from _pytest.monkeypatch import MonkeyPatch

    for key in WORLDS:
        print(f"    {key!r}: {asyncio.run(compute(MonkeyPatch(), key))!r},")
