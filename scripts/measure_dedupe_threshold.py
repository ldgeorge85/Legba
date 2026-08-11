#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""measure_dedupe_threshold.py — MEASURE (never tune) the semantic-dedup
precision curve, so ``cross_source_dedup``'s operating threshold is chosen from
data instead of taste.

WHY THIS EXISTS
---------------

``cross_source_dedup``'s semantic tier ran at a configured 0.95 that nobody ever
validated, because the tier never issued a Qdrant query in its entire history
(``recommend()`` had been removed from the client; the call site swallowed the
missing method and returned ``[]``). When the call path was repaired the
question "is 0.95 right?" had no evidence behind it at all — and the review that
found the dead path estimated that **50.5% of what the tier would link at 0.95
was wrong** (``planning/engine_review/p2_substrate.md`` §2.4).

That error rate is not a cosmetic problem. Linking an alias sets
``signals.canonical_signal_id``, and every desk slice filters
``(canonical_signal_id IS NULL OR canonical_signal_id = id)`` — so a FALSE link
makes a real, distinct signal **invisible to every analyst on the platform**.
Missing a dedup costs a little redundancy; a false dedup silently destroys
evidence. The threshold therefore has to be set by measured precision, and the
measurement has to be reproducible.

METHOD
------

1. Sample signals DETERMINISTICALLY from Postgres: ``ORDER BY id`` over rows
   carrying a real (uuid-shaped) ``embedding_ref``. No RANDOM(), no clock — the
   same ``--sample`` re-runs over the same corpus give the same worksheet.
2. For each, ``query_points(query=<point id>)`` against ``legba_signals`` — the
   EXACT call ``cross_source_dedup._qdrant_neighbours`` makes, imported from the
   handler so the measurement cannot drift from the runtime path — collecting
   every neighbour at or above ``--floor``.
3. Join both sides back to Postgres for title / body / source_id / fetched_at.
   Neighbours whose signal row no longer exists are dropped and counted: they
   are the Qdrant orphan class (the vector store has no delete leg), and the
   handler skips them too.
4. LABEL each pair with a heuristic that is INDEPENDENT OF THE VECTOR — token
   overlap between the two TITLES, backed by body overlap. The live vectors are
   body-only, so the title is evidence the vector did not see. See
   :func:`label_pair` for the exact rule and its deliberate ambiguity band.
5. Report precision per 0.01 score band, cumulative precision at each candidate
   threshold, and the same split by same-source vs cross-source — the tier
   exists for the cross-source case; same-source reissues are ``ingest_dedupe``'s
   job.
