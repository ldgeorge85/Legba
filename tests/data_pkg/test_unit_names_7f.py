"""AMENDMENT 7f — THE UNIT'S HUMAN NAME reaches the voice and the judge.

THE DEFECT, from the live replay (``planning/ASSESSMENT_TENSIONS_IN_MAP_REPORT``
§5.1). The spine carried ``target_name == target_id`` on every block and every
drop-ledger row of every live record, so the only thing naming a unit anywhere
in front of the voice was its machine handle. Handed ``country_watch_kp`` — North
Korea — the core model wrote **"Pakistan"** (``pk``) three times across two arms,
and the judge failed every one of those sentences, correctly: the country the
voice named was nowhere in the evidence map, and neither was any other name.

That was 3 of the 6 tension-relay sentences, the single dominant cause of the
relay's 0-for-6, and it could not be repaired inside the Assessment channel,
whose fence is the payload: THE NAME WAS NEVER ON THE PAYLOAD.

What this file proves:

  * the name reaches BOTH readers — the rendered body the voice writes from and
    the evidence map the judge grades against;
  * the SLUG survives beside it everywhere, because every deterministic matcher
    (the roster fence, the ledger-grounding exemption, the ``[[ref:N]]`` map)
    joins on the handle and would go blind without it;
  * the mistranslation becomes CHECKABLE — the record now states the name the
    voice got wrong, in the same line;
  * a unit with no descriptor name falls back to its slug and nothing raises;
  * and everything else on the payload is BYTE-IDENTICAL, so this is a naming
    change and not a quiet rewrite of the record.
"""

from __future__ import annotations

import copy
from uuid import UUID

import pytest

from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import assembly_render as ar
from legba.data.analysts import assessment_prompts as apr
from legba.data.analysts import assessment_unsupported as au
from legba.data.analysts import composition_slice as cs
from legba.data.analysts import unit_names as un

#: The live 09-07 pairing, kept verbatim: ``kp`` is the unit the record declared
#: and ``pk`` is the country the voice wrote instead. Both are on the roster, so
#: the fixture reproduces the confusion the fix has to make visible.
KP = "country_watch_kp"
PK = "country_watch_pk"

#: The names as ``target_descriptors.name`` actually holds them (checked live
#: 2026-09-08). The em-dash prefix is the descriptor's own, not a test flourish.
LIVE_NAMES = {
    "country_g20_ru": "G20 — Russian Federation",
    "country_g20_cn": "G20 — China",
    KP: "Watch — Korea, Democratic People's Republic of",
    PK: "Watch — Pakistan",
}

#: A unit that is REAL and on the roster but whose descriptor carries no name.
#: It must degrade to the slug rather than to a bare parenthesis or a crash.
UNNAMED = "country_watch_zz"

BLUF = "*As of 2026-09-03.*\n\n**BLUF:** {bluf} [1].\n"

#: PINNED, not ``uuid4()``: the byte-identity proofs below build the payload
#: TWICE — once with names and once without — and diff the two leaf by leaf.
#: A fresh id per build would move ``finding_id`` and ``origin.head_id`` in
#: every arm and the diff would be about UUIDs rather than about names.
IDS = {
    "country_g20_ru": UUID("11111111-1111-4111-8111-111111111111"),
    KP: UUID("22222222-2222-4222-8222-222222222222"),
    "country_g20_cn": UUID("33333333-3333-4333-8333-333333333333"),
}


def _row(uid, *, target_id, bluf, sigs=("s1",)):
    return {
        "id": uid,
        "kind": "finding",
        "analyst_id": "country_composition",
        "target_id": target_id,
        "title": f"head {uid}",
        "body": BLUF.format(bluf=bluf),
        "severity": "high",
        "confidence": 0.7,
        "produced_at": "2026-09-03T10:01:00+00:00",
        "faithfulness_score": 0.9,
        "effective_confidence": 0.8,
        "claim_verdicts": [{"verdict": "supported"}],
        "derived_from": [],
        "data": {"data": {"citations": [
            {"marker": f"[{i}]", "signal_id": s, "source_id": f"source.{s}",
             "title": f"signal {s}", "source": f"https://example.test/{s}"}
            for i, s in enumerate(sigs, start=1)
        ]}},
    }


