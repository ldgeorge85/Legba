# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The V-B absence screen reads the RENDER title, not the raw one (#58, 2026-08-25).

THE DEFECT. ``load_absence_slice_rows`` projected bare ``payload->>'title'``
for the signal leg, while the renderer (``inline_target._signal_title``, T-1b
/ M13) prefers the stored English translation ``payload->>'title_en'`` first,
falling back to ``title`` only when no translation was stamped. On a
translated (non-Latin) source the two disagreed: the V-B screen — and the
stage-2 slice judge, which is SHOWN this same text — read the raw
transliterated / native-script title while the desk the analyst actually
worked from read English. A claim's content terms (English nouns, screened by
``_absence_content_terms``) can never collide with a native-script title, so
the screen silently lost every translated-source violator, the same shape the
2026-08-21 SALIENCE-rider found on the BODY surface.

THE FIX mirrors that rider exactly: the signal leg's title projection becomes
``COALESCE(NULLIF(payload->>'title_en', ''), payload->>'title', '')`` — same
COALESCE/NULLIF idiom, same precedence source (the renderer), same proof
shape. The composed-row (``analyst_outputs``) leg is untouched: composed prose
is always English, so it carries no ``title_en`` surface to prefer.

WHAT IS AND IS NOT COVERED, stated for the same reason the body-precedence
test (``test_verify_absence_slice_body.py``) states it: a fake connection
cannot run SQL. The PROJECTION — the actual site of the defect — is pinned by
asserting the emitted query text: it must project ``payload->>'title_en'``
and prefer it (positionally, inside the same COALESCE) over the bare
``payload->>'title'``. That is a weaker guard than execution and is labelled
as such; it is exactly strong enough to catch the regression it exists to
catch — this test FAILS against the pre-fix projection (which never mentions
``title_en`` at all) and PASSES once the precedence is restored.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from legba.data.provenance.absence_slice import load_absence_slice_rows


class _FakeConn:
    """asyncpg-shaped double. Returns the projection's OWN column names, so a
    row here is the shape the real query yields — the double cannot silently
    disagree with the SQL about what a row looks like. Mirrors
    ``test_verify_absence_slice_body.py``'s double."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.sql: str = ""

    async def fetchrow(self, sql: str, *args):
        if "analyst_traces" in sql:
            return {"input_row_refs": [uuid4() for _ in self._rows]}
        return None

    async def fetch(self, sql: str, *args):
        self.sql = sql
        return self._rows


def _signal(title: str, body: str = "") -> dict[str, Any]:
    return {
        "title": title,
        "body": body,
        "source_id": "src.wire",
        "provenance_kind": "",
        "row_kind": "signal",
    }


# ---------------------------------------------------------------------------
# THE SQL PROJECTION (pinned by text — see the module docstring)
# ---------------------------------------------------------------------------


async def test_sql_projects_title_en_for_the_signal_leg() -> None:
    """THE DEFECT, pinned. The signal leg must project ``payload->>'title_en'``
    at all — the pre-fix query never mentions it."""
    conn = _FakeConn([_signal("t")])
    await load_absence_slice_rows(conn, uuid4())
    sql = " ".join(conn.sql.split())
    assert "payload->>'title_en'" in sql, (
        "the signal leg lost the render title precedence — screen and desk "
        "disagree on a translated source's title again"
    )
    # NULLIF-guarded so an EMPTY title_en (not merely a missing one) falls
    # through to the raw title, mirroring the body COALESCE's own NULLIF
    # guards a few lines below it in the same query.
    assert "NULLIF(payload->>'title_en', '')" in sql


async def test_sql_prefers_title_en_over_the_bare_title() -> None:
    """Precedence, not merely presence: ``title_en`` must be tried BEFORE the
    bare ``title`` inside the same COALESCE, matching
    ``inline_target._signal_title``'s ``title_en or title`` order. A query
    that projects both columns but reads the raw title first would still
    render the wrong surface on a translated source."""
    conn = _FakeConn([_signal("t")])
    await load_absence_slice_rows(conn, uuid4())
    sql = " ".join(conn.sql.split())
    title_en_pos = sql.index("payload->>'title_en'")
    title_pos = sql.index("payload->>'title'")
    assert title_en_pos < title_pos, "title_en must be preferred, not just projected"


async def test_composed_row_leg_carries_no_title_en_surface() -> None:
    """W1(b)'s composed-row (``analyst_outputs``) leg is untouched: composed
    prose is always English, so it has no ``title_en`` column to prefer."""
    conn = _FakeConn([_signal("t")])
    await load_absence_slice_rows(conn, uuid4())
    sql = " ".join(conn.sql.split())
    assert "SELECT COALESCE(title, '') AS title" in sql


# ---------------------------------------------------------------------------
# THE SCREEN SURFACE — the Python-side plumbing carries whatever title the
# projection resolves, byte-identical either way (no logic to regress here;
# the precedence decision itself lives entirely in the SQL pinned above).
# ---------------------------------------------------------------------------


async def test_resolved_title_still_reaches_the_slice_row_text() -> None:
    """A signal whose projected title is already the ENGLISH one (what the
    fixed SQL now resolves to on a translated source) screens and shows
    exactly that text — the plumbing downstream of the projection is
    unaffected by the fix."""
    conn = _FakeConn([_signal("Officials meet in Ankara to discuss tariffs")])
    rows = await load_absence_slice_rows(conn, uuid4())
    assert rows is not None and len(rows) == 1
    assert "Officials meet in Ankara to discuss tariffs" in rows[0].text
