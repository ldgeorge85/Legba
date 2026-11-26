# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The forecast resolvers on fixtures (pure; no DB, no model).

Every outcome a rule can give — yes, no, void, undetermined, needs_judgement —
on fictional questions and fictional source rows, plus the guards that keep a
rule from reading background as news (retrospective sentences, the irrealis
mood, a different actor), the scoped absence, the sub-condition, the series
kind and the declared seam for the unbuilt kinds.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from legba.data import forecast_resolvers as fr

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
DUE = datetime(2026, 3, 20, 23, 59, 59, tzinfo=timezone.utc)
AFTER_GRACE = DUE + timedelta(hours=100)
_ids = itertools.count(1)


def _row(source_id: str, title: str, text: str = "", *, at: datetime = T0 + timedelta(days=3),
         source_class: str | None = "reporting", geo: tuple[str, ...] = ()) -> dict:
    return {
        "signal_id": f"00000000-0000-0000-0000-{next(_ids):012d}", "source_id": source_id,
        "fetched_at": at, "published_at": at.isoformat(), "url": f"https://{source_id}.example/a",
        "title": title, "summary": "", "text": text, "source_class": source_class,
        "source_geo": list(geo),
    }


def _exchange_q(**over) -> dict:
    q = {
        "id": "q-x", "slug": "FX-exchange", "resolver_kind": "event_reported",
        "resolver_spec": {"class": "prisoner exchange carried out", "actors": ["RU", "UA"],
                          "window": ["2026-03-01", "2026-03-20"],
                          "confirmation": "an official statement or two independent reporting sources"},
        "window_start": T0, "window_end": DUE, "due_at": DUE, "grace_hours": 72,
        "void_rules": "Neither government states anything and no outlet reports on an exchange by due + grace.",
        "condition": None, "sealed_at": T0,
    }
    q.update(over)
    return q


def _eval(q, rows=(), *, at=AFTER_GRACE, as_if_due=False, also=None, observations=()):
    parts = {"main": list(rows)}
    if also is not None:
        parts["also[1]"] = list(also)
    return fr.evaluate(q, parts, fr.ResolverContext(as_of=at, as_if_due=as_if_due), observations=observations)


_DONE = "Russia and Ukraine exchanged 300 prisoners of war on Tuesday, both governments said."


def test_two_independent_publishers_reporting_the_act_propose_yes():
    p = _eval(_exchange_q(), [_row("source.alpha.news", "Prisoner swap", _DONE),
                              _row("source.beta.world", "Exchange done", _DONE)])
    assert p.outcome == "yes" and p.lean == "yes"
    conf = next(c for c in p.clauses if c["id"] == "main:confirmation")
    assert conf["status"] == "met" and conf["independent_publishers"] == 2
    e = p.evidence[0]
    assert e["clause"] == "event" and e["published_vs_window"] == "inside_window"
    assert e["span"].startswith("Russia and Ukraine exchanged") and e["ref"].startswith("signal:")


def test_one_publisher_is_below_the_bar_undetermined_then_a_judgement():
    rows = [_row("source.alpha.news", "Prisoner swap", _DONE),
            _row("source.alpha.world", "Prisoner swap again", _DONE)]  # same publisher twice
    assert _eval(_exchange_q(), rows, at=DUE - timedelta(days=1)).outcome == "undetermined"
    p = _eval(_exchange_q(), rows)
    assert p.outcome == "needs_judgement" and p.lean == "below_bar"
    assert any(j["key"] == "main:below_bar" for j in p.judgements)


def test_an_official_source_of_an_actor_alone_confirms():
    row = _row("source.gov_ru.english", "Exchange", _DONE, source_class="official", geo=("RU",))
    assert _eval(_exchange_q(), [row]).outcome == "yes"


def test_agreement_future_and_background_sentences_are_not_reports_of_the_act():
    rows = [
        _row("source.alpha.news", "Talks end", "Russia and Ukraine agreed to exchange 1,000 prisoners."),
        _row("source.beta.news", "Swap planned", "Russia and Ukraine will exchange prisoners next week."),
        _row("source.gamma.news", "Background",
             "Russia and Ukraine have from time to time swapped prisoners during the war."),
        _row("source.delta.news", "Background", "The most recent exchange of prisoners took place in January."),
    ]
    p = _eval(_exchange_q(), rows)
    assert p.evidence == [] and p.outcome == "no"   # outlets spoke: the silence rule does not apply
    assert p.void_rule["applies"] is False and "outlets" in p.void_rule["why"]
    assert p.searched["parts"]["main"]["topical_publishers"] == ["alpha", "beta", "gamma"]  # delta names no actor


def test_a_quiet_no_show_is_arguable_and_goes_to_the_operator():
    p = _eval(_exchange_q(), [])
    assert p.outcome == "needs_judgement"
    assert p.void_rule["applies"] is True and p.void_rule["arguable"] is True
    assert [j["key"] for j in p.judgements] == ["main:quiet_no_show"]


