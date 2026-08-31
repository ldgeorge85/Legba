# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``standing_auditor`` sub-handler — the STANDING EXTERNAL-AUDIT plane (D5).

THE GAP THIS CLOSES. ~84% of this platform's LLM calls are the system watching
itself: faithfulness judges grading our prose against our own citations,
calibration trackers scoring our own bands, lineage sweeps walking our own
``derived_from``. Every one of those is a CONSISTENCY check. None of them can
see the failure mode that matters most to an intelligence product — the tower
being internally immaculate and factually WRONG about the world. The only
external contact the platform has ever had is the episodic operator
correctness round, which is expensive, rare, and (being episodic) cannot
notice a drift between rounds.

This analyst is the standing version of that contact. Daily, on the $0 core
plane, it samples a small n of TOP-LAYER claims — the current world read plus a
rotating handful of desk reads — and checks each against EXTERNAL search,
writing an observable verdict per claim.

WHY ``deterministic`` AND NOT ``inline_target``. An ``llm_planner`` GATHER loop
would let the model choose what to audit. That is exactly backwards: an auditor
whose sample is model-chosen is an auditor that can quietly stop auditing the
uncomfortable desks, and its verdicts could never be replayed. So the SAMPLE,
the CAPS, the ROTATION, the VERDICT VOCABULARY and the WRITES are all code, and
the model is used only twice per head as a bounded instrument: once to lift
checkable claims out of prose, once to judge a claim against retrieved text. Its
verdict is then re-validated in code (see ``parse_verdict_reply`` — a fabricated
URL is dropped and an unsourced verdict is demoted), because an external auditor
that can itself hallucinate a source is worse than none.

TEMPLATE LINEAGE (each leg copied from the closest existing organ, not invented):

  * STRUCTURE — ``composition_lineage_sweep``: the only other analyst that reads
    the composition TOWER heads. META (no ``subscription.targets`` ⇒ one global
    sweep per tick), reads directly via ``deps.pg_pool``, refuses loud on a
    missing pool, bounded per-run caps, a NAMED capped sample on the receipt.
  * $0 LLM LEG — ``signal_summarizer`` / ``fact_contention_arbiter``: the
    deterministic-plane LLM idiom. The handler resolves nothing itself; the
    builder's shared ``_wire_deterministic_llm`` merges a SELF-HOSTED handler
    into ``deps.extras`` under this module's own key and hard-refuses an
    Anthropic component, so a mis-wired descriptor can never route this analyst
    onto the billed plane. No LLM wired ⇒ the run still executes, still writes a
    heartbeat, and says plainly that it audited nothing.
  * SEARCH LEG — the ``web_access`` action pack, through the REAL
    ``AgencyToolBinding.run_tool`` three-way gate. There is NO ad-hoc HTTP in
    this module and no ``httpx`` import: every external byte arrives through the
    registered ``web_search`` pack tool, which owns the SSRF egress guard, the
    governor budget, the ledger row, and the empty-is-suspect doctrine.
  * CRITIQUE — ``structural_claims``: the precedent for a critique that is a
    DIFFERENT KIND rather than a gap in the faithfulness path. Same
    ``analyzed_output_id`` + ``overall_score`` contract so existing readers work
    unchanged, its OWN population stamp
    (:data:`EXTERNAL_AUDIT_PIPELINE_VERSION`), and a title prefix that can never
    collide with the ``title LIKE 'Faithfulness verify%'`` pin every
    faithfulness consumer keys on. ``JUDGE_PIPELINE_VERSION`` is NOT touched:
    this is an independent plane, and pooling its verdicts into the
    faithfulness population would describe a population that never existed.
  * ALERT — ``alert_trigger_scan._write_alert_row``: one ``kind='alert'``
    ``analyst_outputs`` row. This handler writes the ROW and does NOT fan out;
    the outward page is the alert plane's own budgeted step (see
    "INTEGRATION POINT" below).
  * HEARTBEAT — ``claim_watch``: durable state riding the EXISTING
    ``alert_trigger_watermarks`` table (migration 0091) under its own
    ``trigger_class``. No new table.

THE 08-12 LESSON, WHICH IS THE REASON FOR THE HEARTBEAT. A judge outage went
unnoticed for three days because the fallback behaved exactly as designed:
analysts kept running, traces kept landing ``status='success'``, and every
liveness read agreed the fleet was healthy — the deficit was in the GRADER, and
nothing gauged the grader. ``analyst_traces`` alone would repeat that failure
here: a run whose search provider is unbound, whose LLM never resolved, or which
found zero sampled heads still ENDS IN SUCCESS. So every run — including a run
that audited nothing — upserts a heartbeat row carrying WHAT IT DID: when it
ran, how many claims it actually CHECKED (UNCHECKED claims deliberately excluded
from that count, so a dead search plane cannot look busy), the verdict mix, and
an explicit ``degraded_reason``. A watchdog reading that row can tell a quiet
world from a broken auditor, which is the only distinction that matters.

