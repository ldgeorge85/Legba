# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who writes each table — the ONE declared place.

Every table a migration (or a declared runtime DDL) creates is classified here
into exactly one of three classes; ``tests/data_pkg/test_write_attribution.py``
fails when a table is missing, named twice, or names a table no migration
creates, so a new table cannot land unclassified.

(a) ``A`` — the row ALREADY names its writer in a server- or code-stamped
    column: the seat (``analyst_id`` + ``analyst_version``, which resolve to
    ``agent:<id>@<version>`` / ``rule:<id>``), the audit actor the registry
    stamps (``user:<id>``, ``service:<name>``, ``<kind>:bearer:<sha12>``), the
    user a session row belongs to, or the parent row it is written with.
    Nothing is added.
(b) ``B`` — the table holds judgements, labels, decisions, descriptor
    versions, watch lists, entered knowledge or anything a person or seat
    authors, and carries a nullable ``written_by`` reference to ``principals``
    (migration ``20261115T1827_principals_origins_written_by``), filled from
    the request principal, the running seat or the rule from that migration
    on. ``bookkeeping`` names modules whose writes to the table are machine
    bookkeeping (a head flip, a seal, a body annotation) that leave the row's
    author as it was; every OTHER module that writes the table must fill
    ``written_by`` (the test reads each writer's source).
(c) ``C`` — a machine table whose writer is implied by the table: derived,
    append-only or high-volume rows (signals, observations, fetch versions,
    traces' neighbours, projections). The writer is recorded ONCE here as a
    principal handle; no column is added, on purpose (a uuid per row on a
    table taking tens of thousands of rows a week buys nothing a reader needs).

A LEAF: stdlib only. The 7-day report (``scripts/report_written_by.py``) and
the guard test read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "Attribution",
    "CLASS_A",
    "CLASS_B",
    "CLASS_C",
    "TABLES",
    "by_class",
]

CLASS_A = "a"
CLASS_B = "b"
CLASS_C = "c"


@dataclass(frozen=True)
class Attribution:
    cls: str
    note: str
    #: (a) the column(s) that name the writer.
    columns: tuple[str, ...] = ()
    #: (b) modules (repo-relative paths) whose writes are bookkeeping only.
    bookkeeping: tuple[str, ...] = ()
    #: (c) the implied writer, as a principal handle.
    writer: str | None = None


def A(columns: str, note: str = "") -> Attribution:  # noqa: N802 — a table-row constructor
    return Attribution(CLASS_A, note, columns=tuple(c.strip() for c in columns.split(",")))


def B(note: str, *bookkeeping: str) -> Attribution:  # noqa: N802
    return Attribution(CLASS_B, note, bookkeeping=tuple(bookkeeping))


def C(writer: str, note: str = "") -> Attribution:  # noqa: N802
    return Attribution(CLASS_C, note, writer=writer)


_SEAT = "analyst_id, analyst_version"
_INGEST = "rule:source_ingest"

_TABLES: dict[str, Attribution] = {
    # --- sources, config, registry --------------------------------------
    "source_descriptors": B(
        "descriptor versions: the registry route's actor; materializers and the manual batch name their rule",
        "src/legba/data/analysts/deterministic_handlers/entity_gc.py",
    ),
    "target_descriptors": B(
        "descriptor versions: the registry route's actor; the discovery materializer and smoke name their rule",
        "src/legba/data/discovery/autowire.py",
    ),
    "analyst_descriptors": B("descriptor versions: the registry route's actor"),
    "action_pack_descriptors": B("descriptor versions: the registry route's actor"),
    "collection_descriptors": B("descriptor versions: the registry route's actor"),
    "wiring_descriptors": B("subscription grants: the caller passes the granting principal"),
    "stack_components": B("stack component versions: the registry route's actor"),
    "stack_credentials": B("vault rows: the registry route's actor (created_by keeps the audit label)"),
    "retention_policies": B("policy edits: the registry route's actor"),
    "vocabulary_entries": B("vocabulary added, edited or retired through the registry route"),
    "collection_requirements": B("the collection_gap rule writes them; the review route stamps the reviewer"),
    "source_credibility": B("the per-host ledger: the registry route's actor (scored_by keeps the body's name with sign-in off)"),
    "source_layers": C("rule:source_layers_loader", "derived from the layer map file"),
    "desk_apertures": C("rule:source_layers_loader", "derived from the layer map file"),
    "source_track_records": C("rule:source_track_record", "computed earned record"),
    "source_ratings": A("rater"),
    "source_dossiers": A("compiled_by"),
    "descriptor_audit_log": A("actor_id, actor_role", "every descriptor change, stamped by the registry"),
    "descriptor_dead_letter": A("actor"),
    "descriptor_conversion_archives": C("rule:descriptor_conversion"),
    "conversion_webhooks": C("rule:descriptor_conversion", "empty; no writer in the tree"),
    "conversion_executions": C("rule:descriptor_conversion", "empty; no writer in the tree"),
    "ui_panel_registrations": A("descriptor_id, descriptor_version", "written with the descriptor version that declares the panel"),
    "iso_countries": C("rule:migration_seed", "a reference snapshot loaded by its migration"),
    "seed_batches": C("rule:seed_loader", "one row per seed load"),
    "scheduled_events": B("the scheduled-events loader names itself (the row's provenance says where the entry came from)"),
    "discovery_state": A("descriptor_id, descriptor_version", "the discovery descriptor that ran"),
    # --- fetch and evidence ---------------------------------------------
    "source_poll_outcomes": C(_INGEST, "one row per poll; high volume"),
    "source_stream_gaps": C(_INGEST),
    "ingestion_requests": A("requested_by", "code-stamped: analyst::<id>, source::<id>, the drift and archiver requesters"),
    "ingestion_request_records": C("rule:intake", "written with its ingestion_requests row"),
    "collection_loads": C("rule:collection_loader"),
    "fetch_versions": C("rule:fetch_capture", "the stored bytes' register; high volume"),
    "fetch_version_records": C("rule:fetch_capture"),
    "fetch_version_tombstones": C("rule:archive_retention"),
    "fetch_drift_observations": A("observed_by"),
    "evidence_archive": C("rule:evidence_archiver", "high volume"),
    "evidence_archive_cold": C("rule:archive_retention"),
    "evidence_archive_bundles": C("rule:archive_retention"),
    "receipt_anchors": C("rule:receipt_anchor", "the daily Merkle anchor"),
    "receipt_anchor_trees": C("rule:receipt_anchor"),
    "receipt_anchor_leaves": C("rule:receipt_anchor"),
    "corpus_tombstones": C("rule:corpus_retention"),
    # --- signals --------------------------------------------------------
    "signals": C(_INGEST, "one row per fetched item; high volume"),
    "signal_aliases": A("produced_by"),
    # --- facts and contention -------------------------------------------
    "facts": A(_SEAT, "the extracting seat; seed rows also carry seed_batch_id"),
    "fact_decay_states": C("rule:fact_decay_scan"),
    "fact_contention": C("rule:fact_contention", "derived"),
    "fact_contention_values": C("rule:fact_contention", "derived"),
    "fact_contention_tiebreak": C("rule:fact_contention_arbiter", "one writer, the arbiter (model_id names the model it asked)"),
    "narratives": C("rule:narrative_mapper", "derived"),
    "narrative_echo_edges": C("rule:narrative_mapper", "derived"),
    # --- entities and graph ---------------------------------------------
    "entity_profiles": A(_SEAT),
    "entity_profile_versions": A(_SEAT),
    "entity_aliases": C("rule:entity_alias_keys", "empty; kept for the entity lookup lane"),
    "entity_alias_keys": C("rule:entity_alias_keys", "derived lookup keys"),
    "entity_judgement": B("merge verdicts: the entity researcher seat, the reifier's alias rule, a person through Consult"),
    "entity_edges": A(_SEAT),
    "entity_edge_events": A(_SEAT),
    "entity_edges_unresolved": C("rule:entity_edge_writes", "derived"),
    "proposed_edges": A(_SEAT),
    "proposed_edge_markers": C("rule:proposed_edge_marker_sync", "one row per finally refused candidate pair, written by a trigger on proposed_edges and by the one-off copy script"),
    "nexuses": A(_SEAT),
    "signal_entity_links": A(_SEAT),
    "event_entity_links": C("rule:event_writes", "derived links; high volume"),
    "graph_arcs": C("rule:graph_projector", "a rebuilt projection; about ten million rows"),
    "graph_arcs_meta": C("rule:graph_projector"),
    "graph_metrics": A(_SEAT),
    "bearing_edges": C("rule:bearing", "matcher_version names the rule's version"),
    # --- events and situations ------------------------------------------
    "events": A(_SEAT),
    "event_edges": A(_SEAT),
    "event_lifecycle_events": A(_SEAT),
    "signal_event_links": C("rule:event_writes", "derived links; high volume"),
    "situations": A(_SEAT),
    "situation_events": C("rule:situation_trajectory"),
    "situation_event_links": C("rule:event_writes", "derived links"),
    # --- observations ---------------------------------------------------
    "observations": C("rule:collection_loader", "numeric series points; partitioned"),
    "observations_*": C("rule:collection_loader", "the yearly partitions"),
    "observations_default": C("rule:collection_loader"),
    # --- analyst products -----------------------------------------------
    "analyst_outputs": A(_SEAT, "every finding names the seat that wrote it"),
    "finding_supersessions": A("produced_by"),
    "analyst_traces": A(_SEAT, "one row per run"),
    "analyst_trace_bundles": C("rule:trace_bulk_compaction", "compressed bodies of old run traces, written only by the daily compaction job"),
    "analyst_critiques": A("judge_analyst_id, judge_analyst_version"),
    "output_consumption": C("rule:output_consumption", "derived; high volume"),
    "output_dead_letter": A(_SEAT),
    "review_flags": C("rule:claim_watch", "one writer, claim_watch"),
    "journal_entries": A(_SEAT),
    "journal_proposals": B("the proposing seat writes the row; the deciding person stamps the decision"),
    "hypotheses": A(_SEAT),
    "inquiry_ledger": B("the inquiry seat that wrote or closed the entry (descriptor_id alone has no version)"),
    "desk_baselines": C("rule:desk_baseline"),
    "desk_rows_shadow": A("analyst_id, contract_version"),
    # citations as rows (migration 20261115T1828): written with
    # the finding, insert-only; the finding's seat or rule, or the backfill rule
    "finding_sentences": B("the finding's writer (seat or rule); a backfill row names rule:citation_rows_backfill"),
    "citation_support": B("the finding's writer (seat or rule); a backfill row names rule:citation_rows_backfill"),
    "citation_unresolved": B("the finding's writer (seat or rule); a backfill row names rule:citation_rows_backfill"),
    "run_handed": B("the finding's writer (seat or rule); a backfill row names rule:citation_rows_backfill"),
    "finding_citation_receipts": B("the finding's writer (seat or rule); a backfill row names rule:citation_rows_backfill"),
    "indicator_flips": C("rule:indicator_flips", "confirmed_by names the confirming finding, not a writer"),
    # --- forecasts and calibration --------------------------------------
    "acute_forecasts": C("rule:forecast_acute", "one writer"),
    "forecast_questions": B("the loader names itself; created_by is the question's stated author"),
    "forecast_numbers": B("the loader or the arms script names itself; issuer_kind/issuer_ref say whose number"),
    "forecast_resolutions": B("the resolving person (the request principal behind resolved_by)"),
    "forecast_resolution_proposals": C("rule:forecast_resolution", "empty; no writer in the tree"),
    "forecast_resolution_records": A("actor, actor_role"),
    "scorecard_entries": A("entered_by", "empty; no writer in the tree"),
    "override_log": A("actor"),
    "band_calibration_claims": C("rule:band_calibration_tracker"),
    "band_calibration_scan_state": C("rule:band_calibration_tracker"),
    # --- verification and correctness -----------------------------------
    "claim_contentions": A("analyst_id, pipeline_version"),
    "unit_correctness": A("analyst_id"),
    "unit_correctness_claims": A("correctness_id", "written with its parent unit_correctness row"),
    "unit_references": A("builder"),
    "unit_reference_labels": B(
        "reference answers: a person through the labels route, or the desk_reference rule",
        "src/legba/data/analysts/deterministic_handlers/_reference_diff.py",
    ),
    "correctness_labels": B("grading labels: a person through the gold-set route, or the sampling rule"),
    "external_grades": A("analyst_id, grader_component_id, grader_model_name"),
    "goldset_week_samples": C("rule:goldset_sampler", "the weekly sample draw"),
    "grader_calibrations": C("rule:correctness_regate", "written by the regate and loader scripts"),
    "evidence_drift_flags": A("reviewed_by, resolved_by, reopened_by", "code-stamped principals of the seat tools and the auto-close rule"),
    "evidence_drift_classifications": C("rule:evidence_drift", "classifier_version names the rule's version"),
    "evidence_drift_assessments": C("rule:evidence_drift"),
    "evidence_drift_claim_effects": C("rule:evidence_drift"),
    "evidence_drift_would_auto_close": C("rule:evidence_drift"),
    "evidence_drift_auto_closes": A("principal"),
    "evidence_rederive_requests": A("requested_by, taken_by"),
    "eval_runs": A("published_by, withdrawn_by", "validated against the audit-actor shape at publish"),
    "eval_run_arms": A("run_id", "written with its parent eval_runs row"),
    # --- alerts ---------------------------------------------------------
    "alerts": A("raised_by, acked_by, snoozed_by", "acks and snoozes from the request principal; each act also in alert_episode_actions"),
    "alert_events": A("raised_by"),
    "alert_episode_actions": A("actor, actor_kind, actor_role", "every acknowledge / snooze, stamped from the request principal"),
    "alert_sink_deliveries": C("rule:alert_delivery", "one row per delivery attempt"),
    "alert_trigger_watermarks": C("rule:alert_trigger_scan"),
    # --- consult, users, reading ----------------------------------------
    "consult_sessions": A("principal", "the server-stamped actor that opened the session"),
    "consult_turns": A("session_id", "a turn belongs to its session's principal"),
    "consult_writes": B(
        "a Consult change names the person whose session armed it",
        "src/legba/data/registry/consult_write_analyst.py",
    ),
    "watchlist": B("a person's watch list: the request principal (created_by stays the row's owner)"),
    "users": A("invited_by", "the bootstrap command or the inviting admin"),
    "user_credentials": A("user_id"),
    "user_sessions": A("user_id"),
    "user_invites": A("invited_by"),
    "user_seen_cursors": A("user_id"),
    "read_events": B("whose reading it was: the request principal"),
    # --- runtime and ops bookkeeping ------------------------------------
    "trigger_state": A("analyst_id"),
    "budget_ledger": A(_SEAT),
    "global_budget_envelope": C("rule:budget"),
    "budget_demotion_events": A(_SEAT),
    "governor_events": A("requested_by"),
    "decision_shadow_ledger": A("analyst_id"),
    "audit_checkpoints": A("analyst_id"),
    "action_pack_invocations": A("requested_by", "the invoking seat or Consult turn"),
    "profile_projection": C("rule:profile_projection", "derived"),
    # --- runtime DDL (schema_hygiene.RUNTIME_CREATED) -------------------
    "legba_data_migrations": C("rule:migrate", "the migration runner's ledger"),
    "actor_state": C("service:runtime", "runtime actor state"),
    "actor_filter_state": C("service:runtime", "runtime filter state"),
    "legba_jobs": A("requested_by"),
    # --- this lane --------------------------------------------------------
    "principals": C("rule:legba_principal", "created on first use by the legba_principal() function"),
    "origin_groups": B("derived groups name the derivation rule; an admin's new group names the admin"),
    "origins": B("derived origins name the derivation rule; a correction names the admin"),
    "origin_assignment_log": B("every assignment, derived or corrected, with its principal (NOT NULL)"),
    # --- the held family (migration 20261115T1940_held_record) ----
    "assertions": B("rule:held_record_writer (written_by NOT NULL); a declared entry will name its person"),
    "assertion_versions": B("rule:held_record_writer; proposed_by names the desk seat when the run's version is known"),
    "assertion_status_log": A("caused_by", "the principal that caused each status change (NOT NULL), chained by the database"),
    "assertion_status": C("rule:held_record_writer", "a cache the log's trigger maintains; rebuildable from the log"),
    "assertion_support": B("rule:held_record_writer (written_by NOT NULL)"),
    "assertion_shadow_receipts": B("rule:held_record_writer (written_by NOT NULL)"),
}

#: Every classified table (a glob key covers partitions), read-only.
TABLES: Mapping[str, Attribution] = MappingProxyType(_TABLES)


def by_class(cls: str) -> dict[str, Attribution]:
    """The tables of one class, by name."""
    return {name: a for name, a in TABLES.items() if a.cls == cls}
