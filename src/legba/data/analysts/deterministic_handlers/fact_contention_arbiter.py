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
     / ``_is_nongeo_containment_inversion`` / ``_is_capital_metonymy``). ALL of
     them — a gate the fact plane enforces and this list omits is a class that
     re-forms here forever, which is exactly what CW-6's capital metonymy did
     until 2026-08-03. A junk cluster is EXCLUDED from the
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

P3-2 TAIL — soak -> weighted tie-break -> coexistence semantics -> surfacing
(migration 0097). The arbiter's abstain path grows a second, SOAK-GATED
deterministic layer plus caching + a full surface record:

  * **Soak gate** — an abstained group younger than
    ``LEGBA_CONTENTION_SURFACE_SOAK_HOURS`` (default 48h; ``0`` disables) is
    left ``contested`` untouched: evidence is still accumulating, no tie-break
    (weight OR LLM) runs. A Q·C·R·F-DECISIVE winner still surfaces immediately
    (that path is live-validated and dominance-gated already); the soak only
    delays the TIE-BREAK layers.
  * **Weighted tie-break** — past soak, each side gets the deterministic weight
    ``distinct_source_count + source-type diversity + source_credibility sum``
    (:func:`_tiebreak_weight`). The A6 layer-3 earned-track-record seam
    (:func:`_earned_track_record_weight`, P3-3) adds a bounded, damped per-side
    bonus for proven sources — OFF by default (``LEGBA_CONTENTION_EARNED_
    WEIGHT``=0 ⇒ byte-identical to the P3-2 formula), acyclicity-guarded (the
    record is recomputed live EXCLUDING the contention being decided, over
    disputes settled > lag ago; see source_track_record.py). When the
    best side dominates by ``LEGBA_CONTENTION_WEIGHT_RATIO`` (default 1.5) AND
    carries real corroboration (>= ``WEIGHT_TIEBREAK_MIN_SOURCES`` distinct
    sources), it surfaces DETERMINISTICALLY — accumulated corroboration decides,
    no LLM. Applies to BOTH abstain causes (a stale-but-asymmetric dispute is
    exactly the "corroboration accumulated on one side" case).
  * **LLM tie-break (near-tie only, cached)** — the gray zone (near-tie by
    score, weight ratio indecisive) may consult the bounded LLM as before, but
    verdicts are now CACHED per ``(contention, evidence-fingerprint)`` in
    ``fact_contention_tiebreak`` (verdict + justification + model id recorded,
    entity_researcher adjudication pattern) so an unchanged question is never
    re-asked. Transport failures degrade to abstain and are NOT cached.
  * **Coexistence record** — a surfaced winner stamps ``surfaced_by``
    (``deterministic`` | ``llm``) + ``surfaced_at`` + ``surface_rationale`` on
    the group; the stamp is STABLE across passes while the decision stands.
    When the decision changes (winner flips, group re-opens, or collapses) the
    prior record is APPENDED to ``surface_history`` (newest first, capped) —
    the loser fact rows are never touched.
  * **Reversibility** — the decision is recomputed every pass from the open
    rows: new contradicting evidence changes the weights AND the evidence
    fingerprint, so a stale verdict cannot stick — the group re-opens (status
    back to ``contested``, surfaced fields cleared, prior record kept in
    ``surface_history``). Status + ``surfaced_fact_id`` are exactly the fields
    the ``alert_trigger_scan`` contention_flip trigger watches, so every
    surfacing/re-open event fires the alert loop for free.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from ..._entity_canon import is_junk_entity
from ...filters.fact_extractor import (
    _is_capital_metonymy,             # CW-6 — capital-as-government gate
    _is_inverted_relation,
    _is_nongeo_containment_inversion,
    _is_possessive_fragment,          # FU5b — surfacing junk gate
    _is_reflexive_after_canon,
    _is_source_publication_subject,   # FU5b — surfacing junk gate (byline outlet)
)
from ...provenance.models import FindingPayload
from ...provenance.value_clustering import cluster_values
from ...vocabulary import normalize_predicate
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

ARBITER_VERSION = "fact_contention_arbiter/1.1.0"

#: Surface gate (decisions B10/B11). The winner is surfaced only when its score
#: clears this floor AND dominates the runner-up by the ratio below; otherwise
#: the group stays contested with NO surfaced winner (abstain — an honest
#: deadlock, refined by the LLM tie-break in the later Wave 2b).
MIN_SURFACE_SCORE = 0.15
DOMINANCE_RATIO = 1.25

#: P3-2 — soak gate on the TIE-BREAK layers (weight + LLM). An abstained group
#: younger than this many hours stays honestly ``contested`` (evidence still
#: accumulating); ``0`` disables the gate. The Q·C·R·F-decisive path is NOT
#: soak-gated (a dominance-gated winner surfaces immediately, as it always has).
#: Default informed by the 2026-07-24 live soak read: 96/729 live groups were
#: younger than 48h; median live-group age ~8.4 days — a 48h soak delays only
#: the newest disputes while corroboration accrues.
SOAK_HOURS_ENV = "LEGBA_CONTENTION_SURFACE_SOAK_HOURS"
DEFAULT_SURFACE_SOAK_HOURS = 48.0

#: P3-2 — weighted tie-break dominance ratio: the best side's deterministic
#: weight must be at least this multiple of the runner-up's to surface without
#: an LLM. Live soak read: at 1.5 the contested population splits ~35 decisive /
#: ~367 gray (median best/runner weight ratio 1.0 — most live disputes are
#: symmetric 1-source-vs-1-source, which is exactly what the LLM gray zone and
#: the future A6 earned record are for).
WEIGHT_RATIO_ENV = "LEGBA_CONTENTION_WEIGHT_RATIO"
DEFAULT_WEIGHT_DOMINANCE_RATIO = 1.5

#: P3-2 — the weight winner must carry REAL accumulated corroboration:
#: >= this many distinct sources. A 1-source side never wins on weight alone
#: (it can still win a Q·C·R·F dominance or an LLM near-tie verdict).
WEIGHT_TIEBREAK_MIN_SOURCES = 2