6. Report the curve a second time with DEGENERATE pairs excluded (both sides'
   embed input byte-identical, or under the embedder's length floor). Those
   pairs are an artefact of the pre-floor embedder input, not of the threshold;
   the first curve governs what to purge from the existing links, the second
   estimates the regime after a re-embed.
7. Emit a fixed-size worksheet (``--worksheet``) — deterministically sampled
   across the bands, one row per pair, with the labels and the features they
   were derived from — so the labelling can be audited by hand and pinned by a
   regression test.

READ-ONLY, by construction: every SQL statement in this file is a SELECT, and
every Qdrant call is ``query_points`` (a read). It writes exactly one thing, the
local worksheet file named by ``--worksheet``. It changes NO threshold and NO
production data; setting the threshold is a human decision made from this
output.

USAGE
-----

Inside the live runtime container (which already carries the pg/qdrant wiring)::

    docker cp scripts/measure_dedupe_threshold.py \\
        legba-legba-runtime-dapr-1:/tmp/measure_dedupe_threshold.py
    docker exec legba-legba-runtime-dapr-1 \\
        python3 /tmp/measure_dedupe_threshold.py --sample 6000

Or against the published dev-stack ports with ``LEGBA_DATA_PG_*`` exported.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any, Iterable, Mapping

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg  # noqa: E402

from legba.data.analysts.deterministic_handlers.cross_source_dedup import (  # noqa: E402
    _DEFAULT_QDRANT_COLLECTION,
    _UUID_EMBEDDING_REF_RE,
    _qdrant_neighbours,
)
from legba.data.analysts.deterministic_handlers.signal_embedder import (  # noqa: E402
    MIN_BODY_CHARS,
    _clean_html,
)

# ---------------------------------------------------------------------------
# The embed input AS THE LIVE VECTORS WERE BUILT
#
# The corpus in Qdrant today was embedded by the PRE-FLOOR `_pick_body` (first
# non-empty field, no minimum length). To classify a pair as degenerate we have
# to reproduce THAT input, not the current one — so this is a deliberate local
# copy of the old rule, not an import. It exists only to explain the existing
# vectors; nothing else should ever call it.
# ---------------------------------------------------------------------------

_LEGACY_BODY_FIELDS = (
    "distilled_body", "raw_body", "summary", "description", "content_text", "text",
)


def legacy_embed_input(payload: Mapping[str, Any]) -> str:
    """The embed input the PRE-FLOOR embedder would have produced."""
    for key in _LEGACY_BODY_FIELDS:
        value = payload.get(key)
        if not (isinstance(value, str) and value.strip()):
            continue
        cleaned = _clean_html(value)
        if cleaned:
            return cleaned
    return ""


# ---------------------------------------------------------------------------
# The label — deliberately INDEPENDENT of the vector under test
# ---------------------------------------------------------------------------

#: Words carrying no topical information. Kept short and obvious on purpose: a
#: long curated stoplist would be a tuning knob, and this function must be
#: auditable by eye.
_STOPWORDS = frozenset("""
a an the and or but if then than that this these those of in on at to for from
with without by as is are was were be been being it its his her their our your
new says say said after before over under about into out up down not no more
most amid over new news report reports live update updates
""".split())

_WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)

#: Label thresholds. DUPLICATE and DISTINCT are separated by an AMBIGUOUS band —
#: a heuristic that labels every pair is a heuristic lying about its own
#: resolution. Ambiguous pairs are reported and excluded from the precision
#: denominator, and BOTH bounds are printed (ambiguous-as-wrong and
#: ambiguous-dropped) so the reader can see how much is being asserted.
DUP_TITLE_JACCARD = 0.50
DUP_MIXED_TITLE_JACCARD = 0.25
DUP_MIXED_BODY_JACCARD = 0.60
DISTINCT_TITLE_JACCARD = 0.25

LABEL_DUPLICATE = "duplicate"
LABEL_DISTINCT = "distinct"
LABEL_AMBIGUOUS = "ambiguous"
#: Neither side has a title — nothing INDEPENDENT of the vector is left to judge
#: with, so the pair is excluded from every count rather than guessed at.
LABEL_UNLABELLED = "unlabelled"


def content_words(text: str) -> set[str]:
    """Lowercased content words of length >= 3, stopwords removed."""
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def label_pair(
    title_a: str, title_b: str, body_a: str, body_b: str,
) -> tuple[str, float, float]:
    """Is this pair the SAME STORY? Returns ``(label, title_j, body_j)``.

    **Body overlap is only evidence when the bodies are long enough to BE
    evidence.** This is the trap that makes a naive version of this function
    useless on precisely the pairs that matter: the degenerate class shares a
    byte-identical *stub* ("(END)", "Credit: AFP"), which scores body overlap
    1.0 between two completely unrelated stories. A rule that reads that as
    "similar" launders the exact defect it is supposed to detect. So body
    overlap counts only when BOTH bodies clear
    :data:`~...signal_embedder.MIN_BODY_CHARS` — the same floor the embedder
    now applies, for the same reason.

    The rule, in full:

      * **unlabelled** — either title is missing. Nothing independent of the
        vector is left to judge with; excluded from every count, never guessed.
      * **duplicate** — titles share >=50% of their content words; or bodies are
        USABLE and share >=60% with titles also >=25%; or the bodies are
        byte-identical AND long (a 200+ char identical article body is a repost,
        not boilerplate).
      * **distinct** — titles share <=25% and the body evidence does not
        contradict that (either unusable, or under the 60% bar).
      * **ambiguous** — everything else.

    The features are TITLE and BODY TOKEN OVERLAP, never the embedding: the live
    vectors are body-only, so the title is evidence the vector never saw, and a
    label derived from the vector would only measure the vector against itself.
    """
    title_j = jaccard(content_words(title_a), content_words(title_b))
    body_j = jaccard(content_words(body_a), content_words(body_b))
    if not (title_a.strip() and title_b.strip()):
        return LABEL_UNLABELLED, title_j, body_j

    body_usable = min(len(body_a), len(body_b)) >= MIN_BODY_CHARS
    strong_body = body_usable and body_j >= DUP_MIXED_BODY_JACCARD

    if title_j >= DUP_TITLE_JACCARD:
        return LABEL_DUPLICATE, title_j, body_j
    if body_usable and body_a == body_b:
        return LABEL_DUPLICATE, title_j, body_j
    if strong_body and title_j >= DUP_MIXED_TITLE_JACCARD:
        return LABEL_DUPLICATE, title_j, body_j
    if title_j <= DISTINCT_TITLE_JACCARD and not strong_body:
        return LABEL_DISTINCT, title_j, body_j
    return LABEL_AMBIGUOUS, title_j, body_j


