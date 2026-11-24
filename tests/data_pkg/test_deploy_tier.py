# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-P — deploy tiers: the classifier, the backstops, the tier flag on the roll,
and the targeted gate's selection.

What this file proves:

  * THE RULES (``scripts/deploy_tier.py`` ``PATH_RULES``): every tier is
    reachable, an unknown path is full, a mixed range takes the highest tier and
    the UNION of actions, the descriptor family comes from ``schema_uri``.
  * THE IMPORT GRAPH on the REAL tree: registry route leaves map to the registry
    alone, the runtime core to the runtime, the shared schema layer to both.
  * THE BACKSTOPS: a red nightly, a streak of lower-tier rolls, an old full gate,
    a repeat t3 after a gate failure — each forces full; an explicit lower tier
    is refused unless forced, and the force is reported.
  * THE ROLL (``scripts/rolling_deploy.sh --tier``) through a harness that stubs
    docker, git, sleep, the classifier and the gate: default full is today's
    path; t0..t3 do only their tier's work; refusals and gate failures stop
    before any build; every real roll appends one DEPLOY_ROLL line.
  * THE GATE RUNNER (``scripts/run_targeted_gate.sh``): it runs exactly the
    classifier's paths, ordered, and prints the GATE_VERDICT line.

The image carries no git binary, so the classifier is driven through an
in-memory ``Git`` (same interface) and the shell scripts through PATH stubs.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location("deploy_tier_under_test", SCRIPTS / "deploy_tier.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


dt = _load()


# ---------------------------------------------------------------------------
# an in-memory git with the classifier's interface
# ---------------------------------------------------------------------------
class MemGit:
    def __init__(self, revs: dict[str, dict[str, str]], order: list[str] | None = None):
        self.revs = revs
        self.order = order or list(revs)

    def _r(self, rev: str) -> str | None:
        rev = rev.split("^{")[0]
        if rev == "HEAD":
            return self.order[-1]
        return rev if rev in self.revs else None

    def sha(self, rev):
        return self._r(rev) or ""

    def tree(self, rev, path):
        files = self.revs[self._r(rev)]
        h = hashlib.sha1(json.dumps(sorted((p, t) for p, t in files.items()
                                           if p.startswith(path + "/"))).encode())
        return h.hexdigest()

    def is_ancestor(self, a, b):
        a, b = self._r(a), self._r(b)
        return a is not None and b is not None and self.order.index(a) <= self.order.index(b)

    def changed(self, a, b):
        fa, fb = self.revs[self._r(a)], self.revs[self._r(b)]
        out = []
        for p in sorted(set(fa) | set(fb)):
            if p not in fb:
                out.append(("D", p))
            elif p not in fa:
                out.append(("A", p))
            elif fa[p] != fb[p]:
                out.append(("M", p))
        return out

    def ls(self, rev, *paths):
        return sorted(p for p in self.revs[self._r(rev)]
                      if not paths or any(p == q or p.startswith(q.rstrip("/") + "/") for q in paths))

    def read_many(self, rev, paths):
        files = self.revs[self._r(rev)]
        return {p: files[p] for p in paths if p in files}

    def read(self, rev, path):
        return self.read_many(rev, [path]).get(path)

    def grep_files(self, rev, needles, *paths):
        return {p for p in self.ls(rev, *paths)
                if any(n in self.revs[self._r(rev)][p] for n in needles)}


#: A miniature tree with the real entry-point names.
BASE = {
    "src/legba/__init__.py": "",
    "src/legba/data/__init__.py": "",
    "src/legba/data/schemas/__init__.py": "X = 1\n",
    "src/legba/data/registry/__init__.py": "",
    "src/legba/data/registry/server.py": (
        "from legba.data.schemas import X\n"
        "from legba.data.registry import flow_api, writer_api\n"
        "from legba.data import shared_util\n"),
    "src/legba/data/registry/flow_api.py": "def route():\n    return 1\n",
    "src/legba/data/registry/writer_api.py": "Q = 'INSERT INTO t VALUES (1)'\n",
    "src/legba/data/shared_util.py": "def f():\n    return 2\n",
    "src/legba/data/orphan.py": "Z = 3\n",
    "src/legba/data/by_string.py": "W = 4\n",
    "src/legba/runtime/__init__.py": "",
    "src/legba/runtime/dapr_host.py": (
        "import importlib\n"
        "from legba.data.schemas import X\n"
        "from legba.data import shared_util\n"
        "def boot():\n"
        "    from legba.runtime import pack_staleness\n"
        "    return importlib.import_module('legba.data.by_string')\n"),
    "src/legba/runtime/pack_staleness.py": "def sweep():\n    return 0\n",
    "src/legba/runtime/dapr_actors.py": "A = 1\n",
    "src/legba/runtime/dapr_workflow/__init__.py": "",
    "src/legba/runtime/dapr_workflow/worker.py": "from legba.runtime import dapr_host\n",
    "src/legba/ui/__init__.py": "",
    "src/legba/ui/mcp_server.py": "",
    "tests/conftest.py": "",
    "tests/data_pkg/test_flow.py": "from legba.data.registry import flow_api\ndef test_a():\n    pass\n",
    "tests/data_pkg/test_shared.py": "from legba.data.registry import server\ndef test_b():\n    pass\n",
    "tests/runtime/test_pack.py": "import legba.runtime.pack_staleness\ndef test_c():\n    pass\n",
    "tests/data_pkg/test_slim_probe.py": "# slim image\nimport sys\nsys.modules['x'] = None\n",
    "tests/test_roll.py": "ROLL = 'scripts/rolling_deploy.sh'\n",
    "tests/descriptors/test_src.py": "ID = 'source.example.feed'\n",
    "descriptors/source_example.yaml": "identity:\n  id: source.example.feed\n  schema_uri: legba/source/1.0.0\n",
    "descriptors/stack_component_x.yaml": "identity:\n  id: stack.x\n  schema_uri: legba/stack_component/1.0.0\n",
    "descriptors/LIFECYCLE.md": "| family | id | state | file ships |\n",
    "docs/RUNBOOK.md": "runbook\n",
    "legba-ui-v3/src/App.tsx": "export {}\n",
    "scripts/rolling_deploy.sh": "#!/bin/sh\n",
    "pyproject.toml": "[project]\n",
}


def _range(**changes: str | None) -> MemGit:
    head = dict(BASE)
    for path, text in changes.items():
        path = path.replace("__", "/")
        if text is None:
            head.pop(path, None)
        else:
            head[path] = text
    return MemGit({"base": BASE, "head": head}, ["base", "head"])


def _edit(path: str, text: str | None = "# edited\n") -> MemGit:
    head = dict(BASE)
    if text is None:
        head.pop(path)
    else:
        head[path] = (BASE.get(path, "") + text)
    return MemGit({"base": BASE, "head": head}, ["base", "head"])


def _plan(git: MemGit):
    return dt.classify(git, "base", "head", use_cache=False)


# ---------------------------------------------------------------------------
# THE RULES
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,kind", [
    ("docs/RUNBOOK.md", "t0"),
    ("README.md", "t0"),
    ("scripts/disk_floor_check.sh", "t0"),
    ("deploy/cron.d/legba-nightly-suite", "t0"),
    ("legba-models/main.py", "t0"),
    ("descriptors/examples/source_example_x.yaml", "t0"),
    ("tests/data_pkg/test_x.py", "tests"),
    ("descriptors/analyst_x.yaml", "descriptor"),
    ("descriptors/LIFECYCLE.md", "descriptor"),
    ("descriptors/LIFECYCLE.d/source__s.x.md", "descriptor"),
    ("profiles/geopolitics/rosters/source_catalog.yaml", "descriptor"),
    ("legba-ui-v3/src/panels/Flow.tsx", "t2"),
    ("legba-ui-v3/package.json", "t2"),
    ("src/legba/data/registry/flow_api.py", "graph"),
    ("src/legba/runtime/pack_staleness.py", "graph"),
    ("src/legba/data/migrations/0243_x.sql", "full"),
    ("src/legba/data/schemas/analyst.py", "full"),
    ("src/legba/runtime/dapr_actors.py", "full"),
    ("src/legba/runtime/source_actor.py", "full"),
    ("src/legba/data/archive_store.py", "full"),
    ("src/legba/data/outputs/sink.py", "full"),
    ("src/legba/data/registry/descriptor_refs.py", "full"),
    ("src/legba/data/registry/descriptor_prompts.json", "full"),
    ("src/legba/data/registry/descriptor_prompts/analyst_x.json", "full"),
    ("profiles/geopolitics/prompts/legba_geopolitics/lens_left.py", "full"),
    ("docker/Dockerfile.runtime", "full"),
    ("docker-compose.yml", "full"),
    ("pyproject.toml", "full"),
    ("dapr/components/statestore.yaml", "full"),
    ("deploy/compose.test.yml", "full"),
])
def test_each_path_meets_its_rule(path, kind):
    rule = dt.match_rule(path)
    assert rule is not None and rule[1] == kind, (path, rule)


