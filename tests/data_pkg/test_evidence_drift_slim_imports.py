# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D-1 — the source-drift read route imports in the SLIM registry image.

``GET /v3/evidence/drift*`` ships in the registry image, which does not carry
the analyst-runtime stack. Same subprocess probe as
``test_collection_slim_imports``: the slim image's absent modules poisoned to
``None``, and nothing from ``legba.data.analysts`` or ``legba.runtime``
reachable from the route or its reads leaf.
"""
from __future__ import annotations

from tests.data_pkg.test_collection_slim_imports import _assert_ok, _probe


def test_the_evidence_drift_route_imports_in_the_slim_image() -> None:
    _assert_ok(_probe(
        """
        import legba.data.registry.server as server
        import legba.data.registry.evidence_drift_api as api
        import legba.data.evidence_drift_reads as reads
        import legba.data.evidence_drift_slice as mark

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the drift route: %r" % leaked
        assert api.build_evidence_drift_router is not None and reads.open_flags is not None
        assert mark.SOURCE_DRIFT_ROW_KEY == "source_drift"
        print("OK")
        """
    ))
