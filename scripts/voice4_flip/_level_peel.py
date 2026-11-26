# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X4 LEVEL — the third later train to touch two of the flipped
prompts, peeled for the D6 pin exactly as TITLE-FRAME-FIX's line swap is.

``energy_security`` and ``military_posture`` gained level-shaped indicators: the
level entries in the response schema's ``indicators`` example, the LEVEL
INDICATORS rule, and the sentences that count and name them. Every edit is a
run of whole LINES, so each is rolled back by one exact substring swap
``(X4 text, pre-X4 text)``, applied blind by :func:`pre_level_indicators`
(``_flip_common.d6_base`` runs it first — the latest train peels first). Not
re-pinned, for the reason ``_flip_common.LATER_CONTRACT_PARAGRAPHS`` gives.
"""

from __future__ import annotations

#: unit -> ((X4 text, pre-X4 text), ...), in prompt order.
LATER_LEVEL_SWAPS: dict[str, tuple[tuple[str, str], ...]] = {'energy_security': (('   {"id": "supply-route-chokepoint-risk", "statement": "Escalated '
                      "interdiction risk on a maritime chokepoint carrying this country's energy "
                      'imports", "status": "not_observed", "horizon_date": "<as-of + 14 days>", '
                      '"first_seen": "<as-of date>", "citations": []},\n'
                      '   {"id": "energy-trade-restriction-in-force", "statement": "A sanctions, '
                      "embargo, price-cap or export-ban regime on this country's energy trade is "
                      'in force", "status": "not_observed", "shape": "level", "reference": "bar:a '
                      'signal in this window reports the measure still in force or being enforced, '
                      'not only that it was once imposed", "horizon_date": "<as-of + 7 days>", '
                      '"first_seen": "<as-of date>", "citations": []},\n'
                      '   {"id": "energy-route-disruption-in-force", "statement": "A maritime '
                      "chokepoint, pipeline or grid link carrying this country's energy is closed, "
                      'blockaded or under repeated attack", "status": "not_observed", "shape": '
                      '"level", "reference": "bar:a signal in this window reports the route still '
                      'closed, blockaded or under attack", "horizon_date": "<as-of + 7 days>", '
                      '"first_seen": "<as-of date>", "citations": []},\n'
                      '   {"id": "energy-supply-stress-in-force", "statement": "This country\'s '
                      'fuel, gas or power supply is under stress: rationing, load-shedding, an '
                      'emergency reserve release, a declared energy emergency, or prices at a '
                      'reported multi-year high", "status": "not_observed", "shape": "level", '
                      '"reference": "bar:a signal in this window reports rationing, load-shedding, '
                      'an emergency reserve release, a declared energy emergency, or a price at a '
                      'reported multi-year high, still in effect", "horizon_date": "<as-of + 7 '
                      'days>", "first_seen": "<as-of date>", "citations": []}',
                      '   {"id": "supply-route-chokepoint-risk", "statement": "Escalated '
                      "interdiction risk on a maritime chokepoint carrying this country's energy "
                      'imports", "status": "not_observed", "horizon_date": "<as-of + 14 days>", '
                      '"first_seen": "<as-of date>", "citations": []}'),
                     ('    concrete, resolvable near-term signposts, plus every level indicator\n'
                      '    (below). Set each "status" to',
                      '    concrete, resolvable near-term signposts. Set each "status" to'),
                     ("    schema's `YYYY-MM-DD` form; the human-date rule applies to prose only.\n"
                      '  - LEVEL INDICATORS — the entries whose "shape" is "level" measure a\n'
                      '    STANDING CONDITION against the bar named in their "reference", not a\n'
                      '    new event, and they are ALWAYS in the array. Each run, set a level\n'
                      '    indicator to "observed_at_level" when a signal in THIS window\'s slice\n'
                      "    reports the condition at that bar, and cite that signal's [N] in\n"
                      '    "citations"; otherwise set it to "not_observed". A level is observed\n'
                      '    again every window or it is not observed: your PRIOR READ, the\n'
                      '    situation register and any HISTORICAL SERIES line are memory, not a\n'
                      '    report, and an "observed_at_level" that cites only them is set back to\n'
                      '    "not_observed". Never mark a level indicator "triggered", never use\n'
                      '    "observed_at_level" on any other indicator, and copy "shape" and\n'
                      '    "reference" exactly as written in the schema.',
                      "    schema's `YYYY-MM-DD` form; the human-date rule applies to prose "
                      'only.')),
 'military_posture': (('    {"id": "readiness-level-change", "statement": "Declared change to the '
                       'standing readiness/alert level or reserve structure", "status": '
                       '"not_observed", "horizon_date": "<as-of + 30 days>", "first_seen": "<as-of '
                       'date>", "citations": []},\n'
                       '    {"id": "forces-in-active-operations", "statement": "This country\'s '
                       'armed forces are conducting combat or armed security operations", '
                       '"status": "not_observed", "shape": "level", "reference": "bar:a signal in '
                       "this window reports this country's forces in a strike, an engagement or a "
                       'deployed armed operation; a past campaign recalled does not count", '
                       '"horizon_date": "<as-of + 7 days>", "first_seen": "<as-of date>", '
                       '"citations": []},\n'
                       '    {"id": "heightened-posture-in-force", "statement": "A declared '
                       'heightened readiness, mobilization, martial law or emergency deployment is '
                       'in force", "status": "not_observed", "shape": "level", "reference": "bar:a '
                       'signal in this window reports the declared measure still in force", '
                       '"horizon_date": "<as-of + 7 days>", "first_seen": "<as-of date>", '
                       '"citations": []}',
                       '    {"id": "readiness-level-change", "statement": "Declared change to the '
                       'standing readiness/alert level or reserve structure", "status": '
                       '"not_observed", "horizon_date": "<as-of + 30 days>", "first_seen": "<as-of '
                       'date>", "citations": []}'),
                      ('It is REQUIRED and MUST be NON-EMPTY: always output 3-6 signpost entries '
                       'in\n'
                       'the EXACT SHAPE of the first three example objects shown in the schema\n'
                       'above, plus both level indicators (real',
                       'It is REQUIRED and MUST be NON-EMPTY: always output 3-6 entries in the '
                       'EXACT\n'
                       'SHAPE of the three example objects shown in the schema above (real'),
                      ("schema's `YYYY-MM-DD` form; the human-date rule applies to prose only.\n"
                       'LEVEL INDICATORS — the entries whose "shape" is "level" measure a\n'
                       'STANDING CONDITION against the bar named in their "reference", not a new\n'
                       'event, and they are ALWAYS in the array. Each run, set a level indicator\n'
                       'to "observed_at_level" when a signal in THIS window\'s slice reports the\n'
                       'condition at that bar, and cite that signal\'s [N] in "citations";\n'
                       'otherwise set it to "not_observed". A level is observed again every\n'
                       'window or it is not observed: your PRIOR READ, the situation register and\n'
                       'any HISTORICAL SERIES line are memory, not a report, and an\n'
                       '"observed_at_level" that cites only them is set back to "not_observed".\n'
                       'Never mark a level indicator "triggered", never use "observed_at_level"\n'
                       'on any other indicator, and copy "shape" and "reference" exactly as\n'
                       'written in the schema.',
                       "schema's `YYYY-MM-DD` form; the human-date rule applies to prose only."),
                      ('doctrine-revision, and its level indicators forces-in-active-operations\n'
                       'and heightened-posture-in-force. REUSE these exact id slugs verbatim every '
                       'run (only',
                       'doctrine-revision. REUSE these exact id slugs verbatim every run (only'))}


def pre_level_indicators(prompt: str) -> str:
    """``prompt`` with X4 LEVEL's edits rolled back; unchanged when absent.

    A unit's swaps apply only when ALL of its X4 texts are present; a partial
    match means the prompt moved again and the pin would no longer measure what
    it claims, so it raises rather than half-peeling.
    """
    out = prompt
    for unit, swaps in LATER_LEVEL_SWAPS.items():
        present = [new in out for new, _old in swaps]
        if not any(present):
            continue
        if not all(present):
            raise ValueError(f"{unit}: only part of the LEVEL prompt edit is present")
        for new, old in swaps:
            out = out.replace(new, old, 1)
    return out
