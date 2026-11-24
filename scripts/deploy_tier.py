#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""deploy_tier.py — which deploy path a change needs (stdlib only).

Every change used to take the one path: the full ordered suite (~19,400 tests,
~29 min) and a rolling deploy that rebuilds three images and recreates
registry -> runtime -> worker, interrupting whatever production is running.
The operator's rule (2026-11-13): a threshold separates the full roll from an
intermediate one. This script draws that threshold from DATA (``PATH_RULES``
and ``BACKSTOPS`` below, reviewed by tests/data_pkg/test_deploy_tier.py), never
from judgement at roll time.

THE TIERS (highest one any changed path needs wins; actions are the UNION)

  t0    no deploy      docs, tests, host scripts cron runs from the checkout
                       (merging IS their install), files no image copies.
  t1    descriptors    registry-family descriptor YAML / LIFECYCLE / RETIRED
                       rows: the walker only; no build, no recreate.
  t2    UI only        legba-ui-v3/**: the UI build step; no backend restart.
  t3    one service    Python reached (import graph) by ONE service — the
                       registry, or the runtime (+ its FROM-runtime worker) —
                       off every protected path: rebuild + recreate only it.
  full  everything     migrations, shared schemas, runtime core, write paths,
                       Dockerfiles, deps, compose, and ANYTHING UNPLACED.

WHEN IN DOUBT IT IS FULL: an unknown path, a module no service reaches, a
deleted module, a missing deployed SHA, an unreadable revision -> full.

MODULE -> SERVICE is derived mechanically: an AST walk of every module under
src/ (and the profile prompt packages) at the TARGET revision, read straight
from git (never the working tree), from each service's console-script entry
point (pyproject [project.scripts]). Edges are every import statement,
function-level and TYPE_CHECKING ones included, plus every string literal that
names a module (importlib targets; an f-string prefix fans out to the whole
package), plus every dotted module a descriptor YAML names (the registry
resolves those at registration, the runtime binds them). Over-approximation is
the safe direction: it can only make a module look MORE shared. The graph is
cached per (src, tests, descriptors, profiles) tree hash.

THE DEPLOYED SHA: nothing recorded it before this script. rolling_deploy.sh now
appends one ``DEPLOY_ROLL`` line per roll to the deploy log
(``$LEGBA_DEPLOY_LOG``, default /var/log/legba_deploy.log); the newest
successful, non-dry line's ``to=`` is the live SHA. No such line -> full.

USAGE
  deploy_tier.py classify [--range A..B] [--format text|json|shell]
  deploy_tier.py decide --requested auto|t0|t1|t2|t3|full [--force-tier]
                        [--range A..B] [--format text|json|shell]
  deploy_tier.py gate-plan [--range A..B] [--tier T] [--tests-out FILE]
  deploy_tier.py services MODULE [MODULE...]   (debug: who imports it)
  deploy_tier.py deployed-sha
Common: --repo DIR, --to REV (default HEAD), --deploy-log P, --nightly-log P.
Exit: 0 ok; 1 usage/git error; 3 decide refused (a lower tier without
--force-tier).
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# THE RULES — data. Tier ranks: higher wins.
# ---------------------------------------------------------------------------
T0, T1, T2, T3, FULL = "t0", "t1", "t2", "t3", "full"
TIERS = (T0, T1, T2, T3, FULL)
RANK = {t: i for i, t in enumerate(TIERS)}

#: Rule kinds beyond a plain tier: GRAPH = the import graph decides (t3 or
#: full); DESCRIPTOR = the file's own ``identity.schema_uri`` decides (t1 or
#: full); TESTS = t0 whose gate is the file itself.
GRAPH, DESCRIPTOR, TESTS, PLAIN = "graph", "descriptor", "tests", "plain"

#: The services the rolling deploy owns, with the containers a change to each
#: rebuilds. The worker is ``FROM legba/legba-runtime-dapr``: a runtime change
#: rebuilds it after the runtime (trap 1), and a worker-only module still needs
#: the runtime image rebuilt first, so both map to the same pair.
ENTRY_POINTS = {
    "registry": "legba.data.registry.server",
    "runtime": "legba.runtime.dapr_host",
    "worker": "legba.runtime.dapr_workflow.worker",
    "mcp": "legba.ui.mcp_server",
}
SERVICE_CONTAINERS = {
    "registry": ("registry",),
    "runtime": ("runtime", "worker"),
    "worker": ("runtime", "worker"),
}
#: ``mcp`` is traced but never rolled (no rolling tier, full included, builds
#: legba-mcp): a module only it reaches cannot be placed, so it is full.

#: Ordered: the FIRST matching glob decides a path. ``**`` spans directories;
#: ``*`` stays inside one segment. (glob, tier-or-kind, reason)
PATH_RULES: tuple[tuple[str, str, str], ...] = (
    # --- full: what every image is built from -------------------------------
    ("docker/Dockerfile.*", FULL, "a Dockerfile: the image itself changes"),
    ("docker-compose*.yml", FULL, "compose: service wiring, env, profiles"),
    ("pyproject.toml", FULL, "dependencies and console-script entry points"),
    (".dockerignore", FULL, "what every image build context carries"),
    ("docker/Caddyfile", FULL, "the front door's routing for every surface"),
    ("docker/**", FULL, "config mounted into a service no tier restarts (nats, searxng)"),
    ("dapr/**", FULL, "daprd component config: read at sidecar start; recreate the sidecar by hand after the roll"),
    ("legba-media/**", FULL, "the media image is outside the rolling deploy; rebuild it by hand"),
    # --- full: migrations, shared schemas, the runtime core, store/sinks -----
    ("src/legba/data/migrations/**", FULL, "a migration"),
    ("src/legba/data/migrate.py", FULL, "the migration runner"),
    ("src/legba/data/schemas/**", FULL, "the shared schema layer (registry + runtime)"),
    ("src/legba/shared/**", FULL, "shared by every service"),
    ("src/legba/data/archive*.py", FULL, "archive / evidence-store code"),
    ("src/legba/data/postgres.py", FULL, "substrate client"),
    ("src/legba/data/nats.py", FULL, "substrate client"),
    ("src/legba/data/redis.py", FULL, "substrate client"),
    ("src/legba/data/qdrant.py", FULL, "substrate client"),
    ("src/legba/data/opensearch.py", FULL, "substrate client"),
    ("src/legba/data/config.py", FULL, "configuration every service reads"),
    ("src/legba/data/outputs/**", FULL, "output sinks"),
    # registry/server.py and api.py (router assembly) are left to the graph on
    # purpose: a new route edits one of them, they are registry-only, and the
    # registry-only t3 gate carries every slim-image probe.
    ("src/legba/data/registry/descriptor*.py", FULL, "registry core (descriptor store + refs)"),
    ("src/legba/data/registry/_deps.py", FULL, "registry core (dependencies)"),
    ("src/legba/data/registry/bearer_auth.py", FULL, "registry core (auth)"),
    ("src/legba/data/registry/auth_state.py", FULL, "registry core (auth)"),
    ("src/legba/data/registry/route_roles.py", FULL, "registry core (auth)"),
    ("src/legba/data/registry/rate_limit.py", FULL, "registry core (rate limit)"),
    ("src/legba/runtime/dapr_host.py", FULL, "runtime core (entry point)"),
    ("src/legba/runtime/dapr_actors.py", FULL, "runtime core (actors)"),
    ("src/legba/runtime/dapr_cron.py", FULL, "runtime core (reminders)"),
    ("src/legba/runtime/actor_*.py", FULL, "runtime core (actor turn)"),
    ("src/legba/runtime/source_*.py", FULL, "runtime core (source actor / ingest)"),
    ("src/legba/runtime/pipeline.py", FULL, "runtime core (ingest pipeline)"),
    ("src/legba/runtime/reconcile.py", FULL, "runtime core (reconcile loop)"),
    ("src/legba/runtime/lifecycle.py", FULL, "runtime core (lifecycle)"),
    ("src/legba/runtime/state.py", FULL, "runtime core (state)"),
    ("src/legba/runtime/deps.py", FULL, "runtime core (deps)"),
    ("src/legba/runtime/analyst_deps_*.py", FULL, "runtime core (analyst deps)"),
    ("src/legba/runtime/analyst_method.py", FULL, "runtime core (analyst method)"),
    ("src/legba/runtime/leader.py", FULL, "runtime core (leader lease)"),
    ("src/legba/runtime/inbound_drain.py", FULL, "runtime core (inbound drain)"),
    ("src/legba/runtime/shutdown_drain.py", FULL, "runtime core (shutdown drain)"),
    ("src/legba/runtime/registry_client.py", FULL, "runtime core (registry client)"),
    ("src/legba/runtime/dispatched_question.py", FULL, "runtime core (dispatcher)"),
    ("src/legba/runtime/subscription/**", FULL, "runtime core (dispatcher / subscriptions)"),
    ("src/legba/runtime/triggers/**", FULL, "runtime core (triggers)"),
    ("src/legba/runtime/jobs/**", FULL, "runtime core (job workers)"),
    ("src/legba/runtime/dapr_workflow/**", FULL, "runtime core (workflows)"),
    # --- the import graph decides the rest of the Python --------------------
    ("src/**.py", GRAPH, "Python: the import graph decides"),
    ("src/**", FULL, "package data under src/ (its readers cannot be traced)"),
    # --- profiles ------------------------------------------------------------
    ("profiles/*/rosters/source_catalog.yaml", DESCRIPTOR, "the source catalog the walker registers"),
    ("profiles/*/prompts/**", FULL, "prompt package installed into BOTH registry and runtime images"),
    ("profiles/**", FULL, "profile data: generators feed src/ and both image builds"),
    # --- descriptors ---------------------------------------------------------
    ("descriptors/LIFECYCLE.md", DESCRIPTOR, "host lifecycle overlay the walker applies"),
    ("descriptors/RETIRED.md", DESCRIPTOR, "retirement rows the walker applies"),
    ("descriptors/LIFECYCLE.d/*.md", DESCRIPTOR, "one host lifecycle row the walker applies (R-1)"),
    ("descriptors/examples/**", T0, "example descriptors, never registered"),
    ("descriptors/*.yaml", DESCRIPTOR, "a descriptor: its schema_uri family decides"),
    ("descriptors/**", FULL, "unrecognised descriptor-tree file"),
    # --- UI ------------------------------------------------------------------
    ("legba-ui-v3/**", T2, "UI (built by legba-ui-build, served by caddy)"),
    # --- t0 ------------------------------------------------------------------
    ("tests/**", TESTS, "tests: the gate runs them; nothing deploys"),
    ("docs/**", T0, "documentation"),
    ("planning/**", T0, "planning documents"),
    ("legba-models/**", T0, "models-host code: deployed on the models host by hand, never by this roll"),
    ("deploy/cron.d/**", T0, "cron entry: installed into /etc/cron.d by hand; its scripts run from the checkout"),
    ("deploy/**", FULL, "bring-up / infrastructure config"),
    ("scripts/**", T0, "host script: runs from the checkout, so merging installs it"),
    ("release/**", T0, "release manifests"),
    ("*.md", T0, "top-level documentation"),
    ("LICENSE", T0, "licence text"),
    ("*.png", T0, "top-level image"),
    (".gitignore", T0, "git metadata"),
)

