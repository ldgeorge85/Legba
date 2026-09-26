# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression: ``handler_options`` <-> ``handler_options_programs`` must not
be a circular import, in EITHER direction.

**The defect** (latent, found by a standalone-import check run right after
the extract-deps-and-options merge). ``handler_options_programs.py`` used to
import ``OptionSpec`` and the three terse constructor helpers (``_pos_int``,
``_window_days``, ``_window_hours``) back from ``handler_options.py``, and
``handler_options.py`` imported the three catalog blocks
(``DESK_REFERENCE_OPTIONS`` etc.) from ``handler_options_programs.py``. Every
test in this tree and every real consumer (``dapr_actors``,
``discover_analyst_kinds``) imports ``handler_options`` FIRST, so the partial
circular import always happened to resolve — but it was order-dependent, not
actually safe. Importing the SIBLING first, in a fresh interpreter —
``python -c "import legba.data.analysts.handler_options_programs"`` — raised::

    ImportError: cannot import name 'DESK_REFERENCE_OPTIONS' from partially
    initialized module 'legba.data.analysts.handler_options_programs'
    (most likely due to a circular import)

**The fix.** ``OptionSpec`` and the three constructor helpers moved to a LEAF
module, ``handler_options_base.py``, which imports from neither sibling.
``handler_options.py`` imports from the leaf and re-exports the same names;
``handler_options_programs.py`` imports from the leaf only. Neither sibling
imports the other's module object anymore, so there is no cycle left for
import order to paper over.

**Why a subprocess.** A same-process ``import`` here would reuse whatever
``sys.modules`` entry the test collector (or an earlier test in the same
run) already established for ``legba.data.analysts.handler_options`` —
exactly the "always imported in the working order" trap that let the defect
ship. A subprocess with a genuinely fresh interpreter is the only way to
prove the ORDER does not matter.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run_standalone_import(module: str) -> subprocess.CompletedProcess[str]:
    # Inherit this interpreter's resolved sys.path into the child's
    # PYTHONPATH rather than hand-building one — in the test container the
    # non-stdlib dependencies (pydantic and friends) arrive via PYTHONPATH,
    # not the default sys.path, so a hand-built env drops them and every
    # import fails before it reaches the code under test.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        check=False,
    )


def test_handler_options_programs_imports_standalone() -> None:
    """``handler_options_programs`` must import cleanly FIRST, standalone.

    This is the exact reproduction of the defect: before the leaf split,
    this import — as the very first touch of either module in a fresh
    interpreter — raised ``ImportError`` for ``DESK_REFERENCE_OPTIONS`` "from
    a partially initialized module".
    """
    proc = _run_standalone_import("legba.data.analysts.handler_options_programs")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_handler_options_imports_standalone() -> None:
    """The other order — every existing consumer's order — must keep working.

    Not the defect by itself (this direction always worked), but pinned here
    so a future change cannot fix one direction by breaking the other.
    """
    proc = _run_standalone_import("legba.data.analysts.handler_options")
    assert proc.returncode == 0, proc.stdout + proc.stderr
