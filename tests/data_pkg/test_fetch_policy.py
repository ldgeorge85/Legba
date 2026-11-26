# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""the per-host fetch policy and everything it drives.

The defect this pins: every Times of Israel / Jerusalem Post signal of the
last 30 days carries a summary and no ``payload.text`` — both hosts answer
every fetch with an anti-bot challenge (http_403), logged as
``evidence_archiver.blocked_by_challenge`` several times a day. The lane
adds the per-host registry (``sources/_fetch_policy``), the archiver's
impersonation escalation, the same consult in ``web_fetch_tool``, the
per-feed counters on the source-health surface, and the
``evidence_refetch`` backlog drain.

All fakes — no network, no Postgres. The neighbouring suites' idiom: the
HTTP plan is scripted through a fake ``fetch_client`` factory; the store is
a fake pool whose ``conn.fetch`` hands the sweep its candidate rows.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

from legba.data import archive_codec
from legba.data.analysts.agency import web_tools
from legba.data.analysts.agency.tools import ToolCall
from legba.data.analysts.deterministic_handlers import evidence_archiver as ea
from legba.data.analysts.deterministic_handlers import evidence_refetch as er
from legba.data.option_catalog.handler_options import known_option_names
from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.sources import _fetch_policy as fp
from legba.data.sources._egress import FETCH_IMPERSONATE_ENV
from legba.runtime.dapr_actors import _merge_descriptor_options


# ---------------------------------------------------------------------------
# Fakes — the scripted transport and the fake store
# ---------------------------------------------------------------------------


_ARTICLE_HTML = (
    b"<!doctype html><html><head><title>Article</title></head><body>"
    b"<article><h1>A story</h1>"
    b"<p>The article body the consult was looking for: a long-enough, "
    b"perfectly ordinary news paragraph, with a subject, a verb, and no "
    b"challenge chrome anywhere in it. The publisher served the real page "
    b"to the impersonated fingerprint.</p>"
    b"<p>A second paragraph so the extractor has enough text to keep.</p>"
    b"</article></body></html>"
)


