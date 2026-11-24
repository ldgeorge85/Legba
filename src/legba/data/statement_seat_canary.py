# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The seats' fixed CANARY — twenty items whose right answers are known by
construction (plan P1 §3 G4 "a fixed canary of 20 items daily"; §5.6 "the daily
canary is the only detector" of a silent model swap behind the alias).

Each item is a true statement, its kind, subject and valid time, and one
evidence row whose quote occurs verbatim in that row's stored text and states
every number and date the statement states. So, on every item:

* the TYPING seat's right answer is the item's ``kind`` / subject /
  ``valid_from`` / precision (typing agreement is measured against these);
* the VERIFIER seat's right answer is ``supports`` (its false-reject rate);
* the planted-error generator (:mod:`legba.data.statement_planted`) derives
  the wrong statements whose right answer is "does not support" (its catch
  rate).

SYNTHETIC ON PURPOSE. Every place, body and figure below is invented. The
canary measures the instrument, so it must not carry any instance's data, and
it must not be about any one use case: the twenty spread over logistics,
utilities, public health, markets, labour, governance and infrastructure.

A LEAF: stdlib + the shapes leaf.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .statement_shapes import (
    EvidenceTexts,
    StatementDraft,
    SubjectKind,
    SupportKind,
    SupportRole,
    SupportRow,
)

CANARY_VERSION = "statement_seat_canary.v1"


@dataclass(frozen=True)
class CanaryItem:
    """One known-true item. ``text`` is the evidence row's stored text."""

    item_id: str
    statement: StatementDraft
    support: tuple[SupportRow, ...]
    texts: EvidenceTexts
    subject_names: tuple[str, ...]


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


# (id, subject name, kind, statement, quote, valid (y, m, d|None), slots)
_ROWS: tuple[tuple[str, str, str, str, str, tuple[int, int, int | None], dict], ...] = (
    ("c01", "Port of Calder", "event",
     "A fire at the Port of Calder fuel terminal destroyed 3 storage tanks on 12 March 2026.",
     "a fire at the Port of Calder fuel terminal destroyed 3 storage tanks on 12 March",
     (2026, 3, 12), {}),
    ("c02", "Marrow Valley", "state",
     "Marrow Valley has banned exports of raw timber since 1 February 2026.",
     "Marrow Valley has banned exports of raw timber since 1 February",
     (2026, 2, 1), {"predicate": "export_ban"}),
    ("c03", "Orlen Province", "level",
     "Unemployment in Orlen Province stood at 7.4% in January 2026.",
     "unemployment in Orlen Province stood at 7.4% in January 2026",
     (2026, 1, None), {"predicate": "unemployment_rate", "value": 7.4, "unit": "percent"}),
    ("c04", "Tessaly", "attributed",
     "The Tessaly energy ministry said on 4 April 2026 that 2 reactors would close.",
     "the Tessaly energy ministry said on 4 April that 2 reactors would close",
     (2026, 4, 4), {}),
    ("c05", "Brennick", "event",
     "Floods in Brennick displaced 1,200 residents on 9 May 2026.",
     "floods in Brennick displaced 1,200 residents on 9 May",
     (2026, 5, 9), {}),
    ("c06", "Halvard Rail", "state",
     "Freight services on Halvard Rail's northern line have been suspended since 21 June 2026.",
     "freight services on Halvard Rail's northern line have been suspended since 21 June",
     (2026, 6, 21), {}),
    ("c07", "Quillon", "level",
     "Quillon's grain reserves fell to 640,000 tonnes in July 2026.",
     "Quillon's grain reserves fell to 640,000 tonnes in July 2026",
     (2026, 7, None), {"predicate": "grain_reserves", "value": 640000, "unit": "tonnes"}),
    ("c08", "Sarn", "event",
     "Sarn's parliament passed the water bill by 88 votes to 41 on 2 September 2026.",
     "Sarn's parliament passed the water bill by 88 votes to 41 on 2 September",
     (2026, 9, 2), {}),
    ("c09", "Delmere", "event",
     "A cyberattack on the Delmere hospital network took 14 clinics offline on 17 October 2026.",
     "a cyberattack on the Delmere hospital network took 14 clinics offline on 17 October",
     (2026, 10, 17), {}),
    ("c10", "Kestrel Bay", "state",
     "A curfew has been in force in Kestrel Bay since 5 November 2026.",
     "a curfew has been in force in Kestrel Bay since 5 November",
     (2026, 11, 5), {}),
    ("c11", "Varenne", "level",
     "Inflation in Varenne reached 3.1% in February 2026.",
     "inflation in Varenne reached 3.1% in February 2026",
     (2026, 2, None), {"predicate": "inflation_rate", "value": 3.1, "unit": "percent"}),
    ("c12", "Lowick Mining", "event",
     "Lowick Mining halted output at 2 copper mines on 23 January 2026.",
     "Lowick Mining halted output at 2 copper mines on 23 January",
     (2026, 1, 23), {}),
    ("c13", "Norhaven", "attributed",
     "Norhaven police said 6 people were arrested on 11 March 2026.",
     "Norhaven police said 6 people were arrested on 11 March",
     (2026, 3, 11), {}),
    ("c14", "Pellam", "event",
     "Pellam's central bank raised its main rate to 5.25% on 30 April 2026.",
     "Pellam's central bank raised its main rate to 5.25% on 30 April",
     (2026, 4, 30), {}),
    ("c15", "Corvan", "state",
     "Corvan has kept its border crossing at Ashford closed since 8 May 2026.",
     "Corvan has kept its border crossing at Ashford closed since 8 May",
     (2026, 5, 8), {}),
    ("c16", "Tiller Grid", "event",
     "A storm cut power to 52,000 homes served by Tiller Grid on 14 June 2026.",
     "a storm cut power to 52,000 homes served by Tiller Grid on 14 June",
     (2026, 6, 14), {}),
    ("c17", "Ember Valley", "level",
     "Ember Valley recorded 19 measles cases in the week to 3 July 2026.",
     "Ember Valley recorded 19 measles cases in the week to 3 July",
     (2026, 7, 3), {"predicate": "measles_cases_weekly", "value": 19}),
    ("c18", "Ryeport", "event",
     "Dock workers at Ryeport began a 48-hour strike on 12 August 2026.",
     "dock workers at Ryeport began a 48-hour strike on 12 August",
     (2026, 8, 12), {}),
    ("c19", "Calloway", "attributed",
     "The Calloway council announced on 25 September 2026 that 300 new homes would be built.",
     "the Calloway council announced on 25 September that 300 new homes would be built",
     (2026, 9, 25), {}),
    ("c20", "Mirefield", "state",
     "Mirefield's main reservoir has been below 40% capacity since 1 October 2026.",
     "Mirefield's main reservoir has been below 40% capacity since 1 October",
     (2026, 10, 1), {}),
)

_LEAD = "Officials confirmed the details at a briefing, and local reporting agreed that"
_TAIL = "Further figures are expected later in the week, the statement added."


def _build(row: tuple) -> CanaryItem:
    item_id, name, kind, text, quote, (y, m, d), slots = row
    valid = _utc(y, m, d or 1)
    published = (valid + timedelta(days=1)) if d else _utc(y + (m == 12), m % 12 + 1, 2)
    signal_id = f"canary-signal-{item_id}"
    body = f"{_LEAD} {quote}. {_TAIL}"
    statement = StatementDraft(
        text=text, kind=kind, subject_kind=SubjectKind.ENTITY.value,
        subject_key=f"canary:{item_id}", valid_from=valid,
        valid_from_precision="day" if d else "month", slots=dict(slots) or None,
    )
    support = (SupportRow(
        role=SupportRole.EVIDENCE.value, support_kind=SupportKind.SIGNAL.value,
        ref_id=signal_id, quote=quote, source_id=f"canary-source-{item_id}",
        access_class="public", published_at=published, title=f"{name}: briefing",
    ),)
    return CanaryItem(item_id, statement, support,
                      EvidenceTexts(by_signal={signal_id: body}), (name,))


#: The twenty items, in a fixed order.
CANARY: tuple[CanaryItem, ...] = tuple(_build(r) for r in _ROWS)


# ---------------------------------------------------------------------------
# The PARAPHRASE controls (S-6) — twenty more known-true items, harder
# ---------------------------------------------------------------------------

#: The canary's statements nearly copy their quotes; a verifier that only
#: matches strings passes them. These twenty are true by construction too,
#: but each says it in OTHER words than its quote — a number word for a
#: digit, a French source, a paraphrased verb, an approximation the quote
#: shares, a date range or list written another way, a statement that needs
#: TWO quotes of its row. They are the known-true half of the planted-error
#: measurement (``statement_planted``): a verifier must pass these AND catch
#: the plants. Not part of the daily canary (which stays the twenty above).
CONTROLS_VERSION = "statement_seat_controls.v1"

# (id, subject name, kind, statement, quotes, valid (y, m, d|None), slots)
_CONTROL_ROWS: tuple[tuple[str, str, str, str, tuple[str, ...], tuple[int, int, int | None], dict], ...] = (
    ("k01", "Tarsk", "event",
     "Rioting in Tarsk left ten people dead on 3 March 2026.",
     ("ten people were killed in rioting in Tarsk on 3 March",), (2026, 3, 3), {}),
    ("k02", "Haldane", "event",
     "About 300 families in Haldane were evacuated on 7 April 2026.",
     ("some 300 families were evacuated from Haldane on 7 April",), (2026, 4, 7), {}),
    ("k03", "Corran", "event",
     "The Corran trade summit was held on 18–19 May 2026.",
     ("the Corran trade summit was held from May 18 to 19",), (2026, 5, 18), {}),
    ("k04", "Westfold", "level",
     "Joblessness in Westfold fell by 0.4 percentage points to 6.1% in March 2026.",
     ("the unemployment rate in Westfold dropped 0.4 percentage points to 6.1% in March",),
     (2026, 3, None), {"predicate": "unemployment_rate", "value": 6.1, "unit": "percent"}),
    ("k05", "Brask", "attributed",
     "Officials in Brask said at least 12 bridges were damaged by the floods of 9 June 2026.",
     ("at least 12 bridges were damaged, Brask officials said of the floods on 9 June",), (2026, 6, 9), {}),
    ("k06", "Ferrow", "event",
     "A strike by Ferrow port workers began on 2 July 2026 and halted 40 ships.",
     ("Ferrow port workers walked off the job on 2 July", "the strike halted 40 ships at Ferrow"),
     (2026, 7, 2), {}),
    ("k07", "Lissa", "event",
     "Lissa held its third election in two years on 5 August 2026.",
     ("Lissa voted on 5 August in its third election in two years",), (2026, 8, 5), {}),
    ("k08", "Oberly", "event",
     "Teachers in Oberly staged a three-day strike starting on 1 September 2026.",
     ("teachers in Oberly began a 3-day strike on 1 September",), (2026, 9, 1), {}),
    ("k09", "Valmont", "event",
     "Ten people were injured in a bus crash near Valmont on 11 October 2026.",
     ("dix personnes ont été blessées dans un accident de bus près de Valmont le 11 octobre",),
     (2026, 10, 11), {}),
    ("k10", "Norreby", "event",
     "Authorities in Norreby evacuated 73,000 residents on 14 October 2026.",
     ("les autorités de Norreby ont évacué 73 000 habitants le 14 octobre",), (2026, 10, 14), {}),
    ("k11", "Calvast", "event",
     "Between 200 and 250 protesters gathered in Calvast on 2 November 2026.",
     ("between 200 and 250 protesters gathered in central Calvast on 2 November",), (2026, 11, 2), {}),
    ("k12", "Hesk", "state",
     "Road traffic across the Hesk border crossing has been suspended since 4 February 2026.",
     ("the Hesk border crossing has been closed to road traffic since 4 February",), (2026, 2, 4), {}),
    ("k13", "Pardale", "level",
     "Grain exports from Pardale reached 1.2 million tonnes in May 2026.",
     ("Pardale shipped 1.2m tonnes of grain abroad in May",), (2026, 5, None),
     {"predicate": "grain_exports", "value": 1200000, "unit": "tonnes"}),
    ("k14", "Kessel", "attributed",
     "The Kessel health ministry reported 27 new cholera cases on 6 March 2026.",
     ("27 new cases of cholera were recorded, the Kessel health ministry said on 6 March",), (2026, 3, 6), {}),
    ("k15", "Dunlow", "event",
     "Nearly 500 homes in Dunlow lost their water supply on 19 April 2026.",
     ("nearly 500 homes in Dunlow were left without running water on 19 April",), (2026, 4, 19), {}),
    ("k16", "Sallowmere", "event",
     "Sallowmere's council approved a budget of 45 million crowns on 27 January 2026, 3 million more "
     "than last year.",
     ("Sallowmere's council approved a 45 million crown budget on 27 January",
      "the budget is 3 million crowns larger than last year's"), (2026, 1, 27), {}),
    ("k17", "Arvel", "level",
     "Turnout in the Arvel referendum of 8 March 2026 was 61%.",
     ("61 per cent of Arvel voters took part in the referendum held on 8 March",), (2026, 3, 8),
     {"predicate": "turnout", "value": 61, "unit": "percent"}),
    ("k18", "Brightwater", "event",
     "Storms closed schools in Brightwater on 13 and 14 January 2026.",
     ("schools in Brightwater were shut on January 13 and 14 because of storms",), (2026, 1, 13), {}),
    ("k19", "Merrow", "event",
     "Two people died when a dam failed at Merrow on 22 May 2026.",
     ("a dam collapse at Merrow on 22 May killed two people",), (2026, 5, 22), {}),
    ("k20", "Gantry", "attributed",
     "Police in Gantry estimated the crowd at around 5,000 on 30 June 2026.",
     ("police in Gantry put the crowd at about 5,000 on 30 June",), (2026, 6, 30), {}),
)


def _build_control(row: tuple) -> CanaryItem:
    item_id, name, kind, text, quotes, (y, m, d), slots = row
    valid = _utc(y, m, d or 1)
    published = (valid + timedelta(days=2)) if d else _utc(y + (m == 12), m % 12 + 1, 2)
    signal_id = f"control-signal-{item_id}"
    body = f"{_LEAD} {quotes[0]}."
    for q in quotes[1:]:
        body += f" In a separate statement, {q}."
    body += f" {_TAIL}"
    statement = StatementDraft(
        text=text, kind=kind, subject_kind=SubjectKind.ENTITY.value,
        subject_key=f"control:{item_id}", valid_from=valid,
        valid_from_precision="day" if d else "month", slots=dict(slots) or None,
    )
    support = tuple(SupportRow(
        role=SupportRole.EVIDENCE.value, support_kind=SupportKind.SIGNAL.value,
        ref_id=signal_id, quote=q, source_id=f"control-source-{item_id}",
        access_class="public", published_at=published, title=f"{name}: report",
    ) for q in quotes)
    return CanaryItem(item_id, statement, support, EvidenceTexts(by_signal={signal_id: body}), (name,))


#: The twenty paraphrase controls, in a fixed order.
CONTROLS: tuple[CanaryItem, ...] = tuple(_build_control(r) for r in _CONTROL_ROWS)

__all__ = ["CANARY", "CANARY_VERSION", "CONTROLS", "CONTROLS_VERSION", "CanaryItem"]
