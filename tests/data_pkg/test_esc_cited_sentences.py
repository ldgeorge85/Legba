# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X4 / ESC (W11-F52) - the escalation and military_posture units cite what they claim.

THE DEFECT (7 d to 2026-10-13, read-only on the live substrate): 50.8 % of
escalation's and 57.1 % of military_posture's fact-asserting sentences carried no
citation marker, and the BLUF was uncited in 67.9 % / 82.9 % of reads. The LLM
judge grades those sentences as synthesis and forgives most of them; the
deterministic floor counts every one as ``no_citation``, so a floor-only read
(judge down) scored 0.45-0.56 on bodies the judge scores 0.74-0.80.

What is pinned, and why each pin is the one that matters:

  1. THE RULE REACHES EXACTLY THE NINE UNITS. ``UNIT_CITED_SENTENCE_RULE`` is
     pasted verbatim into ``escalation`` and ``military_posture`` (X4) and, since
     W11-F86 (2026-10-24), into the other seven bounded units - and into no other
     analyst descriptor (the nine-desk ``UNIT_READ_CONTRACT`` is a different
     change). Every one still parses through the runtime schema, keeps the state
     its file ships (the walker registers a new head at the roll; nothing here
     activates), and keeps the toolset/cadence it had.
  2. THE FIXTURE THE RULE DESCRIBES IS A CITED BLUF. A body written to the rule
     (cited BLUF, a marker on every sentence of the two read sections, a house-
     form scoped negative with none) carries ZERO ``no_citation`` spans on the
     floor and reads ``bluf_marker == "cited"``. The body the live units wrote
     (uncited BLUF, unsourced analysis) is the control: the floor writes one
     ``no_citation`` span per uncited fact sentence, BLUF included.
  3. THE INSTRUMENT IS THE FLOOR'S OWN COUNT. ``uncited_fact_sentences`` equals the
     number of ``no_citation`` spans ``_deterministic_floor`` writes for the same
     body - so a share read off the route IS the floor's loss, on either path.
  4. THE CRITIC STAMPS IT, ON THE REAL DRIVER. ``verify_inline_target_finding``
     on a real connection persists ``fact_sentences`` / ``uncited_fact_sentences``
     / ``bluf_marker`` on the critique row, read back from the database; a
     non-unit kind is not stamped.
  5. THE ROUTE REPORTS IT. ``GET /v3/system/judge-stats`` answers ``uncited`` per
     analyzed unit per day with its counts beside every share; a legacy critique
     with no stamp is not counted (a missing stamp is not a zero); a share over
     zero fact sentences is ``null``, never ``0.0``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.analysts._tradecraft import UNIT_CITED_SENTENCE_RULE
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.provenance.models import FindingPayload
from legba.data.provenance.uncited_share import (
    BLUF_CITED,
    BLUF_NONE,
    BLUF_UNCITED,
    uncited_share_fields,
)
from legba.data.provenance.verify import _deterministic_floor
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import MASTER_KEY_ENV, CredentialVault
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.judge_stats_api import build_judge_stats_router, build_uncited
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.schemas.analyst import AnalystDescriptor
from legba.runtime.actor_critic import verify_inline_target_finding

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "66" * 32)

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTORS = REPO_ROOT / "descriptors"

#: X4 put the rule on the first two; W11-F86 (2026-10-24) on the other seven
#: bounded units once the 10-15 after-read on the two held.
X4_UNITS = ("escalation", "military_posture")
F86_UNITS = (
    "energy_security",
    "economic_coercion",
    "internal_stability",
    "proliferation_watch",
    "leadership_transition",
    "narrative_coordination",
    "disruption_status",
)
#: K-2 (2026-11-14) — the cyber desk's unit took the rule at birth, beside
#: the HOUSE READ CONTRACT, in the same place the nine carry it.
K2_UNITS = ("cyber_watch",)
CITED_UNITS = X4_UNITS + F86_UNITS + K2_UNITS
#: Every OTHER analyst descriptor in the tree - the compositions and their
#: voices (``country_assessment`` writes ``[[ref:N]]`` over desk blocks and has
#: no '## What changed' section, so the unit rule's text would mis-instruct it),
#: the journal, consult, the deterministic units. None may carry the rule.
OTHER_ANALYSTS = tuple(
    sorted(
        p.stem.removeprefix("analyst_")
        for p in DESCRIPTORS.glob("analyst_*.yaml")
        if p.stem.removeprefix("analyst_") not in CITED_UNITS
    )
)
#: The state each file SHIPS (the walker lifts disruption_status to the host's
#: ``active`` from descriptors/LIFECYCLE.md); the rule moves no state.
_SHIPPED_STATE = {u: "active" for u in CITED_UNITS} | {
    "disruption_status": "draft", "cyber_watch": "draft"}


