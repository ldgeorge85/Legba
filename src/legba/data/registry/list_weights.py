# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry's list routes and their weights.

RULE. A route that returns a list answers a declared LIGHT projection by
default; the full record is read by id, or by asking for it by name
(``fields=full``). A route whose rows are narrow by construction (no document
column) is EXEMPT, with the reason written here. Every list route also carries
a PAGE BUDGET: the most bytes its default page (default limit, default weight)
may answer.

Two readers hold the table to the app:
  * ``tests/data_pkg/test_list_weights.py`` — every mounted GET that pages
    (takes ``limit`` or ``cursor``) is declared here, and every entry is light
    by default or exempt with a reason; the fixture byte-budget tests for the
    document-weight routes read ``page_budget`` from here.
  * ``scripts/report_list_weights.py`` — read-only, against a running
    registry: each route's default page, measured, against its budget.

Budgets were set from live default pages on 2026-11-14 (sizes in the
comments) with headroom, rounded. A route that outgrows
its budget fails the report; raising a number here is a visible act.

Registry-slim: stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass

KB = 1024


@dataclass(frozen=True)
class ListWeight:
    #: how a caller asks for the light weight (``"fields=summary"``), or None
    light: str | None
    #: the light weight is what an omitted parameter answers
    light_by_default: bool
    #: how a caller asks for the whole record (by id route / ``fields=full``)
    full: str | None
    #: why the route has no light projection (narrow rows by construction)
    exempt: str | None
    #: the most bytes the default page may answer
    page_budget: int


_NARROW = "narrow rows by construction (no document column in the page)"

_V1 = "/api/v1"