#: The descriptor families the walker (scripts/bringup_register_descriptors.py)
#: owns; vocabulary seed files ride every walk. Any other family is registered
#: by its own tool and so has no t1 path.
WALKED_FAMILIES = ("action_pack", "source", "target", "analyst")
WALK_RIDERS = ("vocabulary",)

#: Python modules whose source text carries one of these is a WRITE PATH
#: (SQL DDL/DML or a bus publish) and goes full whatever service reaches it.
WRITE_PATH_RE = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+[a-z_][a-z0-9_.]*\s+SET|DELETE\s+FROM|TRUNCATE\b|"
    r"ALTER\s+TABLE|CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|VIEW|MATERIALIZED))|\.publish\(",
    re.IGNORECASE,
)

#: THE GUARD SET — every tier's gate runs these (plus ruff over the tree).
GUARD_TESTS = (
    "tests/test_module_size_gate.py",
    "tests/test_no_undeclared_stubs.py",
    "tests/test_runtime_no_dspy_litellm.py",
    "tests/data_pkg/test_taxon_families.py",
    "tests/data_pkg/test_access_class_taxonomy.py",
    "tests/data_pkg/test_source_class_taxonomy.py",
    "tests/data_pkg/alerts/test_alert_topics_x4.py",
    "tests/data_pkg/test_option_catalog_imports.py",
    "tests/data_pkg/test_option_catalog_leaf.py",
    "tests/data_pkg/test_handler_options_catalog_split.py",
    "tests/data_pkg/test_handler_options_import_cycle.py",
    "tests/data_pkg/test_collection_slim_imports.py",
    "tests/data_pkg/test_belief_api_imports.py",
    "tests/data_pkg/test_graph_triggers_imports.py",
    "tests/data_pkg/test_deploy_tier.py",
)
#: When registry code changes, every slim-image import probe joins the gate:
#: a test file naming "slim" that poisons modules in a subprocess (derived from
#: the tree, so a new probe joins without editing this list).
SLIM_PROBE_RES = (re.compile(r"slim"), re.compile(r"sys\.modules\[[^\]]+\]\s*=\s*None|poison"))
#: t1's fixed gate: the walker over the whole tree into the test DB (every
#: descriptor validated by the real registry), the profile, the bring-up
#: accounting, reference resolution, the catalog, the descriptor test dir.
T1_TESTS = (
    "tests/data_pkg/test_bringup_register_descriptors.py",
    "tests/data_pkg/test_descriptor_reference_resolution_k3.py",
    "tests/data_pkg/test_source_catalog_bringup.py",
    "tests/test_profile.py",
    "tests/test_bringup_accounts_for_live_heads.py",
    "tests/test_p17_registrar_fsm_path.py",
    "tests/descriptors/",
)

