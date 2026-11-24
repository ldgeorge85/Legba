# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`scripts/disk_floor_check.sh` — pages by BAND, not every tick (N-M, 11-13).

The real script runs under a harness: `df`, `curl` and `docker` are stubs on
PATH, the state file / log live in tmp, and the clock is `DISK_NOW`. The real
notifier is never called — the curl stub only records its arguments.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "disk_floor_check.sh"

_DF = """#!/usr/bin/env bash
echo "Use%  Avail"
echo " ${STUB_DF_PCT}%   ${STUB_DF_AVAIL:-20G}"
"""
_CURL = """#!/usr/bin/env bash
printf '%s\\x1f' "$@" >> "$STUB_CURL_LOG"; echo >> "$STUB_CURL_LOG"
"""
_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_CURL_LOG.docker"
"""

H = 3600


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, body in (("df", _DF), ("curl", _CURL), ("docker", _DOCKER)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.state = tmp / "legba-disk.state"
        self.log = tmp / "legba-disk.log"
        self.curl_log = tmp / "curl.log"
        self.t = 1_700_000_000

    def run(self, pct: int, *, advance: int = 1800) -> list[dict[str, str]]:
        """One cron tick at ``pct`` %, ``advance`` seconds after the last.
        Returns the pages this tick sent (title / priority / body)."""
        self.t += advance
        before = self.pages()
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "LEGBA_REPO_DIR": str(self.tmp),
            "LEGBA_ENGINE_NTFY_URL": "http://stub.invalid/engine",
            "DISK_STATE_FILE": str(self.state),
            "DISK_LOG": str(self.log),
            "DISK_NOW": str(self.t),
            "DISK_PRUNE_ENABLED": "0",
            "STUB_DF_PCT": str(pct),
            "STUB_CURL_LOG": str(self.curl_log),
        }
        proc = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr == "", proc.stderr
        return self.pages()[len(before):]

    def pages(self) -> list[dict[str, str]]:
        if not self.curl_log.is_file():
            return []
        out = []
        for line in self.curl_log.read_text().splitlines():
            args = line.split("\x1f")
            page: dict[str, str] = {}
            for i, a in enumerate(args):
                if a == "-H":
                    k, _, v = args[i + 1].partition(": ")
                    page[k.lower()] = v
                if a == "-d":
                    page["body"] = args[i + 1]
            out.append(page)
        return out

    def state_band(self) -> str:
        return self.state.read_text().split()[0]


@pytest.fixture()
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_first_run_ok_is_silent(h: Harness) -> None:
    assert h.run(70) == []
    assert h.state_band() == "0"
    assert "band=ok" in h.log.read_text()


def test_first_run_inside_a_band_pages_once(h: Harness) -> None:
    (p,) = h.run(87)
    assert p["priority"] == "3" and "warning" in p["title"]
    assert "87%" in p["body"] and "20G free" in p["body"]
    assert h.run(87) == []


def test_band_entry_then_stay_no_repeat(h: Harness) -> None:
    assert h.run(80) == []
    (p,) = h.run(86)
    assert "warning" in p["title"]
    for _ in range(48):  # a whole day at 30-min ticks
        assert h.run(88) == []


def test_escalation_to_higher_band(h: Harness) -> None:
    h.run(80)
    h.run(86)
    (p,) = h.run(91, advance=5 * H)
    assert p["priority"] == "4" and "urgent" in p["title"]
    assert "Was warning for 5h" in p["body"]
    (p,) = h.run(94)
    assert p["priority"] == "5" and "critical" in p["title"]
    assert "95%" in p["body"]  # names the flood stage


def test_jump_straight_to_critical_pages_once(h: Harness) -> None:
    h.run(80)
    pages = h.run(95)
    assert len(pages) == 1 and pages[0]["priority"] == "5"


def test_critical_renotifies_every_six_hours_only(h: Harness) -> None:
    h.run(80)
    assert len(h.run(93)) == 1
    sent = [len(h.run(93)) for _ in range(24)]  # 12 h of ticks
    # re-notify at +6 h and +12 h, nothing between
    assert sum(sent) == 2
    assert sent.index(1) == 11  # tick 12 = 6 h after entry
    page = h.pages()[-1]
    assert "still critical" in page["title"] and "for 12h0m" in page["body"]


def test_lower_bands_never_renotify(h: Harness) -> None:
    h.run(80)
    h.run(91)
    assert all(h.run(91, advance=7 * H) == [] for _ in range(5))


def test_recovery_with_hysteresis(h: Harness) -> None:
    h.run(80)
    h.run(86)
    assert h.run(84) == []        # below 85 but not below 83: still warning
    assert h.state_band() == "1"
    (p,) = h.run(82)               # cleared 85 - 2
    assert p["priority"] == "2" and "back below warning" in p["title"]
    assert "Now: ok" in p["body"]
    assert h.run(82) == []


def test_flapping_around_a_threshold_pages_once(h: Harness) -> None:
    h.run(80)
    pages = []
    for pct in (90, 89, 90, 89, 88, 90, 89):
        pages += h.run(pct)
    assert len(pages) == 1 and "urgent" in pages[0]["title"]


def test_recovery_from_critical_steps_to_the_band_it_lands_in(h: Harness) -> None:
    h.run(80)
    h.run(94)
    assert h.run(92) == []          # 93 - 2 = 91: not cleared
    (p,) = h.run(90)
    assert "back below critical" in p["title"] and "Now: urgent" in p["body"]
    assert h.state_band() == "2"


@pytest.mark.parametrize("junk", ["", "garbage\n", "9 1 2\n", "2 x y\n", "\x00\x01"])
def test_corrupt_state_is_first_run(h: Harness, junk: str) -> None:
    h.state.write_text(junk)
    (p,) = h.run(91)
    assert "urgent" in p["title"]
    assert "Was" not in p["body"]
    assert h.state_band() == "2"
    assert "treated as first run" in h.log.read_text() or junk == ""


def test_missing_state_dir_does_not_crash(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.state = tmp_path / "nope" / "state"
    assert len(h.run(91)) == 1
    assert "state write failed" in h.log.read_text()
