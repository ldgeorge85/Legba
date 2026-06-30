# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``fact_contention_arbiter`` sub-handler — Holes-B Wave 2 (DETECT-ONLY).

The contested-claims referee (#101). When two credible sources disagree on a
``(subject, predicate)`` value, both rows already coexist OPEN (the open-triple
unique index keys on ``lower(value)``), but the dispute is invisible at the fact
layer. This handler builds the first-class, recomputable contention sidecar
(``fact_contention`` + ``fact_contention_values``), scores each value cluster
with a deterministic ``Q·C·R·F`` function, surfaces at most one winner per group
(or abstains on a near-tie), and stamps the thin ``facts`` markers
(``contested`` / ``contention_id`` / ``surfaced_winner``).

**INVARIANT B15 — DETECT-ONLY.** This handler NEVER closes, supersedes, deletes,
or rewrites a ``facts`` row: it touches ZERO of ``valid_until`` /
``superseded_by`` / ``value`` / ``confidence``, and never calls
``supersede_prior_facts``. The only ``facts`` writes are the three marker columns
(``contested`` / ``contention_id`` / ``surfaced_winner``); everything else lands
in the sidecar tables. The write-path coexistence change is a separate,
flag-gated Wave 4 — not here.

Pipeline (idempotent, safe to re-run hourly; the scan IS the backfill):
  1. Scan OPEN facts (``valid_until IS NULL AND superseded_by IS NULL``), grouped
     by ``(lower(subject), normalize_predicate(lower(predicate)))``.
  2. Cluster each group's values with the shared fuzzy clusterer (Wave 3 —
     canon + tight normalized-Levenshtein), so "Russia"/"Russian" and
     "Kyiv"/"Kiev" are ONE value, not two.
  3. JUNK-GATE every cluster by reusing the ``fact_extractor`` gates
     (``is_junk_entity`` / ``_is_inverted_relation`` / ``_is_reflexive_after_canon``
     / ``_is_nongeo_containment_inversion``). A junk cluster is EXCLUDED from the
     dispute and recorded with ``is_junk=true`` + ``junk_reason``
     (OPERATOR-REPORTABLE — never silently dropped). The live
     Poland -> {Berlin, Russian} ``located in`` case junk-gates out (both fail the
     inverted/demonym gates) -> no genuine contention opened.
  4. For a ``(subject, predicate)`` with >= 2 NON-junk fuzzy-distinct clusters:
     upsert the ``fact_contention`` group + per-cluster value rows recomputed from
     the open facts (``source_credibility_sum``, ``distinct_source_count``,
     ``confidence_max/mean``, ``source_types``, ``supporting_fact_ids``,
     ``latest_asserted_at``). Score each cluster ``Q·C·R·F``, surface exactly one
     winner iff it clears ``MIN_SURFACE_SCORE`` AND beats the runner-up by
     ``DOMINANCE_RATIO`` (else ABSTAIN — an honest "disputed, no resolution").
  5. COLLAPSE a group (status ``collapsed``, markers cleared) when it drops to
     < 2 non-junk clusters.

Output ``data`` keys (the cadence receipt the operator reads):
    groups_open       int — contention groups currently contested/surfaced
    groups_collapsed  int — groups collapsed this pass (dropped below 2 clusters)
    values_total      int — non-junk value clusters across open groups
    abstained         int — open groups where the arbiter surfaced NO winner
    junk_excluded     int — junk clusters recorded (is_junk=true), operator-reportable
    llm_tiebreaks     int — near-tie abstains the LLM tie-break RESOLVED this pass
                            (Wave 2b — 0 unless ``LEGBA_FACT_CONTENTION_LLM_TIEBREAK``
                            is set AND a vLLM handler is wired)

