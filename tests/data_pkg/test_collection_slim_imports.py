# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The slim-image import guard for the `collection` descriptor family (7g-1).

The family is served by the generic ``/descriptors/{family}`` routes in
``registry/api.py``, which ships in the REGISTRY image — and that image does
not carry the analyst-runtime dependency stack. Three edges added by 7g-1
could each have dragged it in, and all three are one-line mistakes:

* ``registry/api.py`` → ``registry/descriptor_families.py`` (the per-family
  INSERT and the lifecycle state machines);
* ``descriptor_families`` → ``schemas/collection.py`` → ``provenance.access``
  and ``provenance.origin``, which reach the ``legba.data.provenance``
  package on the way;
* ``registry/production_gauge.py`` → ``provenance.origin``, added by the
  SEAMS #57 sweep so the freshness and source-health reads carry the
  origin-class leg.

A deferred import would not help: deferring moves WHEN the graph is walked,
never HOW FAR. Same mechanism as ``test_belief_api_imports`` — a subprocess
probe with the heavy third-party modules poisoned to ``None``, which is
exactly what the slim image does to them.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap

_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


def _probe(body: str) -> subprocess.CompletedProcess:
    script = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        """
    ) + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, (
        f"slim import failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_the_collection_family_imports_in_the_slim_image() -> None:
    _assert_ok(_probe(
        """
        import legba.data.registry.api as api
        from legba.data.registry.descriptor import Family
        from legba.data.schemas.collection import (
            COLLECTION_SCHEMA_VERSION, CollectionDescriptor, FENCED_SURFACES,
        )

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts")
               or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the registry: %r" % leaked
        assert Family.COLLECTION.model is CollectionDescriptor
        assert Family.COLLECTION.table == "collection_descriptors"
        assert COLLECTION_SCHEMA_VERSION == "2026-09/7g-1"
        assert len(FENCED_SURFACES) == 8
        assert api.build_router is not None
        print("OK")
        """
    ))


def test_the_swept_registry_reads_import_in_the_slim_image() -> None:
    """``production_gauge`` gained the origin-class leg (SEAMS #57 sweep).

    It is a non-router judgment module the registry's routers share, so it
    lives under the same constraint as a route: ``provenance.origin`` has to
    stay the leaf its own docstring claims it is.
    """
    _assert_ok(_probe(
        """
        import legba.data.registry.production_gauge as gauge
        from legba.data.provenance.origin import origin_class_clause

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts")
               or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the gauge: %r" % leaked
        assert origin_class_clause("") in gauge._SOURCE_SQL
        print("OK")
        """
    ))
