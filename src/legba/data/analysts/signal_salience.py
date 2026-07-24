# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``signal_salience`` analyst (S-1) — per-signal CONSEQUENCE scoring.

WHY THIS EXISTS
---------------
Consequence does not exist as DATA anywhere in the tower. Signals carry no
magnitude; facts/findings carry only ``confidence`` (support ≠ consequence); the
journal slice AND ``meta_findings_synthesizer._prepare_input_rows`` are
NEWEST-FIRST. Recency was the ONLY ranking, which is what let a tabloid frame
(Graham) and a World-Cup meme outrank a head-of-state event (Khamenei's funeral).

This sweep scores each raw text signal — ``{event_class, actor_rank, magnitude,
authority}`` — on the $0 core plane (gpt-oss-120b, NEVER Anthropic), stamping
``signals.salience`` (migration 0089). Phase-S consumption (S-2) then orders the
slice by (magnitude, authority, recency) and the advisory judge (S-3) checks the
lead against the top-magnitude input.

CLONE LINEAGE — this is a near-verbatim clone of ``entity_researcher``'s E6c
reclassify pass (the batched $0-plane LLM sweep with an ECHO-BOUND parse,
degrade-to-unscored, and a mark-seen cursor that drains the pool), and of
``signal_summarizer`` (the sweep-over-signals precedent: ``WHERE <marker> IS
NULL`` + stamp-every-examined-row idempotency). The MODEL supplies event_class /
actor_rank / magnitude; ``authority`` is stamped DETERMINISTICALLY from the
source's S1-T8 ``source_class`` (never model-chosen — the anti-tabloid-authority
guard). TRACE_ONLY: the real product is the ``signals.salience`` writes; the
returned finding is the cadence receipt.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from ...runtime.analyst_method import AnalystMethodResult, LLMHandlerLike
from ..provenance.kinds import TRACE_ONLY as _TRACE_ONLY
from ..provenance.models import FindingPayload
from ._tradecraft import with_preamble

logger = logging.getLogger(__name__)

# --- analyst-kind contract (discover_analyst_kinds reads these) --------------
KIND_NAME: str = "signal_salience"
HANDLER_VERSION: str = "0.1.0"
# TRACE_ONLY (the entity_researcher / relationship_reifier precedent): the REAL
# product is the signals.salience column this sweep side-writes; the per-run
# finding is a cadence RECEIPT captured in analyst_traces. Keeps it off the
# findings feed + out of the verify gate (a citation-less meta row floors to 0).
OUTPUT_KIND: object = _TRACE_ONLY

DEFAULT_MAX_ROWS = 300          # signals scored per tick
DEFAULT_BATCH = 12              # signals per LLM call
DEFAULT_MAX_TOKENS = 1400
DEFAULT_TEMPERATURE = 0.0
DEFAULT_WINDOW_HOURS = 96       # bound to a recent window ("backfill 72h then forward")
DEFAULT_SNIPPET_CHARS = 320

# ---------------------------------------------------------------------------
# Closed taxonomies (S-1a). A row that scores outside these is coerced via the
# alias maps below or dropped to the safe generic bucket — never invented.
# ---------------------------------------------------------------------------
EVENT_CLASSES: frozenset[str] = frozenset({
    "leader_death", "kinetic_strike", "mass_casualty", "coup_unrest",
    "diplomatic_rupture", "sanctions_economic", "market_move",
    "disaster_natural", "procurement_routine", "meme_sports_culture", "other",
})
_EVENT_CLASS_ALIASES: dict[str, str] = {
    "death": "leader_death", "assassination": "leader_death",
    "strike": "kinetic_strike", "airstrike": "kinetic_strike",
    "attack": "kinetic_strike", "military": "kinetic_strike",
    "casualty": "mass_casualty", "massacre": "mass_casualty",
    "bombing": "mass_casualty",
    "coup": "coup_unrest", "unrest": "coup_unrest", "protest": "coup_unrest",
    "uprising": "coup_unrest",
    "diplomatic": "diplomatic_rupture", "rupture": "diplomatic_rupture",
    "sanctions": "sanctions_economic", "economic": "sanctions_economic",
    "trade": "sanctions_economic",
    "market": "market_move", "markets": "market_move", "currency": "market_move",
    "disaster": "disaster_natural", "natural": "disaster_natural",
    "earthquake": "disaster_natural", "flood": "disaster_natural",
    "procurement": "procurement_routine", "acquisition": "procurement_routine",
    "defense": "procurement_routine",
    "meme": "meme_sports_culture", "sports": "meme_sports_culture",
    "sport": "meme_sports_culture", "culture": "meme_sports_culture",
    "celebrity": "meme_sports_culture", "viral": "meme_sports_culture",
    "generic": "other", "unknown": "other", "none": "other", "misc": "other",
}

