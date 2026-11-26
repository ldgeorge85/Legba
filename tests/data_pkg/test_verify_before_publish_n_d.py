# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verification stands IN FRONT of the text.

  1. Journal: the verify runs BEFORE the row is written; a flagged sentence is
     marked in place and the flags land on the published row (journal
     d4ff1266 replay shape).
  2. Desk tier: ``blocks[].span_checks`` carries the number and actor checks
     of the quoted lead span (RU 470c27a4, US 4e230ea2 shapes).
  4. Journal counting: duplicate citations and overstated outcomes are marked.
  5. Quote fidelity: the arms count the spans whose sha was actually compared.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from legba.data.analysts import assembly_payload as ap
from legba.data.analysts.assembly_span_checks import span_checks
from legba.data.analysts.assembly_spans import extract_lead_span
from legba.data.analysts.journal_assessor import build_journal_verify_inputs
from legba.data.analysts.journal_prepublish import (
    apply_prepublish_verdict,
    strip_verify_marks,
)
from legba.data.analysts.journal_reflect import _reflect_claims
from legba.data.provenance import assembly_arms as AA
from legba.data.provenance.kinds import OutputKind
from legba.data.provenance.models import JournalPayload

R_TOI = "0c4b6ba8-9e3c-4194-9540-12cdb971f215"
R_ODESA = "5d72f03f-bc55-4e13-80c4-15b24ae1fcae"
R_C1 = "0faa410e-2496-4515-9395-aa2f039bf5c5"
R_C2 = "229572ba-44a6-4a8a-ba0a-fb40767cb8ee"
R_AB1 = "5ed90ffe-4b85-4677-95b5-5ce1759c3ed4"
R_AB2 = "fc967ac3-8b72-42ef-9855-8afd3dab056b"
R_D = "db887bad-080f-4e6d-b680-b28c9ac86973"

BODY = (
    "The war has become an exchange of ledgers. Russia spent the night of 10 "
    "November putting 162 drones plus a ballistic across Ukraine into day two of "
    f"the Abu Dhabi talks [[ref:{R_TOI}]] — six substations in four regions, "
    f"energy infrastructure in Odesa [[ref:{R_ODESA}]].\n\n"
    f"Hormuz is where the pricing gets worse. A third oil tanker exploded on a "
    f"naval mine [[ref:{R_C1}]], IRGC Navy confirmed the third blast "
    f"[[ref:{R_C2}]], two tankers burned [[ref:{R_AB1}]], two more followed "
    f"[[ref:{R_AB2}]], and the British maritime agency logged a projectile "
    f"strike off Oman [[ref:{R_D}]] — four tanker kills in one window.\n\n"
    "I wonder what the silence means."
)

TEXTS = {
    R_TOI: {"title": "6 power substations hit in Russian strikes across Ukraine's 4 regions",
            "text": "6 power substations hit in Russian strikes across Ukraine's 4 regions. "
                    "Six major electricity substations across four central Ukrainian regions.",
            "publisher": "source.timesofindia.world"},
    R_ODESA: {"title": "Odesa energy hit", "text": "Strikes damaged energy infrastructure in Odesa.",
              "publisher": "source.ukrinform"},
    R_C1: {"title": "Iran says 3rd oil tanker explodes after hitting naval mine in Strait of Hormuz",
           "text": "Iran says 3rd oil tanker explodes after hitting naval mine in Strait of Hormuz. "
                   "The IRGC Navy claimed that the destruction of US coastal radar had "
                   "increased the risks facing tankers.",
           "publisher": "source.anadolu.english"},
    R_C2: {"title": "IRGC Navy reports blast in third tanker in Hormuz Strait",
           "text": "IRGC Navy reports blast in third tanker in Hormuz Strait.",
           "publisher": "source.irna.english"},
    R_AB1: {"title": "Two oil tankers catch fire after explosions in Strait of Hormuz",
            "text": "Two oil tankers catch fire after explosions in Strait of Hormuz.",
            "publisher": "source.tehrantimes.all"},
    R_AB2: {"title": "Two more offending tankers explode in Strait of Hormuz",
            "text": "Two more offending tankers explode in Strait of Hormuz, left burning.",
            "publisher": "source.irna.english"},
    R_D: {"title": "British maritime agency reports projectile strike on tanker off Oman's coast",
          "text": "UKMTO reports a projectile strike on a tanker 19 nm off Oman.",
          "publisher": "source.tass.english"},
}

