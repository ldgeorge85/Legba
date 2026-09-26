# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The 2026-09-16 consult-review repairs, pinned.

An external review of a live Opus consult graded the reasoning excellent and
everything under it broken. Four defects, one test file:

  1. ``read_document`` reported ``count 0, refs 0`` on all three of its calls,
     including on the interview the whole answer rested on.
  2. ``search_corpus`` returned ``count 5-8`` with ``refs: 0`` on EVERY call,
     while ``vector_search`` returned refs normally.
  3. ``list_findings`` / ``query_hypotheses`` on ``situation_iran_war`` — an
     ACTIVE target — returned zero.
  4. The panel header said ``uncertainty 0.60`` over a FINAL that said
     ``0.55``.

Postgres is REAL (``migrated_pg``, per the no-mocks rule): the namespace
fallbacks, the alias resolution and the target ladder all run against the real
schema with rows this file inserts. The OpenSearch BOUNDARY is a recorded
response — the brief's own instruction ("a property test over recorded
OpenSearch responses"), and the right call regardless: the invariant under test
is a property of the PROJECTION, which must hold for every hit shape the
cluster can emit, not for the handful a live index happens to hold today.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.consult_on_demand import (
    SALVAGE_UNCERTAINTY,
    _bounded_tool_json,
    _refs_from_tool_result,
    final_payload_from_text,
)
from legba.runtime.corpus_read_projection import (
    project_corpus_hits,
    strip_markup,
)
from legba.runtime.substrate_corpus_readers import read_document, search_corpus
from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort
from legba.runtime.target_resolution import resolve_member_targets

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Recorded OpenSearch responses (captured from the live legba_signals_corpus,
# 2026-09-16; bodies trimmed, ids preserved).
# ---------------------------------------------------------------------------

#: The Vance interview — doc ``3f69f001…``, the one the live run read and could
#: not cite. Its ``raw_body`` is the source's own wrapped <article> HTML, which
#: is exactly why the reader must flatten before it spends a body budget.
VANCE_ID = "3f69f001-e65c-48ac-b5a3-99f128ecbcee"
VANCE_SOURCE = {
    "title": "Vance says US war on Iran will enter 'much different phase'",
    "raw_body": (
        '<article class="live-blog-update"><div class="content">'
        "<p>US Vice President JD Vance said that Washington has completed its "
        "objectives for the&nbsp;first phase of the war on iran.</p>"
        "<p>“There are really two phases to this thing, and the first "
        "phase is done,” Vance said.</p></div></article>"
    ),
    "archived_text": (
        "Vance says US war on Iran will enter 'much different phase'\n"
        "US Vice President JD Vance said that Washington has completed its "
        "objectives for the first phase of the war on iran."
    ),
    "source_id": "source.middleeasteye.news",
    "canonical_url": "https://www.middleeasteye.net/live-blog/vance",
    "language": "en",
    "geo": ["IR", "US"],
    "fetched_at": "2026-09-16T00:34:24+00:00",
}


def _recorded_hits(n: int = 8, *, body_chars: int = 6000) -> list[dict]:
    """``OpenSearchStore.search``-shaped hits with LIVE-SIZED bodies.

    The live 8-hit response measured 45 KB. Sizing the recording the same way
    is the point of the test: a lean recording would pass the old code too.
    """
    return [
        {
            "id": str(UUID(int=i + 1)),
            "score": 10.0 - i,
            "source": {
                "title": f"Recorded corpus doc {i}",
                "raw_body": "<p>" + ("shadow fleet tanker " * (body_chars // 20)) + "</p>",
                "source_id": "source.recorded",
                "geo": ["IR"],
                "fetched_at": "2026-09-16T00:00:00+00:00",
            },
        }
        for i in range(n)
    ]


class _RecordedStore:
    """The OpenSearch BOUNDARY, replaying a recorded response.

    Not a stand-in for the substrate (Postgres below is real) — this is the
    external search cluster, and the reader's contract with it is exactly two
    calls wide: ``connect`` then ``search`` / ``get``.
    """

    def __init__(self, *, hits=None, docs=None, fail: bool = False):
        self._hits = hits if hits is not None else []
        self._docs = docs or {}
        self._fail = fail
        self.searches: list[tuple] = []

    async def connect(self):
        return None

    async def search(self, index, query, *, filters=None, size=10):
        if self._fail:
            raise RuntimeError("cluster unreachable")
        self.searches.append((index, query, filters, size))
        return list(self._hits)[:size]

    async def get(self, index, doc_id):
        if self._fail:
            raise RuntimeError("cluster unreachable")
        return self._docs.get(str(doc_id))


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    yield pool
    await pool.close()


# ---------------------------------------------------------------------------
# Defect 2 — a hit MUST carry a ref, or be dropped with a counted reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 5, 8, 20])
async def test_search_corpus_hits_always_carry_refs(n):
    """THE INVARIANT, over every response size the reader can be handed.

    The live failure was ``count 5-8`` with ``refs: 0`` on every call — hits
    the model could weigh but never cite. ``len(refs) == count`` makes an
    empty-refs-with-hits result unconstructible.
    """
    store = _RecordedStore(hits=_recorded_hits(n))
    out = await search_corpus(store, "legba_signals_corpus", query="q", size=50)

    assert out["count"] == n
    assert len(out["refs"]) == out["count"]
    assert len(out["rows"]) == out["count"]
    for row, ref in zip(out["rows"], out["refs"]):
        assert row["id"] == ref
        UUID(ref)  # every ref is a real substrate UUID
    # And the refs actually reach the consult's citation lift.
    assert len(_refs_from_tool_result(out)) == n


async def test_search_corpus_drops_uncitable_hits_with_a_counted_reason():
    """A hit that cannot yield a ref is DROPPED and NAMED — never counted as a
    result the planner is unable to cite, and never silently vanished."""
    hits = [
        {"id": str(uuid4()), "score": 1.0, "source": {"title": "citable"}},
        {"id": "not-a-uuid", "score": 0.9, "source": {"title": "corpus junk"}},
        {"id": None, "score": 0.8, "source": {"title": "no id at all"}},
        "malformed-hit-entirely",
    ]
    store = _RecordedStore(hits=hits)
    out = await search_corpus(store, "idx", query="q")

    assert out["count"] == 1
    assert len(out["refs"]) == 1
    assert out["dropped_count"] == 3
    reasons = {d["reason"] for d in out["dropped"]}
    assert reasons == {"no_citable_id", "malformed_hit"}


async def test_search_corpus_duplicate_hit_ids_are_deduped_not_double_counted():
    ref = str(uuid4())
    store = _RecordedStore(hits=[
        {"id": ref, "score": 2.0, "source": {"title": "first"}},
        {"id": ref, "score": 1.0, "source": {"title": "same doc again"}},
    ])
    out = await search_corpus(store, "idx", query="q")
    assert out["count"] == 1
    assert out["refs"] == [ref]
    assert out["dropped"][0]["reason"] == "duplicate_id"


async def test_search_corpus_result_survives_the_conversation_bound():
    """The second half of defect 2: hits that reached the loop but not the model.

    A live 8-hit response is ~45 KB of raw ``_source``. Handed to the
    conversation at :func:`_bounded_tool_json`'s 8 KB budget, five of the eight
    rows were silently dropped. The projection keeps them all AND keeps every
    ref, because the citation-only raw bodies are shed first.
    """
    store = _RecordedStore(hits=_recorded_hits(8))
    out = await search_corpus(store, "idx", query="q")
    raw_size = len(json.dumps(out))
    assert raw_size > 8000, "recording must be live-sized or this proves nothing"

    message = json.loads(_bounded_tool_json(out, 8000))
    assert "raw_prefix" not in message, "the model must get JSON, not a chop"
    assert len(message["rows"]) == 8, "every hit the planner was told it got"
    assert message["citation_payload_omitted"] is True


async def test_search_corpus_degrades_without_a_corpus_and_on_failure():
    """Degrade-not-break — and BOTH degradations still carry the refs key, so a
    consumer never has to special-case their shape."""
    unwired = await search_corpus(None, "idx", query="q")
    assert unwired["status"] == "no_corpus_wired"
    assert unwired["refs"] == [] and unwired["count"] == 0

    broken = await search_corpus(_RecordedStore(fail=True), "idx", query="q")
    assert "corpus_search_failed" in broken["error"]
    assert broken["refs"] == [] and broken["count"] == 0


def test_project_corpus_hits_is_pure_and_total():
    """The invariant holds for arbitrary junk, not just well-formed hits."""
    for hits in ([], [{}], [{"id": ""}], [{"id": 5}], [{"source": "str"}]):
        out = project_corpus_hits(hits)
        assert len(out["refs"]) == out["count"] == len(out["rows"])


def test_strip_markup_flattens_source_html():
    flat = strip_markup(VANCE_SOURCE["raw_body"])
    assert "<" not in flat and "&nbsp;" not in flat
    assert "first phase of the war on iran" in flat


# ---------------------------------------------------------------------------
# Defect 1 — read_document, across every id namespace a planner can hold
# ---------------------------------------------------------------------------


async def test_read_document_by_corpus_signal_id_is_citable_and_readable(pg_pool):
    """The exact live call that reported ``count 0, refs 0``."""
    store = _RecordedStore(docs={VANCE_ID: VANCE_SOURCE})
    out = await read_document(store, "idx", pg_pool, doc_id=VANCE_ID)

    assert out["status"] == "found"
    assert out["count"] == 1
    assert out["refs"] == [VANCE_ID], "the doc it read is the doc it can cite"
    assert _refs_from_tool_result(out) == [UUID(VANCE_ID)]
    body = out["document"]["body"]
    assert "completed its objectives for the first phase" in body
    assert "<article" not in body, "markup flattened, not spent on the budget"
    # The RAW source stays on the document for the GATHER [N] grounding path
    # (inline_target._citation_entry's faithfulness trust boundary).
    assert out["document"]["archived_text"].startswith("Vance says")


async def test_read_document_of_a_long_article_reaches_the_model_intact(pg_pool):
    """The 52-55 KB docs the live run asked for came back as a ``raw_prefix``
    chop of mid-JSON HTML. Now the cut is declared in-band and the message is
    parseable JSON with a real body in it."""
    source = dict(VANCE_SOURCE, archived_text="long article. " * 6000)
    store = _RecordedStore(docs={VANCE_ID: source})
    out = await read_document(store, "idx", pg_pool, doc_id=VANCE_ID)

    assert out["document"]["body_truncated"] is True
    assert out["document"]["body_chars_total"] > out["document"]["body_chars"]
    message = json.loads(_bounded_tool_json(out, 8000))
    assert "raw_prefix" not in message
    assert message["document"]["body"].startswith("long article.")
    assert message["refs"] == [VANCE_ID]


async def test_read_document_falls_back_to_an_unindexed_signal_row(pg_pool):
    """A signal the corpus has not indexed yet is still a readable document —
    ``not_found`` for a row that is right there is a lie."""
    sid = uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO signals (id, source_id, modality, payload) "
            "VALUES ($1, 'source.test', 'text', $2::jsonb)",
            sid,
            json.dumps({"title": "Un-indexed", "raw_body": "<p>body text</p>"}),
        )
    out = await read_document(_RecordedStore(), "idx", pg_pool, doc_id=str(sid))

    assert out["status"] == "found"
    assert out["origin"] == "signals"
    assert out["refs"] == [str(sid)]
    assert out["document"]["body"] == "body text"


async def test_read_document_resolves_a_dedup_alias_to_its_canonical_row(pg_pool):
    """An alias id must not read as a missing document: the analytic slice
    reads the canonical row, so this reader answers with that row and says so."""
    canonical, alias = uuid4(), uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO signals (id, source_id, modality, payload) "
            "VALUES ($1, 'source.test', 'text', $2::jsonb)",
            canonical, json.dumps({"title": "Canonical", "text": "the real body"}),
        )
        await conn.execute(
            "INSERT INTO signals (id, source_id, modality, payload, "
            "canonical_signal_id) VALUES ($1, 'source.test', 'text', "
            "$2::jsonb, $3)",
            alias, json.dumps({"title": "Dup"}), canonical,
        )
    out = await read_document(_RecordedStore(), "idx", pg_pool, doc_id=str(alias))

    assert out["status"] == "found"
    assert out["origin"] == "signals_canonical"
    assert out["alias_of"] == str(canonical)
    assert out["requested_doc_id"] == str(alias)
    assert out["document"]["body"] == "the real body"


async def test_read_document_reads_a_finding_id(pg_pool):
    """A planner holding a ``list_findings`` ref will call this on it. That id
    is not in the corpus; the row is still real, and ``origin`` marks it as the
    platform's own synthesis rather than a source document."""
    fid = uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, body, analyst_id, "
            "target_id, confidence, schema_uri) VALUES ($1, 'finding', "
            "'A finding', 'The assessed body.', 'escalation', "
            "'desk_not_under_test', 0.7, "
            "'iglu:legba/finding/jsonschema/1-0-0')",
            fid,
        )
    out = await read_document(_RecordedStore(), "idx", pg_pool, doc_id=str(fid))

    assert out["status"] == "found"
    assert out["origin"] == "analyst_output"
    assert out["refs"] == [str(fid)]
    assert out["document"]["body"] == "The assessed body."
    assert out["document"]["target_id"] == "desk_not_under_test"


