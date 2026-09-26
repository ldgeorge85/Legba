"""D-5 — THE CASCADE. `planning/DEMOTION_D1_SPEC_2026-09-04.md` §4.

What this file proves, in the spec's own order:

  * §4.1 — the region rollup: fields, the roster diff that NAMES a member it
    could not see, blocks CARRIED (never re-quoted, byte-identical to the
    member assembly's own object), a deterministic title/body/confidence, and
    the arithmetic declared for deterministic re-derivation.
  * §4.2 — THE TRAP. The world path is STRUCTURALLY incapable of depending on a
    region faithfulness critique, because it no longer reads a region row. The
    test that matters is not "the world still works" but "the world's basis
    gather is never issued against ``region_composition``".
  * §4.3 — the compat census, item by item, as assertions rather than prose:
    ``meta`` preserved, the ``composition:`` supersession signature preserved,
    ``derived_from`` preserved, no ``unstructured``/``coerce_failed`` tag, the
    verify plane's exemption, the auditor's regime predicate, the frame gauge's
    regime predicate, and the UI verdict mirror in lockstep with ``kinds.py``.
  * §5.2 — flag OFF is byte-identical. Every legacy assertion in the 24 region-
    touching test files passes UNEDITED, which is the real proof; what is here
    is the narrow one this train owns.

ONE FLAG, not two (operator ruling). ``LEGBA_REGION_ROLLUP`` does not exist:
the cascade rides ``LEGBA_COMPOSITION_ASSEMBLY``, and the two-term predicate in
``composition_slice.world_reads_countries`` is what makes a PARTIAL rollout
safe in both orders.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import composition_slice as cs
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts import region_rollup as rr
from legba.data.provenance import kinds
from legba.data.provenance.composition_integrity import (
    has_collection_denominator_scope,
)
from legba.data.provenance.structural_claims import verify_structural_claims

REPO_ROOT = Path(__file__).resolve().parents[2]

# A real desk BLUF, U+2011 and curly quotes included — the fixtures are cut from
# the same 2026-09-03 head D-2's tests use, because a synthetic ASCII body
# cannot fail the way the live ones do.
DESK_BODY = (
    "*As of 2026-09-03; slice covers the trailing 72h to that date.*\n"
    "\n"
    "**BLUF:** A coordinated escalation narrative is emerging that links recent "
    "U.S. strikes on Iran—including the deadly wedding attack—to Iranian "
    "retaliatory messaging and President Donald Trump’s hard‑line threats.\n"
)


def _country_assembly_row(
    target_id: str,
    *,
    uid: str | None = None,
    desk: str = "narrative_coordination",
    severity: str | None = "high",
    produced_at: str = "2026-09-03T10:01:00+00:00",
    cited_mass: float = 3.85,
    text: str = "A coordinated escalation narrative is emerging.",
    drops: int = 2,
    lead_ordinal: int | None = 1,
    regime: str = ap.REGIME_ASSEMBLY,
    blocks: bool = True,
) -> dict:
    """A ``country_composition`` head carrying an ``assembly.v1`` payload — the
    rollup's only real input shape."""
    head_id = uid or str(uuid4())
    block = {
        "ordinal": 1,
        "finding_id": f"desk-head-{target_id}",
        "desk": desk,
        "target_id": target_id,
        "target_name": target_id,
        "question": desk.replace("_", " ").capitalize(),
        "produced_at": produced_at,
        "severity": severity,
        "salience": {"cited_mass": cited_mass, "source": "cited_signals"},
        "spans": [{
            "text": text,
            "role": "bluf",
            "origin": {"head_id": f"desk-head-{target_id}", "desk": desk},
        }],
        "signals": [],
        "corroboration": {},
        "verify": {"overall_score": 0.9},
    }
    assembly: dict = {"schema": ap.ASSEMBLY_SCHEMA, "regime": regime}
    if blocks:
        assembly.update({
            "tier": ap.TIER_COUNTRY,
            "blocks": [block],
            "lead": {
                "kind": (
                    ap.LEAD_EARNED_SINGLE if lead_ordinal else ap.LEAD_NONE
                ),
                "block_ordinals": [lead_ordinal] if lead_ordinal else [],
            },
            "drops": {"counts": {"shown_not_carried": drops}},
        })
    return {
        "id": head_id,
        "analyst_id": cs.COUNTRY_COMPOSITION_ANALYST_ID,
        "target_id": target_id,
        "title": f"{target_id} read",
        "body": DESK_BODY,
        "severity": severity,
        "produced_at": produced_at,
        "data": {"data": {"assembly": assembly, "meta": True}},
    }


MEMBERSHIP = {
    "region_id": "region_mena",
    "region_name": "Region — MENA",
    "members": [
        {"target_id": "country_watch_ir", "target_name": "Iran"},
        {"target_id": "country_watch_il", "target_name": "Israel"},
        {"target_id": "country_watch_ye", "target_name": "Yemen"},
    ],
}


def _rollup(rows, *, membership=MEMBERSHIP, horizon=None):
    for r in rows:
        r[cs.REGION_MEMBERSHIP_ROW_KEY] = membership
    return rr.build_region_rollup(
        as_of="2026-09-03T12:00:00+00:00",
        **rr.rollup_inputs(
            rows, region_id=membership["region_id"], horizon_hours=horizon
        ),
    )


# ---------------------------------------------------------------------------
# §4.1 — WHAT THE ROLLUP IS
# ---------------------------------------------------------------------------


def test_rollup_carries_member_blocks_byte_identically_and_never_requotes():
    """The load-bearing property of the whole tier.

    §4.1: *"The rollup carries block objects; it does not re-quote."* If this
    ever stops holding, the region tier has silently re-acquired a way to be
    unfaithful — which is the one thing the retirement was for. The identity is
    asserted on the OBJECT, not on a normalised comparison, because a fold that
    passes is not the same claim as a byte match.
    """
    src = _country_assembly_row("country_watch_ir")
    payload = _rollup([src])
    origin_block = src["data"]["data"]["assembly"]["blocks"][0]
    assert payload["leads"] == [origin_block]
    assert payload["leads"][0]["spans"][0]["text"] == origin_block["spans"][0]["text"]
    # And the carried block still points at the DESK HEAD, not at the country
    # read it travelled through — the depth-1 property §4.1 rests on.
    assert payload["leads"][0]["spans"][0]["origin"]["head_id"] == "desk-head-country_watch_ir"
    assert payload["leads"][0]["finding_id"] == "desk-head-country_watch_ir"


def test_rollup_names_the_member_it_could_not_see():
    """The roster is the DENOMINATOR, so absence is a row rather than a silence."""
    rows = [
        _country_assembly_row("country_watch_ir"),
        _country_assembly_row("country_watch_il"),
    ]
    payload = _rollup(rows)
    assert payload["member_count"] == 3
    assert payload["members_with_head"] == 2
    assert payload["members_carried"] == 2
    assert payload["members_missing"] == ["country_watch_ye"]
    cov = {c["target_id"]: c for c in payload["coverage"]}
    assert cov["country_watch_ir"]["status"] == rr.MEMBER_IN_BASIS
    assert cov["country_watch_ye"]["status"] == rr.MEMBER_NO_ASSEMBLY
    assert cov["country_watch_ye"]["why"] == "no_head_in_horizon"
    assert cov["country_watch_ye"]["has_head"] is False
    # And the member entry keeps its NAME, so the body can say "Yemen" and not
    # "country_watch_ye" — an id in a product sentence is a leak, not a fact.
    missing = [m for m in payload["members"] if m["target_id"] == "country_watch_ye"][0]
    assert missing["target_name"] == "Yemen"
    assert missing["lead_source"] == rr.LEAD_NONE


def test_a_legacy_regime_member_is_no_assembly_not_a_fabricated_lead():
    """A member head with no ``assembly.blocks`` has nothing to carry.

    The rollup could construct a span from that row's prose — and refuses,
    because that is a QUOTING act at the tier that just retired quoting, and its
    origin would be the country read rather than a desk head. It says
    ``no_assembly`` and carries nothing. The distinction from "no head at all"
    is on the entry (``has_head``), so the two absences never pool.
    """
    rows = [
        _country_assembly_row("country_watch_ir"),
        _country_assembly_row(
            "country_watch_il", regime=ap.REGIME_LEGACY, blocks=False
        ),
    ]
    payload = _rollup(rows)
    assert payload["members_with_head"] == 2
    assert payload["members_carried"] == 1
    cov = {c["target_id"]: c for c in payload["coverage"]}
    assert cov["country_watch_il"]["status"] == rr.MEMBER_NO_ASSEMBLY
    assert cov["country_watch_il"]["has_head"] is True
    assert cov["country_watch_il"]["why"] == "legacy_regime_head"
    assert len(payload["leads"]) == 1


def test_rollup_severity_is_the_max_over_carried_members_only():
    rows = [
        _country_assembly_row("country_watch_ir", severity="moderate"),
        _country_assembly_row("country_watch_il", severity="critical"),
    ]
    assert rr.rollup_severity(_rollup(rows)) == "critical"
    # A member the rollup could NOT carry does not contribute its severity: the
    # read's severity describes what the read contains, not what exists.
    rows2 = [
        _country_assembly_row("country_watch_ir", severity="low"),
        _country_assembly_row(
            "country_watch_il", severity="critical", regime=ap.REGIME_LEGACY,
            blocks=False,
        ),
    ]
    assert rr.rollup_severity(_rollup(rows2)) == "low"


def test_severity_reaches_the_read_column_through_the_tag():
    """Region rows carry NULL severity on 100% of today's population. The tag is
    how the write path lifts it into ``analyst_outputs.severity``."""
    payload = _rollup([_country_assembly_row("country_watch_ir", severity="high")])
    assert "severity:high" in rr.rollup_tags(payload)
    assert f"regime:{ap.REGIME_ROLLUP}" in rr.rollup_tags(payload)
    assert "incomplete_roster" in rr.rollup_tags(payload)


def test_confidence_is_the_carried_fraction_not_a_flat_one():
    """A flat 1.0 over a half-empty region is the quiet overclaim the demotion
    exists to stop."""
    rows = [_country_assembly_row("country_watch_ir")]
    assert rr.rollup_confidence(_rollup(rows)) == pytest.approx(1 / 3, abs=1e-4)
    full = [
        _country_assembly_row(t)
        for t in ("country_watch_ir", "country_watch_il", "country_watch_ye")
    ]
    assert rr.rollup_confidence(_rollup(full)) == 1.0
    # An empty roster is 0.0, never a division-by-zero dressed as certainty.
    empty = rr.build_region_rollup(
        region_id="region_x", region_name="X", member_ids=[], heads=[],
        as_of="2026-09-03T12:00:00+00:00",
    )
    assert rr.rollup_confidence(empty) == 0.0
    assert rr.rollup_severity(empty) is None
    assert empty["evidence_window"] == {"earliest": None, "latest": None}


