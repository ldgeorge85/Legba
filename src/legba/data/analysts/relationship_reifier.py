# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``relationship_reifier`` — the PIECE A reified-typed-Nexus producer.

A META analyst kind (sibling of ``meta_findings_synthesizer`` /
``hypothesis_lifecycle``) that turns flat co-mentioned entity pairs into
FIRST-CLASS, signed, typed, temporally-bound ``nexus`` rows.

What it does, once per cadence tick (one global sweep — META analyst):

  1. **READ** candidate co-mentioned entity pairs from ``proposed_edges`` (the
     ``entity_resolution`` producer already lands ``co_occurs`` edges there),
     enriched with any recent ``facts`` bearing on the pair so the LLM has
     context. The AGE graph is consulted best-effort to skip pairs that are
     already reified.
  2. **TYPE** each candidate via the LLM provider plane (the D2 8B path — reuse
     the analyst LLM handle; NEVER litellm). The model assigns a typed
     ``rel_type`` (a canonical predicate), a canonical **polarity sign**
     (+1 supportive / -1 antagonistic / 0 neutral, the structural-balance
     convention), an ``intent``, a ``channel`` (direct/proxy/covert/...), and
     — when the relationship runs through a cut-out — an ``intermediary``.
  3. **WRITE** a ``nexus`` row per typed pair via the live ``write_nexus`` path
     (``valid_from`` = the pair's event time; supersession on a polarity/label
     CHANGE for the same typed triple). This is the same side-write discipline
     ``situation_clustering``/``hypothesis_lifecycle`` use: the nexus rows are
     the real output; the per-run ``FindingPayload`` summary
     (``candidates``/``typed``/``written``/``superseded``/``degraded``) is the
     cadence receipt.

Discipline:

  * **degrade-not-drop** — any per-candidate LLM/parse failure logs, flips the
    run's ``degraded`` counter, and skips THAT candidate; the sweep continues
    and still writes the candidates that did type. Mirrors ``fact_extractor``.
  * **budget plane** — the run checks ``deps.budget.check_envelope()`` before
    each LLM call and stops issuing new calls once the envelope is exhausted
    (the descriptor also caps ``budget_tokens_per_day``); per-run candidate
    count is hard-capped (``MAX_CANDIDATES_PER_RUN``). Token ``usage`` rolls up
    into the returned summary for the runtime's budget recorder.

The polarity sign is the load-bearing artifact: it is what lets the dormant
``structural_balance`` (signed-triad balance) + ``graph_mining`` (proxy-chain
sign products) consumers light up over a SIGNED graph instead of the untyped
``CoOccursWith`` edges they see today (PIECE A light-up).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from uuid import UUID

from ..provenance.models import FindingPayload, NexusPayload
from ..provenance.writes import write_nexus
from ...runtime.analyst_method import AnalystMethodResult, LLMHandlerLike

# The authoritative canonical edge-label → polarity table lives with the
# structural-balance handler (the consumer). The reifier maps the LLM's typed
# label through the SAME table so producer + consumer agree on the sign — there
# is one canonical POLARITY map in the tree, not two.
from .deterministic_handlers.structural_balance import POLARITY

logger = logging.getLogger(__name__)

KIND_NAME: str = "relationship_reifier"
HANDLER_VERSION: str = "0.1.0"
PROMPT_MODULE_PATH: str = "legba.prompts.relationship_reifier.v1"

# OUTPUT_KIND is TRACE_ONLY: this META analyst's REAL product is the `nexus`
# rows it side-writes via write_nexus on the run's own connection. The per-run
# summary FindingPayload it returns is purely a run-receipt — and every run is
# already fully audited in `analyst_traces` (the summary survives in
# `analyst_traces.output_payload`). Marking it TRACE_ONLY stops the redundant
# FINDING row in `analyst_outputs` ("Findings as a real output type" cleanup)
# while keeping the trace + the write_nexus side-writes intact. `run_method`
# still returns AnalystMethodResult(finding=<summary>) so the trace captures it.
from ..provenance.kinds import TRACE_ONLY as _TRACE_ONLY  # noqa: E402
from ..provenance.kinds import OutputKind as _OutputKind  # noqa: E402,F401

