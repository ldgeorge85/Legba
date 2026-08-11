# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the vendored L-101 pydantic schemas."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from legba.data.schemas import (
    AbstractionLevel,
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    EvalBlock,
    FieldMapping,
    LifecycleState,
    LifecycleTransition,
    MappingBlock,
    MethodBlock,
    Property,
    SubscriptionBlock,
    SubscriptionTargets,
    TargetDescriptor,
    TargetIdentity,
    TargetScope,
    TypeSignature,
    content_hash,
)
# Source-first pivot: TargetScope is now a discriminated union (geo/estate/
# entity). The founding geopolitical case is GeoScope — construct it directly
# in tests instead of the (no-longer-callable) union alias. See rss.py /
# schemas/target.py for the post-pivot shape.
from legba.data.schemas.target import GeoScope
from legba.data.schemas.properties import (
    Cron,
    DropdownStatic,
    Number,
    RateLimit,
    Secret,
    StackRef,
    Text,
)
from legba.data.schemas.versioning import ConversionWebhook
from legba.data.schemas.vocabulary import VocabularyEntry, VocabularyRegistry


# ---------------------------------------------------------------------------
# Property factories
# ---------------------------------------------------------------------------


def test_secret_factory():
    s = Property.Secret.of("creds.gdelt.api_key")
    assert s.raw == "creds.gdelt.api_key"
    assert s.factory_kind == "secret"
    assert s.ui_hint == {"masked": True}


def test_secret_factory_rejects_bad_name():
    with pytest.raises(ValueError):
        Property.Secret.of("")
    with pytest.raises(ValueError):
        Property.Secret.of("not a name")
    with pytest.raises(ValueError):
        Property.Secret.of("with/slash")


def test_text_factory_validates_regex():
    t = Property.Text.of("BRA", regex=r"^[A-Z]{3}$")
    assert t.raw == "BRA"
    with pytest.raises(Exception):
        Property.Text.of("x", regex="[")  # bad regex


def test_number_factory_bounds():
    n = Property.Number.of(60, minimum=1, maximum=600)
    assert n.raw == 60
    with pytest.raises(ValueError):
        Property.Number.of(0, minimum=10, maximum=5)


def test_rate_limit_factory():
    rl = Property.RateLimit.of("60/min")
    assert rl.requests_per_second == 1.0
    with pytest.raises(ValueError):
        Property.RateLimit.of("60/yikes")


def test_dropdown_static_factory():
    d = Property.Dropdown.Static.of("primary", ["primary", "fallback"])
    assert d.raw == "primary"
    with pytest.raises(ValueError):
        Property.Dropdown.Static.of("X", ["primary", "fallback"])


def test_stack_ref():
    s = Property.StackRef(raw="llm.anthropic.opus_4_7", expected_family="stack/llm_provider")
    assert s.factory_kind == "stack_ref"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_legal_transition():
    t = LifecycleTransition(
        descriptor_id="x",
        descriptor_kind="target",
        from_state=LifecycleState.DRAFT,
        to_state=LifecycleState.CONFIGURED,
        at=datetime.now(tz=timezone.utc),
        actor="lewis",
    )
    assert t.to_state == LifecycleState.CONFIGURED


def test_illegal_transition_raises():
    with pytest.raises(ValueError, match="illegal transition"):
        LifecycleTransition(
            descriptor_id="x",
            descriptor_kind="target",
            from_state=LifecycleState.RETIRED,
            to_state=LifecycleState.ACTIVE,
            at=datetime.now(tz=timezone.utc),
            actor="lewis",
        )


# ---------------------------------------------------------------------------
# Target descriptor
# ---------------------------------------------------------------------------


_FIXED_CREATED = datetime(2026, 5, 16, 0, 0, 0, tzinfo=timezone.utc)


def _draft_target_identity(target_id: str = "india_energy") -> TargetIdentity:
    return TargetIdentity(
        id=target_id,
        name="Brazil Energy",
        schema_uri="legba/target/2.0.0",
        version="abcd1234ef567890abcd1234ef567890",
        abstraction_level=AbstractionLevel.L1,
        state=LifecycleState.DRAFT,
        owner="lewis@local",
        created=_FIXED_CREATED,
    )


