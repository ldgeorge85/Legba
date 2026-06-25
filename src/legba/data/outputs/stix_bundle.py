# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""STIX 2.1 bundle output kind — L-195.

This module materializes Legba analyst outputs (findings, situations,
hypotheses, alerts, plus their cited entities) as `STIX 2.1
<https://docs.oasis-open.org/cti/stix/v2.1/cs02/stix-v2.1-cs02.html>`_
``bundle`` documents. It's the "share this with another intel system"
surface — where the substrate kind persists analyst output to our own
tables, this kind serializes the same payloads into a wire format that
TAXII 2.1 collections (and analyst tools like OpenCTI / MISP / EclecticIQ)
understand natively.

Scope (per task brief)
----------------------

* **Producer only.** We emit bundles + optionally publish to NATS. We do
  not run a TAXII server; HTTP TAXII upload to an upstream server is a
  follow-up.
* **TLP markings on every SDO** in the bundle, sourced from the
  descriptor's ``outputs.stix_bundle.tlp`` field
  (``white | green | amber | red``).
* **Per-target collection model.** TAXII 2.1 organizes content into
  collections; we use ``legba_target_<target_id>_collection`` as the
  conventional collection name so downstream TAXII servers receive
  per-target streams.
* **Lineage preservation.** Legba's ``derived_from`` UUIDs become STIX
  ``relationship`` SDOs of type ``derived-from`` linking the produced
  report/indicator to ancestor object refs (resolved at call time via a
  caller-supplied lookup; otherwise emitted as opaque references).

Payload → SDO mapping
---------------------

+------------------------+-------------------------------+----------------+
| Legba payload          | STIX SDO                      | Notes          |
+========================+===============================+================+
| ``FindingPayload``     | ``report`` (``report_types=`` | Body becomes   |
|                        | ``["threat-report"]``)        | description    |
+------------------------+-------------------------------+----------------+
| ``SituationPayload``   | ``incident`` + ``report``     | The incident   |
|                        |                               | carries name + |
|                        |                               | description;   |
|                        |                               | the report     |
|                        |                               | wraps it +     |
|                        |                               | cited entities |
+------------------------+-------------------------------+----------------+
| ``HypothesisPayload``  | ``report`` with               | thesis is the  |
|                        | ``report_types=``             | name; counter+ |
|                        | ``["analysis"]`` and a        | evidence in    |
|                        | ``hypothesis`` label          | description    |
+------------------------+-------------------------------+----------------+
| ``AlertPayload``       | ``indicator`` (severity gate) | medium+        |
|                        | OR fallback to ``report``     | severity emits |
|                        |                               | an indicator;  |
|                        |                               | low/info emit  |
|                        |                               | a report       |
+------------------------+-------------------------------+----------------+

Cited entities (people, orgs, locations) attached to a payload via the
descriptor's ``data.cited_entities`` slot — keyed lists like
``{"people": [...], "orgs": [...], "locations": [...]}`` — become
``identity`` / ``location`` SDOs and are linked from the parent SDO via
``object_refs`` (for reports) and ``relationship`` SDOs (``related-to``
linking indicators to their context).

Module surface
--------------

* ``KIND_NAME = "stix_bundle"`` — host-registry discovery hook.
* ``StixBundleExporter`` — class-shaped exporter for callers that want
  to maintain configuration state (TLP, target_id, taxii collection).
* ``export_outputs_to_stix(outputs, *, tlp, target_id, ...) -> Bundle``
  — functional one-shot exporter, returns a ``stix2.Bundle``.
* ``emit(payload, *, descriptor, deps) -> None`` — uniform output-kind
  emit surface; serializes a single payload, publishes the resulting
  bundle to NATS subject ``legba.outputs.stix.<target_id>``, and
  optionally writes to disk (when ``descriptor.outputs.stix_bundle
  .file_sink`` is set).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

import stix2

from ..provenance.models import (
    AlertPayload,
    FindingPayload,
    HypothesisPayload,
    SituationPayload,
)
from ._contract import OutputContext, OutputDeps
from .taxii_client import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_TIMEOUT_SECONDS as TAXII_DEFAULT_TIMEOUT_SECONDS,
    TaxiiConfig,
    TaxiiPushResult,
    TaxiiServerNotConfiguredError,
    push_bundle_to_taxii,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind identity
# ---------------------------------------------------------------------------


KIND_NAME: str = "stix_bundle"

#: Default NATS subject pattern. ``{target_id}`` is substituted at emit time.
NATS_SUBJECT_PATTERN: str = "legba.outputs.stix.{target_id}"

