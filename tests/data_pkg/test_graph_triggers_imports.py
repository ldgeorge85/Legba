# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The slim-image import guard for the V3/P4a trigger-gauge route.

``graph_triggers_api`` ships in the REGISTRY image, which does not carry the
analyst-runtime dependency stack. A route module that reaches
``legba.data.analysts`` — even inside a function body, since deferring moves
WHEN the import graph is walked, never HOW FAR — 500s live. The deployed
precedent is the ``/units/{id}/correctness`` 500 on ``feedparser`` (see
``test_unit_correctness_api.test_the_read_route_imports_without_the_runtime_
dependency_stack``); this is the same guard, applied at creation.

The probe runs in a SUBPROCESS with the heavy third-party modules poisoned to
``None`` — the pytest process has the full dev stack installed, so an
in-process check would pass while the slim image burned.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap

_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


def test_graph_triggers_route_imports_without_the_runtime_stack() -> None:
    """The route must import in the slim image: no analyst/runtime modules in
    the import graph, and the router factory is present."""
    probe = textwrap.dedent(
        """
        import sys
        # Exactly what the slim image does to these: not present.
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.registry.graph_triggers_api as api

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts")
               or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime package reachable from the route: %r" % leaked
        assert api.build_graph_triggers_router is not None
        assert api.GRAPH_TRIGGER_GAUGE_VERSION == "2026-09/p4a"
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        # Keep the parent's PYTHONPATH tail: in the test container the deps
        # live outside src/, reachable ONLY through it (2026-09-22 fix).
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, (
        "graph_triggers_api no longer imports without the runtime dependency "
        f"stack:\n{result.stdout}\n{result.stderr}"
    )
    assert "OK" in result.stdout
