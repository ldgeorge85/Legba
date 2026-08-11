#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pause the five frozen rsshub AP-topic sources (2026-08-03 incident).

The apnews topic feeds froze upstream on 2026-07-28 — every poll re-serves the
same 39-item snapshot, and dedup bumps ``fetched_at`` on each re-encounter, so
six-day-old stories sit at the top of every recency-ordered slice forever (the
journal hyperfocus). Pausing stops the re-bumps; the frozen rows then age out
of all slices naturally within the 72h window. Reverse with --resume once the
F-B freshness fix + AP re-route land.

Usage:  python3 scripts/ops_pause_apnews_frozen.py [--resume] [--env-file .env]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

FEEDS = ("north_korea", "taiwan", "niger", "haiti", "drcongo")
REGISTRY = "http://127.0.0.1:8090"


def _token(env_file: str) -> str:
    with open(env_file, encoding="utf-8") as fh:
        for line in fh:
            if line.strip().startswith("LEGBA_REGISTRY_API_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"no LEGBA_REGISTRY_API_TOKEN in {env_file}")


def main() -> int:
    target = "active" if "--resume" in sys.argv else "paused"
    env_file = ".env"
    if "--env-file" in sys.argv:
        env_file = sys.argv[sys.argv.index("--env-file") + 1]
    tok = _token(env_file)
    hdrs = {"Authorization": f"Bearer {tok}"}
    failures = 0
    for sid in FEEDS:
        url = f"{REGISTRY}/api/v1/registry/descriptors/source/source.rsshub.apnews.{sid}"
        try:
            doc = json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers=hdrs), timeout=20))
            body = doc.get("body", doc)
            if body["identity"]["state"] == target:
                print(f"{sid}: already {target}")
                continue
            body["identity"]["state"] = target
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(), method="PUT",
                headers={**hdrs, "Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=20)
            print(f"{sid}: -> {target} ({resp.status})")
        except urllib.error.HTTPError as exc:
            failures += 1
            print(f"{sid}: HTTP {exc.code} {exc.read()[:200]!r}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