def test_an_unknown_path_has_no_rule_and_classifies_full():
    assert dt.match_rule("weird/new_thing.bin") is None
    git = _range(weird__new_thing="x")
    plan = _plan(git)
    assert plan.tier == "full"
    assert "unknown path" in plan.paths[0].reason
    assert plan.services == ["registry", "runtime", "worker"] and plan.migrate


def test_docs_only_is_t0_with_the_guard_gate_and_no_restart():
    plan = _plan(_edit("docs/RUNBOOK.md"))
    assert plan.tier == "t0" and not plan.restart and not plan.services
    assert not plan.full_gate and plan.est_gate_seconds < 600


def test_the_guard_set_rides_every_tier_on_the_real_tree():
    """On a tree where the guard files exist, even a docs-only range names them all."""
    real = DiskGit(ROOT)
    head = dict(real.revs["tree"])
    head["docs/RUNBOOK.md"] = "edited\n"
    git = MemGit({"base": real.revs["tree"], "head": head}, ["base", "head"])
    plan = dt.classify(git, "base", "head", use_cache=False)
    assert plan.tier == "t0"
    assert set(dt.GUARD_TESTS) <= set(plan.tests)


def test_a_host_script_is_t0_and_selects_the_tests_that_name_it():
    plan = _plan(_edit("scripts/rolling_deploy.sh"))
    assert plan.tier == "t0" and not plan.services
    assert "merging installs it" in plan.paths[0].reason
    assert "tests/test_roll.py" in plan.tests


def test_a_changed_test_is_t0_and_is_its_own_gate():
    plan = _plan(_edit("tests/runtime/test_pack.py", "def test_d():\n    pass\n"))
    assert plan.tier == "t0" and "tests/runtime/test_pack.py" in plan.tests


