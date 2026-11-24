# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X1-A4 — the nightly verdict ledger and the roll gate that reads it.

What this file proves, end to end through the REAL scripts (no re-implementation
of either):

  * THE LEDGER — ``scripts/host_nightly_suite.sh`` appends exactly one
    ``NIGHTLY_VERDICT`` line per run to its self-log, carrying the full SHA,
    PASS/FAIL, the phases run, the dirty bit and per-phase pass/fail counts;
    it never posts to the shared ``legba-alerts`` topic.
  * THE GATE — ``scripts/rolling_deploy.sh`` refuses (exit 6) to roll a SHA
    whose newest full-suite, clean-tree record on that SHA or an ancestor is
    FAIL, and it does so under ``--dry-run`` too (a gate a dry run waves
    through proves nothing). A newer PASS clears it; a record on a sibling
    branch, a dirty tree or a lint-only run never vouches; ``--ignore-nightly``
    needs a reason and writes it to the ledger.

GIT IS SCRIPTED. The test image carries no git binary (a deliberate property
of legba/legba-test: four git-guarded files skip on it, and turning git on
image-wide flips them — X1-A4 measured that, 10-08). So each test puts a small
``git`` on PATH that answers exactly the four questions the scripts ask —
``rev-parse`` (HEAD / a full sha / --short / --abbrev-ref), ``merge-base
--is-ancestor`` over a declared commit graph, and ``diff --quiet HEAD`` — and
fails loudly (exit 2) on anything else. The tests prove the scripts' reading
of the ledger given git's answers; git's ancestry semantics are git's. Every
call is logged, and the gate tests assert the ancestry question was asked.

Nothing in the checkout or on the stack is touched: ``--dry-run
--skip-build`` only announces commands, and the image-age guard is read-only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "scripts" / "host_nightly_suite.sh"
ROLL = ROOT / "scripts" / "rolling_deploy.sh"

BASE, A, B, SIB = ("1" * 40, "a" * 40, "b" * 40, "5" * 40)
#: base -> a -> b on main; sib is a sibling commit off base.
GRAPH = {BASE: [], A: [BASE], B: [A], SIB: [BASE]}

_STUB_GIT = r'''
import json, os, sys
cfg = json.load(open(os.environ["STUB_GIT_CONFIG"]))
with open(os.environ["STUB_GIT_CALLS"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:1] == ["-C"]:
    args = args[2:]
graph, head = cfg["graph"], cfg["head"]

def resolve(rev):
    rev = rev.split("^{")[0]
    if rev == "HEAD":
        return head
    return rev if rev in graph else None

if args[:1] == ["rev-parse"]:
    flags = [a for a in args[1:] if a.startswith("--")]
    rest = [a for a in args[1:] if not a.startswith("--")]
    sha = resolve(rest[0]) if rest else None
    if sha is None:
        sys.exit(1 if "--quiet" in flags else 128)
    if "--abbrev-ref" in flags:
        print(cfg.get("branch", "main"))
    elif "--short" in flags:
        print(sha[:8])
    else:
        print(sha)
    sys.exit(0)
if args[:2] == ["merge-base", "--is-ancestor"]:
    anc, desc = resolve(args[2]), resolve(args[3])
    if anc is None or desc is None:
        sys.exit(128)
    seen, todo = set(), [desc]
    while todo:
        c = todo.pop()
        if c == anc:
            sys.exit(0)
        if c not in seen:
            seen.add(c)
            todo.extend(graph.get(c, []))
    sys.exit(1)
if args[:3] == ["diff", "--quiet", "HEAD"]:
    sys.exit(1 if cfg.get("dirty") else 0)
sys.stderr.write("stub git: unscripted call: %r\n" % (sys.argv[1:],))
sys.exit(2)
'''