#: The live critique d1762373's flagged span, verbatim in its verify-doc form.
JUDGE_SPAN = {
    "text": (
        "Russia spent the night of 10 November putting 162 drones plus a ballistic "
        "across Ukraine into day two of the Abu Dhabi talks [1] — six substations in "
        "four regions, energy infrastructure in Odesa [2]."
    ),
    "reason": "judge_unsupported",
}


def _payload(body: str = BODY) -> JournalPayload:
    claims, refs, _ = _reflect_claims(body)
    now = datetime.now(timezone.utc)
    return JournalPayload(
        entry_kind="entry", title="Journal", body=body, claims=claims,
        cited_substrate_refs=refs, period_start=now, period_end=now,
        honesty_flags=["forecast_unproven"],
    )


def _apply(spans=(JUDGE_SPAN,), texts=TEXTS):
    return apply_prepublish_verdict(
        _payload(), unsupported_spans=list(spans), ref_texts=texts,
        judge_status="llm", reflect=_reflect_claims,
    )


# ---------------------------------------------------------------------------
# 1 — the judge's flag reaches the published row
# ---------------------------------------------------------------------------


def test_the_judge_flagged_lead_is_marked_in_place_not_removed() -> None:
    out, summary = _apply()
    lead = out.body.split("\n\n")[0]
    assert "162 drones" in lead  # marked, never stripped
    assert lead.rstrip().endswith("Odesa [[ref:%s]] [[unsupported]]." % R_ODESA)
    assert summary["unsupported"] == [
        {"reason": "judge_unsupported", "text": JUDGE_SPAN["text"], "located": True}
    ]
    assert "unsupported_claims" in out.honesty_flags
    assert out.data["prepublish_verify"]["version"] == "journal_prepublish.v1"
    # the claims sidecar carries the same mark (re-reflected over the marked body)
    assert any("[[unsupported]]" in c.text_span for c in out.claims)
    assert out.cited_substrate_refs == _payload().cited_substrate_refs


def test_a_contradicted_span_raises_the_contradicted_flag_on_the_row_itself() -> None:
    out, _ = _apply(spans=[{**JUDGE_SPAN, "reason": "judge_contradicted"}])
    assert "contradicted_claims" in out.honesty_flags


def test_reasons_that_are_not_about_support_do_not_mark() -> None:
    out, summary = _apply(spans=[{**JUDGE_SPAN, "reason": "double_counted"}])
    assert "[[unsupported]]" not in out.body
    assert summary["unsupported"] == []


def test_a_reverify_grades_the_writers_words_not_the_marks() -> None:
    out, _ = _apply()
    doc, _ = build_journal_verify_inputs(out)
    assert "[[" not in doc.replace("[[ref:", "")
    assert strip_verify_marks(out.body) == BODY


# ---------------------------------------------------------------------------
# 2 — numbers, journal tier
# ---------------------------------------------------------------------------


def test_162_before_a_ref_that_carries_no_162_is_number_unsupported() -> None:
    out, summary = _apply(spans=[])
    assert [n["phrase"] for n in summary["number_unsupported"]] == ["162 drone"]
    assert "162 drones [[number_unsupported]] plus" in out.body
    assert "number_unsupported" in out.honesty_flags


def test_162_passes_when_the_adjacent_source_states_it() -> None:
    texts = {**TEXTS, R_TOI: {**TEXTS[R_TOI], "text": TEXTS[R_TOI]["text"]
                              + " Russia launched 162 drones overnight."}}
    _, summary = _apply(spans=[], texts=texts)
    assert summary["number_unsupported"] == []


# ---------------------------------------------------------------------------
# 4 — counting and outcome verbs, journal tier
# ---------------------------------------------------------------------------


