# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CLAIM source class, enforced by the platform (pure tests).

A source with ``scope.source_class: claim`` publishes a party's own claim. Its
rows may ground only "<claimant> claimed/posted/listed ...", never the event,
never a corroboration, never a credibility input; it is text only; it cannot
go active without a recorded sign-off. The database halves (the intake
refusal, the fact write path, the document-sink stamp, the transition route)
are in ``test_claim_source_class_db.py``.
"""

from __future__ import annotations

from tests._lifecycle_rows import lifecycle_text
import asyncio
import pathlib
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml

from legba.data import claim_support as CS
from legba.data.schemas.source import SourceDescriptor, SourceScope
from legba.data.sources._contract import Signal

REPO = pathlib.Path(__file__).resolve().parents[2]
DESCRIPTORS = REPO / "descriptors"

DECL = {"claimant": "Example Tracker (leak-site tracker)", "verb": "listed"}
SIGN_OFF = {"by": "owner:alex", "at": "2026-11-14T10:00:00Z", "note": "terms read; metadata only"}


def _body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "identity": {
            "id": "source.test.claims", "name": "claims", "kind": "rss",
            "schema_uri": "legba/source/1.0.0", "version": "0" * 16,
            "owner": "t", "created": "2026-11-14T00:00:00Z", "state": "draft",
        },
        "scope": {"source_class": "claim", "claim": dict(DECL), "access_class": "public"},
        "cadence": {"schedule": {"raw": "0 * * * *"}},
        "config": {"url": {"factory_kind": "text", "raw": "https://tracker.example.org/rss.xml"}},
    }
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(body.get(key), dict):
            body[key] = {**body[key], **val}
        else:
            body[key] = val
    return body


def _validate(body: dict[str, Any]) -> SourceDescriptor:
    return SourceDescriptor.model_validate(body, strict=False)


# ---------------------------------------------------------------------------
# 1. The declared class + the claim declaration
# ---------------------------------------------------------------------------


def test_claim_class_validates_with_its_declaration():
    d = _validate(_body())
    assert d.scope.source_class == "claim"
    assert d.scope.claim.claimant == DECL["claimant"]
    assert d.scope.claim.verb == "listed"
    assert d.scope.claim.sign_off is None


def test_claim_class_without_declaration_is_refused():
    with pytest.raises(Exception, match="needs a scope.claim block"):
        SourceScope.model_validate({"source_class": "claim"})


def test_claim_declaration_on_a_non_claim_source_is_refused():
    with pytest.raises(Exception, match="only for source_class 'claim'"):
        SourceScope.model_validate({"source_class": "reporting", "claim": DECL})


def test_claim_block_is_hash_neutral_for_every_other_source():
    dumped = SourceScope().model_dump(mode="json")
    assert "claim" not in dumped


def test_claimant_path_must_be_a_dotted_path():
    with pytest.raises(Exception, match="dotted payload path"):
        SourceScope.model_validate(
            {"source_class": "claim", "claim": {**DECL, "claimant_path": "item[0]"}}
        )


@pytest.mark.parametrize("blank", ["", "   "])
def test_sign_off_needs_who_and_why(blank: str):
    with pytest.raises(Exception):
        SourceScope.model_validate({"source_class": "claim", "claim": {
            **DECL, "sign_off": {**SIGN_OFF, "by": blank}}})


# ---------------------------------------------------------------------------
# 2. Text only, never a fact source — refused AT SAVE
# ---------------------------------------------------------------------------


def test_fact_extractor_refused_on_a_claim_source():
    with pytest.raises(Exception, match="may not run pipeline stage 'fact_extractor'"):
        _validate(_body(pipeline={"enrichment": [{"kind": "fact_extractor", "config": {}}]}))


def test_follow_policy_refused_on_a_claim_source():
    with pytest.raises(Exception, match="may not declare a follow policy"):
        _validate(_body(follow={"pipeline": "source.page.default", "same_host": True}))


def test_eager_media_refused_on_a_claim_source():
    with pytest.raises(Exception, match="text only"):
        _validate(_body(pipeline={"media": "eager"}))


def test_object_store_refused_on_a_claim_source():
    with pytest.raises(Exception, match="object_store"):
        _validate(_body(deps={"object_store": True}))


def test_active_claim_source_needs_a_sign_off():
    with pytest.raises(Exception, match="claim_sign_off_required"):
        _validate(_body(identity={"state": "active"}))
    signed = _validate(_body(identity={"state": "active"},
                             scope={"claim": {**DECL, "sign_off": SIGN_OFF}}))
    assert signed.scope.claim.sign_off.by == "owner:alex"


def test_transition_refusal_body():
    d = _validate(_body(identity={"state": "configured"}))
    refusal = CS.claim_activation_refusal(d, "active")
    assert refusal["error"] == "claim_sign_off_required"
    assert "no recorded sign-off" in refusal["message"]
    assert CS.claim_activation_refusal(d, "paused") is None
    signed = _validate(_body(scope={"claim": {**DECL, "sign_off": SIGN_OFF}}))
    assert CS.claim_activation_refusal(signed, "active") is None
    ordinary = _validate(_body(scope={"source_class": "reporting", "claim": None}))
    assert CS.claim_activation_refusal(ordinary, "active") is None


def test_telegram_per_channel_claim_class_needs_its_declaration():
    """Lifted C-1's refusal: a channel marked ``claim`` is a claim source
    of its own and must carry ``config.claims.<channel>`` (the per-channel
    rules are in ``test_claim_channels_and_lineage.py``)."""
    from legba.data.sources.telegram import TelegramChannelSourceConfig

    with pytest.raises(Exception, match="no config.claims"):
        TelegramChannelSourceConfig.model_validate({
            "channels": ["somechannel"], "classes": {"somechannel": "claim"},
            "session_secret": "x", "api_id_secret": "y", "api_hash_secret": "z",
        })


# ---------------------------------------------------------------------------
# 3. The claim descriptors that ship
# ---------------------------------------------------------------------------


def test_ransomware_live_descriptor_is_a_claim_source():
    from legba.data.sources.rss import RSSConfig
    from legba.runtime.source_factory import _unwrap_factory_dict

    body = yaml.safe_load((DESCRIPTORS / "source_ransomware_live.yaml").read_text())
    d = _validate(body)
    assert d.identity.id == "source.ransomware_live.claims"
    assert d.identity.state.value == "draft"
    assert d.scope.source_class == "claim"
    assert d.scope.claim.sign_off is not None
    assert d.scope.claim.claimant_path is None      # feed structure unverified
    kinds = [s.kind for s in d.pipeline.enrichment]
    assert "fact_extractor" not in kinds and "geocode" not in kinds
    assert d.follow is None and d.pipeline.media == "reference"
    cfg = RSSConfig.model_validate(_unwrap_factory_dict(d.config))
    assert cfg.url == "https://www.ransomware.live/rss.xml"


def test_ransomlook_descriptor_is_a_claim_source_with_a_recorded_sign_off():
    from legba.data.sources.rss import RSSConfig
    from legba.runtime.source_factory import _unwrap_factory_dict

    body = yaml.safe_load((DESCRIPTORS / "source_ransomlook.yaml").read_text())
    d = _validate(body)
    assert d.identity.id == "source.ransomlook.claims"
    assert d.identity.state.value == "draft"
    assert d.scope.source_class == "claim"
    assert d.scope.claim.verb == "listed"
    assert d.scope.claim.sign_off is not None and d.scope.claim.sign_off.by == "owner"
    assert d.scope.claim.claimant_path is None      # group is in the title text only
    kinds = [s.kind for s in d.pipeline.enrichment]
    assert "fact_extractor" not in kinds and "geocode" not in kinds
    assert d.follow is None and d.pipeline.media == "reference"
    cfg = RSSConfig.model_validate(_unwrap_factory_dict(d.config))
    assert cfg.url == "https://www.ransomlook.io/rss.xml"
    lifecycle = lifecycle_text(DESCRIPTORS)
    # active on the reference host: the overlay row says so, or a walk would switch it off
    assert re.search(r"^\| source \| source\.ransomlook\.claims \| active \| draft \|$",
                     lifecycle, re.M)


@pytest.mark.parametrize("fname,sid,url", [
    ("source_checkpoint_research.yaml", "source.checkpoint_research.news",
     "https://research.checkpoint.com/feed/"),
    ("source_cloudflare_ddos.yaml", "source.cloudflare_ddos.news",
     "https://blog.cloudflare.com/tag/ddos/rss/"),
])
def test_vendor_reporting_descriptors_are_ordinary_reporting_sources(fname, sid, url):
    from legba.data.sources.rss import RSSConfig
    from legba.runtime.source_factory import _unwrap_factory_dict

    d = _validate(yaml.safe_load((DESCRIPTORS / fname).read_text()))
    assert d.identity.id == sid
    assert d.identity.state.value == "draft"
    assert d.scope.source_class == "reporting" and d.scope.claim is None
    assert d.scope.access_class == "public"
    cfg = RSSConfig.model_validate(_unwrap_factory_dict(d.config))
    assert cfg.url == url
    lifecycle = lifecycle_text(DESCRIPTORS)
    # active since 2026-11-16: the overlay row says so, or a walk would switch the feed off
    assert re.search(rf"^\| source \| {re.escape(sid)} \| active \| draft \|$",
                     lifecycle, re.M)


def test_only_the_two_tracker_claim_descriptors_ship():
    claims = []
    for path in sorted(DESCRIPTORS.glob("source_*.yaml")):
        body = yaml.safe_load(path.read_text()) or {}
        if (body.get("scope") or {}).get("source_class") == "claim":
            claims.append(path.name)
    assert claims == ["source_ransomlook.yaml", "source_ransomware_live.yaml"]


# ---------------------------------------------------------------------------
# 4. The ingest stamp: claimant from the feed field, else the declaration
# ---------------------------------------------------------------------------


def test_stamp_takes_the_declared_claimant_without_a_feed_field():
    scope = SourceScope.model_validate({"source_class": "claim", "claim": DECL})
    stamp = CS.claim_stamp(scope, {"title": "x", "author": "LockBit"})
    assert stamp == {"source_class": "claim", "claimant": DECL["claimant"],
                     "claimant_from": "declared", "verb": "listed"}


def test_stamp_takes_the_feed_field_when_declared_and_present():
    scope = SourceScope.model_validate(
        {"source_class": "claim", "claim": {**DECL, "claimant_path": "item.group_name"}})
    stamp = CS.claim_stamp(scope, {"item": {"group_name": " akira "}})
    assert stamp["claimant"] == "akira" and stamp["claimant_from"] == "feed_field"
    # An empty / absent field falls back to the declaration — never a guess.
    fallback = CS.claim_stamp(scope, {"item": {"group_name": ""}})
    assert fallback["claimant"] == DECL["claimant"] and fallback["claimant_from"] == "declared"


def test_no_stamp_for_any_other_source():
    assert CS.claim_stamp(SourceScope(), {"title": "x"}) is None


def test_strip_claim_media_leaves_text_only():
    sig = Signal(source_id="source.test.claims", media_ref="https://x/img.png",
                 object_ref="cas:sha256/aa", payload={"title": "t", "media": {"a": 1},
                                                      "enclosures": [1], "summary": "s"})
    CS.strip_claim_media(sig)
    assert sig.media_ref is None and sig.object_ref is None
    assert sig.payload == {"title": "t", "summary": "s"}


def test_sink_stamps_claims_and_strips_a_spoofed_stamp(monkeypatch):
    """SignalSink.write: a claim source's row gets the stamp (overwriting a
    feed-supplied one); a non-claim source's row loses any claim-shaped key."""
    from legba.runtime.sinks import signal as sink_mod

    written: list[Signal] = []

    async def _fake_write(conn, sig, **kw):
        written.append(sig)
        return None   # "nothing written" — the sink stops before dedupe/fan-out

    monkeypatch.setattr(sink_mod, "write_canonical_signal", _fake_write)

    def _core(scope_body: dict[str, Any]) -> Any:
        d = _validate(_body(scope=scope_body))
        return SimpleNamespace(descriptor=d, actor_id="a", _ingest_dedupe=None)

    claim_sink = sink_mod.SignalSink(_core({}))
    sig = Signal(source_id="source.test.claims", media_ref="https://x/y.png",
                 payload={"title": "Group posted Acme", "claim": {"source_class": "reporting"}})
    asyncio.run(claim_sink.write(None, SimpleNamespace(classification=()), sig))
    assert written[-1].payload["claim"]["claimant"] == DECL["claimant"]
    assert written[-1].payload["claim"]["source_class"] == "claim"
    assert written[-1].media_ref is None

    plain_sink = sink_mod.SignalSink(_core({"source_class": "reporting", "claim": None}))
    spoof = Signal(source_id="source.test.claims",
                   payload={"title": "x", "claim": {"source_class": "claim", "claimant": "me"}})
    asyncio.run(plain_sink.write(None, SimpleNamespace(classification=()), spoof))
    assert "claim" not in written[-1].payload


