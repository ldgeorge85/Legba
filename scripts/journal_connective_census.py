#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Journal connective/register census — READ-ONLY (T0.1, P1 in
planning/JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09.md §3/§6).

Deterministic detectors over ``journal_entries.body`` + its declared
``claims`` sidecar (§3.6 of ``journal_assessor.py``, ``_reflect_claims``):

  (a) connective faults — a STRONG connective cue sitting between two
      separately-cited referents inside one fact claim whose refs share
      NEITHER geo NOR resolved entity identity (the "same conflict"
      class). WEAK cues (amid/meanwhile/at the same time) are counted
      separately, never gated on overlap. NARROWED 2026-09-09 (B4, per
      JOURNAL_CONNECTIVE_CENSUS.md §3/§5's own 13%-precision finding): "as"
      dropped (7/13 FPs, almost always internal to the trailing clause, not
      bridging); "same"/"the same" fires ONLY as "same <conflict|crisis|
      war|front|theatre|campaign>" (3/13 FPs were "at the same time"/"the
      same day", a temporal transition, not an identity claim) — see
      ``_find_strong_cues``.
  (b) evaluative register — a small adjective lexicon, plus a
      scare-quoted proper noun followed within ~6 tokens by
      narrative/so-called/framing.
  (c) inference stacking — a SINGLE-ref fact claim carrying >=2
      inference cues (hints at, suggests, could, may, posture,
      architecture, pattern, signals that, points to, indicates).
  (d) instrument refs — refs whose signal is event-coded (GDELT files,
      or a CAMEO-style "ACTOR <-> ACTOR: verb" pseudo-title); per span:
      instrument-ref share and whether ``[[instrument]]`` is present.
  (e) routine products — refs from scheduled-statistical source_ids (a
      small allowlist derived live from ``source_descriptors``: hazard-
      tagged geojson/json_api feeds + ``*.press``/``*press_release*``
      wires, plus ``source.nws.active_alerts`` as a floor) OR a narrow
      content cue for an institutional tally reported through a general
      wire (e.g. "State Emergency Service"); per span: routine-ref count
      and whether a change/causal connective rides alongside it.

Design note on "entity overlap" (a): the schema's ``signals.entity_classes``
is a coarse TYPE bucket (country/location/person/organization/entity) that
is present on >50% of ALL signals in any 14-day window — it is not a
referent-identity signal and would make the ZERO-overlap gate nearly
unreachable. This script reports ``entity_classes ∩`` for transparency (as
asked) but GATES on the RESOLVED entity identity from
``signal_entity_links`` (signal -> entity_profiles.id) instead, which is
what actually answers "do these two refs talk about the same real-world
thing." See planning/JOURNAL_CONNECTIVE_CENSUS.md §5 for the full
rationale and its own limits.

NEVER writes. Only SELECTs. Run on the host (loopback) or inside the
registry container:

  set -a; . ./.env; set +a
  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      python3 scripts/journal_connective_census.py --days 14 \\
      --json-out /tmp/census.json

Flags:
  --days N        window size, produced_at >= now() - N days (default 14)
  --entry-id UUID restrict to one entry (calibration runs)
  --json-out PATH dump the full per-hit detail as JSON (default: none)
  --top N         worst-sentence table size for the stdout summary (default 10)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

_ENTRY_KINDS = ("entry", "consolidation", "lens", "lens_diff")

# ---------------------------------------------------------------------------
# Lexicons (proposal §3(a)-(e); "at least" — this is the floor, not a ceiling)
# ---------------------------------------------------------------------------
# B4 lexicon narrowing (2026-09-09, JOURNAL_CONNECTIVE_CENSUS.md §3/§5): the
# 14-day hand-graded run found (a) at 13% precision (2 TP / 13 FP). Two
# classes accounted for all 13 FPs: "as" (7 — almost always internal to the
# TRAILING clause, not bridging the gap between the two ref groups) and bare
# "the same"/"same conflict"/"same crisis" catching "at the same time"/"the
# same day" (3 — an honest temporal transition, not an identity claim). Fix:
# drop "as" entirely; "same" fires ONLY as "same <referent>" for a real
# identity-of-topic word (_SAME_REFERENT_RE below), never bare. The one
# other FP class (a cue technically inside the gap but grammatically the
# TRAILING sentence's own clause) is a structural property of "as" specifically
# and is resolved by dropping it, not by a further gap-position rule — the
# BRIDGE property itself (a cue must sit strictly between two DIFFERENT ref
# groups within one claim's span) was already true by construction: ``gap``
# is exactly ``span[end_of_group_a:start_of_group_b]``, so nothing here can
# fire on text inside either ref group's own sentence.
_STRONG_CUES = [
    "in the wake of", "produced", "hints at", "suggests", "underscoring",
    "underscores", "compounds", "reinforcing",
]
# "same X" only counts as a strong (identity-of-topic) cue for one of these
# referent words — "same time"/"same day" is a temporal transition, not a
# claim that two referents are the SAME thing.
_SAME_REFERENT_RE = re.compile(
    r"\bsame (?:conflict|crisis|war|front|theatre|campaign)\b", re.IGNORECASE
)
_WEAK_CUES = ["amid", "meanwhile", "at the same time"]
_EVAL_LEXICON = [
    "brightened", "darkened", "welcome", "unwelcome", "grim", "heartening",
    "worrying", "troubling", "encouraging", "disappointing", "promising",
    "bleak", "ominous", "alarming", "reassuring",
]
_INFERENCE_CUES = [
    "hints at", "suggests", "could", "may", "posture", "architecture",
    "pattern", "signals that", "points to", "indicates",
]
_ROUTINE_CHANGE_CUES = ["compounds", "stress", "indicator", "driven by", "amid"]
_ROUTINE_CONTENT_CUES = [
    "state emergency service", "emergency situations service",
    "ministry of health", "sitrep", "situation report",
]

_REF_MARKER_RE = re.compile(r"\[\[ref:([0-9a-fA-F-]{36})\]\]")
_INSTRUMENT_MARKER_RE = re.compile(r"\[\[instrument\]\]", re.IGNORECASE)
_GAP_TRIVIAL_RE = re.compile(r"^[\s,;.\[\]&]*(?:and)?[\s,;.\[\]&]*$", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z“\"\[])")
_SCARE_QUOTE_RE = re.compile(r"[“\"']([A-Z][^”\"']{1,60})[”\"']")
_SCARE_FOLLOW_RE = re.compile(r"\b(narrative|so-called|so called|framing)\b", re.I)
# CAMEO-style pseudo-title: "POLICE <-> GOVERNMENT: assault" / "PRISON: coerce…"
_INSTRUMENT_TITLE_RE = re.compile(r"^[A-Z][A-Z ]+(?: <-> [A-Z][A-Z ]+)?: ")


def _find_cues(text: str, lexicon: list[str]) -> list[str]:
    low = text.lower()
    hits = []
    for phrase in lexicon:
        pat = r"\b" + re.escape(phrase) + r"\b"
        if re.search(pat, low):
            hits.append(phrase)
    return hits


def _find_strong_cues(text: str) -> list[str]:
    """The narrowed (a) STRONG-cue set (B4, 2026-09-09): the plain phrase
    list plus the "same <referent>" identity pattern — "same"/"the same"
    never fires bare."""
    hits = _find_cues(text, _STRONG_CUES)
    m = _SAME_REFERENT_RE.search(text)
    if m:
        hits.append(m.group(0).lower())
    return hits


def _ref_groups(span: str) -> list[tuple[list[str], int, int]]:
    """Group consecutive ``[[ref:uuid]]`` markers separated only by
    punctuation/"and" into one citation cluster; a real prose gap between
    two markers starts a new group. Returns ``[(uuids, start, end), ...]``
    in document order — ``start``/``end`` bound the group's marker span."""
    matches = list(_REF_MARKER_RE.finditer(span))
    if not matches:
        return []
    groups: list[tuple[list[str], int, int]] = []
    cur_ids = [matches[0].group(1)]
    cur_start, cur_end = matches[0].start(), matches[0].end()
    for m in matches[1:]:
        gap = span[cur_end:m.start()]
        if _GAP_TRIVIAL_RE.match(gap):
            cur_ids.append(m.group(1))
            cur_end = m.end()
        else:
            groups.append((cur_ids, cur_start, cur_end))
            cur_ids = [m.group(1)]
            cur_start, cur_end = m.start(), m.end()
    groups.append((cur_ids, cur_start, cur_end))
    return groups


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------
async def _connect_pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


async def _fetch_entries(
    conn: asyncpg.Connection, since: datetime, entry_id: str | None
) -> list[asyncpg.Record]:
    if entry_id:
        return await conn.fetch(
            "SELECT id::text, entry_kind, title, produced_at, analyst_id, "
            "honesty_flags, claims::text AS claims_json, char_length(body) "
            "AS body_chars FROM journal_entries WHERE id = $1::uuid",
            entry_id,
        )
    return await conn.fetch(
        "SELECT id::text, entry_kind, title, produced_at, analyst_id, "
        "honesty_flags, claims::text AS claims_json, char_length(body) "
        "AS body_chars FROM journal_entries WHERE entry_kind = ANY($1::text[]) "
        "AND produced_at >= $2 ORDER BY produced_at ASC",
        list(_ENTRY_KINDS), since,
    )


async def _fetch_signals(
    conn: asyncpg.Connection, ref_ids: set[str]
) -> dict[str, dict[str, Any]]:
    if not ref_ids:
        return {}
    rows = await conn.fetch(
        "SELECT id::text, source_id, geo, entity_classes, tags, "
        "coalesce(payload->>'title', payload->>'headline', '') AS title "
        "FROM signals WHERE id = ANY($1::uuid[])",
        list(ref_ids),
    )
    return {
        r["id"]: {
            "source_id": r["source_id"], "geo": set(r["geo"] or []),
            "entity_classes": set(r["entity_classes"] or []),
            "tags": set(r["tags"] or []), "title": r["title"] or "",
        }
        for r in rows
    }


async def _fetch_entity_links(
    conn: asyncpg.Connection, ref_ids: set[str]
) -> dict[str, set[str]]:
    if not ref_ids:
        return {}
    rows = await conn.fetch(
        "SELECT signal_id::text, entity_id::text FROM signal_entity_links "
        "WHERE signal_id = ANY($1::uuid[])",
        list(ref_ids),
    )
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["signal_id"], set()).add(r["entity_id"])
    return out


