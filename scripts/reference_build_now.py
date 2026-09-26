#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ON-DEMAND: force one reference build, now, through the REAL path.

The builder's ordinary trigger is its own cadence reminder (hourly at :43, which
builds only when a target is overdue). This is the manual door, and it is
deliberately the SAME door every other analyst has rather than a second entry
point of its own: a PUT to the Dapr sidecar's actor-method API,
``…/v1.0/actors/AnalystActor/<actor_id>/method/run`` with
``{"trigger_kind": "method"}`` — the shape ``scripts/correctness_grade_now.py``
uses, which is the shape ``scripts/trigger_multi_country_runs.py`` uses for
TargetActors. A forced run skips the cadence cooldown
(``_FORCED_TRIGGERS = {"method"}``) and otherwise takes the identical path: the
same deps bundle, the same wired core-plane model, the same web pack, the same
fences.

THE OPTION KEYS ARE THE GRADER'S, AND SO IS THE ENVELOPE. ``--target`` travels
as ``reference_targets`` and ``--as-of`` as ``as_of`` — the SAME key the
correctness grader reads for the same concept — and both ride inside the body's
``options`` object, which is the ONE channel ``dapr_actors`` merges into a
sub-handler's option mapping. A knob at the TOP level of the body is ignored
silently: that is precisely how G1's D1 defect hid, and the G1-fix lane settled
this shape. That is not tidiness: an operator pinning a fortnight passes one
value to both jobs, so the reference and the grading of it can be anchored to
one instant. ``tests/runtime/test_reference_builder_wiring.py`` asserts the envelope and
every key in it against the live catalog, so a drift in either is a red test
rather than a run that quietly used the defaults.

A build forced this way is STILL INERT unless the operator has flipped
``LEGBA_REFERENCE_BUILDER_ENABLED``; off, it returns a receipt saying so and
does nothing at all.

  # the next target the cadence would take (oldest ATTEMPT first, NULLS first,
  # retry backoff honoured — see docs/REFERENCE_BUILDER.md "Which due target")
  PYTHONPATH=src python3 scripts/reference_build_now.py

  # one named target, bypassing the cadence
  PYTHONPATH=src python3 scripts/reference_build_now.py --target country_watch_il

  # build it but DO NOT write it — the reference travels on the receipt, for
  # reading before it counts against a published number
  PYTHONPATH=src python3 scripts/reference_build_now.py \\
      --target country_watch_il --dry-run

BE PATIENT. One build is ~8 minutes of core-plane time (R1 measured 410-498
seconds over 74-80 tool calls), so the default timeout is 30 minutes.

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — the bearer for the head-version lookup
  * ``DAPR_HTTP_URL``        — defaults to http://127.0.0.1:3500
No key is ever printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

REGISTRY = os.environ.get(
    "LEGBA_REGISTRY_URL", "http://127.0.0.1:8090/api/v1/registry"
)
TOKEN = os.environ.get("LEGBA_REGISTRY_TOKEN", "dev")
DAPR = os.environ.get("DAPR_HTTP_URL", "http://127.0.0.1:3500")
DESCRIPTOR_ID = "reference_builder"


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


def build_body(args: argparse.Namespace) -> dict[str, object]:
    """The forced-run body, in the ONE shape the actor actually reads.

    ``trigger_kind`` is the actor's own TOP-LEVEL contract. The handler's knobs
    go inside ``options`` — the per-run channel ``dapr_actors`` merges into the
    sub-handler's option mapping. Top-level keys other than the actor's own are
    IGNORED, with no error and no log line on the handler side, which is exactly
    how G1's D1 defect hid: the on-demand run stamped ``as_of=now`` although the
    body carried a past stamp, because the key was in the wrong place and
    nothing said so. The G1-fix lane settled this shape; this script speaks it,
    and ``tests/runtime/test_reference_builder_wiring.py`` pins that it does.

    Every key inside ``options`` is a DECLARED knob
    (``handler_options.HANDLER_OPTIONS['reference_builder']``), so a typo is
    rejected at the runtime boundary with a receipt entry rather than silently
    ignored here.
    """
    options: dict[str, object] = {}
    if args.target:
        options["reference_targets"] = list(args.target)
    if args.as_of:
        options["as_of"] = args.as_of
    if args.dry_run:
        options["reference_dry_run"] = True
    if args.tool_call_cap:
        options["tool_call_cap"] = int(args.tool_call_cap)
    if args.max_targets:
        options["max_targets_per_run"] = int(args.max_targets)
    body: dict[str, object] = {"trigger_kind": "method"}
    if options:
        body["options"] = options
    return body


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Force one reference build through the analyst actor's "
                    "own run method."
    )
    parser.add_argument(
        "--target", action="append", default=None,
        help="build for this target (repeatable), bypassing BOTH the cadence "
             "and the retry backoff — naming a target is an operator saying "
             "'build this one now'. Omitted: the target the cadence would take "
             "(the due target waiting longest since its last build ATTEMPT, "
             "never-attempted first).",
    )
    parser.add_argument(
        "--as-of", default=None,
        help="ISO instant the built window ENDS at (default: now). The same "
             "option key the correctness grader reads.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="build and fence the reference but DO NOT write it to "
             "unit_references; it travels on the receipt's "
             "data.per_target[].reference instead. Costs the same core-plane "
             "time as a real build.",
    )
    parser.add_argument(
        "--tool-call-cap", type=int, default=None,
        help="override the per-build search+fetch budget (default 80).",
    )
    parser.add_argument(
        "--max-targets", type=int, default=None,
        help="targets this run may build (default 1 — the stagger).",
    )
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument(
        "--save", default=None,
        help="write the response JSON to this path as well as printing it. "
             "Useful with --dry-run, where the built reference is in the body.",
    )
    args = parser.parse_args()

    body = build_body(args)

    try:
        version = head_version(DESCRIPTOR_ID)
    except Exception as exc:  # noqa: BLE001 — the reason is the whole output
        print(f"head-version lookup failed for {DESCRIPTOR_ID}: {exc}")
        print("Is the descriptor registered and active? "
              "descriptors/analyst_reference_builder.yaml")
        return 1

    actor_id = f"analyst::{DESCRIPTOR_ID}::{version[:16]}"
    url = (
        f"{DAPR}/v1.0/actors/AnalystActor/"
        f"{urllib.parse.quote(actor_id, safe='')}/method/run"
    )
    print(f"forcing {actor_id}")
    print(f"  body: {json.dumps(body, sort_keys=True)}")
    print(f"  a build is ~8 minutes of core-plane time; timeout "
          f"{args.timeout}s")
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            payload = response.read().decode("utf-8", "replace")
            print(f"  HTTP {response.status}")
            if args.save:
                with open(args.save, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                print(f"  response written to {args.save} "
                      f"({len(payload):,} bytes)")
                print(f"  {payload[:2000]}")
            else:
                print(f"  {payload[:8000]}")
        return 0
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace") if exc.fp else ""
        print(f"  HTTP {exc.code} {exc.reason}")
        print(f"  {payload[:4000]}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
