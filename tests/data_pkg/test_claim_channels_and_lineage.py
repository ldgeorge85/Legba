# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A per-channel claim class, claim lineage one hop up, and the bearing
path (pure tests; every channel, group and organisation INVENTED).

* A Telegram channel marked ``claim`` (``config.classes`` + its own
  ``config.claims.<channel>`` declaration) is held to a claim source's rules:
  claimant per channel, stamped, cleaned, never mined into facts, and the
  descriptor cannot be active while the channel lacks its own sign-off.
* A composition citing a finding that rested ONLY on claim rows carries the
  claim stamp one hop up, and the composition floor refuses a clause that
  states the claimed event (``claim_as_fact``).
* A ``claim_watch`` bearing edge from a claim row says so on the edge.
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from legba.data import claim_hygiene as H
from legba.data import claim_support as CS
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources._contract import Signal

REPO = pathlib.Path(__file__).resolve().parents[2]
SIGN_OFF = {"by": "owner:example", "at": "2026-11-14T10:00:00Z",
            "note": "channel read for a week; posts are claims; no personal data seen"}
CLAIMS = {"exampleclaims": {"claimant": "Nightjar", "verb": "posted",
                            "claimant_aliases": ["NJ Team"]}}


def _tg_body(*, state: str = "draft", classes: dict | None = None,
             claims: dict | None = None, **top: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "api_id_secret": {"factory_kind": "secret", "raw": "source.telegram.api_id"},
        "api_hash_secret": {"factory_kind": "secret", "raw": "source.telegram.api_hash"},
        "session_secret": {"factory_kind": "secret", "raw": "source.telegram.session"},
        "channels": {"factory_kind": "list", "item_kind": "text",
                     "raw": ["examplenews", "exampleclaims"]},
        "classes": {"factory_kind": "dict", "key_kind": "text", "value_kind": "text",
                    "raw": classes if classes is not None else {"exampleclaims": "claim"}},
    }
    if claims is not None:
        config["claims"] = claims
    body = {
        "identity": {"id": "source.test.telegram", "name": "tg", "kind": "telegram_channel",
                     "schema_uri": "legba/source/1.0.0", "version": "0" * 16, "owner": "t",
                     "created": "2026-11-14T00:00:00Z", "state": state},
        "scope": {"source_class": "reporting", "access_class": "public"},
        "cadence": {"schedule": {"raw": "*/30 * * * *"}},
        "config": config,
        "pipeline": {"enrichment": [{"kind": "fact_extractor", "config": {}}], "media": "reference"},
    }
    body.update(top)
    return body


def _desc(**kw: Any) -> SourceDescriptor:
    return SourceDescriptor.model_validate(_tg_body(**kw), strict=False)


def _tg_config(**kw: Any):
    from legba.data.sources.telegram import TelegramChannelSourceConfig
    from legba.runtime.source_factory import _unwrap_factory_dict

    return TelegramChannelSourceConfig.model_validate(_unwrap_factory_dict(_tg_body(**kw)["config"]))


def _msg(channel: str, text: str) -> Signal:
    return Signal(source_id="source.test.telegram", canonical_url=f"https://t.me/{channel}/7",
                  payload={"text": text, "message_id": 7, "media_type": "photo",
                           "channel": {"username": channel, "id": 1, "title": channel}})


# ---------------------------------------------------------------------------
# 1. The per-channel class: declared, validated, signed off per channel
# ---------------------------------------------------------------------------


def test_a_claim_channel_validates_with_its_declaration():
    cfg = _tg_config(claims=CLAIMS)
    assert cfg.classes["exampleclaims"] == "claim"
    assert cfg.claims["exampleclaims"].claimant == "Nightjar"


def test_a_claim_channel_without_a_declaration_or_a_stray_declaration_is_refused():
    with pytest.raises(Exception, match="no config.claims"):
        _tg_config(claims={})
    with pytest.raises(Exception, match="does not mark 'claim'"):
        _tg_config(classes={}, claims=CLAIMS)


def test_an_active_descriptor_needs_every_claim_channel_signed_off():
    with pytest.raises(Exception, match="claim_sign_off_required"):
        _desc(state="active", claims=CLAIMS)
    signed = {"exampleclaims": {**CLAIMS["exampleclaims"], "sign_off": SIGN_OFF}}
    assert _desc(state="active", claims=signed).identity.state.value == "active"