OUTPUT_KIND: object = _TRACE_ONLY


# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

DEFAULT_MAX_TOKENS: int = 384
"""Completion budget per typing call. The output is one tiny typed JSON object,
not prose — keep it small (8B path, cost-sensitive)."""

DEFAULT_TEMPERATURE: float = 0.1
"""Typing wants determinism."""

MAX_CANDIDATES_PER_RUN: int = 40
"""Hard cap on co-mentioned pairs typed per cadence tick. Bounds the per-run
LLM spend regardless of how many pending edges exist."""

MIN_EDGE_CONFIDENCE: float = 0.45
"""Skip the thinnest co-occurrence edges — a single co-mention (confidence ~0.4
from entity_resolution) is too weak to reify. Pairs accrue confidence as they
re-co-occur; this floors the candidate set at "seen more than once"."""

MAX_FACTS_CONTEXT: int = 6
"""Recent facts about either endpoint rendered into the typing prompt."""

MAX_INTERMEDIARY_CANDIDATES: int = 5
"""Cap on cut-out candidates offered to the typer per pair (#99). The model
SELECTS an intermediary from this OFFERED set (or null) — it never free-texts a
famous-but-absent proxy. Kept small to bound prompt tokens."""

MIN_INTERMEDIARY_PAIR_CONFIDENCE: float = 0.55
"""Only the more-corroborated (A,B) pairs get the (more expensive) 3-entity
candidate path. A bare single co-mention is too thin to chase a cut-out."""

# Canonical relationship-type set the LLM may pick from. This is the POLARITY
# table's key set — the model is constrained to labels we can sign. Anything
# off-list maps to polarity 0 (neutral) at coercion time.
ALLOWED_REL_TYPES: tuple[str, ...] = tuple(POLARITY.keys())

_VALID_CHANNELS: frozenset[str] = frozenset(
    {"direct", "proxy", "covert", "institutional"}
)
_VALID_INTENTS: frozenset[str] = frozenset(
    {"supportive", "hostile", "dual-use", "neutral"}
)


# ---------------------------------------------------------------------------
# Deps surface — LLM port + pg_pool (the reifier reads candidates + recent
# facts and side-writes nexus rows on its own connection, like the
# deterministic META handlers; it is NOT a pure inputs->finding kind).
# ---------------------------------------------------------------------------


@runtime_checkable
class _BudgetLike(Protocol):
    async def check_envelope(self) -> str: ...


@dataclass
class ReifierDeps:
    """The dep bundle ``run_method`` needs.

    Built by ``analyst_deps_builder._build_relationship_reifier`` from the
    resolved primary LLM + the run's ``StandardDeps`` (pg_pool + budget). Tests
    construct it directly with a stub LLM + a real test pg_pool.
    """

    llm: LLMHandlerLike
    pg_pool: Any = None
    budget: _BudgetLike | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    max_candidates: int = MAX_CANDIDATES_PER_RUN
    system_prompt: str | None = None


# ---------------------------------------------------------------------------
# Typing prompt
# ---------------------------------------------------------------------------

from ._tradecraft import with_preamble  # noqa: E402

