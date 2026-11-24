# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract v2.1 (lane D-2): actors NAMED in a cited row are offered and
accepted; the SURFACE subject; GENERIC ROLES refused; the quote-words REPAIR
RULE with planted smuggles and genuine alignments; SPLIT in repair; the
naming hints; the SOURCE ROSTER read classified into the computed block; the
code-built lead. No I/O; no table."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from legba.data import desk_rows_computed as comp
from legba.data.desk_rows_contract import parse_repairs, parse_rows_reply
from legba.data.desk_rows_gates import (
    DESK_GATES_VERSION,
    ESTABLISHED_ELIGIBLE,
    FAILED,
    GateContext,
    apply_repairs,
    build_repair_prompt,
    gate_reply,
    is_surface,
    tally,
    text_refusal,
)
from legba.data.desk_rows_render import code_lead, render_read
from legba.data.desk_rows_roster import ROSTER_SQL, roster_pool, roster_state, selector_admits
from legba.data.desk_rows_surface import is_generic_role, is_proper_name_shaped, surface_refusals
from legba.data.statement_actor_subjects import LinkedEntity
from legba.data.statement_gate import check_statement_shape
from legba.data.statement_typing import SubjectCandidate

SIG1 = "00000000-0000-4000-8000-0000000000a1"
SIG2 = "00000000-0000-4000-8000-0000000000a2"
SIG3 = "00000000-0000-4000-8000-0000000000a3"
T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)

TEXT1 = ("Ukrenergo said hourly outage schedules would apply to all consumers on Sunday. "
         "Naftogaz reported damage to a gas production facility in the Poltava region after a strike.")
TEXT2 = ("Russian forces struck Ukraine's Burshtyn power plant overnight, the regional governor said. "
         "Officials said repairs would take 12 days.")
TEXT3 = "The Kharkiv Thermal Company did not restore heating to 300 buildings, the city council said."


def _row(sid: str, source: str, title: str, text: str) -> dict[str, Any]:
    return {"id": sid, "source_id": source, "title": title, "fetched_at": T0,
            "data": {"title": title, "archived_text": text, "published_at": "2026-11-03T10:00:00+00:00"}}


SLICE = [_row(SIG1, "source.wire.a", "Outage schedules return", TEXT1),
         _row(SIG2, "source.wire.b", "Plant struck overnight", TEXT2),
         _row(SIG3, "source.wire.c", "Heating not restored", TEXT3)]
SCOPE = comp.slice_scope(SLICE, [SIG1, SIG2, SIG3], collection="desk_ua",
                         coverage={"window_start": "2026-11-02T00:00:00+00:00",
                                   "window_end": "2026-11-03T12:00:00+00:00"}, window_hours=None)
#: Naftogaz is linked ONLY to SIG2 (the entity layer missed SIG1, which names
#: it); Ukrenergo is linked to SIG1.
LINKED = [LinkedEntity("e-naft", "Naftogaz", "organization", (), (SIG2,)),
          LinkedEntity("e-ukr", "Ukrenergo", "organization", (), (SIG1,))]


@pytest.fixture(autouse=True)
def _desk(monkeypatch):
    monkeypatch.setattr(comp, "desk_subjects", lambda t: [SubjectCandidate("target", t, ["Ukraine", "Ukrainian"])])


def _ctx(**over: Any) -> GateContext:
    evidence = comp.slice_evidence(SLICE)
    offered = comp.offered_subjects("desk_ua", LINKED, evidence)
    base = dict(evidence=evidence, block_kinds={}, subjects=offered.subjects, actor_links=offered.links,
                desk_subject_count=1, handed=comp.handed_slice(SCOPE, "run-1"), target_id="desk_ua", read_at=T0,
                computed=comp.computed_block(SLICE, SCOPE, evidence=evidence), found_by=offered.found_by)
    base.update(over)
    return GateContext(**base)


def _sid(ctx: GateContext, name: str) -> str:
    return next(f"S{i}" for i, s in enumerate(ctx.subjects, start=1) if s.canonical_name == name)


def _fact(**kw: Any) -> dict[str, Any]:
    return {"id": "F1", "kind": "event", "valid_from": None, "valid_from_precision": "unknown", **kw}