#: A6 P3-3 — the EARNED-track-record consumption seam. The MAX additive weight a
#: proven source's earned record may contribute to its side's deterministic
#: tie-break weight. Default 0.0 = OFF: `_earned_track_record_weight` returns
#: 0.0 for every agg, so the tie-break weight is byte-identical to the P3-2
#: formula (corroboration + diversity + credibility). Flip to a small positive
#: value (e.g. 1.0) later as a MEASURED step. HARD rule: this feeds the
#: tie-break WEIGHT only — never the faithfulness score (A6; trust != grounding).
EARNED_WEIGHT_ENV = "LEGBA_CONTENTION_EARNED_WEIGHT"
DEFAULT_EARNED_WEIGHT = 0.0

#: Cap on ``surface_history`` entries kept per group (newest first; the
#: finalize SQL trims the oldest entry past the cap — one append per pass max,
#: so the cap can never be exceeded).
SURFACE_HISTORY_CAP = 50

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

#: FU5(c) — the nominal credibility of a source whose ``source_credibility`` is
#: NULL/UNKNOWN, used ONLY for the credibility-weighted quorum (never for cred_sum,
#: which sums non-NULL only). Mirrors the machine-extraction tier nominal.
_UNKNOWN_SOURCE_CRED = 0.5

#: FU5(a) — person-subject functional-role predicates (country in VALUE, holder in
#: SUBJECT). The normal (subject, predicate) grouping buckets each PERSON
#: separately, so a cross-person contradiction (Biden vs Trump both 'leader of US')
#: is never clustered. These rows are RE-KEYED on (country, office/role) so the
#: dispute clusters. 'head of state'/'head of government' already key on the
#: country in the SUBJECT (normal path), so only 'leader of' needs re-keying.
_PERSON_SUBJECT_ROLE_PREDICATE = "leader of"


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
    # CW-6 — a capital standing in for its government ("Madrid border with
    # France", "Washington member of NATO"). The fact plane started rejecting
    # these on 2026-08-03; this gate was NOT updated with it, so the contention
    # plane kept clustering the class and K-4 R3 kept harvesting 0/20-scoring
    # questions out of it ("which value of 'border with' for 'madrid' is
    # correct?"). Adding it here is what stops the class RE-FORMING after
    # migration 0176 closes the history — the gates this function reuses are
    # only battle-tested if they are all actually here.
    if _is_capital_metonymy(subject, predicate, value):
        return "capital_metonymy"
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
        "row_count", "_source_cred", "earned_weight",
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
        # FU5(c) — per DISTINCT source (lineage ref) MAX credibility, for the
        # credibility-weighted quorum. Empty ⇒ no per-source credibility was seen
        # (e.g. the pure-logic test aggs), in which case the weighted count falls
        # back to the raw distinct-source count (byte-identical prior behavior).
        self._source_cred: dict[str, float] = {}
        # A6 P3-3 — the damped, non-negative EARNED per-side signal in [0, 1] the
        # arbiter attaches (from source_track_record.earned_weights_for_sources)
        # ONLY when the LEGBA_CONTENTION_EARNED_WEIGHT consumption seam is ON.
        # Defaults 0.0 and stays 0.0 with the seam OFF -> _tiebreak_weight is then
        # byte-identical to the P3-2 formula (the OFF invariant).
        self.earned_weight: float = 0.0

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
        # source_credibility: SUM of non-NULL only (NULL = UNKNOWN, never 0).
        cred = row.get("source_credibility")
        # FU5(c) — the per-source credibility used for the WEIGHTED quorum: the row's
        # score when known, else the machine-extraction nominal (so an unknown-cred
        # source still casts a bounded, non-zero vote — it is not silently dropped).
        cred_nominal = float(cred) if cred is not None else _UNKNOWN_SOURCE_CRED
        derived = row.get("derived_from") or []
        refs = [str(ref) for ref in derived] if derived else [f"fact:{fid}"]
        for ref in refs:
            self.distinct_lineage.add(ref)
            self._source_cred[ref] = max(self._source_cred.get(ref, 0.0), cred_nominal)
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
    def credibility_weighted_source_count(self) -> float:
        """FU5(c) — the quorum vote WEIGHTED by source credibility: the sum of each
        DISTINCT source's credibility, so N low-credibility syndicated copies cannot
        out-vote one authoritative source on raw count alone. Falls back to the raw
        distinct-source count when no per-source credibility was recorded (the
        pure-logic test path), keeping that path byte-identical."""
        if not self._source_cred:
            return float(self.distinct_source_count)
        return sum(self._source_cred.values())

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


async def _open_functional_role_triples(conn: Any) -> list[Mapping[str, Any]]:
    """FU5(a) — fetch open person-subject 'leader of' facts where >= 2 DISTINCT
    persons claim the SAME (country, office/role) — the cross-subject contradiction
    the normal (subject, predicate) grouping cannot see.

    Grouped by (country=value, ``data->>'role'``) so a genuine DUAL-OFFICE country
    (Iran supreme leader vs president — both 'leader of Iran' but DIFFERENT office
    roles) is NOT flagged as a contradiction; only two people claiming the SAME
    office of the same country cluster. Rows carry ``role_key`` so the rekey can
    keep the offices in separate contention groups."""
    return await conn.fetch(
        f"""
        WITH open_role_facts AS (
            SELECT id, subject, predicate, value, confidence, source_type,
                   source_credibility, produced_at, derived_from,
                   coalesce(data->>'role', '') AS role_key
              FROM facts
             WHERE valid_until IS NULL
               AND superseded_by IS NULL
               AND lower(btrim(predicate)) = '{_PERSON_SUBJECT_ROLE_PREDICATE}'
             LIMIT {MAX_SCAN_FACTS}
        ),
        grouped AS (
            SELECT lower(btrim(value)) AS country_key, role_key,
                   count(DISTINCT lower(btrim(subject))) AS n
              FROM open_role_facts
             GROUP BY 1, 2
            HAVING count(DISTINCT lower(btrim(subject))) >= 2
        )
        SELECT f.id, f.subject, f.predicate, f.value, f.confidence, f.source_type,
               f.source_credibility, f.produced_at, f.derived_from, f.role_key
          FROM open_role_facts f
          JOIN grouped g
            ON lower(btrim(f.value)) = g.country_key
           AND f.role_key = g.role_key
         ORDER BY lower(btrim(f.value)), f.role_key, lower(btrim(f.subject)), f.id
        """
    )


