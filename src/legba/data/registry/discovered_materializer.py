# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registry-side discovery materialization loop (L-181 / L-182 consumer).

Per L-106 §2-§5 + L-180 contract + L-200 / Wave D integration brief: the
discovery handler emits :class:`CandidateTarget` instances; the *registry*
runs each candidate through the discovery descriptor's relabel chain,
merges the result with the inherited template body, and upserts a
materialized L1 :class:`TargetDescriptor` row into ``target_descriptors``.

This module is the consumer the L-180 contract was missing — the
``materialize_discovered(...)`` callsite paired with the static-target
shortcut :func:`legba.data.discovery.static.materialize_static` so the
runtime's actor host can dispatch uniformly between the two paths.

Architecture
------------

```
                ┌──────────────────────────────────┐
                │ DiscoveryKind.discover(ctx)      │  L-181 / L-182
                │   ⇒ async iter[CandidateTarget]  │
                └─────────────────┬────────────────┘
                                  │
                                  v
              ┌────────────────────────────────────────┐
              │ reconcile_discovered_targets()         │  this module
              │  ├── evaluate_relabel_chain(candidate) │  L-180 / relabel.py
              │  ├── merge_descriptor_bodies(template, │  this module
              │  │                            relabeled)│
              │  ├── TargetDescriptor.model_validate() │  schemas/target.py
              │  ├── INSERT/UPDATE target_descriptors  │  this module
              │  └── evaluate_disappearance()          │  L-180 /
              │      ├── proceed → retire missing keys │   disappearance.py
              │      └── anomaly → pause + DLQ + alert │
              └────────────────────────────────────────┘
                                  │
                                  v
              ┌────────────────────────────────────────┐
              │ ReconcileResult — per-cycle outcome    │
              │  materialized / retained / retired /   │
              │  dropped (relabel) / dlq (validation)  │
              │  + paused flag if anomaly fired        │
              └────────────────────────────────────────┘
```

Parent-body merge contract
--------------------------

Per Wave D integration brief §5 ("lean" option): the materialized L1
instance inherits its sources / pipeline / analyst / outputs from the L2
template referenced by ``identity.inherits[0]``. The relabel chain
produces a (possibly partial) body that overrides template fields:

  * **scalars**: relabeled value overrides template (last-write-wins).
  * **dicts**: deep-merge — keys present in both branches recurse;
    missing keys come from whichever side has them.
  * **lists**: replaced by the relabeled list when the relabeled side
    specifies the key, otherwise inherited from the template unchanged.
    A future ``merge: replace | extend`` per-list directive can flip this
    to extension semantics — not in Wave D scope. The choice biases
    toward *predictable substitution*: an operator writing
    ``scope.geo = [{{ country_iso2 }}]`` expects exactly that list, not
    the template's placeholder plus the new entry.

The merge is implemented by :func:`merge_descriptor_bodies` below. It is
deterministic — same template + same relabeled labels = same merged body
on every cycle. The materialization loop relies on this to compute the
content hash and skip the substrate write when the body hasn't changed
since the last cycle.

Public surface
--------------

  * :func:`materialize_discovered` — single-candidate materialization.
    Runs the relabel chain → merges with the template body → validates
    against :class:`TargetDescriptor` → upserts the row. Returns the new
    row's UUID (or ``None`` if the relabel chain dropped the candidate
    or the merged body failed pydantic validation, in which case the DLQ
    receives the structured payload).

  * :func:`reconcile_discovered_targets` — orchestrates per-candidate
    materialization for one discovery cycle. Applies disappearance-ratio
    enforcement per L-106 §5 (:func:`evaluate_disappearance`), retires
    candidates whose ``natural_key`` is absent this cycle, and routes
    excess-disappearance candidates to the DLQ + pauses the discovery
    descriptor per the policy.

  * :func:`merge_descriptor_bodies` — pure helper that implements the
    parent-body merge contract above.

  * :class:`ReconcileResult` — structured per-cycle outcome.

Wave D / L-200 integration notes
--------------------------------

The L-200 ``discovery_geopolitical_countries`` descriptor + the
``template_country`` L2 template materialise N per-country L1 instances
through this loop. The relabel chain in
``descriptors/discovery_geopolitical_countries.yaml`` writes
``scope.geo``, ``scope.languages``, ``scope.tags``, ``identity.id``, and
``identity.inherits``; the template provides ``sources``, ``pipeline``,
``analyst``, ``outputs``. The Wave D vocabulary migration (0020) seeds
the entity_classes + relationship_types the template uses so the merged
body passes :class:`DescriptorRegistry`'s vocabulary check on insert.

For now the L2 template body is read from the descriptor registry by
``identity.id`` — the discovery descriptor's ``identity.inherits[0]``
points at the template. Future iterations may allow multiple template
inherits with explicit merge ordering; Wave D scope is single-parent.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Sequence
from uuid import UUID, uuid4

import asyncpg
from pydantic import ValidationError

