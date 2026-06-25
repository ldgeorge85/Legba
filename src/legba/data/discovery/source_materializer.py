# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-discovery materialiser (P-13) — CandidateSource → source_descriptors.

The source-side twin of
:func:`legba.data.registry.discovered_materializer.reconcile_discovered_targets`.
Where that loop materialises target instances, this one materialises *source
instances*, with one extra gate the target side doesn't need:
**validate-before-register**.

Per-candidate flow
------------------

```
  CandidateSource
      │
      ├─ evaluate_relabel_chain(candidate, rules)      relabel.py (shared)
      │     └─ dropped?  → MaterializeSourceOutcome(dropped=True)
      │
      ├─ validate_candidate_source(candidate)          source_validate.py
      │     └─ invalid?  → MaterializeSourceOutcome(rejected=True) + DLQ
      │
      ├─ merge template body + relabel writes
      ├─ SourceDescriptor.model_validate(...)
      │     └─ invalid?  → MaterializeSourceOutcome(dlq=True) + DLQ
      │
      ├─ UPSERT source_descriptors (content-hash version, is_head)
      │
      └─ auto_wire_discovered_source(...)               autowire.py
            └─ targets whose source_selector matches + policy=open
               get the new source bound (via the W2 SourceRef engine).
```

The relabel chain + parent-body merge are reused from the target side
(:mod:`legba.data.discovery.relabel` + the merge contract here mirrors
:func:`legba.data.registry.discovered_materializer.merge_descriptor_bodies`) so
the two flavors share one rewrite engine.

``validate_before_register`` (the ``SourceDiscoveryBlock`` default) gates the
write: a candidate that fails liveness / trial-pull is *rejected* — never
written to ``source_descriptors`` — and routed to the DLQ. This is the gate
that keeps the source pool clean so selector auto-wire only attracts working
sources.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Sequence

import asyncpg
from pydantic import ValidationError

from ..provenance import canonical_json
from ..schemas import content_hash
from ..schemas.source import SourceDescriptor
from ._contract import RelabelRule
from .relabel import RelabelResult, evaluate_relabel_chain
from .source_contract import CandidateSource, SourceCandidateValidation
from .source_validate import validate_candidate_source

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterializeSourceOutcome:
    """Per-candidate outcome of :func:`materialize_discovered_source`."""

    natural_key: str
    source_id: str | None
    version: str | None
    dropped: bool = False
    dropped_reason: str = ""
    rejected: bool = False
    """True iff validate-before-register failed (liveness / trial-pull)."""
    rejected_reason: str = ""
    dlq: bool = False
    dlq_reason: str = ""
    auto_wired_targets: list[str] = field(default_factory=list)
    """Target ids whose source_selector matched + auto-wired the new source."""
    validation: SourceCandidateValidation | None = None
    materialized_body: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ReconcileSourceResult:
    """Per-cycle outcome of :func:`reconcile_discovered_sources`."""

    discovery_id: str
    cycle_started_at: datetime
    cycle_ended_at: datetime
    candidates_in: int
    materialized: list[MaterializeSourceOutcome] = field(default_factory=list)

    @property
    def registered_count(self) -> int:
        return sum(
            1
            for m in self.materialized
            if m.source_id is not None
            and not m.dropped
            and not m.rejected
            and not m.dlq
        )

    @property
    def rejected_count(self) -> int:
        return sum(1 for m in self.materialized if m.rejected)

    @property
    def dropped_count(self) -> int:
        return sum(1 for m in self.materialized if m.dropped)

    @property
    def dlq_count(self) -> int:
        return sum(1 for m in self.materialized if m.dlq)


# ---------------------------------------------------------------------------
# Body merge (shared shape with the target materialiser)
# ---------------------------------------------------------------------------


def _deep_merge_into(dst: MutableMapping[str, Any], src: Mapping[str, Any]) -> None:
    for key, sval in src.items():
        if sval is None:
            continue
        if key not in dst:
            dst[key] = copy.deepcopy(sval)
            continue
        dval = dst[key]
        if isinstance(dval, MutableMapping) and isinstance(sval, Mapping):
            _deep_merge_into(dval, sval)
        else:
            dst[key] = copy.deepcopy(sval)


