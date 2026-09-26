# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 — the stage-1 reference instruction, re-anchored, in ONE place.

``planning/PROOF_ROUND_2026-09-12/stage1_IL.md`` is the instruction an Opus web
lane was given to build ``ref_IL_A.json`` — 33 developments, 8 banded
dimensions, the shape the correctness grader reads. R1 re-anchored it for the
core plane and measured it twice. This module is that text as a function of
(country, window, dimensions), so the SCHEDULED build and the OPERATOR-TRIGGERED
top-up packet cannot drift apart: a top-up that asked for a different shape than
the skeleton would produce a merge of two artefacts, not one reference.

WHAT CHANGED FROM stage1_IL.md, and why each change is here rather than in a
prompt-tweaking lane:

  * **The protocol section is binding and the harness enforces it.** R1 run 1
    ignored a politely-worded protocol and produced a 7.7% span-verification
    rate. The NOTE turn, the manifest and the eviction warning are stated as
    mechanical facts about what will happen, because they are.
  * **The date rule is stated as a REJECTION rule, not a preference.** "Every
    development must have happened inside the window" became "a page with no
    machine-readable publish date cannot carry a development, whatever the
    quote says, and a masthead is not a publish date" — the exact sentence that
    would have stopped R1 run 2 shipping a November-2024 appointment.
  * **The source bounds are stated as a FILTER the model will feel.** Discovery
    is allowlisted, so the instruction says so rather than letting the model
    conclude the web is empty.
  * **`gaps` is defined as a CHECKED SILENCE.** R1's C run declared five gaps
    all of the form "no further X was identified" — an absence of findings
    restated. A gap is what you searched for and could not date; the
    instruction now says what a gap is NOT.

The ≤5-developments cap in the original stage-1 packet is deliberately NOT
carried: that packet fed a 5-item-per-country round, and ``ref_IL_A.json``
itself carries 33. The target here is 20–35 with at least two per dimension.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ._reference_manifest import (
    RATIO_GUARD_FACTOR,
    SEARCH_RUN_LIMIT,
    query_guidance,
)

#: The eight dimensions a country reference bands: ``scorecard_banding``'s seven
#: plus ``proliferation_watch`` (present on the watch countries). Passed in
#: rather than imported here so a target with a different desk set gets a
#: reference over ITS dimensions and not over a constant.
BAND_LADDER_BLOCK = """```python
#: The band ladder, ascending. Demotion walks one step DOWN, clamped at "low".
BAND_LADDER: tuple[str, ...] = ('low', 'watch', 'elevated', 'high', 'critical')

#: The five valid ``severity:<level>`` levels mapped to their base band.
SEVERITY_TO_BAND: dict[str, str] = {'low': 'low', 'moderate': 'watch', 'elevated': 'elevated', 'high': 'high', 'critical': 'critical'}

#: Sentinel band for a dimension with no qualifying verified claim.
INSUFFICIENT: str = 'insufficient-evidence'
```"""

NOTE_FORM = (
    "NOTE: | <summary, at most 2 sentences> | SPAN: <verbatim sentence copied "
    "character-for-character out of the page text you just read> | OUTLET: "
    "<name> | DATE: YYYY-MM-DD | URL: <the url> | TIER: 1|2|3 | DIMS: "
    "<comma-separated dimension names> | SIG: major|notable"
)


def _numbered(dimensions: Sequence[str]) -> str:
    return "\n".join(f"{i}. `{d}`" for i, d in enumerate(dimensions, 1))


def _band_fields(dimensions: Sequence[str]) -> str:
    return ", ".join(f'"{d}": "<band|insufficient-basis>"' for d in dimensions)


