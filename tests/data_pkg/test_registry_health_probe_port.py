# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F97 — the registry's TCP reachability probes dial the port the URL means.

THE DEFECT. ``EmbeddingChecker`` split its endpoint with a scheme-blind helper
that fell back to port 80 whenever the URL carried no explicit port, so the
live embedding component on ``https://api.innoai.net/v1`` reported
``tcp api.innoai.net:80 reachable=True`` (2026-10-23) — a port the https
client never dials, and a green light that proved nothing about 443. The LLM
provider probe had the mirror bug: ``http://host`` without a port probed 443.

THE RULE. An explicit port wins; a written ``https`` scheme means 443 and
``http`` means 80; only a scheme-less ``host`` falls back to the checker's
default. The detail string format is unchanged. No socket is ever opened:
``_tcp_reachable`` is replaced by a recorder.
"""
from __future__ import annotations

import pytest

from legba.data.registry import health as health_mod
from legba.data.registry.health import (
    EmbeddingChecker,
    LLMProviderChecker,
    ResolvedConfig,
    _split_probe_endpoint,
)
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.schemas.stack import EmbeddingServiceConfig


# ---------------------------------------------------------------------------
# The helper, one case per scheme
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint,default_port,expected",
    [
        # https with no port -> 443, whatever the checker's default
        ("https://api.innoai.net/v1", 80, ("api.innoai.net", 443)),
        ("https://api.innoai.net", 8080, ("api.innoai.net", 443)),
        ("HTTPS://api.innoai.net/v1", 80, ("api.innoai.net", 443)),
        # http with no port -> 80, whatever the checker's default
        ("http://tei/embed", 443, ("tei", 80)),
        ("http://tei", 8080, ("tei", 80)),
        # an explicit port always wins over the scheme
        ("https://api.example.com:8443/v1", 80, ("api.example.com", 8443)),
        ("http://legba-embedder:8080/embed", 443, ("legba-embedder", 8080)),
        # scheme-less: the checker's default (unchanged behaviour)
        ("tei", 80, ("tei", 80)),
        ("tei:9000", 80, ("tei", 9000)),
        # an unknown scheme keeps the checker's default
        ("grpc://models", 50051, ("models", 50051)),
        # nothing to dial
        ("", 80, (None, 80)),
    ],
)
def test_probe_port_follows_the_scheme(endpoint, default_port, expected):
    assert _split_probe_endpoint(endpoint, default_port) == expected


def test_the_scheme_blind_split_is_unchanged_for_its_other_callers():
    """``VectorStoreChecker`` hands its port to the qdrant client, not to a TCP
    probe — a portless ``http://qdrant`` must still mean qdrant's 6333."""
    assert health_mod._split_endpoint("http://qdrant", 6333) == ("qdrant", 6333)


# ---------------------------------------------------------------------------
# Through the checkers: the port that is actually dialled
# ---------------------------------------------------------------------------


@pytest.fixture
def dialled(monkeypatch):
    calls: list[tuple[str, int]] = []

    def _fake(host, port, **kw):
        calls.append((host, port))
        return True

    monkeypatch.setattr(health_mod, "_tcp_reachable", _fake)
    return calls


class _NoSecrets:
    async def verify_exists(self, secret_id: str) -> bool:
        return True

    async def resolve(self, secret_id: str) -> bytes:
        return b"x"


@pytest.mark.parametrize(
    "endpoint,port",
    [
        ("https://api.innoai.net/v1", 443),
        ("http://tei/embed", 80),
        ("https://api.innoai.net:8443/v1", 8443),
        ("tei", 80),
    ],
)
async def test_embedding_probe_dials_the_scheme_port(dialled, endpoint, port):
    cfg = EmbeddingServiceConfig(endpoint=Property.Text.of(endpoint))
    got = await EmbeddingChecker().check(
        "embedding.test", ResolvedConfig(config=cfg, resolver=_NoSecrets())
    )
    host = dialled[0][0]
    assert dialled == [(host, port)]
    # the detail string format is exactly the old one, with the right port
    assert got.detail == f"tcp {host}:{port} reachable=True dim=1024"


@pytest.mark.parametrize(
    "endpoint,port",
    [
        ("https://api.innoai.net/v1", 443),
        ("http://slm.internal/v1", 80),
        ("http://localhost:8000/v1", 8000),
        ("api.example.com", 443),
    ],
)
async def test_llm_provider_probe_dials_the_scheme_port(dialled, endpoint, port):
    cfg = LLMProviderConfig(
        api_endpoint=Property.Text.of(endpoint),
        api_key=Property.Secret.of("llm.test.api_key"),
        model_name=Property.Text.of("fast"),
        max_tokens=Property.Number.of(1024, minimum=1, maximum=200000),
    )
    got = await LLMProviderChecker().check(
        "llm.test", ResolvedConfig(config=cfg, resolver=_NoSecrets())
    )
    host = dialled[0][0]
    assert dialled == [(host, port)]
    assert got.detail == f"tcp {host}:{port} reachable=True auth=bearer"
