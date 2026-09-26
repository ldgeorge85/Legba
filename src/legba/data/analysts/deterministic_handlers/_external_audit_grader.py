# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W-4 — THE GRADER PLANE: the rubric in code, the evidence envelope, and the
double-grade routing that makes instrument validity a standing property.

WHY THE RUBRIC IS CODE AND NOT A PROMPT. The C4 audit overturned a scored R2
verdict for one reason, stated in the audit's own words: *"the string ``tier``
appears nowhere in the scorer."* The grader had said the right thing inside its
own atom and the scorer had never read it. At width that mistake repeats 470
times a day. So what transfers from the R-round protocol is not its two-stage
blind reference read (12 opus lanes; it cannot go standing) — it is the round's
EVIDENTIARY CONTRACT, and it transfers into code:

  * **G-1 source tier.** A decisive verdict needs a Tier-1/2 source. Tier-3-only
    demotes to ``NOT_FOUND``, and the demotion is written into the rationale.
    Ruled F-3 (operator overrule of the design's silent default): an unknown
    domain does NOT silently suppress. It produces a decisive verdict with
    ``decisive_source_tier = NULL``, counted and published as its own visible
    ``tier_unknown`` class — because a suppression you cannot see is worse than
    a class you can argue about.
  * **G-2 the decisive span decides.** No verbatim span in a fetch of the cited
    URL, folded through ``text_fold``, means no decisive verdict. Today's
    shipped auditor validates only that the URL *appeared in the results* — a
    page can be in the result set and not say what the grader claims it says.
    The span check itself is W-3's (:mod:`legba.data.provenance.
    external_span_check`); this module calls it by name behind a soft import and
    degrades honestly when it is absent (see :func:`check_decisive_span`).
  * **G-3 time anchoring.** The admissible window is the READ's own
    ``evidence_window``, not the grader's judgement. Out-of-window sources yield
    ``NOT_FOUND`` with ``unchecked_reason='out_of_window'`` recorded.
  * **G-4 no fabricated source.** Ships already and is kept verbatim: a cited
    URL absent from the results is dropped; a decisive verdict left with no
    surviving URL is demoted.
  * **G-5 the grader never sees the read's own citations.** Faithfulness already
    grades against the evidence map. Handing this grader the read's [N] sources
    collapses two instruments into one and re-creates the 84% problem inside the
    thing built to fix it. The read's citation mix is RECORDED on the ledger row
    (``retrieval_origin_mix``) for the A/B stratum — recorded, not shown.

THE FAMILY FENCE IS NOT A DESCRIPTOR COMMENT. The R2 correction is on the
record: *"The R2 'cross-family independence' lane was VOID, not open. It graded
with ``nvidia/nemotron-3-super-120b`` — the same model family as the production
judge … The independence property the lane exists to test was therefore never
tested."* The fence lives in :func:`~legba.runtime.analyst_deps_builder.
resolve_grader_route` and refuses three things — Anthropic, the writer's family,
the live judge's family. A cross-family property that lives only in prose is
that VOID waiting to happen a second time.

INSTRUMENT VALIDITY, MADE CONTINUOUS. R3 died at raw overlap 0.7451 < 0.75 on 51
shared items after R2 called its 0.804 "the round's methodological result". It
did not replicate. So the stop rule stops being a post-hoc verdict and becomes a
live property: a hash-gated 10% of each day's searched claims PLUS 100% of
CONTRADICTED are re-graded by a FOURTH family over the **byte-identical cached
evidence envelope** (:class:`EvidenceEnvelope`). The envelope is the control R3
lacked — its own declared bias #5 measured search access moving C-A by 0.070, so
an overlap computed over two different result sets measures the SEARCH, not the
graders.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ._external_audit_claims import WidthClaim
from ._external_audit_queue import hash_gate
from ._external_audit_sampling import (
    UNCHECKABLE_VERDICT,
    UNCHECKED_ABSENCE_LIVENESS,
    UNCHECKED_SPAN_CHECK_UNAVAILABLE,
    UNCHECKED_SPAN_FETCH_FAILED,
    VERDICT_CONTRADICTED,
    VERDICT_NOT_FOUND,
    VERDICT_SUPPORTED,
    VERDICT_UNCHECKED,
    normalize_core_plane_text,
    parse_strict_json_object,
)

logger = logging.getLogger(__name__)


#: ``deps.extras`` key for the PRIMARY grader handler (the third family).
GRADER_DEPS_EXTRA_KEY = "standing_auditor_grader"
#: ``deps.extras`` key for the AUDIT RATER (the fourth family), used only on the
#: double-grade sample and only over the cached envelope.
AUDIT_RATER_DEPS_EXTRA_KEY = "standing_auditor_audit_rater"

#: Which rater wrote a ledger row. Closed vocabulary (migration 0190's CHECK).
RATER_PRIMARY = "primary"
RATER_AUDIT = "audit"

#: The rubric's own version — bumped on ANY change to the prompt below, the
#: verdict vocabulary, or the mechanical gates. Pooling across a bump is
#: forbidden, which is ``judge_pipeline_version``'s doctrine applied to this
#: population from its first row rather than retrofitted onto it.
RUBRIC_VERSION = "external_world_check/2026-09-05/1"

#: The double-grade sample rate (design F-5, ruled): 10% of searched claims.
#: ~47/day = 329/week against R3's overlap n of 51 — the one axis on which the
#: standing instrument beats every round outright. Do NOT go below 0.05: at that
#: point the overlap number is noisier than the thing it certifies.
DOUBLE_GRADE_FRACTION = 0.10

#: Pre-registered instrument bands (PREREG §4, applied continuously).
OVERLAP_BAND_STANDS = 0.80
OVERLAP_BAND_CONTINGENT = 0.75

GRADER_MAX_TOKENS = 700
GRADER_TIMEOUT_SECONDS = 90.0

#: How much of a result snippet the grader is shown. Bounded so the token math
#: in the design (~2.1k input) stays true at width.
SNIPPET_CHARS = 800
#: Results shown per claim.
RESULTS_SHOWN = 5


# ---------------------------------------------------------------------------
# The rubric prompt — the vocabulary, and nothing the code does not enforce
# ---------------------------------------------------------------------------

GRADER_SYSTEM = (
    "You are the verdict leg of a STANDING EXTERNAL AUDIT. You are given ONE "
    "claim this system published about the world, and the results of ONE "
    "external web search. Decide whether public reporting SUPPORTS the claim, "
    "CONTRADICTS it, or does not settle it.\n"
    "\n"
    "  SUPPORTED    — a result carries a passage that entails the claim.\n"
    "  CONTRADICTED — a result carries a passage that entails the claim's "
    "negation.\n"
    "  NOT_FOUND    — the search answered and nothing in it decides the claim.\n"
    "\n"
    "NOT_FOUND IS THE DEFAULT AND IT IS NOT A FAILURE. It is a statement about "
    "the SEARCH, never about the world. Search covers the engines that "
    "answered, not the world; a result that is merely adjacent, about a "
    "different date, or about a different actor settles NOTHING. Never reach "
    "for SUPPORTED because a result shares a topic, and never reach for "
    "CONTRADICTED because a result omits the claim — absence in a search is not "
    "evidence of absence in the world.\n"
    "\n"
    "A DECISIVE VERDICT NEEDS A DECISIVE SPAN. Every SUPPORTED or CONTRADICTED "
    "verdict MUST quote, VERBATIM, the sentence that carries it, and cite the "
    "URL that sentence came from. Quote the source's words exactly — the "
    "quotation is re-fetched and byte-checked against the live page, and a "
    "paraphrase fails that check and loses the verdict. Use ONLY the URLs "
    "listed below: a URL you did not see in these results is discarded and your "
    "verdict is downgraded, because a fabricated source is the exact failure "
    "this audit exists to catch in others.\n"
    "\n"
    "IF THE SEARCH MISSED, SAY SO IN ONE QUERY. When the results do not settle "
    "the claim but a better query plainly would, return NOT_FOUND and put that "
    "query in \"retry_query\". Exactly one, keyword-shaped, no boolean syntax. "
    "Leave it empty when a second search would not help.\n"
    "\n"
    "Respond with STRICT JSON and nothing else — no prose, no code fences:\n"
    '{"verdict": "SUPPORTED|CONTRADICTED|NOT_FOUND", '
    '"rationale": "<one or two sentences: what the evidence shows, and why it '
    'settles or fails to settle THIS claim>", '
    '"evidence": [{"url": "<a url from the results>", '
    '"quote": "<the exact sentence from that page carrying the verdict>", '
    '"published": "<the date that page states, or empty>"}], '
    '"retry_query": "<one better query, or empty>"}'
)


def grader_prompt(
    claim: WidthClaim,
    results: Sequence[Mapping[str, Any]],
    *,
    search_status: Mapping[str, Any],
    query: str,
) -> str:
    """The per-claim user turn.

    G-5 IS ENFORCED HERE BY OMISSION, and the omission is the point: the read's
    own citations, its evidence map and its [N] markers are NOT in this prompt.
    The only things the grader sees are the claim, the query that was run, the
    read's admissible window, and the search results.
    """
    lines = [
        f"CLAIM: {claim.claim_text}",
        f"QUERY RUN: {query}",
    ]
    window = _window_line(claim.read_evidence_window)
    if window:
        lines.append(f"ADMISSIBLE SOURCE WINDOW: {window}")
    if claim.absence_shaped:
        lines.append(
            "CLAIM SHAPE: this asserts a NON-event. A search that merely fails "
            "to mention it does NOT support it."
        )
    lines += ["", "SEARCH RESULTS:"]
    if not results:
        lines.append("(none)")
    for i, r in enumerate(results[:RESULTS_SHOWN], start=1):
        lines.append(f"[{i}] {r.get('title') or '(untitled)'}")
        lines.append(f"    url: {r.get('url') or ''}")
        published = str(r.get("published") or r.get("published_at") or "").strip()
        if published:
            lines.append(f"    published: {published}")
        snippet = str(r.get("snippet") or r.get("content") or "").strip()
        snippet = snippet.replace("\n", " ")
        if snippet:
            lines.append(f"    text: {snippet[:SNIPPET_CHARS]}")
    warning = str(search_status.get("absence_warning") or "").strip()
    if warning:
        lines += ["", f"SEARCH-PLANE WARNING: {warning}"]
    return "\n".join(lines)


def _window_line(window: Mapping[str, Any]) -> str:
    """The read's window, for the grader's prompt. THREE SPELLINGS, on purpose.

    The tree stamps this window under two different key pairs and this reader
    saw only one of them. ``composition_window.evidence_window_span`` — which is
    what every width-graded read actually carries — writes
    ``{"oldest", "newest"}``; ``region_rollup`` writes ``{"earliest", "latest"}``;
    the API's own window object uses ``{"from", "to"}``. Reading only
    earliest/latest meant this line rendered EMPTY for the production shape, so
    the grader was asked to judge recency with no window in front of it —
    exactly the condition R3 §4.7 warns produces an invented one.

    ``external_span_check.evidence_window_bounds`` accepts oldest/from and
    newest/to; this accepts all three pairs, so no stamp shape can leave the
    prompt and the gate reading different windows.
    """
    earliest = str(
        window.get("oldest") or window.get("earliest") or window.get("from") or ""
    ).strip()
    latest = str(
        window.get("newest") or window.get("latest") or window.get("to") or ""
    ).strip()
    if earliest and latest:
        return f"{earliest} .. {latest}"
    return latest or earliest or ""


# ---------------------------------------------------------------------------
# The evidence envelope — byte-identical for both raters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceEnvelope:
    """Everything a rater is shown for one claim, captured once.

    THE ENVELOPE IS THE CONTROL. R3's declared bias #5 measured search access
    moving C-A by 0.070 (0.455 with search on n=78; 0.385 without on n=13). An
    overlap computed over two different result sets is a measurement of the
    SEARCH, not of the graders. So the audit rater is handed THIS object — the
    same cached results, the same query, the same status block — and never
    issues a search of its own. Frozen so a caller cannot mutate it between the
    two raters and quietly break the invariant the whole validity number rests
    on.
    """

    query: str
    results: tuple[Mapping[str, Any], ...]
    search_status: Mapping[str, Any] = field(default_factory=dict)
    provider: str = ""
    reformulated_from: str = ""

    @property
    def urls(self) -> list[str]:
        return [str(r.get("url") or "") for r in self.results if r.get("url")]

    @property
    def degraded(self) -> bool:
        return bool(self.search_status.get("degraded"))

    @property
    def liveness(self) -> str:
        return str(self.search_status.get("liveness") or "")

    @property
    def supports_absence_claim(self) -> bool:
        return bool(self.search_status.get("supports_absence_claim"))


# ---------------------------------------------------------------------------
# The graded outcome
# ---------------------------------------------------------------------------


@dataclass
class WidthGrade:
    """ONE (claim, rater) result — the row shape of ``external_grades``."""

    claim: WidthClaim
    verdict: str
    rater_role: str = RATER_PRIMARY
    rationale: str = ""
    uncheckable_class: str | None = None
    unchecked_reason: str = ""
    decisive_url: str = ""
    decisive_span: str = ""
    decisive_span_sha256: str = ""
    decisive_source_tier: int | None = None
    decisive_published_at: str = ""
    archive_ref: str = ""
    source_urls: list[str] = field(default_factory=list)
    retry_query: str = ""
    search_provider: str = ""
    search_status: str = ""
    search_liveness: str = ""
    search_degraded: bool = False
    grader_family: str = ""
    grader_component_id: str = ""
    grader_model_name: str = ""
    grader_served_by: str = ""
    sample_fraction: float = 1.0

    @property
    def decisive(self) -> bool:
        return self.verdict in (VERDICT_SUPPORTED, VERDICT_CONTRADICTED)

    @property
    def tier_unknown(self) -> bool:
        """F-3, as the operator ruled it: a VISIBLE class, counted separately.

        A decisive verdict whose decisive URL resolved to no registered tier.
        The design's default silently demoted these; that suppresses true
        CONTRADICTED without leaving a trace. Here the verdict stands and the
        unknown tier is published as its own class, so the suppression is an
        argument somebody can have rather than a hole nobody can see.
        """
        return self.decisive and self.decisive_source_tier is None

    def as_dict(self) -> dict[str, Any]:
        claim = self.claim
        return {
            "claim_key": claim.key,
            "population": claim.population,
            "graded_output_id": claim.graded_output_id,
            "origin_head_id": claim.origin_head_id,
            "block_ordinal": claim.block_ordinal,
            "span_role": claim.span_role,
            "analyst_id": claim.analyst_id,
            "target_id": claim.target_id,
            "desk_key": claim.desk_key,
            "claim_text": claim.claim_text,
            "claim_severity": claim.claim_severity,
            "assembly_regime": claim.assembly_regime,
            "scope_bounded": claim.scope_bounded,
            "absence_shaped": claim.absence_shaped,
            "verdict": self.verdict,
            "uncheckable_class": self.uncheckable_class,
            "unchecked_reason": self.unchecked_reason or None,
            "decisive_url": self.decisive_url or None,
            "decisive_span": self.decisive_span or None,
            "decisive_span_sha256": self.decisive_span_sha256 or None,
            "decisive_source_tier": self.decisive_source_tier,
            "decisive_published_at": self.decisive_published_at or None,
            "archive_ref": self.archive_ref or None,
            "source_urls": list(self.source_urls),
            "search_provider": self.search_provider or None,
            "search_status": self.search_status or None,
            "search_liveness": self.search_liveness or None,
            "search_degraded": self.search_degraded,
            "grader_family": self.grader_family,
            "grader_component_id": self.grader_component_id,
            "grader_model_name": self.grader_model_name or None,
            "grader_served_by": self.grader_served_by or None,
            "rubric_version": RUBRIC_VERSION,
            "rater_role": self.rater_role,
            "retrieval_origin_mix": dict(claim.retrieval_origin_mix),
            "read_evidence_window": dict(claim.read_evidence_window),
            "sample_fraction": self.sample_fraction,
        }


# ---------------------------------------------------------------------------
# The pre-search, deterministic verdicts
# ---------------------------------------------------------------------------


def uncheckable_grade(
    claim: WidthClaim, *, sample_fraction: float = 1.0,
    grader_family: str = "", grader_component_id: str = "",
) -> WidthGrade:
    """The UNCHECKABLE row for a claim the pre-filter classified.

    Written to the ledger rather than dropped, because ~9% of every read is this
    class and a loop that silently discards it publishes a smaller population
    than it audited — which is the same defect as excluding it from the
    denominator without saying so.

    ``grader_family``/``grader_component_id`` are the CALLER's resolved route —
    ``grade_one`` only reaches this branch once a grader is wired, so the claim
    is decided under that configuration even though no call was made. Migration
    0190's ``external_grades_grader_component_nonempty`` / ``..._family_nonempty``
    CHECK constraints make every row attributable to a grader; a row decided
    before any query is still a statement made under one.
    """
    return WidthGrade(
        claim=claim,
        verdict=UNCHECKABLE_VERDICT,
        uncheckable_class=claim.uncheckable_class,
        rationale=(
            f"deterministic pre-filter: {claim.uncheckable_class} — this claim "
            "has no world truth-maker, so no search can decide it"
        ),
        sample_fraction=sample_fraction,
        grader_family=grader_family,
        grader_component_id=grader_component_id,
    )


def absence_unverified_grade(
    claim: WidthClaim, envelope: EvidenceEnvelope, *, sample_fraction: float = 1.0,
    grader_family: str = "", grader_component_id: str = "",
) -> WidthGrade:
    """An absence claim the search plane cannot honestly grade.

    The ``web_access`` pack's own doctrine: an empty result set is suspect by
    default, and an absence claim may only be graded when the search returns
    ``status=empty_verified`` AND ``supports_absence_claim=true``, which requires
    a live control probe proving the engine set is alive. Since 2026-09-21/1
    this grade is rendered ONLY for an unverified EMPTY: a search that returned
    hits is handed to the grader under the claim-shape rule instead (before
    that, every absence claim with hits was refused here — 104 of 104 in the
    24 h measured). So the honest answer for the unverified empty is UNCHECKED
    with the reason named, not NOT_FOUND (which would read as "the search
    answered") and certainly not SUPPORTED.

    ``grader_family``/``grader_component_id`` are the CALLER's resolved route,
    stamped here even though no grader call happened: the search plane's own
    refusal to answer is a verdict rendered under the configured grader, not an
    ungraded row, and migration 0190's non-empty CHECK on both columns requires
    exactly that attribution. ``grader_served_by`` stays unset — that field
    means "who actually answered", and nobody did.
    """
    return WidthGrade(
        claim=claim,
        verdict=VERDICT_UNCHECKED,
        unchecked_reason=UNCHECKED_ABSENCE_LIVENESS,
        rationale=(
            "absence claim: the search plane did not verify its own emptiness "
            f"(liveness={envelope.liveness or 'unverified'}, "
            f"supports_absence_claim={envelope.supports_absence_claim})"
        ),
        search_provider=envelope.provider,
        search_status=str(envelope.search_status.get("status") or ""),
        search_liveness=envelope.liveness,
        search_degraded=envelope.degraded,
        sample_fraction=sample_fraction,
        grader_family=grader_family,
        grader_component_id=grader_component_id,
    )


# ---------------------------------------------------------------------------
# One grader call
# ---------------------------------------------------------------------------


async def grade_claim(
    llm: Any,
    claim: WidthClaim,
    envelope: EvidenceEnvelope,
    *,
    rater_role: str = RATER_PRIMARY,
    grader_family: str = "",
    grader_component_id: str = "",
    sample_fraction: float = 1.0,
) -> WidthGrade:
    """One bounded grader call over the cached envelope. Never raises.

    A timeout or a transport failure yields ``UNCHECKED`` with the reason on the
    row — not ``NOT_FOUND``. "The search found nothing" and "the grader did not
    answer" are different facts about different planes, and a vocabulary that
    shares a shape between them is how a dead grader looks like a quiet world.
    """
    grade = WidthGrade(
        claim=claim,
        verdict=VERDICT_NOT_FOUND,
        rater_role=rater_role,
        grader_family=grader_family,
        grader_component_id=grader_component_id,
        search_provider=envelope.provider,
        search_status=str(envelope.search_status.get("status") or ""),
        search_liveness=envelope.liveness,
        search_degraded=envelope.degraded,
        sample_fraction=sample_fraction,
    )
    try:
        response = await asyncio.wait_for(
            llm.chat_complete(
                [{"role": "user", "content": grader_prompt(
                    claim, envelope.results,
                    search_status=envelope.search_status,
                    query=envelope.query,
                )}],
                max_tokens=GRADER_MAX_TOKENS,
                temperature=0.0,
                system=GRADER_SYSTEM,
            ),
            timeout=GRADER_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        grade.verdict = VERDICT_UNCHECKED
        grade.unchecked_reason = "timeout"
        grade.rationale = "grader call timed out"
        grade.grader_model_name = configured_model_id(llm)
        return grade
    except Exception as exc:
        # THE 2.7-DAY SILENCE (2026-09-18 -> 09-20). OpenRouter removed
        # ``mistralai/mistral-large-2512`` and every grader call 404'd here.
        # The auditor kept writing rows, the family label kept saying
        # "mistral", and this line was the ONLY trace — at WARNING, with no
        # model id on it, so neither a log grep nor a row query could name the
        # dead model. The model id goes on the row now (below) and the
        # liveness watchdog pages on the dead route; this log names the
        # component and the model so the three agree.
        grade.grader_model_name = configured_model_id(llm)
        logger.warning(
            "external_audit.grade_failed component=%s model=%s err=%s",
            grade.grader_component_id or "<unresolved>",
            grade.grader_model_name or "<unrecorded>", exc,
        )
        grade.verdict = VERDICT_UNCHECKED
        grade.unchecked_reason = "grader_unavailable"
        grade.rationale = f"grader call failed: {exc}"
        return grade

    _stamp_provenance(grade, response, llm=llm)
    parse_grader_reply(
        getattr(response, "content", "") or "", grade, allowed_urls=envelope.urls
    )
    return grade


def configured_model_id(llm: Any) -> str:
    """The model id the RESOLVED grader handler is configured to call.

    Read off the live :class:`~legba.data.stack.llm.base.LLMProviderHandler`
    built from the stack component — ``_cfg.model_name.raw`` is the value the
    handler will put on the wire, so this is the model the verdict was graded
    with (or attempted with) whether or not the provider echoed it back.

    WHY THIS EXISTS. ``grader_model_name`` was sourced ONLY from
    ``response.usage.model``. A provider that does not echo the model — and
    every provider that returns an ERROR rather than a response — left the
    field empty, and the critique body rendered the string that started this
    lane: ``grader: mistral (model unrecorded)``. When OpenRouter removed
    ``mistralai/mistral-large-2512`` on 2026-09-18 the auditor therefore wrote
    2.7 days of rows that named a FAMILY and no model, over a route that was
    answering nothing. The configured id is always knowable; there is no state
    in which the row has to say "unrecorded".

    Never raises and never guesses: an unconfigured or foreign object yields
    ``""``, which still renders honestly.
    """
    for attr in ("_cfg", "cfg", "config"):
        cfg = getattr(llm, attr, None)
        model = getattr(cfg, "model_name", None)
        raw = getattr(model, "raw", model)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:256]
    direct = getattr(llm, "model", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()[:256]
    return ""


def _stamp_provenance(
    grade: WidthGrade, response: Any, *, llm: Any | None = None
) -> None:
    """WHICH model, and WHO SERVED IT, off the response's own record.

    D5_VERDICT F4 measured the serving PROVIDER moving verdicts as much as an
    entire prompt program — 9 pass/fail flips in 66 items (13.6%) between two
    OpenRouter endpoints of the SAME model id. A ledger that records the model
    and not the server cannot split a population that moved for that reason, so
    both go on the row.

    The response is the FIRST authority on the model (it is what actually ran);
    the configured handler is the fallback when the provider does not echo one
    (see :func:`configured_model_id`). ``grader_served_by`` has no fallback and
    must not acquire one — that field means "who actually answered", and only
    the answer can say.
    """
    usage = getattr(response, "usage", None)
    grade.grader_model_name = (getattr(usage, "model", "") or "").strip()[:256]
    if not grade.grader_model_name:
        grade.grader_model_name = configured_model_id(llm)
    raw = getattr(response, "raw_response", None)
    if isinstance(raw, Mapping):
        served = raw.get("provider")
        if isinstance(served, str) and served.strip():
            grade.grader_served_by = served.strip()[:256]


def parse_grader_reply(
    content: str, grade: WidthGrade, *, allowed_urls: Sequence[str]
) -> WidthGrade:
    """Parse one grader reply INTO ``grade``, enforcing G-4 in code.

    Two rules held here rather than trusted to the prompt, both inherited
    verbatim from the shipped auditor because they are the reason its verdicts
    are worth anything:

      * an out-of-vocabulary or absent verdict becomes ``NOT_FOUND``, never a
        guess at what the model meant;
      * a cited URL not present in the results is DROPPED, and a decisive
        verdict left with no surviving URL is demoted to ``NOT_FOUND`` with the
        demotion written into the rationale. An auditor that can hallucinate its
        own source is worse than none.
    """
    parsed = parse_strict_json_object(content)
    if parsed is None:
        grade.verdict = VERDICT_NOT_FOUND
        grade.rationale = "grader reply was unparsable"
        return grade
    verdict = str(parsed.get("verdict") or "").strip().upper()
    if verdict not in (VERDICT_SUPPORTED, VERDICT_CONTRADICTED, VERDICT_NOT_FOUND):
        verdict = VERDICT_NOT_FOUND
    grade.rationale = normalize_core_plane_text(
        str(parsed.get("rationale") or "")
    ).strip()[:4000]
    grade.retry_query = normalize_core_plane_text(
        str(parsed.get("retry_query") or "")
    ).strip()[:400]

    allow = {str(u) for u in allowed_urls if u}
    urls: list[str] = []
    first_quote = ""
    first_url = ""
    first_published = ""
    for entry in parsed.get("evidence") or []:
        if not isinstance(entry, Mapping):
            continue
        url = str(entry.get("url") or "").strip()
        quote = normalize_core_plane_text(str(entry.get("quote") or "")).strip()
        if url and url not in allow:
            logger.warning(
                "external_audit.fabricated_url claim=%s url=%s — dropped (not "
                "in the results this grader was shown)",
                grade.claim.key[:16], url,
            )
            continue
        if not url:
            continue
        urls.append(url)
        if not first_url:
            first_url = url
            first_quote = quote[:2000]
            first_published = str(entry.get("published") or "").strip()[:64]

    if verdict in (VERDICT_SUPPORTED, VERDICT_CONTRADICTED) and not urls:
        logger.warning(
            "external_audit.unsourced_verdict claim=%s verdict=%s — demoted to "
            "NOT_FOUND (no surviving source URL)", grade.claim.key[:16], verdict,
        )
        grade.rationale = (
            f"{grade.rationale} [demoted: the grader returned "
            f"{parsed.get('verdict')!r} with no source URL from the results]"
        ).strip()
        verdict = VERDICT_NOT_FOUND

    grade.verdict = verdict
    grade.source_urls = urls[:5]
    grade.decisive_url = first_url if verdict != VERDICT_NOT_FOUND else ""
    grade.decisive_span = first_quote if verdict != VERDICT_NOT_FOUND else ""
    grade.decisive_published_at = (
        first_published if verdict != VERDICT_NOT_FOUND else ""
    )
    return grade


# ---------------------------------------------------------------------------
# G-1 / G-2 / G-3 — W-3's span check, called by name behind a soft import
# ---------------------------------------------------------------------------


def _load_span_check() -> Any | None:
    """W-3's ``external_span_check.check_span``, or ``None``.

    A SOFT import, kept from the original wiring: the checker lives under
    ``data.provenance`` for a reason (it must stay importable from the registry
    image and from a bare test), and a hard import here would couple this
    module's import graph to that decision. When the module is genuinely absent
    every decisive verdict degrades to ``UNCHECKED/span_check_unavailable`` —
    the honest answer, because without the span check a decisive verdict rests
    on the grader's own assertion that a page says something, which is precisely
    the assurance G-2 exists to stop trusting.

    WHAT THE SOFTNESS COST, 2026-09-05 -> 09-06. The import succeeded and the
    CALL did not: this module invoked ``check_span(claim_text=, url=, span=,
    evidence_window=)`` against a function whose parameters are ``(claim,
    candidate_url, fetched_text, published_at, evidence_window, *, produced_at,
    archive_root)``. Every call raised ``TypeError`` into the except-branch
    below, and the branch's honest degrade made the failure look like the
    module's absence — 168 warnings and 211 unchecked decisive proposals that
    each read, in the ledger, exactly like "W-3 has not landed yet". A soft
    import protects against a missing module; nothing here protected against a
    call that could never bind, which is why :func:`span_check_arguments` is now
    a named function a signature test binds against.
    """
    try:
        from ...provenance.external_span_check import check_span
    except Exception:
        return None
    return check_span


def span_check_arguments(
    grade: WidthGrade, page: Any, *, archive_root: Any = None,
    grace_before_hours: float = 0.0,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """EXACTLY the arguments the drain hands W-3's ``check_span``.

    Extracted from the call site on purpose. The 09-05 seam defect was a call
    that could not bind, and it survived a full test suite because every test
    that reached this code passed a MOCK checker — a mock accepts any signature,
    so the one thing that was wrong was the one thing nothing could see. A
    function that BUILDS the arguments can be handed to
    ``inspect.signature(check_span).bind(*args, **kwargs)`` in a test with no
    page, no binding and no pool, and that test fails the moment either side of
    the seam moves.

    The shape is W-3's own §5 contract: a mapping ``DecisiveClaim.coerce``
    accepts, the candidate URL, the page text ALREADY FETCHED by the auditor's
    binding (W-3 does not fetch — "the auditor's binding does"), the page's
    stated publication date, the read's own evidence window, and the three
    keyword-only anchors. ``grace_before_hours`` is the drain's resolved
    ``window_grace_hours`` knob (default ``0.0`` — today's rule); it is always
    passed explicitly rather than left to ``check_span``'s own default so a
    signature test can pin it the same way it pins ``produced_at`` and
    ``archive_root``.
    """
    return (
        (
            {
                "text": grade.claim.claim_text,
                "span": grade.decisive_span,
                "verdict": grade.verdict,
            },
            grade.decisive_url,
            getattr(page, "text", ""),
            getattr(page, "published_at", None),
            dict(grade.claim.read_evidence_window),
        ),
        {
            "produced_at": grade.claim.produced_at,
            "archive_root": archive_root,
            "grace_before_hours": grace_before_hours,
        },
    )


def check_decisive_span(
    grade: WidthGrade,
    envelope: EvidenceEnvelope,
    *,
    checker: Any | None = None,
    page: Any | None = None,
    fetch_reason: str = "",
    archive_root: Any = None,
    grace_before_hours: float = 0.0,
) -> WidthGrade:
    """Apply G-1/G-2/G-3 to a decisive verdict, in place.

    ``page`` is the decisive URL's fetched page
    (:class:`_external_audit_fetch.FetchedPage`), fetched by the DRAIN through
    the auditor's real ``web_access`` binding and robots-gated there. This
    function is pure and synchronous: it owns the fold of W-3's contract onto
    the grade, never the egress. ``fetch_reason`` is the drain's named reason
    when there is no page.

    THREE WAYS A DECISIVE VERDICT DEGRADES HERE, and they are different
    statements that must not share a shape:

    * no checker — W-3's module is absent (``span_check_unavailable``);
    * no page — the fetch leg refused or failed, and it says which
      (``robots_disallowed`` / ``span_fetch_failed``);
    * the checker ran and DEMOTED — that is not a degrade at all. It is a
      verdict, and :func:`apply_span_check` writes W-3's reasons onto the row.
    """
    if not grade.decisive:
        return grade
    if checker is None:
        checker = _load_span_check()
    if checker is None:
        grade.verdict = VERDICT_UNCHECKED
        grade.unchecked_reason = UNCHECKED_SPAN_CHECK_UNAVAILABLE
        grade.rationale = (
            f"{grade.rationale} [undecided: the verbatim span check is not "
            "available, so a decisive verdict cannot be published on the "
            "grader's assertion alone]"
        ).strip()
        return grade
    if page is None:
        # The page was never read, so G-2 has nothing to compare. The DRAIN owns
        # the reason (it knows whether robots.txt refused or the fetch failed);
        # this branch only refuses to publish a verdict on an unread page.
        grade.verdict = VERDICT_UNCHECKED
        grade.unchecked_reason = fetch_reason or UNCHECKED_SPAN_FETCH_FAILED
        grade.rationale = (
            f"{grade.rationale} [undecided: the decisive page was not read "
            f"({grade.unchecked_reason}), so the quoted span could not be "
            "checked against it]"
        ).strip()
        return grade
    args, kwargs = span_check_arguments(
        grade, page, archive_root=archive_root,
        grace_before_hours=grace_before_hours,
    )
    try:
        outcome = checker(*args, **kwargs)
    except Exception as exc:
        # check_span's own contract is "never raises". Reaching here means the
        # SEAM moved again — which is exactly the 09-05 defect — so it is
        # logged loudly and degraded honestly rather than published.
        logger.warning("external_audit.span_check_raised err=%s", exc)
        grade.verdict = VERDICT_UNCHECKED
        grade.unchecked_reason = UNCHECKED_SPAN_CHECK_UNAVAILABLE
        return grade
    return apply_span_check(grade, outcome)


def apply_span_check(grade: WidthGrade, outcome: Any) -> WidthGrade:
    """Fold W-3's :class:`SpanCheck` onto the grade. Pure, so it tests alone.

    THE GATES ARE NOT RE-IMPLEMENTED HERE. They live in ``check_span``, which
    evaluates all of them (never short-circuiting, so ``demotions`` is the
    COMPLETE list) and returns the final verdict. This function's whole job is
    to carry that verdict and its reasons onto the ledger row's shape.

    The previous version of this function guessed at the outcome's spelling —
    ``resolved`` / ``span_sha256`` / ``out_of_window`` read shape-tolerantly
    against a type that actually spells them ``span_resolved`` /
    ``decisive_span_sha256`` / ``in_window`` — and then RE-DERIVED the tier and
    window gates from those guesses. Two implementations of one contract is how
    C4 case #10 happened ("the string ``tier`` appears nowhere in the scorer"),
    so there is now one: W-3's.

    A DEMOTION KEEPS ITS EVIDENCE. ``decisive_url`` and ``decisive_span`` are
    NOT cleared on demotion (``SpanCheck.as_dict`` keeps them too): a row that
    says NOT_FOUND and has thrown away the URL it demoted is indistinguishable a
    month later from a row the grader never sourced, which is the exact
    ambiguity the C4 audit had to resolve by hand across 27 atoms.
    """
    tier = getattr(outcome, "source_tier", None)
    grade.decisive_source_tier = tier if isinstance(tier, int) else None
    grade.decisive_span_sha256 = str(
        getattr(outcome, "decisive_span_sha256", "") or ""
    )[:64]
    grade.archive_ref = str(getattr(outcome, "archive_ref", "") or "")[:512]
    published = getattr(outcome, "decisive_published_at", None)
    if published:
        grade.decisive_published_at = str(published)[:64]

    grade.verdict = str(getattr(outcome, "verdict", "") or grade.verdict)
    reason = getattr(outcome, "unchecked_reason", None)
    if reason:
        grade.unchecked_reason = str(reason)

    demotions = tuple(getattr(outcome, "demotions", ()) or ())
    rationale = str(getattr(outcome, "rationale", "") or "")
    if demotions:
        # WHY, in the row. W-3 already wrote the prose; the machine-readable
        # list rides beside it so a stratum can be counted without parsing
        # English, and neither is reconstructible from the verdict alone.
        grade.rationale = (
            f"{grade.rationale} [demoted: {', '.join(demotions)}] {rationale}"
        ).strip()
    elif rationale:
        grade.rationale = f"{grade.rationale} [{rationale}]".strip()
    return grade


# ---------------------------------------------------------------------------
# The double-grade routing (design §2.4)
# ---------------------------------------------------------------------------


def should_double_grade(
    grade: WidthGrade, *, fraction: float = DOUBLE_GRADE_FRACTION
) -> bool:
    """10% hash-gated of the SEARCHED claims, plus 100% of CONTRADICTED.

    The CONTRADICTED half is not a sampling decision, it is a publication one: a
    page must never rest on one grader's reading of one span on one page. The
    hash-gated half is what makes the weekly overlap a real statistic rather
    than a selected one — content-independent, replayable, the same discipline
    ``judge_sample_unit()`` uses.
    """
    if grade.verdict == VERDICT_CONTRADICTED:
        return True
    if grade.verdict not in (VERDICT_SUPPORTED, VERDICT_NOT_FOUND):
        return False
    return hash_gate(grade.claim.key) < fraction


def overlap_band(raw_agreement: float | None) -> str:
    """The pre-registered band for a raw agreement (PREREG §4).

    ``stands`` ≥ 0.80 · ``contingent`` 0.75–0.80 · ``instrument_limited`` < 0.75.
    ``unmeasured`` for a ``None`` — never a number, because R3's whole lesson is
    that an overlap you did not measure is not an overlap of 1.0.
    """
    if raw_agreement is None:
        return "unmeasured"
    if raw_agreement >= OVERLAP_BAND_STANDS:
        return "stands"
    if raw_agreement >= OVERLAP_BAND_CONTINGENT:
        return "contingent"
    return "instrument_limited"


def raw_agreement(pairs: Sequence[tuple[str, str]]) -> float | None:
    """Raw agreement over the DECISIVE three-verdict vocabulary.

    The same statistic R1/R2/R3 published (0.774 / 0.804 / 0.7451), so the
    standing number is directly comparable to the rounds. ``None`` over zero
    shared items — never 0.0.
    """
    scored = [
        (a, b) for a, b in pairs
        if a in (VERDICT_SUPPORTED, VERDICT_CONTRADICTED, VERDICT_NOT_FOUND)
        and b in (VERDICT_SUPPORTED, VERDICT_CONTRADICTED, VERDICT_NOT_FOUND)
    ]
    if not scored:
        return None
    return sum(1 for a, b in scored if a == b) / len(scored)


__all__ = [
    "AUDIT_RATER_DEPS_EXTRA_KEY",
    "DOUBLE_GRADE_FRACTION",
    "GRADER_DEPS_EXTRA_KEY",
    "GRADER_SYSTEM",
    "OVERLAP_BAND_CONTINGENT",
    "OVERLAP_BAND_STANDS",
    "RATER_AUDIT",
    "RATER_PRIMARY",
    "RUBRIC_VERSION",
    "EvidenceEnvelope",
    "WidthGrade",
    "absence_unverified_grade",
    "apply_span_check",
    "span_check_arguments",
    "check_decisive_span",
    "configured_model_id",
    "grade_claim",
    "grader_prompt",
    "overlap_band",
    "parse_grader_reply",
    "raw_agreement",
    "should_double_grade",
    "uncheckable_grade",
]
