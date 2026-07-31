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
from uuid import UUID

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


#: M21 (2026-07-06 mining audit) — ABSENCE / negation FRAMING. situation_clustering
#: names a frame from its latest member finding, and an "all-clear" assessment
#: ("United States – No discernible instability vector", "no observable WMD
#: activity", "No coordinated narrative detected") frames the ABSENCE of a
#: situation. Because ``intensity_score`` tracks prose VERBOSITY, these verbose
#: null-findings rank at the TOP of the proposal list — a misleading decision aid
#: (proposing a dedicated thematic target for a NON-situation). An absence-FRAMING
#: token marks the frame absence-framed and EXCLUDES it from candidacy.
#:
#: F3 (2026-07-06 review): match the absence-FRAMING construction, NOT a bare
#: negation token. "no" / "not" fire ONLY when followed by whitespace ("no
#: discernible …", "no evidence of …", "not detected") — a HYPHENATED compound
#: ("no-first-use policy", "no-fly zone") is a SUBSTANTIVE posture name, not
#: absence. The remaining absence words ("absence", "negligible", "without",
#: "neither", …) are matched as whole words. Conservative: a substantive frame
#: ("Turkey – State Repression", "China nuclear-capable delivery build-up",
#: "Norway sovereign-fund leverage") carries no absence framing and is unaffected.
_ABSENCE_RE = re.compile(
    r"""
      \b(?:no|not)(?=\s)                          # "no discernible", "not detected"
                                                  #   — NOT "no-fly"/"no-first-use"
    | \b(?:none|never|neither|nothing|nil|without
          |absence|absent|negligible|nonexistent|non-existent
          |lack|lacks|lacking)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_absence_framed(name: str) -> bool:
    """True when a situation name frames the ABSENCE of a situation (M21 / F3).

    Matches an absence-FRAMING construction: a "no"/"not" DETERMINER followed by
    whitespace ("No observable …", "no discernible …", "not detected"), or a
    whole-word absence token ("absence of …", "negligible …", "neither target nor
    wielder", "without …"). A HYPHENATED "no-…"/"not-…" compound
    ("no-first-use policy", "no-fly zone") is a substantive posture NAME and is
    NOT flagged. Whole-word matching also means a real referent that merely
    CONTAINS the letters ("Norway", "Kosovo", "Monaco") is never spuriously
    flagged.
    """
    return bool(_ABSENCE_RE.search(str(name or "")))


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


def stable_slug(sig: str, name: str) -> str:
    """A STABLE ``suggested_target_id`` for a situation (M21).

    The OLD slug keyed off ``terms[0]`` — the situation name's longest keyword —
    which is VOLATILE: situation_clustering re-frames a situation from its latest
    member finding every tick, so one France situation surfaced as
    ``situation_instability_2ba74f`` / ``situation_observable_2ba74f`` /
    ``situation_leadership_2ba74f`` across runs — a non-deterministic identity the
    convergence loop can never settle. The slug now derives ONLY from the stable
    ``situation_signature`` (e.g. ``sig:country_g20_fr`` → ``situation_country_g20_fr``),
    so one situation maps to exactly ONE slug regardless of its current framing.
    Falls back to a stable hash of the name when the signature is empty.
    """
    key = str(sig or "").strip()
    if key:
        # Drop a leading "sig:"/"sig_" marker, then sanitize to a slug token.
        base = re.sub(r"^sig[:_]?", "", key.casefold())
        base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
        if base:
            return f"situation_{base}"[:128]
    import hashlib
    h = hashlib.sha1((str(name or "")).encode("utf-8")).hexdigest()[:12]
    return f"situation_{h}"


def _proposal(
    sig: str, name: str, intensity: float, terms: list[str], situation_id: Any = None,
) -> dict[str, Any]:
    return {
        # A2 (verify-path fix, 2026-07-31): the situations.id PK this proposal was
        # built from — a real, resolvable drill-target (never a fabricated ref).
        # None when the row carries no parseable id (the synthetic/unit-test
        # path); ``_build_finding`` skips those when assembling citations.
        "situation_id": str(situation_id) if situation_id is not None else None,
        "situation_signature": sig,
        "name": name[:512],
        "intensity_score": round(float(intensity), 4),
        "suggested_target_id": stable_slug(sig, name),
        "suggested_predicate": suggested_predicate(terms),
        "terms": terms,
    }


def _build_proposals(
    situations: list[dict[str, Any]], covered_text: str, *, floor: float,
) -> list[dict[str, Any]]:
    """Pure core: high-intensity OPEN situations not yet covered → proposals,
    most-intense first.

    M21 filters, applied in order: skip below the intensity floor; skip an
    ABSENCE/negation-framed frame (an "all-clear" null-finding is not a situation
    to give a target); skip an already-covered frame; then DEDUP on the stable
    slug so one situation (one signature) yields exactly one proposal even if the
    input carries multiple rows / re-framings for it."""
    out: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for s in sorted(
        situations, key=lambda r: float(r.get("intensity_score") or 0.0), reverse=True
    ):
        intensity = float(s.get("intensity_score") or 0.0)
        if intensity < floor:
            continue
        name = str(s.get("name") or "")
        # M21 (a): exclude absence/negation-framed compositions from candidacy.
        if is_absence_framed(name):
            continue
        terms = candidate_terms(name)
        if not terms or _is_covered(terms, covered_text):
            continue
        sig = str(s.get("situation_signature") or "")
        # M21 (b)+(c): dedup at the OUTPUT level on the STABLE (signature-derived)
        # slug — one situation = one proposal.
        slug = stable_slug(sig, name)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        out.append(_proposal(sig, name, intensity, terms, s.get("id")))
        if len(out) >= _MAX_PROPOSALS:
            break
    return out


def _proposal_uuid(p: Mapping[str, Any]) -> UUID | None:
    """The proposal's ``situation_id`` as a real :class:`UUID`, or ``None`` when
    absent/unparseable (the synthetic/unit-test path carries no id) — never a
    fabricated ref."""
    raw = p.get("situation_id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def proposal_lineage(proposals: list[dict[str, Any]]) -> list[UUID]:
    """The real situation UUIDs backing ``proposals``, de-duplicated, in order —
    the ``derived_from`` lineage for this run's finding (A2 verify-path fix)."""
    seen: set[UUID] = set()
    out: list[UUID] = []
    for p in proposals:
        uid = _proposal_uuid(p)
        if uid is not None and uid not in seen:
            seen.add(uid)
            out.append(uid)
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
    # A2 (verify-path fix, 2026-07-31): thematic_proposal HAS evidence rows (the
    # situations it read) — cite them directly instead of shipping citation-less
    # (the JUDGE_READOUT's #1 structural finding: this kind shipped 100%
    # citation-less). One entry per proposal with a resolvable situation id;
    # never fabricated (a row lacking an id is silently skipped).
    citations = [
        {
            "ref_kind": "situation",
            "ref_id": str(uid),
            "title": p.get("name"),
            "situation_signature": p.get("situation_signature"),
        }
        for p in proposals
        if (uid := _proposal_uuid(p)) is not None
    ]
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
            "citations": citations,
        },
    )


async def _resolve_pool(pool: Any, *, floor: float) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, situation_signature, name, intensity_score
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
        # A2 (verify-path fix): real lineage back to the cited situations (was
        # always empty — this finding previously carried NO derived_from at all).
        derived_from=proposal_lineage(proposals),
    )