def _gate(ctx: GateContext, *facts: dict[str, Any], **rows: Any):
    return {o.row_id: o for o in gate_reply(parse_rows_reply({"fact_rows": list(facts), **rows}), ctx)}


# ---------------------------------------------------------------------------
# 1a. actors named in a cited row
# ---------------------------------------------------------------------------


def test_an_actor_the_cited_row_names_but_does_not_link_is_offered_and_passes():
    ctx = _ctx()
    off = comp.offered_subjects("desk_ua", LINKED, ctx.evidence)
    assert off.found_by[f"e-naft"] == {SIG1: "named_in_text", SIG2: "linked"}
    assert off.detail["actor_row_pairs"] == {"named_in_text": 1, "linked": 2}
    fact = _fact(text="Naftogaz reported damage to a gas production facility in the Poltava region.",
                 kind="attributed", subject=_sid(ctx, "Naftogaz"),
                 cites=[{"row": 1, "quote": "Naftogaz reported damage to a gas production facility in the Poltava region"}])
    out = _gate(ctx, fact)["F1"]
    assert out.outcome == ESTABLISHED_ELIGIBLE, out.reasons
    assert out.detail["subject_found_by"] == "named_in_text"
    assert tally([out])["subject_found_by"] == {"named_in_text": 1}


def test_an_actor_neither_linked_to_nor_named_in_the_cited_row_still_fails():
    ctx = _ctx()
    fact = _fact(text="Ukrenergo said the Burshtyn power plant was struck overnight.", kind="attributed",
                 subject=_sid(ctx, "Ukrenergo"),
                 cites=[{"row": 2, "quote": "Russian forces struck Ukraine's Burshtyn power plant overnight"}])
    out = _gate(ctx, fact)["F1"]
    assert out.outcome == FAILED and "subject_not_linked_to_cited_rows" in out.reasons


# ---------------------------------------------------------------------------
# 1b. the surface subject
# ---------------------------------------------------------------------------

SURFACE_OK = _fact(text="The Kharkiv Thermal Company did not restore heating to 300 buildings.", kind="event",
                   subject="Kharkiv Thermal Company", subject_kind="surface",
                   cites=[{"row": 3, "quote": "The Kharkiv Thermal Company did not restore heating to 300 buildings"}])


def test_a_surface_subject_copied_from_the_quote_passes_and_is_counted_apart():
    ctx = _ctx()
    out = _gate(ctx, SURFACE_OK)["F1"]
    assert out.outcome == ESTABLISHED_ELIGIBLE, out.reasons
    assert is_surface(out) and out.result.draft.subject_kind == "surface"
    assert out.result.draft.subject_key == "Kharkiv Thermal Company"
    checks = {(c.gate, c.check): c.passed for c in out.result.gate.checks}
    assert checks[("D2", "surface_subject")] and ("G8", "subject") not in checks
    assert checks[("G2", "subject_named")]                     # still checked against the quote
    assert tally([out])["surface_subject"] == {ESTABLISHED_ELIGIBLE: 1}
    # the statement layer's closed subject vocabulary refuses it: never written there as is
    assert any(c.reason == "bad_subject" for c in check_statement_shape(out.result.draft, out.result.support))
    assert out.as_dict()["draft"]["subject_kind"] == "surface"


@pytest.mark.parametrize("change,reason", [
    ({"subject": "Kharkiv Heating Company"}, "surface_not_in_quote"),
    ({"subject": "the city council", "text": "The city council said heating was not restored to 300 buildings.",
      "cites": [{"row": 3, "quote": "did not restore heating to 300 buildings, the city council said"}]},
     "surface_not_proper_name"),
    ({"subject": "Officials", "text": "Officials said repairs would take 12 days.", "kind": "attributed",
      "cites": [{"row": 2, "quote": "Officials said repairs would take 12 days"}]}, "subject_generic_role"),
    ({"subject": "Naftogaz", "text": "Naftogaz reported damage to a gas production facility.",
      "cites": [{"row": 1, "quote": "Naftogaz reported damage to a gas production facility"}]},
     "surface_names_an_offered_subject"),
])
def test_a_surface_subject_is_refused_with_its_named_reason(change, reason):
    out = _gate(_ctx(), {**SURFACE_OK, **change})["F1"]
    assert out.outcome == FAILED and reason in out.reasons, out.reasons
    assert is_surface(out)


