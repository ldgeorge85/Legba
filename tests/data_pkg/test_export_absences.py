# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The desk brief's TYPED ABSENCE section (lane k5b).

Four layers, mirroring the module's own split:

  * PURE composition — ``absences_block`` / ``render_absences_markdown`` are
    DB-free, so what the brief PRINTS is argued directly: all seven kinds
    always present, the route's three answers kept apart, the stamp rule in
    words (and a stale item saying LAST KNOWN, NOT RE-CHECKED), the proof
    rule, and the per-kind cap stating what it holds back.
  * BYTE-IDENTITY — an export that does not ask for absences must be exactly
    the document it was before this lane: no ``absences`` key on the JSON, no
    section in the markdown, and the pre-existing golden unchanged.
  * INTEGRATION — ``POST /api/v1/v3/export`` with ``appendix.absences``
    through the ASGI app, against the live substrate, proving the block is
    composed SERVER-side and that both formats carry the same answer.
  * GUARDS — the slim-image import probe, and the two-language drift pins:
    the section heading the print document splits on, the glossary sentence
    both reader surfaces carry, and the reader's kind ORDER.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.absence_api import (
    ABSENCE_KINDS,
    AbsenceItem,
    AbsenceOut,
    AbsenceProof,
)
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.export_absences import (
    ABSENCES_HEADING,
    ABSENCES_PER_KIND,
    GLOSSARY_NOTE,
    STATE_ABSENT,
    STATE_CLEAR,
    STATE_NOT_MEASURED,
    absences_block,
    render_absences_markdown,
)
from legba.data.registry.export_api import (
    build_document,
    build_export_router,
    render_markdown,
)
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "55" * 32)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = str(_REPO_ROOT / "src")
_ROUTE = "/api/v1/v3/export"

#: A desk id unique to this module — every table it touches is shared, so the
#: purge below can only ever remove rows this module wrote.
DESK = "country_watch_k5babsence"

_NOW = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# PURE — what the brief prints
# ---------------------------------------------------------------------------


def _proof(**over) -> AbsenceProof:
    base = dict(
        what_was_checked="the latest kind='finding' row for this unit",
        checked_at="2026-09-24T04:40:01+00:00",
        ref="80690b09-6c63-4c82-b826-f89c4c3f4ff7",
        ref_kind="scorecard",
    )
    base.update(over)
    return AbsenceProof(**base)


def _item(**over) -> AbsenceItem:
    base = dict(
        kind="not_collected",
        subject="disruption_status",
        since=None,
        window="no read on record for this desk",
        reason="this bounded unit has never produced a read for this desk",
        as_of="2026-09-24T04:40:01+00:00",
        as_of_basis="the banded-scorecard run's own scan of this desk's units",
        expires_at="2026-09-24T06:00:00+00:00",
        review=None,
        stale=True,
        proof=_proof(),
    )
    base.update(over)
    return AbsenceItem(**base)


def _out(**over) -> AbsenceOut:
    base = dict(
        scope=DESK,
        read_at=_NOW.isoformat(),
        absences=[],
        not_measured=[],
    )
    base.update(over)
    return AbsenceOut(**base)


def _group(block: dict, kind: str) -> dict:
    return next(g for g in block["by_kind"] if g["kind"] == kind)


def test_the_block_carries_all_seven_kinds_in_the_routes_own_order():
    """A kind the brief omits is a kind the reader cannot know was read. All
    seven are present whatever the desk looks like, with the route's own
    published meaning — never a second wording invented here."""
    block = absences_block(_out())
    assert [g["kind"] for g in block["by_kind"]] == list(ABSENCE_KINDS)
    for group in block["by_kind"]:
        assert group["meaning"] == ABSENCE_KINDS[group["kind"]]