INTEGRATION POINT (alert plane). The ``alert-suppression-guard`` branch —
unmerged at this analyst's base — adds a fleet-wide daily page budget with a
per-trigger-class diversity cap, plus the FRAME-3 steady-state suppression
guard, both living inside ``alert_trigger_scan``'s own candidate pipeline. This
handler deliberately writes only the ``kind='alert'`` ROW and performs no
outward fan-out, so when that branch lands the correct wiring is to give
:data:`ALERT_TRIGGER_CLASS` a slot in the budget's kind vocabulary and let
``apply_daily_page_budget`` rank an external-audit contradiction against the
other classes. Nothing in this module needs to change for that; the row it
writes already carries ``trigger_class`` in its tags and ``routing_hint``.

CADENCE + VOLUME. Daily, ONE global sweep. Per run: at most
``1 + max_desks`` heads (default 4), one bounded extraction call per sampled
head, and at most ``max_claims_total`` (default 6) claims each costing one
``web_search`` invocation plus one judge call. Worst case therefore ~10
core-plane calls and ~6 searches per day — a rounding error against the fleet,
which is the point: the value here is CONTINUITY, not volume.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from ...provenance import AnalystContext, write_analyst_output
from ...provenance.kinds import OutputKind
from ...provenance.models import AlertPayload, CritiquePayload, FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from ._external_audit_sampling import (
    CHECKED_VERDICTS,
    VERDICT_CONTRADICTED,
    VERDICT_NOT_FOUND,
    VERDICT_SUPPORTED,
    VERDICT_UNCHECKED,
    CheckableClaim,
    ClaimVerdict,
    SampledHead,
    head_from_row,
    parse_claims_reply,
    parse_verdict_reply,
    rotate_desks,
)

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "standing_auditor"

#: ``deps.extras`` key for the SELF-HOSTED core-plane handler the builder wires
#: via the shared ``_wire_deterministic_llm`` (Anthropic hard-refused there).
LLM_DEPS_EXTRA_KEY = "standing_auditor_llm"

#: ``deps.extras`` key for the ``web_access`` :class:`AgencyToolBinding`. The
#: binding — not a client, not a URL — is what arrives here, so every search
#: this handler runs traverses resolve ∩ allow ∩ applicability, the governor and
#: the invocation ledger.
WEB_BINDING_DEPS_EXTRA_KEY = "standing_auditor_web_binding"

#: This plane's OWN population-split key. Deliberately NOT
#: ``JUDGE_PIPELINE_VERSION``: external-audit verdicts and faithfulness verdicts
#: are different evidence about different questions, and a mean across the two
#: would describe a population that never existed. Bump this — never that — when
#: the prompts, the verdict vocabulary or the validation below change.
EXTERNAL_AUDIT_PIPELINE_VERSION = "2026-08-29/1"

#: The critique's ``data`` sub-key + the marker every consumer reads.
EXTERNAL_AUDIT_DATA_KEY = "external_audit"

#: Title prefix for every critique this plane writes. MUST NOT collide with
#: ``'Faithfulness verify%'`` — that LIKE pin is what keeps the verify surface
#: from ever reading one of these rows as a faithfulness verdict.
CRITIQUE_TITLE_PREFIX = "External audit"

#: The ``alert_trigger_watermarks`` (mig 0091) partition this plane owns, and
#: the ``trigger_class`` its alert rows carry.
ALERT_TRIGGER_CLASS = "external_audit"
#: The single heartbeat row's key inside that partition.
HEARTBEAT_KEY = "_heartbeat"

# --- caps (all overridable via descriptor method.options; see handler_options) ---
DEFAULT_WINDOW_HOURS = 48
DEFAULT_MAX_DESKS = 3
DEFAULT_MAX_CLAIMS_PER_HEAD = 2
DEFAULT_MAX_CLAIMS_TOTAL = 6
DEFAULT_SEARCH_LIMIT = 5

#: Per-call bounds. Same shape as signal_summarizer's: a wall-clock timeout so a
#: wedged plane cannot hold the run, and an output cap. (The vLLM handler drops
#: ``max_tokens`` from the wire by default — a self-hosted server serves its own
#: budget — so this is the hosted-endpoint safety net, not the live control.)
EXTRACT_MAX_TOKENS = 900
JUDGE_MAX_TOKENS = 1200
LLM_TIMEOUT_SECONDS = 90.0
SEARCH_TIMEOUT_SECONDS = 45.0

#: The composition analysts whose heads are the TOP LAYER. Same membership as
#: ``finding_supersession._COMPOSITION_ANALYST_IDS`` minus the meta-report
#: producers: these three are the reads that assert things about the WORLD.
WORLD_ANALYST_ID = "world_assessor"
DESK_ANALYST_IDS: tuple[str, ...] = ("country_composition", "region_composition")

#: Receipt caps, mirroring composition_lineage_sweep's count+sample contract.
_VERDICT_SAMPLE_CAP = 25


