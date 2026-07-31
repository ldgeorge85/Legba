# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DQ Phase 3 / C2 — publisher-origin geo must stay OUT of the ``geo`` column.

The per-source baseline used to stamp the source's ``scope_geo`` (the PUBLISHER'S
origin — anadolu=TR, tass=RU, cna=SG) straight into the indexed ``signals.geo``
column, BEFORE the in-body geocoder ran. Because the enrichment promote step only
APPENDS the in-body ISO to ``geo`` (never demotes the hint), a state wire's WORLD
story ended up double-tagged (a Cuba-war Anadolu story landed geo={TR,US}) and
every desk that subscribes on ``geo && {its ISO}`` read that wire's whole world
output as "its" country.

These tests pin the fix:
  * the origin is parked in ``payload.publisher_origin`` and NOT stamped into
    ``geo`` at tier-1 enrichment;
  * when the body resolves a DIFFERENT country, that in-body country is what
    ``geo`` carries — the origin never appears;
  * (S-2) the origin is applied to ``geo`` as a post-enrichment fallback ONLY
    when the story CONTENT corroborates it (the country is named in the body or
    carried as a country-class NER entity). A world story from a state wire
    whose body names no country stays geo-unattributed — never stamped with the
    outlet's home country. This is the fix for the Singapore-outlet (CNA)
    LeBron/OpenAI/F1 stories that were all landing geo=SG and flooding SG's desk.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
)
from legba.data.sources.baseline import _enrich_structured, run_baseline


class _Cfg(BaseModel):
    pass


def _ctx(scope_geo: list[str]) -> SourceContext:
    return SourceContext(
        target_id="t",
        target_version="v",
        source_id="source.anadolu.english",
        config=_Cfg(),
        state_store=InMemoryStateStore(),
        scope_geo=scope_geo,
    )


def _sig(**payload) -> Signal:
    return Signal(source_id="source.anadolu.english", payload=dict(payload))


async def _resolve_us(signal: Signal, ctx: SourceContext) -> Signal:
    """Fake enrichment_stage mimicking geocode + the dapr_host promote step:
    resolves an IN-BODY country (US) different from the publisher origin (TR)."""
    signal.payload = {**signal.payload, "geo": {"country_iso2": "US"}}
    if "US" not in signal.geo:
        signal.geo.append("US")
    return signal


async def _resolve_nothing(signal: Signal, ctx: SourceContext) -> Signal:
    """Fake enrichment_stage that resolves no in-body geo (geo stays empty)."""
    return signal


# --- tier-1: origin parked, never stamped into geo --------------------------


def test_enrich_structured_parks_origin_and_leaves_geo_empty():
    sig = _sig(title="Cuba to end war-era rationing")
    _enrich_structured(sig, _ctx(["TR"]))
    assert sig.geo == []                                  # origin NOT in geo
    assert sig.payload["publisher_origin"] == ["TR"]      # parked instead


def test_enrich_structured_no_scope_geo_parks_nothing():
    sig = _sig(title="x")
    _enrich_structured(sig, _ctx([]))
    assert sig.geo == []
    assert "publisher_origin" not in sig.payload


# --- in-body country beats the publisher origin -----------------------------


@pytest.mark.asyncio
async def test_run_baseline_inbody_country_beats_publisher_origin():
    sig = _sig(title="Cuba to end war-era rationing")
    out = await run_baseline(sig, _ctx(["TR"]), enrichment_stage=_resolve_us)
    assert out is not None
    assert out.geo == ["US"]                              # in-body country kept
    assert "TR" not in out.geo                            # origin NOT tagged
    assert out.payload["publisher_origin"] == ["TR"]      # origin still parked


# --- fallback: origin applies ONLY when the body corroborates it (S-2) ------


@pytest.mark.asyncio
async def test_run_baseline_fallback_applies_origin_when_body_corroborates():
    # Genuinely-domestic story that NAMES its own country: the geocoder happened
    # to miss it (stub resolves nothing), but the body corroborates TR, so the
    # publisher-origin fallback legitimately tags it.
    sig = _sig(title="Türkiye passes 2027 budget")
    out = await run_baseline(sig, _ctx(["TR"]), enrichment_stage=_resolve_nothing)
    assert out is not None
    assert out.geo == ["TR"]                              # corroborated → tagged
    assert out.payload["publisher_origin"] == ["TR"]


