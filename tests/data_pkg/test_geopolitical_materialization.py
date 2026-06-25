# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wave D — descriptor body-merge contract for the L-200 geopolitical chain.

C-1 NOTE: the two retired pre-pivot integration tests (full L2-template →
L1-instance materialisation + the disappearance-ratio pause) were DELETED —
they asserted the pre-pivot target-owned substrate shape (inline
SourceBindings on materialised targets) that migration 0024 removed. The
post-pivot discovery/materialisation contract is covered by
``tests/descriptors/test_geopolitical_decomposition.py`` (relabel chain,
Antarctica drop, disappearance guard) and
``tests/data_pkg/test_discovery_contract.py``. What remains here is the
pure ``merge_descriptor_bodies`` parent-body merge contract.
"""

from __future__ import annotations

from legba.data.registry.discovered_materializer import merge_descriptor_bodies


# ---------------------------------------------------------------------------
# merge_descriptor_bodies — unit-style (no substrate)
# ---------------------------------------------------------------------------


class TestParentBodyMergeContract:
    """Wave D §5 lean: relabeled-replaces-template for scalars and lists,
    deep-merge for dicts."""

    def test_scalar_relabeled_overrides_template(self):
        t = {"identity": {"id": "template", "owner": "geo"}}
        r = {"identity": {"id": "country_br"}}
        merged = merge_descriptor_bodies(t, r)
        assert merged["identity"]["id"] == "country_br"
        # template's other keys preserved
        assert merged["identity"]["owner"] == "geo"

    def test_list_relabeled_replaces_template(self):
        t = {"scope": {"geo": ["XX"], "tags": []}}
        r = {"scope": {"geo": ["BR"], "tags": ["news", "geopolitical"]}}
        merged = merge_descriptor_bodies(t, r)
        # Relabeled list fully replaces — not appended.
        assert merged["scope"]["geo"] == ["BR"]
        assert merged["scope"]["tags"] == ["news", "geopolitical"]

    def test_dict_deep_merged(self):
        t = {"scope": {"geo": ["XX"], "time_horizon_days": 90}}
        r = {"scope": {"geo": ["BR"]}}
        merged = merge_descriptor_bodies(t, r)
        # Relabeled wrote scope.geo; template's scope.time_horizon_days survives.
        assert merged["scope"]["geo"] == ["BR"]
        assert merged["scope"]["time_horizon_days"] == 90

    def test_none_relabeled_is_no_override(self):
        t = {"identity": {"id": "template", "owner": "geo"}}
        r = {"identity": {"id": None, "owner": "geo"}}
        merged = merge_descriptor_bodies(t, r)
        # None means "no override" — template's id survives.
        assert merged["identity"]["id"] == "template"

    def test_template_keys_inherited_when_relabel_silent(self):
        t = {"sources": [{"id": "rss", "kind": "rss"}], "scope": {"geo": ["XX"]}}
        r = {"scope": {"geo": ["BR"]}}
        merged = merge_descriptor_bodies(t, r)
        # Relabel didn't touch sources — template's value carries.
        assert merged["sources"] == [{"id": "rss", "kind": "rss"}]

    def test_inputs_not_mutated(self):
        t = {"scope": {"geo": ["XX"]}}
        r = {"scope": {"geo": ["BR"]}}
        merge_descriptor_bodies(t, r)
        assert t["scope"]["geo"] == ["XX"]
        assert r["scope"]["geo"] == ["BR"]