def test_stale_is_carried_and_flagged_never_dropped():
    """FRAME-1 doctrine: an old read is admitted with its age STATED."""
    rows = [_country_assembly_row(
        "country_watch_ir", produced_at="2026-08-01T00:00:00+00:00"
    )]
    payload = _rollup(rows, horizon=24.0)
    cov = {c["target_id"]: c for c in payload["coverage"]}
    assert cov["country_watch_ir"]["status"] == rr.MEMBER_STALE
    assert len(payload["leads"]) == 1          # carried, not dropped
    assert cov["country_watch_ir"]["age_h"] > 24.0


def test_render_is_deterministic_and_quotes_the_carried_span_verbatim():
    rows = [
        _country_assembly_row("country_watch_ir", text="Iran signalled restraint."),
        _country_assembly_row("country_watch_il", text="Israel raised alert level."),
    ]
    payload = _rollup(rows)
    body = rr.render_rollup_body(payload)
    assert body == rr.render_rollup_body(payload)       # byte-stable
    assert "> Iran signalled restraint." in body
    assert "> Israel raised alert level." in body
    # ONE marker per carried member, numbered by the rollup's own ordering —
    # which is what makes |leads| == |citations| == |markers| true by
    # construction rather than by inspection.
    markers = sorted(set(re.findall(r"\[\[ref:(\d+)\]\]", body)))
    assert markers == ["1", "2"]
    # The member it could not see is NAMED in the body, not merely counted.
    assert "Yemen: no read inside the horizon." in body


def test_leads_pair_with_carried_members_by_position():
    """The payload invariant the render rests on, pinned.

    ``leads[i]`` is the block carried for the i-th CARRIED member. Joining on an
    id instead would be a latent empty-block bug: the member's ``assembly_id``
    is the COUNTRY ROW's id while the block's ``finding_id`` is the DESK HEAD's,
    and they are never equal.
    """
    rows = [
        _country_assembly_row("country_watch_ir", text="Iran signalled restraint."),
        _country_assembly_row("country_watch_ye", text="Yemen escalated."),
    ]
    payload = _rollup(rows)
    carried = [m for m in payload["members"] if m["lead_source"] == rr.LEAD_CARRIED]
    assert len(payload["leads"]) == payload["members_carried"] == len(carried)
    for member, block in zip(carried, payload["leads"]):
        assert block["target_id"] == member["target_id"]
    # The roster order drives BOTH lists, so Yemen follows Iran even though the
    # rows arrived in that order by accident rather than by rank.
    assert [m["target_id"] for m in carried] == ["country_watch_ir", "country_watch_ye"]


def test_title_is_a_count_and_never_crowns_a_member():
    """Crowning one member as the region's story is the interpretive act this
    tier gave up. Doing it in the ``<h1>`` while the body carries a flat list
    would be the demotion shipping honesty everywhere except the headline."""
    rows = [_country_assembly_row("country_watch_ir", text="Iran signalled restraint.")]
    payload = _rollup(rows)
    title = rr.rollup_title(payload)
    assert title == "Region — MENA, 2026-09-03 — 1 of 3 member country read carried"
    assert "Iran signalled restraint" not in title
    assert len(title) <= 200            # analyst_outputs.title is the page <h1>


def test_the_body_says_out_loud_that_nobody_wrote_it():
    payload = _rollup([_country_assembly_row("country_watch_ir")])
    body = rr.render_rollup_body(payload)
    assert "This is a deterministic rollup: no sentence below was written for it." in body


def test_connective_vocabulary_is_closed_over_the_render():
    """Same mechanism as the assembly's: no connective may assert a state of the
    world, and a new one cannot arrive without a deliberate line in the diff."""
    rows = [
        _country_assembly_row("country_watch_ir"),
        _country_assembly_row("country_watch_il", regime=ap.REGIME_LEGACY, blocks=False),
    ]
    body = rr.render_rollup_body(_rollup(rows))
    quoted = {"A coordinated escalation narrative is emerging."}
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("*", ">", "###", "<!--", "[[ref:")):
            continue
        if any(c in stripped for c in rr.ROLLUP_CONNECTIVES):
            continue
        # Everything else must be a member NAME line or a quoted span.
        assert any(
            n in stripped
            for n in ("Iran", "Israel", "Yemen", "region")
        ) or stripped in quoted, f"unvocabularised connective: {stripped!r}"


# ---------------------------------------------------------------------------
# §4.1 — THE REPLACEMENT VERIFY (the arithmetic is re-derived, not judged)
# ---------------------------------------------------------------------------


def test_declared_arithmetic_re_derives_supported():
    rows = [
        _country_assembly_row("country_watch_ir"),
        _country_assembly_row("country_watch_il"),
    ]
    payload = _rollup(rows)
    claims = rr.rollup_structural_claims(payload)
    report = verify_structural_claims(
        data={"structural_claims": claims},
        derived_from=[m["assembly_id"] for m in payload["members"]
                      if m["lead_source"] == rr.LEAD_CARRIED],
    )
    assert report.had_claims is True
    verdicts = {v.claim_id: v.verdict for v in report.claim_verdicts}
    assert set(verdicts.values()) == {kinds_supported()}, verdicts
    assert any(c["basis"] == "@derived_from" for c in claims)


def kinds_supported() -> str:
    from legba.data.provenance.structural_claims import STRUCTURAL_SUPPORTED

    return STRUCTURAL_SUPPORTED


def test_a_rollup_that_misstates_its_own_membership_is_caught():
    """The point of a re-derivation: it cannot be wrong about being wrong."""
    from legba.data.provenance.structural_claims import STRUCTURAL_MISCOUNT

    payload = _rollup([_country_assembly_row("country_watch_ir")])
    payload["member_count"] = 99                    # the lie
    claims = rr.rollup_structural_claims(payload)
    report = verify_structural_claims(
        data={"structural_claims": claims}, derived_from=[]
    )
    verdicts = {v.claim_id: v.verdict for v in report.claim_verdicts}
    assert any(v == STRUCTURAL_MISCOUNT for v in verdicts.values())


def test_the_faithfulness_pass_no_ops_on_a_rollup_row():
    """§4.1 — there is no prose to grade, so no verdict is produced and none is
    faked. The guard keys on the ROW, so it is regime-stable rather than
    flag-dependent: a rollup written today is still a rollup after a rollback."""
    assert kinds.is_deterministic_rollup(
        {"data": {"rollup": {"schema": kinds.ROLLUP_PAYLOAD_SCHEMA}}}
    )
    assert not kinds.is_deterministic_rollup(
        {"data": {"assembly": {"regime": "legacy"}}}
    )
    assert not kinds.is_deterministic_rollup(None)


def test_structural_claims_opt_in_is_safe_without_a_flag_read(monkeypatch):
    """The C2b contract does the gating: an opted-in analyst whose finding
    carries no claims block is a NO-OP. So the legacy region read is untouched
    with the flag OFF and no environment is consulted anywhere in the path."""
    monkeypatch.delenv(ap.ASSEMBLY_ENV, raising=False)
    assert kinds.structural_claims_verify_opt_in("region_composition") is True
    assert verify_structural_claims(data={}, derived_from=[]).had_claims is False


def test_the_two_drift_guarded_sets_are_untouched():
    """§4.3 item 5 asked for ``region_composition`` in both. It cannot go in
    either without making a load-bearing drift guard assert something false —
    the exempt set is guarded EQUAL to the deterministic FINDING sub-handlers
    and the claims set SUBSET of it. The analyst gets its own registry."""
    assert "region_composition" not in kinds.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
    assert "region_composition" not in kinds.STRUCTURAL_CLAIMS_VERIFY_ANALYSTS
    assert "region_composition" in kinds.DETERMINISTIC_ROLLUP_ANALYSTS
    assert (
        kinds.STRUCTURAL_CLAIMS_VERIFY_ANALYSTS
        <= kinds.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
    )


def test_the_badge_is_regime_derived_and_never_analyst_derived():
    rollup_row = {"data": {"rollup": {"schema": kinds.ROLLUP_PAYLOAD_SCHEMA}}}
    legacy_row = {"data": {"assembly": {"regime": "legacy"}}}
    assert kinds.structural_badge("region_composition", None, rollup_row) == (
        kinds.ROLLUP_EXEMPT_REASON
    )
    assert kinds.structural_badge("region_composition", True, rollup_row) == (
        kinds.ROLLUP_VERIFIED_REASON
    )
    # A LEGACY generative region read keeps the ordinary semantics: no badge, so
    # it renders as the unverified/verified LLM read it actually is.
    assert kinds.structural_badge("region_composition", True, legacy_row) is None
    # And no other analyst can earn the badge by carrying a stray key.
    assert kinds.structural_badge("country_composition", True, rollup_row) is None
    # The default-arg call sites are untouched.
    assert kinds.structural_badge("graph_mining", True) == "structural-verified"


# ---------------------------------------------------------------------------
# §4.2 — THE TRAP, DISSOLVED STRUCTURALLY
# ---------------------------------------------------------------------------


class _RecordingConn:
    """Records every basis-gather call so the test can assert what was NOT read."""

    def __init__(self, roster, members, rows):
        self.roster, self.members, self.rows = roster, members, rows
        self.fetched: list[tuple] = []

    async def fetch(self, sql, *params):
        self.fetched.append((sql, params))
        if "?" in sql and "$1" in sql and params and params[0] == cs.REGION_FRAME_TAG:
            return self.roster
        if params:
            return self.members.get(str(params[0]), [])
        return []


@pytest.mark.asyncio
async def test_world_never_issues_a_basis_gather_against_region_composition(monkeypatch):
    """THE assertion this train exists for.

    Not "the world still produces a read" — a world read that silently degraded
    every region to country-fallback would also produce one, forever, with every
    test green. The claim is STRUCTURAL: under the regime the world's basis
    gather is never issued with ``region_composition`` in its analyst set, so
    the INNER faithfulness lateral cannot drop a rollup that has no critique.
    """
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    roster = [
        {"descriptor_id": "region_mena", "name": "Region — MENA"},
        {"descriptor_id": "region_africa", "name": "Region — Africa"},
    ]
    members = {
        "region_mena": [{"descriptor_id": "country_watch_ir"},
                        {"descriptor_id": "country_watch_il"}],
        "region_africa": [{"descriptor_id": "country_watch_sd"}],
    }
    conn = _RecordingConn(roster, members, [])
    calls: list[dict] = []

    async def _basis(_conn, **kw):
        calls.append(kw)
        return [
            _country_assembly_row("country_watch_ir"),
            _country_assembly_row("country_watch_il"),
            {
                "id": str(uuid4()), "analyst_id": "escalation_composition",
                "target_id": None, "title": "t", "body": DESK_BODY,
                "severity": "high", "produced_at": "2026-09-03T09:00:00+00:00",
                "data": {"data": {}},
            },
        ]

    rows = await cs._assemble_world_country_slice(
        conn,
        region_analyst_ids=[
            cs.REGION_COMPOSITION_ANALYST_ID, "escalation_composition"
        ],
        time_window_hours=336,
        limit=100,
        verify_floor=0.5,
        basis_reader=_basis,
    )
    for kw in calls:
        assert cs.REGION_COMPOSITION_ANALYST_ID not in kw["analyst_ids"], kw
    assert calls[0]["analyst_ids"] == [
        cs.COUNTRY_COMPOSITION_ANALYST_ID, "escalation_composition"
    ]
    # ONE gather covers both tiers (the DISTINCT ON fold keys a country row on
    # its target and a target-less thematic row on its analyst).
    assert len(calls) == 1
    coverage = rows[0]["_region_coverage"]
    modes = {c["region_id"]: c["mode"] for c in coverage}
    assert modes["region_mena"] == cs.REGION_MODE_COUNTRIES
    assert modes["region_africa"] == cs.REGION_MODE_GAP     # named, not missing
    assert modes["thematic:escalation_composition"] == cs.REGION_MODE_THEMATIC
    # The retired vocabulary is never emitted on this path.
    assert cs.REGION_MODE_REGION not in modes.values()
    assert cs.REGION_MODE_COUNTRY_FALLBACK not in modes.values()


