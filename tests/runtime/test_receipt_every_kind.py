# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B-1 guard — EVERY registered source kind gives every signal it writes a receipt.

Parametrised over the kinds registry (``discover_source_kinds``), so a new kind
cannot ship without one:

* each kind is declared in ``receipt_coverage.RECEIPT_COVERAGE`` with a mode;
* a kind with a live-shaped fixture (the frozen-actor equivalence fixtures, plus
  the push and subscribe paths) is RUN through today's source actor and every
  signal it writes must carry a ``fetch_version_records`` link — with a locator
  for a poll response;
* an ``http_tee`` kind without a fixture must route its egress through the
  guarded client (whose transport tees every response) and build no bare
  ``httpx.AsyncClient``;
* the ``not_captured`` set is PINNED here: it may only shrink.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone

import pytest

from legba.data.sources.receipt_coverage import MODES, NOT_CAPTURED_REASONS, RECEIPT_COVERAGE
from legba.runtime import source_actor as new_sa
from legba.runtime.deps import StandardDeps
from legba.runtime.source_factory import discover_source_kinds

from tests.runtime import _ingest_fakes as F

KINDS = sorted(discover_source_kinds())

#: Kinds still without a receipt. Shrink this set; never grow it.
PINNED_NOT_CAPTURED = frozenset({"email_imap", "gdelt_query", "common_crawl_news", "intelmq_collector_bridge"})

#: Live-shaped poll fixtures, keyed by handler kind.
POLL_FIXTURES = {
    "rss": F.rss_case, "geojson": F.geojson_case, "json_api": F.json_api_case,
    "gdelt_files": F.gdelt_redirect_case, "telegram_channel": F.telegram_case,
}
#: Kinds whose receipts are proven by their own path's equivalence suite.
PROVEN_ELSEWHERE = {
    "generic_webhook": "tests/runtime/test_signal_sink_equivalence.py::test_push_path_is_identical_before_and_after",
    "stream_json": "tests/runtime/test_subscribe_connector.py::test_connect_and_records_land_with_receipts_and_cursor",
    "page": "tests/runtime/test_page_pipeline_equivalence.py (the page version is linked)",
}


@pytest.fixture(autouse=True)
def _private_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")


def test_the_coverage_map_is_exactly_the_registry():
    assert set(RECEIPT_COVERAGE) == set(KINDS), (
        "every registered kind declares how its signals get a receipt "
        "(legba/data/sources/receipt_coverage.py)")
    assert set(RECEIPT_COVERAGE.values()) <= set(MODES)


def test_not_captured_is_pinned_and_explained():
    missing = {k for k, m in RECEIPT_COVERAGE.items() if m == "not_captured"}
    assert missing <= PINNED_NOT_CAPTURED, f"a kind may not newly ship without a receipt: {missing - PINNED_NOT_CAPTURED}"
    assert set(NOT_CAPTURED_REASONS) == missing


async def _poll(kind: str) -> tuple[F.RecordingPool, F.NatsRecorder]:
    now = datetime.now(tz=timezone.utc)
    desc, build = POLL_FIXTURES[kind](now)
    pool, nats = F.RecordingPool(), F.NatsRecorder()
    deps = StandardDeps(pg_pool=pool, nats_publish=nats)
    handler = build()
    if hasattr(handler, "__await__"):
        handler = await handler
    actor_id = f"source::{desc.identity.id}::{desc.identity.version[:16]}"
    core = new_sa.SourceCore(actor_id, new_sa.SourceDeps(descriptor=desc, deps=deps, handler=handler))
    await core.pull_once()
    return pool, nats


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.asyncio
async def test_every_kind_links_every_signal_it_writes(kind: str) -> None:
    mode = RECEIPT_COVERAGE[kind]
    if mode == "not_captured":
        # Pinned without a receipt (NOT_CAPTURED_REASONS says what the capture
        # would be); the pin test above keeps the set from growing.
        assert kind in PINNED_NOT_CAPTURED and NOT_CAPTURED_REASONS[kind]
        return
    if kind in POLL_FIXTURES:
        pool, nats = await _poll(kind)
        signals = [c for c in pool.calls if c[1].startswith("INSERT INTO signals")]
        assert signals and len(nats.messages) == len(signals), f"{kind}: the fixture must write signals"
        links = [c for c in pool.calls if "INSERT INTO fetch_version_records" in c[1]]
        assert sorted(str(c[2][1]) for c in links) == sorted(str(c[2][0]) for c in signals), (
            f"{kind}: every written signal links to its receipt")
        assert all(c[2][5] for c in links), f"{kind}: every poll link carries a locator"
        return
    if kind in PROVEN_ELSEWHERE:
        return   # the path's own equivalence suite asserts its links
    if mode == "inbound_envelope":
        cls = discover_source_kinds()[kind]
        assert getattr(cls, "push_source", False), f"{kind}: an inbound kind must be a push kind"
        return
    assert mode == "http_tee", kind
    src = inspect.getsource(inspect.getmodule(discover_source_kinds()[kind]))
    assert re.search(r"\b(guarded_async_client|fetch_client)\b", src), (
        f"{kind}: an http_tee kind fetches through the guarded (teed) client")
    assert "httpx.AsyncClient(" not in src, f"{kind}: no bare client — it would bypass the tee"