def test_restated_incidents_are_marked_and_the_cited_list_deduplicated() -> None:
    out, summary = _apply(spans=[])
    assert sorted(sorted(g) for g in summary["event_groups"]) == sorted(
        [sorted([R_C1, R_C2]), sorted([R_AB1, R_AB2])]
    )
    assert f"[[ref:{R_C2}]] [[duplicate_citation]]" in out.body
    assert f"[[ref:{R_AB2}]] [[duplicate_citation]]" in out.body
    assert "duplicate_event_citations" in out.honesty_flags
    # "four" is the distinct-event count (A+B is one report of two tankers).
    assert all(n["number"] != "four" for n in summary["number_unsupported"])


def test_kills_over_exploded_and_burning_is_marked_overstated() -> None:
    out, summary = _apply(spans=[])
    assert [o["term"] for o in summary["outcome_overstated"]] == ["kills"]
    assert "four tanker kills [[outcome_overstated]] in one window" in out.body
    assert "outcome_overstated" in out.honesty_flags


def test_a_clean_entry_publishes_byte_identical_with_an_empty_verdict() -> None:
    body = f"Strikes damaged energy infrastructure in Odesa [[ref:{R_ODESA}]]."
    p = _payload(body)
    out, summary = apply_prepublish_verdict(
        p, unsupported_spans=[], ref_texts=TEXTS, judge_status="llm",
        reflect=_reflect_claims,
    )
    assert out.body == body
    assert summary["marked"] == 0
    assert out.honesty_flags == ["forecast_unproven"]


# ---------------------------------------------------------------------------
# 1 — the runtime orders verify BEFORE the write, once
# ---------------------------------------------------------------------------


class _Conn:
    async def fetch(self, sql, keys):
        rows = []
        if "FROM signals" in sql:
            for k in keys:
                t = TEXTS.get(str(k))
                if t:
                    rows.append({"id": k, "source_id": t["publisher"],
                                 "payload": {"title": t["title"], "summary": t["text"]}})
        return rows


def _deps(kind: str = "journal_assessor"):
    return SimpleNamespace(descriptor=SimpleNamespace(identity=SimpleNamespace(kind=kind)))


def test_runtime_verifies_once_before_publish_and_keys_the_critique_on_the_new_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from legba.runtime import actor_critic as AC

    calls: list[UUID] = []

    async def fake_verify(conn, *, deps, finding_id, finding_payload, run_id, **kw):
        calls.append(finding_id)
        assert "[[unsupported]]" not in finding_payload.body  # judged pre-mark
        return {"judge_status": "llm", "unsupported_spans": [JUDGE_SPAN]}

    monkeypatch.setattr(AC, "verify_inline_target_finding", fake_verify)
    entry_id, block, out = asyncio.run(AC.verify_journal_before_publish(
        _Conn(), deps=_deps(), output_kind=OutputKind.JOURNAL, payload=_payload(),
        run_id="r1", options={}, declares_verify=True,
    ))
    assert calls == [entry_id]  # ONE judge pass, keyed on the id the row will carry
    assert block["judge_status"] == "llm"
    assert "[[unsupported]]" in out.body
    assert "number_unsupported" in out.honesty_flags


@pytest.mark.parametrize("kind,output_kind,declares", [
    ("inline_target", OutputKind.FINDING, True),
    ("journal_assessor", OutputKind.JOURNAL, False),
    ("chronicle_assessor_kind", OutputKind.JOURNAL, True),
])
def test_runtime_is_a_noop_for_everything_else(monkeypatch, kind, output_kind, declares) -> None:
    from legba.runtime import actor_critic as AC

    async def boom(*a, **k):  # pragma: no cover — must not be called
        raise AssertionError("verify must not run")

    monkeypatch.setattr(AC, "verify_inline_target_finding", boom)
    p = _payload()
    entry_id, block, out = asyncio.run(AC.verify_journal_before_publish(
        _Conn(), deps=_deps(kind), output_kind=output_kind, payload=p,
        run_id="r1", options={}, declares_verify=declares,
    ))
    assert (entry_id, block, out) == (None, None, p)


