"""COMPOSITION SLICE ASSEMBLY — how a composition's inputs are gathered, per mode.

Extracted from ``meta_findings_synthesizer`` (D-2, 2026-09-04) — the seam
``tests/test_module_size_gate.py`` has named as "next in this file" since the
FRAME-2 train: the region / world / thematic SLICE-ASSEMBLY branches, their
roster + membership resolvers, and the per-mode coverage vocabulary they stamp.
One cohesive unit — "which heads does this composition mode read, and what does
it say about the ones it could not find" — with no prompt text, no LLM and no
output shaping in it.

The synthesizer imports this module ONE WAY and re-exports every moved name, so
``synth.REGION_MODE_GAP`` / ``synth._assemble_world_region_slice`` and every test
that reaches for them resolve unchanged.

THE ONE INVERTED DEPENDENCY, and how it is paid. The two assemblers need the
BASIS gather (``read_other_analyst_findings``), which lives with the rest of this
kind's DB reads in the synthesizer. It arrives as an injected ``basis_reader``
callable — the same shape, and for the same reason, as
``composition_window.read_floor_fallback_heads(basis_reader=...)``: the gather is
a parameter of the assembly, not a fact about it. ``READ_SLICE`` passes it
explicitly on the production path. The ``None`` default resolves it through a
DEFERRED import so a direct caller (every ``_assemble_*`` test) keeps working
byte-for-byte; the import is inside the function, so nothing here runs at module
import time and there is no cycle.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

#: Injected BASIS gather: ``(conn, **kwargs) -> list[dict]``. See the module
#: docstring — the reader is a parameter of the assembly, not a fact about it.
BasisReader = Callable[..., Awaitable[list[dict[str, Any]]]]


def _default_basis_reader() -> BasisReader:
    """The production basis gather, resolved lazily.

    DEFERRED on purpose: ``meta_findings_synthesizer`` imports THIS module at its
    own import time, so a module-level import back would be a cycle. Inside a
    function body it is a plain attribute lookup on an already-loaded module.
    """
    from .meta_findings_synthesizer import read_other_analyst_findings

    return read_other_analyst_findings


# S2-T2 REGION composition — the region-frame target-id prefix.
#
# A region composition run (analyst_region_composition.yaml) fans out one worker
# per REGION FRAME (a target tagged ``region``); the fan-out stamps the frame's
# target id into ``target_filter`` / ``options["target_id"]``, and every region
# frame id is ``region_<slug>`` (e.g. ``region_mena``). This prefix is the SOLE
# discriminator that tells a region run apart from a per-COUNTRY one (both carry
# a truthy ``target_id``): a region ``target_id`` is a FRAME with no
# country_composition findings of its OWN, so it must NOT scope
# ``f.target_id = 'region_mena'`` (matches nothing) — it resolves to its MEMBER
# country desks and reads THEIR country_composition heads (a multi-country read,
# world-shaped). Per-country / world / legacy paths never see this prefix.
REGION_TARGET_PREFIX: str = "region_"


def _is_region_target(target_filter: Any) -> bool:
    """True iff ``target_filter`` names a REGION FRAME (``region_<slug>``).

    The discriminator for the S2-T2 region mode. ``None`` / a country id
    (``country_g20_in``) / any non-region string returns ``False`` — so the
    per-country, world, and legacy READ_SLICE branches are left untouched.
    """
    return bool(target_filter) and str(target_filter).startswith(REGION_TARGET_PREFIX)


# S2-T3 — the WORLD compose over REGIONS (the 5th tower floor's crown).
#
#     unit sub-claim → country read → region read → WORLD read
#
# The target-less world_assessor now composes the FIVE region_composition HEADS
# (5-6 inputs) instead of the ~24 country_composition heads — structurally
# removing the MAX_WORLD_INPUT_FINDINGS cap pressure (the P4-C2 "Global without
# the United States" failure class). DEGRADE-NOT-DROP + absence-honest:
#
#   * a region WITH a region_composition head this window feeds that head
#     (mode ``region``);
#   * a region with NO region head DEGRADES to that region's member
#     country_composition heads (mode ``country_fallback``) — the same set the
#     region compose itself would fuse, never silently dropped;
#   * a region with NEITHER is a GAP (mode ``gap``, 0 inputs) — NAMED as an
#     unassessed region in the world prose (via the appended REGION COVERAGE
#     block), never silently missing.
#
# The per-region MODE that ran is stamped in ``data.region_coverage`` so the
# provenance is honest about which floor grounded each region.
REGION_FRAME_TAG: str = "region"
"""The generic frame tag every region frame carries (S2-T1). The roster read
keys on it (``(body->'scope'->'tags') ? 'region'``) — the member country desks
carry the SPECIFIC ``region_<slug>`` tag, NOT this one, so it matches ONLY the
five frames."""

REGION_COMPOSITION_ANALYST_ID: str = "region_composition"
"""The per-region composition analyst id (S2-T2). The world read's declared
``other_analysts`` source; the region layer the world composes over."""

COUNTRY_COMPOSITION_ANALYST_ID: str = "country_composition"
"""The per-country composition analyst id (P3-T1). The world DEGRADE target: a
region with no region head falls back to reading THIS analyst's member-country
heads (the same source the region compose fuses)."""

# Per-region coverage MODE tokens stamped into ``data.region_coverage[].mode``.
REGION_MODE_REGION: str = "region"
"""A region_composition head grounded this region (the intended top floor)."""

REGION_MODE_COUNTRY_FALLBACK: str = "country_fallback"
"""No region head this window → degraded to the region's country reads."""

REGION_MODE_GAP: str = "gap"
"""No region read AND no country reads → an honest, NAMED gap."""

REGION_MODE_THEMATIC: str = "thematic"
"""B0-4 (MASTER_PLAN 2026-07-10): a target-LESS cross-region thematic head
(e.g. escalation_composition) admitted into the world slice as a labeled
cross-region block — the world's one LEGAL way to carry a claim spanning
regions. Verify-floored like every other input; a weak thematic head is
floored out, never injected."""

REGION_MODE_COUNTRIES: str = "countries"
"""D-5 (§4.2) — the ASSEMBLY-REGIME world mode, and the retirement of a lie.

Under the assembly regime the world read grounds a region DIRECTLY on its
member country assemblies. There is no region head in the world's input any
more, so neither of the two modes above describes what happened:
:data:`REGION_MODE_REGION` would name an input that no longer exists, and
:data:`REGION_MODE_COUNTRY_FALLBACK` would call the PRIMARY path a fallback —
a word that told the reader something had gone wrong when nothing had.

The old vocabulary is not deleted: a legacy-regime world run still emits it,
byte-for-byte. This token is what the assembly-regime run emits instead, and it
means exactly one thing: *N member-country assemblies grounded this region, and
no region tier was consulted.*"""

REGION_MODE_UNASSIGNED: str = "unassigned"
"""D-5 — country assemblies belonging to NO region frame.

The old world path read region heads, so a desk carrying no ``region_<slug>``
tag was invisible to it — not dropped, never seen. Reading the country roster
directly makes those desks candidates for the first time, and an unnamed
bucket would put them in the read while leaving the coverage list silently
short. They get their own coverage entry instead: present, counted, and
labelled as sitting outside the region frame — which is also the cheapest
possible detector for a desk somebody forgot to tag."""

UNASSIGNED_REGION_ID: str = "unassigned"
UNASSIGNED_REGION_NAME: str = "Desks outside any region frame"

REGION_MODE_THEMATIC_GAP: str = "thematic_gap"
"""H-3c (MASTER_PLAN 2026-07-10 F/H/S, audit W6): a DECLARED thematic analyst
(in the world's ``other_analysts`` roster, e.g. escalation_composition) that
produced ZERO admitted rows this cycle — its head was floored out on
faithfulness (a weak synthesis, correctly withheld) or is simply absent. Before
H-3c this vanished SILENTLY (no coverage entry), so the world composed as if the
thematic lane had never been wired. Now it is a NAMED gap — the same
absence-honesty idiom as :data:`REGION_MODE_GAP` — so the world prose
acknowledges the floored lane instead of implying full thematic coverage."""