@pytest.mark.asyncio
async def test_run_baseline_fallback_corroborated_by_country_entity():
    # No country name in the free text, but a country-class NER entity attests
    # the origin — that counts as corroboration.
    sig = _sig(
        title="Central bank holds rates steady",
        entities=[{"class": "country", "text": "Türkiye"}],
    )
    out = await run_baseline(sig, _ctx(["TR"]), enrichment_stage=_resolve_nothing)
    assert out is not None
    assert out.geo == ["TR"]


@pytest.mark.asyncio
async def test_run_baseline_withholds_origin_when_body_does_not_corroborate():
    # S-2: a Singapore wire's world story (LeBron) — the body names no country,
    # so the SG origin must NOT be stamped. Under the old rule this landed
    # geo=SG and flooded Singapore's country desk.
    sig = _sig(
        title="LeBron James re-signs with the Los Angeles Lakers",
        text="The NBA star agreed to a two-year contract extension.",
    )
    out = await run_baseline(sig, _ctx(["SG"]), enrichment_stage=_resolve_nothing)
    assert out is not None
    assert out.geo == []                                  # NOT geo=SG
    assert out.payload["publisher_origin"] == ["SG"]      # origin still parked


@pytest.mark.asyncio
async def test_run_baseline_no_enrichment_stage_corroborated_still_falls_back():
    # No enrichment chain (bare source): a domestic story that names its country
    # still gets the home-country tag via the corroboration-gated fallback.
    sig = _sig(title="Türkiye passes 2027 budget")
    out = await run_baseline(sig, _ctx(["TR"]))
    assert out is not None
    assert out.geo == ["TR"]
    assert out.payload["publisher_origin"] == ["TR"]


@pytest.mark.asyncio
async def test_run_baseline_no_enrichment_stage_withholds_uncorroborated():
    # Bare source, world story, no country in the body → no origin tag.
    sig = _sig(title="LeBron James re-signs with the Los Angeles Lakers")
    out = await run_baseline(sig, _ctx(["SG"]))
    assert out is not None
    assert out.geo == []
    assert out.payload["publisher_origin"] == ["SG"]


# --- R4: a title naming a DIFFERENT country contradicts the origin ----------


@pytest.mark.asyncio
async def test_run_baseline_title_country_contradicts_origin():
    # LIVE R4: a BBC Greece-wildfires story landed geo=GB because "the UK
    # Foreign Office" appeared deep in the body and attested the outlet's own
    # origin. The title names Greece and not the UK, so the origin is
    # CONTRADICTED — the signal stays unattributed rather than mis-routed.
    sig = _sig(
        title="Greece wildfires force thousands to flee homes",
        text=(
            "Firefighters battled blazes near Athens overnight. The UK Foreign "
            "Office advised travellers to check local guidance."
        ),
    )
    out = await run_baseline(sig, _ctx(["GB"]), enrichment_stage=_resolve_nothing)
    assert out is not None
    assert out.geo == []                                  # NOT geo=GB
    assert out.payload["publisher_origin"] == ["GB"]


@pytest.mark.asyncio
async def test_run_baseline_title_naming_origin_still_corroborates():
    # The guard only fires on CONTRADICTION: a title that names the origin
    # itself (alongside another country) still corroborates.
    sig = _sig(
        title="UK and Greece sign wildfire response pact",
        text="Officials met in London to finalise the agreement.",
    )
    out = await run_baseline(sig, _ctx(["GB"]), enrichment_stage=_resolve_nothing)
    assert out is not None
    assert out.geo == ["GB"]


@pytest.mark.asyncio
async def test_run_baseline_no_country_in_title_keeps_body_corroboration():
    # With no country named in the title at all, body-wide corroboration is
    # unchanged (the pre-R4 behavior).
    sig = _sig(
        title="Central bank holds rates steady",
        text="Türkiye's central bank left its policy rate unchanged.",
    )
    out = await run_baseline(sig, _ctx(["TR"]), enrichment_stage=_resolve_nothing)
    assert out is not None
    assert out.geo == ["TR"]
