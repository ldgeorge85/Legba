# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard: a SNAPSHOT catalog feed carries no NER stage.

A snapshot feed (NWS active alerts, GDACS) returns the FULL current alert set
on every poll. Enrichment runs before the write (``run_baseline`` then
``write_canonical_signal``'s content-hash collapse), so an ``ner_multilingual``
stage on such a feed POSTs the models host once per re-offered item and the row
then collapses — measured at roughly 310 (NWS) + 100 (GDACS) offered items per day, ~190 of them new.
The catalog tags those feeds ``snapshot=True``; this test pins (a) the two
known feeds carry the tag, (b) EVERY snapshot-tagged feed's built descriptor
has no NER-class stage (``ner_multilingual`` or the NER-dependent
``fact_extractor``), and (c) the builder refuses a snapshot feed that opts into
``fact_extract``. No live HTTP, no DB.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import sys

import pytest

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts"
_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    from bringup_register_source_catalog import CATALOG, build_descriptor  # noqa: E402
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)

_NER_CLASS_STAGES = frozenset({"ner_multilingual", "fact_extractor"})
_KNOWN_SNAPSHOT_IDS = {"source.gdacs.alerts"}
# NWS is snapshot-shaped but keeps NER: ~20% of its signals (null/marine
# polygons) resolve geo only through NER place entities, and NER is its sole
# entity source (SRC-5 measurement, 2026-10-03). It must NOT be tagged.
_SNAPSHOT_SHAPED_BUT_NER_DEPENDENT = {"source.nws.active_alerts"}


def _kinds(entry) -> list[str]:
    return [s.kind for s in build_descriptor(entry).pipeline.enrichment]


def test_known_snapshot_feeds_are_tagged():
    tagged = {e.id for e in CATALOG if e.snapshot}
    assert _KNOWN_SNAPSHOT_IDS <= tagged, _KNOWN_SNAPSHOT_IDS - tagged


@pytest.mark.parametrize(
    "entry", [e for e in CATALOG if e.snapshot], ids=lambda e: e.id
)
def test_snapshot_feed_carries_no_ner_stage(entry):
    kinds = _kinds(entry)
    assert not (_NER_CLASS_STAGES & set(kinds)), (entry.id, kinds)
    # geocode stays: the feeds' own geometry / location fields drive geo.
    assert "geocode" in kinds, (entry.id, kinds)


def test_snapshot_feed_pipelines_are_exactly_the_lean_chain():
    by_id = {e.id: e for e in CATALOG}
    assert _kinds(by_id["source.gdacs.alerts"]) == ["language_detect", "geocode"]


def test_ner_dependent_snapshot_shaped_feeds_keep_ner():
    by_id = {e.id: e for e in CATALOG}
    for sid in _SNAPSHOT_SHAPED_BUT_NER_DEPENDENT:
        assert by_id[sid].snapshot is False, sid
        assert "ner_multilingual" in _kinds(by_id[sid]), sid


def test_non_snapshot_text_feeds_keep_ner():
    """The tag must not leak: a plain rss news feed still runs NER."""
    plain = next(e for e in CATALOG if e.kind == "rss" and not e.snapshot)
    assert "ner_multilingual" in _kinds(plain)


def test_snapshot_feed_cannot_opt_into_fact_extract():
    entry = next(e for e in CATALOG if e.id == "source.gdacs.alerts")
    with pytest.raises(ValueError, match="snapshot feed cannot opt into fact_extract"):
        build_descriptor(dataclasses.replace(entry, fact_extract=True))