def test_a_walked_descriptor_is_t1_and_walks_only_its_family():
    plan = _plan(_edit("descriptors/source_example.yaml", "  note: x\n"))
    assert plan.tier == "t1" and plan.walk_families == ["source"]
    assert not plan.services and not plan.restart and plan.descriptor_check
    # the test naming the descriptor id is selected (here inside the t1 directory)
    assert "tests/descriptors/test_src.py" in plan.tests or "tests/descriptors/" in plan.tests
    assert "tests/descriptors/" in plan.tests  # the t1 set (the files absent here are dropped)


def test_lifecycle_rows_walk_every_family():
    plan = _plan(_edit("descriptors/LIFECYCLE.md", "| source | s.x | active | draft |\n"))
    assert plan.tier == "t1" and plan.walk_families == list(dt.WALKED_FAMILIES)
    # R-1: a row file in LIFECYCLE.d/ is the same kind of change
    plan = _plan(_edit("descriptors/LIFECYCLE.d/source__s.x.md", "| source | s.x | active | draft |\n"))
    assert plan.tier == "t1" and plan.walk_families == list(dt.WALKED_FAMILIES)


def test_a_descriptor_the_walker_does_not_own_is_full():
    plan = _plan(_edit("descriptors/stack_component_x.yaml", "  note: x\n"))
    assert plan.tier == "full" and "not walked" in plan.paths[0].reason


def test_ui_only_is_t2_with_the_ui_gate_and_no_backend_restart():
    plan = _plan(_edit("legba-ui-v3/src/App.tsx"))
    assert plan.tier == "t2" and plan.ui and not plan.services and not plan.restart


def test_a_registry_route_leaf_is_t3_registry_only_with_the_slim_probes():
    plan = _plan(_edit("src/legba/data/registry/flow_api.py"))
    assert plan.tier == "t3" and plan.services == ["registry"] and not plan.migrate
    assert "tests/data_pkg/test_flow.py" in plan.tests  # imports it
    assert "tests/data_pkg/test_slim_probe.py" in plan.tests  # registry changed -> probes
    assert "tests/runtime/test_pack.py" not in plan.tests


def test_a_runtime_leaf_is_t3_runtime_plus_its_worker():
    plan = _plan(_edit("src/legba/runtime/pack_staleness.py"))
    assert plan.tier == "t3" and plan.services == ["runtime", "worker"]
    assert "tests/runtime/test_pack.py" in plan.tests
    assert "tests/data_pkg/test_slim_probe.py" not in plan.tests


def test_a_module_both_services_import_is_full():
    plan = _plan(_edit("src/legba/data/shared_util.py"))
    assert plan.tier == "full" and "shared" in plan.paths[0].reason


def test_a_string_import_counts_as_an_import():
    # dapr_host reaches by_string only through importlib.import_module('...')
    plan = _plan(_edit("src/legba/data/by_string.py"))
    assert plan.tier == "t3" and plan.services == ["runtime", "worker"]


def test_a_write_path_is_full_even_when_one_service_reaches_it():
    plan = _plan(_edit("src/legba/data/registry/writer_api.py"))
    assert plan.tier == "full" and "write path" in plan.paths[0].reason


def test_a_module_no_service_reaches_is_full():
    plan = _plan(_edit("src/legba/data/orphan.py"))
    assert plan.tier == "full" and "cannot place" in plan.paths[0].reason


def test_a_deleted_module_is_full():
    plan = _plan(_edit("src/legba/data/registry/flow_api.py", None))
    assert plan.tier == "full" and "removed" in plan.paths[0].reason


def test_a_mixed_range_takes_the_highest_tier_and_the_union_of_actions():
    head = dict(BASE)
    head["docs/RUNBOOK.md"] += "x\n"
    head["legba-ui-v3/src/App.tsx"] += "x\n"
    head["descriptors/source_example.yaml"] += "  note: x\n"
    head["src/legba/data/registry/flow_api.py"] += "x = 1\n"
    plan = _plan(MemGit({"base": BASE, "head": head}, ["base", "head"]))
    assert plan.tier == "t3"
    assert plan.services == ["registry"] and plan.walk_families == ["source"] and plan.ui
    head["pyproject.toml"] += "x\n"
    plan = _plan(MemGit({"base": BASE, "head": head}, ["base", "head"]))
    assert plan.tier == "full" and plan.full_gate and plan.migrate
    assert plan.walk_families == ["source"] and plan.ui  # full keeps the range's walk + UI


def test_no_deployed_sha_is_full():
    plan = dt.classify(_edit("docs/RUNBOOK.md"), None, "head", use_cache=False)
    assert plan.tier == "full" and "no deployed SHA" in plan.note


def test_every_guard_test_exists_in_the_tree():
    for t in dt.GUARD_TESTS + dt.T1_TESTS:
        assert (ROOT / t).exists(), t


def test_rules_never_shadow_a_full_rule_with_a_graph_rule():
    """The protected src/ globs must come BEFORE the catch-all graph rule."""
    globs = [g for g, _, _ in dt.PATH_RULES]
    graph_at = globs.index("src/**.py")
    for g, kind, _ in dt.PATH_RULES:
        if g.startswith("src/") and kind == "full" and g != "src/**":
            assert globs.index(g) < graph_at, g


