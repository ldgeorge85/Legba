#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FETCH_REVIEW (a)/(a′) acceptance probe — the before/after measurement.

Runs the SAME url list through the SAME fetch path twice, once with
``LEGBA_FETCH_IMPERSONATE`` off and once on, and prints the per-class table the
review asks for. It answers one question and no other: **does the browser
fingerprint change any outcome, and does it break any outcome that already
worked?**

    scripts/fetch_impersonation_probe.py                       # off then on
    scripts/fetch_impersonation_probe.py --mode off            # one mode only
    scripts/fetch_impersonation_probe.py --urls-file sample.txt --mode both

THE BAR (review §5), applied at the end and printed as a verdict:

    keep the flag ON only if >= 1/3 of challenge-class URLs become ``ok``,
    with ZERO regressions — no ``ok`` URL becomes non-``ok``, and no robots
    refusal becomes a fetch.

If the bar is missed the flag stays off, the measurement is published anyway,
and option (b) is NOT opened on a hunch.

DISCIPLINE THIS SCRIPT KEEPS
----------------------------
* **robots first, always.** Every URL's robots.txt is checked for
  ``legba-research/1.0`` before any page connect, with the SAME fail-closed
  ``robots.py`` the tool uses. ``reuters.com`` is expected to come back
  ``robots_refused`` in BOTH modes; that is a correct answer, not a blocker,
  and no mode in this script may be pointed at it.
* **one identity.** The identifying User-Agent is sent in both modes. The only
  thing that changes between them is the TLS/HTTP2 fingerprint.
* **a hard request budget.** Every robots.txt and every page GET is counted
  against ``--budget`` (default 60) and the run STOPS rather than exceeding it.
  The robots cache is shared across modes, so a host's robots.txt is fetched
  once for the whole run.
* **politeness.** ``--per-host-delay`` between requests to the same host, and
  ``--settle-seconds`` between the two modes — do not hammer hosts that are
  already refusing (the S2 no-retry-on-degraded discipline applies here too).
* **read-only.** The corpus sample is a SELECT against ``evidence_archive``
  failure rows; nothing is written anywhere, no credentials are sent, no
  cookies are kept, no paywall is circumvented.

WHERE THIS DIFFERS FROM THE PRODUCTION PATH, deliberately: the production
fetchers call ``raise_for_status()`` inside the streaming context and so never
see a 403's body. This probe READS the body at any status, because
classification is its whole job. That difference is exactly why
``challenge_from_status`` exists in the product code.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(_SRC))

import httpx  # noqa: E402

from legba.data.analysts.agency.robots import (  # noqa: E402
    ALLOWED as ROBOTS_ALLOWED,
)
from legba.data.analysts.agency.robots import (  # noqa: E402
    NO_RULES as ROBOTS_NO_RULES,
)
from legba.data.analysts.agency.robots import (  # noqa: E402
    ROBOTS_USER_AGENT,
    RobotsCache,
    robots_decision,
)
from legba.data.analysts.deterministic_handlers._challenge_detect import (  # noqa: E402
    challenge_from_status,
    detect_challenge_page,
)
from legba.data.research_evidence import extract_archived_text  # noqa: E402
from legba.data.sources._egress import (  # noqa: E402
    FETCH_IMPERSONATE_ENV,
    EgressBlockedError,
    fetch_client,
    guarded_async_client,
)

#: The four hosts FETCH_REVIEW §2 probed. Reuters is on the list precisely so
#: the robots refusal is re-proven every run — it is a NAMED negative test.
REVIEW_HOSTS: tuple[str, ...] = (
    "https://www.reuters.com/",
    "https://apnews.com/",
    "https://www.timesofisrael.com/",
    "https://www.haaretz.com/",
)

#: Heuristic, and labelled as one. A paywall is not something this lane fixes
#: (review §3(e)); it is classified only so it stops being counted as a block
#: the fetcher could have done something about.
_PAYWALL_MARKERS: tuple[str, ...] = (
    "subscribe to continue",
    "already a subscriber",
    "this article is for subscribers",
    "create a free account to keep reading",
    "sign in to read the full",
    "subscribers only",
)

CLASS_OK = "ok"
CLASS_CHALLENGE = "challenge"
CLASS_ROBOTS = "robots_refused"
CLASS_PAYWALL = "paywall"
CLASS_THIN = "thin"
CLASS_ERROR = "error"

#: ``thin`` is a class of its own and NOT folded into ``error`` on purpose:
#: "we got bytes and found no article" and "the fetch failed" are the two
#: things this whole lane exists to stop conflating.
CLASSES = (CLASS_OK, CLASS_CHALLENGE, CLASS_ROBOTS, CLASS_PAYWALL,
           CLASS_THIN, CLASS_ERROR)

