# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A CHANGE REPORT IS ITS OWN RE-EVIDENCE (``legba.data.carried_state``).

The carry rule drops a moderate-or-higher desk read whose indicators
all read ``not_observed`` once the newest read whose indicators DID see
something is older than 3 x the 11 h cadence. A desk with only change-shaped
tripwires and no level-shaped indicator almost never re-evidences (the top-job
desk: 9 of 885 reads in the 14 days to 2026-11-08), so on 2026-11-08 the US
chief-of-staff reads (Miller replaces Wiles) and the Israeli mandate reads
(Herzog tasks Eisenkot) were dropped against triggers 10-14 days old.

The done line, as tests on the REAL reads (headline + BLUF + cited wire items
and their fetch times, as persisted):

  * the four change reads survive — their verdict reports a change and each
    cites 12-16 wire items fetched inside its own 33 h window;
  * stasis reads keep the old rule — DRC and Japan "no change in the top job",
    the US energy read (8 fresh items, but a stasis verdict) are still dropped;
  * a change verdict on STALE evidence is still dropped (Mexico elite-and-unrest
    29a4aa16, *"…welfare-secretary arrest…"*: 2 of 6 cited items inside the
    window);
  * the shared text test no longer reads *"Assessed - …unlikely"* as a change.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data import carried_state as cs
from legba.data import change_report as cr
from legba.data.analysts import assembly_world as aw

CADENCE_H = 11.0  # every country desk's cooldown_seconds 39600