def test_nothing_before_due_plus_grace_is_undetermined_not_no():
    p = _eval(_exchange_q(), [], at=DUE + timedelta(hours=10))
    assert p.outcome == "undetermined" and p.judgements == []


def test_a_qualitative_basis_and_an_operator_void_rule_make_yes_a_judgement():
    q = _exchange_q(
        resolver_spec={**_exchange_q()["resolver_spec"], "basis": "the release agreed at the fictional talks"},
        void_rules="Whether an exchange is the one agreed at the talks is decided by the operator as an "
                   "override_log row.",
    )
    p = _eval(q, [_row("source.alpha.news", "Swap", _DONE), _row("source.beta.news", "Swap", _DONE)])
    assert p.outcome == "needs_judgement" and p.lean == "yes"
    keys = {j["key"] for j in p.judgements}
    assert {"main:basis", "void_rule_1"} <= keys
    assert all(j["route"] == "override_log" for j in p.judgements)


def _strike_q(**spec_over) -> dict:
    spec = {"class": "strike", "actor": "IL", "target_area": "Iranian territory",
            "initiated_by": "Israel", "window": ["2026-03-01", "2026-03-20"]}
    spec.update(spec_over)
    return {"id": "q-s", "slug": "FX-strike", "resolver_kind": "event_reported", "resolver_spec": spec,
            "window_start": T0, "window_end": DUE, "due_at": DUE, "grace_hours": 72,
            "void_rules": "No primary official source and no two independent reporting sources address "
                          "strikes in the window by due + grace.", "condition": None, "sealed_at": T0}


def test_the_doer_must_be_the_one_acting_and_the_target_in_the_span():
    rows = [
        _row("source.alpha.news", "Raid", "Iran struck an Israeli base on Monday."),           # wrong doer
        _row("source.beta.news", "Raid", "Israel struck a Hamas command post in Gaza."),       # wrong target
        _row("source.gamma.news", "Raid", "Israeli jets struck a radar site near Tabriz in Iran on Monday."),
        _row("source.delta.news", "Raid", "Israel struck targets inside Iran overnight, officials said."),
    ]
    p = _eval(_strike_q(), rows)
    assert {e["publisher"] for e in p.evidence} == {"gamma", "delta"}
    assert p.outcome == "yes"


def test_attribution_doubt_is_a_judgement():
    rows = [_row("source.alpha.news", "Blast", "Israel struck a depot in Iran, according to unconfirmed reports; "
                 "the strike was suspected."),
            _row("source.beta.news", "Blast", "Israel struck a depot in Iran overnight.")]
    p = _eval(_strike_q(), rows)
    assert p.outcome == "needs_judgement"
    assert any(j["key"] == "main:attribution" for j in p.judgements)


def test_a_qualified_target_area_is_never_matched_by_rule():
    q = _strike_q(target_area="Iranian territory beyond a named island", actor="US", initiated_by=None)
    rows = [_row("source.alpha.news", "Raid", "US forces struck a site in Iran on Monday."),
            _row("source.beta.news", "Raid", "The United States struck targets in Iran.")]
    p = _eval(q, rows)
    assert p.outcome == "needs_judgement"
    assert next(c for c in p.clauses if c["id"] == "main:target")["status"] == "judgement"


def test_a_report_outside_the_window_is_not_counted():
    late = DUE + timedelta(days=30)
    p = _eval(_strike_q(), [_row("source.alpha.news", "Raid", "Israel struck targets inside Iran.", at=late)],
              at=late + timedelta(days=1))
    assert p.evidence == []
    assert any("outside the window" in n for n in p.notes)


def _halt_q() -> dict:
    return {
        "id": "q-h", "slug": "FX-halt", "resolver_kind": "official_document",
        "resolver_spec": {
            "issuers": ["RU", "UA"], "doc_type": "announcement of a halt on energy-infrastructure strikes",
            "in_effect_on": "2026-03-20",
            "also": [{"kind": "event_reported", "negate": True, "spec": {
                "class": "attributed strike on a refinery or energy infrastructure", "actors": ["RU", "UA"],
                "window": ["2026-03-13", "2026-03-20"]}}],
        },
        "window_start": datetime(2026, 3, 13, tzinfo=timezone.utc), "window_end": DUE, "due_at": DUE,
        "grace_hours": 72, "sealed_at": T0, "condition": None,
        "void_rules": "Whether an announcement is a two-way halt, or a strike is attributed, is decided by the "
                      "operator as an override_log row.",
    }


_HALT = "Russia and Ukraine announced a halt on strikes against energy infrastructure."


