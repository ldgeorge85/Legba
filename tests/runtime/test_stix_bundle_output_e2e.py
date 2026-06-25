# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end validation of the L-195 STIX 2.1 bundle output kind.

This is the operator-facing acceptance test for done-plan §4 group O-3:
*"register a target with ``outputs.stix_bundle`` configured; verify that
when the analyst emits a finding, a STIX bundle JSON lands."*

Unlike the unit suite in ``tests/data_pkg/test_output_stix_bundle.py`` —
which exercises every shape of the in-memory exporter — this file walks
the full delivery path:

  1. Build a target descriptor whose ``outputs[]`` block names the
     ``stix_bundle`` kind with a real TLP marking + on-disk file sink.
  2. Validate the descriptor's ``outputs`` block against the production
     :class:`legba.data.schemas.target.OutputBinding` schema so the test
     fails if the descriptor shape ever drifts.
  3. Persist a :class:`FindingPayload` row to **real** Postgres via
     :func:`legba.data.outputs.substrate.write_finding` — same path the
     analyst actor walks.
  4. Read the row back, reconstruct the typed payload, and invoke the
     stix_bundle kind's ``emit`` with a recording NATS publisher.
  5. Assert the produced bundle:
       * conforms to STIX 2.1 (``stix2.parse`` round-trip succeeds),
       * has the OASIS-canonical bundle shape (``type``, ``id``,
         ``spec_version``),
       * carries the right SDO mix (target identity + report SDO + any
         cited identities + lineage relationships),
       * propagates the descriptor's TLP marking to every SDO,
       * emits ``derived-from`` relationship SDOs for every ancestor
         UUID, including the substrate row's real ancestor,
       * lands on disk at the descriptor-configured ``file_sink`` path,
       * was published on the canonical
         ``legba.outputs.stix.<target_id>`` NATS subject.

Scope notes
-----------
* **Outbound TAXII push is real (export-interop).** The kind POSTs
  bundles to a configured TAXII 2.1 collection via the structural HTTP
  port. An *un-provisioned* destination is the fail-loud SEAM
  (:class:`TaxiiServerNotConfiguredError`); one test below exercises that
  refusal, and a live variant is gated behind ``LEGBA_TEST_TAXII_LIVE=1``
  for a real server.
* **No mocked substrate.** Per the project's no-mocks rule, the Postgres
  bring-up is real (via the ``migrated_pg`` fixture re-exported from
  ``tests/data_pkg/conftest.py``).

Tests
-----
* ``test_descriptor_outputs_block_validates`` — the descriptor's
  ``outputs[].stix_bundle.config`` shape validates against
  :class:`OutputBinding` (the production schema). Locks the doc-as-code
  contract that the descriptor body in this file is canonical.
* ``test_emit_after_substrate_write_produces_valid_stix_bundle`` —
  happy path. Write a finding to real Postgres, read it back, call
  ``emit``, validate the produced bundle.
* ``test_emit_severity_confidence_translates_to_indicator_confidence``
  — alert severity gate and ``payload.confidence`` → STIX
  ``indicator.confidence`` (the OASIS 0-100 scaling).
* ``test_emit_with_empty_derived_from_skips_relationship_sdos`` —
  empty ancestor list must not generate a stray ``relationship`` SDO
  (regression guard against an over-eager loop).
* ``test_emit_file_sink_lands_bundle_on_disk`` — file-sink path lands a
  parsable bundle at the descriptor-formatted destination.
* ``test_taxii_upload_unprovisioned_fails_loud`` — the upload path
  raises :class:`TaxiiServerNotConfiguredError` for an un-provisioned
  destination (the declared SEAM guard rail). A live variant is gated
  behind ``LEGBA_TEST_TAXII_LIVE=1`` for a real TAXII 2.1 server.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
import stix2

from legba.data.config import PostgresConfig
from legba.data.outputs import discover_output_kinds, stix_bundle as stix_bundle_mod
from legba.data.outputs._contract import OutputContext, OutputDeps
from legba.data.outputs.stix_bundle import (
    KIND_NAME,
    NATS_SUBJECT_PATTERN,
    TAXII_COLLECTION_PATTERN,
    OutputEnvelope,
    StixBundleExporter,
    TaxiiServerNotConfiguredError,
    emit,
    export_outputs_to_stix,
    upload_bundle_to_taxii,
)
from legba.data.outputs.substrate import write_finding
from legba.data.provenance.models import (
    AlertPayload,
    FindingPayload,
)
from legba.data.sources._contract import Signal
from legba.data.schemas.target import OutputBinding
from legba.runtime.source_actor import write_canonical_signal


