# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The evidence gate reads every inline citation form.

The N-G gate counted only ``cited_substrate_refs`` (filled from the
``[[ref:<uuid>]]`` form alone), so lens 5b0b4ecc — ten resolving ``[[uuid]]``
citations, zero structured refs — was classed "cites nothing". The gate now
judges structured refs UNION every inline form, each still required to
resolve; and lens / inquiry / crossroads rows (citation-free by design on an
empty pull, an empty ledger, a block of ordinals) are withheld at zero refs
only when the body is a stub or a pseudo tool call.
"""
from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from legba.data import journal_evidence as je
from legba.data.provenance.kinds import OutputKind
from legba.data.provenance.models import JournalPayload

REPO = Path(__file__).resolve().parents[2]
REAL = [str(uuid4()) for _ in range(12)]
FAKE = [f"{i:08x}-9c30-3{i:03x}-8611-a5e7b8199260" for i in range(1, 10)]
HONEST_EMPTY_LENS = (
    "Collection posture: 117 active of 139 wired, 105 fresh / 12 stalled. The reads "
    "I pulled have returned nothing: my field notes carry no tower rows, and I will "
    "not invent what an empty pull did not produce. " * 8
)


class _Conn:
    def __init__(self, known=()) -> None:
        self.known = {str(k).lower() for k in known}

    async def fetch(self, sql, *args):
        if "FROM signals" in sql and "WHERE id = ANY" in sql:
            return [{"id": UUID(str(k))} for k in args[0] if str(k).lower() in self.known]
        return []


def _gate(body, *, kind="entry", structured=(), known=REAL):
    refs = je.collect_refs(structured, body)
    resolved = asyncio.run(je.resolve_ref_existence(_Conn(known), refs))
    return je.evaluate_publish_gate(body=body, refs=refs, resolved=resolved, entry_kind=kind)


@pytest.mark.parametrize("template", [
    "[[ref:{u}]]", "[[{u}]]", "[{u}]", "[ref:{u}]", "【{u}】", "【ref:{u}】",
    "［{u}］", "[[find:{u}]]", "[[sh:{u}]]", "[[ref={u}]]", "[[ {U} ]]",
])
def test_every_live_inline_form_is_extracted(template) -> None:
    u = REAL[0]
    text = "A reading " + template.format(u=u, U=u.upper()) + " and more."
    assert je.extract_inline_refs(text) == [u]


def test_bare_uuids_in_prose_and_numbered_markers_are_not_refs() -> None:
    assert je.extract_inline_refs(f"run_id {REAL[0]} and [[ref:3]] and 【4】") == []


def test_structured_and_inline_union_is_deduplicated() -> None:
    body = f"x [[{REAL[0]}]] y [[ref:{REAL[1].upper()}]] z 【{REAL[0]}】"
    assert je.collect_refs([REAL[1], REAL[2]], body) == [REAL[1], REAL[2], REAL[0]]


def test_5b0b4ecc_shape_inline_double_bracket_lens_passes() -> None:
    body = " ".join(f"Base rate reading {i} [[{u}]]." for i, u in enumerate(REAL[:10])) * 3
    v = _gate(body, kind="lens")
    assert not v.withhold and (v.refs_resolved, v.refs_total) == (10, 10)
    # the same shape on an entry passes too — the form, not the tier, was the bug
    assert not _gate(body, kind="entry").withhold


@pytest.mark.parametrize("kind", ["entry", "lens", "inquiry", "crossroads"])
def test_invented_inline_ids_still_fail(kind) -> None:
    body = " ".join(f"Reading {i} 【{u}】." for i, u in enumerate(FAKE)) * 20
    v = _gate(body, kind=kind)
    assert v.withhold and v.reason == "refs_unresolved" and v.refs_resolved == 0


def test_inline_fabrications_dilute_structured_refs() -> None:
    body = "Prose. " * 100 + " ".join(f"[{u}]" for u in FAKE)
    v = _gate(body, kind="entry", structured=REAL[:3])
    assert v.withhold and (v.refs_resolved, v.refs_total) == (3, 12)


@pytest.mark.parametrize("kind", sorted(je.CITATION_OPTIONAL_KINDS))
def test_honest_uncited_note_passes_for_citation_optional_kinds(kind) -> None:
    v = _gate(HONEST_EMPTY_LENS, kind=kind)
    assert not v.withhold and v.reason is None


@pytest.mark.parametrize("kind", ["entry", "consolidation", "chronicle", "lens_diff", None])
def test_citation_required_kinds_keep_the_zero_refs_bar(kind) -> None:
    v = _gate(HONEST_EMPTY_LENS, kind=kind)
    assert v.withhold and v.reason == "no_refs"


@pytest.mark.parametrize("body,reason", [
    ('{"query_facts": {"temporal_window": {"as_of": "2026-01-25T00:00:00Z"}, "limit": 40}}',
     "tool_call_body"),                                                   # 92e73ce9
    ('I need the health summary before I can write. {"tool": "get_journal_delta", "args": {}}',
     "tool_call_body"),                                                   # 9bc6fdc4
    ("Collection posture: 139 wired, 116 active. Pulling the tower top now.", "stub_body"),
    ("", "stub_body"),
])
@pytest.mark.parametrize("kind", sorted(je.CITATION_OPTIONAL_KINDS))
def test_stub_and_tool_call_bodies_are_withheld(body, reason, kind) -> None:
    v = _gate(body, kind=kind)
    assert v.withhold and v.reason == reason
    assert je.verdict_detail(v).startswith("the draft was")


def test_control_text_still_wins_on_citation_optional_kinds() -> None:
    v = _gate(HONEST_EMPTY_LENS + " <system-warning>x</system-warning>", kind="lens")
    assert v.withhold and v.reason == "control_text"


def test_notes_gate_accepts_inline_forms_other_than_ref_marker() -> None:
    notes = " ".join(f"Observation {i} reads plainly [[{u}]]." for i, u in enumerate(REAL)) * 4
    assert len(notes) >= je.NOTES_MIN_CHARS and je.REF_MARKER not in notes
    assert je.notes_degenerate_reason(notes) is None
    assert je.notes_degenerate_reason("Uncited prose. " * 100) == "no_ref_markers"


def test_publish_gate_reads_inline_refs_on_a_lens_payload(monkeypatch) -> None:
    from legba.runtime import actor_critic as AC

    async def fake_verify(conn, **kw):
        return {"judge_status": "llm", "unsupported_spans": []}

    monkeypatch.setattr(AC, "verify_inline_target_finding", fake_verify)
    now = datetime.now(timezone.utc)
    body = " ".join(f"Base rate reading {i} [[{u}]]." for i, u in enumerate(REAL[:10])) * 3
    payload = JournalPayload(
        entry_kind="lens", title="t", body=body, claims=[], cited_substrate_refs=[],
        period_start=now, period_end=now, honesty_flags=[],
    )
    deps = SimpleNamespace(descriptor=SimpleNamespace(
        identity=SimpleNamespace(kind="journal_assessor", id="lens_baserate")))
    _, _, out = asyncio.run(AC.verify_journal_before_publish(
        _Conn(REAL), deps=deps, output_kind=OutputKind.JOURNAL, payload=payload,
        run_id="r1", options={}, declares_verify=False, trace_steps=None,
    ))
    assert out is payload and je.EVIDENCE_UNAVAILABLE_FLAG not in out.honesty_flags


def test_correction_script_plans_with_the_union_and_the_kind_rule() -> None:
    spec = importlib.util.spec_from_file_location(
        "jco", REPO / "scripts" / "journal_correct_once.py")
    jco = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(jco)
    inline_lens = " ".join(f"r{i} [[{u}]]." for i, u in enumerate(REAL[:5])) * 30
    rows = [
        {"id": "a", "entry_kind": "lens", "body": inline_lens, "refs": []},
        {"id": "b", "entry_kind": "lens", "body": HONEST_EMPTY_LENS, "refs": []},
        {"id": "c", "entry_kind": "lens", "body": '{"tool": "search", "args": {}}', "refs": []},
        {"id": "d", "entry_kind": "entry", "body": HONEST_EMPTY_LENS, "refs": []},
        {"id": "e", "entry_kind": "inquiry", "body": "x " * 400 + f"[[{FAKE[0]}]]", "refs": []},
    ]
    plan = jco.plan_corrections(rows, set(REAL))
    assert {p["id"]: p["reason"] for p in plan} == {
        "c": "tool_call_body", "d": "no_refs", "e": "refs_unresolved",
    }
