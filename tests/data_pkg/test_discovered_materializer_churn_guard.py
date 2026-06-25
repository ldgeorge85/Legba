# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Target-churn hardening for the discovery materialiser.

Regression coverage for the live target-churn bug: the registry-side
discovery materialiser
(:func:`legba.data.registry.discovered_materializer.materialize_discovered`)
re-materialised target descriptors every discovery cycle and, when the
materialised ``descriptor_id`` collided with an operator-registered
workingset target, it (a) minted a NEW content-hash version each cycle
even when the SEMANTIC content was unchanged (because ``identity.inherits``
carries a varying parent-version pointer), and (b) DEMOTED the operator's
``active`` head to a fresh ``draft``. Live impact: the operator's 19
``country_g20_*`` active targets got re-drafted every ~10-30 min, silently
stalling the per-target / cross-target country analysts for ~40h.

Two durable code guards close this class:

  * **Fix 1** — never demote an ACTIVE head owned by a DIFFERENT owner than
    the discovery descriptor. The materialiser yields to the operator and
    returns a dropped outcome (``dropped_reason="skip_active_operator_target"``).
  * **Fix 2** — semantic idempotency: when the current head's behavioural
    fingerprint (scope / sources / analyst / pipeline / outputs) equals the
    new body's, the cycle changed only provenance — treat it as a no-op
    (no demote, no new version).

These tests use real asyncpg against the test Postgres (the ``migrated_pg``
session fixture from ``tests/data_pkg/conftest.py``). Descriptor ids are
uniquified per test with a nonce to avoid shared-DB collisions.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.config import PostgresConfig
from legba.data.discovery import CandidateTarget
from legba.data.provenance import canonical_json
from legba.data.registry.discovered_materializer import (
    _semantic_fingerprint,
    materialize_discovered,
)
from legba.data.schemas import TargetDescriptor, content_hash


# ---------------------------------------------------------------------------
# Fixtures + body builders
# ---------------------------------------------------------------------------


# The operator-target shape that got churned live: an L1, ACTIVE,
# source-first ``country_g20_*`` body owned by ``p17_reregister``. Used as
# both the seeded operator head and the materialisation *template* (so the
# materialised body inherits the same behavioural fields).
_REPO_ROOT = Path("/usr/local/deployments/active/legba")
_G20_YAML = Path(__file__).resolve().parents[2] / "descriptors" / "target_country_g20.yaml"


def _g20_body() -> dict:
    return yaml.safe_load(_G20_YAML.read_text(encoding="utf-8"))


def _discovery_body(nonce: str, *, owner: str, inherits: list[str]) -> dict:
    """A minimal, schema-valid L2 discovery descriptor.

    Its relabel chain writes ``identity.id`` verbatim from the candidate's
    ``target_id`` label, so the test controls the materialised
    ``descriptor_id`` directly (and can collide it with a seeded head).
    """
    return {
        "identity": {
            "id": f"discovery_churn_{nonce}",
            "name": "churn-guard test discovery",
            "schema_uri": "legba/target/2.0.0",
            "version": "0000000000000000",
            "abstraction_level": "L2",
            "inherits": inherits,
            "state": "configured",
            "owner": owner,
            "created": "2026-05-21T00:00:00Z",
        },
        "scope": {
            "domain": "geo",
            "geo": ["XX"],
            "languages": ["en"],
            "entity_classes": ["country"],
            "relationship_types": ["AlliedWith"],
            "time_horizon_days": 90,
            "predicate": None,
        },
        "discovery": {
            "kind": "country_list_discovery",
            "list_source": "iso_3166",
            "emit_per_match": True,
            "relabel": [
                {
                    "source_labels": ["target_id"],
                    "target_label": "identity.id",
                    "action": "format",
                    "replacement": "{{ target_id }}",
                }
            ],
        },
        "sources": [],
        "pipeline": {"ingestion_filters": [], "enrichment": [], "routing": []},
        "analyst": None,
        "outputs": [],
    }


def _discovery_descriptor(
    nonce: str, *, owner: str = "legba_geopolitical", inherits: list[str] | None = None
) -> TargetDescriptor:
    body = _discovery_body(nonce, owner=owner, inherits=inherits or ["template_country"])
    return TargetDescriptor.model_validate_json(json.dumps(body, default=str))


