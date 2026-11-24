# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane U-10 — the Record sheet's reads on the real path (migrated Postgres,
the isolated test stack only).

Covered, through the router mounted on a FastAPI app:

* ``/v3/record/receipts`` — a signal's two receipts (the feed response and its
  page), where each version's bytes are held (cold for one), the source and
  its class, the page address's later version with its drift class; and the
  three no-receipt reasons, each from its own cause (arrived before receipts
  began; a Telegram source; a fetching source with no link);
* ``/v3/record/version/{id}/text`` — an operator gets the stored text, capped
  and marked truncated, with the whole text re-hashed against its stored
  sha256; a READER gets no text (the display policy) and the
  ``DisplayPolicyMiddleware`` rule would withhold ``full_text`` from one even
  if the route did not; a refusing licence withholds the text from everyone;
  a feed response says it kept no text;
* roll 85 (B-1 wired): every signal record carries ``byte_path``'s evidence
  label (``received bytes`` / ``archived later`` / ``no bytes kept``), the
  no-receipt reason is given only for a ``no bytes kept`` record with no link,
  and ``/v3/record/signal/{id}/item`` returns the item AS RECEIVED from the
  held bytes (a feed item cut from its response) under the version-text
  rules;
* ``/v3/record/integrity`` — a recorded run re-hashes (verified), a signed
  checkpoint naming it exactly (verified) or only a later one (covered), the
  first submitted daily anchor after it (covered), and a mutated trace
  (failed).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.config import PostgresConfig
from legba.data.display_policy import READER, display_payload
from legba.data.postgres import PostgresStore
from legba.data.provenance.receipts import RuntimeReceiptChain
from legba.data.registry import route_roles as rr
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.record_sheet_api import build_record_sheet_router
from tests.data_pkg.test_evidence_drift_db import _clean as _clean_drift

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

NEWS = "source.test.u10_news"
TG = "source.test.u10_tg"
WALLED = "source.test.u10_walled"
CLAIMS = "source.test.u10_claims"  # roll 84: a claim source (C-2)
UNIT = "an_u10_record"
NOW = datetime.now(timezone.utc)
T0 = NOW - timedelta(days=3)
FEED = "https://news.u10.example.org/feed.xml"
ARTICLE = "https://news.u10.example.org/2026/11/port-strike"
WALLED_ARTICLE = "https://walled.u10.example.org/a/1"
CLAIM_POST = "https://claims.u10.example.org/p/1"
TEXT_1 = "The port strike entered its third day. Cranes stood idle at every berth."
TEXT_2 = "The port strike ended after talks. Cranes resumed work at noon."


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class _Registry:
    def __init__(self, pg: PostgresStore) -> None:
        self.pg = pg


async def _clean(conn) -> None:
    await _clean_drift(conn)
    await conn.execute("DELETE FROM evidence_archive_cold WHERE bundle_key = 'u10/bundle'")
    await conn.execute("DELETE FROM evidence_archive_bundles WHERE bundle_key = 'u10/bundle'")
    await conn.execute("DELETE FROM signals WHERE source_id = ANY($1::text[])", [NEWS, TG, WALLED, CLAIMS])
    await conn.execute("DELETE FROM source_descriptors WHERE descriptor_id = ANY($1::text[])", [NEWS, TG, WALLED, CLAIMS])
    await conn.execute("DELETE FROM audit_checkpoints WHERE analyst_id = $1", UNIT)
    await conn.execute("DELETE FROM analyst_traces WHERE analyst_id = $1", UNIT)
    await conn.execute("DELETE FROM receipt_anchors WHERE calendar LIKE 'https://u10.%'")