def test_the_three_answers_are_never_collapsed():
    """Items, read-and-clear, and NOT MEASURED are three different facts. A
    brief that prints only the first reports an unread desk as a clean one."""
    block = absences_block(
        _out(
            absences=[_item()],
            not_measured=[
                "searched_found_nothing / search_failed: external_grades "
                "could not be read (UndefinedTableError)"
            ],
        )
    )
    assert _group(block, "not_collected")["state"] == STATE_ABSENT
    # ONE entry naming TWO kinds must reach both — a prefix match finds only
    # the first and leaves the second reading as "read and found nothing".
    assert _group(block, "searched_found_nothing")["state"] == STATE_NOT_MEASURED
    assert _group(block, "search_failed")["state"] == STATE_NOT_MEASURED
    assert "UndefinedTableError" in _group(block, "search_failed")["not_measured"]
    assert _group(block, "below_floor")["state"] == STATE_CLEAR
    assert _group(block, "below_floor")["not_measured"] is None


def test_a_qualified_kind_in_a_compound_entry_still_matches():
    """The route qualifies a shared kind — ``source_stale (sources)`` — and the
    brief must still class that kind as unread."""
    block = absences_block(
        _out(
            not_measured=[
                "collected_but_silent / source_stale (sources): this desk "
                "declares no scope.geo"
            ]
        )
    )
    assert _group(block, "source_stale")["state"] == STATE_NOT_MEASURED
    assert _group(block, "collected_but_silent")["state"] == STATE_NOT_MEASURED


def test_a_stale_item_prints_as_last_known_not_re_checked():
    """An old absence must never pass for a current one on a printed page."""
    md = "\n".join(render_absences_markdown(absences_block(_out(absences=[_item()]))))
    assert "last known absence, NOT re-checked" in md
    assert "was due 2026-09-24T06:00:00+00:00" in md
    # The measured instant and WHICH instant it is, both stated.
    assert "measured 2026-09-24T04:40:01+00:00" in md
    assert "the banded-scorecard run's own scan of this desk's units" in md


def test_a_current_item_prints_its_own_expiry_not_a_staleness_claim():
    item = _item(stale=False, expires_at="2026-12-01T00:00:00+00:00")
    md = "\n".join(render_absences_markdown(absences_block(_out(absences=[item]))))
    assert "current until 2026-12-01T00:00:00+00:00" in md
    assert "last known" not in md


def test_a_declaration_with_no_clock_prints_its_review_path():
    """``layer_declared_absent`` is revised by a map revision, not a clock, so
    it can neither expire nor be stale and must say what supersedes it."""
    item = _item(
        kind="layer_declared_absent",
        subject="domestic_press",
        since="2026-09-20T00:00:00+00:00",
        window=None,
        reason="curated: no masthead operates inside this desk",
        expires_at=None,
        review="map revision",
        stale=False,
        proof=_proof(ref="k5b-map/v1", ref_kind="map_version"),
    )
    md = "\n".join(render_absences_markdown(absences_block(_out(absences=[item]))))
    assert "no schedule — revised by map revision" in md
    assert "record of that look: `k5b-map/v1` (map_version)" in md


def test_every_printed_item_carries_the_proof_rule():
    md = "\n".join(render_absences_markdown(absences_block(_out(absences=[_item()]))))
    assert "checked: the latest kind='finding' row for this unit" in md
    assert "(at 2026-09-24T04:40:01+00:00)" in md
    assert "record of that look:" in md


def test_the_per_kind_cap_states_what_it_holds_back_in_both_formats():
    """A capped list presented as the whole story is the failure a brief about
    absence cannot have — and the JSON must not disagree with the page."""
    many = [_item(subject=f"unit_{n}") for n in range(ABSENCES_PER_KIND + 4)]
    block = absences_block(_out(absences=many))
    group = _group(block, "not_collected")
    assert len(group["items"]) == ABSENCES_PER_KIND
    assert group["count"] == ABSENCES_PER_KIND + 4
    assert group["held_back"] == 4
    md = "\n".join(render_absences_markdown(block))
    assert "+4 more of this kind recorded and not printed here" in md
    # The whole set is reachable, and the line says where.
    assert f"GET /api/v1/v3/absence?scope={DESK}" in md


