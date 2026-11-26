# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry must be able to VALIDATE ``method.options`` on PUT.

**The defect.** Every source PUT response carried a warning, and the registry
log said ``handler-options catalog unimportable here: dependency 'feedparser'
absent in this environment``. ``schemas/analyst.py`` imports the option catalog
to check a descriptor's ``method.options``; the catalog lived at
``legba.data.analysts.handler_options*``, and importing ANY module of that
package first runs ``analysts/__init__.py``, which eagerly imports every kind
module and, through ``deterministic_handlers`` -> ``sources``, ``feedparser``
(and ``networkx``). The slim registry image ships neither, so the check was
skipped in the one process that sits in front of every PUT, and a descriptor
with an undeclared ``method.options`` block was accepted by the registry and
then refused by the runtime parse: the 2026-09-28 90-minute dead analyst.

**The fix** is structural: the catalog moved to the leaf package
``legba.data.option_catalog`` (stdlib + siblings only); the old import paths
are aliases. This file is the guard.

If it goes red, DO NOT satisfy it by deferring an import: a deferred import
moves WHEN the graph is walked, never HOW FAR it reaches. Move what the
catalog needs into the leaf package.

The probes run in SUBPROCESSES with the runtime-only dependencies poisoned
(``sys.modules[name] = None`` makes ``import name`` raise exactly as in the slim
image). The pytest process has the full stack and has very likely already
imported the analysts package, so an in-process check would pass while the
registry stayed blind.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")

#: In docker/Dockerfile.runtime and NOT in docker/Dockerfile.registry's explicit
#: pip list (read both when adding to this): the packages the slim image lacks.
RUNTIME_ONLY_DEPS = (
    "dapr", "langdetect", "numpy", "sklearn", "pandas", "networkx", "pyod",
    "statsforecast", "feedparser", "trafilatura", "curl_cffi", "geopy",
    "aiohttp", "warcio", "aiobotocore", "telethon", "mcp",
)

_POISON = (
    "import sys\n"
    f"for _blocked in {RUNTIME_ONLY_DEPS!r}:\n"
    "    sys.modules[_blocked] = None\n"
)


def _run(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _POISON + textwrap.dedent(probe)],
        capture_output=True, text=True, timeout=180,
        # Keep the parent's PYTHONPATH tail: in the test container the deps live
        # at /install/lib/python3.11/site-packages, reachable ONLY through it.
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )


def _ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout, f"{result.stdout}\n{result.stderr}"


def test_the_poison_bites_the_old_package_path():
    """Control: under the same poison the ANALYSTS package does not import.

    Without this the guards below could pass vacuously (a poison that does not
    bite proves nothing about the slim image)."""
    result = _run(
        """
        try:
            import legba.data.analysts.deterministic
        except ImportError as exc:
            assert "None in sys.modules" in str(exc), str(exc)
            print("OK")
        else:
            raise SystemExit("analysts.deterministic imported under the poison")
        """
    )
    _ok(result)


def test_catalog_and_schemas_import_without_the_runtime_dependency_stack():
    """THE GUARD: the catalog loads, and the analysts package is never walked."""
    result = _run(
        """
        import legba.data.schemas.analyst as A
        from legba.data.option_catalog import handler_options as H
        from legba.data.option_catalog import handler_options_catalog as C

        assert A._load_options_catalog() is not None, "catalog unimportable"
        assert H.ANALYST_KIND_OPTIONS is C.ANALYST_KIND_OPTIONS
        assert H.HANDLER_OPTIONS and H.ANALYST_KIND_OPTIONS

        leaked = sorted(m for m in sys.modules if m.startswith("legba.data.analysts"))
        assert not leaked, "the analysts package entered the graph: %r" % leaked
        print("OK")
        """
    )
    _ok(result)


_DESCRIPTOR = """
from legba.data.schemas.analyst import (
    AnalystDescriptor, AnalystIdentity, AnalystKind, CadenceBlock,
    MethodBlock, SubscriptionBlock, TypeSignature,
)


def build(kind, options):
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="probe_analyst", name="Probe", schema_uri="legba/analyst/2.0.0",
            version="a" * 32, kind=kind,
            type_signature=TypeSignature(
                input_type="legba.x.In", output_type="legba.x.Out"),
            owner="owner@local",
        ),
        subscription=SubscriptionBlock(),
        method=MethodBlock(
            kind="llm_single_turn", impl="legba.data.analysts.inline_target",
            system_prompt="probe system prompt", options=options,
        ),
        cadence=CadenceBlock(),
    )
"""