class _Resp:
    """The streaming-response shape ``_fetch_bytes`` reads."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status_code = status
        self._body = body
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.charset_encoding = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://edge.invalid/")
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError(
                f"edge {self.status_code}", request=req, response=resp,
            )

    async def aiter_bytes(self):
        yield self._body


class _Stream:
    """The async context manager ``client.stream`` returns."""

    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """An opened client whose ``stream`` consults the scripted plan."""

    def __init__(self, plan, *, impersonated: bool) -> None:
        self._plan = plan
        self._impersonated = impersonated

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method: str, url: str):
        status, body = self._plan(urlsplit(url).hostname, self._impersonated)
        return _Stream(_Resp(status, body))


def _fake_fetch_factory(record: list, plan):
    """A ``fetch_client`` stand-in that records every construction kwarg.

    ``plan(host, impersonated) -> (status, body)`` scripts the transport:
    the defect is a 403 on the plain leg and the real page on the
    impersonated leg.
    """

    def factory(*, impersonate=None, guarded, **kwargs):
        record.append(
            {"impersonate": impersonate, "guarded": guarded, **kwargs}
        )
        return _FakeClient(plan, impersonated=(impersonate is True))

    return factory


class _FakeConn:
    """``fetch`` hands the sweep its candidate rows; ``execute`` records."""

    def __init__(self, rows) -> None:
        self._rows = list(rows)
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *params):
        return list(self._rows)

    async def execute(self, sql, *params):
        self.executed.append((sql, params))
        return "OK"


class _Acquire:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


def _signal_row(source_id: str, url: str, *, signal_id: str = "sig-1") -> dict:
    return {
        "id": signal_id,
        "source_id": source_id,
        "canonical_url": url,
        "media_ref": None,
        "payload": {},
        "raw_provenance": {},
        "retention_class": "regular",
        "retrieval_origin": None,
        "prior_attempts": 0,
    }


# ---------------------------------------------------------------------------
# 1) The registry itself
# ---------------------------------------------------------------------------


def test_seeded_hosts_resolve_their_policy():
    for host in ("timesofisrael.com", "www.timesofisrael.com",
                 "jpost.com", "www.jpost.com"):
        policy = fp.fetch_policy_for(f"https://{host}/a-story")
        assert policy.impersonate == fp.IMPERSONATE_ON
        assert policy.http2 is False


def test_an_unlisted_host_gets_the_default_policy():
    policy = fp.fetch_policy_for("https://example.org/story")
    assert policy is fp.DEFAULT_FETCH_POLICY
    assert policy.impersonate == fp.IMPERSONATE_AUTO
    assert policy.http2 is None
    assert policy.max_retries == 0
    assert policy.backoff_s == 0.0


def test_host_of_normalises_url_and_bare_host():
    assert fp.host_of("https://WWW.JPost.com:443/a?x=1") == "www.jpost.com"
    assert fp.host_of("WWW.TIMESOFISRAEL.COM") == "www.timesofisrael.com"


# ---------------------------------------------------------------------------
# 2) The global-flag ordering rule
# ---------------------------------------------------------------------------


def test_policy_on_escalates_when_the_global_is_unset(monkeypatch):
    for value in (None, "", "  "):
        if value is None:
            monkeypatch.delenv(FETCH_IMPERSONATE_ENV, raising=False)
        else:
            monkeypatch.setenv(FETCH_IMPERSONATE_ENV, value)
        assert fp.impersonate_kwarg(fp.fetch_policy_for("jpost.com")) is True
        assert fp.impersonate_kwarg(fp.fetch_policy_for("example.org")) is None


def test_an_explicit_global_off_disables_impersonation_fleet_wide(monkeypatch):
    """`LEGBA_FETCH_IMPERSONATE=off` is the kill switch: no registry row may
    turn a host on under it, and every resolution is an explicit False."""
    on = fp.FetchPolicy(impersonate=fp.IMPERSONATE_ON)
    for value in ("off", "OFF", " off ", "0", "false", "no"):
        monkeypatch.setenv(FETCH_IMPERSONATE_ENV, value)
        assert fp.impersonate_kwarg(fp.fetch_policy_for("jpost.com")) is False
        assert fp.impersonate_kwarg(fp.fetch_policy_for("timesofisrael.com")) is False
        assert fp.impersonate_kwarg(on) is False
        assert fp.impersonate_kwarg(fp.fetch_policy_for("example.org")) is False


@pytest.mark.asyncio
async def test_an_explicit_global_off_stops_the_archiver_escalation(
    monkeypatch, tmp_path,
):
    """A policy host's 403 under a global `off` stays a plain failure: the
    block is counted and the impersonated leg is never opened."""
    monkeypatch.setenv(ea.ARCHIVE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(FETCH_IMPERSONATE_ENV, "off")
    record: list[dict] = []
    monkeypatch.setattr(
        ea, "fetch_client",
        _fake_fetch_factory(
            record, lambda host, imp: (200, _ARTICLE_HTML) if imp else (403, b""),
        ),
    )
    row = _signal_row("src_timesofisrael", "https://www.timesofisrael.com/story")
    counters = await ea._sweep(
        _FakePool(_FakeConn([row])),
        {"per_host_delay_seconds": 0, "timeout_seconds": 5},
    )
    assert counters["archived"] == 0
    assert counters["blocked_by_challenge"] == 1
    assert len(record) == 1 and record[0]["impersonate"] is None


def test_a_global_on_is_never_weakened_by_a_policy(monkeypatch):
    """`never off when the global is on`: even a registry `off` loses."""
    monkeypatch.setenv(FETCH_IMPERSONATE_ENV, "on")
    assert fp.impersonate_kwarg(fp.fetch_policy_for("jpost.com")) is None
    off = fp.FetchPolicy(impersonate=fp.IMPERSONATE_OFF)
    assert fp.impersonate_kwarg(off) is None
    monkeypatch.setenv(FETCH_IMPERSONATE_ENV, "fallback")
    assert fp.impersonate_kwarg(off) is None


# ---------------------------------------------------------------------------
# 3) The done test — the archiver leg, fake transport, per-feed counters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_policy_host_block_escalates_and_still_counts_the_block(
    monkeypatch, tmp_path,
):
    """403 on the plain client, the real body on the impersonated one:

    the archiver stores the body AND tallies challenge_blocked=1 beside
    body_stored=1 for the feed — the refusal happened and is counted, even
    though the escalation won.
    """
    monkeypatch.setenv(ea.ARCHIVE_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(FETCH_IMPERSONATE_ENV, raising=False)

    def plan(host, impersonated):
        if impersonated:
            return 200, _ARTICLE_HTML
        if host == "www.timesofisrael.com":
            return 403, b""
        return 200, _ARTICLE_HTML

    record: list[dict] = []
    monkeypatch.setattr(ea, "fetch_client", _fake_fetch_factory(record, plan))

    row = _signal_row("src_timesofisrael", "https://www.timesofisrael.com/story")
    conn = _FakeConn([row])
    counters = await ea._sweep(
        _FakePool(conn), {"per_host_delay_seconds": 0, "timeout_seconds": 5},
    )

    assert counters["archived"] == 1
    assert counters["blocked_by_challenge"] == 1
    feed = counters["per_source"]["src_timesofisrael"]
    assert feed == {
        "fetched": 1,
        "challenge_blocked": 1,
        "boilerplate_rejected": 0,
        "body_stored": 1,
    }
    # The body really landed in CAS — one object under {sha[:2]}/{sha}.
    stored = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(stored) == 1
    assert archive_codec.decode(stored[0].read_bytes()) == _ARTICLE_HTML
    # The sidecar recorded 'archived' and the signal was stamped.
    sqls = [sql for sql, _ in conn.executed]
    assert any("INSERT INTO evidence_archive" in s for s in sqls)
    assert any("archived_text" in s for s in sqls)
    # Two clients built: the plain primary (byte-identical kwargs) and the
    # impersonated leg.
    assert len(record) == 2
    assert record[0] == {
        "impersonate": None,
        "guarded": ea.guarded_async_client,
        "timeout": 5.0,
        "follow_redirects": True,
        "headers": {"User-Agent": ea._USER_AGENT},
        "http2": False,
    }
    assert record[1]["impersonate"] is True


@pytest.mark.asyncio
async def test_an_unlisted_hosts_construction_is_byte_identical_to_today(
    monkeypatch, tmp_path,
):
    """A host absent from the registry sees today's client construction,
    golden on the kwargs."""
    monkeypatch.setenv(ea.ARCHIVE_ROOT_ENV, str(tmp_path))
    record: list[dict] = []
    monkeypatch.setattr(
        ea, "fetch_client",
        _fake_fetch_factory(record, lambda host, imp: (200, _ARTICLE_HTML)),
    )
    row = _signal_row("src_example", "https://example.org/story")
    await ea._sweep(
        _FakePool(_FakeConn([row])),
        {"per_host_delay_seconds": 0, "timeout_seconds": 5},
    )
    assert len(record) == 1  # no escalation leg was ever opened
    assert record[0] == {
        "impersonate": None,
        "guarded": ea.guarded_async_client,
        "timeout": 5.0,
        "follow_redirects": True,
        "headers": {"User-Agent": ea._USER_AGENT},
        "http2": False,
    }


@pytest.mark.asyncio
async def test_an_unlisted_hosts_403_stays_a_plain_retryable_failure(
    monkeypatch, tmp_path,
):
    """No policy ⇒ no escalation: a 403 stays the counted, recorded failure
    it always was, and the impersonated leg is never opened."""
    monkeypatch.setenv(ea.ARCHIVE_ROOT_ENV, str(tmp_path))
    record: list[dict] = []
    monkeypatch.setattr(
        ea, "fetch_client",
        _fake_fetch_factory(record, lambda host, imp: (403, b"")),
    )
    row = _signal_row("src_example", "https://example.org/story")
    conn = _FakeConn([row])
    counters = await ea._sweep(
        _FakePool(conn), {"per_host_delay_seconds": 0, "timeout_seconds": 5},
    )
    assert counters["archived"] == 0
    assert counters["fetch_failed"] == 1
    assert counters["blocked_by_challenge"] == 1
    feed = counters["per_source"]["src_example"]
    assert feed["challenge_blocked"] == 1
    assert feed["body_stored"] == 0
    assert len(record) == 1 and record[0]["impersonate"] is None
    status = [p[1] for s, p in conn.executed if "INSERT INTO evidence_archive" in s][0]
    assert status == "failed"


# ---------------------------------------------------------------------------
# 4) web_fetch_tool consults the same policy
# ---------------------------------------------------------------------------


class _GetResp:
    def __init__(self, url: str) -> None:
        self.status_code = 200
        self.headers = {"content-type": "text/html"}
        self.url = url
        self.text = "<html><body>article</body></html>"


class _GetClient:
    def __init__(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        return _GetResp(url)


def _recording_get_factory(record: list):
    def factory(*, impersonate=None, guarded, **kwargs):
        record.append(
            {"impersonate": impersonate, "guarded": guarded, **kwargs}
        )
        return _GetClient()

    return factory


@pytest.mark.asyncio
async def test_web_fetch_builds_the_impersonated_client_for_a_policy_host(
    monkeypatch,
):
    record: list[dict] = []
    monkeypatch.setattr(
        web_tools, "fetch_client", _recording_get_factory(record),
    )
    call = ToolCall(
        pack_id="web_access", tool_name="web_fetch",
        args={"url": "https://www.timesofisrael.com/story"},
    )
    result = await web_tools.web_fetch_tool(
        call, SimpleNamespace(tools=[]), SimpleNamespace(),
    )
    assert result.status == "completed"
    assert result.output.get("fetch_outcome") == "ok"
    assert len(record) == 1
    kwargs = record[0]
    assert kwargs["impersonate"] is True
    assert kwargs["http2"] is False


@pytest.mark.asyncio
async def test_web_fetch_an_unlisted_host_gets_todays_kwargs(monkeypatch):
    record: list[dict] = []
    monkeypatch.setattr(
        web_tools, "fetch_client", _recording_get_factory(record),
    )
    call = ToolCall(
        pack_id="web_access", tool_name="web_fetch",
        args={"url": "https://example.org/story"},
    )
    result = await web_tools.web_fetch_tool(
        call, SimpleNamespace(tools=[]), SimpleNamespace(),
    )
    assert result.status == "completed"
    assert result.output.get("fetch_outcome") == "ok"
    assert record == [{
        "impersonate": None,
        "guarded": web_tools.guarded_async_client,
        "follow_redirects": True,
        "timeout": web_tools._DEFAULT_TIMEOUT_SECONDS,
        "headers": {"User-Agent": web_tools._USER_AGENT},
    }]


# ---------------------------------------------------------------------------
# 5) The re-fetch sweep — fake store + fake archiver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_refetch_sweep_re_fetches_textless_policy_host_signals(
    monkeypatch, tmp_path,
):
    """Three textless signals on policy hosts (plus one on an unlisted host,
    proving the authoritative filter) → three re-fetches, OLDEST FIRST,
    counters recorded on the archiver's own vocabulary."""
    monkeypatch.setenv(ea.ARCHIVE_ROOT_ENV, str(tmp_path))
    # Newest first, as the SQL returns them.
    rows = [
        _signal_row("src_timesofisrael",
                    "https://www.timesofisrael.com/newest", signal_id="s3"),
        _signal_row("src_jpost",
                    "https://www.jpost.com/middle", signal_id="s2"),
        _signal_row("src_jpost",
                    "https://www.jpost.com/oldest", signal_id="s1"),
        _signal_row("src_example",
                    "https://example.org/unlisted", signal_id="sx"),
    ]
    conn = _FakeConn(rows)

    calls: list[str] = []

    async def fake_archive(pool, clients, row, *, counters, **kwargs):
        calls.append(row["id"])
        counters["archived"] += 1
        ea._feed(counters, row["source_id"])["body_stored"] += 1

    monkeypatch.setattr(er, "_archive_one", fake_archive)

    result = await er.handle(
        [],
        {"refetch_max_per_tick": 10, "refetch_window_days": 30},
        SimpleNamespace(pg_pool=_FakePool(conn)),
    )
    data = result.finding.data
    assert calls == ["s1", "s2", "s3"]  # oldest first
    assert data["examined"] == 3
    assert data["refetched"] == 3
    assert data["archived"] == 3
    assert data["per_source"]["src_jpost"]["body_stored"] == 2
    assert data["per_source"]["src_timesofisrael"]["body_stored"] == 1


