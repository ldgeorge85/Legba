# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One-shot: invoke ``run`` on each multi-country target actor via the Dapr
sidecar so the first RSS pull fires immediately, no waiting for the 15-min
reminder. Same call shape as the spike test's
``ActorProxy.invoke("run", {"trigger_kind": "method"})`` path but routed
through daprd's HTTP actor-method API (3500/v1.0/actors/...) — no SDK
dependency needed in the operator shell.

Sister script to ``scripts/bringup_register_multi_country_targets.py``: that
script registers + reconciler activates the actors with reminders; this one
short-circuits the first reminder fire so the validation window doesn't
have to be 15 minutes long.

Actor-id discovery: the version prefix in the actor_id is the first 16
hex chars of the descriptor content hash. Fetched dynamically from the
registry so we don't have to hard-code per run.
"""
from __future__ import annotations

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

TARGETS = [
    "japan_news",
    "germany_news",
    "nigeria_news",
    "mexico_news",
    "turkey_news",
]


def _head_version(target_id: str) -> str:
    req = urllib.request.Request(
        f"{REGISTRY}/descriptors/target/{target_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["version"]


def main() -> int:
    failures: list[str] = []
    # Optional positional args restrict the run to a subset (e.g. for
    # re-triggering a single target after a runtime restart). No args =
    # run all five.
    targets = sys.argv[1:] if len(sys.argv) > 1 else TARGETS
    for did in targets:
        try:
            version = _head_version(did)
        except Exception as exc:
            failures.append(f"{did}: head-lookup {exc}")
            continue
        vprefix = version[:16]
        aid = f"target::{did}::{vprefix}"
        url = (
            f"{DAPR}/v1.0/actors/TargetActor/"
            f"{urllib.parse.quote(aid, safe='')}/method/run"
        )
        body = json.dumps({"trigger_kind": "method"}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="PUT",
            headers={"Content-Type": "application/json"},
        )
        try:
            r = urllib.request.urlopen(req, timeout=180)
            out = r.read().decode("utf-8", "replace")
            print(f"{did}: HTTP {r.status} body={out[:400]}")
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace") if e.fp else ""
            print(f"{did}: HTTP {e.code} {e.reason} body={payload[:400]}")
            failures.append(f"{did}: HTTP {e.code}")
        except Exception as e:
            print(f"{did}: ERROR {type(e).__name__}: {e}")
            failures.append(f"{did}: {type(e).__name__}: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