REAL_READS: dict[str, dict[str, Any]] = {
    '3824a07b': {
        'id': '3824a07b-de1d-4ad2-8719-bdb7d5bf3c78',
        'analyst_id': 'leadership_transition',
        'target_id': 'country_g20_us',
        'produced_at': '2026-11-08T00:46:39.107093+00:00',
        'severity': 'moderate',
        'title': "US: post-midterm firings reach Wiles and Miller; no cited signal reaches Trump's job",
        'bluf': "Assessed that a change in the top US leadership job within about four weeks of 8 November 2026 remains unlikely, because the post-midterm shake-up is moving at staff level — Susie Wiles out as chief of staff, Stephen Miller in, and Trump hinting at further firings — while no signal in this desk's collection through 8 November reports impeachment proceedings, a forced resignation, or an activated succession mechanism [103][120][121].",
        'cited': {
            '15c2a3c6-714d-4f14-a015-928a17b9376e': '2026-11-07T12:54:27.365921+00:00',
            'ac17e570-84c4-4893-9ea7-412a41937b98': '2026-11-07T11:14:08.932216+00:00',
            'f38b8498-75c5-4ab4-b0c5-85c92e41f827': '2026-11-07T14:42:01.299358+00:00',
            'e938a1bd-8965-42b7-a6b0-cf4e39026a91': '2026-11-07T20:01:18.366194+00:00',
            'fa2b59e1-6aad-4a92-9766-f9f9f0e5bd7d': '2026-11-07T21:15:11.479223+00:00',
            '2e7d9a4f-e2f3-47db-bb36-cea936fa4bd0': '2026-11-07T18:51:08.562118+00:00',
            '10b2a519-7727-4139-ba64-4de29a5fff16': '2026-11-07T19:17:20.064385+00:00',
            '50824f84-da2c-476f-9d68-bd027d9a245c': '2026-11-07T13:11:01.064333+00:00',
            '2b3c4986-9039-408e-8944-754028061476': '2026-11-07T17:44:02.307929+00:00',
            'b40a3dde-a10c-41b9-9840-6c50485427db': '2026-11-07T11:15:29.151658+00:00',
            '95b70ef6-9189-43a0-8b68-1efdc4132911': '2026-11-07T11:17:24.191183+00:00',
            'c8fae564-0f37-4ddb-9f8c-fdca45ffa51c': '2026-11-07T14:30:00.769746+00:00',
        },
    },
    'f31be7d4': {
        'id': 'f31be7d4-5224-4d0c-be40-4eb67b26e9da',
        'analyst_id': 'leadership_transition',
        'target_id': 'country_g20_us',
        'produced_at': '2026-11-08T11:53:07.83576+00:00',
        'severity': 'moderate',
        'title': 'US: post-midterm churn stays at staff level as Trump reshuffles the White House; no mechanism reaches the presidency',
        'bluf': "Assessed that a change in the US top leadership job within about four weeks of 8 November 2026 remains unlikely — roughly one in five — because the post-midterm turbulence is moving through staff and party ranks rather than toward the presidency itself, with Stephen Miller named chief of staff, a vacant press-secretary post being shopped to an outsider, and a Republican blame game opening over the Iran war, while no signal in this desk's collection through 8 November reports impeachment proceedings, a forced resignation, or an activated succession mechanism [35][62][72][120].",
        'cited': {
            'dba84cfd-2202-4734-a338-fb207d10c392': '2026-11-08T08:11:12.325534+00:00',
            '7c4f547e-ddd7-43f1-b77c-e461c2f39bf6': '2026-11-08T03:45:00.370119+00:00',
            '4073779b-516d-4f48-8c28-d1eaf521396f': '2026-11-08T02:46:22.289165+00:00',
            '2e7d9a4f-e2f3-47db-bb36-cea936fa4bd0': '2026-11-07T18:51:08.562118+00:00',
            '4eb5439a-7021-4311-b42a-65c57f8a90a3': '2026-11-08T05:17:10.238522+00:00',
            'dc958eed-c30c-4b97-8dfd-04bddb277478': '2026-11-08T03:18:16.174556+00:00',
            'aeda9670-f979-494a-93fc-81253773df87': '2026-11-08T02:18:23.999318+00:00',
            'fa2b59e1-6aad-4a92-9766-f9f9f0e5bd7d': '2026-11-07T21:15:11.479223+00:00',
            'd0f742df-2e8f-4f05-b62f-a2f79b0d7635': '2026-11-08T08:49:16.808861+00:00',
            'e0f181c3-12eb-473d-9b79-333b4338b669': '2026-11-07T23:44:26.412956+00:00',
            '10b2a519-7727-4139-ba64-4de29a5fff16': '2026-11-07T19:17:20.064385+00:00',
            '4bf581dc-f255-4f1a-ba85-c6649e917d79': '2026-11-08T06:18:00.809415+00:00',
        },
    },
    '48967698': {
        'id': '48967698-2cd9-4b15-a8c8-3e5453c6ee92',
        'analyst_id': 'leadership_transition',
        'target_id': 'country_watch_il',
        'produced_at': '2026-11-08T01:05:12.382399+00:00',
        'severity': 'high',
        'title': "Israel: Herzog picks Eisenkot over Netanyahu to form government, putting the PM's exit on a 28-day constitutional clock",
        'bluf': 'Assessed: a change of prime minister in Israel is likely over the next four to eight weeks through the constitutional mandate process, because Herzog announced on 7 November that he will task opposition leader Gadi Eisenkot with forming a government — after 67 lawmakers recommended Eisenkot and 46 recommended Netanyahu — and Eisenkot received his mandate on 8 November with 28 days, plus a possible 14-day extension, to assemble 61 seats, while coalition vetoes over Arab-party support and the Shin Bet probe of the Arab vote are what could slow or stall that handover [6][14][20][21][24][101].',
        'cited': {
            '608e18be-9b83-496d-9919-4a96c6506ed3': '2026-11-07T22:45:00.85216+00:00',
            '5ff1a2dd-72c4-42e2-b83d-1d9d1b1c1dc4': '2026-11-07T21:18:22.884597+00:00',
            '8bd03712-bcd6-4c67-8b59-9faacb0d0af8': '2026-11-07T20:45:01.313798+00:00',
            'c8210fc2-5813-46c8-8dc2-690626dd50a4': '2026-11-07T20:34:14.80589+00:00',
            '7b743c45-35f1-46d7-84e0-b0707c80e6e2': '2026-11-07T20:11:01.138849+00:00',
            'd45222bf-d244-4bab-a1f4-5f63d7490c68': '2026-11-06T12:41:10.61116+00:00',
            '54c852d1-dbf1-4b44-a7ea-f7a749a47f71': '2026-11-07T21:54:15.867484+00:00',
            '5575fd92-cded-49bf-b743-9bba32f5718b': '2026-11-07T19:44:03.887855+00:00',
            '296fe850-94c9-4e31-a093-fb49116c0322': '2026-11-07T21:15:35.627088+00:00',
            'c05afa47-92cc-46e6-9fb9-c631b109ce43': '2026-11-07T05:30:00.76188+00:00',
            'df7d95df-8325-4f56-a0f5-abc43a10d66b': '2026-11-07T22:44:26.760794+00:00',
            'a2887ea5-d86f-4f91-8abb-b1e5029bd34c': '2026-11-07T02:27:01.135698+00:00',
            'ef835de7-52f4-46f4-ba4e-d03943a0012b': '2026-11-07T18:11:01.219579+00:00',
            'c0d9abbc-fe94-491c-9b2b-3fed71b820a5': '2026-11-06T18:10:01.461276+00:00',
            '957b91f1-5200-4fa5-a25f-9e17d85e9ad6': '2026-11-06T08:54:09.297166+00:00',
            '2e18d2ec-40e8-425e-aba9-f254c9e47a3d': '2026-11-07T04:11:01.222081+00:00',
            '6b628351-1e21-4a08-9bd7-e99fe6c2c872': '2026-11-07T20:14:16.584478+00:00',
            'e403c588-e642-4c0d-a8b3-de3c413649cc': '2026-11-06T08:15:24.647059+00:00',
        },
    },
    '5191ef46': {
        'id': '5191ef46-86d0-469e-b699-81c96ac937c9',
        'analyst_id': 'leadership_transition',
        'target_id': 'country_watch_il',
        'produced_at': '2026-11-08T12:26:29.362515+00:00',
        'severity': 'high',
        'title': 'Israel: Eisenkot holds the mandate as coalition vetoes, not the clock, are what decide whether Netanyahu exits',
        'bluf': 'Assessed: a change of prime minister in Israel is likely over the next four to eight weeks through the constitutional mandate process, because President Herzog tasked opposition leader Gadi Eisenkot with forming a government on 7 November after 67 lawmakers recommended him against 46 for Netanyahu, and Eisenkot received the mandate on 8 November with 28 days, plus a possible 14-day extension, to assemble 61 seats — while the vetoes over Arab-party and ultra-Orthodox participation, not the calendar, are what could stall that handover [5][14][51][64][65].',
        'cited': {
            '2bbd1ea9-2f5d-44dd-bd9a-d38d0fe98f25': '2026-11-08T10:54:00.920527+00:00',
            'e9e566df-73df-48ad-af78-82679f3a744d': '2026-11-08T09:21:03.104624+00:00',
            '608e18be-9b83-496d-9919-4a96c6506ed3': '2026-11-07T22:45:00.85216+00:00',
            '8bd03712-bcd6-4c67-8b59-9faacb0d0af8': '2026-11-07T20:45:01.313798+00:00',
            'c8210fc2-5813-46c8-8dc2-690626dd50a4': '2026-11-07T20:34:14.80589+00:00',
            '212d3128-e668-4f3b-9a6b-5314576d18ee': '2026-11-08T05:54:16.32627+00:00',
            'e40633cd-df4f-40c4-b46a-13eb7be4b758': '2026-11-07T21:44:28.046627+00:00',
            'df7d95df-8325-4f56-a0f5-abc43a10d66b': '2026-11-07T22:44:26.760794+00:00',
            'a2887ea5-d86f-4f91-8abb-b1e5029bd34c': '2026-11-07T02:27:01.135698+00:00',
            '2353966b-65c9-4060-984e-25e8e1b571bc': '2026-11-08T10:15:40.716279+00:00',
            '76e32713-4331-42fc-b360-04e5a956e63e': '2026-11-08T09:46:00.638533+00:00',
            'c0d9abbc-fe94-491c-9b2b-3fed71b820a5': '2026-11-06T18:10:01.461276+00:00',
            'ec93d74d-62a5-4831-9412-9736f187fa51': '2026-11-07T23:44:18.70028+00:00',
            '1bdfcbb3-9aca-48c3-8136-6c7efda336c7': '2026-11-07T20:49:00.34707+00:00',
            '6f8f5ea6-bac7-4916-879d-420a51e8d9d1': '2026-11-07T20:16:00.267529+00:00',
            '59081aca-93ad-44ce-9425-49061c95f8df': '2026-11-07T22:06:00.924789+00:00',
            '5ff1a2dd-72c4-42e2-b83d-1d9d1b1c1dc4': '2026-11-07T21:18:22.884597+00:00',
            '6930aea1-6f1b-4e3f-97ab-e6c1f9e8f078': '2026-11-07T20:09:08.935155+00:00',
        },
    },
    'dd2bb0dd': {
        'id': 'dd2bb0dd-6f3e-48e7-82b2-1a45443d5e99',
        'analyst_id': 'leadership_transition',
        'target_id': 'country_watch_cd',
        'produced_at': '2026-11-06T01:05:02.089651+00:00',
        'severity': 'moderate',
        'title': "DR Congo: Kabila's FRN threatens to quit Doha talks over third-term referendum; Tshisekedi still holds the top job",
        'bluf': "Assessed that a change in the DRC's top leadership is unlikely over the next 30 days — Tshisekedi retains the presidency with no cited signal showing a no-confidence vote, forced resignation, succession trigger, or military move on Kinshasa, while the pressure on his hold comes from Kabila's FRN threatening on 3 November to abandon the Doha peace talks if the government proceeds with a constitutional referendum to grant Tshisekedi a third term [14][17][18].",
        'cited': {
            'a8aa5589-6393-454e-9afd-d91193788ed9': '2026-11-03T21:44:17.917226+00:00',
            '4ffa9431-a3d9-48b8-9c69-3ed01e2e2383': '2026-11-03T15:55:28.494817+00:00',
            'ca5d6947-5707-4b66-9b05-f627dcdc1df3': '2026-11-03T15:08:00.182195+00:00',
            '585c07f7-2ffe-42c5-aa3f-2f992374700d': '2026-11-03T22:27:01.50161+00:00',
        },
    },
    '97e6b15f': {
        'id': '97e6b15f-ab91-4240-b607-3a8bd6ce2804',
        'analyst_id': 'leadership_transition',
        'target_id': 'country_g20_jp',
        'produced_at': '2026-11-01T13:02:56.820655+00:00',
        'severity': 'moderate',
        'title': 'Japan: Takaichi still chairs cabinet councils as coalition seat-cut bill waits on a 12 December session, with no route open to the top job',
        'bluf': "Assessed likelihood of a change in Japan's top leadership within about 30 days remains low — probably on the order of one in five — because Prime Minister Sanae Takaichi chaired the Council on Economic and Fiscal Policy on 30 October and approved an ordinance the prior Tuesday, her coalition's seat-cut programme is moving toward the session that ends 12 December, and no no-confidence vote, coalition withdrawal, forced resignation, or leader-incapacity signal appears in this desk's collection through 1 November [65][56][89].",
        'cited': {
            '840a00b0-9210-4cb1-93f3-f9d068925b43': '2026-10-30T03:46:00.637077+00:00',
            '96ddbce7-96fd-4565-a9d7-2d49fedbec08': '2026-10-30T06:44:29.852089+00:00',
            '831cab5b-5c54-4c28-9a7e-03d838ecc931': '2026-10-30T14:41:17.038923+00:00',
            '2f797c06-cdd3-44cd-9516-6e819fa1449c': '2026-10-31T17:18:23.980539+00:00',
            'bc1b17fa-f970-44a3-bec0-ec4bd71f4595': '2026-10-31T02:46:00.846555+00:00',
            '15e6dd75-0d0d-4811-8715-fe9ead966548': '2026-10-30T19:18:14.217004+00:00',
            '3990b594-a0a6-4f95-a416-94b4e6aaad1c': '2026-11-01T03:46:07.175246+00:00',
            '9abe934f-28cd-4307-9c20-075335d06ab0': '2026-10-29T23:01:23.423681+00:00',
            '47cc5336-189c-4d69-b1b8-88f1b43952a9': '2026-10-30T06:46:07.284566+00:00',
            'b816f2c8-cca5-43a6-9039-c0b7acb869c4': '2026-10-30T03:46:27.627829+00:00',
        },
    },
    '74b5cc8f': {
        'id': '74b5cc8f-f212-4fd2-ae24-1a167a73f970',
        'analyst_id': 'energy_security',
        'target_id': 'country_g20_us',
        'produced_at': '2026-11-08T00:48:06.730418+00:00',
        'severity': 'high',
        'title': 'US pressure stays high as new Iran strikes and an expanded naval blockade deepen Hormuz risk while diesel deal stalls',
        'bluf': 'US energy-security pressure remains high because fresh US strikes hit Iranian air defenses on Qeshm Island on 6 November with further blasts reported on 8 November, the US announced a new task force to expand its naval blockade of Iran, and no Russian diesel had reached the US a month into the Trump-Putin deal while domestic diesel prices nearly doubled [107][4][96][105].',
        'cited': {
            'db1b9452-6291-4670-8604-43ae903cf816': '2026-11-07T12:21:01.293048+00:00',
            'ca68cfda-a045-46b6-becf-b7c2a61d3511': '2026-11-08T00:45:01.029301+00:00',
            'b8851f40-6535-4b32-aeed-005fcc8f47f1': '2026-11-07T14:11:01.084347+00:00',
            'c1a60a1e-1ca3-4891-80f8-8e496f811679': '2026-11-07T12:45:23.713762+00:00',
            '910c25c4-7790-4046-9432-ec77aeb7d564': '2026-11-07T12:04:00.083838+00:00',
            '9743a008-de1f-4d5b-ac48-e5adc2284134': '2026-11-07T14:34:00.293344+00:00',
            '953fe230-3884-484f-b8f0-798d1062b607': '2026-11-07T15:33:25.214353+00:00',
            '8c95c040-8c1b-435b-b763-a29ab4304584': '2026-11-07T19:11:08.541153+00:00',
        },
    },
    '29a4aa16': {
        'id': '29a4aa16-e9d7-4de8-a216-732bae94f387',
        'analyst_id': 'internal_stability',
        'target_id': 'country_g20_mx',
        'produced_at': '2026-11-03T02:02:34.712384+00:00',
        'severity': 'moderate',
        'title': 'Mexico: Uruapan march and welfare-secretary arrest keep cartel-driven elite friction live while no force split appears',
        'bluf': "I judge Mexico's internal stability to be holding at moderate severity, driven by cartel-linked violence against local officeholders and the resulting elite/institutional friction rather than by street unrest or a security-force split, with no mass protest wave, force defection, emergency measure, or mass-arrest crackdown appearing in this desk's collection through 3 November [10][14].",
        'cited': {
            'b09fce7f-c3dc-48cc-9601-3a1baade5153': '2026-11-02T20:06:14.772979+00:00',
            '6889150d-bf49-4fbe-a093-0bd4850d4159': '2026-11-02T14:01:23.752356+00:00',
            'd591d977-3fb2-46a6-a1a0-18030b92e7a8': '2026-10-31T16:06:00.279668+00:00',
            'a05eddd8-77e3-4230-850f-0fa7ae69791a': '2026-10-31T18:06:00.461882+00:00',
        },
    },
}