def test_refetch_selects_by_policy_hosts(monkeypatch):
    """The sweep's host filter is the registry, not a hard-coded list."""
    assert er._policy_refetched("https://www.jpost.com/x") is True
    assert er._policy_refetched("https://www.timesofisrael.com/x") is True
    assert er._policy_refetched("https://example.org/x") is False
    assert sorted(fp.policy_hosts()) == ["jpost.com", "timesofisrael.com"]


# ---------------------------------------------------------------------------
# 6) Registration — sub_handler, TRACE_ONLY, the declared options, DRAFT yaml
# ---------------------------------------------------------------------------


def test_the_sub_handler_is_registered_trace_only():
    from legba.data.analysts import deterministic as det
    from legba.data.provenance.kinds import TRACE_ONLY

    assert det.SUB_HANDLERS["evidence_refetch"] is er.handle
    assert det.OUTPUT_KIND_BY_SUB_HANDLER["evidence_refetch"] is TRACE_ONLY


def test_the_options_are_declared_before_the_yaml_uses_them():
    names = known_option_names("evidence_refetch")
    assert {"refetch_max_per_tick", "refetch_window_days"} <= set(names)


DESCRIPTOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "descriptors"
    / "analyst_evidence_refetch.yaml"
)


def test_the_shipped_descriptor_is_draft_and_its_options_merge():
    body = yaml.safe_load(DESCRIPTOR_PATH.read_text())
    body["identity"]["version"] = "0" * 16  # the registry stamps the hash
    descriptor = AnalystDescriptor.model_validate(body, strict=False)
    assert descriptor.identity.state == "draft"

    options = {
        "analyst_id": "evidence_refetch",
        "analyst_version": "abc",
        "run_id": "r-1",
        "sub_handler": "evidence_refetch",
    }
    receipt = _merge_descriptor_options(options, descriptor)
    assert receipt is not None and receipt["status"] == "applied"
    assert options["refetch_max_per_tick"] == 50
    assert options["refetch_window_days"] == 30