def _rekey_role_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """FU5(a) — re-key one person-subject 'leader of' row so the COUNTRY (+ office)
    becomes the group subject and the PERSON becomes the disputed VALUE. The fact
    ``id`` (used for the DETECT-ONLY marker stamping) is preserved untouched; only
    the grouping/clustering surfaces are swapped. The office/role is folded into the
    synthetic subject so two OFFICES of the same country stay in SEPARATE contention
    groups (a dual-office country is never a contradiction)."""
    d = dict(row)
    country = str(row.get("value") or "")
    person = str(row.get("subject") or "")
    role = str(row.get("role_key") or "").strip()
    d["subject"] = f"{country} [{role}]" if role else country
    d["value"] = person
    return d


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
    """Compute the ``Q·C·R·F`` score for every non-junk cluster, keyed by value_key.

    FU5(c) — the quorum ``Q`` is normalized over the CREDIBILITY-WEIGHTED source
    count (sum of each distinct source's credibility) rather than the raw
    observation count, so syndication (many low-credibility copies) cannot
    manufacture quorum. On the pure-logic path (no per-source credibility) the
    weighted count degrades to the raw distinct count, so ``Q`` is unchanged there."""
    max_distinct = max(
        (a.credibility_weighted_source_count for a in aggs), default=0.0
    )
    group_cred_total = sum(a.cred_sum for a in aggs)
    scores: dict[str, float] = {}
    for agg in aggs:
        q = _quorum(agg.credibility_weighted_source_count, max_distinct)
        c = _credibility_share(agg.cred_sum, group_cred_total)
        r = _recency(agg.latest_asserted_at, now)
        f = agg.confidence_mean
        scores[agg.value_key] = _arbiter_score(q, c, r, f)
    return scores


def _is_unsurfaceable_value(value: str) -> bool:
    """FU5(b) — a value that must NEVER be SURFACED as a contention winner even if
    its cluster passed the group junk gate (``_junk_reason``): a determiner /
    numeral / stopword (``is_junk_entity``), a spaced-possessive tokenizer fragment
    ('Donald Trump 's'), or a byline outlet name. Reuses the fact_extractor /
    entity-canon junk predicates verbatim (never reimplemented). An EMPTY value is
    NOT judged here (deferred — the group junk gate owns it), so a rep-less
    pure-logic agg is unaffected."""
    v = str(value or "").strip()
    if not v:
        return False
    return (
        is_junk_entity(v)
        or _is_possessive_fragment(v)
        or _is_source_publication_subject(v)
    )


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
    # FU5(b) — never SURFACE a junk value (determiner / numeral / possessive
    # fragment / byline outlet) as the winner even if it cleared both score gates;
    # abstain instead (an honest "no clean winner"). The group junk gate already
    # excludes junk CLUSTERS, but the possessive/byline classes slip past it.
    if _is_unsurfaceable_value(best.representative_value):
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
# P3-2 tail — soak gate + deterministic weighted tie-break (+ A6 seam)
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("fact_contention_arbiter.bad_env %s=%r; using %s", name, raw, default)
        return default


def _surface_soak_hours() -> float:
    """The tie-break soak window in hours (env-configurable, >= 0)."""
    return max(_env_float(SOAK_HOURS_ENV, DEFAULT_SURFACE_SOAK_HOURS), 0.0)


def _weight_dominance_ratio() -> float:
    """The weighted tie-break dominance ratio (env-configurable, >= 1)."""
    return max(_env_float(WEIGHT_RATIO_ENV, DEFAULT_WEIGHT_DOMINANCE_RATIO), 1.0)


def _past_soak(opened_at: datetime | None, now: datetime) -> bool:
    """Has this group soaked long enough for the tie-break layers to run?

    ``opened_at is None`` (a degraded/legacy state read) counts as PAST soak —
    the gate only ever DELAYS on a real, younger-than-window ``opened_at``; an
    unknown age must never silently stall a dispute forever."""
    hours = _surface_soak_hours()
    if hours <= 0:
        return True
    if opened_at is None:
        return True
    return (now - opened_at).total_seconds() >= hours * 3600.0


def _earned_weight_scale() -> float:
    """The A6 P3-3 earned-record weight scale (env; 0 disables — the default).

    OFF (0.0) ⇒ :func:`_earned_track_record_weight` returns 0.0 for every agg,
    so the deterministic tie-break weight is byte-identical to the P3-2 formula
    (the OFF invariant asserted in the tail tests)."""
    return max(_env_float(EARNED_WEIGHT_ENV, DEFAULT_EARNED_WEIGHT), 0.0)


def _earned_track_record_weight(agg: _ValueAgg) -> float:
    """**A6 SEAM — earned track record (P3-3), OFF by default.**

    Adds the source-assurance ledger's layer 3 signal — the EARNED per-source
    track record computed from our own contention/corroboration outcomes
    (source_track_record.py; planning/PROGRAM_RECOMMENDATIONS_2026-07-24.md
    §A6) — as a bounded, damped, NON-NEGATIVE per-side weight.

    ``agg.earned_weight`` is the damped signal in ``[0, 1]`` the arbiter pass
    attaches via :func:`_attach_earned_weights` (lagged + acyclicity-guarded)
    ONLY when the consumption seam is ON; it is 0.0 otherwise. This function
    returns ``scale * agg.earned_weight``, so with ``LEGBA_CONTENTION_EARNED_
    WEIGHT`` unset/0 it is EXACTLY 0.0 and the P3-2 weight is unchanged.

    Circularity guard (enforced upstream in :func:`_attach_earned_weights` +
    source_track_record): damped updates, a neutral prior, and NO same-cycle
    feedback (the record is recomputed live EXCLUDING the contention being
    decided, over contentions settled > lag ago). Additive and bounded. Grades
    feed weighting, NEVER faithfulness."""
    scale = _earned_weight_scale()
    if scale <= 0.0:
        return 0.0
    return scale * max(0.0, float(getattr(agg, "earned_weight", 0.0) or 0.0))


