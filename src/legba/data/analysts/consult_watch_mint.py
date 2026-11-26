# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""F-4 ``propose_watch`` — a consult's watch candidates become standing open questions.

Since W1 (F-1) a consult answer closes with "what would change my mind" and
puts the same items on its ``watch_candidates:`` header; the turn persists them
on its response bag (migration 0223). They stopped there: nothing watched them.
The platform already HAS the watcher — ``claim_watch``, the ~30-minute
deterministic META analyst that matches NEW signals against the standing
OPEN-QUESTION set (``hypotheses`` rows with ``status='open_question'``) and
side-writes ``bearing_edges`` / ``review_flags``. A consult's "what would change
my mind" is exactly an open question with a resolution test and a horizon, so
this module mints each candidate as one such row, and claim_watch picks it up
on its next tick. There is no new table and no new watcher.

THE WRITE PATH IS THE ``open_question`` TOOL'S. Every row goes through
:func:`legba.data.analysts.agency.write_tools.write_open_question` — the CW-3
referent fold (a deictic indicator, "the vote is postponed", is folded against
the consult's own QUESTION, the sentence it was written about), the CW-8 office
guard, and the provenance writer ``write_hypothesis`` — never a raw INSERT.

WHAT A CANDIDATE MUST CARRY, or it is not minted:

  * **a resolution test** — the observation that resolves it, in the
    candidate's own words. The indicator IS that observation ("a second
    independent outlet names the successor"); a candidate with no words left
    once its horizon clause is removed has none, and is stamped
    ``unmintable_no_test``.
  * **a horizon** (SW-10's rule, never a default). The candidate's own
    ``by YYYY-MM-DD`` clause; else the END of the arc window the answer names
    ("survey window: 90 days, 2026-06-29 to 2026-09-27") when that end is
    still in the future; else the candidate is REFUSED with SW-10's own reason,
    ``hypothesis_refused_no_resolves_by``. A horizon at or before now is
    refused by the same name. A survey window ends at the survey, so in
    practice the candidate's own date is the route — the system prompt asks
    for it.

WHERE THE FACTS LIVE. The ``hypotheses`` table has no ``data``, ``created_by``,
``resolution_test`` or ``resolves_by`` column (``writes._insert_hypothesis``
drops payload extras). Everything durable rides the ``open_question_origin``
marker in ``diagnostic_evidence`` — the containment-queryable vocabulary the
K-2a harvest, the K-2b converter and the research dispatcher already share —
with ``origin='consult'`` and ``created_by='consult'``; the row's
``analyst_id``/``run_id`` are the consult descriptor and the actor's run.

THE FOLD (idempotency). The marker's ``source_id`` is
``consult:<sha256(normalised indicator)[:16]>``. Before writing, a containment
probe looks for an OPEN consult watch with that key whose horizon has not
passed; found, the candidate FOLDS onto it (``folded: true``, the existing id)
and nothing is written. A second consult on the same question proposing the
same indicator therefore re-uses the standing watch. The probe and the write
run under a transaction-scoped advisory lock on the key, so two concurrent
consults cannot both miss and both mint.

THE SEMANTIC FOLD (W4-F1, :mod:`.consult_watch_fold`). The key is exact text and
the model re-phrases every time, so a key MISS is then checked against the
desk's standing consult watches whose horizon is within ±3 days: identical claim
cores, or core embeddings at cosine >= ``FOLD_COSINE_THRESHOLD``, fold onto the
standing row (``folded_onto`` + ``similarity`` on the entry, nothing written).
One batched embed call per turn; no embedder or a failed call mints anyway and
stamps ``fold_check: unavailable`` on the entry and the row's marker. The
residual race: two CONCURRENT consults proposing differently-worded duplicates
can both mint (the advisory lock is per exact key); the one-off
``scripts/fold_consult_watches_once.py`` folds such pairs after the fact.

SUBJECTS. The row's ``target_id`` is the desk the turn's citations name most —
claim_watch's per-desk specificity fold and geo plane key on it — and every
cited desk rides the marker's ``subjects``. A turn whose citations name no desk
writes ``target_id`` NULL (claim_watch's shared ``""`` bucket), never a guess.

THE OUTCOME is stamped on the response bag as ``data["watch_minted"]`` — one
entry per candidate: ``{candidate, hypothesis_id, refused, reason, resolves_by,
folded}``, plus ``fold_check`` (and ``folded_onto`` / ``similarity`` when they
apply) on every candidate that reached the fold. The key is ABSENT when there were no candidates, when the descriptor
switched the mint off (``mint_watch_candidates: false``), or when the run has
no write surface (a hand-built deps bundle): absence is "not attempted", never
"zero minted". DEGRADE-NOT-BREAK: nothing here may fail an answer.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID

logger = logging.getLogger(__name__)

#: The descriptor knob (``ANALYST_KIND_OPTIONS["consult_on_demand"]``).
MINT_OPTION = "mint_watch_candidates"
#: ON by default: a minted row costs nothing paid — claim_watch's bearing
#: pipeline runs on the self-hosted planes (its descriptor's primary is the
#: core plane, the gate is the self-hosted 8B, the embedder is the models host's).
DEFAULT_MINT = True

#: The response-bag key (``consult_persistence.RESPONSE_BAG_KEYS``).
BAG_KEY = "watch_minted"

#: The marker key every harvested/converted open question shares (mirrored from
#: ``open_question_conversion.OPEN_QUESTION_MARKER_KEY``; drift-guarded by test).
MARKER_KEY = "open_question_origin"
MARKER_ORIGIN = "consult"
CREATED_BY = "consult"

#: Refusal reasons, by name.
REASON_NO_TEST = "unmintable_no_test"
#: SW-10's own refusal (``inquiry.HYPOTHESIS_WITHOUT_HORIZON_FLAG`` /
#: ``inquiry_state.HYPOTHESIS_NO_HORIZON_REFUSAL``; drift-guarded by test).
REASON_NO_HORIZON = "hypothesis_refused_no_resolves_by"
REASON_OFFICE = "ungrounded_office"
REASON_DEAD_LETTER = "dead_letter"
REASON_FAILED = "mint_failed"

#: The candidate's own horizon clause: a trailing ``by <ISO date[time]>``,
#: optionally set off by a dash, comma or parenthesis.
_HORIZON_RE = re.compile(
    r"[\s,;:(\[—–-]*\bby\s+"
    r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?)"
    r"\s*[)\]]?\s*\.?\s*$",
    re.IGNORECASE,
)
#: The arc window the system prompt makes an answer name.
_SURVEY_WINDOW_RE = re.compile(
    r"survey window\s*:[^\n]*?\bto\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_HAS_WORD_RE = re.compile(r"[^\W\d_]{2,}")
#: The core-plane models write dates with Unicode dashes (roll-32 proof,
#: 2026-10-03: ``by 2026\u201110\u201117`` — U+2011 NON-BREAKING HYPHEN — was
#: refused "names no horizon"). Every dash-class code point folds to ``-``
#: BEFORE the horizon parse, so the model's typography never decides whether a
#: watch stands. The fold key sees the same folded text.
_DASH_FOLD = {c: "-" for c in map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d")}

# The fold probe: an OPEN consult watch with this key whose horizon has not
# passed. Containment first (the shared marker shape), then the horizon read
# off the very element that matched.
_FOLD_PROBE_SQL = """
    SELECT h.id
      FROM hypotheses h
     WHERE h.status = 'open_question'
       AND h.diagnostic_evidence @> $1::jsonb
       AND EXISTS (
             SELECT 1
               FROM jsonb_array_elements(h.diagnostic_evidence) e
              WHERE e ->> 'source_id' = $2
                AND (e ->> 'resolves_by')::timestamptz > now()
           )
     ORDER BY h.produced_at DESC, h.id
     LIMIT 1
"""

# The desks the turn's citations name, most-cited first.
_CITED_SUBJECTS_SQL = """
    SELECT target_id, count(*)::int AS n
      FROM analyst_outputs
     WHERE id = ANY($1::uuid[])
       AND target_id IS NOT NULL
     GROUP BY target_id
     ORDER BY n DESC, target_id
"""


@dataclasses.dataclass(frozen=True)
class WatchCandidate:
    """One parsed candidate: the raw text, its resolution test, its horizon."""

    raw: str
    test: str
    horizon_text: str | None


_CLOSERS = {")": "(", "]": "["}
_EDGE_PUNCT = " \t,;:()[]—–-"


def _closes_inner_group(text: str) -> bool:
    """Does the trailing closer of ``text`` close a group opened INSIDE it?"""
    opener = _CLOSERS[text[-1]]
    return text[:-1].count(opener) > text[:-1].count(text[-1])


def _strip_indicator(indicator: str) -> str:
    """Strip edge punctuation from an indicator, but keep a trailing ``)`` /
    ``]`` that closes a group opened inside it — "(e.g., new missile system)"
    is the indicator's own words; "(by <date>)" wrappers are not (the horizon
    clause already consumed those)."""
    text = indicator.lstrip(_EDGE_PUNCT)
    while text:
        tail = text[-1]
        if tail in _CLOSERS and _closes_inner_group(text):
            break
        if tail not in _EDGE_PUNCT:
            break
        text = text[:-1]
    return text


def parse_candidate(raw: str) -> WatchCandidate:
    """Split a candidate into its indicator (the resolution test) and its own
    ``by <date>`` horizon clause. Never invents either."""
    text = " ".join(str(raw or "").translate(_DASH_FOLD).split())
    horizon_text: str | None = None
    indicator = text
    m = _HORIZON_RE.search(text)
    if m:
        horizon_text = m.group(1)
        indicator = text[: m.start()]
    indicator = _strip_indicator(indicator)
    test = indicator if _HAS_WORD_RE.search(indicator) else ""
    return WatchCandidate(raw=text, test=test, horizon_text=horizon_text)


def survey_window_end(answer: str | None) -> datetime | None:
    """The END of the arc window the answer names, or ``None``."""
    from .inquiry import parse_horizon

    m = _SURVEY_WINDOW_RE.search(str(answer or ""))
    return parse_horizon(m.group(1)) if m else None


def resolve_horizon(
    candidate: WatchCandidate, *, answer: str | None, now: datetime,
) -> tuple[datetime | None, str | None, str | None]:
    """``(resolves_by, source, refusal_detail)`` — exactly one of the first and
    the last is set. SW-10's rule: the horizon must be readable and in the
    future, and there is no default."""
    from .inquiry import parse_horizon

    if candidate.horizon_text is not None:
        horizon = parse_horizon(candidate.horizon_text)
        if horizon is None:
            return None, None, (
                f"horizon {candidate.horizon_text!r} is not an ISO-8601 date"
            )
        if horizon <= now:
            return None, None, (
                f"resolves_by {horizon.isoformat()} is not in the future"
            )
        return horizon, "candidate", None
    window_end = survey_window_end(answer)
    if window_end is not None:
        if window_end <= now:
            return None, None, (
                f"the candidate names no horizon and the answer's arc window "
                f"ends {window_end.date().isoformat()}, not in the future"
            )
        return window_end, "arc_window", None
    return None, None, (
        "the candidate names no horizon and the answer names no arc window"
    )


def fold_key(test: str) -> str:
    """The idempotency key — the normalised indicator, hashed."""
    norm = " ".join(test.casefold().split()).strip(" .;,")
    return "consult:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def mint_enabled(options: Mapping[str, Any] | None) -> bool:
    raw = (options or {}).get(MINT_OPTION)
    return raw if isinstance(raw, bool) else DEFAULT_MINT


def _turn_index(first: Mapping[str, Any]) -> int | None:
    """This assistant turn's 0-based index in its session: how many assistant
    turns the client-held transcript already carries. ``None`` when the run
    carried no transcript (a recovery replay)."""
    messages = first.get("messages")
    if not isinstance(messages, list):
        return None
    return sum(
        1 for m in messages if isinstance(m, Mapping) and m.get("role") == "assistant"
    )


def _origin(first: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "session_id": first.get("session_id"),
        "request_id": first.get("request_id"),
        "turn": _turn_index(first),
    }
    if first.get("synthesize_from"):
        out["recovers_turn_id"] = first.get("turn_id")
    return out


def _analyst_ctx(options: Mapping[str, Any], target_id: str | None) -> Any | None:
    from ..provenance import AnalystContext

    analyst_id = options.get("analyst_id")
    version = options.get("analyst_version")
    run_id = options.get("run_id")
    if not analyst_id or not version or not run_id:
        return None
    try:
        run_uuid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
    except (TypeError, ValueError):
        return None
    return AnalystContext(
        analyst_id=str(analyst_id),
        analyst_version=str(version),
        run_id=run_uuid,
        target_id=target_id,
    )


async def _cited_subjects(conn: Any, refs: list[UUID]) -> list[str]:
    if not refs:
        return []
    try:
        rows = await conn.fetch(_CITED_SUBJECTS_SQL, refs)
    except Exception:  # noqa: BLE001 — a subject is a nicety, the watch is not
        logger.warning("consult.watch_mint.subjects_failed", exc_info=True)
        return []
    return [str(r["target_id"]) for r in rows]


async def mint_watch_candidates(
    consult: Any,
    *,
    first: Mapping[str, Any],
    options: Mapping[str, Any],
    pool: Any,
    now: datetime | None = None,
    embedder: Any | None = None,
) -> list[dict[str, Any]] | None:
    """Mint ``consult``'s watch candidates; return the ``watch_minted`` stamp.

    ``None`` = not attempted (no candidates, the knob off, no write surface),
    and the caller then stamps nothing. ``embedder`` is the embed plane the
    semantic fold (:mod:`.consult_watch_fold`) uses; None mints unchecked.
    """
    from .agency.write_tools import write_open_question

    candidates = consult.data.get("watch_candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    if not mint_enabled(options) or pool is None:
        return None
    if _analyst_ctx(options, None) is None:
        logger.warning(
            "consult.watch_mint.no_identity — the run options carry no "
            "analyst_id/analyst_version/run_id; nothing minted"
        )
        return None
    now = now or datetime.now(timezone.utc)
    refs = list(consult.cited_substrate_refs)
    origin = _origin(first)
    out: list[dict[str, Any]] = []
    try:
        async with pool.acquire() as conn:
            subjects = await _cited_subjects(conn, refs)
            target = subjects[0] if subjects else None
            ctx = _analyst_ctx(options, target)
            ready: list[tuple[dict[str, Any], _Ready]] = []
            for raw in candidates:
                entry: dict[str, Any] = {
                    "candidate": str(raw), "hypothesis_id": None, "refused": False,
                    "reason": None, "resolves_by": None, "folded": False,
                }
                out.append(entry)
                try:
                    prepared = _prepare(entry, parse_candidate(str(raw)), consult=consult, now=now)
                except Exception:  # noqa: BLE001 — one candidate never sinks its siblings
                    logger.warning(
                        "consult.watch_mint.candidate_failed candidate=%r",
                        str(raw)[:120], exc_info=True,
                    )
                    entry.update(refused=True, reason=REASON_FAILED)
                    continue
                if prepared is not None:
                    ready.append((entry, prepared))
            plan = await _plan(conn, ready, target=target, embedder=embedder)
            for entry, prepared in ready:
                try:
                    await _mint_one(
                        conn, entry, prepared,
                        consult=consult, ctx=ctx, refs=refs, origin=origin,
                        subjects=subjects, now=now, write=write_open_question,
                        plan=plan,
                    )
                except Exception:  # noqa: BLE001 — one candidate never sinks its siblings
                    logger.warning(
                        "consult.watch_mint.candidate_failed candidate=%r",
                        entry["candidate"][:120], exc_info=True,
                    )
                    entry.update(refused=True, reason=REASON_FAILED)
    except Exception:  # noqa: BLE001 — the write surface failed; say so per candidate
        logger.warning("consult.watch_mint.surface_failed", exc_info=True)
        decided = len(out)
        out.extend(
            {"candidate": str(raw), "hypothesis_id": None, "refused": True,
             "reason": REASON_FAILED, "resolves_by": None, "folded": False}
            for raw in candidates[decided:]
        )
        for entry in out[:decided]:
            if entry["hypothesis_id"] is None and not entry["refused"]:
                entry.update(refused=True, reason=REASON_FAILED)
    return out


@dataclasses.dataclass(frozen=True)
class _Ready:
    """A candidate that passed SW-10: its parse, horizon, key and fold slot."""

    cand: WatchCandidate
    horizon: datetime
    source: str
    key: str
    core: str
    slot: int


def _prepare(
    entry: dict[str, Any], cand: WatchCandidate, *, consult: Any, now: datetime,
) -> _Ready | None:
    """SW-10's gates; ``None`` (and the refusal stamped) when the candidate
    cannot stand. The slot is assigned by :func:`_plan`."""
    if not cand.test:
        entry.update(refused=True, reason=REASON_NO_TEST)
        return None
    horizon, source, refusal = resolve_horizon(cand, answer=consult.answer, now=now)
    if horizon is None:
        logger.info(
            "consult.watch_mint.%s candidate=%r detail=%s",
            REASON_NO_HORIZON, cand.raw[:120], refusal,
        )
        entry.update(refused=True, reason=REASON_NO_HORIZON, detail=refusal)
        return None
    from .consult_watch_fold import claim_core

    entry["resolves_by"] = horizon.isoformat()
    return _Ready(
        cand=cand, horizon=horizon, source=str(source), key=fold_key(cand.test),
        core=claim_core(cand.test), slot=-1,
    )


async def _plan(
    conn: Any,
    ready: list[tuple[dict[str, Any], _Ready]],
    *,
    target: str | None,
    embedder: Any | None,
) -> Any | None:
    """The semantic fold plan for the candidates whose lexical key MISSES (the
    fast path folds the rest with no vector). Slots are assigned in candidate
    order; ``None`` when every candidate already folds lexically."""
    from .consult_watch_fold import plan_fold

    misses: list[tuple[int, str, datetime]] = []
    for i, (entry, r) in enumerate(ready):
        probe = json.dumps([{"marker": MARKER_KEY, "origin": MARKER_ORIGIN, "source_id": r.key}])
        if await conn.fetchval(_FOLD_PROBE_SQL, probe, r.key) is None:
            slot = len(misses)
            ready[i] = (entry, dataclasses.replace(r, slot=slot))
            misses.append((slot, r.core, r.horizon))
    if not misses:
        return None
    return await plan_fold(conn, misses, target_id=target, embedder=embedder)


async def _mint_one(
    conn: Any,
    entry: dict[str, Any],
    ready: _Ready,
    *,
    consult: Any,
    ctx: Any,
    refs: list[UUID],
    origin: Mapping[str, Any],
    subjects: list[str],
    now: datetime,
    write: Callable[..., Awaitable[Any]],
    plan: Any | None,
) -> None:
    from .consult_watch_fold import CHECK_KEY

    cand, key = ready.cand, ready.key
    marker = {
        "marker": MARKER_KEY,
        "origin": MARKER_ORIGIN,
        "source_id": key,
        "created_by": CREATED_BY,
        "candidate": cand.raw[:512],
        "resolution_test": cand.test[:512],
        "resolves_by": ready.horizon.isoformat(),
        "horizon_source": ready.source,
        "subjects": subjects[:16],
        **origin,
    }
    probe = json.dumps([{"marker": MARKER_KEY, "origin": MARKER_ORIGIN, "source_id": key}])
    async with conn.transaction():
        # Serialise the probe + write per key: two concurrent consults proposing
        # the same indicator cannot both miss the probe and both mint.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
        existing = await conn.fetchval(_FOLD_PROBE_SQL, probe, key)
        if existing is not None:
            entry.update(
                hypothesis_id=str(existing), folded=True,
                folded_onto=str(existing), fold_check=CHECK_KEY,
            )
            return
        decision = (
            plan.decide(ready.slot, ready.core, ready.horizon)
            if plan is not None and ready.slot >= 0 else None
        )
        if decision is not None:
            entry["fold_check"] = decision.check
            marker["fold_check"] = decision.check
            if decision.similarity is not None:
                entry["similarity"] = decision.similarity
                marker["fold_similarity"] = decision.similarity
            if decision.onto is not None:
                entry.update(
                    hypothesis_id=decision.onto, folded=True, folded_onto=decision.onto,
                )
                return
        written = await write(
            conn,
            analyst_ctx=ctx,
            question=cand.test,
            derived_from=refs,
            data={"kind": "open_question", "requested_by": CREATED_BY},
            origin_context=consult.question,
            diagnostic_evidence=[marker],
            requested_by=CREATED_BY,
        )
    if written.refused is not None:
        entry.update(refused=True, reason=REASON_OFFICE, detail=written.refused)
    elif written.row is None:
        entry.update(refused=True, reason=REASON_DEAD_LETTER)
    else:
        entry["hypothesis_id"] = str(written.row.id)
        if plan is not None and ready.slot >= 0:
            plan.add_minted(ready.slot, entry["hypothesis_id"], ready.core,
                            ready.horizon, now)


async def mint_on_result(
    result: Any,
    *,
    first: Mapping[str, Any],
    options: Mapping[str, Any],
    deps: Any,
    record: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> Any:
    """The ``run_method`` hook: mint on an accepted final (the native route, the
    forced final and the recovery replay all return through here), stamp the
    bag on the response AND on the finding carrier, record one trace step.
    Returns ``result`` unchanged when nothing was attempted."""
    consult = getattr(result, "consult_response", None)
    if consult is None:
        return result
    try:
        from .consult_watch_fold import resolve_fold_embedder

        minted = await mint_watch_candidates(
            consult, first=first, options=options,
            pool=getattr(deps, "pg_pool", None),
            embedder=resolve_fold_embedder(deps),
        )
    except Exception:  # noqa: BLE001 — never fails an answer
        logger.warning("consult.watch_mint.failed", exc_info=True)
        return result
    if minted is None:
        return result
    consult = consult.model_copy(update={"data": {**consult.data, BAG_KEY: minted}})
    finding = result.finding
    if isinstance(getattr(finding, "data", None), dict) and "consult_response" in finding.data:
        finding = finding.model_copy(update={"data": {
            **finding.data, "consult_response": consult.model_dump(mode="json"),
        }})
    if record is not None:
        await record({
            "phase": "reflect",
            "kind": "watch_mint",
            "minted": sum(1 for e in minted if e["hypothesis_id"] and not e["folded"]),
            "folded": sum(1 for e in minted if e["folded"]),
            "refused": dict(Counter(e["reason"] for e in minted if e["refused"])),
            "fold_checks": dict(Counter(e["fold_check"] for e in minted if e.get("fold_check"))),
        })
    return dataclasses.replace(result, consult_response=consult, finding=finding)


__all__ = [
    "BAG_KEY",
    "DEFAULT_MINT",
    "MINT_OPTION",
    "REASON_DEAD_LETTER",
    "REASON_FAILED",
    "REASON_NO_HORIZON",
    "REASON_NO_TEST",
    "REASON_OFFICE",
    "WatchCandidate",
    "fold_key",
    "mint_enabled",
    "mint_on_result",
    "mint_watch_candidates",
    "parse_candidate",
    "resolve_horizon",
    "survey_window_end",
]