def test_target_descriptor_minimal():
    td = TargetDescriptor(
        identity=_draft_target_identity(),
        scope=GeoScope(
            geo=["BR"],
            languages=["pt-BR"],
            entity_classes=["organization", "country"],
            time_horizon_days=90,
        ),
    )
    assert td.identity.id == "india_energy"


def test_target_descriptor_active_requires_sources():
    identity = _draft_target_identity().model_copy(update={"state": LifecycleState.ACTIVE})
    with pytest.raises(ValueError, match="must declare at least one source"):
        TargetDescriptor(
            identity=identity,
            scope=GeoScope(
                geo=["BR"], languages=["en"], entity_classes=["entity"],
            ),
        )


def test_content_hash_excludes_version_field():
    td1 = TargetDescriptor(
        identity=_draft_target_identity().model_copy(update={"version": "a" * 16}),
        scope=GeoScope(geo=["BR"], languages=["en"], entity_classes=["entity"]),
    )
    td2 = TargetDescriptor(
        identity=_draft_target_identity().model_copy(update={"version": "b" * 16}),
        scope=GeoScope(geo=["BR"], languages=["en"], entity_classes=["entity"]),
    )
    assert content_hash(td1) == content_hash(td2), (
        "content_hash must be independent of identity.version"
    )


# ---------------------------------------------------------------------------
# Analyst descriptor
# ---------------------------------------------------------------------------


def _draft_analyst(kind: AnalystKind = AnalystKind.INLINE_TARGET) -> AnalystDescriptor:
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="critic_v2",
            name="Critic v2",
            schema_uri="legba/analyst/2.0.0",
            version="a" * 32,
            kind=kind,
            type_signature=TypeSignature(
                input_type="legba.x.In",
                output_type="legba.x.Out",
            ),
            owner="lewis@local",
        ),
        subscription=SubscriptionBlock(),
        method=MethodBlock(kind="llm_planner", prompt_module="legba.prompts.x"),
        cadence=CadenceBlock(),
    )


def test_analyst_minimal():
    a = _draft_analyst()
    assert a.identity.kind == AnalystKind.INLINE_TARGET


def test_analyst_optimizer_requires_eval():
    with pytest.raises(ValueError, match="optimizer analyst must declare an eval"):
        _draft_analyst(AnalystKind.OPTIMIZER)


def test_analyst_critic_requires_llm_method():
    with pytest.raises(ValueError, match="critic analyst method.kind must be an LLM"):
        AnalystDescriptor(
            identity=AnalystIdentity(
                id="critic_d",
                name="Det critic",
                schema_uri="legba/analyst/2.0.0",
                version="a" * 32,
                kind=AnalystKind.CRITIC,
                type_signature=TypeSignature(
                    input_type="x.In",
                    output_type="x.Out",
                ),
                owner="lewis@local",
            ),
            subscription=SubscriptionBlock(),
            method=MethodBlock(kind="deterministic", impl="legba.det.run"),
            cadence=CadenceBlock(),
        )


def test_method_block_requires_prompt_for_llm():
    with pytest.raises(ValueError):
        MethodBlock(kind="llm_planner")


# ---------------------------------------------------------------------------
# Wave B prereq: extended MethodBlock.kind Literal set
# ---------------------------------------------------------------------------


def test_method_block_react_loop_requires_prompt_module():
    """``react_loop`` is LLM-bearing — prompt_module required."""
    with pytest.raises(ValueError, match="requires prompt_module"):
        MethodBlock(kind="react_loop")
    # With prompt_module it passes.
    m = MethodBlock(kind="react_loop", prompt_module="legba.prompts.consult_on_demand.v1")
    assert m.kind == "react_loop"


def test_method_block_stat_forecaster_does_not_require_prompt_module():
    """``stat_forecaster`` has an optional LLM narrative — no required prompt_module."""
    # No prompt_module is fine — the narrative LLM is decorative.
    m = MethodBlock(kind="stat_forecaster")
    assert m.kind == "stat_forecaster"
    # With one it still passes.
    m2 = MethodBlock(kind="stat_forecaster", prompt_module="legba.prompts.predictor.v1")
    assert m2.prompt_module == "legba.prompts.predictor.v1"