@pytest_asyncio.fixture
async def world(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)
    pg = PostgresStore(migrated_pg)
    await pg.connect()
    async with pg.acquire() as c:
        await _clean(c)
        for sid, kind, scope in (
            (NEWS, "rss", {"source_class": "reporting", "license_class": None, "access_class": "public"}),
            (TG, "telegram_channel", {"source_class": "official", "access_class": "public"}),
            (WALLED, "rss", {"source_class": "reporting", "license_class": "anti_ai_walled", "access_class": "public"}),
            (CLAIMS, "rss", {"source_class": "claim", "access_class": "public"}),
        ):
            await c.execute(
                "INSERT INTO source_descriptors (descriptor_id, version, schema_uri, is_head, kind, state, owner, "
                "name, body) VALUES ($1, 'v1', 'legba/source/1.0.0', TRUE, $2, 'active', 'test', $3, $4::jsonb)",
                sid, kind, f"U10 {kind} source", json.dumps({"scope": scope}),
            )

        async def version(address: str, url: str, role: str, body: str, text: str | None, at: datetime,
                          *, source: str | None, stored: bool) -> tuple[int, str]:
            sha = _sha(body)
            vid = await c.fetchval(
                "INSERT INTO fetch_versions (address, url, sha256, hash_basis, role, first_source_id, last_source_id, "
                "status, content_type, size_bytes, object_ref, store_status, text_sha256, text_chars, extracted_text, "
                "first_seen_at, last_seen_at) VALUES ($1, $2, $3, 'entity', $4, $5, $5, 200, 'text/html; charset=utf-8', "
                "$6, $7, $8, $9, $10, $11, $12, $12) RETURNING id",
                address, url, sha, role, source, len(body), f"cas:sha256/{sha}" if stored else None,
                "stored" if stored else "not_stored", None if text is None else _sha(text),
                None if text is None else len(text), text, at,
            )
            return int(vid), sha

        feed = await version(FEED, FEED, "response", "<rss>feed body</rss>", None, T0, source=NEWS, stored=True)
        page1 = await version(ARTICLE, ARTICLE, "page", f"<html>{TEXT_1}</html>", TEXT_1, T0, source=NEWS, stored=True)
        page2 = await version(ARTICLE, ARTICLE, "archiver", f"<html>{TEXT_2}</html>", TEXT_2, T0 + timedelta(days=1),
                              source=None, stored=True)
        walled = await version(WALLED_ARTICLE, WALLED_ARTICLE, "page", "<html>walled</html>", "Walled text.", T0,
                               source=WALLED, stored=False)
        claim_v = await version(CLAIM_POST, CLAIM_POST, "page", "<html>claimed</html>", "Group X claims an attack.",
                                T0, source=CLAIMS, stored=True)
        drift_id = await c.fetchval(
            "INSERT INTO fetch_drift_observations (address, source_id, observed_by, earlier_version_id, later_version_id, "
            "earlier_fetched_at, later_fetched_at, earlier_sha256, later_sha256, later_status, later_gone) "
            "VALUES ($1, $2, 'archiver', $3, $4, $5, $6, $7, $8, 200, false) RETURNING id",
            ARTICLE, NEWS, page1[0], page2[0], T0, T0 + timedelta(days=1), page1[1], page2[1],
        )
        await c.execute(
            "INSERT INTO evidence_drift_classifications (drift_id, address, source_id, earlier_version_id, "
            "later_version_id, earlier_fetched_at, later_fetched_at, drift_class, rule, classifier_version) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, 'substantive_edit', 'test', 'drift-classify/1')",
            drift_id, ARTICLE, NEWS, page1[0], page2[0], T0, T0 + timedelta(days=1),
        )
        await c.execute(
            "INSERT INTO evidence_archive_bundles (bundle_key, backend, month, bundle_sha256, member_count, "
            "stored_bytes, original_bytes, verified_at) VALUES ('u10/bundle', 'local', '2026-11', $1, 1, 10, 10, now())",
            "0" * 64,
        )
        await c.execute(
            "INSERT INTO evidence_archive_cold (sha256, bundle_key, member_offset, member_length, stored_codec, "
            "hot_removed_at) VALUES ($1, 'u10/bundle', 0, 10, 'zstd', now())",
            page1[1],
        )

        async def signal(source: str, url: str | None, at: datetime, title: str) -> str:
            sid = uuid4()
            await c.execute(
                "INSERT INTO signals (id, source_id, fetched_at, payload, content_hash, canonical_url, schema_uri) "
                "VALUES ($1, $2, $3, $4::jsonb, $5, $6, 'iglu:legba/signal/jsonschema/3-0-0')",
                sid, source, at, json.dumps({"title": title}), uuid4().hex, url,
            )
            return str(sid)

        linked = await signal(NEWS, ARTICLE, T0, "Port strike enters day three")
        before = await signal(NEWS, "https://news.u10.example.org/old", datetime(2020, 1, 1, tzinfo=timezone.utc), "Old item")
        telegram = await signal(TG, "https://t.me/u10/1", NOW, "A channel post")
        unlinked = await signal(NEWS, "https://news.u10.example.org/new", NOW, "A new item")
        walled_sig = await signal(WALLED, WALLED_ARTICLE, T0, "A walled item")
        for rid, role, (vid, sha) in ((linked, "response", feed), (linked, "page", page1), (walled_sig, "page", walled)):
            await c.execute(
                "INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256) "
                "VALUES ('signal', $1, $2, $3, $4)", rid, role, vid, sha,
            )

    deps = RegistryAPIDeps(
        descriptor_registry=_Registry(pg),  # type: ignore[arg-type]
        stack_registry=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=None,
        conversion_registry=None,
    )
    inner = FastAPI()
    inner.include_router(build_record_sheet_router(deps), prefix="/api/v1/v3")

    async def app(scope, receive, send):
        """The bearer/session middleware's stamp (``scope['state'].legba_role``),
        taken from a test header — a pure ASGI layer, as the real one is."""
        if scope["type"] == "http":
            for k, v in scope.get("headers") or []:
                if k == b"x-test-role":
                    # what the bearer middleware stamps for an admitted caller
                    # of that class (require_bearer then keeps the stamp)
                    st = scope.setdefault("state", {})
                    st[rr.STATE_AUTH_SOURCE_ATTR] = rr.AUTH_SOURCE_BEARER
                    st["principal_actor"] = f"test:{v.decode()}"
                    st["legba_role"] = v.decode()
        await inner(scope, receive, send)
    ids = {"linked": linked, "before": before, "telegram": telegram, "unlinked": unlinked, "walled": walled_sig,
           "feed": feed[0], "page1": page1[0], "page2": page2[0], "walled_v": walled[0], "claim_v": claim_v[0]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client, pg, ids
    async with pg.acquire() as c:
        await _clean(c)
    await pg.close()


async def test_receipts_state_each_step_and_each_gap(world) -> None:
    client, _pg, ids = world
    order = [ids["linked"], ids["before"], ids["telegram"], ids["unlinked"], str(uuid4())]
    r = await client.get("/api/v1/v3/record/receipts", params={"kind": "signal", "ids": ",".join(order)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    recs = {x["id"]: x for x in body["records"]}
    assert [x["id"] for x in body["records"]] == order

    linked = recs[ids["linked"]]
    assert linked["source"] == {"id": NEWS, "name": "U10 rss source", "kind": "rss", "source_class": "reporting"}
    assert {(link["role"], link["version_id"]) for link in linked["links"]} == {("response", ids["feed"]), ("page", ids["page1"])}
    assert linked["no_receipt_reason"] is None
    feed_v = body["versions"][str(ids["feed"])]
    page_v = body["versions"][str(ids["page1"])]
    assert feed_v["protocol"] == "https" and feed_v["status"] == 200 and feed_v["has_text"] is False
    assert feed_v["held"] == "hot"
    assert page_v["held"] == "cold"
    assert page_v["sha256"] == _sha(f"<html>{TEXT_1}</html>")
    page = body["pages"][linked["page_address"]]
    assert [v["id"] for v in page["versions"]] == [ids["page1"], ids["page2"]]
    assert body["changes"] == [
        {
            "address": linked["page_address"], "earlier_version_id": ids["page1"], "later_version_id": ids["page2"],
            "drift_class": "substantive_edit", "later_fetched_at": body["changes"][0]["later_fetched_at"],
            "later_gone": False, "later_status": 200,
        }
    ]

    assert recs[ids["before"]]["no_receipt_reason"] == "before_receipts_start"
    assert recs[ids["telegram"]]["no_receipt_reason"] == "source_kind_without_receipts"
    assert recs[ids["unlinked"]]["no_receipt_reason"] == "not_linked"
    assert recs[order[-1]]["found"] is False and recs[order[-1]]["no_receipt_reason"] is None


async def test_receipts_are_bounded(world) -> None:
    client, _pg, _ids = world
    many = ",".join(str(uuid4()) for _ in range(26))
    r = await client.get("/api/v1/v3/record/receipts", params={"kind": "signal", "ids": many})
    assert r.status_code == 422


async def test_text_is_capped_rehashed_and_shown_to_an_operator(world) -> None:
    client, _pg, ids = world
    r = await client.get(f"/api/v1/v3/record/version/{ids['page1']}/text", params={"max_chars": 20},
                         headers={"x-test-role": "operator"})
    body = r.json()
    assert body["shown"] is True and body["withheld_reason"] is None
    assert body["full_text"] == TEXT_1[:20]
    assert body["truncated"] is True and body["chars_total"] == len(TEXT_1) and body["chars_shown"] == 20
    assert body["text_hash_ok"] is True  # the WHOLE stored text re-hashes, not the shown part

    whole = (await client.get(f"/api/v1/v3/record/version/{ids['page1']}/text",
                              headers={"x-test-role": "operator"})).json()
    assert whole["full_text"] == TEXT_1 and whole["truncated"] is False

    feed = (await client.get(f"/api/v1/v3/record/version/{ids['feed']}/text",
                             headers={"x-test-role": "operator"})).json()
    assert feed["shown"] is False and feed["withheld_reason"] == "no_text" and feed["full_text"] is None


async def test_a_reader_never_gets_the_text(world) -> None:
    client, _pg, ids = world
    for role in ("read", "reader-own-rows"):
        body = (await client.get(f"/api/v1/v3/record/version/{ids['page1']}/text",
                                 headers={"x-test-role": role})).json()
        assert body["shown"] is False and body["withheld_reason"] == "reader_display_policy", (role, body)
        assert body["full_text"] is None
    # The REST choke point withholds the field from a reader a second time.
    leaked = {"version_id": 1, "sha256": "a" * 64, "full_text": TEXT_1}
    assert "full_text" not in display_payload(leaked, READER)


async def test_a_refusing_licence_withholds_the_text_from_everyone(world) -> None:
    client, _pg, ids = world
    body = (await client.get(f"/api/v1/v3/record/version/{ids['walled_v']}/text",
                             headers={"x-test-role": "operator"})).json()
    assert body["withheld_reason"] == "license_forbids" and body["full_text"] is None
    assert "anti_ai_walled" in body["license_classes"]


async def test_a_claim_sources_stored_text_is_admin_only(world) -> None:
    """Roll 84 (C-2's SEAMS #90, closed): the version-text route applies
    ``claim_support.claim_text_withheld`` — a claim source's stored text goes
    to the operator class only, and the reason named is the claim rule."""
    client, _pg, ids = world
    for role in ("read", "reader-own-rows"):
        body = (await client.get(f"/api/v1/v3/record/version/{ids['claim_v']}/text",
                                 headers={"x-test-role": role})).json()
        assert body["shown"] is False and body["full_text"] is None, (role, body)
        assert body["withheld_reason"] == "claim_text_admin_only", (role, body)
    op = (await client.get(f"/api/v1/v3/record/version/{ids['claim_v']}/text",
                           headers={"x-test-role": "operator"})).json()
    assert op["shown"] is True and op["full_text"] == "Group X claims an attack."
    # a non-claim page is untouched by the rule
    plain = (await client.get(f"/api/v1/v3/record/version/{ids['page1']}/text",
                              headers={"x-test-role": "read"})).json()
    assert plain["withheld_reason"] == "reader_display_policy"


async def test_an_unknown_version_is_not_found(world) -> None:
    client, _pg, _ids = world
    body = (await client.get("/api/v1/v3/record/version/999999999/text", headers={"x-test-role": "operator"})).json()
    assert body["available"] is True and body["found"] is False and body["shown"] is False


async def test_integrity_states(world) -> None:
    client, pg, _ids = world
    chain = RuntimeReceiptChain(pg.pool)
    started = NOW - timedelta(hours=2)
    ended = started + timedelta(seconds=5)
    run_a, run_b = uuid4(), uuid4()
    hash_a, _ = await chain.record(
        run_id=run_a, analyst_id=UNIT, analyst_version="v1", cadence_trigger="manual", target_id=None,
        input_row_refs=[], input_payload={}, prompt_module_hash=None, prompt_rendered=None,
        output_row_refs=[uuid4()], output_payload={"summary": "a"}, run_started_at=started, run_ended_at=ended,
    )
    await chain.record(
        run_id=run_b, analyst_id=UNIT, analyst_version="v1", cadence_trigger="manual", target_id=None,
        input_row_refs=[], input_payload={}, prompt_module_hash=None, prompt_rendered=None,
        output_row_refs=[uuid4()], output_payload={"summary": "b"}, run_started_at=started + timedelta(minutes=1),
        run_ended_at=ended + timedelta(minutes=1),
    )
    async with pg.acquire() as c:
        await c.execute(
            "INSERT INTO audit_checkpoints (analyst_id, chain_head_hash, trace_count, checkpointed_at, signature, signer_did) "
            "VALUES ($1, $2, 1, $3, '\\x00'::bytea, 'did:legba:registry:u10')", UNIT, hash_a, ended + timedelta(seconds=1),
        )
        await c.execute(
            "INSERT INTO receipt_anchors (day, root_hash, leaf_count, calendar, status, submitted_at, created_at) "
            "VALUES (current_date, $1, 3, 'https://u10.cal/a', 'submitted', now(), now())", "f" * 64,
        )

    a = (await client.get("/api/v1/v3/record/integrity", params={"run_id": str(run_a)})).json()
    assert a["found"] is True and a["receipt"]["state"] == "verified"
    assert a["checkpoint"] == {"state": "verified", "signer_did": "did:legba:registry:u10", "exact": True}
    assert a["anchor"]["state"] == "covered" and a["anchor"]["calendars"] >= 1

    b = (await client.get("/api/v1/v3/record/integrity", params={"run_id": str(run_b)})).json()
    assert b["receipt"]["state"] == "verified"
    assert b["checkpoint"]["state"] == "not_covered"  # the only checkpoint predates run b

    async with pg.acquire() as c:
        await c.execute(
            "INSERT INTO audit_checkpoints (analyst_id, chain_head_hash, trace_count, checkpointed_at, signature, signer_did) "
            "VALUES ($1, $2, 2, now() + interval '1 minute', '\\x00'::bytea, 'did:legba:registry:u10')", UNIT, "e" * 64,
        )
        await c.execute(
            "UPDATE analyst_traces SET output_payload = '{\"summary\": \"rewritten\"}'::jsonb WHERE run_id = $1", run_a,
        )
    b2 = (await client.get("/api/v1/v3/record/integrity", params={"run_id": str(run_b)})).json()
    assert b2["checkpoint"]["state"] == "covered" and b2["checkpoint"]["exact"] is False
    a2 = (await client.get("/api/v1/v3/record/integrity", params={"run_id": str(run_a)})).json()
    assert a2["receipt"]["state"] == "failed"

    missing = (await client.get("/api/v1/v3/record/integrity", params={"run_id": str(uuid4())})).json()
    assert missing["found"] is False and missing["receipt"]["state"] == "not_covered"


async def test_every_signal_record_carries_the_byte_path_label(world) -> None:
    """Roll 85: the sheet's label is ``byte_path.evidence_states`` (B-1), not
    a rule of its own; the no-receipt reason is only for an unlinked
    ``no bytes kept`` record."""
    client, _pg, ids = world
    order = [ids["linked"], ids["before"], ids["telegram"], ids["unlinked"], ids["walled"]]
    body = (await client.get("/api/v1/v3/record/receipts", params={"kind": "signal", "ids": ",".join(order)})).json()
    recs = {x["id"]: x for x in body["records"]}
    linked = recs[ids["linked"]]["evidence"]
    assert linked["label"] == "received_bytes" and linked["label_text"] == "received bytes"
    # the article page is preferred over the item's feed response (byte_path's order)
    assert linked["version_id"] == ids["page1"] and linked["link_role"] == "page" and linked["has_text"] is True
    for key in ("before", "telegram", "unlinked"):
        ev = recs[ids[key]]["evidence"]
        assert ev["label"] == "no_bytes_kept" and ev["version_id"] is None and ev["hash_only"] == [], key
    walled = recs[ids["walled"]]
    # linked, but its bytes were never stored: no bytes kept, named by hash — and NOT a no-receipt case
    assert walled["evidence"]["label"] == "no_bytes_kept" and walled["evidence"]["hash_only"] == [ids["walled_v"]]
    assert walled["no_receipt_reason"] is None and walled["links"]


async def test_the_item_as_received_is_read_from_the_held_bytes(world, tmp_path, monkeypatch) -> None:
    """Roll 85 (B-1 wired): a feed response keeps no text of its own, so the
    sheet reads the ITEM from its stored bytes — re-verified, cut at the
    item's locator — to an operator; a reader gets the display policy; a
    record with no bytes says so; an unknown id is not found."""
    from legba.data.archive import ARCHIVE_ROOT_ENV
    from legba.data.sources.fetch_capture import FetchCapture
    from legba.runtime.fetch_receipts import ReceiptRecorder, RecordLink, VersionWriter

    monkeypatch.setenv(ARCHIVE_ROOT_ENV, str(tmp_path))
    client, pg, ids = world
    body = (b"<rss><channel><item><guid>u10-a</guid><title>Cranes idle</title></item>"
            b"<item><guid>u10-b</guid><title>Port strike ends after talks</title></item></channel></rss>")
    rec = ReceiptRecorder(source_id=NEWS, writer=VersionWriter(root=tmp_path), enabled=True)
    rec.log.add(FetchCapture(url="https://news.u10.example.org/items.xml", method="GET", status=200,
                             content_type="application/rss+xml", content_encoding=None, role="response",
                             fetched_at=NOW, complete=True, body=body, sha256=_sha_b(body), size=len(body)))
    async with pg.acquire() as c:
        sid = uuid4()
        await c.execute(
            "INSERT INTO signals (id, source_id, fetched_at, payload, content_hash, canonical_url, schema_uri) "
            "VALUES ($1, $2, now(), $3::jsonb, $4, $5, 'iglu:legba/signal/jsonschema/3-0-0')",
            sid, NEWS, json.dumps({"title": "Port strike ends"}), uuid4().hex, "https://news.u10.example.org/b",
        )
        await rec.link(c, "signal", sid, RecordLink(0, locator={"item_id": "u10-b"}))

    op = (await client.get(f"/api/v1/v3/record/signal/{sid}/item", headers={"x-test-role": "operator"})).json()
    assert op["available"] is True and op["found"] is True, op
    assert op["evidence"]["label"] == "received_bytes" and op["evidence"]["link_role"] == "response"
    assert op["shown"] is True and op["withheld_reason"] is None
    assert "Port strike ends after talks" in op["full_text"] and "Cranes idle" not in op["full_text"]
    assert op["basis"] == "feed_item" and op["version_sha256"] == _sha_b(body)
    assert op["text_sha256"] == _sha_b(op["full_text"].encode()) and op["truncated"] is False

    capped = (await client.get(f"/api/v1/v3/record/signal/{sid}/item", params={"max_chars": 5},
                               headers={"x-test-role": "operator"})).json()
    assert capped["chars_shown"] == 5 and capped["truncated"] is True and capped["chars_total"] == op["chars_total"]

    reader = (await client.get(f"/api/v1/v3/record/signal/{sid}/item", headers={"x-test-role": "read"})).json()
    assert reader["shown"] is False and reader["full_text"] is None
    assert reader["withheld_reason"] == "reader_display_policy"

    none = (await client.get(f"/api/v1/v3/record/signal/{ids['telegram']}/item",
                             headers={"x-test-role": "operator"})).json()
    assert none["found"] is True and none["withheld_reason"] == "no_bytes_kept" and none["full_text"] is None

    walled = (await client.get(f"/api/v1/v3/record/signal/{ids['walled']}/item",
                               headers={"x-test-role": "operator"})).json()
    assert walled["withheld_reason"] == "no_bytes_kept"   # linked, bytes never stored

    unknown = (await client.get(f"/api/v1/v3/record/signal/{uuid4()}/item",
                                headers={"x-test-role": "operator"})).json()
    assert unknown["available"] is True and unknown["found"] is False and unknown["shown"] is False


def _sha_b(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()