#: TAXII 2.1 collection name pattern per target.
TAXII_COLLECTION_PATTERN: str = "legba_target_{target_id}_collection"

#: Stable namespace UUID for derived STIX identifiers (so the same Legba
#: UUID always maps to the same STIX UUID; lets downstream consumers
#: deduplicate across re-exports).
LEGBA_STIX_NAMESPACE: UUID = uuid5(
    NAMESPACE_URL, "https://legba.example/stix-bundle-namespace"
)


# ---------------------------------------------------------------------------
# TLP markings
# ---------------------------------------------------------------------------


Tlp = Literal["white", "green", "amber", "red"]


def _tlp_marking(tlp: Tlp) -> stix2.MarkingDefinition:
    """Return the OASIS-canonical TLP marking-definition for ``tlp``.

    The ``stix2`` library exposes pre-built singletons that carry the
    well-known marking IDs every STIX consumer recognises. Using the
    singletons (rather than constructing our own MarkingDefinitions)
    means downstream tooling treats our markings as identical to the
    OASIS reference set.
    """
    tlp = tlp.lower()  # type: ignore[assignment]
    if tlp == "white":
        return stix2.TLP_WHITE
    if tlp == "green":
        return stix2.TLP_GREEN
    if tlp == "amber":
        return stix2.TLP_AMBER
    if tlp == "red":
        return stix2.TLP_RED
    raise ValueError(
        f"invalid TLP {tlp!r}; expected one of white | green | amber | red"
    )


# ---------------------------------------------------------------------------
# Identifier helpers
# ---------------------------------------------------------------------------


def _stix_id(prefix: str, source: UUID | str | None = None) -> str:
    """Build a STIX-shaped identifier ``<type>--<uuid>``.

    If ``source`` is a UUID we use it directly so substrate row IDs map
    1:1 to STIX object IDs. If ``source`` is a string, we derive a UUIDv5
    in the legba namespace so the same source always yields the same
    STIX ID. ``None`` yields a fresh UUIDv4.
    """
    if source is None:
        u = uuid4()
    elif isinstance(source, UUID):
        u = source
    else:
        u = uuid5(LEGBA_STIX_NAMESPACE, str(source))
    return f"{prefix}--{u}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Cited-entity extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CitedEntities:
    """Bag of identity / location SDOs derived from a payload's
    ``data.cited_entities`` block.

    Populated by :func:`_extract_cited_entities`. The bag carries the
    constructed SDOs (so the bundle can include them) and the parent
    object_refs (so the report SDO can reference them).
    """

    sdos: list[Any] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)


def _extract_cited_entities(
    data: Mapping[str, Any] | None,
    *,
    tlp_marking: stix2.MarkingDefinition,
) -> CitedEntities:
    """Walk a payload's ``data.cited_entities`` block and build STIX
    identity / location SDOs.

    Recognized keys::

        cited_entities:
          people:     [{name: "...", identity_class: "individual"}, ...]
          orgs:       [{name: "...", identity_class: "organization"}, ...]
          locations:  [{name: "...", country: "BR", region: "south-america"}, ...]

    Items may be bare strings (treated as the ``name`` field) or full
    mappings. Anything that doesn't validate against the STIX SDO shape
    is logged + skipped — citation extraction must not fail an export.
    """
    bag = CitedEntities()
    if not data:
        return bag
    cited = data.get("cited_entities") if isinstance(data, Mapping) else None
    if not isinstance(cited, Mapping):
        return bag

    common_kwargs = {"object_marking_refs": [tlp_marking]}

    for key in ("people", "individuals"):
        for entry in cited.get(key, []) or []:
            try:
                spec = {"name": entry} if isinstance(entry, str) else dict(entry)
                spec.setdefault("identity_class", "individual")
                ident = stix2.Identity(**spec, **common_kwargs)
                bag.sdos.append(ident)
                bag.refs.append(ident.id)
            except Exception as exc:
                logger.warning("stix_bundle.cited.skip_person entry=%r err=%s", entry, exc)

    for key in ("orgs", "organizations"):
        for entry in cited.get(key, []) or []:
            try:
                spec = {"name": entry} if isinstance(entry, str) else dict(entry)
                spec.setdefault("identity_class", "organization")
                ident = stix2.Identity(**spec, **common_kwargs)
                bag.sdos.append(ident)
                bag.refs.append(ident.id)
            except Exception as exc:
                logger.warning("stix_bundle.cited.skip_org entry=%r err=%s", entry, exc)

    for entry in cited.get("locations", []) or []:
        try:
            spec = {"name": entry} if isinstance(entry, str) else dict(entry)
            loc = stix2.Location(**spec, **common_kwargs)
            bag.sdos.append(loc)
            bag.refs.append(loc.id)
        except Exception as exc:
            logger.warning("stix_bundle.cited.skip_location entry=%r err=%s", entry, exc)

    return bag