def test_an_undeclared_option_value_degrades_loudly():
    body = yaml.safe_load(DESCRIPTOR_PATH.read_text())
    body["identity"]["version"] = "0" * 16
    body["method"]["options"] = {"refetch_max_per_tick": 999999}
    descriptor = AnalystDescriptor.model_validate(body, strict=False)
    options = {"sub_handler": "evidence_refetch"}
    receipt = _merge_descriptor_options(options, descriptor)
    assert receipt is not None
    assert receipt["rejected"]
    assert "refetch_max_per_tick" not in options


# ---------------------------------------------------------------------------
# The source-health read: body_extraction is one grouped, windowed pass
# ---------------------------------------------------------------------------


class _HealthConn:
    def __init__(self, row: dict) -> None:
        self.row = row
        self.sql: list[str] = []

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        # Only the first query (the per-source rows) returns the row.
        return [self.row] if len(self.sql) == 1 else []

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        return None

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        return 0


class _HealthPool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _health_row(**be) -> dict:
    row = {
        "source_id": "source.timesofisrael.news", "name": "ToI", "state": "active",
        "last_signal_at": None, "last_poll_outcome": "success",
        "last_health_state": None, "last_poll_at": None,
        "last_poll_newest_entry_ts": None, "last_poll_keyword_filtered": None,
        "last_ok_poll_at": None, "cadence_raw": None,
        "be_fetched": None, "be_challenge_blocked": None,
        "be_boilerplate_rejected": None, "be_body_stored": None,
    }
    row.update(be)
    return row