def _extract_chain_writes(
    rules: Sequence[RelabelRule], chain_labels: Mapping[str, Any]
) -> dict[str, Any]:
    """Project the chain's target_label writes into a tree-shaped dict.

    Same approach as the target materialiser — only rule-written paths land in
    the merged body, so the candidate's raw labels (``url`` / ``host`` / ...)
    don't trip ``extra=forbid`` on SourceDescriptor.
    """
    written: set[str] = {r.target_label for r in rules if r.target_label}

    def _project(path: str, src: Mapping[str, Any]) -> Any:
        cur: Any = src
        for seg in path.split("."):
            if not isinstance(cur, Mapping) or seg not in cur:
                return None
            cur = cur[seg]
        return cur

    def _write(dst: MutableMapping[str, Any], path: str, value: Any) -> None:
        parts = path.split(".")
        cur: MutableMapping[str, Any] = dst
        for seg in parts[:-1]:
            if seg not in cur or not isinstance(cur[seg], MutableMapping):
                cur[seg] = {}
            cur = cur[seg]
        cur[parts[-1]] = value

    out: dict[str, Any] = {}
    for path in written:
        val = _project(path, chain_labels)
        if val is None:
            continue
        _write(out, path, copy.deepcopy(val))
    return out


def _assemble_source_body(
    *,
    template_body: Mapping[str, Any],
    relabeled_labels: Mapping[str, Any],
    discovery_descriptor: SourceDescriptor,
    rules: Sequence[RelabelRule],
) -> dict[str, Any]:
    """Combine template body + relabel writes + provenance for a source instance.

    Forces ``identity.abstraction_level=L1`` + ``identity.state=draft`` (the
    runtime promotes a source instance to active once it pulls cleanly), and
    appends the discovery descriptor's id to ``inherits`` for the prior-keys
    reverse lookup.
    """
    base = copy.deepcopy(dict(template_body))
    identity = base.setdefault("identity", {})
    template_id = identity.get("id")
    identity.pop("id", None)
    identity.pop("version", None)
    base.pop("discovery", None)

    write_set = _extract_chain_writes(rules, relabeled_labels)

    merged = copy.deepcopy(base)
    _deep_merge_into(merged, write_set)

    merged_identity = merged.setdefault("identity", {})
    merged_identity["abstraction_level"] = "L1"
    merged_identity["state"] = "draft"
    inherits = list(merged_identity.get("inherits") or [])
    if template_id and template_id not in inherits:
        inherits.append(template_id)
    if discovery_descriptor.identity.id not in inherits:
        inherits.append(discovery_descriptor.identity.id)
    merged_identity["inherits"] = inherits

    if "schema_uri" not in merged_identity:
        merged_identity["schema_uri"] = discovery_descriptor.identity.schema_uri
    if "owner" not in merged_identity:
        merged_identity["owner"] = discovery_descriptor.identity.owner
    if "created" not in merged_identity:
        merged_identity["created"] = datetime.now(tz=timezone.utc).isoformat()
    merged_identity.setdefault("version", "0" * 16)
    # A source instance must declare its handler kind (the candidate carries it
    # in source_kind; the relabel chain may also set identity.kind). Ensure it
    # is present.
    if "kind" not in merged_identity:
        merged_identity["kind"] = template_body.get("identity", {}).get("kind", "")

    return merged


# ---------------------------------------------------------------------------
# materialize_discovered_source — single candidate
# ---------------------------------------------------------------------------