def test_method_block_critic_requires_prompt_module():
    """``critic`` (L-175) is LLM-bearing — prompt_module required."""
    with pytest.raises(ValueError, match="requires prompt_module"):
        MethodBlock(kind="critic")
    m = MethodBlock(kind="critic", prompt_module="legba.prompts.critic.v1")
    assert m.kind == "critic"


def test_method_block_dspy_compile_does_not_require_prompt_module():
    """``dspy_compile`` (L-176 optimizer) operates over OTHER kinds' modules.

    It carries no prompt_module of its own at the MethodBlock level — the
    optimizer's per-trial prompts live in the candidate dspy.Module being
    compiled, not in the optimizer descriptor itself.
    """
    m = MethodBlock(kind="dspy_compile")
    assert m.kind == "dspy_compile"


def test_method_block_unknown_kind_rejected():
    """Closed Literal — bogus values fail Pydantic validation."""
    with pytest.raises(ValueError):
        MethodBlock(kind="totally_made_up_kind")


def test_analyst_critic_accepts_critic_method_kind():
    """The L-175 critic kind can declare ``method.kind = 'critic'`` directly."""
    a = AnalystDescriptor(
        identity=AnalystIdentity(
            id="critic_a",
            name="LLM critic",
            schema_uri="legba/analyst/2.0.0",
            version="a" * 32,
            kind=AnalystKind.CRITIC,
            type_signature=TypeSignature(
                input_type="x.In",
                output_type="x.Out",
            ),
            owner="lewis@local",
        ),
        subscription=SubscriptionBlock(),
        method=MethodBlock(kind="critic", prompt_module="legba.prompts.critic.v1"),
        cadence=CadenceBlock(),
    )
    assert a.method.kind == "critic"


# ---------------------------------------------------------------------------
# Conversion webhook
# ---------------------------------------------------------------------------


def test_conversion_webhook_same_family():
    w = ConversionWebhook(
        from_uri="legba/target/2.0.0",
        to_uri="legba/target/3.0.0",
        impl="legba.conversions.target_2_to_3",
    )
    assert w.direction == "forward"


def test_conversion_webhook_cross_family_rejected():
    with pytest.raises(ValueError, match="cross-family conversion not allowed"):
        ConversionWebhook(
            from_uri="legba/target/2.0.0",
            to_uri="legba/analyst/2.0.0",
            impl="legba.conversions.bad",
        )


# ---------------------------------------------------------------------------
# Vocabulary entry
# ---------------------------------------------------------------------------


def test_vocabulary_entry_relationship_pascal():
    e = VocabularyEntry(
        family="relationship_type",
        value="LocatedIn",
        schema_uri="legba/vocabulary/1.0.0",
        introduced=datetime.now(tz=timezone.utc),
    )
    assert e.value == "LocatedIn"


def test_vocabulary_entry_relationship_rejects_snake():
    with pytest.raises(ValueError, match="PascalCase"):
        VocabularyEntry(
            family="relationship_type",
            value="located_in",
            schema_uri="legba/vocabulary/1.0.0",
            introduced=datetime.now(tz=timezone.utc),
        )


def test_vocabulary_entry_entity_class_snake():
    e = VocabularyEntry(
        family="entity_class",
        value="energy_event",
        schema_uri="legba/vocabulary/1.0.0",
        introduced=datetime.now(tz=timezone.utc),
    )
    assert e.value == "energy_event"


def test_vocabulary_registry_values():
    e1 = VocabularyEntry(
        family="entity_class", value="entity",
        schema_uri="legba/vocabulary/1.0.0",
        introduced=datetime.now(tz=timezone.utc),
    )
    e2 = VocabularyEntry(
        family="entity_class", value="retired",
        schema_uri="legba/vocabulary/1.0.0",
        introduced=datetime.now(tz=timezone.utc),
        deprecated=datetime.now(tz=timezone.utc),
    )
    r = VocabularyRegistry(entries=[e1, e2])
    assert r.values("entity_class") == {"entity"}


# ---------------------------------------------------------------------------
# AnalystKind open taxonomy (L-241)
# ---------------------------------------------------------------------------


from legba.data.schemas import (  # noqa: E402  — late import, lines above keep their layout
    ANALYST_KIND_REGISTRY,
    AnalystKindRegistry,
    is_known_analyst_kind,
    register_analyst_kind,
)