_DEFAULT_DSN = os.environ.get("LEGBA_PROBE_DSN") or os.environ.get(
    "DATABASE_URL", "postgresql://legba:legba@127.0.0.1:5432/legba",
)

_CORPUS_SQL = """
    WITH r AS (
        SELECT split_part(split_part(fetched_url, '/', 3), ':', 1) AS host,
               fetched_url,
               row_number() OVER (
                   PARTITION BY split_part(split_part(fetched_url, '/', 3), ':', 1)
                   ORDER BY updated_at DESC
               ) AS rn
          FROM evidence_archive
         WHERE status = 'failed'
           AND fetched_url IS NOT NULL
           AND length(fetched_url) < 220
    ),
    hosts AS (
        SELECT host FROM r GROUP BY host HAVING count(*) >= $1
         ORDER BY count(*) DESC LIMIT $2
    )
    SELECT r.fetched_url
      FROM r JOIN hosts USING (host)
     WHERE r.rn <= $1
     ORDER BY r.host, r.rn
"""


@dataclass
class Probe:
    url: str
    mode: str
    status: int | None = None
    klass: str = CLASS_ERROR
    body_bytes: int = 0
    chars: int = 0
    detail: str = ""


@dataclass
class Budget:
    """A hard ceiling on live requests. Refuses rather than overshoots."""

    limit: int
    used: int = 0
    refused: list[str] = field(default_factory=list)

    def take(self, what: str) -> bool:
        if self.used >= self.limit:
            self.refused.append(what)
            return False
        self.used += 1
        return True


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class _Politeness:
    def __init__(self, delay: float) -> None:
        self._delay = max(0.0, delay)
        self._last: dict[str, float] = {}

    async def wait(self, url: str) -> None:
        host = _host(url)
        if not host or self._delay <= 0:
            return
        last = self._last.get(host)
        if last is not None:
            remaining = self._delay - (time.monotonic() - last)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last[host] = time.monotonic()


def _classify(status: int | None, body: bytes, content_type: str | None,
              encoding: str | None) -> tuple[str, int, str]:
    """``(class, extracted_chars, detail)`` for one fetched response."""
    tell = detect_challenge_page(body, content_type, encoding)
    if tell is not None:
        return CLASS_CHALLENGE, 0, tell
    status_tell = challenge_from_status(status)
    if status_tell is not None:
        return CLASS_CHALLENGE, 0, status_tell
    if status == 402:
        return CLASS_PAYWALL, 0, "http_402"
    if status is None or status >= 400:
        return CLASS_ERROR, 0, f"http_{status}"
    text = extract_archived_text(body, content_type, encoding, max_chars=200_000)
    if not text:
        return CLASS_THIN, 0, "no_main_text"
    lowered = text.lower()
    for marker in _PAYWALL_MARKERS:
        if marker in lowered and len(text) < 2_000:
            return CLASS_PAYWALL, len(text), marker
    return CLASS_OK, len(text), ""


async def _probe_one(client, url: str, *, max_bytes: int) -> tuple[int, bytes, str | None, str | None]:
    """GET ``url`` and return ``(status, body, content_type, encoding)``.

    Reads the body at ANY status (see the module docstring); the production
    fetchers do not, and must not.
    """
    async with client.stream("GET", url) as resp:
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                break
            chunks.append(chunk)
        return (
            int(resp.status_code),
            b"".join(chunks),
            resp.headers.get("content-type"),
            getattr(resp, "charset_encoding", None),
        )


async def _run_mode(urls: list[str], *, mode: str, robots_cache: RobotsCache,
                    budget: Budget, timeout: float, per_host_delay: float,
                    max_bytes: int) -> list[Probe]:
    os.environ[FETCH_IMPERSONATE_ENV] = mode
    polite = _Politeness(per_host_delay)
    out: list[Probe] = []
    for url in urls:
        probe = Probe(url=url, mode=mode)
        # --- ROBOTS FIRST. Cached across modes, so it is fetched once. ---
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        cached = robots_cache.get(origin) is not None
        if not cached and not budget.take(f"robots:{origin}"):
            probe.klass, probe.detail = CLASS_ERROR, "budget_exhausted"
            out.append(probe)
            continue
        try:
            decision = await robots_decision(
                url, cache=robots_cache, user_agent=ROBOTS_USER_AGENT,
            )
        except Exception as exc:                       # noqa: BLE001 - reported
            probe.klass, probe.detail = CLASS_ERROR, f"robots:{exc!r}"[:80]
            out.append(probe)
            continue
        if decision not in (ROBOTS_ALLOWED, ROBOTS_NO_RULES):
            probe.klass, probe.detail = CLASS_ROBOTS, str(decision)
            out.append(probe)
            continue
        # --- the page ---
        if not budget.take(f"page:{url}"):
            probe.klass, probe.detail = CLASS_ERROR, "budget_exhausted"
            out.append(probe)
            continue
        await polite.wait(url)
        try:
            async with fetch_client(
                guarded=guarded_async_client,
                follow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": ROBOTS_USER_AGENT},
            ) as client:
                status, body, ctype, encoding = await _probe_one(
                    client, url, max_bytes=max_bytes,
                )
        except EgressBlockedError as exc:
            probe.klass, probe.detail = CLASS_ERROR, f"egress_blocked:{exc}"[:80]
            out.append(probe)
            continue
        except (httpx.HTTPError, Exception) as exc:    # noqa: BLE001 - reported
            probe.klass, probe.detail = CLASS_ERROR, f"{type(exc).__name__}"[:80]
            out.append(probe)
            continue
        probe.status = status
        probe.body_bytes = len(body)
        probe.klass, probe.chars, probe.detail = _classify(
            status, body, ctype, encoding,
        )
        out.append(probe)
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _short(url: str, width: int = 58) -> str:
    return url if len(url) <= width else url[: width - 1] + "…"