#: THE BACKSTOPS — data too. ``decide`` applies them to every roll.
BACKSTOPS = {
    # The newest clean full-suite nightly record on this lineage is FAIL ->
    # nothing below full until a full gate passes.
    "nightly_fail_forces_full": True,
    # After this many consecutive lower-tier rolls the next one is full.
    "max_consecutive_lower": 5,
    # More than this many hours since the last PASSING full gate -> full.
    "max_hours_since_full_gate": 24,
    # A t3 roll touching a service whose t3 gate FAILED since the last full
    # roll -> full (the targeted gate already missed something there once).
    "t3_repeat_after_gate_fail": True,
}

#: Gate-time model (measured: nightly ordered phase, 2026-11-12, 19,154 tests
#: in 1,700 s incl. an 85 s collection of 19,458). Per test + fixed overhead
#: (test-stack check, pivot-DB provision, collection floor, ruff).
SECONDS_PER_TEST = 0.089
GATE_FIXED_SECONDS = 75
UI_GATE_SECONDS = 450  # tsc + vitest + two lint ratchets in node:20-slim (~150 s), then the bundle build and the fixture browser walk (~300 s)
#: ``def test_`` count -> collected count (parametrisation), measured 11-13.
COLLECT_FACTOR = 1.34
FULL_SUITE_SECONDS = 1700

DEFAULT_DEPLOY_LOG = os.environ.get("LEGBA_DEPLOY_LOG", "/var/log/legba_deploy.log")
DEFAULT_NIGHTLY_LOG = os.environ.get("LEGBA_NIGHTLY_SELF_LOG", "/var/log/legba_nightly_suite.log")
CACHE_DIR = Path(os.environ.get("LEGBA_DEPLOY_TIER_CACHE",
                                Path.home() / ".cache" / "legba-deploy-tier"))
GRAPH_VERSION = "1"


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, repo: Path):
        self.repo = repo

    def run(self, *args: str, check: bool = True) -> str:
        p = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)
        if check and p.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {p.stderr.strip() or p.returncode}")
        return p.stdout

    def sha(self, rev: str) -> str:
        return self.run("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()

    def tree(self, rev: str, path: str) -> str:
        out = self.run("rev-parse", "--verify", "--quiet", f"{rev}:{path}", check=False).strip()
        return out or "-"

    def is_ancestor(self, a: str, b: str) -> bool:
        p = subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", a, b],
                           capture_output=True, text=True)
        return p.returncode == 0

    def changed(self, a: str, b: str) -> list[tuple[str, str]]:
        """``[(status, path)]`` — renames split into a delete and an add."""
        out = self.run("diff", "--name-status", "--no-renames", "-z", a, b)
        parts = out.split("\0")
        res = []
        for i in range(0, len(parts) - 1, 2):
            res.append((parts[i][:1], parts[i + 1]))
        return res

    def ls(self, rev: str, *paths: str) -> list[str]:
        out = self.run("ls-tree", "-r", "-z", "--name-only", rev, "--", *paths)
        return [p for p in out.split("\0") if p]

    def read_many(self, rev: str, paths: list[str]) -> dict[str, str]:
        if not paths:
            return {}
        payload = "".join(f"{rev}:{p}\n" for p in paths).encode()
        p = subprocess.run(["git", "-C", str(self.repo), "cat-file", "--batch"],
                           input=payload, capture_output=True)
        if p.returncode != 0:
            raise GitError(f"git cat-file --batch: {p.stderr.decode(errors='replace')}")
        out, data, i = {}, p.stdout, 0
        for path in paths:
            nl = data.index(b"\n", i)
            header = data[i:nl].decode()
            i = nl + 1
            if header.endswith(" missing"):
                continue
            size = int(header.split()[2])
            out[path] = data[i:i + size].decode("utf-8", errors="replace")
            i += size + 1
        return out

    def read(self, rev: str, path: str) -> str | None:
        return self.read_many(rev, [path]).get(path)

    def grep_files(self, rev: str, needles: list[str], *paths: str) -> set[str]:
        """Files under ``paths`` at ``rev`` whose text contains any needle (fixed strings)."""
        if not needles:
            return set()
        args = ["grep", "-l", "-F"]
        for n in needles:
            args += ["-e", n]
        args += [rev, "--", *paths]
        out = self.run(*args, check=False)
        return {line.split(":", 1)[1] for line in out.splitlines() if ":" in line}


