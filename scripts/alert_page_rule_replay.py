#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay a window of past alerts through THE page rule, offline.

Input: the JSON lines ``scripts/alert_page_rule_replay.sql`` exports
(read-only) — one line per would-be raise from the three earlier alerting systems, with
what actually reached the push service. Optionally the host logs, for the
pushes no ledger recorded (``--disk-log``, ``--nightly-log``).

Each record becomes the raise its producer now makes (kind, severity on the
ladder, subject, the producer's dedupe key, open or close, any producer hold),
in time order, through the SAME decision code the runtime runs
(:func:`legba.data.alerts.raise_alert.decide`) with an in-memory copy of the
episode table (quiet windows included). The report compares the pages the new
path would have sent with the pages that were sent, and explains every
difference by its reason.

No database, no network::

    PYTHONPATH=src python3 scripts/alert_page_rule_replay.py raises.jsonl \\
        --disk-log /var/log/legba-disk.log --nightly-log /var/log/legba_nightly_suite.log \\
        --watchdog-log /var/log/legba-watchdog.log
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from legba.data.alerts.kinds import KINDS
from legba.data.alerts.ladder import SEVERITY_ORDER, map_alert_severity
from legba.data.alerts.page_rule import PageRule, load_rule
from legba.data.alerts.raise_alert import decide


@dataclass
class Raise:
    ts: datetime
    kind: str
    severity: str
    subject: str
    key: str
    close: bool = False
    hold: Optional[str] = None
    actual: set[str] = field(default_factory=set)
    src: str = ""
    ref: str = ""


@dataclass
class Episode:
    paged_severity: Optional[str] = None
    last_paged_at: Optional[datetime] = None
    last_raised_at: Optional[datetime] = None


def _ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_export(path: str) -> list[Raise]:
    raises: list[Raise] = []
    batches: dict[str, list[dict]] = defaultdict(list)
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        actual = {a for a in (r.get("actual") or "").split(",") if a}
        if r["src"] == "trigger_scan":
            if r.get("batch") and r["kind"] == "band_crossing" and not r.get("hold"):
                batches[r["batch"]].append(r)
                continue
            raises.append(Raise(
                ts=_ts(r["ts"]), kind=r["kind"], severity=r["severity"] or "medium",
                subject=r["subject"] or "", key=f"{r['kind']}:alert:{r['ref']}",
                hold=r.get("hold"), actual=actual, src="trigger_scan", ref=r["ref"]))
        elif r["src"] == "escalate":
            raises.append(Raise(
                ts=_ts(r["ts"]), kind="escalations", severity=r["severity"] or "",
                subject=r["subject"] or "",
                key=f"escalations:finding:{r['ref']}" if r.get("ref") else f"escalations:{r['subject']}",
                hold="level-only" if "skipped_level_only" in actual else None,
                actual=actual, src="escalate", ref=r.get("ref") or ""))
        elif r["src"] == "watchdog":
            state = r.get("state") or "entered"
            kind = r["kind"]
            raises.append(Raise(
                ts=_ts(r["ts"]), kind=kind, severity=r["severity"] or "high",
                subject=r["subject"] or "pipeline", key=f"{kind}:{r['subject'] or 'pipeline'}",
                close=state == "recovered", actual=actual, src="watchdog", ref=r["ref"]))
        else:
            raises.append(Raise(
                ts=_ts(r["ts"]), kind=r["kind"], severity=r["severity"] or "high",
                subject=r["subject"] or "", key=f"{r['kind']}:{r['subject']}",
                actual=actual, src=r["src"], ref=r["ref"]))
    for key, members in batches.items():
        members.sort(key=lambda m: -SEVERITY_ORDER.get(map_alert_severity(m["severity"]), 0))
        actual = set().union(*({a for a in (m.get("actual") or "").split(",") if a} for m in members))
        raises.append(Raise(
            ts=min(_ts(m["ts"]) for m in members), kind="band_crossing",
            severity=members[0]["severity"] or "medium", subject="",
            key=f"band_crossing:alert:{members[0]['ref']}", actual=actual,
            src="trigger_scan(batch)", ref=key))
    return raises


_DISK = re.compile(r"^(\S+) disk=(\d+)% .*band=(\w+) action=(enter|recover|renotify)")
_BAND_SEV = {"warning": "medium", "urgent": "high", "critical": "critical"}


def load_disk_log(path: str, since: datetime) -> list[Raise]:
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        m = _DISK.match(line)
        if not m or _ts(m.group(1)) < since:
            continue
        ts, band, action = _ts(m.group(1)), m.group(3), m.group(4)
        close = action == "recover" and band == "ok"
        sev = "low" if close else _BAND_SEV.get(band, "medium")
        out.append(Raise(ts=ts, kind="host_disk", severity=sev, subject="/", key="host_disk:/",
                         close=close, actual={"delivered"}, src="disk_floor_check.sh",
                         ref=f"{action} {band}"))
    return out


_VERDICT = re.compile(r"^NIGHTLY_VERDICT (\S+) .*verdict=(\w+).* run=(\S+).* advisory=(\d+)")


def load_nightly_log(path: str, since: datetime) -> list[Raise]:
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        m = _VERDICT.match(line)
        if not m or _ts(m.group(1)) < since:
            continue
        ts, verdict, run, advisory = _ts(m.group(1)), m.group(2), m.group(3), int(m.group(4))
        pages = [("high", "nightly suite FAILED")] if verdict == "FAIL" else []
        if verdict != "FAIL" and advisory:
            pages.append(("medium", "nightly advisory"))
        if verdict == "PASS":
            pages.append(("low", "nightly suite PASS"))
        for sev, title in pages:
            out.append(Raise(ts=ts, kind="nightly_suite", severity=sev, subject="nightly",
                             key=f"nightly_suite:{run}:{title}", actual={"delivered"},
                             src="host_nightly_suite.sh", ref=run))
    return out


_FIRE = re.compile(r"^(\S+) \[(llm-heartbeat|search-canary)\] FIRE (.*)$")


def load_watchdog_log(path: str, since: datetime) -> list[Raise]:
    """The heartbeat's and the canary's FIRE lines (each one was a push)."""
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        m = _FIRE.match(line.strip())
        if not m or _ts(m.group(1)) < since:
            continue
        ts, who, what = _ts(m.group(1)), m.group(2), m.group(3).lower()
        if who == "search-canary":
            kind, subject, sev = "host_search_canary", "search_plane", "critical"
        elif "long-context" in what:
            kind, subject, sev = "host_llm_heartbeat", "llm_long_context_degraded", "high"
        elif "silent" in what:
            kind, subject, sev = "host_llm_heartbeat", "llm_plane_silent", "critical"
        else:
            kind, subject, sev = "host_llm_heartbeat", "llm_completion_dead", "critical"
        out.append(Raise(ts=ts, kind=kind, severity=sev, subject=subject,
                         key=f"{kind}:{subject}", actual={"delivered"},
                         src=f"{who} log", ref=what[:36]))
    return out


def replay(raises: list[Raise], rule: PageRule) -> list[tuple[Raise, str, str, Optional[str]]]:
    episodes: dict[str, Episode] = {}
    out = []
    for r in sorted(raises, key=lambda x: x.ts):
        spec = KINDS[r.kind]
        sev = map_alert_severity(r.severity, context="replay")
        ep = episodes.get(r.key)
        if ep is not None and spec.quiet_close_hours is not None and ep.last_raised_at is not None \
                and r.ts - ep.last_raised_at > timedelta(hours=spec.quiet_close_hours):
            episodes.pop(r.key)
            ep = None
        if r.close:
            action = "closed" if ep is not None else "close_without_episode"
        else:
            action = "updated" if ep is not None else "opened"
        decision, reason, entry = decide(
            rule=rule, kind=r.kind, severity=sev, subject_id=r.subject, action=action,
            hold=r.hold, prior_paged_severity=ep.paged_severity if ep else None,
            prior_last_paged_at=ep.last_paged_at if ep else None, now=r.ts)
        if r.close:
            episodes.pop(r.key, None)
        else:
            ep = ep or episodes.setdefault(r.key, Episode())
            ep.last_raised_at = r.ts
            if decision == "paged":
                if ep.paged_severity is None or SEVERITY_ORDER[sev] > SEVERITY_ORDER[ep.paged_severity]:
                    ep.paged_severity = sev
                ep.last_paged_at = r.ts
        out.append((r, decision, reason, entry))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export")
    ap.add_argument("--disk-log")
    ap.add_argument("--nightly-log")
    ap.add_argument("--watchdog-log", help="the heartbeat/canary log (FIRE lines)")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--examples", type=int, default=6)
    args = ap.parse_args(argv)
    raises = load_export(args.export)
    end = max(r.ts for r in raises)
    since = end - timedelta(days=args.days)
    if args.disk_log:
        raises += load_disk_log(args.disk_log, since)
    if args.nightly_log:
        raises += load_nightly_log(args.nightly_log, since)
    if args.watchdog_log:
        raises += load_watchdog_log(args.watchdog_log, since)
    rule = load_rule()
    results = replay(raises, rule)

    def sent(r: Raise) -> str:
        if "delivered" in r.actual:
            return "pushed"
        if "skipped_cooldown" in r.actual:
            return "coalesced"
        return "not pushed"

    table: dict[str, Counter] = defaultdict(Counter)
    diffs: dict[str, list] = defaultdict(list)
    for r, decision, reason, entry in results:
        old = sent(r)
        new = "page" if decision == "paged" else "no page"
        t = table[r.kind]
        t["raises"] += 1
        t[f"old {old}"] += 1
        t[f"new {decision}"] += 1
        old_paged = old in ("pushed", "coalesced")
        if old_paged and decision == "paged":
            t["same: paged"] += 1
        elif not old_paged and decision != "paged":
            t["same: not paged"] += 1
        else:
            label = f"{r.kind}: was {old}, now {new} ({decision}: {reason})"
            if old_paged and decision != "paged":
                label = f"{r.kind}: was {old}, now {decision} — {_why_fewer(decision, reason)}"
            elif decision == "paged":
                label = f"{r.kind}: was {old} ({','.join(sorted(r.actual)) or 'no push-service row'}), now paged — {reason}"
            diffs[label].append(r)

    print(f"rule {rule.version} ({rule.path}); window {since:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC; "
          f"{len(results)} raises")
    old_total = sum(1 for r, *_ in results if sent(r) in ("pushed", "coalesced"))
    old_pushed = sum(1 for r, *_ in results if sent(r) == "pushed")
    new_total = sum(1 for _, d, *_ in results if d == "paged")
    print(f"pages sent then: {old_total} ({old_pushed} pushed alone + {old_total - old_pushed} "
          f"coalesced into another push by the 15 s transport cooldown); pages the rule sends: {new_total}")
    print()
    print(f"{'kind':26s} {'raises':>7s} {'then':>6s} {'now':>6s} {'same+':>6s} {'same-':>6s}  decisions now")
    for kind in sorted(table):
        t = table[kind]
        then = t["old pushed"] + t["old coalesced"]
        dec = ", ".join(f"{k[4:]} {v}" for k, v in sorted(t.items()) if k.startswith("new "))
        print(f"{kind:26s} {t['raises']:7d} {then:6d} {t['new paged']:6d} "
              f"{t['same: paged']:6d} {t['same: not paged']:6d}  {dec}")
    print()
    print("DIFFERENCES (every one, grouped by reason):")
    for label, rows in sorted(diffs.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(rows):4d} × {label}")
        for r in rows[: args.examples]:
            print(f"         {r.ts:%m-%d %H:%M} {r.subject or '-'} [{r.src} {r.ref[:36]}]")
    return 0


def _why_fewer(decision: str, reason: str) -> str:
    if decision == "deduped":
        return f"deduped by the episode ({reason})"
    if decision == "held":
        return f"held by the producer ({reason})"
    return f"logged by the rule ({reason})"


if __name__ == "__main__":
    sys.exit(main())
