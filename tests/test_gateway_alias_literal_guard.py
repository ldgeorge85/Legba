# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T-1 guard: no call site in ``src/legba`` hard-codes a gateway model alias.

The gateway serves its models under aliases (``fast`` / ``deep`` on the core
plane; ``gemma``, ``lightning`` and their ``-think`` twins). Which alias a call
names is component data (``model_name``, ``thinking_model_name``) and is
chosen in ONE place, ``stack/llm/thinking.py:resolve_call_model``. A literal
alias at a call site would bypass that place — a thinking call that the
component, the receipts and the owner cannot see.

The scan is an AST walk, so prose that mentions an alias is never a hit. It
flags an alias string used as:

* a ``model`` / ``model_name`` / ``thinking_model_name`` keyword argument;
* the value of a ``"model"`` / ``"model_name"`` / ``"thinking_model_name"``
  dict key;
* the value assigned to a name containing ``MODEL``.

The allowlist names the pre-existing sites that are NOT a core-plane call's
alias choice, each with its reason. Adding to it needs the same.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "legba"

GATEWAY_CHAT_ALIASES = frozenset({
    "fast", "deep", "gemma", "gemma-think", "lightning", "lightning-think",
})
_MODEL_KEYS = frozenset({"model", "model_name", "thinking_model_name"})

#: (path relative to src/legba, alias) → why it is not an alias choice.
ALLOWED: dict[tuple[str, str], str] = {
    ("data/analysts/deterministic_handlers/_correctness_grade.py", "gemma"):
        "the grader family table's calibration label; the call goes through "
        "the component, whose own model_name is what is sent",
    ("data/analysts/deterministic_handlers/_correctness_grade.py", "lightning"):
        "the grader family table's calibration label (same as above)",
    ("data/stack/nlp_service/gateway.py", "fast"):
        "the NLP component's translate route default (its own component knob, "
        "translate_model), not a chat call on the core plane",
}


def _alias(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value in GATEWAY_CHAT_ALIASES:
            return node.value
    return None


def _hits(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in _MODEL_KEYS and (a := _alias(kw.value)):
                    out.append((node.lineno, a))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value in _MODEL_KEYS
                        and (a := _alias(value))):
                    out.append((value.lineno, a))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if any("MODEL" in n.upper() for n in names) and (a := _alias(node.value)):
                out.append((node.lineno, a))
    return out


def test_no_call_site_hard_codes_a_gateway_alias() -> None:
    offenders: list[str] = []
    seen_allowed: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for line, alias in _hits(path):
            if (rel, alias) in ALLOWED:
                seen_allowed.add((rel, alias))
                continue
            offenders.append(f"src/legba/{rel}:{line} names the gateway alias {alias!r}")
    assert not offenders, (
        "a gateway alias is chosen in ONE place (stack/llm/thinking.py) from the "
        "component row; pass thinking=True instead of naming an alias:\n  "
        + "\n  ".join(offenders)
    )
    stale = set(ALLOWED) - seen_allowed
    assert not stale, f"allowlist entries that no longer match anything: {sorted(stale)}"


def test_the_guard_catches_each_shape(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "await llm.chat_complete(msgs, model='deep')\n"
        "body = {'model': 'fast', 'messages': []}\n"
        "THINK_MODEL = 'lightning-think'\n"
        "mode = 'deep'  # a consult mode, not a model: not a hit\n"
        "# model='deep' in a comment is not a hit\n"
    )
    assert sorted(_hits(sample)) == [(1, "deep"), (2, "fast"), (3, "lightning-think")]