#: The newest INDICATOR-evidencing read each real read was measured against on
#: 2026-11-08 (``data.assembly.coverage[].last_evidence_*`` of compositions
#: 0d2d72d2 and 46b28adc).
LAST_EVIDENCE = {
    "country_g20_us": {"id": "30011973-54df-42b2-9a4c-acd7fc400db4",
                       "produced_at": "2026-10-25T11:12:15.986140+00:00"},
    "country_watch_il": {"id": "4a409b11-d9ad-4855-8ec3-df312f0a9d53",
                         "produced_at": "2026-10-29T13:05:33.382312+00:00"},
}
CHANGE_READS = ("3824a07b", "f31be7d4", "48967698", "5191ef46")
STASIS_READS = ("dd2bb0dd", "97e6b15f", "74b5cc8f")
STALE_CHANGE_READ = "29a4aa16"


def _row(key: str) -> dict[str, Any]:
    r = REAL_READS[key]
    return {
        "id": r["id"],
        "analyst_id": r["analyst_id"],
        "target_id": r["target_id"],
        "produced_at": r["produced_at"],
        "severity": r["severity"],
        "data": {
            "title": r["title"],
            "body": f"*As of …*\n\n**BLUF:** {r['bluf']}\n\n## What changed\n- …",
            "tags": [f"severity:{r['severity']}"],
            "data": {
                "indicators": [
                    {"id": f"tripwire-{i}", "status": "not_observed", "citations": []}
                    for i in range(3)
                ],
                "citations": [
                    {"marker": f"[{i + 1}]", "signal_id": sid}
                    for i, sid in enumerate(r["cited"])
                ],
            },
        },
        cs.UNIT_CADENCE_ROW_KEY: CADENCE_H,
    }