def test_no_document_decides_no_whatever_the_sub_condition_says():
    strike = _row("source.alpha.news", "Drone raid", "Russia struck a refinery in Ukraine overnight.",
                  at=datetime(2026, 3, 15, tzinfo=timezone.utc))
    p = _eval(_halt_q(), [], also=[strike])
    assert p.outcome == "no" and p.judgements == []


def test_a_document_and_a_negated_strike_that_holds_needs_the_operator():
    halt = [_row("source.alpha.news", "Halt", _HALT), _row("source.beta.news", "Halt", _HALT)]
    strikes = [_row("source.gamma.news", "Raid", "Russia struck a refinery in Ukraine overnight.",
                    at=datetime(2026, 3, 15, tzinfo=timezone.utc)),
               _row("source.delta.news", "Raid", "Russia struck a power plant in Ukraine overnight.",
                    at=datetime(2026, 3, 16, tzinfo=timezone.utc))]
    p = _eval(_halt_q(), halt, also=strikes)
    assert p.lean == "no"
    assert p.outcome == "needs_judgement"   # "is a strike attributed" is the operator's (void rules)
    assert any(c["id"].startswith("also[1]") and c["negated"] for c in p.clauses)


def test_a_document_and_a_quiet_week_lean_yes_with_the_halt_judgements():
    halt = [_row("source.alpha.news", "Halt", _HALT), _row("source.beta.news", "Halt", _HALT)]
    p = _eval(_halt_q(), halt, also=[])
    assert p.lean == "yes" and p.outcome == "needs_judgement"
    assert {"main:in_effect", "void_rule_1"} <= {j["key"] for j in p.judgements}


def _result_q(outcome: str = "Fictional Party candidate elected") -> dict:
    return {
        "id": "q-r", "slug": "FX-result", "resolver_kind": "official_result",
        "resolver_spec": {"authority": "Fictional Election Commission", "race": "Northtown mayor",
                          "outcome": outcome, "election_date": "2026-03-10"},
        "window_start": None, "window_end": None, "due_at": DUE, "grace_hours": 72, "sealed_at": T0,
        "void_rules": "The election is postponed; the commission publishes no result by due + grace.",
        "condition": None,
    }


def test_an_official_result_reported_twice_is_yes_and_a_rival_winner_is_a_judgement():
    at = datetime(2026, 3, 11, tzinfo=timezone.utc)
    yes = [_row("source.alpha.news", "Fictional Party wins Northtown", "The Fictional Party candidate won the Northtown mayor race.", at=at),
           _row("source.beta.news", "Northtown result", "Fictional Party's candidate was elected Northtown mayor on Sunday.", at=at)]
    assert _eval(_result_q(), yes).outcome == "yes"
    rival = _row("source.gamma.news", "Upset", "Jane Roe won the Northtown mayor race on Sunday.", at=at)
    p = _eval(_result_q(), yes + [rival])
    assert p.outcome == "needs_judgement" and any(j["key"] == "main:winner" for j in p.judgements)


def test_no_result_found_by_due_plus_grace_is_a_judgement_not_void():
    p = _eval(_result_q(), [])
    assert p.outcome == "needs_judgement" and p.void_rule["applies"] == "unknown"
    assert any(j["key"] == "main:no_result_found" for j in p.judgements)
    assert _eval(_result_q(), [], at=DUE).outcome == "undetermined"


def test_a_first_ballot_qualifier_is_checked_and_a_later_ballot_is_no():
    q = _result_q("Pat Example elected on the first ballot")
    q["resolver_spec"]["race"] = "election of the Speaker, first ballot"
    at = datetime(2026, 3, 11, tzinfo=timezone.utc)
    later = [_row(f"source.{n}.news", "Speaker", "Pat Example was elected Speaker after a third ballot.", at=at)
             for n in ("alpha", "beta")]
    assert _eval(q, later).lean == "no"
    first = [_row(f"source.{n}.news", "Speaker", "Pat Example was elected Speaker on the first ballot.", at=at)
             for n in ("alpha", "beta")]
    assert _eval(q, first).outcome == "yes"


def _series_q() -> dict:
    return {"id": "q-v", "slug": "FX-series", "resolver_kind": "series_threshold",
            "resolver_spec": {"source": "FICT", "series": "FX1", "op": ">=", "value": 100, "on": "2026-03-20"},
            "due_at": DUE, "grace_hours": 120, "sealed_at": T0, "condition": None,
            "void_rules": "The publisher publishes no value for the date by due + grace."}


def _obs(day: str, v: float) -> dict:
    return {"id": f"o-{day}", "series_id": "fict.fx1", "valid_from": day, "value": v, "unit": "usd",
            "source_url": "https://fict.example/fx1", "sha256": "a" * 64, "record_time": day}


def _obs_v(day: str, v: float, recorded: str, oid: str) -> dict:
    return {**_obs(day, v), "id": oid, "record_time": recorded}


