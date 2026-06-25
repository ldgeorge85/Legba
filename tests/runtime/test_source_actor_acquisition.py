# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-06 acceptance — SourceActor acquisition layer against the dev rig.

Runs against the pivot substrate (``legba_pivot_test``) + the dev NATS. Proves
the P-06 acceptance criteria end-to-end:

  1. A poll ``SourceDescriptor`` activates, pulls from a real (local) RSS
     feed, and writes canonical ``signals`` rows with ``source_id`` +
     ``modality`` + null ``produced_by_id`` (raw source rows) + the
     descriptor's ``owner_tenant`` — and publishes a fan-out event to
     ``source.<id>.signals`` on NATS.
  2. A push source ingests an inbound POST (the generic-webhook reference
     kind) and writes a canonical structured signal carrying ``media_ref``.
  3. The per-source baseline is modality-branched: ``reference`` (default)
     leaves media unfetched; ``eager`` runs the media extractor once.
  4. Provisioning (§4.2.1) reconciliation is idempotent + partial-failure
     recovering.

No mocks for the substrate / NATS / HTTP feed — a real local HTTP server
serves the feed, real asyncpg writes, real JetStream publish/consume.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from uuid import uuid4

import asyncpg
import pytest

from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Cron
from legba.data.schemas.source import (
    CadenceBlock,
    FilterStage,
    ProvisionBlock,
    SourceDescriptor,
    SourceIdentity,
    SourcePipeline,
    SourceScope,
)
from legba.data.sources._contract import Signal, SourceContext
from legba.data.sources.generic_webhook import GenericWebhookSourceHandler
from legba.data.sources.provision import (
    ReconcileResult,
    deprovision_all,
    desired_watch_set,
    reconcile_provision,
)
from legba.data.sources.baseline import run_baseline
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import (
    SourceCore,
    SourceDeps,
    write_canonical_signal,
)
from legba.runtime.state import SCHEMA as STATE_SCHEMA

PG = dict(host="127.0.0.1", port=5432, user="legba", password="legba", database="legba_pivot_test")
NATS_URL = "nats://127.0.0.1:4222"


# ---------------------------------------------------------------------------
# Local RSS feed server (real HTTP — no mock transport)
# ---------------------------------------------------------------------------

_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>P06 Test Feed</title>
  <link>http://localhost/feed</link>
  <item>
    <title>Brazil energy policy shift</title>
    <link>http://localhost/a-{n}</link>
    <guid>urn:p06:a-{n}</guid>
    <pubDate>Mon, 02 Jun 2025 09:00:00 GMT</pubDate>
    <description>An energy story.</description>
    <category>energy</category>
  </item>
  <item>
    <title>G20 summit notes</title>
    <link>http://localhost/b-{n}</link>
    <guid>urn:p06:b-{n}</guid>
    <pubDate>Mon, 02 Jun 2025 10:00:00 GMT</pubDate>
    <description>Summit coverage.</description>
    <category>diplomacy</category>
  </item>