from ..discovery._contract import CandidateTarget, RelabelRule, ResyncPolicy
from ..discovery.disappearance import DisappearanceDecision, evaluate_disappearance
from ..discovery.relabel import RelabelResult, evaluate_relabel_chain
from ..provenance import canonical_json
from ..schemas import (
    LifecycleState,
    TargetDescriptor,
    content_hash,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterializeOutcome:
    """Per-candidate outcome of :func:`materialize_discovered`."""

    natural_key: str
    """The candidate's stable id (echoed for convenience)."""

    descriptor_id: str | None
    """The materialized L1 target descriptor id, or None if the chain
    dropped the candidate or validation failed."""

    row_uuid: UUID | None
    """UUID of the inserted ``target_descriptors`` row, or None on drop /
    DLQ. Note: ``target_descriptors`` is keyed by ``(descriptor_id,
    version)`` not by an arbitrary UUID — we mint and return a per-row
    UUID for log correlation; the substrate primary key is the
    composite."""

    version: str | None
    """Content-hash version of the materialized body, or None on drop / DLQ."""

    dropped: bool = False
    """True iff the relabel chain short-circuited via keep/drop/hash_mod."""

    dropped_reason: str = ""
    """Human-readable reason when ``dropped`` — copied from
    :class:`RelabelResult.dropped_reason`."""

    dlq: bool = False
    """True iff the merged body failed pydantic validation and was
    routed to the descriptor DLQ instead of being inserted."""

    dlq_reason: str = ""
    """Human-readable reason when ``dlq`` — the pydantic error summary."""

    materialized_body: Mapping[str, Any] | None = None
    """The merged + validated body that was written, when the row landed."""


@dataclass(frozen=True)
class ReconcileResult:
    """Per-cycle outcome of :func:`reconcile_discovered_targets`."""

    discovery_id: str
    """Echoed for log correlation."""

    cycle_started_at: datetime
    cycle_ended_at: datetime

    candidates_in: int
    """Number of CandidateTargets handed in this cycle."""

    materialized: list[MaterializeOutcome] = field(default_factory=list)
    """Per-candidate outcomes (success + drop + DLQ + retained)."""

    retired: list[str] = field(default_factory=list)
    """descriptor_ids retired this cycle (their natural_key disappeared
    and policy allowed retirement)."""

    routed_to_dlq: list[str] = field(default_factory=list)
    """Natural_keys whose disappearance breached the ratio threshold and
    are held in ``discovery_resync_dlq`` instead of being retired."""

    disappearance: DisappearanceDecision | None = None
    """Echoed from :func:`evaluate_disappearance` — the full structured
    decision so callers can read prior_count / ratio / threshold."""

    paused: bool = False
    """True iff the disappearance anomaly fired with the
    ``alert_and_pause`` policy and this loop transitioned the discovery
    descriptor's lifecycle to PAUSED."""

    # Convenience counters.

    @property
    def inserted_count(self) -> int:
        return sum(
            1
            for m in self.materialized
            if m.row_uuid is not None and not m.dropped and not m.dlq
        )

    @property
    def dropped_count(self) -> int:
        return sum(1 for m in self.materialized if m.dropped)

    @property
    def dlq_count(self) -> int:
        return sum(1 for m in self.materialized if m.dlq)


# ---------------------------------------------------------------------------
# Parent-body merge contract — Wave D §5 lean
# ---------------------------------------------------------------------------


def merge_descriptor_bodies(
    template: Mapping[str, Any],
    relabeled: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge a template body with a relabel-chain output per Wave D §5.

    Rules:

      * **scalars** — relabeled value overrides template (last-write-wins).
      * **dicts** — deep-merge: keys in both recurse; otherwise the side
        that has the key wins.
      * **lists** — relabeled list *replaces* the template's list when
        the relabeled side specifies the key. When the relabeled side
        omits the key, the template's list is inherited as-is.
      * **None on relabeled side** — treated as "no override" (the
        relabel chain didn't write this key), so the template wins.

    The output is a fresh dict — the inputs are not mutated.

    Examples
    --------

    A template like::

        {
          "identity": {"id": "template_country", "owner": "geo"},
          "sources": [{"id": "rss", "kind": "rss"}],
          "scope": {"geo": ["XX"], "tags": []},
        }

    merged with a relabel output of::

        {
          "identity": {"id": "country_news_br"},
          "scope": {"geo": ["BR"], "tags": ["news", "geopolitical"]},
        }

    yields::

        {
          "identity": {"id": "country_news_br", "owner": "geo"},
          "sources": [{"id": "rss", "kind": "rss"}],
          "scope": {"geo": ["BR"], "tags": ["news", "geopolitical"]},
        }

    Note that ``scope.tags`` came from the relabeled side (replaces the
    template's empty list) and ``sources`` came from the template (the
    relabeled side didn't touch it).
    """
    if not isinstance(template, Mapping):
        raise TypeError(
            f"merge_descriptor_bodies: template must be a Mapping, got "
            f"{type(template).__name__}"
        )
    if not isinstance(relabeled, Mapping):
        raise TypeError(
            f"merge_descriptor_bodies: relabeled must be a Mapping, got "
            f"{type(relabeled).__name__}"
        )

    out: dict[str, Any] = copy.deepcopy(dict(template))
    _deep_merge_into(out, relabeled)
    return out


def _deep_merge_into(
    dst: MutableMapping[str, Any],
    src: Mapping[str, Any],
) -> None:
    """In-place deep-merge of ``src`` into ``dst`` per Wave D §5 rules."""
    for key, sval in src.items():
        if sval is None:
            # Relabel chain explicitly set None — treat as "no override"
            # rather than "wipe the template's value". The relabel chain
            # never writes None deliberately (the actions either write a
            # value or short-circuit on drop); a None here means the key
            # was carried through metadata without a definitive value.
            continue
        if key not in dst:
            dst[key] = copy.deepcopy(sval)
            continue
        dval = dst[key]
        if isinstance(dval, MutableMapping) and isinstance(sval, Mapping):
            _deep_merge_into(dval, sval)
        else:
            # Scalars + lists: replace.
            dst[key] = copy.deepcopy(sval)


# ---------------------------------------------------------------------------
# Semantic fingerprint — idempotency guard (Fix 2)
# ---------------------------------------------------------------------------


# The semantic fields that define a materialised target's *behaviour*. Two
# bodies that agree on all of these produce identical ingest + analysis;
# differences in `identity` (notably `identity.inherits`, which carries a
# varying parent-version pointer), `created`, or `version` are pure
# provenance churn and MUST NOT mint a new content-hash version.
_SEMANTIC_FIELDS: tuple[str, ...] = (
    "scope",
    "sources",
    "analyst",
    "pipeline",
    "outputs",
)


def _semantic_fingerprint(body: Mapping[str, Any]) -> str:
    """Return a sha256 over the *behavioural* fields of a descriptor body.

    Hashes only :data:`_SEMANTIC_FIELDS` (``scope`` / ``sources`` /
    ``analyst`` / ``pipeline`` / ``outputs``) — the fields that define
    what a materialised target ingests + how it is analysed. Provenance-only
    fields (``identity`` and in particular ``identity.inherits``'s
    parent-version pointer, plus ``created`` / ``version``) are excluded,
    so a cycle that changed only the inherited parent pointer fingerprints
    identically to the prior cycle.

    Missing keys are tolerated (``body.get(k)`` → ``None``) so partially
    shaped bodies still fingerprint deterministically. The hash is taken
    over :func:`canonical_json` of the projected sub-body, which sorts keys
    and normalises separators, so equal semantic content always yields an
    equal digest regardless of input key order.
    """
    projected = {k: body.get(k) for k in _SEMANTIC_FIELDS}
    return hashlib.sha256(canonical_json(projected)).hexdigest()


# ---------------------------------------------------------------------------
# Body assembly helpers
# ---------------------------------------------------------------------------


def _extract_chain_writes(
    relabel_rules: Sequence[RelabelRule],
    chain_output_labels: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the relabel-chain's *write set* — the keys the rules
    explicitly targeted via ``target_label``.

    :func:`evaluate_relabel_chain` returns the full working label_set,
    which carries the candidate's raw labels alongside the rule writes.
    Those raw labels (``country_iso2``, ``country_region``, ...) don't
    correspond to schema-defined keys, so we must filter to only the
    keys that were actually written by a rule.

    A rule's ``target_label`` may be a dotted path (``scope.geo``); we
    project the chain output into the nested dict shape so the merge
    contract receives a tree-shaped relabel payload.
    """
    written_paths: set[str] = set()
    for rule in relabel_rules:
        if rule.target_label:
            written_paths.add(rule.target_label)

    def _project(path: str, src: Mapping[str, Any]) -> Any:
        parts = path.split(".")
        cur: Any = src
        for seg in parts:
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
    for path in written_paths:
        val = _project(path, chain_output_labels)
        if val is None:
            continue
        _write(out, path, copy.deepcopy(val))
    return out


def _assemble_materialized_body(
    *,
    template_body: Mapping[str, Any],
    relabeled_labels: Mapping[str, Any],
    discovery_descriptor: TargetDescriptor,
    relabel_rules: Sequence[RelabelRule] | None = None,
) -> dict[str, Any]:
    """Combine template + relabel-chain writes + discovery-provenance.

    Only the *target_label* writes from the relabel chain are projected
    into the merged body (see :func:`_extract_chain_writes`) — the raw
    candidate labels and intermediate working-set entries are NOT folded
    into the body. This avoids ``extra=forbid`` violations on the
    extra-rich candidate label sets emitted by handlers like
    ``country_list_discovery`` (which carries ``country_iso2`` /
    ``country_region`` / etc. as candidate metadata).

    The merged body is forced to ``identity.abstraction_level=L1`` +
    ``identity.state=draft`` (instances always materialise as L1
    drafts; the runtime promotes them on first successful run).
    """
    # Start from a deep copy of the template body so we don't mutate the
    # template-row we read from substrate.
    base = copy.deepcopy(dict(template_body))

    # Strip template-only fields that don't belong on the materialised
    # instance.
    identity = base.setdefault("identity", {})
    template_id = identity.get("id")
    identity.pop("id", None)
    identity.pop("version", None)
    base.pop("discovery", None)

    # Project the relabel chain's writes into a tree-shape and merge.
    if relabel_rules is None:
        write_set = dict(relabeled_labels)
    else:
        write_set = _extract_chain_writes(relabel_rules, relabeled_labels)

    # Top-level `tags` written by the chain promote into `scope.tags`
    # (Wave D L-200 surfaced that `tags` is logically a scope-level
    # concept; the relabel chain in `discovery_geopolitical_countries`
    # writes `tags` as a top-level label).
    top_level_tags = write_set.pop("tags", None)
    if top_level_tags is not None:
        scope_writes = write_set.setdefault("scope", {})
        scope_writes["tags"] = list(top_level_tags)

    merged = merge_descriptor_bodies(base, write_set)

    # Force the materialised L1 defaults — the template carries L2 +
    # configured; instances are L1 + draft until the runtime promotes.
    merged_identity = merged.setdefault("identity", {})
    merged_identity["abstraction_level"] = "L1"
    merged_identity["state"] = "draft"
    # Inherits: union the relabel chain's write with [template_id,
    # discovery_id]. The chain typically re-asserts the template id;
    # we always append the discovery id so the registry-side
    # `_load_prior_keys` reverse-lookup is unambiguous.
    inherits = list(merged_identity.get("inherits") or [])
    if template_id and template_id not in inherits:
        inherits.append(template_id)
    if discovery_descriptor.identity.id not in inherits:
        inherits.append(discovery_descriptor.identity.id)
    merged_identity["inherits"] = inherits

    # Stamp discovery provenance + ensure required identity fields.
    if "schema_uri" not in merged_identity:
        merged_identity["schema_uri"] = discovery_descriptor.identity.schema_uri
    if "owner" not in merged_identity:
        merged_identity["owner"] = discovery_descriptor.identity.owner
    if "created" not in merged_identity:
        merged_identity["created"] = datetime.now(tz=timezone.utc).isoformat()
    # Placeholder version — re-stamped by content_hash() after pydantic
    # validates the body. The placeholder shape matches the TargetIdentity
    # version regex (^[a-f0-9]{16,64}$); the content_hash output is the
    # real value.
    merged_identity.setdefault("version", "0" * 16)

    return merged


# ---------------------------------------------------------------------------
# materialize_discovered — single candidate
# ---------------------------------------------------------------------------


async def materialize_discovered(
    conn: asyncpg.Connection,
    candidate: CandidateTarget,
    discovery_descriptor: TargetDescriptor,
    relabel_rules: Sequence[RelabelRule] | None = None,
    *,
    template_body: Mapping[str, Any] | None = None,
    lookup_tables: Mapping[str, Mapping[str, Any]] | None = None,
    dlq: Any = None,
    actor: str = "discovery_materializer",
) -> MaterializeOutcome:
    """Materialise one candidate into a ``target_descriptors`` row.

    Steps:

      1. Resolve the discovery descriptor's relabel rules (parameter or
         ``discovery.relabel``) and apply them via
         :func:`evaluate_relabel_chain`. Dropped candidates short-circuit.
      2. Resolve the parent template body (parameter or DB lookup by
         ``discovery.inherits[0]``).
      3. Merge template + relabeled labels via
         :func:`merge_descriptor_bodies` and stamp discovery provenance.
      4. Validate against :class:`TargetDescriptor`. Validation failures
         route to the descriptor DLQ.
      5. UPSERT into ``target_descriptors`` keyed by
         ``(descriptor_id, version)`` with ``is_head=true`` for the new
         version row. Prior head for the same descriptor_id is demoted.

    The function uses the caller's connection (transaction-friendly:
    callers in :func:`reconcile_discovered_targets` wrap the loop in a
    single transaction). No NATS publish happens here — the orchestrator
    fires per-cycle events.
    """
    rules = list(
        relabel_rules
        if relabel_rules is not None
        else (
            discovery_descriptor.discovery.relabel
            if discovery_descriptor.discovery
            else []
        )
    )

    relabel_result: RelabelResult = evaluate_relabel_chain(
        candidate, rules, lookup_tables=lookup_tables
    )
    if relabel_result.dropped:
        logger.info(
            "discovery.materialize.dropped discovery=%s natural_key=%s "
            "rule_index=%s action=%s reason=%s",
            discovery_descriptor.identity.id,
            candidate.natural_key,
            relabel_result.dropped_at,
            relabel_result.dropped_by_action,
            relabel_result.dropped_reason,
        )
        return MaterializeOutcome(
            natural_key=candidate.natural_key,
            descriptor_id=None,
            row_uuid=None,
            version=None,
            dropped=True,
            dropped_reason=relabel_result.dropped_reason,
        )

    # Resolve the template body. Prefer the parameter (orchestrator
    # passes the already-fetched body to avoid N+1); fall back to a
    # direct lookup using the discovery descriptor's inherits[0].
    resolved_template_body = template_body
    if resolved_template_body is None:
        parent_id: str | None = None
        if discovery_descriptor.identity.inherits:
            parent_id = discovery_descriptor.identity.inherits[0]
        if parent_id is None:
            # Discovery descriptors should always declare a parent
            # template per L-200 / topology_redesign §5.2; treat as a
            # programming error rather than silently materialising a
            # parent-less body.
            raise ValueError(
                f"discovery descriptor {discovery_descriptor.identity.id!r} "
                f"has empty identity.inherits — cannot resolve template body"
            )
        row = await conn.fetchrow(
            "SELECT body FROM target_descriptors "
            "WHERE descriptor_id = $1 AND is_head LIMIT 1",
            parent_id,
        )
        if row is None:
            raise ValueError(
                f"discovery descriptor {discovery_descriptor.identity.id!r} "
                f"references template {parent_id!r} which is not registered"
            )
        body = row["body"]
        if isinstance(body, str):
            body = json.loads(body)
        resolved_template_body = body

    assembled = _assemble_materialized_body(
        template_body=resolved_template_body,
        relabeled_labels=relabel_result.labels,
        discovery_descriptor=discovery_descriptor,
        relabel_rules=rules,
    )

    # Validate. Use the JSON validator path because the descriptor schema
    # is strict=True (enum + datetime fields reject raw-string coercion in
    # the python validator). Round-tripping through JSON applies the
    # standard lax→strict coercion at the pydantic_core level.
    try:
        descriptor = TargetDescriptor.model_validate_json(
            json.dumps(assembled, default=str)
        )
    except ValidationError as exc:
        logger.warning(
            "discovery.materialize.invalid discovery=%s natural_key=%s "
            "errors=%s",
            discovery_descriptor.identity.id,
            candidate.natural_key,
            exc.errors(),
        )
        if dlq is not None:
            try:
                await dlq.record(
                    actor=actor,
                    namespace="target",
                    attempted_payload=assembled,
                    declared_schema_uri=assembled.get("identity", {}).get(
                        "schema_uri"
                    ),
                    validation_error={
                        "reason": "discovery_materialization_invalid",
                        "discovery_id": discovery_descriptor.identity.id,
                        "natural_key": candidate.natural_key,
                        "errors": exc.errors(),
                    },
                )
            except Exception as dlq_exc:                      # pragma: no cover
                logger.exception(
                    "discovery.materialize.dlq_write_failed err=%s",
                    dlq_exc,
                )
        return MaterializeOutcome(
            natural_key=candidate.natural_key,
            descriptor_id=assembled.get("identity", {}).get("id"),
            row_uuid=None,
            version=None,
            dlq=True,
            dlq_reason=str(exc.errors()[0] if exc.errors() else "validation_error"),
            materialized_body=assembled,
        )

    # Stamp the content-hash version (excludes identity.version).
    hash_hex = content_hash(descriptor)
    # Re-validate with the stamped version so the persisted body's
    # identity.version matches the substrate column.
    body_with_version = descriptor.model_dump(mode="json", by_alias=True)
    body_with_version["identity"]["version"] = hash_hex

    # Fetch the current head row once — used by the owner guard (Fix 1)
    # and the semantic-idempotency guard (Fix 2) below. Both run BEFORE
    # the exact-version idempotency insert so we never demote/re-mint a
    # head we shouldn't touch.
    head = await conn.fetchrow(
        "SELECT version, state, owner, body FROM target_descriptors "
        "WHERE descriptor_id = $1 AND is_head LIMIT 1",
        descriptor.identity.id,
    )

    # Fix 1 — never demote an ACTIVE target owned by a DIFFERENT owner.
    # The discovery cycle re-materialises descriptors every cycle; when a
    # materialised descriptor_id collides with an operator-registered
    # workingset target that the operator has promoted to ACTIVE, demoting
    # it back to a fresh `draft` silently stalls that target's analysts.
    # The discovery loop must yield to the operator: skip the write
    # entirely and leave the operator's active head untouched.
    if (
        head is not None
        and head["state"] == LifecycleState.ACTIVE.value
        and head["owner"] != discovery_descriptor.identity.owner
    ):
        logger.warning(
            "discovery.materialize.skip_active_operator_target discovery=%s "
            "descriptor_id=%s head_owner=%s head_state=%s discovery_owner=%s",
            discovery_descriptor.identity.id,
            descriptor.identity.id,
            head["owner"],
            head["state"],
            discovery_descriptor.identity.owner,
        )
        return MaterializeOutcome(
            natural_key=candidate.natural_key,
            descriptor_id=descriptor.identity.id,
            row_uuid=None,
            version=hash_hex,
            dropped=True,
            dropped_reason="skip_active_operator_target",
        )

    # Fix 2 — semantic idempotency. When a head already exists for this
    # descriptor_id and its *behavioural* fingerprint (scope/sources/
    # analyst/pipeline/outputs) equals the new body's, this cycle changed
    # only provenance (e.g. identity.inherits' parent-version pointer).
    # Treat it as a no-op: do NOT demote, do NOT insert a new content-hash
    # version. This is the root fix for the observed per-cycle version
    # churn that re-drafted operator targets. Genuinely-changed bodies
    # fall through to the exact-version idempotency check unchanged.
    if head is not None:
        head_body = head["body"]
        if isinstance(head_body, str):
            try:
                head_body = json.loads(head_body)
            except Exception:
                head_body = {}
        if not isinstance(head_body, Mapping):
            head_body = {}
        if _semantic_fingerprint(head_body) == _semantic_fingerprint(
            body_with_version
        ):
            # Prefer the substrate `version` column (always present) over
            # the body's identity.version, which may be stale/absent.
            head_version = head["version"]
            logger.debug(
                "discovery.materialize.semantic_unchanged discovery=%s "
                "descriptor_id=%s head_version=%s candidate_version=%s",
                discovery_descriptor.identity.id,
                descriptor.identity.id,
                head_version,
                hash_hex,
            )
            return MaterializeOutcome(
                natural_key=candidate.natural_key,
                descriptor_id=descriptor.identity.id,
                row_uuid=None,
                version=head_version,
                materialized_body=body_with_version,
            )

    # Idempotency: if a row with the same (descriptor_id, version) is
    # already head, this cycle is a no-op write (the materialised body
    # didn't change since the last cycle).
    existing = await conn.fetchrow(
        "SELECT version, is_head FROM target_descriptors "
        "WHERE descriptor_id = $1 AND version = $2 LIMIT 1",
        descriptor.identity.id,
        hash_hex,
    )
    row_uuid = uuid4()
    if existing is None:
        # Demote prior head, if any.
        await conn.execute(
            "UPDATE target_descriptors SET is_head = FALSE "
            "WHERE descriptor_id = $1 AND is_head",
            descriptor.identity.id,
        )
        await conn.execute(
            """
            INSERT INTO target_descriptors
              (descriptor_id, version, schema_uri, is_head,
               abstraction_level, state, owner, name, body, inherits,
               created_at)
            VALUES ($1, $2, $3, TRUE, $4, $5, $6, $7, $8::jsonb, $9, NOW())
            """,
            descriptor.identity.id,
            hash_hex,
            descriptor.identity.schema_uri,
            descriptor.identity.abstraction_level.value,
            descriptor.identity.state.value,
            descriptor.identity.owner,
            descriptor.identity.name,
            canonical_json(body_with_version).decode("utf-8"),
            list(descriptor.identity.inherits),
        )
        logger.info(
            "discovery.materialize.inserted discovery=%s descriptor_id=%s "
            "version=%s",
            discovery_descriptor.identity.id,
            descriptor.identity.id,
            hash_hex,
        )
    else:
        # Same version already exists — possibly stale `is_head` if a
        # concurrent rollback happened, so re-assert head.
        if not existing["is_head"]:
            await conn.execute(
                "UPDATE target_descriptors SET is_head = FALSE "
                "WHERE descriptor_id = $1 AND is_head",
                descriptor.identity.id,
            )
            await conn.execute(
                "UPDATE target_descriptors SET is_head = TRUE "
                "WHERE descriptor_id = $1 AND version = $2",
                descriptor.identity.id,
                hash_hex,
            )
        logger.debug(
            "discovery.materialize.unchanged discovery=%s descriptor_id=%s "
            "version=%s",
            discovery_descriptor.identity.id,
            descriptor.identity.id,
            hash_hex,
        )

    return MaterializeOutcome(
        natural_key=candidate.natural_key,
        descriptor_id=descriptor.identity.id,
        row_uuid=row_uuid,
        version=hash_hex,
        materialized_body=body_with_version,
    )


# ---------------------------------------------------------------------------
# Discovery-state persistence helpers
# ---------------------------------------------------------------------------
#
# Per L-106 §2 the registry tracks `(discovery_id, natural_key) → descriptor_id`
# so subsequent cycles can classify retained / new / disappeared without
# scanning the target_descriptors table by name. The migration set
# doesn't ship a dedicated `discovery_state` table yet — Wave D piggybacks
# on `target_descriptors.body` by querying the rows whose body declares
# `identity.inherits @> ARRAY[discovery_id]` for the prior cycle's
# natural_key set. This is the L-200 lean for Wave D; the proper
# `discovery_state` table is L-181's follow-up (tracked in the L-204 /
# Phase 9 tail).


async def _load_prior_keys(
    conn: asyncpg.Connection,
    discovery_descriptor: TargetDescriptor,
) -> dict[str, str]:
    """Return ``{natural_key: descriptor_id}`` for rows previously
    materialised by this discovery descriptor.

    The mapping is reconstructed from the descriptor body itself —
    materialised L1 instances embed the discovery descriptor's id in
    ``inherits`` (the L-200 relabel chain re-asserts the *template* id;
    the registry inserts append ``discovery_descriptor.identity.id`` as
    well so this lookup is unambiguous).

    Wave D substrate-storage note: the descriptor row carries the natural
    key in ``body.identity.id`` (e.g. ``country_geopolitical_br``). The
    inverse mapping back to the *candidate* natural_key (``BR``) lives in
    the body's discovery-provenance trailer that
    :func:`_assemble_materialized_body` writes via the relabel chain.
    The current contract: the natural_key is the iso2 code carried
    in ``scope.geo[0]`` so the prior-keys query reads exactly that.
    """
    rows = await conn.fetch(
        """
        SELECT descriptor_id, body
        FROM target_descriptors
        WHERE is_head
          AND $1 = ANY(inherits)
          AND state <> 'retired'
        """,
        discovery_descriptor.identity.id,
    )
    out: dict[str, str] = {}
    for r in rows:
        body = r["body"]
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception as exc:
                # #95 silent-drop-on-critical-path: an unparseable body would
                # exclude this materialised descriptor with NO trace. Flow
                # unchanged (still skip) — made observable.
                logger.warning(
                    "discovered_materializer.skip_unparsable_body descriptor_id=%s err=%s",
                    r["descriptor_id"], exc,
                )
                continue
        # The discovery descriptor's natural_key shape is the candidate's
        # stable id — for country_list_discovery this is the iso2 in
        # scope.geo[0]. Materialisers can override this by writing a
        # `_discovery_natural_key` label; if present, use it.
        nk = (body or {}).get("_discovery_natural_key")
        if not nk:
            scope = (body or {}).get("scope", {}) or {}
            geo = scope.get("geo") or []
            if geo:
                nk = str(geo[0])
        if not nk:
            # Fallback: use the descriptor id stripped of the deterministic
            # prefix; better to over-classify as 'new' than silently
            # double-write.
            nk = r["descriptor_id"]
        out[str(nk)] = r["descriptor_id"]
    return out


async def _retire_descriptor(
    conn: asyncpg.Connection,
    descriptor_id: str,
) -> None:
    """Mark a previously-materialised L1 instance as ``retired``.

    Updates ``target_descriptors.state`` on the head row; the runtime's
    actor loop observes the state change and stops invoking the actor.
    """
    await conn.execute(
        """
        UPDATE target_descriptors
        SET state = 'retired'
        WHERE descriptor_id = $1 AND is_head
        """,
        descriptor_id,
    )
    logger.info("discovery.materialize.retired descriptor_id=%s", descriptor_id)


async def _pause_discovery(
    conn: asyncpg.Connection,
    discovery_descriptor: TargetDescriptor,
) -> None:
    """Transition the discovery descriptor's head row to ``paused``.

    Per L-106 §5 ``alert_and_pause`` semantics: existing materialised
    instances stay ``active`` (ingest continues on them); only the
    discovery cycle pauses (no new materialisation, no retirement) until
    an operator clears the anomaly via the resync-review UI.
    """
    await conn.execute(
        """
        UPDATE target_descriptors
        SET state = $2
        WHERE descriptor_id = $1 AND is_head
        """,
        discovery_descriptor.identity.id,
        LifecycleState.PAUSED.value,
    )
    logger.warning(
        "discovery.materialize.paused discovery=%s",
        discovery_descriptor.identity.id,
    )


# ---------------------------------------------------------------------------
# reconcile_discovered_targets — full per-cycle loop
# ---------------------------------------------------------------------------


async def reconcile_discovered_targets(
    conn: asyncpg.Connection,
    discovery_descriptor: TargetDescriptor,
    candidates: Sequence[CandidateTarget],
    *,
    template_body: Mapping[str, Any] | None = None,
    lookup_tables: Mapping[str, Mapping[str, Any]] | None = None,
    dlq: Any = None,
    nats_publish: Any = None,
    actor: str = "discovery_materializer",
) -> ReconcileResult:
    """Run one discovery cycle's materialisation loop.

    Per L-106 §2-§5 + L-180 contract:

      1. Apply :func:`materialize_discovered` to every candidate (with
         the relabel chain + parent merge + validation + substrate write).
      2. Diff the current cycle's natural_key set against the prior
         cycle's via :func:`evaluate_disappearance`.
      3. If ``verdict == proceed`` (or ``skipped`` for cold-start):
         retire the descriptors whose natural_key disappeared.
      4. If ``verdict == anomaly``: route disappeared keys to the DLQ +
         pause the discovery descriptor (per ``on_anomaly`` policy).
      5. Optionally publish a per-cycle event on
         ``legba.discovery.cycle.<discovery_id>`` (caller-provided
         publisher; no-op if absent).

    The function uses the caller's connection — wrap in a transaction at
    the callsite if cycle-level atomicity is required. Per-row writes
    inside the loop are autocommitted otherwise.
    """
    cycle_started_at = datetime.now(tz=timezone.utc)
    discovery_id = discovery_descriptor.identity.id

    # Resolve the policy + template once per cycle.
    block = discovery_descriptor.discovery
    if block is None:
        raise ValueError(
            f"reconcile_discovered_targets called on descriptor "
            f"{discovery_id!r} with no discovery block"
        )
    policy: ResyncPolicy = block.resync_policy or ResyncPolicy()
    rules = list(block.relabel)

    # Load prior-cycle natural_key set before we mutate substrate.
    prior_map = await _load_prior_keys(conn, discovery_descriptor)
    prior_keys = set(prior_map.keys())

    # Materialise each candidate.
    outcomes: list[MaterializeOutcome] = []
    current_keys: set[str] = set()
    for cand in candidates:
        current_keys.add(cand.natural_key)
        try:
            outcome = await materialize_discovered(
                conn,
                cand,
                discovery_descriptor,
                rules,
                template_body=template_body,
                lookup_tables=lookup_tables,
                dlq=dlq,
                actor=actor,
            )
        except Exception as exc:
            logger.exception(
                "discovery.materialize.error discovery=%s natural_key=%s err=%s",
                discovery_id, cand.natural_key, exc,
            )
            outcome = MaterializeOutcome(
                natural_key=cand.natural_key,
                descriptor_id=None,
                row_uuid=None,
                version=None,
                dlq=True,
                dlq_reason=f"materialize_exception:{type(exc).__name__}:{exc}",
            )
            if dlq is not None:
                try:
                    await dlq.record(
                        actor=actor,
                        namespace="target",
                        attempted_payload={
                            "discovery_id": discovery_id,
                            "candidate": cand.model_dump(mode="json", by_alias=True),
                        },
                        declared_schema_uri=None,
                        validation_error={
                            "reason": "materialize_exception",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                except Exception as dlq_exc:                  # pragma: no cover
                    logger.exception(
                        "discovery.materialize.dlq_write_failed err=%s",
                        dlq_exc,
                    )
        outcomes.append(outcome)

    # Apply disappearance enforcement.
    decision = evaluate_disappearance(
        prior_keys=prior_keys,
        current_keys=current_keys,
        policy=policy,
    )

    retired: list[str] = []
    paused = False

    if decision.should_retire_disappeared:
        for nk in decision.disappeared:
            descriptor_id = prior_map.get(nk)
            if descriptor_id is None:
                continue
            try:
                await _retire_descriptor(conn, descriptor_id)
                retired.append(descriptor_id)
            except Exception as exc:                          # pragma: no cover
                logger.exception(
                    "discovery.materialize.retire_failed descriptor_id=%s err=%s",
                    descriptor_id, exc,
                )

    if decision.anomaly:
        # Anomaly path: route disappeared keys to DLQ (unless policy is
        # retire_anyway, which short-circuits should_retire_disappeared
        # → True and we already retired above).
        if dlq is not None and decision.routes_to_dlq:
            for nk in decision.routes_to_dlq:
                try:
                    await dlq.record(
                        actor=actor,
                        namespace="discovery_resync",
                        attempted_payload={
                            "discovery_id": discovery_id,
                            "natural_key": nk,
                            "descriptor_id": prior_map.get(nk),
                            "prior_count": decision.prior_count,
                            "current_count": decision.current_count,
                            "ratio": decision.ratio,
                            "threshold": decision.threshold,
                        },
                        declared_schema_uri=None,
                        validation_error={
                            "reason": "discovery_resync_anomaly",
                            "policy": decision.on_anomaly,
                            "ratio": decision.ratio,
                            "threshold": decision.threshold,
                        },
                    )
                except Exception as exc:                      # pragma: no cover
                    logger.exception(
                        "discovery.materialize.resync_dlq_failed natural_key=%s err=%s",
                        nk, exc,
                    )

        if decision.should_pause:
            try:
                await _pause_discovery(conn, discovery_descriptor)
                paused = True
            except Exception as exc:                          # pragma: no cover
                logger.exception(
                    "discovery.materialize.pause_failed discovery=%s err=%s",
                    discovery_id, exc,
                )

        if decision.should_alert and nats_publish is not None:
            try:
                payload = {
                    "discovery_id": discovery_id,
                    "verdict": decision.verdict,
                    "policy": decision.on_anomaly,
                    "prior_count": decision.prior_count,
                    "current_count": decision.current_count,
                    "ratio": decision.ratio,
                    "threshold": decision.threshold,
                    "disappeared": decision.disappeared,
                    "cycle_started_at": cycle_started_at.isoformat(),
                }
                await nats_publish(
                    f"legba.discovery.resync_anomaly.{discovery_id}",
                    json.dumps(payload).encode("utf-8"),
                )
            except Exception as exc:                          # pragma: no cover
                logger.warning(
                    "discovery.materialize.nats_alert_failed err=%s", exc,
                )

    cycle_ended_at = datetime.now(tz=timezone.utc)

    result = ReconcileResult(
        discovery_id=discovery_id,
        cycle_started_at=cycle_started_at,
        cycle_ended_at=cycle_ended_at,
        candidates_in=len(candidates),
        materialized=outcomes,
        retired=retired,
        routed_to_dlq=list(decision.routes_to_dlq) if decision.anomaly else [],
        disappearance=decision,
        paused=paused,
    )

    # Best-effort per-cycle event for the runtime / observability layer.
    if nats_publish is not None:
        try:
            payload = {
                "discovery_id": discovery_id,
                "cycle_started_at": cycle_started_at.isoformat(),
                "cycle_ended_at": cycle_ended_at.isoformat(),
                "candidates_in": result.candidates_in,
                "inserted_count": result.inserted_count,
                "dropped_count": result.dropped_count,
                "dlq_count": result.dlq_count,
                "retired_count": len(result.retired),
                "anomaly": decision.anomaly,
                "paused": paused,
            }
            await nats_publish(
                f"legba.discovery.cycle.{discovery_id}",
                json.dumps(payload).encode("utf-8"),
            )
        except Exception as exc:                              # pragma: no cover
            logger.warning(
                "discovery.materialize.nats_cycle_failed err=%s", exc,
            )

    return result


__all__ = [
    "MaterializeOutcome",
    "ReconcileResult",
    "merge_descriptor_bodies",
    "materialize_discovered",
    "reconcile_discovered_targets",
]