@pytest.mark.asyncio
async def test_an_untagged_desk_is_carried_and_labelled_rather_than_hidden():
    """The old path read region heads, so a desk with no region tag was
    invisible to it. Reading the country roster makes it a candidate for the
    first time — and a silent presence is the same defect as a silent absence
    pointing the other way."""
    conn = _RecordingConn(
        [{"descriptor_id": "region_mena", "name": "Region — MENA"}],
        {"region_mena": [{"descriptor_id": "country_watch_ir"}]},
        [],
    )

    async def _basis(_conn, **kw):
        return [
            _country_assembly_row("country_watch_ir"),
            _country_assembly_row("lane_hormuz"),          # tagged into no frame
        ]

    rows = await cs._assemble_world_country_slice(
        conn, region_analyst_ids=[cs.REGION_COMPOSITION_ANALYST_ID],
        time_window_hours=336, limit=100, verify_floor=0.5, basis_reader=_basis,
    )
    modes = {c["region_id"]: c["mode"] for c in rows[0]["_region_coverage"]}
    assert modes[cs.UNASSIGNED_REGION_ID] == cs.REGION_MODE_UNASSIGNED
    assert any(r["target_id"] == "lane_hormuz" for r in rows)
    aperture = synth._render_world_aperture_block(rows[0]["_region_coverage"])
    assert "belong to no registered region frame" in aperture


def test_world_reads_countries_under_either_half_of_a_partial_rollout(monkeypatch):
    """The two-term predicate, and why one term is not enough.

    A REGION-only rollout is the dangerous one: region rows become rollups with
    no faithfulness critique while the world still reads them, which is exactly
    the state §4.2's trap lives in. A COUNTRY-only rollout is not — region heads
    are still generative and still carry critiques — so it deliberately keeps
    the legacy world read rather than changing a tier nobody asked to change.
    """
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "region_composition")
    assert cs.world_reads_countries(world_analyst_id="world_assessor") is True
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "world_assessor")
    assert cs.world_reads_countries(world_analyst_id="world_assessor") is True
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "country_composition")
    assert cs.world_reads_countries(world_analyst_id="world_assessor") is False
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    assert cs.world_reads_countries(world_analyst_id="world_assessor") is True
    monkeypatch.delenv(ap.ASSEMBLY_ENV, raising=False)
    assert cs.world_reads_countries(world_analyst_id="world_assessor") is False


def test_one_flag_not_two():
    """``LEGBA_REGION_ROLLUP`` does not exist anywhere in the tree. The operator
    ruled ONE regime switch, and a second env name is how a cascade gets
    half-flipped into the trap."""
    hits = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # A quoted STRING literal is the only form that can reach an env read.
        # The name appearing in prose (this train's own docstring explains why
        # the spec proposed it and the ruling collapsed it) is not a switch.
        if '"LEGBA_REGION_ROLLUP"' in text or "'LEGBA_REGION_ROLLUP'" in text:
            hits.append(str(path))
    assert hits == []
    assert rr.region_rollup_enabled.__module__ == "legba.data.analysts.region_rollup"


# ---------------------------------------------------------------------------
# §4.3 — THE COMPAT CENSUS, AS ASSERTIONS
# ---------------------------------------------------------------------------


def test_the_auditor_and_the_frame_gauge_both_exclude_rollup_rows():
    """Item 8, and one the spec's census missed.

    The auditor is live LLM spend over a row with nothing to extract. The frame
    gauge is worse than wasted: a rollup title is deterministic, so it shares
    its frame with every other rollup BY CONSTRUCTION and would report a
    permanent 100% frame lock — a true alarm about a fact that means nothing.
    Both use ``IS DISTINCT FROM`` because the JSONB path is NULL on every
    pre-D-2 row and ``NULL <> 'rollup'`` is NULL, not TRUE.
    """
    from legba.data.analysts.deterministic_handlers import composition_lineage_sweep
    from legba.data.analysts.deterministic_handlers import standing_auditor

    predicate = "(ao.data -> 'data' -> 'assembly' ->> 'regime') IS DISTINCT FROM 'rollup'"
    assert predicate in standing_auditor._DESK_HEADS_SQL
    assert predicate in composition_lineage_sweep._GAUGE_SQL
    # The analyst id STAYS in both rosters: a legacy generative region read is
    # still audited and still gauged, which is what makes this flag-gated.
    assert "region_composition" in standing_auditor.DESK_ANALYST_IDS
    assert "region_composition" in composition_lineage_sweep._GAUGE_ANALYSTS


def test_the_reverify_script_will_not_regrade_a_rollup():
    """Re-running the faithfulness judge over a rollup would MINT a faithfulness
    verdict for a row that never had one and is not entitled to one — the
    fabricated-critique move §4.2 refuses."""
    script = (REPO_ROOT / "scripts" / "reverify_composition_heads.py").read_text()
    assert "IS DISTINCT FROM 'rollup'" in script


def test_ui_verdict_mirror_is_in_lockstep_with_kinds():
    """§4.3 item 6, done the only way it can be checked from Python.

    The UI hand-duplicates the server registry because live-tail rows never pass
    through the reads-API projection, and a drift renders a rollup as an
    ordinary unverified LLM read. There is no vitest runner in this environment,
    so the lockstep is asserted HERE — against the suite that actually runs.
    """
    ts = (REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "verdictModel.ts").read_text()
    mirror = re.search(
        r"DETERMINISTIC_ROLLUP_ANALYSTS: ReadonlySet<string> = new Set\(\[(.*?)\]\)",
        ts, re.S,
    )
    assert mirror is not None, "the UI mirror set is missing"
    ids = set(re.findall(r"'([^']+)'", mirror.group(1)))
    assert ids == set(kinds.DETERMINISTIC_ROLLUP_ANALYSTS)
    assert f"export const ROLLUP_EXEMPT = '{kinds.ROLLUP_EXEMPT_REASON}'" in ts
    assert f"export const ROLLUP_VERIFIED = '{kinds.ROLLUP_VERIFIED_REASON}'" in ts
    assert f"ROLLUP_PAYLOAD_SCHEMA = '{kinds.ROLLUP_PAYLOAD_SCHEMA}'" in ts


def test_export_states_the_rollup_reason_instead_of_no_verdict_recorded():
    """Item 7. *"unverified — no faithfulness verdict recorded"* is TRUE and
    MISLEADING here: it reads as an oversight when it is a design."""
    from legba.data.registry.export_api import _finding_verify

    state, _ = _finding_verify(
        None, "region_composition",
        {"data": {"rollup": {"schema": kinds.ROLLUP_PAYLOAD_SCHEMA}}},
    )
    assert kinds.ROLLUP_EXEMPT_REASON in state
    assert "no faithfulness verdict recorded" not in state
    # A LEGACY region row keeps the ordinary grammar, unchanged.
    legacy, _ = _finding_verify(None, "region_composition", {"data": {}})
    assert "no faithfulness verdict recorded" in legacy


def test_feed_producers_keeps_region_composition():
    """Item 12 — the feed still shows the tier, and the rename temptation does
    not start here. A retired GENERATIVE path is not a retired PRODUCER."""
    ts = (REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "feedProducers.ts").read_text()
    assert "'region_composition'" in ts


def test_the_descriptor_keeps_its_verify_key():
    """Items 10 and the rollback lever, and this is a DELIBERATE deviation from
    the spec's §4.3.

    §4.3 item 10 asks D-5 to drop ``method.llm.verify`` from the region
    descriptor. Under the operator's ONE-FLAG ruling that cannot ride the flag:
    a descriptor edit takes effect with the flag OFF too, so it would stop the
    GENERATIVE region read being verified — an ungated behaviour change smuggled
    in beside a gated one. The row-keyed scope guard in
    ``actor_critic.verify_inline_target_finding`` achieves the same outcome
    under the flag and leaves §5.4's stated rollback lever ("the descriptor's
    ``method.llm.verify`` key is the only thing that must be restorable")
    untouched — it never has to be restored, because it never left.
    """
    y = (REPO_ROOT / "descriptors" / "analyst_region_composition.yaml").read_text()
    assert "verify:" in y
    assert "state: active" in y
    # Cadence untouched: the run still fires, still writes analyst_traces, and
    # so the cadence/production gauges see nothing anomalous.
    assert 'fallback_schedule: "45 11,23 * * *"' in y
    assert "cooldown_seconds: 39600" in y


def test_rollup_row_preserves_the_four_data_contracts(monkeypatch):
    """Items 1-4 of the census, on the shape the rollup arm actually produces."""
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    rows = [_country_assembly_row("country_watch_ir")]
    for r in rows:
        r[cs.REGION_MEMBERSHIP_ROW_KEY] = MEMBERSHIP
    payload, finding, steps = rr.assemble_region_rollup(
        rows, region_id="region_mena", horizon_hours=336,
        as_of="2026-09-03T12:00:00+00:00",
        coerce=synth._coerce_finding, contributing_analysts=["country_composition"],
    )
    # 4 — a rollup body is structured by construction.
    assert "unstructured" not in finding.tags
    assert "coerce_failed" not in finding.tags
    # No LLM ran, so the field whose NAME asserts one did is absent.
    assert "raw_llm_response" not in finding.data
    # The replacement verify is declared on the row.
    assert finding.data[rr.STRUCTURAL_CLAIMS_DATA_KEY]
    assert steps[0]["phase"] == "rollup"
    assert steps[0]["kind"] == rr.ROLLUP_SCHEMA
    assert steps[0]["carried"] == 1
    assert steps[0]["missing"] == 2
    # The regime stamp §5.2 mandates on EVERY composition row, third value.
    stamp = rr.rollup_assembly_stamp()
    assert stamp["regime"] == ap.REGIME_ROLLUP
    assert stamp["schema"] == ap.ASSEMBLY_SCHEMA
    assert kinds.is_deterministic_rollup({"rollup": payload})


