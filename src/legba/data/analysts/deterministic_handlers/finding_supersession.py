# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``finding_supersession`` sub-handler — P-FS finding-level dedup / supersession.

Fixes the live duplicate-findings problem (PIVOT_BUILD_PLAN §12, W3): analysts
re-assess an evolving situation each cadence cycle and re-emit near-duplicate
findings (live: 875 findings / 836 distinct titles, many near-dupes). P-09's
dedup is *signal*-level and does NOT cover this — supersession is the
*analysis-plane* mechanism.

What it does
------------

Clusters findings by **situation signature** and links near-dups so a newer
finding **supersedes** the prior one for the same situation, rather than the
feed accumulating a near-dup per cycle. It mirrors the P-09 ``signal_aliases``
link pattern exactly:

  * NEVER a destructive delete — both finding rows are preserved. The audit
    trail of how the assessment evolved stays intact.
  * The link is recorded in ``finding_supersessions`` (older → newer), and the
    superseded row's ``superseded_by`` pointer is stamped. The latest/canonical
    finding for a situation is the one row whose ``superseded_by IS NULL``.

Situation signature
-------------------

A deterministic grouping key per finding, in priority order:

  1. **Explicit** — ``data.situation_id`` or ``data.situation_signature`` if the
     producing analyst already bound the finding to a situation. This is the
     strong path (e.g. ``situation_detection`` / P-10 situation-scoped analysts).
  2. **Derived** — a normalized signature from the finding's entity/event/topic
     content plus its producing DIMENSION:
     ``sig:<topic>|<sorted entity tokens>#dim:<analyst_id>``. ``topic`` falls
     back through ``data.category`` → ``data.topic`` → the analyst's
     sub_handler. The entity tokens come from ``data.key_entities`` /
     ``data.entities`` / ``data.actors`` / ``data.locations`` (lowercased,
     deduped, sorted) so two findings about the same actors+topic collide
     regardless of phrasing/order. The ``#dim:`` tail is the #64 mega-frame
     repair — see the block comment above ``_SIGNATURE_DIMENSION_MARKER`` for
     the defect (one country-absorbing frame per desk) and why the dimension is
     the one partition available without new producer machinery.

A finding with no derivable signature (no explicit id, no entities, no topic
beyond a bare summary) is **not** clustered — supersession only applies to
situation-bearing findings, never to summary/metrics findings.

