# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 5 lane 3 — the CROSSROADS detectors, the pre-pass seam, the mandate
and the descriptor (planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §5 / §7).

Pure — no DB, no network, no event loop except the three explicitly async
tests, which drive fakes. The four detectors are pure functions over already
fetched rows for exactly this reason (the ``spread_block.build_spread_block``
split), so every threshold below is tested by handing it a synthetic substrate
and reading the answer back.

WHAT IS LOCKED HERE, and why each is the thing that would go wrong:

  * each detector FIRES on its own shape and DOES NOT FIRE one step below it —
    a threshold that is never tested from both sides is a threshold nobody can
    trust;
  * the two EMPTIES render DIFFERENTLY (the B-8 lesson: "measured and quiet" and
    "not measured" are different answers, and collapsing them is how a run with
    no wired read surface narrates a calm world);
  * the block's ``[N]`` ordinals are contiguous from 1 and every row prints one
    — the 7e contract the mandate holds the model to is worthless if the block
    itself skips an ordinal;
  * ``pre_pass_block`` NEVER raises: not on a binding that throws, not on a pool
    that throws, not on garbage options;
  * a run-health silence renders ``[[instrument]]`` and never a substrate ref
    (the journal_read pack's own rule for an instrument read with no row id);
  * the mandate names all four detectors, the ordinal fence and "a silence is a
    finding", and reaches the composed system prompt;
  * the descriptor validates on the ``inquiry`` kind with the house rules
    ($0 budget, temperature 1.0, NO max_tokens) and the three declared grants.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
import yaml

from legba.data.analysts import crossroads_detectors as cd
from legba.data.analysts import crossroads_vocab as vocab

_REPO = Path(__file__).resolve().parents[2]
_DESCRIPTOR = "descriptors/analyst_crossroads.yaml"

NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.timezone.utc)


def _finding(fid, desk, target, title, *, score=0.9, conf=0.8):
    return {
        "id": fid, "analyst_id": desk, "target_id": target, "title": title,
        "body": "", "critic_score": score, "confidence": conf,
        "effective_confidence": min(score, conf),
        "produced_at": NOW.isoformat(),
    }


def _assessment(aid, desk, target, severity, body, day):
    return {
        "id": aid, "analyst_id": desk, "target_id": target,
        "severity": severity, "body": body,
        "produced_at": datetime.datetime(
            2026, 9, day, tzinfo=datetime.timezone.utc,
        ).isoformat(),
    }


def _substrate(**kwargs):
    kwargs.setdefault("reads_ok", ("list_findings",))
    return cd.CrossroadsSubstrate(
        window_start=NOW - datetime.timedelta(hours=24), window_end=NOW, **kwargs,
    )


# ---------------------------------------------------------------------------
# The vocabulary — the word-level judgements the detectors rest on
# ---------------------------------------------------------------------------


def test_commodity_terms_are_declared_and_case_insensitive():
    assert vocab.commodity_terms("Diesel cargoes rerouted") == {"diesel"}
    # a multi-word phrase keys as a phrase, not as its parts
    assert "natural gas" in vocab.commodity_terms("Europe's natural gas storage")
    # a Title-Cased headline yields no proper nouns but still its commodities
    assert vocab.title_terms("Europe Braces For Natural Gas Squeeze") == {"natural gas"}


def test_a_title_cased_headline_yields_no_proper_nouns():
    """Capitalisation carries no information when everything is capitalised —
    the guard that stops a Title-Cased desk headline flooding the term space."""
    mid, leads = vocab.proper_candidates("Russia Steps Up Strikes On Ukraine")
    assert mid == set()
    assert leads == set()


def test_a_sentence_initial_lead_is_separated_not_dropped():
    mid, leads = vocab.proper_candidates("Russia steps up strikes on Ukraine")
    assert leads == {"russia"}          # sentence-initial: no evidence yet
    assert "ukraine" in mid             # mid-sentence: the capital IS evidence
    # a MULTI-word lead run is evidence on its own (the second word's capital)
    mid2, leads2 = vocab.proper_candidates("Saudi Arabia lifts output")
    assert "saudi arabia" in mid2 and leads2 == set()


def test_a_lead_keys_only_when_the_corpus_confirms_it():
    solo = vocab.title_terms("Russia steps up strikes")
    assert "russia" not in solo
    confirmed = vocab.title_terms("Russia steps up strikes", confirmed_leads={"russia"})
    assert "russia" in confirmed


def test_stopwords_and_the_length_floor_drop_non_entities():
    mid, leads = vocab.proper_candidates("Reports say the Ministry met on Friday")
    assert mid == set() and leads == set()


