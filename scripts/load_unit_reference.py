#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load ONE independent reference into ``unit_references`` (G1's input).

The correctness grader grades a country's published read against an INDEPENDENT
reference for the same country and window — R3's shape: ``ref_developments[]``
(summary, decisive_span, outlet, publish_date) plus a band per dimension, built
BLIND to the substrate. Building one is track R2's job (``docs/SEAMS.md`` #55).
This script is how one gets into the database in the meantime: the deploy loads
the reference Program 1 built by hand on 2026-09-16 so the job has something to
grade against on day one.

  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      python3 scripts/load_unit_reference.py \\
          planning/PROGRAM1_2026-09-16/step2/ref_IL_A.json \\
          --target country_watch_il

IDEMPOTENT. ``(target_id, sha256)`` is unique, so re-running is a no-op and says
so. Editing the file mints a NEW row rather than mutating the old one — a
reference a published share was computed against must stay readable exactly as
it was, or the share stops being re-arguable.

WHAT IS DERIVED AND WHAT IS NOT.

  * ``window_start`` / ``window_end`` come from ``header.window`` ("A -> B") or
    from ``--window-start`` / ``--window-end``. No silent default: a reference
    whose window nobody stated cannot be matched to an as-of stamp.
  * ``thin_dimensions`` IS computed here — the dimensions carrying fewer than
    ``--thin-below`` developments, over the dimension list the reference's own
    band table declares. A thin dimension makes every claim on it structurally
    likelier to read ``silent``, so coverage on it must be read with this array
    in hand.
  * ``span_verified_rate`` is NOT computed here and is NOT guessed. It is the
    BUILDER's number — only the builder had the archived page text to check a
    decisive span against, and re-deriving it from the file would only ever
    measure the file against itself. Read from ``header.span_verified_rate`` or
    passed with ``--span-verified-rate``; absent, the column stays NULL, which
    is not the same as zero and is printed as such.

``--merge-into <reference_id>`` is the OPERATOR TOP-UP door (track R2). The
core-plane lane builds a span-anchored skeleton for free and, as R1 measured,
under-covers: ~33% of a knowledgeable reader's major developments, with
per-dimension counts flatter than the record. When the operator chooses to
close that gap they run their own lane over the packet
``scripts/reference_topup_packet.py`` writes, and this mode takes the result
back. It is NOT a rubber stamp: every addition goes through the SAME FENCES the
free lane's own developments went through — its URL is RE-FETCHED through the
platform's SSRF-guarded transport, the page's own machine-readable publish date
must land inside the reference's window, and the decisive span must be an exact
substring of what that page actually serves. An addition that fails is DROPPED
and counted. A paid lane gets no easier ride than the free one, which is the
only way the merged reference stays one instrument rather than two.

The merge MINTS A NEW ROW (new sha256, builder ``core-plane-lane+opus-topup``)
rather than mutating the skeleton, for the same reason editing a file does: a
reference a published share was computed against must stay readable exactly as
it was.

``--seed-calibration`` writes the PROGRAM 1 v4 GATE ROW into
``grader_calibrations`` instead of loading a reference. The grader refuses to
publish any number until such a row exists (see
``_correctness_calibration.py``), so this is a required deploy step and not an
optional one. The numbers seeded are VERDICT_P1v4's, verbatim; the model ids are
read from the grader's own registry so the seeded row can never claim to have
calibrated a model the code would not use.

Env: ``LEGBA_DATA_PG_HOST`` / ``_PORT`` / ``_USER`` / ``_PASSWORD`` / ``_DB``.
No key is ever printed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping, Any
from uuid import UUID, uuid4

import asyncpg

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _correctness_grade as GRADE,
)
from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _reference_store as STORE,
)
from legba.data.analysts.deterministic_handlers._correctness_rubric import (  # noqa: E402
    RUBRIC_NAME,
    RUBRIC_SHA256,
)
from legba.data.analysts.deterministic_handlers._reference_fences import (  # noqa: E402
    apply_fences,
    dimension_counts,
    thin_from_counts,
)
from legba.data.analysts.deterministic_handlers._reference_page import (  # noqa: E402
    ReferenceArchive,
    fetch_and_archive,
)
from legba.data.analysts.deterministic_handlers.reference_builder import (  # noqa: E402
    BUILDER_LABEL_TOPPED_UP,
)

