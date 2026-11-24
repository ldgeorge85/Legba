# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B6 (W11-F13) — THE HOMONYM CLASS: a desk never reads another subject's code
as its own.

The North Korea desk (``country_watch_kp``, geo ``KP``) wrote findings about
Pakistan's Khyber Pakhtunkhwa ("KP" in Pakistani copy) and the judge, the
grader and the composition passed them. This file proves, on the exemplar's
gazetteer:

  * the RULE (``subject_homonyms.HomonymRule.homonym_only``): a homonym form
    and, with the forms cut out, no subject; positive evidence required; case
    rules; the cut-first semantics ("Niger State");
  * the SLICE: the clause the reader appends and its Python mirror agree, a
    target with no ruled code is byte-identical, and on the REAL driver a
    Khyber Pakhtunkhwa signal never enters the KP slice while a North Korea one
    does — the regex text means the same thing in Postgres and Python;
  * the CRITIC: a KP-desk finding naming the province is flagged
    ``off_subject`` (hard) and published at gate score 0.0; a North Korea
    finding is untouched; a profile without the section is not applicable BY
    NAME;
  * the GRADER: an off-subject span is excluded under ``off_subject:kp`` and
    counted, never graded;
  * the gazetteer section loads, is closed, and the export is current.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from nacl.signing import SigningKey
from pydantic import ValidationError

from legba.data import gazetteer as G
from legba.data import subject_homonyms as H
from legba.data.analysts.deterministic_handlers import _correctness_segment as SEG
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.profile import load_profile
from legba.data.provenance.models import FindingPayload
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    FaithfulnessReport,
    _is_judgeable_claim,
    _segment_claims,
)
from legba.data.signal_slice_scope import (
    HOMONYM_TEXT_SQL,
    NO_HOMONYM_FILTER,
    homonym_slice_filter,
)
from legba.runtime.actor_critic import (
    OFF_SUBJECT_CEILING,
    _flag_off_subject_claims,
    verify_inline_target_finding,
)
from legba.runtime.actor_substrate_slice import _read_substrate_slice

_ROOT = Path(__file__).resolve().parents[2]
GEO = _ROOT / "profiles" / "geopolitics"
SANCTIONS = _ROOT / "profiles" / "sanctions_export_control"

KHYBER_TITLES = [
    "7 cops injured as police checkpost comes under attack in KP's Kohat",
    "PM Shehbaz says terrorism in KP ‘must be stopped at any cost’",
    "Khyber Pakhtunkhwa: Terrorist attacks rise but security crackdown holds",
    "KPK police law change raises internal escalation risk",
]
KOREA_TITLES = [
    "North Korea fires ballistic missile into the East Sea",
    "Pyongyang warns US over Taiwan as Kim pledges support for Russia",
    "DPRK state media: KCNA reports party plenum",
    "북한, 동해상으로 탄도미사일 발사",  # names neither: never excluded
    "",
]


@pytest.fixture
def kp() -> H.HomonymRule:
    rule = H.rule_for_code("kp")
    assert rule is not None, "the exemplar gazetteer declares the KP homonyms"
    return rule


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", KHYBER_TITLES)
def test_a_khyber_title_is_homonym_only_on_the_kp_desk(kp, title):
    assert kp.homonym_only(title)


@pytest.mark.parametrize("title", KOREA_TITLES)
def test_a_korea_title_is_never_homonym_only(kp, title):
    assert not kp.homonym_only(title)


def test_naming_the_subject_too_keeps_the_text(kp):
    assert not kp.homonym_only("Pakistan's KP and North Korea both named")


def test_cut_first_so_a_name_inside_a_homonym_never_rescues():
    ne = H.rule_for_code("ne")
    assert ne.homonym_only("Uproar in Niger state, 37 die in custody")
    assert ne.homonym_only("Niger Delta militants attack a pipeline")
    assert not ne.homonym_only("Niger junta in Niamey; Niger Delta oil")
    assert not ne.homonym_only("Niger's junta extends the transition")


def test_an_uppercase_form_is_case_sensitive():
    sa = H.rule_for_code("sa")
    assert sa.homonym_only("Data breach reported at SA private hospital")
    assert sa.homonym_only("Petrobras S.A. posts record profit")
    assert not sa.homonym_only("a sa b")  # "sa" is not "SA"
    assert not sa.homonym_only("Saudi Arabia and SA talks")


def test_the_letter_boundary_is_the_geocoders():
    assert H.rule_for_code("gb").homonym_only("Micron unveils 512GB DDR5 modules")
    assert not H.rule_for_code("kp").homonym_only("KPMG audit flags risks")


