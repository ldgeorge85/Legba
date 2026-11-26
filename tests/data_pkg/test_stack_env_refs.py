# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stack-component fields written as ``${env:NAME}``.

The model-gateway components name their endpoint and model names by
environment variable; the registry stores the reference, and the runtime and
registry resolve it when they build the component. A missing variable fails
loudly; an inline value keeps working unchanged.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from legba.data.schemas.env_refs import (
    EnvRefError,
    env_ref_name,
    resolve_config_env_refs,
    resolve_env_value,
    resolve_model_env_refs,
)
from legba.data.schemas.stack import LLMProvider, LLMProviderConfig, NLPService
from tests._gateway_env import GATEWAY_ENV, set_gateway_env

_REPO = Path(__file__).resolve().parents[2]
_DESCRIPTORS = _REPO / "descriptors"

#: component file -> {field: variable}
_GATEWAY_FILES: dict[str, dict[str, str]] = {
    "stack_component_llm_decide_gateway_typed_choice.yaml": {
        "api_endpoint": "LEGBA_GATEWAY_API_ENDPOINT", "model_name": "LEGBA_GATEWAY_DECIDE_MODEL"},
    "stack_component_llm_grader_gateway_third_family.yaml": {
        "api_endpoint": "LEGBA_GATEWAY_API_ENDPOINT", "model_name": "LEGBA_GATEWAY_GRADER_MODEL",
        "max_output_tokens": "LEGBA_GATEWAY_GRADER_MAX_OUTPUT_TOKENS",
        "context_window_tokens": "LEGBA_GATEWAY_CONTEXT_WINDOW_TOKENS"},
    "stack_component_llm_judge_gateway_rung2_think.yaml": {
        "api_endpoint": "LEGBA_GATEWAY_API_ENDPOINT", "model_name": "LEGBA_GATEWAY_JUDGE_THINK_MODEL",
        "max_output_tokens": "LEGBA_GATEWAY_JUDGE_THINK_MAX_OUTPUT_TOKENS",
        "context_window_tokens": "LEGBA_GATEWAY_CONTEXT_WINDOW_TOKENS"},
    "stack_component_llm_verify_gateway_gold_drafter.yaml": {
        "api_endpoint": "LEGBA_GATEWAY_API_ENDPOINT", "model_name": "LEGBA_GATEWAY_GOLD_DRAFTER_MODEL",
        "max_output_tokens": "LEGBA_GATEWAY_GOLD_DRAFTER_MAX_OUTPUT_TOKENS",
        "context_window_tokens": "LEGBA_GATEWAY_CONTEXT_WINDOW_TOKENS"},
    "stack_component_llm_verify_gateway_judge.yaml": {
        "api_endpoint": "LEGBA_GATEWAY_API_ENDPOINT", "model_name": "LEGBA_GATEWAY_JUDGE_MODEL",
        "max_output_tokens": "LEGBA_GATEWAY_JUDGE_MAX_OUTPUT_TOKENS",
        "context_window_tokens": "LEGBA_GATEWAY_CONTEXT_WINDOW_TOKENS"},
    "stack_component_nlp_gateway_bearer.yaml": {
        "endpoint": "LEGBA_GATEWAY_API_ENDPOINT", "translate_model": "LEGBA_GATEWAY_TRANSLATE_MODEL"},
}


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((_DESCRIPTORS / name).read_text())


def _typed(body: dict[str, Any]) -> Any:
    return (NLPService if "nlp_service" in body["schema_uri"] else LLMProvider).model_validate(body)


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def test_a_reference_resolves_from_the_environment():
    env = {"LEGBA_X_MODEL": "  some-model  "}
    assert resolve_env_value("${env:LEGBA_X_MODEL}", component_id="c", field="f", environ=env) == "some-model"


def test_an_inline_value_passes_unchanged():
    for value in ("https://llm.example.internal/v1", "model-a", "", None, 7, "$HOME", "{env:X}"):
        assert resolve_env_value(value, component_id="c", field="f", environ={}) == value