async def _seed_head(
    conn: asyncpg.Connection,
    descriptor_id: str,
    body: dict,
    *,
    state: str,
    owner: str,
) -> str:
    """Insert a head ``target_descriptors`` row from a (validated) body.

    Returns the content-hash version stamped on the row.
    """
    body = copy.deepcopy(body)
    body.setdefault("identity", {})["id"] = descriptor_id
    descriptor = TargetDescriptor.model_validate_json(json.dumps(body, default=str))
    version = content_hash(descriptor)
    body_with_version = descriptor.model_dump(mode="json", by_alias=True)
    body_with_version["identity"]["version"] = version
    await conn.execute(
        """
        INSERT INTO target_descriptors
          (descriptor_id, version, schema_uri, is_head, abstraction_level,
           state, owner, name, body, inherits, created_at)
        VALUES ($1, $2, $3, TRUE, $4, $5, $6, $7, $8::jsonb, $9, NOW())
        """,
        descriptor_id,
        version,
        descriptor.identity.schema_uri,
        descriptor.identity.abstraction_level.value,
        state,
        owner,
        descriptor.identity.name,
        canonical_json(body_with_version).decode("utf-8"),
        list(descriptor.identity.inherits),
    )
    return version


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    try:
        yield c
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Fix 1 — never demote an ACTIVE target owned by a different owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix1_skip_active_operator_target_owned_by_other(conn: asyncpg.Connection):
    """An ACTIVE head owned by a DIFFERENT owner is NOT demoted; the
    materialiser returns a dropped outcome and leaves the operator's head
    untouched (still is_head + active)."""
    nonce = uuid4().hex[:10]
    descriptor_id = f"country_g20_churn_{nonce}"

    # Operator's active head — owned by p17_reregister.
    op_version = await _seed_head(
        conn, descriptor_id, _g20_body(), state="active", owner="p17_reregister"
    )

    # Discovery descriptor owned by a DIFFERENT owner (legba_geopolitical).
    discovery = _discovery_descriptor(nonce, owner="legba_geopolitical")
    candidate = CandidateTarget(id=descriptor_id, labels={"target_id": descriptor_id})

    outcome = await materialize_discovered(
        conn,
        candidate,
        discovery,
        list(discovery.discovery.relabel),
        template_body=_g20_body(),
    )

    assert outcome.dropped is True
    assert outcome.dropped_reason == "skip_active_operator_target"
    assert outcome.descriptor_id == descriptor_id
    assert outcome.row_uuid is None
    # version echoes the candidate's freshly-computed content hash.
    assert isinstance(outcome.version, str) and len(outcome.version) >= 16
    assert outcome.dlq is False

    # The operator's active head is intact + still the only row.
    rows = await conn.fetch(
        "SELECT version, is_head, state, owner FROM target_descriptors "
        "WHERE descriptor_id = $1",
        descriptor_id,
    )
    assert len(rows) == 1, "no new version should have been minted"
    head = rows[0]
    assert head["is_head"] is True
    assert head["state"] == "active"
    assert head["owner"] == "p17_reregister"
    assert head["version"] == op_version


@pytest.mark.asyncio
async def test_fix1_negative_same_owner_active_head_proceeds(conn: asyncpg.Connection):
    """When the ACTIVE head is owned by the discovery's OWN owner, the
    owner guard does NOT fire and a genuinely-changed body materialises a
    new version (demote-old + insert-new)."""
    nonce = uuid4().hex[:10]
    descriptor_id = f"country_g20_churn_{nonce}"

    # Active head owned by the SAME owner as the discovery descriptor.
    own_body = _g20_body()
    own_body["identity"]["owner"] = "legba_geopolitical"
    await _seed_head(
        conn, descriptor_id, own_body, state="active", owner="legba_geopolitical"
    )

    discovery = _discovery_descriptor(nonce, owner="legba_geopolitical")
    candidate = CandidateTarget(id=descriptor_id, labels={"target_id": descriptor_id})

    # Template with a DIFFERENT scope so the body is NOT a semantic no-op.
    template = _g20_body()
    template["scope"]["time_horizon_days"] = 42

    outcome = await materialize_discovered(
        conn,
        candidate,
        discovery,
        list(discovery.discovery.relabel),
        template_body=template,
    )

    assert outcome.dropped is False
    assert outcome.dlq is False

    rows = await conn.fetch(
        "SELECT is_head, state FROM target_descriptors WHERE descriptor_id = $1 "
        "ORDER BY created_at",
        descriptor_id,
    )
    # Old head demoted, new draft head inserted → two rows, exactly one head.
    assert len(rows) == 2
    heads = [r for r in rows if r["is_head"]]
    assert len(heads) == 1
    assert heads[0]["state"] == "draft"


@pytest.mark.asyncio
async def test_fix1_draft_head_other_owner_not_skipped(conn: asyncpg.Connection):
    """A DRAFT head owned by a different owner is NOT protected by the
    owner guard (only ACTIVE heads are) — materialisation proceeds.

    Judgment-call coverage: the guard is intentionally narrow (ACTIVE +
    different-owner), so a draft owned by anyone re-materialises normally.
    """
    nonce = uuid4().hex[:10]
    descriptor_id = f"country_g20_churn_{nonce}"

    # Draft head owned by a different owner — NOT protected.
    await _seed_head(
        conn, descriptor_id, _g20_body(), state="draft", owner="someone_else"
    )

    discovery = _discovery_descriptor(nonce, owner="legba_geopolitical")
    candidate = CandidateTarget(id=descriptor_id, labels={"target_id": descriptor_id})

    # Different scope → genuinely-changed body so we exercise the insert path.
    template = _g20_body()
    template["scope"]["time_horizon_days"] = 7

    outcome = await materialize_discovered(
        conn,
        candidate,
        discovery,
        list(discovery.discovery.relabel),
        template_body=template,
    )

    assert outcome.dropped is False
    assert outcome.dropped_reason == ""
    rows = await conn.fetch(
        "SELECT is_head FROM target_descriptors WHERE descriptor_id = $1",
        descriptor_id,
    )
    assert len(rows) == 2  # demoted old draft + new draft