def test_generic_roles_and_name_shapes():
    for role in ("officials", "a spokesperson", "Senior military officials", "the Ministry", "security forces",
                 "Authorities"):
        assert is_generic_role(role), role
    for name in ("Ministry of Defence", "Ukrenergo", "Kharkiv Thermal Company", "al-Shabaab", "Hezbollah"):
        assert not is_generic_role(name) and is_proper_name_shaped(name), name
    for bad in ("the city council", "residents of Kharkiv", "Ukraine and", ""):
        assert not is_proper_name_shaped(bad), bad
    assert surface_refusals("Ukraine", ["Ukraine said"], [SubjectCandidate("target", "d", ["Ukraine"])]) == [
        "surface_names_an_offered_subject"]


def test_a_generic_role_is_never_a_counterpart():
    ctx = _ctx()
    fact = _fact(text="Russian forces struck Ukraine's Burshtyn power plant overnight.", subject="S1",
                 counterpart="officials",
                 cites=[{"row": 2, "quote": "Russian forces struck Ukraine's Burshtyn power plant overnight"}])
    assert "counterpart_generic_role" in _gate(ctx, fact)["F1"].reasons


# ---------------------------------------------------------------------------
# 2. the repair rule — planted smuggles refused, alignments accepted
# ---------------------------------------------------------------------------

BROKEN = _fact(id="F1", text="Russian forces struck a power plant overnight.", subject="S1",
               cites=[{"row": 2, "quote": "struck a power plant overnight, the governor said"}])   # quote_absent
QUOTE2 = "Russian forces struck Ukraine's Burshtyn power plant overnight, the regional governor said"


def _repair(row_patch: dict[str, Any], *, action: str = "fix", base: dict[str, Any] = BROKEN):
    ctx = _ctx()
    reply = parse_rows_reply({"fact_rows": [base]})
    outcomes = gate_reply(reply, ctx)
    assert outcomes[0].outcome == FAILED
    entry = {"id": base["id"], "action": action, **({"rows": row_patch} if action == "split" else {"row": row_patch})}
    fixed, detail = apply_repairs(reply, parse_repairs({"repairs": [entry]}, frozenset({base["id"]})), ctx)
    return fixed, detail, ctx


def test_a_repair_that_aligns_the_claim_to_its_new_quote_is_accepted():
    fixed, detail, ctx = _repair({"text": "Russian forces struck Ukraine's Burshtyn power plant overnight, the regional "
                                          "governor said.", "kind": "attributed",
                                  "cites": [{"row": 2, "quote": QUOTE2}]})
    assert detail["accepted"] == ["F1"], detail
    assert detail["proposals"][0]["refused"] is None
    out = gate_reply(fixed, ctx, repaired=frozenset(detail["accepted"]))[0]
    assert out.outcome == ESTABLISHED_ELIGIBLE and out.repaired, out.reasons


@pytest.mark.parametrize("text,cites,reason", [
    # a new claim in words from nowhere
    ("Russian forces struck Ukraine's Burshtyn power plant overnight, cutting power to Lviv.", None,
     "repair_adds_words_not_in_quotes"),
    # a figure the quote does not state
    ("Russian forces struck Ukraine's Burshtyn power plant overnight with 40 drones.", None,
     "repair_new_figure_not_in_quote"),
    # a date the quote does not state
    ("Russian forces struck Ukraine's Burshtyn power plant on 2 November.", None, "repair_new_date_not_in_quote"),
    # polarity flipped
    ("Russian forces did not strike Ukraine's Burshtyn power plant overnight.", None, "repair_changes_polarity"),
    # a row the run was not handed
    ("Russian forces struck Ukraine's Burshtyn power plant overnight.", [{"row": 9, "quote": QUOTE2}],
     "repair_cites_unhanded_row"),
])
def test_a_repair_that_smuggles_a_claim_is_refused(text, cites, reason):
    _, detail, _ = _repair({"text": text, "cites": cites or [{"row": 2, "quote": QUOTE2}]})
    assert detail["accepted"] == [] and detail["refused"] == {reason: 1}, detail