# THE ONE WRITER. Both this script and the scheduled reference_builder go
# through legba…_reference_store: two writers of one row is how thin_dimensions
# comes to mean one thing when a human loads a file and another when the job
# writes one, at which point the grader's `thin` contract quietly depends on WHO
# built the reference. Every derivation rule below is that module's.
_INSERT_REFERENCE = STORE.INSERT_REFERENCE_SQL

#: The calibration gate row. Stays here: `grader_calibrations` is G1's table and
#: this script is its only writer, so there is no second writer to share it with.
#: H12 — the gate method version rides the row (draw + coverage rule it ran
#: under); _correctness_calibration.METHOD_VERSION is its single source.
from legba.data.analysts.deterministic_handlers._correctness_calibration import (  # noqa: E402
    METHOD_VERSION as CALIBRATION_METHOD_VERSION,
)

_INSERT_CALIBRATION = """
INSERT INTO grader_calibrations (
    id, rubric_sha, model_ids, pooled, pairwise, gate_pass, packet_sha,
    n_atoms, pooled_bar, pairwise_bar, notes, method_version
) VALUES ($1, $2, $3::jsonb, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12)
ON CONFLICT (rubric_sha, packet_sha, model_ids) DO NOTHING
RETURNING id
"""

# ---------------------------------------------------------------------------
# PROGRAM 1 v4 — the gate that passed, as a row
# ---------------------------------------------------------------------------
#
# planning/PROGRAM1_2026-09-16/VERDICT_P1v4.md, measured 2026-09-16 on a FRESH
# draw of 30 atoms (0 overlap with v3), 0 UNPARSEABLE of 90 calls, 23/30 atoms
# all-three-agree, 0/30 all-three-differ. Spend: OpenRouter $0.041, core $0,
# Anthropic $0.
#
# WHY THIS DIGEST IS PINNED HERE. ``V4_PACKET_SHA`` is the sha256 of the graded
# PACKET — the exact atoms the three families were shown — and it is half the
# uniqueness key the calibration row is written under
# (``ON CONFLICT (rubric_sha, packet_sha, model_ids)``). It is a PROVENANCE
# RECORD of a measurement that happened, not a credential: there is nothing to
# authenticate with it, and a different packet must produce a different digest
# or the gate stops meaning anything. The pre-push entropy scan carves it out by
# path AND variable name (scripts/prepush_scan.sh §6).
V4_PACKET_SHA = "0a011222a043bf67398a119c250219c83de46ff435259151f17413e1ad2b7fb6"
V4_POOLED = 0.8444
V4_PAIRWISE = [
    {"a": "F0", "b": "F2", "rate": 0.833},
    {"a": "F0", "b": "F3", "rate": 0.900},
    {"a": "F2", "b": "F3", "rate": 0.800},
]
V4_N_ATOMS = 30
V4_NOTES = (
    "PROGRAM 1 v4 calibration gate, 2026-09-16 (planning/PROGRAM1_2026-09-16/"
    "VERDICT_P1v4.md). Three families, ANNEX C v4, a FRESH 30-atom draw over "
    "R3's frozen corpus with 0 overlap with the v3 draw. Pooled 0.8444 against "
    "a 0.75 bar; pairs 0.833 / 0.900 / 0.800 against a 0.70 floor; 0 "
    "UNPARSEABLE of 90 calls; 1 span flagged, label stood. Run by hand, "
    "seeded here so the job has the passing gate it refuses to run without."
)

async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


_iso = STORE.iso


def parse_window(
    reference: dict, start: str | None, end: str | None
) -> tuple[datetime, datetime]:
    """``(window_start, window_end)`` — the store's rule, with this script's
    exit behaviour. No default: a reference whose window nobody stated cannot be
    matched to an as-of stamp, and inventing one would silently point the grader
    at the wrong fortnight."""
    try:
        return STORE.parse_window(reference, start, end)
    except ValueError as exc:
        raise SystemExit(
            f"{exc} (pass --window-start and --window-end)"
        ) from exc