async def materialize_discovered_source(
    conn: asyncpg.Connection,
    candidate: CandidateSource,
    discovery_descriptor: SourceDescriptor,
    rules: Sequence[RelabelRule] | None = None,
    *,
    template_body: Mapping[str, Any] | None = None,
    lookup_tables: Mapping[str, Mapping[str, Any]] | None = None,
    validate_before_register: bool = True,
    secrets_resolve: Any = None,
    source_registry: Any = None,
    probe_handler: Any = None,
    dlq: Any = None,
    auto_wire: bool = True,
    actor: str = "source_discovery_materializer",
) -> MaterializeSourceOutcome:
    """Materialise one :class:`CandidateSource` into a ``source_descriptors`` row.

    Steps: relabel chain → (optional) validate-before-register → merge with the
    template body → validate against :class:`SourceDescriptor` → UPSERT →
    (optional) selector auto-wire.

    ``validate_before_register`` mirrors the ``SourceDiscoveryBlock`` default:
    when True, a candidate failing liveness/trial-pull is *rejected* (never
    written) and routed to the DLQ.
    """
    rules = list(
        rules
        if rules is not None
        else (
            _coerce_rules(discovery_descriptor.discovery.relabel)
            if discovery_descriptor.discovery
            else []
        )
    )

    # 1. Relabel chain (over the candidate's label_set).
    from ._contract import CandidateTarget

    proxy = CandidateTarget(
        natural_key=candidate.natural_key,
        label_set=dict(candidate.label_set),
        source_metadata=dict(candidate.source_metadata),
    )
    relabel_result: RelabelResult = evaluate_relabel_chain(
        proxy, rules, lookup_tables=lookup_tables
    )
    if relabel_result.dropped:
        return MaterializeSourceOutcome(
            natural_key=candidate.natural_key,
            source_id=None,
            version=None,
            dropped=True,
            dropped_reason=relabel_result.dropped_reason,
        )

    # 2. validate-before-register (liveness + trial pull/parse).
    validation: SourceCandidateValidation | None = None
    if validate_before_register:
        validation = await validate_candidate_source(
            candidate,
            secrets_resolve=secrets_resolve,
            source_registry=source_registry,
            handler=probe_handler,
        )
        if not validation.valid:
            logger.info(
                "source_discovery.rejected discovery=%s natural_key=%s reason=%s",
                discovery_descriptor.identity.id,
                candidate.natural_key,
                validation.reason,
            )
            if dlq is not None:
                await _dlq_record(
                    dlq,
                    actor=actor,
                    reason="source_validate_before_register_failed",
                    payload={
                        "discovery_id": discovery_descriptor.identity.id,
                        "natural_key": candidate.natural_key,
                        "validation": validation.model_dump(mode="json"),
                    },
                )
            return MaterializeSourceOutcome(
                natural_key=candidate.natural_key,
                source_id=None,
                version=None,
                rejected=True,
                rejected_reason=validation.reason,
                validation=validation,
            )

    # 3. Resolve template body.
    resolved_template = template_body
    if resolved_template is None:
        parent_id = (
            discovery_descriptor.identity.inherits[0]
            if discovery_descriptor.identity.inherits
            else None
        )
        if parent_id is None:
            raise ValueError(
                f"source-discovery descriptor "
                f"{discovery_descriptor.identity.id!r} has empty "
                f"identity.inherits — cannot resolve template body"
            )
        row = await conn.fetchrow(
            "SELECT body FROM source_descriptors "
            "WHERE descriptor_id = $1 AND is_head LIMIT 1",
            parent_id,
        )
        if row is None:
            raise ValueError(
                f"source-discovery descriptor "
                f"{discovery_descriptor.identity.id!r} references template "
                f"{parent_id!r} which is not registered"
            )
        body = row["body"]
        resolved_template = json.loads(body) if isinstance(body, str) else body

    # 4. Assemble + validate.
    assembled = _assemble_source_body(
        template_body=resolved_template,
        relabeled_labels=relabel_result.labels,
        discovery_descriptor=discovery_descriptor,
        rules=rules,
    )
    try:
        descriptor = SourceDescriptor.model_validate_json(
            json.dumps(assembled, default=str)
        )
    except ValidationError as exc:
        logger.warning(
            "source_discovery.invalid discovery=%s natural_key=%s errors=%s",
            discovery_descriptor.identity.id,
            candidate.natural_key,
            exc.errors(),
        )
        if dlq is not None:
            await _dlq_record(
                dlq,
                actor=actor,
                reason="source_discovery_materialization_invalid",
                payload={
                    "discovery_id": discovery_descriptor.identity.id,
                    "natural_key": candidate.natural_key,
                    "errors": exc.errors(),
                },
                declared_schema_uri=assembled.get("identity", {}).get("schema_uri"),
                attempted_payload=assembled,
            )
        return MaterializeSourceOutcome(
            natural_key=candidate.natural_key,
            source_id=assembled.get("identity", {}).get("id"),
            version=None,
            dlq=True,
            dlq_reason=str(exc.errors()[0] if exc.errors() else "validation_error"),
            validation=validation,
            materialized_body=assembled,
        )

    # 5. UPSERT.
    hash_hex = content_hash(descriptor)
    body_with_version = descriptor.model_dump(mode="json", by_alias=True)
    body_with_version["identity"]["version"] = hash_hex

    existing = await conn.fetchrow(
        "SELECT version FROM source_descriptors "
        "WHERE descriptor_id = $1 AND version = $2 LIMIT 1",
        descriptor.identity.id,
        hash_hex,
    )
    if existing is None:
        await conn.execute(
            "UPDATE source_descriptors SET is_head = FALSE "
            "WHERE descriptor_id = $1 AND is_head",
            descriptor.identity.id,
        )
        await conn.execute(
            """
            INSERT INTO source_descriptors
              (descriptor_id, version, schema_uri, is_head, abstraction_level,
               kind, state, owner, name, body, inherits, created_at)
            VALUES ($1, $2, $3, TRUE, $4, $5, $6, $7, $8, $9::jsonb, $10, NOW())
            """,
            descriptor.identity.id,
            hash_hex,
            descriptor.identity.schema_uri,
            descriptor.identity.abstraction_level.value,
            descriptor.identity.kind,
            descriptor.identity.state.value,
            descriptor.identity.owner,
            descriptor.identity.name,
            canonical_json(body_with_version).decode("utf-8"),
            list(descriptor.identity.inherits),
        )
        logger.info(
            "source_discovery.registered discovery=%s source_id=%s version=%s",
            discovery_descriptor.identity.id,
            descriptor.identity.id,
            hash_hex,
        )

    # 5b. Record the (discovery_id, natural_key) -> source_id mapping in
    # discovery_state (migration 0026) so subsequent cycles can classify
    # retained/new/disappeared without re-deriving from the body (the source
    # natural_key is a feed URL, not recoverable from the slugged source id).
    await _record_discovery_state(
        conn,
        discovery_id=discovery_descriptor.identity.id,
        natural_key=candidate.natural_key,
        descriptor_id=descriptor.identity.id,
        descriptor_version=hash_hex,
        evidence=candidate.evidence.model_dump(mode="json"),
    )

    # 6. Selector auto-wire (PIVOT §4.4) — gated by subscription_policy.
    auto_wired: list[str] = []
    if auto_wire:
        try:
            from .autowire import auto_wire_discovered_source

            auto_wired = await auto_wire_discovered_source(
                conn, source_id=descriptor.identity.id
            )
        except Exception as exc:  # auto-wire failure must not lose the write
            logger.warning(
                "source_discovery.autowire_failed source_id=%s err=%s",
                descriptor.identity.id, exc,
            )

    return MaterializeSourceOutcome(
        natural_key=candidate.natural_key,
        source_id=descriptor.identity.id,
        version=hash_hex,
        auto_wired_targets=auto_wired,
        validation=validation,
        materialized_body=body_with_version,
    )


