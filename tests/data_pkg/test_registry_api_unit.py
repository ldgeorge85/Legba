# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the L-113 registry HTTP router.

These exercise the FastAPI router with stand-in registry stubs so we don't
need Postgres / NATS. The integration tests
(`test_registry_api_integration.py`) hit the full real-substrate stack.

What's covered here:
  * Request/response pydantic shapes round-trip.
  * Auth modes (dev vs. enforced).
  * Error mapping (DescriptorNotFound → 404, VersionConflict → 409, etc.).
  * Vault never returns plaintext.

Stubs implement the minimum surface each endpoint touches.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from legba.data.registry.api import (
    API_TOKEN_ENV,
    RegistryAPIDeps,
    build_router,
)
from legba.data.registry.descriptor import DescriptorRow, Family
from legba.data.registry.errors import (
    DescriptorNotFound,
    DescriptorValidationError,
    IllegalLifecycleTransition,
    VersionConflict,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2026, 5, 16, 10, 0, 0, tzinfo=timezone.utc)


class _StubDescriptorRegistry:
    """Minimal DescriptorRegistry surface."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.behavior: dict[str, Any] = {}  # method -> callable or value

    def _record(self, name: str, *a, **kw) -> None:
        self.calls.append((name, a, kw))

    async def register(self, descriptor, actor, *, actor_role="operator"):
        self._record("register", descriptor, actor)
        if "register" in self.behavior:
            v = self.behavior["register"]
            if isinstance(v, Exception):
                raise v
            return v
        return _make_row(descriptor_id=descriptor.identity.id)

    async def get(self, descriptor_id, *, family, version=None):
        self._record("get", descriptor_id, family, version)
        if "get" in self.behavior:
            v = self.behavior["get"]
            if isinstance(v, Exception):
                raise v
            return v
        return _make_row(descriptor_id=descriptor_id, family=family)

    async def get_typed(self, descriptor_id, *, family, version=None):
        return _StubTyped(descriptor_id)

    async def query_history(self, descriptor_id, *, family):
        return [_make_row(descriptor_id=descriptor_id, family=family)]

    async def update(self, descriptor_id, new_descriptor, actor, *, actor_role="operator"):
        self._record("update", descriptor_id, actor)
        if "update" in self.behavior:
            v = self.behavior["update"]
            if isinstance(v, Exception):
                raise v
            return v
        return _make_row(descriptor_id=descriptor_id, version="v" + "0" * 63)

    async def retire(self, descriptor_id, actor, *, family, actor_role="operator", reason=None):
        self._record("retire", descriptor_id, family, reason)
        if "retire" in self.behavior:
            v = self.behavior["retire"]
            if isinstance(v, Exception):
                raise v
            return v
        return _make_row(descriptor_id=descriptor_id, family=family, state="retired")

    async def promote(self, descriptor_id, candidate_version, actor, *, family, actor_role="operator"):
        if "promote" in self.behavior:
            v = self.behavior["promote"]
            if isinstance(v, Exception):
                raise v
        return _make_row(descriptor_id=descriptor_id, family=family, version=candidate_version)

    async def rollback(self, descriptor_id, target_version, actor, *, family, actor_role="operator", reason=None):
        if "rollback" in self.behavior:
            v = self.behavior["rollback"]
            if isinstance(v, Exception):
                raise v
        return _make_row(descriptor_id=descriptor_id, family=family, version=target_version)

    async def list(self, predicate):
        if predicate.descriptor_id == "missing":
            return []
        fam = predicate.family or Family.TARGET
        return [_make_row(descriptor_id=predicate.descriptor_id or "stub", family=fam)]

    # Used internally by the router for DLQ / audit / vocabulary SQL access.
    @property
    def _pg(self):
        raise RuntimeError("unit test should not hit _pg")


class _StubStackRegistry:
    def __init__(self) -> None:
        # Optional per-kind row override consulted by `list(kind=...)`. When a
        # kind is absent from this map, `list` falls back to a single default
        # row (legacy behavior). Set `kind_rows[kind] = []` to model a
        # not-yet-configured component (drives the config-status first-run).
        self.kind_rows: dict[str, list] | None = None

    async def register(self, body, actor):
        return _StubStackRow()

    async def get(self, component_id, version=None):
        return _StubStackRow(component_id=component_id)

    async def get_by_kind(self, kind, name=None):
        return [_StubStackRow()]

    async def list(self, predicate=None, *, kind=None, state=None, include_history=False):
        if self.kind_rows is not None and kind is not None:
            return list(self.kind_rows.get(kind, []))
        return [_StubStackRow()]

    async def update(self, component_id, body, actor):
        return _StubStackRow(component_id=component_id)

    async def retire(self, component_id, actor):
        return _StubStackRow(component_id=component_id, state="retired")

    async def healthcheck(self, component_id):
        from legba.data.registry.health import HealthState, StackComponentHealth
        return StackComponentHealth(
            component_id=component_id,
            kind="postgres",
            state=HealthState.HEALTHY,
            checked_at=_FIXED_TS,
            detail="ok",
        )


class _StubVault:
    """In-memory vault. NEVER expose plaintext via API."""

    def __init__(self) -> None:
        self._db: dict[str, bytes] = {}

    async def store_secret(self, secret_id, plaintext, *, actor, notes=None):
        if isinstance(plaintext, str):
            plaintext = plaintext.encode()
        self._db[secret_id] = plaintext
        return 1

    async def verify_exists(self, secret_id):
        return secret_id in self._db

    async def delete_secret(self, secret_id):
        return 1 if self._db.pop(secret_id, None) is not None else 0


class _StubDLQ:
    async def resolve(self, dl_id, resolution, resolution_ref=None):
        pass


class _StubAuditLogger:
    pass


class _StubVocabularyCache:
    async def refresh(self):
        return 0


class _StubTyped:
    def __init__(self, descriptor_id: str):
        self.descriptor_id = descriptor_id

    def model_dump(self, **kwargs):
        return {"identity": {"id": self.descriptor_id, "schema_uri": "stub/1.0.0"}}


class _StubStackRow:
    def __init__(
        self, component_id: str = "stub_id", state: str = "draft",
        version: str = "1" * 64, schema_uri: str = "legba/stack/postgres/1.0.0",
        kind: str = "postgres",
    ):
        from legba.data.schemas.lifecycle import LifecycleState
        self.component_id = component_id
        self.version = version
        self.schema_uri = schema_uri
        self.kind = kind
        self.is_head = True
        self.state = LifecycleState(state)
        self.owner = "stub@local"
        self.name = "stub"
        self.body = {}
        self.created_at = _FIXED_TS


def _make_row(
    *,
    descriptor_id: str = "stub_id",
    family: Family = Family.TARGET,
    version: str = "a" * 64,
    state: str = "draft",
) -> DescriptorRow:
    base = dict(
        descriptor_id=descriptor_id,
        version=version,
        schema_uri="legba/target/2.0.0",
        is_head=True,
        state=state,
        owner="stub@local",
        name="Stub",
        body={"identity": {"id": descriptor_id}, "scope": {}},
        created_at=_FIXED_TS,
        family=family,
        inherits=[],
    )
    if family is Family.TARGET:
        return DescriptorRow(**base, abstraction_level="L1")
    return DescriptorRow(**base, kind="inline_target", type_signature={})


# ---------------------------------------------------------------------------
# App factory for unit tests
# ---------------------------------------------------------------------------


def _build_app(token: str | None = None) -> tuple[FastAPI, RegistryAPIDeps]:
    # Reset the env-gated token between tests.
    if token is None:
        os.environ.pop(API_TOKEN_ENV, None)
    else:
        os.environ[API_TOKEN_ENV] = token

    descriptor = _StubDescriptorRegistry()
    deps = RegistryAPIDeps(
        descriptor_registry=descriptor,  # type: ignore[arg-type]
        stack_registry=_StubStackRegistry(),  # type: ignore[arg-type]
        vault=_StubVault(),  # type: ignore[arg-type]
        dlq=_StubDLQ(),  # type: ignore[arg-type]
        audit_logger=_StubAuditLogger(),  # type: ignore[arg-type]
        vocabulary_cache=_StubVocabularyCache(),  # type: ignore[arg-type]
        nats_store=None,
        conversion_registry=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_router(deps), prefix="/api/v1/registry")
    return app, deps


# Sample descriptor body matching the L-110 schemas.
def _sample_target_body(descriptor_id: str) -> dict[str, Any]:
    return {
        "identity": {
            "id": descriptor_id,
            "name": "Brazil Energy",
            "schema_uri": "legba/target/2.0.0",
            "version": "0" * 16,
            "abstraction_level": "L1",
            "state": "draft",
            "owner": "lewis@local",
            "created": _FIXED_TS.isoformat(),
        },
        "scope": {
            # Source-first pivot: TargetScope is a discriminated union; the
            # geopolitical founding case carries domain="geo".
            "domain": "geo",
            "geo": ["BR"],
            "languages": ["pt-BR"],
            "entity_classes": ["organization", "country"],
            "relationship_types": ["LocatedIn"],
            "time_horizon_days": 90,
        },
        "sources": [],
    }


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


def test_dev_mode_accepts_missing_token():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    # GET that doesn't touch _pg: get by id returns the stub row.
    r = client.get("/api/v1/registry/descriptors/target/anything")
    assert r.status_code == 200


def test_enforced_mode_rejects_missing_token():
    app, _ = _build_app(token="secret-bearer-123")
    client = TestClient(app)
    r = client.get("/api/v1/registry/descriptors/target/anything")
    assert r.status_code == 401


def test_enforced_mode_rejects_wrong_token():
    app, _ = _build_app(token="secret-bearer-123")
    client = TestClient(app)
    r = client.get(
        "/api/v1/registry/descriptors/target/anything",
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 403


def test_enforced_mode_accepts_correct_token():
    app, _ = _build_app(token="secret-bearer-123")
    client = TestClient(app)
    r = client.get(
        "/api/v1/registry/descriptors/target/anything",
        headers={"Authorization": "Bearer secret-bearer-123"},
    )
    assert r.status_code == 200


def test_malformed_authorization_header_rejected():
    app, _ = _build_app(token="secret-bearer-123")
    client = TestClient(app)
    r = client.get(
        "/api/v1/registry/descriptors/target/anything",
        headers={"Authorization": "Basic abc=="},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Descriptor route shapes
# ---------------------------------------------------------------------------


def test_register_descriptor_returns_201_and_typed_row():
    app, deps = _build_app(token=None)
    client = TestClient(app)
    body = _sample_target_body("t_unit_1")
    r = client.post("/api/v1/registry/descriptors/target", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["descriptor_id"] == "t_unit_1"
    assert out["family"] == "target"
    assert out["abstraction_level"] == "L1"


def test_register_descriptor_400_on_unknown_family():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.post(
        "/api/v1/registry/descriptors/bogus",
        json=_sample_target_body("x"),
    )
    assert r.status_code == 400


def test_register_descriptor_422_on_invalid_payload():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.post("/api/v1/registry/descriptors/target", json={"identity": {}})
    assert r.status_code == 422


def test_register_descriptor_422_on_dlq_validation_error():
    app, deps = _build_app(token=None)
    deps.descriptor_registry.behavior["register"] = DescriptorValidationError(
        "vocabulary issue",
        attempted_payload={},
        declared_schema_uri="legba/target/2.0.0",
        validation_error={"kind": "vocabulary"},
        dead_letter_id="00000000-0000-0000-0000-000000000111",
    )
    client = TestClient(app)
    r = client.post(
        "/api/v1/registry/descriptors/target",
        json=_sample_target_body("t_dlq_1"),
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["dead_letter_id"] == "00000000-0000-0000-0000-000000000111"


def test_register_descriptor_409_on_version_conflict():
    app, deps = _build_app(token=None)
    deps.descriptor_registry.behavior["register"] = VersionConflict(
        "already registered"
    )
    client = TestClient(app)
    r = client.post(
        "/api/v1/registry/descriptors/target",
        json=_sample_target_body("t_409"),
    )
    assert r.status_code == 409


def test_get_descriptor_404_when_missing():
    app, deps = _build_app(token=None)
    deps.descriptor_registry.behavior["get"] = DescriptorNotFound(
        "target", "missing_id",
    )
    client = TestClient(app)
    r = client.get("/api/v1/registry/descriptors/target/missing_id")
    assert r.status_code == 404


def test_retire_descriptor_returns_retired_state():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.post(
        "/api/v1/registry/descriptors/target/t_unit_2/retire",
        json={"reason": "done"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "retired"


def test_retire_descriptor_409_on_illegal_transition():
    app, deps = _build_app(token=None)
    deps.descriptor_registry.behavior["retire"] = IllegalLifecycleTransition(
        "retired", "retired",
    )
    client = TestClient(app)
    r = client.post(
        "/api/v1/registry/descriptors/target/t_unit_3/retire",
        json={"reason": "x"},
    )
    assert r.status_code == 409


def test_promote_descriptor_round_trip():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.post(
        "/api/v1/registry/descriptors/target/t_unit_4/promote",
        json={"candidate_version": "b" * 64},
    )
    assert r.status_code == 200
    assert r.json()["version"] == "b" * 64


def test_rollback_descriptor_round_trip():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.post(
        "/api/v1/registry/descriptors/target/t_unit_5/rollback",
        json={"target_version": "c" * 64, "reason": "regression"},
    )
    assert r.status_code == 200
    assert r.json()["version"] == "c" * 64


def test_history_returns_list():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.get("/api/v1/registry/descriptors/target/t_unit_6/history")
    assert r.status_code == 200
    assert isinstance(r.json(), list) and len(r.json()) == 1


def test_get_typed_returns_dump():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.get("/api/v1/registry/descriptors/target/t_typed_unit/typed")
    assert r.status_code == 200
    assert r.json()["identity"]["id"] == "t_typed_unit"


# ---------------------------------------------------------------------------
# Stack route shapes
# ---------------------------------------------------------------------------


def test_stack_register_route():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.post("/api/v1/registry/stack", json={"schema_uri": "legba/stack/postgres/1.0.0"})
    assert r.status_code == 201
    assert r.json()["kind"] == "postgres"


def test_stack_get_route():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.get("/api/v1/registry/stack/pg.primary")
    assert r.status_code == 200
    assert r.json()["component_id"] == "pg.primary"


def test_stack_list_with_state_filter():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.get("/api/v1/registry/stack?state=draft")
    assert r.status_code == 200


def test_stack_list_400_on_bogus_state():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.get("/api/v1/registry/stack?state=NOPE")
    assert r.status_code == 400


def test_stack_healthcheck_route():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.post("/api/v1/registry/stack/pg.primary/healthcheck")
    assert r.status_code == 200
    assert r.json()["state"] == "healthy"


# ---------------------------------------------------------------------------
# First-run config status (config-honesty stream).
# ---------------------------------------------------------------------------


def _config_status(client: TestClient) -> dict[str, Any]:
    r = client.get("/api/v1/registry/config/status")
    assert r.status_code == 200, r.text
    return r.json()


def test_config_status_first_run_when_all_required_absent():
    app, deps = _build_app(token=None)
    deps.stack_registry.kind_rows = {
        "llm_provider": [],
        "embedding": [],
        "nlp_service": [],
    }
    out = _config_status(TestClient(app))
    assert out["first_run"] is True
    assert out["all_configured"] is False
    assert out["all_active"] is False
    by_kind = {c["kind"]: c for c in out["required"]}
    # The three required model-serving kinds are reported.
    assert set(by_kind) == {"llm_provider", "embedding", "nlp_service"}
    for c in by_kind.values():
        assert c["configured"] is False
        assert c["active"] is False
        assert c["component_id"] is None


def test_config_status_all_active_is_not_first_run():
    app, deps = _build_app(token=None)
    deps.stack_registry.kind_rows = {
        "llm_provider": [
            _StubStackRow(component_id="llm.primary", state="active",
                          kind="llm_provider",
                          schema_uri="legba/stack/llm_provider/1.0.0"),
        ],
        "embedding": [
            _StubStackRow(component_id="embed.primary", state="active",
                          kind="embedding",
                          schema_uri="legba/stack/embedding/1.0.0"),
        ],
        "nlp_service": [
            _StubStackRow(component_id="nlp.local", state="active",
                          kind="nlp_service",
                          schema_uri="legba/stack/nlp_service/1.0.0"),
        ],
    }
    out = _config_status(TestClient(app))
    assert out["first_run"] is False
    assert out["all_configured"] is True
    assert out["all_active"] is True
    by_kind = {c["kind"]: c for c in out["required"]}
    assert by_kind["llm_provider"]["component_id"] == "llm.primary"
    assert by_kind["llm_provider"]["active"] is True


def test_config_status_configured_but_draft_is_not_active():
    app, deps = _build_app(token=None)
    deps.stack_registry.kind_rows = {
        "llm_provider": [
            _StubStackRow(component_id="llm.primary", state="draft",
                          kind="llm_provider",
                          schema_uri="legba/stack/llm_provider/1.0.0"),
        ],
        "embedding": [
            _StubStackRow(component_id="embed.primary", state="active",
                          kind="embedding",
                          schema_uri="legba/stack/embedding/1.0.0"),
        ],
        "nlp_service": [
            _StubStackRow(component_id="nlp.local", state="active",
                          kind="nlp_service",
                          schema_uri="legba/stack/nlp_service/1.0.0"),
        ],
    }
    out = _config_status(TestClient(app))
    # All three have a row → configured → NOT first-run.
    assert out["first_run"] is False
    assert out["all_configured"] is True
    # But the draft llm provider means the stack isn't fully active.
    assert out["all_active"] is False
    by_kind = {c["kind"]: c for c in out["required"]}
    assert by_kind["llm_provider"]["configured"] is True
    assert by_kind["llm_provider"]["active"] is False
    assert by_kind["llm_provider"]["state"] == "draft"


def test_config_status_retired_row_counts_as_unconfigured():
    app, deps = _build_app(token=None)
    deps.stack_registry.kind_rows = {
        # Only a RETIRED head for the LLM → treated as not configured.
        "llm_provider": [
            _StubStackRow(component_id="llm.old", state="retired",
                          kind="llm_provider",
                          schema_uri="legba/stack/llm_provider/1.0.0"),
        ],
        "embedding": [
            _StubStackRow(component_id="embed.primary", state="active",
                          kind="embedding",
                          schema_uri="legba/stack/embedding/1.0.0"),
        ],
        "nlp_service": [
            _StubStackRow(component_id="nlp.local", state="active",
                          kind="nlp_service",
                          schema_uri="legba/stack/nlp_service/1.0.0"),
        ],
    }
    out = _config_status(TestClient(app))
    assert out["first_run"] is True
    by_kind = {c["kind"]: c for c in out["required"]}
    assert by_kind["llm_provider"]["configured"] is False
    assert by_kind["llm_provider"]["component_id"] is None


def test_config_status_secrets_safe_no_credentials_in_response():
    app, deps = _build_app(token=None)
    deps.stack_registry.kind_rows = {
        "llm_provider": [
            _StubStackRow(component_id="llm.primary", state="active",
                          kind="llm_provider",
                          schema_uri="legba/stack/llm_provider/1.0.0"),
        ],
        "embedding": [],
        "nlp_service": [],
    }
    r = TestClient(app).get("/api/v1/registry/config/status")
    assert r.status_code == 200
    # The status response is identity/lifecycle only — no credential value and
    # no raw stack-component body/config blob. NB: the bare substrings "config"
    # and "secret" are expected (the legit fields are `configured` /
    # `all_configured`), so we forbid credential tokens + the QUOTED raw-blob
    # keys rather than those substrings.
    for leak in ("api_key", "password", "plaintext", "api_endpoint", '"body"', '"config":'):
        assert leak not in r.text, f"config/status leaked {leak!r}: {r.text}"


def test_config_status_requires_auth_when_token_configured():
    app, _ = _build_app(token="secret-bearer-xyz")
    r = TestClient(app).get("/api/v1/registry/config/status")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Vault: NEVER returns plaintext.
# ---------------------------------------------------------------------------


def test_vault_register_does_not_echo_plaintext():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    r = client.post(
        "/api/v1/registry/vault/secrets",
        json={
            "secret_id": "llm.anthropic.api_key",
            "plaintext": "sk-very-secret-never-leak",
            "notes": "Anthropic key",
        },
    )
    assert r.status_code == 201
    out = r.json()
    assert out["secret_id"] == "llm.anthropic.api_key"
    assert "version" in out
    # NEVER echo plaintext back.
    assert "sk-very-secret-never-leak" not in r.text
    assert "plaintext" not in out


def test_vault_exists_returns_bool_only():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    client.post(
        "/api/v1/registry/vault/secrets",
        json={"secret_id": "s1", "plaintext": "topsecret"},
    )
    r = client.get("/api/v1/registry/vault/secrets/s1/exists")
    assert r.status_code == 200
    body = r.json()
    assert body == {"secret_id": "s1", "exists": True}
    # GET on a missing secret returns exists=False (200), not 404.
    r2 = client.get("/api/v1/registry/vault/secrets/no_such/exists")
    assert r2.status_code == 200
    assert r2.json()["exists"] is False


def test_vault_delete_returns_removed_count():
    app, _ = _build_app(token=None)
    client = TestClient(app)
    client.post(
        "/api/v1/registry/vault/secrets",
        json={"secret_id": "s2", "plaintext": "another"},
    )
    r = client.delete("/api/v1/registry/vault/secrets/s2")
    assert r.status_code == 200
    assert r.json()["removed_rows"] == 1


# ---------------------------------------------------------------------------
# OpenAPI exposed
# ---------------------------------------------------------------------------


def test_router_endpoints_visible_in_app_openapi():
    """The app-mounted version of the router exposes the openapi schema."""
    from legba.data.registry.server import API_PREFIX  # noqa
    app, _ = _build_app(token=None)
    # FastAPI's default openapi.json is at /openapi.json on this unit-test app.
    r = TestClient(app).get("/openapi.json")
    assert r.status_code == 200
    paths = set(r.json()["paths"].keys())
    assert "/api/v1/registry/descriptors/{family}" in paths
    assert "/api/v1/registry/stack" in paths
    assert "/api/v1/registry/config/status" in paths
    assert "/api/v1/registry/vault/secrets" in paths
    assert "/api/v1/registry/dead_letter" in paths
    assert "/api/v1/registry/audit" in paths
    assert "/api/v1/registry/vocabulary/{family}" in paths
    assert "/api/v1/registry/conversions" in paths