# ---------------------------------------------------------------------------
# Fix 2 — semantic idempotency (stop provenance-only version churn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix2_provenance_only_change_is_semantic_noop(conn: asyncpg.Connection):
    """Two cycles with semantically-identical bodies but a different
    ``identity.inherits`` parent pointer produce exactly ONE version row —
    the second cycle is a semantic no-op (no demote, no re-mint)."""
    nonce1 = uuid4().hex[:10]
    descriptor_id = f"country_g20_churn_{nonce1}"

    # Cycle 1 — discovery inherits [template_v1].
    discovery1 = _discovery_descriptor(
        nonce1, owner="legba_geopolitical", inherits=["template_v1"]
    )
    candidate = CandidateTarget(id=descriptor_id, labels={"target_id": descriptor_id})
    out1 = await materialize_discovered(
        conn,
        candidate,
        discovery1,
        list(discovery1.discovery.relabel),
        template_body=_g20_body(),
    )
    assert out1.dropped is False and out1.dlq is False
    head_v1 = await conn.fetchval(
        "SELECT version FROM target_descriptors WHERE descriptor_id = $1 AND is_head",
        descriptor_id,
    )
    assert head_v1 == out1.version

    # Cycle 2 — a DIFFERENT discovery (different id + inherits parent
    # pointer) materialising the SAME semantic body. Without Fix 2 this
    # would mint a new content-hash version because identity.inherits
    # changed; with Fix 2 it is a no-op.
    nonce2 = uuid4().hex[:10]
    discovery2 = _discovery_descriptor(
        nonce2, owner="legba_geopolitical", inherits=["template_v2"]
    )
    out2 = await materialize_discovered(
        conn,
        candidate,
        discovery2,
        list(discovery2.discovery.relabel),
        template_body=_g20_body(),
    )

    assert out2.dropped is False
    assert out2.dlq is False
    assert out2.version == head_v1, "semantic no-op must echo the existing head version"

    rows = await conn.fetch(
        "SELECT version, is_head FROM target_descriptors WHERE descriptor_id = $1",
        descriptor_id,
    )
    assert len(rows) == 1, "no new version row should be minted"
    assert rows[0]["is_head"] is True
    assert rows[0]["version"] == head_v1


# ---------------------------------------------------------------------------
# _semantic_fingerprint — pure helper
# ---------------------------------------------------------------------------


class TestSemanticFingerprint:
    """The pure fingerprint helper: equal behaviour ⇒ equal digest;
    provenance-only differences ⇒ SAME digest; behavioural differences ⇒
    DIFFERENT digest."""

    def _body(self) -> dict:
        return {
            "identity": {
                "id": "x",
                "version": "a" * 16,
                "inherits": ["template_country"],
                "created": "2026-01-01T00:00:00Z",
            },
            "scope": {"domain": "geo", "geo": ["BR"], "tags": ["news"]},
            "sources": [{"id": "rss"}],
            "analyst": {"use": "inline_target"},
            "pipeline": {"ingestion_filters": []},
            "outputs": [{"kind": "a2a_skill"}],
        }

    def test_identical_semantic_fields_equal(self):
        a = self._body()
        b = copy.deepcopy(a)
        assert _semantic_fingerprint(a) == _semantic_fingerprint(b)

    def test_key_order_does_not_matter(self):
        a = self._body()
        # Rebuild with reversed insertion order — canonical_json sorts keys.
        b = {k: a[k] for k in reversed(list(a.keys()))}
        assert _semantic_fingerprint(a) == _semantic_fingerprint(b)

    def test_provenance_only_change_same_fingerprint(self):
        a = self._body()
        b = copy.deepcopy(a)
        # Vary ONLY provenance fields — must not change the fingerprint.
        b["identity"]["inherits"] = ["template_country", "discovery_xyz"]
        b["identity"]["version"] = "f" * 16
        b["identity"]["created"] = "2099-12-31T23:59:59Z"
        assert _semantic_fingerprint(a) == _semantic_fingerprint(b)

    def test_differing_scope_changes_fingerprint(self):
        a = self._body()
        b = copy.deepcopy(a)
        b["scope"]["geo"] = ["US"]
        assert _semantic_fingerprint(a) != _semantic_fingerprint(b)

    def test_differing_sources_changes_fingerprint(self):
        a = self._body()
        b = copy.deepcopy(a)
        b["sources"] = [{"id": "rss"}, {"id": "gdelt"}]
        assert _semantic_fingerprint(a) != _semantic_fingerprint(b)

    def test_missing_keys_tolerated(self):
        # An empty body fingerprints deterministically (all-None projection).
        assert _semantic_fingerprint({}) == _semantic_fingerprint({})
        assert isinstance(_semantic_fingerprint({}), str)