# ---------------------------------------------------------------------------
# 5. What a claim row may support
# ---------------------------------------------------------------------------

CLAIM = {"source_class": "claim", "claimant": "LockBit", "verb": "posted"}


@pytest.mark.parametrize("sentence,ok", [
    ("LockBit posted Acme Corp as a victim on 12 November [1].", True),
    ("LockBit claimed an attack on Acme Corp [1].", True),
    ("Acme Corp was named, according to LockBit, on 12 November [1].", True),
    ("Acme Corp was breached by ransomware [1].", False),
    ("LockBit breached Acme Corp [1].", False),
    ("Akira claimed an attack on Acme Corp [1].", False),       # wrong claimant
])
def test_claim_sentence_attributed(sentence: str, ok: bool):
    assert CS.claim_sentence_attributed(sentence, CLAIM) is ok


def test_declared_claimant_matches_on_its_leading_name():
    decl = {"source_class": "claim", "claimant": "ransomware.live (leak-site tracker)"}
    assert CS.claim_sentence_attributed("ransomware.live listed Acme Corp on 12 November [2].", decl)


def test_verify_floor_refuses_a_claim_as_fact():
    from legba.data.provenance import verify as V

    cits = [
        {"marker": "[1]", "signal_id": "s1", "source_id": "source.test.claims", "claim": CLAIM},
        {"marker": "[2]", "signal_id": "s2", "source_id": "source.reuters.world"},
    ]
    fact = V._deterministic_floor("Acme Corp was breached by ransomware [1].", cits)
    assert fact.supported_claims == 0
    assert [s.reason for s in fact.unsupported_spans] == ["claim_as_fact"]
    assert V.fail_class_for_reason("claim_as_fact") == V.FAIL_CLASS_SOFT
    about = V._deterministic_floor("LockBit posted Acme Corp as a victim on its site [1].", cits)
    assert about.supported_claims == 1 and not about.unsupported_spans
    # A non-claim row carries the event; the claim row is then just extra.
    both = V._deterministic_floor("Acme Corp was breached by ransomware [1][2].", cits)
    assert both.supported_claims == 1 and not both.unsupported_spans