def build_instruction(
    *,
    country_name: str,
    country_code: str,
    t0: str,
    window_start: str,
    dimensions: Sequence[str],
    item_prefix: str,
    builder_label: str,
    model_name: str,
    tool_call_cap: int,
    min_developments: int = 20,
    max_developments: int = 35,
) -> str:
    """The system instruction for one country, one window.

    Written in the second person to a knowledgeable analyst, because that is
    what the measured version was and changing the register would change the
    instrument. Everything mechanical is stated as mechanical.
    """
    return f"""You are a knowledgeable country analyst asked for an INDEPENDENT reference read of {country_name} ({country_code}) for the 14 days ending T0 = {t0}.

Window under judgment: {window_start} -> {t0}.

You have the open web through two tools and nothing else. You have NOT seen any other analyst's read of this country and you never will; your reference will be compared against another analyst's product, so write it as the standard you would hold anyone to. Do not speculate about, search for, or attempt to reconstruct any automated system's published read of {country_name} — such artifacts are FORBIDDEN as references (circularity).

## The {len(dimensions)} dimensions you must band

{_numbered(dimensions)}

## The band ladder (verbatim from the product)

{BAND_LADDER_BLOCK}

Five ordinal bands, `low < watch < elevated < high < critical`, plus the honest `insufficient-basis` state when you genuinely cannot band a dimension from the open record. `insufficient-basis` is an honest state, never a default.

## Time anchoring (binding)

* T0 = {t0}. The world-state under judgment is the world AS OF T0.
* Sources PUBLISHED after T0 may establish what was true ON or BEFORE T0; events occurring AFTER T0 never count, in any dimension.
* An event dated BEFORE {window_start} is out of window: it may be context, it is not a development.
* Band as a knowledgeable contemporary AT T0 would band — hindsight is not evidence. If you needed a post-T0 event to decide an item, set `"hindsight": true`.

## THE DATE GATE (this is a rejection rule, not a preference)

A development is accepted ONLY if the page it cites declares a publish date in its OWN MACHINE-READABLE METADATA — JSON-LD `datePublished`, `article:published_time`, or `<time datetime=...>` — and that date falls inside the window. The `fetch_page` tool tells you the date it found and warns you when it found none.

**A page with no machine-readable publish date cannot carry a development, however good the quote.** A date printed in the page's visible text, a "published 3 hours ago" line, and above all the site's MASTHEAD DATE are NOT publish dates. A reference that dates a two-year-old article off today's masthead marks a correct read as wrong, which is worse than having no reference at all. If a page you want is undated, find the same matter at a page that declares its date.

## Source quality bounds (binding, and enforced on discovery)

* **Tier 1 — primary/official**: government and IGO statements and datasets (IMF/World Bank/EIA/UN/IAEA), central banks, courts, election commissions, UCDP/ACLED event data, company filings.
* **Tier 2 — record press**: major wires (Reuters, AP, AFP), newspapers of record (FT, NYT, WSJ, Economist, BBC, DW, Le Monde, Nikkei), and domain reference monitors (ICG CrisisWatch, ISW, IISS).
* **Tier 3 — everything else**: regional outlets, national/partisan press, think-tank blogs, aggregators.

**Your searches are FILTERED to Tier 1–2 by default.** Results from other hosts are dropped before you see them and the tool says so. This is not the web being empty; it is the reference bar. If a dimension genuinely has no Tier 1–2 record, you will be TOLD that the dimension has been opened to wider sources — and only then. State-controlled media of a party to a matter is never decisive AGAINST a claim about that matter. Social media posts are never decisive. Wikipedia is NEVER a source — use it only to learn that a story exists, then cite the underlying outlet.

## HOW TO SEARCH (the harness rewrites a query that breaks these rules, and the rewrite costs you the round)

{query_guidance(country_name, dimensions)}

**Never a `site:` operator, never a date string in a query, never the name of an outlet.** Discovery is ALREADY filtered to reference-grade hosts, so a `site:` operator can only narrow what you are shown; a news page does not carry its own ISO date as searchable text; and the window is enforced on each page's own metadata AFTER you fetch it. The harness strips all three before the search runs and tells you it did.

**After {SEARCH_RUN_LIMIT} searches without a fetch, your next call must be a `fetch_page`** on one of the URLs you have already been shown — or a plain-text NOTE saying which dimension you are still missing and why none of the results is worth fetching. A build that searches more than {RATIO_GUARD_FACTOR:g} times per fetch ends with nothing to quote, and the harness says so when it sees one.

**Copy a URL EXACTLY as the `url` field gave it to you, including any `www.`.** A retyped URL 404s: the last live build lost its only in-window page twice by dropping four characters. The harness repairs a URL it recognises and tells you it did; it cannot repair one it has never shown you.

## WORKING PROTOCOL (the harness enforces this — read it twice)

1. Call `web_search` to find candidate reporting, then `fetch_page` the promising URLs. Search in English; a `time_range` of `month` usually suits this window.
2. **IMMEDIATELY AFTER EVERY `fetch_page`, your next message MUST be plain text with NO tool call, beginning with `NOTE:`.** In it record every development that page establishes, one per line, in exactly this form:

   `{NOTE_FORM}`

   Copy the SPAN EXACTLY as it appears in the `text` field `fetch_page` just returned — every character, every comma, every quote mark, every accent. Do NOT retype it from memory, do NOT shorten it, do NOT tidy it, do NOT translate it.
   The SPAN must come from the fetched PAGE TEXT. A sentence from a `web_search` snippet is NOT a span and will fail verification.
   The URL must be copied character-for-character from the `url` field `fetch_page` returned.
   A span that is not an exact substring of the archived page is UNVERIFIED and the development is DROPPED from your reference.
   If the page establishes nothing usable and in-window, write `NOTE: nothing usable` and move on.
3. **Page text is EVICTED from this conversation a few turns after you read it.** The harness keeps its OWN record of every page you read — url, outlet, the page's machine-readable date, and the opening text of the page — and hands you that record at commit time, so a page you read is never lost. Your NOTE is what you add to it: the sentence you judged decisive and the dimension it bears on.
4. Work dimension by dimension. Target **{min_developments}-{max_developments} developments**, with **at least 2 per dimension** wherever the record supports it. FOLLOW THE RECORD, do not fill a quota: a fortnight in which one dimension carried six major events and another carried none should produce a reference shaped like that, not a flat three per dimension.
5. If a host refuses you (401/403/paywall stub), do NOT try that host again — go to another outlet carrying the same matter. The harness drops a host from your results after it has refused you twice and will not fetch it again.
6. You have a budget of about {tool_call_cap} tool calls. Spend them.
7. When you have gathered enough, send a plain-text message that is exactly `REFERENCE COMPLETE`. The harness will then ask you for the final JSON and hand you every page you actually read, with its date, its window verdict and the text you may quote from. Do NOT emit the JSON before it asks — but if you do, the harness reads it as REFERENCE COMPLETE and asks anyway.
8. **An empty reference is a failure, not an honest answer.** If the harness hands you an in-window page carrying a decisive sentence and you commit no development from it, the run is recorded as `empty_commit_with_material` against this country. `insufficient-basis` is the honest state for a DIMENSION nothing in the record touched; it is never the honest state for a page you were shown.

## Output contract (the FINAL message only — one JSON object, no prose, no markdown fence)

```json
{{
  "header": {{"country": "{country_code}", "t0": "{t0}",
             "window": "{window_start} -> {t0}",
             "builder": "{builder_label}", "model": "{model_name}"}},
  "ref_bands": {{{_band_fields(dimensions)}}},
  "ref_developments": [
    {{"item_id": "{item_prefix}-1",
     "summary": "<at most 2 sentences>",
     "decisive_span": "<VERBATIM from the page you fetched>",
     "outlet": "<outlet name>",
     "publish_date": "YYYY-MM-DD",
     "source_url": "<the url fetch_page returned>",
     "source_tier": 1,
     "significance": "major|notable",
     "hindsight": false,
     "dimension": ["escalation"]}}
  ],
  "gaps": ["<what you SEARCHED FOR inside the window and could NOT date — a checked silence, naming the queries and the outlets that refused you>"],
  "ref_direction": {{"direction": "rising|stable|easing", "why": "<one line>"}},
  "notes": {{"band_reasoning": {{"<dimension>": "<what must be true in the world for this rung, and which in-window items satisfy it>"}}}}
}}
```

* `item_id` runs `{item_prefix}-1`, `{item_prefix}-2`, … in order.
* `significance`: `major` (a knowledgeable reader would call a read defective for missing it) or `notable`.
* `dimension` is a LIST; a development may bear on more than one.
* `gaps` is NOT optional and is NOT "no further X was identified". A gap names what you looked for, how you looked, and why you could not date it — a host that refused you, a matter nobody reported inside the window, a claim you could only find undated. An absence of findings restated is not a gap.
* Emit the JSON object and NOTHING else in that final message."""