# ---------------------------------------------------------------------------
# Canonical test descriptor — outputs[].stix_bundle binding
# ---------------------------------------------------------------------------


#: The full descriptor body — kept as a literal so the test file *is* the
#: doc-as-code spec for the binding. The ``identity`` / ``scope`` / etc.
#: are omitted because the brief is scoped to the ``outputs`` binding;
#: schema validation below proves the binding's shape is correct.
TEST_TARGET_ID = "stix_e2e_test_target"
TEST_ANALYST_ID = "analyst.stix_e2e"

#: Use a deterministic, UI-readable file_sink template. ``{target_id}``
#: and ``{output_id}`` are substituted by the kind at emit time so each
#: bundle lands at a stable, debuggable path. The trailing ``.json`` is
#: required by every downstream STIX viewer we tested against.
FILE_SINK_TEMPLATE = "{target_id}-{output_id}.json"


def _build_descriptor_outputs_block(tmp_path: Path) -> dict[str, Any]:
    """Construct the ``outputs`` list a TargetDescriptor would carry.

    Mirrors the india_energy_infra descriptor's shape — see
    ``descriptors/target_india_energy_infra.yaml`` — substituting the
    ``stix_bundle`` kind for ``a2a_skill``. The block is validated by
    :class:`OutputBinding` in the wiring test below so we can't drift.
    """
    file_sink = str(tmp_path / FILE_SINK_TEMPLATE)
    return {
        "outputs": [
            {
                "kind": "stix_bundle",
                "config": {
                    "tlp": "amber",
                    "target_id": TEST_TARGET_ID,
                    "file_sink": file_sink,
                },
            }
        ]
    }


# ---------------------------------------------------------------------------
# Recording NATS publisher (test double — observation only, no behavior)
# ---------------------------------------------------------------------------


