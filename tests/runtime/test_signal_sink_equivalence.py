# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-L step 0 — the document path before and after the sink extraction.

The FROZEN pre-move actor (``tests/fixtures/ingestion/source_actor_9a59a0da.py``,
the module exactly as it stood at 9a59a0da) and today's actor are run over the
same live-shaped fixtures — RSS (gzip, ETag), GeoJSON, json_api, GDELT files
and Telegram on the poll path, a generic webhook through the inbound drain on
the push path — each against its own recording pool and NATS recorder, twice
(the second poll exercises cursors, the look-back ledger and the S-4 collapse;
the second delivery exercises redelivery idempotency).

Asserted identical, after normalising run-specific uuids and clock reads:
every statement on ``signals`` (after N-S, with the one column migration 0247
added to the INSERT stripped back out — ``RecordingPool.strip_url_hash_column``
asserts its value is a pure function of the row's ``canonical_url``),
``signal_aliases``, ``actor_filter_state``
(cursors and handler state) and ``source_poll_outcomes``, with its arguments,
in order; every published (subject, message); every pull summary; and, on the
push path, every ack/nak/term. The ONLY additions on the new path are rows in
the new ``fetch_*`` tables (fetch receipts, on by default) — and with
``LEGBA_FETCH_RECEIPTS=0`` there are none, so the two recordings are equal in
full.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from legba.runtime import source_actor as new_sa
from legba.runtime.deps import StandardDeps

from tests.runtime import _ingest_fakes as F

@pytest.fixture(autouse=True)
def _private_archive(tmp_path, monkeypatch):
    """Receipts store bytes: keep them in this test's own directory."""
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))


CASES = {
    "rss": F.rss_case, "geojson": F.geojson_case, "json_api": F.json_api_case,
    "gdelt_files": F.gdelt_case, "telegram": F.telegram_case,
    # B-1: the live shape — lastupdate.txt names http://, the host answers 301
    # to https://, and every GDELT signal used to be written with no receipt.
    "gdelt_files_redirect": F.gdelt_redirect_case,
}


async def _handler(build: Any) -> Any:
    h = build()
    if hasattr(h, "__await__"):
        h = await h
    return h


async def _two_polls(mod: Any, desc: Any, build: Any) -> tuple[F.RecordingPool, F.NatsRecorder, list]:
    pool, nats = F.RecordingPool(), F.NatsRecorder()
    deps = StandardDeps(pg_pool=pool, nats_publish=nats)
    actor_id = f"source::{desc.identity.id}::{desc.identity.version[:16]}"
    results = []
    for _ in range(2):
        handler = await _handler(build)
        core = mod.SourceCore(actor_id, mod.SourceDeps(descriptor=desc, deps=deps, handler=handler))
        results.append(await core.pull_once())
    return pool, nats, results


def _comparable(pool: F.RecordingPool, nats: F.NatsRecorder, results: list, now: datetime) -> Any:
    return F.normalise({"sql": pool.calls_on(), "nats": nats.messages, "results": results}, now=now)