def test_verify_floor_is_unchanged_without_claim_citations():
    from legba.data.provenance import verify as V

    cits = [{"marker": "[1]", "signal_id": "s1", "source_id": "source.reuters.world"}]
    r = V._deterministic_floor("Acme Corp was breached by ransomware [1].", cits)
    assert r.supported_claims == 1


def test_judge_evidence_names_a_claim_row():
    assert "CLAIM ONLY" in CS.claim_judge_line(CLAIM)
    assert "never that the claimed event happened" in CS.claim_judge_line(CLAIM)


# ---------------------------------------------------------------------------
# 6. Never an independent source, never a corroboration
# ---------------------------------------------------------------------------


def test_independence_never_counts_a_claim_citation():
    from legba.data.analysts.source_independence import independence_of

    cits = [
        {"signal_id": "s1", "source_id": "source.reuters.world", "title": "Acme hit"},
        {"signal_id": "s2", "source_id": "source.test.claims", "title": "Acme listed",
         "claim": CLAIM},
    ]
    ind = independence_of(cits)
    assert ind.sources == 1 and ind.independent == 1
    assert independence_of([cits[1]]).sources == 0


def _ev(source_id: str, source_class: str = "reporting"):
    from legba.data.analysts.deterministic_handlers._event_matcher import EventEvidence

    return EventEvidence(
        id=uuid4(), title="Acme Corp ransomware attack", category="conflict",
        fetched_at=datetime.now(timezone.utc), source_id=source_id,
        source_class=source_class, geo=("us",), entity_names=("Acme Corp",),
    )


