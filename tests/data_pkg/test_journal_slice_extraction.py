# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B0 (JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §6, the T2.2 seam taken
early) — byte-identity proof for the priming-slice SELECTION machinery's
extraction from ``journal_assessor.py`` to ``journal_slice.py``.

Two layers, mirroring ``test_journal_reflect_extraction.py`` (T1.0)'s own
proof shape:

  * Three committed SYNTHETIC row lists drive ``_select_journal_slice``'s
    branches: nothing scored (byte-for-byte delivered order, plain [:cap]
    cut), everything scored and under the cap (plain salience order,
    unchanged), and MORE than the cap scored with one fresh unscored
    breaking row (the freshness-floor tail-reserve branch).

  * A live check (skipped when the deployment's Postgres isn't reachable)
    pulls the 24h global signal slice shape (id / salience / produced_at /
    source_id / title) for up to 300 recent signals and runs the SAME
    comparison — proving the extraction holds over a REAL slice shape, not
    just the synthetic fixtures.

Both compare the pre-extraction function — loaded via ``importlib`` from
``git show <OLD_SHA>:...journal_assessor.py`` written to a temp file (never
``git stash`` on a shared repo) with its ``__package__`` pinned to
``legba.data.analysts`` so its relative imports (``.signal_salience``) resolve
against the real, currently-checked-out sibling — against BOTH the new
``journal_slice._select_journal_slice`` and the ``journal_assessor``
re-export, asserting the returned row lists are IDENTICAL (same ids, same
order).
"""

from __future__ import annotations

import importlib.util
import os
import socket
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from legba.data.analysts import journal_assessor as new_journal_assessor
from legba.data.analysts import journal_slice as new_journal_slice

REPO_ROOT = Path(__file__).resolve().parents[2]
# The worktree's pinned base commit — journal_assessor.py as it stood
# immediately BEFORE the B0 extraction landed (== the T1.0 head this lane
# started from).
OLD_SHA = "0dc6cc927de2af42aa20bbd61873379518505da5"
OLD_REL_PATH = "src/legba/data/analysts/journal_assessor.py"


def _load_old_select_journal_slice(tmp_path: Path):
    """Load ``journal_assessor.py`` exactly as it was at ``OLD_SHA`` via
    ``importlib`` from a temp file, and return its ``_select_journal_slice``.

    ``git stash`` is forbidden on this shared repo — ``git show <sha>:<path>``
    reads the historical blob without touching the working tree. The loaded
    module's ``__package__`` is pinned to ``legba.data.analysts`` so its
    ``from .signal_salience import ...`` relative import resolves against the
    real package (an unchanged sibling), not a floating standalone module.
    """
    if shutil.which("git") is None:
        # The nightly rig runs in the legba-test image, which ships no git —
        # the base-commit identity proof is host-only; the fixture-based
        # identity tests in this file still run there (2026-09-11).
        pytest.skip("git is not available on this rig; base-commit identity proof is host-only")
    old_source = subprocess.run(
        ["git", "show", f"{OLD_SHA}:{OLD_REL_PATH}"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    old_path = tmp_path / "journal_assessor_pre_b0.py"
    old_path.write_text(old_source, encoding="utf-8")

    mod_name = "legba.data.analysts._journal_assessor_pre_b0_test"
    spec = importlib.util.spec_from_file_location(mod_name, old_path)
    assert spec is not None and spec.loader is not None
    old_module = importlib.util.module_from_spec(spec)
    old_module.__package__ = "legba.data.analysts"
    sys.modules[mod_name] = old_module
    try:
        spec.loader.exec_module(old_module)
    finally:
        sys.modules.pop(mod_name, None)
    return old_module._select_journal_slice


def _ids(rows: list[dict]) -> list:
    return [r.get("id") for r in rows]


# ---------------------------------------------------------------------------
# Synthetic fixtures — shaped like the delivered priming slice, not live rows.
# ---------------------------------------------------------------------------


def _sig(sid: str, mag: float | None, when: str, authority: str = "reporting",
         event_class: str = "other") -> dict:
    row: dict = {"id": sid, "title": f"row {sid}", "produced_at": when}
    if mag is not None:
        row["salience"] = {
            "magnitude": mag, "authority": authority, "event_class": event_class,
        }
    return row


FIXTURE_NOTHING_SCORED = [
    _sig(f"r{i}", None, f"2026-07-{10 + (i % 5):02d}T00:00:00+00:00")
    for i in range(80)
]

FIXTURE_UNDER_CAP_SCORED = [
    _sig("khamenei", 0.95, "2026-07-12T18:00:00+00:00", "state_media", "leader_death"),
    _sig("graham", 0.30, "2026-07-13T08:00:00+00:00"),
    _sig("official", 0.9, "2026-07-12T00:00:00+00:00", "official", "kinetic_strike"),
]

FIXTURE_FRESH_FLOOR = [
    _sig(f"routine{i}", 0.2, "2026-07-10T00:00:00+00:00") for i in range(65)
] + [_sig("breaking", None, "2026-07-14T23:00:00+00:00")]

FIXTURES = (
    ("nothing_scored", FIXTURE_NOTHING_SCORED),
    ("under_cap_scored", FIXTURE_UNDER_CAP_SCORED),
    ("fresh_floor", FIXTURE_FRESH_FLOOR),
)


@pytest.mark.parametrize(
    "rows", [r for _, r in FIXTURES], ids=[i for i, _ in FIXTURES]
)
def test_select_journal_slice_byte_identical_to_pre_extraction(rows, tmp_path) -> None:
    old_select = _load_old_select_journal_slice(tmp_path)
    old_result = old_select(rows)

    new_result = new_journal_slice._select_journal_slice(rows)
    assert _ids(new_result) == _ids(old_result)
    assert new_result == old_result

    # The journal_assessor re-export must be the SAME function, not a copy
    # that could silently drift.
    assert (
        new_journal_assessor._select_journal_slice
        is new_journal_slice._select_journal_slice
    )
    assessor_result = new_journal_assessor._select_journal_slice(rows)
    assert assessor_result == old_result


# ---------------------------------------------------------------------------
# Live check — the 24h global signal slice shape, read-only.
# ---------------------------------------------------------------------------

_LIVE_PG_HOST = os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1")
_LIVE_PG_PORT = int(os.environ.get("LEGBA_DATA_PG_PORT", "5432"))
_LIVE_PG_USER = os.environ.get("LEGBA_DATA_PG_USER", "legba")
_LIVE_PG_PASSWORD = os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba")
_LIVE_PG_DB = os.environ.get("LEGBA_DATA_PG_DB", "legba")


def _live_pg_port_open() -> bool:
    try:
        with socket.create_connection((_LIVE_PG_HOST, _LIVE_PG_PORT), timeout=1.5):
            return True
    except OSError:
        return False


@pytest.mark.skipif(
    not _live_pg_port_open(),
    reason=(
        f"live legba Postgres not reachable at {_LIVE_PG_HOST}:{_LIVE_PG_PORT} "
        "— this check only runs against a real deployment's signals table"
    ),
)
async def test_select_journal_slice_byte_identical_on_live_24h_shape(tmp_path) -> None:
    import asyncpg

    dsn = (
        f"postgresql://{_LIVE_PG_USER}:{_LIVE_PG_PASSWORD}@"
        f"{_LIVE_PG_HOST}:{_LIVE_PG_PORT}/{_LIVE_PG_DB}"
    )
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"could not connect to live signals db: {exc!r}")
        return
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        rows = await conn.fetch(
            "SELECT id::text, source_id, "
            "coalesce(payload->>'title', payload->>'headline', '') AS title, "
            "salience, fetched_at AS produced_at FROM signals "
            "WHERE fetched_at >= $1 ORDER BY fetched_at DESC LIMIT 300",
            since,
        )
    finally:
        await conn.close()

    if not rows:
        pytest.skip("live signals table has no rows in the last 24h to check")

    import json as _json

    shaped = []
    for r in rows:
        sal = r["salience"]
        if isinstance(sal, str):
            sal = _json.loads(sal)
        shaped.append({
            "id": r["id"], "source_id": r["source_id"], "title": r["title"],
            "salience": sal, "produced_at": r["produced_at"].isoformat(),
        })

    old_select = _load_old_select_journal_slice(tmp_path)
    old_result = old_select(shaped)
    new_result = new_journal_slice._select_journal_slice(shaped)
    assert _ids(new_result) == _ids(old_result)