# ---------------------------------------------------------------------------
# path rules
# ---------------------------------------------------------------------------
def _glob_re(glob: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(f"^{out}$")


_RULE_RES = [(_glob_re(g), g, k, r) for g, k, r in PATH_RULES]


def match_rule(path: str) -> tuple[str, str, str] | None:
    """``(glob, kind, reason)`` of the first rule matching ``path``; None = unknown."""
    for rx, g, kind, reason in _RULE_RES:
        if rx.match(path):
            return g, kind, reason
    return None


# ---------------------------------------------------------------------------
# the import graph
# ---------------------------------------------------------------------------
_MOD_STR_RE = re.compile(r"^(legba[A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)(?::[A-Za-z_][A-Za-z0-9_.]*)?$")
_MOD_IN_TEXT_RE = re.compile(r"\b(legba[A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)")


def module_of(path: str) -> str | None:
    """``src/legba/a/b.py`` -> ``legba.a.b``; profile prompt packages likewise."""
    m = re.match(r"^(?:src|profiles/[^/]+/prompts)/(.+)\.py$", path)
    if not m:
        return None
    parts = m.group(1).split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def _resolve(name: str, modules: set[str]) -> str | None:
    """Longest prefix of a dotted name that is a known module."""
    parts = name.split(".")
    while parts:
        cand = ".".join(parts)
        if cand in modules:
            return cand
        parts.pop()
    return None


def _imports_of(mod: str, is_pkg: bool, text: str, modules: set[str]) -> set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {"<syntax-error>"}
    pkg = mod if is_pkg else mod.rpartition(".")[0]
    deps: set[str] = set()

    def add(name: str) -> None:
        r = _resolve(name, modules)
        if r:
            deps.add(r)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".")
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)]
                base_name = ".".join(base)
                target = f"{base_name}.{node.module}" if node.module else base_name
            else:
                target = node.module or ""
            add(target)
            for a in node.names:
                if f"{target}.{a.name}" in modules:
                    deps.add(f"{target}.{a.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            m = _MOD_STR_RE.match(node.value.strip())
            if m:
                add(m.group(1))
        elif isinstance(node, ast.JoinedStr) and node.values:
            head = node.values[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                prefix = head.value.strip().rstrip(".")
                if re.match(r"^legba[A-Za-z0-9_]*(\.[A-Za-z_]\w*)*$", prefix) and prefix in modules:
                    deps.update(m for m in modules if m == prefix or m.startswith(prefix + "."))
    # importing a.b.c runs a and a.b first
    parts = mod.split(".")
    for i in range(1, len(parts)):
        p = ".".join(parts[:i])
        if p in modules:
            deps.add(p)
    deps.discard(mod)
    return deps


@dataclass
class Graph:
    modules: dict[str, str]                 # module -> path
    deps: dict[str, list[str]]              # module -> imported modules
    reach: dict[str, list[str]]             # service -> modules it reaches
    write_paths: list[str]                  # modules carrying a write
    test_deps: dict[str, list[str]]         # test/conftest file -> legba modules (transitive)
    test_script_imports: dict[str, list[str]]  # test file -> scripts/ module names imported
    test_counts: dict[str, int]             # test file -> estimated collected tests
    descriptor_roots: list[str] = field(default_factory=list)

    def services_of(self, mod: str) -> list[str]:
        return sorted(s for s, mods in self.reach.items() if mod in set(mods))


def _closure(roots: set[str], deps: dict[str, list[str]]) -> set[str]:
    seen, todo = set(), list(roots)
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        todo.extend(deps.get(m, ()))
    return seen


def build_graph(git: Git, rev: str, *, use_cache: bool = True) -> Graph:
    key_src = "|".join([GRAPH_VERSION, git.tree(rev, "src"), git.tree(rev, "tests"),
                        git.tree(rev, "descriptors"), git.tree(rev, "profiles"),
                        git.tree(rev, "scripts")])
    key = hashlib.sha256(key_src.encode()).hexdigest()[:32]
    cache = CACHE_DIR / f"graph-{key}.json"
    if use_cache and cache.exists():
        try:
            return Graph(**json.loads(cache.read_text()))
        except (OSError, ValueError, TypeError):
            pass

    files = git.ls(rev, "src", "profiles", "tests", "descriptors", "scripts")
    py_src = [p for p in files if p.endswith(".py") and module_of(p)]
    texts = git.read_many(rev, py_src)
    modules = {module_of(p): p for p in py_src}  # type: ignore[misc]
    modset = set(modules)
    deps = {m: sorted(_imports_of(m, p.endswith("__init__.py"), texts.get(p, ""), modset))
            for m, p in modules.items()}
    write_paths = sorted(m for m, p in modules.items() if WRITE_PATH_RE.search(texts.get(p, "")))

    # Descriptor-named modules: the registry resolves them, the runtime binds them.
    ydocs = [p for p in files if p.startswith("descriptors/") and p.endswith(".yaml")]
    ydocs += [p for p in files if re.match(r"^profiles/[^/]+/.*\.ya?ml$", p)]
    droots: set[str] = set()
    for text in git.read_many(rev, ydocs).values():
        for m in _MOD_IN_TEXT_RE.findall(text):
            r = _resolve(m, modset)
            if r:
                droots.add(r)
    prompt_pkgs = {m for m, p in modules.items() if p.startswith("profiles/")}

    reach: dict[str, list[str]] = {}
    for svc, entry in ENTRY_POINTS.items():
        roots = {entry} if entry in modset else set()
        if svc in ("registry", "runtime"):
            roots |= droots | prompt_pkgs
        reach[svc] = sorted(_closure(roots, deps))

    # Tests: direct imports (legba modules + scripts/ modules) -> closure.
    tfiles = [p for p in files if p.startswith("tests/") and p.endswith(".py")]
    ttexts = git.read_many(rev, tfiles)
    script_mods = {Path(p).stem for p in files if p.startswith("scripts/") and p.endswith(".py")}
    test_deps, test_scripts, test_counts = {}, {}, {}
    for p in tfiles:
        text = ttexts.get(p, "")
        direct = _imports_of("tests._x", False, text, modset)
        direct.discard("<syntax-error>")
        test_deps[p] = sorted(_closure(direct, deps))
        names: set[str] = set()
        try:
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Import):
                    names.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    names.add(node.module.split(".")[0])
        except SyntaxError:
            pass
        test_scripts[p] = sorted(names & script_mods)
        if Path(p).name.startswith("test_"):
            n = len(re.findall(r"^\s*(?:async\s+)?def test_", text, re.M))
            test_counts[p] = max(1, round(n * COLLECT_FACTOR))
    g = Graph(modules=modules, deps=deps, reach=reach, write_paths=write_paths,
              test_deps=test_deps, test_script_imports=test_scripts,
              test_counts=test_counts, descriptor_roots=sorted(droots))
    if use_cache:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(g.__dict__))
            tmp.replace(cache)
        except OSError:
            pass
    return g


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------
@dataclass
class PathVerdict:
    path: str
    status: str
    tier: str
    reason: str
    services: list[str] = field(default_factory=list)       # containers to rebuild
    walk_families: list[str] = field(default_factory=list)
    ui: bool = False
    tests: list[str] = field(default_factory=list)