def _coerce_rules(raw: Any) -> list[RelabelRule]:
    """Coerce a SourceDiscoveryBlock.relabel (list[dict]) into RelabelRule."""
    out: list[RelabelRule] = []
    for r in raw or []:
        if isinstance(r, RelabelRule):
            out.append(r)
        else:
            out.append(RelabelRule.model_validate(r))
    return out


async def _record_discovery_state(
    conn: asyncpg.Connection,
    *,
    discovery_id: str,
    natural_key: str,
    descriptor_id: str,
    descriptor_version: str,
    evidence: Mapping[str, Any],
) -> None:
    """Upsert the (discovery_id, natural_key) -> descriptor_id mapping.

    Idempotent: a re-seen candidate bumps ``last_seen_at`` + ``cycle_count``
    and re-asserts ``state='active'`` (un-retires a returned candidate).
    """
    await conn.execute(
        """
        INSERT INTO discovery_state
          (discovery_id, natural_key, family, descriptor_id,
           descriptor_version, state, evidence)
        VALUES ($1, $2, 'source', $3, $4, 'active', $5::jsonb)
        ON CONFLICT (discovery_id, natural_key) DO UPDATE SET
          descriptor_id = EXCLUDED.descriptor_id,
          descriptor_version = EXCLUDED.descriptor_version,
          state = 'active',
          last_seen_at = NOW(),
          cycle_count = discovery_state.cycle_count + 1,
          evidence = EXCLUDED.evidence
        """,
        discovery_id,
        natural_key,
        descriptor_id,
        descriptor_version,
        json.dumps(dict(evidence), default=str),
    )