async def test_read_document_misses_and_bad_ids_are_honest(pg_pool):
    """Absence stays absence — and every shape carries refs/count so no
    consumer has to special-case it."""
    missing = await read_document(
        _RecordedStore(), "idx", pg_pool, doc_id=str(uuid4()),
    )
    assert missing["status"] == "not_found"
    assert missing["refs"] == [] and missing["count"] == 0

    bad = await read_document(_RecordedStore(), "idx", pg_pool, doc_id="a title")
    assert bad["status"] == "invalid_doc_id"
    assert bad["refs"] == [] and bad["count"] == 0
    assert "substrate UUID" in bad["error"]


async def test_read_document_survives_a_dead_cluster_via_postgres(pg_pool):
    """A cluster outage degrades to the Postgres namespace rather than erroring
    the consult round out."""
    sid = uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO signals (id, source_id, modality, payload) "
            "VALUES ($1, 'source.test', 'text', $2::jsonb)",
            sid, json.dumps({"title": "T", "text": "still readable"}),
        )
    out = await read_document(
        _RecordedStore(fail=True), "idx", pg_pool, doc_id=str(sid),
    )
    assert out["status"] == "found"
    assert out["document"]["body"] == "still readable"


# ---------------------------------------------------------------------------
# Defect 3 — a situation target resolves to its constituent evidence
# ---------------------------------------------------------------------------