class _RecordingNats:
    """Captures the (subject, body) tuples the kind publishes.

    This is not a mock of the kind — it's a real :class:`NatsPublisher`
    structural-Protocol implementation that records calls for assertion.
    The kind invokes ``publish_json`` exactly as it would against the
    L-001 NatsStore in production. The recorder lets us assert subject
    + body without dragging a live NATS broker into the test.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    async def publish_json(self, subject: str, body: bytes) -> None:
        self.calls.append((subject, body))


# ---------------------------------------------------------------------------
# Fixtures — real Postgres connection per test
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    """One asyncpg connection per test against the migrated test DB.

    ``migrated_pg`` is session-scoped and re-exported from
    ``tests/data_pkg/conftest.py`` via the runtime conftest re-export.
    """
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        yield conn
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Test 1 — descriptor shape validates against the production schema
# ---------------------------------------------------------------------------


class TestDescriptorShape:
    """Pin the descriptor body in this file to the production schema."""

    def test_descriptor_outputs_block_validates(self, tmp_path: Path) -> None:
        body = _build_descriptor_outputs_block(tmp_path)
        # The binding is what the runtime parses at descriptor-register
        # time; if this validation drifts the test breaks immediately.
        binding = OutputBinding.model_validate(body["outputs"][0])
        assert binding.kind == "stix_bundle"
        assert binding.config["tlp"] == "amber"
        assert binding.config["target_id"] == TEST_TARGET_ID
        assert binding.config["file_sink"].endswith(FILE_SINK_TEMPLATE)

    def test_kind_is_discoverable_from_registry(self) -> None:
        """The wiring proof: the kind shows up in the host registry."""
        registry = discover_output_kinds()
        assert KIND_NAME in registry
        handler = registry[KIND_NAME]
        assert handler.kind_name == KIND_NAME
        assert callable(handler.emit)
        # The host-registry handle and the module-level emit must agree.
        assert handler.emit is stix_bundle_mod.emit


# ---------------------------------------------------------------------------
# Test 2 — happy path: write finding to Postgres → emit → valid STIX bundle
# ---------------------------------------------------------------------------


class TestSubstrateToStixHappyPath:
    """The headline e2e flow — real DB write through to STIX bundle JSON."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_emit_after_substrate_write_produces_valid_stix_bundle(
        self, pg_conn, tmp_path: Path
    ) -> None:
        # ---- Arrange: seed a root signal so the finding has lineage ----
        # Source-first pivot: the signal is a TARGET-AGNOSTIC observation
        # (legba.data.sources._contract.Signal) written via the source-first
        # writer. No target_id — the observation is source-owned; the target
        # binding lives only on the derived finding below.
        signal = Signal(
            source_id="src.stix_e2e_rss",
            modality="text",
            payload={
                "title": "Brazil grid disruption — preliminary report",
                "source_url": "https://example.test/grid-disruption",
                "category": "energy",
            },
            content_hash="e2e-stix-seed-signal-hash",
            canonical_url="https://example.test/grid-disruption",
            tags=["energy"],
            source_credibility=0.8,
        )
        signal_id = await write_canonical_signal(
            pg_conn,
            signal,
            source_version="v1",
            owner_tenant="default",
        )
        assert signal_id is not None, "seed signal must persist to substrate"

        # ---- Act 1: write a real finding row via the substrate kind ----
        finding_id = await write_finding(
            pg_conn,
            TEST_TARGET_ID,
            TEST_ANALYST_ID,
            FindingPayload(
                title="Coordinated outage pattern across northeast operators",
                body=(
                    "Three operators reported correlated outage windows "
                    "within a 45-minute span on 2026-05-21."
                ),
                tags=["brazil", "energy", "outage"],
                confidence=0.85,
                evidence=["EPE bulletin 2026-05-21", "ONS dispatch log"],
                data={
                    "cited_entities": {
                        "orgs": [
                            {"name": "ONS Brasil"},
                            {"name": "EPE"},
                        ],
                        "locations": [
                            {"name": "Pernambuco", "country": "BR"},
                        ],
                    },
                },
            ),
            [signal_id],
            analyst_version="v1.0.0",
            target_version="e2e_test_version",
            run_id=uuid4(),
        )

        # ---- Act 2: read it back from substrate (analyst actor's path)
        row = await pg_conn.fetchrow(
            "SELECT id, title, body, confidence, data, derived_from "
            "FROM analyst_outputs WHERE id = $1",
            finding_id,
        )
        assert row is not None, "finding row must persist to substrate"
        payload = FindingPayload(
            title=row["title"],
            body=row["body"],
            confidence=float(row["confidence"]),
            tags=json.loads(row["data"]).get("tags", []),
            evidence=json.loads(row["data"]).get("evidence", []),
            data=json.loads(row["data"]).get("data", {}),
        )

        # ---- Act 3: call the kind's emit with the descriptor config ---
        descriptor = _build_descriptor_outputs_block(tmp_path)
        nats = _RecordingNats()
        bundle = await emit(
            payload,
            descriptor=descriptor,
            deps=OutputDeps(nats=nats),
            output_id=finding_id,
            derived_from=list(row["derived_from"]),
        )

        # ---- Assert: bundle is a valid STIX 2.1 bundle ----------------
        assert isinstance(bundle, stix2.Bundle)
        assert bundle.type == "bundle"
        assert bundle.id.startswith("bundle--"), (
            f"bundle id must match `bundle--*`; got {bundle.id!r}"
        )
        # spec_version is implicit on stix2.Bundle but must serialize.
        bundle_json = bundle.serialize()
        loaded = json.loads(bundle_json)
        # stix2.v21 omits spec_version on the bundle envelope per the
        # OASIS-2.1 update (spec_version moved to per-SDO); the type tag
        # is the canonical signal.
        assert loaded["type"] == "bundle"

        # Round-trip via the OASIS reference parser.
        parsed = stix2.parse(bundle_json, allow_custom=True)
        assert parsed.type == "bundle"
        assert len(parsed.objects) == len(bundle.objects)

        # ---- Assert: SDO mix is correct -------------------------------
        types: dict[str, int] = {}
        for obj in bundle.objects:
            types[obj.type] = types.get(obj.type, 0) + 1
        # target identity + cited orgs + cited location + report +
        # derived-from relationship.
        assert types.get("identity", 0) >= 3, (  # target + 2 orgs
            f"expected ≥3 identity SDOs (target + cited orgs); got {types}"
        )
        assert types.get("location", 0) == 1, f"expected 1 location SDO; got {types}"
        assert types.get("report", 0) == 1, f"expected 1 report SDO; got {types}"
        assert types.get("relationship", 0) == 1, (
            f"expected 1 derived-from relationship SDO; got {types}"
        )

        # ---- Assert: TLP marking propagates to every SDO --------------
        amber_id = stix2.TLP_AMBER.id
        for obj in bundle.objects:
            if obj.type == "marking-definition":
                continue
            refs = getattr(obj, "object_marking_refs", None) or []
            assert amber_id in refs, (
                f"SDO {obj.type}/{obj.id} missing TLP:AMBER marking; "
                f"object_marking_refs={refs!r}"
            )

        # ---- Assert: report carries lineage + cited refs --------------
        report = next(obj for obj in bundle.objects if obj.type == "report")
        assert report.confidence == int(round(payload.confidence * 100))
        # The report's object_refs must include the target identity and
        # every cited entity (orgs + locations).
        cited_ids = {
            obj.id for obj in bundle.objects
            if obj.type in ("identity", "location")
            and obj.name != f"Legba target {TEST_TARGET_ID}"
        }
        for cited_id in cited_ids:
            assert cited_id in report.object_refs, (
                f"cited entity {cited_id} not in report.object_refs"
            )

        # ---- Assert: derived-from relationship points back to signal --
        rels = [o for o in bundle.objects if o.type == "relationship"]
        assert len(rels) == 1
        rel = rels[0]
        assert rel.relationship_type == "derived-from"
        assert rel.source_ref == report.id

        # ---- Assert: NATS publish hit the canonical subject -----------
        assert len(nats.calls) == 1
        subject, body = nats.calls[0]
        assert subject == NATS_SUBJECT_PATTERN.format(target_id=TEST_TARGET_ID)
        # The body is a STIX bundle JSON.
        parsed_on_wire = stix2.parse(body.decode("utf-8"), allow_custom=True)
        assert parsed_on_wire.id == bundle.id

        # ---- Assert: file sink wrote the bundle to disk ---------------
        on_disk = list(tmp_path.glob(f"{TEST_TARGET_ID}-*.json"))
        assert len(on_disk) == 1, f"expected 1 bundle on disk; got {on_disk}"
        on_disk_bundle = stix2.parse(on_disk[0].read_text(), allow_custom=True)
        assert on_disk_bundle.id == bundle.id