def test_flag_off_leaves_both_cascade_switches_closed(monkeypatch):
    """§5.2 — the whole of this train is one env var away from not existing."""
    monkeypatch.delenv(ap.ASSEMBLY_ENV, raising=False)
    assert rr.region_rollup_enabled("region_composition") is False
    assert cs.world_reads_countries(world_analyst_id="world_assessor") is False


# ---------------------------------------------------------------------------
# THE REAL BINDING PATH — READ_SLICE and run_method, not the helpers
# ---------------------------------------------------------------------------


class _RegionSliceConn:
    """Routes the region branch's reads by SQL content: the member id resolver,
    the D-5 NAMED member roster, the frame's own name, and the slice."""

    def __init__(self, members, slice_rows):
        self.members, self.slice_rows = members, slice_rows
        self.queries = []

    async def fetch(self, query, *params):
        self.queries.append(query)
        if "target_descriptors" in query and "name" in query:
            return [{"descriptor_id": m["target_id"], "name": m["target_name"]}
                    for m in self.members]
        if "target_descriptors" in query:
            return [{"descriptor_id": m["target_id"]} for m in self.members]
        return list(self.slice_rows)

    async def fetchrow(self, query, *params):
        self.queries.append(query)
        return {"name": "Region \u2014 MENA"}


def _region_descriptor():
    from types import SimpleNamespace

    return SimpleNamespace(
        identity=SimpleNamespace(id="region_composition"),
        subscription=SimpleNamespace(
            other_analysts=[SimpleNamespace(
                id="country_composition", time_window="336h", data_types=[]
            )],
            targets=SimpleNamespace(predicate='has_tag("region")'),
        ),
    )


class _NeverCalledLLM:
    subprovider = "never_called"

    async def chat_complete(self, *a, **k):  # pragma: no cover
        raise AssertionError(
            "the rollup regime must issue ZERO LLM calls at the region tier"
        )


@pytest.mark.asyncio
async def test_read_slice_stamps_the_roster_only_under_the_regime(monkeypatch):
    rows = [_country_assembly_row("country_watch_ir")]
    conn = _RegionSliceConn(MEMBERSHIP["members"], rows)
    desc = _region_descriptor()

    monkeypatch.delenv(ap.ASSEMBLY_ENV, raising=False)
    out = await synth.READ_SLICE(conn, descriptor=desc, target_filter="region_mena")
    assert all(cs.REGION_MEMBERSHIP_ROW_KEY not in r for r in out)
    assert not any("name" in q and "target_descriptors" in q for q in conn.queries), (
        "the legacy region run must issue NOT ONE extra query"
    )

    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    conn2 = _RegionSliceConn(MEMBERSHIP["members"], [dict(r) for r in rows])
    out2 = await synth.READ_SLICE(conn2, descriptor=desc, target_filter="region_mena")
    stamped = [r for r in out2 if cs.REGION_MEMBERSHIP_ROW_KEY in r]
    assert stamped, "the rollup path needs the roster denormalised onto the rows"
    m = stamped[0][cs.REGION_MEMBERSHIP_ROW_KEY]
    assert m["region_id"] == "region_mena"
    assert m["region_name"] == "Region \u2014 MENA"
    assert [x["target_name"] for x in m["members"]] == ["Iran", "Israel", "Yemen"]


@pytest.mark.asyncio
async def test_run_method_produces_a_rollup_with_zero_llm_calls(monkeypatch):
    """THE ACCEPTANCE BAR, through the real binding path.

    ``run_method`` is what the actor calls. If the rollup only worked when a
    test called ``build_region_rollup`` directly, the acceptance claim would be
    about a path nobody takes.
    """
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    ir = _country_assembly_row("country_watch_ir", text="Iran signalled restraint.")
    il = _country_assembly_row("country_watch_il", text="Israel raised alert level.")
    for r in (ir, il):
        r[cs.REGION_MEMBERSHIP_ROW_KEY] = MEMBERSHIP

    from types import SimpleNamespace

    result = await synth.run_method(
        [ir, il],
        {"analyst_id": "region_composition", "target_id": "region_mena",
         "run_id": uuid4()},
        SimpleNamespace(llm=_NeverCalledLLM()),
    )
    f = result.finding
    payload = f.data["rollup"]
    assert payload["schema"] == rr.ROLLUP_SCHEMA
    assert payload["region_name"] == "Region \u2014 MENA"
    assert payload["members_carried"] == 2
    assert payload["members_missing"] == ["country_watch_ye"]
    # 5.2 - the regime stamp rides EVERY composition row; third value.
    assert f.data["assembly"]["regime"] == ap.REGIME_ROLLUP
    # Census item 1 - ``meta`` preserved (the gauge + sweep both filter on it).
    assert f.data["meta"] is True
    # Census item 2 - the ``composition:`` supersession signature preserved, or
    # prior region heads never close and N live heads pile up on /findings.
    assert str(f.data["situation_signature"]).startswith("composition:")
    # Census item 3 - derived_from is the contributing country assembly ids.
    assert set(str(x) for x in result.derived_from) == {str(ir["id"]), str(il["id"])}
    # Census item 4.
    assert "unstructured" not in f.tags and "coerce_failed" not in f.tags
    # The replacement verify is declared, and it re-derives.
    report = verify_structural_claims(
        data={rr.STRUCTURAL_CLAIMS_DATA_KEY: f.data[rr.STRUCTURAL_CLAIMS_DATA_KEY]},
        derived_from=[str(x) for x in result.derived_from],
    )
    assert report.had_claims is True
    assert all(v.verdict == kinds_supported() for v in report.claim_verdicts)
    # ZERO tokens, and the receipt says so.
    assert result.usage == {"prompt_tokens": 0, "completion_tokens": 0}
    assert not any(s.get("kind") == "llm_call" for s in result.intermediate_steps)
    assert any(s.get("phase") == "rollup" for s in result.intermediate_steps)
    # The body quotes both members verbatim.
    assert "> Iran signalled restraint." in f.body
    assert "> Israel raised alert level." in f.body


@pytest.mark.asyncio
async def test_the_correlation_guard_does_not_clobber_the_rollups_confidence(
    monkeypatch,
):
    """W-2a — a COMPLETENESS FRACTION is not an EVIDENCE BELIEF.

    The composition correlation guard caps a row's confidence at the
    de-duplicated evidence ceiling (the max ``effective_confidence`` over
    independent components), and it ran on the rollup too. Live on 2026-09-06 it
    rewrote 1.0 — six of six member country reads carried — to 0.50, 0.60 and
    0.6667 on five region rows whose ``independent_components`` EQUALLED their
    member count: it detected no shared lineage at all and capped anyway,
    because the strongest member's evidence score is simply lower than a full
    roster. The published row then read "6 of 6 member country reads carried"
    beside a confidence of 0.5.

    The audit is a real fact and stays. The overwrite goes, and the skip is
    NAMED so "why was this one not capped" is answerable from the row.
    """
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    ir = _country_assembly_row("country_watch_ir", text="Iran signalled restraint.")
    il = _country_assembly_row("country_watch_il", text="Israel raised alert level.")
    ye = _country_assembly_row("country_watch_ye", text="Yemen reported no change.")
    for r in (ir, il, ye):
        r[cs.REGION_MEMBERSHIP_ROW_KEY] = MEMBERSHIP
        # The LIVE shape, and the one that makes this a real regression: member
        # country reads verify around 0.5-0.67, so the de-duplicated evidence
        # ceiling sits WELL BELOW a full roster and the guard used to cap on it.
        r["effective_confidence"] = 0.6

    from types import SimpleNamespace

    result = await synth.run_method(
        [ir, il, ye],
        {"analyst_id": "region_composition", "target_id": "region_mena",
         "run_id": uuid4()},
        SimpleNamespace(llm=_NeverCalledLLM()),
    )
    f = result.finding
    payload = f.data["rollup"]
    # The whole roster carried, so the completeness probability is 1.0.
    assert payload["members_carried"] == payload["member_count"] == 3
    assert f.confidence == rr.rollup_confidence(payload) == 1.0

    guard = f.data.get("correlation_guard")
    assert guard is not None, "the audit is a real fact and must still be stamped"
    assert guard["confidence_capped"] is False
    assert "confidence_before" not in guard
    assert guard["confidence_cap_skipped"] == rr.ROLLUP_CONFIDENCE_NOT_EVIDENCE
    # The evidence ceiling is PUBLISHED beside the number it no longer bounds —
    # and it really is below 1.0, so the pre-W-2a code would have capped here.
    assert guard["dedup_confidence_ceiling"] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_a_world_assembly_publishes_a_roster_and_a_ledger(monkeypatch):
    """W-2b, through the real binding path.

    The world read has been publishing ``coverage: []`` beside
    ``coverage_roster: []`` while every country read publishes 32 of 32, so its
    aperture block told the voice "unit roster: not computed at this grain, so
    this list is the drop ledger alone" and the blind-spot arm had nothing to
    check a named unit against. With the roster stamped on the slice, the world
    assembly persists BOTH — and a roster unit that produced nothing is a NAMED
    ``no_head_in_horizon`` row rather than a silence.
    """
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    us = _country_assembly_row("country_g20_us", text="US energy pressure is high.")
    ir = _country_assembly_row("country_watch_ir", text="Iran signalled restraint.")
    roster = [
        "country_g20_us", "country_watch_ir", "country_watch_pk",
        "escalation_composition",
    ]
    for r in (us, ir):
        r[cs.WORLD_ROSTER_ROW_KEY] = roster

    from types import SimpleNamespace

    result = await synth.run_method(
        [us, ir],
        {"analyst_id": "world_assessor", "composition": True, "run_id": uuid4()},
        SimpleNamespace(llm=_NeverCalledLLM()),
    )
    assembly = result.finding.data["assembly"]
    assert assembly["tier"] == ap.TIER_WORLD
    # D-2b — the roster is persisted BESIDE the ledger it is the denominator of,
    # so ARM 4(a) diffs two arrays instead of counting a missing roster.
    assert assembly["coverage_roster"] == roster
    ledger = {e["unit"]: e["status"] for e in assembly["coverage"]}
    assert ledger == {
        "country_g20_us": "in_basis",
        "country_watch_ir": "in_basis",
        "country_watch_pk": "no_head_in_horizon",
        "escalation_composition": "no_head_in_horizon",
    }
    # The two units that produced nothing reach the drop ledger's own `no_head`
    # grain, which is what the aperture names.
    assert {d["unit"] for d in assembly["drops"]["no_head"]} == {
        "country_watch_pk", "escalation_composition",
    }
    # And the voice is told a denominator instead of "not computed at this grain".
    from legba.data.analysts import assessment_prompts as apr

    aperture = apr.declared_aperture(assembly)
    assert "not computed at this grain" not in aperture
    assert "declared roster (4 units)" in aperture
    assert (
        "of those, NOT in the basis this cycle: country_watch_pk; "
        "escalation_composition." in aperture
    )


