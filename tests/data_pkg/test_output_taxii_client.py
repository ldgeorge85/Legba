# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the TAXII 2.1 push client (export-interop).

The client closes the outbound STIX loop: it POSTs a STIX bundle's
objects (wrapped in a TAXII ``envelope``) to a configured TAXII 2.1
collection's add-objects endpoint. These tests pin the spec-shaped wire
format, the auth header construction, and the degrade-not-drop delivery
semantics (transient → retry+backoff → structured result, never raised;
4xx → permanent; un-provisioned/cleartext destination → loud refusal).

No live network — a recording HTTP fake stands in for ``httpx.AsyncClient``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import stix2

from legba.data.outputs.taxii_client import (
    TAXII_MEDIA_TYPE,
    TaxiiConfig,
    TaxiiPushResult,
    TaxiiServerNotConfiguredError,
    bundle_to_taxii_envelope,
    push_bundle_to_taxii,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, *, body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Http:
    """Returns a queued sequence of responses (or raises queued exceptions)."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def post(self, url, *, content=None, headers=None, timeout=None, **kw):  # noqa: ANN001
        self.calls.append({"url": url, "content": content, "headers": dict(headers or {})})
        out = self._outcomes.pop(0) if self._outcomes else _Resp(202)
        if isinstance(out, BaseException):
            raise out
        return out


async def _noop_sleep(_seconds: float) -> None:  # injected backoff sleeper
    return None


def _bundle() -> stix2.Bundle:
    ident = stix2.Identity(name="x", identity_class="system")
    report = stix2.Report(
        name="r", published="2026-06-17T00:00:00Z",
        object_refs=[ident.id], report_types=["threat-report"],
    )
    return stix2.Bundle(objects=[ident, report], allow_custom=True)


# ---------------------------------------------------------------------------
# Envelope unwrap
# ---------------------------------------------------------------------------


class TestEnvelope:
    def test_bundle_objects_become_taxii_envelope(self) -> None:
        env = bundle_to_taxii_envelope(_bundle())
        assert set(env) == {"objects"}
        assert isinstance(env["objects"], list) and len(env["objects"]) == 2
        # No bundle wrapper keys leak into the TAXII envelope.
        assert all("type" in o for o in env["objects"])  # SDOs keep their type
        assert "spec_version" not in env

    def test_accepts_json_string(self) -> None:
        env = bundle_to_taxii_envelope(_bundle().serialize())
        assert len(env["objects"]) == 2

    def test_accepts_plain_mapping(self) -> None:
        env = bundle_to_taxii_envelope({"objects": [{"type": "identity", "id": "x"}]})
        assert env["objects"] == [{"type": "identity", "id": "x"}]


# ---------------------------------------------------------------------------
# Config / URL
# ---------------------------------------------------------------------------


class TestConfig:
    def test_objects_url_is_spec_shaped(self) -> None:
        cfg = TaxiiConfig(
            server_url="https://taxii.example/",
            api_root="/api1/",
            collection_id="/c-123/",
        )
        assert cfg.objects_url() == (
            "https://taxii.example/api1/collections/c-123/objects/"
        )


# ---------------------------------------------------------------------------
# Push semantics
# ---------------------------------------------------------------------------


class TestPush:
    async def test_unprovisioned_server_url_raises(self) -> None:
        cfg = TaxiiConfig(server_url="", api_root="api1", collection_id="c1")
        with pytest.raises(TaxiiServerNotConfiguredError):
            await push_bundle_to_taxii(_bundle(), config=cfg, http=_Http([]))

    async def test_missing_http_client_raises(self) -> None:
        cfg = TaxiiConfig(server_url="https://t.example", api_root="api1", collection_id="c1")
        with pytest.raises(TaxiiServerNotConfiguredError):
            await push_bundle_to_taxii(_bundle(), config=cfg, http=None)

    async def test_cleartext_remote_refused(self) -> None:
        cfg = TaxiiConfig(server_url="http://t.example", api_root="api1", collection_id="c1")
        with pytest.raises(TaxiiServerNotConfiguredError):
            await push_bundle_to_taxii(_bundle(), config=cfg, http=_Http([_Resp(202)]))

    async def test_loopback_http_allowed(self) -> None:
        cfg = TaxiiConfig(server_url="http://127.0.0.1:9000", api_root="api1", collection_id="c1")
        http = _Http([_Resp(202, body={"id": "status--1"})])
        result = await push_bundle_to_taxii(_bundle(), config=cfg, http=http, sleep=_noop_sleep)
        assert result.delivered

    async def test_202_delivers_with_media_type_and_status_id(self) -> None:
        cfg = TaxiiConfig(server_url="https://t.example", api_root="api1", collection_id="c1")
        http = _Http([_Resp(202, body={"id": "status--abc"})])
        result = await push_bundle_to_taxii(_bundle(), config=cfg, http=http, sleep=_noop_sleep)
        assert result.outcome == "delivered"
        assert result.status_id == "status--abc"
        hdr = http.calls[0]["headers"]
        assert hdr["Content-Type"] == TAXII_MEDIA_TYPE
        assert hdr["Accept"] == TAXII_MEDIA_TYPE
        body = json.loads(http.calls[0]["content"])
        assert "objects" in body

    async def test_basic_auth_header(self) -> None:
        cfg = TaxiiConfig(
            server_url="https://t.example", api_root="api1", collection_id="c1",
            auth_kind="basic", username="alice", password="s3cret",
        )
        http = _Http([_Resp(202)])
        await push_bundle_to_taxii(_bundle(), config=cfg, http=http, sleep=_noop_sleep)
        import base64

        expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
        assert http.calls[0]["headers"]["Authorization"] == expected

    async def test_4xx_permanent_no_retry(self) -> None:
        cfg = TaxiiConfig(server_url="https://t.example", api_root="api1", collection_id="c1")
        http = _Http([_Resp(404, text="no such collection")])
        result = await push_bundle_to_taxii(_bundle(), config=cfg, http=http, sleep=_noop_sleep)
        assert result.outcome == "permanent_error"
        assert result.http_status == 404
        assert len(http.calls) == 1

    async def test_5xx_retries_then_succeeds(self) -> None:
        cfg = TaxiiConfig(
            server_url="https://t.example", api_root="api1", collection_id="c1",
            backoff_seconds=(0.0, 0.0),
        )
        http = _Http([_Resp(503), _Resp(202, body={"id": "status--ok"})])
        result = await push_bundle_to_taxii(_bundle(), config=cfg, http=http, sleep=_noop_sleep)
        assert result.delivered
        assert result.attempts == 2
        assert len(http.calls) == 2

    async def test_transient_exhaustion_degrades_not_raises(self) -> None:
        # 5xx on every attempt → transient_error result, NEVER raised.
        cfg = TaxiiConfig(
            server_url="https://t.example", api_root="api1", collection_id="c1",
            backoff_seconds=(0.0, 0.0),
        )
        http = _Http([_Resp(500), _Resp(500), _Resp(500)])
        result = await push_bundle_to_taxii(_bundle(), config=cfg, http=http, sleep=_noop_sleep)
        assert result.outcome == "transient_error"
        assert result.attempts == 3
        assert result.http_status == 500

    async def test_network_error_is_transient(self) -> None:
        cfg = TaxiiConfig(
            server_url="https://t.example", api_root="api1", collection_id="c1",
            backoff_seconds=(0.0,),
        )
        http = _Http([ConnectionError("boom"), _Resp(202)])
        result = await push_bundle_to_taxii(_bundle(), config=cfg, http=http, sleep=_noop_sleep)
        assert result.delivered
        assert result.attempts == 2
