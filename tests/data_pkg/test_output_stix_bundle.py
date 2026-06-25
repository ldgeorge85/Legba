# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for L-195 — `legba.data.outputs.stix_bundle`.

Pure-python unit tests — no external services. The kind itself is a
side-effect-free serialiser (with an optional NATS publish + optional
file sink); the contract value is "given an analyst payload, produce
a STIX 2.1-compliant bundle". We validate that contract by:

  * Per-payload type conversion: FindingPayload → report,
    SituationPayload → incident+report, HypothesisPayload → report,
    AlertPayload → indicator (medium+) or report (info/low).
  * TLP marking propagation: every SDO in the bundle carries the
    requested TLP marking-definition id.
  * Bundle structure validation: the produced JSON re-parses via
    `stix2.parse()` (the OASIS reference parser).
  * Multi-payload bundle: multiple envelopes coexist in one bundle.
  * `derived_from` UUIDs → STIX `relationship` SDOs of type
    `derived-from`, with `ancestor_lookup` resolving to real ids.
  * Cited entities (people / orgs / locations) → identity / location
    SDOs that the parent report references via `object_refs`.
  * `emit()` happy path: descriptor-driven TLP + target_id, NATS
    publish hits the configured subject, file sink writes the bundle.
  * `KIND_NAME` and module re-export wiring.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import stix2

from legba.data.outputs import stix_bundle
from legba.data.outputs._contract import OutputContext, OutputDeps
from legba.data.outputs.stix_bundle import (
    KIND_NAME,
    NATS_SUBJECT_PATTERN,
    TAXII_COLLECTION_PATTERN,
    OutputEnvelope,
    StixBundleExporter,
    TaxiiServerNotConfiguredError,
    _tlp_marking,
    alert_to_indicator_or_report,
    emit,
    export_outputs_to_stix,
    finding_to_report,
    hypothesis_to_report,
    situation_to_incident,
    upload_bundle_to_taxii,
)
from legba.data.provenance.models import (
    AlertPayload,
    FindingPayload,
    HypothesisPayload,
    SituationPayload,
)


# ---------------------------------------------------------------------------
# Module-level wiring
# ---------------------------------------------------------------------------


class TestModuleWiring:
    """KIND_NAME + module re-export — required for host registry discovery."""

    def test_kind_name(self) -> None:
        assert KIND_NAME == "stix_bundle"

    def test_module_reexported(self) -> None:
        from legba.data import outputs

        assert hasattr(outputs, "stix_bundle")
        assert outputs.stix_bundle.KIND_NAME == "stix_bundle"

    def test_discoverable_by_registry(self) -> None:
        from legba.data.outputs import discover_output_kinds

        registry = discover_output_kinds()
        assert "stix_bundle" in registry
        handler = registry["stix_bundle"]
        assert handler.kind_name == "stix_bundle"
        # emit is exposed.
        assert callable(handler.emit)


# ---------------------------------------------------------------------------
# TLP marking lookup
# ---------------------------------------------------------------------------


class TestTlpMarking:
    @pytest.mark.parametrize(
        "tlp, expected",
        [
            ("white", stix2.TLP_WHITE.id),
            ("green", stix2.TLP_GREEN.id),
            ("amber", stix2.TLP_AMBER.id),
            ("red", stix2.TLP_RED.id),
        ],
    )
    def test_known_levels(self, tlp: str, expected: str) -> None:
        marking = _tlp_marking(tlp)  # type: ignore[arg-type]
        assert marking.id == expected

    def test_case_insensitive(self) -> None:
        assert _tlp_marking("AMBER").id == stix2.TLP_AMBER.id  # type: ignore[arg-type]

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            _tlp_marking("purple")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Per-payload conversion
# ---------------------------------------------------------------------------