@pytest.mark.asyncio
async def test_a_world_run_without_the_stamp_is_byte_for_byte_unchanged(monkeypatch):
    """The guard is PRESENCE of the stamp. A legacy world run, or any direct
    caller, carries no roster — and then the tier is exactly what it was."""
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    us = _country_assembly_row("country_g20_us", text="US energy pressure is high.")
    ir = _country_assembly_row("country_watch_ir", text="Iran signalled restraint.")

    from types import SimpleNamespace

    result = await synth.run_method(
        [us, ir],
        {"analyst_id": "world_assessor", "composition": True, "run_id": uuid4()},
        SimpleNamespace(llm=_NeverCalledLLM()),
    )
    assembly = result.finding.data["assembly"]
    assert assembly["coverage_roster"] == []
    assert assembly["coverage"] == []


def test_the_skip_reason_names_both_quantities_it_keeps_apart():
    """A skip that does not say what it skipped is the M-11 defect in a costume:
    the reader must be able to tell from the row which two numbers were held
    apart, not just that one of them was left alone."""
    reason = rr.ROLLUP_CONFIDENCE_NOT_EVIDENCE
    assert "FRACTION OF THE ROSTER" in reason
    assert "not an evidence belief" in reason
    assert "does not bound it" in reason


@pytest.mark.asyncio
async def test_a_periphery_member_never_inflates_the_rollups_derived_from(
    monkeypatch,
):
    """Regression — live critique ``041e606c`` on ``region_americas``:
    ``structural_miscount`` on ``:derived_from``, *"asserted 1 != re-derived
    6"*. The row's ``members_with_head``/``basis_count`` were both 1 (one
    country passed the floor into ``sliced``), yet the finding's actual
    ``derived_from`` carried 6 ids — the generic C-TIER envelope code
    (§ EVIDENCE TIERS) appends every PERIPHERY id to ``derived_from`` for
    ANY tiered composition, and a rollup is one, even though it never reads
    a periphery signal (no prose, nothing to hedge) — its ``:derived_from``
    structural claim asserts the SLICE-admitted (basis-only) count.
    """
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    ir = _country_assembly_row("country_watch_ir", text="Iran signalled restraint.")
    il = _country_assembly_row("country_watch_il", text="Israel raised alert level.")
    for r in (ir, il):
        r[cs.REGION_MEMBERSHIP_ROW_KEY] = MEMBERSHIP
        r[synth._EVIDENCE_FLOOR_KEY] = 0.5
    il[synth._EVIDENCE_TIER_KEY] = synth.PERIPHERY_TIER   # below the verify floor

    from types import SimpleNamespace

    result = await synth.run_method(
        [ir, il],
        {"analyst_id": "region_composition", "target_id": "region_mena",
         "run_id": uuid4()},
        SimpleNamespace(llm=_NeverCalledLLM()),
    )
    f = result.finding
    payload = f.data["rollup"]
    # Yemen never showed up at all; Israel showed up ONLY as periphery — the
    # rollup's own view is one carried head, honestly.
    assert payload["member_count"] == 3
    assert payload["members_with_head"] == 1
    assert payload["members_carried"] == 1
    assert set(payload["members_missing"]) == {"country_watch_il", "country_watch_ye"}
    # The envelope honestly records the periphery signal it saw...
    assert f.data["evidence_tiers"]["basis_count"] == 1
    assert f.data["evidence_tiers"]["periphery_count"] == 1
    assert f.data["evidence_tiers"]["periphery_ids"] == [il["id"]]
    # ...but the rollup's LINEAGE stays truthful: `derived_from` is the head it
    # actually carried, not the head it merely glimpsed as periphery.
    assert set(str(x) for x in result.derived_from) == {str(ir["id"])}
    assert il["id"] not in [str(x) for x in result.derived_from]
    # And the C2b re-derivation the row's own claim promised agrees with it —
    # no `structural_miscount`, the live shape this test pins shut.
    report = verify_structural_claims(
        data={rr.STRUCTURAL_CLAIMS_DATA_KEY: f.data[rr.STRUCTURAL_CLAIMS_DATA_KEY]},
        derived_from=[str(x) for x in result.derived_from],
    )
    assert report.had_claims is True
    assert all(v.verdict == kinds_supported() for v in report.claim_verdicts), (
        report.claim_verdicts
    )


@pytest.mark.asyncio
async def test_flag_off_the_same_region_run_still_calls_the_model(monkeypatch):
    """The other half of the byte-identity claim: with the flag off this is the
    generative path it has always been, LLM call included."""
    monkeypatch.delenv(ap.ASSEMBLY_ENV, raising=False)
    from types import SimpleNamespace

    calls = []

    class _LLM:
        subprovider = "double"

        async def chat_complete(self, messages, *, system=None, **kw):
            calls.append(system)
            return SimpleNamespace(
                content=json.dumps({
                    "title": "MENA", "body": "A region read [[ref:1]].",
                    "confidence": 0.5, "tags": ["region"],
                }),
                usage=SimpleNamespace(
                    prompt_tokens=10, completion_tokens=5, reasoning_tokens=0
                ),
            )

    ir = _country_assembly_row("country_watch_ir")
    result = await synth.run_method(
        [ir],
        {"analyst_id": "region_composition", "target_id": "region_mena",
         "run_id": uuid4()},
        SimpleNamespace(llm=_LLM()),
    )
    assert calls, "flag off must still call the model"
    assert calls[-1] == synth._REGION_COMPOSITION_SYSTEM
    assert "rollup" not in result.finding.data
    assert result.finding.data["assembly"] == {
        "schema": ap.ASSEMBLY_SCHEMA, "regime": ap.REGIME_LEGACY
    }


# ---------------------------------------------------------------------------
# THE TWO ARITHMETIC EDGES — both found by review, both regression-pinned
# ---------------------------------------------------------------------------


def test_an_off_roster_carry_never_produces_seven_of_six():
    """A desk retagged out of the region mid-window still has a fresh head.

    Honest data is never discarded, so it is CARRIED — but it must not enter the
    numerator of a fraction whose denominator is the roster. "7 of 6 members
    carried" is not a coverage statement, and a confidence above 1.0 is not a
    probability.
    """
    rows = [
        _country_assembly_row("country_watch_ir"),
        _country_assembly_row("country_watch_il"),
        _country_assembly_row("country_watch_ye"),
        _country_assembly_row("lane_hormuz"),      # not on the MENA roster
    ]
    payload = _rollup(rows)
    assert payload["member_count"] == 3
    assert payload["members_carried"] == 3
    assert payload["extra_carried"] == 1
    assert rr.rollup_confidence(payload) == 1.0
    assert len(payload["leads"]) == 4               # carried, not discarded
    body = rr.render_rollup_body(payload)
    assert "3 member country reads of the region's 3 members" in body
    assert "plus 1 carried from a desk no longer tagged into this region" in body
    # ...and it is still IN the read, named, with its own block.
    assert "lane_hormuz" in body


def test_a_lagging_member_does_not_make_the_lineage_claim_cry_wolf():
    """During the rollout a member can have a head with no assembly to carry.

    ``derived_from`` holds every head the SLICE admitted; ``members_carried``
    holds the ones with a lead. Asserting the carried count against the lineage
    would fire `structural_miscount` on a perfectly honest rollup the first time
    one desk lagged the cutover — a detector crying wolf about the rollout it
    exists to watch.
    """
    from legba.data.provenance.structural_claims import STRUCTURAL_SUPPORTED

    rows = [
        _country_assembly_row("country_watch_ir"),
        _country_assembly_row(
            "country_watch_il", regime=ap.REGIME_LEGACY, blocks=False
        ),
    ]
    payload = _rollup(rows)
    assert payload["members_with_head"] == 2
    assert payload["members_carried"] == 1
    claims = rr.rollup_structural_claims(payload)
    lineage = [c for c in claims if c["id"].endswith(":derived_from")][0]
    assert lineage["asserted"] == 2, "the lineage is the HEADS, not the leads"
    report = verify_structural_claims(
        data={"structural_claims": claims},
        derived_from=[str(r["id"]) for r in rows],
    )
    assert [v.verdict for v in report.claim_verdicts] == [STRUCTURAL_SUPPORTED] * 4


# ---------------------------------------------------------------------------
# THE CITATION ORDER — one ordering, or the labels name the wrong country
#
# THE DEFECT (live, 2026-09-05T23:45Z .. 2026-09-07, 15 of 16 rollup rows).
# ``render_rollup_body`` numbers its member sections over the ROSTER (the
# denominator the tier reports against); the generic composition CITE step
# resolved ``[[ref:N]]`` against ``sliced[N-1]``, and the slice arrives
# SALIENCE-ordered. Two orderings sharing one ordinal space: on
# ``region_americas`` 23:45Z the Argentina section's own ``[[ref:1]]`` carried
# the UNITED STATES' head id, title and body as its evidence — and so did the
# export, the drill link and the judge evidence map for any consumer that
# grades a rollup.
#
# Every OTHER tier is aligned by construction and stays untouched:
# ``build_assembly`` mints ``blocks[i].ordinal = i+1`` straight off
# ``carried = sliced[:BLOCK_CAP]``, so there ``sliced[N-1]`` IS block N. The
# rollup is the one producer whose render order is not the slice order, so it
# publishes its own index (``rollup_ordinal_index``) and the CITE step uses it.
# ---------------------------------------------------------------------------

#: The live ``region_americas`` roster, in the order the membership SQL returns
#: it — which is the order the body renders. Fixed here rather than sorted, so
#: the test breaks if the roster order ever stops driving the render.
AMERICAS = {
    "region_id": "region_americas",
    "region_name": "Region — Americas",
    "members": [
        {"target_id": "country_g20_ar", "target_name": "G20 — Argentina"},
        {"target_id": "country_g20_br", "target_name": "G20 — Brazil"},
        {"target_id": "country_g20_ca", "target_name": "G20 — Canada"},
        {"target_id": "country_g20_mx", "target_name": "G20 — Mexico"},
        {"target_id": "country_g20_us", "target_name": "G20 — United States"},
        {"target_id": "country_watch_ht", "target_name": "Watch — Haiti"},
    ],
}

#: THE EXACT SCRAMBLE, pinned from the live 2026-09-06T23:45Z row
#: (``3d805e1c-5b60-408c-9684-484b4ce3f075``). Fetch/slice order first, render
#: order second; the permutation between them is what the old code shipped as a
#: citation list.
AMERICAS_FETCH_ORDER = [
    "country_g20_us", "country_g20_ca", "country_g20_br",
    "country_watch_ht", "country_g20_ar", "country_g20_mx",
]
AMERICAS_RENDER_ORDER = [m["target_id"] for m in AMERICAS["members"]]