def test_the_transition_refusal_names_the_unsigned_channels():
    d = _desc(state="configured", claims=CLAIMS)
    refusal = CS.claim_activation_refusal(d, "active")
    assert refusal["error"] == "claim_sign_off_required"
    assert refusal["channels"] == ["exampleclaims"]
    signed = _desc(state="configured",
                   claims={"exampleclaims": {**CLAIMS["exampleclaims"], "sign_off": SIGN_OFF}})
    assert CS.claim_activation_refusal(signed, "active") is None


def test_a_claim_channel_descriptor_may_not_follow_links():
    with pytest.raises(Exception, match="follow policy"):
        _desc(claims=CLAIMS, follow={"pipeline": "source.page.default", "same_host": True})


def test_the_live_telegram_descriptors_carry_no_claim_channel_and_still_parse():
    for name in ("source_telegram_monitor.yaml", "source_telegram_x1_staged.yaml"):
        body = yaml.safe_load((REPO / "descriptors" / name).read_text())
        d = SourceDescriptor.model_validate(body, strict=False)
        assert CS.channel_claims(d.config) == {}
        assert "claims" not in d.config
        assert "claim" not in set(((d.config.get("classes") or {}).get("raw") or {}).values())


# ---------------------------------------------------------------------------
# 2. A claim channel's rows: stamped per channel, cleaned, no facts mined
# ---------------------------------------------------------------------------


def test_only_the_claim_channels_rows_are_stamped_and_cleaned():
    d = _desc(claims=CLAIMS)
    claim_row = _msg("@ExampleClaims", "We hit Acme Widgets, files at https://x.example.net/a/b.zip")
    news_row = _msg("examplenews", "Report: https://news.example.org/story/1")
    before = news_row.model_dump_json()
    rep = H.prepare_claim_row(d, claim_row)
    assert rep is not None and rep.counts["url_to_host"] == 1
    stamp = claim_row.payload["claim"]
    assert stamp == {"source_class": "claim", "claimant": "Nightjar", "claimant_from": "declared",
                     "verb": "posted", "aliases": ["NJ Team"], "channel": "exampleclaims"}
    assert claim_row.payload["text"] == "We hit Acme Widgets, files at [link: x.example.net]"
    assert claim_row.canonical_url == "https://t.me/@ExampleClaims/7"   # its own t.me address, kept
    assert "media_type" not in claim_row.payload
    assert H.prepare_claim_row(d, news_row) is None
    assert news_row.model_dump_json() == before


def test_the_sink_stamps_the_claim_channel_and_strips_a_spoof_elsewhere(monkeypatch):
    from legba.runtime.sinks import signal as sink_mod

    written: list[Signal] = []

    async def _fake_write(conn, sig, **kw):
        written.append(sig)
        return None

    monkeypatch.setattr(sink_mod, "write_canonical_signal", _fake_write)
    sink = sink_mod.SignalSink(SimpleNamespace(descriptor=_desc(claims=CLAIMS), actor_id="a",
                                               _ingest_dedupe=None))
    ctx = SimpleNamespace(classification=())
    asyncio.run(sink.write(None, ctx, _msg("exampleclaims", "posted Acme Widgets")))
    assert written[-1].payload["claim"]["claimant"] == "Nightjar"
    spoof = _msg("examplenews", "x")
    spoof.payload["claim"] = {"source_class": "claim", "claimant": "me"}
    asyncio.run(sink.write(None, ctx, spoof))
    assert "claim" not in written[-1].payload


