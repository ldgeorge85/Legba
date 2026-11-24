# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X2/E2 — the ``instrument_snapshot`` source kind.

The DONE line, as assertions:

  * a fixture PAIR of SDN-shaped XML snapshots produces the EXACT added /
    removed / changed sets, keyed on the list's own ``uid`` (a re-indented
    entry is not a change);
  * a fixture with NO change produces the ``checked, unchanged`` receipt
    (naming the ``instrument_unchanged`` typed-absence kind) and NO signal;
  * a change emits ONE dated signal carrying both hashes, the diff, the entry
    counts before/after and the instrument's own published date, with BOTH
    snapshots (and the full diff) on the archive;
  * the licence path: a class that forbids retention cannot bind the kind,
    and the kind's forbid set is the evidence archiver's;
  * the three DRAFT bindings in profiles/sanctions_export_control validate,
    build a handler through the source factory, and declare the same licence
    in scope and config.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
import pytest
import yaml
from pydantic import ValidationError

from legba.data.archive import cas_path
from legba.data import archive_codec
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources._contract import InMemoryStateStore, Signal, SourceContext, SourceHandler
from legba.data.sources.instrument_diff import (
    InstrumentParseError,
    cap_diff,
    diff_instruments,
    parse_csv_records,
    parse_text,
    parse_xml_records,
)
from legba.data.sources.instrument_snapshot import (
    INSTRUMENT_SNAPSHOT_MAX_BYTES,
    RETENTION_FORBIDDEN_LICENSE_CLASSES,
    UNCHANGED_ABSENCE_KIND,
    InstrumentArchiveUnavailable,
    InstrumentSnapshotConfig,
    InstrumentSnapshotSourceHandler,
    load_object,
)
from legba.runtime.source_factory import (
    _unwrap_factory_dict,
    build_source_handler,
    discover_source_kinds,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "instrument_snapshot"
PROFILE_DESCRIPTORS = REPO_ROOT / "profiles" / "sanctions_export_control" / "descriptors"
SDN_A = (FIXTURES / "sdn_a.xml").read_bytes()
SDN_B = (FIXTURES / "sdn_b.xml").read_bytes()
URL = "https://lists.example.test/sdn.xml"
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

SDN_READING: dict[str, Any] = {
    "differ": "xml_records",
    "record_tag": "sdnEntry",
    "key_path": "uid",
    "label_paths": ["lastName", "firstName"],
    "summary_paths": ["sdnType", "programList/program"],
    "published_date_path": "publshInformation/Publish_Date",
    "published_date_format": "%m/%d/%Y",
    "declared_count_path": "publshInformation/Record_Count",
}


def _cfg(**over: Any) -> InstrumentSnapshotConfig:
    body = {"instrument_name": "Test SDN list", "url": URL, "license_class": "public_domain",
            **SDN_READING}
    body.update(over)
    return InstrumentSnapshotConfig.model_validate(body)


def _ctx(cfg: InstrumentSnapshotConfig, store: InMemoryStateStore, now: datetime = T0) -> SourceContext:
    return SourceContext(
        target_id="source.test.sdn", target_version="v0", source_id="source.test.sdn",
        config=cfg, state_store=store, now_fn=lambda: now,
        logger=logging.getLogger("test.instrument_snapshot"),
    )


class _Upstream:
    """Serves a queue of responses for the one instrument URL, recording requests."""

    def __init__(self, *responses: Callable[[httpx.Request], httpx.Response]) -> None:
        self._queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._queue.pop(0)(request)


def _ok(body: bytes, **headers: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(200, content=body, headers=headers)


def _handler(tmp_path: pathlib.Path, upstream: _Upstream) -> InstrumentSnapshotSourceHandler:
    h = InstrumentSnapshotSourceHandler(archive_root=tmp_path / "archive")
    h._http_client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return h


async def _pull(h: InstrumentSnapshotSourceHandler, ctx: SourceContext) -> list[Signal]:
    return [s async for s in h.pull(ctx)]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------------------
# The differ — the fixture pair yields the exact sets
# ---------------------------------------------------------------------------


def _parse(body: bytes):
    r = SDN_READING
    return parse_xml_records(
        body, record_tag=r["record_tag"], key_path=r["key_path"],
        label_paths=r["label_paths"], summary_paths=r["summary_paths"],
        published_date_path=r["published_date_path"],
        published_date_format=r["published_date_format"],
        declared_count_path=r["declared_count_path"],
    )


def test_fixture_pair_yields_the_exact_added_removed_changed_sets():
    a, b = _parse(SDN_A), _parse(SDN_B)
    assert (a.entry_count, b.entry_count) == (4, 4)
    assert (a.declared_count, b.declared_count) == (4, 4)
    assert (a.published_date, b.published_date) == ("2026-10-01", "2026-10-03")
    diff = diff_instruments(a, b)
    assert {e["key"] for e in diff["added"]} == {"1005"}
    assert {e["key"] for e in diff["removed"]} == {"1002"}
    assert {e["key"] for e in diff["changed"]} == {"1003"}       # 1004 re-indented: NOT changed
    assert diff["counts"] == {"added": 1, "removed": 1, "changed": 1}
    (changed,) = diff["changed"]
    assert changed["fields_changed"] == ["programList"]
    assert changed["summary"]["programList/program"] == ["EXAMPLE-PROGRAM-A", "EXAMPLE-PROGRAM-D"]
    assert changed["summary_before"]["programList/program"] == ["EXAMPLE-PROGRAM-A"]
    assert diff["removed"][0]["label"] == "SAMPLE, Alex"
    assert diff["added"][0]["label"] == "ILLUSTRATIVE HOLDINGS LLC"
    # Deterministic: the same pair, the same diff, byte for byte.
    assert json.dumps(diff, sort_keys=True) == json.dumps(diff_instruments(_parse(SDN_A), _parse(SDN_B)), sort_keys=True)


def test_identical_snapshot_diffs_to_nothing():
    diff = diff_instruments(_parse(SDN_A), _parse(SDN_A))
    assert diff["counts"] == {"added": 0, "removed": 0, "changed": 0}


def test_a_record_tag_matching_nothing_fails_loud_never_an_empty_list():
    with pytest.raises(InstrumentParseError, match="no <entry>"):
        parse_xml_records(SDN_A, record_tag="entry", key_path="uid")
    with pytest.raises(InstrumentParseError, match="xml"):
        parse_xml_records(b"<sdnList><sdnEntry>", record_tag="sdnEntry", key_path="uid")


def test_csv_and_text_differs():
    before = parse_csv_records(b"id,name\n1,A\n2,B\n", key_column="id", label_columns=["name"])
    after = parse_csv_records(b"id,name\n2,B2\n3,C\n", key_column="id", label_columns=["name"])
    d = diff_instruments(before, after)
    assert d["counts"] == {"added": 1, "removed": 1, "changed": 1}
    assert d["changed"][0]["fields_changed"] == ["name"]
    headerless = parse_csv_records(b"36,X\n", key_column="0", has_header=False)
    assert set(headerless.entries) == {"36"}
    t = diff_instruments(parse_text(b"a\nb\nc\n"), parse_text(b"a\nB\nc\nd\n"))
    assert t["counts"] == {"lines_added": 2, "lines_removed": 1}
    assert "-b" in t["unified_diff"] and "+B" in t["unified_diff"]


def test_cap_diff_keeps_counts_whole_and_says_it_cut():
    diff = {"kind": "records", "counts": {"added": 5}, "added": [{"key": str(i)} for i in range(5)]}
    capped = cap_diff(diff, max_entries=2, max_text_chars=1000)
    assert len(capped["added"]) == 2 and capped["counts"] == {"added": 5} and capped["truncated"]


# ---------------------------------------------------------------------------
# The handler — first snapshot, unchanged receipt, change signal
# ---------------------------------------------------------------------------


async def test_first_unchanged_changed_sequence(tmp_path):
    store = InMemoryStateStore()
    up = _Upstream(_ok(SDN_A), _ok(SDN_A), _ok(SDN_B))
    h, cfg = _handler(tmp_path, up), _cfg(conditional_get=False)
    root = tmp_path / "archive"

    # 1) first snapshot: one dated signal, the pointer set, the bytes archived
    (first,) = await _pull(h, _ctx(cfg, store))
    assert first.payload["change_kind"] == "first_snapshot"
    assert first.payload["current_sha256"] == _sha(SDN_A)
    assert first.payload["previous"] is None and first.payload["diff"] is None
    assert first.payload["published_date"] == "2026-10-01"
    assert load_object(root, _sha(SDN_A)) == SDN_A
    assert (await store.get("instrument_snapshot_state"))["sha256"] == _sha(SDN_A)

    # 2) the same bytes: NO signal, a checked-unchanged receipt
    later = T0 + timedelta(hours=6)
    assert await _pull(h, _ctx(cfg, store, later)) == []
    receipt = (await store.get("instrument_snapshot_receipts"))[0]
    assert receipt["outcome"] == "unchanged"
    assert receipt["typed_absence_kind"] == UNCHANGED_ABSENCE_KIND == "instrument_unchanged"
    assert receipt["basis"] == "sha256_equal" and receipt["sha256"] == _sha(SDN_A)
    assert receipt["checked_at"] == later.isoformat()
    health = await store.get("instrument_snapshot_health")
    assert health["state"] == "healthy" and health["detail"]["newest_entry_ts"] == T0.isoformat()

    # 3) the amended instrument: ONE change signal with the receipts
    (sig,) = await _pull(h, _ctx(cfg, store, later + timedelta(hours=6)))
    p = sig.payload
    assert p["change_kind"] == "changed"
    assert (p["previous_sha256"], p["current_sha256"]) == (_sha(SDN_A), _sha(SDN_B))
    assert (p["entry_count_before"], p["entry_count_after"]) == (4, 4)
    assert p["published_date"] == "2026-10-03" and p["previous"]["published_date"] == "2026-10-01"
    assert p["diff"]["counts"] == {"added": 1, "removed": 1, "changed": 1}
    assert [e["key"] for e in p["diff"]["added"]] == ["1005"]
    assert [e["key"] for e in p["diff"]["removed"]] == ["1002"]
    assert [e["key"] for e in p["diff"]["changed"]] == ["1003"]
    assert p["diff"]["truncated"] is False
    assert p["license_class"] == "public_domain"
    assert "1 added, 1 removed, 1 changed (published 2026-10-03)" in p["title"]
    assert sig.object_ref == f"cas:sha256/{_sha(SDN_B)}" and sig.retention_class == "evidence_hold"
    assert sig.modality == "structured"
    assert sig.content_hash == hashlib.sha256(f"{_sha(SDN_A)}->{_sha(SDN_B)}".encode()).hexdigest()
    # BOTH snapshots and the full diff are on the archive
    assert load_object(root, _sha(SDN_A)) == SDN_A and load_object(root, _sha(SDN_B)) == SDN_B
    full_ref = p["diff"]["full_diff_object_ref"]
    full = json.loads(archive_codec.decode(cas_path(root, full_ref.split("/", 1)[1]).read_bytes()))
    assert full["diff"]["counts"] == p["diff"]["counts"]
    receipts = await store.get("instrument_snapshot_receipts")
    assert [r["outcome"] for r in receipts] == ["changed", "unchanged", "first_snapshot"]
    assert receipts[0]["signal_id"] == str(sig.signal_id)


async def test_unchanged_fixture_emits_no_signal_only_the_receipt(tmp_path):
    """The DONE line's second half, standalone: a prior snapshot on record, the
    same instrument served again ⇒ zero signals, one unchanged receipt."""
    store = InMemoryStateStore()
    h = _handler(tmp_path, _Upstream(_ok(SDN_B), _ok(SDN_B)))
    cfg = _cfg(conditional_get=False, emit_first_snapshot=False)
    assert await _pull(h, _ctx(cfg, store)) == []           # baseline, no first signal
    assert await _pull(h, _ctx(cfg, store)) == []
    receipts = await store.get("instrument_snapshot_receipts")
    assert [r["outcome"] for r in receipts] == ["unchanged", "first_snapshot"]
    assert receipts[0]["typed_absence_kind"] == "instrument_unchanged"


async def test_conditional_get_304_is_an_unchanged_receipt(tmp_path):
    store = InMemoryStateStore()
    up = _Upstream(_ok(SDN_A, etag='"v1"'), lambda _r: httpx.Response(304))
    h, cfg = _handler(tmp_path, up), _cfg()
    await _pull(h, _ctx(cfg, store))
    assert await _pull(h, _ctx(cfg, store)) == []
    assert up.requests[1].headers.get("if-none-match") == '"v1"'
    receipt = (await store.get("instrument_snapshot_receipts"))[0]
    assert receipt["outcome"] == "unchanged" and receipt["basis"] == "http_not_modified"
    assert receipt["sha256"] == _sha(SDN_A)


async def test_unparseable_snapshot_is_archived_but_never_advances_or_emits(tmp_path):
    store = InMemoryStateStore()
    bad = b"<sdnList><notAnEntry/></sdnList>"
    h, cfg = _handler(tmp_path, _Upstream(_ok(SDN_A), _ok(bad))), _cfg(conditional_get=False)
    await _pull(h, _ctx(cfg, store))
    assert await _pull(h, _ctx(cfg, store)) == []
    receipt = (await store.get("instrument_snapshot_receipts"))[0]
    assert receipt["outcome"] == "parse_failed" and receipt["sha256"] == _sha(bad)
    assert load_object(tmp_path / "archive", _sha(bad)) == bad
    assert (await store.get("instrument_snapshot_state"))["sha256"] == _sha(SDN_A)
    assert (await store.get("instrument_snapshot_health"))["state"] == "unhealthy"


async def test_over_the_cap_is_refused_and_nothing_archived(tmp_path):
    store = InMemoryStateStore()
    h, cfg = _handler(tmp_path, _Upstream(_ok(SDN_A))), _cfg(max_bytes=100)
    assert await _pull(h, _ctx(cfg, store)) == []
    receipt = (await store.get("instrument_snapshot_receipts"))[0]
    assert receipt["outcome"] == "too_large"
    assert not (tmp_path / "archive" / _sha(SDN_A)[:2]).exists()


async def test_fetch_failure_is_a_receipt_not_a_signal(tmp_path):
    store = InMemoryStateStore()
    h = _handler(tmp_path, _Upstream(lambda _r: httpx.Response(503)))
    assert await _pull(h, _ctx(_cfg(), store)) == []
    assert (await store.get("instrument_snapshot_receipts"))[0]["outcome"] == "fetch_failed"


async def test_an_abandoned_change_signal_does_not_advance_the_pointer(tmp_path):
    """At-least-once: a consumer that never resumes the generator (a failed
    write, a poll cut at its budget) leaves the pointer on the previous
    snapshot, so the next poll re-diffs the same pair."""
    store = InMemoryStateStore()
    h, cfg = _handler(tmp_path, _Upstream(_ok(SDN_A), _ok(SDN_B))), _cfg(conditional_get=False)
    await _pull(h, _ctx(cfg, store))
    gen = h.pull(_ctx(cfg, store))
    sig = await gen.__anext__()
    assert sig.payload["change_kind"] == "changed"
    await gen.aclose()
    assert (await store.get("instrument_snapshot_state"))["sha256"] == _sha(SDN_A)


async def test_activation_refused_without_a_writable_archive(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    h = InstrumentSnapshotSourceHandler(archive_root=blocker / "archive")
    with pytest.raises(InstrumentArchiveUnavailable):
        await h.activation_check(_ctx(_cfg(), InMemoryStateStore()))
    assert await _pull(h, _ctx(_cfg(), store := InMemoryStateStore())) == []
    assert (await store.get("instrument_snapshot_receipts"))[0]["outcome"] == "archive_unavailable"


# ---------------------------------------------------------------------------
# Licence + size cap + kind registration
# ---------------------------------------------------------------------------


def test_a_licence_that_forbids_retention_cannot_bind_the_kind():
    for cls in RETENTION_FORBIDDEN_LICENSE_CLASSES:
        with pytest.raises(ValidationError, match="forbids retaining"):
            _cfg(license_class=cls)
    with pytest.raises(ValidationError):
        InstrumentSnapshotConfig.model_validate({"instrument_name": "x", "url": URL})


def test_forbid_set_is_the_evidence_archivers():
    from legba.data.analysts.deterministic_handlers.evidence_archiver import (
        FORBID_RETENTION_CLASSES,
    )
    assert RETENTION_FORBIDDEN_LICENSE_CLASSES == FORBID_RETENTION_CLASSES


def test_size_cap_is_raised_for_this_kind_only():
    from legba.data.analysts.deterministic_handlers import evidence_archiver
    assert INSTRUMENT_SNAPSHOT_MAX_BYTES >= 29 * 1024 * 1024 > evidence_archiver._DEFAULT_MAX_OBJECT_BYTES
    assert evidence_archiver._DEFAULT_MAX_OBJECT_BYTES == 20 * 1024 * 1024    # the global cap untouched
    with pytest.raises(ValidationError):
        _cfg(max_bytes=INSTRUMENT_SNAPSHOT_MAX_BYTES + 1)


def test_kind_is_discovered_and_satisfies_the_contract():
    assert discover_source_kinds()["instrument_snapshot"] is InstrumentSnapshotSourceHandler
    assert isinstance(InstrumentSnapshotSourceHandler(), SourceHandler)


# ---------------------------------------------------------------------------
# The three DRAFT bindings
# ---------------------------------------------------------------------------

BINDINGS = {
    "source_ofac_sdn_list.yaml": ("source.ofac.sdn_list", "public_domain", "xml_records"),
    "source_uk_sanctions_list.yaml": ("source.fcdo.uk_sanctions_list", "open_gov_attribution", "text"),
    "source_ecfr_bis_entity_list.yaml": ("source.ecfr.bis_entity_list", "public_domain", "text"),
}


@pytest.mark.parametrize("filename", sorted(BINDINGS))
def test_draft_binding_validates_and_builds(filename):
    sid, licence, differ = BINDINGS[filename]
    body = yaml.safe_load((PROFILE_DESCRIPTORS / filename).read_text(encoding="utf-8"))
    desc = SourceDescriptor.model_validate(body, strict=False)
    assert desc.identity.id == sid and desc.identity.kind == "instrument_snapshot"
    assert desc.identity.state.value == "draft"                       # never scheduled by this lane
    assert desc.scope.license_class == licence and desc.scope.source_class == "official"
    handler = build_source_handler("instrument_snapshot", body["config"])
    cfg = InstrumentSnapshotConfig.model_validate(_unwrap_factory_dict(body["config"]))
    assert isinstance(handler, InstrumentSnapshotSourceHandler)
    assert cfg.license_class == desc.scope.license_class               # one licence, two places
    assert cfg.differ == differ


def test_sdn_binding_reads_the_fixture_shape():
    body = yaml.safe_load((PROFILE_DESCRIPTORS / "source_ofac_sdn_list.yaml").read_text(encoding="utf-8"))
    cfg = InstrumentSnapshotConfig.model_validate(_unwrap_factory_dict(body["config"]))
    from legba.data.sources.instrument_snapshot import parse_instrument
    a, b = parse_instrument(SDN_A, cfg), parse_instrument(SDN_B, cfg)
    assert diff_instruments(a, b)["counts"] == {"added": 1, "removed": 1, "changed": 1}
    assert b.published_date == "2026-10-03"


def test_presigned_redirect_query_is_never_persisted():
    from legba.data.sources.instrument_snapshot import redact_url
    signed = "https://bucket.s3.example/Published/x/SDN.XML?X-Amz-Security-Token=SECRET&X-Amz-Signature=abc"
    assert redact_url(signed) == "https://bucket.s3.example/Published/x/SDN.XML"
