#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ON-DEMAND: force one correctness-grading sweep, now, through the REAL path.

The grader's ordinary trigger is its own cadence reminder (daily 01:20 UTC).
This is the manual door, and it is deliberately the SAME door every other
analyst has rather than a second entry point of its own: a PUT to the Dapr
sidecar's actor-method API, ``…/v1.0/actors/AnalystActor/<actor_id>/method/run``
with ``{"trigger_kind": "method"}`` — the shape
``scripts/trigger_multi_country_runs.py`` uses for TargetActors. A forced run
skips the cadence cooldown (``_FORCED_TRIGGERS = {"method"}``) and otherwise
takes the identical path: the same deps bundle, the same three wired families,
the same calibration interlock, the same ceiling.

A sweep forced this way is STILL INERT unless the operator has flipped
``LEGBA_CORRECTNESS_GRADER_ENABLED``; off, it returns a receipt saying so and
writes nothing. And it is still idempotent: a (unit, head, reference, rubric)
already graded is skipped BEFORE any paid call, so forcing a second run in the
same day re-reads and re-decides but does not re-spend.

  # the whole population (every target carrying a live reference)
  PYTHONPATH=src python3 scripts/correctness_grade_now.py

  # one target, at a past stamp (a replay; a no-op if already graded)
  PYTHONPATH=src python3 scripts/correctness_grade_now.py \\
      --target country_watch_il --as-of 2026-09-16T19:30:00+00:00

WHERE THE PARAMETERS GO, and why it is not obvious. The actor reads its own
contract keys (``trigger_kind``, ``target_filter``) off the TOP LEVEL of the
body and passes NOTHING else from there to the handler. The per-run parameter
channel is the NESTED ``options`` object — ``dapr_actors`` merges
``payload["options"]`` into the mapping the sub-handler reads, ahead of the
descriptor's own ``method.options`` block, which is exactly the precedence a
forced run needs (an explicit invocation beats the standing config). Sending
``as_of`` at the top level, as this script first did, drops it on the floor
SILENTLY: the run stamps ``now()``, finds no reference covering that instant and
reports "wrote no unit numbers" — which is what happened on 2026-09-16 at
22:32Z. :func:`build_body` is the one place that shape is written, and
``tests/runtime/test_correctness_grader_method_body.py`` drives the actor with
the body THIS function returns.

EXIT CODES. ``0`` only when the sweep actually wrote unit numbers. A run that
graded nothing — flag off, calibration refusal, no current reference, every head
already graded — exits ``2`` and prints the receipt's own headline and per-target
statuses. HTTP 200 is the SIDECAR saying the actor ran; it is not the instrument
saying it measured anything, and treating the two as the same is how a sweep
that graded zero units was read as a success.

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — the bearer for the head-version lookup
  * ``DAPR_HTTP_URL``        — defaults to http://127.0.0.1:3500
  * ``LEGBA_DATA_PG_*``      — the receipt read-back (host/port/user/password/db)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import asyncpg

REGISTRY = os.environ.get(
    "LEGBA_REGISTRY_URL", "http://127.0.0.1:8090/api/v1/registry"
)
TOKEN = os.environ.get("LEGBA_REGISTRY_TOKEN", "dev")
DAPR = os.environ.get("DAPR_HTTP_URL", "http://127.0.0.1:3500")
DESCRIPTOR_ID = "correctness_grader"