def _fetched(key: str) -> dict[str, datetime]:
    return {
        sid: datetime.fromisoformat(at)
        for sid, at in REAL_READS[key]["cited"].items() if at
    }


def _now(key: str, after_h: float = 10.75) -> datetime:
    """The composition ran ~10.75 h after the read (0d2d72d2 on 3824a07b)."""
    return datetime.fromisoformat(REAL_READS[key]["produced_at"]) + timedelta(hours=after_h)


def _last_evidence(key: str) -> dict[str, Any]:
    ev = LAST_EVIDENCE.get(REAL_READS[key]["target_id"])
    if ev is not None:
        return {"id": ev["id"], "produced_at": datetime.fromisoformat(ev["produced_at"])}
    # The stasis fixtures: a trigger 10 days before the read — well past 33 h.
    at = datetime.fromisoformat(REAL_READS[key]["produced_at"]) - timedelta(days=10)
    return {"id": str(uuid4()), "produced_at": at}


def _verdict(key: str, *, fetched: dict[str, Any] | None, after_h: float = 10.75):
    return cs.carry_verdict(
        _row(key), last_evidence=_last_evidence(key), cadence_h=CADENCE_H,
        now=_now(key, after_h), windows=3.0, signal_fetched_at=fetched,
    )


# ---------------------------------------------------------------------------
# 1. The pure rule on the real reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", CHANGE_READS)
def test_the_four_political_change_reads_survive(key: str) -> None:
    row = _row(key)
    assert cs.reports_change(row)
    # Without the fetch map consulted, every one of them was dropped …
    convicted = _verdict(key, fetched=None)
    assert convicted is not None and convicted["cause"] == cs.CAUSE_NOT_RE_EVIDENCED
    assert convicted["last_evidence_age_h"] > 33.0
    # … and with the fetch times of what it cites, it is its own re-evidence.
    proof = cs.change_self_evidence(
        row, signal_fetched_at=_fetched(key), cadence_h=CADENCE_H, now=_now(key), windows=3.0,
    )
    assert proof is not None and proof["re_evidence"] == "change_reported"
    assert proof["fresh_signals"] >= 12 >= cs.CHANGE_SELF_EVIDENCE_MIN_SIGNALS
    assert proof["limit_h"] == pytest.approx(33.0)
    assert _verdict(key, fetched=_fetched(key)) is None


