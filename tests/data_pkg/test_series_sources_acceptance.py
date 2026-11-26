# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Acceptance — the generic ``series_api`` kind and F6's series, on a real
migrated Postgres (container suite, ``migrated_pg``) and a private
evidence-store root.

  * the EIA spot-price descriptor, polled through the real source actor and
    observation sink against an API double keyed like the real one: the first
    poll writes every value; re-polls of unchanged data write ZERO rows; a
    revised day writes exactly ONE row and an earlier as-of read still returns
    the old value; licence and attribution are stamped on every row; every row
    is linked to its fetch receipt; nothing reaches ``signals`` or the bus;
    the API key appears in no receipt URL;
  * the BIS policy-rate descriptor (keyless CSV): countries and the euro area
    land under their own series and subject kinds;
  * the forecast resolver reads the series for a series_threshold question
    sealed like F6 (source EIA, series RBRTE): the value on the date, AS
    RECORDED at the as-of (the earlier vintage before the revision, the
    revised one after), on both sides of the level, with the observation id
    and the fetch receipt as evidence;
  * series_change judges the price by its level rule and the rate by its step
    rule.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
import yaml

from legba.data import forecast_ledger as fl
from legba.data import forecast_resolution as fres
from legba.data import forecast_resolvers as rules
from legba.data.forecast_evidence import collect_evidence
from legba.data.observation_attribution import fetch_attributed
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources.series_api import SeriesApiSourceHandler
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import SourceCore, SourceDeps
from legba.runtime.source_factory import _unwrap_factory_dict
from legba.data.schemas.source_series_api import SeriesApiConfig

from tests.runtime._ingest_fakes import REPO, NatsRecorder, client_for

pytestmark = [pytest.mark.integration]

EIA = "source.eia.petroleum_spot_daily"
BIS = "source.bis.policy_rates_daily"
SECRET = "k3y-fict1onal-0123456789"
LEDGER = ("forecast_resolution_records", "forecast_resolutions", "forecast_resolution_proposals",
          "forecast_numbers", "forecast_questions", "scorecard_entries", "override_log")


@pytest_asyncio.fixture
async def pool(migrated_pg, clean_tables):
    await clean_tables(*LEDGER)
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with p.acquire() as c:
        from legba.runtime.state import SCHEMA

        await c.execute(SCHEMA)
        for t in ("fetch_version_tombstones", "fetch_drift_observations"):
            await c.execute(f"DELETE FROM {t}")
        await c.execute("ALTER TABLE fetch_version_records DISABLE TRIGGER fetch_version_records_insert_only")
        await c.execute("DELETE FROM fetch_version_records")
        await c.execute("ALTER TABLE fetch_version_records ENABLE TRIGGER fetch_version_records_insert_only")
        await c.execute("DELETE FROM fetch_versions")
        await c.execute("ALTER TABLE observations DISABLE TRIGGER observations_live_append_only")
        await c.execute("DELETE FROM observations WHERE origin_class = 'live'")
        await c.execute("ALTER TABLE observations ENABLE TRIGGER observations_live_append_only")
        await c.execute("DELETE FROM actor_filter_state WHERE filter_id = ANY($1::text[])",
                        [f"source:{EIA}", f"source:{BIS}"])
    yield p
    await p.close()


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    r = tmp_path / "archive"
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(r))
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")
    return r


def _descriptor(name: str, version: str) -> tuple[SourceDescriptor, SeriesApiConfig]:
    body = yaml.safe_load((REPO / "descriptors" / name).read_text())
    body["identity"]["state"] = "active"
    body["identity"]["version"] = version
    return (SourceDescriptor.model_validate(body, strict=False),
            SeriesApiConfig(**_unwrap_factory_dict(body["config"])))


def _business_days(n: int, *, end_lag: int = 2) -> list[str]:
    out, d = [], datetime.now(tz=timezone.utc).date() - timedelta(days=end_lag)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


