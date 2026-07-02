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
own ~4 unit heads, so this cap never actually bites there."""

MAX_WORLD_INPUT_FINDINGS: int = 64
"""Cap for the WORLD/global read (no ``target_id`` stamp). Its slice is already
folded to exactly ONE head per country by ``DISTINCT ON (analyst_id,
target_id)``, so the natural input count IS the desk roster — the cap must stay
>= the roster or the world composition silently drops countries. The P4 pre-push
review (C2) found the 15-cap fused a "Global" read WITHOUT the United States. 64
leaves headroom above the current 24 desks; ``_orient`` warns if it ever trims
on the world path (a dropped input == a country the world read cannot see)."""

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
    """TASK — per-country COMPOSITION. You are given the VERIFIED, faithfulness-checked SUB-CLAIMS (first-order unit findings) for ONE country from up to four bounded units (leadership_transition, energy_security, escalation, narrative_coordination). Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source unit analyst_id, effective_confidence (already min(confidence, faithfulness)), title and body. Produce ONE second-order per-country READ. RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the sub-claim block it rests on; NEVER invent an N and NEVER cite an N not shown; a clause with no sub-claim behind it must NOT assert a fact. (b) HEDGE to the evidence — prefer 'the units indicate / suggest / as of the latest sweep' over categorical claims, and weaken your language as effective_confidence drops. (c) If the sub-claims DISAGREE or point different directions, SURFACE the disagreement explicitly (name the tension) — do NOT average it into a false consensus. (d) Lead body with a one-line BLUF; do not restate any sub-claim verbatim. (e) HONEST EMPTY: if there are no verified sub-claims for this country, say so plainly with confidence 0.0 and NO fabricated evidence. (f) TRACEABILITY — a [[ref:N]] marker is a PROMISE that sub-claim block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown sub-claim blocks actually say. NEVER introduce a fact, proper noun, place-name, or event specific (a magnitude, date, location, or count) that is not present in a cited block — do NOT add concrete details a unit did not state (e.g. an event's magnitude or location, or a named actor, commitment, or position no block mentions). If you cannot ground a clause in a shown block, DROP the clause; an in-range [[ref:N]] does NOT license a claim its block does not make. (g) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence actually shown for a cited block, and invent NO per-unit confidence figure or a unit that is not present; do NOT silently change a unit's stated severity or which driver it called dominant — if you aggregate differing unit severities, say so explicitly (e.g. 'aggregating unit severities moderate+low -> moderate'). Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] markers...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
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
    """TASK — GLOBAL world COMPOSITION. You are given the VERIFIED, faithfulness-checked per-COUNTRY READS (second-order country_composition findings), one or more per country. Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source analyst_id, effective_confidence (already min(confidence, faithfulness)), title and body. You MAY also be given a CONTESTED FACTS block: open disputes over a single fact (subject+predicate) where the arbiter surfaced more than one value cluster. Produce ONE second-order WORLD READ. RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the COUNTRY READ block it rests on; NEVER invent an N, NEVER cite a raw signal, and NEVER cite an N not shown; a clause with no country read behind it must NOT assert a fact. (b) HEDGE to the evidence — prefer 'the country reads indicate / suggest / as of the latest composition' over categorical claims, and weaken your language as effective_confidence drops. (c) SURFACE CROSS-COUNTRY DISAGREEMENT: when one country's read and another's point in different directions, NAME BOTH countries and cite BOTH diverging country-read blocks via their two [[ref:N]] ordinals — do NOT average them into a false global consensus. (d) Lead body with a one-line BLUF; do not restate any country read verbatim. (e) CONTESTED FACTS: when a claim touches a listed contested group, NAME both surfaced sides and mark it [[contested:<contention_id>]] using EXACTLY a contention_id shown in the block; NEVER pick a side the arbiter did not surface and NEVER invent a contested id. (f) HONEST EMPTY: if there are no country reads, say so plainly with confidence 0.0 and NO fabricated evidence. (g) TRACEABILITY — a [[ref:N]] marker is a PROMISE that country-read block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown country reads actually say. NEVER introduce a country, actor, event specific, or figure not present in a cited country-read block; if you cannot ground a clause in a shown block, DROP it (an in-range [[ref:N]] does NOT license a claim its block does not make). (h) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence shown for a cited block, and do NOT silently alter a country read's severity or dominant driver; make any aggregation explicit. Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] (and any [[contested:<id>]]) markers...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
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
# Substrate-read helper — other-analyst findings slice
# ---------------------------------------------------------------------------


async def read_other_analyst_findings(
    conn: asyncpg.Connection,
    *,
    analyst_ids: Sequence[str],
    time_window_hours: int = 24,
    limit: int = 100,
    target_id: str | None = None,
    verify_floor: float | None = None,
    include_meta: bool = False,
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
    dedupe_composition = target_id is not None or include_meta
    if dedupe_composition:
        where.append("f.superseded_by IS NULL")

    if target_id is not None:
        params.append(str(target_id))
        where.append(f"f.target_id = ${len(params)}")

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
    # --- ORIENT --------------------------------------------------------
    # The world/global read (no ``target_id`` stamp) fuses one head PER COUNTRY,
    # so its input count is the desk roster and it must NOT be trimmed to the
    # per-country default (the P4 review C2 found the 15-cap dropped the US from
    # a "Global" read). A per-country composition reads only its own ~4 unit
    # heads, so the default cap never bites there.
    _cap = MAX_INPUT_FINDINGS if options.get("target_id") else MAX_WORLD_INPUT_FINDINGS
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

    if not sliced:
        # Defensive empty-input path. The runtime ordinarily short-circuits
        # before calling us (see ``AnalystActor.run`` NOOP/no_inputs branch),
        # but emit a minimal diagnostic finding rather than crash. Stamped
        # with ``meta=True`` so a downstream "list meta-findings" filter
        # still finds it; confidence=0.0 so it doesn't pollute synthesis
        # confidence stats.
        finding = FindingPayload(
            title="No source findings to synthesize",
            body="The other-analyst output slice for this run was empty.",
            confidence=0.0,
            tags=["empty_slice", "meta"],
            data={
                "meta": True,
                "contributing_analysts": list(contributing_analysts),
            },
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

    # Composition selection — two flavors + the legacy global meta:
    #   * TARGET-SCOPED (``options["target_id"]``) → the per-COUNTRY composition
    #     (country_composition): ``_COMPOSITION_SYSTEM``.
    #   * GLOBAL verify-declaring meta (``options["composition"]``, no target_id)
    #     → the WORLD composition (the repointed world_assessor):
    #     ``_WORLD_COMPOSITION_SYSTEM`` — composes the per-country reads,
    #     surfaces CROSS-COUNTRY disagreement, and (T4) appends the CONTESTED
    #     FACTS block. The actor stamps ``composition``/``contention_groups``.
    #   * else → the legacy GLOBAL meta (analyst_meta_synthesizer.yaml),
    #     byte-for-byte unchanged (``system_prompt`` = ``_SYSTEM_PROMPT``).
    # Both compositions cite sub-claims by their [[ref:N]] ordinal handle,
    # resolved into ``data.citations`` (ref_id=<finding uuid>, ref_kind='finding');
    # the render prefixes each sub-claim block with its [[ref:N]] handle + the
    # finding_id for debug (source ids on).
    target_scoped = bool(options.get("target_id"))
    world_composition = (not target_scoped) and bool(options.get("composition"))
    is_composition = target_scoped or world_composition
    if target_scoped:
        effective_system = _COMPOSITION_SYSTEM
    elif world_composition:
        effective_system = _WORLD_COMPOSITION_SYSTEM
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

    # --- PLAN ----------------------------------------------------------
    user_prompt = _render_user_prompt(
        sliced, contributing_analysts, include_source_ids=is_composition
    )
    if contention_groups:
        user_prompt = user_prompt + "\n" + _render_contested_block(contention_groups)
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

    Returns ``analyst_outputs`` rows with the same column projection that
    downstream lineage extraction expects.
    """
    if analyst_ids:
        ids = [str(a) for a in analyst_ids]
    else:
        ids = _resolve_other_analyst_ids(descriptor)

    if time_window_hours is None:
        time_window_hours = _resolve_window_hours(descriptor)

    # Three branches:
    #   * TARGET-SCOPED (per-country composition) ⇒ scope to the country
    #     (``target_id``) + verify-floor; meta findings stay EXCLUDED (the units
    #     are first-order).
    #   * GLOBAL verify-declaring composition (the world_assessor: target_filter
    #     None AND declares method.llm.verify) ⇒ NO target scope, but apply the
    #     verify-floor gate AND ``include_meta=True`` — the world read composes
    #     country_composition findings, which ARE ``meta=True`` (without
    #     include_meta the slice is silently ZEROED).
    #   * LEGACY GLOBAL meta (target_filter None, no verify) ⇒ the cross-target,
    #     unfiltered read, byte-for-byte unchanged.
    if target_filter:
        target_id: str | None = str(target_filter)
        verify_floor: float | None = _resolve_verify_floor(descriptor)
        include_meta = False
    elif _declares_verify(descriptor):
        target_id = None
        verify_floor = _resolve_verify_floor(descriptor)
        include_meta = True
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
    "SCHEMA_VERSION",
    "VERIFY_FLOOR_ENV",
    "CONTENTION_GROUP_LIMIT",
    "CONTENTION_VALUES_PER_GROUP",
    "_COMPOSITION_SYSTEM",
    "_WORLD_COMPOSITION_SYSTEM",
    "_declares_verify",
    "_extract_contested_markers",
    "_extract_ref_markers",
    "_render_contested_block",
    "_resolve_other_analyst_ids",
    "_resolve_verify_floor",
    "_resolve_window_hours",
    "build_prompt_module",
    "read_open_contention",
    "read_other_analyst_findings",
    "run_method",
]