def _tiebreak_weight(agg: _ValueAgg) -> float:
    """P3-2 deterministic side weight: corroboration + diversity + credibility.

    ``distinct_source_count`` (distinct lineage — a chatty source counts once)
    + distinct ``source_types`` (diversity: two lanes agreeing beats one lane
    twice) + the summed non-NULL ``source_credibility`` of carrying sources,
    + the A6 earned-track-record seam (0.0 today). Deliberately NO recency /
    confidence factor — this layer answers "where did corroboration
    ACCUMULATE", not "what was said last" (recency already had its say in
    Q·C·R·F)."""
    return (
        float(agg.distinct_source_count)
        + float(len(agg.source_types))
        + float(agg.cred_sum)
        + _earned_track_record_weight(agg)
    )


def _tiebreak_weights(aggs: list[_ValueAgg]) -> dict[str, float]:
    return {a.value_key: _tiebreak_weight(a) for a in aggs}


#: Resolve each side's carrying source ids via the SAME lineage the aggregator
#: counts on: supporting_fact_ids -> facts.derived_from -> signals.source_id.
_FACT_SOURCES_SQL = """
SELECT f.id AS fact_id, s.source_id
  FROM facts f
 CROSS JOIN LATERAL unnest(f.derived_from) AS d(sig)
  JOIN signals s ON s.id = d.sig
 WHERE f.id = ANY($1::uuid[])
"""


async def _attach_earned_weights(
    conn: Any,
    aggs: list[_ValueAgg],
    *,
    contention_id: UUID,
    now: datetime,
) -> None:
    """A6 P3-3 — attach each side's damped EARNED signal (ON-seam only).

    Called ONLY when ``LEGBA_CONTENTION_EARNED_WEIGHT`` > 0. Resolves each
    ``agg``'s carrying sources, looks up their LIVE earned side-weight, and
    stamps ``agg.earned_weight`` with the strongest carrier's signal (a side is
    as proven as its best-track-record source). Circularity guard: the earned
    weights are recomputed live EXCLUDING ``contention_id`` (acyclicity) over
    contentions settled > lag ago (source_track_record.earned_weights_for_
    sources). Any failure DEGRADES to no bonus (earned_weight stays 0.0) — the
    seam never breaks the deterministic tie-break."""
    from . import source_track_record as _str  # lazy: OFF path never imports

    try:
        fact_ids: list[UUID] = []
        for agg in aggs:
            fact_ids.extend(agg.supporting_fact_ids)
        if not fact_ids:
            return
        rows = await conn.fetch(_FACT_SOURCES_SQL, list(dict.fromkeys(fact_ids)))
        by_fact: dict[UUID, set[str]] = {}
        all_sources: set[str] = set()
        for r in rows:
            sid = r["source_id"]
            if not sid:
                continue
            by_fact.setdefault(r["fact_id"], set()).add(str(sid))
            all_sources.add(str(sid))
        if not all_sources:
            return
        weights = await _str.earned_weights_for_sources(
            conn, all_sources, now=now, exclude_contention=contention_id,
        )
        for agg in aggs:
            side_sources: set[str] = set()
            for fid in agg.supporting_fact_ids:
                side_sources |= by_fact.get(fid, set())
            agg.earned_weight = max(
                (weights.get(s, 0.0) for s in side_sources), default=0.0
            )
    except Exception:  # degrade-not-break — the seam is advisory
        logger.exception(
            "fact_contention_arbiter.earned_weight_attach_failed contention=%s",
            contention_id,
        )


def _select_weight_winner(
    aggs: list[_ValueAgg], weights: dict[str, float]
) -> _ValueAgg | None:
    """Deterministic weighted tie-break for a soaked, abstained group.

    Returns the weight winner iff (a) its weight dominates the runner-up by
    ``_weight_dominance_ratio()``, (b) it carries real corroboration
    (>= ``WEIGHT_TIEBREAK_MIN_SOURCES`` distinct sources), and (c) it is a
    surfaceable value; else ``None`` (gray zone — the LLM's territory on a
    near-tie, an honest abstain otherwise). Total order (weight desc,
    value_key asc) so unchanged evidence always picks the same side."""
    if len(aggs) < 2:
        return None
    ordered = sorted(aggs, key=lambda a: (-weights.get(a.value_key, 0.0), a.value_key))
    best, runner = ordered[0], ordered[1]
    best_w = weights.get(best.value_key, 0.0)
    runner_w = weights.get(runner.value_key, 0.0)
    if best.distinct_source_count < WEIGHT_TIEBREAK_MIN_SOURCES:
        return None
    if best_w <= 0.0:
        return None
    if runner_w > 0.0 and best_w < _weight_dominance_ratio() * runner_w:
        return None
    if _is_unsurfaceable_value(best.representative_value):
        return None
    return best


def _evidence_fingerprint(aggs: list[_ValueAgg]) -> str:
    """Stable sha256 over the group's per-side evidence (order-insensitive).

    Any evidence change — a new supporting row, new lineage, credibility or
    confidence drift, a newer assertion — changes the fingerprint, so a cached
    LLM verdict can never outlive the evidence it judged (the reversibility
    lever: new contradicting evidence re-opens the question)."""
    payload = sorted(
        (
            {
                "k": a.value_key,
                "n": a.distinct_source_count,
                "t": sorted(a.source_types),
                "c": round(float(a.cred_sum), 3),
                "a": a.latest_asserted_at.isoformat() if a.latest_asserted_at else "",
                "f": round(float(a.confidence_mean), 3),
            }
            for a in aggs
        ),
        key=lambda d: d["k"],
    )
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class _SurfaceDecision:
    """The pass's surfacing decision for one group: winner (or None) + the
    coexistence receipt (who decided, why)."""

    __slots__ = ("winner", "surfaced_by", "rationale")

    def __init__(
        self,
        winner: _ValueAgg | None = None,
        surfaced_by: str | None = None,
        rationale: str | None = None,
    ) -> None:
        self.winner = winner
        self.surfaced_by = surfaced_by
        self.rationale = rationale


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
    "value_key, or ABSTAIN if they remain a genuine tie. Be conservative: when "
    "in doubt, ABSTAIN. Reply with ONE JSON object and nothing else: "
    "{\"winner\": \"<value_key>\", \"why\": \"<one short sentence>\"} or "
    "{\"winner\": \"ABSTAIN\", \"why\": \"<one short sentence>\"}. Never invent "
    "a value_key not listed."
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
        'Reply with ONE JSON object: {"winner": "<value_key>", "why": "<one '
        'short sentence>"} or {"winner": "ABSTAIN", "why": "<one short sentence>"}.'
    )
    return "\n".join(lines)