class _EiaApi:
    """The EIA v2 spot route, keyed: 403 without the key, offset/length
    paging, ``start`` honoured, values as strings (API >= 2.1.6)."""

    def __init__(self, values: dict[tuple[str, str], float]) -> None:
        self.values = dict(values)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.params.get("api_key") != SECRET:
            return httpx.Response(403, json={"error": {"code": "API_KEY_MISSING"}})
        start = request.url.params["start"]
        rows = [{"period": d, "series": s, "value": f"{v:.2f}", "units": "$/BBL",
                 "series-description": "fictional"}
                for (d, s), v in sorted(self.values.items()) if d >= start]
        off, size = int(request.url.params["offset"]), int(request.url.params["length"])
        return httpx.Response(200, json={"response": {"total": str(len(rows)), "data": rows[off:off + size]}},
                              headers={"content-type": "application/json; charset=utf-8"})


async def _secrets(ref: str) -> str:
    assert ref == "source.eia.api_key"
    return SECRET


async def _poll(pool, name: str, version: str, transport, nats: NatsRecorder) -> dict[str, Any]:
    desc, cfg = _descriptor(name, version)
    handler = SeriesApiSourceHandler(cfg, http_client=client_for(transport))
    core = SourceCore(f"source::{desc.identity.id}::{version}",
                      SourceDeps(descriptor=desc, handler=handler,
                                 deps=StandardDeps(pg_pool=pool, nats_publish=nats, secrets_resolve=_secrets)))
    return await core.pull_once()


_ASOF = ("SELECT DISTINCT ON (valid_from) value FROM observations WHERE source_id = $1 AND series_id = $2 "
         "AND valid_from = $3 AND record_time <= $4 ORDER BY valid_from, record_time DESC")


async def _seal_f6_like(conn, *, slug: str, on: str, level: float) -> dict:
    """A question sealed like F6 (source EIA, series RBRTE), dated in the past."""
    on_d = date.fromisoformat(on)
    due = datetime(on_d.year, on_d.month, on_d.day, 23, 59, 59, tzinfo=timezone.utc)
    sealed = due - timedelta(days=20)
    q = fl.ForecastQuestion(
        slug=slug, question=f"Fictional: EIA Brent spot (series RBRTE) is at or above ${level:g}/bbl on {on}.",
        resolver_kind="series_threshold",
        resolver_spec={"source": "EIA", "series": "RBRTE", "op": ">=", "value": level, "on": on,
                       "holiday_rule": "last trading day on or before the date"},
        due_at=due, grace_hours=120,
        void_rules="EIA publishes no RBRTE value for the date (or the last trading day before) by due + grace.",
    )
    _, qid = await fl.upsert_question(conn, q)
    await fl.state_number(conn, qid, fl.ForecastNumber("operator", "operator", 0.3, sealed - timedelta(hours=1)))
    await fl.seal_question(conn, qid, at=sealed)
    row = dict(await conn.fetchrow(fl._SELECT_QUESTION_BY_ID, qid))
    row["resolver_spec"] = fl._json_field(row["resolver_spec"])
    return row