# ---------------------------------------------------------------------------
# Substrate access (SELECT-only)
# ---------------------------------------------------------------------------

_SAMPLE_SQL = f"""
    SELECT id, embedding_ref
      FROM signals
     WHERE embedding_ref ~ '{_UUID_EMBEDDING_REF_RE}'
     ORDER BY id
     LIMIT $1
"""

_ROWS_SQL = """
    SELECT id, source_id, fetched_at, payload
      FROM signals
     WHERE id = ANY($1::uuid[])
"""


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _title(payload: Mapping[str, Any]) -> str:
    value = payload.get("title")
    return _clean_html(value) if isinstance(value, str) else ""


def pg_settings() -> dict[str, Any]:
    return {
        "host": os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        "port": int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        "user": os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        "password": os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        "database": os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    }


# ---------------------------------------------------------------------------
# Curve assembly
# ---------------------------------------------------------------------------

BANDS: tuple[float, ...] = (
    0.80, 0.82, 0.84, 0.86, 0.88, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99,
)


def band_of(score: float) -> float:
    lo = BANDS[0]
    for edge in BANDS:
        if score >= edge:
            lo = edge
    return lo


class Tally:
    """Label counts for a set of pairs, plus BOTH precision bounds.

    ``upper`` drops ambiguous pairs from the denominator; ``lower`` counts every
    one of them as a false link. The truth is between, and quoting only the
    upper bound is how a measurement flatters itself.
    """

    __slots__ = ("dup", "dis", "amb", "unl")

    def __init__(self, rows: Iterable[Mapping[str, Any]]):
        self.dup = self.dis = self.amb = self.unl = 0
        for row in rows:
            label = row["label"]
            if label == LABEL_DUPLICATE:
                self.dup += 1
            elif label == LABEL_DISTINCT:
                self.dis += 1
            elif label == LABEL_UNLABELLED:
                self.unl += 1
            else:
                self.amb += 1

    @property
    def judged(self) -> int:
        return self.dup + self.dis

    @property
    def upper(self) -> float | None:
        return (self.dup / self.judged) if self.judged else None

    @property
    def lower(self) -> float | None:
        denom = self.dup + self.dis + self.amb
        return (self.dup / denom) if denom else None


