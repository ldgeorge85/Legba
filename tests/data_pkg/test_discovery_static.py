# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-183 — Static-target shortcut tests.

Per L-106 §4.3 a target descriptor with no ``discovery`` block is **not**
a degenerate one-candidate discovery. The descriptor *is* the instance.
No state row, no diff, no relabel.

These tests confirm:

  * :func:`is_static_descriptor` correctly identifies the no-block path
    against both pydantic ``TargetDescriptor`` instances AND raw dicts.
  * :func:`materialize_static` returns a single
    :class:`StaticMaterialization` whose body equals the descriptor.
  * Routing the wrong descriptor into :func:`materialize_static` raises
    a clear error (programming-error case).
  * :func:`discover_discovery_kinds` carries the static sentinel and
    surfaces it under :data:`STATIC_KIND_NAME` — the dispatcher pattern
    works uniformly across discovery + static, so a future
    ``country_list_discovery`` kind doesn't have to special-case the
    no-discovery branch.
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.data.discovery import (
    STATIC_KIND_NAME,
    StaticMaterialization,
    discover_discovery_kinds,
    is_static_descriptor,
    materialize_static,
)


# ---------------------------------------------------------------------------
# is_static_descriptor
# ---------------------------------------------------------------------------


class TestIsStaticDescriptor:
    def test_none_is_static(self):
        # Defensive — a missing descriptor is treated as static so the
        # materialization loop doesn't crash.
        assert is_static_descriptor(None) is True

    def test_dict_without_discovery_key_is_static(self):
        body = {"identity": {"id": "target.foo", "version": "v1"}}
        assert is_static_descriptor(body) is True

    def test_dict_with_null_discovery_is_static(self):
        body = {"identity": {"id": "target.foo"}, "discovery": None}
        assert is_static_descriptor(body) is True

    def test_dict_with_empty_discovery_is_static(self):
        body = {"identity": {"id": "target.foo"}, "discovery": {}}
        assert is_static_descriptor(body) is True

    def test_dict_with_static_sentinel_kind_is_static(self):
        body = {
            "identity": {"id": "target.foo"},
            "discovery": {"kind": STATIC_KIND_NAME},
        }
        assert is_static_descriptor(body) is True

    def test_dict_with_real_discovery_kind_is_not_static(self):
        body = {
            "identity": {"id": "country_news_template"},
            "discovery": {"kind": "country_list_discovery", "list_source": "x"},
        }
        assert is_static_descriptor(body) is False

    def test_pydantic_descriptor_without_discovery_is_static(self):
        # Build a minimal TargetDescriptor with no discovery block. The
        # schema imports are heavy, so we use a duck-typed stand-in here
        # that exposes the relevant attributes the predicate looks at.
        class _Identity:
            id = "target.bar"

        class _FakeDescriptor:
            identity = _Identity()
            discovery = None

        assert is_static_descriptor(_FakeDescriptor()) is True

    def test_pydantic_descriptor_with_discovery_is_not_static(self):
        class _Identity:
            id = "country_news_template"

        class _DiscoveryBlock:
            kind = "country_list_discovery"

        class _FakeDescriptor:
            identity = _Identity()
            discovery = _DiscoveryBlock()

        assert is_static_descriptor(_FakeDescriptor()) is False


# ---------------------------------------------------------------------------
# materialize_static
# ---------------------------------------------------------------------------


class TestMaterializeStatic:
    def test_materialize_dict_returns_carrier(self):
        body = {
            "identity": {"id": "target.south_china_sea_monitor", "version": "v1"},
            "sources": [{"kind": "rss", "config": {"url": "https://example.com"}}],
        }
        result = materialize_static(body)
        assert isinstance(result, StaticMaterialization)
        assert result.natural_key == "target.south_china_sea_monitor"
        assert result.descriptor_id == "target.south_china_sea_monitor"
        assert result.dropped is False
        assert result.kept is True
        assert result.discovered_from is None
        # Identity materialization — body equals input.
        assert dict(result.materialized_body) == body

    def test_materialize_pydantic_uses_model_dump(self):
        class _Identity:
            id = "target.bar"

            @staticmethod
            def model_dump(**kwargs: Any) -> dict[str, Any]:
                return {"id": "target.bar"}

        class _FakeDescriptor:
            discovery = None

            def __init__(self) -> None:
                self.identity = _Identity()

            def model_dump(self, **kwargs: Any) -> dict[str, Any]:
                return {"identity": {"id": "target.bar"}, "scope": {}}

        result = materialize_static(_FakeDescriptor())
        assert result.natural_key == "target.bar"
        assert result.descriptor_id == "target.bar"
        assert result.materialized_body == {
            "identity": {"id": "target.bar"},
            "scope": {},
        }

    def test_materialize_refuses_discovery_descriptor(self):
        body = {
            "identity": {"id": "country_news_template"},
            "discovery": {"kind": "country_list_discovery"},
        }
        with pytest.raises(ValueError, match="active discovery block"):
            materialize_static(body)

    def test_materialize_requires_identity_id(self):
        with pytest.raises(ValueError, match="identity.id"):
            materialize_static({"identity": {}})

    def test_materialize_rejects_unknown_type(self):
        with pytest.raises(TypeError):
            materialize_static(42)


# ---------------------------------------------------------------------------
# Registry dispatch — uniform shape across discovery + static
# ---------------------------------------------------------------------------


class TestRegistryDispatchSymmetry:
    def test_static_kind_dispatches_via_registry(self):
        """The L-183 acceptance criterion: a future ``country_list_discovery``
        kind doesn't have to special-case the no-discovery branch. The
        static path lives behind the same registry as discovery kinds.
        """
        registry = discover_discovery_kinds()
        assert STATIC_KIND_NAME in registry

        bundle = registry[STATIC_KIND_NAME]
        # The static bundle exposes a synchronous materialize_static
        # callable. Discovery bundles expose an async discover() iterator
        # instead. The dispatcher branches on ``is_static``.
        assert bundle.is_static is True
        assert callable(bundle.materialize_static)
        assert bundle.discover is None
        assert bundle.healthcheck is None

    def test_registry_bundle_for_static_runs_materialize(self):
        registry = discover_discovery_kinds()
        bundle = registry[STATIC_KIND_NAME]
        result = bundle.materialize_static(
            {"identity": {"id": "target.foo"}, "scope": {}}
        )
        assert isinstance(result, StaticMaterialization)
        assert result.descriptor_id == "target.foo"

    def test_static_sentinel_carries_schema_version(self):
        registry = discover_discovery_kinds()
        bundle = registry[STATIC_KIND_NAME]
        assert bundle.schema_version.startswith("legba/discovery/static/")