def test_polarity_is_signed_and_a_tie_is_zero():
    assert vocab.title_polarity("Escalation intensifies on the border") == 1
    assert vocab.title_polarity("Ceasefire holds as pressure eases") == -1
    assert vocab.title_polarity("A quiet week for the ministry") == 0
    # one up + one down is a TIE, and a tie is NO claim — never a coin flip.
    assert vocab.title_polarity("Escalation eases") == 0


def test_cited_mass_counts_distinct_markers_across_the_three_shapes():
    body = "a [1] b [2] c [1] d [[ref:3]] e [[ref:0f8b3c2a-1111-4222-8333-444455556666]]"
    assert vocab.cited_mass(body) == 4   # {1,2} + {3} + {uuid}
    assert vocab.cited_mass("") == 0
    assert vocab.cited_mass(None) == 0


# ---------------------------------------------------------------------------
# (a) PATTERNS
# ---------------------------------------------------------------------------


def test_patterns_fire_at_three_desks_and_not_at_two():
    base = [
        _finding("f1", "escalation", "t1", "Strikes deepen inside Russia"),
        _finding("f2", "energy_security", "t2", "Refinery hit inside Russia"),
    ]
    assert cd.detect_patterns(base) == []          # two desks is coverage
    base.append(_finding("f3", "economic_coercion", "t3", "Sanctions on Russia widen"))
    rows = cd.detect_patterns(base)
    assert [r.subject for r in rows] == ["russia"]
    assert rows[0].desks == ("economic_coercion", "energy_security", "escalation")
    assert "3 distinct desks" in rows[0].arithmetic
    assert rows[0].ledger_kind == "observation"
    assert rows[0].resolution_test


def test_patterns_ignore_unverified_findings():
    """An unverified claim is not evidence of convergence — the faithfulness
    floor this reuses is the scorecard engine's own, not a second one."""
    rows = cd.detect_patterns([
        _finding("f1", "d1", "t1", "Strikes deepen inside Russia"),
        _finding("f2", "d2", "t2", "Refinery hit inside Russia"),
        _finding("f3", "d3", "t3", "Sanctions on Russia widen", score=0.2),
    ])
    assert rows == []
    # ... and a finding with NO verdict at all is likewise not verified
    assert not cd.is_verified({"critic_score": None})
    assert cd.is_verified({"critic_score": 0.5})


def test_patterns_do_not_fire_on_the_same_desk_three_times():
    rows = cd.detect_patterns([
        _finding(f"f{i}", "escalation", f"t{i}", "Strikes deepen inside Russia")
        for i in range(3)
    ])
    assert rows == []


def test_patterns_are_capped_and_ordered_strongest_first():
    rows = cd.detect_patterns(
        [
            _finding(f"a{i}", f"d{i}", "t", "Prices for diesel and crude climb")
            for i in range(4)
        ] + [
            _finding(f"b{i}", f"e{i}", "t", "Wheat cargoes delayed")
            for i in range(3)
        ],
        max_rows=1,
    )
    assert len(rows) == 1
    assert len(rows[0].desks) == 4      # the widest convergence survives the cap


# ---------------------------------------------------------------------------
# (b) DRIFTS
# ---------------------------------------------------------------------------

_FLAT_MASS = "[1] [2] [3]"


def test_drift_fires_when_the_band_climbs_and_the_mass_does_not():
    rows = cd.detect_drifts([
        _assessment("a1", "escalation", "country_watch_ir", "low", _FLAT_MASS, 20),
        _assessment("a2", "escalation", "country_watch_ir", "moderate", _FLAT_MASS, 21),
        _assessment("a3", "escalation", "country_watch_ir", "elevated", _FLAT_MASS, 22),
        _assessment("a4", "escalation", "country_watch_ir", "high", _FLAT_MASS, 23),
    ])
    assert len(rows) == 1
    assert rows[0].kind == "drift"
    assert "low -> watch -> elevated -> high" in rows[0].arithmetic
    assert "cited mass 3 -> 3 (unchanged)" in rows[0].arithmetic
    assert rows[0].ledger_kind == "hypothesis"       # design §3: needs a test
    assert "Frozen at write" in rows[0].resolution_test


def test_drift_does_not_fire_when_the_evidence_follows_the_band():
    rows = cd.detect_drifts([
        _assessment("a1", "escalation", "t", "low", "[1]", 20),
        _assessment("a2", "escalation", "t", "moderate", "[1] [2]", 21),
        _assessment("a3", "escalation", "t", "elevated", "[1] [2] [3]", 22),
        _assessment("a4", "escalation", "t", "high", "[1] [2] [3] [4]", 23),
    ])
    assert rows == []