_SYSTEM_PROMPT = with_preamble(
    """TASK — type the relationship between two co-mentioned entities. Decide whether a meaningful, directed relationship holds and, if so, classify it.
Return ONE JSON object, nothing else:
{
  "related": true|false,            // false => no real relationship; skip
  "subject": "<acting entity>",     // who initiates/conducts
  "object": "<affected entity>",    // who is targeted/affected
  "intermediary": "<proxy>"|null,   // a cut-out the relationship runs through, else null
  "rel_type": "<one of the allowed types>",
  "polarity": -1|0|1,               // -1 antagonistic, +1 supportive, 0 neutral/dual-use
  "intent": "supportive"|"hostile"|"dual-use"|"neutral",
  "channel": "direct"|"proxy"|"covert"|"institutional",
  "confidence": 0.0-1.0
}
Rules: pick rel_type ONLY from the allowed list. If merely co-mentioned with no real relationship, set related=false.
INTERMEDIARY rule: set "intermediary" to null UNLESS a "Candidate intermediaries" list is offered AND one of those listed entities genuinely acts as the cut-out the A->B relationship runs through. You MUST copy the intermediary VERBATIM from the offered list — never name a proxy that is not on the list, however plausible. If no offered candidate fits, intermediary=null and channel is direct/institutional as appropriate.
Worked examples:
  - Hostile supply via a proxy: A arms a militia that attacks B -> subject=A, object=B, intermediary=the militia, rel_type=SuppliesWeaponsTo, polarity=-1, channel=proxy, intent=hostile.
  - Institutional membership: country X joins alliance Y -> subject=X, object=Y, intermediary=null, rel_type=MemberOf, polarity=+1, channel=institutional, intent=supportive.
  - Dual-use presence: company C operates a facility in country D with no stated alignment -> subject=C, object=D, intermediary=null, rel_type=OperatesIn, polarity=0, channel=direct, intent=dual-use."""
)