# ---------------------------------------------------------------------------
# Prompts — bounded, single-turn, STRICT JSON both ways
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = (
    "You are the claim-extraction leg of an EXTERNAL AUDIT. You are given one "
    "analytical read this system published about the world. Your ONLY job is "
    "to lift out the claims that an independent person could CHECK against "
    "public reporting, and to write the search query that would check each.\n"
    "\n"
    "A CHECKABLE claim asserts a concrete state of the world: an event that "
    "happened, a policy that was announced, a figure that was published, a "
    "position someone took. It is NOT checkable if it is a forecast, a "
    "judgement, an assessment of intent, a probability, or a statement about "
    "this system's own coverage. Prefer the claim the read leans on hardest — "
    "the one that, if false, would make the read wrong.\n"
    "\n"
    "Write each claim as a SELF-CONTAINED sentence: whoever checks it will not "
    "see the document you read, so resolve every pronoun and name every actor, "
    "place and date. The query is what you would type into a search engine — "
    "short, specific, keyword-shaped, no boolean syntax.\n"
    "\n"
    "Respond with STRICT JSON and nothing else — no prose, no code fences:\n"
    '{"claims": [{"claim": "<self-contained checkable sentence>", '
    '"query": "<search query that would verify or refute it>"}]}\n'
    "Return an EMPTY claims array if the read contains nothing checkable. That "
    "is a legitimate answer and a useful one; inventing a claim to fill the "
    "quota is not."
)

_JUDGE_SYSTEM = (
    "You are the verdict leg of an EXTERNAL AUDIT. You are given ONE claim this "
    "system published, and the results of ONE external web search. Decide "
    "whether the search results SUPPORT the claim, CONTRADICT it, or simply do "
    "not settle it.\n"
    "\n"
    "  SUPPORTED    — a result states or clearly entails the claim.\n"
    "  CONTRADICTED — a result states something incompatible with the claim.\n"
    "  NOT_FOUND    — the results do not settle it either way.\n"
    "\n"
    "NOT_FOUND IS THE DEFAULT AND IT IS NOT A FAILURE. Search covers the "
    "engines that answered, not the world; results that are merely adjacent, "
    "about a different date, or about a different actor settle NOTHING. Never "
    "reach for SUPPORTED because a result is on the same topic, and never reach "
    "for CONTRADICTED because a result omits the claim — absence in a search is "
    "not evidence of absence in the world.\n"
    "\n"
    "Every SUPPORTED or CONTRADICTED verdict MUST quote the text that carries "
    "it and cite the URL that text came from. Use ONLY the URLs listed in the "
    "results below — a URL you did not see in those results will be discarded "
    "and your verdict downgraded, because a fabricated source is the exact "
    "failure this audit exists to catch.\n"
    "\n"
    "Respond with STRICT JSON and nothing else — no prose, no code fences:\n"
    '{"verdict": "SUPPORTED|CONTRADICTED|NOT_FOUND", '
    '"rationale": "<one or two sentences: what the evidence shows and why it '
    'settles or fails to settle the claim>", '
    '"evidence": [{"url": "<a url from the results>", '
    '"quote": "<the exact sentence from that result carrying the verdict>"}]}'
)


def _extract_prompt(head: SampledHead) -> str:
    delta = head.severity_delta or "not stated"
    return (
        f"READ: {head.title}\n"
        f"DESK: {head.desk_key}   ANALYST: {head.analyst_id}\n"
        f"SEVERITY: {head.severity or 'not stated'}   "
        f"SEVERITY_DELTA: {delta}\n"
        f"\n{head.body[:12000]}\n"
    )