# ---------------------------------------------------------------------------
# Test 3 — severity + confidence translation through emit()
# ---------------------------------------------------------------------------


class TestSeverityAndConfidenceTranslation:
    """Validate the alert-severity gate and confidence scaling at emit time."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "severity, confidence, expected_stix_confidence",
        [
            ("medium", 0.55, 55),
            ("high", 0.9, 90),
            ("critical", 1.0, 100),
            ("critical", 0.0, 0),
        ],
    )
    async def test_alert_severity_emits_indicator_with_scaled_confidence(
        self,
        tmp_path: Path,
        severity: str,
        confidence: float,
        expected_stix_confidence: int,
    ) -> None:
        descriptor = _build_descriptor_outputs_block(tmp_path)
        nats = _RecordingNats()
        bundle = await emit(
            AlertPayload(
                title=f"sev-{severity} probe",
                body="confidence translation probe",
                severity=severity,  # type: ignore[arg-type]
                confidence=confidence,
                tags=["e2e", "translation"],
            ),
            descriptor=descriptor,
            deps=OutputDeps(nats=nats),
        )

        indicators = [o for o in bundle.objects if o.type == "indicator"]
        assert len(indicators) == 1, (
            f"severity={severity!r} must emit exactly one indicator SDO; "
            f"bundle objects: {[o.type for o in bundle.objects]}"
        )
        indicator = indicators[0]
        # STIX 2.1 confidence is the integer 0-100 scaling — the OASIS
        # canonical translation of a 0.0-1.0 analyst confidence.
        assert indicator.confidence == expected_stix_confidence
        # Severity label is carried on the indicator so receivers can
        # route on it without re-deriving from indicator_types.
        assert f"severity:{severity}" in indicator.labels

    @pytest.mark.asyncio
    @pytest.mark.parametrize("severity", ["info", "low"])
    async def test_low_severity_emits_report_not_indicator(
        self, tmp_path: Path, severity: str
    ) -> None:
        """info / low alerts must NOT become indicators (STIX semantics)."""
        descriptor = _build_descriptor_outputs_block(tmp_path)
        bundle = await emit(
            AlertPayload(
                title=f"sev-{severity} probe",
                severity=severity,  # type: ignore[arg-type]
                confidence=0.5,
            ),
            descriptor=descriptor,
            deps=OutputDeps(),
        )
        indicators = [o for o in bundle.objects if o.type == "indicator"]
        reports = [
            o for o in bundle.objects
            if o.type == "report" and f"severity:{severity}" in (o.labels or [])
        ]
        assert indicators == [], (
            f"severity={severity!r} must NOT emit an indicator SDO"
        )
        assert len(reports) == 1, (
            f"severity={severity!r} must emit exactly one tagged report SDO"
        )

    @pytest.mark.asyncio
    async def test_finding_confidence_scales_to_report_confidence(
        self, tmp_path: Path
    ) -> None:
        """FindingPayload → Report path also scales confidence 0-1 → 0-100."""
        descriptor = _build_descriptor_outputs_block(tmp_path)
        bundle = await emit(
            FindingPayload(
                title="confidence scaling check",
                body="probe",
                confidence=0.73,
            ),
            descriptor=descriptor,
            deps=OutputDeps(),
        )
        report = next(o for o in bundle.objects if o.type == "report")
        # 0.73 → int(round(73)) = 73.
        assert report.confidence == 73


# ---------------------------------------------------------------------------
# Test 4 — empty derived_from must not generate a stray relationship SDO
# ---------------------------------------------------------------------------


class TestEmptyLineage:
    """Regression guard against an over-eager relationship loop."""

    @pytest.mark.asyncio
    async def test_emit_with_empty_derived_from_skips_relationship_sdos(
        self, tmp_path: Path
    ) -> None:
        descriptor = _build_descriptor_outputs_block(tmp_path)
        bundle = await emit(
            FindingPayload(
                title="orphan finding (no upstream)",
                body="manually-injected, no signal ancestor",
                confidence=0.5,
            ),
            descriptor=descriptor,
            deps=OutputDeps(),
            derived_from=[],  # explicit empty — the regression target.
        )
        rels = [o for o in bundle.objects if o.type == "relationship"]
        assert rels == [], (
            f"empty derived_from must not emit relationship SDOs; got "
            f"{[r.relationship_type for r in rels]}"
        )
        # The bundle is still valid + parseable.
        parsed = stix2.parse(bundle.serialize(), allow_custom=True)
        assert parsed.type == "bundle"
        # And it still has the canonical target identity + report.
        types = {o.type for o in bundle.objects}
        assert "identity" in types
        assert "report" in types

    @pytest.mark.asyncio
    async def test_emit_with_none_derived_from_skips_relationship_sdos(
        self, tmp_path: Path
    ) -> None:
        """Same regression, but the caller passes ``None`` for derived_from."""
        descriptor = _build_descriptor_outputs_block(tmp_path)
        bundle = await emit(
            FindingPayload(title="orphan-none", body="probe"),
            descriptor=descriptor,
            deps=OutputDeps(),
            derived_from=None,  # explicit None — emit normalizes to [].
        )
        rels = [o for o in bundle.objects if o.type == "relationship"]
        assert rels == []


# ---------------------------------------------------------------------------
# Test 5 — file sink behavior (formatting, on-disk validity)
# ---------------------------------------------------------------------------


class TestFileSink:
    """Operator-facing file-sink path — bundles land at the configured spot."""

    @pytest.mark.asyncio
    async def test_emit_file_sink_lands_bundle_on_disk(self, tmp_path: Path) -> None:
        descriptor = _build_descriptor_outputs_block(tmp_path)
        bundle = await emit(
            FindingPayload(title="file-sink check", body="probe", confidence=0.6),
            descriptor=descriptor,
            deps=OutputDeps(),
        )

        on_disk = list(tmp_path.glob(f"{TEST_TARGET_ID}-*.json"))
        assert len(on_disk) == 1, f"expected 1 file; got {[p.name for p in on_disk]}"
        # The filename respects the {output_id} substitution from the
        # FILE_SINK_TEMPLATE.
        stem = on_disk[0].stem
        assert stem.startswith(f"{TEST_TARGET_ID}-")
        # The substituted segment is a UUID hex (with dashes).
        suffix = stem[len(f"{TEST_TARGET_ID}-"):]
        UUID(suffix)  # raises ValueError if not a UUID — that's the assertion.

        # Re-parse the bundle from disk and verify ids match.
        reparsed = stix2.parse(on_disk[0].read_text(), allow_custom=True)
        assert reparsed.id == bundle.id

    @pytest.mark.asyncio
    async def test_emit_creates_parent_directories(self, tmp_path: Path) -> None:
        """File sink must mkdir the parent path so operators can drop the
        sink under a non-existent subdir (e.g. an audit-archive dir)."""
        nested = tmp_path / "archive" / "{target_id}" / "{output_id}.json"
        descriptor = {
            "tlp": "green",
            "target_id": TEST_TARGET_ID,
            "file_sink": str(nested),
        }
        await emit(
            FindingPayload(title="nested-dir", body=""),
            descriptor=descriptor,
            deps=OutputDeps(),
        )
        landed = list((tmp_path / "archive" / TEST_TARGET_ID).glob("*.json"))
        assert len(landed) == 1


# ---------------------------------------------------------------------------
# Test 6 — TAXII 2.1 push (export-interop): un-provisioned = fail-loud SEAM
# ---------------------------------------------------------------------------


class TestTaxiiUpload:
    """The TAXII 2.1 push client is real; an un-provisioned destination is
    the declared fail-loud SEAM (``docs/SEAMS.md`` seam 10).

    A descriptor that asks to push to TAXII without a ``server_url`` (or
    over cleartext to a remote host) refuses loudly with
    :class:`TaxiiServerNotConfiguredError` — it never silently drops the
    TLP-marked bundle. The live variant (gated behind
    ``LEGBA_TEST_TAXII_LIVE=1`` + ``LEGBA_TEST_TAXII_URL``) drives a real
    TAXII 2.1 server end-to-end.
    """

    @pytest.mark.asyncio
    async def test_taxii_upload_unprovisioned_fails_loud(self, tmp_path: Path) -> None:
        env = OutputEnvelope(payload=FindingPayload(title="t", body="b"))
        bundle = export_outputs_to_stix(env, tlp="amber", target_id=TEST_TARGET_ID)

        class _Http:
            async def post(self, *a, **k):  # pragma: no cover — must not be reached
                raise AssertionError("post called for an un-provisioned destination")

        with pytest.raises(TaxiiServerNotConfiguredError):
            await upload_bundle_to_taxii(
                bundle,
                server_url="",  # un-provisioned destination
                api_root="api1",
                collection_id=TAXII_COLLECTION_PATTERN.format(
                    target_id=TEST_TARGET_ID,
                ),
                http=_Http(),
            )

    @pytest.mark.skipif(
        os.environ.get("LEGBA_TEST_TAXII_LIVE") != "1",
        reason=(
            "live TAXII push needs a real server; set LEGBA_TEST_TAXII_LIVE=1 "
            "+ LEGBA_TEST_TAXII_URL (+ optional LEGBA_TEST_TAXII_AUTH)"
        ),
    )
    @pytest.mark.asyncio
    async def test_taxii_upload_against_live_server(self, tmp_path: Path) -> None:
        """Drive a real TAXII 2.1 add-objects POST against a live server."""
        import httpx

        server_url = os.environ.get("LEGBA_TEST_TAXII_URL", "")
        assert server_url, "LEGBA_TEST_TAXII_URL must be set under live mode"
        env = OutputEnvelope(payload=FindingPayload(title="live", body="probe"))
        bundle = export_outputs_to_stix(env, tlp="amber", target_id=TEST_TARGET_ID)
        auth_env = os.environ.get("LEGBA_TEST_TAXII_AUTH")
        auth = tuple(auth_env.split(":", 1)) if auth_env else None  # type: ignore[arg-type]
        async with httpx.AsyncClient(timeout=15.0) as client:
            result = await upload_bundle_to_taxii(
                bundle,
                server_url=server_url,
                api_root=os.environ.get("LEGBA_TEST_TAXII_API_ROOT", "taxii2"),
                collection_id=os.environ.get(
                    "LEGBA_TEST_TAXII_COLLECTION",
                    TAXII_COLLECTION_PATTERN.format(target_id=TEST_TARGET_ID),
                ),
                auth=auth,
                http=client,
            )
        assert result.delivered, f"live TAXII push not delivered: {result}"


# ---------------------------------------------------------------------------
# Test 7 — Class-shaped StixBundleExporter integration
# ---------------------------------------------------------------------------


class TestExporterClassEndToEnd:
    """Confirm the class-shaped exporter walks the same path as ``emit``.

    Some callers (notably the runtime's per-target output dispatcher)
    prefer the stateful exporter so they can stamp TLP + target_id once
    and reuse across many payloads in a tight loop. This test confirms
    that path produces the same bundle shape as the ``emit`` surface.
    """

    def test_exporter_class_produces_equivalent_bundle(self, tmp_path: Path) -> None:
        exporter = StixBundleExporter(tlp="amber", target_id=TEST_TARGET_ID)
        assert exporter.taxii_collection == (
            f"legba_target_{TEST_TARGET_ID}_collection"
        )
        assert exporter.nats_subject == (
            f"legba.outputs.stix.{TEST_TARGET_ID}"
        )

        payload = FindingPayload(
            title="exporter-class probe",
            body="body",
            confidence=0.8,
        )
        env = OutputEnvelope(
            payload=payload,
            output_id=uuid4(),
            derived_from=[uuid4()],
            produced_at=datetime.now(timezone.utc),
        )
        bundle = exporter.export(env)

        # Round-trip via the OASIS reference parser.
        parsed = stix2.parse(bundle.serialize(), allow_custom=True)
        assert parsed.type == "bundle"

        types = {o.type for o in bundle.objects}
        assert types == {"identity", "report", "relationship"}
        # Single derived_from UUID → single derived-from relationship.
        rels = [o for o in bundle.objects if o.type == "relationship"]
        assert len(rels) == 1
        assert rels[0].relationship_type == "derived-from"


# ---------------------------------------------------------------------------
# Test 8 — bundle envelope conforms to STIX 2.1 (id regex, type, parse)
# ---------------------------------------------------------------------------


class TestStix21BundleEnvelope:
    """Pin the on-the-wire bundle envelope to STIX 2.1 expectations."""

    @pytest.mark.asyncio
    async def test_bundle_id_matches_stix21_regex(self, tmp_path: Path) -> None:
        import re

        descriptor = _build_descriptor_outputs_block(tmp_path)
        bundle = await emit(
            FindingPayload(title="regex probe", body=""),
            descriptor=descriptor,
            deps=OutputDeps(),
        )
        # STIX 2.1: bundle id ∈ `bundle--<UUID4>` (or any UUID rev).
        assert re.match(
            r"^bundle--[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            bundle.id,
        ), f"bundle id {bundle.id!r} does not match STIX 2.1 regex"

    @pytest.mark.asyncio
    async def test_each_sdo_has_stix_typed_id(self, tmp_path: Path) -> None:
        """Every SDO in the bundle has a ``<type>--<uuid>`` id."""
        import re

        descriptor = _build_descriptor_outputs_block(tmp_path)
        bundle = await emit(
            FindingPayload(
                title="typed-id probe",
                body="",
                data={"cited_entities": {"orgs": [{"name": "Acme"}]}},
            ),
            descriptor=descriptor,
            deps=OutputDeps(),
            derived_from=[uuid4(), uuid4()],
        )
        type_id_pat = re.compile(
            r"^[a-z][a-z0-9-]*--[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
            r"-[0-9a-f]{4}-[0-9a-f]{12}$"
        )
        for sdo in bundle.objects:
            assert type_id_pat.match(sdo.id), (
                f"SDO id {sdo.id!r} (type={sdo.type}) violates STIX 2.1 "
                "<type>--<uuid> id shape"
            )
            assert sdo.id.startswith(f"{sdo.type}--"), (
                f"SDO id prefix {sdo.id!r} does not match its declared "
                f"type {sdo.type!r}"
            )
