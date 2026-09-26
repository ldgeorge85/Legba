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

from legba.data.analysts import handler_options, handler_options_catalog
from legba.data.analysts.handler_options import ANALYST_KIND_OPTIONS, HANDLER_OPTIONS

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
    ("ANALYST_KIND_OPTIONS", "inline_target", "offer_events"),
    ("ANALYST_KIND_OPTIONS", "inline_target", "open_events_limit"),
    # wave M / 7g-2 — the HISTORICAL SERIES desk-grounding block: the second
    # per-analyst GRANT and its bound, on exactly the terms of the first.
    # Read by the slice reader; proven in
    # tests/data_pkg/test_history_grounding.py.
    ("ANALYST_KIND_OPTIONS", "inline_target", "offer_history"),
    ("ANALYST_KIND_OPTIONS", "inline_target", "history_series_limit"),
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
    # wave M / 7a — the CONTRARY-EVIDENCE pass. Seven knobs, six of which bound
    # a cost or a record's shelf life; `paid_rung` is the only one that can
    # cause a charge and it ships OFF, fenced twice (the descriptor's value and
    # a code-side truncation of the ladder to rung 0). Proven in
    # tests/data_pkg/test_contrary_evidence_pass.py.
    ("HANDLER_OPTIONS", "contrary_evidence_pass"),
)


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
    for catalog, key in POST_PIN_KEYS:
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
    for catalog_name, _key in POST_PIN_KEYS:
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
