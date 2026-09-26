# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T1.0 (JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §6) — byte-identity proof
for the REFLECT claim machinery's extraction from ``journal_assessor.py`` to
``journal_reflect.py``.

Two layers:

  * Three committed SYNTHETIC fixture bodies (shaped like live journal entries
    — blank-line-separated spans, ``[[ref:<uuid>]]`` markers, ``[[spec]]`` /
    ``[[instrument]]`` markers — but not live text) drive every branch of
    ``_reflect_claims``: cited-fact binding + cross-span ref dedup, the
    perspective/speculation exemptions (explicit marker, wonder cue, no
    factual hint, hint-vs-cue tie-break), and the Review-A ``[[instrument]]``
    worldliness guard (both the honest self-read that stays exempt and the
    world-fact dodge that downgrades to an uncited, flagged fact claim).

  * A live check (skipped when the deployment's Postgres isn't reachable)
    pulls the 5 most recent real ``journal_entries.body`` rows and runs the
    SAME comparison over them.

Both compare the pre-extraction function — loaded via ``importlib`` from
``git show <OLD_SHA>:...journal_assessor.py`` written to a temp file (never
``git stash`` on a shared repo) with its ``__package__`` pinned to
``legba.data.analysts`` so its relative imports resolve against the real,
currently-checked-out siblings (``journal_leak_guards``, ``..provenance.models``,
etc. — all unchanged by this extraction) — against BOTH the new
``journal_reflect._reflect_claims`` and the ``journal_assessor`` re-export,
asserting the returned ``(claims, flat_refs, flags)`` tuples are identical.
``JournalClaim`` is a pydantic model (equality by field value) and the OLD
module's relative import resolves to the SAME live class object as the NEW
modules', so direct tuple equality is a real byte-identity check, not a
structural approximation.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from legba.data.analysts import journal_assessor as new_journal_assessor
from legba.data.analysts import journal_reflect as new_journal_reflect

REPO_ROOT = Path(__file__).resolve().parents[2]
# The worktree's pinned base commit — journal_assessor.py as it stood
# immediately BEFORE the T1.0 extraction landed.
OLD_SHA = "f5548afa025934f5f71a697162de339c9548daba"
OLD_REL_PATH = "src/legba/data/analysts/journal_assessor.py"


