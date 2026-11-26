# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retired stack names stay readable (``legba.data.schemas.legacy_name_digests``).

A registry row registered under a retired NLP provider value must still
validate, and a retired component id must keep its grader family, while the
tree never spells the retired names. The table holds digests only, so these
tests prove the mechanism on a synthetic retired name injected into the
table, plus the table's own shape.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from legba.data.schemas import legacy_name_digests as L
from legba.data.schemas.stack import NLPServiceConfig
from legba.runtime.external_grader_route import (
    GRADER_FAMILY_BY_COMPONENT,
    grader_family_for_component,
)

_RETIRED_PROVIDER = "retired_vendor_gateway"
_RETIRED_COMPONENT = "llm.verify.retired_vendor_judge.openai_compat"
_NEW_JUDGE = "llm.verify.gateway_judge.openai_compat"


@pytest.fixture()
def injected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(L.LEGACY_NLP_PROVIDER_DIGESTS, L.name_digest(_RETIRED_PROVIDER),
                        "bearer_gateway")
    monkeypatch.setitem(L.LEGACY_COMPONENT_ID_DIGESTS, L.name_digest(_RETIRED_COMPONENT),
                        _NEW_JUDGE)


def _gateway_config(provider: str) -> dict[str, Any]:
    t = lambda s: {"factory_kind": "text", "raw": s}  # noqa: E731
    return {
        "endpoint": t("https://gateway.example.invalid/v1"),
        "api_key": {"factory_kind": "secret", "raw": "llm.gateway.api_key"},
        "provider": t(provider),
        "translate_model": t("fast"),
    }


def test_the_table_holds_digests_of_retired_names_only():
    current_ids = set(GRADER_FAMILY_BY_COMPONENT) | {"nlp.gateway.bearer"}
    assert len(L.LEGACY_COMPONENT_ID_DIGESTS) == 6
    for digest, new in L.LEGACY_COMPONENT_ID_DIGESTS.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert new in current_ids, new
        assert digest != L.name_digest(new), "a name must not alias itself"
    assert set(L.LEGACY_NLP_PROVIDER_DIGESTS.values()) == {"bearer_gateway"}
    for digest in L.LEGACY_NLP_PROVIDER_DIGESTS:
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert digest not in (L.name_digest("bearer_gateway"), L.name_digest("legba_models"))


def test_current_and_unknown_names_pass_through_unchanged():
    for name in ("bearer_gateway", "legba_models", "spacy"):
        assert L.canonical_nlp_provider(name) == name
    for cid in (_NEW_JUDGE, "llm.primary.openai_compat", "llm.unknown.x"):
        assert L.canonical_component_id(cid) == cid


def test_a_row_under_a_retired_provider_value_still_validates(injected):
    cfg = NLPServiceConfig.model_validate(_gateway_config(_RETIRED_PROVIDER))
    # read-side only: the stored value is kept as written (its content hash holds)
    assert cfg.provider is not None and cfg.provider.raw == _RETIRED_PROVIDER
    # and it is held to the gateway's rules (Bearer only)
    bad = _gateway_config(_RETIRED_PROVIDER)
    bad["api_user"] = {"factory_kind": "secret", "raw": "x.user"}
    bad["api_pass"] = {"factory_kind": "secret", "raw": "x.pass"}
    with pytest.raises(ValueError, match="Bearer-only"):
        NLPServiceConfig.model_validate(bad)


def test_an_unknown_provider_is_still_refused():
    with pytest.raises(ValueError, match="must be"):
        NLPServiceConfig.model_validate(_gateway_config(_RETIRED_PROVIDER))


def test_a_retired_component_id_keeps_its_grader_family(injected):
    assert grader_family_for_component(_RETIRED_COMPONENT) == "google_gemma"
    assert grader_family_for_component(_NEW_JUDGE) == "google_gemma"


def test_without_the_alias_a_retired_id_has_no_family():
    assert grader_family_for_component(_RETIRED_COMPONENT) == ""


async def test_the_nlp_factory_builds_the_gateway_client_for_a_retired_provider(injected):
    from legba.data.stack.nlp_service import GatewayNlpClient
    from legba.runtime.nlp_client_factory import build_nlp_client_from_stack_component
    from legba.runtime.registry_client import RegistryHTTPClient

    cid = "nlp.gateway.bearer"
    body = {"id": cid, "schema_uri": "legba/stack/nlp_service/1.0.0",
            "config": _gateway_config(_RETIRED_PROVIDER)}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": cid, "version": "0" * 16, "body": body})

    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                              base_url="http://registry.test")
    reg = RegistryHTTPClient(base_url="http://registry.test", token=None, client=inner)

    async def secrets(secret_id: str) -> bytes:
        return b"tok-test"

    client = await build_nlp_client_from_stack_component(
        cid, registry_client=reg, secrets_resolve=secrets)
    assert isinstance(client, GatewayNlpClient)
    await inner.aclose()


async def test_the_health_probe_treats_a_retired_provider_as_the_gateway(injected, monkeypatch):
    from legba.data.registry.health import HealthState, NLPServiceChecker, ResolvedConfig

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"object": "list", "data": []})

    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    class _Resolver:
        async def resolve(self, secret_id: str) -> bytes:
            return b"tok-test"

    cfg = NLPServiceConfig.model_validate(_gateway_config(_RETIRED_PROVIDER))
    result = await NLPServiceChecker().check(
        "nlp.gateway.bearer", ResolvedConfig(config=cfg, resolver=_Resolver()))
    assert result.state == HealthState.HEALTHY
    assert str(seen[0].url).endswith("/v1/models")
