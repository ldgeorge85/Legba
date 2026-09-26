# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``contrary_evidence_pass`` — the CONTRARY-EVIDENCE pass (Program 7a).

THE GAP THIS CLOSES, STATED EXACTLY. This platform already runs four kinds of
disagreement machinery and not one of them goes looking for opposition:

  * ``claim_contradiction`` (R2) compares claims that ALREADY CAME IN — P and
    ¬P inside one desk's verified set, over a closed polarity vocabulary;
  * ``fact_contention_arbiter`` compares VALUES ALREADY EXTRACTED for the same
    subject/predicate;
  * ``competing_hypotheses`` (ACH) resolves a thesis against the EVIDENCE BASE
    we happen to hold;
  * ``standing_auditor`` searches the open web FOR THE CLAIM — its query is the
    claim's own, and NOT_FOUND is its honest common answer.

Every one of them is passive about opposition. Nothing in the tree formulates
the COUNTER-query. The operator's 2026-09-23 capture names the model — IARPA
REASON: "for material claims, actively retrieve the strongest counter-evidence,
not only supporting evidence. Emit contention records, never verdicts." This
analyst is that act, and the whole of it.

WHAT IT DOES, ONCE PER DAY, ON THE $0 CORE PLANE
------------------------------------------------
1. SELECTS material claims using the standing auditor's own bar, imported and
   not re-implemented: the same top layer, the same deterministic claim
   enumeration off the published record's spans, the same checkable/uncheckable
   pre-filter, the same severity-then-lead priority. One subtraction — a claim
   longer than R2's single-proposition ceiling is not selected, because a
   compound sentence has no single negation.
2. FORMULATES ONE COUNTER-QUERY per claim. Deterministically where the claim
   takes a side in R2's calibrated polarity vocabulary (no model, replayable
   from the claim text alone); otherwise one bounded core-plane call whose
   prompt says, three times, that the reply is a SEARCH QUERY and never a
   verdict — and whose reply is then re-validated in code against a paraphrase
   gate before a single search is spent.
3. RUNS IT ON THE FREE RUNG. Rung 0 is SearXNG through the granted
   ``web_access`` pack, with the agency gate, the SSRF guard, the governor and
   the invocation ledger all on the path — there is no ad-hoc HTTP in this
   module and no ``httpx`` import. The metered rung is DECLARED, WIRED and OFF:
   ``paid_rung`` defaults to ``false`` and the pass never escalates without it.
4. FETCHES the top hits through the auditor's own fences — robots consulted
   BEFORE the call, non-2xx refused, trafilatura extraction, a discovered
   publication date that is never invented — so a counter-ref this platform
   cites is a page this platform actually holds, hashed with the same function
   the research plane hashes a landed page with.
5. FENCES THE STANCE. The first live run (2026-09-25, 60 claims, $0) produced
   three ``contradicts`` rows and ALL THREE WERE FALSE — two encyclopedia
   articles and a lone think-tank piece from another month. So a counter page
   now has to be a REPORTING page (not an encyclopedia / dictionary host),
   DATED inside the claim's own window, ABOUT the claim's subject in the
   sentence that matched, and NOT ALONE: ``contradicts`` needs two independent
   pages, one is ``qualifies``. The four fences and their live fixtures are
   :mod:`._contrary_fences`; each writes its own number onto the row.
6. WRITES ONE ``claim_contentions`` ROW PER CLAIM, carrying a STANCE derived in
   code from the fetched text against the claim's own polarity. No model is
   asked whether the claim is true, here or anywhere on this path.