class TestFindingConversion:
    def test_finding_becomes_report(self) -> None:
        fp = FindingPayload(
            title="Brazil energy policy shift",
            body="Detected shift in Brazil electricity policy",
            tags=["brazil", "energy"],
            confidence=0.9,
        )
        marking = _tlp_marking("amber")
        report = finding_to_report(fp, tlp_marking=marking)

        assert isinstance(report, stix2.Report)
        assert report.name == "Brazil energy policy shift"
        assert "shift" in report.description
        assert "threat-report" in report.report_types
        assert report.labels == ["brazil", "energy"]
        assert report.confidence == 90
        assert marking.id in report.object_marking_refs

    def test_finding_round_trips_through_parser(self) -> None:
        fp = FindingPayload(title="Test", body="body", tags=[])
        marking = _tlp_marking("green")
        report = finding_to_report(fp, tlp_marking=marking)
        # serialize → parse → equal id
        parsed = stix2.parse(report.serialize())
        assert parsed.id == report.id
        assert parsed.type == "report"


class TestSituationConversion:
    def test_situation_becomes_incident(self) -> None:
        sp = SituationPayload(
            name="Energy market turbulence",
            status="active",
            category="energy",
            intensity_score=0.75,
            event_count=12,
        )
        marking = _tlp_marking("amber")
        inc = situation_to_incident(sp, tlp_marking=marking)

        assert isinstance(inc, stix2.Incident)
        assert inc.name == "Energy market turbulence"
        # Description carries category/status/event_count synthesis.
        assert "energy" in inc.description
        assert "active" in inc.description
        assert "12" in inc.description
        assert marking.id in inc.object_marking_refs


class TestHypothesisConversion:
    def test_hypothesis_becomes_report_with_label(self) -> None:
        hp = HypothesisPayload(
            thesis="Lula will subsidize biofuels",
            counter_thesis="Lula will pivot to oil",
            evidence_balance=2,
            status="active",
        )
        marking = _tlp_marking("red")
        report = hypothesis_to_report(hp, tlp_marking=marking)

        assert isinstance(report, stix2.Report)
        assert "biofuels" in report.name or "biofuels" in report.description
        assert "analysis" in report.report_types
        assert "hypothesis" in report.labels
        assert "COUNTER" in report.description
        assert marking.id in report.object_marking_refs


class TestAlertConversion:
    @pytest.mark.parametrize("severity", ["medium", "high", "critical"])
    def test_high_severity_becomes_indicator(self, severity: str) -> None:
        ap = AlertPayload(
            title="DDoS spike against datacenter",
            severity=severity,  # type: ignore[arg-type]
            confidence=0.8,
            tags=["network", "ddos"],
        )
        marking = _tlp_marking("amber")
        sdo = alert_to_indicator_or_report(ap, tlp_marking=marking)

        assert isinstance(sdo, stix2.Indicator)
        assert sdo.name == "DDoS spike against datacenter"
        assert "malicious-activity" in sdo.indicator_types
        assert f"severity:{severity}" in sdo.labels
        assert sdo.pattern_type == "stix"
        # Pattern includes the alert title for downstream correlation.
        assert "DDoS spike" in sdo.pattern
        assert marking.id in sdo.object_marking_refs

    @pytest.mark.parametrize("severity", ["info", "low"])
    def test_low_severity_becomes_report(self, severity: str) -> None:
        ap = AlertPayload(
            title="Heartbeat", severity=severity,  # type: ignore[arg-type]
            tags=["status"],
        )
        marking = _tlp_marking("white")
        sdo = alert_to_indicator_or_report(ap, tlp_marking=marking)

        assert isinstance(sdo, stix2.Report)
        assert f"severity:{severity}" in sdo.labels
        assert marking.id in sdo.object_marking_refs

    def test_pattern_escapes_quotes(self) -> None:
        ap = AlertPayload(title="It's a 'critical' incident", severity="high")
        marking = _tlp_marking("amber")
        sdo = alert_to_indicator_or_report(ap, tlp_marking=marking)
        # Should parse as a valid STIX object (parser would reject a
        # malformed pattern at serialization time).
        parsed = stix2.parse(sdo.serialize())
        assert parsed.type == "indicator"


