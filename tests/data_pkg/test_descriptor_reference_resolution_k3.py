# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-3 — descriptor string references resolve, and failing to resolve is LOUD.

Sixty-two descriptor rows name a Python module or a dispatch key by string.
Nothing in the import graph connects the two, so the only thing standing
between a rename and a silently-rewired analyst is a resolver that refuses to
swallow its own failure.

House rule (the ``journal_propose`` lesson: a capability was granted, wired,
tested, and never once invoked, because the test hand-built the binding the
production path was supposed to build): **every test here drives the real
resolver.** No test constructs a registry dict, stubs an importer, or asserts
on a hand-written reference list. The two shapes used are:

* drive the shipped function against the shipped code tree, or
* drive the shipped function against a deliberately-broken table, injected by
  monkeypatching *the same module-level tuple production reads*.

The most load-bearing test in the file is
:func:`test_every_shipped_descriptor_reference_resolves` — it extracts
references from ``descriptors/*.yaml`` with the production extractor and
resolves them with the production resolver, so a rename that breaks a
descriptor fails here rather than in production three weeks later.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from legba.data.kind_discovery import KindDiscoveryError
from legba.data.registry.descriptor_refs import (
    DescriptorReference,
    ReferenceStatus,
    ReferenceType,
    audit_references,
    extract_references,
    require_resolvable,
    resolve_reference,
)


DESCRIPTOR_DIR = Path(__file__).resolve().parents[2] / "descriptors"


# ---------------------------------------------------------------------------
# Layer 1 — boot reconcile: a declared module that is not there must raise
# ---------------------------------------------------------------------------


def test_analyst_discovery_raises_on_a_renamed_module(monkeypatch):
    """The rename scenario, on the real walker.

    Simulates exactly what a rename does: the loader table still names the old
    module. Before K-3 this logged one warning and returned a registry missing
    that kind, so every descriptor binding it failed later with 'unknown
    analyst kind' and nothing pointed at the rename.
    """
    from legba.data import analysts

    monkeypatch.setattr(
        analysts, "_KIND_MODULE_NAMES",
        analysts._KIND_MODULE_NAMES + ("deterministic_renamed_away",),
    )

    with pytest.raises(KindDiscoveryError) as exc_info:
        analysts.discover_analyst_kinds()

    message = str(exc_info.value)
    assert "deterministic_renamed_away" in message
    assert "import_failed" in message


def test_discovery_error_names_every_failure_not_just_the_first(monkeypatch):
    """One boot must report the whole blast radius.

    Raising on the first bad entry would make an operator fix a broken table
    one deploy at a time — the aggregate is the point.
    """
    from legba.data import analysts

    monkeypatch.setattr(
        analysts, "_KIND_MODULE_NAMES",
        analysts._KIND_MODULE_NAMES + ("gone_one", "gone_two", "gone_three"),
    )

    with pytest.raises(KindDiscoveryError) as exc_info:
        analysts.discover_analyst_kinds()

    assert len(exc_info.value.failures) == 3
    message = str(exc_info.value)
    for name in ("gone_one", "gone_two", "gone_three"):
        assert name in message


def test_analyst_discovery_raises_when_the_kind_contract_is_missing(monkeypatch):
    """A module that imports but exposes no ``KIND_NAME`` is equally invisible.

    ``legba.data.analysts.agency`` is a real sub-package with no kind contract
    — a plausible mis-entry, and previously a silent skip.
    """
    from legba.data import analysts

    monkeypatch.setattr(
        analysts, "_KIND_MODULE_NAMES", analysts._KIND_MODULE_NAMES + ("agency",),
    )

    with pytest.raises(KindDiscoveryError) as exc_info:
        analysts.discover_analyst_kinds()

    detail = str(exc_info.value)
    assert "missing_contract" in detail
    assert "KIND_NAME" in detail


def test_output_kind_discovery_raises_on_a_renamed_module(monkeypatch):
    """An unimportable output module used to silently stop ALL exports."""
    from legba.data import outputs

    monkeypatch.setattr(
        outputs, "_OUTPUT_KIND_MODULE_NAMES",
        outputs._OUTPUT_KIND_MODULE_NAMES + ("alert_renamed_away",),
    )

    with pytest.raises(KindDiscoveryError, match="alert_renamed_away"):
        outputs.discover_output_kinds()


def test_discovery_kind_registry_raises_on_a_renamed_module(monkeypatch):
    from legba.data.discovery import registry as discovery_registry

    monkeypatch.setattr(
        discovery_registry, "_KIND_MODULE_NAMES",
        discovery_registry._KIND_MODULE_NAMES + ("file_sd_renamed_away",),
    )

    with pytest.raises(KindDiscoveryError, match="file_sd_renamed_away"):
        discovery_registry.discover_discovery_kinds()


def test_source_discovery_raises_on_a_renamed_module(monkeypatch):
    from legba.runtime import source_factory

    monkeypatch.setattr(
        source_factory, "_SOURCE_MODULE_TABLE",
        source_factory._SOURCE_MODULE_TABLE + (("rss_renamed_away", "RSSSourceHandler"),),
    )

    with pytest.raises(KindDiscoveryError, match="rss_renamed_away"):
        source_factory.discover_source_kinds()


def test_source_discovery_raises_when_the_handler_class_is_renamed(monkeypatch):
    """The source table names a module AND a class, so it can be wrong twice."""
    from legba.runtime import source_factory

    monkeypatch.setattr(
        source_factory, "_SOURCE_MODULE_TABLE",
        source_factory._SOURCE_MODULE_TABLE + (("rss", "RSSHandlerRenamedAway"),),
    )

    with pytest.raises(KindDiscoveryError) as exc_info:
        source_factory.discover_source_kinds()

    assert "RSSHandlerRenamedAway" in str(exc_info.value)


def test_source_discovery_raises_on_a_duplicate_kind(monkeypatch):
    """Two classes claiming one kind is ambiguity, not degradation.

    'Keeping the first registration' made the winner depend on tuple order, so
    reordering the table would silently swap which handler every descriptor of
    that kind runs.
    """
    from legba.runtime import source_factory

    monkeypatch.setattr(
        source_factory, "_SOURCE_MODULE_TABLE",
        source_factory._SOURCE_MODULE_TABLE + (("rss", "RSSSourceHandler"),),
    )

    with pytest.raises(KindDiscoveryError) as exc_info:
        source_factory.discover_source_kinds()

    assert "duplicate_kind" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Layer 1 (positive) — the shipped tables are honest today
# ---------------------------------------------------------------------------


def test_all_four_shipped_loader_tables_load_clean():
    """The drift guard: every module named in every loader table must load.

    This is the test a rename breaks. It calls the four production discovery
    functions with no patching at all — if any of them starts raising, a
    loader table names something that no longer exists.
    """
    from legba.data.analysts import discover_analyst_kinds
    from legba.data.discovery.registry import discover_discovery_kinds
    from legba.data.outputs import discover_output_kinds
    from legba.runtime.source_factory import discover_source_kinds

    assert discover_analyst_kinds(), "analyst kind registry is empty"
    assert discover_output_kinds(), "output kind registry is empty"
    assert discover_discovery_kinds(), "discovery kind registry is empty"
    assert discover_source_kinds(), "source kind registry is empty"


def test_source_kinds_are_keyed_by_class_kind_not_module_name():
    """Pin the mismatch class the audit exists to catch.

    Five source handlers dispatch under a kind string that differs from their
    module filename. Anyone renaming ``discord.py`` would reasonably expect the
    kind to be ``discord``; it is ``discord_webhook``, and a descriptor naming
    the module name has never worked. Pinning it here means the divergence is
    a documented fact rather than a trap.
    """
    from legba.runtime.source_factory import discover_source_kinds

    kinds = discover_source_kinds()
    for module_name, kind in (
        ("discord", "discord_webhook"),
        ("common_crawl", "common_crawl_news"),
        ("telegram", "telegram_channel"),
        ("gdelt", "gdelt_query"),
        ("intelmq", "intelmq_collector_bridge"),
    ):
        assert kind in kinds, f"source kind {kind!r} vanished"
        assert module_name not in kinds, (
            f"{module_name!r} is a MODULE name, not a dispatch kind — "
            f"descriptors must name {kind!r}"
        )


# ---------------------------------------------------------------------------
# Layer 2 — reference extraction + resolution
# ---------------------------------------------------------------------------


def _analyst_ref(field_path: str, raw: str, ref_type: ReferenceType,
                 *, state: str = "active") -> DescriptorReference:
    return DescriptorReference(
        family="analyst", descriptor_id="t", version="1", state=state,
        field_path=field_path, raw=raw, ref_type=ref_type,
    )


def test_resolver_reports_a_live_prompt_constant_as_ok():
    ref = _analyst_ref(
        "method.prompt_module",
        "legba.prompts.journal_assessor:JOURNAL_SYSTEM",
        ReferenceType.MODULE_ATTR,
    )
    assert resolve_reference(ref).status is ReferenceStatus.OK


def test_resolver_reports_a_renamed_prompt_module_as_dead():
    ref = _analyst_ref(
        "method.prompt_module",
        "legba.prompts.journal_assessor_renamed:JOURNAL_SYSTEM",
        ReferenceType.MODULE_ATTR,
    )
    resolution = resolve_reference(ref)
    assert resolution.status is ReferenceStatus.DEAD
    assert "import failed" in resolution.detail


def test_resolver_reports_a_renamed_constant_as_dead():
    """The module survives the rename; the constant does not.

    This is the sneakiest shape — the module imports fine, so an
    import-only check would pass it.
    """
    ref = _analyst_ref(
        "method.prompt_module",
        "legba.prompts.journal_assessor:JOURNAL_SYSTEM_RENAMED",
        ReferenceType.MODULE_ATTR,
    )
    resolution = resolve_reference(ref)
    assert resolution.status is ReferenceStatus.DEAD
    assert "no attribute" in resolution.detail


def test_resolver_reports_a_non_string_prompt_constant_as_mismatch():
    """Resolving is not enough — the runtime only honours a ``str``.

    ``legba.prompts.journal_assessor`` certainly has a ``__name__``; pointing
    ``prompt_module`` at it resolves and is still wrong, and the runtime would
    drop it for the kind default without a word.
    """
    ref = _analyst_ref(
        "method.prompt_module",
        "legba.data.analysts.deterministic:SUB_HANDLERS",
        ReferenceType.MODULE_ATTR,
    )
    resolution = resolve_reference(ref)
    assert resolution.status is ReferenceStatus.MISMATCH
    assert "not str" in resolution.detail


def test_resolver_reports_an_unknown_sub_handler_as_dead():
    ref = _analyst_ref("method.sub_handler", "no_such_handler",
                       ReferenceType.SUB_HANDLER)
    assert resolve_reference(ref).status is ReferenceStatus.DEAD


def test_resolver_reports_an_unknown_analyst_kind_as_dead():
    ref = _analyst_ref("method.kind", "no_such_kind", ReferenceType.ANALYST_KIND)
    assert resolve_reference(ref).status is ReferenceStatus.DEAD


def test_resolver_reports_an_unknown_pack_tool_as_dead():
    ref = DescriptorReference(
        family="action_pack", descriptor_id="p", version="1", state="active",
        field_path="tools[0].name", raw="no_such_tool",
        ref_type=ReferenceType.PACK_TOOL,
    )
    assert resolve_reference(ref).status is ReferenceStatus.DEAD


def test_pack_tools_resolve_against_the_registry_the_agency_actually_uses():
    """Guard against auditing the wrong palette.

    ``consult_on_demand._KNOWN_TOOLS`` governs the consult surface only. An
    audit built on it reports every write / web / media tool as dead — nine
    false positives across four live packs. The palette must be
    ``default_tool_registry()``, the same builder agency dispatch uses.
    """
    for tool in ("process_media", "escalate", "create_incident",
                 "web_fetch", "web_search", "propose_fact"):
        ref = DescriptorReference(
            family="action_pack", descriptor_id="p", version="1", state="active",
            field_path="tools[0].name", raw=tool, ref_type=ReferenceType.PACK_TOOL,
        )
        assert resolve_reference(ref).status is ReferenceStatus.OK, tool


def test_deterministic_descriptor_without_sub_handler_is_reported_as_implicit():
    """The invisible binding must not read as 'no reference'.

    ``dapr_actors`` falls back to ``identity.id`` when ``method.sub_handler``
    is null, so a deterministic analyst can bind purely because its descriptor
    id happens to equal a ``SUB_HANDLERS`` key. It works, it is load-bearing,
    and renaming the descriptor id unbinds it with no error — so it has to
    appear in the audit, not vanish from it.
    """
    refs = extract_references(
        family="analyst",
        descriptor_id="cross_source_dedup",
        version="1",
        state="active",
        body={"method": {"kind": "deterministic", "sub_handler": None}},
        kind="deterministic",
    )
    implicit = [r for r in refs if r.ref_type is ReferenceType.IMPLICIT_SUB_HANDLER]
    assert len(implicit) == 1
    assert resolve_reference(implicit[0]).status is ReferenceStatus.IMPLICIT


def test_deterministic_descriptor_binding_by_neither_field_nor_id_is_dead():
    refs = extract_references(
        family="analyst",
        descriptor_id="not_a_handler_name",
        version="1",
        state="active",
        body={"method": {"kind": "deterministic", "sub_handler": None}},
        kind="deterministic",
    )
    implicit = [r for r in refs if r.ref_type is ReferenceType.IMPLICIT_SUB_HANDLER]
    assert resolve_reference(implicit[0]).status is ReferenceStatus.DEAD


def test_require_resolvable_raises_and_names_every_failure():
    from legba.data.registry.descriptor_refs import DescriptorReferenceError

    refs = [
        _analyst_ref("method.prompt_module", "legba.prompts.gone_a:X",
                     ReferenceType.MODULE_ATTR),
        _analyst_ref("method.sub_handler", "gone_b", ReferenceType.SUB_HANDLER),
    ]
    with pytest.raises(DescriptorReferenceError) as exc_info:
        require_resolvable(refs, context="unit")

    assert len(exc_info.value.failures) == 2
    assert "gone_a" in str(exc_info.value)
    assert "gone_b" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Layer 2 (positive) — the shipped descriptors are honest today
# ---------------------------------------------------------------------------


def _shipped_descriptor_references() -> list[DescriptorReference]:
    """Extract references from every ``descriptors/*.yaml`` with the real extractor."""
    refs: list[DescriptorReference] = []
    for path in sorted(DESCRIPTOR_DIR.glob("*.yaml")):
        body = yaml.safe_load(path.read_text()) or {}
        identity = body.get("identity") or {}
        if "method" in body:
            family = "analyst"
        elif "tools" in body or "channels" in body:
            family = "action_pack"
        elif path.name.startswith("source_"):
            family = "source"
        else:
            continue
        refs.extend(extract_references(
            family=family,
            descriptor_id=str(identity.get("id") or path.stem),
            version=str(identity.get("version") or "0"),
            state=str(identity.get("state") or "unknown"),
            body=body,
            kind=str(identity.get("kind") or "") or None,
        ))
    return refs


def test_shipped_descriptors_yield_references_at_all():
    """Guard the guard: an extractor that finds nothing would pass vacuously."""
    refs = _shipped_descriptor_references()
    assert len(refs) > 100, f"only {len(refs)} references extracted — extractor broke"
    found = {r.ref_type for r in refs}
    for expected in (ReferenceType.MODULE_ATTR, ReferenceType.SUB_HANDLER,
                     ReferenceType.ANALYST_KIND, ReferenceType.PACK_TOOL):
        assert expected in found, f"no {expected.value} references extracted"


def test_every_shipped_descriptor_reference_resolves():
    """THE drift guard. A rename that breaks a descriptor fails here.

    Runs the production extractor over ``descriptors/*.yaml`` and the
    production resolver over the result — the same two functions the registry
    write path calls. Non-binding states are excluded because a ``retired``
    descriptor may legitimately name a module that has since been deleted.
    """
    refs = [r for r in _shipped_descriptor_references()
            if r.state in ("active", "paused")]
    failures = [r for r in audit_references(refs) if r.failing]
    assert not failures, "\n".join(f.as_line() for f in failures)


# ---------------------------------------------------------------------------
# Layer 3 — runtime dispatch
# ---------------------------------------------------------------------------


def test_runtime_prompt_resolution_raises_on_a_renamed_module():
    """The persona-swap bug, on the real resolver.

    Returning ``None`` here is what every caller reads as 'no custom prompt
    declared', so a renamed ``lens_*`` module handed the lens analyst the
    generic journal persona and the run reported success.
    """
    from legba.runtime.analyst_deps_builder import (
        PromptModuleResolutionError, _resolve_prompt_module,
    )

    with pytest.raises(PromptModuleResolutionError) as exc_info:
        _resolve_prompt_module("legba.prompts.lens_diff_renamed:LENS_DIFF_SYSTEM",
                               analyst_id="lens_diff")

    assert "lens_diff" in str(exc_info.value)


def test_runtime_prompt_resolution_raises_on_a_renamed_constant():
    from legba.runtime.analyst_deps_builder import (
        PromptModuleResolutionError, _resolve_prompt_module,
    )

    with pytest.raises(PromptModuleResolutionError, match="no attribute"):
        _resolve_prompt_module("legba.prompts.lens_diff:LENS_DIFF_SYSTEM_RENAMED",
                               analyst_id="lens_diff")


def test_runtime_prompt_resolution_returns_the_declared_persona():
    """The success path must still return the exact declared constant."""
    from legba.prompts.lens_diff import LENS_DIFF_SYSTEM
    from legba.runtime.analyst_deps_builder import _resolve_prompt_module

    resolved = _resolve_prompt_module(
        "legba.prompts.lens_diff:LENS_DIFF_SYSTEM", analyst_id="lens_diff",
    )
    assert resolved == LENS_DIFF_SYSTEM


def test_runtime_prompt_resolution_returns_none_when_unset():
    from legba.runtime.analyst_deps_builder import _resolve_prompt_module

    assert _resolve_prompt_module(None) is None
    assert _resolve_prompt_module("") is None


def test_runtime_prompt_resolution_logs_a_dead_dspy_package_at_error(caplog):
    """The colon-less shape is not this path's business, but it is not silent.

    ``_resolve_prompt_module`` correctly returns ``None`` for a DSPy package
    path — gepa consumes those. A DEAD one still gets an ERROR naming the
    analyst, because ``None`` is the right answer here and therefore hides it.
    """
    from legba.runtime.analyst_deps_builder import _resolve_prompt_module

    with caplog.at_level(logging.ERROR):
        result = _resolve_prompt_module(
            "legba.prompts.does_not_exist.v1", analyst_id="some_analyst",
        )

    assert result is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("prompt_module.dead" in m for m in messages), (
        f"a dead prompt package must be logged at ERROR; got {messages}"
    )
    assert any("some_analyst" in m for m in messages), (
        "the ERROR must name the analyst, or it is unactionable"
    )


@pytest.mark.asyncio
async def test_optimizer_refuses_to_optimize_a_missing_prompt_module():
    """GEPA must not evolve a placeholder into a live system prompt.

    The old path returned ``<<missing prompt module: ...>>`` at
    ``logger.debug`` and handed that marker to the optimizer as the parent
    text — so a promoted candidate could be a mutation of a placeholder.
    """
    from legba.runtime.dapr_workflow.gepa import (
        PromptModuleImportError, _load_parent_prompt_text,
    )

    with pytest.raises(PromptModuleImportError) as exc_info:
        await _load_parent_prompt_text("legba.prompts.leadership_transition.v1")

    assert "leadership_transition" in str(exc_info.value)


@pytest.mark.skip(reason="optimizer plane mothballed 2026-08-21 (RUST-4)")
@pytest.mark.asyncio
async def test_optimizer_still_loads_a_real_parent_prompt():
    """The success path is untouched — the raise is narrow."""
    from legba.runtime.dapr_workflow.gepa import _load_parent_prompt_text

    text = await _load_parent_prompt_text("legba.prompts.country_assessor.v1")
    assert text and not text.startswith("<<missing")


def test_deterministic_dispatch_still_refuses_an_unknown_sub_handler():
    """Pin the one resolver that was already loud, so it stays that way."""
    from legba.data.analysts.deterministic import (
        DeterministicDispatchError, _resolve_sub_handler_name,
    )

    with pytest.raises(DeterministicDispatchError):
        _resolve_sub_handler_name({"sub_handler": "no_such_handler"})
    with pytest.raises(DeterministicDispatchError):
        _resolve_sub_handler_name({})


# ---------------------------------------------------------------------------
# ENV_LIMITED — a missing THIRD-PARTY dep is not a dangling reference.
# Found live 2026-08-04: the registry image ships without pycountry, so the
# reference gate 422'd a valid PUT of relationship_reifier (its impl chain
# imports pycountry). The gate exists to catch typos and renames in OUR tree;
# the runtime boot gate owns real imports with full deps.
# ---------------------------------------------------------------------------

from legba.data.registry.descriptor_refs import (  # noqa: E402
    FAILING_STATUSES,
    ReferenceStatus,
    _missing_dependency_name,
    _resolve_dotted_module,
    _resolve_module_attr,
)


def test_missing_dependency_name_classifies_third_party_vs_dangling():
    dep = ModuleNotFoundError("No module named 'pycountry'", name="pycountry")
    target = "legba.data.analysts.relationship_reifier"
    assert _missing_dependency_name(dep, target) == "pycountry"

    # The target itself (or its parent) missing = genuinely dangling.
    gone = ModuleNotFoundError(
        "No module named 'legba.prompts.foo'", name="legba.prompts.foo"
    )
    assert _missing_dependency_name(gone, "legba.prompts.foo.v1") is None
    assert _missing_dependency_name(gone, "legba.prompts.foo") is None

    # Non-ModuleNotFound import errors stay DEAD-class.
    assert _missing_dependency_name(ImportError("boom"), target) is None


def test_resolver_returns_env_limited_for_absent_third_party_dep(monkeypatch):
    import importlib

    def _raise(name):
        raise ModuleNotFoundError(
            "No module named 'pycountry'", name="pycountry"
        )

    monkeypatch.setattr(importlib, "import_module", _raise)
    status, detail, resolved = _resolve_dotted_module(
        "legba.data.analysts.relationship_reifier"
    )
    assert status is ReferenceStatus.ENV_LIMITED
    assert "pycountry" in detail

    status, detail, resolved, value = _resolve_module_attr(
        "legba.data.analysts.relationship_reifier:_SYSTEM_PROMPT"
    )
    assert status is ReferenceStatus.ENV_LIMITED


def test_env_limited_is_not_a_failing_status():
    assert ReferenceStatus.ENV_LIMITED not in FAILING_STATUSES


def test_resolver_still_dead_when_target_module_missing(monkeypatch):
    import importlib

    def _raise(name):
        raise ModuleNotFoundError(
            "No module named 'legba.prompts.ghost'", name="legba.prompts.ghost"
        )

    monkeypatch.setattr(importlib, "import_module", _raise)
    status, _, _ = _resolve_dotted_module("legba.prompts.ghost.v1")
    assert status is ReferenceStatus.DEAD
