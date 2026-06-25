# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the L-110 descriptor registry support pieces.

No live containers needed — these exercise the bits that don't touch
Postgres or NATS: event-subject naming, audit-payload shape, signing /
verification, and the vocabulary validator factory.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from nacl.signing import SigningKey

from legba.data.registry.errors import UnknownVocabularyValue
from legba.data.registry.events import (
    DEAD_LETTER_TOPIC_PREFIX,
    DESCRIPTOR_TOPIC_PREFIX,
    VOCABULARY_UPDATED_TOPIC,
    audit_payload,
    dead_letter_event_payload,
    dead_letter_subject,
    descriptor_event_payload,
    descriptor_subject,
    vocabulary_subject,
)
from legba.data.registry.signing import (
    SigningIdentity,
    sign_audit_payload,
    verify_audit_payload,
)
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.schemas import (
    VocabularyEntry,
    VocabularyRegistry,
)


# ---------------------------------------------------------------------------
# NATS subject naming
# ---------------------------------------------------------------------------


def test_descriptor_subject_pattern():
    assert descriptor_subject("registered", "target", "br_energy") == (
        f"{DESCRIPTOR_TOPIC_PREFIX}.registered.target.br_energy"
    )
    assert descriptor_subject("retired", "analyst", "critic_v2") == (
        "descriptor.retired.analyst.critic_v2"
    )


def test_dead_letter_subject_with_and_without_id():
    assert dead_letter_subject("target", "x") == (
        f"{DEAD_LETTER_TOPIC_PREFIX}.target.x"
    )
    assert dead_letter_subject("target", None) == (
        f"{DEAD_LETTER_TOPIC_PREFIX}.target.__unknown__"
    )


def test_vocabulary_subject():
    assert vocabulary_subject("entity_class") == (
        f"{VOCABULARY_UPDATED_TOPIC}.entity_class"
    )


# ---------------------------------------------------------------------------
# Event payload shape
# ---------------------------------------------------------------------------


def test_audit_payload_required_fields():
    payload = audit_payload(
        action="register",
        family="target",
        descriptor_id="x",
        actor_id="lewis",
        to_version="a" * 64,
    )
    assert payload["action"] == "register"
    assert payload["namespace"] == "target"
    assert payload["descriptor_id"] == "x"
    assert payload["actor_id"] == "lewis"
    assert payload["from_version"] is None
    assert payload["to_version"] == "a" * 64
    assert "occurred_at" in payload
    assert payload["change_summary"] == {}


def test_descriptor_event_payload_includes_only_set_fields():
    p = descriptor_event_payload(
        action="updated",
        family="analyst",
        descriptor_id="d",
        actor="op",
        from_version="abc",
        to_version="def",
    )
    assert "version" not in p
    assert p["from_version"] == "abc"
    assert p["to_version"] == "def"
    assert p["action"] == "updated"


def test_dead_letter_event_payload_has_error_kind():
    p = dead_letter_event_payload(
        family="target",
        descriptor_id="x",
        actor="op",
        declared_schema_uri="legba/target/2.0.0",
        error_kind="vocabulary",
        error_summary="unknown values: ['foo']",
    )
    assert p["error_kind"] == "vocabulary"
    assert p["family"] == "target"
    assert p["dead_letter_id"] is None


# ---------------------------------------------------------------------------
# Ed25519 signing
# ---------------------------------------------------------------------------


def _fixed_identity() -> SigningIdentity:
    # Deterministic key derived from a 32-byte seed; lets us re-verify across
    # the test suite without leaking key material.
    seed = b"L-110-test-key-seed-deterministic-1234"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:test",
    )


def test_sign_then_verify_roundtrip():
    identity = _fixed_identity()
    payload = audit_payload(
        action="register",
        family="target",
        descriptor_id="abc",
        actor_id="lewis",
        to_version="deadbeef" * 8,
    )
    sig = sign_audit_payload(identity, payload)
    assert len(sig) == 64
    assert verify_audit_payload(identity.verify_key, payload, sig) is True


def test_sign_detects_payload_tampering():
    from legba.data.registry.errors import AuditChainError

    identity = _fixed_identity()
    payload = audit_payload(
        action="register",
        family="target",
        descriptor_id="abc",
        actor_id="lewis",
        to_version="deadbeef" * 8,
    )
    sig = sign_audit_payload(identity, payload)
    tampered = dict(payload)
    tampered["actor_id"] = "mallory"
    with pytest.raises(AuditChainError):
        verify_audit_payload(identity.verify_key, tampered, sig)


# ---------------------------------------------------------------------------
# Vocabulary validator factory (no DB — uses an injected snapshot)
# ---------------------------------------------------------------------------


class _StaticCache(VocabularyCache):
    """Subclass that skips the DB and seeds the snapshot directly."""

    def __init__(self, snapshot: dict[str, set[str]], aliases: dict[str, dict[str, str]] | None = None):
        # Bypass parent __init__ that wants a pg_store.
        self._pg = None
        self._seed_aliases = True
        self._values_by_family = {k: set(v) for k, v in snapshot.items()}
        self._aliases = aliases or {}
        # Build a registry that matches the snapshot for symmetry.
        entries = []
        for family, values in snapshot.items():
            for v in values:
                entries.append(
                    VocabularyEntry(
                        family=family,
                        value=v,
                        schema_uri="legba/vocabulary/1.0.0",
                        introduced=datetime.now(tz=timezone.utc),
                    )
                )
        self._registry = VocabularyRegistry(entries=entries)
        import asyncio
        self._lock = asyncio.Lock()
        self._sub = None
        self._nats = None


def test_vocabulary_validator_passes_known_values():
    cache = _StaticCache({"entity_class": {"organization", "country"}})
    validate = cache.make_validator("entity_class")
    assert validate(["organization", "country"]) == ["organization", "country"]


def test_vocabulary_validator_rejects_unknown_values():
    cache = _StaticCache({"entity_class": {"organization"}})
    validate = cache.make_validator("entity_class")
    with pytest.raises(UnknownVocabularyValue) as exc:
        validate(["organization", "ufo", "cryptid"])
    assert exc.value.family == "entity_class"
    assert exc.value.unknown == ["cryptid", "ufo"]


def test_vocabulary_validator_resolves_aliases():
    cache = _StaticCache(
        {"relationship_type": {"InvolvedIn"}},
        aliases={"relationship_type": {"INVOLVED_IN": "InvolvedIn"}},
    )
    validate = cache.make_validator("relationship_type")
    # Alias resolves to canonical.
    assert validate(["INVOLVED_IN"]) == ["InvolvedIn"]
    assert validate(["InvolvedIn"]) == ["InvolvedIn"]


def test_vocabulary_validator_contains_helper():
    cache = _StaticCache(
        {"entity_class": {"organization"}},
        aliases={"entity_class": {"org": "organization"}},
    )
    assert cache.contains("entity_class", "organization") is True
    assert cache.contains("entity_class", "org") is True
    assert cache.contains("entity_class", "unknown") is False


def test_vocabulary_validator_families_listing():
    cache = _StaticCache({"entity_class": {"x"}, "relationship_type": {"Y"}})
    assert cache.families() == ["entity_class", "relationship_type"]