# ---------------------------------------------------------------------------
# THE IMPORT GRAPH on the real tree
# ---------------------------------------------------------------------------
class DiskGit(MemGit):
    """The working tree as one revision (the graph reads only what git would)."""

    def __init__(self, root: Path):
        files = {}
        for top in ("src", "profiles", "tests", "descriptors", "scripts"):
            for p in (root / top).rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".yaml", ".yml"):
                    files[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
        super().__init__({"tree": files}, ["tree"])


@pytest.fixture(scope="module")
def real_graph():
    return dt.build_graph(DiskGit(ROOT), "tree", use_cache=False)


def test_the_real_registry_route_leaf_maps_to_the_registry_alone(real_graph):
    assert real_graph.services_of("legba.data.registry.flow_api") == ["registry"]
    assert real_graph.services_of("legba.data.registry.server") == ["registry"]


def test_the_real_runtime_core_and_shared_schema_map_where_they_live(real_graph):
    assert "runtime" in real_graph.services_of("legba.runtime.dapr_actors")
    assert "registry" not in real_graph.services_of("legba.runtime.dapr_host")
    both = real_graph.services_of("legba.data.schemas")
    assert "registry" in both and "runtime" in both
    assert "worker" in real_graph.services_of("legba.runtime.dapr_workflow.worker")


def test_the_real_graph_marks_sql_writers(real_graph):
    assert "legba.data.provenance.writes" in real_graph.write_paths
    assert "legba.data.registry.flow_api" not in real_graph.write_paths


# ---------------------------------------------------------------------------
# THE BACKSTOPS
# ---------------------------------------------------------------------------
LINEAR = MemGit({"a": {}, "b": {}, "c": {}}, ["a", "b", "c"])
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z


def _iso(t: float) -> str:
    import datetime as _d
    return _d.datetime.fromtimestamp(t, _d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nightly(t: float, sha: str, verdict: str) -> str:
    return (f"NIGHTLY_VERDICT {_iso(t)} sha={sha} verdict={verdict} phases=lint,ordered,shuffled "
            f"dirty=0 run=x ordered=1p/0f/0e")


def _roll(t: float, tier: str, *, result: str = "ok", services: str = "-", gate: str = "targeted",
          gate_result: str = "PASS(1p/0f/0e)", to: str = "b") -> str:
    return (f"DEPLOY_ROLL {_iso(t)} result={result} exit=0 from=a to={to} tier_requested={tier} "
            f"tier_computed={tier} tier_required={tier} tier_used={tier} forced=0 forced_by=- "
            f"backstops=- gate={gate} gate_result={gate_result} services={services} walk=- ui=0 "
            f"migrate=0 dry_run=0 actor=op")


def _backstop(tmp_path, computed, nightly=(), rolls=(), services=()):
    nl, dl = tmp_path / "nightly.log", tmp_path / "deploy.log"
    nl.write_text("".join(l + "\n" for l in nightly))
    dl.write_text("".join(l + "\n" for l in rolls))
    return dt.apply_backstops(computed, git=LINEAR, rolled="c", deploy_log_path=str(dl),
                              nightly_log_path=str(nl), services=list(services), now=NOW)


def test_a_fresh_green_nightly_lets_a_lower_tier_through(tmp_path):
    tier, hits = _backstop(tmp_path, "t3", nightly=[_nightly(NOW - 3600, "b", "PASS")])
    assert tier == "t3" and hits == []


def test_a_red_nightly_forces_full_until_a_full_gate_passes(tmp_path):
    red = [_nightly(NOW - 7200, "a", "PASS"), _nightly(NOW - 3600, "b", "FAIL")]
    tier, hits = _backstop(tmp_path, "t1", nightly=red)
    assert tier == "full" and hits[0].startswith("nightly:")
    cleared = [_roll(NOW - 600, "full", gate="full", gate_result="PASS(19000p/0f/0e)")]
    tier, hits = _backstop(tmp_path, "t1", nightly=red, rolls=cleared)
    assert tier == "t1", hits


def test_five_lower_rolls_in_a_row_make_the_next_full(tmp_path):
    fresh = [_nightly(NOW - 3600, "b", "PASS")]
    four = [_roll(NOW - 3000 + i, "t0") for i in range(4)]
    assert _backstop(tmp_path, "t0", nightly=fresh, rolls=four)[0] == "t0"
    five = [_roll(NOW - 3000 + i, "t1") for i in range(5)]
    tier, hits = _backstop(tmp_path, "t0", nightly=fresh, rolls=five)
    assert tier == "full" and hits[0].startswith("streak:")
    # a full roll resets the count
    reset = five[:3] + [_roll(NOW - 2000, "full", gate="nightly-ledger")] + five[3:]
    assert _backstop(tmp_path, "t0", nightly=fresh, rolls=reset)[0] == "t0"


def test_an_old_full_gate_forces_full(tmp_path):
    tier, hits = _backstop(tmp_path, "t2", nightly=[_nightly(NOW - 25 * 3600, "b", "PASS")])
    assert tier == "full" and hits[0].startswith("age:")
    tier, hits = _backstop(tmp_path, "t2")  # nothing on record at all
    assert tier == "full" and "no passing full gate" in hits[0]


def test_a_repeat_t3_on_a_service_whose_gate_failed_is_full(tmp_path):
    fresh = [_nightly(NOW - 3600, "b", "PASS")]
    fail = [_roll(NOW - 900, "t3", result="gate_fail", services="registry",
                  gate_result="FAIL(3p/1f/0e)")]
    tier, hits = _backstop(tmp_path, "t3", nightly=fresh, rolls=fail, services=["registry"])
    assert tier == "full" and hits[0].startswith("repeat:")
    tier, _ = _backstop(tmp_path, "t3", nightly=fresh, rolls=fail, services=["runtime", "worker"])
    assert tier == "t3"


def _decide(tmp_path, requested, computed="t1", force=False):
    plan = dt.Plan("a", "c", computed, [], [], [], False, False, False, [], False, False, 0)
    (tmp_path / "n.log").write_text(_nightly(NOW - 60, "b", "PASS") + "\n")
    return dt.decide(requested, plan, force=force, git=LINEAR,
                     deploy_log_path=str(tmp_path / "none.log"),
                     nightly_log_path=str(tmp_path / "n.log"), now=NOW)


def test_decide_refuses_a_lower_explicit_tier_unless_forced(tmp_path):
    d = _decide(tmp_path, "t0", computed="t3")
    assert d.refused and not d.forced and "REFUSED" in d.message
    d = _decide(tmp_path, "t0", computed="t3", force=True)
    assert d.used == "t0" and d.forced and not d.refused
    d = _decide(tmp_path, "full", computed="t1")
    assert d.used == "full" and not d.refused
    d = _decide(tmp_path, "auto", computed="t1")
    assert d.used == "t1" and not d.refused


def test_backstop_rules_are_data():
    assert dt.BACKSTOPS == {
        "nightly_fail_forces_full": True,
        "max_consecutive_lower": 5,
        "max_hours_since_full_gate": 24,
        "t3_repeat_after_gate_fail": True,
    }


def test_the_deployed_sha_is_the_newest_real_successful_roll(tmp_path):
    log = tmp_path / "d.log"
    log.write_text("\n".join([
        _roll(NOW - 300, "t0", to="a"),
        _roll(NOW - 200, "t3", to="b"),
        _roll(NOW - 100, "t3", to="c", result="gate_fail"),
        _roll(NOW - 50, "t1", to="c").replace("dry_run=0", "dry_run=1"),
    ]) + "\n")
    assert dt.deployed_sha(str(log)) == "b"
    assert dt.deployed_sha(str(tmp_path / "absent.log")) is None


# ---------------------------------------------------------------------------
# THE ROLL — rolling_deploy.sh --tier through stubs
# ---------------------------------------------------------------------------
SHA = "c" * 40

_DOCKER_STUB = r'''#!/usr/bin/env bash
echo "$*" >> "$STUB_DOCKER_CALLS"
case "$*" in
  inspect*) date -u +%Y-%m-%dT%H:%M:%SZ; exit 0 ;;
  *" ps "*|*" ps") for s in legba-registry legba-runtime-dapr legba-dapr-workflow-worker legba-ui-build; do echo "$s healthy"; done; exit 0 ;;
  *" logs "*) echo "x dapr_actors.pack_staleness.sweep n=3"; [ -n "${STUB_PARSE_FAIL:-}" ] && echo "x analyst_deps_resolver.parse_failed actor_id=a err=b"; exit 0 ;;
  "builder prune --help") printf '%s\n' "${STUB_PRUNE_HELP:-      --max-used-space bytes   Maximum amount of disk space allowed to keep for cache
      --reserved-space bytes   Amount of disk space always allowed to keep for cache}"; exit 0 ;;
  "buildx du") echo "Total: 1GB"; exit 0 ;;
esac
exit 0
'''

_GIT_STUB = r'''#!/usr/bin/env bash
case "$*" in *rev-parse*) echo "''' + SHA + r'''"; exit 0 ;; *"diff --quiet"*) exit 0 ;; esac
exit 1
'''

_TIER_STUB = r'''#!/usr/bin/env bash
echo "$*" >> "$STUB_TIER_CALLS"
printf '%s\n' "$STUB_TIER_OUT"
exit "${STUB_TIER_RC:-0}"
'''

_GATE_STUB = r'''#!/usr/bin/env bash
echo "$*" >> "$STUB_GATE_CALLS"
echo "GATE_VERDICT 2026-11-13T00:00:00Z sha=x verdict=${STUB_GATE_VERDICT:-PASS} tier=$1 tier_computed=t3 range=$2 phases=lint,targeted dirty=0 run=r targeted=7p/0f/0e lint=ok ui=- tests=3 wall=9s"
[ "${STUB_GATE_VERDICT:-PASS}" = PASS ]
'''


def _tier_out(used: str, *, computed: str | None = None, services: str = "", walk: str = "",
              ui: str = "0", forced: str = "0", required: str | None = None) -> str:
    return "\n".join([
        f"TIER_COMPUTED={computed or used}", "TIER_FROM=" + "a" * 40, f"TIER_TO={SHA}",
        f"TIER_SERVICES='{services}'", f"TIER_WALK_FAMILIES='{walk}'", f"TIER_UI={ui}",
        f"TIER_REQUIRED={required or computed or used}", f"TIER_USED={used}",
        f"TIER_FORCED={forced}", "TIER_REFUSED=0", "TIER_BACKSTOPS=''", "TIER_BACKSTOP_CODES=''",
        "TIER_MESSAGE='stub'",
    ])


@pytest.fixture
def rig(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "profiles" / "geopolitics").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "rolling_deploy.sh", repo / "scripts" / "rolling_deploy.sh")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", _DOCKER_STUB), ("git", _GIT_STUB), ("sleep", "#!/bin/sh\nexit 0\n"),
                       ("tier_stub", _TIER_STUB), ("gate_stub", _GATE_STUB)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("LEGBA_")},
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "STUB_DOCKER_CALLS": str(tmp_path / "docker.calls"),
        "STUB_TIER_CALLS": str(tmp_path / "tier.calls"),
        "STUB_GATE_CALLS": str(tmp_path / "gate.calls"),
        "LEGBA_TARGETED_GATE_SCRIPT": str(bindir / "gate_stub"),
        "LEGBA_DEPLOY_ACTOR": "operator",
    }
    return {"repo": repo, "tmp": tmp_path, "env": env, "log": tmp_path / "deploy.log"}