@pytest.mark.parametrize("key", STASIS_READS)
def test_stasis_reads_keep_the_rule(key: str) -> None:
    assert not cs.reports_change(_row(key))
    v = _verdict(key, fetched=_fetched(key))
    assert v is not None and v["cause"] == cs.CAUSE_NOT_RE_EVIDENCED


def test_a_stasis_read_is_dropped_however_fresh_its_citations() -> None:
    """The US energy read cites 8 items fetched inside its window; its verdict
    is stasis (*"US pressure stays high …"*), so freshness alone saves nothing."""
    row, key = _row("74b5cc8f"), "74b5cc8f"
    floor = datetime.fromisoformat(REAL_READS[key]["produced_at"]) - timedelta(hours=33)
    assert sum(1 for at in _fetched(key).values() if at >= floor) >= 3
    assert cs.change_self_evidence(
        row, signal_fetched_at=_fetched(key), cadence_h=CADENCE_H, now=_now(key), windows=3.0,
    ) is None


def test_a_change_verdict_on_stale_evidence_is_still_dropped() -> None:
    key = STALE_CHANGE_READ
    assert cs.reports_change(_row(key))
    floor = datetime.fromisoformat(REAL_READS[key]["produced_at"]) - timedelta(hours=33)
    assert sum(1 for at in _fetched(key).values() if at >= floor) < cs.CHANGE_SELF_EVIDENCE_MIN_SIGNALS
    assert _verdict(key, fetched=_fetched(key)) is not None


