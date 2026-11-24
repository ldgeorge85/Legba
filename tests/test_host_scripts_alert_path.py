# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-1 — the host scripts raise through the single alert path, and fall back
to their direct push only when the path is unavailable.

Real scripts under stubs: ``docker`` records the ``legba.data.alerts.cli``
call it would have made (and succeeds or fails on demand), ``curl`` records
the direct push. No real container, database or push service is reached.

* off by default (``LEGBA_ALERT_RAISE`` unset): the direct push, exactly as
  before — a hand run or a test never touches the live runtime;
* on, and the runtime answers: one raise with the kind, the ladder severity for
  the old priority, the subject, and ``--close`` for a recovery; NO direct push;
* on, and the runtime does not answer: the direct push (losing the database
  never loses the disk page).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_disk_floor_check import Harness

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "scripts" / "lib" / "legba_alert.sh"


def _docker_stub(h: Harness, exit_code: int) -> Path:
    log = h.tmp / "docker.log"
    stub = h.bin / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"cat >/dev/null; printf '%s\\n' \"$*\" >> {log}\nexit {exit_code}\n"
    )
    stub.chmod(0o755)
    return log


@pytest.fixture()
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_off_by_default_the_direct_push_is_unchanged(h: Harness, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("LEGBA_ALERT_RAISE", raising=False)
    log = _docker_stub(h, 0)
    (page,) = h.run(87)
    assert page["priority"] == "3"
    assert not log.exists()


def test_on_the_disk_band_is_raised_not_pushed(h: Harness, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("LEGBA_ALERT_RAISE", "1")
    log = _docker_stub(h, 0)
    assert h.run(87) == []           # entered warning: raised, no direct push
    assert h.run(91) == []           # entered urgent
    assert h.run(80) == []           # left both bands: a recovery
    calls = log.read_text().splitlines()
    assert len(calls) == 3
    assert "python -m legba.data.alerts.cli raise --kind host_disk --severity medium --subject /" in calls[0]
    assert "--severity high" in calls[1]
    assert "--severity low" in calls[2] and calls[2].endswith("--close")
    assert all("--raised-by disk_floor_check.sh" in c for c in calls)


def test_on_but_unreachable_falls_back_to_the_direct_push(h: Harness, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("LEGBA_ALERT_RAISE", "1")
    log = _docker_stub(h, 3)   # the CLI's "no database" exit
    (page,) = h.run(94)
    assert page["priority"] == "5" and "critical" in page["title"]
    assert "--severity critical" in log.read_text()


def test_the_priority_ladder_and_slug_helpers() -> None:
    out = subprocess.run(
        ["bash", "-c", f". {LIB}; for p in 5 4 3 2 1; do legba_severity_for_priority $p; done; "
         "legba_slug 'Legba: LLM completion DEAD'"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.stdout.split() == ["critical", "high", "medium", "low", "info", "llm_completion_dead"]