def _parse_tiebreak_reply(
    raw: str, valid_keys: set[str]
) -> tuple[str | None, str, bool]:
    """STRICTLY parse the tie-break reply.

    Returns ``(winner_key | None, justification, genuine)``:

      * ``winner_key`` — ONE listed value_key, else ``None`` (→ ABSTAIN). No
        hallucinated value can ever be surfaced.
      * ``justification`` — the model's ``why`` (bounded), possibly empty.
      * ``genuine`` — ``True`` only for a well-formed verdict (a listed pick OR
        the literal ABSTAIN): these are CACHEABLE per evidence fingerprint. Any
        deviation (no JSON, missing ``winner``, an unlisted value_key) is NOT
        genuine — never cached, so a misbehaving reply can be retried on a
        later pass (bounded by the per-pass cap)."""
    if not raw:
        return None, "", False
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    start = candidate.find("{")
    if start < 0:
        return None, "", False
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
        return None, "", False
    try:
        obj = json.loads(candidate[start:end])
    except (json.JSONDecodeError, ValueError):
        return None, "", False
    if not isinstance(obj, dict):
        return None, "", False
    why_raw = obj.get("why") or obj.get("justification") or ""
    why = str(why_raw)[:600] if isinstance(why_raw, (str, int, float)) else ""
    winner = obj.get("winner")
    if not isinstance(winner, str):
        return None, why, False
    winner = winner.strip()
    if not winner:
        return None, why, False
    if winner.upper() == "ABSTAIN":
        return None, why, True
    if winner in valid_keys:
        return winner, why, True
    return None, why, False


def _parse_tiebreak_winner(
    raw: str, valid_keys: set[str]
) -> str | None:
    """Back-compat shim: the winner key alone (see :func:`_parse_tiebreak_reply`)."""
    return _parse_tiebreak_reply(raw, valid_keys)[0]


class _TiebreakOutcome:
    """One LLM tie-break result: the pick (or None), the recorded receipt
    (justification + model id), and whether it is a GENUINE verdict (cacheable)
    vs a transport/parse failure (retryable, never cached)."""

    __slots__ = ("winner_key", "justification", "model_id", "cacheable")

    def __init__(
        self,
        winner_key: str | None,
        justification: str = "",
        model_id: str | None = None,
        cacheable: bool = False,
    ) -> None:
        self.winner_key = winner_key
        self.justification = justification
        self.model_id = model_id
        self.cacheable = cacheable


async def _llm_tiebreak(
    llm: Any,
    subject_key: str,
    predicate_key: str,
    aggs: list[_ValueAgg],
    scores: dict[str, float],
    now: datetime,
) -> _TiebreakOutcome:
    """Bounded, strictly-parsed LLM tie-break for ONE near-tie group.

    Returns a :class:`_TiebreakOutcome` (never raises). Degrade-not-break: any
    timeout / exception / unparsable / unlisted result yields an abstain outcome
    (the near-tie stands), mirroring the ACH per-cell lexical fallback — and is
    NOT cacheable, so a recovered model can retry the same evidence later. The
    call is single-turn, temperature 0, output-capped, and wrapped in a per-call
    wall-clock timeout. DETECT-ONLY: this only SELECTS a winner — the caller
    still surfaces it through the same sidecar + marker path."""
    if llm is None or len(aggs) < 2:
        return _TiebreakOutcome(None, "no llm / not a dispute")
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
        return _TiebreakOutcome(None, "tie-break call timed out")
    except Exception as exc:  # degrade-not-break: any handler failure → abstain
        logger.warning("fact_contention_arbiter.tiebreak_failed err=%s", exc)
        return _TiebreakOutcome(None, f"tie-break call failed: {exc}")
    content = getattr(response, "content", "") or ""
    usage = getattr(response, "usage", None)
    model_id = (getattr(usage, "model", "") or "").strip() or None
    winner_key, why, genuine = _parse_tiebreak_reply(content, set(by_key))
    return _TiebreakOutcome(winner_key, why, model_id, cacheable=genuine)


# ---------------------------------------------------------------------------
# P3-2 — LLM tie-break verdict cache (fact_contention_tiebreak, mig 0097)
# ---------------------------------------------------------------------------


async def _load_cached_tiebreak(
    conn: Any, contention_id: UUID, fingerprint: str
) -> Mapping[str, Any] | None:
    """The cached genuine verdict for this exact evidence state, or ``None``.

    A hit means the SAME question (same contention, same per-side evidence) was
    already answered — never re-ask (entity_judgement pattern). Read via
    ``fetch`` + LIMIT 1 (degrades to a miss on any read failure)."""
    try:
        rows = await conn.fetch(
            """
            SELECT verdict, winner_value_key, justification, model_id
              FROM fact_contention_tiebreak
             WHERE contention_id = $1 AND evidence_fingerprint = $2
             LIMIT 1
            """,
            contention_id,
            fingerprint,
        )
    except Exception as exc:  # cache read failure = miss, never a block
        logger.warning("fact_contention_arbiter.tiebreak_cache_read_failed err=%s", exc)
        return None
    return rows[0] if rows else None


async def _persist_tiebreak(
    conn: Any,
    contention_id: UUID,
    fingerprint: str,
    *,
    verdict: str,
    winner_value_key: str | None,
    justification: str,
    model_id: str | None,
) -> None:
    """Upsert a GENUINE tie-break verdict (pick / explicit-abstain 'unsure').

    Best-effort: a cache-write failure never blocks the pass (worst case the
    same question is re-asked next pass, bounded by the per-pass cap)."""
    try:
        await conn.execute(
            """
            INSERT INTO fact_contention_tiebreak (
                contention_id, evidence_fingerprint, verdict,
                winner_value_key, justification, model_id
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (contention_id, evidence_fingerprint) DO UPDATE
               SET verdict = EXCLUDED.verdict,
                   winner_value_key = EXCLUDED.winner_value_key,
                   justification = EXCLUDED.justification,
                   model_id = EXCLUDED.model_id,
                   decided_at = now()
            """,
            contention_id,
            fingerprint,
            verdict,
            winner_value_key,
            justification,
            model_id,
        )
    except Exception as exc:
        logger.warning("fact_contention_arbiter.tiebreak_cache_write_failed err=%s", exc)


