# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mechanical no-undeclared-stubs gate (POC_PLAN N-1, decision D3).

Scans every production module under ``src/legba/**`` (NOT ``tests/``) for
stub markers and asserts each hit is either

  (a) the conventional abstract-method idiom (a bare ``raise
      NotImplementedError`` — no message — inside a class method, or an
      ``@abstractmethod``-decorated definition), or
  (b) registered in ``docs/SEAMS.md`` by ``file:symbol`` inside the
      machine-readable allowlist block.

There are deliberately NO per-line pragma escapes: the ONLY way to ship a
stub-shaped symbol in a production path is to declare it in the seam
registry, where it carries a what/why/guard-rail entry a reviewer can see.
A new undeclared stub turns this test red.

Markers detected (engineered against false positives — word-boundary
matching over snake_case/camelCase segments, not substrings):

  * ``raise NotImplementedError("...")`` with a message — a loud-fail seam;
    acceptable, but it must be REGISTERED. A bare no-arg raise inside a
    class method is the abstract-method idiom and is allowed as (a).
    A bare raise in a module-level function is a stub and must register.
  * a class subclassing ``NotImplementedError`` (deferred-feature error
    types) and every ``raise`` of it.
  * def/class names containing the word segments stub/placeholder/fake/
    mock/dummy (functions + classes) or echo (classes only — function
    names like ``_echo_clusters_in_bucket`` are legitimate domain terms in
    the adversarial-signals detector; echo-style fake EXTRACTOR/handler
    classes like ``EchoCaptionExtractor`` are the seam being gated).
    "stubborn"/"mockup"-style substrings do NOT match (segment match).
  * ``unittest.mock`` / ``mock`` / ``pytest_mock`` imports in src/**.
  * ``return {}`` with a TODO/FIXME/XXX comment on the same line or the
    three lines above it.

Allowlist format parsed from docs/SEAMS.md (between the BEGIN/END
markers): one ``src/legba/<path>.py:<dotted.symbol>`` per line. An entry
naming a class (dotted prefix) covers symbols nested under it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "legba"
SEAMS_PATH = REPO_ROOT / "docs" / "SEAMS.md"

ALLOWLIST_BEGIN = "<!-- BEGIN SEAM ALLOWLIST -->"
ALLOWLIST_END = "<!-- END SEAM ALLOWLIST -->"

#: Word segments that mark a def/class name as stub-shaped.
NAME_MARKER_WORDS = {
    "stub", "stubs", "stubbed",
    "placeholder", "placeholders",
    "fake", "fakes", "faked",
    "mock", "mocks", "mocked",
    "dummy",
}
#: Segments that only count for CLASS names (see module docstring).
CLASS_ONLY_MARKER_WORDS = {"echo"}

#: Modules whose import in src/** constitutes "mock usage".
MOCK_IMPORT_PREFIXES = ("unittest.mock", "mock", "pytest_mock")

_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX)\b", re.IGNORECASE)
_ALLOW_LINE_RE = re.compile(r"^(src/legba/\S+?\.py):(\S+)$")


def _name_words(name: str) -> set[str]:
    """Split a snake_case / camelCase identifier into lowercase word
    segments. ``_StubParam`` -> {stub, param}; ``stubborn`` -> {stubborn}."""
    words: set[str] = set()
    for part in re.split(r"[_\d]+", name):
        for w in re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+", part):
            words.add(w.lower())
    return words


def _is_abstractmethod(node: ast.AST) -> bool:
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "abstractmethod":
            return True
        if isinstance(target, ast.Name) and target.id == "abstractmethod":
            return True
    return False


def _raise_target_name(node: ast.Raise) -> str | None:
    """Name being raised: ``raise X`` or ``raise X(...)`` -> ``"X"``."""
    exc = node.exc
    if exc is None:
        return None
    if isinstance(exc, ast.Call):
        exc = exc.func
    if isinstance(exc, ast.Name):
        return exc.id
    return None


def _raise_has_args(node: ast.Raise) -> bool:
    return isinstance(node.exc, ast.Call) and bool(
        node.exc.args or node.exc.keywords
    )


class _StubScanner(ast.NodeVisitor):
    """One-file scanner. Collects (qualname, lineno, reason) hits."""

    def __init__(self, source_lines: list[str]) -> None:
        self.lines = source_lines
        self.stack: list[tuple[str, str]] = []  # (kind, name)
        self.hits: list[tuple[str, int, str]] = []
        self.nie_subclasses: set[str] = set()

    # -- helpers --------------------------------------------------------

    def _qualname(self, leaf: str | None = None) -> str:
        names = [n for _, n in self.stack]
        if leaf:
            names.append(leaf)
        return ".".join(names) if names else "<module>"

    def _in_class_method(self) -> bool:
        """True when the innermost def sits directly inside a class."""
        kinds = [k for k, _ in self.stack]
        for i in range(len(kinds) - 1, -1, -1):
            if kinds[i] == "def":
                return i > 0 and kinds[i - 1] == "class"
        return False

    def _enclosing_is_abstract(self, node: ast.AST) -> bool:
        return _is_abstractmethod(node)

    # -- definitions ----------------------------------------------------

    def _check_def_name(self, node: ast.AST, kind: str) -> None:
        name = node.name  # type: ignore[attr-defined]
        words = _name_words(name)
        markers = words & NAME_MARKER_WORDS
        if kind == "class":
            markers |= words & CLASS_ONLY_MARKER_WORDS
        if markers:
            self.hits.append((
                self._qualname(name), node.lineno,
                f"{kind} name carries stub marker segment(s) "
                f"{sorted(markers)}",
            ))

    def _visit_scoped(self, node: ast.AST, kind: str) -> None:
        self._check_def_name(node, kind)
        self.stack.append((kind, node.name))  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for base in node.bases:
            base_name = base.attr if isinstance(base, ast.Attribute) else (
                base.id if isinstance(base, ast.Name) else None
            )
            if base_name == "NotImplementedError":
                self.nie_subclasses.add(node.name)
                self.hits.append((
                    self._qualname(node.name), node.lineno,
                    "class subclasses NotImplementedError "
                    "(deferred-feature error type)",
                ))
        self._visit_scoped(node, "class")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if _is_abstractmethod(node):
            # (a) declared-abstract definition: allowed wholesale.
            return
        self._visit_scoped(node, "def")

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    # -- statements -----------------------------------------------------

    def visit_Raise(self, node: ast.Raise) -> None:
        target = _raise_target_name(node)
        if target == "NotImplementedError":
            if _raise_has_args(node):
                self.hits.append((
                    self._qualname(), node.lineno,
                    "raise NotImplementedError(...) with message "
                    "(loud-fail seam — must be registered)",
                ))
            elif not self._in_class_method():
                self.hits.append((
                    self._qualname(), node.lineno,
                    "bare raise NotImplementedError outside a class method "
                    "(module-level stub)",
                ))
            # bare no-arg raise inside a class method = abstract idiom (a).
        elif target in self.nie_subclasses:
            self.hits.append((
                self._qualname(), node.lineno,
                f"raises NotImplementedError subclass {target!r}",
            ))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in MOCK_IMPORT_PREFIXES or any(
                alias.name.startswith(p + ".") for p in MOCK_IMPORT_PREFIXES
            ):
                self.hits.append((
                    "__mock_import__", node.lineno,
                    f"imports mock module {alias.name!r} in a production path",
                ))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        if mod in MOCK_IMPORT_PREFIXES or any(
            mod.startswith(p + ".") for p in MOCK_IMPORT_PREFIXES
        ):
            self.hits.append((
                "__mock_import__", node.lineno,
                f"imports from mock module {mod!r} in a production path",
            ))

    def visit_Return(self, node: ast.Return) -> None:
        if (
            isinstance(node.value, ast.Dict)
            and not node.value.keys
            and self._has_nearby_todo(node.lineno)
        ):
            self.hits.append((
                self._qualname(), node.lineno,
                "`return {}` with a TODO/FIXME marker nearby",
            ))
        self.generic_visit(node)

    def _has_nearby_todo(self, lineno: int) -> bool:
        lo = max(0, lineno - 4)  # the return line + 3 lines above
        return any(_TODO_RE.search(line) for line in self.lines[lo:lineno])


def _scan_file(path: Path) -> list[tuple[str, int, str]]:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:  # a broken prod module is its own red flag
        return [("<module>", exc.lineno or 0, f"file does not parse: {exc}")]
    scanner = _StubScanner(text.splitlines())
    scanner.visit(tree)
    return scanner.hits


def _load_allowlist() -> set[str]:
    assert SEAMS_PATH.is_file(), (
        f"{SEAMS_PATH} is missing — the declared-seam registry is mandatory "
        "(no-stub rule, decision D3)."
    )
    text = SEAMS_PATH.read_text(encoding="utf-8")
    assert ALLOWLIST_BEGIN in text and ALLOWLIST_END in text, (
        "docs/SEAMS.md must contain the machine-readable allowlist block "
        f"delimited by {ALLOWLIST_BEGIN!r} / {ALLOWLIST_END!r}"
    )
    block = text.split(ALLOWLIST_BEGIN, 1)[1].split(ALLOWLIST_END, 1)[0]
    entries: set[str] = set()
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "```", "<!--")):
            continue
        m = _ALLOW_LINE_RE.match(line)
        assert m, (
            f"malformed SEAMS.md allowlist line {line!r} — expected "
            "`src/legba/<path>.py:<dotted.symbol>`"
        )
        entries.add(line)
    return entries


def _is_allowed(rel_path: str, qualname: str, allowlist: set[str]) -> bool:
    if f"{rel_path}:{qualname}" in allowlist:
        return True
    # A registered class/function covers symbols nested under it.
    parts = qualname.split(".")
    for i in range(1, len(parts)):
        if f"{rel_path}:{'.'.join(parts[:i])}" in allowlist:
            return True
    return False


def _iter_src_files() -> list[Path]:
    files = sorted(SRC_ROOT.rglob("*.py"))
    assert files, f"no python files found under {SRC_ROOT} — wrong checkout?"
    return files


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _defined_symbol_names(path: Path) -> set[str]:
    """Every name DEFINED in a file (class/def/assignment targets, at any
    nesting) — by AST, so a docstring/comment mention does NOT count.

    This is what makes the allowlist staleness check real: deleting the
    code must delete the registry entry. A prose mention of a removed
    symbol ("the former Foo is gone") no longer satisfies validation
    (the 2026-06 adversarial-verify catch).
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
                elif isinstance(tgt, ast.Attribute):
                    names.add(tgt.attr)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_seams_registry_well_formed() -> None:
    """Every allowlist entry must point at a file + symbol that still exist
    AS A DEFINITION — a removed module/symbol must be cleaned out of the
    registry too (a docstring mention does not count)."""
    allowlist = _load_allowlist()
    assert allowlist, "SEAMS.md allowlist block is empty — at minimum the media seam entries must be present"
    problems: list[str] = []
    for entry in sorted(allowlist):
        rel, symbol = entry.split(":", 1)
        path = REPO_ROOT / rel
        if not path.is_file():
            problems.append(f"{entry}: file {rel} does not exist")
            continue
        leaf = symbol.split(".")[-1]
        if leaf.startswith("__") and leaf.endswith("__"):
            continue  # sentinel symbols (e.g. __mock_import__)
        if leaf not in _defined_symbol_names(path):
            problems.append(
                f"{entry}: symbol leaf {leaf!r} is not DEFINED in {rel} "
                f"(class/def/assignment) — stale registry entry "
                f"(a docstring/comment mention does not satisfy this check)"
            )
    assert not problems, (
        "docs/SEAMS.md allowlist has stale entries:\n  " + "\n  ".join(problems)
    )


def test_no_undeclared_stubs_in_src() -> None:
    allowlist = _load_allowlist()
    undeclared: list[str] = []
    for path in _iter_src_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for qualname, lineno, reason in _scan_file(path):
            if not _is_allowed(rel, qualname, allowlist):
                undeclared.append(f"{rel}:{lineno} [{qualname}] {reason}")
    assert not undeclared, (
        "Undeclared stub markers in production paths (no-stub rule, "
        "decision D3). Either build the real path, or — if deferral is a "
        "deliberate decision — add a what/why/guard-rail entry to "
        "docs/SEAMS.md AND its `file:symbol` line to the machine-readable "
        "allowlist block. There is no per-line escape hatch.\n  "
        + "\n  ".join(undeclared)
    )