async def _seed_targets(conn) -> None:
    """The live topology in miniature: a region frame with tag-members, and a
    SITUATION frame that has neither members nor producers — the orphan shape
    ``situation_iran_war`` actually has in production."""
    rows = [
        ("country_watch_ir", {
            "identity": {"id": "country_watch_ir", "name": "Iran"},
            "scope": {"geo": ["IR"], "tags": ["watch", "region_mena"]},
        }),
        ("country_g20_sa", {
            "identity": {"id": "country_g20_sa", "name": "Saudi Arabia"},
            "scope": {"geo": ["SA"], "tags": ["g20", "region_mena"]},
        }),
        ("region_mena", {
            "identity": {"id": "region_mena", "name": "MENA"},
            "scope": {"geo": [], "tags": ["region"], "themes": ["mena"]},
        }),
        ("situation_orphan", {
            "identity": {"id": "situation_orphan", "name": "Orphan frame"},
            "scope": {
                "geo": [], "tags": ["thematic", "situation"],
                "themes": ["unmapped_subject"],
                "predicate": 'contains_any(["nothing"])',
            },
        }),
        ("situation_iran_war", {
            "identity": {"id": "situation_iran_war", "name": "US-Iran War"},
            "scope": {
                "geo": [], "tags": ["thematic", "situation", "iran"],
                "themes": ["iran", "war"],
                "predicate": 'contains_any(["iran"]) and contains_any(["war"])',
            },
        }),
    ]
    for did, body in rows:
        await conn.execute(
            "INSERT INTO target_descriptors (descriptor_id, version, "
            "schema_uri, owner, name, body, is_head, state) "
            "VALUES ($1, 'v1', 'legba/target/2.0.0', 'test', $1, "
            "$2::jsonb, TRUE, 'active') ON CONFLICT DO NOTHING",
            did, json.dumps(body),
        )
    await conn.execute(
        "INSERT INTO iso_countries (iso2, iso3, numeric, name, official) "
        "VALUES ('IR', 'IRN', '364', 'Iran, Islamic Republic of', "
        "'Islamic Republic of Iran') ON CONFLICT DO NOTHING"
    )