def test_a_repair_may_not_drop_an_attribution_or_a_condition():
    attributed = _fact(text="The governor said Russian forces struck a power plant.", kind="attributed",
                       subject="S1", cites=[{"row": 2, "quote": "struck a plant, the governor said"}])
    assert text_refusal(attributed["text"], "Russian forces struck Ukraine's Burshtyn power plant.", [QUOTE2]) == \
        "repair_drops_scope"
    assert text_refusal("Repairs could take days.", "Repairs take days.", ["repairs could take days"]) == \
        "repair_drops_scope"
    # adding an attribution its quote carries narrows the claim: allowed
    assert text_refusal("Russian forces struck Ukraine's Burshtyn power plant.",
                        "Russian forces struck Ukraine's Burshtyn power plant, the regional governor said.", [QUOTE2]) is None
    # a synonym inside the attribution class is not a change of attribution; a name is not a scope word
    assert text_refusal("Rezaei said no talks are under way with the United States.",
                        "Rezaei stated no talks are under way with the United States.", []) is None
    assert text_refusal("The State Department said talks stalled.", "The State Department stressed talks stalled.",
                        ["the State Department stressed that talks stalled"]) is None
    # "n't" is "not": the same polarity; dropping the negation is not
    assert text_refusal("Trump said the US does not need a deal.", "Trump said the US doesn't need a deal.",
                        ["the US doesn't need a deal"]) is None
    assert text_refusal("Hoya could not meet the request.", "Hoya would struggle to meet the request.",
                        ["Hoya would struggle to meet the request"]) == "repair_changes_polarity"
    # words copied from the quote, roles swapped: refused unless the quote gives that order
    swap_q = ["Hungary's foreign minister Peter Szijjarto said Russia would resume supplies to Slovakia"]
    assert text_refusal("Russia will resume supplies to Slovakia, Szijjarto said.",
                        "Slovakia will resume supplies to Russia, Szijjarto said.", swap_q) == "repair_reorders_parties"
    assert text_refusal("Szijjarto said Russia would resume supplies to Slovakia.",
                        "Russia would resume supplies to Slovakia, Szijjarto said.", swap_q) is None
    # words from a quote that is NOT in the cited row's text do not count
    _, detail, _ = _repair({"text": "Russian forces struck Ukraine's Burshtyn plant, killing workers.",
                            "cites": [{"row": 2, "quote": "struck Ukraine's Burshtyn plant, killing workers"}]})
    assert detail["refused"] == {"repair_adds_words_not_in_quotes": 1}


TWO_FIGS = _fact(id="F1", text="Officials said repairs would take 12 days and 300 buildings lost heating.",
                 kind="attributed", subject="S1",
                 cites=[{"row": 2, "quote": "Officials said repairs would take 12 days"},
                        {"row": 3, "quote": "did not restore heating to 300 buildings"}])


def test_a_multi_figure_row_may_be_split_one_figure_per_row_each_with_its_own_quote():
    parts = [{"text": "Officials said repairs would take 12 days.",
              "cites": [{"row": 2, "quote": "Officials said repairs would take 12 days"}]},
             {"text": "Officials said 300 buildings lost heating permanently.", "kind": "attributed",
              "cites": [{"row": 3, "quote": "did not restore heating to 300 buildings, the city council said"}]}]
    fixed, detail, ctx = _repair(parts, action="split", base=TWO_FIGS)
    # the second part adds "permanently" — in neither the original nor its quote: the split is refused
    assert detail["refused"] == {"repair_adds_words_not_in_quotes": 1} and [f.id for f in fixed.fact_rows] == ["F1"]
    parts[1]["text"] = "The city council said heating was not restored to 300 buildings."
    _, detail, _ = _repair(parts, action="split", base=TWO_FIGS)
    assert detail["refused"] == {"repair_changes_polarity": 1}      # a negation the original did not state
    parts[1]["text"] = "The city council said 300 buildings lost heating."
    fixed, detail, ctx = _repair(parts, action="split", base=TWO_FIGS)
    assert detail["split"] == 1 and detail["accepted"] == ["F1", "F2"], detail
    rows = {f.id: f for f in fixed.fact_rows}
    assert rows["F2"].split_from == "F1" and rows["F1"].split_from == "F1"
    outs = {o.row_id: o for o in gate_reply(fixed, ctx, repaired=frozenset(detail["accepted"]))}
    assert all("more_than_one_figure" not in o.reasons for o in outs.values())
    assert tally(list(outs.values()))["split_rows"] == 2