def _print_curve(title: str, pairs: list[Mapping[str, Any]]) -> None:
    print(f"\n{title}")
    print(f"  {'band':>6}{'pairs':>7}{'dup':>6}{'dist':>6}{'amb':>6}{'unl':>6}"
          f"{'prec':>8}   |  >= band cumulative: prec [lower bound] over N links")
    by_band: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for pair in pairs:
        by_band[band_of(pair["score"])].append(pair)
    for edge in BANDS:
        here = Tally(by_band.get(edge, []))
        cum_pairs = [p for p in pairs if p["score"] >= edge]
        cum = Tally(cum_pairs)
        prec_s = f"{here.upper:.3f}" if here.upper is not None else "    -"
        if cum.upper is not None:
            cum_s = (f"{cum.upper:.3f} [{cum.lower:.3f}]  "
                     f"n={cum.judged}/{len(cum_pairs)} links")
        else:
            cum_s = "-"
        print(f"  {edge:>6.2f}{len(by_band.get(edge, [])):>7}{here.dup:>6}"
              f"{here.dis:>6}{here.amb:>6}{here.unl:>6}{prec_s:>8}   |  {cum_s}")


async def run(args: argparse.Namespace) -> None:
    try:
        from qdrant_client import AsyncQdrantClient
    except ImportError:  # pragma: no cover
        print("qdrant-client is not importable here — run inside the runtime "
              "container (see the module docstring).", file=sys.stderr)
        raise SystemExit(2)

    pool = await asyncpg.create_pool(min_size=1, max_size=4, **pg_settings())
    qdrant = AsyncQdrantClient(
        host=os.environ.get("LEGBA_QDRANT_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_QDRANT_PORT", "6333")),
        check_compatibility=False,
    )
    try:
        async with pool.acquire() as conn:
            sampled = await conn.fetch(_SAMPLE_SQL, args.sample)
        print(f"sampled {len(sampled)} vectored signals (deterministic id order), "
              f"top-{args.top_k} neighbours at >= {args.floor}")

        # --- 2. neighbours, straight through the handler's own call ----------
        raw_pairs: list[tuple[str, str, float]] = []
        for row in sampled:
            neighbours = await _qdrant_neighbours(
                qdrant, args.collection, row["embedding_ref"], args.floor,
                limit=args.top_k,
            )
            for nid, score in neighbours:
                if str(nid) == str(row["id"]):
                    continue
                raw_pairs.append((str(row["id"]), str(nid), score))

        # Deduplicate (a,b)/(b,a) — a link is undirected.
        best: dict[tuple[str, str], float] = {}
        for a, b, score in raw_pairs:
            key = (a, b) if a < b else (b, a)
            best[key] = max(best.get(key, 0.0), score)
        print(f"  {len(raw_pairs)} neighbour hits -> {len(best)} distinct pairs")

        # --- 3. hydrate both sides ------------------------------------------
        wanted = {sid for pair in best for sid in pair}
        async with pool.acquire() as conn:
            rows = await conn.fetch(_ROWS_SQL, list(wanted))
        by_id = {str(r["id"]): r for r in rows}
        orphaned = len(wanted) - len(by_id)
        print(f"  {len(by_id)} of {len(wanted)} pair members still exist in "
              f"signals ({orphaned} Qdrant orphans dropped)")

        # --- 4. label --------------------------------------------------------
        pairs: list[dict[str, Any]] = []
        for (a, b), score in best.items():
            row_a, row_b = by_id.get(a), by_id.get(b)
            if row_a is None or row_b is None:
                continue
            pay_a, pay_b = _as_dict(row_a["payload"]), _as_dict(row_b["payload"])
            title_a, title_b = _title(pay_a), _title(pay_b)
            body_a, body_b = legacy_embed_input(pay_a), legacy_embed_input(pay_b)
            label, title_j, body_j = label_pair(title_a, title_b, body_a, body_b)
            pairs.append({
                "a": a, "b": b, "score": score, "label": label,
                "title_j": title_j, "body_j": body_j,
                "title_a": title_a, "title_b": title_b,
                "source_a": row_a["source_id"], "source_b": row_b["source_id"],
                "cross_source": row_a["source_id"] != row_b["source_id"],
                # Degenerate = the pre-floor embedder gave both sides the same
                # input, or an input too thin to carry meaning. Either way the
                # cosine is an artefact of the INPUT, not of the content.
                "degenerate": (
                    (body_a == body_b and bool(body_a))
                    or min(len(body_a), len(body_b)) < MIN_BODY_CHARS
                ),
                "len_a": len(body_a), "len_b": len(body_b),
            })
        print(f"  {len(pairs)} labelled pairs")

        # --- 5/6. the curves --------------------------------------------------
        _print_curve("PRECISION CURVE — all pairs (the corpus as it stands today)",
                     pairs)
        _print_curve("PRECISION CURVE — cross-source pairs only (the P-02 case "
                     "the tier exists for)",
                     [p for p in pairs if p["cross_source"]])
        clean = [p for p in pairs if not p["degenerate"]]
        _print_curve(
            f"PRECISION CURVE — degenerate pairs excluded ({len(pairs) - len(clean)} "
            "of {0} dropped; estimates the post-re-embed regime)".format(len(pairs)),
            clean,
        )
        _print_curve("PRECISION CURVE — cross-source AND non-degenerate",
                     [p for p in clean if p["cross_source"]])

        share = (sum(1 for p in pairs if p["degenerate"]) / len(pairs)) if pairs else 0
        print(f"\ndegenerate share of all pairs: {share:.1%}")
        print(f"cross-source share of all pairs: "
              f"{(sum(1 for p in pairs if p['cross_source']) / len(pairs)) if pairs else 0:.1%}")

        # --- 7. worksheet -----------------------------------------------------
        if args.worksheet:
            write_worksheet(pairs, args.worksheet, args.worksheet_per_band)
            print(f"\nworksheet written: {args.worksheet}")
        print("\nNothing was written to Postgres or Qdrant by this run — "
              "read-only measurement only.")
    finally:
        await pool.close()
        await qdrant.close()