def _run(rig, *args, tier_out="", tier_rc=0, **env):
    e = {**rig["env"], "STUB_TIER_OUT": tier_out, "STUB_TIER_RC": str(tier_rc), **env}
    # rolling_deploy.sh runs `python3 <tier script>`: give it a python shim
    shim = rig["tmp"] / "tier_shim.py"
    shim.write_text(textwrap.dedent(f"""
        import os, subprocess, sys
        sys.exit(subprocess.call([{str(rig['tmp'] / 'bin' / 'tier_stub')!r}, *sys.argv[1:]]))
    """))
    e["LEGBA_DEPLOY_TIER_SCRIPT"] = str(shim)
    cmd = ["bash", str(rig["repo"] / "scripts" / "rolling_deploy.sh"),
           "--nightly-log", str(rig["tmp"] / "none.log"), "--deploy-log", str(rig["log"]), *args]
    p = subprocess.run(cmd, cwd=rig["repo"], capture_output=True, text=True, timeout=120, env=e)
    return p, p.stdout + p.stderr


def _calls(rig, name):
    f = rig["tmp"] / f"{name}.calls"
    return f.read_text().splitlines() if f.exists() else []


def _log(rig):
    return [l for l in rig["log"].read_text().splitlines()] if rig["log"].exists() else []


