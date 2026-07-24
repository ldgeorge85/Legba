# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``reenrich_ner`` deterministic sub-handler.

The ONE-TIME NER backfill: re-runs the LIVE ``NERMultilingualHandler``
(translate-then-NER + telegram ``payload.text``) over the ~9,143 signals ingested
BEFORE the multilingual/telegram fix landed (they carry 0 entities). These tests are
DETERMINISTIC and need no live substrate / models-host — they inject a fake pg_pool
and a fake NlpServiceClient (canned triples / translations) and cover:

  * **Registration** — a first-class registered TRACE_ONLY deterministic sub-handler.
  * **Synthetic** (``deps=None``) — no substrate → a zeroed, well-formed run.
  * **Degrade — no nlp** — a missing nlp_client no-ops the tick (``skipped_no_nlp``),
    the SELECT never runs (rows left UNSTAMPED).
  * **Happy path** — a telegram (English) signal + a Russian signal both GAIN
    entities via the REUSED production handler → payload.entities written +
    entity_classes promoted + entities_resolved_at reset + reenriched_at stamped.
  * **No-entity path** — a signal whose NER yields nothing is drained with
    reenriched_at only (entities_resolved_at NOT nulled).
  * **Degrade — poison vs outage** — a poison row amid a healthy tick is stamped
    reenriched_at + the failure sentinel (drains); an all-fail tick (probable
    outage) leaves the rows UNSTAMPED for retry.
  * **SQL-marker invariants** — the write statements stamp the right markers.