def test_the_moved_judge_call_is_still_metered_once(monkeypatch) -> None:
    """The post-receipt meter only sees calls after the receipt flush; the
    journal judge now runs before it, so the pre-publish seam meters it."""
    from legba.data import run_accounting as RA
    from legba.data.provenance import budget as B
    from legba.runtime import actor_critic as AC

    calls_made: list[dict] = []

    async def fake_verify(*a, **k):
        calls_made.append({"component_id": "llm.verify.x", "total_tokens": 10})
        return {"judge_status": "llm", "unsupported_spans": []}

    metered: list[list[dict]] = []

    async def fake_record(conn, *, analyst_id, calls, bucket=None):
        metered.append(list(calls))
        return []

    monkeypatch.setattr(AC, "verify_inline_target_finding", fake_verify)
    monkeypatch.setattr(RA, "llm_call_watermark", lambda: len(calls_made))
    monkeypatch.setattr(RA, "llm_calls_since", lambda w: [dict(c) for c in calls_made[w:]])
    monkeypatch.setattr(B, "record_judge_calls_budget", fake_record)
    deps = SimpleNamespace(descriptor=SimpleNamespace(
        identity=SimpleNamespace(kind="journal_assessor", id="journal_assessor")))
    asyncio.run(AC.verify_journal_before_publish(
        _Conn(), deps=deps, output_kind=OutputKind.JOURNAL, payload=_payload(),
        run_id="r1", options={}, declares_verify=True,
    ))
    assert len(metered) == 1 and metered[0][0]["leg"] == "verify_judge"


def test_judge_unavailable_still_runs_the_deterministic_checks(monkeypatch) -> None:
    from legba.runtime import actor_critic as AC

    async def no_judge(*a, **k):
        return None  # the journal path lands NO critique when the judge is down

    monkeypatch.setattr(AC, "verify_inline_target_finding", no_judge)
    entry_id, block, out = asyncio.run(AC.verify_journal_before_publish(
        _Conn(), deps=_deps(), output_kind=OutputKind.JOURNAL, payload=_payload(),
        run_id="r1", options={}, declares_verify=True,
    ))
    assert entry_id is not None and block is None
    assert "[[unsupported]]" not in out.body
    assert "[[number_unsupported]]" in out.body
    assert out.data["prepublish_verify"]["judge_status"] is None


# ---------------------------------------------------------------------------
# 2 / 3 — the desk tier, on the quoted lead span
# ---------------------------------------------------------------------------

RU_BODY = (
    "*As of 11 November 2026; slice covers the trailing 72h; 119 signals.*\n\n"
    "**BLUF:** Russia's strike posture holds into day two of the Abu Dhabi talks — "
    "162 drones and a ballistic missile overnight into 10 November across eight "
    "Ukrainian regions [11][51][120].\n\n## What changed\n- x [5].\n"
)
CBC = (
    "Russia launched 162 drones of various types and a ballistic missile at Ukraine "
    "overnight. Russia attacked 10 Ukrainian regions overnight. Its forces shot "
    "down 155 Ukrainian drones overnight above eight Russian regions."
)


def _desk_row(body, target_id, cits):
    return {"id": "h1", "target_id": target_id, "body": body,
            "data": {"data": {"citations": cits}}}


def test_ru_head_number_flag_rides_the_block() -> None:
    row = _desk_row(RU_BODY, "country_g20_ru", [
        {"marker": "[11]", "signal_id": "s11", "title": "Zelensky on talks",
         "source_text": "Zelensky said Ukraine wants peace."},
        {"marker": "[51]", "signal_id": "s51", "title": "Russia, Ukraine talks",
         "source_text": CBC},
        {"marker": "[120]", "ref_id": "p", "ref_kind": "prior_read",
         "evidence_text": "eight Ukrainian regions (prior read)"},
    ])
    span = extract_lead_span(row, head_id="h1")
    checks = span_checks(span, row)
    assert checks["flags"] == ["number_unsupported"]
    assert [n["phrase"] for n in checks["number_unsupported"]] == ["eight ukrainian region"]
    # a prior_read marker is NOT a signal: it neither supports nor refutes.
    assert checks["number_unsupported"][0]["markers"] == ["[11]", "[51]", "[120]"]


