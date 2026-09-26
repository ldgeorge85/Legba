# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A TIMED-OUT fetch gets one retry, and says so. A refused one does not.

WHY THIS FILE EXISTS. ``web_fetch`` caught ``httpx.HTTPError`` in one arm and
reported every member of it as ``fetch_failed: <str(exc)>``. ``httpx.ReadTimeout``
carries an EMPTY message, so a hung host reached the caller as
``web_fetch.http_error … err=`` — an empty reason that reads, downstream, as
"this host refused us". Measured: a 07:23Z reference build spent four of its
fifteen fetch records on ``dfat.gov.au`` / ``defence.gov.au`` URLs that were
probed afterwards and found to be REAL pages, one dated inside the build's own
window, which served 200 from the same network minutes later. A France build
read 0 pages the same way.

Three claims are pinned here:

  1. timeout -> ONE retry -> success: the page is returned, ``attempts == 2``;
  2. timeout twice -> a ``timed_out`` outcome under its own name, never
     ``fetch_failed``, and never a third attempt;
  3. everything that is NOT a timeout — a served challenge/paywall stub, a
     404, an SSRF refusal, a transport error — is attempted exactly ONCE.

The challenge-detection machinery is deliberately not touched by any of this:
a challenge page is a SERVED page (HTTP 200 with a body), so it arrives here as
``ok`` and the verdict stays with the caller that reads the body.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from legba.data.analysts.agency import web_tools
from legba.data.analysts.agency.tools import ToolCall, ToolContext
from legba.data.analysts.deterministic_handlers._challenge_detect import (
    BLOCKED_BY_CHALLENGE,
    detect_challenge_page,
)
from legba.data.schemas.action_pack import ActionPack
from legba.data.sources._egress import EgressBlockedError

URL = "https://www.dfat.gov.au/geo/australia/real-page"


def _pack() -> ActionPack:
    return ActionPack.model_validate({
        "identity": {
            "id": "web_access", "name": "web_access",
            "schema_uri": "legba/action_pack/1.0.0", "version": "a" * 16,
            "state": "active", "owner": "test",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "tools": [{"name": "web_fetch"}],
    }, strict=False)


def _call(url: str = URL) -> ToolCall:
    return ToolCall(pack_id="web_access", tool_name="web_fetch",
                    args={"url": url})


class _FakeClient:
    def __init__(self, script):
        self._script = script

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _install(monkeypatch, *script):
    """``script`` is one entry per EXPECTED attempt: a response or a raise."""
    remaining = list(script)
    attempts: list[str] = []

    def _fake_fetch_client(**kwargs):
        attempts.append(kwargs.get("headers", {}).get("User-Agent", ""))
        return _FakeClient(remaining)

    monkeypatch.setattr(web_tools, "fetch_client", _fake_fetch_client)
    return attempts


def _response(status=200, body="<html><body>" + "x" * 400 + "</body></html>"):
    return httpx.Response(
        status_code=status, text=body,
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", URL),
    )


# ---------------------------------------------------------------------------
# 1) timeout -> retry -> success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeout_exc", [
    httpx.ReadTimeout(""),          # the MEASURED shape: empty message
    httpx.ConnectTimeout("timed out"),
    httpx.PoolTimeout(""),
])
@pytest.mark.asyncio
async def test_one_timeout_then_the_page(monkeypatch, timeout_exc):
    attempts = _install(monkeypatch, timeout_exc, _response())
    result = await web_tools.web_fetch_tool(_call(), _pack(), ToolContext())

    assert result.status == "completed"
    assert result.output["status_code"] == 200
    assert result.output["fetch_outcome"] == web_tools.FETCH_OUTCOME_OK
    assert result.output["attempts"] == 2
    assert len(attempts) == 2


# ---------------------------------------------------------------------------
# 2) timeout twice -> timed_out, under its own name, and no third attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_timeouts_are_reported_as_timed_out_not_as_a_refusal(
    monkeypatch,
):
    attempts = _install(monkeypatch, httpx.ReadTimeout(""), httpx.ReadTimeout(""))
    result = await web_tools.web_fetch_tool(_call(), _pack(), ToolContext())

    assert result.status == "failed"
    assert result.output["fetch_outcome"] == web_tools.FETCH_OUTCOME_TIMED_OUT
    assert result.output["attempts"] == 2
    assert len(attempts) == 2, "exactly ONE retry, never a loop"

    error = result.error or ""
    assert error.startswith("fetch_timed_out:")
    # The empty-message defect: the CLASS is named even when str(exc) is "".
    assert "ReadTimeout" in error
    # And it must not be readable as the host refusing.
    assert "NOT a refusal" in error
    assert "fetch_failed" not in error


# ---------------------------------------------------------------------------
# 3) everything that is not a timeout is attempted ONCE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_challenge_page_is_served_not_retried(monkeypatch):
    """A challenge page is a SERVED page: one attempt, ``ok``, body intact.

    The challenge VERDICT stays where it was — with the caller that reads the
    body through ``_challenge_detect`` — and this test proves the retry did not
    step on it by asserting the detector still fires on what came back.
    """
    challenge = (
        "<html><head><title>Just a moment...</title></head><body>"
        "Checking your browser before accessing the site. "
        "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate'></script>"
        "</body></html>"
    )
    attempts = _install(monkeypatch, _response(status=200, body=challenge))
    result = await web_tools.web_fetch_tool(_call(), _pack(), ToolContext())

    assert len(attempts) == 1, "a served page is never re-fetched"
    assert result.status == "completed"
    assert result.output["fetch_outcome"] == web_tools.FETCH_OUTCOME_OK
    assert result.output["attempts"] == 1
    # Untouched machinery, still firing on the body this tool handed back.
    assert detect_challenge_page(
        result.output["body"].encode("utf-8"), "text/html",
    ) is not None
    assert BLOCKED_BY_CHALLENGE == "blocked_by_challenge"


@pytest.mark.parametrize("status", [403, 404, 429, 503])
@pytest.mark.asyncio
async def test_a_non_2xx_is_one_attempt_and_stays_a_completed_fetch(
    monkeypatch, status,
):
    attempts = _install(monkeypatch, _response(status=status, body="nope"))
    result = await web_tools.web_fetch_tool(_call(), _pack(), ToolContext())

    assert len(attempts) == 1
    assert result.status == "completed"
    assert result.output["status_code"] == status
    assert result.output["fetch_outcome"] == web_tools.FETCH_OUTCOME_OK


@pytest.mark.asyncio
async def test_an_ssrf_refusal_is_never_retried(monkeypatch):
    attempts = _install(monkeypatch, EgressBlockedError("private target"))
    result = await web_tools.web_fetch_tool(
        _call("http://10.0.0.5/internal"), _pack(), ToolContext(),
    )

    assert len(attempts) == 1
    assert result.status == "failed"
    assert (result.error or "").startswith("egress_blocked:")
    assert result.output["fetch_outcome"] == web_tools.FETCH_OUTCOME_BLOCKED


@pytest.mark.asyncio
async def test_a_non_timeout_transport_error_is_never_retried(monkeypatch):
    attempts = _install(monkeypatch, httpx.RemoteProtocolError("bad chunk"))
    result = await web_tools.web_fetch_tool(_call(), _pack(), ToolContext())

    assert len(attempts) == 1
    assert result.status == "failed"
    assert (result.error or "").startswith("fetch_failed:")
    assert result.output["fetch_outcome"] == web_tools.FETCH_OUTCOME_ERROR


def test_the_retry_budget_is_one():
    assert web_tools._FETCH_TIMEOUT_RETRIES == 1
