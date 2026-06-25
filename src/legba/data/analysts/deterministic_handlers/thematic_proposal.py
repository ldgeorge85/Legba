# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""thematic_proposal — PROPOSE thematic frames for uncovered hot situations.

Phase 5b (detect → propose → promote). The bottom-up ``situation_clustering``
pipeline materializes situations as a BYPRODUCT of per-country analysis; this
detector closes the loop toward 5c thematic targets: it finds OPEN,
high-intensity situations that NO active thematic target covers and PROPOSEs a
candidate thematic frame for each — a suggested ``contains_any`` scope.predicate
an operator can review and register (via
``scripts/bringup_register_situation_targets.py``) to give that situation its
OWN focused signal slice + dedicated analyst.

Capability model (ratified): **analysts PROPOSE, operators ACTIVATE — no
control-plane writes.** This handler therefore NEVER registers a target; it
emits a single FINDING whose ``data.proposals`` lists the candidate frames. The
operator promotes by registering the suggested descriptor. Once a thematic
target covers a situation (its predicate mentions the situation's salient
terms), the situation stops being proposed — the loop converges.

Deterministic + pure: candidate terms come from the situation NAME (the latest
member finding's framing); "covered" is a substring check against the active
thematic targets' predicates. No LLM (semantic theme-discovery is the declared
enhancement). ``deps=None`` runs a synthetic (no-DB) summary for unit tests.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "thematic_proposal"

# Only OPEN frames at/above this recency-weighted intensity are worth proposing
# as their own thematic target (env-tunable LEGBA_SITUATION_DETECT_MIN_INTENSITY).
# Intensity = sum of exp(-half-life) member weights, so ~1.0 ≈ one fresh member.
# The live ceiling observed to date is ~1.3 (situations rarely carry multiple
# very-fresh members at once), so the floor is 1.0 — high enough to skip stale
# decayed frames, low enough to be reachable. Raise it via env in a busier feed.
_DEFAULT_MIN_INTENSITY = 1.0
# How many candidate terms to put in a suggested contains_any predicate, and how
# many proposals to emit per run (keep the proposal finding readable).
_MAX_TERMS = 6
_MAX_PROPOSALS = 10

# Compact English stopword set — enough to keep a suggested predicate to the
# salient nouns of a headline (the operator edits the suggestion anyway).
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "as", "into", "over", "after", "before", "amid", "its",
    "is", "are", "was", "were", "be", "been", "has", "have", "had", "will",
    "new", "says", "said", "could", "would", "may", "might", "their", "this",
    "that", "these", "those", "his", "her", "they", "them", "more", "than",
    "against", "between", "while", "about", "near", "amids", "via", "per",
})
_TERM_RE = re.compile(r"[a-z][a-z0-9'\-]{2,}")


def min_intensity() -> float:
    """Intensity floor for proposing a situation as a thematic frame."""
    raw = os.getenv("LEGBA_SITUATION_DETECT_MIN_INTENSITY")
    if not raw or not raw.strip():
        return _DEFAULT_MIN_INTENSITY
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MIN_INTENSITY


def candidate_terms(name: str) -> list[str]:
    """Salient lowercased terms from a situation name (stopword/length filtered),
    most-distinctive (longest) first, de-duplicated, capped at ``_MAX_TERMS``."""
    seen: set[str] = set()
    terms: list[str] = []
    # Longest tokens first — they're the most discriminative for a keyword frame.
    for tok in sorted(_TERM_RE.findall(name.casefold()), key=len, reverse=True):
        if tok in _STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        terms.append(tok)
        if len(terms) >= _MAX_TERMS:
            break
    return terms


def suggested_predicate(terms: list[str]) -> str:
    """A ``contains_any([...])`` scope.predicate from candidate terms."""
    quoted = ", ".join(f'"{t}"' for t in terms)
    return f"contains_any([{quoted}])"


#: The quoted terms inside an active thematic predicate (`contains_any(["iran",
#: "tehran"])` → iran, tehran), so coverage matches a situation TERM against a
#: predicate TERM — not a raw substring of the whole `contains_any([...])` text.
_PRED_TERM_RE = re.compile(r'"([^"]+)"')


def _is_covered(terms: list[str], covered_text: str) -> bool:
    """True when an active thematic target already frames this situation.

    Matches BIDIRECTIONALLY on inflection — a situation term "Iranian"/"attacks"
    must count as covered by a predicate term "iran"/"attack" (and vice versa).
    The old one-directional `term in covered_text` check missed exactly that
    (``"iranian" in "iran"`` is False), so an already-framed situation was
    re-proposed forever and the loop never converged. Compares quoted predicate
    terms (not the raw `contains_any(...)` text) so the helper name/brackets
    can't spuriously match.
    """
    pred_terms = _PRED_TERM_RE.findall(covered_text)
    return any(
        c == p or c in p or p in c
        for c in terms
        for p in pred_terms
        if c and p
    )


