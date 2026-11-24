# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`scripts/host_nightly_suite.sh` — the ordered/shuffled phase classifier.

THE DEFECT THIS PINS. pytest exits 1 whenever ANY test failed, allowlisted or
not. The phase classifier read that bare `rc != 0` as "pytest never ran" and
reported

    ordered: run did not complete (rc=1, no test failures parsed)
    VERDICT FAIL phases=[ordered:infra shuffled]

for a run that had completed and whose nineteen failures had all been parsed
and matched against the allowlist. Both live summaries under
/var/log/legba/nightly/ carry it: 20260804 shows `ordered:infra` next to an
`ordered rc=1 :: 11 failed, 10382 passed` line it had printed itself moments
earlier.

Two things were wrong with that, and the second is worse than the first. It
sent the operator hunting a broken rig on a night when the rig was fine. And it
spent the word "infra" on the ordinary case — so on the night pytest genuinely
cannot start, the one message that says so is the message that cried wolf every
previous morning.

These tests drive the REAL script end to end, with a stub runner standing in
for `run_tests_in_container.sh`, so they traverse the actual classifier rather
than a re-implementation of it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


SUITE = Path(__file__).resolve().parents[2] / "scripts" / "host_nightly_suite.sh"

# Two known-failure node ids. They used to point into the live KNOWN_FAILURES
# array (and before that into KNOWN_SHARED_STATE), which meant every
# retirement of a live entry had to rewrite this test's premise. Both arrays
# are now empty (KNOWN_SHARED_STATE retired 2026-08-25, the last eight
# KNOWN_FAILURES entries 2026-11-06 once the isolated test namespace removed
# their cause), so the tests below run a COPY of the real script whose only
# difference is these two synthetic entries spliced into KNOWN_FAILURES
# (``_suite_with_known``). The classifier, the parsing and the verdict logic
# are the script's own, byte for byte.
KNOWN = "tests/data_pkg/test_synthetic_known_one.py::test_known_rig_failure"
KNOWN2 = "tests/data_pkg/test_synthetic_known_two.py::test_known_rig_failure"
UNKNOWN = "tests/data_pkg/test_totally_new_regression.py::test_something_real_broke"