_dev_dimensions = STORE.dev_dimensions


thin_dimensions = STORE.thin_dimensions


span_verified_rate = STORE.span_verified_rate


async def load_reference(args: argparse.Namespace) -> int:
    raw_bytes = open(args.path, "rb").read()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    reference = json.loads(raw_bytes.decode("utf-8"))
    developments = reference.get("ref_developments") or []
    if not developments:
        raise SystemExit(
            f"{args.path} carries no ref_developments — every claim graded "
            "against it would be ungradable. Refusing to load."
        )
    header = reference.get("header") or {}
    target_id = args.target or str(header.get("target_id") or "")
    if not target_id:
        raise SystemExit(
            "--target is required (the file carries no header.target_id). The "
            "reference is per COUNTRY TARGET and a wrong one would grade one "
            "country's read against another's world."
        )
    window_start, window_end = parse_window(
        reference, args.window_start, args.window_end
    )
    built_at = (
        _iso(args.built_at) if args.built_at
        else _iso(header["built_at"]) if header.get("built_at")
        else datetime.now(timezone.utc)
    )
    builder = args.builder or str(header.get("builder") or "")
    if not builder:
        raise SystemExit(
            "--builder is required (the file carries no header.builder). The "
            "route is part of the number's provenance: a reference the core "
            "plane built is a different instrument from one an out-of-plane "
            "model built."
        )
    thin = thin_dimensions(reference, args.thin_below)
    rate = span_verified_rate(reference, args.span_verified_rate)

    print(f"reference   : {args.path}")
    print(f"  sha256    : {digest}")
    print(f"  target    : {target_id}")
    print(f"  window    : {window_start.isoformat()} .. {window_end.isoformat()}")
    print(f"  built_at  : {built_at.isoformat()}  builder={builder}")
    print(f"  developments: {len(developments)}  bands: "
          f"{len(reference.get('ref_bands') or {})}")
    print(f"  thin (<{args.thin_below} developments): {thin or 'none'}")
    print(
        "  span_verified_rate: "
        + (f"{rate}" if rate is not None
           else "NULL (the builder reported none — not the same as zero)")
    )
    if args.dry_run:
        print("DRY RUN — nothing written")
        return 0

    conn = await _connect()
    try:
        row = await conn.fetchrow(
            _INSERT_REFERENCE, uuid4(), target_id, window_start, window_end,
            built_at, builder, json.dumps(reference),
            None if rate is None else Decimal(str(rate)), thin, digest,
        )
    finally:
        await conn.close()
    if row is None:
        print(
            "  ALREADY LOADED — (target_id, sha256) is unique and this exact "
            "reference is already in unit_references. Nothing written."
        )
        return 0
    print(f"  wrote unit_references id={row['id']}")
    return 0


async def seed_calibration(args: argparse.Namespace) -> int:
    models = GRADE.model_ids()
    print("seeding the PROGRAM 1 v4 calibration gate row")
    print(f"  rubric    : {RUBRIC_NAME}  sha256 {RUBRIC_SHA256}")
    print(f"  models    : {json.dumps(models, sort_keys=True)}")
    print(f"  pooled    : {V4_POOLED}   pairs: "
          + ", ".join(f"{p['a']}x{p['b']}={p['rate']}" for p in V4_PAIRWISE))
    print(f"  packet    : {V4_PACKET_SHA}   n_atoms={V4_N_ATOMS}")
    print(f"  core model resolved from {GRADE.CORE_MODEL_ENV} "
          f"(fallback {GRADE.CORE_MODEL_DEFAULT!r}) -> {models['F0']!r}")
    if models["F0"] != GRADE.CORE_MODEL_DEFAULT:
        print(
            "  NOTE: the core plane serves a model id the v4 gate did NOT run "
            f"against ({GRADE.CORE_MODEL_DEFAULT!r}). Seeding this row would "
            "claim a calibration that never happened. Re-gate on a fresh draw "
            "(scripts/correctness_regate.py) instead of seeding."
        )
        if not args.force:
            return 2
    if args.dry_run:
        print("DRY RUN — nothing written")
        return 0

    conn = await _connect()
    try:
        row = await conn.fetchrow(
            _INSERT_CALIBRATION, uuid4(), RUBRIC_SHA256,
            json.dumps(models, sort_keys=True), Decimal(str(V4_POOLED)),
            json.dumps(V4_PAIRWISE), True, V4_PACKET_SHA, V4_N_ATOMS,
            Decimal("0.75"), Decimal("0.70"), V4_NOTES,
            CALIBRATION_METHOD_VERSION,
        )
    finally:
        await conn.close()
    if row is None:
        print("  ALREADY SEEDED — this (rubric, packet, model set) is already "
              "a row. Nothing written.")
        return 0
    print(f"  wrote grader_calibrations id={row['id']}")
    return 0