# ---------------------------------------------------------------------------
# Lineage relationship helpers
# ---------------------------------------------------------------------------


def _derived_from_relationships(
    source_ref: str,
    derived_from: Sequence[UUID] | None,
    *,
    ancestor_lookup: Mapping[UUID, str] | None = None,
    tlp_marking: stix2.MarkingDefinition | None = None,
) -> list[stix2.Relationship]:
    """Build STIX ``relationship`` SDOs of type ``derived-from`` for each
    ancestor UUID.

    ``ancestor_lookup`` maps Legba substrate UUIDs → already-known STIX
    SDO ids (so a caller that has previously emitted the ancestor can
    link to its real id). Unknown ancestors get a synthesized
    ``identity--<uuidv5>`` reference; this is intentional — a STIX
    relationship requires a typed target_ref, and treating ancestors as
    opaque identities means receivers can ingest the bundle without
    pre-loading our substrate.
    """
    if not derived_from:
        return []
    out: list[stix2.Relationship] = []
    common: dict[str, Any] = {}
    if tlp_marking is not None:
        common["object_marking_refs"] = [tlp_marking]

    for ancestor_uuid in derived_from:
        if ancestor_lookup and ancestor_uuid in ancestor_lookup:
            target_ref = ancestor_lookup[ancestor_uuid]
        else:
            target_ref = _stix_id("identity", ancestor_uuid)
        rel = stix2.Relationship(
            source_ref=source_ref,
            target_ref=target_ref,
            relationship_type="derived-from",
            **common,
        )
        out.append(rel)
    return out


# ---------------------------------------------------------------------------
# Payload → SDO converters
# ---------------------------------------------------------------------------


def finding_to_report(
    payload: FindingPayload,
    *,
    tlp_marking: stix2.MarkingDefinition,
    object_refs: Sequence[str] = (),
    finding_id: UUID | None = None,
    produced_at: datetime | None = None,
) -> stix2.Report:
    """Map a :class:`FindingPayload` to a STIX 2.1 ``report`` SDO.

    A finding is a generic analyst observation; STIX's ``report``
    container is the closest match — it's a curated set of intel about
    one or more entities. We pass ``report_types=["threat-report"]``;
    operator tooling that wants finer granularity can override via the
    payload's ``tags`` (we copy tags into ``labels``).
    """
    refs = list(object_refs) or [_stix_id("identity")]
    return stix2.Report(
        id=_stix_id("report", finding_id),
        name=payload.title,
        description=payload.body or payload.title,
        published=produced_at or _now(),
        report_types=["threat-report"],
        object_refs=refs,
        labels=list(payload.tags) if payload.tags else None,
        confidence=int(round(payload.confidence * 100)),
        object_marking_refs=[tlp_marking],
    )


def situation_to_incident(
    payload: SituationPayload,
    *,
    tlp_marking: stix2.MarkingDefinition,
    situation_id: UUID | None = None,
    produced_at: datetime | None = None,
) -> stix2.Incident:
    """Map a :class:`SituationPayload` to a STIX 2.1 ``incident`` SDO.

    STIX 2.1 introduced ``incident`` precisely to represent ongoing /
    composite events that aren't single observables; Legba situations
    fit that exactly. ``status`` becomes the incident description tag
    (STIX incident has no first-class status field at 2.1).
    """
    description_parts: list[str] = []
    if payload.category:
        description_parts.append(f"category: {payload.category}")
    if payload.status:
        description_parts.append(f"status: {payload.status}")
    if payload.event_count:
        description_parts.append(f"event_count: {payload.event_count}")
    if payload.intensity_score:
        description_parts.append(f"intensity_score: {payload.intensity_score:.3f}")
    description = " | ".join(description_parts) or payload.name

    return stix2.Incident(
        id=_stix_id("incident", situation_id),
        name=payload.name,
        description=description,
        created=produced_at or _now(),
        modified=produced_at or _now(),
        object_marking_refs=[tlp_marking],
    )