@pytest.mark.asyncio
async def test_source_health_body_extraction_is_one_windowed_grouped_pass():
    """The counters land on the row, and the SQL aggregates evidence_archive
    ONCE (a grouped CTE over the window), never a per-feed lateral — the
    lateral form cost 3.6 s warm on the live table (2026-11-05)."""
    from legba.runtime import substrate_source_health as sh

    conn = _HealthConn(_health_row(
        be_fetched=560, be_challenge_blocked=532,
        be_boilerplate_rejected=0, be_body_stored=20,
    ))
    out = await sh.get_source_health(_HealthPool(conn))
    sql = conn.sql[0]
    assert sql.startswith("WITH be AS")
    assert "GROUP BY es.source_id" in sql
    assert f"interval '{sh.BODY_EXTRACTION_WINDOW_DAYS} days'" in sql
    assert ") be ON TRUE" not in sql
    be = out["rows"][0]["body_extraction"]
    assert be["fetched"] == 560 and be["challenge_blocked"] == 532
    assert be["body_stored"] == 20
    assert be["body_extraction_rate"] == pytest.approx(20 / 560)
    assert be["window_days"] == sh.BODY_EXTRACTION_WINDOW_DAYS


@pytest.mark.asyncio
async def test_the_staleness_tripwire_skips_the_body_extraction_aggregate():
    from legba.runtime import substrate_source_health as sh

    conn = _HealthConn(_health_row())
    out = await sh.get_source_health(_HealthPool(conn), include_body_extraction=False)
    assert "evidence_archive" not in conn.sql[0]
    assert out["rows"][0]["body_extraction"] is None
    # The watchdog is the caller that opts out.
    import inspect
    from legba.runtime import source_staleness_watch as sw
    assert "include_body_extraction=False" in inspect.getsource(sw)