WHAT IT NEVER DOES, AND THESE ARE THE DESIGN
--------------------------------------------
* **No verdict.** ``stance`` describes the RETRIEVAL ("a page the platform holds
  asserts the opposite"), never the claim. A verdict column would make this a
  third grader beside the faithfulness judge and the external audit, and
  pooling three populations is how a number stops meaning anything.
* **No corpus writes.** The pages it fetches are selected ADVERSARIALLY — that
  is the point of the pass — and landing that selection in ``signals`` would
  move freshness, source health, salience and calibration for every desk that
  reads them, silently and in one direction. Existing corpus rows are LINKED by
  content hash where they already exist; nothing is created.
* **No spend by default.** Rung 0 only. The metered rung needs this descriptor's
  ``paid_rung: true`` AND the pack's own ``max_cost_usd_per_day`` cap AND the
  provider declared as a fallback; absent any of the three the escalation
  returns a REASON, never a silent fall back to rung 0, and the refusal is
  counted on the heartbeat.

THE HEARTBEAT, AND WHY. A run with no LLM wired, no search binding or no new
reads still ends ``analyst_traces.status='success'`` — the exact shape of the
08-12 judge outage that stayed invisible for three days. So every run, including
one that contended nothing, upserts a row into the existing
``alert_trigger_watermarks`` table under this pass's own ``trigger_class``,
carrying what it did: claims selected, queries issued, searches run, pages
fetched, the stance mix, the paraphrase refusals, and an explicit
``degraded_reason``. A watchdog reading that row can tell a quiet web from a
broken pass, which is the only distinction that matters.

DEGRADE, DON'T DIE. A missing ``deps.pg_pool`` RAISES — a contrary pass that
cannot read the tower must not report a clean zero. Every other gap completes
the run and names itself in the heartbeat.

VOLUME. ``claims_per_run`` (default 60) claims, one search each, at most
``max_refs_per_claim`` (default 2) fetches each: ≤180 egress calls per day
against a pack governor sized in the hundreds of thousands per hour. The
capture's own risk note — "the contrary pass doubles search volume on the
audit's claims" — is why the cadence ships DAILY rather than at the auditor's
hourly drain, and why the cap is a descriptor knob rather than a constant.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

from ....runtime.analyst_method import AnalystMethodResult
from ...provenance.models import FindingPayload
from ...research_evidence import content_hash_for
from ..claim_contradiction import MAX_CLAIM_CHARS
from ._contrary_fences import FENCES, HOST_CLASSES, claim_window
from ._contrary_query import (
    REASON_NO_LLM,
    REASON_PARAPHRASE,
    CounterQuery,
    counter_query_from_polarity,
    describe,
    propose_counter_query,
)
from ._contrary_stance import (
    STANCE_CONTRADICTS,
    STANCE_NONE_FOUND,
    STANCE_QUALIFIES,
    STANCE_SEARCH_FAILED,
    STANCES,
    StanceOutcome,
    derive_stance,
    failed,
)
from ._contrary_store import (
    CONTRARY_PIPELINE_VERSION,
    HEARTBEAT_KEY,
    MATERIAL_ANALYST_IDS,
    READ_FETCH_CAP,
    TRIGGER_CLASS,
    WATERMARK_KEY,
    build_row,
    fetch_reads,
    host_class_catalog,
    link_signals,
    load_state,
    material_claims,
    save_state,
    write_contention,
)
from ._external_audit_fetch import fetch_decisive_page
from ._external_audit_search import escalate_to_paid_rung, paid_rungs, run_search
from ._external_audit_queue import serp_provider_order

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "contrary_evidence_pass"

#: ``deps.extras`` key for the SELF-HOSTED core-plane handler the builder wires
#: via the shared ``_wire_deterministic_llm`` (Anthropic hard-refused there).
LLM_DEPS_EXTRA_KEY = "contrary_evidence_llm"

#: ``deps.extras`` key for the ``web_access`` :class:`AgencyToolBinding`. The
#: BINDING arrives here — not a client, not a URL — so every search and every
#: fetch traverses resolve ∩ allow ∩ applicability, the governor and the ledger.
WEB_BINDING_DEPS_EXTRA_KEY = "contrary_evidence_web_binding"

# --- caps (all overridable via descriptor method.options) ------------------
DEFAULT_CLAIMS_PER_RUN = 60
DEFAULT_MAX_REFS_PER_CLAIM = 2
DEFAULT_SEARCH_LIMIT = 5
DEFAULT_CONTENTION_TTL_HOURS = 168

#: THE HOUSE TEMPERATURE, and it is not a knob.
#:
#: 1.0 everywhere on this platform is a standing operator rule, and there is no
#: argument for an exception here even though this call is a bounded instrument:
#: what it returns is a SEARCH QUERY, re-validated in code against a paraphrase
#: gate before it is ever issued, so sampling variance costs nothing a rejection
#: does not already handle — while a per-run override would put a model's
#: sampling regime in a descriptor field nobody reads and make two runs of the
#: same claim incomparable for a reason invisible on the row.
HOUSE_TEMPERATURE = 1.0

#: Receipt sample cap — ``composition_lineage_sweep``'s count+sample contract.
_SAMPLE_CAP = 25

#: Reasons that are not a counter-query failure. Kept apart from
#: ``_contrary_query``'s set because these describe the RETRIEVAL, and the two
#: have to stratify a week of rows independently.
REASON_SEARCH_REFUSED = "search_refused"
REASON_NO_BINDING = "no_web_binding"
REASON_NO_PAGE = "no_page_fetched"


def _pos(raw: Any, default: int) -> int:
    """A positive-int knob, or its in-source default.

    ``options.get("<literal>")`` at the call site, deliberately: the X-1
    catalog's reachability sweep proves a declared knob is real by grepping for
    the literal read in THIS module, and a key threaded through a variable would
    be invisible to it.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _flag(raw: Any) -> bool:
    """A boolean knob. Absent, malformed or falsey ⇒ OFF, always."""
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# One claim
# ---------------------------------------------------------------------------


async def formulate(
    llm: Any, claim: Any, *, temperature: float = HOUSE_TEMPERATURE,
) -> CounterQuery:
    """The counter-query for one claim — deterministic first, model second.

    The order is the whole preference and it is not about cost. A polarity
    negation is REPLAYABLE: the same claim yields the same query a month later,
    with no model in the loop to have drifted. The model leg exists because the
    calibrated vocabulary is narrow by design, not because it is better.
    """
    deterministic = counter_query_from_polarity(claim.claim_text)
    if deterministic is not None and deterministic.issued:
        return deterministic
    if deterministic is not None and deterministic.reason:
        # A polarity term with no subject to attach it to. Do NOT hand this to
        # the model: the claim HAS a position and the deterministic leg already
        # decided it cannot be queried about. Asking a model to try anyway is
        # how an unqueryable claim acquires a query nobody can replay.
        return deterministic
    if llm is None:
        return CounterQuery(query="", reason=REASON_NO_LLM)
    return await propose_counter_query(
        llm, claim.claim_text, desk_key=claim.desk_key,
        as_of=str(claim.produced_at or ""), temperature=temperature,
    )


async def gather_pages(
    binding: Any,
    results: Sequence[Mapping[str, Any]],
    *,
    cap: int,
    now: datetime,
    robots_cache: Any = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Fetch the top hits through the auditor's fences.

    Returns ``(pages, attempted, refused)``. A search RESULT is a URL and a
    snippet; neither is evidence. Only a page that came back through
    ``fetch_decisive_page`` — robots consulted before the call, non-2xx refused,
    text extracted, date discovered — becomes a ``refs`` entry, and its hash is
    ``research_evidence.content_hash_for`` so this pass and the research plane
    can never disagree about what a page's identity is.
    """
    pages: list[dict[str, Any]] = []
    attempted = refused = 0
    for result in results or ():
        if len(pages) >= cap:
            break
        url = str(result.get("url") or "").strip()
        if not url:
            continue
        attempted += 1
        page, reason = await fetch_decisive_page(
            binding, url, robots_cache=robots_cache
        )
        if page is None:
            refused += 1
            logger.debug(
                "contrary_evidence.fetch_refused url=%s reason=%s", url, reason
            )
            continue
        pages.append({
            "url": page.url,
            "text": page.text,
            "sha256": content_hash_for(page.text),
            "chars": len(page.text),
            "published_at": page.published_at,
            "extracted": page.extracted,
            "status_code": page.status_code,
            "fetched_at": now.isoformat(),
        })
    return pages, attempted, refused


async def contend_one(
    claim: Any,
    *,
    llm: Any,
    binding: Any,
    provider_order: Sequence[str],
    paid_rung: bool,
    search_limit: int,
    max_refs: int,
    now: datetime,
    robots_cache: Any = None,
    temperature: float = HOUSE_TEMPERATURE,
    ttl_hours: int = DEFAULT_CONTENTION_TTL_HOURS,
    host_catalog: Mapping[str, str] | None = None,
) -> tuple[CounterQuery, Any, str, str, dict[str, int]]:
    """One claim, end to end. Returns ``(counter, outcome, rung, reason, spend)``.

    Never raises: every plane on this path degrades to a reasoned
    ``search_failed`` rather than costing the run its heartbeat.
    """
    spend = {"searches": 0, "fetches": 0, "fetch_refused": 0, "paid": 0,
             "paid_refused": 0}
    counter = await formulate(llm, claim, temperature=temperature)
    if not counter.issued:
        return counter, failed(), "", counter.reason, spend
    if binding is None:
        return counter, failed(), "", REASON_NO_BINDING, spend

    spend["searches"] += 1
    envelope, why = await run_search(
        binding, counter.query, limit=search_limit,
        provider_order=provider_order,
    )
    if envelope is None:
        return (counter, failed(), "",
                (why or REASON_SEARCH_REFUSED)[:200], spend)

    rung = envelope.provider or (provider_order[0] if provider_order else "")
    results = list(envelope.results)

    # THE METERED RUNG, and the ONE case it is reached in. Rung 0 ANSWERED with
    # an empty nobody can believe — which is the opposite of the tool's own
    # ladder, whose fallback fires when a rung FAILS. Off by default; a refusal
    # (no cap declared, rung undeclared, key unresolved) comes back as a reason
    # and is COUNTED, never swallowed into a quiet zero.
    if paid_rung and not results and paid_rungs(provider_order):
        spend["paid"] += 1
        paid_envelope, paid_why = await escalate_to_paid_rung(
            binding, counter.query, limit=search_limit,
            provider_order=provider_order,
        )
        if paid_envelope is None:
            spend["paid_refused"] += 1
            logger.info("contrary_evidence.paid_rung_refused why=%s", paid_why)
        else:
            results = list(paid_envelope.results)
            rung = paid_envelope.provider or rung

    if not results:
        # The search ANSWERED and returned nothing. That is none_found, not a
        # failure: the free rung reached the engines and the engines had no
        # opposition to offer. Conflating it with search_failed would make a
        # quiet web look like a broken pass.
        return counter, StanceOutcome(), rung, "", spend

    pages, attempted, refused = await gather_pages(
        binding, results, cap=max_refs, now=now, robots_cache=robots_cache,
    )
    spend["fetches"] += attempted
    spend["fetch_refused"] += refused
    if not pages:
        return counter, failed(), rung, REASON_NO_PAGE, spend

    # THE FOUR FENCES run inside ``derive_stance`` — see ``_contrary_fences``.
    # The claim's own window and the host catalog are the only two things they
    # need that a page does not carry, and both are computed here so no fence
    # reaches for a database from inside the derivation.
    outcome = derive_stance(
        claim.claim_text, pages,
        group=counter.polarity_group, sign=counter.polarity_sign,
        window=claim_window(claim, ttl_hours=ttl_hours),
        catalog=host_catalog,
    )
    return counter, outcome, rung, "", spend


# ---------------------------------------------------------------------------
# The heartbeat and the receipt
# ---------------------------------------------------------------------------


def build_heartbeat_state(
    *,
    ran_at: datetime,
    selection: Mapping[str, int],
    queries_issued: int,
    paraphrase_refused: int,
    stance_mix: Mapping[str, int],
    spend: Mapping[str, int],
    rows_written: int,
    write_failures: int,
    watermark: str,
    degraded_reason: str,
    fence_mix: Mapping[str, int] | None = None,
    host_class_mix: Mapping[str, int] | None = None,
    host_catalog_size: int = 0,
) -> dict[str, Any]:
    """The heartbeat's ``state`` fingerprint.

    ``claims_contended`` counts rows whose stance came from a RETRIEVAL that
    happened — ``search_failed`` is deliberately excluded, the same exclusion
    ``standing_auditor``'s ``claims_checked`` makes and for the same reason: a
    counter that included them would show a dead search plane as a busy pass.

    ``fences`` and ``host_classes`` report ZEROS rather than omitting a key, so
    a week of heartbeats says which fence is doing the work and what the free
    rung is actually returning. The first live run would have read
    ``host_classes: {reference: 3, ...}`` against 3 of 3 false contradictions,
    which is the whole argument for F1 in one line of a receipt.
    """
    contended = sum(
        n for k, n in stance_mix.items() if k != STANCE_SEARCH_FAILED
    )
    fences = dict(fence_mix or {})
    hosts = dict(host_class_mix or {})
    return {
        "sub_handler": SUB_HANDLER_NAME,
        "pipeline_version": CONTRARY_PIPELINE_VERSION,
        "ran_at": ran_at.isoformat(),
        "selection": dict(selection),
        "queries_issued": queries_issued,
        "paraphrase_refused": paraphrase_refused,
        "claims_contended": contended,
        "stances": dict(stance_mix),
        "fences": {f: int(fences.get(f, 0)) for f in FENCES},
        "host_classes": {h: int(hosts.get(h, 0)) for h in HOST_CLASSES},
        "host_catalog_size": int(host_catalog_size),
        "searches": int(spend.get("searches") or 0),
        "pages_fetched": int(spend.get("fetches") or 0),
        "fetch_refused": int(spend.get("fetch_refused") or 0),
        "paid_escalations": int(spend.get("paid") or 0),
        "paid_escalations_refused": int(spend.get("paid_refused") or 0),
        "rows_written": rows_written,
        "write_failures": write_failures,
        "watermark": watermark,
        "degraded": bool(degraded_reason),
        "degraded_reason": degraded_reason,
        "healthy": bool(contended) and not degraded_reason,
    }


def _build_receipt(
    *, state: Mapping[str, Any], samples: Sequence[str],
) -> FindingPayload:
    contended = int(state.get("claims_contended") or 0)
    mix = state.get("stances") or {}
    degraded = str(state.get("degraded_reason") or "")
    selection = state.get("selection") or {}
    headline = (
        f"contended {contended} claim(s) from "
        f"{int(selection.get('reads') or 0)} read(s)"
        if contended else (degraded or "contended nothing this run")
    )
    body = [
        f"Contrary-evidence pass — {headline}.",
        f"  pipeline={CONTRARY_PIPELINE_VERSION} "
        f"watermark={state.get('watermark') or '(cold start)'}",
        f"  selection: {selection}",
        f"  queries_issued={state.get('queries_issued')} "
        f"paraphrase_refused={state.get('paraphrase_refused')}",
        f"  stances={mix}",
        f"  fences={state.get('fences')}",
        f"  host_classes={state.get('host_classes')} "
        f"(catalog {state.get('host_catalog_size')} hosts)",
        f"  searches={state.get('searches')} "
        f"pages_fetched={state.get('pages_fetched')} "
        f"fetch_refused={state.get('fetch_refused')} "
        f"paid={state.get('paid_escalations')}"
        + (
            f"(+{state.get('paid_escalations_refused')} refused)"
            if state.get("paid_escalations_refused") else ""
        ),
        f"  rows_written={state.get('rows_written')} "
        f"write_failures={state.get('write_failures')}",
    ]
    if degraded:
        body.append(f"  DEGRADED: {degraded}")
    body.extend(f"  - {line}" for line in samples[:_SAMPLE_CAP])
    return FindingPayload(
        title=f"Contrary-evidence pass — {headline}"[:2048],
        body="\n".join(body)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME, "contrary_evidence",
              "severity:low"],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "meta": True,
            "contrary_evidence": dict(state),
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: Any, options: Mapping[str, Any], deps: Any
) -> AnalystMethodResult:
    """One contrary-evidence sweep.

    REFUSES LOUD on a missing ``deps.pg_pool`` (the ``composition_lineage_sweep``
    contract). Every other missing plane DEGRADES: the run completes, writes a
    heartbeat that names the gap, and reports ``claims_contended=0``.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "contrary_evidence_pass requires a live deps.pg_pool — refusing to "
            "report a clean contrary pass without reading the tower"
        )
    extras = dict(getattr(deps, "extras", None) or {})
    llm = extras.get(LLM_DEPS_EXTRA_KEY)
    binding = extras.get(WEB_BINDING_DEPS_EXTRA_KEY)

    claims_per_run = _pos(options.get("claims_per_run"), DEFAULT_CLAIMS_PER_RUN)
    max_refs = _pos(
        options.get("max_refs_per_claim"), DEFAULT_MAX_REFS_PER_CLAIM
    )
    search_limit = _pos(options.get("search_limit"), DEFAULT_SEARCH_LIMIT)
    ttl_hours = _pos(
        options.get("contention_ttl_hours"), DEFAULT_CONTENTION_TTL_HOURS
    )
    read_fetch_cap = _pos(options.get("read_fetch_cap"), READ_FETCH_CAP)
    paid_rung = _flag(options.get("paid_rung"))
    provider_order = serp_provider_order(options.get("serp_provider_order"))
    if not paid_rung:
        # BELT AND BRACES over the operator's word. The pass never calls the
        # escalation with the flag off, and with the flag off the ladder it
        # could call it with is rung 0 alone — so a descriptor PUT that adds a
        # metered rung to the list cannot spend anything until a SECOND,
        # explicitly-named knob is also flipped.
        provider_order = provider_order[:1]
    raw_run = options.get("run_id")
    try:
        run_id: UUID | None = (
            raw_run if isinstance(raw_run, UUID) else UUID(str(raw_run))
        )
    except (TypeError, ValueError):
        run_id = None

    now = datetime.now(timezone.utc)
    degraded: list[str] = []
    if llm is None:
        degraded.append(
            "no core-plane LLM wired — only claims with a deterministic "
            "polarity negation can be contended this run"
        )
    if binding is None:
        degraded.append(
            "no web_access binding wired (pack not granted / agency plane down)"
        )

    # ---- select ----------------------------------------------------------
    async with pool.acquire() as conn:
        state = await load_state(conn, key=WATERMARK_KEY)
        watermark = str(state.get("watermark") or "")
        rows, new_watermark = await fetch_reads(
            conn, watermark=watermark, now=now, fetch_cap=read_fetch_cap
        )
        # ONE catalog read per run, for F1's label. Its absence is not a
        # degraded run: F1's decision is the reference-host constant, and an
        # unlabelled host reads as ``unknown``, which passes.
        host_catalog = await host_class_catalog(conn)
    claims, selection = material_claims(
        rows, cap=claims_per_run, max_claim_chars=MAX_CLAIM_CHARS
    )
    selection["read_fetch_cap_reached"] = int(len(rows) >= read_fetch_cap)
    if not rows:
        degraded.append("no new top-layer reads since the watermark")

    # ---- contend ---------------------------------------------------------
    results: list[tuple[Any, CounterQuery, Any, str, str, dict[str, int]]] = []
    spend_total = {"searches": 0, "fetches": 0, "fetch_refused": 0, "paid": 0,
                   "paid_refused": 0}
    robots_cache: Any = None
    if binding is not None:
        from ..agency.robots import RobotsCache  # noqa: PLC0415 — lazy, heavy

        robots_cache = RobotsCache()
    for claim in claims:
        counter, outcome, rung, reason, spend = await contend_one(
            claim, llm=llm, binding=binding, provider_order=provider_order,
            paid_rung=paid_rung, search_limit=search_limit, max_refs=max_refs,
            now=now, robots_cache=robots_cache, ttl_hours=ttl_hours,
            host_catalog=host_catalog,
        )
        for key, value in spend.items():
            spend_total[key] = spend_total.get(key, 0) + value
        results.append((claim, counter, outcome, rung, reason, spend))

    # ---- write -----------------------------------------------------------
    stance_mix: dict[str, int] = {}
    fence_mix: dict[str, int] = {}
    host_class_mix: dict[str, int] = {}
    queries_issued = 0
    paraphrase_refused = 0
    written = failures = 0
    samples: list[str] = []
    async with pool.acquire() as conn:
        for claim, counter, outcome, rung, reason, _spend in results:
            stance_mix[outcome.stance] = stance_mix.get(outcome.stance, 0) + 1
            for fence in getattr(outcome, "fences", ()):
                fence_mix[fence] = fence_mix.get(fence, 0) + 1
            # Counted per PAGE, not per claim: what the free rung returns is a
            # property of the retrieval, and a per-claim count would hide a
            # claim whose two pages were both encyclopedias behind one tick.
            for ref in outcome.refs:
                klass = getattr(ref, "host_class", "") or ""
                if klass:
                    host_class_mix[klass] = host_class_mix.get(klass, 0) + 1
            queries_issued += int(counter.issued)
            paraphrase_refused += int(counter.reason == REASON_PARAPHRASE)
            hashes = [r.sha256 for r in outcome.refs if r.sha256]
            linked = await link_signals(conn, hashes) if hashes else []
            row = build_row(
                claim, counter, outcome, rung=rung, reason=reason, as_of=now,
                ttl_hours=ttl_hours, receipt_id=run_id, ref_signal_ids=linked,
            )
            if await write_contention(conn, row):
                written += 1
            else:
                failures += 1
            if len(samples) < _SAMPLE_CAP:
                samples.append(
                    f"[{outcome.stance}/{outcome.derivation}] "
                    f"{claim.desk_key}: {claim.claim_text[:120]} :: "
                    f"{describe(counter)}"
                    + (f" :: {outcome.url}" if outcome.url else "")
                )

        heartbeat = build_heartbeat_state(
            ran_at=now,
            selection=selection,
            queries_issued=queries_issued,
            paraphrase_refused=paraphrase_refused,
            stance_mix=stance_mix,
            spend=spend_total,
            rows_written=written,
            write_failures=failures,
            watermark=new_watermark,
            degraded_reason="; ".join(degraded),
            fence_mix=fence_mix,
            host_class_mix=host_class_mix,
            host_catalog_size=len(host_catalog),
        )
        heartbeat_ok = await save_state(
            conn, heartbeat, key=HEARTBEAT_KEY,
            fired=bool(stance_mix.get(STANCE_CONTRADICTS)),
        )
        # THE WATERMARK ADVANCES LAST, and only over reads this run actually
        # enumerated. A watermark written before the loop would skip every claim
        # a crash interrupted — the finding_supersession defect, where a cap and
        # an ordering silently froze a whole leg.
        await save_state(conn, {"watermark": new_watermark}, key=WATERMARK_KEY)
    heartbeat["heartbeat_written"] = heartbeat_ok

    if degraded:
        logger.warning(
            "contrary_evidence.degraded reasons=%r claims=%d written=%d",
            degraded, len(claims), written,
        )
    else:
        logger.info(
            "contrary_evidence.ran reads=%d claims=%d queries=%d "
            "contradicts=%d qualifies=%d none_found=%d search_failed=%d "
            "searches=%d pages=%d written=%d",
            int(selection.get("reads") or 0), len(claims), queries_issued,
            stance_mix.get(STANCE_CONTRADICTS, 0),
            stance_mix.get(STANCE_QUALIFIES, 0),
            stance_mix.get(STANCE_NONE_FOUND, 0),
            stance_mix.get(STANCE_SEARCH_FAILED, 0),
            spend_total["searches"], spend_total["fetches"], written,
        )

    return AnalystMethodResult(
        finding=_build_receipt(state=heartbeat, samples=samples),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "CONTRARY_PIPELINE_VERSION",
    "DEFAULT_CLAIMS_PER_RUN",
    "DEFAULT_CONTENTION_TTL_HOURS",
    "DEFAULT_MAX_REFS_PER_CLAIM",
    "DEFAULT_SEARCH_LIMIT",
    "HEARTBEAT_KEY",
    "LLM_DEPS_EXTRA_KEY",
    "MATERIAL_ANALYST_IDS",
    "REASON_NO_BINDING",
    "REASON_NO_PAGE",
    "REASON_SEARCH_REFUSED",
    "STANCES",
    "SUB_HANDLER_NAME",
    "TRIGGER_CLASS",
    "WATERMARK_KEY",
    "WEB_BINDING_DEPS_EXTRA_KEY",
    "build_heartbeat_state",
    "contend_one",
    "formulate",
    "gather_pages",
    "handle",
]