def hypothesis_to_report(
    payload: HypothesisPayload,
    *,
    tlp_marking: stix2.MarkingDefinition,
    hypothesis_id: UUID | None = None,
    produced_at: datetime | None = None,
) -> stix2.Report:
    """Map a :class:`HypothesisPayload` to a STIX 2.1 ``report`` SDO
    typed as an analytic hypothesis.

    STIX has no first-class hypothesis SDO; ``report`` with
    ``report_types=["analysis"]`` and a ``hypothesis`` label is the
    OASIS-recommended convention.
    """
    body_parts: list[str] = [payload.thesis]
    if payload.counter_thesis:
        body_parts.append(f"COUNTER: {payload.counter_thesis}")
    if payload.diagnostic_evidence:
        body_parts.append(
            "EVIDENCE: " + "; ".join(str(e) for e in payload.diagnostic_evidence)
        )
    body_parts.append(f"evidence_balance: {payload.evidence_balance}")
    body_parts.append(f"status: {payload.status}")
    description = "\n".join(body_parts)

    # Reports require at least one object_ref. Use an identity placeholder
    # if no real refs are passed in via cited entities; the bundle exporter
    # may extend this list before serialisation.
    return stix2.Report(
        id=_stix_id("report", hypothesis_id),
        name=payload.thesis[:512],
        description=description,
        published=produced_at or _now(),
        report_types=["analysis"],
        labels=["hypothesis"],
        object_refs=[_stix_id("identity")],
        object_marking_refs=[tlp_marking],
    )


def alert_to_indicator_or_report(
    payload: AlertPayload,
    *,
    tlp_marking: stix2.MarkingDefinition,
    alert_id: UUID | None = None,
    produced_at: datetime | None = None,
) -> stix2.Indicator | stix2.Report:
    """Map an :class:`AlertPayload` to either a STIX ``indicator`` or
    ``report`` SDO depending on severity.

    Severity gate:
      * ``medium`` / ``high`` / ``critical`` → ``indicator`` SDO. The
        STIX pattern is synthesized from the payload's tags + routing
        hint so the indicator is at least syntactically valid; real
        operator-grade patterns are built by sources that detect
        IOC-shaped signals, not by the generic alert path.
      * ``info`` / ``low`` → ``report`` SDO. These severities are not
        "indicators of compromise" in STIX terminology, so emitting an
        indicator would mislead downstream consumers.
    """
    when = produced_at or _now()
    if payload.severity in ("medium", "high", "critical"):
        # Build a minimal but syntactically valid STIX 2.1 pattern. The
        # pattern is keyed on the alert title so consumers can correlate
        # repeated alerts with the same root.
        # Use a quoted string-comparison pattern over `incident.name` — a
        # custom-property fallback that nearly every STIX engine accepts.
        safe_title = payload.title.replace("'", "\\'")[:512]
        pattern = f"[x-legba-alert:title = '{safe_title}']"
        return stix2.Indicator(
            id=_stix_id("indicator", alert_id),
            name=payload.title,
            description=payload.body or payload.title,
            indicator_types=["malicious-activity"],
            pattern=pattern,
            pattern_type="stix",
            valid_from=when,
            confidence=int(round(payload.confidence * 100)),
            labels=[f"severity:{payload.severity}", *payload.tags],
            object_marking_refs=[tlp_marking],
        )
    # Low / info severity → report.
    return stix2.Report(
        id=_stix_id("report", alert_id),
        name=payload.title,
        description=payload.body or payload.title,
        published=when,
        report_types=["observed-data"],
        labels=[f"severity:{payload.severity}", *payload.tags],
        object_refs=[_stix_id("identity")],
        confidence=int(round(payload.confidence * 100)),
        object_marking_refs=[tlp_marking],
    )


# ---------------------------------------------------------------------------
# OutputEnvelope — the input shape for the multi-payload exporter
# ---------------------------------------------------------------------------


@dataclass
class OutputEnvelope:
    """One analyst-output row to translate into the bundle.

    Mirrors the in-flight :class:`AnalystOutput` from the kind contract
    but only carries what the STIX exporter actually needs. The runtime
    builds these from substrate rows; tests build them directly.
    """

    payload: FindingPayload | SituationPayload | HypothesisPayload | AlertPayload
    output_id: UUID = field(default_factory=uuid4)
    derived_from: list[UUID] = field(default_factory=list)
    produced_at: datetime | None = None
    cited_data: Mapping[str, Any] | None = None
    """Optional ``data.cited_entities``-shaped mapping; if absent, falls
    back to ``payload.data`` when the payload type carries one."""


def _resolve_cited_data(env: OutputEnvelope) -> Mapping[str, Any] | None:
    """Pull the cited_entities block off the envelope, or from
    ``payload.data`` when the payload exposes one.
    """
    if env.cited_data:
        return env.cited_data
    data = getattr(env.payload, "data", None)
    if isinstance(data, Mapping):
        return data
    return None