def test_a_split_whose_parts_share_one_quote_is_refused():
    same = [{"text": "Officials said repairs would take 12 days.",
             "cites": [{"row": 2, "quote": "Officials said repairs would take 12 days"}]},
            {"text": "Officials said repairs would take days.",
             "cites": [{"row": 2, "quote": "Officials said repairs would take 12 days"}]}]
    _, detail, _ = _repair(same, action="split", base=TWO_FIGS)
    assert detail["refused"] == {"split_parts_share_quotes": 1}
    assert parse_repairs({"repairs": [{"id": "F1", "action": "split", "rows": [{}]}]},
                         frozenset({"F1"})).refused == (("F1", "split_shape"),)


def test_the_repair_prompt_shows_where_the_cited_row_names_the_subject():
    ctx = _ctx()
    fact = _fact(text="Naftogaz reported damage to a facility.", kind="attributed", subject=_sid(ctx, "Naftogaz"),
                 cites=[{"row": 1, "quote": "reported damage to a gas production facility"}])
    reply = parse_rows_reply({"fact_rows": [fact]})
    outcomes = gate_reply(reply, ctx)
    assert "G2.subject_named:subject_not_named" in outcomes[0].reasons
    prompt = build_repair_prompt(reply, outcomes, ctx)
    assert 'ROW [1] NAMES THE SUBJECT HERE: "Naftogaz reported damage to a gas production facility' in prompt
    assert "SPLIT" in prompt and "subject_kind" in prompt
    assert DESK_GATES_VERSION == "desk_rows_gates.d2"


# ---------------------------------------------------------------------------
# 3a. the source roster
# ---------------------------------------------------------------------------

SELECTOR = [{"source_id": None, "source_selector": {"tags": ["news"], "kinds": ["rss"], "owner_tenant": "shared"}}]


def _src(sid: str, *, polls: int, errors: int = 0, productive: int = 0, tags=("news",), kind="rss") -> dict:
    return {"source_id": sid, "kind": kind, "tags": list(tags), "geo": [], "languages": [], "tenant": "shared",
            "polls": polls, "errors": errors, "productive": productive, "signals_written": productive,
            "last_outcome": None, "target_sources": SELECTOR}


def test_the_roster_is_the_targets_selector_pool_classified_by_its_polls():
    rows = [_src("source.wire.a", polls=10, productive=4), _src("s.out", polls=10, productive=2),
            _src("s.quiet", polls=10), _src("s.broken", polls=6, errors=6), _src("s.idle", polls=0),
            _src("s.tg", polls=5, kind="telegram"), _src("s.analysis", polls=3, tags=("analysis",))]
    pool, basis = roster_pool(rows)
    assert basis == "target_selector" and {r["source_id"] for r in pool} == {
        "source.wire.a", "s.out", "s.quiet", "s.broken", "s.idle"}
    st = roster_state(rows, ["source.wire.a"])
    assert st["roster_size"] == 5 and st["delivered_to_desk"] == 1 and st["delivered_out_of_scope"] == 1
    assert st["silent"] == ["s.quiet"] and st["failing"] == ["s.broken"] and st["not_polled"] == ["s.idle"]
    assert st["polling_clean"] == 3
    assert selector_admits({"tags": ["news"], "geo": ["UA"]}, {"tags": ["news"], "geo": ["PL"]}) is False
    block = comp.computed_block(SLICE, SCOPE, roster=st)
    assert block["sources_failing"] == ["s.broken"] and block["sources_silent"] == ["s.quiet"]
    assert block["sources_silent_basis"] == "poll_outcomes"
    # not read / failed read: stated as such, never an empty list
    for r in (None, {"basis": "roster_read_failed"}, roster_state([], [])):
        b = comp.computed_block(SLICE, SCOPE, roster=r)
        assert b["sources_silent"] is None and b["sources_failing"] is None
    assert "source_poll_outcomes_source_time_idx" not in ROSTER_SQL and "occurred_at >= $2" in ROSTER_SQL


