#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G3 — RE-GATE the correctness instrument on a FRESH draw.

The grader refuses to publish a number unless a passing ``grader_calibrations``
row covers its rubric sha AND every model id it would use
(``_correctness_calibration.py``). That refusal is the whole of G3's mechanism:
change the rubric, repoint a family at another model, or move the claim grain,
and the instrument STOPS until somebody re-measures whether the families still
read the rubric the same way. This script is that re-measurement.

WHAT IT DOES

  1. draws a FRESH sample of claims from the LIVE population for one target at
     one stamp — the same freeze + segmenter + exclusions the job itself runs,
     so the calibration is measured on the claims the job actually grades;
  2. builds ONE byte-identical, leak-scanned packet (the same builder);
  3. grades every drawn claim with EVERY family — no triage here: a gate is a
     question about whether the families agree, and you cannot ask it of claims
     two of them never saw;
  4. computes pooled and pairwise exact-label agreement (the ported
     ``family_agreement``), applies PREREG_P1 §6's bars — pooled >= 0.75 AND
     every pair >= 0.70 — and writes the row.

NOTHING SPENDS WITHOUT ``--grade``. The default run draws, builds, prints the
packet sha and the estimated cost, and stops. ``--grade`` additionally requires
``--cap`` (USD per paid family), and the cap is checked BEFORE each call against
the largest cost seen so far, so the run stops before the call that would
breach rather than after it. F0 is the $0 core plane and costs nothing either
way.

A FIRED GATE IS THE INSTRUMENT WORKING. A failing run still writes its row, with
``gate_pass = false`` — the refusal downstream then keeps the grader off until
somebody decides what to do, which is the correct outcome and not an error to
be worked around. ``--dry-run`` skips the write entirely.

RUN IT WHERE THE PLANES ARE. It needs the substrate (``LEGBA_DATA_PG_*``), and
``--grade`` additionally needs the registry (``LEGBA_REGISTRY_API_URL`` /
``LEGBA_REGISTRY_API_TOKEN``) and the vault (``LEGBA_DATA_MASTER_KEY``) to build
the same LLM handlers the runtime builds — no second HTTP client, no key in a
flag, no key ever printed.

  # $0 — draw + packet + estimate, no call
  PYTHONPATH=src python3 scripts/correctness_regate.py --target country_watch_il

  # the graded re-gate, in the registry container
  PYTHONPATH=/app/src python3 /app/scripts/correctness_regate.py \\
      --target country_watch_il --grade --cap 0.25

  # the same gate on the FROZEN v4 packet instead of a live draw
  PYTHONPATH=/app/src python3 /app/scripts/correctness_regate.py \\
      --packet planning/PROGRAM1_2026-09-16/calibration_v4/packet_F0.json \\
      --grade --cap 0.05 --notes "F3 repoint to mistral-medium-3.1"

``--packet`` — THE FROZEN ATOM SET, and why it is worth having beside the live
draw. A live draw measures whether the families agree TODAY on TODAY's claims.
That is the right instrument for a rubric change. It is the WRONG instrument
for a MODEL change: when one family is repointed (OpenRouter removed
``mistral-large-2512`` on 2026-09-20 and the judge component now serves
``mistral-medium-3.1``), the question is whether the NEW model reads the rubric
the way the old one did — and the only way to ask that is to hand it the SAME
atoms the passing gate was measured on. ``--packet`` takes the v4 packet
``build_p1_sample.py`` froze (``{round, packet, note_to_grader, rubric,
output_contract, items[{p1_id, assertion, reference}]}``) and grades it exactly
as a live draw is graded: all three families, the same ``--grade``/``--cap``
discipline, the same pooled >= 0.75 / pairwise >= 0.70 bars, the same row.

The loader REFUSES a packet whose embedded ``rubric`` is not byte-identical to
the one this build pins. The row it would write carries THIS build's
``rubric_sha``, so grading a different rubric's bytes under that sha is exactly
the silent re-labelling the digest exists to prevent. Same for the output
contract: the parser is built from it.