LIST_ROUTES: dict[str, ListWeight] = {
    # --- the document-weight lists: light by default (P-8) ---------------
    # live default page (50 rows): full 2,748 KB → summary 27 KB
    f"{_V1}/findings": ListWeight(
        "fields=summary", True, "GET /findings/{finding_id} or fields=full",
        None, 96 * KB),
    # live default page (25 rows): full 483 KB → summary 17 KB
    f"{_V1}/journal": ListWeight(
        "fields=summary", True, "GET /journal/{entry_id} or fields=full",
        None, 64 * KB),
    # live default page (50 rows, world_assessor): full 11,832 KB → light
    f"{_V1}/analysts/{{analyst_id}}/outputs": ListWeight(
        "fields=summary", True, "fields=full (a finding: GET /findings/{id})",
        None, 96 * KB),
    # Walk fix (live: /signals?limit=500 was 2.21 MB of the Watch's
    # 4.57 MB): `signal_list_weight.SignalSummaryRow`. Live default page
    # (50 rows): full 213 KB → summary ~20 KB (500 rows: 2,275 KB → 207 KB,
    # measured from a live page projected offline).
    f"{_V1}/signals": ListWeight(
        "fields=summary", True, "fields=full", None, 48 * KB),
    # --- documents, not flipped, with the reason --------------------------
    # 248 descriptors, 805 KB: the registry's machine CRUD surface (the
    # runtime, bring-up scripts and the console's descriptor editor read the
    # bodies), so the default stays the whole body. the
    # console's rosters ask `fields=summary` by name (`descriptor_list_weight`;
    # live: targets 279 KB → 73 KB, analysts 546 KB → 71 KB).
    f"{_V1}/registry/descriptors": ListWeight(
        "fields=summary", False, None,
        "descriptor bodies are the registry's machine API (runtime, bring-up, "
        "editor read them); the console's rosters ask fields=summary", 1_600 * KB),
    # The Watch page was 2.01 MB against a 2.00 budget; its largest read was
    # /v3/events?limit=500 at 419 KB, so: the map and the time band ask
    # `fields=summary` by name (`event_list_weight.EventSummaryRow`, 500 rows
    # 419 KB -> 239 KB live). The default stays the whole row: a subject's
    # timeline, a situation's tracked events and the export basket read it.
    f"{_V1}/v3/events": ListWeight(
        "fields=summary", False, "fields=full (the default) or GET /v3/events/{event_id}",
        "the default page (50 rows, ~42 KB) is the whole event, read by the "
        "timeline, situation and export readers; the map and the time band "
        "ask fields=summary", 128 * KB),
    # 179 sources, 378 KB: same surface, source descriptors.
    f"{_V1}/registry/sources": ListWeight(
        None, False, None,
        "source descriptor bodies are the registry's machine API; a light "
        "projection is a follow-up", 800 * KB),
    # 50 rows, 256 KB (at the line): the console's situation panels read
    # `data`; flagged for the next light projection.
    f"{_V1}/situations": ListWeight(
        None, False, None,
        "at the 250 KB line (256 KB live); the console reads situation data — "
        "the next light projection", 512 * KB),
    f"{_V1}/v3/situations": ListWeight(
        None, False, None,
        "at the 250 KB line (256 KB live); the console reads situation data — "
        "the next light projection", 512 * KB),
    # 298 rows, 575 KB unpaged: requirement documents, read whole by the
    # collection panel; not paged by default.
    f"{_V1}/v3/collection-requirements": ListWeight(
        None, False, None,
        "unpaged requirement documents read whole by the collection panel "
        "(575 KB live); a light projection is a follow-up", 1_200 * KB),
    # --- narrow lists: exempt ----------------------------------------------
    # RA-1: the reader's roster — the summary weight by construction (the
    # same cut as /registry/descriptors?fields=summary minus the identity
    # columns a reader does not get); 500 targets ≈ the 73 KB summary page.
    f"{_V1}/v3/roster": ListWeight(
        None, False, "GET /registry/descriptors/{family}/{id} (admin)",
        "summary rows by construction (reader_reads_api.RosterRow: no body "
        "beyond the roster keys)", 256 * KB),
    f"{_V1}/analysts/{{analyst_id}}/critiques": ListWeight(None, False, None, _NARROW, 128 * KB),
    f"{_V1}/analysts/{{analyst_id}}/runs": ListWeight(None, False, None, _NARROW, 64 * KB),
    f"{_V1}/budget/demotions": ListWeight(None, False, None, _NARROW, 96 * KB),
    # 1,000 narrow rows, 257 KB
    f"{_V1}/budget/ledger": ListWeight(None, False, None, _NARROW, 512 * KB),
    f"{_V1}/consult/sessions": ListWeight(None, False, None, _NARROW, 64 * KB),
    f"{_V1}/consult/writes": ListWeight(None, False, None, _NARROW, 128 * KB),
    f"{_V1}/contention": ListWeight(None, False, None, _NARROW, 320 * KB),
    f"{_V1}/entities": ListWeight(None, False, None, _NARROW, 128 * KB),
    f"{_V1}/entities/graph": ListWeight(None, False, None, _NARROW, 128 * KB),
    f"{_V1}/eval/labels": ListWeight(None, False, None, _NARROW, 96 * KB),
    f"{_V1}/graph/ego": ListWeight(None, False, None, _NARROW, 256 * KB),
    f"{_V1}/graph/structure": ListWeight(None, False, None, _NARROW, 64 * KB),
    f"{_V1}/journal_proposals": ListWeight(None, False, None, _NARROW, 96 * KB),
    f"{_V1}/registry/action_packs": ListWeight(None, False, None, _NARROW, 128 * KB),
    f"{_V1}/registry/audit": ListWeight(None, False, None, _NARROW, 128 * KB),
    f"{_V1}/registry/dead_letter": ListWeight(None, False, None, _NARROW, 64 * KB),
    f"{_V1}/registry/governor_events": ListWeight(None, False, None, _NARROW, 160 * KB),
    f"{_V1}/source_credibility": ListWeight(None, False, None, _NARROW, 96 * KB),
    f"{_V1}/units/{{target_id}}/correctness": ListWeight(None, False, None, _NARROW, 96 * KB),
    f"{_V1}/v3/belief": ListWeight(None, False, None, _NARROW, 256 * KB),
    f"{_V1}/v3/contentions": ListWeight(None, False, None, _NARROW, 384 * KB),
    f"{_V1}/v3/eval/scorecard": ListWeight(None, False, None, _NARROW, 384 * KB),
    f"{_V1}/v3/evidence/drift/assessments": ListWeight(None, False, None, _NARROW, 128 * KB),
    f"{_V1}/v3/evidence/drift/editors": ListWeight(None, False, None, _NARROW, 128 * KB),
    f"{_V1}/v3/evidence/drift/flags": ListWeight(None, False, None, _NARROW, 128 * KB),
    f"{_V1}/v3/graph/arcs": ListWeight(None, False, None, _NARROW, 256 * KB),
    f"{_V1}/v3/ledger/forecasts": ListWeight(None, False, None, _NARROW, 96 * KB),
    f"{_V1}/v3/map/at_sea": ListWeight(None, False, None, _NARROW, 64 * KB),
    f"{_V1}/v3/map/subject_reads": ListWeight(None, False, None, _NARROW, 128 * KB),
    f"{_V1}/v3/narratives": ListWeight(None, False, None, _NARROW, 160 * KB),
    f"{_V1}/v3/narratives/echo": ListWeight(None, False, None, _NARROW, 64 * KB),
    f"{_V1}/v3/since": ListWeight(None, False, None, _NARROW, 256 * KB),
    f"{_V1}/v3/situations/{{situation_id}}/trajectory": ListWeight(None, False, None, _NARROW, 128 * KB),
    f"{_V1}/v3/source-quality": ListWeight(None, False, None, _NARROW, 512 * KB),
    f"{_V1}/v3/system/escalations": ListWeight(None, False, None, _NARROW, 320 * KB),
    f"{_V1}/v3/system/production-gauge": ListWeight(None, False, None, _NARROW, 512 * KB),
    f"{_V1}/v3/system/receipt-anchors": ListWeight(None, False, None, _NARROW, 128 * KB),
    # More list routes, all narrow
    # (aggregates, counts, or capped records), budgets priced, not measured.
    f"{_V1}/v3/eval/runs": ListWeight(None, False, None, _NARROW, 128 * KB),  # E-3: aggregates only
    f"{_V1}/v3/ledger/resolution-queue": ListWeight(None, False, None, _NARROW, 128 * KB),  # F-1
    f"{_V1}/v3/ledger/resolution-records": ListWeight(None, False, None, _NARROW, 256 * KB),  # U-11: evidence capped at 40
    f"{_V1}/v3/since/counts": ListWeight(None, False, None, _NARROW, 8 * KB),  # S-1: counts only
    # One narrow row per origin (about one
    # per source descriptor, ~180 live), budget priced, not measured.
    f"{_V1}/registry/origins": ListWeight(None, False, None, _NARROW, 256 * KB),
    # The held family, light by default (240
    # characters of text per row); 50 rows priced at ~25 KB, budget 64 KB
    # (tests/data_pkg/test_held_record_db.py measures a page of long texts).
    f"{_V1}/v3/record/assertions": ListWeight(
        "fields=summary", True, "fields=full or GET /v3/record/assertions/{assertion_id}", None, 64 * KB),
}


def undeclared_problems() -> list[str]:
    """Entries that break the rule (neither light by default nor exempt)."""
    out = []
    for path, w in LIST_ROUTES.items():
        if w.light_by_default and not w.light:
            out.append(f"{path}: light_by_default without a light projection")
        if not w.light_by_default and not w.exempt:
            out.append(f"{path}: not light by default and no exempt reason")
        if w.page_budget <= 0:
            out.append(f"{path}: no page budget")
    return out
