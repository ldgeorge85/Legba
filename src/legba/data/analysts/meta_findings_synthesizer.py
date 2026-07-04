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
MAX_EVIDENCE_ITEMS: int = 3

# P3-T3/T7 — how much of a cited sub-claim's body to capture on its citation as
# ``evidence_text`` at synth time, so the composition faithfulness VERIFY (run in
# a LATER actor step) checks each composed clause against the EXACT point-in-time
# evidence the model saw — no verify-time re-fetch (which could read a superseded
# sub-claim). Bounded so ``data['citations']`` stays compact.
MAX_EVIDENCE_TEXT_CHARS: int = 600


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

# The g20+watch desk-coverage roster: one card per active ASSESSED desk (the
# tags every unit fans out to — matches scorecard_producer's roster + the units'
# ``has_tag('g20') or has_tag('watch')`` subscription). The thematic compose diffs
# this roster against the desks with an escalation head to NAME any desk with no
# head as an honest gap (degrade-not-drop). Literal tag list mirrors
# scorecard_producer._G20_TARGETS_SQL (kept in sync).
_DESK_ROSTER_SQL = """
    SELECT descriptor_id, name
      FROM target_descriptors
     WHERE is_head = TRUE
       AND COALESCE(state, 'active') <> 'retired'
       AND (body -> 'scope' -> 'tags') ?| array['g20', 'watch']
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


from ._tradecraft import with_preamble  # noqa: E402

_SYSTEM_PROMPT = with_preamble(
    """TASK — second-order synthesis. You are given FIRST-ORDER FINDINGS from OTHER analysts (each with title, body, confidence, evidence, and a source analyst_id). Produce ONE second-order FINDING that is only visible when these outputs are considered together: the higher-order pattern, the convergent claim, the contradiction, or the emergent narrative. Lead `body` with the BLUF. DO NOT re-state any individual finding verbatim. Cite which analysts ground each claim (by analyst_id). If the findings disagree, surface the disagreement rather than averaging it away.
Respond with strict JSON, nothing else: {"title": "...", "body": "...", "confidence": 0.0-1.0, "evidence": ["..."], "tags": ["..."]}"""
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
    """TASK — per-country COMPOSITION. You are given the VERIFIED, faithfulness-checked SUB-CLAIMS (first-order unit findings) for ONE country from up to seven bounded units (leadership_transition, energy_security, escalation, narrative_coordination, internal_stability, military_posture, economic_coercion). Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source unit analyst_id, effective_confidence (already min(confidence, faithfulness)), title and body. Produce ONE second-order per-country READ. RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the sub-claim block it rests on; NEVER invent an N and NEVER cite an N not shown; a clause with no sub-claim behind it must NOT assert a fact. (b) HEDGE to the evidence — prefer 'the units indicate / suggest / as of the latest sweep' over categorical claims, and weaken your language as effective_confidence drops. (c) If the sub-claims DISAGREE or point different directions, SURFACE the disagreement explicitly (name the tension) — do NOT average it into a false consensus. (d) Lead body with a one-line BLUF; do not restate any sub-claim verbatim. (e) HONEST EMPTY: if there are no verified sub-claims for this country, say so plainly with confidence 0.0 and NO fabricated evidence. (f) TRACEABILITY — a [[ref:N]] marker is a PROMISE that sub-claim block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown sub-claim blocks actually say. NEVER introduce a fact, proper noun, place-name, or event specific (a magnitude, date, location, or count) that is not present in a cited block — do NOT add concrete details a unit did not state (e.g. an event's magnitude or location, or a named actor, commitment, or position no block mentions). If you cannot ground a clause in a shown block, DROP the clause; an in-range [[ref:N]] does NOT license a claim its block does not make. (g) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence actually shown for a cited block, and invent NO per-unit confidence figure or a unit that is not present; do NOT silently change a unit's stated severity or which driver it called dominant — if you aggregate differing unit severities, say so explicitly (e.g. 'aggregating unit severities moderate+low -> moderate'). (h) UNIT COVERAGE — account for EVERY bounded unit whose sub-claim block is SHOWN: either CITE it via its [[ref:N]] handle, or, when its read is unremarkable, NAME the unit and say so plainly (e.g. 'the military_posture and narrative_coordination units report nothing notable this window') — do NOT silently drop a shown unit from the read. Do NOT claim to cover a unit whose block is NOT shown; that unit is an unassessed GAP — name it as such and NEVER infer its state. Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] markers...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
)