def test_the_fact_extractor_passes_a_stamped_channel_row_through():
    from legba.data.filters.fact_extractor import FactExtractorHandler

    handler = object.__new__(FactExtractorHandler)
    handler._signals_in = handler._signals_out = 0

    async def _boom(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("a claim channel row reached triple extraction")

    handler._extract_triples = _boom
    row = _msg("exampleclaims", "Nightjar hit Acme Widgets")
    H.prepare_claim_row(_desc(claims=CLAIMS), row)
    out = asyncio.run(handler.transform(row, SimpleNamespace(source_class="reporting")))
    assert out is row and handler._signals_out == 1


def test_event_candidates_read_the_row_stamp_before_the_descriptor_class():
    from legba.data.analysts.deterministic_handlers import _event_candidates as EC

    for sql in (EC._SIGNAL_SLICE_SQL, EC._SIGNAL_BY_ID_SQL):
        stamp_at = sql.index("s.payload->'claim'->>'source_class' = 'claim' THEN 'claim'")
        assert stamp_at < sql.index("sd.body->'scope'->>'source_class' IN")


# ---------------------------------------------------------------------------
# 3. Lineage one hop up
# ---------------------------------------------------------------------------

STAMP = {"source_class": "claim", "claimant": "Nightjar", "claimant_from": "declared", "verb": "posted"}


def _head(citations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": "00000000-0000-0000-0000-0000000000a1", "analyst_id": "unit.x",
            "title": "t", "body": "b", "data": {"data": {"citations": citations}}}


def test_a_head_resting_only_on_claims_stamps_its_composition_citation():
    from legba.data.analysts.composition_citations import _build_composition_citation

    only = _head([{"signal_id": "s1", "source_id": "source.a", "claim": STAMP},
                  {"signal_id": "s2", "source_id": "source.b",
                   "claim": {**STAMP, "claimant": "Corvid"}}])
    cit = _build_composition_citation(1, only)
    assert cit["claim"] == {"source_class": "claim", "claimant": "Nightjar", "claimant_from": "lineage",
                            "verb": "posted", "hop": 2, "claimants": ["Nightjar", "Corvid"]}
    mixed = _head([{"signal_id": "s1", "source_id": "source.a", "claim": STAMP},
                   {"signal_id": "s3", "source_id": "source.reuters.world"}])
    assert "claim" not in _build_composition_citation(1, mixed)
    assert "claim" not in _build_composition_citation(1, _head([]))


def test_the_composition_floor_refuses_a_claim_stated_as_fact_one_hop_up():
    from legba.data.provenance import verify as V

    cits = [{"marker": "[[ref:1]]", "ordinal": 1, "ref_id": "a1", "ref_kind": "finding",
             "claim": {**STAMP, "claimant_from": "lineage", "hop": 2}},
            {"marker": "[[ref:2]]", "ordinal": 2, "ref_id": "a2", "ref_kind": "finding"}]
    fact = V._deterministic_floor("Nightjar breached Acme Widgets [[ref:1]].", cits)
    assert [s.reason for s in fact.unsupported_spans] == ["claim_as_fact"]
    about = V._deterministic_floor("Nightjar claimed it breached Acme Widgets [[ref:1]].", cits)
    assert about.supported_claims == 1 and not about.unsupported_spans
    both = V._deterministic_floor("Acme Widgets was breached [[ref:1]][[ref:2]].", cits)
    assert both.supported_claims == 1
    assert not any(s.reason == "claim_as_fact" for s in both.unsupported_spans)


def test_a_composition_without_claim_lineage_is_unchanged():
    from legba.data.provenance import verify as V

    cits = [{"marker": "[[ref:1]]", "ordinal": 1, "ref_id": "a1", "ref_kind": "finding"}]
    r = V._deterministic_floor("Acme Widgets was breached [[ref:1]].", cits)
    assert r.supported_claims == 1 and not r.unsupported_spans


# ---------------------------------------------------------------------------
# 4. The bearing path
# ---------------------------------------------------------------------------


def _edge(**kw: Any):
    from datetime import datetime, timezone

    from legba.data.analysts.deterministic_handlers.bearing_gate import EdgeCandidate

    now = datetime.now(timezone.utc)
    return EdgeCandidate(signal_id="s", signal_as_of=now, signal_text="", question_id="q",
                         question_as_of=now, question_thesis="t", weight=0.5, **kw)


def test_a_claim_rows_bearing_edge_says_it_is_a_claim():
    assert _edge().data_payload() == {}                       # gate off, not a claim: 0116 default
    assert _edge(claim=STAMP).data_payload() == {"evidence_class": "claim", "claimant": "Nightjar"}
    gated = _edge(claim=STAMP, gate="yes", gate_ref="llm.x").data_payload()
    assert gated["evidence_class"] == "claim" and gated["bearing_gate"] == "yes"
    assert "evidence_class" not in _edge(gate="yes", gate_ref="llm.x").data_payload()


def test_claim_watch_reads_the_stamp_off_the_signal_row():
    import inspect

    from legba.data.analysts.deterministic_handlers import claim_watch as CW

    src = inspect.getsource(CW)
    assert 's_claim = claim_of(_parse_jsonish(s["payload"]))' in src
    assert "claim=s_claim" in src