def _print_mode_table(probes: list[Probe]) -> None:
    print(f"\n--- mode={probes[0].mode if probes else '?'} "
          f"({len(probes)} urls) ---")
    print(f"{'url':<58} {'status':>6} {'class':<14} {'bytes':>9} {'chars':>7}  detail")
    for p in probes:
        print(
            f"{_short(p.url):<58} {str(p.status or '-'):>6} {p.klass:<14} "
            f"{p.body_bytes:>9} {p.chars:>7}  {p.detail[:34]}"
        )


def _print_false_absence(probes: list[Probe]) -> None:
    """(a′) — how many of these pages USED to read as "the web had nothing".

    This is the half of the lane that measures as a real defect fixed, and it
    is measured at the SHIPPED default (flag off), because that is what every
    deployment runs. Two shapes, both previously invisible as blocks:

      * a 200 carrying an interstitial — the archiver stored bytes and found
        no article, the ``web_evidence`` tool reported
        ``no_main_text_extracted`` at teaser depth. Indistinguishable, to a
        planner, from a page with nothing on it.
      * a 401/403/429 — a bare ``fetch_failed`` on both paths, which reads as
        a transport problem rather than a publisher's refusal.
    """
    blocked = [p for p in probes if p.klass == CLASS_CHALLENGE]
    # Split by WHICH TIER the production path would use, which is decided by
    # the HTTP status and not by what this probe managed to read: the product
    # fetchers call raise_for_status() inside the stream, so at >=400 they
    # classify on the status alone, and the body tell only reaches them when
    # the interstitial arrives with a 2xx.
    refused = [p for p in blocked if (p.status or 0) >= 400]
    served_200 = [p for p in blocked if (p.status or 0) < 400]
    with_body_tell = [p for p in blocked if not p.detail.startswith("http_")]
    print("\n=== FALSE ABSENCE RECOVERED (a′, at the shipped default) ===")
    print(f"  urls probed                            : {len(probes)}")
    print(f"  now classified as BLOCKED              : {len(blocked)}")
    print(f"    ├─ edge refused (401/403/429)        : {len(refused)}"
          "   was: a bare fetch_failed")
    print(f"    └─ 2xx carrying an interstitial      : {len(served_200)}"
          "   was: thin extraction ⇒ read as an empty web")
    print(f"  of those, a body tell is also readable : {len(with_body_tell)}"
          "   (what the ARCHIVER sees in the bytes it stored)")
    thin = [p for p in probes if p.klass == CLASS_THIN]
    print(f"  still thin (bytes, no article, no tell): {len(thin)}"
          "   — honestly unknown, never claimed as a block")
    if with_body_tell:
        print("  interstitial tells seen:")
        for p in with_body_tell:
            print(f"    {_short(p.url, 52):<52} {str(p.status):>4}  {p.detail[:34]}")


def _counts(probes: list[Probe]) -> dict[str, int]:
    counts = dict.fromkeys(CLASSES, 0)
    for p in probes:
        counts[p.klass] = counts.get(p.klass, 0) + 1
    return counts