# ---------------------------------------------------------------------------
# Functional one-shot exporter
# ---------------------------------------------------------------------------


def export_outputs_to_stix(
    outputs: Iterable[OutputEnvelope] | OutputEnvelope,
    *,
    tlp: Tlp,
    target_id: str,
    ancestor_lookup: Mapping[UUID, str] | None = None,
    extra_external_refs: Sequence[Mapping[str, Any]] | None = None,
) -> stix2.Bundle:
    """Translate one or more :class:`OutputEnvelope` into a STIX 2.1
    ``Bundle``.

    All SDOs carry the TLP marking specified by ``tlp``. The bundle
    object itself is not markable in STIX 2.1 (it's a transport
    envelope), so per-SDO marking is the spec-compliant path.

    Parameters
    ----------
    outputs:
        One envelope, or an iterable of envelopes. When passing a
        single envelope, callers may pass it directly without wrapping
        in a list.
    tlp:
        TLP marking level for every SDO in the bundle.
    target_id:
        Legba target ID — emitted as a labelled identity SDO so
        downstream tooling can group bundle-contents by source target.
    ancestor_lookup:
        Optional ``UUID → stix_id`` map used by the ``derived-from``
        relationship builder so previously-emitted ancestors resolve
        to their real SDO ids.
    extra_external_refs:
        Optional list of external-reference dicts (``{source_name,
        url, description}``) attached to every report-like SDO so a
        receiver can click back into Legba's UI.
    """
    if isinstance(outputs, OutputEnvelope):
        envelopes = [outputs]
    else:
        envelopes = list(outputs)
    if not envelopes:
        raise ValueError("export_outputs_to_stix requires at least one output")

    marking = _tlp_marking(tlp)
    sdos: list[Any] = []

    # The target identity — emitted once so every payload can object-ref it.
    target_identity = stix2.Identity(
        id=_stix_id("identity", f"legba-target:{target_id}"),
        name=f"Legba target {target_id}",
        identity_class="system",
        description=f"Legba intelligence target {target_id}",
        object_marking_refs=[marking],
    )
    sdos.append(target_identity)

    for env in envelopes:
        sdos.extend(_envelope_to_sdos(
            env,
            target_identity_id=target_identity.id,
            tlp_marking=marking,
            ancestor_lookup=ancestor_lookup,
        ))

    return stix2.Bundle(objects=sdos, allow_custom=True)


def _envelope_to_sdos(
    env: OutputEnvelope,
    *,
    target_identity_id: str,
    tlp_marking: stix2.MarkingDefinition,
    ancestor_lookup: Mapping[UUID, str] | None,
) -> list[Any]:
    """Render one :class:`OutputEnvelope` into the SDOs (+ relationships)
    that should land in the bundle.
    """
    sdos: list[Any] = []
    cited = _extract_cited_entities(_resolve_cited_data(env), tlp_marking=tlp_marking)
    sdos.extend(cited.sdos)

    object_refs = [target_identity_id, *cited.refs]
    primary_sdo: Any

    if isinstance(env.payload, FindingPayload):
        primary_sdo = finding_to_report(
            env.payload,
            tlp_marking=tlp_marking,
            object_refs=object_refs,
            finding_id=env.output_id,
            produced_at=env.produced_at,
        )
        sdos.append(primary_sdo)
    elif isinstance(env.payload, SituationPayload):
        incident = situation_to_incident(
            env.payload,
            tlp_marking=tlp_marking,
            situation_id=env.output_id,
            produced_at=env.produced_at,
        )
        sdos.append(incident)
        # Wrap the incident + cited refs in a report so the bundle is
        # self-contained for STIX viewers that index off reports.
        report = stix2.Report(
            id=_stix_id("report", uuid5(LEGBA_STIX_NAMESPACE, f"sit-report:{env.output_id}")),
            name=f"Situation: {env.payload.name}",
            description=incident.description,
            published=env.produced_at or _now(),
            report_types=["observed-data"],
            labels=["incident-type", env.payload.category or "uncategorized"],
            object_refs=[incident.id, *object_refs],
            object_marking_refs=[tlp_marking],
        )
        sdos.append(report)
        primary_sdo = report
    elif isinstance(env.payload, HypothesisPayload):
        # If we have cited refs, use those; otherwise hypothesis_to_report
        # uses its own identity placeholder.
        report = hypothesis_to_report(
            env.payload,
            tlp_marking=tlp_marking,
            hypothesis_id=env.output_id,
            produced_at=env.produced_at,
        )
        # Override the placeholder object_refs with what we actually have.
        if object_refs:
            # stix2 objects are frozen; rebuild with extended refs.
            report = report.new_version(object_refs=object_refs)
        sdos.append(report)
        primary_sdo = report
    elif isinstance(env.payload, AlertPayload):
        primary_sdo = alert_to_indicator_or_report(
            env.payload,
            tlp_marking=tlp_marking,
            alert_id=env.output_id,
            produced_at=env.produced_at,
        )
        sdos.append(primary_sdo)
        # Indicators don't carry object_refs; emit a related-to
        # relationship so cited entities still link to the alert.
        if isinstance(primary_sdo, stix2.Indicator):
            for ref in cited.refs:
                sdos.append(
                    stix2.Relationship(
                        source_ref=primary_sdo.id,
                        target_ref=ref,
                        relationship_type="related-to",
                        object_marking_refs=[tlp_marking],
                    )
                )
    else:
        raise TypeError(
            f"unsupported payload type for STIX export: {type(env.payload).__name__}"
        )

    # Lineage: derived_from → relationship SDOs.
    sdos.extend(
        _derived_from_relationships(
            primary_sdo.id,
            env.derived_from,
            ancestor_lookup=ancestor_lookup,
            tlp_marking=tlp_marking,
        )
    )
    return sdos


