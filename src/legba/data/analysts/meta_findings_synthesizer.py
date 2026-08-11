# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-172 meta_findings_synthesizer analyst kind.

Reads OTHER analysts' first-order outputs (rows in ``analyst_outputs`` with
``kind == 'finding'``) and synthesizes them into a second-order
:class:`FindingPayload` marked ``data["meta"] = True``. The substrate-write
wrapper stamps ``derived_from`` with the contributing finding UUIDs so the
lineage walker can backtrack one hop to the first-order findings (and two
hops to the underlying signals).

Per ``plans/design/legba_kind_contracts.md`` §5 (analyst kind contract) and
``plans/design/legba_topology_redesign.md`` §5.3::

    Reads:  other analysts' outputs only (NOT raw substrate signals).
    Method: narrower-context LLM — synthesizing already-structured findings
            into higher-order narratives.
    Writes: second-order findings (``FindingPayload`` with ``data.meta=True``
            and ``data.contributing_analysts=[...]``; ``derived_from`` is
            populated by the substrate-write wrapper from the UUID list this
            run returns on :class:`AnalystMethodResult.derived_from`).

The module conforms to the package shape declared in
:mod:`legba.data.analysts`: ``KIND_NAME`` + ``run_method`` +
``build_prompt_module``. It is the sibling of ``inline_target`` and
``cross_target_raw``; the analyst-actor layer in
:mod:`legba.runtime.dapr_actors` treats all three interchangeably.

Subscription / read-side
~~~~~~~~~~~~~~~~~~~~~~~~

The analyst descriptor expresses *which* other analysts feed this synth via
:class:`legba.data.schemas.analyst.SubscriptionAnalyst` entries on
``subscription.other_analysts`` (per L-101 §4). The runtime resolves those
to a concrete ``analyst_id`` set and either (a) calls
:func:`read_other_analyst_findings` itself before invoking ``run_method``,
or (b) passes ``options['source_analyst_ids']`` so this module can validate
the rows came from the expected set. We accept both pathways: if rows are
already supplied in ``inputs`` we use them; the helper exists so a downstream
caller (registry-side resolution, planner-side replay, or the optimizer's
trace-driven re-evaluation) can build the slice in isolation.

Token budget
~~~~~~~~~~~~

Narrower than the LLM kinds that read raw substrate (``inline_target`` at
``max_tokens=1024``, ``cross_target_raw`` at ``1536``). Findings are already
structured — title, body, evidence, confidence — so per-input prompt
footprint is smaller AND the synthesis output is itself a single tight
second-order claim, not a verbose first-order one. Default
``max_tokens=768`` for completions; cap inputs at ``15`` findings.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from uuid import UUID

import asyncpg

from ..provenance.consumption import (
    CONSUMPTION_CONTEXT_BASIS,
    CONSUMPTION_CONTEXT_PERIPHERY,
)
from ._llm_budget import CHARS_PER_TOKEN, budget_chars
from .claim_contradiction import detect_contradictions, render_tension_block
from ..provenance.models import FindingPayload
from ...runtime.analyst_method import AnalystMethodResult, LLMHandlerLike

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


KIND_NAME: str = "meta_findings_synthesizer"
SCHEMA_VERSION: str = "legba/analyst.meta_findings_synthesizer/1-0-0"
HANDLER_VERSION: str = "0.1.0"
PROMPT_MODULE_PATH: str = "legba.prompts.meta_findings_synthesizer.v1"

# OUTPUT_KIND is the canonical analyst-output kind the runtime writes the
# synthesis as. We use FINDING (per the integration spec) so the output
# behaves as a structured finding row — the kind tags itself with
# ``meta:true`` in payload.data so the substrate is queryable on the
# second-order vs first-order distinction without needing a separate kind.
from ..provenance.kinds import OutputKind as _OutputKind  # noqa: E402

OUTPUT_KIND: _OutputKind = _OutputKind.FINDING


# Narrower context defaults — findings are already structured, so the
# per-input render cost is much lower than for raw signals AND the desired
# output is one tight second-order claim, not a verbose first-order finding.
DEFAULT_MAX_TOKENS: int = 768
"""Completion budget for the synthesis call. Smaller than inline_target's
1024 / cross_target_raw's 1536 because the output is a single second-order
synthesis claim, not a new finding from raw text."""

DEFAULT_TEMPERATURE: float = 0.2
"""Same as the sibling LLM kinds — synthesis still wants determinism."""

MAX_INPUT_FINDINGS: int = 15
"""Cap on how many first-order findings get rendered into the prompt for a
PER-COUNTRY composition. Findings are denser than signals; 15 of them at ~600
chars each fits the narrower context budget. A per-country read fuses only its
own ~7 unit heads, so this cap never actually bites there."""

MAX_WORLD_INPUT_FINDINGS: int = 64
"""Cap for the WORLD/global read (no ``target_id`` stamp). Its slice is folded to
exactly ONE head per (analyst, target) by ``DISTINCT ON (analyst_id,
target_id)``, so the natural input count IS the source roster — the cap must stay
>= the roster or the world composition silently drops inputs. The P4 pre-push
review (C2) found the 15-cap fused a "Global" read WITHOUT the United States. S2-T3
repointed the world read over the FIVE region heads (5-6 inputs), so the cap no
longer bites in the happy path; it still guards the DEGRADE path, where a region
with no region head falls back to its ~4-6 member country heads (worst case all
five regions degrade → ~24 country heads, still well under 64). ``_orient`` warns
if it ever trims on the world path (a dropped input == a region/country the world
read cannot see)."""

MAX_TITLE_CHARS: int = 200
MAX_BODY_CHARS: int = 600
"""FLOOR on the per-input body excerpt — it used to be the ceiling.

F-D (2026-08-03): this constant, times :data:`MAX_INPUT_FINDINGS`, WAS the
composition's entire input window. 15 x 600 chars is roughly 2,250 estimated
tokens against the 32,000-token budget the UNIT path packs against — about 7%.
The tier meant to see ACROSS desks saw less of its inputs than any leaf saw of
its signals. The excerpt is now sized from the shared budget by
:func:`composition_body_cap`; this stays as the never-go-below floor, so no path
can render less than the historical excerpt."""

MAX_EVIDENCE_ITEMS: int = 3

#: F-D — ceiling on ONE input finding's body excerpt however much budget is free.
#: Composition inputs are FINDINGS, not articles: live bodies average ~1.2-2.4k
#: chars across every producing desk, so this holds essentially all of them whole
#: while still bounding a pathological row.
MAX_FULL_BODY_CHARS: int = 4000

#: F-D — the share of the input-token budget the FINDINGS BLOCK may claim. The
#: rest of the turn is the system prompt, the grounding preamble, the periphery
#: and continuity blocks, the contested sidecar and the freshness advisory — all
#: separately bounded, all spliced around this block. Half is deliberately
#: conservative: the point is to stop reading through a keyhole, not to fill the
#: window.
COMPOSITION_SLICE_BUDGET_SHARE: float = 0.5

# P3-T3/T7 — how much of a cited sub-claim's body to capture on its citation as
# ``evidence_text`` at synth time, so the composition faithfulness VERIFY (run in
# a LATER actor step) checks each composed clause against the EXACT point-in-time
# evidence the model saw — no verify-time re-fetch (which could read a superseded
# sub-claim).
#
# F-D (2026-08-03): raised 600 -> 3600, matching the UNIT judge's whole-evidence
# bound (``verify._EVIDENCE_TOTAL_CHARS``). At 600 the cap BOUND on essentially
# every composition citation in production — measured read-only on the live
# substrate, country_composition citations averaged 567 of 600 chars against
# cited bodies averaging 2,352 — so the judge graded the whole composition tower
# against roughly the first quarter of each sub-claim, and a composed clause
# resting on anything the cited finding said after its BLUF read as ungrounded.
# The composition citation now carries the same evidence window a unit citation
# does. (The 08-03 panel reported this as "0 of 551 citations carry resolvable
# text"; that measurement read ``source_text``/``snippet``/``body``, which is the
# UNIT citation shape. Composition citations carry ``evidence_text`` and 542 of
# 542 had it — the defect was never absence, it was the width of the window.)
MAX_EVIDENCE_TEXT_CHARS: int = 3600


# F-1 (MASTER_PLAN 2026-07-13) — COMPOSE-TIME HEAD RE-RESOLUTION (freshness).
#
# The direct inputs a composition reads are ALWAYS current heads (the deduped
# read gates ``f.superseded_by IS NULL``). But a composition head freezes its
# lower-tier CITATIONS at its own tick: if a sub-finding it cited later REVERSES
# (is superseded by a materially different current head), that reversal does not
# propagate up until every intervening tier re-composes. The Italy staleness race
# (2026-07-13): escalation ``ed158597`` (conf 0.90, "expulsions drive escalation
# risk") reversed to ``f0cd1c87`` (conf 0.30, "no signs of near-term escalation")
# at 00:44; the country→region→world heads composed before the propagation caught
# up, so the world assessment cited the SUPERSEDED high-escalation reading.
#
# The fix: at compose time, walk each input head's ``derived_from`` lineage
# (bounded) and flag any sub-finding SUPERSEDED by a materially-different head
# AFTER the citing tier composed (a genuine post-hoc reversal, not routine
# re-run churn). Surface the flags as a directive FRESHNESS ADVISORY prepended to
# the prompt (the model demotes/caveats the stale framing) + a trace ledger.
# Strictly ADDITIVE and FAIL-SAFE — a freshness-pass error never breaks a compose.
FRESHNESS_MAX_DEPTH: int = 4
"""How many lineage hops down from an input head the freshness walk descends
(world → region → country → unit reaches the unit findings at depth 3)."""

FRESHNESS_MAX_NODES: int = 400
"""Hard cap on total lineage findings the walk fetches per slice (bounds cost;
signal/fact lineage ids never match the ``kind='finding'`` fetch and drop out)."""

FRESHNESS_MATERIAL_CONF_DELTA: float = 0.25
"""A superseded sub-finding is a MATERIAL stale-root only if its current
successor's confidence differs by at least this much — filters routine re-run
churn (stable confidence) from genuine reversals (Italy was 0.90 → 0.30)."""

FRESHNESS_MAX_ADVISORY: int = 6
"""Cap on the compact per-target advisory rendered into the prompt (the full,
per-(unit,target) ledger still lands in the trace ``data.freshness``)."""


# P3 per-COUNTRY composition — verify-floor gate.
#
# When this synth runs TARGET-SCOPED (a per-country composition descriptor with a
# ``subscription.targets`` block → the runtime fans out one worker per G20 target
# with ``target_filter`` set), the source-finding slice is restricted to
# sub-claims that PASSED the faithfulness-verify pass above this floor. The floor
# compares against ``effective_confidence = min(finding.confidence,
# faithfulness_score)`` — the SAME fold the read API surfaces
# (``substrate_reads_api._hydrate_finding``). A sub-claim with NO faithfulness
# critique (verify never ran) is excluded by the INNER JOIN; a verify-FAILED one
# is excluded by the floor; an ``unstructured`` / ``coerce_failed`` coerce-fallback
# is excluded by tag. GLOBAL meta runs (no target binding, ``target_filter=None``)
# are UNAFFECTED — they keep the legacy cross-target, unfiltered read.
#
# Default 0.0 = "floor-0 gate": the gate STRUCTURE is wired (verify must have run
# and produced a structured, non-failed finding) but the numeric bar starts at
# the loosest admissible value so nothing verified is silently dropped by an
# un-calibrated threshold. Raise it OPS-side via LEGBA_COMPOSITION_VERIFY_FLOOR
# (no schema change / registry rebuild) once the score distribution is calibrated.
DEFAULT_VERIFY_FLOOR: float = 0.0
"""Minimum ``effective_confidence`` a verified sub-claim must clear to enter the
per-country composition slice. Env-overridable via ``LEGBA_COMPOSITION_VERIFY_FLOOR``."""

VERIFY_FLOOR_ENV: str = "LEGBA_COMPOSITION_VERIFY_FLOOR"


# C-TIER (2026-07) — TWO-TIER composition evidence: BASIS + PERIPHERY.
#
# The operator's direction verbatim: "can we not include but properly weight or
# separate it from others. Like even, conflicting points. Don't want to lose
# real signal but want to distill it." Neither of the two prior behaviors does
# that: a HARD floor silently DROPS below-floor findings (signal lost), while
# the default floor-0 gate lets them BLEND indistinguishably into the basis
# evidence (signal laundered). The two-tier split keeps both honest:
#
#   * BASIS     — verify-passed sub-claims with ``effective_confidence =
#                 min(confidence, faithfulness) >= the floor``. Rendered exactly
#                 as today: the load-bearing evidence the composition may cite
#                 as established.
#   * PERIPHERY — sub-claims the basis bar EXCLUDED: verify-scored BELOW the
#                 floor, or never verified at all (claim-bearing but ungraded).
#                 Coerce-fallback garbage stays excluded outright (not
#                 claim-bearing). Capped (worst-first: severity, then recency)
#                 and rendered under an explicit delimited section that requires
#                 hedged attribution and asks for conflicts with the basis to be
#                 SURFACED ("tensions worth watching"), never blended or dropped.
#
# FLAG / FLOOR INTERACTION (the least-surprising wiring, chosen deliberately):
#
#   * ``LEGBA_COMPOSITION_TIERED_EVIDENCE`` unset/off (code DEFAULT OFF) — the
#     legacy behavior byte-for-byte: the basis bar is ``_resolve_verify_floor``
#     (env floor, default 0.0) and NO periphery is gathered or rendered.
#   * flag ON — the split engages on EVERY composition read (PER-COUNTRY,
#     REGION, WORLD, and THEMATIC): the basis bar becomes the SPLIT floor = the
#     env floor when the operator pinned ``LEGBA_COMPOSITION_VERIFY_FLOOR``,
#     else :data:`TIERED_BASIS_FLOOR_DEFAULT` (0.50 — the scorecard's
#     system-wide verification floor, lockstep-tested against
#     ``scorecard_banding.FAITH_FLOOR``). Rationale: at the legacy 0.0 default
#     the "split" would be vacuous (nothing verified is ever below 0.0), so
#     turning the flag on without a meaningful bar would silently keep the
#     blend it exists to fix. ``DEFAULT_VERIFY_FLOOR`` itself is UNCHANGED —
#     the OFF path never moves.
#   * WORLD / THEMATIC scope note (the former SEAMS §44, resolved 2026-07):
#     their periphery gather is the complement over the SAME declared analyst
#     roster + target scope their PRIMARY fetch uses (thematic: the unit across
#     the desk allow-list / all desks; world: the region/thematic heads,
#     target-unscoped, meta-inclusive). The world's DEGRADE path (member-country
#     country_composition heads for a headless region) does NOT get its own
#     periphery complement — a region whose head fell below the bar already
#     surfaces AS periphery, while its verified country reads feed the basis;
#     gathering the fallback tier's complement too would double-surface the same
#     weak lane. The legacy global meta stays untiered, byte-for-byte.
#
# DEFAULT OFF (flip note): because flag-ON meaningfully moves the basis bar
# (0.0 → 0.50 at the default env), the byte-path is NOT identical to today even
# when the periphery set is empty — so per the shipping rule this defaults OFF.
# Flip: ``LEGBA_COMPOSITION_TIERED_EVIDENCE=1`` (optionally pin the bar via
# ``LEGBA_COMPOSITION_VERIFY_FLOOR``). With the flag ON and an EMPTY periphery
# the rendered PROMPT is byte-identical to the same-floor legacy render (the
# section only exists when periphery does); the envelope additionally carries
# the additive ``data.evidence_tiers`` stamp.
TIERED_EVIDENCE_ENV: str = "LEGBA_COMPOSITION_TIERED_EVIDENCE"

TIERED_BASIS_FLOOR_DEFAULT: float = 0.50
"""The BASIS bar when tiered evidence is ON and no env floor is pinned. A
test-enforced MIRROR of ``deterministic_handlers.scorecard_banding.FAITH_FLOOR``
(the system-wide 0.50 verification-floor decision) — a local copy per the house
registry-slim idiom (importing the handler package would drag ~20 sub-handler
modules into this kind module); lockstep is asserted by
``tests/data_pkg/test_composition_tiered_evidence.py``."""

PERIPHERY_CAP: int = 8
"""Max periphery items rendered per composition — worst-first (severity rank,
then recency), so the cap keeps the items most worth surfacing."""

PERIPHERY_BODY_CHARS: int = 400
"""Periphery body excerpt cap — tighter than ``MAX_BODY_CHARS`` (periphery is
hedged context, never the load-bearing narrative)."""

PERIPHERY_TIER: str = "periphery"
"""The tier token: the ``_evidence_tier`` row marker READ_SLICE stamps on
periphery rows AND the ``tier`` value the cite phase stamps on a citation that
resolves into the periphery section (the verify pass keys its hedge-required
rule on it)."""

_EVIDENCE_TIER_KEY: str = "_evidence_tier"
_EVIDENCE_FLOOR_KEY: str = "_evidence_floor"

#: ``severity:<level>`` → worst-first rank for the periphery cap sort. Missing /
#: unknown level ranks -1 (sorts last — an unscored item never displaces a
#: scored one). Mirrors ``scorecard_banding.SEVERITY_TO_BAND``'s level set.
_SEVERITY_RANK: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "elevated": 2,
    "moderate": 1,
    "low": 0,
}


