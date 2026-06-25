# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-180 — Discovery-kind contract unit tests.

Exercises the L-106 §2 + §3 + §5 surfaces in :mod:`legba.data.discovery`:

  * Protocol shape: :class:`DiscoveryKind` is runtime-checkable.
  * :class:`CandidateTarget` round-trips through pydantic and honors the
    ``id`` / ``labels`` aliases the L-180 brief calls out.
  * :class:`RelabelRule` evaluator covers all 9 actions (``set``,
    ``set_list``, ``format``, ``lookup``, ``lookup_languages``,
    ``merge_list``, ``keep``, ``drop``, ``hash_mod``).
  * Chain evaluation composes (each rule sees the previous rule's output).
  * Drop-short-circuit semantics (``keep`` False / ``drop`` True /
    ``hash_mod`` miss stop the chain).
  * The L-106 §3 worked-example chain expands correctly for ``BR``.
  * Disappearance-ratio threshold (0.30 default) triggers anomaly +
    routes the disappeared natural_keys to DLQ payload; under-threshold
    cycles proceed; cold start / min_prior_active bypass.
  * Threshold=0 disables the check.
  * :func:`discover_discovery_kinds` returns the static sentinel and
    tolerates absent first-party kinds.

No substrate dependencies — the contract layer is pure pydantic + the
relabel evaluator. The Starlark predicate sandbox is consulted opportunistically;
the tests use the fallback AST-restricted evaluator so they run in any
environment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncIterator, ClassVar

import pytest
from pydantic import BaseModel, ValidationError

from legba.data.discovery import (
    DEFAULT_DISAPPEARANCE_RATIO_THRESHOLD,
    CandidateTarget,
    DiscoveryContext,
    DiscoveryEvidence,
    DiscoveryHealth,
    DiscoveryKind,
    InMemoryStateStore,
    RELABEL_ACTIONS,
    RELABEL_ACTION_HANDLERS,
    RelabelResult,
    RelabelRule,
    ResyncPolicy,
    apply_relabel_rule,
    discover_discovery_kinds,
    evaluate_disappearance,
    evaluate_relabel_chain,
)
from legba.data.discovery.static import STATIC_KIND_NAME


# ---------------------------------------------------------------------------
# CandidateTarget — pydantic round-trip + aliases
# ---------------------------------------------------------------------------


class TestCandidateTarget:
    def test_natural_key_required(self):
        with pytest.raises(ValidationError):
            CandidateTarget(label_set={"x": 1})  # type: ignore[call-arg]

    def test_brief_alias_shape(self):
        """The L-180 brief talks about ``(id, labels, source_metadata)``.

        The pydantic model exposes ``id`` as an alias for ``natural_key``
        and ``labels`` as an alias for ``label_set`` so the brief shape
        parses without surface friction.
        """
        c = CandidateTarget(
            id="BR",
            labels={"country_iso2": "BR"},
            source_metadata={"region": "south_america"},
        )
        assert c.natural_key == "BR"
        assert c.id == "BR"
        assert c.label_set == {"country_iso2": "BR"}
        assert c.labels == {"country_iso2": "BR"}
        assert c.source_metadata == {"region": "south_america"}

    def test_roundtrip_json(self):
        c = CandidateTarget(
            natural_key="US",
            label_set={"country_iso2": "US", "country_languages": ["en-US"]},
            source_metadata={"region": "north_america"},
            evidence=DiscoveryEvidence(
                source_id="stack.country_lists.global_active",
                source_version="v42",
                row_index=12,
                extra={"region": "north_america"},
            ),
        )
        body = c.model_dump(mode="json")
        c2 = CandidateTarget.model_validate(body)
        assert c2 == c
        assert c2.evidence.source_version == "v42"

    def test_seen_at_defaults_to_utc_now(self):
        before = datetime.now(tz=timezone.utc)
        c = CandidateTarget(natural_key="X")
        after = datetime.now(tz=timezone.utc)
        assert before <= c.seen_at <= after
        assert c.seen_at.tzinfo is not None


# ---------------------------------------------------------------------------
# DiscoveryKind Protocol — structural check
# ---------------------------------------------------------------------------


class _DummyConfig(BaseModel):
    list_size: int = 3


class DummyDiscoveryKind:
    """Minimal concrete handler used only to validate that the
    :class:`DiscoveryKind` Protocol is satisfied by a plain class."""

    kind: ClassVar[str] = "dummy_discovery"
    family: ClassVar[str] = "discovery"
    schema_version: ClassVar[str] = "legba/discovery/dummy/1.0.0"
    config_schema: ClassVar[type[BaseModel]] = _DummyConfig

    async def discover(
        self, ctx: DiscoveryContext
    ) -> AsyncIterator[CandidateTarget]:
        for i in range(ctx.config.list_size):
            yield CandidateTarget(natural_key=f"K{i}", label_set={"idx": i})

    async def healthcheck(self, ctx: DiscoveryContext) -> DiscoveryHealth:
        return DiscoveryHealth(state="healthy", candidates_24h=ctx.config.list_size)


class TestDiscoveryKindProtocol:
    def test_dummy_satisfies_runtime_protocol(self):
        # runtime_checkable Protocol — structural typing.
        assert isinstance(DummyDiscoveryKind(), DiscoveryKind)

    def test_protocol_has_required_classvars(self):
        h = DummyDiscoveryKind()
        assert h.kind == "dummy_discovery"
        assert h.family == "discovery"
        assert h.schema_version.startswith("legba/discovery/")
        assert issubclass(h.config_schema, BaseModel)

    def test_context_carries_state_store_and_config(self):
        ctx = DiscoveryContext(
            discovery_id="d1",
            discovery_version="abc",
            config=_DummyConfig(list_size=2),
            state_store=InMemoryStateStore(),
        )
        assert ctx.discovery_id == "d1"
        assert isinstance(ctx.config, _DummyConfig)


# ---------------------------------------------------------------------------
# RelabelRule — single-action coverage (all 9)
# ---------------------------------------------------------------------------


def _make_candidate(**labels: Any) -> CandidateTarget:
    return CandidateTarget(
        natural_key=str(labels.get("country_iso2", labels.get("k", "X"))),
        label_set=labels,
        source_metadata={"region": labels.get("region", "")},
    )


class TestRelabelActionsCoverage:
    def test_action_set_copies_value(self):
        c = _make_candidate(country_iso2="BR")
        r = RelabelRule(
            source_labels=["country_iso2"], target_label="iso", action="set"
        )
        labels, dropped, _ = apply_relabel_rule(r, c)
        assert dropped is False
        assert labels["iso"] == "BR"

    def test_action_set_list_wraps_scalar(self):
        c = _make_candidate(country_iso2="BR")
        r = RelabelRule(
            source_labels=["country_iso2"], target_label="scope.geo", action="set_list"
        )
        labels, dropped, _ = apply_relabel_rule(r, c)
        assert dropped is False
        assert labels["scope"]["geo"] == ["BR"]

    def test_action_set_list_preserves_list(self):
        c = _make_candidate(country_languages=["pt-BR", "en"])
        r = RelabelRule(
            source_labels=["country_languages"],
            target_label="scope.languages",
            action="set_list",
        )
        labels, _, _ = apply_relabel_rule(r, c)
        assert labels["scope"]["languages"] == ["pt-BR", "en"]

    def test_action_format_renders_jinja_ish_template(self):
        c = _make_candidate(country_iso2="BR")
        r = RelabelRule(
            source_labels=["country_iso2"],
            target_label="id",
            action="format",
            replacement="country_news_{{ country_iso2 | lower }}",
        )
        labels, _, _ = apply_relabel_rule(r, c)
        assert labels["id"] == "country_news_br"

    def test_action_format_supports_slug_filter(self):
        c = _make_candidate(country_name="São Paulo Bureau")
        r = RelabelRule(
            source_labels=["country_name"],
            target_label="slug",
            action="format",
            replacement="{{ country_name | slug }}",
        )
        labels, _, _ = apply_relabel_rule(r, c)
        assert labels["slug"] == "sao-paulo-bureau"

    def test_action_format_unknown_filter_raises(self):
        c = _make_candidate(x="y")
        r = RelabelRule(
            source_labels=["x"], target_label="z", action="format",
            replacement="{{ x | nonsense }}",
        )
        with pytest.raises(ValueError, match="unknown format filter"):
            apply_relabel_rule(r, c)

    def test_action_lookup_resolves_from_injected_table(self):
        c = _make_candidate(country_iso2="MX")
        r = RelabelRule(
            source_labels=["country_iso2"],
            target_label="region",
            action="lookup",
            table="region_by_country",
        )
        labels, _, _ = apply_relabel_rule(
            r, c,
            lookup_tables={"region_by_country": {"MX": "north_america"}},
        )
        assert labels["region"] == "north_america"

    def test_action_lookup_fallback_when_table_missing(self):
        c = _make_candidate(country_iso2="ZZ")
        r = RelabelRule(
            source_labels=["country_iso2"],
            target_label="region",
            action="lookup",
            table="region_by_country",
            fallback="unknown",
        )
        labels, _, _ = apply_relabel_rule(r, c)
        assert labels["region"] == "unknown"

    def test_action_lookup_languages_uses_builtin_seed(self):
        c = _make_candidate(country_iso2="BR")
        r = RelabelRule(
            source_labels=["country_iso2"],
            target_label="scope.languages",
            action="lookup_languages",
        )
        labels, _, _ = apply_relabel_rule(r, c)
        assert labels["scope"]["languages"] == ["pt-BR"]

    def test_action_lookup_languages_falls_back_to_second_source_label(self):
        c = _make_candidate(country_iso2="ZZ", country_languages=["xx-XX"])
        r = RelabelRule(
            source_labels=["country_iso2", "country_languages"],
            target_label="scope.languages",
            action="lookup_languages",
        )
        labels, _, _ = apply_relabel_rule(r, c)
        # Unknown country code → fall back to the candidate's own list.
        assert labels["scope"]["languages"] == ["xx-XX"]

    def test_action_merge_list_appends_static(self):
        c = _make_candidate(country_iso2="BR")
        # Pre-seed scope.languages then merge ["en"].
        c = CandidateTarget(
            natural_key="BR",
            label_set={"scope": {"languages": ["pt-BR"]}, "country_iso2": "BR"},
        )
        r = RelabelRule(
            source_labels=["scope.languages"],
            target_label="scope.languages",
            action="merge_list",
            extend_with=["en"],
        )
        labels, _, _ = apply_relabel_rule(r, c)
        assert labels["scope"]["languages"] == ["pt-BR", "en"]

    def test_action_merge_list_dedupes(self):
        c = CandidateTarget(
            natural_key="BR",
            label_set={"scope": {"languages": ["pt-BR", "en"]}},
        )
        r = RelabelRule(
            source_labels=["scope.languages"],
            target_label="scope.languages",
            action="merge_list",
            extend_with=["en", "es"],
        )
        labels, _, _ = apply_relabel_rule(r, c)
        assert labels["scope"]["languages"] == ["pt-BR", "en", "es"]

    def test_action_keep_drops_when_predicate_false(self):
        c = _make_candidate(country_iso2="AQ", region="antarctica")
        r = RelabelRule(
            source_labels=["region"],
            action="keep",
            predicate="region != 'antarctica'",
        )
        labels, dropped, reason = apply_relabel_rule(r, c)
        assert dropped is True
        assert "keep" in reason

    def test_action_keep_passes_when_predicate_true(self):
        c = _make_candidate(country_iso2="BR", region="south_america")
        r = RelabelRule(
            source_labels=["region"],
            action="keep",
            predicate="region != 'antarctica'",
        )
        _, dropped, _ = apply_relabel_rule(r, c)
        assert dropped is False

    def test_action_drop_drops_when_predicate_true(self):
        c = _make_candidate(country_iso2="AQ", region="antarctica")
        r = RelabelRule(
            source_labels=["region"],
            action="drop",
            predicate="region == 'antarctica'",
        )
        _, dropped, reason = apply_relabel_rule(r, c)
        assert dropped is True
        assert "drop" in reason

    def test_action_drop_passes_when_predicate_false(self):
        c = _make_candidate(country_iso2="BR", region="south_america")
        r = RelabelRule(
            source_labels=["region"],
            action="drop",
            predicate="region == 'antarctica'",
        )
        _, dropped, _ = apply_relabel_rule(r, c)
        assert dropped is False

    def test_action_hash_mod_shard_match(self):
        c = _make_candidate(country_iso2="BR")
        # Compute the expected shard ourselves to know what eq to ask for.
        import hashlib
        digest = hashlib.sha256("BR".encode()).digest()
        h = int.from_bytes(digest[:8], "big", signed=False)
        eq = h % 4
        r = RelabelRule(
            source_labels=["country_iso2"], action="hash_mod",
            modulus=4, eq=eq,
        )
        _, dropped, _ = apply_relabel_rule(r, c)
        assert dropped is False

    def test_action_hash_mod_shard_miss_drops(self):
        c = _make_candidate(country_iso2="BR")
        import hashlib
        digest = hashlib.sha256("BR".encode()).digest()
        h = int.from_bytes(digest[:8], "big", signed=False)
        # Pick a different residue so the candidate falls out of shard.
        eq = (h % 4 + 1) % 4
        r = RelabelRule(
            source_labels=["country_iso2"], action="hash_mod",
            modulus=4, eq=eq,
        )
        _, dropped, reason = apply_relabel_rule(r, c)
        assert dropped is True
        assert "hash_mod" in reason

    def test_unknown_action_raises(self):
        c = _make_candidate(x="y")
        r = RelabelRule(source_labels=["x"], target_label="z", action="bogus")
        with pytest.raises(ValueError, match="unknown relabel action"):
            apply_relabel_rule(r, c)

    def test_all_nine_actions_covered_in_handler_table(self):
        """Sanity check — every action in the closed set has a handler."""
        for action in RELABEL_ACTIONS:
            assert action in RELABEL_ACTION_HANDLERS, action


# ---------------------------------------------------------------------------
# Chain composition + L-106 §3 worked example
# ---------------------------------------------------------------------------


class TestRelabelChain:
    def test_chain_each_rule_sees_previous_output(self):
        c = CandidateTarget(natural_key="BR", label_set={"country_iso2": "BR"})
        rules = [
            RelabelRule(
                source_labels=["country_iso2"],
                target_label="iso_upper",
                action="set",
            ),
            RelabelRule(
                source_labels=["iso_upper"],
                target_label="id",
                action="format",
                replacement="x_{{ iso_upper | lower }}",
            ),
        ]
        r = evaluate_relabel_chain(c, rules)
        assert r.kept is True
        assert r.labels["iso_upper"] == "BR"
        assert r.labels["id"] == "x_br"

    def test_chain_short_circuits_on_drop(self):
        c = CandidateTarget(natural_key="AQ", label_set={"region": "antarctica"})
        rules = [
            RelabelRule(
                source_labels=["region"], target_label="r", action="set"
            ),
            RelabelRule(
                source_labels=["region"], action="drop",
                predicate="region == 'antarctica'",
            ),
            RelabelRule(
                source_labels=["region"], target_label="never_set", action="set"
            ),
        ]
        r = evaluate_relabel_chain(c, rules)
        assert r.dropped is True
        assert r.dropped_at == 1
        assert r.dropped_by_action == "drop"
        # The pre-drop write landed; the post-drop write did NOT.
        assert r.labels["r"] == "antarctica"
        assert "never_set" not in r.labels

    def test_l106_worked_example_for_BR(self):
        """The exact chain from L-106 §3 should expand BR to the documented
        materialized labels."""
        c = CandidateTarget(
            natural_key="BR",
            label_set={
                "country_iso2": "BR",
                "country_languages": ["pt-BR"],
                "region": "south_america",
            },
        )
        rules = [
            RelabelRule(
                source_labels=["country_iso2"],
                target_label="scope.geo",
                action="set_list",
            ),
            RelabelRule(
                source_labels=["country_iso2", "country_languages"],
                target_label="scope.languages",
                action="lookup_languages",
            ),
            RelabelRule(
                source_labels=["scope.languages"],
                target_label="scope.languages",
                action="merge_list",
                extend_with=["en"],
            ),
            RelabelRule(
                source_labels=["country_iso2"],
                target_label="id",
                action="format",
                replacement="country_news_{{ country_iso2 | lower }}",
            ),
            RelabelRule(
                source_labels=["region"],
                action="keep",
                predicate="region != 'antarctica'",
            ),
        ]
        r = evaluate_relabel_chain(c, rules)
        assert r.kept is True
        assert r.labels["scope"]["geo"] == ["BR"]
        assert r.labels["scope"]["languages"] == ["pt-BR", "en"]
        assert r.labels["id"] == "country_news_br"

    def test_l106_worked_example_drops_antarctica(self):
        c = CandidateTarget(
            natural_key="AQ",
            label_set={
                "country_iso2": "AQ",
                "country_languages": [],
                "region": "antarctica",
            },
        )
        rules = [
            RelabelRule(
                source_labels=["country_iso2"],
                target_label="scope.geo",
                action="set_list",
            ),
            RelabelRule(
                source_labels=["region"],
                action="keep",
                predicate="region != 'antarctica'",
            ),
            RelabelRule(
                source_labels=["country_iso2"],
                target_label="id",
                action="format",
                replacement="country_news_{{ country_iso2 | lower }}",
            ),
        ]
        r = evaluate_relabel_chain(c, rules)
        assert r.dropped is True
        assert r.dropped_by_action == "keep"
        # The pre-drop set_list landed; the post-drop format did not.
        assert r.labels["scope"]["geo"] == ["AQ"]
        assert "id" not in r.labels

    def test_chain_returns_relabel_result(self):
        c = CandidateTarget(natural_key="X", label_set={"a": 1})
        r = evaluate_relabel_chain(c, rules=[])
        assert isinstance(r, RelabelResult)
        assert r.kept is True
        assert dict(r.labels) == {"a": 1}


# ---------------------------------------------------------------------------
# Disappearance-ratio
# ---------------------------------------------------------------------------


class TestDisappearance:
    def test_default_threshold_is_zero_point_three_zero(self):
        assert DEFAULT_DISAPPEARANCE_RATIO_THRESHOLD == 0.30

    def test_under_threshold_proceeds(self):
        # 20 prior, 4 disappeared = 0.20 ratio → under 0.30
        prior = [f"K{i}" for i in range(20)]
        current = [f"K{i}" for i in range(4, 20)]  # drop K0..K3
        d = evaluate_disappearance(prior, current)
        assert d.verdict == "proceed"
        assert d.ratio == pytest.approx(0.20)
        assert d.should_retire_disappeared is True
        assert d.should_pause is False
        assert d.should_alert is False
        assert d.routes_to_dlq == []
        assert d.disappeared == ["K0", "K1", "K2", "K3"]
        assert d.new == []
        assert len(d.retained) == 16

    def test_over_threshold_triggers_anomaly(self):
        # 20 prior, 8 disappeared = 0.40 ratio → over 0.30
        prior = [f"K{i}" for i in range(20)]
        current = [f"K{i}" for i in range(8, 20)]
        d = evaluate_disappearance(prior, current)
        assert d.verdict == "anomaly"
        assert d.ratio == pytest.approx(0.40)
        assert d.should_pause is True
        assert d.should_alert is True
        assert d.should_retire_disappeared is False
        # Disappeared keys route to DLQ payload.
        assert d.routes_to_dlq == [f"K{i}" for i in range(8)]

    def test_exactly_at_threshold_proceeds(self):
        """Threshold semantics: strictly greater than triggers anomaly.

        Per L-106 §5 'more than 30%' — 30.0% does not trip.
        """
        prior = [f"K{i}" for i in range(10)]
        current = [f"K{i}" for i in range(3, 10)]  # drop 3 / 10 = 0.30
        d = evaluate_disappearance(prior, current)
        assert d.ratio == pytest.approx(0.30)
        assert d.verdict == "proceed"

    def test_cold_start_skips_check(self):
        d = evaluate_disappearance([], ["K0", "K1"])
        assert d.verdict == "skipped"
        assert d.prior_count == 0
        assert d.ratio == 0.0
        assert d.new == ["K0", "K1"]

    def test_min_prior_active_bypass(self):
        # 5 prior < min_prior_active=10 → skipped
        prior = [f"K{i}" for i in range(5)]
        current = ["K0"]  # 4/5 disappeared = 80%
        d = evaluate_disappearance(prior, current)
        assert d.verdict == "skipped"
        assert d.ratio == pytest.approx(0.8)

    def test_threshold_zero_disables_check(self):
        prior = [f"K{i}" for i in range(20)]
        current: list[str] = []  # 100% disappeared
        d = evaluate_disappearance(
            prior, current, policy=ResyncPolicy(disappearance_ratio_threshold=0.0)
        )
        assert d.verdict == "proceed"
        assert d.should_retire_disappeared is True

    def test_alert_only_policy_alerts_but_proceeds(self):
        prior = [f"K{i}" for i in range(20)]
        current = [f"K{i}" for i in range(15, 20)]
        d = evaluate_disappearance(
            prior, current,
            policy=ResyncPolicy(on_anomaly="alert_only"),
        )
        assert d.verdict == "anomaly"
        assert d.should_alert is True
        assert d.should_pause is False

    def test_retire_anyway_policy_skips_dlq_routing(self):
        prior = [f"K{i}" for i in range(20)]
        current = [f"K{i}" for i in range(15, 20)]
        d = evaluate_disappearance(
            prior, current,
            policy=ResyncPolicy(on_anomaly="retire_anyway"),
        )
        assert d.verdict == "anomaly"
        assert d.should_retire_disappeared is True
        assert d.routes_to_dlq == []  # retire_anyway → no DLQ block

    def test_classification_partitions_keys(self):
        prior = ["A", "B", "C", "D"]
        current = ["B", "C", "E"]
        d = evaluate_disappearance(
            prior, current,
            policy=ResyncPolicy(min_prior_active=0),
        )
        assert d.disappeared == ["A", "D"]
        assert d.retained == ["B", "C"]
        assert d.new == ["E"]
        assert d.ratio == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Discovery-kind registry
# ---------------------------------------------------------------------------


class TestDiscoveryKindRegistry:
    def test_static_sentinel_is_always_present(self):
        registry = discover_discovery_kinds()
        assert STATIC_KIND_NAME in registry
        bundle = registry[STATIC_KIND_NAME]
        assert bundle.is_static is True
        assert bundle.materialize_static is not None
        # Static path has no async discover() callable.
        assert bundle.discover is None

    def test_first_party_kinds_register_when_present(self):
        """Wave B (L-181 / L-182) lands kinds under
        ``legba.data.discovery.country_list_discovery`` and
        ``legba.data.discovery.file_sd_discovery``. They are NOT
        required at L-180 time; the walker tolerates absence.
        """
        registry = discover_discovery_kinds()
        # Pre-Wave-B: only the static sentinel is expected.
        # Post-Wave-B: country_list / file_sd kinds also appear.
        for kind_name in registry:
            bundle = registry[kind_name]
            if kind_name == STATIC_KIND_NAME:
                continue
            # Non-static kinds expose a callable discover() entry.
            assert bundle.discover is not None
            assert bundle.is_static is False