US_BODY = (
    "**BLUF:** The US-Iran confrontation is drifting toward negotiation, held back "
    "by Israeli pressure on Trump, even as diplomatic channels advance [18][40].\n"
)


def test_us_head_actor_flag_on_the_russia_ukraine_citation() -> None:
    row = _desk_row(US_BODY, "country_g20_us", [
        {"marker": "[18]", "signal_id": "s18", "source_text": (
            "Ukrainian President Volodymyr Zelensky said he was happy with messages "
            "from the US on a resolution of the war with Russia, as talks between "
            "Ukraine, Russia and the US wrapped up; Ukraine and Russia exchanged "
            "lists.")},
        {"marker": "[40]", "signal_id": "s40", "source_text": (
            "Rubio saw no need to resume combat operations against Iran and hailed "
            "US-mediated talks between Russia and Ukraine.")},
    ])
    checks = span_checks(extract_lead_span(row, head_id="h1"), row)
    assert checks["flags"] == ["actor_mismatch"]
    assert [a["marker"] for a in checks["actor_mismatch"]] == ["[18]"]


def test_a_head_without_a_citation_bridge_is_not_checked() -> None:
    row = {"id": "h1", "target_id": "country_g20_us", "body": US_BODY, "data": {}}
    assert span_checks(extract_lead_span(row, head_id="h1"), row) is None


def test_build_assembly_stamps_span_checks_on_every_desk_block() -> None:
    row = _desk_row(RU_BODY, "country_g20_ru", [
        {"marker": "[51]", "signal_id": "s51", "source_id": "source.cbc",
         "title": "t", "source": "https://x", "source_text": CBC},
    ])
    row.update({"analyst_id": "military_posture", "title": "h", "severity": "high",
                "produced_at": "2026-11-11T07:21:00+00:00"})
    ap.attach_cited_salience([row], {"s51": 0.9})
    payload = ap.build_assembly(
        tier=ap.TIER_COUNTRY, as_of="2026-11-11T12:00:00+00:00",
        candidates=[row], carried=[row],
    )
    block = payload["blocks"][0]
    assert block["span_checks"]["flags"] == ["number_unsupported"]


# ---------------------------------------------------------------------------
# 5 — quote fidelity: k of n spans FULLY checked
# ---------------------------------------------------------------------------


def _arms_case(captured: str, full: str):
    raw = full.encode("utf-8")
    needle = "The corridor reopened under monitoring."
    start = full.index(needle)
    a = {"schema": AA.ASSEMBLY_SCHEMA, "regime": "assembly", "blocks": [{
        "ordinal": 1, "finding_id": "h1", "desk": "d", "target_id": "t",
        "spans": [{"text": needle, "origin": {
            "head_id": "h1", "start": start, "end": start + len(needle),
            "body_sha256": hashlib.sha256(raw).hexdigest(), "body_len": len(raw),
        }}],
    }]}
    origins = {"h1": {"evidence_text": captured, "has_evidence_text": True,
                      "source": "d", "target_id": "t", "produced_at": "", "ordinal": 1}}
    return AA.quote_fidelity(a, origins)


def test_a_complete_capture_counts_the_span_as_fully_checked() -> None:
    body = "**BLUF:** The corridor reopened under monitoring. More text."
    res = _arms_case(body, body)
    assert AA.quote_fidelity_coverage(res) == (1, 1)
    assert "assembly_quote_sha_verified" in AA.COUNTERS


def test_a_truncated_capture_is_containment_only_never_fully_checked() -> None:
    full = "**BLUF:** The corridor reopened under monitoring. " + "x" * 5000
    res = _arms_case(full[:4000], full)
    assert res.findings == []
    assert AA.quote_fidelity_score(res) == 1.0  # containment held …
    assert AA.quote_fidelity_coverage(res) == (0, 1)  # … the sha was never compared
    assert res.counters["assembly_quote_sha_unverifiable"] == 1