# S2-T4 THEMATIC composition — fuse ONE unit dimension across ALL desks.
#
# A THEMATIC composition (escalation_composition) reads the latest verified head
# of ONE UNIT analyst dimension (analyst_id='escalation') for EVERY g20+watch
# desk and fuses them into ONE global read — the ANALYST axis, not the TARGET
# axis the country/region/world compositions fuse along. It is the SAME
# meta_findings_synthesizer kind; the thematic behavior is descriptor + this
# READ_SLICE branch.
THEMATIC_DIMENSION_KEY: str = "thematic_dimension"
"""The ``subscription.substrate`` marker key naming the UNIT analyst_id dimension
a THEMATIC composition fuses across ALL desks (e.g. ``'escalation'``). Its
PRESENCE is the SOLE discriminator that routes a target-less, verify-declaring run
to the thematic branch INSTEAD of the world-over-regions branch (both are
target-less + verify-declaring). Lives in the open ``subscription.substrate`` dict
(``dict[str, Any]``) so no schema change / registry rebuild is needed to add it."""


THEMATIC_DESKS_KEY: str = "thematic_desks"
"""The optional ``subscription.substrate`` marker (S2-T5) restricting a THEMATIC
composition to an ALLOW-LIST of desk ids instead of every g20+watch desk — the
IR-IL escalation DYAD sets ``['country_watch_ir','country_watch_il']``. Absent /
empty ⇒ the thematic read spans ALL desks (escalation_composition is byte-for-byte
unchanged). Only meaningful alongside ``THEMATIC_DIMENSION_KEY``. Lives in the open
``subscription.substrate`` dict so no schema change is needed."""

# The ASSESSED-desk coverage roster: one row per active desk a bounded unit fans
# out to. The thematic compose diffs this roster against the desks that HAVE a
# head this window to NAME any desk with no head as an honest gap
# (degrade-not-drop).
#
# `g20` + `watch` = the subscription key for the seven BROAD geopolitics units
# (has_tag('g20') or has_tag('watch')).
#
# `supply_chain` = the subscription key for the `disruption_status` unit — the
# supply-chain pack's `lane_*` / `flow_*` desks (2026-07-29,
# planning/SUPPLY_CHAIN_PACK_PLAN_2026-07-29.md §3.5). Those desks deliberately
# carry NEITHER g20 nor watch (tagging a lane `watch` would fan all seven country
# units onto non-country desks), so without this literal a supply-chain desk that
# produced no head would be SILENTLY MISSING from ``data.desk_coverage`` instead
# of NAMED as a gap. Nothing crashes and nothing is fabricated — but silent
# coverage is the failure mode this platform exists to refuse.
#
# DELIBERATELY NOT WIDENED IN LOCKSTEP (plan §3.5 — this is a decision, not an
# oversight): ``scorecard_producer._G20_TARGETS_SQL`` (supply-chain desks get NO
# scorecard — a card with 7 country dimensions reading `insufficient-evidence`
# plus one supply-chain dimension would misrepresent what the pack measures),
# ``alert_trigger_scan._DESKS_SQL`` (no `baseline_deviation` alerts for these
# desks) and ``desk_baseline._DESKS_SQL`` (no desk baselines). Those three keep
# the bare ``array['g20', 'watch']`` predicate. This roster is the ONLY one that
# must see a supply-chain desk, because it is the only one whose job is naming
# ABSENCE.
_DESK_ROSTER_SQL = """
    SELECT descriptor_id, name
      FROM target_descriptors
     WHERE is_head = TRUE
       AND COALESCE(state, 'active') <> 'retired'
       AND (body -> 'scope' -> 'tags') ?| array['g20', 'watch', 'supply_chain']
     ORDER BY descriptor_id
"""

# Per-desk coverage MODE tokens stamped into ``data.desk_coverage[].mode``.
THEMATIC_MODE_PRESENT: str = "present"
"""A verified escalation head grounded this desk this window."""

THEMATIC_MODE_GAP: str = "gap"
"""No escalation head for this desk this window → an honest, NAMED gap."""


# S2-T2 REGION composition — resolve a region frame → its member country desks.
_REGION_MEMBERS_SQL = """
    SELECT descriptor_id
      FROM target_descriptors
     WHERE is_head = TRUE
       AND state = 'active'
       AND descriptor_id <> $1
       AND (body -> 'scope' -> 'tags') ? $1
     ORDER BY descriptor_id
"""


async def _resolve_region_member_target_ids(conn, region_id: str) -> list[str]:
    """Resolve a REGION FRAME's member COUNTRY desks (S2-T2).

    The member desks are the active head targets whose ``scope.tags`` carry the
    region's slug tag — which is the SAME ``region_<slug>`` string that IS the
    region frame's own target id (``region_id``). So a region and its members
    share one tag: the frame is ``region_mena`` and each MENA desk (Saudi Arabia,
    Turkey, Israel, Iran, …) is tagged ``region_mena``. The frame itself is
    EXCLUDED (``descriptor_id <> region_id``) — it has no country_composition
    finding of its own; only its member desks do. Mirrors the tag-membership
    idiom the scorecard producer uses for the g20/watch roster
    (``(body -> 'scope' -> 'tags') ?| array[...]``); ``body`` is JSONB so the
    ``?`` element-test needs no cast. An EMPTY result (a region with no tagged
    member desks) is a HONEST gap — the caller's SET filter then reads zero
    country reads and the synth narrates the region as unassessed.
    """
    rows = await conn.fetch(_REGION_MEMBERS_SQL, str(region_id))
    return [str(r["descriptor_id"]) for r in rows]


# S2-T3 WORLD compose over REGIONS — resolve the region-frame ROSTER (the five
# S2-T1 frames). Keyed on the generic ``region`` frame tag — the member country
# desks carry the SPECIFIC ``region_<slug>`` tag, NOT this one, so this matches
# ONLY the frames. Ordered by id for a stable, deterministic world coverage list.
_REGION_ROSTER_SQL = """
    SELECT descriptor_id, name
      FROM target_descriptors
     WHERE is_head = TRUE
       AND state = 'active'
       AND (body -> 'scope' -> 'tags') ? $1
     ORDER BY descriptor_id
"""


async def _resolve_region_roster(conn) -> list[dict[str, str]]:
    """Resolve the active REGION-FRAME roster (S2-T3).

    Returns ``[{"region_id", "region_name"}, ...]`` for every active head target
    tagged ``region`` (the five S2-T1 frames). The world compose diffs this
    authoritative region set against the region heads actually present to decide
    which regions DEGRADE to their country reads and which are HONEST gaps. An
    empty roster (a pre-S2-T1 topology with no region frames) tells the caller to
    fall back to a plain region-head read (no gap/degrade frame to reason over).
    """
    rows = await conn.fetch(_REGION_ROSTER_SQL, REGION_FRAME_TAG)
    roster: list[dict[str, str]] = []
    for r in rows:
        rid = str(r["descriptor_id"])
        name = r["name"]
        roster.append({"region_id": rid, "region_name": str(name) if name else rid})
    return roster