ACTOR_RANKS: frozenset[str] = frozenset({
    "head_of_state", "state_organ", "major_org", "substate", "individual", "none",
})
_ACTOR_RANK_ALIASES: dict[str, str] = {
    "headofstate": "head_of_state", "president": "head_of_state",
    "leader": "head_of_state", "monarch": "head_of_state", "pm": "head_of_state",
    "supremeleader": "head_of_state",
    "stateorgan": "state_organ", "ministry": "state_organ",
    "government": "state_organ", "state": "state_organ", "agency": "state_organ",
    "majororg": "major_org", "organization": "major_org",
    "organisation": "major_org", "org": "major_org", "alliance": "major_org",
    "subnational": "substate", "regional": "substate", "province": "substate",
    "person": "individual", "pundit": "individual", "senator": "individual",
    "official": "individual",
    "": "none", "na": "none", "unknown": "none",
}

# authority (deterministic, from S1-T8 source_class) — NOT model-chosen. Ranks
# frame authority: a wire/official report outranks adversary state_media / an
# unclassified source when they tie on recency (the Graham tabloid-frame fix).
AUTHORITY_BY_SOURCE_CLASS: dict[str, str] = {
    "official": "official",
    "reporting": "reporting",
    "analysis": "analysis",
    "state_media": "state_media",
}
AUTHORITY_RANK: dict[str, int] = {
    "official": 4, "reporting": 3, "analysis": 2, "state_media": 1, "unknown": 0,
}


def _authority_for(source_class: str | None) -> str:
    """Deterministic authority tier from the source's S1-T8 class (never LLM)."""
    return AUTHORITY_BY_SOURCE_CLASS.get((source_class or "").strip().lower(), "unknown")


def salience_sort_key(sal: Mapping[str, Any] | None) -> tuple[float, int]:
    """The consumption sort key (S-2 will reuse this): (magnitude, authority_rank).

    A degraded / missing / unscored salience sorts LAST (magnitude -1.0). Higher
    tuple = MORE salient. Pure — safe to import from the consumption path."""
    if not isinstance(sal, Mapping):
        return (-1.0, 0)
    mag = sal.get("magnitude")
    try:
        mag_f = float(mag)
    except (TypeError, ValueError):
        mag_f = -1.0
    auth = AUTHORITY_RANK.get(str(sal.get("authority") or "unknown"), 0)
    return (mag_f, auth)


# ---------------------------------------------------------------------------
# S-2 / S-1d CONSUMPTION + PROPAGATION helpers (pure — safe to import from the
# journal, inline_target, and meta_findings_synthesizer consumers). Consequence
# propagates UP the tower by MAX: a finding is as consequential as its most
# consequential input, so the world composition can lead with the highest-
# magnitude event in the WHOLE tree — the identity of the original top leaf
# signal (top_signal_id / top_title) rides the max all the way up.
# ---------------------------------------------------------------------------


def magnitude_of(sal: Mapping[str, Any] | None) -> float:
    """The consequence magnitude of a salience dict, or ``-1.0`` when it is
    missing / degraded / unparseable (so an unscored input sorts LAST and never
    outranks a scored one)."""
    if not isinstance(sal, Mapping):
        return -1.0
    try:
        return float(sal.get("magnitude"))
    except (TypeError, ValueError):
        return -1.0