def _build_user_prompt(
    *,
    source: str,
    target: str,
    evidence_text: str,
    facts: Sequence[Mapping[str, Any]],
    candidate_intermediaries: Sequence[str] = (),
) -> str:
    lines = [
        f"Entity A: {source}",
        f"Entity B: {target}",
        f"Allowed rel_type values: {', '.join(ALLOWED_REL_TYPES)}",
        "",
        "Co-mention evidence:",
        (evidence_text or "(none)")[:1200],
    ]
    if facts:
        lines.append("")
        lines.append("Recent facts about these entities:")
        for f in facts[:MAX_FACTS_CONTEXT]:
            lines.append(
                f"  - {f.get('subject')} {f.get('predicate')} {f.get('value')}"
            )
    if candidate_intermediaries:
        lines.append("")
        lines.append(
            "Candidate intermediaries (third entities co-mentioned with BOTH "
            "A and B). SELECT one ONLY if it is the cut-out the A->B "
            "relationship runs through — copy it verbatim — else use null:"
        )
        for c in candidate_intermediaries:
            lines.append(f"  - {c}")
    lines.append("")
    lines.append("Classify the relationship as the JSON object specified.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM call + parse
# ---------------------------------------------------------------------------


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Best-effort: pull the first balanced ``{...}`` object out of an LLM
    response (handles ```json fences + leading prose). Mirrors the
    meta_findings_synthesizer parser. Returns None on failure (degrade)."""
    if not raw:
        return None
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    start = candidate.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for i in range(start, len(candidate)):
        c = candidate[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        parsed = json.loads(candidate[start:end])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _canonical_polarity(rel_type: str, llm_polarity: Any) -> int:
    """Resolve the canonical sign. The POLARITY table is authoritative for the
    rel_type; the LLM's polarity only overrides for the proxy/intent twist
    (e.g. a hostile SuppliesWeaponsTo already signs -1, but a hostile-via-proxy
    on an otherwise-neutral predicate needs the LLM sign). We take the table
    sign when it is non-zero, else fall back to the LLM's sign coerced to
    {-1,0,+1}."""
    table = POLARITY.get(rel_type, 0)
    if table != 0:
        return table
    try:
        v = int(llm_polarity)
    except (TypeError, ValueError):
        return 0
    return 1 if v > 0 else (-1 if v < 0 else 0)


def _coerce_typing(
    obj: Mapping[str, Any],
    *,
    fallback_subject: str,
    fallback_object: str,
    allowed_intermediaries: Sequence[str] = (),
) -> NexusPayload | None:
    """Turn the parsed LLM object into a validated :class:`NexusPayload`, or
    ``None`` when the model said there is no real relationship / the shape is
    unusable (degrade-not-drop: a None just skips this candidate).

    ``allowed_intermediaries`` is the OFFERED cut-out set (#99). A returned
    intermediary that is not in this set is dropped to null — the typer SELECTS
    or nulls, it may never free-text a famous-but-absent proxy. When no set is
    offered, any returned intermediary is also dropped (no candidate path ran)."""
    if not obj.get("related", False):
        return None
    rel_type = str(obj.get("rel_type") or "").strip()
    if rel_type not in ALLOWED_REL_TYPES:
        # Off-list label — the consumers can't sign it; skip rather than write a
        # neutral nexus that adds no signal.
        return None
    subject = str(obj.get("subject") or fallback_subject).strip()
    object_ = str(obj.get("object") or fallback_object).strip()
    if not subject or not object_ or subject.lower() == object_.lower():
        return None
    intermediary = obj.get("intermediary")
    intermediary = (
        str(intermediary).strip() if intermediary not in (None, "", "null") else None
    )
    # SELECT-or-null enforcement: an intermediary survives ONLY if it is one of
    # the offered candidates (case-insensitive) and is distinct from both
    # endpoints. Anything else (a hallucinated proxy, or one returned when no
    # candidates were offered) is nulled — the relationship stays direct.
    if intermediary is not None:
        _allowed = {c.strip().lower() for c in allowed_intermediaries if c.strip()}
        if (
            intermediary.lower() not in _allowed
            or intermediary.lower() == subject.lower()
            or intermediary.lower() == object_.lower()
        ):
            intermediary = None
    polarity = _canonical_polarity(rel_type, obj.get("polarity"))
    channel = str(obj.get("channel") or "direct").strip().lower()
    if channel not in _VALID_CHANNELS:
        channel = "proxy" if intermediary else "direct"
    # A "proxy" channel is meaningless without a cut-out — if the intermediary
    # was nulled (hallucinated / not offered), the relationship is direct.
    if channel == "proxy" and not intermediary:
        channel = "direct"
    intent = str(obj.get("intent") or "").strip().lower()
    if intent not in _VALID_INTENTS:
        intent = "hostile" if polarity < 0 else ("supportive" if polarity > 0 else "neutral")
    try:
        confidence = float(obj.get("confidence", 0.6))
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))
    try:
        return NexusPayload(
            subject=subject[:2048],
            intermediary=(intermediary[:2048] if intermediary else None),
            object=object_[:2048],
            rel_type=rel_type,
            label=f"{subject} {rel_type} {object_}"[:4096],
            polarity=polarity,
            intent=intent,
            channel=channel,
            confidence=confidence,
        )
    except Exception as exc:  # pragma: no cover - pydantic guard
        logger.warning("relationship_reifier.coerce_failed err=%s", exc)
        return None


# ---------------------------------------------------------------------------
# Substrate reads (own connection — the META/maintenance precedent)
# ---------------------------------------------------------------------------


async def _read_candidates(conn: Any, *, limit: int) -> list[dict[str, Any]]:
    """Pull pending co-mentioned entity pairs from ``proposed_edges`` that are
    not yet reified into an OPEN nexus. Ordered by confidence so the
    most-corroborated pairs are typed first within the per-run cap."""
    rows = await conn.fetch(
        """
        SELECT pe.source_entity, pe.target_entity, pe.evidence_text,
               pe.confidence, pe.produced_at, pe.derived_from
          FROM proposed_edges pe
         WHERE pe.confidence >= $1
           AND NOT EXISTS (
               SELECT 1 FROM nexuses n
                WHERE n.valid_until IS NULL AND n.superseded_by IS NULL
                  AND lower(n.subject) = lower(pe.source_entity)
                  AND lower(n.object)  = lower(pe.target_entity)
           )
         ORDER BY pe.confidence DESC, pe.produced_at DESC
         LIMIT $2
        """,
        MIN_EDGE_CONFIDENCE,
        limit,
    )
    return [dict(r) for r in rows]


async def _intermediary_candidates_for(
    conn: Any, *, source: str, target: str, limit: int
) -> list[str]:
    """Third entities C co-mentioned with BOTH A and B (#99 proxy-chain path).

    A ``co_occurs`` edge in ``proposed_edges`` is undirected for this purpose, so
    C is any entity that shares a co_occurs edge with A AND a (distinct) co_occurs
    edge with B — a structurally-plausible cut-out for the A->B relationship. We
    return only NAMES (the typer SELECTS verbatim from this offered set, never
    free-texts), ordered by combined corroboration so the strongest cut-outs come
    first within the small cap. C is never A or B."""
    rows = await conn.fetch(
        """
        WITH neighbours AS (
            SELECT
                CASE WHEN lower(source_entity) = lower($1)
                     THEN target_entity ELSE source_entity END AS c,
                confidence,
                $1 AS anchor
              FROM proposed_edges
             WHERE relationship_type = 'co_occurs'
               AND (lower(source_entity) = lower($1)
                    OR lower(target_entity) = lower($1))
            UNION ALL
            SELECT
                CASE WHEN lower(source_entity) = lower($2)
                     THEN target_entity ELSE source_entity END AS c,
                confidence,
                $2 AS anchor
              FROM proposed_edges
             WHERE relationship_type = 'co_occurs'
               AND (lower(source_entity) = lower($2)
                    OR lower(target_entity) = lower($2))
        )
        SELECT c, sum(confidence) AS score
          FROM neighbours
         WHERE lower(c) <> lower($1)
           AND lower(c) <> lower($2)
         GROUP BY lower(c), c
        HAVING count(DISTINCT lower(anchor)) = 2
         ORDER BY score DESC
         LIMIT $3
        """,
        source,
        target,
        limit,
    )
    return [str(r["c"]) for r in rows]


async def _recent_facts_for(
    conn: Any, *, source: str, target: str
) -> list[dict[str, Any]]:
    """Open facts whose subject is either endpoint — context for the typer."""
    rows = await conn.fetch(
        """
        SELECT subject, predicate, value
          FROM facts
         WHERE valid_until IS NULL AND superseded_by IS NULL
           AND (lower(subject) = lower($1) OR lower(subject) = lower($2))
         ORDER BY confidence DESC, produced_at DESC
         LIMIT $3
        """,
        source,
        target,
        MAX_FACTS_CONTEXT,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# LLM port
# ---------------------------------------------------------------------------


async def _type_via_llm(
    llm: LLMHandlerLike,
    *,
    user_prompt: str,
    system_prompt: str,
    max_tokens: int,
    temperature: float,
) -> tuple[str, dict[str, int]]:
    """One chat_complete typing call. Mirrors the sibling kinds' shape."""
    messages = [{"role": "user", "content": user_prompt}]
    response = await llm.chat_complete(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
    )
    content = getattr(response, "content", "") or ""
    usage_raw = getattr(response, "usage", None)
    usage = {
        "prompt_tokens": getattr(usage_raw, "prompt_tokens", 0) if usage_raw else 0,
        "completion_tokens": (
            getattr(usage_raw, "completion_tokens", 0) if usage_raw else 0
        ),
        "reasoning_tokens": (
            getattr(usage_raw, "reasoning_tokens", 0) if usage_raw else 0
        ),
    }
    return content, usage


# ---------------------------------------------------------------------------
# Public entry — run_method
# ---------------------------------------------------------------------------


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: ReifierDeps | LLMHandlerLike,
) -> AnalystMethodResult:
    """Execute one ``relationship_reifier`` sweep.

    ``deps`` accepts a :class:`ReifierDeps` (production) or a bare
    :class:`LLMHandlerLike` (the back-compat test path — pg-less, types only
    the rows passed in ``inputs``). Returns an :class:`AnalystMethodResult`
    whose ``finding`` is the per-run summary; the nexus rows are side-written.
    """
    if not isinstance(deps, ReifierDeps):
        deps = ReifierDeps(llm=deps)

    analyst_id = str(options.get("analyst_id") or KIND_NAME)
    target_id = options.get("target_id")
    run_id = options.get("run_id")
    if isinstance(run_id, str):
        try:
            run_id = UUID(run_id)
        except ValueError:
            run_id = None
    system_prompt = deps.system_prompt or _SYSTEM_PROMPT
    now = datetime.now(tz=timezone.utc)

    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    candidates: list[dict[str, Any]] = []
    pool = deps.pg_pool

    # 1) Assemble candidate pairs. Prefer the live proposed_edges sweep; fall
    #    back to the inputs the runtime materialized (test / no-pool path).
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                candidates = await _read_candidates(conn, limit=deps.max_candidates)
        except Exception as exc:
            logger.warning("relationship_reifier.read_candidates_failed err=%s", exc)
            candidates = []
    if not candidates:
        for row in inputs[: deps.max_candidates]:
            src = row.get("source_entity") or row.get("subject") or row.get("src")
            tgt = row.get("target_entity") or row.get("object") or row.get("dst")
            if src and tgt:
                candidates.append({
                    "source_entity": str(src),
                    "target_entity": str(tgt),
                    "evidence_text": str(row.get("evidence_text") or ""),
                    "produced_at": row.get("produced_at") or now,
                    "derived_from": list(row.get("derived_from") or []),
                })

    n_candidates = len(candidates)
    typed = 0
    written = 0
    superseded = 0
    degraded = 0
    budget_paused = False

    for cand in candidates:
        # Honor the budget envelope before each LLM call (degrade-not-drop:
        # stop issuing new calls, keep what we already wrote).
        if deps.budget is not None:
            try:
                envelope = await deps.budget.check_envelope()
            except Exception:  # pragma: no cover - defensive
                envelope = "ok"
            if envelope != "ok":
                budget_paused = True
                break

        source = str(cand["source_entity"])
        target = str(cand["target_entity"])
        facts_ctx: list[dict[str, Any]] = []
        intermediaries: list[str] = []
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    facts_ctx = await _recent_facts_for(
                        conn, source=source, target=target
                    )
                    # 3-entity proxy-chain path (#99): only for the more-
                    # corroborated pairs (cost guard), offer the third entities
                    # co-mentioned with BOTH endpoints so the typer SELECTS a
                    # real cut-out instead of hallucinating one.
                    try:
                        pair_conf = float(cand.get("confidence") or 0.0)
                    except (TypeError, ValueError):
                        pair_conf = 0.0
                    if pair_conf >= MIN_INTERMEDIARY_PAIR_CONFIDENCE:
                        intermediaries = await _intermediary_candidates_for(
                            conn,
                            source=source,
                            target=target,
                            limit=MAX_INTERMEDIARY_CANDIDATES,
                        )
            except Exception:  # pragma: no cover - context is best-effort
                facts_ctx = []
                intermediaries = []

        user_prompt = _build_user_prompt(
            source=source,
            target=target,
            evidence_text=str(cand.get("evidence_text") or ""),
            facts=facts_ctx,
            candidate_intermediaries=intermediaries,
        )
        try:
            raw, usage = await _type_via_llm(
                deps.llm,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                max_tokens=deps.max_tokens,
                temperature=deps.temperature,
            )
        except Exception as exc:
            logger.warning(
                "relationship_reifier.llm_failed pair=%s/%s err=%s",
                source, target, exc,
            )
            degraded += 1
            continue
        for k in total_usage:
            total_usage[k] += int(usage.get(k, 0) or 0)

        obj = _extract_json_object(raw)
        if obj is None:
            logger.warning(
                "relationship_reifier.parse_failed pair=%s/%s", source, target
            )
            degraded += 1
            continue
        payload = _coerce_typing(
            obj,
            fallback_subject=source,
            fallback_object=target,
            allowed_intermediaries=intermediaries,
        )
        if payload is None:
            # Model said no real relationship, or shape unusable — not a
            # failure, just nothing to reify for this pair.
            continue
        typed += 1

        # event time = the pair's produced_at (the co-mention's event clock),
        # else now. Mirrors fact_extractor stamping valid_from at event time.
        ev = cand.get("produced_at")
        payload.valid_from = ev if isinstance(ev, datetime) else now
        derived = [u for u in (cand.get("derived_from") or []) if isinstance(u, UUID)]
        payload.source_signal_ids = list(derived)

        # 3) Side-write the nexus row (write_nexus supersedes a prior open row
        #    on polarity/label change). Degrade-not-drop on write failure.
        if pool is None:
            # No-pool test path: the typing was counted (``typed``); without a
            # pool there is nothing to persist, so skip the write and move on.
            continue
        from ..provenance import AnalystContext  # local import — avoid cycle

        actx = AnalystContext(
            analyst_id=analyst_id,
            analyst_version=str(options.get("analyst_version") or ""),
            run_id=run_id if isinstance(run_id, UUID) else None,  # type: ignore[arg-type]
            target_id=target_id,
            target_version=options.get("target_version"),
        )
        try:
            async with pool.acquire() as conn:
                before = await conn.fetchval(
                    "SELECT count(*) FROM nexuses "
                    "WHERE lower(subject)=lower($1) AND lower(object)=lower($2) "
                    "AND superseded_by IS NOT NULL",
                    payload.subject, payload.object,
                )
                out, dlq = await write_nexus(
                    conn,
                    analyst_ctx=actx,
                    payload=payload,
                    derived_from=derived,
                )
                if out is not None:
                    written += 1
                    after = await conn.fetchval(
                        "SELECT count(*) FROM nexuses "
                        "WHERE lower(subject)=lower($1) AND lower(object)=lower($2) "
                        "AND superseded_by IS NOT NULL",
                        payload.subject, payload.object,
                    )
                    if (after or 0) > (before or 0):
                        superseded += 1
                elif dlq is not None:
                    degraded += 1
        except Exception as exc:
            logger.warning(
                "relationship_reifier.write_failed pair=%s/%s err=%s",
                source, target, exc,
            )
            degraded += 1
            continue

    finding = _build_summary(
        n_candidates=n_candidates,
        typed=typed,
        written=written,
        superseded=superseded,
        degraded=degraded,
        budget_paused=budget_paused,
        target_id=target_id,
    )
    return AnalystMethodResult(finding=finding, usage=total_usage)


def _build_summary(
    *,
    n_candidates: int,
    typed: int,
    written: int,
    superseded: int,
    degraded: int,
    budget_paused: bool,
    target_id: str | None,
) -> FindingPayload:
    title = (
        f"Relationship reifier: {written} nexuses written "
        f"({typed} typed / {n_candidates} candidates)"
    )
    if target_id:
        title = f"{title} for {target_id}"
    tags = ["meta", "relationship_reifier"]
    if written:
        tags.append("nexuses_written")
    if degraded:
        tags.append("degraded")
    if budget_paused:
        tags.append("budget_paused")
    return FindingPayload(
        title=title[:2048],
        body=(
            f"candidates={n_candidates} typed={typed} written={written} "
            f"superseded={superseded} degraded={degraded} "
            f"budget_paused={budget_paused}"
        )[:65536],
        confidence=1.0,
        tags=tags,
        data={
            "meta": True,
            "sub_handler": "relationship_reifier",
            "candidates": n_candidates,
            "typed": typed,
            "written": written,
            "superseded": superseded,
            "degraded": degraded,
            "budget_paused": budget_paused,
        },
    )


__all__ = [
    "KIND_NAME",
    "OUTPUT_KIND",
    "ReifierDeps",
    "run_method",
    "ALLOWED_REL_TYPES",
]