async def _assemble_world_region_slice(
    conn,
    *,
    region_analyst_ids: Sequence[str],
    time_window_hours: int,
    limit: int,
    verify_floor: float | None,
    basis_reader: BasisReader | None = None,
) -> list[dict[str, Any]]:
    """S2-T3 — assemble the world compose slice over REGIONS with per-region
    DEGRADE-NOT-DROP + absence-honest gaps.

    The world read composes the region_composition HEADS (5-6 inputs) instead of
    the ~24 country heads. For each region in the roster:

      * a present region head feeds the world directly (mode ``region``);
      * a region with NO head DEGRADES to its member country_composition heads
        (mode ``country_fallback``) — the same set the region compose would fuse;
      * a region with neither is a GAP (mode ``gap``, 0 inputs);
      * B0-4: a target-LESS head from a cross-region THEMATIC analyst in the
        roster (e.g. escalation_composition) is admitted as a labeled block
        (mode ``thematic``) — the world's one legal cited cross-region object.

    Every returned row is stamped with ``_region_id`` + ``_region_mode`` and — so
    the target-LESS world ``_run`` (which has NO DB access) can stamp the per-region
    MODE into ``data`` and NAME any gap in the prose — the full per-region coverage
    list is denormalized onto EVERY returned row as ``_region_coverage``. These
    synthetic ``_``-prefixed keys are ephemeral input-row annotations: the
    orient/render/cite paths read only their own known keys, and the persisted
    finding is built fresh in ``_coerce_finding`` (never from these rows).

    A read that surfaces ZERO rows (all regions gap → a total lower-floor outage)
    returns ``[]``; the actor then NOOPs the world run (no finding written) — the
    same empty-slice contract every meta read already honors.
    """
    _read_basis = basis_reader or _default_basis_reader()
    roster = await _resolve_region_roster(conn)

    # The region_composition heads — one HEAD per region via DISTINCT ON, verify-
    # floored + meta-inclusive (region_composition rows are meta=True). This is
    # the intended TOP-floor source; the country fallback below only fills gaps.
    region_rows = await _read_basis(
        conn,
        analyst_ids=list(region_analyst_ids),
        time_window_hours=time_window_hours,
        limit=limit,
        target_id=None,
        verify_floor=verify_floor,
        include_meta=True,
    )
    heads_by_region: dict[str, list[dict[str, Any]]] = {}
    # B0-4 — target-LESS heads from cross-region THEMATIC analysts in the
    # other_analysts roster (e.g. escalation_composition) are NOT dropped:
    # they become a labeled thematic block, the world's one legal cited
    # cross-region object. (Before B0-4 the `_is_region_target` filter
    # silently discarded them after the verify-floored fetch.)
    thematic_by_analyst: dict[str, list[dict[str, Any]]] = {}
    for r in region_rows:
        tid = str(r.get("target_id") or "")
        if not _is_region_target(tid):
            aid = str(r.get("analyst_id") or "thematic")
            r["_region_id"] = f"thematic:{aid}"
            r["_region_mode"] = REGION_MODE_THEMATIC
            thematic_by_analyst.setdefault(aid, []).append(r)
            continue
        r["_region_id"] = tid
        r["_region_mode"] = REGION_MODE_REGION
        heads_by_region.setdefault(tid, []).append(r)

    # No region roster (pre-S2-T1 topology) → no frame to diff gaps/degrade over;
    # feed whatever region heads exist. Coverage is simply absent (the world run
    # behaves like a plain region-head read).
    if not roster:
        return region_rows

    combined: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for region in roster:
        rid = region["region_id"]
        rname = region["region_name"]
        heads = heads_by_region.get(rid)
        if heads:
            combined.extend(heads)
            coverage.append(
                {
                    "region_id": rid,
                    "region_name": rname,
                    "mode": REGION_MODE_REGION,
                    "input_count": len(heads),
                }
            )
            continue
        # DEGRADE — no region head this window → read the region's member-country
        # country_composition heads (target-id SET), verify-floored + meta-inclusive.
        member_ids = await _resolve_region_member_target_ids(conn, rid)
        country_rows = (
            await _read_basis(
                conn,
                analyst_ids=[COUNTRY_COMPOSITION_ANALYST_ID],
                time_window_hours=time_window_hours,
                limit=limit,
                target_ids=member_ids,
                verify_floor=verify_floor,
                include_meta=True,
            )
            if member_ids
            else []
        )
        for cr in country_rows:
            cr["_region_id"] = rid
            cr["_region_mode"] = REGION_MODE_COUNTRY_FALLBACK
        if country_rows:
            combined.extend(country_rows)
            coverage.append(
                {
                    "region_id": rid,
                    "region_name": rname,
                    "mode": REGION_MODE_COUNTRY_FALLBACK,
                    "input_count": len(country_rows),
                }
            )
        else:
            # No region read AND no country reads → an HONEST, NAMED gap.
            coverage.append(
                {
                    "region_id": rid,
                    "region_name": rname,
                    "mode": REGION_MODE_GAP,
                    "input_count": 0,
                }
            )

    # Pass through any region head whose frame is NOT in the roster (a stale /
    # deregistered frame that still has a fresh head) — honest data still feeds
    # the world, though the roster is the authoritative set for the coverage list.
    roster_ids = {region["region_id"] for region in roster}
    for tid, heads in heads_by_region.items():
        if tid not in roster_ids:
            combined.extend(heads)

    # B0-4 — admit the cross-region THEMATIC heads (already verify-floored by
    # the fetch) as labeled blocks + coverage entries. This is the tower top's
    # one LEGAL cited cross-region object (e.g. escalation_composition): before
    # this, a genuine world-level claim spanning regions had no input it could
    # cite, so the world read was structurally anti-synthetic (review W1-W3).
    for aid, rows in sorted(thematic_by_analyst.items()):
        combined.extend(rows)
        coverage.append(
            {
                "region_id": f"thematic:{aid}",
                "region_name": f"{aid} (cross-region thematic)",
                "mode": REGION_MODE_THEMATIC,
                "input_count": len(rows),
            }
        )

    # H-3c (MASTER_PLAN F/H/S, audit W6) — a DECLARED thematic analyst that
    # produced ZERO admitted rows this cycle was floored out (weak faith) or is
    # absent. It is NOT in ``thematic_by_analyst`` (no rows survived the fetch),
    # so before H-3c it left NO trace and the world composed as if its lane
    # (e.g. escalation_composition) had never been wired. Emit an HONEST, NAMED
    # thematic gap — the same absence-honesty idiom as the region GAP above — so
    # the aperture block names the floored lane instead of implying coverage.
    # The region + country composition ids are the frame producers, not thematic
    # lanes, so they are never counted as gaps here.
    declared_thematic = [
        aid for aid in region_analyst_ids
        if aid not in (REGION_COMPOSITION_ANALYST_ID, COUNTRY_COMPOSITION_ANALYST_ID)
    ]
    for aid in sorted(set(declared_thematic)):
        if aid not in thematic_by_analyst:
            coverage.append(
                {
                    "region_id": f"thematic:{aid}",
                    "region_name": f"{aid} (cross-region thematic)",
                    "mode": REGION_MODE_THEMATIC_GAP,
                    "input_count": 0,
                }
            )

    # Denormalize coverage onto every row so the DB-less world ``_run`` can read it.
    for row in combined:
        row["_region_coverage"] = coverage
    return combined


# ---------------------------------------------------------------------------
# THE COVERAGE PROSE — what the per-mode vocabulary above SAYS (D-5 seam)
# ---------------------------------------------------------------------------
#
# Moved from ``meta_findings_synthesizer`` by D-5, to pay for the cascade's own
# lines under the module-size ratchet. It belongs here on the merits, not only
# on the arithmetic: every one of these three functions is a `str(c.get("mode"))
# == <TOKEN>` filter over the coverage lists this module BUILDS, using the mode
# tokens this module DEFINES. They were the only readers of that vocabulary
# living somewhere else.
#
# No LLM, no DB, no output shaping — pure render, same as the rest of this file.
# The synthesizer imports them ONE WAY and re-exports them, so
# ``synth._render_region_coverage_block`` still resolves.