@dataclass
class Plan:
    from_sha: str | None
    to_sha: str
    tier: str
    paths: list[PathVerdict]
    services: list[str]
    walk_families: list[str]
    ui: bool
    migrate: bool
    restart: bool
    tests: list[str]
    full_gate: bool
    descriptor_check: bool
    est_gate_seconds: int
    note: str = ""
    est_targeted_seconds: int = 0


_FAMILY_RE = re.compile(r"schema_uri:\s*['\"]?legba/([a-z_]+)/", re.M)


def _descriptor_verdict(git: Git, rev_new: str, rev_old: str | None, path: str,
                        status: str, base: PathVerdict) -> PathVerdict:
    if path.endswith(".md"):
        base.tier, base.walk_families = T1, list(WALKED_FAMILIES)
        return base
    if path.startswith("profiles/"):
        base.tier, base.walk_families = T1, ["source"]
        return base
    text = git.read(rev_new, path) if status != "D" else None
    if text is None and rev_old:
        text = git.read(rev_old, path)
    m = _FAMILY_RE.search(text or "")
    fam = m.group(1) if m else None
    if fam in WALKED_FAMILIES:
        base.tier, base.walk_families = T1, [fam]
        base.reason = f"{fam} descriptor (walked)"
        did = re.search(r"^\s*id:\s*['\"]?([A-Za-z0-9_.\-]+)", text or "", re.M)
        base.tests = sorted(git.grep_files(rev_new, [Path(path).name] + ([did.group(1)] if did else []),
                                           "tests"))
    elif fam in WALK_RIDERS:
        base.tier, base.walk_families = T1, list(WALKED_FAMILIES)
        base.reason = f"{fam} seed file (seeded by every walk)"
    else:
        base.tier = FULL
        base.reason = (f"{fam or 'unparseable'} descriptor: not walked by "
                       "bringup_register_descriptors.py (its own registrar) — no t1 path")
    return base


def _graph_verdict(g: Graph, git: Git, rev: str, path: str, status: str,
                   base: PathVerdict) -> PathVerdict:
    mod = module_of(path)
    if status == "D" or mod not in g.modules:
        base.tier, base.reason = FULL, "module removed: its old importers are not re-traced"
        return base
    all_svcs = g.services_of(mod)
    svcs = [s for s in all_svcs if s in SERVICE_CONTAINERS]
    containers = sorted({c for s in svcs for c in SERVICE_CONTAINERS[s]})
    base.services = containers
    if not svcs:
        base.tier = FULL
        base.reason = (f"reached only by {','.join(all_svcs)}, which no roll builds (cannot place)"
                       if all_svcs else "no service's import graph reaches it (cannot place)")
    elif mod in set(g.write_paths):
        base.tier, base.reason = FULL, f"write path (SQL write / bus publish) reached by {','.join(svcs)}"
    elif "registry" in svcs and len(containers) > 1:
        base.tier, base.reason = FULL, f"shared: imported by {','.join(svcs)}"
    else:
        base.tier = T3
        base.reason = f"leaf of {','.join(svcs)} only"
    # tests: transitive importers + any test naming the module or file
    tests = {t for t, ds in g.test_deps.items() if mod in set(ds)}
    tests |= git.grep_files(rev, [mod, path.removeprefix("src/")], "tests")
    base.tests = sorted(tests)
    return base


def _script_tests(g: Graph, git: Git, rev: str, path: str) -> list[str]:
    name = Path(path).name
    stem = Path(path).stem
    tests = {t for t, ss in g.test_script_imports.items() if stem in ss}
    tests |= git.grep_files(rev, [name], "tests")
    return sorted(tests)


def _expand_conftests(tests: set[str]) -> set[str]:
    """A conftest selects its whole directory (pytest collects it per dir)."""
    out = set()
    for t in tests:
        p = Path(t)
        if p.name == "conftest.py":
            out.add(str(p.parent) + "/")
        elif p.name.startswith("test_") and p.suffix == ".py":
            out.add(t)
        elif t.endswith("/"):
            out.add(t)
        # helper modules (tests/_x.py, fixtures): their importers are selected via the graph
    dirs = sorted(t for t in out if t.endswith("/"))
    return {t for t in out
            if not any(t != d and t.startswith(d) for d in dirs)}