def _norm(text: str) -> str:
    """Whitespace-normalized text - the rule is a Python constant pasted into a
    YAML block scalar, so the WRAPPING differs by construction."""
    return " ".join(text.split())


def _doc(unit: str) -> dict:
    return yaml.safe_load((DESCRIPTORS / f"analyst_{unit}.yaml").read_text())


# ---------------------------------------------------------------------------
# 1. The rule reaches exactly the nine units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", X4_UNITS)
def test_the_two_prompts_carry_the_rule_verbatim_and_once(unit: str) -> None:
    prompt = _norm(_doc(unit)["method"]["system_prompt"])
    rule = _norm(UNIT_CITED_SENTENCE_RULE)
    assert prompt.count(rule) == 1
    # It sits beside the citation instruction, ahead of the JSON schema block.
    assert prompt.index(rule) < prompt.index("Respond with STRICT JSON ONLY")
    assert prompt.index("PRODUCE EXACTLY ONE FINDING") < prompt.index(rule)


@pytest.mark.parametrize("unit", CITED_UNITS)
def test_each_prompt_carries_it_once_as_its_own_paragraph_ahead_of_the_schema(
    unit: str,
) -> None:
    """W11-F86 - the same paragraph at the same place on all nine: once, whole
    (its own blank-line-separated paragraph, which is what lets the voice4 peel
    account for it), immediately ahead of the unit's response-schema paragraph
    and before the HOUSE READ CONTRACT. The seven prompts word their schema
    paragraph three ways ('Respond with STRICT JSON ONLY', 'OUTPUT: respond
    with STRICT JSON', 'Produce EXACTLY ONE finding. Respond with STRICT JSON');
    each carries 'STRICT JSON'."""
    raw = _doc(unit)["method"]["system_prompt"]
    rule = _norm(UNIT_CITED_SENTENCE_RULE)
    paragraphs = [_norm(p) for p in raw.split("\n\n")]
    assert paragraphs.count(rule) == 1
    at = paragraphs.index(rule)
    assert "STRICT JSON" in paragraphs[at + 1], unit
    contract = next(i for i, p in enumerate(paragraphs) if p.startswith("HOUSE READ CONTRACT"))
    assert at < contract, unit
    prompt = _norm(raw)
    assert prompt.count(rule) == 1
    # The sections the rule names are sections this unit's body shape has.
    for section in ("## What changed", "## Why it matters", "## What would change this read"):
        assert section in prompt, f"{unit}: the rule names {section!r}"


def test_the_rule_reaches_exactly_the_nine_bounded_units() -> None:
    assert len(set(CITED_UNITS)) == len(CITED_UNITS) == 10
    for unit in CITED_UNITS:
        assert _doc(unit)["identity"]["kind"] == "inline_target", unit
    assert "country_assessment" in OTHER_ANALYSTS
    n_analysts = len(list(DESCRIPTORS.glob("analyst_*.yaml")))
    assert len(OTHER_ANALYSTS) + len(CITED_UNITS) == n_analysts


@pytest.mark.parametrize("unit", OTHER_ANALYSTS)
def test_no_other_analyst_carries_it(unit: str) -> None:
    method = _doc(unit).get("method") or {}
    prompt = _norm(str(method.get("system_prompt") or ""))
    assert "EVERY SENTENCE THAT STATES A FACT CARRIES ITS MARKER" not in prompt


def test_the_rule_names_what_the_floor_counts() -> None:
    """The text is worded to the line the floor draws: the BLUF is a cited
    sentence, the two read sections carry a marker on every sentence, the
    scoped negative keeps the house 'no <X> appears ...' head, and the markers
    are ASCII."""
    rule = _norm(UNIT_CITED_SENTENCE_RULE)
    for needle in (
        "THE BLUF IS A CITED SENTENCE",
        "'## What changed' AND IN '## Why it matters'",
        "no <X> appears in this desk's collection through <date>",
        "ASCII brackets",
    ):
        assert needle in rule