def build_first_user(
    *, country_name: str, country_code: str, t0: str, window_start: str
) -> str:
    return (
        f"Begin. Build the reference for {country_name} ({country_code}) for "
        f"the window {window_start} -> {t0}. Start with a broad search, then "
        "work dimension by dimension. Remember: every fetch_page is followed "
        "by a NOTE turn."
    )


# ---------------------------------------------------------------------------
# The operator-triggered TOP-UP packet
# ---------------------------------------------------------------------------

TOPUP_HEADER = """# REFERENCE TOP-UP — {country_name} ({country_code})
# Window: {window_start} -> {t0} · reference {reference_id}
# Built by: {builder} · {n_established} established developments · thin: {thin}

THIS IS NOT A FRESH REFERENCE. A free core-plane lane has already built a
span-anchored skeleton for this country and window under hard fences (every
cited URL fetched, every span an exact substring of the archived page, every
development dated from the page's own machine-readable metadata inside the
window). It verified; it UNDER-COVERS. R1 measured the shape of the gap
precisely: the core lane carried 33% of a knowledgeable reader's major
developments, and its per-dimension counts came out FLAT (3-6 everywhere) where
the record was lumpy (13 escalation items, 12 internal stability, 2
proliferation). Flatness is the tell: it filled a quota instead of following
the record.

Your job is the gap and only the gap.
"""