def _stub_repo(tmp_path: Path, *, stdout: str, rc: int) -> Path:
    """A fake REPO_ROOT whose `scripts/run_tests_in_container.sh` prints
    `stdout` and exits `rc` — i.e. a canned pytest run."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    runner = root / "scripts" / "run_tests_in_container.sh"
    runner.write_text(
        "#!/usr/bin/env bash\n"
        f"cat <<'STUBEOF'\n{stdout}\nSTUBEOF\n"
        f"exit {rc}\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    return root


def _suite_with_known(tmp_path: Path) -> Path:
    """A copy of the real suite script with KNOWN and KNOWN2 spliced in as
    the first entries of its KNOWN_FAILURES array (escaped as the script's
    own entries are: node-id regexes with literal dots)."""
    src = SUITE.read_text(encoding="utf-8")
    marker = "KNOWN_FAILURES=(\n"
    assert src.count(marker) == 1, "KNOWN_FAILURES array declaration moved"
    entries = "".join(
        "  '" + nodeid.replace(".", "\\.") + "'\n" for nodeid in (KNOWN, KNOWN2)
    )
    copy = tmp_path / "host_nightly_suite.sh"
    copy.write_text(src.replace(marker, marker + entries), encoding="utf-8")
    return copy


def _run_nightly(tmp_path: Path, repo_root: Path, phases: str = "ordered") -> str:
    """Run the suite script (a copy carrying the two synthetic allowlist
    entries) against the stub repo; return its summary text."""
    log_dir = tmp_path / "logs"
    env = {
        **os.environ,
        "LEGBA_REPO_ROOT": str(repo_root),
        "LEGBA_NIGHTLY_LOG_DIR": str(log_dir),
        "LEGBA_NIGHTLY_SELF_LOG": str(tmp_path / "self.log"),
        "LEGBA_NIGHTLY_LOCK_FILE": str(tmp_path / "nightly.lock"),
        # Unroutable: `page` swallows the curl failure and warns. No alert is
        # sent from a test run.
        "NTFY_URL": "http://127.0.0.1:9/none",
        # X1-A4: the nightly posts to its own topic through this variable.
        "LEGBA_NIGHTLY_NTFY_URL": "http://127.0.0.1:9/none",
        "LEGBA_TEST_LOCK_FILE": str(tmp_path / "tests.lock"),
        "PHASES": phases,
        "LEGBA_NIGHTLY_PHASE_TIMEOUT": "60",
    }
    proc = subprocess.run(
        ["bash", str(_suite_with_known(tmp_path))], env=env,
        capture_output=True, text=True, timeout=180,
    )
    # The script tees its summary to stdout as well as to the run dir.
    return proc.stdout


@pytest.fixture(autouse=True)
def _require_bash_tools():
    for tool in ("bash", "flock"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not available")
    if not SUITE.is_file():
        pytest.skip(f"missing {SUITE}")


def test_rc1_with_only_known_failures_is_not_an_infra_verdict():
    """THE REGRESSION. Every failure allowlisted, rc=1 — this is the designed
    steady state of the rig and must pass, not page as a broken run."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        log = (
            f"FAILED {KNOWN} - ModuleNotFoundError: No module named 'dspy'\n"
            f"FAILED {KNOWN2} - assert 4 == 3\n"
            "2 failed, 10380 passed, 70 skipped in 984.99s (0:16:24)\n"
        )
        out = _run_nightly(tmp_path, _stub_repo(tmp_path, stdout=log, rc=1))

    assert "run did not complete" not in out, (
        "rc=1 with every failure parsed and allowlisted is NOT an infra failure"
    )
    assert "ordered:infra" not in out
    assert "VERDICT PASS" in out, out
    assert "all 2 reported failure(s) known" in out


def test_known_shared_state_is_currently_empty_and_is_known_does_not_choke():
    """THE EMPTY-ARRAY EDGE CASE the 2026-08-25 retirement introduced.

    KNOWN_SHARED_STATE now declares zero live entries (both retired — see the
    script's own note). The script runs under ``set -uo pipefail``, and
    ``"${KNOWN_SHARED_STATE[@]}"`` on an empty array is only safe (no "unbound
    variable" abort) on bash >= 4.4. Pinned here rather than trusted to the
    interpreter version: a KNOWN_FAILURES-only failure must still classify
    exactly as before — recognized, no page, no infra misfire — with the
    sibling array empty.
    """
    src = SUITE.read_text(encoding="utf-8")
    import re as _re

    body = _re.search(r"KNOWN_SHARED_STATE=\((.*?)^\)", src, _re.S | _re.M)
    assert body is not None, "KNOWN_SHARED_STATE array declaration went missing"
    live_entries = [
        line for line in body.group(1).splitlines()
        if line.strip().startswith("'")
    ]
    assert live_entries == [], (
        "KNOWN_SHARED_STATE grew a live entry again — update this test's "
        "premise"
    )

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        log = (
            f"FAILED {KNOWN} - ModuleNotFoundError: No module named 'dspy'\n"
            "1 failed, 10381 passed in 984.99s (0:16:24)\n"
        )
        out = _run_nightly(tmp_path, _stub_repo(tmp_path, stdout=log, rc=1))

    assert "run did not complete" not in out, out
    assert "ordered:infra" not in out
    assert "VERDICT PASS" in out, out
    assert "all 1 reported failure(s) known" in out


def test_known_failures_is_currently_empty():
    """The 2026-11-06 retirement emptied KNOWN_FAILURES. An entry added back
    must carry its rig-not-code reason in the script; this pin makes the
    addition a deliberate edit here too rather than a silent mask."""
    import re as _re

    src = SUITE.read_text(encoding="utf-8")
    body = _re.search(r"KNOWN_FAILURES=\((.*?)^\)", src, _re.S | _re.M)
    assert body is not None, "KNOWN_FAILURES array declaration went missing"
    live_entries = [
        line for line in body.group(1).splitlines()
        if line.strip().startswith("'")
    ]
    assert live_entries == []