def test_an_empty_desk_prints_every_kind_as_READ_not_as_blank():
    block = absences_block(_out())
    assert block["item_count"] == 0
    md = "\n".join(render_absences_markdown(block))
    # 7g-2: eight kinds. The composer iterates ABSENCE_KINDS and is
    # kind-agnostic, so this pins the vocabulary against the printed brief.
    assert md.count("read for this desk, nothing absent under this kind") == 8
    assert "typed absences recorded: 0" in md


def test_the_section_opens_on_the_heading_the_print_document_splits_on():
    md = render_absences_markdown(absences_block(_out()))
    assert md[0] == f"## {ABSENCES_HEADING}"


def test_no_block_renders_no_lines():
    assert render_absences_markdown(None) == []
    assert render_absences_markdown({}) == []


# ---------------------------------------------------------------------------
# BYTE-IDENTITY — an export that did not ask is the document it always was
# ---------------------------------------------------------------------------


def _doc(**over) -> dict:
    base = dict(
        title="Desk brief",
        generated_at=_NOW,
        items=[
            {
                "kind": "finding",
                "id": "11111111-1111-1111-1111-111111111111",
                "row_kind": "finding",
                "title": "A read",
                "analyst_id": "escalation",
                "analyst_version": "v3",
                "target_id": DESK,
                "produced_at": "2026-09-24T06:00:00+00:00",
                "superseded": False,
                "body": "Body.",
                "citations": [],
                "verify_state": "unverified — no critique row",
                "confidence": None,
            }
        ],
        appendix="## Open situations & tracked events\n\n- none open",
    )
    base.update(over)
    return build_document(**base)


def test_a_document_that_did_not_ask_carries_no_absences_key_at_all():
    """The key is ABSENT, not null: a document built without this field must
    serialize to exactly the bytes it did before the field existed."""
    doc = _doc()
    assert "absences" not in doc
    assert json.dumps(doc) == json.dumps(_doc())


def test_the_markdown_is_unchanged_when_no_absences_were_asked_for():
    plain = render_markdown(_doc())
    assert ABSENCES_HEADING not in plain
    # And explicitly passing `None` is the same document as not passing it.
    assert render_markdown(_doc(absences=None)) == plain


def test_the_section_is_rendered_after_the_situations_appendix():
    """What a desk HAS is read first; what it does not have is read against
    it, so the absences follow the cited events rather than preceding them."""
    doc = _doc(absences=absences_block(_out(absences=[_item()])))
    md = render_markdown(doc)
    assert md.index("Open situations & tracked events") < md.index(ABSENCES_HEADING)
    # Its own `---` rule — the print document's page break hangs off it.
    assert f"---\n\n## {ABSENCES_HEADING}" in md


def test_both_formats_carry_the_same_block():
    """The JSON document and the markdown are two renderings of ONE block, so
    a reader cannot be told two different things about one desk's silence."""
    block = absences_block(_out(absences=[_item()]))
    doc = _doc(absences=block)
    assert doc["absences"] is block
    md = render_markdown(doc)
    assert doc["absences"]["by_kind"][0]["items"][0]["subject"] in md


# ---------------------------------------------------------------------------
# INTEGRATION — the route, bound through the app
# ---------------------------------------------------------------------------


def _fixed_identity() -> SigningIdentity:
    return SigningIdentity(
        signing_key=SigningKey(b"k5b-export-absences-test-seed-01"[:32]),
        signer_did="did:legba:registry:k5b-export-absences",
    )


@pytest_asyncio.fixture
async def export_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)
    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    await _purge(pg_store)
    identity = _fixed_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    descriptor_registry = DescriptorRegistry(
        pg_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await descriptor_registry.start()
    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_export_router(deps), prefix="/api/v1/v3")
    yield app, pg_store
    await _purge(pg_store)
    await descriptor_registry.stop()
    await pg_store.close()