def test_the_default_is_full_and_today_s_path(rig):
    p, out = _run(rig, "--dry-run", "--skip-build", tier_out=_tier_out("full", computed="t1"))
    assert p.returncode == 0, out
    assert "--requested full" in _calls(rig, "tier")[0]
    for svc in ("legba-registry", "legba-runtime-dapr", "legba-dapr-workflow-worker"):
        assert f"--force-recreate {svc}" in out
    assert "python -m legba.data.migrate" in out
    assert _calls(rig, "gate") == []  # the full default runs no gate of its own
    assert not rig["log"].exists()  # a dry run writes no deploy log


def test_a_failing_classifier_never_blocks_the_default_full_roll(rig):
    p, out = _run(rig, "--dry-run", "--skip-build", tier_out="boom", tier_rc=1)
    assert p.returncode == 0, out
    assert "tier classifier failed" in out and "--force-recreate legba-runtime-dapr" in out


def test_t3_registry_builds_and_recreates_the_registry_alone_and_logs_it(rig):
    p, out = _run(rig, "--tier", "t3", tier_out=_tier_out("t3", services="registry"))
    assert p.returncode == 0, out
    docker = "\n".join(_calls(rig, "docker"))
    assert "build legba-registry" in docker
    assert "legba-runtime-dapr" not in docker  # never built or recreated
    assert "--force-recreate legba-registry" in docker
    assert "legba.data.migrate" not in docker
    assert _calls(rig, "gate")[0].split()[0] == "t3"
    (line,) = _log(rig)
    assert "result=ok" in line and "tier_used=t3" in line and "services=registry" in line
    assert f"to={SHA}" in line and "gate=targeted" in line and "gate_result=PASS(7p/0f/0e)" in line
    assert "dry_run=0" in line and "forced=0" in line


def test_the_post_roll_prune_caps_the_cache_with_the_ceiling_flag(rig):
    """Roll 87: 8.8 GB of reclaimable build cache against the 4 GB cap and the
    prune reclaimed 0 B. Docker 29's prune lists both flags; ``--reserved-space``
    is a floor (it prunes nothing on its own), ``--max-used-space`` the cap."""
    p, out = _run(rig, "--tier", "t3", tier_out=_tier_out("t3", services="registry"))
    assert p.returncode == 0, out
    prunes = [c for c in _calls(rig, "docker") if c.startswith("builder prune -f")]
    assert prunes == ["builder prune -f --max-used-space 4GB"], prunes


def test_a_builder_without_the_ceiling_flag_keeps_the_old_spelling(rig):
    p, out = _run(rig, "--tier", "t3", tier_out=_tier_out("t3", services="registry"),
                  STUB_PRUNE_HELP="      --keep-storage bytes   Amount of disk space to keep for cache")
    assert p.returncode == 0, out
    prunes = [c for c in _calls(rig, "docker") if c.startswith("builder prune -f")]
    assert prunes == ["builder prune -f --keep-storage 4GB"], prunes


def test_t3_runtime_rebuilds_the_worker_after_the_runtime(rig):
    p, out = _run(rig, "--tier", "t3", tier_out=_tier_out("t3", services="runtime worker"))
    assert p.returncode == 0, out
    docker = _calls(rig, "docker")
    builds = [c for c in docker if " build " in f" {c} "]
    assert any("build legba-runtime-dapr" in c for c in builds)
    assert any("build --no-cache legba-dapr-workflow-worker" in c for c in builds)
    assert not any("legba-registry" in c for c in docker)


def test_t0_touches_no_container_and_records_the_sha(rig):
    p, out = _run(rig, "--tier", "t0", tier_out=_tier_out("t0"))
    assert p.returncode == 0, out
    assert not any(" up " in f" {c} " or " build " in f" {c} " for c in _calls(rig, "docker"))
    (line,) = _log(rig)
    assert "tier_used=t0" in line and "result=ok" in line and "services=-" in line