# P3-T5 GLOBAL (world) composition system prompt.
#
# Selected in-kind by the runtime's ``options["composition"]`` stamp (set only on
# a verify-declaring GLOBAL meta run — the repointed world_assessor). Mirrors
# ``_COMPOSITION_SYSTEM`` but WORLD-worded: the "sub-claims" here are the
# per-COUNTRY reads (country_composition findings), so the world read cites a
# COUNTRY-READ via its [[ref:N]] ordinal handle and its load-bearing surface is
# CROSS-COUNTRY disagreement. It additionally consumes an appended CONTESTED FACTS
# block (open public.fact_contention disputes) and marks any touched dispute
# ``[[contested:<contention_id>]]`` naming BOTH arbiter-surfaced sides.
_WORLD_COMPOSITION_SYSTEM = with_preamble(
    """TASK — REGIONAL COMPOSITION. You are given the VERIFIED, faithfulness-checked per-COUNTRY READS (second-order country_composition findings) for the member countries of ONE world region, one or more per country. Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source analyst_id, effective_confidence (already min(confidence, faithfulness)), title and body. You MAY also be given a CONTESTED FACTS block: open disputes over a single fact (subject+predicate) where the arbiter surfaced more than one value cluster. Produce ONE second-order REGIONAL READ over the shown country reads. RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the COUNTRY READ block it rests on; NEVER invent an N, NEVER cite a raw signal, and NEVER cite an N not shown; a clause with no country read behind it must NOT assert a fact. (b) HEDGE to the evidence — prefer 'the country reads indicate / suggest / as of the latest composition' over categorical claims, and weaken your language as effective_confidence drops. (c) SURFACE CROSS-COUNTRY DISAGREEMENT: when one country's read and another's point in different directions, NAME BOTH countries and cite BOTH diverging country-read blocks via their two [[ref:N]] ordinals — do NOT average them into a false regional consensus. (d) Lead body with a one-line BLUF that NAMES the specific REGION this read covers — infer the region from the shown country reads, which are ALL members of ONE region — and frame the assessment AS a regional read; do NOT open with a global 'The world faces…' frame (this is a REGION, not the world), and do not restate any country read verbatim. (e) CONTESTED FACTS: when a claim touches a listed contested group, NAME both surfaced sides and mark it [[contested:<contention_id>]] using EXACTLY a contention_id shown in the block; NEVER pick a side the arbiter did not surface and NEVER invent a contested id. (f) HONEST EMPTY: if there are no country reads, say so plainly with confidence 0.0 and NO fabricated evidence. (g) TRACEABILITY — a [[ref:N]] marker is a PROMISE that country-read block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown country reads actually say. NEVER introduce a country, actor, event specific, or figure not present in a cited country-read block; if you cannot ground a clause in a shown block, DROP it (an in-range [[ref:N]] does NOT license a claim its block does not make). (h) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence shown for a cited block, and do NOT silently alter a country read's severity or dominant driver; make any aggregation explicit. Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] (and any [[contested:<id>]]) markers...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
)


# S2-T3 GLOBAL (world) composition over REGIONS system prompt.
#
# Selected in-kind by the runtime's ``options["composition"]`` stamp on the
# target-LESS world_assessor run. Mirrors ``_WORLD_COMPOSITION_SYSTEM`` but
# REGION-worded: the cited blocks are per-REGION reads (region_composition
# findings), so the load-bearing surface is CROSS-REGION disagreement. Because
# the world read DEGRADES a region with no region read to that region's country
# reads, a shown block may instead be one of a region's per-COUNTRY reads (still
# a real, cited block). It additionally consumes the CONTESTED FACTS block (open
# public.fact_contention disputes) and a REGION COVERAGE block that NAMES any
# region with NO read at all — the model must surface those as unassessed gaps.
# Distinct constant from ``_WORLD_COMPOSITION_SYSTEM`` so the S2-T2 region compose
# (which composes COUNTRY reads and keeps that prompt) is untouched.
_WORLD_OVER_REGIONS_SYSTEM = with_preamble(
    """TASK — GLOBAL world COMPOSITION over REGIONS. You are given the VERIFIED, faithfulness-checked per-REGION READS (second-order region_composition findings), one per region. For a region that had NO region read this cycle, one or more of its per-COUNTRY reads are shown IN ITS PLACE (a degrade — treat them as that region's available evidence). Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source analyst_id, effective_confidence (already min(confidence, faithfulness)), title and body. You MAY also be given a CONTESTED FACTS block (open disputes over a single fact where the arbiter surfaced more than one value cluster) and a REGION COVERAGE block naming world regions that have NO read at all this cycle. Produce ONE second-order WORLD READ. RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the read block it rests on; NEVER invent an N, NEVER cite a raw signal, and NEVER cite an N not shown; a clause with no read behind it must NOT assert a fact. (b) HEDGE to the evidence — prefer 'the region reads indicate / suggest / as of the latest composition' over categorical claims, and weaken your language as effective_confidence drops. (c) SURFACE CROSS-REGION DISAGREEMENT: when one region's read and another's point in different directions, NAME BOTH regions and cite BOTH diverging blocks via their two [[ref:N]] ordinals — do NOT average them into a false global consensus. (d) Lead body with a one-line BLUF; do not restate any read verbatim. (e) CONTESTED FACTS: when a claim touches a listed contested group, NAME both surfaced sides and mark it [[contested:<contention_id>]] using EXACTLY a contention_id shown in the block; NEVER pick a side the arbiter did not surface and NEVER invent a contested id. (f) REGION GAPS: if the REGION COVERAGE block lists a region as having NO read, NAME that region plainly as an unassessed gap with NO current read — do NOT infer, estimate, or invent its state, and NEVER attach a [[ref:N]] to a gap region. (g) HONEST EMPTY: if there are no reads at all, say so plainly with confidence 0.0 and NO fabricated evidence. (h) TRACEABILITY — a [[ref:N]] marker is a PROMISE that block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown reads actually say. NEVER introduce a region, country, actor, event specific, or figure not present in a cited block; if you cannot ground a clause in a shown block, DROP it (an in-range [[ref:N]] does NOT license a claim its block does not make). (i) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence shown for a cited block, and do NOT silently alter a read's severity or dominant driver; make any aggregation explicit. Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] (and any [[contested:<id>]]) markers...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
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
    """TASK — GLOBAL THEMATIC COMPOSITION over ESCALATION. You are given the VERIFIED, faithfulness-checked per-DESK ESCALATION READS (first-order `escalation` unit findings), ONE per country desk, each for a DIFFERENT country. Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source analyst_id, its DESK (target_id — the country the read is for), effective_confidence (already min(confidence, faithfulness)), title and body. You MAY also be given a DESK COVERAGE block naming desks that have NO escalation read this cycle. Produce ONE second-order GLOBAL ESCALATION READ that surveys near-term escalation risk ACROSS the desks. RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the desk read block it rests on, and NAME the desk (country) it is about; NEVER invent an N, NEVER cite a raw signal, and NEVER cite an N not shown; a clause with no desk read behind it must NOT assert a fact. (b) HEDGE to the evidence — prefer 'the desk reads indicate / suggest / as of the latest sweep' over categorical claims, and weaken your language as effective_confidence drops. (c) SURFACE CROSS-DESK STRUCTURE: name the desks with the HIGHEST assessed escalation risk and cite each; when desks point in different directions, NAME BOTH and cite BOTH diverging blocks via their two [[ref:N]] ordinals — do NOT average them into a false global consensus. (d) CORRELATION — two desks whose reads rest on the SAME underlying wire signal (a shared cross-border incident, one alliance move seen from both sides) are NOT independent corroboration; do NOT count them twice or let a single shared event inflate the global picture. When two cited desks clearly describe the SAME underlying event, SAY SO rather than presenting them as two independent data points. (e) DESK GAPS: if the DESK COVERAGE block lists a desk as having NO read, NAME that desk plainly as an unassessed gap with NO current escalation read — do NOT infer, estimate, or invent its state, and NEVER attach a [[ref:N]] to a gap desk. (f) Lead body with a one-line BLUF naming where global escalation risk is concentrated; do not restate any desk read verbatim. (g) HONEST EMPTY: if there are no desk reads at all, say so plainly with confidence 0.0 and NO fabricated evidence. (h) TRACEABILITY — a [[ref:N]] marker is a PROMISE that desk-read block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown desk reads actually say. NEVER introduce a country, actor, event specific, or figure not present in a cited block; if you cannot ground a clause in a shown block, DROP it (an in-range [[ref:N]] does NOT license a claim its block does not make). (i) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence shown for a cited block, and do NOT silently alter a desk read's severity or dominant vector; make any aggregation explicit. Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] markers naming each desk...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
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
    # Newest-first; None timestamps sort last. Coerce produced_at to a string so
    # a NULL/str value can never collide with datetime rows under `<` — the
    # heterogeneous-key TypeError that hard-froze the inline_target assessors.
    def _sort_key(row: Mapping[str, Any]) -> str:
        v = row.get("produced_at")
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        iso = getattr(v, "isoformat", None)
        return iso() if callable(iso) else str(v)

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
        body = body[:MAX_BODY_CHARS]
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
            evidence.append(str(e)[:160])
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
            attribution = (
                f"      analyst_id={analyst_id} {fid_part}"
                f"effective_confidence={conf_val} produced_at={produced_at}"
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
# Helpers — output coercion
# ---------------------------------------------------------------------------


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
        return FindingPayload(
            title=str(parsed.get("title") or fallback_title)[:2048],
            body=str(parsed.get("body") or "")[:65536],
            confidence=float(parsed.get("confidence", 0.5)),
            evidence=[str(e) for e in (parsed.get("evidence") or [])][:50],
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
        join = """
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
        """
        params.append(float(verify_floor))
        where.append(f"LEAST(f.confidence, v.faithfulness_score) >= ${len(params)}")
        # Drop coerce-fallback rows even when they score as vacuously faithful.
        where.append(
            "(f.data -> 'tags' ?| array['unstructured','coerce_failed']) IS NOT TRUE"
        )
        select_extra = (
            ", LEAST(f.confidence, v.faithfulness_score) AS effective_confidence,"
            " v.faithfulness_score AS faithfulness_score"
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


async def read_open_contention(
    conn: asyncpg.Connection,
    *,
    limit: int = CONTENTION_GROUP_LIMIT,
    values_per_group: int = CONTENTION_VALUES_PER_GROUP,
) -> list[dict[str, Any]]:
    """Read OPEN contested-fact groups (status ``contested`` / ``surfaced``) +
    their non-junk value clusters for the world composition's CONTESTED FACTS
    block.

    Returns a list of ``{"contention_id", "subject_key", "predicate_key",
    "status", "values": [{"value_key", "surfaced_winner", "arbiter_score",
    "distinct_source_count"}, ...]}``. Only groups with ≥2 non-junk clusters
    (an actual two-sided dispute) are returned. Read-only + bounded; a missing
    relation propagates (the caller treats this additive enrichment as
    best-effort — a contention read failure never blocks the world compose).
    """
    group_rows = await conn.fetch(
        """
        SELECT fc.id, fc.subject_key, fc.predicate_key, fc.status
          FROM fact_contention fc
         WHERE fc.status IN ('contested', 'surfaced')
         ORDER BY fc.updated_at DESC, fc.id DESC
         LIMIT $1
        """,
        int(limit),
    )
    if not group_rows:
        return []

    group_ids = [g["id"] for g in group_rows]
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
    for g in group_rows:
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
    return out


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
        :class:`legba.runtime.analyst_method.LLMHandlerLike`.

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
    return await _run(
        inputs,
        options,
        llm=deps.llm,
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=DEFAULT_TEMPERATURE,
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
    sliced, derived_from, derived_analysts = _orient(inputs, cap=_cap)

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
        finding = FindingPayload(
            title="No source findings to synthesize",
            body="The other-analyst output slice for this run was empty.",
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
                },
                {"phase": "reflect", "kind": "noop_no_inputs"},
            ],
        )

    # Composition selection — three flavors + the legacy global meta (the mode
    # flags were resolved at the top of ``_run``):
    #   * REGION (``options["target_id"]`` = ``region_<slug>``) → the WORLD-shaped
    #     ``_WORLD_COMPOSITION_SYSTEM`` (a region read is MULTI-country, so it uses
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
    # empty-slice branch). A region read is MULTI-country -> world-shaped prompt; the
    # world read is MULTI-region -> the region-worded ``_WORLD_OVER_REGIONS_SYSTEM``.
    if region_scoped:
        effective_system = _WORLD_COMPOSITION_SYSTEM
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
    # stamped onto options. Kept ONLY if actually present (a contention read
    # failure / empty disputes leaves the block absent → the prompt's contested
    # rule is inert; never fabricated).
    contention_groups: list[Mapping[str, Any]] = []
    if world_composition:
        raw_groups = options.get("contention_groups")
        if isinstance(raw_groups, (list, tuple)):
            contention_groups = [g for g in raw_groups if isinstance(g, Mapping)]

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

    # --- PLAN ----------------------------------------------------------
    user_prompt = _render_user_prompt(
        sliced, contributing_analysts, include_source_ids=is_composition
    )
    if contention_groups:
        user_prompt = user_prompt + "\n" + _render_contested_block(contention_groups)
    if region_coverage:
        _coverage_block = _render_region_coverage_block(region_coverage)
        if _coverage_block:
            user_prompt = user_prompt + "\n" + _coverage_block
    if desk_coverage:
        _desk_block = _render_desk_coverage_block(desk_coverage)
        if _desk_block:
            user_prompt = user_prompt + "\n" + _desk_block
    steps: list[dict[str, Any]] = [
        {
            "phase": "orient",
            "kind": "deterministic",
            "in_count": len(inputs),
            "kept_count": len(sliced),
            "derived_count": len(derived_from),
            "analysts": len(contributing_analysts),
        },
        {
            "phase": "plan",
            "kind": "render_prompt",
            "prompt_chars": len(user_prompt),
            "prompt_module": PROMPT_MODULE_PATH,
            "composition": is_composition,
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
        num_subclaims = len(sliced)
        index_by_ordinal: dict[int, Mapping[str, Any]] = {
            n: row for n, row in enumerate(sliced, start=1)
        }
        resolved_ords, dropped_refs = _extract_ref_markers(
            finding.body, num_subclaims
        )
        citations: list[dict[str, Any]] = []
        for n in resolved_ords:
            src_row = index_by_ordinal[n]
            uid = _coerce_uuid(src_row.get("id"))
            if uid is None:
                # No drill target on the cited sub-claim → count, never fabricate
                # a ref (mirrors the unit path's malformed-id handling).
                dropped_refs += 1
                continue
            citation: dict[str, Any] = {
                "marker": f"[[ref:{n}]]",
                "ordinal": n,
                "ref_id": str(uid),
                "ref_kind": "finding",
            }
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
            # P3-T3/T7 — capture the sub-claim's EVIDENCE the verifier needs,
            # point-in-time, so the composition verify runs DB-free. ``data`` is
            # open JSONB so all three keys are additive.
            #   * evidence_text     — the cited sub-claim's body (judge evidence).
            #   * effective_confidence — the verify-floored min(conf, faithful)
            #     the reader surfaced (the T7 hedge/cap ceiling). Guarded: a row
            #     with no eff score is simply omitted → never falsely capped.
            #   * derived_from      — the sub-claim's underlying lineage/signal
            #     ids (the T7 shared-lineage / double-count detector).
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
            citations.append(citation)
        finding.data["citations"] = citations
        steps.append({
            "phase": "cite",
            "kind": "resolve_refs",
            "citations": len(citations),
            "refs_dropped": dropped_refs,
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
    """True iff the descriptor declares ``method.llm.verify`` (the composition
    verify OPT-IN).

    Mirrors ``analyst_deps_builder._verify_llm_component_id`` WITHOUT importing it
    — this kind module stays standalone (no runtime-package load cycle) and the
    check is a simple presence test over the open ``method.llm`` dict (schemas/
    analyst.py: ``dict[str, Any]``, so no schema change). Both compositions
    (country + world) carry ``verify``; the old global meta does NOT → the world
    branch below (verify-floor + include_meta) engages ONLY for a composition.
    """
    method = getattr(descriptor, "method", None)
    llm = getattr(method, "llm", None) if method is not None else None
    if not isinstance(llm, Mapping):
        return False
    return llm.get("verify") is not None


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
      * a region with neither is a GAP (mode ``gap``, 0 inputs).

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
    for r in region_rows:
        tid = str(r.get("target_id") or "")
        if not _is_region_target(tid):
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
    unit is a FIRST-ORDER finding). Then diffs the g20+watch roster:

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
        return await read_other_analyst_findings(
            conn,
            analyst_ids=ids,
            time_window_hours=time_window_hours,
            limit=limit,
            target_ids=member_ids,
            verify_floor=_resolve_verify_floor(descriptor),
            include_meta=True,
        )

    # THEMATIC branch (S2-T4) — a target-LESS run whose descriptor carries a
    # ``subscription.substrate.thematic_dimension`` marker. Fuses ONE verified head
    # per DESK of that UNIT analyst dimension across ALL desks (post-supersession,
    # verify-floored), NAMING any desk with no head as a gap. Checked BEFORE the
    # WORLD branch (both are target-less + verify-declaring) so the marker's
    # presence is the discriminator; an early return leaves the world / per-country
    # / legacy switch below byte-for-byte. ``ids`` = other_analysts (the unit).
    if not target_filter and thematic_dimension(descriptor):
        return await _assemble_thematic_unit_slice(
            conn,
            unit_analyst_ids=ids,
            time_window_hours=time_window_hours,
            limit=limit,
            verify_floor=_resolve_verify_floor(descriptor),
            desk_ids=thematic_desks(descriptor),   # S2-T5: dyad desk allow-list (None ⇒ all desks)
        )

    # WORLD branch (S2-T3) — the target-LESS verify-declaring global meta = the
    # world_assessor. It now composes the region_composition heads (degrading a
    # headless region to its country reads, naming a fully-absent region as a
    # gap), NOT the ~24 country heads directly. An early return, so the
    # per-country + legacy switch below is byte-for-byte the P3-T2 code.
    if not target_filter and _declares_verify(descriptor):
        return await _assemble_world_region_slice(
            conn,
            region_analyst_ids=ids,
            time_window_hours=time_window_hours,
            limit=limit,
            verify_floor=_resolve_verify_floor(descriptor),
        )

    # Two branches (BYTE-FOR-BYTE the P3-T2 per-country + legacy read):
    #   * TARGET-SCOPED (per-country composition) ⇒ scope to the country
    #     (``target_id``) + verify-floor; meta findings stay EXCLUDED (the units
    #     are first-order).
    #   * LEGACY GLOBAL meta (target_filter None, no verify) ⇒ the cross-target,
    #     unfiltered read, byte-for-byte unchanged.
    if target_filter:
        target_id: str | None = str(target_filter)
        verify_floor: float | None = _resolve_verify_floor(descriptor)
        include_meta = False
    else:
        target_id = None
        verify_floor = None
        include_meta = False

    return await read_other_analyst_findings(
        conn,
        analyst_ids=ids,
        time_window_hours=time_window_hours,
        limit=limit,
        target_id=target_id,
        verify_floor=verify_floor,
        include_meta=include_meta,
    )


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------


__all__ = [
    "AnalystMethodResult",
    "COMPOSITION_SIG_PREFIX",
    "COUNTRY_COMPOSITION_ANALYST_ID",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_VERIFY_FLOOR",
    "HANDLER_VERSION",
    "KIND_NAME",
    "LLMHandlerLike",
    "MAX_INPUT_FINDINGS",
    "MetaFindingsDeps",
    "MetaFindingsSynthesizerRunner",
    "OUTPUT_KIND",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "REGION_COMPOSITION_ANALYST_ID",
    "REGION_FRAME_TAG",
    "REGION_MODE_COUNTRY_FALLBACK",
    "REGION_MODE_GAP",
    "REGION_MODE_REGION",
    "REGION_TARGET_PREFIX",
    "SCHEMA_VERSION",
    "THEMATIC_DIMENSION_KEY",
    "THEMATIC_MODE_GAP",
    "THEMATIC_MODE_PRESENT",
    "VERIFY_FLOOR_ENV",
    "WORLD_TARGET_TOKEN",
    "CONTENTION_GROUP_LIMIT",
    "CONTENTION_VALUES_PER_GROUP",
    "_COMPOSITION_SYSTEM",
    "_THEMATIC_COMPOSITION_SYSTEM",
    "_WORLD_COMPOSITION_SYSTEM",
    "_WORLD_OVER_REGIONS_SYSTEM",
    "_assemble_thematic_unit_slice",
    "_assemble_world_region_slice",
    "_composition_signature",
    "_correlated_ordinal_components",
    "_correlation_guard",
    "_declares_verify",
    "_extract_contested_markers",
    "_extract_ref_markers",
    "_is_region_target",
    "_render_contested_block",
    "_render_desk_coverage_block",
    "_render_region_coverage_block",
    "_resolve_desk_roster",
    "_resolve_other_analyst_ids",
    "_resolve_region_member_target_ids",
    "_resolve_region_roster",
    "_resolve_verify_floor",
    "_resolve_window_hours",
    "build_prompt_module",
    "read_open_contention",
    "read_other_analyst_findings",
    "run_method",
    "thematic_dimension",
    "thematic_desks",
]