async def _fetch_critiques(
    conn: asyncpg.Connection, entry_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not entry_ids:
        return {}
    # NOTE: the analyst_outputs.data column nests the verification payload a
    # level deeper than its own top-level keys (data->'data'->'verification'),
    # while ``analyzed_output_id`` (the join key) sits at the TOP level
    # (data->>'analyzed_output_id') — verified live; the two are NOT siblings.
    rows = await conn.fetch(
        "SELECT data->>'analyzed_output_id' AS entry_id, "
        "data->'data'->'verification'->>'faithfulness_score' AS score, "
        "data->'data'->'verification'->>'judge_status' AS judge_status, "
        "data->'data'->'verification'->>'checkable_claims' AS checkable, "
        "data->'data'->'verification'->>'supported_claims' AS supported "
        "FROM analyst_outputs WHERE title LIKE 'Faithfulness verify%' "
        "AND data->>'analyzed_output_id' = ANY($1::text[])",
        entry_ids,
    )
    return {r["entry_id"]: dict(r) for r in rows if r["entry_id"]}


async def _fetch_routine_allowlist(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT descriptor_id FROM source_descriptors WHERE is_head AND ("
        "descriptor_id = 'source.nws.active_alerts' "
        "OR (kind IN ('geojson','json_api') AND body->'scope'->'tags' ? 'hazard') "
        "OR descriptor_id LIKE '%.press' "
        "OR descriptor_id LIKE '%press_release%')"
    )
    return {r["descriptor_id"] for r in rows}


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------
def _ref_desc(rid: str, signals: dict[str, Any]) -> str:
    s = signals.get(rid)
    if not s:
        return f"{rid[:8]}… (unresolved ref)"
    geo = ",".join(sorted(s["geo"])) or "-"
    return f"{s['source_id']} · {{{geo}}} · {s['title'][:70]!r}"


def _overlap(
    ids_a: list[str], ids_b: list[str],
    signals: dict[str, Any], entities: dict[str, set[str]],
) -> dict[str, Any]:
    def union(field: str, ids: list[str]) -> set[str]:
        out: set[str] = set()
        for rid in ids:
            s = signals.get(rid)
            if s:
                out |= s[field]
        return out

    geo_a, geo_b = union("geo", ids_a), union("geo", ids_b)
    ec_a, ec_b = union("entity_classes", ids_a), union("entity_classes", ids_b)
    tags_a, tags_b = union("tags", ids_a), union("tags", ids_b)
    ent_a = set().union(*(entities.get(r, set()) for r in ids_a)) if ids_a else set()
    ent_b = set().union(*(entities.get(r, set()) for r in ids_b)) if ids_b else set()
    src_a = {signals[r]["source_id"] for r in ids_a if r in signals}
    src_b = {signals[r]["source_id"] for r in ids_b if r in signals}
    return {
        "geo_a": sorted(geo_a), "geo_b": sorted(geo_b),
        "geo_overlap": bool(geo_a & geo_b),
        "entity_class_overlap": bool(ec_a & ec_b),
        "entity_class_a": sorted(ec_a), "entity_class_b": sorted(ec_b),
        "resolved_entity_overlap": bool(ent_a & ent_b),
        "tags_overlap": bool(tags_a & tags_b),
        "same_source": bool(src_a & src_b),
    }


def _detect_connectives(
    claim: dict[str, Any], signals: dict[str, Any], entities: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    faults: list[dict[str, Any]] = []
    weak: list[dict[str, Any]] = []
    if claim["kind"] != "fact" or len(claim.get("refs") or []) < 2:
        return faults, weak
    span = claim["text_span"]
    groups = _ref_groups(span)
    for i in range(len(groups) - 1):
        ids_a, _, end_a = groups[i]
        ids_b, start_b, _ = groups[i + 1]
        gap = span[end_a:start_b]
        strong = _find_strong_cues(gap)
        weak_c = _find_cues(gap, _WEAK_CUES)
        if not strong and not weak_c:
            continue
        ov = _overlap(ids_a, ids_b, signals, entities)
        rec = {
            "sentence_gap": gap.strip(), "cues_strong": strong, "cues_weak": weak_c,
            "refs_before": [_ref_desc(r, signals) for r in ids_a],
            "refs_after": [_ref_desc(r, signals) for r in ids_b],
            "overlap": ov,
        }
        if strong and not ov["geo_overlap"] and not ov["resolved_entity_overlap"]:
            faults.append(rec)
        elif weak_c:
            weak.append(rec)
    return faults, weak


def _detect_register(body_or_span: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for sent in _split_sentences(body_or_span):
        lex = _find_cues(sent, _EVAL_LEXICON)
        if lex:
            hits.append({"sentence": sent, "kind": "lexicon", "terms": lex})
        for m in _SCARE_QUOTE_RE.finditer(sent):
            tail = sent[m.end():m.end() + 60]
            if _SCARE_FOLLOW_RE.search(tail):
                hits.append({
                    "sentence": sent, "kind": "scare_quote",
                    "terms": [m.group(1)],
                })
    return hits


def _detect_inference_stack(claim: dict[str, Any]) -> dict[str, Any] | None:
    if claim["kind"] != "fact" or len(claim.get("refs") or []) != 1:
        return None
    span = _REF_MARKER_RE.sub("", claim["text_span"])
    cues = _find_cues(span, _INFERENCE_CUES)
    if len(cues) < 2:
        return None
    return {"text_span": claim["text_span"], "cues": cues, "count": len(cues)}


def _is_instrument(rid: str, signals: dict[str, Any]) -> bool | None:
    s = signals.get(rid)
    if not s:
        return None
    if s["source_id"] == "source.gdelt.files":
        return True
    return bool(_INSTRUMENT_TITLE_RE.match(s["title"] or ""))


def _detect_instrument(
    claim: dict[str, Any], signals: dict[str, Any]
) -> dict[str, Any] | None:
    refs = claim.get("refs") or []
    if claim["kind"] != "fact" or not refs:
        return None
    flags = [_is_instrument(r, signals) for r in refs]
    known = [f for f in flags if f is not None]
    if not known or not any(known):
        return None
    share = sum(1 for f in known if f) / len(refs)
    return {
        "text_span": claim["text_span"], "share": round(share, 3),
        "n_refs": len(refs), "n_instrument": sum(1 for f in known if f),
        "n_unresolved": len(refs) - len(known),
        "carries_marker": bool(_INSTRUMENT_MARKER_RE.search(claim["text_span"])),
        "refs": [_ref_desc(r, signals) for r in refs],
    }


def _detect_routine(
    claim: dict[str, Any], signals: dict[str, Any], allowlist: set[str],
) -> dict[str, Any] | None:
    refs = claim.get("refs") or []
    if claim["kind"] != "fact" or not refs:
        return None
    hits: list[str] = []
    for r in refs:
        s = signals.get(r)
        if not s:
            continue
        low_title = (s["title"] or "").lower()
        if s["source_id"] in allowlist:
            hits.append(f"{r}:source_class")
        elif any(c in low_title for c in _ROUTINE_CONTENT_CUES):
            hits.append(f"{r}:content_cue")
    if not hits:
        return None
    span = claim["text_span"]
    connective = _find_cues(span, _ROUTINE_CHANGE_CUES)
    return {
        "text_span": span, "routine_refs": hits, "n_routine": len(hits),
        "carries_change_connective": bool(connective), "connectives": connective,
        "refs": [_ref_desc(r, signals) for r in refs],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def _run(args: argparse.Namespace) -> dict[str, Any]:
    conn = await _connect_pg()
    try:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        entries = await _fetch_entries(conn, since, args.entry_id)
        entry_dicts = []
        all_refs: set[str] = set()
        for e in entries:
            claims = json.loads(e["claims_json"])
            for c in claims:
                all_refs.update(c.get("refs") or [])
            entry_dicts.append({**dict(e), "claims": claims})

        signals = await _fetch_signals(conn, all_refs)
        entities = await _fetch_entity_links(conn, all_refs)
        critiques = await _fetch_critiques(conn, [e["id"] for e in entry_dicts])
        allowlist = await _fetch_routine_allowlist(conn)

        reports = []
        for e in entry_dicts:
            claims = e["claims"]
            fact_n = sum(1 for c in claims if c["kind"] == "fact")
            persp_n = sum(1 for c in claims if c["kind"] == "perspective")
            faults: list[dict] = []
            weak: list[dict] = []
            register: list[dict] = []
            stacks: list[dict] = []
            instruments: list[dict] = []
            routines: list[dict] = []
            for c in claims:
                f, w = _detect_connectives(c, signals, entities)
                faults.extend(f)
                weak.extend(w)
                register.extend(_detect_register(c["text_span"]))
                stack = _detect_inference_stack(c)
                if stack:
                    stacks.append(stack)
                inst = _detect_instrument(c, signals)
                if inst:
                    instruments.append(inst)
                rout = _detect_routine(c, signals, allowlist)
                if rout:
                    routines.append(rout)
            crit = critiques.get(e["id"], {})
            reports.append({
                "id": e["id"], "entry_kind": e["entry_kind"], "title": e["title"],
                "produced_at": e["produced_at"].isoformat(), "analyst_id": e["analyst_id"],
                "body_chars": e["body_chars"], "claim_fact": fact_n,
                "claim_perspective": persp_n, "honesty_flags": list(e["honesty_flags"] or []),
                "faithfulness": float(crit["score"]) if crit.get("score") else None,
                "judge_status": crit.get("judge_status"),
                "checkable_claims": int(crit["checkable"]) if crit.get("checkable") else None,
                "supported_claims": int(crit["supported"]) if crit.get("supported") else None,
                "connective_faults": faults, "weak_cues": weak,
                "register_hits": register, "inference_stacks": stacks,
                "instrument_spans": instruments, "routine_spans": routines,
            })
        return {
            "since": since.isoformat(), "days": args.days,
            "n_entries": len(reports), "routine_allowlist": sorted(allowlist),
            "entries": reports,
        }
    finally:
        await conn.close()


def _print_summary(data: dict[str, Any], top_n: int) -> None:
    entries = data["entries"]
    print(f"journal_connective_census — {data['n_entries']} entries since {data['since']}")
    print(f"routine allowlist ({len(data['routine_allowlist'])}): "
          f"{', '.join(data['routine_allowlist'])}")
    totals = Counter()
    worst: list[tuple[str, dict]] = []
    for e in entries:
        totals["faults"] += len(e["connective_faults"])
        totals["weak"] += len(e["weak_cues"])
        totals["register"] += len(e["register_hits"])
        totals["stacks"] += len(e["inference_stacks"])
        totals["instrument"] += len(e["instrument_spans"])
        totals["routine"] += len(e["routine_spans"])
        for f in e["connective_faults"]:
            worst.append((e["id"], f))
        print(
            f"  {e['produced_at'][:16]}  {e['entry_kind']:13s} "
            f"chars={e['body_chars']:5d} claims={e['claim_fact']}f/{e['claim_perspective']}p "
            f"faith={e['faithfulness']}  a={len(e['connective_faults'])} "
            f"b={len(e['register_hits'])} c={len(e['inference_stacks'])} "
            f"d={len(e['instrument_spans'])} e={len(e['routine_spans'])} "
            f"flags={e['honesty_flags']}"
        )
    n = max(data["n_entries"], 1)
    print("\ntotals:", dict(totals))
    print(f"connective faults / entry = {totals['faults'] / n:.3f}")
    print(f"weak-cue rate / entry     = {totals['weak'] / n:.3f}")
    print(f"\ntop {top_n} connective-fault sentences:")
    for eid, f in worst[:top_n]:
        print(f"  [{eid[:8]}] cue={f['cues_strong']} :: {f['sentence_gap'][:100]!r}")
        print(f"      before: {f['refs_before']}")
        print(f"      after:  {f['refs_after']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--entry-id", default=None)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    data = asyncio.run(_run(args))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(data, fh, indent=2, default=str)
    _print_summary(data, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