def _americas_rows():
    """Six member heads whose SLICE order is not their ROSTER order.

    ``_orient`` sorts unstamped inputs newest-first, so descending
    ``produced_at`` down :data:`AMERICAS_FETCH_ORDER` is what puts the slice in
    that order — the same mechanism (consequence, then recency) that produced
    the live scramble, rather than a hand-placed list a refactor could quietly
    stop honouring.
    """
    rows = []
    for i, target_id in enumerate(AMERICAS_FETCH_ORDER):
        row = _country_assembly_row(
            target_id,
            text=f"{target_id} said its own distinct sentence.",
            produced_at=f"2026-09-03T{10 - i:02d}:00:00+00:00",
        )
        # A DISTINCT body per member, because ``evidence_text`` is the cited
        # row's whole body and swapping it is exactly what the defect did: the
        # judge, the export and the drill link were all handed a neighbour's
        # read under this member's marker.
        row["body"] = f"{DESK_BODY}\nThis is the {target_id} read.\n"
        # Salience DESCENDING down the fetch order, which is what makes the
        # slice order that order: ``_orient`` sorts by consequence first. This
        # is the live mechanism, not a hand-placed list — and it is also what
        # keeps ``eval.salience_check`` in the picture, since that stamp reads
        # ``sliced[0]`` and is deliberately NOT re-pointed by this fix.
        row["data"]["data"]["salience"] = {
            "magnitude": round(0.9 - 0.1 * i, 2),
            "source": "signals",
            "top_signal_id": f"sig-{target_id}",
        }
        row[cs.REGION_MEMBERSHIP_ROW_KEY] = AMERICAS
        rows.append(row)
    return rows


def test_rollup_ordinal_index_follows_the_render_not_the_fetch():
    """The published index IS the walk ``render_rollup_body`` performs."""
    rows = _americas_rows()
    payload = _rollup(rows, membership=AMERICAS)
    index = rr.rollup_ordinal_index(payload, rows)
    assert [index[n]["target_id"] for n in sorted(index)] == AMERICAS_RENDER_ORDER
    # ... and NOT the order the rows arrived in, which is the whole point.
    assert [r["target_id"] for r in rows] == AMERICAS_FETCH_ORDER
    assert [index[n]["target_id"] for n in sorted(index)] != AMERICAS_FETCH_ORDER
    # Dense over 1..n, so no marker can resolve to a hole.
    assert sorted(index) == list(range(1, len(AMERICAS_RENDER_ORDER) + 1))


def test_rollup_ordinal_index_keys_on_assembly_id_never_on_position():
    """A member whose row is absent yields NO entry, never a neighbour's row.

    The failure this whole fix exists to end is a marker resolving to the wrong
    head. Silently sliding the next row into the gap would re-introduce it at
    the one moment the data is already odd.
    """
    rows = _americas_rows()
    payload = _rollup(rows, membership=AMERICAS)
    index = rr.rollup_ordinal_index(payload, rows[1:])   # drop the US row
    assert 5 not in index, "the missing member's ordinal stays empty"
    assert index[1]["target_id"] == "country_g20_ar"
    assert index[6]["target_id"] == "country_watch_ht"


@pytest.mark.asyncio
async def test_every_section_marker_resolves_to_its_own_head(monkeypatch):
    """THE REGRESSION, through the real binding path.

    Fails on the pre-fix code by construction: under ``sliced[N-1]`` section 1
    (Argentina) resolved to ``country_g20_us``, section 2 (Brazil) to
    ``country_g20_ca``, and so on down the permutation pinned above.
    """
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    from types import SimpleNamespace

    rows = _americas_rows()
    by_target = {r["target_id"]: r for r in rows}
    result = await synth.run_method(
        list(rows),
        {"analyst_id": "region_composition", "target_id": "region_americas",
         "run_id": uuid4()},
        SimpleNamespace(llm=_NeverCalledLLM()),
    )
    f = result.finding
    citations = f.data["citations"]
    by_ordinal = {c["ordinal"]: c for c in citations}

    # The body renders the roster order, each section closing on its own marker.
    sections = re.findall(r"^### (\d+) — (.*)$", f.body, re.M)
    assert [name for _, name in sections] == [
        m["target_name"] for m in AMERICAS["members"]
    ]

    # THE PROPERTY: [[ref:N]] in section N names section N's OWN head.
    for (ordinal, name), target_id in zip(sections, AMERICAS_RENDER_ORDER):
        n = int(ordinal)
        citation = by_ordinal[n]
        assert citation["ref_id"] == str(by_target[target_id]["id"]), (
            f"section {n} ({name}) must cite {target_id}"
        )
        assert citation["target_id"] == target_id
        assert citation["marker"] == f"[[ref:{n}]]"
        # And the evidence a judge/export would show for that marker is that
        # member's own read, not a neighbour's.
        assert f"This is the {target_id} read." in citation["evidence_text"]
        assert citation["title"] == f"{target_id} read"

    # The old resolution is pinned as WRONG, so a revert cannot pass quietly.
    sliced_order = [h["target_id"] for h in f.data["head_ages"]["heads"]]
    assert sliced_order == AMERICAS_FETCH_ORDER
    assert sliced_order != AMERICAS_RENDER_ORDER
    assert by_ordinal[1]["target_id"] != sliced_order[0]


@pytest.mark.asyncio
async def test_citation_list_order_is_the_section_order(monkeypatch):
    """``citations[]`` is built FROM the rendered section list, so its order IS
    the reading order — never ``derived_from``'s and never the slice's."""
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    from types import SimpleNamespace

    result = await synth.run_method(
        _americas_rows(),
        {"analyst_id": "region_composition", "target_id": "region_americas",
         "run_id": uuid4()},
        SimpleNamespace(llm=_NeverCalledLLM()),
    )
    citations = result.finding.data["citations"]
    assert [c["target_id"] for c in citations] == AMERICAS_RENDER_ORDER
    assert [c["ordinal"] for c in citations] == [1, 2, 3, 4, 5, 6]
    # One citation per carried member — the invariant the render banner claims.
    payload = result.finding.data["rollup"]
    assert len(citations) == payload["members_carried"] == len(payload["leads"])


@pytest.mark.asyncio
async def test_derived_from_is_still_every_carried_member(monkeypatch):
    """The rollup-miscount fix (99f45bc9 / 0b6e7c83) must not regress.

    ``derived_from`` stays the SLICE's lineage — every head admitted, in slice
    order — and is emphatically NOT what the citation list is now built from.
    Two different questions: *what did this row read* and *what does section N
    quote*.
    """
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    from types import SimpleNamespace

    rows = _americas_rows()
    result = await synth.run_method(
        list(rows),
        {"analyst_id": "region_composition", "target_id": "region_americas",
         "run_id": uuid4()},
        SimpleNamespace(llm=_NeverCalledLLM()),
    )
    assert [str(x) for x in result.derived_from] == [
        str(r["id"]) for r in rows
    ], "lineage is the slice, unchanged and complete"
    lineage = [
        c for c in result.finding.data[rr.STRUCTURAL_CLAIMS_DATA_KEY]
        if c["id"].endswith(":derived_from")
    ][0]
    assert lineage["asserted"] == 6
    report = verify_structural_claims(
        data={rr.STRUCTURAL_CLAIMS_DATA_KEY:
              result.finding.data[rr.STRUCTURAL_CLAIMS_DATA_KEY]},
        derived_from=[str(x) for x in result.derived_from],
    )
    assert all(v.verdict == kinds_supported() for v in report.claim_verdicts)


@pytest.mark.asyncio
async def test_only_the_ordinals_moved_every_other_field_is_byte_identical(
    monkeypatch,
):
    """THE GOLDEN. Re-numbering is the ONLY difference the fix may make.

    The pre-fix citation list is reconstructed here exactly as the old code
    built it — ``_build_composition_citation(n, sliced[n-1])`` — and compared
    against the shipped one key by key. Everything but ``ordinal``/``marker``
    must match, and every SCORED field on the row (confidence, severity, the
    correlation guard's ceiling, the salience check) must be untouched, because
    a labelling fix that moves a score is not a labelling fix.
    """
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    from types import SimpleNamespace

    rows = _americas_rows()
    result = await synth.run_method(
        list(rows),
        {"analyst_id": "region_composition", "target_id": "region_americas",
         "run_id": uuid4()},
        SimpleNamespace(llm=_NeverCalledLLM()),
    )
    f = result.finding
    shipped = {c["ref_id"]: c for c in f.data["citations"]}

    # The OLD mapping, rebuilt through the same shipped helper.
    sliced = sorted(rows, key=lambda r: r["produced_at"], reverse=True)
    old = [synth._build_composition_citation(n, r)
           for n, r in enumerate(sliced, start=1)]
    assert [c["target_id"] for c in old] == AMERICAS_FETCH_ORDER

    assert set(shipped) == {c["ref_id"] for c in old}
    for before in old:
        after = shipped[before["ref_id"]]
        moved = {k for k in set(before) | set(after)
                 if before.get(k) != after.get(k)}
        assert moved <= {"ordinal", "marker"}, (
            f"the fix moved {moved - {'ordinal', 'marker'}} — it may move only "
            "which ordinal names which row"
        )

    # The guard partitions by shared lineage, which is a SET property — the
    # ceiling that caps confidence cannot move when only the labels do.
    assert synth._correlation_guard(old)["dedup_confidence_ceiling"] == (
        f.data["correlation_guard"]["dedup_confidence_ceiling"]
    )
    assert f.data["correlation_guard"]["cited_heads"] == 6
    assert f.data["correlation_guard"]["independent_components"] == 6
    # The rollup's own arithmetic is untouched: confidence is the carried
    # fraction, severity the max over carried members, both read off the payload
    # the CITE step never writes to.
    assert f.confidence == rr.rollup_confidence(f.data["rollup"])
    assert f.data["rollup"]["members_carried"] == 6
    # ``salience_check`` is the ONE scored stamp that still reads the slice
    # (``sliced[resolved_ords[0] - 1]``) and is deliberately left alone — see
    # the report: changing it would move a graded verdict, and the rollup is not
    # faithfulness-graded at all (``actor_critic`` no-ops on the regime).
    check = f.data["eval"]["salience_check"]
    assert check["lead_ref"] == 1
    assert check["lead_magnitude"] == 0.9 == check["top_magnitude"], (
        "the stamp still measures sliced[0] — the highest-consequence input — "
        "so its verdict is byte-identical before and after the re-numbering"
    )
    assert check["pass"] is True and check["gap"] == 0.0