def max_salience(saliences: Iterable[Mapping[str, Any] | None]) -> dict | None:
    """Return a COPY of the highest-magnitude salience dict in the iterable, or
    ``None`` when none carries a usable magnitude. This is the propagation
    primitive for COMPOSITIONS: the caller passes each input finding's stamped
    ``data.salience`` and the winner (which already carries the leaf's
    ``top_signal_id`` / ``top_title``) is forwarded up unchanged."""
    best: Mapping[str, Any] | None = None
    best_mag = -1.0
    for sal in saliences:
        m = magnitude_of(sal)
        if m > best_mag:
            best_mag = m
            best = sal
    if best is None or best_mag < 0.0:
        return None
    return dict(best)


def _signal_row_title(row: Mapping[str, Any]) -> str | None:
    """Best-effort short title from a signal row (column then payload)."""
    t = row.get("title")
    if isinstance(t, str) and t.strip():
        return t.strip()
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        payload = row.get("data")
    if isinstance(payload, Mapping):
        for k in ("title", "headline", "name"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def build_signal_finding_salience(
    signal_rows: Iterable[Mapping[str, Any]],
) -> dict | None:
    """Build a UNIT finding's ``data.salience`` from the raw signal rows it read
    (S-1d, unit path). Picks the MAX-magnitude scored signal and forwards its
    class / actor_rank / authority + a short title + the leaf signal id, so the
    finding is stamped with the identity of the single most consequential thing
    it rests on. Returns ``None`` when no row carries a usable ``salience`` (the
    finding is then left unstamped → it sorts LAST at the composition tier and
    the S-3 judge treats it as unscored, never as low-consequence).

    Each row must expose ``salience`` at the TOP level (the shared slice reader
    selects the ``signals.salience`` column) — distinct from a finding row, whose
    salience is nested in the FindingPayload envelope (``data -> data ->
    salience``); use :func:`max_salience` over the extracted dicts there."""
    best_row: Mapping[str, Any] | None = None
    best_mag = -1.0
    n_scored = 0
    for row in signal_rows:
        if not isinstance(row, Mapping):
            continue
        m = magnitude_of(row.get("salience"))
        if m >= 0.0:
            n_scored += 1
        if m > best_mag:
            best_mag = m
            best_row = row
    if best_row is None or best_mag < 0.0:
        return None
    sal = best_row.get("salience")
    sal = sal if isinstance(sal, Mapping) else {}
    title = _signal_row_title(best_row)
    sid = best_row.get("id")
    return {
        "magnitude": best_mag,
        "event_class": sal.get("event_class"),
        "actor_rank": sal.get("actor_rank"),
        "authority": sal.get("authority"),
        "top_title": (title[:160] if title else None),
        "top_signal_id": (str(sid) if sid is not None else None),
        "source": "signals",
        "n_scored": n_scored,
    }


@dataclass(frozen=True)
class SignalRow:
    id: str
    text: str            # title + snippet, already truncated
    source_class: str | None


@dataclass(frozen=True)
class SalienceVerdict:
    signal_id: str
    event_class: str
    actor_rank: str
    magnitude: float | None
    authority: str
    confidence: float
    degraded: bool = False
    model_id: str | None = None


_SALIENCE_SYSTEM = with_preamble(
    """TASK — score each NEWS SIGNAL for CONSEQUENCE (how much the world would move if this is true), for an intelligence salience index. You are given a numbered list of signals, each with an `id`. For EACH return one score object. Output ONE JSON array, nothing else — one object per signal. ECHO the `id` VERBATIM so the score binds to the right signal:
[{"id": "<id verbatim>", "event_class": "<one class>", "actor_rank": "<one rank>", "magnitude": 0.0-1.0, "confidence": 0.0-1.0}]

event_class (choose ONE): leader_death | kinetic_strike | mass_casualty | coup_unrest | diplomatic_rupture | sanctions_economic | market_move | disaster_natural | procurement_routine | meme_sports_culture | other
  - leader_death = death/incapacitation of a HEAD OF STATE or supreme leader (NOT an ordinary politician's death).
  - kinetic_strike = active military strike/missile/drone/airstrike/cross-border fire.
  - mass_casualty = large loss of life (attack, bombing, massacre).
  - coup_unrest = coup, government collapse, mass uprising, martial law.
  - diplomatic_rupture = severed ties, treaty exit, ambassador expulsion, ultimatum.
  - sanctions_economic = sanctions package, export controls, major economic coercion.
  - market_move = large market/commodity/currency move with geopolitical weight.
  - disaster_natural = earthquake/flood/etc — scale by loss/impact, not mere occurrence.
  - procurement_routine = arms deal, defense-budget line, long-lead acquisition (a routine procurement is NOT escalation).
  - meme_sports_culture = sports result, celebrity, viral/meme, culture item.
  - other = a genuine signal that fits no class — do NOT force-fit; default mid-low.

actor_rank (choose ONE — WHO the signal is about/from): head_of_state | state_organ | major_org | substate | individual | none
  - head_of_state (president/PM/supreme leader) and state_organ (ministry, central bank, SNSC) LIFT magnitude.
  - individual (a senator, a pundit, one person) does NOT by itself make a signal high-magnitude.

magnitude 0.0-1.0 — THE consequence score. Anchors:
  - 0.85-1.0: leader_death(head_of_state), kinetic_strike, mass_casualty.
  - 0.6-0.85: coup_unrest, diplomatic_rupture, major disaster.
  - 0.4-0.65: sanctions_economic, market_move.
  - 0.1-0.35: procurement_routine, an ordinary individual's news.
  - 0.0-0.2: meme_sports_culture.
Ranking matters more than the absolute number: a Supreme-Leader funeral MUST outrank a viral meme; a missile strike MUST outrank a routine arms deal; a head-of-state event MUST outrank an ordinary senator's death.

Worked examples:
  - "Iran's Supreme Leader Khamenei buried; state funeral in Tehran" -> {"event_class":"leader_death","actor_rank":"head_of_state","magnitude":0.95,"confidence":0.9}
  - "US missile strike hits IRGC position near Damascus" -> {"event_class":"kinetic_strike","actor_rank":"state_organ","magnitude":0.9,"confidence":0.85}
  - "Senator Lindsey Graham dies at 70" -> {"event_class":"other","actor_rank":"individual","magnitude":0.3,"confidence":0.7}
  - "Australia signs long-planned submarine procurement contract" -> {"event_class":"procurement_routine","actor_rank":"state_organ","magnitude":0.2,"confidence":0.8}
  - "World Cup final ends in penalty shootout; meme goes viral" -> {"event_class":"meme_sports_culture","actor_rank":"none","magnitude":0.05,"confidence":0.9}

Do NOT invent facts beyond the signal text. If a signal is too thin to judge, use event_class "other" with a LOW magnitude + LOW confidence. Output nothing but the JSON array."""
)


def _signal_text(payload: Any, *, snippet_chars: int = DEFAULT_SNIPPET_CHARS) -> str:
    """Best-effort title + short body from a signal payload (jsonb → dict).

    Signals have no `title` column; the human-readable text lives in `payload`.
    Robust to shape: try title/headline for the head, distilled_body/summary/
    body/text for the snippet. Collapse whitespace + truncate."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    if not isinstance(payload, dict):
        return ""
    title = ""
    for k in ("title", "headline", "name"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            title = v.strip()
            break
    body = ""
    for k in ("distilled_body", "summary", "body", "text", "content", "description"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            body = v.strip()
            break
    text = (title + " — " + body).strip(" —") if body else title
    text = re.sub(r"\s+", " ", text)
    return text[:snippet_chars]


def _coerce_event_class(raw: object) -> str:
    v = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if v in EVENT_CLASSES:
        return v
    return _EVENT_CLASS_ALIASES.get(v.replace("_", ""), "other")


def _coerce_actor_rank(raw: object) -> str:
    v = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if v in ACTOR_RANKS:
        return v
    return _ACTOR_RANK_ALIASES.get(v.replace("_", ""), "none")


def _clamp01(raw: object) -> float | None:
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, f))


# The model's JSON array may be wrapped in prose / a code fence; extract the
# first well-formed array (the entity_researcher._extract_json_array precedent).
def _extract_json_array(content: str) -> list[Any]:
    if not content:
        return []
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _build_prompt(batch: list[SignalRow]) -> str:
    lines = ["SIGNALS:"]
    for i, s in enumerate(batch, 1):
        lines.append(f'{i}. id={s.id} | {s.text or "(no text)"}')
    return "\n".join(lines)


def _parse_salience_batch(
    content: str, batch: list[SignalRow], *, model_id: str | None = None,
) -> list[SalienceVerdict]:
    """Bind each model score to a signal STRICTLY by its ECHOED ``id``. An item
    whose id matches no batch signal is DROPPED; a signal left unbound (or with
    an invalid score) is returned DEGRADED (marks the row, magnitude None).

    There is DELIBERATELY NO positional fallback: a signal's salience is the
    tower's ranking axis, so binding a score to the WRONG signal (which a
    positional guess does whenever the model omits/reorders ids) reproduces the
    exact tabloid-frame failure this whole layer exists to fix (adversarial
    review 2026-07-13: CRITICAL). Unbindable rows degrade to unscored and drain
    on the mark; a systemic echo failure surfaces in the dry-run BEFORE apply."""
    by_id: dict[str, SignalRow] = {s.id: s for s in batch}
    assigned: dict[str, dict] = {}
    for item in _extract_json_array(content):
        if not isinstance(item, dict):
            continue
        rid = item.get("id")
        if not (isinstance(rid, str) and rid.strip() in by_id):
            continue  # no id match → drop the item (NEVER a positional guess)
        target = by_id[rid.strip()]
        if target.id not in assigned:
            assigned[target.id] = item

    out: list[SalienceVerdict] = []
    for s in batch:
        item = assigned.get(s.id)
        authority = _authority_for(s.source_class)
        if not isinstance(item, dict):
            out.append(SalienceVerdict(
                signal_id=s.id, event_class="other", actor_rank="none",
                magnitude=None, authority=authority, confidence=0.0,
                degraded=True, model_id=model_id))
            continue
        mag = _clamp01(item.get("magnitude"))
        conf = _clamp01(item.get("confidence")) or 0.0
        out.append(SalienceVerdict(
            signal_id=s.id,
            event_class=_coerce_event_class(item.get("event_class")),
            actor_rank=_coerce_actor_rank(item.get("actor_rank")),
            magnitude=mag,
            authority=authority,
            confidence=conf,
            degraded=(mag is None),
            model_id=model_id,
        ))
    return out


_SELECT_BATCH_SQL = """
    SELECT s.id, s.payload,
           d.body::jsonb->'scope'->>'source_class' AS source_class
      FROM signals s
      LEFT JOIN source_descriptors d
        ON d.is_head = TRUE AND d.descriptor_id = s.source_id
     WHERE s.salience IS NULL
       AND s.modality = 'text'
       AND s.fetched_at > now() - make_interval(hours => $1)
     ORDER BY s.fetched_at DESC
     LIMIT $2