def test_analystkind_enum_has_ten_builtins():
    """L-241: built-in kinds, including consult_on_demand + the PIECE 4
    deep_consult submit kind (anchor §5) + the PIECE A relationship_reifier
    (the reified-typed-Nexus producer) + the PIECE C competing_hypotheses
    (the ACH meta-analyst)."""
    values = {k.value for k in AnalystKind}
    assert values == {
        "inline_target",
        "cross_target_raw",
        "meta_findings_synthesizer",
        "deterministic",
        "predictor",
        "critic",
        "optimizer",
        "cross_analyst_correlator",
        "relationship_reifier",
        "competing_hypotheses",
        "consult_on_demand",
        "deep_consult",
    }


def test_analystkind_consult_on_demand_registers():
    """The new built-in kind validates without runtime registration."""
    a = _draft_analyst(AnalystKind.CONSULT_ON_DEMAND)
    assert a.identity.kind == AnalystKind.CONSULT_ON_DEMAND
    assert a.identity.kind == "consult_on_demand"


def test_analyst_kind_unknown_string_rejected():
    """Unknown extension kinds are rejected by the schema validator."""
    # Snapshot + restore to avoid leaking the test fixture to other tests.
    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    try:
        # Make sure it's not registered.
        for v in list(snapshot):
            if v == "wholly_new_kind":
                ANALYST_KIND_REGISTRY.unregister(v)
        with pytest.raises(ValueError, match="unknown analyst kind"):
            AnalystIdentity(
                id="x",
                name="X",
                schema_uri="legba/analyst/2.0.0",
                version="a" * 32,
                kind="wholly_new_kind",
                type_signature=TypeSignature(
                    input_type="x.In", output_type="x.Out",
                ),
                owner="lewis@local",
            )
    finally:
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


def test_analyst_kind_extension_register_then_validates():
    """Once registered, an extension kind passes schema validation."""
    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    try:
        register_analyst_kind("anomaly_explainer")
        assert is_known_analyst_kind("anomaly_explainer")
        ident = AnalystIdentity(
            id="ae_v1",
            name="Anomaly Explainer",
            schema_uri="legba/analyst/2.0.0",
            version="a" * 32,
            kind="anomaly_explainer",
            type_signature=TypeSignature(input_type="x.In", output_type="x.Out"),
            owner="lewis@local",
        )
        assert ident.kind == "anomaly_explainer"
    finally:
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


def test_analyst_kind_extension_shape_enforced():
    """Extension kinds must obey lowercase_snake_case."""
    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    try:
        with pytest.raises(ValueError, match="lowercase_snake_case"):
            register_analyst_kind("BadKind")
        with pytest.raises(ValueError, match="lowercase_snake_case"):
            register_analyst_kind("bad kind")
        # Empty/non-string rejected too.
        with pytest.raises(ValueError):
            register_analyst_kind("")
    finally:
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


def test_analyst_kind_registry_unregister_is_idempotent_and_safe():
    """Unregistering a built-in is silently a no-op."""
    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    try:
        ANALYST_KIND_REGISTRY.unregister("optimizer")
        # Still a valid built-in kind.
        assert is_known_analyst_kind("optimizer")
        # Unregistering an unknown extension is a no-op.
        ANALYST_KIND_REGISTRY.unregister("never_existed")
    finally:
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


def test_analyst_kind_registry_replace_extensions_resets_set():
    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    try:
        register_analyst_kind("temp_one")
        register_analyst_kind("temp_two")
        assert "temp_one" in ANALYST_KIND_REGISTRY.extension_values()
        ANALYST_KIND_REGISTRY.replace_extensions({"temp_three"})
        ext = ANALYST_KIND_REGISTRY.extension_values()
        assert ext == {"temp_three"}
        # Built-ins survive a replace.
        assert is_known_analyst_kind("optimizer")
    finally:
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


