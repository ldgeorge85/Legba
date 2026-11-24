# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""I-1 — the ingestion intake's read route and the source descriptor's follow
block import in the SLIM registry image.

``GET /v3/ingestion/intake`` ships in the registry image, which does not
carry the analyst-runtime stack; ``SourceDescriptor`` (now with ``follow``)
is the registry's own schema. A deferred import of a runtime module would
still fail there (deferring moves WHEN the graph is walked, never HOW FAR),
so this is the same subprocess probe as ``test_collection_slim_imports``:
the slim image's absent modules poisoned to ``None``, and nothing from
``legba.data.analysts`` or ``legba.runtime`` reachable.
"""
from __future__ import annotations

from tests.data_pkg.test_collection_slim_imports import _assert_ok, _probe


def test_the_intake_route_imports_in_the_slim_image() -> None:
    _assert_ok(_probe(
        """
        import legba.data.registry.server as server
        import legba.data.registry.intake_api as intake_api
        import legba.data.intake_visibility as vis
        import legba.data.intake_contract as contract
        from legba.data.schemas.source import SourceDescriptor

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the intake route: %r" % leaked
        assert "follow" in SourceDescriptor.model_fields
        assert intake_api.build_intake_router is not None and vis.intake_counts is not None
        assert contract.IntakeLimits().freshness_hours == 6.0
        print("OK")
        """
    ))
