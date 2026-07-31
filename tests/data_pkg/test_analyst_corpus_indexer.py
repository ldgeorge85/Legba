# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``corpus_indexer`` deterministic sub-handler + the corpus
DIRTY-MARKER / version-guard contract it shares with ``signal_summarizer``.

The index plane of the signal-content-depth program. These tests are
DETERMINISTIC and need no live substrate / OpenSearch — they cover:

  * **Registration** — a first-class registered deterministic sub-handler.
  * **Synthetic** (``deps=None``) — no substrate → a zeroed, well-formed run.
  * ``_parse_update_count`` — parses asyncpg's ``UPDATE N`` command tag (feeds the
    ``requeued_dirty`` observability counter); never raises.
  * **The SQL contract** — the load-bearing invariants that, if silently
    regressed, reopen the lost-update race the version-guard closes:
      - the summarizer's summary write nulls ``indexed_at`` AND bumps ``updated_at``
        (both are required — see the DIRTY-MARKER contract);
      - the summarizer's failed / short stamps do NOT touch ``indexed_at`` (no
        indexable content changed → no re-index);
      - the indexer's stamp is VERSION-GUARDED (``IS NOT DISTINCT FROM`` over a
        parallel ``unnest``) and its SELECT carries ``updated_at``.
  * **Recall preservation** — ``signal_to_doc`` keeps ``raw_body`` as its own
    indexed field even when a (shorter) ``distilled_body`` takes over ``best_body``,
    so a post-summary re-index ENRICHES the doc and never shrinks search recall.