@pytest.fixture(autouse=True)
def _require_tools():
    for tool in ("bash", "flock", "tac"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not available")


def _stub_env(tmp_path: Path, *, head: str, dirty: bool = False) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "git"
    stub.write_text(f"#!{sys.executable}\n{_STUB_GIT}")
    stub.chmod(0o755)
    cfg = tmp_path / "git.json"
    cfg.write_text(json.dumps({"graph": GRAPH, "head": head, "dirty": dirty}))
    return {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "STUB_GIT_CONFIG": str(cfg),
        "STUB_GIT_CALLS": str(tmp_path / "git.calls"),
    }


# ---------------------------------------------------------------------------
# THE GATE
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, object]:
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    shutil.copy2(ROLL, r / "scripts" / "rolling_deploy.sh")
    return {"root": r, "tmp": tmp_path, "ledger": tmp_path / "nightly.log"}


def _record(sha: str, verdict: str, *, phases: str = "lint,ordered,shuffled", dirty: int = 0) -> str:
    return (f"NIGHTLY_VERDICT 2026-10-08T10:59:23Z sha={sha} verdict={verdict} "
            f"phases={phases} dirty={dirty} run=20261008T100001Z "
            f"ordered=17375p/1f/0e shuffled=17374p/3f/0e unexpected=4")


def _roll(repo: dict[str, object], *extra: str, rolled: str = B) -> subprocess.CompletedProcess[str]:
    root = repo["root"]
    args = ["bash", str(root / "scripts" / "rolling_deploy.sh"), "--dry-run", "--skip-build",
            "--nightly-log", str(repo["ledger"]), "--rolled-sha", rolled, *extra]
    return subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=120,
                          env=_stub_env(repo["tmp"], head=rolled))


def _calls(repo: dict[str, object]) -> list[str]:
    path = repo["tmp"] / "git.calls"
    return path.read_text().splitlines() if path.exists() else []


def test_a_red_record_on_an_ancestor_blocks_even_a_dry_run(repo):
    repo["ledger"].write_text(_record(A, "FAIL") + "\n")
    proc = _roll(repo)
    assert proc.returncode == 6, proc.stdout + proc.stderr
    assert "nightly gate" in proc.stderr and "is FAIL" in proc.stderr
    # Refused at step 0: nothing after the gate was even announced.
    assert "build: registry" not in proc.stdout and "registry: recreate" not in proc.stdout
    assert any(c.endswith(f"merge-base --is-ancestor {A} {B}") for c in _calls(repo)), _calls(repo)


def test_a_red_record_on_the_rolled_sha_itself_blocks(repo):
    repo["ledger"].write_text(_record(B, "FAIL") + "\n")
    assert _roll(repo).returncode == 6