def _slim_probes(git: Git, rev: str) -> list[str]:
    a = git.grep_files(rev, ["slim"], "tests")
    out = []
    texts = git.read_many(rev, sorted(a))
    for p, t in texts.items():
        if Path(p).name.startswith("test_") and all(rx.search(t) for rx in SLIM_PROBE_RES):
            out.append(p)
    return sorted(out)


def estimate_seconds(tests: list[str], g: Graph | None, ui: bool, full: bool) -> int:
    if full:
        return FULL_SUITE_SECONDS + (UI_GATE_SECONDS if ui else 0)
    n = 0
    for t in tests:
        if g is None:
            n += 10
        elif t.endswith("/"):
            n += sum(c for f, c in g.test_counts.items() if f.startswith(t))
        else:
            n += g.test_counts.get(t, 10)
    py = min(GATE_FIXED_SECONDS + n * SECONDS_PER_TEST, FULL_SUITE_SECONDS)
    return int(py + (UI_GATE_SECONDS if ui else 0))


def classify(git: Git, from_rev: str | None, to_rev: str, *, use_cache: bool = True) -> Plan:
    to_sha = git.sha(to_rev)
    if not to_sha:
        raise GitError(f"cannot resolve {to_rev}")
    from_sha = git.sha(from_rev) if from_rev else None
    if not from_sha:
        why = (f"cannot resolve the deployed revision {from_rev!r}" if from_rev
               else "no deployed SHA on record (the deploy log has no successful roll)")
        return Plan(None, to_sha, FULL, [PathVerdict("*", "?", FULL, why)],
                    ["registry", "runtime", "worker"], [], False, True, True,
                    sorted(GUARD_TESTS), True, False, FULL_SUITE_SECONDS, note=why,
                    est_targeted_seconds=estimate_seconds(sorted(GUARD_TESTS), None, False, False))
    changes = git.changed(from_sha, to_sha)
    # The graph (cached per tree hash) places Python, finds script/helper
    # importers among the tests and sizes the gate.
    g = build_graph(git, to_sha, use_cache=use_cache) if changes else None
    verdicts: list[PathVerdict] = []
    for status, path in changes:
        rule = match_rule(path)
        if rule is None:
            verdicts.append(PathVerdict(path, status, FULL, "unknown path (no rule): full by default"))
            continue
        glob, kind, reason = rule
        v = PathVerdict(path, status, kind if kind in TIERS else T0, reason)
        if kind == GRAPH:
            v = _graph_verdict(g, git, to_sha, path, status, v)  # type: ignore[arg-type]
        elif kind == DESCRIPTOR:
            v = _descriptor_verdict(git, to_sha, from_sha, path, status, v)
        elif kind == TESTS:
            v.tier = T0
            v.tests = [path] if status != "D" else []
            if Path(path).name == "conftest.py":
                v.tests = [str(Path(path).parent) + "/"] if status != "D" else []
            elif not Path(path).name.startswith("test_") and g is not None and status != "D":
                # a test helper: every test importing it (by module name or file name)
                stem = Path(path).stem
                v.tests = sorted(git.grep_files(to_sha, [stem], "tests"))
        elif kind == T2:
            v.ui = True
        elif path.startswith("scripts/") and g is not None:
            v.tests = _script_tests(g, git, to_sha, path)
        verdicts.append(v)

    tier = max((v.tier for v in verdicts), key=RANK.__getitem__, default=T0)
    services = sorted({s for v in verdicts for s in v.services})
    walk = sorted({f for v in verdicts for f in v.walk_families},
                  key=lambda f: WALKED_FAMILIES.index(f))
    ui = any(v.ui for v in verdicts)
    full = tier == FULL
    if full:
        services = ["registry", "runtime", "worker"]
    tests: set[str] = set()
    for v in verdicts:
        tests.update(v.tests)
    # The targeted selection is computed even for a full plan: a FORCED lower
    # tier is gated by it (gate-plan --tier), never by nothing.
    tests.update(GUARD_TESTS)
    if walk:
        tests.update(T1_TESTS)
    if "registry" in services:
        tests.update(_slim_probes(git, to_sha))
    # A selected file the target revision deleted cannot run.
    present = set(git.ls(to_sha, "tests"))
    tests = {t for t in _expand_conftests(tests) if t.endswith("/") or t in present}
    tl = sorted(tests)
    targeted = estimate_seconds(tl, g, ui, False)
    return Plan(from_sha, to_sha, tier, verdicts, services, walk, ui, full,
                restart=bool(services) or full, tests=tl, full_gate=full,
                descriptor_check=bool(walk),
                est_gate_seconds=estimate_seconds(tl, g, ui, True) if full else targeted,
                est_targeted_seconds=targeted)


# ---------------------------------------------------------------------------
# ledgers: the deploy log and the nightly verdicts
# ---------------------------------------------------------------------------
def _fields(line: str) -> dict[str, str]:
    out = {}
    for tok in line.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            out.setdefault(k, v)
    parts = line.split()
    if len(parts) > 1:
        out.setdefault("_ts", parts[1])
    return out


def _ts(s: str) -> float | None:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def read_deploy_log(path: str) -> list[dict[str, str]]:
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    return [_fields(l) for l in lines if l.startswith("DEPLOY_ROLL ")]


def deployed_sha(path: str) -> str | None:
    for rec in reversed(read_deploy_log(path)):
        if rec.get("result") == "ok" and rec.get("dry_run") == "0" and rec.get("to"):
            return rec["to"]
    return None


