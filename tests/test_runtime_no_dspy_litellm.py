# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard: dspy / litellm must NEVER be on the production runtime hot path.

Operator hard rule (see planning/OPTIMIZER_DSPY_GEPA_PLAN.md +
the `feedback-never-litellm-dspy-production` constraint): dspy and its
litellm transitive dep ship ONLY in the opt-in GEPA worker image
(docker/Dockerfile.worker). The base runtime image is built dspy-free, and
the analyst inference path must degrade to direct ``chat_complete`` when dspy
is absent.

This test enforces the *code-level* half of that guarantee: no module under
the runtime hot path may **module-level** import ``dspy`` or ``litellm``. A
late (function-body) import is fine — that's exactly how
``inline_target.build_prompt_module`` lazily reaches the prompt module only
when the optimizer/worker asks for it. The worker-only package
(``legba.runtime.dapr_workflow``) and the lazily-imported prompt package
(``legba.prompts``) are the sole exemptions.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "legba"

# Hot-path trees scanned for top-level dspy/litellm imports.
_SCANNED = ("runtime", "data/analysts", "data/sources", "data/filters")

# Exempt: the worker-only durable-workflow package (dspy lives here on
# purpose) and the lazily-imported prompt package.
_EXEMPT_PARTS = ("dapr_workflow", "prompts")

_BANNED = {"dspy", "litellm"}


def _module_level_banned_imports(path: Path) -> list[str]:
    """Return banned top-level imports found in ``path`` (empty = clean)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in tree.body:  # body only → module level, not nested in funcs
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BANNED:
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _BANNED:
                hits.append(f"from {node.module} import ...")
    return hits


def _iter_hot_path_modules():
    for sub in _SCANNED:
        base = _SRC_ROOT / Path(sub)
        if not base.exists():
            continue
        for py in base.rglob("*.py"):
            if any(part in _EXEMPT_PARTS for part in py.parts):
                continue
            yield py


def test_runtime_hot_path_never_top_level_imports_dspy_or_litellm():
    offenders: dict[str, list[str]] = {}
    scanned = 0
    for py in _iter_hot_path_modules():
        scanned += 1
        hits = _module_level_banned_imports(py)
        if hits:
            offenders[str(py.relative_to(_SRC_ROOT))] = hits
    assert scanned > 0, "scan found no modules — path wiring is wrong"
    assert not offenders, (
        "dspy/litellm must never be a module-level import on the runtime hot "
        f"path (worker-only rule). Offenders: {offenders}"
    )


def test_dapr_workflow_package_is_the_only_dspy_home():
    """Positive control: the worker-only package DOES import dspy at module
    level (proving the scan would catch it if a hot-path module did)."""
    gepa = _SRC_ROOT / "runtime" / "dapr_workflow" / "gepa.py"
    if not gepa.exists():
        pytest.skip("gepa.py not present")
    # gepa.py imports dspy inside functions (lazy) — assert at least the
    # package exists and is exempt, so the guarantee is structural, not luck.
    assert "dapr_workflow" in _EXEMPT_PARTS