@pytest.mark.parametrize("env", [{}, {"LEGBA_X_MODEL": ""}, {"LEGBA_X_MODEL": "   "}])
def test_a_missing_or_empty_variable_fails_naming_it_and_the_component(env):
    with pytest.raises(EnvRefError) as exc:
        resolve_env_value("${env:LEGBA_X_MODEL}", component_id="llm.x.openai_compat",
                          field="model_name", environ=env)
    msg = str(exc.value)
    assert "LEGBA_X_MODEL" in msg and "llm.x.openai_compat" in msg and "model_name" in msg


@pytest.mark.parametrize("bad", ["${env:lower}", "${env:}", "${env:X", "${env:X}/v1", "${env: X}"])
def test_a_malformed_reference_is_refused_not_used_as_a_literal(bad):
    with pytest.raises(EnvRefError, match="malformed"):
        env_ref_name(bad)


def test_the_config_mapping_resolves_every_field_and_leaves_inline_ones():
    cfg = {
        "api_endpoint": {"factory_kind": "text", "raw": "${env:LEGBA_GW}"},
        "model_name": {"factory_kind": "text", "raw": "inline-model"},
        "api_key": {"factory_kind": "secret", "raw": "llm.gateway.api_key"},
        "max_tokens": {"factory_kind": "number", "raw": 1},
    }
    out = resolve_config_env_refs(cfg, component_id="c", environ={"LEGBA_GW": "https://gw.test/v1"})
    assert out["api_endpoint"] == {"factory_kind": "text", "raw": "https://gw.test/v1"}
    assert out["model_name"] is cfg["model_name"]
    assert out["api_key"] is cfg["api_key"]
    assert cfg["api_endpoint"]["raw"] == "${env:LEGBA_GW}", "the input is not mutated"


def test_the_typed_config_twin_resolves_and_keeps_the_rest():
    body = _load("stack_component_llm_verify_gateway_judge.yaml")
    cfg = LLMProvider.model_validate(body).config
    out = resolve_model_env_refs(cfg, component_id="j", environ=GATEWAY_ENV)
    assert isinstance(out, LLMProviderConfig)
    assert out.api_endpoint.raw == GATEWAY_ENV["LEGBA_GATEWAY_API_ENDPOINT"]
    assert out.model_name.raw == GATEWAY_ENV["LEGBA_GATEWAY_JUDGE_MODEL"]
    assert out.max_concurrent.raw == cfg.max_concurrent.raw
    assert cfg.model_name.raw == "${env:LEGBA_GATEWAY_JUDGE_MODEL}", "the input is not mutated"
    with pytest.raises(EnvRefError, match="LEGBA_GATEWAY_JUDGE_MODEL"):
        resolve_model_env_refs(cfg, component_id="j", environ={"LEGBA_GATEWAY_API_ENDPOINT": "https://x/v1"})


def test_the_resolver_module_imports_only_the_standard_library():
    """The registry service imports it on its slim image."""
    import ast

    import legba.data.schemas.env_refs as mod

    tree = ast.parse(Path(mod.__file__).read_text())
    imported = {
        (n.module if isinstance(n, ast.ImportFrom) else a.name).split(".")[0]
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert imported <= {"__future__", "os", "re", "typing"}, imported


# ---------------------------------------------------------------------------
# The six gateway definitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_GATEWAY_FILES))
def test_each_gateway_definition_names_its_variables_and_resolves(name, monkeypatch):
    body = _load(name)
    typed = _typed(body)
    for field, var in _GATEWAY_FILES[name].items():
        assert body["config"][field]["raw"] == "${env:" + var + "}", (name, field)
    values = set_gateway_env(monkeypatch)
    resolved = resolve_model_env_refs(typed.config, component_id=typed.id)
    for field, var in _GATEWAY_FILES[name].items():
        assert getattr(resolved, field).raw == values[var], (name, field)
    # The resolved config still validates as its kind.
    type(typed.config).model_validate(resolved.model_dump(), strict=False)


def test_every_variable_the_definitions_name_is_in_env_example():
    example = (_REPO / ".env.example").read_text()
    names = {v for fields in _GATEWAY_FILES.values() for v in fields.values()}
    assert names == set(GATEWAY_ENV)
    for var in names:
        assert re.search(rf"^{var}=\S", example, re.M), var


def test_the_stored_hash_does_not_depend_on_the_instance(monkeypatch):
    """Resolution happens at use time: the body the registry hashes is the
    reference, whatever this process's environment says."""
    from legba.data.registry.stack import _compute_version

    body = _load("stack_component_llm_verify_gateway_judge.yaml")
    set_gateway_env(monkeypatch)
    first = _compute_version(LLMProvider.model_validate(body))
    set_gateway_env(monkeypatch, LEGBA_GATEWAY_API_ENDPOINT="https://other.example.test/v1",
                    LEGBA_GATEWAY_JUDGE_MODEL="other-model")
    assert _compute_version(LLMProvider.model_validate(body)) == first


#: SHA-256 of the registrable domain of one instance's model gateway. Kept as a
#: digest so the guard does not itself put the name back in the tree.
_BLOCKED_DOMAIN_DIGESTS = frozenset({
    "3e30f8698bd67284c4e6369eb1c7b708ce48ae203cd5a05dbe939e60e36c21f8",
})
_HOST_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)