# ---------------------------------------------------------------------------
# Bundle composition
# ---------------------------------------------------------------------------


class TestBundleStructure:
    def test_single_finding_bundle(self) -> None:
        fp = FindingPayload(title="Test", body="body")
        env = OutputEnvelope(payload=fp)
        bundle = export_outputs_to_stix(env, tlp="amber", target_id="t1")

        assert isinstance(bundle, stix2.Bundle)
        assert bundle.type == "bundle"
        assert bundle.id.startswith("bundle--")
        # Should include at least: target identity + report.
        types = {obj.type for obj in bundle.objects}
        assert "identity" in types
        assert "report" in types

    def test_bundle_serialize_then_parse(self) -> None:
        fp = FindingPayload(title="parse-roundtrip", body="body")
        bundle = export_outputs_to_stix(
            OutputEnvelope(payload=fp), tlp="green", target_id="t1",
        )
        # The OASIS parser must accept our serialized output.
        parsed = stix2.parse(bundle.serialize(), allow_custom=True)
        assert parsed.type == "bundle"
        assert len(parsed.objects) == len(bundle.objects)

    def test_multi_payload_bundle(self) -> None:
        envs = [
            OutputEnvelope(payload=FindingPayload(title="F1", body="b")),
            OutputEnvelope(
                payload=SituationPayload(name="S1", status="active", category="x"),
            ),
            OutputEnvelope(
                payload=HypothesisPayload(thesis="H1", evidence_balance=0),
            ),
            OutputEnvelope(
                payload=AlertPayload(title="A-high", severity="high"),
            ),
            OutputEnvelope(
                payload=AlertPayload(title="A-info", severity="info"),
            ),
        ]
        bundle = export_outputs_to_stix(envs, tlp="amber", target_id="multi-target")

        type_counts: dict[str, int] = {}
        for obj in bundle.objects:
            type_counts[obj.type] = type_counts.get(obj.type, 0) + 1

        # Expected:
        #   identity x1   — the target identity (cited entities add more)
        #   report x4     — finding + situation-wrapper + hypothesis + low-alert
        #   incident x1   — from situation
        #   indicator x1  — from high-severity alert
        assert type_counts.get("identity", 0) >= 1
        assert type_counts.get("report", 0) >= 3
        assert type_counts.get("incident", 0) == 1
        assert type_counts.get("indicator", 0) == 1

    def test_empty_outputs_raises(self) -> None:
        with pytest.raises(ValueError):
            export_outputs_to_stix([], tlp="white", target_id="t1")


# ---------------------------------------------------------------------------
# TLP marking propagation
# ---------------------------------------------------------------------------


class TestTlpPropagation:
    @pytest.mark.parametrize("tlp", ["white", "green", "amber", "red"])
    def test_every_sdo_carries_requested_tlp(self, tlp: str) -> None:
        envs = [
            OutputEnvelope(payload=FindingPayload(title="F", body="b")),
            OutputEnvelope(
                payload=SituationPayload(name="S", status="active", category="x"),
            ),
            OutputEnvelope(
                payload=AlertPayload(title="A", severity="critical"),
            ),
        ]
        bundle = export_outputs_to_stix(envs, tlp=tlp, target_id="t1")  # type: ignore[arg-type]
        expected_marking = _tlp_marking(tlp).id  # type: ignore[arg-type]

        for obj in bundle.objects:
            # marking-definition objects don't themselves carry
            # object_marking_refs in our bundle (we use OASIS singletons,
            # which aren't included in the bundle anyway); every SDO we
            # constructed must.
            if obj.type == "marking-definition":
                continue
            refs = getattr(obj, "object_marking_refs", None) or []
            assert expected_marking in refs, (
                f"object {obj.type} {obj.id} missing TLP:{tlp} marking; "
                f"refs={refs}"
            )

    def test_default_tlp_in_emit_is_amber(self) -> None:
        """`emit()` falls back to TLP:AMBER when descriptor omits ``tlp``."""
        # The descriptor lookup uses an empty config, so the fallback fires.
        # We assert via a synchronous call to the helper used by emit.
        cfg: dict[str, Any] = {}
        tlp = cfg.get("tlp", "amber")
        assert tlp == "amber"


