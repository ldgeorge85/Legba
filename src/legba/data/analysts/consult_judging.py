# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A consult turn's cited claims, judged the way a finding is (X2 lane C1).

THE DEFECT (W11-F5, W11-F27). Every finding a desk writes passes the
faithfulness verify — the deterministic floor, then the LLM judge over the
JUDGE-2 failure chain — and carries the verdict on its critique row. A consult
answer, the surface readers actually ask, passed nothing and said nothing: no
score, and no statement that it had none.

WHAT THIS DOES. After the answer is composed, the turn's CITED sentences are
re-shaped into the ``[N]`` + citations-bridge form the verify pass binds on and
sent through :func:`provenance.verify.verify_finding_faithfulness` — the same
floor, the same judge prompt, the same folds — over the same failure chain
(``runtime.judge_fallback_chain``). The bridge is the journal's
(``runtime.actor_critic._resolve_journal_citation_bridge``): a consult cites
findings, signals, situations, facts, nexuses and hypotheses by bare uuid,
which is exactly the set that resolver reads. Nothing about HOW a claim is
graded is new, so ``JUDGE_PIPELINE_VERSION`` is stamped, not bumped.

WHICH CLAIMS. A consult answer carries no ordinals; it names its evidence
inline as ``[<uuid>]`` or ``[<8+ hex prefix>]`` (the census reads the same
signal — ``consult_provenance_census``). Only sentences carrying such a marker
are judged: an unmarked sentence is model knowledge, which the census already
counts and which a judge told to grade "against the cited evidence" has
nothing to grade against. An answer with no cited sentence is UNASSESSABLE —
never scored, and never 0.5 (W11-F29: the fleet's averaging folds unassessable
rows at 0.5; a consult stamp has no number at all in that state).

THE THREE STATES (the turn header prints one of them, never nothing):

* ``judged`` — an adjudicated verdict (``judge_status`` ``llm`` / ``partial``);
  ``score`` is the pooled supported share. A verdict from a WEAK rung (the
  in-house 8B) says so: ``weak_judge: true``.
* ``unassessable`` — nothing a judge could grade; ``score`` is ``None`` and
  ``reason`` names why.