def test_an_off_roster_carry_gets_its_own_ordinal_and_resolves_to_itself():
    """``num_subclaims`` for the rollup is ``members_carried + extra_carried``.

    ON-ROSTER and OFF-ROSTER carries are counted in two different fields on
    purpose (a desk retagged mid-window is carried, but must not appear in the
    numerator of a fraction whose denominator is the roster). The RENDER walks
    both — ``carried`` is every member with a lead — so the ordinal space has to
    span both too, or the last section's marker falls out of range and is
    dropped as fabricated.
    """
    rows = _americas_rows()[:2]                      # fetch order us, ca
    stray = _country_assembly_row(
        "country_watch_zz", produced_at="2026-09-03T05:00:00+00:00"
    )
    stray[cs.REGION_MEMBERSHIP_ROW_KEY] = AMERICAS   # a desk no longer tagged in
    rows.append(stray)
    payload = _rollup(rows, membership=AMERICAS)
    assert payload["members_carried"] == 2
    assert payload["extra_carried"] == 1

    sections = re.findall(r"^### (\d+) — (.*)$", rr.render_rollup_body(payload), re.M)
    index = rr.rollup_ordinal_index(payload, rows)
    assert len(sections) == len(index) == (
        payload["members_carried"] + payload["extra_carried"]
    )
    # The two roster members come back in ROSTER order (ca before us, the
    # reverse of how they arrived), and the off-roster carry renders LAST —
    # appended after the roster walk — with its ordinal naming its own row.
    assert [index[n]["target_id"] for n in sorted(index)] == [
        "country_g20_ca", "country_g20_us", "country_watch_zz",
    ]
    carried = [m for m in payload["members"] if m["lead_source"] == rr.LEAD_CARRIED]
    for ordinal, _name in sections:
        n = int(ordinal)
        assert str(index[n]["id"]) == carried[n - 1]["assembly_id"]
# AMENDMENT 4a (2026-09-07, pre-T0) — THE CARRY IS BY MASS, NOT BY POSITION
#
# The defect, measured over 129 live member-carries: `_lead_block` took
# `lead.block_ordinals[0]` when a lead was crowned and ordinal 1 when it was
# not — and `block_ordinals[0]` is ITSELF 1 on 129 of 129 carries, because the
# co-lead band is anchored on ordinal 1 by construction. So ordinal 1 was the
# ONLY path, and ordinal 1 is the SEVERITY-order top, which on a day when the
# severe dimension is quiet is a block with no cited evidence under it. 78 of
# 129 carries were not the read's top-mass block; 22 carried 0.00 while the
# same read held evidence elsewhere.
#
# This half is UNFLAGGED and pre-T0: it moves region BODY bytes and nothing the
# freeze protects. `test_the_carry_change_moves_no_scored_number` is the guard
# that keeps that claim honest.
# ---------------------------------------------------------------------------


def _multi_block_row(
    target_id, *, blocks, lead_kind=ap.LEAD_NONE, ordinals=(), uid=None,
):
    """A member head with SEVERAL blocks — the shape the carry rule chooses in.

    ``blocks`` is a list of ``(desk, cited_mass, text)`` in ordinal order, plus
    an optional 4th element ``n_cited`` for the register tests.
    """
    row = _country_assembly_row(target_id, uid=uid)
    built = []
    for i, spec in enumerate(blocks, start=1):
        desk, mass, text = spec[0], spec[1], spec[2]
        n_cited = spec[3] if len(spec) > 3 else (3 if mass > 0 else 0)
        built.append({
            "ordinal": i,
            "finding_id": f"desk-head-{target_id}-{i}",
            "desk": desk,
            "target_id": target_id,
            "target_name": target_id,
            "question": desk.replace("_", " ").capitalize(),
            "produced_at": "2026-09-03T10:01:00+00:00",
            "severity": "high",
            "salience": {
                "cited_mass": mass, "n_cited": n_cited, "source": "cited_signals",
            },
            "spans": [{
                "text": text,
                "role": "bluf",
                "scope_tokens": [],
                "origin": {"head_id": f"desk-head-{target_id}-{i}", "desk": desk},
            }],
            "signals": [],
            "corroboration": {},
            "verify": {"overall_score": 0.9},
        })
    asm = row["data"]["data"]["assembly"]
    asm["blocks"] = built
    asm["lead"] = {"kind": lead_kind, "block_ordinals": list(ordinals)}
    return row


def _rollup_floor(rows, floor, *, membership=MEMBERSHIP):
    for r in rows:
        r[cs.REGION_MEMBERSHIP_ROW_KEY] = membership
    return rr.build_region_rollup(
        as_of="2026-09-03T12:00:00+00:00",
        mass_floor=floor,
        **rr.rollup_inputs(
            rows, region_id=membership["region_id"], horizon_hours=None
        ),
    )


def test_the_fallback_carry_is_the_heaviest_block_not_the_first():
    """UKRAINE's shape, 2026-09-06 23:30Z. ``lead.kind`` is ``none``, ordinal 1
    is an energy_security block at 0.00 saying a cease-fire *"eases the
    immediate blackout risk"*, and the escalation block at 1.58 — the day's
    actual evidence — sat at ordinal 2 and was never carried."""
    row = _multi_block_row("country_watch_ir", blocks=[
        ("energy_security", 0.0, "A cease-fire eases the immediate blackout risk."),
        ("escalation", 1.58, "Deep-range drone attacks are intensifying."),
        ("military_posture", 0.5, "Posture is unchanged."),
    ])
    block, ordinal, why = rr._lead_block(row["data"]["data"]["assembly"])
    assert ordinal == 2 and why == rr.CARRY_BY_MASS
    assert block["desk"] == "escalation"


def test_a_co_lead_member_carries_the_heaviest_co_lead_not_the_first():
    """``co_leads`` was a LABEL on ordinal 1 — the band always began there. The
    carry now reads the masses rather than the label."""
    row = _multi_block_row(
        "country_watch_ir",
        blocks=[
            ("economic_coercion", 0.15, "Tariff pressure holds."),
            ("narrative_coordination", 0.10, "Messaging is steady."),
            ("escalation", 0.66, "A consulate closure follows the strike."),
        ],
        lead_kind=ap.LEAD_CO_LEADS,
        ordinals=[1, 2, 3],
    )
    block, ordinal, why = rr._lead_block(row["data"]["data"]["assembly"])
    assert (ordinal, why) == (3, rr.CARRY_BY_MASS)
    assert block["desk"] == "escalation"


def test_an_earned_crown_is_honoured_exactly_as_the_member_issued_it():
    """The one branch that does NOT move. A member that earned a single lead
    has already made this decision on its own arithmetic, and this tier
    re-deriving it would be the second bug §4.1 exists to forbid."""
    row = _multi_block_row(
        "country_watch_ir",
        blocks=[
            ("military_posture", 0.20, "Posture hardens."),
            ("escalation", 1.58, "Strikes intensify."),
        ],
        lead_kind=ap.LEAD_EARNED_SINGLE,
        ordinals=[1],
    )
    block, ordinal, why = rr._lead_block(row["data"]["data"]["assembly"])
    assert (ordinal, why) == (1, rr.CARRY_EARNED_LEAD)
    assert block["desk"] == "military_posture"


def test_all_zero_falls_back_to_ordinal_one_and_says_so():
    """The honest floor of the rule. No mass anywhere means there is nothing to
    rank on, so the carry is the top of the member's own total order — what
    this tier always did, now stamped instead of assumed."""
    row = _multi_block_row("country_watch_ir", blocks=[
        ("economic_coercion", 0.0, "Tariffs remain unchanged."),
        ("escalation", 0.0, "No new incidents are reported."),
    ])
    block, ordinal, why = rr._lead_block(row["data"]["data"]["assembly"])
    assert (ordinal, why) == (1, rr.CARRY_FALLBACK_ORDINAL_1)
    assert block["desk"] == "economic_coercion"


def test_a_well_cited_absence_claim_never_becomes_the_regional_block():
    """INDONESIA, 23:30Z: ``narrative_coordination`` reads *"no coordinated
    narrative is evident in collected reporting"* across 92 signals and scores
    5.79 — the heaviest block in the read, and a statement that nothing
    happened. ``argmax(cited_mass)`` alone would promote it into the
    Indo-Pacific read. The exclusion is ARM 2's own truthmaker, reused."""
    row = _multi_block_row("country_watch_ir", blocks=[
        ("energy_security", 0.80, "LNG offtake contracts were re-priced."),
        (
            "narrative_coordination", 5.79,
            "No coordinated narrative is evident in collected reporting.",
        ),
    ])
    block, ordinal, why = rr._lead_block(row["data"]["data"]["assembly"])
    assert (ordinal, why) == (1, rr.CARRY_BY_MASS)
    assert block["desk"] == "energy_security"


def test_a_declared_scope_token_excludes_a_block_even_if_the_span_was_trimmed():
    """ARM 2's ``scope_truncated`` case: the span no longer CONTAINS the
    qualifier but the payload still declares it. Either signal is enough,
    because the cost of wrongly excluding is the second-heaviest thread and the
    cost of wrongly including is a regional headline saying nothing happened."""
    row = _multi_block_row("country_watch_ir", blocks=[
        ("energy_security", 0.80, "LNG offtake contracts were re-priced."),
        ("narrative_coordination", 5.79, "No coordinated narrative is evident."),
    ])
    asm = row["data"]["data"]["assembly"]
    asm["blocks"][1]["spans"][0]["scope_tokens"] = ["collection_denominator"]
    block, ordinal, why = rr._lead_block(asm)
    assert (ordinal, why) == (1, rr.CARRY_BY_MASS)


def test_every_block_being_an_absence_claim_falls_back_rather_than_carrying_one():
    row = _multi_block_row("country_watch_ir", blocks=[
        ("narrative_coordination", 5.79,
         "No coordinated narrative is evident in collected reporting."),
        ("internal_stability", 1.20,
         "No unrest appears in the available evidence."),
    ])
    block, ordinal, why = rr._lead_block(row["data"]["data"]["assembly"])
    assert (ordinal, why) == (1, rr.CARRY_FALLBACK_ORDINAL_1)
    assert block["desk"] == "narrative_coordination"


def test_the_reviewers_canada_shape_is_a_regression_test():
    """The specimen that opened this lane. Canada, 23:30Z: ``lead.kind``
    ``none`` (ratio 2.50 and share 0.714, refused on the count alone), ordinal
    1 an economic_coercion block at 0.00 whose span says the 50% tariff
    *"remains unchanged"*, while the escalation block carried the day's mass.
    The rollup carried the 0.00 one, and an external reviewer graded the result
    "unchanged at 0.00"."""
    row = _multi_block_row("country_watch_ir", blocks=[
        ("economic_coercion", 0.0,
         "Canada continues to bear a 50% U.S. tariff that remains unchanged."),
        ("escalation", 0.25,
         "A diplomatic rupture over US-Canada tariff measures is widening."),
        ("energy_security", 0.0, "Pipeline flows are steady."),
    ])
    block, ordinal, why = rr._lead_block(row["data"]["data"]["assembly"])
    assert (ordinal, why) == (2, rr.CARRY_BY_MASS)
    assert "diplomatic rupture" in block["spans"][0]["text"]