def test_desk_code_reads_the_profiles_id_convention():
    assert H.desk_code("country_watch_kp") == "kp"
    assert H.desk_code("country_g20_sa") == "sa"
    assert H.desk_code("lane_hormuz") is None and H.desk_code(None) is None
    assert H.rule_for_target("country_g20_us") is None  # no collision observed, no rule


def test_off_subject_claims_keeps_order_and_only_the_homonym_claims(kp):
    claims = ["KP police modernise", "North Korea tests", "KP minister abducted."]
    assert H.off_subject_claims(claims, kp) == ["KP police modernise", "KP minister abducted."]


def test_subject_homonyms_is_a_leaf():
    tree = ast.parse((_ROOT / "src/legba/data/subject_homonyms.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert (node.level, node.module) in {
                (0, "__future__"), (0, "dataclasses"), (0, "typing"), (1, "gazetteer"),
            }, node.module
        elif isinstance(node, ast.Import):
            assert [a.name for a in node.names] in (["re"], ["threading"])


# ---------------------------------------------------------------------------
# The gazetteer section
# ---------------------------------------------------------------------------


def test_the_section_is_declared_and_the_guard_applicable_on_the_exemplar():
    gz = G.gazetteer_of(load_profile(GEO))
    assert "subject_homonyms" in gz.declared
    assert G.applicable("off_subject", gz, record=False) is None
    entry = gz.get("subject_homonyms")["kp"]
    assert "Khyber Pakhtunkhwa" in entry.exclude and entry.reads_as


def test_a_profile_without_the_section_has_no_rules_and_the_guard_is_not_applicable():
    gz = G.gazetteer_of(load_profile(SANCTIONS))
    na = G.applicable("off_subject", gz, record=False)
    assert isinstance(na, G.NotApplicable) and "subject_homonyms" in na.missing
    assert H.rules(gz) == {}


@pytest.mark.parametrize("mutate, err", [
    (lambda d: d["subject_homonyms"]["kp"].update(exclude=[]), "exclude"),
    (lambda d: d["subject_homonyms"]["kp"].update(reads_as=[]), "reads_as"),
    (lambda d: d["subject_homonyms"]["kp"].update(stray=["x"]), "stray"),
    (lambda d: d["subject_homonyms"].update(KP={"reads_as": ["x"], "exclude": ["KP"]}), "pattern"),
])
def test_the_section_model_is_closed(tmp_path, mutate, err):
    data = yaml.safe_load((GEO / "rosters" / "gazetteer.yaml").read_text(encoding="utf-8"))
    mutate(data)
    p = tmp_path / "gazetteer.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValidationError, match=err):
        G.load_gazetteer_file(p)


# ---------------------------------------------------------------------------
# The slice clause (no DB)
# ---------------------------------------------------------------------------


def test_a_target_with_no_ruled_code_appends_nothing():
    for geo in (["US"], ["BR", "AR"], [], None):
        f = homonym_slice_filter(geo)
        assert f is NO_HOMONYM_FILTER and f.sql(params := []) == [] and params == []


def test_the_clause_numbers_after_existing_params_and_keeps_plain_codes():
    params: list[Any] = ["prior"]
    f = homonym_slice_filter(["CD", "MN"])
    assert f.plain == ("CD",) and [r.code for r in f.rules] == ["mn"]
    (clause,) = f.sql(params)
    assert clause.startswith("(geo && $2::text[] OR (geo && $3::text[] AND NOT (")
    assert params[1] == ["CD"] and params[2] == ["MN"] and HOMONYM_TEXT_SQL in clause


def test_admits_mirrors_the_rule_per_code():
    f = homonym_slice_filter(["KP"])
    assert not f.admits(["KP"], KHYBER_TITLES[0])
    assert f.admits(["KP"], KOREA_TITLES[0])
    multi = homonym_slice_filter(["CD", "MN"])
    assert multi.admits(["CD"], "Freeze Warning by NWS Duluth MN")  # a plain code admits
    assert not multi.admits(["MN"], "Freeze Warning by NWS Duluth MN")


# ---------------------------------------------------------------------------
# The REAL driver: the regex text in Postgres, and the slice reader
# ---------------------------------------------------------------------------

_SOURCE_PREFIX = "source.test.b6hom"
_TARGET_PREFIX = "b6hom_"


@pytest.mark.integration
async def test_the_patterns_mean_the_same_in_postgres_and_python(migrated_pg: PostgresConfig):
    import asyncpg

    conn = await asyncpg.connect(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
    )
    try:
        texts = KHYBER_TITLES + KOREA_TITLES + [
            "Uproar in Niger state", "Petrobras S.A. profit", "Georgia, US, shooting",
            "Freeze Warning by NWS Duluth MN", "512GB DDR5", "l’UA appelle",
        ]
        for rule in H.rules().values():
            params: list[Any] = [None, None]  # $1 payload, $2 geo — the row
            (clause,) = homonym_slice_filter([rule.geo_code]).sql(params)
            sql = f"SELECT {clause} FROM (SELECT $1::jsonb AS payload, $2::text[] AS geo) s"
            for text in texts:
                params[0], params[1] = json.dumps({"title": text}), [rule.geo_code]
                admitted = await conn.fetchval(sql, *params)
                assert admitted is (not rule.homonym_only(text)), (rule.code, text)
    finally:
        await conn.close()


def _identity():
    from legba.data.registry.signing import SigningIdentity

    return SigningIdentity(signing_key=SigningKey(b"b6-homonym-slice-test-seed-01234"[:32]),
                           signer_did="did:legba:registry:b6-test")


async def _scrub(conn) -> None:
    await conn.execute(f"DELETE FROM signals WHERE source_id LIKE '{_SOURCE_PREFIX}%'")
    await conn.execute(f"DELETE FROM source_descriptors WHERE descriptor_id LIKE '{_SOURCE_PREFIX}%'")
    await conn.execute(f"DELETE FROM target_descriptors WHERE descriptor_id LIKE '{_TARGET_PREFIX}%'")


async def _signal(conn, source_id: str, title: str, geo: list[str]) -> str:
    sid = uuid4()
    now = datetime.now(tz=timezone.utc)
    await conn.execute(
        "INSERT INTO signals (id, source_id, owner_tenant, modality, payload, content_hash, "
        "fetched_at, created_at, geo) VALUES ($1,$2,'default','text',$3::jsonb,$4,$5,$5,$6::text[])",
        sid, source_id, json.dumps({"title": title}), f"h_{uuid4().hex}", now, geo,
    )
    return str(sid)


@pytest_asyncio.fixture
async def kp_world(migrated_pg: PostgresConfig):
    from legba.data.registry.audit import AuditLogger
    from legba.data.registry.descriptor import DescriptorRegistry
    from legba.data.schemas.source import SourceDescriptor
    from legba.data.schemas.target import TargetDescriptor

    store = PostgresStore(migrated_pg)
    await store.connect()
    async with store.acquire() as conn:
        await _scrub(conn)
    identity = _identity()
    reg = DescriptorRegistry(store, signing_identity=identity, audit_logger=AuditLogger(identity=identity))
    await reg.start()
    body = yaml.safe_load((_ROOT / "descriptors" / "source_38north.yaml").read_text())
    src = f"{_SOURCE_PREFIX}_{uuid4().hex[:10]}"
    body["identity"].update({"id": src, "version": "0" * 16, "state": "draft"})
    await reg.register(SourceDescriptor.model_validate(body, strict=False), "test")
    tid = f"{_TARGET_PREFIX}{uuid4().hex[:10]}"
    await reg.register(TargetDescriptor.model_validate({
        "identity": {
            "id": tid, "name": tid, "schema_uri": "legba/target/2.0.0", "version": "0" * 16,
            "abstraction_level": "L1", "state": "draft", "owner": "test",
            "created": "2026-10-08T00:00:00Z",
        },
        "scope": {"domain": "geo", "geo": ["KP"]},
        "sources": [{"source_id": src}],
    }, strict=False), "test")
    async with store.acquire() as conn:
        head = await conn.fetchval(
            "SELECT body->'scope'->'geo' FROM target_descriptors WHERE descriptor_id=$1 AND is_head",
            tid,
        )
        assert (json.loads(head) if isinstance(head, str) else head) == ["KP"]  # read back
        ids = {
            "khyber": await _signal(conn, src, KHYBER_TITLES[0], ["KP"]),
            "khyber_name": await _signal(conn, src, KHYBER_TITLES[2], ["KP"]),
            "korea": await _signal(conn, src, KOREA_TITLES[0], ["KP"]),
            "korean_lang": await _signal(conn, src, KOREA_TITLES[3], ["KP"]),
            "both": await _signal(conn, src, "KP police and North Korea", ["KP"]),
        }
    yield SimpleNamespace(store=store, tid=tid, ids=ids)
    await reg.stop()
    async with store.acquire() as conn:
        await _scrub(conn)
    await store.close()


@pytest.mark.integration
async def test_a_khyber_signal_never_enters_the_kp_slice(kp_world):
    probe = SimpleNamespace(
        identity=SimpleNamespace(id="b6hom_probe", kind="predictor"),
        subscription=SimpleNamespace(substrate={}, targets=SimpleNamespace(time_window="24h")),
    )
    async with kp_world.store.acquire() as conn:
        rows = await _read_substrate_slice(conn, descriptor=probe, target_filter=kp_world.tid)
    inv = {v: k for k, v in kp_world.ids.items()}
    got = {inv[str(r["id"])] for r in rows if r.get("id") is not None and str(r["id"]) in inv}
    assert got == {"korea", "korean_lang", "both"}


# ---------------------------------------------------------------------------
# The critic
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        return "INSERT 0 1"

    async def fetchval(self, *a, **k):
        return None


def _critique_data(conn: _FakeConn) -> dict:
    for sql, args in conn.executes:
        if "INSERT INTO analyst_outputs" in sql:
            for a in args:
                if isinstance(a, str) and a.lstrip().startswith("{"):
                    try:
                        parsed = json.loads(a)
                    except json.JSONDecodeError:
                        continue
                    if "overall_score" in parsed:
                        return parsed
    raise AssertionError("no critique INSERT recorded")


def _deps():
    descriptor = SimpleNamespace(
        identity=SimpleNamespace(kind="inline_target", id="internal_stability", version="1"),
        method=SimpleNamespace(llm={"primary": {"raw": "llm.primary.openai_compat"}}),
    )
    return SimpleNamespace(descriptor=descriptor, verify_judge=None)


async def _verify(title: str, body: str, target_id: str = "country_watch_kp") -> dict:
    conn = _FakeConn()
    sid = str(uuid4())
    finding = FindingPayload(
        title=title, body=body, confidence=0.9,
        data={"citations": [{"marker": "[1]", "signal_id": sid}]},
    )
    result = await verify_inline_target_finding(
        conn, deps=_deps(), finding_id=uuid4(), finding_payload=finding, run_id=uuid4(),
        target_id=target_id,
    )
    assert result is not None
    return _critique_data(conn)


async def test_a_kp_desk_claim_naming_the_province_is_flagged_off_subject():
    data = await _verify(
        "Khyber Pakhtunkhwa: Terrorist attacks rise but security crackdown holds",
        "- Terrorist attacks in KP rose this week [1].\n",
    )
    verification = data["data"]["verification"]
    spans = [s for s in verification["unsupported_spans"] if s["reason"] == "off_subject"]
    assert len(spans) == 2 and all(s["fail_class"] == FAIL_CLASS_HARD for s in spans)
    assert verification["counters"]["off_subject_claims"] == 2
    assert any(v["reason"] == "off_subject" for v in verification["claim_verdicts"])
    # capped through the T7 ceiling idiom: effective confidence min(0.9, 0.0)
    assert verification["confidence_ceiling"] == OFF_SUBJECT_CEILING == 0.0
    assert data["overall_score"] == 0.0


async def test_a_north_korea_finding_is_untouched():
    data = await _verify(
        "North Korea fires ballistic missile", "- Pyongyang launched a missile [1].\n",
    )
    verification = data["data"]["verification"]
    assert "off_subject_claims" not in verification["counters"]
    assert not any(s["reason"] == "off_subject" for s in verification["unsupported_spans"])
    assert data["overall_score"] > 0.0


async def test_a_desk_with_no_rule_is_untouched():
    data = await _verify("KP police law change", "- KP police reform [1].\n",
                         target_id="country_g20_us")
    assert "off_subject_claims" not in data["data"]["verification"]["counters"]


def test_no_profile_section_is_not_applicable_by_name(monkeypatch):
    gz = G.gazetteer_of(load_profile(SANCTIONS))
    monkeypatch.setattr(G, "_current", gz)
    report = FaithfulnessReport(faithfulness_score=1.0, checkable_claims=0, supported_claims=0)
    n = _flag_off_subject_claims(
        report, title="Khyber Pakhtunkhwa", body="", target_id="country_watch_kp",
    )
    assert n == 0 and report.counters == {"gazetteer_not_applicable_off_subject": 1}
    assert report.confidence_ceiling is None and report.unsupported_spans == []


# ---------------------------------------------------------------------------
# The correctness grader
# ---------------------------------------------------------------------------


def test_the_grader_excludes_an_off_subject_span_by_name_and_counts_it(kp):
    head = {
        "grain": SEG.GRAIN_DESK, "analyst_id": "internal_stability",
        "output_id": str(uuid4()), "created_at": "2026-10-08T00:00:00Z",
        "grader_body": (
            "Terrorist attacks in Khyber Pakhtunkhwa rose sharply over the past week.\n"
            "North Korea's leadership showed no visible change in the window reported."
        ),
    }
    out = SEG.segment_all([head], _segment_claims, _is_judgeable_claim, frozenset(), kp)
    assert out["totals"]["excluded_by_reason"] == {"off_subject:kp": 1}
    assert [a["text"] for a in out["claims"]] == [
        "North Korea's leadership showed no visible change in the window reported."
    ]
    # no rule = byte-identical: the province claim is graded as before
    plain = SEG.segment_all([head], _segment_claims, _is_judgeable_claim, frozenset())
    assert plain["totals"]["n_kept"] == 2 and plain["totals"]["excluded_by_reason"] == {}
