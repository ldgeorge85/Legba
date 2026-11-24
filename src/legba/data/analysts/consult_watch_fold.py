# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W4-F1 — the consult watch fold made semantic, and DEC-19 (consult watches stay
out of the scheduled desks' grounding).

WHY. F-4 (:mod:`legba.data.analysts.consult_watch_mint`) folds a consult's watch
candidate onto a standing watch only when the NORMALISED TEXT is identical
(``consult:<sha256(indicator)>``). The model re-phrases every time, so a re-ask
minted new rows: live 2026-10-03, ``major-arms-acquisition indicator observed``
(e6227510, 21:21Z) and ``detection of a major arms acquisition (e.g., new
missile system`` (53d11647, 22:06Z) — the same watch, same desk, same horizon,
two rows. This module adds the semantic leg; the lexical key stays the fast path.

THE RULE. A candidate folds onto an OPEN consult watch when all three hold:

  * the SAME desk (``target_id``; a desk-less turn compares only against other
    desk-less watches);
  * a horizon within :data:`FOLD_HORIZON_DAYS` (±3 days) of the candidate's —
    "by 10-10" and "by 10-17" are different watches even on one subject;
  * the same CLAIM: the two indicators' claim cores are identical, or their
    embeddings (the platform's own ``embed.primary.openai_compat``, the plane
    claim_watch's vector gate uses) sit at cosine >= :data:`FOLD_COSINE_THRESHOLD`.

WHY A CLAIM CORE, measured not assumed (live embedder ``embedding-inno1``,
1024-d, 2026-10-03). On the RAW indicator texts the embedder cannot separate the
duplicate from a different claim: the arms pair scores 0.71, and so does
"major-arms-acquisition indicator observed" against "naval-buildup indicator
observed" — the shared watch framing ("indicator observed", "detection of",
"evidence of", an "(e.g., ...)" example) dominates the vector, and the negation
"... indicator not observed" scores 0.86 against its own affirmative. So both
sides are compared on their CLAIM CORE (:func:`claim_core`): the framing and the
example parenthetical stripped, a negation never stripped. On the cores the
duplicate pair is lexically identical ("major arms acquisition"), a true
paraphrase ("major arms purchase") scores 0.95, the cross pairs on the same desk
score 0.43-0.57 and the negation 0.71 — so 0.86 separates them with room either
side. The recorded vectors are the test fixture
(``tests/data_pkg/fixtures/consult_watch_fold_vectors.json``).

THE FOUR LIVE ROWS (country_watch_ir) are ONE duplicate pair, not two: the arms
pair (both ``by 2026-10-17``) folds; "naval-buildup indicator observed"
(``by 2026-10-10``) and "evidence of an enrichment-level increase" (``by
2026-10-17``) are different claims — and the naval row's horizon is 7 days from
every other row, outside the ±3-day window — so neither folds anywhere.

BOUNDED, ONE CALL, FAILS CLOSED TO MINT. At most :data:`FOLD_MAX_CANDIDATES`
candidates and :data:`FOLD_MAX_PEERS` standing watches per desk, embedded in ONE
batched call under :data:`FOLD_EMBED_TIMEOUT_SECONDS`. No embedder, a failed or
timed-out call, or a failed peer read never refuses a watch: the candidate is
minted and stamped ``fold_check: unavailable``. A fold writes nothing at mint
time — the candidate re-uses the standing row, and its ``watch_minted`` entry
records ``folded_onto`` and the ``similarity``. The one-off
``scripts/fold_consult_watches_once.py`` folds rows minted BEFORE this landed by
stamping the later row's marker (:func:`mark_folded`) — no delete, status
unchanged.

DEC-19 (W3-F16). A minted consult watch with a ``target_id`` used to enter that
desk's STANDING OPEN QUESTIONS grounding block for the scheduled units
(``unit_grounding.read_desk_open_questions``) and the corpus researcher's R-1
backlog (``grounding.resolve_open_questions``). An operator's ad-hoc question
must not steer the scheduled desks' reads, and the R-1 drain CLAIMS a question
(``status='in_progress'``), which would take it out of claim_watch's open set —
the very watcher F-4 minted it for. Both reads EXCLUDE ``created_by='consult'``
rows unless :data:`DESK_GROUNDING_FLAG` is on (default OFF). claim_watch still
watches them: its open-set SQL is untouched.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

#: Cosine floor on the CLAIM CORES (see the module docstring for the measurement).
FOLD_COSINE_THRESHOLD = 0.86
#: A standing watch is a fold target only when its horizon is within this many
#: days of the candidate's.
FOLD_HORIZON_DAYS = 3
#: Candidates embedded per consult turn (the rest fold lexically only and are
#: stamped ``over_cap``).
FOLD_MAX_CANDIDATES = 8
#: Standing consult watches read per desk (newest first).
FOLD_MAX_PEERS = 20
#: Wall clock for the one batched embed call; past it the fold is ``unavailable``.
FOLD_EMBED_TIMEOUT_SECONDS = 10.0
#: Chars of a claim core sent to the embedder.
FOLD_EMBED_CHARS = 512

#: ``fold_check`` vocabulary — how the candidate's fold was decided.
CHECK_KEY = "key"                  # the lexical fast path (identical normalised indicator)
CHECK_CORE = "core"                # identical claim cores, no vector needed
CHECK_SEMANTIC = "semantic"        # cosine on the cores was computed
CHECK_NO_PEERS = "no_peers"        # no standing consult watch on this desk within ±3 days
CHECK_UNAVAILABLE = "unavailable"  # embedder absent/failed or peers unreadable: minted unchecked
CHECK_OVER_CAP = "over_cap"        # beyond FOLD_MAX_CANDIDATES: minted with the lexical checks only

#: DEC-19 — the env flag that puts consult watches BACK into the scheduled desks'
#: open-question grounding. Default OFF.
DESK_GROUNDING_FLAG = "LEGBA_DESK_GROUNDING_INCLUDES_CONSULT_WATCHES"

# Mirrored from consult_watch_mint (drift-guarded by test) so the grounding
# readers can import this leaf without the mint.
_MARKER_KEY = "open_question_origin"
_ORIGIN = "consult"
_CREATED_BY = "consult"

#: Containment literal for "a consult-minted watch" (the shared marker shape).
CONSULT_WATCH_CONTAINMENT = json.dumps(
    [{"marker": _MARKER_KEY, "created_by": _CREATED_BY}], separators=(",", ":"),
)


# ---------------------------------------------------------------------------
# DEC-19 — the grounding exclusion
# ---------------------------------------------------------------------------


def desk_grounding_includes_consult_watches() -> bool:
    """True only when the operator set :data:`DESK_GROUNDING_FLAG` on."""
    raw = os.environ.get(DESK_GROUNDING_FLAG, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def consult_watch_exclusion_sql(column: str = "diagnostic_evidence") -> str:
    """A SQL ``AND`` clause excluding consult-minted watches — ``''`` when the
    flag is on (the read is then byte-identical to its pre-DEC-19 text).
    ``IS NOT TRUE`` keeps a row whose evidence is not an array."""
    if desk_grounding_includes_consult_watches():
        return ""
    return f"\n       AND ({column} @> '{CONSULT_WATCH_CONTAINMENT}'::jsonb) IS NOT TRUE"


def is_consult_watch(diagnostic_evidence: Any) -> bool:
    """Whether a row's evidence carries the consult watch marker."""
    ev = diagnostic_evidence
    if isinstance(ev, (str, bytes)):
        try:
            ev = json.loads(ev)
        except ValueError:
            return False
    if not isinstance(ev, list):
        return False
    return any(
        isinstance(e, Mapping)
        and e.get("marker") == _MARKER_KEY
        and e.get("created_by") == _CREATED_BY
        for e in ev
    )


def drop_consult_watches(rows: Sequence[Any]) -> list[Any]:
    """The Python-side form of the exclusion for a read that ranks in Python
    (the R-1 backlog): rows whose ``diagnostic_evidence`` is a consult watch are
    dropped unless the flag is on."""
    if desk_grounding_includes_consult_watches():
        return list(rows)
    return [r for r in rows if not is_consult_watch(r["diagnostic_evidence"])]


# ---------------------------------------------------------------------------
# The claim core
# ---------------------------------------------------------------------------

_DASHES = {c: "-" for c in map(ord, "‐‑‒–—―−﹘﹣－")}
_EXAMPLE_RE = re.compile(
    r"\s*[(\[]\s*(?:e\.?\s?g\.?|i\.?\s?e\.?|for example|for instance|such as)"
    r"[^)\]]*[)\]]?",
    re.IGNORECASE,
)
_LEAD_FRAME_RE = re.compile(
    r"^(?:(?:the|a|an|any)\s+)?"
    r"(?:(?:first|new|further|fresh|credible|confirmed|clear|direct|public)\s+)*"
    r"(?:detection|evidence|observation|confirmation|signs?|reports?|reporting|"
    r"indications?|indicators?)\s+(?:of|that)\s+",
    re.IGNORECASE,
)
# Never strips a negated observation: "x not observed" keeps its "not observed".
_TRAIL_FRAME_RE = re.compile(
    r"(?<!\bnot)(?<!\bno)(?<!\bnever)\s+(?:indicators?\s+)?"
    r"(?:(?:is|are|was|were|being)\s+)?"
    r"(?:observed|detected|confirmed|reported|seen|recorded)$",
    re.IGNORECASE,
)
_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_SEPARATORS_RE = re.compile(r"[-_/]+")
_EDGE_PUNCT = " \t,;:.()[]\"'"


def claim_core(text: str) -> str:
    """The claim an indicator watches for, with the watch framing removed.

    Casefolded; the ``(e.g., ...)`` example, a leading "detection/evidence/
    signs of" and a trailing "(indicator) observed/detected/confirmed" are
    stripped (a NEGATED trailing observation never is); hyphens become spaces;
    a leading article goes. Falls back to the normalised whole text when the
    stripping would leave nothing."""
    base = " ".join(str(text or "").translate(_DASHES).casefold().split())
    core = _EXAMPLE_RE.sub("", base).strip(_EDGE_PUNCT)
    core = _LEAD_FRAME_RE.sub("", core).strip(_EDGE_PUNCT)
    core = _TRAIL_FRAME_RE.sub("", core).strip(_EDGE_PUNCT)
    core = _ARTICLE_RE.sub("", core)
    core = " ".join(_SEPARATORS_RE.sub(" ", core).split()).strip(_EDGE_PUNCT)
    if core:
        return core
    return " ".join(_SEPARATORS_RE.sub(" ", base).split()).strip(_EDGE_PUNCT)


def cosine(a: Sequence[float] | None, b: Sequence[float] | None) -> float | None:
    """Plain cosine; ``None`` when either side is missing or degenerate."""
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return None
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Peers and the decision (pure)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FoldPeer:
    """One standing consult watch a candidate may fold onto."""

    id: str
    core: str
    resolves_by: datetime
    produced_at: datetime | None = None
    vec: list[float] | None = None


@dataclasses.dataclass(frozen=True)
class FoldDecision:
    """``onto`` is the standing watch's id when the candidate folds."""

    check: str
    onto: str | None = None
    similarity: float | None = None


def within_horizon(a: datetime, b: datetime, days: int = FOLD_HORIZON_DAYS) -> bool:
    return abs(a - b) <= timedelta(days=days)


def _earliest_key(p: FoldPeer) -> tuple:
    return (p.produced_at is None, p.produced_at or datetime.max, p.id)


def decide_fold(
    core: str,
    vec: Sequence[float] | None,
    horizon: datetime,
    peers: Sequence[FoldPeer],
    *,
    threshold: float = FOLD_COSINE_THRESHOLD,
    embedded: bool = True,
) -> FoldDecision:
    """Decide one candidate against the desk's standing watches.

    Identical cores fold first (onto the EARLIEST such watch) with no vector;
    otherwise the best cosine at or above ``threshold`` folds (ties to the
    earliest). ``embedded=False`` (no vectors this turn) with comparable peers
    and no core match is ``unavailable`` — the caller mints."""
    eligible = [p for p in peers if within_horizon(p.resolves_by, horizon)]
    if not eligible:
        return FoldDecision(CHECK_NO_PEERS)
    same = sorted((p for p in eligible if p.core == core), key=_earliest_key)
    if same:
        return FoldDecision(CHECK_CORE, onto=same[0].id, similarity=1.0)
    if not embedded or vec is None:
        return FoldDecision(CHECK_UNAVAILABLE)
    scored = [(s, p) for p in eligible if (s := cosine(vec, p.vec)) is not None]
    if not scored:
        return FoldDecision(CHECK_UNAVAILABLE)
    best = max(s for s, _ in scored)
    if best >= threshold:
        winners = sorted((p for s, p in scored if s == best), key=_earliest_key)
        return FoldDecision(CHECK_SEMANTIC, onto=winners[0].id, similarity=round(best, 4))
    return FoldDecision(CHECK_SEMANTIC, similarity=round(best, 4))


# ---------------------------------------------------------------------------
# The reads and the one batched embed call
# ---------------------------------------------------------------------------

# The desk's standing consult watches: open, not already folded, horizon not
# passed. ``idx_hypotheses_target_id`` (or ``idx_hypotheses_status`` for the
# desk-less bucket) drives it; the marker element is read off the row it matched.
_PEERS_SQL = """
    SELECT h.id, h.thesis, h.produced_at, m.e AS marker
      FROM hypotheses h
      CROSS JOIN LATERAL (
            SELECT e FROM jsonb_array_elements(h.diagnostic_evidence) e
             WHERE e ->> 'marker' = '{marker}' AND e ->> 'origin' = '{origin}'
             LIMIT 1
      ) m
     WHERE h.status = 'open_question'
       AND {target}
       AND h.diagnostic_evidence @> $1::jsonb
       AND NOT (m.e ? 'folded_onto')
       AND (m.e ->> 'resolves_by')::timestamptz > now()
     ORDER BY h.produced_at DESC, h.id
     LIMIT {limit}
"""


def peers_sql(target_id: str | None, *, limit: int = FOLD_MAX_PEERS) -> str:
    target = "h.target_id = $2" if target_id else "h.target_id IS NULL"
    return _PEERS_SQL.format(
        marker=_MARKER_KEY, origin=_ORIGIN, target=target, limit=int(limit),
    )


def _marker_of(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _parse_ts(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def read_fold_peers(
    conn: Any, target_id: str | None, *, limit: int = FOLD_MAX_PEERS,
) -> list[FoldPeer]:
    """The desk's standing consult watches, newest first, at most ``limit``."""
    probe = json.dumps([{"marker": _MARKER_KEY, "origin": _ORIGIN}])
    args: list[Any] = [probe] + ([str(target_id)] if target_id else [])
    rows = await conn.fetch(peers_sql(target_id, limit=limit), *args)
    out: list[FoldPeer] = []
    for r in rows:
        marker = _marker_of(r["marker"])
        horizon = _parse_ts(marker.get("resolves_by"))
        if horizon is None:
            continue
        test = marker.get("resolution_test") or r["thesis"] or ""
        out.append(FoldPeer(
            id=str(r["id"]), core=claim_core(str(test)), resolves_by=horizon,
            produced_at=r["produced_at"],
        ))
    return out


async def embed_batch(
    embedder: Any, texts: Sequence[str], *, timeout: float = FOLD_EMBED_TIMEOUT_SECONDS,
) -> list[list[float]] | None:
    """ONE batched call (``embed_many``) when the client has it, else the texts
    one by one under the same wall clock. ``None`` on ANY failure — the caller
    then mints unchecked; this never raises."""
    if embedder is None or not texts:
        return None
    items = [str(t)[:FOLD_EMBED_CHARS] for t in texts]

    async def _run() -> list[list[float]]:
        many = getattr(embedder, "embed_many", None)
        if callable(many):
            return [list(v) for v in await many(items)]
        return [list(await embedder.embed(t)) for t in items]

    try:
        vecs = await asyncio.wait_for(_run(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — fails closed to mint
        logger.warning("consult.watch_fold.embed_failed n=%d err=%s", len(items), exc)
        return None
    if len(vecs) != len(items) or any(not v for v in vecs):
        logger.warning("consult.watch_fold.embed_short n=%d got=%d", len(items), len(vecs))
        return None
    return vecs


def resolve_fold_embedder(deps: Any) -> Any | None:
    """The consult run's handle on the embed plane: the substrate port's
    port-threaded embedder (``embed.primary.openai_compat``), or None."""
    for holder in (deps, getattr(deps, "substrate", None)):
        emb = getattr(holder, "fold_embedder", None) or getattr(holder, "embedder", None)
        if emb is not None and (
            callable(getattr(emb, "embed_many", None)) or callable(getattr(emb, "embed", None))
        ):
            return emb
    return None


# ---------------------------------------------------------------------------
# The fold plan for one consult turn
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FoldPlan:
    """The desk's peers plus this turn's candidate vectors (by candidate slot)."""

    peers: list[FoldPeer]
    vectors: dict[int, list[float]]
    embedded: bool
    peers_readable: bool = True

    def decide(self, slot: int, core: str, horizon: datetime) -> FoldDecision:
        if not self.peers_readable:
            return FoldDecision(CHECK_UNAVAILABLE)
        if slot >= FOLD_MAX_CANDIDATES:
            decision = decide_fold(core, None, horizon, self.peers, embedded=False)
            if decision.check == CHECK_UNAVAILABLE:
                return FoldDecision(CHECK_OVER_CAP)
            return decision
        return decide_fold(
            core, self.vectors.get(slot), horizon, self.peers, embedded=self.embedded,
        )

    def add_minted(self, slot: int, hid: str, core: str, horizon: datetime,
                   produced_at: datetime) -> None:
        """A row this turn just minted becomes a peer for the later candidates."""
        self.peers.append(FoldPeer(
            id=hid, core=core, resolves_by=horizon, produced_at=produced_at,
            vec=self.vectors.get(slot),
        ))


async def plan_fold(
    conn: Any,
    candidates: Sequence[tuple[int, str, datetime]],
    *,
    target_id: str | None,
    embedder: Any | None,
) -> FoldPlan:
    """Read the desk's peers and embed, in ONE call, the claim cores of the
    candidates that have something to compare against plus those peers.

    ``candidates`` are ``(slot, core, horizon)`` for the candidates whose
    lexical key MISSED. Never raises."""
    try:
        peers = await read_fold_peers(conn, target_id)
    except Exception as exc:  # noqa: BLE001 — fails closed to mint
        logger.warning("consult.watch_fold.peers_failed target=%s err=%s", target_id, exc)
        return FoldPlan(peers=[], vectors={}, embedded=False, peers_readable=False)
    capped = [c for c in candidates if c[0] < FOLD_MAX_CANDIDATES]
    # A candidate needs vectors when a standing watch, or an earlier candidate of
    # this turn, sits within its horizon window under a DIFFERENT core.
    wanted: list[tuple[int, str, datetime]] = []
    for i, (slot, core, horizon) in enumerate(capped):
        others = [(p.core, p.resolves_by) for p in peers] + [
            (c, h) for _s, c, h in capped[:i]
        ] + [(c, h) for _s, c, h in capped[i + 1:]]
        if any(oc != core and within_horizon(oh, horizon) for oc, oh in others):
            wanted.append((slot, core, horizon))
    if not wanted:
        return FoldPlan(peers=peers, vectors={}, embedded=True)
    horizons = [h for _s, _c, h in wanted]
    peer_idx = [
        i for i, p in enumerate(peers)
        if any(within_horizon(p.resolves_by, h) for h in horizons)
    ]
    texts = [c for _s, c, _h in wanted] + [peers[i].core for i in peer_idx]
    vecs = await embed_batch(embedder, texts)
    if vecs is None:
        return FoldPlan(peers=peers, vectors={}, embedded=False)
    vectors = {slot: vecs[k] for k, (slot, _c, _h) in enumerate(wanted)}
    for k, i in enumerate(peer_idx):
        peers[i].vec = vecs[len(wanted) + k]
    return FoldPlan(peers=peers, vectors=vectors, embedded=True)


# ---------------------------------------------------------------------------
# The retro-fold stamp (the one-off script's write)
# ---------------------------------------------------------------------------


async def mark_folded(
    conn: Any,
    row_id: Any,
    *,
    onto: str,
    similarity: float | None,
    check: str,
    now: datetime,
) -> bool:
    """Stamp ``folded_onto`` / ``fold_similarity`` / ``fold_check`` /
    ``folded_at`` on an OPEN consult watch's marker. No delete, status
    unchanged; a row already folded, or no longer open, is left alone (False)."""
    async with conn.transaction():
        raw = await conn.fetchval(
            "SELECT diagnostic_evidence FROM hypotheses "
            "WHERE id = $1::uuid AND status = 'open_question' FOR UPDATE",
            row_id,
        )
        ev = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(ev, list):
            return False
        changed = False
        for e in ev:
            if (isinstance(e, dict) and e.get("marker") == _MARKER_KEY
                    and e.get("origin") == _ORIGIN and "folded_onto" not in e):
                e.update(
                    folded_onto=str(onto), fold_similarity=similarity,
                    fold_check=check, folded_at=now.isoformat(),
                )
                changed = True
                break
        if not changed:
            return False
        await conn.execute(
            "UPDATE hypotheses SET diagnostic_evidence = $2::jsonb, updated_at = now() "
            "WHERE id = $1::uuid",
            row_id, json.dumps(ev),
        )
    return True


__all__ = [
    "CHECK_CORE",
    "CHECK_KEY",
    "CHECK_NO_PEERS",
    "CHECK_OVER_CAP",
    "CHECK_SEMANTIC",
    "CHECK_UNAVAILABLE",
    "CONSULT_WATCH_CONTAINMENT",
    "DESK_GROUNDING_FLAG",
    "FOLD_COSINE_THRESHOLD",
    "FOLD_EMBED_TIMEOUT_SECONDS",
    "FOLD_HORIZON_DAYS",
    "FOLD_MAX_CANDIDATES",
    "FOLD_MAX_PEERS",
    "FoldDecision",
    "FoldPeer",
    "FoldPlan",
    "claim_core",
    "consult_watch_exclusion_sql",
    "cosine",
    "decide_fold",
    "desk_grounding_includes_consult_watches",
    "drop_consult_watches",
    "embed_batch",
    "is_consult_watch",
    "mark_folded",
    "peers_sql",
    "plan_fold",
    "read_fold_peers",
    "resolve_fold_embedder",
    "within_horizon",
]
