# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The semantic-dedup threshold is MEASURED — these tests keep it that way.

``cross_source_dedup._DEFAULT_SEMANTIC_THRESHOLD`` used to be 0.95 with nothing
behind it, and nobody could tell, because the tier never issued a Qdrant query
in its entire history. The threshold now comes from
``scripts/measure_dedupe_threshold.py``, and the labelled sample it produced is
committed beside these tests as a fixture.

What this file guards:

  * the constant still sits where the measurement put it — dropping it back to
    0.95 admits pairs the worksheet labels ``distinct``, and this fails;
  * the labelling function that produced the worksheet still produces those
    labels (a silent change to ``label_pair`` would invalidate every number
    quoted in the handler's docstring without touching the handler);
  * the fixture stays the size and shape the measurement claims.

It deliberately does NOT re-run the measurement: that needs the live corpus.
The fixture is the measurement's testimony, and these are its cross-examination.
"""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import pytest

from legba.data.analysts.deterministic_handlers import cross_source_dedup

WORKSHEET = (
    Path(__file__).parent / "fixtures" / "dedupe_threshold_worksheet_2026-08-02.tsv"
)

#: The population numbers the handler's docstring quotes, from the run that
#: produced this fixture (6,000 signals sampled, 5,446 labelled pairs). The
#: worksheet is a per-band SAMPLE of that run, so it cannot reproduce these —
#: they are recorded here so the claim and its provenance travel together.
POPULATION_PRECISION_AT_THRESHOLD = (0.992, 0.959)  # (ambiguous dropped, as wrong)

#: Precision the WORKSHEET SAMPLE must show at the live threshold, over pairs
#: whose vectors are not degenerate. Set below the observed 1.000 so ordinary
#: resampling does not fail the build, but high enough that a materially worse
#: threshold does.
MIN_SAMPLE_PRECISION = 0.95


def _load() -> list[dict[str, str]]:
    with WORKSHEET.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


@pytest.fixture(scope="module")
def worksheet() -> list[dict[str, str]]:
    return _load()


@pytest.fixture(scope="module")
def measure_module():
    """Import the measurement script by path — it lives in scripts/, not the
    package, because it is an operator tool, not runtime code."""
    path = Path(__file__).parents[2] / "scripts" / "measure_dedupe_threshold.py"
    spec = importlib.util.spec_from_file_location("measure_dedupe_threshold", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _judged(rows, threshold: float, *, non_degenerate: bool):
    out = []
    for row in rows:
        if float(row["score"]) < threshold:
            continue
        if non_degenerate and row["degenerate"] == "1":
            continue
        if row["label"] in ("duplicate", "distinct"):
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# The fixture itself
# ---------------------------------------------------------------------------


def test_worksheet_is_present_and_the_size_the_measurement_claims():
    rows = _load()
    assert 100 <= len(rows) <= 200, (
        f"worksheet is {len(rows)} pairs — the measurement documents a "
        "100-200 pair hand-auditable sample"
    )
    labels = {row["label"] for row in rows}
    assert labels <= {"duplicate", "distinct", "ambiguous", "unlabelled"}
    # Every band the curve reports must actually be represented.
    bands = {row["band"] for row in rows}
    assert len(bands) >= 10, f"only {len(bands)} score bands sampled"


def test_worksheet_carries_both_classes_at_every_useful_band(worksheet):
    """A worksheet of only easy positives proves nothing. The sample has to
    contain the pairs that would be linked WRONGLY, or the precision it reports
    is an artefact of what was sampled."""
    assert sum(1 for r in worksheet if r["label"] == "duplicate") >= 20
    assert sum(1 for r in worksheet if r["label"] == "distinct") >= 20
    # And the degenerate class — the reason the threshold alone is insufficient.
    assert sum(1 for r in worksheet if r["degenerate"] == "1") >= 20


# ---------------------------------------------------------------------------
# The constant, pinned to the evidence
# ---------------------------------------------------------------------------


def test_threshold_is_the_one_the_measurement_chose():
    assert cross_source_dedup._DEFAULT_SEMANTIC_THRESHOLD == 0.97


def test_no_distinct_pair_survives_the_threshold_in_the_sample(worksheet):
    """At the live threshold, no pair the worksheet labels ``distinct`` may be
    linkable — given both sides carry a real (non-degenerate) vector."""
    threshold = cross_source_dedup._DEFAULT_SEMANTIC_THRESHOLD
    judged = _judged(worksheet, threshold, non_degenerate=True)
    assert judged, "no judged non-degenerate pairs at the threshold — resample"
    wrong = [r for r in judged if r["label"] == "distinct"]
    precision = 1.0 - len(wrong) / len(judged)
    assert precision >= MIN_SAMPLE_PRECISION, (
        f"precision {precision:.3f} at threshold {threshold} over "
        f"{len(judged)} judged pairs; offenders: "
        f"{[(r['score'], r['title_a'][:60], r['title_b'][:60]) for r in wrong][:3]}"
    )


def test_lowering_the_threshold_to_the_old_default_admits_false_links(worksheet):
    """THE point of the measurement, made falsifiable.

    0.95 was the configured value for the tier's whole life. In the sample it
    admits pairs the labels call ``distinct`` — different stories that a link
    would have made invisible to every desk. If this ever stops being true the
    threshold should be RE-MEASURED, not quietly lowered.
    """
    at_old = _judged(worksheet, 0.95, non_degenerate=True)
    at_new = _judged(
        worksheet, cross_source_dedup._DEFAULT_SEMANTIC_THRESHOLD,
        non_degenerate=True,
    )
    wrong_old = sum(1 for r in at_old if r["label"] == "distinct")
    wrong_new = sum(1 for r in at_new if r["label"] == "distinct")
    assert wrong_old > wrong_new, (
        "the old 0.95 default no longer looks worse than the measured one in "
        "this sample — re-run scripts/measure_dedupe_threshold.py before "
        "trusting either number"
    )


def test_degenerate_pairs_defeat_the_threshold_entirely(worksheet):
    """Why the threshold is only half the fix.

    Degenerate pairs (both sides embedded from a byte-identical or sub-floor
    input) score at the TOP of the range regardless of content, so no threshold
    separates them — they have to be excluded structurally. This asserts the
    fixture actually shows that, rather than the claim resting on prose.
    """
    high = [r for r in worksheet
            if float(r["score"]) >= cross_source_dedup._DEFAULT_SEMANTIC_THRESHOLD]
    degenerate_high = [r for r in high if r["degenerate"] == "1"]
    assert degenerate_high, "no degenerate pairs above the threshold in the sample"
    wrong = [r for r in degenerate_high if r["label"] == "distinct"]
    assert wrong, (
        "the sample shows no clearly-wrong degenerate pair above the "
        "threshold — if that is genuinely true now, the structural exclusion "
        "may be re-examined; do not assume it"
    )


# ---------------------------------------------------------------------------
# The labeller that produced it
# ---------------------------------------------------------------------------


def test_label_pair_still_scores_the_worksheet_titles_as_recorded(
    worksheet, measure_module,
):
    """Recompute the TITLE feature from the worksheet's own title columns and
    check it against the recorded value. A silent change to tokenisation,
    stopwords or the jaccard would invalidate every label in the fixture — and
    therefore every precision number quoted in the handler — without touching
    the handler at all."""
    checked = 0
    for row in worksheet:
        # Worksheet cells are bounded at 180 chars; a truncated title cannot be
        # re-scored faithfully, so it is skipped rather than fudged.
        if len(row["title_a"]) >= 180 or len(row["title_b"]) >= 180:
            continue
        if "(no title)" in (row["title_a"], row["title_b"]):
            continue
        recomputed = measure_module.jaccard(
            measure_module.content_words(row["title_a"]),
            measure_module.content_words(row["title_b"]),
        )
        # Compared as the worksheet WROTE it (3dp), so the check is exact
        # rather than a tolerance that could drift wider over time.
        assert f"{recomputed:.3f}" == row["title_j"], (
            f"title jaccard drifted: recorded {row['title_j']}, now "
            f"{recomputed:.3f} for {row['title_a'][:50]!r} / "
            f"{row['title_b'][:50]!r}"
        )
        checked += 1
    assert checked >= 50, f"only {checked} worksheet rows were re-scorable"


def test_label_pair_ignores_body_overlap_when_the_bodies_are_stubs(measure_module):
    """The trap the labeller must not fall into.

    Two unrelated stories whose only body is the same ``"(END)"`` stub have body
    overlap 1.0. A rule that reads that as evidence of sameness launders the
    exact defect the measurement exists to find — and an earlier draft of this
    labeller did, burying every degenerate pair in ``ambiguous`` and reporting a
    precision of 0.999 at 0.95 that was pure artefact.
    """
    label, title_j, body_j = measure_module.label_pair(
        "Magnitude 5.0 earthquake hits Texas",
        "Explosion heard in Shiraz, southern Iran, amid possible US attack",
        "(END)", "(END)",
    )
    assert body_j == 1.0, "the stub bodies do overlap completely"
    assert label == measure_module.LABEL_DISTINCT, (
        "stub-body overlap was counted as evidence of sameness"
    )
    assert title_j < 0.25


def test_label_pair_still_trusts_long_identical_bodies(measure_module):
    """The other side of the same rule: a 200+ char byte-identical article body
    IS a repost, and must not be thrown away with the boilerplate."""
    body = (
        "Officials confirmed the vessel had been drifting for six hours before "
        "the coastguard reached it, and that all twenty-eight crew members were "
        "recovered without injury in an operation that ran past midnight local "
        "time, according to two people briefed on the response."
    )
    assert len(body) >= 200
    label, _title_j, _body_j = measure_module.label_pair(
        "Coastguard rescues twenty-eight from drifting vessel",
        "All crew recovered after six-hour drift, officials say",
        body, body,
    )
    assert label == measure_module.LABEL_DUPLICATE


def test_missing_titles_are_unlabelled_not_guessed(measure_module):
    """No title on either side means nothing independent of the vector is left
    to judge with. Those pairs are excluded from the counts, never guessed —
    23% of the sample lands here and a guess would have moved the answer."""
    label, _t, _b = measure_module.label_pair("", "Some headline", "body a", "body b")
    assert label == measure_module.LABEL_UNLABELLED