Semantic near-dup (best-effort) is identical in spirit to P-09: when a Qdrant
client is injected AND findings carry an ``embedding_ref`` we *could* merge
signatures by similarity. The mechanism reserves that seam (``deps.extras
['qdrant']``) but the shipped library path is the deterministic
signature-match — exactly as the contract permits ("the handler library can be
minimal").

Canonical / latest selection within a cluster is deterministic: the **most
recent** finding wins (latest ``produced_at``, tie-broken by largest ``id``),
because for an evolving situation the freshest assessment is the one the
UI/feed should surface. Older members are superseded and linked to it.

Output ``data`` keys:
    clustered_count    int — situation clusters processed (>=2 findings each)
    superseded_count   int — supersession links written this run
    latest_count       int — distinct situations with a current/latest finding
    clusters           [{situation_signature, latest_finding_id,
                         superseded_finding_ids, reason, score}]
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "finding_supersession"

# Reason tags written into finding_supersessions.reason.
_REASON_EXPLICIT = "situation_id"
_REASON_DERIVED = "signature_match"

# Exact signature matches are certain → score 1.0.
_EXACT_SCORE = 1.0

# DQ P6 (2026-07-03) — the COMPOSITION / META analyst-kind gate. These analysts
# emit second-order ASSESSMENT REPORTS (a country/region/world executive read),
# NOT evolving situation findings. Stamping them with a situation_signature made
# situation_clustering mint a "situation" per report stream named after the
# report title (e.g. "United Kingdom – Composite Assessment", and one row whose
# name was a raw JSON-envelope fragment) — report receipts masquerading as
# frames that never ground per-country yet compete in the global intensity
# ranking. A report is not a situation: exclude these producers from clustering /
# signature stamping entirely. (Their supersession is handled by the dedicated
# composition-supersession fold, migration 0058/0060 — not this finding-level
# situation clusterer.)
_COMPOSITION_ANALYST_IDS = frozenset({
    "country_composition",
    "region_composition",
    "escalation_composition",
    "world_assessor",
    # M18 (2026-07-06) — the cross_analyst_correlator is a META report producer
    # too (analysis-of-analysis): its findings are contradiction/agreement/
    # blind_spot meta-observations, NOT evolving situation frames. Exclude it from
    # the situation clusterer for the SAME reason as the compositions (a report is
    # not a situation) — its supersession is the dedicated write-path
    # :func:`fold_prior_correlation_heads`, mirroring the composition fold.
    "cross_correlator",
})

# Only findings produced no earlier than this many days ago are considered for
# clustering on the live path — old findings are settled history, not an
# evolving situation. Generous default; override via options['lookback_days'].
_DEFAULT_LOOKBACK_DAYS = 30
# DQ-C3 (2026-06-21): the per-run fetch cap. The OLD value (5000) combined with
# ORDER BY produced_at ASC pulled the OLDEST 5000 open findings — which are
# ~83% entity-less cross_source_dedup metric rows (no derivable signature) — so
# the fresh, clusterable country_assessor findings fell OUTSIDE the window and
# were never signature-stamped. That silently froze the whole situations leg
# (clustering re-processed 20 stale rows; all situations closed at 06-12; the
# ASSESSED-SITUATIONS grounding block went empty; thematic_proposal starved).
# Fix: fetch NEWEST-first and lift the cap above the real open-finding pool
# (~19k) so it is a pure safety valve, not a starvation window. ROOT follow-up:
# cross_source_dedup metric outputs should be TRACE_ONLY (not kind='finding'),
# which would shrink this pool ~83% — tracked separately.
_MAX_FINDINGS = 50000


# ---------------------------------------------------------------------------
# Situation signature derivation (shared by live + synthetic paths)
# ---------------------------------------------------------------------------


def _parse_data(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _entity_tokens(data: Mapping[str, Any]) -> list[str]:
    """Normalized entity tokens from the common entity-bearing keys.

    Reads BOTH the top-level finding dump AND its nested ``data`` payload
    sub-dict. The persisted ``analyst_outputs.data`` column is the full
    payload model_dump (``FindingPayload`` is ``extra='forbid'``), so an
    LLM analyst's structured entities live in ``data->'data'->'key_entities'``
    (the inline_target producer sets them there), while a deterministic
    finding may carry them at the top level. Deterministic METRICS findings
    (whose inner dict holds only counts — canonical_count, edges_upserted,
    issues, …) match none of these keys and so yield no tokens here, which is
    exactly the scoping we want: they never cluster.
    """
    tokens: set[str] = set()
    inner = data.get("data")
    sources = (data, inner) if isinstance(inner, Mapping) else (data,)
    for src in sources:
        for key in ("key_entities", "entities", "actors", "locations", "geo", "geo_countries"):
            vals = src.get(key)
            if not vals:
                continue
            if isinstance(vals, (str, bytes)):
                vals = [vals]
            for v in vals:
                t = str(v).strip().lower()
                if len(t) >= 2:
                    tokens.add(t)
    return sorted(tokens)


def _topic(data: Mapping[str, Any], fallback: str | None) -> str:
    inner = data.get("data")
    sources = (data, inner) if isinstance(inner, Mapping) else (data,)
    for src in sources:
        for key in ("category", "topic", "situation_kind", "event_type"):
            v = src.get(key)
            if v:
                return str(v).strip().lower()
    return (fallback or "").strip().lower()


# How many of a finding's entity tokens enter the situation signature.
# 0 = topic-only (the coarsest, most STABLE key): every finding sharing a topic
# clusters into one evolving situation that the lifecycle decay then makes
# breathe (active → dormant → closed → reopen). The previous full-entity-set
# key was the reason only 1 situation formed from 11.5k findings — two findings
# about the SAME evolving event carry slightly different entity lists each
# cycle, so they hashed to different situations and nothing clustered. A small
# positive K (e.g. 2) keeps some entity granularity at the cost of stability;
# 0 is the default because robustness ("situations actually form + breathe")
# is the goal. Finer event-level clustering would need a richer producer signal
# (an event_type, or embeddings) — tracked as a future enhancement.
_SITUATION_SIGNATURE_ENTITY_K = 0


# ---------------------------------------------------------------------------
# THE MEGA-FRAME REPAIR (#64) — the signature carries its DIMENSION
# ---------------------------------------------------------------------------
#
# THE DEFECT, from the FRAME program's §1.4 diagnosis (rows read read-only
# 2026-08-20) and unchanged since: with ``_SITUATION_SIGNATURE_ENTITY_K = 0`` the
# derived key is ``sig:<topic>``, and :func:`_topic` resolves a unit finding's
# topic to its ``category``, which for a country unit IS the target id. So EVERY
# unit finding on a desk — all seven dimensions, several reads a day each —
# clustered into ONE situation named ``sig:country_g20_ar``. The DB confirmed it
# fleet-wide: exactly one open frame per desk across all 33 desks
# (``sig:country_g20_ar`` 377 events, ``sig:country_g20_us`` 436,
# ``sig:country_watch_cd`` 367). At the H1 census the AR frame carried 364
# members of which 42 were the maritime-pilots story. The M23 war and the AR
# property-bill fight were never frames; they were undifferentiated members of a
# desk-blob, and a register whose unit of identity is "a country" cannot answer
# "what is happening in this country" with anything but one row.
#
# THE FIX, and what it deliberately is NOT. It is NOT story-grained clustering —
# that needs an event-typed producer signal or embeddings, was costed and
# deferred by the FRAME program (§1.4, "new machinery"), and is still deferred.
# It is the ONE partition the substrate already carries for free: the producing
# analyst. A desk's dimensions are separate ANALYSTS asking separate bounded
# questions, and ``_cluster`` has always known they are different — its cluster
# key is ``(signature, analyst_id)`` precisely so "a country's leadership read is
# not made stale by its narrative read". The SIGNATURE did not know it, so
# supersession scoped per dimension while MATERIALIZATION did not: ``situations``
# is keyed ``(situation_signature, analyst_id)`` where that analyst_id is the
# CLUSTERING handler's, one value fleet-wide. Seven dimensions, one row.
#
# Putting the dimension IN the signature makes the two layers agree. Supersession
# is unchanged by construction (a key that already discriminated on analyst_id
# now discriminates on a signature that encodes it — the same partition), while
# materialization gains the split it never had.
#
# WHY THE DIMENSION AND NOT A CLOSED EVENT VOCABULARY — the choice, and its
# bounded half. The 2026-08-29 register premise review proposes the fuller key
# ``sig:<topic>|<desk>|<event_key>``, where ``event_key`` is drawn from a CLOSED
# per-desk vocabulary declared in the action pack. That is the right end state
# and this is deliberately its DESK half, for three reasons that are about what
# can be migrated rather than about what is desirable:
#
#   1. THE EVENT KEY CANNOT BE MIGRATED. No finding in the substrate carries one.
#      A migration can only split stored frames by a property their members
#      already have, and the producing analyst is the only such property. The
#      event key is forward-only work that begins whenever the packs ship; the
#      desk split is the part that can re-home 13,539 existing members today.
#   2. THE EVENT KEY IS A PROMPT-AND-PACK CHANGE (nine action packs, a schema
#      field, and a validator that must REJECT an out-of-vocabulary key — without
#      that rejection it is precisely the K=full failure again). That is a change
#      to what the desks are asked, and it wants the operator's eyes on the
#      vocabulary before it is built.
#   3. THE TWO COMPOSE WITHOUT A SECOND MIGRATION. The grammar below reserves
#      ``#evt:`` as a further suffix and the parsers already read past it, so
#      adding the event key later re-keys nothing that this migration re-keyed:
#      a frame's topic and dimension survive the addition unchanged.
#
# AND WHY THIS DOES NOT LAND BACK AT K=full (1 situation from 11.5k findings).
# That failure had one cause: the key was a hash of a MODEL-GENERATED set whose
# membership churned every cycle, so two reads of the same event never collided.
# ``analyst_id`` is the opposite kind of value — a registered descriptor identity
# stamped by the runtime, drawn from a vocabulary that is closed by the registry
# and cannot churn between two runs of the same unit. The split factor is
# therefore known in advance and bounded by the analyst set (~8 on a country
# desk), not by content. That is the same property the review demands of the
# event vocabulary — "fixed by the action pack, not regenerated by the model each
# cycle" — obtained here for free because the registry already fixes it.
#
# THE MARKER IS DELIBERATELY UNMISTAKABLE. ``|`` already separates the (currently
# unused) entity tail, and a topic is free text lifted from a producer's
# ``category``/``topic``/``event_type`` field or, failing that, from an entity
# token — so no single punctuation character can be assumed absent from it.
# ``#dim:`` is a three-token marker, the dimension itself is sanitized so it can
# never contain one, and :func:`signature_dimension` splits on the LAST
# occurrence. A signature therefore round-trips: topic and dimension both come
# back out, which is what lets ``situation_clustering`` keep resolving
# ``target_id`` from the topic exactly as before.
_SIGNATURE_DIMENSION_MARKER = "#dim:"

#: RESERVED — the story-grained suffix (``#evt:<event_key>``) the closed per-desk
#: vocabulary would append. Nothing writes it yet; the parsers below read past it
#: so that when something does, every key this migration wrote keeps parsing to
#: the same topic and the same dimension. It is a grammar slot, not a stub: no
#: code path claims the feature exists.
_SIGNATURE_EVENT_MARKER = "#evt:"

#: The dimension token for a finding whose producer cannot be identified. Real
#: and reachable: ``analyst_id`` is nullable on ``analyst_outputs``, and the
#: migration must re-home members whose producing row no longer exists. Such
#: findings get their OWN frame rather than being folded into a dimension they
#: may not belong to — an unattributed read is a fact about our bookkeeping, not
#: evidence about somebody else's dimension.
UNATTRIBUTED_DIMENSION = "_unattributed"

#: Cap on the dimension token. Analyst ids are short slugs; the cap is a bound on
#: what an unvalidated producer field can do to an indexed text key.
_DIMENSION_MAX_CHARS = 64


def dimension_token(analyst_id: Any) -> str:
    """The signature-safe dimension token for a producing ``analyst_id``.

    Lowercased, whitespace-stripped, ``#`` and ``|`` folded to ``_`` (so the
    token can never counterfeit either signature separator), capped.
    Empty/absent yields :data:`UNATTRIBUTED_DIMENSION`.

    THIS FUNCTION HAS A SQL TWIN. Migration 0188 computes the same token in
    Postgres to re-key the fleet's stored rows, and a token the two spell
    differently is a DUPLICATE FRAME under the unique index — the one drift that
    would silently undo the whole repair. The twin is asserted row-for-row by
    ``test_dimension_token_python_and_sql_agree``; keep the two edits together.
    """
    token = str(analyst_id or "").strip().lower().replace("#", "_").replace("|", "_")
    return token[:_DIMENSION_MAX_CHARS] if token else UNATTRIBUTED_DIMENSION


def signature_dimension(sig: Any) -> str | None:
    """The dimension a signature carries, or ``None`` if it carries none.

    ``None`` means "pre-#64 key" (or an explicit ``sit:`` key, which is scoped by
    its own producer already and is never dimensioned). A reserved ``#evt:``
    suffix is read past, so a future story-grained key still reports the
    dimension this one wrote.
    """
    text = str(sig or "")
    if not text.startswith("sig:") or _SIGNATURE_DIMENSION_MARKER not in text:
        return None
    tail = text.rsplit(_SIGNATURE_DIMENSION_MARKER, 1)[1]
    return tail.split(_SIGNATURE_EVENT_MARKER, 1)[0] or None


def strip_dimension(sig: Any) -> str:
    """``sig`` with any dimension suffix removed — the pre-#64 topic key.

    What ``situation_clustering`` recovers ``category`` (and therefore
    ``target_id``) from, and what the migration re-keys FROM.
    """
    text = str(sig or "")
    if not text.startswith("sig:") or _SIGNATURE_DIMENSION_MARKER not in text:
        return text
    return text.rsplit(_SIGNATURE_DIMENSION_MARKER, 1)[0]


def with_dimension(sig: Any, analyst_id: Any) -> str:
    """``sig`` keyed to the dimension that produced it — idempotent.

    Only DERIVED (``sig:``) keys are dimensioned. An explicit ``sit:`` key is
    handed to the clusterer by its producer (the composition heads), already
    carries that producer in its own text, and is returned untouched. A signature
    that already carries a dimension is returned untouched too, so this is safe
    to apply to a row of unknown vintage — which is exactly how the live path
    uses it while the fleet is half-migrated.
    """
    text = str(sig or "")
    if not text.startswith("sig:") or _SIGNATURE_DIMENSION_MARKER in text:
        return text
    return f"{text}{_SIGNATURE_DIMENSION_MARKER}{dimension_token(analyst_id)}"


def _explicit_signature(data: Mapping[str, Any]) -> str | None:
    """Explicit ``situation_signature`` / ``situation_id``, or ``None``.

    Reads BOTH the top-level dump AND its nested ``data`` payload sub-dict — the
    persisted ``analyst_outputs.data`` column is the full payload model_dump
    (``FindingPayload`` is ``extra='forbid'``), so an analyst that stamps an
    explicit signature onto its FindingPayload ``data`` (the S8-T3
    meta_findings_synthesizer composition heads set
    ``data['situation_signature']``) lands it at
    ``data->'data'->'situation_signature'``, NOT the top level. Mirrors the
    dual-source read already used by :func:`_entity_tokens` / :func:`_topic`.
    """
    inner = data.get("data")
    sources = (data, inner) if isinstance(inner, Mapping) else (data,)
    for src in sources:
        explicit = src.get("situation_signature") or src.get("situation_id")
        if explicit:
            return str(explicit).strip()
    return None


def derive_signature(
    data: Mapping[str, Any],
    *,
    sub_handler_fallback: str | None = None,
    analyst_id: Any = None,
) -> str | None:
    """Deterministic situation signature for a finding, or ``None``.

    Priority:
      1. Explicit ``situation_id`` / ``situation_signature`` (top-level OR the
         nested payload ``data`` sub-dict — see :func:`_explicit_signature`).
      2. Derived ``sig:<topic>[|<top-K entity tokens>]#dim:<dimension>`` — only
         when there is at least one entity token (so a bare summary finding never
         clusters). ``dimension`` is the producing analyst (see
         :func:`with_dimension` and the mega-frame block comment above); it is
         what stops all seven of a desk's dimensions from collapsing into one
         country-absorbing frame.

    ``analyst_id`` is the PRODUCER of this finding, not the clusterer. Omitted
    (the pure-payload callers) the signature is keyed to
    :data:`UNATTRIBUTED_DIMENSION`, which is honest: a finding whose producer we
    were not told is not evidence about any particular dimension.

    ``None`` means "do not cluster this finding".
    """
    explicit = _explicit_signature(data)
    if explicit:
        return f"sit:{explicit}"

    tokens = _entity_tokens(data)
    if not tokens:
        # Entity gate: a finding with no resolvable entities never clusters
        # (keeps deterministic metrics findings out). Unchanged.
        return None
    topic = _topic(data, sub_handler_fallback)
    if not topic:
        # No topic — anchor on the single strongest entity so entity-only
        # findings still cluster, just loosely.
        topic = tokens[0]
    if _SITUATION_SIGNATURE_ENTITY_K > 0:
        key_tokens = tokens[:_SITUATION_SIGNATURE_ENTITY_K]
        return with_dimension(f"sig:{topic}|{','.join(key_tokens)}", analyst_id)
    return with_dimension(f"sig:{topic}", analyst_id)


# ---------------------------------------------------------------------------
# Clustering core (operates on normalized finding rows)
# ---------------------------------------------------------------------------


def _cluster(
    findings: list[dict[str, Any]],
    *,
    sub_handler_fallback: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Group findings by derived situation signature (only multi-member groups).

    Each finding dict must carry ``id``, ``produced_at`` (comparable) and either
    an already-computed ``situation_signature`` or ``data`` to derive from.
    """
    # Cluster key is (situation_signature, analyst_id): a finding supersedes only
    # PRIOR findings of the SAME analyst within a situation. Different analysts
    # sharing a target-level signature (e.g. the 4 bounded units all stamped
    # `sig:country_g20_us`) are DIFFERENT dimensions and must NOT supersede each
    # other — a country's leadership read is not made stale by its narrative read.
    # Since #64 the SIGNATURE encodes that same dimension, so the composite key is
    # unchanged in behaviour and this stays a no-op for supersession semantics.
    groups: dict[tuple[str, Any], list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        # DQ P6 defense-in-depth (also covers the synthetic deps=None path where
        # the live SQL gate did not run): a COMPOSITION / META producer's report
        # never clusters into a situation.
        if str(f.get("analyst_id") or "") in _COMPOSITION_ANALYST_IDS:
            continue
        sig = f.get("situation_signature")
        if sig:
            # #64 — NORMALIZE A STORED KEY OF UNKNOWN VINTAGE. A finding stamped
            # before the re-key carries the topic-only signature in its COLUMN,
            # and this branch takes the column verbatim, so without this hop the
            # mega-frame would go on being fed by its own back-catalogue until
            # migration 0188 had run. Doing it here means the repair holds from
            # the moment the code deploys and the migration is a consistency
            # pass over stored rows, not a correctness prerequisite racing it.
            sig = with_dimension(sig, f.get("analyst_id"))
        else:
            sig = derive_signature(
                _parse_data(f.get("data")),
                sub_handler_fallback=sub_handler_fallback,
                analyst_id=f.get("analyst_id"),
            )
        if not sig:
            continue
        f["_sig"] = sig
        groups[(sig, f.get("analyst_id"))].append(f)
    # Only (signature, analyst) keys with >1 finding are supersession candidates.
    return {key: rows for key, rows in groups.items() if len(rows) > 1}


def _pick_latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic latest: newest ``produced_at`` then largest ``id``.

    For an evolving situation the freshest assessment is canonical.
    """
    def _key(r: dict[str, Any]) -> tuple[Any, str]:
        return (r.get("produced_at"), str(r.get("id")))

    return max(rows, key=_key)


# ---------------------------------------------------------------------------
# Live-pool path (asyncpg)
# ---------------------------------------------------------------------------


async def _link_supersession(
    conn: Any,
    *,
    superseded_id: Any,
    superseding_id: Any,
    situation_signature: str,
    reason: str,
    score: float,
    produced_by: str | None,
) -> bool:
    """Write one supersession link + stamp the superseded row's pointer.

    Returns True iff a NEW link row was inserted (idempotent — a repeat run over
    the same cluster returns False). NEVER deletes a finding row.
    """
    inserted = await conn.fetchval(
        """
        INSERT INTO finding_supersessions
            (superseded_finding_id, superseding_finding_id,
             situation_signature, reason, score, produced_by)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (superseded_finding_id, superseding_finding_id) DO NOTHING
        RETURNING superseded_finding_id
        """,
        superseded_id, superseding_id, situation_signature, reason, score, produced_by,
    )
    # Stamp the superseded row's pointer (idempotent). NEVER deletes the row —
    # only sets the link columns + the cluster signature for fast reads.
    await conn.execute(
        """
        UPDATE analyst_outputs
        SET superseded_by = $2,
            superseded_at = NOW(),
            situation_signature = $3
        WHERE id = $1
        """,
        superseded_id, superseding_id, situation_signature,
    )
    return inserted is not None


# ---------------------------------------------------------------------------
# FU6 — LIVE composition-head fold
# ---------------------------------------------------------------------------
#
# Composition findings (country/region/world/thematic) are EXCLUDED from the
# situation clusterer above (:data:`_COMPOSITION_ANALYST_IDS`) — they are
# assessment REPORTS, not evolving situations — so nothing folded their heads
# LIVE: every cadence left ANOTHER open head until a migration (0058/0074) folded
# them by hand. After the P6/P7 world target_id fix the current world heads carry
# an EMPTY situation_signature COLUMN, so a plain column-keyed fold would miss
# them. This runs at the composition WRITE path: it stamps the new head's
# situation_signature column and closes every OTHER open head of the SAME analyst
# carrying the SAME raw composition signature — matched on the PERSISTED DATA
# payload (``data->'data'->>'situation_signature'``), so it catches the
# empty-column heads too — mirroring the finding_supersessions audit edge.
# APPEND-ONLY + idempotent (guarded by superseded_by IS NULL + ON CONFLICT).

#: The canonical 'sit:' column prefix derive_signature stamps (so the FU6 column
#: value matches the historical rows + migrations 0058/0074).
_COMPOSITION_SIG_COLUMN_PREFIX = "sit:"

#: The raw data-payload signature prefix meta_findings_synthesizer stamps
#: (``composition:<analyst_id>:<target|world>``) — the defensive guard that keeps
#: this fold from ever touching a first-order (non-composition) finding.
_COMPOSITION_RAW_SIG_PREFIX = "composition:"

_COMPOSITION_FOLD_PRODUCED_BY = "composition_head_fold"


async def fold_prior_composition_heads(
    conn: Any,
    *,
    analyst_id: str | None,
    raw_signature: str | None,
    new_head_id: Any,
    reason: str = "composition head supersession (FU6)",
) -> int:
    """Stamp the new composition head's signature column + close prior heads of the
    SAME ``(analyst_id, raw_signature)``. Returns #prior heads closed.

    ``raw_signature`` is the ``composition:<analyst_id>:<target|world>`` value the
    synthesizer stamps onto ``FindingPayload.data['situation_signature']`` (NO
    'sit:' prefix). Prior open heads are matched on the PERSISTED payload
    (``data->'data'->>'situation_signature'``, COALESCE the top level) so an
    empty-column head — the live world-head symptom — is still caught. No-op when
    an arg is missing or the signature is not a composition signature (so a
    first-order finding is never touched)."""
    if not (analyst_id and raw_signature and new_head_id):
        return 0
    if not str(raw_signature).startswith(_COMPOSITION_RAW_SIG_PREFIX):
        return 0
    column_sig = f"{_COMPOSITION_SIG_COLUMN_PREFIX}{raw_signature}"
    # Stamp the NEW head's column (the write path leaves it NULL for findings).
    await conn.execute(
        """
        UPDATE analyst_outputs
           SET situation_signature = $2
         WHERE id = $1 AND situation_signature IS DISTINCT FROM $2
        """,
        new_head_id, column_sig,
    )
    prior = await conn.fetch(
        """
        SELECT id
          FROM analyst_outputs
         WHERE analyst_id = $1
           AND kind = 'finding'
           AND superseded_by IS NULL
           AND id <> $2
           AND COALESCE(
                   data->'data'->>'situation_signature',
                   data->>'situation_signature'
               ) = $3
        """,
        analyst_id, new_head_id, raw_signature,
    )
    closed = 0
    for row in prior:
        await _link_supersession(
            conn,
            superseded_id=row["id"],
            superseding_id=new_head_id,
            situation_signature=column_sig,
            reason=reason,
            score=_EXACT_SCORE,
            produced_by=_COMPOSITION_FOLD_PRODUCED_BY,
        )
        closed += 1
    return closed


# ---------------------------------------------------------------------------
# M17 — LIVE cross_correlator-head fold (+ blind_spot decay)
# ---------------------------------------------------------------------------
#
# The cross_analyst_correlator (like the compositions) is EXCLUDED from the
# situation clusterer, so nothing folded its heads LIVE — every cadence left
# ANOTHER open meta-observation head (the ~32-stale-head symptom, incl a now-false
# blind_spot). Its findings carry a stable ``data['situation_signature']`` of the
# form ``xcorr:<correlation_type>:<sorted target set>`` (derived by the kind). This
# runs at the correlator WRITE path: (1) stamps the new head's column + closes
# every OTHER open head of the SAME (analyst, xcorr-signature); (2) DECAYS stale
# ``blind_spot`` heads — a blind_spot the correlator has STOPPED asserting (never
# gets a same-signature successor) ages past a TTL and is retired to the new head.
# APPEND-ONLY + idempotent (guarded by superseded_by IS NULL + ON CONFLICT).

#: The raw data-payload signature prefix the correlator stamps — the guard that
#: keeps this fold from ever touching a composition or first-order finding.
_CORRELATION_RAW_SIG_PREFIX = "xcorr:"

_CORRELATION_FOLD_PRODUCED_BY = "correlation_head_fold"


async def fold_prior_correlation_heads(
    conn: Any,
    *,
    analyst_id: str | None,
    raw_signature: str | None,
    new_head_id: Any,
    blind_spot_ttl_hours: int | None = None,
    reason: str = "correlation head supersession (M17)",
) -> tuple[int, int]:
    """Stamp the new correlator head's signature column, close prior SAME-signature
    heads, and decay stale ``blind_spot`` heads. Returns ``(folded, decayed)``.

    ``raw_signature`` is the ``xcorr:<correlation_type>:<targets>`` value the kind
    stamps onto ``FindingPayload.data['situation_signature']``. Prior open heads
    are matched on the PERSISTED payload (``data->'data'->>'situation_signature'``,
    COALESCE the top level). No-op when an arg is missing or the signature is not a
    correlation signature (so a composition / first-order finding is never touched).
    """
    if not (analyst_id and raw_signature and new_head_id):
        return 0, 0
    if not str(raw_signature).startswith(_CORRELATION_RAW_SIG_PREFIX):
        return 0, 0
    column_sig = f"{_COMPOSITION_SIG_COLUMN_PREFIX}{raw_signature}"
    # Stamp the NEW head's column (the write path leaves it NULL for findings).
    await conn.execute(
        """
        UPDATE analyst_outputs
           SET situation_signature = $2
         WHERE id = $1 AND situation_signature IS DISTINCT FROM $2
        """,
        new_head_id, column_sig,
    )
    # (1) Same-signature supersession — close every OTHER open head of this
    #     analyst carrying the SAME xcorr signature.
    prior = await conn.fetch(
        """
        SELECT id
          FROM analyst_outputs
         WHERE analyst_id = $1
           AND kind = 'finding'
           AND superseded_by IS NULL
           AND id <> $2
           AND COALESCE(
                   data->'data'->>'situation_signature',
                   data->>'situation_signature'
               ) = $3
        """,
        analyst_id, new_head_id, raw_signature,
    )
    folded = 0
    for row in prior:
        await _link_supersession(
            conn,
            superseded_id=row["id"],
            superseding_id=new_head_id,
            situation_signature=column_sig,
            reason=reason,
            score=_EXACT_SCORE,
            produced_by=_CORRELATION_FOLD_PRODUCED_BY,
        )
        folded += 1

    # (2) blind_spot decay — retire an OLD open blind_spot head ONLY when its SCOPE
    #     WAS REVISITED. The correlator emits exactly ONE finding per run by strict
    #     priority (contradiction > blind_spot > agreement), so a STILL-OPEN gap
    #     that keeps getting preempted by contradictions/agreements is never
    #     re-emitted — a blanket age-sweep would then silently CLOSE a real,
    #     still-open gap (adversarial FIX #1). Instead, a stale blind_spot H is
    #     decayed only if a NEWER live cross_correlator head N exists whose
    #     referenced-target set is a SUPERSET of (or equal to) H's — evidence the
    #     correlator LOOKED at that scope again and did NOT re-raise the gap. An
    #     un-revisited standing gap stays LIVE (age alone never closes it). The TTL
    #     is the secondary floor. (Same-signature re-assertion is already step 1.)
    decayed = 0
    ttl = int(blind_spot_ttl_hours) if blind_spot_ttl_hours else 0
    if ttl > 0:
        stale = await conn.fetch(
            """
            SELECT h.id
              FROM analyst_outputs h
             WHERE h.analyst_id = $1
               AND h.kind = 'finding'
               AND h.superseded_by IS NULL
               AND h.id <> $2
               AND h.produced_at < NOW() - make_interval(hours => $3)
               AND COALESCE(
                       h.data->'data'->>'correlation_type',
                       h.data->>'correlation_type'
                   ) = 'blind_spot'
               AND EXISTS (
                   SELECT 1
                     FROM analyst_outputs n
                    WHERE n.analyst_id = $1
                      AND n.kind = 'finding'
                      AND n.superseded_by IS NULL
                      AND n.id <> h.id
                      AND n.produced_at > h.produced_at
                      AND COALESCE(h.data->'data'->'xcorr_targets', '[]'::jsonb)
                          <@ COALESCE(n.data->'data'->'xcorr_targets', '[]'::jsonb)
               )
            """,
            analyst_id, new_head_id, ttl,
        )
        for row in stale:
            await _link_supersession(
                conn,
                superseded_id=row["id"],
                superseding_id=new_head_id,
                situation_signature=column_sig,
                reason="blind_spot decay (M17)",
                score=_EXACT_SCORE,
                produced_by=_CORRELATION_FOLD_PRODUCED_BY,
            )
            decayed += 1
    return folded, decayed


async def _fetch_findings(
    conn: Any,
    *,
    lookback_days: int,
    analyst_id: str | None,
    owner_tenant: str | None,
) -> list[dict[str, Any]]:
    """Recent findings still eligible for supersession.

    Only rows that are NOT already superseded (``superseded_by IS NULL``) are
    pulled — once a finding is superseded it stays history, so a re-run only
    ever re-clusters the currently-live set + any new arrivals. ``owner_tenant``
    scopes via the finding's ``data->>'owner_tenant'`` when present (findings
    don't carry a typed tenant column on analyst_outputs).
    """
    clauses = [
        "kind = 'finding'",
        "superseded_by IS NULL",
        f"produced_at > NOW() - INTERVAL '{int(lookback_days)} days'",
    ]
    params: list[Any] = []
    # DQ P6 — never cluster COMPOSITION / META report findings (they are
    # assessment receipts, not evolving-situation findings). Excluding them here
    # is the primary gate: an excluded producer's rows never enter the cluster,
    # so situation_clustering never mints a "situation" for a report stream.
    params.append(list(_COMPOSITION_ANALYST_IDS))
    clauses.append(f"analyst_id <> ALL(${len(params)}::text[])")
    if analyst_id:
        params.append(analyst_id)
        clauses.append(f"analyst_id = ${len(params)}")
    if owner_tenant:
        params.append(owner_tenant)
        clauses.append(f"(data->>'owner_tenant') = ${len(params)}")
    params.append(_MAX_FINDINGS)
    where = " AND ".join(clauses)
    rows = await conn.fetch(
        f"""
        SELECT id, title, data, produced_at, situation_signature, analyst_id
        FROM analyst_outputs
        WHERE {where}
        ORDER BY produced_at DESC, id DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "title": r["title"],
            "data": r["data"],
            "produced_at": r["produced_at"],
            "situation_signature": r["situation_signature"],
            "analyst_id": r["analyst_id"],
        })
    return out


async def _resolve_pool(
    pool: Any,
    *,
    produced_by: str | None,
    analyst_id: str | None,
    owner_tenant: str | None,
    lookback_days: int,
    sub_handler_fallback: str | None,
) -> tuple[int, int, int, list[dict[str, Any]]]:
    """Supersession over the live ``analyst_outputs`` pool.

    Returns ``(clustered_count, superseded_count, latest_count, clusters)``.
    """
    clustered_count = 0
    superseded_count = 0
    clusters: list[dict[str, Any]] = []

    async with pool.acquire() as conn:
        findings = await _fetch_findings(
            conn,
            lookback_days=lookback_days,
            analyst_id=analyst_id,
            owner_tenant=owner_tenant,
        )
        groups = _cluster(findings, sub_handler_fallback=sub_handler_fallback)

        for (sig, _cluster_analyst_id), rows in groups.items():
            latest = _pick_latest(rows)
            latest_id = latest["id"]
            reason = (
                _REASON_EXPLICIT if sig.startswith("sit:") else _REASON_DERIVED
            )
            # Stamp the latest row's signature so the latest-per-situation read
            # (superseded_by IS NULL + situation_signature) finds it.
            await conn.execute(
                """
                UPDATE analyst_outputs
                SET situation_signature = $2
                WHERE id = $1 AND situation_signature IS DISTINCT FROM $2
                """,
                latest_id, sig,
            )
            clustered_count += 1
            superseded_now: list[str] = []
            for r in rows:
                if r["id"] == latest_id:
                    continue
                did_insert = await _link_supersession(
                    conn,
                    superseded_id=r["id"],
                    superseding_id=latest_id,
                    situation_signature=sig,
                    reason=reason,
                    score=_EXACT_SCORE,
                    produced_by=produced_by,
                )
                if did_insert:
                    superseded_count += 1
                superseded_now.append(str(r["id"]))
            clusters.append({
                "situation_signature": sig,
                "latest_finding_id": str(latest_id),
                "superseded_finding_ids": superseded_now,
                "reason": reason,
                "score": _EXACT_SCORE,
            })

        # latest_count: distinct live situations after this run.
        latest_count = await conn.fetchval(
            "SELECT COUNT(DISTINCT situation_signature) FROM analyst_outputs "
            "WHERE kind='finding' AND situation_signature IS NOT NULL "
            "AND superseded_by IS NULL"
        ) or 0

    return clustered_count, superseded_count, int(latest_count), clusters


# ---------------------------------------------------------------------------
# Synthetic-input path (unit tests, no substrate)
# ---------------------------------------------------------------------------


def _resolve_synthetic(
    inputs: list[dict[str, Any]],
    *,
    sub_handler_fallback: str | None,
) -> tuple[int, int, int, list[dict[str, Any]]]:
    """Signature clustering over pre-shaped finding rows (deps=None path).

    Input row shape:
        {"id": str|UUID, "produced_at": comparable,
         "data": {...} | "situation_signature": str}

    Returns ``(clustered_count, superseded_count, latest_count, clusters)``.
    """
    rows = [dict(r) for r in inputs]
    groups = _cluster(rows, sub_handler_fallback=sub_handler_fallback)

    clustered_count = 0
    superseded_count = 0
    clusters: list[dict[str, Any]] = []
    live_sigs: set[str] = set()

    for (sig, _cluster_analyst_id), members in groups.items():
        latest = _pick_latest(members)
        latest_id = str(latest.get("id"))
        superseded = [str(r.get("id")) for r in members if str(r.get("id")) != latest_id]
        reason = _REASON_EXPLICIT if sig.startswith("sit:") else _REASON_DERIVED
        clustered_count += 1
        superseded_count += len(superseded)
        live_sigs.add(sig)
        clusters.append({
            "situation_signature": sig,
            "latest_finding_id": latest_id,
            "superseded_finding_ids": superseded,
            "reason": reason,
            "score": _EXACT_SCORE,
        })
    # singletons (a single finding per signature) are also "live situations".
    for r in rows:
        sig = r.get("_sig")
        if sig:
            live_sigs.add(sig)
    return clustered_count, superseded_count, len(live_sigs), clusters


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    *,
    clustered_count: int,
    superseded_count: int,
    latest_count: int,
    clusters: list[dict[str, Any]] | None,
    target_id: str | None,
) -> FindingPayload:
    title = (
        f"Finding supersession: {clustered_count} situation clusters, "
        f"{superseded_count} findings superseded, {latest_count} latest"
    )
    if target_id:
        title = f"{title} for {target_id}"
    body = "\n".join([
        f"clustered_count={clustered_count}",
        f"superseded_count={superseded_count}",
        f"latest_count={latest_count}",
    ])
    tags = ["deterministic", SUB_HANDLER_NAME]
    if superseded_count:
        tags.append("findings_superseded")
    data: dict[str, Any] = {
        "sub_handler": SUB_HANDLER_NAME,
        "clustered_count": clustered_count,
        "superseded_count": superseded_count,
        "latest_count": latest_count,
    }
    if clusters is not None:
        data["clusters"] = clusters
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data=data,
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Options
    -------
    analyst_id:
        When set on the live path, scopes clustering to one analyst's findings
        (the common case: an evolving-situation analyst re-emitting each cycle).
        Omit to cluster across all finding-producing analysts (the stray
        mis-scoped-duplicate-analyst case the risk item also calls out).
    owner_tenant:
        Restrict to one tenant via ``data->>'owner_tenant'``.
    lookback_days:
        Only findings this recent are eligible (default 30). Older findings are
        settled history, not an evolving situation.
    """
    produced_by = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    analyst_id = options.get("scope_analyst_id") or options.get("cluster_analyst_id")
    owner_tenant = options.get("owner_tenant")
    lookback_days = int(options.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))
    sub_handler_fallback = options.get("topic_fallback")

    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    if pool is not None:
        try:
            clustered_count, superseded_count, latest_count, clusters = await _resolve_pool(
                pool,
                produced_by=produced_by,
                analyst_id=analyst_id,
                owner_tenant=owner_tenant,
                lookback_days=lookback_days,
                sub_handler_fallback=sub_handler_fallback,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("finding_supersession.pool_failed err=%s", exc)
            clustered_count, superseded_count, latest_count, clusters = 0, 0, 0, []
        # Drop per-cluster detail from the finding if it's large — the link
        # rows are the source of truth; the finding is a summary.
        clusters_for_finding = clusters if len(clusters) <= 100 else None
    else:
        clustered_count, superseded_count, latest_count, clusters = _resolve_synthetic(
            inputs, sub_handler_fallback=sub_handler_fallback,
        )
        clusters_for_finding = clusters

    finding = _build_finding(
        clustered_count=clustered_count,
        superseded_count=superseded_count,
        latest_count=latest_count,
        clusters=clusters_for_finding,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "handle",
    "derive_signature",
    "dimension_token",
    "signature_dimension",
    "strip_dimension",
    "with_dimension",
    "SUB_HANDLER_NAME",
    "UNATTRIBUTED_DIMENSION",
]
