# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1-1 alert-sink plane — registry, payload anatomy, receipt links.

Unit tests at the :mod:`legba.data.alerts.sinks` seam: the sink registry
(keyed by sink_kind, fail-loud duplicates), the converged
:class:`AlertSinkPayload` anatomy (verify state, receipt link, redaction),
and the dispatcher's DB-enriched payload assembly (fake duck-typed pool —
mirrors ``tests/data_pkg/agency/test_channel_delivery_audit.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from legba.data.alerts import (
    ENV_PUBLIC_BASE_URL,
    AlertSinkDispatcher,
    receipt_link,
    redact_url_to_host,
    register_alert_sink,
    registered_sink_kinds,
    runtime_alert_payload,
)
from legba.data.alerts.sinks import (
    _SINK_FACTORIES,
    normalise_severity,
    unverified_state,
    verify_state_from_score,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_webhook_is_registered_by_package_import(self) -> None:
        assert "webhook" in registered_sink_kinds()

    def test_duplicate_registration_raises(self) -> None:
        marker = f"test_sink_{uuid4().hex[:8]}"
        register_alert_sink(marker, lambda: None)  # type: ignore[arg-type,return-value]
        try:
            with pytest.raises(ValueError, match="already registered"):
                register_alert_sink(marker, lambda: None)  # type: ignore[arg-type,return-value]
            # replace=True is the sanctioned override (tests / reload).
            register_alert_sink(marker, lambda: None, replace=True)  # type: ignore[arg-type,return-value]
        finally:
            _SINK_FACTORIES.pop(marker, None)


# ---------------------------------------------------------------------------
# Verify state + severity + redaction helpers
# ---------------------------------------------------------------------------


class TestAnatomyHelpers:
    def test_verify_state_folds_score(self) -> None:
        assert verify_state_from_score(0.87) == "faithfulness=0.87"

    def test_verify_state_unverified_names_reason(self) -> None:
        state = verify_state_from_score(None)
        assert state.startswith("unverified — ")
        assert "no faithfulness verdict" in state

    def test_verify_state_rejects_bool(self) -> None:
        # True is an int subclass — must NOT read as a score of 1.00.
        assert verify_state_from_score(True).startswith("unverified")

    def test_unverified_state_shape(self) -> None:
        assert unverified_state("because") == "unverified — because"

    def test_normalise_severity(self) -> None:
        assert normalise_severity("HIGH") == "high"
        assert normalise_severity("bogus", default="info") == "info"
        assert normalise_severity(None) == "info"

    def test_redact_url_to_host_strips_secret_path(self) -> None:
        assert (
            redact_url_to_host("https://hooks.example.com/T123/B456/secret")
            == "hooks.example.com"
        )

    def test_redact_url_to_host_tolerates_junk(self) -> None:
        assert redact_url_to_host("") == ""
        assert redact_url_to_host(None) == ""


# ---------------------------------------------------------------------------
# Receipt link
# ---------------------------------------------------------------------------


class TestReceiptLink:
    def test_relative_path_always_present(self, monkeypatch: Any) -> None:
        monkeypatch.delenv(ENV_PUBLIC_BASE_URL, raising=False)
        rid = uuid4()
        path, url = receipt_link(rid)
        assert path == f"/api/v1/lineage/finding/{rid}"
        assert url is None

    def test_absolute_url_from_env_base(self, monkeypatch: Any) -> None:
        monkeypatch.setenv(ENV_PUBLIC_BASE_URL, "https://legba.example.org/")
        path, url = receipt_link("abc", row_kind="alert")
        assert path == "/api/v1/lineage/alert/abc"
        assert url == "https://legba.example.org/api/v1/lineage/alert/abc"

    def test_no_row_id_no_link(self) -> None:
        assert receipt_link(None) == (None, None)
        assert receipt_link("") == (None, None)


# ---------------------------------------------------------------------------
# Runtime (deterministic) payload
# ---------------------------------------------------------------------------


class TestRuntimePayload:
    def test_stall_alert_payload_shape(self) -> None:
        p = runtime_alert_payload(
            channel_name="liveness_stall",
            summary="Pipeline stall: no signal or finding for 20 min",
            detail="check the pollers",
            severity="high",
        )
        assert p.channel_name == "liveness_stall"
        assert p.severity == "high"
        assert p.verify_state.startswith("unverified — ")
        assert "deterministic" in p.verify_state
        assert p.alert_row_id is None and p.receipt_path is None
        assert p.detected_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Dispatcher payload assembly (DB-enriched, fake pool)
# ---------------------------------------------------------------------------


class _EnrichPool:
    """Duck-typed pool: fetchrow → the finding, fetch → its cited signals."""

    def __init__(self, finding: dict[str, Any] | None, signals: list[dict[str, Any]]) -> None:
        self._finding = finding
        self._signals = signals
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.queries.append((sql, args))
        return self._finding

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.queries.append((sql, args))
        return self._signals

    async def execute(self, sql: str, *args: Any) -> None:
        self.queries.append((sql, args))


@pytest.mark.asyncio
class TestPayloadForFinding:
    async def test_enriched_payload_carries_full_anatomy(self) -> None:
        oid = uuid4()
        sig_a, sig_b = uuid4(), uuid4()
        produced = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        pool = _EnrichPool(
            finding={"produced_at": produced, "derived_from": [sig_a, sig_b]},
            signals=[
                {"canonical_url": "https://news.example.org/a", "geo": ["UA"]},
                {"canonical_url": "https://wire.example.net/b", "geo": ["UA", "RU"]},
                {"canonical_url": None, "geo": []},
            ],
        )
        d = AlertSinkDispatcher(pg_pool=pool, sinks=[])
        p = await d.payload_for_finding(
            channel_name="escalations",
            alert_row_id=str(oid),
            target_id="ua",
            severity="critical",
            effective_confidence=0.91,
            title="Coup-risk spike",
            detail="Multiple corroborating signals.",
            faithfulness_score=0.83,
        )
        assert p.summary == "Coup-risk spike"
        assert p.severity == "critical"
        assert p.target_id == "ua"
        assert p.effective_confidence == pytest.approx(0.91)
        assert p.verify_state == "faithfulness=0.83"
        assert p.event_at == produced
        assert p.source_links == (
            "https://news.example.org/a",
            "https://wire.example.net/b",
        )
        assert p.geo == ("UA", "RU")
        assert p.alert_row_id == str(oid)
        assert p.receipt_path == f"/api/v1/lineage/finding/{oid}"

    async def test_enrichment_failure_degrades_not_raises(self) -> None:
        class _BrokenPool:
            async def fetchrow(self, sql: str, *args: Any) -> Any:
                raise RuntimeError("connection blip")

        d = AlertSinkDispatcher(pg_pool=_BrokenPool(), sinks=[])
        p = await d.payload_for_finding(
            channel_name="escalations",
            alert_row_id=str(uuid4()),
            target_id="us",
            severity="high",
            effective_confidence=0.9,
            title="t",
        )
        assert p.source_links == () and p.geo == () and p.event_at is None
        # The receipt anchor survives un-enriched.
        assert p.receipt_path is not None

    async def test_unverified_when_no_faithfulness_score(self) -> None:
        d = AlertSinkDispatcher(pg_pool=None, sinks=[])
        p = await d.payload_for_finding(
            channel_name="escalations",
            alert_row_id=None,
            target_id=None,
            severity="high",
            effective_confidence=None,
            title="t",
        )
        assert p.verify_state.startswith("unverified — ")
        assert p.receipt_path is None