def head_version(descriptor_id: str) -> str:
    """The live head's content hash — the third segment of the actor id.

    Fetched rather than hardcoded: the actor id carries the descriptor version,
    so a stale prefix addresses an actor that no longer exists and the PUT 404s
    with no hint as to why.
    """
    request = urllib.request.Request(
        f"{REGISTRY}/descriptors/analyst/{descriptor_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))["version"]


#: The receipt's own numbers, read back off the row the run wrote. The PUT
#: response carries only the actor's outcome and the output row id — never the
#: finding — so "did this grade anything" is a question only the row answers.
_RECEIPT_SQL = """
SELECT title, data->'data' AS payload
  FROM analyst_outputs
 WHERE id = $1
"""


def build_body(
    targets: list[str] | None, as_of: str | None
) -> dict[str, object]:
    """The forced-run body, in the ONE shape the actor actually reads.

    ``trigger_kind`` is the actor's own top-level contract. The handler's knobs
    go inside ``options`` — the per-run channel ``dapr_actors`` merges into the
    sub-handler's option mapping. Top-level keys other than the actor's own are
    IGNORED, with no error and no log line on the handler side.
    """
    options: dict[str, object] = {}
    if targets:
        options["grader_targets"] = list(targets)
    if as_of:
        options["as_of"] = as_of
    body: dict[str, object] = {"trigger_kind": "method"}
    if options:
        body["options"] = options
    return body


async def _read_receipt(output_id: str) -> dict[str, object] | None:
    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        row = await conn.fetchrow(_RECEIPT_SQL, output_id)
    finally:
        await conn.close()
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    return {"title": row["title"], "data": payload or {}}


def report(receipt: dict[str, object]) -> int:
    """Print what the instrument says it did, and return this script's status.

    ``0`` only if a unit number was written. Everything else is a run that read,
    decided and measured NOTHING, and says which of those it was.
    """
    data = receipt.get("data") or {}
    written = int(data.get("n_units_written") or 0)
    print(f"  receipt: {receipt.get('title')}")
    print(f"  as_of={data.get('as_of')} "
          f"units_written={written} cost=${data.get('run_cost_usd')}")
    for target in data.get("per_target") or []:
        reference = target.get("reference") or {}
        age = reference.get("age_days")
        print(
            f"    [{target.get('status')}] {target.get('target_id')}: "
            f"{target.get('n_units_written')} unit(s), "
            f"{target.get('n_claims')} claim(s)"
            + ("" if age is None else f", reference_age={float(age):.2f}d")
        )
    for warning in data.get("warnings") or []:
        print(f"    WARNING: {warning}")
    if written:
        return 0
    if data.get("enabled") is False:
        print("  NOTHING GRADED: the grader flag is off "
              "(LEGBA_CORRECTNESS_GRADER_ENABLED).")
    elif data.get("refusal"):
        print(f"  NOTHING GRADED: REFUSED — {data['refusal'].get('reason')}")
    else:
        statuses = sorted(
            {str(t.get("status")) for t in (data.get("per_target") or [])}
        )
        print("  NOTHING GRADED: no unit number was written "
              f"({', '.join(statuses) or 'no targets'}).")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Force one correctness-grading sweep through the analyst "
                    "actor's own run method."
    )
    parser.add_argument("--target", action="append", default=None,
                        help="bound the sweep to this target (repeatable). "
                             "Omitted: every target carrying a live reference.")
    parser.add_argument("--as-of", default=None,
                        help="ISO instant to measure at (default: now). A "
                             "replay of a stamp already graded is a no-op.")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the receipt read-back (no Postgres "
                             "access). Exits 0 on a 200 — which says the "
                             "actor ran, NOT that anything was graded.")
    args = parser.parse_args()

    body = build_body(args.target, args.as_of)

    try:
        version = head_version(DESCRIPTOR_ID)
    except Exception as exc:  # noqa: BLE001 — the reason is the whole output
        print(f"head-version lookup failed for {DESCRIPTOR_ID}: {exc}")
        print("Is the descriptor registered? "
              "scripts/bringup_register_correctness_grader.py")
        return 1

    actor_id = f"analyst::{DESCRIPTOR_ID}::{version[:16]}"
    url = (
        f"{DAPR}/v1.0/actors/AnalystActor/"
        f"{urllib.parse.quote(actor_id, safe='')}/method/run"
    )
    print(f"forcing {actor_id}")
    print(f"  body: {json.dumps(body, sort_keys=True)}")
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            print(f"  HTTP {response.status}")
            print(f"  {raw[:4000]}")
        outcome = json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace") if exc.fp else ""
        print(f"  HTTP {exc.code} {exc.reason}")
        print(f"  {payload[:4000]}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR {type(exc).__name__}: {exc}")
        return 1

    # HTTP 200 means the ACTOR ran, not that the INSTRUMENT measured anything.
    if str(outcome.get("outcome")) != "success":
        print(f"  NOTHING GRADED: actor outcome={outcome.get('outcome')!r} "
              f"reason={outcome.get('reason')!r}")
        return 2
    output_id = outcome.get("output_id") or outcome.get("finding_id")
    if not output_id:
        print("  NOTHING GRADED: the run wrote no receipt row to read back.")
        return 2
    if args.no_verify:
        print("  --no-verify: the receipt was NOT read back; this exit code "
              "says the actor ran, not that anything was graded.")
        return 0
    try:
        receipt = asyncio.run(_read_receipt(str(output_id)))
    except Exception as exc:  # noqa: BLE001 — the reason is the whole output
        print(f"  RECEIPT READ FAILED {type(exc).__name__}: {exc}")
        print("  Cannot confirm anything was graded — exiting non-zero. "
              "Set LEGBA_DATA_PG_* or pass --no-verify.")
        return 1
    if receipt is None:
        print(f"  RECEIPT MISSING: no analyst_outputs row {output_id}.")
        return 1
    return report(receipt)


if __name__ == "__main__":
    sys.exit(main())