def test_drift_does_not_fire_on_a_non_monotone_band():
    rows = cd.detect_drifts([
        _assessment("a1", "escalation", "t", "low", _FLAT_MASS, 20),
        _assessment("a2", "escalation", "t", "elevated", _FLAT_MASS, 21),
        _assessment("a3", "escalation", "t", "moderate", _FLAT_MASS, 22),
        _assessment("a4", "escalation", "t", "high", _FLAT_MASS, 23),
    ])
    assert rows == []


def test_drift_needs_four_rows_for_three_steps():
    rows = cd.detect_drifts([
        _assessment("a1", "escalation", "t", "low", _FLAT_MASS, 20),
        _assessment("a2", "escalation", "t", "moderate", _FLAT_MASS, 21),
        _assessment("a3", "escalation", "t", "elevated", _FLAT_MASS, 22),
    ])
    assert rows == []


def test_an_unmappable_severity_skips_the_group_rather_than_guessing():
    rows = cd.detect_drifts([
        _assessment("a1", "escalation", "t", "low", _FLAT_MASS, 20),
        _assessment("a2", "escalation", "t", "unheard-of", _FLAT_MASS, 21),
        _assessment("a3", "escalation", "t", "elevated", _FLAT_MASS, 22),
        _assessment("a4", "escalation", "t", "high", _FLAT_MASS, 23),
    ])
    assert rows == []
    assert cd.band_rank("unheard-of") is None
    assert cd.band_rank("moderate") == 1     # the SEVERITY_TO_BAND 'watch' rung


def test_drift_reads_the_band_ladder_the_scorecard_engine_publishes():
    """The crossroads must not carry a private copy of the band ladder: two
    instruments disagreeing about what 'elevated' means is a silent divergence
    nobody would notice until a card and an entry contradicted each other."""
    from legba.data.analysts.deterministic_handlers import scorecard_banding

    for severity, band in scorecard_banding.SEVERITY_TO_BAND.items():
        assert cd.band_rank(severity) == scorecard_banding.BAND_LADDER.index(band)


# ---------------------------------------------------------------------------
# (c) CONTRADICTIONS
# ---------------------------------------------------------------------------

_NAMES = {"country_watch_ir": "Iran"}


def test_contradiction_fires_on_two_desks_with_no_contention():
    rows = cd.detect_contradictions(
        [
            _finding("up", "escalation", "country_watch_ir",
                     "Border escalation intensifies"),
            _finding("down", "internal_stability", "country_watch_ir",
                     "Ceasefire holds and pressure eases"),
        ],
        frozenset(),
        target_names=_NAMES,
    )
    assert len(rows) == 1
    assert rows[0].desks == ("escalation", "internal_stability")
    assert rows[0].refs == ("up", "down")
    assert rows[0].ledger_kind == "question"
    assert "correct_situation" in (rows[0].dispatch or "")
    assert "does NOT grant journal_propose" in (rows[0].dispatch or "")


def test_contradiction_is_keyed_on_the_targets_name_not_its_slug():
    """``fact_contention.subject_key`` is ``lower(subject)`` over the FACTS
    table, and a fact subject is an entity as written. Probing the slug would
    miss every real contention and fire on every pair the arbiter already
    holds — the bug this assertion exists to pin."""
    findings = [
        _finding("up", "escalation", "country_watch_ir",
                 "Border escalation intensifies"),
        _finding("down", "internal_stability", "country_watch_ir",
                 "Ceasefire holds and pressure eases"),
    ]
    # the arbiter holding the NAME suppresses the row ...
    assert cd.detect_contradictions(
        findings, {"iran"}, target_names=_NAMES) == []
    # ... and holding the SLUG does not, because that is not what it keys on.
    assert len(cd.detect_contradictions(
        findings, {"country_watch_ir"}, target_names=_NAMES)) == 1
    assert cd.contention_subject_key("  Iran ") == "iran"


def test_contradiction_needs_two_different_desks():
    rows = cd.detect_contradictions(
        [
            _finding("up", "escalation", "country_watch_ir",
                     "Border escalation intensifies"),
            _finding("down", "escalation", "country_watch_ir",
                     "Ceasefire holds and pressure eases"),
        ],
        frozenset(),
        target_names=_NAMES,
    )
    assert rows == []


def test_contradiction_skips_a_target_it_cannot_name():
    """No name means the arbiter condition cannot be checked, and firing on an
    unchecked condition is exactly how a false contradiction gets written."""
    rows = cd.detect_contradictions(
        [
            _finding("up", "escalation", "country_watch_ir",
                     "Border escalation intensifies"),
            _finding("down", "internal_stability", "country_watch_ir",
                     "Ceasefire holds and pressure eases"),
        ],
        frozenset(),
        target_names={},
    )
    assert rows == []