* ``not_judged`` — no judge adjudicated: the flag is off, no judge route, the
  synthesis was cut, the judge timed out, or every rung failed and only the
  deterministic floor ran (the floor is NOT published as a score here — the
  journal path's rule: resolvable-but-unsupporting citations pass a floor).

ROUTE. ``LEGBA_CONSULT_JUDGE_STACK_REF`` names the first rung; default the
in-house ``llm.verify.slm_8b`` ($0, labelled weak). Pointing it at the fleet's
hosted judge is an operator decision with a per-turn cost; the chain tail is
the platform default (``LEGBA_JUDGE_CHAIN_DEFAULT``). The master switch is the
fleet's own ``LEGBA_VERIFY_LLM_JUDGE`` — one flag decides whether anything is
LLM-judged.

Never raises into the run: an answer is never lost to its own verification.

W11-F93 — WHEN, AND WITHIN WHAT (2026-10-15: a capped consult turn printed a
bare "not judged · judge unavailable"). What the code did, read against the
three ways the judge could have been wrong about a capped turn:

1. A PARTIAL answer? No. A turn whose synthesis was cut (``partial`` /
   ``none``) was refused (``synthesis_incomplete``), and the call already
   followed every TEXT edit (the hold banner, the truth line).
2. Citations RENUMBERED by the cap? No. The ``[N]`` ordinals are rebuilt from
   the final text here, and the claims cap rolls back any ordinal an uncounted
   sentence introduced.
3. The per-run cap REFUSING the judge? Not the cap itself: the judge resolves
   its OWN handler through ``judge_resolver`` and ``SpendGuard`` never meters
   it (``LEGBA_CONSULT_MAX_INPUT_TOKENS_PER_RUN`` /
   ``LEGBA_CONSULT_MAX_COST_USD_PER_RUN`` bound drilling, nothing else). What
   the cap DOES do is make the turn the judge then has to read as big as a
   turn gets: a run that drilled to its ceiling and was force-synthesised over
   the whole transcript cites the most records, and the verify pass sent ALL
   of it — up to 40 claims, each cited record's evidence at 4,000 chars — in
   one request with no input bound of its own. A request past the judge
   route's window comes back refused or empty on every rung, the pass falls to
   the floor, and the stamp said ``judge_unavailable`` with the real cause
   buried in ``unverified_reason`` (which nothing printed).

And the ordering itself was a convention, not a contract: the judge ran
inside the loop BEFORE the watch mint and before the result left the analyst,
and nothing tied its verdict to the text the front door then persisted.

THE FIX.

* The judge runs at the SETTLE POINT — :func:`judge_settled_result`, the last
  step of ``run_method`` on both paths (the loop and the recovery replay),
  after every edit and the mint — and stamps ``settled_sha256`` over the exact
  answer and cited-ref set it graded. The front door
  (``registry.consult_turn_stamps.verification_stamp``) re-derives the digest
  from the answer it persists and withdraws a verdict whose text moved
  (``judged_text_changed`` / ``not_settled``).
* The judge is EXEMPT from the run's spend ceiling, by construction and now by
  statement: a turn the cap stopped is judged, never marked unavailable for
  having been capped. It gets its OWN small budget instead —
  ``LEGBA_CONSULT_JUDGE_MAX_INPUT_TOKENS`` (12,000 estimated tokens of claims +
  evidence): the cited claims are fitted to it from the head (the tail is
  dropped and counted, ``claims_dropped_for_budget``), and only when not even
  the first claim's evidence fits once clipped is the turn not judged, with
  ``budget_exhausted``. It is a $0 lane by default; this bounds the request's
  size for the route's window, not its cost.
* Every not-judged stamp that is a failure to judge carries
  ``judge_unavailable_reason`` from a closed vocabulary — ``budget_exhausted``
  | ``route_down`` | ``timeout`` | ``not_settled`` (defined in the registry
  leaf both images import) — and the panel prints it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import UUID

from ..registry import consult_turn_stamps as _stamps

logger = logging.getLogger(__name__)

__all__ = [
    "CONSULT_JUDGE_STACK_REF_ENV",
    "DEFAULT_CONSULT_JUDGE_STACK_REF",
    "STATE_JUDGED",
    "STATE_NOT_JUDGED",
    "STATE_UNASSESSABLE",
    "VERIFICATION_SCHEMA",
    "build_consult_verify_inputs",
    "consult_judge_ref",
    "fit_judge_input",
    "judge_consult_turn",
    "judge_settled_result",
    "not_judged",
]

VERIFICATION_SCHEMA = "consult_verification/1"

STATE_JUDGED = "judged"
STATE_UNASSESSABLE = "unassessable"
STATE_NOT_JUDGED = "not_judged"

CONSULT_JUDGE_STACK_REF_ENV = "LEGBA_CONSULT_JUDGE_STACK_REF"
DEFAULT_CONSULT_JUDGE_STACK_REF = "llm.verify.slm_8b"
CONSULT_JUDGE_TIMEOUT_ENV = "LEGBA_CONSULT_JUDGE_TIMEOUT_SECONDS"
DEFAULT_CONSULT_JUDGE_TIMEOUT_SECONDS = 90.0
CONSULT_JUDGE_MAX_CLAIMS_ENV = "LEGBA_CONSULT_JUDGE_MAX_CLAIMS"
DEFAULT_CONSULT_JUDGE_MAX_CLAIMS = 40
#: W11-F93 — the judge's OWN input budget (claims + evidence, estimated tokens),
#: separate from the run's spend ceiling, which never applies to the judge.
CONSULT_JUDGE_MAX_INPUT_TOKENS_ENV = "LEGBA_CONSULT_JUDGE_MAX_INPUT_TOKENS"
DEFAULT_CONSULT_JUDGE_MAX_INPUT_TOKENS = 12_000
#: The estimate: ~4 characters a token, plus the judge's own prompt.
_CHARS_PER_TOKEN = 4
_JUDGE_PROMPT_OVERHEAD_TOKENS = 2_000
#: Per-citation bridge framing (title/source labels), characters.
_CITATION_FRAME_CHARS = 40
#: Evidence is never clipped below this per record to make one claim fit.
_MIN_EVIDENCE_CHARS = 400
_ORDINAL_RE = re.compile(r"\[(\d+)\]")

#: Bounded list of the unsupported claims the stamp carries, for the tooltip.
_UNSUPPORTED_SAMPLE = 8
_UNSUPPORTED_TEXT_CHARS = 240
_UNSUPPORTED_FULL_CHARS = 1000

#: A bracket group that may carry refs: ``[f7a4de08-…]``, ``[198e33db]``,
#: ``[a, b]``, and the ``[[ref:<uuid>]]`` form. Markdown links ``[t](u)`` and
#: ordinals ``[3]`` never tokenise as refs (a ref token is 8+ hex).
_BRACKET_RE = re.compile(r"\[\[ref:([0-9a-fA-F-]{8,36})\]\]|\[([^\[\]\n]{8,400})\]")
_REF_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{1,12}){0,4}$")
_TOKEN_SPLIT_RE = re.compile(r"[\s,;]+")
#: A sentence ends at ``.``/``!``/``?`` — optionally inside a closing bold,
#: italic, quote or bracket (``…displaced.** Iran``) — before whitespace and
#: a sentence-opening character. Fixed-width lookbehinds, one per closer.
_SENTENCE_SPLIT_RE = re.compile(
    r"(?:(?<=[.!?])|(?<=[.!?]\*\*)|(?<=[.!?][*_\"”’)\]]))\s+(?=[\"“(*_\[A-Z0-9])"
)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def consult_judge_ref() -> str:
    """The first judge rung for consult turns (env, else the in-house 8B)."""
    raw = (os.getenv(CONSULT_JUDGE_STACK_REF_ENV) or "").strip()
    return raw or DEFAULT_CONSULT_JUDGE_STACK_REF


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def not_judged(reason: str, **extra: Any) -> dict[str, Any]:
    """The explicit label: no judge adjudicated this turn, and why.

    A label that is a failure to judge also says WHY in the closed W11-F93
    vocabulary (``judge_unavailable_reason``); ``extra`` may pass it explicitly.
    """
    stamp = {
        "schema": VERIFICATION_SCHEMA,
        "state": STATE_NOT_JUDGED,
        "score": None,
        "reason": reason,
        **extra,
    }
    why = _stamps.unavailable_reason_for(stamp)
    if why is not None:
        stamp["judge_unavailable_reason"] = why
    return stamp