def test_t1_walks_only_the_changed_families_then_reads_the_sweep(rig):
    p, out = _run(rig, "--tier", "t1", tier_out=_tier_out("t1", walk="source"))
    assert p.returncode == 0, out
    docker = "\n".join(_calls(rig, "docker"))
    assert "bringup_register_descriptors.py --profile profiles/geopolitics --family source" in docker
    assert "--family analyst" not in docker
    assert "--force-recreate" not in docker and " build " not in f" {docker} "
    assert "staleness sweep seen" in out and "no descriptor parse failures" in out


def test_t1_stops_on_a_runtime_parse_failure(rig):
    p, out = _run(rig, "--tier", "t1", tier_out=_tier_out("t1", walk="analyst"), STUB_PARSE_FAIL="1")
    assert p.returncode == 11, out
    assert "result=aborted" in _log(rig)[0] and "exit=11" in _log(rig)[0]


def test_t2_builds_the_ui_and_restarts_caddy_only(rig):
    p, out = _run(rig, "--tier", "t2", tier_out=_tier_out("t2", ui="1"))
    assert p.returncode == 0, out
    docker = _calls(rig, "docker")
    assert any("build legba-ui-build" in c for c in docker)
    assert any("restart legba-caddy" in c for c in docker)
    assert not any("--force-recreate" in c for c in docker)


def test_a_refused_lower_tier_stops_before_anything_and_is_logged(rig):
    p, out = _run(rig, "--tier", "t0", tier_out=_tier_out("t3", computed="t3"), tier_rc=3)
    assert p.returncode == 9, out
    assert _calls(rig, "gate") == [] and not any(" build " in f" {c} " for c in _calls(rig, "docker"))
    (line,) = _log(rig)
    assert "result=refused" in line and "tier_requested=t0" in line


def test_a_forced_tier_is_passed_on_and_recorded_with_who(rig):
    p, out = _run(rig, "--tier", "t0", "--force-tier",
                  tier_out=_tier_out("t0", computed="t3", required="t3", forced="1",
                                     services="registry"))
    assert p.returncode == 0, out
    assert "--force-tier" in _calls(rig, "tier")[0]
    assert not any(" up " in f" {c} " for c in _calls(rig, "docker"))  # t0 does only t0's work
    (line,) = _log(rig)
    assert "forced=1" in line and "forced_by=operator" in line and "tier_required=t3" in line


def test_force_tier_needs_an_explicit_lower_tier(rig):
    p, out = _run(rig, "--tier", "auto", "--force-tier", tier_out=_tier_out("t0"))
    assert p.returncode == 1 and "--force-tier forces an explicit lower tier" in out


def test_a_failed_gate_stops_the_roll_before_any_build(rig):
    p, out = _run(rig, "--tier", "t3", tier_out=_tier_out("t3", services="registry"),
                  STUB_GATE_VERDICT="FAIL")
    assert p.returncode == 10, out
    assert not any(" build " in f" {c} " or " up " in f" {c} " for c in _calls(rig, "docker"))
    (line,) = _log(rig)
    assert "result=gate_fail" in line and "services=registry" in line and "tier_used=t3" in line


def test_auto_resolving_to_full_runs_the_full_gate_and_rides_the_walk_and_ui(rig):
    p, out = _run(rig, "--tier", "auto", "--dry-run", "--skip-build",
                  tier_out=_tier_out("full", computed="t3", walk="source", ui="1"))
    assert p.returncode == 0, out
    assert "gate: full for tier full" in out
    assert "--family source" in out and "build legba-ui-build" in out
    assert "python -m legba.data.migrate" in out


def test_a_bad_tier_value_is_a_usage_error(rig):
    p, out = _run(rig, "--tier", "t9")
    assert p.returncode == 1 and "--tier must be" in out