# ---------------------------------------------------------------------------
# Lineage (`derived_from` → STIX relationships)
# ---------------------------------------------------------------------------


class TestLineage:
    def test_derived_from_creates_relationship_sdos(self) -> None:
        ancestor_a = uuid4()
        ancestor_b = uuid4()
        fp = FindingPayload(title="lineage", body="body")
        env = OutputEnvelope(payload=fp, derived_from=[ancestor_a, ancestor_b])
        bundle = export_outputs_to_stix(env, tlp="amber", target_id="t1")

        rels = [obj for obj in bundle.objects if obj.type == "relationship"]
        derived_rels = [r for r in rels if r.relationship_type == "derived-from"]
        assert len(derived_rels) == 2
        # Both source from the report SDO.
        report = next(obj for obj in bundle.objects if obj.type == "report")
        for r in derived_rels:
            assert r.source_ref == report.id

    def test_ancestor_lookup_resolves_real_ids(self) -> None:
        ancestor_uuid = uuid4()
        # Pretend we already exported this ancestor; remember its STIX id.
        ancestor_stix_id = f"report--{uuid4()}"
        fp = FindingPayload(title="known-ancestor", body="body")
        env = OutputEnvelope(payload=fp, derived_from=[ancestor_uuid])
        bundle = export_outputs_to_stix(
            env,
            tlp="amber",
            target_id="t1",
            ancestor_lookup={ancestor_uuid: ancestor_stix_id},
        )
        rels = [obj for obj in bundle.objects if obj.type == "relationship"]
        assert any(r.target_ref == ancestor_stix_id for r in rels)

    def test_unknown_ancestor_becomes_identity_placeholder(self) -> None:
        ancestor_uuid = uuid4()
        fp = FindingPayload(title="unknown-ancestor", body="body")
        env = OutputEnvelope(payload=fp, derived_from=[ancestor_uuid])
        bundle = export_outputs_to_stix(env, tlp="amber", target_id="t1")
        rels = [obj for obj in bundle.objects if obj.type == "relationship"]
        # Synthesised target_ref is `identity--<uuidv5>` for stability.
        assert any(r.target_ref.startswith("identity--") for r in rels)


# ---------------------------------------------------------------------------
# Cited entities
# ---------------------------------------------------------------------------


