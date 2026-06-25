# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the L-197 alert output kind.

Covers:

  * Severity-based default routing (info / low / medium / high / critical)
  * Descriptor-level surface opt-in / opt-out
  * Missing-extra graceful degradation (xmpp / matrix not installed)
  * Critical-severity retry on transient sink failure
  * Pushover priority + auth resolution
  * NATS envelope shape

No external services required — all transports are exercised through
typed fakes that satisfy the structural-typing surfaces in
:mod:`legba.data.outputs._contract`. Real HTTPS / XMPP / Matrix
deliveries would burn token budgets and are intentionally avoided.
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import uuid4

import pytest

from legba.data.outputs import alert
from legba.data.outputs._contract import (
    OutputContext,
    OutputDeps,
    OutputSurface,
    SurfaceResult,
)
from legba.data.outputs.alert_sinks import (
    matrix as matrix_sink,
    nats as nats_sink,
    pushover as pushover_sink,
    xmpp as xmpp_sink,
)
from legba.data.provenance.models import AlertPayload, FindingPayload


# ---------------------------------------------------------------------------
# Typed fakes
# ---------------------------------------------------------------------------


class _RecordingNats:
    def __init__(self, raise_n: int = 0) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._raise_n = raise_n

    async def publish_core(self, subject: str, payload: bytes) -> None:
        # The alert sink publishes on the streamless legba.alerts.* subject via
        # publish_core (not publish_json — that NoStreamResponseErrors here).
        if self._raise_n > 0:
            self._raise_n -= 1
            raise RuntimeError("simulated transient NATS error")
        self.calls.append((subject, json.loads(payload.decode("utf-8"))))

    async def publish_json(self, subject: str, payload: bytes) -> None:
        await self.publish_core(subject, payload)