def test_contradiction_does_not_fire_without_opposite_polarity():
    rows = cd.detect_contradictions(
        [
            _finding("a", "escalation", "country_watch_ir",
                     "Border escalation intensifies"),
            _finding("b", "internal_stability", "country_watch_ir",
                     "Protests continue in three provinces"),
        ],
        frozenset(),
        target_names=_NAMES,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# (d) SILENCES
# ---------------------------------------------------------------------------

_ROSTER = [
    {"target_id": "country_watch_ir", "name": "Iran", "tags": ["watch"]},
    {"target_id": "country_g20_de", "name": "Germany", "tags": ["g20"]},
    {"target_id": "country_g20_br", "name": "Brazil", "tags": ["g20"]},
    {"target_id": "lane_suez", "name": "Suez lane", "tags": ["supply_chain"]},
]
_LENS = [{
    "id": "11111111-1111-4111-8111-111111111111", "analyst_id": "lens_left",
    "title": "The week", "body": "Iran and Germany dominate the window",
}]


def test_a_quiet_unit_fires_and_a_fresh_one_does_not():
    rows = cd.detect_silences(
        [
            {"analyst_id": "silent_desk", "hours_ago": 400.0},
            {"analyst_id": "busy_desk", "hours_ago": 2.0},
        ],
        [], [],
    )
    assert [r.subject for r in rows] == ["silent_desk"]
    assert "3 cadences = 72h" in rows[0].arithmetic


def test_a_unit_exactly_at_the_threshold_is_not_yet_silent():
    assert cd.detect_silences([{"analyst_id": "d", "hours_ago": 72.0}], [], []) == []
    assert len(cd.detect_silences([{"analyst_id": "d", "hours_ago": 72.1}], [], [])) == 1


def test_a_unit_with_no_resolvable_last_run_is_silent():
    rows = cd.detect_silences([{"analyst_id": "d", "hours_ago": None}], [], [])
    assert "no resolvable last run" in rows[0].arithmetic


def test_a_run_health_silence_carries_no_substrate_ref():
    """A trace is keyed by run_id, not by a citeable substrate row — the port
    returns ``refs: []`` for exactly that reason, so the row renders
    ``[[instrument]]`` and never a fabricated uuid."""
    row = cd.detect_silences([{"analyst_id": "d", "hours_ago": 400.0}], [], [])[0]
    assert row.refs == () and row.instrument_only is True
    block = cd.render_crossroads_block([row], _substrate())
    assert "[[instrument]]" in block
    assert "[[ref:" not in block


def test_the_country_leg_names_only_countries_no_lens_named():
    rows = cd.detect_silences([], _LENS, _ROSTER)
    assert len(rows) == 1
    row = rows[0]
    assert "Brazil" in row.arithmetic
    assert "Iran" not in row.arithmetic and "Germany" not in row.arithmetic
    # a supply-chain lane is a unit, not a country — it is out of this denominator
    assert "1 of 3 roster countries" in row.arithmetic
    # the lens rows ARE citeable journal ids
    assert row.refs == ("11111111-1111-4111-8111-111111111111",)


def test_the_country_leg_is_silent_when_every_country_was_named():
    lens = [dict(_LENS[0], body="Iran, Germany and Brazil all moved")]
    assert cd.detect_silences([], lens, _ROSTER) == []


def test_the_country_leg_reports_nothing_when_no_lens_row_was_read():
    """No lens rows means the aperture was not measured; reporting every
    country as skipped would be a fabricated silence, not a cautious one."""
    assert cd.detect_silences([], [], _ROSTER) == []


# ---------------------------------------------------------------------------
# The renderer — the 7e ordinal contract
# ---------------------------------------------------------------------------


def _row(kind="pattern", subject="x"):
    return cd.CrossroadsRow(
        kind=kind, subject=subject, desks=("d1", "d2"), arithmetic="2 desks",
        refs=("r1",), ledger_kind="observation", ledger_text="t",
        resolution_test="rt",
    )


def test_ordinals_are_contiguous_from_one_and_every_row_prints_one():
    rows = [_row(subject=f"s{i}") for i in range(4)]
    block = cd.render_crossroads_block(rows, _substrate())
    for n in range(1, 5):
        assert f"  [{n}] PATTERN — s{n - 1}" in block
    assert "[5]" not in block
    assert block.count("arithmetic:") == 4
    assert block.count("RESOLUTION TEST:") == 4


def test_the_scope_line_states_what_was_read():
    sub = _substrate(
        findings=({"id": "a"},), assessments=({"id": "b"},),
        lens_ids_expected=("lens_left", "lens_right"),
        lens_rows=({"analyst_id": "lens_left"},),
        roster=({"target_id": "t"},),
    )
    block = cd.render_crossroads_block([], sub)
    assert "findings read 1" in block
    assert "lens ids seen 1 of 2" in block
    assert cd.METHOD_VERSION in block


def test_a_failed_read_withholds_its_count():
    """2026-09-24, the first forced run: the scope line printed
    'findings read 0 (cap 200)' beside 'READS THAT FAILED: list_findings', and
    the model's ledger hypothesis became 'a systemic pause caused every desk
    to produce no findings'. A failed read is not a read of zero rows: the
    count is withheld and the line says UNMEASURED, per read, by name."""
    sub = cd.CrossroadsSubstrate(
        window_start=NOW - datetime.timedelta(hours=24), window_end=NOW,
        reads_ok=("get_run_health",),
        reads_failed=("list_findings", "get_assessments"),
    )
    block = cd.render_crossroads_block([], sub)
    assert "findings UNMEASURED (list_findings read failed)" in block
    assert "assessments UNMEASURED (get_assessments read failed)" in block
    assert "findings read 0" not in block
    assert "assessments read 0" not in block
    assert "READS THAT FAILED (their detectors are UNMEASURED, not quiet)" in block
    # A read that succeeded with nothing in it is still a count.
    ok = cd.render_crossroads_block([], _substrate(reads_ok=("list_findings",)))
    assert "findings read 0 (cap" in ok


def test_the_two_empties_render_differently():
    """THE B-8 lesson. 'measured and quiet' and 'not measured' are different
    answers; collapsing them lets a run with no read surface narrate a calm
    world."""
    measured = cd.render_crossroads_block([], _substrate())
    unmeasured = cd.render_crossroads_block(
        [], cd.CrossroadsSubstrate(window_start=NOW, window_end=NOW, reads_ok=()),
    )
    assert "no detector fired" in measured
    assert "DETECTORS DID NOT RUN" not in measured
    assert "DETECTORS DID NOT RUN" in unmeasured
    assert "no detector fired" not in unmeasured
    assert measured != unmeasured


def test_a_failed_read_is_named_as_unmeasured_not_quiet():
    block = cd.render_crossroads_block(
        [], _substrate(reads_failed=("get_run_health",)),
    )
    assert "READS THAT FAILED" in block
    assert "UNMEASURED, not quiet" in block
    assert "get_run_health" in block


def test_run_detectors_survives_one_detector_raising(monkeypatch):
    monkeypatch.setattr(
        cd, "detect_drifts",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    sub = _substrate(findings=tuple(
        _finding(f"f{i}", f"d{i}", f"t{i}", "Strikes deepen inside Russia")
        for i in range(3)
    ))
    rows = cd.run_detectors(sub)
    assert [r.kind for r in rows] == ["pattern"]   # the other three still ran


# ---------------------------------------------------------------------------
# The seam — pre_pass_block
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, output, status="completed"):
        self.output = output
        self.status = status


class _Outcome:
    def __init__(self, output, admitted=True, status="completed"):
        self.admitted = admitted
        self.block_cause = None
        self.tool_result = _Result(output, status) if output is not None else None


class _Binding:
    """A governed binding whose ``run_tool`` answers from a canned map — the
    real dispatch shape (``AgencyOutcome``), so the pre-pass is exercised
    through the same triage the live path uses."""

    def __init__(self, payloads, *, explode=False):
        self.payloads = payloads
        self.explode = explode
        self.calls: list[str] = []

    async def run_tool(self, name, args):
        self.calls.append(name)
        if self.explode:
            raise RuntimeError("binding down")
        return _Outcome(self.payloads.get(name))


class _Pool:
    def __init__(self, rows=(), explode=False):
        self.rows = rows
        self.explode = explode

    async def fetch(self, sql, *args):
        if self.explode:
            raise RuntimeError("pool down")
        return list(self.rows)


class _Deps:
    def __init__(self, pool=None):
        self.pg_pool = pool


@pytest.mark.asyncio
async def test_pre_pass_block_reads_through_the_governed_binding():
    binding = _Binding({
        "list_findings": {"rows": [
            _finding("f1", "escalation", "t1", "Strikes deepen inside Russia"),
            _finding("f2", "energy_security", "t2", "Refinery hit inside Russia"),
            _finding("f3", "economic_coercion", "t3", "Sanctions on Russia widen"),
        ]},
        "get_assessments": {"rows": []},
        "get_run_health": {"rows": []},
        "list_targets": {"rows": _ROSTER},
    })
    bindings = {
        n: binding for n in
        ("list_findings", "get_assessments", "get_run_health", "list_targets")
    }
    block = await cd.pre_pass_block(
        {"gather_tool_bindings": bindings, "now": NOW.isoformat()},
        _Deps(_Pool()),
    )
    assert "CROSSROADS BLOCK" in block
    assert "[1] PATTERN — russia" in block
    # every governed read was actually dispatched through the binding
    assert set(binding.calls) == {
        "list_findings", "get_assessments", "get_run_health", "list_targets",
    }


@pytest.mark.asyncio
async def test_pre_pass_block_degrades_when_nothing_is_wired():
    block = await cd.pre_pass_block({}, _Deps(None))
    assert "DETECTORS DID NOT RUN" in block
    assert "no detector fired" not in block


@pytest.mark.asyncio
async def test_pre_pass_block_never_raises_on_a_broken_surface():
    exploding = _Binding({}, explode=True)
    block = await cd.pre_pass_block(
        {"gather_tool_bindings": {"list_findings": exploding}},
        _Deps(_Pool(explode=True)),
    )
    assert isinstance(block, str) and "CROSSROADS BLOCK" in block
    # every read failed, so the block must say NOT RUN rather than "quiet"
    assert "DETECTORS DID NOT RUN" in block


def test_a_vacuous_leaf_probe_does_not_count_as_a_read():
    """A probe that ran with nothing to look up is not evidence the surface
    works. If it flipped ``any_read_succeeded`` a run with a dead pool AND a
    dead binding would render 'a measured quiet window'."""
    sub = _substrate(reads_ok=("fact_contention (leaf)",))
    assert sub.any_read_succeeded is False
    assert "DETECTORS DID NOT RUN" in cd.render_crossroads_block([], sub)
    assert _substrate(reads_ok=("list_targets",)).any_read_succeeded is True


@pytest.mark.asyncio
async def test_pre_pass_block_never_raises_on_garbage_options():
    for bad in (None, "nonsense", 17, {"gather_tool_bindings": "not-a-map"},
                {"window_hours": "soon"}):
        block = await cd.pre_pass_block(bad, _Deps(None))
        assert isinstance(block, str) and block


@pytest.mark.asyncio
async def test_a_refused_tool_call_is_a_failed_read_not_an_empty_one():
    """An un-admitted governed call means the read did not happen. Treating it
    as ``rows: []`` would turn a grant problem into a quiet world."""
    class _Refused(_Binding):
        async def run_tool(self, name, args):
            self.calls.append(name)
            return _Outcome(None, admitted=False)

    binding = _Refused({})
    sub = await cd.fetch_crossroads_substrate(
        tool_bindings={"list_findings": binding}, pg=None,
        lens_analyst_ids=(), now=NOW,
    )
    assert "list_findings" in sub.reads_failed
    assert "list_findings" not in sub.reads_ok


@pytest.mark.asyncio
async def test_nothing_to_probe_is_a_clean_read_not_a_failed_one():
    """The null / ``[]`` distinction again, one layer down: "the arbiter holds
    none of these subjects" is a measurement; "the probe could not run" is
    not."""
    ran = await cd._fetch_contended_subjects(_Pool(), [])
    assert ran == frozenset()                      # ran, found nothing
    assert await cd._fetch_contended_subjects(None, ["iran"]) is None
    assert await cd._fetch_contended_subjects(_Pool(explode=True), ["iran"]) is None


@pytest.mark.asyncio
async def test_the_arbiter_probe_is_a_bounded_membership_read():
    """One ``= ANY($1::text[])`` call over the whole window's subject names —
    never a per-row probe (the query shape that has stalled this box twice)."""
    calls: list[tuple] = []

    class _Recording(_Pool):
        async def fetch(self, sql, *args):
            calls.append((sql, args))
            return []

    binding = _Binding({
        "list_findings": {"rows": [
            _finding("f1", "d1", "country_watch_ir", "Escalation intensifies"),
            _finding("f2", "d2", "country_g20_de", "Pressure eases"),
        ]},
        "get_assessments": {"rows": []},
        "get_run_health": {"rows": []},
        "list_targets": {"rows": _ROSTER},
    })
    bindings = {
        n: binding for n in
        ("list_findings", "get_assessments", "get_run_health", "list_targets")
    }
    await cd.fetch_crossroads_substrate(
        tool_bindings=bindings, pg=_Recording(),
        lens_analyst_ids=("lens_left",), now=NOW,
    )
    contention = [c for c in calls if "fact_contention" in c[0]]
    assert len(contention) == 1                 # ONE call, not one per finding
    assert "= ANY($1::text[])" in contention[0][0]
    assert sorted(contention[0][1][0]) == ["germany", "iran"]   # names, not slugs


@pytest.mark.asyncio
async def test_the_lens_leaf_read_asks_for_the_whole_lens_roster():
    """The governed ``get_lens_reads`` tool is fenced to the four faculties on
    purpose; answering "a country EVERY lens skipped" off four of ten would
    report a country ``lens_left`` named as unnamed — a fabricated silence."""
    from legba.data.analysts.journal_assessor import LENS_ANALYST_IDS

    calls: list[tuple] = []

    class _Recording(_Pool):
        async def fetch(self, sql, *args):
            calls.append((sql, args))
            return []

    await cd.fetch_crossroads_substrate(
        tool_bindings={}, pg=_Recording(),
        lens_analyst_ids=cd._lens_roster(), now=NOW,
    )
    lens_calls = [c for c in calls if "entry_kind = 'lens'" in c[0]]
    assert len(lens_calls) == 1
    assert sorted(lens_calls[0][1][0]) == sorted(LENS_ANALYST_IDS)
    assert len(LENS_ANALYST_IDS) > 4


# ---------------------------------------------------------------------------
# The mandate
# ---------------------------------------------------------------------------


def test_the_mandate_names_all_four_detectors():
    from legba.prompts.crossroads import CROSSROADS_MANDATE

    for anchor in ("(a) PATTERNS", "(b) DRIFTS", "(c) CONTRADICTIONS",
                   "(d) SILENCES"):
        assert anchor in CROSSROADS_MANDATE


def test_the_mandate_says_what_no_desk_can_say_and_never_a_new_fact():
    from legba.prompts.crossroads import CROSSROADS_SYSTEM

    assert "you never assert a new fact" in CROSSROADS_SYSTEM
    assert "none of them is positioned to say" in CROSSROADS_SYSTEM
    assert "not a fifth opinion" in CROSSROADS_SYSTEM.lower()


def test_the_mandate_says_a_silence_is_a_finding():
    from legba.prompts.crossroads import CROSSROADS_MANDATE

    assert "A SILENCE IS A FINDING" in CROSSROADS_MANDATE


def test_the_mandate_carries_the_ordinal_fence():
    from legba.prompts.crossroads import CROSSROADS_SYSTEM

    assert "CITE BY ORDINAL" in CROSSROADS_SYSTEM
    assert "NEVER RECOMPUTE" in CROSSROADS_SYSTEM
    assert "AN ORDINAL YOU INVENT FAILS THE RUN" in CROSSROADS_SYSTEM
    assert "[[instrument]]" in CROSSROADS_SYSTEM


def test_the_mandate_keeps_the_two_empties_apart():
    from legba.prompts.crossroads import CROSSROADS_HONESTY

    assert "no detector fired" in CROSSROADS_HONESTY
    assert "DETECTORS DID NOT RUN" in CROSSROADS_HONESTY
    # the non-adjudication rule is load-bearing and must survive an edit
    assert "NEVER SETTLE A CONTRADICTION" in CROSSROADS_HONESTY


def test_every_mandate_piece_reaches_the_composed_system_prompt():
    from legba.prompts import crossroads as mod

    for piece in (mod.CROSSROADS_PERSONA, mod.CROSSROADS_BLOCK_FENCE,
                  mod.CROSSROADS_MANDATE, mod.CROSSROADS_HONESTY,
                  mod.CROSSROADS_NARRATE_PREAMBLE):
        assert piece in mod.CROSSROADS_SYSTEM
    assert set(mod.__all__) >= {"CROSSROADS_SYSTEM", "CROSSROADS_MANDATE"}


def test_the_mandate_line_the_block_prints_agrees_with_the_renderer():
    """The mandate quotes the two empty lines verbatim. If the renderer's
    wording moves and the mandate's does not, the model is being held to a
    string it will never see."""
    from legba.prompts.crossroads import CROSSROADS_HONESTY

    assert "no detector fired" in cd._NO_FIRE_LINE
    assert "no detector fired" in CROSSROADS_HONESTY
    assert "DETECTORS DID NOT RUN" in cd._NOT_RUN_LINE
    assert "DETECTORS DID NOT RUN" in CROSSROADS_HONESTY


# ---------------------------------------------------------------------------
# The descriptor
# ---------------------------------------------------------------------------


@pytest.fixture()
def crossroads_body():
    body = yaml.safe_load((_REPO / _DESCRIPTOR).read_text(encoding="utf-8"))
    body.setdefault("identity", {})["version"] = "0" * 16
    return body


def test_crossroads_descriptor_validates(crossroads_body):
    """The ``inquiry`` kind and its option catalog are lane p5_kind's to ship;
    this registers both locally so the DESCRIPTOR's own shape is under test
    here rather than the other lane's progress. The autouse
    ``_preserve_analyst_kind_registry`` fixture restores the kind registry at
    the test boundary; the catalog entry is popped in the finally."""
    from legba.data.analysts.handler_options import (
        ANALYST_KIND_OPTIONS,
        OptionSpec,
    )
    from legba.data.schemas.analyst import AnalystDescriptor, register_analyst_kind

    register_analyst_kind("inquiry")
    added = "inquiry" not in ANALYST_KIND_OPTIONS
    if added:
        ANALYST_KIND_OPTIONS["inquiry"] = (
            OptionSpec("pre_pass_module", "str", "the deterministic pre-pass hook"),
        )
    try:
        desc = AnalystDescriptor.model_validate(crossroads_body, strict=False)
    finally:
        if added:
            ANALYST_KIND_OPTIONS.pop("inquiry", None)

    assert desc.identity.id == "crossroads"
    assert desc.identity.kind == "inquiry"
    assert desc.identity.state.value == "draft"   # the orchestrator promotes
    assert desc.method.options["pre_pass_module"] == (
        "legba.data.analysts.crossroads_detectors:pre_pass_block"
    )


def test_crossroads_descriptor_house_rules(crossroads_body):
    method = crossroads_body["method"]
    assert int(method["budget_tokens_per_day"]) == 0
    assert float(method["llm"]["temperature"]) == 1.0
    assert "max_tokens" not in method["llm"]      # the core plane never caps output
    assert "verify" in method["llm"]              # the journal-family V1 gate
    assert method["prompt_module"] == "legba.prompts.crossroads:CROSSROADS_SYSTEM"


def test_crossroads_descriptor_grants_and_cadence(crossroads_body):
    packs = {p["pack_id"] for p in crossroads_body["action_packs"]}
    # journal_propose is the dispatch for a contradiction row (added at the merge).
    assert packs == {"journal_read", "substrate_read", "inquiry_state", "journal_propose"}
    assert "web_access" not in packs and "research" not in packs
    assert "journal_propose" in packs             # the dispatch: a contradiction goes out as a proposal through the journal gate (merge decision 2026-09-24)
    assert crossroads_body["outputs"] == []
    # daily, AFTER the lens band (the six leans finish at 11:30 UTC)
    assert crossroads_body["cadence"]["fallback_schedule"] == "0 12 * * *"
    assert int(crossroads_body["cadence"]["cooldown_seconds"]) < 86400
    assert crossroads_body.get("grounding", {}).get("enabled") is False


def test_the_pre_pass_module_ref_resolves_to_a_real_callable(crossroads_body):
    """The COLON ``module:ATTR`` form, resolved the way the kind will resolve
    it — a typo here is a silently missing block, not an error."""
    import importlib

    ref = crossroads_body["method"]["options"]["pre_pass_module"]
    module_path, _, attr = ref.partition(":")
    assert attr, "pre_pass_module must use the COLON module:ATTR form"
    hook = getattr(importlib.import_module(module_path), attr)
    assert callable(hook)
    assert hook is cd.pre_pass_block


def test_the_prompt_module_ref_resolves_to_the_mandate(crossroads_body):
    import importlib

    from legba.prompts.crossroads import CROSSROADS_SYSTEM

    module_path, _, attr = crossroads_body["method"]["prompt_module"].partition(":")
    assert getattr(importlib.import_module(module_path), attr) is CROSSROADS_SYSTEM


def test_the_manifest_carries_the_new_descriptor_state():
    """``descriptor_prompts.json`` is a build output; a new descriptor adds a
    ``states`` row and the production gauge compares against it."""
    from legba.data.registry.production_gauge_integrity import load_state_manifest

    states = load_state_manifest()
    assert states["analyst:crossroads"]["state"] == "draft"
    assert states["analyst:crossroads"]["source"] == "analyst_crossroads.yaml"


def test_the_inquiry_option_catalog_must_declare_the_pre_pass_hook(crossroads_body):
    """THE CROSS-LANE CONTRACT — asserted over live state, never skipped.

    Lane p5_kind owns ``ANALYST_KIND_OPTIONS['inquiry']`` (it ships ``brief``).
    Two assertions, both real: this descriptor's ONE option key is
    ``pre_pass_module`` (unconditional), and — the moment that catalog entry
    lands — it must declare that key. Without it ``AnalystDescriptor`` refuses
    this descriptor's whole options block as structurally inert (X-1/QW1-B) and
    the crossroads would register with no detectors at all. A skip here would
    hide exactly the integration failure this guard exists for, so the
    catalog-absent case simply has nothing yet to check rather than opting the
    test out.
    """
    from legba.data.analysts.handler_options import ANALYST_KIND_OPTIONS

    assert set(crossroads_body["method"]["options"]) == {"pre_pass_module"}
    specs = ANALYST_KIND_OPTIONS.get("inquiry")
    if specs is not None:
        assert "pre_pass_module" in {s.name for s in specs}
