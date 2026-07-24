# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``signal_summarizer`` sub-handler — async distillation of long signal bodies.

Downstream analysts read a signal's body to synthesize cited prose, but that
body is usually the PUBLISHER's teaser/lede — tuned for a human reader, not for
analytic extraction. This sub-handler writes OUR own analysis-tuned summary into
``signals.payload.distilled_body`` so the downstream synthesis reads a compact,
faithful, quote-preserving brief instead of the raw publisher copy.

It is an ASYNC SWEEP (never inline in ingest — ingestion must stay cheap): every
time the bound ``deterministic`` analyst fires (cadence), it distills the next
throttled batch of un-summarized signals, draining the body-bearing backlog over
~1–2 days.

Per examined signal (mirrors ``entity_resolution``'s stamp-all-examined
idempotency exactly, so the partial index drains and nothing is re-scanned):

  * Pick the body = the LONGEST HTML-cleaned field among ``raw_body`` /
    ``summary`` / ``text`` / ``body`` / ``content`` / ``description``. NOT
    first-non-empty (entity_resolution's order): ``raw_body`` holds the full
    content:encoded / full-text article while ``summary`` is the RSS teaser, so a
    first-non-empty pick would distill the teaser and shadow the richer body.
  * If that body is SUBSTANTIAL (> :data:`MIN_BODY_LEN` chars), call the CORE
    self-hosted LLM plane (``llm.primary.openai_compat`` / gpt-oss-120b — $0, NEVER
    Anthropic) for a hybrid brief + preserved-quotes summary and write it to
    ``payload.distilled_body``.
  * Every EXAMINED row (summarized, skipped-short, or LLM-failed) is stamped
    ``summarized_at = now()`` so short/no-body rows drain out of the partial
    index cheaply and are never re-scanned, and a poison row that fails the LLM
    is not retried forever (it is stamped with a ``payload.summarize_failed``
    marker instead).

Throttle (the CORE gateway is SHARED with the embed path):
  * A per-run cap :data:`_DEFAULT_MAX_SUMMARIES` (``options.max_summaries``) on
    the number of LLM calls per tick.
  * Summaries run SEQUENTIALLY within a batch (no ``asyncio.gather`` fan-out) so
    a tick never bursts the gateway.
  * Short rows stamp cheaply in ONE bulk UPDATE (no LLM call) — they may be a
    large batch and cost nothing.

Degrade-not-break: any LLM error / timeout / empty reply skips summarizing that
one row but STILL stamps ``summarized_at`` (with the failure marker) so the sweep
makes forward progress and never wedges on one bad row.

Output ``data`` keys (the cadence receipt the operator reads):
    summarized      int — rows given an OUR-analysis distilled_body this run
    skipped_short   int — rows drained with no LLM call (short / no body)
    failures        int — rows whose LLM summary failed (stamped + marked)
    skipped_no_llm  int — long rows LEFT UNSTAMPED because no LLM was wired
                          (0 in production; > 0 only signals a mis-wired plane)
    llm_calls       int — LLM calls made this run (= summarized + failures)
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "signal_summarizer"

#: The key under which the runtime stashes the resolved CORE-plane LLM handler on
#: ``StandardDeps.extras`` for this sweep (wired in analyst_deps_builder when the
#: descriptor declares ``method.llm.primary`` — the self-hosted vLLM plane; the
#: deps-builder hard-refuses an Anthropic component).
LLM_DEPS_EXTRA_KEY = "signal_summarizer_llm"

#: How many signals to SELECT per tick (short rows drain cheaply, long rows are
#: bounded separately by the summary cap below). Mirrors entity_resolution.
_DEFAULT_BATCH = 500

#: Per-run cap on LLM SUMMARY calls (the shared CORE gateway throttle). Both a
#: successful summary and a failed call count toward it. ~40/tick over a 15-min
#: cadence drains the ~4.5k body-bearing backlog in ~1–2 days.
_DEFAULT_MAX_SUMMARIES = 40

#: Body-length floor: only distill bodies LONGER than the analyst's render cap
#: (inline_target._MAX_SNIPPET_CHARS = 1500). At/under that the analyst already
#: reads the WHOLE body in the slice, so a summary adds nothing — and summarizing a
#: sub-cap teaser only EXPANDS it (the model pads a short teaser into a longer brief,
#: inventing structure/"indicators"), which is worse than the teaser. Above the cap,
#: distillation genuinely COMPRESSES a long article to fit. Keep this == the render
#: cap so SUMMARY_MAX_CHARS (1200) < MIN_BODY_LEN guarantees the summary is shorter
#: than its source. A short/absent body is stamped-and-skipped (its own text IS what
#: the analyst reads).
MIN_BODY_LEN = 1500

#: Per-call output token cap (a ~1200-char brief fits comfortably under this).
SUMMARY_MAX_TOKENS = 512

#: Per-call wall-clock timeout (seconds). On expiry the row DEGRADES to a stamped
#: failure (never a hang / retry-forever).
SUMMARY_TIMEOUT_SECONDS = 30.0

#: Hard input bound: the prompt only carries the first N chars of the article so
#: a pathologically long body can never overrun the model context / gateway.
#: Sized to capture the vast majority of full articles WHOLE (observed long-form
#: e.g. GlobalVoices runs ~15-20k chars ≈ ~5k tokens) — a lower bound truncated the
#: tail and produced summaries that misrepresented the back half of the article.
#: Overrun still degrades safely (the per-call timeout stamps the row failed →
#: renderer falls back to raw_body).
MAX_BODY_INPUT_CHARS = 20000

#: Hard stored-summary bound (belt to the prompt's "<= ~1200 chars" rule) so a
#: runaway model reply can never bloat payload.distilled_body.
SUMMARY_MAX_CHARS = 1200

#: Candidate body fields. We pick the LONGEST HTML-cleaned one — NOT the first —
#: because raw_body holds the full content:encoded / full-text article while
#: `summary` is the RSS teaser; a first-non-empty pick (entity_resolution's order)
#: would distill the teaser and shadow the richer body downstream. Iterated
#: raw_body-first so an exact length tie (no-content-encoded feeds where
#: raw_body == summary) still prefers raw_body.
_BODY_FIELDS = ("raw_body", "summary", "text", "body", "content", "description")

#: HTML strip so the longest-field pick AND the MAX_BODY_INPUT_CHARS truncation
#: both operate on real prose, not markup (raw_body is raw content:encoded HTML).
_HTML_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WS_RE = re.compile(r"\s+")


def _clean_html(text: str) -> str:
    """Drop script/style, strip tags, unescape entities, collapse whitespace."""
    if not text:
        return ""
    text = _HTML_SCRIPT_STYLE_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _HTML_WS_RE.sub(" ", text).strip()

_SUMMARY_SYSTEM_PROMPT = (
    "You are an intelligence analyst's summarizer. Produce a compact, faithful "
    "analytic summary in ENGLISH of the given article for downstream analysts."
)

#: Hybrid brief + preserved-quotes format. ``{body}`` is the (bounded) article.
_SUMMARY_USER_TEMPLATE = (
    "Summarize the following article for downstream intelligence analysts.\n\n"
    "Produce, in this order:\n"
    "1. A one-sentence BLUF: what happened and why it matters.\n"
    "2. Then 3-6 bullets covering the key actors, the claims (with specific "
    "figures and dates), and the developments. PRESERVE the source's verbatim "
    'wording in "quotes" for the most load-bearing claims so they stay '
    "verifiable.\n"
    "3. Optionally, one final line 'Indicators to watch: ...' ONLY if the "
    "article implies forward-looking signals.\n\n"
    "Rules: keep the whole summary under ~1200 characters; do NOT invent facts "
    "not in the article; attribute every claim to its source or speaker; if the "
    "article is in another language, summarize in English; output ONLY the "
    "summary, with no preamble.\n\n"
    "ARTICLE:\n{body}"
)

# ---------------------------------------------------------------------------
# Atomic per-row writes. Each is ONE UPDATE that stamps summarized_at together
# with whatever payload change it makes (jsonb_set for distilled_body / the
# failure marker), so a row is never left half-written.
# ---------------------------------------------------------------------------

#: Summarized row — write the distilled body + stamp. $2 is json.dumps(summary).
#:
#: CRITICAL — reset ``indexed_at = NULL`` so ``corpus_indexer`` RE-INDEXES this
#: signal (with its new distilled_body → best_body) on its next sweep. The
#: summarizer and the indexer are SEPARATE cadence sweeps: a signal indexed BEFORE
#: it was summarized would otherwise keep a summary-less corpus doc forever (the
#: indexer only scans ``WHERE indexed_at IS NULL``). Nulling the marker re-enters
#: the row into that scan; the OpenSearch ``_id`` (= signal id) makes the re-index
#: an in-place OVERWRITE (never a duplicate). This is the corpus DIRTY-MARKER
#: contract (see corpus_indexer): ANY writer that changes indexable signal content
#: (distilled_body today; translation / enrichment fields later) MUST, in the SAME
#: UPDATE, null indexed_at AND bump updated_at=now() — BOTH below. The updated_at
#: bump is load-bearing: it is what protects this re-null from being clobbered by a
#: concurrent indexer batch's version-guarded stamp (nulling indexed_at alone would
#: reopen the lost-update race). Safe: raw_body is indexed as its own searchable field, so a re-index
#: only ENRICHES the doc (adds our brief + boosted best_body) — it never shrinks
#: recall. Bounded: summarized_at is stamped in the same UPDATE, so the summarizer
#: never re-touches the row → indexed_at is reset AT MOST ONCE per signal (no churn).
_WRITE_SUMMARY_SQL = """
    UPDATE signals
       SET payload = jsonb_set(
               COALESCE(payload, '{}'::jsonb), '{distilled_body}', $2::jsonb, true
           ),
           summarized_at = now(),
           updated_at = now(),
           indexed_at = NULL
     WHERE id = $1
"""

#: LLM-failed row — stamp + a marker so a poison row is not retried forever.
_STAMP_FAILED_SQL = """
    UPDATE signals
       SET payload = jsonb_set(
               COALESCE(payload, '{}'::jsonb), '{summarize_failed}', 'true'::jsonb, true
           ),
           summarized_at = now(),
           updated_at = now()
     WHERE id = $1
"""

#: Short / no-body rows — bulk stamp (no LLM). $1 is a uuid[] of examined ids.
_STAMP_SHORT_BULK_SQL = """
    UPDATE signals
       SET summarized_at = now(), updated_at = now()
     WHERE id = ANY($1::uuid[])
"""

#: NEWEST-first scan of the un-summarized text pool (WHERE matches the partial
#: index idx_signals_unsummarized; btree supports the reverse scan). Newest-first
#: is deliberate: analysts read a ~72h recency window, so fresh long-form must be
#: distilled within ~a tick of ingest to reach the slice — draining oldest-first
#: instead spends the budget on aged-out signals no analyst will ever read while
#: today's signals wait. The stale backlog drains last, harmlessly, in the
#: background (sweep capacity ~2k/hr >> new-signal arrival, so no starvation).
_SELECT_BATCH_SQL = """
    SELECT id, payload
      FROM signals
     WHERE summarized_at IS NULL
       AND modality = 'text'
     ORDER BY fetched_at DESC
     LIMIT $1
"""


def _as_dict(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _pick_body(payload: Mapping[str, Any]) -> str:
    """Longest HTML-cleaned candidate body (raw_body-biased on exact-length ties)."""
    best = ""
    for _k in _BODY_FIELDS:
        _v = payload.get(_k)
        if not (isinstance(_v, str) and _v.strip()):
            continue
        cleaned = _clean_html(_v)
        if len(cleaned) > len(best):
            best = cleaned
    return best


async def _summarize_one(llm: Any, body: str) -> str | None:
    """Bounded, sequential CORE-plane summary of ONE article body.

    Returns the trimmed summary string, or ``None`` to DEGRADE (any timeout /
    exception / empty reply). temperature=0 for reproducibility; output-capped;
    per-call wall-clock timeout; the input body is bounded to
    :data:`MAX_BODY_INPUT_CHARS`. NEVER raises to the caller (degrade-not-break)."""
    prompt = _SUMMARY_USER_TEMPLATE.format(body=body[:MAX_BODY_INPUT_CHARS])
    try:
        response = await asyncio.wait_for(
            llm.chat_complete(
                [{"role": "user", "content": prompt}],
                max_tokens=SUMMARY_MAX_TOKENS,
                temperature=0.0,
                system=_SUMMARY_SYSTEM_PROMPT,
            ),
            timeout=SUMMARY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("signal_summarizer.summary_timeout")
        return None
    except Exception as exc:  # degrade-not-break: any handler failure → skip
        logger.warning("signal_summarizer.summary_failed err=%s", exc)
        return None
    content = getattr(response, "content", "") or ""
    summary = content.strip()
    if not summary:
        return None
    return summary[:SUMMARY_MAX_CHARS]


async def _sweep_batch(
    pool: Any,
    *,
    llm: Any | None,
    batch_limit: int,
    max_summaries: int,
) -> dict[str, int]:
    """Distill the next throttled batch of un-summarized text signals.

    Sequential (no fan-out). The connection is NOT held across the slow LLM
    calls — the batch is SELECTed once, then each write briefly re-acquires a
    pooled connection — so a ~40-summary tick never pins a connection for
    minutes. All writes are idempotent + forward-progressing."""
    counters = {
        "summarized": 0,
        "skipped_short": 0,
        "failures": 0,
        "skipped_no_llm": 0,
        "llm_calls": 0,
    }

    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_BATCH_SQL, batch_limit)

    short_ids: list[Any] = []
    for r in rows:
        payload = _as_dict(r["payload"])
        body = _pick_body(payload)
        if len(body) > MIN_BODY_LEN:
            # Long body — needs an LLM summary.
            if llm is None:
                # No CORE plane wired (mis-config). Leave UNSTAMPED so the row is
                # summarized once the plane is available — never destroy the
                # chance to distill it by stamping it blind.
                counters["skipped_no_llm"] += 1
                continue
            if counters["llm_calls"] >= max_summaries:
                # Per-run gateway budget spent — leave the remaining long rows
                # UNSTAMPED for the next tick.
                break
            counters["llm_calls"] += 1
            summary = await _summarize_one(llm, body)
            async with pool.acquire() as conn:
                if summary is None:
                    await conn.execute(_STAMP_FAILED_SQL, r["id"])
                    counters["failures"] += 1
                else:
                    await conn.execute(_WRITE_SUMMARY_SQL, r["id"], json.dumps(summary))
                    counters["summarized"] += 1
        else:
            # Short / no body — drain cheaply (bulk-stamped below).
            short_ids.append(r["id"])

    if short_ids:
        async with pool.acquire() as conn:
            await conn.execute(_STAMP_SHORT_BULK_SQL, short_ids)
        counters["skipped_short"] = len(short_ids)

    # M2: the mis-wired-plane case is NOT benign — un-summarizable long rows are
    # left UNSTAMPED (so they're distilled once the plane returns), but if the CORE
    # plane stays unwired they accumulate and the partial index grows unbounded.
    # Go LOUD so the operator sees a misconfig instead of a silent stall.
    if counters["skipped_no_llm"]:
        logger.warning(
            "signal_summarizer.no_llm_plane skipped_no_llm=%d — the CORE summarizer "
            "plane (method.llm.primary) did not resolve; long bodies left "
            "un-summarized this tick. Check the descriptor + deps wiring.",
            counters["skipped_no_llm"],
        )

    return counters


def _build_finding(counters: Mapping[str, int]) -> FindingPayload:
    title = (
        f"Signal summarizer: distilled {counters.get('summarized', 0)} body(ies), "
        f"drained {counters.get('skipped_short', 0)} short, "
        f"{counters.get('failures', 0)} failed"
    )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "signal_summarizer"]
    if counters.get("summarized", 0):
        tags.append("summarized")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, **dict(counters)},
    )


