# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W4b-F4 — nothing imports the option catalog through its deprecated alias path.

``legba.data.analysts.handler_options*`` are ``sys.modules``-replacing aliases of
``legba.data.option_catalog.handler_options*`` (W3-F17). Importing through the
alias first runs ``analysts/__init__``, which walks the runtime-only dependency
stack the slim registry image lacks, so every importer uses the leaf path. This
guard scans ``src/``, ``tests/`` and ``scripts/`` (Python) for any import of the
old path: ``from legba.data.analysts.handler_options... import``, ``from
legba.data.analysts import handler_options...``, a relative form that resolves
there, ``import legba.data.analysts.handler_options...``, and a literal passed to
``importlib.import_module`` / ``__import__``.

Exempt: the six alias files themselves, and the one test that PROVES the alias
still works (``test_option_catalog_leaf.py``).
"""

from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_OLD_PKG = "legba.data.analysts"
_OLD_PREFIX = f"{_OLD_PKG}.handler_options"

_ALIAS_DIR = _ROOT / "src" / "legba" / "data" / "analysts"
_EXEMPT = {
    _ROOT / "tests" / "data_pkg" / "test_option_catalog_leaf.py",
    _ROOT / "tests" / "data_pkg" / "test_option_catalog_imports.py",
}


def _is_alias_file(path: pathlib.Path) -> bool:
    return path.parent == _ALIAS_DIR and path.name.startswith("handler_options")


def _package_of(path: pathlib.Path) -> list[str] | None:
    """Dotted package (as parts) a file under src/ lives in, else None."""
    try:
        rel = path.relative_to(_ROOT / "src").with_suffix("")
    except ValueError:
        return None
    parts = list(rel.parts)
    return parts if parts[-1] == "__init__" and parts.pop() else parts[:-1]


def _resolve(node: ast.ImportFrom, path: pathlib.Path) -> str:
    if node.level == 0:
        return node.module or ""
    pkg = _package_of(path)
    if pkg is None:
        return ""
    base = pkg[: len(pkg) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _is_old(name: str) -> bool:
    return name.startswith(_OLD_PREFIX)


def _offences(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = _resolve(node, path)
            if _is_old(mod):
                out.append(f"{path}:{node.lineno}: from {mod} import ...")
            elif mod == _OLD_PKG:
                out += [
                    f"{path}:{node.lineno}: from {mod} import {a.name}"
                    for a in node.names if a.name.startswith("handler_options")
                ]
        elif isinstance(node, ast.Import):
            out += [
                f"{path}:{node.lineno}: import {a.name}"
                for a in node.names if _is_old(a.name)
            ]
        elif isinstance(node, ast.Call):
            fn = node.func
            called = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if called in {"import_module", "__import__"} and node.args:
                arg = node.args[0]
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and _is_old(arg.value)
                ):
                    out.append(f"{path}:{node.lineno}: {called}({arg.value!r})")
    return out


def _python_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for top in ("src", "tests", "scripts"):
        files += (_ROOT / top).rglob("*.py")
    return sorted(
        p for p in files
        if "__pycache__" not in p.parts
        and not _is_alias_file(p)
        and p not in _EXEMPT
    )


def test_no_file_imports_the_deprecated_alias_path():
    files = _python_files()
    assert len(files) > 500, "the scan found almost nothing; the root is wrong"
    offenders = [o for p in files for o in _offences(p)]
    assert not offenders, (
        "import the option catalog from legba.data.option_catalog.* (the stdlib-only "
        "leaf), not the deprecated legba.data.analysts.handler_options* alias:\n"
        + "\n".join(offenders)
    )


def test_the_scan_catches_every_import_form(tmp_path):
    """The guard is only worth having if it fires: feed it each shape."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from legba.data.analysts.handler_options import X\n"
        "from legba.data.analysts import handler_options_catalog\n"
        "import legba.data.analysts.handler_options_base\n"
        "import importlib\n"
        "importlib.import_module('legba.data.analysts.handler_options_events')\n"
        "__import__('legba.data.analysts.handler_options')\n"
        "from legba.data.option_catalog.handler_options import Y\n"
        "from legba.data.analysts import something_else\n",
        encoding="utf-8",
    )
    assert len(_offences(bad)) == 5, _offences(bad)

    node = ast.parse("from ..data.analysts.handler_options import Z").body[0]
    rel = _ROOT / "src" / "legba" / "runtime" / "x.py"
    assert _resolve(node, rel) == "legba.data.analysts.handler_options"
    node = ast.parse("from ..analysts.handler_options import Z").body[0]
    inner = _ROOT / "src" / "legba" / "data" / "schemas" / "analyst.py"
    assert _resolve(node, inner) == "legba.data.analysts.handler_options"
    node = ast.parse("from .handler_options import Z").body[0]
    sib = _ROOT / "src" / "legba" / "data" / "analysts" / "inline_target.py"
    assert _resolve(node, sib) == "legba.data.analysts.handler_options"
    node = ast.parse("from . import handler_options").body[0]
    init = _ROOT / "src" / "legba" / "data" / "analysts" / "__init__.py"
    assert _resolve(node, init) == "legba.data.analysts"