async def test_situation_frame_resolves_to_its_constituent_desks(pg_pool):
    """The ladder: tag membership, then shared geo, then the gazetteer.

    ``situation_iran_war`` is reached by the last rung — theme ``iran`` → ``IR``
    → the desks covering it — which is precisely the desk the live consult had
    to find by hand after the tool told it there was nothing there.
    """
    async with pg_pool.acquire() as conn:
        await _seed_targets(conn)

        region = await resolve_member_targets(conn, "region_mena")
        assert region["via"] == "tag_membership"
        assert set(region["members"]) == {"country_watch_ir", "country_g20_sa"}

        situation = await resolve_member_targets(conn, "situation_iran_war")
        assert situation["exists"] is True
        assert situation["via"] == "gazetteer_geo"
        assert situation["members"] == ["country_watch_ir"]
        assert situation["scope_predicate"]


async def test_unknown_target_is_named_as_unknown_not_as_empty(pg_pool):
    """"No rows" and "no such target" are different answers to an analyst, and
    conflating them is what turns a typo into "the world is quiet"."""
    async with pg_pool.acquire() as conn:
        await _seed_targets(conn)
        out = await resolve_member_targets(conn, "situation_typo")
    assert out["exists"] is False
    assert "country_watch_ir" in out["known_target_ids"]