def _tiered_evidence_enabled() -> bool:
    """Whether the C-TIER two-tier evidence split is flag-enabled. Code default
    OFF (see the flip note above)."""
    raw = os.getenv(TIERED_EVIDENCE_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve_split_floor(descriptor: Any) -> float:
    """The BASIS bar for a tiered (flag-ON) composition read.

    The env floor keeps working as the basis bar when the operator pinned it
    (clamped to ``[0.0, 1.0]``, same parse as :func:`_resolve_verify_floor`);
    unset ⇒ :data:`TIERED_BASIS_FLOOR_DEFAULT` (0.50, the scorecard lockstep) —
    NOT ``DEFAULT_VERIFY_FLOOR`` (0.0), at which the split would be vacuous.
    ``descriptor`` is accepted for parity with :func:`_resolve_verify_floor`.
    """
    raw = os.getenv(VERIFY_FLOOR_ENV)
    if raw is not None:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (ValueError, TypeError):
            logger.warning(
                "meta_findings_synthesizer.split_floor.bad_env value=%r — using default",
                raw,
            )
    return TIERED_BASIS_FLOOR_DEFAULT


# CONTINUITY (Phase 1, 2026-07-31) — TEMPORAL CONTINUITY VIA CITABLE REFS.
#
# The units are deliberately STATELESS slice-of-now analyzers, and so was every
# composition above them: each cycle re-derived the picture from scratch, so a
# new event could never read as "this ESCALATES the situation we were already
# watching". Phase 1 gives the compositions (country / region / thematic) and the
# WORLD read a memory — WITHOUT a new kind, a new schema, or a trajectory ledger
# (that is Phase 2).
#
# TWO HARD LESSONS BIND THE DESIGN:
#
#   1. The world_context RAG ROLLBACK. An UNCITED prior leaking into cited
#      analysis is this platform's NAMED failure mode (the uncited-prior-leak
#      mechanism). So continuity context enters ONLY as CITABLE REFS: each
#      continuity block gets its own ``[[ref:N]]`` ordinal in the SAME flat
#      resolution space as the basis + periphery sub-claims, and the prompt
#      requires the model to cite it exactly like any other evidence. The verify
#      pass grades a clause resting on a continuity ref against that ref's
#      ``evidence_text`` with NO change to the verify path — a continuity block
#      is just another rendered block with an ordinal.
#   2. TEMPORAL COLLAPSE. The prior read carries its OWN ``produced_at`` and each
#      situation its OWN ``last_event_at`` / age, both rendered INTO the block;
#      the prompt clause anchors every temporal statement on those dates and
#      forbids anchoring on run/fetch time (the tradecraft preamble's rule 6,
#      restated where it can actually bite).
#
# TWO REFS, both bounded, both absent-by-default:
#
#   * PRIOR READ (``continuity_prior_ref``) — THE SAME TARGET's previous
#     non-superseded, VERIFIED head from THIS composition analyst. One ref,
#     labeled as the prior read, carrying its produced_at + age. First run (or a
#     prior head that never cleared verify) ⇒ simply ABSENT and the slice is
#     byte-compatible with the pre-continuity read.
#   * OPEN-SITUATION REGISTER (``continuity_situations_ref``) — a compact,
#     bounded register of the target-relevant OPEN ``situations`` rows (name,
#     status, intensity, event_count, last_event_at, age), rendered as ONE
#     referenceable evidence block. Scope-filtered to the composition's target
#     the SAME way the rest of its slice is scoped (per-country: the country;
#     region: its member desks; thematic: its desk allow-list; world: unscoped).
#
# NOT WIRED into the LEGACY global meta (``analyst_meta_synthesizer.yaml``) — it
# keeps the standing "legacy read byte-for-byte" discipline every branch honors.
#
# BEST-EFFORT, DEGRADE-NEVER-BREAK: the continuity gather is an ADDITIVE
# enrichment (the same posture ``read_open_contention`` has at the actor layer).
# Any error reading it logs and yields NO continuity rows — a compose never fails
# because its memory was unavailable.
CONTINUITY_ROW_KEY: str = "_continuity"
"""Row marker READ_SLICE stamps on a continuity row so the DB-less ``_run`` can
partition it out of the BASIS/PERIPHERY tiers on DATA, never on env. Value is
:data:`CONTINUITY_PRIOR` or :data:`CONTINUITY_SITUATIONS`."""

CONTINUITY_PRIOR: str = "prior_read"
"""Marker value: the row IS this target's previous verified composition head."""

CONTINUITY_SITUATIONS: str = "situations_register"
"""Marker value: the row is the synthetic open-situation REGISTER block."""

CONTINUITY_SITUATIONS_ROW_KEY: str = "_situations"
"""Key on the synthetic register row carrying its bounded situation dicts."""

CONTINUITY_PRIOR_RECEIPT: str = "continuity_prior_ref"
"""Receipt key: 1 when a prior-read ref entered the slice, else 0."""

CONTINUITY_SITUATIONS_RECEIPT: str = "continuity_situations_ref"
"""Receipt key: 1 when the situation-register ref entered the slice, else 0."""

CONTINUITY_PRIOR_LOOKBACK_HOURS: int = 168
"""How far back the prior-read lookup reaches (7 days) — INDEPENDENT of the
slice window, because "the previous read" is a per-head fact, not a per-slice
one: a composition on a 24h window whose last cycle was skipped still has a
prior read worth diffing against. Bounded so a months-old head is never dressed
up as "the prior read"; the block always shows its produced_at + age so the model
(and the verify pass) can see exactly how stale the memory is."""

CONTINUITY_PRIOR_BODY_CHARS: int = 900
"""Body excerpt cap for the prior-read block. Wider than
:data:`PERIPHERY_BODY_CHARS` (the diff is the whole point — a truncated prior
read produces a fabricated-looking "change") but well under the basis
``MAX_BODY_CHARS`` * the slice size, so the block cannot dominate the turn."""

SITUATION_REGISTER_CAP: int = 8
"""Max open situations rendered in the register — worst-first (intensity, then
recency). A register is a compact ORIENTING index, not a second slice."""

SITUATION_REGISTER_NAME_CHARS: int = 120
"""Per-frame name cap in the register. Tighter than :data:`MAX_TITLE_CHARS` — a
situation name is a short frame LABEL, and the cap is what bounds the register's
worst-case footprint (and therefore its captured evidence text) to a known size."""

SITUATION_REGISTER_TRAJECTORY_DEPTH: int = 3
"""How many DATED deltas per frame the register carries (continuity P2, D5).
Three is the plan's number and is what a reader needs to see a direction rather
than a point: one delta is an event, three is a trend or the absence of one."""

SITUATION_REGISTER_WHY_CHARS: int = 180
"""Per-delta ``why`` cap in the register. A ledger ``why`` is already one
sentence; this bounds the pathological case so the register's worst-case
footprint (and therefore its captured evidence text) stays a known size."""

SITUATION_REGISTER_EVIDENCE_CHARS: int = 5000
"""Cap on the register's captured ``evidence_text``. Sized to hold the WHOLE
rendered register at its own bounds (:data:`SITUATION_REGISTER_CAP` frames x
~225 chars of frame line + :data:`SITUATION_REGISTER_TRAJECTORY_DEPTH` trajectory
lines of ~250 + header ~ 4.4k) rather than reusing
:data:`MAX_EVIDENCE_TEXT_CHARS` (600, sized for ONE sub-claim body). Load-bearing:
``verify._ordinal_evidence_map`` applies NO cap of its own, so the synth-side
capture IS what the judge grades against — a 600-char cut would silently hide the
tail of the register and false-demote a faithful claim about a frame the model was
actually shown."""

SITUATION_REGISTER_REF_KIND: str = "situation_register"
"""``ref_kind`` stamped on a citation that resolves into the register block.

DELIBERATELY NOT ``'finding'``: the register is not an ``analyst_outputs`` row,
and it has NO single substrate id, so the citation carries NO ``ref_id`` — it
carries ``situation_ids`` (the REAL ``situations`` uuids behind the block)
instead. Fabricating a ``ref_id`` (e.g. the top situation's) to make a drill
link resolve would be exactly the dishonesty this platform refuses. Consumers
that key on ``ref_kind == 'finding'`` (``scorecard_reconcile.composition_usages``,
``verify._uses_subclaim_convention``) skip it unchanged."""

CONTINUITY_CITATION_KEY: str = "continuity"
"""Citation field naming WHICH continuity ref a citation resolved into
(:data:`CONTINUITY_PRIOR` / :data:`CONTINUITY_SITUATIONS`). Additive — a basis
or periphery citation never carries it."""


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

# T7 cross-desk correlation guard float-noise tolerance (mirrors verify's
# ``_HEDGE_EPSILON``): a confidence is capped only when it exceeds the
# de-duplicated ceiling by MORE than this.
_GUARD_EPSILON: float = 1e-6


# ---------------------------------------------------------------------------
# Deps surface — LLM port only (no substrate side-deps; the runtime
# materializes inputs before calling run_method, same as the other kinds).
# ---------------------------------------------------------------------------


@runtime_checkable
class MetaFindingsDeps(Protocol):
    """Minimum dep surface ``run_method`` needs.

    The runtime constructs this from ``StandardDeps`` (typically a small
    adapter that surfaces ``deps.extras['llm']``). A plain object with an
    ``llm`` attribute conforming to
    :class:`legba.runtime.analyst_method.LLMHandlerLike` satisfies it;
    tests use a stub.
    """

    llm: LLMHandlerLike


# ---------------------------------------------------------------------------
# Prompt module (DSPy wrapping deferred to L-176 / L-105 §2)
# ---------------------------------------------------------------------------


from ._tradecraft import (  # noqa: E402
    COMPOSITION_BODY_SHAPE,
    CONSEQUENCE_RULE,
    NO_INSTRUMENT_READINGS,
    as_of_rule,
    with_preamble,
)

_SYSTEM_PROMPT = with_preamble(
    """TASK — second-order synthesis. You are given FIRST-ORDER FINDINGS from OTHER analysts (each with title, body, confidence, evidence, and a source analyst_id). Produce ONE second-order FINDING that is only visible when these outputs are considered together: the higher-order pattern, the convergent claim, the contradiction, or the emergent narrative. Lead `body` with the BLUF. DO NOT re-state any individual finding verbatim. Cite which analysts ground each claim (by analyst_id). If the findings disagree, surface the disagreement rather than averaging it away.
Respond with strict JSON, nothing else: {"title": "...", "body": "...", "confidence": 0.0-1.0, "evidence": ["..."], "tags": ["..."]}"""
)


# CONTINUITY prompt clause (Phase 1) — ONE definition, four compositions.
#
# The clause is LETTERED per prompt (each composition prompt numbers its rules
# (a)…(n) and the sequences differ), so it is generated rather than pasted — a
# copy per prompt would drift the moment one is edited, and the whole point of a
# continuity contract is that every composition floor states it identically.
#
# It encodes exactly four obligations, in the order a reader needs them:
#   1. SAY WHAT CHANGED versus the cited prior read (the deliverable).
#   2. ANCHOR "when" on the blocks' OWN dates/ages — never run/fetch time (the
#      temporal-collapse guard; the tradecraft preamble states this generally,
#      this restates it where a prior-read block makes it bite).
#   3. NO-CHANGE IS AN ANSWER — say so plainly rather than re-deriving, which is
#      what makes a stateless re-derivation visibly different from a real diff.
#   4. NEVER assert continuity that is not grounded in one of the two blocks, and
#      when NEITHER is shown, name the run as a FIRST read. This is the clause
#      that keeps the RAG-rollback failure mode (an uncited prior leaking into
#      cited analysis) structurally unavailable: the ONLY licensed sources of
#      "before" are two blocks that must be cited like any other evidence.
#: D1 anchored for the COMPOSITION layer. A composition has no slice header —
#: its dated anchors are the ``produced_at=`` values the block renderer prints
#: on every shown block, so the as-of is taken from the newest of those. Same
#: zero-new-facts property as the unit form: the date is a copy of rendered
#: text, never a read of the wall clock.
_COMPOSITION_AS_OF = as_of_rule(
    "'*As of <date>; composed from <N> <unit|country|region|desk> reads, "
    "latest <time>.*'. Take the date and time from the MOST RECENT produced_at "
    "printed on a shown block — rendered as a human calendar date and time, "
    "never as the raw ISO/microsecond value — and take the count from the "
    "blocks actually shown. If the shown blocks span more than a day, say so "
    "in the same line ('reads span 1-3 August')."
)


def _continuity_rule(letter: str) -> str:
    """The continuity rule text, lettered for one composition prompt.

    PHASE-V — carries the D1 as-of clause in front of the continuity
    obligations, and repairs the two lines D5 traced the machine-internals leak
    to. Both repairs are one-liners with outsized effect, because the model was
    reading each as a REPORTING REQUIREMENT rather than as the anti-embellishment
    guard it was written to be:

      * The worked example ``'no material change since the prior read of <its
        produced_at>'`` invited a literal substitution of the column value, so
        56/117 compositions narrated a microsecond ISO timestamp at the
        operator. It now shows a human date and states the rendering rule.
      * ``'describe a situation ONLY as ... its own name, status, intensity and
        event count'`` was meant to CAP what may be said about a frame; it read
        as a list of fields to print, so 27/117 compositions reported
        ``intensity 54.59 and 302 events`` as prose. The cap now names only the
        two reader-facing fields, and the two instrument readings are explicitly
        decide-with-never-print.
    """
    return (
        _COMPOSITION_AS_OF + " "
        f"({letter}) CONTINUITY — a PRIOR READ block (this same target's previous "
        "verified read, carrying its OWN produced_at) and/or an OPEN SITUATION "
        "REGISTER block (the currently-open situation frames for this scope, each "
        "with its own status, intensity, event count and last_event_at/age) may be "
        "shown at the END of the evidence, each with its own [[ref:N]] handle. When "
        "either is shown you MUST: (1) state EXPLICITLY what CHANGED versus the "
        "cited prior read — name the change and cite the prior read by its "
        "[[ref:N]] handle exactly like any other block; (2) anchor EVERY temporal "
        "statement on the dates and ages printed IN those blocks (the prior read's "
        "produced_at, a situation's last_event_at / age) — NEVER on 'today', 'now', "
        "'as of this run', or the time you are running; (3) if nothing material "
        "changed, SAY SO plainly and briefly (e.g. 'no material change since the "
        "3 August morning read [[ref:N]]' — a HUMAN calendar date derived from the "
        "block's produced_at, NEVER the raw ISO/microsecond timestamp) rather than "
        "re-deriving the "
        "same picture in different words; (4) describe a situation ONLY as the "
        "register states it — its own name and status — "
        "and never upgrade, downgrade, or re-date it beyond what the register "
        "shows. The register's intensity score and event_count are internal "
        "instrument readings: USE them to decide, never PRINT them, and never "
        "promote a NEGATIVE finding into a named 'situation frame'. NEVER assert "
        "continuity of ANY kind — an escalation, a "
        "de-escalation, a trend, an 'ongoing'/'longstanding' framing, or that "
        "something has 'been building' — unless it is grounded in the cited PRIOR "
        "READ block or the SITUATION REGISTER block. If NEITHER block is shown "
        "this is a FIRST read of this target: say so plainly and make NO claim "
        "about what came before. "
    )


# ---------------------------------------------------------------------------
# PHASE-V D6 — the composition rule set, generated per prompt
# ---------------------------------------------------------------------------
#
# Every composition reads like the minutes of a status meeting because that is
# what it was asked for. 94 of 117 sampled compositions ran an OBSERVATION /
# JUDGMENT roll call — one bullet per unit in fixed order, then the same items
# again with "Assessment:" in front — and the residual "judgment" was one
# tautological sentence. Three prompt lines produce that, and all three are
# repaired here rather than in three hand-edited copies:
#
#   * The COVERAGE rule was stated as an EQUAL-AIRTIME obligation whose worked
#     example was a unit-naming sentence, so the model satisfied it with an
#     enumeration and had no room left for an argument. Its integrity guarantee
#     (never silently drop a shown block) is real and is KEPT — it just moves to
#     a footer.
#   * The HEDGE rule handed the model the bureaucratic register verbatim ("the
#     units indicate / suggest"), so hedging became a house voice instead of a
#     calibration duty.
#   * The ONLY structural instruction was "lead body with a one-line BLUF", so
#     the model invented a skeleton — and the skeleton the other two rules imply
#     is the roll call.
#
# WHAT IS DELIBERATELY NOT TOUCHED: the TRACEABILITY rule in every prompt (a
# [[ref:N]] marker is a PROMISE that block N literally states the claim it tags;
# never introduce a fact, proper noun, or specific not present in a cited
# block). D6's one risky line — "say what these blocks TOGETHER show that none
# shows alone" — is exactly the line that invites synthesis beyond the evidence,
# and TRACEABILITY is what polices it. The shape rule below must never ship
# without it.


def _shape_rule(letter: str, *, block_noun: str, lead: str) -> str:
    """The (d)-slot SHAPE rule: judgment in the body, coverage in a footer."""
    return (
        f"({letter}) {COMPOSITION_BODY_SHAPE} "
        f"The BLUF names {lead}. '## The picture' is CONNECTED ARGUMENT: what "
        f"these {block_noun} TOGETHER show that none shows alone, ordered by "
        "consequence — every clause still carrying its [[ref:N]] and still "
        "bound by the TRACEABILITY rule below, which is what keeps 'together' "
        "from becoming 'invented'. Do NOT write a paragraph or a bullet per "
        f"{block_noun[:-1] if block_noun.endswith('s') else block_noun}, do NOT "
        "restate a shown block verbatim, and do NOT emit an OBSERVATION / "
        "JUDGMENT skeleton or any other section list of your own. "
    )


def _tension_rule(letter: str, *, block_noun: str, a: str, b: str) -> str:
    """The (c)-slot DISAGREEMENT rule + where the disagreement is written.

    Extends the existing directional-disagreement rule to FACTUAL disagreement,
    which is the leg that failed silently and visibly: on one desk, one day, the
    energy_security unit reported the Strait of Hormuz "remains effectively
    shut" while economic_coercion reported "no concrete closure is in place" —
    and the country composition inherited both blocks and narrated them as
    agreement. The old rule only ever asked about sub-claims pointing in
    different DIRECTIONS; two blocks asserting incompatible states of the same
    FACT sailed through it.
    """
    return (
        f"({letter}) SURFACE DISAGREEMENT in the '## Tension' section — do NOT "
        f"average it into a false consensus. When {a} and {b} point in "
        f"different directions, NAME BOTH and cite BOTH diverging {block_noun} "
        "via their two [[ref:N]] ordinals. DISAGREEMENT INCLUDES FACTUAL "
        "DISAGREEMENT, not only directional disagreement: BEFORE you write, "
        "check whether two shown blocks assert incompatible STATES OF THE SAME "
        "FACT (one says a chokepoint is closed, another says no closure is in "
        "place). When they do, name both sources, quote both "
        "characterizations, cite both [[ref:N]] handles, and say which is "
        "better supported and why — do NOT silently adopt one, and do NOT "
        "combine them into a sentence that implies they agree. When the shown "
        "blocks genuinely agree, say so plainly in one line rather than "
        "manufacturing a tension. "
    )


def _hedge_rule(letter: str, *, tic: str, worked: str) -> str:
    """The (b)-slot HEDGE rule — a calibration duty, not a house voice."""
    return (
        f"({letter}) HEDGE to the evidence — weaken your language as "
        "effective_confidence drops, and attribute a judgment to the source "
        "that made it when it matters who said it. Hedging is a CALIBRATION "
        f"DUTY, not a house voice: do NOT open clauses with '{tic}' as a tic. "
        f"Prefer a dated, attributed statement ('{worked}') over an agentless "
        "hedge. "
    )


def _coverage_rule(letter: str, *, block_noun: str, unit_noun: str) -> str:
    """The coverage rule — integrity guarantee kept, airtime obligation dropped."""
    return (
        f"({letter}) COVERAGE IS A FOOTER, NOT THE BODY. Every shown "
        f"{unit_noun} must still be accounted for — silently dropping one is an "
        f"integrity failure — but a {unit_noun} whose read is unremarkable is "
        "accounted for by NAMING IT IN THE '## Coverage' LINE, not by giving it "
        f"a bullet in the body. A {unit_noun} with NO block shown is an "
        "unassessed GAP: name it in that same line as a gap and NEVER infer, "
        f"estimate, or invent its state. A {unit_noun} earns space in '## The "
        f"picture' ONLY when it changes the read. Do not claim to cover a "
        f"{unit_noun} whose block is not shown, and never attach a [[ref:N]] to "
        "a gap. "
    )


# P3 per-COUNTRY composition system prompt.
#
# Selected in-kind by the runtime's ``options["target_id"]`` stamp (set only when
# the run is target-scoped — a per-country composition descriptor). The GLOBAL
# meta run keeps ``_SYSTEM_PROMPT`` byte-for-byte. Distinct from the global
# synthesis prompt in three load-bearing ways: (1) it cites EVERY factual clause
# with an inline ``[[ref:N]]`` ordinal marker resolving to the Nth sub-claim in
# the rendered bundle (so the composition is itself citable and a LATER stage
# can run a faithfulness verify OVER the composition); (2) it hedges to
# ``effective_confidence`` and weakens language as the evidence weakens; (3) it
# surfaces disagreement between sub-claims rather than averaging a false
# consensus, and narrates an HONEST EMPTY read (confidence 0.0, no fabricated
# evidence) when a country has no verified sub-claims.
_COMPOSITION_SYSTEM = with_preamble(
    """TASK — per-country COMPOSITION. You are given the VERIFIED, faithfulness-checked SUB-CLAIMS (first-order unit findings) for ONE country from up to seven bounded units (leadership_transition, energy_security, escalation, narrative_coordination, internal_stability, military_posture, economic_coercion). Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source unit analyst_id, effective_confidence (already min(confidence, faithfulness)), title and body. Produce ONE second-order per-country READ. RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the sub-claim block it rests on; NEVER invent an N and NEVER cite an N not shown; a clause with no sub-claim behind it must NOT assert a fact. """
    + _hedge_rule(
        "b",
        tic="the units indicate / suggest",
        worked="the escalation desk's 3 August read has ...",
    )
    + _tension_rule(
        "c",
        block_noun="sub-claim blocks",
        a="one unit's sub-claim",
        b="another's",
    )
    + _shape_rule(
        "d",
        block_noun="units",
        lead=(
            "the single most consequential thing on this desk and why it "
            "matters — ranked on the STAKES the cited blocks describe (cited "
            "loss of life or armed conflict, then cited disruption to a system "
            "many actors depend on, then cited irreversibility, then cited "
            "proximity), NEVER on which block scored the highest "
            "effective_confidence and NEVER on which item merely changed"
        ),
    )
    + """(e) HONEST EMPTY: if there are no verified sub-claims for this country, say so plainly with confidence 0.0 and NO fabricated evidence. (f) TRACEABILITY — a [[ref:N]] marker is a PROMISE that sub-claim block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown sub-claim blocks actually say. NEVER introduce a fact, proper noun, place-name, or event specific (a magnitude, date, location, or count) that is not present in a cited block — do NOT add concrete details a unit did not state (e.g. an event's magnitude or location, or a named actor, commitment, or position no block mentions). If you cannot ground a clause in a shown block, DROP the clause; an in-range [[ref:N]] does NOT license a claim its block does not make. (g) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence actually shown for a cited block, and invent NO per-unit confidence figure or a unit that is not present; do NOT silently change a unit's stated severity or which driver a unit called its lead — if you aggregate differing unit severities, say so explicitly (e.g. 'aggregating unit severities moderate+low -> moderate'). """
    + NO_INSTRUMENT_READINGS
    + " "
    + _coverage_rule("h", block_noun="sub-claim blocks", unit_noun="unit")
    + _continuity_rule("i")
    + """Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] markers...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
)


# S2-T2 REGIONAL composition system prompt (region_composition).
#
# Selected in-kind for a REGION run (``options["target_id"]`` = ``region_<slug>``;
# dispatched at the ``region_scoped`` branch below). Mirrors ``_COMPOSITION_SYSTEM``
# but REGION-worded: the cited "sub-claims" are the per-COUNTRY reads
# (country_composition findings) of ONE region, so the read cites a COUNTRY-READ via
# its [[ref:N]] ordinal handle and its load-bearing surface is CROSS-COUNTRY
# disagreement WITHIN the region. It additionally consumes an appended CONTESTED
# FACTS block (open public.fact_contention disputes) and marks any touched dispute
# ``[[contested:<contention_id>]]`` naming BOTH arbiter-surfaced sides. (The true
# WORLD run composes REGIONS via ``_WORLD_OVER_REGIONS_SYSTEM`` below.)
_REGION_COMPOSITION_SYSTEM = with_preamble(
    """TASK — REGIONAL COMPOSITION. You are given the VERIFIED, faithfulness-checked per-COUNTRY READS (second-order country_composition findings) for the member countries of ONE world region, one or more per country. Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source analyst_id, effective_confidence (already min(confidence, faithfulness)), title and body. You MAY also be given a CONTESTED FACTS block: open disputes over a single fact (subject+predicate) where the arbiter surfaced more than one value cluster. Produce ONE second-order REGIONAL READ over the shown country reads. RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the COUNTRY READ block it rests on; NEVER invent an N, NEVER cite a raw signal, and NEVER cite an N not shown; a clause with no country read behind it must NOT assert a fact. """
    + _hedge_rule(
        "b",
        tic="the country reads indicate / suggest",
        worked="Sudan's 3 August country read carries ...",
    )
    + _tension_rule(
        "c",
        block_noun="country-read blocks",
        a="one country's read",
        b="another's",
    )
    + _shape_rule(
        "d",
        block_noun="country reads",
        lead=(
            "the specific REGION this read covers (infer it from the shown "
            "country reads, which are ALL members of ONE region) and the "
            "single most consequential thing in it — do NOT open with a global "
            "'The world faces...' frame; this is a REGION, not the world"
        ),
    )
    + CONSEQUENCE_RULE
    + " "
    + """(e) CONTESTED FACTS: when a claim touches a listed contested group, NAME both surfaced sides and mark it [[contested:<contention_id>]] using EXACTLY a contention_id shown in the block; NEVER pick a side the arbiter did not surface and NEVER invent a contested id. (f) HONEST EMPTY: if there are no country reads, say so plainly with confidence 0.0 and NO fabricated evidence. (g) TRACEABILITY — a [[ref:N]] marker is a PROMISE that country-read block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown country reads actually say. NEVER introduce a country, actor, event specific, or figure not present in a cited country-read block; if you cannot ground a clause in a shown block, DROP it (an in-range [[ref:N]] does NOT license a claim its block does not make). (h) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence shown for a cited block, and do NOT silently alter a country read's severity or the driver it called its lead; make any aggregation explicit. """
    + NO_INSTRUMENT_READINGS
    + " "
    + _coverage_rule("i", block_noun="country-read blocks", unit_noun="country")
    + _continuity_rule("j")
    + """Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] (and any [[contested:<id>]]) markers...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
)

# Back-compat alias — the constant was named ``_WORLD_COMPOSITION_SYSTEM`` in
# round-1, a misnomer: it is REGION-scoped (dispatched only for region runs). The
# name/comment now say REGIONAL; the alias keeps existing references (tests,
# imports) resolving to the renamed constant.
_WORLD_COMPOSITION_SYSTEM = _REGION_COMPOSITION_SYSTEM


# S2-T3 GLOBAL (world) composition over REGIONS system prompt.
#
# Selected in-kind by the runtime's ``options["composition"]`` stamp on the
# target-LESS world_assessor run. Mirrors ``_REGION_COMPOSITION_SYSTEM`` but
# REGION-worded: the cited blocks are per-REGION reads (region_composition
# findings), so the load-bearing surface is CROSS-REGION disagreement. Because
# the world read DEGRADES a region with no region read to that region's country
# reads, a shown block may instead be one of a region's per-COUNTRY reads (still
# a real, cited block). It additionally consumes the CONTESTED FACTS block (open
# public.fact_contention disputes) and a REGION COVERAGE block that NAMES any
# region with NO read at all — the model must surface those as unassessed gaps.
# Distinct constant from ``_REGION_COMPOSITION_SYSTEM`` so the S2-T2 region compose
# (which composes COUNTRY reads and keeps that prompt) is untouched.
_WORLD_OVER_REGIONS_SYSTEM = with_preamble(
    """TASK — GLOBAL world COMPOSITION over REGIONS. You are given the VERIFIED, faithfulness-checked per-REGION READS (second-order region_composition findings), one per region. For a region that had NO region read this cycle, one or more of its per-COUNTRY reads are shown IN ITS PLACE (a degrade — treat them as that region's available evidence). Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source analyst_id, effective_confidence (already min(confidence, faithfulness)), title and body. You MAY also be given a CONTESTED FACTS block (open disputes over a single fact where the arbiter surfaced more than one value cluster) and a REGION COVERAGE block naming world regions that have NO read at all this cycle. Produce ONE second-order WORLD READ. RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the read block it rests on; NEVER invent an N, NEVER cite a raw signal, and NEVER cite an N not shown; a clause with no read behind it must NOT assert a fact. """
    + _hedge_rule(
        "b",
        tic="the region reads indicate / suggest",
        worked="the Africa read of 3 August carries ...",
    )
    + _tension_rule(
        "c",
        block_noun="region-read blocks",
        a="one region's read",
        b="another's",
    )
    + _shape_rule(
        "d",
        block_noun="region reads",
        lead=(
            "the single most consequential situation on this board and why it "
            "matters. THIS IS THE WORLD HEADLINE: it is read as the tower's "
            "answer to 'what matters most right now', so getting the ranking "
            "right is this read's whole job"
        ),
    )
    + CONSEQUENCE_RULE
    + " "
    + """(e) CONTESTED FACTS: when a claim touches a listed contested group, NAME both surfaced sides and mark it [[contested:<contention_id>]] using EXACTLY a contention_id shown in the block; NEVER pick a side the arbiter did not surface and NEVER invent a contested id. (f) REGION GAPS: if the REGION COVERAGE block lists a region as having NO read, NAME that region plainly in the '## Coverage' line as an unassessed gap with NO current read — do NOT infer, estimate, or invent its state, and NEVER attach a [[ref:N]] to a gap region. (g) HONEST EMPTY: if there are no reads at all, say so plainly with confidence 0.0 and NO fabricated evidence. (h) TRACEABILITY — a [[ref:N]] marker is a PROMISE that block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown reads actually say. NEVER introduce a region, country, actor, event specific, or figure not present in a cited block; if you cannot ground a clause in a shown block, DROP it (an in-range [[ref:N]] does NOT license a claim its block does not make). (i) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence shown for a cited block, and do NOT silently alter a read's severity or the driver it called its lead; make any aggregation explicit. """
    + NO_INSTRUMENT_READINGS
    + " "
    + _coverage_rule("j", block_noun="region-read blocks", unit_noun="region")
    + _continuity_rule("k")
    + """Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] (and any [[contested:<id>]]) markers...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
)


# S2-T4 THEMATIC (escalation) composition system prompt.
#
# Selected in-kind by the runtime's ``options["thematic_dimension"]`` stamp on the
# target-LESS thematic run. Mirrors ``_WORLD_OVER_REGIONS_SYSTEM`` but THEMATIC-
# worded: the cited blocks are per-DESK escalation-unit reads (one per country
# desk), so the load-bearing surface is CROSS-DESK convergence/divergence of
# ESCALATION risk. It consumes a DESK COVERAGE block naming any desk with no
# escalation head (absence-honest gaps). The CORRELATION rule is load-bearing: two
# desks whose reads rest on the SAME underlying wire signal are NOT independent
# corroboration — the kind's T7 guard de-duplicates the evidence numerically, and
# the prompt tells the model not to treat shared-signal desks as independent.
# Distinct constant from ``_WORLD_OVER_REGIONS_SYSTEM`` so the world/region
# compositions are untouched.
_THEMATIC_COMPOSITION_SYSTEM = with_preamble(
    """TASK — GLOBAL THEMATIC COMPOSITION over ESCALATION. You are given the VERIFIED, faithfulness-checked per-DESK ESCALATION READS (first-order `escalation` unit findings), ONE per country desk, each for a DIFFERENT country. Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source analyst_id, its DESK (target_id — the country the read is for), effective_confidence (already min(confidence, faithfulness)), title and body. You MAY also be given a DESK COVERAGE block naming desks that have NO escalation read this cycle. Produce ONE second-order GLOBAL ESCALATION READ that surveys near-term escalation risk ACROSS the desks. RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the desk read block it rests on, and NAME the desk (country) it is about; NEVER invent an N, NEVER cite a raw signal, and NEVER cite an N not shown; a clause with no desk read behind it must NOT assert a fact. """
    + _hedge_rule(
        "b",
        tic="the desk reads indicate / suggest",
        worked="the Ukraine desk's 3 August read has ...",
    )
    + _tension_rule(
        "c",
        block_noun="desk-read blocks",
        a="one desk's read",
        b="another's",
    )
    + """(d) CORRELATION — two desks whose reads rest on the SAME underlying wire signal (a shared cross-border incident, one alliance move seen from both sides) are NOT independent corroboration; do NOT count them twice or let a single shared event inflate the global picture. When two cited desks clearly describe the SAME underlying event, SAY SO rather than presenting them as two independent data points. (e) DESK GAPS: if the DESK COVERAGE block lists a desk as having NO read, NAME that desk plainly in the '## Coverage' line as an unassessed gap with NO current escalation read — do NOT infer, estimate, or invent its state, and NEVER attach a [[ref:N]] to a gap desk. """
    + _shape_rule(
        "f",
        block_noun="desk reads",
        lead=(
            "where global escalation risk is actually concentrated. THIS READ "
            "FEEDS THE WORLD HEADLINE, so its ordering propagates: rank the "
            "desks by stakes, and never present them as a list sorted by score"
        ),
    )
    + CONSEQUENCE_RULE
    + " "
    + """(g) HONEST EMPTY: if there are no desk reads at all, say so plainly with confidence 0.0 and NO fabricated evidence. (h) TRACEABILITY — a [[ref:N]] marker is a PROMISE that desk-read block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown desk reads actually say. NEVER introduce a country, actor, event specific, or figure not present in a cited block; if you cannot ground a clause in a shown block, DROP it (an in-range [[ref:N]] does NOT license a claim its block does not make). (i) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence shown for a cited block, and do NOT silently alter a desk read's severity or the vector it called its lead; make any aggregation explicit. """
    + NO_INSTRUMENT_READINGS
    + " "
    + _continuity_rule("j")
    + """Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] markers naming each desk...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
)


# A ``[[ref:N]]`` marker — a 1-BASED ORDINAL (small int) naming the position of
# the cited sub-claim in the rendered bundle. The composition prompt asks the
# model to cite each factual clause with one of these, using EXACTLY the small
# integer N stamped at the START of the sub-claim block it rests on. An ordinal is
# a 1-2 digit int the model copies RELIABLY (mirroring the unit ``[N]`` → Nth
# signal contract) — whereas a raw 36-char uuid was copied UNRELIABLY (the world
# run fabricated all 10, scoring the composition 0.0). Post-generation we keep only
# markers whose N is in ``[1, len(sliced)]`` and DROP (never emit) any out-of-range
# (fabricated) one — honesty by construction. Wrapped ``[[ref:...]]`` so verify's
# syntax discriminator still tells a composition marker from a unit ``[N]`` (the
# two regexes are provably disjoint — ``\[(\d+)\]`` never matches ``[[ref:5]]`` and
# ``\[\[ref:`` never matches ``[5]``).
_REF_MARKER_RE = re.compile(r"\[\[ref:(\d+)\]\]")

# A ``[[contested:<uuid>]]`` marker (T4, world composition only) — the
# contention_id of an open public.fact_contention dispute the model was shown in
# the CONTESTED FACTS block. Post-generation we keep only markers whose id is in
# the assembled group-id set and DROP (never emit) any fabricated/unlisted one,
# so the world read can never surface a "contested group" it was not fed. The
# real contention_id lets the UI resolve it through the existing
# GET /api/v1/contention?group=<id> read (substrate_reads_api._hydrate_contention).
_CONTESTED_MARKER_RE = re.compile(r"\[\[contested:([0-9a-fA-F-]{36})\]\]")


def _extract_ref_markers(
    body: str,
    num_subclaims: int,
) -> tuple[list[int], int]:
    """Resolve the ``[[ref:N]]`` ordinal markers in ``body`` against the slice RANGE.

    Returns ``(resolved_ordinals, dropped_count)``:

      * ``resolved_ordinals`` — the DISTINCT 1-based ordinals ``N`` that appear as
        ``[[ref:N]]`` markers AND lie in ``[1, num_subclaims]`` (i.e. point at a
        real sub-claim block in the rendered bundle), in first-appearance order.
      * ``dropped_count`` — the number of DISTINCT markers whose ``N`` is OUT OF
        RANGE (``< 1`` or ``> num_subclaims``) — a fabricated handle. These are
        counted for observability and NEVER emitted — the composition never
        surfaces a citation it cannot ground in a rendered sub-claim. Copying a
        1-2 digit int is reliable, so a dropped ordinal is far rarer than the raw
        uuid it replaced, but the drop-and-count honesty contract is preserved.

    ``N`` is the ordinal position in the (already ORIENTed + trimmed) ``sliced``
    list — the SAME ``enumerate(sliced, start=1)`` index the render stamps and the
    CITE block re-derives, so ``N`` ⇒ ``sliced[N-1]`` with no drift.
    """
    resolved: list[int] = []
    seen: set[int] = set()
    dropped = 0
    for match in _REF_MARKER_RE.finditer(body or ""):
        n = int(match.group(1))
        if n in seen:
            continue
        seen.add(n)
        if 1 <= n <= num_subclaims:
            resolved.append(n)
        else:
            dropped += 1
    return resolved, dropped


def _extract_contested_markers(
    body: str,
    allowed_ids: set[str],
) -> tuple[list[str], int]:
    """Resolve ``[[contested:<uuid>]]`` markers in ``body`` against ``allowed_ids``.

    Same honesty contract as :func:`_extract_ref_markers` (DISTINCT, canonical,
    first-appearance order; fabricated/unlisted markers DROPPED + counted, never
    emitted). ``allowed_ids`` is the set of contention_ids the model was shown in
    the CONTESTED FACTS block — so the world read can only mark a dispute the
    arbiter actually surfaced, and its ``[[contested:<id>]]`` always resolves
    through the existing /api/v1/contention read.
    """
    resolved: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for match in _CONTESTED_MARKER_RE.finditer(body or ""):
        raw = match.group(1)
        canon = _coerce_uuid(raw)
        key = str(canon) if canon is not None else raw
        if key in seen:
            continue
        seen.add(key)
        if canon is not None and str(canon) in allowed_ids:
            resolved.append(str(canon))
        else:
            dropped += 1
    return resolved, dropped


# CHILD-REF DEFUSE (P2 gallery finding #2) — a lower-tier composition's own
# ``[[ref:N]]`` markers survive verbatim INSIDE the body/evidence text a
# PARENT tier renders as one of ITS OWN evidence blocks. A composition's
# rendered user turn prefixes EACH block with ITS OWN ``[[ref:N]]`` ordinal
# handle — the ONE resolvable citation space for THIS run — but the block's
# BODY is a lower tier's completed prose, written to cite ITS OWN, unrelated
# ordinals over ITS OWN evidence set (e.g. a region_composition's body reads
# "...isolated internal security events... [[ref:1]]" pointing at THAT
# country's own unit #1, not this tier's block #1 — live capture 2026-07-31,
# P2 gallery §2 Obs.1 / §4 Obs.2). Left verbatim, a model asked to cite
# ``[[ref:N]]`` can copy one of these foreign markers into its own output,
# where an in-range collision is silently reinterpreted as pointing at THIS
# tier's block N (the WRONG evidence) rather than being caught by the honest
# out-of-range filter.
_CHILD_REF_REWRITE_RE = _REF_MARKER_RE


def _defuse_child_ref_markers(text: str) -> str:
    """Rewrite a lower tier's embedded ``[[ref:N]]`` markers to a visually
    distinct, non-resolvable form before ``text`` is rendered as a PARENT
    tier's evidence.

    ``[[ref:N]]`` becomes ``(child ref N)`` — unambiguous both from this
    tier's OWN ``[[ref:N]]`` ordinal space and from a first-order unit's
    bracketed ``[N]`` signal index, while still preserving the information
    that the child cited something there. Pure / idempotent; text with no
    embedded marker (the overwhelming common case: first-order unit bodies
    never contain ``[[ref:N]]``) is returned unchanged with no allocation
    beyond the input.
    """
    if not text or "[[ref:" not in text:
        return text
    return _CHILD_REF_REWRITE_RE.sub(lambda m: f"(child ref {m.group(1)})", text)


# A2 (verify-path structural fix, 2026-07-31) — bound on the UNMARKED-BASIS
# citation fallback (see ``_run``'s CITE block): when the model's ``[[ref:N]]``
# prose resolves to NOTHING despite a real basis, cite the basis directly rather
# than shipping an empty citations array. Capped so the rare fallback can't
# balloon the payload with the per-citation ``evidence_text``.
_FALLBACK_BASIS_CITATIONS_CAP = 25


def composition_body_cap(n_inputs: int) -> int:
    """Per-input body-excerpt cap, sized from the SHARED input-token budget (F-D).

    The findings block may claim :data:`COMPOSITION_SLICE_BUDGET_SHARE` of
    ``LEGBA_LLM_INPUT_TOKEN_BUDGET``, split evenly across the inputs, then
    clamped into ``[MAX_BODY_CHARS, MAX_FULL_BODY_CHARS]``.

    The FLOOR is what makes this safe to ship: however small the budget, or
    however many inputs a world read degrades into, every input still renders at
    least the historical 600-char excerpt. Nothing is ever dropped for budget —
    the slice is already count-capped upstream (``MAX_INPUT_FINDINGS`` /
    ``MAX_WORLD_INPUT_FINDINGS``) and a dropped input is a country the world read
    cannot see, which is a worse failure than a wide turn.
    """
    usable = int(budget_chars() * COMPOSITION_SLICE_BUDGET_SHARE)
    per_row = usable // max(int(n_inputs), 1)
    return max(MAX_BODY_CHARS, min(per_row, MAX_FULL_BODY_CHARS))


def _build_composition_citation(
    n: int, src_row: Mapping[str, Any],
) -> dict[str, Any] | None:
    """One resolved composition-citation entry for basis/periphery row
    ``src_row`` at ordinal ``n``.

    Shared by the resolved-``[[ref:N]]`` loop and the A2 unmarked-basis fallback
    in :func:`_run`'s CITE block — the SAME shape either way (only the caller
    decides whether to stamp ``"resolution": "fallback_basis"``). Returns
    ``None`` when the row carries no resolvable drill-target id (never a
    fabricated ref, mirroring the unit path's malformed-id handling).
    """
    uid = _coerce_uuid(src_row.get("id"))
    if uid is None:
        return None
    citation: dict[str, Any] = {
        "marker": f"[[ref:{n}]]",
        "ordinal": n,
        "ref_id": str(uid),
        "ref_kind": "finding",
    }
    # C-TIER: a citation resolving into the PERIPHERY section carries its tier
    # so the verify pass can require hedged attribution on any clause resting
    # only on it. Basis citations are byte-identical (no key).
    if src_row.get(_EVIDENCE_TIER_KEY) == PERIPHERY_TIER:
        citation["tier"] = PERIPHERY_TIER
    src = src_row.get("analyst_id")
    if src:
        citation["source"] = str(src)
    # S2-T4: the cited head's DESK (target_id) — names which desk a clause
    # rests on (the thematic prompt cites by desk) and keys the cross-desk
    # correlation guard's audit. Additive; absent on a target-less block.
    tgt = src_row.get("target_id")
    if tgt is not None:
        citation["target_id"] = str(tgt)
    title = src_row.get("title")
    if title is not None:
        citation["title"] = str(title)
    # P3-T3/T7 — capture the sub-claim's EVIDENCE the verifier needs, point-in-
    # time, so the composition verify runs DB-free. ``data`` is open JSONB so
    # all three keys are additive.
    #   * evidence_text     — the cited sub-claim's body (judge evidence).
    #   * effective_confidence — the verify-floored min(conf, faithful) the
    #     reader surfaced (the T7 hedge/cap ceiling). Guarded: a row with no
    #     eff score is simply omitted → never falsely capped.
    #   * derived_from      — the sub-claim's underlying lineage/signal ids
    #     (the T7 shared-lineage / double-count detector).
    citation["evidence_text"] = str(src_row.get("body") or "")[
        :MAX_EVIDENCE_TEXT_CHARS
    ]
    eff = src_row.get("effective_confidence")
    if eff is not None:
        try:
            citation["effective_confidence"] = float(eff)
        except (TypeError, ValueError):
            pass
    citation["derived_from"] = [
        str(u) for u in (src_row.get("derived_from") or [])
    ]
    return citation


# ---------------------------------------------------------------------------
# S2-T4 — cross-desk CORRELATION GUARD (the plan's T7 guard)
# ---------------------------------------------------------------------------
#
# Sibling desk-units can rest on the SAME underlying wire signal (a shared hop
# one level down the lineage), so fusing two desks' escalation heads that cite one
# shared signal must NOT double-count that evidence. This mirrors the verify-time
# T7 floor (``verify._deterministic_floor_subclaim`` / ``_correlated_components``)
# but runs at SYNTH time over the composition's OWN ``data['citations']`` so the
# de-duplication is stamped into the FINDING for auditability (not just the paired
# critique). Both consume the same signal captured on each citation at synth time:
# ``derived_from`` (the cited head's underlying lineage/signal ids) +
# ``effective_confidence`` (its verify-floored ``min(conf, faithful)``).


def _correlated_ordinal_components(
    ordinals: Sequence[int],
    derived_by_ordinal: Mapping[int, set[str]],
) -> list[list[int]]:
    """Connected components over cited-head ORDINALS, joined when their
    ``derived_from`` sets intersect. Each component = ONE independent evidence
    unit. Pure-stdlib union-find; O(n^2) pairwise, fine at composition scale.
    """
    ids = list(ordinals)
    parent: dict[int, int] = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            da = derived_by_ordinal.get(ids[i]) or set()
            db = derived_by_ordinal.get(ids[j]) or set()
            if da and db and (da & db):
                union(ids[i], ids[j])

    comps: dict[int, list[int]] = {}
    for i in ids:
        comps.setdefault(find(i), []).append(i)
    return [sorted(c) for c in comps.values()]


def _correlation_guard(citations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Detect + de-duplicate shared-lineage evidence across the cited desk heads.

    Two cited heads whose ``derived_from`` sets intersect are ONE independent
    evidence unit, not two — counting both inflates the fused read. Returns an
    audit dict (stamped into ``finding.data['correlation_guard']``):

      * ``cited_heads``            — number of citations with a resolvable ordinal.
      * ``independent_components`` — number of components after collapsing shared
        lineage (``< cited_heads`` ⇒ at least one duplicate was folded).
      * ``shared_lineage_detected`` — True iff any component has >1 member.
      * ``correlated_groups``      — one entry per multi-member component naming its
        ``ordinals``, ``desks`` (the target ids), and the ``shared_signals`` that
        joined them — the audit of WHAT the guard folded.
      * ``dedup_confidence_ceiling`` — the DE-DUPLICATED ceiling: the max, over
        INDEPENDENT components, of each component's max ``effective_confidence``
        (never a sum / noisy-OR that grows with correlated duplicates). ``None``
        when NO citation carried an effective_confidence (never a fabricated cap).

    HONEST: a citation missing ``derived_from`` forms its own singleton component
    (never falsely correlated); a citation missing ``effective_confidence`` simply
    doesn't contribute to the ceiling.
    """
    ordinals: list[int] = []
    seen: set[int] = set()
    derived_by_ord: dict[int, set[str]] = {}
    eff_by_ord: dict[int, float] = {}
    desk_by_ord: dict[int, str] = {}
    for c in citations:
        if not isinstance(c, Mapping):
            continue
        n = c.get("ordinal")
        if not isinstance(n, int) or isinstance(n, bool) or n in seen:
            continue
        seen.add(n)
        ordinals.append(n)
        df = c.get("derived_from")
        derived_by_ord[n] = (
            {str(x) for x in df if x is not None and str(x)}
            if isinstance(df, (list, tuple))
            else set()
        )
        eff = c.get("effective_confidence")
        if eff is not None:
            try:
                eff_by_ord[n] = float(eff)
            except (TypeError, ValueError):
                pass
        desk = c.get("target_id") or c.get("source")
        if desk:
            desk_by_ord[n] = str(desk)

    components = _correlated_ordinal_components(ordinals, derived_by_ord)

    rep_effs: list[float] = []
    correlated_groups: list[dict[str, Any]] = []
    for comp in components:
        comp_effs = [eff_by_ord[n] for n in comp if n in eff_by_ord]
        if comp_effs:
            rep_effs.append(max(comp_effs))
        if len(comp) > 1:
            shared: set[str] = set()
            for a_i in range(len(comp)):
                for b_i in range(a_i + 1, len(comp)):
                    shared |= (
                        derived_by_ord.get(comp[a_i], set())
                        & derived_by_ord.get(comp[b_i], set())
                    )
            correlated_groups.append(
                {
                    "ordinals": comp,
                    "desks": sorted({desk_by_ord[n] for n in comp if n in desk_by_ord}),
                    "shared_signals": sorted(shared),
                }
            )

    return {
        "cited_heads": len(ordinals),
        "independent_components": len(components),
        "shared_lineage_detected": bool(correlated_groups),
        "correlated_groups": correlated_groups,
        "dedup_confidence_ceiling": max(rep_effs) if rep_effs else None,
    }


def build_prompt_module() -> Any:
    """Construct and return the DSPy module bound to this analyst kind.

    Wave B prereq #4: backfills the dspy.Module surface for the L-176
    optimizer.  Lazy-imports so this file imports cleanly when dspy
    isn't installed; raises :class:`ModuleNotFoundError` otherwise,
    matching the inline_target contract.
    """
    from legba.prompts.meta_findings_synthesizer.v1 import build as _build
    return _build()


# ---------------------------------------------------------------------------
# Helpers — input shaping
# ---------------------------------------------------------------------------


def _coerce_uuid(raw: Any) -> UUID | None:
    """Best-effort coerce of a row id into a UUID, swallowing malformed ids."""
    if raw is None:
        return None
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _extract_input_salience(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The stamped ``salience`` dict of a composition INPUT finding.

    A finding's ``data`` column is the FindingPayload ENVELOPE, so the stamped
    ``FindingPayload.data['salience']`` lands at ``data -> data -> salience``.
    Returns that dict, or ``None`` when the input is unstamped / malformed (it
    then contributes nothing to the sort or the propagation)."""
    env = row.get("data")
    if not isinstance(env, Mapping):
        return None
    inner = env.get("data")
    if not isinstance(inner, Mapping):
        return None
    sal = inner.get("salience")
    return sal if isinstance(sal, Mapping) else None


def _input_salience_magnitude(row: Mapping[str, Any]) -> float:
    """S-2b sort primary: the consequence magnitude stamped on a composition
    input finding, or ``-1.0`` when unstamped (sorts LAST — unscored, never
    mistaken for low-consequence). See ``signal_salience.magnitude_of``."""
    from .signal_salience import magnitude_of

    return magnitude_of(_extract_input_salience(row))


def _verified_claim_texts(row: Mapping[str, Any]) -> list[str]:
    """The SUPPORTED claim texts from this input's faithfulness verify ledger.

    R2. The ledger (``data.verification.claim_verdicts``) has been persisted per
    finding since P2-4 and read by nothing but the UI; the composition gather now
    projects it (``read_other_analyst_findings``'s lateral). Only ``supported``
    rows are returned — a contradiction between two claims the verify pass already
    rejected is not news, and building a tension block out of failed claims would
    hand the composition our own errors as evidence.

    Tolerates the two shapes asyncpg hands back (parsed list, or a JSON string)
    and every malformed row in between; a row it cannot read contributes nothing.
    """
    raw = row.get("claim_verdicts")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("verdict") != "supported":
            continue
        text = entry.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text.strip())
    return out


def _render_salience_lead_block(sliced: Sequence[Mapping[str, Any]]) -> str:
    """S-2b: a compact directive telling the composition model its sub-claim
    blocks are ordered by CONSEQUENCE and to LEAD with the most consequential
    development (or say why not). Returns ``""`` when NO input is scored (nothing
    to order by yet — before S-1d propagates, the compose is byte-for-byte)."""
    top_mag = -1.0
    for row in sliced:
        m = _input_salience_magnitude(row)
        if m > top_mag:
            top_mag = m
    if top_mag < 0.0:
        return ""
    return (
        "SALIENCE ORDERING — the sub-claim blocks below are ordered by "
        "CONSEQUENCE (the `salience=` value on each attribution line, 0=trivial, "
        "1=world-moving); block [[ref:1]] is the highest-consequence read this "
        "cycle. LEAD your BLUF with the most consequential development, or state "
        "explicitly why the lead sits elsewhere (e.g. a higher-consequence read "
        "that is lower-confidence, or the top read being stale). Do NOT bury a "
        "high-salience development beneath routine ones just because more blocks "
        "mention the routine matter — magnitude is not vote-count."
    )


# S-3: the magnitude gap between the top input and the LEAD-cited input beyond
# which the advisory flags a BURIED lead. 0.30 ≈ one full consequence band (e.g.
# a kinetic 0.9 lead vs a routine-procurement 0.2 lead) — a real burial, not
# ordinary hedging. Advisory-only; NEVER gates.
_SALIENCE_LEAD_GAP: float = 0.30


def _build_salience_check(
    comp_salience: Mapping[str, Any],
    sliced: Sequence[Mapping[str, Any]],
    resolved_ords: Sequence[int],
) -> dict | None:
    """S-3 ADVISORY salience judge — did the composition's LEAD open on its
    highest-consequence input?

    ``_orient`` sorts the inputs by salience, so the top-magnitude input is
    ``[[ref:1]]`` and ``comp_salience.magnitude`` is that top magnitude. The lead
    citation is ``resolved_ords[0]`` (the FIRST in-range ``[[ref:N]]`` the body
    cites, in first-appearance order). We compare the lead-cited input's
    magnitude to the top; a gap beyond ``_SALIENCE_LEAD_GAP`` flags a BURIED lead
    — the flattening/burial class j5 caught (a routine development led while a
    world-moving one sat lower). This ABSORBS F-1's deferred semantic role: F-1's
    Δconfidence proxy could not read consequence; this reads it directly.

    ADVISORY: the verdict is a stamp on ``data.eval.salience_check`` — it NEVER
    gates, floors, or alters confidence. Returns ``None`` (no stamp) only when
    the composition carries no scored top input; an uncited lead yields a
    ``pass=None`` (not-judgeable) verdict, not a silent skip."""
    from .signal_salience import magnitude_of

    top_mag = magnitude_of(comp_salience)
    if top_mag < 0.0:
        return None
    top_title = comp_salience.get("top_title")
    top_title = top_title[:160] if isinstance(top_title, str) else None
    if not resolved_ords:
        return {
            "pass": None,
            "top_magnitude": round(top_mag, 3),
            "top_title": top_title,
            "lead_ref": None,
            "lead_magnitude": None,
            "gap": None,
            "reason": "no resolvable [[ref:N]] citation — the lead is not judgeable",
        }
    lead_ref = int(resolved_ords[0])
    lead_row = sliced[lead_ref - 1] if 1 <= lead_ref <= len(sliced) else None
    lead_mag_raw = _input_salience_magnitude(lead_row) if lead_row is not None else -1.0
    lead_mag = None if lead_mag_raw < 0.0 else lead_mag_raw
    gap = (top_mag - lead_mag) if lead_mag is not None else None
    passed = (gap is None) or (gap <= _SALIENCE_LEAD_GAP)
    if lead_mag is None:
        reason = "lead citation carries no salience — not judged against consequence"
    elif passed:
        reason = f"lead opens on a top-consequence input (gap {round(gap, 3)})"
    else:
        reason = (
            f"lead opens on ref {lead_ref} (magnitude {round(lead_mag, 3)}); a "
            f"higher-consequence input exists (magnitude {round(top_mag, 3)}, "
            f"gap {round(gap, 3)}) — possible burial"
        )
    return {
        "pass": bool(passed),
        "top_magnitude": round(top_mag, 3),
        "top_title": top_title,
        "lead_ref": lead_ref,
        "lead_magnitude": (round(lead_mag, 3) if lead_mag is not None else None),
        "gap": (round(gap, 3) if gap is not None else None),
        "reason": reason,
    }


def _orient(
    inputs: Sequence[Mapping[str, Any]],
    *,
    cap: int = MAX_INPUT_FINDINGS,
) -> tuple[list[Mapping[str, Any]], list[UUID], list[str]]:
    """Sort + trim + extract lineage from the finding-row slice.

    Returns ``(trimmed_rows, derived_from_uuids, contributing_analysts)``:

      * ``trimmed_rows`` — newest-first, capped at ``cap`` (default
        ``MAX_INPUT_FINDINGS`` for a per-country read; the world/global path
        passes ``MAX_WORLD_INPUT_FINDINGS`` so it never drops a country).
      * ``derived_from_uuids`` — the row ids of the rows kept, in
        prompt order. Returned so ``run_method`` can hand them to
        :class:`AnalystMethodResult.derived_from` and the substrate-write
        wrapper can stamp the resulting meta-finding's ``derived_from``
        column with them.
      * ``contributing_analysts`` — distinct ``analyst_id`` strings from
        the kept rows, first-seen order. Stamped into the meta-finding's
        ``data.contributing_analysts`` so operators can filter without
        joining the lineage table.

    Malformed-id rows are skipped silently; the rest of the row still
    contributes to the prompt because the LLM doesn't need the UUID. The
    lineage walker tolerates partial ``derived_from`` lists.
    """
    # S-2b: order by CONSEQUENCE first, recency second. Primary = the input
    # finding's stamped salience magnitude (max input salience, propagated up the
    # tower); secondary = produced_at. An UNSTAMPED input gets magnitude -1.0 → it
    # sorts LAST but keeps recency order within the unscored tail, so before S-1d
    # stamps anything the order is byte-for-byte the prior newest-first behavior.
    # produced_at is coerced to a string so a NULL/str value can never collide
    # with datetime rows under `<` (the heterogeneous-key TypeError that once
    # hard-froze the assessors). Both descend under reverse=True (highest
    # magnitude, then newest, leads — so [[ref:1]] is the top-consequence input).
    def _sort_key(row: Mapping[str, Any]) -> tuple[float, str]:
        mag = _input_salience_magnitude(row)
        v = row.get("produced_at")
        if v is None:
            rec = ""
        elif isinstance(v, str):
            rec = v
        else:
            iso = getattr(v, "isoformat", None)
            rec = iso() if callable(iso) else str(v)
        return (mag, rec)

    ordered = sorted(inputs, key=_sort_key, reverse=True)
    if len(ordered) > cap:
        logger.warning(
            "meta_findings_synthesizer.orient TRIMMING %d->%d inputs (cap=%d) — "
            "a dropped input is a country/unit head the composition will NOT see",
            len(ordered), cap, cap,
        )
    trimmed = list(ordered[:cap])

    derived_from: list[UUID] = []
    contributing: list[str] = []
    seen_analysts: set[str] = set()
    for row in trimmed:
        uid = _coerce_uuid(row.get("id"))
        if uid is not None:
            derived_from.append(uid)
        aid = row.get("analyst_id")
        if isinstance(aid, str) and aid and aid not in seen_analysts:
            seen_analysts.add(aid)
            contributing.append(aid)

    logger.debug(
        "meta_findings_synthesizer.orient in=%d kept=%d derived=%d analysts=%d",
        len(inputs), len(trimmed), len(derived_from), len(contributing),
    )
    return trimmed, derived_from, contributing


def _render_user_prompt(
    rows: Sequence[Mapping[str, Any]],
    contributing_analysts: Sequence[str],
    *,
    include_source_ids: bool = False,
) -> str:
    """Render the (already-ORIENTed) finding rows into the synth user prompt.

    Each row is trimmed aggressively — title + analyst attribution +
    confidence + a short body excerpt + up to ``MAX_EVIDENCE_ITEMS`` evidence
    bullets. Findings are already structured so we want compact, scannable
    framing, not the verbose snippet rendering used for raw signals.

    ``include_source_ids`` (P3 per-country composition): when True, each block is
    PREFIXED with its copyable ordinal handle ``[[ref:{i}]]`` (the resolution key
    the CITE block + verify re-derive) and additionally shows ``finding_id=<uuid>``
    (operator/debug provenance only — the model is told to copy the ordinal, NOT
    the uuid) and labels the score ``effective_confidence=`` (the
    ``LEAST(confidence, faithfulness_score)`` fold the verify-floored reader
    projects) so the composition model can CITE each factual clause with a
    ``[[ref:N]]`` marker pointing at the exact sub-claim it rests on. When False
    (the legacy GLOBAL meta) the render is byte-for-byte unchanged — the block head
    stays the unit-style ``[{i}]`` and the model cites by ``analyst_id``, not id.
    """
    header = (
        f"First-order findings to synthesize: {len(rows)}.\n"
        f"Contributing analysts: {', '.join(contributing_analysts) or '(none)'}.\n\n"
    )
    # F-D: the excerpt width comes from the SHARED input-token budget, not from a
    # fixed constant that made this tier read its inputs at ~7% of the leaves'
    # window. Floored at the historical 600 so no render ever narrows.
    body_cap = composition_body_cap(len(rows))
    body_lines: list[str] = []
    for i, row in enumerate(rows, start=1):
        title = str(row.get("title") or "(untitled)")[:MAX_TITLE_CHARS]
        analyst_id = str(row.get("analyst_id") or "(unknown)")
        confidence = row.get("confidence")
        produced_at = row.get("produced_at")
        # Body may live in the row's `body` column (analyst_outputs table) or
        # nested under `data.body` if a caller assembled a richer row dict.
        body = row.get("body")
        if not isinstance(body, str):
            data = row.get("data")
            if isinstance(data, dict):
                inner = data.get("body")
                body = inner if isinstance(inner, str) else ""
            else:
                body = ""
        # Defuse a lower-tier composition's OWN [[ref:N]] markers embedded in
        # its body BEFORE truncating — a truncated marker (cut mid-bracket)
        # would otherwise dodge the rewrite and leave a dangling artifact.
        body = _defuse_child_ref_markers(body)[:body_cap]
        # Evidence likewise — column or nested.
        evidence: list[str] = []
        ev_raw = row.get("evidence")
        if not isinstance(ev_raw, list):
            data = row.get("data")
            if isinstance(data, dict):
                inner = data.get("evidence")
                if isinstance(inner, list):
                    ev_raw = inner
                else:
                    ev_raw = []
            else:
                ev_raw = []
        for e in list(ev_raw)[:MAX_EVIDENCE_ITEMS]:
            evidence.append(_defuse_child_ref_markers(str(e))[:160])
        ev_block = (
            "      evidence:\n" + "\n".join(f"        - {e}" for e in evidence)
            if evidence
            else ""
        )
        # Attribution line. The GLOBAL meta (include_source_ids=False) keeps the
        # legacy byte-for-byte form. The per-country COMPOSITION path surfaces the
        # finding_id (the cite target) + the effective_confidence fold.
        if include_source_ids:
            uid = _coerce_uuid(row.get("id"))
            eff = row.get("effective_confidence")
            conf_val = eff if eff is not None else confidence
            fid_part = f"finding_id={uid} " if uid is not None else ""
            # S-2b: expose the input's CONSEQUENCE magnitude (0..1) so the model
            # can distinguish a world-moving read from a routine one — the blocks
            # are salience-ordered ([[ref:1]] = the top), and this makes the WHY
            # legible. Omitted for an unstamped input (no consequence claim).
            _sal_mag = _input_salience_magnitude(row)
            sal_part = f" salience={_sal_mag:.2f}" if _sal_mag >= 0.0 else ""
            # R3 (2026-08-05): the desk's own SEVERITY call, on the page beside the
            # confidence. The ranking defect (a routine howitzer procurement leading
            # over a war) happened because confidence was THE ONLY NUMBER RENDERED,
            # so an ordering prompt had nothing to order BY. Salience landed here in
            # S-2b; severity is the other half, it is a first-class column the row
            # already carries, and the periphery block has rendered it since it was
            # written. Omitted when the input carries no severity tag — never
            # invented.
            _sev = _row_severity_level(row)
            sev_part = f" severity={_sev}" if _sev else ""
            attribution = (
                f"      analyst_id={analyst_id} {fid_part}"
                f"effective_confidence={conf_val}{sal_part}{sev_part}"
                f" produced_at={produced_at}"
            )
        else:
            attribution = (
                f"      analyst_id={analyst_id} confidence={confidence}"
                f" produced_at={produced_at}"
            )
        # Composition blocks lead with the copyable ordinal handle ``[[ref:{i}]]``
        # (the model is instructed to cite EXACTLY this number); the global meta
        # keeps the byte-for-byte unit-style ``[{i}]`` head.
        head = f"[[ref:{i}]] {title}" if include_source_ids else f"[{i}] {title}"
        body_lines.append(
            f"{head}\n"
            f"{attribution}\n"
            f"      body: {body}"
            + (("\n" + ev_block) if ev_block else "")
        )
    return header + "\n".join(body_lines)


# ---------------------------------------------------------------------------
# C-TIER — periphery selection + rendering (two-tier composition evidence)
# ---------------------------------------------------------------------------


def _row_severity_level(row: Mapping[str, Any]) -> str | None:
    """The ``severity:<level>`` level from a finding row's stamped tags.

    A finding's ``data`` column is the FindingPayload envelope, so its tags land
    at ``data -> 'tags'`` (see the meta-filter note on
    :func:`read_other_analyst_findings`). Tolerates a JSON-encoded ``data``
    string (asyncpg without a JSONB codec). The LAST valid tag wins (the analyst
    contract emits exactly one); absent/unknown → ``None``.
    """
    env = row.get("data")
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except (ValueError, TypeError):
            return None
    if not isinstance(env, Mapping):
        return None
    tags = env.get("tags")
    if not isinstance(tags, (list, tuple)):
        return None
    level: str | None = None
    for tag in tags:
        if not isinstance(tag, str) or not tag.startswith("severity:"):
            continue
        candidate = tag.split(":", 1)[1].strip().lower()
        if candidate in _SEVERITY_RANK:
            level = candidate
    return level


def _row_severity_rank(row: Mapping[str, Any]) -> int:
    """Worst-first sort rank for :func:`_select_periphery` (missing → -1)."""
    level = _row_severity_level(row)
    return _SEVERITY_RANK.get(level, -1) if level is not None else -1


def _row_body_excerpt(row: Mapping[str, Any], cap: int) -> str:
    """The row's body excerpt — column first, then ``data.body`` (the same
    fallback chain :func:`_render_user_prompt` uses), with the SAME
    child-``[[ref:N]]``-marker defuse (periphery rows for a region/world/
    thematic composition are the SAME lower-tier composition heads as the
    basis rows, just below the floor — they carry the identical pollution
    risk)."""
    body = row.get("body")
    if not isinstance(body, str):
        data = row.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                data = None
        inner = data.get("body") if isinstance(data, Mapping) else None
        body = inner if isinstance(inner, str) else ""
    return _defuse_child_ref_markers(body)[:cap]


def _select_periphery(
    rows: Sequence[Mapping[str, Any]], *, cap: int = PERIPHERY_CAP
) -> list[Mapping[str, Any]]:
    """Deterministic worst-first periphery selection: severity rank DESC, then
    recency DESC, then row id (pure tiebreak) — so the cap keeps the items most
    worth surfacing and the same input set always yields the same list. Pure;
    unit-tested for cap + order determinism."""

    def _key(row: Mapping[str, Any]) -> tuple[int, str, str]:
        v = row.get("produced_at")
        if v is None:
            rec = ""
        elif isinstance(v, str):
            rec = v
        else:
            iso = getattr(v, "isoformat", None)
            rec = iso() if callable(iso) else str(v)
        return (_row_severity_rank(row), rec, str(row.get("id") or ""))

    ordered = sorted(rows, key=_key, reverse=True)
    return list(ordered[:cap])


def _periphery_ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The kept periphery rows' finding ids (envelope honesty; malformed ids
    are skipped, mirroring ``_orient``)."""
    out: list[str] = []
    for row in rows:
        uid = _coerce_uuid(row.get("id"))
        if uid is not None:
            out.append(str(uid))
    return out


def _render_periphery_block(
    rows: Sequence[Mapping[str, Any]],
    *,
    start_ordinal: int,
    floor: float | None,
) -> str:
    """Render the PERIPHERY tier as an explicit delimited prompt section.

    Ordinals CONTINUE the basis numbering (``start_ordinal = len(basis)+1``) so
    ``[[ref:N]]`` stays one flat resolution space — the cite phase maps ordinal
    ``N`` to the Nth rendered block across BOTH sections, and the verify pass
    tells the tiers apart by the ``tier`` stamp on the resolved citation, never
    by re-parsing the prompt. Each item carries its honest status
    (``below_floor`` with its score, or ``unverified``) so the model sees WHY
    the item is quarantined. Returns ``""`` for an empty set — the empty-
    periphery prompt is byte-identical to the untiered render.
    """
    if not rows:
        return ""
    floor_txt = (
        f"{float(floor):.2f}" if isinstance(floor, (int, float)) else "(unset)"
    )
    header = (
        "=== WEAKLY-SUPPORTED / UNVERIFIED SIGNALS "
        f"(below the verification floor {floor_txt}) ===\n"
        f"The {len(rows)} item(s) below did NOT clear the verification floor: "
        "each either scored below it on its faithfulness verify "
        "(status=below_floor) or never passed one (status=unverified). They are "
        "NOT established facts and MUST NOT be cited as established fact. Rules "
        "for this section:\n"
        "  - These items may inform HEDGED context only. Any claim resting "
        "solely on an item below MUST be attributed and hedged (e.g. "
        '"weakly-supported reporting suggests ..." / "an unverified read '
        'indicates ..."). The verify pass flags unhedged use.\n'
        "  - Where an item below CONFLICTS with a verified finding above, "
        "SURFACE the tension explicitly — a brief, hedged 'Tensions worth "
        "watching' note naming both sides — never drop it and never blend it "
        "in silently.\n"
        "  - Do NOT let these items set the BLUF or the severity; the verified "
        "findings above are the load-bearing evidence.\n"
        "  - Cite these by their [[ref:N]] handle exactly like the findings "
        "above.\n\n"
    )
    body_lines: list[str] = []
    for i, row in enumerate(rows, start=start_ordinal):
        title = str(row.get("title") or "(untitled)")[:MAX_TITLE_CHARS]
        analyst_id = str(row.get("analyst_id") or "(unknown)")
        produced_at = row.get("produced_at")
        status = "unverified" if row.get("faithfulness_score") is None else "below_floor"
        eff = row.get("effective_confidence")
        try:
            score_part = f" effective_confidence={float(eff):.2f}" if eff is not None else ""
        except (TypeError, ValueError):
            score_part = ""
        level = _row_severity_level(row)
        sev_part = f" severity={level}" if level else ""
        body = _row_body_excerpt(row, PERIPHERY_BODY_CHARS)
        body_lines.append(
            f"[[ref:{i}]] {title}\n"
            f"      analyst_id={analyst_id} status={status}{score_part}{sev_part}"
            f" produced_at={produced_at}\n"
            f"      body: {body}"
        )
    return header + "\n".join(body_lines)


# ---------------------------------------------------------------------------
# CONTINUITY (Phase 1) — the prior-read + open-situation-register refs
# ---------------------------------------------------------------------------


def _resolve_self_analyst_id(descriptor: Any) -> str | None:
    """This composition's OWN ``analyst_id`` (``identity.id``), or ``None``.

    The prior-read lookup needs to know WHOSE previous head to read — a
    country_composition's prior read is another country_composition head, not a
    unit's. ``None`` (a descriptor stub with no identity block) means we cannot
    know, so the prior-read ref is simply OMITTED rather than guessed: an
    unattributable "prior read" is exactly the uncited prior this design exists
    to refuse.
    """
    identity = getattr(descriptor, "identity", None)
    raw = getattr(identity, "id", None) if identity is not None else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


# The PRIOR-READ fetch. Deliberately the SAME admissibility as the basis gather
# (:func:`read_other_analyst_findings` with a ``verify_floor``): the INNER join to
# the latest ``Faithfulness verify%`` critique is the "verify must have run" gate,
# ``LEAST(confidence, faithfulness)`` is the same effective_confidence fold, and
# the coerce-fallback tag drop keeps a garbage body out. Consequences that are
# FEATURES, not gaps:
#   * an unverified prior head (verify never ran / is still pending) is NOT
#     admitted — we would be diffing against something the platform itself has
#     not vouched for;
#   * the current cycle's OWN head does not exist yet at compose time, and
#     ``superseded_by IS NULL`` + newest-first pins the read to the live head —
#     which, at compose time, IS the previous cycle's read;
#   * an honest-EMPTY prior head (the zero-source diagnostic finding) carries no
#     faithfulness critique, so it falls out of the INNER join — a composition
#     never diffs against "we had nothing last cycle".
_PRIOR_READ_SQL_TEMPLATE = """
    SELECT f.id, f.kind, f.title, f.body, f.confidence, f.severity, f.data,
           f.target_id, f.target_version, f.analyst_id, f.analyst_version,
           f.produced_at, f.derived_from, f.schema_uri, f.run_id,
           LEAST(f.confidence, v.faithfulness_score) AS effective_confidence,
           v.faithfulness_score AS faithfulness_score,
           EXTRACT(EPOCH FROM (NOW() - f.produced_at)) / 3600.0 AS age_hours
      FROM analyst_outputs f
      JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE f.kind = 'finding'
       AND f.analyst_id = $1
       AND f.superseded_by IS NULL
       AND f.produced_at > NOW() - make_interval(hours => $2)
       AND LEAST(f.confidence, v.faithfulness_score) >= $3
       AND (f.data -> 'tags' ?| array['unstructured','coerce_failed']) IS NOT TRUE
       AND {target_clause}
     ORDER BY f.produced_at DESC, f.id DESC
     LIMIT 1
"""


async def read_prior_composition_head(
    conn,  # type: ignore[no-untyped-def]
    *,
    analyst_id: str,
    target_id: str | None,
    verify_floor: float | None,
    lookback_hours: int = CONTINUITY_PRIOR_LOOKBACK_HOURS,
) -> dict[str, Any] | None:
    """The SAME target's previous non-superseded, VERIFIED composition head.

    ``target_id`` ``None`` reads the TARGET-LESS head (``f.target_id IS NULL``) —
    the world and thematic compositions write target-less findings (see
    :data:`WORLD_TARGET_TOKEN`), so their "same target" is the target-less lane,
    never a stray desk's head. ``verify_floor`` ``None`` falls back to
    :data:`DEFAULT_VERIFY_FLOOR` so the verify GATE (the INNER join) still
    applies — the floor number is the tunable, the gate is not.

    Returns the row dict stamped with :data:`CONTINUITY_ROW_KEY` =
    :data:`CONTINUITY_PRIOR`, or ``None`` when there is no admissible prior head
    (a FIRST run, a prior head that never cleared verify, or one older than
    ``lookback_hours``). ``None`` is the byte-compatible path: no ref, no block,
    no receipt.
    """
    if not analyst_id:
        return None
    target_clause = "f.target_id IS NULL" if target_id is None else "f.target_id = $4"
    sql = _PRIOR_READ_SQL_TEMPLATE.format(target_clause=target_clause)
    params: list[Any] = [
        str(analyst_id),
        int(lookback_hours),
        float(verify_floor if verify_floor is not None else DEFAULT_VERIFY_FLOOR),
    ]
    if target_id is not None:
        params.append(str(target_id))
    rows = await conn.fetch(sql, *params)
    if not rows:
        return None
    row = dict(rows[0])
    # Never claim a prior read we cannot point at: an id-less / mis-shaped row is
    # dropped rather than rendered as an unciteable "previous read".
    if _coerce_uuid(row.get("id")) is None:
        return None
    row[CONTINUITY_ROW_KEY] = CONTINUITY_PRIOR
    return row


# The OPEN-SITUATION register fetch. "Open" is the same predicate the thematic
# proposer uses over this table (``superseded_by IS NULL`` + not-yet-expired
# validity + not closed) so two readers of the same frame never disagree about
# which situations are live. Ordered worst-first (intensity, then recency) and
# capped, because the register is an ORIENTING index, not a second evidence
# slice.
_SITUATION_REGISTER_SQL_TEMPLATE = """
    SELECT s.id, s.name, s.status, s.category, s.intensity_score, s.event_count,
           s.last_event_at, s.target_id,
           COALESCE(s.valid_from, s.created_at) AS opened_at,
           EXTRACT(EPOCH FROM (NOW() - COALESCE(s.valid_from, s.created_at)))
               / 86400.0 AS age_days
      FROM situations s
     WHERE s.superseded_by IS NULL
       AND (s.valid_until IS NULL OR s.valid_until > NOW())
       AND s.status <> 'closed'
       {target_clause}
     ORDER BY s.intensity_score DESC, s.last_event_at DESC NULLS LAST, s.id DESC
     LIMIT {limit}
"""


async def read_open_situations(
    conn,  # type: ignore[no-untyped-def]
    *,
    target_id: str | None = None,
    target_ids: Sequence[str] | None = None,
    limit: int = SITUATION_REGISTER_CAP,
) -> list[dict[str, Any]]:
    """The bounded, target-scoped register of OPEN situation frames.

    Scoping mirrors how the REST of the composition's slice is scoped — the same
    ``target_id`` / ``target_ids`` split :func:`read_other_analyst_findings`
    takes, for the same reason: a per-country read must not see another desk's
    frames, a region read sees its member desks' frames, and a target-less world /
    thematic read (both filters ``None``) sees the live frames globally. An EMPTY
    ``target_ids`` set is honored (guarded on ``is not None``) and yields ZERO
    rows — the honest empty scope, never an accidental unscoped read.

    Returns compact, JSON-safe dicts. A row missing an id or a name is SKIPPED
    (never padded with a placeholder): the register may only name frames that
    actually exist.
    """
    params: list[Any] = []
    if target_id is not None:
        params.append(str(target_id))
        target_clause = f"AND s.target_id = ${len(params)}"
    elif target_ids is not None:
        params.append([str(t) for t in target_ids])
        target_clause = f"AND s.target_id = ANY(${len(params)}::TEXT[])"
    else:
        target_clause = ""
    sql = _SITUATION_REGISTER_SQL_TEMPLATE.format(
        target_clause=target_clause, limit=int(limit)
    )
    rows = await conn.fetch(sql, *params)
    out: list[dict[str, Any]] = []
    for raw in rows:
        r = dict(raw)
        sid = _coerce_uuid(r.get("id"))
        name = r.get("name")
        if sid is None or not isinstance(name, str) or not name.strip():
            continue
        out.append(
            {
                "situation_id": str(sid),
                "name": name.strip()[:SITUATION_REGISTER_NAME_CHARS],
                "status": str(r.get("status") or "unknown"),
                "intensity_score": _as_float(r.get("intensity_score")),
                "event_count": _as_int(r.get("event_count")),
                "last_event_at": _iso_text(r.get("last_event_at")),
                "opened_at": _iso_text(r.get("opened_at")),
                "age_days": _as_float(r.get("age_days")),
                "target_id": (
                    str(r["target_id"]) if r.get("target_id") is not None else None
                ),
            }
        )
    await _attach_trajectory(conn, out)
    return out


async def _attach_trajectory(
    conn,  # type: ignore[no-untyped-def]
    situations: list[dict[str, Any]],
) -> None:
    """CONTINUITY P2 (plan D5) — enrich each register frame with its TRAJECTORY.

    This is the upgrade from a Phase-1 register to a Phase-2 one. Phase 1 could
    only show a frame's CURRENT numbers, so a composition asked "what changed"
    had to infer movement from a single snapshot — which is precisely the shape
    that invites a model to narrate a trend it cannot see. Each frame now carries
    the ledger's own answer: its trajectory state and its last few DATED deltas,
    each with the date of the EVIDENCE that moved it.

    Mutates ``situations`` in place, adding ``trajectory_state`` and
    ``trajectory`` (a bounded, newest-first list). A frame the ledger has never
    spoken about gets NEITHER key — absent, not defaulted, so "never assessed"
    stays distinguishable from "assessed and steady".

    BEST-EFFORT, DEGRADE-NEVER-BREAK, matching the posture of the whole
    continuity gather: any error logs and leaves the register exactly as Phase 1
    rendered it. A compose never fails because its memory was unavailable.
    """
    if not situations:
        return
    try:
        from ..situations.trajectory import read_current_states, read_trajectories

        ids = [s["situation_id"] for s in situations]
        states = await read_current_states(conn, ids)
        ledger = await read_trajectories(
            conn, ids, per_situation=SITUATION_REGISTER_TRAJECTORY_DEPTH,
        )
    except Exception as exc:  # pragma: no cover — best-effort enrichment
        logger.warning(
            "meta_synth.situation_trajectory.unavailable err=%s — register "
            "renders without trajectory (Phase-1 shape)", exc,
        )
        return
    for entry in situations:
        sid = entry["situation_id"]
        state = states.get(sid)
        if state is None:
            continue
        entry["trajectory_state"] = state
        entry["trajectory"] = [
            {
                "delta": row["delta"],
                "occurred_at": _iso_text(row["occurred_at"]),
                "why": str(row["why"])[:SITUATION_REGISTER_WHY_CHARS],
            }
            for row in ledger.get(sid, ())
        ]


def _iso_text(value: Any) -> str | None:
    """A JSON-safe timestamp string: ISO-8601 for a datetime, the string itself
    when the driver already handed one back, ``None`` otherwise. Wider than
    :func:`_iso_or_none` (datetime-only) because a register entry that silently
    lost its ``last_event_at`` to a str/datetime mismatch would render as
    ``(none)`` — an invented absence, which is the one thing the register must
    never report."""
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    if isinstance(value, str) and value.strip():
        return value
    return None


def _as_float(value: Any) -> float | None:
    """Float coercion that returns ``None`` rather than a fabricated 0.0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    """Int coercion that returns ``None`` rather than a fabricated 0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _continuity_selection(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Split the marked continuity rows into ``(prior_row, register_row)``.

    FIRST-wins per kind (READ_SLICE emits at most one of each; a duplicate would
    be a bug, and taking the first keeps the ordinal space deterministic rather
    than silently renumbering). Either may be ``None`` — both refs are
    independently optional.
    """
    prior: Mapping[str, Any] | None = None
    register: Mapping[str, Any] | None = None
    for row in rows:
        kind = row.get(CONTINUITY_ROW_KEY)
        if kind == CONTINUITY_PRIOR and prior is None:
            prior = row
        elif kind == CONTINUITY_SITUATIONS and register is None:
            register = row
    return prior, register


def _register_situations(row: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """The situation dicts carried on a register row (``[]`` when absent)."""
    if row is None:
        return []
    raw = row.get(CONTINUITY_SITUATIONS_ROW_KEY)
    if not isinstance(raw, (list, tuple)):
        return []
    return [s for s in raw if isinstance(s, Mapping)]


def _render_prior_read_lines(row: Mapping[str, Any], ordinal: int) -> list[str]:
    """The PRIOR READ sub-block — one citable ordinal, dated by its OWN clock."""
    title = str(row.get("title") or "(untitled)")[:MAX_TITLE_CHARS]
    produced_at = _iso_text(row.get("produced_at")) or "(unknown)"
    age = _as_float(row.get("age_hours"))
    age_part = f" age={age:.1f}h" if age is not None else ""
    eff = _as_float(row.get("effective_confidence"))
    eff_part = f" effective_confidence={eff:.2f}" if eff is not None else ""
    analyst_id = str(row.get("analyst_id") or "(unknown)")
    body = _row_body_excerpt(row, CONTINUITY_PRIOR_BODY_CHARS)
    return [
        f"[[ref:{ordinal}]] PRIOR READ (this target's previous verified read): {title}",
        f"      analyst_id={analyst_id} produced_at={produced_at}{age_part}{eff_part}",
        f"      body: {body}",
    ]


def _render_situation_register_lines(
    situations: Sequence[Mapping[str, Any]], ordinal: int
) -> list[str]:
    """The OPEN SITUATION REGISTER sub-block — ONE citable ordinal for the whole
    register (it is a single orienting index, not N pieces of evidence)."""
    lines = [
        f"[[ref:{ordinal}]] OPEN SITUATION REGISTER "
        f"({len(situations)} open frame(s) in scope, highest-intensity first):",
    ]
    for s in situations:
        intensity = s.get("intensity_score")
        intensity_txt = (
            f"{float(intensity):.2f}" if isinstance(intensity, (int, float)) else "n/a"
        )
        events = s.get("event_count")
        events_txt = str(events) if isinstance(events, int) else "n/a"
        age = s.get("age_days")
        age_txt = f"{float(age):.1f}d" if isinstance(age, (int, float)) else "n/a"
        state = s.get("trajectory_state")
        lines.append(
            f"      - {s.get('name')} :: status={s.get('status')} "
            f"intensity={intensity_txt} events={events_txt} "
            f"last_event_at={s.get('last_event_at') or '(none)'} open_for={age_txt}"
            + (f" trajectory={state}" if state else "")
        )
        # CONTINUITY P2 — the frame's own DATED deltas, newest first. This is
        # what turns "what changed since the prior read" from a reconstruction
        # into a quotation: the model is shown the ledger's dated answer and
        # cites the register block for it, so a trajectory claim is graded
        # against the trajectory record rather than against a snapshot.
        for delta in s.get("trajectory") or ():
            if not isinstance(delta, Mapping):
                continue
            lines.append(
                f"          * {delta.get('occurred_at') or '(undated)'} "
                f"{delta.get('delta')}: {delta.get('why')}"
            )
    return lines


def _render_continuity_block(
    prior: Mapping[str, Any] | None,
    situations: Sequence[Mapping[str, Any]],
    *,
    start_ordinal: int,
) -> str:
    """Render the CONTINUITY section — up to two citable blocks, or ``""``.

    Ordinals CONTINUE the basis+periphery numbering (``start_ordinal =
    len(basis) + len(periphery) + 1``) so ``[[ref:N]]`` stays ONE flat resolution
    space: the cite phase maps ordinal ``N`` to the Nth rendered block across all
    three sections, and no consumer has to re-parse the prompt to tell them
    apart (the resolved citation's ``continuity`` stamp does that).

    Returns ``""`` when there is neither a prior read nor an open situation — so
    a FIRST run's prompt is byte-identical to the pre-continuity render.
    """
    if prior is None and not situations:
        return ""
    header = [
        "=== CONTINUITY (what this desk already knew) ===",
        "The block(s) below are the ONLY licensed source of 'before'. Cite them "
        "by their [[ref:N]] handle exactly like any other evidence, state what "
        "CHANGED against them, and take every date from the block itself — never "
        "from the time you are running. If nothing material changed, say that "
        "plainly. Never assert a trend, escalation, or 'ongoing' framing that "
        "these blocks do not support. Where a frame carries dated trajectory "
        "lines, those ARE the record of how it moved — use them for direction "
        "and their dates for timing; where a frame carries none, the system has "
        "no trajectory for it, which is not the same as it being steady.",
        "",
    ]
    body_lines: list[str] = []
    ordinal = start_ordinal
    if prior is not None:
        body_lines.extend(_render_prior_read_lines(prior, ordinal))
        ordinal += 1
    if situations:
        if body_lines:
            body_lines.append("")
        body_lines.extend(_render_situation_register_lines(situations, ordinal))
    return "\n".join(header + body_lines)


async def _gather_continuity_rows(
    conn,  # type: ignore[no-untyped-def]
    *,
    descriptor: Any,
    analyst_ids: Sequence[str],
    verify_floor: float | None,
    prior_target_id: str | None,
    situation_target_id: str | None = None,
    situation_target_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Gather the (at most two) marked CONTINUITY rows for a composition read.

    BEST-EFFORT by contract: this is an ADDITIVE enrichment on top of an already
    complete slice, so ANY failure (a missing relation, a degraded read replica,
    a descriptor without an identity block) logs and yields NO continuity rows.
    A composition must never fail — or silently lose its evidence slice —
    because its memory was unavailable. The same posture the actor layer takes
    around ``read_open_contention``.

    The two refs are gathered INDEPENDENTLY (each in its own try) so a failure of
    one never suppresses the other.

    An EMPTY ``analyst_ids`` short-circuits to ``[]`` with NO query — extending
    ``read_other_analyst_findings``'s "refuse the query rather than scan" contract
    to the continuity reads. A composition whose source roster resolved to nothing
    emits an honest-empty head with no LLM call, so there is no prose for a memory
    to annotate; querying anyway would spend two reads to feed a prompt that is
    never rendered.
    """
    if not analyst_ids:
        return []
    out: list[dict[str, Any]] = []

    self_analyst_id = _resolve_self_analyst_id(descriptor)
    if self_analyst_id:
        try:
            prior = await read_prior_composition_head(
                conn,
                analyst_id=self_analyst_id,
                target_id=prior_target_id,
                verify_floor=verify_floor,
            )
            if prior is not None:
                out.append(prior)
        except Exception as exc:  # pragma: no cover — best-effort enrichment
            logger.warning(
                "meta_findings_synthesizer.continuity.prior_read.failed "
                "analyst_id=%s target_id=%s err=%s",
                self_analyst_id, prior_target_id, exc,
            )

    try:
        situations = await read_open_situations(
            conn,
            target_id=situation_target_id,
            target_ids=situation_target_ids,
        )
    except Exception as exc:  # pragma: no cover — best-effort enrichment
        logger.warning(
            "meta_findings_synthesizer.continuity.situations.failed "
            "target_id=%s err=%s",
            situation_target_id, exc,
        )
        situations = []
    if situations:
        out.append(
            {
                CONTINUITY_ROW_KEY: CONTINUITY_SITUATIONS,
                CONTINUITY_SITUATIONS_ROW_KEY: situations,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Helpers — output coercion
# ---------------------------------------------------------------------------

_EVIDENCE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _looks_like_resolvable_evidence(item: str) -> bool:
    """True when ``item`` is a genuinely resolvable evidence identifier: an
    absolute http(s) URL or a UUID.

    P2 gallery finding #3 (evidence-field contamination): a composition
    finding cites structurally via ``[[ref:N]]`` markers IN THE BODY, resolved
    to ``data.citations`` by the CITE step in :func:`_run` — the ``evidence``
    array is a legacy per-unit-analyst field the model tends to fill by
    echoing its OWN citation markers back (bare ints like ``"2"``/``"14"``,
    bracket-style ``"[1]"``, or composition-tier ``"ref:1"`` / ``"[[ref:16]]"``
    strings — see the live captures in the P2 gallery: three different
    schemes, all meaningless once detached from the tier that emitted them).
    None of that is resolvable on its own; a genuine URL or UUID is. Used by
    :func:`_coerce_finding` to filter the model's own ``evidence`` list so a
    composition's stored ``evidence`` never becomes scheme soup a LATER,
    higher tier then renders as if it meant something (the SAME contamination
    :func:`_defuse_child_ref_markers` defuses on the render side for
    ``[[ref:N]]``-shaped body/evidence text already in the wild).
    """
    text = item.strip()
    if not text:
        return False
    if _EVIDENCE_URL_RE.match(text):
        return True
    try:
        UUID(text)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _coerce_finding(
    raw: str,
    *,
    fallback_title: str,
    contributing_analysts: Sequence[str],
) -> FindingPayload:
    """Parse the LLM JSON response into a :class:`FindingPayload`.

    Always stamps ``data.meta = True`` and ``data.contributing_analysts``
    so downstream filters can find meta-findings without joining lineage.
    Fail-safe parsing mirrors the sibling kinds: malformed JSON degrades
    to a low-confidence finding carrying the raw body, leaving the actor's
    output-row landing to the iglu-schema validator (which routes truly
    malformed payloads to the DLQ at write time).
    """
    meta_marks = {
        "meta": True,
        "contributing_analysts": list(contributing_analysts),
    }

    parsed: Any
    try:
        candidate = raw.strip()
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.lower().startswith("json"):
                candidate = candidate[4:]
            candidate = candidate.strip()
        if candidate.startswith("{"):
            depth = 0
            end = len(candidate)
            for i, c in enumerate(candidate):
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            candidate = candidate[:end]
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("meta_findings_synthesizer.finding.parse_failed err=%s", exc)
        return FindingPayload(
            title=fallback_title[:200],
            body=raw[:32000],
            confidence=0.3,
            tags=["unstructured", "meta"],
            data={**meta_marks, "raw_llm_response": raw[:8000]},
        )

    if not isinstance(parsed, dict):
        return FindingPayload(
            title=fallback_title[:200],
            body=str(parsed)[:32000],
            confidence=0.3,
            tags=["unstructured", "meta"],
            data={**meta_marks, "raw_llm_response": raw[:8000]},
        )

    try:
        tags_in = [str(t) for t in (parsed.get("tags") or [])][:50]
        # Stamp the meta tag idempotently so downstream filters can match
        # without parsing the JSONB data column.
        if "meta" not in tags_in:
            tags_in.append("meta")
        # P2 gallery finding #3: keep only GENUINELY RESOLVABLE evidence
        # identifiers (a URL or a UUID) — never the model's own bare-int /
        # bracket / [[ref:N]] citation-scheme echo, which means nothing once
        # this composition's `evidence` array is copied forward and rendered
        # by a HIGHER tier (see :func:`_looks_like_resolvable_evidence`). An
        # evidence list with nothing resolvable stays EMPTY rather than
        # carrying scheme soup.
        evidence_in = [
            str(e) for e in (parsed.get("evidence") or [])
            if _looks_like_resolvable_evidence(str(e))
        ][:50]
        return FindingPayload(
            title=str(parsed.get("title") or fallback_title)[:2048],
            body=str(parsed.get("body") or "")[:65536],
            confidence=float(parsed.get("confidence", 0.5)),
            evidence=evidence_in,
            tags=tags_in,
            data={**meta_marks, "raw_llm_response": raw[:8000]},
        )
    except Exception as exc:
        logger.warning("meta_findings_synthesizer.finding.coerce_failed err=%s", exc)
        return FindingPayload(
            title=fallback_title[:200],
            body=raw[:32000],
            confidence=0.3,
            tags=["coerce_failed", "meta"],
            data={**meta_marks, "raw_llm_response": raw[:8000]},
        )


# ---------------------------------------------------------------------------
# Composition supersession signature (S8-T3)
# ---------------------------------------------------------------------------


COMPOSITION_SIG_PREFIX: str = "composition"
"""Prefix for a composition head's supersession signature. Distinct from the
content-derived ``sig:`` / explicit-situation ``sit:`` prefixes so a composition
head can never collide with a unit finding's content signature."""

WORLD_TARGET_TOKEN: str = "world"
"""Target token for the WORLD composition head (its ``target_id`` is NULL)."""


def _composition_signature(
    analyst_id: str | None,
    target_id: str | None,
) -> str:
    """Per-head supersession signature for a meta-composition FindingPayload.

    meta_findings_synthesizer compositions carry no entity/topic content, so
    :func:`finding_supersession.derive_signature` cannot derive a signature and
    the heads never cluster — every cadence cycle leaves ANOTHER live head (the
    live symptom the S8-T3 fix targets: ~8 concurrent US composition heads
    reachable by the read/findings API, hidden from the FUSION read only by its
    ``DISTINCT ON (analyst_id, target_id)`` belt). Stamping this signature onto
    ``FindingPayload.data['situation_signature']`` gives the supersession
    clusterer an explicit key (``derive_signature`` reads the nested payload
    ``data`` sub-dict too, so the persisted ``analyst_outputs.data`` column
    surfaces it).

    The finding_supersession cluster key is ``(situation_signature, analyst_id)``
    so the signature MUST encode ``target_id`` — a bare per-analyst signature
    would collapse ALL of one analyst's per-country compositions into a SINGLE
    head. The world composition (``target_id`` NULL) uses the ``'world'`` literal.
    Keeping ``analyst_id`` in the string as well makes it self-descriptive and
    lets a future sibling composition kind (S2-T2 region_composition) reuse this
    helper without collision.
    """
    target = str(target_id) if target_id else WORLD_TARGET_TOKEN
    aid = str(analyst_id) if analyst_id else "unknown"
    return f"{COMPOSITION_SIG_PREFIX}:{aid}:{target}"


# ---------------------------------------------------------------------------
# Substrate-read helper — other-analyst findings slice
# ---------------------------------------------------------------------------


async def read_other_analyst_findings(
    conn: asyncpg.Connection,
    *,
    analyst_ids: Sequence[str],
    time_window_hours: int = 24,
    limit: int = 100,
    target_id: str | None = None,
    target_ids: Sequence[str] | None = None,
    verify_floor: float | None = None,
    include_meta: bool = False,
    dedupe_heads: bool = False,
) -> list[dict[str, Any]]:
    """Fetch ``analyst_outputs`` rows where ``kind='finding'`` for a set
    of source analysts.

    Mirrors the column projection of the sibling read helpers
    (:func:`legba.data.analysts.cross_target_raw.read_cross_target_slice`,
    :func:`legba.runtime.dapr_actors._read_substrate_slice`) so finding
    rows are interchangeable with signal rows at the actor layer — the
    runtime dispatcher doesn't need a per-kind switch on row shape.

    The query intentionally:
      * scopes to ``kind = 'finding'`` (first-order findings only — meta
        findings have ``data.data.meta=True`` and are excluded so the
        synthesizer doesn't recurse on its own output);
      * filters ``analyst_id = ANY(...)`` so the subscription's
        ``other_analysts`` set is the only source;
      * walks newest-first within the time window.

    Empty ``analyst_ids`` short-circuits to ``[]`` — refusing the query is
    safer than scanning the entire ``analyst_outputs`` table when the
    subscription resolved no source analysts.

    P3 per-country composition
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    Two OPTIONAL, additive filters — both ``None`` reproduces the legacy
    global-meta query byte-for-byte (so the existing global synthesizer is
    unaffected):

      * ``target_id`` — when set, restrict the slice to sub-claims produced
        for THIS country target (``target_id = $N``). The runtime passes the
        run's ``target_filter`` here, so a per-country composition reads ONLY
        that country's unit findings, not the whole G20 cross-section.
      * ``target_ids`` (S2-T2 REGION composition) — when set, restrict the slice
        to a SET of member-country targets (``target_id = ANY($N::TEXT[])``). A
        region reads the country_composition HEAD for EACH of its member desks,
        so its scope is the member SET rather than the single-country equality.
        Mutually exclusive with ``target_id`` (``target_id`` wins if both are
        passed). An EMPTY set yields ``= ANY(ARRAY[]::TEXT[])`` → ZERO rows (the
        honest region-gap: a region with no member desks reads nothing), NEVER an
        unscoped whole-pool read. Like ``target_id`` it forces the composition
        head-fold (``superseded_by IS NULL`` + one-head-per-``(analyst,target)``
        ``DISTINCT ON``), so a region reads exactly ONE country_composition head
        per member country.
      * ``verify_floor`` — when set, admit ONLY sub-claims that PASSED the
        faithfulness-verify pass above the floor. An INNER ``JOIN LATERAL`` to
        the paired ``kind='critique'`` faithfulness row (``title LIKE
        'Faithfulness verify%'``) both (a) EXCLUDES findings with no verify
        critique (verify never ran → not admissible) and (b) exposes the
        verify score so ``effective_confidence = LEAST(f.confidence,
        faithfulness_score)`` — the same fold
        :func:`legba.data.registry.substrate_reads_api._hydrate_finding`
        surfaces — can be floored. Verify-FAILED (low score) rows fall below
        the floor; ``unstructured`` / ``coerce_failed`` coerce-fallback rows
        (a garbage body is *vacuously* faithful, so the score alone won't drop
        them) are excluded by tag. Nothing is fabricated: an empty admissible
        set yields ``[]`` and the synth's empty-slice path narrates the gap
        honestly.

    Note on the meta-filter path: :func:`legba.data.provenance.writes.
    _insert_analyst_output` stores ``payload.model_dump(mode="json")`` in
    the ``data`` JSONB column — i.e. the full FindingPayload, with the
    payload's own ``data`` field nested one level deeper. So a meta-marked
    finding has its flag at ``data -> 'data' ->> 'meta' = 'true'``, not
    at the top level, and the finding's own ``tags`` array lands at
    ``data -> 'tags'``. The query reflects that. If the storage layout
    changes (L-190 split into per-kind tables), update this query and
    the matching test.

    ``include_meta`` (P3-T5 GLOBAL/world composition): default ``False`` keeps
    the meta-exclusion clause so the byte-identical legacy behavior holds for
    ALL existing callers (a first-order synth must never recurse on its own
    meta output). When ``True`` the clause is DROPPED — the world composition
    reads country_composition findings, which ARE ``meta=True``; without this
    the world slice would be silently zeroed (the highest-risk item — locked by
    a test).
    """
    if not analyst_ids:
        return []

    params: list[Any] = [list(analyst_ids), int(time_window_hours)]
    where: list[str] = [
        "f.kind = 'finding'",
        "f.analyst_id = ANY($1::TEXT[])",
        "f.produced_at > NOW() - make_interval(hours => $2)",
    ]
    if not include_meta:
        where.append("(f.data -> 'data' ->> 'meta') IS DISTINCT FROM 'true'")

    # P4 content-audit fix (2026-07-01): COMPOSITION reads (per-country target
    # scope, or the world include_meta read) must fold to exactly ONE HEAD per
    # (unit, country). Drop superseded prior-cycle findings so ``derived_from``
    # can't double-count a single unit across stale dupes — the audit found
    # compositions narrating "both leadership-transition units"/"the
    # energy-security units" (plural) when one fresh unit had 1..N superseded
    # prior-cycle rows still in the window. The head-per-(analyst_id,target_id)
    # DISTINCT ON below is the belt to this suspenders (covers the case where
    # supersession lagged and left >1 non-superseded row). The legacy
    # global-meta path (both filters off) is left BYTE-FOR-BYTE unchanged.
    #
    # S2-T4 THEMATIC composition: ``dedupe_heads`` forces this head-fold for a
    # target-LESS, analyst-dimension read (one head per DESK of a UNIT across ALL
    # desks) — none of the target/meta filters is set there, so it needs its own
    # switch. DISTINCT ON (analyst_id, target_id) with a single constant analyst_id
    # then yields exactly one head per target_id (one per desk).
    dedupe_composition = (
        target_id is not None
        or target_ids is not None
        or include_meta
        or dedupe_heads
    )
    if dedupe_composition:
        where.append("f.superseded_by IS NULL")

    if target_id is not None:
        params.append(str(target_id))
        where.append(f"f.target_id = ${len(params)}")
    elif target_ids is not None:
        # S2-T2 REGION composition: a SET of member-country targets. The empty
        # set is kept (guarded on ``is not None``, not truthiness) so a region
        # with no member desks reads ZERO rows — the honest gap — instead of
        # silently dropping the filter and reading every country.
        params.append([str(t) for t in target_ids])
        where.append(f"f.target_id = ANY(${len(params)}::TEXT[])")

    join = ""
    select_extra = ""
    if verify_floor is not None:
        # INNER JOIN the LATEST faithfulness-verify critique for this finding.
        # INNER (not LEFT) is the "verify must have run" gate — unverified
        # sub-claims never enter the composition. The score → effective_confidence
        # fold mirrors substrate_reads_api._hydrate_finding.
        # R2 (2026-08-05): the same lateral now also lifts the CLAIM LEDGER the
        # verify pass already wrote for this finding. It has been on disk since
        # P2-4 and no Python has ever read it — the composition gathered its
        # inputs' SCORES and never their CLAIMS, which is precisely why it could
        # not notice that two of them asserted incompatible states of the same
        # fact. One extra projected column, no extra query, no new join.
        join = """
            JOIN LATERAL (
                SELECT (cr.data->>'overall_score')::real AS faithfulness_score,
                       cr.data->'data'->'verification'->'claim_verdicts'
                           AS claim_verdicts
                  FROM analyst_outputs cr
                 WHERE cr.kind = 'critique'
                   AND cr.data->>'analyzed_output_id' = f.id::text
                   AND cr.data->>'overall_score' IS NOT NULL
                   AND cr.title LIKE 'Faithfulness verify%'
                 ORDER BY cr.produced_at DESC, cr.id DESC
                 LIMIT 1
            ) v ON TRUE
        """
        params.append(float(verify_floor))
        where.append(f"LEAST(f.confidence, v.faithfulness_score) >= ${len(params)}")
        # Drop coerce-fallback rows even when they score as vacuously faithful.
        where.append(
            "(f.data -> 'tags' ?| array['unstructured','coerce_failed']) IS NOT TRUE"
        )
        select_extra = (
            ", LEAST(f.confidence, v.faithfulness_score) AS effective_confidence,"
            " v.faithfulness_score AS faithfulness_score,"
            " v.claim_verdicts AS claim_verdicts"
        )

    _cols = (
        "f.id, f.kind, f.title, f.body, f.confidence, f.severity, f.data, "
        "f.target_id, f.target_version, f.analyst_id, f.analyst_version, "
        "f.produced_at, f.derived_from, f.schema_uri, f.run_id"
    )
    if dedupe_composition:
        # DISTINCT ON (analyst_id, target_id) newest-first → exactly one HEAD per
        # unit per country (per-country: target_id is constant → one row per unit;
        # world: analyst_id is constant → one row per country). The outer wrapper
        # restores the newest-first slice ordering + LIMIT the caller expects.
        sql = f"""
        SELECT * FROM (
            SELECT DISTINCT ON (f.analyst_id, f.target_id)
                   {_cols}{select_extra}
            FROM analyst_outputs f
            {join}
            WHERE {' AND '.join(where)}
            ORDER BY f.analyst_id, f.target_id, f.produced_at DESC, f.id DESC
        ) dedup
        ORDER BY dedup.produced_at DESC
        LIMIT {int(limit)}
        """
    else:
        sql = f"""
        SELECT {_cols}{select_extra}
        FROM analyst_outputs f
        {join}
        WHERE {' AND '.join(where)}
        ORDER BY f.produced_at DESC
        LIMIT {int(limit)}
        """
    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def read_periphery_findings(
    conn,  # type: ignore[no-untyped-def]
    *,
    analyst_ids: Sequence[str],
    time_window_hours: int,
    floor: float,
    limit: int = 32,
    target_id: str | None = None,
    target_ids: Sequence[str] | None = None,
    include_meta: bool = False,
) -> list[dict[str, Any]]:
    """C-TIER — gather the PERIPHERY tier: what the basis bar EXCLUDED.

    The exact COMPLEMENT of the :func:`read_other_analyst_findings`
    ``verify_floor`` admissibility over the same scope (same analyst set,
    window, target scope, meta filter, head-fold dedupe, coerce-tag drop),
    inverted on the verify leg:

      * LEFT (not INNER) join to the latest ``Faithfulness verify%`` critique —
        an UNVERIFIED head is periphery (claim-bearing but ungraded), not
        invisible;
      * admitted iff ``v.faithfulness_score IS NULL`` (unverified) OR
        ``LEAST(confidence, faithfulness) < floor`` (verify-scored below the
        bar) — i.e. exactly the rows the basis gather refuses;
      * coerce-fallback (``unstructured``/``coerce_failed``) rows stay excluded
        OUTRIGHT — a garbage body is not claim-bearing signal, it is noise;
      * ``effective_confidence`` is NULL for an unverified row (an explicit
        CASE — SQL ``LEAST`` ignores NULLs, which would otherwise launder a raw
        confidence into a verified-looking score), so an ungraded head can
        never raise a ceiling or masquerade as verified.

    Every returned row is stamped ``_evidence_tier='periphery'`` +
    ``_evidence_floor=<floor>`` so the DB-less ``_run`` partitions on data, not
    env. The DB fetch is head-folded + capped at ``limit``; the worst-first
    PERIPHERY_CAP selection happens in the pure :func:`_select_periphery` so
    the ordering rule is unit-testable without a database.
    """
    if not analyst_ids:
        return []

    params: list[Any] = [list(analyst_ids), int(time_window_hours)]
    where: list[str] = [
        "f.kind = 'finding'",
        "f.analyst_id = ANY($1::TEXT[])",
        "f.produced_at > NOW() - make_interval(hours => $2)",
        "f.superseded_by IS NULL",
    ]
    if not include_meta:
        where.append("(f.data -> 'data' ->> 'meta') IS DISTINCT FROM 'true'")
    if target_id is not None:
        params.append(str(target_id))
        where.append(f"f.target_id = ${len(params)}")
    elif target_ids is not None:
        params.append([str(t) for t in target_ids])
        where.append(f"f.target_id = ANY(${len(params)}::TEXT[])")
    params.append(float(floor))
    where.append(
        "(v.faithfulness_score IS NULL"
        f" OR LEAST(f.confidence, v.faithfulness_score) < ${len(params)})"
    )
    where.append(
        "(f.data -> 'tags' ?| array['unstructured','coerce_failed']) IS NOT TRUE"
    )

    sql = f"""
    SELECT * FROM (
        SELECT DISTINCT ON (f.analyst_id, f.target_id)
               f.id, f.kind, f.title, f.body, f.confidence, f.severity, f.data,
               f.target_id, f.target_version, f.analyst_id, f.analyst_version,
               f.produced_at, f.derived_from, f.schema_uri, f.run_id,
               CASE WHEN v.faithfulness_score IS NULL THEN NULL
                    ELSE LEAST(f.confidence, v.faithfulness_score)
               END AS effective_confidence,
               v.faithfulness_score AS faithfulness_score
        FROM analyst_outputs f
        LEFT JOIN LATERAL (
            SELECT (cr.data->>'overall_score')::real AS faithfulness_score
              FROM analyst_outputs cr
             WHERE cr.kind = 'critique'
               AND cr.data->>'analyzed_output_id' = f.id::text
               AND cr.data->>'overall_score' IS NOT NULL
               AND cr.title LIKE 'Faithfulness verify%'
             ORDER BY cr.produced_at DESC, cr.id DESC
             LIMIT 1
        ) v ON TRUE
        WHERE {' AND '.join(where)}
        ORDER BY f.analyst_id, f.target_id, f.produced_at DESC, f.id DESC
    ) dedup
    ORDER BY dedup.produced_at DESC
    LIMIT {int(limit)}
    """
    rows = await conn.fetch(sql, *params)
    out: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        row[_EVIDENCE_TIER_KEY] = PERIPHERY_TIER
        row[_EVIDENCE_FLOOR_KEY] = float(floor)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# F-1 — compose-time head re-resolution (transitive freshness)
# ---------------------------------------------------------------------------


_FRESHNESS_FETCH_SQL: str = """
    SELECT id, analyst_id, target_id, confidence, produced_at, superseded_by,
           derived_from, left(title, 200) AS title
      FROM analyst_outputs
     WHERE id = ANY($1::uuid[]) AND kind = 'finding'
"""


def _iso_or_none(value: Any) -> str | None:
    """ISO-8601 a datetime-ish value; ``None``/malformed → ``None``."""
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    return None


async def _detect_stale_inputs(
    conn,  # type: ignore[no-untyped-def]
    rows: Sequence[Mapping[str, Any]],
    *,
    max_depth: int = FRESHNESS_MAX_DEPTH,
    max_nodes: int = FRESHNESS_MAX_NODES,
    min_delta: float = FRESHNESS_MATERIAL_CONF_DELTA,
) -> dict[str, Any] | None:
    """Walk each input head's lineage for MATERIALLY-reversed sub-findings (F-1).

    Bounded BFS over ``derived_from`` (findings only — signal/fact ids drop out
    on the ``kind='finding'`` fetch). A lineage finding is a *stale-root* iff it
    was SUPERSEDED and its current successor's confidence differs by
    ``>= min_delta`` AND the supersession happened AFTER the finding that CITES
    it composed (``succ.produced_at > parent.produced_at``) — i.e. a genuine
    post-hoc reversal the citing tier could not have known about, not routine
    re-run churn the read gate already resolved.

    Returns a freshness dict ``{inputs_as_of, stale_roots, advisory}`` (or
    ``None`` when there is nothing to report). The caller denormalizes it onto
    every input row (mirroring ``_region_coverage``) so the DB-less ``_run`` can
    render + trace it. NEVER mutates the substrate.
    """
    if not rows:
        return None

    # Seed the frontier with each input head's direct lineage children, tagged
    # with the citing parent's produced_at (the input head itself).
    frontier: list[tuple[UUID, Any]] = []
    for row in rows:
        parent_at = row.get("produced_at")
        for child in (row.get("derived_from") or []):
            uid = _coerce_uuid(child)
            if uid is not None:
                frontier.append((uid, parent_at))

    visited: set[UUID] = set()
    superseded_nodes: list[dict[str, Any]] = []
    node_budget = max_nodes
    depth = 0

    while frontier and depth < max_depth and node_budget > 0:
        # Collapse this level to unique, unvisited ids, keeping the EARLIEST
        # citing-parent produced_at per node. If a finding is cited by both an
        # early and a late parent, the EARLY citer is the one whose framing can
        # be stale — so flag if the reversal postdates ANY citer (surfacing
        # staleness beats suppressing it; the pass is advisory-only, never gates).
        level_parent: dict[UUID, Any] = {}
        for uid, parent_at in frontier:
            if uid in visited:
                continue
            if uid not in level_parent:
                level_parent[uid] = parent_at
            else:
                prev = level_parent[uid]
                if parent_at is not None and (prev is None or parent_at < prev):
                    level_parent[uid] = parent_at
        if not level_parent:
            break
        level_ids = list(level_parent.keys())[:node_budget]  # hard budget cap
        node_budget -= len(level_ids)
        visited.update(level_ids)

        fetched = await conn.fetch(_FRESHNESS_FETCH_SQL, level_ids)
        next_frontier: list[tuple[UUID, Any]] = []
        for r in fetched:
            rid = r["id"] if isinstance(r["id"], UUID) else _coerce_uuid(r["id"])
            if r["superseded_by"] is not None:
                superseded_nodes.append(
                    {
                        "id": r["id"],
                        "analyst_id": r["analyst_id"],
                        "target_id": r["target_id"],
                        "confidence": r["confidence"],
                        "produced_at": r["produced_at"],
                        "title": r["title"],
                        "superseded_by": r["superseded_by"],
                        "parent_at": level_parent.get(rid),
                    }
                )
            # Descend regardless — a still-current node may cite a deeper reversal.
            for child in (r["derived_from"] or []):
                cuid = _coerce_uuid(child)
                if cuid is not None and cuid not in visited:
                    next_frontier.append((cuid, r["produced_at"]))
        frontier = next_frontier
        depth += 1

    if not superseded_nodes:
        return None

    # Resolve each superseded node to its TERMINAL current head by hopping the
    # supersession chain (not just ONE hop): a unit that re-reverses within the
    # window (A → A' → A'') must be judged against A'' (the CURRENT reading) so
    # the Δconfidence + the "reversed to" title reflect where the claim actually
    # landed, not an intermediate. Bounded per hop, batched, cycle-safe.
    chain_rows: dict[str, Any] = {}
    to_fetch: list[UUID] = sorted(
        {
            _coerce_uuid(n["superseded_by"])
            for n in superseded_nodes
            if _coerce_uuid(n["superseded_by"]) is not None
        },
        key=str,
    )
    for _hop in range(max_depth + 2):  # a couple more than lineage depth = ample
        pending = [u for u in to_fetch if str(u) not in chain_rows]
        if not pending:
            break
        next_fetch: list[UUID] = []
        for r in await conn.fetch(_FRESHNESS_FETCH_SQL, pending):
            chain_rows[str(r["id"])] = r
            if r["superseded_by"] is not None:
                nxt = _coerce_uuid(r["superseded_by"])
                if nxt is not None and str(nxt) not in chain_rows:
                    next_fetch.append(nxt)
        to_fetch = next_fetch

    def _terminal_head(succ_uid: UUID | None) -> Any:
        """Follow the supersession chain from ``succ_uid`` to the current head."""
        if succ_uid is None:
            return None
        seen: set[str] = set()
        cur = str(succ_uid)
        last = chain_rows.get(cur)
        while cur in chain_rows and cur not in seen:
            seen.add(cur)
            row = chain_rows[cur]
            last = row
            if row["superseded_by"] is None:
                return row
            nxt = _coerce_uuid(row["superseded_by"])
            if nxt is None:
                return row
            cur = str(nxt)
        return last  # deepest reachable (chain truncated by hop cap / cycle)

    stale: list[dict[str, Any]] = []
    for n in superseded_nodes:
        succ = _terminal_head(_coerce_uuid(n["superseded_by"]))
        if succ is None or succ["confidence"] is None:
            continue  # can't judge materiality without the current head
        parent_at = n["parent_at"]
        succ_at = succ["produced_at"]
        # Temporal gate: the reversal must post-date the finding that cites the
        # superseded one, else the citer would have read the successor already.
        if not (parent_at is not None and succ_at is not None and succ_at > parent_at):
            continue
        # Materiality gate — MAGNITUDE of the confidence swing only (a direction-
        # agnostic proxy: an under-weighted risk that jumped up is as stale as an
        # over-weighted one that dropped). It intentionally does NOT read semantic
        # direction (a same-band re-scope with a big Δconf can trip it), so the
        # advisory is CAPPED + per-target deduped to bound noise, and it only ever
        # ANNOTATES (never gates). Semantic reversal detection is S-phase (salience).
        old_conf = float(n["confidence"] or 0.0)
        new_conf = float(succ["confidence"])
        delta = abs(old_conf - new_conf)
        if delta < min_delta:
            continue
        stale.append(
            {
                "unit": n["analyst_id"],
                "target": n["target_id"],
                "old_id": str(n["id"]),
                "old_title": n["title"],
                "old_confidence": round(old_conf, 3),
                "new_id": str(succ["id"]),
                "new_title": succ["title"],
                "new_confidence": round(new_conf, 3),
                "delta_confidence": round(delta, 3),
                "superseded_at": _iso_or_none(succ_at),
            }
        )

    if not stale:
        return None

    stale.sort(key=lambda s: s["delta_confidence"], reverse=True)
    # Full ledger for the trace: dedupe by (unit, target), keep the sharpest.
    seen_ut: set[tuple[Any, Any]] = set()
    full: list[dict[str, Any]] = []
    for s in stale:
        key = (s["unit"], s["target"])
        if key in seen_ut:
            continue
        seen_ut.add(key)
        full.append(s)
    # Compact prompt advisory: one line per TARGET (the sharpest reversal), capped.
    seen_target: set[Any] = set()
    advisory: list[dict[str, Any]] = []
    for s in full:
        if s["target"] in seen_target:
            continue
        seen_target.add(s["target"])
        advisory.append(s)
        if len(advisory) >= FRESHNESS_MAX_ADVISORY:
            break

    return {
        "inputs_as_of": [
            {
                "id": str(_coerce_uuid(row.get("id")) or ""),
                "as_of": _iso_or_none(row.get("produced_at")),
            }
            for row in rows
        ],
        "stale_roots": full,
        "advisory": advisory,
    }


# KW-1 NOTE (comment only, no behavior change): the F-1 walk below descends
# ``derived_from`` per-slice on every compose. Now that ``output_consumption``
# (migration 0106) materializes the forward edges at write time, a
# behavior-identical fast path over that index is a candidate LATER
# optimization — deliberately not taken in the KW-1 wave.
async def _attach_freshness(
    conn,  # type: ignore[no-untyped-def]
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compose-time freshness re-resolution (F-1) — additive + fail-safe.

    Denormalizes the freshness dict onto EVERY input row as ``_freshness`` (read
    once from ``rows[0]`` by ``_run``, the ``_region_coverage`` precedent). On ANY
    error the slice is returned unchanged — a composition is load-bearing and must
    never break on the advisory pass.
    """
    if not rows:
        return rows
    try:
        freshness = await _detect_stale_inputs(conn, rows)
    except Exception:  # noqa: BLE001 — intentional fail-safe: never break a compose
        logger.warning(
            "meta_findings_synthesizer.freshness pass FAILED (non-fatal, "
            "composition proceeds with no advisory)",
            exc_info=True,
        )
        return rows
    if freshness is not None:
        for row in rows:
            row["_freshness"] = freshness
    return rows


def _render_freshness_advisory_block(advisory: Sequence[Mapping[str, Any]]) -> str:
    """Render the compact per-target stale-root advisory (F-1) for the prompt.

    Directive, not decorative: the model is told to DEMOTE any framing resting on
    the superseded reading. Empty advisory → empty string (no block, no data)."""
    if not advisory:
        return ""
    lines = [
        "FRESHNESS ADVISORY (compose-time re-resolution):",
        "Since the findings below were composed, these underlying assessments were",
        "SUPERSEDED by a materially different CURRENT head. Do NOT lead with, or",
        "over-weight, any framing that rests on the superseded reading — prefer the",
        "current reading and, if the earlier one shaped the inputs, say so plainly.",
    ]
    for s in advisory:
        unit = str(s.get("unit") or "unit")
        target = str(s.get("target") or "")
        tgt = f" [{target}]" if target else ""
        lines.append(
            f'- {unit}{tgt}: "{s.get("old_title", "")}" (confidence '
            f'{s.get("old_confidence")}) → SUPERSEDED by "{s.get("new_title", "")}" '
            f"(confidence {s.get('new_confidence')}) at {s.get('superseded_at')}."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# T4 — CONTESTED FACTS read (world composition only)
# ---------------------------------------------------------------------------
#
# The SECONDARY contested surface (the load-bearing one is cross-country
# [[ref:N]] cross-country disagreement, no new plumbing). A bounded, read-only look at the
# open ``public.fact_contention`` disputes (migration 0055) so the WORLD
# composition can NAME both surfaced sides and mark the dispute
# ``[[contested:<contention_id>]]``. The SELECTs mirror
# ``substrate_reads_api.list_contention`` (same sidecar tables, same non-junk +
# arbiter ordering) so the marker resolves through the EXISTING
# GET /api/v1/contention?group=<id> read with no read-API change. DETECT-ONLY:
# fact_contention is fact subject/predicate-keyed and target-less, so this
# citation is GLOBAL — wired for the world read ONLY (the per-country
# country_composition keeps sub-claim-level disagreement).

CONTENTION_GROUP_LIMIT: int = 12
"""Cap on open contested groups fed into the world CONTESTED FACTS block."""

CONTENTION_VALUES_PER_GROUP: int = 4
"""Cap on non-junk value clusters shown per group (arbiter order; both sides)."""

CONTENTION_SCORE_FLOOR_ENV: str = "LEGBA_COMPOSITION_CONTENTION_FLOOR"

CONTENTION_SCORE_FLOOR_DEFAULT: float = 0.50
"""The minimum per-group TOP ``arbiter_score`` (Q·C·R·F,
``fact_contention_arbiter._arbiter_score``) an open contention group must
clear to enter the world CONTESTED FACTS block.

``arbiter_score`` is a MULTIPLICATIVE product of four already-normalized
``[0, 1]`` factors, so it compresses hard — a live capture (2026-07-31, the P2
gallery) found the system-wide ceiling across all 679 open groups was ~0.37,
with the prior RECENCY-ordered ``LIMIT 12`` serving pure NER-relation-
extraction noise ("zionist | member of | hamas", score 0.10) as though it were
a live geopolitical dispute. This floor is DELIBERATELY the same 0.50
"verified" bar :data:`TIERED_BASIS_FLOOR_DEFAULT` already uses for
effective_confidence tiering — one canonical "this is real" bar reused across
the module, not the tradecraft preamble's separate confidence-language ladder
(whose 0.3 "speculative" ceiling describes a DIFFERENT scale — per-source
confidence, not a multiplicative group score). Most cycles will clear
NOTHING at this floor — that is the point: see
:func:`_render_contested_absent_line` for the honest line that renders
instead of silently omitting the block. Env-overridable via
``LEGBA_COMPOSITION_CONTENTION_FLOOR`` (clamped to ``[0.0, 1.0]``)."""


def _resolve_contention_floor() -> float:
    """The env-tunable :data:`CONTENTION_SCORE_FLOOR_DEFAULT` (clamped to
    ``[0.0, 1.0]``; a bad value logs a warning and falls back to the
    default — same parse contract as :func:`_resolve_split_floor`)."""
    raw = os.getenv(CONTENTION_SCORE_FLOOR_ENV)
    if raw is None:
        return CONTENTION_SCORE_FLOOR_DEFAULT
    try:
        return max(0.0, min(1.0, float(raw)))
    except (ValueError, TypeError):
        logger.warning(
            "meta_findings_synthesizer.contention_floor.bad_env value=%r — "
            "using default",
            raw,
        )
        return CONTENTION_SCORE_FLOOR_DEFAULT


async def read_open_contention(
    conn: asyncpg.Connection,
    *,
    limit: int = CONTENTION_GROUP_LIMIT,
    values_per_group: int = CONTENTION_VALUES_PER_GROUP,
    score_floor: float | None = None,
) -> dict[str, Any]:
    """Read OPEN contested-fact groups (status ``contested`` / ``surfaced``),
    ranked by SCORE (not recency), + their non-junk value clusters for the
    world composition's CONTESTED FACTS block.

    THE FIX (P2 gallery finding): a group's rank key is its TOP non-junk
    value cluster's ``arbiter_score`` DESC — not ``updated_at`` — and a group
    whose top score does not clear ``score_floor`` (default
    :func:`_resolve_contention_floor`) is EXCLUDED outright rather than
    rendered as though it were a real dispute. Recency ordering was surfacing
    whatever churned most recently, never the platform's highest-confidence
    disputes (see :data:`CONTENTION_SCORE_FLOOR_DEFAULT`'s docstring).

    Returns ``{"groups": [...], "served_count", "suppressed_count",
    "considered_count", "floor"}``:

      * ``groups`` — the SAME per-group shape as before (``contention_id``,
        ``subject_key``, ``predicate_key``, ``status``, ``values``), capped
        to ``limit``, score-ordered, floor-filtered.
      * ``considered_count`` — every OPEN (``contested``/``surfaced``) group,
        regardless of score.
      * ``served_count`` — groups that cleared the floor AND had ≥2 non-junk
        value clusters (an actual two-sided dispute).
      * ``suppressed_count`` — ``considered_count - served_count``: honest
        envelope accounting so a caller NEVER has to guess whether "no
        groups" means nothing was open or everything got filtered — see
        :func:`_render_contested_absent_line`.

    Read-only + bounded; a missing relation propagates (the caller treats
    this additive enrichment as best-effort — a contention read failure
    never blocks the world compose).
    """
    floor = (
        max(0.0, min(1.0, float(score_floor)))
        if score_floor is not None
        else _resolve_contention_floor()
    )
    group_rows = await conn.fetch(
        """
        WITH scored AS (
            SELECT fc.id, fc.subject_key, fc.predicate_key, fc.status,
                   (SELECT MAX(fcv.arbiter_score)
                      FROM fact_contention_values fcv
                     WHERE fcv.contention_id = fc.id
                       AND fcv.is_junk = false) AS top_score
              FROM fact_contention fc
             WHERE fc.status IN ('contested', 'surfaced')
        )
        SELECT id, subject_key, predicate_key, status, top_score
          FROM scored
         ORDER BY top_score DESC NULLS LAST, id DESC
        """
    )
    if not group_rows:
        return {
            "groups": [], "served_count": 0, "suppressed_count": 0,
            "considered_count": 0, "floor": floor,
        }

    considered_count = len(group_rows)
    cleared = [
        g for g in group_rows
        if g["top_score"] is not None and float(g["top_score"]) >= floor
    ]
    selected = cleared[:limit]
    if not selected:
        return {
            "groups": [],
            "served_count": 0,
            "suppressed_count": considered_count,
            "considered_count": considered_count,
            "floor": floor,
        }

    group_ids = [g["id"] for g in selected]
    value_rows = await conn.fetch(
        """
        SELECT fcv.contention_id, fcv.value_key, fcv.arbiter_score,
               fcv.surfaced_winner, fcv.distinct_source_count
          FROM fact_contention_values fcv
         WHERE fcv.contention_id = ANY($1::uuid[])
           AND fcv.is_junk = false
         ORDER BY fcv.surfaced_winner DESC,
                  fcv.arbiter_score DESC NULLS LAST,
                  fcv.distinct_source_count DESC
        """,
        group_ids,
    )
    values_by_group: dict[Any, list[dict[str, Any]]] = {}
    for vr in value_rows:
        values_by_group.setdefault(vr["contention_id"], []).append(
            {
                "value_key": str(vr["value_key"]),
                "surfaced_winner": bool(vr["surfaced_winner"]),
                "arbiter_score": (
                    float(vr["arbiter_score"])
                    if vr["arbiter_score"] is not None
                    else None
                ),
                "distinct_source_count": int(vr["distinct_source_count"] or 0),
            }
        )

    out: list[dict[str, Any]] = []
    for g in selected:
        vals = values_by_group.get(g["id"], [])
        if len(vals) < 2:
            # Not a two-sided dispute (the other cluster is junk-gated / folded).
            continue
        out.append(
            {
                "contention_id": str(g["id"]),
                "subject_key": str(g["subject_key"]),
                "predicate_key": str(g["predicate_key"]),
                "status": str(g["status"]),
                "values": vals[:values_per_group],
            }
        )
    served_count = len(out)
    return {
        "groups": out,
        "served_count": served_count,
        "suppressed_count": considered_count - served_count,
        "considered_count": considered_count,
        "floor": floor,
    }


def _render_contested_block(groups: Sequence[Mapping[str, Any]]) -> str:
    """Render the open contested groups into the appended CONTESTED FACTS block.

    Each group is labelled with a STABLE ``[[contested:<contention_id>]]`` marker
    naming BOTH surfaced value clusters (winner flagged). Empty ``groups`` → ``""``
    (the block is simply absent; the world prompt's contested rule is then inert).
    """
    if not groups:
        return ""
    lines = [
        "",
        "CONTESTED FACTS (open disputes — surface BOTH sides, mark "
        "[[contested:<id>]], never pick a side the arbiter did not surface):",
    ]
    for g in groups:
        sides = "; ".join(
            (
                f"{v['value_key']}"
                + (" [arbiter-surfaced winner]" if v.get("surfaced_winner") else "")
                + (
                    f" (score={v['arbiter_score']:.2f})"
                    if v.get("arbiter_score") is not None
                    else ""
                )
            )
            for v in g.get("values", [])
        )
        lines.append(
            f"[[contested:{g['contention_id']}]] "
            f"subject={g['subject_key']} predicate={g['predicate_key']} :: {sides}"
        )
    return "\n".join(lines)


def _render_contested_absent_line(
    *, considered: int, suppressed: int, floor: float,
) -> str:
    """The HONEST fallback for the world CONTESTED FACTS block when the read
    attempted the contention gather but NOTHING cleared
    :data:`CONTENTION_SCORE_FLOOR_DEFAULT` (or there was nothing open at all).

    Never render nothing silently here: a silently-absent block is
    indistinguishable from a dead/broken read to anyone reading the prompt or
    the finding's envelope — this line says plainly that the mechanism ran
    and what it found, mirroring the APERTURE block's always-state-it
    posture (P2 gallery Observation 3 on the world capture) rather than the
    prior CONTESTED FACTS behavior of simply vanishing.
    """
    if considered <= 0:
        return "CONTESTED FACTS: no open fact disputes this cycle."
    return (
        "CONTESTED FACTS: "
        f"{considered} open dispute(s) considered this cycle; "
        f"{suppressed} did not clear the arbiter-score floor ({floor:.2f}) — "
        "no contested-fact block above threshold this cycle."
    )


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
    regions = [
        c for c in coverage
        if str(c.get("mode")) in (REGION_MODE_REGION, REGION_MODE_COUNTRY_FALLBACK)
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
# THE ONE prompt-block interface (C-4)
# ---------------------------------------------------------------------------
# The composition user turn is a BASE render (:func:`_render_user_prompt`) plus
# EIGHT optional blocks, each with its own guard, its own join separator, and its
# own POSITION — appended after the findings (evidence sections) or prepended
# ahead of them (directives the model must read first). That assembly was eight
# ad-hoc ``user_prompt = user_prompt + "\n" + block`` statements whose ORDER,
# separators and empty-checks were all load-bearing but implicit; the final order
# [freshness -> salience -> base -> periphery -> contested -> region -> aperture
# -> desk] only emerged from the interleaving of appends and prepends.
#
# It is ONE ordered walk here, with shared char/token accounting so every block's
# footprint is measured in one place instead of nowhere.
#
# BYTE-IDENTICAL BY CONSTRUCTION: blocks are applied in the same order with the
# same separators and the same guards, and each renderer is invoked LAZILY (only
# when its guard passes) exactly as before. Two asymmetries are preserved
# deliberately rather than "cleaned up":
#   * the CONTESTED block appends WITHOUT an empty-render check (every other
#     block skips an empty render) — hence ``require_non_empty=False``. It is
#     unreachable today (its renderer returns "" only for empty groups, and its
#     guard already requires non-empty groups) but it is not this lane's call to
#     change what happens if that ever stops holding.
#   * PREPENDS run AFTER appends, and salience prepends BEFORE freshness, which
#     is what leaves freshness first in the final turn.

#: Rough token estimate divisor — THE shared chars/4 convention
#: (``_llm_budget.CHARS_PER_TOKEN``), no tokenizer on the hot path. Aliased
#: rather than re-spelled so the composition and unit estimates cannot drift.
_PROMPT_CHARS_PER_TOKEN = CHARS_PER_TOKEN

_BLOCK_APPEND = "append"
_BLOCK_PREPEND = "prepend"


class _PromptBlockAssembler:
    """Ordered, budget-accounted assembly of the composition prompt blocks.

    Usage is declarative: construct with the base render, then ``add`` each block
    in its established order. ``add`` is a no-op when the block's guard is false,
    so the caller keeps its existing conditions in one readable place and the
    renderer stays lazy.
    """

    __slots__ = ("_text", "_ledger")

    def __init__(self, base: str) -> None:
        self._text = base
        # (block name, rendered chars) in APPLICATION order; the base is first.
        self._ledger: list[tuple[str, int]] = [("base", len(base))]

    def add(
        self,
        name: str,
        render: Any,
        *,
        when: Any,
        position: str,
        separator: str,
        require_non_empty: bool = True,
    ) -> None:
        """Render and splice one optional block.

        ``render`` is a zero-arg callable invoked ONLY when ``when`` is truthy —
        preserving the original lazy evaluation (several renderers are only valid
        under their guard). ``require_non_empty=False`` splices even an empty
        render, separator included.
        """
        if not when:
            return
        block = render()
        if require_non_empty and not block:
            return
        if position == _BLOCK_PREPEND:
            self._text = block + separator + self._text
        elif position == _BLOCK_APPEND:
            self._text = self._text + separator + block
        else:  # pragma: no cover - programming error
            raise ValueError(f"unknown prompt-block position: {position!r}")
        self._ledger.append((name, len(block)))

    @property
    def prompt(self) -> str:
        """The assembled user turn."""
        return self._text

    @property
    def total_chars(self) -> int:
        """Total assembled size — what the ``plan`` trace step records."""
        return len(self._text)

    @property
    def est_tokens(self) -> int:
        """Cheap chars/4 estimate of the assembled turn's input footprint."""
        return (len(self._text) + _PROMPT_CHARS_PER_TOKEN - 1) // _PROMPT_CHARS_PER_TOKEN

    @property
    def block_ledger(self) -> list[tuple[str, int]]:
        """Per-block (name, chars) in application order — the shared accounting."""
        return list(self._ledger)


# ---------------------------------------------------------------------------
# REASON+ACT — direct LLM call (DSPy wrapping deferred to L-176)
# ---------------------------------------------------------------------------


async def _reason_via_llm(
    llm: LLMHandlerLike,
    *,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    system_prompt: str,
) -> tuple[str, dict[str, int]]:
    """Single chat_complete call.  Same shape as the sibling kinds.

    Returns ``(content_str, usage_dict)`` in the flat token-accounting form
    the budget enforcer expects. Raises whatever the underlying handler
    raises so the actor's failure-classification logic can route it.
    """
    messages = [{"role": "user", "content": user_prompt}]
    response = await llm.chat_complete(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
    )
    content = getattr(response, "content", "") or ""
    usage_raw = getattr(response, "usage", None)
    usage_dict = {
        "prompt_tokens": getattr(usage_raw, "prompt_tokens", 0) if usage_raw else 0,
        "completion_tokens": (
            getattr(usage_raw, "completion_tokens", 0) if usage_raw else 0
        ),
        "reasoning_tokens": (
            getattr(usage_raw, "reasoning_tokens", 0) if usage_raw else 0
        ),
    }
    return content, usage_dict


# ---------------------------------------------------------------------------
# Runner — wires the synth LLM call together
# ---------------------------------------------------------------------------


class MetaFindingsSynthesizerRunner:
    """Callable conforming to the runtime's ``AnalystRunFn`` shape.

    Constructed once per analyst actor; the runtime injects a configured
    LLM handler. Each call makes one chat_complete invocation and returns
    one second-order :class:`FindingPayload`.

    Signature parity with ``InlineTargetRunner`` / ``CrossTargetRawRunner``
    is intentional — the actor layer in :mod:`legba.runtime.dapr_actors`
    treats them interchangeably.
    """

    def __init__(
        self,
        llm: LLMHandlerLike,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        system_prompt: str | None = None,
    ) -> None:
        self._llm = llm
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._system_prompt = system_prompt or _SYSTEM_PROMPT

    async def __call__(
        self,
        inputs: list[dict[str, Any]],
        options: Mapping[str, Any],
    ) -> AnalystMethodResult:
        return await _run(
            inputs,
            options,
            llm=self._llm,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            system_prompt=self._system_prompt,
        )


# ---------------------------------------------------------------------------
# Module-level run_method — the kind's entry point
# ---------------------------------------------------------------------------


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: MetaFindingsDeps,
) -> AnalystMethodResult:
    """Entry point the runtime calls per analyst-actor run for this kind.

    The host walks :mod:`legba.data.analysts` at startup, binds
    ``KIND_NAME`` -> this function, and dispatches by descriptor.kind.

    Parameters
    ----------
    inputs:
        First-order finding rows. Row shape mirrors
        ``analyst_outputs`` columns (id, kind, title, body, confidence,
        analyst_id, produced_at, data, evidence-via-data, ...). The
        runtime resolves the subscription's ``other_analysts`` list, calls
        :func:`read_other_analyst_findings` (or equivalent), and passes
        the rows here. Empty input is permitted — the runner emits a
        zero-source meta-finding rather than raising, matching the
        sibling kinds' contract.
    options:
        Per-run metadata. Conventional keys:
          * ``analyst_id``, ``analyst_version``, ``run_id`` — provenance.
          * ``source_analyst_ids`` — *optional* explicit list of source
            analysts from subscription resolution. When supplied, used as
            the authoritative ordering of ``contributing_analysts``;
            missing/empty falls back to the set derived from ``inputs``.
        Additional keys are ignored to keep the actor wrapper free of
        kind-specific surface assumptions.
    deps:
        Object satisfying :class:`MetaFindingsDeps` — at minimum carries
        an ``llm`` attribute conforming to
        :class:`legba.runtime.analyst_method.LLMHandlerLike`. An OPTIONAL
        ``temperature`` attribute (the deps builder threads the descriptor's
        ``method.llm.temperature`` — the 2026-07-24 sampling-audit fix)
        overrides :data:`DEFAULT_TEMPERATURE` when set; absent/None keeps
        the default, so pre-fix carriers behave byte-identically.

    Returns
    -------
    AnalystMethodResult
        Carrying a :class:`FindingPayload` whose ``data`` field includes
        ``meta=True`` and ``contributing_analysts=[...]``. The
        ``derived_from`` field on the result is the list of contributing
        first-order finding UUIDs; the runtime forwards it to
        :func:`legba.data.provenance.writes.write_analyst_output` so the
        substrate row's ``derived_from`` column carries the lineage edge.
        Token usage rolls up under the ``usage`` dict for budget recording.
    """
    # Sampling-audit fix (2026-07-24): honor the descriptor's OPTIONAL
    # ``method.llm.temperature`` (threaded onto deps by the builder) with the
    # same precedence the unit inline_target path uses — descriptor value when
    # set, else the kind default. getattr-guarded so every existing deps
    # carrier (tests' llm-only stubs included) behaves byte-identically.
    _temp = getattr(deps, "temperature", None)
    temperature = (
        float(_temp)
        if isinstance(_temp, (int, float)) and not isinstance(_temp, bool)
        else DEFAULT_TEMPERATURE
    )
    return await _run(
        inputs,
        options,
        llm=deps.llm,
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=temperature,
        system_prompt=_SYSTEM_PROMPT,
    )


# ---------------------------------------------------------------------------
# Shared run path (used by both ``run_method`` and the Runner wrapper)
# ---------------------------------------------------------------------------


async def _run(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    *,
    llm: LLMHandlerLike,
    max_tokens: int,
    temperature: float,
    system_prompt: str,
) -> AnalystMethodResult:
    """Internal — the actual orient → render → reason → coerce sequence.

    Separated from :func:`run_method` so the :class:`MetaFindingsSynthesizerRunner`
    closure-shape (per-actor configured ``max_tokens`` etc.) and the simpler
    deps-passing entry point share a single body.
    """
    # Composition MODE detection — drives the input cap, the system prompt, and
    # the CITE block. Five flavors:
    #   * PER-COUNTRY  (``options["target_id"]`` = a country id)     → single-country
    #   * REGION       (``options["target_id"]`` = ``region_<slug>``) → multi-country
    #   * THEMATIC     (``options["thematic_dimension"]``, no target) → multi-desk
    #   * WORLD        (``options["composition"]``, no target/theme)  → multi-region
    #   * legacy meta  (none)                                        → global synth
    # THEMATIC and WORLD are BOTH target-less + verify-declaring; the
    # ``thematic_dimension`` stamp (the actor lifts it from the descriptor's
    # ``subscription.substrate`` marker) is the discriminator between them.
    _target_opt = options.get("target_id")
    target_scoped = bool(_target_opt)
    region_scoped = target_scoped and str(_target_opt).startswith(REGION_TARGET_PREFIX)
    thematic_dim = None if target_scoped else options.get("thematic_dimension")
    thematic_composition = bool(thematic_dim)
    world_composition = (
        (not target_scoped)
        and bool(options.get("composition"))
        and not thematic_composition
    )
    is_composition = target_scoped or world_composition or thematic_composition

    # --- ORIENT --------------------------------------------------------
    # C-TIER: partition the input rows into BASIS and PERIPHERY BEFORE the
    # orient sort — periphery rows (READ_SLICE-marked ``_evidence_tier``) are
    # NEVER blended into the load-bearing slice: they neither consume the input
    # cap, nor drive salience/contributing-analysts, nor render as ordinary
    # sub-claim blocks. Data-driven (row markers, not env): the flag gate lives
    # entirely in READ_SLICE, so an unmarked slice — every legacy caller — is
    # byte-for-byte the untiered path. ``_tier_floor`` (stamped on every tiered
    # row) doubles as the tiered-mode signal so an ON-but-empty-periphery run
    # still records the honest envelope stamp.
    #
    # CONTINUITY rides the SAME data-driven partition, one layer out: the marked
    # continuity rows are lifted off FIRST so they can never be mistaken for
    # basis evidence (they must not consume the input cap, drive salience,
    # contribute to ``contributing_analysts``, or enter ``derived_from`` — a
    # composition is not DERIVED from its own memory, it is ANNOTATED by it).
    # The ``if continuity_rows else inputs`` identity fallback keeps a slice
    # without continuity byte-for-byte on the pre-continuity path.
    continuity_rows = [r for r in inputs if r.get(CONTINUITY_ROW_KEY)]
    tiered_pool = (
        [r for r in inputs if not r.get(CONTINUITY_ROW_KEY)]
        if continuity_rows
        else inputs
    )
    prior_row, register_row = _continuity_selection(continuity_rows)
    register_situations = _register_situations(register_row)
    periphery_rows = [
        r for r in tiered_pool if r.get(_EVIDENCE_TIER_KEY) == PERIPHERY_TIER
    ]
    basis_inputs = (
        [r for r in tiered_pool if r.get(_EVIDENCE_TIER_KEY) != PERIPHERY_TIER]
        if periphery_rows
        else tiered_pool
    )
    _tier_floor: float | None = None
    for _row in tiered_pool:
        _tf = _row.get(_EVIDENCE_FLOOR_KEY)
        if isinstance(_tf, (int, float)) and not isinstance(_tf, bool):
            _tier_floor = float(_tf)
            break
    tiered_evidence = _tier_floor is not None
    periphery_sel = (
        _select_periphery(periphery_rows) if periphery_rows else []
    )
    # RECEIPTS — how many continuity refs actually entered this slice. Reported
    # wherever the slice reports its composition stats (the ``orient`` step on
    # BOTH the normal and the honest-empty path, its own ``continuity`` step, and
    # the finding envelope) so "did the world read get its memory this cycle" is
    # answerable from a trace without re-running the gather. 0/1 each — these are
    # single refs by construction, and counting them is how a silently-absent
    # memory becomes visible instead of reading as a first run forever.
    continuity_receipts: dict[str, int] = {
        CONTINUITY_PRIOR_RECEIPT: 1 if prior_row is not None else 0,
        CONTINUITY_SITUATIONS_RECEIPT: 1 if register_situations else 0,
    }

    # A per-COUNTRY read fuses only its own ~7 unit heads, so the narrow default
    # cap never bites there. The WORLD read AND a REGION read each fuse one head
    # PER COUNTRY, so their input count is a desk roster (region = its member
    # subset) and MUST NOT be trimmed to the per-country default (the P4 review
    # C2 found the 15-cap dropped the US from a "Global" read; the same
    # one-head-per-country invariant holds for a region — a dropped input is a
    # member country the region read cannot see).
    _cap = (
        MAX_INPUT_FINDINGS
        if (target_scoped and not region_scoped)
        else MAX_WORLD_INPUT_FINDINGS
    )
    sliced, derived_from, derived_analysts = _orient(basis_inputs, cap=_cap)

    # The runtime can supply ``source_analyst_ids`` directly via options.
    # If so, use that ordering as the authoritative ``contributing_analysts``
    # (subscription-resolution time-of-bind is the source of truth for which
    # analysts the descriptor intends to read), and union with whatever the
    # actually-present rows attributed to (defense against stale resolution).
    provided: list[str] = []
    raw_provided = options.get("source_analyst_ids")
    if isinstance(raw_provided, (list, tuple)):
        provided = [str(a) for a in raw_provided if isinstance(a, str) and a]
    contributing_analysts: list[str]
    if provided:
        seen = set(provided)
        contributing_analysts = list(provided) + [
            a for a in derived_analysts if a not in seen
        ]
    else:
        contributing_analysts = derived_analysts

    # Composition detection — resolved BEFORE the empty-slice branch so an
    # honest-empty per-country / world composition ALSO carries the S8-T3
    # supersession signature (else a country with zero verified sub-claims
    # would still accumulate one live diagnostic head per cycle). Two flavors +
    # the legacy global meta:
    #   * TARGET-SCOPED (``options["target_id"]``) → the per-COUNTRY composition.
    #   * THEMATIC (``options["thematic_dimension"]``, no target_id) → the thematic
    #     dimension composition (escalation_composition).
    #   * GLOBAL verify-declaring meta (``options["composition"]``, no target_id)
    #     → the WORLD composition.
    #   * else → the legacy GLOBAL meta (byte-for-byte unchanged, no signature).
    target_scoped = bool(options.get("target_id"))
    thematic_dim = None if target_scoped else options.get("thematic_dimension")
    thematic_composition = bool(thematic_dim)
    world_composition = (
        (not target_scoped)
        and bool(options.get("composition"))
        and not thematic_composition
    )
    is_composition = target_scoped or world_composition or thematic_composition
    # S8-T3 per-head supersession signature (None for the legacy global meta so
    # its behavior is byte-for-byte unchanged). Encodes target_id — see
    # ``_composition_signature``.
    composition_signature = (
        _composition_signature(options.get("analyst_id"), options.get("target_id"))
        if is_composition
        else None
    )

    if not sliced:
        # Defensive empty-input path. The runtime ordinarily short-circuits
        # before calling us (see ``AnalystActor.run`` NOOP/no_inputs branch),
        # but emit a minimal diagnostic finding rather than crash. Stamped
        # with ``meta=True`` so a downstream "list meta-findings" filter
        # still finds it; confidence=0.0 so it doesn't pollute synthesis
        # confidence stats.
        empty_data: dict[str, Any] = {
            "meta": True,
            "contributing_analysts": list(contributing_analysts),
        }
        if composition_signature is not None:
            # Cluster even the honest-empty composition head (append-only
            # supersession folds prior-cycle empties to the newest).
            empty_data["situation_signature"] = composition_signature
        # C-TIER: an empty BASIS with a non-empty PERIPHERY is still an
        # empty-slice run — a composition is never synthesized from weak
        # signals alone (nothing verified exists to hedge them against). But
        # the weak signal is RECORDED, never lost: the envelope names the
        # periphery ids + the floor, and the body says why nothing composed.
        empty_body = "The other-analyst output slice for this run was empty."
        if tiered_evidence:
            empty_data["evidence_tiers"] = {
                "basis_count": 0,
                "periphery_count": len(periphery_sel),
                "periphery_ids": _periphery_ids(periphery_sel),
                "floor": _tier_floor,
            }
            if periphery_sel:
                empty_body = (
                    "The verified (basis) other-analyst slice for this run was "
                    f"empty. {len(periphery_sel)} below-floor/unverified "
                    "signal(s) were present (recorded in data.evidence_tiers) "
                    "but a composition is never synthesized from weak signals "
                    "alone."
                )
        finding = FindingPayload(
            title="No source findings to synthesize",
            body=empty_body,
            confidence=0.0,
            tags=["empty_slice", "meta"],
            data=empty_data,
        )
        return AnalystMethodResult(
            finding=finding,
            usage={},
            derived_from=[],
            intermediate_steps=[
                {
                    "phase": "orient",
                    "kind": "deterministic",
                    "in_count": len(inputs),
                    "kept_count": 0,
                    **continuity_receipts,
                },
                {"phase": "reflect", "kind": "noop_no_inputs"},
            ],
            # KW-1: even the honest-empty composition head CONSUMED the
            # periphery it recorded (data.evidence_tiers.periphery_ids) — the
            # forward index must know those rows were read, so a later mover
            # among them can flag this head. Basis is empty by definition here.
            consumed_edges=(
                [
                    (u, CONSUMPTION_CONTEXT_PERIPHERY)
                    for r in periphery_sel
                    if (u := _coerce_uuid(r.get("id"))) is not None
                ]
                if is_composition
                else []
            ),
        )

    # Composition selection — three flavors + the legacy global meta (the mode
    # flags were resolved at the top of ``_run``):
    #   * REGION (``options["target_id"]`` = ``region_<slug>``) → the multi-country
    #     ``_REGION_COMPOSITION_SYSTEM`` (a region read is MULTI-country, so it uses
    #     the cross-country hedge + disagreement shape, NOT the single-country
    #     ``_COMPOSITION_SYSTEM``). Checked FIRST so a region ``target_id`` never
    #     falls into the per-country branch.
    #   * PER-COUNTRY (a non-region ``options["target_id"]``) → the per-COUNTRY
    #     composition (country_composition): ``_COMPOSITION_SYSTEM``.
    #   * GLOBAL verify-declaring meta (``options["composition"]``, no target_id)
    #     → the WORLD compose over REGIONS (S2-T3, the repointed world_assessor):
    #     ``_WORLD_OVER_REGIONS_SYSTEM`` — composes the per-REGION reads, surfaces
    #     CROSS-REGION disagreement, (T4) appends the CONTESTED FACTS block, and
    #     (S2-T3) NAMES any region with no read via the appended REGION COVERAGE
    #     block. The actor stamps ``composition``/``contention_groups``; READ_SLICE
    #     stamps the per-region coverage onto the rows.
    #   * else → the legacy GLOBAL meta (analyst_meta_synthesizer.yaml),
    #     byte-for-byte unchanged (``system_prompt`` = ``_SYSTEM_PROMPT``).
    # All three compositions cite sub-claims by their [[ref:N]] ordinal handle,
    # resolved into ``data.citations`` (ref_id=<finding uuid>, ref_kind='finding');
    # the render prefixes each sub-claim block with its [[ref:N]] handle + the
    # finding_id for debug (source ids on). ``target_scoped`` / ``world_composition``
    # / ``is_composition`` / ``region_scoped`` were resolved above (before the
    # empty-slice branch). A region read is MULTI-country -> region-composition
    # prompt; the world read is MULTI-region -> ``_WORLD_OVER_REGIONS_SYSTEM``.
    if region_scoped:
        effective_system = _REGION_COMPOSITION_SYSTEM
    elif target_scoped:
        effective_system = _COMPOSITION_SYSTEM
    elif thematic_composition:
        # THEMATIC (escalation) — one head per DESK of a UNIT dimension across ALL
        # desks; the cross-DESK escalation-worded prompt (checked BEFORE the world
        # branch so a thematic run never falls into the world-over-regions prompt).
        effective_system = _THEMATIC_COMPOSITION_SYSTEM
    elif world_composition:
        effective_system = _WORLD_OVER_REGIONS_SYSTEM
    else:
        effective_system = system_prompt

    # T4 (world composition only): the open contested groups the actor read +
    # stamped onto options — now the score-floored ``read_open_contention``
    # dict shape (``{"groups", "served_count", "suppressed_count",
    # "considered_count", "floor"}``). A bare list is ALSO accepted (back-compat
    # with a caller/test that hasn't adopted the counters) — the counters just
    # degrade to "unknown" (served = len(groups), no suppressed/considered
    # accounting). ``contention_attempted`` distinguishes "the read ran and
    # found nothing above the floor" (render the honest absent-line) from "this
    # composition never gathers contention at all / the read failed" (render
    # nothing — the prompt's contested rule stays inert, never fabricated).
    contention_groups: list[Mapping[str, Any]] = []
    contention_attempted = False
    contention_served = 0
    contention_suppressed = 0
    contention_considered = 0
    contention_floor = CONTENTION_SCORE_FLOOR_DEFAULT
    if world_composition:
        raw_contention = options.get("contention_groups")
        if isinstance(raw_contention, Mapping):
            contention_attempted = True
            raw_groups = raw_contention.get("groups")
            if isinstance(raw_groups, (list, tuple)):
                contention_groups = [g for g in raw_groups if isinstance(g, Mapping)]
            _served = _as_int(raw_contention.get("served_count"))
            contention_served = _served if _served is not None else len(contention_groups)
            contention_suppressed = _as_int(raw_contention.get("suppressed_count")) or 0
            contention_considered = _as_int(raw_contention.get("considered_count")) or 0
            _floor_raw = raw_contention.get("floor")
            if isinstance(_floor_raw, (int, float)):
                contention_floor = float(_floor_raw)
        elif isinstance(raw_contention, (list, tuple)):
            contention_attempted = True
            contention_groups = [g for g in raw_contention if isinstance(g, Mapping)]
            contention_served = len(contention_groups)

    # S2-T3 (world composition only): the per-region COVERAGE list READ_SLICE
    # denormalized onto every input row (``_region_coverage``) — the per-region
    # MODE (region / country_fallback / gap) the world read grounded each region
    # on. Read from the first row that carries it; harmlessly absent on a legacy /
    # pre-S2-T1 world read (coverage stays empty → no gap block, no data stamp).
    region_coverage: list[Mapping[str, Any]] = []
    if world_composition:
        for row in sliced:
            rc = row.get("_region_coverage")
            if isinstance(rc, list):
                region_coverage = [c for c in rc if isinstance(c, Mapping)]
                break

    # S2-T4 (thematic composition only): the per-desk COVERAGE list READ_SLICE
    # denormalized onto every input row (``_thematic_coverage``) — which desks had
    # an escalation head (present) vs none (gap). Read from the first row that
    # carries it; harmlessly absent on a non-thematic run.
    desk_coverage: list[Mapping[str, Any]] = []
    if thematic_composition:
        for row in sliced:
            dc = row.get("_thematic_coverage")
            if isinstance(dc, list):
                desk_coverage = [c for c in dc if isinstance(c, Mapping)]
                break

    # F-1 (MASTER_PLAN 2026-07-13): the compose-time freshness re-resolution the
    # READ_SLICE pass walked onto every input row (mirroring _region_coverage) —
    # any lower-tier sub-finding SUPERSEDED by a materially different current head
    # AFTER the tier that cited it composed (the Italy staleness race). Read once
    # from the first row that carries it; harmlessly absent on a fresh/legacy slice.
    freshness_meta: Mapping[str, Any] | None = None
    for row in sliced:
        fm = row.get("_freshness")
        if isinstance(fm, Mapping):
            freshness_meta = fm
            break
    freshness_advisory: list[Mapping[str, Any]] = []
    if isinstance(freshness_meta, Mapping):
        raw_adv = freshness_meta.get("advisory")
        if isinstance(raw_adv, list):
            freshness_advisory = [a for a in raw_adv if isinstance(a, Mapping)]

    # --- PLAN ----------------------------------------------------------
    user_prompt = _render_user_prompt(
        sliced, contributing_analysts, include_source_ids=is_composition
    )
    # R2: compare the shown findings' VERIFIED claims against EACH OTHER before
    # composing. Keyed on the rendered ordinal (i, 1-based) so a detected pair's
    # handles are the ones the model can actually cite. Non-composition paths and
    # rows whose critique carried no ledger contribute nothing — the check is
    # silently inert rather than absent, and ``contradictions_checked`` below
    # records which of those two it was.
    _claims_by_ref: dict[int, list[str]] = {}
    if is_composition:
        for _i, _row in enumerate(sliced, start=1):
            _verified = _verified_claim_texts(_row)
            if _verified:
                _claims_by_ref[_i] = _verified
    _input_contradictions = (
        detect_contradictions(_claims_by_ref) if _claims_by_ref else []
    )
    if _input_contradictions:
        logger.warning(
            "meta.composition.input_contradictions n=%d refs=%s — the input set "
            "asserts incompatible states; the tension block is being rendered",
            len(_input_contradictions),
            [(c.a_ref, c.b_ref, c.group) for c in _input_contradictions],
        )
    # The EIGHT optional prompt blocks, through the ONE budgeted interface
    # (:class:`_PromptBlockAssembler`). Order, separators and guards are the
    # established ones — see the class comment for the two preserved asymmetries
    # (contested splices without an empty-check; prepends run after appends, so
    # the final turn reads [freshness → salience → findings → …]).
    _blocks = _PromptBlockAssembler(user_prompt)
    # C-TIER: the PERIPHERY section renders APPENDED to (never interleaved
    # with) the basis blocks, under its explicit delimiter + hedge/conflict
    # rules, with ordinals continuing the basis numbering. Empty periphery ⇒
    # no section ⇒ the prompt is byte-identical to the untiered render.
    _blocks.add(
        "periphery",
        lambda: _render_periphery_block(
            periphery_sel, start_ordinal=len(sliced) + 1, floor=_tier_floor
        ),
        when=is_composition and periphery_sel,
        position=_BLOCK_APPEND,
        separator="\n\n",
    )
    # CONTINUITY: appended DIRECTLY after the periphery so the rendered order and
    # the ordinal order are the same walk — basis 1..K, periphery K+1..K+P,
    # continuity K+P+1.. — and a reader of either the prompt or ``data.citations``
    # sees one flat, contiguous [[ref:N]] space. It sits ahead of the coverage /
    # contested blocks because those are DIRECTIVES about the current slice,
    # while this is EVIDENCE that carries its own citable handles.
    _continuity_start_ordinal = len(sliced) + len(periphery_sel) + 1
    _blocks.add(
        "continuity",
        lambda: _render_continuity_block(
            prior_row,
            register_situations,
            start_ordinal=_continuity_start_ordinal,
        ),
        when=is_composition and (prior_row is not None or register_situations),
        position=_BLOCK_APPEND,
        separator="\n\n",
    )
    # R2: the COMPUTED tension. Appended right after continuity and ahead of the
    # contested (fact-plane) block, because the two are the same idea at two
    # altitudes — this one is disagreement between the desk's own FINDINGS, that
    # one between the world's facts — and a reader should meet them together.
    # ``_input_contradictions`` was computed over the SAME ``sliced`` list the
    # findings block rendered, so its [[ref:N]] handles are the shown ordinals.
    _blocks.add(
        "input_contradictions",
        lambda: render_tension_block(_input_contradictions),
        when=is_composition and bool(_input_contradictions),
        position=_BLOCK_APPEND,
        separator="\n\n",
    )
    _blocks.add(
        "contested",
        lambda: (
            _render_contested_block(contention_groups)
            if contention_groups
            else _render_contested_absent_line(
                considered=contention_considered,
                suppressed=contention_suppressed,
                floor=contention_floor,
            )
        ),
        when=contention_attempted,
        position=_BLOCK_APPEND,
        separator="\n",
        require_non_empty=False,
    )
    _blocks.add(
        "region_coverage",
        lambda: _render_region_coverage_block(region_coverage),
        when=region_coverage,
        position=_BLOCK_APPEND,
        separator="\n",
    )
    # B0-10 (MASTER_PLAN 2026-07-10) — APERTURE honesty for the WORLD compose:
    # the sample is the registered desk roster (operator-chosen), not global
    # coverage. Rendered ALWAYS (not just on gaps) so the world prose names its
    # own bounds — faithfulness verify is silent about what was never collected,
    # so the aperture must be stated, not implied (review W8).
    _blocks.add(
        "world_aperture",
        lambda: _render_world_aperture_block(region_coverage),
        when=world_composition and region_coverage,
        position=_BLOCK_APPEND,
        separator="\n",
    )
    _blocks.add(
        "desk_coverage",
        lambda: _render_desk_coverage_block(desk_coverage),
        when=desk_coverage,
        position=_BLOCK_APPEND,
        separator="\n",
    )
    # S-2b: PREPEND the salience-lead directive (composition only) so the model
    # leads by CONSEQUENCE, not by which matter more blocks happen to mention.
    # Prepended BEFORE the freshness block below, so the final order is
    # [freshness → salience → findings] — freshness (demote stale) stays first.
    _blocks.add(
        "salience_lead",
        lambda: _render_salience_lead_block(sliced),
        when=is_composition,
        position=_BLOCK_PREPEND,
        separator="\n\n",
    )
    # F-1: PREPEND the freshness advisory (a directive: demote/caveat any framing
    # that rests on a since-superseded reading) so the model reads it BEFORE the
    # findings — the earliest, highest-priority instruction in the user turn.
    _blocks.add(
        "freshness_advisory",
        lambda: _render_freshness_advisory_block(freshness_advisory),
        when=freshness_advisory,
        position=_BLOCK_PREPEND,
        separator="\n\n",
    )
    user_prompt = _blocks.prompt
    steps: list[dict[str, Any]] = [
        {
            "phase": "orient",
            "kind": "deterministic",
            "in_count": len(inputs),
            "kept_count": len(sliced),
            "derived_count": len(derived_from),
            "analysts": len(contributing_analysts),
            **continuity_receipts,
        },
        {
            "phase": "plan",
            "kind": "render_prompt",
            # Same number as ``len(user_prompt)`` — read off the shared block
            # accounting so the assembler is the one place prompt size is known.
            "prompt_chars": _blocks.total_chars,
            "prompt_module": PROMPT_MODULE_PATH,
            "composition": is_composition,
        },
        {
            "phase": "freshness",
            "kind": "reresolve_inputs",
            "stale_roots": (
                len(freshness_meta.get("stale_roots", []))
                if isinstance(freshness_meta, Mapping)
                else 0
            ),
            "advised": len(freshness_advisory),
        },
        {
            "phase": "continuity",
            "kind": "citable_refs",
            **continuity_receipts,
            "situations": len(register_situations),
            "start_ordinal": _continuity_start_ordinal,
        },
    ]

    # --- REASON+ACT ----------------------------------------------------
    try:
        content, usage = await _reason_via_llm(
            llm,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=effective_system,
        )
    except Exception:
        # Re-raise — actor classifies (transient / budget / hard fail) per
        # kind_contracts §7. Don't swallow.
        steps.append({"phase": "reason", "kind": "llm_error"})
        raise

    steps.append({
        "phase": "reason",
        "kind": "llm_call",
        "subprovider": getattr(llm, "subprovider", "unknown"),
        "tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
    })

    # --- REFLECT -------------------------------------------------------
    fallback_title = (
        f"Synthesis across {len(contributing_analysts)} analyst(s)"
        if contributing_analysts
        else "Cross-analyst synthesis"
    )
    finding = _coerce_finding(
        content,
        fallback_title=fallback_title,
        contributing_analysts=contributing_analysts,
    )
    steps.append({
        "phase": "reflect",
        "kind": "coerce_finding",
        "confidence": finding.confidence,
        "evidence_count": len(finding.evidence),
        "structured": "unstructured" not in finding.tags,
    })

    # --- SUPERSEDE (S8-T3, composition only) --------------------------
    # Stamp the per-head supersession signature so finding_supersession clusters
    # these composition heads down to ONE canonical head per (analyst_id,
    # target_id) — append-only (the supersession handler / the 0058 backfill
    # link older heads to the newest via ``superseded_by``, NEVER delete a row).
    # The legacy global meta gets no signature (composition_signature is None), so
    # its clustering behavior is byte-for-byte unchanged.
    if composition_signature is not None:
        finding.data["situation_signature"] = composition_signature

    # --- FRESHNESS LEDGER (F-1, composition only) ---------------------
    # Record the compose-time re-resolution ledger — the input heads' as-of times
    # + the superseded sub-findings we advised the model to demote — so verify /
    # the journal / an operator can see the staleness the model was told about,
    # whether or not the prose acted on it. Stamped only when a MATERIAL reversal
    # was found (otherwise no key — the common fresh compose stays byte-for-byte).
    if isinstance(freshness_meta, Mapping) and freshness_meta.get("stale_roots"):
        finding.data["freshness"] = {
            "inputs_as_of": freshness_meta.get("inputs_as_of", []),
            "stale_roots": freshness_meta.get("stale_roots", []),
            "advised": len(freshness_advisory),
        }

    # --- SALIENCE (S-1d, propagation) ---------------------------------
    # Propagate CONSEQUENCE up the tower: this composition's salience = the MAX
    # over its input findings' stamped ``data.salience`` (each already the max of
    # ITS inputs, recursively down to the raw signal). The winner is forwarded
    # unchanged, so the original top LEAF signal's identity (top_signal_id /
    # top_title) reaches the world read — the S-3 judge can then ask "did the
    # world lead with the highest-consequence event in the WHOLE tree". Fail-safe:
    # unstamped inputs contribute nothing; if NONE is stamped, no key is written
    # (byte-for-byte the pre-S compose).
    from .signal_salience import max_salience as _max_salience

    _input_saliences = [_extract_input_salience(row) for row in sliced]
    _scored_inputs = [s for s in _input_saliences if s is not None]
    _composition_salience = _max_salience(_scored_inputs)
    if _composition_salience is not None:
        _composition_salience["n_scored"] = len(_scored_inputs)
        finding.data["salience"] = _composition_salience

    # --- CITE (composition only) --------------------------------------
    # Resolve the model's inline ``[[ref:N]]`` ORDINAL markers against the rendered
    # slice RANGE: ``N`` maps to ``sliced[N-1]`` (the SAME ``enumerate(sliced,
    # start=1)`` order the render stamped, so ordinal N == the Nth sub-claim ==
    # the Nth ``derived_from`` entry — no drift). Only in-range ordinals become
    # citations — an out-of-range (fabricated) handle is DROPPED (counted, never
    # emitted). Each citation carries ``ref_id`` (the cited FINDING uuid — the
    # correct drill target) + ``ref_kind='finding'`` (the kind-aware discriminator)
    # + ``ordinal`` (the deterministic resolution key) so a LATER stage can run a
    # faithfulness verify over the composition itself.
    if is_composition:
        # C-TIER: the ordinal space spans basis THEN periphery (the same order
        # the render stamped), so a periphery citation resolves like any other
        # — the ``tier`` stamp below is what tells the verify pass apart.
        # CONTINUITY extends that ONE space by up to two more blocks, in the same
        # order the render emitted them (prior read, then register), so ordinal N
        # still means "the Nth rendered block" with no drift.
        continuity_seq: list[Mapping[str, Any]] = []
        if prior_row is not None:
            continuity_seq.append(prior_row)
        if register_situations and register_row is not None:
            continuity_seq.append(register_row)
        num_subclaims = len(sliced) + len(periphery_sel) + len(continuity_seq)
        index_by_ordinal: dict[int, Mapping[str, Any]] = {
            n: row
            for n, row in enumerate(
                (*sliced, *periphery_sel, *continuity_seq), start=1
            )
        }
        resolved_ords, dropped_refs = _extract_ref_markers(
            finding.body, num_subclaims
        )
        citations: list[dict[str, Any]] = []
        for n in resolved_ords:
            src_row = index_by_ordinal[n]
            # CONTINUITY — the open-situation REGISTER is not an
            # ``analyst_outputs`` row and has NO single substrate id, so it gets
            # its own citation shape: ``ref_kind='situation_register'`` with the
            # REAL ``situations`` uuids on ``situation_ids`` and NO ``ref_id``.
            # Minting a ``ref_id`` (say, the top situation's) purely so a drill
            # link resolves would be a fabricated anchor — the one thing the
            # citation contract forbids. ``evidence_text`` carries the rendered
            # register, so the verify pass grades a register-backed clause
            # against exactly what the model was shown, with no verify change.
            if src_row.get(CONTINUITY_ROW_KEY) == CONTINUITY_SITUATIONS:
                citations.append(
                    {
                        "marker": f"[[ref:{n}]]",
                        "ordinal": n,
                        "ref_kind": SITUATION_REGISTER_REF_KIND,
                        CONTINUITY_CITATION_KEY: CONTINUITY_SITUATIONS,
                        "title": (
                            f"Open-situation register ({len(register_situations)} "
                            "open frame(s))"
                        ),
                        "situation_ids": [
                            str(s.get("situation_id"))
                            for s in register_situations
                            if s.get("situation_id")
                        ],
                        "evidence_text": "\n".join(
                            _render_situation_register_lines(register_situations, n)
                        )[:SITUATION_REGISTER_EVIDENCE_CHARS],
                    }
                )
                continue
            citation = _build_composition_citation(n, src_row)
            if citation is None:
                # No drill target on the cited sub-claim → count, never fabricate
                # a ref (mirrors the unit path's malformed-id handling).
                dropped_refs += 1
                continue
            # CONTINUITY — the PRIOR READ is a real finding, so it keeps the
            # ordinary ``ref_id``/``ref_kind='finding'`` shape (its drill target
            # is exactly right: the previous read). What it deliberately does NOT
            # carry is the T7 pair, and the omission is the point: the prior read
            # is MEMORY, not corroboration. Feeding its ``effective_confidence``
            # into the correlation guard would let last cycle's own conclusion
            # raise this cycle's de-duplicated evidence ceiling — a composition
            # bootstrapping its confidence off itself — and feeding its
            # ``derived_from`` would fold it into a shared-lineage component with
            # a current sub-claim, conflating "what we said before" with "what we
            # see now". The shared helper stamps both; strip them here.
            if src_row.get(CONTINUITY_ROW_KEY) == CONTINUITY_PRIOR:
                citation.pop("effective_confidence", None)
                citation.pop("derived_from", None)
                citation[CONTINUITY_CITATION_KEY] = CONTINUITY_PRIOR
                citation["produced_at"] = _iso_text(src_row.get("produced_at"))
            citations.append(citation)
        # A2 (verify-path structural fix, 2026-07-31): the model cited via
        # [[ref:N]] ZERO times (or every marker it used fell out of range)
        # despite a REAL basis — ``sliced``/``derived_from`` is non-empty. Never
        # leave ``citations`` empty when the compose rests on real evidence: fall
        # back to citing the BASIS directly (bounded; periphery excluded — a
        # periphery clause needs the model's own hedge, not an auto-attribution),
        # each entry flagged so a reader can tell an unmarked compose from a
        # marker-resolved one. Still never fabricates a clause-to-source mapping
        # — it states plainly which basis rows this compose was built from.
        # No-op (byte-identical) when the model DID cite (the common case).
        citations_fallback = False
        if not citations and sliced:
            fallback_n = min(len(sliced), _FALLBACK_BASIS_CITATIONS_CAP)
            for n in range(1, fallback_n + 1):
                citation = _build_composition_citation(n, index_by_ordinal[n])
                if citation is None:
                    continue
                citation["resolution"] = "fallback_basis"
                citations.append(citation)
            citations_fallback = bool(citations)
        finding.data["citations"] = citations
        steps.append({
            "phase": "cite",
            "kind": "resolve_refs",
            "citations": len(citations),
            "refs_dropped": dropped_refs,
            "citations_fallback": citations_fallback,
            # How many of the resolved citations landed on a CONTINUITY block —
            # i.e. did the model actually USE its memory, or was the block shown
            # and ignored? Separately countable from the refs OFFERED
            # (``continuity_receipts``), because "offered but never cited" is the
            # failure mode a continuity clause is supposed to make impossible.
            "continuity_cited": sum(
                1 for c in citations if c.get(CONTINUITY_CITATION_KEY)
            ),
        })

        # --- SALIENCE CHECK (S-3, advisory) ---------------------------
        # Did the composition's LEAD open on its highest-consequence input? The
        # inputs are salience-ordered so [[ref:1]] is the top; we compare the
        # lead citation's magnitude to the top and stamp the verdict at
        # data.eval.salience_check. ADVISORY — it never gates or alters the
        # finding; it makes the burial/flattening class MEASURABLE per compose.
        if _composition_salience is not None:
            _salience_check = _build_salience_check(
                _composition_salience, sliced, resolved_ords
            )
            if _salience_check is not None:
                _eval_block = finding.data.get("eval")
                if not isinstance(_eval_block, dict):
                    _eval_block = {}
                _eval_block["salience_check"] = _salience_check
                finding.data["eval"] = _eval_block
                steps.append({
                    # R3: no longer advisory. The verify pass reads this stamp and
                    # counts a FAILED lead as a soft faithfulness failure, so the
                    # verdict has a consequence instead of a note. The check itself
                    # is unchanged — it was always right, it was only ever unread.
                    "phase": "salience_check",
                    "kind": "counted",
                    "pass": _salience_check.get("pass"),
                    "gap": _salience_check.get("gap"),
                })

        # --- INPUT CONTRADICTIONS (R2) --------------------------------
        # Stamp what the tension block was built from, so the composition's own
        # row records which P ∧ ¬P pairs it was SHOWN. The verify pass reads this
        # to decide whether the body actually surfaced them; without the stamp,
        # "the composition ignored a contradiction" would be unprovable after the
        # fact. ``contradictions_checked`` distinguishes "looked, found none" from
        # "never looked" — the S-1 habit applied to this check's own zero.
        _eval_block = finding.data.get("eval")
        if not isinstance(_eval_block, dict):
            _eval_block = {}
        _eval_block["contradictions_checked"] = bool(_claims_by_ref)
        _eval_block["contradictions"] = [
            c.as_dict() for c in _input_contradictions
        ]
        finding.data["eval"] = _eval_block
        steps.append({
            "phase": "input_contradictions",
            "kind": "counted",
            "checked_refs": len(_claims_by_ref),
            "detected": len(_input_contradictions),
        })

        # --- CORRELATION GUARD (S2-T4 T7) -----------------------------
        # Detect cited heads that rest on the SAME underlying wire signal (shared
        # ``derived_from``) and collapse each correlated cluster to ONE independent
        # evidence unit, so a signal two sibling desk-units both cite is NOT
        # double-counted. DE-WEIGHT: cap the fused confidence to the de-duplicated
        # evidence ceiling (the max over INDEPENDENT components — never a sum). The
        # whole audit is stamped into ``data.correlation_guard`` for traceability.
        # Runs for EVERY composition (the machinery is shared with the region/world
        # cross-target fusions), but the cross-DESK thematic fusion is its target
        # case. The verify pass enforces the same anti-double-count at grade time.
        if citations:
            guard = _correlation_guard(citations)
            ceiling = guard.get("dedup_confidence_ceiling")
            capped = False
            if ceiling is not None and finding.confidence > ceiling + _GUARD_EPSILON:
                guard["confidence_before"] = finding.confidence
                finding.confidence = float(ceiling)
                capped = True
            guard["confidence_capped"] = capped
            finding.data["correlation_guard"] = guard
            steps.append({
                "phase": "correlation_guard",
                "kind": "dedupe_shared_lineage",
                "cited_heads": guard["cited_heads"],
                "independent_components": guard["independent_components"],
                "shared_lineage": guard["shared_lineage_detected"],
                "confidence_capped": capped,
            })

    # --- CONTESTED (world composition only) ---------------------------
    # Resolve the model's inline ``[[contested:<uuid>]]`` markers against the
    # assembled group-id set. A fabricated/unlisted id is DROPPED (counted, never
    # emitted) — the world read can only surface a dispute the arbiter actually
    # opened. Each kept marker carries the REAL contention_id so the UI resolves
    # it through the existing GET /api/v1/contention?group=<id> read.
    if world_composition and contention_groups:
        by_id = {str(g["contention_id"]): g for g in contention_groups}
        resolved_contested, dropped_contested = _extract_contested_markers(
            finding.body, set(by_id)
        )
        contested: list[dict[str, Any]] = []
        for cid in resolved_contested:
            g = by_id[cid]
            contested.append(
                {
                    "marker": f"[[contested:{cid}]]",
                    "contention_id": cid,
                    "subject_key": g.get("subject_key"),
                    "predicate_key": g.get("predicate_key"),
                    "values": list(g.get("values", [])),
                }
            )
        finding.data["contested"] = contested
        steps.append({
            "phase": "contested",
            "kind": "resolve_contested",
            "contested": len(contested),
            "contested_dropped": dropped_contested,
        })

    # --- CONTESTED FACTS envelope honesty (score-floor served/suppressed) --
    # Recorded whenever the world composition ATTEMPTED the contention gather
    # (whether or not anything cleared the floor) — so a reader of the finding
    # row alone can tell "0 groups because nothing was open" apart from "0
    # groups because N were open but suppressed by the score floor" without
    # replaying the read. Distinct from ``data.contested`` above, which is the
    # model's OWN resolved [[contested:<id>]] citations (a subset of ``served``,
    # never larger).
    if world_composition and contention_attempted:
        finding.data["contested_facts"] = {
            "served_count": contention_served,
            "suppressed_count": contention_suppressed,
            "considered_count": contention_considered,
            "floor": contention_floor,
        }
        steps.append({
            "phase": "contested_facts",
            "kind": "score_filter",
            "served": contention_served,
            "suppressed": contention_suppressed,
            "considered": contention_considered,
            "floor": contention_floor,
        })

    # --- REGION COVERAGE (S2-T3, world composition only) --------------
    # Stamp the per-region MODE (region / country_fallback / gap) the world read
    # grounded each region on, so the provenance is HONEST about which tower floor
    # backed each region. ``region_gaps`` is the convenience list of the NAMED
    # absent regions (the ones the REGION COVERAGE prompt block asked the model to
    # surface as unassessed). Absent on a legacy / pre-S2-T1 world read.
    if world_composition and region_coverage:
        finding.data["region_coverage"] = [dict(c) for c in region_coverage]
        region_gaps = [
            str(c.get("region_name") or c.get("region_id"))
            for c in region_coverage
            if str(c.get("mode")) == REGION_MODE_GAP
        ]
        if region_gaps:
            finding.data["region_gaps"] = region_gaps
        steps.append({
            "phase": "region_coverage",
            "kind": "stamp_coverage",
            "regions": len(region_coverage),
            "gaps": len(region_gaps),
        })

    # --- DESK COVERAGE (S2-T4, thematic composition only) -------------
    # Stamp the per-desk MODE (present / gap) so the provenance is HONEST about
    # which desks had an escalation read. ``desk_gaps`` is the convenience list of
    # the NAMED absent desks (the ones the DESK COVERAGE prompt block asked the
    # model to surface as unassessed). Absent on a non-thematic run.
    if thematic_composition and desk_coverage:
        finding.data["desk_coverage"] = [dict(c) for c in desk_coverage]
        desk_gaps = [
            str(c.get("desk_name") or c.get("desk_id"))
            for c in desk_coverage
            if str(c.get("mode")) == THEMATIC_MODE_GAP
        ]
        if desk_gaps:
            finding.data["desk_gaps"] = desk_gaps
        steps.append({
            "phase": "desk_coverage",
            "kind": "stamp_coverage",
            "desks": len(desk_coverage),
            "gaps": len(desk_gaps),
        })

    # --- CONTINUITY ENVELOPE (Phase 1, composition only) ----------------
    # Envelope honesty for the memory the same way ``evidence_tiers`` does it for
    # the evidence: record WHICH prior read (id + its own produced_at) and WHICH
    # open situations this compose was shown, so "what did the world read know
    # about before?" is answerable from the row without replaying the gather —
    # and so a cycle where the memory was ABSENT reads as absent rather than as
    # a first run. Stamped only when a ref was actually offered, so every
    # pre-continuity / first-run compose is byte-for-byte unchanged.
    if is_composition and (prior_row is not None or register_situations):
        continuity_env: dict[str, Any] = dict(continuity_receipts)
        if prior_row is not None:
            _prior_uid = _coerce_uuid(prior_row.get("id"))
            continuity_env["prior_finding_id"] = (
                str(_prior_uid) if _prior_uid is not None else None
            )
            continuity_env["prior_produced_at"] = _iso_text(
                prior_row.get("produced_at")
            )
            continuity_env["prior_age_hours"] = _as_float(prior_row.get("age_hours"))
        if register_situations:
            continuity_env["situation_ids"] = [
                str(s.get("situation_id"))
                for s in register_situations
                if s.get("situation_id")
            ]
        finding.data["continuity"] = continuity_env

    # --- FORWARD CONSUMPTION (KW-1, migration 0106) ---------------------
    # The consumption points, captured exactly where they were decided:
    # BASIS = the oriented, capped rows (``derived_from`` is basis-only at
    # this line — the tiered block below appends the periphery ids after
    # us), PERIPHERY = the selected periphery rows. Scoped to COMPOSITION
    # runs (country/region/world/thematic); the legacy global meta stamps
    # nothing, matching the standing legacy-read-unchanged discipline. The
    # runtime materializes these into ``output_consumption`` on the same
    # flow as the output write — best-effort, degrade-not-break.
    consumed_edges: list[tuple[UUID, str]] = []
    if is_composition:
        consumed_edges = [
            (u, CONSUMPTION_CONTEXT_BASIS) for u in derived_from
        ] + [
            (u, CONSUMPTION_CONTEXT_PERIPHERY)
            for r in periphery_sel
            if (u := _coerce_uuid(r.get("id"))) is not None
        ]

    # --- EVIDENCE TIERS (C-TIER, tiered compositions only) -------------
    # Envelope honesty: record what the composition was BUILT ON — N verified
    # basis + M weak periphery signals and the floor that split them — so the
    # UI/scorecard can say so without re-deriving the gather. The kept
    # periphery ids are ALSO appended to ``derived_from`` (after the basis
    # ids, matching the ordinal order) — a hedged claim resting on a weak
    # signal is real lineage, not a secret. Additive: absent on every
    # untiered run.
    if tiered_evidence:
        finding.data["evidence_tiers"] = {
            "basis_count": len(sliced),
            "periphery_count": len(periphery_sel),
            "periphery_ids": _periphery_ids(periphery_sel),
            "floor": _tier_floor,
        }
        for _peri_row in periphery_sel:
            _peri_uid = _coerce_uuid(_peri_row.get("id"))
            if _peri_uid is not None:
                derived_from.append(_peri_uid)
        steps.append({
            "phase": "evidence_tiers",
            "kind": "gather_split",
            "basis": len(sliced),
            "periphery": len(periphery_sel),
            "floor": _tier_floor,
        })

    # --- NARRATE + PERSIST envelope ------------------------------------
    # The runtime stamps the substrate-row ``derived_from`` column from
    # the UUID list we return; we already stuck ``meta=True`` and
    # ``contributing_analysts`` in the payload's data field. Nothing more
    # to do here besides the trace envelope.
    steps.append({
        "phase": "narrate",
        "kind": "envelope",
        "contributing_analysts": len(contributing_analysts),
    })
    steps.append({
        "phase": "persist",
        "kind": "envelope",
        "derived_from": len(derived_from),
    })

    return AnalystMethodResult(
        finding=finding,
        usage=usage,
        derived_from=derived_from,
        intermediate_steps=steps,
        consumed_edges=consumed_edges,
    )


# ---------------------------------------------------------------------------
# Per-kind substrate-slice reader bound to the actor-host dispatcher.
# The actor dispatcher invokes ``READ_SLICE(conn, descriptor=..., ...)``
# instead of its default signals-only reader when this kind runs.
# ---------------------------------------------------------------------------


def _resolve_other_analyst_ids(descriptor: Any) -> list[str]:
    """Resolve the source-analyst id set from ``subscription.other_analysts``.

    This is the documented read surface for the meta kinds (per L-101 §4 and
    the module docstring): the descriptor lists which OTHER analysts feed the
    synth via :class:`legba.data.schemas.analyst.SubscriptionAnalyst` entries
    on ``subscription.other_analysts``. Each entry carries an ``id``. The prior
    implementation read ``subscription.targets.id_list``, a field that does not
    exist on :class:`SubscriptionTargets` — so the resolution always yielded
    ``[]`` and the synth silently NOOPed forever. This reads the real surface.
    """
    sub = getattr(descriptor, "subscription", None)
    others = getattr(sub, "other_analysts", None) or [] if sub is not None else []
    return [str(getattr(a, "id", "")) for a in others if getattr(a, "id", "")]


def _resolve_window_hours(descriptor: Any, default: int = 24) -> int:
    """Resolve the read window (hours) from ``other_analysts[].time_window``.

    Honors the descriptor's declared per-analyst window (e.g. ``"336h"`` for a
    14-day look-back) so the slice isn't pinned to the hardcoded 24h default.
    Takes the widest declared window across the listed source analysts (the
    synth wants every contributing analyst's findings visible). Parses the
    ``SubscriptionAnalyst.time_window`` string form (``"<int>h"``; also accepts
    ``"<int>d"`` days for convenience). Falls back to ``default`` when nothing
    parses.
    """
    sub = getattr(descriptor, "subscription", None)
    others = getattr(sub, "other_analysts", None) or [] if sub is not None else []
    best: int | None = None
    for a in others:
        raw = getattr(a, "time_window", None)
        if not isinstance(raw, str):
            continue
        token = raw.strip().lower()
        try:
            if token.endswith("h"):
                hours = int(token[:-1])
            elif token.endswith("d"):
                hours = int(token[:-1]) * 24
            else:
                hours = int(token)
        except (ValueError, TypeError):
            continue
        if hours > 0:
            best = hours if best is None else max(best, hours)
    return best if best is not None else default


def _resolve_verify_floor(descriptor: Any, default: float = DEFAULT_VERIFY_FLOOR) -> float:
    """Resolve the per-country composition verify floor.

    OPS-tunable via ``LEGBA_COMPOSITION_VERIFY_FLOOR`` (clamped to ``[0.0, 1.0]``)
    so raising the bar is a one-line env change — no schema field, no registry
    rebuild. ``descriptor`` is accepted for a future per-descriptor override but
    is intentionally not read from an ``extra="forbid"`` schema block today.

    X-1 boundary (2026-07-29): ``method.options`` now exists and IS read at fire
    time — but only for ``kind=deterministic``, whose sub-handlers route through
    the ``handler_options`` catalog. This composition is an LLM kind, so the
    schema still refuses an options block here and the env var remains the only
    lever. Widening it means giving the LLM kinds their own declared catalog;
    until that exists this comment stays true rather than becoming a promise.
    """
    raw = os.getenv(VERIFY_FLOOR_ENV)
    if raw is not None:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (ValueError, TypeError):
            logger.warning(
                "meta_findings_synthesizer.verify_floor.bad_env value=%r — using default",
                raw,
            )
    return default


def _declares_verify(descriptor: Any) -> bool:
    """True iff the descriptor declares the ``method.llm.verify`` OR the P2-4
    ``method.llm.judge`` KEY (the composition verify OPT-IN).

    Mirrors the ``analyst_deps_builder.resolve_judge_route_from_llm_block`` rung-0
    OPT-IN GATE WITHOUT importing it — this kind module stays standalone (no
    runtime-package load cycle). DEFECT B fix (2026-07-29): this is now a KEY
    PRESENCE test (``"verify" in llm or "judge" in llm``), exactly mirroring
    rung 0's ``if "judge" not in llm and "verify" not in llm: return None``.
    Before the fix this tested VALUE presence (``llm.get("verify") is not
    None``), so a descriptor carrying a null-valued ``verify``/``judge`` key —
    e.g. ``{"verify": None, "primary": <ref>}`` — fell through the ladder to a
    resolvable route (the actor plane judged/verified it) while reading here as
    NOT opted in (the composer skipped the verify-floor / include_meta branch).
    See ``tests/data_pkg/test_judge_profile_resolution_pinned.py`` for the
    corrected pin. Both compositions (country + world) carry ``verify``; the old
    global meta does NOT → the world branch below (verify-floor + include_meta)
    engages ONLY for a composition. No live descriptor carries a null-valued
    key, so this fix changes no behavior on live data.
    """
    method = getattr(descriptor, "method", None)
    llm = getattr(method, "llm", None) if method is not None else None
    if not isinstance(llm, Mapping):
        return False
    return "verify" in llm or "judge" in llm


def thematic_dimension(descriptor: Any) -> str | None:
    """The THEMATIC composition UNIT dimension (S2-T4), or ``None``.

    Reads ``subscription.substrate[THEMATIC_DIMENSION_KEY]`` (the open substrate
    dict, no schema change). A non-empty string ⇒ this meta_findings_synthesizer is
    a THEMATIC composition: fuse the LATEST verified head of that UNIT analyst_id
    for EVERY desk into ONE global read (escalation_composition → ``'escalation'``).
    Absent / empty ⇒ ``None`` → the per-country / region / world / legacy branches
    are untouched. Presence is the discriminator between the (both target-less +
    verify-declaring) thematic and world-over-regions branches; the world branch is
    checked AFTER this one so a thematic marker wins.
    """
    sub = getattr(descriptor, "subscription", None)
    substrate = getattr(sub, "substrate", None) if sub is not None else None
    if not isinstance(substrate, Mapping):
        return None
    raw = substrate.get(THEMATIC_DIMENSION_KEY)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def thematic_desks(descriptor: Any) -> list[str] | None:
    """The THEMATIC composition DESK allow-list (S2-T5), or ``None``.

    Reads ``subscription.substrate[THEMATIC_DESKS_KEY]`` (the open substrate dict,
    no schema change). A non-empty list/tuple of desk ids ⇒ this thematic
    composition fuses the named UNIT dimension across ONLY those desks (the IR-IL
    escalation DYAD → ``['country_watch_ir','country_watch_il']``) instead of every
    g20+watch desk. Absent / empty ⇒ ``None`` → the thematic read spans ALL desks
    (escalation_composition is byte-for-byte unchanged). Only meaningful alongside a
    ``thematic_dimension`` marker.
    """
    sub = getattr(descriptor, "subscription", None)
    substrate = getattr(sub, "substrate", None) if sub is not None else None
    if not isinstance(substrate, Mapping):
        return None
    raw = substrate.get(THEMATIC_DESKS_KEY)
    if isinstance(raw, (list, tuple)):
        desks = [str(d).strip() for d in raw if str(d).strip()]
        return desks or None
    return None


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
    roster = await _resolve_region_roster(conn)

    # The region_composition heads — one HEAD per region via DISTINCT ON, verify-
    # floored + meta-inclusive (region_composition rows are meta=True). This is
    # the intended TOP-floor source; the country fallback below only fills gaps.
    region_rows = await read_other_analyst_findings(
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
            await read_other_analyst_findings(
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
    rows = await read_other_analyst_findings(
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


async def READ_SLICE(  # noqa: N802 — host-discovered constant alias
    conn,  # type: ignore[no-untyped-def]
    *,
    descriptor,  # type: ignore[no-untyped-def]
    target_filter,  # type: ignore[no-untyped-def]
    analyst_ids: Sequence[str] | None = None,
    time_window_hours: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Adapter exposing :func:`read_other_analyst_findings` under the
    host-dispatcher signature.

    Resolves the source-analyst id list in this priority order:

      1. ``analyst_ids=`` argument (used by tests / direct callers),
      2. the descriptor's ``subscription.other_analysts[].id`` (the documented
         read surface — each :class:`SubscriptionAnalyst` entry names a source
         analyst whose findings feed this synth),
      3. an empty list (yields ``[]``).

    When the caller does not pin ``time_window_hours`` it is resolved from the
    descriptor's ``other_analysts[].time_window`` (widest declared window),
    defaulting to 24h.

    P3 per-country vs global-meta split — keyed purely on ``target_filter``:

      * ``target_filter`` SET (a per-country composition descriptor carries a
        ``subscription.targets`` block, so the runtime fans this synth out one
        worker per G20 target with the target id in ``target_filter``) →
        scope the slice to that country (``target_id``) AND apply the
        verify-floor gate (``verify_floor``). The composition reads ONLY that
        country's verify-passed unit sub-claims.
      * ``target_filter`` NONE (the legacy GLOBAL meta descriptor has no
        ``subscription.targets`` → one global run) → neither filter applies;
        the cross-target, unfiltered read is preserved unchanged.

    S2-T2 REGION composition — a NEW 4th mode (keyed on the ``region_`` prefix):

      * ``target_filter`` is a REGION FRAME id (``region_<slug>``): the region
        composition descriptor's ``subscription.targets`` block matches the five
        region frames, so the runtime fans this synth out one worker per FRAME
        with ``target_filter='region_mena'`` etc. A frame has NO
        country_composition finding of its own, so the per-country branch would
        scope ``f.target_id='region_mena'`` and match nothing. Instead we resolve
        the frame → its MEMBER country desks (:func:`_resolve_region_member_target_ids`)
        and read THEIR country_composition heads as a SET (``target_ids``),
        verify-floored + ``include_meta=True`` (country_composition rows are
        ``meta=True``). An empty member set → an empty slice the synth narrates as
        a gap.

    S2-T3 WORLD compose over REGIONS — the target-LESS verify-declaring branch:

      * ``target_filter`` NONE AND the descriptor declares ``method.llm.verify``
        (the world_assessor, whose ``other_analysts`` now names
        ``region_composition``): the world read composes the region_composition
        HEADS (5-6 inputs) instead of the ~24 country heads. A region with NO
        region head DEGRADES to its member country_composition heads (never
        silently dropped); a region with neither is a NAMED gap. Assembled by
        :func:`_assemble_world_region_slice`, which stamps each row with the
        per-region MODE + denormalizes the coverage list for the DB-less ``_run``.
        The per-COUNTRY and LEGACY global-meta branches below stay BYTE-FOR-BYTE.

    CONTINUITY (Phase 1) — every COMPOSITION branch above (per-country, region,
    thematic, world) additionally appends up to TWO marked
    :data:`CONTINUITY_ROW_KEY` rows: this target's previous verified head and the
    bounded open-situation register for the SAME scope that branch reads its
    evidence over. They are appended AFTER the freshness/periphery passes so
    neither walks them, and they carry their own marker so the DB-less ``_run``
    partitions them out of the basis/periphery tiers. Best-effort: a continuity
    read failure yields no rows and never disturbs the slice. The LEGACY global
    meta gets none.

    Returns ``analyst_outputs`` rows with the same column projection that
    downstream lineage extraction expects.
    """
    if analyst_ids:
        ids = [str(a) for a in analyst_ids]
    else:
        ids = _resolve_other_analyst_ids(descriptor)

    if time_window_hours is None:
        time_window_hours = _resolve_window_hours(descriptor)

    # REGION branch (S2-T2) — checked FIRST, an early return, so the per-country /
    # world / legacy switch below stays byte-for-byte. A region ``target_filter``
    # is a FRAME id; resolve it to the member country desks and read THEIR
    # country_composition heads as a target-id SET (multi-country, world-shaped).
    if _is_region_target(target_filter):
        member_ids = await _resolve_region_member_target_ids(conn, str(target_filter))
        # C-TIER: flag ON ⇒ the basis bar is the SPLIT floor (env floor when
        # pinned, else the 0.50 scorecard lockstep) and the member heads the
        # bar excluded come back as marked PERIPHERY rows. Flag OFF (default)
        # ⇒ byte-for-byte the legacy region read.
        _tiered = _tiered_evidence_enabled()
        _floor = (
            _resolve_split_floor(descriptor)
            if _tiered
            else _resolve_verify_floor(descriptor)
        )
        rows = await _attach_freshness(
            conn,
            await read_other_analyst_findings(
                conn,
                analyst_ids=ids,
                time_window_hours=time_window_hours,
                limit=limit,
                target_ids=member_ids,
                verify_floor=_floor,
                include_meta=True,
            ),
        )
        if _tiered:
            periphery = await read_periphery_findings(
                conn,
                analyst_ids=ids,
                time_window_hours=time_window_hours,
                floor=_floor,
                target_ids=member_ids,
                include_meta=True,
            )
            for row in rows:
                row[_EVIDENCE_FLOOR_KEY] = _floor
            rows = rows + periphery
        # CONTINUITY — the region's own prior read (the FRAME's head, target_id =
        # the region frame id) + the open situations of its MEMBER desks (the
        # same scope this branch's evidence is read over).
        return rows + await _gather_continuity_rows(
            conn,
            descriptor=descriptor,
            analyst_ids=ids,
            verify_floor=_floor,
            prior_target_id=str(target_filter),
            situation_target_ids=member_ids,
        )

    # THEMATIC branch (S2-T4) — a target-LESS run whose descriptor carries a
    # ``subscription.substrate.thematic_dimension`` marker. Fuses ONE verified head
    # per DESK of that UNIT analyst dimension across ALL desks (post-supersession,
    # verify-floored), NAMING any desk with no head as a gap. Checked BEFORE the
    # WORLD branch (both are target-less + verify-declaring) so the marker's
    # presence is the discriminator; an early return leaves the world / per-country
    # / legacy switch below byte-for-byte. ``ids`` = other_analysts (the unit).
    if not target_filter and thematic_dimension(descriptor):
        # C-TIER: flag ON ⇒ the basis bar is the SPLIT floor and the per-desk
        # unit heads it excluded (below-floor OR unverified) come back as marked
        # PERIPHERY rows over the SAME scope (unit roster + dyad allow-list,
        # first-order). Flag OFF (default) ⇒ byte-for-byte the legacy read.
        _tiered = _tiered_evidence_enabled()
        _floor = (
            _resolve_split_floor(descriptor)
            if _tiered
            else _resolve_verify_floor(descriptor)
        )
        _desks = thematic_desks(descriptor)   # S2-T5: dyad desk allow-list (None ⇒ all desks)
        rows = await _attach_freshness(
            conn,
            await _assemble_thematic_unit_slice(
                conn,
                unit_analyst_ids=ids,
                time_window_hours=time_window_hours,
                limit=limit,
                verify_floor=_floor,
                desk_ids=_desks,
            ),
        )
        if _tiered:
            periphery = await read_periphery_findings(
                conn,
                analyst_ids=ids,
                time_window_hours=time_window_hours,
                floor=_floor,
                target_ids=(list(_desks) if _desks else None),
                include_meta=False,     # the unit is a FIRST-ORDER finding
            )
            for row in rows:
                row[_EVIDENCE_FLOOR_KEY] = _floor
            rows = rows + periphery
        # CONTINUITY — the thematic head is TARGET-LESS, so its prior read is the
        # target-less lane; the situation register follows the SAME desk scope the
        # thematic evidence does (the dyad allow-list, or every desk when unset).
        return rows + await _gather_continuity_rows(
            conn,
            descriptor=descriptor,
            analyst_ids=ids,
            verify_floor=_floor,
            prior_target_id=None,
            situation_target_ids=(list(_desks) if _desks else None),
        )

    # WORLD branch (S2-T3) — the target-LESS verify-declaring global meta = the
    # world_assessor. It now composes the region_composition heads (degrading a
    # headless region to its country reads, naming a fully-absent region as a
    # gap), NOT the ~24 country heads directly. An early return, so the
    # per-country + legacy switch below is byte-for-byte the P3-T2 code.
    # C-TIER (former SEAMS §44, resolved): flag ON ⇒ the basis bar is the SPLIT
    # floor (threaded through the whole assemble, degrade reads included) and
    # the region/thematic heads the bar excluded come back as marked PERIPHERY
    # rows over the SAME primary scope (the declared roster, target-unscoped,
    # meta-inclusive — region_composition heads ARE meta=True). The DEGRADE
    # path's member-country complement is deliberately NOT gathered (see the
    # module-top C-TIER scope note). Flag OFF (default) ⇒ byte-for-byte legacy.
    if not target_filter and _declares_verify(descriptor):
        _tiered = _tiered_evidence_enabled()
        _floor = (
            _resolve_split_floor(descriptor)
            if _tiered
            else _resolve_verify_floor(descriptor)
        )
        rows = await _attach_freshness(
            conn,
            await _assemble_world_region_slice(
                conn,
                region_analyst_ids=ids,
                time_window_hours=time_window_hours,
                limit=limit,
                verify_floor=_floor,
            ),
        )
        if _tiered:
            periphery = await read_periphery_findings(
                conn,
                analyst_ids=ids,
                time_window_hours=time_window_hours,
                floor=_floor,
                include_meta=True,
            )
            for row in rows:
                row[_EVIDENCE_FLOOR_KEY] = _floor
            rows = rows + periphery
        # CONTINUITY — the world head is TARGET-LESS (see WORLD_TARGET_TOKEN), so
        # its prior read is the target-less lane and its situation register is
        # UNSCOPED: the world read's evidence scope is the whole roster, so
        # narrowing its register to one desk would be a different aperture than
        # the read it annotates.
        return rows + await _gather_continuity_rows(
            conn,
            descriptor=descriptor,
            analyst_ids=ids,
            verify_floor=_floor,
            prior_target_id=None,
        )

    # Two branches (BYTE-FOR-BYTE the P3-T2 per-country + legacy read when the
    # C-TIER flag is OFF, its code default):
    #   * TARGET-SCOPED (per-country composition) ⇒ scope to the country
    #     (``target_id``) + verify-floor; meta findings stay EXCLUDED (the units
    #     are first-order). C-TIER flag ON ⇒ the basis bar is the SPLIT floor
    #     and the unit heads it excluded come back as marked PERIPHERY rows.
    #   * LEGACY GLOBAL meta (target_filter None, no verify) ⇒ the cross-target,
    #     unfiltered read, byte-for-byte unchanged (never tiered).
    if target_filter:
        target_id: str | None = str(target_filter)
        _tiered = _tiered_evidence_enabled()
        verify_floor: float | None = (
            _resolve_split_floor(descriptor)
            if _tiered
            else _resolve_verify_floor(descriptor)
        )
        include_meta = False
    else:
        target_id = None
        _tiered = False
        verify_floor = None
        include_meta = False

    rows = await read_other_analyst_findings(
        conn,
        analyst_ids=ids,
        time_window_hours=time_window_hours,
        limit=limit,
        target_id=target_id,
        verify_floor=verify_floor,
        include_meta=include_meta,
    )
    # F-1: annotate freshness for the PER-COUNTRY composition (``target_filter``
    # set). The LEGACY global meta (``target_filter`` None here → ``target_id``
    # None) stays byte-for-byte — no freshness pass, matching the standing
    # "legacy read unchanged" discipline every branch above honors.
    if target_filter:
        rows = await _attach_freshness(conn, rows)
        if _tiered and verify_floor is not None:
            periphery = await read_periphery_findings(
                conn,
                analyst_ids=ids,
                time_window_hours=time_window_hours,
                floor=verify_floor,
                target_id=target_id,
                include_meta=False,
            )
            for row in rows:
                row[_EVIDENCE_FLOOR_KEY] = verify_floor
            rows = rows + periphery
        # CONTINUITY — the per-COUNTRY composition: its own prior head for THIS
        # desk + THIS desk's open situations. The LEGACY global meta below the
        # guard gets NONE, keeping the standing byte-for-byte legacy discipline.
        rows = rows + await _gather_continuity_rows(
            conn,
            descriptor=descriptor,
            analyst_ids=ids,
            verify_floor=verify_floor,
            prior_target_id=target_id,
            situation_target_id=target_id,
        )
    return rows


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------


__all__ = [
    "AnalystMethodResult",
    "COMPOSITION_SIG_PREFIX",
    "COMPOSITION_SLICE_BUDGET_SHARE",
    "CONTINUITY_CITATION_KEY",
    "CONTINUITY_PRIOR",
    "CONTINUITY_PRIOR_BODY_CHARS",
    "CONTINUITY_PRIOR_LOOKBACK_HOURS",
    "CONTINUITY_PRIOR_RECEIPT",
    "CONTINUITY_ROW_KEY",
    "CONTINUITY_SITUATIONS",
    "CONTINUITY_SITUATIONS_RECEIPT",
    "CONTINUITY_SITUATIONS_ROW_KEY",
    "COUNTRY_COMPOSITION_ANALYST_ID",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_VERIFY_FLOOR",
    "HANDLER_VERSION",
    "KIND_NAME",
    "LLMHandlerLike",
    "MAX_FULL_BODY_CHARS",
    "MAX_INPUT_FINDINGS",
    "composition_body_cap",
    "MetaFindingsDeps",
    "MetaFindingsSynthesizerRunner",
    "OUTPUT_KIND",
    "PERIPHERY_BODY_CHARS",
    "PERIPHERY_CAP",
    "PERIPHERY_TIER",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "REGION_COMPOSITION_ANALYST_ID",
    "REGION_FRAME_TAG",
    "REGION_MODE_COUNTRY_FALLBACK",
    "REGION_MODE_GAP",
    "REGION_MODE_REGION",
    "REGION_MODE_THEMATIC",
    "REGION_MODE_THEMATIC_GAP",
    "REGION_TARGET_PREFIX",
    "SCHEMA_VERSION",
    "SITUATION_REGISTER_CAP",
    "SITUATION_REGISTER_EVIDENCE_CHARS",
    "SITUATION_REGISTER_NAME_CHARS",
    "SITUATION_REGISTER_REF_KIND",
    "THEMATIC_DIMENSION_KEY",
    "THEMATIC_MODE_GAP",
    "THEMATIC_MODE_PRESENT",
    "TIERED_BASIS_FLOOR_DEFAULT",
    "TIERED_EVIDENCE_ENV",
    "VERIFY_FLOOR_ENV",
    "WORLD_TARGET_TOKEN",
    "CONTENTION_GROUP_LIMIT",
    "CONTENTION_VALUES_PER_GROUP",
    "CONTENTION_SCORE_FLOOR_ENV",
    "CONTENTION_SCORE_FLOOR_DEFAULT",
    "_COMPOSITION_SYSTEM",
    "_REGION_COMPOSITION_SYSTEM",
    "_THEMATIC_COMPOSITION_SYSTEM",
    "_WORLD_COMPOSITION_SYSTEM",
    "_WORLD_OVER_REGIONS_SYSTEM",
    "_assemble_thematic_unit_slice",
    "_assemble_world_region_slice",
    "_attach_freshness",
    "_composition_signature",
    "_continuity_rule",
    "_continuity_selection",
    "_correlated_ordinal_components",
    "_correlation_guard",
    "_declares_verify",
    "_detect_stale_inputs",
    "_extract_contested_markers",
    "_extract_ref_markers",
    "_defuse_child_ref_markers",
    "_is_region_target",
    "_gather_continuity_rows",
    "_render_continuity_block",
    "_render_contested_block",
    "_render_contested_absent_line",
    "_resolve_contention_floor",
    "_render_freshness_advisory_block",
    "_render_desk_coverage_block",
    "_render_periphery_block",
    "_render_region_coverage_block",
    "_resolve_desk_roster",
    "_resolve_other_analyst_ids",
    "_resolve_region_member_target_ids",
    "_resolve_region_roster",
    "_resolve_split_floor",
    "_resolve_verify_floor",
    "_resolve_window_hours",
    "_select_periphery",
    "_tiered_evidence_enabled",
    "_resolve_self_analyst_id",
    "build_prompt_module",
    "read_open_contention",
    "read_open_situations",
    "read_other_analyst_findings",
    "read_periphery_findings",
    "read_prior_composition_head",
    "run_method",
    "thematic_dimension",
    "thematic_desks",
]