def test_the_carry_reason_is_stamped_on_every_carried_member_and_only_those():
    rows = [
        _multi_block_row("country_watch_ir", blocks=[
            ("energy_security", 0.0, "Steady."),
            ("escalation", 1.2, "Strikes intensify."),
        ]),
        _country_assembly_row(
            "country_watch_il", regime=ap.REGIME_LEGACY, blocks=False
        ),
    ]
    payload = _rollup(rows)
    by_target = {m["target_id"]: m for m in payload["members"]}
    assert by_target["country_watch_ir"]["carry_reason"] == rr.CARRY_BY_MASS
    # A member with nothing to carry did not "fall back" — it was absent, and a
    # reason there would be a sentence about a choice nobody made.
    assert by_target["country_watch_il"]["carry_reason"] is None
    assert by_target["country_watch_ye"]["carry_reason"] is None
    for m in payload["members"]:
        assert m["carry_reason"] in (None, *rr.CARRY_REASONS)


def test_the_carry_change_moves_no_scored_number():
    """THE GUARD that keeps Amendment 4a pre-T0 legal.

    Build the same region under the SHIPPED carry rule (ordinal 1) and the new
    one, and assert everything the freeze protects is equal. Only ``leads[]``,
    ``members[].lead_block_ordinal``, ``members[].cited_mass`` and the BODY may
    differ."""
    specs = [
        ("energy_security", 0.0, "A cease-fire eases the blackout risk."),
        ("escalation", 1.58, "Deep-range drone attacks are intensifying."),
    ]
    # ONE head id across both builds: the structural claims' basis is the
    # member row's own id, and a fresh uuid per build would fail this test for
    # a reason that has nothing to do with the carry.
    head = "3f5b7c42-0000-4000-8000-00000000d5a1"
    payload_new = _rollup([
        _multi_block_row("country_watch_ir", blocks=specs, uid=head)])
    # The shipped rule, reproduced by handing the member a lead that names
    # ordinal 1 — which is what `block_ordinals[0]` was on 129 of 129 carries.
    payload_old = _rollup([_multi_block_row(
        "country_watch_ir", blocks=specs, uid=head,
        lead_kind=ap.LEAD_EARNED_SINGLE, ordinals=[1],
    )])

    assert payload_old["members"][0]["lead_block_ordinal"] == 1
    assert payload_new["members"][0]["lead_block_ordinal"] == 2

    assert rr.rollup_title(payload_old) == rr.rollup_title(payload_new)
    assert rr.rollup_confidence(payload_old) == rr.rollup_confidence(payload_new)
    assert rr.rollup_severity(payload_old) == rr.rollup_severity(payload_new)
    for field in (
        "members_carried", "member_count", "members_missing",
        "members_with_head", "extra_carried",
    ):
        assert payload_old[field] == payload_new[field], field
    old_claims = rr.rollup_structural_claims(payload_old)
    new_claims = rr.rollup_structural_claims(payload_new)
    assert [c["id"] for c in old_claims] == [c["id"] for c in new_claims]
    assert [c["asserted"] for c in old_claims] == [c["asserted"] for c in new_claims]
    assert [c["basis"] for c in old_claims] == [c["basis"] for c in new_claims]
    # And the thing that DID move is the body — which is the point.
    assert rr.render_rollup_body(payload_old) != rr.render_rollup_body(payload_new)


# ---------------------------------------------------------------------------
# AMENDMENT 4a — THE REGISTER (labels only, no number moves)
# ---------------------------------------------------------------------------


def test_the_register_separates_the_two_ways_a_carry_reads_zero():
    """A ``cited mass 0.00`` line means one of two completely different things
    and today a reader cannot tell which. Measured split at 23:45Z: 23 live /
    2 cited-nothing / 7 sub-floor-only."""
    assert rr.member_register(
        {"salience": {"cited_mass": 1.2, "n_cited": 4}}) == rr.REGISTER_LIVE
    assert rr.member_register(
        {"salience": {"cited_mass": 0.0, "n_cited": 0}}) == rr.REGISTER_CITED_NOTHING
    assert rr.member_register(
        {"salience": {"cited_mass": 0.0, "n_cited": 9}}) == rr.REGISTER_SUB_FLOOR_ONLY
    # Nothing carried is not a register — it is an absence.
    assert rr.member_register(None) is None
    # A block with no salience object at all cited nothing this instrument can
    # see, which is what the zero it would otherwise show already means.
    assert rr.member_register({}) == rr.REGISTER_CITED_NOTHING


def test_the_register_reaches_the_payload_and_the_body_and_moves_no_number():
    rows = [_multi_block_row("country_watch_ir", blocks=[
        ("escalation", 0.0, "No new incidents are reported.", 0),
        ("energy_security", 0.0, "Flows are steady.", 7),
    ])]
    payload = _rollup(rows)
    carried = [m for m in payload["members"] if m["lead_source"] == rr.LEAD_CARRIED]
    assert [m["register"] for m in carried] == [rr.REGISTER_CITED_NOTHING]
    assert "register cited nothing" in rr.render_rollup_body(payload)
    for m in payload["members"]:
        assert m["register"] in (None, *rr.REGISTERS)


def test_the_register_never_filters_a_member_out_of_the_rollup():
    """A label, not a gate. Every carried member is still carried, whatever its
    register — the reviewer's complaint was that zero-mass carries were
    UNEXPLAINED, not that they were present."""
    rows = [
        _multi_block_row(
            "country_watch_ir", blocks=[("escalation", 0.0, "Quiet.", 0)]),
        _multi_block_row(
            "country_watch_il", blocks=[("escalation", 2.0, "Loud.", 5)]),
    ]
    payload = _rollup(rows)
    assert payload["members_carried"] == 2
    assert {m["register"] for m in payload["members"] if m["register"]} == {
        rr.REGISTER_CITED_NOTHING, rr.REGISTER_LIVE,
    }


# ---------------------------------------------------------------------------
# AMENDMENT 4a — LEGBA_ROLLUP_MASS_FLOOR (ships DARK at 0.0)
# ---------------------------------------------------------------------------


def test_the_mass_floor_defaults_to_zero_and_the_option_wins_over_the_env(
    monkeypatch,
):
    monkeypatch.delenv(rr.ROLLUP_MASS_FLOOR_ENV, raising=False)
    assert rr.rollup_mass_floor() == 0.0
    monkeypatch.setenv(rr.ROLLUP_MASS_FLOOR_ENV, "0.10")
    assert rr.rollup_mass_floor() == pytest.approx(0.10)
    assert rr.rollup_mass_floor(
        {rr.ROLLUP_MASS_FLOOR_OPTION: 0.05}) == pytest.approx(0.05)
    # Unreadable on either channel keeps what it would have had.
    assert rr.rollup_mass_floor(
        {rr.ROLLUP_MASS_FLOOR_OPTION: "a bit"}) == pytest.approx(0.10)
    monkeypatch.setenv(rr.ROLLUP_MASS_FLOOR_ENV, "not a number")
    assert rr.rollup_mass_floor() == 0.0
    # Negative clamps rather than inverting the comparison.
    monkeypatch.setenv(rr.ROLLUP_MASS_FLOOR_ENV, "-1")
    assert rr.rollup_mass_floor() == 0.0


def test_the_mass_floor_at_zero_is_byte_identical_to_the_carry_without_it():
    rows = [_multi_block_row("country_watch_ir", blocks=[
        ("energy_security", 0.02, "A marginal move."),
        ("escalation", 0.08, "Another marginal move."),
    ])]
    assert rr.render_rollup_body(_rollup(rows)) == rr.render_rollup_body(
        _rollup_floor(rows, 0.0)
    )


def test_the_mass_floor_demotes_a_sub_floor_member_to_the_ordinal_one_fallback():
    """Inside the documented safe band. Every block under the floor means there
    is nothing left to rank, so the member lands on ordinal 1 and SAYS so — it
    does not silently carry a block the operator just declared to be noise."""
    rows = [_multi_block_row("country_watch_ir", blocks=[
        ("energy_security", 0.02, "A marginal move."),
        ("escalation", 0.08, "Another marginal move."),
    ])]
    member = _rollup_floor(rows, 0.10)["members"][0]
    assert member["carry_reason"] == rr.CARRY_FALLBACK_ORDINAL_1
    assert member["lead_block_ordinal"] == 1


# ---------------------------------------------------------------------------
# AMENDMENT 4c — the carry sees the SAME absence test the crown does
# ---------------------------------------------------------------------------


def test_a_clock_bounded_absence_claim_is_excluded_from_the_carry_too():
    """The gap the first cut left. ARM 2's predicate fires on a COLLECTION
    denominator and explicitly NOT on a clock, and the live absence spans are
    clock-bounded — so ARM 2 alone caught 1 of the 8 region carries that opened
    with a negation. ARGENTINA and MEXICO, 2026-09-06 23:30Z."""
    for absent, real in (
        ("No coordinated narrative is evident in Argentina's media over the "
         "past three days.",
         "Argentina's escalation risk is elevated as President Javier Milei "
         "has signed an accord."),
        ("No coordinated narrative is evident across Mexican sources in this "
         "72-hour window.",
         "Mexico's energy-security pressure stays elevated despite Hurricane "
         "Marie."),
    ):
        # ARM 2 alone does not see it; the union does.
        assert has_collection_denominator_scope(absent) is False
        row = _multi_block_row("country_watch_ir", blocks=[
            ("escalation", 0.15, real),
            ("narrative_coordination", 0.64, absent),
        ])
        block, ordinal, why = rr._lead_block(row["data"]["data"]["assembly"])
        assert (ordinal, why) == (1, rr.CARRY_BY_MASS)
        assert block["desk"] == "escalation"


def test_the_carry_and_the_crown_share_one_absence_definition():
    """Two tiers reaching different verdicts about the same sentence is how a
    regional read ends up disagreeing with the country read it is quoting. The
    rollup does not own a copy of this test — it IS the assembler's."""
    assert rr._is_absence_scoped is ap.block_is_absence


def test_a_mid_sentence_negation_is_still_carried():
    """The line the predicate draws, at the carry. Niger's military_posture
    block says 'shows no material shift' — a finding about Niger's posture, not
    a statement that the desk found nothing — so it stays eligible and is
    beaten on MASS, not excluded on shape."""
    row = _multi_block_row("country_watch_ir", blocks=[
        ("military_posture", 2.00,
         "Niger's standing high-alert military posture shows no material "
         "shift in this window."),
        ("escalation", 1.23, "Russian proxy support continues to drive risk."),
    ])
    block, ordinal, why = rr._lead_block(row["data"]["data"]["assembly"])
    assert (ordinal, why) == (1, rr.CARRY_BY_MASS)
    assert block["desk"] == "military_posture"
