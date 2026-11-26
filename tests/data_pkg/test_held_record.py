# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The held family's rules, without a database.

* the ``assertion_type`` family: declared, its desk-row map derived, the
  reserved ``extracted`` basis refused;
* the class rules on one invented shadow record: a fact the verifier
  supports is held, a "partly" one is not held with its verdict, a failed or
  withdrawn row is not written, an assessment is held only when its premises
  are, an absence is held and scoped to its window, a restatement is a link;
* the matching rule: the claim key under the fold, in-record matches;
* the chain: the Python hash, a tampered row, a gap, a cache rebuild;
* the switch is off by default and nothing a reader sees names the tables.

Every fixture is invented (the port of Northaven, the Eastmark authority).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from legba.data import held_record as H
from legba.data.held_record_chain import ZERO_HASH, rebuild_cache, row_hash, verify_chain
from legba.data.type_registry import REQUIRED, TypeDecl, types  # noqa: F401
from legba.data.type_registry.assertion_types import FAMILY

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "legba"
E1 = "0b6c2f0e-1111-4c2a-9d3e-000000000001"
SIG = [f"00000000-0000-4000-8000-0000000000a{i}" for i in range(1, 6)]


def _fact(rid, text, *, kind="event", outcome="established_eligible", verdict="supports", subject=("entity", E1),
          cites=(0,)):
    o = {"id": rid, "row_kind": "fact", "outcome": outcome, "text": text, "cited": [1],
         "draft": {"kind": kind, "subject_kind": subject[0], "subject_key": subject[1],
                   "valid_from": "2026-11-03T00:00:00+00:00", "valid_from_precision": "day", "slots": None},
         "support": [{"ref_id": SIG[i], "quote": f"quote {i} about Northaven", "quote_found_in": "page_text"}
                     for i in cites]}
    if outcome == "established_eligible" and verdict:
        o["verdict"] = verdict
    return o


def shadow_record(shadow_id=101, target="desk_northaven"):
    outcomes = [
        _fact("F1", "Northaven has been closed to tankers since 3 November [1]."),
        _fact("F2", "The Eastmark authority said the closure would last a week.", kind="attributed",
              verdict="supports", cites=(1,)),
        _fact("F3", "Tanker berths at Northaven fell to 4.", kind="level", verdict="partly", cites=(2,)),
        _fact("F4", "A second closure began.", outcome="failed"),
        _fact("F5", "Something withdrawn.", outcome="withdrawn"),
        _fact("F6", "northaven has been closed to tankers since 3 November", cites=(3,)),
        {"id": "A1", "row_kind": "absence", "outcome": "absence", "text": "No row reports a refinery fire.",
         "detail": {"topic": "a refinery fire", "scope": {"collection": target, "source_ids": ["s1"],
                                                          "window_start": "2026-11-01T00:00:00+00:00",
                                                          "window_end": "2026-11-04T00:00:00+00:00"}}},
        {"id": "J1", "row_kind": "assessment", "outcome": "assessment", "text": "Exports will stay low.",
         "detail": {"confidence": "likely", "premises_passed": ["F1"]}},
        {"id": "J2", "row_kind": "assessment", "outcome": "assessment", "text": "Berths will recover.",
         "detail": {"confidence": "unlikely", "premises_passed": ["F1", "F3"]}},
        {"id": "J3", "row_kind": "assessment", "outcome": "failed", "text": "x"},
        {"id": "R1", "row_kind": "restatement", "outcome": "restatement", "text": "Still closed.", "cited": [7],
         "detail": {"status": "confirmed", "backing": ["F1"]}},
    ]
    return {"id": shadow_id, "run_id": "11111111-2222-3333-4444-555555555555", "analyst_id": "energy_security",
            "target_id": target, "occurred_at": "2026-11-05T10:00:00+00:00", "contract_version": "desk_rows.v2.1",
            "gate_version": "statement_gate.v4", "record": {"outcomes": outcomes}}


# ---------------------------------------------------------------------------
# the family
# ---------------------------------------------------------------------------


def test_the_family_declares_one_type_per_desk_row_kind():
    m = H.type_map()
    assert {k: d.attr("assertion_class") for k, d in m.items()} == dict(H.CLASS_OF_ROW_KIND)
    assert {d.attr("assertion_class") for d in types("assertion_type")} == {"statement", "attributed",
                                                                           "assessment", "absence"}
    for d in types("assertion_type"):
        assert d.evidence == "background" and "extracted" not in d.attr("bases")


def test_the_family_refuses_the_reserved_basis_and_a_bad_class():
    bad = TypeDecl("assertion_type", "x", "doc", evidence="background",
                   attrs={"assertion_class": "fact", "key": ("text",), "bases": ("extracted",), "desk_row_kinds": ()})
    problems = FAMILY.check(bad)
    assert any("assertion_class" in p for p in problems) and any("reserved" in p for p in problems)