def newest_nightly(git: Git, path: str, rolled: str) -> dict[str, str] | None:
    """The roll gate's record: newest ordered, clean-tree verdict on rolled or an ancestor."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.startswith("NIGHTLY_VERDICT "):
            continue
        f = _fields(line)
        if "ordered" not in f.get("phases", "").split(",") or f.get("dirty") != "0" or not f.get("sha"):
            continue
        if f["sha"] == rolled or git.is_ancestor(f["sha"], rolled):
            return f
    return None


def last_full_gate_pass(git: Git, nightly_log: str, deploy_log: list[dict[str, str]],
                        rolled: str) -> float | None:
    """Newest PASS of a full gate: a clean ordered nightly PASS on this lineage, or
    a deploy-log roll whose gate was the full suite and passed."""
    best = None
    try:
        lines = Path(nightly_log).read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        if not line.startswith("NIGHTLY_VERDICT "):
            continue
        f = _fields(line)
        if (f.get("verdict") == "PASS" and "ordered" in f.get("phases", "").split(",")
                and f.get("dirty") == "0" and f.get("sha")
                and (f["sha"] == rolled or git.is_ancestor(f["sha"], rolled))):
            t = _ts(f.get("_ts", ""))
            best = max(best or 0, t or 0) or best
    for rec in deploy_log:
        if rec.get("gate") == "full" and rec.get("gate_result", "").startswith("PASS"):
            t = _ts(rec.get("_ts", ""))
            if t:
                best = max(best or 0, t)
    return best


@dataclass
class Decision:
    requested: str
    computed: str
    required: str
    used: str
    forced: bool
    refused: bool
    backstops: list[str]
    message: str


def apply_backstops(computed: str, *, git: Git, rolled: str, deploy_log_path: str,
                    nightly_log_path: str, services: list[str], now: float,
                    rules: dict = BACKSTOPS) -> tuple[str, list[str]]:
    if computed == FULL:
        return FULL, []
    hits: list[str] = []
    log = [r for r in read_deploy_log(deploy_log_path) if r.get("dry_run") == "0"]
    if rules.get("nightly_fail_forces_full"):
        rec = newest_nightly(git, nightly_log_path, rolled)
        fail_at = _ts(rec.get("_ts", "")) if rec else None
        cleared = rec is not None and fail_at is not None and any(
            r.get("gate") == "full" and r.get("gate_result", "").startswith("PASS")
            and (_ts(r.get("_ts", "")) or 0) > fail_at and r.get("to")
            and (r["to"] == rolled or git.is_ancestor(r["to"], rolled))
            for r in log)
        if rec and rec.get("verdict") != "PASS" and not cleared:
            hits.append(f"nightly: the newest clean full-suite record ({rec.get('sha', '')[:12]}) is "
                        f"{rec.get('verdict') or 'unparsable'} — nothing below full until a full gate passes")
    n_max = rules.get("max_consecutive_lower")
    if n_max:
        streak = 0
        for rec in reversed(log):
            if rec.get("result") != "ok":
                continue
            if rec.get("tier_used") == FULL:
                break
            streak += 1
        if streak >= n_max:
            hits.append(f"streak: {streak} consecutive lower-tier rolls (max {n_max}) — this one is full")
    h_max = rules.get("max_hours_since_full_gate")
    if h_max:
        last = last_full_gate_pass(git, nightly_log_path, log, rolled)
        if last is None:
            hits.append(f"age: no passing full gate on record for this lineage (max {h_max} h)")
        elif now - last > h_max * 3600:
            hits.append(f"age: the last passing full gate is {(now - last) / 3600:.1f} h old (max {h_max} h)")
    if rules.get("t3_repeat_after_gate_fail") and computed == T3:
        for rec in reversed(log):
            if rec.get("result") == "ok" and rec.get("tier_used") == FULL:
                break
            if rec.get("result") == "gate_fail" and rec.get("tier_used") == T3:
                failed = set(filter(None, rec.get("services", "").split(",")))
                if failed & set(services):
                    hits.append(f"repeat: a t3 gate failed on {','.join(sorted(failed & set(services)))} "
                                "since the last full roll — the repeat is full")
                    break
    return (FULL if hits else computed), hits


def decide(requested: str, plan: Plan, *, force: bool, git: Git, deploy_log_path: str,
           nightly_log_path: str, now: float | None = None) -> Decision:
    now = time.time() if now is None else now
    required, hits = apply_backstops(plan.tier, git=git, rolled=plan.to_sha,
                                     deploy_log_path=deploy_log_path,
                                     nightly_log_path=nightly_log_path,
                                     services=plan.services, now=now)
    if requested == "auto":
        return Decision(requested, plan.tier, required, required, False, False, hits,
                        f"auto: tier {required}" + (f" (escalated from {plan.tier})" if hits else ""))
    if requested not in TIERS:
        raise ValueError(f"unknown tier {requested!r}")
    if RANK[requested] >= RANK[required]:
        return Decision(requested, plan.tier, required, requested, False, False, hits,
                        f"explicit tier {requested} (computed {plan.tier}, required {required})")
    if force:
        return Decision(requested, plan.tier, required, requested, True, False, hits,
                        f"FORCED tier {requested} below the required {required} (--force-tier; recorded)")
    return Decision(requested, plan.tier, required, required, False, True, hits,
                    f"REFUSED: tier {requested} is below the required {required}; pass --force-tier "
                    "to roll it anyway (recorded in the deploy log)")


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
def _plan_dict(plan: Plan) -> dict:
    d = dict(plan.__dict__)
    d["paths"] = [v.__dict__ for v in plan.paths]
    return d


def print_plan(plan: Plan, fmt: str, decision: Decision | None = None, out=sys.stdout) -> None:
    if fmt == "json":
        d = _plan_dict(plan)
        if decision:
            d["decision"] = decision.__dict__
        json.dump(d, out, indent=2)
        out.write("\n")
        return
    if fmt == "shell":
        kv = {
            "TIER_COMPUTED": plan.tier,
            "TIER_FROM": plan.from_sha or "",
            "TIER_TO": plan.to_sha,
            "TIER_SERVICES": " ".join(plan.services),
            "TIER_WALK_FAMILIES": " ".join(plan.walk_families),
            "TIER_UI": "1" if plan.ui else "0",
            "TIER_MIGRATE": "1" if plan.migrate else "0",
            "TIER_RESTART": "1" if plan.restart else "0",
            "TIER_EST_GATE_S": str(plan.est_gate_seconds),
            "TIER_NOTE": plan.note,
        }
        if decision:
            kv.update({
                "TIER_REQUIRED": decision.required,
                "TIER_USED": decision.used,
                "TIER_FORCED": "1" if decision.forced else "0",
                "TIER_REFUSED": "1" if decision.refused else "0",
                "TIER_BACKSTOPS": " | ".join(decision.backstops),
                "TIER_BACKSTOP_CODES": ",".join(b.split(":", 1)[0] for b in decision.backstops),
                "TIER_MESSAGE": decision.message,
            })
        for k, v in kv.items():
            out.write(f"{k}={shlex.quote(v)}\n")
        return
    rng = f"{(plan.from_sha or '?')[:12]}..{plan.to_sha[:12]}"
    out.write(f"range    {rng}  ({len(plan.paths)} path(s))\n")
    out.write(f"tier     {plan.tier}\n")
    for v in plan.paths:
        extra = []
        if v.services:
            extra.append("rebuild=" + ",".join(v.services))
        if v.walk_families:
            extra.append("walk=" + ",".join(v.walk_families))
        if v.ui:
            extra.append("ui")
        out.write(f"  {v.tier:<4} {v.status} {v.path}  — {v.reason}"
                  + (f"  [{' '.join(extra)}]" if extra else "") + "\n")
    out.write(f"rebuild  {' '.join(plan.services) or 'none'}"
              f"{'  (+ migrate)' if plan.migrate else ''}\n")
    out.write(f"walk     {' '.join(plan.walk_families) or 'no'}\n")
    out.write(f"ui       {'yes' if plan.ui else 'no'}\n")
    out.write(f"restart  {'yes' if plan.restart else 'no'}\n")
    if plan.full_gate:
        out.write(f"gate     FULL ordered suite (~{plan.est_gate_seconds // 60} min)\n")
    else:
        out.write(f"gate     {len(plan.tests)} test path(s) + ruff"
                  f"{' + UI tsc/vitest/ratchets' if plan.ui else ''}"
                  f" (~{max(1, round(plan.est_gate_seconds / 60))} min)\n")
        for t in plan.tests:
            out.write(f"  {t}\n")
    if decision:
        out.write(f"decision {decision.message}\n")
        for b in decision.backstops:
            out.write(f"  backstop {b}\n")


# ---------------------------------------------------------------------------
def _range(args: argparse.Namespace, git: Git) -> tuple[str | None, str]:
    if args.range:
        if ".." not in args.range:
            raise SystemExit("--range must be A..B")
        a, b = args.range.split("..", 1)
        return (a or None), (b or "HEAD")
    return deployed_sha(args.deploy_log), args.to


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Which deploy tier a change needs.")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--deploy-log", default=DEFAULT_DEPLOY_LOG)
    ap.add_argument("--nightly-log", default=DEFAULT_NIGHTLY_LOG)
    ap.add_argument("--no-cache", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("classify", "decide", "gate-plan"):
        p = sub.add_parser(name)
        p.add_argument("--range", default=None, help="A..B (default: deployed SHA..--to)")
        p.add_argument("--to", default="HEAD")
        p.add_argument("--format", choices=("text", "json", "shell"), default="text")
        if name == "decide":
            p.add_argument("--requested", required=True, choices=("auto",) + TIERS)
            p.add_argument("--force-tier", action="store_true")
            p.add_argument("--now", type=float, default=None, help="epoch seconds (tests)")
        if name == "gate-plan":
            p.add_argument("--tier", choices=TIERS, default=None,
                           help="the tier being gated (full -> the full suite)")
            p.add_argument("--tests-out", default=None, help="write the test paths here, one per line")
    p = sub.add_parser("services")
    p.add_argument("modules", nargs="+")
    p.add_argument("--to", default="HEAD")
    sub.add_parser("deployed-sha")
    args = ap.parse_args(argv)
    git = Git(Path(args.repo))
    try:
        if args.cmd == "deployed-sha":
            sha = deployed_sha(args.deploy_log)
            print(sha or "")
            return 0 if sha else 1
        if args.cmd == "services":
            g = build_graph(git, git.sha(args.to), use_cache=not args.no_cache)
            for m in args.modules:
                print(f"{m}: {','.join(g.services_of(m)) or '(no service)'}"
                      f"{' [write path]' if m in set(g.write_paths) else ''}")
            return 0
        frm, to = _range(args, git)
        plan = classify(git, frm, to, use_cache=not args.no_cache)
        if args.cmd == "classify":
            print_plan(plan, args.format)
            return 0
        if args.cmd == "gate-plan":
            # The tier being gated decides full vs targeted: a full tier runs the
            # whole suite even when the range computes lower; a FORCED lower
            # tier over a full range runs the targeted selection (never nothing).
            if args.tier is not None:
                plan.full_gate = args.tier == FULL
            if plan.full_gate:
                plan.tests = []
                plan.est_gate_seconds = estimate_seconds([], None, plan.ui, True)
            else:
                plan.est_gate_seconds = plan.est_targeted_seconds
            if args.tests_out:
                Path(args.tests_out).write_text("".join(f"{t}\n" for t in plan.tests))
            kv = {
                "GATE_FULL": "1" if plan.full_gate else "0",
                "GATE_UI": "1" if plan.ui else "0",
                "GATE_DESCRIPTORS": "1" if plan.descriptor_check else "0",
                "GATE_N_TESTS": str(len(plan.tests)),
                "GATE_EST_S": str(plan.est_gate_seconds),
                "GATE_TIER_COMPUTED": plan.tier,
                "GATE_FROM": plan.from_sha or "",
                "GATE_TO": plan.to_sha,
            }
            if args.format == "text":
                print_plan(plan, "text")
            else:
                for k, v in kv.items():
                    print(f"{k}={shlex.quote(v)}")
            return 0
        dec = decide(args.requested, plan, force=args.force_tier, git=git,
                     deploy_log_path=args.deploy_log, nightly_log_path=args.nightly_log,
                     now=args.now)
        print_plan(plan, args.format, dec)
        return 3 if dec.refused else 0
    except GitError as exc:
        print(f"deploy_tier: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