async def test_list_findings_on_a_situation_target_returns_the_desk_findings(pg_pool):
    """End to end through the port: the live call that returned zero."""
    async with pg_pool.acquire() as conn:
        await _seed_targets(conn)
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, body, analyst_id, "
            "target_id, confidence, schema_uri) VALUES ($1, 'finding', "
            "'Hormuz escalation', 'body', 'escalation', 'country_watch_ir', "
            "0.8, 'iglu:legba/finding/jsonschema/1-0-0')",
            uuid4(),
        )
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pg_pool, qdrant_client=None)

    direct = await port.list_findings(target_id="country_watch_ir")
    assert direct["count"] >= 1
    assert "status" not in direct, "a target with rows never widens"

    widened = await port.list_findings(target_id="situation_iran_war")
    assert widened["count"] == direct["count"]
    assert widened["status"] == "resolved_to_member_targets"
    assert widened["resolved_targets"] == ["country_watch_ir"]
    assert widened["resolved_via"] == "gazetteer_geo"
    assert all(r["target_id"] == "country_watch_ir" for r in widened["rows"])
    assert "the desks covering its scope" in widened["note"]

    unknown = await port.list_findings(target_id="situation_typo")
    assert unknown["count"] == 0
    assert unknown["status"] == "unknown_target"
    assert "list_targets" in unknown["note"]


async def test_query_hypotheses_on_a_situation_target_resolves_too(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed_targets(conn)
        await conn.execute(
            "INSERT INTO hypotheses (id, thesis, counter_thesis, status, "
            "target_id, analyst_id) VALUES ($1, 'Regeneration', 'Collapse', "
            "'active', 'country_watch_ir', 'competing_hypotheses')",
            uuid4(),
        )
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pg_pool, qdrant_client=None)

    out = await port.query_hypotheses(target_id="situation_iran_war")
    assert out["count"] >= 1
    assert out["status"] == "resolved_to_member_targets"
    assert "Regeneration" in [r["thesis"] for r in out["rows"]]