# ---------------------------------------------------------------------------
# THE GATE RUNNER — run_targeted_gate.sh
# ---------------------------------------------------------------------------
_PLAN_STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
out = args[args.index("--tests-out") + 1]
tier = args[args.index("--tier") + 1]
full = "1" if tier == "full" else "0"
tests = [] if full == "1" else json.loads(os.environ["STUB_TESTS"])
open(out, "w").write("".join(t + "\n" for t in tests))
print(f"GATE_FULL={full}")
print(f"GATE_UI={os.environ.get('STUB_UI', '0')}")
print("GATE_DESCRIPTORS=0")
print(f"GATE_N_TESTS={len(tests)}")
print("GATE_EST_S=120")
print("GATE_TIER_COMPUTED=t3")
print("GATE_FROM=" + "a" * 40)
print("GATE_TO=" + os.environ["STUB_TO"])
'''

_RUNNER_STUB = r'''#!/usr/bin/env bash
echo "$*" >> "$STUB_RUNNER_CALLS"
if [ "$1" = "--lint" ]; then echo "All checks passed!"; exit 0; fi
if [ -n "${STUB_FAIL:-}" ]; then echo "FAILED tests/x/test_a.py::test_one - boom"; echo "1 failed, 6 passed in 3.1s"; exit 1; fi
echo "7 passed in 3.1s"
'''

_WALK_STUB = r'''#!/usr/bin/env bash
echo "$* out=${LEGBA_WALK_OUT:-}" >> "$STUB_WALK_CALLS"
if [ -n "${STUB_WALK_FAIL:-}" ]; then echo "WALK_VERDICT FAIL (1)"; echo "  - longest main-thread task: 4200 ms"; exit 1; fi
echo "WALK_VERDICT PASS"
'''


@pytest.fixture
def gate(tmp_path):
    for tool in ("bash", "flock"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not available")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "run_targeted_gate.sh", repo / "scripts" / "run_targeted_gate.sh")
    (repo / "scripts" / "deploy_tier.py").write_text(_PLAN_STUB)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("runner", _RUNNER_STUB), ("git", _GIT_STUB),
                       ("docker", "#!/bin/sh\necho \"$*\" >> \"$STUB_DOCKER_CALLS\"\nexit 0\n"),
                       ("walk", _WALK_STUB)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("LEGBA_")},
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "LEGBA_GATE_RUNNER": str(bindir / "runner"),
        "LEGBA_TEST_LOCK_FILE": str(tmp_path / "lock"),
        "LEGBA_GATE_LOG_DIR": str(tmp_path / "logs"),
        "LEGBA_GATE_WALK": str(bindir / "walk"),
        "STUB_WALK_CALLS": str(tmp_path / "walk.calls"),
        "STUB_RUNNER_CALLS": str(tmp_path / "runner.calls"),
        "STUB_DOCKER_CALLS": str(tmp_path / "docker.calls"),
        "STUB_TESTS": json.dumps(["tests/data_pkg/test_flow_api.py", "tests/test_module_size_gate.py"]),
        "STUB_TO": SHA,
    }
    return {"repo": repo, "tmp": tmp_path, "env": env}


def _gate(g, tier="t3", *extra, **env):
    p = subprocess.run(["bash", str(g["repo"] / "scripts" / "run_targeted_gate.sh"), tier,
                        "a" * 40 + ".." + SHA, *extra],
                       cwd=g["repo"], capture_output=True, text=True, timeout=60,
                       env={**g["env"], **env})
    calls = (g["tmp"] / "runner.calls")
    return p, p.stdout + p.stderr, calls.read_text().splitlines() if calls.exists() else []


def _verdict(out: str) -> dict[str, str]:
    line = next(l for l in out.splitlines() if l.startswith("GATE_VERDICT "))
    return dict(t.split("=", 1) for t in line.split()[2:] if "=" in t)


def test_the_runner_runs_exactly_the_classifier_s_paths_ordered(gate):
    p, out, calls = _gate(gate)
    assert p.returncode == 0, out
    assert calls[0] == "--lint"
    assert calls[1] == "tests/data_pkg/test_flow_api.py tests/test_module_size_gate.py -p no:randomly -rfE"
    v = _verdict(out)
    assert v["verdict"] == "PASS" and v["tier"] == "t3" and v["sha"] == SHA
    assert v["targeted"] == "7p/0f/0e" and v["phases"] == "lint,targeted" and v["tests"] == "2"
    assert "ordered" not in v["phases"]  # never readable as a full-suite record


def test_a_failing_test_fails_the_gate(gate):
    p, out, _ = _gate(gate, STUB_FAIL="1")
    assert p.returncode == 1
    v = _verdict(out)
    assert v["verdict"] == "FAIL" and v["targeted"] == "6p/1f/0e" and "targeted" in v["reason"]


def test_the_full_tier_runs_the_whole_ordered_suite(gate):
    p, out, calls = _gate(gate, "full")
    assert p.returncode == 0, out
    assert calls[1] == "tests/ -p no:randomly -rfE"
    assert _verdict(out)["phases"] == "lint,full"


def test_a_checkout_not_at_the_range_tip_cannot_pass(gate):
    p, out, calls = _gate(gate, STUB_TO="d" * 40)
    assert p.returncode == 1 and "checkout_at_" in _verdict(out)["reason"]
    assert calls == []  # nothing ran against the wrong tree


def test_ui_changes_run_tsc_vitest_and_both_ratchets(gate):
    p, out, _ = _gate(gate, STUB_UI="1")
    assert p.returncode == 0, out
    docker = (gate["tmp"] / "docker.calls").read_text()
    assert "node:20-slim" in docker
    for step in ("npm run lint", "lint:tokens", "lint:absence", "npm run test"):
        assert step in docker
    assert _verdict(out)["ui"] == "ok"


def test_ui_changes_then_run_the_browser_walk_in_fixture_mode(gate):
    p, out, _ = _gate(gate, STUB_UI="1")
    assert p.returncode == 0, out
    walk = (gate["tmp"] / "walk.calls").read_text()
    assert walk.startswith("fixture --build out=") and walk.strip().endswith("/walk")
    v = _verdict(out)
    assert v["walk"] == "ok" and v["phases"] == "lint,targeted,ui,walk"


def test_a_failing_walk_fails_the_gate_and_names_the_breach(gate):
    p, out, _ = _gate(gate, STUB_UI="1", STUB_WALK_FAIL="1")
    assert p.returncode == 1
    v = _verdict(out)
    assert v["verdict"] == "FAIL" and v["walk"] == "fail" and "ui_walk" in v["reason"]
    assert "4200 ms" in out


def test_no_ui_change_runs_no_walk(gate):
    p, out, _ = _gate(gate)
    assert p.returncode == 0, out
    assert not (gate["tmp"] / "walk.calls").exists()
    assert _verdict(out)["walk"] == "-"


def test_dry_run_prints_the_selection_and_runs_nothing(gate):
    p, out, calls = _gate(gate, "t3", "--dry-run")
    assert p.returncode == 0 and calls == []
    assert "tests/data_pkg/test_flow_api.py" in out and "GATE_VERDICT" not in out