@pytest.mark.parametrize("kind", sorted(CASES))
@pytest.mark.asyncio
async def test_poll_path_is_identical_before_and_after(kind: str, monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")
    now = datetime.now(tz=timezone.utc)
    desc, build = CASES[kind](now)
    old = F.load_frozen_actor()

    o_pool, o_nats, o_res = await _two_polls(old, desc, build)
    n_pool, n_nats, n_res = await _two_polls(new_sa, desc, build)
    assert n_pool.strip_url_hash_column() == len(n_nats.messages)  # N-S: one INSERT column

    assert o_nats.messages, f"{kind}: the fixture must actually publish"
    assert _comparable(n_pool, n_nats, n_res, now) == _comparable(o_pool, o_nats, o_res, now)
    # The old path never touches the receipt tables.
    assert o_pool.fetch_table_calls() == []
    # B-1: EVERY kind's written signals link to a receipt — Telegram's is the
    # message as the client received it (MTProto is not HTTP), GDELT's the
    # export zip it read, followed through the http->https redirect.
    links = [c for c in n_pool.fetch_table_calls() if "fetch_version_records" in c[1]]
    assert len(links) == len(n_nats.messages), "every written signal is linked to its response"
    assert n_pool.versions, "the poll's response is kept as a version"
    assert all(c[2][2] == "response" for c in links)
    # ... and each link says where in that version its item sits.
    assert all(len(c[2]) == 6 and c[2][5] for c in links), "every link carries a locator"


@pytest.mark.parametrize("kind", sorted(CASES))
@pytest.mark.asyncio
async def test_receipts_off_is_byte_identical_in_full(kind: str, monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "0")
    now = datetime.now(tz=timezone.utc)
    desc, build = CASES[kind](now)
    old = F.load_frozen_actor()
    o_pool, o_nats, o_res = await _two_polls(old, desc, build)
    n_pool, n_nats, n_res = await _two_polls(new_sa, desc, build)
    n_pool.strip_url_hash_column()   # N-S: the one added INSERT column (asserted pure)
    full = lambda p, n, r: F.normalise({"sql": p.calls, "nats": n.messages, "results": r}, now=now)  # noqa: E731
    assert full(n_pool, n_nats, n_res) == full(o_pool, o_nats, o_res)


# ---------------------------------------------------------------------------
# PUSH path: generic webhook → inbound drain → emit callback → sink
# ---------------------------------------------------------------------------


class _Msg:
    def __init__(self, data: bytes, delivered: int = 1) -> None:
        self.data = data
        self.subject = "legba.inbound.source.test.nl_webhook"
        self.metadata = type("M", (), {"num_delivered": delivered})()
        self.acts: list[str] = []

    async def ack(self) -> None:
        self.acts.append("ack")

    async def nak(self, *a: Any, **k: Any) -> None:
        self.acts.append("nak")

    async def term(self) -> None:
        self.acts.append("term")


class _Router:
    def __init__(self, handler: Any) -> None:
        self._h = handler

    def get_handler(self, source_id: str) -> Any:
        return self._h


async def _push_run(mod: Any, envelope: bytes) -> tuple[F.RecordingPool, F.NatsRecorder, list]:
    from legba.data.sources.generic_webhook import GenericWebhookConfig, GenericWebhookSourceHandler
    from legba.runtime.inbound_drain import InboundWebhookDrain

    desc = F.descriptor("source.test.nl_webhook", "generic_webhook", {"shared_secret": "s3cret"})
    desc = desc.model_copy(update={"acquisition": "push"})
    pool, nats = F.RecordingPool(), F.NatsRecorder()
    deps = StandardDeps(pg_pool=pool, nats_publish=nats)
    handler = GenericWebhookSourceHandler(GenericWebhookConfig(shared_secret="s3cret"))
    core = mod.SourceCore("source::source.test.nl_webhook::" + "b" * 16,
                          mod.SourceDeps(descriptor=desc, deps=deps, handler=handler))
    handler._emit_signal = core.make_emit_callback()
    await handler.on_configure(core._make_context())
    drain = InboundWebhookDrain(nats_store=None, router=_Router(handler))
    acts = []
    for delivered in (1, 2):   # first delivery, then a redelivery of the same envelope
        msg = _Msg(envelope, delivered)
        await drain._handle_message(msg)
        acts.append(msg.acts)
    return pool, nats, [acts, drain.stats.written, drain.stats.acked]


@pytest.mark.asyncio
async def test_push_path_is_identical_before_and_after(monkeypatch) -> None:
    from legba.data.sources.webhook_router import encode_inbound_envelope

    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")
    now = datetime.now(tz=timezone.utc)
    body = b'{"id": "evt-1", "url": "https://hooks.example.org/e/1", "title": "sensor alarm", "level": 7}'
    env = encode_inbound_envelope("source.test.nl_webhook", body,
                                  {"x-webhook-token": "s3cret", "content-type": "application/json"})
    old = F.load_frozen_actor()
    o_pool, o_nats, o_res = await _push_run(old, env)
    n_pool, n_nats, n_res = await _push_run(new_sa, env)
    assert n_pool.strip_url_hash_column() == 1   # N-S: the one added INSERT column

    assert o_res[0] == [["ack"], ["ack"]], "ack-after-write on both deliveries"
    assert len(o_nats.messages) == 1, "the redelivery is idempotent: one signal, one message"
    assert F.normalise({"sql": n_pool.calls_on(), "nats": n_nats.messages, "r": n_res}, now=now) == \
        F.normalise({"sql": o_pool.calls_on(), "nats": o_nats.messages, "r": o_res}, now=now)
    # The push receipt: the envelope stored once, linked to the one signal;
    # the secret header is kept only as its hash.
    inbound = [c for c in n_pool.fetch_table_calls() if "receipt_kind" in c[1]]
    assert len(inbound) == 1
    headers_json = inbound[0][2][8]
    assert "s3cret" not in headers_json and "sha256:" in headers_json
    links = [c for c in n_pool.calls if "fetch_version_records" in c[1]]
    assert len(links) == 1 and links[0][2][2] == "inbound"