async def test_an_empty_frame_names_itself_rather_than_reading_as_quiet(pg_pool):
    """A frame that resolves to NO desks still must not answer with a bare
    empty set: it names itself as an empty frame and hands over its scope
    predicate, so the planner reads the raw slice instead of concluding the
    subject is quiet."""
    async with pg_pool.acquire() as conn:
        await _seed_targets(conn)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pg_pool, qdrant_client=None)
    out = await port.list_findings(target_id="situation_orphan")

    assert out["count"] == 0
    assert out["status"] == "no_rows_for_target"
    assert out["scope_predicate"] == 'contains_any(["nothing"])'
    assert "do NOT read this empty result as quiet" in out["note"]


# ---------------------------------------------------------------------------
# Defect 4 — one uncertainty, one owner
# ---------------------------------------------------------------------------


NARRATED_FINAL = """I now have enough to write a full assessment. The \
munitions-stockpile constraint is the key piece. Let me compose the final answer.

<<<FINAL>>>
uncertainty: 0.55
cited_refs: 3f69f001-e65c-48ac-b5a3-99f128ecbcee
unanswered_aspects: no order-of-battle confirmation

## Bottom line
The first phase is done; the second is a regeneration race."""


def test_a_narrated_final_keeps_the_models_own_uncertainty_and_refs():
    """Turn ``4835dfa8``, verbatim in shape.

    A reasoning planner narrated one sentence before the sentinel. The lead-only
    rule bounced it, the salvage stamped 0.60 over an answer that said 0.55, and
    fifteen deliberately chosen ``cited_refs`` were discarded. All three
    followed from the same rejected parse.
    """
    payload = final_payload_from_text(NARRATED_FINAL)

    assert payload["final"] is True
    assert payload["uncertainty"] == 0.55, "the model's number, not a default"
    assert payload["cited_refs"] == [VANCE_ID]
    assert payload["unanswered_aspects"] == ["no order-of-battle confirmation"]
    assert payload["answer"].startswith("## Bottom line")
    assert "Let me compose" not in payload["answer"], "the preamble is not answer"


def test_a_leading_sentinel_still_parses_exactly_as_before():
    payload = final_payload_from_text(
        "<<<FINAL>>>\nuncertainty: 0.2\n\n## Answer\nbody"
    )
    assert payload["uncertainty"] == 0.2
    assert payload["answer"] == "## Answer\nbody"


def test_a_quoted_contract_does_not_hijack_the_final():
    """The old lead rule existed to stop a model quoting the contract back at
    us. A fenced sentinel is a quotation by construction and stays one; a real
    final after it still wins."""
    raw = (
        "The contract asks for:\n"
        "```\n<<<FINAL>>>\nuncertainty: 0.9\n```\n"
        "and here is mine.\n\n"
        "<<<FINAL>>>\nuncertainty: 0.3\n\n## Real answer\nprose"
    )
    payload = final_payload_from_text(raw)
    assert payload["uncertainty"] == 0.3
    assert payload["answer"] == "## Real answer\nprose"


def test_headerless_prose_lifts_its_headers_rather_than_contradicting_them():
    """No sentinel at all, but the model still wrote the metadata. The number
    the operator reads in the answer IS the number the header reports."""
    payload = final_payload_from_text(
        "uncertainty: 0.42\ncited_refs: " + VANCE_ID + "\n\n## Answer\nprose"
    )
    assert payload["uncertainty"] == 0.42
    assert payload["cited_refs"] == [VANCE_ID]
    assert payload["answer"] == "## Answer\nprose"


def test_the_salvage_default_is_a_single_named_constant():
    """When the model genuinely wrote no metadata there is ONE default, and it
    is named — not two ``0.6`` literals in two arms of the loop."""
    payload = final_payload_from_text("Just prose, no metadata at all.")
    assert payload["uncertainty"] == SALVAGE_UNCERTAINTY
    assert payload["answer"] == "Just prose, no metadata at all."


def test_a_legacy_json_final_still_parses():
    payload = final_payload_from_text(
        json.dumps({"final": True, "answer": "legacy", "uncertainty": 0.11})
    )
    assert payload["uncertainty"] == 0.11
    assert payload["answer"] == "legacy"