Wave 2b — LLM tie-break on ABSTAIN (decision #2). The deterministic ``Q·C·R·F``
arbiter stays the default. There are TWO abstain causes at :func:`_select_winner`:
(1) the best cluster is genuinely WEAK (``best_score < MIN_SURFACE_SCORE``) — the
LLM is NEVER consulted, the abstain stands; (2) a NEAR-TIE between >= 2 non-junk
clusters that BOTH clear ``MIN_SURFACE_SCORE`` but neither dominates the other by
``DOMINANCE_RATIO`` — the ONLY case the LLM may break. When
``LEGBA_FACT_CONTENTION_LLM_TIEBREAK`` is set (default OFF) AND deps carry a vLLM
handler (the SELF-HOSTED ``llm.primary.openai_compat`` plane — NEVER Anthropic /
Opus, that plane is consult/deep only), a BOUNDED, strictly-parsed call picks ONE
``value_key`` OR ABSTAIN. Any failure / timeout / unparsable / llm-unavailable
result DEGRADES to abstain (mirrors the ACH per-cell lexical fallback). Bounded:
per-call token cap + timeout, and at most ``MAX_LLM_TIEBREAKS`` calls per pass.

DETECT-ONLY (B15) holds for the tie-break too: an LLM-chosen winner is surfaced
through the SAME sidecar + marker path (status surfaced, ``surfaced_winner``); it
NEVER touches a fact ``value`` / ``valid_until`` / ``superseded_by`` /
``confidence``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from ..._entity_canon import is_junk_entity
from ...filters.fact_extractor import (
    _is_inverted_relation,
    _is_nongeo_containment_inversion,
    _is_reflexive_after_canon,
)
from ...provenance.models import FindingPayload
from ...provenance.value_clustering import cluster_values
from ...vocabulary import normalize_predicate
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

ARBITER_VERSION = "fact_contention_arbiter/1.0.0"

#: Surface gate (decisions B10/B11). The winner is surfaced only when its score
#: clears this floor AND dominates the runner-up by the ratio below; otherwise
#: the group stays contested with NO surfaced winner (abstain — an honest
#: deadlock, refined by the LLM tie-break in the later Wave 2b).
MIN_SURFACE_SCORE = 0.15
DOMINANCE_RATIO = 1.25

#: Wave 2b — LLM tie-break flag (decision #2). OFF by default: behavior is
#: byte-for-byte the deterministic Wave-2 arbiter (no LLM, current abstain). ON
#: only consults the LLM on a NEAR-TIE abstain (cause 2), never a weak abstain.
LLM_TIEBREAK_ENV = "LEGBA_FACT_CONTENTION_LLM_TIEBREAK"

#: Per-pass cap on LLM tie-break calls (bound: only abstaining near-tie groups
#: trigger one, and never more than this many in a single hourly pass).
MAX_LLM_TIEBREAKS = 10

#: Per-call output token cap for the tie-break (the answer is one value_key or
#: ABSTAIN — tiny). Bounded so a misconfigured handler can't run away.
LLM_TIEBREAK_MAX_TOKENS = 256

#: Per-call wall-clock timeout (seconds). On expiry the call is abandoned and the
#: group ABSTAINS (degrade-not-break).
LLM_TIEBREAK_TIMEOUT_SECONDS = 30.0

#: The key under which the runtime stashes the resolved vLLM handler on
#: ``StandardDeps.extras`` for the arbiter (wired in analyst_deps_builder iff the
#: flag is ON and the descriptor declares method.llm.primary).
LLM_DEPS_EXTRA_KEY = "fact_contention_llm"

#: Recency half-life (B9): a value last asserted ``HALFLIFE_DAYS`` ago scores
#: R=0.5; recency is ONE bounded factor in the multiplicative score, not the
#: sole decider (the core fix vs last-writer-by-recency).
HALFLIFE_DAYS = 30.0

#: Sanity bound on the open-fact scan (hourly cadence). The arbiter recomputes
#: from open rows every pass, so the bound only caps a pathological single-pass
#: cost; the next pass picks up anything skipped.
MAX_SCAN_FACTS = 200_000


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _group_keys(subject: str, predicate: str) -> tuple[str, str]:
    """The canonical ``(subject_key, predicate_key)`` for a fact triple."""
    subject_key = " ".join(str(subject or "").split()).strip().lower()
    predicate_key = normalize_predicate(str(predicate or "").strip().lower())
    return subject_key, predicate_key


def _junk_reason(subject: str, predicate: str, value: str) -> str | None:
    """Return the name of the FIRST ``fact_extractor`` gate that rejects this
    ``(subject, predicate, value)`` triple, else ``None``.

    Reuses the existing, battle-tested extractor gates verbatim (does NOT
    reimplement them). Order is the cheapest/most-specific first; the returned
    label is operator-reportable (it explains WHY a value was excluded from the
    dispute rather than silently dropping it).
    """
    if is_junk_entity(value) or is_junk_entity(subject):
        return "junk_entity"
    if _is_reflexive_after_canon(subject, value):
        return "reflexive_after_canon"
    if _is_inverted_relation(subject, predicate, value):
        return "inverted_relation"
    if _is_nongeo_containment_inversion(subject, predicate, value):
        return "nongeo_containment_inversion"
    return None


def _safe_div(num: float, den: float) -> float:
    return (num / den) if den > 0 else 0.0


def _quorum(distinct_source_count: int, max_distinct_in_group: int) -> float:
    """``Q`` — log-damped distinct-source count, normalized within the group.

    ``log(1 + n) / log(1 + max_n)`` so the most-corroborated value scores 1.0 and
    the 5th source adds less than the 2nd (diminishing returns). Counts DISTINCT
    lineage, not rows, so a single chatty source can't manufacture quorum.
    """
    if max_distinct_in_group <= 0:
        return 0.0
    return _safe_div(
        math.log1p(max(distinct_source_count, 0)),
        math.log1p(max_distinct_in_group),
    )


def _credibility_share(cred_sum: float, group_cred_total: float) -> float:
    """``C`` — this value's SHARE of the group's total credibility mass."""
    return _safe_div(cred_sum, group_cred_total)


def _recency(latest_asserted_at: datetime | None, now: datetime) -> float:
    """``R`` — exponential half-life decay on the value's latest assertion."""
    if latest_asserted_at is None:
        return 0.0
    age_days = max((now - latest_asserted_at).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / HALFLIFE_DAYS)


def _arbiter_score(q: float, c: float, r: float, f: float) -> float:
    """``Q·C·R·F`` — multiplicative so a zero on any axis kills the value (AND
    semantics: no credible source / no recent assertion / no confidence each
    veto a win). All factors are already normalized to ``[0, 1]``."""
    return q * c * r * f


class _ValueAgg:
    """Per-cluster aggregation recomputed from the open facts of one value group."""

    __slots__ = (
        "value_key", "representative_fact_id", "representative_value",
        "distinct_lineage", "supporting_fact_ids", "source_types",
        "cred_sum", "confidence_sum", "confidence_max", "latest_asserted_at",
        "row_count",
    )

    def __init__(self, value_key: str) -> None:
        self.value_key = value_key
        self.representative_fact_id: UUID | None = None
        self.representative_value: str = ""
        self.distinct_lineage: set[str] = set()
        self.supporting_fact_ids: list[UUID] = []
        self.source_types: set[str] = set()
        self.cred_sum: float = 0.0
        self.confidence_sum: float = 0.0
        self.confidence_max: float = 0.0
        self.latest_asserted_at: datetime | None = None
        self.row_count: int = 0

    def add(self, row: Mapping[str, Any]) -> None:
        fid = row["id"]
        # The representative is the most-recently-asserted row (the keeper/anchor).
        produced_at = row.get("produced_at")
        if self.representative_fact_id is None or (
            produced_at is not None
            and self.latest_asserted_at is not None
            and produced_at > self.latest_asserted_at
        ) or self.latest_asserted_at is None:
            self.representative_fact_id = fid
            self.representative_value = row.get("value") or ""
        self.supporting_fact_ids.append(fid)
        self.row_count += 1
        st = row.get("source_type")
        if st:
            self.source_types.add(str(st))
        # distinct_source_count := distinct lineage (derived_from signal/fact ids),
        # falling back to the distinct fact-row id when a row has no lineage — so a
        # single chatty source (one lineage, many rows) counts ONCE, but two
        # lineage-less rows still count as two distinct sources.
        derived = row.get("derived_from") or []
        if derived:
            for ref in derived:
                self.distinct_lineage.add(str(ref))
        else:
            self.distinct_lineage.add(f"fact:{fid}")
        # source_credibility: SUM of non-NULL only (NULL = UNKNOWN, never 0).
        cred = row.get("source_credibility")
        if cred is not None:
            self.cred_sum += float(cred)
        conf = float(row.get("confidence") or 0.0)
        self.confidence_sum += conf
        self.confidence_max = max(self.confidence_max, conf)
        if produced_at is not None and (
            self.latest_asserted_at is None or produced_at > self.latest_asserted_at
        ):
            self.latest_asserted_at = produced_at

    @property
    def distinct_source_count(self) -> int:
        return len(self.distinct_lineage)

    @property
    def confidence_mean(self) -> float:
        return _safe_div(self.confidence_sum, float(self.row_count))


async def _open_triples(conn: Any) -> list[Mapping[str, Any]]:
    """Fetch the open facts of every ``(subject, predicate)`` that has >= 2 open
    rows (a single open row can't be contested) — the candidate set for grouping.

    Bounded scan; orders so each group's rows are contiguous. ``produced_at`` and
    ``derived_from`` feed recency + distinct-lineage; ``source_credibility`` feeds
    the credibility share (NULL-safe at the aggregator)."""
    return await conn.fetch(
        f"""
        WITH open_facts AS (
            SELECT id, subject, predicate, value, confidence, source_type,
                   source_credibility, produced_at, derived_from
              FROM facts
             WHERE valid_until IS NULL
               AND superseded_by IS NULL
            LIMIT {MAX_SCAN_FACTS}
        ),
        grouped AS (
            SELECT lower(btrim(subject)) AS subject_key,
                   lower(btrim(predicate)) AS predicate_raw,
                   count(*) AS n
              FROM open_facts
             GROUP BY 1, 2
            HAVING count(*) >= 2
        )
        SELECT f.*
          FROM open_facts f
          JOIN grouped g
            ON lower(btrim(f.subject)) = g.subject_key
           AND lower(btrim(f.predicate)) = g.predicate_raw
         ORDER BY lower(btrim(f.subject)), lower(btrim(f.predicate)), f.id
        """
    )


def _bucket_rows(rows: list[Mapping[str, Any]]) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    """Bucket the candidate open rows by canonical ``(subject_key, predicate_key)``.

    Note the SQL groups on ``lower(predicate)`` but the canonical key applies
    ``normalize_predicate`` (predicate synonyms collapse), so two raw predicates
    can land in one canonical bucket here — intentional, it widens the dispute
    correctly."""
    buckets: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = _group_keys(row["subject"], row["predicate"])
        buckets.setdefault(key, []).append(row)
    return buckets


def _aggregate_group(
    rows: list[Mapping[str, Any]],
) -> tuple[list[_ValueAgg], list[tuple[_ValueAgg, str]]]:
    """Cluster a group's rows into value clusters and split non-junk vs junk.

    Returns ``(non_junk_aggs, junk_aggs)`` where ``junk_aggs`` carries the
    rejecting gate name per cluster (operator-reportable)."""
    values = [row.get("value") or "" for row in rows]
    clusters = cluster_values(values)
    non_junk: list[_ValueAgg] = []
    junk: list[tuple[_ValueAgg, str]] = []
    for cluster in clusters:
        agg = _ValueAgg(cluster.key)
        for member_idx in cluster.members:
            agg.add(rows[member_idx])
        # Junk-gate on the cluster's representative (subject, predicate, value).
        rep_row = rows[cluster.members[0]]
        reason = _junk_reason(
            rep_row.get("subject") or "",
            rep_row.get("predicate") or "",
            agg.representative_value,
        )
        if reason is not None:
            junk.append((agg, reason))
        else:
            non_junk.append(agg)
    return non_junk, junk


def _score_group(aggs: list[_ValueAgg], now: datetime) -> dict[str, float]:
    """Compute the ``Q·C·R·F`` score for every non-junk cluster, keyed by value_key."""
    max_distinct = max((a.distinct_source_count for a in aggs), default=0)
    group_cred_total = sum(a.cred_sum for a in aggs)
    scores: dict[str, float] = {}
    for agg in aggs:
        q = _quorum(agg.distinct_source_count, max_distinct)
        c = _credibility_share(agg.cred_sum, group_cred_total)
        r = _recency(agg.latest_asserted_at, now)
        f = agg.confidence_mean
        scores[agg.value_key] = _arbiter_score(q, c, r, f)
    return scores


def _select_winner(
    aggs: list[_ValueAgg], scores: dict[str, float]
) -> _ValueAgg | None:
    """Apply the abstain gate (B10/B11) + the deterministic tie-break (B12).

    Returns the surfaced-winner agg, or ``None`` to ABSTAIN. Idempotent: the
    total-order tie-break (distinct-source, credibility, recency, value_key ASC)
    makes two passes over unchanged data pick the same winner."""
    if not aggs:
        return None
    ordered = sorted(
        aggs,
        key=lambda a: (
            scores.get(a.value_key, 0.0),
            a.distinct_source_count,
            a.cred_sum,
            a.latest_asserted_at or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    # Stable secondary key for exact-score ties: value_key ASC (so reverse=True
    # above does not invert it — re-sort the tied head).
    best = ordered[0]
    best_score = scores.get(best.value_key, 0.0)
    runner_up_score = scores.get(ordered[1].value_key, 0.0) if len(ordered) > 1 else 0.0
    # Resolve an exact-score tie at the top by value_key ASC (total, reproducible).
    tied = [a for a in aggs if abs(scores.get(a.value_key, 0.0) - best_score) <= 1e-9]
    if len(tied) > 1:
        best = sorted(
            tied,
            key=lambda a: (
                -a.distinct_source_count,
                -a.cred_sum,
                -(a.latest_asserted_at or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
                a.value_key,
            ),
        )[0]
    if best_score < MIN_SURFACE_SCORE:
        return None
    if runner_up_score > 0 and best_score < DOMINANCE_RATIO * runner_up_score:
        return None
    return best


def _abstain_cause(aggs: list[_ValueAgg], scores: dict[str, float]) -> str | None:
    """Classify WHY :func:`_select_winner` abstained (or ``None`` when it didn't).

    Mirrors the two abstain gates of :func:`_select_winner` EXACTLY (the same
    floor + dominance comparisons) so the cause is authoritative, not re-derived:

      * ``None``       — there IS a deterministic winner (no abstain).
      * ``"weak"``     — the best cluster is below ``MIN_SURFACE_SCORE`` (cause 1).
                          The LLM is NEVER consulted here — the dispute is
                          genuinely thin, surfacing anything would be noise.
      * ``"near_tie"`` — both top clusters clear ``MIN_SURFACE_SCORE`` but the
                          best fails to dominate the runner-up by
                          ``DOMINANCE_RATIO`` (cause 2). The ONLY case Wave 2b's
                          LLM tie-break may run.
    """
    if not aggs:
        return None
    ordered = sorted(
        (scores.get(a.value_key, 0.0) for a in aggs), reverse=True,
    )
    best_score = ordered[0]
    runner_up_score = ordered[1] if len(ordered) > 1 else 0.0
    if best_score < MIN_SURFACE_SCORE:
        return "weak"
    if runner_up_score > 0 and best_score < DOMINANCE_RATIO * runner_up_score:
        return "near_tie"
    return None


# ---------------------------------------------------------------------------
# Wave 2b — bounded LLM tie-break on a NEAR-TIE abstain (decision #2)
# ---------------------------------------------------------------------------


def _llm_tiebreak_enabled() -> bool:
    """``LEGBA_FACT_CONTENTION_LLM_TIEBREAK`` truthy? (default OFF).

    OFF → the arbiter is byte-for-byte the deterministic Wave-2 handler: no LLM,
    the near-tie abstain stands.
    """
    return os.getenv(LLM_TIEBREAK_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


_TIEBREAK_SYSTEM_PROMPT = (
    "You are a deterministic intelligence-analysis referee resolving a NEAR-TIE "
    "between competing claimed values for one (subject, predicate). The candidates "
    "are statistically too close for the rule-based scorer to separate. Weigh "
    "distinct-source corroboration, source credibility share, recency, source "
    "types, and the representative phrasings. Pick the SINGLE best-supported "
    "value_key, or ABSTAIN if they remain a genuine tie. Reply with ONE JSON "
    "object and nothing else: {\"winner\": \"<value_key>\"} or {\"winner\": "
    "\"ABSTAIN\"}. Never invent a value_key not listed."
)


def _build_tiebreak_prompt(
    subject_key: str,
    predicate_key: str,
    aggs: list[_ValueAgg],
    scores: dict[str, float],
    now: datetime,
) -> str:
    """Render the competing clusters + their evidence as a strict tie-break prompt.

    Gives the model exactly the deterministic factors (distinct_source_count,
    credibility share, recency, source_types, representative value) so its call is
    grounded in the SAME evidence the scorer used — not free-form speculation."""
    group_cred_total = sum(a.cred_sum for a in aggs)
    lines: list[str] = [
        f"subject: {subject_key}",
        f"predicate: {predicate_key}",
        "",
        "Candidate values (value_key — evidence):",
    ]
    for agg in aggs:
        cred_share = _credibility_share(agg.cred_sum, group_cred_total)
        recency = _recency(agg.latest_asserted_at, now)
        types = ", ".join(sorted(agg.source_types)) or "unknown"
        lines.append(
            f"- {agg.value_key!r}: representative={agg.representative_value!r}; "
            f"distinct_source_count={agg.distinct_source_count}; "
            f"credibility_share={cred_share:.3f}; recency={recency:.3f}; "
            f"confidence_mean={agg.confidence_mean:.3f}; source_types=[{types}]; "
            f"score={scores.get(agg.value_key, 0.0):.4f}"
        )
    lines.append("")
    lines.append(
        'Reply with ONE JSON object: {"winner": "<value_key>"} '
        'or {"winner": "ABSTAIN"}.'
    )
    return "\n".join(lines)


def _parse_tiebreak_winner(
    raw: str, valid_keys: set[str]
) -> str | None:
    """STRICTLY parse the tie-break reply to ONE listed value_key, else ``None``.

    Returns ``None`` (→ ABSTAIN) on any deviation: no JSON object, missing
    ``winner``, the literal ``ABSTAIN``, or a value_key not in ``valid_keys`` (no
    hallucinated value can ever be surfaced)."""
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
        ch = candidate[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        obj = json.loads(candidate[start:end])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    winner = obj.get("winner")
    if not isinstance(winner, str):
        return None
    winner = winner.strip()
    if not winner or winner.upper() == "ABSTAIN":
        return None
    return winner if winner in valid_keys else None


async def _llm_tiebreak(
    llm: Any,
    subject_key: str,
    predicate_key: str,
    aggs: list[_ValueAgg],
    scores: dict[str, float],
    now: datetime,
) -> _ValueAgg | None:
    """Bounded, strictly-parsed LLM tie-break for ONE near-tie group.

    Returns the chosen winner agg, or ``None`` to ABSTAIN. Degrade-not-break: any
    timeout / exception / unparsable / unlisted result yields ``None`` (the
    near-tie abstain stands), mirroring the ACH per-cell lexical fallback. The
    call is single-turn, temperature 0, output-capped, and wrapped in a per-call
    wall-clock timeout. DETECT-ONLY: this only SELECTS a winner — the caller still
    surfaces it through the same sidecar + marker path."""
    if llm is None or len(aggs) < 2:
        return None
    by_key = {a.value_key: a for a in aggs}
    prompt = _build_tiebreak_prompt(subject_key, predicate_key, aggs, scores, now)
    try:
        response = await asyncio.wait_for(
            llm.chat_complete(
                [{"role": "user", "content": prompt}],
                max_tokens=LLM_TIEBREAK_MAX_TOKENS,
                temperature=0.0,
                system=_TIEBREAK_SYSTEM_PROMPT,
            ),
            timeout=LLM_TIEBREAK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "fact_contention_arbiter.tiebreak_timeout subject=%r predicate=%r",
            subject_key, predicate_key,
        )
        return None
    except Exception as exc:  # degrade-not-break: any handler failure → abstain
        logger.warning("fact_contention_arbiter.tiebreak_failed err=%s", exc)
        return None
    content = getattr(response, "content", "") or ""
    winner_key = _parse_tiebreak_winner(content, set(by_key))
    if winner_key is None:
        return None
    return by_key.get(winner_key)


# ---------------------------------------------------------------------------
# Sidecar persistence (DETECT-ONLY: facts writes are markers only)
# ---------------------------------------------------------------------------


async def _upsert_group(
    conn: Any, subject_key: str, predicate_key: str
) -> UUID:
    """Upsert the ``fact_contention`` group, returning its id (idempotent)."""
    return await conn.fetchval(
        """
        INSERT INTO fact_contention (subject_key, predicate_key, status, updated_at)
        VALUES ($1, $2, 'contested', now())
        ON CONFLICT (subject_key, predicate_key) DO UPDATE
           SET updated_at = now()
        RETURNING id
        """,
        subject_key,
        predicate_key,
    )


async def _replace_group_values(
    conn: Any,
    contention_id: UUID,
    non_junk: list[_ValueAgg],
    junk: list[tuple[_ValueAgg, str]],
    scores: dict[str, float],
    winner: _ValueAgg | None,
) -> None:
    """Recompute the group's value rows from open facts (delete-then-insert).

    Delete-then-insert (within the run's transaction) keeps the sidecar EXACTLY
    recomputable from the current open rows — a value that aged out leaves no
    stale row. This writes ONLY the sidecar, never a ``facts`` data column."""
    await conn.execute(
        "DELETE FROM fact_contention_values WHERE contention_id = $1",
        contention_id,
    )
    for agg in non_junk:
        await conn.execute(
            """
            INSERT INTO fact_contention_values (
                contention_id, value_key, representative_fact_id,
                distinct_source_count, source_credibility_sum,
                confidence_max, confidence_mean, source_types,
                supporting_fact_ids, latest_asserted_at, arbiter_score,
                surfaced_winner, is_junk, junk_reason, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, false, NULL, now()
            )
            """,
            contention_id,
            agg.value_key,
            agg.representative_fact_id,
            agg.distinct_source_count,
            agg.cred_sum,
            agg.confidence_max,
            agg.confidence_mean,
            sorted(agg.source_types),
            agg.supporting_fact_ids,
            agg.latest_asserted_at,
            scores.get(agg.value_key, 0.0),
            winner is not None and agg.value_key == winner.value_key,
        )
    for agg, reason in junk:
        await conn.execute(
            """
            INSERT INTO fact_contention_values (
                contention_id, value_key, representative_fact_id,
                distinct_source_count, source_credibility_sum,
                confidence_max, confidence_mean, source_types,
                supporting_fact_ids, latest_asserted_at, arbiter_score,
                surfaced_winner, is_junk, junk_reason, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NULL, false, true, $11, now()
            )
            """,
            contention_id,
            agg.value_key,
            agg.representative_fact_id,
            agg.distinct_source_count,
            agg.cred_sum,
            agg.confidence_max,
            agg.confidence_mean,
            sorted(agg.source_types),
            agg.supporting_fact_ids,
            agg.latest_asserted_at,
            reason,
        )


async def _finalize_group(
    conn: Any,
    contention_id: UUID,
    non_junk: list[_ValueAgg],
    winner: _ValueAgg | None,
) -> None:
    """Set the group's status/surfaced pointer + (re)stamp the ``facts`` markers.

    DETECT-ONLY: the ONLY ``facts`` columns written are ``contested`` /
    ``contention_id`` / ``surfaced_winner``. ``valid_until`` / ``superseded_by`` /
    ``value`` / ``confidence`` are NEVER touched here."""
    surfaced_value = winner.value_key if winner is not None else None
    surfaced_fact_id = winner.representative_fact_id if winner is not None else None
    status = "surfaced" if winner is not None else "contested"
    await conn.execute(
        """
        UPDATE fact_contention
           SET status = $2,
               surfaced_value = $3,
               surfaced_fact_id = $4,
               value_count = $5,
               resolved_at = now(),
               arbiter_version = $6,
               updated_at = now()
         WHERE id = $1
        """,
        contention_id,
        status,
        surfaced_value,
        surfaced_fact_id,
        len(non_junk),
        ARBITER_VERSION,
    )
    # Clear stale markers from any fact previously tied to this group but no
    # longer a member (e.g. aged out), then stamp the current members.
    member_ids = [
        fid for agg in non_junk for fid in agg.supporting_fact_ids
    ]
    await conn.execute(
        """
        UPDATE facts
           SET contested = false, contention_id = NULL, surfaced_winner = false,
               updated_at = now()
         WHERE contention_id = $1
           AND NOT (id = ANY($2::uuid[]))
        """,
        contention_id,
        member_ids,
    )
    winner_ids = (
        list(winner.supporting_fact_ids) if winner is not None else []
    )
    for agg in non_junk:
        await conn.execute(
            """
            UPDATE facts
               SET contested = true,
                   contention_id = $1,
                   surfaced_winner = (id = ANY($2::uuid[])),
                   updated_at = now()
             WHERE id = ANY($3::uuid[])
            """,
            contention_id,
            winner_ids,
            agg.supporting_fact_ids,
        )


async def _collapse_group(conn: Any, contention_id: UUID) -> None:
    """Collapse a group that dropped below 2 non-junk clusters (status collapsed,
    markers cleared on every member). The lone survivor becomes a normal open
    fact again — DETECT-ONLY: no value/validity change."""
    await conn.execute(
        """
        UPDATE facts
           SET contested = false, contention_id = NULL, surfaced_winner = false,
               updated_at = now()
         WHERE contention_id = $1
        """,
        contention_id,
    )
    await conn.execute("DELETE FROM fact_contention_values WHERE contention_id = $1", contention_id)
    await conn.execute(
        """
        UPDATE fact_contention
           SET status = 'collapsed', surfaced_value = NULL, surfaced_fact_id = NULL,
               value_count = 0, junk_count = 0, resolved_at = now(),
               arbiter_version = $2, updated_at = now()
         WHERE id = $1
        """,
        contention_id,
        ARBITER_VERSION,
    )


async def _run_arbiter(pool: Any, llm: Any | None = None) -> dict[str, int]:
    """One full arbiter pass over the open facts. Idempotent.

    ``llm`` — the OPTIONAL self-hosted vLLM tie-break handler (Wave 2b). When it
    is ``None`` (flag off / not wired) the pass is byte-for-byte the deterministic
    Wave-2 arbiter. When present, a NEAR-TIE abstain (cause 2) may be resolved by
    a bounded LLM call, capped at ``MAX_LLM_TIEBREAKS`` per pass. A WEAK abstain
    (cause 1) NEVER calls the LLM."""
    counts = {
        "groups_open": 0,
        "groups_collapsed": 0,
        "values_total": 0,
        "abstained": 0,
        "junk_excluded": 0,
        "llm_tiebreaks": 0,
    }
    now = _now()
    llm_tiebreaks_left = MAX_LLM_TIEBREAKS if llm is not None else 0
    async with pool.acquire() as conn:
        rows = await _open_triples(conn)
        buckets = _bucket_rows(list(rows))
        live_keys: set[tuple[str, str]] = set()
        for (subject_key, predicate_key), group_rows in buckets.items():
            non_junk, junk = _aggregate_group(group_rows)
            counts["junk_excluded"] += len(junk)
            if len(non_junk) < 2:
                # Not a genuine dispute (all-but-one value is junk, or a single
                # clustered value). Collapse any pre-existing group for this
                # triple; otherwise nothing to open.
                existing = await conn.fetchval(
                    "SELECT id FROM fact_contention WHERE subject_key = $1 AND predicate_key = $2",
                    subject_key,
                    predicate_key,
                )
                if existing is not None:
                    await _collapse_group(conn, existing)
                    counts["groups_collapsed"] += 1
                continue
            live_keys.add((subject_key, predicate_key))
            scores = _score_group(non_junk, now)
            winner = _select_winner(non_junk, scores)
            # Wave 2b — LLM tie-break on a NEAR-TIE abstain ONLY (decision #2). A
            # WEAK abstain (cause 1) is left alone; a genuine deterministic winner
            # is never second-guessed. Bounded by MAX_LLM_TIEBREAKS per pass.
            if (
                winner is None
                and llm is not None
                and llm_tiebreaks_left > 0
                and _abstain_cause(non_junk, scores) == "near_tie"
            ):
                llm_tiebreaks_left -= 1
                llm_winner = await _llm_tiebreak(
                    llm, subject_key, predicate_key, non_junk, scores, now,
                )
                if llm_winner is not None:
                    winner = llm_winner
                    counts["llm_tiebreaks"] += 1
            contention_id = await _upsert_group(conn, subject_key, predicate_key)
            await _replace_group_values(conn, contention_id, non_junk, junk, scores, winner)
            await conn.execute(
                "UPDATE fact_contention SET junk_count = $2 WHERE id = $1",
                contention_id,
                len(junk),
            )
            await _finalize_group(conn, contention_id, non_junk, winner)
            counts["groups_open"] += 1
            counts["values_total"] += len(non_junk)
            if winner is None:
                counts["abstained"] += 1

        # Collapse any standing group whose triple no longer appears as a live
        # >=2-cluster dispute (its values converged / aged out).
        stale = await conn.fetch(
            "SELECT id, subject_key, predicate_key FROM fact_contention WHERE status <> 'collapsed'"
        )
        for srow in stale:
            if (srow["subject_key"], srow["predicate_key"]) not in live_keys:
                await _collapse_group(conn, srow["id"])
                counts["groups_collapsed"] += 1
    return counts


def _build_finding(counts: Mapping[str, int], target_id: str | None) -> FindingPayload:
    title = (
        f"Fact contention: {counts['groups_open']} open, "
        f"{counts['abstained']} abstained, {counts['junk_excluded']} junk-excluded"
    )
    if target_id:
        title = f"{title} for {target_id}"
    body = (
        f"groups_open={counts['groups_open']}\n"
        f"groups_collapsed={counts['groups_collapsed']}\n"
        f"values_total={counts['values_total']}\n"
        f"abstained={counts['abstained']}\n"
        f"junk_excluded={counts['junk_excluded']}\n"
        f"llm_tiebreaks={counts.get('llm_tiebreaks', 0)}"
    )
    tags = ["deterministic", "fact_contention_arbiter", "detect_only"]
    if counts["groups_open"]:
        tags.append("contention_open")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": "fact_contention_arbiter", **dict(counts)},
    )


def _resolve_tiebreak_llm(deps: Any | None) -> Any | None:
    """Pull the self-hosted vLLM tie-break handler off ``deps.extras`` iff the
    Wave-2b flag is ON.

    Returns ``None`` (no tie-break) when the flag is OFF, ``deps`` is absent, or
    no handler was wired — keeping the off-path behavior byte-for-byte
    deterministic. The handler is injected by
    :func:`legba.runtime.analyst_deps_builder._build_deterministic` ONLY when the
    flag is set AND the descriptor declares ``method.llm.primary`` (the
    ``llm.primary.openai_compat`` plane)."""
    if deps is None or not _llm_tiebreak_enabled():
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
    """Sub-handler entry point — see module docstring. DETECT-ONLY (B15)."""
    counts = {
        "groups_open": 0,
        "groups_collapsed": 0,
        "values_total": 0,
        "abstained": 0,
        "junk_excluded": 0,
        "llm_tiebreaks": 0,
    }
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        llm = _resolve_tiebreak_llm(deps)
        try:
            counts = await _run_arbiter(pool, llm)
        except Exception as exc:  # pragma: no cover - defensive cadence guard
            logger.warning("fact_contention_arbiter.run_failed err=%s", exc)

    finding = _build_finding(counts, options.get("target_id"))
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
