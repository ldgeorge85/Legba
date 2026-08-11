# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CW-4 / CW-5 — the contention-question guards, and the K-4 R3 replay.

The parametrised cases are VERBATIM (thesis, signal title) pairs from
``planning/K4_R3_WORKSHEET_LABELED.csv`` with their measured labels, so the
anchor's regression net is the evidence that justified it.

The replay test at the bottom is the CW train's PRE-REGISTERED acceptance
gate. It skips when the labeled worksheet is absent, because ``planning/`` is
gitignored — the CSVs are internal evidence, not shipped code. That is a real
limitation and it is why the guard cases above are inlined here: they keep the
detector honest in a checkout that has no worksheet at all.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from legba.data.analysts.deterministic_handlers import claim_watch_guards as g

REPO = Path(__file__).resolve().parents[2]
#: Overridable so the gate is runnable from a git WORKTREE, which has no
#: planning/ of its own (the directory is gitignored — it lives only in the
#: main checkout).
WORKSHEET = Path(
    os.environ.get(
        "LEGBA_K4_R3_WORKSHEET", REPO / "planning" / "K4_R3_WORKSHEET_LABELED.csv"
    )
)


# ---------------------------------------------------------------------------
# Parsing — the guards apply to contention questions and to NOTHING else
# ---------------------------------------------------------------------------


def test_a_harvested_contention_thesis_is_parsed():
    key = g.contention_key(
        'Contested fact: which value of "operates in" for "israel defense '
        'forces" is correct? 2 competing value clusters; current surfaced '
        'winner: "the west bank".'
    )
    assert key == g.ContentionKey("israel defense forces", "operates in")


@pytest.mark.parametrize(
    "thesis",
    [
        "Will Iran proceed with closing the Strait of Hormuz?",
        "Collection gap: the leadership_transition dimension for desk X "
        "is starved. What sources would close it?",
        "",
        None,
        'Contested fact: which value of "" for "" is correct?',
    ],
)
def test_every_other_shape_is_out_of_scope(thesis):
    assert g.contention_key(thesis) is None


# ---------------------------------------------------------------------------
# CW-5 — the subject anchor
# ---------------------------------------------------------------------------

_TEXAS = (
    'Contested fact: which value of "located in" for "texas" is correct? '
    '2 competing value clusters; current surfaced winner: "spacex".'
)
_MADRID = (
    'Contested fact: which value of "capital of" for "madrid" is correct? '
    '2 competing value clusters; current surfaced winner: "spain".'
)


def test_the_measured_failure_is_refused():
    """R3, labeled spurious: "which value of 'located in' for TEXAS" matched
    a SpaceX Starship recovery story with no Texas token anywhere in it. The
    matcher was edging off the contested VALUE."""
    key = g.contention_key(_TEXAS)
    assert not g.subject_anchored(
        key.subject,
        signal_text=g.anchor_text({
            "title": "SpaceX Starship floats belly up 6 days after historic "
                     "soft splashdown",
            "summary": "Recovery crews are still working to secure the "
                       "vehicle in the Indian Ocean.",
        }),
        signal_names={"spacex", "starship"},
    )


def test_four_madrid_rows_that_never_mention_madrid_are_refused():
    key = g.contention_key(_MADRID)
    for title in (
        "Almost 50,000 migrants crossed Morocco-Ceuta border over the weekend",
        "At Least 18 Die after Breach of Border between Morocco and Ceuta",
        "Hundreds of migrants who reached Spain's Ceuta ask for permits",
        "Over 40,000 migrants from Morocco recently entered Spanish enclave",
    ):
        assert not g.subject_anchored(
            key.subject,
            signal_text=g.anchor_text({"title": title}),
            signal_names={"morocco", "ceuta", "spain"},
        ), title


def test_a_literal_subject_anchors():
    key = g.contention_key(_MADRID)
    assert g.subject_anchored(
        key.subject,
        signal_text=g.anchor_text({
            "title": "Mass influx of migrants in Ceuta reignites tensions "
                     "between Madrid and Rabat"
        }),
    )


def test_a_canon_alias_anchors_where_the_literal_surface_never_appears():
    """7 of R3's correct matches name the subject ONLY through an alias — a
    literal-only anchor would have discarded all of them alongside the
    spurious rows, which is the difference between a guard and a blunt cut."""
    key = g.contention_key(
        'Contested fact: which value of "conflict with" for "saudi arabia" '
        'is correct? 3 competing value clusters; current surfaced winner: '
        '"yemen".'
    )
    assert g.subject_anchored(
        key.subject,
        signal_text=g.anchor_text({
            "title": "Saudis said to gear up for ground invasion against "
                     "Iran-backed Houthis in Yemen"
        }),
        signal_names={"saudi arabia", "yemen"},
    )