def test_series_threshold_reads_the_date_itself_on_both_sides_of_the_level():
    p = _eval(_series_q(), observations=[_obs("2026-03-20", 101.0), _obs("2026-03-19", 90.0)])
    assert p.outcome == "yes" and p.evidence[0]["value"] == 101.0
    assert p.evidence[0]["basis"] == "the sealed date" and p.evidence[0]["ref"] == "observation:o-2026-03-20"
    assert _eval(_series_q(), observations=[_obs("2026-03-20", 99.5)]).outcome == "no"
    assert _eval(_series_q(), observations=[_obs("2026-03-20", 100.0)]).outcome == "yes"   # at or above
    assert _eval(_series_q(), observations=[_obs("2026-03-20", 101.0)], at=DUE - timedelta(days=2)).outcome == "undetermined"


def test_the_last_trading_day_stands_in_only_once_a_later_day_is_held():
    # A holiday: the publisher has released a later day without the date.
    p = _eval(_series_q(), observations=[_obs("2026-03-19", 101.0), _obs("2026-03-23", 80.0)])
    assert p.outcome == "yes" and p.evidence[0]["valid_from"].startswith("2026-03-19")
    assert "later day is held" in p.evidence[0]["basis"]
    # The date is not released yet (a weekly publisher): the day before must
    # NOT settle it — undetermined in grace, a judgement after it, never void.
    stale = [_obs("2026-03-19", 101.0)]
    assert _eval(_series_q(), observations=stale, at=DUE + timedelta(hours=100)).outcome == "undetermined"
    p = _eval(_series_q(), observations=stale, at=DUE + timedelta(hours=121))
    assert p.outcome == "needs_judgement" and p.evidence == []
    assert p.judgements[0]["key"] == "main:series_not_past_date" and p.void_rule["arguable"] is True


def test_a_late_revision_is_read_as_recorded_at_the_as_of():
    first = _obs_v("2026-03-20", 99.0, "2026-03-21T12:00:00+00:00", "o-v1")
    revised = _obs_v("2026-03-20", 100.5, "2026-03-23T12:00:00+00:00", "o-v2")
    before = _eval(_series_q(), observations=[first, revised], at=datetime(2026, 3, 22, tzinfo=timezone.utc),
                   as_if_due=True)
    assert before.lean == "no" and before.evidence[0]["ref"] == "observation:o-v1"
    after = _eval(_series_q(), observations=[first, revised])
    assert after.outcome == "yes" and after.evidence[0]["ref"] == "observation:o-v2"
    assert after.evidence[0]["record_time"].startswith("2026-03-23")


def test_a_series_not_held_is_a_coverage_gap_never_void():
    spent = DUE + timedelta(hours=121)  # this question's grace is 120 h
    assert _eval(_series_q(), observations=[], at=DUE + timedelta(hours=100)).outcome == "undetermined"
    p = _eval(_series_q(), observations=[], at=spent)
    assert p.outcome == "needs_judgement" and p.void_rule is None
    # Held only long before the date: our ingest stopped, not the publisher.
    p = _eval(_series_q(), observations=[_obs("2026-01-02", 80.0)], at=spent)
    assert p.outcome == "needs_judgement" and p.void_rule["applies"] == "unknown"
    # Held PAST the date with nothing for it or the week before: the sealed void rule.
    p = _eval(_series_q(), observations=[_obs("2026-01-02", 80.0), _obs("2026-03-25", 80.0)], at=spent)
    assert p.outcome == "void" and p.void_rule["applies"] is True


@pytest.mark.parametrize("kind", ["count_threshold", "scheduled_event_status"])
def test_the_unbuilt_kinds_refuse_loud(kind):
    with pytest.raises(fr.ResolverKindNotBuilt, match="SEAMS #76"):
        _eval({**_series_q(), "resolver_kind": kind, "resolver_spec": {}})


def test_the_rehearsal_shifts_a_window_that_has_not_opened():
    q = _halt_q()
    plans = fr.search_plans(q, fr.ResolverContext(as_of=datetime(2026, 3, 5, tzinfo=timezone.utc), as_if_due=True))
    sub = next(p for p in plans if p.part == "also[1]")
    assert sub.since < datetime(2026, 3, 5, tzinfo=timezone.utc)
    real = next(p for p in fr.search_plans(q) if p.part == "also[1]")
    assert real.since == datetime(2026, 3, 13, tzinfo=timezone.utc)


def test_void_rule_sentences_classify_without_rewriting():
    s = fr.void_rule_sentences(
        "Neither the government nor the command publishes anything on strikes in the window by due + grace; "
        "ambiguity is decided by the operator as an override_log row.")
    assert [x["kind"] for x in s] == ["silence", "operator"]
    assert s[0]["scopes"] == ["official"]