def _canon(token: str) -> str:
    try:
        return str(UUID(token))
    except (ValueError, AttributeError):
        return token.lower()


def _resolve_token(token: str, cited: Sequence[str]) -> str | None:
    """A ref token → the full cited id it names, or ``None``.

    A full uuid is taken as written (canonicalised) — it resolves, or not, at
    the bridge, which is the honest place for a fabricated id to fail. A
    prefix must name exactly ONE of the answer's own cited refs; an ambiguous
    or unknown prefix is no citation at all.
    """
    token = token.strip().lower()
    if not _REF_TOKEN_RE.match(token):
        return None
    if len(token) == 36:
        return _canon(token)
    hits = {c for c in cited if c.startswith(token)}
    return next(iter(hits)) if len(hits) == 1 else None


def build_consult_verify_inputs(
    answer: str, cited_refs: Sequence[Any], *, max_claims: int | None = None,
) -> tuple[str, list[str], int, bool]:
    body, ordered, total, truncated, _originals = _verify_claims(
        answer, cited_refs, max_claims=max_claims,
    )
    return body, ordered, total, truncated


def _verify_claims(
    answer: str, cited_refs: Sequence[Any], *, max_claims: int | None = None,
) -> tuple[str, list[str], int, bool, dict[str, str]]:
    """``(body, ordered_refs, cited_sentences, truncated)`` for the verify pass.

    ``body`` holds ONLY the answer's cited sentences, one per paragraph, each
    ref marker rewritten to the ordinal ``[N]`` the floor and the judge bind
    on; ``ordered_refs[N-1]`` is the id behind ``[N]``. ``cited_sentences`` is
    how many sentences carried a marker (before the cap); ``truncated`` says
    the cap cut some. An empty ``body`` means nothing was cited.
    """
    cap = max_claims if max_claims is not None else int(
        _env_float(CONSULT_JUDGE_MAX_CLAIMS_ENV, DEFAULT_CONSULT_JUDGE_MAX_CLAIMS)
    )
    cited = [_canon(str(r)) for r in cited_refs if r]
    ordered: list[str] = []
    index: dict[str, int] = {}

    def _ordinal(ref: str) -> int:
        if ref not in index:
            ordered.append(ref)
            index[ref] = len(ordered)
        return index[ref]

    def _sub(m: re.Match[str]) -> str:
        if m.group(1):
            ref = _resolve_token(m.group(1), cited)
            return f"[{_ordinal(ref)}]" if ref else m.group(0)
        tokens = [t for t in _TOKEN_SPLIT_RE.split(m.group(2)) if t]
        refs = [_resolve_token(t, cited) for t in tokens]
        if not refs or any(r is None for r in refs):
            return m.group(0)
        return "".join(f"[{_ordinal(r)}]" for r in refs if r)

    kept: list[str] = []
    originals: dict[str, str] = {}
    total = 0
    for line in (answer or "").splitlines():
        if not line.strip() or _HEADING_RE.match(line):
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(line.strip()):
            before = len(ordered), dict(index)
            rewritten = _BRACKET_RE.sub(_sub, sentence)
            if rewritten == sentence:
                continue
            total += 1
            if len(kept) >= cap:
                # Roll back ordinals this uncounted sentence introduced, so the
                # bridge carries no ref no judged claim cites.
                del ordered[before[0]:]
                index.clear()
                index.update(before[1])
                continue
            kept.append(rewritten.strip())
            originals[rewritten.strip()] = sentence.strip()
    return "\n\n".join(kept), ordered, total, total > len(kept), originals