class _HttpResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _RecordingHttp:
    """Records POSTs, returns canned responses.

    `responses` is a list of `_HttpResponse` consumed in FIFO order. If the
    list is exhausted, falls back to `default_response`.
    """

    def __init__(
        self,
        responses: list[_HttpResponse] | None = None,
        default_response: _HttpResponse | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses or [])
        self._default = default_response or _HttpResponse(200, "1")

    async def post(
        self,
        url: str,
        *,
        data: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> _HttpResponse:
        self.calls.append({"url": url, "data": dict(data or {}), "json": dict(json or {})})
        if self._responses:
            return self._responses.pop(0)
        return self._default


class _RecordingXmpp:
    def __init__(self, raise_n: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self._raise_n = raise_n

    async def send_message(self, to: str, body: str) -> None:
        if self._raise_n > 0:
            self._raise_n -= 1
            raise RuntimeError("simulated XMPP transient")
        self.calls.append((to, body))


class _RecordingMatrix:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def send_message(self, room_id: str, body: str) -> None:
        self.calls.append((room_id, body))


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


def _ctx() -> OutputContext:
    return OutputContext(
        analyst_id="analyst.test",
        analyst_version="1.0.0",
        target_id="target.test",
        target_version="1",
        run_id=str(uuid4()),
    )


def _ctx_with_secrets(token: str = "tok", user: str = "usr") -> OutputContext:
    async def _resolver(key: str) -> str:
        return {"pushover.token": token, "pushover.user": user}.get(key, "")
    return OutputContext(
        analyst_id="analyst.test",
        analyst_version="1.0.0",
        target_id="target.test",
        target_version="1",
        run_id=str(uuid4()),
        secrets_resolve=_resolver,
    )


def _payload(severity: str = "medium", **overrides: Any) -> AlertPayload:
    base = dict(
        title="Server X CPU spike",
        body="Sustained 95% CPU for 6 minutes.",
        severity=severity,
        confidence=0.8,
        tags=["infra", "cpu"],
        routing_hint="https://example.org/alert/123",
    )
    base.update(overrides)
    return AlertPayload(**base)


# ---------------------------------------------------------------------------
# Default severity matrix
# ---------------------------------------------------------------------------


def test_default_surfaces_matrix_matches_documented_ladder():
    # Documented at module level; pin it as a behaviour test so future
    # edits to the ladder trip a clear failure.
    assert alert.DEFAULT_SURFACES["info"]     == ("nats",)
    assert alert.DEFAULT_SURFACES["low"]      == ("nats",)
    assert alert.DEFAULT_SURFACES["medium"]   == ("nats", "pushover")
    assert alert.DEFAULT_SURFACES["high"]     == ("nats", "pushover", "xmpp", "matrix")
    assert alert.DEFAULT_SURFACES["critical"] == ("nats", "pushover", "xmpp", "matrix")


# ---------------------------------------------------------------------------
# Per-severity routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_info_routes_only_to_nats():
    nats = _RecordingNats()
    http = _RecordingHttp()
    xmpp = _RecordingXmpp()
    matrix = _RecordingMatrix()
    deps = OutputDeps(nats=nats, http=http, xmpp=xmpp, matrix=matrix)

    results = await alert.emit(_payload("info"), descriptor=None, deps=deps, ctx=_ctx())

    surfaces = {r.surface for r in results}
    assert surfaces == {"nats"}
    assert len(nats.calls) == 1
    assert nats.calls[0][0] == "legba.alerts.info"
    # No operator-facing transports should fire.
    assert http.calls == []
    assert xmpp.calls == []
    assert matrix.calls == []


@pytest.mark.asyncio
async def test_low_routes_only_to_nats_by_default():
    """`low` is NATS-only by default; descriptor must opt in for Pushover."""
    nats = _RecordingNats()
    http = _RecordingHttp()
    deps = OutputDeps(nats=nats, http=http)

    results = await alert.emit(_payload("low"), descriptor=None, deps=deps, ctx=_ctx())

    assert {r.surface for r in results} == {"nats"}
    assert nats.calls[0][0] == "legba.alerts.low"
    assert http.calls == []


@pytest.mark.asyncio
async def test_low_opt_in_pushover():
    nats = _RecordingNats()
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    deps = OutputDeps(nats=nats, http=http)
    descriptor = {
        "surfaces": [
            {"name": "pushover", "mode": "on", "min_severity": "low"},
        ],
        "pushover": {"user": "u-key"},
    }

    results = await alert.emit(
        _payload("low"), descriptor=descriptor, deps=deps, ctx=_ctx_with_secrets()
    )

    by_surface = {r.surface: r for r in results}
    assert by_surface["nats"].delivered
    assert by_surface["pushover"].delivered
    # Pushover priority for low is -1.
    assert http.calls[0]["data"]["priority"] == -1


@pytest.mark.asyncio
async def test_medium_routes_nats_and_pushover_by_default():
    nats = _RecordingNats()
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    deps = OutputDeps(nats=nats, http=http)

    results = await alert.emit(
        _payload("medium"), descriptor=None, deps=deps, ctx=_ctx_with_secrets()
    )

    by_surface = {r.surface: r for r in results}
    assert set(by_surface) == {"nats", "pushover"}
    assert by_surface["nats"].delivered
    assert by_surface["pushover"].delivered
    assert nats.calls[0][0] == "legba.alerts.medium"
    # Pushover priority for medium is 0.
    assert http.calls[0]["data"]["priority"] == 0


@pytest.mark.asyncio
async def test_high_routes_all_four_when_extras_wired():
    nats = _RecordingNats()
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    xmpp = _RecordingXmpp()
    matrix = _RecordingMatrix()
    deps = OutputDeps(nats=nats, http=http, xmpp=xmpp, matrix=matrix)
    descriptor = {
        "xmpp": {"to": "ops@example.org"},
        "matrix": {"room": "!alerts:example.org"},
    }

    results = await alert.emit(
        _payload("high"), descriptor=descriptor, deps=deps, ctx=_ctx_with_secrets()
    )

    by_surface = {r.surface: r for r in results}
    assert set(by_surface) == {"nats", "pushover", "xmpp", "matrix"}
    assert all(r.delivered for r in results)
    assert nats.calls[0][0] == "legba.alerts.high"
    # Pushover priority for high is 1.
    assert http.calls[0]["data"]["priority"] == 1
    assert xmpp.calls[0][0] == "ops@example.org"
    assert matrix.calls[0][0] == "!alerts:example.org"


@pytest.mark.asyncio
async def test_critical_routes_all_with_priority_2_pushover():
    nats = _RecordingNats()
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    xmpp = _RecordingXmpp()
    matrix = _RecordingMatrix()
    deps = OutputDeps(nats=nats, http=http, xmpp=xmpp, matrix=matrix)
    descriptor = {
        "xmpp": {"to": "ops@example.org"},
        "matrix": {"room": "!alerts:example.org"},
    }

    results = await alert.emit(
        _payload("critical"), descriptor=descriptor, deps=deps, ctx=_ctx_with_secrets()
    )

    by_surface = {r.surface: r for r in results}
    assert set(by_surface) == {"nats", "pushover", "xmpp", "matrix"}
    assert nats.calls[0][0] == "legba.alerts.critical"
    # Pushover priority for critical is 2; retry+expire must be set.
    posted = http.calls[0]["data"]
    assert posted["priority"] == 2
    assert posted["retry"] == 60
    assert posted["expire"] == 3600


# ---------------------------------------------------------------------------
# Descriptor opt-in / opt-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_descriptor_opt_out_drops_pushover_at_high():
    """`high` defaults include pushover; descriptor mode=off overrides."""
    nats = _RecordingNats()
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    xmpp = _RecordingXmpp()
    deps = OutputDeps(nats=nats, http=http, xmpp=xmpp)
    descriptor = {
        "surfaces": [{"name": "pushover", "mode": "off"}],
        "xmpp": {"to": "ops@example.org"},
        # Suppress matrix so the test doesn't care about it.
        "_unused": True,
    }
    # We also opt matrix off so the test doesn't depend on extra-installation
    # status. Default high includes matrix; opt-out suppresses it.
    descriptor["surfaces"].append({"name": "matrix", "mode": "off"})

    results = await alert.emit(
        _payload("high"), descriptor=descriptor, deps=deps, ctx=_ctx_with_secrets()
    )

    surfaces = {r.surface for r in results}
    assert "pushover" not in surfaces
    assert "matrix" not in surfaces
    assert surfaces == {"nats", "xmpp"}
    assert http.calls == []


@pytest.mark.asyncio
async def test_descriptor_opt_in_at_higher_min_severity_no_op_below_threshold():
    """opt-in with min_severity=high should NOT fire on medium alerts."""
    nats = _RecordingNats()
    http = _RecordingHttp()
    xmpp = _RecordingXmpp()
    deps = OutputDeps(nats=nats, http=http, xmpp=xmpp)
    descriptor = {
        "surfaces": [
            {"name": "xmpp", "mode": "on", "min_severity": "high",
             "destination": "ops@example.org"},
        ],
    }

    results = await alert.emit(
        _payload("medium"), descriptor=descriptor, deps=deps, ctx=_ctx_with_secrets()
    )

    surfaces = {r.surface for r in results}
    assert surfaces == {"nats", "pushover"}  # medium default, no xmpp
    assert xmpp.calls == []


@pytest.mark.asyncio
async def test_descriptor_destination_override_used():
    nats = _RecordingNats()
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    xmpp = _RecordingXmpp()
    matrix = _RecordingMatrix()
    deps = OutputDeps(nats=nats, http=http, xmpp=xmpp, matrix=matrix)
    descriptor = {
        "surfaces": [
            {"name": "xmpp", "destination": "muc-room@conference.example.org"},
            {"name": "matrix", "destination": "!ops:example.org"},
        ],
    }

    await alert.emit(
        _payload("high"), descriptor=descriptor, deps=deps, ctx=_ctx_with_secrets()
    )

    assert xmpp.calls[0][0] == "muc-room@conference.example.org"
    assert matrix.calls[0][0] == "!ops:example.org"


# ---------------------------------------------------------------------------
# Missing-extra fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_xmpp_extra_records_skipped_no_error(monkeypatch):
    """When `legba[xmpp]` isn't installed AND no deps.xmpp publisher is
    wired, the sink records `skipped` with `extra-not-installed` instead
    of raising."""
    monkeypatch.setattr(xmpp_sink, "XMPP_AVAILABLE", False)
    nats = _RecordingNats()
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    matrix = _RecordingMatrix()
    # Intentionally pass no xmpp deps.
    deps = OutputDeps(nats=nats, http=http, matrix=matrix)
    descriptor = {
        "matrix": {"room": "!alerts:example.org"},
        # Don't even need an XMPP destination — the surface should be
        # skipped before destination resolution.
    }

    results = await alert.emit(
        _payload("high"), descriptor=descriptor, deps=deps, ctx=_ctx_with_secrets()
    )

    by_surface = {r.surface: r for r in results}
    assert by_surface["xmpp"].outcome == "skipped"
    assert by_surface["xmpp"].detail == "extra-not-installed"
    # Other surfaces still deliver.
    assert by_surface["nats"].delivered
    assert by_surface["pushover"].delivered
    assert by_surface["matrix"].delivered


@pytest.mark.asyncio
async def test_missing_matrix_extra_records_skipped_no_error(monkeypatch):
    monkeypatch.setattr(matrix_sink, "MATRIX_AVAILABLE", False)
    nats = _RecordingNats()
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    xmpp = _RecordingXmpp()
    deps = OutputDeps(nats=nats, http=http, xmpp=xmpp)
    descriptor = {"xmpp": {"to": "ops@example.org"}}

    results = await alert.emit(
        _payload("high"), descriptor=descriptor, deps=deps, ctx=_ctx_with_secrets()
    )

    by_surface = {r.surface: r for r in results}
    assert by_surface["matrix"].outcome == "skipped"
    assert by_surface["matrix"].detail == "extra-not-installed"


# ---------------------------------------------------------------------------
# Critical retry on transient failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critical_retries_on_transient_failure_eventually_succeeds():
    """Critical alerts retry transient sink failures.

    The NATS fake is configured to raise the first 2 publish_json calls,
    then succeed. With CRITICAL_RETRY_MAX_ATTEMPTS=3 we expect the
    final outcome to be `delivered`.
    """
    nats = _RecordingNats(raise_n=2)
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    deps = OutputDeps(nats=nats, http=http)
    # Tight backoff so the test stays fast.
    descriptor = {"_retry_backoff": [0.001, 0.001, 0.001]}

    results = await alert.emit(
        _payload("critical"), descriptor=descriptor, deps=deps, ctx=_ctx_with_secrets()
    )

    by_surface = {r.surface: r for r in results}
    # NATS retried successfully.
    assert by_surface["nats"].outcome == "delivered"
    assert len(nats.calls) == 1  # only the final successful publish recorded


@pytest.mark.asyncio
async def test_non_critical_does_not_retry_on_transient_failure():
    """`medium` does not retry — transient error stays transient."""
    nats = _RecordingNats(raise_n=2)
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    deps = OutputDeps(nats=nats, http=http)

    results = await alert.emit(
        _payload("medium"), descriptor=None, deps=deps, ctx=_ctx_with_secrets()
    )

    by_surface = {r.surface: r for r in results}
    assert by_surface["nats"].outcome == "transient_error"
    # Pushover still succeeds.
    assert by_surface["pushover"].delivered


@pytest.mark.asyncio
async def test_critical_retry_gives_up_after_max_attempts():
    """If transient errors persist past the retry budget, surface
    `transient_error`."""
    # raise_n high enough that retries never succeed.
    nats = _RecordingNats(raise_n=99)
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    deps = OutputDeps(nats=nats, http=http)
    descriptor = {"_retry_backoff": [0.001] * 5}

    results = await alert.emit(
        _payload("critical"), descriptor=descriptor, deps=deps, ctx=_ctx_with_secrets()
    )

    by_surface = {r.surface: r for r in results}
    assert by_surface["nats"].outcome == "transient_error"
    # Other surfaces still delivered.
    assert by_surface["pushover"].delivered


# ---------------------------------------------------------------------------
# Envelope shape (NATS)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nats_envelope_contains_required_fields():
    nats = _RecordingNats()
    deps = OutputDeps(nats=nats)
    ctx = _ctx()
    payload = _payload("info", title="t", body="b", tags=["x"])

    await alert.emit(payload, descriptor=None, deps=deps, ctx=ctx)

    assert len(nats.calls) == 1
    subject, env = nats.calls[0]
    assert subject == "legba.alerts.info"
    for key in (
        "kind", "severity", "title", "body", "confidence", "tags", "evidence",
        "routing_hint", "analyst_id", "analyst_version", "target_id",
        "target_version", "run_id", "emitted_at",
    ):
        assert key in env
    assert env["kind"] == "alert"
    assert env["severity"] == "info"
    assert env["analyst_id"] == ctx.analyst_id


# ---------------------------------------------------------------------------
# Pushover credential paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pushover_resolves_token_via_vault():
    """Pushover token comes from the secrets resolver, not the descriptor."""
    captured: list[str] = []

    async def resolver(key: str) -> str:
        captured.append(key)
        return {"pushover.token": "VAULT-TOK", "pushover.user": "VAULT-USR"}.get(key, "")

    ctx = OutputContext(secrets_resolve=resolver)
    nats = _RecordingNats()
    http = _RecordingHttp(default_response=_HttpResponse(200, "1"))
    deps = OutputDeps(nats=nats, http=http)

    await alert.emit(_payload("medium"), descriptor=None, deps=deps, ctx=ctx)

    assert "pushover.token" in captured
    posted = http.calls[0]["data"]
    assert posted["token"] == "VAULT-TOK"
    assert posted["user"] == "VAULT-USR"


@pytest.mark.asyncio
async def test_pushover_4xx_is_permanent_error():
    nats = _RecordingNats()
    http = _RecordingHttp(default_response=_HttpResponse(400, '{"errors":["invalid user"]}'))
    deps = OutputDeps(nats=nats, http=http)

    results = await alert.emit(
        _payload("medium"), descriptor=None, deps=deps, ctx=_ctx_with_secrets()
    )

    by_surface = {r.surface: r for r in results}
    assert by_surface["pushover"].outcome == "permanent_error"
    assert "400" in by_surface["pushover"].detail


@pytest.mark.asyncio
async def test_pushover_5xx_is_transient_error():
    nats = _RecordingNats()
    http = _RecordingHttp(default_response=_HttpResponse(503, "Service Unavailable"))
    deps = OutputDeps(nats=nats, http=http)

    results = await alert.emit(
        _payload("medium"), descriptor=None, deps=deps, ctx=_ctx_with_secrets()
    )

    by_surface = {r.surface: r for r in results}
    assert by_surface["pushover"].outcome == "transient_error"


@pytest.mark.asyncio
async def test_pushover_missing_creds_is_permanent_error():
    """No resolver, no fallback → no-token permanent error."""
    nats = _RecordingNats()
    http = _RecordingHttp()
    deps = OutputDeps(nats=nats, http=http)

    results = await alert.emit(
        _payload("medium"), descriptor=None, deps=deps, ctx=_ctx()
    )

    by_surface = {r.surface: r for r in results}
    assert by_surface["pushover"].outcome == "permanent_error"
    assert by_surface["pushover"].detail == "no-token"


# ---------------------------------------------------------------------------
# Surface parsing edge cases
# ---------------------------------------------------------------------------


def test_parse_surfaces_accepts_list_form():
    parsed = alert._parse_surfaces({
        "surfaces": [
            {"name": "pushover", "mode": "on", "min_severity": "low"},
        ],
    })
    assert "pushover" in parsed
    assert parsed["pushover"].mode == "on"
    assert parsed["pushover"].min_severity == "low"


def test_parse_surfaces_accepts_mapping_form():
    parsed = alert._parse_surfaces({
        "surfaces": {
            "pushover": {"mode": "off"},
            "matrix": {"mode": "on", "destination": "!ops:example.org"},
        },
    })
    assert parsed["pushover"].mode == "off"
    assert parsed["matrix"].destination == "!ops:example.org"


def test_parse_surfaces_unwraps_full_descriptor():
    """Accept the full descriptor with nested outputs.alert.surfaces."""
    parsed = alert._parse_surfaces({
        "outputs": {
            "alert": {
                "surfaces": [{"name": "matrix", "mode": "off"}],
            },
        },
    })
    assert parsed["matrix"].mode == "off"


def test_parse_surfaces_rejects_malformed():
    with pytest.raises((ValueError, Exception)):
        alert._parse_surfaces({"surfaces": "not-a-list-or-mapping"})


def test_parse_surfaces_rejects_unknown_surface_name():
    """OutputSurface validates name against the Literal set."""
    with pytest.raises(Exception):
        alert._parse_surfaces({
            "surfaces": [{"name": "slack", "mode": "on"}],
        })


# ---------------------------------------------------------------------------
# Misc — KIND_NAME stability
# ---------------------------------------------------------------------------


def test_kind_name_is_alert():
    assert alert.KIND_NAME == "alert"


# ---------------------------------------------------------------------------
# L-197 — runtime output-binding path: FindingPayload → AlertPayload coercion
# + severity gate (the audit's alert_sink_deliveries=0 fix).
# ---------------------------------------------------------------------------


_ALERT_DESC = {
    "outputs": [
        {"kind": "alert", "config": {"min_severity": "high"}},
    ]
}


async def test_emit_coerces_high_confidence_finding_to_alert():
    """A FindingPayload (no explicit severity) with high confidence is
    coerced into a high-severity AlertPayload and reaches NATS."""
    nats = _RecordingNats()
    deps = OutputDeps(nats=nats)
    finding = FindingPayload(
        title="Coup attempt reported in country X",
        body="Multiple corroborating signals.",
        confidence=0.8,  # → "high" on the ladder
        tags=["g20", "instability"],
    )
    results = await alert.emit(
        finding, descriptor=_ALERT_DESC, deps=deps, ctx=_ctx(),
        output_id=uuid4(),
    )
    # High severity → the full ladder is attempted; nats is always present.
    assert "nats" in [r.surface for r in results]
    assert nats.calls, "high finding must publish to NATS"
    subject, body = nats.calls[0]
    assert subject == "legba.alerts.high"
    assert body["severity"] == "high"


async def test_emit_gates_out_low_confidence_finding():
    """A low-confidence finding is below the min_severity gate → no surfaces
    fire and emit returns an empty result list (no-op)."""
    nats = _RecordingNats()
    deps = OutputDeps(nats=nats)
    finding = FindingPayload(
        title="Routine market commentary",
        body="Nothing actionable.",
        confidence=0.3,  # → "low" on the ladder, below high gate
    )
    results = await alert.emit(
        finding, descriptor=_ALERT_DESC, deps=deps, ctx=_ctx(),
        output_id=uuid4(),
    )
    assert results == []
    assert nats.calls == []


async def test_emit_honours_explicit_data_severity_over_confidence():
    """When the analyst stamps data.severity, it wins over the confidence
    ladder (mirrors _maybe_escalate_finding's resolution order)."""
    nats = _RecordingNats()
    deps = OutputDeps(nats=nats)
    finding = FindingPayload(
        title="Escalation",
        body="...",
        confidence=0.2,  # ladder would say "info"
        data={"severity": "critical"},
    )
    results = await alert.emit(
        finding, descriptor=_ALERT_DESC, deps=deps, ctx=_ctx(),
        output_id=uuid4(),
    )
    assert nats.calls and nats.calls[0][1]["severity"] == "critical"
    assert results, "explicit critical severity must fire surfaces"


async def test_emit_threads_output_id_into_alert_row_id_for_audit():
    """output_id is stamped onto ctx.alert_row_id so the alert_sink_deliveries
    audit writer (when a pg_pool is wired) FKs against the parent finding."""

    class _RecordingPool:
        def __init__(self) -> None:
            self.rows: list[tuple] = []

        async def execute(self, sql: str, *args: Any) -> None:
            self.rows.append(args)

    pool = _RecordingPool()
    nats = _RecordingNats()
    deps = OutputDeps(nats=nats, pg_pool=pool)
    oid = uuid4()
    finding = FindingPayload(title="High sev", body="b", confidence=0.85)
    # ctx without a preset alert_row_id — emit must thread output_id in.
    ctx = OutputContext(analyst_id="country_assessor", analyst_version="1")
    await alert.emit(
        finding, descriptor=_ALERT_DESC, deps=deps, ctx=ctx, output_id=oid,
    )
    assert pool.rows, "a delivery audit row must be written when pg_pool is set"
    # First positional arg of the INSERT is the alert_row_id (the output_id).
    assert pool.rows[0][0] == oid


async def test_emit_malformed_surface_mode_is_audited_not_swallowed():
    """K-1 regression: a descriptor surface with mode=True (the YAML-1.1
    `mode: on`→bool trap) fails OutputSurface validation in _parse_surfaces. The
    failure must re-raise AND write an AUDITED alert_sink_deliveries error row
    (sink_kind='_descriptor', status='error') — not vanish, leaving the table
    empty (which is exactly how the dead alert pipeline hid: 0 deliveries, 0
    attempts)."""

    class _RecordingPool:
        def __init__(self) -> None:
            self.rows: list[tuple] = []

        async def execute(self, sql: str, *args: Any) -> None:
            self.rows.append(args)

    pool = _RecordingPool()
    deps = OutputDeps(nats=_RecordingNats(), pg_pool=pool)
    bad_desc = {
        "outputs": [
            {"kind": "alert", "config": {
                "min_severity": "high",
                # mode=True mimics PyYAML parsing the bareword `on`.
                "surfaces": [{"name": "pushover", "mode": True, "min_severity": "high"}],
            }},
        ]
    }
    finding = FindingPayload(title="High sev", body="b", confidence=0.9)
    with pytest.raises(Exception):
        await alert.emit(
            finding, descriptor=bad_desc, deps=deps,
            ctx=OutputContext(analyst_id="country_assessor", analyst_version="1"),
            output_id=uuid4(),
        )
    assert pool.rows, "the parse failure must write an audited error row, not be swallowed"
    args = pool.rows[0]
    assert "_descriptor" in args and "error" in args


async def test_emit_honours_surfaces_in_dispatcher_list_shape():
    """The runtime dispatcher passes a LIST-shaped descriptor
    ({"outputs": [{"kind": "alert", "config": {...}}]}). _parse_surfaces must
    resolve the alert binding's surface overrides from that shape so the live
    descriptor's opt-in/opt-out is honoured (e.g. matrix forced off at high)."""
    nats = _RecordingNats()
    matrix = _RecordingMatrix()
    deps = OutputDeps(nats=nats, matrix=matrix)
    desc = {
        "outputs": [
            {
                "kind": "alert",
                "config": {
                    "min_severity": "high",
                    "surfaces": [{"name": "matrix", "mode": "off"}],
                },
            }
        ]
    }
    finding = FindingPayload(title="t", body="b", confidence=0.95)  # critical
    results = await alert.emit(
        finding, descriptor=desc, deps=deps, ctx=_ctx(), output_id=uuid4(),
    )
    surfaces = [r.surface for r in results]
    assert "matrix" not in surfaces, "descriptor forced matrix off"
    assert "nats" in surfaces


async def test_emit_default_gate_is_high_when_config_absent():
    """With no descriptor config, the default gate is 'high' — a medium
    finding does NOT page."""
    nats = _RecordingNats()
    deps = OutputDeps(nats=nats)
    medium = FindingPayload(title="m", body="b", confidence=0.6)  # → medium
    results = await alert.emit(medium, descriptor=None, deps=deps, ctx=_ctx())
    assert results == []
    assert nats.calls == []
