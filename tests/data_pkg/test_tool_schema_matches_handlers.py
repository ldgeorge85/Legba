# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The declared tool grammar equals what the handlers actually read (W1 F-7).

``TOOL_SCHEMAS`` (``legba.data.stack.llm.tool_rounds``) is the one place the
argument signature of a tool is stated for the native channel, and
``validate_args`` REFUSES an argument the schema does not declare. A handler
that reads a key the schema omits is therefore unreachable for a model that
obeys the schema; a schema property no handler reads is a promise the tool
does not keep. The wave-Q report found four such drifts by hand; this pins them
by AST so the next one fails here rather than in a live consult.

For every entry in ``TOOL_SCHEMAS`` the test finds the handler registered for
the tool (``registry.register("<tool>", fn)`` anywhere under
``data/analysts/agency``), walks it and the module-local helpers it calls, and
collects each ``<args>.get("k")`` / ``<args>["k"]`` where ``<args>`` is
``call.args`` or a local ``args`` alias. A dispatcher that branches on
``if name == "<tool>"`` contributes only the branch for the tool under test.
The set read, minus :data:`INJECTED_ARG_KEYS`, must equal the declared
properties — less the documented read-side aliases in ``_READ_ALIASES``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from legba.data.stack.llm.tool_rounds import INJECTED_ARG_KEYS, TOOL_SCHEMAS

_AGENCY = (
    Path(__file__).resolve().parents[2]
    / "src" / "legba" / "data" / "analysts" / "agency"
)

#: Keys a handler reads that the schema deliberately does NOT declare — each a
#: tolerated alias of a declared key, so the model is shown one spelling.
#: ``series_*`` read ``from``/``to`` first and fall back to ``since``/``until``
#: (the sibling readers' spelling). Adding to this table is a decision.
_READ_ALIASES: dict[str, frozenset[str]] = {
    "series_history": frozenset({"since", "until"}),
    "series_compare": frozenset({"since", "until"}),
    "live_series": frozenset({"since", "until"}),
}

_BRANCH_NAMES = ("name", "tool_name")


def _is_args_expr(node: ast.AST) -> bool:
    """``call.args`` / a local ``args`` / ``(call.args or {})``."""
    if isinstance(node, ast.Name):
        return node.id == "args"
    if isinstance(node, ast.Attribute):
        return (
            node.attr == "args"
            and isinstance(node.value, ast.Name)
            and node.value.id == "call"
        )
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return any(_is_args_expr(v) for v in node.values)
    return False


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _branch_const(test: ast.AST) -> str | None:
    """``name == "x"`` -> ``"x"`` (the dispatcher branch key), else ``None``."""
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.left, ast.Name)
        and test.left.id in _BRANCH_NAMES
    ):
        return _const_str(test.comparators[0])
    return None


class _Index:
    """Functions and registrations of the handler modules (AST only)."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.funcs: dict[str, dict[str, ast.AST]] = {}
        self.registered: dict[str, tuple[str, str]] = {}  # tool -> (mod, fn)
        for mod, src in sources.items():
            tree = ast.parse(src)
            self.funcs[mod] = {
                n.name: n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for n in ast.walk(tree):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "register"
                    and len(n.args) >= 2
                    and _const_str(n.args[0]) is not None
                    and isinstance(n.args[1], ast.Name)
                ):
                    # first registration wins: a module that re-registers a
                    # sibling's handler (journal_read) names the same callable
                    self.registered.setdefault(
                        _const_str(n.args[0]), (mod, n.args[1].id))

    def find(self, mod: str, fn: str) -> tuple[str, ast.AST] | None:
        if fn in self.funcs.get(mod, {}):
            return mod, self.funcs[mod][fn]
        for m, fs in self.funcs.items():
            if fn in fs:
                return m, fs[fn]
        return None

    def reads(self, tool: str) -> set[str]:
        mod, fn = self.registered[tool]
        out: set[str] = set()
        self._walk_fn(mod, fn, tool, out, set())
        return out

    def _walk_fn(self, mod, fn, tool, out, seen) -> None:
        found = self.find(mod, fn)
        if found is None or (found[0], fn) in seen:
            return
        seen.add((found[0], fn))
        self._visit(found[0], found[1], tool, out, seen)

    def _visit(self, mod, node, tool, out, seen) -> None:
        for child in ast.iter_child_nodes(node):
            self._visit_node(mod, child, tool, out, seen)

    def _visit_node(self, mod, node, tool, out, seen) -> None:
        if isinstance(node, ast.If) and _branch_const(node.test) is not None:
            # a dispatcher branch: only the tool's own branch counts
            taken = node.body if _branch_const(node.test) == tool else node.orelse
            for s in taken:
                self._visit_node(mod, s, tool, out, seen)
            return
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and _is_args_expr(node.func.value)
            and _const_str(node.args[0]) is not None
        ):
            out.add(_const_str(node.args[0]))
        elif (
            isinstance(node, ast.Subscript)
            and _is_args_expr(node.value)
            and _const_str(node.slice) is not None
        ):
            out.add(_const_str(node.slice))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            self._walk_fn(mod, node.func.id, tool, out, seen)
        self._visit(mod, node, tool, out, seen)


def _sources() -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(_AGENCY.glob("*.py"))}


def _declared(tool: str) -> set[str]:
    return set(TOOL_SCHEMAS[tool].json_schema["properties"])


@pytest.fixture(scope="module")
def index() -> _Index:
    return _Index(_sources())


def test_every_schema_has_a_registered_handler(index):
    """A schema with no handler registered is a tool nobody can run."""
    missing = sorted(t for t in TOOL_SCHEMAS if t not in index.registered)
    assert not missing, f"TOOL_SCHEMAS entries with no registered handler: {missing}"


@pytest.mark.parametrize("tool", sorted(TOOL_SCHEMAS))
def test_declared_properties_equal_what_the_handler_reads(index, tool):
    read = index.reads(tool) - INJECTED_ARG_KEYS
    declared = _declared(tool)
    aliases = _READ_ALIASES.get(tool, frozenset())
    assert not (read - declared - aliases), (
        f"{tool}: the handler reads {sorted(read - declared - aliases)} but the "
        "schema does not declare it — validate_args refuses it before the "
        "handler runs"
    )
    assert not (declared - read), (
        f"{tool}: the schema declares {sorted(declared - read)} but no handler "
        "reads it — the model is offered an argument that does nothing"
    )


def test_the_aliases_table_is_not_stale(index):
    for tool, aliases in _READ_ALIASES.items():
        assert aliases <= index.reads(tool), (
            f"{tool}: _READ_ALIASES names {sorted(aliases)} but the handler no "
            "longer reads them — drop the entry"
        )
        assert not (aliases & _declared(tool))


def test_the_extractor_sees_a_hand_made_drift():
    """The guard must fail when a drift is introduced — proven on a synthetic
    handler module, so the check does not depend on editing the real tree."""
    src = '''
def _helper(args):
    return args.get("extra")

async def _dispatch(call, name):
    args = call.args
    if name == "alpha":
        return args.get("a"), args["b"], _helper(args)
    elif name == "beta":
        return args.get("c")

async def alpha_tool(call):
    return await _dispatch(call, "alpha")

async def beta_tool(call):
    return (call.args or {}).get("only")

def register(registry):
    registry.register("alpha", alpha_tool)
    registry.register("beta", beta_tool)
'''
    idx = _Index({"synthetic.py": src})
    assert idx.reads("alpha") == {"a", "b", "extra"}
    assert idx.reads("beta") == {"only"}