def _render_region_coverage_block(coverage: Sequence[Mapping[str, Any]]) -> str:
    """Render the appended REGION COVERAGE block for the world compose (S2-T3).

    ONLY the GAP regions (mode ``gap`` — no region read AND no country reads) are
    listed, so the world model NAMES each as an unassessed region (absence-honest)
    instead of silently omitting it. Regions grounded by a region read or a
    country-fallback need no prose nudge — their reads appear as cited blocks (the
    MODE is still stamped into ``data.region_coverage``). Empty / no-gap coverage
    → ``""`` (the block is absent; the world prompt's region-gap rule is inert).
    """
    gaps = [c for c in coverage if str(c.get("mode")) == REGION_MODE_GAP]
    if not gaps:
        return ""
    lines = [
        "",
        "REGION COVERAGE (absence-honest — these world regions have NO read this "
        "cycle: neither a region composition nor any member-country read. NAME "
        "each as an unassessed gap; do NOT infer or invent its state):",
    ]
    for g in gaps:
        name = str(g.get("region_name") or g.get("region_id") or "(unknown region)")
        rid = str(g.get("region_id") or "")
        lines.append(f"- {name} ({rid}): no current read.")
    return "\n".join(lines)


def _render_world_aperture_block(coverage: Sequence[Mapping[str, Any]]) -> str:
    """B0-10 — render the ALWAYS-ON aperture disclosure for the world compose.

    The world view is composed from the platform's REGISTERED desk roster — a
    bounded, operator-chosen sample (G20 + watch-tier states + any thematic
    blocks) — not global coverage. Faithfulness verify is structurally silent
    about what was never collected, so the sample bounds must be STATED in the
    product, not implied. Unlike :func:`_render_region_coverage_block` (gaps
    only), this renders whenever coverage exists: sample honesty is not an
    exception path.
    """
    if not coverage:
        return ""
    # D-5: ``countries`` joins the GROUNDED set. It is the assembly regime's
    # primary mode (the world grounded this region on its member country
    # assemblies), so omitting it would under-count grounded regions and make
    # the aperture line understate the read — an absence claim that is false in
    # the safe direction is still false. ``unassigned`` deliberately stays OUT:
    # those desks are present but belong to no region, and counting them as a
    # grounded REGION would invent a frame that does not exist.
    regions = [
        c for c in coverage
        if str(c.get("mode")) in (
            REGION_MODE_REGION, REGION_MODE_COUNTRY_FALLBACK, REGION_MODE_COUNTRIES
        )
    ]
    unassigned = [
        c for c in coverage if str(c.get("mode")) == REGION_MODE_UNASSIGNED
    ]
    gaps = [c for c in coverage if str(c.get("mode")) == REGION_MODE_GAP]
    thematic = [c for c in coverage if str(c.get("mode")) == REGION_MODE_THEMATIC]
    thematic_gaps = [
        c for c in coverage if str(c.get("mode")) == REGION_MODE_THEMATIC_GAP
    ]
    lines = [
        "",
        "APERTURE (sample honesty — state this in the BLUF, do not imply "
        "global coverage):",
        f"- This view composes {len(regions)} grounded region read(s)"
        + (f" + {len(thematic)} cross-region thematic block(s)" if thematic else "")
        + (f", with {len(gaps)} named gap(s)" if gaps else "")
        + (
            f" and {len(thematic_gaps)} floored/absent thematic lane(s)"
            if thematic_gaps
            else ""
        )
        + ".",
        "- The underlying sample is the platform's registered desk roster — a "
        "bounded, operator-chosen set (G20 + watch-tier states), NOT global "
        "coverage. Regions, crises, and states outside the roster are simply "
        "not assessed here; say so rather than generalizing.",
        "- Where a region is grounded by a single desk, describe THAT desk "
        "(e.g. 'South Africa'), never the whole region.",
    ]
    # D-5 — desks carrying no region tag are IN this read and in NO region. Say
    # so: the old path could not see them at all, and a silent presence is the
    # same defect as a silent absence pointing the other way.
    if unassigned:
        lines.append(
            f"- {sum(int(c.get('input_count') or 0) for c in unassigned)} desk "
            "read(s) in this view belong to no registered region frame; do NOT "
            "attribute them to a region."
        )
    # H-3c — a DECLARED cross-region thematic lane (e.g. escalation_composition)
    # that produced no admitted read this cycle: its head was floored out on
    # faithfulness (correctly withheld) or is absent. NAME it as unassessed so
    # the world never implies a cross-region synthesis it does not have.
    for tg in thematic_gaps:
        name = str(tg.get("region_name") or tg.get("region_id") or "(thematic)")
        lines.append(
            f"- Thematic lane NOT available this cycle: {name} produced no "
            "admitted read (its head was floored out on faithfulness, or is "
            "absent). Do NOT infer or assert a cross-region synthesis for it; "
            "name it as an unassessed lane."
        )
    return "\n".join(lines)


def _render_desk_coverage_block(coverage: Sequence[Mapping[str, Any]]) -> str:
    """Render the appended DESK COVERAGE block for the THEMATIC compose (S2-T4).

    ONLY the GAP desks (mode ``gap`` — no escalation head this window) are listed,
    so the thematic model NAMES each as an unassessed desk (absence-honest) instead
    of silently omitting it. Desks WITH a read need no prose nudge — their reads
    appear as cited blocks. Empty / no-gap coverage → ``""`` (the block is absent;
    the thematic prompt's desk-gap rule is then inert).
    """
    gaps = [c for c in coverage if str(c.get("mode")) == THEMATIC_MODE_GAP]
    if not gaps:
        return ""
    lines = [
        "",
        "DESK COVERAGE (absence-honest — these desks have NO escalation read this "
        "cycle. NAME each as an unassessed gap; do NOT infer or invent its state):",
    ]
    for g in gaps:
        name = str(g.get("desk_name") or g.get("desk_id") or "(unknown desk)")
        did = str(g.get("desk_id") or "")
        lines.append(f"- {name} ({did}): no current escalation read.")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# D-5 THE CASCADE — the world reads COUNTRY assemblies, and the region tier
# becomes a rollup (planning/DEMOTION_D1_SPEC_2026-09-04.md §4)
# ---------------------------------------------------------------------------
#
# THE TRAP THIS DISSOLVES, stated where the code lives. ``read_other_analyst_
# findings`` admits a row only through an INNER ``JOIN LATERAL`` requiring
# ``kind='critique'`` AND ``title LIKE 'Faithfulness verify%'`` AND
# ``overall_score IS NOT NULL``, and the world path passes a non-``None``
# ``verify_floor`` unconditionally. A DETERMINISTIC region rollup has no
# faithfulness critique — so with the region tier retired and the world still
# reading it, EVERY region would fall through to the ``country_fallback`` branch
# and ``region_coverage[].mode`` would never read ``"region"`` again. The tower
# would lose its middle floor silently, in production, with every test green.
#
# Structural critiques do not rescue it (they are explicitly never faithfulness
# critiques), and writing a faithfulness-shaped critique for a grading that did
# not happen is the dishonesty this codebase refuses everywhere else.
#
# So the dependency is REMOVED rather than defended: under the assembly regime
# the world assembler stops reading ``region_composition`` at all and reads the
# ``country_composition`` roster directly. The world path then cannot depend on
# a region critique because it cannot see a region row — which is a structural
# property, not a rule somebody has to keep obeying.


