# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-1 — the cyber desk: its sources, the slice's TEXT ARM, and selection.

* The text arm (``scope.signal_text_terms``, data/signal_slice_scope.py): the
  term grammar, the two regex dialects agreeing, the SQL it renders beside the
  tag arm, and that a target declaring no terms renders byte-identical clauses
  (the pre-K-1 strings, spelled out here) and dumps without the key (content
  hash unchanged).
* The json_api ``body_template`` through the real handler on a KEV-shaped
  record (no payload from the real endpoint is committed or fetched).
* Every K-1 descriptor validates (schema + handler config), declares the
  desk's classification, and that classification is a seeded vocabulary
  value; the target renders the two-arm statement and no geo clause.
* Selection on synthetic signals — a vulnerability entry, an advisory, a trade
  article, an outage report with no geography, a country-tagged outage, and a
  control — reaches the cyber desk and the right country desk ONCE each: in
  Python (the mirror, runs anywhere) and through the real slice reader on the
  real driver (the isolated test stack only).
"""

from __future__ import annotations

from tests._lifecycle_rows import lifecycle_text
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import yaml
from nacl.signing import SigningKey

from legba.data.schemas.source import SourceDescriptor
from legba.data.schemas.target import TargetDescriptor
from legba.data.signal_slice_scope import (
    CANONICAL_ONLY_SQL,
    SLICE_TEXT_SQL,
    SignalTagFilter,
    slice_text,
    target_tag_filter,
    text_term_error,
    text_terms_match,
    text_terms_regex,
)
from legba.data.sources._contract import InMemoryStateStore, SourceContext
from legba.data.sources.json_api import JsonApiConfig, JsonApiSourceHandler
from legba.data.sources.rss import RSSConfig
from legba.runtime.actor_substrate_slice import _read_substrate_slice
from legba.runtime.source_factory import _unwrap_factory_dict

_ROOT = Path(__file__).resolve().parents[2]
_DESCRIPTORS = _ROOT / "descriptors"
CYBER = "geopolitics.topic:cyber"

K1_SOURCES = (
    "source_cisa_kev.yaml", "source_cisa_advisories.yaml", "source_ncsc_uk.yaml",
    "source_certfr_alerts.yaml", "source_certfr_cti.yaml", "source_certeu_advisories.yaml",
    "source_bsi_press.yaml", "source_therecord.yaml", "source_cyberscoop.yaml",
    "source_securityweek.yaml", "source_aws_status.yaml", "source_azure_status.yaml",
    "source_gcp_status.yaml", "source_gworkspace_status.yaml",
)
TARGET = _DESCRIPTORS / "target_theme_cyber.yaml"


def _yaml(name: str | Path) -> dict[str, Any]:
    p = name if isinstance(name, Path) else _DESCRIPTORS / name
    body = yaml.safe_load(p.read_text())
    body["identity"]["version"] = "0" * 16
    return body


# ---------------------------------------------------------------------------
# The text arm: grammar and matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("term", ["ransomware", "cyber attack", "cve-", "cert-fr", "apt28",
                                  "denial of service"])
def test_valid_terms(term):
    assert text_term_error(term) is None


@pytest.mark.parametrize("term", ["", "Ransomware", "cyber  attack", "a.b", "x|y", "(apt)",
                                  "-lead", "ends -", "é", "x" * 49, 7])
def test_invalid_terms_are_refused_by_name(term):
    assert text_term_error(term) is not None
    if isinstance(term, str):
        with pytest.raises(ValueError):
            text_terms_regex([term], dialect="python")


@pytest.mark.parametrize(("text", "hit"), [
    ("Hospital hit by cyberattack", True),
    ("Hospital hit by cyber-attack", True),
    ("Hospital hit by Cyber Attack", True),
    ("North Korean HACKERS steal $400m", True),
    ("Patch CVE-2026-114713 now", True),
    ("A cyberpunk novel", False),
    ("Ethical hackathon opens", False),       # word start: 'hacker' is not 'hackathon'
    ("Microsoft outage was a technical issue", False),
    ("Global outage hits Outlook", True),
    ("A denial-of-service flood", True),
])
def test_matching_is_word_start_and_joiner_tolerant(text, hit):
    terms = ("cyber attack", "hacker", "cve-", "global outage", "denial of service")
    assert text_terms_match(terms, text) is hit


def test_the_two_dialects_differ_only_in_the_word_start_anchor():
    terms = ("cyber attack", "cve-", "ransomware")
    pg = text_terms_regex(terms, dialect="postgres")
    py = text_terms_regex(terms, dialect="python")
    assert pg.startswith(r"\m(?:") and py.startswith(r"\b(?:")
    assert pg[2:] == py[2:]
    assert pg == r"\m(?:cyber[\s-]?attack|cve-|ransomware)"
    with pytest.raises(ValueError):
        text_terms_regex(terms, dialect="mysql")


def test_slice_text_mirrors_the_sql_expression():
    assert SLICE_TEXT_SQL == (
        "(coalesce(payload->>'title','') || ' ' || "
        "coalesce(payload->>'summary', payload->>'distilled_body', ''))"
    )
    assert slice_text({"title": "T", "summary": "S", "distilled_body": "D"}) == "T S"
    assert slice_text({"title": "T", "distilled_body": "D"}) == "T D"
    assert slice_text({"summary": "S"}) == " S"
    assert slice_text({"title": "T", "summary": ""}) == "T "   # '' is not NULL
    assert slice_text(None) == " "


# ---------------------------------------------------------------------------
# The text arm: SQL, byte identity, the Python mirror
# ---------------------------------------------------------------------------


def test_no_terms_renders_the_pre_k1_clauses_byte_for_byte():
    params: list[Any] = []
    got = SignalTagFilter(include=("x", "src:x"), exclude=("sport",)).sql(params)
    assert got == ["tags && $1::text[]", "NOT (tags && $2::text[])"]
    params2: list[Any] = ["w"]
    fb = SignalTagFilter(include=("x",), untagged_fallback=True).sql(params2)
    assert fb == ["(tags && $2::text[] OR tags = '{}')"]
    assert target_tag_filter({"geo": ["US"]}).sql([]) == []
    assert not target_tag_filter({"geo": ["US"]}).active


def test_tags_or_text_is_one_clause_with_the_regex_bound():
    params: list[Any] = [24]
    f = target_tag_filter({"signal_tags": [CYBER], "signal_text_terms": ["ransomware", "cve-"]})
    got = f.sql(params)
    assert got == [f"(tags && $2::text[] OR {SLICE_TEXT_SQL} ~* $3)"]
    assert params == [24, [CYBER], r"\m(?:ransomware|cve-)"]


def test_text_only_and_exclude_compose():
    params: list[Any] = []
    f = SignalTagFilter(exclude=("sport",), text_terms=("ransomware",))
    assert f.active
    assert f.sql(params) == [f"{SLICE_TEXT_SQL} ~* $1", "NOT (tags && $2::text[])"]


def test_admits_mirrors_the_sql():
    f = target_tag_filter({"signal_tags": [CYBER], "signal_text_terms": ["ransomware"],
                           "exclude_signal_tags": ["sport"]})
    assert f.admits([CYBER])
    assert f.admits([], "Ransomware gang leaks files")
    assert not f.admits([], "Weather")
    assert not f.admits([], None)
    assert not f.admits(["sport", CYBER], "ransomware")         # exclude wins
    assert SignalTagFilter().admits([], None)                   # inactive admits all


def test_invalid_declared_terms_are_ignored_by_the_reader_and_refused_by_the_schema():
    f = target_tag_filter({"signal_text_terms": ["ok", "Bad Term", 3]})
    assert f.text_terms == ("ok",)
    body = _yaml(TARGET)
    body["scope"]["signal_text_terms"] = ["Bad Term"]
    with pytest.raises(Exception):
        TargetDescriptor.model_validate(body, strict=False)


def test_an_undeclared_target_dumps_without_the_key():
    """Hash-neutral: every existing target body is byte-identical."""
    checked = 0
    for p in sorted(_DESCRIPTORS.glob("target_*.yaml")):
        if p == TARGET:
            continue
        try:
            t = TargetDescriptor.model_validate(_yaml(p), strict=False)
        except Exception:      # a pre-pivot legacy file (target_germany_news.yaml …)
            continue
        assert "signal_text_terms" not in t.model_dump(mode="json")["scope"], p.name
        checked += 1
    assert checked >= 40


# ---------------------------------------------------------------------------
# The descriptors
# ---------------------------------------------------------------------------


def _vocab_values(family: str) -> set[str]:
    out: set[str] = set()
    for p in _DESCRIPTORS.glob("vocabulary_*.yaml"):
        body = yaml.safe_load(p.read_text())
        if body.get("family") == family:
            out |= {str(e["value"]) for e in body.get("entries") or []}
    return out


@pytest.mark.parametrize("name", K1_SOURCES)
def test_every_k1_source_validates_and_routes_to_the_desk(name):
    body = _yaml(name)
    d = SourceDescriptor.model_validate(body, strict=False)
    cfg = _unwrap_factory_dict(body["config"])
    (JsonApiConfig if d.identity.kind == "json_api" else RSSConfig).model_validate(cfg)
    assert d.identity.state.value == "draft"
    assert d.scope.classifications == [CYBER]
    assert "cyber_desk" in d.scope.tags                  # the target's selector key
    assert d.scope.license_class is not None             # the ledger is never left unset
    assert d.scope.access_class == "public"
    fam, value = CYBER.split(":")
    assert value in _vocab_values(fam)
    if d.scope.license_class in ("open_gov_attribution", "cc_by"):
        assert d.scope.attribution                       # the licence requires credit


def test_the_desk_target_validates_and_renders_two_arms_and_no_geo():
    t = TargetDescriptor.model_validate(_yaml(TARGET), strict=False)
    assert t.identity.state.value == "draft"
    assert t.scope.geo == [] and t.scope.predicate is None
    assert t.scope.signal_tags == [CYBER]
    assert not {"g20", "watch", "supply_chain", "nuclear_watch", "region"} & set(t.scope.tags)
    assert all(not s.source_id for s in t.sources)       # a pin would AND away the text arm
    for term in t.scope.signal_text_terms:
        assert text_term_error(term) is None


def test_lifecycle_rows_name_every_k1_descriptor():
    rows = {(m.group(1), m.group(2)): m.group(3) for m in re.finditer(
        r"^\| (source|target) \| (\S+) \| (\w+) \| draft \|$",
        lifecycle_text(_DESCRIPTORS), re.M)}
    for name in K1_SOURCES:
        sid = yaml.safe_load((_DESCRIPTORS / name).read_text())["identity"]["id"]
        assert rows.get(("source", sid)) == "active", sid
    assert rows.get(("target", "theme_cyber")) == "active"  # activated with its readers


@pytest.mark.asyncio
async def test_the_kev_record_lands_as_one_titled_dated_document():
    body = _yaml("source_cisa_kev.yaml")
    cfg = JsonApiConfig(**_unwrap_factory_dict(body["config"]))
    rec = {
        "cveID": "CVE-2026-0001", "vendorProject": "Acme", "product": "Gateway",
        "vulnerabilityName": "Acme Gateway Command Injection Vulnerability",
        "dateAdded": "2026-11-12", "shortDescription": "Acme Gateway contains a command injection.",
        "requiredAction": "Apply mitigations per vendor instructions.", "dueDate": "2026-12-03",
        "knownRansomwareCampaignUse": "Known", "notes": "https://nvd.nist.gov/vuln/detail/CVE-2026-0001",
        "cwes": ["CWE-77"],
    }
    old = dict(rec, cveID="CVE-2020-0002", dateAdded="2020-01-01")
    doc = json.dumps({"title": "x", "count": 2, "vulnerabilities": [rec, old]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text=doc, request=req)))
    handler = JsonApiSourceHandler(cfg, http_client=client)
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    ctx = SourceContext(target_id="t", target_version="v", source_id="source.cisa.kev",
                        config=cfg, state_store=InMemoryStateStore(), now_fn=lambda: now,
                        scope_geo=[], scope_languages=["en"])
    sigs = [s async for s in handler.pull(ctx, since=None)]
    await handler.aclose()
    assert len(sigs) == 1                                  # the 2020 entry is outside 30 days
    s = sigs[0]
    assert s.payload["external_id"] == "CVE-2026-0001"
    assert s.payload["title"] == "CVE-2026-0001 — Acme Gateway Command Injection Vulnerability"
    assert s.payload["published_at"].startswith("2026-11-12")
    assert s.payload["raw_body"] == (
        "Acme Gateway contains a command injection. Vendor: Acme. Product: Gateway. "
        "Added to the CISA catalogue of known exploited vulnerabilities: 2026-11-12. "
        "Remediation due for U.S. federal agencies: 2026-12-03. Known use in ransomware "
        "campaigns: Known. Required action: Apply mitigations per vendor instructions."
    )
    assert s.payload["item"]["cwes"] == ["CWE-77"]          # the whole record kept
    assert s.geo == [] and s.canonical_url is None


def test_body_template_missing_fields_drop_with_their_labels_and_fall_back():
    from legba.data.sources.json_api_mapping import render_title

    tpl = _unwrap_factory_dict(_yaml("source_cisa_kev.yaml")["config"])["body_template"]
    got = render_title(tpl, {"shortDescription": "D.", "dueDate": "2026-12-01"})
    assert got == "D.. Remediation due for U.S. federal agencies: 2026-12-01"
    with pytest.raises(Exception):
        JsonApiConfig(url_template="https://x.invalid/a", body_template="no placeholder")


# ---------------------------------------------------------------------------
# Selection on synthetic signals — the Python mirror (runs anywhere)
# ---------------------------------------------------------------------------

#: name -> (tags, geo, title). The five the desk must read, and a control.
SYNTHETIC: dict[str, tuple[list[str], list[str], str]] = {
    "kev": ([CYBER], [], "CVE-2026-0001 — Acme Gateway Command Injection Vulnerability"),
    "advisory": ([CYBER], [], "Siemens RUGGEDCOM APE1808"),
    "trade": ([CYBER], ["US"], "Uber confirms drivers' licence data stolen"),
    "outage_nogeo": ([], [], "Global outage hits Outlook and Teams; no sign of a cyberattack"),
    "outage_de": ([], ["DE"], "Ransomware attack disrupts hospitals across Germany"),
    "control_de": ([], ["DE"], "Bundestag passes the budget"),
}
EXPECT_CYBER = {"kev", "advisory", "trade", "outage_nogeo", "outage_de"}
EXPECT_DE = {"outage_de", "control_de"}
EXPECT_US = {"trade"}


def _cyber_scope() -> dict[str, Any]:
    return _yaml(TARGET)["scope"]


def test_selection_mirror_routes_each_synthetic_signal_once():
    cyber = target_tag_filter(_cyber_scope())
    got_cyber = [n for n, (tags, _g, title) in SYNTHETIC.items()
                 if cyber.admits(tags, slice_text({"title": title}))]
    assert sorted(got_cyber) == sorted(EXPECT_CYBER)       # each once (a list, no dupes)
    for desk, want in (("DE", EXPECT_DE), ("US", EXPECT_US)):
        got = [n for n, (_t, geo, _ti) in SYNTHETIC.items() if desk in geo]
        assert sorted(got) == sorted(want)


# ---------------------------------------------------------------------------
# The slice statement for the desk (built SQL, no DB)
# ---------------------------------------------------------------------------


class _Capture:
    def __init__(self, bodies: dict[str, dict]) -> None:
        self.bodies = bodies
        self.sqls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *a):
        return {"body": self.bodies.get(a[0], {})} if a else None

    async def fetch(self, sql, *params):
        if "FROM signals" in sql:
            self.sqls.append((sql, params))
        return []


def _probe() -> SimpleNamespace:
    return SimpleNamespace(identity=SimpleNamespace(id="k1_probe", kind="predictor"),
                           subscription=SimpleNamespace(substrate={},
                                                        targets=SimpleNamespace(time_window="24h")))


@pytest.mark.asyncio
async def test_the_desk_statement_is_the_two_arm_include_on_the_broad_path():
    conn = _Capture({"theme_cyber": _yaml(TARGET)})
    await _read_substrate_slice(conn, descriptor=_probe(), target_filter="theme_cyber")
    sql, params = conn.sqls[0]
    assert f"(tags && $1::text[] OR {SLICE_TEXT_SQL} ~* $2)" in sql
    assert "geo &&" not in sql and "source_id = ANY" not in sql
    assert CANONICAL_ONLY_SQL in sql and "LIMIT 360" in sql
    assert params[0] == [CYBER] and params[1].startswith(r"\m(?:cyber[\s-]?attack|")


# ---------------------------------------------------------------------------
# Selection on synthetic signals — the REAL slice reader on the real driver
# ---------------------------------------------------------------------------


def _identity():
    from legba.data.registry.signing import SigningIdentity

    return SigningIdentity(signing_key=SigningKey(b"k1-cyber-desk-slice-seed-01234567"[:32]),
                           signer_did="did:legba:registry:k1-test")


async def _scrub(conn) -> None:
    await conn.execute("DELETE FROM signals WHERE source_id LIKE 'source.test.k1%'")
    await conn.execute("DELETE FROM source_descriptors WHERE descriptor_id LIKE 'source.test.k1%'")
    await conn.execute("DELETE FROM target_descriptors WHERE descriptor_id LIKE 'k1\\_%'")
    await conn.execute(
        "DELETE FROM descriptor_dead_letter "
        "WHERE attempted_payload->'identity'->>'id' LIKE 'k1\\_%' "
        "OR attempted_payload->'identity'->>'id' LIKE 'source.test.k1%'")


@pytest_asyncio.fixture
async def world(migrated_pg):
    from legba.data.postgres import PostgresStore
    from legba.data.registry.audit import AuditLogger
    from legba.data.registry.descriptor import DescriptorRegistry

    store = PostgresStore(migrated_pg)
    await store.connect()
    async with store.acquire() as conn:
        await _scrub(conn)
        existed = await conn.fetchval(
            "SELECT count(*) FROM vocabulary_entries WHERE family = 'geopolitics.topic' "
            "AND value = 'cyber'")
        if not existed:
            await conn.execute(
                "INSERT INTO vocabulary_entries (family, value) VALUES ('geopolitics.topic', 'cyber')")
    ident = _identity()
    reg = DescriptorRegistry(store, signing_identity=ident, audit_logger=AuditLogger(identity=ident))
    await reg.start()                      # the vocabulary cache now holds geopolitics.topic
    # A source registered from the real KEV descriptor proves the classification binds.
    body = _yaml("source_cisa_kev.yaml")
    sid = f"source.test.k1_{uuid4().hex[:10]}"
    body["identity"].update({"id": sid, "state": "draft"})
    await reg.register(SourceDescriptor.model_validate(body, strict=False), "test")
    ids: dict[str, str] = {}
    now = datetime.now(tz=timezone.utc)
    async with store.acquire() as conn:
        for name, (tags, geo, title) in SYNTHETIC.items():
            u = uuid4()
            await conn.execute(
                "INSERT INTO signals (id, source_id, owner_tenant, modality, payload, content_hash, "
                "fetched_at, created_at, tags, geo) "
                "VALUES ($1,$2,'default','text',$3::jsonb,$4,$5,$5,$6::text[],$7::text[])",
                u, sid, json.dumps({"title": title}), f"h_{uuid4().hex}", now, tags, geo)
            ids[name] = str(u)
    yield SimpleNamespace(store=store, reg=reg, ids=ids, created_vocab=not existed)
    await reg.stop()
    async with store.acquire() as conn:
        await _scrub(conn)
        if not existed:
            await conn.execute(
                "DELETE FROM vocabulary_entries WHERE family = 'geopolitics.topic' AND value = 'cyber'")
    await store.close()


async def _register_target(w, body: dict[str, Any]) -> str:
    tid = f"k1_{uuid4().hex[:10]}"
    body["identity"].update({"id": tid, "state": "draft"})
    await w.reg.register(TargetDescriptor.model_validate(body, strict=False), "test")
    return tid


async def _read(w, tid: str) -> list[str]:
    inv = {v: k for k, v in w.ids.items()}
    async with w.store.acquire() as conn:
        rows = await _read_substrate_slice(conn, descriptor=_probe(), target_filter=tid)
    return [inv[str(r["id"])] for r in rows if r.get("id") is not None and str(r["id"]) in inv]


@pytest.mark.integration
async def test_synthetic_signals_reach_the_cyber_desk_and_their_country_desk_once_each(world):
    cyber = await _register_target(world, _yaml(TARGET))
    de_body = _yaml("target_country_g20_de.yaml")
    de = await _register_target(world, de_body)
    got_cyber = await _read(world, cyber)
    got_de = await _read(world, de)
    assert sorted(got_cyber) == sorted(EXPECT_CYBER)
    assert sorted(got_de) == sorted(EXPECT_DE)
    # The country-tagged outage is ONE signal in both desks, not two copies.
    assert world.ids["outage_de"] in {world.ids[n] for n in got_cyber}
    assert len(got_cyber) == len(set(got_cyber)) and len(got_de) == len(set(got_de))