def _print_delta(off: list[Probe], on: list[Probe]) -> int:
    """Print the before/after table + the verdict. Returns the exit code."""
    by_url_off = {p.url: p for p in off}
    by_url_on = {p.url: p for p in on}
    shared = [u for u in by_url_off if u in by_url_on]

    print("\n=== BEFORE / AFTER (per class) ===")
    co, cn = _counts(off), _counts(on)
    print(f"{'class':<16} {'off':>6} {'on':>6} {'delta':>7}")
    for klass in CLASSES:
        print(f"{klass:<16} {co[klass]:>6} {cn[klass]:>6} "
              f"{cn[klass] - co[klass]:>+7}")

    print("\n=== PER-URL MOVES (only where the class changed) ===")
    moves = [
        (u, by_url_off[u].klass, by_url_on[u].klass)
        for u in shared
        if by_url_off[u].klass != by_url_on[u].klass
    ]
    if not moves:
        print("  (none — every URL landed in the same class in both modes)")
    for url, a, b in moves:
        print(f"  {_short(url):<58} {a} -> {b}")

    challenged = [u for u in shared if by_url_off[u].klass == CLASS_CHALLENGE]
    rescued = [u for u in challenged if by_url_on[u].klass == CLASS_OK]
    regressed_ok = [
        u for u in shared
        if by_url_off[u].klass == CLASS_OK and by_url_on[u].klass != CLASS_OK
    ]
    robots_broken = [
        u for u in shared
        if by_url_off[u].klass == CLASS_ROBOTS and by_url_on[u].klass != CLASS_ROBOTS
    ]

    print("\n=== VERDICT (review §5 bar) ===")
    need = (len(challenged) + 2) // 3
    print(f"  challenge-class URLs (flag off) : {len(challenged)}")
    print(f"  became ok with the flag on      : {len(rescued)} (bar: >= {need})")
    print(f"  ok URLs that regressed          : {len(regressed_ok)}")
    print(f"  robots refusals that became a fetch: {len(robots_broken)}")
    passed = (
        challenged
        and len(rescued) >= need
        and not regressed_ok
        and not robots_broken
    )
    if robots_broken:
        print("  ROBOTS REGRESSION — this is not a tuning question. Stop.")
    print(f"  ==> {'KEEP THE FLAG ON' if passed else 'FLAG STAYS OFF'}")
    if not passed:
        print("      (publish the measurement anyway; do NOT open option (b) "
              "on a hunch — review §5)")
    return 0


async def _corpus_urls(dsn: str, *, per_host: int, hosts: int) -> list[str]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(_CORPUS_SQL, per_host, hosts)
    finally:
        await conn.close()
    return [r["fetched_url"] for r in rows]


async def _main_async(args: argparse.Namespace) -> int:
    if args.urls_file:
        urls = [
            line.strip()
            for line in Path(args.urls_file).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    else:
        corpus = await _corpus_urls(
            args.dsn, per_host=args.per_host, hosts=args.hosts,
        )
        urls = list(REVIEW_HOSTS) + corpus
    if not urls:
        print("no urls to probe", file=sys.stderr)
        return 2

    distinct_hosts = {_host(u) for u in urls}
    print(f"probing {len(urls)} urls across {len(distinct_hosts)} hosts; "
          f"budget {args.budget} requests; UA {ROBOTS_USER_AGENT}")

    budget = Budget(limit=args.budget)
    robots_cache = RobotsCache()
    modes = ["off", "on"] if args.mode == "both" else [args.mode]
    results: dict[str, list[Probe]] = {}
    for i, mode in enumerate(modes):
        if i:
            print(f"\n… settling {args.settle_seconds}s before mode={mode} "
                  "(do not hammer hosts that are already refusing)")
            await asyncio.sleep(args.settle_seconds)
        results[mode] = await _run_mode(
            urls, mode=mode, robots_cache=robots_cache, budget=budget,
            timeout=args.timeout, per_host_delay=args.per_host_delay,
            max_bytes=args.max_bytes,
        )
        _print_mode_table(results[mode])
        if mode == "off":
            _print_false_absence(results[mode])

    print(f"\nrequests used: {budget.used}/{budget.limit}")
    if budget.refused:
        print(f"REFUSED for budget ({len(budget.refused)}): "
              f"{budget.refused[:3]}…")
    if "off" in results and "on" in results:
        return _print_delta(results["off"], results["on"])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["off", "on", "fallback", "both"],
                    default="both")
    ap.add_argument("--dsn", default=_DEFAULT_DSN,
                    help="read-only DSN for the corpus sample (never printed)")
    ap.add_argument("--urls-file", default=None,
                    help="one url per line; skips the DB entirely")
    ap.add_argument("--hosts", type=int, default=10,
                    help="distinct corpus hosts to sample (default 10)")
    ap.add_argument("--per-host", type=int, default=2,
                    help="corpus urls per host (default 2 => 20 urls)")
    ap.add_argument("--budget", type=int, default=60,
                    help="HARD ceiling on live requests, robots included")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--per-host-delay", type=float, default=2.0)
    ap.add_argument("--settle-seconds", type=float, default=60.0,
                    help="pause between the two modes (review §5)")
    ap.add_argument("--max-bytes", type=int, default=5_000_000)
    args = ap.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