def test_analyst_kind_registry_replace_extensions_filters_builtins_and_garbage():
    """Built-ins and malformed values get dropped instead of being stored."""
    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    try:
        ANALYST_KIND_REGISTRY.replace_extensions(
            {"optimizer", "good_ext", "", "Bad Cased"}
        )
        ext = ANALYST_KIND_REGISTRY.extension_values()
        assert "optimizer" not in ext  # built-in, not an extension
        assert "good_ext" in ext
        assert "" not in ext
        # `Bad Cased` is malformed but `replace_extensions` is forgiving —
        # we drop it to avoid poisoning the cache after a noisy DB snapshot.
        assert "Bad Cased" not in ext
    finally:
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


def test_analyst_kind_field_accepts_enum_or_string():
    """The schema's `kind` field accepts both an `AnalystKind` enum value
    and the underlying string — they compare equal either way."""
    a_enum = AnalystIdentity(
        id="x", name="X",
        schema_uri="legba/analyst/2.0.0", version="a" * 32,
        kind=AnalystKind.CRITIC,
        type_signature=TypeSignature(input_type="x.In", output_type="x.Out"),
        owner="o",
    )
    a_str = AnalystIdentity(
        id="x", name="X",
        schema_uri="legba/analyst/2.0.0", version="a" * 32,
        kind="critic",
        type_signature=TypeSignature(input_type="x.In", output_type="x.Out"),
        owner="o",
    )
    assert a_enum.kind == a_str.kind == "critic" == AnalystKind.CRITIC


def test_analyst_kind_registry_isolated_instance():
    """Fresh `AnalystKindRegistry` instances start with only built-ins."""
    fresh = AnalystKindRegistry()
    assert fresh.builtin_values() == {k.value for k in AnalystKind}
    assert fresh.extension_values() == set()
    assert fresh.is_valid("optimizer")
    assert not fresh.is_valid("custom_x")
    fresh.register("custom_x")
    assert fresh.is_valid("custom_x")


# ---------------------------------------------------------------------------
# The dead-options warner must never raise out of a dep-light environment.
# Live incident 2026-08-04: the registry image lacks pycountry; the catalog
# import chain (deterministic_handlers -> entity_resolution -> geocode) made
# /typed 500 for every options-bearing deterministic analyst, silencing
# claim_watch and signal_embedder for 14h. Warn-only means warn-only.
# ---------------------------------------------------------------------------

def test_env_limited_dep_classifies_third_party_vs_legba():
    from legba.data.schemas.analyst import _env_limited_dep

    third = ModuleNotFoundError("No module named 'pycountry'", name="pycountry")
    assert _env_limited_dep(third) == "pycountry"
    ours = ModuleNotFoundError(
        "No module named 'legba.data.analysts.handler_options'",
        name="legba.data.analysts.handler_options",
    )
    assert _env_limited_dep(ours) is None


def test_warn_on_dead_options_skips_when_catalog_unimportable(monkeypatch):
    import legba.data.schemas.analyst as analyst_mod

    monkeypatch.setattr(analyst_mod, "_load_options_catalog", lambda: None)
    desc = analyst_mod.AnalystDescriptor(
        identity=AnalystIdentity(
            id="claim_watch",
            name="Claim watch",
            schema_uri="legba/analyst/2.0.0",
            version="a" * 32,
            kind=AnalystKind.DETERMINISTIC,
            type_signature=TypeSignature(
                input_type="legba.x.In", output_type="legba.x.Out",
            ),
            owner="lewis@local",
        ),
        subscription=SubscriptionBlock(),
        method=MethodBlock(
            kind="deterministic",
            impl="legba.data.analysts.deterministic",
            sub_handler="claim_watch",
            options={"contention_liveness_days": 14},
        ),
        cadence=CadenceBlock(),
    )
    # Must validate cleanly — the options check degrades to a log line.
    assert desc.identity.id == "claim_watch"


def test_load_options_catalog_reraises_on_missing_legba_module(monkeypatch):
    import builtins
    import legba.data.schemas.analyst as analyst_mod

    real_import = builtins.__import__

    def poisoned(name, *a, **k):
        if "handler_options" in name:
            raise ModuleNotFoundError(
                "No module named 'legba.data.analysts.handler_options'",
                name="legba.data.analysts.handler_options",
            )
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", poisoned)
    monkeypatch.delitem(
        __import__("sys").modules, "legba.data.analysts.handler_options",
        raising=False,
    )
    import pytest
    with pytest.raises(ModuleNotFoundError):
        analyst_mod._load_options_catalog()