# ---------------------------------------------------------------------------
# Class-shaped exporter (stateful — holds config across many calls)
# ---------------------------------------------------------------------------


@dataclass
class StixBundleExporter:
    """Stateful STIX bundle exporter.

    Bundles up the TLP marking + target_id + collection so callers
    invoking the exporter from inside a target-scoped runtime don't
    repeat themselves. Mirrors the spirit of the A2A skill exporter
    object: a thin, class-shaped wrapper over the functional core.
    """

    tlp: Tlp
    target_id: str
    taxii_collection: str = ""
    ancestor_lookup: dict[UUID, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Eager-validate the TLP value at construction time.
        _tlp_marking(self.tlp)
        if not self.taxii_collection:
            self.taxii_collection = TAXII_COLLECTION_PATTERN.format(
                target_id=self.target_id
            )

    @property
    def nats_subject(self) -> str:
        return NATS_SUBJECT_PATTERN.format(target_id=self.target_id)

    def export(self, outputs: Iterable[OutputEnvelope] | OutputEnvelope) -> stix2.Bundle:
        """Build a STIX bundle from one or more outputs. Pure function;
        no side effects."""
        return export_outputs_to_stix(
            outputs,
            tlp=self.tlp,
            target_id=self.target_id,
            ancestor_lookup=self.ancestor_lookup or None,
        )

    def remember_ancestor(self, legba_uuid: UUID, stix_id: str) -> None:
        """Record a Legba-UUID → STIX-id mapping so future
        ``derived-from`` relationships resolve to real ancestor ids
        instead of synthesized identity placeholders.
        """
        self.ancestor_lookup[legba_uuid] = stix_id

    def to_json(self, outputs: Iterable[OutputEnvelope] | OutputEnvelope) -> str:
        """Build the bundle and return its canonical STIX JSON
        serialisation."""
        return self.export(outputs).serialize()


# ---------------------------------------------------------------------------
# emit() — the uniform output-kind surface
# ---------------------------------------------------------------------------


def _payload_from_descriptor(descriptor: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Extract the ``outputs.stix_bundle.config`` block from a
    descriptor mapping. Accepts either the raw config dict, or a full
    descriptor with ``outputs: [{kind: stix_bundle, config: {...}}]``.
    """
    if not descriptor:
        return {}
    if "tlp" in descriptor or "target_id" in descriptor:
        return descriptor  # already the config block
    outputs_list = descriptor.get("outputs")
    if isinstance(outputs_list, list):
        for binding in outputs_list:
            if not isinstance(binding, Mapping):
                continue
            if binding.get("kind") == KIND_NAME:
                cfg = binding.get("config")
                if isinstance(cfg, Mapping):
                    return cfg
    if isinstance(descriptor.get("outputs"), Mapping):
        inner = descriptor["outputs"]
        if isinstance(inner.get(KIND_NAME), Mapping):
            return inner[KIND_NAME]
    return {}


async def emit(
    payload: Any,
    *,
    descriptor: Mapping[str, Any] | None,
    deps: OutputDeps,
    ctx: OutputContext | None = None,
    output_id: UUID | None = None,
    derived_from: Sequence[UUID] | None = None,
) -> stix2.Bundle:
    """Emit a single analyst payload as a STIX bundle.

    The handler:

      1. Reads ``tlp`` + ``target_id`` (+ optional ``file_sink``) from
         the descriptor's ``outputs.stix_bundle.config`` block.
      2. Wraps the payload in an :class:`OutputEnvelope` and serialises
         to a STIX 2.1 bundle.
      3. Publishes the bundle JSON to the configured NATS subject
         (``legba.outputs.stix.<target_id>``). NATS publishing is
         best-effort: a missing publisher logs a warning rather than
         raising — STIX export is a producer kind and an absent
         downstream should not blow up the analyst.
      4. Optionally writes the bundle to a file when descriptor sets
         ``file_sink: /some/path/{target_id}-{output_id}.json``.

    Returns the constructed :class:`stix2.Bundle` so callers can attach
    additional sinks (HTTP TAXII upload, S3, etc.) without us baking
    them into the kind. TAXII upload is a documented follow-up.
    """
    cfg = _payload_from_descriptor(descriptor)
    tlp: Tlp = cfg.get("tlp", "amber")  # amber is the safe default
    target_id: str = cfg.get("target_id") or (ctx.target_id if ctx else "") or "unknown"

    env = OutputEnvelope(
        payload=payload,
        output_id=output_id or uuid4(),
        derived_from=list(derived_from or []),
    )
    bundle = export_outputs_to_stix(env, tlp=tlp, target_id=target_id)
    bundle_json = bundle.serialize()
    bundle_bytes = bundle_json.encode("utf-8")

    # NATS publish — best-effort.
    if deps.nats is not None:
        subject = NATS_SUBJECT_PATTERN.format(target_id=target_id)
        try:
            await deps.nats.publish_json(subject, bundle_bytes)
        except Exception as exc:
            logger.warning(
                "stix_bundle.emit.nats_publish_failed subject=%s err=%s",
                subject, exc,
            )
    else:
        logger.info(
            "stix_bundle.emit.no_nats_publisher target_id=%s bundle_id=%s",
            target_id, bundle.id,
        )

    # Optional file sink.
    file_sink = cfg.get("file_sink")
    if file_sink:
        try:
            path = Path(str(file_sink).format(
                target_id=target_id,
                output_id=env.output_id,
                bundle_id=bundle.id,
            ))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(bundle_json)
        except Exception as exc:
            logger.warning(
                "stix_bundle.emit.file_sink_failed path=%r err=%s",
                file_sink, exc,
            )

    # Optional TAXII 2.1 push — opt-in via the descriptor's
    # ``outputs.stix_bundle.config.taxii`` binding. Best-effort, runs after
    # the durable NATS/file sinks so an upstream-server outage never blocks
    # or loses the bundle (degrade-not-drop). A missing ``taxii`` binding is
    # a no-op; an un-provisioned/cleartext destination logs + skips here
    # (the loud refusal fires for direct upload_bundle_to_taxii callers).
    await _maybe_push_taxii(bundle, cfg=cfg, target_id=target_id, deps=deps)

    return bundle


# ---------------------------------------------------------------------------
# TAXII 2.1 upload — real outbound push (export-interop)
# ---------------------------------------------------------------------------


async def upload_bundle_to_taxii(
    bundle: stix2.Bundle,
    *,
    server_url: str,
    api_root: str,
    collection_id: str,
    auth: tuple[str, str] | None = None,
    token: str | None = None,
    http: Any | None = None,
    deps: OutputDeps | None = None,
    timeout_seconds: float = TAXII_DEFAULT_TIMEOUT_SECONDS,
    backoff_seconds: Sequence[float] | None = None,
) -> TaxiiPushResult:
    """Push a STIX bundle to a TAXII 2.1 collection (add-objects endpoint).

    POSTs the bundle's objects (wrapped in a TAXII ``envelope``) to
    ``{server_url}/{api_root}/collections/{collection_id}/objects/`` via
    the structural HTTP port (``http`` arg or ``deps.http``).

    Delivery is **degrade-not-drop**: transient failures (network / 5xx)
    are retried with bounded backoff and, on exhaustion, returned as a
    :class:`TaxiiPushResult` — never raised. Only an *un-provisioned*
    destination (empty ``server_url`` / no HTTP client / cleartext host)
    raises :class:`TaxiiServerNotConfiguredError`, the declared SEAM guard
    rail (``docs/SEAMS.md`` seam 10).

    ``auth`` is a ``(username, password)`` tuple for HTTP Basic; pass
    ``token`` instead for bearer auth.
    """
    client = http if http is not None else (deps.http if deps is not None else None)
    config = TaxiiConfig(
        server_url=server_url,
        api_root=api_root,
        collection_id=collection_id,
        auth_kind="bearer" if token else ("basic" if auth else "none"),
        username=auth[0] if auth else None,
        password=auth[1] if auth else None,
        token=token,
        timeout_seconds=timeout_seconds,
        backoff_seconds=tuple(backoff_seconds) if backoff_seconds is not None
        else DEFAULT_BACKOFF_SECONDS,
    )
    return await push_bundle_to_taxii(bundle, config=config, http=client)


def _taxii_config_from_descriptor(
    cfg: Mapping[str, Any],
    *,
    target_id: str,
) -> TaxiiConfig | None:
    """Build a :class:`TaxiiConfig` from the ``outputs.stix_bundle.config``
    block's optional ``taxii`` sub-mapping.

    Returns ``None`` when no ``taxii`` binding is present (TAXII push is
    opt-in — descriptors that only want NATS/file never trigger it). When
    present but missing ``server_url`` the resolver lets the loud
    :class:`TaxiiServerNotConfiguredError` surface downstream (the
    un-provisioned-destination seam), so a half-configured binding fails
    loud rather than silently skipping.

    ``collection_id`` defaults to the per-target collection name pattern
    when the binding omits it.
    """
    taxii = cfg.get("taxii") if isinstance(cfg, Mapping) else None
    if not isinstance(taxii, Mapping) or not taxii:
        return None
    collection_id = (
        str(taxii.get("collection_id") or "")
        or TAXII_COLLECTION_PATTERN.format(target_id=target_id)
    )
    return TaxiiConfig(
        server_url=str(taxii.get("server_url") or ""),
        api_root=str(taxii.get("api_root") or "taxii2"),
        collection_id=collection_id,
        auth_kind=str(taxii.get("auth_kind") or "none"),  # type: ignore[arg-type]
        username=taxii.get("username"),
        password=taxii.get("password"),
        token=taxii.get("token"),
        timeout_seconds=float(taxii.get("timeout_seconds", TAXII_DEFAULT_TIMEOUT_SECONDS)),
        backoff_seconds=tuple(taxii.get("backoff_seconds") or DEFAULT_BACKOFF_SECONDS),
        headers=dict(taxii.get("headers") or {}),
    )


async def _maybe_push_taxii(
    bundle: stix2.Bundle,
    *,
    cfg: Mapping[str, Any],
    target_id: str,
    deps: OutputDeps,
) -> TaxiiPushResult | None:
    """Best-effort TAXII push for the ``emit`` path — degrade, never raise.

    Resolves the descriptor ``taxii`` binding; if absent, returns ``None``
    (no push requested). If present, pushes via ``deps.http``. A delivery
    failure (transient/permanent) is logged and returned as a result. A
    config error (un-provisioned / cleartext destination) is logged but
    swallowed here so it never breaks the already-durable bundle — the
    binding is misconfigured, not the run. The loud
    :class:`TaxiiServerNotConfiguredError` still fires for direct callers
    of :func:`upload_bundle_to_taxii`.
    """
    config = _taxii_config_from_descriptor(cfg, target_id=target_id)
    if config is None:
        return None
    try:
        result = await push_bundle_to_taxii(bundle, config=config, http=deps.http)
    except TaxiiServerNotConfiguredError as exc:
        logger.warning(
            "stix_bundle.emit.taxii_unconfigured target_id=%s err=%s",
            target_id, exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — never break a durable bundle
        logger.warning(
            "stix_bundle.emit.taxii_push_failed target_id=%s err=%s",
            target_id, exc,
        )
        return None
    if not result.delivered:
        logger.warning(
            "stix_bundle.emit.taxii_not_delivered target_id=%s outcome=%s "
            "status=%s detail=%s",
            target_id, result.outcome, result.http_status, result.detail,
        )
    return result


__all__ = [
    "KIND_NAME",
    "LEGBA_STIX_NAMESPACE",
    "NATS_SUBJECT_PATTERN",
    "OutputEnvelope",
    "StixBundleExporter",
    "TAXII_COLLECTION_PATTERN",
    "TaxiiConfig",
    "TaxiiPushResult",
    "TaxiiServerNotConfiguredError",
    "Tlp",
    "alert_to_indicator_or_report",
    "emit",
    "export_outputs_to_stix",
    "finding_to_report",
    "hypothesis_to_report",
    "push_bundle_to_taxii",
    "situation_to_incident",
    "upload_bundle_to_taxii",
]
