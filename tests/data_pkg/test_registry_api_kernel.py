# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-2 — the registry-API kernel split, and the two ways it could rot.

`legba.data.registry._deps` holds the kernel that 26 of this package's 50
modules were reaching into a 2,524-line `api.py` to get: the B-2 bearer gate,
the C3 `sunset_headers` stamp, and the `RegistryAPIDeps` bundle + `_get_deps`
resolver. `api.py` imports it ONE WAY and re-exports every moved name, so not a
single importer changed (rewriting them onto `_deps` is K-5, operator-gated).

That arrangement has exactly two failure modes, and this file pins both:

  * **The re-export silently narrows.** Someone tidies `api.py`'s import block,
    drops a `noqa`-marked name nobody greps for, and `from .api import
    sunset_headers` starts failing in a sibling router — or worse, a test that
    monkeypatches `api.API_TOKEN_ENV` quietly patches a name that no longer
    exists. So: every kernel name must still resolve THROUGH `api`, and must be
    the SAME OBJECT as `_deps`' — a copy would mean two auth gates in one
    process, one of them un-patchable.
  * **The edge reverses.** The whole point of the split is that `_deps` is a
    leaf. The moment anything in `_deps` imports `api` — even inside a function
    — the import hub is back and the circular-import hazard with it. Checked
    statically over the source, which catches the deferred-import form that a
    runtime check would miss.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import legba.data.registry._deps as kernel
import legba.data.registry.api as api

#: Everything the kernel owns. The first five are the cross-module surface the
#: cleanup analysis measured (`planning/CODE_CLEANUP_ANALYSIS_2026-08-02.md`
#: section 3.4); the rest are the constants and privates that tests and
#: `api.py`'s own module body reach for by name.
KERNEL_NAMES = (
    "RegistryAPIDeps",
    "require_bearer",
    "sunset_headers",
    "_authorize_ws_token",
    "_get_deps",
    "API_TOKEN_ENV",
    "DEV_MODE_ENV",
    "MISCONFIGURED_AUTH_DETAIL",
    "DEPRECATION_SUNSET_HTTP_DATE",
    "_bearer_from_header",
    "_current_token",
    "_dev_mode",
    "_token_matches",
)

_DEPS_SRC = Path(kernel.__file__)


def test_every_kernel_name_still_resolves_through_api() -> None:
    """The re-export is the compatibility contract for all 26 importers."""
    missing = [n for n in KERNEL_NAMES if not hasattr(api, n)]
    assert not missing, (
        "these kernel names stopped resolving on legba.data.registry.api: "
        f"{missing}. They moved to `_deps` in K-2 and are re-exported from "
        "`api`; every existing importer still spells them `api.<name>`. "
        "Restore the re-export, or rewrite the importers first (that is K-5)."
    )


def test_the_reexport_is_the_same_object_not_a_copy() -> None:
    """One gate per process. Two would be one un-monkeypatchable too many."""
    divergent = [n for n in KERNEL_NAMES if getattr(api, n) is not getattr(kernel, n)]
    assert not divergent, (
        f"api.<name> is not _deps.<name> for {divergent} — the kernel was "
        "duplicated rather than re-exported. Tests that patch one would leave "
        "the other live, which for the auth gate means a fail-closed check that "
        "silently no longer runs."
    )


def test_build_router_deliberately_stays_in_api() -> None:
    """The fifth cross-module name is the 1,400-line router factory — it is the
    reason `api.py` is big, and it is NOT kernel. Pin the boundary so a later
    split does not drag it in here and recreate the hub under a new name."""
    assert hasattr(api, "build_router")
    assert not hasattr(kernel, "build_router")
    # Same for the first-run config contract: a config-status list, not auth.
    assert hasattr(api, "REQUIRED_MODEL_COMPONENT_KINDS")
    assert not hasattr(kernel, "REQUIRED_MODEL_COMPONENT_KINDS")


def test_deps_never_imports_api_at_any_depth() -> None:
    """`_deps` is a leaf. Statically checked, so a deferred (in-function)
    import — the usual way a cycle sneaks back in — is caught too."""
    tree = ast.parse(_DEPS_SRC.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = node.module or ""
            relative_sibling = node.level == 1 and target == "api"
            if relative_sibling or target.endswith("registry.api"):
                offenders.append(f"line {node.lineno}: from {'.' * node.level}{target} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("registry.api"):
                    offenders.append(f"line {node.lineno}: import {alias.name}")
    assert not offenders, (
        "legba/data/registry/_deps.py imports the module it was extracted "
        f"FROM: {offenders}. That closes the cycle K-2 opened up. Whatever "
        "`_deps` needs from `api` either belongs in `_deps` or does not belong "
        "in the kernel."
    )


def test_importing_the_kernel_does_not_drag_in_the_http_surface() -> None:
    """The payoff, measured: a sibling router importing the kernel must not
    load `api.py` — every pydantic model, every route body, and every module
    `api.py` later grows an import of."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import legba.data.registry._deps; "
            "print('legba.data.registry.api' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "False", (
        "importing legba.data.registry._deps pulled in legba.data.registry.api "
        "— the kernel is no longer a leaf, so the import-hub decoupling K-2 "
        f"bought is gone.\nstdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