# ---------------------------------------------------------------------------
# --merge-into: the operator top-up, through the SAME fences
# ---------------------------------------------------------------------------


_BAND_VOCAB: frozenset[str] = frozenset(
    ("low", "watch", "elevated", "high", "critical", "insufficient-basis")
)


def _refuse_off_ladder_bands(bands: Mapping[str, Any]) -> None:
    """Every ``ref_bands`` value must be on the five-band ladder or the honest
    ``insufficient-basis`` state (``_reference_instruction.BAND_LADDER``)."""
    bad = {k: v for k, v in bands.items() if v not in _BAND_VOCAB}
    if bad:
        raise SystemExit(
            f"ref_bands off the ladder {sorted(_BAND_VOCAB)}: {bad} — fix the file"
        )


async def merge_topup(args: argparse.Namespace) -> int:
    """Union an operator lane's additions into an existing reference.

    Five steps, and the third is the one that makes this honest:

      1. read the skeleton row (it is never mutated);
      2. read the additions file;
      3. **RE-FETCH every cited URL and RE-RUN THE FENCES over the additions** —
         the page's own machine-readable publish date must land inside the
         skeleton's window, and the decisive span must be an exact substring of
         what that page actually serves today. Drops are counted and named;
      4. union by MATTER (same URL+date, or the same span) so a top-up that
         re-reports an established development cannot double-count it in any
         per-dimension depth count;
      5. recompute `thin_dimensions` over the union, recompute
         `span_verified_rate` over BOTH lanes' candidates, and write a NEW row
         with builder `core-plane-lane+opus-topup`.

    A re-fetch can fail for reasons that have nothing to do with the top-up's
    quality — a host that 403s this lane, a page that has since moved. That is
    still a DROP, and it is reported as one, because a span nobody can re-check
    is not a verified span. The alternative — trusting the lane's own report —
    is exactly the property R1 could not re-derive from `ref_IL_A.json`.
    """
    row = await _read_reference(args.merge_into)
    base = json.loads(row["ref_json"]) if isinstance(row["ref_json"], str) \
        else dict(row["ref_json"])
    base_devs = list(base.get("ref_developments") or [])
    window_start, window_end = row["window_start"], row["window_end"]

    raw_bytes = open(args.path, "rb").read()
    addition = json.loads(raw_bytes.decode("utf-8"))
    add_devs = list(addition.get("ref_developments") or [])
    if not add_devs:
        raise SystemExit(
            f"{args.path} carries no ref_developments — there is nothing to "
            "merge. Refusing to mint a new row identical to the old one."
        )

    print(f"merging into : {row['id']}")
    print(f"  target     : {row['target_id']}  builder={row['builder']}")
    print(f"  window     : {window_start.isoformat()} .. "
          f"{window_end.isoformat()}")
    print(f"  skeleton   : {len(base_devs)} development(s)")
    print(f"  additions  : {len(add_devs)} claimed")

    # --- step 3: re-fetch and re-fence ------------------------------------
    archive = ReferenceArchive.open_default(enabled=not args.no_archive)
    urls = sorted({
        str(d.get("source_url") or "").strip()
        for d in add_devs if str(d.get("source_url") or "").strip()
    })
    print(f"  re-fetching {len(urls)} cited page(s) through the guarded "
          "transport…")
    for url in urls:
        page = await fetch_and_archive(url, archive)
        flag = "ok " if page.usable else "FAIL"
        print(f"    [{flag}] {page.status:>3} {page.chars:>7,}c "
              f"{page.publish_date or 'UNDATED':>10}  {url[:96]}")

    survivors, stats = apply_fences(
        add_devs,
        archive=archive,
        window_start=window_start.date(),
        window_end=window_end.date(),
        # A top-up is asked for TIER 1-2 work and gets no blanket Tier-3
        # opening; it inherits the dimensions the skeleton had to open, which
        # are the ones the record genuinely does not carry at that level.
        tier3_dimensions=(base.get("header") or {}).get("tier3_opened_for") or (),
    )
    record = stats.as_record()
    print(f"  fences     : {stats.accepted}/{stats.candidates} additions "
          f"survived")
    for reason, count in sorted(record["rejected"].items()):
        if count:
            print(f"    dropped {count}: {reason}")
    if not survivors:
        raise SystemExit(
            "every addition failed a fence. Nothing merged, nothing written — "
            "the skeleton stands unchanged."
        )

    # --- step 4: union by matter ------------------------------------------
    merged_devs, merge_stats = STORE.merge_developments(base_devs, survivors)
    header = dict(base.get("header") or {})
    prefix = str(
        (base_devs[0].get("item_id") if base_devs else "") or "RD-C"
    ).rsplit("-", 1)[0] or "RD-C"
    merged_devs = STORE.renumber(merged_devs, prefix)
    print(f"  union      : {merge_stats['base']} kept + "
          f"{merge_stats['added']} added, "
          f"{merge_stats['duplicates_dropped']} duplicate(s) collapsed")

    # --- step 5: bands, gaps, thin, rate, write ---------------------------
    merged = dict(base)
    merged["ref_developments"] = merged_devs
    # A band outside the ladder (2026-09-25: a top-up lane wrote "severe")
    # would ride into the grader's comparison unrecognised; refuse it here,
    # where the file is still the operator's to fix.
    _refuse_off_ladder_bands(addition.get("ref_bands") or {})
    bands = dict(base.get("ref_bands") or {})
    rebanded = {
        k: v for k, v in (addition.get("ref_bands") or {}).items()
        if k in bands and str(v).strip() and str(v) != str(bands.get(k))
    }
    bands.update(rebanded)
    merged["ref_bands"] = bands
    gaps = list(base.get("gaps") or [])
    for gap in addition.get("gaps") or []:
        if str(gap) not in {str(g) for g in gaps}:
            gaps.append(gap)
    merged["gaps"] = gaps

    dims = sorted(bands)
    counts = dimension_counts(merged_devs, dims)
    thin = thin_from_counts(counts, args.thin_below)

    base_fences = dict(header.get("fences") or {})
    base_candidates = int(base_fences.get("candidates") or len(base_devs))
    base_verified = int(base_fences.get("verified") or len(base_devs))
    total_candidates = base_candidates + stats.candidates
    rate = (
        round((base_verified + stats.accepted) / total_candidates, 6)
        if total_candidates else None
    )

    header.update({
        "builder": BUILDER_LABEL_TOPPED_UP,
        "built_at": datetime.now(timezone.utc).replace(
            microsecond=0).isoformat(),
        "span_verified_rate": rate,
        "thin_dimensions": thin,
        "per_dimension_counts": counts,
        "topup": {
            "merged_from_reference_id": str(row["id"]),
            "merged_from_sha256": row["sha256"],
            "skeleton_builder": str(row["builder"]),
            "additions_file": os.path.basename(args.path),
            "additions_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "additions_model": str(
                (addition.get("header") or {}).get("model") or "unstated"
            ),
            "additions_claimed": len(add_devs),
            "fences": record,
            "union": merge_stats,
            "rebanded": rebanded,
            "skeleton_fences": base_fences,
            "note": (
                "Every addition was re-fetched through the platform's "
                "SSRF-guarded transport and re-fenced (date gate + span "
                "verification against what the page serves) before it was "
                "merged. span_verified_rate is over BOTH lanes' candidates."
            ),
        },
    })
    merged["header"] = header

    print(f"  merged     : {len(merged_devs)} development(s) over "
          f"{len(dims)} dimension(s)")
    print(f"  per-dim    : "
          + ", ".join(f"{d}={counts.get(d, 0)}" for d in dims))
    print(f"  thin (<{args.thin_below}): {', '.join(thin) or 'none'}")
    print(f"  span_verified_rate: {rate} "
          f"({base_verified + stats.accepted}/{total_candidates}, both lanes)")
    if rebanded:
        print(f"  rebanded   : "
              + ", ".join(f"{k} -> {v}" for k, v in sorted(rebanded.items())))
    if args.dry_run:
        print("DRY RUN — nothing written")
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(merged, handle, indent=1, ensure_ascii=False)
            print(f"  merged reference written to {args.out}")
        return 0

    conn = await _connect()
    try:
        reference_id, derived = await STORE.write_reference(
            conn,
            target_id=str(row["target_id"]),
            reference=merged,
            window_start=window_start,
            window_end=window_end,
            built_at=_iso(header["built_at"]),
            builder=BUILDER_LABEL_TOPPED_UP,
            rate=rate,
            thin_below=args.thin_below,
        )
    finally:
        await conn.close()
    if reference_id is None:
        print("  ALREADY LOADED — this exact merged reference is already a row.")
        return 0
    print(f"  wrote unit_references id={reference_id} "
          f"sha256={derived['sha256'][:16]}…")
    print("  the skeleton row is UNCHANGED and still readable; the grader "
          "picks the newest BUILD for a stamp, which is this one.")
    return 0