@pytest.mark.asyncio
async def test_eia_series_end_to_end_and_f6_reads_it(pool, root) -> None:
    days = _business_days(15)
    api = _EiaApi({(d, s): (90.0 + i if s == "RBRTE" else 85.0 + i)
                   for i, d in enumerate(days) for s in ("RBRTE", "RWTC")})
    on = days[-5]                                  # later days are held: a published date
    api.values[(on, "RBRTE")] = 101.25             # above the F6 level on the date
    nats = NatsRecorder()

    first = await _poll(pool, "source_eia_petroleum_spot_daily.yaml", "e" * 16, api, nats)
    assert first["sink_counts"]["observation"]["written"] == 30, first
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM observations WHERE source_id = $1", EIA) == 30
        assert await c.fetchval("SELECT count(*) FROM signals WHERE source_id = $1", EIA) == 0
        prov = await c.fetch("SELECT provenance FROM observations WHERE source_id = $1", EIA)
        import json as _json

        stamps = {(p["license_class"], p["attribution"]) for p in (_json.loads(r["provenance"]) for r in prov)}
        assert stamps == {("public_domain", "Source: U.S. Energy Information Administration (EIA), spot "
                                            "prices FOB; release date = the row's record time")}
        assert await c.fetchval("SELECT count(*) FROM fetch_version_records WHERE record_kind = 'observation'") == 30
        urls = [r["url"] for r in await c.fetch("SELECT url, address FROM fetch_versions")]
        addrs = [r["address"] for r in await c.fetch("SELECT address FROM fetch_versions")]
        assert urls and all(SECRET not in u and "[redacted]" in u for u in urls)
        assert all(SECRET not in (a or "") for a in addrs)
        src_urls = {r["source_url"] for r in await c.fetch("SELECT source_url FROM observations")}
        assert all(SECRET not in u for u in src_urls)
        attributed = await fetch_attributed(c, EIA, series_id="eia.pet_spot.rbrte")
        assert len(attributed) == 15 and {a["licence_basis"] for a in attributed} == {"row"}
    assert nats.messages == []

    second = await _poll(pool, "source_eia_petroleum_spot_daily.yaml", "e" * 16, api, nats)
    third = await _poll(pool, "source_eia_petroleum_spot_daily.yaml", "e" * 16, api, nats)
    assert second["sink_counts"]["observation"]["written"] == 0
    assert third["sink_counts"]["observation"] == {"written": 0, "unchanged": 30, "revised": 0, "rejected": 0}

    async with pool.acquire() as c:
        # F6-like questions on both sides of the level, read before any revision.
        q_above = await _seal_f6_like(c, slug="FX6a-brent", on=on, level=100)
        q_below = await _seal_f6_like(c, slug="FX6b-brent", on=on, level=120)
    between = datetime.now(tz=timezone.utc)

    # A late revision of the date's value: exactly one new row.
    api.values[(on, "RBRTE")] = 99.40
    fourth = await _poll(pool, "source_eia_petroleum_spot_daily.yaml", "e" * 16, api, nats)
    assert fourth["sink_counts"]["observation"]["written"] == 1
    assert fourth["sink_counts"]["observation"]["revised"] == 1
    async with pool.acquire() as c:
        d = date.fromisoformat(on)
        assert float(await c.fetchval(_ASOF, EIA, "eia.pet_spot.rbrte", d, between)) == 101.25
        assert float(await c.fetchval(_ASOF, EIA, "eia.pet_spot.rbrte", d, datetime.now(tz=timezone.utc))) == 99.40

        # The resolver reads the series AS RECORDED at its as-of.
        ctx_before = rules.ResolverContext(as_of=between)
        found = await collect_evidence(c, q_above, as_of=between, ctx=ctx_before)
        p = rules.evaluate(q_above, {}, ctx_before, observations=found["observations"])
        assert p.lean == "yes" and p.evidence[0]["value"] == 101.25
        ev = p.evidence[0]
        assert ev["ref"].startswith("observation:") and ev["fetch_version_id"] is not None
        assert ev["fetch_sha256"] and ev["source_id"] == EIA and ev["basis"] == "the sealed date"
        receipt = await c.fetchrow("SELECT sha256 FROM fetch_versions WHERE id = $1", ev["fetch_version_id"])
        assert receipt["sha256"] == ev["fetch_sha256"]

        now = datetime.now(tz=timezone.utc)
        found = await collect_evidence(c, q_above, as_of=now, ctx=rules.ResolverContext(as_of=now))
        p = rules.evaluate(q_above, {}, rules.ResolverContext(as_of=now), observations=found["observations"])
        assert p.outcome == "no" and p.evidence[0]["value"] == 99.40   # the revision, read after it

        # The job (as the analyst runs it): due, so the rule's proposal is
        # written with its evidence; series_threshold is machine-resolvable
        # and settles by rule (forecast_ledger.MACHINE_RESOLVABLE).
        report = await fres.run_resolution_job(c, now=now, slugs=["FX6a-brent", "FX6b-brent"])
        got = {e["slug"]: e for e in report["questions"]}
        assert got["FX6a-brent"]["outcome"] == "no" and got["FX6b-brent"]["outcome"] == "no"
        assert report["written"] == 2 and report["settled"] == 2
        refs = await c.fetchval("SELECT evidence_refs FROM forecast_resolutions WHERE question_id = $1::uuid",
                                q_below["id"])
        assert any(r.startswith("observation:") for r in refs) and any(r.startswith("fetch_version:") for r in refs)
        again = await fres.run_resolution_job(c, now=now, slugs=["FX6a-brent", "FX6b-brent"])
        assert again["questions"] == []                                   # resolved: no longer open

    # series_change: the price judged by its level rule, never by the band.
    from legba.data.analysts.deterministic_handlers import series_change

    deps = type("D", (), {"pg_pool": pool})()
    res = await series_change.handle([], {"source_ids": [EIA], "level_rules": ["eia.pet_spot.rbrte>=100"],
                                          "current_days": 10}, deps)
    data = res.finding.data
    assert data["method_version"] == "series_change/2026-11.3"   # Named family rules (unused here)
    # Brent: 99.40 (the revised date) -> 101 the next day enters >= 100; holding
    # above it afterwards is not an event. WTI has no rule: the band, and 15
    # days are too few for its baseline.
    (entered,) = data["crossings"]
    assert (entered["series_id"], entered["status"], entered["from"]["value"], entered["to"]["value"]) == (
        "eia.pet_spot.rbrte", "level_entered", 99.4, 101.0)
    assert data["counts"]["event"] == 1 and data["counts"]["insufficient"] == 1
    assert data["counts"]["above"] == data["counts"]["below"] == data["counts"]["within"] == 0