# ---------------------------------------------------------------------------
# the class rules
# ---------------------------------------------------------------------------


def _by_row(plan):
    return {r.row_id: r for r in plan.records}


def test_the_class_rules():
    plan = H.plan_shadow_record(shadow_record())
    rows = _by_row(plan)
    assert (rows["F1"].class_, rows["F1"].type_id, rows["F1"].status) == ("statement", "event", H.HELD)
    assert rows["F1"].reason_code == "verified_supports" and len(rows["F1"].evidence) == 1
    assert (rows["F2"].class_, rows["F2"].status) == ("attributed", H.HELD)
    assert rows["F2"].speaker_text == "Eastmark" or rows["F2"].speaker_text is None   # code-read, never guessed
    # "partly" is never held: written not held, with the verdict
    assert (rows["F3"].status, rows["F3"].reason_code, rows["F3"].verdict) == (H.NOT_HELD, "verifier_partly", "partly")
    # failed / withdrawn rows are not written
    assert "F4" not in rows and "F5" not in rows and "J3" not in rows
    assert plan.not_written == {"fact:failed": 1, "fact:withdrawn": 1, "assessment:failed": 1}
    # an absence is held, about the collection, its period the window
    a1 = rows["A1"]
    assert (a1.class_, a1.status, a1.subject.kind, a1.subject.key) == ("absence", H.HELD, "target", "desk_northaven")
    assert a1.valid_from == datetime(2026, 11, 1, tzinfo=timezone.utc) and a1.valid_to.day == 4
    # an assessment is held only when every premise is held
    assert (rows["J1"].status, rows["J1"].premises) == (H.HELD, ["F1"])
    assert (rows["J2"].status, rows["J2"].reason_code) == (H.NOT_HELD, "premise_not_held")
    # a restatement is a link, not a record
    assert "R1" not in rows and [(lk.row_id, lk.ref) for lk in plan.links] == [("R1", 7)]


def test_a_missing_verdict_is_not_held():
    rec = shadow_record()
    rec["record"]["outcomes"] = [_fact("F1", "Northaven reopened.", verdict=None)]
    (r,) = H.plan_shadow_record(rec).records
    assert (r.status, r.reason_code) == (H.NOT_HELD, "not_verified")


def test_an_unreadable_subject_or_undeclared_kind_is_refused_and_counted():
    rec = shadow_record()
    rec["record"]["outcomes"] = [_fact("F1", "x", subject=("entity", "not-a-uuid")), _fact("F2", "y", kind="coverage")]
    plan = H.plan_shadow_record(rec)
    assert plan.records == [] and plan.refused == {"fact:subject_unreadable": 1, "fact:type_not_declared": 1}


# ---------------------------------------------------------------------------
# the matching rule
# ---------------------------------------------------------------------------


def test_the_claim_key_folds_case_markers_and_trailing_punctuation():
    k = H.claim_key("event", E1, {"text": "Northaven has been closed to tankers since 3 November [1]."}, ("text",))
    assert k == H.claim_key("event", E1, {"text": "northaven  has been closed to tankers since 3 November"}, ("text",))
    assert k != H.claim_key("state", E1, {"text": "Northaven has been closed to tankers since 3 November"}, ("text",))
    assert k != H.claim_key("event", "desk_x", {"text": "Northaven has been closed to tankers since 3 November"},
                            ("text",))
    assert k != H.claim_key("event", E1, {"text": "Northaven has been closed to tankers since 4 November"}, ("text",))


def test_a_second_row_with_the_same_key_in_one_record_is_matched_not_duplicated():
    rows = _by_row(H.plan_shadow_record(shadow_record()))
    assert rows["F6"].claim_key == rows["F1"].claim_key and rows["F6"].same_as_row == "F1"
    assert rows["F1"].same_as_row is None


def test_an_absence_key_is_topic_and_collection_not_window():
    a = shadow_record(1)
    b = shadow_record(2)
    b["record"]["outcomes"][6]["detail"]["scope"]["window_start"] = "2026-11-04T00:00:00+00:00"
    ka = _by_row(H.plan_shadow_record(a))["A1"].claim_key
    kb = _by_row(H.plan_shadow_record(b))["A1"].claim_key
    assert ka == kb
    assert ka != _by_row(H.plan_shadow_record(shadow_record(3, target="desk_other")))["A1"].claim_key


# ---------------------------------------------------------------------------
# the chain (the Python mirror)
# ---------------------------------------------------------------------------


