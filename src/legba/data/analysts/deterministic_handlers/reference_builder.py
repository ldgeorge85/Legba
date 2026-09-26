# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 — THE REFERENCE UNIT: the job that builds what the grader grades against.

G1 shipped the correctness instrument and deliberately did NOT ship the thing it
measures against (``docs/SEAMS.md`` #55). ``unit_references`` was populated by
hand, once, for one country. This module is that seam closed: per country per
14-day window, a span-anchored independent reference, built on the $0 core
plane, under fences that are code.

WHY THIS SHAPE AND NOT THE OBVIOUS ONE. The obvious design is "let a good model
read the web and write the reference". R1 measured that, twice, and the verdict
was NO — not because the model could not find things (run 2 had 20–45 results a
query and closed a gap the Opus lane had declared: the Supreme Court striking
the Haredi-draft arrest moratorium) but because it SELECTED badly and DATED
badly. It carried 33% of a knowledgeable reader's major developments and it
shipped a development that was FALSE — a November-2024 appointment dated off a
masthead. For a reference, the second failure is disqualifying: a thin reference
under-covers, but a reference carrying a false development actively MIS-GRADES
live reads. It marks correct work wrong.

So the design is HYBRID, and the split is between what a model is good at and
what it is not:

  * **The model finds and quotes.** Free, on hardware we own, under the six
    fences in :mod:`_reference_fences`. It produces a skeleton it can defend
    span by span.
  * **Code decides what is accepted.** The fetched-URL manifest, the date gate,
    the tier allowlist and the span check run AFTER the model commits, over
    archived text the model cannot reach. Nothing the model asserts about a
    date, a URL or a tier is taken on its word.
  * **Thinness is declared, not hidden.** A dimension with fewer than two
    surviving developments is written to ``thin_dimensions`` — the column the
    grader ALREADY reads. The free lane therefore under-claims rather than
    mis-grades, which is the only honest thing a free lane can do.
  * **The substance gap is the operator's money decision, and it is never
    taken here.** ``scripts/reference_topup_packet.py`` writes a packet for an
    operator-run lane; ``scripts/load_unit_reference.py --merge-into`` takes the
    result back through the SAME fences. Nothing schedules it. No LLM call in
    this module ever leaves the core plane.

THE STAGGER IS DERIVED, NOT SCHEDULED. R1 measured one reference at ~1–1.7M
core-plane prompt tokens and ~8 minutes of ai1. A 33-target roster on a
fortnightly cadence is ~4–5 hours of ai1 per cycle, which is affordable only if
at most one runs at a time. So the cadence tick fires often and the handler
picks the SINGLE most overdue target (``max_targets_per_run`` = 1): the roster
staggers itself, a new target joins by appearing in the roster, and no stagger
table exists to drift out of sync with the roster.

FLAG. ``LEGBA_REFERENCE_BUILDER_ENABLED``, default OFF. Off, the handler returns
a receipt saying so and does NOTHING — no search, no fetch, no model call, no
row. The deploy is inert until an operator flips it.

COST. The core plane only, always. There is no paid route in this module to
gate, which is why it carries no ceiling env: the ceiling is the architecture.
Tokens in/out and wall time are recorded on the reference's own ``ref_json``
header, so the cost of every reference travels with it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ....runtime.analyst_method import AnalystMethodResult
from ...provenance.models import FindingPayload
from . import _reference_store as STORE
from ._reference_fences import (
    DomainBlocklist,
    NoteGate,
    apply_fences,
    dimension_counts,
    thin_from_counts,
)
from . import _reference_manifest as MANIFEST
from . import _reference_notes as NOTES
from . import _reference_roster as ROSTER
from ._reference_instruction import build_first_user, build_instruction
from ._reference_loop import (
    BUILD_MAX_SECONDS_DEFAULT,
    BUILD_MAX_TOKENS_DEFAULT,
    ToolPlane,
    run_loop,
)
from ._reference_page import (
    DEFAULT_PAGE_CHARS_TO_MODEL,
    ReferenceArchive,
)

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "reference_builder"

#: This builder's OWN population stamp. A reference built under a changed
#: pipeline is a different instrument's output and must be distinguishable
#: rather than pooled — the same rule the grader applies to its own numbers.
BUILDER_PIPELINE_VERSION = "2026-09-16/1"

#: The value written to ``unit_references.builder``. The grader publishes the
#: route as part of the number's provenance: a reference the core plane built is
#: a different instrument from one an out-of-plane model built, and a reader
#: must be able to tell without opening the JSON.
BUILDER_LABEL = "core-plane-lane"

#: After an operator top-up merges in. Set by the merge, never by this handler.
BUILDER_LABEL_TOPPED_UP = "core-plane-lane+opus-topup"

#: The kill switch. Default OFF, read HERE so an operator can disarm the lane
#: with an env change and a recreate, without unregistering the descriptor.
ENABLED_ENV = "LEGBA_REFERENCE_BUILDER_ENABLED"

#: deps.extras keys. The core-plane model leg and the web_access pack binding,
#: each under this handler's own key so no other handler can ever be handed them.
LLM_DEPS_EXTRA_KEY = "reference_builder_llm"
WEB_BINDING_DEPS_EXTRA_KEY = "reference_builder_web"

#: The window a reference covers, and the cadence at which it is rebuilt. Equal
#: by default and deliberately: consecutive references then TILE the timeline
#: with no gap and no overlap, so every as-of stamp the grader asks about falls
#: inside exactly one reference's window.
DEFAULT_WINDOW_DAYS = 14
DEFAULT_CADENCE_DAYS = 7

#: ONE target per run. See the banner: this IS the stagger.
DEFAULT_MAX_TARGETS_PER_RUN = 1

#: R1's measured budget. 80 calls produced 19 developments across 8 dimensions
#: at 78.9% span verification and ~8 minutes of ai1.
DEFAULT_TOOL_CALL_CAP = 80

#: Fewer than this many verified developments in a dimension ⇒ thin.
DEFAULT_THIN_BELOW = 2

#: How recently a desk must have produced for its target to be in the roster.
DEFAULT_ROSTER_WINDOW_DAYS = 30

#: The sampling temperature the builder runs at. NOT a descriptor option: it is
#: not a volume knob, it is part of the instrument's identity, and a reference
#: built at a different temperature is a different instrument's output. R1
#: measured both runs at 1.0 (the fleet default) and this is that value. Note
#: what is NEVER sent alongside it: ``max_tokens``. That is a HARD house rule for
#: the core plane, and the loop honours it by never passing one.
DEFAULT_TEMPERATURE = 1.0

# ---------------------------------------------------------------------------
# R2-FIX(1) — THE BUILDER'S OWN WEB BUCKET (ops 2026-09-17)
# ---------------------------------------------------------------------------
#
# WHAT STARVED THE 00:43Z BUILD. The ``web_access`` pack declares
# ``governor.budget_account: web_access`` and ``max_invocations_per_hour: 120``,
# and ``Agency.run_pack_tool`` resolves the account as
# ``res.governor.budget_account or call.budget_account`` — so the PACK's account
# wins over the per-analyst one the binding passes, and every analyst holding
# the grant draws on ONE hourly bucket. Measured on the live substrate: in one
# two-hour window ``standing_auditor`` alone spent 120 of 120. When the builder's
# tick fired it found the hour already gone, and 63 of its 67 tool calls came
# back "not admitted by the pack" — an entire build spent learning that.
#
# THE MECHANISM, and why it is not a second pack. ``ActionPackRef`` (the
# analyst's own grant) carries ``governor_override``, and
# ``agency.resolution._merge_governor`` applies it LAST with an explicit rule:
# every numeric cap takes the MORE RESTRICTIVE of the two, and
# ``budget_account`` FOLLOWS THE OVERRIDE, because "re-targeting the ledger
# account is not a loosening". That is exactly the carve-out wanted: a private
# bucket, and a cap that is still under the pack's own. A second registered pack
# would duplicate the tool specs, the SSRF guard's route and the prompt rules,
# and would need its own registration, its own lifecycle and its own drift; this
# needs neither new machinery nor a runtime change.
#
# NOT A RAISE OF THE SHARED BUCKET. web_access stays at 120/hour for everyone
# else — raising it would simply hand the auditor a bigger hour.

#: The builder's PRIVATE ledger account for ``web_access`` invocations. Nothing
#: else writes to it, so nothing else can spend it.
WEB_BUDGET_ACCOUNT = "web_access_reference_builder"

#: The builder's own hourly invocation cap on that account. One build may spend
#: ``tool_call_cap`` (80) admitted calls and the tick fires hourly, so 100 is one
#: whole build plus 25% headroom — and it is UNDER the pack's own 120, which the
#: tightening-only merge requires. Refused and cached calls never reach the
#: binding, so 80 is a real ceiling rather than an estimate.
WEB_INVOCATIONS_PER_HOUR = 1_000_000   # 2026-09-20 operator: no search cap (was 100/h; the pack is 1,000,000/h)


# ---------------------------------------------------------------------------
# R2-FIX(2) — THE DAY BUCKET, SIZED TO THE CADENCE
# ---------------------------------------------------------------------------
#
# 4,000,000 was sized for "a handful of builds" before the cadence was halved to
# 7 days, and it is what the 00:43Z run exhausted. The arithmetic the lane
# actually runs on: a roster of ``BUDGET_SIZED_FOR_ROSTER`` targets on a
# ``DEFAULT_CADENCE_DAYS`` cadence needs ceil(32 / 7) = 5 builds a day, and a
# build may spend ``DEFAULT_BUILD_MAX_TOKENS``. 5 x 1.2M = 6M, which 4M cannot
# hold — so the lane was structurally guaranteed to hit a BUDGET_THROTTLED
# cooldown roughly every day, whatever else was fixed.
#
# 8M carries that with a third to spare, on the $0 core plane where the only
# real cost is ai1 wall time (~5 x 5.5 min/day). The wiring test asserts the
# three numbers stay consistent, so a cadence change or a roster that outgrows
# this one is a RED TEST rather than a cooldown discovered in the logs.

#: The per-day token bucket (``method.budget_tokens_per_day`` on the
#: descriptor). Exceeding it is not a slow tick: it is a one-hour global
#: cooldown stamped with no log line.
DEFAULT_BUDGET_TOKENS_PER_DAY = 0

#: The roster size the day bucket above was sized for. The roster is DERIVED
#: from which dimension desks have produced recently, so it moves; this is the
#: number the budget assumes, and the wiring test fails when the two stop
#: agreeing. A roster that grows past it needs the bucket raised, not this
#: constant lowered.
BUDGET_SIZED_FOR_ROSTER = 32


#: D3/D4 — THE BUILD MUST FIT THE PLANE THAT RUNS IT. Re-exported from
#: ``_reference_loop`` so the descriptor, the option catalog and the wiring test
#: all read ONE number, and a default that moves in code cannot leave the
#: descriptor documenting a volume nobody is running. See the loop module's own
#: banner for the 2026-09-17 incident these two bound.
DEFAULT_BUILD_MAX_SECONDS = int(BUILD_MAX_SECONDS_DEFAULT)
DEFAULT_BUILD_MAX_TOKENS = int(BUILD_MAX_TOKENS_DEFAULT)

#: Receipt caps — a finding body is not a log.
_RECEIPT_ITEM_CAP = 16

#: How many of the targets BEHIND the chosen one the receipt names, with the
#: reason each is where it is. Three is what an operator needs to answer "is the
#: queue moving?" without the row becoming a roster dump.
_QUEUE_PREVIEW = 3

# ---------------------------------------------------------------------------
# SQL. Every statement this lane runs is parameterised and SELECT-only; the one
# INSERT lives in ``_reference_store``, shared with the loader so there is
# exactly one writer of ``unit_references``. The roster, identity and attempt-
# ledger statements live in ``_reference_roster`` with the scheduler that reads
# them (R2-FIX(2)) — see that module's banner for the ordering defect they fix.
# ---------------------------------------------------------------------------


def builder_enabled() -> bool:
    """The flag. Off, this handler does NOTHING — see the banner."""
    return str(os.getenv(ENABLED_ENV, "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _pos(raw: Any, default: int) -> int:
    """A positive-int knob, or its in-source default.

    Callers pass ``options.get("<literal>")`` rather than a key name,
    deliberately: the X-1 catalog's reachability sweep proves a declared knob is
    real by grepping for the literal read in THIS module.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _str_list(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(v).strip() for v in raw if str(v).strip())


def _flag(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def resolve_t0(options: Mapping[str, Any]) -> datetime:
    """The ONE stamped instant this build's window ends at.

    Read from ``as_of`` — the SAME option key the correctness grader reads for
    the same concept — so an operator replaying a stamp passes one key to both
    jobs and a reference and the grading of it can be pinned to one instant.
    ``t0`` is accepted as an alias because that is what the R1/R3 harnesses and
    the reference JSON header call it, and a reader holding a ``ref_*.json``
    should not have to translate.
    """
    raw = options.get("as_of")
    if raw is None:
        raw = options.get("t0")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        try:
            return STORE.iso(raw)
        except ValueError:
            logger.warning(
                "reference_builder.as_of_unparseable raw=%r — using now()", raw
            )
    return datetime.now(timezone.utc)


def grader_grace_days() -> tuple[int, str]:
    """How long the GRADER keeps treating a reference as current, and the env.

    Read from the grader rather than duplicated, and read DEFENSIVELY: the
    reference-currency fix is a sibling lane's, so a tree where it has not
    landed yet must degrade to "cannot tell" rather than to a wrong number.
    """
    from . import correctness_grader as CG

    env = getattr(CG, "REFERENCE_GRACE_ENV", "")
    if not env:
        return -1, ""
    default = int(getattr(CG, "DEFAULT_REFERENCE_GRACE_DAYS", 7))
    raw = (os.getenv(env) or "").strip()
    try:
        return (int(raw) if raw else default), env
    except ValueError:
        return default, env


def coverage_warning(cadence_days: int) -> str | None:
    """The one place the BUILD cadence and the GRADE currency window meet.

    A reference built at T is accepted by the grader for as-of stamps up to
    ``T + grace``. The next one lands at ``T + cadence``. So LIVE — which is the
    only way the grader ever runs — a target is covered for ``grace`` days after
    each build and reads ``reference_stale`` for the ``cadence - grace`` days
    after that. At the shipped defaults (cadence 14, grace 7) that is seven
    covered days and seven stale days per fortnight, and NOTHING else in either
    job would say so: the builder would report healthy builds and the grader
    would report stale references, each correct, neither naming the other.

    Two remedies and they are the operator's, not this handler's, because both
    spend something: halve ``cadence_days`` (twice the ai1 time, references
    always fresh) or raise the grace env (no extra cost, but claims are graded
    against an older account of the world).
    """
    grace, env = grader_grace_days()
    if grace < 0 or cadence_days <= grace:
        return None
    return (
        f"COVERAGE GAP: references are rebuilt every {cadence_days} days but "
        f"the grader treats one as current for only {grace} days after its "
        f"window closes ({env}), so each target reads `reference_stale` for "
        f"about {cadence_days - grace} day(s) per cycle and is not graded then. "
        f"Close it by lowering cadence_days to {grace} (more ai1 time, fresher "
        f"references) or by raising {env} to {cadence_days} (free, but claims "
        "are graded against an older account of the world). Both are operator "
        "decisions and neither is taken here."
    )


# ---------------------------------------------------------------------------
# THE ROSTER AND THE STAGGER — see ``_reference_roster``
# ---------------------------------------------------------------------------
#
# Re-exported at this module's surface because the roster, the cadence and the
# order they imply ARE this handler's scheduler; they moved out to keep the
# module under its size gate when R2-FIX(2) added the attempt ledger, not
# because they stopped belonging to the builder. Callers and tests address
# them here exactly as before.

resolve_roster = ROSTER.resolve_roster
target_identity = ROSTER.target_identity
due_targets = ROSTER.due_targets

#: The retry fence R2-FIX(2) added, surfaced here for the descriptor, the
#: option catalog and the wiring test to read ONE number.
DEFAULT_RETRY_BACKOFF_HOURS = ROSTER.DEFAULT_RETRY_BACKOFF_HOURS
RETRY_BACKOFF_ENV = ROSTER.RETRY_BACKOFF_ENV
UNBUILDABLE_AFTER_FAILURES = ROSTER.UNBUILDABLE_AFTER_FAILURES


def _licence_lookup(pool: Any):
    """A per-URL licence verdict, acquiring its own connection.

    A closure over the POOL and not over a connection: one build runs for
    minutes, and holding a pool connection open across it would starve every
    other analyst on the box for a lookup that takes milliseconds.

    Fails SAFE, never open: any error resolves to teaser depth (body not
    stored), which is what an unreviewed host gets anyway.
    """
    from ...research_evidence import depth_for_license, resolve_host_verdict

    async def lookup(url: str) -> tuple[str, str, str | None]:
        try:
            async with pool.acquire() as conn:
                verdict = await resolve_host_verdict(conn, url)
        except Exception as exc:  # noqa: BLE001 — fail safe
            logger.warning(
                "reference_builder.licence_lookup_failed url=%s err=%s", url, exc
            )
            return "teaser", "license_unreviewed", None
        depth, reason = depth_for_license(verdict.license_class)
        return depth, reason, verdict.license_class

    return lookup


# ---------------------------------------------------------------------------
# One target
# ---------------------------------------------------------------------------


async def build_reference(
    *,
    pool: Any,
    llm: Any,
    binding: Any,
    target_id: str,
    identity: Mapping[str, str],
    dimensions: Sequence[str],
    t0: datetime,
    window_days: int,
    tool_call_cap: int,
    thin_below: int,
    page_chars_to_model: int,
    min_developments: int,
    temperature: float,
    archive: ReferenceArchive,
    write: bool,
    build_max_seconds: float = BUILD_MAX_SECONDS_DEFAULT,
    build_max_tokens: int = BUILD_MAX_TOKENS_DEFAULT,
) -> dict[str, Any]:
    """Build, fence and (optionally) persist ONE country reference.

    Returns an OUTCOME dict — never raises for a build failure. Every failure
    mode gets a named ``status`` and reaches the receipt: ``no_model``,
    ``no_web``, ``no_commit``, ``all_rejected``, ``built``, ``duplicate``. A
    crash here would take down a sweep that may have already built another
    target; a named status is a row a human can act on.

    D3 — ``no_commit`` AND ``all_rejected`` NAME DIFFERENT FAILURES, and the
    00:43Z Argentina receipt is why the distinction had to be made explicit.
    It read ``all_rejected`` with "0 verified of 0 committed" and a rejection
    table of seven zeros — a sentence that cannot be true, because nothing was
    rejected: the model never committed a development at all. They are opposite
    diagnoses. ``all_rejected`` says the model FOUND things and the fences threw
    every one of them out — a fence to look at, or a window with nothing
    citable in it. ``no_commit`` says the build produced nothing to fence —
    a plane, a budget or a loop to look at. So ``all_rejected`` is now reachable
    ONLY with at least one committed development, and a build that committed
    none leaves its notes behind for the next attempt (``_reference_notes``).
    """
    window_start = t0 - timedelta(days=int(window_days))
    code = str(identity.get("code") or "")
    name = str(identity.get("name") or target_id)
    prefix = f"RD-{code or target_id.upper()}-C"
    outcome: dict[str, Any] = {
        "target_id": target_id,
        "country": name,
        "country_code": code,
        "window_start": window_start.isoformat(),
        "window_end": t0.isoformat(),
        "dimensions": list(dimensions),
        "status": "built",
        "warnings": [],
    }
    if llm is None:
        outcome["status"] = "no_model"
        outcome["warnings"].append(
            f"{target_id}: no core-plane model wired under "
            f"deps.extras[{LLM_DEPS_EXTRA_KEY!r}] — nothing searched, nothing "
            "fetched, nothing built."
        )
        return outcome
    if binding is None:
        outcome["status"] = "no_web"
        outcome["warnings"].append(
            f"{target_id}: no web_access binding wired under "
            f"deps.extras[{WEB_BINDING_DEPS_EXTRA_KEY!r}] (pack not granted, or "
            "the agency plane is down) — a reference cannot be built without "
            "the open web, and one built without it would not be independent."
        )
        return outcome

    model_name = str(getattr(llm, "model_name", "") or getattr(
        llm, "component_id", "") or "core-plane")
    blocklist = DomainBlocklist()
    plane = ToolPlane(
        binding=binding,
        archive=archive,
        blocklist=blocklist,
        licence_lookup=_licence_lookup(pool) if pool is not None else None,
        page_chars_to_model=page_chars_to_model,
    )
    instruction = build_instruction(
        country_name=name,
        country_code=code or target_id,
        t0=t0.isoformat(),
        window_start=window_start.isoformat(),
        dimensions=dimensions,
        item_prefix=prefix,
        builder_label=BUILDER_LABEL,
        model_name=model_name,
        tool_call_cap=tool_call_cap,
        min_developments=min_developments,
    )
    first_user = build_first_user(
        country_name=name, country_code=code or target_id,
        t0=t0.isoformat(), window_start=window_start.isoformat(),
    )
    # D3(c) — START FROM WHAT THE LAST ATTEMPT LEARNED. A build that ended in
    # ``no_commit`` left its NOTE lines behind; they are LEADS, never evidence,
    # and every one of them still has to survive the same fences. See
    # ``_reference_notes`` for the three bounds on the carry.
    carried = NOTES.load(target_id)
    if carried:
        first_user += NOTES.preamble(carried)
        # D6 — a carried PAGE is an admitted URL. The loop recorded it after a
        # successful fetch on the last attempt, so it is known to exist, and the
        # "fetch an offered result" fence must not treat it as a composed one.
        admitted = plane.admit(NOTES.carried_urls(carried))
        outcome["carried_notes"] = {
            "saved_at": carried.get("saved_at"),
            "note_lines": carried.get("note_lines"),
            "page_lines": carried.get("page_lines"),
            "chars": carried.get("chars"),
            "from_stop_reason": carried.get("stop_reason"),
            "urls_admitted": admitted,
        }

    result = await run_loop(
        llm=llm,
        plane=plane,
        instruction=instruction,
        first_user=first_user,
        dimensions=dimensions,
        tool_call_cap=tool_call_cap,
        note_gate=NoteGate(),
        temperature=temperature,
        min_noted=min_developments,
        max_seconds=build_max_seconds,
        max_tokens=build_max_tokens,
        window_start=window_start.date(),
        window_end=t0.date(),
    )

    outcome["loop"] = {
        "stop_reason": result.stop_reason,
        "rounds": result.rounds,
        "tool_calls": result.tool_calls,
        "searches": len(plane.searches),
        "fetches": len(plane.fetches),
        "pages_archived": len(archive.fetched_pages()),
        "pushbacks": result.pushbacks,
        "tier3_opened": result.tier3_opened,
        "note_gate": result.note_gate,
        "blocklist": blocklist.as_record(),
        "wall_seconds": result.wall_seconds,
        "usage": dict(result.usage),
        "commit_trigger": result.commit_trigger,
        "search_degraded": result.search_degraded,
        "build_max_seconds": result.build_max_seconds,
        "build_max_tokens": result.build_max_tokens,
        # D6 — WHAT THE LOOP READ, in numbers, whether or not the model used
        # any of it. ``manifest_in_window`` is the denominator for the one
        # question a zero-development build has to answer.
        "manifest_pages": len(result.manifest_entries),
        "manifest_in_window": len(MANIFEST.in_window_entries(
            result.manifest_entries, window_start.date(), t0.date()
        )),
        "url_repairs": result.url_repairs,
        # WHAT THE FETCHES BOUGHT, by outcome. ``timed_out`` is the one that
        # was invisible before: a real page this lane failed to reach reads
        # identically to a host that refused it unless they are counted apart.
        "fetch_outcomes": dict(result.fetch_outcomes or {}),
        "unfetchable_refused": list(result.unfetchable_refused),
        "unoffered_refused": list(result.unoffered_refused),
        "ratio_nudges": result.ratio_nudges,
        # Capped like every other receipt list: a finding body is not a log,
        # and the pages beyond the cap are still counted above.
        "manifest": MANIFEST.manifest_records(
            result.manifest_entries[:_RECEIPT_ITEM_CAP]
        ),
        "queries": dict(result.queries or {}),
    }
    if result.reference is None:
        return _no_commit(
            outcome, result,
            target_id=target_id, window_end=t0.isoformat(),
            detail="the lane produced no parseable reference",
            window_start=window_start.date(), window_t0=t0.date(),
        )

    # ---- the fences, over text the model cannot reach --------------------
    snippet_blob = " ‖ ".join(
        s for entry in plane.searches for s in (entry.get("snippets") or [])
    )
    survivors, stats = apply_fences(
        result.reference.get("ref_developments") or [],
        archive=archive,
        window_start=window_start.date(),
        window_end=t0.date(),
        tier3_dimensions=result.tier3_opened,
        snippet_blob=snippet_blob,
    )
    survivors = STORE.renumber(survivors, prefix)
    counts = dimension_counts(survivors, dimensions)
    thin = thin_from_counts(counts, thin_below)

    reference = dict(result.reference)
    reference["ref_developments"] = survivors
    bands = reference.get("ref_bands")
    if not isinstance(bands, dict) or not bands:
        bands = {d: "insufficient-basis" for d in dimensions}
    else:
        bands = {d: bands.get(d, "insufficient-basis") for d in dimensions}
    reference["ref_bands"] = bands
    reference["header"] = _header(
        reference.get("header"),
        target_id=target_id, country=name, code=code, t0=t0,
        window_start=window_start, model_name=model_name, result=result,
        stats=stats, plane=plane, thin=thin, counts=counts,
        tool_call_cap=tool_call_cap,
    )
    outcome["fences"] = stats.as_record()
    snippet_sourced = sum(
        1 for d in survivors if d.get("span_source") == "snippet"
    )
    outcome["snippet_sourced"] = snippet_sourced
    if snippet_sourced:
        # The consequence of the UNRECORDED LICENCE CLASSES, surfaced rather
        # than buried. `depth_for_license` puts every host without a reviewed
        # class at teaser depth, so its body is never stored — the span was
        # verified against the page in memory and then the bytes were dropped.
        # Those developments are real and they are correctly fenced; what they
        # are not is RE-VERIFIABLE after this process exits. Recording a licence
        # class for a host is an operator act, and this line is how they learn
        # it would buy something.
        outcome["warnings"].append(
            f"{target_id}: {snippet_sourced} of {len(survivors)} development(s) "
            "cite a host with no reviewed license_class, so their page bodies "
            "were NOT archived (research_evidence.depth_for_license => teaser). "
            "Their spans were verified in-run and are flagged span_source="
            "'snippet'; they cannot be re-verified later. Recording a "
            "license_class for those hosts in source_credibility is what makes "
            "them durably re-arguable."
        )
    outcome["per_dimension"] = counts
    outcome["thin_dimensions"] = thin
    outcome["n_developments"] = len(survivors)
    outcome["span_verified_rate"] = stats.span_verified_rate
    outcome["bands"] = bands
    outcome["sha256"] = STORE.canonical_sha256(reference)
    outcome["reference"] = reference

    if not survivors:
        if not int(stats.candidates or 0):
            # NOT ``all_rejected``: there was nothing to reject. The model
            # emitted a parseable object carrying no development at all, which
            # is the same failure as emitting nothing — a build that never
            # committed — and it is named as such so the next reader is sent to
            # the loop and the plane rather than to the fences.
            #
            # D6 SPLIT IT IN TWO, and the split is the whole diagnosis. If the
            # loop's OWN manifest held an in-window page with usable text when
            # that empty object arrived, the plane worked, the fences were never
            # reached, the evidence was rendered into the commit turn — and the
            # model wrote nothing anyway. That is not a no_commit and calling it
            # one sends every future reader to the search plane. It is
            # ``empty_commit_with_material``, counted, with its notes kept.
            material = MANIFEST.has_material(
                result.manifest_entries, window_start.date(), t0.date()
            )
            return _no_commit(
                outcome, result,
                target_id=target_id, window_end=t0.isoformat(),
                detail="the lane committed a reference with NO development",
                window_start=window_start.date(), window_t0=t0.date(),
                status=(
                    MANIFEST.EMPTY_COMMIT_WITH_MATERIAL if material
                    else "no_commit"
                ),
            )
        outcome["status"] = "all_rejected"
        outcome["warnings"].append(
            f"{target_id}: every one of {stats.candidates} committed "
            f"development failed a fence ({stats.as_record()['rejected']}). "
            "Nothing written — a reference with no development would make "
            "every claim graded against it `silent`."
        )
        return outcome

    if not write:
        outcome["status"] = "built_not_written"
        outcome["warnings"].append(
            f"{target_id}: reference_dry_run is set — built and fenced, NOT "
            "written to unit_references. The reference travels on this "
            "receipt's data.per_target[].reference."
        )
        return outcome

    async with pool.acquire() as conn:
        reference_id, derived = await STORE.write_reference(
            conn,
            target_id=target_id,
            reference=reference,
            window_start=window_start,
            window_end=t0,
            built_at=datetime.now(timezone.utc),
            builder=BUILDER_LABEL,
            sha256=outcome["sha256"],
            rate=stats.span_verified_rate,
            thin_below=thin_below,
        )
    outcome["derived"] = derived
    if reference_id is None:
        outcome["status"] = "duplicate"
        outcome["warnings"].append(
            f"{target_id}: a reference with this exact sha256 is already "
            "loaded for this target. Nothing written."
        )
        return outcome
    outcome["reference_id"] = str(reference_id)
    # A committed reference supersedes any carry: leaving it would seed the
    # NEXT window from the last one's leads.
    NOTES.clear(target_id)
    return outcome


def _no_commit(
    outcome: dict[str, Any],
    result: Any,
    *,
    target_id: str,
    window_end: str,
    detail: str,
    window_start: Any = None,
    window_t0: Any = None,
    status: str = "no_commit",
) -> dict[str, Any]:
    """Name the build that committed nothing, and keep what it learned.

    One exit for three shapes of the same surface fact — an unparseable reply, a
    parseable object with an empty ``ref_developments``, and that same empty
    object arriving while the loop held in-window material — because the
    bookkeeping is identical (nothing written, the notes kept, the attempt
    recorded as a failure) even though the third points somewhere the other two
    do not. ``status`` is what separates them on the row.
    """
    outcome["status"] = status
    # A rejection table on a build that committed nothing is the 00:43Z receipt's
    # own lie — seven zeros under the word "rejected" sent its reader to the
    # fences. Nothing was fenced, so nothing is reported as fenced.
    outcome.pop("fences", None)
    outcome["no_commit_reason"] = result.stop_reason
    outcome["commit_trigger"] = result.commit_trigger
    # D6 — THE CARRY NO LONGER DEPENDS ON THE MODEL. The pages the loop read are
    # carried beside whatever NOTE lines the model wrote, so a build that ends
    # ``no_notes`` still leaves the next attempt its leads.
    carried = NOTES.save(
        target_id, result.notes,
        window_end=window_end,
        stop_reason=result.stop_reason,
        commit_trigger=result.commit_trigger,
        pages=MANIFEST.carry_lines(
            result.manifest_entries, window_start, window_t0
        ),
    )
    outcome["notes_carried"] = bool(carried)
    if carried:
        outcome["notes_carried_lines"] = carried.get("note_lines")
        outcome["pages_carried_lines"] = carried.get("page_lines")
    wall = f"{result.wall_seconds:.0f}s of {result.build_max_seconds:.0f}s"
    tokens = f"{result.total_tokens():,} of {result.build_max_tokens:,}"
    if status == MANIFEST.EMPTY_COMMIT_WITH_MATERIAL:
        in_window = MANIFEST.in_window_entries(
            result.manifest_entries, window_start, window_t0
        )
        outcome["warnings"].append(
            f"{target_id}: EMPTY COMMIT WITH MATERIAL — the model committed a "
            f"reference with NO development while {len(in_window)} page(s) it "
            "had read, dated inside the window and quotable, were rendered "
            "into its commit turn: "
            + ", ".join(e.url for e in in_window[:_RECEIPT_ITEM_CAP])
            + f". {result.tool_calls} tool calls, {wall}, {tokens} tokens. "
            "Nothing written. This is NOT `no_commit` and NOT `all_rejected`: "
            "the plane answered, the pages were read, the fences were never "
            "reached. It is a MODEL failure, and it is counted as one."
        )
        return outcome
    outcome["warnings"].append(
        f"{target_id}: NO COMMIT — {detail} "
        f"(stop_reason={result.stop_reason}"
        + (f", commit_trigger={result.commit_trigger}"
           if result.commit_trigger else "")
        + (", search_degraded" if result.search_degraded else "")
        + f") after {result.tool_calls} tool calls, {wall}, {tokens} tokens. "
        "Nothing written. This is NOT `all_rejected`: no development was "
        "committed, so no fence rejected one."
        + (
            f" The {carried.get('note_lines')} NOTE line(s) it did accumulate "
            "are kept for the next attempt on this target."
            if carried else
            " It accumulated no NOTE lines, so there is nothing to carry."
        )
    )
    return outcome


def _header(
    existing: Any,
    *,
    target_id: str,
    country: str,
    code: str,
    t0: datetime,
    window_start: datetime,
    model_name: str,
    result: Any,
    stats: Any,
    plane: ToolPlane,
    thin: Sequence[str],
    counts: Mapping[str, int],
    tool_call_cap: int,
) -> dict[str, Any]:
    """The reference's own provenance block — the cost travels with the artefact.

    Everything a later reader needs to decide whether to trust this reference,
    in the reference: which model, which fences, how many candidates survived
    them and why the rest did not, what it cost in tokens and wall time, and
    which dimensions came out thin. ``ref_IL_A.json`` could not be re-verified
    because its lane shipped no archive and no counts; this one ships both.
    """
    header = dict(existing) if isinstance(existing, Mapping) else {}
    header.update({
        "target_id": target_id,
        "country": country,
        "country_code": code,
        "t0": t0.isoformat(),
        "window": f"{window_start.isoformat()} -> {t0.isoformat()}",
        "builder": BUILDER_LABEL,
        "pipeline_version": BUILDER_PIPELINE_VERSION,
        "model": model_name,
        "served_by": "core plane (self-hosted); $0 on any paid API",
        "built_at": datetime.now(timezone.utc).replace(
            microsecond=0).isoformat(),
        "fetch_tools": (
            "web_search + fetch_page through the web_access action pack "
            "(SSRF-guarded egress, governed invocations); page text archived "
            "content-addressed; spans checked as exact substrings of the "
            "archived text"
        ),
        "instruction_source": (
            "planning/PROOF_ROUND_2026-09-12/stage1_IL.md, re-anchored per "
            "country/window (_reference_instruction.build_instruction)"
        ),
        "fences": stats.as_record(),
        "span_verified_rate": stats.span_verified_rate,
        "thin_dimensions": list(thin),
        "per_dimension_counts": dict(counts),
        "tier3_opened_for": list(result.tier3_opened),
        "cost": {
            "paid_api_usd": 0.0,
            "plane": "core",
            "prompt_tokens": int(result.usage.get("prompt_tokens", 0)),
            "completion_tokens": int(result.usage.get("completion_tokens", 0)),
            "total_tokens": int(result.usage.get("total_tokens", 0)),
            "model_rounds": result.rounds,
            "tool_calls": result.tool_calls,
            "tool_call_cap": tool_call_cap,
            "searches": len(plane.searches),
            "fetches": len(plane.fetches),
            "wall_seconds": result.wall_seconds,
        },
        "stop_reason": result.stop_reason,
    })
    return header


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------


def build_receipt(
    *,
    t0: datetime,
    enabled: bool,
    roster_size: int,
    due: Sequence[Mapping[str, Any]],
    per_target: Sequence[Mapping[str, Any]],
    warnings: Sequence[str],
    cadence_days: int,
    window_days: int,
    dry_run: bool,
    selected: Sequence[Mapping[str, Any]] = (),
    attempts_now: Mapping[str, Any] | None = None,
    unbuildable: Sequence[str] = (),
    backoff_hours: float = 0.0,
    backoff_source: str = "default",
) -> FindingPayload:
    """The lane's own row — a counting-not-repairing receipt.

    It never gates and never alerts. The REFERENCES are side-written to
    ``unit_references``; this is the lane's watchable heartbeat, and it carries
    the caveat on its own face so a reader who sees only this row sees the
    limits with the numbers.
    """
    written = [t for t in per_target if t.get("status") == "built"]
    backed_off = [d for d in due if not d.get("eligible", True)]
    if not enabled:
        headline = f"reference builder DISABLED ({ENABLED_ENV} is off)"
    elif not per_target and due and len(backed_off) == len(due):
        # R2-FIX(2) — the one case that must never print as "no target due".
        # Every due target has failed inside the backoff window, so the lane is
        # correctly spending NOTHING; a receipt saying "nothing due" would send
        # its reader to the cadence, which is not where the fault is.
        headline = (
            f"every one of {len(due)} due target(s) is in retry backoff "
            f"({backoff_hours:g}h) — nothing built, nothing spent"
        )
    elif not per_target:
        headline = (
            f"no target due (roster {roster_size}, cadence {cadence_days}d)"
            if roster_size else "no roster — no country desk has produced recently"
        )
    elif written:
        total = sum(int(t.get("n_developments") or 0) for t in written)
        headline = (
            f"built {len(written)} reference(s), {total} verified development(s)"
        )
    else:
        statuses = sorted({str(t.get("status")) for t in per_target})
        headline = f"built no reference ({', '.join(statuses)})"

    body = [
        f"Reference builder — {headline}.",
        f"  t0={t0.isoformat()} window={window_days}d cadence={cadence_days}d "
        f"roster={roster_size} due={len(due)} pipeline={BUILDER_PIPELINE_VERSION}",
        f"  plane=core builder={BUILDER_LABEL} paid_api=$0.00"
        + ("  DRY RUN (nothing written)" if dry_run else ""),
    ]
    # R2-FIX(2) — WHY THIS TARGET, AND WHO IS BEHIND IT. The 05:20Z receipt
    # named the target it built and nothing about the choice, so two identical
    # receipts for the same unbuildable country read as a coincidence rather
    # than as a scheduler that could not move on. The queue is now on the row:
    # what was chosen and on what ground, then the next three with theirs.
    if selected:
        for entry in selected[:_RECEIPT_ITEM_CAP]:
            body.append(f"  CHOSE {ROSTER.queue_line(entry)}")
    chosen_ids = {str(e.get("target_id")) for e in selected}
    behind = [d for d in due if str(d.get("target_id")) not in chosen_ids]
    for entry in behind[:_QUEUE_PREVIEW]:
        body.append(f"  next  {ROSTER.queue_line(entry)}")
    if len(behind) > _QUEUE_PREVIEW:
        body.append(
            f"  ... and {len(behind) - _QUEUE_PREVIEW} more due target(s); "
            f"backoff {backoff_hours:g}h from {backoff_source}"
        )
    if unbuildable:
        body.append(
            f"  UNBUILDABLE BY THIS LANE after "
            f"{ROSTER.UNBUILDABLE_AFTER_FAILURES} consecutive failures: "
            + ", ".join(list(unbuildable)[:_RECEIPT_ITEM_CAP])
            + ". Still retried every "
            f"{backoff_hours:g}h and never dropped. An operator top-up is the "
            "route that closes them: scripts/reference_topup_packet.py "
            "--candidates"
        )
    for target in per_target[:_RECEIPT_ITEM_CAP]:
        fences = target.get("fences") or {}
        body.append(
            f"  - [{target.get('status')}] {target.get('target_id')} "
            f"({target.get('country')}): "
            f"{target.get('n_developments', 0)} verified of "
            f"{fences.get('candidates', 0)} committed, rate="
            + (
                "—" if target.get("span_verified_rate") is None
                else f"{float(target['span_verified_rate']) * 100:.1f}%"
            )
        )
        # D3 — the no-commit line names what stopped the build. Without it a
        # reader sees "0 verified of 0 committed" and has nothing to act on;
        # the 00:43Z receipt printed a seven-zero rejection table instead,
        # which pointed at the fences when the fault was the search plane.
        if target.get("status") in (
            "no_commit", MANIFEST.EMPTY_COMMIT_WITH_MATERIAL
        ):
            loop_rec = target.get("loop") or {}
            parts = [f"stop_reason={target.get('no_commit_reason') or '—'}"]
            if target.get("commit_trigger"):
                parts.append(f"forced_commit={target['commit_trigger']}")
            if loop_rec.get("search_degraded"):
                parts.append("search_degraded")
            parts.append(
                f"cap={loop_rec.get('build_max_seconds', 0):.0f}s/"
                f"{int(loop_rec.get('build_max_tokens', 0)):,}tok"
            )
            parts.append(
                "notes_carried" if target.get("notes_carried") else "no_notes"
            )
            body.append(f"      {target.get('status')}: " + " ".join(parts))
        # D6 — WHAT THE LOOP READ, on every row. A build's yield is decided by
        # how many in-window pages it got in front of the model, and until this
        # line existed an operator could not see that number without opening the
        # trace.
        loop_rec = target.get("loop") or {}
        if loop_rec:
            pages = (
                f"      pages: {loop_rec.get('manifest_pages', 0)} read, "
                f"{loop_rec.get('manifest_in_window', 0)} in window; "
                f"{loop_rec.get('searches', 0)} searches / "
                f"{loop_rec.get('fetches', 0)} fetches"
            )
            # WHY the fetches ended, whenever any of them ended badly. A
            # timed-out fetch is a page that EXISTS and that this lane failed
            # to reach; until this printed, a build that read 0 pages because
            # every host hung looked exactly like one whose sources refused it.
            outcomes = loop_rec.get("fetch_outcomes") or {}
            spent = [
                f"{outcomes[k]} {label}"
                for k, label in (
                    ("timed_out", "timed out"), ("blocked", "blocked"),
                    ("stub", "stub"), ("refused", "refused"),
                )
                if outcomes.get(k)
            ]
            if spent:
                pages += " (" + ", ".join(spent) + ")"
            extras = []
            if loop_rec.get("url_repairs"):
                extras.append(f"{loop_rec['url_repairs']} url repaired")
            if (loop_rec.get("queries") or {}).get("queries_rewritten"):
                extras.append(
                    f"{loop_rec['queries']['queries_rewritten']} queries "
                    "rewritten"
                )
            if loop_rec.get("unfetchable_refused"):
                extras.append(
                    "unfetchable refused: "
                    + ", ".join(loop_rec["unfetchable_refused"][:4])
                )
            if loop_rec.get("unoffered_refused"):
                extras.append(
                    "composed URLs refused: "
                    + ", ".join(loop_rec["unoffered_refused"][:4])
                )
            if loop_rec.get("ratio_nudges"):
                extras.append(f"{loop_rec['ratio_nudges']} ratio nudge(s)")
            body.append(pages + ("; " + "; ".join(extras) if extras else ""))
        rejected = {k: v for k, v in (fences.get("rejected") or {}).items() if v}
        if rejected:
            body.append(
                "      rejected: "
                + ", ".join(f"{k}={v}" for k, v in sorted(rejected.items()))
            )
        if target.get("thin_dimensions"):
            body.append(
                f"      THIN: {', '.join(target['thin_dimensions'])}"
            )
        loop = target.get("loop") or {}
        usage = loop.get("usage") or {}
        if usage:
            body.append(
                f"      {loop.get('tool_calls', 0)} tool calls, "
                f"{int(usage.get('prompt_tokens', 0)):,} in / "
                f"{int(usage.get('completion_tokens', 0)):,} out, "
                f"{loop.get('wall_seconds', 0)}s"
            )
    for warning in list(warnings)[:_RECEIPT_ITEM_CAP]:
        body.append(f"  WARNING: {warning}")

    return FindingPayload(
        title=f"Reference builder — {headline}"[:2048],
        body="\n".join(body)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME, "correctness_measurement",
              "severity:low"],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "pipeline_version": BUILDER_PIPELINE_VERSION,
            "builder": BUILDER_LABEL,
            "enabled": bool(enabled),
            "dry_run": bool(dry_run),
            "t0": t0.isoformat(),
            "window_days": window_days,
            "cadence_days": cadence_days,
            "roster_size": roster_size,
            "n_due": len(due),
            "n_eligible": sum(1 for d in due if d.get("eligible", True)),
            "n_retry_backoff": len(backed_off),
            "due_queue": [
                {k: v for k, v in d.items() if k != "dimensions"}
                for d in due[:_RECEIPT_ITEM_CAP]
            ],
            # R2-FIX(2) — THE ATTEMPT LEDGER. This object is the whole storage
            # design for "order by last attempt": the next tick reads it back
            # out of this row (``_reference_roster.ATTEMPTS_SQL``) and needs no
            # table of its own. Three scalars per target, capped.
            "attempts": dict(attempts_now or {}),
            "unbuildable_by_lane": list(unbuildable),
            "retry_backoff_hours": float(backoff_hours),
            "retry_backoff_source": backoff_source,
            "n_references_written": len(written),
            # D3 — COUNTED, not just named. A lane that builds nothing twice
            # running is a different fact from one that builds nothing once,
            # and the only way to see it from outside is for the number to be
            # on the row. ``n_no_commit`` counts builds that committed no
            # development at all; ``n_search_degraded`` counts builds whose
            # search plane stopped answering, whether or not that is what
            # stopped them.
            "n_no_commit": sum(
                1 for t in per_target if t.get("status") == "no_commit"
            ),
            "n_all_rejected": sum(
                1 for t in per_target if t.get("status") == "all_rejected"
            ),
            # D6 — the MODEL failure, counted separately from the plane's and
            # the fences'. A lane whose n_empty_commit_with_material climbs is
            # a lane whose instruction or whose model is the thing to change;
            # nothing else on this row says that.
            "n_empty_commit_with_material": sum(
                1 for t in per_target
                if t.get("status") == MANIFEST.EMPTY_COMMIT_WITH_MATERIAL
            ),
            "n_manifest_pages": sum(
                int((t.get("loop") or {}).get("manifest_pages", 0))
                for t in per_target
            ),
            "n_manifest_in_window": sum(
                int((t.get("loop") or {}).get("manifest_in_window", 0))
                for t in per_target
            ),
            "n_url_repairs": sum(
                int((t.get("loop") or {}).get("url_repairs", 0))
                for t in per_target
            ),
            "n_search_degraded": sum(
                1 for t in per_target
                if (t.get("loop") or {}).get("search_degraded")
            ),
            "n_forced_commit": sum(
                1 for t in per_target
                if (t.get("loop") or {}).get("commit_trigger") not in
                ("", "model", None)
            ),
            "cost": {
                "paid_api_usd": 0.0,
                "plane": "core",
                "prompt_tokens": sum(
                    int(((t.get("loop") or {}).get("usage") or {}).get(
                        "prompt_tokens", 0)) for t in per_target
                ),
                "completion_tokens": sum(
                    int(((t.get("loop") or {}).get("usage") or {}).get(
                        "completion_tokens", 0)) for t in per_target
                ),
                "wall_seconds": round(sum(
                    float((t.get("loop") or {}).get("wall_seconds", 0.0))
                    for t in per_target
                ), 1),
            },
            "per_target": [dict(t) for t in per_target],
            "warnings": list(warnings),
            "caveat": (
                "This reference is a SKELETON built by the free core plane "
                "under hard fences, not a complete account of the window. R1 "
                "measured the shape of what it misses: ~33% of a knowledgeable "
                "reader's major developments, with per-dimension counts "
                "flatter than the record. Every dimension carrying fewer than "
                "two verified developments is written to thin_dimensions, "
                "which the correctness grader reads — so a thin dimension "
                "under-claims rather than mis-grades. Closing the substance "
                "gap is an OPERATOR-TRIGGERED lane "
                "(scripts/reference_topup_packet.py), never a scheduled one, "
                "and nothing in this module can start it."
            ),
        },
    )


def _zero_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: Any, options: Mapping[str, Any], deps: Any
) -> AnalystMethodResult:
    """One reference-building tick: the most overdue target, at one stamp.

    REFUSES LOUD on a missing ``deps.pg_pool`` — the roster, the cadence and the
    write all live in the substrate, and a builder that cannot read it must not
    emit a clean-looking zero. Every OTHER missing plane DEGRADES to a named
    status on the receipt: no model, no web binding, no commit, everything
    rejected.

    ``inputs`` is the generic materialized slice the cadence actor hands every
    META analyst. It is IGNORED, and that is load-bearing: a reference built
    from the platform's own slice would not be independent of the reads it is
    used to grade, which is the one property that makes the number mean
    anything.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "reference_builder requires a live deps.pg_pool — refusing to "
            "report a reference-building run without reading the substrate"
        )

    t0 = resolve_t0(options)
    enabled = builder_enabled()
    explicit = _str_list(options.get("reference_targets"))
    cadence_days = _pos(options.get("cadence_days"), DEFAULT_CADENCE_DAYS)
    window_days = _pos(options.get("window_days"), DEFAULT_WINDOW_DAYS)
    max_targets = _pos(
        options.get("max_targets_per_run"), DEFAULT_MAX_TARGETS_PER_RUN
    )
    tool_call_cap = _pos(options.get("tool_call_cap"), DEFAULT_TOOL_CALL_CAP)
    thin_below = _pos(options.get("thin_below"), DEFAULT_THIN_BELOW)
    page_chars = _pos(
        options.get("page_chars_to_model"), DEFAULT_PAGE_CHARS_TO_MODEL
    )
    min_developments = _pos(options.get("min_developments"), 20)
    # D3/D4 — the two walls, settable so an operator can shrink a build on a
    # loaded box and never widen one past what the plane can carry.
    build_max_seconds = _pos(
        options.get("build_max_seconds"), DEFAULT_BUILD_MAX_SECONDS
    )
    build_max_tokens = _pos(
        options.get("build_max_tokens"), DEFAULT_BUILD_MAX_TOKENS
    )
    roster_window = _pos(
        options.get("roster_window_days"), DEFAULT_ROSTER_WINDOW_DAYS
    )
    dry_run = _flag(options.get("reference_dry_run"))
    # R2-FIX(2) — the retry fence. Env FIRST (see RETRY_BACKOFF_ENV on why the
    # precedence is inverted relative to the two build walls); the source string
    # travels on the receipt so a reader never has to guess which one bit.
    backoff_hours, backoff_source = ROSTER.retry_backoff_hours(
        options.get("retry_backoff_hours")
    )

    if not enabled:
        return AnalystMethodResult(
            finding=build_receipt(
                t0=t0, enabled=False, roster_size=0, due=(), per_target=(),
                warnings=(
                    f"{ENABLED_ENV} is off — nothing searched, nothing "
                    "fetched, no model call, nothing written.",
                ),
                cadence_days=cadence_days, window_days=window_days,
                dry_run=dry_run, backoff_hours=backoff_hours,
                backoff_source=backoff_source,
            ),
            usage=_zero_usage(),
        )

    from . import scorecard_banding as banding
    from .correctness_grader import PROLIFERATION

    dimensions = tuple(banding.DIMENSIONS) + (PROLIFERATION,)

    extras = dict(getattr(deps, "extras", None) or {})
    llm = extras.get(LLM_DEPS_EXTRA_KEY)
    binding = extras.get(WEB_BINDING_DEPS_EXTRA_KEY)

    warnings: list[str] = []
    async with pool.acquire() as conn:
        roster = await resolve_roster(
            conn, t0=t0, dimensions=dimensions,
            roster_window_days=roster_window,
        )
        # R2-FIX(2) — the lane's own history, read back off its own receipts.
        # One indexed SELECT; unreadable degrades to {} and the old
        # most-overdue-first order, which is a worse schedule but still one.
        attempts = await ROSTER.load_attempts(conn, t0=t0)
        if explicit:
            missing = [t for t in explicit if t not in roster]
            if missing:
                warnings.append(
                    "named target(s) not in the live roster (no dimension desk "
                    f"has produced in {roster_window} days): {', '.join(missing)} "
                    "— building anyway, over the full dimension set."
                )
            selected = ROSTER.named_targets(
                explicit, roster, dimensions=dimensions, attempts=attempts
            )
            due = selected
            if len(selected) > max_targets:
                # Never silent. An operator who named three targets and got one
                # would reasonably conclude the other two failed.
                warnings.append(
                    f"{len(selected)} target(s) were named but "
                    f"max_targets_per_run is {max_targets}, so only "
                    f"{', '.join(t['target_id'] for t in selected[:max_targets])}"
                    " will be built this run. One build is ~8 minutes of core "
                    "plane; raise max_targets_per_run, or force the rest in "
                    "separate runs."
                )
        else:
            due = await due_targets(
                conn, roster, t0=t0, cadence_days=cadence_days,
                attempts=attempts, backoff_hours=backoff_hours,
            )
            # THE SELECTION IS FROM THE ELIGIBLE HEAD, not from the front of the
            # list. A backed-off target stays in ``due`` so the receipt can say
            # why it was passed over; what it does not get is the hour.
            selected = [d for d in due if d.get("eligible", True)][:max_targets]
        identities = await target_identity(
            conn, [t["target_id"] for t in selected] or ["-"]
        )

    per_target: list[dict[str, Any]] = []
    for entry in selected[:max_targets]:
        archive = ReferenceArchive.open_default()
        outcome = await build_reference(
            pool=pool,
            llm=llm,
            binding=binding,
            target_id=entry["target_id"],
            identity=identities.get(entry["target_id"], {}),
            dimensions=entry["dimensions"] or list(dimensions),
            t0=t0,
            window_days=window_days,
            tool_call_cap=tool_call_cap,
            thin_below=thin_below,
            page_chars_to_model=page_chars,
            min_developments=min_developments,
            temperature=DEFAULT_TEMPERATURE,
            archive=archive,
            write=not dry_run,
            build_max_seconds=build_max_seconds,
            build_max_tokens=build_max_tokens,
        )
        outcome["last_built_at"] = entry.get("last_built_at")
        outcome["age_days"] = entry.get("age_days")
        per_target.append(outcome)
        warnings.extend(outcome.get("warnings") or [])

    # R2-FIX(2) — the ledger AFTER this run, so a third strike is named on the
    # tick that earns it rather than one tick later.
    attempts_now = ROSTER.attempt_records(per_target, at=t0)
    projected = ROSTER.apply_outcomes(attempts, per_target, at=t0)
    unbuildable = ROSTER.unbuildable_targets(projected)
    # THE FLAG IS STANDING STATE; THE WARNING IS AN EVENT. `unbuildable_by_lane`
    # stays on every receipt and on every due-queue entry, because an operator
    # reading any single row should see which countries this lane cannot build.
    # The paragraph below is only written for a target THIS RUN attempted —
    # naming it on the tick that earns the strike, and again on each later
    # failure, which the backoff paces to about once a day. Repeating it hourly
    # for ever would turn the one warning that names a route to take into the
    # line every reader learns to skip.
    for target_id in [t for t in unbuildable if t in attempts_now]:
        record = projected[target_id]
        warnings.append(
            f"{target_id}: UNBUILDABLE BY THIS LANE — "
            f"{record.consecutive_failures} consecutive failed build(s), most "
            f"recently {record.last_status}. It is NOT dropped: it stays on the "
            f"roster and is retried every {backoff_hours:g}h "
            f"({backoff_source}), because \"this lane cannot fetch it\" is a "
            "statement about today's fetchers and today's licence classes, both "
            "of which change. What closes it today is an operator top-up: "
            "scripts/reference_topup_packet.py --candidates."
        )
    if not selected and roster and not due:
        warnings.append(
            f"every one of the {len(roster)} roster targets carries a "
            f"reference newer than the {cadence_days}-day cadence. Nothing "
            "built, nothing spent."
        )
    if not selected and due:
        soonest = min(
            (str(d.get("retry_after")) for d in due if d.get("retry_after")),
            default="—",
        )
        warnings.append(
            f"all {len(due)} due target(s) are inside the "
            f"{backoff_hours:g}h retry backoff ({backoff_source}); the first "
            f"comes back at {soonest}. Nothing built, nothing spent. This is "
            "the fence working — a lane that retried a failing target every "
            "hour is what it replaced — but a roster where EVERY target is "
            "backed off means the failures are the plane's, not the targets'."
        )
    if not roster:
        warnings.append(
            "no target has a dimension desk that produced in the last "
            f"{roster_window} days — the roster is empty and there is nothing "
            "to build a reference for."
        )
    gap = coverage_warning(cadence_days)
    if gap:
        warnings.append(gap)

    total_usage = {
        "prompt_tokens": sum(
            int(((t.get("loop") or {}).get("usage") or {}).get("prompt_tokens", 0))
            for t in per_target
        ),
        "completion_tokens": sum(
            int(((t.get("loop") or {}).get("usage") or {}).get(
                "completion_tokens", 0))
            for t in per_target
        ),
        "reasoning_tokens": sum(
            int(((t.get("loop") or {}).get("usage") or {}).get(
                "reasoning_tokens", 0))
            for t in per_target
        ),
    }
    return AnalystMethodResult(
        finding=build_receipt(
            t0=t0, enabled=True, roster_size=len(roster), due=due,
            per_target=per_target, warnings=warnings,
            cadence_days=cadence_days, window_days=window_days,
            dry_run=dry_run, selected=selected, attempts_now=attempts_now,
            unbuildable=unbuildable, backoff_hours=backoff_hours,
            backoff_source=backoff_source,
        ),
        usage=total_usage,
    )


__all__ = [
    "BUILDER_LABEL",
    "BUILDER_LABEL_TOPPED_UP",
    "BUILDER_PIPELINE_VERSION",
    "DEFAULT_CADENCE_DAYS",
    "DEFAULT_MAX_TARGETS_PER_RUN",
    "DEFAULT_RETRY_BACKOFF_HOURS",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_THIN_BELOW",
    "DEFAULT_TOOL_CALL_CAP",
    "DEFAULT_WINDOW_DAYS",
    "ENABLED_ENV",
    "LLM_DEPS_EXTRA_KEY",
    "RETRY_BACKOFF_ENV",
    "SUB_HANDLER_NAME",
    "UNBUILDABLE_AFTER_FAILURES",
    "WEB_BINDING_DEPS_EXTRA_KEY",
    "build_receipt",
    "build_reference",
    "builder_enabled",
    "coverage_warning",
    "grader_grace_days",
    "due_targets",
    "handle",
    "resolve_roster",
    "resolve_t0",
    "target_identity",
]
