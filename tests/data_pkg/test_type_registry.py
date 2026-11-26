# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The type registry's INVARIANTS (they replace the pinned lists).

A pinned total says only "someone added one" and makes every change that adds a
thing edit the same line. These rules say what must be TRUE of every declared
type instead, so a new type passes them by being complete, not by bumping a
number:

* every deterministic handler: its code imports and is callable, its options
  have exactly one home (catalogue or declaration), a descriptor binds it, a
  test names it, it has a doc line;
* the verify-exempt set IS the finding emitters, and the console mirror equals
  it (generated; never hand-synced again);
* every alert kind: a topic, a doc line, a producer outside the registry names
  it, and it is in the shipped page rule exactly when it is not declared
  ``logged_only``;
* every generated artefact is current (``scripts/gen_type_tables.py --check``);
* a bad declaration of any shape stops the build, every failure named at once;
* the registry, ``provenance.kinds`` and ``alerts.kinds`` import in the slim
  registry image (the runtime-only dependencies poisoned).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from legba.data.alerts import kinds as AK
from legba.data.alerts import page_rule as PR
from legba.data.alerts import topics as AT
from legba.data.analysts import deterministic as D
from legba.data.kind_discovery import KindDiscoveryError
from legba.data.option_catalog.handler_options import HANDLER_OPTIONS
from legba.data.provenance import kinds as PK
from legba.data.type_registry import (
    FAMILY_PACKAGES,
    TypeRegistry,
    default_registry,
    load_implementations,
)

REPO = Path(__file__).resolve().parents[2]
REG = default_registry()
HANDLERS = REG.types("deterministic_handler")
ALERT_KINDS = REG.types("alert_kind")


def _corpus(root: Path, *, exclude: str | None = None) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in root.rglob("*.py")
        if exclude is None or exclude not in p.as_posix()
    )


# ---------------------------------------------------------------------------
# Deterministic handlers
# ---------------------------------------------------------------------------


def test_every_handler_implementation_imports_and_is_the_dispatch_entry():
    impls = load_implementations(HANDLERS, registry="deterministic_handler")
    assert impls == D.SUB_HANDLERS
    for decl in HANDLERS:
        module, _, attr = decl.implementation.partition(":")
        assert impls[decl.id] is getattr(sys.modules[module], attr)


def test_every_handler_has_exactly_one_options_home():
    assert set(HANDLER_OPTIONS) == {d.id for d in HANDLERS}


def test_every_handler_is_bound_by_a_descriptor():
    bound: set[str] = set()
    for path in (REPO / "descriptors").glob("*.yaml"):
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(body, dict) or not isinstance(body.get("method"), dict):
            continue
        options = body["method"].get("options") or {}
        if isinstance(options, dict) and options.get("sub_handler"):
            bound.add(str(options["sub_handler"]))
        identity = body.get("identity") or {}
        if identity.get("kind") == "deterministic":
            bound.add(str(identity.get("id")))
    unbound = sorted(d.id for d in HANDLERS if d.id not in bound)
    assert not unbound, f"declared handlers no descriptor binds: {unbound}"


def test_every_handler_is_named_by_a_test():
    tests = _corpus(REPO / "tests", exclude="type_registry")
    unnamed = sorted(
        d.id for d in HANDLERS if not re.search(rf"\b{re.escape(d.id)}\b", tests))
    assert not unnamed, f"declared handlers no test names: {unnamed}"


def test_the_exempt_sets_are_derived_and_consistent():
    assert PK.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS == frozenset(
        k for k, v in D.OUTPUT_KIND_BY_SUB_HANDLER.items() if v is PK.OutputKind.FINDING)
    assert PK.STRUCTURAL_CLAIMS_VERIFY_ANALYSTS <= PK.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
    assert set(D.OUTPUT_KIND_BY_SUB_HANDLER) == set(D.SUB_HANDLERS)


def test_the_console_mirror_equals_the_server_set():
    ts = (REPO / "legba-ui-v3" / "src" / "lib" / "verdictModel.ts").read_text(encoding="utf-8")
    block = ts.split("// BEGIN GENERATED structural-verify-exempt", 1)[1].split(
        "// END GENERATED structural-verify-exempt", 1)[0]
    assert set(re.findall(r"'([a-z_]+)'", block)) == set(PK.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS)


# ---------------------------------------------------------------------------
# Alert kinds
# ---------------------------------------------------------------------------


def test_every_alert_kind_has_a_topic_and_a_doc_line():
    for decl in ALERT_KINDS:
        assert AT.TOPIC_BY_KIND[decl.id] in AT.TOPICS
        assert decl.doc.strip()
    assert list(AK.KINDS) == [d.id for d in ALERT_KINDS]


def test_paging_declarations_match_the_shipped_page_rule():
    """Every kind is in the shipped rule, or declared logged-only — never
    neither, never both. (The rule is policy and stays a file the owner edits;
    this keeps the declarations honest about it.)"""
    shipped = PR.load_rule(REPO / "src" / "legba" / "data" / "alerts" / "page_rule.yaml")
    paged = {e.kind for e in shipped.entries}
    declared_paged = {d.id for d in ALERT_KINDS if d.attr("paging") == "page_rule"}
    assert paged == declared_paged, (
        f"in the rule but declared logged_only: {sorted(paged - declared_paged)}; "
        f"declared page_rule but absent from the rule: {sorted(declared_paged - paged)}")