def _payload(*, names):
    """The live world shape: a roster, two carried blocks that conflict, and a
    third below the cut that conflicts with the lead.

    Built by ``build_assembly`` itself — the real ``_tensions`` walk over real
    BLUF spans — so both tension populations come from the detector rather than
    from a hand-written dict, and the naming is exercised on the producer's own
    output.
    """
    rows = [
        _row(IDS["country_g20_ru"], target_id="country_g20_ru",
             bluf="Russia's energy-security pressure is pushing escalation "
                  "risk upward with a sharp rise in refinery outages"),
        _row(IDS[KP], target_id=KP, sigs=("s2",),
             bluf="the peninsula's energy-security pressure is easing with a "
                  "marked drop in supply disruptions"),
        _row(IDS["country_g20_cn"], target_id="country_g20_cn", sigs=("s3",),
             bluf="China's energy-security pressure remains high with no sign "
                  "of easing"),
    ]
    ap.attach_cited_salience(rows, {"s1": 0.95, "s2": 0.70, "s3": 0.50})
    ordered = sorted(rows, key=ap.order_key)
    roster = ["country_g20_ru", "country_g20_cn", KP, PK, UNNAMED]
    return ap.build_assembly(
        tier=ap.TIER_WORLD,
        as_of="2026-09-03T12:00:00+00:00",
        candidates=ordered,
        carried=ordered[:2],
        coverage=[{"unit": u, "status": "in_basis"} for u in roster],
        coverage_roster=roster,
        target_names=names,
    )


@pytest.fixture
def named():
    return _payload(names=LIVE_NAMES)


@pytest.fixture
def unnamed():
    """The shipped arm: no map passed, which is what every caller did."""
    return _payload(names=None)


# ---------------------------------------------------------------------------
# THE FIXTURE IS ONLY WORTH ANYTHING IF THE DETECTOR BUILT IT
# ---------------------------------------------------------------------------


def test_the_fixture_carries_two_tensions_and_a_roster(named) -> None:
    assert len(named["tensions"]) >= 2, named["tensions"]
    kinds = {t["kind"] for t in named["tensions"]}
    assert kinds == {"carried_pair", "carried_vs_dropped"}, kinds
    assert len(named["coverage_roster"]) == 5
    assert named["tension_checked"]["pairs_found_carried"] >= 1
    assert named["tension_checked"]["pairs_found_uncarried"] >= 1


# ---------------------------------------------------------------------------
# THE NAME REACHES BOTH READERS — AND THE SLUG SURVIVES BESIDE IT
# ---------------------------------------------------------------------------


def test_the_name_and_the_slug_both_reach_the_rendered_body(named) -> None:
    """THE VOICE'S READER. It writes English from this and nothing else."""
    body = ar.render_assembly_body(named)
    assert LIVE_NAMES[KP] in body, (
        "the unit the voice mistranslated is not named in the body it writes "
        "from; that is the whole defect"
    )
    assert KP in body, (
        "the slug must ride beside the name — the roster fence and the "
        "ledger-grounding exemption join on the handle, not on the prose"
    )
    assert f"{LIVE_NAMES[KP]} ({KP})" in body


def test_the_name_and_the_slug_both_reach_the_evidence_map(named) -> None:
    """THE JUDGE'S READER. ``record_arithmetic`` rides in every citation's
    ``evidence_text``, so this is the text a graded sentence is checked against."""
    arithmetic = apr.record_arithmetic(named)
    assert f"{LIVE_NAMES[KP]} ({KP})" in arithmetic
    assert f"{LIVE_NAMES[PK]} ({PK})" in arithmetic, (
        "the roster names every declared unit, so the country the voice "
        "reached for must be in the map too — that is what makes the "
        "mistranslation checkable rather than merely wrong"
    )


def test_the_declared_roster_is_rendered_by_name_and_by_handle(named) -> None:
    block = apr.declared_aperture(named)
    for unit in named["coverage_roster"]:
        assert unit in block, f"the fence marks against {unit}; the map must print it"
    for slug, name in LIVE_NAMES.items():
        if slug in named["coverage_roster"]:
            assert f"{name} ({slug})" in block