</channel></rss>"""


class _FeedHandler(BaseHTTPRequestHandler):
    feed_body = _FEED.format(n="x")

    def do_GET(self):  # noqa: N802
        body = self.feed_body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        return


@pytest.fixture(scope="module")
def feed_url():
    server = HTTPServer(("127.0.0.1", 0), _FeedHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}/feed"
    server.shutdown()


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(**PG)
    async with p.acquire() as c:
        await c.execute(STATE_SCHEMA)  # ensure actor_filter_state exists
    yield p
    await p.close()


def _payload(row) -> dict:
    """asyncpg returns JSONB as a str unless a codec is set; decode it."""
    raw = row["payload"]
    return raw if isinstance(raw, dict) else json.loads(raw)


@pytest.fixture
async def nats_publish():
    import nats

    # B-1: honour token auth when the substrate NATS requires it.
    _token = os.getenv("LEGBA_NATS_TOKEN") or None
    nc = await nats.connect(NATS_URL, **({"token": _token} if _token else {}))
    js = nc.jetstream()
    # Ensure a stream that captures source.*.signals so we can verify publish.
    from nats.js.api import RetentionPolicy, StreamConfig

    stream = f"P06_{uuid4().hex[:8]}"
    await js.add_stream(
        StreamConfig(name=stream, subjects=["source.>"], retention=RetentionPolicy.LIMITS,
                     max_msgs=1000, max_age=3600.0)
    )

    async def _publish(subject: str, payload: bytes) -> None:
        await js.publish(subject, payload)

    yield _publish, js, stream
    try:
        await js.delete_stream(stream)
    except Exception:
        pass
    await nc.close()


def _rss_descriptor(source_id: str, url: str, *, media: str = "reference") -> SourceDescriptor:
    from datetime import datetime

    return SourceDescriptor(
        identity=SourceIdentity(
            id=source_id,
            name="P06 RSS",
            kind="rss",
            schema_uri="legba/source/3.0.0",
            version="a" * 16,
            owner="test:p06",
            created=datetime.now(tz=timezone.utc),
            state=LifecycleState.ACTIVE,
        ),
        scope=SourceScope(owner_tenant="acme", geo=["BR"], languages=["en"]),
        acquisition="poll",
        config={"url": url},
        cadence=CadenceBlock(schedule=Cron(raw="*/5 * * * *")),
        pipeline=SourcePipeline(media=media),
    )


# ---------------------------------------------------------------------------
# 1. Poll source: activate → pull real feed → canonical write → NATS publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_source_pulls_writes_canonical_and_publishes(pool, nats_publish, feed_url):
    publish, js, stream = nats_publish
    source_id = f"source.test.rss_{uuid4().hex[:8]}"
    sd = _rss_descriptor(source_id, feed_url)
    actor_id = f"source::{source_id}::{sd.identity.version[:8]}"

    deps = StandardDeps(pg_pool=pool, nats_publish=publish)
    core = SourceCore(actor_id, SourceDeps(descriptor=sd, deps=deps))

    # Production publishes ONE message per signal to
    # ``legba.signals.<tenant>.<source_token>.<modality>.raw`` on the shared
    # ``legba_signals`` stream (L-205 — the old aggregate
    # ``source.<id>.signals`` envelope this test originally asserted was
    # retired). The stream's INTEREST retention drops messages no consumer
    # wants, so the subject-filtered consumer must exist BEFORE the pull.
    from legba.data.nats import SIGNAL_STREAM_NAME, signal_subject

    expect_subject = signal_subject(
        tenant="acme", source_id=source_id, modality="text", event_class="raw",
    )
    consumer = await js.pull_subscribe(
        expect_subject, durable=None, stream=SIGNAL_STREAM_NAME,
    )

    result = await core.pull_once()
    assert result["outcome"] == "success", result
    assert result["signals_written"] == 2, result

    # Canonical rows: target-agnostic, source-owned, modality + tenant stamped.
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT source_id, modality, produced_by_kind, produced_by_id, "
            "owner_tenant, content_hash, payload FROM signals WHERE source_id=$1 "
            "ORDER BY fetched_at",
            source_id,
        )
    assert len(rows) == 2
    for r in rows:
        assert r["source_id"] == source_id
        assert r["modality"] == "text"
        assert r["produced_by_kind"] == "source"   # raw source row
        assert r["produced_by_id"] is None
        assert r["owner_tenant"] == "acme"
        assert r["content_hash"]                    # baseline backstop / handler
        assert "title" in _payload(r)

    # Published one fan-out envelope PER signal (canonical Signal JSON with
    # the source tenant stamped — the 761be14 regression surface).
    msgs = await consumer.fetch(2, timeout=10)
    assert len(msgs) == 2
    seen_ids = set()
    for m in msgs:
        env = json.loads(m.data.decode("utf-8"))
        await m.ack()
        assert env["source_id"] == source_id
        assert env["owner_tenant"] == "acme"
        seen_ids.add(env["signal_id"])
    assert len(seen_ids) == 2

    # Cursor persisted (idempotency / restart-survival seam).
    ctx = core._make_context()
    cur = await ctx.state_store.get("cursor")
    assert cur and cur["rows_pulled"] == 2 and cur["last_pulled_at"]

    # Restart survival: a FRESH SourceCore (new in-process instance, same
    # actor_id) re-hydrates the cursor from the crash-safe state store — no
    # corruption, the cursor monotonically advances.
    core2 = SourceCore(actor_id, SourceDeps(descriptor=sd, deps=deps))
    ctx2 = core2._make_context()
    cur2 = await ctx2.state_store.get("cursor")
    assert cur2["last_pulled_at"] == cur["last_pulled_at"]
    result2 = await core2.pull_once()
    # The feed re-serves the same 2 items; the handler+baseline write fresh
    # rows (cross-source dedup is P-09, not here) but the cursor advances and
    # state stays consistent — the seam survives the restart.
    assert result2["outcome"] in {"success", "noop"}
    cur3 = await ctx2.state_store.get("cursor")
    assert cur3["rows_pulled"] >= cur2["rows_pulled"]


# ---------------------------------------------------------------------------
# 1b. Source-side ingest dedupe (P-02, tiers 1+2) wired into the baseline path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_dedupe_links_alias_canonical_at_ingest(pool):
    """A source declaring ``ingestion_filters: [dedupe_tier_1, dedupe_tier_2]``
    links duplicate raw items to a canonical AT INGEST — keeping every raw row
    (alias/canonical, never destructive collapse) and honouring canonical_only.

    Drives ``SourceCore._process_one`` directly with two push signals that share
    a content hash via two distinct sources, then asserts the second is aliased
    to the first while both rows survive.
    """
    from datetime import datetime

    source_id = f"source.test.dedup_{uuid4().hex[:8]}"
    tenant = f"ingdedup_{uuid4().hex[:8]}"
    sd = SourceDescriptor(
        identity=SourceIdentity(
            id=source_id, name="dedup", kind="generic_webhook",
            schema_uri="legba/source/3.0.0", version="d" * 16, owner="test:p02",
            created=datetime.now(tz=timezone.utc), state=LifecycleState.ACTIVE,
        ),
        scope=SourceScope(owner_tenant=tenant, languages=["en"]),
        acquisition="push",
        pipeline=SourcePipeline(
            ingestion_filters=[
                FilterStage(kind="dedupe_tier_1"),
                FilterStage(kind="dedupe_tier_2"),
            ],
        ),
    )
    actor_id = f"source::{source_id}::dedup"
    deps = StandardDeps(pg_pool=pool)
    core = SourceCore(actor_id, SourceDeps(descriptor=sd, deps=deps))
    # The descriptor wired the ingest dedupe engine (tiers 1+2).
    assert core._ingest_dedupe is not None
    assert core._ingest_dedupe.is_tier_active(1)
    assert core._ingest_dedupe.is_tier_active(2)

    ctx = core._make_context()
    # Same content hash, distinct URLs/sources => tier-2 cross-source dup.
    shared_hash = f"ing_{uuid4().hex}"
    sig_a = Signal(
        source_id=source_id, modality="text",
        payload={"title": "Quake hits region"},
        canonical_url="https://reuters.example/quake", content_hash=shared_hash,
    )
    sig_b = Signal(
        source_id=source_id, modality="text",
        payload={"title": "Quake hits region"},
        canonical_url="https://ap.example/quake", content_hash=shared_hash,
    )

    try:
        async with pool.acquire() as conn:
            out_a = await core._process_one(conn, ctx, sig_a)
            out_b = await core._process_one(conn, ctx, sig_b)
        assert out_a is not None and out_b is not None
        # Regression: the RETURNED (and therefore PUBLISHED) signal must carry
        # the source's tenant, not the Signal model default. The reactive
        # trigger matcher re-checks the published envelope's owner_tenant against
        # the subscription binding's owner_tenant; if _process_one left the
        # default here, every fan-out signal would be rejected and reactive
        # triggering would silently never fire (the DB row is stamped via a
        # param, so batch/cadence stayed green — only the per-signal path broke).
        assert out_a.owner_tenant == tenant
        assert out_b.owner_tenant == tenant
        # The dup (B) is linked to A in-memory the instant it's processed.
        # A's returned copy predates B, so it still reads canonical=None — A
        # *was* the canonical/unknown root at that instant. The durable
        # self-canonical stamp on A's ROW (written when the dup links to it) is
        # asserted from the DB below, not off the stale in-memory object.
        assert out_b.canonical_signal_id == sig_a.signal_id

        async with pool.acquire() as conn:
            # BOTH raw rows preserved.
            raw = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE source_id=$1", source_id)
            assert raw == 2

            # A's row is stamped self-canonical (the dup linked back to it).
            a_canon = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1",
                sig_a.signal_id)
            assert a_canon == sig_a.signal_id

            # An alias link was written by ingest dedupe.
            alias = await conn.fetchrow(
                "SELECT alias_signal_id, canonical_signal_id, reason "
                "FROM signal_aliases WHERE alias_signal_id=$1", sig_b.signal_id)
            assert alias is not None
            assert str(alias["canonical_signal_id"]) == str(sig_a.signal_id)
            assert alias["reason"] == "content_hash"

            # canonical_only subscription sees exactly 1 row of the dup set.
            canon_only = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE source_id=$1 "
                "AND (canonical_signal_id = id OR canonical_signal_id IS NULL)",
                source_id)
            assert canon_only == 1
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_aliases WHERE alias_signal_id IN "
                "(SELECT id FROM signals WHERE source_id=$1)", source_id)
            await conn.execute(
                "DELETE FROM signals WHERE source_id=$1", source_id)


# ---------------------------------------------------------------------------
# 2. Push source: inbound POST ingest → canonical structured write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_source_ingests_inbound_post(pool, nats_publish):
    publish, js, stream = nats_publish
    from datetime import datetime

    source_id = f"source.test.webhook_{uuid4().hex[:8]}"
    sd = SourceDescriptor(
        identity=SourceIdentity(
            id=source_id, name="P06 webhook", kind="generic_webhook",
            schema_uri="legba/source/3.0.0", version="b" * 16, owner="test:p06",
            created=datetime.now(tz=timezone.utc), state=LifecycleState.ACTIVE,
        ),
        scope=SourceScope(owner_tenant="acme"),
        acquisition="push",
        config={"modality": "structured", "id_field": "camera_id", "media_ref_field": "vod_url"},
    )
    actor_id = f"source::{source_id}::{sd.identity.version[:8]}"
    deps = StandardDeps(pg_pool=pool, nats_publish=publish)
    handler = GenericWebhookSourceHandler(GenericWebhookSourceHandler.config_schema(
        modality="structured", id_field="camera_id", media_ref_field="vod_url",
    ))
    core = SourceCore(actor_id, SourceDeps(descriptor=sd, deps=deps, handler=handler))

    # The actor binds the handler's emit callback to the core's pipeline.
    emit = core.make_emit_callback()
    ctx = core._make_context()
    handler.bind_emit(ctx, emit)

    # Simulate the inbound POST body (facial-rec match shape).
    body = json.dumps({
        "camera_id": "cam-42", "location": "gate-3",
        "vod_url": "https://fleet.example/vod/abc.mp4",
        "face": "person:X",
    }).encode("utf-8")
    async for sig in handler.ingest(ctx, body, {}):
        await emit(sig)

    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT source_id, modality, media_ref, payload, produced_by_kind "
            "FROM signals WHERE source_id=$1", source_id,
        )
    assert row is not None
    assert row["source_id"] == source_id
    assert row["modality"] == "structured"
    assert row["media_ref"] == "https://fleet.example/vod/abc.mp4"  # REFERENCE, not fetched
    assert row["produced_by_kind"] == "source"
    assert _payload(row)["external_id"] == "cam-42"

    # Bad token path -> 401-mapped PermissionError when a secret is set.
    h2 = GenericWebhookSourceHandler(GenericWebhookSourceHandler.config_schema(shared_secret="s3cret"))
    with pytest.raises(PermissionError):
        async for _ in h2.ingest(ctx, b"{}", {"x-webhook-token": "wrong"}):
            pass


# ---------------------------------------------------------------------------
# 3. Modality-branched baseline: reference vs eager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_reference_vs_eager_seam_refusal(pool):
    """A-2 / D2: eager media is a DECLARED SEAM that refuses loudly.

    A ``media: "eager"`` descriptor with no real extractor wired refuses
    ACTIVATION (typed error from ``SourceCore.__init__``), and the eager
    baseline branch refuses per-signal too (belt + suspenders) — no fabricated
    caption/transcript may land in the pool. The reference tier + tier-1
    structured enrichment are unaffected.
    """
    from legba.data.jobs.media import MediaEndpointNotConfiguredError

    source_id = f"source.test.media_{uuid4().hex[:8]}"
    deps = StandardDeps(pg_pool=pool)

    # eager descriptor + no real extractor → SourceCore REFUSES activation.
    sd_eager = _rss_descriptor(source_id, "http://unused", media="eager")
    sd_eager = sd_eager.model_copy(update={"acquisition": "push"})
    with pytest.raises(MediaEndpointNotConfiguredError):
        SourceCore(
            f"source::{source_id}::eager",
            SourceDeps(descriptor=sd_eager, deps=deps),
        )

    # reference descriptor activates fine; build the ctx from it.
    sd_ref = _rss_descriptor(source_id, "http://unused", media="reference")
    sd_ref = sd_ref.model_copy(update={"acquisition": "push"})
    core = SourceCore(
        f"source::{source_id}::ref", SourceDeps(descriptor=sd_ref, deps=deps),
    )
    ctx = core._make_context()

    img = Signal(source_id=source_id, modality="image",
                 media_ref="https://x/cam.jpg", content_hash="h1")

    # reference: media NOT fetched/processed (no baseline_extraction).
    ref_out = await run_baseline(img.model_copy(deep=True), ctx, media="reference")
    assert "baseline_extraction" not in ref_out.payload

    # eager per-signal guard: media modality + no real extractor → typed
    # refusal, the signal is never written.
    with pytest.raises(MediaEndpointNotConfiguredError):
        await run_baseline(img.model_copy(deep=True), ctx, media="eager")

    # Tier-1 structured enrichment fills typed columns from scope hints.
    txt = Signal(source_id=source_id, modality="text",
                 payload={"tags": ["Energy", "G20"], "title": "t"}, language_hint="pt")
    e = await run_baseline(txt, ctx, media="reference")
    assert e.language == "pt"
    assert "energy" in e.tags and "g20" in e.tags
    assert e.geo == ["BR"]  # carried from descriptor scope


# ---------------------------------------------------------------------------
# 4. Provisioning idempotency + partial-failure recovery (§4.2.1)
# ---------------------------------------------------------------------------


class _FakeUpstream:
    """Records calls; can be told to fail specific watch params (transiently)."""

    def __init__(self):
        self.registered: set[str] = set()
        self.register_calls: list[str] = []
        self.deregister_calls: list[str] = []
        self.fail_params: set[str] = set()

    async def register(self, *, call, watch_param, idempotency_key, credential):
        self.register_calls.append(watch_param)
        if watch_param in self.fail_params:
            return False
        self.registered.add(watch_param)
        return True

    async def deregister(self, *, call, watch_param, idempotency_key, credential):
        self.deregister_calls.append(watch_param)
        if watch_param in self.fail_params:
            return False
        self.registered.discard(watch_param)
        return True


@pytest.mark.asyncio
async def test_provision_reconcile_idempotent_and_partial_failure(pool):
    from datetime import datetime

    source_id = f"source.test.fleet_{uuid4().hex[:8]}"
    sd = SourceDescriptor(
        identity=SourceIdentity(
            id=source_id, name="fleet", kind="generic_webhook",
            schema_uri="legba/source/3.0.0", version="c" * 16, owner="test:p06",
            created=datetime.now(tz=timezone.utc), state=LifecycleState.DRAFT,
        ),
        acquisition="push",
        provision=ProvisionBlock(
            enabled=True,
            register_call={"url": "https://fleet/watch/{watch_param}"},
            deregister_call={"url": "https://fleet/watch/{watch_param}"},
            watch_param_field="face",
            idempotency_key_field="watch_key",
        ),
    )
    actor_id = f"source::{source_id}::prov"
    deps = StandardDeps(pg_pool=pool)
    core = SourceCore(actor_id, SourceDeps(descriptor=sd, deps=deps))
    ctx = core._make_context()
    prov = sd.provision
    client = _FakeUpstream()

    # Subscriber-driven watchlist: two authorized subscriptions add faces.
    subs = [{"face": "person:X"}, {"face": "person:Y"}]
    desired = desired_watch_set(prov, subscriptions=subs)
    assert desired == {"person:X", "person:Y"}

    r1 = await reconcile_provision(ctx, prov, desired=desired, client=client)
    assert set(r1.added) == {"person:X", "person:Y"}
    assert r1.converged
    assert client.registered == {"person:X", "person:Y"}

    # Re-run with identical desired -> idempotent no-op (no new upstream calls).
    calls_before = len(client.register_calls)
    r2 = await reconcile_provision(ctx, prov, desired=desired, client=client)
    assert r2.added == [] and r2.converged
    assert len(client.register_calls) == calls_before  # no duplicate registration

    # Partial failure: adding person:Z fails upstream -> stays pending, others OK.
    client.fail_params = {"person:Z"}
    desired2 = {"person:X", "person:Y", "person:Z"}
    r3 = await reconcile_provision(ctx, prov, desired=desired2, client=client)
    assert "person:Z" in r3.pending
    assert not r3.converged
    assert client.registered == {"person:X", "person:Y"}

    # Recovery: upstream heals -> next reconcile retries Z and converges.
    client.fail_params = set()
    r4 = await reconcile_provision(ctx, prov, desired=desired2, client=client)
    assert "person:Z" in r4.added and r4.converged
    assert client.registered == {"person:X", "person:Y", "person:Z"}

    # Deprovision-all (rollback / on_retire) removes everything.
    r5 = await deprovision_all(ctx, prov, client=client)
    assert set(r5.removed) == {"person:X", "person:Y", "person:Z"}
    assert client.registered == set()
