# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift guard (R6, 2026-08-04 review-residue train) — the prose counts in
``docs/*.md`` + ``README.md`` must agree with the GENERATED manifest.

WHY: ``docs/RELEASE_STATE.md`` exists precisely so nobody hand-types a count,
and it works — but only for the docs that actually quote it. Everything else
kept its hand-typed number, and the corpus drifted apart: at the time this
guard was written the tree simultaneously claimed **eight**, **nine** and
**seven** bounded reasoning units across ten different files, with the README's
own architecture diagram saying 8 while the generated manifest said 9. A reader
cannot tell which is true, and neither can a reviewer — the exact failure mode
the manifest was built to end, reappearing one layer up.

THE PINNED NUMBER IS NOT WRITTEN DOWN HERE. It is parsed out of the generated
``docs/RELEASE_STATE.md`` headings, which the generator derives from a live
SELECT. So registering a tenth unit or a 33rd desk does not require editing
this test — regenerate the manifest and this guard starts demanding "ten"
everywhere on its own. A hardcoded expected count here would just be an
eleventh place for the number to drift.

WHAT IS AND ISN'T A VIOLATION. The corpus legitimately talks about SUBSETS
("the seven broad units", "seven of the nine bounded units", "ONE bounded
unit") and about ORDINALS ("an eighth, narrower unit", "the ninth,
`disruption_status`"). Those are not drift and must not be flagged — a guard
that cries wolf on correct prose gets suppressed and then guards nothing. The
matcher therefore only fires on a phrase that reads as a TOTAL, and
:data:`_SUBSET_MARKERS` documents each exemption. Ordinals never match at all
(``eighth`` is not ``eight``).

Pure — no DB, no network. Reads files from the checkout.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
RELEASE_STATE = DOCS / "RELEASE_STATE.md"

#: Every doc the guard polices: the whole of docs/ plus the public README.
#: RELEASE_STATE.md itself is included — it is generated, so it can only ever
#: agree with itself, and including it means a hand-edit of the generated file
#: (which the file's own header forbids) is caught here too.
#:
#: CHANGELOG.md is deliberately NOT policed. It is a historical record: an
#: entry that said "eight bounded units" when eight was the truth is CORRECT
#: and must not be rewritten to match today's count. Policing it would turn an
#: accurate history into a drift report and pressure someone into falsifying
#: the log to get the suite green.
def _policed_files() -> list[Path]:
    return sorted(DOCS.glob("*.md")) + [REPO_ROOT / "README.md"]


_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
}

_COUNT_TOKEN = "|".join(_NUMBER_WORDS) + r"|\d+"

#: A filler token between the count and the noun. Deliberately admits markdown
#: decoration (``**``, backticks) and hyphens, because the corpus writes
#: "**nine bounded reasoning units**", "the nine bounded-unit descriptors" and
#: "nine bounded `inline_target` UNITS" — a matcher that only understood plain
#: words would silently pass exactly the bolded headline claims that matter
#: most.
_FILLER = r"(?:[ -](?:[A-Za-z_`*'’-]+))"

#: A phrase asserting a TOTAL number of bounded reasoning units. The
#: lookbehind rejects markdown heading SLUGS — a cross-reference like
#: ``(#12-a-bounded-reasoning-unit--the-verify-pass)`` is a link target whose
#: leading number is a section index, not a count of anything.
_UNITS_RE = re.compile(
    rf"(?<![#\-])\b({_COUNT_TOKEN})\b"
    rf"({_FILLER}{{0,3}}?)"
    r"[ -](?:bounded|reasoning)"
    rf"({_FILLER}{{0,2}}?)"
    r"[ -]\**units?\b",
    re.IGNORECASE,
)

#: "32 country desks" — the country-desk TOTAL. The word ``country`` is
#: REQUIRED, not optional: the corpus also counts thematic desks ("10 desks"),
#: nuclear-relevant desks ("~8 desks") and per-tier subsets, and a matcher that
#: accepted a bare "N desks" would flag every one of those correct sentences.
#: Narrow and sound beats broad and noisy — a guard nobody trusts is a guard
#: nobody keeps.
_COUNTRY_DESKS_RE = re.compile(
    rf"(?<![#\-])\b({_COUNT_TOKEN})\b[ -]\**country\**[ -]\**desks?\b",
    re.IGNORECASE,
)

#: "19 G20 desks / members / targets". The trailing noun is required so the
#: slash form "32 g20/watch desks" (a COMBINED roster, not a G20 count) does
#: not get read as a claim about how many G20 members there are.
_G20_RE = re.compile(
    rf"(?<![#\-])\b({_COUNT_TOKEN})\b\s+\**G20\**\s+(?:country\s+)?"
    r"\**(?:desks?|members?|targets?)\b",
    re.IGNORECASE,
)

#: "13-country high-consequence **watch** tier" and its many decorations.
_WATCH_TIER_RE = re.compile(
    rf"(?<![#\-])\b({_COUNT_TOKEN})\b[- ](?:country|desk|target)?[- ]?"
    r"(?:high-consequence )?\**watch\**[- ]tier\b",
    re.IGNORECASE,
)

#: The bare form — "the eight units", "the nine units" — which carries no
#: "bounded"/"reasoning" keyword but means the same thing. Anchored on a
#: leading "the" so it cannot swallow unrelated counts ("32 desks, 8 units of
#: work" is not this shape).
_BARE_UNITS_RE = re.compile(
    rf"\bthe\s+({_COUNT_TOKEN})\s+\**units?\b",
    re.IGNORECASE,
)

#: Substrings that make a matched span a SUBSET or COMPARATIVE claim rather
#: than a total. Each one is a real, correct phrasing in the tree:
#:
#:   * ``broad``   — "the seven broad units": the seven blanket-predicate
#:                   units, as opposed to the tag-scoped narrow ones.
#:   * ``of the``  — "seven of the nine bounded units": an N-of-M claim whose
#:                   TOTAL is the M, which this same matcher checks separately
#:                   when the M-bearing phrase stands on its own.
#:   * ``geopolitics`` — "the eight geopolitics units": the country-desk
#:                   family, excluding the domain-scoped `disruption_status`.
_SUBSET_MARKERS: tuple[str, ...] = ("broad", "of the", "geopolitics")


def _pinned_unit_count() -> int:
    """The generated manifest's bounded-unit count — the single source of
    truth every other doc must match."""
    text = RELEASE_STATE.read_text(encoding="utf-8")
    m = re.search(r"^## Bounded reasoning units \((\d+)\)$", text, re.MULTILINE)
    assert m is not None, (
        f"could not find the generated '## Bounded reasoning units (N)' "
        f"heading in {RELEASE_STATE} — either the manifest was hand-edited or "
        f"scripts/generate_release_manifest.py changed its heading shape; this "
        f"guard has nothing to pin against until that is fixed"
    )
    return int(m.group(1))


def _pinned_desk_counts() -> tuple[int, int, int]:
    """``(total, g20, watch)`` country desks, parsed from the generated
    manifest's Country desks section."""
    text = RELEASE_STATE.read_text(encoding="utf-8")
    m = re.search(
        r"^- \*\*Total: (\d+)\*\* — (\d+) G20 \+ (\d+) watch tier$",
        text,
        re.MULTILINE,
    )
    assert m is not None, (
        f"could not find the generated country-desk total line in "
        f"{RELEASE_STATE}; see _pinned_unit_count for why that is fatal"
    )
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _violations(pattern: re.Pattern[str], expected: int) -> list[str]:
    out: list[str] = []
    for path in _policed_files():
        if not path.exists():
            continue
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            for m in pattern.finditer(line):
                span = m.group(0)
                low = span.lower()
                if any(marker in low for marker in _SUBSET_MARKERS):
                    continue
                token = m.group(1).lower()
                value = _NUMBER_WORDS.get(token)
                if value is None:
                    value = int(token)
                # "ONE bounded unit" is a singular reference to an arbitrary
                # unit ("what ONE bounded unit sees"), never a total.
                if value == 1:
                    continue
                if value != expected:
                    out.append(f"{rel}:{lineno}: {span!r} (expected {expected})")
    return out


def test_release_state_manifest_is_parseable() -> None:
    """Sanity: the generated headings this guard pins against actually parse,
    and to plausible numbers — so a manifest shape change fails loud here
    rather than silently disarming every assertion below."""
    units = _pinned_unit_count()
    total, g20, watch = _pinned_desk_counts()
    assert units >= 1, "manifest reports zero bounded units"
    assert total == g20 + watch, (
        f"generated desk total {total} != {g20} G20 + {watch} watch — the "
        f"manifest disagrees with itself"
    )


def test_bounded_unit_counts_agree_across_docs() -> None:
    """Every doc that states a TOTAL number of bounded reasoning units must
    state the number the generated manifest reports.

    On failure: either the prose is stale (fix the prose), or a unit was
    registered/retired and `docs/RELEASE_STATE.md` was not regenerated (run
    `PYTHONPATH=src python3 scripts/generate_release_manifest.py`). Subset
    phrasings ("the seven broad units", "seven of the nine") are exempt by
    design — see `_SUBSET_MARKERS`.
    """
    expected = _pinned_unit_count()
    bad = _violations(_UNITS_RE, expected) + _violations(_BARE_UNITS_RE, expected)
    assert not bad, (
        f"docs disagree with docs/RELEASE_STATE.md on the bounded-unit count "
        f"({expected}):\n  " + "\n  ".join(bad)
    )


def test_country_desk_counts_agree_across_docs() -> None:
    """The country-desk roster (total / G20 / watch tier) must read the same
    in every doc as in the generated manifest.

    This is the same drift the unit count had, one noun over: the watch tier
    grew from 6 desks to 13 and the prose that said "25 country desks (19 G20 +
    the 6-desk watch tier)" simply stayed behind in the docs nobody re-read.
    """
    total, g20, watch = _pinned_desk_counts()
    bad = (
        _violations(_COUNTRY_DESKS_RE, total)
        + _violations(_G20_RE, g20)
        + _violations(_WATCH_TIER_RE, watch)
    )
    assert not bad, (
        f"docs disagree with docs/RELEASE_STATE.md on the country-desk roster "
        f"({total} total = {g20} G20 + {watch} watch):\n  " + "\n  ".join(bad)
    )


def test_generator_pins_the_judge_env_var_spelling() -> None:
    """R6: the release manifest reports the LIVE effective judge route, which
    it can only do if its copy of the override env-var name matches the
    runtime's. The generator duplicates the constant rather than importing the
    deps builder (which would drag the whole runtime into a thin asyncpg
    script), so this pins the copy to the original — a rename on one side
    without the other would silently return the manifest to reporting
    descriptor defaults as if they were the effective route, which is the
    exact bug R6 fixed.
    """
    import sys

    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import generate_release_manifest as grm

    from legba.runtime import analyst_deps_builder as adb

    assert grm.JUDGE_STACK_REF_ENV == adb.JUDGE_STACK_REF_ENV, (
        f"scripts/generate_release_manifest.py copies the judge-override env "
        f"var as {grm.JUDGE_STACK_REF_ENV!r} but the runtime resolves "
        f"{adb.JUDGE_STACK_REF_ENV!r}"
    )