def test_event_promotion_never_counts_a_claim_member():
    from legba.data.analysts.deterministic_handlers._event_candidates import cluster_candidate
    from legba.data.analysts.deterministic_handlers._event_matcher import EventCluster

    only_claims = EventCluster(members=(_ev("source.a.claims", "claim"),
                                        _ev("source.b.claims", "claim")), oversized=False)
    cand = cluster_candidate(only_claims)
    assert not cand.promoted and cand.decline_reason == "claim_only"

    one_plus_claim = EventCluster(members=(_ev("source.reuters.world"),
                                           _ev("source.a.claims", "claim")), oversized=False)
    cand = cluster_candidate(one_plus_claim)
    assert not cand.promoted and cand.decline_reason == "single_source"
    assert cand.distinct_source_count == 1

    two_plus_claim = EventCluster(members=(_ev("source.reuters.world"), _ev("source.ap.world"),
                                           _ev("source.a.claims", "claim")), oversized=False)
    assert cluster_candidate(two_plus_claim).promoted


def test_event_link_model_admits_the_claim_class():
    from legba.data.provenance.models import EventSignalLinkPayload

    link = EventSignalLinkPayload(signal_id=uuid4(), linked_at=datetime.now(timezone.utc),
                                  source_class="claim")
    assert link.source_class == "claim"