def test_two_fresh_items_are_not_enough() -> None:
    key = "3824a07b"
    two = dict(list(_fetched(key).items())[:2])
    stale = {sid: at - timedelta(days=30) for sid, at in _fetched(key).items() if sid not in two}
    assert _verdict(key, fetched={**two, **stale}) is not None
    three = dict(list(_fetched(key).items())[:3])
    stale3 = {sid: at - timedelta(days=30) for sid, at in _fetched(key).items() if sid not in three}
    assert all(
        datetime.fromisoformat(REAL_READS[key]["produced_at"]) - at <= timedelta(hours=33)
        for at in three.values()
    )
    assert _verdict(key, fetched={**three, **stale3}) is None


def test_a_change_read_older_than_its_own_window_is_not_its_own_evidence() -> None:
    """The read is re-evidence AT its own time: 40 h on, it no longer is."""
    assert _verdict("3824a07b", fetched=_fetched("3824a07b"), after_h=40.0) is not None


def test_items_fetched_after_the_read_do_not_count() -> None:
    key = "48967698"
    later = {sid: datetime.fromisoformat(REAL_READS[key]["produced_at"]) + timedelta(hours=1)
             for sid in REAL_READS[key]["cited"]}
    assert _verdict(key, fetched=later) is not None


# ---------------------------------------------------------------------------
# 2. The shared text test (world floor + carry rule)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    # A dash after the epistemic frame used to end the lead clause at "assessed".
    ("Assessed - a change in Sudan's top leadership is unlikely over the next four "
     "weeks: no signal reports a forced resignation, elite defection, or coup [46].", False),
    ("Assessed: no change in France's top leadership is likely over the next two weeks "
     "- Macron and PM Lecornu remain in office with no resignation [3].", False),
    ("Assessed that a change in the top US leadership job within about four weeks "
     "remains unlikely, because the post-midterm shake-up is moving at staff level.", False),
    ("Assessed: a change of prime minister in Israel is likely over the next four to "
     "eight weeks through the constitutional mandate process [6].", True),
    ("US: post-midterm firings reach Wiles and Miller; no cited signal reaches "
     "Trump's job", True),
    # A change word named only to be denied (dropped heads of 2026-11-08).
    ("Through 8 November 2026 this desk's collection reports no unrest, "
     "security-force split, or elite defection in Burkina Faso.", False),
    ("Iran: wartime state stays cohesive as blockade tightens, with no street or "
     "defection signals in this 72-hour slice", False),
    ("Taiwan: PNG LNG halt costs Taiwan nothing it cannot replace, while fuel "
     "freeze holds", False),
    ("Haiti's coup vulnerability remains high because the state's street-security "
     "still rests on more than 2,000 Salvadoran troops", False),
    # Mali's world block — eligible for the restraint floor, absent the fetch map, on the denied "defection" alone.
    ("Repression of political critics remains this desk's driving instability "
     "vector, now coupled with the state's capacity test from a mass Guinea influx "
     "— and no mass protest, security-force defection, or elite fracture appears "
     "in this desk's collection through 8 November [61][31][14].", False),
    # … while an asserted change after a denied one still counts, and
    # "no longer" is a change report.
    ("Elite fracture over the presidential wiretap treason case remains the driver "
     "of this desk's read, with a fourth suspect named on 6 November, a former "
     "national police chief and the president's chief of staff detained.", True),
    ("The ruling coalition no longer commands a majority after two parties "
     "defected on 4 November [2][5].", True),
    ("Assessed", False),
    ("", False),
])
def test_the_change_test(text: str, expected: bool) -> None:
    assert cr.text_reports_change(text) is expected