def test_a_stale_synthetic_entry_is_reported_not_paged():
    """With the synthetic entries matching nothing, the stale-entry report
    names them and the night still passes."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        out = _run_nightly(
            tmp_path,
            _stub_repo(tmp_path, stdout="10382 passed in 984.99s\n", rc=0),
        )

    assert "VERDICT PASS" in out, out
    assert "allowlist entry matched nothing (retire it?): tests/data_pkg/test_synthetic_known_one" in out


def test_rc1_with_nothing_parsed_still_reports_infra():
    """The branch the fix must NOT have disarmed: a collection error or a dead
    docker exits non-zero having reported no node ids at all."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        out = _run_nightly(
            tmp_path,
            _stub_repo(
                tmp_path,
                stdout="ERROR: could not connect to docker daemon\n",
                rc=1,
            ),
        )

    assert "run did not complete (rc=1, no test failures parsed)" in out, out
    assert "ordered:infra" in out
    assert "VERDICT FAIL" in out


def test_an_unknown_failure_still_pages():
    """The allowlist must not swallow a new one — including alongside known
    failures, which is how a real regression actually arrives."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        log = (
            f"FAILED {KNOWN} - ModuleNotFoundError: No module named 'dspy'\n"
            f"FAILED {UNKNOWN} - assert False\n"
            "2 failed, 10380 passed in 984.99s (0:16:24)\n"
        )
        out = _run_nightly(tmp_path, _stub_repo(tmp_path, stdout=log, rc=1))

    assert "VERDICT FAIL" in out, out
    assert UNKNOWN in out
    # Named as a plain phase failure, not as infra: pytest ran fine.
    assert "ordered:infra" not in out
    assert "run did not complete" not in out


def test_a_clean_run_passes():
    """rc=0, nothing parsed — the genuinely green night."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        out = _run_nightly(
            tmp_path,
            _stub_repo(tmp_path, stdout="10382 passed in 984.99s\n", rc=0),
        )

    assert "VERDICT PASS" in out, out
    assert "run did not complete" not in out
    # The "all N known" line belongs to the rc!=0 path only.
    assert "reported failure(s) known" not in out


def test_captured_error_logging_is_not_mistaken_for_a_failure():
    """The `tests/` anchor in `failures_from`. Application logging emits lines
    starting `ERROR <logger>:<file>:<line>`; a looser pattern turns those into
    phantom node ids and pages about a passing run."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        log = (
            "ERROR legba.data.registry.api:api.py:1940 ws send failed\n"
            "ERROR asyncio:base_events.py:1738 Task exception was never retrieved\n"
            "10382 passed in 984.99s\n"
        )
        out = _run_nightly(tmp_path, _stub_repo(tmp_path, stdout=log, rc=0))

    assert "VERDICT PASS" in out, out


def test_the_hygiene_reports_are_logged_and_never_move_the_verdict():
    """Roll 85: the three read-only P-8 reports run after the test phases;
    findings (rc=1), a connection error (rc=2) and a clean report are each
    one summary line, and none of them turns a clean night into a FAIL."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        repo = _stub_repo(tmp_path, stdout="10382 passed in 984.99s\n", rc=0)
        for name, rc in (("report_list_weights", 1), ("report_schema_drift", 2), ("report_unused_indexes", 0)):
            (repo / "scripts" / f"{name}.py").write_text(
                f"import sys\nprint('{name} ran', sys.argv[1:])\nsys.exit({rc})\n", encoding="utf-8")
        out = _run_nightly(tmp_path, repo)
    assert "hygiene report_list_weights: findings (rc=1)" in out
    assert "hygiene report_schema_drift: did not run cleanly (rc=2)" in out
    assert "hygiene report_unused_indexes: clean" in out
    assert "VERDICT PASS" in out and "VERDICT FAIL" not in out
