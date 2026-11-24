# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane E-1 — the due-and-absent detector (``due_and_absent``).

  * the descriptor parses on the runtime's model, merges its options with
    nothing rejected, ships draft (LIFECYCLE ``configured``), budget 0;
  * the dispatcher, the structural-exempt set and its UI mirror;
  * the alert kind is closed-listed with a topic and is never paged under
    rule 2026-11-14/1 (nor under the rule-unavailable fallback, at ``low``);
  * the wording rule: banned phrasings raise, quoted record text is exempt,
    every rendered line carries the collection-absence sentence;
  * each expectation type's classification and the ledger diff (pure);
  * against a real migrated Postgres, through ``deterministic.run_method``
    (the real binding path): first run reports, the stored ledger dedupes the
    second, state changes re-report as resolved late / voided, every raise is
    LOGGED in the alert log and nothing reaches the delivery plane. The DB
    test runs inside one transaction that is rolled back, so the shared test
    database keeps none of its rows (the forecast ledger refuses DELETE).
"""

from __future__ import annotations

from tests._lifecycle_rows import lifecycle_text
import json
import pathlib
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml

from legba.data.analysts.deterministic_handlers import due_and_absent as DA
from legba.data.analysts.deterministic_handlers import due_and_absent_reads as RD
from legba.data.schemas.analyst import AnalystDescriptor

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DESC = _ROOT / "descriptors"
_FILE = "analyst_due_and_absent.yaml"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _raw() -> dict[str, Any]:
    return yaml.safe_load((_DESC / _FILE).read_text())


def test_the_imports_resolve_to_this_tree() -> None:
    assert pathlib.Path(DA.__file__).resolve().is_relative_to(_ROOT / "src")


# ---------------------------------------------------------------------------
# 1. Descriptor, wiring, lifecycle
# ---------------------------------------------------------------------------


def test_the_descriptor_parses_on_the_runtime_model_and_ships_draft() -> None:
    d = AnalystDescriptor.model_validate(_raw())
    assert (d.identity.id, d.identity.kind, d.identity.state) == ("due_and_absent", "deterministic", "draft")
    raw = _raw()
    assert raw["method"]["sub_handler"] == "due_and_absent"
    assert raw["method"]["budget_tokens_per_day"] == 0
    assert raw["cadence"]["fallback_schedule"] == "48 7 * * *"
    assert raw["cadence"]["cooldown_seconds"] < 24 * 3600


def test_the_options_merge_through_the_runtime_with_nothing_rejected() -> None:
    from legba.runtime.dapr_actors import _merge_descriptor_options

    options: dict[str, Any] = {}
    receipt = _merge_descriptor_options(options, AnalystDescriptor.model_validate(_raw()), actor_id="test")
    assert receipt is not None and not receipt.get("rejected"), receipt
    assert options["acute_grace_hours"] == 24
    assert options["series_tolerance"][0] == "day=3"
    assert DA.resolve_options(options)["types"] == list(DA.TYPES)


def test_a_bad_option_is_rejected_by_the_catalog() -> None:
    from legba.data.option_catalog.handler_options_programs import DUE_AND_ABSENT_OPTIONS

    spec = {s.name: s for s in DUE_AND_ABSENT_OPTIONS}
    assert spec["series_tolerance"].validate(["day=x"])[0] is False
    assert spec["expectation_types"].validate(["events"])[0] is False
    assert spec["expectation_types"].validate(["series_period"])[0] is True


def test_lifecycle_records_it_active() -> None:
    # Activated at roll 83 through the transition route (2026-11-14).
    assert "| analyst | due_and_absent | active | draft |" in lifecycle_text(_DESC)


def test_the_dispatcher_binds_it_as_a_finding() -> None:
    from legba.data.analysts import deterministic as D
    from legba.data.provenance.kinds import OutputKind

    assert D.OUTPUT_KIND_BY_SUB_HANDLER["due_and_absent"] is OutputKind.FINDING
    assert D.SUB_HANDLERS["due_and_absent"] is DA.handle


def test_structural_exempt_and_the_ui_mirror() -> None:
    from legba.data.provenance import kinds as pk

    assert "due_and_absent" in pk.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
    ui = (_ROOT / "legba-ui-v3" / "src" / "lib" / "verdictModel.ts").read_text()
    block = ui.split("STRUCTURAL_VERIFY_EXEMPT_ANALYSTS", 1)[1].split("])", 1)[0]
    assert "'due_and_absent'" in block


def test_docs_carry_the_method_version_and_the_alert_kind() -> None:
    analysis = (_ROOT / "docs" / "ANALYSIS.md").read_text()
    assert f"| `due_and_absent` | `{DA.METHOD_VERSION}` |" in analysis
    alerting = (_ROOT / "docs" / "ALERTING.md").read_text()
    assert "| expectations | `due_and_absent` |" in alerting


@pytest.mark.asyncio
async def test_it_refuses_without_a_pool() -> None:
    from legba.data.analysts.deterministic import run_method

    with pytest.raises(RuntimeError, match="due_and_absent requires a live deps.pg_pool"):
        await run_method([], {"sub_handler": "due_and_absent"}, SimpleNamespace(pg_pool=None))


# ---------------------------------------------------------------------------
# 2. The alert kind: closed-listed, a topic, never paged
# ---------------------------------------------------------------------------


def test_the_alert_kind_is_closed_listed_with_a_topic() -> None:
    from legba.data.alerts.kinds import KINDS
    from legba.data.alerts.topics import TOPIC_BY_KIND

    kind = KINDS[DA.ALERT_KIND]
    assert (kind.lifecycle, kind.quiet_close_hours, kind.group) == ("condition", None, "expectations")
    assert TOPIC_BY_KIND[DA.ALERT_KIND] == "engine"


@pytest.mark.parametrize("severity", ["info", "low", "medium", "high", "critical"])
def test_rule_2026_11_14_1_never_pages_it(severity: str) -> None:
    from legba.data.alerts.page_rule import load_rule
    from legba.data.alerts.raise_alert import decide

    rule = load_rule()
    assert rule.version == "2026-11-14/1"
    assert all(e.kind != DA.ALERT_KIND for e in rule.entries)
    for action in ("opened", "updated", "closed"):
        decision, reason, entry = decide(
            rule=rule, kind=DA.ALERT_KIND, severity=severity, subject_id="x", action=action,
            hold=None, prior_paged_severity=None, prior_last_paged_at=None, now=NOW)
        assert (decision, entry) == ("logged", None), (severity, action, reason)


def test_even_the_rule_unavailable_fallback_does_not_page_its_low_raise() -> None:
    from legba.data.alerts.raise_alert import _PAGE_EVERYTHING, decide

    decision, _, _ = decide(rule=_PAGE_EVERYTHING, kind=DA.ALERT_KIND, severity="low", subject_id="x",
                            action="opened", hold=None, prior_paged_severity=None,
                            prior_last_paged_at=None, now=NOW)
    assert decision == "logged"


# ---------------------------------------------------------------------------
# 3. The wording rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", DA.BANNED_PHRASES)
def test_every_banned_phrase_is_refused(phrase: str) -> None:
    with pytest.raises(DA.WordingViolation):
        DA.check_wording(f"The summit {phrase.upper()} on the day.")


def test_quoted_record_text_is_not_the_detectors_claim() -> None:
    text = f"expected a close of {DA.quote('the vote did not happen by Friday')}."
    assert DA.check_wording(text) == text
    with pytest.raises(DA.WordingViolation):
        DA.check_wording(text + " It did not happen.")


def _fq(**kw: Any) -> dict[str, Any]:
    row = {"id": str(uuid4()), "slug": "q-one", "question": "?", "resolver_kind": "event_reported",
           "status": "open", "due_at": NOW - timedelta(days=5), "grace_hours": 72,
           "window_start": NOW - timedelta(days=30), "window_end": NOW - timedelta(days=5),
           "resolution_outcome": None, "resolved_at": None, "proposal_id": None,
           "proposal_outcome": None, "proposal_at": None, "proposal_searched": None,
           "decisive_proposal_at": None, "legacy_proposal_at": None}
    row.update(kw)
    return row


def _every_type_absent() -> list[dict[str, Any]]:
    return [
        DA.classify_forecast_question(_fq()),
        DA.classify_acute_forecast({"id": str(uuid4()), "region": "country_g20_xx", "event_class": "hazard_severe",
                                    "window_start": NOW - timedelta(days=10), "window_end": NOW - timedelta(days=3),
                                    "resolved_outcome": None, "resolved_by": None, "resolved_at": None},
                                   grace_hours=24),
        DA.classify_scheduled_event({"id": str(uuid4()), "title": "A summit that did not happen?",
                                     "starts_at": NOW - timedelta(days=4), "ends_at": None, "scope": "global",
                                     "subject_keys": ["alpha", "beta"], "confidence": "confirmed",
                                     "retired_at": None}, None, tolerance_hours=48, min_key_hits=2),
        DA.classify_inquiry({"id": str(uuid4()), "descriptor_id": "inquiry_x", "kind": "hypothesis",
                             "text": "h", "status": "open", "resolves_by": NOW - timedelta(days=2),
                             "closed_at": None, "dispatched_to": None}),
        DA.classify_series({"source_id": "source.s", "series_id": "s.v", "period": "day",
                            "newest": date(2026, 11, 1), "newest_id": str(uuid4()), "subjects": 3},
                           {"state": "active", "lag_days": "2"}, now=NOW, tolerance={"day": 3}),
    ]


def test_every_type_renders_the_collection_absence_sentence_and_passes_the_rule() -> None:
    exps = _every_type_absent()
    assert [e["state"] for e in exps] == ["absent"] * 5
    changes, _ = DA.diff(exps, {}, now=NOW, measured_types=DA.TYPES)
    assert len(changes) == 5
    for c in changes:
        line = DA.render_change(c, now=NOW, health_text="Sources that would carry it: x: silent.")
        assert DA.ABSENCE_SENTENCE in line
        assert "overdue by" in line and "Looked for:" in line
    f = DA.build_finding(changes, [DA.render_change(c, now=NOW) for c in changes], ledger={}, counts={},
                         read_errors={}, truncated=[], alerts={}, as_of=NOW)
    assert "not proof of absence in the world" in f.body


# ---------------------------------------------------------------------------
# 4. Classification, one type at a time
# ---------------------------------------------------------------------------


def test_forecast_question_states() -> None:
    assert DA.classify_forecast_question(_fq())["state"] == "absent"
    late = DA.classify_forecast_question(_fq(decisive_proposal_at=NOW - timedelta(hours=1)))
    assert late["state"] == "met" and late["met_at"] == NOW - timedelta(hours=1)
    assert DA.classify_forecast_question(_fq(status="void"))["state"] == "voided"
    assert DA.classify_forecast_question(_fq(resolution_outcome="yes", resolved_at=NOW))["state"] == "met"
    e = DA.classify_forecast_question(_fq(due_at=NOW - timedelta(days=1), grace_hours=48))
    assert e["by"] == NOW + timedelta(days=1)
    searched = {"parts": {"main": {"corpus": "signals", "fetched_from": "a", "fetched_to": "b",
                                   "prefilter_all": ["x"], "rows_matched": 0, "signals_in_scope": 9}}}
    e = DA.classify_forecast_question(_fq(proposal_outcome="undetermined", proposal_at=NOW,
                                          proposal_searched=json.dumps(searched)))
    assert e["state"] == "absent" and "0 matched of 9 in scope" in e["looked_for"]
    assert e["note"] is None
    assert DA.classify_forecast_question(_fq())["note"].startswith("the resolver job has recorded no reading")


def test_acute_states_and_sources() -> None:
    base = {"id": str(uuid4()), "region": "r", "event_class": "c", "window_start": NOW - timedelta(days=9),
            "window_end": NOW - timedelta(days=2), "resolved_outcome": None, "resolved_by": None,
            "resolved_at": None}
    e = DA.classify_acute_forecast(base, grace_hours=24)
    assert e["state"] == "absent" and e["sources"] == list(DA.HAZARD_SEVERE_SOURCES)
    assert DA.classify_acute_forecast({**base, "resolved_by": "voided:x"}, grace_hours=24)["state"] == "voided"
    assert DA.classify_acute_forecast({**base, "resolved_outcome": 0, "resolved_at": NOW},
                                      grace_hours=24)["state"] == "met"
    assert "unresolved:expired" in DA.classify_acute_forecast(
        {**base, "resolved_by": "unresolved:expired"}, grace_hours=24)["note"]


def test_scheduled_event_states() -> None:
    row = {"id": str(uuid4()), "title": "t", "starts_at": NOW - timedelta(days=5),
           "ends_at": NOW - timedelta(days=4), "scope": "kv", "subject_keys": ["a", "b", "c"],
           "confidence": "reported", "retired_at": None}
    e = DA.classify_scheduled_event(row, None, tolerance_hours=48, min_key_hits=2)
    assert e["state"] == "absent" and e["by"] == NOW - timedelta(days=2)
    assert e["subject_kind"] == "target" and e["subject_id"] == "kv"
    assert DA.classify_scheduled_event({**row, "subject_keys": []}, None, tolerance_hours=48,
                                       min_key_hits=2)["state"] == "not_checkable"
    assert DA.classify_scheduled_event({**row, "retired_at": NOW}, None, tolerance_hours=48,
                                       min_key_hits=2)["state"] == "voided"
    met = DA.classify_scheduled_event(row, {"id": "s", "fetched_at": NOW}, tolerance_hours=48, min_key_hits=2)
    assert met["state"] == "met" and met["met_at"] == NOW


def test_inquiry_states() -> None:
    row = {"id": str(uuid4()), "descriptor_id": "inq", "kind": "hypothesis", "text": "t",
           "status": "open", "resolves_by": NOW - timedelta(days=1), "closed_at": None, "dispatched_to": None}
    assert DA.classify_inquiry(row)["state"] == "absent"
    assert DA.classify_inquiry({**row, "status": "refuted", "closed_at": NOW})["state"] == "met"
    assert DA.classify_inquiry({**row, "status": "expired", "closed_at": NOW})["state"] == "voided"
    assert DA.classify_inquiry(row)["carrier"] == "inq"


def test_series_uses_the_declared_period_lag_and_tolerance() -> None:
    row = {"source_id": "source.s", "series_id": "s.v", "period": "day", "newest": date(2026, 11, 8),
           "newest_id": str(uuid4()), "subjects": 1}
    tol = DA.parse_tolerance(None)
    # 11-08 + 1 day + lag 3 + 3 tolerance = 11-15 > NOW: not yet due.
    assert DA.classify_series(row, {"state": "active", "lag_days": "3"}, now=NOW, tolerance=tol)["state"] == "met"
    # no declared lag: 0 assumed, and the line says so: 11-12 < NOW.
    e = DA.classify_series(row, {"state": "active", "lag_days": None}, now=NOW, tolerance=tol)
    assert e["state"] == "absent" and "no declared lag (0 assumed)" in e["what"]
    assert DA.classify_series(row, {"state": "configured", "lag_days": "3"}, now=NOW,
                              tolerance=tol)["state"] == "voided"
    assert DA.parse_tolerance(["day=5", "bogus=1"])["day"] == 5
    assert e["key"] == "series_period:source.s:s.v"


def test_keys_fold_escape_and_match_whole_words_only() -> None:
    assert RD.fold_keys(["G20", " ", "g20", "Two  Words"]) == ["g20", "two words"]
    assert RD.like_patterns(["50%_off", "a\\b"]) == ["%50\\%\\_off%", "%a\\\\b%"]
    assert RD.key_hits("The G20 summit (a.b) met", ["g20", "A.B", "summ"]) == 2
    assert RD.key_hits("G200 summitry", ["g20", "summit"]) == 0


def test_match_candidates_takes_the_earliest_at_or_after_the_start() -> None:
    t0 = NOW - timedelta(days=3)
    cands = [
        {"id": "a", "source_id": "s1", "fetched_at": t0 - timedelta(hours=1), "txt": "alpha beta"},
        {"id": "c", "source_id": "s2", "fetched_at": (t0 + timedelta(hours=5)).isoformat(), "txt": "alpha, beta!"},
        {"id": "b", "source_id": "s1", "fetched_at": t0 + timedelta(hours=2), "txt": "alpha only"},
    ]
    got = RD.match_candidates([("e1", t0, ["alpha", "beta"]), ("e2", t0, ["gamma"])], cands, min_hits=2)
    assert set(got) == {"e1"} and got["e1"]["id"] == "c"
    one = RD.match_candidates([("e1", t0, ["alpha", "beta"])], cands, min_hits=1)
    assert one["e1"]["id"] == "b"


# ---------------------------------------------------------------------------
# 5. The ledger diff and the watchdog boundary
# ---------------------------------------------------------------------------


def test_diff_reports_only_state_changes() -> None:
    a, b, c = (DA.classify_inquiry({"id": str(uuid4()), "descriptor_id": "inq", "kind": "hypothesis",
                                    "text": "t", "status": "open", "resolves_by": NOW - timedelta(days=1),
                                    "closed_at": None, "dispatched_to": None}) for _ in range(3))
    changes, ledger = DA.diff([a, b], {}, now=NOW, measured_types=DA.TYPES)
    assert [x["transition"] for x in changes] == ["newly_overdue"] * 2 and set(ledger) == {a["key"], b["key"]}
    # same state again: nothing reported, ledger carried
    changes2, ledger2 = DA.diff([a, b], ledger, now=NOW + timedelta(days=1), measured_types=DA.TYPES)
    assert changes2 == [] and ledger2 == ledger
    # a resolved, b voided, a third row newly overdue
    a2, b2 = {**a, "state": "met", "met_at": NOW}, {**b, "state": "voided", "void_reason": "closed"}
    changes3, ledger3 = DA.diff([a2, b2, c], ledger, now=NOW, measured_types=DA.TYPES)
    assert {(x["key"], x["transition"]) for x in changes3} == {
        (a["key"], "resolved_late"), (b["key"], "voided"), (c["key"], "newly_overdue")}
    assert set(ledger3) == {c["key"]}


def test_diff_voids_a_vanished_row_but_carries_an_unmeasured_type() -> None:
    prior = {"inquiry_expectation:x": {"type": "inquiry_expectation", "ref_id": "x", "since": "s"},
             "series_period:s:v": {"type": "series_period", "ref_id": "y", "since": "s"}}
    changes, ledger = DA.diff([], prior, now=NOW, measured_types=["inquiry_expectation"])
    assert [(c["key"], c["transition"]) for c in changes] == [("inquiry_expectation:x", "voided")]
    assert set(ledger) == {"series_period:s:v"}
    line = DA.render_change(changes[0], now=NOW)
    assert "no longer in the record" in line


def test_prior_ledger_reads_the_nested_payload_dump() -> None:
    stored = {"title": "t", "data": {"sub_handler": "due_and_absent", "ledger": {"k": {"type": "t"}}}}
    assert DA.prior_ledger(json.dumps(stored)) == {"k": {"type": "t"}}
    assert DA.prior_ledger({"data": {"sub_handler": "kev_digest", "ledger": {"k": {}}}}) == {}


def test_a_series_whose_polls_fail_belongs_to_the_watchdog() -> None:
    e = _every_type_absent()[4]
    change = {**e, "transition": "newly_overdue"}
    assert DA.watchdog_owns(change, {"source.s": {"class": "failing"}}) is True
    assert DA.watchdog_owns(change, {"source.s": {"class": "polling_clean"}}) is False
    line = DA.render_change(change, now=NOW, owned_by_watchdog=True)
    assert "liveness watchdog" in line and "no due_and_absent alert is raised" in line


def test_describe_health_names_classes_and_watchdog_episodes() -> None:
    h = {"a": {"class": "polling_clean", "polls": 3, "written": 0, "watchdog": []},
         "b": {"class": "silent", "polls": 0, "watchdog": ["source_cadence_stall"]}}
    text = DA.describe_health(["a", "b"], h)
    assert "a: polling clean (3 polls, 0 new rows); no watchdog episode" in text
    assert "b: silent (no poll recorded in the window); watchdog episodes: source_cadence_stall" in text
    whole = DA.describe_health(None, h)
    assert "the row names no source" in whole.lower() and "1 silent" in whole
    assert "has written nothing since the due date" in DA.describe_health(None, {}, carrier="i", carrier_outputs=0)
    assert RD.poll_class({"polls": 2, "errors": 2, "clean": 0}) == "failing"
    assert RD.poll_class({"polls": 2, "errors": 1, "clean": 1}) == "partly_failing"
    assert RD.poll_class(None) == "silent"


# ---------------------------------------------------------------------------
# 6. Against a real migrated Postgres, through deterministic.run_method
# ---------------------------------------------------------------------------


class _Acq:
    def __init__(self, conn: Any) -> None:
        self._c = conn

    async def __aenter__(self) -> Any:
        return self._c

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _TxPool:
    """A pool over ONE connection inside an open transaction: the handler and
    raise_alert both use it, and the test rolls everything back."""

    def __init__(self, conn: Any) -> None:
        self._c = conn

    def acquire(self) -> _Acq:
        return _Acq(self._c)

    async def fetchrow(self, *a: Any) -> Any:
        return await self._c.fetchrow(*a)

    async def fetch(self, *a: Any) -> Any:
        return await self._c.fetch(*a)

    async def execute(self, *a: Any) -> Any:
        return await self._c.execute(*a)


class _NoDelivery:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def fan_out(self, payload: Any) -> list[Any]:
        self.calls.append(payload)
        return []


@pytest.fixture
async def conn(migrated_pg):
    import asyncpg

    c = await asyncpg.connect(migrated_pg.dsn)
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def _seed(c: Any, tag: str) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    sha = "0" * 64
    ids: dict[str, str] = {}
    ids["fq"] = str(await c.fetchval(
        "INSERT INTO forecast_questions (slug, question, resolver_kind, resolver_spec, due_at, grace_hours,"
        " exogenous, created_by, sealed_at, content_sha256) VALUES ($1,'q?','event_reported','{}'::jsonb,"
        " $2, 24, true, 'test', $3, $4) RETURNING id",
        f"e1-{tag}", now - timedelta(days=3), now - timedelta(days=20), sha))
    ids["acute"] = str(await c.fetchval(
        "INSERT INTO acute_forecasts (region, event_class, window_start, window_end, p, p_base, method,"
        " resolution_test) VALUES ($1,'hazard_severe',$2,$3,0.2,0.1,'recent_rate_poisson','t') RETURNING id",
        f"country_e1_{tag}", now - timedelta(days=10), now - timedelta(days=3)))
    ids["sched"] = str(await c.fetchval(
        "INSERT INTO scheduled_events (title, starts_at, scope, subject_keys) VALUES ($1,$2,'global',$3)"
        " RETURNING id", f"E1 event {tag}", now - timedelta(days=5), [f"zq{tag}", f"zr{tag}"]))
    ids["sched_met"] = str(await c.fetchval(
        "INSERT INTO scheduled_events (title, starts_at, scope, subject_keys) VALUES ($1,$2,'global',$3)"
        " RETURNING id", f"E1 met {tag}", now - timedelta(days=5), [f"zs{tag}", f"zt{tag}"]))
    await c.execute(
        "INSERT INTO signals (id, source_id, owner_tenant, modality, payload, content_hash, fetched_at, created_at)"
        " VALUES ($1,'source.e1test','default','text',$2::jsonb,$3,$4,$4)",
        uuid4(), json.dumps({"title": f"Zs{tag} and ZT{tag} met today"}), f"h_{uuid4().hex}",
        now - timedelta(days=4, hours=12))
    ids["inq"] = str(await c.fetchval(
        "INSERT INTO inquiry_ledger (descriptor_id, kind, text, resolution_test, resolves_by)"
        " VALUES ($1,'hypothesis','x holds','t',$2) RETURNING id",
        f"inquiry_e1_{tag}", now - timedelta(days=1)))
    src = f"source.e1test.series_{tag}"
    await c.execute(
        "INSERT INTO source_descriptors (descriptor_id, version, schema_uri, is_head, kind, state, owner, name,"
        " body) VALUES ($1,'v1','legba/source/1.0.0',true,'poll','active','test','t',$2::jsonb)",
        src, json.dumps({"config": {"lag_days": {"factory_kind": "number", "raw": 1}}}))
    for days_ago in (12, 11):
        d = (now - timedelta(days=days_ago)).date()
        await c.execute(
            "INSERT INTO observations (collection_id, series_id, subject_kind, subject, valid_from, valid_to,"
            " record_time, unit, source_url, sha256, origin_class, source_id, methodology_version,"
            " fetch_sha256, period, record_time_basis, value) VALUES ($1,$2,'global','global',$3,$3,$4,"
            "'percent','https://example.invalid',$5,'live',$6,'m/1',$5,'day','provider',1.0)",
            f"live:{src}", f"e1.{tag}.v", d, now, sha, src)
    ids["series_key"] = f"series_period:{src}:e1.{tag}.v"
    ids["src"] = src
    return ids


@pytest.mark.asyncio
async def test_run_method_reports_once_dedupes_and_logs_alerts_without_paging(conn) -> None:
    from legba.data.alerts import raise_alert as ra
    from legba.data.analysts.deterministic import run_method

    tag = uuid4().hex[:8]
    ids = await _seed(conn, tag)
    pool = _TxPool(conn)
    sink = _NoDelivery()
    ra.register_dispatcher(pool, sink)
    analyst = f"due_and_absent_e1test_{tag}"
    opts = {"sub_handler": "due_and_absent", "analyst_id": analyst}
    deps = SimpleNamespace(pg_pool=pool)
    mine = {f"forecast_question:{ids['fq']}", f"acute_forecast:{ids['acute']}",
            f"scheduled_event:{ids['sched']}", f"inquiry_expectation:{ids['inq']}", ids["series_key"]}
    try:
        first = await run_method([], dict(opts), deps)
        rows = {r["key"]: r for r in first.finding.data["rows"]}
        assert mine <= set(rows), sorted(mine - set(rows))
        assert f"scheduled_event:{ids['sched_met']}" not in rows  # a matching signal was collected
        assert all(rows[k]["transition"] == "newly_overdue" for k in mine)
        assert mine <= set(first.finding.data["ledger"])
        assert first.force_trace_only is False
        assert DA.ABSENCE_SENTENCE in first.finding.body
        # the series row names its own source with its poll class (no poll recorded: silent),
        # so the watchdog owns it and no due_and_absent alert is raised for it
        assert rows[ids["series_key"]]["alert"] == "watchdog_owns"
        assert rows[ids["series_key"]]["health"][ids["src"]]["class"] == "silent"
        assert rows[f"inquiry_expectation:{ids['inq']}"]["health"]["outputs_since_due"] == 0
        events = await conn.fetch(
            "SELECT dedupe_key, decision, severity FROM alert_events WHERE kind = 'due_and_absent'"
            " AND dedupe_key = ANY($1::text[])", [f"due_and_absent:{k}" for k in mine])
        assert len(events) == 4 and {e["decision"] for e in events} == {"logged"}
        assert {e["severity"] for e in events} == {"low"}
        assert sink.calls == []

        await conn.execute(
            "INSERT INTO analyst_outputs (kind, title, body, confidence, data, analyst_id, schema_uri, produced_at)"
            " VALUES ('finding', $1, $2, 1.0, $3::jsonb, $4, 'legba/finding/1.0.0', now())",
            first.finding.title, first.finding.body, json.dumps(first.finding.model_dump(mode="json")), analyst)
        second = await run_method([], dict(opts), deps)
        assert not (mine & {r["key"] for r in second.finding.data["rows"]})

        await conn.execute("UPDATE inquiry_ledger SET status='confirmed', closed_at=now() WHERE id=$1::uuid",
                           ids["inq"])
        await conn.execute("UPDATE scheduled_events SET retired_at=now() WHERE id=$1::uuid", ids["sched"])
        third = await run_method([], dict(opts), deps)
        got = {r["key"]: r["transition"] for r in third.finding.data["rows"]}
        assert got[f"inquiry_expectation:{ids['inq']}"] == "resolved_late"
        assert got[f"scheduled_event:{ids['sched']}"] == "voided"
        assert third.force_trace_only is False
        closed = await conn.fetch(
            "SELECT state FROM alerts WHERE dedupe_key = ANY($1::text[])",
            [f"due_and_absent:inquiry_expectation:{ids['inq']}", f"due_and_absent:scheduled_event:{ids['sched']}"])
        assert [r["state"] for r in closed] == ["closed", "closed"]
        assert sink.calls == []
    finally:
        ra.reset_dispatchers()
