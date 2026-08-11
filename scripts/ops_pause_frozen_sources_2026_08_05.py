#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pause the two genuinely-broken feeds from the 2026-08-05 quiet-source pass.

RECOMMENDATION, NOT AN AUTOMATIC ACTION. Nothing runs this on a schedule; it
issues registry PUTs and is the operator's to fire. Reverse with --resume.

    python3 scripts/ops_pause_frozen_sources_2026_08_05.py --dry-run
    python3 scripts/ops_pause_frozen_sources_2026_08_05.py
    python3 scripts/ops_pause_frozen_sources_2026_08_05.py --resume

================================ THE FINDING ================================

Seven sources carried a persistent silence deficit on the gauge. All seven were
probed live from the runtime container on 2026-08-05, and the useful result is
how FEW of them were our fault:

  FIVE ARE JUST QUIET, and are NOT in this script. For each, the newest item in
  the live feed matches our stored cursor exactly — we are not missing
  anything, the publisher has not published:
    * source.eia.press           newest 2026-07-07 (monthly STEO cadence)
    * source.rand.press          newest 2026-06-23
    * source.euvsdisinfo.cases   newest 2026-07-22
    * source.dfrlab.reports      newest 2026-07-29
    * source.pancanal.news       newest 2026-07-24
  RAND deserves a specific note because it is on record as a cursor-poison
  repeat offender: its CDN re-stamps Last-Modified/ETag on every single request,
  which is exactly the input that produced the historical poison. We are no
  longer fooled by it — the handler derives `newest_entry_ts` from real entry
  dates and ignores the noisy channel headers, and it reads 2026-06-23 through
  the noise. The Fix B / Fix C / B0-11 hardening is holding. NO CURSOR RESET IS
  WARRANTED FOR ANY OF THE SEVEN; all seven cursors track real upstream state.

  TWO ARE BROKEN UPSTREAM, and are the two below.

------------------------------------------------------------------ 1. spiegel
source.spiegel.international — the feed generator is FROZEN, not empty.
`https://www.spiegel.de/international/index.rss` answers 200 with a valid
`application/rss+xml` document carrying 20 items — and its newest <pubDate>,
its <lastBuildDate> and the channel <pubDate> are ALL pinned to
Thu, 9 Jul 2026 12:01:00 +0200. Our cursor sits on that same instant; 98
consecutive empty polls, 0 successes in 30 days. A major outlet's international
desk does not publish nothing for four weeks, so this is the generator stuck,
not the newsroom quiet. searxng confirms the URL is still the canonical
documented feed, so there is nothing to re-point to.

Same class as the apnews freeze (scripts/ops_pause_apnews_frozen.py), differing
only in that this one serves a stale document rather than a re-served snapshot.

----------------------------------------------------------------- 2. state.gov
source.stategov.press_releases — the origin serves an ERROR PAGE with HTTP 200.
Every request to `https://www.state.gov/rss-feed/press-releases/feed/` returns
200, `content-type: text/html`, and a 659,508-byte AWS S3 "Technical
Difficulties" page (`server: AmazonS3`, `x-cache: Error from cloudfront`). The
source has produced ZERO signal rows in its entire life, across 33 of 33 polls
logged "empty" and none logged as an error.

DO NOT SIMPLY RE-POINT THE URL. `https://www.state.gov/press-releases/feed/` is
the obvious candidate and it was probed too — it returns the byte-identical
659,508-byte error page. state.gov's own origin is degraded on both paths, so a
descriptor edit would move the failure, not fix it. That is why this ships as a
pause and not as a URL change: re-probe both paths before changing either.

The DETECTION half of this is already fixed in-tree and is the more valuable
half — see `_safe_parse` in src/legba/data/sources/rss.py. feedparser scrapes an
HTML page's <title>/<meta> into `.feed`, so the old "no entries and no feed
metadata" guard never fired and this was recorded HEALTHY. A 200 carrying a
document feedparser cannot identify as any feed format is now a degraded poll
with the content-type in the health record, so the next origin that does this
announces itself instead of impersonating a quiet publisher.

============================== WHAT PAUSING DOES =============================

It stops the polls. It does not delete anything, and `--resume` puts both back
to `active`. Both are worth re-probing periodically — neither failure is ours,
so both can equally well fix themselves without notice.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

#: (descriptor_id, one-line reason) — see the module docstring for the evidence.
SOURCES = (
    ("source.spiegel.international",
     "feed generator frozen at 2026-07-09; 98 consecutive empty polls"),
    ("source.stategov.press_releases",
     "origin serves a 200 + text/html S3 error page on every path; 0 rows ever"),
)
REGISTRY = "http://127.0.0.1:8090"


def _token(env_file: str) -> str:
    with open(env_file, encoding="utf-8") as fh:
        for line in fh:
            if line.strip().startswith("LEGBA_REGISTRY_API_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"no LEGBA_REGISTRY_API_TOKEN in {env_file}")


def main() -> int:
    argv = sys.argv[1:]
    target = "active" if "--resume" in argv else "paused"
    dry_run = "--dry-run" in argv
    env_file = ".env"
    if "--env-file" in argv:
        env_file = argv[argv.index("--env-file") + 1]

    tok = _token(env_file)
    hdrs = {"Authorization": f"Bearer {tok}"}
    failures = 0

    for sid, reason in SOURCES:
        url = f"{REGISTRY}/api/v1/registry/descriptors/source/{sid}"
        try:
            doc = json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers=hdrs), timeout=20))
            body = doc.get("body", doc)
            current = body["identity"]["state"]
            if current == target:
                print(f"{sid}: already {target}")
                continue
            if dry_run:
                print(f"{sid}: WOULD {current} -> {target}  ({reason})")
                continue
            body["identity"]["state"] = target
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(), method="PUT",
                headers={**hdrs, "Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=20)
            print(f"{sid}: {current} -> {target} ({resp.status})  # {reason}")
        except urllib.error.HTTPError as exc:
            failures += 1
            print(f"{sid}: HTTP {exc.code} {exc.read()[:200]!r}")
        except Exception as exc:  # noqa: BLE001 - operator-facing script
            failures += 1
            print(f"{sid}: {type(exc).__name__}: {exc}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