def _load_old_reflect_claims(tmp_path: Path):
    """Load ``journal_assessor.py`` exactly as it was at ``OLD_SHA`` via
    ``importlib`` from a temp file, and return its ``_reflect_claims``.

    ``git stash`` is forbidden on this shared repo — ``git show <sha>:<path>``
    reads the historical blob without touching the working tree. The loaded
    module's ``__package__`` is pinned to ``legba.data.analysts`` so its
    ``from .journal_leak_guards import ...`` / ``from ..provenance.models
    import ...`` / etc. relative imports resolve against the real package
    (unchanged siblings), not a floating standalone module.
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
    old_path = tmp_path / "journal_assessor_pre_t10.py"
    old_path.write_text(old_source, encoding="utf-8")

    mod_name = "legba.data.analysts._journal_assessor_pre_t10_test"
    spec = importlib.util.spec_from_file_location(mod_name, old_path)
    assert spec is not None and spec.loader is not None
    old_module = importlib.util.module_from_spec(spec)
    old_module.__package__ = "legba.data.analysts"
    sys.modules[mod_name] = old_module
    try:
        spec.loader.exec_module(old_module)
    finally:
        sys.modules.pop(mod_name, None)
    return old_module._reflect_claims


# ---------------------------------------------------------------------------
# Synthetic fixture bodies — shaped like live journal entries, not live text.
# ---------------------------------------------------------------------------

# Exercises: a single-ref cited claim, a two-ref claim that repeats an
# already-seen ref (per-claim refs keep the repeat; the flat cross-span
# dedup list does not), and a third, distinct ref in its own span.
FIXTURE_CITED_FACTS = (
    "Markets rallied after the central bank's surprise rate cut "
    "[[ref:11111111-1111-1111-1111-111111111111]].\n"
    "\n"
    "The rally extended into a second session, with trading volume nearly "
    "double the weekly average "
    "[[ref:11111111-1111-1111-1111-111111111111]], and analysts pointed to "
    "the same policy signal driving both moves "
    "[[ref:22222222-2222-2222-2222-222222222222]].\n"
    "\n"
    "A separate wave of buying hit emerging-market bonds on the same signal "
    "[[ref:33333333-3333-3333-3333-333333333333]]."
)

# Exercises every perspective/speculation exemption path: an explicit
# [[spec]] marker, a first-person wonder cue with no marker, a span with NO
# factual hint at all (exempt without any cue), and a span carrying BOTH a
# factual hint and a perspective cue (the voice-preservation tie-break).
FIXTURE_PERSPECTIVE_SPECULATION = (
    "[[spec]] The city might see fresh protests this weekend if the curfew "
    "is extended again.\n"
    "\n"
    "I wonder whether the ceasefire will hold through the week, given how "
    "thin the guarantees looked in the briefing.\n"
    "\n"
    "The city held its usual weekend calm, market stalls open, families "
    "out for their evening walk.\n"
    "\n"
    "It feels like inflation is cooling faster than the forecasts "
    "suggested, though I would not call it settled yet."
)

# Exercises the Review-A [[instrument]] guard both ways: an honest
# self-metric read (self-vocabulary only) that stays exempt, and a
# world-fact dodge (>=2 non-self capitalized words) that loses the
# exemption and downgrades to an uncited, [needs_citation]-flagged fact —
# plus a plain uncited factual span and a plain cited one for contrast.
FIXTURE_INSTRUMENT_GUARD = (
    "[[instrument]] The critic subsystem's calibration run stayed within "
    "its usual band this week, nothing to flag on our own dials.\n"
    "\n"
    "[[instrument]] Ukraine and Russia have escalated the conflict again "
    "this week.\n"
    "\n"
    "Oil prices rose sharply this week on the same supply news.\n"
    "\n"
    "Markets closed higher after the announcement "
    "[[ref:44444444-4444-4444-4444-444444444444]]."
)

FIXTURE_BODIES = (
    ("cited_facts", FIXTURE_CITED_FACTS),
    ("perspective_speculation", FIXTURE_PERSPECTIVE_SPECULATION),
    ("instrument_guard", FIXTURE_INSTRUMENT_GUARD),
)


@pytest.mark.parametrize(
    "body", [b for _, b in FIXTURE_BODIES], ids=[i for i, _ in FIXTURE_BODIES]
)
def test_reflect_claims_byte_identical_to_pre_extraction(body, tmp_path) -> None:
    old_reflect_claims = _load_old_reflect_claims(tmp_path)
    old_result = old_reflect_claims(body)

    new_result = new_journal_reflect._reflect_claims(body)
    assert new_result == old_result

    # The journal_assessor re-export must be the SAME function, not a copy
    # that could silently drift.
    assert new_journal_assessor._reflect_claims is new_journal_reflect._reflect_claims
    assessor_result = new_journal_assessor._reflect_claims(body)
    assert assessor_result == old_result


# ---------------------------------------------------------------------------
# Live check — the 5 most recent real journal_entries rows.
# ---------------------------------------------------------------------------

_LIVE_PG_HOST = os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1")
_LIVE_PG_PORT = int(os.environ.get("LEGBA_DATA_PG_PORT", "5432"))
_LIVE_PG_USER = os.environ.get("LEGBA_DATA_PG_USER", "legba")
_LIVE_PG_PASSWORD = os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba")
_LIVE_PG_DB = os.environ.get("LEGBA_DATA_PG_DB", "legba")


def _live_pg_port_open() -> bool:
    """Cheap, sync reachability probe for the skipif condition (collection
    time — no event loop available yet). The test itself still tolerates a
    connect/auth/table failure by skipping from inside the test body."""
    try:
        with socket.create_connection((_LIVE_PG_HOST, _LIVE_PG_PORT), timeout=1.5):
            return True
    except OSError:
        return False


@pytest.mark.skipif(
    not _live_pg_port_open(),
    reason=(
        f"live legba Postgres not reachable at {_LIVE_PG_HOST}:{_LIVE_PG_PORT} "
        "— this check only runs against a real deployment's journal_entries table"
    ),
)
async def test_reflect_claims_byte_identical_on_live_journal_entries(tmp_path) -> None:
    import asyncpg

    dsn = (
        f"postgresql://{_LIVE_PG_USER}:{_LIVE_PG_PASSWORD}@"
        f"{_LIVE_PG_HOST}:{_LIVE_PG_PORT}/{_LIVE_PG_DB}"
    )
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"could not connect to live journal_entries db: {exc!r}")
        return
    try:
        rows = await conn.fetch(
            "SELECT id, body FROM journal_entries "
            "ORDER BY produced_at DESC LIMIT 5"
        )
    finally:
        await conn.close()

    if not rows:
        pytest.skip("live journal_entries table has no rows to check")

    old_reflect_claims = _load_old_reflect_claims(tmp_path)
    mismatches = []
    for row in rows:
        body = row["body"] or ""
        old_result = old_reflect_claims(body)
        new_result = new_journal_reflect._reflect_claims(body)
        if new_result != old_result:
            mismatches.append(str(row["id"]))
    assert not mismatches, (
        "REFLECT extraction diverged on live journal_entries rows: "
        + ", ".join(mismatches)
    )