class _BisApi:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=self.body, headers={"content-type": "text/csv;charset=UTF-8"})


def _bis_csv(rates: dict[tuple[str, str], str]) -> bytes:
    head = ("FREQ,REF_AREA,UNIT_MEASURE,UNIT_MULT,TIME_FORMAT,COMPILATION,DECIMALS,SOURCE_REF,"
            "SUPP_INFO_BREAKS,TITLE,TIME_PERIOD,OBS_VALUE,OBS_STATUS,OBS_CONF,OBS_PRE_BREAK\n")
    rows = "".join(f"D,{a},368,0,,fictional,4,Fictional bank,, Central bank policy rates - {a},{d},{v},"
                   f"{'M' if v == 'NaN' else 'A'},F,\n" for (a, d), v in sorted(rates.items()))
    return (head + rows).encode()


@pytest.mark.asyncio
async def test_bis_policy_rates_land_by_subject_kind_and_a_cut_is_a_step(pool, root) -> None:
    today = datetime.now(tz=timezone.utc).date()
    days = [(today - timedelta(days=4 + i)).isoformat() for i in range(10)][::-1]
    rates = {("US", d): ("3.875" if i < 7 else "3.625") for i, d in enumerate(days)}
    rates.update({("XM", d): "2" for d in days})
    rates[("CL", days[0])] = "NaN"
    api = _BisApi(_bis_csv(rates))
    nats = NatsRecorder()
    first = await _poll(pool, "source_bis_policy_rates_daily.yaml", "b" * 16, api, nats)
    assert first["sink_counts"]["observation"]["written"] == 20, first
    again = await _poll(pool, "source_bis_policy_rates_daily.yaml", "b" * 16, api, nats)
    assert again["sink_counts"]["observation"]["written"] == 0
    async with pool.acquire() as c:
        kinds = {(r["series_id"], r["subject"], r["subject_kind"]) for r in await c.fetch(
            "SELECT DISTINCT series_id, subject, subject_kind FROM observations WHERE source_id = $1", BIS)}
        assert kinds == {("bis.cbpol.policy_rate", "US", "country"), ("bis.cbpol.policy_rate_area", "XM", "entity")}
        assert await c.fetchval("SELECT count(*) FROM signals WHERE source_id = $1", BIS) == 0
    assert nats.messages == []

    from legba.data.analysts.deterministic_handlers import series_change

    deps = type("D", (), {"pg_pool": pool})()
    res = await series_change.handle([], {"source_ids": [BIS], "step_series": ["bis.cbpol.*"]}, deps)
    data = res.finding.data
    (cut,) = data["crossings"]
    assert (cut["subject"], cut["status"], cut["from"]["value"], cut["to"]["value"]) == ("US", "step_down", 3.875, 3.625)
    assert data["counts"]["event"] == 1 and data["counts"]["quiet"] == 1      # XM held flat: quiet, not "within"
    assert len(data["citations"]) == 2 and [str(u) for u in res.derived_from] == [x["ref_id"] for x in data["citations"]]
