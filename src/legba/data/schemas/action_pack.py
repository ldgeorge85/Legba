# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Action-pack descriptor schema — modular, allow-listed analyst capability.

Per ``docs/PIVOT_PROPOSAL.md`` §4.8 + ``docs/PIVOT_BUILD_PLAN.md`` P-04/P-11.
An ``ActionPack`` is a peer descriptor family (content-hashed, versioned,
audited) that bundles *(tools + prompt fragments/rules + escalation channels +
a per-pack governor + an applicability predicate)* — the Claude-Code-skill model
made declarative. Analysts declare ``action_packs`` (what they may use); targets
/ domain templates declare ``allowed_action_packs`` (what the context permits);
effective capability is the intersection, gated by each pack's governor.

Seed packs: ``media_processing`` (process_media), ``discovery``
(discover_sources), ``incident_response`` (escalate/create_incident → channels).
The pack *library* is incremental; this schema is the design-once spine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .lifecycle import AbstractionLevel, LifecycleState, WireEnumCoercion

ActionPackId = Annotated[
    str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$", max_length=128)
]
Tag = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]


class ActionPackIdentity(WireEnumCoercion):
    # strict=True + enum fields ⇒ the wire's bare strings need the mixin's
    # ``mode='before'`` coercers; see :class:`WireEnumCoercion`.
    model_config = ConfigDict(strict=True, extra="forbid")

    id: ActionPackId
    name: str
    schema_uri: str = Field(pattern=r"^legba/action_pack/\d+\.\d+\.\d+$")
    version: str = Field(pattern=r"^[a-f0-9]{16,64}$")
    abstraction_level: AbstractionLevel = AbstractionLevel.L1
    inherits: list[ActionPackId] = Field(default_factory=list, max_length=8)
    state: LifecycleState = LifecycleState.DRAFT
    owner: str
    created: datetime
    retire_after: datetime | None = None


class ToolSpec(BaseModel):
    """One action a pack grants. ``impl`` resolves to a registered tool handler."""

    model_config = ConfigDict(strict=True, extra="allow")

    name: str                       # e.g. process_media / discover_sources / escalate / create_incident
    impl: str | None = None         # module:callable; None → built-in resolved by name
    config: dict[str, Any] = Field(default_factory=dict)
    async_job: bool = False         # true → enqueues onto the NATS job plane


class Channel(BaseModel):
    """An output/escalation binding the pack's actions may emit to.

    Reuses the existing output-kind surface — ``kind`` is an output kind, ``config``
    its binding (sink address, severity threshold, etc.).
    """

    model_config = ConfigDict(strict=True, extra="allow")

    name: str
    kind: Literal[
        "alert", "webhook", "a2a_skill", "nats_stream", "mcp_tool", "stix_bundle",
    ]
    config: dict[str, Any] = Field(default_factory=dict)


class PackGovernor(BaseModel):
    """Per-pack budget + rate caps. Enforced in P-11; extends budget_ledger."""

    model_config = ConfigDict(strict=True, extra="forbid")

    budget_account: str | None = None
    max_invocations_per_hour: int | None = Field(default=None, ge=0)
    max_cost_usd_per_day: float | None = Field(default=None, ge=0)
    # discovery/crawl-specific caps (used by the discovery pack)
    max_sources_per_window: int | None = Field(default=None, ge=0)
    crawl_max_depth: int | None = Field(default=None, ge=0)
    crawl_max_pages: int | None = Field(default=None, ge=0)
    api_rate_per_minute: int | None = Field(default=None, ge=0)


class ActionPack(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    identity: ActionPackIdentity
    tools: list[ToolSpec] = Field(default_factory=list)
    # prompt fragments / behavioral rules injected when the pack is active
    prompt_fragments: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    channels: list[Channel] = Field(default_factory=list)
    governor: PackGovernor = Field(default_factory=PackGovernor)
    # applicability: a domain/context predicate for when the pack is relevant.
    # Tags it applies to + an optional Starlark predicate over target context.
    applies_to_tags: list[Tag] = Field(default_factory=list)
    applicability_predicate: str | None = None

    @field_validator("applicability_predicate", mode="after")
    @classmethod
    def _compile_predicate(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from ..predicates import (
            TARGET_SCOPE_APPLICABILITY_CTX,
            PredicateCompilationError,
            PredicateSurface,
            compile_predicate,
        )
        try:
            # G4: applicability predicates are evaluated ONLY against the
            # target-side ctx (agency.resolution._is_applicable) — narrow the
            # contract so signal-helper predicates are refused at
            # registration instead of silently never gating the pack open.
            compile_predicate(
                v,
                PredicateSurface.TARGET_SCOPE,
                ctx_contract=TARGET_SCOPE_APPLICABILITY_CTX,
            )
        except PredicateCompilationError as exc:
            raise ValueError(
                f"action_pack.applicability_predicate failed to compile: {exc}"
            ) from exc
        return v

    @model_validator(mode="after")
    def _constraints(self) -> "ActionPack":
        if not self.tools and not self.channels:
            raise ValueError("an action pack must grant at least one tool or channel")
        return self


class ActionPackRef(BaseModel):
    """A grant/allow reference to an action pack (on analysts / targets)."""

    model_config = ConfigDict(strict=True, extra="forbid")

    pack_id: ActionPackId
    # optional per-binding governor override (tightening only, enforced in P-11)
    governor_override: PackGovernor | None = None


__all__ = [
    "ActionPackIdentity",
    "ToolSpec",
    "Channel",
    "PackGovernor",
    "ActionPack",
    "ActionPackRef",
]