@pytest.mark.parametrize("unit", CITED_UNITS)
def test_the_descriptor_still_parses_through_the_runtime_schema(unit: str) -> None:
    """A descriptor PUT that does not parse is a dead analyst (09-28)."""
    doc = _doc(unit)
    model = AnalystDescriptor.model_validate(doc)
    assert model.identity.id == unit
    assert model.identity.state.value == _SHIPPED_STATE[unit]  # a new head, not a new state
    assert model.method.options["judge_sample_rate"] == 1.0
    assert model.method.llm["temperature"] == 1.0
    assert doc["method"]["budget_tokens_per_day"] == 0
    assert "max_tokens" not in {k for k in doc["method"] if k != "llm"}


# ---------------------------------------------------------------------------
# 2./3. The fixture the rule describes - and the control it replaces
# ---------------------------------------------------------------------------

_CITATIONS = [
    {"marker": f"[{n}]", "signal_id": str(uuid4()), "title": f"signal {n}",
     "source_text": f"source text {n}"}
    for n in (11, 47, 57)
]

#: Written to the rule: cited BLUF, a marker on every sentence of the read
#: sections, a house-form scoped negative with none, forward-looking sections
#: with none.
CITED_BODY = """*As of 2026-10-13; slice covers the trailing 72h to that date; 120 signals.*

**BLUF:** Russian missile and drone strikes on Ukrainian cities have intensified, keeping escalation risk high [11][47].

## What changed
- Kyiv was hit by a missile barrage on 13 October [11].
- Kharkiv region suffered multiple drone strikes the same day [57].

## Why it matters
Confirmed attacks on Ukrainian population centers are materiel escalation, not rhetoric [11][47].
No reservist call-up appears in this desk's collection through 13 October.

## What would change this read
A verified cease-fire announcement would lower the risk; this desk collects diplomatic reporting.

## Indicators
- Reservist call-up orders
- Airspace closure over the theater
"""

#: The shape the live units wrote (10-09 / 10-12): an uncited BLUF that names
#: actors, and analysis sentences that name places and actors with no marker.
UNCITED_BODY = """*As of 2026-10-13; slice covers the trailing 72h to that date; 120 signals.*

**BLUF:** Russian missile and drone strikes on Ukrainian cities have intensified, keeping escalation risk high.

## What changed
- Kyiv was hit by a missile barrage on 13 October [11].
- Kharkiv region suffered multiple drone strikes the same day.

## Why it matters
Confirmed attacks on Ukrainian population centers are materiel escalation, not rhetoric.
No reservist call-up appears in this desk's collection through 13 October.

## What would change this read
A verified cease-fire announcement would lower the risk; this desk collects diplomatic reporting.

## Indicators
- Reservist call-up orders
"""


def _reasons(body: str) -> list[str]:
    report = _deterministic_floor(body, _CITATIONS)
    return [s.reason for s in report.unsupported_spans]


def test_a_body_written_to_the_rule_has_a_cited_bluf_and_no_uncited_fact() -> None:
    f = uncited_share_fields(CITED_BODY)
    assert f == {"fact_sentences": 4, "uncited_fact_sentences": 0, "bluf_marker": BLUF_CITED}
    report = _deterministic_floor(CITED_BODY, _CITATIONS)
    assert "no_citation" not in _reasons(CITED_BODY)
    assert report.faithfulness_score == pytest.approx(1.0)


def test_the_body_the_live_units_wrote_is_the_control() -> None:
    f = uncited_share_fields(UNCITED_BODY)
    assert f["bluf_marker"] == BLUF_UNCITED
    assert (f["fact_sentences"], f["uncited_fact_sentences"]) == (4, 3)
    # The BLUF is one of the floor's no_citation spans - the cost the rule removes.
    spans = _deterministic_floor(UNCITED_BODY, _CITATIONS).unsupported_spans
    assert any(s.text.lstrip().startswith("**BLUF:**") and s.reason == "no_citation"
               for s in spans)
    assert _deterministic_floor(UNCITED_BODY, _CITATIONS).faithfulness_score == pytest.approx(0.25)


@pytest.mark.parametrize("body", [CITED_BODY, UNCITED_BODY])
def test_the_instrument_equals_the_floors_own_no_citation_count(body: str) -> None:
    f = uncited_share_fields(body)
    assert f["uncited_fact_sentences"] == _reasons(body).count("no_citation")
    assert f["fact_sentences"] == _deterministic_floor(body, _CITATIONS).checkable_claims


