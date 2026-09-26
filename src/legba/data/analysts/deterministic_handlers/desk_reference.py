# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``desk_reference`` sub-handler — THE OUT-OF-PLANE DAILY DESK REFERENCE (A-1).

THE GAP THIS CLOSES. Under the quotation regime the desk sentence IS the
product, and nothing anywhere in the stack asks whether a story the desk's OWN
slice carried reached the desk's OWN output. Measured on 2026-09-05 at the grain
this handler writes: ``country_watch_il``'s eight unit desks each carried 9 of
120 Iran-naming rows in their own slice; ZERO of the eight named Iran in prose,
zero cited an Iran signal, and zero open frames licensed it. The internal
coverage-floor detector cannot see this — it goes quiet exactly when our own
collection thins (``country_g20_tr|israel`` resolved to ``breached:false`` on
09-04 while TR's desks still named Israel in 0 of 7 heads).

So the reference has to come from OUTSIDE. Daily, per (target x bounded unit),
this handler runs ONE code-built web search, hands a model that has never seen
our substrate the unit's own bounded question plus those results and nothing
else, and stores the answer as a dated, URL-bearing row on
``unit_reference_labels`` (migration 0057's purpose-built table; 0191 adds the
window/items/status/provenance columns).

WHY ``deterministic`` AND NOT ``llm_planner`` — verbatim the standing auditor's
own ruling: *"the SAMPLE, the CAPS, the ROTATION, the VERDICT VOCABULARY and the
WRITES are code; the model is a bounded instrument"*. It also routes around
``dapr_actors.py`` entirely: a sub-handler plus a descriptor, exactly as
``standing_auditor`` and ``coverage_floor`` did.

THE FIVE THINGS THAT ARE CODE, NOT PROMPT
-----------------------------------------
1. **The query.** ``f"{subject} {UNIT_QUERY_TERMS[unit]} {date}"`` — a fixed,
   auditable, hand-written per-unit term table in this module, the same doctrine
   as ``collection_gap.SOURCE_CLASSES_BY_DIMENSION`` (*"a fixed, auditable table
   — NEVER model-generated"*). The subject resolves through
   ``_polity_match.home_country_name`` over the target's geo, because the
   gazetteer alone is wrong on the wire: ``GB`` returns "United Kingdom" and the
   reporting says "Britain"; ``TR`` returns "Türkiye" and it says "Turkey".
2. **The search.** One ``web_search`` per pair through the REAL
   ``AgencyToolBinding`` — SSRF guard, governor budget, invocation ledger. There
   is no ad-hoc HTTP in this module and no ``httpx`` import.
3. **The prompt.** :func:`build_reference_prompt` is a PURE function of the
   bounded question, the subject, the window and the search results. **No
   substrate row of any kind may enter it.** Pinned by
   :func:`assert_prompt_builder_is_substrate_free` and its test — an AST walk
   over the function that fails if it so much as names ``deps``, ``pg_pool``,
   ``conn`` or ``signals``. If our own feeds seeded the reference, the
   collection diff would be circular by construction.
4. **The URL fence.** :func:`parse_items_reply` rejects any item whose ``urls``
   are not a subset of the result set's URLs. A rejected item is DROPPED and
   COUNTED (``items_url_fenced``), never repaired. This is the zero-FP arm and
   it is code, not prompt.
5. **The liveness gate.** ``web_tools``' contract is READ, not ignored.
   :func:`classify_search_outcome` maps all five documented outcomes onto the
   row's ``status``, and only ``ok`` / ``nothing_material`` are scorable. A
   degraded or unverified day writes ``None`` for both metrics — never ``0.0``,
   never a mean-poisoning zero. The 08-12 judge outage moved fleet faithfulness
   0.898 -> 0.583 for three days and nothing alerted; a search outage must never
   read as a quiet world.

WHICH MODEL, AND WHY IT IS A DESCRIPTOR FIELD (design F-1, ORCHESTRATOR RULING)
------------------------------------------------------------------------------
The reference is worthless in-plane: a ``gpt-oss-120b`` reference against
``gpt-oss-120b`` desks measures the family's shared blind spots and calls them
agreement. It must also not share a family with the JUDGE, or the instrument
that measures attention and the instrument that measures faithfulness inherit
each other's blindness — a weaker version of the disease this campaign treats.

So the shipped descriptor binds a THIRD family: ``method.llm.primary ->
llm.judge.cerebras_gemma4_31b.openai_compat`` (Gemma-4-31B on Cerebras, already
a registered active component). It is NOT nemotron (that is the judge) and NOT
``llm.primary`` (that is the writer). Because it is a DESCRIPTOR field resolved
by the shared ``_wire_deterministic_llm``, an operator can re-point it — to a
self-hosted family on ai1, say — with a ``PUT`` and no code edit, no image
rebuild. The Anthropic hard-refuse in that helper still applies.

WHAT THIS INSTRUMENT CANNOT CLAIM (design §2.6, stated before any number)
------------------------------------------------------------------------
It cannot see past the reference model's own blindness: one search engine's
first page is not the world, so **the gap is a lower bound, never an upper
bound**. It is one provider deep. It cannot say the desk was WRONG — attention
is not accuracy. It cannot attribute a gap to a layer. Salience is the reference
model's, not the world's.

FLAG. ``LEGBA_DESK_REFERENCE_ENABLED``, default OFF. Off, the handler returns a
receipt saying so and writes NOTHING: no row, no search, no LLM call, no
watermark. Total byte-identity with the fleet as it stands.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from . import _reference_gap_dispatch

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "desk_reference"

#: THIS INSTRUMENT'S OWN population stamp. Deliberately NOT
#: ``JUDGE_PIPELINE_VERSION`` and not ``EXTERNAL_AUDIT_PIPELINE_VERSION``: a
#: reference is different evidence about a different question, and a mean across
#: two of them describes a population that never existed (the standing auditor's
#: own precedent). Bump this — never those — when the query table, the system
#: prompt, the URL fence or the liveness ladder change. R-1's pre-registered
#: delta refuses to compute across a stamp boundary for exactly this reason.
#:
#: ``/2`` AND NOT ``/1``, RESOLVING A REAL COLLISION rather than dodging a test.
#: This instrument and the JUDGE both took a stamp on 2026-09-05 — the judge's
#: is the verify-regime fix — and the ``<date>/<n>`` form makes same-day trains
#: collide by construction. ``test_the_pipeline_stamp_is_its_own_and_pools_with
#: _nothing`` caught it, and it is not a formality: this handler writes a BARE
#: ``pipeline_version`` into its provenance block (:1247, :1290), so an identical
#: string sitting beside a critique's ``judge_pipeline_version`` is exactly the
#: cross-instrument pool the constant above exists to prevent. THIS instrument
#: moved rather than the judge's for two reasons, both checkable: it is ``draft``
#: with ZERO live rows (``unit_reference_labels`` 0, ``analyst_outputs`` 0 on
#: 2026-09-05), so the bump partitions nothing; and the judge stamp is the frozen
#: R4 T0 baseline, which is named in the prereg and cannot move for a
#: coincidence of dates. ``n`` is a within-day ordinal, not a revision count —
#: this is still this instrument's FIRST stamp.
REFERENCE_PIPELINE_VERSION = "2026-09-05/2"

#: THE F-2 GATE, declared in ONE place and read by both label consumers.
#:
#: ``unit_correctness_scorer``'s secondary axis (``correctness_vs_reference``)
#: has reported ``None`` every day of its life because ``unit_reference_labels``
#: held one row, for a retired analyst, with zero ``canonical_source_ids``. The
#: moment this handler writes rows with real ones, that axis starts reporting a
#: number — a MACHINE-authored number under the key built for OPERATOR labels.
#: That is the disease arriving by side effect. Both readers
#: (``unit_correctness_scorer._LABELS_SQL`` and
#: ``correctness_axis.UNIT_LABELS_SQL``) exclude this prefix, and
#: ``test_desk_reference.py`` proves the scorer's output is byte-identical
#: before and after these rows exist.
REFERENCE_LABELED_BY_PREFIX = "desk_reference/"

#: The exact ``labeled_by`` value every machine reference row carries.
REFERENCE_LABELED_BY = f"{REFERENCE_LABELED_BY_PREFIX}{REFERENCE_PIPELINE_VERSION}"

#: ``deps.extras`` keys the builder populates (see
#: ``analyst_deps_builder._build_deterministic``).
LLM_DEPS_EXTRA_KEY = "desk_reference_llm"
WEB_BINDING_DEPS_EXTRA_KEY = "desk_reference_web_binding"

#: Kill switch. Default OFF — off, the analyst does not run.
ENABLED_ENV = "LEGBA_DESK_REFERENCE_ENABLED"

#: Durable state rides the EXISTING ``alert_trigger_watermarks`` table
#: (migration 0091) under this handler's own ``trigger_class`` — the
#: ``claim_watch`` / ``standing_auditor`` precedent. No new table.
WATERMARK_CLASS = "desk_reference"
#: The rotation cursor's key, and the heartbeat's. Neither contains '|', so
#: neither can ever collide with a per-pair key.
ROTATION_CURSOR_KEY = "_rotation"
HEARTBEAT_KEY = "_heartbeat"

# --- row status vocabulary (migration 0191's `status` column) ---------------
#: The search served results and admitted no degradation.
STATUS_OK = "ok"
#: Zero results, and the control probe PROVED the engine set is answering — so
#: the empty is real for this query. An honest "nothing material" reference.
STATUS_NOTHING_MATERIAL = "nothing_material"
#: Partial service, or every hit lost to degradation the provider ADMITTED.
STATUS_DEGRADED = "degraded"
#: Zero results with no admitted degradation and a control probe that could not
#: show the engine set answering — or a block, a timeout, a deferral. UNKNOWN.
STATUS_UNVERIFIED = "unverified_liveness"

#: The only two statuses either diff will score. Everything else DECLINES:
#: ``None``, never ``0.0``, and excluded from every weekly mean.
SCORABLE_STATUSES: frozenset[str] = frozenset({STATUS_OK, STATUS_NOTHING_MATERIAL})

#: Item materiality vocabulary. Code-validated, never free text — A-5's paging
#: bar reads it and a vocabulary that drifts is a bar that drifts.
MATERIALITY_VOCABULARY: tuple[str, ...] = ("high", "medium", "low")
_DEFAULT_MATERIALITY = "medium"


# ---------------------------------------------------------------------------
# THE QUERY TABLE — fixed, auditable, hand-written, NEVER model-generated
# ---------------------------------------------------------------------------
#
# The nine bounded questions (live on the descriptors since the 09-05 PUT) are
# what make this instrument possible: without them the reference could only be
# "what happened in Israel", which no diff can score against a MILITARY POSTURE
# desk. These terms are the search-engine-shaped half of the same question —
# short, keyword-shaped, no boolean syntax — and they are HERE rather than in a
# prompt because a model-chosen query is a query that can quietly stop asking
# about the uncomfortable thing.

UNIT_QUERY_TERMS: dict[str, str] = {
    "escalation": "escalation risk military tension conflict",
    "energy_security": "energy security oil gas electricity supply",
    "economic_coercion": "sanctions tariffs export controls trade restrictions",
    "internal_stability": "political stability protests unrest government crisis",
    "leadership_transition": "leadership succession election cabinet resignation",
    "military_posture": "military deployment forces exercise posture",
    "narrative_coordination": "state media propaganda disinformation narrative",
    "proliferation_watch": "nuclear weapons enrichment missile programme",
    "disruption_status": "shipping traffic disruption closure transit flow",
}

#: The bounded units, in the order the census reports them. A unit missing from
#: :data:`UNIT_QUERY_TERMS` is skipped and REPORTED — never searched with an
#: improvised query.
BOUNDED_UNITS: tuple[str, ...] = tuple(UNIT_QUERY_TERMS)

#: TIER v1 (design D-f / F-3) — the READ SET only: 5 targets x their live units
#: = 38 (target, unit) pairs/day. Fleet total is 238 pairs and daily-for-all is
#: not on the table (238 paid out-of-plane calls and 238 searches a day for an
#: ungraded instrument).
#:
#: A FIXED TABLE, not a tier read. The 27 tiered ``target_descriptors`` do carry
#: ``analyst.cadence.fallback_schedule = "0 * * * *"`` against the read set's
#: ``*/10 * * * *`` — but design F-5 measured that field to be a VESTIGE: the
#: nine bounded units are SHARED analyst descriptors driving their own cadence,
#: and read-set desks produce 13.8 findings/desk/day against tiered desks' 13.7.
#: The tier PUT moved the per-target token budget, not desk cadence. So this
#: design implements its own rotation and does not lean on a mechanism that is
#: not yet wired. Overridable per-descriptor via ``options['reference_targets']``.
DEFAULT_READ_SET: tuple[str, ...] = (
    "country_g20_sa",
    "country_g20_us",
    "country_watch_il",
    "country_watch_ir",
    "country_watch_ua",
)

# --- caps (every one a COST bound; all descriptor-settable, see handler_options)
DEFAULT_MAX_PAIRS_PER_RUN = 40
DEFAULT_MAX_ITEMS_PER_REFERENCE = 5
DEFAULT_SEARCH_LIMIT = 8
DEFAULT_WINDOW_HOURS = 24
DEFAULT_CENSUS_DAYS = 7

#: Per-call bounds (the standing_auditor shape): a wall-clock timeout so a
#: wedged plane cannot hold the run, and an output cap for hosted endpoints.
REFERENCE_MAX_TOKENS = 900
LLM_TIMEOUT_SECONDS = 90.0
SEARCH_TIMEOUT_SECONDS = 45.0

#: Defensive bounds. Hitting one is REPORTED, never silent.
_MAX_CENSUS_ROWS = 4_000
_MAX_SNIPPET_CHARS = 1024
_MAX_HEADLINE_CHARS = 300
_MAX_SENTENCE_CHARS = 800
_MAX_ENTITIES_PER_ITEM = 12
_MAX_URLS_PER_ITEM = 4
_RECEIPT_SAMPLE_CAP = 12


# ---------------------------------------------------------------------------
# The prompt — bounded, single-turn, STRICT JSON back
# ---------------------------------------------------------------------------

REFERENCE_SYSTEM_PROMPT = (
    "You are an independent reference writer. You are given ONE analytical "
    "question about ONE country or corridor, a 24-hour window, and the results "
    "of ONE external web search. Your ONLY job is to say what actually "
    "happened in that window that bears on that question.\n"
    "\n"
    "Write at most five ITEMS, most consequential first. Each item is one "
    "concrete development: an event that happened, a decision that was taken, "
    "a figure that was published, a position someone took. NOT a forecast, NOT "
    "an assessment of intent, NOT a probability, and NOT background.\n"
    "\n"
    "For each item give:\n"
    "  headline   — a short factual noun phrase naming the development.\n"
    "  sentence   — one sentence a reader could check, with the actors, the "
    "place and the date resolved (whoever reads it will not see these "
    "results).\n"
    "  entities   — the proper names the item is ABOUT: countries, groups, "
    "organisations, people. Write them as they appear in the reporting.\n"
    "  urls       — the URLs from the RESULTS BELOW that carry this item.\n"
    "  published_at — the date the reporting carries, ISO-8601, or null.\n"
    "  materiality — high, medium or low.\n"
    "\n"
    "USE ONLY THE URLS LISTED IN THE RESULTS BELOW. An item citing a URL that "
    "is not in that list will be DISCARDED in full — a fabricated source is "
    "the exact failure this reference exists to avoid.\n"
    "\n"
    "IF NOTHING IN THE RESULTS BEARS ON THE QUESTION, RETURN AN EMPTY ITEMS "
    "ARRAY. That is a legitimate answer and a useful one; inventing a "
    "development to fill the quota is not.\n"
    "\n"
    "Respond with STRICT JSON and nothing else — no prose, no code fences:\n"
    '{"items": [{"headline": "...", "sentence": "...", '
    '"entities": ["..."], "urls": ["..."], "published_at": "YYYY-MM-DD", '
    '"materiality": "high|medium|low"}]}'
)


def build_reference_prompt(
    *,
    bounded_question: str,
    subject: str,
    window_start: datetime,
    window_end: datetime,
    results: Sequence[Mapping[str, Any]],
) -> str:
    """THE SUBSTRATE FENCE — a PURE function of its five arguments.

    Zero substrate rows may enter the reference prompt. If our own feeds seeded
    the reference, the collection diff would be circular by construction: the
    instrument would be asking whether we collected what we told it about.

    This is enforced structurally, not by convention:
    :func:`assert_prompt_builder_is_substrate_free` walks this function's AST
    and fails if it names ``deps``, ``pg_pool``, ``conn``, ``signals`` or any
    other substrate accessor, and pins its parameter list so a later edit cannot
    quietly thread a slice in through a new keyword. The D-6 fence idiom.
    """
    lines = [
        f"QUESTION: {bounded_question}",
        f"SUBJECT: {subject}",
        (
            f"WINDOW: {window_start.isoformat()} to {window_end.isoformat()} "
            "(UTC)"
        ),
        "",
        "SEARCH RESULTS:",
    ]
    if not results:
        lines.append("(none)")
    for i, r in enumerate(results, start=1):
        lines.append(f"[{i}] {r.get('title') or '(untitled)'}")
        lines.append(f"    url: {r.get('url') or ''}")
        published = str(r.get("published_at") or r.get("published") or "").strip()
        if published:
            lines.append(f"    published: {published}")
        snippet = str(r.get("snippet") or r.get("content") or "").strip()
        snippet = snippet.replace("\n", " ")
        if snippet:
            lines.append(f"    text: {snippet[:_MAX_SNIPPET_CHARS]}")
    return "\n".join(lines)


#: Names the prompt builder may not so much as mention. Not a blocklist of
#: imports — a blocklist of NAMES, because the fence has to survive someone
#: threading a row in through a local alias.
_SUBSTRATE_NAMES: frozenset[str] = frozenset({
    "deps", "pg_pool", "pool", "conn", "connection", "acquire", "fetch",
    "fetchrow", "fetchval", "execute", "signals", "analyst_outputs",
    "analyst_traces", "situations", "substrate", "cursor",
})

#: The prompt builder's EXACT parameter list. A new keyword is how a slice gets
#: in without touching a forbidden name, so the signature is pinned too.
PROMPT_BUILDER_PARAMS: tuple[str, ...] = (
    "bounded_question", "subject", "window_start", "window_end", "results",
)


def assert_prompt_builder_is_substrate_free() -> None:
    """AST + param guard over :func:`build_reference_prompt`. Raises on breach.

    Called by the handler on every run (it is microseconds over one small
    function) so the fence fails in PRODUCTION and not only under pytest — a
    guard that only runs in CI is a guard a hotfix can route around.
    """
    source = inspect.getsource(build_reference_prompt)
    tree = ast.parse(source.lstrip())
    func = tree.body[0]
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise RuntimeError("build_reference_prompt is not a plain function")

    args = func.args
    params = tuple(
        a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
    )
    if params != PROMPT_BUILDER_PARAMS:
        raise RuntimeError(
            "build_reference_prompt's signature changed to "
            f"{params!r} (expected {PROMPT_BUILDER_PARAMS!r}) — a new parameter "
            "is how a substrate row gets into the reference prompt without "
            "naming a forbidden accessor. The reference must never see our own "
            "feeds or the collection diff is circular by construction."
        )
    if args.vararg is not None or args.kwarg is not None:
        raise RuntimeError(
            "build_reference_prompt grew *args/**kwargs — the fence cannot see "
            "through them"
        )

    named: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Name):
            named.add(node.id)
        elif isinstance(node, ast.Attribute):
            named.add(node.attr)
    breach = sorted(named & _SUBSTRATE_NAMES)
    if breach:
        raise RuntimeError(
            f"build_reference_prompt names substrate accessor(s) {breach} — "
            "ZERO substrate rows may enter the reference prompt (design D-d). "
            "If our own feeds seed the reference, the collection diff measures "
            "itself."
        )


# ---------------------------------------------------------------------------
# The query — deterministic, replayable from the row's own provenance
# ---------------------------------------------------------------------------


def build_query(subject: str, unit: str, day: date) -> str:
    """``"<subject> <unit terms> <YYYY-MM-DD>"`` — and nothing model-shaped.

    Replayable: the row stores this string in ``provenance.query``, so an
    operator can re-run the exact search that produced any reference. A unit
    with no entry in :data:`UNIT_QUERY_TERMS` returns ``""`` and is SKIPPED
    rather than searched with an improvised query.
    """
    terms = UNIT_QUERY_TERMS.get(unit)
    if not terms or not subject:
        return ""
    return f"{subject} {terms} {day.isoformat()}"


def utc_day_window(now: datetime, *, window_hours: int) -> tuple[datetime, datetime]:
    """The 24h the reference covers: the UTC day the run falls in, back-dated.

    ``window_start`` is the day boundary rather than ``now - 24h`` so the value
    is a stable per-day key — it is the third column of migration 0191's unique
    index, which is what makes "one reference per (target, unit, UTC day)" a
    schema fact instead of a handler convention.
    """
    end = now.astimezone(timezone.utc)
    start = datetime.combine(
        end.date(), time(0, 0), tzinfo=timezone.utc
    ) - timedelta(hours=max(0, window_hours - 24))
    return start, end


# ---------------------------------------------------------------------------
# The URL fence + reply parsing
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _canonical_url(raw: Any) -> str:
    """A URL reduced to what an equality test should compare.

    Trailing slash and surrounding whitespace only — deliberately NOT a full
    normalisation. The fence is an EQUALITY check against a set we handed the
    model seconds earlier; anything cleverer starts admitting URLs the model
    edited, which is the failure the fence exists to catch.
    """
    return str(raw or "").strip().rstrip("/")


def _clean_materiality(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in MATERIALITY_VOCABULARY else _DEFAULT_MATERIALITY


def parse_items_reply(
    raw: str,
    *,
    allowed_urls: Sequence[str],
    cap: int,
) -> tuple[list[dict[str, Any]], int, str]:
    """Parse the model's reply into fenced items.

    Returns ``(items, n_url_fenced, parse_error)``.

    **THE URL FENCE.** An item is admitted only when every URL it carries is in
    ``allowed_urls`` — the exact result set this handler passed the model. An
    item with an off-result URL is DROPPED WHOLE and counted in
    ``n_url_fenced``; it is never repaired by stripping the bad URL, because an
    item whose evidence we cannot locate is not evidence. An item with NO urls
    at all is dropped the same way: the reference is a URL-bearing artefact by
    construction (design D-e), and an unsourced item cannot be re-checked.

    A reply that will not parse yields ``([], 0, reason)`` — the run degrades to
    an empty reference with the reason on the row, never to a guess.
    """
    allowed = {_canonical_url(u) for u in allowed_urls if _canonical_url(u)}
    text = str(raw or "").strip()
    if not text:
        return [], 0, "empty reply"
    payload: Any = None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        match = _JSON_BLOCK.search(text)
        if match is not None:
            try:
                payload = json.loads(match.group(0))
            except (TypeError, ValueError):
                payload = None
    if not isinstance(payload, Mapping):
        return [], 0, "reply was not a JSON object"
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return [], 0, "reply carried no items array"

    items: list[dict[str, Any]] = []
    fenced = 0
    for entry in raw_items:
        if len(items) >= cap:
            break
        if not isinstance(entry, Mapping):
            continue
        urls = [
            _canonical_url(u)
            for u in (entry.get("urls") or [])
            if _canonical_url(u)
        ][:_MAX_URLS_PER_ITEM]
        if not urls or not set(urls) <= allowed:
            # DROPPED and COUNTED, never repaired.
            fenced += 1
            continue
        headline = str(entry.get("headline") or "").strip()[:_MAX_HEADLINE_CHARS]
        sentence = str(entry.get("sentence") or "").strip()[:_MAX_SENTENCE_CHARS]
        if not headline and not sentence:
            fenced += 1
            continue
        entities = [
            str(e).strip()
            for e in (entry.get("entities") or [])
            if str(e or "").strip()
        ][:_MAX_ENTITIES_PER_ITEM]
        items.append({
            "ordinal": len(items) + 1,
            "headline": headline,
            "sentence": sentence,
            "entities": entities,
            "entity_folds": [],  # stamped by the caller through the shared canon
            "urls": urls,
            "published_at": (
                str(entry.get("published_at")).strip()[:64]
                if entry.get("published_at") else None
            ),
            "materiality": _clean_materiality(entry.get("materiality")),
        })
    return items, fenced, ""


# ---------------------------------------------------------------------------
# The liveness ladder — all five documented ``web_tools`` outcomes
# ---------------------------------------------------------------------------


def classify_search_outcome(
    *,
    admitted: bool,
    block_cause: str | None,
    tool_status: str,
    error: str,
    output: Mapping[str, Any] | None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Map a ``web_search`` outcome onto a row ``status`` + its results.

    ``web_tools``' module docstring documents FIVE outcomes and this function
    is the whole of our reading of them. The mapping, and why each lands where
    it does:

    ==================================================  ======================
    ``web_search`` outcome                              row ``status``
    ==================================================  ======================
    completed, ``degraded=false``, ``count>0``          ``ok``
    completed, ``count=0``, ``supports_absence_claim``  ``nothing_material``
    completed, ``degraded=true``, ``count>0``           ``degraded``
    failed, ``search_degraded_no_results``              ``degraded``
    failed, ``search_liveness_unverified`` / deferral   ``unverified_liveness``
    ==================================================  ======================

    The third row is the one that is easy to get wrong: a partially-served
    search DOES carry usable hits, but ``supports_absence_claim=false`` — the
    missing engines could have carried exactly the story the desk skipped. An
    attention metric computed over a reference that may be missing its own most
    important item is worse than no metric, so a degraded day DECLINES.

    A blocked call, a timeout, an exception and a completed-but-empty result
    with NO absence licence all land on ``unverified_liveness``: the search did
    not happen, which is a different fact from "the world was quiet", and the
    only honest thing to do with it is decline.
    """
    out = dict(output or {})
    status_bits = {
        k: out.get(k)
        for k in (
            "status", "degraded", "degraded_detail", "unresponsive_engines",
            "liveness", "liveness_detail", "supports_absence_claim",
            "absence_statement", "absence_warning", "provider",
            "provider_route", "provider_route_class", "count", "deferral",
        )
        if k in out
    }
    if not admitted:
        status_bits["gate_block_cause"] = block_cause or "blocked"
        return STATUS_UNVERIFIED, [], status_bits
    if tool_status != "completed":
        reason = str(error or "")
        row_status = (
            STATUS_DEGRADED
            if "search_degraded_no_results" in reason
            else STATUS_UNVERIFIED
        )
        status_bits["error"] = reason[:500]
        return row_status, [], status_bits

    results = [
        dict(r) for r in (out.get("results") or []) if isinstance(r, Mapping)
    ]
    if out.get("degraded"):
        # Partial service. Usable hits, but the absence claim is void and the
        # reference may be missing its own most important item.
        return STATUS_DEGRADED, results, status_bits
    if not results:
        # A liveness-VERIFIED empty is an honest "nothing material"; anything
        # else that reaches here without an absence licence is UNKNOWN.
        if out.get("supports_absence_claim"):
            return STATUS_NOTHING_MATERIAL, [], status_bits
        return STATUS_UNVERIFIED, [], status_bits
    return STATUS_OK, results, status_bits


# ---------------------------------------------------------------------------
# The rotation — a deterministic cursor, so a restart never re-runs yesterday
# ---------------------------------------------------------------------------


def rotate_pairs(
    pairs: Sequence[tuple[str, str]], *, cursor: int, take: int
) -> tuple[list[tuple[str, str]], int]:
    """Take ``take`` pairs starting at ``cursor``, wrapping. Pure.

    The ``rotate_desks`` idiom, with the cursor DURABLE (in this class's own
    watermark namespace) rather than date-seeded: at v1 the read set is 38 pairs
    against a 40-pair cap so one run covers all of them and the cursor barely
    moves, but at v2 (238 pairs over a 7-day rotation) the cursor is what stops
    a restart re-running yesterday's slice and starving the tail. Pairs are
    pre-sorted by the caller, so the traversal is replayable.
    """
    n = len(pairs)
    if n == 0 or take <= 0:
        return [], cursor
    start = cursor % n
    if take >= n:
        return list(pairs), 0
    taken = [pairs[(start + i) % n] for i in range(take)]
    return taken, (start + take) % n


# ---------------------------------------------------------------------------
# Reads — the population census, the descriptors, today's already-written rows
# ---------------------------------------------------------------------------

#: WHICH units actually produce for which targets. A live census rather than a
#: hand-kept matrix: ``proliferation_watch`` runs on 8 targets and
#: ``disruption_status`` on 6 flow/lane ones, and a hard-coded list would go
#: stale the first time a desk is added. Bounded and reported.
_PAIR_CENSUS_SQL = """
SELECT DISTINCT ao.target_id, ao.analyst_id
  FROM analyst_outputs ao
 WHERE ao.kind = 'finding'
   AND ao.analyst_id = ANY($1::text[])
   AND ao.target_id = ANY($2::text[])
   AND ao.produced_at > now() - make_interval(days => $3)
 ORDER BY ao.target_id, ao.analyst_id
 LIMIT $4
"""

#: The target's geo + display name, for the query SUBJECT. Nothing else about
#: the target reaches the prompt.
_TARGETS_SQL = """
SELECT td.descriptor_id AS target_id,
       td.body -> 'identity' ->> 'name' AS name,
       ARRAY(SELECT jsonb_array_elements_text(td.body -> 'scope' -> 'geo')) AS geo
  FROM target_descriptors td
 WHERE td.is_head = TRUE
   AND td.descriptor_id = ANY($1::text[])
"""

#: Each unit's OWN bounded question, off its live analyst descriptor. This is
#: what makes the reference scoreable against a *military posture* desk instead
#: of "what happened in Israel".
_BOUNDED_QUESTIONS_SQL = """
SELECT ad.descriptor_id AS analyst_id,
       ad.body -> 'method' ->> 'bounded_question' AS bounded_question
  FROM analyst_descriptors ad
 WHERE ad.is_head = TRUE
   AND ad.descriptor_id = ANY($1::text[])
"""

_ISO_NAMES_SQL = """
SELECT iso2, name FROM iso_countries WHERE iso2 = ANY($1::text[])
"""

#: Already written today. The unique index makes a double-write impossible, but
#: reading first means a re-run SKIPS rather than burning a paid out-of-plane
#: call and a search on a row that will be rejected.
_TODAYS_ROWS_SQL = """
SELECT unit_analyst_id, target_id
  FROM unit_reference_labels
 WHERE window_start = $1
   AND labeled_by LIKE $2
"""

_INSERT_REFERENCE_SQL = """
INSERT INTO unit_reference_labels (
    id, unit_analyst_id, target_id, reference_answer, canonical_source_ids,
    labeled_by, window_start, window_end, items, status, provenance
) VALUES ($1, $2, $3, $4, '{}'::uuid[], $5, $6, $7, $8::jsonb, $9, $10::jsonb)
ON CONFLICT (unit_analyst_id, target_id, window_start)
  WHERE window_start IS NOT NULL
  DO NOTHING
RETURNING id
"""

_HEARTBEAT_SQL = """
INSERT INTO alert_trigger_watermarks (trigger_class, watermark_key, state,
                                      fired_at, updated_at)
VALUES ($1, $2, $3::jsonb, NULL, now())
ON CONFLICT (trigger_class, watermark_key) DO UPDATE
   SET state = EXCLUDED.state,
       updated_at = now()
"""

_READ_WATERMARK_SQL = """
SELECT state FROM alert_trigger_watermarks
 WHERE trigger_class = $1 AND watermark_key = $2
"""


def reference_enabled(options: Mapping[str, Any] | None = None) -> bool:
    """``LEGBA_DESK_REFERENCE_ENABLED`` — default OFF.

    Off, the handler writes NOTHING: no row, no search, no LLM call, no
    watermark, no gauge. The fleet is byte-identical to a tree without this
    train. The flag is read HERE (not only in the deps builder) so an operator
    can disarm the instrument with an env change and a recreate, without
    unregistering the descriptor.
    """
    raw = os.getenv(ENABLED_ENV, "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _pos(raw: Any, default: int) -> int:
    """A positive-int knob, or its in-source default.

    Callers pass ``options.get("<literal>")`` rather than a key, deliberately:
    the X-1 catalog's reachability sweep proves a declared knob is real by
    grepping for the literal read in THIS module.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _str_list(raw: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        values = tuple(str(v).strip() for v in raw if str(v or "").strip())
        if values:
            return values
    return default


def target_subject(
    *, target_id: str, name: str | None, geo: Sequence[str],
    iso_names: Mapping[str, str],
) -> str:
    """The SUBJECT string a query is built around.

    A single-country target resolves through the shared polity matcher's
    newsroom spelling (``home_country_name``), because the gazetteer alone
    misnames two of the fleet's own desks on the wire. A multi-geo target (a
    lane, a flow) has no single country, so it falls back to its own descriptor
    name and then to a de-slugged id — deterministic in every branch, and never
    an improvised guess.
    """
    from ..._polity_match import home_country_name

    codes = [str(g) for g in (geo or []) if isinstance(g, str)]
    if len(codes) == 1:
        resolved = home_country_name(codes[0], iso_names.get(codes[0], ""))
        if resolved:
            return resolved
    if name:
        return str(name)
    return str(target_id).replace("_", " ").strip()


async def _load_population(
    conn: Any, *, targets: Sequence[str], units: Sequence[str], census_days: int
) -> tuple[
    list[tuple[str, str]], dict[str, dict[str, Any]], dict[str, str], list[str]
]:
    """The (target, unit) pairs, the target subjects, the bounded questions.

    Returns ``(pairs, targets_by_id, questions_by_unit, warnings)``. A unit with
    no live bounded question is DROPPED and named in ``warnings``: without the
    question the reference could only be "what happened in Israel", which no
    diff can score against a military-posture desk.
    """
    warnings: list[str] = []
    target_rows = await conn.fetch(_TARGETS_SQL, list(targets))
    iso_codes = sorted({
        str(g)
        for r in target_rows
        for g in (r["geo"] or [])
        if isinstance(g, str)
    })
    iso_rows = await conn.fetch(_ISO_NAMES_SQL, iso_codes) if iso_codes else []
    iso_names = {str(r["iso2"]): str(r["name"]) for r in iso_rows}
    targets_by_id = {
        str(r["target_id"]): {
            "geo": [str(g) for g in (r["geo"] or []) if isinstance(g, str)],
            "subject": target_subject(
                target_id=str(r["target_id"]),
                name=r["name"],
                geo=r["geo"] or [],
                iso_names=iso_names,
            ),
            "iso_names": iso_names,
        }
        for r in target_rows
    }
    missing_targets = sorted(set(targets) - set(targets_by_id))
    if missing_targets:
        warnings.append(f"unknown target(s): {missing_targets}")

    question_rows = await conn.fetch(_BOUNDED_QUESTIONS_SQL, list(units))
    questions = {
        str(r["analyst_id"]): str(r["bounded_question"] or "").strip()
        for r in question_rows
    }
    questions = {k: v for k, v in questions.items() if v}
    unquestioned = sorted(set(units) - set(questions))
    if unquestioned:
        warnings.append(
            f"unit(s) with no live method.bounded_question, skipped: "
            f"{unquestioned}"
        )

    census = await conn.fetch(
        _PAIR_CENSUS_SQL,
        [u for u in units if u in questions],
        [t for t in targets if t in targets_by_id],
        int(census_days),
        int(_MAX_CENSUS_ROWS),
    )
    pairs = sorted(
        (str(r["target_id"]), str(r["analyst_id"]))
        for r in census
        if str(r["analyst_id"]) in UNIT_QUERY_TERMS
    )
    return pairs, targets_by_id, questions, warnings


# ---------------------------------------------------------------------------
# One pair: search -> prompt -> fence -> row
# ---------------------------------------------------------------------------


async def _run_search(
    binding: Any, query: str, *, limit: int
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """One ``web_search`` through the REAL agency binding, classified.

    Degrades to ``unverified_liveness`` on every failure path — a timeout, a
    gate block, an exception. The search not happening and the world being quiet
    reach the row as DIFFERENT things, which is the entire reason ``web_tools``
    has honesty fields at all.
    """
    try:
        outcome = await asyncio.wait_for(
            binding.run_tool("web_search", {"query": query, "limit": limit}),
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return STATUS_UNVERIFIED, [], {"error": "web_search timed out"}
    except Exception as exc:  # noqa: BLE001 — degrade-not-break
        logger.warning("desk_reference.search_failed err=%s", exc)
        return STATUS_UNVERIFIED, [], {"error": f"web_search raised: {exc}"}

    result = getattr(outcome, "tool_result", None)
    return classify_search_outcome(
        admitted=bool(getattr(outcome, "admitted", False)),
        block_cause=getattr(outcome, "block_cause", None),
        tool_status=str(getattr(result, "status", "") or ""),
        error=str(getattr(result, "error", "") or ""),
        output=dict(getattr(result, "output", None) or {}),
    )


async def _write_reference(
    llm: Any, prompt: str
) -> tuple[str, str, str]:
    """One bounded out-of-plane call. Returns ``(reply, model, error)``.

    Degrades to an empty reply, never raises: a model outage writes a reference
    with zero items and an explicit reason, which both diffs then decline on —
    the honest shape. A raise here would lose the whole sweep's other 37 pairs.
    """
    try:
        response = await asyncio.wait_for(
            llm.chat_complete(
                [{"role": "user", "content": prompt}],
                max_tokens=REFERENCE_MAX_TOKENS,
                temperature=0.0,
                system=REFERENCE_SYSTEM_PROMPT,
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return "", "", "reference call timed out"
    except Exception as exc:  # noqa: BLE001 — degrade-not-break
        logger.warning("desk_reference.llm_failed err=%s", exc)
        return "", "", f"reference call failed: {exc}"
    usage = getattr(response, "usage", None)
    model = (getattr(usage, "model", "") or "").strip()
    return str(getattr(response, "content", "") or ""), model, ""


def build_reference_answer(items: Sequence[Mapping[str, Any]], status: str) -> str:
    """The ``reference_answer`` text column — 0057's own field, used for what it
    says. A human reading the row must be able to see the reference without
    unpacking JSON."""
    if not items:
        return f"(no reference items — status={status})"
    return "\n".join(
        f"{i.get('ordinal')}. {i.get('headline') or ''} — {i.get('sentence') or ''}"
        .strip()
        for i in items
    )


async def _persist_reference(
    conn: Any,
    *,
    target_id: str,
    unit: str,
    items: Sequence[Mapping[str, Any]],
    status: str,
    window_start: datetime,
    window_end: datetime,
    provenance: Mapping[str, Any],
) -> UUID | None:
    """INSERT one reference row. Returns its id, or None if the day already had
    one (the unique index is the fire-once, not a handler convention)."""
    row = await conn.fetchrow(
        _INSERT_REFERENCE_SQL,
        uuid4(),
        unit,
        target_id,
        build_reference_answer(items, status),
        REFERENCE_LABELED_BY,
        window_start,
        window_end,
        json.dumps(list(items)),
        status,
        json.dumps(dict(provenance)),
    )
    return row["id"] if row else None


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def build_heartbeat_state(
    *,
    ran_at: datetime,
    pairs_attempted: int,
    references_written: int,
    status_mix: Mapping[str, int],
    items_written: int,
    items_url_fenced: int,
    degraded_reason: str,
) -> dict[str, Any]:
    """The heartbeat's ``state`` fingerprint — the 08-12 lesson, made a row.

    ``references_written`` counts only rows that actually landed, and
    ``status_mix`` splits them by liveness, so a dead search plane cannot look
    busy: a run of 38 ``unverified_liveness`` rows reads as an outage on the
    heartbeat even though ``analyst_traces.status`` says ``success``.
    """
    scorable = sum(
        n for k, n in status_mix.items() if k in SCORABLE_STATUSES
    )
    return {
        "sub_handler": SUB_HANDLER_NAME,
        "pipeline_version": REFERENCE_PIPELINE_VERSION,
        "ran_at": ran_at.isoformat(),
        "pairs_attempted": pairs_attempted,
        "references_written": references_written,
        "scorable_references": scorable,
        "status_mix": dict(status_mix),
        "items_written": items_written,
        "items_url_fenced": items_url_fenced,
        "degraded": bool(degraded_reason),
        "degraded_reason": degraded_reason,
        "healthy": bool(scorable) and not degraded_reason,
    }


def _build_receipt(
    *,
    state: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    diff: Mapping[str, Any] | None,
    window_start: datetime,
    window_end: datetime,
    tier: str,
    warnings: Sequence[str],
    enabled: bool,
    dispatch: Mapping[str, Any] | None = None,
) -> FindingPayload:
    """The daily gauge row (design §3.1 / D-k) — a counting-not-repairing gauge.

    Every ratio inside ``collection_gauge`` / ``attention_gauge`` is ``None``
    when its denominator is empty. *"A gauge that reports a confident zero for
    'no data' is exactly the failure mode this retires."* This finding NEVER
    alerts and NEVER gates: A-5 (the ``attention_gap`` trigger class) is
    deliberately not built here.

    ``dispatch`` is A-4's receipt block and is the ONE payload key this train
    adds. With ``LEGBA_REFERENCE_GAP_DISPATCH_ENABLED`` off it is
    ``{"enabled": false}`` and every other key, and every byte of every other
    key, is what it was before A-4 existed.
    """
    written = int(state.get("references_written") or 0)
    scorable = int(state.get("scorable_references") or 0)
    if not enabled:
        headline = f"desk reference DISABLED ({ENABLED_ENV} is off)"
    elif written:
        headline = (
            f"wrote {written} reference(s), {scorable} scorable, over "
            f"{state.get('pairs_attempted')} pair(s)"
        )
    else:
        headline = str(state.get("degraded_reason") or "wrote no references")

    body = [
        f"Out-of-plane desk reference — {headline}.",
        f"  window={window_start.isoformat()} .. {window_end.isoformat()} "
        f"tier={tier} pipeline={REFERENCE_PIPELINE_VERSION}",
        f"  statuses={state.get('status_mix')} items={state.get('items_written')} "
        f"url_fenced={state.get('items_url_fenced')}",
    ]
    if state.get("degraded_reason"):
        body.append(f"  DEGRADED: {state.get('degraded_reason')}")
    for w in warnings:
        body.append(f"  WARNING: {w}")
    if diff:
        body.append(
            f"  collection: pairs={len(diff.get('collection_gauge') or {})} "
            f"attention: pairs={len(diff.get('attention_gauge') or {})} "
            f"instrument={(diff.get('instrument') or {}).get('status')}"
        )
    for s in samples[:_RECEIPT_SAMPLE_CAP]:
        body.append(
            f"  - [{s.get('status')}] {s.get('target_id')}|{s.get('unit')}: "
            f"{s.get('n_items')} item(s), fenced {s.get('url_fenced')}"
        )

    data: dict[str, Any] = {
        "sub_handler": SUB_HANDLER_NAME,
        "meta": True,
        "enabled": enabled,
        "reference_window": {
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "pairs": int(state.get("pairs_attempted") or 0),
            "tier": tier,
        },
        "heartbeat": dict(state),
        "warnings": list(warnings),
        "references": [dict(s) for s in samples[:_RECEIPT_SAMPLE_CAP]],
        # NEVER an upper bound. Stated on the row itself so a reader who sees
        # only the gauge sees the caveat with it.
        "caveat": (
            "The gap is a LOWER bound, never an upper one: the reference sees "
            "one search engine's first page, not the world. Attention is not "
            "accuracy — a desk may correctly judge a story irrelevant to its "
            "bounded question. Salience here is the reference model's."
        ),
    }
    data.update(dict(diff or {}))
    # LAST, and after the diff splice, so the A-4 block can never be shadowed
    # by a diff key and the flag-off diff of this payload against its pre-A-4
    # self is exactly one added key.
    data["reference_gap_dispatch"] = dict(
        dispatch if dispatch is not None else _reference_gap_dispatch.empty_payload()
    )
    return FindingPayload(
        title=f"Desk reference — {headline}"[:2048],
        body="\n".join(body)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME, "attention_measurement",
              "severity:low"],
        data=data,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: Any, options: Mapping[str, Any], deps: Any
) -> AnalystMethodResult:
    """One daily out-of-plane reference sweep, then the two diffs and the gauge.

    REFUSES LOUD on a missing ``deps.pg_pool`` (the ``composition_lineage_sweep``
    / ``standing_auditor`` contract: an instrument that cannot read the substrate
    must not emit a clean-looking zero). Every OTHER missing plane DEGRADES —
    no LLM wired, no search binding, no pairs in the census — and says so on a
    heartbeat row, because a crash is invisible to everything except a log.

    ``inputs`` is the generic materialized slice the cadence actor hands every
    META analyst. It is IGNORED, and that is load-bearing: the reference must
    not see our substrate (design D-d).
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "desk_reference requires a live deps.pg_pool — refusing to report "
            "an attention measurement without reading the substrate"
        )

    now = datetime.now(timezone.utc)
    window_hours = _pos(options.get("window_hours"), DEFAULT_WINDOW_HOURS)
    window_start, window_end = utc_day_window(now, window_hours=window_hours)
    targets = _str_list(options.get("reference_targets"), DEFAULT_READ_SET)
    max_pairs = _pos(options.get("max_pairs_per_run"), DEFAULT_MAX_PAIRS_PER_RUN)
    max_items = _pos(
        options.get("max_items_per_reference"), DEFAULT_MAX_ITEMS_PER_REFERENCE
    )
    search_limit = _pos(options.get("search_limit"), DEFAULT_SEARCH_LIMIT)
    census_days = _pos(options.get("census_days"), DEFAULT_CENSUS_DAYS)
    tier = "read_set" if targets == DEFAULT_READ_SET else "custom"

    enabled = reference_enabled(options)
    if not enabled:
        # OFF: no row, no search, no LLM call, no watermark, no gauge. The only
        # artefact is this receipt, which the descriptor's own `state: draft`
        # keeps out of the substrate until an operator activates it.
        state = build_heartbeat_state(
            ran_at=now, pairs_attempted=0, references_written=0,
            status_mix={}, items_written=0, items_url_fenced=0,
            degraded_reason=f"{ENABLED_ENV} is off",
        )
        return AnalystMethodResult(
            finding=_build_receipt(
                state=state, samples=(), diff=None,
                window_start=window_start, window_end=window_end, tier=tier,
                warnings=(), enabled=False,
            ),
            usage={"prompt_tokens": 0, "completion_tokens": 0,
                   "reasoning_tokens": 0},
        )

    # THE SUBSTRATE FENCE, checked in production and not only under pytest.
    assert_prompt_builder_is_substrate_free()

    extras = dict(getattr(deps, "extras", None) or {})
    llm = extras.get(LLM_DEPS_EXTRA_KEY)
    binding = extras.get(WEB_BINDING_DEPS_EXTRA_KEY)
    degraded: list[str] = []
    if llm is None:
        degraded.append(
            "no out-of-plane LLM wired (method.llm.primary unset or refused)"
        )
    if binding is None:
        degraded.append(
            "no web_access binding wired (pack not granted / agency plane down)"
        )

    async with pool.acquire() as conn:
        pairs, targets_by_id, questions, warnings = await _load_population(
            conn, targets=targets, units=BOUNDED_UNITS, census_days=census_days,
        )
        cursor_row = await conn.fetchrow(
            _READ_WATERMARK_SQL, WATERMARK_CLASS, ROTATION_CURSOR_KEY
        )
        already = {
            (str(r["target_id"]), str(r["unit_analyst_id"]))
            for r in await conn.fetch(
                _TODAYS_ROWS_SQL, window_start, f"{REFERENCE_LABELED_BY_PREFIX}%"
            )
        }

    cursor_state = cursor_row["state"] if cursor_row else None
    if isinstance(cursor_state, str):
        try:
            cursor_state = json.loads(cursor_state)
        except (TypeError, ValueError):
            cursor_state = None
    cursor = int((cursor_state or {}).get("cursor") or 0)

    if not pairs:
        degraded.append("no (target, unit) pairs in the census window")
    selected, next_cursor = rotate_pairs(pairs, cursor=cursor, take=max_pairs)
    # A pair already referenced TODAY is skipped before it costs a paid call —
    # the unique index would reject the write anyway, and burning the search to
    # discover that is the kind of waste an ungraded instrument cannot afford.
    selected = [p for p in selected if p not in already]

    samples: list[dict[str, Any]] = []
    status_mix: dict[str, int] = {}
    written = 0
    items_total = 0
    fenced_total = 0

    if llm is not None and binding is not None and selected:
        from ._reference_diff import item_folds

        for target_id, unit in selected:
            target = targets_by_id.get(target_id) or {}
            subject = str(target.get("subject") or "")
            query = build_query(subject, unit, window_end.date())
            if not query:
                warnings.append(
                    f"no query term table entry / subject for {target_id}|{unit}"
                )
                continue
            status, results, search_bits = await _run_search(
                binding, query, limit=search_limit
            )
            items: list[dict[str, Any]] = []
            fenced = 0
            parse_error = ""
            model = ""
            if status in SCORABLE_STATUSES and results:
                prompt = build_reference_prompt(
                    bounded_question=questions.get(unit, ""),
                    subject=subject,
                    window_start=window_start,
                    window_end=window_end,
                    results=results,
                )
                reply, model, llm_error = await _write_reference(llm, prompt)
                if llm_error:
                    parse_error = llm_error
                else:
                    items, fenced, parse_error = parse_items_reply(
                        reply,
                        allowed_urls=[r.get("url") for r in results],
                        cap=max_items,
                    )
                for item in items:
                    item["entity_folds"] = item_folds(
                        item,
                        home_blob=_home_blob_for(target),
                    )
            if status in SCORABLE_STATUSES and not items and not results:
                status = STATUS_NOTHING_MATERIAL

            provenance = {
                "query": query,
                "subject": subject,
                "unit": unit,
                "bounded_question": questions.get(unit, ""),
                "model": model,
                "pipeline_version": REFERENCE_PIPELINE_VERSION,
                "n_results": len(results),
                "items_url_fenced": fenced,
                "parse_error": parse_error,
                "search": search_bits,
            }
            async with pool.acquire() as conn:
                row_id = await _persist_reference(
                    conn,
                    target_id=target_id,
                    unit=unit,
                    items=items,
                    status=status,
                    window_start=window_start,
                    window_end=window_end,
                    provenance=provenance,
                )
            if row_id is not None:
                written += 1
                status_mix[status] = status_mix.get(status, 0) + 1
                items_total += len(items)
                fenced_total += fenced
            samples.append({
                "target_id": target_id,
                "unit": unit,
                "status": status,
                "n_items": len(items),
                "url_fenced": fenced,
                "written": row_id is not None,
                "query": query,
            })

    # ---- the two diffs + the validity harness (A-2 / A-3) ------------------
    diff_payload: dict[str, Any] | None = None
    # A-4's sink. `None` when the dispatch flag is off, which is what makes the
    # off state cost NOTHING: run_reference_diff builds no candidate, runs no
    # extra query, and the payload it returns is unchanged either way.
    dispatch_on = _reference_gap_dispatch.dispatch_enabled(options)
    gap_sink: list[dict[str, Any]] | None = [] if dispatch_on else None
    try:
        from ._reference_diff import run_reference_diff

        async with pool.acquire() as conn:
            diff_payload = await run_reference_diff(
                conn,
                window_start=window_start,
                window_end=window_end,
                labeled_by_prefix=REFERENCE_LABELED_BY_PREFIX,
                pipeline_version=REFERENCE_PIPELINE_VERSION,
                now=now,
                gap_sink=gap_sink,
            )
    except Exception as exc:  # noqa: BLE001 — the reference must survive the diff
        logger.warning("desk_reference.diff_failed err=%s", exc)
        warnings.append(f"reference diff failed: {exc}")

    # ---- A-4: the research dispatch side-write -----------------------------
    # Never raises by contract, and is wrapped anyway: the gauge above is this
    # analyst's durable product and a side-write may not cost the operator a
    # measurement. A failure lands in the receipt block, not in an exception.
    dispatch_payload: dict[str, Any] | None = None
    if dispatch_on:
        try:
            dispatch_payload = (
                await _reference_gap_dispatch.run_reference_gap_dispatch(
                    pool, gap_sink or [], run_id=options.get("run_id"),
                )
            )
        except Exception as exc:  # noqa: BLE001 — belt and braces
            logger.warning("desk_reference.reference_gap_dispatch_failed err=%s", exc)
            dispatch_payload = {
                **_reference_gap_dispatch.empty_payload(enabled=True),
                "failures": [str(exc)],
            }

    state = build_heartbeat_state(
        ran_at=now,
        pairs_attempted=len(selected),
        references_written=written,
        status_mix=status_mix,
        items_written=items_total,
        items_url_fenced=fenced_total,
        degraded_reason="; ".join(degraded),
    )
    async with pool.acquire() as conn:
        await _upsert_state(conn, ROTATION_CURSOR_KEY, {"cursor": next_cursor})
        await _upsert_state(conn, HEARTBEAT_KEY, state)

    if degraded:
        logger.warning(
            "desk_reference.degraded reasons=%r pairs=%d written=%d",
            degraded, len(selected), written,
        )
    else:
        logger.info(
            "desk_reference.ran pairs=%d written=%d statuses=%r fenced=%d",
            len(selected), written, status_mix, fenced_total,
        )

    return AnalystMethodResult(
        finding=_build_receipt(
            state=state, samples=samples, diff=diff_payload,
            window_start=window_start, window_end=window_end, tier=tier,
            warnings=warnings, enabled=True, dispatch=dispatch_payload,
        ),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


def _home_blob_for(target: Mapping[str, Any]) -> str:
    """The desk's own-country prose blob, for the home-country exclusion.

    A reference item that names ONLY the desk's own country is
    ``unanchorable``: every desk's slice is about its own country, so a "did we
    collect it?" question keyed on the home polity answers itself. Excluded from
    BOTH the numerator and the denominator of BOTH metrics.
    """
    from ..._polity_match import home_prose

    geo = [str(g) for g in (target.get("geo") or []) if isinstance(g, str)]
    iso_names = dict(target.get("iso_names") or {})
    return home_prose(geo, [iso_names[g] for g in geo if g in iso_names])


async def _upsert_state(
    conn: Any, key: str, state: Mapping[str, Any]
) -> bool:
    """Upsert one durable state row. Never raises — losing the heartbeat is a
    real degradation but not one worth costing the run its references."""
    try:
        await conn.execute(
            _HEARTBEAT_SQL, WATERMARK_CLASS, key, json.dumps(dict(state))
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "desk_reference.state_write_failed key=%s err=%s — the sweep ran "
            "but did not record that it ran", key, exc,
        )
        return False


__all__ = [
    "BOUNDED_UNITS",
    "DEFAULT_READ_SET",
    "ENABLED_ENV",
    "HEARTBEAT_KEY",
    "LLM_DEPS_EXTRA_KEY",
    "MATERIALITY_VOCABULARY",
    "REFERENCE_LABELED_BY",
    "REFERENCE_LABELED_BY_PREFIX",
    "REFERENCE_PIPELINE_VERSION",
    "REFERENCE_SYSTEM_PROMPT",
    "ROTATION_CURSOR_KEY",
    "SCORABLE_STATUSES",
    "STATUS_DEGRADED",
    "STATUS_NOTHING_MATERIAL",
    "STATUS_OK",
    "STATUS_UNVERIFIED",
    "SUB_HANDLER_NAME",
    "UNIT_QUERY_TERMS",
    "WATERMARK_CLASS",
    "WEB_BINDING_DEPS_EXTRA_KEY",
    "assert_prompt_builder_is_substrate_free",
    "build_heartbeat_state",
    "build_query",
    "build_reference_answer",
    "build_reference_prompt",
    "classify_search_outcome",
    "handle",
    "parse_items_reply",
    "reference_enabled",
    "rotate_pairs",
    "target_subject",
    "utc_day_window",
]