"""

_WRITE_SALIENCE_SQL = """
    UPDATE signals SET salience = $2::jsonb, updated_at = now()
     WHERE id = $1::uuid
"""


async def select_salience_candidates(
    conn, *, window_hours: int, limit: int,
) -> list[SignalRow]:
    """The bounded un-scored recent-text pool (newest-first)."""
    if limit <= 0:
        return []
    rows = await conn.fetch(_SELECT_BATCH_SQL, int(window_hours), int(limit))
    return [
        SignalRow(
            id=str(r["id"]),
            text=_signal_text(r["payload"]),
            source_class=r["source_class"],
        )
        for r in rows
    ]


def _verdict_to_jsonb(v: SalienceVerdict, *, scored_at: str) -> str:
    return json.dumps({
        "event_class": v.event_class,
        "actor_rank": v.actor_rank,
        "magnitude": v.magnitude,
        "authority": v.authority,
        "confidence": round(v.confidence, 3),
        "model_id": v.model_id,
        "scored_at": scored_at,
        **({"degraded": True} if v.degraded else {}),
    })


async def score_signals(
    conn,
    llm: LLMHandlerLike,
    *,
    apply: bool = False,
    max_rows: int = DEFAULT_MAX_ROWS,
    batch_size: int = DEFAULT_BATCH,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    model_id: str | None = None,
    now_iso: str | None = None,
    sample_size: int = 12,
) -> tuple[int, int, list[dict]]:
    """One salience pass. Returns ``(examined, scored, sample)``.

    Selects the un-scored recent-text pool, LLM-scores in batches (degrade the
    WHOLE batch to unscored on a failed call), and — when ``apply`` — writes
    ``signals.salience`` for EVERY examined row (a real score, or a
    ``degraded:true`` marker), so the pool drains and no row is re-scored or
    retried forever. ``apply=False`` mutates nothing, so the report shows what
    WOULD be written (a dry-run first, the entity_researcher precedent)."""
    candidates = await select_salience_candidates(
        conn, window_hours=window_hours, limit=max_rows)
    if not candidates:
        return (0, 0, [])

    scored_at = now_iso or datetime.now(timezone.utc).isoformat()
    verdicts: list[SalienceVerdict] = []
    for start in range(0, len(candidates), max(1, batch_size)):
        batch = candidates[start : start + batch_size]
        try:
            response = await llm.chat_complete(
                [{"role": "user", "content": _build_prompt(batch)}],
                max_tokens=max_tokens,
                temperature=temperature,
                system=_SALIENCE_SYSTEM,
            )
            content = getattr(response, "content", "") or ""
            batch_v = _parse_salience_batch(content, batch, model_id=model_id)
        except Exception as exc:  # degrade-not-break: whole batch → unscored marker
            logger.warning("signal_salience.batch_failed err=%s", exc)
            authority_by = {s.id: _authority_for(s.source_class) for s in batch}
            batch_v = [
                SalienceVerdict(
                    signal_id=s.id, event_class="other", actor_rank="none",
                    magnitude=None, authority=authority_by[s.id],
                    confidence=0.0, degraded=True, model_id=model_id)
                for s in batch
            ]
        verdicts.extend(batch_v)

    scored = 0
    sample: list[dict] = []
    for v in verdicts:
        if not v.degraded:
            scored += 1
            if len(sample) < sample_size:
                sample.append({
                    "event_class": v.event_class, "actor_rank": v.actor_rank,
                    "magnitude": v.magnitude, "authority": v.authority,
                })
        if not apply:
            continue
        try:
            result = await conn.execute(
                _WRITE_SALIENCE_SQL, v.signal_id,
                _verdict_to_jsonb(v, scored_at=scored_at))
            # A PK UPDATE matches exactly one row; "UPDATE 0" means the signal was
            # deleted between the SELECT and the write (self-correcting — a deleted
            # row never re-enters the pool). Surface it so a real race is visible
            # rather than silently swallowed (review 2026-07-13).
            if isinstance(result, str) and result.rsplit(" ", 1)[-1] == "0":
                logger.debug("signal_salience.write_no_row id=%s", v.signal_id)
        except Exception as exc:  # a single-row failure must not sink the pass
            logger.warning("signal_salience.write_failed id=%s err=%s",
                           v.signal_id, exc)
    return (len(verdicts), scored, sample)


# ===========================================================================
# run_method wrapper — deps built by analyst_deps_builder._build_signal_salience
# from the descriptor's method.llm.primary ($0 core plane) + StandardDeps.
# ===========================================================================


@runtime_checkable
class _BudgetLike(Protocol):
    async def check_envelope(self) -> str: ...


@dataclass
class SignalSalienceDeps:
    """Dep bundle for :func:`run_method`. Built by
    ``analyst_deps_builder._build_signal_salience`` from the resolved primary LLM
    + the run's StandardDeps (pg_pool + budget). ``apply`` gates the ONLY mutating
    behavior (the descriptor option ``score_mode``: ``'apply'`` → True, anything
    else → a DRY-RUN). Ships dry-run; flip to apply with a descriptor PUT."""

    llm: LLMHandlerLike
    pg_pool: Any = None
    budget: _BudgetLike | None = None
    apply: bool = False
    max_rows: int = DEFAULT_MAX_ROWS
    batch_size: int = DEFAULT_BATCH
    window_hours: int = DEFAULT_WINDOW_HOURS
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE


def _empty_summary(reason: str) -> FindingPayload:
    return FindingPayload(
        title=f"Signal salience: no-op ({reason})"[:2048],
        body=f"signal_salience did not run this tick: {reason}"[:65536],
        confidence=1.0, tags=["meta", "signal_salience", "noop"],
        data={"meta": True, "sub_handler": KIND_NAME, "reason": reason},
    )


def _report_summary(
    examined: int, scored: int, sample: list[dict], *,
    apply: bool, model_id: str | None,
) -> FindingPayload:
    mode = "apply" if apply else "dry_run"
    head = (f"signal_salience [{mode}]: examined {examined}, "
            f"scored {scored}, degraded {examined - scored}")
    body = head
    if sample:
        body += "\n\nSample:\n" + "\n".join(
            f"- {s['event_class']}/{s['actor_rank']} mag={s['magnitude']} "
            f"auth={s['authority']}" for s in sample)
    return FindingPayload(
        title=head[:2048],
        body=body[:65536],
        confidence=1.0,
        tags=["meta", "signal_salience", f"mode:{mode}"],
        data={"meta": True, "sub_handler": KIND_NAME, "model_id": model_id,
              "examined": examined, "scored": scored,
              "degraded": examined - scored, "sample": sample},
    )


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: SignalSalienceDeps | LLMHandlerLike,
) -> AnalystMethodResult:
    """One ``signal_salience`` sweep: select un-scored recent text → LLM-score →
    (dry-run|apply) stamp ``signals.salience``. Degrade-not-break: a missing pool
    or any error yields a no-op receipt, never a raise. The REAL product is the
    salience writes; the finding is the run receipt (TRACE_ONLY)."""
    if not isinstance(deps, SignalSalienceDeps):
        deps = SignalSalienceDeps(llm=deps)

    model_id = str(options.get("model_id") or "") or None
    if deps.pg_pool is None:
        return AnalystMethodResult(finding=_empty_summary("no pg_pool"))

    if deps.budget is not None:
        try:
            if (await deps.budget.check_envelope()) != "ok":
                return AnalystMethodResult(finding=_empty_summary("budget_paused"))
        except Exception:  # pragma: no cover - defensive; proceed on a probe error
            pass

    try:
        async with deps.pg_pool.acquire() as conn:
            examined, scored, sample = await score_signals(
                conn, deps.llm,
                apply=deps.apply, max_rows=deps.max_rows,
                batch_size=deps.batch_size, window_hours=deps.window_hours,
                max_tokens=deps.max_tokens, temperature=deps.temperature,
                model_id=model_id,
            )
    except Exception as exc:  # pragma: no cover - degrade-not-break
        logger.warning("signal_salience.run_failed err=%s", exc)
        return AnalystMethodResult(finding=_empty_summary(f"error: {exc}"[:200]))

    logger.info("signal_salience.examined=%d scored=%d apply=%s",
                examined, scored, deps.apply)
    return AnalystMethodResult(
        finding=_report_summary(examined, scored, sample,
                                apply=deps.apply, model_id=model_id))


__all__ = [
    "KIND_NAME", "OUTPUT_KIND", "HANDLER_VERSION",
    "EVENT_CLASSES", "ACTOR_RANKS", "AUTHORITY_BY_SOURCE_CLASS", "AUTHORITY_RANK",
    "SignalRow", "SalienceVerdict", "SignalSalienceDeps",
    "salience_sort_key", "magnitude_of", "max_salience",
    "build_signal_finding_salience",
    "score_signals", "select_salience_candidates",
    "run_method",
]
