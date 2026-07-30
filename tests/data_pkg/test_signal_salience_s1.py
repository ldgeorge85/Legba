# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-1 (MASTER_PLAN 2026-07-10, Phase S) — the signal_salience scorer.

Covers the pure reducers, the ECHO-BOUND parse (bind by echoed id, drop
unmatched, degrade unbound), the WRITE-path idempotency (mark every examined row,
degrade-not-break on an LLM failure), and the S-1c EVAL TRUTH-SET: the ranking
invariants that the whole salience layer exists to enforce — a head-of-state
death / kinetic strike outranks a meme or a routine procurement; a Supreme Leader
outranks an ordinary senator (the Khamenei/Graham failures). Ranking is asserted
via ``salience_sort_key`` on canned model outputs (the LLM itself is faked).
"""
from __future__ import annotations

import json

import pytest

from legba.data.analysts import signal_salience as ss


# ---------------------------------------------------------------------------
# Pure reducers
# ---------------------------------------------------------------------------


def test_authority_is_deterministic_from_source_class() -> None:
    assert ss._authority_for("official") == "official"
    assert ss._authority_for("reporting") == "reporting"
    assert ss._authority_for("state_media") == "state_media"
    assert ss._authority_for(None) == "unknown"
    assert ss._authority_for("garbage") == "unknown"
    # official outranks state_media / unknown (the anti-tabloid tie-break)
    assert ss.AUTHORITY_RANK["official"] > ss.AUTHORITY_RANK["reporting"]
    assert ss.AUTHORITY_RANK["reporting"] > ss.AUTHORITY_RANK["state_media"]
    assert ss.AUTHORITY_RANK["state_media"] > ss.AUTHORITY_RANK["unknown"]


def test_coerce_event_class_closed_and_aliased() -> None:
    assert ss._coerce_event_class("leader_death") == "leader_death"
    assert ss._coerce_event_class("Assassination") == "leader_death"
    assert ss._coerce_event_class("airstrike") == "kinetic_strike"
    assert ss._coerce_event_class("procurement") == "procurement_routine"
    assert ss._coerce_event_class("sports") == "meme_sports_culture"
    assert ss._coerce_event_class("nonsense-token") == "other"   # safe bucket


def test_coerce_actor_rank_closed_and_aliased() -> None:
    assert ss._coerce_actor_rank("head_of_state") == "head_of_state"
    assert ss._coerce_actor_rank("President") == "head_of_state"
    assert ss._coerce_actor_rank("ministry") == "state_organ"
    assert ss._coerce_actor_rank("senator") == "individual"
    assert ss._coerce_actor_rank("") == "none"


def test_salience_sort_key_orders_and_degrades_last() -> None:
    hi = ss.salience_sort_key({"magnitude": 0.9, "authority": "reporting"})
    lo = ss.salience_sort_key({"magnitude": 0.1, "authority": "reporting"})
    deg = ss.salience_sort_key({"magnitude": None, "authority": "official"})
    assert hi > lo > deg                      # a degraded/unscored row sorts LAST
    # authority breaks a magnitude tie (the Graham tabloid-frame fix)
    off = ss.salience_sort_key({"magnitude": 0.5, "authority": "official"})
    tab = ss.salience_sort_key({"magnitude": 0.5, "authority": "state_media"})
    assert off > tab


# ---------------------------------------------------------------------------
# Echo-bound parse
# ---------------------------------------------------------------------------


def _row(rid: str, source_class: str | None = "reporting") -> ss.SignalRow:
    return ss.SignalRow(id=rid, text=f"text for {rid}", source_class=source_class)


def test_parse_binds_by_echoed_id_and_drops_unmatched() -> None:
    batch = [_row("aaa"), _row("bbb")]
    # Model returns them OUT OF ORDER + an id that matches nothing → the stray is
    # dropped; each row binds to its echoed id, not its position.
    content = json.dumps([
        {"id": "bbb", "event_class": "kinetic_strike", "actor_rank": "state_organ", "magnitude": 0.9, "confidence": 0.8},
        {"id": "zzz", "event_class": "leader_death", "actor_rank": "head_of_state", "magnitude": 1.0, "confidence": 0.9},
        {"id": "aaa", "event_class": "meme_sports_culture", "actor_rank": "none", "magnitude": 0.05, "confidence": 0.9},
    ])
    out = {v.signal_id: v for v in ss._parse_salience_batch(content, batch)}
    assert out["aaa"].event_class == "meme_sports_culture"   # bound by id, not order
    assert out["bbb"].event_class == "kinetic_strike"
    assert out["aaa"].magnitude == 0.05 and out["bbb"].magnitude == 0.9


def test_parse_degrades_unbound_and_out_of_range_rows() -> None:
    batch = [_row("aaa", "official"), _row("bbb")]
    # Only 'aaa' scored; 'bbb' is absent → degraded (marks the row, magnitude None,
    # authority still stamped deterministically).
    content = json.dumps([
        {"id": "aaa", "event_class": "sanctions_economic", "actor_rank": "state_organ", "magnitude": 0.6, "confidence": 0.7},
    ])
    out = {v.signal_id: v for v in ss._parse_salience_batch(content, batch)}
    assert out["aaa"].degraded is False and out["aaa"].authority == "official"
    assert out["bbb"].degraded is True and out["bbb"].magnitude is None


def test_parse_junk_content_degrades_whole_batch_without_raising() -> None:
    batch = [_row("aaa"), _row("bbb")]
    out = ss._parse_salience_batch("the model said no", batch)
    assert all(v.degraded for v in out) and len(out) == 2


def test_parse_reordered_without_ids_degrades_never_misbinds() -> None:
    # The CRITICAL review case: the model reorders items and OMITS ids. There is
    # NO positional fallback, so nothing binds — every row degrades to unscored
    # (magnitude None) rather than a head-of-state event silently receiving a
    # meme's score. This is the tabloid-frame bug, structurally prevented.
    batch = [_row("iran_death"), _row("sub_deal"), _row("world_cup")]
    content = json.dumps([
        {"event_class": "meme_sports_culture", "actor_rank": "none", "magnitude": 0.05},
        {"event_class": "procurement_routine", "actor_rank": "state_organ", "magnitude": 0.2},
        {"event_class": "leader_death", "actor_rank": "head_of_state", "magnitude": 0.95},
    ])
    out = ss._parse_salience_batch(content, batch)
    assert all(v.degraded and v.magnitude is None for v in out)


def test_parse_unknown_id_item_is_dropped_not_positionally_bound() -> None:
    # An item with a corrupted id in position 0 must NOT claim batch[0]; the
    # correctly-echoed item binds its own signal.
    batch = [_row("aaa"), _row("bbb")]
    content = json.dumps([
        {"id": "corrupt", "event_class": "meme_sports_culture", "actor_rank": "none", "magnitude": 0.05},
        {"id": "aaa", "event_class": "kinetic_strike", "actor_rank": "state_organ", "magnitude": 0.9, "confidence": 0.8},
    ])
    out = {v.signal_id: v for v in ss._parse_salience_batch(content, batch)}
    assert out["aaa"].magnitude == 0.9 and out["aaa"].degraded is False  # bound by id
    assert out["bbb"].degraded is True                                    # unbound → degraded


# ---------------------------------------------------------------------------
# S-1c EVAL TRUTH-SET — the ranking invariants (canned model outputs)
# ---------------------------------------------------------------------------

# The worked-example signals, scored as the prompt's anchors dictate.
_WORKED = {
    "khamenei": {"event_class": "leader_death", "actor_rank": "head_of_state", "magnitude": 0.95, "authority": "reporting"},
    "strike":   {"event_class": "kinetic_strike", "actor_rank": "state_organ", "magnitude": 0.9,  "authority": "reporting"},
    "graham":   {"event_class": "other", "actor_rank": "individual", "magnitude": 0.3, "authority": "reporting"},
    "sub_deal": {"event_class": "procurement_routine", "actor_rank": "state_organ", "magnitude": 0.2, "authority": "official"},
    "meme":     {"event_class": "meme_sports_culture", "actor_rank": "none", "magnitude": 0.05, "authority": "state_media"},
}


def _rank(name: str):
    return ss.salience_sort_key(_WORKED[name])


def test_eval_leader_death_outranks_meme_and_procurement() -> None:
    assert _rank("khamenei") > _rank("meme")          # Supreme Leader > World-Cup meme
    assert _rank("khamenei") > _rank("sub_deal")


def test_eval_kinetic_strike_outranks_routine_procurement() -> None:
    assert _rank("strike") > _rank("sub_deal")        # a strike > an arms deal


def test_eval_head_of_state_outranks_ordinary_senator() -> None:
    assert _rank("khamenei") > _rank("graham")        # the Graham failure, fixed


def test_eval_consequence_classes_outrank_floor_classes() -> None:
    top = min(_rank("khamenei"), _rank("strike"))
    floor = max(_rank("meme"), _rank("sub_deal"))
    assert top > floor                                # every top-class > every floor-class


# ---------------------------------------------------------------------------
# score_signals — write-path idempotency + degrade-not-break
# ---------------------------------------------------------------------------


class _CannedLLM:
    def __init__(self, content: str, *, raise_exc: bool = False) -> None:
        self._content = content
        self._raise = raise_exc
        self.calls = 0

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None, system=None, **kw):
        self.calls += 1
        if self._raise:
            raise RuntimeError("llm down")
        from types import SimpleNamespace
        return SimpleNamespace(content=self._content)


class _FakeConn:
    def __init__(self, rows) -> None:
        self._rows = rows
        self.writes: list[tuple[str, dict]] = []

    async def fetch(self, sql, *params):
        return list(self._rows)

    async def execute(self, sql, *params):
        # _WRITE_SALIENCE_SQL: (id, jsonb-string). Mimic asyncpg's status string.
        self.writes.append((params[0], json.loads(params[1])))
        return "UPDATE 1"


def _srow(rid: str, payload: dict, source_class: str = "reporting") -> dict:
    return {"id": rid, "payload": payload, "source_class": source_class}


@pytest.mark.asyncio
async def test_score_signals_apply_writes_and_marks_every_row() -> None:
    rows = [
        _srow("aaa", {"title": "Khamenei buried"}, "reporting"),
        _srow("bbb", {"title": "World Cup meme goes viral"}, "state_media"),
    ]
    content = json.dumps([
        {"id": "aaa", "event_class": "leader_death", "actor_rank": "head_of_state", "magnitude": 0.95, "confidence": 0.9},
        {"id": "bbb", "event_class": "meme_sports_culture", "actor_rank": "none", "magnitude": 0.05, "confidence": 0.9},
    ])
    conn = _FakeConn(rows)
    examined, scored, sample = await ss.score_signals(
        conn, _CannedLLM(content), apply=True, max_rows=10, batch_size=12,
        model_id="gpt-oss-test", now_iso="2026-07-13T22:00:00+00:00")
    assert examined == 2 and scored == 2
    # BOTH rows written (idempotent mark); authority stamped deterministically.
    written = {rid: sal for rid, sal in conn.writes}
    assert set(written) == {"aaa", "bbb"}
    assert written["aaa"]["magnitude"] == 0.95 and written["aaa"]["authority"] == "reporting"
    assert written["bbb"]["authority"] == "state_media"
    assert all("scored_at" in s and s["model_id"] == "gpt-oss-test" for s in written.values())


@pytest.mark.asyncio
async def test_score_signals_llm_failure_degrades_but_still_marks() -> None:
    rows = [_srow("aaa", {"title": "x"}), _srow("bbb", {"title": "y"})]
    conn = _FakeConn(rows)
    examined, scored, sample = await ss.score_signals(
        conn, _CannedLLM("", raise_exc=True), apply=True, max_rows=10)
    assert examined == 2 and scored == 0            # none scored, but...
    # ...every examined row STILL written as a degraded marker (pool drains).
    assert {rid for rid, _ in conn.writes} == {"aaa", "bbb"}
    assert all(sal.get("degraded") is True for _, sal in conn.writes)


@pytest.mark.asyncio
async def test_score_signals_dry_run_writes_nothing() -> None:
    rows = [_srow("aaa", {"title": "x"})]
    content = json.dumps([{"id": "aaa", "event_class": "other", "actor_rank": "none", "magnitude": 0.2, "confidence": 0.5}])
    conn = _FakeConn(rows)
    examined, scored, _ = await ss.score_signals(conn, _CannedLLM(content), apply=False)
    assert examined == 1 and scored == 1 and conn.writes == []   # dry-run mutates nothing


# ---------------------------------------------------------------------------
# Per-channel source_class override (2026-07-29 Ansar Allah decision) — the
# stamping-path fix. source_class is otherwise a whole-descriptor field
# (SourceScope.source_class, read via the s.source_id -> source_descriptors
# join); config.channels.classes lets ONE channel on a shared telegram
# descriptor carry a different class, resolved per-signal by
# ``_channel_class_override`` keyed off ``payload.channel.username`` and
# preferred over the descriptor's ``source_class`` column in
# ``select_salience_candidates`` — reaching the S-1 authority stamp
# (``signals.salience.authority``) the SAME way the descriptor-level default
# always has, not a cosmetic payload tag nothing reads.
# ---------------------------------------------------------------------------

_WRAPPED_CLASSES = {
    "factory_kind": "dict", "key_kind": "text", "value_kind": "text",
    "raw": {"Almasirah_En": "state_media", "ansarollah1": "state_media"},
}
_BARE_CLASSES = {"Almasirah_En": "state_media", "ansarollah1": "state_media"}


def test_channel_class_override_wrapped_factory_shape() -> None:
    assert ss._channel_class_override(_WRAPPED_CLASSES, "Almasirah_En") == "state_media"
    assert ss._channel_class_override(_WRAPPED_CLASSES, "@Almasirah_En") == "state_media"
    assert ss._channel_class_override(_WRAPPED_CLASSES, "bloomberg") is None


def test_channel_class_override_bare_dict_shape() -> None:
    assert ss._channel_class_override(_BARE_CLASSES, "ansarollah1") == "state_media"
    assert ss._channel_class_override(_BARE_CLASSES, "bloomberg") is None


def test_channel_class_override_json_string_shape() -> None:
    """asyncpg with no jsonb codec hands the column back as a raw string."""
    assert ss._channel_class_override(json.dumps(_WRAPPED_CLASSES), "ansarollah1") == "state_media"


def test_channel_class_override_at_prefixed_map_key_matches_stripped_handle() -> None:
    """The descriptor author may write the override key with '@' — the
    lookup normalizes both sides (mirrors telegram.py's
    ``_strip_channel_prefix``)."""
    raw = {"@Almasirah_En": "state_media"}
    assert ss._channel_class_override(raw, "Almasirah_En") == "state_media"


@pytest.mark.parametrize("absent", [None, {}, "not json", 123, [], {"Almasirah_En": "state_media"}])
def test_channel_class_override_none_when_no_channel_handle(absent) -> None:
    assert ss._channel_class_override(absent, None) is None
    assert ss._channel_class_override(absent, "") is None


def test_channel_class_override_none_when_classes_map_absent() -> None:
    """Non-telegram sources (or a telegram descriptor with no
    config.classes) -> no override; caller falls back to the descriptor
    default untouched."""
    assert ss._channel_class_override(None, "Almasirah_En") is None


def test_payload_channel_handle_extracts_and_strips_at_prefix() -> None:
    assert ss._payload_channel_handle({"channel": {"username": "@Almasirah_En"}}) == "Almasirah_En"
    assert ss._payload_channel_handle({"channel": {"username": "ansarollah1"}}) == "ansarollah1"


def test_payload_channel_handle_json_string_payload() -> None:
    payload = json.dumps({"channel": {"username": "ansarollah1"}})
    assert ss._payload_channel_handle(payload) == "ansarollah1"


@pytest.mark.parametrize("payload", [{}, {"channel": {}}, {"channel": {"username": None}}, "not json", None])
def test_payload_channel_handle_none_for_non_telegram_or_malformed_payloads(payload) -> None:
    assert ss._payload_channel_handle(payload) is None


@pytest.mark.asyncio
async def test_select_salience_candidates_applies_override_and_leaves_others_default() -> None:
    """The stamping-path test (not just config parsing): three signals off
    ONE shared descriptor whose scope.source_class is `reporting` — two from
    channels overridden `state_media` (config.channels.classes), one from a
    non-overridden channel. select_salience_candidates must reflect the
    override on the first two and the descriptor default on the third."""
    rows = [
        {
            "id": "am1", "payload": {"channel": {"username": "Almasirah_En"}},
            "source_class": "reporting", "channel_classes": _WRAPPED_CLASSES,
        },
        {
            "id": "an1", "payload": {"channel": {"username": "ansarollah1"}},
            "source_class": "reporting", "channel_classes": _WRAPPED_CLASSES,
        },
        {
            "id": "bb1", "payload": {"channel": {"username": "bloomberg"}},
            "source_class": "reporting", "channel_classes": _WRAPPED_CLASSES,
        },
    ]
    conn = _FakeConn(rows)
    candidates = await ss.select_salience_candidates(conn, window_hours=96, limit=10)
    by_id = {c.id: c for c in candidates}
    assert by_id["am1"].source_class == "state_media"
    assert by_id["an1"].source_class == "state_media"
    assert by_id["bb1"].source_class == "reporting"   # non-overridden — descriptor default, unchanged


@pytest.mark.asyncio
async def test_select_salience_candidates_backward_compatible_without_channel_classes_key() -> None:
    """A row shape that predates this column (e.g. any caller/test fixture
    that never sets ``channel_classes``) must still work — byte-identical to
    the pre-override behavior — instead of raising."""
    rows = [_srow("aaa", {"title": "no channel here"}, "reporting")]
    assert "channel_classes" not in rows[0]
    conn = _FakeConn(rows)
    candidates = await ss.select_salience_candidates(conn, window_hours=96, limit=10)
    assert candidates[0].source_class == "reporting"


@pytest.mark.asyncio
async def test_score_signals_stamps_authority_from_per_channel_override() -> None:
    """FULL stamping-path: the override reaches the WRITTEN signal's
    salience.authority (signals.salience, migration 0089) — not merely a
    parsed config value. Two Ansar Allah channels riding a `reporting`-
    classed descriptor come out `state_media`; a third channel on the SAME
    descriptor with no override stays `reporting`."""
    rows = [
        {
            "id": "am1", "payload": {"title": "Ansar Allah statement (Almasirah)", "channel": {"username": "Almasirah_En"}},
            "source_class": "reporting", "channel_classes": _WRAPPED_CLASSES,
        },
        {
            "id": "an1", "payload": {"title": "Ansar Allah statement (ansarollah1)", "channel": {"username": "ansarollah1"}},
            "source_class": "reporting", "channel_classes": _WRAPPED_CLASSES,
        },
        {
            "id": "bb1", "payload": {"title": "Bloomberg wire item", "channel": {"username": "bloomberg"}},
            "source_class": "reporting", "channel_classes": _WRAPPED_CLASSES,
        },
    ]
    content = json.dumps([
        {"id": "am1", "event_class": "diplomatic_rupture", "actor_rank": "state_organ", "magnitude": 0.6, "confidence": 0.8},
        {"id": "an1", "event_class": "diplomatic_rupture", "actor_rank": "state_organ", "magnitude": 0.6, "confidence": 0.8},
        {"id": "bb1", "event_class": "sanctions_economic", "actor_rank": "state_organ", "magnitude": 0.4, "confidence": 0.8},
    ])
    conn = _FakeConn(rows)
    examined, scored, _ = await ss.score_signals(
        conn, _CannedLLM(content), apply=True, max_rows=10, batch_size=12,
        model_id="gpt-oss-test", now_iso="2026-07-29T00:00:00+00:00")
    assert examined == 3 and scored == 3
    written = {rid: sal for rid, sal in conn.writes}
    assert written["am1"]["authority"] == "state_media"
    assert written["an1"]["authority"] == "state_media"
    assert written["bb1"]["authority"] == "reporting"   # unaffected — descriptor default
