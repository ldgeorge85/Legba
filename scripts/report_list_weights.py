#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Each registry list route's default page, measured against its declared
budget (``registry/list_weights.py``) — READ-ONLY GETs, nightly-runnable.

One GET per route with no parameters (the default limit and weight); a path
parameter takes a sample id (``--analyst-id``, ``--target-id``); a route that
needs a required parameter it is not given is listed as skipped. Prints
route, status, bytes, budget and verdict — sizes only, never the bodies.

    python3 scripts/report_list_weights.py [--base http://127.0.0.1:8090]

Token: ``scripts/_token.py`` (operator). Exit 1 when any route is over budget.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from legba.data.registry.list_weights import LIST_ROUTES  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--analyst-id", default="world_assessor")
    ap.add_argument("--target-id", default="country_g20_br")
    args = ap.parse_args()
    from _token import resolve_token

    token = resolve_token(principal="operator")
    over = 0
    for path, w in sorted(LIST_ROUTES.items()):
        url_path = path.replace("{analyst_id}", args.analyst_id).replace(
            "{target_id}", args.target_id)
        if "{" in url_path:
            print(f"skip   {path}  (needs a path id)")
            continue
        req = urllib.request.Request(
            args.base + url_path, headers={"Authorization": f"Bearer {token}"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                n, code = len(r.read()), r.status
        except urllib.error.HTTPError as e:
            n, code = len(e.read()), e.code
        ms = (time.monotonic() - t0) * 1000
        if code != 200:
            print(f"skip   {path}  HTTP {code} (a required parameter)")
            continue
        verdict = "ok" if n <= w.page_budget else "OVER"
        over += verdict == "OVER"
        weight = "light" if w.light_by_default else "exempt"
        print(f"{verdict:<6} {path}  {n / 1024:.1f} KB / {w.page_budget / 1024:.0f} KB"
              f"  {ms:.0f} ms  [{weight}]")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