def test_schemas_analyst_rejects_a_catalogless_kind_and_accepts_a_declared_one():
    """With the runtime-only deps absent the registry-side validator still BITES.

    ``critic`` declares no kind catalog, so a ``method.options`` block on it
    would be silently inert: refused at registration (the check that used to be
    skipped in the slim image, so the registry said 200 and the runtime parse
    then killed the analyst). ``inline_target`` declares a catalog and a
    declared knob passes."""
    result = _run(
        _DESCRIPTOR
        + """
import pydantic
from legba.data.schemas.analyst import AnalystKind

try:
    build(AnalystKind.CRITIC, {"some_knob": 1})
except pydantic.ValidationError as exc:
    assert "silently inert" in str(exc), str(exc)
else:
    raise SystemExit("a catalog-less kind's options block was ACCEPTED")

good = build(AnalystKind.INLINE_TARGET, {"open_events_limit": 5})
assert good.method.options == {"open_events_limit": 5}
print("OK")
"""
    )
    _ok(result)


def test_an_undeclared_knob_on_a_declared_kind_is_resolved_as_unknown_key():
    """Registry-side the catalog now KNOWS the kind: an undeclared knob on
    ``inline_target`` resolves as ``unknown_key`` (warn-not-raise, by design —
    see ``AnalystDescriptor._warn_on_dead_options``) while a declared one is
    accepted. Proven under the poison, i.e. as the slim image sees it."""
    result = _run(
        """
        from legba.data.option_catalog.handler_options import resolve_kind_options

        bad = resolve_kind_options("inline_target", {"no_such_knob": 1})
        assert [r.cause for r in bad.rejected] == ["unknown_key"], bad
        assert bad.accepted == {}
        good = resolve_kind_options("inline_target", {"open_events_limit": 5})
        assert good.accepted == {"open_events_limit": 5} and not good.rejected
        print("OK")
        """
    )
    _ok(result)


def test_the_old_import_paths_are_aliases_of_the_leaf_modules():
    """``legba.data.analysts.handler_options*`` stays importable AND is the same
    module object, so a monkeypatch through either name hits one module."""
    import importlib

    for name in (
        "handler_options", "handler_options_base", "handler_options_catalog",
        "handler_options_events", "handler_options_inquiry",
        "handler_options_programs",
    ):
        old = importlib.import_module(f"legba.data.analysts.{name}")
        new = importlib.import_module(f"legba.data.option_catalog.{name}")
        assert old is new, name

    from legba.data.analysts import handler_options as via_package
    from legba.data.option_catalog import handler_options as leaf
    assert via_package is leaf


def test_the_leaf_package_imports_nothing_outside_the_stdlib_and_itself():
    """Static: fails on the import STATEMENT, not on whatever it drags in today."""
    pkg = pathlib.Path(_SRC) / "legba" / "data" / "option_catalog"
    files = sorted(pkg.glob("*.py"))
    assert len(files) >= 7, files
    stdlib = set(sys.stdlib_module_names)
    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
                offenders += [f"{path.name}: import {n}" for n in names if n not in stdlib]
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    top = (node.module or "").split(".")[0]
                    if top not in stdlib:
                        offenders.append(f"{path.name}: from {node.module}")
                elif node.level != 1 and not (
                    # The type registry is itself a stdlib-only leaf
                    # (statically guarded in test_type_registry.py); the
                    # declared-options splice is the one module that reads it.
                    path.name == "handler_options_declared.py"
                    and node.level == 2 and node.module == "type_registry"
                ):
                    # `..x` would climb out of the leaf package.
                    offenders.append(
                        f"{path.name}: from {'.' * node.level}{node.module or ''}"
                    )
    assert not offenders, (
        "option_catalog must stay a leaf (stdlib + sibling modules only): "
        f"{offenders}"
    )


def test_schemas_never_name_the_analysts_package_for_the_catalog():
    """The registry-side validator must import the LEAF, not the alias path
    (the alias path walks ``analysts/__init__`` first)."""
    src = pathlib.Path(_SRC, "legba", "data", "schemas", "analyst.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    bad = [
        f"line {n.lineno}"
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and "handler_options" in (n.module or "")
        and (n.level >= 2 and (n.module or "").startswith("analysts"))
    ]
    assert not bad, f"schemas/analyst.py imports the catalog via analysts: {bad}"
    assert "option_catalog.handler_options" in src


@pytest.mark.parametrize("name", ["handler_options_catalog", "handler_options"])
def test_leaf_modules_expose_the_same_declared_names(name):
    import importlib
    mod = importlib.import_module(f"legba.data.option_catalog.{name}")
    assert mod.__all__
    for exported in mod.__all__:
        assert hasattr(mod, exported), exported