async def _purge(pg_store: PostgresStore) -> None:
    async with pg_store.acquire() as conn:
        await conn.execute(
            "DELETE FROM public.analyst_outputs WHERE target_id = $1", DESK
        )
        await conn.execute(
            "DELETE FROM public.desk_apertures WHERE target_id = $1", DESK
        )


@pytest_asyncio.fixture
async def client(export_app):
    app, _ = export_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


async def _seed_finding(pg_store: PostgresStore) -> str:
    """One basket item, so the export has something to carry the section on."""
    fid = str(uuid4())
    async with pg_store.acquire() as conn:
        await conn.execute(
            "INSERT INTO public.analyst_outputs "
            "  (id, kind, title, body, confidence, data, target_id, "
            "   analyst_id, analyst_version, produced_at, schema_uri) "
            "VALUES ($1::uuid, 'finding', $2, $3, 0.8, '{}'::jsonb, $4, "
            "        'escalation', 'v3', $5, "
            "        'iglu:legba/finding/jsonschema/1-0-0')",
            fid,
            "Escalation — unit read",
            "Border incidents counted 7 in 14 days.",
            DESK,
            datetime.now(timezone.utc) - timedelta(hours=2),
        )
        await conn.execute(
            "INSERT INTO public.desk_apertures "
            "(target_id, layer, declared, reason, map_version) "
            "VALUES ($1, 'official', 'absent', $2, 'k5b-test-map/v1')",
            DESK,
            "curated: no state feed is registered for this desk",
        )
    return fid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_json_export_carries_the_server_composed_block(export_app, client):
    _, pg_store = export_app
    fid = await _seed_finding(pg_store)

    r = await client.post(
        _ROUTE,
        json={
            "items": [{"kind": "finding", "id": fid}],
            "format": "json",
            "appendix": {"markdown": "## Open situations", "absences": True,
                         "scope": DESK},
        },
    )
    assert r.status_code == 200, r.text
    doc = r.json()
    block = doc["absences"]
    assert block["scope"] == DESK
    # The block's `read_at` is the DOCUMENT's own instant, not a second clock.
    assert block["read_at"] == doc["generated_at"]
    assert [g["kind"] for g in block["by_kind"]] == list(ABSENCE_KINDS)
    assert block["note"] == GLOSSARY_NOTE
    # The seeded declaration is there, with its reason verbatim and no clock.
    declared = _group(block, "layer_declared_absent")
    assert declared["state"] == STATE_ABSENT
    assert declared["items"][0]["subject"] == "official"
    assert declared["items"][0]["expires_at"] is None
    assert declared["items"][0]["review"] == "map revision"
    # A desk with no card is a desk whose floor kind was NOT MEASURED — never
    # a desk reported as banding cleanly.
    assert _group(block, "below_floor")["state"] == STATE_NOT_MEASURED
    # The caller's own appendix is still carried, verbatim and unparsed.
    assert doc["appendix"] == "## Open situations"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_markdown_export_prints_the_same_answer(export_app, client):
    _, pg_store = export_app
    fid = await _seed_finding(pg_store)

    r = await client.post(
        _ROUTE,
        json={
            "items": [{"kind": "finding", "id": fid}],
            "format": "markdown",
            "appendix": {"absences": True, "scope": DESK},
        },
    )
    assert r.status_code == 200, r.text
    md = r.text
    assert f"## {ABSENCES_HEADING}" in md
    assert GLOSSARY_NOTE in md
    assert "curated: no state feed is registered for this desk" in md
    assert "no schedule — revised by map revision" in md
    # Every one of the seven is named, whatever state it is in.
    for kind in ABSENCE_KINDS:
        assert f"### {kind.replace('_', ' ')}" in md


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_export_that_does_not_ask_is_byte_identical(export_app, client):
    """The string form of ``appendix`` — every pre-k5b caller — must produce
    exactly the bytes it always did."""
    _, pg_store = export_app
    fid = await _seed_finding(pg_store)
    body = {
        "items": [{"kind": "finding", "id": fid}],
        "format": "markdown",
        "appendix": "## Open situations\n\n- none open",
    }
    a = await client.post(_ROUTE, json=body)
    b = await client.post(
        _ROUTE, json={**body, "appendix": {"markdown": body["appendix"]}}
    )
    assert a.status_code == 200 and b.status_code == 200
    assert ABSENCES_HEADING not in a.text
    # The object form WITHOUT the ask renders the same document as the string
    # form: asking is the only thing that changes the bytes. The generated_at
    # stamp differs between two calls, so compare everything else.
    strip = lambda t: re.sub(r"- generated_at: .*", "", t)  # noqa: E731
    assert strip(a.text) == strip(b.text)

    j = await client.post(_ROUTE, json={**body, "format": "json"})
    assert "absences" not in j.json()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_asking_without_a_scope_is_refused_rather_than_guessed(
    export_app, client
):
    """A basket can hold rows from more than one desk. Guessing which desk the
    brief is about would stamp another desk's silence onto this document."""
    _, pg_store = export_app
    fid = await _seed_finding(pg_store)
    r = await client.post(
        _ROUTE,
        json={
            "items": [{"kind": "finding", "id": fid}],
            "format": "json",
            "appendix": {"absences": True},
        },
    )
    assert r.status_code == 422, r.text
    assert "appendix.scope" in r.text


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_the_absences_composer_imports_without_the_runtime_stack() -> None:
    """``export_absences`` reaches the registry through ``absence_api``, which
    ships in the SLIM image — so it must not drag the analyst/runtime graph in
    behind it (the 2026-09-17 registry-slim rule)."""
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.registry.export_absences as m

        leaked = sorted(
            n for n in sys.modules
            if n.startswith("legba.data.analysts") or n.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime package reachable: %r" % leaked
        assert m.absences_block is not None
        assert m.ABSENCES_HEADING
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p
            ),
        },
    )
    assert result.returncode == 0, (
        f"slim import failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout


_PRINT_TS = _REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "printDocument.ts"
_ABSENCE_TS = _REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "absenceModel.ts"


def test_the_print_documents_heading_matches_this_modules() -> None:
    """The print document splits the composed markdown on this heading to give
    the absences their own page. Renamed on one side only, the section runs on
    from the situations appendix and loses its page break — silently."""
    text = _PRINT_TS.read_text(encoding="utf-8")
    m = re.search(r"export const ABSENCES_HEADING = '([^']+)'", text)
    assert m, "ABSENCES_HEADING not found in printDocument.ts"
    assert m.group(1) == ABSENCES_HEADING


def test_the_glossary_sentence_is_identical_on_both_reader_surfaces() -> None:
    """The printed brief carries this module's copy and the Morning Read
    carries the TypeScript one. A reader who meets typed absence on two
    surfaces must not be told two slightly different things about what it is."""
    text = _ABSENCE_TS.read_text(encoding="utf-8")
    block = re.search(
        r"export const TYPED_ABSENCE_GLOSSARY_NOTE =\s*(.*?)\n\n", text, re.S
    )
    assert block, "TYPED_ABSENCE_GLOSSARY_NOTE not found in absenceModel.ts"
    ts = "".join(re.findall(r"'([^']*)'", block.group(1)))
    assert ts == GLOSSARY_NOTE


def test_the_readers_kind_ORDER_matches_the_routes_own() -> None:
    """The band renders the vocabulary in the order the route publishes it. A
    reader list that drifted would drop or reorder a kind on the surface while
    the route kept answering with all seven."""
    text = _ABSENCE_TS.read_text(encoding="utf-8")
    block = re.search(
        r"ABSENCE_KIND_ORDER: readonly AbsenceKind\[\] = \[(.*?)\]", text, re.S
    )
    assert block, "ABSENCE_KIND_ORDER not found in absenceModel.ts"
    assert re.findall(r"'([a-z_]+)'", block.group(1)) == list(ABSENCE_KINDS)