def test_the_as_of_line_states_the_roster_in_counts():
    ctx = _ctx(computed=comp.computed_block(SLICE, SCOPE, roster=roster_state(
        [_src("source.wire.a", polls=4, productive=1), _src("s.broken", polls=2, errors=2)], ["source.wire.a"])))
    reply = parse_rows_reply({"fact_rows": [SURFACE_OK]})
    body, _ = render_read(reply, gate_reply(reply, ctx), ctx.computed)
    assert "of the 2 feeds in this desk's source pool, 1 failing and 0 polled with nothing new" in body.splitlines()[0]


# ---------------------------------------------------------------------------
# 3b. the lead built by code
# ---------------------------------------------------------------------------


def test_the_code_lead_takes_the_top_judgement_then_the_verified_fact():
    ctx = _ctx()
    f2 = _fact(id="F2", text="Russian forces struck Ukraine's Burshtyn power plant overnight.", subject="S1",
               cites=[{"row": 2, "quote": "Russian forces struck Ukraine's Burshtyn power plant overnight"}])
    outs = gate_reply(parse_rows_reply({"fact_rows": [SURFACE_OK, f2], "assessment_rows": [
        {"id": "J1", "text": "Strikes on generation keep supply tight.", "confidence": "likely",
         "rests_on": ["F2"]}]}), ctx)
    lead = code_lead(outs, subject_order=[s.key for s in ctx.subjects], verdicts={"F2": "supports"})
    assert lead == ("Strikes on generation keep supply tight. (likely) [2] Russian forces struck Ukraine's Burshtyn "
                    "power plant overnight. [2]")
    assert code_lead([]) == "Nothing in this window passed the desk's checks."


# ---------------------------------------------------------------------------
# Roll 85: the party-swap guard on a planted set built by code
# ---------------------------------------------------------------------------


def _measure(cases):
    from legba.data.desk_rows_gates import text_refusal as _tr
    return {f"{label}: {o} => {n}": _tr(o, n, q) for label, o, n, q in cases}


def test_the_party_swap_guard_on_the_planted_set():
    """29 swaps (subject/counterpart, actor/target in passives, voice inverted
    with the roles, speaker traded) and 24 good repairs that only reorder
    (faithful passive, moved attribution, reordered co-actors, moved place).
    D-2's order rule measured 21/29 caught and 24/24 good repairs refused;
    the role rule must catch all and refuse none."""
    from tests.data_pkg._party_swap_planted import good_cases, swap_cases
    swaps, goods = swap_cases(), good_cases()
    assert len(swaps) >= 20 and len(goods) >= 20
    missed = [k for k, why in _measure(swaps).items() if why != "repair_reorders_parties"]
    refused = [(k, why) for k, why in _measure(goods).items() if why is not None]
    assert missed == [] and refused == []


def test_the_party_swap_guard_on_the_held_out_set():
    """Written after the rule was tuned, run once as-is: 12/12 caught, 0/12 refused."""
    from tests.data_pkg._party_swap_planted import held_out_good_cases, held_out_swap_cases
    missed = [k for k, why in _measure(held_out_swap_cases()).items() if why != "repair_reorders_parties"]
    refused = [(k, why) for k, why in _measure(held_out_good_cases()).items() if why is not None]
    assert missed == [] and refused == []


def test_party_roles_reads_who_is_who():
    from legba.data.desk_rows_gates import party_roles
    assert party_roles("Russia struck Ukraine, Reuters said.") == {
        "Russia": "agent", "Ukraine": "patient", "Reuters": "speaker"}
    assert party_roles("Ukraine's plant was struck by Russia.") == {"Ukraine": "patient", "Russia": "agent"}
    assert party_roles("According to Guterres, Mali and Niger held talks in Bamako.") == {
        "Guterres": "speaker", "Mali": "agent", "Niger": "agent", "Bamako": "obl:in"}
    assert party_roles("Russia sold weapons to Poland.") == {"Russia": "agent", "Poland": "obl:to"}
    # a name used twice has no one role
    assert "Russia" not in party_roles("Russia said Russia struck Ukraine.")