def test_the_world_floor_reads_the_same_objects() -> None:
    assert aw._CHANGE_RE is cr.CHANGE_RE and aw._STASIS_RE is cr.STASIS_RE
    from legba.data.analysts import assembly_carry as ac
    assert ac._LEAD_CLAUSE_END_RE is cr.LEAD_CLAUSE_END_RE


def test_the_bluf_and_headline_are_the_verdict_surfaces() -> None:
    headline, bluf = cs.verdict_texts(_row("3824a07b"))
    assert headline.startswith("US: post-midterm firings reach Wiles and Miller")
    assert bluf.startswith("Assessed that a change in the top US leadership job")
    # The BLUF opens on stasis; the headline reports the change.
    assert not cr.text_reports_change(bluf) and cr.text_reports_change(headline)


# ---------------------------------------------------------------------------
# 3. The stamp (READ_SLICE side), fake connection
# ---------------------------------------------------------------------------


class _Conn:
    """Answers LAST_EVIDENCE_SQL and SIGNAL_FETCHED_SQL from canned rows."""

    def __init__(self, evidence: list[dict[str, Any]], fetched: dict[str, datetime],
                 *, fail_fetched: bool = False) -> None:
        self.evidence, self.fetched, self.fail = evidence, fetched, fail_fetched
        self.calls: list[str] = []

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append(sql)
        if sql == cs.LAST_EVIDENCE_SQL:
            return list(self.evidence)
        if sql == cs.SIGNAL_FETCHED_SQL:
            if self.fail:
                raise RuntimeError("down")
            return [{"id": s, "fetched_at": self.fetched[s]} for s in params[0] if s in self.fetched]
        raise AssertionError(sql)


def _evidence_rows(*keys: str) -> list[dict[str, Any]]:
    out = []
    for k in keys:
        ev = _last_evidence(k)
        out.append({"analyst_id": REAL_READS[k]["analyst_id"],
                    "target_id": REAL_READS[k]["target_id"], **ev})
    return out


@pytest.mark.asyncio
async def test_stamp_keeps_the_change_read_and_stamps_the_stasis_read() -> None:
    change, stasis = _row("3824a07b"), _row("74b5cc8f")
    conn = _Conn(_evidence_rows("3824a07b", "74b5cc8f"),
                 {**_fetched("3824a07b"), **_fetched("74b5cc8f")})
    n = await cs.stamp_carried_state(conn, [change, stasis], now=_now("3824a07b"), windows=3.0)
    assert n == 1
    assert cs.CARRIED_ROW_KEY not in change
    assert stasis[cs.CARRIED_ROW_KEY]["cause"] == cs.CAUSE_NOT_RE_EVIDENCED
    # Two statements: the last evidence, then the fetch times of the change
    # read's citations only (the stasis read's are never asked for).
    assert conn.calls == [cs.LAST_EVIDENCE_SQL, cs.SIGNAL_FETCHED_SQL]


@pytest.mark.asyncio
async def test_a_failed_fetch_read_convicts_as_before() -> None:
    change = _row("3824a07b")
    conn = _Conn(_evidence_rows("3824a07b"), _fetched("3824a07b"), fail_fetched=True)
    assert await cs.stamp_carried_state(conn, [change], now=_now("3824a07b"), windows=3.0) == 1
    assert change[cs.CARRIED_ROW_KEY]["cause"] == cs.CAUSE_NOT_RE_EVIDENCED


@pytest.mark.asyncio
async def test_no_fetch_query_when_no_convicted_read_reports_a_change() -> None:
    stasis = _row("dd2bb0dd")
    conn = _Conn(_evidence_rows("dd2bb0dd"), _fetched("dd2bb0dd"))
    assert await cs.stamp_carried_state(conn, [stasis], now=_now("dd2bb0dd"), windows=3.0) == 1
    assert conn.calls == [cs.LAST_EVIDENCE_SQL]