def _resolve_summarizer_llm(deps: Any | None) -> Any | None:
    """Pull the CORE-plane summarizer handler off ``deps.extras`` (or ``None``).

    Injected by
    :func:`legba.runtime.analyst_deps_builder._build_deterministic` when the
    descriptor declares ``method.llm.primary`` (the self-hosted
    ``llm.primary.openai_compat`` plane). Absent → the sweep drains short rows
    but leaves long rows for a tick where the plane is wired."""
    if deps is None:
        return None
    extras = getattr(deps, "extras", None)
    if not isinstance(extras, Mapping):
        return None
    return extras.get(LLM_DEPS_EXTRA_KEY)


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Sweeps the substrate directly via ``deps.pg_pool`` (the ``inputs`` slice is
    ignored — the unit of work is "the next batch of un-summarized text
    signals", not a time window), matching ``entity_resolution``. ``deps is
    None`` (unit-test path, no live substrate) yields a zeroed run. Usage is
    always zeroed — the deterministic kind is token-budget-exempt and the CORE
    plane is self-hosted / $0 (mirrors fact_contention_arbiter)."""
    counters: dict[str, int] = {
        "summarized": 0,
        "skipped_short": 0,
        "failures": 0,
        "skipped_no_llm": 0,
        "llm_calls": 0,
    }
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        batch_limit = int(options.get("batch_limit", _DEFAULT_BATCH))
        max_summaries = int(options.get("max_summaries", _DEFAULT_MAX_SUMMARIES))
        llm = _resolve_summarizer_llm(deps)
        try:
            counters = await _sweep_batch(
                pool,
                llm=llm,
                batch_limit=batch_limit,
                max_summaries=max_summaries,
            )
        except Exception as exc:
            logger.warning("signal_summarizer.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME", "LLM_DEPS_EXTRA_KEY"]