A LIVE DRAW STAYS THE DEFAULT. ``--packet`` and ``--target`` are mutually
exclusive, and the draw knobs (``--n``, ``--seed``, ``--as-of``,
``--head-window-days``) are REFUSED with ``--packet`` rather than silently
ignored — a flag that does nothing is a flag somebody will believe.
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
from uuid import uuid4

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _correctness_adjudicate as ADJ,
)
from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _correctness_calibration as CAL,
)
from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _correctness_grade as GRADE,
)
from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _correctness_packet as PACKET,
)
from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _correctness_segment as SEG,
)
from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    correctness_grader as JOB,
)
from legba.data.analysts.deterministic_handlers._correctness_rubric import (  # noqa: E402
    OUTPUT_CONTRACT,
    RUBRIC_NAME,
    RUBRIC_SHA256,
)

# H12 — the gate method version rides the row (draw + coverage rule it ran
# under); _correctness_calibration.METHOD_VERSION is its single source.
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


class FrozenPacketError(ValueError):
    """A frozen packet this script refuses to gate on. Loud, never a fallback."""


def load_frozen_packet(path: str) -> dict:
    """A frozen v4 atom packet, CHECKED against what this build would publish.

    ``build_p1_sample.py``'s shape: ``{round, packet, note_to_grader, rubric,
    output_contract, items[{p1_id, assertion, reference}]}``. It is NOT rebuilt
    through ``PACKET.build_packet`` — that builder mints ``DR-``/``CR-`` ids
    from live heads and would refuse the ``P1-`` namespace the frozen draw used
    — so every invariant the builder would have enforced is enforced HERE
    instead, and each refusal says which one fired.

    THE RUBRIC CHECK IS THE LOAD-BEARING ONE. The row this run writes carries
    this build's ``RUBRIC_SHA256``. A packet carrying a different rubric's bytes
    would put two rubrics under one sha, which is the exact silent re-labelling
    the digest exists to prevent, so it is refused rather than warned about.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            packet = json.load(handle)
    except (OSError, ValueError) as exc:
        raise FrozenPacketError(f"{path}: cannot be read as JSON — {exc}")
    if not isinstance(packet, dict):
        raise FrozenPacketError(
            f"{path}: the top level is {type(packet).__name__}, not the packet "
            "object build_p1_sample.py writes"
        )

    rubric = packet.get("rubric")
    if not isinstance(rubric, str) or not rubric:
        raise FrozenPacketError(
            f"{path}: no `rubric` field. A calibration row names a rubric_sha; "
            "a packet that does not carry the rubric it was graded under "
            "cannot be checked against it."
        )
    actual = hashlib.sha256(rubric.encode("utf-8")).hexdigest()
    if actual != RUBRIC_SHA256:
        raise FrozenPacketError(
            f"{path}: the packet's rubric is sha256 {actual}, this build pins "
            f"{RUBRIC_SHA256} ({RUBRIC_NAME}). The row this run would write "
            "carries THIS build's sha, so gating on those bytes would pool two "
            "rubrics under one identity. Refusing."
        )
    contract = packet.get("output_contract")
    if contract != OUTPUT_CONTRACT:
        raise FrozenPacketError(
            f"{path}: the packet's output_contract is not this build's. The "
            "reply parser is built from the contract; a packet written against "
            "another one would be scored by a parser it never agreed to."
        )

    items = packet.get("items")
    if not isinstance(items, list) or not items:
        raise FrozenPacketError(f"{path}: no `items` to grade")
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise FrozenPacketError(f"{path}: item {index} is not an object")
        atom_id = str(item.get("p1_id") or "").strip()
        if not atom_id:
            raise FrozenPacketError(
                f"{path}: item {index} has no `p1_id` — the label rows are "
                "keyed on it and an unkeyed atom cannot be scored"
            )
        if atom_id in seen:
            raise FrozenPacketError(
                f"{path}: duplicate p1_id {atom_id} — two atoms a grader "
                "cannot tell apart"
            )
        seen.add(atom_id)
        if not str(item.get("assertion") or "").strip():
            raise FrozenPacketError(
                f"{path}: item {atom_id} has an empty assertion"
            )
        if not item.get("reference"):
            raise FrozenPacketError(
                f"{path}: item {atom_id} carries no reference — there would be "
                "nothing to grade it against"
            )

    leaks = PACKET.scan_packet(packet)
    if leaks:
        raise FrozenPacketError(
            f"{path}: LEAK SCAN FAILED — {PACKET.leak_summary(leaks)}. An "
            "external family must never be handed the platform's own telemetry."
        )
    return packet


def _iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _open_store():
    from legba.data.postgres import PostgresStore

    store = PostgresStore.from_env()
    await store.connect()
    return store


async def build_handlers(component_ids: dict[str, str], store) -> dict:
    """The SAME handlers the runtime builds, from the SAME registry + vault.

    Rolling a second HTTP client here would mean the re-gate measured a
    transport the job does not use — a different timeout, a different price
    table, a different way of reading ``usage`` — and the number would then be
    about this script rather than about the instrument.
    """
    from legba.data.registry.credentials import CredentialVault
    from legba.runtime.analyst_deps_builder import (
        build_llm_handler_from_stack_component,
    )
    from legba.runtime.registry_client import RegistryHTTPClient

    vault = CredentialVault(store)

    async def _secrets_resolve(secret_id: str) -> bytes:
        return await vault.resolve(secret_id)

    registry_client = RegistryHTTPClient()
    handlers: dict[str, object] = {}
    for family, component_id in component_ids.items():
        try:
            handlers[family] = await build_llm_handler_from_stack_component(
                component_id,
                registry_client=registry_client,
                secrets_resolve=_secrets_resolve,
            )
        except Exception as exc:  # noqa: BLE001 — a dead family is a finding
            print(f"  {family}: UNAVAILABLE ({component_id}) — {exc}")
    return handlers


async def run(args: argparse.Namespace) -> int:
    from legba.data.provenance import verify
    from legba.data.analysts.deterministic_handlers import scorecard_banding

    as_of = _iso(args.as_of) if args.as_of else datetime.now(timezone.utc)
    models = GRADE.model_ids()
    dimensions = tuple(scorecard_banding.DIMENSIONS) + (JOB.PROLIFERATION,)

    print(f"RE-GATE — {RUBRIC_NAME} sha256 {RUBRIC_SHA256}")
    if args.packet:
        print(f"  packet    : {args.packet}  (FROZEN atom set — no live draw)")
    else:
        print(f"  target    : {args.target}")
        print(f"  as_of     : {as_of.isoformat()}")
    print(f"  models    : {json.dumps(models, sort_keys=True)}")
    print(f"  bars      : pooled >= {ADJ.POOLED_BAR}, every pair >= "
          f"{ADJ.PAIRWISE_BAR}  (PREREG_P1 §6)")

    # Read and CHECK the frozen packet before anything opens a connection: a
    # packet this script would refuse must cost nothing at all.
    frozen = None
    if args.packet:
        try:
            frozen = load_frozen_packet(args.packet)
        except FrozenPacketError as exc:
            print(f"  REFUSED — {exc}")
            return 2

    store = await _open_store()
    try:
        if frozen is not None:
            packet = frozen
            drawn = list(packet["items"])
            packet_sha = PACKET.packet_sha256(packet)
            seed = None
            print(f"  atoms     : {len(drawn)} frozen atom(s) — the SAME set "
                  "the passing gate was measured on, which is the only way to "
                  "ask whether a repointed model reads the rubric the old one "
                  "did")
            print(f"  packet    : sha256 {packet_sha}  "
                  f"({len(PACKET.packet_text(packet))} chars, leak-clean, "
                  "rubric byte-identical to the pin)")
        else:
            drawn, packet, packet_sha, seed, code = await _draw_live_packet(
                store, args, as_of, models, dimensions, verify,
            )
            if code is not None:
                return code

        estimate = sum(
            float(GRADE.FAMILIES[f]["est_first_cost_usd"]) * len(drawn)
            for f in GRADE.PAID_FAMILIES
        )
        if not args.grade:
            print(f"\n  NOT GRADING (no --grade). Estimated paid cost if you "
                  f"did: ~${estimate:.4f} across {GRADE.PAID_FAMILIES}; F0 is "
                  "$0. Re-run with --grade --cap <usd-per-paid-family>.")
            return 0
        if args.cap is None:
            print("\n  --grade requires --cap (USD per paid family run). "
                  "Refusing to spend without a stated ceiling.")
            return 2
        return await _gate(store, args, packet, packet_sha, drawn, seed,
                           as_of, models)
    finally:
        await store.close()


async def _draw_live_packet(store, args, as_of, models, dimensions, verify):
    """The LIVE draw — unchanged: freeze, segment, draw, build, leak-scan.

    Returns ``(drawn, packet, packet_sha, seed, exit_code)``; a non-None exit
    code means the caller returns it and nothing is graded.
    """
    nothing = (None, None, None, None)
    async with store.acquire() as conn:
        grace = JOB.reference_grace_days()
        grace_days = int(grace[0] if isinstance(grace, tuple) else grace)
        ref_row = await conn.fetchrow(
            JOB._REFERENCE_SQL, args.target, as_of, grace_days,
        )
        if ref_row is None:
            print(
                f"  NO REFERENCE whose window contains {as_of.isoformat()} "
                f"for {args.target}. A calibration needs the same evidence "
                "the job grades against; refusing to draw."
            )
            return nothing + (2,)
        reference_raw = ref_row["ref_json"]
        if isinstance(reference_raw, (str, bytes)):
            reference_raw = json.loads(reference_raw)
        heads, missing = await JOB.freeze_heads(
            conn, args.target, as_of,
            window_days=args.head_window_days, dimensions=dimensions,
        )
    if not heads:
        print("  NO HEADS as of the stamp — nothing to draw from.")
        return nothing + (2,)
    if missing:
        print(f"  missing heads (recorded, never padded): {missing}")

    segmented = SEG.segment_all(
        heads, verify._segment_claims, verify._is_judgeable_claim,
        frozenset(dimensions),
    )
    pool = segmented["claims"]
    print(f"  claims    : {len(pool)} kept of "
          f"{segmented['totals']['n_spans']} spans  "
          f"excluded={segmented['totals']['excluded_by_reason']}")
    if not pool:
        print("  the segmenter kept no claim — nothing to draw from.")
        return nothing + (2,)

    seed = args.seed or CAL.draw_seed(
        RUBRIC_SHA256, list(models.values()),
        as_of.astimezone(timezone.utc).date().isoformat(),
    )
    drawn = CAL.draw_calibration_sample(pool, args.n, seed)
    print(f"  seed      : {seed[:16]}…  (derived from rubric+models+date)")
    print(f"  drawn     : {len(drawn)} atom(s)  by_unit="
          + json.dumps({
              unit: sum(1 for c in drawn if c["analyst_id"] == unit)
              for unit in sorted({c['analyst_id'] for c in drawn})
          }))

    country = str(args.target)[-2:].upper()
    reference = PACKET.reduce_reference(reference_raw, country)
    packet = PACKET.build_packet(
        drawn, reference, packet_kind=f"regate_{country}",
    )
    leaks = PACKET.scan_packet(packet)
    if leaks:
        print(f"  LEAK SCAN FAILED — {PACKET.leak_summary(leaks)}. "
              "Refusing to call any family.")
        return nothing + (2,)
    packet_sha = PACKET.packet_sha256(packet)
    print(f"  packet    : sha256 {packet_sha}  "
          f"({len(PACKET.packet_text(packet))} chars, leak-clean, "
          "byte-identical across families by construction)")
    return drawn, packet, packet_sha, seed, None


async def _gate(store, args, packet, packet_sha, drawn, seed, as_of, models):
    """Grade EVERY family over the packet, score it, and write the row.

    Shared by both front halves on purpose: a frozen packet and a live draw must
    be graded by the same code under the same cap discipline, or the two gates
    would not be comparable and the frozen one would be worth nothing.
    """
    handlers = await build_handlers(
        {f: str(GRADE.FAMILIES[f]["component"]) for f in GRADE.FAMILY_ORDER},
        store,
    )
    if len(handlers) < 2:
        print("  fewer than two families resolved — an agreement rate over "
              "one family is not one. Refusing to score.")
        return 2

    system_message = GRADE.build_system_message()
    policy = GRADE.span_policy()
    labels_by_family: dict[str, dict[str, str]] = {}
    spend: dict[str, float] = {}
    for family in GRADE.FAMILY_ORDER:
        llm = handlers.get(family)
        if llm is None:
            continue
        paid = bool(GRADE.FAMILIES[family]["paid"])
        worst = float(GRADE.FAMILIES[family]["est_first_cost_usd"])
        spent = 0.0
        rows: dict[str, str] = {}
        for item in packet["items"]:
            if paid and GRADE.would_breach(spent, args.cap, worst):
                print(f"  {family}: CAP GUARD — ${spent:.6f} spent, next "
                      f"est ${worst:.6f}, cap ${args.cap:.2f}. STOPPING "
                      f"BEFORE {item['p1_id']}.")
                break
            row = await GRADE.grade_one(
                item, family, llm,
                system_message=system_message, policy=policy,
            )
            cost = float(row.get("cost_usd") or 0.0)
            spent += cost
            worst = max(worst, cost)
            rows[str(item["p1_id"])] = str(row.get("verdict") or "")
        labels_by_family[family] = rows
        spend[family] = round(spent, 6)
        print(f"  {family}: {len(rows)} atom(s), ${spent:.6f}")

    agreement = ADJ.family_agreement(labels_by_family)
    print(f"\n  AGREEMENT")
    for pair in agreement["pairs"]:
        print(f"    {pair['a']} x {pair['b']}  {pair['n_agree']}/"
              f"{pair['n_shared']} = {pair['rate']}")
    print(f"    pooled {agreement['pooled_rate']}  "
          f"(bar {agreement['pooled_bar']}, pairwise floor "
          f"{agreement['pairwise_bar']})")
    print(f"    {agreement['verdict']}")
    gate_pass = bool(agreement["pass"])
    print(f"    GATE: {'PASS' if gate_pass else 'FIRE'}")
    print(f"    spend: {spend}")

    if args.dry_run:
        print("\n  DRY RUN — no grader_calibrations row written")
        return 0
    if agreement["pooled_rate"] is None:
        print("\n  UNMEASURED (no shared atoms) — a gate with no overlap is "
              "not a gate. Refusing to write a row.")
        return 2

    if args.packet:
        default_notes = (
            f"G3 re-gate on the FROZEN packet {args.packet} "
            f"(sha256 {packet_sha[:16]}…) n={len(drawn)} "
            f"spend={json.dumps(spend, sort_keys=True)}"
        )
    else:
        default_notes = (
            f"G3 re-gate on a fresh draw: target={args.target} "
            f"as_of={as_of.isoformat()} n={len(drawn)} seed={seed[:16]}… "
            f"spend={json.dumps(spend, sort_keys=True)}"
        )
    notes = args.notes or default_notes
    graded_models = {f: models[f] for f in sorted(labels_by_family)}
    async with store.acquire() as conn:
        row = await conn.fetchrow(
            _INSERT_CALIBRATION, uuid4(), RUBRIC_SHA256,
            json.dumps(graded_models, sort_keys=True),
            Decimal(str(agreement["pooled_rate"])),
            json.dumps(agreement["pairs"]), gate_pass, packet_sha,
            len(drawn), Decimal(str(ADJ.POOLED_BAR)),
            Decimal(str(ADJ.PAIRWISE_BAR)), notes, CALIBRATION_METHOD_VERSION,
        )
    if row is None:
        print("\n  ALREADY SCORED — this (rubric, packet, model set) is "
              "already a row. A second roll of the same dice is not "
              "independent confirmation; nothing written.")
        return 0
    print(f"\n  wrote grader_calibrations id={row['id']} "
          f"gate_pass={gate_pass}")
    return 0 if gate_pass else 1


#: The draw knobs. With ``--packet`` there is no draw, so these are REFUSED
#: rather than ignored — a flag that silently does nothing is a flag somebody
#: will believe fired.
DRAW_ONLY_FLAGS: tuple[tuple[str, str], ...] = (
    ("target", "--target"), ("as_of", "--as-of"), ("n", "--n"),
    ("seed", "--seed"), ("head_window_days", "--head-window-days"),
)


def build_parser() -> argparse.ArgumentParser:
    """The CLI, as its own function so the parsing is testable without a DB."""
    parser = argparse.ArgumentParser(
        description="G3 — re-run the correctness calibration on a fresh draw "
                    "(or on a FROZEN atom packet) and write a "
                    "grader_calibrations row."
    )
    parser.add_argument("--target", default=None,
                        help="country target, e.g. country_watch_il. Required "
                             "unless --packet is given.")
    parser.add_argument("--packet", default=None,
                        help="path to a FROZEN v4 atom packet (build_p1_sample"
                             ".py's shape) to gate on INSTEAD of a live draw. "
                             "Use it when a FAMILY moved and the question is "
                             "whether the new model reads the rubric the way "
                             "the gated one did. Mutually exclusive with "
                             "--target.")
    parser.add_argument("--as-of", default=None,
                        help="ISO instant (default: now)")
    parser.add_argument("--n", type=int, default=None,
                        help=f"atoms drawn (default {CAL.DEFAULT_DRAW_N}, "
                             "PREREG_P1 §2's n)")
    parser.add_argument("--seed", default=None,
                        help="override the DERIVED seed. Use only for a "
                             "deliberate re-draw; a seed somebody picks is a "
                             "seed somebody can pick again until it passes.")
    parser.add_argument("--head-window-days", type=int, default=None,
                        help=f"default {JOB.DEFAULT_HEAD_WINDOW_DAYS}")
    parser.add_argument("--grade", action="store_true",
                        help="actually call the families (PAID for F2/F3)")
    parser.add_argument("--cap", type=float, default=None,
                        help="hard USD cap per PAID family run; required with "
                             "--grade")
    parser.add_argument("--dry-run", action="store_true",
                        help="grade but do not write the calibration row")
    parser.add_argument("--notes", default=None)
    return parser


def resolve_args(parser: argparse.ArgumentParser,
                 argv: list[str] | None = None) -> argparse.Namespace:
    """Parse, then police the two populations against each other.

    EXACTLY ONE population. ``--target`` draws live; ``--packet`` gates a frozen
    set. Accepting both would leave the script choosing silently, and the row it
    writes names only the packet sha — so nobody reading the table afterwards
    could tell which population was measured.
    """
    args = parser.parse_args(argv)
    if bool(args.target) == bool(args.packet):
        parser.error(
            "exactly one of --target (live draw, the default way) or --packet "
            "(a frozen atom set) is required"
        )
    if args.packet:
        offenders = [
            flag for attr, flag in DRAW_ONLY_FLAGS
            if flag != "--target" and getattr(args, attr) is not None
        ]
        if offenders:
            parser.error(
                f"{', '.join(offenders)} only mean something for a live draw; "
                "--packet gates a frozen atom set and nothing is drawn. "
                "Refusing rather than ignoring them."
            )
    if args.n is None:
        args.n = CAL.DEFAULT_DRAW_N
    if args.head_window_days is None:
        args.head_window_days = JOB.DEFAULT_HEAD_WINDOW_DAYS
    return args


def main() -> int:
    args = resolve_args(build_parser())
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
