# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``entity_researcher`` analyst (E4) — agentic de-fragmentation of entities.

WHY THIS EXISTS
---------------
The NER + backfill pipeline fragments one real-world entity across many
``entity_profiles`` rows (the Khamenei-30 cluster of MULTIPLE people, SNSC in 4
pieces, "the X"/"X" article-twins) and mis-types ~half of them (the title-case→
person heuristic). E1 stopped NEW fragments at the write path; the researcher is
the retroactive cleanup: it CONSUMES the blocked candidate pairs from E3
(:func:`legba.data._entity_candidates.generate_candidates`), ADJUDICATES the
gray band with the core-plane LLM ($0 gpt-oss-120b), and records a verdict per
pair in ``entity_judgement`` (an audit row + a re-adjudication cache keyed by
``pair_key``).

This module is built in layers; THIS file currently holds the ADJUDICATION core
(E4b). It NEVER merges — merge execution (tombstone + redirect via
``merged_into``, reversible + ledgered, MP:DEC-B) is a separate, later step that
only acts on ``same`` verdicts and the deterministic ``auto_merge`` band. Keeping
adjudication side-effect-free (audit writes only) means a bad verdict is caught
by the eval harness (:mod:`legba.data._entity_eval`) BEFORE any graph mutation.

The adjudicator is deliberately CONSERVATIVE: it must default to ``not_same`` /
``unsure`` and only say ``same`` for a true surface variant of ONE referent —
the father/son over-merge (Ali Khamenei vs his son Mojtaba) is the canonical
error to avoid, and the system prompt calls it out explicitly.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from ...runtime.analyst_method import AnalystMethodResult, LLMHandlerLike
from ..provenance.kinds import TRACE_ONLY as _TRACE_ONLY
from ..provenance.models import FindingPayload
from .._entity_candidates import CandidatePair
from ._tradecraft import with_preamble

logger = logging.getLogger(__name__)

# --- analyst-kind contract (discover_analyst_kinds reads these) --------------
KIND_NAME: str = "entity_researcher"
HANDLER_VERSION: str = "0.1.0"
# OUTPUT_KIND is TRACE_ONLY (the relationship_reifier precedent): this global
# maintenance analyst's REAL product is the entity_profiles MERGES it side-
# writes; the per-run summary is a cadence RECEIPT, fully captured in
# analyst_traces.output_payload. TRACE_ONLY keeps it off the findings feed +
# out of the verify gate (a citation-less meta summary would floor to faith 0).
OUTPUT_KIND: object = _TRACE_ONLY

DEFAULT_ADJ_MAX_TOKENS = 1400
DEFAULT_ADJ_BATCH = 10
DEFAULT_ADJ_TEMPERATURE = 0.0

_VALID_VERDICTS = frozenset({"same", "not_same", "unsure"})


@dataclass(frozen=True)
class ClassCorrection:
    """An OPTIONAL adjudicator-surfaced class-label correction (P4 Class 6
    Observation 2, planning/prompt_gallery/p4_judge_bearing.md §Class 6): the
    adjudicator is fed the UPSTREAM (possibly wrong) ``entity_class`` label as
    ground truth framing, and routinely reasons PAST that wrong label to the
    correct read ("Women's Africa Cup of Nations" typed ``person`` for both
    sides, but the model's own "why" — "word order variant, same tournament" —
    implicitly corrects it) with no schema field to SURFACE the correction back
    to the reclassify pipeline. ``side`` names which candidate the correction
    applies to ('a' | 'b'); ``correct_class`` is the model's proposed
    replacement, already validated against the closed taxonomy (an invalid/
    unrecognized class is dropped at parse time — see ``_coerce_class_correction``
    — never persisted). This NEVER changes the same/not_same verdict itself —
    it is an independent, optional observation."""

    side: str  # 'a' | 'b'
    correct_class: str


@dataclass(frozen=True)
class Verdict:
    """One adjudicated pair. ``entity_a``/``entity_b`` are the profile ids in the
    same order as the source :class:`CandidatePair` (left/right)."""

    pair_key: str
    entity_a: str
    entity_b: str
    verdict: str  # 'same' | 'not_same' | 'unsure'
    confidence: float
    justification: str
    decided_by: str = "llm"
    model_id: str | None = None
    #: P4 Class 6 Obs. 2 — optional, parsed from the model's reply when it
    #: flags a wrong upstream entity_class label (see :class:`ClassCorrection`).
    class_correction: ClassCorrection | None = None


_ADJ_SYSTEM = with_preamble(
    """TASK — decide whether two entity NAMES denote the SAME real-world entity, for a knowledge-graph de-duplication pass.

You are given a numbered list of candidate PAIRS. For EACH pair return one verdict. Output ONE JSON array, nothing else — one object per pair, in order. ECHO the two names verbatim in "a" and "b" so the verdict is unambiguously bound to the right pair:
[{"n": 1, "a": "<A name verbatim>", "b": "<B name verbatim>", "verdict": "same"|"not_same"|"unsure", "confidence": 0.0-1.0, "why": "<=15 words", "class_correction": {"side": "a"|"b", "correct_class": "<class>"} (OPTIONAL — see below, omit unless a given class is wrong)}]

DECISION RULES (be conservative — a wrong "same" permanently fuses two distinct entities):
- "same" ONLY when the two names are the identical real referent: a surface variant, alias, abbreviation/acronym, honorific/title variant, or transliteration of ONE entity. (e.g. "Ali Khamenei" = "Ayatollah Ali Khamenei"; "US" = "United States"; "SNSC" = "Supreme National Security Council".)
- TRANSLITERATION (names romanized from Arabic/Persian/Hebrew/Cyrillic scripts): romanization spelling variants ("Hussein"/"Hussain", "Hezbollah"/"Hizbullah"), a single-letter difference in the SAME name ("Khamenei"/"Khameni"), diacritic folds ("Türkiye"/"Turkiye"), and honorific prefixes (Seyyed/Sayyid, Ayatollah, Imam, Sheikh, Haji, Mullah) added to the same personal name all denote ONE entity -> "same". This NEVER relaxes the different-people rule: two people sharing a FAMILY name with DIFFERENT given names (father/son, "Ali Khamenei" vs "Mojtaba Khamenei") stay "not_same" — the transliteration rule applies only when the underlying given+family name is the same after the spelling/honorific variation is accounted for.
- "not_same" when they are DIFFERENT entities, even if closely related:
    * two different PEOPLE who share a surname — "Ali Khamenei" vs his son "Mojtaba Khamenei" = NOT same;
    * a PART vs its WHOLE, a MEMBER vs its GROUP, a person vs the ORG they lead ("Khamenei" vs "the Axis of Resistance" = not_same);
    * a place vs a different place of the same name in another country;
    * a magazine/company vs a geographic feature of the same name ("the Atlantic" the magazine vs "Atlantic" the ocean).
- "unsure" when you genuinely cannot tell from the names + classes alone. NEVER guess "same" to be helpful — default to "unsure".
- The entity CLASS is given; a person and an organization are almost never "same".
- OPTIONAL — CLASS CORRECTION: the entity CLASS you were given for A/B is UPSTREAM data, not guaranteed correct. If your own reasoning about the pair tells you one side's stated class is WRONG (e.g. two names typed "person" that are actually the same sports tournament, or a building typed "person"), you MAY add an OPTIONAL "class_correction" field naming the side and the class you believe is correct: {"side":"a"|"b","correct_class":"<one of: country|organization|corporation|location|person|event|entity>"}. Only add it when you are genuinely confident the GIVEN class is mistyped — omit the field entirely otherwise (most pairs need no correction). When BOTH sides carry the same wrong class, flag side "a". This is a SEPARATE, independent observation — it never changes your same/not_same verdict.

Worked examples:
  1. A="United States" (country) | B="the United States of America" (country) -> {"verdict":"same","confidence":0.98,"why":"full name vs common name, one country"}
  2. A="Ali Khamenei" (person) | B="Mojtaba Khamenei" (person) -> {"verdict":"not_same","confidence":0.95,"why":"father and son, distinct people"}
  3. A="Hezbollah" (organization) | B="Hizbullah" (organization) -> {"verdict":"same","confidence":0.9,"why":"transliteration variants of one group"}
  4. A="Atlantic" (location) | B="the Atlantic" (organization) -> {"verdict":"not_same","confidence":0.9,"why":"ocean vs magazine, different classes"}
  5. A="Ali Khamenei" (person) | B="Seyyed Ali Khameni" (person) -> {"verdict":"same","confidence":0.9,"why":"honorific plus romanization variant, one person"}
  6. A="Imam Hussein" (person) | B="Imam Hussain" (person) -> {"verdict":"same","confidence":0.9,"why":"ei/ai romanization of one name"}
  7. A="Continental Youth Football Championship" (person) | B="the Continental Youth Football Championship" (person) -> {"verdict":"same","confidence":0.95,"why":"same tournament, article variant","class_correction":{"side":"a","correct_class":"event"}}
"""
)