# ---------------------------------------------------------------------------
# 4. The absence route's reads on the REAL driver
# ---------------------------------------------------------------------------

_UNIT = "rc65_change_fixture_unit"
_TARGET = "country_rc65_change_fixture"
_OWNER = "test_rc65_change_report"
_SOURCE = "source.rc65_change_fixture"


async def _seed_signal(conn, *, fetched_at: datetime) -> str:
    sid = uuid4()
    await conn.execute(
        "INSERT INTO public.signals (id, source_id, fetched_at, payload, content_hash) "
        "VALUES ($1, $2, $3, '{}'::jsonb, $4)",
        sid, _SOURCE, fetched_at, f"rc65-{sid}",
    )
    return str(sid)


async def _seed_read(conn, key: str, *, produced_at: datetime, signal_ids: list[str],
                     indicator: str = "not_observed") -> str:
    r = REAL_READS[key]
    rid = uuid4()
    env = {
        "title": r["title"],
        "body": f"**BLUF:** {r['bluf']}",
        "tags": [f"severity:{r['severity']}"],
        "data": {
            "indicators": [{"id": "tripwire-0", "status": indicator, "citations": []}],
            "citations": [{"marker": f"[{i + 1}]", "signal_id": s} for i, s in enumerate(signal_ids)],
        },
    }
    await conn.execute(
        "INSERT INTO public.analyst_outputs "
        "  (id, kind, title, body, confidence, severity, data, target_id, analyst_id, "
        "   produced_at, schema_uri) "
        "VALUES ($1, 'finding', $2, $3, 0.6, $4, $5::jsonb, $6, $7, $8, "
        "        'iglu:legba/finding/jsonschema/1-0-0')",
        rid, r["title"], env["body"], r["severity"], json.dumps(env), _TARGET, _UNIT, produced_at,
    )
    return str(rid)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_route_reads_keep_a_change_read_on_the_real_driver(migrated_pg) -> None:
    from legba.data.postgres import PostgresStore

    store = PostgresStore(migrated_pg)
    await store.connect()
    now = datetime.now(timezone.utc)

    async def _clean(conn) -> None:
        await conn.execute("DELETE FROM public.analyst_outputs WHERE target_id = $1", _TARGET)
        await conn.execute("DELETE FROM public.analyst_descriptors WHERE owner = $1", _OWNER)
        await conn.execute("DELETE FROM public.signals WHERE source_id = $1", _SOURCE)

    try:
        async with store.acquire() as conn:
            await _clean(conn)
            await conn.execute(
                "INSERT INTO public.analyst_descriptors "
                "  (descriptor_id, version, schema_uri, is_head, kind, state, owner, name, body) "
                "VALUES ($1, 'rc65v1', 'legba/analyst/2.0.0', TRUE, 'inline_target', "
                "        'active', $2, $1, $3::jsonb)",
                _UNIT, _OWNER, json.dumps({"cadence": {"cooldown_seconds": 39600}}),
            )
            await _seed_read(conn, "3824a07b", produced_at=now - timedelta(days=14),
                             signal_ids=[], indicator="triggered")
            stale = [await _seed_signal(conn, fetched_at=now - timedelta(days=5)) for _ in range(4)]
            await _seed_read(conn, "3824a07b", produced_at=now - timedelta(hours=2), signal_ids=stale)
            # A change read citing only stale wire items: convicted.
            verdicts = await cs.unit_carried_verdicts(conn, target=_TARGET, units=[_UNIT], now=now)
            assert len(verdicts) == 1 and verdicts[0]["cause"] == cs.CAUSE_NOT_RE_EVIDENCED

            fresh = [await _seed_signal(conn, fetched_at=now - timedelta(hours=h)) for h in (3, 5, 9)]
            await _seed_read(conn, "3824a07b", produced_at=now - timedelta(hours=1), signal_ids=fresh)
            # The newest head reports a change and cites three fresh items: kept.
            assert await cs.unit_carried_verdicts(conn, target=_TARGET, units=[_UNIT], now=now) == []

            await _seed_read(conn, "dd2bb0dd", produced_at=now - timedelta(minutes=30), signal_ids=fresh)
            # A stasis head over the same fresh items: convicted again.
            verdicts = await cs.unit_carried_verdicts(conn, target=_TARGET, units=[_UNIT], now=now)
            assert len(verdicts) == 1
            assert UUID(verdicts[0]["head_id"])
    finally:
        async with store.acquire() as conn:
            await _clean(conn)
        await store.close()