def write_worksheet(
    pairs: list[Mapping[str, Any]], path: str, per_band: int,
) -> None:
    """Deterministically sample ``per_band`` pairs per band and write them out.

    The sample order is ``sha256(a|b)`` — stable across runs and independent of
    the score, so the worksheet cannot be accused of cherry-picking the easy
    end of a band.
    """
    by_band: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for pair in pairs:
        by_band[band_of(pair["score"])].append(pair)

    chosen: list[Mapping[str, Any]] = []
    for edge in BANDS:
        rows = sorted(
            by_band.get(edge, []),
            key=lambda p: hashlib.sha256(
                f"{p['a']}|{p['b']}".encode()
            ).hexdigest(),
        )
        chosen.extend(rows[:per_band])

    columns = (
        "score", "band", "label", "title_j", "body_j", "cross_source",
        "degenerate", "len_a", "len_b", "source_a", "source_b",
        "title_a", "title_b", "signal_a", "signal_b",
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\t".join(columns) + "\n")
        for pair in chosen:
            handle.write("\t".join([
                f"{pair['score']:.6f}",
                f"{band_of(pair['score']):.2f}",
                pair["label"],
                f"{pair['title_j']:.3f}",
                f"{pair['body_j']:.3f}",
                "1" if pair["cross_source"] else "0",
                "1" if pair["degenerate"] else "0",
                str(pair["len_a"]), str(pair["len_b"]),
                str(pair["source_a"]), str(pair["source_b"]),
                _cell(pair["title_a"]), _cell(pair["title_b"]),
                pair["a"], pair["b"],
            ]) + "\n")


def _cell(text: str) -> str:
    """One worksheet cell: tabs/newlines stripped, bounded, never truncated
    mid-escape."""
    flat = " ".join((text or "").split())
    return html.unescape(flat)[:180] or "(no title)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=6000,
                        help="Signals sampled (deterministic id order).")
    parser.add_argument("--top-k", type=int, default=5,
                        help="Neighbours requested per sampled signal.")
    parser.add_argument("--floor", type=float, default=0.80,
                        help="Lowest score collected — the bottom of the curve.")
    parser.add_argument("--collection", default=_DEFAULT_QDRANT_COLLECTION)
    parser.add_argument("--worksheet", default="",
                        help="Path to write the deterministic pair worksheet.")
    parser.add_argument("--worksheet-per-band", type=int, default=12,
                        help="Pairs sampled per band into the worksheet.")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