def _build_prompt(batch: list[CandidatePair]) -> str:
    lines = ["PAIRS:"]
    for i, p in enumerate(batch, 1):
        lines.append(
            f'{i}. A="{p.left_name}" (class={p.left_class or "?"}) | '
            f'B="{p.right_name}" (class={p.right_class or "?"})'
        )
    return "\n".join(lines)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _extract_json_array(content: str) -> list:
    """Tolerantly pull the JSON array of verdicts out of a model reply.

    Strips markdown fences, then parses the first ``[...]`` span. Returns [] on
    any failure (the caller then defaults every pair in the batch to unsure).
    """
    if not content:
        return []
    text = _FENCE_RE.sub("", content.strip())
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):  # a single object or {"verdicts":[...]}
            for v in obj.values():
                if isinstance(v, list):
                    return v
            return [obj]
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: grab the outermost bracketed array by hand.
    start, depth = text.find("["), 0
    if start >= 0:
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        return obj if isinstance(obj, list) else []
                    except (json.JSONDecodeError, ValueError):
                        return []
    return []


def _coerce_verdict(raw: object) -> str:
    v = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in _VALID_VERDICTS:
        return v
    if v in ("different", "distinct", "no", "false"):
        return "not_same"
    if v in ("yes", "true", "match", "duplicate"):
        return "same"
    return "unsure"  # anything unrecognized is the safe default


def _norm_name(s: object) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _coerce_class_correction(raw: object) -> ClassCorrection | None:
    """Parse the OPTIONAL ``class_correction`` field (P4 Class 6 Obs. 2).

    Strict on purpose: a malformed/unrecognized ``side`` or ``correct_class``
    (e.g. a hallucinated class outside the closed taxonomy) drops the whole
    correction rather than persisting a garbage hint downstream — the
    reclassify pipeline only ever sees a validated ``side``/``correct_class``
    pair, never raw model text. References ``_VALID_ENTITY_CLASSES`` (the
    closed taxonomy shared with the reclassify pass, defined later in this
    module) — safe via late binding since this is only ever called at runtime,
    after the module has finished loading."""
    if not isinstance(raw, Mapping):
        return None
    side = str(raw.get("side") or "").strip().lower()
    if side not in ("a", "b"):
        return None
    correct_class = str(raw.get("correct_class") or "").strip().lower()
    if correct_class not in _VALID_ENTITY_CLASSES:
        return None
    return ClassCorrection(side=side, correct_class=correct_class)