def _citation_chars(c: Mapping[str, Any]) -> int:
    return _CITATION_FRAME_CHARS + sum(
        len(str(c.get(k) or "")) for k in ("title", "source", "source_text")
    )


def _refs_used(claims: Sequence[str], n_citations: int) -> int:
    """How many leading citations the claims bind (ordinals are first-seen)."""
    top = 0
    for claim in claims:
        for m in _ORDINAL_RE.finditer(claim):
            top = max(top, int(m.group(1)))
    return min(top, n_citations)


def fit_judge_input(
    claims: Sequence[str],
    citations: Sequence[Mapping[str, Any]],
    *,
    max_input_tokens: int | None = None,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]] | None:
    """Fit the claims + their evidence to the judge's own input budget.

    Claims are kept from the HEAD (they are in answer order) while the request
    fits; the tail is dropped and counted. Ordinals are assigned first-seen, so
    the kept claims bind a PREFIX of ``citations`` and the rest is dropped with
    them. When not even the first claim fits, its evidence is clipped evenly
    (never below ``_MIN_EVIDENCE_CHARS`` a record). ``None`` = nothing fits:
    the judge's budget is exhausted before it reads a single claim.
    """
    budget = max_input_tokens if max_input_tokens is not None else judge_max_input_tokens()
    budget_chars = (budget - _JUDGE_PROMPT_OVERHEAD_TOKENS) * _CHARS_PER_TOKEN
    cites = [dict(c) for c in citations]
    if budget_chars <= 0 or not claims:
        return None

    def _cost(kept: Sequence[str], used: Sequence[Mapping[str, Any]]) -> int:
        return sum(len(c) + 2 for c in kept) + sum(_citation_chars(c) for c in used)

    keep = 0
    for k in range(1, len(claims) + 1):
        if _cost(claims[:k], cites[: _refs_used(claims[:k], len(cites))]) > budget_chars:
            break
        keep = k
    clipped = False
    if keep == 0:
        used = cites[: _refs_used(claims[:1], len(cites))]
        if not used:
            return None
        frame = len(claims[0]) + 2 + sum(
            _CITATION_FRAME_CHARS + len(str(c.get("title") or ""))
            + len(str(c.get("source") or "")) for c in used
        )
        share = (budget_chars - frame) // len(used)
        if share < _MIN_EVIDENCE_CHARS:
            return None
        for c in used:
            c["source_text"] = str(c.get("source_text") or "")[:share]
        clipped, keep = True, 1
    kept = list(claims[:keep])
    used = cites[: _refs_used(kept, len(cites))]
    return kept, used, {
        "claims_dropped_for_budget": len(claims) - keep,
        "evidence_clipped": clipped,
        "judge_input_tokens_est": _cost(kept, used) // _CHARS_PER_TOKEN
        + _JUDGE_PROMPT_OVERHEAD_TOKENS,
        "judge_max_input_tokens": budget,
    }