# ---------------------------------------------------------------------------
# Sidecar persistence (DETECT-ONLY: facts writes are markers only)
# ---------------------------------------------------------------------------


async def _upsert_group(
    conn: Any, subject_key: str, predicate_key: str
) -> UUID:
    """Upsert the ``fact_contention`` group, returning its id (idempotent).

    A group RE-EMERGING from ``collapsed`` (the dispute reformed) resets its
    ``opened_at`` so the P3-2 soak clock starts fresh — a reformed dispute is
    new evidence, not a 3-month-old one."""
    return await conn.fetchval(
        """
        INSERT INTO fact_contention (subject_key, predicate_key, status, updated_at)
        VALUES ($1, $2, 'contested', now())
        ON CONFLICT (subject_key, predicate_key) DO UPDATE
           SET updated_at = now(),
               opened_at = CASE
                   WHEN fact_contention.status = 'collapsed' THEN now()
                   ELSE fact_contention.opened_at
               END
        RETURNING id
        """,
        subject_key,
        predicate_key,
    )


async def _group_surface_state(conn: Any, contention_id: UUID) -> Mapping[str, Any] | None:
    """The group's current soak/surface state (or ``None`` on a degraded read).

    Read via ``fetch`` (not fetchrow) so a legacy/fake connection without the
    0097 columns degrades to ``None`` → treated as "no prior surface, unknown
    age" (fail-open on soak, no history to preserve)."""
    try:
        rows = await conn.fetch(
            """
            SELECT opened_at, status, surfaced_value, surfaced_fact_id,
                   surfaced_by, surfaced_at, surface_rationale
              FROM fact_contention
             WHERE id = $1
            """,
            contention_id,
        )
    except Exception as exc:
        logger.warning("fact_contention_arbiter.surface_state_read_failed err=%s", exc)
        return None
    return rows[0] if rows else None


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