def test_a_newer_pass_clears_an_older_red(repo):
    repo["ledger"].write_text(_record(A, "FAIL") + "\n" + _record(B, "PASS") + "\n")
    proc = _roll(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "is PASS" in proc.stdout


def test_records_that_cannot_vouch_are_skipped(repo):
    """A sibling-branch FAIL, a dirty-tree FAIL and a lint-only FAIL are all
    newer than the ancestor PASS; none of them judges this SHA."""
    repo["ledger"].write_text("\n".join([
        _record(BASE, "PASS"),
        _record(SIB, "FAIL"),
        _record(A, "FAIL", dirty=1),
        _record(A, "FAIL", phases="lint"),
        _record("f" * 40, "FAIL"),  # a sha this repository does not know
        f"NIGHTLY_VERDICT 2026-10-08T11:00:00Z sha={A} verdict=FAIL",  # fields missing: must not abort the script
        "garbage line NIGHTLY_VERDICT sha=",
    ]) + "\n")
    proc = _roll(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"ancestor {BASE[:12]}" in proc.stdout


def test_no_record_at_all_warns_and_proceeds(repo):
    proc = _roll(repo)  # the ledger file does not exist
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NO full-suite nightly record" in proc.stdout


def test_the_override_needs_a_reason_and_is_written_to_the_ledger(repo):
    repo["ledger"].write_text(_record(A, "FAIL") + "\n")
    bare = _roll(repo, "--ignore-nightly", "--ui")
    assert bare.returncode == 1, "a flag must never be swallowed as the reason"
    blank = _roll(repo, "--ignore-nightly", "   ")
    assert blank.returncode == 1
    ok = _roll(repo, "--ignore-nightly", "hotfix for the 10-08 page")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    lines = repo["ledger"].read_text().splitlines()
    assert lines[-1].startswith("NIGHTLY_GATE_OVERRIDE ")
    assert f"sha={B}" in lines[-1] and f"record_sha={A}" in lines[-1]
    assert lines[-1].endswith("reason=hotfix for the 10-08 page")


# ---------------------------------------------------------------------------
# THE LEDGER LINE
# ---------------------------------------------------------------------------


def _nightly(tmp_path: Path, *, stdout: str, rc: int, dirty: bool = False) -> tuple[list[str], str]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    runner = root / "scripts" / "run_tests_in_container.sh"
    runner.write_text(f"#!/usr/bin/env bash\ncat <<'EOF'\n{stdout}\nEOF\nexit {rc}\n")
    runner.chmod(0o755)
    self_log = tmp_path / "self.log"
    env = {
        **_stub_env(tmp_path, head=B, dirty=dirty),
        "LEGBA_REPO_ROOT": str(root),
        "LEGBA_NIGHTLY_LOG_DIR": str(tmp_path / "logs"),
        "LEGBA_NIGHTLY_SELF_LOG": str(self_log),
        "LEGBA_NIGHTLY_LOCK_FILE": str(tmp_path / "nightly.lock"),
        "LEGBA_TEST_LOCK_FILE": str(tmp_path / "tests.lock"),
        "LEGBA_NIGHTLY_NTFY_URL": "http://127.0.0.1:9/none",  # unroutable: no page leaves a test
        "PHASES": "ordered",
        "LEGBA_NIGHTLY_PHASE_TIMEOUT": "60",
    }
    proc = subprocess.run(["bash", str(SUITE)], env=env, capture_output=True, text=True, timeout=180)
    verdicts = [ln for ln in self_log.read_text().splitlines() if ln.startswith("NIGHTLY_VERDICT ")]
    return verdicts, proc.stdout + proc.stderr


def test_a_green_night_writes_one_pass_line_with_sha_and_counts(tmp_path: Path):
    verdicts, out = _nightly(tmp_path, stdout="17380 passed, 118 skipped in 900.00s", rc=0)
    assert len(verdicts) == 1, out
    line = verdicts[0]
    assert f" sha={B} " in line and " verdict=PASS " in line
    assert " phases=ordered " in line and " dirty=0 " in line
    assert " ordered=17380p/0f/0e " in line and " unexpected=0 " in line
    assert line.endswith(" advisory=0 gate_phases=ordered")


def test_a_red_night_writes_one_fail_line(tmp_path: Path):
    verdicts, out = _nightly(
        tmp_path,
        stdout="FAILED tests/test_x.py::test_new_regression - assert 0\n"
               "1 failed, 17379 passed in 900.00s",
        rc=1,
    )
    assert len(verdicts) == 1, out
    assert f" sha={B} " in verdicts[0] and " verdict=FAIL " in verdicts[0]
    assert " ordered=17379p/1f/0e " in verdicts[0] and " unexpected=1 " in verdicts[0]
    assert verdicts[0].endswith(" advisory=0 gate_phases=ordered")


def test_a_dirty_tree_is_stamped_so_the_gate_ignores_it(tmp_path: Path):
    verdicts, out = _nightly(tmp_path, stdout="17380 passed in 900.00s", rc=0, dirty=True)
    assert len(verdicts) == 1 and " dirty=1 " in verdicts[0], out


def test_the_verdict_goes_to_its_own_topic_never_the_shared_alerts_one():
    src = SUITE.read_text()
    assert 'NTFY_URL="${LEGBA_NIGHTLY_NTFY_URL:-http://127.0.0.1:8093/nightly}"' in src
    assert "8093/legba-alerts" not in src