def test_the_tension_names_the_uncarried_side_the_voice_got_wrong(named) -> None:
    """THE 09-07 SENTENCE, as an assertion.

    The uncarried side was named ``country_watch_kp`` and nothing else. The
    statement now carries the name, so a voice writing "Pakistan" contradicts a
    string the judge can find.
    """
    uncarried = [t for t in named["tensions"] if not t["b_carried"]]
    assert uncarried
    for t in uncarried:
        assert t["b_ref"]["target_name"] == LIVE_NAMES[t["b_ref"]["target_id"]]
        assert LIVE_NAMES[t["b_ref"]["target_id"]] in t["statement"]
        assert t["b_ref"]["target_id"] in t["statement"]
    rendered = apr.declared_tensions(named)
    assert f"{LIVE_NAMES[KP]} ({KP})" in rendered


def test_the_drop_ledger_row_stops_repeating_the_slug_twice(named) -> None:
    """``target_name`` was ``str(tid)`` — the slug, in the field whose name says
    it is not one — on 50 of 50 live drop rows."""
    rows = [r for grain in ("shown_not_carried", "not_selected", "trimmed",
                            "below_floor") for r in named["drops"][grain]]
    assert rows
    named_rows = [r for r in rows if r["target_id"] in LIVE_NAMES]
    assert named_rows
    for r in named_rows:
        assert r["target_name"] == LIVE_NAMES[r["target_id"]]
        assert r["target_name"] != r["target_id"]


def test_the_published_map_is_scoped_to_the_units_the_record_mentions(
    named,
) -> None:
    published = named[un.PAYLOAD_NAMES_KEY]
    assert published == {
        k: v for k, v in sorted(LIVE_NAMES.items())
        if k in named["coverage_roster"]
    }
    assert UNNAMED not in published, (
        "a unit with no name must not be stored as its own slug — that would "
        "make 'resolved to itself' indistinguishable from 'never resolved'"
    )


def test_the_deterministic_marker_now_grounds_a_sentence_on_the_human_name(
    named,
) -> None:
    """``aperture_unit_identifiers``' own docstring names this defect: "the
    ledger carries the SLUG and never the human name, so 'Russia' resolved
    against nothing". It resolves now."""
    vocabulary = au.aperture_vocabulary(named)
    assert "korea" in vocabulary or "korea's" in vocabulary, sorted(vocabulary)[:40]
    assert "pakistan" in vocabulary


# ---------------------------------------------------------------------------
# THE FALLBACK — A UNIT WITH NO NAME IS STILL A UNIT
# ---------------------------------------------------------------------------


def test_a_unit_with_no_descriptor_name_falls_back_to_its_slug(named) -> None:
    """No crash, no empty parenthesis, no invented name — the handle, alone."""
    assert un.unit_label(None, UNNAMED) == UNNAMED
    assert un.unit_label("", UNNAMED) == UNNAMED
    assert un.unit_label(UNNAMED, UNNAMED) == UNNAMED
    assert un.name_of(LIVE_NAMES, UNNAMED) == UNNAMED
    block = apr.declared_aperture(named)
    assert f"{UNNAMED} (" not in block
    assert UNNAMED in block


@pytest.mark.parametrize("name,slug,expected", [
    (None, None, ""),
    ("Watch — Pakistan", None, "Watch — Pakistan"),
    (None, PK, PK),
    ("Watch — Pakistan", PK, f"Watch — Pakistan ({PK})"),
    ("  Watch — Pakistan  ", f"  {PK} ", f"Watch — Pakistan ({PK})"),
])
def test_the_label_degrades_in_both_directions(name, slug, expected) -> None:
    assert un.unit_label(name, slug) == expected


def test_nothing_raises_on_a_payload_that_carries_no_map(unnamed) -> None:
    """Every pre-7f row on the substrate is this shape."""
    assert un.payload_names(unnamed) == {}
    assert ar.render_assembly_body(unnamed)
    assert apr.record_arithmetic(unnamed)
    assert apr.build_assessment_prompt(unnamed)


# ---------------------------------------------------------------------------
# BYTE-IDENTITY — THIS IS A NAMING CHANGE, NOT A REWRITE
# ---------------------------------------------------------------------------

#: The only leaves permitted to move between the arms. ``target_name`` is the
#: field this amendment exists to fill; ``statement`` is a rendered tension
#: sentence and names its two units; the map is additive.
MOVABLE = {"target_name", "statement", un.PAYLOAD_NAMES_KEY}


