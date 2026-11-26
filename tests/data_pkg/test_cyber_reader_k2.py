# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cyber desk gets a reader.

Three pieces, each held on the path it really runs on:

  * ``cyber_watch`` — the ONE bounded unit over ``theme_cyber``. The
    descriptor parses on the runtime's own model, binds through the runtime's
    own target-binding function (``source_first_runtime._analyst_ids_for_
    target``) to the cyber desk and to nothing else in the tree, merges its
    ``method.options`` through the runtime's own X-1 merge with nothing
    rejected, and runs end to end through ``inline_target.run_method`` with a
    capturing stub LLM (no model is called) — whose user prompt carries the
    FEED ROSTER block.
  * ``feed_roster_block`` — the deterministic header block: pure render, the
    degrade path, and the count statement against a real migrated Postgres.
  * ``kev_digest`` — the deterministic digest: pure entry/line/finding shape,
    the dispatcher binding, and the watermark run against a real Postgres.
"""

from __future__ import annotations

from tests._lifecycle_rows import lifecycle_text
import json
import pathlib
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest
import yaml

from legba.data.analysts import feed_roster_block as FR
from legba.data.analysts.deterministic_handlers import kev_digest as KD
from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.schemas.target import TargetDescriptor

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DESC = _ROOT / "descriptors"
_UNIT_FILE = "analyst_cyber_watch.yaml"
_DIGEST_FILE = "analyst_kev_digest.yaml"


def _raw(fname: str) -> dict[str, Any]:
    return yaml.safe_load((_DESC / fname).read_text())


def _unit() -> AnalystDescriptor:
    return AnalystDescriptor.model_validate(_raw(_UNIT_FILE))


def _prompt() -> str:
    return _raw(_UNIT_FILE)["method"]["system_prompt"]


def _norm(text: str) -> str:
    return " ".join(text.split())


def _cron_hours(expr: str) -> set[int]:
    return {int(h) for h in expr.split()[1].split(",")}


# ---------------------------------------------------------------------------
# 1. The unit descriptor — parses on the runtime, binds to the desk only
# ---------------------------------------------------------------------------


def test_both_descriptors_parse_on_the_runtime_model_and_ship_draft():
    unit = _unit()
    digest = AnalystDescriptor.model_validate(_raw(_DIGEST_FILE))
    assert (unit.identity.id, unit.identity.kind) == ("cyber_watch", "inline_target")
    assert (digest.identity.id, digest.identity.kind) == ("kev_digest", "deterministic")
    assert unit.identity.state.value == "draft"
    assert digest.identity.state.value == "draft"


def _tree_targets() -> list[TargetDescriptor]:
    """Every target file the walker registers (RETIRED.md's parked pre-pivot
    examples carry a shape the current schema refuses, and never register)."""
    retired = (_DESC / "RETIRED.md").read_text()
    out = []
    for path in sorted(_DESC.glob("target_*.yaml")):
        body = yaml.safe_load(path.read_text())
        if f"| {body['identity']['id']} |" in retired:
            continue
        out.append(TargetDescriptor.model_validate(body, strict=False))
    assert len(out) >= 45
    return out


def test_the_runtime_binding_puts_the_unit_on_the_cyber_desk_and_nowhere_else():
    """The REAL binding function the runtime boot-wires with: the unit's
    ``has_tag("cyber")`` selector is evaluated against every target file in
    the tree, and only theme_cyber matches."""
    from legba.runtime.source_first_runtime import _analyst_ids_for_target

    analysts = {"cyber_watch": {"body": _raw(_UNIT_FILE)}}
    bound = [t.identity.id for t in _tree_targets()
             if "cyber_watch" in _analyst_ids_for_target(t, analysts)]
    assert bound == ["theme_cyber"]


def test_no_other_analyst_binds_to_the_cyber_desk():
    """Activating the desk fans no OTHER analyst of any kind onto it (a
    selector with no predicate would match every target): the roll can flip
    theme_cyber active knowing exactly one reader wakes."""
    from legba.runtime.source_first_runtime import _analyst_ids_for_target

    desk = next(t for t in _tree_targets() if t.identity.id == "theme_cyber")
    analysts = {}
    for path in sorted(_DESC.glob("analyst_*.yaml")):
        body = yaml.safe_load(path.read_text())
        analysts[body["identity"]["id"]] = {"body": body}
    assert _analyst_ids_for_target(desk, analysts) == ["cyber_watch"]


def test_the_unit_options_merge_through_the_runtime_with_nothing_rejected():
    from legba.runtime.dapr_actors import _merge_descriptor_options
    from legba.data.slice_coverage import watermark_config

    unit = _unit()
    options: dict[str, Any] = {}
    receipt = _merge_descriptor_options(options, unit, actor_id="test")
    assert receipt is not None and not receipt.get("rejected"), receipt
    assert options["slice_since_watermark"] is True
    assert options["judge_sample_rate"] == 1.0
    assert FR.parse_roster(options["feed_roster"]) == FR.parse_roster(
        _raw(_UNIT_FILE)["method"]["options"]["feed_roster"])
    assert watermark_config(unit) is not None


def test_the_digest_options_merge_through_the_runtime_with_nothing_rejected():
    from legba.runtime.dapr_actors import _merge_descriptor_options

    digest = AnalystDescriptor.model_validate(_raw(_DIGEST_FILE))
    options: dict[str, Any] = {}
    receipt = _merge_descriptor_options(options, digest, actor_id="test")
    assert receipt is not None and not receipt.get("rejected"), receipt
    assert options["source_ids"] == ["source.cisa.kev"]
    assert options["initial_lookback_hours"] == 72


def test_the_feed_roster_is_exactly_the_k1_desk_feeds_with_their_kinds():
    """Every source K-1 tagged `cyber_desk` is on the roster once, and its
    kind follows the feed: agencies are advisories, the three outlets trade
    press, the four status pages provider status, the two vendor research
    publications vendor research."""
    k1 = {}
    for path in sorted(_DESC.glob("source_*.yaml")):
        body = yaml.safe_load(path.read_text())
        if "cyber_desk" in (body.get("scope") or {}).get("tags", []):
            k1[body["identity"]["id"]] = body
    roster = dict(FR.parse_roster(_raw(_UNIT_FILE)["method"]["options"]["feed_roster"]))
    assert set(roster) == set(k1) and len(roster) == 16
    for sid, kind in roster.items():
        if sid.endswith(".status"):
            assert kind == "provider_status", sid
        elif sid in {"source.cyberscoop.news", "source.securityweek.news", "source.therecord.news"}:
            assert kind == "trade_press", sid
        elif sid in {"source.checkpoint_research.news", "source.cloudflare_ddos.news"}:
            assert kind == "vendor_research", sid
            assert k1[sid]["scope"]["source_class"] == "reporting", sid
        else:
            assert kind == "advisory", sid
            assert k1[sid]["scope"]["source_class"] == "official", sid


def test_the_unit_runs_on_the_free_core_plane_with_no_caps():
    raw = _raw(_UNIT_FILE)
    llm = raw["method"]["llm"]
    for ref in ("primary", "verify"):
        assert llm[ref]["factory_kind"] == "stack_ref"
        assert llm[ref]["raw"] == "llm.primary.openai_compat"
    assert llm["temperature"] == 1.0
    assert "max_tokens" not in llm
    assert raw["method"]["budget_tokens_per_day"] == 0
    assert _raw(_DIGEST_FILE)["method"]["budget_tokens_per_day"] == 0
    assert "anthropic" not in json.dumps(raw).lower()
    from legba.data.provenance import kinds as pk

    assert "cyber_watch" not in pk.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
    assert "kev_digest" in pk.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


def test_the_bounded_unit_drift_guard_accepts_the_unit():
    sys.path.insert(0, str(_ROOT / "scripts"))
    from bringup_register_analysts import _assert_unit_eval_coverage

    _assert_unit_eval_coverage([(_UNIT_FILE, _raw(_UNIT_FILE))])


def test_cadence_window_and_narrow_shape():
    raw = _raw(_UNIT_FILE)
    targets = raw["subscription"]["targets"]
    assert targets["predicate"] == 'has_tag("cyber")'
    assert targets["data_types"] == ["signal"]
    assert targets["time_window"] == "24h"
    assert _cron_hours(raw["cadence"]["fallback_schedule"]) == {3, 15}
    assert raw["cadence"]["cooldown_seconds"] < 12 * 3600
    # the digest fires after the KEV feed's own polls, and the unit after both
    kev_poll = _raw("source_cisa_kev.yaml")["cadence"]["schedule"]["raw"]
    assert _cron_hours(kev_poll) == {2, 14}
    assert _cron_hours(_raw(_DIGEST_FILE)["cadence"]["fallback_schedule"]) == {2, 14}
    assert raw["action_packs"] == []
    from legba.data.analysts.deterministic_handlers import scorecard_banding as sb

    assert "cyber_watch" not in sb.DIMENSIONS


def test_lifecycle_records_both_analysts_active():
    # the overlay rows say active (docs/RUNBOOK.md "The descriptor walker"), so
    # a fresh instance of the example profile comes up with both analysts on
    text = lifecycle_text(_DESC)
    for aid in ("cyber_watch", "kev_digest"):
        assert f"| analyst | {aid} | active | draft |" in text
    assert "| target | theme_cyber | active | draft |" in text


# ---------------------------------------------------------------------------
# 2. The prompt — house contract verbatim, the three kinds, attribution
# ---------------------------------------------------------------------------


def test_the_prompt_carries_the_house_contract_verbatim():
    from legba.data.analysts._tradecraft import UNIT_READ_CONTRACT_D6

    assert _norm(UNIT_READ_CONTRACT_D6) in _norm(_prompt())


def test_the_assembled_prompt_states_the_as_of_rule():
    from legba.data.analysts._tradecraft import with_preamble_if_absent
    from legba.data.analysts.unit_grounding import with_grounding_clause

    assembled = _norm(with_grounding_clause(with_preamble_if_absent(_prompt()) or ""))
    assert "AS-OF AND WINDOW" in assembled and "ZERO NEW FACTS" in assembled


def test_banned_phrases_appear_only_inside_the_prohibition():
    from legba.data.analysts._tradecraft import BANNED_PHRASE_MARKERS

    prompt = _norm(_prompt()).lower()
    for marker in BANNED_PHRASE_MARKERS:
        for m in re.finditer(re.escape(marker.lower()), prompt):
            window = prompt[max(0, m.start() - 200): m.start()]
            assert "banned phrases" in window or "do not write" in window, marker


def test_the_prompt_keeps_the_three_source_kinds_apart():
    p = _norm(_prompt())
    assert "SOURCE KINDS — KEEP THEM APART" in p
    assert "THREE SOURCE KINDS" not in p
    assert '"<agency> published <advisory / catalogue entry> about <subject>"' in p
    assert 'supports "<outlet> reported …"' in p
    assert '"<provider> declared an incident / a degradation / a recovery' in p
    assert "one kind never borrows the authority of another" in p
    assert "FEED ROSTER" in p and "never your own reading of a URL" in p


def test_the_prompt_attributes_nothing_beyond_the_source():
    p = _norm(_prompt())
    assert "ATTRIBUTION — NEVER BEYOND THE SOURCE" in p
    assert "in the source's own terms" in p
    assert 'never become "by"' in p
    assert "A claim of responsibility is the claimant's claim" in p


def test_the_prompt_declares_silent_feeds_in_the_scoped_form():
    p = _norm(_prompt())
    assert "COVERAGE — SAY WHICH FEEDS WERE SILENT" in p
    assert "this desk's collection since <date>" in p
    assert "never evidence that nothing happened" in p
    assert "SCOPE ABSENCE TO THE COLLECTION" in p


def test_indicator_examples_are_consumable_by_the_indicator_tracker():
    from legba.data.analysts.deterministic_handlers import indicator_tracker as it

    entries = [json.loads(line.strip().rstrip(","))
               for line in _prompt().splitlines() if line.strip().startswith('{"id":')]
    assert 3 <= len(entries) <= 6
    assert len(it._clean_indicators(entries)) == len(entries)
    assert all(e["status"] in it._STATUSES for e in entries)
    assert "CANONICAL IDS" in _prompt() and "do NOT re-mint" in _prompt()
    assert '"topic:cyber_watch"' in _prompt()


# ---------------------------------------------------------------------------
# 3. The FEED ROSTER block — pure, degrade, and through run_method
# ---------------------------------------------------------------------------

_ROSTER = [("source.agency.a", "advisory"), ("source.outlet.b", "trade_press"),
           ("source.status.c", "provider_status")]
_SINCE = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)


def _row(title: str, source_id: str) -> dict[str, Any]:
    return {"title": title, "source_url": "", "source_id": source_id,
            "produced_at": "2026-11-14T08:00:00+00:00",
            "data": {"published_at": "2026-11-14T07:00:00+00:00", "summary": title}}


def test_parse_roster_keeps_declared_order_and_drops_malformed():
    assert FR.parse_roster(["source.a=advisory", "bad entry", "source.a=x", "source.b=trade_press"]) == [
        ("source.a", "advisory"), ("source.b", "trade_press")]
    assert FR.parse_roster(None) == [] and FR.parse_roster("source.a=advisory") == []


def test_block_renders_counts_silence_ordinals_and_other_outlets():
    sliced = [_row("one", "source.agency.a"), _row("two", "source.news.x"),
              _row("three", "source.agency.a"), _row("four", "source.outlet.b")]
    text = FR.build_feed_roster_block(sliced, _ROSTER, {"source.agency.a": 3, "source.outlet.b": 1},
                                      since=_SINCE)
    assert "since 2026-11-14T03:00+00:00" in text
    assert "source.agency.a: 3 collected; in this slice: [1][3]" in text
    assert "source.outlet.b: 1 collected; in this slice: [4]" in text
    assert "source.status.c: 0 collected — SILENT; in this slice: none" in text
    assert "other outlets (general reporting the desk's selector matched): [2]" in text
    # grouped by kind, in declared order
    assert text.index("  advisory:") < text.index("  trade_press:") < text.index("  provider_status:")


def test_block_without_a_count_never_says_silent():
    text = FR.build_feed_roster_block([_row("one", "source.agency.a")], _ROSTER, None, since=_SINCE)
    assert "SILENT" not in text
    assert text.count("collected count unavailable this run") == 3


def test_window_start_prefers_the_watermark_stamp():
    stamp = {"slice_coverage": {"window_start": "2026-11-14T02:30:00+00:00"}, "slice_window_hours": 24}
    assert FR.window_start(stamp) == datetime(2026, 11, 14, 2, 30, tzinfo=timezone.utc)
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    assert FR.window_start({"slice_window_hours": 12}, now=now) == now - timedelta(hours=12)
    assert FR.window_start({}, now=now) == now - timedelta(hours=24)


@pytest.mark.asyncio
async def test_a_failing_count_degrades_to_unknown():
    class _Boom:
        async def fetch(self, *a: Any) -> Any:
            raise RuntimeError("substrate blip")

    assert await FR.collected_counts(_Boom(), ["source.a"], _SINCE) is None
    assert await FR.collected_counts(None, ["source.a"], _SINCE) is None


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _CapturingLLM:
    """Records the rendered prompt. No model is called."""

    subprovider = "openai"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages: list[Mapping[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"messages": messages})
        return _Response(content=json.dumps({
            "title": "Quiet window on the cyber desk",
            "body": "**BLUF:** CISA added one entry [1].",
            "confidence": 0.3, "evidence": ["1"], "tags": ["topic:cyber_watch", "severity:low"],
        }), usage=_Usage())


class _CountPG:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.args: tuple[Any, ...] = ()

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM signals" in sql and "GROUP BY source_id" in sql:
            self.args = args
            return [{"source_id": k, "n": v} for k, v in self.counts.items()]
        return []


@pytest.mark.asyncio
async def test_run_method_renders_the_roster_from_the_real_descriptor_options():
    """The descriptor's own options, merged by the runtime's own X-1 merge,
    through inline_target.run_method: the user prompt carries the roster with
    the KEV row's ordinal under `advisory` and the K-1 feeds that supplied
    nothing marked SILENT."""
    from legba.data.analysts.inline_target import InlineTargetDeps, run_method
    from legba.runtime.dapr_actors import _merge_descriptor_options

    options: dict[str, Any] = {"target_id": "theme_cyber", "analyst_id": "cyber_watch",
                               "slice_window_hours": 12}
    _merge_descriptor_options(options, _unit(), actor_id="test")
    llm = _CapturingLLM()
    pg = _CountPG({"source.cisa.kev": 2, "source.therecord.news": 1})
    rows = [_row("CVE-2026-0001 — Example Product Flaw", "source.cisa.kev"),
            _row("Hospital network hit by ransomware", "source.therecord.news"),
            _row("Cyberattack disrupts city services", "source.general.news")]
    result = await run_method(rows, options, InlineTargetDeps(llm=llm, pg=pg))
    prompt = str(llm.calls[0]["messages"][-1]["content"])
    assert "FEED ROSTER" in prompt
    assert "source.cisa.kev: 2 collected; in this slice: [1]" in prompt
    assert "source.therecord.news: 1 collected; in this slice: [2]" in prompt
    assert "source.ncsc.uk_all: 0 collected — SILENT" in prompt
    assert "general reporting the desk's selector matched): [3]" in prompt
    assert sorted(pg.args[0]) == sorted(sid for sid, _ in FR.parse_roster(options["feed_roster"]))
    step = next(s for s in result.intermediate_steps if s.get("kind") == "feed_roster")
    assert step["feeds"] == 16 and step["counted"] is True
    assert len(step["silent"]) == 14


@pytest.mark.asyncio
async def test_a_unit_without_the_option_renders_no_roster():
    from legba.data.analysts.inline_target import InlineTargetDeps, run_method

    llm = _CapturingLLM()
    result = await run_method([_row("x", "source.a")],
                              {"target_id": "country_g20_au", "analyst_id": "escalation"},
                              InlineTargetDeps(llm=llm))
    assert "FEED ROSTER" not in str(llm.calls[0]["messages"][-1]["content"])
    assert not any(s.get("kind") == "feed_roster" for s in result.intermediate_steps)


# ---------------------------------------------------------------------------
# 4. The KEV digest — pure shape and the dispatcher binding
# ---------------------------------------------------------------------------

_ITEM = {
    "cveID": "CVE-2025-61624", "vendorProject": "Fortinet", "product": "Multiple Products",
    "vulnerabilityName": "Fortinet Multiple Products Path Traversal Vulnerability",
    "dateAdded": "2026-10-16", "dueDate": "2026-10-19", "knownRansomwareCampaignUse": "Unknown",
    "shortDescription": "ignored by the digest",
}


def _kev_row(item: Mapping[str, Any], *, created: datetime | None = None) -> dict[str, Any]:
    return {"id": uuid4(), "source_id": "source.cisa.kev", "item": json.dumps(item),
            "external_id": item.get("cveID"), "created_at": created or _SINCE}


def test_an_entry_copies_the_record_fields_verbatim():
    e = KD.entry_from_row(_kev_row(_ITEM))
    assert e is not None
    for k in KD.FIELDS:
        assert e[k] == _ITEM[k]
    assert "shortDescription" not in e
    line = KD.render_line(e)
    assert line == ("- CVE-2025-61624 — Fortinet / Multiple Products: Fortinet Multiple Products "
                    "Path Traversal Vulnerability. Added 2026-10-16; due 2026-10-19; known "
                    "ransomware campaign use: Unknown.")


def test_a_missing_field_reads_not_given_and_a_row_without_a_cve_is_dropped():
    e = KD.entry_from_row(_kev_row({"cveID": "CVE-2026-1", "knownRansomwareCampaignUse": ""}))
    assert e is not None and e["knownRansomwareCampaignUse"] is None
    assert "known ransomware campaign use: not given." in KD.render_line(e)
    assert KD.entry_from_row({"id": uuid4(), "item": {}, "external_id": None}) is None


def test_digest_dedupes_by_cve_drops_the_previous_digest_and_sorts_newest_first():
    rows = [_kev_row({**_ITEM, "cveID": "CVE-1", "dateAdded": "2026-11-01"}),
            _kev_row({**_ITEM, "cveID": "CVE-2", "dateAdded": "2026-11-03"}),
            _kev_row({**_ITEM, "cveID": "CVE-2", "dateAdded": "2026-11-03"}),
            _kev_row({**_ITEM, "cveID": "CVE-3", "dateAdded": "2026-11-02"})]
    got = KD.digest_entries(rows, already_listed=frozenset({"CVE-3"}))
    assert [e["cveID"] for e in got] == ["CVE-2", "CVE-1"]


def test_prior_state_reads_the_nested_payload_dump():
    stored = {"title": "t", "data": {"sub_handler": "kev_digest",
                                     "watermark": "2026-11-14T08:23:14+00:00",
                                     "entries": [{"cveID": "CVE-1"}, {"cveID": "CVE-2"}]}}
    mark, listed = KD.prior_state(json.dumps(stored))
    assert mark == datetime(2026, 11, 14, 8, 23, 14, tzinfo=timezone.utc)
    assert listed == frozenset({"CVE-1", "CVE-2"})
    assert KD.prior_state(None) == (None, frozenset())
    assert KD.prior_state({"data": {"sub_handler": "other"}}) == (None, frozenset())


def test_the_finding_is_structural_and_cites_the_signals():
    e = KD.entry_from_row(_kev_row(_ITEM))
    f = KD.build_finding([e], since=_SINCE, watermark=_SINCE, first_run=True,
                         as_of=_SINCE, sources=["source.cisa.kev"])
    assert f.title == "KEV digest: 1 new catalogue entry since 2026-11-14T03:00Z"
    assert "known-ransomware-use flag as the feed gives it" in f.body
    assert f.data["entries_total"] == 1 and f.data["watermark"] == _SINCE.isoformat()
    assert f.data["citations"][0]["ref_id"] == e["signal_id"]
    assert "no_llm" in f.tags


def test_the_dispatcher_binds_kev_digest_as_a_finding():
    from legba.data.analysts import deterministic as D
    from legba.data.provenance.kinds import OutputKind

    assert D.OUTPUT_KIND_BY_SUB_HANDLER["kev_digest"] is OutputKind.FINDING
    assert D.SUB_HANDLERS["kev_digest"] is KD.handle


@pytest.mark.asyncio
async def test_the_digest_refuses_without_a_pool():
    from legba.data.analysts.deterministic import run_method

    with pytest.raises(RuntimeError, match="kev_digest requires a live deps.pg_pool"):
        await run_method([], {"sub_handler": "kev_digest"}, SimpleNamespace(pg_pool=None))


# ---------------------------------------------------------------------------
# 5. Against a real migrated Postgres (the isolated test stack)
# ---------------------------------------------------------------------------


@pytest.fixture
async def pool(migrated_pg):
    import asyncpg

    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    yield p
    await p.close()


async def _insert_signal(conn: Any, source_id: str, item: Mapping[str, Any] | None, *,
                         created: datetime, canonical: UUID | None = None) -> UUID:
    u = uuid4()
    payload = {"title": "t", "external_id": (item or {}).get("cveID")}
    if item is not None:
        payload["item"] = dict(item)
    await conn.execute(
        "INSERT INTO signals (id, source_id, owner_tenant, modality, payload, content_hash, "
        "fetched_at, created_at, canonical_signal_id) "
        "VALUES ($1,$2,'default','text',$3::jsonb,$4,$5,$5,$6)",
        u, source_id, json.dumps(payload), f"h_{uuid4().hex}", created, canonical)
    return u


@pytest.mark.asyncio
async def test_the_digest_watermark_lists_each_entry_once(pool):
    """First run lists the canonical entries in its look-back (never the
    alias); a quiet re-run is trace-only; a new entry after the stored
    watermark is the only line of the next digest."""
    from legba.data.analysts.deterministic import run_method

    src = f"source.k2test.kev_{uuid4().hex[:8]}"
    analyst = f"kev_digest_k2test_{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    async with pool.acquire() as c:
        a = await _insert_signal(c, src, {**_ITEM, "cveID": "CVE-2026-0001"}, created=now - timedelta(hours=5))
        await _insert_signal(c, src, {**_ITEM, "cveID": "CVE-2026-0001"}, created=now - timedelta(hours=4),
                             canonical=a)
        await _insert_signal(c, src, {**_ITEM, "cveID": "CVE-2026-0002"}, created=now - timedelta(hours=3))
        await _insert_signal(c, src, {**_ITEM, "cveID": "CVE-2025-0009"}, created=now - timedelta(days=10))
    opts = {"sub_handler": "kev_digest", "analyst_id": analyst, "source_ids": [src],
            "initial_lookback_hours": 72}
    deps = SimpleNamespace(pg_pool=pool)
    first = await run_method([], dict(opts), deps)
    assert first.force_trace_only is False
    assert [e["cveID"] for e in first.finding.data["entries"]] == ["CVE-2026-0002", "CVE-2026-0001"]
    assert first.finding.data["first_run"] is True
    assert len(first.derived_from) == 2

    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO analyst_outputs (kind, title, body, confidence, data, analyst_id, schema_uri, "
            "produced_at) VALUES ('finding', $1, $2, 1.0, $3::jsonb, $4, 'legba/finding/1.0.0', now())",
            first.finding.title, first.finding.body, json.dumps(first.finding.model_dump(mode="json")),
            analyst)
    quiet = await run_method([], dict(opts), deps)
    assert quiet.force_trace_only is True and quiet.finding.data["entries_total"] == 0
    assert quiet.finding.data["first_run"] is False

    async with pool.acquire() as c:
        await _insert_signal(c, src, {**_ITEM, "cveID": "CVE-2026-0003"}, created=datetime.now(timezone.utc))
    nxt = await run_method([], dict(opts), deps)
    assert [e["cveID"] for e in nxt.finding.data["entries"]] == ["CVE-2026-0003"]
    async with pool.acquire() as c:
        await c.execute("DELETE FROM analyst_outputs WHERE analyst_id = $1", analyst)
        await c.execute("DELETE FROM signals WHERE source_id = $1", src)


@pytest.mark.asyncio
async def test_the_roster_count_reads_the_window_canonical_rows_only(pool):
    src_a = f"source.k2test.a_{uuid4().hex[:8]}"
    src_b = f"source.k2test.b_{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    async with pool.acquire() as c:
        keep = await _insert_signal(c, src_a, None, created=now - timedelta(hours=1))
        await _insert_signal(c, src_a, None, created=now - timedelta(hours=2))
        await _insert_signal(c, src_a, None, created=now - timedelta(hours=2), canonical=keep)
        await _insert_signal(c, src_a, None, created=now - timedelta(hours=30))
        await _insert_signal(c, src_b, None, created=now - timedelta(hours=30))
    counts = await FR.collected_counts(pool, [src_a, src_b], now - timedelta(hours=12))
    assert counts == {src_a: 2}
    text = FR.build_feed_roster_block([], [(src_a, "advisory"), (src_b, "advisory")], counts,
                                      since=now - timedelta(hours=12))
    assert f"{src_b}: 0 collected — SILENT" in text
    async with pool.acquire() as c:
        await c.execute("DELETE FROM signals WHERE source_id = ANY($1::text[])", [src_a, src_b])