def _proposal(sig: str, name: str, intensity: float, terms: list[str]) -> dict[str, Any]:
    # Suffix a short signature hash so two unrelated situations that happen to
    # share a longest term (both "diplomatic") don't collide on one target id.
    import hashlib
    h = hashlib.sha1((sig or "").encode("utf-8")).hexdigest()[:6]
    base = re.sub(r"[^a-z0-9]+", "_", (terms[0] if terms else "situation")).strip("_")
    return {
        "situation_signature": sig,
        "name": name[:512],
        "intensity_score": round(float(intensity), 4),
        "suggested_target_id": f"situation_{base}_{h}"[:128],
        "suggested_predicate": suggested_predicate(terms),
        "terms": terms,
    }


def _build_proposals(
    situations: list[dict[str, Any]], covered_text: str, *, floor: float,
) -> list[dict[str, Any]]:
    """Pure core: high-intensity OPEN situations not yet covered → proposals,
    most-intense first."""
    out: list[dict[str, Any]] = []
    for s in sorted(
        situations, key=lambda r: float(r.get("intensity_score") or 0.0), reverse=True
    ):
        intensity = float(s.get("intensity_score") or 0.0)
        if intensity < floor:
            continue
        name = str(s.get("name") or "")
        terms = candidate_terms(name)
        if not terms or _is_covered(terms, covered_text):
            continue
        out.append(_proposal(str(s.get("situation_signature") or ""), name, intensity, terms))
        if len(out) >= _MAX_PROPOSALS:
            break
    return out


def _build_finding(proposals: list[dict[str, Any]]) -> FindingPayload:
    n = len(proposals)
    if n:
        lead = ", ".join(p["suggested_target_id"] for p in proposals[:5])
        body = (
            f"{n} open high-intensity situation(s) lack a dedicated thematic "
            f"target. Suggested frames (operator-register to promote): {lead}"
            + (" …" if n > 5 else "")
        )
    else:
        body = "No uncovered high-intensity situations to propose as thematic frames."
    return FindingPayload(
        title=f"Situation detection: {n} candidate thematic frame(s) proposed"[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME, "proposal"],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "proposals": proposals if n <= _MAX_PROPOSALS else proposals[:_MAX_PROPOSALS],
            "proposal_count": n,
        },
    )


async def _resolve_pool(pool: Any, *, floor: float) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT situation_signature, name, intensity_score
            FROM situations
            WHERE superseded_by IS NULL
              AND (valid_until IS NULL OR valid_until > now())
              AND status <> 'closed'
              AND intensity_score >= $1
            ORDER BY intensity_score DESC
            LIMIT 200
            """,
            float(floor),
        )
        # The covered set: every active thematic target's predicate text. A
        # situation whose salient term appears here already has a frame.
        cov = await conn.fetch(
            """
            SELECT body->'scope'->>'predicate' AS pred
            FROM target_descriptors
            WHERE is_head AND state = 'active'
              AND body->'scope'->>'domain' = 'thematic'
            """
        )
    covered_text = " ".join((r["pred"] or "").casefold() for r in cov)
    return _build_proposals([dict(r) for r in rows], covered_text, floor=floor)


async def _last_emitted_body(pool: Any, analyst_id: str) -> str | None:
    """Body of the most recent FEED finding this analyst emitted (or None).

    Trace-only suppressed runs write no ``analyst_outputs`` row, so this is the
    last NON-suppressed proposal — exactly what a re-proposal should be deduped
    against.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT body FROM analyst_outputs "
            "WHERE analyst_id = $1 AND kind = 'finding' "
            "ORDER BY produced_at DESC LIMIT 1",
            analyst_id,
        )
    return row["body"] if row else None


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see the module docstring.

    ``deps`` is the analyst pool bundle (``deps.pg_pool``); ``deps=None`` runs
    the synthetic path (groups pre-shaped ``inputs`` rows, no DB) for unit tests.
    """
    floor = min_intensity()
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        try:
            proposals = await _resolve_pool(pool, floor=floor)
        except Exception as exc:  # noqa: BLE001 — degrade-not-drop
            logger.warning("thematic_proposal.pool_failed err=%s", exc)
            proposals = []
    else:
        # Synthetic: inputs are pre-shaped situation rows; no covered set.
        proposals = _build_proposals(
            [dict(r) for r in inputs], covered_text="", floor=floor,
        )

    finding = _build_finding(proposals)
    # Emit a FEED finding only when there is something NEW to surface. The
    # convergence loop RE-LISTS the same uncovered situations every cadence tick
    # until the operator registers them, so repeating the identical proposal
    # finding into the feed is noise. Suppress (trace-only) when there are no
    # candidates, or when the proposal set is unchanged from the last EMITTED
    # finding (the body is deterministic from the proposal set, so a body match
    # == an unchanged set). Degrade to emit on any dedup-check failure.
    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    force_trace_only = not proposals
    if proposals and pool is not None:
        try:
            force_trace_only = (
                await _last_emitted_body(pool, analyst_id) == finding.body
            )
        except Exception as exc:  # noqa: BLE001 — degrade: emit rather than crash
            logger.warning("thematic_proposal.dedup_check_failed err=%s", exc)
            force_trace_only = False
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        force_trace_only=force_trace_only,
    )