def test_the_instrument_is_total() -> None:
    assert uncited_share_fields("") == {
        "fact_sentences": 0, "uncited_fact_sentences": 0, "bluf_marker": BLUF_NONE,
    }
    assert uncited_share_fields("## Indicators\n- nothing\n")["bluf_marker"] == BLUF_NONE
    # A composition-style [[ref:N]] marker counts as a marker.
    f = uncited_share_fields("**BLUF:** Tehran resumed enrichment [[ref:2]].")
    assert (f["bluf_marker"], f["uncited_fact_sentences"]) == (BLUF_CITED, 0)
    # A range marker counts too (the floor's own M14 reading).
    assert uncited_share_fields("**BLUF:** Tehran resumed enrichment [1-9].")["bluf_marker"] == BLUF_CITED
    # An exempt BLUF is still reported uncited (no marker) but is not a fact miss.
    f = uncited_share_fields("**BLUF:** Escalation risk remains elevated.")
    assert (f["bluf_marker"], f["fact_sentences"], f["uncited_fact_sentences"]) == (
        BLUF_UNCITED, 0, 0,
    )


# ---------------------------------------------------------------------------
# 4. The critic stamps it - REAL DRIVER
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    yield store
    await store.close()


def _deps(kind: str, analyst_id: str):
    descriptor = SimpleNamespace(
        identity=SimpleNamespace(kind=kind, id=analyst_id, version="1.0.0"),
        method=SimpleNamespace(llm={"primary": {"raw": "llm.primary.openai_compat"}}),
    )
    return SimpleNamespace(descriptor=descriptor, verify_judge=None)


async def _verification(pg: PostgresStore, fid) -> dict:
    async with pg.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data FROM analyst_outputs WHERE kind = 'critique' "
            "AND data->>'analyzed_output_id' = $1", str(fid),
        )
    assert row is not None, "the critique row was not persisted"
    data = row["data"]
    data = json.loads(data) if isinstance(data, str) else dict(data)
    return data["data"]["verification"]


@pytest_asyncio.fixture
async def critic_rows(pg):
    fids: list = []
    yield fids
    async with pg.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE kind = 'critique' "
            "AND data->>'analyzed_output_id' = ANY($1::text[])",
            [str(f) for f in fids],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,expected",
    [
        (CITED_BODY, (4, 0, BLUF_CITED)),
        (UNCITED_BODY, (4, 3, BLUF_UNCITED)),
    ],
)
async def test_the_critic_persists_the_share_fields_on_the_real_driver(
    pg, critic_rows, monkeypatch, body, expected,
):
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    fid = uuid4()
    critic_rows.append(fid)
    finding = FindingPayload(
        title="Ukraine", body=body, confidence=0.8, data={"citations": _CITATIONS},
    )
    async with pg.pool.acquire() as conn:
        result = await verify_inline_target_finding(
            conn, deps=_deps("inline_target", "escalation"), finding_id=fid,
            finding_payload=finding, run_id=uuid4(),
        )
    assert result is not None and result["judge_status"] == "deterministic"
    v = await _verification(pg, fid)
    assert (v["fact_sentences"], v["uncited_fact_sentences"], v["bluf_marker"]) == expected
    # The persisted share IS the floor's no_citation count on the same row.
    n_nocite = sum(1 for s in v["unsupported_spans"] if s["reason"] == "no_citation")
    assert v["uncited_fact_sentences"] == n_nocite


@pytest.mark.asyncio
async def test_a_composition_critique_is_not_stamped(pg, critic_rows, monkeypatch):
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    fid = uuid4()
    critic_rows.append(fid)
    finding = FindingPayload(
        title="World", body="**BLUF:** Tehran resumed enrichment [[ref:1]].\n",
        confidence=0.8,
        data={"citations": [{"marker": "[[ref:1]]", "ref_kind": "finding",
                              "finding_id": str(uuid4()), "source_text": "x"}]},
    )
    async with pg.pool.acquire() as conn:
        await verify_inline_target_finding(
            conn, deps=_deps("meta_findings_synthesizer", "country_composition"),
            finding_id=fid, finding_payload=finding, run_id=uuid4(),
        )
    v = await _verification(pg, fid)
    assert "fact_sentences" not in v and "bluf_marker" not in v


# ---------------------------------------------------------------------------
# 5. The route reports it - REAL APP over ASGI
# ---------------------------------------------------------------------------

_STATS = "/api/v1/v3/system/judge-stats"