def world_reads_countries(*, world_analyst_id: Any) -> bool:
    """Does the WORLD slice read COUNTRY assemblies instead of region heads?

    ONE flag (``LEGBA_COMPOSITION_ASSEMBLY``, per-analyst), and TWO terms,
    because the trap above fires under either half of a partial rollout:

      * the REGION tier is in the regime ⇒ region rows are rollups with no
        faithfulness critique, so the world MUST stop reading them or it
        silently degrades every region to country-fallback forever;
      * the WORLD tier is in the regime ⇒ the world assembles, and §4.2's whole
        point is that its candidate pool becomes ~32 country lead spans instead
        of 6 already-crowned region BLUFs.

    A country-ONLY rollout (the flag naming ``country_composition`` alone) keeps
    the legacy world-over-regions read, and correctly: region heads are still
    generative there, still carry critiques, and nothing is trapped.

    The deferred import keeps this module free of an import-time dependency on
    the assembly package, exactly as ``_default_basis_reader`` does.
    """
    from .assembly_payload import assembly_enabled

    return bool(
        assembly_enabled(REGION_COMPOSITION_ANALYST_ID)
        or assembly_enabled(world_analyst_id)
    )


def world_admissible_analyst_ids(
    region_analyst_ids: Sequence[str],
) -> list[str]:
    """W-2 — the analyst ids the WORLD surface may read, post-D-5. ONE list.

    :func:`_assemble_world_country_slice` has always derived this set inline for
    its BASIS query: the country frame producer plus whatever declared
    cross-region THEMATIC lanes the descriptor names. What it never fed was the
    other gather over the same surface — the C-TIER PERIPHERY read, which kept
    using the descriptor's raw ``other_analysts`` roster. Post-D-5 that roster
    still names ``region_composition``, and region reads are no longer the
    world's candidates at all.

    THE LEAK THAT PRODUCED, measured on the 2026-09-06 12:00Z world read: the
    basis gather ranked 33 country + thematic candidates and carried 8, while
    the periphery gather returned the FIVE region rollups — rows that never
    competed, whose ``cited_mass`` is 0.0 and whose ``rank`` is null — and the
    drop ledger filed them under ``below_floor``. The record therefore published
    "5 below the verification floor" naming Indo-Pacific, Europe, the Americas,
    MENA and Africa; the Assessment read that ledger and wrote that the record
    "lacks any region-level reads that passed verification" for all five — while
    the same export ships those five rollups as findings. They are
    ``region_rollup.v1`` derivations, byte-identical carries of the very country
    reads the world DID carry, and calling them candidates that failed a floor
    is false twice: they were not candidates, and they did not fail.

    A why-class cannot repair it either. ``not_a_candidate`` would still put
    them in ``declared_aperture``'s "units this record could have carried and
    did not" — and the world could not have carried them. So the fix is at the
    GATHER: the periphery is the complement of the basis over the SAME analyst
    set, which is what the C-TIER contract said all along, and sharing this
    function is what makes that a structural property instead of a rule two call
    sites have to keep obeying.
    """
    declared_thematic = [
        aid for aid in region_analyst_ids
        if aid not in (REGION_COMPOSITION_ANALYST_ID, COUNTRY_COMPOSITION_ANALYST_ID)
    ]
    return [COUNTRY_COMPOSITION_ANALYST_ID] + sorted(set(declared_thematic))


# The member roster WITH display names — the rollup names the members it could
# not see, and an id is not a name. A separate constant rather than a widened
# ``_REGION_MEMBERS_SQL`` so the existing resolver stays byte-for-byte on the
# legacy path (it is called on every region run, flag on or off).
_REGION_MEMBER_ROSTER_SQL = """
    SELECT descriptor_id, name
      FROM target_descriptors
     WHERE is_head = TRUE
       AND state = 'active'
       AND descriptor_id <> $1
       AND (body -> 'scope' -> 'tags') ? $1
     ORDER BY descriptor_id
"""

_REGION_NAME_SQL = """
    SELECT name FROM target_descriptors
     WHERE descriptor_id = $1 AND is_head = TRUE
     LIMIT 1
"""

#: W-2b — denormalised onto every WORLD-slice row: the world's DECLARED UNIT
#: ROSTER (every region frame's member country desk, plus the declared
#: cross-region thematic lanes), sorted.
#:
#: THE HOLE IT FILLS. The world read has been publishing ``coverage: []`` and
#: ``coverage_roster: []`` while every country read publishes 32 of 32, because
#: the only denominator ``_run`` knew about is ``options['source_analyst_ids']``
#: — the DESK roster, which is per-country by construction. With no roster the
#: aperture block tells the voice "unit roster: not computed at this grain, so
#: this list is the drop ledger alone", and the blind-spot arm has nothing to
#: check a named unit against: the 2026-09-07 00:15Z Assessment's region-naming
#: sentence graded *supported* only because the five region rows happened to sit
#: in ``drops.below_floor`` — the very leak W-2 closes.
#:
#: It is the ``_region_coverage`` idiom, for the same reason: a DB-less ``_run``
#: must be able to name a unit that produced NOTHING, and that is a fact about
#: the roster which cannot be recovered from the rows that arrived. Resolved for
#: free inside the slice, which already reads every frame's membership to bucket
#: the country rows.
WORLD_ROSTER_ROW_KEY: str = "_world_roster"


#: Amendment 7f — THE UNIT'S HUMAN NAME. Same column and same ``is_head``
#: predicate as ``_REGION_MEMBER_ROSTER_SQL`` above, which has been resolving
#: exactly this for the region rollup's member roster since D-5; there is no
#: second source of truth for a unit's name and this does not invent one.
#:
#: NOT filtered on ``state = 'active'``, and that is deliberate: the drop
#: ledger names reads from desks that may have been retired since, and a
#: retired desk's name is still the true name of the thing the record
#: carried. Filtering here would hand those rows back their slug for no
#: reason a reader could see.
_UNIT_NAME_SQL = """
    SELECT descriptor_id, name
      FROM target_descriptors
     WHERE is_head = TRUE
       AND descriptor_id = ANY($1::text[])
"""

#: Denormalised onto every slice row: ``{slug: human name}`` for the units
#: this slice can name. The ``_world_roster`` idiom exactly — the DB-less
#: ``_run`` builds the payload and must be able to name a unit, which is a
#: fact about the DESCRIPTOR TABLE and cannot be recovered from the rows that
#: arrived.
UNIT_NAMES_ROW_KEY: str = "_unit_names"


async def resolve_unit_names(conn, target_ids: Sequence[str]) -> dict[str, str]:
    """``{slug: name}`` for the given units — ONE read, or none at all.

    THE DEFECT THIS CLOSES (``planning/ASSESSMENT_TENSIONS_IN_MAP_REPORT.md``
    §5.1). The spine carries ``target_name == target_id`` on every block and
    every drop row, so the voice is handed ``country_watch_kp`` and writes
    "Pakistan" — ``kp`` is North Korea — and the judge fails the sentence,
    correctly, because no name at all is in the map to check it against.

    A name is not evidence and this is not a widening of the fence: it is the
    label of a unit the record ALREADY declares, resolved once here where a
    connection legally exists, so the fenced readers downstream can print it.
    """
    ids = sorted({str(t) for t in target_ids if t})
    if not ids:
        return {}
    rows = await conn.fetch(_UNIT_NAME_SQL, ids)
    return {
        str(r["descriptor_id"]): str(r["name"])
        for r in rows
        if r["name"] and str(r["name"]) != str(r["descriptor_id"])
    }


def stamp_unit_names(
    rows: Sequence[dict[str, Any]], names: Mapping[str, str]
) -> None:
    """Denormalise the name map onto every slice row (in place)."""
    for row in rows:
        row[UNIT_NAMES_ROW_KEY] = dict(names)


def unit_names_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """The name map off the slice rows, or ``{}`` — the ``world_roster_of``
    idiom, and the same honest empty when nothing stamped one."""
    for row in rows:
        names = row.get(UNIT_NAMES_ROW_KEY)
        if isinstance(names, Mapping) and names:
            return {str(k): str(v) for k, v in names.items()}
    return {}


def stamp_world_roster(
    rows: Sequence[dict[str, Any]], roster: Sequence[str]
) -> None:
    """Denormalise the world's declared unit roster onto every slice row."""
    units = list(roster)
    for row in rows:
        row[WORLD_ROSTER_ROW_KEY] = units