"""

from __future__ import annotations

from uuid import uuid4

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    TRACE_ONLY,
    run_method,
)
from legba.data.analysts.deterministic_handlers import reenrich_ner
from legba.data.provenance.models import FindingPayload
from legba.data.stack.nlp_service import NlpServiceUnavailable
from legba.runtime.analyst_method import AnalystMethodResult

SUB = "reenrich_ner"

#: Canned relation triples that classify to entities in the closed taxonomy —
#: "Vladimir Putin" / "Joe Biden" both map to ``person`` (2 title-cased tokens).
_GOOD_TRIPLES = [{"subject": "Vladimir Putin", "predicate": "met", "object": "Joe Biden"}]


# ---------------------------------------------------------------------------
# Fakes — a recording pg_pool + a canned NlpServiceClient
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls

    async def fetch(self, sql, *args):
        self._calls.append(("fetch", sql, args))
        return self._rows

    async def execute(self, sql, *args):
        self._calls.append(("execute", sql, args))
        n = len(args[0]) if args and isinstance(args[0], list) else 0
        return f"UPDATE {n}"


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, rows):
        self.calls: list[tuple] = []
        self._conn = _FakeConn(rows, self.calls)

    def acquire(self):
        return _FakeAcquire(self._conn)


class _FakeNlp:
    """Canned NlpServiceClient. ``extract`` raises on a text containing
    ``fail_marker`` (a service failure); otherwise returns ``triples``. ``translate``
    echoes a translated string so the non-Latin routing exercises the real path."""

    def __init__(
        self,
        *,
        triples=None,
        fail_marker: str | None = None,
        fail_translate: bool = False,
        empty_extract: bool = False,
    ):
        self.triples = triples if triples is not None else list(_GOOD_TRIPLES)
        self.fail_marker = fail_marker
        self.fail_translate = fail_translate
        self.empty_extract = empty_extract
        self.extract_calls: list[str] = []
        self.translate_calls: list[tuple] = []

    async def translate(self, text, *, source_lang, target_lang):
        self.translate_calls.append((text, source_lang, target_lang))
        if self.fail_translate:
            raise NlpServiceUnavailable("translate backend boom")
        return {"translated": f"TR({source_lang}): {text}"}

    async def extract(self, text):
        self.extract_calls.append(text)
        if self.fail_marker is not None and self.fail_marker in text:
            raise NlpServiceUnavailable("extract backend boom")
        if self.empty_extract:
            return {"triples": []}
        return {"triples": list(self.triples)}


class _FakeDeps:
    def __init__(self, pool, extras):
        self.pg_pool = pool
        self.extras = extras


def _row(payload, *, source_id="telegram_ch", language="en", entity_classes=None):
    return {
        "id": uuid4(),
        "source_id": source_id,
        "language": language,
        "entity_classes": entity_classes,
        "payload": payload,
    }


def _executed(pool, sql):
    return [c for c in pool.calls if c[0] == "execute" and c[1] == sql]


def _deps_with_nlp(pool, nlp):
    return _FakeDeps(pool, {reenrich_ner.NLP_DEPS_EXTRA_KEY: nlp})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_reenrich_ner_registered():
    assert SUB in SUB_HANDLERS, "reenrich_ner missing from SUB_HANDLERS"
    assert SUB_HANDLERS[SUB] is reenrich_ner.handle
    # Its real product is the side-written signal enrichment — trace-only.
    assert OUTPUT_KIND_BY_SUB_HANDLER[SUB] is TRACE_ONLY


# ---------------------------------------------------------------------------
# Synthetic path — no substrate, zeroed run, never spends tokens
# ---------------------------------------------------------------------------


async def test_synthetic_no_deps_zeroed_run():
    result = await run_method(
        [], {"sub_handler": SUB, "analyst_id": "rn", "run_id": uuid4()}, None,
    )
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    data = result.finding.data
    assert data["sub_handler"] == SUB
    assert data["examined"] == 0
    assert data["reenriched"] == 0
    assert data["entities_added"] == 0
    assert data["no_entities"] == 0
    assert data["failures"] == 0
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


# ---------------------------------------------------------------------------
# Degrade — no nlp client wired → no-op tick, rows left UNSTAMPED
# ---------------------------------------------------------------------------


async def test_degrade_no_nlp():
    pool = _FakePool([_row({"text": "Vladimir Putin met Joe Biden"})])
    deps = _FakeDeps(pool, {})  # no nlp client in extras
    result = await reenrich_ner.handle(
        [], {"sub_handler": SUB, "analyst_id": "rn", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["skipped_no_nlp"] == 1
    assert data["examined"] == 0
    # No SELECT / stamp ran — the no-op precedes the sweep.
    assert pool.calls == []


# ---------------------------------------------------------------------------
# Happy path — a telegram + a russian signal both gain entities
# ---------------------------------------------------------------------------


async def test_happy_path_reenriches_promotes_and_nulls_resolution():
    tg = _row(
        {"text": "Vladimir Putin met Joe Biden in Geneva", "language": "en"},
        source_id="telegram_world", language="en", entity_classes=None,
    )
    ru = _row(
        {"text": "Владимир Путин", "language": "ru"},
        source_id="rss_ru", language="ru", entity_classes=[],
    )
    pool = _FakePool([tg, ru])
    nlp = _FakeNlp(triples=_GOOD_TRIPLES)
    deps = _deps_with_nlp(pool, nlp)

    result = await reenrich_ner.handle(
        [], {"sub_handler": SUB, "analyst_id": "rn", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["examined"] == 2
    assert data["reenriched"] == 2
    # 2 entities per row (Putin + Biden, both `person`, deduped in-triple-set).
    assert data["entities_added"] == 4
    assert data["no_entities"] == 0
    assert data["failures"] == 0

    # The Russian row was translated BEFORE NER (translate-then-NER path exercised).
    assert len(nlp.translate_calls) == 1
    assert nlp.translate_calls[0][1] == "ru"
    # Both rows hit /extract.
    assert len(nlp.extract_calls) == 2

    # Each re-enriched row was written via the atomic reenriched UPDATE — payload
    # entities + promoted classes + entities_resolved_at reset + reenriched_at.
    writes = _executed(pool, reenrich_ner._WRITE_REENRICHED_SQL)
    assert len(writes) == 2
    written_ids = {w[2][0] for w in writes}
    assert written_ids == {tg["id"], ru["id"]}
    # The promoted class list (arg $3) is the distinct entity classes = ["person"].
    for w in writes:
        assert w[2][2] == ["person"]
    # No drain / failure stamp ran (every row gained entities).
    assert _executed(pool, reenrich_ner._STAMP_NO_ENTITIES_BULK_SQL) == []
    assert _executed(pool, reenrich_ner._STAMP_FAILED_BULK_SQL) == []


# ---------------------------------------------------------------------------
# No-entity path — drained with reenriched_at only (resolution NOT nulled)
# ---------------------------------------------------------------------------


async def test_no_entity_row_drained_without_nulling_resolution():
    row = _row({"text": "nothing nameable here"})
    pool = _FakePool([row])
    nlp = _FakeNlp(triples=[])  # NER runs, finds nothing
    deps = _deps_with_nlp(pool, nlp)

    result = await reenrich_ner.handle(
        [], {"sub_handler": SUB, "analyst_id": "rn", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["examined"] == 1
    assert data["reenriched"] == 0
    assert data["no_entities"] == 1
    assert data["failures"] == 0
    # Extract was called (the NER ran) but found nothing.
    assert len(nlp.extract_calls) == 1
    # Drained via the no-entities bulk stamp (reenriched_at only) — NOT the reenriched
    # write, so entities_resolved_at is left untouched.
    drained = _executed(pool, reenrich_ner._STAMP_NO_ENTITIES_BULK_SQL)
    assert len(drained) == 1
    assert drained[0][2][0] == [row["id"]]
    assert _executed(pool, reenrich_ner._WRITE_REENRICHED_SQL) == []


# ---------------------------------------------------------------------------
# Degrade — poison row (healthy tick) vs all-fail (outage)
# ---------------------------------------------------------------------------


async def test_poison_row_stamped_failed_when_tick_healthy():
    good = _row({"text": "Vladimir Putin met Joe Biden"}, source_id="telegram_a")
    poison = _row({"text": "this row is POISON"}, source_id="telegram_b")
    pool = _FakePool([good, poison])
    nlp = _FakeNlp(triples=_GOOD_TRIPLES, fail_marker="POISON")
    deps = _deps_with_nlp(pool, nlp)

    result = await reenrich_ner.handle(
        [], {"sub_handler": SUB, "analyst_id": "rn", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["reenriched"] == 1
    assert data["failures"] == 1
    # The healthy row was written; the poison row drained via the failed sentinel.
    assert len(_executed(pool, reenrich_ner._WRITE_REENRICHED_SQL)) == 1
    failed = _executed(pool, reenrich_ner._STAMP_FAILED_BULK_SQL)
    assert len(failed) == 1
    assert failed[0][2][0] == [poison["id"]]


async def test_all_fail_tick_leaves_rows_unstamped():
    a = _row({"text": "POISON one"}, source_id="telegram_a")
    b = _row({"text": "POISON two"}, source_id="telegram_b")
    pool = _FakePool([a, b])
    nlp = _FakeNlp(triples=_GOOD_TRIPLES, fail_marker="POISON")
    deps = _deps_with_nlp(pool, nlp)

    result = await reenrich_ner.handle(
        [], {"sub_handler": SUB, "analyst_id": "rn", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["examined"] == 2
    assert data["reenriched"] == 0
    assert data["failures"] == 2
    # Nothing re-enriched → probable outage → NO stamp UPDATE ran at all (rows stay
    # reenriched_at IS NULL, retried next tick).
    assert [c for c in pool.calls if c[0] == "execute"] == []


# ---------------------------------------------------------------------------
# SQL-marker invariants (guard the write contract in plain text)
# ---------------------------------------------------------------------------


async def test_translate_failure_retried_not_drained():
    """A /translate outage bumps the handler's _translate_failures (NOT
    _signals_failed) and falls back to NER on UNtranslated non-Latin text (→ 0
    entities). That MUST be treated as a failure (retry), never drained as a
    genuine empty — else a transient NLLB blip permanently burns the very
    non-Latin rows this backfill exists to rescue. On an all-fail tick the rows
    are left UNSTAMPED for retry."""
    ru = _row({"text": "нечто важное"}, source_id="tass_news", language="ru")
    pool = _FakePool([ru])
    nlp = _FakeNlp(fail_translate=True, empty_extract=True)
    result = await reenrich_ner.handle(
        [], {"sub_handler": SUB, "analyst_id": "rn", "run_id": uuid4()},
        _deps_with_nlp(pool, nlp),
    )
    data = result.finding.data
    assert nlp.translate_calls, "the ru row should have routed through /translate"
    assert data["no_entities"] == 0, "a translate-failed row must NOT be a genuine empty"
    assert data["failures"] == 1
    # nothing re-enriched this tick → probable outage → rows left UNSTAMPED for retry
    assert _executed(pool, reenrich_ner._STAMP_NO_ENTITIES_BULK_SQL) == []
    assert _executed(pool, reenrich_ner._STAMP_FAILED_BULK_SQL) == []


def test_select_predicate_invariants():
    sql = reenrich_ner._SELECT_BATCH_SQL
    assert "reenriched_at IS NULL" in sql
    assert "jsonb_array_length" in sql          # the no-entities guard
    assert "telegram" in sql                    # the telegram candidate branch
    assert "lower(payload->>'language') = ANY" in sql  # the non-latin lang branch


def test_reenriched_write_nulls_resolution_and_stamps():
    sql = reenrich_ner._WRITE_REENRICHED_SQL
    assert "reenriched_at = now()" in sql
    assert "entities_resolved_at = NULL" in sql
    assert "entity_classes = $3::text[]" in sql
    assert "'{entities}'" in sql


def test_no_entities_stamp_does_not_null_resolution():
    sql = reenrich_ner._STAMP_NO_ENTITIES_BULK_SQL
    assert "reenriched_at = now()" in sql
    # A drain must NOT reset entities_resolved_at (no new entities to re-fold).
    assert "entities_resolved_at" not in sql


def test_failed_stamp_carries_sentinel():
    sql = reenrich_ner._STAMP_FAILED_BULK_SQL
    assert "reenriched_at = now()" in sql
    assert "reenrich_failed" in sql