def test_counting_sql_paths_exclude_claim_rows():
    """Every SQL counting path carries the shared not-a-claim predicate."""
    from legba.data.analysts import edge_qualification as EQ
    from legba.data.analysts.deterministic_handlers import _event_lifecycle as EL
    from legba.data.analysts.deterministic_handlers import evidence_archiver as EA
    from legba.data.analysts.deterministic_handlers import source_track_record as TR

    pred = CS.NOT_CLAIM_SIGNAL_SQL.format(alias="s")
    assert pred in TR._RECORDS_SQL
    assert pred in EQ.POOL_SCORING_SQL and pred in EQ.EVIDENCE_CTES
    assert pred in EA._SELECT_CANDIDATES_SQL
    assert EL._OPEN_EVENT_STATS_SQL_FANNED.count("FILTER (WHERE sel.source_class <> 'claim')") == 1


def test_institutional_lift_never_applies_to_a_claim():
    from legba.data.analysts.salience_institutional import (
        NO_LIFT_SOURCE_CLASSES,
        recognise_institutional_change,
    )

    assert "claim" in NO_LIFT_SOURCE_CLASSES
    assert recognise_institutional_change(
        "President resigns, group claims", source_class="claim") is None


def test_contrary_pass_fences_a_claim_host():
    from legba.data.analysts.deterministic_handlers import _contrary_fences as cf

    catalog = {"tracker.example.org": "claim"}
    assert cf.host_class_of("https://tracker.example.org/post/1", catalog) == "claim"
    assert "claim" in cf.FENCED_HOST_CLASSES
    migration = (REPO / "src/legba/data/migrations/0258_claim_host_class.sql").read_text()
    assert "'claim'" in migration