def _parse_batch(content: str, batch: list[CandidatePair]) -> list[Verdict]:
    """Bind each model verdict to a pair by its ECHOED names (authoritative),
    falling back to the 1-based ``n`` ONLY when an item echoes no names.

    Adversarial-review HIGH fix: a verdict whose echoed names match NO pair is
    DROPPED, so an off-by-one / mislabeled ``n`` can never cross-assign a 'same'
    to the wrong pair (the Ali/Mojtaba inheritance bug). Any pair left unbound
    becomes ``unsure`` — never a silent ``same``."""
    # Index the batch by the unordered normalized name-set (a batch never holds
    # two pairs with the same set — pair_key dedup upstream guarantees it).
    pair_by_names: dict[frozenset[str], CandidatePair] = {
        frozenset({_norm_name(p.left_name), _norm_name(p.right_name)}): p
        for p in batch
    }

    assigned: dict[str, dict] = {}  # pair_key -> item
    for item in _extract_json_array(content):
        if not isinstance(item, dict):
            continue
        a, b = item.get("a"), item.get("b")
        target: CandidatePair | None = None
        if a is not None and b is not None:
            # Names echoed => AUTHORITATIVE: match a real pair or DROP the item.
            target = pair_by_names.get(frozenset({_norm_name(a), _norm_name(b)}))
        else:
            # No names echoed => fall back to positional n (back-compat).
            try:
                n = int(item.get("n"))
            except (TypeError, ValueError):
                n = None
            if n is not None and 1 <= n <= len(batch):
                target = batch[n - 1]
        if target is not None and target.pair_key not in assigned:
            assigned[target.pair_key] = item

    out: list[Verdict] = []
    for p in batch:
        item = assigned.get(p.pair_key, {})
        verdict = _coerce_verdict(item.get("verdict"))
        try:
            conf = float(item.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        why = str(item.get("why") or item.get("justification") or "")[:600]
        if not item:
            why = why or "no matching verdict for this pair; defaulted to unsure"
        out.append(
            Verdict(
                pair_key=p.pair_key,
                entity_a=p.left_id,
                entity_b=p.right_id,
                verdict=verdict,
                confidence=conf,
                justification=why,
                class_correction=_coerce_class_correction(item.get("class_correction")),
            )
        )
    return out


async def _load_cached(conn, pair_keys: list[str]) -> dict[str, Verdict]:
    """Fetch already-decided verdicts for these pair_keys (the re-adjudication
    cache). Returns {} if the table is absent or nothing is cached."""
    if not pair_keys:
        return {}
    rows = await conn.fetch(
        """
        SELECT pair_key, entity_a, entity_b, verdict, confidence,
               justification, decided_by, model_id
          FROM entity_judgement
         WHERE pair_key = ANY($1::text[])
        """,
        list(pair_keys),
    )
    out: dict[str, Verdict] = {}
    for r in rows:
        out[str(r["pair_key"])] = Verdict(
            pair_key=str(r["pair_key"]),
            entity_a=str(r["entity_a"]) if r["entity_a"] else "",
            entity_b=str(r["entity_b"]) if r["entity_b"] else "",
            verdict=str(r["verdict"]),
            confidence=float(r["confidence"] or 0.0),
            justification=str(r["justification"] or ""),
            decided_by=str(r["decided_by"] or "llm"),
            model_id=str(r["model_id"]) if r["model_id"] else None,
        )
    return out


async def _persist(conn, verdicts: list[Verdict]) -> None:
    """Upsert verdicts into entity_judgement (idempotent on pair_key). A
    re-adjudication overwrites the prior row but a HUMAN decision is never
    clobbered by an llm/rule pass."""
    for v in verdicts:
        await conn.execute(
            """
            INSERT INTO entity_judgement
                (pair_key, entity_a, entity_b, verdict, justification,
                 decided_by, model_id, confidence)
            VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, $8)
            ON CONFLICT (pair_key) DO UPDATE SET
                verdict       = EXCLUDED.verdict,
                justification = EXCLUDED.justification,
                decided_by    = EXCLUDED.decided_by,
                model_id      = EXCLUDED.model_id,
                confidence    = EXCLUDED.confidence,
                decided_at    = now()
            WHERE entity_judgement.decided_by <> 'human'
            """,
            v.pair_key, v.entity_a or None, v.entity_b or None, v.verdict,
            v.justification, v.decided_by, v.model_id, v.confidence,
        )


async def _record_class_correction_hint(
    conn, target_id: str, correction: ClassCorrection, *, pair_key: str,
) -> bool:
    """Best-effort: stamp the flagged side's ``entity_profiles.data`` with an
    ``adjudicator_class_hint`` note (P4 Class 6 Obs. 2 — the adjudicator
    routinely reasons past a WRONG upstream ``entity_class`` label it was fed
    as ground truth, with no channel to report it back to the reclassify
    pipeline). Reuses the SAME ``data`` jsonb column + ``jsonb_set`` idiom the
    reclassify pass already reads/writes (``reclass_seen_at`` / ``reclass``,
    below) — CHEAP: no new column, no new migration. The reclassify pool SQL
    (:data:`_RECLASS_SUSPECT_SQL` / :data:`_RECLASS_ENTITY_SUSPECT_SQL`)
    queue-jumps a hinted row ahead of the lexical-suspect heuristic (see their
    ORDER BY), so a hint concretely changes queue PRIORITY — it never itself
    reclassifies; the LLM reclass pass still makes the final call. Never
    raises: a write failure degrades to "hint not recorded", exactly like
    every other best-effort annotation in this module (mirrors
    ``reclassify_entities``'s own apply-failure handling)."""
    if conn is None or not target_id:
        return False
    try:
        await conn.execute(
            """
            UPDATE entity_profiles
               SET data = jsonb_set(
                     COALESCE(data, '{}'::jsonb),
                     '{adjudicator_class_hint}',
                     $2::jsonb, true
                   ),
                   updated_at = now()
             WHERE id = $1::uuid
            """,
            target_id,
            json.dumps({
                "correct_class": correction.correct_class,
                "pair_key": pair_key,
                "flagged_by": "entity_researcher.adjudicate",
            }),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort, never sinks the batch
        logger.warning(
            "entity_researcher.class_correction_hint_failed pair_key=%s err=%s",
            pair_key, exc,
        )
        return False


async def adjudicate_pairs(
    conn,
    llm: LLMHandlerLike,
    pairs: list[CandidatePair],
    *,
    model_id: str | None = None,
    batch_size: int = DEFAULT_ADJ_BATCH,
    max_tokens: int = DEFAULT_ADJ_MAX_TOKENS,
    temperature: float = DEFAULT_ADJ_TEMPERATURE,
    use_cache: bool = True,
    persist: bool = True,
    apply_class_corrections: bool = False,
) -> list[Verdict]:
    """Adjudicate candidate ``pairs`` into same/not_same/unsure verdicts.

    Cached pairs (already in ``entity_judgement``) are returned as-is and never
    re-sent to the LLM. Remaining pairs are batched; a batch whose LLM call
    raises or returns unparseable text degrades every pair in it to ``unsure``
    (never a silent ``same``) so one bad batch can't corrupt the graph. When
    ``persist`` is true every NEW verdict is upserted (human decisions are
    preserved). Returns verdicts for ALL input pairs, cache + fresh.

    ``apply_class_corrections`` (P4 Class 6 Obs. 2, default False — mirrors
    ``reclassify_entities``'s own ``apply`` gate, so a dry-run pass mutates
    NO ``entity_profiles`` row, exactly like every other entity_profiles write
    in this module): when true, every parsed :class:`ClassCorrection` is
    recorded onto the flagged side's ``entity_profiles.data`` (best-effort —
    see :func:`_record_class_correction_hint`). The COUNT of verdicts carrying
    a ``class_correction`` (whether or not this flag is set) is always
    available to the caller via ``Verdict.class_correction`` on the returned
    list — ``run_entity_research`` is the counter (``ResearchReport.
    class_corrections_flagged``).
    """
    if not pairs:
        return []

    results: dict[str, Verdict] = {}
    remaining = list(pairs)

    if use_cache:
        cached = await _load_cached(conn, [p.pair_key for p in pairs])
        if cached:
            results.update(cached)
            remaining = [p for p in pairs if p.pair_key not in cached]

    for start in range(0, len(remaining), max(1, batch_size)):
        batch = remaining[start : start + batch_size]
        try:
            response = await llm.chat_complete(
                [{"role": "user", "content": _build_prompt(batch)}],
                max_tokens=max_tokens,
                temperature=temperature,
                system=_ADJ_SYSTEM,
            )
            content = getattr(response, "content", "") or ""
            batch_verdicts = _parse_batch(content, batch)
        except Exception as exc:  # degrade-not-break: whole batch -> unsure
            logger.warning("entity_researcher.adjudicate_batch_failed err=%s", exc)
            batch_verdicts = [
                Verdict(
                    pair_key=p.pair_key, entity_a=p.left_id, entity_b=p.right_id,
                    verdict="unsure", confidence=0.0,
                    justification=f"adjudication error: {exc}",
                )
                for p in batch
            ]
        if model_id:
            batch_verdicts = [
                Verdict(**{**v.__dict__, "model_id": model_id}) for v in batch_verdicts
            ]
        if persist:
            try:
                await _persist(conn, batch_verdicts)
            except Exception as exc:  # audit-write failure must not lose verdicts
                logger.warning("entity_researcher.persist_failed err=%s", exc)
        if apply_class_corrections:
            pair_by_key = {p.pair_key: p for p in batch}
            for v in batch_verdicts:
                if v.class_correction is None:
                    continue
                p = pair_by_key.get(v.pair_key)
                if p is None:
                    continue
                target_id = (
                    p.left_id if v.class_correction.side == "a" else p.right_id
                )
                await _record_class_correction_hint(
                    conn, target_id, v.class_correction, pair_key=v.pair_key,
                )
        for v in batch_verdicts:
            results[v.pair_key] = v

    # Preserve input order.
    return [results[p.pair_key] for p in pairs if p.pair_key in results]


# ===========================================================================
# E4c — MERGE EXECUTOR (tombstone + redirect, reversible + ledgered).
#
# The graph-mutating step: for a 'same' verdict (or the deterministic auto_merge
# band) elect a KEEPER, set the loser's ``merged_into`` = keeper (0086 redirect;
# resolve_entity chases it) + mark it gc_status='merged' (so future keeper
# elections + candidate generation skip it), and fold the loser's surface into
# the keeper's ``data.merged_aliases`` — the E1↔E4 synergy: once folded, E1's
# resolve_keeper canonicalizes any FUTURE mention of the loser surface onto the
# keeper at write time. Fully reversible via :func:`unmerge`. NON-destructive:
# no row is deleted (junk DELETES are E6, backup-then-delete per MP:DEC-B).
# ===========================================================================

import json as _json  # local alias (module already imports json)

#: Keeper class priority (lower = kept). Mirrors _entity_resolve/_entity_canon.
_KEEPER_CLASS_RANK: dict[str, int] = {
    "country": 0, "organization": 1, "corporation": 1,
    "location": 2, "person": 3, "entity": 4,
}


def _rank(cls: str | None) -> int:
    return _KEEPER_CLASS_RANK.get((cls or "entity").strip().lower(), 5)


@dataclass(frozen=True)
class MergeReport:
    merged: int
    skipped: int
    pairs: tuple[tuple[str, str], ...]  # (keeper_id, loser_id) applied


async def elect_keeper(conn, id_a: str, id_b: str) -> tuple[str, str] | None:
    """Pick (keeper_id, loser_id) between two profile ids: highest class-priority,
    then higher completeness_score, then older created_at, then lexicographically
    smaller id (deterministic). Returns None if either row is missing or the ids
    are equal after resolving redirects."""
    if not id_a or not id_b:
        return None
    rows = await conn.fetch(
        """
        SELECT id, entity_class, COALESCE(completeness_score, 0) AS comp,
               created_at
          FROM entity_profiles
         WHERE id = ANY($1::uuid[])
        """,
        [id_a, id_b],
    )
    by_id = {str(r["id"]): r for r in rows}
    ra, rb = by_id.get(str(id_a)), by_id.get(str(id_b))
    if ra is None or rb is None or str(id_a) == str(id_b):
        return None

    def _key(r):
        # sort ascending; the FIRST is the keeper -> smallest tuple wins.
        return (
            _rank(r["entity_class"]),
            -float(r["comp"] or 0.0),
            r["created_at"],
            str(r["id"]),
        )

    keeper, loser = sorted((ra, rb), key=_key)
    return str(keeper["id"]), str(loser["id"])


async def merge_pair(
    conn, keeper_id: str, loser_id: str, *,
    reason: str = "", decided_by: str = "llm",
) -> bool:
    """Tombstone-merge ``loser_id`` into ``keeper_id`` (reversible). Resolves both
    ids to their terminal survivors first (never chains onto a tombstone, never
    forms a cycle). Idempotent: a loser already pointing at the keeper is a no-op
    success. Returns True when a merge was applied."""
    if not keeper_id or not loser_id:
        return False
    keeper = str(await conn.fetchval("SELECT resolve_entity($1::uuid)", keeper_id))
    loser = str(await conn.fetchval("SELECT resolve_entity($1::uuid)", loser_id))
    if keeper == loser:
        return False  # already same terminal (or would self-merge / cycle)

    async with conn.transaction():
        # Fold the loser's surface + its own folded aliases into the keeper so
        # E1's resolve_keeper canonicalizes future mentions onto the keeper.
        krow = await conn.fetchrow(
            "SELECT canonical_name, COALESCE(data, '{}'::jsonb) AS data "
            "FROM entity_profiles WHERE id = $1::uuid FOR UPDATE", keeper)
        lrow = await conn.fetchrow(
            "SELECT canonical_name, COALESCE(data, '{}'::jsonb) AS data "
            "FROM entity_profiles WHERE id = $1::uuid FOR UPDATE", loser)
        if krow is None or lrow is None:
            return False
        kdata = dict(_json.loads(krow["data"]) if isinstance(krow["data"], str)
                     else krow["data"])
        ldata = (_json.loads(lrow["data"]) if isinstance(lrow["data"], str)
                 else lrow["data"]) or {}
        aliases = list(kdata.get("merged_aliases") or [])
        seen = {str(a).strip().lower() for a in aliases}
        for cand in [lrow["canonical_name"], *(ldata.get("merged_aliases") or [])]:
            c = str(cand or "").strip()
            if c and c.lower() not in seen and c.lower() != str(
                    krow["canonical_name"]).strip().lower():
                aliases.append(c)
                seen.add(c.lower())
        kdata["merged_aliases"] = aliases
        await conn.execute(
            "UPDATE entity_profiles SET data = $2::jsonb, updated_at = now() "
            "WHERE id = $1::uuid", keeper, _json.dumps(kdata))

        # Tombstone the loser: redirect + gc_status merged + a small ledger note.
        ldata_new = dict(ldata)
        ldata_new["gc_status"] = "merged"
        ldata_new["merge"] = {
            "into": keeper, "reason": str(reason or "")[:400],
            "decided_by": decided_by,
        }
        await conn.execute(
            "UPDATE entity_profiles "
            "SET merged_into = $2::uuid, data = $3::jsonb, updated_at = now() "
            "WHERE id = $1::uuid", loser, keeper, _json.dumps(ldata_new))
    return True


async def unmerge(conn, loser_id: str) -> bool:
    """Reverse a tombstone-merge: clear ``merged_into`` + gc_status='merged' on
    the loser (the keeper's alias fold is left — harmless, and re-derivable).
    Returns True when a tombstone was reopened."""
    row = await conn.fetchrow(
        "SELECT COALESCE(data, '{}'::jsonb) AS data, merged_into "
        "FROM entity_profiles WHERE id = $1::uuid", loser_id)
    if row is None or row["merged_into"] is None:
        return False
    data = dict(_json.loads(row["data"]) if isinstance(row["data"], str)
                else row["data"])
    data.pop("merge", None)
    if data.get("gc_status") == "merged":
        data.pop("gc_status", None)
    await conn.execute(
        "UPDATE entity_profiles "
        "SET merged_into = NULL, data = $2::jsonb, updated_at = now() "
        "WHERE id = $1::uuid", loser_id, _json.dumps(data))
    return True


async def execute_merges(
    conn,
    verdicts: list[Verdict],
    pairs: list[CandidatePair],
    *,
    min_confidence: float = 0.75,
    apply_auto_band: bool = True,
    dry_run: bool = False,
) -> MergeReport:
    """Apply merges for the confident 'same' verdicts + (optionally) the
    deterministic ``auto_merge`` band. Elects the keeper per pair, tombstone-
    merges the loser (reversible). ``dry_run`` reports what WOULD merge without
    mutating. A pair missing from the graph, or that resolves to one entity, is
    skipped (not an error)."""
    by_key = {p.pair_key: p for p in pairs}
    verdict_by_key = {v.pair_key: v for v in verdicts}
    applied: list[tuple[str, str]] = []
    skipped = 0

    for key, pair in by_key.items():
        v = verdict_by_key.get(key)
        want = (
            (v is not None and v.verdict == "same" and v.confidence >= min_confidence)
            or (apply_auto_band and pair.band == "auto_merge")
        )
        if not want:
            continue
        elected = await elect_keeper(conn, pair.left_id, pair.right_id)
        if elected is None:
            skipped += 1
            continue
        keeper_id, loser_id = elected
        if dry_run:
            applied.append((keeper_id, loser_id))
            continue
        reason = (
            f"{v.verdict}@{v.confidence:.2f}: {v.justification}"
            if v is not None else f"deterministic auto_merge ({pair.block_key})"
        )
        decided_by = "rule" if (v is None or pair.band == "auto_merge"
                                and v.verdict != "same") else "llm"
        if await merge_pair(conn, keeper_id, loser_id, reason=reason,
                            decided_by=decided_by):
            applied.append((keeper_id, loser_id))
        else:
            skipped += 1

    return MergeReport(merged=len(applied), skipped=skipped, pairs=tuple(applied))


# ===========================================================================
# E4d (core) — the framework-agnostic ORCHESTRATION: generate -> adjudicate ->
# execute. run_method (the analyst-framework entry) is a thin wrapper over this;
# keeping the pipeline here lets it be built + tested without the descriptor /
# deps-builder / actor wiring, and lets a CLI / one-off cleanup (E6) reuse it.
# ===========================================================================

from .._entity_candidates import (
    DEFAULT_MIN_TRGM,
    DEFAULT_TRGM_MIN_DEGREE,
    generate_candidates,
)

DEFAULT_MAX_PAIRS = 200
DEFAULT_SAME_MIN_CONF = 0.75


@dataclass(frozen=True)
class ResearchReport:
    mode: str  # 'dry_run' | 'apply'
    candidates: int
    gray: int
    auto: int
    adjudicated: int
    same: int
    not_same: int
    unsure: int
    merges_applied: int
    merges_skipped: int
    sample: tuple[dict, ...]  # up to N {keeper, loser, verdict, why} for the finding
    # E6c reclassify pass (0 / empty when reclassify_max == 0). #219: these
    # totals are now the SUM across whichever pools ran (person + entity);
    # reclass_by_class carries the per-pool breakdown.
    reclass_examined: int = 0
    reclass_changed: int = 0
    reclass_sample: tuple[dict, ...] = ()
    #: #219 — per-source-class breakdown, e.g. {"person": {"examined": 80,
    #: "changed": 12}, "entity": {"examined": 20, "changed": 5}}. Empty when
    #: reclassify_max == 0 or every pool it was given was allotted 0 rows.
    reclass_by_class: dict[str, dict[str, int]] = field(default_factory=dict)
    #: P4 Class 6 Obs. 2 (QW1-D fix 3) — how many of THIS pass's adjudicated
    #: verdicts carried an optional ``class_correction`` (a wrong upstream
    #: entity_class the adjudicator's own reasoning flagged). Counted
    #: regardless of ``mode`` (a pure observation, no mutation); the row-level
    #: hint itself is only WRITTEN to entity_profiles.data in apply mode (see
    #: ``adjudicate_pairs``'s ``apply_class_corrections``).
    class_corrections_flagged: int = 0
    #: up to N {name, side, correct_class, why} for the finding — mirrors
    #: ``reclass_sample``'s shape.
    class_correction_sample: tuple[dict, ...] = ()

    def summary(self) -> str:
        verb = "would merge" if self.mode == "dry_run" else "merged"
        s = (
            f"entity_researcher [{self.mode}]: {self.candidates} candidates "
            f"({self.auto} auto / {self.gray} gray); adjudicated {self.adjudicated} "
            f"(same {self.same} / not_same {self.not_same} / unsure {self.unsure}); "
            f"{verb} {self.merges_applied} (skipped {self.merges_skipped})."
        )
        if self.reclass_examined:
            rverb = "would reclassify" if self.mode == "dry_run" else "reclassified"
            s += (f" reclassify: examined {self.reclass_examined}, "
                  f"{rverb} {self.reclass_changed}.")
            if self.reclass_by_class:
                breakdown = ", ".join(
                    f"{cls}={v['examined']}/{v['changed']}"
                    for cls, v in self.reclass_by_class.items()
                )
                s += f" [{breakdown} examined/changed]"
        if self.class_corrections_flagged:
            s += (
                f" class_correction: {self.class_corrections_flagged} flagged "
                "by the adjudicator."
            )
        return s

    def to_data(self) -> dict:
        return {
            "mode": self.mode, "candidates": self.candidates,
            "gray": self.gray, "auto": self.auto,
            "adjudicated": self.adjudicated, "same": self.same,
            "not_same": self.not_same, "unsure": self.unsure,
            "merges_applied": self.merges_applied,
            "merges_skipped": self.merges_skipped,
            "sample": list(self.sample),
            "reclass_examined": self.reclass_examined,
            "reclass_changed": self.reclass_changed,
            "reclass_sample": list(self.reclass_sample),
            "reclass_by_class": dict(self.reclass_by_class),
            "class_corrections_flagged": self.class_corrections_flagged,
            "class_correction_sample": list(self.class_correction_sample),
        }


# ===========================================================================
# E6c — RECLASSIFY pass (the person-skew fix). The NER title-case->person default
# mis-typed ~half the graph (oceans, aircraft, orgs, events landed in `person`,
# which BLOCKS the merge — persons never auto-merge, so a mis-classed "the X" can
# never fold onto its bare "X" twin). This pass LLM-classifies the SUSPECT person
# rows (article-prefixed OR carrying an org/location/event lexical signal) into
# the closed class set and rewrites entity_class for a confident change, storing
# data.reclass = {from,...} for reversibility. Conservative: a real personal name
# is confirmed `person` (a no-op) — only a clear mis-type moves. Bounded per tick
# (reclassify_max); every EXAMINED row is marked data.reclass_seen_at so the pool
# drains across ticks and is never re-sent to the LLM. Mirrors the adjudication
# machinery (batched, echo-bound parse, degrade-to-no-change).
#
# #219 (2026-07-23) — GENERIC-ENTITY EXTENSION. The generic `entity` bucket has
# the SAME problem as `person` did: DQ M6 (2026-07-06 audit) measured 29.5% of
# entity_profiles fall into it, and it is the fallback for BOTH a genuinely
# unclear referent AND a real organization/location/event the deterministic
# gazetteers (is_org_surface / is_region_surface / is_known_org_surface) never
# caught — R-2 (`6f270a2`, 2026-07-21) made this WORSE on purpose: an article-
# prefixed surface that would land `person` now demotes to `entity` instead (the
# safer of two imperfect buckets), which means `entity` is now also catching the
# person-pool's overflow. `entity` never auto-merges either (the generic bucket
# is GRAY-only in `_entity_candidates.generate_candidates`), so a real org stuck
# there can still fold onto its true-class twin once reclassified, same as the
# person case. This section GENERALIZES the pool selection + LLM pass to run
# over EITHER source class (`select_reclass_candidates` / `reclassify_entities`
# now take `source_class`), reusing the IDENTICAL response schema, echo-bound
# parse, degrade-to-no-change, and reversibility ledger — only the pool SQL and
# one framing sentence in the system prompt differ per source class. The two
# pools SHARE the existing `reclassify_max` budget (split, not additive — see
# `reclass_entity_share` on `run_entity_research` / `EntityResearcherDeps`) so
# this extension cannot double the per-tick LLM volume.
# ===========================================================================

#: The closed target taxonomy the classifier may assign (person-skew targets).
_VALID_ENTITY_CLASSES = frozenset({
    "country", "organization", "corporation", "location", "person", "event",
    "entity",
})
DEFAULT_RECLASS_MIN_CONF = 0.75

#: Shared lexical-suspect keyword families, factored out so the person and
#: entity pool predicates below can NEVER silently drift on which institution/
#: geography/event keywords carry queue priority (a reviewer flagged the
#: pre-factor version: both SQL constants copied these three regex fragments
#: verbatim — the same de-duplication this file already applies to the LLM
#: prompt text via `_RECLASS_SCHEMA_AND_DEFS` below). Each is a bare
#: alternation (no `\y...\y` wrapper) so a caller can compose it into either
#: an `ORDER BY` alternation or, if ever needed, a `WHERE` filter.
_INSTITUTION_KEYWORDS = (
    r"ministry|ministries|department|council|committee|commission|agency|"
    r"authority|bureau|assembly|organisation|organization|association|"
    r"federation|confederation|party|front|coalition|corps|army|navy|"
    r"air\s?force|division|brigade|regiment|battalion|fleet|university|"
    r"college|institute|academy|bank|company|corporation|holding|court|"
    r"parliament|congress|senate|cabinet|directorate|secretariat|embassy|"
    r"consulate|tribunal|forum|alliance|nato|opec|asean|brics|union"
)
_GEOGRAPHY_KEYWORDS = (
    r"ocean|sea|gulf|strait|straits|peninsula|desert|river|archipelago|"
    r"lake|province|oblast|prefecture|region|territory|plateau|canal|"
    r"reservoir"
)
_EVENT_KEYWORDS = (
    r"war|battle|treaty|summit|conference|declaration|agreement|accord|"
    r"protocol|convention|referendum|election|revolution|uprising|"
    r"offensive|operation|championship|olympics"
)
#: Corporate-suffix tokens — a DQ M6-flagged under-caught target the person
#: pool never needed (a person-shaped name rarely carries one). Anchored to
#: the STRING END (`\.?\s*$`, not a bare `\y...\y`) by the entity-pool SQL
#: below — a review caught the word-boundary-only form matching short 2-3
#: letter tokens (co/sa/ag/se) MID-string on an unrelated word ("the Group
#: 7", "the Donors Group for Palestine" — verified live: the word-boundary
#: form flags both, the end-anchored form flags neither). Kept here as the
#: bare alternation; the entity predicate below supplies the end-anchor.
_CORP_SUFFIX_KEYWORDS = (
    r"inc|incorporated|corp|ltd|llc|plc|co|group|holdings?|ag|sa|se|gmbh|"
    r"kk|spa|nv"
)

#: The reclassify pool is EVERY never-examined active `person` row — one LLM
#: look each, exactly once (`reclass_seen_at` drains the pool). SUSPECT lexical
#: signals (article prefix, org/location/event keywords — the E6 census
#: patterns) only affect ORDER: suspects jump the queue, then newest-first so
#: fresh inflow gets its look within ~a cadence tick and the historical tail
#: drains opportunistically. The 2026-07-21 review motivated the widening: the
#: suspect-only pool cleaned keyword-flagged junk but let quiet mis-types
#: ("West Berlin", "Novaya Pošta", "Dodge", "Lesedi La Rona") sit typed person
#: forever. The LLM makes the final call (conservative prompt, 0.75 gate,
#: real persons kept) — the prefilter only orders the queue.
#:
#: P4 Class 6 Obs. 2 (QW1-D fix 3): a row carrying an ``adjudicator_class_hint``
#: (the adjudicator flagged this exact row's class as wrong while reasoning
#: about a merge candidate — see ``_record_class_correction_hint``) queue-jumps
#: AHEAD of the lexical heuristics below — an LLM-confirmed signal on THIS row
#: outranks a regex guess. The LLM reclassify pass still makes the final call;
#: the hint only ever affects ORDER, never a filter/decision.
_RECLASS_SUSPECT_SQL = rf"""
    SELECT id, canonical_name, entity_class
      FROM entity_profiles
     WHERE entity_class = 'person'
    AND merged_into IS NULL
    AND COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk')
    AND COALESCE(data->>'reclass_seen_at', '') = ''
    ORDER BY (data->'adjudicator_class_hint') IS NOT NULL DESC,
             (canonical_name ~* '^(the|a|an)\s') DESC,
             (
                canonical_name ~* '\y({_INSTITUTION_KEYWORDS})\y'
             OR canonical_name ~* '\y({_GEOGRAPHY_KEYWORDS})\y'
             OR canonical_name ~* '\y({_EVENT_KEYWORDS})\y'
             ) DESC,
             created_at DESC
    LIMIT $1
"""

#: #219 — the SAME "every never-examined row, suspects first" pool shape as
#: `_RECLASS_SUSPECT_SQL`, applied to the generic `entity` bucket instead of
#: `person`. Selection predicate rationale (kept conservative + explainable):
#:   * base filter = `entity_class = 'entity'` (index-backed by the existing
#:     `idx_entity_profiles_class` btree — the SAME index the person query
#:     already relies on, so this adds no new index and no new migration);
#:   * ORDER (not FILTER — the LLM sees the whole pool, the prefilter only
#:     queues it) reuses the SAME institutional/geographic/event keyword
#:     families as the person predicate above (the DQ M6 (2026-07-06) curated
#:     categories: `is_org_surface` / `is_region_surface` / `is_known_org_surface`
#:     in `_entity_canon.py`) that most often explain a row stuck generic, PLUS
#:     a TRAILING corporate-suffix signal (`corporation` is a DQ M6-flagged
#:     under-caught target the person pool never needed). A genuinely
#:     ambiguous trailing short token ("General Sa") can still queue-jump —
#:     this is an ORDER-only signal (never a filter; the conservative LLM
#:     prompt makes the real call, see `_RECLASS_FRAMING["entity"]` below), so
#:     the worst case is a slightly mis-prioritized queue position, never a
#:     wrong class assignment;
#:   * then newest-first, so fresh mis-routed inflow (notably the R-2
#:     article-prefix->entity demotion, `6f270a2`, which now feeds THIS pool)
#:     gets its LLM look within ~a tick rather than waiting out the historical
#:     backlog. `reclass_seen_at` drains the pool exactly as it does for person.
#: P4 Class 6 Obs. 2 (QW1-D fix 3): same adjudicator-hint queue-jump as
#: ``_RECLASS_SUSPECT_SQL`` above, applied to the generic-entity pool.
_RECLASS_ENTITY_SUSPECT_SQL = rf"""
    SELECT id, canonical_name, entity_class
      FROM entity_profiles
     WHERE entity_class = 'entity'
    AND merged_into IS NULL
    AND COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk')
    AND COALESCE(data->>'reclass_seen_at', '') = ''
    ORDER BY (data->'adjudicator_class_hint') IS NOT NULL DESC,
             (
                canonical_name ~* '\y({_INSTITUTION_KEYWORDS})\y'
             OR canonical_name ~* '\y({_GEOGRAPHY_KEYWORDS})\y'
             OR canonical_name ~* '\y({_EVENT_KEYWORDS})\y'
             OR canonical_name ~* '\y({_CORP_SUFFIX_KEYWORDS})\.?\s*$'
             ) DESC,
             created_at DESC
    LIMIT $1
"""

#: Dispatch table: source class -> its pool SQL. Both branches share the SAME
#: shape (id, canonical_name, entity_class), the SAME drain marker
#: (`reclass_seen_at`), and the SAME downstream parse/apply path — only the
#: WHERE + ORDER differ. Adding a third source class later is a one-line entry.
_RECLASS_SOURCE_SQL: dict[str, str] = {
    "person": _RECLASS_SUSPECT_SQL,
    "entity": _RECLASS_ENTITY_SUSPECT_SQL,
}


@dataclass(frozen=True)
class ReclassCandidate:
    id: str
    name: str
    cur_class: str


@dataclass(frozen=True)
class ReclassVerdict:
    entity_id: str
    name: str
    from_class: str
    to_class: str
    confidence: float
    why: str
    model_id: str | None = None


#: Class definitions + response schema are IDENTICAL for every source class —
#: the closed target taxonomy never changes, only which pool is under review.
#: Kept as one shared block so the two prompts can never drift on the part
#: that matters (the schema `_parse_reclass_batch` depends on).
_RECLASS_SCHEMA_AND_DEFS = """You are given a numbered list of NAMES. For EACH return one class. Output ONE JSON array, nothing else — one object per name, in order. ECHO the name verbatim in "name" so the class is bound to the right row:
[{"n": 1, "name": "<name verbatim>", "class": "<one of: country|organization|corporation|location|person|event|entity>", "confidence": 0.0-1.0, "why": "<=12 words"}]

CLASS DEFINITIONS:
- person = a named individual human (Vladimir Putin, Ali Khamenei, President Macron). A full personal name — even title-prefixed — is person.
- organization = an institution / agency / ministry / military unit / alliance / party / court / bank (the Red Cross, NATO, the 4th Marine Division, the Foreign Ministry).
- corporation = a named for-profit company (Boeing, Gazprom, Mercedes-Benz).
- location = a place or geographic feature (the Indian Ocean, the Strait of Hormuz, Lake Chad, the Kerch Peninsula).
- country = a sovereign state (Iran, the Russian Federation, North Korea).
- event = a war / battle / treaty / summit / conference / championship / operation (the Second World War, the Kazan Declaration).
- entity = a real referent that fits none of the above cleanly (a concept, a vessel/aircraft type, a programme), OR you are unsure."""

#: The original 5 worked examples, UNCHANGED (this exact prompt scored 8/8 on
#: the live gpt-oss classifier at reclassify_max=10 before flipping live,
#: `9b53f00` — not touched, so the validated person-pool prompt stays byte-
#: identical). The entity-pool prompt gets ONE extra example (#6, a genuinely-
#: ambiguous concept that correctly stays `entity`) since that pool's whole
#: point is "most rows correctly stay put" — person's examples skew mis-type-
#: heavy on purpose (that pool's failure mode is a quiet miss, not an itch to
#: move something that should stay).
_RECLASS_WORKED_EXAMPLES_PERSON = """Worked examples:
  1. "the Indian Ocean" -> {"class":"location","confidence":0.98,"why":"a named ocean"}
  2. "Ali Khamenei" -> {"class":"person","confidence":0.97,"why":"a named individual"}
  3. "the Russian Foreign Ministry" -> {"class":"organization","confidence":0.96,"why":"a state ministry"}
  4. "the Second World War" -> {"class":"event","confidence":0.95,"why":"a historical war"}
  5. "Sea King" -> {"class":"entity","confidence":0.6,"why":"a helicopter type, not a person/place"}"""

_RECLASS_WORKED_EXAMPLES_ENTITY = (
    _RECLASS_WORKED_EXAMPLES_PERSON
    + '\n  6. "Bluetooth" -> {"class":"entity","confidence":0.7,"why":"a technology standard, not org/place/person"}'
)

#: Per-source-class framing + conservative-default rule + worked examples.
#: Only THIS triple varies between pools — the task intro sentence, schema,
#: and class definitions are shared verbatim (#219). Each entry is
#: (intro_sentence, conservative_rule, worked_examples) keyed by `source_class`.
_RECLASS_FRAMING: dict[str, tuple[str, str, str]] = {
    "person": (
        "Every name below is CURRENTLY typed `person` by a weak heuristic; most are correct, but some are mis-typed (an ocean, an organisation, a war).",
        '- If the name is genuinely a PERSON\'s name, keep "person" — do NOT move a real person.\n'
        "- Only assign a non-person class when the name clearly denotes a non-person referent.\n"
        '- When you cannot tell, use "entity" with a LOW confidence (it is the safe generic bucket — better than a wrong specific class).',
        _RECLASS_WORKED_EXAMPLES_PERSON,
    ),
    "entity": (
        "Every name below is CURRENTLY typed `entity` — the generic catch-all bucket a name falls into when it is genuinely unclear OR when nothing more specific caught it (a real organization, corporation, location, or event can land here uncaught, e.g. an article-prefixed surface demoted from `person`).",
        '- If the name is genuinely unclear, a concept, or does not cleanly fit any specific class, KEEP "entity" — do NOT force a specific class onto a real generic referent.\n'
        "- Only assign a specific class (organization/corporation/location/event/country/person) when the name clearly and unambiguously denotes that kind of referent.\n"
        '- When you cannot tell, use "entity" with a LOW confidence — staying put is always the safe default for this pool.',
        _RECLASS_WORKED_EXAMPLES_ENTITY,
    ),
}


def _build_reclass_system(source_class: str) -> str:
    """Compose the reclassify system prompt for ``source_class`` ('person' or
    'entity'). Schema + class definitions are shared verbatim across both
    pools (#219) — only the intro framing, the conservative-default rule, and
    the worked-example set are pool-specific, so a reviewer can eyeball the
    one paragraph that changed rather than diff two near-duplicate walls of
    text. Unknown ``source_class`` falls back to the 'person' framing."""
    intro, rule, examples = _RECLASS_FRAMING.get(
        source_class, _RECLASS_FRAMING["person"])
    return with_preamble(
        f"""TASK — assign the correct ENTITY CLASS to each name, for a knowledge-graph type-correction pass. {intro} Return the TRUE class.

{_RECLASS_SCHEMA_AND_DEFS}

RULES (be conservative):
{rule}

{examples}
"""
    )


#: Back-compat default (person pool) — pre-#219 call sites + tests import this
#: name directly; it is byte-identical to `_build_reclass_system('person')`,
#: which is itself byte-identical to the pre-#219 `_RECLASS_SYSTEM` literal
#: (verified: only refactored into a builder, no wording touched).
_RECLASS_SYSTEM = _build_reclass_system("person")


def _build_reclass_prompt(batch: list[ReclassCandidate]) -> str:
    # #219: the header names the batch's ACTUAL current class (every row in one
    # batch shares it — select_reclass_candidates only ever draws from one pool
    # per call) instead of hardcoding 'person', so an entity-pool batch doesn't
    # tell the LLM it is reviewing persons.
    cur = batch[0].cur_class if batch else "person"
    lines = [f"NAMES (all currently typed '{cur}'):"]
    for i, c in enumerate(batch, 1):
        lines.append(f'{i}. "{c.name}"')
    return "\n".join(lines)


def _coerce_class(raw: object) -> str | None:
    v = str(raw or "").strip().lower().replace(" ", "").replace("-", "")
    aliases = {
        "org": "organization", "organisation": "organization",
        "company": "corporation", "corp": "corporation",
        "place": "location", "geo": "location", "geographic": "location",
        "individual": "person", "human": "person", "people": "person",
        "nation": "country", "state": "country",
        "generic": "entity", "unknown": "entity", "other": "entity",
    }
    if v in _VALID_ENTITY_CLASSES:
        return v
    return aliases.get(v)


def _parse_reclass_batch(
    content: str, batch: list[ReclassCandidate],
) -> list[ReclassVerdict]:
    """Bind each model class to a candidate by its ECHOED name (authoritative),
    falling back to the 1-based ``n`` only when no name is echoed. An item that
    matches NO candidate is dropped; a candidate left unbound (or with an invalid
    class) defaults to NO CHANGE (to_class == from_class) — never a silent move."""
    # A name -> the candidates carrying it, IN ORDER: a whitespace-only surface
    # variant ("the  War" vs "the War") can normalize to the same key while the
    # UNIQUE(lower(canonical_name), class) index kept the rows distinct, so a
    # dict would silently drop the second. Consume the list first-unassigned-first
    # (the LLM's class for identical surfaces is the same anyway) — adversarial
    # review hardening.
    by_name: dict[str, list[ReclassCandidate]] = {}
    for c in batch:
        by_name.setdefault(_norm_name(c.name), []).append(c)
    assigned: dict[str, dict] = {}  # candidate id -> item
    for item in _extract_json_array(content):
        if not isinstance(item, dict):
            continue
        nm = item.get("name")
        target: ReclassCandidate | None = None
        if nm is not None:
            for cand in by_name.get(_norm_name(nm), []):
                if cand.id not in assigned:
                    target = cand
                    break
        else:
            try:
                n = int(item.get("n"))
            except (TypeError, ValueError):
                n = None
            if n is not None and 1 <= n <= len(batch):
                target = batch[n - 1]
        if target is not None and target.id not in assigned:
            assigned[target.id] = item

    out: list[ReclassVerdict] = []
    for c in batch:
        item = assigned.get(c.id, {})
        to_cls = _coerce_class(item.get("class")) or c.cur_class  # unknown -> keep
        try:
            conf = float(item.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        why = str(item.get("why") or "")[:400]
        out.append(ReclassVerdict(
            entity_id=c.id, name=c.name, from_class=c.cur_class,
            to_class=to_cls, confidence=conf, why=why,
        ))
    return out


async def select_reclass_candidates(
    conn, limit: int, *, source_class: str = "person",
) -> list[ReclassCandidate]:
    """The bounded pool: every never-examined active ``source_class`` row,
    lexical suspects first (see :data:`_RECLASS_SOURCE_SQL` for the per-class
    predicate), then newest-first — so fresh inflow gets its one LLM look
    within ~a tick. ``source_class`` selects the pool ('person' — the E6c
    original — or 'entity' — #219); an unrecognized value falls back to
    'person' (fail-safe: never silently query an unbounded/unknown pool)."""
    if limit <= 0:
        return []
    sql = _RECLASS_SOURCE_SQL.get(source_class, _RECLASS_SUSPECT_SQL)
    rows = await conn.fetch(sql, int(limit))
    return [
        ReclassCandidate(
            id=str(r["id"]), name=r["canonical_name"],
            cur_class=str(r["entity_class"] or source_class),
        )
        for r in rows
    ]


async def reclassify_entities(
    conn,
    llm: LLMHandlerLike,
    *,
    apply: bool = False,
    max_rows: int = 200,
    batch_size: int = DEFAULT_ADJ_BATCH,
    min_confidence: float = DEFAULT_RECLASS_MIN_CONF,
    model_id: str | None = None,
    sample_size: int = 12,
    source_class: str = "person",
) -> tuple[int, int, list[dict]]:
    """One reclassify pass over ONE pool (``source_class`` — 'person' or
    'entity', #219). Returns ``(examined, changed, sample)``.

    Selects the pool, LLM-classifies in batches (degrade-to-no-change on
    a failed batch), and — when ``apply`` — rewrites entity_class for a confident
    change to a DIFFERENT valid class, storing data.reclass = {from,to,...} for
    reversibility, and marks EVERY examined row data.reclass_seen_at (so the pool
    drains and is never re-sent). ``apply=False`` mutates nothing (mark included),
    so the report shows exactly what WOULD change. The response SCHEMA, echo-
    bound PARSE, and degrade-to-no-change behavior are IDENTICAL across both
    source classes — only the pool SQL and the system-prompt framing differ."""
    candidates = await select_reclass_candidates(
        conn, max_rows, source_class=source_class)
    if not candidates:
        return (0, 0, [])
    system_prompt = _build_reclass_system(source_class)

    verdicts: list[ReclassVerdict] = []
    for start in range(0, len(candidates), max(1, batch_size)):
        batch = candidates[start : start + batch_size]
        try:
            response = await llm.chat_complete(
                [{"role": "user", "content": _build_reclass_prompt(batch)}],
                max_tokens=DEFAULT_ADJ_MAX_TOKENS,
                temperature=0.0,
                system=system_prompt,
            )
            content = getattr(response, "content", "") or ""
            batch_v = _parse_reclass_batch(content, batch)
        except Exception as exc:  # degrade-not-break: whole batch -> no change
            logger.warning("entity_researcher.reclass_batch_failed err=%s", exc)
            batch_v = [
                ReclassVerdict(
                    entity_id=c.id, name=c.name, from_class=c.cur_class,
                    to_class=c.cur_class, confidence=0.0,
                    why=f"reclassify error: {exc}",
                )
                for c in batch
            ]
        if model_id:
            batch_v = [ReclassVerdict(**{**v.__dict__, "model_id": model_id})
                       for v in batch_v]
        verdicts.extend(batch_v)

    changed = 0
    sample: list[dict] = []
    for v in verdicts:
        is_change = (
            v.to_class != v.from_class
            and v.to_class in _VALID_ENTITY_CLASSES
            and v.confidence >= min_confidence
        )
        if is_change:
            changed += 1
            if len(sample) < sample_size:
                sample.append({
                    "name": v.name[:60], "from": v.from_class,
                    "to": v.to_class, "confidence": round(v.confidence, 2),
                    "why": v.why[:100],
                })
        if not apply:
            continue
        # APPLY: mark examined (always), rewrite class + ledger on a real change.
        try:
            if is_change:
                await conn.execute(
                    "UPDATE entity_profiles SET entity_class = $2, "
                    "data = jsonb_set(jsonb_set("
                    "  coalesce(data,'{}'::jsonb),"
                    "  '{reclass}', $3::jsonb, true),"
                    "  '{reclass_seen_at}', to_jsonb(now()::text), true), "
                    "updated_at = now() WHERE id = $1::uuid",
                    v.entity_id, v.to_class,
                    _json.dumps({
                        "from": v.from_class, "to": v.to_class,
                        "confidence": round(v.confidence, 3),
                        "why": v.why[:200], "by": "entity_researcher",
                        "model_id": v.model_id,
                    }),
                )
            else:
                await conn.execute(
                    "UPDATE entity_profiles SET data = jsonb_set("
                    "  coalesce(data,'{}'::jsonb),"
                    "  '{reclass_seen_at}', to_jsonb(now()::text), true) "
                    "WHERE id = $1::uuid", v.entity_id)
        except Exception as exc:  # a single-row failure must not sink the pass
            logger.warning("entity_researcher.reclass_apply_failed id=%s err=%s",
                           v.entity_id, exc)
    return (len(verdicts), changed, sample)


async def run_entity_research(
    conn,
    llm: LLMHandlerLike,
    *,
    apply: bool = False,
    model_id: str | None = None,
    same_min_confidence: float = DEFAULT_SAME_MIN_CONF,
    max_pairs: int = DEFAULT_MAX_PAIRS,
    min_trgm: float = DEFAULT_MIN_TRGM,
    trgm_limit: int = 0,
    trgm_min_degree: int = DEFAULT_TRGM_MIN_DEGREE,
    adj_batch: int = DEFAULT_ADJ_BATCH,
    sample_size: int = 12,
    reclassify_max: int = 0,
    reclass_min_confidence: float = DEFAULT_RECLASS_MIN_CONF,
    reclass_entity_share: float = 0.0,
) -> ResearchReport:
    """One research pass: block -> adjudicate the gray band -> execute merges.

    ``apply=False`` (the DEFAULT) is a full DRY-RUN — it still calls the LLM and
    records verdicts (cheap, $0, cached) but ``execute_merges`` mutates nothing,
    so the report shows exactly what WOULD merge. Flip ``apply=True`` only once a
    dry-run looks right. Bounded by ``max_pairs`` per pass (the sweep is
    incremental across cadence ticks). Degrade-not-break: adjudication + merge
    each swallow their own errors, so a pass never raises for a normal miss.

    ``reclass_entity_share`` (#219) SPLITS the ``reclassify_max`` budget between
    the person pool (E6c) and the generic-entity pool — it never adds to it: the
    entity pool gets ``round(reclassify_max * reclass_entity_share)`` rows and
    the person pool gets the REMAINDER, so ``person_max + entity_max ==
    reclassify_max`` ALWAYS holds exactly (clamped so neither goes negative) —
    this sum invariant, not the split's precise 50/50-ness, is what the "cap
    must not grow" constraint actually needs, and it is verified to hold
    across every (cap, share) combination tested, including small caps. Note:
    Python's ``round()`` is round-half-to-even, so at a SMALL reclassify_max
    (well below the live operating value of 150) a share near 0.5 can round
    to a slightly uneven split (e.g. cap=1 share=0.5 -> entity=0/person=1, not
    an even split) — harmless (the sum invariant still holds; this only
    affects which side gets a single "rounding" row at small caps) but worth
    knowing if verifying reclass_by_class's exact counts by hand. Default 0.0
    preserves the pre-#219 behavior byte-for-byte (100% person, entity pool
    never queried). A pool that ends up with 0 rows is simply skipped (its
    ``reclassify_entities`` call is short-circuited by the empty-candidates
    guard), so setting the share to 0.0 or 1.0 cleanly disables one side."""
    # trgm_limit<=0 => exact-block-key only. When it IS enabled, trgm_min_degree
    # bounds the self-join to hub profiles (R9b) — that is what makes the probe
    # affordable on the actor cadence, and it is the ONLY channel that can
    # propose a cross-block-key duplicate (Kiev/Kyiv). exact_limit is generous
    # (the exact self-join is sub-second); the final [:max_pairs] bounds
    # adjudication, and generate_candidates now ranks by degree WITHIN a band
    # (R9a) so that truncation keeps the hubs rather than the alphabet.
    pairs = (await generate_candidates(
        conn, min_trgm=min_trgm,
        exact_limit=max(max_pairs * 4, 1000), trgm_limit=int(trgm_limit),
        trgm_min_degree=int(trgm_min_degree),
    ))[:max_pairs]
    gray = [p for p in pairs if p.band == "gray"]
    auto = len(pairs) - len(gray)

    verdicts = await adjudicate_pairs(
        conn, llm, gray, model_id=model_id, batch_size=adj_batch,
        # P4 Class 6 Obs. 2 (QW1-D fix 3): the entity_profiles.data hint write
        # is gated on `apply`, mirroring reclassify_entities's own apply gate —
        # a dry-run pass mutates NO entity_profiles row, exactly like every
        # other write in this module. The COUNTER below is computed regardless.
        apply_class_corrections=apply,
    )
    tally = {"same": 0, "not_same": 0, "unsure": 0}
    for v in verdicts:
        tally[v.verdict] = tally.get(v.verdict, 0) + 1

    report = await execute_merges(
        conn, verdicts, pairs,
        min_confidence=same_min_confidence, dry_run=not apply,
    )

    # Build a small human sample for the finding, mapping merged ids back to
    # names + the verdict/justification that drove them.
    name_by_id: dict[str, str] = {}
    for p in pairs:
        name_by_id[p.left_id] = p.left_name
        name_by_id[p.right_id] = p.right_name
    verdict_by_pk = {v.pair_key: v for v in verdicts}
    pair_by_pk = {p.pair_key: p for p in pairs}
    sample: list[dict] = []
    for keeper_id, loser_id in report.pairs[:sample_size]:
        pk = "::".join(sorted((keeper_id, loser_id)))
        v = verdict_by_pk.get(pk)
        band = pair_by_pk[pk].band if pk in pair_by_pk else "?"
        sample.append({
            "keeper": name_by_id.get(keeper_id, keeper_id[:8]),
            "loser": name_by_id.get(loser_id, loser_id[:8]),
            "verdict": v.verdict if v else "auto_merge",
            "confidence": round(v.confidence, 2) if v else 1.0,
            "why": (v.justification[:120] if v else f"auto_merge:{band}"),
        })

    # P4 Class 6 Obs. 2 (QW1-D fix 3) — the COUNTER (always computed, no
    # mutation) + a small human sample naming which side + class was flagged.
    class_corrections_flagged = 0
    class_correction_sample: list[dict] = []
    for v in verdicts:
        if v.class_correction is None:
            continue
        class_corrections_flagged += 1
        if len(class_correction_sample) >= sample_size:
            continue
        p = pair_by_pk.get(v.pair_key)
        flagged_name = None
        if p is not None:
            flagged_name = (
                p.left_name if v.class_correction.side == "a" else p.right_name
            )
        class_correction_sample.append({
            "name": (flagged_name or "?")[:80],
            "side": v.class_correction.side,
            "correct_class": v.class_correction.correct_class,
            "why": v.justification[:120],
        })

    # E6c/#219 — reclassify passes (OFF unless reclassify_max > 0). Run AFTER
    # merges so this tick's reclassifications feed NEXT tick's candidate
    # generation. The SAME reclassify_max budget is SPLIT (never added to)
    # across the person pool (E6c) and the generic-entity pool (#219) via
    # reclass_entity_share — see the docstring above for the exact split math.
    reclass_examined = reclass_changed = 0
    reclass_sample: list[dict] = []
    reclass_by_class: dict[str, dict[str, int]] = {}
    if reclassify_max > 0:
        raw_share = float(reclass_entity_share)
        # NaN fails safe to 0.0 (100% person, the well-validated pool) — a bare
        # max(0.0, min(1.0, nan)) would otherwise resolve to 1.0 (Python's
        # min/max keep the first argument on a NaN comparison, since every
        # comparison against NaN is False), which is the WRONG fail-safe
        # direction: it would silently divert the full budget to the newer,
        # less-validated entity pool instead of the one already proven live.
        # Reachable only via a malformed descriptor value (e.g. an operator
        # typo of `reclass_entity_share: .nan`, valid YAML 1.1) — caught here
        # rather than trusted to never happen.
        share = 0.0 if raw_share != raw_share else max(0.0, min(1.0, raw_share))
        entity_max = round(reclassify_max * share)
        person_max = max(0, reclassify_max - entity_max)
        # Defense-in-depth, not load-bearing given the share clamp just above
        # (entity_max is already <= reclassify_max and >= 0 whenever share is
        # in [0,1] and reclassify_max > 0 — verified by exhaustive check, not
        # just inspection). Kept anyway: it costs nothing and protects the
        # "never exceed the combined per-tick cap" invariant if a future edit
        # ever loosens the share clamp above without re-deriving this proof.
        entity_max = max(0, min(reclassify_max, entity_max))
        for pool_class, pool_max in (("person", person_max), ("entity", entity_max)):
            if pool_max <= 0:
                continue
            try:
                p_examined, p_changed, p_sample = await reclassify_entities(
                    conn, llm, apply=apply, max_rows=pool_max,
                    batch_size=adj_batch, min_confidence=reclass_min_confidence,
                    model_id=model_id, sample_size=sample_size,
                    source_class=pool_class,
                )
            except Exception as exc:  # degrade-not-break: reclassify never sinks the run
                logger.warning(
                    "entity_researcher.reclassify_failed pool=%s err=%s",
                    pool_class, exc)
                continue
            reclass_examined += p_examined
            reclass_changed += p_changed
            reclass_by_class[pool_class] = {
                "examined": p_examined, "changed": p_changed,
            }
            remaining = max(0, sample_size - len(reclass_sample))
            if remaining and p_sample:
                reclass_sample.extend(
                    {**s, "pool": pool_class} for s in p_sample[:remaining]
                )

    return ResearchReport(
        mode="apply" if apply else "dry_run",
        candidates=len(pairs), gray=len(gray), auto=auto,
        adjudicated=len(verdicts),
        same=tally["same"], not_same=tally["not_same"], unsure=tally["unsure"],
        merges_applied=report.merged, merges_skipped=report.skipped,
        sample=tuple(sample),
        reclass_examined=reclass_examined, reclass_changed=reclass_changed,
        reclass_sample=tuple(reclass_sample),
        reclass_by_class=reclass_by_class,
        class_corrections_flagged=class_corrections_flagged,
        class_correction_sample=tuple(class_correction_sample),
    )


# ===========================================================================
# E4d — analyst-framework wrapper: EntityResearcherDeps + run_method.
# Mirrors relationship_reifier: a global META analyst that BOTH calls the LLM
# (adjudication, $0 core plane) AND reads/writes the substrate directly. The
# deps-builder (_build_entity_researcher) resolves the primary LLM from the
# descriptor's method.llm.primary and threads pg_pool + budget.
# ===========================================================================


@runtime_checkable
class _BudgetLike(Protocol):
    async def check_envelope(self) -> str: ...


@dataclass
class EntityResearcherDeps:
    """Dep bundle for :func:`run_method`. Built by
    ``analyst_deps_builder._build_entity_researcher`` from the resolved primary
    LLM + the run's StandardDeps (pg_pool + budget). Tests construct it directly
    with a stub LLM + a real test pg_pool.

    ``apply`` gates the ONLY mutating behavior: the descriptor's method option
    ``merge_mode`` maps ``'apply'`` -> True, anything else (incl. the default
    ``'adjudicate_only'``) -> False = a full DRY-RUN. Ships dry-run; flip to
    apply with a descriptor PUT once a dry-run looks right.
    """

    llm: LLMHandlerLike
    pg_pool: Any = None
    budget: _BudgetLike | None = None
    apply: bool = False
    max_pairs: int = DEFAULT_MAX_PAIRS
    same_min_confidence: float = DEFAULT_SAME_MIN_CONF
    # <=0 => exact-block-key only. Positive enables the trigram probe, which is
    # the only channel that can propose a duplicate whose block keys DIFFER
    # (Kiev/Kyiv); pair it with trgm_min_degree or the self-join is unbounded.
    trgm_limit: int = 0
    #: R9b — link-count floor applied to BOTH trigram endpoints. 0 = no floor
    #: (the historical unbounded ~61s scan). Descriptor-settable alongside
    #: trgm_limit; see _entity_candidates._TRGM_HUB_SQL for the cost model.
    trgm_min_degree: int = DEFAULT_TRGM_MIN_DEGREE
    max_tokens: int = DEFAULT_ADJ_MAX_TOKENS
    temperature: float = DEFAULT_ADJ_TEMPERATURE
    batch_size: int = DEFAULT_ADJ_BATCH
    # E6c reclassify pass: rows/tick (0 => OFF), and the min confidence to move.
    reclassify_max: int = 0
    reclass_min_confidence: float = DEFAULT_RECLASS_MIN_CONF
    # #219 — fraction of reclassify_max allotted to the generic-entity pool
    # (the REMAINDER goes to the person pool); 0.0 => pre-#219 behavior, 100%
    # person, entity pool never queried. SPLITS the existing budget, never
    # adds to it (see run_entity_research's docstring for the exact math).
    reclass_entity_share: float = 0.0


def _empty_summary(reason: str) -> FindingPayload:
    return FindingPayload(
        title=f"Entity researcher: no-op ({reason})"[:2048],
        body=f"entity_researcher did not run this tick: {reason}"[:65536],
        confidence=1.0, tags=["meta", "entity_researcher", "noop"],
        data={"meta": True, "sub_handler": KIND_NAME, "reason": reason},
    )


def _report_summary(rep: ResearchReport, model_id: str | None) -> FindingPayload:
    tags = ["meta", "entity_researcher", f"mode:{rep.mode}"]
    if rep.merges_applied:
        tags.append("merges_applied" if rep.mode == "apply" else "merges_proposed")
    body = rep.summary() + "\n\nSample:\n" + "\n".join(
        f"- {s['keeper']} << {s['loser']}  [{s['verdict']} "
        f"{s.get('confidence', '')}] {s['why']}" for s in rep.sample
    )
    if rep.reclass_sample:
        body += "\n\nReclassify sample:\n" + "\n".join(
            f"- {s['name']}: {s['from']} -> {s['to']}  "
            f"[{s.get('confidence', '')}] {s['why']}" for s in rep.reclass_sample
        )
    if rep.class_correction_sample:
        body += "\n\nClass-correction flags (adjudicator, P4 Class 6):\n" + "\n".join(
            f"- {s['name']} (side {s['side']}): looks like {s['correct_class']}  "
            f"{s['why']}" for s in rep.class_correction_sample
        )
    return FindingPayload(
        title=rep.summary()[:2048],
        body=body[:65536],
        confidence=1.0,
        tags=tags,
        data={"meta": True, "sub_handler": KIND_NAME, "model_id": model_id,
              **rep.to_data()},
    )


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: EntityResearcherDeps | LLMHandlerLike,
) -> AnalystMethodResult:
    """One ``entity_researcher`` sweep: block -> adjudicate -> (dry-run|apply).

    ``deps`` accepts an :class:`EntityResearcherDeps` (production) or a bare
    :class:`LLMHandlerLike` (back-compat test path). The REAL product is the
    side-written entity_profiles merges (apply mode) + the entity_judgement
    verdicts; the returned finding is the run receipt. Degrade-not-break: a
    missing pool or any error yields a no-op summary, never a raise."""
    if not isinstance(deps, EntityResearcherDeps):
        deps = EntityResearcherDeps(llm=deps)

    model_id = str(options.get("model_id") or "") or None
    if deps.pg_pool is None:
        return AnalystMethodResult(finding=_empty_summary("no pg_pool"))

    # Honor the token envelope before issuing any LLM adjudication.
    if deps.budget is not None:
        try:
            if (await deps.budget.check_envelope()) != "ok":
                return AnalystMethodResult(finding=_empty_summary("budget_paused"))
        except Exception:  # pragma: no cover - defensive; proceed on a probe error
            pass

    try:
        async with deps.pg_pool.acquire() as conn:
            rep = await run_entity_research(
                conn, deps.llm,
                apply=deps.apply, model_id=model_id,
                same_min_confidence=deps.same_min_confidence,
                max_pairs=deps.max_pairs, trgm_limit=deps.trgm_limit,
                trgm_min_degree=deps.trgm_min_degree,
                adj_batch=deps.batch_size,
                reclassify_max=deps.reclassify_max,
                reclass_min_confidence=deps.reclass_min_confidence,
                reclass_entity_share=deps.reclass_entity_share,
            )
    except Exception as exc:  # pragma: no cover - degrade-not-break
        logger.warning("entity_researcher.run_failed err=%s", exc)
        return AnalystMethodResult(finding=_empty_summary(f"error: {exc}"[:200]))

    logger.info("entity_researcher.%s", rep.summary())
    return AnalystMethodResult(finding=_report_summary(rep, model_id))


__all__ = [
    "KIND_NAME", "OUTPUT_KIND", "HANDLER_VERSION",
    "Verdict", "adjudicate_pairs", "DEFAULT_ADJ_BATCH",
    "MergeReport", "elect_keeper", "merge_pair", "unmerge", "execute_merges",
    "ResearchReport", "run_entity_research",
    "EntityResearcherDeps", "run_method",
]