def _judge_prompt(claim: CheckableClaim, results: Sequence[Mapping[str, Any]],
                  search_status: Mapping[str, Any]) -> str:
    lines = [
        f"CLAIM: {claim.claim}",
        f"QUERY RUN: {claim.query}",
        "",
        "SEARCH RESULTS:",
    ]
    if not results:
        lines.append("(none)")
    for i, r in enumerate(results, start=1):
        lines.append(f"[{i}] {r.get('title') or '(untitled)'}")
        lines.append(f"    url: {r.get('url') or ''}")
        snippet = str(r.get("snippet") or "").strip().replace("\n", " ")
        if snippet:
            lines.append(f"    text: {snippet[:800]}")
    warning = str(search_status.get("absence_warning") or "").strip()
    if warning:
        lines += ["", f"SEARCH-PLANE WARNING: {warning}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reads — the top-layer heads
# ---------------------------------------------------------------------------

#: The live world read. ``superseded_by IS NULL`` + newest-first is the head
#: contract every composition consumer uses.
_WORLD_HEAD_SQL = """
SELECT ao.id, ao.analyst_id, ao.target_id, ao.title, ao.body, ao.data,
       ao.produced_at
FROM analyst_outputs ao
WHERE ao.kind = 'finding'
  AND ao.analyst_id = $1
  AND ao.superseded_by IS NULL
ORDER BY ao.produced_at DESC, ao.id DESC
LIMIT 1
"""

#: One head per desk. ``DISTINCT ON (target_id)`` + ``produced_at DESC`` is the
#: same head projection ``production_gauge_staleness`` reads the composition
#: heads with, so the two surfaces can never disagree about what "the head" is.
_DESK_HEADS_SQL = """
SELECT DISTINCT ON (ao.target_id)
       ao.id, ao.analyst_id, ao.target_id, ao.title, ao.body, ao.data,
       ao.produced_at
FROM analyst_outputs ao
WHERE ao.kind = 'finding'
  AND ao.analyst_id = ANY($1::text[])
  AND ao.target_id IS NOT NULL
  AND ao.superseded_by IS NULL
  AND ao.produced_at > NOW() - make_interval(hours => $2)
ORDER BY ao.target_id, ao.produced_at DESC, ao.id DESC
LIMIT $3
"""

#: Bounded desk fan-in. The rotation only ever takes ``max_desks`` of these; the
#: cap exists so a fleet growth spurt cannot turn the pre-sort into a big read.
#:
#: IT IS A SAFETY VALVE, NOT A WINDOW — and the distinction matters because this
#: codebase has been bitten by the difference before (``finding_supersession``:
#: an ``ORDER BY produced_at ASC`` + cap pulled the OLDEST rows, so the fresh
#: ones fell outside the window and were never stamped, silently freezing the
#: whole situations leg). ``DISTINCT ON (target_id)`` forces ORDER BY to lead
#: with ``target_id``, so if this cap were ever REACHED it would truncate
#: ALPHABETICALLY: desks late in the alphabet would never once be audited, and
#: nothing would say so. Today the live fleet is ~40 desks against a cap of 400 —
#: a 10x margin, so the cap is unreachable and the ordering is irrelevant. If the
#: fleet ever approaches it, do NOT simply raise this number: the fix is to rank
#: inside SQL (severity, then recency) so the truncation drops the LEAST
#: interesting desks rather than the last ones by name, and to count + report the
#: truncation on the heartbeat the way claim_watch reports ``signals_skipped_ahead``.
_DESK_FETCH_CAP = 400


async def _fetch_heads(
    conn: Any, *, window_hours: int
) -> tuple[SampledHead | None, list[SampledHead]]:
    """The live world read + one head per desk inside the window."""
    world_row = await conn.fetchrow(_WORLD_HEAD_SQL, WORLD_ANALYST_ID)
    world = head_from_row(world_row, world=True) if world_row else None
    desk_rows = await conn.fetch(
        _DESK_HEADS_SQL, list(DESK_ANALYST_IDS), window_hours, _DESK_FETCH_CAP
    )
    desks = [head_from_row(r) for r in desk_rows]
    return world, desks


# ---------------------------------------------------------------------------
# The audit of one claim — search (pack tool) then judge (core plane)
# ---------------------------------------------------------------------------


async def _search_claim(
    binding: Any, claim: CheckableClaim, *, limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Run ONE ``web_search`` through the real agency binding.

    Returns ``(results, search_status, unchecked_reason)``. A non-empty
    ``unchecked_reason`` means the search plane never answered — a BLOCK at the
    gate, a tool failure, a degraded/unverified empty — and the claim must be
    recorded UNCHECKED rather than judged. This is the whole reason the pack
    tool's honesty fields exist: "the search found nothing" and "the search did
    not happen" have to reach the verdict as different things.
    """
    try:
        outcome = await asyncio.wait_for(
            binding.run_tool(
                "web_search", {"query": claim.query, "limit": limit}
            ),
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return [], {}, "web_search timed out"
    except Exception as exc:  # degrade-not-break
        logger.warning("standing_auditor.search_failed err=%s", exc)
        return [], {}, f"web_search raised: {exc}"

    if not getattr(outcome, "admitted", False):
        cause = getattr(outcome, "block_cause", None) or "blocked"
        return [], {}, f"agency gate blocked web_search: {cause}"
    result = getattr(outcome, "tool_result", None)
    if result is None:
        return [], {}, "web_search returned no tool result"
    output = dict(getattr(result, "output", None) or {})
    status = {
        k: output.get(k)
        for k in (
            "status", "degraded", "degraded_detail", "unresponsive_engines",
            "liveness", "liveness_detail", "supports_absence_claim",
            "absence_statement", "absence_warning", "provider", "count",
        )
        if k in output
    }
    if getattr(result, "status", "") != "completed":
        # A degraded/unverified empty arrives HERE, as a clean tool failure
        # carrying its own explanation. Never a silent zero-result success.
        return [], status, str(getattr(result, "error", "") or "web_search failed")
    results = [r for r in (output.get("results") or []) if isinstance(r, Mapping)]
    return [dict(r) for r in results], status, ""


async def _judge_claim(
    llm: Any, claim: CheckableClaim, results: Sequence[Mapping[str, Any]],
    search_status: Mapping[str, Any],
) -> ClaimVerdict:
    """One bounded core-plane verdict call. Degrades to NOT_FOUND, never raises."""
    try:
        response = await asyncio.wait_for(
            llm.chat_complete(
                [{"role": "user",
                  "content": _judge_prompt(claim, results, search_status)}],
                max_tokens=JUDGE_MAX_TOKENS,
                temperature=0.0,
                system=_JUDGE_SYSTEM,
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return ClaimVerdict(claim=claim, verdict=VERDICT_NOT_FOUND,
                            rationale="verdict call timed out")
    except Exception as exc:
        logger.warning("standing_auditor.judge_failed err=%s", exc)
        return ClaimVerdict(claim=claim, verdict=VERDICT_NOT_FOUND,
                            rationale=f"verdict call failed: {exc}")
    allowed = [str(r.get("url") or "") for r in results]
    verdict = parse_verdict_reply(
        getattr(response, "content", "") or "", claim, allowed_urls=allowed
    )
    verdict.search_status = dict(search_status)
    # WHICH model graded this, off the response's own usage record (the
    # fact_contention_arbiter idiom) — provenance that survives a core-plane
    # model swap, so a later audit of the audit can split its population by
    # grader rather than assume there was only ever one.
    usage = getattr(response, "usage", None)
    verdict.judge_model = (getattr(usage, "model", "") or "").strip()
    return verdict


async def _extract_claims(
    llm: Any, head: SampledHead, *, cap: int
) -> list[CheckableClaim]:
    """One bounded core-plane extraction call. Degrades to ``[]``, never raises."""
    try:
        response = await asyncio.wait_for(
            llm.chat_complete(
                [{"role": "user", "content": _extract_prompt(head)}],
                max_tokens=EXTRACT_MAX_TOKENS,
                temperature=0.0,
                system=_EXTRACT_SYSTEM,
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("standing_auditor.extract_timeout desk=%s", head.desk_key)
        return []
    except Exception as exc:
        logger.warning("standing_auditor.extract_failed err=%s", exc)
        return []
    return parse_claims_reply(
        getattr(response, "content", "") or "", head, cap=cap
    )


# ---------------------------------------------------------------------------
# Writes — critique per verdict, alert per contradicted high-severity claim
# ---------------------------------------------------------------------------


def _analyst_ctx(
    *, analyst_id: str, analyst_version: str | None, run_id: UUID,
    target_id: str | None,
) -> AnalystContext:
    return AnalystContext(
        analyst_id=analyst_id,
        analyst_version=analyst_version or "0" * 16,
        run_id=run_id,
        target_id=target_id,
    )


def build_audit_critique_payload(verdict: ClaimVerdict) -> CritiquePayload:
    """The ``CritiquePayload`` for ONE external-audit verdict.

    Uses the EXISTING critique contract — ``analyzed_output_id`` plus a
    top-level ``overall_score`` — so the finding↔critique join and the reads-API
    verification surface work with no change. What differs from a faithfulness
    critique is deliberate and is all readable off the row:

      * the TITLE prefix (never ``Faithfulness verify``), so the LIKE-pinned
        faithfulness readers cannot pick it up;
      * ``data[EXTERNAL_AUDIT_DATA_KEY]`` carrying the verdict ledger and this
        plane's OWN ``pipeline_version``;
      * ``overall_score`` is COMPUTE-AND-SHOW, never a demotion lever. It is
        1.0 for SUPPORTED and NOT_FOUND and 0.0 only for CONTRADICTED, because
        ``effective_confidence = min(confidence, overall_score)`` is a
        faithfulness-path gate and an external audit must not silently reach
        into it — a NOT_FOUND, which is the common case and means only that the
        search did not settle the question, would otherwise quietly demote a
        perfectly sound read. A CONTRADICTED verdict is the one case where the
        demotion is the honest answer, and it is also the case that pages.
    """
    v = verdict.verdict
    head = verdict.claim.head
    score = 0.0 if v == VERDICT_CONTRADICTED else 1.0
    body_lines = [
        f"External audit of {head.analyst_id} @ {head.desk_key} "
        f"(output {head.output_id})",
        f"  claim: {verdict.claim.claim}",
        f"  query: {verdict.claim.query}",
        f"  verdict: {v}",
        f"  standing severity: {head.severity or 'not stated'} "
        f"(delta: {head.severity_delta or 'not stated'})",
    ]
    if verdict.rationale:
        body_lines.append(f"  rationale: {verdict.rationale}")
    for q, u in zip(verdict.quotes, verdict.source_urls):
        body_lines.append(f'  - "{q}" — {u}')
    for u in verdict.source_urls[len(verdict.quotes):]:
        body_lines.append(f"  - (no quote) — {u}")
    if verdict.unchecked_reason:
        body_lines.append(f"  NOT AUDITED: {verdict.unchecked_reason}")
    if verdict.search_status:
        body_lines.append(f"  search: {verdict.search_status}")

    tags = [
        "external_audit",
        f"verdict:{v.lower()}",
        f"desk:{head.desk_key}",
        f"audited_analyst:{head.analyst_id}",
    ]
    if head.severity:
        tags.append(f"audited_severity:{head.severity}")
    return CritiquePayload(
        title=f"{CRITIQUE_TITLE_PREFIX} ({v}) — {head.desk_key}"[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=list(verdict.source_urls),
        tags=tags,
        rubric="external_world_check",
        analyzed_output_id=(
            head.output_id if isinstance(head.output_id, UUID) else None
        ),
        analyzed_analyst_id=head.analyst_id[:256],
        # The existing critique provenance field, used for what it says: WHICH
        # model rendered this verdict. It is the audit plane's analogue of
        # ``judge_llm_ref`` on a faithfulness row, and the reason a core-plane
        # model swap can never silently pool two graders' verdicts.
        judge_model=verdict.judge_model[:128],
        overall_score=score,
        data={
            EXTERNAL_AUDIT_DATA_KEY: {
                "external_audit": True,
                "pipeline_version": EXTERNAL_AUDIT_PIPELINE_VERSION,
                "sub_handler": SUB_HANDLER_NAME,
                **verdict.as_dict(),
            }
        },
    )


def build_audit_alert_payload(verdict: ClaimVerdict) -> AlertPayload:
    """The ``kind='alert'`` row for a CONTRADICTED high-severity claim.

    The severity floor is the AUDITED claim's standing band, not the auditor's
    opinion: a contradiction on a ``severity:low`` desk read is a data-quality
    note, while a contradiction on a ``high``/``critical`` read is the platform
    telling an operator something load-bearing about the world may be wrong.
    """
    head = verdict.claim.head
    quote = verdict.quotes[0] if verdict.quotes else ""
    url = verdict.source_urls[0] if verdict.source_urls else ""
    return AlertPayload(
        title=(
            f"External audit CONTRADICTED a {head.severity} claim on "
            f"{head.desk_key}"
        )[:2048],
        body=(
            f"The standing external auditor checked a claim from "
            f"{head.analyst_id} @ {head.desk_key} (output {head.output_id}) "
            f"against external search and found reporting that CONTRADICTS "
            f"it.\n\n"
            f"CLAIM: {verdict.claim.claim}\n"
            f"VERDICT RATIONALE: {verdict.rationale}\n"
            + (f'EVIDENCE: "{quote}"\n' if quote else "")
            + (f"SOURCE: {url}\n" if url else "")
            + "\nThis is an EXTERNAL check, not a faithfulness verdict: the "
            "read may be perfectly faithful to the signals it cited and still "
            "be wrong about the world. Confirm against the source before "
            "acting; the auditor cites what it retrieved and nothing more."
        )[:65536],
        confidence=1.0,
        evidence=list(verdict.source_urls),
        tags=[
            "deterministic",
            SUB_HANDLER_NAME,
            f"trigger:{ALERT_TRIGGER_CLASS}",
            f"severity:{head.severity}",
            f"desk:{head.desk_key}",
        ],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "trigger_class": ALERT_TRIGGER_CLASS,
            EXTERNAL_AUDIT_DATA_KEY: verdict.as_dict(),
        },
        severity="critical" if head.severity == "critical" else "high",
        routing_hint=ALERT_TRIGGER_CLASS,
    )


async def _write_verdict_rows(
    conn: Any, verdicts: Sequence[ClaimVerdict], *, analyst_id: str,
    analyst_version: str | None, run_id: UUID,
) -> tuple[int, int, int]:
    """Persist one critique per verdict + one alert per alertable verdict.

    Returns ``(critiques, alerts, write_failures)``. A rejected write is counted
    and logged, never raised: one malformed verdict must not cost the run its
    heartbeat, which is the row that proves the auditor is alive.
    """
    critiques = alerts = failures = 0
    for verdict in verdicts:
        head = verdict.claim.head
        ctx = _analyst_ctx(
            analyst_id=analyst_id, analyst_version=analyst_version,
            run_id=run_id, target_id=head.target_id,
        )
        derived = [head.output_id] if isinstance(head.output_id, UUID) else []
        try:
            row, dead = await write_analyst_output(
                conn,
                analyst_ctx=ctx,
                kind=OutputKind.CRITIQUE,
                output_payload=build_audit_critique_payload(verdict),
                derived_from=derived,
                row_id=uuid4(),
            )
        except Exception as exc:
            logger.warning("standing_auditor.critique_write_raised err=%s", exc)
            failures += 1
            continue
        if dead is not None or row is None:
            logger.warning(
                "standing_auditor.critique_rejected claim=%s reason=%s",
                verdict.claim.claim_key, getattr(dead, "reason", "schema_fail"),
            )
            failures += 1
            continue
        critiques += 1

        if not verdict.alertable:
            continue
        try:
            arow, adead = await write_analyst_output(
                conn,
                analyst_ctx=ctx,
                kind=OutputKind.ALERT,
                output_payload=build_audit_alert_payload(verdict),
                derived_from=derived,
                row_id=uuid4(),
            )
        except Exception as exc:
            logger.warning("standing_auditor.alert_write_raised err=%s", exc)
            failures += 1
            continue
        if adead is not None or arow is None:
            logger.warning(
                "standing_auditor.alert_rejected claim=%s reason=%s",
                verdict.claim.claim_key,
                getattr(adead, "reason", "schema_fail"),
            )
            failures += 1
            continue
        alerts += 1
    return critiques, alerts, failures


# ---------------------------------------------------------------------------
# The heartbeat — the 08-12 lesson, made a row
# ---------------------------------------------------------------------------

_HEARTBEAT_SQL = """
INSERT INTO alert_trigger_watermarks (trigger_class, watermark_key, state,
                                      fired_at, updated_at)
VALUES ($1, $2, $3::jsonb, $4, now())
ON CONFLICT (trigger_class, watermark_key) DO UPDATE
   SET state = EXCLUDED.state,
       fired_at = COALESCE(EXCLUDED.fired_at,
                           alert_trigger_watermarks.fired_at),
       updated_at = now()
"""


def build_heartbeat_state(
    *, ran_at: datetime, heads_sampled: Sequence[str], claims_extracted: int,
    claims_checked: int, verdict_mix: Mapping[str, int], critiques: int,
    alerts: int, write_failures: int, degraded_reason: str,
) -> dict[str, Any]:
    """The heartbeat's ``state`` fingerprint.

    ``claims_checked`` counts only verdicts in
    :data:`~._external_audit_sampling.CHECKED_VERDICTS`. That exclusion IS the
    heartbeat: a run whose search provider is unbound still ends in
    ``analyst_traces.status='success'``, so a counter that included UNCHECKED
    claims would show a dead auditor as a busy one — which is precisely how the
    judge outage stayed invisible for three days.
    """
    return {
        "sub_handler": SUB_HANDLER_NAME,
        "pipeline_version": EXTERNAL_AUDIT_PIPELINE_VERSION,
        "ran_at": ran_at.isoformat(),
        "heads_sampled": list(heads_sampled),
        "claims_extracted": claims_extracted,
        "claims_checked": claims_checked,
        "verdicts": dict(verdict_mix),
        "critiques_written": critiques,
        "alerts_written": alerts,
        "write_failures": write_failures,
        "degraded": bool(degraded_reason),
        "degraded_reason": degraded_reason,
        "healthy": bool(claims_checked) and not degraded_reason,
    }


async def _write_heartbeat(
    conn: Any, state: Mapping[str, Any], *, alerts: int
) -> bool:
    """Upsert the single heartbeat row. Returns False on failure (never raises).

    ``fired_at`` advances only when this run actually PAGED, matching the 0091
    column contract (last time this key fired an alert), so the row carries both
    "when did the auditor last run" and "when did it last find something".
    """
    try:
        await conn.execute(
            _HEARTBEAT_SQL,
            ALERT_TRIGGER_CLASS,
            HEARTBEAT_KEY,
            json.dumps(dict(state)),
            datetime.now(timezone.utc) if alerts else None,
        )
        return True
    except Exception as exc:
        # LOUD: the heartbeat is the organ that makes this analyst watchable, so
        # losing it is a real degradation even though the audit itself ran.
        logger.error(
            "standing_auditor.heartbeat_write_failed err=%s — the auditor ran "
            "but did not record that it ran; the liveness family cannot see "
            "this run", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def _pos(raw: Any, default: int) -> int:
    """A positive-int knob, or its in-source default.

    Callers pass ``options.get("<literal>")`` rather than a key, deliberately:
    the X-1 catalog's reachability sweep proves a declared knob is real by
    grepping for the literal read in THIS module, and a key threaded through a
    variable would be invisible to it — declared-but-unreachable config is the
    exact defect that catalog exists to prevent.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _date_key(now: datetime) -> str:
    """The rotation's seed material — the UTC date, so a run is replayable from
    its own timestamp alone."""
    return now.astimezone(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def _build_receipt(
    *, state: Mapping[str, Any], verdicts: Sequence[ClaimVerdict],
    heartbeat_ok: bool, window_hours: int,
) -> FindingPayload:
    checked = int(state.get("claims_checked") or 0)
    mix = state.get("verdicts") or {}
    degraded = str(state.get("degraded_reason") or "")
    headline = (
        f"audited {checked} claim(s) across "
        f"{len(state.get('heads_sampled') or [])} head(s)"
        if checked else (degraded or "audited nothing this run")
    )
    body = [
        f"Standing external audit — {headline}.",
        f"  window_hours={window_hours} "
        f"pipeline={EXTERNAL_AUDIT_PIPELINE_VERSION}",
        f"  heads: {', '.join(state.get('heads_sampled') or []) or '(none)'}",
        f"  extracted={state.get('claims_extracted')} checked={checked} "
        f"verdicts={mix}",
        f"  critiques={state.get('critiques_written')} "
        f"alerts={state.get('alerts_written')} "
        f"write_failures={state.get('write_failures')}",
        f"  heartbeat_written={heartbeat_ok}",
    ]
    if degraded:
        body.append(f"  DEGRADED: {degraded}")
    for v in verdicts[:_VERDICT_SAMPLE_CAP]:
        body.append(f"  - [{v.verdict}] {v.claim.head.desk_key}: "
                    f"{v.claim.claim[:160]}")
    return FindingPayload(
        title=f"Standing external audit — {headline}"[:2048],
        body="\n".join(body)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME, "external_audit",
              "severity:low"],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "meta": True,
            "heartbeat_written": heartbeat_ok,
            "window_hours": window_hours,
            EXTERNAL_AUDIT_DATA_KEY: dict(state),
            "verdicts": [v.as_dict() for v in verdicts[:_VERDICT_SAMPLE_CAP]],
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: Any, options: Mapping[str, Any], deps: Any
) -> AnalystMethodResult:
    """One daily standing-audit sweep.

    REFUSES LOUD on a missing ``deps.pg_pool`` (the composition_lineage_sweep
    contract: an audit that cannot read the tower must not emit a clean-looking
    zero). Every OTHER missing plane — no LLM wired, no search binding, no heads
    in the window — DEGRADES: the run completes, writes a heartbeat that names
    the gap, and reports ``claims_checked=0``. That asymmetry is deliberate. A
    crash is invisible to everything except a log; a heartbeat saying "I ran and
    audited nothing because the search provider is unbound" is the signal an
    operator can actually act on, and it is exactly what was missing on 08-12.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "standing_auditor requires a live deps.pg_pool — refusing to "
            "report a clean external audit without reading the tower"
        )
    extras = dict(getattr(deps, "extras", None) or {})
    llm = extras.get(LLM_DEPS_EXTRA_KEY)
    binding = extras.get(WEB_BINDING_DEPS_EXTRA_KEY)

    window_hours = _pos(options.get("window_hours"), DEFAULT_WINDOW_HOURS)
    max_desks = _pos(options.get("max_desks"), DEFAULT_MAX_DESKS)
    per_head_cap = _pos(
        options.get("max_claims_per_head"), DEFAULT_MAX_CLAIMS_PER_HEAD
    )
    total_cap = _pos(options.get("max_claims_total"), DEFAULT_MAX_CLAIMS_TOTAL)
    search_limit = _pos(options.get("search_limit"), DEFAULT_SEARCH_LIMIT)

    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    analyst_version = options.get("analyst_version")
    raw_run = options.get("run_id")
    try:
        run_id = raw_run if isinstance(raw_run, UUID) else UUID(str(raw_run))
    except (TypeError, ValueError):
        run_id = uuid4()

    now = datetime.now(timezone.utc)
    degraded: list[str] = []
    if llm is None:
        degraded.append(
            "no core-plane LLM wired (method.llm.primary unset or refused)"
        )
    if binding is None:
        degraded.append(
            "no web_access binding wired (pack not granted / agency plane down)"
        )

    # ---- sample ----------------------------------------------------------
    async with pool.acquire() as conn:
        world, desk_heads = await _fetch_heads(conn, window_hours=window_hours)

    sampled: list[SampledHead] = []
    if world is not None:
        sampled.append(world)
    else:
        degraded.append("no live world read to audit")
    sampled.extend(rotate_desks(desk_heads, date_key=_date_key(now),
                                take=max_desks))
    if not sampled:
        degraded.append("no top-layer heads in the window")

    # ---- extract -> search -> judge --------------------------------------
    verdicts: list[ClaimVerdict] = []
    claims_extracted = 0
    # BOTH planes or neither. Extracting claims with no search binding would
    # spend the core plane to produce a pile of UNCHECKED critique rows that say
    # nothing the heartbeat's degraded_reason does not already say — noise in
    # analyst_outputs bought with calls that could never audit anything. The
    # UNCHECKED verdict still earns its place one level down, where the binding
    # EXISTS and an individual search failed: that is a real per-claim fact.
    if llm is not None and binding is not None and sampled:
        for head in sampled:
            if len(verdicts) >= total_cap:
                break
            claims = await _extract_claims(llm, head, cap=per_head_cap)
            claims_extracted += len(claims)
            for claim in claims:
                if len(verdicts) >= total_cap:
                    break
                results, status, unchecked = await _search_claim(
                    binding, claim, limit=search_limit
                )
                if unchecked:
                    verdicts.append(ClaimVerdict(
                        claim=claim, verdict=VERDICT_UNCHECKED,
                        search_status=status, unchecked_reason=unchecked,
                    ))
                    continue
                verdicts.append(await _judge_claim(llm, claim, results, status))

    mix: dict[str, int] = {}
    for v in verdicts:
        mix[v.verdict] = mix.get(v.verdict, 0) + 1
    checked = sum(n for k, n in mix.items() if k in CHECKED_VERDICTS)

    # ---- write -----------------------------------------------------------
    async with pool.acquire() as conn:
        critiques, alerts, failures = await _write_verdict_rows(
            conn, verdicts, analyst_id=analyst_id,
            analyst_version=analyst_version, run_id=run_id,
        )
        state = build_heartbeat_state(
            ran_at=now,
            heads_sampled=[h.desk_key for h in sampled],
            claims_extracted=claims_extracted,
            claims_checked=checked,
            verdict_mix=mix,
            critiques=critiques,
            alerts=alerts,
            write_failures=failures,
            degraded_reason="; ".join(degraded),
        )
        heartbeat_ok = await _write_heartbeat(conn, state, alerts=alerts)

    if degraded:
        logger.warning(
            "standing_auditor.degraded reasons=%r heads=%d checked=%d",
            degraded, len(sampled), checked,
        )
    else:
        logger.info(
            "standing_auditor.ran heads=%d extracted=%d checked=%d "
            "supported=%d contradicted=%d not_found=%d alerts=%d",
            len(sampled), claims_extracted, checked,
            mix.get(VERDICT_SUPPORTED, 0), mix.get(VERDICT_CONTRADICTED, 0),
            mix.get(VERDICT_NOT_FOUND, 0), alerts,
        )

    return AnalystMethodResult(
        finding=_build_receipt(
            state=state, verdicts=verdicts, heartbeat_ok=heartbeat_ok,
            window_hours=window_hours,
        ),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "ALERT_TRIGGER_CLASS",
    "CRITIQUE_TITLE_PREFIX",
    "EXTERNAL_AUDIT_DATA_KEY",
    "EXTERNAL_AUDIT_PIPELINE_VERSION",
    "HEARTBEAT_KEY",
    "LLM_DEPS_EXTRA_KEY",
    "SUB_HANDLER_NAME",
    "WEB_BINDING_DEPS_EXTRA_KEY",
    "build_audit_alert_payload",
    "build_audit_critique_payload",
    "build_heartbeat_state",
    "handle",
]