def test_fact_extractor_backstop_passes_a_claim_signal_through():
    from legba.data.filters.fact_extractor import FactExtractorHandler

    handler = object.__new__(FactExtractorHandler)
    handler._signals_in = 0
    handler._signals_out = 0

    async def _boom(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("a claim signal reached triple extraction")

    handler._extract_triples = _boom
    sig = Signal(source_id="source.test.claims", payload={"title": "Group claims Acme"})
    out = asyncio.run(handler.transform(sig, SimpleNamespace(source_class="claim")))
    assert out is sig and handler._signals_out == 1


# ---------------------------------------------------------------------------
# 7. Text only: links are never followed
# ---------------------------------------------------------------------------


def test_follow_step_never_runs_for_a_claim_source(monkeypatch):
    from legba.runtime import intake_follow as F

    monkeypatch.setattr(F, "follow_enabled", lambda: True)
    core = SimpleNamespace(descriptor=SimpleNamespace(
        follow=SimpleNamespace(kinds=["anchor"]),
        scope=SimpleNamespace(source_class="claim"),
        identity=SimpleNamespace(id="source.test.claims"),
    ))
    sig = Signal(source_id="source.test.claims", payload={"summary": "see https://x.org/a"})
    assert asyncio.run(F.follow_written(core, [sig])) == 0


def test_claim_link_refusal_is_a_named_reason():
    from legba.data.intake_contract import REFUSAL_REASONS

    assert "claim_link_refused" in REFUSAL_REASONS


# ---------------------------------------------------------------------------
# 8. Presentation: slice label, feed roster line, API fields
# ---------------------------------------------------------------------------


def _row(n: int, *, claim: dict | None = None, source_id: str = "source.reuters.world"):
    data: dict[str, Any] = {"title": f"Row {n}", "summary": "body text"}
    if claim:
        data["claim"] = claim
    return {"id": str(uuid4()), "title": f"Row {n}", "data": data, "source_id": source_id,
            "source_url": f"https://e.org/{n}", "produced_at": "2026-11-14T00:00:00Z"}


def test_slice_row_label_names_the_claimant():
    from legba.data.analysts.slice_render import _render_signal

    claim_block = _render_signal(1, _row(1, claim=CLAIM, source_id="source.test.claims"))
    assert "CLAIM by LockBit" in claim_block
    assert "never the event itself" in claim_block
    assert "CLAIM by" not in _render_signal(2, _row(2))


def test_feed_roster_names_claim_rows_apart():
    from legba.data.analysts.feed_roster_block import build_feed_roster_block

    since = datetime(2026, 11, 14, tzinfo=timezone.utc)
    rows = [_row(1), _row(2, claim=CLAIM, source_id="source.test.claims")]
    block = build_feed_roster_block(rows, [("source.test.claims", "claim")], {}, since=since)
    assert "CLAIM rows" in block and "[2] (LockBit)" in block
    plain = build_feed_roster_block([_row(1)], [("source.reuters.world", "trade")], {}, since=since)
    assert "CLAIM rows" not in plain


def test_findings_projection_carries_the_claimant():
    from legba.data.findings_projection import JUDGMENT_FIELDS, project_citations

    assert "claimant" in JUDGMENT_FIELDS
    [c, p] = project_citations([
        {"ordinal": 1, "source_id": "source.test.claims", "claim": CLAIM},
        {"ordinal": 2, "source_id": "source.reuters.world"},
    ])
    assert c["claimant"] == "LockBit" and p["claimant"] is None


def test_signal_row_carries_the_class_and_claimant():
    from legba.data.registry.substrate_reads_api import _hydrate_signal

    base = {"id": uuid4(), "source_id": "source.test.claims", "fetched_at": datetime.now(timezone.utc),
            "canonical_url": "https://e.org/1", "derived_from": [], "schema_uri": "x",
            "source_credibility": None}
    claim_row = _hydrate_signal({**base, "payload": {"title": "t", "claim": CLAIM}})
    assert claim_row.evidence_class == "claim" and claim_row.claimant == "LockBit"
    plain = _hydrate_signal({**base, "payload": {"title": "t"}})
    assert plain.evidence_class is None and plain.claimant is None


def test_citation_index_carries_the_claim_stamp():
    from legba.data.analysts import inline_target as IT

    index = IT._build_citation_index([_row(1, claim=CLAIM), _row(2)])
    assert index[1]["claim"]["claimant"] == "LockBit"
    assert "claim" not in index[2]
    cit = IT._citation_from_index_entry(1, index[1])
    assert cit["claim"]["claimant"] == "LockBit"
