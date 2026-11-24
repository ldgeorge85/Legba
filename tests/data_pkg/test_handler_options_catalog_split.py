# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H15 (wave G) — the ``handler_options.py`` catalog split is byte-identical.

**The defect this guards against.** ``handler_options.py`` sat AT its
module-size-gate ceiling (1,660 lines): every lane that added a knob or a
kind fought the line count. This split moves the two assembled catalogs —
``HANDLER_OPTIONS`` (the sub-handler catalog) and ``ANALYST_KIND_OPTIONS``
(the analyst-kind catalog), plus their catalog-local spec constructors and
the splice-in of the events/inquiry/programs sibling catalogs — into a new
sibling, ``handler_options_catalog.py``. ``handler_options.py`` keeps only
the ``OptionSpec`` machinery (the reserved-key table, the dataclasses, the
resolve/degrade traversal) and imports both dicts back, re-exported under
the same names.

**The proof.** A pure move must change NOTHING an importer can observe: same
keys, same ``OptionSpec`` field values (name/kind/doc/minimum/maximum/
minimum_inclusive/choices/pattern) for every spec in both dicts, in the same
order — which is exactly what every ``known_option_names()`` /
``known_kind_option_names()`` answer is computed from. Rather than asserting
against a hand-copied expectation (which could silently drift from the real
pre-split catalog the same way the module's own docstring warns unsynced
duplicate constants do), this test pins a SHA-256 fingerprint of both dicts
taken from ``handler_options.py`` at pin ``79fd1fd9`` — the commit
immediately BEFORE this split — as the frozen contract. If the fingerprint
here ever needs to change, it means the catalog's CONTENT changed and this
file's frozen value must be re-derived from a deliberate, reviewed diff, not
quietly updated to make the test pass.
"""
from __future__ import annotations

import hashlib
import json

from legba.data.option_catalog import handler_options, handler_options_catalog
from legba.data.option_catalog.handler_options import ANALYST_KIND_OPTIONS, HANDLER_OPTIONS

#: SHA-256 of the canonical JSON below, computed from ``handler_options.py``
#: at pin 79fd1fd9 (portfolio-ingestion-v2) — the pre-split module, before
#: ``HANDLER_OPTIONS`` / ``ANALYST_KIND_OPTIONS`` moved to this sibling.
FROZEN_FINGERPRINT = (
    "b5c601f48d62d99085b007e398dbd443530740022d33e88b04e6862db7436984"
)

#: The pre-split catalog sizes, pinned alongside the fingerprint so a key
#: count regression fails with a legible number rather than just a hash diff.
FROZEN_HANDLER_OPTIONS_KEY_COUNT = 52
FROZEN_ANALYST_KIND_OPTIONS_KEY_COUNT = 7

#: Deliberate catalog ADDITIONS landed after the 79fd1fd9 pin, as
#: ``(catalog name, catalog key, option name)``.
#:
#: The fingerprint test strips these before hashing rather than re-deriving
#: :data:`FROZEN_FINGERPRINT`, and that is the point: a pin re-derived on every
#: lane that declares a knob stops being a pin. Stripping keeps the frozen
#: digest meaning exactly what it meant the day it was taken — "every
#: pre-existing spec, field for field, in order" — while each post-pin knob
#: stays a named, reviewable line in a diff instead of a changed hash.
#:
#: Adding a knob? Append it here in the same commit, and assert what it DOES
#: in that lane's own test file. Removing one? Delete its line here too —
#: :func:`test_declared_post_pin_additions_are_really_present` fails on a
#: stripper entry that no longer strips anything.
POST_PIN_ADDITIONS: tuple[tuple[str, str, str], ...] = (
    # wave I / p2_offer — the V3/P2 OPEN EVENTS desk-grounding block: the
    # per-analyst GRANT and its bound. Read by the slice reader; proven in
    # tests/data_pkg/test_unit_grounding.py.
    # lane K-2 — a desk's declared FEED ROSTER (feed_roster_block.py): per
    # feed, its kind, its collected rows in the window and its slice ordinals.
    # Proven in tests/data_pkg/test_cyber_reader_k2.py.
    # lane P-7 — the graph projector's cost knobs: the arc diet (two groups,
    # default off), the unlogged build, skip-when-unchanged and its age
    # ceiling. Proven in tests/data_pkg/test_graph_projector.py.
    ("HANDLER_OPTIONS", "graph_projector", "include_contention_arcs"),
    ("HANDLER_OPTIONS", "graph_projector", "include_lineage_copies"),
    ("HANDLER_OPTIONS", "graph_projector", "unlogged"),
    ("HANDLER_OPTIONS", "graph_projector", "skip_when_unchanged"),
    ("HANDLER_OPTIONS", "graph_projector", "max_skip_seconds"),
    ("ANALYST_KIND_OPTIONS", "inline_target", "feed_roster"),
    ("ANALYST_KIND_OPTIONS", "inline_target", "offer_events"),
    ("ANALYST_KIND_OPTIONS", "inline_target", "open_events_limit"),
    # wave M / 7g-2 — the HISTORICAL SERIES desk-grounding block: the second
    # per-analyst GRANT and its bound, on exactly the terms of the first.
    # Read by the slice reader; proven in
    # tests/data_pkg/test_history_grounding.py.
    ("ANALYST_KIND_OPTIONS", "inline_target", "offer_history"),
    ("ANALYST_KIND_OPTIONS", "inline_target", "history_series_limit"),
    # wave W4 / F-3 — the world read's subject-group floor: the stale bar for a
    # block the floor kept (decision D4, default 72 h). Labels only. Proven in
    # tests/data_pkg/test_assembly_floor.py.
    ("ANALYST_KIND_OPTIONS", "meta_findings_synthesizer", "world_floor_stale_hours"),
    # wave W8 / TAX-1 — the per-source diversity cap on a scoped slice, read by
    # both slice readers through data/signal_slice_scope.py. Proven in
    # tests/data_pkg/test_signal_tag_slice.py (the cross_target_raw key below
    # carries the same spec).
    ("ANALYST_KIND_OPTIONS", "inline_target", "slice_per_source_cap"),
    # wave X3 / W11-F55 — the indicator_tracker flip debounce: the run count
    # and the span at which a status holds. Proven in
    # tests/data_pkg/test_analyst_indicator_tracker.py.
    ("HANDLER_OPTIONS", "indicator_tracker", "hysteresis_runs"),
    ("HANDLER_OPTIONS", "indicator_tracker", "hysteresis_hours"),
    # wave X4 / LEVEL (W11-F36) — which of a unit's canonical indicators
    # measure a LEVEL, and against what reference. Read by inline_target's
    # run_method through data/indicator_shape.py. Proven in
    # tests/data_pkg/test_level_indicators.py.
    ("ANALYST_KIND_OPTIONS", "inline_target", "level_indicators"),
    # W11-F108 — the claim_watch bearing gate's decision-model SHADOW: the
    # component it asks and its per-run call cap. Off in code; moves no
    # decision. Proven in tests/runtime/test_bearing_shadow.py.
    ("HANDLER_OPTIONS", "claim_watch", "bearing_shadow_ref"),
    ("HANDLER_OPTIONS", "claim_watch", "bearing_shadow_cap"),
    # W11-F91 — the multi-round GATHER evidence cap on an inline_target unit
    # (derived from the input budget when absent; 0 = off). Proven in
    # tests/data_pkg/test_gather_context_budget.py.
    ("ANALYST_KIND_OPTIONS", "inline_target", "evidence_char_cap"),
    # 2026-11-09 clusterer-watermark — the signal slice's adaptive page floor
    # and its one-time catch-up floor. Proven in
    # tests/data_pkg/test_event_clustering_slice_progress.py.
    ("HANDLER_OPTIONS", "event_clustering", "min_signals_per_run"),
    ("HANDLER_OPTIONS", "event_clustering", "catch_up_floor_hours"),
    # N-Z (2026-11-14) — the catch-up split, multi-page passes; proven in
    # tests/data_pkg/test_event_clustering_backlog_n_z.py.
    ("HANDLER_OPTIONS", "event_clustering", "catch_up_lag_minutes"),
    ("HANDLER_OPTIONS", "event_clustering", "catch_up_head_minutes"),
    ("HANDLER_OPTIONS", "event_clustering", "max_pages_per_pass"),
    # lane N-B — the desk slice's WATERMARK window and its two floors. Read by
    # the slice reader through data/slice_coverage.py. Proven in
    # tests/data_pkg/test_slice_coverage.py.
    ("ANALYST_KIND_OPTIONS", "inline_target", "slice_since_watermark"),
    ("ANALYST_KIND_OPTIONS", "inline_target", "slice_watermark_min_hours"),
    ("ANALYST_KIND_OPTIONS", "inline_target", "slice_watermark_first_run_hours"),
    # lane N-L — the evidence archiver archives the bytes the POLL kept (its
    # fetch receipt) instead of re-fetching, default on. Proven in
    # tests/data_pkg/test_evidence_archiver*.py (the default path) and
    # tests/data_pkg/test_ingestion_series_acceptance.py.
    ("HANDLER_OPTIONS", "evidence_archiver", "prefer_ingest_copy"),
    # lane I-1 — the archiver's re-fetch routed through the ingestion intake
    # (default off), the page pipeline it runs under, and the wait before the
    # archiver falls back to its own fetch. Proven in
    # tests/data_pkg/test_ingestion_intake.py.
    ("HANDLER_OPTIONS", "evidence_archiver", "fetch_via_intake"),
    ("HANDLER_OPTIONS", "evidence_archiver", "intake_pipeline"),
    ("HANDLER_OPTIONS", "evidence_archiver", "intake_wait_minutes"),
)

#: Knobs REMOVED after the pin, as ``(catalog name, catalog key, index, name,
#: doc)``: the spec as it stood at the pin (a bool ``_flag``), re-inserted at its
#: original index before hashing, so the frozen digest keeps meaning "every
#: pre-existing spec, field for field, in order" while each removal stays a named
#: line in a diff. :func:`test_declared_post_pin_removals_are_really_gone` fails
#: on an entry whose knob is still in the catalog.
POST_PIN_REMOVALS: tuple[tuple[str, str, int, str, str], ...] = (
    # 2026-11-15 DM-G - the Apache AGE graph held no row and was dropped; its two
    # readers (graph_mining, structural_balance) and their `augment_from_age`
    # knob went with it. No live descriptor carried the option.
    ("HANDLER_OPTIONS", "graph_mining", 1, "augment_from_age",
     "Augment the mined graph from Apache AGE."),
    ("HANDLER_OPTIONS", "structural_balance", 1, "augment_from_age",
     "Augment the signed graph from Apache AGE."),
)

#: Whole catalog KEYS added after the pin — a new sub-handler or analyst kind,
#: as ``(catalog name, catalog key)``.
#:
#: A key is stripped differently from a knob and needs its own list: stripping
#: every spec out of a new key would leave the key behind with an empty list,
#: which changes the digest and the count for a reason the pin was never about.
#: Same discipline either way — a named, reviewable line per addition, and
#: :func:`test_declared_post_pin_keys_are_really_present` fails on an entry that
#: no longer strips anything, so a removed sub-handler cannot quietly restore
#: the pin while the catalog has in fact moved twice.
POST_PIN_KEYS: tuple[tuple[str, str], ...] = (
    # lane N-L — the series_change detector declares its rule knobs
    # (`current_days`, `baseline_days`, `n_sigma`, `min_current`,
    # `min_baseline_points`, `source_ids`). Proven in
    # tests/runtime/test_ingestion_sinks.py and
    # tests/data_pkg/test_ingestion_series_acceptance.py.
    ("HANDLER_OPTIONS", "series_change"),
    # lane K-2 — the KEV digest declares its three knobs (`source_ids`,
    # `initial_lookback_hours`, `max_entries`). Proven in
    # tests/data_pkg/test_cyber_reader_k2.py.
    ("HANDLER_OPTIONS", "kev_digest"),
    # lane E-1 — the due-and-absent detector's seven knobs (which types,
    # each date's tolerance, the calendar's look-back and key-hit floor, the
    # per-type read cap). Proven in tests/data_pkg/test_due_and_absent.py.
    ("HANDLER_OPTIONS", "due_and_absent"),
    # wave M / 7a — the CONTRARY-EVIDENCE pass. Seven knobs, six of which bound
    # a cost or a record's shelf life; `paid_rung` is the only one that can
    # cause a charge and it ships OFF, fenced twice (the descriptor's value and
    # a code-side truncation of the ladder to rung 0). Proven in
    # tests/data_pkg/test_contrary_evidence_pass.py.
    ("HANDLER_OPTIONS", "contrary_evidence_pass"),
    # wave Q / the consult review (2026-09-28) — the competing_hypotheses kind
    # declares its one knob, `enable_status_transition_resolution`: the
    # self-consistency resolution sweep's documented opt-out, which had never
    # been declared and so could neither validate nor reach the handler.
    # Proven in tests/data_pkg/test_meta_hypothesis_descriptors.py (the
    # descriptor carrying it validates) and test_competing_hypotheses.py.
    ("ANALYST_KIND_OPTIONS", "competing_hypotheses"),
    # wave W1 / SW-12 — the evidence archive's expiry sweep declares its three
    # knobs (`retention_days`, `batch_limit`, `dry_run`). The sweep itself ships
    # OFF behind LEGBA_ARCHIVE_RETENTION. Proven in
    # tests/data_pkg/test_archive_retention.py.
    ("HANDLER_OPTIONS", "archive_retention"),
    # wave W5 / WATCH-CLOSE — the consult-watch horizon sweep declares its two
    # knobs (`batch_limit`, `dry_run`). Proven in
    # tests/data_pkg/test_watch_horizon_sweep.py.
    ("HANDLER_OPTIONS", "watch_horizon_sweep"),
    # wave W2 / F-6 — the consult kind declares `context_ceiling_tokens`, the
    # input ceiling its calls (the final synthesis above all) honour. Proven in
    # tests/data_pkg/test_consult_context_ceiling.py. Wave W3 / F-4 adds
    # `mint_watch_candidates` under the same key (the whole key is post-pin,
    # so this one line strips both); proven in
    # tests/data_pkg/test_consult_watch_mint.py.
    ("ANALYST_KIND_OPTIONS", "consult_on_demand"),
    # wave W8 / TAX-1 — the cross_target_raw kind declares its one knob,
    # `slice_per_source_cap` (the same spec inline_target carries). Proven in
    # tests/data_pkg/test_signal_tag_slice.py.
    ("ANALYST_KIND_OPTIONS", "cross_target_raw"),
    # wave W9 / VIEW-1 — the profile projector declares its one knob,
    # `dry_run` (build every plane's rows, write nothing). Proven in
    # tests/data_pkg/test_profile_projection.py.
    ("HANDLER_OPTIONS", "profile_projector"),
    # wave W11 / INST-1 — the keyed (deterministic) reference kind declares
    # its list shape and bindings (`feed_*`, `key_*`, `date_field`,
    # `subject_field`, `list_complete`, `bindings`, …). Proven in
    # tests/data_pkg/test_inst1_non_country_correctness.py.
    ("HANDLER_OPTIONS", "reference_deterministic"),
    # W11-F118 — the pending-enrichment sweep declares its one knob,
    # `pending_batch` (rows re-enriched per run). Declared in
    # handler_options_enrichment.py. Proven in
    # tests/data_pkg/test_analyst_reenrich_pending.py.
    ("HANDLER_OPTIONS", "reenrich_pending"),
    # W11-F84 — the verify-backfill sweep declares `backfill_batch` and
    # `min_age_minutes`. Declared in handler_options_enrichment.py. Proven in
    # tests/data_pkg/test_analyst_verify_backfill.py.
    ("HANDLER_OPTIONS", "verify_backfill"),
    # W11-F138 — the challenge-block re-fetch sweep declares
    # `refetch_max_per_tick` and `refetch_window_days`. Declared in
    # handler_options_enrichment.py. Proven in
    # tests/data_pkg/test_fetch_policy.py.
    ("HANDLER_OPTIONS", "evidence_refetch"),
    # ST1-A — the forecast resolver declares NO knob (explicit empty tuple in
    # handler_options_inquiry.py). Proven in
    # tests/data_pkg/test_forecast_resolution.py.
    ("HANDLER_OPTIONS", "forecast_resolver"),
    # R-1 (type registry): from here on a NEW deterministic handler declares
    # its knobs in its own declaration module (``input_schema=``) and they are
    # spliced into HANDLER_OPTIONS by handler_options_declared.py. Those keys
    # are stripped automatically (see _registry_declared_keys) — no line here.
)


def _registry_declared_keys() -> tuple[tuple[str, str], ...]:
    """Keys whose knobs live in a type-registry declaration, not the catalogue.

    Derived, not listed: adding a handler that declares its own options never
    edits this file. The frozen digest still covers every pre-existing key."""
    from legba.data.option_catalog.handler_options_declared import (
        declared_handler_option_ids,
    )

    return tuple(("HANDLER_OPTIONS", k) for k in declared_handler_option_ids())


def _strip_post_pin_additions(
    handler_options_dict: dict, analyst_kind_options_dict: dict,
) -> tuple[dict, dict]:
    """Both catalogs as they stood at the pin — post-pin knobs removed."""
    stripped = {
        "HANDLER_OPTIONS": {k: list(v) for k, v in handler_options_dict.items()},
        "ANALYST_KIND_OPTIONS": {
            k: list(v) for k, v in analyst_kind_options_dict.items()
        },
    }
    for catalog, key, name in POST_PIN_ADDITIONS:
        specs = stripped[catalog][key]
        before = len(specs)
        stripped[catalog][key] = [sp for sp in specs if sp.name != name]
        assert len(stripped[catalog][key]) == before - 1, (
            f"POST_PIN_ADDITIONS names {catalog}[{key}].{name}, which is not "
            "in the catalog — delete the line if the knob was removed"
        )
    from legba.data.option_catalog.handler_options_base import OptionSpec

    for catalog, key, index, name, doc in POST_PIN_REMOVALS:
        specs = stripped[catalog][key]
        assert name not in {sp.name for sp in specs}, (
            f"POST_PIN_REMOVALS names {catalog}[{key}].{name}, which is still "
            "in the catalog - delete the line if the knob came back"
        )
        specs.insert(index, OptionSpec(name, "bool", doc))
    for catalog, key in POST_PIN_KEYS + _registry_declared_keys():
        assert stripped[catalog].pop(key, None) is not None, (
            f"POST_PIN_KEYS names {catalog}[{key}], which is not in the "
            "catalog — delete the line if the sub-handler was removed"
        )
    return stripped["HANDLER_OPTIONS"], stripped["ANALYST_KIND_OPTIONS"]


def _spec_tuple(spec: object) -> tuple:
    """Every field ``OptionSpec.validate`` can act on, in a JSON-safe shape."""
    return (
        spec.name,
        spec.kind,
        spec.doc,
        spec.minimum,
        spec.maximum,
        spec.minimum_inclusive,
        list(spec.choices) if spec.choices is not None else None,
        spec.pattern.pattern if spec.pattern is not None else None,
    )


def _canon(catalog: dict) -> dict:
    return {key: [_spec_tuple(spec) for spec in specs] for key, specs in catalog.items()}


def _fingerprint(handler_options_catalog_dict, analyst_kind_options_dict) -> str:
    payload = {
        "HANDLER_OPTIONS": _canon(handler_options_catalog_dict),
        "ANALYST_KIND_OPTIONS": _canon(analyst_kind_options_dict),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def test_assembled_catalogs_match_the_pre_split_fingerprint():
    """Every key and every ``OptionSpec`` field, byte-identical to the pin —
    once the knobs DECLARED in :data:`POST_PIN_ADDITIONS` are set aside."""
    digest = _fingerprint(*_strip_post_pin_additions(
        HANDLER_OPTIONS, ANALYST_KIND_OPTIONS,
    ))
    assert digest == FROZEN_FINGERPRINT, (
        "handler_options.HANDLER_OPTIONS / ANALYST_KIND_OPTIONS no longer "
        "match the pre-split (79fd1fd9) fingerprint — an UNDECLARED catalog "
        "change. If it is deliberate, declare the added knob(s) in "
        "POST_PIN_ADDITIONS and assert what they do in the owning lane's "
        "test file; never re-derive FROZEN_FINGERPRINT to make this pass, "
        "which would empty the pin of the content it exists to hold"
    )


def test_declared_post_pin_additions_are_really_present():
    """The stripper must never hide a knob that is already gone — that would
    silently restore the pin while the catalog had in fact moved twice."""
    for catalog_name, key, name in POST_PIN_ADDITIONS:
        catalog = {
            "HANDLER_OPTIONS": HANDLER_OPTIONS,
            "ANALYST_KIND_OPTIONS": ANALYST_KIND_OPTIONS,
        }[catalog_name]
        assert name in {sp.name for sp in catalog[key]}, (
            f"{catalog_name}[{key}].{name} is declared post-pin but absent"
        )


def test_declared_post_pin_keys_are_really_present():
    """A declared post-pin KEY must actually be in the catalog, for the same
    reason a declared knob must: a stripper line that strips nothing silently
    restores the pin over a catalog that has moved."""
    for catalog_name, key in POST_PIN_KEYS:
        catalog = {
            "HANDLER_OPTIONS": HANDLER_OPTIONS,
            "ANALYST_KIND_OPTIONS": ANALYST_KIND_OPTIONS,
        }[catalog_name]
        assert key in catalog, (
            f"{catalog_name}[{key}] is declared post-pin but absent"
        )


def test_catalog_key_counts_match_the_pin():
    """The pin's counts, plus the keys DECLARED in :data:`POST_PIN_KEYS` — so
    a new sub-handler shows up here as a named line rather than as a bumped
    number nobody can date."""
    post_pin = {c: 0 for c in ("HANDLER_OPTIONS", "ANALYST_KIND_OPTIONS")}
    for catalog_name, _key in POST_PIN_KEYS + _registry_declared_keys():
        post_pin[catalog_name] += 1
    assert len(HANDLER_OPTIONS) == (
        FROZEN_HANDLER_OPTIONS_KEY_COUNT + post_pin["HANDLER_OPTIONS"]
    )
    assert len(ANALYST_KIND_OPTIONS) == (
        FROZEN_ANALYST_KIND_OPTIONS_KEY_COUNT + post_pin["ANALYST_KIND_OPTIONS"]
    )


def test_handler_options_reexports_the_catalog_module_objects_not_copies():
    """A copy could silently drift from the catalog it was copied from — the
    re-export must be the SAME dict object handler_options_catalog built."""
    assert handler_options.HANDLER_OPTIONS is handler_options_catalog.HANDLER_OPTIONS
    assert (
        handler_options.ANALYST_KIND_OPTIONS
        is handler_options_catalog.ANALYST_KIND_OPTIONS
    )


def test_known_option_names_spot_checks_survive_the_move():
    """A handful of names from opposite ends of the pre-split catalog (the
    reachability sweep in test_handler_options_x1.py covers the rest)."""
    assert "per_desk_cap" in handler_options.known_option_names("alert_trigger_scan")
    assert "ttl_days" in handler_options.known_option_names("analyst_traces_retention")
    assert "slice_focus" in handler_options.known_kind_option_names("inline_target")
    assert (
        "judge_sample_rate"
        in handler_options.known_kind_option_names("meta_findings_synthesizer")
    )
    assert (
        "qualification_bar"
        in handler_options.known_kind_option_names("relationship_reifier")
    )


def test_declared_post_pin_removals_are_really_gone():
    for catalog_name, key, _index, name, _doc in POST_PIN_REMOVALS:
        catalog = {
            "HANDLER_OPTIONS": HANDLER_OPTIONS,
            "ANALYST_KIND_OPTIONS": ANALYST_KIND_OPTIONS,
        }[catalog_name]
        assert name not in {sp.name for sp in catalog[key]}