"""

from __future__ import annotations

from uuid import uuid4

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    run_method,
)
from legba.data.analysts.deterministic_handlers import corpus_indexer, signal_summarizer
from legba.data.opensearch import signal_to_doc
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult

SUB = "corpus_indexer"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_corpus_indexer_registered():
    assert SUB in SUB_HANDLERS, "corpus_indexer missing from SUB_HANDLERS"
    assert SUB in OUTPUT_KIND_BY_SUB_HANDLER
    assert SUB_HANDLERS[SUB] is corpus_indexer.handle


# ---------------------------------------------------------------------------
# Synthetic path — no substrate, zeroed run, never spends tokens
# ---------------------------------------------------------------------------


async def test_synthetic_no_deps_zeroed_run():
    result = await run_method(
        [], {"sub_handler": SUB, "analyst_id": "ci", "run_id": uuid4()}, None,
    )
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    data = result.finding.data
    assert data["sub_handler"] == SUB
    assert data["examined"] == 0
    assert data["indexed"] == 0
    assert data["requeued_dirty"] == 0
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


# ---------------------------------------------------------------------------
# _parse_update_count — parses the asyncpg command tag, never raises
# ---------------------------------------------------------------------------


def test_parse_update_count():
    assert corpus_indexer._parse_update_count("UPDATE 998") == 998
    assert corpus_indexer._parse_update_count("UPDATE 0") == 0
    # Defensive: unexpected / empty / None tags degrade to 0 (never raise).
    assert corpus_indexer._parse_update_count("") == 0
    assert corpus_indexer._parse_update_count("weird") == 0
    assert corpus_indexer._parse_update_count(None) == 0


# ---------------------------------------------------------------------------
# The SQL contract — the invariants that keep the lost-update race closed
# ---------------------------------------------------------------------------


def test_summary_write_nulls_indexed_at_and_bumps_updated_at():
    # BOTH are load-bearing (DIRTY-MARKER contract): nulling indexed_at re-queues
    # the row; bumping updated_at protects that re-null from the indexer's
    # version-guarded stamp. Missing either reopens the race.
    sql = signal_summarizer._WRITE_SUMMARY_SQL
    assert "indexed_at = NULL" in sql
    assert "updated_at = now()" in sql


def test_failed_and_short_stamps_do_not_touch_indexed_at():
    # A failed summary writes only summarize_failed; a short row writes no body —
    # no indexable content changed, so they must NOT re-queue for the corpus.
    assert "indexed_at" not in signal_summarizer._STAMP_FAILED_SQL
    assert "indexed_at" not in signal_summarizer._STAMP_SHORT_BULK_SQL


def test_indexer_stamp_is_version_guarded():
    stamp = corpus_indexer._STAMP_BULK_SQL
    assert "IS NOT DISTINCT FROM" in stamp, "the version guard must be present"
    assert "unnest(" in stamp, "id + updated_at must be passed as parallel arrays"
    # The SELECT must carry updated_at (the version token the guard compares).
    assert "updated_at" in corpus_indexer._SELECT_BATCH_SQL


# ---------------------------------------------------------------------------
# Recall preservation — a post-summary re-index enriches, never shrinks
# ---------------------------------------------------------------------------


def test_signal_to_doc_preserves_raw_body_when_distilled_present():
    raw = "x" * 4000  # the full article body
    distilled = "our short analytic brief"
    doc = signal_to_doc(
        {
            "id": uuid4(),
            "payload": {
                "title": "t",
                "raw_body": raw,
                "distilled_body": distilled,
            },
        }
    )
    # best_body flips to OUR brief (first-non-empty precedence)...
    assert doc["best_body"] == distilled
    # ...but raw_body stays its OWN indexed field, so full-text recall survives.
    assert doc["raw_body"] == raw
    assert doc["distilled_body"] == distilled


def test_signal_to_doc_best_body_falls_back_to_raw_body():
    raw = "y" * 2000
    doc = signal_to_doc({"id": uuid4(), "payload": {"raw_body": raw}})
    assert doc["best_body"] == raw
    # No distilled_body key present → it is dropped from the doc (lean).
    assert "distilled_body" not in doc


# ---------------------------------------------------------------------------
# R6 — chat-platform payload.text reaches the corpus (telegram/discord never
# populate raw_body; ~96.8% of telegram content was previously invisible)
# ---------------------------------------------------------------------------


def test_signal_to_doc_text_field_is_indexed_and_wins_best_body():
    """A telegram-shaped signal (only payload.text populated, no raw_body /
    distilled_body / archived_text) must surface its message as BOTH its own
    indexed ``text`` field and ``best_body`` — before the fix, best_body fell
    through to nothing (empty doc, invisible to search)."""
    msg = "Forces reported movement near the border crossing overnight."
    doc = signal_to_doc({"id": uuid4(), "payload": {"text": msg}})
    assert doc["text"] == msg
    assert doc["best_body"] == msg


def test_signal_to_doc_text_outranks_archived_text():
    """R6b: a telegram signal's archived_text is frequently embed-widget UI
    chrome (t.me preview pages have no article for trafilatura to find), so
    the real message body (payload.text) must win best_body over a present
    (but untrustworthy for this source shape) archived_text."""
    msg = "Convoy reportedly struck on the coastal road."
    widget_chrome = "Download\nContext\nEmbed\ntelegram-widget.js?22"
    doc = signal_to_doc(
        {"id": uuid4(), "payload": {"text": msg, "archived_text": widget_chrome}}
    )
    assert doc["best_body"] == msg
    # archived_text is still preserved as its own field (never destroyed) —
    # only demoted in the best_body preference ladder.
    assert doc["archived_text"] == widget_chrome


# ---------------------------------------------------------------------------
# R13 — a non-string body-shaped payload value must never reach OpenSearch
# ---------------------------------------------------------------------------


def test_signal_to_doc_omits_non_string_raw_body():
    """A structured (dict) payload.raw_body — e.g. a GDELT CAMEO event dump —
    must be OMITTED from the doc, never handed through as-is: OpenSearch
    rejects a dict against a ``{"type": "text"}`` mapping with a
    ``mapper_parsing_exception`` that fails the WHOLE bulk doc. No exception
    should be raised building the doc either way."""
    cameo_event = {"event_code": "190", "actor1": "GOV", "actor2": "REB", "goldstein": -10.0}
    doc = signal_to_doc({"id": uuid4(), "payload": {"raw_body": cameo_event}})
    assert "raw_body" not in doc
    # A structured, non-prose value never wins best_body either.
    assert "best_body" not in doc


def test_signal_to_doc_omits_non_string_text_and_archived_text():
    doc = signal_to_doc(
        {
            "id": uuid4(),
            "payload": {"text": ["not", "a", "string"], "archived_text": 12345},
        }
    )
    assert "text" not in doc
    assert "archived_text" not in doc