TOPUP_RULES = """
## What you are asked for

**Find what is MISSING.** The established developments are listed below. Do not
re-report them. Do not restate them in different words. For each dimension, ask
the question the core lane could not: *what would a knowledgeable reader call
this read defective for missing?* — and then go and date it.

Weight your effort by what the record actually carried, not by the dimension
list. If one dimension carried the fortnight's decisive events, most of your
additions belong there.

## The rules your additions must satisfy (identical to the skeleton's)

* **The date gate.** Every addition cites a page whose OWN machine-readable
  metadata (JSON-LD `datePublished`, `article:published_time`, `<time
  datetime>`) declares a publish date INSIDE the window. A masthead date is not
  a publish date. An undated page cannot carry a development.
* **The span.** `decisive_span` is copied character-for-character out of the
  page you fetched — not out of a search snippet, not from memory.
* **The URL.** `source_url` is a page you actually fetched and read.
* **The tier.** Prefer Tier 1 (official / IGO / central bank / court) and
  Tier 2 (wires, newspapers of record, ICG / ISW / IISS). Record the tier you
  actually used.
* **The dimensions listed below are the ones this reference bands.** An addition
  outside them has nowhere to go.

## Output

ONE JSON object, same shape as the skeleton, containing ONLY YOUR ADDITIONS:

```json
{
  "header": {"country": "<CC>", "t0": "<T0>", "window": "<start> -> <T0>",
             "builder": "opus-topup-lane", "model": "<exact model id>"},
  "ref_developments": [ { ...same fields as below... } ],
  "gaps": ["<what you searched for and could NOT date>"],
  "ref_bands": {"<dimension>": "<band>"}
}
```

`ref_bands` is optional and is your view AFTER your additions — where you give
one it replaces the skeleton's band for that dimension, and the merge records
that it did. Omit a dimension to leave the skeleton's band standing.

Merge it with:

    PYTHONPATH=src python3 scripts/load_unit_reference.py <your-additions.json> \\
        --merge-into {reference_id}

The merge re-verifies every span against the archive, re-runs the date gate,
unions by matter, recomputes `thin_dimensions`, and bumps the builder to
`core-plane-lane+opus-topup`. An addition that fails a fence is DROPPED by the
merge and counted — the same fences, applied to your work and to the lane's.
"""