def test_initials_bridge_what_the_entity_plane_does_not_merge():
    """The election keeps "idf" and "israel defense forces" as SEPARATE
    keepers, so the canon cannot get from one to the other. Two R3 correct
    matches ride on exactly that gap; three letters, word-bounded, closes
    it."""
    key = g.contention_key(
        'Contested fact: which value of "operates in" for "israel defense '
        'forces" is correct? 2 competing value clusters.'
    )
    assert "idf" in g.subject_surfaces(key.subject)
    assert g.subject_anchored(
        key.subject,
        signal_text=g.anchor_text({"title": "Illegal blockades deepen"}),
        signal_names={"idf", "gaza", "israel"},
    )


def test_a_two_letter_acronym_is_not_an_identity_claim():
    """"the indian ocean" must not anchor on "io" — two letters collide with
    far too much ordinary text."""
    assert "io" not in g.subject_surfaces("the indian ocean")
    assert "tio" not in g.subject_surfaces("the indian ocean")


def test_the_anchor_is_word_bounded():
    """A substring anchor is not an anchor: "tal" must not match "total"."""
    assert not g.subject_anchored(
        "tal", signal_text="total exports fell across the metals sector"
    )
    assert g.subject_anchored("tal", signal_text="clashes near tal afar")


def test_a_possessive_subject_key_still_matches_the_article_form():
    """The arbiter's subject_key carries the tokenizer's spaced possessive
    ("the prosecutor general 's office"); the article writes it joined."""
    assert g.subject_anchored(
        "the prosecutor general 's office",
        signal_text="the prosecutor general's office confirmed the charge",
    )


def test_a_subject_with_no_anchorable_surface_leaves_the_pair_alone():
    """An inert guard is correct here. Refusing every pair for a subject the
    canon cannot represent would be the guard failing, not the pair."""
    assert g.subject_anchored("today", signal_text="nothing relevant here")
    assert g.subject_anchored("", signal_text="")


def test_anchor_text_reads_more_than_the_prompt_digest():
    """Deliberately NOT the gate's 600-char single-body digest: a contested
    subject often appears once, deep in the body."""
    text = g.anchor_text({
        "title": "Headline", "summary": "Summary", "body": "Body mentions Sumy"
    })
    assert "headline" in text and "summary" in text and "sumy" in text
    assert len(g.anchor_text({"body": "x" * 99_999})) <= g.MAX_ANCHOR_TEXT_CHARS


# ---------------------------------------------------------------------------
# CW-4 — liveness, shipped OFF
# ---------------------------------------------------------------------------


async def test_liveness_is_disabled_by_default_and_says_why():
    """Replayed over the gold set the filter removed 7 correct matches for 8
    false ones against a 60% base false rate — a group COLLAPSES once the
    arbiter resolves the dispute, i.e. downstream of the evidence arriving.
    A guard that cannot be shown to help does not ship armed."""
    assert g.DEFAULT_CONTENTION_LIVENESS_DAYS == 0.0

    class _Conn:
        async def fetch(self, *a, **k):  # pragma: no cover — must not run
            raise AssertionError("a disabled filter must not query")

    ids = {"a", "b"}
    assert await g.live_contention_ids(
        _Conn(), ids, liveness_days=g.DEFAULT_CONTENTION_LIVENESS_DAYS
    ) == ids


async def test_an_unreadable_substrate_degrades_OPEN():
    """A filter that cannot read must not silently mute a whole question
    class — the bearing gate's posture, for the bearing gate's reason."""
    class _Broken:
        async def fetch(self, *a, **k):
            raise RuntimeError("relation does not exist")

    assert await g.live_contention_ids(
        _Broken(), {"a", "b"}, liveness_days=30
    ) == {"a", "b"}


# ---------------------------------------------------------------------------
# THE PRE-REGISTERED ACCEPTANCE GATE
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not WORKSHEET.exists(),
    reason="planning/ is gitignored — the K-4 labels are internal evidence",
)
def test_the_replay_meets_the_pre_registered_bar():
    """population-weighted precision >= 0.60 AND >= 60% correct_match
    retention, over the 120 labeled R3 rows, through the shipped guards.

    Offline (no --db): the anchor sees only the worksheet's title + truncated
    summary, so it reads STRICTER than production and the number is a floor
    under a floor. CW-2 is scored as a no-op throughout — a prompt change is
    not replayable, so the train takes no credit for it here.
    """
    # INHERIT the environment and extend it, rather than replacing it. A
    # hand-built `env={"PYTHONPATH": REPO/src, "PATH": ...}` looks tidy and is
    # a trap: it works on a host where the dependencies sit on the default
    # sys.path, and fails everywhere they arrive via PYTHONPATH. In the test
    # container they arrive via PYTHONPATH (/install/lib/python3.11/site-packages),
    # so the replaced env cost the child `pydantic` and this gate failed on
    # every run in the main checkout — invisibly, because the worksheet it
    # guards lives in gitignored planning/ and the test SKIPS in any worktree.
    #
    # Handing over this interpreter's resolved sys.path is the version that
    # cannot rot: it is by construction the same import surface the test
    # itself is running under.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "replay_k4_r3.py"),
            "--worksheet", str(WORKSHEET),
        ],
        capture_output=True, text=True, cwd=REPO,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "=> MET" in proc.stdout