@pytest_asyncio.fixture
async def api(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)
    store = PostgresStore(migrated_pg)
    await store.connect()
    identity = SigningIdentity(
        signing_key=SigningKey(b"esc-uncited-route-seed-000000001"[:32]),
        signer_did="did:legba:registry:esc-uncited-test",
    )
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(store)
    vocab = VocabularyCache(store)
    vault = CredentialVault(store)
    registry = DescriptorRegistry(
        store, vocabulary_cache=vocab, signing_identity=identity,
        audit_logger=audit, dead_letter=dlq,
    )
    await registry.start()
    deps = RegistryAPIDeps(
        descriptor_registry=registry,
        stack_registry=StackRegistry(store, vault, audit=audit, dlq=dlq),
        vault=vault, dlq=dlq, audit_logger=audit, vocabulary_cache=vocab,
        nats_store=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_judge_stats_router(deps), prefix="/api/v1/v3")
    ids: list = []

    async def critique(*, analyst: str, created: datetime, verification: dict):
        oid = uuid4()
        payload = {
            "analyzed_analyst_id": analyst,
            "analyzed_output_id": str(uuid4()),
            "data": {"verification": verification},
        }
        async with store.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO analyst_outputs (id, kind, title, body, confidence, data, "
                "schema_uri, run_id, produced_at, created_at) VALUES ($1, 'critique', "
                "'Faithfulness verify (score 0.85)', '', 0.5, $2::jsonb, "
                "'iglu:legba/analyst_output/jsonschema/1-0-0', $3, $4, $4)",
                oid, json.dumps(payload), uuid4(), created,
            )
        ids.append(oid)

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    yield SimpleNamespace(client=client, critique=critique)
    await client.aclose()
    async with store.pool.acquire() as conn:
        await conn.execute("DELETE FROM analyst_outputs WHERE id = ANY($1::uuid[])", ids)
    await store.close()


def _stamp(facts: int, unc: int, bluf: str) -> dict:
    return {"judge_status": "llm", "fact_sentences": facts,
            "uncited_fact_sentences": unc, "bluf_marker": bluf}


@pytest.mark.asyncio
async def test_the_route_reports_the_share_per_unit_per_day(api):
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    unit = f"esc_probe_{uuid4().hex[:8]}"
    # Today: 3 stamped reads (one BLUF uncited, one none), one LEGACY read with no stamp.
    await api.critique(analyst=unit, created=now, verification=_stamp(10, 5, BLUF_UNCITED))
    await api.critique(analyst=unit, created=now, verification=_stamp(8, 0, BLUF_CITED))
    await api.critique(analyst=unit, created=now, verification=_stamp(0, 0, BLUF_NONE))
    await api.critique(analyst=unit, created=now, verification={"judge_status": "llm"})
    # Yesterday: one read, all cited.
    await api.critique(analyst=unit, created=now - timedelta(days=1),
                       verification=_stamp(4, 0, BLUF_CITED))

    r = await api.client.get(_STATS, params={"days": 7, "analyst_id": unit})
    assert r.status_code == 200
    body = r.json()["uncited"]
    assert body["measured"] is True
    rows = {row["day"]: row for row in body["rows"]}
    assert len(rows) == 2
    today = rows[now.date().isoformat()]
    # The unstamped legacy read is NOT counted: a missing stamp is not a zero.
    assert today["reads"] == 3
    assert (today["fact_sentences"], today["uncited_fact_sentences"]) == (18, 5)
    assert today["uncited_share"] == pytest.approx(5 / 18, abs=1e-4)
    assert (today["bluf_reads"], today["bluf_uncited_reads"]) == (2, 1)
    assert today["bluf_uncited_share"] == pytest.approx(0.5)
    yday = rows[(now - timedelta(days=1)).date().isoformat()]
    assert yday["uncited_share"] == 0.0 and yday["bluf_uncited_share"] == 0.0


@pytest.mark.asyncio
async def test_a_unit_with_no_stamped_critique_has_no_row_not_a_zero_row(api):
    r = await api.client.get(_STATS, params={"days": 7, "analyst_id": f"never_{uuid4().hex[:8]}"})
    body = r.json()["uncited"]
    assert body == {"measured": True, "rows": []}


def test_a_share_over_zero_is_absent_never_zero() -> None:
    out = build_uncited([{
        "day": datetime(2026, 10, 13).date(), "analyst_id": "escalation", "reads": 2,
        "fact_sentences": 0, "uncited_fact_sentences": 0,
        "bluf_reads": 0, "bluf_uncited_reads": 0,
    }])
    row = out.rows[0]
    assert row.uncited_share is None and row.bluf_uncited_share is None