def _domain_suffix_digests(host: str) -> set[str]:
    labels = host.lower().split(".")
    return {hashlib.sha256(".".join(labels[i:]).encode()).hexdigest() for i in range(len(labels) - 1)}


def test_no_tracked_descriptor_or_profile_names_the_instance_gateway_host():
    try:
        files = subprocess.run(
            ["git", "ls-files", "descriptors", "profiles"], cwd=_REPO,
            capture_output=True, text=True, check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        files = []
    if not files:  # no git checkout (the test container): every file on disk
        files = [str(p.relative_to(_REPO)) for d in ("descriptors", "profiles")
                 for p in (_REPO / d).rglob("*") if p.is_file()]
    assert files, "found no descriptors"
    hits = []
    for rel in files:
        try:
            text = (_REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for host in set(_HOST_RE.findall(text)):
            if _domain_suffix_digests(host) & _BLOCKED_DOMAIN_DIGESTS:
                hits.append(rel)
    assert not hits, f"an instance's gateway address is back in tracked files: {sorted(set(hits))}"


def test_no_gateway_definition_writes_an_address_inline():
    for path in sorted(_DESCRIPTORS.glob("stack_component_*gateway*.yaml")):
        body = yaml.safe_load(path.read_text())
        for field in ("api_endpoint", "endpoint"):
            entry = body["config"].get(field)
            if entry is not None:
                assert env_ref_name(entry["raw"]), f"{path.name}: {field} is written inline"


# ---------------------------------------------------------------------------
# The use-time paths
# ---------------------------------------------------------------------------


def _registry(component_id: str, body: dict[str, Any]):
    from legba.runtime.registry_client import RegistryHTTPClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": component_id, "version": "0" * 16, "body": body})

    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://registry.test")
    return RegistryHTTPClient(base_url="http://registry.test", token=None, client=inner)


async def _secrets(secret_id: str) -> bytes:
    return b"tok-test"


async def test_the_llm_build_resolves_the_endpoint_and_model(monkeypatch):
    from legba.runtime.analyst_deps_builder import build_llm_handler_from_stack_component

    body = _load("stack_component_llm_judge_gateway_rung2_think.yaml")
    values = set_gateway_env(monkeypatch)
    handler = await build_llm_handler_from_stack_component(
        body["id"], registry_client=_registry(body["id"], body), secrets_resolve=_secrets,
    )
    assert handler._cfg.api_endpoint.raw == values["LEGBA_GATEWAY_API_ENDPOINT"]  # noqa: SLF001
    assert handler._cfg.model_name.raw == values["LEGBA_GATEWAY_JUDGE_THINK_MODEL"]  # noqa: SLF001
    # The alias's declared maximum and the window, read from their variables.
    assert handler.model_max_output_tokens() == int(values["LEGBA_GATEWAY_JUDGE_THINK_MAX_OUTPUT_TOKENS"])
    assert handler.context_window_tokens() == int(values["LEGBA_GATEWAY_CONTEXT_WINDOW_TOKENS"])


async def test_the_llm_build_fails_loudly_on_an_unset_variable(monkeypatch):
    from legba.runtime.analyst_deps_builder import (
        AnalystDepsBuildError,
        build_llm_handler_from_stack_component,
    )

    body = _load("stack_component_llm_grader_gateway_third_family.yaml")
    set_gateway_env(monkeypatch)
    monkeypatch.delenv("LEGBA_GATEWAY_GRADER_MODEL")
    with pytest.raises(AnalystDepsBuildError) as exc:
        await build_llm_handler_from_stack_component(
            body["id"], registry_client=_registry(body["id"], body), secrets_resolve=_secrets,
        )
    assert "LEGBA_GATEWAY_GRADER_MODEL" in str(exc.value) and body["id"] in str(exc.value)


async def test_an_inline_llm_component_builds_unchanged_without_any_variable(monkeypatch):
    from legba.runtime.analyst_deps_builder import build_llm_handler_from_stack_component

    for name in GATEWAY_ENV:
        monkeypatch.delenv(name, raising=False)
    body = _load("stack_component_llm_verify_gateway_judge.yaml")
    body["config"]["api_endpoint"]["raw"] = "https://llm.example.internal/v1"
    body["config"]["model_name"]["raw"] = "inline-model"
    # The model's limits written inline as plain numbers.
    body["config"]["max_output_tokens"] = {"factory_kind": "number", "raw": 32768}
    body["config"]["context_window_tokens"] = {"factory_kind": "number", "raw": 262144}
    handler = await build_llm_handler_from_stack_component(
        body["id"], registry_client=_registry(body["id"], body), secrets_resolve=_secrets,
    )
    assert handler._cfg.api_endpoint.raw == "https://llm.example.internal/v1"  # noqa: SLF001
    assert handler._cfg.model_name.raw == "inline-model"  # noqa: SLF001
    assert handler.model_max_output_tokens() == 32768
    assert handler.context_window_tokens() == 262144


async def test_the_nlp_build_fails_loudly_on_an_unset_variable(monkeypatch):
    from legba.runtime.nlp_client_factory import (
        NlpClientFactoryError,
        build_nlp_client_from_stack_component,
    )

    body = _load("stack_component_nlp_gateway_bearer.yaml")
    set_gateway_env(monkeypatch, LEGBA_GATEWAY_TRANSLATE_MODEL="")
    with pytest.raises(NlpClientFactoryError, match="LEGBA_GATEWAY_TRANSLATE_MODEL"):
        await build_nlp_client_from_stack_component(
            body["id"], registry_client=_registry(body["id"], body), secrets_resolve=_secrets,
        )


async def test_the_registry_probe_names_an_unset_variable(monkeypatch):
    from legba.data.registry.health import HealthState, StackHealthDispatcher

    class _Resolver:
        async def resolve(self, secret_id: str) -> bytes:
            return b"tok-test"

    for name in GATEWAY_ENV:
        monkeypatch.delenv(name, raising=False)
    body = _load("stack_component_llm_verify_gateway_judge.yaml")
    cfg = LLMProvider.model_validate(body).config
    result = await StackHealthDispatcher().check_component(body["id"], "llm_provider", cfg, _Resolver())
    assert result.state == HealthState.UNHEALTHY
    assert "LEGBA_GATEWAY_API_ENDPOINT" in result.detail


def test_the_route_watch_reads_the_host_through_the_variable(monkeypatch):
    from legba.runtime.llm_route_liveness import endpoint_host

    cfg = _load("stack_component_llm_verify_gateway_judge.yaml")["config"]
    set_gateway_env(monkeypatch)
    assert endpoint_host(cfg, "j") == "gateway.example.test"
    assert endpoint_host({"api_endpoint": {"raw": "https://LLM.example.internal"}}) == "llm.example.internal"
    monkeypatch.delenv("LEGBA_GATEWAY_API_ENDPOINT")
    with pytest.raises(EnvRefError, match="LEGBA_GATEWAY_API_ENDPOINT"):
        endpoint_host(cfg, "j")