def world_roster_of(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Read the stamp back off the first row that carries it; ``[]`` when no row
    does — an absent roster stays absent rather than being reconstructed from
    the rows that arrived, which is the original defect."""
    for row in rows:
        units = row.get(WORLD_ROSTER_ROW_KEY)
        if isinstance(units, (list, tuple)) and units:
            return [str(u) for u in units]
    return []


#: Denormalised onto every region-slice row: the frame's identity + its FULL
#: member roster. The ``_region_coverage`` idiom — the DB-less ``_run`` builds
#: the rollup and must be able to name a member that produced nothing, which is
#: a fact about the ROSTER and cannot be recovered from the rows that arrived.
REGION_MEMBERSHIP_ROW_KEY: str = "_region_membership"


async def resolve_region_membership(conn, region_id: str) -> dict[str, Any]:
    """The region frame's identity + named member roster (D-5).

    Two small reads on the same table the legacy resolver already hits. Called
    ONLY on the rollup path, so the legacy region run issues not one extra
    query.
    """
    rows = await conn.fetch(_REGION_MEMBER_ROSTER_SQL, str(region_id))
    members: list[dict[str, str]] = []
    for r in rows:
        did = str(r["descriptor_id"])
        name = r["name"]
        members.append({"target_id": did, "target_name": str(name) if name else did})
    frame = await conn.fetchrow(_REGION_NAME_SQL, str(region_id))
    region_name = str(frame["name"]) if frame and frame["name"] else str(region_id)
    return {
        "region_id": str(region_id),
        "region_name": region_name,
        "members": members,
    }


def stamp_region_membership(
    rows: Sequence[dict[str, Any]], membership: Mapping[str, Any]
) -> None:
    """Denormalise ``membership`` onto every slice row (in place)."""
    for row in rows:
        row[REGION_MEMBERSHIP_ROW_KEY] = membership


# ---------------------------------------------------------------------------
# STEP E — THE WORLD TIER'S CONTEXT: THE COUNTRY ASSESSMENT
# ---------------------------------------------------------------------------

#: STEP E's analyst id, spelled here rather than imported from
#: ``assessment_channel``: that module imports the synthesizer's dispatch and
#: this one is imported BY the synthesizer, so an import would close a cycle for
#: a nine-character string. ``test_world_reads_countries_step_e.py`` pins the two
#: spellings equal — the ``ASSEMBLY_SCHEMA`` / ``LEAD_EARNED_SINGLE`` discipline.
COUNTRY_ASSESSMENT_ANALYST_ID: str = "country_assessment"

#: How far back a FALLBACK country assessment may be. Matches
#: ``assessment_channel.DEFAULT_SPINE_WINDOW_HOURS`` (pinned by a test): the
#: channel will not write an assessment from a record older than this, so an
#: assessment older than this is one the channel itself would refuse to produce
#: today, and carrying it as a country's current voice would be a currency claim
#: nothing on the page supports.
COUNTRY_ASSESSMENT_CONTEXT_WINDOW_HOURS: int = 24

#: ``context_match`` — the EXACT match: this assessment was written FROM the very
#: country assembly the world block is carried through (``derived_from[0]`` is
#: that row's id). The assessment channel's whole warrant is
#: ``derived_from == [spine_id]``, so this predicate is not a heuristic about
#: freshness — it is the channel's own contract read back.
CONTEXT_MATCH_THIS_RECORD: str = "assessment_of_this_record"

#: ``context_match`` — the FALLBACK: no assessment names this candidate row, so
#: the newest live assessment for the same TARGET inside the window is carried
#: instead, and the difference is RECORDED rather than smoothed over. It happens
#: when the country composed again after its assessment ran (the world then
#: carries a block from a record the country voice has not read yet), and a
#: reader who can see which rule fired can see exactly that.
CONTEXT_MATCH_TARGET_RECENT: str = "assessment_of_target_recent"

#: Denormalised onto every world-slice row: ``{candidate_row_id: {...}}``. The
#: ``_unit_names`` / ``_world_roster`` idiom exactly — the DB-less ``_run``
#: builds the payload and must be able to hand the world voice the country
#: voice's words, which is a fact about ANOTHER TABLE and cannot be recovered
#: from the rows that arrived.
COUNTRY_ASSESSMENT_CONTEXT_ROW_KEY: str = "_country_assessment_context"

#: The assessment heads for a set of candidate country assemblies, in ONE read.
#:
#: ``DISTINCT ON (a.derived_from[1])`` folds to the newest live head per SPINE,
#: which is the exact grain the exact-match rule wants. The predicates are the
#: assessment channel's own read, restated: ``kind='finding'`` (the channel also
#: writes ``kind='critique'`` verify rows under the same analyst_id — 4 of the 5
#: newest rows live on 2026-09-20 were critiques), ``superseded_by IS NULL`` (the
#: supersession fold keeps one live head per country), and the freshness window.
_COUNTRY_ASSESSMENT_BY_SPINE_SQL = """
    SELECT DISTINCT ON (a.derived_from[1])
           a.derived_from[1] AS spine_id, a.id, a.target_id, a.body,
           a.produced_at
      FROM analyst_outputs a
     WHERE a.kind = 'finding'
       AND a.analyst_id = $1
       AND a.superseded_by IS NULL
       AND a.derived_from[1] = ANY($2::uuid[])
       AND a.body <> ''
     ORDER BY a.derived_from[1], a.produced_at DESC, a.id DESC
"""

#: The FALLBACK read, by target and bounded by the window. Written out rather
#: than assembled from a fragment, for the reason ``_SPINE_SQL_TARGET`` gives:
#: the two statements sit side by side so a reader can see that the difference
#: is the KEY (spine id vs target id) and the added freshness bound, and a test
#: diffs them rather than a runtime.
_COUNTRY_ASSESSMENT_BY_TARGET_SQL = """
    SELECT DISTINCT ON (a.target_id)
           a.target_id, a.id, a.body, a.produced_at, a.derived_from[1] AS spine_id
      FROM analyst_outputs a
     WHERE a.kind = 'finding'
       AND a.analyst_id = $1
       AND a.superseded_by IS NULL
       AND a.target_id = ANY($2::text[])
       AND a.produced_at >= NOW() - make_interval(hours => $3)
       AND a.body <> ''
     ORDER BY a.target_id, a.produced_at DESC, a.id DESC
"""


async def resolve_country_assessment_context(
    conn,
    rows: Sequence[Mapping[str, Any]],
    *,
    window_hours: int = COUNTRY_ASSESSMENT_CONTEXT_WINDOW_HOURS,
) -> dict[str, dict[str, Any]]:
    """``{candidate_row_id: {head_id, body, target_id, produced_at, match}}``.

    STEP E's ONE READ (two statements, both bounded, both keyed on ids this
    slice already holds). ``rows`` are the COUNTRY ASSEMBLY candidates the world
    tier will carry blocks from; the result is keyed by those rows' ids because
    that id is what a carried block publishes as ``via_head_id`` — so the
    DB-less assembler can join the two without knowing anything about this read.

    TWO RULES, IN ORDER, AND THE SECOND IS LABELLED.

      1. the assessment written FROM THIS RECORD — ``derived_from[0]`` is the
         candidate's own id. Exact, and it is the channel's own contract rather
         than a guess: ``run_assessment`` raises unless ``derived_from`` is
         exactly ``[spine_id]``.
      2. failing that, the newest live assessment for the same TARGET inside
         ``window_hours``. The country composed again after its voice ran, so
         the world is carrying a block the country voice has not read; the
         country's CURRENT read of that country is still the best account of it
         there is, and carrying it under :data:`CONTEXT_MATCH_TARGET_RECENT`
         says so on the record rather than pretending the two rows are one.

    THEMATIC CANDIDATES ARE NOT PASSED HERE AND GET NOTHING. An
    ``escalation_composition`` row is a cross-region object with no country
    voice behind it; the caller passes only ``country_composition`` rows, and
    a thematic block therefore carries no context span at all. Measured
    2026-09-20 on the live world record ``ae07ce8c``: 8 blocks carried, 7 from
    country assemblies, 1 (ordinal 1) from ``escalation_composition`` — which
    is why the filter is the CANDIDATE'S ANALYST rather than "did a match turn
    up", a test that would have silently attached Ukraine's country voice to a
    thematic block through the fallback.

    Empty in, empty out, and no query issued.
    """
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        rid = str(row.get("id") or "")
        if rid:
            by_id[rid] = row
    if not by_id:
        return {}

    out: dict[str, dict[str, Any]] = {}
    exact = await conn.fetch(
        _COUNTRY_ASSESSMENT_BY_SPINE_SQL,
        COUNTRY_ASSESSMENT_ANALYST_ID,
        sorted(by_id),
    )
    for r in exact:
        spine_id = str(r["spine_id"])
        if spine_id not in by_id:
            continue
        out[spine_id] = {
            "head_id": str(r["id"]),
            "body": str(r["body"] or ""),
            "target_id": (
                str(r["target_id"]) if r["target_id"] is not None else None
            ),
            "produced_at": r["produced_at"],
            "match": CONTEXT_MATCH_THIS_RECORD,
        }

    missing = {
        rid: str(row.get("target_id") or "")
        for rid, row in by_id.items()
        if rid not in out and row.get("target_id")
    }
    if not missing:
        return out

    recent = await conn.fetch(
        _COUNTRY_ASSESSMENT_BY_TARGET_SQL,
        COUNTRY_ASSESSMENT_ANALYST_ID,
        sorted(set(missing.values())),
        int(window_hours),
    )
    by_target = {str(r["target_id"]): r for r in recent}
    for rid, tid in missing.items():
        r = by_target.get(tid)
        if r is None:
            continue
        out[rid] = {
            "head_id": str(r["id"]),
            "body": str(r["body"] or ""),
            "target_id": tid,
            "produced_at": r["produced_at"],
            "match": CONTEXT_MATCH_TARGET_RECENT,
        }
    return out


def stamp_country_assessment_context(
    rows: Sequence[dict[str, Any]], context: Mapping[str, Mapping[str, Any]]
) -> None:
    """Denormalise the context map onto every slice row (in place)."""
    payload = {str(k): dict(v) for k, v in context.items()}
    for row in rows:
        row[COUNTRY_ASSESSMENT_CONTEXT_ROW_KEY] = payload


def country_assessment_context_of(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """The context map off the slice rows, or ``{}`` — the ``unit_names_of``
    idiom, and the same honest empty when nothing stamped one: a legacy slice, a
    direct caller, or a world run on a day no country voice produced."""
    for row in rows:
        ctx = row.get(COUNTRY_ASSESSMENT_CONTEXT_ROW_KEY)
        if isinstance(ctx, Mapping) and ctx:
            return {str(k): dict(v) for k, v in ctx.items()}
    return {}


async def _assemble_world_country_slice(
    conn,
    *,
    region_analyst_ids: Sequence[str],
    time_window_hours: int,
    limit: int,
    verify_floor: float | None,
    basis_reader: BasisReader | None = None,
) -> list[dict[str, Any]]:
    """D-5 §4.2 — the WORLD slice over COUNTRY assemblies.

    Same contract as :func:`_assemble_world_region_slice` and the same output
    annotations (``_region_id`` / ``_region_mode`` / ``_region_coverage``), so
    every downstream reader — the coverage stamp, the gap render, the aperture
    block, the DB-less ``_run`` — works unedited. What changes is the INPUT
    TIER, and with it three things worth naming:

      * **the candidate pool.** 6 already-crowned region BLUFs become ~32
        country lead spans. The breadth win VOICE §4.4 predicted, and the
        earned-lead test's real denominator: a test over six pre-selected
        headlines was measuring the region tier's choices, not the day's.
      * **the depth.** A world span is cut from a COUNTRY assembly whose own
        blocks quote DESK HEADS, so the chain to the desk that wrote the
        sentence is two verifiable hops with one implementation, and no hop
        passes through prose a model wrote about prose a model wrote.
      * **the thematic lane is untouched.** A declared cross-region thematic
        analyst (``escalation_composition``) is still admitted as a labelled
        block and still NAMED as a gap when it produced nothing. That lane is
        the world's one legal cross-region object and retiring the region tier
        must not quietly retire it too.

    ONE basis query covers both (country heads + thematic heads share the
    ``DISTINCT ON (analyst_id, target_id)`` fold: a country row keys on its
    target, a target-less thematic row keys on its analyst). The per-region
    member reads that follow are the same tag-membership SQL the region tier
    uses, reused verbatim.
    """
    _read_basis = basis_reader or _default_basis_reader()
    roster = await _resolve_region_roster(conn)

    # W-2 — the basis set and the PERIPHERY set are now one list, named once
    # (:func:`world_admissible_analyst_ids`). The thematic-gap pass below reads
    # its lanes back off the same list rather than re-deriving them, so the two
    # cannot drift apart the way the periphery gather silently had.
    admissible = world_admissible_analyst_ids(region_analyst_ids)
    declared_thematic = [
        aid for aid in admissible if aid != COUNTRY_COMPOSITION_ANALYST_ID
    ]
    rows = await _read_basis(
        conn,
        analyst_ids=admissible,
        time_window_hours=time_window_hours,
        limit=limit,
        target_id=None,
        verify_floor=verify_floor,
        include_meta=True,
    )

    country_rows: list[dict[str, Any]] = []
    thematic_by_analyst: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        aid = str(r.get("analyst_id") or "")
        tid = str(r.get("target_id") or "")
        if aid == COUNTRY_COMPOSITION_ANALYST_ID and tid:
            country_rows.append(r)
            continue
        # A target-LESS head from a declared cross-region thematic analyst. A
        # country_composition row with no target_id cannot happen (the fan-out
        # stamps one) and would be unplaceable in a region; it is dropped from
        # the region buckets rather than filed under a region it is not in.
        if aid and aid != COUNTRY_COMPOSITION_ANALYST_ID:
            r["_region_id"] = f"thematic:{aid}"
            r["_region_mode"] = REGION_MODE_THEMATIC
            thematic_by_analyst.setdefault(aid, []).append(r)

    # No region roster (a pre-S2-T1 topology): there is no frame to bucket over,
    # so feed the country heads unbucketed. Coverage is simply absent — the same
    # honest degrade the legacy assembler makes in the same situation.
    if not roster:
        unbucketed = country_rows + [
            r for rs in thematic_by_analyst.values() for r in rs
        ]
        # STEP E stamps on BOTH arms. A pre-S2-T1 topology still assembles a
        # world record out of country assemblies, and a world voice handed one
        # sentence per country is the defect this train exists to close — it is
        # not less true because the region frames are missing.
        stamp_country_assessment_context(
            unbucketed,
            await resolve_country_assessment_context(conn, country_rows),
        )
        return unbucketed

    region_by_member: dict[str, dict[str, str]] = {}
    for region in roster:
        rid = region["region_id"]
        for member_id in await _resolve_region_member_target_ids(conn, rid):
            # A desk tagged into two frames is filed under the FIRST by
            # descriptor_id order — deterministic, and the duplicate would
            # otherwise double-count the desk in the world's coverage totals.
            region_by_member.setdefault(member_id, region)

    # W-2b — the DECLARED UNIT ROSTER, resolved here because this is the one
    # place that already knows it: every frame's member country desks (the
    # denominator the world ranks over) plus the declared thematic lanes (the
    # world's one legal cross-region object). Sorted so the persisted roster is
    # stable across runs and ARM 4(a) diffs two arrays rather than two orderings.
    world_roster = sorted(set(region_by_member) | set(declared_thematic))

    combined: list[dict[str, Any]] = []
    by_region: dict[str, list[dict[str, Any]]] = {}
    unassigned: list[dict[str, Any]] = []
    for r in country_rows:
        region = region_by_member.get(str(r.get("target_id") or ""))
        if region is None:
            r["_region_id"] = UNASSIGNED_REGION_ID
            r["_region_mode"] = REGION_MODE_UNASSIGNED
            unassigned.append(r)
            continue
        r["_region_id"] = region["region_id"]
        r["_region_mode"] = REGION_MODE_COUNTRIES
        by_region.setdefault(region["region_id"], []).append(r)

    coverage: list[dict[str, Any]] = []
    for region in roster:
        rid = region["region_id"]
        members = by_region.get(rid) or []
        combined.extend(members)
        coverage.append({
            "region_id": rid,
            "region_name": region["region_name"],
            "mode": REGION_MODE_COUNTRIES if members else REGION_MODE_GAP,
            "input_count": len(members),
        })
    if unassigned:
        combined.extend(unassigned)
        coverage.append({
            "region_id": UNASSIGNED_REGION_ID,
            "region_name": UNASSIGNED_REGION_NAME,
            "mode": REGION_MODE_UNASSIGNED,
            "input_count": len(unassigned),
        })

    for aid, trows in sorted(thematic_by_analyst.items()):
        combined.extend(trows)
        coverage.append({
            "region_id": f"thematic:{aid}",
            "region_name": f"{aid} (cross-region thematic)",
            "mode": REGION_MODE_THEMATIC,
            "input_count": len(trows),
        })
    for aid in sorted(set(declared_thematic)):
        if aid not in thematic_by_analyst:
            coverage.append({
                "region_id": f"thematic:{aid}",
                "region_name": f"{aid} (cross-region thematic)",
                "mode": REGION_MODE_THEMATIC_GAP,
                "input_count": 0,
            })

    for row in combined:
        row["_region_coverage"] = coverage
    stamp_world_roster(combined, world_roster)
    # Amendment 7f — the names of every unit this slice can put in front of
    # the voice: the declared roster plus the target of every row that
    # arrived (the drop ledger is built from these, and a dropped desk that
    # is on no roster still gets named).
    stamp_unit_names(
        combined,
        await resolve_unit_names(
            conn,
            list(world_roster)
            + [str(r.get("target_id") or "") for r in combined],
        ),
    )
    # STEP E — THE COUNTRY VOICE, resolved here because this is the one place a
    # connection legally exists and the one place that already knows WHICH rows
    # are country assemblies. ``country_rows`` and not ``combined``: a thematic
    # candidate has no country voice behind it and must not be given one through
    # the fallback. The assembler joins on the candidate row id, which a carried
    # block republishes as ``via_head_id``.
    stamp_country_assessment_context(
        combined,
        await resolve_country_assessment_context(conn, country_rows),
    )
    return combined


# ---------------------------------------------------------------------------
# S2-T4 THEMATIC composition — desk roster + slice assembly
# ---------------------------------------------------------------------------


async def _resolve_desk_roster(conn) -> list[dict[str, str]]:
    """Resolve the active g20+watch DESK roster (S2-T4).

    Returns ``[{"desk_id", "desk_name"}, ...]`` for every active head target
    tagged ``g20`` or ``watch`` (the desks the units fan out to). The thematic
    compose diffs this authoritative desk set against the desks that actually have
    an escalation head to decide which desks are HONEST gaps. Tag-based (matches
    scorecard_producer + the units' subscription) so registering a new desk with
    the g20/watch tag auto-joins the coverage with zero code change. An empty
    roster (a pre-tag topology) tells the caller to skip the gap/coverage frame.
    """
    rows = await conn.fetch(_DESK_ROSTER_SQL)
    roster: list[dict[str, str]] = []
    for r in rows:
        did = str(r["descriptor_id"])
        name = r["name"]
        roster.append({"desk_id": did, "desk_name": str(name) if name else did})
    return roster


async def _assemble_thematic_unit_slice(
    conn,
    *,
    unit_analyst_ids: Sequence[str],
    time_window_hours: int,
    limit: int,
    verify_floor: float | None,
    desk_ids: Sequence[str] | None = None,
    basis_reader: BasisReader | None = None,
) -> list[dict[str, Any]]:
    """S2-T4 — assemble the THEMATIC composition slice: ONE verified head per DESK
    of a UNIT analyst dimension, across ALL desks, with desk-coverage gaps.

    Reads the latest verify-floored head of the ``escalation`` unit for EVERY desk
    (``dedupe_heads=True`` folds superseded prior-cycle rows + ``DISTINCT ON
    (analyst_id, target_id)`` yields one head per desk; ``include_meta=False`` — the
    unit is a FIRST-ORDER finding). Then diffs the assessed-desk roster
    (``_DESK_ROSTER_SQL``: g20 + watch + supply_chain):

      * a desk WITH a head feeds the compose (mode ``present``);
      * a desk with NO head is a GAP (mode ``gap``, 0 inputs) — NAMED, not dropped.

    Every returned row is stamped with ``_desk_id`` + ``_desk_mode`` and — so the
    target-LESS thematic ``_run`` (which has NO DB access) can NAME any gap desk in
    the prose — the full per-desk coverage list is denormalized onto EVERY returned
    row as ``_thematic_coverage``. These synthetic ``_``-prefixed keys are ephemeral
    input-row annotations (the orient/render/cite paths read only their own known
    keys; the persisted finding is built fresh in ``_coerce_finding``).

    A read that surfaces ZERO escalation heads returns ``[]``; the actor then NOOPs
    the run (no finding written) — the standard empty-slice contract.
    """
    _read_basis = basis_reader or _default_basis_reader()
    rows = await _read_basis(
        conn,
        analyst_ids=list(unit_analyst_ids),
        time_window_hours=time_window_hours,
        limit=limit,
        target_id=None,
        target_ids=(list(desk_ids) if desk_ids else None),  # S2-T5: dyad allow-list
        verify_floor=verify_floor,
        include_meta=False,     # the escalation UNIT is a FIRST-ORDER finding
        dedupe_heads=True,      # one head per (analyst,target) desk, superseded folded
    )
    # Which desks actually have a head (one per desk after DISTINCT ON).
    desks_with_head: set[str] = set()
    for r in rows:
        tid = str(r.get("target_id") or "")
        r["_desk_id"] = tid
        r["_desk_mode"] = THEMATIC_MODE_PRESENT
        if tid:
            desks_with_head.add(tid)

    if not rows:
        # No heads at all → empty slice → the actor NOOPs (no coverage to stamp).
        return rows

    roster = await _resolve_desk_roster(conn)
    if desk_ids:
        # S2-T5 DYAD: coverage spans ONLY the allow-list desks (not all g20+watch).
        allow = {str(d) for d in desk_ids}
        roster = [d for d in roster if d["desk_id"] in allow]
    coverage: list[dict[str, Any]] = []
    for desk in roster:
        did = desk["desk_id"]
        dname = desk["desk_name"]
        if did in desks_with_head:
            coverage.append(
                {
                    "desk_id": did,
                    "desk_name": dname,
                    "mode": THEMATIC_MODE_PRESENT,
                    "input_count": 1,
                }
            )
        else:
            coverage.append(
                {
                    "desk_id": did,
                    "desk_name": dname,
                    "mode": THEMATIC_MODE_GAP,
                    "input_count": 0,
                }
            )

    # Denormalize coverage onto every row so the DB-less thematic ``_run`` reads it.
    for row in rows:
        row["_thematic_coverage"] = coverage
    return rows