def _leaves(node, path=""):
    if isinstance(node, dict):
        for k in sorted(node):
            yield from _leaves(node[k], f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _leaves(v, f"{path}[{i}]")
    else:
        yield path, node


def test_every_field_but_the_names_is_byte_identical(named, unnamed) -> None:
    """The proof that matters: walk both payloads leaf by leaf and require that
    nothing outside :data:`MOVABLE` moved."""
    a = dict(_leaves(unnamed))
    b = dict(_leaves(named))
    assert set(a) | {f".{un.PAYLOAD_NAMES_KEY}.{k}" for k in LIVE_NAMES} >= set(a)
    moved = [
        p for p in set(a) & set(b)
        if a[p] != b[p] and p.rsplit(".", 1)[-1] not in MOVABLE
    ]
    assert moved == [], f"fields moved that are not names: {moved[:8]}"
    # And the arms must differ SOMEWHERE, or the test proves nothing.
    assert any(a[p] != b[p] for p in set(a) & set(b))


def test_stripping_the_names_gives_back_the_shipped_payload(named, unnamed) -> None:
    """The strong form: put the slugs back and the two arms are one object."""
    stripped = copy.deepcopy(named)
    # `{}`, not popped: the key is ADDITIVE and always present, and the
    # shipped arm publishes an empty map rather than omitting it.
    stripped[un.PAYLOAD_NAMES_KEY] = {}
    for block in stripped["blocks"]:
        block["target_name"] = block["target_id"]
    for grain in ("shown_not_carried", "not_selected", "trimmed", "below_floor"):
        for r in stripped["drops"][grain]:
            r["target_name"] = r["target_id"]
    for t, orig in zip(stripped["tensions"], unnamed["tensions"]):
        if t["b_ref"] is not None:
            t["b_ref"]["target_name"] = unnamed["tensions"][
                stripped["tensions"].index(t)
            ]["b_ref"]["target_name"]
        t["statement"] = orig["statement"]
    assert stripped == unnamed


def test_the_roster_render_stays_inside_its_re_decided_budget(named) -> None:
    """The ceiling is a TEST, not a slice (``APERTURE_ROSTER_BUDGET_CHARS``),
    and this amendment is that test doing its job: it fired at 1,230 chars on
    the live 33-unit roster and the decision was made again with the number in
    hand rather than truncated back into a guess."""
    rendered = apr._named_units([
        un.unit_label(LIVE_NAMES.get(u), u) for u in named["coverage_roster"]
    ])
    assert len(rendered) <= apr.APERTURE_ROSTER_BUDGET_CHARS


# ---------------------------------------------------------------------------
# THE RESOLVER — ONE READ, THE COLUMN THE ROLLUP ALREADY USES
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return [r for r in self.rows if r["descriptor_id"] in set(args[0])]


@pytest.mark.asyncio
async def test_the_resolver_reads_the_name_column_once() -> None:
    conn = _FakeConn([
        {"descriptor_id": KP, "name": LIVE_NAMES[KP]},
        {"descriptor_id": PK, "name": LIVE_NAMES[PK]},
        {"descriptor_id": UNNAMED, "name": None},
        {"descriptor_id": "country_g20_xx", "name": "country_g20_xx"},
    ])
    out = await cs.resolve_unit_names(
        conn, [KP, PK, UNNAMED, "country_g20_xx", KP, "", None]
    )
    assert out == {KP: LIVE_NAMES[KP], PK: LIVE_NAMES[PK]}
    assert len(conn.calls) == 1, "one read, never one per unit"
    sql, args = conn.calls[0]
    assert "target_descriptors" in sql and "is_head = TRUE" in sql
    assert args[0] == sorted({KP, PK, UNNAMED, "country_g20_xx"}), (
        "deduplicated and sorted, so two runs over one slice issue one query"
    )


@pytest.mark.asyncio
async def test_the_resolver_does_not_read_at_all_for_an_empty_roster() -> None:
    conn = _FakeConn([])
    assert await cs.resolve_unit_names(conn, []) == {}
    assert await cs.resolve_unit_names(conn, ["", None]) == {}
    assert conn.calls == []


def test_the_map_travels_on_the_slice_rows_the_world_roster_idiom() -> None:
    rows = [{"target_id": KP}, {"target_id": PK}]
    assert cs.unit_names_of(rows) == {}
    cs.stamp_unit_names(rows, {KP: LIVE_NAMES[KP]})
    assert all(r[cs.UNIT_NAMES_ROW_KEY] == {KP: LIVE_NAMES[KP]} for r in rows)
    assert cs.unit_names_of(rows) == {KP: LIVE_NAMES[KP]}