async def _dlq_record(
    dlq: Any,
    *,
    actor: str,
    reason: str,
    payload: Mapping[str, Any],
    declared_schema_uri: str | None = None,
    attempted_payload: Mapping[str, Any] | None = None,
) -> None:
    try:
        await dlq.record(
            actor=actor,
            namespace="source",
            attempted_payload=dict(attempted_payload or payload),
            declared_schema_uri=declared_schema_uri,
            validation_error={"reason": reason, **dict(payload)},
        )
    except Exception as exc:  # pragma: no cover
        logger.exception("source_discovery.dlq_write_failed err=%s", exc)


# ---------------------------------------------------------------------------
# reconcile_discovered_sources — full per-cycle loop
# ---------------------------------------------------------------------------


async def reconcile_discovered_sources(
    conn: asyncpg.Connection,
    discovery_descriptor: SourceDescriptor,
    candidates: Sequence[CandidateSource],
    *,
    template_body: Mapping[str, Any] | None = None,
    lookup_tables: Mapping[str, Mapping[str, Any]] | None = None,
    secrets_resolve: Any = None,
    source_registry: Any = None,
    probe_handler: Any = None,
    dlq: Any = None,
    auto_wire: bool = True,
    nats_publish: Any = None,
    actor: str = "source_discovery_materializer",
) -> ReconcileSourceResult:
    """Run one source-discovery cycle's materialisation loop.

    Each candidate goes through :func:`materialize_discovered_source`. The
    ``validate_before_register`` flag is read from the discovery descriptor's
    :class:`~legba.data.schemas.source.SourceDiscoveryBlock`.
    """
    cycle_started_at = datetime.now(tz=timezone.utc)
    discovery_id = discovery_descriptor.identity.id
    block = discovery_descriptor.discovery
    if block is None:
        raise ValueError(
            f"reconcile_discovered_sources called on descriptor {discovery_id!r} "
            f"with no discovery block"
        )
    rules = _coerce_rules(block.relabel)
    validate = bool(getattr(block, "validate_before_register", True))

    outcomes: list[MaterializeSourceOutcome] = []
    for cand in candidates:
        try:
            outcome = await materialize_discovered_source(
                conn,
                cand,
                discovery_descriptor,
                rules,
                template_body=template_body,
                lookup_tables=lookup_tables,
                validate_before_register=validate,
                secrets_resolve=secrets_resolve,
                source_registry=source_registry,
                probe_handler=probe_handler,
                dlq=dlq,
                auto_wire=auto_wire,
                actor=actor,
            )
        except Exception as exc:
            logger.exception(
                "source_discovery.error discovery=%s natural_key=%s err=%s",
                discovery_id, cand.natural_key, exc,
            )
            outcome = MaterializeSourceOutcome(
                natural_key=cand.natural_key,
                source_id=None,
                version=None,
                dlq=True,
                dlq_reason=f"materialize_exception:{type(exc).__name__}:{exc}",
            )
            if dlq is not None:
                await _dlq_record(
                    dlq,
                    actor=actor,
                    reason="source_materialize_exception",
                    payload={
                        "discovery_id": discovery_id,
                        "natural_key": cand.natural_key,
                        "error": str(exc),
                    },
                )
        outcomes.append(outcome)

    cycle_ended_at = datetime.now(tz=timezone.utc)
    result = ReconcileSourceResult(
        discovery_id=discovery_id,
        cycle_started_at=cycle_started_at,
        cycle_ended_at=cycle_ended_at,
        candidates_in=len(candidates),
        materialized=outcomes,
    )

    if nats_publish is not None:
        try:
            await nats_publish(
                f"legba.source_discovery.cycle.{discovery_id}",
                json.dumps(
                    {
                        "discovery_id": discovery_id,
                        "candidates_in": result.candidates_in,
                        "registered_count": result.registered_count,
                        "rejected_count": result.rejected_count,
                        "dropped_count": result.dropped_count,
                        "dlq_count": result.dlq_count,
                        "cycle_started_at": cycle_started_at.isoformat(),
                    }
                ).encode("utf-8"),
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("source_discovery.nats_cycle_failed err=%s", exc)

    return result


__all__ = [
    "MaterializeSourceOutcome",
    "ReconcileSourceResult",
    "materialize_discovered_source",
    "reconcile_discovered_sources",
]
