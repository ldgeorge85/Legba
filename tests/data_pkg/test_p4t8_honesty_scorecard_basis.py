# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4-T8 honesty contract #1 — a colored band ⟺ a real basis id.

The invariant, restated as an exclusive-or over the WHOLE band engine + the
producer's row assembly:

    a scorecard dimension whose band is NOT ``insufficient-evidence`` MUST name
    ``>= 1`` real ``analyst_outputs.id`` in ``basis`` (each id == the exact
    finding that drove it), AND an ``insufficient-evidence`` dimension MUST carry
    ``basis == []``.

Never a colored band with an empty basis; never an insufficient band with a
basis. These tests FAIL the instant either half is violated — a fabricated band
(color with no evidence) or a phantom basis (evidence on an honest-empty card).

Pure functions only (no DB). Grounded in
``scorecard_banding.band_dimension`` (L235-313 — real band ⟹ ``basis=[finding_id]``;
``_insufficient`` L225-227 ⟹ ``basis=[]``) and
``scorecard_producer.build_scorecard_payload`` (L234-274 — copies the verdict
verbatim into ``data['bands']``) / ``basis_uuids_for_verdict`` (L187-217) /
``fold_unit_eval`` (L149-179).

Selectable via ``pytest -k p4t8_honesty``.
"""
from __future__ import annotations

import itertools
from uuid import UUID, uuid4

from legba.data.analysts.deterministic_handlers import scorecard_banding as sb
from legba.data.analysts.deterministic_handlers import scorecard_producer as sp


# ---------------------------------------------------------------------------
# Helper — the gathered-Claim shape from tests/data_pkg/test_scorecard_banding.py
# ---------------------------------------------------------------------------


def _claim(
    *,
    analyst_id="escalation",
    confidence=0.9,
    faithfulness=0.9,
    severity="high",
    extra_tags=(),
    finding_id=None,
    produced_at="2026-06-30T00:00:00+00:00",
):
    """A gathered Claim carrying a ``severity:<level>`` tag (unless severity=None)."""
    tags = list(extra_tags)
    if severity is not None:
        tags.append(f"severity:{severity}")
    return sb.Claim(
        finding_id=finding_id or str(uuid4()),
        analyst_id=analyst_id,
        confidence=confidence,
        faithfulness_score=faithfulness,
        tags=tuple(tags),
        produced_at=produced_at,
    )


def _is_uuid_str(raw) -> bool:
    try:
        UUID(str(raw))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# THE contract, restated as an XOR over the full band_dimension grid
# ---------------------------------------------------------------------------


def test_band_basis_xor_invariant_over_full_grid():
    """For EVERY point in the band_dimension input grid, exactly ONE of:

      (A) band != INSUFFICIENT  AND  len(basis) >= 1  AND every basis id equals
          the passed finding_id (a non-empty str), OR
      (B) band == INSUFFICIENT  AND  basis == [].

    Never a colored band with an empty basis; never insufficient with a basis.
    """
    severities = ("low", "moderate", "elevated", "high", "critical")
    confidences = (0.9, 0.5, 0.3499)          # confident / damped / below-floor
    faiths = (0.9, 0.45, None)                # ok / low-faith / verify-failed
    coerce_flags = (False, True)

    fid = str(uuid4())
    grid = itertools.product(severities, confidences, faiths, coerce_flags)
    claims = [
        _claim(
            confidence=conf,
            faithfulness=faith,
            severity=sev,
            extra_tags=("coerce_failed",) if coerce else (),
            finding_id=fid,
        )
        for sev, conf, faith, coerce in grid
    ]
    # The R0 no-finding leg — a dimension that never fired.
    claims.append(None)

    for claim in claims:
        v = sb.band_dimension(claim)
        colored = v.band != sb.INSUFFICIENT
        insufficient = v.band == sb.INSUFFICIENT
        # exactly one branch is true
        assert colored != insufficient
        if colored:
            assert len(v.basis) >= 1, f"colored band {v.band!r} with EMPTY basis"
            for bid in v.basis:
                assert isinstance(bid, str) and bid, "basis id is not a non-empty str"
                assert bid == fid, "colored band names a fabricated basis id"
        else:
            assert v.basis == [], f"insufficient band carries a basis {v.basis!r}"


def test_every_insufficient_reason_carries_empty_basis():
    """Each insufficient guard (R0..R3) returns basis=[] AND all-null numerics —
    a machine ``reason`` but never a fabricated band, never a synthesized id."""
    fid = str(uuid4())
    cases = {
        "no-finding": None,
        "verify-failed-none": _claim(faithfulness=None, finding_id=fid),
        "verify-failed-coerce": _claim(
            extra_tags=("coerce_failed",), finding_id=fid
        ),
        "low-faithfulness": _claim(confidence=0.9, faithfulness=0.45, finding_id=fid),
        "below-floor": _claim(confidence=0.3499, faithfulness=0.9, finding_id=fid),
        "no-severity-tag": _claim(
            confidence=0.9, faithfulness=0.9, severity=None, finding_id=fid
        ),
        "invalid-severity": _claim(
            confidence=0.9, faithfulness=0.9, severity="moderate-ish", finding_id=fid
        ),
    }
    for label, claim in cases.items():
        v = sb.band_dimension(claim)
        assert v.band == sb.INSUFFICIENT, f"{label}: expected insufficient"
        assert v.basis == [], f"{label}: insufficient band carries a basis"
        assert v.effective_confidence is None, f"{label}: numeric leaked"
        assert v.critic_score is None, f"{label}: critic score leaked"


# ---------------------------------------------------------------------------
# The producer row assembly never orphans a basis (band ⟺ basis, verbatim)
# ---------------------------------------------------------------------------


def test_build_scorecard_payload_bands_never_orphan_a_basis():
    fid_e = str(uuid4())
    fid_c = str(uuid4())
    verdict = sb.band_target(
        "target:usa",
        {"escalation": _claim(finding_id=fid_e)},
        composition=_claim(analyst_id="country_composition", finding_id=fid_c),
    )
    payload = sp.build_scorecard_payload("country_g20_us", verdict)

    # The producer copies the T1 verdict VERBATIM into data['bands'].
    assert payload.data["bands"] == verdict

    for dim in payload.data["bands"]["dimensions"].values():
        if dim["band"] != sb.INSUFFICIENT:
            assert dim["basis"], "colored dimension with an empty basis"
            for bid in dim["basis"]:
                assert _is_uuid_str(bid), "colored dimension basis is not a uuid"
        else:
            assert dim["basis"] == [], "insufficient dimension carries a basis"

    # derived_from == the UNION of the REAL basis ids (zero fabricated, zero
    # dangling): exactly the escalation finding + the composition finding.
    assert set(sp.basis_uuids_for_verdict(verdict)) == {UUID(fid_e), UUID(fid_c)}


def test_all_insufficient_card_is_emitted_with_no_basis():
    """A country with NO qualifying verified claim STILL emits a card — an
    all-insufficient scorecard tagged ``scorecard_all_insufficient`` with an
    EMPTY basis everywhere. Emitted, never omitted, never fabricated."""
    verdict = sb.band_target("target:usa", {}, composition=None)
    payload = sp.build_scorecard_payload("country_g20_us", verdict)

    assert "scorecard_all_insufficient" in payload.tags
    assert sp.basis_uuids_for_verdict(verdict) == []
    for dim in payload.data["bands"]["dimensions"].values():
        assert dim["band"] == sb.INSUFFICIENT
        assert dim["basis"] == []


def test_eval_fold_never_manufactures_a_basis():
    """Folding the per-unit eval attaches a display block but NEVER invents a
    basis id or flips a band off INSUFFICIENT — an unmeasured/measured eval is
    orthogonal to the band ⟺ basis contract."""
    fid_e = str(uuid4())
    verdict = sb.band_target(
        "target:usa", {"escalation": _claim(finding_id=fid_e)}, composition=None
    )
    # leadership_transition never fired → insufficient with basis [].
    insuff = verdict["dimensions"]["leadership_transition"]
    assert insuff["band"] == sb.INSUFFICIENT and insuff["basis"] == []
    colored = verdict["dimensions"]["escalation"]
    assert colored["band"] != sb.INSUFFICIENT and colored["basis"] == [fid_e]

    sp.fold_unit_eval(
        verdict,
        {"escalation": {"faithfulness": 0.9, "correctness_vs_reference": 0.8}},
        faith_floor=sb.FAITH_FLOOR,
    )

    # The eval block attached everywhere...
    assert "eval" in verdict["dimensions"]["leadership_transition"]
    assert "eval" in verdict["dimensions"]["escalation"]
    # ...but no basis was manufactured and no band flipped off INSUFFICIENT.
    assert verdict["dimensions"]["leadership_transition"]["basis"] == []
    assert verdict["dimensions"]["leadership_transition"]["band"] == sb.INSUFFICIENT
    assert verdict["dimensions"]["escalation"]["basis"] == [fid_e]