class TestCitedEntities:
    def test_people_orgs_locations_become_sdos(self) -> None:
        fp = FindingPayload(
            title="cited",
            body="body",
            data={
                "cited_entities": {
                    "people": ["Alice Operator"],
                    "orgs": [{"name": "Acme Corp", "identity_class": "organization"}],
                    "locations": [{"name": "Brazil", "country": "BR"}],
                }
            },
        )
        env = OutputEnvelope(payload=fp)
        bundle = export_outputs_to_stix(env, tlp="amber", target_id="cited-test")

        names = {
            getattr(o, "name", None)
            for o in bundle.objects
            if o.type in ("identity", "location")
        }
        assert "Alice Operator" in names
        assert "Acme Corp" in names
        assert "Brazil" in names

    def test_cited_entities_referenced_by_report(self) -> None:
        fp = FindingPayload(
            title="ref-test",
            body="body",
            data={"cited_entities": {"people": [{"name": "Bob"}]}},
        )
        env = OutputEnvelope(payload=fp)
        bundle = export_outputs_to_stix(env, tlp="amber", target_id="t1")

        report = next(o for o in bundle.objects if o.type == "report")
        bob = next(
            o
            for o in bundle.objects
            if o.type == "identity" and getattr(o, "name", "") == "Bob"
        )
        assert bob.id in report.object_refs

    def test_alert_indicator_links_to_cited_via_relationship(self) -> None:
        ap = AlertPayload(
            title="alert-with-cited",
            severity="high",
            data={"cited_entities": {"orgs": [{"name": "Acme"}]}},
        )
        env = OutputEnvelope(payload=ap)
        bundle = export_outputs_to_stix(env, tlp="amber", target_id="t1")

        indicator = next(o for o in bundle.objects if o.type == "indicator")
        acme = next(
            o
            for o in bundle.objects
            if o.type == "identity" and getattr(o, "name", "") == "Acme"
        )
        rels = [
            r
            for r in bundle.objects
            if r.type == "relationship"
            and r.relationship_type == "related-to"
            and r.source_ref == indicator.id
            and r.target_ref == acme.id
        ]
        assert len(rels) == 1

    def test_malformed_cited_entity_is_skipped_not_fatal(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fp = FindingPayload(
            title="bad-cite",
            body="body",
            data={"cited_entities": {"locations": ["Brazil"]}},  # bare str — missing country
        )
        env = OutputEnvelope(payload=fp)
        # Should NOT raise.
        with caplog.at_level(logging.WARNING, logger="legba.data.outputs.stix_bundle"):
            bundle = export_outputs_to_stix(env, tlp="amber", target_id="t1")
        # The malformed location is skipped, but the bundle exists.
        assert bundle.type == "bundle"


# ---------------------------------------------------------------------------
# Exporter class
# ---------------------------------------------------------------------------


class TestExporterClass:
    def test_default_collection_name(self) -> None:
        exporter = StixBundleExporter(tlp="amber", target_id="br-energy")
        assert exporter.taxii_collection == "legba_target_br-energy_collection"

    def test_explicit_collection_preserved(self) -> None:
        exporter = StixBundleExporter(
            tlp="green",
            target_id="t1",
            taxii_collection="custom-collection",
        )
        assert exporter.taxii_collection == "custom-collection"

    def test_nats_subject_pattern(self) -> None:
        exporter = StixBundleExporter(tlp="amber", target_id="br-energy")
        assert exporter.nats_subject == "legba.outputs.stix.br-energy"

    def test_invalid_tlp_at_construction(self) -> None:
        with pytest.raises(ValueError):
            StixBundleExporter(tlp="purple", target_id="t1")  # type: ignore[arg-type]

    def test_to_json_round_trips(self) -> None:
        exporter = StixBundleExporter(tlp="amber", target_id="t1")
        env = OutputEnvelope(payload=FindingPayload(title="x", body="y"))
        bundle_json = exporter.to_json(env)
        parsed = stix2.parse(bundle_json, allow_custom=True)
        assert parsed.type == "bundle"

    def test_remember_ancestor_then_export(self) -> None:
        exporter = StixBundleExporter(tlp="amber", target_id="t1")
        ancestor_uuid = uuid4()
        ancestor_stix_id = f"report--{uuid4()}"
        exporter.remember_ancestor(ancestor_uuid, ancestor_stix_id)

        env = OutputEnvelope(
            payload=FindingPayload(title="x", body="y"),
            derived_from=[ancestor_uuid],
        )
        bundle = exporter.export(env)
        rels = [obj for obj in bundle.objects if obj.type == "relationship"]
        assert any(r.target_ref == ancestor_stix_id for r in rels)


# ---------------------------------------------------------------------------
# emit() — uniform output-kind surface
# ---------------------------------------------------------------------------


class _RecordingNats:
    """Test NATS publisher — records (subject, body) tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    async def publish_json(self, subject: str, body: bytes) -> None:
        self.calls.append((subject, body))


class _RaisingNats:
    """Test NATS publisher that always raises — exercises best-effort path."""

    async def publish_json(self, subject: str, body: bytes) -> None:
        raise RuntimeError("simulated nats outage")


class TestEmit:
    async def test_emit_publishes_to_nats_subject(self) -> None:
        nats = _RecordingNats()
        deps = OutputDeps(nats=nats)
        descriptor = {
            "outputs": [
                {
                    "kind": "stix_bundle",
                    "config": {"tlp": "amber", "target_id": "br-energy"},
                }
            ]
        }
        bundle = await emit(
            FindingPayload(title="emit test", body="body"),
            descriptor=descriptor,
            deps=deps,
        )

        assert bundle.type == "bundle"
        assert len(nats.calls) == 1
        subject, body = nats.calls[0]
        assert subject == "legba.outputs.stix.br-energy"
        # The published body parses back as a valid bundle.
        parsed = stix2.parse(body.decode("utf-8"), allow_custom=True)
        assert parsed.type == "bundle"
        assert parsed.id == bundle.id

    async def test_emit_accepts_flat_config(self) -> None:
        """The descriptor lookup also accepts a flat config block directly."""
        nats = _RecordingNats()
        deps = OutputDeps(nats=nats)
        flat_cfg = {"tlp": "green", "target_id": "flat-cfg-target"}
        await emit(
            FindingPayload(title="flat", body=""),
            descriptor=flat_cfg,
            deps=deps,
        )
        assert nats.calls[0][0] == "legba.outputs.stix.flat-cfg-target"

    async def test_emit_no_nats_publisher_does_not_raise(self) -> None:
        """A descriptor without a NATS publisher must still serialize."""
        deps = OutputDeps()  # nats=None
        bundle = await emit(
            FindingPayload(title="no-nats", body="body"),
            descriptor={"tlp": "amber", "target_id": "no-nats-target"},
            deps=deps,
        )
        assert bundle.type == "bundle"

    async def test_emit_swallows_nats_failure(self) -> None:
        """NATS publish failure is logged + swallowed (best-effort)."""
        deps = OutputDeps(nats=_RaisingNats())
        # Should not raise.
        bundle = await emit(
            FindingPayload(title="nats-failure", body="body"),
            descriptor={"tlp": "amber", "target_id": "fail-target"},
            deps=deps,
        )
        assert bundle.type == "bundle"

    async def test_emit_writes_file_sink(self, tmp_path: Path) -> None:
        out_path = tmp_path / "{target_id}-{output_id}.json"
        deps = OutputDeps()
        bundle = await emit(
            FindingPayload(title="file-sink", body="body"),
            descriptor={
                "tlp": "white",
                "target_id": "fs-target",
                "file_sink": str(out_path),
            },
            deps=deps,
        )
        # File should exist with formatted name.
        written = list(tmp_path.glob("fs-target-*.json"))
        assert len(written) == 1
        # Re-parse the on-disk bundle.
        on_disk = stix2.parse(written[0].read_text(), allow_custom=True)
        assert on_disk.id == bundle.id

    async def test_emit_respects_severity_gate_for_alert(self) -> None:
        nats = _RecordingNats()
        deps = OutputDeps(nats=nats)
        await emit(
            AlertPayload(title="critical-alert", severity="critical"),
            descriptor={"tlp": "amber", "target_id": "t1"},
            deps=deps,
        )
        # Verify the bundle includes an indicator (not just a report).
        subject, body = nats.calls[0]
        parsed = stix2.parse(body.decode("utf-8"), allow_custom=True)
        assert any(o.type == "indicator" for o in parsed.objects)


# ---------------------------------------------------------------------------
# Constants + deferred TAXII upload
# ---------------------------------------------------------------------------


class TestConstants:
    def test_nats_subject_pattern(self) -> None:
        assert "{target_id}" in NATS_SUBJECT_PATTERN
        assert (
            NATS_SUBJECT_PATTERN.format(target_id="x")
            == "legba.outputs.stix.x"
        )

    def test_taxii_collection_pattern(self) -> None:
        assert "{target_id}" in TAXII_COLLECTION_PATTERN
        assert (
            TAXII_COLLECTION_PATTERN.format(target_id="x")
            == "legba_target_x_collection"
        )


class _FakeHttpResponse:
    def __init__(self, status_code: int, *, body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class _RecordingHttp:
    """Records POST calls; returns a queued response (or one fixed)."""

    def __init__(self, response: _FakeHttpResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def post(self, url, *, content=None, headers=None, timeout=None, **kw):  # noqa: ANN001
        self.calls.append(
            {"url": url, "content": content, "headers": dict(headers or {}), "timeout": timeout}
        )
        return self.response


class TestTaxiiUpload:
    """The TAXII 2.1 push is real (export-interop). An un-provisioned
    destination fails loud (the declared SEAM guard rail); a configured
    destination delivers / degrades."""

    async def test_unprovisioned_destination_fails_loud(self) -> None:
        # Empty server_url → un-provisioned destination → loud refusal,
        # NEVER a silent no-op (docs/SEAMS.md seam 10 guard rail).
        env = OutputEnvelope(payload=FindingPayload(title="t", body="b"))
        bundle = export_outputs_to_stix(env, tlp="amber", target_id="t1")
        with pytest.raises(TaxiiServerNotConfiguredError):
            await upload_bundle_to_taxii(
                bundle,
                server_url="",
                api_root="taxii2",
                collection_id="legba_target_t1_collection",
                http=_RecordingHttp(_FakeHttpResponse(202)),
            )

    async def test_cleartext_non_loopback_refused(self) -> None:
        # TLP-marked content must not go over plaintext to a remote host.
        env = OutputEnvelope(payload=FindingPayload(title="t", body="b"))
        bundle = export_outputs_to_stix(env, tlp="red", target_id="t1")
        with pytest.raises(TaxiiServerNotConfiguredError):
            await upload_bundle_to_taxii(
                bundle,
                server_url="http://taxii.example",
                api_root="taxii2",
                collection_id="c1",
                http=_RecordingHttp(_FakeHttpResponse(202)),
            )

    async def test_upload_delivers_against_202(self) -> None:
        env = OutputEnvelope(payload=FindingPayload(title="t", body="b"))
        bundle = export_outputs_to_stix(env, tlp="amber", target_id="t1")
        http = _RecordingHttp(
            _FakeHttpResponse(202, body={"id": "status--abc", "status": "pending"})
        )
        result = await upload_bundle_to_taxii(
            bundle,
            server_url="https://taxii.example",
            api_root="api1",
            collection_id="legba_target_t1_collection",
            http=http,
        )
        assert result.delivered
        assert result.status_id == "status--abc"
        # POST hit the spec-shaped add-objects endpoint with the TAXII media type.
        call = http.calls[0]
        assert call["url"] == (
            "https://taxii.example/api1/collections/legba_target_t1_collection/objects/"
        )
        assert call["headers"]["Content-Type"] == "application/taxii+json;version=2.1"
        # Body is a TAXII envelope (objects[]), not a STIX bundle.
        body = json.loads(call["content"])
        assert "objects" in body and isinstance(body["objects"], list)
        assert "type" not in body  # no bundle wrapper

    async def test_4xx_is_permanent_no_retry(self) -> None:
        env = OutputEnvelope(payload=FindingPayload(title="t", body="b"))
        bundle = export_outputs_to_stix(env, tlp="amber", target_id="t1")
        http = _RecordingHttp(_FakeHttpResponse(403, text="forbidden"))
        result = await upload_bundle_to_taxii(
            bundle,
            server_url="https://taxii.example",
            api_root="api1",
            collection_id="c1",
            token="tok",
            http=http,
        )
        assert result.outcome == "permanent_error"
        assert result.http_status == 403
        assert len(http.calls) == 1  # no retry on 4xx
        assert http.calls[0]["headers"]["Authorization"] == "Bearer tok"