async def _read_reference(reference_id: str) -> Any:
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            STORE.SELECT_REFERENCE_SQL, UUID(str(reference_id))
        )
    finally:
        await conn.close()
    if row is None:
        raise SystemExit(
            f"no unit_references row with id {reference_id!r}. A top-up is a "
            "DIFFERENCE against an existing skeleton; there is nothing to "
            "merge into."
        )
    return row



def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load one independent reference into unit_references, or "
                    "seed the Program 1 v4 calibration gate row."
    )
    parser.add_argument("path", nargs="?", help="the reference JSON file")
    parser.add_argument("--target", default=None,
                        help="country target id, e.g. country_watch_il")
    parser.add_argument("--builder", default=None,
                        help="who built it and how, e.g. opus-web-lane")
    parser.add_argument("--window-start", default=None)
    parser.add_argument("--window-end", default=None)
    parser.add_argument("--built-at", default=None)
    parser.add_argument("--span-verified-rate", type=float, default=None,
                        help="the BUILDER's measured rate; never re-derived here")
    parser.add_argument("--thin-below", type=int, default=2,
                        help="a dimension with fewer than this many "
                             "developments is THIN (default 2)")
    parser.add_argument("--seed-calibration", action="store_true",
                        help="write the PROGRAM 1 v4 gate row instead of "
                             "loading a reference")
    parser.add_argument("--force", action="store_true",
                        help="seed the v4 row even though the core plane "
                             "serves a model the gate did not run against")
    parser.add_argument("--merge-into", default=None,
                        help="OPERATOR TOP-UP: union this file's developments "
                             "into the unit_references row with this id, "
                             "re-fetching and re-fencing every addition, and "
                             "write the result as a NEW row with builder "
                             "'core-plane-lane+opus-topup'. The skeleton row "
                             "is never mutated.")
    parser.add_argument("--no-archive", action="store_true",
                        help="--merge-into: keep re-fetched pages in memory "
                             "only (spans are still verified; they cannot be "
                             "re-verified later)")
    parser.add_argument("-o", "--out", default=None,
                        help="--merge-into --dry-run: write the merged "
                             "reference JSON here instead of the database")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.seed_calibration:
        return asyncio.run(seed_calibration(args))
    if args.merge_into:
        if not args.path:
            parser.error("--merge-into needs the additions file as its "
                         "positional argument")
        return asyncio.run(merge_topup(args))
    if not args.path:
        parser.error("a reference path is required (or --seed-calibration)")
    return asyncio.run(load_reference(args))


if __name__ == "__main__":
    sys.exit(main())