def _prior_surface_record(prior: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The prior pass's surface decision as a history-appendable dict, or
    ``None`` when nothing was surfaced (status not ``surfaced`` / no pointer)."""
    if prior is None:
        return None
    if str(prior.get("status") or "") != "surfaced":
        return None
    fact_id = prior.get("surfaced_fact_id")
    if fact_id is None:
        return None
    surfaced_at = prior.get("surfaced_at")
    return {
        "surfaced_value": prior.get("surfaced_value"),
        "surfaced_fact_id": str(fact_id),
        "surfaced_by": prior.get("surfaced_by"),
        "surfaced_at": surfaced_at.isoformat() if surfaced_at is not None else None,
        "surface_rationale": prior.get("surface_rationale"),
    }


async def _finalize_group(
    conn: Any,
    contention_id: UUID,
    non_junk: list[_ValueAgg],
    decision: _SurfaceDecision,
    *,
    prior: Mapping[str, Any] | None,
    now: datetime,
) -> bool:
    """Set the group's status/surfaced pointer + coexistence record + (re)stamp
    the ``facts`` markers. Returns ``True`` when a previously-surfaced group
    RE-OPENED (winner withdrawn → status back to ``contested``).

    Coexistence semantics (P3-2, mig 0097): a surfaced winner carries
    ``surfaced_by`` / ``surfaced_at`` / ``surface_rationale``; the stamp is
    STABLE across passes while the SAME winner stands (idempotent recompute).
    When the decision changes — winner flips, or the group re-opens — the prior
    record is APPENDED (newest first, capped) to ``surface_history`` and the
    live fields move. ``status`` + ``surfaced_fact_id`` are exactly the fields
    the alert_trigger_scan contention_flip trigger fingerprints, so every such
    event fires the alert loop.

    DETECT-ONLY: the ONLY ``facts`` columns written are ``contested`` /
    ``contention_id`` / ``surfaced_winner``. ``valid_until`` / ``superseded_by`` /
    ``value`` / ``confidence`` are NEVER touched here."""
    winner = decision.winner
    surfaced_value = winner.value_key if winner is not None else None
    surfaced_fact_id = winner.representative_fact_id if winner is not None else None
    status = "surfaced" if winner is not None else "contested"

    prior_record = _prior_surface_record(prior)
    same_winner = (
        prior_record is not None
        and winner is not None
        and prior_record["surfaced_fact_id"] == str(surfaced_fact_id)
    )
    if same_winner and prior is not None and prior.get("surfaced_at") is not None:
        # The standing decision stands — keep the original stamp verbatim.
        surfaced_by = prior.get("surfaced_by") or decision.surfaced_by
        surfaced_at = prior.get("surfaced_at")
        rationale = prior.get("surface_rationale") or decision.rationale
        history_json: str | None = None
    else:
        surfaced_by = decision.surfaced_by if winner is not None else None
        surfaced_at = now if winner is not None else None
        rationale = decision.rationale if winner is not None else None
        # Append the prior surface record iff a DIFFERENT decision replaces it
        # (a pre-0097 same-winner row with no surfaced_at just gets stamped).
        if prior_record is not None and not same_winner:
            history_json = json.dumps(
                {
                    **prior_record,
                    "superseded_at": now.isoformat(),
                    "superseded_by_status": status,
                }
            )
        else:
            history_json = None
    reopened = prior_record is not None and winner is None

    await conn.execute(
        f"""
        UPDATE fact_contention
           SET status = $2,
               surfaced_value = $3,
               surfaced_fact_id = $4,
               value_count = $5,
               resolved_at = now(),
               arbiter_version = $6,
               surfaced_by = $7,
               surfaced_at = $8,
               surface_rationale = $9,
               surface_history = CASE
                   WHEN $10::jsonb IS NULL THEN COALESCE(surface_history, '[]'::jsonb)
                   ELSE (jsonb_build_array($10::jsonb)
                         || COALESCE(surface_history, '[]'::jsonb))
                        - {SURFACE_HISTORY_CAP}
               END,
               updated_at = now()
         WHERE id = $1
        """,
        contention_id,
        status,
        surfaced_value,
        surfaced_fact_id,
        len(non_junk),
        ARBITER_VERSION,
        surfaced_by,
        surfaced_at,
        rationale,
        history_json,
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
    return reopened


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


async def _process_group(
    conn: Any,
    subject_key: str,
    predicate_key: str,
    group_rows: list[Mapping[str, Any]],
    *,
    now: datetime,
    llm: Any | None,
    llm_tiebreaks_left: int,
    counts: dict[str, int],
    live_keys: set[tuple[str, str]],
) -> int:
    """Cluster + score + surface ONE contention group, updating ``counts`` /
    ``live_keys`` and returning the (possibly-decremented) LLM tie-break budget.

    Shared by the normal (subject, predicate) pass and the FU5(a) role-keyed pass —
    both feed the SAME detect-only pipeline (aggregate → junk-gate → score →
    surface/abstain → sidecar + markers)."""
    non_junk, junk = _aggregate_group(group_rows)
    counts["junk_excluded"] += len(junk)
    if len(non_junk) < 2:
        # Not a genuine dispute (all-but-one value is junk, or a single clustered
        # value). Collapse any pre-existing group for this key; else nothing to open.
        existing = await conn.fetchval(
            "SELECT id FROM fact_contention WHERE subject_key = $1 AND predicate_key = $2",
            subject_key,
            predicate_key,
        )
        if existing is not None:
            await _collapse_group(conn, existing)
            counts["groups_collapsed"] += 1
        return llm_tiebreaks_left
    live_keys.add((subject_key, predicate_key))
    # Upsert FIRST so the soak clock (opened_at) + any prior surface record are
    # in hand before the tie-break layers decide (they are recomputed from the
    # open rows every pass — the reversibility guarantee).
    contention_id = await _upsert_group(conn, subject_key, predicate_key)
    prior = await _group_surface_state(conn, contention_id)
    opened_at = prior.get("opened_at") if prior is not None else None

    scores = _score_group(non_junk, now)
    qcrf_winner = _select_winner(non_junk, scores)
    if qcrf_winner is not None:
        # A dominance-gated Q·C·R·F winner surfaces IMMEDIATELY (not soak-gated).
        decision = _SurfaceDecision(
            qcrf_winner, "deterministic",
            _qcrf_rationale(qcrf_winner, scores),
        )
    else:
        decision, llm_tiebreaks_left = await _resolve_tiebreak(
            conn, contention_id, subject_key, predicate_key, non_junk, scores,
            now=now, opened_at=opened_at, llm=llm,
            llm_tiebreaks_left=llm_tiebreaks_left, counts=counts,
        )

    await _replace_group_values(conn, contention_id, non_junk, junk, scores, decision.winner)
    await conn.execute(
        "UPDATE fact_contention SET junk_count = $2 WHERE id = $1",
        contention_id,
        len(junk),
    )
    reopened = await _finalize_group(
        conn, contention_id, non_junk, decision, prior=prior, now=now,
    )
    counts["groups_open"] += 1
    counts["values_total"] += len(non_junk)
    if decision.winner is None:
        counts["abstained"] += 1
    if reopened:
        counts["reopened"] += 1
    return llm_tiebreaks_left


def _qcrf_rationale(winner: _ValueAgg, scores: dict[str, float]) -> str:
    """One-line deterministic Q·C·R·F receipt for the surface record."""
    ordered = sorted(scores.values(), reverse=True)
    runner = ordered[1] if len(ordered) > 1 else 0.0
    return (
        f"deterministic Q·C·R·F: {winner.value_key!r} score="
        f"{scores.get(winner.value_key, 0.0):.4f} dominated runner-up "
        f"{runner:.4f} (>= {DOMINANCE_RATIO}x, >= {MIN_SURFACE_SCORE} floor)"
    )


def _weight_rationale(winner: _ValueAgg, weights: dict[str, float]) -> str:
    """One-line deterministic weighted-tie-break receipt for the surface record."""
    ordered = sorted(weights.values(), reverse=True)
    runner = ordered[1] if len(ordered) > 1 else 0.0
    return (
        f"deterministic weighted tie-break (post-soak): {winner.value_key!r} "
        f"weight={weights.get(winner.value_key, 0.0):.3f} "
        f"(sources={winner.distinct_source_count}, types={len(winner.source_types)}, "
        f"cred={winner.cred_sum:.3f}) dominated runner-up {runner:.3f} "
        f"(>= {_weight_dominance_ratio()}x)"
    )


async def _resolve_tiebreak(
    conn: Any,
    contention_id: UUID,
    subject_key: str,
    predicate_key: str,
    non_junk: list[_ValueAgg],
    scores: dict[str, float],
    *,
    now: datetime,
    opened_at: datetime | None,
    llm: Any | None,
    llm_tiebreaks_left: int,
    counts: dict[str, int],
) -> tuple[_SurfaceDecision, int]:
    """The P3-2 abstain tail: soak gate → weighted tie-break → cached LLM
    tie-break. Returns ``(_SurfaceDecision, llm_tiebreaks_left)``.

    Q·C·R·F has already abstained here. A WEAK abstain (best below the floor) is
    always left alone — a genuinely thin dispute has no winner to accumulate. A
    group younger than the soak window is likewise left ``contested`` (evidence
    still accruing). Past soak, a near-tie may (a) resolve deterministically when
    accumulated corroboration weight is decisive, else (b) consult the cached
    LLM."""
    cause = _abstain_cause(non_junk, scores)
    # A weak abstain never runs any tie-break (weight OR LLM): nothing has
    # accumulated on either side worth surfacing.
    if cause != "near_tie":
        return _SurfaceDecision(None), llm_tiebreaks_left
    # Soak gate — a young near-tie is still accumulating; wait.
    if not _past_soak(opened_at, now):
        counts["soak_deferred"] += 1
        return _SurfaceDecision(None), llm_tiebreaks_left

    # (a) Deterministic weighted tie-break — accumulated corroboration decides.
    # A6 P3-3 (OFF by default): behind LEGBA_CONTENTION_EARNED_WEIGHT, blend in
    # each side's damped EARNED track record (lagged + acyclicity-guarded). With
    # the seam OFF this is skipped entirely and the weight is the P3-2 formula.
    if _earned_weight_scale() > 0.0:
        await _attach_earned_weights(
            conn, non_junk, contention_id=contention_id, now=now,
        )
    weights = _tiebreak_weights(non_junk)
    weight_winner = _select_weight_winner(non_junk, weights)
    if weight_winner is not None:
        counts["weight_tiebreaks"] += 1
        logger.info(
            "fact_contention_arbiter.weight_tiebreak subject=%r predicate=%r pick=%r",
            subject_key, predicate_key, weight_winner.value_key,
        )
        return (
            _SurfaceDecision(
                weight_winner, "deterministic",
                _weight_rationale(weight_winner, weights),
            ),
            llm_tiebreaks_left,
        )

    # (b) Gray zone — the bounded, CACHED LLM tie-break (near-tie only).
    if llm is None:
        return _SurfaceDecision(None), llm_tiebreaks_left
    fingerprint = _evidence_fingerprint(non_junk)
    by_key = {a.value_key: a for a in non_junk}
    cached = await _load_cached_tiebreak(conn, contention_id, fingerprint)
    if cached is not None:
        # Same question, same evidence — never re-ask.
        counts["llm_cache_hits"] += 1
        wk = cached.get("winner_value_key")
        winner = by_key.get(wk) if wk else None
        if winner is not None:
            counts["llm_tiebreaks"] += 1
            return (
                _SurfaceDecision(
                    winner, "llm",
                    f"cached LLM tie-break: {cached.get('justification') or 'pick'}"
                    + (f" [model {cached.get('model_id')}]" if cached.get("model_id") else ""),
                ),
                llm_tiebreaks_left,
            )
        return _SurfaceDecision(None), llm_tiebreaks_left
    if llm_tiebreaks_left <= 0:
        # Cap reached — leave the near-tie abstained (next pass may resolve it).
        return _SurfaceDecision(None), llm_tiebreaks_left

    llm_tiebreaks_left -= 1
    counts["llm_tiebreak_calls"] += 1
    outcome = await _llm_tiebreak(llm, subject_key, predicate_key, non_junk, scores, now)
    logger.info(
        "fact_contention_arbiter.llm_tiebreak subject=%r predicate=%r outcome=%s",
        subject_key, predicate_key,
        f"pick:{outcome.winner_key}" if outcome.winner_key is not None else "abstain",
    )
    # Cache only a GENUINE verdict (a listed pick or an explicit model abstain);
    # transport/parse failures are never cached so a recovered model can retry.
    if outcome.cacheable:
        await _persist_tiebreak(
            conn, contention_id, fingerprint,
            verdict="pick" if outcome.winner_key is not None else "unsure",
            winner_value_key=outcome.winner_key,
            justification=outcome.justification,
            model_id=outcome.model_id,
        )
    if outcome.winner_key is not None:
        winner = by_key.get(outcome.winner_key)
        if winner is not None:
            counts["llm_tiebreaks"] += 1
            return (
                _SurfaceDecision(
                    winner, "llm",
                    f"LLM tie-break: {outcome.justification or 'pick'}"
                    + (f" [model {outcome.model_id}]" if outcome.model_id else ""),
                ),
                llm_tiebreaks_left,
            )
    return _SurfaceDecision(None), llm_tiebreaks_left


def _new_counts() -> dict[str, int]:
    """The cadence-receipt counters (zeroed). Kept in one place so
    :func:`_run_arbiter` and :func:`handle` never drift."""
    return {
        "groups_open": 0,
        "groups_collapsed": 0,
        "values_total": 0,
        "abstained": 0,
        "junk_excluded": 0,
        "llm_tiebreaks": 0,
        "llm_tiebreak_calls": 0,
        # P3-2 tail receipts.
        "weight_tiebreaks": 0,   # abstains a deterministic WEIGHT tie-break resolved
        "llm_cache_hits": 0,     # near-ties served from the tie-break verdict cache
        "soak_deferred": 0,      # near-ties left contested — still inside the soak window
        "reopened": 0,           # surfaced groups withdrawn back to contested this pass
    }


async def _run_arbiter(pool: Any, llm: Any | None = None) -> dict[str, int]:
    """One full arbiter pass over the open facts. Idempotent.

    ``llm`` — the OPTIONAL self-hosted vLLM tie-break handler (Wave 2b). When it
    is ``None`` (flag off / not wired) the pass is byte-for-byte the deterministic
    Wave-2 arbiter. When present, a NEAR-TIE abstain (cause 2) may be resolved by
    a bounded LLM call, capped at ``MAX_LLM_TIEBREAKS`` per pass. A WEAK abstain
    (cause 1) NEVER calls the LLM."""
    counts = _new_counts()
    now = _now()
    llm_tiebreaks_left = MAX_LLM_TIEBREAKS if llm is not None else 0
    async with pool.acquire() as conn:
        rows = await _open_triples(conn)
        buckets = _bucket_rows(list(rows))
        # FU5(a) — role-keyed clustering: person-subject 'leader of' rows re-keyed
        # on (country, office) so two people both 'leader of US' cluster into one
        # dispute the normal (subject, predicate) grouping cannot see.
        role_rows = [_rekey_role_row(r) for r in await _open_functional_role_triples(conn)]
        role_buckets = _bucket_rows(role_rows)
        live_keys: set[tuple[str, str]] = set()
        for (subject_key, predicate_key), group_rows in {**buckets, **role_buckets}.items():
            llm_tiebreaks_left = await _process_group(
                conn, subject_key, predicate_key, group_rows,
                now=now, llm=llm, llm_tiebreaks_left=llm_tiebreaks_left,
                counts=counts, live_keys=live_keys,
            )

        # Collapse any standing group whose key no longer appears as a live
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
        f"weight_tiebreaks={counts.get('weight_tiebreaks', 0)}\n"
        f"soak_deferred={counts.get('soak_deferred', 0)}\n"
        f"reopened={counts.get('reopened', 0)}\n"
        f"llm_tiebreaks={counts.get('llm_tiebreaks', 0)}\n"
        f"llm_tiebreak_calls={counts.get('llm_tiebreak_calls', 0)}\n"
        f"llm_cache_hits={counts.get('llm_cache_hits', 0)}"
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
    counts = _new_counts()
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
