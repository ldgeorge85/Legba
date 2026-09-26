# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A reference build counts WHAT ITS FETCHES BOUGHT, and prints it.

``pages: 0 read, 0 in window; 6 searches / 8 fetches`` was the whole receipt a
zero-yield build left behind, and it cannot distinguish the two failures that
take opposite remedies:

  * every host REFUSED this lane — a source problem; find other outlets;
  * every host TIMED OUT — a transport problem; the pages exist (dfat.gov.au
    and defence.gov.au were probed afterwards and served 200 from the same
    network minutes later, one of them dated inside the build's own window).

So the plane tallies every fetch by outcome — ``ok`` / ``stub`` / ``blocked`` /
``timed_out`` / ``refused``, summing to the fetch count — the loop carries the
tally out, and the receipt prints it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from legba.data.analysts.deterministic_handlers import reference_builder as RB
from legba.data.analysts.deterministic_handlers._reference_fences import (
    DomainBlocklist,
)
from legba.data.analysts.deterministic_handlers._reference_loop import ToolPlane
from legba.data.analysts.deterministic_handlers._reference_page import (
    ReferenceArchive,
)

from .test_reference_builder import _Outcome, _Result

OK_URL = "https://www.abc.net.au/news/2026-09-18/real-story"
HUNG_URL = "https://www.dfat.gov.au/geo/australia/hung-page"
STUB_URL = "https://www.example.org/paywalled"
DEAD_URL = "https://www.example.net/gone"

ARTICLE = "<html><body><p>" + ("A real sentence about the matter. " * 60) + "</p></body></html>"


class _OutcomeBinding:
    """A pack binding double whose ``web_fetch`` is scripted per URL."""

    def __init__(self, script) -> None:
        self.script = dict(script)
        self.calls: list[str] = []

    async def run_tool(self, name, args):
        url = str(args.get("url", ""))
        self.calls.append(url)
        step = self.script.get(url)
        if step == "hang":
            # What the PLANE's own backstop sees when the tool never answers.
            await asyncio.sleep(3600)
        if step == "tool_timed_out":
            return _Outcome(_Result(
                {"url": url, "fetch_outcome": "timed_out", "attempts": 2},
                status="failed",
                error="fetch_timed_out: ReadTimeout after 2 attempt(s) at 15s",
            ))
        if step == "dead":
            return _Outcome(_Result(
                {"url": url, "fetch_outcome": "error", "attempts": 1},
                status="failed", error="fetch_failed: connection reset",
            ))
        if step == "stub":
            return _Outcome(_Result({
                "url": url, "status_code": 200, "content_type": "text/html",
                "body": "<html><body>Subscribe to read</body></html>",
                "fetch_outcome": "ok", "attempts": 1,
            }))
        return _Outcome(_Result({
            "url": url, "status_code": 200, "content_type": "text/html",
            "body": ARTICLE, "fetch_outcome": "ok", "attempts": 1,
        }))


def _plane(script, **kw) -> ToolPlane:
    return ToolPlane(
        binding=_OutcomeBinding(script), archive=ReferenceArchive(None),
        blocklist=DomainBlocklist(), **kw,
    )


# ---------------------------------------------------------------------------
# 1) The four outcomes, told apart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_fetch_is_tallied_by_what_it_actually_bought():
    plane = _plane({
        OK_URL: "ok", HUNG_URL: "tool_timed_out",
        STUB_URL: "stub", DEAD_URL: "dead",
    })
    for url in (OK_URL, HUNG_URL, STUB_URL, DEAD_URL):
        await plane.fetch_page(url)

    assert dict(plane.fetch_outcomes) == {
        "ok": 1, "timed_out": 1, "stub": 1, "blocked": 1,
    }
    # The invariant that makes the tally readable next to "N fetches".
    assert sum(plane.fetch_outcomes.values()) == len(plane.fetches)


@pytest.mark.asyncio
async def test_a_timed_out_host_is_not_told_to_the_model_as_a_refusal():
    """A model told BLOCKED writes the host off for the rest of the run."""
    plane = _plane({HUNG_URL: "tool_timed_out"})
    payload = await plane.fetch_page(HUNG_URL)

    assert payload["outcome"] == "timed_out"
    assert "TIMED OUT" in payload["note"]
    assert "NOT a refusal" in payload["note"]
    assert "BLOCKED" not in payload["note"]


@pytest.mark.asyncio
async def test_the_planes_own_backstop_also_counts_as_timed_out():
    """The tool never answered at all — still a timeout, not a refusal."""
    plane = _plane({HUNG_URL: "hang"}, fetch_timeout=0.05)
    payload = await plane.fetch_page(HUNG_URL)

    assert payload["outcome"] == "timed_out"
    assert dict(plane.fetch_outcomes) == {"timed_out": 1}


@pytest.mark.asyncio
async def test_a_refusal_that_never_reached_the_network_is_counted_apart():
    """A blocklisted or composed URL costs no egress and is not a 'blocked'."""
    plane = _plane({OK_URL: "ok"})
    plane.offered = {"abc.net.au/news/2026-09-18/real-story": OK_URL}
    composed = "https://www.theguardian.com/invented/path"
    payload = await plane.fetch_page(composed)

    assert payload["status"] == "refused"
    assert dict(plane.fetch_outcomes) == {"refused": 1}
    assert plane.binding.calls == [], "a refusal spends no fetch"


@pytest.mark.asyncio
async def test_a_cached_page_still_counts_as_a_page_read():
    plane = _plane({OK_URL: "ok"})
    await plane.fetch_page(OK_URL)
    await plane.fetch_page(OK_URL)

    assert plane.binding.calls == [OK_URL], "the second read came from the archive"
    assert dict(plane.fetch_outcomes) == {"ok": 2}


# ---------------------------------------------------------------------------
# 2) The receipt prints it
# ---------------------------------------------------------------------------


def _pages_line(loop_rec: dict) -> str:
    """The receipt's own ``pages:`` line, rendered by the shipped builder."""
    payload = RB.build_receipt(
        t0=datetime(2026, 9, 20, tzinfo=timezone.utc),
        enabled=True, roster_size=1,
        due=[{"target_id": "AU", "eligible": True}],
        per_target=[{
            "target_id": "AU", "status": "no_commit",
            "no_commit_reason": "no development committed",
            "loop": loop_rec, "fences": {},
        }],
        warnings=[], cadence_days=7, window_days=14, dry_run=True,
    )
    lines = [ln for ln in payload.body.splitlines() if "pages:" in ln]
    assert len(lines) == 1, payload.body
    return lines[0]


def test_the_receipt_line_names_the_timed_out_fetches():
    """The operator-facing line. Without it, "0 pages read" has no cause."""
    line = _pages_line({
        "manifest_pages": 0, "manifest_in_window": 0,
        "searches": 6, "fetches": 8,
        "fetch_outcomes": {"timed_out": 5, "blocked": 2, "refused": 1},
    })
    assert "0 read, 0 in window" in line
    assert "6 searches / 8 fetches" in line
    assert "5 timed out" in line
    assert "2 blocked" in line
    assert "1 refused" in line


def test_a_clean_build_prints_no_outcome_parenthetical():
    """Nothing went wrong — say nothing. The line stays what it was."""
    line = _pages_line({
        "manifest_pages": 4, "manifest_in_window": 3,
        "searches": 3, "fetches": 4, "fetch_outcomes": {"ok": 4},
    })
    assert line.strip() == "pages: 4 read, 3 in window; 3 searches / 4 fetches"


def test_an_older_receipt_record_without_the_tally_still_renders():
    """Forward-compatible: a loop record from before this field is not a crash."""
    line = _pages_line({
        "manifest_pages": 1, "manifest_in_window": 1,
        "searches": 2, "fetches": 2,
    })
    assert "1 read, 1 in window" in line