def _chain(n=3, aid="7e0e5b8e-0000-4000-8000-000000000001"):
    rows, prev = [], ZERO_HASH
    for seq in range(1, n + 1):
        r = {"assertion_id": aid, "seq": seq, "status": "held" if seq % 2 else "not_held", "version_id": 10 + seq,
             "reason_code": "verified_supports", "reason": "why", "verdict": None,
             "caused_by": "5b0c1a2e-0000-4000-8000-0000000000c1", "source_ref": f"shadow:1:F{seq}",
             "recorded_at": datetime(2026, 11, 5, 10, seq, tzinfo=timezone.utc), "prev_hash": prev}
        r["row_hash"] = row_hash(r)
        prev = r["row_hash"]
        rows.append(r)
    return rows


def test_an_intact_chain_verifies():
    assert verify_chain(_chain()) == (3, [])


@pytest.mark.parametrize("tamper,reason", [
    (lambda rows: rows[1].update(reason="edited"), "row_hash_mismatch"),
    (lambda rows: rows.pop(1), "seq_gap"),
    (lambda rows: rows[2].update(prev_hash="f" * 64), "prev_hash_mismatch"),
])
def test_a_tampered_chain_is_named_at_its_row(tamper, reason):
    rows = _chain()
    tamper(rows)
    _, breaks = verify_chain(rows)
    assert [b.reason for b in breaks] == [reason]


def test_the_cache_rebuilds_from_the_log():
    rows = _chain()
    cache = rebuild_cache(rows)[rows[0]["assertion_id"]]
    assert (cache["status"], cache["version_id"], cache["last_seq"], cache["entries"]) == ("held", 13, 3, 3)
    assert cache["last_row_hash"] == rows[-1]["row_hash"]


# ---------------------------------------------------------------------------
# the switch, and nothing a reader sees
# ---------------------------------------------------------------------------


def test_the_feed_is_off_by_default(monkeypatch):
    import asyncio

    from legba.data.held_record_feed import FEED_ENV, feed_analysts, feed_pending
    from legba.runtime.held_record_loop import start_held_record_if_enabled

    monkeypatch.delenv(FEED_ENV, raising=False)
    assert feed_analysts() == frozenset()
    assert asyncio.run(start_held_record_if_enabled(object())) is None
    rep = asyncio.run(feed_pending(object()))           # never touches the pool
    assert (rep.read, rep.written) == (0, 0)
    monkeypatch.setenv(FEED_ENV, " energy_security , ")
    assert feed_analysts() == frozenset({"energy_security"})


_TABLES = ("assertions", "assertion_versions", "assertion_status_log", "assertion_status", "assertion_support",
           "assertion_shadow_receipts")
_ALLOWED = {
    "src/legba/data/held_record_store.py", "src/legba/data/held_record_reads.py", "src/legba/data/held_record_feed.py",
    "src/legba/data/held_record_chain.py", "src/legba/data/held_record.py", "src/legba/data/write_attribution.py",
    "src/legba/data/migrations/20261115T1940_held_record.sql", "scripts/held_record_replay.py",
    "scripts/held_record_verify.py", "scripts/report_written_by.py",
    "src/legba/runtime/held_record_loop.py",  # logs the writer's "assertions" count key; no SQL
}


def test_only_the_held_record_modules_name_its_tables():
    rx = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:public\.)?(" + "|".join(_TABLES) + r")\b"
                    r"|[\"'](" + "|".join(_TABLES) + r")[\"']")
    hits = set()
    # scripts/seed_data.py is gitignored (curated seed data, never shipped): it exists only in a
    # checkout that kept it, and its "assertions" is a JSON key of an old seed shape, not this family.
    untracked = {REPO / "scripts" / "seed_data.py"}
    for root in (SRC, REPO / "scripts", REPO / "legba-ui-v3" / "src"):
        for p in root.rglob("*"):
            if p in untracked:
                continue
            if p.suffix in {".py", ".ts", ".tsx", ".sql"} and p.is_file():
                if rx.search(p.read_text(encoding="utf-8", errors="replace")):
                    hits.add(str(p.relative_to(REPO)))
    assert hits <= _ALLOWED, sorted(hits - _ALLOWED)
    assert "src/legba/data/held_record_store.py" in hits


def test_the_reads_leaf_is_stdlib_plus_its_two_leaves():
    text = (SRC / "data" / "held_record_reads.py").read_text()
    imports = set(re.findall(r"^from (\S+) import|^import (\S+)", text, re.M))
    mods = {a or b for a, b in imports}
    assert mods <= {"__future__", "json", "collections.abc", "datetime", "typing", ".display_policy",
                    ".held_record_chain"}, mods
    chain = (SRC / "data" / "held_record_chain.py").read_text()
    assert not re.search(r"^from \.", chain, re.M)


def test_the_writer_never_writes_outside_the_family():
    text = (SRC / "data" / "held_record_store.py").read_text() + (SRC / "data" / "held_record_feed.py").read_text()
    writes = set(re.findall(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", text))
    assert writes <= set(_TABLES), writes