def build_topup_packet(
    *,
    country_name: str,
    country_code: str,
    t0: str,
    window_start: str,
    reference_id: str,
    builder: str,
    dimensions: Sequence[str],
    established: Sequence[Mapping[str, object]],
    thin: Sequence[str],
    bands: Mapping[str, object],
    gaps: Sequence[str],
    counts: Mapping[str, int],
) -> str:
    """The stage-1 packet re-anchored as a DIFFERENCE task, for an operator lane.

    This is a FILE, not a call. Nothing in the platform runs it and nothing in
    the platform pays for it; the operator hands it to whatever lane they
    choose, and ``load_unit_reference.py --merge-into`` takes the result back.
    That boundary is the whole point of the hybrid design — the free lane is
    scheduled, the paid one never is.
    """
    lines = [
        TOPUP_HEADER.format(
            country_name=country_name, country_code=country_code,
            window_start=window_start, t0=t0, reference_id=reference_id,
            builder=builder, n_established=len(established),
            thin=", ".join(thin) or "none",
        ),
        TOPUP_RULES.replace("{reference_id}", reference_id),
        "\n## The dimensions this reference bands, with what it holds today\n",
        "| dimension | band | verified developments | thin? |",
        "|---|---|---|---|",
    ]
    for dim in dimensions:
        count = int(counts.get(dim, 0))
        lines.append(
            f"| `{dim}` | {bands.get(dim, '—')} | {count} | "
            f"{'**THIN**' if dim in set(thin) else ''} |"
        )
    lines.append(
        "\nA dimension marked THIN carries fewer than two verified "
        "developments. The correctness grader reads `thin_dimensions` and "
        "treats those dimensions' numbers accordingly, so closing one is worth "
        "more than adding a fourth item to a dimension that already has three."
    )

    lines.append("\n## ALREADY ESTABLISHED — do not repeat these\n")
    if not established:
        lines.append("_(none — the skeleton produced nothing; build freely)_")
    for dev in established:
        dims = dev.get("dimension")
        dims_text = ", ".join(dims) if isinstance(dims, (list, tuple)) else str(dims or "")
        lines.append(
            f"* **{dev.get('item_id')}** [{dims_text}] "
            f"({dev.get('publish_date')}, {dev.get('outlet')}, tier "
            f"{dev.get('source_tier')}"
            + (", TIER-3 ADMITTED" if dev.get("tier3_admitted") else "")
            + f") — {dev.get('summary')}\n"
            f"  <{dev.get('source_url')}>"
        )

    lines.append("\n## The skeleton's declared gaps (verify and close, do not repeat)\n")
    if not gaps:
        lines.append("_(the skeleton declared no gaps)_")
    for gap in gaps:
        lines.append(f"* {gap}")

    lines.append(
        "\n---\n\nCOST NOTE, stated so it is never a surprise: this packet is "
        "an operator-triggered lane. Nothing schedules it, nothing in the "
        "platform calls it, and the scheduled reference builder runs to "
        "completion without it. R1 sized a full out-of-plane reference at "
        "roughly 230k metered tokens; a top-up over an established skeleton is "
        "less, because discovery has already happened."
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "BAND_LADDER_BLOCK",
    "NOTE_FORM",
    "build_first_user",
    "build_instruction",
    "build_topup_packet",
]