def judge_max_input_tokens() -> int:
    """Env ``LEGBA_CONSULT_JUDGE_MAX_INPUT_TOKENS`` (a bad pin falls back)."""
    return int(_env_float(
        CONSULT_JUDGE_MAX_INPUT_TOKENS_ENV, DEFAULT_CONSULT_JUDGE_MAX_INPUT_TOKENS,
    ))


async def _bridge(pg_pool: Any, ordered_refs: list[str]) -> list[dict[str, Any]]:
    from ...runtime.actor_critic import _resolve_journal_citation_bridge

    async with pg_pool.acquire() as conn:
        return await _resolve_journal_citation_bridge(conn, ordered_refs)


async def judge_consult_turn(
    *,
    answer: str,
    cited_refs: Sequence[Any],
    synthesis_status: str,
    judge_resolver: Callable[[str], Awaitable[Any]] | None,
    pg_pool: Any | None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """The turn's ``consult_verification/1`` stamp. Never raises."""
    try:
        return await _judge(
            answer=answer, cited_refs=cited_refs,
            synthesis_status=synthesis_status, judge_resolver=judge_resolver,
            pg_pool=pg_pool,
            timeout_s=timeout_s or _env_float(
                CONSULT_JUDGE_TIMEOUT_ENV, DEFAULT_CONSULT_JUDGE_TIMEOUT_SECONDS
            ),
        )
    except Exception as exc:  # noqa: BLE001 — an answer is never lost to its verify
        logger.warning("consult.judge.failed err=%s", exc, exc_info=True)
        name = type(exc).__name__
        return not_judged(
            "judge_failed", detail=name,
            judge_unavailable_reason=(
                _stamps.UNAVAILABLE_BUDGET_EXHAUSTED if name == "BudgetExhausted"
                else _stamps.UNAVAILABLE_TIMEOUT if "Timeout" in name
                else _stamps.UNAVAILABLE_ROUTE_DOWN
            ),
        )


async def _judge(
    *,
    answer: str,
    cited_refs: Sequence[Any],
    synthesis_status: str,
    judge_resolver: Callable[[str], Awaitable[Any]] | None,
    pg_pool: Any | None,
    timeout_s: float,
) -> dict[str, Any]:
    from ..provenance.judge_assessability import (
        ADJUDICATED_JUDGE_STATUSES,
        NON_SCORE_STATES,
    )
    from ..provenance.judge_pipeline_version import JUDGE_PIPELINE_VERSION
    from ..provenance.verify import _llm_judge_enabled, verify_finding_faithfulness

    if synthesis_status != "complete":
        return not_judged("synthesis_incomplete")
    body, ordered, cited_sentences, truncated, originals = _verify_claims(
        answer, cited_refs,
    )
    if not body:
        # Nothing cited — the census counts it as model knowledge; a judge has
        # nothing to grade. A state with a reason, never a number.
        return {
            "schema": VERIFICATION_SCHEMA,
            "state": STATE_UNASSESSABLE,
            "score": None,
            "reason": "no_cited_claims",
            "cited_sentences": 0,
            "judge_pipeline_version": JUDGE_PIPELINE_VERSION,
        }
    if not _llm_judge_enabled():
        return not_judged("judge_disabled", cited_sentences=cited_sentences)
    if judge_resolver is None:
        return not_judged("no_judge_route", cited_sentences=cited_sentences)
    if pg_pool is None:
        return not_judged("no_evidence_reader", cited_sentences=cited_sentences)

    from ...runtime.judge_fallback_chain import (
        JudgeChain,
        handler_model,
        judge_stamps,
        open_session,
        resolve_judge_chain,
        weak_rungs,
    )

    ref = consult_judge_ref()
    try:
        first = await judge_resolver(ref)
    except Exception as exc:  # noqa: BLE001 — an unbuildable route is a label
        logger.warning("consult.judge.route_unresolved ref=%s err=%s", ref, exc)
        return not_judged(
            "judge_route_unresolved", judge_llm_ref=ref,
            cited_sentences=cited_sentences,
        )
    rungs = resolve_judge_chain({}, ref)
    judge: Any = first
    if len(rungs) > 1:
        judge = JudgeChain(
            rungs, first_handler=first, factory=judge_resolver, route_class="consult",
        )
    session = open_session(judge)
    # W11-F93 — the judge's OWN budget, never the run's spend ceiling: a turn
    # the cap stopped is the biggest turn there is, and is judged on as much of
    # it as the judge route can read, from the head.
    fitted = fit_judge_input(body.split("\n\n"), await _bridge(pg_pool, ordered))
    if fitted is None:
        return not_judged(
            "judge_input_over_budget", judge_llm_ref=ref,
            cited_sentences=cited_sentences,
            judge_max_input_tokens=judge_max_input_tokens(),
        )
    claims, citations, fit = fitted
    body = "\n\n".join(claims)
    try:
        report = await asyncio.wait_for(
            verify_finding_faithfulness(
                body=body, citations=citations, judge_llm=session,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return not_judged(
            "judge_timeout", judge_llm_ref=ref, timeout_s=timeout_s,
            cited_sentences=cited_sentences,
        )

    judge_model, judge_ref, judge_route, extras = judge_stamps(
        judge=judge, session=session, judge_model=handler_model(first),
        judge_llm_ref=ref, judge_route="consult", report=report,
    )
    stamp: dict[str, Any] = {
        "schema": VERIFICATION_SCHEMA,
        "judge_status": report.judge_status,
        "checkable_claims": report.checkable_claims,
        "supported_claims": report.supported_claims,
        "cited_sentences": cited_sentences,
        "claims_judged": len(claims),
        "claims_truncated": truncated or fit["claims_dropped_for_budget"] > 0,
        **fit,
        # W11-F93 — what was graded, so the front door can prove it persists
        # the same answer (``consult_turn_stamps.verification_stamp``).
        "settled_sha256": _stamps.settle_digest(answer, cited_refs),
        "judge_model": judge_model or None,
        "judge_llm_ref": judge_ref or None,
        "judge_route": judge_route or None,
        "weak_judge": bool(
            (extras.get("judge_chain") or {}).get("weak") or judge_ref in weak_rungs()
        ),
        "judge_pipeline_version": JUDGE_PIPELINE_VERSION,
        "judged_at": _now(),
        **extras,
    }
    if report.score_state in NON_SCORE_STATES:
        return {
            **stamp, "state": STATE_UNASSESSABLE, "score": None,
            "reason": report.score_state_reason or report.score_state,
        }
    if report.judge_status not in ADJUDICATED_JUDGE_STATUSES:
        # Every rung failed or answered nothing: the WHY is the last failure's
        # class (``unverified_reason``), in the closed vocabulary; the verify
        # pass's own reason (``judge_error`` / ``judge_empty``) rides beside it.
        unverified = extras.get("unverified_reason") or {}
        return {
            **stamp, "state": STATE_NOT_JUDGED, "score": None,
            "reason": "judge_unavailable",
            "verify_unavailable_reason": report.judge_unavailable_reason,
            "judge_unavailable_reason": _stamps.unavailable_reason_for_class(
                unverified.get("class") if isinstance(unverified, Mapping) else None
            ),
        }
    return {
        **stamp,
        "state": STATE_JUDGED,
        "score": round(float(report.faithfulness_score), 4),
        "reason": None,
        "unsupported": [
            {
                "text": s.text[:_UNSUPPORTED_TEXT_CHARS],
                "reason": s.reason,
            }
            for s in report.unsupported_spans[:_UNSUPPORTED_SAMPLE]
        ],
        # Q-1 (rule D) — the WHOLE list, each claim mapped back to the answer
        # sentence it was cut from, so the grounding pass can act on every one
        # (the sample above stays the tooltip's bounded list).
        "unsupported_claims": [
            {
                "text": _original_for(s.text, originals)[:_UNSUPPORTED_FULL_CHARS],
                "claim": s.text[:_UNSUPPORTED_FULL_CHARS],
                "reason": s.reason,
            }
            for s in report.unsupported_spans
        ],
    }


def _original_for(claim: str, originals: Mapping[str, str]) -> str:
    """The answer sentence a judged claim (or a sub-span of one) came from."""
    claim = (claim or "").strip()
    if claim in originals:
        return originals[claim]
    for rewritten, original in originals.items():
        if claim and (claim in rewritten or rewritten in claim):
            return original
    return claim


async def judge_settled_result(
    result: Any,
    *,
    deps: Any,
    record: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> Any:
    """The SETTLE POINT (W11-F93): judge the result ``run_method`` returns.

    Called last on both paths — after every text edit, the census, the floor
    and the watch mint — so the answer and cited refs judged are the ones the
    front door persists; the stamp carries ``settled_sha256`` to prove it. The
    judge reads nothing of the run's spend meter (exempt from the per-run
    ceiling; it keeps its own input budget). Stamps ``data["verification"]`` on
    the response AND on the finding carrier, and rides one ``judge_turn``
    frame. Never raises; a result with no consult response passes through.
    """
    consult = getattr(result, "consult_response", None)
    if consult is None:
        return result
    stamp = await judge_consult_turn(
        answer=consult.answer,
        cited_refs=[str(r) for r in consult.cited_substrate_refs],
        synthesis_status=str(consult.data.get("synthesis_status") or "complete"),
        judge_resolver=getattr(deps, "judge_resolver", None),
        # Q-2: deep consult reads evidence through ``judge_pool`` (its
        # ``pg_pool`` stays unset so the chat-only watch mint stays off).
        pg_pool=getattr(deps, "judge_pool", None) or getattr(deps, "pg_pool", None),
    )
    if record is not None:
        try:
            await record({
                "phase": "verify",
                "kind": "judge_turn",
                **{k: stamp.get(k) for k in (
                    "state", "score", "reason", "judge_llm_ref",
                    "judge_unavailable_reason",
                )},
            })
        except Exception:  # noqa: BLE001 — telemetry never costs the stamp
            logger.debug("consult.judge.frame_failed", exc_info=True)
    consult = consult.model_copy(update={"data": {**consult.data, "verification": stamp}})
    finding = getattr(result, "finding", None)
    if isinstance(getattr(finding, "data", None), dict) and "consult_response" in finding.data:
        finding = finding.model_copy(update={"data": {
            **finding.data, "consult_response": consult.model_dump(mode="json"),
        }})
    return dataclasses.replace(result, consult_response=consult, finding=finding)