def test_every_alert_kind_is_named_by_a_producer():
    src = _corpus(REPO / "src" / "legba", exclude="type_registry")
    scripts = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in (REPO / "scripts").rglob("*") if p.suffix in (".py", ".sh"))
    unnamed = sorted(
        d.id for d in ALERT_KINDS if not re.search(rf"\b{re.escape(d.id)}\b", src + scripts))
    assert not unnamed, f"alert kinds nothing outside the registry raises: {unnamed}"


def test_handler_alerts_name_declared_alert_kinds():
    known = {d.id for d in ALERT_KINDS}
    for decl in HANDLERS:
        assert set(decl.attr("alerts")) <= known, decl.id


# ---------------------------------------------------------------------------
# Generated artefacts, loud failure, the slim image
# ---------------------------------------------------------------------------


def test_generated_tables_are_current():
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "gen_type_tables.py"), "--check"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_every_bad_declaration_is_named_in_one_error():
    with pytest.raises(KindDiscoveryError) as err:
        TypeRegistry.build(extra_modules=[
            "tests.data_pkg.type_registry_bad.bad_declarations",
            "tests.data_pkg.type_registry_bad.broken_import",
            "tests.data_pkg.type_registry_bad.declares_nothing",
        ], entry_point_group=None)
    text = str(err.value)
    for needle in (
        "graph_mining' is also declared",
        "bad_no_doc: doc is required",
        "bad_unknown_field: unknown fields",
        "bad_trace_claims: structural_claims needs output='finding'",
        "bad_no_topic: required field 'topic' is missing",
        "unknown family 'record_kind'",
        "broken_import [import_failed]",
        "declares_nothing [missing_contract] named as a type module but declares nothing",
    ):
        assert needle in text, f"{needle!r} not in:\n{text}"


def test_a_family_package_module_must_declare_exactly_once(tmp_path, monkeypatch):
    pkg = tmp_path / "rr_family_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "two.py").write_text(textwrap.dedent("""
        from legba.data.type_registry import declare
        declare(family="alert_kind", id="rr_one", position=1, subject_kind="host",
                lifecycle="event", quiet_close_hours=1, group="host", topic="engine",
                paging="logged_only", doc="x")
        declare(family="alert_kind", id="rr_two", position=2, subject_kind="host",
                lifecycle="event", quiet_close_hours=1, group="host", topic="engine",
                paging="logged_only", doc="x")
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    packages = dict(FAMILY_PACKAGES)
    packages["alert_kind"] = "rr_family_pkg"
    with pytest.raises(KindDiscoveryError, match="exactly once"):
        TypeRegistry.build(family_packages=packages, entry_point_group=None)


def test_entry_point_modules_are_walked(monkeypatch):
    """Another distribution adds types through ``legba.types``."""
    from importlib import metadata

    class _EP:
        name = "fixture"
        value = "tests.data_pkg.type_registry_fixture.echo_alert_decl"

    real = metadata.entry_points
    monkeypatch.setattr(
        metadata, "entry_points",
        lambda **kw: [_EP()] if kw.get("group") == "legba.types" else real(**kw))
    reg = TypeRegistry.build()
    assert "fixture_echo_alert" in reg.ids("alert_kind")


def test_the_registry_package_is_a_leaf():
    """Static, like the option catalogue's guard: the registry and every
    declaration module import only the stdlib, the registry itself,
    ``kind_discovery`` and the stdlib-only ``handler_options_base``."""
    import ast

    allowed = {
        "legba.data.type_registry", "legba.data.kind_discovery",
        "legba.data.option_catalog.handler_options_base",
    }
    stdlib = set(sys.stdlib_module_names)
    pkg = REPO / "src" / "legba" / "data" / "type_registry"
    offenders = []
    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(REPO / "src").with_suffix("").parts
        here = ".".join(rel[:-1] if rel[-1] != "__init__" else rel[:-1])
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                offenders += [f"{path.name}: {a.name}" for a in node.names
                              if a.name.split(".")[0] not in stdlib]
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    target = node.module or ""
                else:
                    base = here.split(".")[: len(here.split(".")) - (node.level - 1)]
                    target = ".".join(base + ([node.module] if node.module else []))
                if target.split(".")[0] in stdlib:
                    continue
                if not any(target == a or target.startswith(a + ".") for a in allowed):
                    offenders.append(f"{path.relative_to(pkg)}: {target}")
    assert not offenders, offenders


_SLIM_PROBE = """
import sys
for _blocked in {deps!r}:
    sys.modules[_blocked] = None
import legba.data.provenance.kinds as pk
import legba.data.alerts.kinds as ak
import legba.data.alerts.topics as at
import legba.data.option_catalog.handler_options as ho
assert pk.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS and ak.KINDS and at.TOPIC_BY_KIND
assert set(ho.HANDLER_OPTIONS)
assert "legba.data.analysts.deterministic" not in sys.modules
print("OK")
"""


def test_registry_side_imports_under_the_slim_image():
    from tests.data_pkg.test_option_catalog_leaf import RUNTIME_ONLY_DEPS

    result = subprocess.run(
        [sys.executable, "-c", _SLIM_PROBE.format(deps=RUNTIME_ONLY_DEPS)],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (str(REPO / "src"), os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr
