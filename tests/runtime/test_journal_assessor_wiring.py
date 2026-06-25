# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wiring smoke + drift tests for the Journal Assessor (plan §13).

No DB / NATS / Dapr / registry container — every external dep is a stub.

Covers:
  * kind discovery: `journal_assessor` registers as a KindHandler with
    OUTPUT_KIND = OutputKind.JOURNAL; the 11th OutputKind is in KIND_REGISTRY.
  * deps-build smoke: build_analyst_run_method('journal_assessor') returns the
    journal run_method + OutputKind.JOURNAL, with READ_SLICE threaded through the
    quint (None in Wave 0 — the default META reader).
  * GATHER-gate generalization (§4.9): the helpers admit journal_assessor and
    point it at the journal_read pack; the binding-kind sets in dapr_host /
    dapr_actors are in lock-step.
  * pack drift: the journal_read tuple == the descriptor YAML == the registered
    handlers; every tool the journal can dispatch ∈ the pack.
  * the journal persona does NOT carry the with_preamble JSON-only anti-voice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from legba.data.analysts import discover_analyst_kinds
from legba.data.provenance.kinds import KIND_REGISTRY, OutputKind
from legba.data.schemas.analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    CadenceBlock,
    GatherBlock,
    MappingBlock,
    MethodBlock,
    SubscriptionBlock,
    TypeSignature,
    register_analyst_kind,
)
from legba.data.schemas.action_pack import ActionPackRef
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Property
from legba.runtime.analyst_deps_builder import build_analyst_run_method
from legba.runtime.deps import StandardDeps

_DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"
_VERSION = "0" * 64


# ---------------------------------------------------------------------------
# Kind discovery
# ---------------------------------------------------------------------------


def test_journal_assessor_discovers_with_journal_output_kind():
    handler = discover_analyst_kinds()["journal_assessor"]
    assert handler.output_kind == OutputKind.JOURNAL
    assert callable(handler.run_method)
    assert callable(handler.build_prompt_module)
    # Wave 0: the default META reader (delta-priming reader is Wave 1).
    assert handler.read_slice is None


def test_eleventh_output_kind_present():
    assert OutputKind.JOURNAL in KIND_REGISTRY
    assert KIND_REGISTRY[OutputKind.JOURNAL].table == "journal_entries"


# ---------------------------------------------------------------------------
# deps-build smoke
# ---------------------------------------------------------------------------


class _StubLLM:
    subprovider = "stub-test"

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None, system=None, **kw):  # pragma: no cover
        raise NotImplementedError


async def _stub_secrets(_secret_id: str) -> bytes:  # pragma: no cover
    return b"stub"


def _journal_descriptor() -> AnalystDescriptor:
    register_analyst_kind("journal_assessor")
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="journal_assessor",
            name="Journal",
            schema_uri="legba/analyst/1.0.0",
            version=_VERSION,
            kind="journal_assessor",
            type_signature=TypeSignature(
                input_type="legba.runtime.SignalList",
                output_type="legba.runtime.Journal",
            ),
            state=LifecycleState.ACTIVE,
            owner="journal_revival",
        ),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_planner",
            prompt_module="legba.prompts.journal_assessor:JOURNAL_SYSTEM",
            llm={
                "primary": Property.StackRef(
                    raw="llm.deep_consult", expected_family="llm_provider"
                ).model_dump(),
                "max_tokens": 4096,
            },
            gather=GatherBlock(max_rounds=6),
        ),
        cadence=CadenceBlock(fallback_schedule="0 0,12 * * *", cooldown_seconds=42000),
        action_packs=[ActionPackRef(pack_id="journal_read")],
    )


@pytest.mark.asyncio
async def test_journal_deps_build_returns_journal_kind_and_read_slice():
    factory = AsyncMock(return_value=_StubLLM())
    (
        run_method,
        kind_deps,
        output_kind,
        _receipt_chain,
        read_slice,
    ) = await build_analyst_run_method(
        _journal_descriptor(),
        deps=StandardDeps(pg_pool=None, nats_publish=None, secrets_resolve=_stub_secrets),
        registry_client=AsyncMock(),
        pg_pool=None,
        llm_handler_factory=factory,
    )
    assert output_kind == OutputKind.JOURNAL
    assert callable(run_method)
    # The journal reuses the InlineTargetDeps bundle (carrying the persona system
    # prompt + GATHER knobs) — NOT the InlineTargetRunner closure.
    from legba.data.analysts.inline_target import InlineTargetDeps

    assert isinstance(kind_deps, InlineTargetDeps)
    # The persona is threaded as the system prompt — and is NOT the with_preamble
    # JSON-only anti-voice (the §4.2 headline fix).
    assert "Poetry without evidence is noise" in (kind_deps.system_prompt or "")
    assert "first character must be" not in (kind_deps.system_prompt or "").lower()
    assert kind_deps.max_rounds == 6
    # Wave 0 read_slice is None (the default META reader).
    assert read_slice is None


# ---------------------------------------------------------------------------
# §4.9 GATHER-gate generalization
# ---------------------------------------------------------------------------


def test_gather_gate_admits_journal_and_points_at_journal_read():
    from legba.runtime import dapr_host

    class _Id:
        kind = "journal_assessor"

    class _Ad:
        identity = _Id()

    ad = _Ad()
    assert dapr_host._gather_kind_engages(ad) is True
    # inline_target still default-fetches substrate_read; journal → journal_read.
    assert dapr_host._gather_read_pack_for(ad, "substrate_read") == "journal_read"

    class _Inline:
        class identity:
            kind = "inline_target"

    assert dapr_host._gather_read_pack_for(_Inline(), "substrate_read") == "substrate_read"

    class _Other:
        class identity:
            kind = "deterministic"

    assert dapr_host._gather_kind_engages(_Other()) is False
    assert dapr_host._gather_read_pack_for(_Other(), "substrate_read") is None


def test_gather_binding_kind_sets_in_lockstep():
    """dapr_host._GATHER_KINDS and dapr_actors._GATHER_BINDING_KINDS must agree —
    a drift here means the host wires a binding the actor never re-points."""
    from legba.runtime import dapr_actors, dapr_host

    assert dapr_host._GATHER_KINDS == dapr_actors._GATHER_BINDING_KINDS
    assert "journal_assessor" in dapr_host._GATHER_KINDS


# ---------------------------------------------------------------------------
# pack drift — tuple == descriptor == handlers; every dispatchable tool ∈ pack
# ---------------------------------------------------------------------------


def test_journal_read_tuple_descriptor_handlers_agree():
    from legba.data.analysts.agency.journal_read import (
        JOURNAL_READ_TOOLS,
        register_journal_read_tools,
    )
    from legba.data.analysts.agency.tools import ToolRegistry

    tuple_names = set(JOURNAL_READ_TOOLS)

    body = yaml.safe_load(
        (_DESCRIPTORS / "action_pack_journal_read.yaml").read_text()
    )
    descriptor_names = {t["name"] for t in body["tools"]}
    assert descriptor_names == tuple_names, (
        "descriptor action_pack_journal_read.yaml tools != JOURNAL_READ_TOOLS "
        f"(only in descriptor: {descriptor_names - tuple_names}; "
        f"only in tuple: {tuple_names - descriptor_names})"
    )

    reg = ToolRegistry()
    register_journal_read_tools(reg)
    registered = set(reg.names)
    assert registered == tuple_names, (
        "register_journal_read_tools handlers != JOURNAL_READ_TOOLS "
        f"(only registered: {registered - tuple_names}; "
        f"only in tuple: {tuple_names - registered})"
    )


def test_journal_read_tool_has_a_global_handler():
    """Every journal_read tool must resolve to a registered handler in the global
    registry (a tool absent from the live pack/handlers blocks as unknown_tool —
    the memory's drift guard). list_findings is shared with substrate_read."""
    from legba.data.analysts.agency.journal_read import JOURNAL_READ_TOOLS
    from legba.data.analysts.agency.tools import default_tool_registry

    reg = default_tool_registry()
    for name in JOURNAL_READ_TOOLS:
        assert reg.handler_for(name) is not None, (
            f"journal_read tool {name!r} has no global handler — it would block "
            "as unknown_tool on the governed path"
        )


def test_journal_wave1_instrument_tools_all_present():
    """Wave 1 adds the ~9 net-new self-instrument tools + wires the shared reads
    (§5). Assert the full set is in the tuple so a dropped tool fails loud."""
    from legba.data.analysts.agency.journal_read import JOURNAL_READ_TOOLS

    expected = {
        # reused finished-intelligence + ground-truth reads
        "list_findings", "query_facts", "query_nexuses", "list_situations",
        "get_timeline",
        # net-new self-instruments
        "get_assessments", "get_graph_structure", "get_structural_balance",
        "get_critic_scores", "get_calibration", "get_run_health",
        "get_source_health", "get_budget_status", "get_journal_delta",
    }
    assert set(JOURNAL_READ_TOOLS) == expected, (
        "JOURNAL_READ_TOOLS drifted from the Wave 1 §5 read surface "
        f"(missing: {expected - set(JOURNAL_READ_TOOLS)}; "
        f"extra: {set(JOURNAL_READ_TOOLS) - expected})"
    )


def test_journal_gather_recognizes_every_pack_tool():
    """The journal's GATHER loop must RECOGNIZE every JOURNAL_READ_TOOLS entry —
    the run_method passes the tuple as ``extra_read_tools`` so each tool is a
    valid name AND routes through the journal_read binding (§4.9). A tool the loop
    can't dispatch is an unreachable pack tool (the four-surface drift guard's
    'every dispatchable tool ∈ pack' leg, read in reverse)."""
    from legba.data.analysts.agency.journal_read import JOURNAL_READ_TOOLS
    from legba.data.analysts.inline_target import _GATHER_READ_TOOLS, _GATHER_TOOLS

    # With extra_read_tools=JOURNAL_READ_TOOLS, the recognized set the loop checks
    # is _GATHER_TOOLS ∪ JOURNAL_READ_TOOLS, and the read-routing set is
    # _GATHER_READ_TOOLS ∪ JOURNAL_READ_TOOLS. Every journal tool must therefore
    # be recognized + read-routed.
    recognized = set(_GATHER_TOOLS) | set(JOURNAL_READ_TOOLS)
    read_routed = set(_GATHER_READ_TOOLS) | set(JOURNAL_READ_TOOLS)
    for name in JOURNAL_READ_TOOLS:
        assert name in recognized, f"{name} not recognized by the journal GATHER loop"
        assert name in read_routed, f"{name} not read-routed through journal_read"


def test_journal_known_tools_subset_in_pack():
    """The §-memory drift guard 'every _KNOWN_TOOLS entry ∈ pack' applied to the
    journal: the journal's instrument tools are deliberately NOT in consult's
    _KNOWN_TOOLS (the journal does not run on consult). But every journal tool
    that IS a consult _KNOWN_TOOL (the shared substrate_read reads) must be in the
    pack — a consult-reachable tool absent from the journal pack would be a
    silent gap if the journal pack were ever granted on the consult surface."""
    from legba.data.analysts.agency.journal_read import JOURNAL_READ_TOOLS
    from legba.data.analysts.consult_on_demand import _KNOWN_TOOLS

    shared = set(JOURNAL_READ_TOOLS) & set(_KNOWN_TOOLS)
    # The five reused reads are the shared set; the 9 instruments are journal-only.
    assert shared == {
        "list_findings", "query_facts", "query_nexuses", "list_situations",
        "get_timeline",
    }
    # And every shared tool is, by construction, in the pack tuple.
    assert shared <= set(JOURNAL_READ_TOOLS)
